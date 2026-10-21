"""禅道测试线的写操作：测试单创建与修改、用例创建与修改。

流程骨架在 `actions.py`，本模块只声明字段表与测试单特有的关联预检。

**五条与别处不同的接口行为（都不是笔误）**

1. **测试单的修改端点只在 v2**：v1 条目（`api/v1/entries/testtask.php`）只实现
   `get()` 与 `delete()`，**`PUT v1/testtasks/{id}` 返回 200 且响应为空**——静默假成功，
   比 4xx 更容易骗过人。真接口是 `PUT v2/testtasks/{id}`（官方 v2 文档 3.18.2），
   返回 `{"status":"success","message":"保存成功"}`。故本模块用 v2 改、用 v1 读。
2. **v2 的修改是整份覆盖语义**：`name`、`build`、`begin`、`end` 四项必须齐全
   （只传 name 会被拒：『开始日期』不能为空），说明服务端**不会**用旧值兜底。
   因此 `update_fill` 生效——用户没给的必填字段自动沿用当前值。
3. **测试单的 `project` 只认路径参数**（源码用 `$this->param('project')` 读路径或 query），
   放 JSON body 里会被忽略并报 "need project id!"——与执行创建同一个坑。
4. **用例的 `product` 必须放 body**：`POST v1/testcases` 可用，而路径式
   `POST v1/products/{id}/testcases` 返回 **403**（两个路由指向同一个条目类，权限判定不同）。
5. **`status=closed` 对测试单是死状态**：PUT 成 `closed` 后，记录会从所有 v1 列表里消失、
   页面状态列渲染异常，且 `closedBy` / `closedDate` / `realFinishedDate` 都无法通过 REST 写入
   （传了也返回「保存成功」，实际被静默忽略）。**测试单的「关闭」是 `status=done`**——页面显示
   「已关闭」、记录仍在列表内，`realFinishedDate` 为空也不影响显示。故 `status` 的合法值刻意
   不含 `closed`。同理，开始是 `doing`、重新打开是 `wait`，都不需要额外的动作路由。

⚠️ **用例的写权限**：开发所用账号对用例的创建与修改**都返回 403**。注意
`POST v1/testcases` 缺 product 时返回的是 400「Need product id.」——**必填校验先于权限检查**，
只看这个 400 会误判成"有权限"。补上 product 后才是 403。

**测试单的创建与修改均已真实跑通**：创建走 v1、修改走 v2，回查字段一致。
"""

from actions import (ACCOUNT, BOTH, CREATE, DATE, ENUM, ID, INT, STEPS, TEXT, TEXTS, UPDATE,
                     execute as _execute, prepare as _prepare)
from bug_actions import builds as list_builds
from common import SkillError

