"""禅道待办操作：新建、修改、完成、激活。

依据开源版 `api/v1/entries/todos.php`、`todo.php`、`todofinish.php`、`todoactivate.php`
与 `module/todo/` 的控制器、模型、表单定义实现。

两处与其它模块不同的约定，都是接口本身如此，不是笔误：
  - **完成与激活是 GET 请求**（`GET v1/todos/{id}/finish|activate`），两个条目类只实现了
    `get()`。也就是说这两个 GET 带副作用，调用即写入，绝不能当查询重试。
  - **没有关闭待办的接口**：`config/apiv1.php` 只注册 `/todos`、`/todos/:id`、
    `/todos/:id/finish`、`/todos/:id/activate`。用 PUT 把 status 改成 closed 会绕过模型的
    `close()`（它负责写 closedBy/closedDate 并改 assignedTo），属半吊子写入，故不提供。
"""

import datetime as dt
from pathlib import Path
import re

from common import (actions_dir, SkillError, brief, digest, missing_error, move_to_trash,
                    plain_text, private_write, read_json, rich_text, stamp, state_dir)

LABELS = {'create': '新建待办', 'update': '修改待办', 'finish': '完成待办', 'activate': '激活待办'}
STATUS_TEXT = {'wait': '未开始', 'doing': '进行中', 'done': '已完成', 'closed': '已关闭'}
OPEN_STATUS = ('wait', 'doing')
# 创建：只做个人事务型待办（type=custom）。关联型待办需要 objectID 等字段，不在本技能范围内。
CREATE_FIELDS = {'name', 'desc', 'date', 'begin', 'end', 'pri', 'private'}
# 修改：status 只放开 wait/doing（开始或暂停）；完成必须显式走 finish，才会写完成时间。
EDIT_FIELDS = CREATE_FIELDS | {'status'}
PERMISSION_HINT = ('当前账号无权操作待办（HTTP 403），本次操作未执行。待办属个人事务，'
                   '通常无需额外授权；若持续出现请让禅道管理员检查该账号的待办权限。')


def ident(value):
    if isinstance(value, bool) or not str(value).isdigit() or int(value) < 1:
        raise SkillError('待办编号必须为正整数。')
    return int(value)


def clock_value(value):
    """时间归一化成四位数；空值与禅道的 0 都表示未设置。"""
    text = stamp(value)
    return '' if text in ('', '0', '00', '000', '0000') else text


def clock(value, key):
    """校验并归一化时间输入；接口接受 HH:MM，服务端去掉冒号后按四位数存储。"""
    text = str(value or '').strip()
    if not text:
        return ''
    match = re.fullmatch(r'(\d{1,2}):?(\d{2})', text)
    if not match:
        raise SkillError(f'{key} 必须是 HH:MM 形式，例如 09:30。')
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 24 or minute > 59 or (hour == 24 and minute):
        raise SkillError(f'{key} 必须在 00:00 至 24:00 之间。')
    return f'{hour:02d}{minute:02d}'


def flag(value):
    """private 是 0/1；网页表单会送 'on'，一并接受。"""
    if value in (True, 1, '1', 'on', 'true'):
        return '1'
    if value in (False, 0, '0', 'off', 'false', None, ''):
        return '0'
    raise SkillError('private 只能是 true/false（是否私有）。')


def normalize(fields):
    """校验并归一化用户给的字段；返回新对象，不改动入参。"""
    values = dict(fields)
    if 'name' in values:
        if not isinstance(values['name'], str) or not values['name'].strip():
            raise SkillError('待办名称不能为空。')
        values['name'] = values['name'].strip()
    if 'desc' in values:
        if not isinstance(values['desc'], str):
            raise SkillError('待办说明 desc 必须是文本。')
        values['desc'] = rich_text(values['desc'])
    if 'pri' in values:
        if isinstance(values['pri'], bool) or values['pri'] not in (1, 2, 3, 4):
            raise SkillError('待办优先级必须为 1 至 4。')
    if 'date' in values:
        try:
            values['date'] = dt.date.fromisoformat(str(values['date'])).isoformat()
        except ValueError as exc:
            raise SkillError('日期必须是 YYYY-MM-DD。') from exc
    for key in ('begin', 'end'):
        if key in values:
            values[key] = clock(values[key], key)
    if values.get('begin') and values.get('end') and values['begin'] > values['end']:
        raise SkillError('开始时间不能晚于结束时间。')
    if 'status' in values and values['status'] not in OPEN_STATUS:
        raise SkillError('修改待办只能把状态改为 wait（未开始）或 doing（进行中）；'
                         '完成请用 finish，激活请用 activate。')
    if 'private' in values:
        values['private'] = flag(values['private'])
    return values


