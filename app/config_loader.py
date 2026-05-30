import json
from copy import deepcopy
from pathlib import Path
from datetime import datetime

from app.utils import compute_next_run
from app.secret_store import load_dingtalk_secrets, load_robot_secrets


LEGACY_IMGBED_HOME = 'https://imgbed-e0n.pages.dev/'
LEGACY_IMGBED_UPLOAD = 'https://imgbed-e0n.pages.dev/upload?serverCompress=true&uploadChannel=telegram&uploadNameType=default&autoRetry=true&uploadFolder='


def normalize_imgbed_upload_url(url):
    text = str(url or '').strip()
    if not text:
        return text
    if text.rstrip('/') == LEGACY_IMGBED_HOME.rstrip('/'):
        return LEGACY_IMGBED_UPLOAD
    return text


def apply_runtime_secrets(data):
    cfg = json.loads(json.dumps(data, ensure_ascii=False))
    defaults = cfg.setdefault('defaults', {})
    defaults.setdefault('app_settings', {})
    defaults.setdefault('browser', {})
    defaults.setdefault('message', {})
    dingtalk = defaults.setdefault('dingtalk', {})
    defaults.setdefault('robot_configs', [])
    defaults['imgbed_upload_url'] = normalize_imgbed_upload_url(defaults.get('imgbed_upload_url', ''))
    cfg.setdefault('send_logs', [])

    secrets = load_dingtalk_secrets()
    if secrets.get('webhook'):
        dingtalk['webhook'] = secrets['webhook']
    if secrets.get('secret'):
        dingtalk['secret'] = secrets['secret']

    for robot in defaults.get('robot_configs', []) or []:
        robot_id = robot.get('id')
        if not robot_id:
            continue
        robot.setdefault('enabled', True)
        robot.setdefault('name', robot_id)
        robot.setdefault('message_mode', 'markdown')
        robot.setdefault('runtime_secrets', {})
        r = load_robot_secrets(robot_id)
        robot['runtime_secrets'] = {
            'webhook': r.get('webhook', ''),
            'secret': r.get('secret', ''),
        }
    return cfg


def load_config(config_path):
    path = Path(config_path)
    with path.open('r', encoding='utf-8') as f:
        data = json.load(f)
    return apply_runtime_secrets(data)


