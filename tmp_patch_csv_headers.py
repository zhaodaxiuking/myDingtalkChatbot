from pathlib import Path

path = Path(r"C:\Users\Administrator\Documents\code\opencode\DingtalkChatbot\DingtalkChatbot-1.5.7\app\webui_server.py")
s = path.read_text(encoding='utf-8')

start = s.index("CSV_TASK_COLUMNS = [")
end = s.index("def _truthy_text(value):")
new_block = '''CSV_TASK_COLUMNS = [
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


'''
s = s[:start] + new_block + s[end:]

s = s.replace(
    """    reader = csv.DictReader(io.StringIO(raw))\n    if not reader.fieldnames:\n        raise ValueError('CSV 缺少表头')\n    cfg = read_raw_config()\n""",
    """    reader = csv.DictReader(io.StringIO(raw))\n    if not reader.fieldnames:\n        raise ValueError('CSV 缺少表头')\n    reader.fieldnames = [CSV_TASK_IMPORT_HEADER_MAP.get(str(x or '').strip(), str(x or '').strip()) for x in reader.fieldnames]\n    cfg = read_raw_config()\n"""
)

s = s.replace(
    """    for row in reader:\n        row_index += 1\n        if not any(str(v or '').strip() for v in row.values()):\n            continue\n""",
    """    for row in reader:\n        row_index += 1\n        row = _normalize_csv_row_keys(row)\n        if not any(str(v or '').strip() for v in row.values()):\n            continue\n"""
)

path.write_text(s, encoding='utf-8')
print('patched')
