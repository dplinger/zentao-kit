"""禅道日志命令：只读查询、生成可审阅快照，以及显式确认后的批量写入。"""

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
import datetime as dt
from decimal import Decimal, InvalidOperation
import html
import json
import os
from pathlib import Path
import re
import sys
import urllib.parse

from auth import (base_url, auth_status, load_session, locate_base, profile, renew_session, request,
                  setting, start_login, wait_login)
from common import (SkillError, date_range, digest, emit, missing_error, private_write, read_json,
                    state_dir)

# 预览统一返回面向用户的确认话术，避免宿主转述内部提交规则。
PREVIEW_MESSAGE = "日志写入后可在对应任务的详情页「工时记录」中编辑或删除（企业版另有「地盘 → 日志」汇总视图）。请先核对以上记录；需要调整的地方直接告诉我，确认无误后回复“提交”，我再写入禅道。"
TERMINAL_TASK_STATUSES = {"done", "finished", "completed", "closed", "cancel", "canceled", "cancelled", "已完成", "已关闭", "已取消"}


def hours(value):
    """拒绝 NaN、无穷大、布尔值和负工时，使用十进制避免累加误差。"""
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or result.as_tuple().exponent < -2:
            raise ValueError()
        return result
    except (InvalidOperation, ValueError):
        raise SkillError("工时必须是非负数，最多两位小数。") from None


def ident(value):
    if isinstance(value, bool) or not str(value).isdigit() or int(value) < 1:
        raise SkillError("项目、执行和任务编号必须为正整数。")
    return int(value)


def account_of(value):
    return value.get("account", "") if isinstance(value, dict) else str(value or "")


def terminal_task(status):
    """已结束任务不能用于登记日志，避免禅道因写日志将其重新激活。"""
    return str(status or "").strip().lower() in TERMINAL_TASK_STATUSES


def collection(value, key):
    """仅接受已知结构；未知响应不能被解释成没有日志。"""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for candidate in (key, "data"):
            if candidate in value:
                return collection(value[candidate], key)
        if value and all(str(k).isdigit() and isinstance(v, dict) for k, v in value.items()):
            return list(value.values())
    raise SkillError(f"响应缺少 {key} 列表；请补充脱敏响应样例后适配，当前操作已停止。")


class Client:
    def __init__(self):
        session = load_session()
        self.base = session["base_url"]
        # 仅在用户显式配置过站点时比对，避免未配置时误判为“站点不同”。
        configured = setting('base_url')
        if configured and base_url(configured) != base_url(self.base):
            raise SkillError('AUTH_REQUIRED：配置站点与登录站点不同，请重新登录。')
        self.token = session["token"]
        try:
            who = profile(self.base, self.token)  # 使用缓存前先验证鉴权和当前身份。
        except SkillError as exc:
            if not str(exc).startswith("AUTH_REQUIRED"):
                raise
            session = renew_session(session)  # 仅令牌过期时自动重登一次。
            self.token = session["token"]
            who = profile(self.base, self.token)
        if who["account"] != session["account"]:
            raise SkillError("登录身份发生变化，请重新鉴权。")
        self.session = session
        self.account = who["account"]

    def call(self, method, path, body=None):
        return request(self.base, method, path, self.token, body)

    def candidates(self):
        from candidates import fetch_candidates
        rows = fetch_candidates(self.session, self.account)
        available = []
        for row in rows:
            task = self.task(row["id"])
            if terminal_task(task["status"]):
                continue
            available.append({**row, "status": task["status"], "left": task["left"]})
        return available

    def listing(self, path, key, status):
        result, seen = [], set()
        for page in range(1, 1001):
            # 项目接口使用 page/limit，执行与任务接口使用 pageID/recPerPage。
            paging = {"page": page, "limit": 100} if path == "v1/projects" else {"pageID": page, "recPerPage": 100}
            query = urllib.parse.urlencode({"status": status, **paging})
            response = self.call("GET", f"{path}?{query}")
            rows = collection(response, key)
            total = response.get("total") if isinstance(response, dict) else None
            pager = response.get("pager", {}) if isinstance(response, dict) else {}
            if isinstance(pager, dict):
                total = pager.get("recTotal", total)
            if not rows:
                if total is not None and len(result) < int(total):
                    raise SkillError("接口分页提前结束，查询结果不完整。")
                return result
            for row in rows:
                marker = ident(row["id"])
                if marker in seen:
                    raise SkillError("接口分页返回重复记录，请核对 pageID/recPerPage 参数。")
                seen.add(marker)
                result.append(row)
            if total is not None and len(result) >= int(total):
                if len(result) != int(total):
                    raise SkillError("分页总数与实际记录数不一致，请重新查询。")
                return result
            if isinstance(pager, dict) and pager.get("pageTotal") is not None and page >= int(pager["pageTotal"]):
                if total is not None and len(result) != int(total):
                    raise SkillError("分页已结束但记录总数不符。")
                return result
            # 无 total 时继续直到空页，不能以少于 100 条认定完整。
        raise SkillError("分页超过上限，未返回不完整结果。")

    def task(self, task_id):
        try:
            value = self.call("GET", f"v1/tasks/{task_id}")
        except SkillError as exc:
            raise (missing_error(exc, "任务", task_id) or exc) from None
        if isinstance(value, dict) and "task" in value:
            value = value["task"]
        if not isinstance(value, dict) or ident(value.get("id", 0)) != task_id:
            raise SkillError("任务详情响应格式不匹配，需核对实际接口。")
        required = ("execution", "left", "status", "name", "assignedTo")
        if any(k not in value for k in required):
            raise SkillError("任务详情缺少执行、剩余工时或状态等字段。")
        return {"id": task_id, "execution": ident(value["execution"]), "name": value["name"],
                "left": float(hours(value["left"])), "status": value["status"],
                "assignedTo": account_of(value["assignedTo"]),
                "lastEditedDate": value.get("lastEditedDate", "")}

    def logs(self, task_id):
        value = self.call("GET", f"v1/tasks/{task_id}/estimate")
        # 官方 v1 响应是 effort 下按日志 ID 组织的对象，兼容已知 estimates 包装。
        if isinstance(value, dict) and "effort" in value:
            rows = [] if value["effort"] == {} else collection(value["effort"], "effort")
        else:
            rows = collection(value, "estimates")
        if isinstance(value, dict) and "total" in value and int(value["total"]) != len(rows):
            raise SkillError("日志响应可能被分页截断；需确认全量日志接口后再提交。")
        normalized = []
        for row in rows:
            if not isinstance(row, dict) or any(k not in row for k in ("date", "work", "consumed", "left", "account")):
                raise SkillError("日志响应缺少日期、内容、工时或 account；无法可靠核对重复。")
            if row.get("deleted") in (True, 1, "1"):
                continue
            if ("objectID" in row and ident(row["objectID"]) != task_id) or row.get("objectType", "task") != "task":
                raise SkillError("日志响应关联对象与查询任务不一致，已停止。")
            date = str(row["date"])[:10]
            dt.date.fromisoformat(date)
            normalized.append({"date": date, "work": clean_work(row["work"]),
                               "consumed": float(hours(row["consumed"])), "left": float(hours(row["left"])),
                               "account": account_of(row["account"])})
        return normalized


