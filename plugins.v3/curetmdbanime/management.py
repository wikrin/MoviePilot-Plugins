"""管理能力检查和请求规则；传输由 runtime 负责，HTTP 错误由端点转换。"""

from __future__ import annotations

from collections.abc import Callable

from .runtime import ServiceController, ServiceError, redact


class ManagementClient:
    def __init__(self, runtime: ServiceController, token_provider: Callable[[], str]):
        self.runtime = runtime
        self._token_provider = token_provider

    def token(self) -> str:
        return self._token_provider()

    def probe(self) -> dict:
        self.runtime.token = self.token()
        return self.runtime.probe()

    def health(self, capability: str) -> dict:
        health = self.probe()
        if not health.get("capabilities", {}).get(capability):
            raise ServiceError("服务未开启此管理能力，请更新服务；开启鉴权时需配置匹配的管理令牌")
        if capability == "logs":
            if not health.get("capabilities", {}).get("persistent_logs"):
                raise ServiceError("服务不支持持久化日志分页，请部署新版二进制构建")
            if health.get("logging", {}).get("available") is False:
                raise ServiceError("持久化日志暂不可读，请修复存储并重启服务；可尝试仅实时输出", 503)
        return health

    def request(self, path: str, timeout: int = 3) -> dict:
        """调用方拥有命令锁；每次请求重新检查管理能力和当前令牌。"""
        try:
            self.health("logs" if path.startswith("/management/logs") else "upstreams")
            value = self.runtime.request(path, timeout)
            if not isinstance(value, dict) or not isinstance(value.get("items"), list):
                raise ServiceError("服务接口响应无效，请更新服务")
            return value
        except Exception as error:
            raise ServiceError(
                redact(str(error)) if isinstance(error, ServiceError) else "服务请求失败或超时",
                error.status_code if isinstance(error, ServiceError) else 502,
            ) from error
