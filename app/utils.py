from datetime import datetime, timedelta
from pathlib import Path

WEEKDAY_MAP = {
    'monday': 0,
    'tuesday': 1,
    'wednesday': 2,
    'thursday': 3,
    'friday': 4,
    'saturday': 5,
    'sunday': 6,
    '周一': 0,
    '周二': 1,
    '周三': 2,
    '周四': 3,
    '周五': 4,
    '周六': 5,
    '周日': 6,
}


def ensure_dir(path_str):
    path = Path(path_str)
    path.mkdir(parents=True, exist_ok=True)
    return path


def ts_now():
    return datetime.now().strftime('%Y%m%d_%H%M%S')


def col_to_index(col_name):
    s = col_name.strip().upper()
    n = 0
    for ch in s:
        if not ('A' <= ch <= 'Z'):
            raise ValueError(f'invalid column: {col_name}')
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def parse_a1_cell(cell_text):
    s = cell_text.strip().upper()
    letters = ''.join(ch for ch in s if 'A' <= ch <= 'Z')
    digits = ''.join(ch for ch in s if ch.isdigit())
    if not letters:
        raise ValueError(f'invalid cell: {cell_text}')
    col = col_to_index(letters)
    row = int(digits) - 1 if digits else None
    return row, col


def parse_cell_range(cell_range):
    text = cell_range.strip().upper()
    if ':' not in text:
        raise ValueError('cell_range must look like M:AE or A1:F7')
    start, end = text.split(':', 1)
    start_row, start_col = parse_a1_cell(start)
    end_row, end_col = parse_a1_cell(end)
    return {
        'raw': text,
        'start_row': start_row,
        'end_row': end_row,
        'start_col': start_col,
        'end_col': end_col,
        'has_rows': start_row is not None and end_row is not None,
    }


def excel_col_name(index_zero_based):
    n = index_zero_based + 1
    s = ''
    while n > 0:
        m = (n - 1) % 26
        s = chr(65 + m) + s
        n = (n - 1) // 26
    return s


def parse_time_hhmm(value):
    hh, mm = value.strip().split(':', 1)
    return int(hh), int(mm)


def compute_next_run(schedule_cfg, now=None):
    now = now or datetime.now()
    if not schedule_cfg:
        return None
    schedule_type = (schedule_cfg.get('type') or '').lower()
    time_text = schedule_cfg.get('time')
    if not time_text:
        return None
    hour, minute = parse_time_hhmm(time_text)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if schedule_type == 'daily':
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    if schedule_type == 'weekly':
        weekday_value = schedule_cfg.get('weekday', 'monday')
        target_weekday = WEEKDAY_MAP.get(str(weekday_value).strip().lower(), 0)
        days_ahead = target_weekday - now.weekday()
        if days_ahead < 0:
            days_ahead += 7
        candidate = candidate + timedelta(days=days_ahead)
        if candidate <= now:
            candidate += timedelta(days=7)
        return candidate

    return None