def clean_work(value):
    # 服务端可能对文本进行 HTML 编码；保留内容本身的换行语义。
    return html.unescape(str(value)).replace("\r\n", "\n").strip()


def signature(row, account=None, include_left=True):
    value = [row["date"], clean_work(row["work"]), str(hours(row["consumed"]).normalize()), account or row["account"]]
    if include_left:
        value.append(str(hours(row["left"]).normalize()))
    return tuple(value)


def logs_digest(rows):
    return digest(sorted(signature(row) for row in rows))


def resolve_plan(plan, client):
    """读完任务和日志后生成确定的数组；该函数绝不发送写请求。"""
    first, last = date_range(start=plan["start"], end=plan["end"])
    if plan.get("account") != client.account:
        raise SkillError("草稿 account 与当前登录账号不一致。")
    source = plan.get("rows")
    if not isinstance(source, list) or not source:
        raise SkillError("草稿必须包含至少一条实际工作，不能为空白日期虚构工作。")
    rows, tasks, logs, warnings = [], {}, {}, []
    daily_counts = Counter()
    for index, raw in enumerate(source):
        row = dict(raw)
        # 任务未匹配时仍生成完整预览；只在最终写入前阻止提交。
        linked = row.get("task") not in (None, "") and row.get("execution") not in (None, "")
        if linked:
            row["task"] = ident(row["task"])
            row["execution"] = ident(row["execution"])
        else:
            row["task"] = row["execution"] = None
        day = dt.date.fromisoformat(row["date"])
        if not first <= day <= last:
            raise SkillError("日志日期超出本次范围。")
        # 限制每日条数，防止草稿把同一天拆成超过 8 条后提交。
        daily_counts[day] += 1
        if daily_counts[day] > 8:
            raise SkillError(f"{day} 的日志超过 8 条，请把同一工作项的过程动作合并后重新预览。")
        row["work"] = str(row.get("work", "")).strip()
        if not row["work"]:
            raise SkillError("每条工作必须有工作内容。")
        row.setdefault("sources", ["自动整理，待用户核对来源"])
        if not isinstance(row["sources"], list) or not all(isinstance(x, str) and x for x in row["sources"]):
            raise SkillError("sources 必须是非空字符串列表；线下工作填写用户补充依据。")
        row["match_reason"] = row.get("match_reason") or ("已关联，待用户核对匹配依据" if linked else "未找到可确认的任务，待用户在预览指定")
        row["order"] = index
        if not linked:
            row["task_name"] = "待关联"
            row["execution_name"] = "待关联"
            rows.append(row)
            continue
        task_id = row["task"]
        if task_id not in tasks:
            task = client.task(task_id)
            if terminal_task(task["status"]):
                row["task"] = row["execution"] = None
                row["task_name"] = row["execution_name"] = "待关联"
                row["match_reason"] = f"任务 {task_id} 状态为 {task['status']}，已从预览关联中排除，避免写日志后重新激活"
                warnings.append(f"任务 {task_id} 已完成、关闭或取消，未写入预览关联；请为 {row['date']} 的工作重新选择任务。")
                rows.append(row)
                continue
            tasks[task_id] = task
            logs[task_id] = client.logs(task_id)
        task = tasks[task_id]
        if task["execution"] != row["execution"]:
            raise SkillError(f"任务 {row['task']} 不属于指定执行，不能登记。")
        if task["assignedTo"] != client.account:
            warnings.append(f"任务 {row['task']} 当前指派给 {task['assignedTo'] or '未指派'}，请确认本人登记是否合适。")
        rows.append(row)
    existing_hours = defaultdict(Decimal)
    for task_logs in logs.values():
        for log in task_logs:
            if log["account"] == client.account and first.isoformat() <= log["date"] <= last.isoformat():
                existing_hours[log["date"]] += hours(log["consumed"])
    # 用户补充所选任务以外的已登记工时，参与补足8小时，但不作为新日志提交。
    other_hours = {}
    for day, entry in plan.get("other_logged_hours", {}).items():
        if not first <= dt.date.fromisoformat(day) <= last:
            raise SkillError("其他已登记工时的日期超出本次范围。")
        if not isinstance(entry, dict) or not str(entry.get("reason", "")).strip():
            raise SkillError("其他已登记工时必须提供 hours 和用户确认依据 reason。")
        amount = hours(entry["hours"])
        if amount > 24 or existing_hours[day] + amount > 24:
            raise SkillError("当天已有工时超过24小时，请核对是否重复计入。")
        other_hours[day] = float(amount)
        existing_hours[day] += amount
        warnings.append(f"{day} 另计所选任务外已登记 {amount} 小时，依据：{entry['reason']}；请确认未重复计入。")
    groups = defaultdict(list)
    for row in rows:
        groups[row["date"]].append(row)
    # 未知耗时按日补足 8 小时，按整数小时粗分：条数少时单条工时相应变大。
    for day, entries in groups.items():
        known = sum((hours(r["consumed"]) for r in entries if r.get("consumed") is not None), Decimal(0))
        unknown = [r for r in entries if r.get("consumed") is None]
        if unknown:
            if plan.get("hours_policy", "fill8") != "fill8":
                raise SkillError("存在未知工时：请指定 hours_policy=fill8，或填写实际工时。")
            available = Decimal(8) - existing_hours[day] - known
            whole = int(available)
            if whole < len(unknown):
                # 表单不接受空或零工时。已有记录已达 8 小时也要完整展示本次工作，交由最终预览调整。
                whole = len(unknown)
                warnings.append(f"{day} 已有工时已接近或超过 8 小时；本次仍按 8 小时基准分配，请在最终预览核对。")
            share, remainder = divmod(whole, len(unknown))
            for i, row in enumerate(unknown):
                row["consumed"] = float(share + (i < remainder))
                row["hours_source"] = "按当日8小时上限整数粗分（待用户确认）"
        for row in entries:
            if hours(row["consumed"]) <= 0 or hours(row["consumed"]) > 24:
                raise SkillError("单条日志耗时必须大于 0 且不超过 24 小时。")
            row["consumed"] = float(hours(row["consumed"]))
            row.setdefault("hours_source", "用户提供")
        total = sum((hours(r["consumed"]) for r in entries), existing_hours[day])
        if total > 24:
            raise SkillError(f"{day} 已有与拟登记工时超过 24 小时，请修正。")
        if total != 8:
            warnings.append(f"{day} 已查询及用户补充的已有工时+拟登记合计 {total} 小时，请核对。")
    day = first
    while day <= last:
        if day.isoformat() not in groups and not existing_hours[day.isoformat()] and day.isoformat() not in plan.get("excluded_dates", {}):
            warnings.append(f"{day} 没有拟登记或已查询到的日志：请确认休息/排除，或补充线下工作。")
        day += dt.timedelta(days=1)
    rows.sort(key=lambda r: (r["date"], r["order"]))
    balances = {task_id: hours(task["left"]) for task_id, task in tasks.items()}
    policies = {key: dict(value) for key, value in plan.get("remaining", {}).items()}
    for task_id, task in tasks.items():
        policy = policies.get(str(task_id), {})
        if policy.get("mode") not in (None, "budget", "fixed8"):
            raise SkillError("剩余工时策略仅支持 budget 或 fixed8。")
        if policy:
            if not policy.get("reason"):
                raise SkillError("修改剩余工时需要记录用户选择依据 reason。")
            if policy["mode"] == "budget":
                balances[task_id] = hours(policy["hours"])
            warnings.append(f"任务 {task_id} 原剩余 {task['left']} 小时；本次策略 {policy}。")
        need = sum((hours(r["consumed"]) for r in rows if r["task"] == task_id), Decimal(0))
        if policy.get("mode") != "fixed8" and balances[task_id] <= need:
            policies[str(task_id)] = {"mode": "fixed8"}
            warnings.append(f"剩余工时不足或登记后将归零：任务 {task_id} 原剩余 {task['left']} 小时，本次需 {need} 小时。"
                            "预览建议每条登记后固定剩余8小时；可能增加余额，请随最终表格确认或修改。")
    seen = set()
    for row in rows:
        task_id = row["task"]
        if task_id is None:
            row["left"] = 8.0
            row["left_source"] = "未关联任务，预览默认剩余8小时"
            continue
        policy = policies.get(str(task_id), {})
        if policy.get("mode") == "fixed8":
            row["left"] = 8.0
        else:
            balances[task_id] -= hours(row["consumed"])
            # 剩余为零会使任务关闭；任何异常漏网值都用 8 小时兜底。
            row["left"] = float(balances[task_id]) if balances[task_id] > 0 else 8.0
        row["task_name"] = tasks[task_id]["name"]
        mark = (task_id, row["date"], clean_work(row["work"]))
        if mark in seen:
            raise SkillError("草稿有同任务、同日期、同内容的重复条目，请先合并或修改。")
        seen.add(mark)
        if any(log["account"] == client.account and log["date"] == row["date"]
               and clean_work(log["work"]) == clean_work(row["work"]) for log in logs[task_id]):
            raise SkillError(f"ALREADY_EXISTS：任务 {task_id} 的 {row['date']} 已有相同工作内容，请从草稿移除后重新预览。")
    if any(row["task"] is None for row in rows):
        warnings.append("存在待关联工作：工时和剩余工时已完整填入默认值，但必须指定任务后才能提交。")
    warnings.append("8小时核对只包含本次所选任务中的本人日志；请补充其他任务、缺陷或未关联对象上的已登记工时。")
    for date, reason in plan.get("excluded_dates", {}).items():
        warnings.append(f"已排除 {date}：{reason}")
    return {"base_url": client.base, "account": client.account, "start": plan["start"], "end": plan["end"],
            "rows": rows, "tasks": {str(k): v for k, v in tasks.items()},
            "logs_before": {str(k): logs_digest(v) for k, v in logs.items()},
            "existing_hours": {k: float(v) for k, v in existing_hours.items()},
            "other_logged_hours": other_hours,
            "can_submit": not any(row["task"] is None for row in rows),
            "warnings": list(dict.fromkeys(warnings))}


