"""任务增改与状态流转：先预览，确认后单次写入并回查；不自动重试写请求。"""

import datetime as dt
import html
from pathlib import Path
import re

from common import (actions_dir, SkillError, digest, missing_error, move_to_trash,
                    private_write, read_json, state_dir)

FIELDS = ('name', 'type', 'assignedTo', 'estStarted', 'deadline', 'pri', 'estimate', 'module', 'story', 'desc')
TYPES = {'design', 'devel', 'request', 'test', 'study', 'discuss', 'ui', 'affair', 'misc'}
LABELS = {'create': '新建任务', 'update': '修改任务', 'finish': '完成任务', 'close': '关闭任务'}
# 修改任务不应改变的稳定字段；请求本身包含的字段仍按用户期望值核对。
UPDATE_INVARIANTS = ('project', 'execution', 'status', 'assignedTo', 'estimate', 'left', 'consumed')


def detail(client, task, version='v1'):
    """读取任务详情；修改任务时使用与写接口一致的 V2 字段结构。"""
    from zentao import ident
    task = ident(task)
    try:
        value = client.call('GET', f'{version}/tasks/{task}')
    except SkillError as exc:
        raise (missing_error(exc, '任务', task) or exc) from None
    value = value.get('task', value) if isinstance(value, dict) else {}
    if not isinstance(value, dict) or value.get('id') is None or ident(value['id']) != task:
        raise SkillError('任务详情格式不匹配，已停止。')
    return value


def field(value, key):
    """将接口中的账号和关联对象转换为写入接口使用的值。"""
    result = value.get(key)
    if isinstance(result, dict):
        result = result.get('account' if key == 'assignedTo' else 'id')
    if isinstance(result, str):
        result = html.unescape(result)
    return result


def validate(body, action):
    from zentao import hours, ident
    allowed = set(FIELDS) | {'executionID'} if action == 'create' else set(FIELDS) if action == 'update' else {'currentConsumed', 'assignedTo', 'realStarted', 'finishedDate'} if action == 'finish' else {'comment'}
    if set(body) - allowed:
        raise SkillError('存在该操作不支持的字段，请按任务操作说明修正。')
    if action in ('create', 'update'):
        required = ('name', 'type', 'assignedTo', 'pri', 'estimate')
        if any(key not in body or body[key] is None for key in required):
            raise SkillError('缺少任务名称、类型、指派账号、优先级或预计工时，请补齐预览。')
        if not isinstance(body['name'], str) or not body['name'].strip() or body['type'] not in TYPES:
            raise SkillError('任务名称不能为空，任务类型必须为已支持类型。')
        if isinstance(body['pri'], bool) or body['pri'] not in (1, 2, 3, 4):
            raise SkillError('任务优先级必须为 1 至 4。')
        body['estimate'] = float(hours(body['estimate']))
        if action == 'create':
            body['executionID'] = ident(body.get('executionID'))
    if 'assignedTo' in body and (not isinstance(body['assignedTo'], str) or not body['assignedTo'].strip()):
        raise SkillError('指派人须为登录账号名，不能用姓名代替。')
    if action == 'finish':
        if any(not body.get(k) for k in ('assignedTo', 'realStarted', 'finishedDate')) or 'currentConsumed' not in body:
            raise SkillError('完成任务需要本次消耗、指派账号、实际开始和完成日期。')
        body['currentConsumed'] = float(hours(body['currentConsumed']))
    for key in ('estStarted', 'deadline', 'realStarted', 'finishedDate'):
        if body.get(key):
            dt.date.fromisoformat(body[key])
    start, end = ('realStarted', 'finishedDate') if action == 'finish' else ('estStarted', 'deadline')
    if body.get(start) and body.get(end) and body[start] > body[end]:
        raise SkillError('开始日期不能晚于结束日期。')


