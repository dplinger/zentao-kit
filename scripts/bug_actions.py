"""禅道 Bug 查询与操作：创建、修改、确认用 v1，状态流转用 v2。"""

import datetime as dt
import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re

from auth import fetch_page, upload
from common import (actions_dir, SkillError, account, digest, missing_error, move_to_trash,
                    private_write, read_json, state_dir)

TYPES = {'codeerror', 'config', 'install', 'security', 'performance', 'standard', 'automation', 'designdefect', 'others'}
RESOLUTIONS = {'bydesign', 'duplicate', 'external', 'fixed', 'notrepro', 'postponed', 'willnotfix', 'tostory'}
EDIT_FIELDS = {'title', 'severity', 'pri', 'type', 'openedBuild', 'steps', 'project', 'execution', 'story', 'assignedTo'}
FIELDS = {
    # 创建走 v1 接口，产品字段名为 product（v2 用 productID，实测创建时会落空）。
    'create': EDIT_FIELDS | {'product'},
    'update': EDIT_FIELDS,
    'confirm': {'assignedTo', 'type', 'mailto', 'comment', 'pri'},
    'close': {'comment'},
    'activate': {'assignedTo', 'openedBuild', 'comment'},  # openedBuild 必填。
    'resolve': {'resolution', 'resolvedBuild', 'resolvedDate', 'assignedTo', 'comment'},
}
STATUS = {'create': 'active', 'close': 'closed', 'activate': 'active', 'resolve': 'resolved'}
# 附件走 v1/files（字段 imgFile）上传，返回文件编号后按站点编辑器的写法内嵌到重现步骤。
# 图片类型以文件头为准：粘贴的截图常被宿主存成 .jpg/.jfif，扩展名不一定可靠。
IMAGE_MAGIC = ((b'\x89PNG\r\n\x1a\n', 'png', 'image/png'),
               (b'\xff\xd8\xff', 'jpg', 'image/jpeg'),
               (b'GIF87a', 'gif', 'image/gif'),
               (b'GIF89a', 'gif', 'image/gif'))
MAX_IMAGE_BYTES = 5 * 1024 * 1024
UPLOAD_FIELD = 'imgFile'
ATTACH_ACTIONS = {'create', 'update'}
# 站点会重置未提供的字段（实测修改时 assignedTo 被清空），这些字段在修改后不应无故变化。
MONITORED = EDIT_FIELDS | {'product', 'status', 'confirmed'}


class PlainText(HTMLParser):
    """Bug 重现步骤保留文字与图片引用，不执行页面内容。"""
    def __init__(self):
        super().__init__()
        self.parts, self.images, self.hidden = [], [], 0

    def handle_starttag(self, tag, attrs):
        self.hidden += tag in ('script', 'style')
        if tag in ('p', 'div', 'br', 'li', 'tr'):
            self.parts.append('\n')
        if tag == 'img':
            self.images.append(dict(attrs).get('src', ''))

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def redact(value):
    """避免 Bug 原文中的常见凭据进入模型输出或预览。"""
    text = str(value or '')
    text = re.sub(r'(?i)(\b(?:x-api-key|api[-_]?key|authorization|token|password|cookie)\b\s*[:=]\s*[\"\']?)([^\s\"\';,]+)',
                  r'\1[已脱敏]', text)
    return re.sub(r'\bsk-[A-Za-z0-9_-]{12,}\b', '[已脱敏]', text)


def ident(value):
    if isinstance(value, bool) or not str(value).isdigit() or int(value) < 1:
        raise SkillError('Bug 或产品编号必须为正整数。')
    return int(value)


def sniff_image(content):
    """按文件头判断真实图片类型，返回 (后缀, MIME)；粘贴的截图扩展名未必可靠。"""
    for magic, suffix, mime in IMAGE_MAGIC:
        if content.startswith(magic):
            return suffix, mime
    return None


