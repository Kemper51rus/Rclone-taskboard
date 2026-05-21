from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen


DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080
DEFAULT_API_PROXY_URL = "http://127.0.0.1:8081"
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


def _read_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _static_root() -> Path:
    configured = os.getenv("TASKBOARD_FRONTEND_STATIC_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[1] / "static"


def _api_proxy_url() -> str:
    return os.getenv("TASKBOARD_FRONTEND_API_PROXY_URL", DEFAULT_API_PROXY_URL).rstrip("/") + "/"


def _filtered_request_headers(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in handler.headers.items():
        lower = key.lower()
        if lower in HOP_BY_HOP_HEADERS or lower in {"host", "content-length", "accept-encoding"}:
            continue
        headers[key] = value
    headers["Accept-Encoding"] = "identity"
    return headers


class FrontendHandler(BaseHTTPRequestHandler):
    server_version = "RcloneTaskboardFrontend/1.0"

    def do_GET(self) -> None:
        if self.path.startswith("/api/"):
            self._proxy_api()
            return
        if self.path == "/frontend-health":
            self._send_json({
                "status": "ok",
                "component": "frontend",
                "api_proxy_url": _api_proxy_url().rstrip("/"),
            })
            return
        if self.path == "/frontend-config.js":
            self._send_config_js()
            return
        self._serve_static()

    def do_HEAD(self) -> None:
        if self.path.startswith("/api/"):
            self._proxy_api()
            return
        self._serve_static(head_only=True)

    def do_POST(self) -> None:
        self._proxy_api_or_404()

    def do_PUT(self) -> None:
        self._proxy_api_or_404()

    def do_PATCH(self) -> None:
        self._proxy_api_or_404()

    def do_DELETE(self) -> None:
        self._proxy_api_or_404()

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (self.client_address[0], self.log_date_time_string(), fmt % args))

    def _proxy_api_or_404(self) -> None:
        if not self.path.startswith("/api/"):
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        self._proxy_api()

    def _proxy_api(self) -> None:
        split = urlsplit(self.path)
        target_path = split.path.lstrip("/")
        if split.query:
            target_path = f"{target_path}?{split.query}"
        target_url = urljoin(_api_proxy_url(), target_path)
        content_length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(content_length) if content_length > 0 else None
        request = Request(
            target_url,
            data=body,
            headers=_filtered_request_headers(self),
            method=self.command,
        )
        try:
            with urlopen(request, timeout=float(os.getenv("TASKBOARD_FRONTEND_PROXY_TIMEOUT", "60"))) as response:
                payload = response.read()
                self.send_response(response.status)
                self._copy_response_headers(dict(response.headers), len(payload))
                if self.command != "HEAD":
                    self.wfile.write(payload)
        except HTTPError as exc:
            payload = exc.read()
            self.send_response(exc.code)
            self._copy_response_headers(dict(exc.headers), len(payload))
            if self.command != "HEAD":
                self.wfile.write(payload)
        except URLError as exc:
            self._send_json(
                {
                    "detail": f"backend proxy unavailable: {exc.reason}",
                    "api_proxy_url": _api_proxy_url().rstrip("/"),
                },
                status=HTTPStatus.BAD_GATEWAY,
            )

    def _copy_response_headers(self, headers: dict[str, str], content_length: int) -> None:
        for key, value in headers.items():
            lower = key.lower()
            if lower in HOP_BY_HOP_HEADERS or lower in {"content-length", "content-encoding"}:
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(content_length))
        self.end_headers()

    def _serve_static(self, head_only: bool = False) -> None:
        split = urlsplit(self.path)
        request_path = split.path
        if request_path in {"", "/"}:
            request_path = "/dashboard.html"
        elif request_path == "/favicon.svg":
            request_path = "/rclone-taskboard-logo.svg"
        relative = request_path.lstrip("/")
        root = _static_root()
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN, "Forbidden")
            return
        if not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        payload = b"" if head_only else candidate.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(candidate.stat().st_size))
        if candidate.name == "dashboard.html":
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head_only:
            self.wfile.write(payload)

    def _send_config_js(self) -> None:
        payload = (
            "window.RCLONE_TASKBOARD_FRONTEND = "
            + json.dumps(
                {
                    "apiBasePath": "/api",
                    "apiProxyUrl": _api_proxy_url().rstrip("/"),
                },
                ensure_ascii=False,
            )
            + ";\n"
        ).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, payload: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def main() -> None:
    host = os.getenv("TASKBOARD_FRONTEND_HOST", DEFAULT_HOST)
    port = _read_int("TASKBOARD_FRONTEND_PORT", DEFAULT_PORT)
    root = _static_root()
    if not (root / "dashboard.html").is_file():
        raise SystemExit(f"dashboard.html not found in frontend static root: {root}")
    server = ThreadingHTTPServer((host, port), FrontendHandler)
    print(
        f"rclone-taskboard frontend serving {root} on {host}:{port}, "
        f"proxying /api to {_api_proxy_url().rstrip('/')}",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
