import re
from typing import ClassVar, Dict, List, Optional

from app.core.meta.metabase import MetaBase
from app.core.metainfo import MetaInfo
from app.chain import ChainBase
from app.helper.message import TemplateHelper
from app.log import logger
from app.schemas.types import MediaType
from app.schemas.message import Notification

from ..aggregator import MessageAggregator
from ..frameinspector import FrameInspector
from ..handlers import BaseRuleHandler, registry
from ..models import NotificationRule, FrameResult
from ..utils import YamlParser


class BasicHandler(BaseRuleHandler):
    """基础类型处理器"""
    type: ClassVar[str] = [
        "subscribeAdded",
        "subscribeComplete",
        "organizeSuccess",
        "downloadAdded",
    ]

    def can_handle(self, message: Notification, rule: NotificationRule) -> bool:
        return message.ctype and rule.type == message.ctype.value

    def handle(self, message: Notification, rule: NotificationRule) -> Optional[Dict]:
        return TemplateHelper().get_cache_context(message.to_dict()) or {}


class RegexHandler(BaseRuleHandler):
    """正则类型处理器"""
    type = "regex"

    def can_handle(self, message: Notification, rule: NotificationRule) -> bool:
        return not message.ctype and rule.type == "regex"

    def handle(self, message: Notification, rule: NotificationRule) -> Optional[Dict]:
        if not rule.yaml_content:
            return {}

        yaml_data = YamlParser.parse(rule.yaml_content)
        extractors = yaml_data.get("extractors", [])
        meta_fields = YamlParser.extract_meta_fields(yaml_data)
        aggregate: dict = yaml_data.get("Aggregate")

        if not extractors:
            logger.warn("rule extractors is empty")
            return {}

        context = self.extract_fields(message, extractors)

        # 过滤掉meta属性字段
        if meta_fields:
            try:
                meta = self._create_meta_instance(fields=meta_fields, context=context)
                mediainfo = ChainBase().recognize_media(meta=meta)
                if mediainfo:
                    # 删除meta属性字段
                    context = {k: v for k, v in context.items() if k not in meta_fields}
                context = TemplateHelper().builder.build(meta=meta, mediainfo=mediainfo, include_raw_objects=False, **context)
            except Exception as e:
                logger.warn(f"Failed to create meta instance: {e}")

        if aggregate and MessageAggregator().try_aggregate_message(
            rule, message, context, aggregate
        ):
            return None

        return context

    @staticmethod
    def extract_fields(message: Notification, extractors: List[dict]) -> dict:
        if not isinstance(extractors, list):
            return {}

        context = {}
        for extractor in extractors:
            field = extractor.get("field")
            if not field:
                continue

            raw_value = getattr(message, field, None)
            if not raw_value:
                continue

            text = str(raw_value)
            logger.debug(f"文本内容: {text}")
            for key, pattern in extractor.items():
                if key == "field":
                    continue
                try:
                    match = re.search(pattern, text)
                    logger.debug(f"pattern: `{pattern}` → match: {match}")
                    if match:
                        if match.groupdict():
                            context.update(match.groupdict())
                        elif match.lastindex == 1:
                            context[key] = match.group(1)
                        else:
                            context[key] = match.group()
                except re.error as e:
                    logger.warn(f"正则表达式无效: {pattern}, 错误: {e}")

        return context

    @staticmethod
    def _create_meta_instance(fields: Dict[str, str], context: Dict) -> Optional[MetaBase]:
        if not isinstance(fields, dict) or not isinstance(context, dict):
            return None

        title_key = fields.get('title')
        if not title_key or not context.get(title_key):
            return None

        meta = MetaInfo(
            title=context[title_key],
            subtitle=context.get(fields.get('subtitle'))
        )

        def map_type(value_key):
            value = context.get(value_key)
            return MediaType.MOVIE if value in ("movie", "电影") else MediaType.TV

        field_mapping = {"type": map_type}

        for key, value_key in fields.items():
            if key in ['title', 'subtitle']:
                continue

            if key in field_mapping:
                setattr(meta, key, field_mapping[key](value_key))
            elif (value := context.get(value_key)) is not None:
                setattr(meta, key, value)

        return meta


class FrameHandler(BaseRuleHandler):
    type = list(registry._handlers.keys())
    default = True # 默认处理器

    @staticmethod
    def _need_media_recognition(frameresult: FrameResult) -> bool:
        """判断是否需要媒体识别"""
        return (
            frameresult.need_media_info
            and frameresult.meta
            and frameresult.meta.title
        )

    def can_handle(self, message: Notification, rule: NotificationRule) -> bool:
        return rule.enabled and registry.get_handler(rule.type)

    def handle(self, message: Notification, rule: NotificationRule) -> Optional[Dict]:
        """处理帧数据并返回上下文"""
        func = registry.get_handler(rule.type)
        if not func:
            return {}

        frameresult: FrameResult = func()
        if not self._need_media_recognition(frameresult):
            return frameresult.context

        mediainfo = ChainBase().recognize_media(meta=frameresult.meta)
        if not mediainfo:
            return frameresult.context
        # 减小体积
        mediainfo.clear()
        return TemplateHelper().builder.build(
            meta=frameresult.meta,
            mediainfo=mediainfo,
            **frameresult.context,
        )


class FrameYamlHandler(BaseRuleHandler):
    """
    处理帧数据
    """
    type = "frame"

    def can_handle(self, message: Notification, rule: NotificationRule) -> bool:
        return not message.ctype and rule.type == "frame" and rule.yaml_content

    def handle(self, message: Notification, rule: NotificationRule):
        # 加载yaml
        yaml = YamlParser.parse(rule.yaml_content)
        if not(frame := yaml.get("Frame", {})):
            logger.error("规则配置错误：Frame 节点不存在")
            return {}
        context = FrameInspector.inspect(frame)
        required = frame.get("required", [])
        if any(field not in context for field in required):
            return {}
        # 获取聚合配置
        aggregate: dict = yaml.get("Aggregate")
        if aggregate and MessageAggregator().try_aggregate_message(
            rule, message, context, aggregate
        ):
            return True

        logger.info(f"命中规则: {rule.name}")
        return context
