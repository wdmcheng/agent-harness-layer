"""把 uv package-index 请求限制在冻结 endpoint 的 no-redirect loopback relay。"""

from __future__ import annotations

import http.client
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar, cast

from release_models import urlopen_no_redirect

_FORWARDED_REQUEST_HEADERS = ("Authorization", "Content-Type", "Accept", "User-Agent")


class _SimpleIndexLinkParser(HTMLParser):
    """只收集 simple-index 链接；不执行、不渲染 registry 返回的 HTML。"""

    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """记录 `<a href>`，其余标签和属性不参与确认。"""

        if tag.lower() != "a":
            return
        href = next((value for name, value in attrs if name.lower() == "href"), None)
        if href is not None:
            self.hrefs.append(href)


def _simple_index_confirms(payload: bytes, *, filename: str, checksum: str) -> bool:
    """按 PEP 503 HTML 或 PEP 691 JSON 精确确认 distribution 文件名与 SHA-256。"""

    text = payload.decode("utf-8", errors="replace")
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, dict):
        document = cast(dict[str, object], decoded)
        files = document.get("files")
        if isinstance(files, list):
            for item in cast(list[object], files):
                raw = cast(dict[str, object], item) if isinstance(item, dict) else None
                if not isinstance(raw, dict) or raw.get("filename") != filename:
                    continue
                hashes = raw.get("hashes")
                if (
                    isinstance(hashes, dict)
                    and cast(dict[str, object], hashes).get("sha256") == checksum
                ):
                    return True
    parser = _SimpleIndexLinkParser()
    parser.feed(text)
    for href in parser.hrefs:
        parsed = urllib.parse.urlsplit(href)
        linked_name = urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1])
        hashes = urllib.parse.parse_qs(parsed.fragment, keep_blank_values=True)
        if linked_name == filename and hashes.get("sha256") == [checksum]:
            return True
    return False


@dataclass
class RelayState:
    """记录每次 uv 调用可用于安全分类的状态，不保存 credential 或请求 body。"""

    upload_statuses: list[int] = field(default_factory=lambda: [])
    check_statuses: list[int] = field(default_factory=lambda: [])
    redirect_count: int = 0
    connection_error_routes: list[str] = field(default_factory=lambda: [])
    partial_count: int = 0
    upload_replay_blocked: bool = False
    blocked_replay_count: int = 0
    confirmed_check_count: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_status(self, *, route: str, status: int) -> None:
        """按 upload/check 分开记录外部响应状态，供 wrapper 决定能否重试。"""

        with self._lock:
            target = self.upload_statuses if route == "upload" else self.check_statuses
            target.append(status)

    def record_redirect(self, *, route: str) -> None:
        """记录一次被 relay 截断的 30x。"""

        with self._lock:
            self.redirect_count += 1
            if route == "upload":
                self.upload_replay_blocked = True

    def record_connection_error(self, *, route: str) -> None:
        """记录无确定响应的路由，避免把未知 upload 与无副作用 check 混为一谈。"""

        with self._lock:
            self.connection_error_routes.append(route)
            if route == "upload":
                self.upload_replay_blocked = True

    def record_partial(self) -> None:
        """记录 registry 返回的 202；该状态不得被 uv 的 2xx 规则误认成功。"""

        with self._lock:
            self.partial_count += 1
            self.upload_replay_blocked = True

    def reject_upload_replay(self) -> bool:
        """在未知 upload 后拒绝 uv 同一进程内的协议级重发。"""

        with self._lock:
            if not self.upload_replay_blocked:
                return False
            self.blocked_replay_count += 1
            return True

    def record_confirmed_check(self) -> None:
        """记录 relay 自己解析出的同名同 SHA 正向证据，不信任 uv 退出码推断。"""

        with self._lock:
            self.confirmed_check_count += 1

    def snapshot(self) -> tuple[int, int, int, int, int, int, int]:
        """返回一次 uv 调用前后的稳定计数快照。"""

        with self._lock:
            return (
                len(self.upload_statuses),
                len(self.check_statuses),
                self.redirect_count,
                len(self.connection_error_routes),
                self.partial_count,
                self.blocked_replay_count,
                self.confirmed_check_count,
            )


class _RelayHTTPServer(ThreadingHTTPServer):
    """让被中止的 uv 子进程不会因残留转发线程阻塞 relay 收口。"""

    daemon_threads = True


