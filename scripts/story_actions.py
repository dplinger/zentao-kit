"""禅道需求操作：新建、修改、变更、关闭、激活。

全部走 v1 接口（修改用 PUT `/stories/{id}`，状态流转用 POST `/stories/{id}/{action}`），
依据开源版 `api/v1/entries/story*.php` 的路由与字段定义实现。

**权限**：需求的写权限通常属产品经理角色，开发或测试账号常见无权限
（实测创建/修改/关闭/激活/指派/评审均返回 HTTP 403，而对照的假路由返回 404、
Bug 与任务的写返回 400 或成功——说明路由存在、只是缺需求写权限）。
本模块的错误提示专门把 403 翻译成「账号缺需求权限」，请据此引导用户。
"""

import html
from pathlib import Path
import re

from common import (actions_dir, SkillError, brief, digest, missing_error, move_to_trash,
                    plain_text, private_write, read_json, rich_text, state_dir)

LABELS = {'create': '新建需求', 'update': '修改需求', 'change': '变更需求',
          'close': '关闭需求', 'activate': '激活需求'}
# 可编辑字段，取自 v1 条目类的 batchSetPost 列表
EDIT_FIELDS = {'title', 'spec', 'verify', 'pri', 'category', 'estimate', 'plan',
               'module', 'source', 'sourceNote', 'keywords', 'type', 'stage'}
FIELDS = {
    'create': EDIT_FIELDS | {'product'},
    'update': EDIT_FIELDS,
    'change': {'title', 'spec', 'verify', 'comment'},
    'close': {'closedReason', 'duplicateStory', 'comment'},
    'activate': {'assignedTo', 'status', 'comment'},
}
# 写后应处于的状态；创建时若产品启用了强制评审，服务端会把状态置为 reviewing，故都接受。
EXPECT_STATUS = {'create': ('active', 'reviewing'), 'change': None,
                 'close': ('closed',), 'activate': ('active', 'reviewing')}
# 修改后不应无故变化的字段：服务端会用旧值兜底，仍逐项核对以发现漂移。
MONITORED = tuple(sorted(EDIT_FIELDS | {'product', 'status'}))
# 关闭原因的常见取值；不硬校验取值以兼容不同发行版，但要求非空。
CLOSE_REASONS = ('done', 'duplicate', 'postponed', 'willnotdo')
PERMISSION_HINT = ('当前账号没有需求的写权限（HTTP 403），本次操作未执行。需求的创建、修改、'
                   '关闭、激活通常属于产品经理角色权限，请让禅道管理员为该账号授予需求相关权限后重试。')


def ident(value):
    if isinstance(value, bool) or not str(value).isdigit() or int(value) < 1:
        raise SkillError('需求或产品编号必须为正整数。')
    return int(value)


def field(value, key):
    result = value.get(key)
    if isinstance(result, dict):
        result = result.get('account' if key == 'assignedTo' else 'id')
    if isinstance(result, str):
        result = html.unescape(result)
    return result


def detail(client, story_id):
    """读取需求实时详情。用 v1 单条接口——列表接口与 v2 详情都不含 spec 正文。"""
    story_id = ident(story_id)
    try:
        value = client.call('GET', f'v1/stories/{story_id}')
    except SkillError as exc:
        raise (missing_error(exc, '需求', story_id) or exc) from None
    value = value.get('story', value) if isinstance(value, dict) else {}
    if not isinstance(value, dict) or str(value.get('id')) != str(story_id):
        raise SkillError('需求详情格式不匹配，已停止。')
    return value


