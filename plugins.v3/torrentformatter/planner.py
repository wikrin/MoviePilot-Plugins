"""
只读计划与路径规则：渲染通过调用方注入，规划不连接下载器或数据库。
"""

import hashlib
import json
import re
from dataclasses import dataclass, replace
from pathlib import PurePosixPath, PureWindowsPath
from typing import Callable, Literal

from .models import (
    Change,
    ChangeCounts,
    DownloaderCapabilities,
    FormatterRules,
    MediaBrief,
    PreviewResponse,
    TorrentFile,
    TorrentInfo,
)


class SkipFile(ValueError):
    """
    文件元数据不足，保留原文件而非猜测名称。
    """


@dataclass(frozen=True)
class TorrentReadiness:
    """
    详情快照的就绪结论，不把普通下载状态当作冲突。

    :param code: 就绪、移动/校验、元数据未完成或文件明细缺失
    :param message: 未就绪的中文说明；就绪时为空
    """

    code: Literal["ready", "moving", "metadata", "files"]
    message: str = ""


def torrent_readiness(torrent: TorrentInfo) -> TorrentReadiness:
    """
    检查详情是否能安全规划，合法零字节文件仍须有文件明细。

    :param torrent: 包含文件查询结果的详情快照，不用于摘要列表
    :return: 明确的就绪原因码与说明
    """
    if torrent.moving:
        return TorrentReadiness("moving", "种子正在移动或校验，请等待完成")
    if not torrent.metadata_complete:
        return TorrentReadiness("metadata", "磁力种子元信息尚未就绪")
    if not torrent.files:
        return TorrentReadiness("files", "文件明细尚未就绪，不能安全规划")
    return TorrentReadiness("ready")


def is_windows_path(value: str) -> bool:
    """
    判断远端 Windows 盘符/UNC 路径，不读取宿主文件系统。

    :param value: 远端路径
    :return: 是否使用 Windows 语义
    """
    return bool(PureWindowsPath(value).drive)


def normalized_path(value: str) -> str:
    """
    规范分隔符，保留远端盘符及 UNC 根。

    :param value: 远端路径
    :return: POSIX 展示路径
    """
    flavor = PureWindowsPath if is_windows_path(value) else PurePosixPath
    return flavor(value.replace("\\", "/")).as_posix()


def path_key(value: str, windows: bool = False) -> str:
    """
    生成用于冲突与边界比较的路径键。

    :param value: 原路径
    :param windows: 是否大小写不敏感
    :return: 比较键
    """
    value = normalized_path(value)
    return value.casefold() if windows else value


def validate_relative(
    value: str, *, windows: bool = False, name_only: bool = False
) -> str:
    """
    验证模板目标在种子相对布局中，不允许越界/空名称。

    :param value: 已渲染的目标
    :param windows: 远端是否采用 Windows 路径语义
    :param name_only: 是否必须是单段显示名
    :return: 规范化相对路径
    :raises ValueError: 不安全或空目标
    """
    value = str(value).strip().replace("\\", "/")
    if not value or value == ".":
        raise ValueError("模板生成了空目标")
    if len(value) > 4096:
        raise ValueError("模板目标过长（最多 4096 个字符）")
    if value.startswith("/") or PureWindowsPath(value).drive:
        raise ValueError("模板必须生成相对路径，不能包含绝对路径或盘符")
    parts = value.split("/")
    if ".." in parts:
        raise ValueError("模板目标包含 ..，会越出保存目录")
    if any(ord(char) < 32 for char in value):
        raise ValueError("模板目标包含控制字符")
    value = PurePosixPath(value).as_posix()
    if name_only and len(PurePosixPath(value).parts) != 1:
        raise ValueError("种子显示名不能包含目录分隔符")
    if windows and any(
        re.search(r'[<>:"|?*]|[. ]$', part) for part in PurePosixPath(value).parts
    ):
        raise ValueError("目标包含 Windows 路径不支持的字符或结尾")
    if windows and any(
        re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part)
        for part in PurePosixPath(value).parts
    ):
        raise ValueError("目标包含 Windows 保留名称")
    return value


