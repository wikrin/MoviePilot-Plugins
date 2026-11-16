"""
下载器局部适配器：列表不加载文件，详情仅查询精确 Hash。
"""

from abc import ABC, abstractmethod
from pathlib import PurePosixPath
from typing import Any

from app.sdk.services import DownloaderHelper

from .models import DownloaderCapabilities, TorrentFile, TorrentInfo


class DownloaderUnavailable(RuntimeError):
    """
    下载器未启用、未连接或不在本插件支持范围内。
    """


class Downloader(ABC):
    """
    单次请求持有的下载器适配器，不保存为插件共享可变属性。
    """

    capabilities: DownloaderCapabilities

    @abstractmethod
    def set_auto_tmm(self, torrent_hash: str, enable: bool) -> None:
        """
        设置精确种子的自动管理状态。

        :param torrent_hash: 非空种子 Hash
        :param enable: 目标状态
        """

    @abstractmethod
    def set_torrent_save_path(
        self, torrent_hash: str, location: str, move: bool = True
    ) -> None:
        """
        移动精确种子的保存位置。

        :param torrent_hash: 非空种子 Hash
        :param location: 下载器中的目标保存路径
        :param move: Transmission 是否移动数据
        """

    @abstractmethod
    def torrents_rename(
        self, torrent_hash: str, old_path: str, new_torrent_name: str
    ) -> None:
        """
        修改独立显示名称，不支持时抛出异常。

        :param torrent_hash: 非空种子 Hash
        :param old_path: 原显示名
        :param new_torrent_name: 目标显示名
        """

    @abstractmethod
    def rename_file(self, torrent_hash: str, old_path: str, new_path: str) -> None:
        """
        修改文件相对路径。

        :param torrent_hash: 非空种子 Hash
        :param old_path: 原相对路径
        :param new_path: 目标相对路径
        """

    @abstractmethod
    def torrents_info(
        self, torrent_hash: str | None = None, include_files: bool = True
    ) -> list[TorrentInfo]:
        """
        查询摘要或详情；只有详情读取文件列表。

        :param torrent_hash: 精确 Hash，None 表示列表
        :param include_files: 是否读取文件明细
        :return: 快照列表，缺失种子返回空列表，连接错误向上抛出
        """

    def list_torrents(self) -> list[TorrentInfo]:
        """
        一次列表调用，不查询每个种子的文件。

        :return: 摘要快照
        """
        return self.torrents_info(include_files=False)

    def get_torrent(self, torrent_hash: str) -> TorrentInfo | None:
        """
        读取并验证单个种子，绝不将空 Hash 传入批量接口。

        :param torrent_hash: 非空精确 Hash
        :return: 快照或 None
        """
        if not torrent_hash:
            raise ValueError("种子 Hash 不能为空")
        items = self.torrents_info(torrent_hash, include_files=True)
        return next(
            (item for item in items if item.hash.lower() == torrent_hash.lower()), None
        )


