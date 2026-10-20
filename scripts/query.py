"""只读查询：按实体取列表或详情。

服务端只负责范围（产品/项目/执行）与分页；**状态与关键词筛选一律在本地完成**，
原因是实测 v1 接口的 status 过滤参数不生效（传 status=active 会返回 0 条，
而同一批数据里明明有 active 需求）。本地筛选行为可预期，也便于跨版本一致。
"""

import datetime
import html
import re
import urllib.parse

from auth import BUSINESS_FAILURE, setting
from common import SkillError, is_missing, missing_error, person, stamp
from routes import ENTITIES, SCOPE_PROBE, pick_list, spec_of

PAGE_SIZE = 100        # 每页向服务端请求的条数
SCAN_CAP = 500         # 本地筛选时的扫描上限，避免一次拉取过多
DEFAULT_LIMIT = 20
TEXT_FIELDS = ("spec", "desc", "precondition", "steps", "report", "content")
TAG = re.compile(r"<[^>]+>")


def clock(value):
    """待办的时间字段在列表里是 09:00、单条详情里是 0900，统一成 09:00。"""
    text = stamp(value)
    return f"{text[:2]}:{text[2:]}" if len(text) == 4 else value


def day(value):
    """日期字段在列表里是 2026-10-16、单条详情里是 20261016，统一成 2026-10-16。"""
    text = stamp(value)
    return f"{text[:4]}-{text[4:6]}-{text[6:]}" if len(text) == 8 else value


def plain(value, limit=600):
    """富文本转纯文本并截断：接口里的正文是 HTML，直接输出会把无用的标签灌进对话。"""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", str(value or ""), flags=re.S | re.I)
    text = html.unescape(TAG.sub(" ", text))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + "…（已截断）" if len(text) > limit else text


def local_time(value):
    """接口用 ISO UTC 时间戳；按个人时区换算，否则用户会看到差 8 小时的时间。"""
    if not isinstance(value, str) or not value.endswith("Z"):
        return value
    try:
        moment = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        offset = datetime.datetime.strptime(setting("utc_offset", "+08:00"), "%z").utcoffset()
    except (ValueError, TypeError):
        return value
    if offset is None:
        return value
    return moment.astimezone(datetime.timezone(offset)).strftime("%Y-%m-%d %H:%M")


def entities():
    """列出可查询实体，供宿主自省可查范围，不必猜实体名。"""
    return {"status": "ok", "entities": [
        {"entity": name,
         "label": spec["label"],
         "scopes": [item["arg"] for item in spec["lists"] if item["arg"]],
         "detail": bool(spec.get("detail"))}
        for name, spec in ENTITIES.items()]}


def _rows(response, key):
    """取出集合；键存在但为空是「没有记录」，键不存在是结构变化，两者必须区分。"""
    if not isinstance(response, dict) or key not in response:
        raise SkillError(f"响应里没有 {key} 列表，接口结构可能已变化；不能据此认为没有数据。")
    value = response[key]
    if value is None:
        raise SkillError(f"响应里 {key} 为空值，接口结构可能已变化；不能据此认为没有数据。")
    if isinstance(value, dict):
        return list(value.values())
    if isinstance(value, list):
        return value
    raise SkillError(f"响应里 {key} 不是列表，接口结构可能已变化；不能据此认为没有数据。")


def _page(client, path, paging, page, size, collection):
    """取一页数据，返回 (本页记录, 服务端总数, 总页数)。"""
    if paging == "none":
        response = client.call("GET", path)
        return _rows(response, collection), None, None
    if paging == "page":
        query = urllib.parse.urlencode({"page": page, "limit": size})
        key, page_key = "total", None
    else:
        query = urllib.parse.urlencode({"pageID": page, "recPerPage": size})
        key, page_key = None, "pageTotal"
    response = client.call("GET", f"{path}{'&' if '?' in path else '?'}{query}")
    rows = _rows(response, collection)
    total = page_total = None
    if key:
        total = _int(response.get(key))
    else:
        pager = response.get("pager") if isinstance(response.get("pager"), dict) else {}
        total = _int(pager.get("recTotal"))
        page_total = _int(pager.get(page_key))
    return rows, total, page_total


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _matches(row, status, keyword):
    if status and status.lower() not in str(row.get("status", "")).lower():
        return False
    if keyword:
        # 不限定字段：需求的标题、用户的姓名与账号、测试单的名称都在各自字段里，
        # 逐个配置容易漏，匹配全部短标量值更实用。
        text = " ".join(str(value) for value in row.values()
                        if isinstance(value, (str, int, float)) and len(str(value)) < 200)
        if keyword.lower() not in text.lower():
            return False
    return True


def _project(row, spec):
    """按摘要字段裁剪；字段名与预期不符时退回短标量字段，避免输出空对象误导用户。"""
    picked = {key: row[key] for key in spec["summary"] if key in row}
    if not picked:
        picked = {key: value for key, value in row.items()
                  if isinstance(value, (str, int, float, bool)) and key not in TEXT_FIELDS
                  and len(str(value)) < 120}
    for key, value in list(picked.items()):
        value = person(value)
        if isinstance(value, str):
            if key == "date":
                value = day(value)
            elif key in ("begin", "end"):
                value = clock(value)
            elif TAG.search(value):
                value = plain(value)
            elif len(value) >= 19 and value.endswith("Z"):
                value = local_time(value)
            else:
                value = value.strip()
        picked[key] = value
    return picked


