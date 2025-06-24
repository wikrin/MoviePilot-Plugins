from typing import Dict, Optional, TYPE_CHECKING

from app.log import logger
from app.plugins.notifyext.models import MessageGroup, NotificationRule
from app.scheduler import Scheduler, BackgroundScheduler
from app.schemas.message import Notification
from app.utils.singleton import SingletonClass

from .utils import MessageTimeUtils


if TYPE_CHECKING:
    from . import NotifyExt


class MessageAggregator(metaclass=SingletonClass):
    """消息聚合器"""

    def __init__(self, plugin: "NotifyExt"):
        self.plugin = plugin
        self._messages: Dict[str, MessageGroup] = {}
        self._restore_state()

    def try_aggregate_message(self, rule: NotificationRule, message: Notification,
                         context: dict, aggregate_config: dict) -> Optional[Dict]:
        """处理消息聚合"""
        required = aggregate_config.get("required", [])
        if all(field in context for field in required):
            # 发送延迟
            send_on = aggregate_config.get("send_on", 2)
            logger.info(f"命中规则: {rule.name}")
            self.add_message(rule, message, context, send_on)
            return True
        return False

    def add_message(self, rule: NotificationRule, message: Notification, context: dict, send_in: float):
        now = MessageTimeUtils.now_iso()
        if rule.id not in self._messages:
            self._messages[rule.id] = MessageGroup(
                rule=rule,
                send_in=send_in,
                message=message,
                first_time=now,
                last_time=now,
            )
            run_time = MessageTimeUtils.get_delay_time(hours=send_in)
            self.add_job(rule=rule, run_time=run_time)

        group = self._messages[rule.id]
        group.messages.append(context)
        group.last_time = now.isoformat()

        logger.info(f"{message} 已添加至消息组")

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
        if self.plugin.send_message(rule=group.rule, context=merged, message=group.message):
            # 移除任务
            self.remove_job(rule_id)
            # 删除消息
            self._messages.pop(rule_id)
            # 保存状态
            self._save_state()

    def _save_state(self):
        state = {k: v.dict() for k, v in self._messages.items()}
        self.plugin.save_data("aggregate_state", state)

    def _restore_state(self):
        state = self.plugin.get_data("aggregate_state") or {}
        if not state:
            return
        for rule_id, group_data in state.items():
            self._messages[rule_id] = MessageGroup(**group_data)
        if not self._messages:
            return
        now = MessageTimeUtils.now()
        for group in self._messages.values():
            send_time = MessageTimeUtils.get_send_time(group.first_time, group.send_in)
            if send_time < now:
                # 超时延迟发送
                _send_time = MessageTimeUtils.add_time(base_time=now, minutes=5)
                self.add_job(group.rule, _send_time)
            else:
                self.add_job(group.rule, send_time)

    def add_job(self, rule: NotificationRule, run_time):
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

    def remove_job(self, rule_id: str = None):
        if rule_id and not self._messages.get(rule_id, None):
            return
        Scheduler().remove_plugin_job(self.plugin.__class__.__name__, rule_id)

    def stop_task(self):
        if not self._messages:
            return
        self._save_state()
        self.remove_job()

    @property
    def _scheduler(self) -> Optional[BackgroundScheduler]:
        return Scheduler()._scheduler

    @property
    def has_active_tasks(self):
        return bool(self._messages)