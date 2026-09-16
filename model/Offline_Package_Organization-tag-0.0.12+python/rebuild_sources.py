from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import uuid
import zipfile
import zlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from annotation_db import AnnotationDatabase


ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCES = ROOT / "sources"
DEFAULT_OUTPUT = ROOT / "output"
DEFAULT_CONFIG = ROOT / "config.yaml"
DEFAULT_LOG = ROOT / "log.txt"
DEFAULT_DATABASE = ROOT / "annotations.sqlite3"

TIME_TEXT_FORMAT = "%Y-%m-%d %H:%M:%S"
DATE_TEXT_FORMAT = "%Y%m%d"


@dataclass(frozen=True)
class InnerPair:
    suffix: str
    accuracy_zip: Path
    image_zip: Path


@dataclass
class ProcessResult:
    outer_zip: Path
    output_zips: list[Path]
    output_dir: Path
    copied_images: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="重构 sources 外层压缩包。")
    parser.add_argument(
        "mode",
        nargs="?",
        type=str.lower,
        choices=("1", "2", "3", "4", "5", "m", "h", "y", "n", "r"),
        help="1=m 无 SQLite 审核；2=h 含 SQLite 审核；3=y 不拆包；4=n 拆包；5=r 采集清单",
    )
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES, help="输入 sources 目录")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="输出目录")
    parser.add_argument("--package", type=Path, default=None, help="M/H 模式指定要确认的完整外层 ZIP")
    parser.add_argument("--newwrap", type=Path, default=ROOT / "newwrap", help="M/H 模式输出目录")
    parser.add_argument("--host", default="0.0.0.0", help="M/H 模式 Web 监听地址；默认开放局域网访问")
    parser.add_argument("--port", type=int, default=8000, help="M/H 模式 Web 首选端口，默认 8000；占用时自动使用后续空闲端口")
    parser.add_argument("--no-browser", action="store_true", help="M/H 模式不自动打开浏览器")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="config.yaml 路径")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG, help="日志文件路径")
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE,
        help="SQLite 标注历史数据库路径",
    )
    parser.add_argument(
        "--collection-reports",
        action="store_true",
        help="仅从最近的清单生成采集点位文本",
    )
    parser.add_argument("--task-id", default=None, help="新 taskID；默认使用当前时间 yyyyMMddHHmmss")
    parser.add_argument("--run-time", default=None, help="新执行时间，格式 yyyy-MM-dd HH:mm:ss；默认当前时间")
    parser.add_argument(
        "--split-limit",
        type=int,
        default=None,
        help="覆盖 config.yaml 中的 split_limit",
    )
    parser.add_argument(
        "--no-split",
        action="store_true",
        help="不按 algoType 和 split_limit 拆包，直接输出单个整合压缩包",
    )
    parser.add_argument(
        "--interaction-mode",
        choices=("local", "lan"),
        default=None,
        help="M/H 模式交互方式；未指定时会提示选择 1/2",
    )
    return parser.parse_args()


def setup_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def portal_automated() -> bool:
    return (
        os.environ.get("RUST_PORTAL_AUTOMATED") == "1"
        or os.environ.get("RUST_PORTAL_HEADLESS") == "1"
    )


def parse_run_time(value: str | None) -> datetime:
    if value:
        try:
            return datetime.strptime(value, TIME_TEXT_FORMAT)
        except ValueError as exc:
            raise ValueError(f"--run-time 格式错误，应为 yyyy-MM-dd HH:mm:ss: {value}") from exc
    return datetime.now().replace(microsecond=0)


def normalize_mode(mode: str | None) -> str:
    aliases = {
        "1": "m",
        "2": "h",
        "3": "y",
        "4": "n",
        "5": "r",
        "m": "m",
        "h": "h",
        "y": "y",
        "n": "n",
        "r": "r",
    }
    return aliases.get((mode or "").lower(), "n")


def natural_key(value: str) -> list[Any]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", value)]


def collection_point_names(workbook_path: Path) -> list[str]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise RuntimeError("读取清单 xlsx 需要 openpyxl") from exc

    point_names: list[str] = []
    seen: set[str] = set()
    workbook = load_workbook(workbook_path, read_only=True, data_only=True)
    try:
        for sheet in workbook.worksheets:
            point_column_index: int | None = None
            solution_column_index: int | None = None
            for row in sheet.iter_rows(values_only=True):
                values = list(row)
                headers = {
                    str(value or "").strip(): index
                    for index, value in enumerate(values)
                    if str(value or "").strip()
                }
                if "点位名称" in headers and "解决方案" in headers:
                    point_column_index = headers["点位名称"]
                    solution_column_index = headers["解决方案"]
                    continue
                if point_column_index is None or solution_column_index is None:
                    continue
                if len(values) <= max(point_column_index, solution_column_index):
                    continue
                solution = str(values[solution_column_index] or "").strip()
                if "采集" not in solution:
                    continue
                point_name = str(values[point_column_index] or "").strip()
                if point_name and point_name not in seen:
                    point_names.append(point_name)
                    seen.add(point_name)
    finally:
        workbook.close()

    logging.info(
        "读取采集点位: count=%s path=%s",
        len(point_names),
        workbook_path,
    )
    return point_names


def write_collection_reports(root: Path, limit: int = 3) -> list[Path]:
    workbooks = [
        path
        for path in root.rglob("*-清单.xlsx")
        if path.is_file()
    ]
    workbooks.sort(
        key=lambda path: (path.stat().st_mtime_ns, str(path).casefold()),
        reverse=True,
    )
    selected_workbooks = workbooks[:limit]
    if not selected_workbooks:
        logging.warning("h 模式未找到清单 xlsx: %s", root)
        print(f"h 模式未找到清单 xlsx: {root}")
        return []

    report_paths: list[Path] = []
    for workbook_path in selected_workbooks:
        report_path = workbook_path.with_name(f"{workbook_path.stem}-采集.txt")
        point_names = collection_point_names(workbook_path)
        report_path.write_text(
            "\n".join(point_names) + ("\n" if point_names else ""),
            encoding="utf-8",
        )
        report_paths.append(report_path)
        logging.info(
            "h 模式生成采集清单: %s（匹配点位=%s，来源=%s）",
            report_path,
            len(point_names),
            workbook_path,
        )
        print(f"h 模式已生成: {report_path}（{len(point_names)} 个点位）")
    return report_paths


