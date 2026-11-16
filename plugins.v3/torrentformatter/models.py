"""
种子格式化的状态快照、请求级规则与前后端契约。
"""

from dataclasses import asdict, dataclass, field
from typing import Annotated, Any, Literal, Self
from urllib.parse import quote

from apscheduler.triggers.cron import CronTrigger
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    field_validator,
    model_validator,
)


@dataclass(frozen=True)
class TorrentFile:
    """
    下载器文件的稳定身份及原始布局。

    :param name: 包含目录的种子相对文件路径
    :param size: 文件字节数
    :param priority: 下载器原生优先级
    :param index: 稳定文件索引；旧备份可能没有
    :param selected: 是否选中下载；padding 文件为 False
    """

    name: str
    size: int
    priority: int = 0
    index: int | None = None
    selected: bool = True


@dataclass(frozen=True)
class TorrentInfo:
    """
    一次查询的种子快照，不包含下载器凭据。

    :param name: 下载器显示名称
    :param save_path: 下载器中的保存路径，不是宿主挂载路径
    :param total_size: 种子总字节数
    :param hash: 种子 Hash
    :param auto_tmm: qBittorrent 自动管理状态
    :param category: 种子分类
    :param tags: 标签，未设置时为空元组
    :param files: 按稳定索引排列的文件
    :param state: 下载器状态文本
    :param moving: 是否正在移动或校验数据
    :param metadata_complete: 磁力元信息是否就绪
    """

    name: str
    save_path: str
    total_size: int
    hash: str
    auto_tmm: bool = False
    category: str = ""
    tags: tuple[str, ...] = ()
    files: tuple[TorrentFile, ...] = ()
    state: str = ""
    moving: bool = False
    metadata_complete: bool = True


def identity_key(downloader: str, torrent_hash: str) -> str:
    """
    编码复合身份，下载器名称中的分隔符不会产生歧义。

    :param downloader: 下载器名称
    :param torrent_hash: 种子 Hash
    :return: 可用于插件数据索引的键
    """
    return f"{quote(downloader, safe='')}:{torrent_hash.lower()}"


def snapshot_dict(snapshot: TorrentInfo) -> dict[str, Any]:
    """
    返回与内存快照隔离、可持久化的副本。

    :param snapshot: 种子状态
    :return: 可由 JSON 编码的状态字典
    """
    return asdict(snapshot)


def snapshot_from_dict(data: dict[str, Any]) -> TorrentInfo:
    """
    读取新旧快照；旧文件索引保持 None，以便恢复时明确验证。

    :param data: 插件备份字典
    :return: 规范化后的种子快照
    """
    return TorrentInfo(
        name=str(data.get("name") or ""),
        save_path=str(data.get("save_path") or "").replace("\\", "/"),
        total_size=int(data.get("total_size") or 0),
        hash=str(data.get("hash") or "").lower(),
        auto_tmm=bool(data.get("auto_tmm", False)),
        category=str(data.get("category") or ""),
        tags=tuple(data.get("tags") or ()),
        files=tuple(
            TorrentFile(
                name=str(item.get("name") or "").replace("\\", "/"),
                size=int(item.get("size") or 0),
                priority=int(item.get("priority") or 0),
                index=item.get("index"),
                selected=bool(item.get("selected", True)),
            )
            for item in data.get("files") or ()
        ),
        state=str(data.get("state") or ""),
        moving=bool(data.get("moving", False)),
        metadata_complete=bool(data.get("metadata_complete", True)),
    )