SPECS = {
    'testtask': {
        'label': '测试单',
        'wrap': 'testtask',
        'collection': 'v1/projects/{project}/testtasks',
        'path_args': ('project',),
        'detail': 'v1/testtasks/{id}',
        # v1 没有 PUT（返回 200 空响应，假成功），真接口在 v2。
        'update_endpoint': 'v2/testtasks/{id}',
        # v2 的 PUT 是整份覆盖：必填项缺一即报错，未传的非必填项会被**静默清空**，故用详情铺满。
        'update_fill': True,
        # 补齐的旧值同样属于本次覆盖内容，写后必须逐项确认没有被服务端改写或清空。
        'verify_full_body': True,
        # 关联与负责人是测试单最关键的归属信息，额外作为未请求字段检查的明确契约。
        'monitor_fields': ('project', 'product', 'execution', 'build', 'owner'),
        # v2 文档把产品写成 productID（实测 v1 的 product 也认，但以文档为准更稳）。
        'update_wire': {'product': 'productID'},
        'view': '/testtask-view-{id}.html',
        'notices': {'create': '测试单必须指定项目、产品、执行与构建，且执行要属于该项目并已关联该产品、'
                              '构建要属于该产品；这些关联在预览阶段会先核对。',
                    'update': '修改走 v2 接口，是整份覆盖语义——没传的字段会被清空，因此脚本会自动沿用'
                              '当前记录里的值，你只需给出要改的项。`execution` 可留空；`status` 改成 '
                              '`done` 即关闭测试单（开始用 `doing`、重新打开用 `wait`）；`type` 可传字符串或数组。'},
        'fields': {
            'project': {'type': ID, 'required': BOTH, 'scope': 'project'},
            'product': {'type': ID, 'required': BOTH, 'scope': 'product'},
            # 执行可空（v2 不传会清成 0，不报错），故只要求创建时给。
            'execution': {'type': ID, 'required': CREATE, 'scope': 'execution'},
            'build': {'type': ID, 'required': BOTH},
            'name': {'type': TEXT, 'required': BOTH},
            'begin': {'type': DATE, 'required': BOTH},
            'end': {'type': DATE, 'required': BOTH},
            'owner': {'type': ACCOUNT},
            'type': {'type': TEXTS},
            'pri': {'type': ENUM, 'values': ('1', '2', '3', '4')},
            # v2 文档给出的取值；服务端对非法值会拒绝，本地先拦一道。
            # 刻意不含 closed：它对测试单是死状态（记录从所有列表消失、页面渲染异常），关闭用 done。
            'status': {'type': ENUM, 'values': ('wait', 'doing', 'done', 'blocked'),
                       'hint': '测试单没有 closed——写入 closed 会让记录从所有列表消失且页面显示异常，'
                               '关闭请用 done。'},
            'desc': {'type': TEXT},
        },
    },
    'testcase': {
        'label': '用例',
        'wrap': 'testcase',
        'collection': 'v1/testcases',
        'detail': 'v1/testcases/{id}',
        'view': '/testcase-view-{id}.html',
        # 用例步骤：接口对步骤字段的命名在版本间不一致，回查不逐条比对，避免误报。
        'skip_verify': ('steps',),
        # 只保护产品、模块和需求关联；普通内容字段由本次请求核验即可。
        'monitor_fields': ('product', 'module', 'story'),
        'notices': {'create': '用例必须给产品、标题、类型、优先级和步骤；product 走请求体'
                              '（路径式会被判成无权）。',
                    'update': '提交的是整份步骤：给出 steps 会整体替换原步骤，未给出则保持原样。'},
        'fields': {
            'product': {'type': ID, 'required': CREATE, 'scope': 'product'},
            'title': {'type': TEXT, 'required': CREATE},
            # 常见取值 feature/performance/interface/install/security/other，不硬校验以兼容不同发行版
            'type': {'type': TEXT, 'required': CREATE},
            'pri': {'type': ENUM, 'values': ('1', '2', '3', '4'), 'required': CREATE},
            'steps': {'type': STEPS, 'required': CREATE},
            'module': {'type': ID},
            'stage': {'type': TEXT},
            'story': {'type': ID},
            'precondition': {'type': TEXT},
            'branch': {'type': INT, 'ops': UPDATE},
            'script': {'type': TEXT, 'ops': UPDATE},
            'keywords': {'type': TEXT, 'ops': UPDATE},
        },
    },
}


LINK_FIELDS = ('project', 'product', 'execution', 'build')


def check_testtask(client, action, body, requested):
    """预览阶段核对测试单的关联约束。

    接口对不匹配的组合只回一句 `error execution id!` / `error build id!`，用户无法定位；
    而这两种关系是测试单能否建成的关键，故先自己查清：
    执行必须属于该项目、且已关联该产品；构建必须属于该项目、且属于该产品。

    修改时只在**动了关联字段**时才重查——历史测试单未必还满足当年的约束，
    否则改个名字都会被拦住。
    """
    if action == 'update' and not (set(LINK_FIELDS) & requested):
        return
    project, product = body['project'], body['product']
    execution = body['execution']
    value = client.call('GET', f'v1/executions/{execution}')
    if isinstance(value, dict) and isinstance(value.get('execution'), dict):
        value = value['execution']
    if not isinstance(value, dict) or str(value.get('id')) != str(execution):
        raise SkillError('执行详情格式不匹配，已停止。')
    if str(value.get('project')) != str(project):
        raise SkillError(f'执行 {execution} 不属于项目 {project}；测试单必须把执行指定在同一项目下。')
    linked = {int(item['id']) for item in (value.get('products') or [])
              if isinstance(item, dict) and item.get('id')}
    if product not in linked:
        raise SkillError(f'执行 {execution} 没有关联产品 {product}；'
                         '请先在产品或执行的关联设置里建立关系，再提测试单。')

    build = body['build']
    rows = list_builds(client, project=project)
    row = next((item for item in rows if item['id'] == build), None)
    if row is None:
        raise SkillError(f'项目 {project} 下没有编号 {build} 的构建；测试单必须关联该项目的构建。')
    if row.get('product') and row['product'] != product:
        raise SkillError(f'构建 {build} 属于产品 {row["product"]}，与所选产品 {product} 不一致。')


SPECS['testtask']['precheck'] = check_testtask


def prepare(plan, client, entity, source_path=None):
    return _prepare(plan, client, SPECS[entity], entity, source_path)


def execute(review, confirmation, client):
    return _execute(review, confirmation, client, SPECS)