def table(snapshot):
    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", "<br>").replace("\r", "")
    lines = ["# 禅道日志提交预览", "", f"账号：{snapshot['account']}　日期：{snapshot['start']} 至 {snapshot['end']}",
             f"站点：{snapshot['base_url']}", "", "写入后可在“地盘 → 日志”的列表视图中编辑或删除；请先核对以下全部条目。", "",
             "| 日期 | 执行／任务 | 工作内容 | 拟增工时 | 预计登记后剩余 |",
             "|---|---|---|---:|---:|"]
    totals = defaultdict(float)
    for r in snapshot["rows"]:
        execution = r.get("execution_name") or (f"执行 #{r['execution']}" if r["execution"] else "待关联")
        task = (f"[#{r['task']} {r['task_name']}]({snapshot['base_url']}/task-view-{r['task']}.html)"
                if r["task"] else "待关联")
        left = f"{r['left']:g} 小时" + ("（预览默认）" if r["task"] is None else "")
        values = [r["date"], f"{execution}／{task}", r["work"], f"{r['consumed']:g} 小时", left]
        lines.append("| " + " | ".join(cell(v) for v in values) + " |")
        totals[r["date"]] += r["consumed"]
    daily = []
    for day in sorted(set(totals) | set(snapshot["existing_hours"])):
        label = dt.date.fromisoformat(day).strftime("%m月%d")
        daily.append(f"{label} {totals[day] + snapshot['existing_hours'].get(day, 0):g}h")
    lines += ["", "**每日合计**：" + "｜".join(daily)]
    lines += ["", "## 待核对事项", ""] + ["- " + cell(w) for w in snapshot["warnings"]]
    lines += ["", "## 工时与匹配依据", ""]
    for r in snapshot["rows"]:
        task_label = f"#{r['task']}" if r["task"] else "待关联"
        lines.append(f"- {r['date']} / {task_label}：" + cell(r['hours_source'] + "；" + "；".join(r['sources']) + "；" + str(r['match_reason'])))
    return "\n".join(lines) + "\n"


