"""实体写操作的共用骨架：字段类型校验 + 两阶段（预览 → 确认 → 单次写入 → 回查）。

产品、项目、执行、测试单、用例的接口规律一致（集合 POST 创建、单条 PUT 修改、单条 GET 详情），
差异只在字段与关联上，因此共用本骨架——每个模块只声明一张 SPECS 表，不外带流程代码。

约定（都来自实测的接口行为）：
  - 关联编号一律在预览阶段预检存在：接口对写错的编号未必报错（D7）。
  - 回查核对本次请求的字段；实体还可用 `monitor_fields` 声明修改时必须保持不变的关键字段。
    不检查未声明的派生字段（进度、工时、子项统计），避免无关变化造成误报。
  - 比对一律放宽（日期去分隔符、关联编号取子集）：同一字段在不同端点、不同形态间的
    表达方式并不统一（C7、C9）。
"""

import datetime as dt
from pathlib import Path
import re

from common import (actions_dir, SkillError, brief, digest, missing_error, move_to_trash,
                    plain_text, private_write, read_json, rich_text, stamp, state_dir)

TEXT, TEXTS, DATE, ID, IDS, ACCOUNT, ACCOUNTS, ENUM, INT, STEPS = (
    'text', 'texts', 'date', 'id', 'ids', 'account', 'accounts', 'enum', 'int', 'steps')
BOTH, CREATE, UPDATE = ('create', 'update'), ('create',), ('update',)
ACTION_LABEL = {'create': '新建', 'update': '修改'}
STEP_TYPES = ('step', 'item', 'group')
PERMISSION_HINT = ('当前账号没有{label}的写权限（HTTP 403），本次操作未执行。{label}的写权限通常属'
                   '管理员、产品经理或测试角色，请让禅道管理员为该账号授予相关权限后重试。')


def ident(value, label='编号'):
    if isinstance(value, bool) or not str(value).isdigit() or int(value) < 1:
        raise SkillError(f'{label}必须为正整数。')
    return int(value)


def number_list(value, label):
    """ID 数组：接受数组或逗号分隔字符串，去重后返回整数列表。"""
    items = value if isinstance(value, (list, tuple)) else str(value).split(',')
    numbers, seen = [], set()
    for item in items:
        if str(item).strip() == '':
            continue
        number = ident(item, label)
        if number not in seen:
            seen.add(number)
            numbers.append(number)
    if not numbers:
        raise SkillError(f'{label}至少要有一个有效编号。')
    return numbers


def account_list(value, label):
    items = value if isinstance(value, (list, tuple)) else str(value).split(',')
    names = [str(item).strip() for item in items if str(item).strip()]
    if not names:
        raise SkillError(f'{label}至少要有一个账号。')
    return names


def step_list(value):
    """用例步骤：数组，每项 {desc, expect, type}；type 取 step/item/group。"""
    if not isinstance(value, (list, tuple)):
        raise SkillError('用例步骤 steps 必须是数组，每项含 desc 与 expect。')
    steps = []
    for index, item in enumerate(value, 1):
        if not isinstance(item, dict):
            raise SkillError(f'第 {index} 条步骤必须是含 desc 与 expect 的对象。')
        desc = str(item.get('desc') or '').strip()
        if not desc:
            raise SkillError(f'第 {index} 条步骤缺少 desc（步骤描述）。')
        kind = str(item.get('type') or 'step')
        if kind not in STEP_TYPES:
            raise SkillError(f'第 {index} 条步骤的 type 只能是 ' + '、'.join(STEP_TYPES) + '。')
        steps.append({'desc': desc, 'expect': str(item.get('expect') or ''), 'type': kind})
    if not steps:
        raise SkillError('用例步骤 steps 至少要有一条。')
    return steps


