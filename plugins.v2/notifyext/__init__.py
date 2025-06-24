# 基础库
import copy

import threading
from typing import Any, Optional, Tuple, Dict, List

# 第三方库
from sqlalchemy.orm import Session

# 项目库
from app.db import db_query
from app.db.models.message import Message
from app.helper.message import MessageQueueManager, TemplateHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.message import Notification

from .aggregator import MessageAggregator
from .framehandler import registry
from .models import NotificationRule, TemplateConf
from .rulehandlers import RuleHandlerMeta
from .utils import MessageTimeUtils


class NotifyExt(_PluginBase):
    # 插件名称
    plugin_name = "消息通知扩展"
    # 插件描述
    plugin_desc = "拦截设定时间内重复消息，根据规则聚合/分发消息"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/wikrin/MoviePilot-Plugins/main/icons/message_a.png"
    # 插件版本
    plugin_version = "2.1.1"
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
    _local = threading.local()

    _rules: list[NotificationRule] = []
    _templates: dict[str] = {}

    # 配置属性
    _enabled: bool = False
    _cooldown: int = 0

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()
        # 加载插件配置
        self.load_config(config)
        # 加载规则配置
        self.load_configuration()
        # 初始化消息聚合
        self.aggregator = MessageAggregator(self)

    def load_config(self, config: dict):
        """加载配置"""
        if config:
            # 遍历配置中的键并设置相应的属性
            for key in (
                "enabled",
                "cooldown",
            ):
                setattr(self, f"_{key}", config.get(key, getattr(self, f"_{key}")))

    def load_configuration(self):
        """加载配置规则和模板"""
        self._rules = self.get_rules()
        _templates = self.get_templates()
        self._templates = {t.id: t.template for t in _templates}

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        """
        pass

    def stop_service(self):
        """退出插件"""
        try:
            if self.need_stop:
                self.aggregator.stop_task()
        except Exception as e:
            logger.error(f"退出插件失败：{str(e)}")

    @property
    def need_stop(self) -> bool:
        """
        判断插件是否需要退出
        """
        try:
            return self.aggregator.has_active_tasks
        except Exception:
            return False

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/templates",
                "endpoint": self.get_templates,
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
            {
                "path": "/frameitems",
                "endpoint": self.get_frame_items,
                "methods": ["GET"],
                "auth": "bear",  # 鉴权类型：apikey/bear
                "summary": "帧处理器项",
                "description": "获取frame方式的已实现项",
            },
        ]

    @property
    def _rules_key(self):
        return "notifyext_rules"

    @property
    def _templates_key(self):
        return "notifyext_templates"

    def get_templates(self) -> list[TemplateConf]:
        templates = self.get_data(key=self._templates_key) or []
        return [TemplateConf(**t) for t in templates]

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

    def get_frame_items(self) -> List:
        return registry.list_all()

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

        if message.mtype and MessageTimeUtils.is_within_cooldown(
            self.get_message_history(message), self._cooldown
        ):
            return False

        return self.handle_message(message)

    def handle_message(self, message: Notification) -> Optional[bool]:

        sent_any = None

        for rule in self._rules:
            if not rule.enabled:
                logger.debug(f"{rule.name} 未启用")
                continue

            if rule.switch and rule.switch != message.mtype.value:
                logger.debug(f"{rule.name}场景开关: {rule.switch} 不匹配消息类型 {message.mtype.value}")
                continue

            # 获取对应类型的处理器实例(单例)
            handler = RuleHandlerMeta.get_handler(rule.type)
            if not handler:
                continue
            if not handler.can_handle(message, rule):
                continue
            result = handler.handle(message, rule)

            if result is None:
                return False
            # 过滤空值
            result = {k: v for k, v in result.items() if v}
            if not result:
                continue

            if self.send_message(rule=rule, message=message, context=result):
                sent_any = True

        return sent_any

    def send_message(self, rule: NotificationRule, context: dict, message: Notification = None) -> bool:
        send = False
        if not (msg := self._rendered_message(rule, context, message)):
            return send
        try:
            type(self)._local.flag = True
            MessageQueueManager().send_message("post_message", message=msg)
            send = True
        finally:
            if hasattr(type(self)._local, "flag"):
                del type(self)._local.flag
            return send

    def _rendered_message(self, rule: NotificationRule, context: dict, message: Notification = None) -> Optional[Notification]:
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

