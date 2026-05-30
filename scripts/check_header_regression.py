import argparse
import json
import re
import sys
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.alidocs_capture import capture_from_alidocs
from app.config_loader import build_task_config, load_config

DEFAULT_TASK_IDS = [
    '富士康-晋城富士康',
    '富士康-龙华富士康',
    '比亚迪-南宁青秀',
    '赢合-湖北益佳通',
]
NOISE_TOKENS = ['root', 'span', 'data-type', '[object Object]']


def _clean_html_text(text: str) -> str:
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.I)
    text = re.sub(r'</p\s*>', '\n', text, flags=re.I)
    text = re.sub(r'<.*?>', '', text, flags=re.S)
    return unescape(text).strip()


def _extract_header_rows(html: str) -> list[list[str]]:
    rows = re.findall(r'<thead.*?>(.*?)</thead>', html, flags=re.S | re.I)
    if not rows:
        return []
    thead = rows[0]
    tr_blocks = re.findall(r'<tr.*?>(.*?)</tr>', thead, flags=re.S | re.I)
    header_rows = []
    for tr in tr_blocks:
        cells = re.findall(r'<th.*?>(.*?)</th>', tr, flags=re.S | re.I)
        header_rows.append([_clean_html_text(cell) for cell in cells])
    return header_rows


def check_task(config_path: Path, task_id: str) -> dict:
    cfg = load_config(config_path)
    task = build_task_config(cfg, task_id)
    result = capture_from_alidocs(task)

    html_path = Path(result.get('htmlPath') or '')
    image_path = Path(result.get('imagePath') or '')
    result_rows = int(result.get('resultRows') or 0)
    empty_result = result_rows == 0

    header_rows = []
    blank_header_count = 0
    has_asl_noise = False
    headers = []

    if html_path.exists():
        html = html_path.read_text(encoding='utf-8', errors='ignore')
        header_rows = _extract_header_rows(html)
        if len(header_rows) >= 2:
            headers = header_rows[1]
        elif header_rows:
            headers = header_rows[-1]
        blank_header_count = sum(1 for x in headers if not x)
        joined = '\n'.join(headers)
        has_asl_noise = any(token in joined for token in NOISE_TOKENS)

    ok = empty_result or (html_path.exists() and image_path.exists() and blank_header_count == 0 and not has_asl_noise)
    return {
        'task_id': task_id,
        'ok': ok,
        'htmlPath': str(html_path) if html_path else '',
        'imagePath': str(image_path) if image_path else '',
        'resultRows': result_rows,
        'emptyResult': empty_result,
        'blank_header_count': blank_header_count,
        'has_asl_noise': has_asl_noise,
        'tail_headers': headers[-8:] if headers else [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='Run header regression checks for AliDocs capture tasks.')
    parser.add_argument('--config', default=str(ROOT / 'config' / 'config.json'))
    parser.add_argument('--task', action='append', dest='tasks', help='Task ID to check; can be used multiple times')
    parser.add_argument('--output', default=str(ROOT / 'output' / 'header_regression_check.json'))
    args = parser.parse_args()

    config_path = Path(args.config)
    task_ids = args.tasks or DEFAULT_TASK_IDS

    results = [check_task(config_path, task_id) for task_id in task_ids]
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    print(output_path)
    print(json.dumps(results, ensure_ascii=False, indent=2))

    return 0 if all(item.get('ok') for item in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