def convert(name, meta, value):
    """按字段定义校验并归一化单个值。"""
    kind = meta['type']
    if kind == TEXT:
        if not isinstance(value, str):
            raise SkillError(f'{name} 必须是文本。')
        return rich_text(value) if name in ('desc', 'precondition') else value
    if kind == TEXTS:
        if isinstance(value, str):
            return value
        if isinstance(value, (list, tuple)) and all(isinstance(item, str) and item.strip()
                                                    for item in value):
            return [item.strip() for item in value]
        raise SkillError(f'{name} 必须是文本或文本数组。')
    if kind == DATE:
        try:
            return dt.date.fromisoformat(str(value)).isoformat()
        except ValueError as exc:
            raise SkillError(f'{name} 必须是 YYYY-MM-DD 格式的日期。') from exc
    if kind == ID:
        return ident(value, f'{name} 编号')
    if kind == IDS:
        return number_list(value, f'{name} 编号')
    if kind == ACCOUNT:
        if not isinstance(value, str) or not value.strip():
            raise SkillError(f'{name} 须为登录账号名，不能用姓名代替。')
        return value.strip()
    if kind == ACCOUNTS:
        return account_list(value, name)
    if kind == ENUM:
        if str(value) not in meta['values']:
            # 某些字段的非法值有特定后果（如测试单的 status=closed），用 hint 说清楚为什么被拦。
            raise SkillError(f'{name} 只能是 ' + '、'.join(meta['values']) + '。'
                             + (meta.get('hint') or ''))
        return str(value)
    if kind == INT:
        if isinstance(value, bool) or not str(value).lstrip('-').isdigit():
            raise SkillError(f'{name} 必须是整数。')
        return int(value)
    if kind == STEPS:
        return step_list(value)
    raise SkillError(f'字段 {name} 的定义无法识别。')


def build(spec, action, fields, client, lenient=False):
    """校验字段、生成提交体，并预检所有关联编号真实存在。

    `lenient` 用于整份覆盖型 PUT（见 spec 的 `update_fill`）：此类接口要求必填字段齐全，
    用户通常只改其中一项，必填校验改到 `fill_required` 补齐之后再报，避免逼用户重填旧值。
    """
    allowed = {name for name, meta in spec['fields'].items() if action in meta.get('ops', BOTH)}
    unknown = sorted(set(fields) - allowed)
    if unknown:
        raise SkillError(f'{ACTION_LABEL[action]}{spec["label"]}不支持的字段：' + '、'.join(unknown)
                         + '；可用字段：' + '、'.join(sorted(allowed)) + '。')
    body = dict(spec.get('defaults', {})) if action == 'create' else {}
    for name, value in fields.items():
        body[name] = convert(name, spec['fields'][name], value)
    if not lenient:
        missing = [name for name, meta in spec['fields'].items()
                   if action in (meta.get('required') or ()) and not body.get(name)]
        if missing:
            raise SkillError(f'{ACTION_LABEL[action]}{spec["label"]}缺少必填字段：' + '、'.join(missing) + '。')
    if body.get('begin') and body.get('end') and body['begin'] > body['end']:
        raise SkillError('开始日期不能晚于结束日期。')

    # 关联编号写错时接口未必报错，必须先验证存在。
    from query import verify_scope
    for name, meta in spec['fields'].items():
        if name not in body or not meta.get('scope'):
            continue
        values = body[name] if isinstance(body[name], (list, tuple)) else [body[name]]
        for value in values:
            verify_scope(client, {meta['scope']: value})
    return body


def fill_body(spec, body, before):
    """整份覆盖型 PUT 的取值合并：先用当前详情铺满可写字段，再用用户请求覆盖。

    v2 测试单就是这种语义——**没传的字段会被静默清空，而不是保留旧值**。
    实测代价：只传 name/build/begin/end 改个名字，`execution` 与 `owner` 当场被抹成
    0 与空串。所以这里不只是补必填，而是把所有声明过的可写字段都带上。
    """
    merged = {}
    for name, meta in spec['fields'].items():
        if name in body or 'update' not in meta.get('ops', BOTH):
            continue
        value = field(before, name)
        # 空值与禅道的 0 都表示未设置：非必填就跳过（传空等于继续清空，没有意义）。
        if value is None or str(value).strip() in ('', '0'):
            continue
        merged[name] = convert(name, meta, value)
    merged.update(body)
    missing = [name for name, meta in spec['fields'].items()
               if 'update' in (meta.get('required') or ()) and not merged.get(name)]
    if missing:
        raise SkillError(f'修改{spec["label"]}缺少必填字段：' + '、'.join(missing)
                         + '；当前记录里也没有可沿用的值，请在 fields 中给出。')
    return merged


