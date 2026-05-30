from copy import deepcopy
from datetime import datetime

from .alidocs_capture import capture_from_alidocs
from .dingtalk_sender import DingtalkSender


def _append_date_suffix(text):
    base = (text or '').strip()
    date_text = datetime.now().strftime('%y/%m/%d')
    if base:
        return f'{base}（{date_text}）'
    return f'（{date_text}）'


def _build_merged_header_text(text):
    return f'**{text}**' if text else ''


def _build_subtask_heading_text(text):
    return f'> {text}' if text else ''


def _get_task_robot_ids(task):
    robot_ids = task.get('robot_ids') or []
    if isinstance(robot_ids, str):
        robot_ids = [robot_ids]
    robot_ids = [str(x).strip() for x in robot_ids if str(x).strip()]
    if robot_ids:
        return robot_ids
    robot_id = str(task.get('robot_id') or '').strip()
    return [robot_id] if robot_id else []


def _build_sender(task):
    dingtalk = task.get('dingtalk', {}) or {}
    webhook = (dingtalk.get('webhook') or '').strip()
    secret = (dingtalk.get('secret') or '').strip()
    if not webhook:
        robot_id = task.get('robot_id') or 'default'
        raise ValueError(f'未配置钉钉 Webhook，请先为机器人 {robot_id} 保存配置')
    return DingtalkSender(
        webhook=webhook,
        secret=secret,
        imgbed_upload_url=task['imgbed_upload_url'],
    )


def _clone_task_for_robot(task, robot_id):
    cloned = deepcopy(task)
    cloned['robot_id'] = robot_id
    cloned['robot_ids'] = [robot_id]
    robot_map = cloned.get('robot_map', {}) or {}
    robot_task = robot_map.get(robot_id)
    if robot_task:
        cloned['robot'] = robot_task.get('robot')
        cloned['dingtalk'] = deepcopy(robot_task.get('dingtalk') or {})
    return cloned


def _build_empty_filter_text(task):
    task_name = str(task.get('name') or task.get('_task_id') or task.get('id') or '').strip()
    filter_cfg = task.get('filter', {}) or {}
    filter_col = str(filter_cfg.get('column_name') or '').strip()
    filter_equals = str(filter_cfg.get('equals') or '').strip()
    filter_text = f'{filter_col}={filter_equals}' if filter_col else filter_equals

    lines = []
    if task_name:
        lines.append(task_name)
    if filter_text:
        lines.append(f'{filter_text} 结果：无')
    elif not lines:
        lines.append('筛选结果为空')
    else:
        lines.append('结果：无')
    return '\n\n'.join(lines)


def _capture_single_task(task):
    capture_result = capture_from_alidocs(task)
    if capture_result.get('emptyResult'):
        return {
            'task_id': task.get('_task_id') or task.get('id'),
            'task_name': task.get('name', ''),
            'capture': capture_result,
            'upload_url': '',
            'message_text': _build_empty_filter_text(task),
            'robot_id': task.get('robot_id') or '',
        }
    sender = _build_sender(task)
    upload_url = sender.upload_local_image(capture_result['imagePath'])
    return {
        'task_id': task.get('_task_id') or task.get('id'),
        'task_name': task.get('name', ''),
        'capture': capture_result,
        'upload_url': upload_url,
        'robot_id': task.get('robot_id') or '',
    }


def _prepare_task_payload(task):
    message_cfg = task.get('message', {}) or {}
    merge_cfg = task.get('merge', {}) or {}
    child_tasks = merge_cfg.get('child_tasks', []) or []
    missing_task_ids = merge_cfg.get('missing_task_ids', []) or []
    title = message_cfg.get('title', '截图通知')

    if child_tasks:
        if missing_task_ids:
            raise ValueError(f'合并任务缺少子任务: {", ".join(missing_task_ids)}')
        merged_items = [_capture_single_task(child_task) for child_task in child_tasks]
        final_text = _build_merged_markdown_text(task, merged_items)
        return {
            'merge_mode': True,
            'send_mode': 'markdown',
            'title': title,
            'message_text': final_text,
            'items': merged_items,
            'merged_count': len(merged_items),
        }

    capture_result = capture_from_alidocs(task)
    mode = message_cfg.get('mode', 'markdown')
    if capture_result.get('emptyResult'):
        return {
            'merge_mode': False,
            'send_mode': 'markdown',
            'title': title,
            'message_text': _build_empty_filter_text(task),
            'capture': capture_result,
            'upload_url': '',
            'empty_result': True,
        }

    text = message_cfg.get('text', task.get('name', '截图任务完成'))
    text = _append_date_suffix(text)
    sender = _build_sender(task)
    upload_url = sender.upload_local_image(capture_result['imagePath'])

    return {
        'merge_mode': False,
        'send_mode': 'image' if mode == 'image' else 'markdown',
        'title': title,
        'message_text': text,
        'capture': capture_result,
        'upload_url': upload_url,
    }