class FormatterRules(BaseModel):
    """
    有效命名规则；实例不可变，临时覆盖不写回全局配置。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    rename_torrent: bool = Field(False, description="修改下载器独立显示名称")
    rename_file: bool = Field(False, description="实验性修改种子文件布局")
    format_torrent_name: str = Field(
        "{{ title }}{% if year %} ({{ year }}){% endif %}{% if season_episode %} - {{season_episode}}{% endif %}",
        max_length=8192,
        description="种子显示名 Jinja2 模板",
    )
    format_save_path: str = Field(
        "{{title}}{% if year %} ({{year}}){% endif %}",
        max_length=8192,
        description="保存路径追加模板；留空不移动",
    )
    format_movie_path: str = Field(
        "{{title}}{% if year %} ({{year}}){% endif %}{% if part %}-{{part}}{% endif %}{% if videoFormat %} - {{videoFormat}}{% endif %}{{fileExt}}",
        max_length=8192,
        description="电影相对文件路径模板",
    )
    format_tv_path: str = Field(
        "Season {{season}}/{{title}} - {{season_episode}}{% if part %}-{{part}}{% endif %}{% if episode %} - 第 {{episode}} 集{% endif %}{{fileExt}}",
        max_length=8192,
        description="剧集相对文件路径模板",
    )


class TorrentFormatterConfig(FormatterRules):
    """
    插件完整配置，复用命名字段并保持已保存配置的扁平键名。

    不可变模型和下载器元组避免实例间共享可变配置；未知的宿主字段兼容忽略。
    """

    model_config = ConfigDict(extra="ignore", frozen=True, validate_default=True)
    cron: str = Field("", description="定时扫描 Cron 表达式；留空默认每天 08:00")
    cron_enabled: bool = Field(False, description="启用定时扫描")
    event_enabled: bool = Field(False, description="启用下载添加事件处理")
    downloader: tuple[str, ...] = Field(
        default_factory=tuple, description="定时扫描的下载器名称列表"
    )
    exclude_tags: str = Field(
        "", description="排除标签，逗号分隔，空白条目匹配未设置标签的种子"
    )
    exclude_dirs: str = Field("", description="排除保存目录，每行一个，按目录边界匹配")

    @field_validator(
        "cron",
        "exclude_tags",
        "exclude_dirs",
        "format_save_path",
        "format_torrent_name",
        "format_movie_path",
        "format_tv_path",
        mode="before",
    )
    @classmethod
    def normalize_empty_text(cls, value: Any) -> Any:
        """
        兼容前端清空输入时提交的 null，不把空模板恢复为默认模板。

        :param value: 已保存字段输入
        :return: 空字符串或原输入，类型由 Pydantic 校验
        """
        return "" if value is None else value

    @field_validator(
        "cron_enabled", "event_enabled", "rename_torrent", "rename_file", mode="before"
    )
    @classmethod
    def normalize_disabled(cls, value: Any) -> Any:
        """
        null 兼容为关闭，字符串布尔值交给 Pydantic 正确解析。

        :param value: 开关输入
        :return: False 或原输入
        """
        return False if value is None else value

    @field_validator("downloader", mode="before")
    @classmethod
    def normalize_downloaders(cls, value: Any) -> Any:
        """
        兼容空列表，拒绝将字符串错误拆成下载器名称字符。

        :param value: 名称列表
        :return: 空元组或列表输入
        """
        if value is None:
            return ()
        if isinstance(value, str):
            raise ValueError("downloader 必须是下载器名称列表，不能是字符串")
        return value

    @model_validator(mode="after")
    def validate_schedule(self) -> Self:
        """
        开启定时任务时复用实际调度器解析，关闭时保留尚未启用的表达式。

        :return: 校验后的配置
        """
        if self.cron_enabled:
            try:
                CronTrigger.from_crontab(self.cron or "0 8 * * *")
            except ValueError as error:
                raise ValueError(f"定时任务 Cron 表达式无效：{error}") from error
        return self

    def naming_rules(self) -> FormatterRules:
        """
        返回只包含命名字段的请求级模型，不把自动任务配置传入临时覆盖。

        :return: 与完整配置隔离的命名规则
        """
        return FormatterRules.model_validate(
            self.model_dump(include=set(FormatterRules.model_fields))
        )


class RuleOverrides(BaseModel):
    """
    本次请求的规则差量；未传字段沿用已保存规则，空值清空模板。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    rename_torrent: bool | None = None
    rename_file: bool | None = None
    format_torrent_name: str | None = Field(None, max_length=8192)
    format_save_path: str | None = Field(None, max_length=8192)
    format_movie_path: str | None = Field(None, max_length=8192)
    format_tv_path: str | None = Field(None, max_length=8192)

    def apply(self, base: FormatterRules) -> FormatterRules:
        """
        合并请求中明确提供的字段，保留 False 和空字符串。

        :param base: 已保存的有效规则
        :return: 新的不可变规则对象
        """
        values = base.model_dump()
        for key in self.model_fields_set:
            value = getattr(self, key)
            values[key] = (value or "") if key.startswith("format_") else bool(value)
        return FormatterRules(**values)


class DownloaderCapabilities(BaseModel):
    """
    可执行的下载器能力，不支持的操作不能伪装为成功。
    """

    model_config = ConfigDict(frozen=True)
    kind: Literal["qbittorrent", "transmission"]
    save_path: bool = True
    rename_torrent: bool = False
    rename_file: bool = True
    file_move: bool = False
    auto_tmm: bool = False