def validate(action, fields):
    if action not in FIELDS:
        raise SkillError('需求操作仅支持 create/update/change/close/activate。')
    if not isinstance(fields, dict) or set(fields) - FIELDS[action]:
        raise SkillError('需求操作包含不支持的字段，请按需求操作说明修正。')
    for key in ('title', 'spec', 'verify', 'sourceNote', 'keywords', 'comment'):
        if key in fields and not isinstance(fields[key], str):
            raise SkillError(f'{key} 必须是文本。')
    if 'pri' in fields and (isinstance(fields['pri'], bool) or fields['pri'] not in (1, 2, 3, 4)):
        raise SkillError('需求优先级必须为 1 至 4。')
    for key in ('product', 'plan', 'module', 'duplicateStory'):
        if key in fields and (isinstance(fields[key], bool) or not str(fields[key]).isdigit()):
            raise SkillError(f'{key} 必须是非负整数编号。')
    if 'estimate' in fields:
        from zentao import hours
        fields['estimate'] = float(hours(fields['estimate']))
    if action == 'create':
        missing = [name for name in ('title', 'spec', 'pri', 'category') if not fields.get(name)]
        if missing:
            raise SkillError('创建需求必须提供标题、描述、优先级和类别（缺少：'
                             + '、'.join(missing) + '）；类别常见取值为 ' + '、'.join(
                                 ('feature', 'interface', 'performance', 'safe', 'experience',
                                  'improve', 'other')) + '。')
    if action == 'change' and not fields.get('title'):
        raise SkillError('变更需求必须提供标题。')
    if action == 'update' and not fields:
        raise SkillError('修改需求至少指定一个字段。')
    if action == 'close':
        if not fields.get('closedReason'):
            raise SkillError('关闭需求必须给出关闭原因 closedReason，常见取值：'
                             + '、'.join(CLOSE_REASONS) + '。')
        if fields['closedReason'] == 'duplicate' and not fields.get('duplicateStory'):
            raise SkillError('关闭原因为重复时，必须给出重复需求编号 duplicateStory。')
    if 'assignedTo' in fields and (not isinstance(fields['assignedTo'], str)
                                   or not fields['assignedTo'].strip()):
        raise SkillError('指派人须为登录账号名，不能用姓名代替。')


def prepare(plan, client, source_path=None):
    """保存绑定账号与远端版本的操作预览，绝不写入禅道。"""
    action = plan.get('action')
    if action not in LABELS or not isinstance(plan.get('fields'), dict):
        raise SkillError('需指定 create/update/change/close/activate 以及 fields 对象。')
    requested = dict(plan['fields'])
    for key in ('spec', 'verify'):
        if key in requested:
            requested[key] = rich_text(requested[key])
    story_id = None if action == 'create' else ident(plan.get('story'))
    if action == 'create':
        # 产品编号可放计划顶层或 fields；编号不存在时接口不报错，必须先验证。
        from query import verify_scope
        raw = plan.get('product') if plan.get('product') is not None else requested.get('product')
        product = ident(raw)
        verify_scope(client, {'product': product})
        requested['product'] = product
    validate(action, requested)

    before = None
    if story_id is not None:
        before = detail(client, story_id)
        product = ident(before.get('product'))
        status = str(before.get('status') or '')
        if action in ('update', 'change') and status == 'closed':
            raise SkillError('已关闭的需求不能直接修改或变更，请先激活。')
        if action == 'change' and status not in ('active', 'reviewing', 'changing', 'draft'):
            raise SkillError(f'当前状态（{status}）不适合变更，请核对该需求。')
        if action == 'close' and status not in ('active', 'reviewing', 'changing'):
            raise SkillError(f'当前状态（{status}）不适合关闭；请确认该需求是否已评审通过。')
        if action == 'activate' and status != 'closed':
            raise SkillError('只能激活已关闭的需求。')
    if action == 'create' and product is None:
        raise SkillError('创建需求必须指定产品编号。')

    body = requested.copy()
    # 快照只存摘要与指纹：需求正文可能有上兆字节，整份存进状态目录会让快照膨胀。
    snapshot = {'action': action, 'account': client.account, 'base_url': client.base,
                'story': story_id, 'product': product, 'body': body, 'requested': requested,
                'before_digest': digest(before) if before else None,
                'before_status': before.get('status') if before else None}
    if source_path:
        snapshot.update(source_path=str(Path(source_path).resolve()), source_digest=digest(plan))
    review = digest(snapshot)
    private_write(actions_dir() / f'{review}.json', snapshot)
    changes = {key: {'before': preview_value(key, field(before, key) if before else None),
                     'after': preview_value(key, body[key])}
               for key in requested if key != 'comment'}
    notice = None
    if action == 'close':
        notice = ('关闭原因为 ' + str(body['closedReason']) + '；关闭后需求将不再出现在未关闭列表中，'
                  '需要时可用 activate 重新激活。')
    elif action == 'change':
        notice = '变更会生成新版本并保留历史版本，原版本的评审状态不会自动沿用。'
    return {'status': 'preview_only', 'review': review, 'action': LABELS[action],
            'story': story_id, 'product': product,
            'title': field(before, 'title') if before else requested.get('title'),
            'before_status': before.get('status') if before else None,
            'changes': changes, 'notice': notice,
            'message': '请核对以上需求操作，确认无误后回复“确认执行”；需要调整可直接告诉我。'}


def preview_value(key, value):
    """预览里把富文本压成纯文本并截断，避免整篇需求描述刷屏（写入用的仍是完整值）。"""
    return brief(value) if key in ('spec', 'verify') else value


