"""托管进程和外部服务连接。生命周期操作串行，工作线程只使用启动时快照。"""

from __future__ import annotations

import asyncio
import os
import platform
import re
import subprocess
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import requests
from anyio import CancelScope
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.network import AsyncRequestUtils


class ServiceError(RuntimeError):
    def __init__(self, message, status_code=502):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class ServiceTarget:
    address: str
    token: str = field(repr=False)
    event: threading.Event = field(repr=False)
    generation: int


def redact(message: str) -> str:
    message = re.sub(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+", r"\1[REDACTED]", message)
    message = re.sub(
        r"(?i)(api_key|apikey|token|authorization|password|proxy)=([^\s&]+)",
        r"\1=[REDACTED]",
        message,
    )
    return re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", message)[:4096]


class ServiceController:
    def __init__(self, directory: Path, name: str, version: str, ready, disconnected):
        self.directory, self.name, self.version = directory, name, version
        self.ready, self.disconnected = ready, disconnected
        self.command_lock = threading.Lock()
        self.lock = threading.RLock()
        self.thread = None
        self.process = None
        self.event = threading.Event()
        self.state = "stopped"
        self.error = ""
        self.installed_version = ""
        self.config = None
        self.token = ""
        self.health = None
        self.generation = 0
        self.process_metrics = None
        self._last_process_cpu = None
        self._last_metrics_at = None
        self._socket_counters = {}
        self._network_totals = {"rx_bytes": 0, "tx_bytes": 0}
        self._metrics_sample_lock = threading.Lock()

    @property
    def executable(self):
        return self.directory / self.name

    @property
    def base_url(self):
        return (
            self.config.external_url
            if self.config.service_mode == "external"
            else f"http://127.0.0.1:{self.config.port}"
        )

    def log(self, level, message):
        """插件自身的安装、控制和连接事件只写入日志。"""
        getattr(logger, "warning" if level == "warn" else level, logger.info)(redact(str(message)))

    def snapshot(self):
        with self.lock:
            process = self.process
        if process and process.poll() is None:
            # 状态请求也触发采样，避免仅依赖监控循环导致指标长期为空。
            self._sample_process_metrics(process)
        with self.lock:
            process = self.process
            pid = process.pid if process and process.poll() is None else None
            metrics = (
                dict(self.process_metrics)
                if pid and self.process_metrics and self.process_metrics["pid"] == pid
                else None
            )
            return {
                "log_available": bool(
                    (self.health or {}).get("capabilities", {}).get("logs")
                    and (self.health or {}).get("logging", {}).get("available")
                ),
                "log_stream_available": bool((self.health or {}).get("capabilities", {}).get("log_stream")),
                "state": self.state,
                "error": self.error,
                "mode": self.config.service_mode if self.config else "local",
                "address": self.base_url if self.config else "",
                "pid": pid,
                "process_metrics": metrics,
                "installed_version": self.installed_version,
                "required_version": self.version,
                "binary_exists": self.executable.exists(),
                "health": self.health,
                "busy": self.command_lock.locked(),
                "controllable": bool(self.config and self.config.service_mode == "local"),
            }

    @staticmethod
    def management_headers(path, token):
        # bearer 不转发到二进制；公开健康/映射请求不携带管理令牌。
        return (
            {"Authorization": f"Bearer {token}"}
            if token and path.startswith(("/management/logs", "/management/upstreams"))
            else {}
        )

    @staticmethod
    def check_response(response, path):
        if response.status_code in (401, 403):
            # 上游 401 不能触发浏览器的 MoviePilot 会话签退。
            raise ServiceError("管理令牌未配置或不匹配，请检查服务 MANAGEMENT_TOKEN")
        if response.status_code == 503 and path.startswith("/management/logs"):
            raise ServiceError("服务日志存储或补发暂不可用，请检查服务并修复存储", 503)
        if response.status_code == 429:
            raise ServiceError("服务请求过多或日志流连接数已满，请稍后重试", 429)
        if response.status_code != 200:
            raise ServiceError(f"服务接口返回 HTTP {response.status_code}，可能需要更新服务")

    def request(self, path, timeout=3, address=None):
        # 管理流量直接到配置地址，避免环境代理拦截局域网容器。
        with self.lock:
            url = (address or self.base_url) + path
            headers = self.management_headers(path, self.token)
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(url, headers=headers, timeout=timeout, allow_redirects=False)
            self.check_response(response, path)
            return response.json()

    def target(self):
        with self.lock:
            if self.config is None:
                raise ServiceError("服务配置尚未初始化", 409)
            return ServiceTarget(self.base_url, self.token, self.event, self.generation)

    def target_current(self, target):
        with self.lock:
            return bool(
                self.config
                and not target.event.is_set()
                and self.event is target.event
                and self.generation == target.generation
                and self.base_url == target.address
                and self.token == target.token
            )

    @asynccontextmanager
    async def open_log_stream(self, params, target):
        """仅在页面订阅期间连接上游；SDK 上下文负责关闭响应/客户端。"""
        if not self.target_current(target):
            raise ServiceError("服务已停止或配置已变化，请刷新后重试", 409)
        path = "/management/logs/stream"
        headers = {
            "Accept": "text/event-stream",
            **self.management_headers(path, target.token),
        }
        client = AsyncRequestUtils(
            headers=headers,
            timeout=45,
            trust_env=False,
            follow_redirects=False,
            verify=True,
            http2=False,
        )
        async with client.get_stream(
            target.address + path,
            params=params,
            raise_exception=True,
            timeout=(5, 45, 5, 5),
        ) as response:
            self.check_response(response, path)
            if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "text/event-stream":
                raise ServiceError("服务未返回有效日志流，请更新服务")
            if not self.target_current(target):
                raise ServiceError("服务已停止或配置已变化，请刷新后重试", 409)
            yield response

    async def iter_log_stream(self, response, target):
        """字节透传及背压；每 250ms 检查生命周期，不截取或缓存日志。"""
        reader = response.aiter_bytes().__aiter__()
        pending = None
        try:
            while self.target_current(target):
                if pending is None:
                    pending = asyncio.create_task(anext(reader))
                done, _ = await asyncio.wait((pending,), timeout=0.25)
                if not done:
                    continue
                chunk = pending.result()
                pending = None
                if not self.target_current(target):
                    break
                if chunk:
                    yield chunk
        except StopAsyncIteration:
            pass
        except Exception:  # noqa: BLE001 -- 已发响应头，只能结束流让客户端按游标重连
            pass
        finally:
            with CancelScope(shield=True):
                if pending is not None:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                close = getattr(reader, "aclose", None)
                if close:
                    await close()

    def probe(self):
        try:
            health = self.request("/management/health", timeout=2)
            if (
                not isinstance(health, dict)
                or health.get("service") != "curetmdbanime"
                or health.get("api_version") != 1
                or not isinstance(health.get("capabilities"), dict)
                or not isinstance(health.get("logging", {}), dict)
            ):
                raise ServiceError("地址不是受支持的 CureTMDbAnime 管理服务")
            with self.lock:
                self.health = health
            return health
        except Exception as error:
            with self.lock:
                self.health = None
            if isinstance(error, ServiceError):
                raise
            raise ServiceError("服务连接失败或超时") from error

    def start(self, config, token):
        if self.thread and self.thread.is_alive():
            raise ServiceError("已有服务任务尚未结束")
        self.generation += 1
        self.config, self.token = config.model_copy(deep=True), token
        self.process_metrics = None
        self._last_process_cpu = None
        self._last_metrics_at = None
        self._socket_counters = {}
        self._network_totals = {"rx_bytes": 0, "tx_bytes": 0}
        self.event = threading.Event()
        self.state, self.error, self.health = (
            "connecting" if config.service_mode == "external" else "starting",
            "",
            None,
        )
        self.thread = threading.Thread(target=self._run, name="CTMDbA-service", daemon=True)
        self.thread.start()

    def stop(self):
        self.event.set()
        self.disconnected()
        with self.lock:
            process = self.process
        if process and process.poll() is None:
            process.terminate()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=12)
            if self.thread.is_alive():
                raise ServiceError("服务任务仍在退出，请稍后重试")
        self.thread = None
        self.state = "stopped"
        self.health = None
        self.process_metrics = None

    @staticmethod
    def _read_process_usage(pid):
        """读取单个 Linux 进程的 CPU 时间与常驻内存，不读取容器统计。"""
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            fields = stat[stat.rfind(")") + 2 :].split()
            cpu_ticks = int(fields[11]) + int(fields[12])
            rss_pages = int(fields[21])
            return (
                cpu_ticks / os.sysconf("SC_CLK_TCK"),
                rss_pages * os.sysconf("SC_PAGE_SIZE"),
            )
        except OSError, ValueError, IndexError:
            return None

    @staticmethod
    def _process_socket_inodes(pid):
        """从目标进程的 FD 表获取 socket inode，避免依赖 ss 的进程权限信息。"""
        sockets = set()
        try:
            for descriptor in Path(f"/proc/{pid}/fd").iterdir():
                try:
                    target = os.readlink(descriptor)
                except OSError:
                    continue
                match = re.fullmatch(r"socket:\[(\d+)\]", target)
                if match:
                    sockets.add(int(match.group(1)))
        except OSError:
            pass
        return sockets

    @staticmethod
    def _read_tcp_socket_counters(pid):
        """通过 TCP_INFO 读取指定 PID 的 TCP socket 累计字节数。"""
        process_sockets = ServiceController._process_socket_inodes(pid)
        try:
            result = subprocess.run(
                ["ss", "-tnipe"],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=2,
                check=False,
            )
        except OSError, subprocess.TimeoutExpired:
            return None
        if result.returncode != 0:
            return None

        sockets = {}
        owned, inode, received, sent = False, None, None, None

        def save_socket():
            if owned and inode and received is not None and sent is not None:
                sockets[inode] = (received, sent)

        for line in result.stdout.splitlines() + [""]:
            # 空行哨兵也需提交最后一个 socket，不能遗漏末项计数。
            if not line or not line[0].isspace():
                save_socket()
                inode_match = re.search(r"\bino:(\d+)", line)
                inode = int(inode_match.group(1)) if inode_match else None
                owned = inode in process_sockets or bool(re.search(rf"\bpid={pid}\b", line))
                received = sent = None
            if owned:
                rx_match = re.search(r"\bbytes_received:(\d+)", line)
                tx_match = re.search(r"\bbytes_sent:(\d+)", line)
                if rx_match:
                    received = int(rx_match.group(1))
                if tx_match:
                    sent = int(tx_match.group(1))
        return sockets

    def _sample_process_metrics(self, process):
        self._metrics_sample_lock.acquire()
        try:
            self._collect_process_metrics(process)
        finally:
            self._metrics_sample_lock.release()

    def _collect_process_metrics(self, process):
        usage = self._read_process_usage(process.pid)
        if not usage:
            return
        cpu_seconds, memory_bytes = usage
        now = time.monotonic()
        sockets = self._read_tcp_socket_counters(process.pid)
        with self.lock:
            if self.process is not process:
                return
            elapsed = now - self._last_metrics_at if self._last_metrics_at else None
            cpu_percent = (
                max(0.0, (cpu_seconds - self._last_process_cpu) / elapsed * 100)
                if elapsed and self._last_process_cpu is not None
                else None
            )
            self._last_process_cpu = cpu_seconds
            self._last_metrics_at = now

            rx_rate = tx_rate = None
            rx_bytes = tx_bytes = None
            if sockets is not None:
                rx_delta = tx_delta = 0
                for inode, (received, sent) in sockets.items():
                    previous = self._socket_counters.get(inode, (0, 0))
                    rx_delta += max(0, received - previous[0])
                    tx_delta += max(0, sent - previous[1])
                self._socket_counters = sockets
                self._network_totals["rx_bytes"] += rx_delta
                self._network_totals["tx_bytes"] += tx_delta
                rx_bytes = self._network_totals["rx_bytes"]
                tx_bytes = self._network_totals["tx_bytes"]
                if elapsed:
                    rx_rate, tx_rate = rx_delta / elapsed, tx_delta / elapsed

            self.process_metrics = {
                "pid": process.pid,
                "memory_bytes": memory_bytes,
                "cpu_percent": cpu_percent,
                "network_rx_bytes": rx_bytes,
                "network_tx_bytes": tx_bytes,
                "network_rx_rate": rx_rate,
                "network_tx_rate": tx_rate,
            }

    def read_version(self, path=None):
        path = path or self.executable
        if not path.exists():
            return ""
        path.chmod(path.stat().st_mode | 0o100)
        result = subprocess.run([str(path), "-v"], capture_output=True, text=True, timeout=5, check=True)
        return result.stdout.strip()

    def download(self):
        system, arch = platform.system().lower(), platform.machine().lower()
        arch = {
            "x86_64": "amd64",
            "amd64": "amd64",
            "aarch64": "arm64",
            "arm64": "arm64",
        }.get(arch)
        if system not in {"linux", "darwin"} or not arch:
            raise ServiceError("不支持的操作系统或 CPU 架构")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.state, self.error = "downloading", ""
        url = f"https://github.com/wikrin/{self.name}/releases/download/v{self.version}/{self.name}-{system}-{arch}"
        # 同目录临时文件 + 原子替换，下载失败保留原二进制。
        fd, temporary = tempfile.mkstemp(dir=self.directory, prefix=".binary-")
        try:
            with os.fdopen(fd, "wb") as dest:  # noqa: SIM117 -- 文件句柄先于网络请求打开，异常时仍关闭
                with requests.get(url, proxies=settings.PROXY, timeout=(10, 8), stream=True) as response:
                    response.raise_for_status()
                    size = 0
                    for chunk in response.iter_content(65536):
                        if self.event.is_set():
                            raise ServiceError("下载已取消")
                        size += len(chunk)
                        if size > 256 * 1024 * 1024:
                            raise ServiceError("下载文件超过大小限制")
                        dest.write(chunk)
            path = Path(temporary)
            path.chmod(0o755)
            version = self.read_version(path)
            if not version or (version != "dev" and version.lstrip("v") != self.version):
                raise ServiceError("下载文件版本与目标版本不一致")
            os.replace(temporary, self.executable)
            self.installed_version = version
            self.log("info", f"二进制已安装，版本 {version}")
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _arguments(self):
        cfg = self.config
        args = [
            str(self.executable),
            "--debug",
            "--host",
            "127.0.0.1",
            "--port",
            str(cfg.port),
            "--data-dir",
            str(self.directory),
        ]
        if settings.BANGUMI_PROXY_ENABLE and settings.BANGUMI_API_DOMAIN:
            args.extend(["--bangumi-api-url", settings.BANGUMI_API_DOMAIN])
        if cfg.tvdb_api_url:
            args.extend(["--tvdb-api-url", cfg.tvdb_api_url])
        if cfg.bangumi_use_proxy:
            args.append("--bangumi-use-proxy")
        if cfg.source:
            args.extend(["--cure-source", cfg.source])
        if settings.PROXY_HOST:
            args.extend(["--proxy", settings.PROXY_HOST])
        if settings.TMDB_API_DOMAIN:
            args.extend(["--tmdb-api-url", f"https://{settings.TMDB_API_DOMAIN}"])
        return args

    def upstream_options(self):
        """只读实际启动参数中影响上游探测的选项，跳过其他选项及其值。"""
        arguments = iter(self._arguments()[1:])
        options = {"bangumi_use_proxy": False}
        for flag in arguments:
            if flag == "--bangumi-use-proxy":
                options["bangumi_use_proxy"] = True
            elif flag != "--debug":
                value = next(arguments, "")
                if flag in (
                    "--tmdb-api-url",
                    "--tvdb-api-url",
                    "--bangumi-api-url",
                    "--proxy",
                ):
                    options[flag] = value.rstrip("/")
        return options

    def _run(self):
        process = None
        try:
            if self.config.service_mode == "local":
                self.directory.mkdir(parents=True, exist_ok=True)
                self.installed_version = self.read_version()
                installed = tuple(int(part) for part in re.findall(r"\d+", self.installed_version))
                required = tuple(int(part) for part in self.version.split("."))
                if self.installed_version != "dev" and (not installed or installed < required):
                    self.download()
                if self.event.is_set():
                    return
                env = dict(os.environ, MANAGEMENT_TOKEN=self.token)
                process = subprocess.Popen(
                    self._arguments(),
                    # 二进制自行持久化；页面只读管理 API，不采集原始控制台输出。
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=env,
                )
                with self.lock:
                    self.process = process
            self._monitor(process)
        except Exception as error:  # noqa: BLE001 -- 线程边界必须清理子进程
            if not self.event.is_set():
                self.state, self.error = "error", redact(str(error))
                self.log("error", f"服务任务失败：{self.error}")
        finally:
            self.disconnected()
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            with self.lock:
                self.process = None
                self.process_metrics = None
                self.health = None
                if self.event.is_set():
                    self.state = "stopped"

    def _monitor(self, process):
        connected, checked = False, 0
        started = time.monotonic()
        sampled = 0
        while not self.event.is_set():
            if process and process.poll() is not None:
                raise ServiceError(f"进程已退出，退出码 {process.returncode}")
            if process and time.monotonic() - sampled >= 1:
                sampled = time.monotonic()
                self._sample_process_metrics(process)
            if time.monotonic() - checked >= (5 if connected else 1):
                checked = time.monotonic()
                try:
                    self.probe()
                except ServiceError as error:
                    # 旧版仅兼容映射接口，不伪造版本/管理能力。
                    try:
                        mapping = self.request("/cache/mapping/0", timeout=2)
                        if not isinstance(mapping, dict) or any(not str(key).isdigit() for key in mapping):
                            raise ServiceError("不是有效的映射响应")
                        self.health = {
                            "service": "curetmdbanime",
                            "status": "legacy",
                            "version": "未知（旧版）",
                            "capabilities": {},
                        }
                    except requests.RequestException, ValueError, ServiceError:
                        self.disconnected()
                        connected = False
                        self.state, self.error = "connecting", str(error)
                        if process and time.monotonic() - started > 30:
                            raise ServiceError("服务启动后未通过连通性检测") from error
                    else:
                        connected = True
                else:
                    connected = True
                if connected and not self.event.is_set():
                    self.ready(self.base_url, self.event)
                    self.state, self.error = "running", ""
            self.event.wait(0.25)