def read_attachments(paths):
    """只读校验本地图片并生成摘要；不复制、不改名、不写入用户目录。"""
    if not isinstance(paths, list) or not paths:
        raise SkillError('attachments 必须是非空的图片路径数组。')
    result = []
    for raw in paths:
        if not isinstance(raw, str) or not raw.strip():
            raise SkillError('附件路径必须是非空字符串。')
        path = Path(raw).expanduser()
        if not path.is_file():
            raise SkillError(f'附件不存在或不是文件：{path.name}')
        content = path.read_bytes()
        if not content:
            raise SkillError(f'附件是空文件：{path.name}')
        if len(content) > MAX_IMAGE_BYTES:
            raise SkillError(f'附件超过 {MAX_IMAGE_BYTES // 1048576}MB：{path.name}')
        kind = sniff_image(content)
        if not kind:
            raise SkillError(f'不是支持的图片格式（PNG/JPG/GIF）：{path.name}')
        suffix, mime = kind
        result.append({'path': str(path.resolve()), 'name': path.name,
                       'upload_name': (Path(path.name).stem or 'image') + '.' + suffix,
                       'bytes': len(content), 'suffix': suffix, 'content_type': mime,
                       'sha256': hashlib.sha256(content).hexdigest()})
    return result


def attachment_payload(metadata):
    """写入前重读附件并与预览摘要比对，避免预览后被替换。"""
    result = []
    for meta in metadata or []:
        path = Path(meta['path'])
        if not path.is_file():
            raise SkillError(f'附件已不存在：{meta["name"]}')
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != meta['sha256']:
            raise SkillError(f'附件内容已变化，请重新预览：{meta["name"]}')
        result.append((meta, content))
    return result


def merge_images(steps, embedded):
    """内嵌沿用站点编辑器写法（浏览器实测可显示）；{imgN} 指定位置，否则追加到末尾。"""
    text = str(steps or '')
    for index, (file_id, url, suffix) in enumerate(embedded, 1):
        name = 'file-read-' + str(file_id) + '.' + suffix
        tag = ('<p><img onload="setImageSize(this,0)" src="' + url + '"'
               + ' alt="' + name + '" /></p>')
        placeholder = '{img' + str(index) + '}'
        text = text.replace(placeholder, tag) if placeholder in text else text + tag
    return re.sub(r'\{img\d+\}', '', text)


def detail(client, bug_id, include_actions=False):
    """读取完整对象供校验；未知结构不能当作不存在。"""
    bug_id = ident(bug_id)
    try:
        response = client.call('GET', f'v2/bugs/{bug_id}')
    except SkillError as exc:
        raise (missing_error(exc, 'Bug', bug_id) or exc) from None
    if not isinstance(response, dict) or response.get('status') != 'success':
        raise SkillError('Bug 详情接口未返回成功状态。')
    value = response.get('bug')
    if not isinstance(value, dict) or ident(value.get('id')) != bug_id:
        raise SkillError('Bug 详情响应格式不匹配。')
    if any(key not in value for key in ('product', 'title', 'status', 'confirmed')):
        raise SkillError('Bug 详情缺少必要字段。')
    # v2 把操作历史放在外层；只记录数量，避免把评论中的敏感信息带入预览。
    result = {**value, '_actions_count': len(response.get('actions') or [])}
    if include_actions:
        result['_actions'] = response.get('actions') or []
    return result


def view(client, bug_id):
    value = detail(client, bug_id, include_actions=True)
    parser = PlainText()
    parser.feed(str(value.get('steps') or ''))
    return {'id': ident(value['id']), 'title': redact(value['title']), 'product': value['product'],
            'project': value.get('project'), 'execution': value.get('execution'),
            'status': value['status'], 'confirmed': value['confirmed'], 'assignedTo': account(value.get('assignedTo')),
            'severity': value.get('severity'), 'pri': value.get('pri'), 'type': value.get('type'),
            'resolution': value.get('resolution'), 'resolvedBuild': value.get('resolvedBuild'),
            'description': redact(''.join(parser.parts).strip()), 'images': [redact(src) for src in parser.images],
            'files_count': len(value.get('files') or []), 'actions_count': value['_actions_count'],
            'actions': [{'action': row.get('action'), 'actor': account(row.get('actor')),
                         'date': row.get('date'), 'comment': redact(row.get('comment'))}
                        for row in value['_actions'] if isinstance(row, dict)],
            'url': client.base + f'/bug-view-{bug_id}.html'}


