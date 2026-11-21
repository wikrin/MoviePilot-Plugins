"""
短期预览 token、可回读的逐项执行和下载记录路径投影。
"""

import re
import threading
import time
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Callable
from uuid import uuid4

from app.sdk.logging import logger

from .downloaders import Downloader
from .models import (
    Change,
    ExecutionResponse,
    FrozenPlan,
    OperationLog,
    TorrentInfo,
    identity_key,
    snapshot_dict,
    snapshot_from_dict,
)
from .planner import is_windows_path, normalized_path, path_key


class PreviewExpired(ValueError):
    """
    预览过期、被重载清除或已经使用，需重新生成。
    """


@dataclass
class _Ticket:
    """
    内存中的预览及执行回执，不持久化未执行的计划。

    :param plan: 服务端冻结计划
    :param expires_at: 单调时钟到期值
    :param claimed: 是否已开始执行
    :param result: 已完成的调用回执
    """

    plan: FrozenPlan
    expires_at: float
    claimed: bool = False
    result: ExecutionResponse | None = None


class PreviewStore:
    """
    有界、线程安全的实例级预览缓存，默认十分钟和五十个计划。
    """

    def __init__(
        self,
        ttl: int = 600,
        capacity: int = 50,
        clock: Callable[[], float] = time.monotonic,
    ):
        """
        创建预览缓存。

        :param ttl: token 生命周期秒数
        :param capacity: 最大计划数
        :param clock: 可注入的单调时钟
        """
        self.ttl, self.capacity, self.clock = ttl, capacity, clock
        self._items: OrderedDict[str, _Ticket] = OrderedDict()
        self._lock = threading.Lock()

    def clear(self) -> None:
        """
        重载/停止时使所有预览失效。
        """
        with self._lock:
            self._items.clear()

    def invalidate_history(self, history_id: str) -> None:
        """
        删除备份后使关联预览/旧回执失效，不影响其他记录。

        :param history_id: 已删除的原记录复合身份
        """
        with self._lock:
            for token, ticket in list(self._items.items()):
                preview = ticket.plan.preview
                if (
                    ticket.plan.history_id == history_id
                    or identity_key(preview.downloader, preview.torrent_hash)
                    == history_id
                ):
                    del self._items[token]

    def put(self, plan: FrozenPlan) -> FrozenPlan:
        """
        保存拷贝并返回对外副本，调用方无法修改缓存路径。

        :param plan: 不含 token 的计划
        :return: 带 token 的拷贝
        """
        token = uuid4().hex
        plan = deepcopy(plan)
        preview = plan.preview.model_copy(
            update={"preview_token": token, "expires_in": self.ttl}, deep=True
        )
        plan = replace(plan, preview=preview)
        with self._lock:
            for key in list(self._items):
                ticket = self._items[key]
                if ticket.expires_at <= self.clock() and not (
                    ticket.claimed and ticket.result is None
                ):
                    del self._items[key]
            while len(self._items) >= self.capacity:
                removable = next(
                    (
                        key
                        for key, ticket in self._items.items()
                        if not ticket.claimed or ticket.result is not None
                    ),
                    None,
                )
                if removable is None:
                    raise PreviewExpired("预览缓存正忙，请稍后再试")
                del self._items[removable]
            self._items[token] = _Ticket(plan, self.clock() + self.ttl)
        return deepcopy(plan)

    def discard_unclaimed(self, token: str) -> bool:
        """
        仅丢弃尚未领取且没有回执的计划，重试不能清除已开始的操作。

        :param token: 自动流程放弃的不透明 token
        :return: 确认尚未执行并移除时返回 True，未知/已领取/已有回执返回 False
        """
        with self._lock:
            ticket = self._items.get(token)
            if ticket is None or ticket.claimed or ticket.result is not None:
                return False
            del self._items[token]
            return True

    def lookup(self, token: str) -> tuple[FrozenPlan, ExecutionResponse | None]:
        """
        读取计划与幂等回执，拒绝未知或已到期的 token。

        :param token: 客户端的不透明 token
        :return: 计划与可选回执的深拷贝
        """
        with self._lock:
            ticket = self._items.get(token)
            if ticket is None or ticket.expires_at <= self.clock():
                self._items.pop(token, None)
                raise PreviewExpired("预览已过期或插件已重载，请重新预览")
            if ticket.claimed and ticket.result is None:
                raise PreviewExpired("此预览已开始执行，请查询操作结果，不要重复提交")
            return deepcopy(ticket.plan), deepcopy(ticket.result)

    def claim(self, token: str) -> None:
        """
        以单次使用方式领取 token，外层执行锁串行保护状态校验。

        :param token: 已校验的 token
        """
        with self._lock:
            ticket = self._items.get(token)
            if ticket is None or ticket.expires_at <= self.clock() or ticket.claimed:
                raise PreviewExpired("此预览已经使用或过期，请重新预览")
            ticket.claimed = True

    def complete(self, token: str, result: ExecutionResponse) -> None:
        """
        保存重复请求可读取的结果，不进行二次执行。

        :param token: 已领取 token
        :param result: 执行回执
        """
        with self._lock:
            if ticket := self._items.get(token):
                ticket.result = result.model_copy(deep=True)
                ticket.expires_at = self.clock() + self.ttl