def draft_preview(plan):
    """缺项草稿只用于集中核对，不产生可提交摘要；完整日期范围不漏行。"""
    first, last = date_range(start=plan["start"], end=plan["end"])
    rows = plan.get("rows", [])
    if not isinstance(rows, list):
        raise SkillError("rows 必须是列表。")
    missing = []
    for index, row in enumerate(rows, 1):
        if not first <= dt.date.fromisoformat(row["date"]) <= last:
            raise SkillError("日志日期超出范围。")
        fields = ["work"] if not row.get("work") else []
        if fields:
            missing.append({"row": index, "date": row["date"], "fields": fields, "note": row.get("needs_review", "")})
    excluded = plan.get("excluded_dates", {})
    for day, reason in excluded.items():
        if not first <= dt.date.fromisoformat(day) <= last or not isinstance(reason, str) or not reason.strip():
            raise SkillError("排除日期须在范围内，并记录用户提供的排除依据。")
        if any(row["date"] == day for row in rows):
            raise SkillError("排除日期不能同时包含待提交日志。")
    if missing or not rows:
        return {"status": "draft_incomplete", "can_submit": False, "rows": rows, "missing": missing,
                "message": PREVIEW_MESSAGE}
    return None


def prepare(path, client):
    plan = read_json(path)
    incomplete = draft_preview(plan)
    if incomplete:
        return incomplete
    snapshot = resolve_plan(plan, client)
    snapshot["source_path"] = str(Path(path).resolve())
    snapshot["source_digest"] = digest(plan)
    review_id = digest(snapshot)
    folder = state_dir() / "reviews"
    private_write(folder / f"{review_id}.json", snapshot)
    preview = folder / f"{review_id}-日志预览.md"
    private_write(preview, table(snapshot))
    return {"status": "preview_only", "review": review_id, "preview": str(preview), "can_submit": snapshot["can_submit"],
            "rows": snapshot["rows"], "warnings": snapshot["warnings"],
            "message": PREVIEW_MESSAGE}