def _send_prepared_payload(task, prepared):
    sender = _build_sender(task)
    send_mode = prepared.get('send_mode') or 'markdown'
    if send_mode == 'image':
        send_result = sender.send_image(prepared['upload_url'])
        return {
            'capture': prepared.get('capture'),
            'upload_url': prepared.get('upload_url'),
            'send_result': send_result,
            'message_text': prepared.get('message_text', ''),
            'robot_id': task.get('robot_id') or '',
        }

    final_text = prepared.get('message_text', '')
    if prepared.get('merge_mode'):
        send_result = sender.send_markdown(title=prepared.get('title', '截图通知'), text=final_text)
        return {
            'merge_mode': True,
            'task_id': task.get('_task_id') or task.get('id'),
            'robot_id': task.get('robot_id') or '',
            'merged_count': prepared.get('merged_count', 0),
            'items': deepcopy(prepared.get('items') or []),
            'send_result': send_result,
            'message_text': final_text,
        }

    final_text = f"{final_text}\n\n![image]({prepared['upload_url']})" if prepared.get('upload_url') else final_text
    send_result = sender.send_markdown(title=prepared.get('title', '截图通知'), text=final_text)
    return {
        'capture': prepared.get('capture'),
        'upload_url': prepared.get('upload_url'),
        'send_result': send_result,
        'message_text': prepared.get('message_text', ''),
        'robot_id': task.get('robot_id') or '',
    }


def _build_merged_markdown_text(task, merged_items):
    message_cfg = task.get('message', {}) or {}
    merge_cfg = task.get('merge', {}) or {}
    text = (message_cfg.get('text') or '').strip()
    text = _append_date_suffix(text)
    include_subtitles = merge_cfg.get('include_subtitles', True)

    parts = []
    header = _build_merged_header_text(text)
    if header:
        parts.append(header)

    item_blocks = []
    for item in merged_items:
        item_name = item.get('task_name') or item.get('task_id')
        upload_url = item.get('upload_url')
        text_only = item.get('message_text', '')
        if upload_url:
            image_line = f"![{item_name}]({upload_url})"
            if include_subtitles:
                item_blocks.append(f"{_build_subtask_heading_text(item_name)}\n{image_line}")
            else:
                item_blocks.append(image_line)
        elif text_only:
            normalized_text = text_only.lstrip()
            already_has_name = bool(item_name) and (
                normalized_text == item_name
                or normalized_text.startswith(f"{item_name}\n")
                or normalized_text.startswith(f"{item_name}\r\n")
            )
            if include_subtitles and not already_has_name:
                item_blocks.append(f"{_build_subtask_heading_text(item_name)}\n{text_only}")
            else:
                item_blocks.append(text_only)

    if item_blocks:
        parts.append('\n\n'.join(item_blocks))

    return '\n\n'.join(x for x in parts if x)


def run_task(task):
    robot_ids = _get_task_robot_ids(task)
    base_task = task if not robot_ids else _clone_task_for_robot(task, robot_ids[0])
    prepared = _prepare_task_payload(base_task)

    if not robot_ids:
        return _send_prepared_payload(task, prepared)
    if len(robot_ids) == 1:
        return _send_prepared_payload(base_task, prepared)

    results = []
    for robot_id in robot_ids:
        robot_task = _clone_task_for_robot(task, robot_id)
        results.append(_send_prepared_payload(robot_task, prepared))
    return {
        'task_id': task.get('_task_id') or task.get('id'),
        'robot_id': robot_ids[0],
        'robot_ids': robot_ids,
        'sequential_send': True,
        'send_count': len(results),
        'prepared_once': True,
        'results': results,
    }


def test_upload_only(task):
    merge_cfg = task.get('merge', {}) or {}
    child_tasks = merge_cfg.get('child_tasks', []) or []
    missing_task_ids = merge_cfg.get('missing_task_ids', []) or []
    if child_tasks:
        if missing_task_ids:
            raise ValueError(f'合并任务缺少子任务: {", ".join(missing_task_ids)}')
        return {
            'merge_mode': True,
            'task_id': task.get('_task_id') or task.get('id'),
            'items': [_capture_single_task(child_task) for child_task in child_tasks],
            'robot_id': task.get('robot_id') or '',
            'robot_ids': _get_task_robot_ids(task),
        }

    capture_result = capture_from_alidocs(task)
    if capture_result.get('emptyResult'):
        return {
            'capture': capture_result,
            'upload_url': '',
            'message_text': _build_empty_filter_text(task),
            'robot_id': task.get('robot_id') or '',
            'robot_ids': _get_task_robot_ids(task),
        }
    sender = _build_sender(task)
    upload_url = sender.upload_local_image(capture_result['imagePath'])
    return {
        'capture': capture_result,
        'upload_url': upload_url,
        'robot_id': task.get('robot_id') or '',
        'robot_ids': _get_task_robot_ids(task),
    }