def detail(client, spec, ident_value):
    """读取实时详情；接口多数直接返回实体对象，兼容包一层的情形。"""
    try:
        value = client.call('GET', spec['detail'].format(id=ident_value))
    except SkillError as exc:
        raise (missing_error(exc, spec['label'], ident_value) or exc) from None
    if isinstance(value, dict) and isinstance(value.get(spec['wrap']), dict):
        value = value[spec['wrap']]
    if not isinstance(value, dict) or str(value.get('id')) != str(ident_value):
        raise SkillError(f'{spec["label"]}详情格式不匹配，已停止。')
    return value


def field(value, key):
    result = (value or {}).get(key)
    if isinstance(result, dict):
        result = result.get('account') or result.get('id')
    return result


def loose_numbers(value):
    """尽量取出编号集合：接口对关联字段的表达方式不统一（数组、对象数组、逗号串）。"""
    if value is None:
        return set()
    if isinstance(value, dict):
        # 实体对象只取明确的 id，避免把名称里的版本号等数字误当成关联编号。
        if value.get('id') is not None:
            return loose_numbers(value['id'])
        numeric_keys = {int(key) for key in value if str(key).isdigit()}
        if numeric_keys:
            return numeric_keys
        found = set()
        for item in value.values():
            found |= loose_numbers(item)
        return found
    if isinstance(value, (list, tuple)):
        found = set()
        for item in value:
            found |= loose_numbers(item)
        return found
    return {int(part) for part in re.findall(r'\d+', str(value))}


def matches(actual, expected, key, meta):
    """宽松比对：字段会被服务端规范化或派生，严格相等必然误报。"""
    value = field(actual, key)
    kind = meta['type']
    if key in ('desc', 'precondition'):
        return plain_text(value) == plain_text(expected)
    if kind == DATE:
        return stamp(value)[:8] == stamp(expected)[:8]
    if kind in (ID, INT):
        try:
            return float(value or 0) == float(expected or 0)
        except (TypeError, ValueError):
            return False
    if kind == IDS:
        return set(expected) <= loose_numbers(value)
    if kind == ACCOUNTS:
        return {str(name) for name in expected} <= {part for part in re.split(r'[,\s]+', str(value or '')) if part}
    if kind == TEXTS:
        def values(item):
            items = item if isinstance(item, (list, tuple)) else str(item or '').split(',')
            return {str(part).strip() for part in items if str(part).strip()}
        return values(value) == values(expected)
    if kind == STEPS:
        rows = value if isinstance(value, list) else []
        got = [(str(row.get('desc') or '').strip(), str(row.get('expect') or '').strip())
               for row in rows if isinstance(row, dict)]
        return got[:len(expected)] == [(row['desc'], row['expect']) for row in expected]
    return str(value or '').strip() == str(expected or '').strip()


def monitor_fields(spec):
    """校验并返回实体声明的稳定字段，配置错误必须在写入前暴露。"""
    fields = tuple(spec.get('monitor_fields', ()))
    unknown = [key for key in fields if key not in spec['fields']]
    if unknown:
        raise SkillError(f'{spec["label"]}的 monitor_fields 含未声明字段：'
                         + '、'.join(unknown) + '。')
    return fields


def unchanged(before, actual, key, meta):
    """按字段语义判断修改前后是否一致；关联编号数组必须集合完全相等。"""
    expected = field(before, key)
    if meta['type'] == IDS:
        return loose_numbers(expected) == loose_numbers(field(actual, key))
    return matches(actual, expected, key, meta)


def drifted_fields(spec, before, actual, requested):
    """返回修改后意外变化的稳定字段；只检查实体显式声明的 monitor_fields。"""
    drifted = []
    for key in monitor_fields(spec):
        if key in requested:
            continue
        meta = spec['fields'][key]
        expected = field(before, key)
        # 详情接口没有返回该字段时无法形成可靠基线，跳过以免误报。
        if expected is None:
            continue
        if not unchanged(before, actual, key, meta):
            drifted.append(key)
    return drifted


