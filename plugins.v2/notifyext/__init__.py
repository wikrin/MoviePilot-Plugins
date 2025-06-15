# 基础库
import datetime
import re
from typing import Any, Dict, List, Optional, Tuple

# 第三方库
from pydantic import BaseModel
from sqlalchemy.orm import Session
from ruamel.yaml import YAML, CommentedMap
from ruamel.yaml.error import YAMLError


# 项目库
from app.core.metainfo import MetaInfo
from app.db import db_query
from app.db.models.message import Message
from app.helper.message import MessageQueueManager, TemplateHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.message import Notification
from app.schemas.types import MediaType


class TemplateConf(BaseModel):
    name: str
    id: str
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
    # 规则类型 (可选值: regex, ctype)
    type: Optional[str] = "ctype"
    # YAML 配置
    yaml_content: Optional[str] = None
    # 媒体类型(None为全部, 可选值: movie, tv)
    media_type: Optional[str] = None
    # 媒体类别
    media_category: List[str] = []
    # 正则模式模板ID
    template_id: Optional[str] = None
    # 订阅添加
    subscribeAdded: Optional[str] = None
    # 订阅完成
    subscribeComplete: Optional[str] = None
    # 入库成功
    organizeSuccess: Optional[str] = None
    # 下载添加
    downloadAdded: Optional[str] = None


