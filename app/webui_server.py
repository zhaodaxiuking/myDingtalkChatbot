import csv
import ctypes
import html
import io
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
from cgi import FieldStorage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config_loader import load_config, build_task_config, list_tasks, add_task, remove_task, toggle_task_enabled, update_task
from app.secret_store import (
    build_dingtalk_secret_status,
    build_robot_secret_status,
    load_dingtalk_secrets,
    migrate_config_secrets,
    save_dingtalk_secrets,
    save_robot_secrets,
    delete_robot_secrets,
)
from app.task_runner import run_task, test_upload_only

CONFIG_PATH = ROOT / 'config' / 'config.json'
SCHEDULER_STATUS_PATH = ROOT / 'output' / 'scheduler_status.json'
SCHEDULER_LOCK_PATH = ROOT / 'output' / 'scheduler.lock'
SCHEDULER_SCRIPT = ROOT / 'scripts' / 'scheduler.py'
WEBUI_STATE_PATH = ROOT / 'output' / 'webui_state.json'
SEND_LOGS_PATH = ROOT / 'output' / 'send_logs.json'
WEBUI_ERROR_LOG_PATH = ROOT / 'output' / 'webui_errors.log'
WEBUI_RUNTIME_PATH = ROOT / 'output' / 'webui_runtime.json'
BROWSER_LAUNCH_LOG_PATH = ROOT / 'output' / 'browser_launch.log'
KEEP_VALUE = '__KEEP__'
WEBUI_SERVER = None
CDP_URL = 'http://127.0.0.1:18810'
CHROME_CANDIDATES = [
    r'C:\Program Files\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
    str(Path.home() / r'AppData\Local\Google\Chrome\Application\chrome.exe'),
    r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
]


def slugify(text):
    base = re.sub(r'[^a-zA-Z0-9\u4e00-\u9fff_-]+', '-', str(text or '').strip()).strip('-')
    return base or 'task'


def now_text():
    return time.strftime('%Y-%m-%d %H:%M:%S')


def load_json(path, default):
    path = Path(path)
    if not path.exists():
        return json.loads(json.dumps(default, ensure_ascii=False))
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return json.loads(json.dumps(default, ensure_ascii=False))


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def append_webui_error_log(message):
    WEBUI_ERROR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with WEBUI_ERROR_LOG_PATH.open('a', encoding='utf-8') as f:
        f.write(f'[{now_text()}] {message}\n\n')


def load_webui_state():
    return load_json(WEBUI_STATE_PATH, {
        'auto_start_scheduler': True,
        'last_restart_at': '',
    })


def save_webui_state(data):
    save_json(WEBUI_STATE_PATH, data)


def load_webui_runtime():
    return load_json(WEBUI_RUNTIME_PATH, {
        'pid': None,
        'host': '127.0.0.1',
        'port': 8787,
        'started_at': '',
        'command': '',
    })


def save_webui_runtime(data):
    save_json(WEBUI_RUNTIME_PATH, data)


def clear_webui_runtime():
    try:
        WEBUI_RUNTIME_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def _cdp_http_ready(url=CDP_URL, timeout=1.5):
    parsed = urlparse(url)
    host = parsed.hostname or '127.0.0.1'
    port = parsed.port or 18800
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def find_browser_executable():
    for path in CHROME_CANDIDATES:
        if Path(path).exists():
            return path
    return None


def _run_powershell(script):
    return subprocess.run(
        ['powershell', '-NoProfile', '-Command', script],
        capture_output=True,
        text=True,
        check=False,
    )


def _kill_pid(pid):
    if not pid:
        return False
    try:
        subprocess.run(['taskkill', '/PID', str(int(pid)), '/F'], check=True, capture_output=True, text=True)
        return True
    except Exception:
        return False


def _find_project_browser_pids():
    profile_dir = str((ROOT / 'output' / 'cdp_browser_profile').resolve()).replace("'", "''")
    script = (
        "$items=Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -match '^(chrome|msedge)\\.exe$' -and $_.CommandLine -like '*--remote-debugging-port=18810*' -and $_.CommandLine -like '*"
        + profile_dir +
        "*' }; "
        "foreach($p in $items){ Write-Output $p.ProcessId }"
    )
    result = _run_powershell(script)
    pids = []
    for line in (result.stdout or '').splitlines():
        text = line.strip()
        if text.isdigit():
            pids.append(int(text))
    return pids


def stop_capture_browser():
    pids = _find_project_browser_pids()
    if not pids:
        return '截图浏览器未运行'
    killed = []
    failed = []
    for pid in pids:
        if _kill_pid(pid):
            killed.append(str(pid))
        else:
            failed.append(str(pid))
    if failed:
        return f"截图浏览器部分关闭失败，已关闭 PID={', '.join(killed) or '无'}；失败 PID={', '.join(failed)}"
    return f"已关闭截图浏览器，PID={', '.join(killed)}"


def _clear_browser_profile_locks(user_data_dir):
    removed = []
    for name in ('SingletonLock', 'SingletonCookie', 'SingletonSocket'):
        path = Path(user_data_dir) / name
        try:
            if path.exists():
                path.unlink()
                removed.append(name)
        except Exception:
            pass
    return removed


def _bring_pid_window_to_front(pid, timeout_seconds=8.0):
    if os.name != 'nt' or not pid:
        return False
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
    except Exception:
        return False

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    handles = []

    def callback(hwnd, lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            proc_id = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc_id))
            if int(proc_id.value) == int(pid):
                handles.append(hwnd)
                return False
        except Exception:
            return True
        return True

    deadline = time.time() + max(float(timeout_seconds or 0), 1.0)
    while time.time() < deadline:
        handles.clear()
        user32.EnumWindows(WNDENUMPROC(callback), 0)
        if handles:
            hwnd = handles[0]
            try:
                user32.ShowWindow(hwnd, 9)
                user32.ShowWindow(hwnd, 5)
                user32.SetForegroundWindow(hwnd)
                return True
            except Exception:
                return False
        time.sleep(0.3)
    return False


def ensure_capture_browser_open():
    if _cdp_http_ready():
        return {'ok': True, 'already_running': True}

    browser_path = find_browser_executable()
    if not browser_path:
        raise RuntimeError('未找到可用的 Chrome / Edge 浏览器，无法启动截图浏览器。')

    cfg = read_raw_config()
    default_alidocs_url = str((cfg.get('defaults', {}) or {}).get('alidocs_url') or '').strip()
    target_url = default_alidocs_url or 'https://alidocs.dingtalk.com/'

    user_data_dir = ROOT / 'output' / 'cdp_browser_profile'
    user_data_dir.mkdir(parents=True, exist_ok=True)
    removed_locks = _clear_browser_profile_locks(user_data_dir)
    args = [
        browser_path,
        '--new-window',
        '--remote-debugging-port=18810',
        '--remote-debugging-address=127.0.0.1',
        f'--user-data-dir={str(user_data_dir)}',
        '--no-first-run',
        '--no-default-browser-check',
        '--start-maximized',
        target_url,
    ]
    creationflags = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
    BROWSER_LAUNCH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with BROWSER_LAUNCH_LOG_PATH.open('a', encoding='utf-8') as browser_log:
        browser_log.write(f"[{now_text()}] launching browser: {args}\n")
        if removed_locks:
            browser_log.write(f"[{now_text()}] removed stale locks: {', '.join(removed_locks)}\n")
        proc = subprocess.Popen(
            args,
            cwd=str(ROOT),
            stdout=browser_log,
            stderr=browser_log,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
            close_fds=True,
        )
        deadline = time.time() + 15
        while time.time() < deadline:
            if _cdp_http_ready():
                browser_log.write(f"[{now_text()}] cdp ready on 18810\n")
                brought_front = _bring_pid_window_to_front(proc.pid)
                browser_log.write(f"[{now_text()}] bring-to-front={'ok' if brought_front else 'failed'} pid={proc.pid}\n")
                browser_log.flush()
                return {'ok': True, 'already_running': False, 'url': target_url, 'pid': proc.pid, 'brought_front': brought_front}
            exit_code = proc.poll()
            if exit_code is not None:
                browser_log.write(f"[{now_text()}] browser exited early with code={exit_code}\n")
                browser_log.flush()
                raise RuntimeError(f'截图浏览器启动失败：浏览器进程已提前退出（exit={exit_code}）。请查看 {BROWSER_LAUNCH_LOG_PATH.name}')
            time.sleep(0.5)
    raise RuntimeError('截图浏览器启动失败：18810 调试端口未就绪。请确认本机浏览器可正常启动，并查看 browser_launch.log。')


def update_auto_start_scheduler(enabled):
    state = load_webui_state()
    state['auto_start_scheduler'] = bool(enabled)
    save_webui_state(state)
    if state['auto_start_scheduler'] and has_enabled_tasks():
        return start_scheduler(force=False)
    if not state['auto_start_scheduler']:
        return stop_scheduler()
    return '已保存自动启动调度器设置'


def load_send_logs():
    data = load_json(SEND_LOGS_PATH, [])
    return data if isinstance(data, list) else []


def append_send_log(task_id, robot_id='', success=True, triggered_by='manual', action='run', detail='', error=''):
    logs = load_send_logs()
    logs.insert(0, {
        'id': len(logs) + 1,
        'task_id': task_id,
        'robot_id': robot_id,
        'success': bool(success),
        'triggered_by': triggered_by,
        'action': action,
        'detail': detail,
        'error': error,
        'send_time': now_text(),
    })
    logs = logs[:500]
    for idx, row in enumerate(logs, start=1):
        row['id'] = idx
    save_json(SEND_LOGS_PATH, logs)
    return logs[0]


def _collect_browser_target_logs(payload):
    entries = []

    def walk(obj):
        if isinstance(obj, dict):
            capture = obj.get('capture') or {}
            bt = capture.get('browserTarget') or {}
            matched_url = (bt.get('matchedUrl') or '').strip()
            matched_title = (bt.get('matchedTitle') or '').strip()
            frame_url = (bt.get('frameUrl') or '').strip()
            used_iframe = bt.get('usedIframe')
            if matched_url or matched_title or frame_url or used_iframe is not None:
                task_name = obj.get('task_name') or obj.get('task_id') or obj.get('robot_id') or payload.get('task_id') or ''
                parts = []
                if task_name:
                    parts.append(str(task_name))
                if matched_title:
                    parts.append(f'title={matched_title}')
                if matched_url:
                    parts.append(f'url={matched_url}')
                if used_iframe is not None:
                    parts.append(f'iframe={"yes" if used_iframe else "no"}')
                if frame_url:
                    parts.append(f'frame_url={frame_url}')
                entries.append(' | '.join(parts))
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(payload)
    seen = []
    for item in entries:
        if item not in seen:
            seen.append(item)
    return seen


def _append_browser_target_detail(detail, payload):
    lines = _collect_browser_target_logs(payload)
    if not lines:
        return detail
    browser_detail = '命中页面: ' + ' || '.join(lines)
    if detail:
        return f'{detail} | {browser_detail}'
    return browser_detail


def _pid_exists(pid):
    if not pid:
        return False
    try:
        result = subprocess.run(
            ['tasklist', '/FI', f'PID eq {int(pid)}'],
            capture_output=True,
            text=True,
            check=False,
        )
        text = (result.stdout or '') + (result.stderr or '')
        return str(pid) in text and 'python.exe' in text.lower()
    except Exception:
        return False


def _read_process_commandline(pid):
    if not pid:
        return ''
    try:
        result = subprocess.run(
            ['powershell', '-NoProfile', '-Command', f"(Get-CimInstance Win32_Process -Filter \"ProcessId = {int(pid)}\").CommandLine"],
            capture_output=True,
            text=True,
            check=False,
        )
        return (result.stdout or '').strip()
    except Exception:
        return ''


def is_same_webui_process(pid):
    if not _pid_exists(pid):
        return False
    cmdline = _read_process_commandline(pid).lower().replace('/', '\\')
    return 'app\\webui_server.py' in cmdline


def ensure_single_webui_instance(host='127.0.0.1', port=8787):
    runtime = load_webui_runtime()
    pid = runtime.get('pid')
    if pid and int(pid) == int(getattr(__import__('os'), 'getpid')()):
        return None
    if pid and is_same_webui_process(pid):
        return f'WebUI 已在运行中，PID={pid}，地址 http://{runtime.get("host") or host}:{runtime.get("port") or port}'
    if pid and not _pid_exists(pid):
        clear_webui_runtime()
    elif pid and _pid_exists(pid) and not is_same_webui_process(pid):
        clear_webui_runtime()
    return None


def ensure_config_shape(cfg):
    defaults = cfg.setdefault('defaults', {})
    defaults.setdefault('app_settings', {})
    defaults.setdefault('browser', {})
    defaults.setdefault('robot_configs', [])
    defaults.setdefault('default_robot_id', '')
    defaults.setdefault('message', {})
    defaults.setdefault('capture', {})
    defaults.setdefault('dingtalk', {})
    cfg.setdefault('tasks', [])
    return cfg


def read_raw_config():
    cfg = load_json(CONFIG_PATH, {})
    return ensure_config_shape(cfg)


def write_raw_config(cfg):
    save_json(CONFIG_PATH, ensure_config_shape(cfg))


def save_robot_config(form):
    cfg = read_raw_config()
    defaults = cfg.setdefault('defaults', {})
    robots = defaults.setdefault('robot_configs', [])
    robot_id = (form.get('robot_id') or [''])[0].strip() or slugify((form.get('robot_name') or ['robot'])[0])
    name = (form.get('robot_name') or [''])[0].strip() or robot_id
    enabled = (form.get('robot_enabled') or ['on'])[0] == 'on'
    webhook = (form.get('robot_webhook') or [''])[0].strip()
    secret = (form.get('robot_secret') or [''])[0].strip()
    legacy_imgbed_upload_url = (form.get('imgbed_upload_url') or [''])[0].strip()
    if legacy_imgbed_upload_url:
        append_webui_error_log(f'忽略机器人表单中的旧图床字段，robot_id={robot_id}')
    existing = next((x for x in robots if x.get('id') == robot_id), None)
    if existing is None:
        existing = {
            'id': robot_id,
            'name': name,
            'enabled': enabled,
            'message_mode': 'markdown',
        }
        robots.append(existing)
    else:
        existing['name'] = name
        existing['enabled'] = enabled
    save_robot_secrets(robot_id, webhook=webhook if webhook != KEEP_VALUE else None, secret=secret if secret != KEEP_VALUE else None)
    if not defaults.get('default_robot_id'):
        defaults['default_robot_id'] = robot_id
    write_raw_config(cfg)
    return robot_id