def prepare(plan, client, source_path=None):
    """保存绑定账号、目标和当前远端状态的操作预览，不写入禅道。"""
    from zentao import ident
    action = plan.get('action')
    if action not in LABELS or not isinstance(plan.get('fields'), dict):
        raise SkillError('需指定 create/update/finish/close 以及 fields 对象。')
    requested = dict(plan['fields'])
    before, task, execution = None, None, None
    if action == 'create':
        project = ident(plan.get('project'))
        execution_id = ident(requested.get('executionID'))
        rows = client.listing(f'v2/projects/{project}/executions', 'executions', 'all')
        execution = next((r for r in rows if ident(r['id']) == execution_id), None)
        if execution is None:
            raise SkillError('所选执行不属于指定项目，已停止。')
        body = requested.copy()
    else:
        task = ident(plan.get('task'))
        before = detail(client, task)
        if action == 'update':
            if not requested or set(requested) - set(FIELDS):
                raise SkillError('修改任务必须指定支持的字段。')
            # PUT 按用户示例发送完整字段，未要求修改的值从实时详情保留。
            update_before = detail(client, task, 'v2')
            body = {key: field(update_before, key) for key in FIELDS}
            # 禅道会以 null 返回未设置的可选日期，写接口使用空字符串保留其空值。
            for key in ('estStarted', 'deadline'):
                if body[key] is None:
                    body[key] = ''
            body.update(requested)
            missing = [key for key, value in body.items() if value is None]
            if missing:
                raise SkillError(f'任务详情缺少修改接口需要的字段：{", ".join(missing)}；不能用空值覆盖，请核对接口。')
        else:
            body = requested.copy()
        if action == 'finish' and before.get('status') not in ('wait', 'doing', 'pause'):
            raise SkillError('当前任务状态不适合完成操作，请核对，不能重复登记完成工时。')
        if action == 'close' and before.get('status') != 'done':
            raise SkillError('请先完成任务再关闭；当前仅支持关闭已完成任务。')
    validate(body, action)
    if action == 'finish' and before.get('consumed') is None:
        raise SkillError('任务详情缺少累计消耗，无法核对完成操作。')
    snapshot = {'action': action, 'account': client.account, 'base_url': client.base,
                'task': task, 'body': body, 'before': before, 'requested': requested,
                'execution': {'id': execution['id'], 'name': execution.get('name', ''), 'project': project} if execution else None}
    if source_path:
        snapshot.update(source_path=str(Path(source_path).resolve()), source_digest=digest(plan))
    review = digest(snapshot)
    private_write(actions_dir() / f'{review}.json', snapshot)
    changes = {key: {'before': field(before, key) if before else None, 'after': body[key]} for key in requested}
    return {'status': 'preview_only', 'review': review, 'action': LABELS[action], 'task': task,
            'execution': snapshot['execution'], 'changes': changes,
            'message': '请核对以上任务操作，确认无误后回复“确认执行”；需要调整可直接告诉我。'}


def matches(actual, expected, key):
    value = field(actual, key)
    if key in ('estStarted', 'deadline', 'realStarted', 'finishedDate'):
        return str(value or '')[:10] == str(expected or '')[:10] or not expected and str(value).startswith('0000-00-00')
    if key in ('project', 'execution', 'estimate', 'left', 'consumed', 'pri', 'module', 'story'):
        try:
            return float(value or 0) == float(expected or 0)
        except (TypeError, ValueError):
            return False
    return value == expected


def drifted_fields(before, actual, requested):
    """返回修改任务后发生意外变化的稳定字段。"""
    return [key for key in UPDATE_INVARIANTS
            if key not in requested and key in before
            and not matches(actual, field(before, key), key)]


