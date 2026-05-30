import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config_loader import load_config, build_task_config, list_tasks
from app.task_runner import run_task, test_upload_only


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default=str(ROOT / 'config' / 'config.json'))
    parser.add_argument('--task')
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--upload-only', action='store_true')
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.list:
        print(json.dumps(list_tasks(cfg, enabled_only=False), ensure_ascii=False, indent=2))
        return
    if not args.task:
        raise SystemExit('--task is required unless --list is used')
    task = build_task_config(cfg, args.task)
    if args.upload_only:
        result = test_upload_only(task)
    else:
        result = run_task(task)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