def matches(actual, expected, key):
    value = field(actual, key)
    if key in ('spec', 'verify'):
        return plain_text(value) == plain_text(expected)
    if key in ('pri', 'estimate', 'product', 'plan', 'module', 'duplicateStory'):
        try:
            return float(value or 0) == float(expected or 0)
        except (TypeError, ValueError):
            return False
    return str(value or '').strip() == str(expected or '').strip()


def execute(review, confirmation, client):
    """单次发送后读详情回查；结果不明保留收据且禁止重发。"""
    from zentao import submission_lock
    if not re.fullmatch(r'[a-f0-9]{64}', review) or confirmation != review:
        raise SkillError('缺少与需求预览一致的确认。')
    path = actions_dir() / f'{review}.json'
    receipt = path.with_suffix('.receipt.json')
    with submission_lock():
        if not path.exists() or receipt.exists():
            raise SkillError('需求预览不存在或该操作已尝试执行，禁止重复提交。')
        snapshot = read_json(path)
        if digest(snapshot) != review or snapshot['account'] != client.account or snapshot['base_url'] != client.base:
            raise SkillError('需求预览、账号或站点发生变化，请重新预览。')
        if snapshot.get('source_path') and digest(read_json(snapshot['source_path'])) != snapshot['source_digest']:
            raise SkillError('需求草稿已修改，请重新预览。')
        action, story_id, body = snapshot['action'], snapshot['story'], snapshot['body']
        before = None
        if story_id is not None:
            # 重新读取实时详情：既核对是否被人改过，也用于「未请求字段漂移」的比对。
            before = detail(client, story_id)
            if digest(before) != snapshot['before_digest']:
                raise SkillError('需求自预览后发生变化，请重新生成预览。')
        suffix = f'/{action}' if action in ('change', 'close', 'activate') else ''
        endpoint = 'v1/stories' if action == 'create' else f'v1/stories/{story_id}{suffix}'
        private_write(receipt, {'status': 'inflight', 'action': action, 'story': story_id})
        try:
            response = client.call('PUT' if action == 'update' else 'POST', endpoint, body)
            if not isinstance(response, dict):
                raise SkillError('需求写接口未返回有效响应。')
            if action == 'create':
                story_id = ident(response.get('id'))
                private_write(receipt, {'status': 'inflight', 'action': action, 'story': story_id})
            elif str(response.get('id')) != str(story_id):
                raise SkillError('需求写接口返回的编号与请求不一致，结果不明。')
            actual = detail(client, story_id)
            if ident(actual.get('product')) != snapshot['product']:
                raise SkillError('回查发现需求所属产品与预览不一致。')
            allowed = EXPECT_STATUS.get(action)
            if allowed and actual.get('status') not in allowed:
                raise SkillError(f'写后需求状态为 {actual.get("status")}，与预览预期不一致。')
            if action in ('create', 'update', 'change'):
                expected = {key: value for key, value in body.items()
                            if key not in ('comment', 'product', 'status')}
                if action == 'change':
                    expected = {key: value for key, value in body.items()
                                if key in ('title', 'spec', 'verify')}
                if not all(matches(actual, value, key) for key, value in expected.items()):
                    raise SkillError('回查字段与请求不一致。')
            if action == 'activate' and body.get('assignedTo'):
                if not matches(actual, body['assignedTo'], 'assignedTo'):
                    raise SkillError('回查发现指派人未生效。')
            if action == 'update':
                # 服务端用旧值兜底未提供的字段；仍逐项核对，发现漂移即报出具体字段名。
                drifted = [key for key in MONITORED
                           if key not in snapshot['requested']
                           and (before or {}).get(key) not in (None, '')
                           and not matches(actual, (before or {})[key], key)]
                if drifted:
                    raise SkillError('修改需求后以下未请求字段被改变：' + '、'.join(drifted) + '。')
        except SkillError as exc:
            private_write(receipt, {'status': 'unknown', 'action': action, 'story': story_id})
            text = str(exc)
            if '403' in text or 'PERMISSION_DENIED' in text:
                raise SkillError(PERMISSION_HINT + '已保留操作记录，禁止自动重发。') from None
            raise SkillError(f'{exc} 已保留收据；请只读回查，禁止自动重发。') from None
        except Exception:
            private_write(receipt, {'status': 'unknown', 'action': action, 'story': story_id})
            raise SkillError('需求写入结果未完整核验，已保留操作记录。请只读核对禅道，禁止自动重发。') from None
        private_write(receipt, {'status': 'verified', 'action': action, 'story': story_id})
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
        return {'status': 'verified', 'action': LABELS[action], 'story': story_id,
                'url': client.base + f'/story-view-{story_id}.html', 'cleanup': cleanup}
