"""只读查询的实体路由表：新增一个可查实体只需在 ENTITIES 里加一项。

端点与字段依据对真实环境的调用结果整理（企业版 12.7 与开源版源码交叉核对），
不是照抄官方文档——文档与实际返回有出入的地方以实测为准。

分页风格（paging）：
  page    → ?page=&limit=，总数在响应顶层 total（v1 接口）
  pageID  → ?pageID=&recPerPage=，总数在 pager.recTotal / pageTotal（v2 接口）
  none    → 一次返回全量，分页参数无效（项目集接口）

lists 决定「按什么范围查」：取第一个用户已给出参数的入口；都没给则取无需参数的
入口；若一个都没有，说明该实体必须带范围参数。
"""

# 字段摘要：只取用户关心的列。接口未返回该字段时自动跳过，因此可跨版本复用。
ENTITIES = {
    "stories": {
        "label": "需求",
        "collection": "stories",
        "detail": "v1/stories/{id}",
        "summary": ("id", "product", "title", "status", "stage", "pri", "estimate", "planTitle",
                    "openedBy", "openedDate", "assignedTo", "assignedDate", "lastEditedDate",
                    "closedReason", "version"),
        "lists": (
            {"arg": "product", "path": "v1/products/{product}/stories", "paging": "page"},
            {"arg": "project", "path": "v2/projects/{project}/stories", "paging": "pageID"},
            {"arg": "execution", "path": "v2/executions/{execution}/stories", "paging": "pageID"},
        ),
    },
    "todos": {
        "label": "待办",
        "collection": "todos",
        "detail": "v1/todos/{id}",
        "summary": ("id", "account", "date", "begin", "end", "type", "status", "cycle",
                    "content", "name", "pri", "desc"),
        "lists": (
            # status=all 才会返回全部待办；不带参数时接口只给未完成的一部分。
            {"arg": None, "path": "v1/todos?status=all", "paging": "page"},
        ),
    },
    "testtasks": {
        "label": "测试单",
        "collection": "testtasks",
        "detail": "v1/testtasks/{id}",
        "summary": ("id", "name", "product", "project", "execution", "build", "owner",
                    "status", "pri", "begin", "end", "createdBy", "createdDate"),
        "lists": (
            {"arg": "product", "path": "v1/testtasks?product={product}", "paging": "page"},
            {"arg": None, "path": "v1/testtasks", "paging": "page"},
        ),
    },
    "testcases": {
        "label": "用例",
        "collection": "testcases",
        "detail": "v1/testcases/{id}",
        "summary": ("id", "caseID", "product", "module", "story", "title", "pri", "type",
                    "status", "openedBy", "openedDate", "lastRunResult"),
        "lists": (
            {"arg": "product", "path": "v1/products/{product}/testcases", "paging": "page"},
        ),
    },
    "plans": {
        "label": "产品计划",
        "collection": "plans",
        # 该接口没有单条详情端点（v1/plans/{id} 返回 404），列表已含全部字段。
        "detail": None,
        "summary": ("id", "product", "title", "status", "begin", "end", "finishedDate", "desc"),
        "lists": (
            {"arg": "product", "path": "v1/products/{product}/plans", "paging": "page"},
        ),
    },
    "programs": {
        "label": "项目集",
        "collection": "programs",
        "detail": "v1/programs/{id}",
        # 项目集接口忽略分页参数，一次返回全量（实测 40 条）。
        "summary": ("id", "name", "project", "model", "type", "category", "status",
                    "lifetime", "begin", "end"),
        "lists": (
            {"arg": None, "path": "v1/programs", "paging": "none"},
        ),
    },
    "products": {
        "label": "产品",
        "collection": "products",
        "detail": "v2/products/{id}",
        "detail_key": "product",
        "summary": ("id", "name", "code", "status", "type", "PO", "QD", "createdBy"),
        "lists": (
            {"arg": None, "path": "v2/products?browseType=all", "paging": "pageID"},
        ),
    },
    "users": {
        "label": "用户",
        "collection": "users",
        "detail": "v2/users/{id}",
        "detail_key": "user",
        # v2 的 pageID/recPerPage 生效且字段更全（含 id/dept/role）；v1 只给账号与拼音。
        "summary": ("id", "account", "realname", "dept", "role", "email", "mobile"),
        "lists": (
            {"arg": None, "path": "v2/users", "paging": "pageID"},
        ),
    },
    "bugs": {
        "label": "Bug",
        "collection": "bugs",
        "detail": "v2/bugs/{id}",
        "detail_key": "bug",
        # 资源视角的 Bug 查询：必须给产品、项目或执行之一（REST 没有「我的 Bug」入口）。
        # 与 bugs 命令（人的视角，走我的地盘页面）互为兜底——页面不可用时用这条。
        "summary": ("id", "product", "project", "execution", "title", "status", "severity",
                    "pri", "type", "assignedTo", "openedBy", "openedDate", "resolvedBy",
                    "resolution", "lastEditedDate"),
        "lists": (
            {"arg": "product", "path": "v2/products/{product}/bugs?browseType=all", "paging": "pageID"},
            {"arg": "project", "path": "v2/projects/{project}/bugs?browseType=all", "paging": "pageID"},
            {"arg": "execution", "path": "v2/executions/{execution}/bugs?browseType=all", "paging": "pageID"},
        ),
    },
}


def spec_of(name):
    return ENTITIES.get(name)


# 范围参数的存在性预检端点。实测教训：编号写错时部分接口（如产品下的用例）不会报错，
# 而是忽略该参数返回全部数据，用户会拿到看似有效但完全不对的结果。
SCOPE_PROBE = {
    "product": ("v1/products/{value}", "产品"),
    "project": ("v1/projects/{value}", "项目"),
    "execution": ("v2/executions/{value}", "执行"),
    "program": ("v1/programs/{value}", "项目集"),
}


def pick_list(spec, scope):
    """按用户给出的范围参数选列表入口；都没给时取无需参数的入口。"""
    for item in spec["lists"]:
        if item["arg"] and scope.get(item["arg"]) is not None:
            return item
    for item in spec["lists"]:
        if not item["arg"]:
            return item
    return None
