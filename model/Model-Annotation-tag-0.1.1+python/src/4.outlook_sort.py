#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
脚本 4.outlook_sort.py
读取训练展望原始分组，对每个区块内的 task 行：
  1. 基于 Task ID 去重（保留首次出现的行）
  2. 按“未训练优先，数字倒序”排列
  3. 在每个区块末尾追加未训练 autoedge 列表和备注标签列表
输出到训练展望最终排序。
"""

import re
from pathlib import Path

from ask_data_utils import (
    ASK_DATA_FILE,
    MODEL_FILE,
    OUTLOOK_FILE,
    OUTLOOK_SORTED_FILE,
    excluded_task_ids,
    load_category_mapping_exclude_substrings,
    parse_ask_lines,
    remove_excluded_task_refs,
    resolve_existing_work_file,
    resolve_mapping_file,
    resolve_work_dir,
    text_exclude_reason,
)

BASE_DIR = Path(__file__).resolve().parent
WORK_DIR = resolve_work_dir(BASE_DIR)
INPUT_FILE = WORK_DIR / OUTLOOK_FILE
MODEL_DATA_FILE = WORK_DIR / MODEL_FILE
OUTPUT_FILE = WORK_DIR / OUTLOOK_SORTED_FILE
MAPPING_FILE_PATH = resolve_mapping_file(WORK_DIR)
MAPPING_EXCLUDE_SUBSTRINGS = load_category_mapping_exclude_substrings(
    MAPPING_FILE_PATH
)
ASK_SOURCE_FILE = resolve_existing_work_file(WORK_DIR, ASK_DATA_FILE, BASE_DIR)
EXCLUDED_TASK_IDS = excluded_task_ids(
    parse_ask_lines(
        ASK_SOURCE_FILE.read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines()
    )
    if ASK_SOURCE_FILE.exists()
    else [],
    MAPPING_EXCLUDE_SUBSTRINGS,
)
TASK_REF_RE = re.compile(r'\b(?:autoedge|cvat)@\d+\b', re.IGNORECASE)

def parse_task_line(line: str):
    """解析 ---Task 行，返回 (task_id, status, 原始行) 如果匹配，否则 None"""
    m = re.match(r'^(---Task\s+)(\d+)(\s+)(未训练|已训练)(\s+.+)$', line)
    if m:
        task_id = int(m.group(2))
        status = m.group(4)  # "未训练" or "已训练"
        return task_id, status, line
    return None


def extract_task_refs(text: str) -> list[str]:
    refs = []
    seen = set()
    for match in TASK_REF_RE.finditer(text or ""):
        ref = match.group(0).lower()
        if ref not in seen:
            seen.add(ref)
            refs.append(ref)
    return refs


def split_model_line(line: str):
    if "  完整Task:" in line:
        key, content = line.split("  完整Task:", 1)
    elif "完整Task:" in line:
        key, content = line.split("完整Task:", 1)
    else:
        key, content = line, line
    return key.strip(), content


def split_fill_source_key(key: str) -> tuple[str, str]:
    marker = "  补齐Task来源:"
    if marker not in key:
        return key, ""
    clean_key, source_text = key.split(marker, 1)
    return clean_key.strip(), source_text.strip()


def parse_fill_sources(source_text: str) -> list[dict]:
    sources = []
    for segment in source_text.split(";"):
        segment = segment.strip()
        if not segment or "=" not in segment:
            continue
        label, refs_text = segment.split("=", 1)
        refs = extract_task_refs(refs_text)
        if refs:
            sources.append({"label": label.strip(), "refs": refs})
    return sources


def parse_model_meta(key: str) -> tuple[str, str]:
    clean_key, _source_text = split_fill_source_key(key)
    note_part = clean_key
    start_time = ""
    for marker in ("  训练开始时间 ", "  开始时间 "):
        if marker in note_part:
            note_part, start_time = note_part.rsplit(marker, 1)
            start_time = start_time.strip()
            break
    if "  备注 " in note_part:
        _prefix, note = note_part.split("  备注 ", 1)
    else:
        note = note_part
    return note.strip(), start_time


def load_model_entries() -> dict[str, dict]:
    entries = {}
    if not MODEL_DATA_FILE.exists():
        return entries
    for line in MODEL_DATA_FILE.read_text(encoding="utf-8").splitlines():
        if not line.startswith("最新Task"):
            continue
        if text_exclude_reason(line, MAPPING_EXCLUDE_SUBSTRINGS):
            continue
        refs = extract_task_refs(line)
        if refs and all(
            ref.split("@", 1)[1] in EXCLUDED_TASK_IDS
            for ref in refs
        ):
            continue
        line = remove_excluded_task_refs(line, EXCLUDED_TASK_IDS)
        key, content = split_model_line(line)
        _clean_key, source_text = split_fill_source_key(key)
        note, start_time = parse_model_meta(key)
        entries[key] = {
            "key": key,
            "note": note,
            "start_time": start_time,
            "refs": extract_task_refs(content),
            "fill_sources": parse_fill_sources(source_text),
        }
    return entries


def source_entry_from_line(line: str, model_entries: dict[str, dict]) -> dict:
    key, content = split_model_line(line)
    entry = model_entries.get(key)
    if entry:
        return entry
    note, start_time = parse_model_meta(key)
    return {
        "key": key,
        "note": note,
        "start_time": start_time,
        "refs": extract_task_refs(content),
        "fill_sources": parse_fill_sources(split_fill_source_key(key)[1]),
    }


def sort_block(task_lines: list[str]) -> list[str]:
    """对区块内的 task 行去重并排序，返回排序后的行列表"""
    # ---- 去重：保留首次出现的 Task ID ----
    seen_ids = set()
    deduped = []
    for line in task_lines:
        parsed = parse_task_line(line)
        if parsed:
            tid = parsed[0]
            if tid not in seen_ids:
                seen_ids.add(tid)
                deduped.append(line)
            # 重复 ID 的行直接丢弃
        else:
            # 非 Task 行原样保留（不参与去重）
            deduped.append(line)

    # ---- 分组排序 ----
    untrained = []   # (task_id, line)
    trained = []
    others = []

    for line in deduped:
        parsed = parse_task_line(line)
        if parsed:
            tid, status, _ = parsed
            if status == "未训练":
                untrained.append((tid, line))
            else:
                trained.append((tid, line))
        else:
            others.append(line)

    # 未训练降序，已训练降序
    untrained.sort(key=lambda x: x[0], reverse=True)
    trained.sort(key=lambda x: x[0], reverse=True)

    result = []
    result.extend(line for _, line in untrained)
    result.extend(line for _, line in trained)
    result.extend(others)
    return result


def format_source_label(entry: dict) -> str:
    note = entry.get("note") or "未知训练标注"
    start_time = entry.get("start_time") or ""
    return f"{note}[{start_time}]" if start_time else note


def task_ref_sort_key(ref: str) -> tuple[str, int]:
    match = TASK_REF_RE.fullmatch(ref.strip())
    if not match:
        return ref.casefold(), 0
    prefix, task_id = ref.split("@", 1)
    return prefix.casefold(), -int(task_id)


def build_task_group_summary(lines: list[str], source_entries: list[dict]) -> list[str]:
    """
    生成块末尾的 Task 群：
      1. 合并未训练 Task 与最终完整 Task；
      2. 去重后按 autoedge/cvat 和数字倒序排列；
      3. 用 #Task群# 表示，不输出“信息”或“无”占位文字。
    """
    refs = []
    seen = set()
    for line in lines:
        parsed = parse_task_line(line)
        if not parsed or parsed[1] != "未训练":
            continue
        ref = f"autoedge@{parsed[0]}".lower()
        if ref in seen:
            continue
        seen.add(ref)
        refs.append(ref)

    if source_entries:
        for ref in source_entries[0].get("refs", []):
            normalized = ref.lower()
            if normalized in seen:
                continue
            seen.add(normalized)
            refs.append(normalized)

    if not refs:
        return []

    refs.sort(key=task_ref_sort_key)
    return ["#" + ",".join(refs) + "#"]


def append_untrained_summary(lines: list[str], source_entries: list[dict]) -> list[str]:
    """给任务块追加 [autoedge@...]、{备注&...} 和 #Task群# 汇总。"""
    untrained_ids = []
    untrained_tags = []
    seen_tags = set()

    for line in lines:
        parsed = parse_task_line(line)
        if not parsed:
            continue
        task_id, status, _ = parsed
        if status != "未训练":
            continue
        untrained_ids.append(str(task_id))

        stripped = line.strip()
        if stripped:
            last_field = stripped.rsplit(None, 1)[-1]
            if last_field not in seen_tags:
                seen_tags.add(last_field)
                untrained_tags.append(last_field)

    enriched = list(lines)
    if untrained_ids:
        enriched.append('[' + ','.join(f'autoedge@{task_id}' for task_id in untrained_ids) + ',]')
    if untrained_tags:
        enriched.append('{' + ''.join(untrained_tags) + '}')
    enriched.extend(build_task_group_summary(lines, source_entries))
    return enriched