class TargetRequest(BaseModel):
    """
    单种子复合身份请求，禁止空 Hash 导致下载器全选。
    """

    model_config = ConfigDict(extra="forbid")
    downloader: str = Field(
        min_length=1, max_length=200, description="已启用下载器名称"
    )
    torrent_hash: str = Field(
        pattern=r"^(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})$", description="精确种子 Hash"
    )

    @field_validator("torrent_hash")
    @classmethod
    def normalize_hash(cls, value: str) -> str:
        """
        统一 Hash 大小写。

        :param value: 已验证的 Hash
        :return: 小写 Hash
        """
        return value.lower()


class MediaSelection(BaseModel):
    """
    手动指定的 TMDB 身份，标题和海报仍由宿主获取。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    media_source: Literal["themoviedb"] = "themoviedb"
    media_id: str = Field(pattern=r"^[1-9][0-9]{0,11}$", description="TMDB 正整数 ID")
    media_type: Literal["movie", "tv"]


class PreviewRequest(TargetRequest):
    """
    格式化或恢复的只读预览输入。
    """

    mode: Literal["format", "recover"] = "format"
    media: MediaSelection | None = None
    season: int | None = Field(None, ge=0, le=99, description="仅补充未识别的季号")
    overrides: RuleOverrides = Field(default_factory=RuleOverrides)


class ProcessRequest(BaseModel):
    """
    执行服务端已冻结的计划，不接收客户端目标路径。
    """

    model_config = ConfigDict(extra="forbid")
    preview_token: str = Field(min_length=16, max_length=128)


class RecoverRequest(TargetRequest):
    """
    兼容旧客户端的恢复请求；新 UI 必须带恢复预览 token。
    """

    preview_token: str | None = Field(None, min_length=16, max_length=128)


class MediaBrief(BaseModel):
    """
    已解析的媒体摘要及识别来源，用于用户核对。
    """

    media_source: str = "themoviedb"
    media_id: str = ""
    media_type: Literal["movie", "tv"]
    title: str
    year: str = ""
    poster: str = ""
    source: Literal["manual", "history", "assisted", "recognized"] = "recognized"


ChangeKind = Literal["save_path", "name", "file", "auto_tmm"]


class Change(BaseModel):
    """
    一项可展示的差异；仅 changed 项可以成为执行操作。
    """

    model_config = ConfigDict(frozen=True)
    id: str
    kind: ChangeKind
    before: str
    after: str
    status: Literal["changed", "unchanged", "skipped", "unsupported", "invalid"]
    reason: str = ""
    file_id: int | None = None


class ChangeCounts(BaseModel):
    """
    区分目录移动、相对文件改名与全路径影响范围的计数。
    """

    total_files: int = 0
    files: int = 0
    save_path: int = 0
    name: int = 0
    auto_tmm: int = 0
    skipped: int = 0
    unsupported: int = 0
    affected_files: int = 0


class PreviewResponse(BaseModel):
    """
    只读计划及其服务端 token，blocked/无变更不能执行。
    """

    status: Literal["ready", "blocked", "no_changes"]
    message: str = ""
    mode: Literal["format", "recover"] = "format"
    downloader: str
    torrent_hash: str
    preview_token: str | None = None
    expires_in: int = 0
    before: TorrentInfo
    after: TorrentInfo
    media: MediaBrief | None = None
    rules: FormatterRules = Field(default_factory=FormatterRules)
    capabilities: DownloaderCapabilities
    changes: list[Change] = Field(default_factory=list)
    counts: ChangeCounts = Field(default_factory=ChangeCounts)
    warnings: list[str] = Field(default_factory=list)


class OperationLog(BaseModel):
    """
    单项调用的回读结果；unknown 不能视为已完成。
    """

    change: Change
    status: Literal["pending", "confirmed", "failed", "unknown"] = "pending"
    message: str = ""


class ExecutionResponse(BaseModel):
    """
    执行或回查结果，不承诺跨下载器和数据库原子回滚。
    """

    status: Literal["success", "partial", "failed", "verifying"]
    message: str
    operation_id: str
    mode: Literal["format", "recover"] = "format"
    downloader: str
    torrent_hash: str
    operations: list[OperationLog] = Field(default_factory=list)
    actual: TorrentInfo | None = None
    can_recover: bool = False


class TorrentSummary(BaseModel):
    """
    无文件明细的下载器列表摘要。
    """

    hash: str
    name: str
    save_path: str
    total_size: int
    state: str = ""
    processed: bool = False


class TorrentListResponse(BaseModel):
    """
    单下载器分页种子列表。
    """

    items: list[TorrentSummary]
    total: int
    page: int
    page_size: int
    capabilities: DownloaderCapabilities


class TorrentDetail(BaseModel):
    """
    实时快照及可恢复记录提示。
    """

    torrent: TorrentInfo
    capabilities: DownloaderCapabilities
    processed: bool = False
    history_id: str | None = None
    media: MediaBrief | None = None
    warnings: list[str] = Field(default_factory=list)


class RulesResponse(BaseModel):
    """
    Page 使用的已保存规则和排除条件，不暴露全局秘密配置。
    """

    defaults: FormatterRules = Field(default_factory=FormatterRules)
    effective: FormatterRules
    exclude_dirs: str = ""
    exclude_tags: str = ""
    exclude_words: list[str] = Field(default_factory=list)


class HistoryLocation(BaseModel):
    """
    原始下载器或已知转种目标，始终使用稳定 Hash。
    """

    downloader: str = Field(description="核验链中的下载器名称")
    torrent_hash: str = Field(description="该位置使用的稳定种子 Hash")


class HistoryPresence(BaseModel):
    """
    历史记录对应种子的实时位置及删除确认摘要。
    """

    state: Literal[
        "available",
        "transferred",
        "missing",
        "unavailable",
        "conflict",
        "busy",
        "backup_missing",
    ] = Field(description="备份与种子位置的核验状态")
    message: str = Field(description="可以直接展示的中文核验说明")
    target_downloader: str = Field(default="", description="最终核验到的下载器名称")
    target_hash: str = Field(default="", description="最终核验使用的稳定种子 Hash")
    locations: list[HistoryLocation] = Field(
        default_factory=list, description="从原位置到已知转种目标的顺序"
    )
    can_delete: bool = Field(
        default=True, description="当前是否允许放弃插件备份，不表示会删除种子"
    )
    delete_signature: str = Field(
        default="", description="绑定备份、转种链和实时状态的删除确认摘要"
    )
    checked_at: str = Field(
        default="", description="下载器摘要真正完成核验的 UTC 时间；未查询时为空"
    )
    cached: bool = Field(
        default=False, description="本次状态是否至少复用了一个五秒内的跨请求摘要"
    )


class DeleteHistoryRequest(BaseModel):
    """
    仅删除插件历史，必须明确确认并校验旧状态摘要。
    """

    model_config = ConfigDict(extra="forbid")
    history_id: str = Field(
        min_length=1, max_length=1024, description="当前插件实例中的稳定历史身份"
    )
    delete_signature: str = Field(
        pattern=r"^[a-f0-9]{64}$", description="历史列表返回的最新删除确认摘要"
    )
    confirmed: Literal[True] = Field(description="必须明确确认放弃该备份的恢复能力")


class DeleteHistoryBatchItem(BaseModel):
    """
    批量删除中的单条历史身份与确认摘要。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    history_id: str = Field(
        min_length=1, max_length=1024, description="当前插件实例中的稳定历史身份"
    )
    delete_signature: str = Field(
        pattern=r"^[a-f0-9]{64}$", description="用户确认时看到的删除状态摘要"
    )


