"""
联邦配置与管理请求契约。
"""

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CureTMDbAnimeConfig(BaseModel):
    """
    CureTMDbAnime 插件配置。
    :param enabled: 是否启用插件
    :param service_mode: 服务模式，local 表示本地内嵌服务，external 表示外部服务
    :param external_url: 外部服务地址，仅在 service_mode=external 时生效
    :param management_token: 外部服务管理令牌
    :param bangumi_api_url: 番剧 API 地址
    :param tvdb_api_url: TheTVDB API 地址
    :param bangumi_use_proxy: 是否通过代理访问番剧 API
    :param enable_correction: 是否启用元数据修正功能
    :param assume_season_by_window: 是否启用按播出窗口推测季号功能
    :param production_cycle_gap_days: 生产周期间隔天数，用于推测季号，默认 70 天
    :param grace_episodes: 宽限集数，用于推测季号，默认 2 集
    :param source: 元数据修正源 JSON 地址，默认指向 GitHub 仓库
    :param port: 内嵌服务监听端口，仅在 service_mode=local 时生效
    :param history_max_records: 修正历史最多保留的资源数量；超过后自动清理较旧记录，默认 10000 条
    """

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    service_mode: Literal["local", "external"] = "local"
    external_url: str = "http://127.0.0.1:8632"
    management_token: str = ""
    bangumi_api_url: str = "https://api.bgm.tv"
    tvdb_api_url: str = "https://api4.thetvdb.com/v4"
    bangumi_use_proxy: bool = True
    enable_correction: bool = True
    assume_season_by_window: bool = False
    production_cycle_gap_days: int = Field(default=70, ge=20, le=365)
    grace_episodes: int = Field(default=2, ge=0, le=5)
    source: str | None = "https://raw.githubusercontent.com/wikrin/CureTMDb/main/tv.json"
    port: int = Field(default=8632, ge=1024, le=65535)
    history_max_records: int = Field(default=10000, ge=100, le=100000)

    @field_validator("external_url")
    @classmethod
    def validate_address(cls, value: str) -> str:
        """
        规范化外部服务地址并拒绝凭据、查询参数及无效端口。

        :param value: 用户填写的 HTTP(S) 基础地址
        :return: 移除首尾空白及末尾斜线的地址
        :raises ValueError: 地址不符合基础地址约束
        """
        value = value.strip().rstrip("/")
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ValueError("服务地址必须为不含账号、查询参数或片段的 HTTP(S) 基础地址")
        try:
            _ = parts.port
        except ValueError as error:
            raise ValueError("服务端口无效") from error
        return value


class ControlRequest(BaseModel):
    """
    本地托管二进制的控制请求。
    """

    action: Literal["start", "stop", "restart", "download", "update"] = Field(
        description="执行的本地生命周期操作，外部模式不支持"
    )


class CleanupRequest(BaseModel):
    """
    聚合资源历史清理范围。

    filtered 指定结果时仅移除该分类，其他分类和调用计数保留。
    """

    scope: Literal["expired", "filtered", "all"] = Field(
        default="expired", description="清理过期对象、当前筛选的所有页或全部资源"
    )
    query: str = Field(
        default="",
        max_length=200,
        description="媒体名、原资源标题或副标题的完整字面子串，大小写不敏感",
    )
    result: Literal["", "changed", "unchanged", "skipped", "failed"] = Field(
        default="",
        description="filtered 时仅删除曾出现的选中分类；空值删除匹配资源全部分类",
    )
    media_key: str = Field(default="", max_length=200, description="仅清理指定媒体；空值表示不限制媒体")
    confirmed: bool = Field(default=False, description="调用方是否明确确认不可恢复的清理范围")
