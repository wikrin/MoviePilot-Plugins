import datetime

from ruamel.yaml import YAML, YAMLError

from app.log import logger
from app.utils.timer import TimerUtils



class MessageTimeUtils:
    """消息时间处理工具类"""

    @staticmethod
    def now() -> datetime.datetime:
        return datetime.datetime.now()

    @staticmethod
    def get_send_time(first_time: str, send_in: float) -> datetime.datetime:
        """计算消息发送时间"""
        base_time = datetime.datetime.fromisoformat(first_time)
        return base_time + datetime.timedelta(hours=send_in)

    @staticmethod
    def get_delay_time(minutes: int = 0, hours: int = 0, days: int = 0) -> datetime.datetime:
        """
        获取延迟时间
        Args:
            minutes: 延迟分钟数
            hours: 延迟小时数
            days: 延迟天数
        Returns:
            延迟后的时间
        """
        return datetime.datetime.now() + datetime.timedelta(
            minutes=minutes,
            hours=hours,
            days=days
        )

    @staticmethod
    def add_time(base_time: datetime.datetime, minutes: int = 0, hours: int = 0, days: int = 0) -> datetime.datetime:
        """
        增加指定时间
        Args:
            base_time: 基准时间
            minutes: 增加的分钟数
            hours: 增加的小时数
            days: 增加的天数
        Returns:
            计算后的时间
        """
        return base_time + datetime.timedelta(
            minutes=minutes,
            hours=hours,
            days=days
        )

    @staticmethod
    def is_overtime(send_time: datetime.datetime) -> bool:
        """检查是否超时"""
        return TimerUtils.diff_minutes(send_time) > 0

    @staticmethod
    def now_iso() -> str:
        """获取当前时间的ISO格式字符串"""
        return datetime.datetime.now().isoformat()

    @staticmethod
    def is_within_cooldown(msg, cooldown_minutes: int) -> bool:
        """检查消息是否在冷却时间内"""
        if not msg:
            return False

        minutes = TimerUtils.diff_minutes(
            datetime.datetime.strptime(msg.reg_time, "%Y-%m-%d %H:%M:%S")
        )

        if minutes < cooldown_minutes:
            logger.info(f"上次发送消息 {minutes} 分钟前, 跳过此次发送。")
            return True
        return False


class YamlParser:
    """YAML解析工具类"""

    @staticmethod
    def parse(yaml_content: str) -> dict:
        """解析YAML内容"""
        if not yaml_content:
            return {}
        yaml = YAML()
        try:
            return yaml.load(yaml_content)
        except YAMLError as e:
            logger.error(f"YAML 解析失败: {e}")
            return {}

    @staticmethod
    def extract_meta_fields(yaml_data: dict) -> dict:
        """提取元数据字段"""
        if not isinstance(yaml_data, dict):
            return {}

        meta_fields = yaml_data.get("MetaBase")
        if not isinstance(meta_fields, dict):
            return {}

        return {
            k: v for k, v in meta_fields.items()
            if k and v is not None and not isinstance(v, type(None))
        }