class RegistryRelay:
    """短命 loopback relay；只暴露固定 upload/check 路由和冻结外部目标。"""

    def __init__(
        self,
        *,
        upload_endpoint: str,
        check_endpoint: str,
        expected_filename: str,
        expected_sha256: str,
    ) -> None:
        self.upload_endpoint = upload_endpoint
        self.check_endpoint = check_endpoint
        self.expected_filename = expected_filename
        self.expected_sha256 = expected_sha256
        self.state = RelayState()
        self._server: _RelayHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def authority(self) -> str:
        """返回 uv `--allow-insecure-host` 使用的 loopback host:port。"""

        if self._server is None:
            raise RuntimeError("registry relay is not running")
        return f"127.0.0.1:{self._server.server_port}"

    @property
    def publish_url(self) -> str:
        """返回只接收 uv multipart POST 的本地地址。"""

        return f"http://{self.authority}/upload"

    @property
    def check_url(self) -> str:
        """返回只转发 uv simple-index GET 的本地地址。"""

        return f"http://{self.authority}/check"

    def __enter__(self) -> RegistryRelay:
        """绑定随机 loopback 端口并启动后台转发线程。"""

        relay = self

        class Handler(BaseHTTPRequestHandler):
            """闭包绑定当前 relay，拒绝任何未声明的方法或路径。"""

            server_version = "agent-harness-registry-relay/1"
            sys_version = ""
            protocol_version = "HTTP/1.1"
            _relay: ClassVar[RegistryRelay] = relay

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 固定名称
                """只把 `/upload` 的原始 multipart body 转给冻结 upload endpoint。"""

                if self.path != "/upload":
                    self._reject_method()
                    return
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                if self._relay.state.reject_upload_replay():
                    # uv 的 check-url 语义可能在同一进程内重新 POST；外部结果未知后
                    # 只允许 check 确认成功，不再把任何 distribution 转发到 registry。
                    self._respond(409, b"unknown upload requires manual review", "text/plain")
                    return
                self._forward(route="upload", method="POST", body=body)

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 固定名称
                """只把 uv 追加的规范化 project path 转给冻结 simple-index base。"""

                parsed = urllib.parse.urlsplit(self.path)
                segments = parsed.path.removeprefix("/check/").split("/")
                if (
                    len(segments) != 2
                    or not segments[0]
                    or segments[1]
                    or parsed.query
                    or parsed.fragment
                    or any(
                        character not in "abcdefghijklmnopqrstuvwxyz0123456789._-"
                        for character in segments[0]
                    )
                ):
                    self._reject_method()
                    return
                self._forward(
                    route="check",
                    method="GET",
                    body=None,
                    check_project=segments[0],
                )

            def _forward(
                self,
                *,
                route: str,
                method: str,
                body: bytes | None,
                check_project: str | None = None,
            ) -> None:
                """使用共享 no-redirect client 转发一次，Location 永不回传给 uv。"""

                if route == "upload":
                    endpoint = self._relay.upload_endpoint
                else:
                    if check_project is None:
                        self._reject_method()
                        return
                    endpoint = self._relay.check_endpoint.rstrip("/") + f"/{check_project}/"
                headers = {
                    name: value
                    for name in _FORWARDED_REQUEST_HEADERS
                    if (value := self.headers.get(name)) is not None
                }
                request = urllib.request.Request(
                    endpoint,
                    data=body,
                    method=method,
                    headers=headers,
                )
                endpoint_host = urllib.parse.urlsplit(endpoint).hostname
                try:
                    with urlopen_no_redirect(
                        request,
                        timeout=10,
                        # 测试替身必须直连 loopback；否则机器级代理可能把 EOF
                        # 改写成确定 HTTP 503，掩盖真正的未知上传结果。
                        bypass_proxy=endpoint_host in {"127.0.0.1", "localhost", "::1"},
                    ) as response:
                        status = int(response.status)
                        payload = response.read()
                        content_type = response.headers.get(
                            "Content-Type", "application/octet-stream"
                        )
                except urllib.error.HTTPError as exc:
                    status = int(exc.code)
                    if 300 <= status < 400:
                        self._relay.state.record_redirect(route=route)
                        self._respond(502, b"registry redirect rejected", "text/plain")
                        return
                    payload = b"registry request rejected"
                    content_type = "text/plain"
                except (
                    urllib.error.URLError,
                    TimeoutError,
                    ConnectionError,
                    OSError,
                    http.client.HTTPException,
                ):
                    # 已收到 HTTP status 也不代表响应完整；IncompleteRead 等协议
                    # 异常发生在外部已处理 POST 之后，必须按未知结果锁死重放。
                    self._relay.state.record_connection_error(route=route)
                    self._respond(503, b"registry connection unavailable", "text/plain")
                    return

                self._relay.state.record_status(route=route, status=status)
                if (
                    route == "check"
                    and 200 <= status < 300
                    and _simple_index_confirms(
                        payload,
                        filename=self._relay.expected_filename,
                        checksum=self._relay.expected_sha256,
                    )
                ):
                    self._relay.state.record_confirmed_check()
                if route == "upload" and status == 202:
                    self._relay.state.record_partial()
                    self._respond(502, b"registry upload state is uncertain", "text/plain")
                    return
                self._respond(status, payload, content_type)

            def _reject_method(self) -> None:
                """拒绝 uv 合同外路径，避免 relay 成为通用 credential 转发器。"""

                self._respond(405, b"registry relay route rejected", "text/plain")

            def _respond(self, status: int, payload: bytes, content_type: str) -> None:
                """回传必要响应；不转发 Location、Set-Cookie 或外部 server header。"""

                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except BrokenPipeError:
                    pass

            def log_message(self, format: str, *args: object) -> None:
                """关闭含本地路径或状态的默认访问日志。"""

                del format, args

        self._server = _RelayHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """停止 relay 并释放端口；不吞掉调用方异常。"""

        del exc_type, exc, traceback
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
