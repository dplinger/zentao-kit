"""禅道构建（Build）的写操作：新建与修改。

构建是发版流程的枢纽对象——Bug 的「解决版本」、测试单的「构建」都指向它。
接口来自开源版 `api/v1/entries/builds.php`（列表与创建）与 `build.php`（单条读写）。

**四条实测要点**

1. **创建是项目级路径**：`POST v1/projects/{project}/builds`，`project` 只认路径参数，
   放请求体无效。
2. **必填五项**：`execution`、`product`、`name`、`builder`、`date`——少一个即 400
   （`requireFields('execution,product,name,builder,date')`）。
3. **执行必须属于该项目**：服务端对跨项目组合只回 `error execution id!` 之类，
   用户无法定位，故预览阶段先自己核对。
4. **`GET v1/builds/{id}` 直接返回构建对象**（不包一层），创建成功返回 201 + 同一结构。

**构建不是"版本号"的同义词**：禅道另有一个虚拟版本 `trunk`（主干），它不在这张表里，
只能从 Bug 解决表单读到——所以新建构建不会影响 `trunk` 的可用性。
"""

from actions import (ACCOUNT, BOTH, CREATE, DATE, ID, INT, TEXT,
                     execute as _execute, prepare as _prepare)
from common import SkillError

SPECS = {
    'build': {
        'label': '构建',
        'wrap': 'build',
        'collection': 'v1/projects/{project}/builds',
        'path_args': ('project',),
        'detail': 'v1/builds/{id}',
        'view': '/build-view-{id}.html',
        # 修改时沿用当前值铺满：服务端把 product/execution/name 当必填，改一句描述也要重复填
        # 三个关联字段，填错就会把构建挪走。改成从详情自动补齐，用户只给要改的项。
        'update_fill': True,
        # 归属信息被悄悄改动等于把构建挪到别的项目/执行下，必须逐次核对。
        'monitor_fields': ('project', 'product', 'execution'),
        'notices': {
            'create': '构建必须指定项目、产品、执行、名称、创建人与日期，执行要属于该项目。'
                      '`builder` 填登录账号，`date` 用 YYYY-MM-DD。',
            'update': '会自动沿用当前的产品、执行、名称、构建者与打包日期，只需给出要改的项；'
                      '所属项目由记录本身固定、所属执行在页面上是只读的，不会因修改而变。',
        },
        'fields': {
            'project': {'type': ID, 'required': CREATE, 'scope': 'project'},
            'product': {'type': ID, 'required': BOTH, 'scope': 'product'},
            'execution': {'type': ID, 'required': BOTH, 'scope': 'execution'},
            'name': {'type': TEXT, 'required': BOTH},
            'builder': {'type': ACCOUNT, 'required': CREATE},
            'date': {'type': DATE, 'required': CREATE},
            'branch': {'type': INT},
            'scmPath': {'type': TEXT},
            'filePath': {'type': TEXT},
            'desc': {'type': TEXT},
        },
    },
}


def check_build(client, action, body, requested):
    """预览阶段核对构建的归属：执行必须落在同一个项目下。

    修改时只在动了执行时才查归属。项目的路径由记录本身固定、页面也不允许改所属执行
    （只读并提示「提交测试单的构建，不能修改其所属迭代」），所以 update 缺 `project`
    时只确认执行存在，不做归属比对——否则会拿不到 project 直接崩（台账 D14）。
    """
    if action == 'update' and 'execution' not in requested:
        return
    execution = body.get('execution')
    if not execution:
        return
    value = client.call('GET', f'v1/executions/{execution}')
    if isinstance(value, dict) and isinstance(value.get('execution'), dict):
        value = value['execution']
    if not isinstance(value, dict) or str(value.get('id')) != str(execution):
        raise SkillError('执行详情格式不匹配，已停止。')
    project = body.get('project')
    if project is None:
        return
    if str(value.get('project')) != str(project):
        raise SkillError(f'执行 {execution} 不属于项目 {project}；构建必须挂在同项目的执行下。')


SPECS['build']['precheck'] = check_build


def recover_create(client, body):
    """创建成功时服务端回 `201` + 空响应（实测 #376 就是这样落库的），按业务特征回查新记录。

    只用「同项目 + 同执行 + 同名称 + 同日期」唯一命中来确认；零条或多条都返回 None/报错，
    避免把别人的同名构建认成自己刚建的那条。
    """
    try:
        value = client.call('GET', f"v1/projects/{body['project']}/builds?limit=200")
    except SkillError:
        return None
    rows = value.get('builds') if isinstance(value, dict) else None
    name = str(body.get('name') or '')
    day = str(body.get('date') or '')[:10]
    hits = [row for row in rows or []
            if isinstance(row, dict) and str(row.get('name') or '') == name
            and str(row.get('date') or '')[:10] == day
            and str(row.get('execution')) == str(body.get('execution'))]
    if len(hits) == 1 and str(hits[0].get('id') or '').isdigit():
        return int(hits[0]['id'])
    if len(hits) > 1:
        raise SkillError(f'服务端返回空响应，按名称与日期回查到 {len(hits)} 条构建，'
                         '无法确定刚建的是哪条，请人工核对。')
    return None


SPECS['build']['recover_create'] = recover_create


def prepare(plan, client, entity='build', source_path=None):
    return _prepare(plan, client, SPECS[entity], entity, source_path)


def execute(review, confirmation, client):
    return _execute(review, confirmation, client, SPECS)
