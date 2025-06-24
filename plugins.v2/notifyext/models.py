from dataclasses import dataclass
from pydantic import BaseModel, Field
from typing import Any, Optional, List, Dict

from app.schemas.message import Notification


@dataclass
class FrameResult:
    """
    封装 frame 提取结果，用于后续处理和逻辑判断
    """
    # 是否需要获取媒体信息
    need_media_info: bool = False
    # 元数据
    meta: Optional[Any] = None
    # 处理后的上下文
    context: Optional[dict] = None


class FrameHandlerItem(BaseModel):
    # 标题
    label: str = Field(alias='title')
    # 值
    name: str = Field(alias='value')
    # 场景开关
    category: str = Field(alias='switch')
    # 描述
    description: str = Field(alias='subtitle')

    class Config:
        allow_population_by_field_name = True


class TemplateConf(BaseModel):
    # 模板名称
    name: str
    # 模板ID
    id: str
    # 模板
    template: Optional[str] = None


class NotificationRule(BaseModel):
    # 配置名
    name: str
    # 配置ID
    id: str
    # 目标渠道
    target: str
    # 配置开关
    enabled: bool = False
    # 场景开关
    switch: str = ""
    # 规则类型
    type: Optional[str] = None
    # YAML 配置
    yaml_content: Optional[str] = None
    # 模板ID
    template_id: Optional[str] = None


class MessageGroup(BaseModel):
    """消息组"""
    rule: NotificationRule
    send_in: float = 2
    message: Notification
    first_time: str
    last_time: str
    messages: List[Dict] = []

    class Config:
        arbitrary_types_allowed = True

    def dict(self, *args, **kwargs):
        d = super().dict(*args, **kwargs)
        d["message"] = self.message.to_dict() if self.message else None
        return d