def _rows(value, key):
    """从响应中取出列表；未知结构不能被当成空列表。"""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for candidate in (key, 'data'):
            if candidate in value:
                return _rows(value[candidate], key)
        if value and all(str(k).isdigit() and isinstance(v, dict) for k, v in value.items()):
            return list(value.values())
    raise SkillError(f'响应缺少 {key} 列表，当前操作已停止。')


def builds(client, project=None, execution=None):
    """读取项目或执行的构建列表。

    构建接口（v1/projects/{id}/builds、v1/executions/{id}/builds）只返回真实构建，
    不含禅道的虚拟版本 trunk（主干）；trunk 仅出现在网页解决表单里。
    因此解决 Bug 时若需完整合法取值，应配合 resolved_build_options 使用。
    """
    if (project is None) == (execution is None):
        raise SkillError('构建查询需且仅需指定项目或执行之一。')
    path = (f'v1/projects/{ident(project)}/builds' if project is not None
            else f'v1/executions/{ident(execution)}/builds')
    response = client.call('GET', f'{path}?limit=200')
    if not isinstance(response, dict):
        raise SkillError('构建列表响应格式不匹配。')
    rows = _rows(response, 'builds')
    result = []
    for row in rows:
        if not isinstance(row, dict) or 'id' not in row or 'name' not in row:
            raise SkillError('构建记录缺少编号或名称，无法可靠取值。')
        result.append({'id': ident(row['id']), 'name': str(row['name']),
                       'product': ident(row.get('product', 0)), 'execution': ident(row.get('execution', 0)),
                       'date': str(row.get('date') or '')[:10]})
    return result


def resolved_build_options(client, bug_id):
    """从 Bug 解决表单读取『解决版本』的合法取值，这是唯一含 trunk 的权威来源。

    返回按表单顺序排列的选项，并把 Bug 当前 openedBuild/resolvedBuild 标注出来，
    供预览直接推荐：优先与 openedBuild 一致，否则回退 trunk。
    """
    bug_id = ident(bug_id)
    value = detail(client, bug_id)
    page = fetch_page(client.session, f'/bug-resolve-{bug_id}.html')
    match = re.search(r'name":"resolvedBuild","items":(\[.*?\])', html.unescape(page))
    if not match:
        raise SkillError('未能从解决表单读取解决版本选项，请确认页面结构未变更。')
    try:
        items = json.loads(match.group(1))
    except ValueError:
        raise SkillError('解决版本选项结构解析失败。')
    options = [{'value': str(it['value']), 'text': str(it['text'])} for it in items
               if isinstance(it, dict) and 'value' in it and 'text' in it]
    if not options:
        raise SkillError('解决版本选项为空，无法继续。')
    opened = str(value.get('openedBuild') or '').strip()
    values = [it['value'] for it in options]
    recommended = opened if opened in values else ('trunk' if 'trunk' in values else values[0])
    return {'bug': bug_id, 'openedBuild': opened,
            'resolvedBuild': str(value.get('resolvedBuild') or '').strip(),
            'recommended': recommended, 'options': options}


def bug_list(client, scope, target):
    """逐页读取产品、项目或执行 Bug，核对总数、页码和重复编号。"""
    if scope not in ('products', 'projects', 'executions'):
        raise SkillError('Bug 列表范围不支持。')
    target = ident(target)
    key = {'products': 'product', 'projects': 'project', 'executions': 'execution'}[scope]
    result, seen, total = [], set(), None
    for page in range(1, 1001):
        value = client.call('GET', f'v2/{scope}/{target}/bugs?browseType=all&recPerPage=100&pageID={page}')
        if not isinstance(value, dict) or value.get('status') != 'success' or not isinstance(value.get('bugs'), list):
            raise SkillError('Bug 列表响应格式不匹配。')
        try:
            pager = value['pager']
            actual, count, limit = (int(pager[k]) for k in ('pageID', 'recTotal', 'recPerPage'))
        except (KeyError, TypeError, ValueError):
            raise SkillError('Bug 分页信息不完整。') from None
        if actual != page or count < 0 or limit < 1 or (total is not None and count != total):
            raise SkillError('Bug 分页发生变化。')
        total = count
        rows = value['bugs']
        if len(rows) != min(limit, max(0, total - len(result))):
            raise SkillError('Bug 分页数量不完整。')
        for row in rows:
            if not isinstance(row, dict) or int(row.get(key, 0)) != target:
                raise SkillError('Bug 列表含有其他范围的记录。')
            number = ident(row.get('id'))
            if number in seen:
                raise SkillError('Bug 分页存在重复编号。')
            seen.add(number)
            result.append({'id': number, 'title': redact(row.get('title')), 'status': row.get('status'),
                           'assignedTo': account(row.get('assignedTo')), 'severity': row.get('severity'),
                           'pri': row.get('pri'), 'url': client.base + f'/bug-view-{number}.html'})
        if len(result) == total:
            return {'status': 'ok', 'scope': scope, 'target': target, 'total': total, 'bugs': result}
    raise SkillError('Bug 分页超出读取上限。')


