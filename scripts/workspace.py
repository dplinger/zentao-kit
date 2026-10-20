"""管理本次操作的内部文件；全部提交验证成功后立即清理。"""

import argparse
from pathlib import Path
import re
import uuid

from common import SkillError, emit, move_to_trash, private_write, read_json, state_dir


def storage_info(acknowledge=False):
    """返回实际存放位置；告知记录合并到已有偏好，不读取鉴权文件。"""
    root = state_dir().resolve()
    path = root / 'preferences.json'
    preferences = read_json(path) if path.exists() else {}
    first_use = preferences.get('storage_notice_path') != str(root)
    if acknowledge:
        preferences['storage_notice_path'] = str(root)
        private_write(path, preferences)
    return {'path': str(root), 'first_use': first_use,
            'message': f'小提示：登录信息和临时草稿保存在 {root}，支持自定义位置，想更换时告诉我即可。日志提交成功后，本次临时文件会自动清理。'}


def create():
    cleanup_history()  # 每次开始时补清历史已完成批次，不需要定时任务。
    run = uuid.uuid4().hex
    folder = state_dir() / "runs" / run
    folder.mkdir(parents=True)
    return {"run": run, **{key: str((folder / name).resolve()) for key, name in
                           (("records", "records.json"), ("plan", "plan.json"), ("report", "report.md"))}}


def cleanup_verified(review):
    """仅清理已核验批次及其内部工作文件，不删除用户指定的外部草稿。"""
    from zentao import load_review
    root = state_dir().resolve()
    snapshot = load_review(review)
    receipt_path = root / "receipts" / f"{review}.json"
    receipt = read_json(receipt_path)
    expected = {str(row["task"]) for row in snapshot["rows"]}
    if not expected or any(receipt.get("tasks", {}).get(task, {}).get("status") != "verified" for task in expected):
        raise SkillError("提交尚未全部验证，保留恢复资料。")
    source = Path(snapshot["source_path"]).resolve()
    paths = []
    # 同一草稿修改产生的旧预览也属于本次操作；有不确定收据时整批保留。
    for path in (root / "reviews").glob("*.json"):
        candidate = read_json(path)
        if Path(candidate.get("source_path", "")).resolve() != source:
            continue
        if not re.fullmatch(r"[a-f0-9]{64}", path.stem):
            continue
        old_receipt = root / "receipts" / path.name
        if old_receipt.exists():
            tasks = read_json(old_receipt).get("tasks", {})
            if not tasks or any(t.get("status") != "verified" for t in tasks.values()):
                raise SkillError("同一草稿还有未核验收据，暂不清理。")
        paths.extend([path, path.with_name(path.stem + "-日志预览.md"), old_receipt])
    folder = source.parent
    internal = (source.name == "plan.json" and folder.parent == root / "runs"
                and re.fullmatch(r"[a-f0-9]{32}", folder.name))
    if internal:
        paths.append(folder)
    # 先验证所有目标，拒绝链接或跳出用户状态目录。
    for path in paths:
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise SkillError("清理目标异常，已保留资料。")
    # 收据最后移入回收站；清理中断时仍留有成功凭据供后续核对。
    paths.sort(key=lambda p: (p.parent == root / "receipts", p.parent == root / "reviews"))
    for path in paths:
        move_to_trash(path, root)
    return {"status": "cleaned", "message": "本次预览、收据和内部过程文件已移入系统回收站。"}


