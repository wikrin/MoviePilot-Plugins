# 基础库
import datetime
from typing import Any, Dict, List, Optional, Tuple

# 第三方库
from pydantic import BaseModel
from sqlalchemy.orm import Session

# 项目库
from app.db import db_query
from app.db.models.message import Message
from app.helper.message import MessageQueueManager, TemplateHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.message import Notification


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
    # 媒体类型(None为全部, 可选值: movie, tv)
    media_type: Optional[str] = None
    # 媒体类别
    media_category: List[str] = []
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
    plugin_version = "1.0.0"
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

    # 配置属性
    _enabled: bool = False
    _cooldown: int = 0

    def init_plugin(self, config: dict = None):
        self.messagequeue = MessageQueueManager()
        self.templatehelper = TemplateHelper()

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
        if message.channel or message.source:
            # 存在渠道或源, 交由主程序处理
            return None
        if not message.mtype:
            # 不存在消息类型，可能为交互消息，交由主程序处理
            return None
        if self.is_within_notification_cooldown(message):
            # 冷却时间未到，不发送消息
            return False
        if not message.ctype:
            # 不存在消息内容类型，交由主程序处理
            return None
        # 根据消息获取上下文
        context = self.templatehelper.get_cache_context(message.to_dict())
        if not context:
            # 获取不到上下文，交由主程序处理
            return None
        rules = self.get_rules()
        templates = self.get_templates()
        flag = None
        for rule in rules:
            if not rule.enabled:
                continue

            if rule.media_type and rule.media_type != context.get("type"):
                continue

            if rule.media_category and context.get("category") not in rule.media_category:
                continue

            template_id = getattr(rule, message.ctype.value, None)
            if not template_id and template_id not in templates:
                continue

            # 解析模板
            template_content = self.templatehelper.parse_template_content(templates[template_id])
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

            message.source = rule.target
            self.messagequeue.send_message("post_message", message=message)
            flag = True

        return flag

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

    def get_rules(self) -> list[NotificationRule]:
        rules = self.get_data(key=self._rules_key) or []
        return [NotificationRule(**rule) for rule in rules]

    def save_rules(self, rules: list[NotificationRule]):
        data = [rule.dict() for rule in rules]
        return self.save_data(key=self._rules_key, value=data)

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
