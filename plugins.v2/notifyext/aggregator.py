import re
from typing import Dict, Optional, TYPE_CHECKING

from app.core.cache import FileCache

from app.log import logger
from app.plugins.notifyext.models import AggregateConf, MessageGroup, NotificationRule
from app.scheduler import Scheduler, BackgroundScheduler
from app.schemas.message import Notification
from app.utils.singleton import Singleton

from .utils import TimeUtils


if TYPE_CHECKING:
    from . import NotifyExt


class MessageAggregator(metaclass=Singleton):
    """消息聚合器"""

    def __init__(self, plugin: "NotifyExt"):
        self.plugin = plugin
        self._messages: Dict[str, MessageGroup] = {}
        self._restore_state()

    def _need_aggregate(self, aggregate: AggregateConf, message: Notification) -> bool:
        """检查消息是否需要聚合"""
        if not aggregate:
            return False

        allowed_attrs = ['title', 'text']
        exclude_pattern = aggregate.exclude
        include_pattern = aggregate.include

        if not include_pattern and not aggregate.wait_time:
            return False

        # 收集有效的字符串属性值
        attr_values = [
            value for attr in allowed_attrs
            if (value := getattr(message, attr, None)) and isinstance(value, str)
        ]

        # 检查是否命中排除规则
        if exclude_pattern and any(match := re.search(exclude_pattern, value) for value in attr_values):
            logger.debug(f"匹配排除规则: {match}")
            return False

        # 检查是否命中包含规则
        if include_pattern:
            if any(match := re.search(include_pattern, value) for value in attr_values):
                logger.debug(f"匹配包含规则: {match}")
                return True
            return False

        return True

    def try_aggregate_message(self, message: Notification, rule: NotificationRule, context: dict) -> bool:
        """处理消息聚合"""
        if not self._need_aggregate(rule.aggregate, message):
            return False
        logger.debug(f"开始处理消息聚合")
        self.add_message(message, rule, context)
        return True

    def add_message(self, message: Notification, rule: NotificationRule, context: dict):
        wait_time = rule.aggregate.wait_time
        if wait_time <= 0:
            # 作为拦截处理
            logger.warn(f"聚合时间小于等于0，将作为拦截处理")
            return
        now = TimeUtils.now_iso()
        run_time = TimeUtils.get_delay_time(minutes=wait_time)
        if rule.id not in self._messages:
            self._messages[rule.id] = MessageGroup(
                rule=rule,
                wait_time=wait_time,
                message=message,
                first_time=now,
                last_time=now,
            )
            self._add_job(rule=rule, run_time=run_time)

        # 延迟任务
        self._reschedule_job(rule_id=rule.id, run_time=run_time)
        group = self._messages[rule.id]
        group.messages.append(context)
        group.last_time = now

        logger.info(f"title: {message.title} text: {message.text} 已添加至消息组")

    def _send_group(self, rule_id: str):
        group = self._messages.get(rule_id)
        if not group.messages:
            return

        merged = {
            "count": len(group.messages),
            "messages": list(group.messages),
            "first_time": group.first_time,
            "last_time": group.last_time,
        }

        # 渲染消息
        if self.plugin.send_message(message=group.message, rule=group.rule, context=merged):
            # 移除任务
            self._remove_job(rule_id)
            # 删除消息
            self._messages.pop(rule_id)
            # 保存状态
            self._save_state()

    def _save_state(self):
        FileCache().set(key=self.__class__.__name__, value=self._messages, region="aggregate_state")

    def _restore_state(self):
        self._messages = FileCache().get(key=self.__class__.__name__, region="aggregate_state") or {}
        if not self._messages:
            return
        now = TimeUtils.now()
        for group in self._messages.values():
            send_time = TimeUtils.get_send_time(group.first_time, group.wait_time)
            if send_time < now:
                # 超时延迟发送
                _send_time = TimeUtils.add_time(base_time=now, minutes=5)
                self._add_job(group.rule, _send_time)
            else:
                self._add_job(group.rule, send_time)

    def _add_job(self, rule: NotificationRule, run_time):
        if self._scheduler is None:
            return
        # 任务信息
        job_info = {
            "func": self._send_group,
            "name": f"发送 {rule.name} 消息组",
            "id": rule.id,
            "kwargs": {"rule_id": rule.id},
        }
        # 添加服务
        Scheduler()._jobs[rule.id] = {
            **job_info,
            "provider_name": self.plugin.plugin_name,
            "running": False,
        }
        # 添加任务
        self._scheduler.add_job(
            **job_info,
            trigger="date",
            run_date=run_time,
        )
        logger.info(f"{rule.name} 定时任务已添加")

    def _remove_job(self, rule_id: str = None):
        if rule_id and not self._messages.get(rule_id, None):
            return
        Scheduler().remove_plugin_job(self.plugin.__class__.__name__, rule_id)

    def _reschedule_job(self, rule_id, run_time):
        if self._scheduler is None:
            return
        self._scheduler.reschedule_job(rule_id, trigger="date", run_date=run_time)

    def stop_task(self):
        if not self._messages:
            return
        self._save_state()

    @property
    def _scheduler(self) -> Optional[BackgroundScheduler]:
        return Scheduler()._scheduler

    @property
    def has_active_tasks(self):
        return bool(self._messages)