def change_confirmed(change: Change, torrent: TorrentInfo) -> bool:
    """
    使用精确状态回读确认一项修改。

    :param change: 预期差异
    :param torrent: 实时快照
    :return: 是否已经生效
    """
    windows = is_windows_path(torrent.save_path)
    if change.kind == "auto_tmm":
        return torrent.auto_tmm == (change.after == "true")
    if change.kind == "name":
        return torrent.name == change.after
    if change.kind == "save_path":
        return (
            path_key(torrent.save_path, windows) == path_key(change.after, windows)
            and not torrent.moving
        )
    return any(
        file.index == change.file_id
        and path_key(file.name, windows) == path_key(change.after, windows)
        for file in torrent.files
    )


def matches_target(actual: TorrentInfo, target: TorrentInfo) -> bool:
    """
    比较实际布局与完整目标；普通下载进度/标签不影响完成判断。

    :param actual: 实际状态
    :param target: 计划目标
    :return: 是否已完成且不在移动/校验中
    """
    windows = is_windows_path(target.save_path)
    return (
        not actual.moving
        and actual.hash == target.hash
        and actual.name == target.name
        and actual.auto_tmm == target.auto_tmm
        and path_key(actual.save_path, windows) == path_key(target.save_path, windows)
        and {
            (file.index, path_key(file.name, windows), file.size)
            for file in actual.files
        }
        == {
            (file.index, path_key(file.name, windows), file.size)
            for file in target.files
        }
    )


def reconcile_execution(
    record: dict[str, Any], actual: TorrentInfo | None
) -> ExecutionResponse:
    """
    只读回查执行日志，不继续任何未完成操作。

    :param record: 已持久化的执行记录
    :param actual: 下载器实时状态或 None
    :return: 明确的完成/部分失败/验证中状态
    """
    logs = [OperationLog.model_validate(item) for item in record.get("operations", ())]
    if actual:
        logs = [
            log.model_copy(update={"status": "confirmed", "message": "修改已生效"})
            if log.status in {"unknown", "failed"}
            and change_confirmed(log.change, actual)
            else log
            for log in logs
        ]
    target = snapshot_from_dict(record["after"])
    if record.get("running") and any(log.status == "pending" for log in logs):
        status, message = "verifying", "执行请求仍在处理，请稍后回查，不要再次提交"
    elif record.get("database_error"):
        status, message = (
            "partial",
            "下载器已部分修改，但下载记录更新失败；请恢复后再处理："
            + record["database_error"],
        )
    elif (
        actual
        and all(log.status == "confirmed" for log in logs)
        and matches_target(actual, target)
    ):
        status, message = (
            "success",
            "恢复完成" if record["mode"] == "recover" else "处理完成",
        )
    elif actual and (actual.moving or any(log.status == "unknown" for log in logs)):
        status, message = (
            "verifying",
            "操作已提交，实际状态尚未确认；请刷新结果，未执行项不会自动继续",
        )
    elif any(log.status == "confirmed" for log in logs):
        status, message = (
            "partial",
            "部分修改已生效，后续操作已停止；请先恢复再重新处理",
        )
    else:
        status, message = "failed", "未确认修改成功，原始备份仍保留；请检查下载器后恢复"
    return ExecutionResponse(
        status=status,
        message=message,
        operation_id=record["operation_id"],
        mode=record["mode"],
        downloader=record["downloader"],
        torrent_hash=record["torrent_hash"],
        operations=logs,
        actual=actual,
        can_recover=record["mode"] == "format" or status != "success",
    )