@contextmanager
def submission_lock():
    """阻止同一台机器上多个智能体同时提交；进程异常退出时保留锁以便核查。"""
    path = state_dir() / "submit.lock"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise SkillError("已有提交锁。请先核实另一个提交进程及收据，不能直接并发或删除锁重试。") from None
    try:
        with os.fdopen(fd, "w") as out:
            out.write(str(os.getpid()))
        yield
    finally:
        path.unlink(missing_ok=True)


def load_review(review_id):
    if not re.fullmatch(r"[a-f0-9]{64}", review_id):
        raise SkillError("review 必须是 prepare 返回的完整摘要。")
    path = state_dir() / "reviews" / f"{review_id}.json"
    if not path.exists():
        raise SkillError("预览不存在或已在成功提交后清理，禁止重复提交；请先查询禅道已有日志。")
    value = read_json(path)
    if digest(value) != review_id:
        raise SkillError("预览快照被修改，请重新 prepare。")
    return value


def submit(review_id, confirmation, client):
    if confirmation != review_id:
        raise SkillError("缺少与本次预览一致的确认摘要。必须先获得用户对最终表格的明确确认。")
    snapshot = load_review(review_id)
    if not snapshot.get("can_submit"):
        raise SkillError("存在待关联任务的工作，预览已完整生成；请先关联任务并重新预览，不能提交。")
    if snapshot["account"] != client.account or snapshot["base_url"] != client.base:
        raise SkillError("登录账号或目标站点与预览不一致。")
    if digest(read_json(snapshot["source_path"])) != snapshot["source_digest"]:
        raise SkillError("草稿已变化，旧确认失效，请重新 prepare 并展示。")
    batches = defaultdict(list)
    for row in snapshot["rows"]:
        batches[str(row["task"])].append(row)
    receipt_path = state_dir() / "receipts" / f"{review_id}.json"
    with submission_lock():
        receipt = read_json(receipt_path) if receipt_path.exists() else {"review": review_id, "tasks": {}}
        # 任何一次发送前，先校验所有尚未提交任务，减少半批次写入。
        for task_id, rows in batches.items():
            status = receipt["tasks"].get(task_id, {}).get("status")
            if status == "verified":
                continue
            current_logs = client.logs(int(task_id))
            expected = Counter(signature(r, client.account) for r in rows)
            actual = Counter(signature(r) for r in current_logs)
            if status in ("inflight", "unknown"):
                if all(actual[k] >= n for k, n in expected.items()):
                    receipt["tasks"][task_id] = {"status": "verified", "count": len(rows), "recovered": True}
                    private_write(receipt_path, receipt)
                    continue
                raise SkillError("UNCERTAIN_WRITE：上次提交结果不明或仅部分写入。已禁止自动重发，请人工核对禅道和收据。")
            if client.task(int(task_id)) != snapshot["tasks"][task_id] or logs_digest(current_logs) != snapshot["logs_before"][task_id]:
                raise SkillError(f"STALE_REVIEW：任务 {task_id} 或日志自预览后发生变化，请重新整理预览。")
        for task_id, rows in batches.items():
            if receipt["tasks"].get(task_id, {}).get("status") == "verified":
                continue
            # 再次检查，缩短其他人修改与本批次提交之间的时间窗口。
            if client.task(int(task_id)) != snapshot["tasks"][task_id] or logs_digest(client.logs(int(task_id))) != snapshot["logs_before"][task_id]:
                raise SkillError(f"STALE_REVIEW：任务 {task_id} 在提交前变化，已停止后续提交。")
            body = {key: [row[key] for row in rows] for key in ("date", "work", "consumed", "left")}
            receipt["tasks"][task_id] = {"status": "inflight", "count": len(rows)}
            private_write(receipt_path, receipt)  # 必须在网络请求之前落盘。
            try:
                client.call("POST", f"v1/tasks/{task_id}/estimate", body)
                actual = Counter(signature(r) for r in client.logs(int(task_id)))
                expected = Counter(signature(r, client.account) for r in rows)
                if not all(actual[k] >= n for k, n in expected.items()):
                    raise SkillError("接口返回后未回查到全部日志。")
                receipt["tasks"][task_id] = {"status": "verified", "count": len(rows)}
                private_write(receipt_path, receipt)
            except Exception:
                receipt["tasks"][task_id] = {"status": "unknown", "count": len(rows)}
                private_write(receipt_path, receipt)
                raise SkillError("UNCERTAIN_WRITE：当前任务提交未能完整验证；已保存收据并停止。禁止自动重发。") from None
        # 返回内存中的提交摘要；清理失败也不能把已成功的提交当成失败重发。
        from workspace import cleanup_verified
        try:
            cleanup_result = cleanup_verified(review_id)
        except (OSError, ValueError, KeyError, SkillError):
            cleanup_result = {"status": "pending", "message": "日志已全部写入；临时文件清理未完成，禁止重发日志。"}
        return {"status": "verified", "cleanup": cleanup_result, **receipt}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    auth = sub.add_parser("auth", help="打开本机登录窗口；用户本人输入账号密码")
    auth.add_argument("--base-url", default=None)
    auth.add_argument("--terminal", action="store_true")
    auth.add_argument("--terminal-window", action="store_true", help="打开独立终端并运行交互式登录")
    auth.add_argument("--wait", action="store_true",
                      help="在当前进程内等待登录完成；宿主会回收命令产生的子进程时使用")
    auth.add_argument("--seconds", type=int, default=600, help="--wait 的最长等待秒数，默认 600")
    sub.add_parser("auth-status")
    p = sub.add_parser("auth-wait")
    p.add_argument("--seconds", type=int, default=20)
    projects = sub.add_parser("projects")
    projects.add_argument("--status", default="all")
    sub.add_parser("logout")
    sub.add_parser("whoami")
    p = sub.add_parser("candidates", help="读取当前用户负责的任务，供预览时切换")
    executions = sub.add_parser("executions")
    executions.add_argument("--project", required=True, type=int)
    executions.add_argument("--status", default="all")
    tasks = sub.add_parser("tasks")
    tasks.add_argument("--execution", required=True, type=int)
    tasks.add_argument("--status", default="all")
    for command in ("task", "logs"):
        p = sub.add_parser(command)
        p.add_argument("--task", type=int, required=True)
    p = sub.add_parser("prepare", help="读取草稿、查询禅道，生成表格；绝不写入")
    p.add_argument("--plan", required=True)
    p = sub.add_parser("submit", help="仅在用户明确确认最终表格后调用")
    p.add_argument("--review", required=True)
    p.add_argument("--confirm", required=True)
    p = sub.add_parser("receipt")
    p.add_argument("--review", required=True)
    p = sub.add_parser('task-prepare', help='预览新建、修改、完成或关闭任务')
    p.add_argument('--plan', required=True)
    p = sub.add_parser('task-submit', help='用户确认任务预览后执行一次')
    p.add_argument('--review', required=True)
    p.add_argument('--confirm', required=True)
    p = sub.add_parser('configure', help='保存站点和时区到个人配置')
    p.add_argument('--base-url')
    p.add_argument('--utc-offset')
    p = sub.add_parser('bugs', help='读取当前用户关联的 Bug；默认查询指派给我并输出摘要字段')
    p.add_argument('--bug', type=int, help='只输出指定 Bug 的问题描述')
    p.add_argument('--scope', choices=('assignedTo', 'openedBy', 'resolvedBy', 'closedBy'),
                   default='assignedTo', help='归属范围：指派、创建、解决或关闭（默认 assignedTo）')
    p = sub.add_parser('bug-products', help='读取指定产品的 Bug 列表')
    p.add_argument('--product', required=True, type=int)
    p = sub.add_parser('product-find', help='按名称查找产品编号，供 Bug 查询使用')
    p.add_argument('--name', required=True)
    p = sub.add_parser('bug-projects', help='读取指定项目的 Bug 列表')
    p.add_argument('--project', required=True, type=int)
    p = sub.add_parser('bug-executions', help='读取指定执行的 Bug 列表')
    p.add_argument('--execution', required=True, type=int)
    p = sub.add_parser('bug-detail', help='读取指定 Bug 的详情摘要')
    p.add_argument('--bug', required=True, type=int)
    p = sub.add_parser('builds', help='读取项目或执行的构建列表（不含虚拟版本 trunk）')
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument('--project', type=int)
    group.add_argument('--execution', type=int)
    p = sub.add_parser('resolve-builds', help='读取指定 Bug 解决版本的合法取值与推荐项（含 trunk）')
    p.add_argument('--bug', required=True, type=int)
    p = sub.add_parser('bug-prepare', help='预览 Bug 创建、修改及状态流转')
    p.add_argument('--plan', required=True)
    p = sub.add_parser('bug-submit', help='确认 Bug 预览后执行一次并回查')
    p.add_argument('--review', required=True)
    p.add_argument('--confirm', required=True)
    sub.add_parser('entities', help='列出可查询的实体与各自需要的范围参数')
    p = sub.add_parser('query', help='只读查询需求、待办、测试单、用例、产品计划、项目集、产品、用户')
    p.add_argument('--entity', required=True, help='实体名；用 entities 查看可用值')
    p.add_argument('--id', help='给出编号时改查该条详情')
    p.add_argument('--product', type=int)
    p.add_argument('--project', type=int)
    p.add_argument('--execution', type=int)
    p.add_argument('--status', help='按状态筛选（本地匹配，包含即命中）')
    p.add_argument('--keyword', help='按标题或名称筛选（本地匹配）')
    p.add_argument('--limit', type=int, default=20, help='最多返回条数，默认 20')
    p.add_argument('--all', action='store_true', help='取回全部命中记录（上限 500 条）')
    p = sub.add_parser('story-prepare', help='预览新建、修改、变更、关闭或激活需求')
    p.add_argument('--plan', required=True)
    p = sub.add_parser('story-submit', help='确认需求预览后执行一次并回查')
    p.add_argument('--review', required=True)
    p.add_argument('--confirm', required=True)
    p = sub.add_parser('todo-prepare', help='预览新建、修改、完成或激活待办')
    p.add_argument('--plan', required=True)
    p = sub.add_parser('todo-submit', help='确认待办预览后执行一次并回查')
    p.add_argument('--review', required=True)
    p.add_argument('--confirm', required=True)
    p = sub.add_parser('report', help='读取内建统计：项目/执行/产品/Bug 进度、年度贡献与产出')
    p.add_argument('--fields', help='逗号分隔的统计项；不给则列出可用项')
    p.add_argument('--year', type=int, help='统计年份，默认今年')
    who = p.add_mutually_exclusive_group()
    who.add_argument('--me', action='store_true', help='统计当前登录账号')
    who.add_argument('--account', help='指定账号（只影响项目概况、雷达、年度产出）')
    p.add_argument('--limit', type=int, default=20, help='明细列表最多列出条数，默认 20')
    p = sub.add_parser('mine', help='按人查询：我的需求、我的测试单（走「我的地盘」页面）')
    p.add_argument('--entity', help='stories（需求）/ testtasks（测试单）；不给则列出可用视图')
    p.add_argument('--scope', default='assignedTo',
                   help='归属范围，默认 assignedTo（指派给我）；需求还支持 openedBy/reviewedBy/closedBy')
    p.add_argument('--limit', type=int, default=20, help='最多列出条数，默认 20')
    p.add_argument('--all', action='store_true', help='列出全部命中记录')
    for entity, label in (('product', '产品'), ('project', '项目'), ('execution', '执行'),
                          ('testtask', '测试单'), ('testcase', '用例')):
        p = sub.add_parser(f'{entity}-prepare', help=f'预览新建{label}'
                           + ('或修改' if entity != 'testtask' else '（接口不支持修改）'))
        p.add_argument('--plan', required=True)
        p = sub.add_parser(f'{entity}-submit', help=f'确认{label}预览后执行一次并回查')
        p.add_argument('--review', required=True)
        p.add_argument('--confirm', required=True)
    args = parser.parse_args()

    if args.command == 'entities':
        from query import entities
        return entities()
    if args.command == 'report' and not args.fields:
        # 列出可用统计项不需要登录，先自省再取数。
        from reports import catalog
        return catalog()
    if args.command == 'mine' and not args.entity:
        # 列出「我的」视图同样不需要登录。
        from mine import entities as mine_views
        return mine_views()
    if args.command == 'configure':
        path = state_dir() / 'preferences.json'
        prefs = read_json(path) if path.exists() else {}
        notice = None
        if args.base_url:
            # 先规范并探测再落盘：地址写错时当场报错，不让用户白输一遍账号密码。
            prefs['base_url'], notice = locate_base(args.base_url)
        if args.utc_offset:
            if not re.fullmatch(r'[+-](?:0[0-9]|1[0-3]):[0-5][0-9]|[+-]14:00', args.utc_offset):
                raise SkillError('时区偏移应在 -14:00 至 +14:00 内。')
            prefs['utc_offset'] = args.utc_offset
        private_write(path, prefs)
        current = setting('base_url')
        result = {'base_url': current, 'utc_offset': setting('utc_offset', '+08:00')}
        if notice:
            # 协议被自动升级，或站点只能走明文 HTTP——都必须让用户知道。
            result['notice'] = notice
        if args.base_url:
            result['message'] = '站点已保存并探测通过；尚未登录时请继续执行 auth。'
        elif not current:
            result['message'] = ('尚未配置禅道站点。请向用户索取地址后执行 '
                                 'configure --base-url <地址> 或 auth --base-url <地址>。')
        return result

    if args.command == "auth":
        return start_login(args.base_url or setting('base_url'), args.terminal,
                           args.terminal_window, args.wait, args.seconds)
    if args.command == "auth-wait":
        return wait_login(args.seconds)
    if args.command == "auth-status":
        return auth_status()
    if args.command == "logout":
        (state_dir() / "session.json").unlink(missing_ok=True)
        (state_dir() / "auth-status.json").unlink(missing_ok=True)
        return {"status": "logged_out", "message": "已移除本机缓存；如需撤销服务端令牌，请在禅道处理。"}
    if args.command == "receipt":
        load_review(args.review)
        path = state_dir() / "receipts" / f"{args.review}.json"
        return read_json(path) if path.exists() else {"status": "not_submitted"}
    if args.command == "prepare":
        incomplete = draft_preview(read_json(args.plan))
        if incomplete:
            return incomplete
    client = Client()
    if args.command == 'task-prepare':
        from task_actions import prepare as prepare_task
        return prepare_task(read_json(args.plan), client, args.plan)
    if args.command == 'task-submit':
        from task_actions import execute
        return execute(args.review, args.confirm, client)
    if args.command == 'bugs':
        from bugs import fetch_bugs
        return fetch_bugs(client.session, client.account, args.bug, args.scope)
    if args.command in ('product-find', 'bug-products', 'bug-projects', 'bug-executions', 'bug-detail',
                        'builds', 'resolve-builds', 'bug-prepare', 'bug-submit'):
        from bug_actions import (builds as list_builds, bug_list, find_products, resolved_build_options,
                                 view, prepare as prepare_bug, execute as execute_bug)
        if args.command == 'builds':
            return list_builds(client, project=args.project, execution=args.execution)
        if args.command == 'resolve-builds':
            return resolved_build_options(client, args.bug)
        if args.command == 'product-find':
            return find_products(client, args.name)
        if args.command == 'bug-products':
            return bug_list(client, 'products', args.product)
        if args.command == 'bug-projects':
            return bug_list(client, 'projects', args.project)
        if args.command == 'bug-executions':
            return bug_list(client, 'executions', args.execution)
        if args.command == 'bug-detail':
            return view(client, args.bug)
        if args.command == 'bug-prepare':
            return prepare_bug(read_json(args.plan), client, args.plan)
        return execute_bug(args.review, args.confirm, client)
    if args.command == 'story-prepare':
        from story_actions import prepare as prepare_story
        return prepare_story(read_json(args.plan), client, args.plan)
    if args.command == 'story-submit':
        from story_actions import execute as execute_story
        return execute_story(args.review, args.confirm, client)
    if args.command == 'todo-prepare':
        from todo_actions import prepare as prepare_todo
        return prepare_todo(read_json(args.plan), client, args.plan)
    if args.command == 'todo-submit':
        from todo_actions import execute as execute_todo
        return execute_todo(args.review, args.confirm, client)
    if args.command == 'query':
        from query import detail as query_detail, list_entity
        if args.id:
            return query_detail(client, args.entity, args.id)
        scope = {key: value for key, value in (('product', args.product), ('project', args.project),
                                               ('execution', args.execution)) if value}
        return list_entity(client, args.entity, scope, args.status, args.keyword,
                           args.limit, args.all)
    if args.command == 'report':
        from reports import fetch as fetch_report
        return fetch_report(client, args.fields, args.year,
                            client.account if args.me else args.account, args.limit)
    if args.command == 'mine':
        from mine import fetch as fetch_mine
        return fetch_mine(client, args.entity, args.scope, args.limit, args.all)
    if args.command.rsplit('-', 1)[0] in ('product', 'project', 'execution'):
        from portfolio_actions import execute as portfolio_execute, prepare as portfolio_prepare
        entity, _, kind = args.command.partition('-')
        if kind == 'prepare':
            return portfolio_prepare(read_json(args.plan), client, entity, args.plan)
        return portfolio_execute(args.review, args.confirm, client)
    if args.command.rsplit('-', 1)[0] in ('testtask', 'testcase'):
        from test_actions import execute as test_execute, prepare as test_prepare
        entity, _, kind = args.command.partition('-')
        if kind == 'prepare':
            return test_prepare(read_json(args.plan), client, entity, args.plan)
        return test_execute(args.review, args.confirm, client)
    if args.command == "whoami":
        return {"account": client.account, "base_url": client.base}
    if args.command == "candidates":
        rows = client.candidates()
        return {"status": "ok" if rows else "no_tasks", "tasks": rows,
                "message": "可按任务编号或名称切换。" if rows else "当前没有可登记的候选任务，请先在禅道创建任务后继续。"}
    if args.command == "projects":
        return [{k: r.get(k) for k in ("id", "name", "status")} for r in client.listing("v1/projects", "projects", args.status)]
    if args.command == "executions":
        rows = client.listing(f"v2/projects/{ident(args.project)}/executions", "executions", args.status)
        return [{k: r.get(k) for k in ("id", "name", "status", "project")} for r in rows]
    if args.command == "tasks":
        rows = client.listing(f"v2/executions/{ident(args.execution)}/tasks", "tasks", args.status)
        return [{k: r.get(k) for k in ("id", "name", "execution", "status", "assignedTo", "left", "consumed", "story")} for r in rows]
    if args.command == "task":
        return client.task(ident(args.task))
    if args.command == "logs":
        # 接口对不存在的任务返回空列表，与「任务存在但没有日志」同形；先自证任务存在，避免给错数据。
        client.task(ident(args.task))
        return client.logs(ident(args.task))
    if args.command == "prepare":
        return prepare(args.plan, client)
    if args.command == "submit":
        return submit(args.review, args.confirm, client)


if __name__ == "__main__":
    try:
        emit(main())
    except SkillError as exc:
        emit({"status": "blocked", "message": str(exc)})
        sys.exit(2)
    except (OSError, ValueError, KeyError, TypeError):
        emit({"status": "blocked", "message": "本地文件或响应结构不符合约定，请核对脱敏样例；未输出原始数据。"})
        sys.exit(2)