def normalize_robot_ids_from_form(form):
    values = []
    for key in ('robot_ids', 'robot_id'):
        for value in form.get(key, []) or []:
            text = str(value or '').strip()
            if text:
                values.append(text)
    deduped = []
    seen = set()
    for robot_id in values:
        if robot_id in seen:
            continue
        seen.add(robot_id)
        deduped.append(robot_id)
    return deduped


def delete_robot_config(robot_id):
    cfg = read_raw_config()
    defaults = cfg.setdefault('defaults', {})
    robots = defaults.get('robot_configs', []) or []
    defaults['robot_configs'] = [x for x in robots if x.get('id') != robot_id]
    if defaults.get('default_robot_id') == robot_id:
        defaults['default_robot_id'] = defaults['robot_configs'][0]['id'] if defaults['robot_configs'] else ''
    fallback_robot_id = defaults.get('default_robot_id', '')
    for task in cfg.get('tasks', []) or []:
        robot_ids = task.get('robot_ids') or []
        if isinstance(robot_ids, str):
            robot_ids = [robot_ids]
        robot_ids = [x for x in robot_ids if x and x != robot_id]
        if task.get('robot_id') == robot_id:
            task['robot_id'] = ''
        if robot_ids:
            task['robot_ids'] = robot_ids
            task['robot_id'] = robot_ids[0]
        elif task.get('robot_id'):
            task.pop('robot_ids', None)
        else:
            task.pop('robot_ids', None)
            task['robot_id'] = fallback_robot_id
    write_raw_config(cfg)
    delete_robot_secrets(robot_id)


def save_imgbed_config(form):
    cfg = read_raw_config()
    defaults = cfg.setdefault('defaults', {})
    raw_url = (form.get('imgbed_upload_url') or [''])[0].strip()
    normalized_url = normalize_imgbed_upload_url(raw_url)
    defaults['imgbed_upload_url'] = normalized_url
    write_raw_config(cfg)
    return defaults['imgbed_upload_url']


def parse_filter_expression(expr):
    text = (expr or '').strip()
    if not text:
        return '', ''
    for sep in ['=', '＝', ':', '：']:
        if sep in text:
            left, right = text.split(sep, 1)
            return left.strip(), right.strip()
    return text, ''


def build_task_payload(
    *,
    mode='filter_capture',
    sheet_name='',
    filter_enabled_text='否',
    filter_expression='',
    filter_column='',
    filter_equals='',
    cell_range='',
    capture_style='compact',
    cycle='daily',
    weekday='monday',
    time_text='09:00',
    enabled=False,
    task_name='',
    task_id='',
    merge_enabled=False,
    merge_task_ids_text='',
    merge_title='截图通知',
    merge_text='',
    include_subtitles=True,
    robot_ids=None,
    browser_tab_keyword='',
    browser_tab_url_keyword='',
):
    mode = (mode or 'filter_capture').strip()
    sheet_name = (sheet_name or '').strip()
    filter_enabled_text = (filter_enabled_text or '否').strip()
    filter_expression = (filter_expression or '').strip()
    filter_column = (filter_column or '').strip()
    filter_equals = (filter_equals or '').strip()
    if filter_expression:
        expr_col, expr_val = parse_filter_expression(filter_expression)
        filter_column = expr_col or filter_column
        filter_equals = expr_val or filter_equals
    cell_range = (cell_range or '').strip().upper()
    capture_style = (capture_style or 'compact').strip()
    cycle = (cycle or 'daily').strip().lower()
    weekday = (weekday or 'monday').strip()
    time_text = (time_text or '09:00').strip()
    enabled = bool(enabled)
    task_name = (task_name or f'{sheet_name}_{cell_range}').strip() or f'{sheet_name}_{cell_range}'
    task_id = (task_id or slugify(task_name)).strip() or slugify(task_name)
    merge_enabled = bool(merge_enabled)
    merge_task_ids_text = (merge_task_ids_text or '').strip()
    merge_title = (merge_title or '截图通知').strip() or '截图通知'
    merge_text = (merge_text or '').strip()
    include_subtitles = bool(include_subtitles)
    robot_ids = [str(x).strip() for x in (robot_ids or []) if str(x).strip()]
    deduped_robot_ids = []
    seen_robot_ids = set()
    for rid in robot_ids:
        if rid in seen_robot_ids:
            continue
        seen_robot_ids.add(rid)
        deduped_robot_ids.append(rid)
    robot_ids = deduped_robot_ids
    robot_id = robot_ids[0] if robot_ids else ''
    browser_tab_keyword = (browser_tab_keyword or '').strip()
    browser_tab_url_keyword = (browser_tab_url_keyword or '').strip()

    if merge_enabled:
        task_ids = [x.strip() for x in re.split(r'[\r\n,，]+', merge_task_ids_text) if x.strip()]
        if not task_ids:
            raise ValueError('已开启合并发送时，子任务ID不能为空')
        task = {
            'id': task_id,
            'name': task_name,
            'enabled': enabled,
            'robot_id': robot_id,
            'robot_ids': robot_ids,
            'mode': 'merge_send',
            'schedule': {
                'type': cycle,
                'time': time_text,
            },
            'merge': {
                'enabled': True,
                'task_ids': task_ids,
                'include_subtitles': include_subtitles,
            },
            'browser_target': {
                'tab_keyword': browser_tab_keyword,
                'tab_url_keyword': browser_tab_url_keyword,
            },
            'message': {
                'mode': 'markdown',
                'title': merge_title,
                'text': merge_text,
            }
        }
        if cycle == 'weekly':
            task['schedule']['weekday'] = weekday
        return task

    if not sheet_name:
        raise ValueError('sheet 不能为空')
    if not cell_range:
        raise ValueError('截图区域不能为空')

    filter_enabled = (filter_enabled_text == '是') and mode == 'filter_capture'

    task = {
        'id': task_id,
        'name': task_name,
        'enabled': enabled,
        'robot_id': robot_id,
        'robot_ids': robot_ids,
        'mode': mode,
        'sheet_name': sheet_name,
        'schedule': {
            'type': cycle,
            'time': time_text,
        },
        'capture': {
            'cell_range': cell_range,
            'optimize_width': True,
            'title_prefix': 'AliDocs截图',
            'style': capture_style
        },
        'browser_target': {
            'tab_keyword': browser_tab_keyword,
            'tab_url_keyword': browser_tab_url_keyword,
        },
        'filter': {
            'enabled': filter_enabled,
            'column_name': filter_column,
            'equals': filter_equals,
        },
        'message': {
            'mode': 'markdown',
            'title': '截图通知',
            'text': task_name,
        }
    }
    if cycle == 'weekly':
        task['schedule']['weekday'] = weekday
    return task


def build_task_from_form(form):
    return build_task_payload(
        mode=(form.get('mode') or ['filter_capture'])[0],
        sheet_name=(form.get('sheet_name') or [''])[0],
        filter_enabled_text=(form.get('filter_enabled') or ['否'])[0],
        filter_expression=(form.get('filter_expression') or [''])[0],
        filter_column=(form.get('filter_column') or [''])[0],
        filter_equals=(form.get('filter_equals') or [''])[0],
        cell_range=(form.get('cell_range') or [''])[0],
        capture_style=(form.get('capture_style') or ['compact'])[0],
        cycle=(form.get('cycle') or ['daily'])[0],
        weekday=(form.get('weekday') or ['monday'])[0],
        time_text=(form.get('time') or ['09:00'])[0],
        enabled=(form.get('enabled') or ['off'])[0] == 'on',
        task_name=(form.get('task_name') or [''])[0],
        task_id=(form.get('task_id') or [''])[0],
        merge_enabled=(form.get('merge_enabled') or ['否'])[0].strip() == '是',
        merge_task_ids_text=(form.get('merge_task_ids') or [''])[0],
        merge_title=(form.get('merge_title') or ['截图通知'])[0],
        merge_text=(form.get('merge_text') or [''])[0],
        include_subtitles=(form.get('merge_include_subtitles') or ['on'])[0] == 'on',
        robot_ids=normalize_robot_ids_from_form(form),
        browser_tab_keyword=(form.get('browser_tab_keyword') or [''])[0],
        browser_tab_url_keyword=(form.get('browser_tab_url_keyword') or [''])[0],
    )


CSV_TASK_COLUMNS = [
    'id', 'name', 'enabled', 'robot_ids', 'mode', 'sheet_name',
    'schedule_type', 'schedule_time', 'schedule_weekday',
    'browser_tab_keyword', 'browser_tab_url_keyword',
    'cell_range', 'capture_style',
    'filter_enabled', 'filter_column', 'filter_equals',
    'message_title', 'message_text',
    'merge_enabled', 'merge_task_ids', 'merge_include_subtitles',
]
CSV_TASK_COLUMN_LABELS = {
    'id': '任务ID',
    'name': '任务名称',
    'enabled': '是否启用(1=是,0=否)',
    'robot_ids': '机器人ID列表(用 | 分隔,按顺序发送)',
    'mode': '模式(filter_capture/direct_range/merge_send)',
    'sheet_name': 'Sheet表名',
    'schedule_type': '周期类型(daily/weekly)',
    'schedule_time': '执行时间(HH:MM)',
    'schedule_weekday': '每周几(仅weekly有效)',
    'browser_tab_keyword': '浏览器标签关键字',
    'browser_tab_url_keyword': '浏览器标签URL关键字',
    'cell_range': '截图区域',
    'capture_style': '截图样式(compact/standard)',
    'filter_enabled': '是否启用筛选(1=是,0=否)',
    'filter_column': '筛选列名',
    'filter_equals': '筛选值',
    'message_title': '消息标题',
    'message_text': '消息正文',
    'merge_enabled': '是否合并发送(1=是,0=否)',
    'merge_task_ids': '合并子任务ID列表(用 | 分隔)',
    'merge_include_subtitles': '合并时是否带子标题(1=是,0=否)',
}
CSV_TASK_EXPORT_HEADERS = [f"{col}（{CSV_TASK_COLUMN_LABELS.get(col, col)}）" for col in CSV_TASK_COLUMNS]
CSV_TASK_IMPORT_HEADER_MAP = {col: col for col in CSV_TASK_COLUMNS}
CSV_TASK_IMPORT_HEADER_MAP.update({header: col for col, header in zip(CSV_TASK_COLUMNS, CSV_TASK_EXPORT_HEADERS)})


def _csv_export_row(task):
    schedule = task.get('schedule', {}) or {}
    capture = task.get('capture', {}) or {}
    filter_cfg = task.get('filter', {}) or {}
    message = task.get('message', {}) or {}
    merge = task.get('merge', {}) or {}
    browser_target = task.get('browser_target', {}) or {}
    robot_ids = task.get('robot_ids') or ([] if not task.get('robot_id') else [task.get('robot_id')])
    if isinstance(robot_ids, str):
        robot_ids = [robot_ids]
    return {
        'id': task.get('id', ''),
        'name': task.get('name', ''),
        'enabled': '1' if task.get('enabled', True) else '0',
        'robot_ids': ' | '.join([str(x).strip() for x in robot_ids if str(x).strip()]),
        'mode': task.get('mode', 'filter_capture'),
        'sheet_name': task.get('sheet_name', ''),
        'schedule_type': schedule.get('type', 'daily'),
        'schedule_time': schedule.get('time', ''),
        'schedule_weekday': schedule.get('weekday', ''),
        'browser_tab_keyword': browser_target.get('tab_keyword', ''),
        'browser_tab_url_keyword': browser_target.get('tab_url_keyword', ''),
        'cell_range': capture.get('cell_range', ''),
        'capture_style': capture.get('style', 'compact'),
        'filter_enabled': '1' if filter_cfg.get('enabled') else '0',
        'filter_column': filter_cfg.get('column_name', ''),
        'filter_equals': filter_cfg.get('equals', ''),
        'message_title': message.get('title', ''),
        'message_text': message.get('text', ''),
        'merge_enabled': '1' if merge.get('enabled') or task.get('mode') == 'merge_send' else '0',
        'merge_task_ids': ' | '.join(merge.get('task_ids', []) or []),
        'merge_include_subtitles': '1' if merge.get('include_subtitles', True) else '0',
    }


def _csv_export_display_row(task):
    raw = _csv_export_row(task)
    return {header: raw.get(col, '') for col, header in zip(CSV_TASK_COLUMNS, CSV_TASK_EXPORT_HEADERS)}


def _normalize_csv_row_keys(row):
    normalized = {}
    for key, value in (row or {}).items():
        text = str(key or '').strip()
        canonical = CSV_TASK_IMPORT_HEADER_MAP.get(text, text)
        normalized[canonical] = value
    return normalized


def export_tasks_csv_text():
    cfg = read_raw_config()
    tasks = cfg.get('tasks', []) or []
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_TASK_EXPORT_HEADERS)
    writer.writeheader()
    for task in tasks:
        writer.writerow(_csv_export_display_row(task))
    return buf.getvalue()


def export_tasks_csv_template_text():
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_TASK_EXPORT_HEADERS)
    writer.writeheader()
    writer.writerow(_csv_export_display_row({
        'id': '示例-湖北益佳通',
        'name': '示例-湖北益佳通',
        'enabled': True,
        'robot_ids': ['赢合群助手', '测试1'],
        'mode': 'filter_capture',
        'sheet_name': '湖北益佳通',
        'schedule': {'type': 'daily', 'time': '09:00'},
        'browser_target': {'tab_keyword': '2025赢合IFM多动子项目问题改善跟进表', 'tab_url_keyword': ''},
        'capture': {'cell_range': 'G:AE', 'style': 'compact'},
        'filter': {'enabled': True, 'column_name': '现场问题是否关闭', 'equals': '否'},
        'message': {'title': '截图通知', 'text': '示例-湖北益佳通'},
        'merge': {'enabled': False, 'include_subtitles': True, 'task_ids': []},
    }))
    writer.writerow(_csv_export_display_row({
        'id': '示例-问题进度汇报',
        'name': '示例-问题进度汇报',
        'enabled': True,
        'robot_ids': ['赢合群助手', 'HN技术支持助手'],
        'mode': 'merge_send',
        'sheet_name': '',
        'schedule': {'type': 'daily', 'time': '18:10'},
        'browser_target': {'tab_keyword': '2025赢合IFM多动子项目问题改善跟进表', 'tab_url_keyword': ''},
        'capture': {'cell_range': '', 'style': 'compact'},
        'filter': {'enabled': False, 'column_name': '', 'equals': ''},
        'message': {'title': '赢合-问题进度汇报', 'text': '赢合-问题进度汇报，点击对应图片可进入查看详细内容。'},
        'merge': {
            'enabled': True,
            'include_subtitles': True,
            'task_ids': ['数据统计表单_截图发送', '惠州赢合样机_否_截图发送', '湖北益佳通_现场问题是否关闭 = 否_截图发送'],
        },
    }))
    return buf.getvalue()


