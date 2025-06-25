from functools import wraps
import importlib
from abc import ABC, ABCMeta, abstractmethod
from typing import Any, Callable, ClassVar, Dict, List, Optional, Tuple, Type, Union

from app.log import logger
from app.schemas.message import Notification
from app.utils.singleton import SingletonClass

from ..frameinspector import FrameInspector
from ..models import NotificationRule, Notification, FrameHandlerItem, FrameResult


class RuleHandlerMeta(SingletonClass):
    """规则处理器元类"""
    _handlers: ClassVar[Dict[str, Type['BaseRuleHandler']]] = {}
    _default_handler: ClassVar[Optional[Type['BaseRuleHandler']]] = None

    def __new__(mcs, name, bases, attrs):
        cls = super().__new__(mcs, name, bases, attrs)

        # 跳过基类
        if ABC in bases:
            return cls

        # 注册默认处理器
        if mcs._is_default_handler(attrs):
            mcs._register_default_handler(cls)
            return cls

        # 注册指定类型处理器
        mcs._register_handlers_by_type(cls, attrs)

        return cls

    @classmethod
    def _is_default_handler(mcs, attrs) -> bool:
        """判断是否为默认处理器"""
        return attrs.get('default', False)

    @classmethod
    def _register_default_handler(mcs, cls):
        """注册默认处理器"""
        mcs._default_handler = cls
        logger.debug(f"Registered default handler: {cls.__name__}")

    @classmethod
    def _register_handlers_by_type(mcs, cls, attrs):
        """根据 handler_type 注册处理器"""
        handler_type = attrs.get('type')
        if not handler_type:
            return

        if isinstance(handler_type, list):
            for t in handler_type:
                mcs._handlers[t] = cls
                logger.debug(f"Registered message handler: {cls.__name__} for type {t}")
        else:
            mcs._handlers[handler_type] = cls
            logger.debug(f"Registered message handler: {cls.__name__} for type {handler_type}")

    @classmethod
    def get_handler(mcs, handler_type: str) -> Optional['BaseRuleHandler']:
        """获取处理器实例"""
        # 优先获取指定类型的处理器
        handler_class = mcs._handlers.get(handler_type)
        if handler_class:
            return handler_class()
        # 找不到对应类型的处理器，返回默认处理器
        if mcs._default_handler:
            return mcs._default_handler()
        return None


class BaseRuleHandler(ABC, metaclass=RuleHandlerMeta):
    """消息处理器基类"""
    type: ClassVar[Union[str, List[str]]] = None

    @abstractmethod
    def can_handle(self, message: Notification, rule: NotificationRule) -> bool:
        pass

    @abstractmethod
    def handle(self, message: Notification, rule: NotificationRule) -> Optional[Dict]:
        pass


class FrameHandlerMetaclass(ABCMeta):
    """帧处理器元类"""

    def __new__(mcs, name, bases, attrs):

        cls = super().__new__(mcs, name, bases, attrs)

        # 跳过元类自身
        if not (ABC in bases or name == "FrameHandlerMetaclass"):

            # 注册带有 __is_handler__ 标志的方法
            mcs._register_handlers(cls, attrs)

            # 将第一个参数名为 cls 的普通方法转为类方法
            mcs._convert_classmethods(cls, attrs)

        return cls

    @classmethod
    def _register_handlers(mcs, cls, attrs):
        """
        注册所有标记为 handler 的方法
        """
        for attr_name, attr_value in attrs.items():
            if not hasattr(attr_value, "__is_handler__"):
                continue

            # 提取 handler 元信息
            handler_info = mcs._build_handler_info(cls, attr_name, attr_value)
            registry._handlers[handler_info["name"]] = handler_info["data"]

            logger.debug(f"Registering frame handler: {handler_info['data']['category']} - {handler_info['name']}")

    @classmethod
    def _build_handler_info(mcs, cls, attr_name, attr_value):
        """
        构建 handler 注册信息
        """
        category = getattr(attr_value, "__category__", None)
        effective_category = category or getattr(cls, "category", "")

        qualified_name = f"{cls.__name__}.{attr_name}"
        _doc = attr_value.__doc__ or ""

        return {
            "name": qualified_name,
            "data": {
                "func_path": f"{attr_value.__module__}.{qualified_name}",
                "label": mcs._extract_tag(_doc, ":label"),
                "category": effective_category,
                "description": cls.__doc__,
            }
        }

    @classmethod
    def _convert_classmethods(mcs, cls, attrs):
        """
        将第一个参数名为 cls 的方法包装成类方法
        """
        from inspect import signature

        for attr_name, attr_value in attrs.items():
            if callable(attr_value) and not isinstance(attr_value, (classmethod, staticmethod)):
                try:
                    sig = signature(attr_value)
                    params = list(sig.parameters.values())
                    if params and params[0].name == "cls":
                        wrapped = classmethod(attr_value)
                        setattr(cls, attr_name, wrapped)
                except ValueError:
                    pass  # 忽略无法解析签名的对象

    def _extract_tag(doc: str, tag: str) -> str:
        """
        从 docstring 中提取指定标签后的内容。

        :param doc: 原始 docstring
        :param tag: 要提取的标签（如 ":label"）
        :return: 提取到的内容 或 ""
        """
        if not doc:
            return ""

        lines = [line.strip() for line in doc.strip().splitlines()]

        for line in lines:
            if line.startswith(tag):
                content = line[len(tag):].split("\n", 1)[0].strip()
                return content

        return ""