class NotifyExt(_PluginBase):
    # 插件名称
    plugin_name = "消息通知扩展"
    # 插件描述
    plugin_desc = "扩展消息通知模块功能"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/wikrin/MoviePilot-Plugins/main/icons/message_a.png"
    # 插件版本
    plugin_version = "1.1.0"
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
    _rules_key: str = "notifyext_rules"
    _templates_key: str = "notifyext_templates"
    _rules: dict = {}
    _templates: dict = {}

    # 配置属性
    _enabled: bool = False
    _cooldown: int = 0

    def init_plugin(self, config: dict = None):
        self.messagequeue = MessageQueueManager()
        self.templatehelper = TemplateHelper()

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
                self._scheduler.remove_all_jobs()
                self._scheduler.shutdown()
                self._scheduler = None
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
        return {"post_message": self.post_message}

    def post_message(self, message: Notification):
        """
        挟持主程序消息模块
        """
        if message.mtype and self.is_within_notification_cooldown(message):
            # 非交互消息且冷却时间未到，不发送消息
            return False
        for rule in self._rules:
            if not rule.enabled:
                continue

            if rule.type == "regex":
                template_id, context = self.regex_handle(message, rule)
            else:
                template_id, context = self.ctype_handle(message, rule)

            if not context and not template_id:
                continue

            if template_id not in self._templates:
                continue

            if rule.media_type and rule.media_type != context.get("type"):
                continue

            if rule.media_category and context.get("category") not in rule.media_category:
                continue

            # 解析模板
            template_content = self.templatehelper.parse_template_content(self._templates[template_id])
            if not template_content:
                continue

            rendered = self.templatehelper.render_with_context(template_content, context)
            if not rendered:
                continue

            rendered = self.templatehelper._TemplateHelper__process_formatted_string(rendered)
            if isinstance(rendered, dict):
                for key, value in rendered.items():
                    if hasattr(message, key):
                        setattr(message, key, value)

            message.source = message.source or rule.target
        # 修改引用的消息对象, return None 继续执行run_module
        return None

    def ctype_handle(self, message: Notification, rule: NotificationRule):
        context = self.templatehelper.get_cache_context(message.to_dict())
        template_id = getattr(rule, message.ctype.value, None)
        return template_id, context

    def regex_handle(self, message: Notification, rule: NotificationRule) -> Optional[Tuple[str, dict]]:
        # 正则匹配消息内容
        if rule.type != "regex":
            return None, None
        if not rule.yaml_content:
            logger.warn("rule yaml is empty")
            return None, None
        if not rule.template_id:
            logger.warn("rule template_id is empty")
            return None, None
        yaml: CommentedMap = self.load_yaml_content(rule.yaml_content)
        extractors = yaml.get("extractors")
        meta_fields = self._extract_meta_fields(yaml)
        if not extractors:
            logger.warn("rule extractors is empty")
            return None, None
        context = self._extract_fields(message, extractors)
        # 过滤掉meta属性字段
        if meta_fields:
            try:
                meta = self.create_meta_instance(fields=meta_fields, context=context)
                mediainfo = self.chain.recognize_media(meta=meta)
                if mediainfo:
                    # 删除meta属性字段
                    context = {k: v for k, v in context.items() if k not in meta_fields}
                context = self.templatehelper.builder.build(meta=meta, mediainfo=mediainfo, **context)
            except Exception as e:
                logger.warn(f"Failed to create meta instance: {e}")

        return rule.template_id, context

    def _extract_fields(self, message: Notification, extractors):
        if not isinstance(extractors, list):
            return None
        context = {}
        for extractor in extractors:
            field = extractor.get("field")
            if not field:
                continue

            raw_value = getattr(message, field, None)
            if not raw_value:
                continue

            text = str(raw_value)

            for key in extractor:
                if key == "field":
                    continue
                pattern = extractor[key]
                try:
                    match = re.search(pattern, text)
                    if match:
                        # 如果有命名组，则更新所有命名组
                        if match.groupdict():
                            context.update(match.groupdict())
                        else:
                            # 否则直接保存整个匹配结果
                            context[key] = match.group(0)
                except re.error as e:
                    logger.warn(f"字段 '{key}' 正则表达式无效：{pattern}，错误：{e}")

        return context

    @staticmethod
    def create_meta_instance(fields: Dict[str, str], context: Dict) -> Optional[MetaInfo]:
        """
        使用 context 构造 MetaInfo 实例，并注入额外字段
        :param fields: 要注入的字段字典（如 title, type）
        :param context: 正则提取出的字段字典（如 name, year）
        :return: 构造完成的 MetaInfo 对象 或 None
        """
        if not isinstance(fields, dict) or not isinstance(context, dict):
            return None

        title_key = fields.get('title')
        if not title_key or not context.get(title_key):
            return None

        # 构建 MetaInfo 实例
        meta = MetaInfo(
            title=context[title_key],
            subtitle=context.get(fields.get('subtitle'))
        )

        # 字段映射规则
        def map_type(value_key):
            value = context.get(value_key)
            return MediaType.MOVIE if value in ("movie", "电影") else MediaType.TV

        field_mapping = {
            "type": map_type,
        }

        for key, value_key in fields.items():
            if key in ['title', 'subtitle']:
                continue

            if key in field_mapping:
                setattr(meta, key, field_mapping[key](value_key))
            elif (value := context.get(value_key)) is not None:
                setattr(meta, key, value)

        return meta

    @staticmethod
    def load_yaml_content(yaml_content) -> dict:
        yaml = YAML()
        try:
            return yaml.load(yaml_content)
        except YAMLError as e:
            logger.error(f"YAML 解析失败: {e}")
            return {}
        except Exception as e:
            logger.error(f"[ERROR] 未知错误: {e}")
            return {}

    def templates(self) -> list[TemplateConf]:
        """
        获取模板
        """
        templates = self.get_data(key=self._templates_key) or []
        return [TemplateConf(**t) for t in templates]

    def get_templates(self) -> dict[str, str]:
        templates = self.templates()
        return{t.id: t.template for t in templates}

    def save_templates(self, templates: list[TemplateConf]):
        data = [t.dict() for t in templates]
        self.save_data(key=self._templates_key, value=data)
        # 更新self属性
        self._templates = {t.id: t.template for t in templates}

    def get_rules(self) -> list[NotificationRule]:
        rules = self.get_data(key=self._rules_key) or []
        return [NotificationRule(**rule) for rule in rules]

    def save_rules(self, rules: list[NotificationRule]):
        data = [rule.dict() for rule in rules]
        self.save_data(key=self._rules_key, value=data)
        # 更新self属性
        self._rules = rules

    def is_within_notification_cooldown(self, message: Notification):
        """
        判断消息是否在冷却时间内。
        """
        if result := self.get_message_history(message):
            last_time = datetime.datetime.strptime(result.reg_time, "%Y-%m-%d %H:%M:%S")
            now_time = datetime.datetime.now()
            cooldown_minutes = (now_time - last_time).total_seconds() // 60
            if cooldown_minutes < self._cooldown:
                logger.info(f"上次发送消息 {cooldown_minutes} 分钟前, 跳过此次发送。")
                return True
        return False

    @staticmethod
    def _extract_meta_fields(yaml_data) ->  dict:
        """
        提取并过滤有效的 Meta 字段。
        """
        def is_valid_meta_field(key: str, value: Any):
            """
            判断是否为有效的 Meta 字段。
            """
            return key and value is not None and not isinstance(value, (type(None)))

        if not isinstance(yaml_data, dict):
            return {}

        meta_fields = yaml_data.get("MetaBase")
        if not isinstance(meta_fields, dict):
            return {}

        return {
            k: v for k, v in meta_fields.items()
            if is_valid_meta_field(k, v)
        }

    @staticmethod
    @db_query
    def get_message_history(message: Notification, db: Session = None) -> Optional[Message]:
        """获取历史消息"""
        try:
            result = db.query(Message).filter(Message.mtype == message.mtype.value,
                                              Message.title == message.title,
                                              Message.text == message.text).order_by(
                Message.id.desc()).offset(1).first()
            return result
        except Exception as e:
            logger.error(f"获取message记录失败: {str(e)}")
            return None
