"""日志技能共用的日期、私有文件和结构化输出工具；仅使用标准库。"""

import datetime as dt
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


class SkillError(Exception):
    """可向用户展示、不包含凭据的业务错误。"""


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def stamp(value):
    """只保留数字：同一字段在不同接口里的写法不同（09:00 与 0900、2026-10-16 与 20261016）。"""
    return re.sub(r"\D", "", str(value or ""))


def plain_text(value):
    """富文本转纯文本：去标签并压缩空白，用于比对与预览。"""
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def rich_text(value):
    """纯文本包一层段落；已含标签的原样保留。禅道的描述字段都是富文本编辑器。"""
    text = str(value or "")
    if not text.strip() or re.search(r"<[a-z][^>]*>", text, re.I):
        return text
    return "<p>" + html.escape(text).replace("\n", "<br />") + "</p>"


def brief(value, limit=160):
    """预览用：富文本压成纯文本再截断，避免整篇正文刷屏（写入用的仍是完整值）。"""
    text = plain_text(value)
    return text[:limit] + "…（已截断）" if len(text) > limit else text


def account(value):
    """人员字段可能是嵌套对象（含 account）也可能就是账号字符串；统一取账号。"""
    return value.get("account", "") if isinstance(value, dict) else str(value or "")


def person(value):
    """人员字段是嵌套对象，压成「账号（姓名）」一行，避免把多层结构灌进上下文。"""
    if isinstance(value, dict) and value.get("account"):
        name = str(value.get("realname") or "").strip()
        if name and name != value["account"]:
            return f"{value['account']}（{name}）"
        return value["account"]
    return value


def is_missing(exc):
    """判断异常是否表示「记录不存在」。

    禅道对同一件事的表达不统一：v1 用 HTTP 400 或 404，v2 用 HTTP 200 + `… does not exist.`
    判定集中在这里，避免各模块各写一套字符串匹配。
    """
    text = str(exc)
    return ("HTTP 400" in text or "HTTP 404" in text
            or "does not exist" in text.lower() or "不存在" in text)


def missing_error(exc, label, value):
    """把接口表达的「记录不存在」翻译成可读提示；其它错误返回 None，由调用方原样抛出。

    调用写法：except SkillError as exc: raise (missing_error(exc, "需求", 42) or exc) from None
    """
    if is_missing(exc):
        return SkillError(f"没有找到编号 {value} 的{label}（接口返回未找到）；请核对编号。")
    return None


def state_dir():
    """个人状态放在用户主目录，与可分享的技能代码分离；环境变量可覆盖。"""
    root = Path(os.environ.get("ZENTAO_WORKLOG_HOME", Path.home() / ".zentao-worklog"))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        root.chmod(0o700)
    return root


def actions_dir():
    """实体写操作的预览与收据目录：所有模块共用一处。

    预览与收据都以 sha256 命名，模块之间不会撞名；集中在一个目录才好排查和清理，
    也避免每加一个实体就在状态目录里多开一个空文件夹。
    """
    path = state_dir() / "actions"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def private_write(path, value):
    """同目录原子替换，避免凭据、草稿和提交检查点出现半写入。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".worklog-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _trash_windows(path):
    """使用 Windows Shell 将文件或目录送入回收站。"""
    import ctypes
    from ctypes import wintypes

    class FileOperation(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                    ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                    ("fFlags", wintypes.WORD), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", wintypes.LPVOID), ("lpszProgressTitle", wintypes.LPCWSTR)]

    operation = FileOperation()
    operation.wFunc = 3  # FO_DELETE
    operation.pFrom = str(path) + "\0"  # API 要求双 NUL 结尾；字符串自身还会追加一个。
    operation.fFlags = 0x0040 | 0x0010 | 0x0004 | 0x0400
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    # Shell 偶尔返回异常状态却已移走目标；以原路径是否仍存在判断本次目标的结果。
    if path.exists():
        raise SkillError(f"系统回收站操作未完成（返回码 {result}），该文件仍在原处。")


def _trash_macos(path):
    # AppleScript 使用原始 Unicode 路径；JSON 默认的 \u 转义不是文件名字符。
    script = 'tell application "Finder" to delete POSIX file ' + json.dumps(str(path), ensure_ascii=False)
    subprocess.run(["osascript", "-e", script], check=True, timeout=30,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _trash_linux(path):
    for name, args in (("gio", ["trash"]), ("trash-put", [])):
        executable = shutil.which(name)
        if executable:
            subprocess.run([executable, *args, str(path)], check=True, timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
    raise SkillError("系统没有可用的桌面回收站命令，文件已保留。")


def move_to_trash(path, root=None):
    """只把个人状态根目录内的实体文件或目录送入系统回收站，不永久删除。"""
    path = Path(path)
    if not path.exists():
        return False
    root = Path(root or state_dir()).resolve()
    if path.is_symlink():
        raise SkillError("清理目标是符号链接，已拒绝操作。")
    target = path.resolve()
    if target == root or not target.is_relative_to(root):
        raise SkillError("清理目标超出禅道日志个人目录，已拒绝操作。")
    try:
        if os.name == "nt":
            _trash_windows(target)
        elif sys.platform == "darwin":
            _trash_macos(target)
        else:
            _trash_linux(target)
    except SkillError:
        raise
    except (OSError, subprocess.SubprocessError):
        if target.exists():
            raise SkillError("无法使用系统回收站，该文件仍在原处。") from None
    # 各平台回收站命令返回后统一核对，避免误报成功或已移走后误报失败。
    if target.exists():
        raise SkillError("系统回收站操作未完成，该文件仍在原处。")
    return True


def date_range(single=None, start=None, end=None, today=None):
    """默认本周一至今天；明确日期使用闭区间，不预填未来工作。"""
    today = today or dt.date.today()
    if single and (start or end):
        raise SkillError("--date 不能与 --start/--end 同时使用。")
    if bool(start) != bool(end):
        raise SkillError("日期范围需要同时提供 --start 和 --end。")
    try:
        first = dt.date.fromisoformat(single or start) if single or start else today - dt.timedelta(days=today.weekday())
        last = dt.date.fromisoformat(single or end) if single or end else today
    except ValueError as exc:
        raise SkillError("日期必须是 YYYY-MM-DD。") from exc
    if first > last or last > today:
        raise SkillError("日期范围倒置或包含未来日期；请调整为实际工作日期。")
    return first, last


def add_dates(parser):
    parser.add_argument("--date")
    parser.add_argument("--start")
    parser.add_argument("--end")