class BaseFrameHandler(ABC, metaclass=FrameHandlerMetaclass):
    """
    抽象帧处理器基类，所有帧处理器都应继承此类
    """
    # 配置字段
    depth: int = 15
    skip: int = 2
    category: str = ""

    @classmethod
    def get_config(cls) -> dict:
        return {
            "cls_name": cls.__name__.removesuffix("Handler"),
            "depth": cls.depth,
            "skip": cls.skip,
        }

    @classmethod
    def extract(cls, **kwargs) -> dict:
        """
        提取上下文逻辑
        """
        config = cls.get_config()
        config.update(kwargs)
        return FrameInspector.inspect(config)


class HandlerRegistry:
    def __init__(self):
        self._handlers: Dict[str, Dict] = {}

    def register(self, f=None, *, category: str = None):
        """
        注册处理器方法的装饰器工厂函数
        """
        def decorator(func):
            # 标记为处理器方法
            func.__is_handler__ = True
            func.__category__ = category

            @wraps(func)
            def wrapper(*args, **kwargs):

                result = func(*args, **kwargs)

                # 统一返回值
                return self._wrap_result(result)

            return wrapper

        return decorator if f is None else decorator(f)

    def get_handler(self, name: str) -> Optional[Callable]:
        if name not in self._handlers:
            return None

        if func_path := self._handlers[name].get("func_path"):
            try:
                return self.load_handler(func_path)
            except (ImportError, ValueError, AttributeError) as e:
                logger.error(f"无法加载 handler 函数：{func_path} {e}")

        return None

    def list_all(self) -> List[FrameHandlerItem]:
        return [
            FrameHandlerItem(name=name, **handler)
            for name, handler in self._handlers.items()
        ]

    @classmethod
    def _wrap_result(cls, result: Any) -> Optional[Dict[str, Any]]:
        """
        统一处理处理器方法的返回值
        :param result: 原始返回结果
        :return: 处理后的结果
        """
        try:
            # 如果结果已经是 FrameResult 直接返回实例
            if isinstance(result, FrameResult):
                return result

            if isinstance(result, dict):
                # 是字典且包含必要的数据
                if "meta" in result:
                    meta = result.pop("meta")
                    return FrameResult(
                        need_media_info=True, meta=meta, context=result
                    )

                return FrameResult(need_media_info=False, context=result)

            # 结果不符合要求的格式，记录警告
            logger.warning(
                f"Invalid output format from {cls.__name__}: {type(result)}"
            )
            return None

        except Exception as e:
            logger.error(
                f"Error executing handler {cls.__name__}: {str(e)}",
                exc_info=True,
            )
            return None

    @staticmethod
    def load_handler(
            func_path: str,
        ) -> Optional[Tuple[Type[BaseFrameHandler], Callable]]:
        """
        从 func_path 加载 handler 方法。
        func_path 格式应为 "module.class_name.method_name"
        """
        # 分割模块、类、方法
        parts = func_path.rsplit(".", 2)  # 最多分割两部分：模块.类.方法
        if len(parts) != 3:
            logger.error(f"无效的函数路径：{func_path}")
            return None

        module_path, class_name, method_name = parts

        # 导入模块
        module = importlib.import_module(module_path)

        # 获取类
        cls = getattr(module, class_name, None)
        if not isinstance(cls, type):
            logger.error(f"找不到类 {class_name} 在模块 {module_path}")
            return None

        # 获取类方法
        func = getattr(cls, method_name, None)
        if not callable(func):
            logger.error(f"{class_name}.{method_name} 不是一个可调用的方法")
            return None

        # 验证是否是注册过的 handler
        if not getattr(func, "__is_handler__", False):
            logger.warning(
                f"{class_name}.{method_name} 不是一个有效的 handler 方法"
            )
            return None

        return func


# 全局注册中心实例
registry = HandlerRegistry()