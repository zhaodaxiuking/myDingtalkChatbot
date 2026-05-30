import argparse
import json
import os
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
import sys
import schedule

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config_loader import load_config, build_task_config, list_tasks
from app.task_runner import run_task

LOCK_PATH = ROOT / 'output' / 'scheduler.lock'
STATUS_PATH = ROOT / 'output' / 'scheduler_status.json'
TASK_QUEUE = queue.Queue()
ENQUEUED = set()
ENQUEUED_ORDER = []
ENQUEUED_LOCK = threading.Lock()
STATUS_LOCK = threading.Lock()
STATUS = {
    'scheduler_running': False,
    'pid': None,
    'current_task': None,
    'queue': [],
    'last_events': [],
    'updated_at': None,
}
REGISTERED_JOBS = {}  # 记录已注册的任务ID，用于检测配置变化
REGISTERED_JOBS_LOCK = threading.Lock()


def now_text():
    return f'[{datetime.now():%Y-%m-%d %H:%M:%S}]'


def now_iso():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def save_status():
    with STATUS_LOCK:
        STATUS['updated_at'] = now_iso()
        STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATUS_PATH.write_text(json.dumps(STATUS, ensure_ascii=False, indent=2), encoding='utf-8')


def push_event(kind, task_id=None, detail=None):
    with STATUS_LOCK:
        STATUS['last_events'].insert(0, {
            'time': now_iso(),
            'kind': kind,
            'task_id': task_id,
            'detail': detail,
        })
        STATUS['last_events'] = STATUS['last_events'][:30]
    save_status()


def refresh_queue_snapshot():
    with ENQUEUED_LOCK:
        snapshot = list(ENQUEUED_ORDER)
    with STATUS_LOCK:
        STATUS['queue'] = snapshot
    save_status()


def acquire_lock(lock_path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
        os.write(fd, str(os.getpid()).encode('utf-8'))
        return fd
    except FileExistsError:
        raise RuntimeError(f'scheduler is already running, lock exists: {lock_path}')


def release_lock(fd, lock_path):
    try:
        os.close(fd)
    except Exception:
        pass
    try:
        Path(lock_path).unlink(missing_ok=True)
    except Exception:
        pass


def run_task_safely(config_path, task_id):
    print(f'{now_text()} running task: {task_id}')
    with STATUS_LOCK:
        STATUS['current_task'] = task_id
    save_status()
    push_event('started', task_id=task_id)

    cfg = load_config(config_path)
    task = build_task_config(cfg, task_id)
    result = run_task(task)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    push_event('finished', task_id=task_id, detail='success')


def enqueue_task(task_id):
    with ENQUEUED_LOCK:
        if task_id in ENQUEUED:
            print(f'{now_text()} skip enqueue duplicate task: {task_id}')
            push_event('skip_duplicate', task_id=task_id)
            return
        ENQUEUED.add(task_id)
        ENQUEUED_ORDER.append(task_id)
    TASK_QUEUE.put(task_id)
    print(f'{now_text()} queued task: {task_id} | queue_size={TASK_QUEUE.qsize()}')
    refresh_queue_snapshot()
    push_event('queued', task_id=task_id, detail=f'queue_size={TASK_QUEUE.qsize()}')


def scheduled_enqueue(config_path, task_id):
    _ = config_path
    enqueue_task(task_id)


def worker_loop(config_path):
    while True:
        task_id = TASK_QUEUE.get()
        try:
            with ENQUEUED_LOCK:
                if task_id in ENQUEUED_ORDER:
                    ENQUEUED_ORDER.remove(task_id)
            refresh_queue_snapshot()
            run_task_safely(config_path, task_id)
        except Exception as e:
            print(f'{now_text()} task failed: {task_id} | {e}')
            push_event('failed', task_id=task_id, detail=str(e))
        finally:
            with ENQUEUED_LOCK:
                ENQUEUED.discard(task_id)
            with STATUS_LOCK:
                STATUS['current_task'] = None
            TASK_QUEUE.task_done()
            refresh_queue_snapshot()
            save_status()
            print(f'{now_text()} finished task: {task_id} | remaining={TASK_QUEUE.qsize()}')


def normalize_time_text(value):
    """Normalize schedule time to HH:MM or HH:MM:SS for python-schedule."""
    text = str(value or '').strip()
    parts = text.split(':')
    if len(parts) not in (2, 3):
        return text
    try:
        hour = int(parts[0])
        minute = int(parts[1])
        second = int(parts[2]) if len(parts) == 3 else None
    except ValueError:
        return text
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and (second is None or 0 <= second <= 59)):
        return text
    if second is None:
        return f'{hour:02d}:{minute:02d}'
    return f'{hour:02d}:{minute:02d}:{second:02d}'