def execute_plan(
    *,
    adapter: Downloader,
    plan: FrozenPlan,
    operation_id: str,
    persist: Callable[[dict[str, Any]], None],
    sync_database: Callable[[dict[str, Any], TorrentInfo], None],
    read_attempts: int = 3,
    read_delay: float = 0.15,
) -> tuple[ExecutionResponse, dict[str, Any]]:
    """
    先备份后逐项调用，回读不确定时停止依赖操作而不是重复调用。

    :param adapter: 本次局部连接
    :param plan: 通过状态校验的冻结计划
    :param operation_id: 唯一操作标识
    :param persist: 原始快照与日志持久化边界
    :param sync_database: 仅同步目标下载器记录的边界
    :param read_attempts: 每项最大回读次数
    :param read_delay: 短期回读间隔
    :return: 回执与日志
    """
    preview = plan.preview
    operations = [change for change in preview.changes if change.status == "changed"]
    # 关闭自动管理最先执行；恢复自动管理必须最后，移动尽量靠后。
    order = {"file": 1, "name": 2, "save_path": 3}
    operations.sort(
        key=lambda change: (
            0
            if change.kind == "auto_tmm" and change.after == "false"
            else 4
            if change.kind == "auto_tmm"
            else order[change.kind]
        )
    )
    record: dict[str, Any] = {
        "schema_version": 2,
        "operation_id": operation_id,
        "mode": preview.mode,
        "downloader": preview.downloader,
        "torrent_hash": preview.torrent_hash,
        "history_id": plan.history_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "before": snapshot_dict(preview.before),
        "after": snapshot_dict(preview.after),
        "media": preview.media.model_dump() if preview.media else None,
        "capabilities": preview.capabilities.model_dump(),
        "rules": preview.rules.model_dump(),
        "changes": [change.model_dump() for change in preview.changes],
        "operations": [
            OperationLog(change=change).model_dump() for change in operations
        ],
        "db_before": deepcopy(plan.db_before),
        "db_current": deepcopy(plan.db_before),
        "db_restore": deepcopy(plan.db_restore),
        "status": "verifying",
        "message": "原始状态已备份，准备执行",
        "database_error": "",
        "running": True,
    }
    logger.info(
        f"{preview.downloader}:{preview.torrent_hash} 开始"
        f"{'恢复' if preview.mode == 'recover' else '格式化'}："
        f"计划变更 {len(operations)} 项，备份数据库记录 {sum(len(values) for values in plan.db_before.values())} 项"
    )
    # 持久化失败必须直接向上抛出，绝不调用下载器。
    persist(record)
    logger.info(
        f"{preview.downloader}:{preview.torrent_hash} 原始状态及数据库记录备份完成，开始逐项执行"
    )
    actual: TorrentInfo | None = preview.before
    for index, change in enumerate(operations):
        call_error = ""
        logger.info(
            f"{preview.downloader}:{preview.torrent_hash} 执行 {index + 1}/{len(operations)} "
            f"{change.kind}：{change.before} ==> {change.after}"
        )
        try:
            if change.kind == "auto_tmm":
                logger.info(
                    f"{preview.downloader}:{preview.torrent_hash} "
                    f"{'关闭' if change.after != 'true' else '开启'} Torrent 自动管理"
                )
                adapter.set_auto_tmm(preview.torrent_hash, change.after == "true")
            elif change.kind == "file":
                adapter.rename_file(preview.torrent_hash, change.before, change.after)
            elif change.kind == "name":
                adapter.torrents_rename(
                    preview.torrent_hash, change.before, change.after
                )
            else:
                adapter.set_torrent_save_path(preview.torrent_hash, change.after)
        except Exception as error:
            call_error = str(error)
            logger.error(
                f"{preview.downloader}:{preview.torrent_hash} {change.kind} 调用失败：{call_error}"
            )
        confirmed, read_error = False, ""
        for attempt in range(read_attempts):
            try:
                actual = adapter.get_torrent(preview.torrent_hash)
                confirmed = actual is not None and change_confirmed(change, actual)
                if confirmed:
                    break
            except Exception as error:
                read_error = str(error)
                actual = None
            if attempt + 1 < read_attempts:
                time.sleep(read_delay)
        operation_message = (
            "修改已生效"
            if confirmed
            else (call_error or read_error or "下载器尚未回报目标状态")
        )
        record["operations"][index].update(
            status="confirmed" if confirmed else "unknown",
            message=operation_message,
        )
        if confirmed:
            logger.info(
                f"{preview.downloader}:{preview.torrent_hash} {change.kind} 修改已生效"
            )
        else:
            logger.warn(
                f"{preview.downloader}:{preview.torrent_hash} {change.kind} 未确认：{operation_message}"
            )
        if actual:
            record["actual"] = snapshot_dict(actual)
            try:
                sync_database(record, actual)
            except Exception as error:
                record["database_error"] = str(error)
                logger.error(
                    f"{preview.downloader}:{preview.torrent_hash} 下载记录同步失败：{error}"
                )
        result = reconcile_execution(record, actual)
        record.update(status=result.status, message=result.message)
        persist(record)
        if not confirmed or call_error or record["database_error"]:
            break
    if not operations:
        actual = adapter.get_torrent(preview.torrent_hash)
        if actual:
            try:
                sync_database(record, actual)
            except Exception as error:
                record["database_error"] = str(error)
    record["running"] = False
    result = reconcile_execution(record, actual)
    logger.info(
        f"{preview.downloader}:{preview.torrent_hash} 执行结束：{result.status}，{result.message}"
    )
    record.update(
        status=result.status,
        message=result.message,
        operations=[log.model_dump() for log in result.operations],
    )
    if actual:
        record["actual"] = snapshot_dict(actual)
    persist(record)
    return result, record