def _truthy_text(value):
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'y', 'on', '是'}


def _split_csv_multi(value):
    text = str(value or '').strip()
    if not text:
        return []
    return [x.strip() for x in re.split(r'\s*\|\s*|[\r\n,，;；]+', text) if x.strip()]


def _normalize_task_for_compare(task):
    return json.dumps(task, ensure_ascii=False, sort_keys=True)


def import_tasks_from_csv_text(text):
    raw = text.lstrip('\ufeff').strip()
    if not raw:
        raise ValueError('CSV 内容为空')
    reader = csv.DictReader(io.StringIO(raw))
    if not reader.fieldnames:
        raise ValueError('CSV 缺少表头')
    reader.fieldnames = [CSV_TASK_IMPORT_HEADER_MAP.get(str(x or '').strip(), str(x or '').strip()) for x in reader.fieldnames]
    cfg = read_raw_config()
    tasks = cfg.setdefault('tasks', [])
    existing_ids = {str(x.get('id') or '').strip() for x in tasks if str(x.get('id') or '').strip()}
    existing_configs = {_normalize_task_for_compare(x) for x in tasks}
    imported = 0
    skipped = 0
    imported_ids = []
    skipped_details = []
    row_index = 1
    for row in reader:
        row_index += 1
        row = _normalize_csv_row_keys(row)
        if not any(str(v or '').strip() for v in row.values()):
            continue
        task = build_task_payload(
            mode=row.get('mode', 'filter_capture'),
            sheet_name=row.get('sheet_name', ''),
            filter_enabled_text='是' if _truthy_text(row.get('filter_enabled')) else '否',
            filter_column=row.get('filter_column', ''),
            filter_equals=row.get('filter_equals', ''),
            cell_range=row.get('cell_range', ''),
            capture_style=row.get('capture_style', 'compact'),
            cycle=row.get('schedule_type', 'daily'),
            weekday=row.get('schedule_weekday', 'monday'),
            time_text=row.get('schedule_time', '09:00'),
            enabled=_truthy_text(row.get('enabled')),
            task_name=row.get('name', ''),
            task_id=row.get('id', ''),
            merge_enabled=_truthy_text(row.get('merge_enabled')) or str(row.get('mode', '')).strip() == 'merge_send',
            merge_task_ids_text='\n'.join(_split_csv_multi(row.get('merge_task_ids', ''))),
            merge_title=row.get('message_title', '截图通知'),
            merge_text=row.get('message_text', ''),
            include_subtitles=not str(row.get('merge_include_subtitles', '')).strip() or _truthy_text(row.get('merge_include_subtitles')),
            robot_ids=_split_csv_multi(row.get('robot_ids', '')),
            browser_tab_keyword=row.get('browser_tab_keyword', ''),
            browser_tab_url_keyword=row.get('browser_tab_url_keyword', ''),
        )
        task_id = str(task.get('id') or '').strip()
        task_key = _normalize_task_for_compare(task)
        skip_reason = ''
        if task_id in existing_ids:
            skip_reason = '任务ID已存在'
        elif task_key in existing_configs:
            skip_reason = '配置内容已存在'
        if skip_reason:
            skipped += 1
            skipped_details.append({
                'row': row_index,
                'task_id': task_id,
                'task_name': str(task.get('name') or '').strip(),
                'reason': skip_reason,
            })
            continue
        tasks.append(task)
        existing_ids.add(task_id)
        existing_configs.add(task_key)
        imported += 1
        imported_ids.append(task_id)
    write_raw_config(cfg)
    return {
        'imported': imported,
        'skipped': skipped,
        'imported_ids': imported_ids[:50],
        'skipped_details': skipped_details[:100],
    }


def render_import_result(result):
    imported_ids = result.get('imported_ids') or []
    skipped_details = result.get('skipped_details') or []
    imported_html = ''.join(f'<li><code>{html.escape(str(x))}</code></li>' for x in imported_ids) or '<li>无</li>'
    skipped_html = ''.join(
        f"<tr><td>{item.get('row','')}</td><td><code>{html.escape(str(item.get('task_id') or ''))}</code></td><td>{html.escape(str(item.get('task_name') or ''))}</td><td>{html.escape(str(item.get('reason') or ''))}</td></tr>"
        for item in skipped_details
    ) or '<tr><td colspan="4">无</td></tr>'
    return f'''<!doctype html><html><head><meta charset="utf-8"><title>CSV 导入结果</title>
<style>
body{{font-family:Arial,"Microsoft YaHei",sans-serif;margin:24px;background:#f7f7f9;color:#222}}
.container{{max-width:1200px;margin:0 auto}}
.card{{background:#fff;border-radius:12px;padding:16px;margin-bottom:16px;box-shadow:0 2px 10px rgba(0,0,0,.06)}}
.grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}}
.table-wrap{{overflow:auto}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{border:1px solid #e5e7eb;padding:8px 10px;vertical-align:top;text-align:left}}
th{{background:#f8fafc}}
code{{background:#f1f5f9;padding:2px 6px;border-radius:6px}}
.stat{{font-size:28px;font-weight:700;margin-top:8px}}
.ok{{color:#15803d}} .warn{{color:#b45309}}
button{{padding:10px 16px;border:none;border-radius:8px;background:#1677ff;color:#fff;cursor:pointer}}
.muted{{color:#667085;font-size:13px}}
</style></head><body>
<div id="modal-overlay" class="modal-overlay">
  <div class="modal-content" id="modal-content" style="position:relative;">
    <button class="modal-close" onclick="closeModal()">&times;</button>
  </div>
</div>
<div class="container">
<div class="card"><h1 style="margin-top:0;">CSV 导入结果</h1><div class="muted">已经按“重复任务ID / 重复配置内容”自动跳过，无覆盖现有任务。</div></div>
<div class="grid"><div class="card"><div class="muted">新增任务</div><div class="stat ok">{int(result.get('imported') or 0)}</div><ul>{imported_html}</ul></div><div class="card"><div class="muted">跳过任务</div><div class="stat warn">{int(result.get('skipped') or 0)}</div><div class="muted">下表会直接告诉你是哪一行、哪个任务、为什么被跳过。</div></div></div>
<div class="card"><h2 style="margin-top:0;">跳过明细</h2><div class="table-wrap"><table><thead><tr><th>CSV 行号</th><th>任务ID</th><th>任务名称</th><th>跳过原因</th></tr></thead><tbody>{skipped_html}</tbody></table></div></div>
<div class="card"><a href="/"><button type="button">返回首页</button></a></div>
</div></body></html>'''


def sanitize_config_for_display(cfg):
    safe = json.loads(json.dumps(cfg, ensure_ascii=False))
    defaults = safe.setdefault('defaults', {})
    dingtalk = defaults.setdefault('dingtalk', {})
    status = build_dingtalk_secret_status(load_dingtalk_secrets())
    dingtalk['webhook'] = status['webhook']['masked'] if status['webhook']['configured'] else ''
    dingtalk['secret'] = status['secret']['masked'] if status['secret']['configured'] else ''
    safe.setdefault('secret_status', status)
    for robot in defaults.get('robot_configs', []) or []:
        robot.pop('runtime_secrets', None)
        robot['secret_status'] = build_robot_secret_status(robot.get('id'))
    return safe


def _default_scheduler_status():
    return {
        'scheduler_running': False,
        'pid': None,
        'current_task': None,
        'queue': [],
        'last_events': [],
        'updated_at': None,
    }


def _pid_exists(pid):
    if not pid:
        return False
    try:
        result = subprocess.run(
            ['tasklist', '/FI', f'PID eq {int(pid)}'],
            capture_output=True,
            text=True,
            check=False,
        )
        text = (result.stdout or '') + (result.stderr or '')
        return str(pid) in text and 'python.exe' in text.lower()
    except Exception:
        return False


def _clear_stale_scheduler_artifacts(status=None):
    if status is None:
        status = _default_scheduler_status()
    status['scheduler_running'] = False
    status['pid'] = None
    status['current_task'] = None
    status['queue'] = []
    status['updated_at'] = now_text()
    try:
        save_json(SCHEDULER_STATUS_PATH, status)
    except Exception:
        pass
    try:
        SCHEDULER_LOCK_PATH.unlink(missing_ok=True)
    except Exception:
        pass
    return status


def load_scheduler_status():
    if not SCHEDULER_STATUS_PATH.exists():
        return _default_scheduler_status()
    try:
        status = json.loads(SCHEDULER_STATUS_PATH.read_text(encoding='utf-8'))
    except Exception:
        status = _default_scheduler_status()
        status['read_error'] = True
        return status

    pid = status.get('pid')
    running = bool(status.get('scheduler_running'))
    if running and not _pid_exists(pid):
        status.setdefault('last_events', [])
        status['last_events'].insert(0, {
            'time': now_text(),
            'kind': 'scheduler_stale_cleared',
            'task_id': None,
            'detail': f'pid={pid}',
        })
        status['last_events'] = status['last_events'][:30]
        return _clear_stale_scheduler_artifacts(status)
    return status


def has_enabled_tasks():
    cfg = load_config(CONFIG_PATH)
    return any(bool(x.get('enabled', True)) for x in cfg.get('tasks', []) or [])


def start_scheduler(force=False):
    status = load_scheduler_status()
    if status.get('scheduler_running') and status.get('pid'):
        return f"调度器已在运行中，PID={status.get('pid')}"
    if not force and not has_enabled_tasks():
        return '当前没有启用任务，未启动调度器'
    _clear_stale_scheduler_artifacts(status)
    creationflags = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
    subprocess.Popen(
        [sys.executable, str(SCHEDULER_SCRIPT)],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=True,
    )
    return '已发起启动调度器请求，请刷新查看状态'


def stop_scheduler():
    status = load_scheduler_status()
    pid = status.get('pid')
    if not pid:
        _clear_stale_scheduler_artifacts(status)
        return '调度器当前未运行'
    if not _pid_exists(pid):
        _clear_stale_scheduler_artifacts(status)
        return f'检测到陈旧调度器状态，已清理（原 PID={pid}）'
    try:
        subprocess.run(['taskkill', '/PID', str(pid), '/F'], check=True, capture_output=True, text=True)
        time.sleep(1)
        _clear_stale_scheduler_artifacts(status)
        return f'已停止调度器，PID={pid}'
    except subprocess.CalledProcessError as e:
        detail = (e.stdout or '') + (e.stderr or '')
        return f'停止调度器失败，PID={pid}，详情：{detail.strip()}'


def shutdown_project(delay_seconds=1.5):
    scheduler_msg = stop_scheduler()
    browser_msg = stop_capture_browser()
    runtime = load_webui_runtime()
    current_pid = os.getpid()
    runtime_pid = runtime.get('pid')

    def _shutdown_self():
        time.sleep(max(float(delay_seconds or 0), 0.2))
        try:
            clear_webui_runtime()
        except Exception:
            pass
        try:
            if WEBUI_SERVER is not None:
                WEBUI_SERVER.shutdown()
                WEBUI_SERVER.server_close()
        except Exception:
            pass
        os._exit(0)

    if runtime_pid and int(runtime_pid) != int(current_pid):
        killed = _kill_pid(runtime_pid)
        try:
            clear_webui_runtime()
        except Exception:
            pass
        webui_msg = '已关闭 WebUI 进程' if killed else '关闭 WebUI 进程失败'
        return f'{scheduler_msg}；{browser_msg}；{webui_msg}，PID={runtime_pid}'

    threading.Thread(target=_shutdown_self, daemon=True).start()
    return f'{scheduler_msg}；{browser_msg}；WebUI 正在关闭，请稍候。'


def render_shutdown_page(message='项目正在关闭，请稍候。'):
    return f'''<!doctype html><html><head><meta charset="utf-8"><title>正在关闭项目</title>
<style>
body{{font-family:Arial,"Microsoft YaHei",sans-serif;margin:0;background:#f6f8fb;color:#1f2937}}
.wrap{{max-width:760px;margin:56px auto;padding:0 20px}}
.card{{background:#fff;border-radius:16px;padding:24px 28px;box-shadow:0 10px 30px rgba(15,23,42,.08)}}
h1{{margin:0 0 12px;font-size:28px}}
.desc{{color:#475467;line-height:1.7}}
.code{{margin-top:16px;padding:14px 16px;background:#f8fafc;border:1px solid #e5e7eb;border-radius:12px;white-space:pre-wrap;word-break:break-word}}
</style></head><body><div class="wrap"><div class="card"><h1>项目正在关闭</h1><div class="desc">已收到关闭请求，正在停止调度器、截图浏览器和 WebUI 进程。</div><div class="code">{html.escape(str(message or '项目正在关闭，请稍候。'))}</div></div></div></body></html>'''


