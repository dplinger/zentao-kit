"""禅道构建（Build）的写操作：新建与修改。

构建是发版流程的枢纽对象——Bug 的「解决版本」、测试单的「构建」都指向它。
写操作走 **v2**：`POST v2/builds` 创建、`PUT v2/builds/{id}` 修改、`GET v2/builds/{id}` 详情。

**为什么不用 v1**

v1（`POST v1/projects/{project}/builds`）有一处补不上的缺陷：**`system`（所属应用）写不进去**。
而 `system` 在页面上是必填项，v1 建出来的构建恒为 `system=0`，属于「未分类」，会被构建列表
过滤掉——页面上看不见、回查也查不到，于是把「已建成」误判成「没建成」，再把「没建成」误判成
「没权限」（台账 D17）。v2 把 `system` 列为必填并真正落库，从根上消除这一串误判。

**只发必填字段**

创建只提交六项：`executionID`、`product`、`name`、`system`、`builder`、`date`。
`branch`、`scmPath`、`filePath`、`desc` 均非必填，**不主动带上**——多传一个就多一处出错点。
只有用户明确要求指定分支时，才把 `branch` 原样透传。

**构建不是"版本号"的同义词**：禅道另有虚拟版本 `trunk`（主干），不在这张表里，
只能从 Bug 解决表单读到——新建构建不影响 `trunk` 的可用性。
"""

import json

from actions import (ACCOUNT, CREATE, DATE, ID, INT, TEXT,
                     execute as _execute, prepare as _prepare)
from common import SkillError

SPECS = {
    'build': {
        'label': '构建',
        'wrap': 'build',
        'collection': 'v2/builds',
        'detail': 'v2/builds/{id}',
        'view': '/build-view-{id}.html',
        # 字段名两个接口不一致（实测 + 官方文档）：创建写 executionID，修改写 execution。
        # 内部统一用 execution，只在创建发送时改名——改错了要么报错要么把执行写成 0。
        'create_wire': {'execution': 'executionID'},
        # 关闭 update_fill：实测 v2 构建的 PUT 是**局部更新**——只传 name 也能成功，
        # 其余字段原样保留。这与 v2 测试单的整份覆盖语义正好相反，照搬会把 6 个字段白传一遍。
        # 故修改时只发用户给的字段，不铺旧值。
        # 把构建挪到别的产品/执行下等于换了对象，必须逐次核对。
        'monitor_fields': ('product', 'execution'),
        'notices': {
            'create': '构建必填六项：执行、产品、名称、所属应用、构建者、打包日期。'
                      '`builder` 填登录账号，`date` 用 YYYY-MM-DD。'
                      '「所属应用」在产品只有一个候选时自动补上，有多个时会列出来让你指定。'
                      '分支、源码地址、下载地址、描述都不是必填，不指定就不提交。',
            'update': '只改你给出的字段，未指定的（产品、执行、名称、所属应用、构建者、'
                      '打包日期、描述等）保持原样，不会被清空。',
        },
        'fields': {
            'execution': {'type': ID, 'required': CREATE, 'scope': 'execution'},
            'product': {'type': ID, 'required': CREATE, 'scope': 'product'},
            'name': {'type': TEXT, 'required': CREATE},
            # 服务端必填，但候选值按产品不同，故不交给通用必填校验，由 check_build 补齐。
            'system': {'type': INT},
            'builder': {'type': ACCOUNT, 'required': CREATE},
            'date': {'type': DATE, 'required': CREATE},
            # 非必填；只在创建时允许指定，避免修改时无故带一个服务端未声明的字段。
            'branch': {'type': INT, 'ops': CREATE},
            'scmPath': {'type': TEXT},
            'filePath': {'type': TEXT},
            'desc': {'type': TEXT},
        },
    },
}


def system_options(client, product):
    """读取产品的「所属应用」候选——页面保存时用的就是这个接口。

    返回 `[{'text': '管控中心', 'value': 2}]`；取不到时返回空列表，由调用方决定怎么报错。
    """
    from auth import fetch_page
    try:
        text = fetch_page(client.session, f'/build-ajaxGetSystemList-{product}.json')
        rows = json.loads(text)
    except (SkillError, ValueError):
        return []
    options = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        try:
            options.append({'text': str(row.get('text') or ''), 'value': int(row.get('value'))})
        except (TypeError, ValueError):
            continue
    return options


def check_build(client, action, body, requested):
    """创建时补全必填的「所属应用」。

    `system` 是 v2 创建的必填项，但取值跟产品绑定（同产品可有多个应用），
    所以不放进通用必填校验，改在这里按产品查候选：唯一就直接用，多个就列出来让用户选。

    修改时不补：PUT 是局部更新，没传就保留原值，多补一个反而多一处出错点。
    """
    if action != 'create' or body.get('system'):
        return
    product = body.get('product')
    options = system_options(client, product) if product else []
    if len(options) == 1:
        body['system'] = options[0]['value']
        return
    if options:
        listed = '、'.join(f"{item['text']}={item['value']}" for item in options)
        raise SkillError(f'产品 {product} 有多个所属应用，请在 fields.system 中指定其一：{listed}。')
    raise SkillError(f'未能取到产品 {product} 的所属应用候选，请在 fields.system 中直接给出编号。'
                     '「所属应用」是必填项，缺了服务端会拒绝。')


SPECS['build']['precheck'] = check_build


def recover_create(client, body):
    """创建返回空响应时，按执行构建列表回查刚建的记录。

    v2 建出来的构建 `system` 有值，不像 v1 那样被列表过滤，所以列表里找得回来。
    命中条件：同执行 + 同名称 + 同日期，取编号最大的一条。
    """
    execution = body.get('execution')
    try:
        value = client.call('GET', f'v2/executions/{execution}/builds?limit=200')
    except SkillError:
        return None
    rows = value.get('builds') if isinstance(value, dict) else None
    name = str(body.get('name') or '')
    day = str(body.get('date') or '')[:10]
    hits = [row for row in (rows or [])
            if isinstance(row, dict)
            and str(row.get('name') or '') == name
            and str(row.get('date') or '')[:10] == day
            and str(row.get('execution')) == str(execution)]
    if not hits:
        return None
    return int(max(hits, key=lambda row: int(row.get('id') or 0))['id'])


SPECS['build']['recover_create'] = recover_create


def prepare(plan, client, entity='build', source_path=None):
    return _prepare(plan, client, SPECS[entity], entity, source_path)


def execute(review, confirmation, client):
    return _execute(review, confirmation, client, SPECS)