def validate_display_name(value: str) -> str:
    """
    独立显示名不是文件路径，不套用 Windows 文件名字符限制。

    :param value: 已渲染显示名
    :return: 安全的单段非空名称
    """
    value = str(value).strip()
    if not value or value in {".", ".."} or len(value) > 4096:
        raise ValueError("显示名为空、无效或过长")
    if "/" in value or "\\" in value or any(ord(char) < 32 for char in value):
        raise ValueError("种子显示名不能包含目录分隔符或控制字符")
    return value


def append_save_path(original: str, relative: str) -> str:
    """
    追加目录并去除最大尾部/前缀重叠，避免反复增加目录。

    :param original: 下载器的原保存目录
    :param relative: 合法相对目录
    :return: 远端目标目录
    """
    windows = is_windows_path(original)
    flavor = PureWindowsPath if windows else PurePosixPath
    root = flavor(original)
    if not root.is_absolute():
        raise ValueError("下载器保存路径不是绝对路径，无法安全追加")
    parts = PurePosixPath(relative).parts
    original_parts = root.parts
    overlap = 0
    for length in range(min(len(parts), len(original_parts)), 0, -1):
        old = original_parts[-length:]
        new = parts[:length]
        if (tuple(x.casefold() for x in old) if windows else old) == (
            tuple(x.casefold() for x in new) if windows else new
        ):
            overlap = length
            break
    return root.joinpath(*parts[overlap:]).as_posix()


def _snapshot_values(snapshot: TorrentInfo) -> dict[str, object]:
    """
    投影执行所依赖的身份、布局与文件选择，不包含标签或优先级高低。

    :param snapshot: 实时种子详情
    :return: 按稳定文件索引排列的可序列化校验字段
    """
    windows = is_windows_path(snapshot.save_path)
    files = sorted(
        snapshot.files,
        key=lambda file: (
            file.index is None,
            file.index if file.index is not None else -1,
            path_key(file.name, windows),
            file.size,
            file.selected,
        ),
    )
    return {
        "hash": snapshot.hash.lower(),
        "name": snapshot.name,
        "save_path": path_key(snapshot.save_path, windows),
        "total_size": snapshot.total_size,
        "auto_tmm": snapshot.auto_tmm,
        "files": [
            {
                "index": file.index,
                "name": path_key(file.name, windows),
                "size": file.size,
                "selected": file.selected,
            }
            for file in files
        ],
    }


def snapshot_fingerprint(snapshot: TorrentInfo) -> str:
    """
    固定身份/布局/选择，不因普通状态、分类、标签或优先级高低而失效。

    :param snapshot: 实时种子快照
    :return: SHA256 稳定字段摘要，就绪与排除须另行核验
    """
    return fingerprint(_snapshot_values(snapshot))


def snapshot_changed_fields(
    before: TorrentInfo, actual: TorrentInfo
) -> tuple[str, ...]:
    """
    复用摘要投影定位变化字段，不向日志输出整份文件列表。

    :param before: 计划冻结时的详情
    :param actual: 执行前实时详情
    :return: 实质变化的字段名，文件变化统一汇总为 files
    """
    old, live = _snapshot_values(before), _snapshot_values(actual)
    return tuple(key for key in old if old[key] != live[key])


def fingerprint(value: object) -> str:
    """
    计算 JSON 校验要素的稳定摘要。

    :param value: 可序列化值
    :return: SHA256 摘要
    """
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def exclusion_reason(torrent: TorrentInfo, exclude_dirs: str, exclude_tags: str) -> str:
    """
    保存目录按边界排除，标签按精确值排除，空白标签匹配无标签种子。

    :param torrent: 当前种子
    :param exclude_dirs: 每行一个目录
    :param exclude_tags: 逗号分隔的标签
    :return: 被阻止原因或空字符串
    """
    windows = is_windows_path(torrent.save_path)
    current = path_key(torrent.save_path, windows).rstrip("/")
    for value in exclude_dirs.splitlines():
        value = value.strip()
        if value:
            key = path_key(value, windows).rstrip("/")
            if current == key or current.startswith(key + "/"):
                return f"保存路径命中排除目录：{value}"
    if exclude_tags:
        blocked_tags = {value.strip() for value in exclude_tags.split(",")}
        tags = set(torrent.tags) or {""}
        common = blocked_tags & tags
        if common:
            return (
                "命中排除标签："
                + "、".join(sorted(common - {""}))
                + ("未设置标签" if "" in common else "")
            )
    return ""


