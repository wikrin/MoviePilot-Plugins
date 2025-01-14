# 基础库
import datetime
from typing import Any, Dict, List
import threading

# 第三方库
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

# 项目库
from app.chain.transfer import TransferChain
from app.core.config import settings
from app.core.event import eventmanager, Event
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.subscribe import Subscribe
from app.schemas.types import EventType, SystemConfigKey


class USyncIdentifiers(_PluginBase):
    # 插件名称
    plugin_name = "识别词单向同步"
    # 插件描述
    plugin_desc = "单向同步订阅自定义识别词至全局词表"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/wikrin/MoviePilot-Plugins/main/icons/unisync_a.png"
    # 插件版本
    plugin_version = "1.0.0"
    # 插件作者
    plugin_author = "Attente"
    # 作者主页
    author_url = "https://github.com/wikrin"
    # 插件配置项ID前缀
    plugin_config_prefix = "usyncidentifiers_"
    # 加载顺序
    plugin_order = 12
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _scheduler = None
    _lock = threading.Lock()

    # 配置属性
    _enabled: bool = False
    _notify: bool = False
    _onlyonce: bool = False

    def init_plugin(self, config: dict = None):
        self.transferchain = TransferChain()
        self.subscribeoper = SubscribeOper()
        self._custom_words = self.get_data("FullIdentifiers") or {}

        # 停止现有任务
        self.stop_service()
        self.load_config(config)

        if self._onlyonce:
            self.schedule_once()

    def load_config(self, config: dict):
        """加载配置"""
        if config:
            # 遍历配置中的键并设置相应的属性
            for key in (
                "enabled",
                "onlyonce",
            ):
                setattr(self, f"_{key}", config.get(key, getattr(self, f"_{key}")))

    def schedule_once(self):
        """调度一次性任务"""
        self._scheduler = BackgroundScheduler(timezone=settings.TZ)
        logger.info("订阅识别词同步，立即运行一次")
        self._scheduler.add_job(
            func=self.run_only_once,
            trigger='date',
            run_date=datetime.datetime.now(tz=pytz.timezone(settings.TZ))
            + datetime.timedelta(seconds=3),
        )
        self._scheduler.start()

        # 关闭一次性开关
        self._onlyonce = False
        self.__update_config()

    def __update_config(self):
        """更新设置"""
        self.update_config(
            {
                "enabled": self._enabled,
                "onlyonce": self._onlyonce,
            }
        )

    def get_form(self):
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 6, 'md': 3},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 6, 'md': 3},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
        }

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

    def get_api(self):
        pass

    def get_command(self):
        pass

    def get_page(self):
        pass

    def get_state(self):
        return self._enabled

    @eventmanager.register(EventType.SubscribeAdded)
    def handle_subscribe_added(self, event: Event):
        if not event or not self._enabled:
            return
        try:
            subscription_id = event.event_data.get("subscribe_id")
            if subscription := self.subscribeoper.get(subscription_id):
                custom_words_str = subscription.custom_words
                if custom_words_str:
                    custom_words_list = custom_words_str.split('\n')
                    self._update_custom_identifiers(custom_words_list, subscription_id)
        except Exception as e:
            logger.error(f"处理订阅添加事件失败：{str(e)}")

    @eventmanager.register([EventType.SubscribeDeleted, EventType.SubscribeComplete])
    def handle_subscribe_deleted_or_completed(self, event: Event):
        if not event or not self._enabled:
            return
        try:
            subscription_id = event.event_data.get("subscribe_id")
            subscription_info = event.event_data.get("subscribe_info", {})
            custom_words_str = subscription_info.get("custom_words")
            if custom_words_str:
                custom_words_list = custom_words_str.split('\n')
                self._remove_custom_identifiers(custom_words_list, subscription_id)
        except Exception as e:
            logger.error(f"处理订阅删除或完成事件失败：{str(e)}")

    # @eventmanager.register(EventType.SubscribeUpdated)
    def handle_subscribe_updated(self, event: Event):
        if not event or not self._enabled:
            return
        try:
            subscription_id = event.event_data.get("subscribe_id")
            subscription_info = event.event_data.get("subscribe_info", {})
            custom_words_str = subscription_info.get("custom_words")
            if custom_words_str:
                new_custom_words = custom_words_str.split('\n')
                old_custom_words = self._custom_words.get(str(subscription_id), [])
                self._update_custom_identifiers(new_custom_words, subscription_id, old_custom_words)
        except Exception as e:
            logger.error(f"处理订阅更新事件失败：{str(e)}")

    def run_only_once(self):
        if not self._enabled:
            return
        try:
            with self._lock:
                existing_custom_words = set()
                if self._custom_words:
                    existing_custom_words = {word for words in self._custom_words.values() for word in words}
                current_custom_identifiers = self.systemconfig.get(SystemConfigKey.CustomIdentifiers) or []
                current_custom_identifiers = [word for word in current_custom_identifiers if word not in existing_custom_words]
                subscriptions: list[Subscribe] = self.subscribeoper.list()
                subscription_custom_words = []
                for subscription in subscriptions:
                    custom_words_str = subscription.custom_words
                    if custom_words_str:
                        custom_words_list = custom_words_str.split('\n')
                        subscription_custom_words.extend(custom_words_list)
                        self._custom_words[str(subscription.id)] = custom_words_list
                if subscription_custom_words:
                    add_words = [word for word in subscription_custom_words if word not in current_custom_identifiers]
                    add_words.extend(current_custom_identifiers)
                    self.systemconfig.set(SystemConfigKey.CustomIdentifiers, add_words)
        except Exception as e:
            logger.error(f"处理一次性任务失败：{str(e)}")

    def _update_custom_identifiers(self, new_custom_words: List[str], subscription_id: int, old_custom_words: List[str] = None):
        with self._lock:
            current_custom_identifiers = self.systemconfig.get(SystemConfigKey.CustomIdentifiers) or []
            if old_custom_words:
                removed_words = set(old_custom_words) - set(new_custom_words)
                current_custom_identifiers = [word for word in current_custom_identifiers if word not in removed_words]
            added_words = [word for word in new_custom_words if word not in current_custom_identifiers]
            added_words.extend(current_custom_identifiers)
            self.systemconfig.set(SystemConfigKey.CustomIdentifiers, added_words)
            self._custom_words[str(subscription_id)] = new_custom_words
            self.save_data('FullIdentifiers', self._custom_words)

    def _remove_custom_identifiers(self, custom_words_to_remove: List[str], subscription_id: int):
        with self._lock:
            current_custom_identifiers = self.systemconfig.get(SystemConfigKey.CustomIdentifiers) or []
            removed_words = set(custom_words_to_remove)
            current_custom_identifiers = [word for word in current_custom_identifiers if word not in removed_words]
            self.systemconfig.set(SystemConfigKey.CustomIdentifiers, current_custom_identifiers)
            del self._custom_words[str(subscription_id)]
            self.save_data('FullIdentifiers', self._custom_words)