def execute(review, confirmation, client):
    from zentao import hours, ident, submission_lock
    if not re.fullmatch(r'[a-f0-9]{64}', review) or confirmation != review:
        raise SkillError('缺少与任务预览一致的确认。')
    path = actions_dir() / f'{review}.json'
    receipt_path = path.with_suffix('.receipt.json')
    with submission_lock():
        if not path.exists():
            raise SkillError('任务预览不存在或已执行，禁止重复提交。')
        snapshot = read_json(path)
        if digest(snapshot) != review or snapshot['account'] != client.account or snapshot['base_url'] != client.base:
            raise SkillError('任务预览、账号或站点发生变化，请重新预览。')
        if receipt_path.exists():
            raise SkillError('该操作已尝试执行，请先只读查询禅道核对结果，禁止自动重发。')
        if snapshot.get('source_path') and digest(read_json(snapshot['source_path'])) != snapshot['source_digest']:
            raise SkillError('任务草稿已修改，请重新预览确认。')
        action, task, body = snapshot['action'], snapshot['task'], snapshot['body']
        if task and digest(detail(client, task)) != digest(snapshot['before']):
            raise SkillError('任务自预览后发生变化，请重新生成预览。')
        endpoint = 'v2/tasks' if action == 'create' else f'v2/tasks/{task}' + (f'/{action}' if action in ('finish', 'close') else '')
        private_write(receipt_path, {'status': 'inflight', 'action': action, 'task': task})
        try:
            response = client.call('POST' if action == 'create' else 'PUT', endpoint, body)
            # 业务成功不能仅凭 HTTP 200 判断，创建必须取得返回的新编号。
            if action == 'create':
                value = response.get('task', response.get('data', response)) if isinstance(response, dict) else {}
                task = ident(value.get('id')) if isinstance(value, dict) else ident(None)
            private_write(receipt_path, {'status': 'inflight', 'action': action, 'task': task})
            actual = detail(client, task)
            if action in ('create', 'update'):
                failed = [key for key, value in body.items()
                          if key != 'executionID' and not matches(actual, value, key)]
                if failed:
                    raise SkillError('回查发现以下完整提交字段不一致：' + '、'.join(failed) + '。')
                valid = True
                if action == 'create':
                    valid = valid and str(field(actual, 'execution')) == str(body['executionID'])
                else:
                    drifted = drifted_fields(snapshot['before'], actual, snapshot['requested'])
                    if drifted:
                        raise SkillError('修改任务后以下未请求字段被改变：' + '、'.join(drifted) + '。')
            elif action == 'finish':
                valid = actual.get('status') == 'done' and all(matches(actual, body[k], k) for k in ('assignedTo', 'realStarted', 'finishedDate'))
                valid = valid and hours(actual.get('consumed', -1)) == hours(snapshot['before']['consumed']) + hours(body['currentConsumed'])
            else:
                valid = actual.get('status') == 'closed'
            if not valid:
                raise SkillError('回查结果与请求不一致。')
        except SkillError as exc:
            private_write(receipt_path, {'status': 'unknown', 'action': action, 'task': task})
            raise SkillError(f'{exc} 已保留操作记录；请只读核对禅道，禁止自动重发。') from None
        except Exception:
            private_write(receipt_path, {'status': 'unknown', 'action': action, 'task': task})
            raise SkillError('任务操作结果未完整核验，已保留操作记录。请只读核对禅道，禁止自动重发。') from None
        private_write(receipt_path, {'status': 'verified', 'action': action, 'task': task})
        cleanup = 'cleaned'
        try:
            # 只移除技能自身创建的计划文件，用户指定的外部文件保持原样。
            source = Path(snapshot.get('source_path', ''))
            state_root = state_dir().resolve()
            root = state_root / 'runs'
            if (source.name == 'plan.json' and re.fullmatch(r'[a-f0-9]{32}', source.parent.name)
                    and source.parent.parent == root and not source.is_symlink()
                    and source.resolve().parent.parent == root):
                move_to_trash(source.parent, state_root)
            move_to_trash(path, state_root)
            move_to_trash(receipt_path, state_root)
        except OSError:
            cleanup = 'pending'
        return {'status': 'verified', 'action': LABELS[action], 'task': task,
                'url': client.base + f'/task-view-{task}.html', 'cleanup': cleanup}