def summarize_changes(changes: list[Change], total_files: int) -> ChangeCounts:
    """
    分别统计实际会执行的变化与未处理原因。

    :param changes: 完整差异列表
    :param total_files: 种子全部文件数
    :return: 影响范围
    """
    count = ChangeCounts(total_files=total_files)
    for change in changes:
        if change.status == "changed":
            field = "files" if change.kind == "file" else change.kind
            setattr(count, field, getattr(count, field) + 1)
        elif change.status == "skipped":
            count.skipped += 1
        elif change.status == "unsupported":
            count.unsupported += 1
    count.affected_files = total_files if count.save_path else count.files
    return count


def build_format_preview(
    *,
    downloader: str,
    torrent: TorrentInfo,
    rules: FormatterRules,
    capabilities: DownloaderCapabilities,
    render_torrent: Callable[[str], str],
    render_file: Callable[[TorrentFile, str], str],
    media: MediaBrief | None = None,
    exclude_words: tuple[str, ...] = (),
    blocked_reason: str = "",
) -> PreviewResponse:
    """
    生成相同规则下的目录/显示名/文件计划，只调用注入的只读渲染器。

    :param downloader: 当前下载器名称
    :param torrent: 原始快照
    :param rules: 有效规则
    :param capabilities: 当前下载器能力
    :param render_torrent: 种子级模板渲染器
    :param render_file: 文件级模板渲染器
    :param media: 已识别媒体摘要
    :param exclude_words: 系统整理屏蔽词（正则）
    :param blocked_reason: 已处理/排除等整体阻止原因
    :return: 不含 token 的只读预览
    """
    changes: list[Change] = []
    warnings: list[str] = []
    windows = is_windows_path(torrent.save_path)
    files = list(torrent.files)
    new_save_path, new_name, auto_tmm = (
        torrent.save_path,
        torrent.name,
        torrent.auto_tmm,
    )
    blocked_reason = blocked_reason or torrent_readiness(torrent).message
    if rules.rename_file and (
        any(file.index is None for file in torrent.files)
        or len({file.index for file in torrent.files}) != len(torrent.files)
    ):
        blocked_reason = blocked_reason or "文件缺少唯一稳定索引，不能安全改名"

    def scalar(kind: str, original: str, template: str, supported: bool) -> str:
        """
        规划一个种子级字符串目标。

        :param kind: 显示名或保存目录
        :param original: 原值
        :param template: Jinja2 模板
        :param supported: 下载器是否支持
        :return: 可支持的预计值
        """
        status, target, reason = "unchanged", original, ""
        if blocked_reason:
            status, reason = "skipped", blocked_reason
        elif template:
            try:
                rendered = render_torrent(template)
                value = (
                    validate_display_name(rendered)
                    if kind == "name"
                    else validate_relative(rendered, windows=windows)
                )
                target = (
                    append_save_path(original, value) if kind == "save_path" else value
                )
                different = (
                    target != original
                    if kind == "name"
                    else path_key(target, windows) != path_key(original, windows)
                )
                if different:
                    status = "changed" if supported else "unsupported"
                    reason = "下载器不支持独立种子显示名" if not supported else ""
            except Exception as error:
                status, reason, target = "invalid", str(error), original
        changes.append(
            Change(
                id=kind,
                kind=kind,
                before=original,
                after=target,
                status=status,
                reason=reason,
            )
        )
        return target if status == "changed" else original

    new_save_path = scalar(
        "save_path", torrent.save_path, rules.format_save_path, capabilities.save_path
    )
    new_name = scalar(
        "name",
        torrent.name,
        rules.format_torrent_name if rules.rename_torrent else "",
        capabilities.rename_torrent,
    )
    file_template = (
        rules.format_tv_path
        if media and media.media_type == "tv"
        else rules.format_movie_path
    )
    for position, file in enumerate(torrent.files):
        status, target, reason = "unchanged", file.name, ""
        if blocked_reason:
            status, reason = "skipped", blocked_reason
        elif rules.rename_file and file_template:
            try:
                if not file.selected:
                    raise SkipFile("未选中下载或属于 padding 文件，保留原路径")
                for keyword in exclude_words:
                    if keyword and re.search(keyword, file.name, re.IGNORECASE):
                        raise SkipFile(f"命中系统屏蔽词：{keyword}")
                target = validate_relative(
                    render_file(file, file_template), windows=windows
                )
                if path_key(target, windows) != path_key(file.name, windows):
                    if not capabilities.rename_file or (
                        not capabilities.file_move
                        and PurePosixPath(target).parent
                        != PurePosixPath(file.name).parent
                    ):
                        status, reason = (
                            "unsupported",
                            "下载器不支持文件跨目录重排"
                            if capabilities.rename_file
                            else "下载器不支持文件改名",
                        )
                    else:
                        status = "changed"
                        files[position] = replace(file, name=target)
            except SkipFile as error:
                status, reason, target = "skipped", str(error), file.name
            except Exception as error:
                status, reason, target = "invalid", str(error), file.name
        changes.append(
            Change(
                id=f"file:{file.index if file.index is not None else position}",
                kind="file",
                file_id=file.index,
                before=file.name,
                after=target,
                status=status,
                reason=reason,
            )
        )

    # 包括未改变的文件一起校验，禁止覆盖；交换名称也不能依靠调用顺序碰运气。
    owners: dict[str, int] = {}
    conflicts: set[int] = set()
    original_names = {
        path_key(file.name, windows): file.index for file in torrent.files
    }
    for position, file in enumerate(files):
        key = path_key(file.name, windows)
        if key in owners:
            conflicts.update((position, owners[key]))
        owners[key] = position
        if (
            file.name != torrent.files[position].name
            and key in original_names
            and original_names[key] != file.index
        ):
            conflicts.add(position)
    for index, change in enumerate(changes):
        if change.kind == "file" and change.status == "changed":
            position = next(
                (
                    i
                    for i, file in enumerate(torrent.files)
                    if file.index == change.file_id
                ),
                -1,
            )
            if position in conflicts:
                changes[index] = change.model_copy(
                    update={
                        "status": "invalid",
                        "reason": "目标与其他文件冲突；不会覆盖或交换文件",
                    }
                )

    if (
        new_save_path != torrent.save_path
        and torrent.auto_tmm
        and capabilities.auto_tmm
    ):
        auto_tmm = False
        changes.append(
            Change(
                id="auto_tmm",
                kind="auto_tmm",
                before="true",
                after="false",
                status="changed",
                reason="移动保存路径前需关闭自动管理；恢复时还原",
            )
        )
    # Transmission 的单文件根名称会随文件改名联动，而不是独立显示名操作。
    if (
        capabilities.kind == "transmission"
        and len(files) == 1
        and PurePosixPath(torrent.files[0].name).parent == PurePosixPath(".")
    ):
        new_name = files[0].name
    after = replace(
        torrent,
        name=new_name,
        save_path=new_save_path,
        files=tuple(files),
        auto_tmm=auto_tmm,
    )
    invalid = [change.reason for change in changes if change.status == "invalid"]
    warnings.extend(
        dict.fromkeys(
            change.reason
            for change in changes
            if change.status in {"skipped", "unsupported"} and change.reason
        )
    )
    if rules.rename_file:
        warnings.append(
            "文件布局修改是实验功能，可能影响辅种及已整理记录；确认差异后再执行。"
        )
    status = (
        "blocked"
        if blocked_reason or invalid
        else (
            "ready"
            if any(change.status == "changed" for change in changes)
            else "no_changes"
        )
    )
    return PreviewResponse(
        status=status,
        message=blocked_reason
        or (
            "；".join(dict.fromkeys(invalid))
            if invalid
            else (
                "预览已就绪，尚未修改任何文件"
                if status == "ready"
                else "没有可执行的变更"
            )
        ),
        downloader=downloader,
        torrent_hash=torrent.hash,
        before=torrent,
        after=after,
        media=media,
        rules=rules,
        capabilities=capabilities,
        changes=changes,
        counts=summarize_changes(changes, len(torrent.files)),
        warnings=warnings,
    )
