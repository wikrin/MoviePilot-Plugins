# 基础库
import datetime
from typing import Any, Dict, List
import threading

# 第三方库
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

# 项目库
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
    _onlyonce: bool = False

    def init_plugin(self, config: dict = None):
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
    
    def run_only_once(self):
        """
        处理一次性任务
        """
        try:
            subscriptions: list[Subscribe] = self.subscribeoper.list()
            for subscription in subscriptions:
                if custom_words_str := subscription.custom_words:
                    logger.info(f"{subscription.name} ({subscription.year}) 开始同步识别词")
                    self._add(custom_words_str.split('\n'), subscription.id)
                else:
                    logger.info(f"{subscription.name} ({subscription.year}) 未设置识别词")
        except Exception as e:
            logger.error(f"处理一次性任务失败：{str(e)}")

    @eventmanager.register(EventType.SubscribeAdded)
    def handle_subscribe_added(self, event: Event):
        """
        处理订阅添加事件
        """
        self._handle_subscription_event(event, self._add)

    @eventmanager.register([EventType.SubscribeDeleted, EventType.SubscribeComplete])
    def handle_subscribe_deleted_or_completed(self, event: Event):
        """
        处理订阅删除或完成事件
        """
        self._handle_subscription_event(event, self._remove)

    @eventmanager.register(EventType.SubscribeModified)
    def handle_subscribe_updated(self, event: Event):
        """
        处理订阅更新事件
        """
        self._handle_subscription_event(event, self._update)

    def _handle_subscription_event(self, event: Event, handler):
        """
        处理订阅事件
        :param event: 事件
        :param handler: 处理函数
        """
        if not event or not self._enabled:
            return
        try:
            # 获取订阅ID
            subscription_id = event.event_data.get("subscribe_id")
            # 获取订阅信息
            subscription_info = event.event_data.get("subscribe_info", {})
            if subscription_id and not subscription_info:
                # 订阅添加事件从数据库获取订阅信息
                subscription_info = self.subscribeoper.get(subscription_id).to_dict()
            if custom_words_str := subscription_info.get("custom_words"):
                custom_words_list = custom_words_str.split('\n')
                handler(custom_words_list, subscription_id)
        except Exception as e:
            logger.error(f"处理订阅事件失败：{str(e)}")

    def _add(self, add_words: List[str], subscription_id: int, index: int = 0):
        """
        添加识别词至词表
        :param add_words: 添加的识别词
        :param subscription_id: 订阅ID
        :param index: 插入位置
        """
        with self._lock:
            current_identifiers = self.systemconfig.get(SystemConfigKey.CustomIdentifiers) or []
            # 去重
            current_identifiers = [word for word in current_identifiers if word not in add_words]
            # 插入到指定位置
            current_identifiers[index:index] = add_words
            self.systemconfig.set(SystemConfigKey.CustomIdentifiers, current_identifiers)
            logger.info(f"成功添加 {add_words} 至词表第 {index} 行")
            self._custom_words[str(subscription_id)] = add_words
            self.save_data('FullIdentifiers', self._custom_words)

    def _remove(self, remove_words: List[str], subscription_id: int) -> int:
        """
        从词表移除识别词
        :param remove_words: 待移除的识别词
        :param subscription_id: 订阅ID
        :return: 移除识别词的起始索引
        """
        _index = 0
        with self._lock:
            if current_identifiers := self.systemconfig.get(SystemConfigKey.CustomIdentifiers) or []:
                # 获取识别词位置
                try:
                    for word in remove_words:
                        _index = current_identifiers.index(word)
                        # 获取到索引即退出
                        break
                except ValueError:
                    pass
                logger.info(f"从词表移除识别词：{remove_words}")
                current_identifiers = [word for word in current_identifiers if word not in remove_words]
                self.systemconfig.set(SystemConfigKey.CustomIdentifiers, current_identifiers)
                if str(subscription_id) in self._custom_words:
                    del self._custom_words[str(subscription_id)]
                self.save_data('FullIdentifiers', self._custom_words)
        return _index
    
    def _update(self, new_words: List[str], subscription_id: int, index: int = 0):
        """
        更新识别词至词表
        :param new_custom_words: 待添加的识别词
        :param subscription_id: 订阅ID
        :param _index: 插入位置
        """
        if old_words := self._custom_words.get(str(subscription_id), []):
            if old_words == new_words:
                # 识别词未改变，跳过更新
                return
            # 同一剧集顺序不同先移除全部
            index = self._remove(old_words, subscription_id)
        self._add(new_words, subscription_id, index)