"""「我的地盘」（`/my-work-*`）页面取数：把「人的视角」的读取与校验收敛到一处。

为什么单独成模块：`bugs` 与 `candidates` 先前各写了一套「取页面 JSON → 二次解码 →
分页推进 → 查重」的代码，需求与测试单又要用同一套，故在此收敛（第 3 个使用者出现后才做）。

⚠️ 实测（企业版 12.7）有两条必须防住的坑：

1. **非法 browseType 会退化成「返回全量」**。`/my-work-story-zzznotascope-…` 返回 20 条
   **别人创建**的需求，而 `assignedTo` 返回 0 条；`testtask` 更极端——只有 `assignedTo`
   有效，`owner` / `createdBy` / `openedBy` / 任意值一律返回全部记录。
   用户问「我的需求」却拿到全公司的需求，属最危险的一类错数据。
2. **结果集合的键名随 mode 变化**（`stories` / `tasks` / `cases` / `bugs` / `todos`），
   同一个 mode 还可能随 scope 变（测试单的 `assignedTo` 用 `tasks`）。

因此本模块强制两件事：`scope` 必须由调用方用**本地白名单**给出（绝不拼用户输入），
每条记录的**归属字段**必须等于当前账号，对不上就报错停止——这是唯一能把
「我的」与「全部的」区分开的办法。
"""

import json

from auth import fetch_page
from common import SkillError, account, is_missing

PAGE_SIZE = 100
MAX_PAGES = 50


def _decode(raw, hint):
    """页面数据是 `{status, data}`，且 `data` 是二次编码的 JSON 字符串。"""
    try:
        outer = json.loads(raw)
        data = outer.get('data') if isinstance(outer, dict) else None
        if isinstance(data, str):
            data = json.loads(data)
        if not isinstance(data, dict):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise SkillError('「我的地盘」页面结构不兼容，请核对禅道版本并确认已重新登录；'
                         '不能视为没有数据。' + hint) from None
    return data


def _page(session, path, hint):
    """取页面 JSON；页面缺失或无权访问时，把可走的替代路径一并告知。"""
    try:
        return fetch_page(session, path)
    except SkillError as exc:
        if is_missing(exc) or 'HTTP 403' in str(exc):
            raise SkillError(f'「我的地盘」页面不可用：{exc} {hint}') from None
        raise


def collect(session, mode, scope, keys, owner_key, owner, hint=''):
    """逐页读取某个「我的地盘」视图，返回记录列表。

    keys      结果集合的键名候选（按出现顺序取第一个；实测随 mode 变化）
    owner_key 归属字段名——每条记录的该字段必须等于 owner，否则报错停止
    hint      出错时附给用户的替代路径说明
    """
    rows, seen, skipped, total = [], set(), 0, None
    for page in range(1, MAX_PAGES + 1):
        path = f'/my-work-{mode}-{scope}-0-id_desc-0-{PAGE_SIZE}-{page}.json'
        data = _decode(_page(session, path, hint), hint)
        batch = None
        for key in keys:
            if key in data:
                value = data[key]
                batch = list(value.values()) if isinstance(value, dict) else value
                break
        if not isinstance(batch, list):
            raise SkillError(f'「我的地盘」{mode} 视图里没有 {" / ".join(keys)} 集合，'
                             '接口结构可能已变化；不能视为没有数据。' + hint)
        pager = data.get('pager') if isinstance(data.get('pager'), dict) else {}
        if pager.get('recTotal') is not None:
            try:
                total = int(pager['recTotal'])
            except (TypeError, ValueError):
                raise SkillError('「我的地盘」分页信息不完整，已停止。') from None
            if pager.get('pageID') is not None and str(pager['pageID']) != str(page):
                raise SkillError('「我的地盘」分页编号与请求不一致，已停止。')
        for row in batch:
            if not isinstance(row, dict) or row.get('id') in (None, ''):
                raise SkillError(f'「我的地盘」{mode} 视图有记录缺少编号，已停止。')
            if str(row.get('deleted')).lower() in ('1', 'true'):
                skipped += 1          # 软删除记录不输出，但要计入总数核对
                continue
            if account(row.get(owner_key)) != owner:
                raise SkillError(f'「我的地盘」返回了不属于当前账号的记录（{owner_key} 与登录账号不符），'
                                 '已停止；这类视图的参数写错时服务端可能退化成返回全量数据。')
            if row['id'] in seen:
                raise SkillError('「我的地盘」分页出现重复记录，已停止。')
            seen.add(row['id'])
            rows.append(row)
        if not batch or (total is not None and len(rows) + skipped >= total):
            break
    else:
        raise SkillError('「我的地盘」分页超过读取上限，已停止。')
    if total is not None and len(rows) + skipped != total:
        raise SkillError('「我的地盘」记录数与站点返回的总数不一致，请重新查询。')
    return rows