def restart_project():
    state = load_webui_state()
    state['last_restart_at'] = now_text()
    save_webui_state(state)
    scheduler_msg = stop_scheduler()
    runtime = load_webui_runtime()
    host = runtime.get('host') or '127.0.0.1'
    port = int(runtime.get('port') or 8787)
    helper_code = (
        "import socket,subprocess,sys,time;"
        "host=sys.argv[1];port=int(sys.argv[2]);root=sys.argv[3];"
        "deadline=time.time()+20;"
        "busy=True;"
        "\nwhile time.time()<deadline:\n"
        " s=socket.socket(); s.settimeout(0.5);\n"
        " try:\n  s.connect((host,port)); busy=True\n"
        " except Exception:\n  busy=False\n"
        " finally:\n  s.close()\n"
        " if not busy:\n  break\n"
        " time.sleep(0.5);\n"
        "flags=getattr(subprocess,'CREATE_NEW_PROCESS_GROUP',0)|getattr(subprocess,'DETACHED_PROCESS',0);"
        "subprocess.Popen([sys.executable,'app\\webui_server.py'], cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True)"
    )
    creationflags = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0) | getattr(subprocess, 'DETACHED_PROCESS', 0)
    subprocess.Popen(
        [sys.executable, '-c', helper_code, host, str(port), str(ROOT)],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=True,
    )

    def _shutdown_self():
        time.sleep(1.0)
        try:
            clear_webui_runtime()
        except Exception:
            pass
        try:
            if WEBUI_SERVER is not None:
                WEBUI_SERVER.shutdown()
                WEBUI_SERVER.server_close()
        except Exception:
            pass
        os._exit(0)

    threading.Thread(target=_shutdown_self, daemon=True).start()
    return f'项目正在受控重启中，请 3 秒后刷新页面。{scheduler_msg}'


def sync_scheduler_by_config():
    state = load_webui_state()
    if not state.get('auto_start_scheduler', True):
        return '已关闭自动启动调度器，不自动同步'
    if not has_enabled_tasks():
        return stop_scheduler()

    status = load_scheduler_status()
    if status.get('scheduler_running') and status.get('pid'):
        stop_msg = stop_scheduler()
        start_msg = start_scheduler(force=True)
        return f'{stop_msg}；{start_msg}'

    return start_scheduler(force=True)


def build_view_model():
    cfg = load_config(CONFIG_PATH)
    scheduler_status = load_scheduler_status()
    state = load_webui_state()
    runtime = load_webui_runtime()
    webui_file = Path(__file__).resolve()
    defaults = cfg.get('defaults', {}) or {}
    robots = []
    for robot in defaults.get('robot_configs', []) or []:
        status = build_robot_secret_status(robot.get('id'))
        robots.append({
            'id': robot.get('id', ''),
            'name': robot.get('name', ''),
            'enabled': robot.get('enabled', True),
            'webhook_masked': status['webhook']['masked'],
            'secret_masked': status['secret']['masked'],
            'webhook_configured': status['webhook']['configured'],
            'secret_configured': status['secret']['configured'],
        })
    tasks = list_tasks(cfg, enabled_only=False)
    normal_tasks = [x for x in tasks if x.get('mode') != 'merge_send']
    merge_tasks = [x for x in tasks if x.get('mode') == 'merge_send']
    send_logs = load_send_logs()
    return {
        'scheduler_status': scheduler_status,
        'webui_state': state,
        'webui_runtime': runtime,
        'webui_version': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(webui_file.stat().st_mtime)),
        'webui_file_name': webui_file.name,
        'robots': robots,
        'normal_tasks': normal_tasks,
        'merge_tasks': merge_tasks,
        'send_logs': send_logs,
        'default_imgbed_upload_url': defaults.get('imgbed_upload_url', ''),
        'default_alidocs_url': defaults.get('alidocs_url', ''),
    }


def _task_rows(tasks, merge=False):
    rows = []
    for task in tasks:
        task_id = str(task.get('id') or '')
        task_id_json = json.dumps(task_id, ensure_ascii=False)
        merge_cfg = task.get('merge', {}) or {}
        merge_ids = ', '.join(merge_cfg.get('task_ids', []) or [])
        capture_cfg = task.get('capture', {}) or {}
        filter_cfg = task.get('filter', {}) or {}
        browser_target = task.get('browser_target', {}) or {}
        filter_text = ''
        if not merge and filter_cfg.get('enabled'):
            filter_text = f"{filter_cfg.get('column_name', '')}={filter_cfg.get('equals', '')}"
        robot_ids = task.get('robot_ids') or ([] if not task.get('robot_id') else [task.get('robot_id')])
        robot_text = ' → '.join(str(x) for x in robot_ids if x) or '默认机器人'
        is_enabled = bool(task.get('enabled', True))
        status_badge = ('<span class="badge badge-success">启用</span>' if is_enabled
                        else '<span class="badge badge-muted">停用</span>')
        toggle_label = '停用' if is_enabled else '启用'
        toggle_class = 'row-action toggle-action' + (' toggle-off' if is_enabled else ' toggle-on')
        merge_attr = 'true' if merge else 'false'
        rows.append(f"""
        <tr class="{'row-enabled' if is_enabled else 'row-disabled'}">
          <td><code>{html.escape(task_id)}</code></td>
          <td>{html.escape(task.get('name') or '')}</td>
          <td>{html.escape(robot_text)}</td>
          <td>{status_badge}</td>
          <td><code class="schedule-code">{html.escape(json.dumps(task.get('schedule', {}), ensure_ascii=False))}</code></td>
          <td>{html.escape(task.get('next_run') or '')}</td>
          <td>{html.escape(task.get('sheet_name') or '')}</td>
          <td>{html.escape((capture_cfg.get('cell_range') or '') if not merge else merge_ids)}</td>
          <td>{html.escape(filter_text if not merge else task.get('message', {}).get('title', ''))}</td>
          <td>{html.escape(browser_target.get('tab_keyword') or '')}</td>
          <td class="row-actions">
            <button type="button" class="row-action edit-action" onclick="openTaskEditPopup({task_id_json}, {merge_attr})">编辑</button>
            <form method="post" action="/run" class="inline-form"><input type="hidden" name="task_id" value="{html.escape(task_id)}"><button type="submit" class="row-action run-action">测试发送</button></form>
            <form method="post" action="/upload-only" class="inline-form"><input type="hidden" name="task_id" value="{html.escape(task_id)}"><button type="submit" class="row-action upload-action">截图上传</button></form>
            <form method="post" action="/toggle" class="inline-form"><input type="hidden" name="task_id" value="{html.escape(task_id)}"><input type="hidden" name="enabled" value="{'0' if is_enabled else '1'}"><button type="submit" class="{toggle_class}">{toggle_label}</button></form>
            <form method="post" action="/delete" class="inline-form" onsubmit="return confirm('确定删除任务 {html.escape(task_id)} 吗？');"><input type="hidden" name="task_id" value="{html.escape(task_id)}"><button type="submit" class="row-action delete-action">删除</button></form>
          </td>
        </tr>
        """)
    return ''.join(rows) or '<tr><td colspan="11" class="empty-row">暂无数据</td></tr>'


def render_robot_form(title='机器人配置', robot=None, message=''):
    cfg = load_config(CONFIG_PATH)
    robot = robot or {}
    robot_id = robot.get('id', '')
    status = build_robot_secret_status(robot_id) if robot_id else {
        'webhook': {'configured': False, 'masked': ''},
        'secret': {'configured': False, 'masked': ''},
    }
    webhook_value = status['webhook']['masked'] if status['webhook']['configured'] else ''
    secret_value = status['secret']['masked'] if status['secret']['configured'] else ''
    enabled_checked = 'checked' if robot.get('enabled', True) else ''
    return f'''<div style="padding:0;">
<h2 style="margin:0 0 20px 0;font-size:20px;color:#e2e8f0;">{html.escape(title)}</h2>
{('<div style="padding:12px 16px;background:rgba(22,163,74,0.15);color:#4ade80;border-radius:8px;margin-bottom:16px;border:1px solid rgba(22,163,74,0.3);">'+html.escape(message)+'</div>') if message else ''}
<form method="post" action="/save-robot">
<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;">
<div><label style="display:block;font-weight:600;margin-bottom:6px;color:#94a3b8;font-size:13px;">机器人名称</label><input name="robot_name" value="{html.escape(robot.get('name', ''))}" placeholder="示例：生产群机器人" style="width:100%;padding:10px 12px;background:#1e293b;border:1px solid #334155;border-radius:8px;color:#e2e8f0;box-sizing:border-box;"></div>
<div><label style="display:block;font-weight:600;margin-bottom:6px;color:#94a3b8;font-size:13px;">机器人ID（可选）</label><input name="robot_id" value="{html.escape(robot_id)}" placeholder="留空自动生成" style="width:100%;padding:10px 12px;background:#1e293b;border:1px solid #334155;border-radius:8px;color:#e2e8f0;box-sizing:border-box;"></div>
<div><label style="display:block;font-weight:600;margin-bottom:6px;color:#94a3b8;font-size:13px;">Webhook</label><input type="password" name="robot_webhook" value="{html.escape(webhook_value)}" placeholder="请输入 Webhook" style="width:100%;padding:10px 12px;background:#1e293b;border:1px solid #334155;border-radius:8px;color:#e2e8f0;box-sizing:border-box;"></div>
<div><label style="display:block;font-weight:600;margin-bottom:6px;color:#94a3b8;font-size:13px;">Secret</label><input type="password" name="robot_secret" value="{html.escape(secret_value)}" placeholder="请输入 Secret" style="width:100%;padding:10px 12px;background:#1e293b;border:1px solid #334155;border-radius:8px;color:#e2e8f0;box-sizing:border-box;"></div>
</div>
<div style="margin-top:16px;"><label style="display:flex;align-items:center;gap:8px;color:#e2e8f0;cursor:pointer;"><input type="checkbox" name="robot_enabled" {enabled_checked} style="width:auto;">启用机器人</label></div>
<div style="margin-top:12px;padding:10px 14px;background:rgba(59,130,246,0.1);color:#60a5fa;border-radius:8px;font-size:13px;border:1px solid rgba(59,130,246,0.2);">图床上传地址已改为首页单独维护，保存机器人不会再覆盖图床配置。</div>
<div style="margin-top:20px;display:flex;gap:12px;justify-content:flex-end;padding-top:16px;border-top:1px solid #334155;">
<button type="button" onclick="closeModal()" style="padding:10px 20px;background:#475569;color:#e2e8f0;border:none;border-radius:8px;cursor:pointer;font-weight:500;">取消</button>
<button type="submit" style="padding:10px 20px;background:#10b981;color:#fff;border:none;border-radius:8px;cursor:pointer;font-weight:500;">保存机器人</button>
</div>
</form>
</div>'''


def render_popup_success(message='保存成功'):
    return f'''<!doctype html>
<html><head><meta charset="utf-8"><title>处理中</title>
<style>
body {{ font-family: Arial, "Microsoft YaHei", sans-serif; margin: 0; background:#f7f7f9; color:#222; }}
.wrap {{ min-height: 100vh; display:flex; align-items:center; justify-content:center; padding:24px; box-sizing:border-box; }}
.card {{ background:#fff; border-radius:12px; padding:24px; box-shadow:0 2px 10px rgba(0,0,0,.06); text-align:center; min-width:320px; }}
.msg {{ color:#065f46; font-weight:600; }}
.muted {{ color:#667085; font-size:13px; margin-top:10px; }}
</style>
<script>
window.addEventListener('DOMContentLoaded', () => {{
  try {{
    if (window.opener && !window.opener.closed) {{
      window.opener.location.reload();
    }}
  }} catch (e) {{}}
  setTimeout(() => {{
    try {{ window.close(); }} catch (e) {{}}
    try {{ location.href = '/'; }} catch (e) {{}}
  }}, 120);
}});
</script></head>
<body><div class="wrap"><div class="card"><div class="msg">{html.escape(message)}</div><div class="muted">正在刷新原页面并关闭当前窗口…</div></div></div></body></html>'''


