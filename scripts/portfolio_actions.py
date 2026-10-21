"""禅道产品、项目、执行的创建与修改。

流程骨架在 `actions.py`（两阶段 + 字段类型系统），本模块只声明三者的字段表。

**权限**：三者的写权限通常属管理员或产品经理角色，开发或测试账号常见无权限
（实测返回 HTTP 403；对照：同编号的 GET 返回 400/404，说明路由存在、只是缺权限）。
遇到 403 时脚本会翻译成「账号缺权限」的明确提示，请如实转达。

**两处源码级的坑（都不是笔误）**：
  - 项目创建里 `setPost('parent', request('program', 0))` 紧接着被
    `setPost('parent', request('parent', 0))` 覆盖 ⇒ **项目集必须用 `parent`，`program` 无效**
    （产品那边 `program` 是有效的，两者不一致）。
  - 执行创建的 `project` 只从**路径参数或 query** 取（`param('project')`），放 body 里会被
    忽略并覆盖为 0 ⇒ 走 `v1/projects/{project}/executions`。
"""

from actions import (ACCOUNT, ACCOUNTS, CREATE, DATE, ENUM, ID, IDS, INT, TEXT, UPDATE,
                     execute as _execute, prepare as _prepare)

SPECS = {
    'product': {
        'label': '产品',
        'wrap': 'product',
        'collection': 'v1/products',
        'detail': 'v1/products/{id}',
        'view': '/product-view-{id}.html',
        'defaults': {'acl': 'private'},
        'skip_verify': ('whitelist',),
        # 只监控归属、负责人、类型和状态，避免普通字段规范化造成写后误报。
        'monitor_fields': ('program', 'line', 'PO', 'QD', 'RD', 'type', 'status'),
        'notices': {'create': '产品代号 code 是否必填由站点设置决定；站点要求而本次未填时，'
                              '服务端会提示，按提示补填。'},
        'fields': {
            'name': {'type': TEXT, 'required': CREATE},
            'code': {'type': TEXT},
            'program': {'type': ID, 'scope': 'program'},
            'line': {'type': ID},
            'PO': {'type': ACCOUNT},
            'QD': {'type': ACCOUNT},
            'RD': {'type': ACCOUNT},
            'type': {'type': ENUM, 'values': ('normal', 'branch', 'platform')},
            'desc': {'type': TEXT},
            'acl': {'type': ENUM, 'values': ('private', 'open', 'custom')},
            'whitelist': {'type': ACCOUNTS},
            'status': {'type': ENUM, 'values': ('normal', 'closed'), 'ops': UPDATE},
        },
    },
    'project': {
        'label': '项目',
        'wrap': 'project',
        'collection': 'v1/projects',
        'detail': 'v1/projects/{id}',
        'view': '/project-view-{id}.html',
        'defaults': {'acl': 'private', 'model': 'scrum'},
        'skip_verify': ('whitelist',),
        # 只保护产品与项目集归属、负责人、研发模型和访问模式等核心配置。
        'monitor_fields': ('products', 'parent', 'PM', 'model', 'multiple', 'acl'),
        'notices': {'create': '项目集用 parent 指定（接口里 program 参数会被 parent 覆盖）；'
                              '未指定 model 时按 scrum 创建；多迭代项目用 multiple=on，不填即单迭代。'},
        'fields': {
            'name': {'type': TEXT, 'required': CREATE},
            'code': {'type': TEXT},
            'begin': {'type': DATE, 'required': CREATE},
            'end': {'type': DATE, 'required': CREATE},
            'products': {'type': IDS, 'required': CREATE, 'scope': 'product'},
            # 项目集只能走 parent：创建时 program 会被 parent 覆盖（见模块说明）。
            'parent': {'type': ID, 'scope': 'program'},
            'PM': {'type': ACCOUNT},
            'model': {'type': ENUM, 'values': ('scrum', 'waterfall', 'kanban',
                                               'agileplus', 'waterfallplus', 'ipd')},
            'acl': {'type': ENUM, 'values': ('private', 'open', 'custom')},
            'desc': {'type': TEXT},
            'whitelist': {'type': ACCOUNTS},
            'multiple': {'type': ENUM, 'values': ('on', 'no')},
            'storyType': {'type': TEXT},
            'days': {'type': INT, 'ops': UPDATE},
            'budget': {'type': TEXT, 'ops': UPDATE},
            'budgetUnit': {'type': TEXT, 'ops': UPDATE},
            'workflowGroup': {'type': INT, 'ops': UPDATE},
        },
    },
    'execution': {
        'label': '执行',
        'wrap': 'execution',
        'collection': 'v1/projects/{project}/executions',
        'path_args': ('project',),
        'detail': 'v1/executions/{id}',
        'view': '/execution-view-{id}.html',
        'defaults': {'acl': 'private'},
        'skip_verify': ('whitelist',),
        # 只保护项目归属、父执行、关联产品、状态、负责人和访问模式。
        'monitor_fields': ('project', 'parent', 'products', 'status', 'PM', 'acl'),
        'notices': {'create': '执行建在指定项目下（project 走路径参数）；关联产品用 products'
                              '（编号数组），不填由服务端沿用项目已关联的产品。'},
        'fields': {
            'name': {'type': TEXT, 'required': CREATE},
            'begin': {'type': DATE, 'required': CREATE},
            'end': {'type': DATE, 'required': CREATE},
            'project': {'type': ID, 'required': CREATE, 'scope': 'project'},
            'code': {'type': TEXT},
            'lifetime': {'type': ENUM, 'values': ('0', '1')},
            'desc': {'type': TEXT},
            'days': {'type': INT},
            'percent': {'type': INT},
            'parent': {'type': ID},
            'acl': {'type': ENUM, 'values': ('private', 'open', 'custom')},
            'status': {'type': TEXT, 'ops': UPDATE},
            'PO': {'type': ACCOUNT},
            'PM': {'type': ACCOUNT},
            'QD': {'type': ACCOUNT},
            'RD': {'type': ACCOUNT},
            'whitelist': {'type': ACCOUNTS},
            'products': {'type': IDS, 'scope': 'product'},
            'plans': {'type': IDS},
            'teamMembers': {'type': ACCOUNTS},
        },
    },
}


def prepare(plan, client, entity, source_path=None):
    return _prepare(plan, client, SPECS[entity], entity, source_path)


def execute(review, confirmation, client):
    return _execute(review, confirmation, client, SPECS)