class QbittorrentDownloader(Downloader):
    """
    复用宿主 qBittorrent 连接，提供独立显示名和相对文件路径能力。
    """

    capabilities = DownloaderCapabilities(
        kind="qbittorrent",
        rename_torrent=True,
        file_move=True,
        auto_tmm=True,
    )

    def __init__(self, qbc: Any):
        """
        保存本次请求使用的现有连接。

        :param qbc: 宿主模块实例
        """
        self.qbc = qbc.qbc

    def set_auto_tmm(self, torrent_hash: str, enable: bool) -> None:
        """
        修改自动管理，Hash 由执行器保证非空。

        :param torrent_hash: 精确 Hash
        :param enable: 目标状态
        """
        self.qbc.torrents_set_auto_management(
            torrent_hashes=torrent_hash, enable=enable
        )

    def set_torrent_save_path(
        self, torrent_hash: str, location: str, move: bool = True
    ) -> None:
        """
        提交保存位置修改，执行器随后回读确认。

        :param torrent_hash: 精确 Hash
        :param location: 目标保存路径
        :param move: 兼容统一签名，qBittorrent 始终由自身移动
        """
        self.qbc.torrents_set_location(torrent_hashes=torrent_hash, location=location)

    def torrents_rename(
        self, torrent_hash: str, old_path: str, new_torrent_name: str
    ) -> None:
        """
        修改下载器独立显示名。

        :param torrent_hash: 精确 Hash
        :param old_path: 原显示名
        :param new_torrent_name: 新显示名
        """
        self.qbc.torrents_rename(
            torrent_hash=torrent_hash, new_torrent_name=new_torrent_name
        )

    def rename_file(self, torrent_hash: str, old_path: str, new_path: str) -> None:
        """
        修改一个文件路径，支持跨相对目录。

        :param torrent_hash: 精确 Hash
        :param old_path: 原文件路径
        :param new_path: 新文件路径
        """
        self.qbc.torrents_rename_file(
            torrent_hash=torrent_hash, old_path=old_path, new_path=new_path
        )

    def torrents_info(
        self, torrent_hash: str | None = None, include_files: bool = True
    ) -> list[TorrentInfo]:
        """
        明确区分种子列表与文件 RPC，避免摘要 N+1。

        :param torrent_hash: 精确 Hash 或列表
        :param include_files: 是否获取文件
        :return: 种子快照
        """
        raw_items = (
            self.qbc.torrents_info(torrent_hashes=torrent_hash)
            if torrent_hash
            else self.qbc.torrents_info()
        )
        result = []
        for item in raw_items or ():
            item_hash = str(item.get("hash") or "").lower()
            files = (
                self.qbc.torrents_files(torrent_hash=item_hash) if include_files else ()
            )
            state = str(item.get("state") or "")
            result.append(
                TorrentInfo(
                    name=str(item.get("name") or ""),
                    save_path=str(item.get("save_path") or "").replace("\\", "/"),
                    total_size=int(item.get("total_size") or 0),
                    hash=item_hash,
                    auto_tmm=bool(item.get("auto_tmm", False)),
                    category=str(item.get("category") or ""),
                    tags=tuple(
                        tag.strip()
                        for tag in str(item.get("tags") or "").split(",")
                        if tag.strip()
                    ),
                    files=tuple(
                        TorrentFile(
                            name=str(file.get("name") or "").replace("\\", "/"),
                            size=int(file.get("size") or 0),
                            priority=int(file.get("priority", 1)),
                            index=int(file.get("index", file.get("id", index))),
                            selected=int(file.get("priority", 1)) != 0,
                        )
                        for index, file in enumerate(files or ())
                    ),
                    state=state,
                    moving=state.lower()
                    in {"moving", "checkingup", "checkingdl", "checkingresumedata"},
                    metadata_complete=state.lower() not in {"metadl", "forcedmetadl"},
                )
            )
        return result


