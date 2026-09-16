# -*- coding: utf-8 -*-
"""独立局域网周报 Web 服务。"""
import base64
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

import weekly_report


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = weekly_report.ensure_output_dir()
SERVICE_NAME = "weekly_report_lan_web"


def configure_console():
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def log(message):
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), message), flush=True)


def load_server_config():
    try:
        cfg = weekly_report.load_config()
    except Exception:
        cfg = {}
    clipboard_cfg = cfg.get("clipboard", {}) or {}
    host = str(clipboard_cfg.get("lan_host", "0.0.0.0") or "0.0.0.0")
    try:
        port = int(clipboard_cfg.get("lan_port", 8765) or 8765)
    except (TypeError, ValueError):
        port = 8765
    return host, port


def pick_port(start_port):
    for candidate in range(int(start_port), int(start_port) + 20):
        if not weekly_report.is_tcp_port_open("127.0.0.1", candidate):
            return candidate
    raise RuntimeError("8765-8784 端口都已被占用，请先关闭旧服务。")


def make_session_dir(session_dir=None):
    session_id = "lan_web_%s" % datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return weekly_report.ensure_lan_session_dir(
        OUTPUT_DIR,
        session_dir=session_dir,
        session_id=session_id,
    )


def read_uploads(session_dir):
    payload = weekly_report.read_lan_uploads_metadata(session_dir)
    return {
        kind: dict(meta, exists=True) if meta else None
        for kind, meta in payload.items()
    }


def parse_multipart_file(content_type, body):
    boundary_match = re.search(
        r'boundary=(?:"([^"]+)"|([^;]+))',
        content_type,
        flags=re.I,
    )
    if not boundary_match:
        return None, None, "缺少上传边界。"
    boundary = (boundary_match.group(1) or boundary_match.group(2)).strip()
    marker = b"--" + boundary.encode("utf-8")
    for raw_part in body.split(marker):
        part = raw_part
        if not part or part in (b"--", b"--\r\n"):
            continue
        if part.startswith(b"\r\n"):
            part = part[2:]
        if part.endswith(b"\r\n"):
            part = part[:-2]
        if part.endswith(b"--"):
            part = part[:-2]
            if part.endswith(b"\r\n"):
                part = part[:-2]
        header_bytes, sep, payload = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        headers = header_bytes.decode("utf-8", errors="ignore")
        disposition = ""
        for line in headers.splitlines():
            if line.lower().startswith("content-disposition:"):
                disposition = line
                break
        if 'name="file"' not in disposition:
            continue
        filename_match = re.search(r'filename="([^"]*)"', disposition)
        filename = filename_match.group(1) if filename_match else ""
        if not filename:
            return None, None, "没有收到文件。"
        return filename, payload, None
    return None, None, "没有收到文件。"


def upload_kind_from_path(path):
    request_path = urlparse(path).path
    if request_path == "/api/temp-old":
        return "old"
    if request_path == "/api/temp-result":
        return "result"
    return None