def _common_relative(torrent: TorrentInfo) -> str:
    """
    计算种子文件的公共相对根，用于目录型下载历史路径。

    :param torrent: 布局快照
    :return: 公共根或空字符串
    """
    if not torrent.files:
        return ""
    parts = [PurePosixPath(file.name).parts for file in torrent.files]
    common = []
    for values in zip(*parts):
        if len(set(values)) != 1:
            break
        common.append(values[0])
    return "/".join(common)


def _mapped_root(mapped: str, before: str, actual: str) -> str:
    """
    根据追加/还原层级投影挂载根，不猜测跨盘符绝对映射。

    :param mapped: 下载记录中的宿主保存根
    :param before: 下载器原根
    :param actual: 下载器新根
    :return: 宿主目标根
    """
    remote = PureWindowsPath if is_windows_path(before) else PurePosixPath
    local = PureWindowsPath if is_windows_path(mapped) else PurePosixPath
    old, new, host = remote(before), remote(actual), local(mapped)
    try:
        suffix = new.relative_to(old)
        return host.joinpath(*suffix.parts).as_posix()
    except ValueError:
        try:
            removed = old.relative_to(new).parts
        except ValueError as error:
            raise ValueError("下载器目录跨根变化，无法安全推断宿主路径映射") from error
        for _ in removed:
            if host.parent == host:
                raise ValueError("宿主挂载根不足以安全还原目录")
            host = host.parent
        return host.as_posix()


def _nonlocal_path(value: object) -> bool:
    """
    非本地存储 URI 不是下载器物理文件，不改写协议或对象地址。

    :param value: 记录路径
    :return: 是否不属于本地路径
    """
    match = (
        re.match(r"^([A-Za-z][A-Za-z0-9+.-]*):", value or "")
        if isinstance(value, str)
        else None
    )
    return bool(match and len(match[1]) > 1 and match[1] != "local")