class TransmissionDownloader(Downloader):
    """
    Transmission 只支持位置和同目录改名，不模拟独立显示名能力。
    """

    capabilities = DownloaderCapabilities(kind="transmission")
    _summary_fields = (
        "id",
        "hashString",
        "name",
        "downloadDir",
        "totalSize",
        "labels",
        "status",
        "metadataPercentComplete",
    )
    _file_fields = ("files", "priorities", "wanted")

    def __init__(self, trc: Any):
        """
        使用宿主已有 RPC 客户端。

        :param trc: 宿主 Transmission 模块实例
        """
        self.trc = trc.trc

    def set_auto_tmm(self, torrent_hash: str, enable: bool) -> None:
        """
        明确拒绝不支持的能力。

        :param torrent_hash: 精确 Hash
        :param enable: 请求状态
        """
        raise NotImplementedError("Transmission 不支持自动管理")

    def set_torrent_save_path(
        self, torrent_hash: str, location: str, move: bool = True
    ) -> None:
        """
        提交移动，返回接受不代表物理移动已完成。

        :param torrent_hash: 精确 Hash
        :param location: 目标位置
        :param move: 是否移动数据
        """
        self.trc.move_torrent_data(ids=torrent_hash, location=location, move=move)

    def torrents_rename(
        self, torrent_hash: str, old_path: str, new_torrent_name: str
    ) -> None:
        """
        拒绝独立显示名称修改。

        :param torrent_hash: 精确 Hash
        :param old_path: 原显示名
        :param new_torrent_name: 请求的新显示名
        """
        raise NotImplementedError("Transmission 不支持独立种子显示名")

    def rename_file(self, torrent_hash: str, old_path: str, new_path: str) -> None:
        """
        RPC name 必须是 basename；禁止将跨目录路径当作新名称。

        :param torrent_hash: 精确 Hash
        :param old_path: 原相对路径
        :param new_path: 新相对路径
        """
        old, new = PurePosixPath(old_path), PurePosixPath(new_path)
        if old.parent != new.parent:
            raise ValueError("Transmission 不支持文件跨目录重排")
        self.trc.rename_torrent_path(
            torrent_id=torrent_hash, location=old_path, name=new.name
        )

    def torrents_info(
        self, torrent_hash: str | None = None, include_files: bool = True
    ) -> list[TorrentInfo]:
        """
        仅为详情请求文件字段，保留 padding 的原索引。

        :param torrent_hash: 精确 Hash 或列表
        :param include_files: 是否读取文件明细
        :return: 快照；只有缺失种子返回空列表
        """
        arguments = self._summary_fields + (self._file_fields if include_files else ())
        if torrent_hash:
            try:
                item = self.trc.get_torrent(
                    torrent_id=torrent_hash, arguments=arguments
                )
            except KeyError:
                return []
            items = [item]
        else:
            items = self.trc.get_torrents(arguments=arguments)
        result = []
        for item in items or ():
            fields = item.fields
            raw_files = fields.get("files") or ()
            priorities = fields.get("priorities") or ()
            wanted = fields.get("wanted") or ()
            state = str(item.status)
            result.append(
                TorrentInfo(
                    name=str(item.name),
                    save_path=str(item.download_dir).replace("\\", "/"),
                    total_size=int(item.total_size),
                    hash=str(item.hash_string).lower(),
                    tags=tuple(fields.get("labels") or ()),
                    files=tuple(
                        TorrentFile(
                            name=str(file["name"]).replace("\\", "/"),
                            size=int(file["length"]),
                            index=index,
                            priority=int(priorities[index])
                            if index < len(priorities)
                            else 0,
                            selected=(
                                bool(wanted[index]) if index < len(wanted) else True
                            )
                            and "_____padding_file_" not in file["name"],
                        )
                        for index, file in enumerate(raw_files)
                    ),
                    state=state,
                    moving=state in {"checking", "check pending"},
                    metadata_complete=float(fields.get("metadataPercentComplete", 1))
                    >= 1,
                )
            )
        return result


def resolve_downloader(name: str) -> Downloader:
    """
    按名称解析一个局部适配器，不改写插件共享状态。

    :param name: 已启用下载器名称
    :return: 适配器
    :raises DownloaderUnavailable: 下载器不可用或类型不支持
    """
    if not name:
        raise DownloaderUnavailable("请选择下载器")
    helper = DownloaderHelper()
    service = helper.get_service(name=name)
    if service:
        if (
            helper.is_downloader("qbittorrent", service=service)
            and service.instance.qbc
        ):
            return QbittorrentDownloader(service.instance)
        if (
            helper.is_downloader("transmission", service=service)
            and service.instance.trc
        ):
            return TransmissionDownloader(service.instance)
    raise DownloaderUnavailable(f"下载器 {name} 不存在、未启用、未连接或类型不支持")
