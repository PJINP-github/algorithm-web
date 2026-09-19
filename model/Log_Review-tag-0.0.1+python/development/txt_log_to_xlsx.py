#!/usr/bin/env python3
"""Convert root-level .txt inference logs into same-name .xlsx workbooks.

The parser is intentionally tolerant: malformed JSON, algorithm errors, missing
ROI/intersection warnings, and non-standard text lines are recorded in the
workbook instead of stopping the export.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
except ImportError as exc:  # pragma: no cover - depends on local environment
    raise SystemExit(
        "缺少 openpyxl，无法生成 xlsx。请先安装 openpyxl，或使用带 openpyxl 的 Python 环境。"
    ) from exc


LOG_TS_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - trueno3 - DEBUG - (?P<json>\{.*\})$"
)
T3I_RE = re.compile(r"^\[T3I\]\s+(?P<object_id>\S+)\s+//\s+(?P<kind>[^@]+)@(?P<model>.+?)\s+:\s+(?P<stage>.+)$")
T3E_RE = re.compile(r"^\[T3E\]\s+Error @ (?P<body>.+)$")
POST_RE = re.compile(r"^\[POST_RET\]\s+(?P<host>\S+)\s+(?P<port>\S+)\s+\[(?P<request_id>[^\]]+)\]\s+@\s+(?P<body>.*)$")
SYNC_RE = re.compile(r"^sync-return\s+(?P<code>\S+)")
ANALYSE_RE = re.compile(
    r"^analyse_ret @ (?P<index>\d+) : "
    r"\(\((?P<x1>-?\d+), (?P<y1>-?\d+)\), \((?P<x2>-?\d+), (?P<y2>-?\d+)\)\) "
    r"(?P<conf>-?\d+(?:\.\d+)?)$"
)
WARN_RE = re.compile(r"^\[WARN\]\s+(?P<message>.+)$")


REQUEST_HEADERS = [
    "序号",
    "日志文件",
    "行号",
    "识别时间戳",
    "日志记录时间",
    "分析响应耗时(ms)",
    "objectId",
    "requestId",
    "点位",
    "任务ID",
    "相机IP",
    "图片路径",
    "请求类型",
    "imageRecogType",
    "模型类别",
    "使用模型",
    "阶段序号",
    "分类类别",
    "中文结果描述",
    "结果类型",
    "返回码",
    "置信度",
    "分析区域",
    "结果位置",
    "回调目标",
    "回调状态",
    "异常级别",
    "备注信息",
]

OVERVIEW_HEADERS = [
    "objectId",
    "结果类型",
    "中文结果描述",
    "置信度",
    "点位",
    "请求类型",
    "imageRecogType",
    "模型类别",
    "阶段序号",
    "使用模型",
]

ANALYSE_HEADERS = [
    "序号",
    "日志文件",
    "行号",
    "objectId",
    "分析索引",
    "分析区域",
    "宽度",
    "高度",
    "置信度",
    "是否整图",
    "备注信息",
]

STAGE_HEADERS = [
    "序号",
    "日志文件",
    "行号",
    "objectId",
    "模型类别",
    "使用模型",
    "阶段序号",
    "备注信息",
]

ANOMALY_HEADERS = [
    "序号",
    "日志文件",
    "行号",
    "objectId",
    "requestId",
    "异常级别",
    "异常类型",
    "异常描述",
    "关联模型/类型",
    "原始内容",
]

SUMMARY_HEADERS = ["指标", "值", "备注"]


@dataclass
class StageEvent:
    line_no: int
    object_id: str
    kind: str
    model: str
    stage: str


@dataclass
class AnalyseEvent:
    line_no: int
    object_id: str
    index: str
    x1: int
    y1: int
    x2: int
    y2: int
    conf: float

    @property
    def area_text(self) -> str:
        return f"(({self.x1}, {self.y1}), ({self.x2}, {self.y2}))"

    @property
    def width(self) -> int:
        return self.x2 - self.x1

    @property
    def height(self) -> int:
        return self.y2 - self.y1

    @property
    def is_full_image(self) -> bool:
        return (self.x1, self.y1, self.x2, self.y2) in {
            (0, 0, 1920, 1080),
            (0, 0, 2560, 1440),
        }


@dataclass
class JsonEvent:
    line_no: int
    log_time: datetime | None
    payload: dict[str, Any]


@dataclass
class PostEvent:
    line_no: int
    host: str
    port: str
    request_id: str
    body: str


@dataclass
class AnomalyEvent:
    line_no: int
    object_id: str = ""
    request_id: str = ""
    level: str = "WARN"
    kind: str = ""
    message: str = ""
    related: str = ""
    raw: str = ""


@dataclass
class ParsedLog:
    path: Path
    total_lines: int = 0
    json_events: list[JsonEvent] = field(default_factory=list)
    stages: list[StageEvent] = field(default_factory=list)
    analyses: list[AnalyseEvent] = field(default_factory=list)
    posts: list[PostEvent] = field(default_factory=list)
    sync_codes: list[str] = field(default_factory=list)
    anomalies: list[AnomalyEvent] = field(default_factory=list)
    raw_recognized_lines: int = 0


def truncate(value: Any, limit: int = 30000) -> str:
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "...[已截断]"


def parse_log_time(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S,%f")
    except ValueError:
        return None


def parse_request_time(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S,%f"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def unique_join(values: list[str]) -> str:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return " | ".join(result)


def unique_stages(stages: list[StageEvent]) -> list[StageEvent]:
    seen: set[tuple[str, str, str]] = set()
    result: list[StageEvent] = []
    for stage in stages:
        key = (stage.kind, stage.model, stage.stage)
        if key in seen:
            continue
        seen.add(key)
        result.append(stage)
    return result


def image_info(image_path: str) -> tuple[str, str, str, str]:
    task_id = ""
    camera_ip = ""
    request_id = ""
    point = ""

    task_match = re.search(r"/image/task/([^/]+)/", image_path)
    if task_match:
        task_id = task_match.group(1)

    ip_match = re.search(r"(192\.168\.3\.\d+)_", image_path)
    if ip_match:
        camera_ip = ip_match.group(1)

    request_match = re.search(r"_1_([^/]+?)\.jpg(?:$|\?)", image_path)
    if request_match:
        request_id = request_match.group(1)

    if task_id and camera_ip:
        point = f"task/{task_id} @ {camera_ip}"
    elif task_id:
        point = f"task/{task_id}"
    elif camera_ip:
        point = camera_ip

    return task_id, camera_ip, request_id, point


def safe_json_dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except TypeError:
        return str(value)


def parse_txt(path: Path) -> ParsedLog:
    parsed = ParsedLog(path=path)
    current_object_id = ""

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_no, raw_line in enumerate(handle, 1):
            parsed.total_lines = line_no
            line = raw_line.rstrip("\r\n")
            recognized = False

            if match := T3I_RE.match(line):
                current_object_id = match.group("object_id")
                parsed.stages.append(
                    StageEvent(
                        line_no=line_no,
                        object_id=current_object_id,
                        kind=match.group("kind"),
                        model=match.group("model"),
                        stage=match.group("stage").strip(),
                    )
                )
                recognized = True

            elif match := ANALYSE_RE.match(line):
                try:
                    parsed.analyses.append(
                        AnalyseEvent(
                            line_no=line_no,
                            object_id=current_object_id,
                            index=match.group("index"),
                            x1=int(match.group("x1")),
                            y1=int(match.group("y1")),
                            x2=int(match.group("x2")),
                            y2=int(match.group("y2")),
                            conf=float(match.group("conf")),
                        )
                    )
                except ValueError as exc:
                    parsed.anomalies.append(
                        AnomalyEvent(
                            line_no=line_no,
                            object_id=current_object_id,
                            level="ERROR",
                            kind="analyse_ret_parse_error",
                            message=str(exc),
                            raw=line,
                        )
                    )
                recognized = True

            elif match := LOG_TS_RE.match(line):
                log_time = parse_log_time(match.group("ts"))
                try:
                    payload = json.loads(match.group("json"))
                    object_id = str(payload.get("Request", {}).get("objectId", ""))
                    if object_id:
                        current_object_id = object_id
                    parsed.json_events.append(JsonEvent(line_no=line_no, log_time=log_time, payload=payload))
                except json.JSONDecodeError as exc:
                    parsed.anomalies.append(
                        AnomalyEvent(
                            line_no=line_no,
                            object_id=current_object_id,
                            level="ERROR",
                            kind="json_parse_error",
                            message=f"{exc.msg} @ pos {exc.pos}",
                            raw=line,
                        )
                    )
                recognized = True

            elif match := POST_RE.match(line):
                parsed.posts.append(
                    PostEvent(
                        line_no=line_no,
                        host=match.group("host"),
                        port=match.group("port"),
                        request_id=match.group("request_id"),
                        body=match.group("body"),
                    )
                )
                recognized = True

            elif match := SYNC_RE.match(line):
                parsed.sync_codes.append(match.group("code"))
                recognized = True

            elif match := WARN_RE.match(line):
                message = match.group("message")
                parsed.anomalies.append(
                    AnomalyEvent(
                        line_no=line_no,
                        object_id=current_object_id,
                        level="WARN",
                        kind="warning",
                        message=message,
                        raw=line,
                    )
                )
                recognized = True

            elif match := T3E_RE.match(line):
                body_text = match.group("body")
                object_id = current_object_id
                request_id = ""
                message = body_text
                try:
                    body = ast.literal_eval(body_text)
                    request_id = str(body.get("requestId", ""))
                    message = str(body.get("desc", body_text))
                except (ValueError, SyntaxError):
                    if req_match := re.search(r"'requestId': '([^']+)'", body_text):
                        request_id = req_match.group(1)
                parsed.anomalies.append(
                    AnomalyEvent(
                        line_no=line_no,
                        object_id=object_id,
                        request_id=request_id,
                        level="ERROR",
                        kind="algorithm_error",
                        message=message,
                        raw=line,
                    )
                )
                recognized = True

            elif "ERROR" in line:
                parsed.anomalies.append(
                    AnomalyEvent(
                        line_no=line_no,
                        object_id=current_object_id,
                        level="ERROR",
                        kind="unclassified_error",
                        message=line,
                        raw=line,
                    )
                )
                recognized = True

            if recognized:
                parsed.raw_recognized_lines += 1

    return parsed


def build_rows(
    parsed: ParsedLog,
) -> tuple[list[list[Any]], list[list[Any]], list[list[Any]], list[list[Any]], list[list[Any]], list[list[Any]]]:
    stages_by_object: dict[str, list[StageEvent]] = defaultdict(list)
    analyses_by_object: dict[str, list[AnalyseEvent]] = defaultdict(list)
    posts_by_request: dict[str, PostEvent] = {}

    for stage in parsed.stages:
        stages_by_object[stage.object_id].append(stage)
    for analyse in parsed.analyses:
        analyses_by_object[analyse.object_id].append(analyse)
    for post in parsed.posts:
        posts_by_request[post.request_id] = post

    request_rows: list[list[Any]] = []
    overview_rows: list[list[Any]] = []
    anomaly_rows: list[list[Any]] = []

    for index, event in enumerate(parsed.json_events, 1):
        payload = event.payload
        request = payload.get("Request", {}) or {}
        response = payload.get("Response", {}) or {}
        object_id = str(request.get("objectId", response.get("objectId", "")))
        type_list = [str(item) for item in request.get("typeList", [])]
        image_recog_type = str(request.get("imageRecogType", ""))
        images = [str(item) for item in request.get("imageUrlList", [])]
        image_path = images[0] if images else ""
        task_id, camera_ip, request_id, point = image_info(image_path)
        request_time = parse_request_time(request.get("timestamp"))
        elapsed_ms = ""
        if request_time and event.log_time:
            elapsed_ms = round((event.log_time - request_time).total_seconds() * 1000, 3)

        stage_events = stages_by_object.get(object_id, [])
        model_kinds = unique_join([stage.kind for stage in stage_events] + type_list + [image_recog_type])
        models = unique_join([stage.model for stage in stage_events])
        stage_numbers = unique_join([stage.stage for stage in stage_events])

        analyse_events = analyses_by_object.get(object_id, [])
        analyse_text = unique_join([item.area_text for item in analyse_events])
        post = posts_by_request.get(request_id)
        callback_target = f"{post.host}:{post.port}" if post else ""
        callback_status = post.body if post else ""

        results = response.get("results", [])
        if not isinstance(results, list):
            results = []
            anomaly_rows.append(
                make_anomaly_row(
                    len(anomaly_rows) + 1,
                    parsed.path.name,
                    event.line_no,
                    object_id,
                    request_id,
                    "ERROR",
                    "response_results_not_list",
                    "Response.results 不是列表",
                    image_recog_type,
                    safe_json_dumps(response),
                )
            )

        if not results:
            results = [{}]
            anomaly_rows.append(
                make_anomaly_row(
                    len(anomaly_rows) + 1,
                    parsed.path.name,
                    event.line_no,
                    object_id,
                    request_id,
                    "WARN",
                    "empty_results",
                    "Response.results 为空",
                    image_recog_type,
                    safe_json_dumps(response),
                )
            )

        for result in results:
            if not isinstance(result, dict):
                result = {"raw": result}

            code = str(result.get("code", ""))
            desc = str(result.get("desc", ""))
            value = str(result.get("value", ""))
            conf = result.get("conf", "")
            result_type = str(result.get("type", ""))
            result_pos = safe_json_dumps(result.get("pos", ""))
            notes: list[str] = []
            anomaly_level = ""

            if code and code != "2000":
                anomaly_level = "WARN"
                notes.append(f"返回码非2000: {code}")
                anomaly_rows.append(
                    make_anomaly_row(
                        len(anomaly_rows) + 1,
                        parsed.path.name,
                        event.line_no,
                        object_id,
                        request_id,
                        "WARN",
                        "non_2000_code",
                        f"返回码 {code}，描述：{desc}",
                        result_type or image_recog_type,
                        safe_json_dumps(result),
                    )
                )
            if "ERROR" in desc.upper():
                anomaly_level = "ERROR"
                notes.append("结果描述包含 ERROR")

            request_rows.append(
                [
                    len(request_rows) + 1,
                    parsed.path.name,
                    event.line_no,
                    request.get("timestamp", ""),
                    event.log_time.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] if event.log_time else "",
                    elapsed_ms,
                    object_id,
                    request_id,
                    point,
                    task_id,
                    camera_ip,
                    image_path,
                    ", ".join(type_list),
                    image_recog_type,
                    model_kinds,
                    models,
                    stage_numbers,
                    value,
                    desc,
                    result_type,
                    code,
                    conf,
                    analyse_text,
                    result_pos,
                    callback_target,
                    callback_status,
                    anomaly_level,
                    "；".join(notes),
                ]
            )

            overview_stages = unique_stages(stage_events)
            if overview_stages:
                for stage in overview_stages:
                    overview_rows.append(
                        [
                            object_id,
                            result_type,
                            desc,
                            conf,
                            point,
                            ", ".join(type_list),
                            image_recog_type,
                            stage.kind,
                            stage.stage,
                            stage.model,
                        ]
                    )
            else:
                overview_rows.append(
                    [
                        object_id,
                        result_type,
                        desc,
                        conf,
                        point,
                        ", ".join(type_list),
                        image_recog_type,
                        image_recog_type,
                        "",
                        "",
                    ]
                )

    analyse_rows = [
        [
            idx,
            parsed.path.name,
            item.line_no,
            item.object_id,
            item.index,
            item.area_text,
            item.width,
            item.height,
            item.conf,
            "是" if item.is_full_image else "否",
            "区域宽高异常" if item.width <= 0 or item.height <= 0 else "",
        ]
        for idx, item in enumerate(parsed.analyses, 1)
    ]

    stage_rows = [
        [
            idx,
            parsed.path.name,
            item.line_no,
            item.object_id,
            item.kind,
            item.model,
            item.stage,
            "",
        ]
        for idx, item in enumerate(parsed.stages, 1)
    ]

    for item in parsed.anomalies:
        anomaly_rows.append(
            make_anomaly_row(
                len(anomaly_rows) + 1,
                parsed.path.name,
                item.line_no,
                item.object_id,
                item.request_id,
                item.level,
                item.kind,
                item.message,
                item.related,
                item.raw,
            )
        )

    summary_rows = make_summary_rows(parsed, request_rows, anomaly_rows)
    return request_rows, overview_rows, analyse_rows, stage_rows, anomaly_rows, summary_rows


def make_anomaly_row(
    index: int,
    file_name: str,
    line_no: int,
    object_id: str,
    request_id: str,
    level: str,
    kind: str,
    message: str,
    related: str,
    raw: str,
) -> list[Any]:
    return [
        index,
        file_name,
        line_no,
        object_id,
        request_id,
        level,
        kind,
        message,
        related,
        truncate(raw, 1000),
    ]


def make_summary_rows(parsed: ParsedLog, request_rows: list[list[Any]], anomaly_rows: list[list[Any]]) -> list[list[Any]]:
    request_types = Counter(row[13] or row[12] for row in request_rows)
    result_codes = Counter(row[20] for row in request_rows)
    anomaly_levels = Counter(row[5] for row in anomaly_rows)
    callback_success = sum(1 for post in parsed.posts if post.body == '{"code":200}')

    rows: list[list[Any]] = [
        ["源文件", str(parsed.path), ""],
        ["总行数", parsed.total_lines, ""],
        ["识别到的结构化行数", parsed.raw_recognized_lines, "T3I / DEBUG JSON / analyse_ret / POST_RET / WARN / ERROR / sync-return"],
        ["请求结果行数", len(request_rows), "一条 Response.results 会展开为一行"],
        ["DEBUG JSON 数量", len(parsed.json_events), ""],
        ["算法阶段 T3I 数量", len(parsed.stages), ""],
        ["analyse_ret 数量", len(parsed.analyses), ""],
        ["POST_RET 数量", len(parsed.posts), ""],
        ["sync-return 数量", len(parsed.sync_codes), ""],
        ["POST_RET code=200 数量", callback_success, ""],
        ["异常/警告行数", len(anomaly_rows), ""],
    ]

    for key, count in sorted(request_types.items()):
        rows.append([f"请求类型：{key}", count, ""])
    for key, count in sorted(result_codes.items()):
        rows.append([f"返回码：{key}", count, ""])
    for key, count in sorted(anomaly_levels.items()):
        rows.append([f"异常级别：{key}", count, ""])
    return rows


def append_sheet(workbook: Workbook, title: str, headers: list[str], rows: list[list[Any]]) -> None:
    sheet = workbook.create_sheet(title)
    sheet.append(headers)
    for row in rows:
        sheet.append([truncate(value) for value in row])
    style_sheet(sheet)


def style_sheet(sheet: Any) -> None:
    header_fill = PatternFill("solid", fgColor="1F4E78")
    header_font = Font(color="FFFFFF", bold=True)
    thin = Side(style="thin", color="FF000000")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions

    for cell in sheet[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = border

    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            cell.border = border

    col_widths: dict[int, int] = {}
    for row in sheet.iter_rows():
        for cell in row:
            value = "" if cell.value is None else str(cell.value)
            display_len = max((len(part) for part in value.splitlines()), default=1)
            col_widths[cell.column] = max(col_widths.get(cell.column, 0), display_len)

    for column_index, max_len in col_widths.items():
        letter = get_column_letter(column_index)
        sheet.column_dimensions[letter].width = min(max(max_len + 2, 8), 40)

    for row_idx, row in enumerate(sheet.iter_rows(), start=1):
        if row_idx == 1:
            sheet.row_dimensions[row_idx].height = 20
            continue
        max_lines = 1
        for cell in row:
            value = "" if cell.value is None else str(cell.value)
            line_count = max(1, value.count("\n") + 1)
            col_width = sheet.column_dimensions[get_column_letter(cell.column)].width or 8
            wrapped_lines = max(line_count, int((len(value) / max(col_width, 1)) + 0.999))
            max_lines = max(max_lines, wrapped_lines)
        sheet.row_dimensions[row_idx].height = min(max(18, max_lines * 15), 90)


def write_workbook(parsed: ParsedLog, output_path: Path) -> None:
    request_rows, overview_rows, analyse_rows, stage_rows, anomaly_rows, summary_rows = build_rows(parsed)

    workbook = Workbook()
    default_sheet = workbook.active
    workbook.remove(default_sheet)

    append_sheet(workbook, "总览", OVERVIEW_HEADERS, overview_rows)
    append_sheet(workbook, "请求汇总", REQUEST_HEADERS, request_rows)
    append_sheet(workbook, "分析明细", ANALYSE_HEADERS, analyse_rows)
    append_sheet(workbook, "阶段模型", STAGE_HEADERS, stage_rows)
    append_sheet(workbook, "异常与警告", ANOMALY_HEADERS, anomaly_rows)
    append_sheet(workbook, "文件统计", SUMMARY_HEADERS, summary_rows)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(output_path)


def convert_root_txt_files(root: Path, overwrite: bool) -> list[Path]:
    root = root.resolve()
    txt_files = sorted(path for path in root.glob("*.txt") if path.is_file())
    outputs: list[Path] = []

    for txt_file in txt_files:
        output_path = txt_file.with_suffix(".xlsx")
        if output_path.exists() and not overwrite:
            print(f"跳过已存在文件：{output_path}")
            continue
        parsed = parse_txt(txt_file)
        write_workbook(parsed, output_path)
        outputs.append(output_path)
        print(f"已生成：{output_path}")

    if not txt_files:
        print(f"未发现根目录 txt：{root}")
    return outputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="将项目根目录下的 txt 日志转换为同名 xlsx。")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="项目根目录，默认当前目录。")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已存在的同名 xlsx。")
    args = parser.parse_args(argv)

    if not args.root.exists() or not args.root.is_dir():
        print(f"项目根目录不存在或不是目录：{args.root}", file=sys.stderr)
        return 2

    convert_root_txt_files(args.root, overwrite=args.overwrite)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