def process_text(text: str) -> str:
    """处理整个文件内容，返回新文本"""
    # 按空行分割区块
    blocks = re.split(r'\n\s*\n', text)
    new_blocks = []
    model_entries = load_model_entries()

    for block in blocks:
        if not block.strip():
            continue
        lines = block.splitlines()
        if not lines:
            continue

        first_line = lines[0]
        if first_line.startswith("最新Task"):
            source_lines = [
                remove_excluded_task_refs(line, EXCLUDED_TASK_IDS)
                for line in lines
                if line.startswith("最新Task")
                and not text_exclude_reason(line, MAPPING_EXCLUDE_SUBSTRINGS)
                and not (
                    extract_task_refs(line)
                    and all(
                        ref.split("@", 1)[1] in EXCLUDED_TASK_IDS
                        for ref in extract_task_refs(line)
                    )
                )
            ]
            task_lines = [line for line in lines if not line.startswith("最新Task")]
            task_lines = [
                line
                for line in task_lines
                if not text_exclude_reason(line, MAPPING_EXCLUDE_SUBSTRINGS)
                and not (
                    parse_task_line(line)
                    and str(parse_task_line(line)[0]) in EXCLUDED_TASK_IDS
                )
            ]
            source_entries = [source_entry_from_line(line, model_entries) for line in source_lines]
            sorted_tasks = sort_block(task_lines)
            sorted_tasks = append_untrained_summary(sorted_tasks, source_entries)
            new_block = "\n".join(source_lines + sorted_tasks)
        else:
            filtered_lines = [
                line
                for line in lines
                if not text_exclude_reason(line, MAPPING_EXCLUDE_SUBSTRINGS)
                and not (
                    parse_task_line(line)
                    and str(parse_task_line(line)[0]) in EXCLUDED_TASK_IDS
                )
            ]
            new_block = "\n".join(filtered_lines)
            if not new_block.strip():
                continue
        new_blocks.append(new_block)

    return "\n\n".join(new_blocks) + "\n"

def main():
    if not INPUT_FILE.exists():
        print(f"错误: 输入文件不存在 {INPUT_FILE}")
        return

    original_text = INPUT_FILE.read_text(encoding="utf-8")
    processed = process_text(original_text)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(processed, encoding="utf-8")
    print(f"处理完成，输出文件: {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