def save_config(config_path, data):
    path = Path(config_path)
    cfg = json.loads(json.dumps(data, ensure_ascii=False))
    defaults = cfg.setdefault('defaults', {})
    defaults['imgbed_upload_url'] = normalize_imgbed_upload_url(defaults.get('imgbed_upload_url', ''))
    with path.open('w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def deep_merge(base, override):
    if not isinstance(base, dict) or not isinstance(override, dict):
        return override
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def get_robot_config(root_config, robot_id):
    defaults = root_config.get('defaults', {}) or {}
    for robot in defaults.get('robot_configs', []) or []:
        if robot.get('id') == robot_id:
            return robot
    return None


def normalize_task_robot_ids(task, default_robot_id=''):
    robot_ids = task.get('robot_ids') or []
    if isinstance(robot_ids, str):
        robot_ids = [robot_ids]
    robot_ids = [str(x).strip() for x in robot_ids if str(x).strip()]
    if robot_ids:
        return robot_ids
    robot_id = str(task.get('robot_id') or '').strip()
    if robot_id:
        return [robot_id]
    default_robot_id = str(default_robot_id or '').strip()
    return [default_robot_id] if default_robot_id else []


def apply_robot_runtime_to_task(root_config, task, robot_id):
    if not robot_id:
        return
    robot = get_robot_config(root_config, robot_id)
    if not robot:
        return
    task['robot'] = robot
    task['robot_id'] = robot_id
    dingtalk = task.setdefault('dingtalk', {})
    runtime_secrets = robot.get('runtime_secrets', {}) or {}
    if runtime_secrets.get('webhook'):
        dingtalk['webhook'] = runtime_secrets['webhook']
    if runtime_secrets.get('secret'):
        dingtalk['secret'] = runtime_secrets['secret']


def build_task_config(root_config, task_id):
    defaults = root_config.get('defaults', {})
    tasks = root_config.get('tasks', [])
    for task in tasks:
        if task.get('id') == task_id:
            merged = deep_merge(defaults, task)
            merged['_task_id'] = task_id
            if 'alidocs_url' not in task or not task.get('alidocs_url'):
                merged['alidocs_url'] = ''
            if 'sheet_name' not in task or not task.get('sheet_name'):
                merged['sheet_name'] = ''

            browser_target = merged.get('browser_target') or {}
            default_browser = defaults.get('browser', {}) or {}
            merged['browser_target'] = {
                'tab_keyword': browser_target.get('tab_keyword') or default_browser.get('tab_keyword', ''),
                'tab_url_keyword': browser_target.get('tab_url_keyword') or default_browser.get('tab_url_keyword', ''),
            }

            robot_ids = normalize_task_robot_ids(merged, defaults.get('default_robot_id'))
            merged['robot_ids'] = robot_ids
            merged['robot_id'] = robot_ids[0] if robot_ids else ''
            merged['robot_map'] = {}
            for rid in robot_ids:
                robot_task = deep_merge({}, merged)
                apply_robot_runtime_to_task(root_config, robot_task, rid)
                merged['robot_map'][rid] = {
                    'robot': deepcopy(robot_task.get('robot')),
                    'dingtalk': deepcopy(robot_task.get('dingtalk', {})),
                }
            if robot_ids:
                apply_robot_runtime_to_task(root_config, merged, robot_ids[0])

            merge_cfg = merged.get('merge', {}) or {}
            child_ids = merge_cfg.get('task_ids', []) or []
            if child_ids:
                child_tasks = []
                missing = []
                for child_id in child_ids:
                    child = next((x for x in tasks if x.get('id') == child_id), None)
                    if not child:
                        missing.append(child_id)
                        continue
                    child_merged = deep_merge(defaults, child)
                    child_merged['_task_id'] = child_id
                    if 'alidocs_url' not in child or not child.get('alidocs_url'):
                        child_merged['alidocs_url'] = ''
                    if 'sheet_name' not in child or not child.get('sheet_name'):
                        child_merged['sheet_name'] = ''
                    child_browser_target = child_merged.get('browser_target') or {}
                    default_browser = defaults.get('browser', {}) or {}
                    child_merged['browser_target'] = {
                        'tab_keyword': child_browser_target.get('tab_keyword') or default_browser.get('tab_keyword', ''),
                        'tab_url_keyword': child_browser_target.get('tab_url_keyword') or default_browser.get('tab_url_keyword', ''),
                    }
                    parent_robot_ids = merged.get('robot_ids') or normalize_task_robot_ids(merged, defaults.get('default_robot_id'))
                    child_robot_ids = normalize_task_robot_ids(child_merged, '') or parent_robot_ids
                    child_merged['robot_ids'] = child_robot_ids
                    child_merged['robot_id'] = child_robot_ids[0] if child_robot_ids else ''
                    if child_robot_ids:
                        apply_robot_runtime_to_task(root_config, child_merged, child_robot_ids[0])
                    child_tasks.append(child_merged)
                merged['merge']['child_tasks'] = child_tasks
                if missing:
                    merged['merge']['missing_task_ids'] = missing
            return merged
    raise ValueError(f'task not found: {task_id}')


def get_raw_task(root_config, task_id):
    for task in root_config.get('tasks', []):
        if task.get('id') == task_id:
            return task
    raise ValueError(f'task not found: {task_id}')


def list_tasks(root_config, enabled_only=False):
    tasks = []
    defaults = root_config.get('defaults', {}) or {}
    default_robot_id = defaults.get('default_robot_id', '')
    for task in root_config.get('tasks', []):
        if enabled_only and not task.get('enabled', True):
            continue
        next_run = compute_next_run(task.get('schedule', {}), now=datetime.now())
        task_robot_ids = normalize_task_robot_ids(task, default_robot_id)
        task_robot_id = task_robot_ids[0] if task_robot_ids else ''
        browser_target = task.get('browser_target') or {}
        default_browser = defaults.get('browser', {}) or {}
        tasks.append({
            'id': task.get('id'),
            'name': task.get('name'),
            'enabled': task.get('enabled', True),
            'schedule': task.get('schedule', {}),
            'mode': task.get('mode', 'filter_capture'),
            'sheet_name': task.get('sheet_name', ''),
            'filter': task.get('filter', {}),
            'capture': task.get('capture', {}),
            'merge': task.get('merge', {}),
            'message': task.get('message', {}),
            'browser_target': {
                'tab_keyword': browser_target.get('tab_keyword') or default_browser.get('tab_keyword', ''),
                'tab_url_keyword': browser_target.get('tab_url_keyword') or default_browser.get('tab_url_keyword', ''),
            },
            'robot_id': task_robot_id,
            'robot_ids': task_robot_ids,
            'next_run': next_run.strftime('%Y-%m-%d %H:%M:%S') if next_run else None,
        })
    return tasks


def add_task(config_path, task):
    cfg = load_config(config_path)
    tasks = cfg.setdefault('tasks', [])
    for item in tasks:
        if item.get('id') == task.get('id'):
            raise ValueError(f"task id already exists: {task.get('id')}")
    tasks.append(task)
    save_config(config_path, cfg)
    return task


def update_task(config_path, task_id, new_task):
    cfg = load_config(config_path)
    tasks = cfg.get('tasks', [])
    idx = None
    for i, item in enumerate(tasks):
        if item.get('id') == task_id:
            idx = i
            break
    if idx is None:
        raise ValueError(f'task not found: {task_id}')

    new_id = new_task.get('id')
    for i, item in enumerate(tasks):
        if i != idx and item.get('id') == new_id:
            raise ValueError(f'task id already exists: {new_id}')

    tasks[idx] = new_task
    save_config(config_path, cfg)
    return new_task


def remove_task(config_path, task_id):
    cfg = load_config(config_path)
    tasks = cfg.get('tasks', [])
    new_tasks = [t for t in tasks if t.get('id') != task_id]
    if len(new_tasks) == len(tasks):
        raise ValueError(f'task not found: {task_id}')
    cfg['tasks'] = new_tasks
    save_config(config_path, cfg)


def toggle_task_enabled(config_path, task_id, enabled):
    cfg = load_config(config_path)
    for task in cfg.get('tasks', []):
        if task.get('id') == task_id:
            task['enabled'] = bool(enabled)
            save_config(config_path, cfg)
            return
    raise ValueError(f'task not found: {task_id}')