def find_products(client, name):
    """分页查找产品名称，返回编号供只读 Bug 查询使用。"""
    if not isinstance(name, str) or not name.strip():
        raise SkillError('请提供产品名称。')
    matches, seen, expected = [], set(), None
    for page in range(1, 1001):
        value = client.call('GET', f'v2/products?browseType=all&recPerPage=100&pageID={page}')
        if not isinstance(value, dict) or value.get('status') != 'success' or not isinstance(value.get('products'), list):
            raise SkillError('产品列表响应格式不匹配。')
        pager = value.get('pager') or {}
        try:
            total = int(pager.get('recTotal', value.get('total'))) if pager.get('recTotal', value.get('total')) is not None else None
            if total is not None and (total < 0 or expected is not None and total != expected):
                raise ValueError()
            if 'pageID' in pager and int(pager['pageID']) != page:
                raise ValueError()
        except (TypeError, ValueError):
            raise SkillError('产品分页信息不一致。') from None
        expected = total
        rows = value['products']
        if total is not None and len(rows) != min(100, max(0, total - len(seen))):
            raise SkillError('产品分页数量不完整。')
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('name'), str):
                raise SkillError('产品记录格式不匹配。')
            number = ident(row.get('id'))
            if number in seen:
                raise SkillError('产品分页存在重复编号。')
            seen.add(number)
            if name.casefold() in row['name'].casefold():
                matches.append({'id': number, 'name': redact(row['name'])})
        if total is not None and len(seen) == total or total is None and len(rows) < 100:
            return {'status': 'ok', 'query': name, 'matches': matches}
    raise SkillError('产品分页超出读取上限。')


def validate(action, fields):
    """只允许所选版本文档列出的字段；不提供删除路由。"""
    if action not in FIELDS:
        raise SkillError('不支持该 Bug 操作。')
    if not isinstance(fields, dict) or set(fields) - FIELDS[action]:
        raise SkillError('Bug 操作包含不支持的字段。')
    for key in ('title', 'type', 'assignedTo', 'comment', 'steps', 'resolution'):
        if key in fields and (not isinstance(fields[key], str) or not fields[key].strip() and key not in ('comment', 'steps')):
            raise SkillError(f'{key} 字段格式不正确。')
    if 'type' in fields and fields['type'] not in TYPES:
        raise SkillError('Bug 类型不在官方支持范围内。')
    if 'resolution' in fields and fields['resolution'] not in RESOLUTIONS:
        raise SkillError('Bug 解决方案不在官方支持范围内。')
    for key in ('severity', 'pri'):
        if key in fields and (isinstance(fields[key], bool) or fields[key] not in (1, 2, 3, 4)):
            raise SkillError(f'{key} 必须是 1 至 4。')
    for key in ('product', 'project', 'execution', 'story'):
        if key in fields and (isinstance(fields[key], bool) or not str(fields[key]).isdigit()):
            raise SkillError(f'{key} 必须是非负整数。')
    for key in ('openedBuild', 'mailto'):
        if key in fields and (not isinstance(fields[key], list) or not all(isinstance(x, (str, int)) and not isinstance(x, bool) for x in fields[key])):
            raise SkillError(f'{key} 必须是数组。')
    for key in ('deadline', 'resolvedDate'):
        if fields.get(key):
            dt.date.fromisoformat(fields[key])
    if action == 'create' and (not fields.get('title') or not fields.get('product') or not fields.get('openedBuild')):
        raise SkillError('创建 Bug 必须指定产品编号、标题和影响版本。')
    if action == 'resolve' and not fields.get('resolution'):
        raise SkillError('解决 Bug 必须指定解决方案。')
    if action == 'activate' and not fields.get('openedBuild'):
        raise SkillError('重新激活 Bug 必须指定影响版本 openedBuild。')
    if action == 'resolve' and fields.get('resolution') == 'duplicate':
        raise SkillError('v2 解决接口文档未提供重复 Bug 编号字段，暂不能选择 duplicate。')
    if action == 'update' and not fields:
        raise SkillError('修改 Bug 至少指定一个字段。')