def project_database_paths(
    db_before: dict[str, Any], before: TorrentInfo, actual: TorrentInfo
) -> dict[str, Any]:
    """
    由精确文件对应及已知挂载根投影路径，未知存储/无关字段保留。

    :param db_before: 仅属于目标下载器的原字段
    :param before: 原种子布局
    :param actual: 已回读布局
    :return: 不修改输入的字段快照
    """
    result = deepcopy(db_before)
    # 显示名、自动管理等非布局变化不能顺带规范化数据库中的路径字段。
    # 比较文件身份及路径，忽略顺序、优先级和选择状态等非路径属性。
    if normalized_path(before.save_path) == normalized_path(actual.save_path) and {
        file.index: (normalized_path(file.name), file.size) for file in before.files
    } == {
        file.index: (normalized_path(file.name), file.size) for file in actual.files
    }:
        return result
    windows = is_windows_path(before.save_path)
    current_files = {file.index: file for file in actual.files}
    names = {
        path_key(file.name, windows): current_files[file.index].name
        for file in before.files
        if file.index in current_files and file.size == current_files[file.index].size
    }
    roots: dict[str, str] = {
        normalized_path(before.save_path): normalized_path(actual.save_path)
    }
    for key, payload in result.get("files", {}).items():
        source = db_before["files"][key]
        relative, full, saved = (
            source.get("filepath"),
            source.get("fullpath"),
            source.get("savepath"),
        )
        if _nonlocal_path(full) or _nonlocal_path(saved):
            continue
        old_name = (
            relative
            if relative and path_key(relative, windows) in names
            else next(
                (
                    file.name
                    for file in before.files
                    if isinstance(full, str)
                    and normalized_path(full).endswith("/" + file.name)
                ),
                None,
            )
        )
        if not old_name or path_key(old_name, windows) not in names:
            continue
        new_name = names[path_key(old_name, windows)]
        mapped = None
        if isinstance(full, str) and path_key(full, windows).endswith(
            "/" + path_key(old_name, windows)
        ):
            mapped = normalized_path(full)[: -(len(old_name) + 1)] or "/"
        saved_suffix = None
        if saved and mapped:
            path_type = PureWindowsPath if windows else PurePosixPath
            try:
                saved_suffix = path_type(normalized_path(saved)).relative_to(
                    path_type(normalized_path(mapped))
                )
                path_type(old_name).relative_to(saved_suffix)
            except ValueError as error:
                raise ValueError(
                    "下载记录中的文件路径与保存目录不匹配，无法安全更新记录，已停止处理"
                ) from error
        if mapped:
            new_root = _mapped_root(mapped, before.save_path, actual.save_path)
            roots[mapped] = new_root
            payload["fullpath"] = normalized_path(new_root.rstrip("/") + "/" + new_name)
            new_relative = new_name
            if saved is not None:
                payload["savepath"] = normalized_path(
                    new_root.rstrip("/") + ("/" + saved_suffix.as_posix() if saved_suffix else "")
                )
        elif full and path_key(full, windows) == path_key(old_name, windows):
            payload["fullpath"] = new_name
            new_relative = new_name
        else:
            new_relative = new_name
        if relative:
            payload["filepath"] = new_relative

    old_common, new_common = _common_relative(before), _common_relative(actual)
    for payload in result.get("history", {}).values():
        value = payload.get("path")
        if not isinstance(value, str) or not value or _nonlocal_path(value):
            continue
        path = normalized_path(value)
        for old_root in sorted(roots, key=len, reverse=True):
            new_root = roots[old_root]
            if path_key(path, windows) == path_key(
                old_root.rstrip("/") + ("/" + old_common if old_common else ""), windows
            ):
                payload["path"] = normalized_path(
                    new_root.rstrip("/") + ("/" + new_common if new_common else "/")
                )
                break
            if path_key(path, windows) == path_key(old_root, windows) or path_key(
                path, windows
            ).startswith(path_key(old_root, windows).rstrip("/") + "/"):
                suffix = path[len(old_root.rstrip("/")) :].lstrip("/")
                suffix = names.get(path_key(suffix, windows), suffix)
                payload["path"] = normalized_path(
                    new_root.rstrip("/") + ("/" + suffix if suffix else "/")
                )
                break
    return result
