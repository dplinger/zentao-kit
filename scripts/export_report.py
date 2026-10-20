"""将模型整理的 Markdown 报告保存到本地，并记住个人输出目录；仅用标准库。"""

import argparse
import os
from pathlib import Path
import sys

from common import SkillError, emit, private_write, read_json, state_dir


def downloads_dir():
    """优先读取系统下载目录配置，支持 Windows 下载文件夹重定向。"""
    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
                value, _ = winreg.QueryValueEx(key, "{374DE290-123F-4565-9164-39C4925E467B}")
                return Path(os.path.expandvars(value)).expanduser()
        except OSError:
            pass
    elif sys.platform.startswith("linux"):
        config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "user-dirs.dirs"
        if config.exists():
            for line in config.read_text(encoding="utf-8").splitlines():
                if line.startswith("XDG_DOWNLOAD_DIR="):
                    value = line.split("=", 1)[1].strip().strip('"')
                    path = Path(value.replace("${HOME}", str(Path.home())).replace("$HOME", str(Path.home())))
                    if path.is_absolute():
                        return path
    return Path.home() / "Downloads"


def export_report(source, name=None, output_dir=None, remember=True):
    """显式目录优先，其次个人偏好，最后系统下载目录；同名报告自动编号。"""
    source = Path(source)
    name = name or source.name
    if (not name or any(c in name for c in '/\\<>:"|?*')
            or Path(name).suffix.lower() != ".md" or name.endswith((" ", "."))):
        raise SkillError("报告名称必须是不含目录或特殊字符的 .md 文件名。")
    content = source.read_text(encoding="utf-8-sig")
    if not content.strip():
        raise SkillError("报告内容为空，请先整理工作记录。")
    preferences_path = state_dir() / "preferences.json"
    preferences = read_json(preferences_path) if preferences_path.exists() else {}
    if not isinstance(preferences, dict):
        raise SkillError("个人目录偏好格式错误，请修复 state/preferences.json。")
    folder = Path(output_dir or preferences.get("report_output_dir") or downloads_dir()).expanduser().resolve()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    index = 1
    # 排他创建，既保留原文件，也避免并发导出时覆盖。
    while True:
        try:
            stream = target.open("x", encoding="utf-8", newline="\n")
            break
        except FileExistsError:
            index += 1
            target = folder / f"{Path(name).stem}-{index}.md"
    with stream:
        stream.write(content)
    result = {"status": "saved", "path": str(target), "directory_remembered": False}
    if output_dir and remember:
        preferences["report_output_dir"] = str(folder)
        try:
            private_write(preferences_path, preferences)
            result["directory_remembered"] = True
        except OSError:
            result["warning"] = "报告已保存，但目录偏好未能保存。"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="整理完成的 Markdown 草稿")
    parser.add_argument("--name", help="输出文件名，默认沿用草稿名")
    parser.add_argument("--output-dir", help="指定输出目录，成功后默认记住")
    parser.add_argument("--no-remember", action="store_true", help="指定目录仅用于本次")
    args = parser.parse_args()
    try:
        emit(export_report(args.input, args.name, args.output_dir, not args.no_remember))
    except (SkillError, OSError, ValueError) as exc:
        emit({"status": "error", "message": str(exc)})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
