"""私有 package registry 与 provider HTTP 合同测试的 loopback 替身。"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import threading
from collections.abc import Generator
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar, cast


class RegistryHandler(BaseHTTPRequestHandler):
    """本地 registry/provider 替身；响应序列由测试控制且只记录去敏请求元数据。"""

    status_sequence: ClassVar[list[int]] = [200]
    requests: ClassVar[list[dict[str, object]]] = []
    response_body: ClassVar[bytes] = b'{"id":"local-release","url":"http://127.0.0.1/release"}'
    redirect_location: ClassVar[str | None] = None
    check_redirect_location: ClassVar[str | None] = None
    require_package_index: ClassVar[bool] = False
    uploaded_files: ClassVar[dict[str, str]] = {}
    persist_successful_uploads: ClassVar[bool] = True
    persist_upload_on_statuses: ClassVar[set[int]] = set()
    disconnect_upload_response_count: ClassVar[int] = 0
    persist_upload_before_disconnect: ClassVar[bool] = False
    truncate_response_body_count: ClassVar[int] = 0
    persist_truncated_response: ClassVar[bool] = False
    disconnect_check_after_upload_count: ClassVar[int] = 0
    simple_index_json: ClassVar[bool] = False

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 固定公开方法名
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")
        json_payload: dict[str, object] | None = None
        if content_type.startswith("application/json"):
            try:
                decoded = json.loads(body)
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, dict):
                json_payload = cast(dict[str, object], decoded)
        form_fields: dict[str, str] = {}
        upload_filename: str | None = None
        upload_sha256: str | None = None
        if content_type.startswith("multipart/form-data"):
            message = BytesParser(policy=default).parsebytes(
                b"Content-Type: "
                + content_type.encode("ascii")
                + b"\r\nMIME-Version: 1.0\r\n\r\n"
                + body
            )
            for part in message.iter_parts():
                name = part.get_param("name", header="Content-Disposition")
                raw_payload = part.get_payload(decode=True)
                payload = raw_payload if isinstance(raw_payload, bytes) else b""
                filename = part.get_filename()
                if name == "content" and filename is not None:
                    upload_filename = filename
                    upload_sha256 = hashlib.sha256(payload).hexdigest()
                elif isinstance(name, str):
                    form_fields[name] = payload.decode("utf-8", errors="replace")
        authorization = self.headers.get("Authorization", "")
        auth_scheme = authorization.partition(" ")[0]
        basic_username: str | None = None
        if auth_scheme == "Basic":
            try:
                decoded_auth = base64.b64decode(authorization.partition(" ")[2]).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                decoded_auth = ""
            basic_username = decoded_auth.partition(":")[0] or None
        type(self).requests.append(
            {
                "method": "POST",
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "auth_scheme": auth_scheme,
                "basic_username": basic_username,
                "content_type": content_type,
                "checksum": form_fields.get("sha256_digest")
                or self.headers.get("X-Artifact-SHA256"),
                "body_sha256": hashlib.sha256(body).hexdigest(),
                "json_payload": json_payload,
                "form_fields": form_fields,
                "upload_filename": upload_filename,
                "upload_sha256": upload_sha256,
            }
        )
        if type(self).require_package_index and upload_filename is None:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"package index multipart upload required")
            return
        if type(self).disconnect_upload_response_count > 0:
            type(self).disconnect_upload_response_count -= 1
            if (
                type(self).persist_upload_before_disconnect
                and upload_filename is not None
                and upload_sha256 is not None
            ):
                type(self).uploaded_files[upload_filename] = upload_sha256
            # distribution body 已被服务端完整读取，此时断连代表结果未知；测试借此
            # 证明 wrapper 不会把“可能已落库”误归类为可安全重放。
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()
            return
        redirect_location = type(self).redirect_location
        if redirect_location is not None:
            self.send_response(302)
            self.send_header("Location", redirect_location)
            self.end_headers()
            return
        status = type(self).status_sequence.pop(0) if type(self).status_sequence else 200
        truncating_response = type(self).truncate_response_body_count > 0
        if (
            (
                (
                    200 <= status < 300
                    and type(self).persist_successful_uploads
                    and (not truncating_response or type(self).persist_truncated_response)
                )
                or status in type(self).persist_upload_on_statuses
            )
            and upload_filename is not None
            and upload_sha256 is not None
        ):
            type(self).uploaded_files[upload_filename] = upload_sha256
        self.send_response(status)
        if status == 202:
            self.send_header("X-Upload-State", "partial")
        self.send_header("Content-Type", "application/json")
        if type(self).truncate_response_body_count > 0:
            type(self).truncate_response_body_count -= 1
            # 先给出成功状态与更长 Content-Length，再只写有限 body 并断开，复现
            # provider/registry 已处理请求但调用方无法确认完整响应的边界。
            self.send_header("Content-Length", str(len(type(self).response_body) + 100))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(type(self).response_body)
            self.wfile.flush()
            self.close_connection = True
            return
        self.end_headers()
        self.wfile.write(type(self).response_body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 固定公开方法名
        """提供 simple index hash，并记录 redirect GET 以证明 credential 不被转发。"""

        type(self).requests.append(
            {
                "method": "GET",
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "checksum": self.headers.get("X-Artifact-SHA256"),
                "body_sha256": hashlib.sha256(b"").hexdigest(),
                "json_payload": None,
            }
        )
        if type(self).disconnect_check_after_upload_count > 0 and any(
            request["method"] == "POST" for request in type(self).requests
        ):
            type(self).disconnect_check_after_upload_count -= 1
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()
            return
        check_redirect_location = type(self).check_redirect_location
        if check_redirect_location is not None and self.path.startswith("/simple/"):
            self.send_response(302)
            self.send_header("Location", check_redirect_location)
            self.end_headers()
            return
        if self.path.startswith("/simple/"):
            if type(self).simple_index_json:
                files = [
                    {
                        "filename": name,
                        "url": f"/files/{name}",
                        "hashes": {"sha256": checksum},
                    }
                    for name, checksum in sorted(type(self).uploaded_files.items())
                ]
                payload = json.dumps(
                    {"meta": {"api-version": "1.0"}, "name": "agent-harness", "files": files}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.pypi.simple.v1+json")
                self.end_headers()
                self.wfile.write(payload)
                return
            links = "\n".join(
                f'<a href="/files/{name}#sha256={checksum}">{name}</a>'
                for name, checksum in sorted(type(self).uploaded_files.items())
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(links.encode("utf-8"))
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(type(self).response_body)

    def log_message(self, format: str, *args: object) -> None:
        """关闭标准库含请求细节的默认日志，测试只检查显式记录。"""

        del format, args


def loopback_server_fixture() -> Generator[tuple[str, type[RegistryHandler]], None, None]:
    """启动仅监听 loopback 的短命 HTTP 替身，并在测试结束可靠关闭线程。"""

    RegistryHandler.status_sequence = [200]
    RegistryHandler.requests = []
    RegistryHandler.response_body = b'{"id":"local-release","url":"http://127.0.0.1/release"}'
    RegistryHandler.redirect_location = None
    RegistryHandler.check_redirect_location = None
    RegistryHandler.require_package_index = False
    RegistryHandler.uploaded_files = {}
    RegistryHandler.persist_successful_uploads = True
    RegistryHandler.persist_upload_on_statuses = set()
    RegistryHandler.disconnect_upload_response_count = 0
    RegistryHandler.persist_upload_before_disconnect = False
    RegistryHandler.truncate_response_body_count = 0
    RegistryHandler.persist_truncated_response = False
    RegistryHandler.disconnect_check_after_upload_count = 0
    RegistryHandler.simple_index_json = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), RegistryHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", RegistryHandler
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
