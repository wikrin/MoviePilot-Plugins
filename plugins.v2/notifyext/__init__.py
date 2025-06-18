# 基础库
import copy
import datetime
import re
import threading
from typing import Any, Optional, Tuple, Dict, List

# 第三方库
from pydantic import BaseModel
from sqlalchemy.orm import Session
from ruamel.yaml import YAML, CommentedMap, YAMLError

# 项目库
from app.core.meta.metabase import MetaBase
from app.core.metainfo import MetaInfo
from app.db import db_query
from app.db.models.message import Message
from app.helper.message import MessageQueueManager, TemplateHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.message import Notification
from app.schemas.types import MediaType
from app.utils.singleton import SingletonClass


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
    # 规则类型
    type: Optional[str] = None
    # YAML 配置
    yaml_content: Optional[str] = None
    # 模板ID
    template_id: Optional[str] = None


class MessageGroup(BaseModel):
    """消息组"""
    rule: NotificationRule
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


class MessageAggregator(metaclass=SingletonClass):
    """消息聚合器"""

    def __init__(self, plugin: 'NotifyExt', window: int):
        self.plugin = plugin
        self.window = window
        self._messages: Dict[str, MessageGroup] = {}
        self._scheduler = plugin._scheduler
        self._restore_state()

    def add_message(self, rule: NotificationRule, message: Notification, context: dict):
        now = datetime.datetime.now().isoformat()
        if not self._messages:
            self._start_check_task()

        if rule.id not in self._messages:
            self._messages[rule.id] = MessageGroup(
                rule=rule,
                message=message,
                first_time=now,
                last_time=now,
            )

        group = self._messages[rule.id]
        group.messages.append(context)
        group.last_time = now
        logger.info(f"{message} 已添加至消息组")

    def _send_group(self, rule_id: str):
        group = self._messages.get(rule_id)
        if not group.messages:
            return

        merged = {
            "count": len(group.messages),
            "messages": [msg for msg in group.messages],
            "first_time": group.first_time,
            "last_time": group.last_time,
        }

        # 渲染消息
        if message := self.plugin.rendered_message(group.rule, merged, group.message):
            logger.info(f"发送 {group.rule.name} 聚合消息")
            self.plugin.post_message(**message.dict())
            # 删除消息
            self._messages.pop(rule_id)
            # 保存状态
            self._save_state()

        if not self._messages:
            self._scheduler.remove_job("check_aggregate_messages")

    def _save_state(self):
        state = {k: v.dict() for k, v in self._messages.items()}
        self.plugin.save_data("aggregate_state", state)

    def _restore_state(self):
        state = self.plugin.get_data("aggregate_state") or {}
        if not state:
            return
        for rule_id, group_data in state.items():
            self._messages[rule_id] = MessageGroup(**group_data)
        if self._messages:
            self._start_check_task()

    def _start_check_task(self):
        from apscheduler.schedulers.background import BackgroundScheduler
        self._scheduler = BackgroundScheduler()
        self._scheduler.add_job(
            func=self._check_expired_groups,
            trigger='interval',
            minutes=10,
            id='check_aggregate_messages',
            name="aggregate_check",
        )
        self._scheduler.start()
        logger.info("启动消息检查任务")

    def stop_check_task(self):
        self._save_state()
        self._scheduler.shutdown()
        self._scheduler = None
        logger.info("停止消息检查任务")

    def _check_expired_groups(self):
        if not self._messages:
            return

        now = datetime.datetime.now()
        for rule_id in list(self._messages.keys()):
            group = self._messages[rule_id]
            if (
                now - datetime.datetime.fromisoformat(group.first_time)
            ).total_seconds() >= self.window * 3600:
                self._send_group(rule_id)