class DeleteHistoryBatchRequest(BaseModel):
    """
    一次最多删除一百个不同历史身份的明确确认请求。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    items: list[DeleteHistoryBatchItem] = Field(
        min_length=1,
        max_length=100,
        description="需要逐项核验并删除的历史身份，最多一百条且不得重复",
    )
    confirmed: Literal[True] = Field(description="必须明确确认放弃全部所选备份的恢复能力")

    @model_validator(mode="after")
    def validate_distinct_history_ids(self) -> Self:
        """
        拒绝同一批次中的重复身份，避免回执和总数产生歧义。

        :return: 已确认历史身份互不重复的请求
        """
        history_ids = [item.history_id for item in self.items]
        if len(history_ids) != len(set(history_ids)):
            raise ValueError("items 不能包含重复的 history_id")
        return self


class DeleteHistoryResult(BaseModel):
    """
    批量删除中单条记录的最终业务回执。
    """

    status: Literal["deleted", "not_found", "conflict", "busy", "failed"] = Field(
        description="删除、已不存在、状态冲突、忙碌或存储失败"
    )
    history_id: str = Field(description="请求中的稳定历史身份")
    message: str = Field(description="可以直接展示的逐项中文原因")
    presence: HistoryPresence | None = Field(
        default=None, description="冲突或忙碌时本次实时核验到的状态"
    )


class DeleteHistoryBatchResponse(BaseModel):
    """
    单次事务批量清理后的汇总与逐项结果。
    """

    status: Literal["success", "partial", "failed"] = Field(
        description="全部移除、部分移除或没有记录被移除"
    )
    message: str = Field(description="本批次的中文汇总说明")
    results: list[DeleteHistoryResult] = Field(description="与请求顺序一致的逐项结果")
    removed_history_ids: list[str] = Field(
        default_factory=list, description="可以从界面移除的 deleted 或 not_found 身份"
    )
    invalidated_history_ids: list[str] = Field(
        default_factory=list,
        description="因共享旧备份归属改变而必须重新核验的保留身份",
    )
    affected_targets: list[HistoryLocation] = Field(
        default_factory=list, description="删除记录关联的原位置和已知转种目标"
    )


class DeleteHistoryResponse(BaseModel):
    """
    插件备份及关联日志的删除回执，不涉及种子或文件。
    """

    status: Literal["deleted"] = Field(default="deleted", description="兼容单条接口的成功状态")
    history_id: str = Field(description="已经删除的稳定历史身份")
    message: str = Field(description="不删除种子或文件的成功说明")


class HistoryItem(BaseModel):
    """
    统一新旧历史的分页摘要，详情再查询下载器。
    """

    id: str
    downloader: str
    torrent_hash: str
    name: str
    save_path: str
    target_save_path: str = ""
    files_count: int = 0
    status: str = "success"
    created_at: str = ""
    legacy: bool = False
    restorable: bool = True
    presence: HistoryPresence | None = None


class HistoryListResponse(BaseModel):
    """
    分页历史列表及记录总量。
    """

    items: list[HistoryItem]
    total: int
    page: int
    page_size: int


class HistoryLookupRequest(BaseModel):
    """
    显式实时核验最多一百个跨页历史身份的只读请求。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    history_ids: list[
        Annotated[
            str,
            Field(
                min_length=1,
                max_length=1024,
                description="当前插件实例中的稳定历史身份",
            ),
        ]
    ] = Field(
        min_length=1,
        max_length=100,
        description="需要按稳定身份实时核验的记录，最多一百条且不得重复",
    )

    @model_validator(mode="after")
    def validate_distinct_history_ids(self) -> Self:
        """
        拒绝重复身份，确保每个请求项只有一条确定回执。

        :return: 已确认历史身份互不重复的请求
        """
        if len(self.history_ids) != len(set(self.history_ids)):
            raise ValueError("history_ids 不能包含重复项")
        return self