def image_to_data_uri(image_path):
    ext = os.path.splitext(image_path)[1].lower()
    mime = "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"
    with open(image_path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("ascii")
    return "data:%s;base64,%s" % (mime, encoded)


def report_markdown_to_html(report, base_dir):
    body_parts = []
    image_pattern = re.compile(r"^!\[(?P<alt>.*?)\]\((?P<path>.*?)\)$")
    for line in report.splitlines():
        stripped = line.strip()
        image_match = image_pattern.match(stripped)
        if image_match:
            image_path = image_match.group("path").strip()
            if not os.path.isabs(image_path):
                image_path = os.path.join(base_dir, image_path)
            if os.path.exists(image_path):
                alt = html.escape(image_match.group("alt") or "表格图片")
                body_parts.append(
                    '<img class="report-image" alt="%s" src="%s">' %
                    (alt, image_to_data_uri(image_path))
                )
            continue
        if not stripped:
            body_parts.append("<br>")
            continue
        if stripped.startswith("#"):
            level = min(3, len(stripped) - len(stripped.lstrip("#")))
            text = stripped[level:].strip() or stripped
            body_parts.append(
                "<h%d>%s</h%d>" % (level, html.escape(text), level)
            )
        else:
            body_parts.append("<p>%s</p>" % html.escape(line))
    return "\n".join(body_parts)


def tail_text(text, limit=8000):
    text = text or ""
    if len(text) <= limit:
        return text
    return text[-limit:]


def find_saved_report_path(process_output):
    matches = re.findall(r"\[已保存\]\s*(.+)", process_output or "")
    for candidate in reversed(matches):
        path = candidate.strip().strip('"')
        if os.path.isfile(path):
            return path
    return None


def find_latest_report_path(start_time):
    candidates = []
    for entry in os.scandir(OUTPUT_DIR):
        if not entry.is_file():
            continue
        name = entry.name
        if not name.startswith(weekly_report.GENERATED_FILE_PREFIX):
            continue
        if not name.lower().endswith(".md"):
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue
        if stat.st_mtime >= start_time - 2:
            candidates.append((stat.st_mtime, entry.path))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def generate_weekly(site, accuracy_source, session_dir):
    started = time.time()
    args = [
        sys.executable,
        os.path.join(BASE_DIR, "weekly_report.py"),
        site,
        "text",
        accuracy_source,
        "--local-clipboard",
        "--lan-upload-dir",
        session_dir,
        "--no-clipboard",
    ]
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    completed = subprocess.run(
        args,
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    process_output = "\n".join(
        part for part in (completed.stdout, completed.stderr) if part
    )
    if completed.returncode != 0:
        raise RuntimeError(tail_text(process_output, 4000) or "周报生成失败。")

    saved_path = find_saved_report_path(process_output) or find_latest_report_path(started)
    if saved_path and os.path.isfile(saved_path):
        with open(saved_path, "r", encoding="utf-8") as f:
            report_text = f.read()
        report_base_dir = os.path.dirname(saved_path)
    else:
        report_text = process_output
        report_base_dir = OUTPUT_DIR

    try:
        plain_text = weekly_report.report_to_plain_clipboard_text(report_text)
    except Exception:
        plain_text = report_text

    return {
        "site": site,
        "accuracySource": accuracy_source,
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "savedPath": saved_path or "",
        "text": report_text,
        "plainText": plain_text,
        "html": report_markdown_to_html(report_text, report_base_dir),
        "log": tail_text(process_output),
    }


class AppState:
    def __init__(self, session_dir):
        self.session_dir = session_dir
        self.lock = threading.Lock()
        self.running = False
        self.report = None
        self.last_error = ""
        self.last_log = ""

    def status_payload(self):
        with self.lock:
            report = self.report
            return {
                "ok": True,
                "service": SERVICE_NAME,
                "sessionId": weekly_report.get_session_id_from_dir(self.session_dir),
                "sessionDir": self.session_dir,
                "running": self.running,
                "lastError": self.last_error,
                "lastLog": self.last_log,
                "uploads": read_uploads(self.session_dir),
                "report": {
                    "exists": bool(report),
                    "site": report.get("site", "") if report else "",
                    "generatedAt": report.get("generatedAt", "") if report else "",
                    "savedPath": report.get("savedPath", "") if report else "",
                },
            }

    def report_payload(self):
        with self.lock:
            report = dict(self.report or {})
        if not report:
            return {"exists": False}
        report["exists"] = True
        return report


def build_index_html(public_url, session_dir):
    public_url = html.escape(public_url)
    session_dir = html.escape(session_dir)
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>局域网周报服务</title>
  <style>
    :root { color-scheme: light; --line:#d8dee8; --ink:#172033; --muted:#687386; --blue:#1f6feb; --green:#16784d; --red:#b42318; --bg:#f4f6f9; }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: "Microsoft YaHei", Arial, sans-serif; color: var(--ink); background: var(--bg); }
    header { padding: 18px 22px; background: #ffffff; border-bottom: 1px solid var(--line); }
    h1 { margin: 0 0 8px; font-size: 22px; font-weight: 700; }
    .url { margin: 0; color: var(--muted); font-size: 14px; word-break: break-all; }
    main { max-width: 1120px; margin: 0 auto; padding: 18px; }
    section { background: #fff; border: 1px solid var(--line); border-radius: 6px; padding: 16px; margin-bottom: 14px; }
    h2 { margin: 0 0 12px; font-size: 17px; }
    .grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
    label { display: block; font-size: 13px; color: var(--muted); margin-bottom: 6px; }
    input[type="text"], select { width: 100%; height: 38px; padding: 0 10px; border: 1px solid var(--line); border-radius: 4px; font: inherit; background: #fff; }
    input[type="file"] { width: 100%; min-height: 38px; }
    button { min-height: 36px; padding: 0 13px; border: 1px solid #1b2435; border-radius: 4px; background: #1b2435; color: #fff; font: inherit; cursor: pointer; }
    button.secondary { background: #fff; color: #1b2435; border-color: var(--line); }
    button.danger { background: #fff; color: var(--red); border-color: #f0b9b4; }
    button:disabled { opacity: .52; cursor: not-allowed; }
    .row { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
    .upload-box { display: grid; grid-template-columns: minmax(0, 1fr); gap: 8px; border: 1px solid var(--line); border-radius: 6px; padding: 12px; }
    .state { min-height: 22px; color: var(--muted); font-size: 13px; word-break: break-all; }
    .status { min-height: 24px; color: var(--green); font-size: 14px; }
    .status.error { color: var(--red); white-space: pre-wrap; }
    .actions { display: flex; gap: 8px; flex-wrap: wrap; }
    .report-toolbar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 12px; }
    #reportMeta { color: var(--muted); font-size: 13px; }
    #reportView { min-height: 220px; padding: 14px; border: 1px solid var(--line); border-radius: 4px; background: #fbfcfe; overflow: auto; }
    #reportView.empty { display: flex; align-items: center; justify-content: center; color: var(--muted); }
    #reportView p { margin: 0 0 8px; line-height: 1.55; white-space: pre-wrap; }
    #reportView h1, #reportView h2, #reportView h3 { margin: 12px 0 8px; }
    .report-image { display: block; max-width: 100%; height: auto; margin: 10px 0 16px; border: 1px solid var(--line); }
    pre { max-height: 180px; overflow: auto; padding: 10px; background: #101828; color: #d9e3f0; border-radius: 4px; white-space: pre-wrap; }
    @media (max-width: 760px) { main { padding: 12px; } .grid { grid-template-columns: 1fr; } header { padding: 14px; } }
  </style>
</head>
<body>
  <header>
    <h1>局域网周报服务</h1>
    <p class="url">访问地址：<strong>__PUBLIC_URL__</strong></p>
    <p class="url">本次临时目录：__SESSION_DIR__</p>
  </header>
  <main>
    <section>
      <h2>临时 table</h2>
      <div class="grid">
        <div class="upload-box">
          <strong>old.xlsx</strong>
          <div id="oldState" class="state">读取中...</div>
          <input id="oldFile" type="file" accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet">
          <div class="row">
            <button id="oldUpload" type="button" onclick="uploadFile('old')">上传 old</button>
            <button id="oldDelete" class="danger" type="button" onclick="deleteFile('old')">删除 old</button>
          </div>
        </div>
        <div class="upload-box">
          <strong>result-2.xlsx</strong>
          <div id="resultState" class="state">读取中...</div>
          <input id="resultFile" type="file" accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet">
          <div class="row">
            <button id="resultUpload" type="button" onclick="uploadFile('result')">上传 result-2</button>
            <button id="resultDelete" class="danger" type="button" onclick="deleteFile('result')">删除 result-2</button>
          </div>
        </div>
      </div>
    </section>
    <section>
      <h2>生成周报</h2>
      <div class="grid">
        <div>
          <label for="site">站点关键字</label>
          <input id="site" type="text" placeholder="例如：滨州">
        </div>
        <div>
          <label for="accuracy">准确率数据</label>
          <select id="accuracy">
            <option value="raw">准确率</option>
            <option value="audited">审核后准确率</option>
          </select>
        </div>
      </div>
      <div class="actions" style="margin-top:12px;">
        <button id="generateButton" type="button" onclick="generateReport()">生成周报</button>
        <button class="secondary" type="button" onclick="refreshAll()">刷新页面状态</button>
      </div>
      <div id="status" class="status"></div>
    </section>
    <section>
      <div class="report-toolbar">
        <h2 style="margin:0;">周报内容</h2>
        <button class="secondary" type="button" onclick="copyReport()">复制周报</button>
        <span id="reportMeta"></span>
      </div>
      <div id="reportView" class="empty">暂无周报。上传临时表后可直接生成；不上传时使用本机 0Work 中的数据。</div>
    </section>
    <section>
      <h2>运行日志</h2>
      <pre id="logView">等待操作。</pre>
    </section>
  </main>
  <script>
    const defs = {
      old: { endpoint: "/api/temp-old", label: "old.xlsx", fileId: "oldFile", stateId: "oldState", uploadId: "oldUpload", deleteId: "oldDelete" },
      result: { endpoint: "/api/temp-result", label: "result-2.xlsx", fileId: "resultFile", stateId: "resultState", uploadId: "resultUpload", deleteId: "resultDelete" }
    };
    let uploads = { old: null, result: null };
    let reportText = "";
    let reportHtml = "";

    function setStatus(text, isError) {
      const el = document.getElementById("status");
      el.textContent = text || "";
      el.className = isError ? "status error" : "status";
    }
    function setLog(text) {
      document.getElementById("logView").textContent = text || "等待操作。";
    }
    function formatSize(size) {
      if (!size) return "";
      if (size < 1024) return size + " B";
      if (size < 1024 * 1024) return (size / 1024).toFixed(1) + " KB";
      return (size / 1024 / 1024).toFixed(2) + " MB";
    }
    function renderUpload(kind) {
      const def = defs[kind];
      const meta = uploads[kind];
      const state = document.getElementById(def.stateId);
      const file = document.getElementById(def.fileId);
      const upload = document.getElementById(def.uploadId);
      const del = document.getElementById(def.deleteId);
      if (meta) {
        const parts = [meta.originalName || def.label, formatSize(meta.size), meta.mtime].filter(Boolean);
        state.textContent = "已暂存：" + parts.join(" / ");
        file.disabled = true;
        upload.disabled = true;
        del.disabled = false;
      } else {
        state.textContent = "未暂存。";
        file.disabled = false;
        upload.disabled = false;
        del.disabled = true;
      }
    }
    function renderReport(report) {
      const view = document.getElementById("reportView");
      const meta = document.getElementById("reportMeta");
      if (!report || !report.exists) {
        reportText = "";
        reportHtml = "";
        view.className = "empty";
        view.textContent = "暂无周报。上传临时表后可直接生成；不上传时使用本机 0Work 中的数据。";
        meta.textContent = "";
        return;
      }
      reportText = report.plainText || report.text || "";
      reportHtml = report.html || "";
      view.className = "";
      view.innerHTML = reportHtml || "<p></p>";
      meta.textContent = [report.site, report.generatedAt].filter(Boolean).join(" / ");
    }
    async function refreshAll(loadFullReport) {
      try {
        const data = await fetch("/api/status", { cache: "no-store" }).then(r => r.json());
        uploads = data.uploads || { old: null, result: null };
        renderUpload("old");
        renderUpload("result");
        document.getElementById("generateButton").disabled = !!data.running;
        if (data.lastError) setStatus(data.lastError, true);
        else if (data.running) setStatus("周报正在生成，请稍候。", false);
        else setStatus("服务已就绪。", false);
        setLog(data.lastLog || data.lastError || "服务已启动。");
        if (loadFullReport && data.report && data.report.exists) {
          const report = await fetch("/api/report", { cache: "no-store" }).then(r => r.json());
          renderReport(report);
        }
      } catch (e) {
        setStatus("无法连接服务：" + e, true);
      }
    }
    async function uploadFile(kind) {
      const def = defs[kind];
      if (uploads[kind]) {
        setStatus("已有暂存 " + def.label + "，请先删除后再添加。", true);
        return;
      }
      const fileEl = document.getElementById(def.fileId);
      const file = fileEl.files && fileEl.files[0];
      if (!file) {
        setStatus("请选择 " + def.label + "。", true);
        return;
      }
      const form = new FormData();
      form.append("file", file);
      setStatus("正在上传 " + def.label + "...", false);
      const response = await fetch(def.endpoint, { method: "POST", body: form });
      const data = await response.json();
      if (!response.ok || !data.ok) {
        setStatus(data.error || "上传失败。", true);
        return;
      }
      uploads[kind] = data.file;
      fileEl.value = "";
      renderUpload(kind);
      setStatus("已暂存 " + def.label + "。", false);
      setLog(data.message || "上传完成。");
    }
    async function deleteFile(kind) {
      const def = defs[kind];
      const response = await fetch(def.endpoint, { method: "DELETE" });
      const data = await response.json();
      if (!response.ok || !data.ok) {
        setStatus(data.error || "删除失败。", true);
        return;
      }
      uploads[kind] = null;
      renderUpload(kind);
      setStatus("已删除暂存 " + def.label + "。", false);
      setLog(data.message || "删除完成。");
    }
    async function generateReport() {
      const site = document.getElementById("site").value.trim();
      if (!site) {
        setStatus("请输入站点关键字。", true);
        return;
      }
      const accuracySource = document.getElementById("accuracy").value;
      const button = document.getElementById("generateButton");
      button.disabled = true;
      setStatus("正在生成周报，请稍候。", false);
      const response = await fetch("/api/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ site, accuracySource })
      });
      const data = await response.json();
      button.disabled = false;
      if (!response.ok || !data.ok) {
        setStatus(data.error || "生成失败。", true);
        setLog(data.log || data.error || "生成失败。");
        return;
      }
      renderReport(data.report);
      setStatus("周报已生成，可点击复制周报。", false);
      setLog(data.report.log || "生成完成。");
      await refreshAll(false);
    }
    async function copyReport() {
      if (!reportText) {
        setStatus("暂无可复制的周报。", true);
        return;
      }
      try {
        if (reportHtml && window.ClipboardItem) {
          const item = new ClipboardItem({
            "text/html": new Blob([reportHtml], { type: "text/html" }),
            "text/plain": new Blob([reportText], { type: "text/plain" })
          });
          await navigator.clipboard.write([item]);
          setStatus("已复制图文到当前电脑剪贴板。", false);
        } else {
          await navigator.clipboard.writeText(reportText);
          setStatus("已复制文字到当前电脑剪贴板。", false);
        }
      } catch (e) {
        const temp = document.createElement("textarea");
        temp.value = reportText;
        temp.style.position = "fixed";
        temp.style.left = "-9999px";
        document.body.appendChild(temp);
        temp.focus();
        temp.select();
        const ok = document.execCommand("copy");
        document.body.removeChild(temp);
        setStatus(ok ? "已复制到当前电脑剪贴板。" : "浏览器未允许复制，请选中页面内容手动复制。", !ok);
      }
    }
    refreshAll(true);
  </script>
</body>
</html>""".replace("__PUBLIC_URL__", public_url).replace("__SESSION_DIR__", session_dir)


class LanWeeklyHandler(BaseHTTPRequestHandler):
    server_version = "WeeklyLanWeb/1.0"

    def handle(self):
        try:
            super().handle()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            return

    def log_message(self, format, *args):
        return

    @property
    def state(self):
        return self.server.app_state

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html_text):
        body = html_text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self.send_html(build_index_html(self.server.public_url, self.state.session_dir))
            return
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if path == "/api/status":
            self.send_json(200, self.state.status_payload())
            return
        if path == "/api/report":
            self.send_json(200, self.state.report_payload())
            return
        upload_kind = upload_kind_from_path(path)
        if upload_kind:
            meta = weekly_report.read_lan_upload_metadata(
                self.state.session_dir,
                upload_kind,
            )
            self.send_json(200, dict(meta, exists=True) if meta else {"exists": False})
            return
        self.send_error(404)

    def do_DELETE(self):
        upload_kind = upload_kind_from_path(self.path)
        if not upload_kind:
            self.send_error(404)
            return
        upload_path, meta_path = weekly_report.get_lan_upload_paths(
            self.state.session_dir,
            upload_kind,
        )
        removed = False
        for path in (upload_path, meta_path):
            try:
                if os.path.exists(path):
                    os.remove(path)
                    removed = True
            except OSError as exc:
                self.send_json(500, {"ok": False, "error": "删除失败: %s" % exc})
                return
        log("删除暂存 %s" % weekly_report.LAN_UPLOAD_KINDS[upload_kind]["default_name"])
        self.send_json(200, {"ok": True, "removed": removed, "message": "删除完成。"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/generate":
            self.handle_generate()
            return
        upload_kind = upload_kind_from_path(path)
        if upload_kind:
            self.handle_upload(upload_kind)
            return
        self.send_error(404)

    def read_request_json(self):
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if content_length <= 0:
            return {}
        body = self.rfile.read(content_length)
        return json.loads(body.decode("utf-8", errors="replace") or "{}")

    def handle_generate(self):
        try:
            data = self.read_request_json()
        except Exception:
            self.send_json(400, {"ok": False, "error": "请求内容无法解析。"})
            return
        site = str(data.get("site", "")).strip()
        if not site:
            self.send_json(400, {"ok": False, "error": "请输入站点关键字。"})
            return
        try:
            accuracy_source = weekly_report.parse_accuracy_source_arg(
                data.get("accuracySource", "raw")
            ) or "raw"
        except Exception:
            self.send_json(400, {"ok": False, "error": "准确率数据选项无效。"})
            return

        with self.state.lock:
            if self.state.running:
                self.send_json(409, {"ok": False, "error": "周报正在生成，请稍候。"})
                return
            self.state.running = True
            self.state.last_error = ""
            self.state.last_log = "正在生成周报..."

        log("开始生成周报：%s" % site)
        try:
            report = generate_weekly(site, accuracy_source, self.state.session_dir)
        except Exception as exc:
            message = str(exc)
            with self.state.lock:
                self.state.running = False
                self.state.last_error = message
                self.state.last_log = message
            log("生成失败：%s" % message.splitlines()[-1])
            self.send_json(500, {"ok": False, "error": message, "log": message})
            return

        with self.state.lock:
            self.state.running = False
            self.state.report = report
            self.state.last_error = ""
            self.state.last_log = report.get("log", "")
        log("周报已生成：%s" % (report.get("savedPath") or site))
        self.send_json(200, {"ok": True, "report": dict(report, exists=True)})

    def handle_upload(self, upload_kind):
        upload_cfg = weekly_report.LAN_UPLOAD_KINDS[upload_kind]
        upload_path, meta_path = weekly_report.get_lan_upload_paths(
            self.state.session_dir,
            upload_kind,
        )
        if os.path.exists(upload_path):
            self.send_json(409, {
                "ok": False,
                "error": "已有暂存 %s，请先删除后再添加。" % upload_cfg["default_name"],
            })
            return
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self.send_json(400, {"ok": False, "error": "请选择 .xlsx 文件上传。"})
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if content_length <= 0:
            self.send_json(400, {"ok": False, "error": "没有收到文件。"})
            return

        body = self.rfile.read(content_length)
        filename, payload, parse_error = parse_multipart_file(content_type, body)
        if parse_error:
            self.send_json(400, {"ok": False, "error": parse_error})
            return
        original_name = os.path.basename(unquote(filename))
        if not original_name.lower().endswith(".xlsx"):
            self.send_json(400, {"ok": False, "error": "只支持上传 Microsoft Excel 工作表 (.xlsx)。"})
            return
        if not payload:
            self.send_json(400, {"ok": False, "error": "上传文件为空。"})
            return

        fd, tmp_path = tempfile.mkstemp(
            prefix=upload_cfg["temp_prefix"],
            suffix=".xlsx",
            dir=self.state.session_dir,
        )
        try:
            with os.fdopen(fd, "wb") as tmp_file:
                tmp_file.write(payload)
            ok, error = weekly_report.validate_xlsx_file(tmp_path)
            if not ok:
                self.send_json(400, {
                    "ok": False,
                    "error": "上传文件不是可读取的 .xlsx: %s" % error,
                })
                return
            os.replace(tmp_path, upload_path)
            weekly_report.write_lan_upload_metadata(
                meta_path,
                original_name,
                upload_path,
                upload_kind,
            )
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass

        meta = weekly_report.read_lan_upload_metadata(
            self.state.session_dir,
            upload_kind,
        ) or {}
        meta["exists"] = True
        log("上传暂存 %s：%s" % (upload_cfg["default_name"], original_name))
        self.send_json(200, {
            "ok": True,
            "file": meta,
            "message": "上传完成：%s" % original_name,
        })


def run_server(host=None, port=None, session_dir=None):
    default_host, default_port = load_server_config()
    host = host or default_host
    port = pick_port(port or default_port)
    session_dir = make_session_dir(session_dir)
    public_url = "http://%s:%d/" % (weekly_report.get_lan_ip(), port)
    local_url = "http://127.0.0.1:%d/" % port

    server = ThreadingHTTPServer((host, int(port)), LanWeeklyHandler)
    server.app_state = AppState(session_dir)
    server.public_url = public_url

    log("局域网 Web 服务已启动")
    log("局域网地址：%s" % public_url)
    log("本机地址：%s" % local_url)
    log("本次临时目录：%s" % session_dir)
    log("关闭窗口或按 Ctrl+C 停止服务")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("正在停止服务...")
    finally:
        server.server_close()
        log("服务已停止")


def parse_args(argv):
    args = {
        "host": None,
        "port": None,
        "session_dir": None,
    }
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--host":
            if i + 1 >= len(argv):
                raise ValueError("--host 缺少参数")
            args["host"] = argv[i + 1]
            i += 2
        elif arg == "--port":
            if i + 1 >= len(argv):
                raise ValueError("--port 缺少参数")
            args["port"] = int(argv[i + 1])
            i += 2
        elif arg == "--session-dir":
            if i + 1 >= len(argv):
                raise ValueError("--session-dir 缺少参数")
            args["session_dir"] = argv[i + 1]
            i += 2
        else:
            raise ValueError("未知参数: %s" % arg)
    return args


def main(argv=None):
    configure_console()
    argv = argv or sys.argv
    try:
        args = parse_args(argv)
    except Exception as exc:
        print("错误: %s" % exc)
        return 1
    try:
        run_server(args["host"], args["port"], args["session_dir"])
    except Exception as exc:
        print("错误: %s" % exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