def verification_fields(spec, action, body, requested):
    """返回写后要核验的字段；整份覆盖型修改核验实际提交的全部业务字段。"""
    candidates = body if action == 'update' and spec.get('verify_full_body') else requested
    return [key for key in candidates
            if key in spec['fields'] and key not in spec.get('skip_verify', ())]


def prepare(plan, client, spec, entity, source_path=None):
    """保存绑定账号与远端版本的操作预览，绝不写入禅道。"""
    monitor_fields(spec)
    action = plan.get('action')
    if action not in ACTION_LABEL or not isinstance(plan.get('fields'), dict):
        raise SkillError('需指定 create 或 update 以及 fields 对象。')
    if not plan['fields']:
        raise SkillError(f'{ACTION_LABEL[action]}{spec["label"]}至少要指定一个字段。')
    body = build(spec, action, plan['fields'], client,
                 lenient=bool(action == 'update' and spec.get('update_fill')))

    target, before = None, None
    if action == 'update':
        # 编号键与实体同名：{"action":"update","product":84,"fields":{…}}
        if plan.get(entity) is None:
            raise SkillError(f'修改{spec["label"]}需用 {entity} 指定编号，例如 {{"{entity}": 84}}。')
        target = ident(plan.get(entity), f'{spec["label"]}编号')
        before = detail(client, spec, target)
        if spec.get('update_fill'):
            body = fill_body(spec, body, before)
        # 服务端会把未提供的字段用旧值兜底，故日期范围要按改后的组合再核一次。
        start = body.get('begin') or str(field(before, 'begin') or '')[:10]
        end = body.get('end') or str(field(before, 'end') or '')[:10]
        if start and end and start > end:
            raise SkillError('开始日期不能晚于结束日期（含服务端将保留的旧值）。')

    # 实体特有的关联约束交给各模块自查（如测试单的执行/构建归属）。
    # 传入用户实际请求的字段，避免「只改名称」也被历史关联挡住。
    if spec.get('precheck'):
        spec['precheck'](client, action, body, set(plan['fields']))

    snapshot = {'entity': entity, 'action': action, 'account': client.account,
                'base_url': client.base, 'target': target, 'body': body,
                'requested': sorted(plan['fields']),
                'before': before,
                'before_digest': digest(before) if before else None}
    if source_path:
        snapshot.update(source_path=str(Path(source_path).resolve()), source_digest=digest(plan))
    review = digest(snapshot)
    private_write(actions_dir() / f'{review}.json', snapshot)

    def show(key, value):
        if value is None:
            return None
        return brief(value) if key in ('desc', 'precondition') else value

    changes = {key: {'before': show(key, field(before, key)), 'after': show(key, body[key])}
               for key in plan['fields']}
    return {'status': 'preview_only', 'review': review, 'entity': entity,
            'action': ACTION_LABEL[action] + spec['label'], 'id': target,
            'name': field(before, 'name') if before else body.get('name'),
            'changes': changes, 'notice': spec.get('notices', {}).get(action),
            'message': '请核对以上内容，确认无误后回复“确认执行”；需要调整可直接告诉我。'}