def render_task_form(title, action, task=None, message=''):
    cfg = load_config(CONFIG_PATH)
    task = task or {}
    schedule = task.get('schedule', {}) or {}
    capture = task.get('capture', {}) or {}
    filter_cfg = task.get('filter', {}) or {}
    mode = task.get('mode', 'filter_capture')
    capture_style = capture.get('style', 'compact')
    cycle = schedule.get('type', 'daily')
    weekday = schedule.get('weekday', 'monday')
    enabled_checked = 'checked' if task.get('enabled', True) else ''
    filter_enabled = '是' if filter_cfg.get('enabled') else '否'
    filter_expression = ''
    if filter_cfg.get('column_name') or filter_cfg.get('equals'):
        filter_expression = f"{filter_cfg.get('column_name', '')} = {filter_cfg.get('equals', '')}".strip()
    merge_cfg = task.get('merge', {}) or {}
    merge_enabled = '是' if merge_cfg.get('enabled') or merge_cfg.get('task_ids') else '否'
    merge_task_ids_text = '\n'.join(merge_cfg.get('task_ids', []) or [])
    message_cfg = task.get('message', {}) or {}
    merge_title = message_cfg.get('title', '截图通知')
    merge_text = message_cfg.get('text', '')
    merge_include_subtitles_checked = 'checked' if merge_cfg.get('include_subtitles', True) else ''
    browser_target = task.get('browser_target', {}) or {}
    current_robot_ids = task.get('robot_ids') or ([] if not task.get('robot_id') else [task.get('robot_id')])
    robot_options = []
    for robot in cfg.get('defaults', {}).get('robot_configs', []) or []:
        selected = 'selected' if robot.get('id') in current_robot_ids else ''
        robot_options.append(f'<option value="{html.escape(robot.get("id", ""))}" {selected}>{html.escape(robot.get("name", robot.get("id", "")))}</option>')

    def selected_value(value, expected):
        return 'selected' if value == expected else ''

    return f'''<div style="padding:0;">
<h2 style="margin:0 0 20px 0;font-size:20px;color:#e2e8f0;">{html.escape(title)}</h2>
{('<div style="padding:12px 16px;background:rgba(22,163,74,0.15);color:#4ade80;border-radius:8px;margin-bottom:16px;border:1px solid rgba(22,163,74,0.3);">'+html.escape(message)+'</div>') if message else ''}
<form method="post" action="{html.escape(action)}" id="taskForm">
<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;">
<div><label style="display:block;font-weight:600;margin-bottom:6px;color:#94a3b8;font-size:13px;">任务名称</label><input name="task_name" value="{html.escape(task.get('name', ''))}" style="width:100%;padding:10px 12px;background:#1e293b;border:1px solid #334155;border-radius:8px;color:#e2e8f0;box-sizing:border-box;"></div>
<div><label style="display:block;font-weight:600;margin-bottom:6px;color:#94a3b8;font-size:13px;">任务ID</label><input name="task_id" value="{html.escape(task.get('id', ''))}" style="width:100%;padding:10px 12px;background:#1e293b;border:1px solid #334155;border-radius:8px;color:#e2e8f0;box-sizing:border-box;"></div>
<div><label style="display:block;font-weight:600;margin-bottom:6px;color:#94a3b8;font-size:13px;">机器人（可多选）</label><select name="robot_ids" multiple size="4" style="width:100%;padding:10px 12px;background:#1e293b;border:1px solid #334155;border-radius:8px;color:#e2e8f0;box-sizing:border-box;">{''.join(robot_options)}</select></div>
<div><label style="display:block;font-weight:600;margin-bottom:6px;color:#94a3b8;font-size:13px;">是否启用合并发送</label><select id="merge_enabled" name="merge_enabled" onchange="onMergeChange()" style="width:100%;padding:10px 12px;background:#1e293b;border:1px solid #334155;border-radius:8px;color:#e2e8f0;box-sizing:border-box;"><option value="否" {selected_value(merge_enabled,'否')}>否</option><option value="是" {selected_value(merge_enabled,'是')}>是</option></select></div>
<div><label style="display:block;font-weight:600;margin-bottom:6px;color:#94a3b8;font-size:13px;">周期</label><select id="cycle" name="cycle" onchange="onCycleChange()" style="width:100%;padding:10px 12px;background:#1e293b;border:1px solid #334155;border-radius:8px;color:#e2e8f0;box-sizing:border-box;"><option value="daily" {selected_value(cycle,'daily')}>每日</option><option value="weekly" {selected_value(cycle,'weekly')}>每周</option></select></div>
<div id="week-wrap" style="display:none;"><label style="display:block;font-weight:600;margin-bottom:6px;color:#94a3b8;font-size:13px;">每周几</label><select name="weekday" style="width:100%;padding:10px 12px;background:#1e293b;border:1px solid #334155;border-radius:8px;color:#e2e8f0;box-sizing:border-box;"><option value="monday" {selected_value(weekday,'monday')}>周一</option><option value="tuesday" {selected_value(weekday,'tuesday')}>周二</option><option value="wednesday" {selected_value(weekday,'wednesday')}>周三</option><option value="thursday" {selected_value(weekday,'thursday')}>周四</option><option value="friday" {selected_value(weekday,'friday')}>周五</option><option value="saturday" {selected_value(weekday,'saturday')}>周六</option><option value="sunday" {selected_value(weekday,'sunday')}>周日</option></select></div>
<div><label style="display:block;font-weight:600;margin-bottom:6px;color:#94a3b8;font-size:13px;">时间</label><input name="time" value="{html.escape(schedule.get('time', '09:00'))}" style="width:100%;padding:10px 12px;background:#1e293b;border:1px solid #334155;border-radius:8px;color:#e2e8f0;box-sizing:border-box;"></div>
<div><label style="display:block;font-weight:600;margin-bottom:6px;color:#94a3b8;font-size:13px;">标签关键字</label><input name="browser_tab_keyword" value="{html.escape(browser_target.get('tab_keyword', ''))}" placeholder="匹配浏览器标签标题" style="width:100%;padding:10px 12px;background:#1e293b;border:1px solid #334155;border-radius:8px;color:#e2e8f0;box-sizing:border-box;"></div>
</div>
<div id="merge-task-wrap" style="display:none;margin-top:16px;padding:16px;background:#1e293b;border-radius:8px;border:1px solid #334155;">
<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px;"><div><label style="display:block;font-weight:600;margin-bottom:6px;color:#94a3b8;font-size:13px;">合并消息标题</label><input name="merge_title" value="{html.escape(merge_title)}" style="width:100%;padding:10px 12px;background:#0f172a;border:1px solid #334155;border-radius:8px;color:#e2e8f0;box-sizing:border-box;"></div><div><label style="display:block;font-weight:600;margin-bottom:6px;color:#94a3b8;font-size:13px;">合并消息正文</label><input name="merge_text" value="{html.escape(merge_text)}" style="width:100%;padding:10px 12px;background:#0f172a;border:1px solid #334155;border-radius:8px;color:#e2e8f0;box-sizing:border-box;"></div></div>
<div><label style="display:block;font-weight:600;margin-bottom:6px;color:#94a3b8;font-size:13px;">子任务ID</label><textarea name="merge_task_ids" style="width:100%;min-height:100px;padding:10px 12px;background:#0f172a;border:1px solid #334155;border-radius:8px;color:#e2e8f0;box-sizing:border-box;resize:vertical;">{html.escape(merge_task_ids_text)}</textarea></div>
<div style="margin-top:12px;"><label style="display:flex;align-items:center;gap:8px;color:#e2e8f0;cursor:pointer;"><input type="checkbox" name="merge_include_subtitles" {merge_include_subtitles_checked} style="width:auto;">每张图前面带子任务标题</label></div>
</div>
<div id="single-task-wrap" style="margin-top:16px;padding:16px;background:#1e293b;border-radius:8px;border:1px solid #334155;">
<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;">
<div><label style="display:block;font-weight:600;margin-bottom:6px;color:#94a3b8;font-size:13px;">模式</label><select id="mode" name="mode" onchange="onModeChange()" style="width:100%;padding:10px 12px;background:#0f172a;border:1px solid #334155;border-radius:8px;color:#e2e8f0;box-sizing:border-box;"><option value="direct_range" {selected_value(mode,'direct_range')}>1. 直接根据区域截图</option><option value="filter_capture" {selected_value(mode,'filter_capture')}>2. 根据筛选条件截图</option></select></div>
<div><label style="display:block;font-weight:600;margin-bottom:6px;color:#94a3b8;font-size:13px;">sheet（表名）</label><input name="sheet_name" value="{html.escape(task.get('sheet_name', ''))}" style="width:100%;padding:10px 12px;background:#0f172a;border:1px solid #334155;border-radius:8px;color:#e2e8f0;box-sizing:border-box;"></div>
<div id="filter-wrap"><label style="display:block;font-weight:600;margin-bottom:6px;color:#94a3b8;font-size:13px;">是否筛选</label><select name="filter_enabled" style="width:100%;padding:10px 12px;background:#0f172a;border:1px solid #334155;border-radius:8px;color:#e2e8f0;box-sizing:border-box;"><option value="是" {selected_value(filter_enabled,'是')}>是</option><option value="否" {selected_value(filter_enabled,'否')}>否</option></select></div>
<div id="filter-wrap-2"><label style="display:block;font-weight:600;margin-bottom:6px;color:#94a3b8;font-size:13px;">筛选表达式</label><input name="filter_expression" value="{html.escape(filter_expression)}" placeholder="列名=值" style="width:100%;padding:10px 12px;background:#0f172a;border:1px solid #334155;border-radius:8px;color:#e2e8f0;box-sizing:border-box;"></div>
<div><label style="display:block;font-weight:600;margin-bottom:6px;color:#94a3b8;font-size:13px;">截图区域</label><input name="cell_range" value="{html.escape(capture.get('cell_range', ''))}" style="width:100%;padding:10px 12px;background:#0f172a;border:1px solid #334155;border-radius:8px;color:#e2e8f0;box-sizing:border-box;"></div>
<div><label style="display:block;font-weight:600;margin-bottom:6px;color:#94a3b8;font-size:13px;">截图样式</label><select name="capture_style" style="width:100%;padding:10px 12px;background:#0f172a;border:1px solid #334155;border-radius:8px;color:#e2e8f0;box-sizing:border-box;"><option value="compact" {selected_value(capture_style,'compact')}>紧凑版</option><option value="standard" {selected_value(capture_style,'standard')}>标准版</option></select></div>
</div>
</div>
<div style="margin-top:20px;display:flex;align-items:center;gap:16px;">
<label style="display:flex;align-items:center;gap:8px;color:#e2e8f0;cursor:pointer;font-weight:600;"><input type="checkbox" name="enabled" {enabled_checked} style="width:auto;">启用该任务</label>
</div>
<div style="margin-top:20px;display:flex;gap:12px;justify-content:flex-end;padding-top:16px;border-top:1px solid #334155;">
<button type="button" onclick="closeModal()" style="padding:10px 20px;background:#475569;color:#e2e8f0;border:none;border-radius:8px;cursor:pointer;font-weight:500;">取消</button>
<button type="submit" style="padding:10px 20px;background:#10b981;color:#fff;border:none;border-radius:8px;cursor:pointer;font-weight:500;">保存任务</button>
</div>
</form>
</div>
<script>
function onModeChange() {{
  const mode = document.getElementById('mode').value;
  document.getElementById('filter-wrap').style.display = mode === 'direct_range' ? 'none' : 'block';
  document.getElementById('filter-wrap-2').style.display = mode === 'direct_range' ? 'none' : 'block';
}}
function onCycleChange() {{
  document.getElementById('week-wrap').style.display = document.getElementById('cycle').value === 'weekly' ? 'block' : 'none';
}}
function onMergeChange() {{
  const on = document.getElementById('merge_enabled').value === '是';
  document.getElementById('single-task-wrap').style.display = on ? 'none' : 'block';
  document.getElementById('merge-task-wrap').style.display = on ? 'block' : 'none';
}}
onModeChange(); onCycleChange(); onMergeChange();
</script></div>'''