def register_jobs(config_path):
    global REGISTERED_JOBS
    cfg = load_config(config_path)
    weekday_map = {
        'monday': schedule.every().monday,
        'tuesday': schedule.every().tuesday,
        'wednesday': schedule.every().wednesday,
        'thursday': schedule.every().thursday,
        'friday': schedule.every().friday,
        'saturday': schedule.every().saturday,
        'sunday': schedule.every().sunday,
    }
    cn_map = {
        '周一': 'monday',
        '周二': 'tuesday',
        '周三': 'wednesday',
        '周四': 'thursday',
        '周五': 'friday',
        '周六': 'saturday',
        '周日': 'sunday',
    }

    for item in list_tasks(cfg, enabled_only=True):
        schedule_cfg = item.get('schedule', {}) or {}
        schedule_type = (schedule_cfg.get('type') or '').lower()
        time_text = normalize_time_text(schedule_cfg.get('time'))
        if not time_text:
            continue
        if schedule_type == 'daily':
            schedule.every().day.at(time_text).do(scheduled_enqueue, config_path, item['id'])
            print(f"registered daily task {item['id']} at {time_text}")
            REGISTERED_JOBS[item['id']] = time_text
        elif schedule_type == 'weekly':
            weekday = str(schedule_cfg.get('weekday', 'monday')).strip().lower()
            weekday = cn_map.get(weekday, weekday)
            job = weekday_map.get(weekday)
            if not job:
                print(f"skip task {item['id']}: invalid weekday {schedule_cfg.get('weekday')}")
                continue
            job.at(time_text).do(scheduled_enqueue, config_path, item['id'])
            print(f"registered weekly task {item['id']} on {weekday} at {time_text}")
            REGISTERED_JOBS[item['id']] = f"{weekday} {time_text}"


def init_status():
    with STATUS_LOCK:
        STATUS['scheduler_running'] = True
        STATUS['pid'] = os.getpid()
        STATUS['current_task'] = None
        STATUS['queue'] = []
        STATUS['last_events'] = []
    save_status()
    push_event('scheduler_started', detail=f'pid={os.getpid()}')


def stop_status():
    push_event('scheduler_stopped')
    with STATUS_LOCK:
        STATUS['scheduler_running'] = False
        STATUS['current_task'] = None
        STATUS['queue'] = []
    save_status()


def reload_jobs(config_path):
    """重新加载任务配置，检测配置变化并更新调度"""
    global REGISTERED_JOBS
    
    # 取消所有已注册的任务
    schedule.clear()
    
    # 重新注册任务
    cfg = load_config(config_path)
    weekday_map = {
        'monday': schedule.every().monday,
        'tuesday': schedule.every().tuesday,
        'wednesday': schedule.every().wednesday,
        'thursday': schedule.every().thursday,
        'friday': schedule.every().friday,
        'saturday': schedule.every().saturday,
        'sunday': schedule.every().sunday,
    }
    cn_map = {
        '周一': 'monday',
        '周二': 'tuesday',
        '周三': 'wednesday',
        '周四': 'thursday',
        '周五': 'friday',
        '周六': 'saturday',
        '周日': 'sunday',
    }
    
    new_jobs = {}
    for item in list_tasks(cfg, enabled_only=True):
        schedule_cfg = item.get('schedule', {}) or {}
        schedule_type = (schedule_cfg.get('type') or '').lower()
        time_text = normalize_time_text(schedule_cfg.get('time'))
        if not time_text:
            continue
        if schedule_type == 'daily':
            schedule.every().day.at(time_text).do(scheduled_enqueue, config_path, item['id'])
            print(f"{now_text()} reloaded daily task {item['id']} at {time_text}")
            new_jobs[item['id']] = time_text
        elif schedule_type == 'weekly':
            weekday = str(schedule_cfg.get('weekday', 'monday')).strip().lower()
            weekday = cn_map.get(weekday, weekday)
            job = weekday_map.get(weekday)
            if not job:
                continue
            job.at(time_text).do(scheduled_enqueue, config_path, item['id'])
            print(f"{now_text()} reloaded weekly task {item['id']} on {weekday} at {time_text}")
            new_jobs[item['id']] = f"{weekday} {time_text}"
    
    with REGISTERED_JOBS_LOCK:
        REGISTERED_JOBS = new_jobs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default=str(ROOT / 'config' / 'config.json'))
    args = parser.parse_args()

    lock_fd = acquire_lock(LOCK_PATH)
    print(f'{now_text()} scheduler lock acquired: {LOCK_PATH}')
    init_status()

    worker = threading.Thread(target=worker_loop, args=(args.config,), daemon=True)
    worker.start()

    try:
        register_jobs(args.config)
        last_reload = time.time()
        reload_interval = 60  # 每60秒检查一次配置变化
        
        while True:
            schedule.run_pending()
            time.sleep(1)
            
            # 每60秒重新加载配置
            if time.time() - last_reload > reload_interval:
                last_reload = time.time()
                try:
                    reload_jobs(args.config)
                except Exception as e:
                    print(f'{now_text()} reload failed: {e}')
    finally:
        stop_status()
        release_lock(lock_fd, LOCK_PATH)
        print(f'{now_text()} scheduler lock released: {LOCK_PATH}')


if __name__ == '__main__':
    main()