def execute(review, confirmation, client, specs):
    """单次发送后读详情回查；结果不明保留收据且禁止重发。"""
    from zentao import submission_lock
    if not re.fullmatch(r'[a-f0-9]{64}', review) or confirmation != review:
        raise SkillError('缺少与预览一致的确认。')
    path = actions_dir() / f'{review}.json'
    receipt = path.with_suffix('.receipt.json')
    with submission_lock():
        if not path.exists() or receipt.exists():
            raise SkillError('预览不存在或该操作已尝试执行，禁止重复提交。')
        snapshot = read_json(path)
        if digest(snapshot) != review or snapshot['account'] != client.account or snapshot['base_url'] != client.base:
            raise SkillError('预览、账号或站点发生变化，请重新预览。')
        if snapshot.get('source_path') and digest(read_json(snapshot['source_path'])) != snapshot['source_digest']:
            raise SkillError('草稿已修改，请重新预览。')
        spec = specs[snapshot['entity']]
        action, target, body = snapshot['action'], snapshot['target'], snapshot['body']
        if action == 'update' and not isinstance(snapshot.get('before'), dict):
            raise SkillError('该修改预览由旧版骨架生成，缺少稳定字段快照，请重新生成预览。')
        if target is not None and digest(detail(client, spec, target)) != snapshot['before_digest']:
            raise SkillError(f'{spec["label"]}自预览后发生变化，请重新生成预览。')
        submitted_body = body
        if action == 'create':
            method = 'POST'
            endpoint = spec['collection']
            # 部分集合的路径参数只认路径（如执行的 project），放 body 会被服务端覆盖成 0。
            for key in spec.get('path_args', ()):
                endpoint = endpoint.replace('{' + key + '}', str(body[key]))
        else:
            method = 'PUT'
            # 少数实体的修改端点与详情端点不在同一版本：测试单 v1 没有 PUT（返回 200 空响应，
            # 是假成功），真接口在 v2。故允许单独声明 update_endpoint。
            endpoint = (spec.get('update_endpoint') or spec['detail']).format(id=target)
            # 同一实体在 v1/v2 的字段名可能不同（测试单：v1 认 product，v2 文档写 productID）。
            if spec.get('update_wire'):
                submitted_body = {(spec['update_wire'].get(key, key)): value
                                  for key, value in body.items()}
        private_write(receipt, {'status': 'inflight', 'entity': snapshot['entity'],
                                'action': action, 'target': target})
        try:
            response = client.call(method, endpoint, submitted_body)
            if not isinstance(response, dict):
                raise SkillError('写接口未返回有效响应。')
            if action == 'create':
                created = response[spec['wrap']] if isinstance(response.get(spec['wrap']), dict) else response
                if created.get('id') is None:
                    raise SkillError('创建未返回编号，结果不明。')
                target = ident(created['id'], f'{spec["label"]}编号')
                private_write(receipt, {'status': 'inflight', 'entity': snapshot['entity'],
                                        'action': action, 'target': target})
            elif response.get('id') is not None and str(response.get('id')) != str(target):
                raise SkillError('写接口返回的编号与请求不一致，结果不明。')
            actual = detail(client, spec, target)
            # skip_verify 用于「服务端表达方式与请求形态差异过大、核对必然误报」的字段。
            keys = verification_fields(spec, action, body, snapshot['requested'])
            failed = [key for key in keys
                      if not matches(actual, body[key], key, spec['fields'][key])]
            if failed:
                raise SkillError('回查发现以下字段与请求不一致：' + '、'.join(failed) + '。')
            if action == 'update':
                drifted = drifted_fields(spec, snapshot['before'], actual,
                                         set(snapshot['requested']))
                if drifted:
                    raise SkillError('修改后以下未请求字段被改变：' + '、'.join(drifted) + '。')
        except SkillError as exc:
            private_write(receipt, {'status': 'unknown', 'entity': snapshot['entity'],
                                    'action': action, 'target': target})
            text = str(exc)
            if '403' in text or 'PERMISSION_DENIED' in text:
                raise SkillError(PERMISSION_HINT.format(label=spec['label'])
                                 + '已保留操作记录，禁止自动重发。') from None
            raise SkillError(f'{exc} 已保留收据；请只读回查，禁止自动重发。') from None
        except Exception:
            private_write(receipt, {'status': 'unknown', 'entity': snapshot['entity'],
                                    'action': action, 'target': target})
            raise SkillError('写入结果未完整核验，已保留操作记录。请只读核对禅道，禁止自动重发。') from None
        private_write(receipt, {'status': 'verified', 'entity': snapshot['entity'],
                                'action': action, 'target': target})
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
        return {'status': 'verified', 'action': ACTION_LABEL[action] + spec['label'],
                'id': target, 'url': client.base + spec['view'].format(id=target),
                'cleanup': cleanup}