class NotifyExt(_PluginBase):
    # 插件名称
    plugin_name = "消息通知扩展"
    # 插件描述
    plugin_desc = "拦截设定时间内重复消息，根据规则聚合/分发消息"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/wikrin/MoviePilot-Plugins/main/icons/message_a.png"
    # 插件版本
    plugin_version = "2.0.1"
    # 插件作者
    plugin_author = "Attente"
    # 作者主页
    author_url = "https://github.com/wikrin"
    # 插件配置项ID前缀
    plugin_config_prefix = "notifyext_"
    # 加载顺序
    plugin_order = 5
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _scheduler = None
    _local = threading.local()

    _rules_key: str = "notifyext_rules"
    _templates_key: str = "notifyext_templates"
    _rules: list[NotificationRule] = []
    _templates: dict = {}

    # 配置属性
    _enabled: bool = False
    _cooldown: int = 0

    def init_plugin(self, config: dict = None):
        self.messagequeue = MessageQueueManager()
        self.aggregator = MessageAggregator(self, 2)

        self._rules = self.get_rules()
        self._templates = self.get_templates()

        # 停止现有任务
        self.stop_service()
        self.load_config(config)

    def load_config(self, config: dict):
        """加载配置"""
        if config:
            # 遍历配置中的键并设置相应的属性
            for key in (
                "enabled",
                "cooldown",
            ):
                setattr(self, f"_{key}", config.get(key, getattr(self, f"_{key}")))

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        """
        pass

    def stop_service(self):
        """退出插件"""
        try:
            if self._scheduler:
                self.aggregator.stop_check_task()
        except Exception as e:
            logger.error(f"退出插件失败：{str(e)}")

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/templates",
                "endpoint": self.templates,
                "methods": ["GET"],
                "auth": "bear",  # 鉴权类型：apikey/bear
                "summary": "获取消息通知模板",
                "description": "获取消息通知模板",
            },
            {
                "path": "/templates",
                "endpoint": self.save_templates,
                "methods": ["POST"],
                "auth": "bear",  # 鉴权类型：apikey/bear
                "summary": "保存消息通知模板",
                "description": "保存消息通知模板",
            },
            {
                "path": "/rules",
                "endpoint": self.get_rules,
                "methods": ["GET"],
                "auth": "bear",  # 鉴权类型：apikey/bear
                "summary": "获取消息分发规则",
                "description": "获取消息分发规则",
            },
            {
                "path": "/rules",
                "endpoint": self.save_rules,
                "methods": ["POST"],
                "auth": "bear",  # 鉴权类型：apikey/bear
                "summary": "保存消息分发规则",
                "description": "保存消息分发规则",
            },
        ]

    def templates(self) -> list[TemplateConf]:
        templates = self.get_data(key=self._templates_key) or []
        return [TemplateConf(**t) for t in templates]

    def get_templates(self) -> dict[str, str]:
        templates = self.templates()
        return {t.id: t.template for t in templates}

    def save_templates(self, templates: list[TemplateConf]):
        data = [t.dict() for t in templates]
        self.save_data(key=self._templates_key, value=data)
        # 刷新模板
        self._templates = {t.id: t.template for t in templates}

    def get_rules(self) -> list[NotificationRule]:
        rules = self.get_data(key=self._rules_key) or []
        return [NotificationRule(**rule) for rule in rules]

    def save_rules(self, rules: list[NotificationRule]):
        data = [rule.dict() for rule in rules]
        self.save_data(key=self._rules_key, value=data)
        # 刷新规则
        self._rules = rules

    def is_within_notification_cooldown(self, message: Notification):
        if result := self.get_message_history(message):
            last_time = datetime.datetime.strptime(result.reg_time, "%Y-%m-%d %H:%M:%S")
            now_time = datetime.datetime.now()
            cooldown_minutes = (now_time - last_time).total_seconds() // 60
            if cooldown_minutes < self._cooldown:
                logger.info(f"上次发送消息 {cooldown_minutes} 分钟前, 跳过此次发送。")
                return True
        return False

    def get_command(self):
        pass

    def get_form(self):
        return [], {}

    def get_page(self):
        pass

    def get_state(self):
        return self._enabled

    @staticmethod
    def get_render_mode() -> Tuple[str, Optional[str]]:
        """
        获取插件渲染模式
        :return: 1、渲染模式，支持：vue/vuetify，默认vuetify；2、vue模式下编译后文件的相对路径，默认为`dist/assets`，vuetify模式下为None
        """
        return "vue", "dist/assets"

    def get_module(self) -> Dict[str, Any]:
        """
        获取插件模块声明，用于胁持系统模块实现（方法名：方法实现）
        """
        return {"post_message": self.on_post_message}

    def on_post_message(self, message: Notification):
        if getattr(type(self)._local, "flag", False):
            return None

        if message.mtype and self.is_within_notification_cooldown(message):
            return False

        return self.handle_message(message)

    def rendered_message(self, rule: NotificationRule, context: dict, message: Notification = None) -> Optional[Notification]:
        _template = self._templates.get(rule.template_id)
        if not _template:
            logger.error(f"模板 {rule.template_id} 不存在")
            return None
        template_content = TemplateHelper().parse_template_content(_template, template_type="literal")
        # 避免引用修改源数据
        msg = copy.deepcopy(message) if message else Notification()
        logger.info(f"规则：{rule.name} 开始通过模板渲染消息")
        rendered = TemplateHelper().render_with_context(template_content, context)
        if not rendered:
            return None
        rendered = TemplateHelper()._TemplateHelper__process_formatted_string(rendered)

        if isinstance(rendered, dict):
            for key, value in rendered.items():
                if value and hasattr(msg, key):
                    setattr(msg, key, value)

            msg.source = rule.target
            return msg
        return None

    def handle_message(self, message: Notification) -> Optional[bool]:

        sent_any = None

        for rule in self._rules:
            if not rule.enabled:
                continue

            if message.ctype and rule.type == message.ctype.value:
                result = self._handle_basic_type(message, rule)

            elif not message.ctype and rule.type == "regex":
                result = self._handle_regex_type(message, rule)
            else:
                continue

            if result is None:
                return False

            elif not result:
                continue

            if msg := self.rendered_message(rule, result, message):
                try:
                    type(self)._local.flag = True
                    self.messagequeue.send_message("post_message", message=msg)
                    sent_any = True
                finally:
                    if hasattr(type(self)._local, "flag"):
                        del type(self)._local.flag

        return sent_any

    def _handle_basic_type(self, message: Notification, rule: NotificationRule) -> dict:
        return TemplateHelper().get_cache_context(message.to_dict()) or {}

    def _handle_regex_type(self, message: Notification, rule: NotificationRule) -> Optional[dict]:

        if not rule.yaml_content:
           return {}
        # 加载yaml
        yaml: CommentedMap = self._load_yaml_content(rule.yaml_content)
        # 获取提取器
        extractors = yaml.get("extractors")
        # 获取元数据字段
        meta_fields = self._extract_meta_fields(yaml)
        # 获取聚合配置
        aggregate: dict = yaml.get("Aggregate")
        if not extractors:
            logger.warn("rule extractors is empty")
            return {}
        context = self._extract_fields(message, extractors)
        # 过滤掉meta属性字段
        if meta_fields:
            try:
                meta = self._create_meta_instance(fields=meta_fields, context=context)
                mediainfo = self.chain.recognize_media(meta=meta)
                if mediainfo:
                    # 删除meta属性字段
                    context = {k: v for k, v in context.items() if k not in meta_fields}
                context = TemplateHelper().builder.build(meta=meta, mediainfo=mediainfo, include_raw_objects=False, **context)
            except Exception as e:
                logger.warn(f"Failed to create meta instance: {e}")

        if aggregate:
            required = aggregate.get("required", [])
            if all(field in context for field in required):
                logger.info(f"命中规则: {rule.name}")
                return MessageAggregator().add_message(rule, message, context)
            else:
                return {}
        logger.info(f"命中规则: {rule.name}")
        return context


    @staticmethod
    def _load_yaml_content(yaml_content) -> dict:
        if not yaml_content:
            return {}
        yaml = YAML()
        try:
            return yaml.load(yaml_content)
        except YAMLError as e:
            logger.error(f"YAML 解析失败: {e}")
            return {}
        except Exception as e:
            logger.error(f"[ERROR] 未知错误: {e}")
            return {}

    @staticmethod
    def _extract_fields(message: Notification, extractors: List[dict]) -> dict:
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
    def _extract_meta_fields(yaml_data: dict) -> dict:
        if not isinstance(yaml_data, dict):
            return {}

        meta_fields = yaml_data.get("MetaBase")
        if not isinstance(meta_fields, dict):
            return {}

        return {
            k: v for k, v in meta_fields.items()
            if k and v is not None and not isinstance(v, type(None))
        }

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

    @staticmethod
    @db_query
    def get_message_history(message: Notification, db: Session = None) -> Optional[Message]:
        try:
            result = db.query(Message).filter(
                Message.mtype == message.mtype.value,
                Message.title == message.title,
                Message.text == message.text
            ).order_by(Message.id.desc()).offset(1).first()
            return result
        except Exception as e:
            logger.error(f"获取message记录失败: {str(e)}")
            return None