def update_notice(action, requested, before):
    """提醒修改时会重置的字段，避免用户误以为未提供的字段会被保留。"""
    if action != 'update' or not before:
        return None
    risky = [key for key in ('assignedTo',) if key not in requested and before.get(key) not in (None, '')]
    if not risky:
        return None
    return '站点会重置未提供的字段（实测 ' + '、'.join(risky) + ' 会被清空）；如需保持请一并写入 fields。'


def attachment_notice(attachments, notice):
    """把附件提示并入预览提醒，说明写入顺序与失败影响。"""
    if not attachments:
        return notice
    names = '、'.join(item['name'] for item in attachments)
    hint = f'{len(attachments)} 张图片（{names}）将先上传、再内嵌到重现步骤'
    return notice + ' ' + hint + '。' if notice else hint + '；上传失败会立即停止，不会写入 Bug。'


def prepare(plan, client, source_path=None):
    """绑定账号及远端版本生成审阅摘要，绝不写站点。"""
    action = plan.get('action')
    if action not in FIELDS:
        raise SkillError('Bug 操作仅支持 create/update/confirm/close/activate/resolve。')
    requested = plan.get('fields')
    validate(action, requested)
    attachments = read_attachments(plan.get('attachments')) if plan.get('attachments') else []
    if attachments and action not in ATTACH_ACTIONS:
        raise SkillError('只有创建或修改 Bug 支持附带图片。')
    bug_id = None if action == 'create' else ident(plan.get('bug'))
    before = None if bug_id is None else detail(client, bug_id)
    # 解决版本为服务端必填但网页显示可选，缺失时按 openedBuild 优先、trunk 兜底的规则自动补齐。
    build_notice = ''
    if action == 'resolve' and not requested.get('resolvedBuild') and bug_id is not None:
        picker = resolved_build_options(client, bug_id)
        requested['resolvedBuild'] = picker['recommended']
        build_notice = (f"解决版本未指定，已按规则自动选为 {picker['recommended']}"
                        f"（Bug 提交版本 {picker['openedBuild'] or '空'}，可选 "
                        + '、'.join(it['value'] for it in picker['options']) + '）；如需更改请指定。')
    if action == 'create':
        product = ident(requested['product'])
        body = requested.copy()
    else:
        product = ident(before['product'])
        status = str(before['status'])
        if action in ('confirm', 'resolve') and status != 'active':
            raise SkillError('仅能确认或解决激活状态的 Bug。')
        if action == 'confirm' and str(before['confirmed']).lower() not in ('0', 'false', ''):
            raise SkillError('该 Bug 已确认，无需重复确认。')
        if action == 'close' and status != 'resolved':
            raise SkillError('仅能关闭已解决的 Bug。')
        if action == 'activate' and status not in ('resolved', 'closed'):
            raise SkillError('仅能重新激活已解决或已关闭的 Bug。')
        # 修改走 v1，也只发送用户指定的字段，避免覆盖其他已有信息。
        body = requested.copy()
    snapshot = {'action': action, 'bug': bug_id, 'product': product, 'account': client.account,
                'base_url': client.base, 'body': body, 'requested': requested,
                'before_digest': digest(before) if before else None,
                'before_status': before.get('status') if before else None,
                'before_confirmed': before.get('confirmed') if before else None,
                'attachments': attachments}
    if source_path:
        snapshot.update(source_path=str(Path(source_path).resolve()), source_digest=digest(plan))
    review = digest(snapshot)
    private_write(actions_dir() / f'{review}.json', snapshot)
    return {'status': 'preview_only', 'review': review, 'action': action, 'bug': bug_id,
            'product': product, 'title': redact(before.get('title') if before else requested.get('title')),
            'before_status': snapshot['before_status'],
            'after_status': STATUS.get(action, snapshot['before_status']),
            'before_fields': {k: redact(before.get(k)) if isinstance(before.get(k), str) else before.get(k)
                              for k in requested if before and k not in ('comment', 'mailto')},
            'changes': {k: redact(v) if isinstance(v, str) else v for k, v in requested.items()},
            'attachments': [{'name': item['name'], 'bytes': item['bytes'],
                             'sha256': item['sha256'][:8]} for item in attachments],
            'notice': ' '.join(x for x in (attachment_notice(attachments, update_notice(action, requested, before)), build_notice) if x),
            'message': '请核对 Bug 操作；确认无误后回复“确认执行”。'}