def detail(client, todo):
    """读取待办实时详情。条目直接返回待办对象，兼容包一层 todo 的形态。"""
    todo = ident(todo)
    try:
        value = client.call('GET', f'v1/todos/{todo}')
    except SkillError as exc:
        raise (missing_error(exc, '待办', todo) or exc) from None
    if isinstance(value, dict) and isinstance(value.get('todo'), dict):
        value = value['todo']
    if not isinstance(value, dict) or str(value.get('id')) != str(todo):
        raise SkillError('待办详情格式不匹配，已停止。')
    return value


def field(value, key):
    """待办字段取值；人员或关联字段是嵌套对象时取其账号或编号。"""
    value = (value or {}).get(key)
    if isinstance(value, dict):
        return value.get('account') or value.get('id')
    return value


def matches(actual, expected, key):
    value = field(actual, key)
    if key == 'desc':
        return plain_text(value) == plain_text(expected)
    if key in ('begin', 'end'):
        return clock_value(value) == clock_value(expected)
    if key == 'private':
        return flag(value) == flag(expected)
    if key == 'date':
        return stamp(value) == stamp(expected)
    return str(value or '').strip() == str(expected or '').strip()


def preview_value(key, value):
    """预览里统一展示形态：说明压成纯文本并截断，时间补回冒号（写入用的仍是原值）。"""
    if value is None:
        return None
    if key in ('begin', 'end'):
        text = clock_value(value)
        return f'{text[:2]}:{text[2:]}' if len(text) == 4 else value
    return brief(value) if key == 'desc' else value


def prepare(plan, client, source_path=None):
    """保存绑定账号与远端版本的操作预览，绝不写入禅道。"""
    action = plan.get('action')
    fields = plan.get('fields') or {}
    if action not in LABELS or not isinstance(fields, dict):
        raise SkillError('需指定 create/update/finish/activate 以及 fields 对象。')
    if action in ('finish', 'activate'):
        if fields:
            raise SkillError(f'{LABELS[action]}不接受字段：完成与激活是单一动作，没有可改的属性。')
    elif set(fields) - (CREATE_FIELDS if action == 'create' else EDIT_FIELDS):
        raise SkillError('待办操作包含不支持的字段，请按待办操作说明修正。')

    requested = normalize(fields) if action in ('create', 'update') else {}
    todo, before = None, None
    if action == 'create':
        if 'name' not in requested:
            raise SkillError('新建待办必须提供名称 name。')
        requested.setdefault('date', dt.date.today().isoformat())
    else:
        todo = ident(plan.get('todo'))
        before = detail(client, todo)
        status = str(before.get('status') or '')
        kind = str(before.get('type') or '')
        if kind != 'custom':
            # 任务/需求/Bug 转成的待办由来源对象驱动，这里既不修改也不完成它。
            raise SkillError(f'该待办由{kind}类型对象生成（关联模式），只读即可；'
                             '本技能只用待办接口操作个人事务型（custom）待办。')
        if action == 'update':
            if not requested:
                raise SkillError('修改待办至少指定一个字段。')
            if status not in OPEN_STATUS:
                raise SkillError(f'当前状态为{STATUS_TEXT.get(status, status)}，不能直接修改；'
                                 '请先用 activate 激活。')
        elif action == 'finish' and status not in OPEN_STATUS:
            raise SkillError(f'当前状态为{STATUS_TEXT.get(status, status)}，不需要再完成'
                             '（只有未开始或进行中的待办可以完成）。')
        elif action == 'activate' and status not in ('done', 'closed'):
            raise SkillError(f'当前状态为{STATUS_TEXT.get(status, status)}，'
                             '只有已完成或已关闭的待办才需要激活。')

    # 快照只存摘要与指纹：待办说明可能很长，整份存进状态目录会让快照无谓膨胀。
    snapshot = {'action': action, 'account': client.account, 'base_url': client.base,
                'todo': todo, 'body': requested, 'requested': requested,
                'before_digest': digest(before) if before else None}
    if source_path:
        snapshot.update(source_path=str(Path(source_path).resolve()), source_digest=digest(plan))
    review = digest(snapshot)
    private_write(actions_dir() / f'{review}.json', snapshot)
    changes = {key: {'before': preview_value(key, field(before, key)),
                     'after': preview_value(key, requested[key])}
               for key in requested}
    notice = None
    if action == 'create':
        notice = '待办按个人事务型（custom）创建并归属当前登录人；接口不支持指派给他人。'
    elif action == 'finish':
        notice = '完成后状态变为「已完成」并记录完成时间；需要重做时用 activate 激活。'
    elif action == 'activate':
        notice = '激活后状态回到「未开始」，可以再次修改或完成。'
    return {'status': 'preview_only', 'review': review, 'action': LABELS[action], 'todo': todo,
            'name': field(before, 'name') if before else requested.get('name'),
            'before_status': before.get('status') if before else None,
            'changes': changes, 'notice': notice,
            'message': '请核对以上待办操作，确认无误后回复“确认执行”；需要调整可直接告诉我。'}