class HistoryLookupResult(BaseModel):
    """
    单个历史身份的实时存在性与完整列表项。
    """

    history_id: str = Field(description="请求中的稳定历史身份")
    status: Literal["found", "not_found"] = Field(
        description="记录仍存在或已经确认插件备份不存在"
    )
    message: str = Field(description="可以直接展示的核验结果说明")
    item: HistoryItem | None = Field(
        default=None, description="仍存在时返回的实时历史列表项"
    )


class HistoryLookupResponse(BaseModel):
    """
    与请求顺序一致的跨页历史实时核验结果。
    """

    results: list[HistoryLookupResult] = Field(description="逐个历史身份的只读核验结果")


class ProcessedTorrentData(RootModel[dict[str, str]]):
    """
    保留旧 API 的 Hash 与下载器映射形态。
    """


class TorrentData(BaseModel):
    """
    保留旧 API 的备份摘要形态。
    """

    name: str
    files_count: int
    save_path: str


@dataclass(frozen=True)
class FrozenPlan:
    """
    服务端冻结的计划和校验要素，不直接接收来自客户端的实例。

    :param preview: 对外的计划副本
    :param fingerprint: 预览时目标状态摘要
    :param config_fingerprint: 已保存规则及排除项摘要
    :param history_id: 恢复记录身份；格式化时为 None
    :param history_fingerprint: 恢复备份及最新操作的摘要
    :param db_before: 本次目标下载器的数据库路径快照
    :param db_restore: 恢复成功时需要还原的原始字段
    """

    preview: PreviewResponse
    fingerprint: str
    config_fingerprint: str
    history_id: str | None = None
    history_fingerprint: str = ""
    db_before: dict[str, Any] = field(default_factory=dict)
    db_restore: dict[str, Any] = field(default_factory=dict)