def load_keywords(config_path: Path) -> list[str]:
    if not config_path.exists():
        logging.warning("未找到 config.yaml，recognition 关键字为空: %s", config_path)
        return []

    keywords: list[str] = []
    in_keywords = False
    for raw_line in config_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if re.match(r"^\s*keywords\s*:\s*$", line):
            in_keywords = True
            continue
        if in_keywords:
            match = re.match(r"^\s*-\s*(.+?)\s*$", line)
            if match:
                value = strip_yaml_quotes(match.group(1))
                if value:
                    keywords.append(value)
                continue
            if not raw_line.startswith((" ", "\t")):
                in_keywords = False

    logging.info("读取 config.yaml，keywords=%s", keywords)
    return keywords


def load_bool_config(config_path: Path, key: str, default: bool) -> bool:
    if not config_path.exists():
        return default

    key_pattern = re.compile(rf"^\s*{re.escape(key)}\s*:\s*(.*?)\s*$", re.IGNORECASE)
    for raw_line in config_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.split("#", 1)[0]
        match = key_pattern.match(line)
        if not match:
            continue
        value = strip_yaml_quotes(match.group(1)).lower()
        if value in ("y", "yes", "true", "1"):
            logging.info("读取 config.yaml，%s=True", key)
            return True
        if value in ("n", "no", "false", "0"):
            logging.info("读取 config.yaml，%s=False", key)
            return False
        raise ValueError(f"config.yaml 的 {key} 只能是 y/n: {value}")

    logging.info("config.yaml 未配置 %s，默认=%s", key, default)
    return default


def load_split_limit(config_path: Path) -> int:
    if not config_path.exists():
        raise FileNotFoundError(f"未找到 config.yaml，无法读取 split_limit: {config_path}")

    split_limit: int | None = None
    key_pattern = re.compile(r"^\s*split_limit\s*:\s*(.*?)\s*$", re.IGNORECASE)
    for raw_line in config_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.split("#", 1)[0]
        match = key_pattern.match(line)
        if not match:
            continue
        value = strip_yaml_quotes(match.group(1))
        try:
            split_limit = int(value)
        except ValueError as exc:
            raise ValueError(f"config.yaml 的 split_limit 必须是整数: {value}") from exc
        break

    if split_limit is None:
        raise ValueError(f"config.yaml 缺少 split_limit 配置: {config_path}")
    if split_limit <= 0:
        raise ValueError(f"config.yaml 的 split_limit 必须大于 0: {split_limit}")

    logging.info("读取 config.yaml，split_limit=%s", split_limit)
    return split_limit


def strip_yaml_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def status_value(value: Any) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return 0
    return value if value in (0, 1, 2, 3) else 0