def cleanup_discarded(review):
    """清理用户明确放弃且从未提交的预览；发现任何收据时保留恢复资料。"""
    from zentao import load_review
    root = state_dir().resolve()
    snapshot = load_review(review)
    source = Path(snapshot["source_path"]).resolve()
    paths = []
    for path in (root / "reviews").glob("*.json"):
        candidate = read_json(path)
        if Path(candidate.get("source_path", "")).resolve() != source:
            continue
        if not re.fullmatch(r"[a-f0-9]{64}", path.stem):
            continue
        receipt = root / "receipts" / path.name
        if receipt.exists():
            raise SkillError("本次草稿存在提交收据，不能按未提交预览清理；请先回查提交结果。")
        paths.extend([path, path.with_name(path.stem + "-日志预览.md")])
    folder = source.parent
    internal = (source.name == "plan.json" and folder.parent == root / "runs"
                and re.fullmatch(r"[a-f0-9]{32}", folder.name))
    if internal:
        paths.append(folder)
    for path in paths:
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise SkillError("清理目标异常，已保留资料。")
    # 先清理过程文件，最后移走预览快照；中途失败时仍可凭 review 重试。
    paths.sort(key=lambda path: path.suffix == ".json" and path.parent == root / "reviews")
    for path in paths:
        move_to_trash(path, root)
    return {"status": "cleaned", "message": "已放弃本次预览并移入系统回收站；未向禅道写入数据。"}


def cleanup_history():
    """补清旧版本遗留的已核验批次；未提交、未知结果、损坏文件均保留。"""
    root = state_dir()
    result = {'cleaned': [], 'retained': []}
    if (root / 'submit.lock').exists():
        return {'status': 'busy', **result}
    for path in list((root / 'receipts').glob('*.json')):
        try:
            cleanup_verified(path.stem)
            result['cleaned'].append(path.stem)
        except (OSError, ValueError, KeyError, SkillError):
            result['retained'].append(path.stem)
    return {'status': 'checked', **result}


def cleanup(run, review=None, report=None, discard=False):
    """限定清理当前运行的固定文件，不递归删除；未确认写入的资料必须保留。"""
    if not re.fullmatch(r"[a-f0-9]{32}", run):
        raise SkillError("运行编号无效。")
    folder = state_dir() / "runs" / run
    if folder.is_symlink() or folder.resolve().parent != (state_dir() / "runs").resolve():
        raise SkillError("运行目录超出内部工作区。")
    if review:
        from zentao import load_review
        snapshot = load_review(review)
        if Path(snapshot["source_path"]).resolve() != (folder / "plan.json").resolve():
            raise SkillError("预览不属于本次运行。")
        return cleanup_discarded(review) if discard else cleanup_verified(review)
    elif report:
        if discard:
            raise SkillError("--discard 仅用于明确放弃尚未提交的禅道预览。")
        # 仅报告模式不能借导出成功清掉未完成的禅道登记资料。
        if (folder / "plan.json").exists():
            raise SkillError("存在禅道草稿，必须先验证提交结果。")
        target = Path(report).resolve()
        if target.is_relative_to(folder.resolve()) or not target.is_file() or target.suffix.lower() != ".md":
            raise SkillError("请提供已导出到运行目录以外的 Markdown 报告。")
        if target.read_text(encoding="utf-8-sig") != (folder / "report.md").read_text(encoding="utf-8-sig"):
            raise SkillError("导出内容与草稿不一致，保留草稿。")
    else:
        raise SkillError("清理前须提供已验证的 review 或已导出的 report。")
    move_to_trash(folder, state_dir().resolve())
    return {"status": "cleaned", "run": run, "message": "已将本次报告过程文件移入系统回收站。"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("create")
    p = sub.add_parser('storage-info', help='查询实际个人目录及首次使用提示')
    p.add_argument('--acknowledge', action='store_true', help='展示提示后记录已告知')
    sub.add_parser("cleanup-history", help="仅清理历史已全部核验的提交")
    p = sub.add_parser("cleanup")
    p.add_argument("--run", required=True)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--review")
    group.add_argument("--report")
    p.add_argument("--discard", action="store_true", help="用户明确放弃未提交预览时清理过程文件")
    args = parser.parse_args()
    try:
        if args.command == 'storage-info':
            emit(storage_info(args.acknowledge))
        elif args.command == 'cleanup-history':
            emit(cleanup_history())
        else:
            emit(create() if args.command == "create" else cleanup(args.run, args.review, args.report, args.discard))
    except (SkillError, OSError, ValueError, KeyError) as exc:
        emit({"status": "blocked", "message": str(exc) if isinstance(exc, SkillError) else "工作区不可用，已保留资料。"})
        raise SystemExit(2)
