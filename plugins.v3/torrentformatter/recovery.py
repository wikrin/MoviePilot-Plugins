"""
恢复计划：验证稳定文件身份及旧备份顺序，完全可恢复才允许执行。
"""

from dataclasses import replace
from pathlib import PurePosixPath, PureWindowsPath

from .models import (
    Change,
    DownloaderCapabilities,
    FormatterRules,
    MediaBrief,
    PreviewResponse,
    TorrentInfo,
)
from .planner import is_windows_path, path_key, summarize_changes, validate_relative


def build_recovery_preview(
    *,
    downloader: str,
    current: TorrentInfo,
    original: TorrentInfo,
    capabilities: DownloaderCapabilities,
    rules: FormatterRules,
    media: MediaBrief | None = None,
    legacy: bool = False,
) -> PreviewResponse:
    """
    生成逆向计划，旧备份只有数量/索引顺序/大小可验证时才匹配。

    :param downloader: 当前实际目标下载器
    :param current: 实时布局
    :param original: 原始备份
    :param capabilities: 当前下载器能力
    :param rules: 仅用于展示，不重新渲染恢复路径
    :param media: 历史媒体摘要
    :param legacy: 是否来自没有稳定索引的旧备份
    :return: 可执行或明确阻止的恢复预览
    """
    changes: list[Change] = []
    errors: list[str] = []
    windows = is_windows_path(current.save_path)
    files = list(current.files)
    if current.moving or not current.metadata_complete:
        errors.append("种子正在移动/校验或元信息未就绪，请等待后重新预览")
    if original.hash != current.hash:
        errors.append("实际种子 Hash 与备份不一致，可能是转种 ID 被重用；拒绝恢复")
    if is_windows_path(original.save_path) != windows:
        errors.append("备份与当前下载器使用不同路径语义，请核对转种后的挂载位置")
    flavor = PureWindowsPath if windows else PurePosixPath
    if not flavor(
        original.save_path
    ).is_absolute() or ".." in original.save_path.replace("\\", "/").split("/"):
        errors.append("原保存路径无效，不能安全恢复")
    if original.total_size and original.total_size != current.total_size:
        errors.append("种子总大小与原始备份不一致")

    live_files = sorted(
        current.files, key=lambda file: file.index if file.index is not None else -1
    )
    if legacy and len(live_files) != len(original.files):
        # 旧 Transmission 备份过滤过 padding；保留当前 padding 并验证其余索引。
        filtered = [
            file for file in live_files if "_____padding_file_" not in file.name
        ]
        if len(filtered) == len(original.files):
            live_files = filtered
    if len(live_files) != len(original.files):
        errors.append("文件数量与备份不一致，拒绝按位置盲目恢复")
    elif len({file.index for file in live_files}) != len(live_files) or any(
        file.index is None for file in live_files
    ):
        errors.append("当前文件缺少唯一稳定索引，无法安全匹配")
    else:
        originals = {file.index: file for file in original.files}
        indexed = all(file.index is not None for file in original.files) and len(
            originals
        ) == len(original.files)
        if not indexed and not legacy:
            errors.append("备份文件索引不完整，无法安全匹配")
        else:
            for position, live in enumerate(live_files):
                old = originals.get(live.index) if indexed else original.files[position]
                if old is None or live.size != old.size:
                    errors.append(f"文件索引 {live.index} 的大小/身份与备份不一致")
                    continue
                target, status, reason = old.name, "unchanged", ""
                try:
                    target = validate_relative(target, windows=windows)
                    if path_key(target, windows) != path_key(live.name, windows):
                        if not capabilities.rename_file or (
                            not capabilities.file_move
                            and PurePosixPath(target).parent
                            != PurePosixPath(live.name).parent
                        ):
                            status, reason = (
                                "unsupported",
                                "当前下载器不支持恢复此跨目录文件布局",
                            )
                        else:
                            status = "changed"
                            for index, file in enumerate(files):
                                if file.index == live.index:
                                    files[index] = replace(file, name=target)
                                    break
                except ValueError as error:
                    status, reason = "invalid", str(error)
                changes.append(
                    Change(
                        id=f"file:{live.index}",
                        kind="file",
                        file_id=live.index,
                        before=live.name,
                        after=target,
                        status=status,
                        reason=reason,
                    )
                )

    original_names = {
        path_key(file.name, windows): file.index for file in current.files
    }
    target_names: dict[str, int | None] = {}
    conflicts = set()
    for file in files:
        key = path_key(file.name, windows)
        if key in target_names:
            conflicts.update((file.index, target_names[key]))
        target_names[key] = file.index
        if (
            file.name
            != next(item.name for item in current.files if item.index == file.index)
            and key in original_names
            and original_names[key] != file.index
        ):
            conflicts.add(file.index)
    changes = [
        change.model_copy(
            update={
                "status": "invalid",
                "reason": "恢复目标与当前其他文件冲突，不会覆盖文件",
            }
        )
        if change.kind == "file"
        and change.status == "changed"
        and change.file_id in conflicts
        else change
        for change in changes
    ]

    changed_path = path_key(original.save_path, windows) != path_key(
        current.save_path, windows
    )
    changes.append(
        Change(
            id="save_path",
            kind="save_path",
            before=current.save_path,
            after=original.save_path,
            status="changed"
            if changed_path and capabilities.save_path
            else "unsupported"
            if changed_path
            else "unchanged",
            reason="当前下载器不支持保存路径恢复"
            if changed_path and not capabilities.save_path
            else "",
        )
    )
    implicit_name = (
        capabilities.kind == "transmission"
        and len(files) == 1
        and PurePosixPath(files[0].name).parent == PurePosixPath(".")
    )
    changed_name = current.name != original.name
    name_supported = capabilities.rename_torrent or (
        implicit_name and files[0].name == original.name
    )
    name_status = (
        "changed"
        if changed_name and capabilities.rename_torrent
        else "unsupported"
        if changed_name and not name_supported
        else "unchanged"
    )
    changes.append(
        Change(
            id="name",
            kind="name",
            before=current.name,
            after=original.name,
            status=name_status,
            reason="随根文件名恢复，无独立显示名操作"
            if changed_name and implicit_name and name_supported
            else "当前下载器不支持恢复独立种子显示名"
            if name_status == "unsupported"
            else "",
        )
    )
    paused_tmm = current.auto_tmm and changed_path and capabilities.auto_tmm
    if paused_tmm:
        changes.append(
            Change(
                id="auto_tmm:pause",
                kind="auto_tmm",
                before="true",
                after="false",
                status="changed",
                reason="移动前暂停自动管理",
            )
        )
    before_tmm = False if paused_tmm else current.auto_tmm
    if original.auto_tmm != before_tmm:
        changes.append(
            Change(
                id="auto_tmm:restore",
                kind="auto_tmm",
                before=str(before_tmm).lower(),
                after=str(original.auto_tmm).lower(),
                status="changed" if capabilities.auto_tmm else "unsupported",
                reason="路径恢复完成后还原自动管理"
                if capabilities.auto_tmm
                else "当前下载器不支持原自动管理状态",
            )
        )
    errors.extend(
        change.reason
        for change in changes
        if change.status in {"invalid", "unsupported"}
    )
    for reason in dict.fromkeys(errors):
        changes.append(
            Change(
                id=f"validation:{len(changes)}",
                kind="save_path",
                before=current.save_path,
                after=current.save_path,
                status="invalid",
                reason=reason,
            )
        )
    after = replace(
        current,
        name=original.name,
        save_path=original.save_path,
        files=tuple(files),
        auto_tmm=original.auto_tmm,
    )
    changed = any(change.status == "changed" for change in changes)
    return PreviewResponse(
        mode="recover",
        status="blocked" if errors else "ready",
        message="；".join(dict.fromkeys(errors))
        if errors
        else "恢复预览已就绪，尚未修改任何文件"
        if changed
        else "已与备份一致；确认后仅还原下载记录并清理历史",
        downloader=downloader,
        torrent_hash=current.hash,
        before=current,
        after=after,
        capabilities=capabilities,
        media=media,
        rules=rules,
        changes=changes,
        counts=summarize_changes(changes, len(current.files)),
        warnings=["只在全部布局及下载记录确认恢复后清除备份。"]
        + (
            ["旧版备份按稳定索引顺序和文件大小核验，不猜测文件对应关系。"]
            if legacy
            else []
        ),
    )