def safe_extract(zip_path: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            raw_name = info.filename.replace("\\", "/")
            if raw_name in ("", "/"):
                continue
            pure = PurePosixPath(raw_name)
            if pure.is_absolute() or any(part == ".." for part in pure.parts):
                raise ValueError(f"ZIP 条目路径不安全: {zip_path} -> {info.filename}")

            target = (dest / Path(*pure.parts)).resolve()
            if not target.is_relative_to(dest_resolved):
                raise ValueError(f"ZIP 条目越界: {zip_path} -> {info.filename}")

            if info.is_dir() or raw_name.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with archive.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
            except (zipfile.BadZipFile, zlib.error) as exc:
                if target.exists():
                    target.unlink()
                raise ValueError(
                    f"ZIP 条目解压失败，压缩包可能已损坏: {zip_path} -> {info.filename}: {exc}"
                ) from exc


def find_outer_zips(sources_dir: Path) -> list[Path]:
    if not sources_dir.exists():
        raise FileNotFoundError(f"sources 目录不存在: {sources_dir}")
    zips = sorted((p for p in sources_dir.glob("*.zip") if p.is_file()), key=lambda p: natural_key(p.name))
    if not zips:
        raise FileNotFoundError(f"sources 目录下没有外层 ZIP: {sources_dir}")
    return zips


def nonconflicting_path(path: Path) -> Path:
    if not path.exists():
        return path
    counter = 1
    while True:
        candidate = path.with_name(f"{path.stem} -{counter}{path.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def parse_zip_selection(value: str, count: int) -> list[int]:
    text = value.strip()
    if not text:
        return list(range(count))
    if re.search(r"[\s,，]+", text):
        indexes = [int(item) for item in re.split(r"[\s,，]+", text) if item]
    elif text.isdigit() and len(text) > 1 and all(1 <= int(character) <= count for character in text):
        indexes = [int(character) for character in text]
    elif text.isdigit():
        indexes = [int(text)]
    else:
        raise ValueError(f"压缩包选择无效: {value}")

    selected: list[int] = []
    for index in indexes:
        if index < 1 or index > count:
            raise ValueError(f"压缩包序号超出范围: {index}")
        if index not in selected:
            selected.append(index)
    return [index - 1 for index in selected]


def choose_outer_zips(sources_dir: Path) -> list[Path]:
    zips = find_outer_zips(sources_dir)
    if portal_automated() or not sys.stdin.isatty():
        logging.info("门户/非交互模式自动选择 sources ZIP: %s", [path.name for path in zips])
        return zips

    print("选择 sources 压缩包:")
    for index, zip_path in enumerate(zips, 1):
        print(f"  {index}.{zip_path.name}")
    selection = input("请选择序号，多个可输入 23 / 2,3 / 2 3，回车=全部 [all] ").strip()
    indexes = parse_zip_selection(selection, len(zips))
    selected = [zips[index] for index in indexes]
    logging.info("已选择 sources ZIP: %s", [path.name for path in selected])
    return selected


def choose_config_path(config_path: Path) -> Path:
    if config_path != DEFAULT_CONFIG or portal_automated() or not sys.stdin.isatty():
        return config_path

    configs = sorted(
        [path for path in ROOT.glob("config*.yaml") if path.is_file()],
        key=lambda path: natural_key(path.name),
    )
    if not configs:
        return config_path
    if len(configs) == 1:
        logging.info("使用配置文件: %s", configs[0])
        return configs[0]

    print("选择 config 配置文件:")
    for index, path in enumerate(configs, 1):
        print(f"  {index}.{path.name}")
    selection = input("请选择配置序号，回车=1 [1] ").strip()
    index = 1 if not selection else int(selection)
    if index < 1 or index > len(configs):
        raise ValueError(f"配置序号超出范围: {index}")
    selected = configs[index - 1]
    logging.info("使用配置文件: %s", selected)
    return selected


def choose_interaction_mode(configured: str | None) -> str:
    if configured:
        return configured
    if portal_automated() or not sys.stdin.isatty():
        logging.info("m 模式门户/非交互环境默认选择本地模式")
        return "local"

    print("m 模式交互方式:")
    print("  1. 本地模式：直接打开本机已整理好的压缩包")
    print("  2. 局域网模式：通过网页上传、标注、下载")
    selection = input("请选择 1/2，回车默认 1 [1] ").strip()
    if selection in ("", "1"):
        logging.info("m 模式交互方式：本地模式")
        return "local"
    if selection == "2":
        logging.info("m 模式交互方式：局域网模式")
        return "lan"
    raise ValueError(f"m 模式交互方式无效: {selection}")


def clear_directory(path: Path) -> None:
    if path.exists():
        for child in path.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    path.mkdir(parents=True, exist_ok=True)


def prompt_clear_work_dirs(output_dir: Path, newwrap_dir: Path) -> None:
    if not sys.stdin.isatty():
        return
    answer = input("是否清空 newwrap 和 output 文件夹? [y/N] ").strip().lower()
    if answer in ("y", "yes"):
        clear_directory(newwrap_dir)
        clear_directory(output_dir)
        logging.info("已清空目录: %s / %s", newwrap_dir, output_dir)


def find_inner_pairs(extracted_outer: Path) -> list[InnerPair]:
    accuracy: dict[str, Path] = {}
    image: dict[str, Path] = {}
    pattern = re.compile(r"^(accuracy|image_result)_(.+)\.zip$", re.IGNORECASE)

    for zip_path in extracted_outer.rglob("*.zip"):
        match = pattern.match(zip_path.name)
        if not match:
            continue
        kind, suffix = match.group(1).lower(), match.group(2)
        target = accuracy if kind == "accuracy" else image
        if suffix in target:
            raise ValueError(f"重复的内层 ZIP 后缀 {suffix}: {target[suffix]} / {zip_path}")
        target[suffix] = zip_path

    missing_image = sorted(set(accuracy) - set(image), key=natural_key)
    missing_accuracy = sorted(set(image) - set(accuracy), key=natural_key)
    if missing_image or missing_accuracy:
        raise ValueError(
            "内层 ZIP 无法按数字后缀配对: "
            f"缺 image_result={missing_image}, 缺 accuracy={missing_accuracy}"
        )

    pairs = [
        InnerPair(suffix=suffix, accuracy_zip=accuracy[suffix], image_zip=image[suffix])
        for suffix in sorted(accuracy, key=natural_key)
    ]
    if not pairs:
        raise FileNotFoundError(f"未找到 accuracy_* / image_result_* 内层 ZIP: {extracted_outer}")
    return pairs


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as file:
        return json.load(file)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.write("\n")


def find_single_file(root: Path, name: str) -> Path:
    matches = [p for p in root.rglob(name) if p.is_file()]
    if not matches:
        raise FileNotFoundError(f"未找到 {name}: {root}")
    if len(matches) > 1:
        raise ValueError(f"找到多个 {name}: {matches}")
    return matches[0]


def find_image_root(extracted_image_zip: Path, image_json: Path) -> Path:
    sibling = image_json.parent / "image"
    if sibling.is_dir():
        return sibling
    matches = [p for p in extracted_image_zip.rglob("image") if p.is_dir()]
    if not matches:
        raise FileNotFoundError(f"未找到 image 目录: {extracted_image_zip}")
    if len(matches) > 1:
        raise ValueError(f"找到多个 image 目录: {matches}")
    return matches[0]


def update_metadata(value: Any, new_task_id: str, run_dt: datetime) -> Any:
    run_text = run_dt.strftime(TIME_TEXT_FORMAT)
    date_text = run_dt.strftime(DATE_TEXT_FORMAT)
    epoch_ms = int(run_dt.timestamp() * 1000)

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            for key in list(node):
                lower = key.lower()
                if key in ("projectSN", "stationSN"):
                    continue
                if lower in ("taskid", "task_id", "id"):
                    node[key] = new_task_id
                    continue
                if lower == "statisticdate":
                    node[key] = date_text
                    continue
                if lower == "latestreporttime":
                    node[key] = epoch_ms
                    continue
                if "time" in lower:
                    node[key] = epoch_ms if isinstance(node[key], (int, float)) else run_text
                    continue
                if "date" in lower and isinstance(node[key], str):
                    node[key] = date_text
                    continue
                node[key] = walk(node[key])
            return node
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    return walk(value)


def find_metadata_value(value: Any, target_key: str) -> str:
    target_key = target_key.lower()

    def walk(node: Any) -> str:
        if isinstance(node, dict):
            for key, child in node.items():
                if key.lower() == target_key and child is not None:
                    text = str(child).strip()
                    if text:
                        return text
                found = walk(child)
                if found:
                    return found
        elif isinstance(node, list):
            for child in node:
                found = walk(child)
                if found:
                    return found
        return ""

    return walk(value)


def update_task_name(value: Any, task_name: str) -> Any:
    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            for key in list(node):
                if key.lower() == "taskname":
                    node[key] = task_name
                else:
                    node[key] = walk(node[key])
            return node
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    return walk(value)


def update_yaml_text(text: str, new_task_id: str, run_dt: datetime) -> str:
    run_text = run_dt.strftime(TIME_TEXT_FORMAT)
    date_text = run_dt.strftime(DATE_TEXT_FORMAT)
    epoch_ms = str(int(run_dt.timestamp() * 1000))

    replacements = {
        "taskID": new_task_id,
        "taskId": new_task_id,
        "task_id": new_task_id,
        "id": new_task_id,
        "taskRunTime": run_text,
        "statisticDate": date_text,
        "latestReportTime": epoch_ms,
    }

    updated_lines: list[str] = []
    key_pattern = re.compile(r"^(\s*)([A-Za-z_][\w-]*)(\s*:\s*)(.*)$")
    for line in text.splitlines():
        match = key_pattern.match(line)
        if not match:
            updated_lines.append(line)
            continue
        indent, key, sep, old_value = match.groups()
        if key in replacements:
            updated_lines.append(f"{indent}{key}{sep}{quote_like_yaml(old_value, replacements[key])}")
            continue
        lower = key.lower()
        if "time" in lower:
            replacement = epoch_ms if old_value.strip().isdigit() else run_text
            updated_lines.append(f"{indent}{key}{sep}{quote_like_yaml(old_value, replacement)}")
            continue
        if "date" in lower:
            updated_lines.append(f"{indent}{key}{sep}{quote_like_yaml(old_value, date_text)}")
            continue
        updated_lines.append(line)
    return "\n".join(updated_lines) + ("\n" if text.endswith("\n") else "")


def quote_like_yaml(old_value: str, new_value: str) -> str:
    stripped = old_value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in ("'", '"'):
        quote = stripped[0]
        return f"{quote}{new_value}{quote}"
    return new_value


def match_keyword(recognition: Any, keywords: list[str]) -> int | None:
    text = "" if recognition is None else str(recognition)
    for index, keyword in enumerate(keywords):
        if keyword and keyword in text:
            return index
    return None


def sort_images_by_config(
    images: list[dict[str, Any]],
    keywords: list[str],
    empty_recognition_last: bool,
    cluster_by_recognition: bool,
) -> list[dict[str, Any]]:
    recognition_order: dict[str, int] = {}
    decorated: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    for original_order, item in enumerate(images):
        recognition_text = str(item.get("recognition") or "").strip()
        if recognition_text not in recognition_order:
            recognition_order[recognition_text] = len(recognition_order)
        keyword_index = match_keyword(item.get("recognition"), keywords)
        decorated.append(
            (
                (
                    keyword_index is not None
                    or (empty_recognition_last and not recognition_text),
                    keyword_index if keyword_index is not None else -1,
                    recognition_order[recognition_text] if cluster_by_recognition else -1,
                    original_order,
                ),
                item,
            )
        )
    decorated.sort(key=lambda entry: entry[0])
    return [item for _, item in decorated]


def sort_accuracy_by_config(
    accuracy: dict[str, Any],
    keywords: list[str],
    empty_recognition_last: bool,
    cluster_by_recognition: bool,
) -> None:
    statistics = accuracy.get("data", {}).get("statistic")
    if not isinstance(statistics, list):
        raise ValueError("accuracy.json 缺少 data.statistic 数组")
    for stat in statistics:
        if not isinstance(stat, dict):
            raise ValueError("statistic 条目不是对象")
        images = stat.get("imagesData")
        if not isinstance(images, list):
            raise ValueError(f"imagesData 不是数组: {stat.get('algoType', '')}")
        if any(not isinstance(item, dict) for item in images):
            raise ValueError(f"imagesData 条目不是对象: {stat.get('algoType', '')}")
        stat["imagesData"] = sort_images_by_config(
            images,
            keywords=keywords,
            empty_recognition_last=empty_recognition_last,
            cluster_by_recognition=cluster_by_recognition,
        )


def sanitize_stem(value: str, fallback: str, max_length: int = 120) -> str:
    text = value.strip() or fallback
    text = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", text)
    text = re.sub(r"\s+", "_", text)
    text = text.strip(" ._")
    if not text:
        text = fallback
    return text[:max_length].rstrip(" ._") or fallback


def unique_filename(stem: str, suffix: str, used: set[str]) -> str:
    suffix = suffix if suffix.startswith(".") else f".{suffix}"
    candidate = f"{stem}{suffix}"
    counter = 2
    while candidate.lower() in used:
        candidate = f"{stem}_{counter}{suffix}"
        counter += 1
    used.add(candidate.lower())
    return candidate


def uuid_filename(suffix: str, used: set[str]) -> str:
    suffix = suffix if suffix.startswith(".") else f".{suffix}"
    while True:
        candidate = f"{uuid.uuid4()}{suffix}"
        if candidate.lower() not in used:
            used.add(candidate.lower())
            return candidate


def selected_filename_for_item(item: dict[str, Any], original_name: str) -> str:
    suffix = Path(original_name).suffix.lower() or ".jpg"
    recognition = str(item.get("recognition") or "").strip()
    point_name = str(item.get("pointName") or "").strip()
    point_code = str(item.get("pointCode") or "").strip()
    fallback = Path(original_name).stem
    parts = [part for part in (recognition, point_name or point_code) if part]
    stem = sanitize_stem("_".join(parts), fallback)
    return f"{stem}{suffix}"


def copy_yaml_extras(source_root: Path, target_root: Path, new_task_id: str, run_dt: datetime) -> None:
    for path in source_root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in (".yaml", ".yml"):
            continue
        rel = path.relative_to(source_root)
        target = target_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        text = path.read_text(encoding="utf-8-sig")
        target.write_text(update_yaml_text(text, new_task_id, run_dt), encoding="utf-8", newline="\n")


def merge_pairs(
    pairs: list[InnerPair],
    work_root: Path,
    output_root: Path,
    keywords: list[str],
    empty_recognition_last: bool,
    cluster_by_recognition: bool,
    new_task_id: str,
    run_dt: datetime,
    annotation_db: AnnotationDatabase | None = None,
) -> tuple[Path, Path, int]:
    staging_root = output_root / "_work"
    merged_accuracy_root = staging_root / "accuracy"
    merged_image_root = staging_root / "image_result"
    merged_image_dir = merged_image_root / "image"
    selected_dir = output_root / f"image_{run_dt.strftime(DATE_TEXT_FORMAT)}"

    stat_by_algo: dict[str, dict[str, Any]] = {}
    stat_original_order: list[str] = []
    base_accuracy: dict[str, Any] | None = None
    base_image: dict[str, Any] | None = None
    used_image_names: set[str] = set()
    used_selected_names: set[str] = set()
    copied_selected = 0

    for pair in pairs:
        logging.info("解压内层 ZIP: %s / %s", pair.accuracy_zip.name, pair.image_zip.name)
        pair_root = work_root / f"pair_{pair.suffix}"
        accuracy_root = pair_root / "accuracy"
        image_result_root = pair_root / "image_result"
        safe_extract(pair.accuracy_zip, accuracy_root)
        safe_extract(pair.image_zip, image_result_root)

        accuracy_json_path = find_single_file(accuracy_root, "accuracy.json")
        image_json_path = find_single_file(image_result_root, "image.json")
        image_root = find_image_root(image_result_root, image_json_path)

        accuracy_json = load_json(accuracy_json_path)
        image_json = load_json(image_json_path)
        if annotation_db:
            saved = annotation_db.upsert_marked_accuracy(accuracy_json)
            if saved:
                logging.info("归并阶段保存已有标注到 SQLite: %s 条", saved)
        if base_accuracy is None:
            base_accuracy = copy.deepcopy(accuracy_json)
        if base_image is None:
            base_image = copy.deepcopy(image_json)

        copy_yaml_extras(accuracy_root, merged_accuracy_root, new_task_id, run_dt)
        copy_yaml_extras(image_result_root, merged_image_root, new_task_id, run_dt)

        statistics = accuracy_json.get("data", {}).get("statistic")
        if not isinstance(statistics, list):
            raise ValueError(f"accuracy.json 缺少 data.statistic 数组: {accuracy_json_path}")

        for stat in statistics:
            algo_type = str(stat.get("algoType") or "").strip()
            if not algo_type:
                raise ValueError(f"statistic 缺少 algoType: {accuracy_json_path}")
            if algo_type not in stat_by_algo:
                stat_copy = copy.deepcopy(stat)
                stat_copy["imagesData"] = []
                stat_by_algo[algo_type] = stat_copy
                stat_original_order.append(algo_type)

            target_stat = stat_by_algo[algo_type]
            images = stat.get("imagesData") or []
            if not isinstance(images, list):
                raise ValueError(f"imagesData 不是数组: {accuracy_json_path} -> {algo_type}")

            for item in images:
                if not isinstance(item, dict):
                    raise ValueError(f"imagesData 条目不是对象: {accuracy_json_path} -> {algo_type}")
                original_name = str(item.get("imageName") or "").strip()
                if not original_name:
                    raise ValueError(f"imagesData 条目缺少 imageName: {accuracy_json_path} -> {algo_type}")

                source_image = image_root / algo_type / original_name
                if not source_image.is_file():
                    matches = [p for p in (image_root / algo_type).glob(original_name) if p.is_file()]
                    if not matches:
                        raise FileNotFoundError(f"图片缺失: {source_image}")
                    source_image = matches[0]

                item_copy = copy.deepcopy(item)
                suffix = Path(original_name).suffix.lower() or ".jpg"
                new_name = uuid_filename(suffix, used_image_names)
                item_copy["imageName"] = new_name

                target_image = merged_image_dir / algo_type / new_name
                target_image.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_image, target_image)

                if match_keyword(item_copy.get("recognition"), keywords) is not None:
                    selected_dir.mkdir(parents=True, exist_ok=True)
                    selected_detail_name = selected_filename_for_item(item_copy, original_name)
                    selected_name = unique_filename(
                        Path(selected_detail_name).stem,
                        Path(selected_detail_name).suffix,
                        used_selected_names,
                    )
                    shutil.copy2(target_image, selected_dir / selected_name)
                    copied_selected += 1

                target_stat["imagesData"].append(item_copy)

    if base_accuracy is None or base_image is None:
        raise ValueError("没有可合并的内层 ZIP")

    for algo_type in stat_original_order:
        stat = stat_by_algo[algo_type]
        images = stat["imagesData"]
        stat["imagesData"] = sort_images_by_config(
            images,
            keywords=keywords,
            empty_recognition_last=empty_recognition_last,
            cluster_by_recognition=cluster_by_recognition,
        )
        recompute_statistic_numbers(stat)

    base_accuracy.setdefault("data", {})["statistic"] = [stat_by_algo[algo] for algo in stat_original_order]
    update_metadata(base_accuracy, new_task_id, run_dt)
    update_metadata(base_image, new_task_id, run_dt)

    write_json(merged_accuracy_root / "accuracy.json", base_accuracy)
    write_json(merged_image_root / "image.json", base_image)
    validate_merged(merged_accuracy_root / "accuracy.json", merged_image_dir)
    logging.info("归并完成，命中复制图片 %s 张", copied_selected)
    return merged_accuracy_root, merged_image_root, copied_selected


def recompute_statistic_numbers(stat: dict[str, Any]) -> None:
    images = stat.get("imagesData") or []
    total = len(images)
    correct = sum(1 for item in images if status_value(item.get("auditStatus")) == 1)
    inaccuracy = sum(1 for item in images if status_value(item.get("auditStatus")) == 2)
    blur = sum(1 for item in images if status_value(item.get("auditStatus")) == 3)
    detected = correct + inaccuracy

    stat["pointTotal"] = total
    if "correct" in stat:
        stat["correct"] = correct
    if "inaccuracy" in stat:
        stat["inaccuracy"] = inaccuracy
    if "blur" in stat:
        stat["blur"] = blur
    if "accuracyRate" in stat:
        stat["accuracyRate"] = int(correct * 100 / detected) if detected else 0
    if "detectionRate" in stat:
        stat["detectionRate"] = int(detected * 100 / total) if total else 0


def validate_merged(accuracy_json_path: Path, image_dir: Path) -> None:
    accuracy = load_json(accuracy_json_path)
    seen: set[str] = set()
    for stat in accuracy.get("data", {}).get("statistic", []):
        algo_type = str(stat.get("algoType") or "")
        images = stat.get("imagesData") or []
        disk_dir = image_dir / algo_type
        disk_names = {p.name for p in disk_dir.glob("*.jpg")} if disk_dir.is_dir() else set()
        json_names = [str(item.get("imageName") or "") for item in images]
        if len(json_names) != len(set(json_names)):
            raise ValueError(f"JSON 中存在重复 imageName: {algo_type}")
        missing = sorted(set(json_names) - disk_names)
        extra = sorted(disk_names - set(json_names))
        if missing or extra:
            raise ValueError(f"图片引用不一致: {algo_type}, missing={missing[:5]}, extra={extra[:5]}")
        for name in json_names:
            key = f"{algo_type}/{name}".lower()
            if key in seen:
                raise ValueError(f"重复图片路径: {algo_type}/{name}")
            seen.add(key)


def zip_dir_contents(source_dir: Path, target_zip: Path) -> None:
    target_zip.parent.mkdir(parents=True, exist_ok=True)
    if target_zip.exists():
        target_zip.unlink()
    with zipfile.ZipFile(target_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source_dir.rglob("*"), key=lambda p: natural_key(str(p.relative_to(source_dir)))):
            if path.is_file():
                archive.write(path, path.relative_to(source_dir).as_posix())


def build_outer_zip(inner_accuracy_zip: Path, inner_image_zip: Path, target_zip: Path) -> None:
    target_zip.parent.mkdir(parents=True, exist_ok=True)
    if target_zip.exists():
        target_zip.unlink()
    with zipfile.ZipFile(target_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(inner_accuracy_zip, inner_accuracy_zip.name)
        archive.write(inner_image_zip, inner_image_zip.name)

    with zipfile.ZipFile(target_zip) as archive:
        names = sorted(info.filename for info in archive.infolist() if not info.is_dir())
    expected = sorted([inner_accuracy_zip.name, inner_image_zip.name])
    if names != expected:
        raise ValueError(f"外层 ZIP 条目不符合预期: {names}")


def output_outer_name(original: Path, new_task_id: str) -> str:
    stem = original.stem
    match = re.match(r"^(.*?)([-_])(\d+)$", stem)
    if match:
        stem = f"{match.group(1)}{match.group(2)}{new_task_id}"
    else:
        stem = f"{stem}_{new_task_id}"
    return f"{stem}{original.suffix}"


def output_port_name(original: Path, new_task_id: str, port_index: int) -> str:
    stem = original.stem
    match = re.match(r"^(.*?)([-_])(\d+)$", stem)
    if match:
        stem = f"{match.group(1)}{match.group(2)}{new_task_id}"
    else:
        stem = f"{stem}_{new_task_id}"
    return f"{stem}-port-{port_index}{original.suffix}"


def total_accuracy_images(accuracy_json_path: Path) -> int:
    accuracy = load_json(accuracy_json_path)
    return sum(
        len(stat.get("imagesData") or [])
        for stat in accuracy.get("data", {}).get("statistic", [])
        if isinstance(stat, dict)
    )


def safe_suffix(value: str) -> str:
    return sanitize_stem(value, "unknown", max_length=80)


def chunk_items(items: list[Any], size: int) -> list[list[Any]]:
    if size <= 0:
        raise ValueError("--split-limit 必须大于 0")
    return [items[index : index + size] for index in range(0, len(items), size)]


def build_split_packages(
    outer_zip: Path,
    package_dir: Path,
    work_root: Path,
    merged_accuracy_root: Path,
    merged_image_root: Path,
    new_task_id: str,
    run_dt: datetime,
    split_limit: int,
) -> list[Path]:
    accuracy_json_path = merged_accuracy_root / "accuracy.json"
    image_json_path = merged_image_root / "image.json"
    image_root = merged_image_root / "image"
    accuracy = load_json(accuracy_json_path)
    image_json = load_json(image_json_path)
    statistics = accuracy.get("data", {}).get("statistic")
    if not isinstance(statistics, list):
        raise ValueError(f"accuracy.json 缺少 data.statistic 数组: {accuracy_json_path}")
    station_name = find_metadata_value(accuracy, "stationName") or find_metadata_value(image_json, "stationName")
    if not station_name:
        raise ValueError(f"拆包文件缺少 stationName: {accuracy_json_path}")

    output_zips: list[Path] = []
    split_index = 1
    split_root = work_root / "split"

    for stat in statistics:
        if not isinstance(stat, dict):
            raise ValueError(f"statistic 条目不是对象: {accuracy_json_path}")
        algo_type = str(stat.get("algoType") or "").strip()
        if not algo_type:
            raise ValueError(f"statistic 缺少 algoType: {accuracy_json_path}")
        images = stat.get("imagesData") or []
        if not isinstance(images, list):
            raise ValueError(f"imagesData 不是数组: {accuracy_json_path} -> {algo_type}")
        if not images:
            continue

        chunks = chunk_items(images, split_limit)
        part_count = len(chunks)
        for part_index, image_chunk in enumerate(chunks, start=1):
            algo_suffix = safe_suffix(algo_type)
            split_suffix = algo_suffix if part_count == 1 else f"{algo_suffix}-port-{part_index}"
            split_task_id = f"{new_task_id}_{split_index:03d}"
            split_index += 1

            current_accuracy = copy.deepcopy(accuracy)
            current_stat = copy.deepcopy(stat)
            current_stat["imagesData"] = copy.deepcopy(image_chunk)
            recompute_statistic_numbers(current_stat)
            current_accuracy.setdefault("data", {})["statistic"] = [current_stat]
            current_image = copy.deepcopy(image_json)
            update_metadata(current_accuracy, split_task_id, run_dt)
            update_metadata(current_image, split_task_id, run_dt)
            split_task_name = f"{station_name}_{algo_type}"
            update_task_name(current_accuracy, split_task_name)
            update_task_name(current_image, split_task_name)

            current_root = split_root / split_suffix
            current_accuracy_root = current_root / "accuracy"
            current_image_root = current_root / "image_result"
            current_image_dir = current_image_root / "image" / algo_type
            current_accuracy_root.mkdir(parents=True, exist_ok=True)
            current_image_dir.mkdir(parents=True, exist_ok=True)
            copy_yaml_extras(merged_accuracy_root, current_accuracy_root, split_task_id, run_dt)
            copy_yaml_extras(merged_image_root, current_image_root, split_task_id, run_dt)

            for item in image_chunk:
                image_name = str(item.get("imageName") or "").strip()
                if not image_name:
                    raise ValueError(f"拆包条目缺少 imageName: {algo_type}")
                source_image = image_root / algo_type / image_name
                if not source_image.is_file():
                    raise FileNotFoundError(f"拆包图片缺失: {source_image}")
                shutil.copy2(source_image, current_image_dir / image_name)

            write_json(current_accuracy_root / "accuracy.json", current_accuracy)
            write_json(current_image_root / "image.json", current_image)
            validate_merged(current_accuracy_root / "accuracy.json", current_image_root / "image")

            inner_accuracy_zip = package_dir / f"accuracy_{split_task_id}.zip"
            inner_image_zip = package_dir / f"image_result_{split_task_id}.zip"
            zip_dir_contents(current_accuracy_root, inner_accuracy_zip)
            zip_dir_contents(current_image_root, inner_image_zip)

            outer_name = (
                output_outer_name(outer_zip, f"{new_task_id}_{split_suffix}")
                if part_count == 1
                else output_port_name(outer_zip, f"{new_task_id}_{algo_suffix}", part_index)
            )
            final_zip = package_dir / outer_name
            build_outer_zip(inner_accuracy_zip, inner_image_zip, final_zip)
            output_zips.append(final_zip)
            logging.info(
                "拆包输出: algoType=%s part=%s/%s images=%s output=%s",
                algo_type,
                part_index,
                part_count,
                len(image_chunk),
                final_zip,
            )

    if not output_zips:
        raise ValueError("拆包未生成任何输出")
    return output_zips


def process_outer_zip(
    outer_zip: Path,
    output_dir: Path,
    keywords: list[str],
    empty_recognition_last: bool,
    cluster_by_recognition: bool,
    new_task_id: str,
    run_dt: datetime,
    split_limit: int | None,
    annotation_db: AnnotationDatabase | None = None,
) -> ProcessResult:
    logging.info("开始处理外层 ZIP: %s", outer_zip)
    package_dir = nonconflicting_path(output_dir / f"{outer_zip.stem}_{new_task_id}")
    package_dir.mkdir(parents=True)

    with tempfile.TemporaryDirectory(prefix="rebuild_sources_") as temp_name:
        temp_root = Path(temp_name)
        outer_extract = temp_root / "outer"
        safe_extract(outer_zip, outer_extract)
        pairs = find_inner_pairs(outer_extract)
        logging.info("识别到 %s 组内层 ZIP: %s", len(pairs), [pair.suffix for pair in pairs])

        merged_accuracy_root, merged_image_root, copied_images = merge_pairs(
            pairs=pairs,
            work_root=temp_root / "inner",
            output_root=package_dir,
            keywords=keywords,
            empty_recognition_last=empty_recognition_last,
            cluster_by_recognition=cluster_by_recognition,
            new_task_id=new_task_id,
            run_dt=run_dt,
            annotation_db=annotation_db,
        )

        image_total = total_accuracy_images(merged_accuracy_root / "accuracy.json")
        if split_limit is not None:
            logging.info("N 模式按 algoType 拆包，图片总数 %s，algoType 内 split_limit=%s", image_total, split_limit)
            final_zips = build_split_packages(
                outer_zip=outer_zip,
                package_dir=package_dir,
                work_root=temp_root,
                merged_accuracy_root=merged_accuracy_root,
                merged_image_root=merged_image_root,
                new_task_id=new_task_id,
                run_dt=run_dt,
                split_limit=split_limit,
            )
        else:
            if split_limit is None:
                logging.info("已选择不拆包，直接输出单个整合压缩包")
            inner_accuracy_zip = package_dir / f"accuracy_{new_task_id}.zip"
            inner_image_zip = package_dir / f"image_result_{new_task_id}.zip"
            zip_dir_contents(merged_accuracy_root, inner_accuracy_zip)
            zip_dir_contents(merged_image_root, inner_image_zip)

            final_zip = package_dir / output_outer_name(outer_zip, new_task_id)
            build_outer_zip(inner_accuracy_zip, inner_image_zip, final_zip)
            final_zips = [final_zip]

        staging_root = package_dir / "_work"
        if staging_root.exists():
            shutil.rmtree(staging_root)
        logging.info("完成输出 %s 个压缩包", len(final_zips))

    return ProcessResult(
        outer_zip=outer_zip,
        output_zips=final_zips,
        output_dir=package_dir,
        copied_images=copied_images,
    )


def process_selected_outer_zips(
    outer_zips: list[Path],
    output_dir: Path,
    keywords: list[str],
    empty_recognition_last: bool,
    cluster_by_recognition: bool,
    new_task_id: str,
    run_dt: datetime,
    split_limit: int | None,
    annotation_db: AnnotationDatabase | None = None,
) -> ProcessResult:
    if not outer_zips:
        raise ValueError("未选择任何 sources ZIP")
    if len(outer_zips) == 1:
        return process_outer_zip(
            outer_zip=outer_zips[0],
            output_dir=output_dir,
            keywords=keywords,
            empty_recognition_last=empty_recognition_last,
            cluster_by_recognition=cluster_by_recognition,
            new_task_id=new_task_id,
            run_dt=run_dt,
            split_limit=split_limit,
            annotation_db=annotation_db,
        )

    logging.info("开始合并处理 %s 个外层 ZIP: %s", len(outer_zips), [path.name for path in outer_zips])
    package_dir = nonconflicting_path(output_dir / f"selected_{len(outer_zips)}packages_{new_task_id}")
    package_dir.mkdir(parents=True)

    with tempfile.TemporaryDirectory(prefix="rebuild_sources_") as temp_name:
        temp_root = Path(temp_name)
        pairs: list[InnerPair] = []
        for source_index, outer_zip in enumerate(outer_zips, 1):
            outer_extract = temp_root / f"outer_{source_index}"
            safe_extract(outer_zip, outer_extract)
            for pair in find_inner_pairs(outer_extract):
                pairs.append(
                    InnerPair(
                        suffix=f"{source_index}_{pair.suffix}",
                        accuracy_zip=pair.accuracy_zip,
                        image_zip=pair.image_zip,
                    )
                )
        logging.info("多包合并识别到 %s 组内层 ZIP", len(pairs))

        merged_accuracy_root, merged_image_root, copied_images = merge_pairs(
            pairs=pairs,
            work_root=temp_root / "inner",
            output_root=package_dir,
            keywords=keywords,
            empty_recognition_last=empty_recognition_last,
            cluster_by_recognition=cluster_by_recognition,
            new_task_id=new_task_id,
            run_dt=run_dt,
            annotation_db=annotation_db,
        )

        image_total = total_accuracy_images(merged_accuracy_root / "accuracy.json")
        if split_limit is not None:
            logging.info("N 模式按 algoType 拆包，图片总数 %s，algoType 内 split_limit=%s", image_total, split_limit)
            final_zips = build_split_packages(
                outer_zip=outer_zips[0],
                package_dir=package_dir,
                work_root=temp_root,
                merged_accuracy_root=merged_accuracy_root,
                merged_image_root=merged_image_root,
                new_task_id=new_task_id,
                run_dt=run_dt,
                split_limit=split_limit,
            )
        else:
            if split_limit is None:
                logging.info("已选择不拆包，直接输出单个整合压缩包")
            inner_accuracy_zip = package_dir / f"accuracy_{new_task_id}.zip"
            inner_image_zip = package_dir / f"image_result_{new_task_id}.zip"
            zip_dir_contents(merged_accuracy_root, inner_accuracy_zip)
            zip_dir_contents(merged_image_root, inner_image_zip)

            final_zip = package_dir / f"selected_{len(outer_zips)}packages_{new_task_id}.zip"
            build_outer_zip(inner_accuracy_zip, inner_image_zip, final_zip)
            final_zips = [final_zip]

        staging_root = package_dir / "_work"
        if staging_root.exists():
            shutil.rmtree(staging_root)
        logging.info("多包合并完成输出 %s 个压缩包", len(final_zips))

    return ProcessResult(
        outer_zip=outer_zips[0],
        output_zips=final_zips,
        output_dir=package_dir,
        copied_images=copied_images,
    )


def process_all_outer_zips(
    sources_dir: Path,
    output_dir: Path,
    keywords: list[str],
    empty_recognition_last: bool,
    cluster_by_recognition: bool,
    new_task_id: str,
    run_dt: datetime,
    split_limit: int | None,
    annotation_db: AnnotationDatabase | None = None,
) -> list[ProcessResult]:
    return [
        process_outer_zip(
            outer_zip=outer_zip,
            output_dir=output_dir,
            keywords=keywords,
            empty_recognition_last=empty_recognition_last,
            cluster_by_recognition=cluster_by_recognition,
            new_task_id=new_task_id,
            run_dt=run_dt,
            split_limit=split_limit,
            annotation_db=annotation_db,
        )
        for outer_zip in find_outer_zips(sources_dir)
    ]


def main() -> int:
    args = parse_args()
    setup_logging(args.log)

    try:
        mode = normalize_mode(args.mode)
        if args.collection_reports or mode == "r":
            write_collection_reports(ROOT)
            return 0

        annotation_db = None
        database_path: Path | None = None
        if mode != "m":
            annotation_db = AnnotationDatabase(args.database)
            database_path = args.database
            logging.info("使用 SQLite 标注数据库: %s", args.database)
        else:
            logging.info("M 模式不使用 SQLite 标注数据库")
        args.config = choose_config_path(args.config)
        run_dt = parse_run_time(args.run_time)
        new_task_id = args.task_id or run_dt.strftime("%Y%m%d%H%M%S")
        keywords = load_keywords(args.config)
        empty_recognition_last = load_bool_config(args.config, "empty_recognition_last", True)
        cluster_by_recognition = load_bool_config(args.config, "cluster_by_recognition", False)
        review_mode = mode in ("m", "h")
        interaction_mode = choose_interaction_mode(args.interaction_mode) if review_mode else None
        use_sources = not review_mode or (interaction_mode == "local" and args.package is None)
        selected_outer_zips = choose_outer_zips(args.sources) if use_sources else []
        if use_sources:
            prompt_clear_work_dirs(args.output, args.newwrap)
        args.output.mkdir(parents=True, exist_ok=True)

        if review_mode:
            from interactive_mode import run_interactive_mode

            args.newwrap.mkdir(parents=True, exist_ok=True)
            package = args.package
            if interaction_mode == "local" and package is None:
                logging.info("%s 模式本地模式先执行 Y 模式，产出完整外层 ZIP", mode.upper())
                y_result = process_selected_outer_zips(
                    outer_zips=selected_outer_zips,
                    output_dir=args.output,
                    keywords=keywords,
                    empty_recognition_last=empty_recognition_last,
                    cluster_by_recognition=cluster_by_recognition,
                    new_task_id=new_task_id,
                    run_dt=run_dt,
                    split_limit=None,
                    annotation_db=annotation_db,
                )
                if len(y_result.output_zips) != 1:
                    raise ValueError(
                        f"{mode.upper()} 模式本地模式一次只能确认一个完整外层 ZIP；"
                        "请在 sources 中保留一个 ZIP，或用 --package 指定"
                    )
                package = y_result.output_zips[0]
            return run_interactive_mode(
                package=package,
                output_dir=args.output,
                newwrap_dir=args.newwrap,
                config_path=args.config,
                host=args.host,
                port=args.port,
                no_browser=args.no_browser,
                interaction_mode=interaction_mode or "local",
                database_path=database_path,
            )

        no_split = args.no_split or mode == "y"
        if no_split:
            split_limit = None
        else:
            configured_split_limit = load_split_limit(args.config)
            split_limit = args.split_limit if args.split_limit is not None else configured_split_limit
            if split_limit <= 0:
                raise ValueError("--split-limit 必须大于 0")

        result = process_selected_outer_zips(
            outer_zips=selected_outer_zips,
            output_dir=args.output,
            keywords=keywords,
            empty_recognition_last=empty_recognition_last,
            cluster_by_recognition=cluster_by_recognition,
            new_task_id=new_task_id,
            run_dt=run_dt,
            split_limit=split_limit,
            annotation_db=annotation_db,
        )
        results = [result]
        if no_split:
            from interactive_mode import write_reviewed_package_checklist

            for output_zip in result.output_zips:
                checklist_path = write_reviewed_package_checklist(
                    package=output_zip,
                    output_dir=result.output_dir,
                    config_path=args.config,
                )
                logging.info("Y 模式生成已审核清单: %s", checklist_path)

        logging.info("全部处理完成，共处理 %s 个外层输入包", len(results))
        for result in results:
            outputs = ", ".join(str(path) for path in result.output_zips)
            logging.info(
                "结果: input=%s outputs=%s selected_images=%s",
                result.outer_zip,
                outputs,
                result.copied_images,
            )
        return 0
    except Exception:
        logging.exception("处理失败")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
