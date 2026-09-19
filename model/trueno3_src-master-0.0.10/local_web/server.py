"""基于 Python 标准库的本地推理网页服务。

功能：
    提供静态网页和 JSON API。服务层不依赖 Flask、Tornado 或任何服务
    器专用包，因此在本机安装 PyTorch、Ultralytics、OpenCV 和 PyYAML
    后即可直接运行。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from local_inference import LocalInferenceEngine


class LocalWebServer(ThreadingHTTPServer):
    """持有本地推理引擎的线程化 HTTP 服务。"""

    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        engine: LocalInferenceEngine,
        static_dir: Path,
    ) -> None:
        """初始化服务并注入共享引擎和静态资源目录。"""
        super().__init__(server_address, handler_class)
        self.engine = engine
        self.static_dir = static_dir.resolve()


class RequestHandler(BaseHTTPRequestHandler):
    """处理网页资源和本地推理 JSON 请求。"""

    server: LocalWebServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format_string: str, *args: Any) -> None:
        """将 HTTP 访问日志写入引擎日志而不是直接打印到终端。"""
        self.server.engine._log(format_string % args)

    def do_GET(self) -> None:
        """处理网页、能力配置、模型信息、健康状态和日志接口。"""
        parsed = urlparse(self.path)
        try:
            if parsed.path in {"/", "/index.html"}:
                self._serve_static("index.html")
            elif parsed.path.startswith("/static/"):
                self._serve_static(parsed.path.removeprefix("/static/"))
            elif parsed.path == "/api/health":
                self._send_json(self.server.engine.health())
            elif parsed.path == "/api/config":
                self._send_json(self.server.engine.ability_catalog())
            elif parsed.path == "/api/models":
                self._send_json(
                    {
                        "models": [
                            item.to_dict()
                            for item in self.server.engine.discover_models()
                        ]
                    }
                )
            elif parsed.path == "/api/model-info":
                query = parse_qs(parsed.query)
                model_name = query.get("model", [""])[0]
                self._send_json(self.server.engine.model_info(model_name or None))
            elif parsed.path == "/api/logs":
                query = parse_qs(parsed.query)
                limit = int(query.get("limit", ["200"])[0])
                file_logs = self.server.engine.read_src_log_lines(limit)
                if file_logs:
                    logs = file_logs[-limit:]
                else:
                    logs = self.server.engine.recent_logs(limit)
                self._send_json({"logs": logs})
            else:
                self._send_error_json(HTTPStatus.NOT_FOUND, "接口不存在。")
        except Exception as exc:
            self._send_exception(exc)

    def do_POST(self) -> None:
        """处理图片推理接口。"""
        parsed = urlparse(self.path)
        if parsed.path != "/api/infer":
            self._send_error_json(HTTPStatus.NOT_FOUND, "接口不存在。")
            return
        try:
            payload = self._read_json()
            image = self._decode_image(payload.get("image"))
            settings = {
                key: payload[key]
                for key in ("conf", "iou", "imgsz")
                if payload.get(key) not in (None, "")
            }
            result = self.server.engine.infer(
                image=image,
                ability_name=str(payload.get("ability", "")),
                roi=payload.get("roi"),
                model_override=payload.get("model") or None,
                stage_models=payload.get("stage_models"),
                helpers=payload.get("helpers"),
                settings=settings,
            )
            self._send_json(result)
        except Exception as exc:
            self._send_exception(exc)

    def _read_json(self) -> dict[str, Any]:
        """读取并解析请求体中的 JSON 对象。"""
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            raise ValueError("请求体为空。")
        if content_length > 80 * 1024 * 1024:
            raise ValueError("请求图片过大，单次请求上限为 80 MB。")
        raw = self.rfile.read(content_length)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象。")
        return payload

    @staticmethod
    def _decode_image(data: Any) -> Any:
        """将 Data URL 或纯 Base64 图片解码成 OpenCV BGR 图像。"""
        if not isinstance(data, str) or not data:
            raise ValueError("image 字段不能为空。")
        encoded = data.split(",", 1)[1] if "," in data else data
        try:
            raw = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise ValueError("image 不是有效的 Base64 数据。") from exc
        try:
            import cv2
            import numpy as np
        except ImportError as exc:
            raise RuntimeError("缺少 OpenCV 或 NumPy，无法解码图片。") from exc
        image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("无法解码图片，请使用常见的 JPG、PNG 或 WEBP 格式。")
        return image

    def _serve_static(self, relative_path: str) -> None:
        """安全地发送静态网页资源。"""
        requested = (self.server.static_dir / relative_path).resolve()
        if not requested.is_relative_to(self.server.static_dir) or not requested.is_file():
            self._send_error_json(HTTPStatus.NOT_FOUND, "静态资源不存在。")
            return
        body = requested.read_bytes()
        content_type = mimetypes.guess_type(str(requested))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        """发送 UTF-8 JSON 响应。"""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: HTTPStatus, message: str) -> None:
        """发送结构化错误响应。"""
        self._send_json(
            {"ok": False, "error": message, "status": int(status)},
            status,
        )

    def _send_exception(self, exc: Exception) -> None:
        """记录异常并返回适合网页显示的错误信息。"""
        self.server.engine._log(
            f"请求失败: {type(exc).__name__}: {exc}"
        )
        self.server.engine._logger.debug(traceback.format_exc())
        status = (
            HTTPStatus.BAD_REQUEST
            if isinstance(exc, (ValueError, KeyError))
            else HTTPStatus.INTERNAL_SERVER_ERROR
        )
        self._send_error_json(status, str(exc))


def create_server(
    engine: LocalInferenceEngine,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> LocalWebServer:
    """创建本地网页服务实例。"""
    static_dir = Path(__file__).resolve().parent / "static"
    return LocalWebServer((host, port), RequestHandler, engine, static_dir)