def verify_scope(client, scope):
    """先确认范围编号真实存在。

    实测教训：范围编号写错时，部分接口不报错，而是忽略该参数把全部数据返回——
    用户会拿到看似有效、实则与所问无关的结果。宁可多一次请求，也不能给错数据。
    """
    for key, value in scope.items():
        probe = SCOPE_PROBE.get(key)
        if not probe:
            continue
        path, label = probe
        try:
            client.call("GET", path.format(value=value))
        except SkillError as exc:
            # 编号不存在（400/404/v2 业务失败）或无权查看时，本节都在「范围编号」上，如实归因。
            if not (is_missing(exc) or "HTTP 403" in str(exc) or BUSINESS_FAILURE in str(exc)):
                raise
            raise SkillError(f"指定的{label}编号 {value} 不存在，或当前账号无权查看；"
                             "请核对编号后再查询。") from None


def list_entity(client, name, scope, status, keyword, limit, take_all):
    spec = spec_of(name)
    if spec is None:
        raise SkillError(f"不支持查询「{name}」；可用实体：" + "、".join(ENTITIES) + "。")
    entry = pick_list(spec, scope)
    if entry is None:
        names = "、".join("--" + item["arg"] for item in spec["lists"] if item["arg"])
        raise SkillError(f"查询{spec['label']}需要指定范围参数（{names}）；"
                         "可用 `entities` 命令查看各实体支持的参数。")
    verify_scope(client, scope)
    path = entry["path"].format(**scope)
    want = SCAN_CAP if take_all else max(limit, 1)
    # 无筛选时按需取一页即可；有筛选时必须多扫，否则会把「没扫到」误当成「不存在」。
    scan_cap = SCAN_CAP if (status or keyword or take_all) else want

    rows, matched, page, total, page_total, seen = [], [], 1, None, None, set()
    while True:
        try:
            batch, total, page_total = _page(client, path, entry["paging"], page, PAGE_SIZE,
                                             spec["collection"])
        except SkillError as exc:
            narrowed = scope and (is_missing(exc) or BUSINESS_FAILURE in str(exc))
            if not narrowed:
                raise
            given = "、".join(f"{key}={value}" for key, value in scope.items())
            raise SkillError(f"禅道拒绝了这次查询（{given}）：该范围编号不存在，或当前账号无权查看；"
                             "请核对编号，或改用 `entities` 查看该实体支持的范围参数。") from None
        for row in batch:
            marker = row.get("id") if isinstance(row, dict) else None
            if marker is not None:
                if marker in seen:
                    raise SkillError("分页返回了重复记录，已停止，避免输出错乱。")
                seen.add(marker)
            rows.append(row)
        matched = [row for row in rows if _matches(row, status, keyword)]
        if len(matched) >= want or not batch or entry["paging"] == "none":
            break
        if len(rows) >= scan_cap:
            break
        if total is not None and len(rows) >= total:
            break
        if page_total is not None and page >= page_total:
            break
        page += 1

    items = [_project(row, spec) for row in matched[:want]]
    result = {"status": "ok" if items else "empty", "entity": name, "label": spec["label"],
              "scope": scope or None, "total": total, "scanned": len(rows),
              "count": len(items), "items": items}
    notes = []
    if status or keyword:
        parts = [part for part in (f"状态含「{status}」" if status else "",
                                   f"标题含「{keyword}」" if keyword else "") if part]
        notes.append("已按" + "、".join(parts) + "在本地筛选")
        if not items:
            notes.append(f"扫描 {len(rows)} 条均未命中" if rows else "该范围内没有记录")
    elif not items:
        notes.append("该范围内没有记录")
    if len(matched) > len(items):
        notes.append(f"命中 {len(matched)} 条，仅显示前 {len(items)} 条（可用 --limit 或 --all 取更多）")
    if total is not None and len(rows) < total:
        notes.append(f"该范围共 {total} 条，本次仅读取前 {len(rows)} 条")
    result["message"] = "；".join(notes) + "。" if notes else f"已取回 {len(items)} 条记录。"
    return result


def detail(client, name, ident_value):
    spec = spec_of(name)
    if spec is None:
        raise SkillError(f"不支持查询「{name}」；可用实体：" + "、".join(ENTITIES) + "。")
    if not spec.get("detail"):
        raise SkillError(f"{spec['label']}没有单条详情接口；请用 query 查看列表。")
    # 用例列表里的编号是 case_1001 形式，接口只认数字，这里宽容剥掉前缀。
    number = re.sub(r"^[A-Za-z]+_", "", str(ident_value)).strip()
    try:
        response = client.call("GET", spec["detail"].format(id=number))
    except SkillError as exc:
        raise (missing_error(exc, spec["label"], ident_value) or exc) from None
    key = spec.get("detail_key")
    value = response.get(key) if key and isinstance(response, dict) else response
    if not isinstance(value, dict):
        raise SkillError(f"{spec['label']}详情响应格式不匹配，需核对实际接口。")
    item = _project(value, spec)
    for field in TEXT_FIELDS:
        # 正文（需求描述、Bug 重现步骤等）单独保留，但转成纯文本并截断。
        if value.get(field) and field not in item:
            item[field] = plain(value[field], 1500)
    return {"status": "ok", "entity": name, "label": spec["label"], "item": item}