def render_page(message='', result=None, error=''):
    vm = build_view_model()
    scheduler_status = vm['scheduler_status']
    state = vm['webui_state']
    runtime = vm.get('webui_runtime', {}) or {}
    queue_items = scheduler_status.get('queue', []) or []
    last_events = scheduler_status.get('last_events', []) or []
    queue_html = ''.join(f'<li><code>{html.escape(str(x))}</code></li>' for x in queue_items) or '<li>空</li>'
    event_html = ''.join(
        f"<li><code>{html.escape(str(evt.get('time', '')))}</code> | {html.escape(str(evt.get('kind', '')))} | <code>{html.escape(str(evt.get('task_id', '') or '-'))}</code> | {html.escape(str(evt.get('detail', '') or ''))}</li>"
        for evt in last_events[:10]
    ) or '<li>暂无</li>'
    robot_rows = ''.join(
        f"<tr><td><code>{html.escape(r['id'])}</code></td><td>{html.escape(r['name'])}</td><td>{('<span class=\"badge badge-success\">启用</span>' if r['enabled'] else '<span class=\"badge badge-muted\">停用</span>')}</td><td>{('<span class=\"chip chip-ok\">已配置</span>' if r['webhook_configured'] else '<span class=\"chip chip-warn\">未配置</span>')} <code>{html.escape(r['webhook_masked'] or '')}</code></td><td>{('<span class=\"chip chip-ok\">已配置</span>' if r['secret_configured'] else '<span class=\"chip chip-warn\">未配置</span>')} <code>{html.escape(r['secret_masked'] or '')}</code></td><td class='row-actions'><button type='button' class='row-action edit-action' onclick='openRobotEditPopup({json.dumps(r['id'], ensure_ascii=False)})'>编辑</button><form method='post' action='/delete-robot' class='inline-form' onsubmit=\"return confirm('确定删除机器人 {html.escape(r['id'])} 吗？');\"><input type='hidden' name='robot_id' value='{html.escape(r['id'])}'><button type='submit' class='row-action delete-action'>删除</button></form></td></tr>"
        for r in vm['robots']
    ) or '<tr><td colspan="6" class="empty-row">暂无机器人配置</td></tr>'
    log_rows = ''.join(
        f"<tr><td>{row.get('id','')}</td><td>{html.escape(row.get('send_time',''))}</td><td>{html.escape(row.get('task_id',''))}</td></tr>"
        for row in vm['send_logs'][:100]
    ) or '<tr><td colspan="3">暂无发送日志</td></tr>'
    result_html = f"<div class='card'><h2>运行结果</h2><pre>{html.escape(json.dumps(result, ensure_ascii=False, indent=2))}</pre></div>" if result is not None else ''
    error_html = f"<div class='card'><h2 style='color:#b00020;'>错误</h2><pre>{html.escape(error)}</pre></div>" if error else ''
    auto_checked = 'checked' if state.get('auto_start_scheduler', True) else ''
    scheduler_btn = '<form method="post" action="/scheduler/stop" class="inline-form"><button type="submit" class="tool-btn danger">停止调度</button></form>' if scheduler_status.get('scheduler_running') else '<form method="post" action="/scheduler/start" class="inline-form"><button type="submit" class="tool-btn success">启动调度</button></form>'
    return f'''<!doctype html><html><head><meta charset="utf-8"><title>DingtalkChatbot 任务中心</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Noto+Sans+SC:wght@400;500;600;700&display=swap');

:root {{
  --primary: #2563eb;
  --primary-hover: #1d4ed8;
  --primary-light: #eff6ff;
  --success: #16a34a;
  --success-hover: #15803d;
  --success-light: #f0fdf4;
  --danger: #dc2626;
  --danger-hover: #b91c1c;
  --danger-light: #fef2f2;
  --warning: #f59e0b;
  --warning-light: #fffbeb;
  --text-primary: #111827;
  --text-secondary: #4b5563;
  --text-muted: #6b7280;
  --bg-primary: #f8fafc;
  --bg-card: #ffffff;
  --bg-hover: #f1f5f9;
  --border: #e2e8f0;
  --border-light: #f1f5f9;
  --shadow-sm: 0 1px 2px 0 rgba(0, 0, 0, 0.05);
  --shadow-md: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -2px rgba(0, 0, 0, 0.1);
  --shadow-lg: 0 10px 15px -3px rgba(0, 0, 0, 0.1), 0 4px 6px -4px rgba(0, 0, 0, 0.1);
  --radius-sm: 6px;
  --radius-md: 8px;
  --radius-lg: 12px;
  --radius-xl: 16px;
}}

* {{
  box-sizing: border-box;
}}

body {{
  font-family: 'Inter', 'Noto Sans SC', -apple-system, BlinkMacSystemFont, sans-serif;
  margin: 0;
  padding: 24px;
  background: linear-gradient(135deg, #f0f4ff 0%, #f8fafc 50%, #f0fdf4 100%);
  color: var(--text-primary);
  min-height: 100vh;
  line-height: 1.5;
}}

.container {{
  max-width: 1600px;
  margin: 0 auto;
}}

.card {{
  background: var(--bg-card);
  border-radius: var(--radius-xl);
  padding: 24px;
  margin-bottom: 20px;
  box-shadow: var(--shadow-md);
  border: 1px solid var(--border-light);
  transition: box-shadow 0.2s ease, transform 0.2s ease;
}}

.card:hover {{
  box-shadow: var(--shadow-lg);
}}

h1, h2, h3, h4 {{
  font-weight: 700;
  color: var(--text-primary);
  letter-spacing: -0.025em;
}}

h1 {{
  font-size: 28px;
  margin: 0;
  background: linear-gradient(135deg, var(--primary) 0%, #7c3aed 100%);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}}

h2 {{
  font-size: 20px;
  margin: 0;
}}

button {{
  font-family: inherit;
  padding: 10px 18px;
  border: none;
  border-radius: var(--radius-md);
  background: var(--primary);
  color: white;
  cursor: pointer;
  font-weight: 600;
  font-size: 14px;
  transition: all 0.2s ease;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
}}

button:hover {{
  background: var(--primary-hover);
  transform: translateY(-1px);
  box-shadow: 0 4px 12px rgba(37, 99, 235, 0.3);
}}

button:active {{
  transform: translateY(0);
}}

button.tool-btn {{
  padding: 8px 14px;
  font-size: 13px;
  min-width: 44px;
  margin-top: 0;
  margin-right: 0;
}}

button.tool-btn.secondary {{
  background: var(--text-secondary);
}}

button.tool-btn.secondary:hover {{
  background: var(--text-primary);
  box-shadow: 0 4px 12px rgba(75, 85, 99, 0.3);
}}

button.tool-btn.success {{
  background: var(--success);
}}

button.tool-btn.success:hover {{
  background: var(--success-hover);
  box-shadow: 0 4px 12px rgba(22, 163, 74, 0.3);
}}

button.tool-btn.danger {{
  background: var(--danger);
}}

button.tool-btn.danger:hover {{
  background: var(--danger-hover);
  box-shadow: 0 4px 12px rgba(220, 38, 38, 0.3);
}}

button.link-btn {{
  background: var(--primary-light);
  color: var(--primary);
  border: 1px solid rgba(37, 99, 235, 0.2);
}}

button.link-btn:hover {{
  background: rgba(37, 99, 235, 0.1);
}}

input, select, textarea {{
  font-family: inherit;
  padding: 10px 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  width: 100%;
  font-size: 14px;
  transition: all 0.2s ease;
  background: var(--bg-card);
  color: var(--text-primary);
}}

input:focus, select:focus, textarea:focus {{
  outline: none;
  border-color: var(--primary);
  box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.1);
}}

input::placeholder, textarea::placeholder {{
  color: var(--text-muted);
}}

label {{
  display: block;
  font-weight: 600;
  margin-bottom: 8px;
  color: var(--text-primary);
  font-size: 14px;
}}

.grid {{
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 20px;
}}

.top-layout {{
  display: grid;
  grid-template-columns: minmax(0, 1.45fr) minmax(420px, 0.95fr);
  gap: 20px;
  align-items: start;
}}

.table-row {{
  margin-bottom: 20px;
}}

table {{
  width: 100%;
  border-collapse: separate;
  border-spacing: 0;
  font-size: 13px;
  border-radius: var(--radius-lg);
  overflow: hidden;
  border: 1px solid var(--border);
}}

th, td {{
  padding: 12px 14px;
  vertical-align: top;
  text-align: left;
  border-bottom: 1px solid var(--border);
}}

th {{
  background: var(--bg-primary);
  font-weight: 600;
  color: var(--text-primary);
  position: sticky;
  top: 0;
  z-index: 1;
}}

tr:last-child td {{
  border-bottom: none;
}}

tr:hover td {{
  background: var(--bg-hover);
}}

pre {{
  background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
  color: #e2e8f0;
  padding: 20px;
  border-radius: var(--radius-lg);
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  font-size: 13px;
  line-height: 1.6;
  border: 1px solid #334155;
}}

code {{
  background: var(--bg-primary);
  padding: 3px 8px;
  border-radius: var(--radius-sm);
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  font-size: 12px;
  color: var(--primary);
  border: 1px solid var(--border);
}}

.inline-form {{
  display: inline-block;
  margin: 0;
}}

.msg {{
  padding: 14px 18px;
  background: var(--success-light);
  color: var(--success);
  border-radius: var(--radius-lg);
  margin-bottom: 20px;
  font-weight: 500;
  border: 1px solid rgba(22, 163, 74, 0.2);
  display: flex;
  align-items: center;
  gap: 10px;
}}

.msg::before {{
  content: "✓";
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  background: var(--success);
  color: white;
  border-radius: 50%;
  font-size: 14px;
  font-weight: bold;
}}

.muted {{
  color: var(--text-muted);
  font-size: 13px;
}}

.header-line {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
  margin-bottom: 20px;
}}

.header-actions {{
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}}

.toolbar-actions {{
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}}

.summary-grid {{
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 20px;
  margin-top: 20px;
}}

.info-stack {{
  display: grid;
  gap: 12px;
}}

.info-panel {{
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 14px 16px;
  background: var(--bg-primary);
  transition: all 0.2s ease;
}}

.info-panel:hover {{
  border-color: var(--primary);
  background: var(--primary-light);
}}

.info-panel b {{
  display: block;
  margin-bottom: 8px;
  color: var(--text-primary);
  font-weight: 600;
}}

.compact-list {{
  margin: 0;
  padding-left: 20px;
  color: var(--text-secondary);
}}

.compact-list li {{
  margin: 6px 0;
  line-height: 1.5;
  padding: 4px 0;
}}

.inline-config {{
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 12px;
  align-items: end;
  margin-top: 16px;
}}

.inline-config label {{
  margin-bottom: 0;
}}

.inline-config .field-wrap {{
  display: grid;
  gap: 8px;
}}

.split-stack {{
  display: grid;
  gap: 20px;
}}

/* 自定义滚动条样式 */
* {{
  scrollbar-width: thin;
  scrollbar-color: var(--border) transparent;
}}

*::-webkit-scrollbar {{
  width: 8px;
  height: 8px;
}}

*::-webkit-scrollbar-track {{
  background: var(--bg-primary);
  border-radius: 4px;
}}

*::-webkit-scrollbar-thumb {{
  background: var(--border);
  border-radius: 4px;
  border: 2px solid var(--bg-primary);
}}

*::-webkit-scrollbar-thumb:hover {{
  background: var(--text-muted);
}}

/* 表格容器滚动条 */
div[style*="overflow"] {{
  border-radius: var(--radius-lg);
}}

div[style*="overflow"] table {{
  margin: 0;
}}

div[style*="overflow"] th {{
  background: var(--bg-primary);
}}

/* 列表项动画 */
ul.compact-list li {{
  transition: all 0.2s ease;
}}

ul.compact-list li:hover {{
  background: var(--bg-hover);
  padding-left: 8px;
  border-radius: var(--radius-sm);
}}

/* 状态指示器 */
.status-running {{
  color: var(--success);
  font-weight: 600;
}}

.status-stopped {{
  color: var(--text-muted);
}}

/* 响应式设计 */
@media (max-width: 1280px) {{
  .top-layout {{
    grid-template-columns: 1fr;
  }}
  .summary-grid {{
    grid-template-columns: 1fr;
  }}
  .inline-config {{
    grid-template-columns: 1fr;
  }}
}}

@media (max-width: 768px) {{
  body {{
    padding: 16px;
  }}
  .grid {{
    grid-template-columns: 1fr;
  }}
  .card {{
    padding: 16px;
  }}
  h1 {{
    font-size: 24px;
  }}
}}

/* 表单元素增强 */
select {{
  appearance: none;
  background-image: url("data:image/svg+xml,%3csvg xmlns='http://www.w3.org/2000/svg' fill='none' viewBox='0 0 20 20'%3e%3cpath stroke='%236b7280' stroke-linecap='round' stroke-linejoin='round' stroke-width='1.5' d='M6 8l4 4 4-4'/%3e%3c/svg%3e");
  background-position: right 10px center;
  background-repeat: no-repeat;
  background-size: 20px;
  padding-right: 40px;
}}

/* 表格操作列 */
td.row-actions {{
  white-space: normal;
  min-width: 280px;
}}

td.row-actions .inline-form {{
  display: inline-flex;
}}

/* 信息面板网格 */
.info-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 12px;
}}

.info-item {{
  padding: 12px;
  background: var(--bg-primary);
  border-radius: var(--radius-md);
  border: 1px solid var(--border-light);
}}

.info-item .label {{
  font-size: 12px;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin-bottom: 4px;
}}

.info-item .value {{
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}}

/* 表格容器 */
.table-container {{
  overflow: auto;
  max-height: 500px;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
}}

.table-container table {{
  margin: 0;
  min-width: 100%;
}}

.table-container th {{
  position: sticky;
  top: 0;
  z-index: 1;
  background: var(--bg-primary);
  border-bottom: 2px solid var(--border);
}}

/* 卡片标题区域 */
.card-header {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
  padding-bottom: 16px;
  border-bottom: 1px solid var(--border-light);
}}

/* 按钮组样式 */
.btn-group {{
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}}

/* 状态徽章 */
.badge {{
  display: inline-flex;
  align-items: center;
  padding: 4px 10px;
  border-radius: 20px;
  font-size: 12px;
  font-weight: 600;
  gap: 6px;
}}

.badge-success {{
  background: var(--success-light);
  color: var(--success);
}}

.badge-danger {{
  background: var(--danger-light);
  color: var(--danger);
}}

.badge-warning {{
  background: var(--warning-light);
  color: var(--warning);
}}

.badge-muted {{
  background: var(--bg-primary);
  color: var(--text-muted);
}}

.badge::before {{
  content: "";
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: currentColor;
}}

.chip {{
  display: inline-flex;
  align-items: center;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.02em;
  margin-right: 4px;
}}

.chip-ok {{
  background: var(--success-light);
  color: var(--success);
  border: 1px solid rgba(22, 163, 74, 0.18);
}}

.chip-warn {{
  background: var(--warning-light);
  color: var(--warning);
  border: 1px solid rgba(245, 158, 11, 0.22);
}}

/* 表格行操作按钮组 */
.row-actions {{
  white-space: nowrap;
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  align-items: center;
}}

.row-action {{
  padding: 5px 11px;
  font-size: 12px;
  font-weight: 600;
  border-radius: 7px;
  border: 1px solid transparent;
  margin: 0;
  line-height: 1.3;
  transition: all 0.15s ease;
  min-width: 0;
}}

.row-action:hover {{
  transform: translateY(-1px);
  box-shadow: 0 4px 10px rgba(15, 23, 42, 0.08);
}}

.row-action.edit-action {{
  background: var(--primary-light);
  color: var(--primary);
  border-color: rgba(37, 99, 235, 0.2);
}}

.row-action.edit-action:hover {{
  background: var(--primary);
  color: #fff;
  border-color: var(--primary);
}}

.row-action.run-action {{
  background: rgba(22, 163, 74, 0.10);
  color: var(--success);
  border-color: rgba(22, 163, 74, 0.22);
}}

.row-action.run-action:hover {{
  background: var(--success);
  color: #fff;
  border-color: var(--success);
}}

.row-action.upload-action {{
  background: rgba(245, 158, 11, 0.10);
  color: var(--warning);
  border-color: rgba(245, 158, 11, 0.22);
}}

.row-action.upload-action:hover {{
  background: var(--warning);
  color: #fff;
  border-color: var(--warning);
}}

.row-action.toggle-action {{
  background: var(--bg-primary);
  color: var(--text-secondary);
  border-color: var(--border);
}}

.row-action.toggle-action:hover {{
  background: var(--text-secondary);
  color: #fff;
  border-color: var(--text-secondary);
}}

.row-action.toggle-off:hover {{
  background: var(--warning);
  border-color: var(--warning);
}}

.row-action.toggle-on:hover {{
  background: var(--success);
  border-color: var(--success);
}}

.row-action.delete-action {{
  background: var(--danger-light);
  color: var(--danger);
  border-color: rgba(220, 38, 38, 0.2);
}}

.row-action.delete-action:hover {{
  background: var(--danger);
  color: #fff;
  border-color: var(--danger);
}}

.row-action[disabled] {{
  opacity: 0.6;
  cursor: not-allowed;
  transform: none;
}}

.empty-row {{
  text-align: center;
  color: var(--text-muted);
  font-size: 13px;
  padding: 32px 16px !important;
  font-style: italic;
}}

.schedule-code {{
  font-size: 11px;
  max-width: 200px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  display: inline-block;
  vertical-align: middle;
}}

/* 表格行状态 */
tr.row-enabled td {{
  background: linear-gradient(90deg, rgba(22, 163, 74, 0.04), transparent 28%);
}}

tr.row-disabled td {{
  background: var(--bg-primary);
  color: var(--text-muted);
  opacity: 0.78;
}}

tr.row-enabled:hover td,
tr.row-disabled:hover td {{
  background: var(--bg-hover);
}}

/* 模态框样式 */
.modal-overlay {{
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.55);
  backdrop-filter: blur(6px);
  -webkit-backdrop-filter: blur(6px);
  z-index: 1000;
  justify-content: center;
  align-items: flex-start;
  padding: 56px 20px 40px;
  animation: fadeIn 0.18s ease;
}}

.modal-overlay.active {{
  display: flex;
}}

@keyframes fadeIn {{
  from {{ opacity: 0; }}
  to {{ opacity: 1; }}
}}

@keyframes slideUp {{
  from {{
    opacity: 0;
    transform: translateY(16px) scale(0.985);
  }}
  to {{
    opacity: 1;
    transform: translateY(0) scale(1);
  }}
}}

.modal-content {{
  position: relative;
  background: #1e293b;
  border-radius: 18px;
  padding: 28px 28px 24px;
  max-width: 780px;
  width: 100%;
  max-height: calc(100vh - 96px);
  overflow-y: auto;
  box-shadow: 0 32px 64px -12px rgba(2, 6, 23, 0.55), 0 0 0 1px rgba(148, 163, 184, 0.08);
  border: 1px solid #334155;
  animation: slideUp 0.22s cubic-bezier(0.16, 1, 0.3, 1);
}}

.modal-content.compact {{
  max-width: 560px;
}}

.modal-content::-webkit-scrollbar {{
  width: 6px;
}}

.modal-content::-webkit-scrollbar-track {{
  background: #1e293b;
  border-radius: 3px;
}}

.modal-content::-webkit-scrollbar-thumb {{
  background: #475569;
  border-radius: 3px;
}}

.modal-content::-webkit-scrollbar-thumb:hover {{
  background: #64748b;
}}

.modal-close {{
  position: absolute;
  top: 14px;
  right: 14px;
  width: 32px;
  height: 32px;
  border-radius: 10px;
  background: rgba(148, 163, 184, 0.15);
  border: none;
  color: #cbd5e1;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 18px;
  line-height: 1;
  transition: all 0.18s ease;
  z-index: 2;
}}

.modal-close:hover {{
  background: rgba(248, 113, 113, 0.2);
  color: #fecaca;
  transform: rotate(90deg);
}}

.modal-loading {{
  text-align: center;
  padding: 56px 24px;
  color: #94a3b8;
}}

.modal-spinner {{
  width: 28px;
  height: 28px;
  border: 3px solid #334155;
  border-top-color: #60a5fa;
  border-radius: 50%;
  animation: spin 0.9s linear infinite;
  margin: 0 auto 14px;
}}

.modal-error-banner {{
  padding: 12px 16px;
  margin-bottom: 16px;
  background: rgba(220, 38, 38, 0.12);
  color: #fca5a5;
  border: 1px solid rgba(220, 38, 38, 0.3);
  border-radius: 10px;
  font-size: 13px;
  line-height: 1.55;
  word-break: break-word;
  max-height: 200px;
  overflow: auto;
}}

.app-toast {{
  position: fixed;
  left: 50%;
  bottom: 36px;
  transform: translate(-50%, 16px);
  background: linear-gradient(135deg, #0f172a, #1e293b);
  color: #f1f5f9;
  padding: 12px 22px;
  border-radius: 999px;
  box-shadow: 0 16px 40px -10px rgba(15, 23, 42, 0.5);
  font-size: 14px;
  font-weight: 500;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.22s ease, transform 0.22s ease;
  z-index: 2000;
  border: 1px solid rgba(148, 163, 184, 0.18);
}}

.app-toast.show {{
  opacity: 1;
  transform: translate(-50%, 0);
}}

.app-toast.error {{
  background: linear-gradient(135deg, #7f1d1d, #b91c1c);
  border-color: rgba(252, 165, 165, 0.3);
}}

/* 模态框内的表单元素 */
.modal-content input[type="text"],
.modal-content input[type="password"],
.modal-content input[type="email"],
.modal-content input[type="number"],
.modal-content select,
.modal-content textarea {{
  width: 100%;
  padding: 10px 12px;
  background: #0f172a;
  border: 1px solid #334155;
  border-radius: 8px;
  color: #e2e8f0;
  box-sizing: border-box;
  font-size: 14px;
  transition: border-color 0.2s ease, box-shadow 0.2s ease;
}}

.modal-content input:focus,
.modal-content select:focus,
.modal-content textarea:focus {{
  outline: none;
  border-color: #3b82f6;
  box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.2);
}}

.modal-content label {{
  display: block;
  font-weight: 600;
  margin-bottom: 6px;
  color: #94a3b8;
  font-size: 13px;
}}

.modal-content button[type="submit"] {{
  padding: 10px 20px;
  background: #10b981;
  color: #fff;
  border: none;
  border-radius: 8px;
  cursor: pointer;
  font-weight: 500;
  transition: all 0.2s ease;
}}

.modal-content button[type="submit"]:hover {{
  background: #059669;
  box-shadow: 0 4px 12px rgba(16, 185, 129, 0.3);
}}

.modal-content button[type="button"] {{
  padding: 10px 20px;
  background: #475569;
  color: #e2e8f0;
  border: none;
  border-radius: 8px;
  cursor: pointer;
  font-weight: 500;
  transition: all 0.2s ease;
}}

.modal-content button[type="button"]:hover {{
  background: #64748b;
}}
</style>
<script>
function isEmbeddedOrMobile(){{
  const ua = navigator.userAgent || '';
  return /Telegram|MicroMessenger|Mobile|Android|iPhone|iPad/i.test(ua);
}}

function openManagedPage(url, name, features){{
  if (isEmbeddedOrMobile()) {{
    window.location.href = url;
    return;
  }}
  const win = window.open(url, name, features);
  if (!win) {{
    window.location.href = url;
  }}
}}

function openModal(title, url, options) {{
  const opts = options || {{}};
  const overlay = document.getElementById('modal-overlay');
  const content = document.getElementById('modal-content');
  if (!overlay || !content) {{
    window.location.href = url;
    return;
  }}
  if (opts.compact) content.classList.add('compact');
  else content.classList.remove('compact');
  content.innerHTML = '<button type="button" class="modal-close" onclick="closeModal()" aria-label="关闭">&times;</button><div class="modal-loading"><div class="modal-spinner"></div>加载中…</div>';
  overlay.classList.add('active');
  document.body.style.overflow = 'hidden';

  fetch(url, {{ headers: {{ 'X-Requested-With': 'XMLHttpRequest' }} }})
    .then(response => response.text())
    .then(htmlText => {{
      content.innerHTML = '<button type="button" class="modal-close" onclick="closeModal()" aria-label="关闭">&times;</button>' + htmlText;
      content.scrollTop = 0;
      content.querySelectorAll('script').forEach(script => {{
        const newScript = document.createElement('script');
        newScript.textContent = script.textContent;
        newScript.dataset.modalInjected = 'true';
        document.body.appendChild(newScript);
      }});
      content.querySelectorAll('form').forEach(form => {{
        form.addEventListener('submit', handleModalFormSubmit);
      }});
      const firstInput = content.querySelector('input:not([type=hidden]):not([type=checkbox]), select, textarea');
      if (firstInput) {{
        try {{ firstInput.focus(); }} catch (e) {{}}
      }}
    }})
    .catch(error => {{
      content.innerHTML = '<button type="button" class="modal-close" onclick="closeModal()" aria-label="关闭">&times;</button><div class="modal-error-banner">加载失败：' + (error && error.message ? error.message : error) + '</div>';
    }});
}}

function closeModal() {{
  const overlay = document.getElementById('modal-overlay');
  if (!overlay) return;
  overlay.classList.remove('active');
  document.body.style.overflow = '';
  document.querySelectorAll('script[data-modal-injected]').forEach(s => s.remove());
}}

async function handleModalFormSubmit(e) {{
  e.preventDefault();
  const form = e.currentTarget;
  const submitBtn = form.querySelector('button[type="submit"]');
  const originalText = submitBtn ? submitBtn.textContent : '';
  if (submitBtn) {{
    submitBtn.disabled = true;
    submitBtn.dataset.originalText = originalText;
    submitBtn.textContent = '处理中…';
  }}
  try {{
    const formData = new FormData(form);
    const params = new URLSearchParams();
    formData.forEach((value, key) => {{ params.append(key, value); }});
    const response = await fetch(form.action, {{
      method: (form.method || 'POST').toUpperCase(),
      headers: {{ 'X-Requested-With': 'XMLHttpRequest', 'Accept': 'text/html' }},
      body: params,
    }});
    if (response.ok) {{
      closeModal();
      showToast('保存成功，正在刷新…');
      setTimeout(() => window.location.reload(), 360);
    }} else {{
      const text = await response.text();
      const match = text.match(/<pre[^>]*>([\\s\\S]*?)<\\/pre>/);
      const detail = (match ? match[1] : '请稍后重试').trim();
      showModalError(detail);
      if (submitBtn) {{
        submitBtn.disabled = false;
        submitBtn.textContent = submitBtn.dataset.originalText || '保存';
      }}
    }}
  }} catch (err) {{
    showModalError(err && err.message ? err.message : String(err));
    if (submitBtn) {{
      submitBtn.disabled = false;
      submitBtn.textContent = submitBtn.dataset.originalText || '保存';
    }}
  }}
}}

function showModalError(detail) {{
  const content = document.getElementById('modal-content');
  if (!content) return;
  let errBox = content.querySelector('.modal-error-banner');
  if (!errBox) {{
    errBox = document.createElement('div');
    errBox.className = 'modal-error-banner';
    const closeBtn = content.querySelector('.modal-close');
    if (closeBtn && closeBtn.nextSibling) content.insertBefore(errBox, closeBtn.nextSibling);
    else content.insertBefore(errBox, content.firstChild);
  }}
  errBox.textContent = '保存失败：' + String(detail).slice(0, 600);
  errBox.scrollIntoView({{ behavior: 'smooth', block: 'nearest' }});
}}

function showToast(message, kind) {{
  let toast = document.getElementById('app-toast');
  if (!toast) {{
    toast = document.createElement('div');
    toast.id = 'app-toast';
    toast.className = 'app-toast';
    document.body.appendChild(toast);
  }}
  toast.textContent = message;
  toast.classList.remove('error');
  if (kind === 'error') toast.classList.add('error');
  toast.classList.add('show');
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(() => toast.classList.remove('show'), 2400);
}}

function openRobotConfigPopup(){{openModal('新增机器人', '/robot-edit', {{compact:true}});}}
function openRobotEditPopup(robotId){{openModal('编辑机器人', '/robot-edit?robot_id=' + encodeURIComponent(robotId), {{compact:true}});}}
function openTaskPopup(kind){{const qs=kind==='merge' ? '?merge=1' : ''; openModal('新增任务', '/edit'+qs);}}
function openTaskEditPopup(taskId, isMerge){{const qs='?task_id='+encodeURIComponent(taskId)+(isMerge ? '&merge=1' : ''); openModal('编辑任务', '/edit'+qs);}}
function openCaptureBrowser(){{window.location.href='/open-browser';}}
function confirmShutdownProject(){{
  return confirm('确定关闭该项目吗？\n\n将停止调度器、关闭截图浏览器，并退出当前 WebUI。');
}}

document.addEventListener('click', function(e) {{
  if (e.target.id === 'modal-overlay') {{
    closeModal();
  }}
}});

document.addEventListener('keydown', function(e) {{
  if (e.key === 'Escape') {{
    closeModal();
  }}
}});
</script>
<style>
@keyframes spin {{
  to {{ transform: rotate(360deg); }}
}}
</style></head><body><div class="container"><div class="card"><div class="header-line"><h1 style="margin:0;">DingtalkChatbot 任务中心</h1><div class="toolbar-actions">{scheduler_btn}<button type="button" class="tool-btn secondary" onclick="openCaptureBrowser()">打开浏览器</button><form method="post" action="/restart-project" class="inline-form"><button type="submit" class="tool-btn">重启项目</button></form><form method="post" action="/shutdown-project" class="inline-form" onsubmit="return confirmShutdownProject()"><button type="submit" class="tool-btn danger">关闭项目</button></form></div></div>{('<div class="msg">'+html.escape(message)+'</div>') if message else ''}<div class="card" style="margin-top:16px;"><div class="header-line"><h2 style="margin:0;">浏览器使用说明</h2></div><ul class="compact-list" style="margin-top:10px;"><li>点击“打开浏览器”后，会在当前 Windows 机器上启动截图浏览器，不会在手机网页里打开新页。</li><li>如果浏览器已启动但你看不到窗口，请直接在项目根目录双击 <code>打开截图浏览器.bat</code>。</li><li>如果首次打开停在“钉钉统一身份认证”，请先在该 Chrome 窗口里完成一次登录，后续截图会复用这个登录态。</li></ul></div><div class="top-layout" style="margin-top:16px;"><div class="card" style="margin-bottom:0;"><div class="header-line"><h2 style="margin:0;">调度概览</h2><div class="header-actions"><form method="post" action="/settings/auto-start" class="inline-form"><label style="margin:0;display:flex;align-items:center;gap:8px;font-weight:500;"><input type="checkbox" name="auto_start_scheduler" value="1" {auto_checked} style="width:auto;margin:0;">启动后自动启用调度</label><button type="submit" class="tool-btn success">保存设置</button></form></div></div><div class="summary-grid"><div><div>调度运行中：<code>{'是' if scheduler_status.get('scheduler_running') else '否'}</code></div><div>调度器 PID：<code>{html.escape(str(scheduler_status.get('pid') or '未启动'))}</code></div><div>当前执行：<code>{html.escape(str(scheduler_status.get('current_task') or '无'))}</code></div><div>最近更新时间：<code>{html.escape(str(scheduler_status.get('updated_at') or '未知'))}</code></div><div style="margin-top:10px; padding-top:10px; border-top:1px dashed #d0d5dd;"><div>当前 WebUI PID：<code>{html.escape(str(runtime.get('pid') or '未知'))}</code></div><div>WebUI 启动时间：<code>{html.escape(str(runtime.get('started_at') or '未知'))}</code></div><div>代码文件：<code>{html.escape(str(vm.get('webui_file_name') or 'webui_server.py'))}</code></div><div>代码更新时间：<code>{html.escape(str(vm.get('webui_version') or '未知'))}</code></div><div>最近重启时间：<code>{html.escape(str(state.get('last_restart_at') or '未记录'))}</code></div></div><div class="muted" style="margin-top:10px;">“关闭项目”会停止调度器、关闭截图浏览器，并退出当前 WebUI 进程。</div></div><div class="info-stack"><div class="info-panel" style="max-height:200px;overflow:auto;"><b>排队任务</b><ul class="compact-list">{queue_html}</ul></div><div class="info-panel" style="max-height:200px;overflow:auto;"><b>最近事件</b><ul class="compact-list">{event_html}</ul></div></div></div></div><div class="split-stack"><div class="card" style="margin-bottom:0;"><div class="header-line"><h2 style="margin:0;">多钉钉机器人配置</h2><div class="header-actions"><button type="button" class="tool-btn secondary" onclick="openRobotConfigPopup()" title="添加机器人配置">新增机器人</button></div></div><div class="table-container" style="margin-top:16px;"><table><thead><tr><th>ID</th><th>名称</th><th>状态</th><th>Webhook</th><th>Secret</th><th>操作</th></tr></thead><tbody>{robot_rows}</tbody></table></div></div><div class="card" style="margin-bottom:0;"><div class="header-line"><h2 style="margin:0;">图床配置</h2></div><form method="post" action="/save-imgbed" class="inline-config"><div class="field-wrap"><label>图床上传地址</label><input name="imgbed_upload_url" value="{html.escape(vm.get('default_imgbed_upload_url') or '')}" placeholder="请输入图床上传地址"></div><button type="submit" class="tool-btn success">保存图床配置</button></form></div></div></div></div>
<div class="table-row"><div class="card"><div class="header-line"><h2 style="margin:0;">任务</h2><div class="header-actions"><button type="button" class="tool-btn" title="新增任务" onclick="openTaskPopup('')">新增任务</button><a href="/export-tasks.csv"><button type="button" class="tool-btn secondary">导出CSV</button></a><a href="/export-tasks-template.csv"><button type="button" class="tool-btn secondary">下载模板</button></a></div></div><div class="muted">普通任务：保留当前任务字段，并新增机器人、标签关键字字段。</div><form method="post" action="/import-tasks" enctype="multipart/form-data" class="inline-config"><div class="field-wrap"><label>导入任务CSV</label><input type="file" name="csv_file" accept=".csv"></div><button type="submit" class="tool-btn success">导入CSV</button></form><div class="muted" style="margin-top:8px;">导入时如遇相同任务ID或相同配置内容，会自动跳过，不覆盖现有任务；导入完成后会展示新增/跳过明细。</div><div class="table-container" style="margin-top:12px;"><table><thead><tr><th>ID</th><th>名称</th><th>机器人</th><th>状态</th><th>周期</th><th>下次执行</th><th>sheet</th><th>截图区域</th><th>筛选</th><th>标签关键字</th><th>操作</th></tr></thead><tbody>{_task_rows(vm['normal_tasks'], merge=False)}</tbody></table></div></div></div>
<div class="table-row"><div class="card"><div class="header-line"><h2 style="margin:0;">合并任务</h2><div class="header-actions"><button type="button" class="tool-btn" title="新增合并任务" onclick="openTaskPopup('merge')">新增合并任务</button></div></div><div class="muted">合并任务：与任务表共用大部分结构，重点展示子任务列表与消息标题。</div><div class="table-container" style="margin-top:12px;"><table><thead><tr><th>ID</th><th>名称</th><th>机器人</th><th>状态</th><th>周期</th><th>下次执行</th><th>sheet</th><th>子任务ID</th><th>消息标题</th><th>标签关键字</th><th>操作</th></tr></thead><tbody>{_task_rows(vm['merge_tasks'], merge=True)}</tbody></table></div></div></div>
<div class="table-row"><div class="card"><div class="header-line"><h2 style="margin:0;">发送日志</h2></div><div class="muted">按你的要求，先只展示：序号、发送时间、任务ID。</div><div class="table-container" style="margin-top:12px;"><table><thead><tr><th>序号</th><th>发送时间</th><th>任务ID</th></tr></thead><tbody>{log_rows}</tbody></table></div></div></div>
{result_html}{error_html}
<div id="modal-overlay" class="modal-overlay" role="dialog" aria-modal="true">
  <div id="modal-content" class="modal-content"></div>
</div>
<div id="app-toast" class="app-toast"></div>
</body></html>'''


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        try:
            append_webui_error_log('HTTP ' + (format % args))
        except Exception:
            pass

    def _send_html(self, content, status=200):
        data = content.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _safe_send_error(self, status, title, detail):
        body = f"<html><body><h1>{html.escape(title)}</h1><pre>{html.escape(detail)}</pre></body></html>"
        try:
            self._send_html(body, status=status)
        except Exception:
            pass

    def _redirect(self, location='/'):
        self.send_response(302)
        self.send_header('Location', location)
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()

    def _send_bytes(self, data, content_type='application/octet-stream', status=200, filename=''):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        if filename:
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def _read_form(self):
        length = int(self.headers.get('Content-Length', '0'))
        raw = self.rfile.read(length).decode('utf-8')
        return parse_qs(raw)

    def _read_multipart(self):
        form = FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                'REQUEST_METHOD': 'POST',
                'CONTENT_TYPE': self.headers.get('Content-Type', ''),
            },
        )
        fields = {}
        files = {}
        for key in form.keys():
            item = form[key]
            items = item if isinstance(item, list) else [item]
            for entry in items:
                if entry.filename:
                    files.setdefault(key, []).append({
                        'filename': entry.filename,
                        'content': entry.file.read(),
                    })
                else:
                    fields.setdefault(key, []).append(entry.value)
        return fields, files

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            page_message = (qs.get('message') or [''])[0]
            if parsed.path == '/config':
                cfg = sanitize_config_for_display(load_config(CONFIG_PATH))
                self._send_html('<pre>{}</pre><p><a href="/">返回</a></p>'.format(html.escape(json.dumps(cfg, ensure_ascii=False, indent=2))))
                return
            if parsed.path == '/export-tasks.csv':
                data = export_tasks_csv_text().encode('utf-8-sig')
                self._send_bytes(data, content_type='text/csv; charset=utf-8', filename='tasks_export.csv')
                return
            if parsed.path == '/export-tasks-template.csv':
                data = export_tasks_csv_template_text().encode('utf-8-sig')
                self._send_bytes(data, content_type='text/csv; charset=utf-8', filename='tasks_template.csv')
                return
            if parsed.path == '/robot-edit':
                robot_id = (qs.get('robot_id') or [''])[0]
                cfg = load_config(CONFIG_PATH)
                robot = next((x for x in cfg.get('defaults', {}).get('robot_configs', []) if x.get('id') == robot_id), {}) if robot_id else {}
                self._send_html(render_robot_form('编辑机器人' if robot_id else '新增机器人', robot=robot))
                return
            if parsed.path == '/open-browser':
                browser_result = ensure_capture_browser_open()
                target_url = str(browser_result.get('url') or '')
                brought_front = bool(browser_result.get('brought_front'))
                front_text = '并已尝试切到前台显示' if brought_front else '但未能确认已切到前台显示'
                suffix = f' 目标页：{target_url}' if target_url else ''
                self._redirect('/?message=' + quote('截图浏览器已在项目所在 Windows 机器上打开/保持运行（不是当前手机页面内打开），' + front_text + '。若首次打开停在钉钉统一身份认证，请先在该 Chrome 窗口完成登录。看不到窗口时，可在项目根目录双击“打开截图浏览器.bat”。' + suffix))
                return
            if parsed.path == '/edit':
                task_id = (qs.get('task_id') or [''])[0]
                merge = (qs.get('merge') or [''])[0] == '1'
                task = {}
                if task_id:
                    task = next((x for x in read_raw_config().get('tasks', []) if x.get('id') == task_id), {})
                elif merge:
                    task = {'merge': {'enabled': True, 'include_subtitles': True}, 'mode': 'merge_send'}
                action = '/save-edit?original_task_id=' + task_id if task_id else '/add-task'
                self._send_html(render_task_form('编辑任务' if task_id else ('新增合并任务' if merge else '新增任务'), action, task=task))
                return
            self._send_html(render_page(message=page_message))
        except Exception:
            detail = traceback.format_exc()
            append_webui_error_log(f'GET {self.path}\n{detail}')
            self._safe_send_error(500, 'GET failed', detail)

    def do_POST(self):
        task_id = ''
        try:
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            if parsed.path == '/shutdown-project':
                message = shutdown_project(delay_seconds=2.0)
                self._send_html(render_shutdown_page(message=message))
                return
            if parsed.path == '/import-tasks':
                form, files = self._read_multipart()
            else:
                form = self._read_form()
                files = {}
            task_id = (form.get('task_id') or [''])[0]
            if parsed.path == '/scheduler/start':
                self._send_html(render_page(message=start_scheduler(force=True)))
                return
            if parsed.path == '/scheduler/stop':
                self._send_html(render_page(message=stop_scheduler()))
                return
            if parsed.path == '/restart-project':
                self._send_html(render_page(message=restart_project()))
                return
            if parsed.path == '/settings/auto-start':
                enabled = (form.get('auto_start_scheduler') or [''])[0] == '1'
                message = update_auto_start_scheduler(enabled)
                self._send_html(render_page(message=message))
                return
            if parsed.path == '/save-imgbed':
                save_imgbed_config(form)
                self._send_html(render_page(message='图床配置已保存'))
                return
            if parsed.path == '/save-robot':
                robot_id = save_robot_config(form)
                self._send_html(render_popup_success(message=f'机器人 {robot_id} 已保存'))
                return
            if parsed.path == '/delete-robot':
                robot_id = (form.get('robot_id') or [''])[0].strip()
                delete_robot_config(robot_id)
                self._send_html(render_page(message=f'机器人 {robot_id} 已删除'))
                return
            if parsed.path == '/save-secrets':
                current = load_dingtalk_secrets()
                webhook = (form.get('webhook') or [''])[0].strip()
                secret = (form.get('secret') or [''])[0].strip()
                save_dingtalk_secrets(
                    webhook=current.get('webhook') if not webhook or webhook == KEEP_VALUE else webhook,
                    secret=current.get('secret') if not secret or secret == KEEP_VALUE else secret,
                )
                self._redirect('/?message=' + quote('默认钉钉 Webhook / Secret 已保存到 .env'))
                return
            if parsed.path == '/import-tasks':
                upload = ((files.get('csv_file') or [None])[0] or {})
                content = upload.get('content') or b''
                if not content:
                    raise ValueError('请选择一个 CSV 文件再导入')
                try:
                    text = content.decode('utf-8-sig')
                except UnicodeDecodeError:
                    text = content.decode('gb18030')
                result = import_tasks_from_csv_text(text)
                sync_scheduler_by_config()
                self._send_html(render_import_result(result))
                return
            if parsed.path == '/add-task':
                task = build_task_from_form(form)
                add_task(CONFIG_PATH, task)
                sync_scheduler_by_config()
                self._send_html(render_popup_success(message=f'任务 {task.get("id")} 已添加'))
                return
            if parsed.path == '/save-edit':
                original_task_id = (qs.get('original_task_id') or [''])[0]
                task = build_task_from_form(form)
                update_task(CONFIG_PATH, original_task_id, task)
                sync_scheduler_by_config()
                self._send_html(render_popup_success(message=f'任务 {original_task_id} 已更新为 {task.get("id")}'))
                return
            if parsed.path == '/delete':
                remove_task(CONFIG_PATH, task_id)
                sync_scheduler_by_config()
                self._redirect('/?message=' + quote(f'任务 {task_id} 已删除'))
                return
            if parsed.path == '/toggle':
                enabled = (form.get('enabled') or ['1'])[0] == '1'
                toggle_task_enabled(CONFIG_PATH, task_id, enabled)
                sync_scheduler_by_config()
                self._redirect('/?message=' + quote(f'任务 {task_id} 已{"启用" if enabled else "停用"}'))
                return

            if parsed.path in ('/upload-only', '/run'):
                if not task_id:
                    raise ValueError('task_id 不能为空')
                task = build_task_config(load_config(CONFIG_PATH), task_id)
                if parsed.path == '/upload-only':
                    upload_result = test_upload_only(task)
                    robot_text = ' -> '.join(upload_result.get('robot_ids') or [task.get('robot_id','')])
                    detail = _append_browser_target_detail('仅截图+上传', upload_result)
                    append_send_log(task_id, robot_id=robot_text, success=True, triggered_by='manual', action='upload-only', detail=detail)
                    self._redirect('/?message=' + quote(f'任务 {task_id} 已执行（仅截图+上传）'))
                    return
                run_result = run_task(task)
                robot_text = ' -> '.join(run_result.get('robot_ids') or [task.get('robot_id','')])
                detail = _append_browser_target_detail('测试并发送', run_result)
                append_send_log(task_id, robot_id=robot_text, success=True, triggered_by='manual', action='run', detail=detail)
                self._redirect('/?message=' + quote(f'任务 {task_id} 已执行并发送'))
                return
            self._send_html(render_page(error='unknown action'), status=404)
        except Exception:
            detail = traceback.format_exc()
            friendly_detail = detail
            if 'connect ECONNREFUSED 127.0.0.1:18800' in detail or 'connectOverCDP' in detail:
                friendly_detail = '截图浏览器未启动，当前无法连接 127.0.0.1:18810。请先点击页面右上角“打开浏览器”，等待阿里文档页面打开后，再重新测试发送。\n\n原始错误：\n' + detail
            action_name = (urlparse(self.path).path or '').lstrip('/') or 'unknown'
            context_lines = [f'POST {self.path}']
            if task_id:
                context_lines.append(f'task_id={task_id}')
            context_lines.append(f'action={action_name}')
            context_lines.append(friendly_detail)
            append_webui_error_log('\n'.join(context_lines))
            if task_id:
                try:
                    cfg = load_config(CONFIG_PATH)
                    task = build_task_config(cfg, task_id)
                    robot_ids = task.get('robot_ids') or ([] if not task.get('robot_id') else [task.get('robot_id')])
                    robot_id = ' -> '.join(robot_ids)
                except Exception:
                    robot_id = ''
                append_send_log(task_id, robot_id=robot_id, success=False, triggered_by='manual', action=action_name, error=friendly_detail)
            visible_detail = f'task_id={task_id}\naction={action_name}\n\n{friendly_detail}' if task_id else f'action={action_name}\n\n{friendly_detail}'
            self._safe_send_error(500, 'POST failed', visible_detail)


def main(host='127.0.0.1', port=8787):
    global WEBUI_SERVER
    single_instance_error = ensure_single_webui_instance(host=host, port=port)
    if single_instance_error:
        print(single_instance_error)
        return 1
    migrate_result = migrate_config_secrets(CONFIG_PATH)
    if migrate_result.get('migrated'):
        print('Migrated Dingtalk secrets from config.json to .env')
    if load_webui_state().get('auto_start_scheduler', True) and has_enabled_tasks():
        try:
            start_scheduler(force=False)
        except Exception:
            pass
    server = ThreadingHTTPServer((host, port), Handler)
    WEBUI_SERVER = server
    save_webui_runtime({
        'pid': os.getpid(),
        'host': host,
        'port': port,
        'started_at': now_text(),
        'command': 'app\\webui_server.py',
    })
    print(f'WebUI running at http://{host}:{port}')
    try:
        server.serve_forever()
    finally:
        runtime = load_webui_runtime()
        if runtime.get('pid') == os.getpid():
            clear_webui_runtime()
        WEBUI_SERVER = None


if __name__ == '__main__':
    raise SystemExit(main() or 0)