def same(actual, expected, key):
    if key in ('assignedTo',):
        return account(actual.get(key)) == expected
    if key in ('title', 'steps'):
        return html.unescape(str(actual.get(key) or '')).strip() == html.unescape(str(expected or '')).strip()
    if key == 'openedBuild':
        actual_builds = {item.strip() for item in str(actual.get(key) or '').split(',') if item.strip()}
        expected_builds = {str(item) for item in expected} if isinstance(expected, list) else {str(expected)}
        return actual_builds == expected_builds
    if key in ('resolvedDate', 'deadline'):
        return str(actual.get(key) or '')[:10] == str(expected or '')[:10]
    return str(actual.get(key) or '') == str(expected or '')


def execute(review, confirmation, client):
    """单次发送后读详情回查；结果不明保留收据且禁止重发。"""
    from zentao import submission_lock
    if not re.fullmatch(r'[a-f0-9]{64}', review) or review != confirmation:
        raise SkillError('缺少与 Bug 预览一致的确认。')
    path = actions_dir() / f'{review}.json'
    receipt = path.with_suffix('.receipt.json')
    with submission_lock():
        if not path.exists() or receipt.exists():
            raise SkillError('Bug 预览不存在或该操作已尝试执行，禁止重复提交。')
        snapshot = read_json(path)
        if digest(snapshot) != review or snapshot['account'] != client.account or snapshot['base_url'] != client.base:
            raise SkillError('Bug 预览、账号或站点已变化。')
        if snapshot.get('source_path') and digest(read_json(snapshot['source_path'])) != snapshot['source_digest']:
            raise SkillError('Bug 草稿已修改，请重新预览。')
        action, bug_id, body = snapshot['action'], snapshot['bug'], snapshot['body']
        before = None
        if bug_id:
            before = detail(client, bug_id)
            if digest(before) != snapshot['before_digest']:
                raise SkillError('Bug 自预览后发生变化，请重新预览。')
        requested = dict(snapshot['requested'])
        # 实测 v2 修改接口对 assignedTo 等字段会静默失败（返回空体、不落库）；
        # 创建、修改、确认统一走 v1，仅状态流转用 v2 子路由。
        if action in ('create', 'update', 'confirm'):
            endpoint = 'v1/bugs' if action == 'create' else f'v1/bugs/{bug_id}'
        else:
            endpoint = f'v2/bugs/{bug_id}'
        if action in ('confirm', 'close', 'activate', 'resolve'):
            endpoint += '/' + action
        # 图片先上传再写 Bug：上传失败时站点只多出孤立文件，不会留下半成品记录。
        uploaded = []
        for meta, content in attachment_payload(snapshot.get('attachments')):
            # 用嗅探出的真实类型命名并标注 MIME，避免宿主缓存扩展名（.jfif 等）误导服务端。
            response = upload(client.base, 'v1/files', client.token, UPLOAD_FIELD,
                              meta['upload_name'], content, content_type=meta['content_type'])
            file_id = response.get('id') if isinstance(response, dict) else None
            if not str(file_id or '').isdigit():
                raise SkillError('附件上传未返回文件编号，请核对站点接口。')
            uploaded.append({'id': int(file_id), 'name': meta['name'],
                             'url': str(response.get('url') or ''), 'suffix': meta['suffix']})
        if uploaded:
            body['steps'] = merge_images(body.get('steps') or (before or {}).get('steps'),
                                         [(item['id'], item['url'], item['suffix']) for item in uploaded])
            requested['steps'] = body['steps']
        private_write(receipt, {'status': 'inflight', 'action': action, 'bug': bug_id,
                                'attachments': uploaded})
        try:
            response = client.call('POST' if action in ('create', 'confirm') else 'PUT', endpoint, body)
            if not isinstance(response, dict):
                raise SkillError('Bug 写接口未返回有效响应。')
            if action in ('create', 'update'):
                # v1 接口直接返回 Bug 对象，其 status 是 Bug 状态（active 等）而非接口状态；
                # 统一以是否返回 id 作为成功依据。
                if 'id' not in response:
                    raise SkillError('Bug 写接口未返回成功状态。')
            elif action != 'confirm' and response.get('status') != 'success':
                raise SkillError('Bug 写接口未返回成功状态。')
            if action == 'confirm' and ident(response.get('id')) != bug_id:
                raise SkillError('确认 Bug 响应编号不匹配。')
            if action == 'create':
                bug_id = ident(response.get('id'))
                private_write(receipt, {'status': 'inflight', 'action': action, 'bug': bug_id})
            actual = detail(client, bug_id)
            valid = (ident(actual['product']) == snapshot['product'])
            if action in STATUS:
                valid = valid and actual['status'] == STATUS[action]
            if action == 'confirm':
                valid = valid and str(actual['confirmed']).lower() not in ('0', 'false', '')
            if action in ('create', 'update'):
                valid = valid and all(same(actual, value, key) for key, value in requested.items()
                                      if key != 'product')
            if action == 'update':
                # 只发部分字段时，站点会把未提供的字段重置；逐项核对未请求字段是否被改动。
                drifted = [key for key in sorted(MONITORED)
                           if key not in requested
                           and (before or {}).get(key) not in (None, '')
                           and not same(actual, (before or {})[key], key)]
                if drifted:
                    raise SkillError('修改 Bug 后以下未请求字段被改变：' + '、'.join(drifted) + '。')
            if uploaded:
                # 内嵌图片以 fileID 出现在重现步骤里，逐张核对，避免只写了一半。
                steps_now = str(actual.get('steps') or '')
                missing = [item['name'] for item in uploaded if f'fileID={item["id"]}' not in steps_now]
                if missing:
                    raise SkillError('内嵌图片未出现在重现步骤：' + '、'.join(missing))
            if action == 'resolve':
                valid = valid and actual.get('resolution') == body['resolution']
            if action in ('confirm', 'activate', 'resolve'):
                valid = valid and all(same(actual, value, key) for key, value in body.items()
                                      if key not in ('comment', 'mailto', 'resolution'))
            if not valid:
                raise SkillError('写后 Bug 状态与预览不一致。')
        except SkillError as exc:
            # 让字段漂移、状态不符等具体原因直接呈现，而不是被笼统的“结果不明”盖住。
            private_write(receipt, {'status': 'unknown', 'action': action, 'bug': bug_id})
            raise SkillError(f'{exc} 已保留收据；请只读回查，禁止自动重发。') from None
        except Exception:
            private_write(receipt, {'status': 'unknown', 'action': action, 'bug': bug_id})
            raise SkillError('Bug 写入结果未完整核验，已保留收据；请只读回查，禁止自动重发。') from None
        private_write(receipt, {'status': 'verified', 'action': action, 'bug': bug_id})
        cleanup = 'cleaned'
        try:
            # 只清理技能运行目录中的计划；用户提供的外部文件保持原样。
            source = Path(snapshot.get('source_path', ''))
            root = state_dir().resolve()
            if (source.name == 'plan.json' and re.fullmatch(r'[a-f0-9]{32}', source.parent.name)
                    and source.parent.parent == root / 'runs' and not source.is_symlink()
                    and source.resolve().parent.parent == root / 'runs'):
                move_to_trash(source.parent, root)
            move_to_trash(path, state_dir())
            move_to_trash(receipt, state_dir())
        except SkillError:
            cleanup = 'pending'
        return {'status': 'verified', 'action': action, 'bug': bug_id,
                'url': client.base + f'/bug-view-{bug_id}.html', 'cleanup': cleanup}
