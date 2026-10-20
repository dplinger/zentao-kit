"""「我的 X」查询：走「我的地盘」页面（人的视角），逐条核对归属后输出精简摘要。

与 `query` 的分工：`query --entity stories` 这类命令必须给产品编号，答不了「我的需求是
哪些」；REST 里也没有「我」这个资源（实测 `v1/v2 tasks`、`v2/my/*` 均 403）。要按人问，
只能走禅道「我的地盘」的页面数据。

⚠️ 安全前提（见 my_work.py 顶部）：非法 browseType 会让服务端**退化成返回全量**，
所以每个 mode 的 scope 都在这里**本地白名单**，再由 my_work 逐条核对归属字段。
"""

from common import SkillError, person
from my_work import collect

# 每个视图：页面 mode、可用归属（scope → 归属字段名）、结果集合键名、展示字段、详情页路径。
# scope 一律取自本表，绝不透传用户输入——实测非法值会让服务端返回别人的数据。
VIEWS = {
    'stories': {
        'label': '需求',
        'mode': 'story',
        'keys': ('stories',),
        'scopes': {'assignedTo': 'assignedTo', 'openedBy': 'openedBy',
                   'reviewedBy': 'reviewedBy', 'closedBy': 'closedBy'},
        'scope_label': {'assignedTo': '指派给我', 'openedBy': '我创建',
                        'reviewedBy': '我评审', 'closedBy': '我关闭'},
        'summary': ('id', 'title', 'product', 'productTitle', 'status', 'stage', 'pri',
                    'estimate', 'openedBy', 'assignedTo', 'reviewedBy', 'openedDate',
                    'assignedDate', 'plan', 'closedReason'),
        'path': 'story-view-{id}.html',
        'rest_hint': 'REST 没有等价的「我的需求」入口（`query --entity stories` 必须给产品编号）。',
    },
    'testtasks': {
        'label': '测试单',
        'mode': 'testtask',
        'keys': ('tasks',),
        # 实测：只有 assignedTo 有效，owner/createdBy/openedBy 及任意值都返回全部记录。
        'scopes': {'assignedTo': 'owner'},
        'scope_label': {'assignedTo': '指派给我'},
        'summary': ('id', 'name', 'product', 'productName', 'project', 'execution',
                    'build', 'buildName', 'owner', 'status', 'pri', 'begin', 'end',
                    'createdBy', 'createdDate'),
        'path': 'testtask-view-{id}.html',
        'rest_hint': '可改用 `query --entity testtasks --product <产品编号>`，但那是按产品查、不是按人。',
    },
}

DEFAULT_LIMIT = 20
MAX_LIMIT = 200


def entities():
    """列出「我的」视图与各自的归属范围，供宿主自省。"""
    return {'status': 'ok',
            'entity': 'mine',
            'message': '按人查询：`mine --entity <实体> [--scope <归属>]`。',
            'views': [{'entity': name,
                       'label': spec['label'],
                       'scopes': [{'scope': key, 'meaning': spec['scope_label'][key]}
                                  for key in spec['scopes']]}
                      for name, spec in VIEWS.items()]}


def fetch(client, entity, scope, limit, take_all):
    """读取「我的 X」列表；归属范围与条数都在本地校验后再请求。"""
    spec = VIEWS.get(entity)
    if spec is None:
        raise SkillError(f'不支持「我的」视图「{entity}」；可用：'
                         + '、'.join(f'{name}（{item["label"]}）' for name, item in VIEWS.items()) + '。')
    owner_key = spec['scopes'].get(scope)
    if owner_key is None:
        options = '、'.join(f'{key}（{spec["scope_label"][key]}）' for key in spec['scopes'])
        raise SkillError(f'{spec["label"]}支持的归属范围只有：{options}；'
                         f'「{scope}」不是可用值（服务端对未知范围会返回所有人的数据）。')
    if not take_all and (limit is None or not 1 <= limit <= MAX_LIMIT):
        raise SkillError(f'--limit 需在 1 到 {MAX_LIMIT} 之间，或用 --all 取全部。')

    rows = collect(client.session, spec['mode'], scope, spec['keys'], owner_key,
                   client.account, spec['rest_hint'])
    chosen = rows if take_all else rows[:limit]

    base = client.session['base_url'].rstrip('/')
    items = []
    for row in chosen:
        item = {key: person(row[key]) for key in spec['summary'] if key in row}
        item['url'] = f"{base}/{spec['path'].format(id=row['id'])}"
        items.append(item)

    scope_text = spec['scope_label'][scope]
    notes = [f'归属范围：{scope_text}']
    if not items:
        notes.append('该范围内没有记录')
    elif len(rows) > len(items):
        notes.append(f'共 {len(rows)} 条，仅显示前 {len(items)} 条（可用 --limit 或 --all 取更多）')
    return {'status': 'ok' if items else 'empty',
            'entity': entity, 'label': spec['label'], 'scope': scope,
            'scope_meaning': scope_text,
            'total': len(rows), 'count': len(items), 'items': items,
            'message': '；'.join(notes) + '。'}