def execute(review, confirmation, client):
    """单次发送后读详情回查；结果不明保留收据且禁止重发。"""
    from zentao import submission_lock
    if not re.fullmatch(r'[a-f0-9]{64}', review) or confirmation != review:
        raise SkillError('缺少与待办预览一致的确认。')
    path = actions_dir() / f'{review}.json'
    receipt = path.with_suffix('.receipt.json')
    with submission_lock():
        if not path.exists() or receipt.exists():
            raise SkillError('待办预览不存在或该操作已尝试执行，禁止重复提交。')
        snapshot = read_json(path)
        if digest(snapshot) != review or snapshot['account'] != client.account or snapshot['base_url'] != client.base:
            raise SkillError('待办预览、账号或站点发生变化，请重新预览。')
        if snapshot.get('source_path') and digest(read_json(snapshot['source_path'])) != snapshot['source_digest']:
            raise SkillError('待办草稿已修改，请重新预览。')
        action, todo, body = snapshot['action'], snapshot['todo'], snapshot['body']
        before = None
        if todo is not None:
            # 重新读取实时详情：既核对是否被人改过，也用于「未请求字段漂移」的比对。
            before = detail(client, todo)
            if digest(before) != snapshot['before_digest']:
                raise SkillError('待办自预览后发生变化，请重新生成预览。')
        # 完成与激活在接口里是 GET（带副作用）；创建用 POST，修改用 PUT。
        if action in ('finish', 'activate'):
            method, endpoint = 'GET', f'v1/todos/{todo}/{action}'
        elif action == 'update':
            method, endpoint = 'PUT', f'v1/todos/{todo}'
        else:
            method, endpoint = 'POST', 'v1/todos'
        private_write(receipt, {'status': 'inflight', 'action': action, 'todo': todo})
        try:
            response = client.call(method, endpoint, body if method != 'GET' else None)
            if not isinstance(response, dict):
                raise SkillError('待办写接口未返回有效响应。')
            if action == 'create':
                created = response['todo'] if isinstance(response.get('todo'), dict) else response
                if created.get('id') is None:
                    raise SkillError('创建待办未返回编号，结果不明。')
                todo = ident(created['id'])
                private_write(receipt, {'status': 'inflight', 'action': action, 'todo': todo})
            elif str(response.get('id')) != str(todo):
                raise SkillError('待办写接口返回的编号与请求不一致，结果不明。')
            actual = detail(client, todo)
            if action == 'finish':
                if str(actual.get('status')) != 'done' or str(actual.get('finishedDate') or '').startswith('0000'):
                    raise SkillError('完成待办后回查的状态或完成时间不符。')
            elif action == 'activate':
                if str(actual.get('status')) != 'wait':
                    raise SkillError(f'激活后状态为 {actual.get("status")}，与预期不符。')
            else:
                expected = body if action == 'create' else snapshot['requested']
                if not all(matches(actual, value, key) for key, value in expected.items()):
                    raise SkillError('回查字段与请求不一致。')
                if action == 'update':
                    # 服务端用旧值兜底未提供的字段；仍逐项核对，发现漂移即报出具体字段名。
                    drifted = [key for key in sorted(EDIT_FIELDS)
                               if key not in snapshot['requested']
                               and not matches(actual, field(before, key), key)]
                    if drifted:
                        raise SkillError('修改待办后以下未请求字段被改变：' + '、'.join(drifted) + '。')
        except SkillError as exc:
            private_write(receipt, {'status': 'unknown', 'action': action, 'todo': todo})
            text = str(exc)
            if '403' in text or 'PERMISSION_DENIED' in text:
                raise SkillError(PERMISSION_HINT + '已保留操作记录，禁止自动重发。') from None
            raise SkillError(f'{exc} 已保留收据；请只读回查，禁止自动重发。') from None
        except Exception:
            private_write(receipt, {'status': 'unknown', 'action': action, 'todo': todo})
            raise SkillError('待办写入结果未完整核验，已保留操作记录。请只读核对禅道，禁止自动重发。') from None
        private_write(receipt, {'status': 'verified', 'action': action, 'todo': todo})
        cleanup = 'cleaned'
        try:
            # 只移除技能自身创建的计划文件，用户指定的外部文件保持原样。
            source = Path(snapshot.get('source_path', ''))
            root = state_dir().resolve()
            if (source.name == 'plan.json' and re.fullmatch(r'[a-f0-9]{32}', source.parent.name)
                    and source.parent.parent == root / 'runs' and not source.is_symlink()
                    and source.resolve().parent.parent == root / 'runs'):
                move_to_trash(source.parent, root)
            move_to_trash(path, root)
            move_to_trash(receipt, root)
        except (OSError, SkillError):
            cleanup = 'pending'
        return {'status': 'verified', 'action': LABELS[action], 'todo': todo,
                'url': client.base + f'/todo-view-{todo}.html', 'cleanup': cleanup}
