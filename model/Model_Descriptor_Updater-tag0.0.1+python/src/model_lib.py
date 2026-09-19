"""通用工具：配置、路径、文件分类、锚点扫描。"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

_MODEL_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _MODEL_ROOT / "config.yaml"

ANCHOR_DEF_RE = re.compile(
    r"^(?P<indent>\s*)(?P<key>[\w\-.]+)\s*:\s*&(?P<anchor>\w+)\s+(?P<value>\S.*?)\s*$",
    re.MULTILINE,
)

_DEFAULT_CONFIG: dict[str, Any] = {
    "model_file_suffixes": [".pt", ".om"],
    "model_descriptor_name": "model_descriptor.yaml",
    "output_yaml_name": "model_descriptor_updated.yaml",
    "output_folder_prefix": "model_descriptor_pack",
    "Algorithm_Order": [],
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict[str, Any]:
    raw: dict[str, Any] = {}
    if _CONFIG_PATH.exists():
        with _CONFIG_PATH.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    return _deep_merge(_DEFAULT_CONFIG, raw)


def normalize_path(raw: Any) -> Path | None:
    if raw is None:
        return None
    s = str(raw).strip().strip('"').strip("'")
    if not s:
        return None
    p = Path(s)
    if p.exists():
        return p
    if os.name == "posix" and "\\" in s:
        alt = Path(s.replace("\\", "/"))
        if alt.exists():
            return alt
    return p


def strip_model_suffix(name: str, suffixes: list[str]) -> str:
    lower = name.lower()
    for suf in suffixes:
        s = str(suf).lower()
        if not s:
            continue
        if lower.endswith(s):
            return name[: -len(suf)]
    return name


def is_model_file(name: str, suffixes: list[str]) -> bool:
    lower = name.lower()
    return any(lower.endswith(str(s).lower()) for s in suffixes if str(s))


def safe_filename(name: str) -> str:
    bad = '<>:"/\\|?*\n\r\t'
    out = "".join("_" if c in bad else c for c in str(name))
    return out.strip().strip(".") or "output"


def detect_has_anchors(yaml_text: str) -> bool:
    return "&" in yaml_text


def pick_latest_model(models: list[Path], substring: str) -> Path | None:
    if not substring:
        return None
    matched: list[tuple[float, Path]] = []
    for m in models:
        try:
            if substring in m.name and m.is_file():
                matched.append((m.stat().st_mtime, m))
        except OSError:
            continue
    if not matched:
        return None
    matched.sort(key=lambda item: item[0], reverse=True)
    return matched[0][1]


def scan_anchor_definitions(yaml_text: str) -> list[tuple[str, str]]:
    return [
        (m.group("anchor"), m.group("value").strip())
        for m in ANCHOR_DEF_RE.finditer(yaml_text)
    ]


def detect_ambiguous_substrings(
    yaml_text: str, algorithm_order: list[dict[str, Any]]
) -> list[tuple[str, list[str]]]:
    anchor_defs = scan_anchor_definitions(yaml_text)
    if not anchor_defs:
        return []

    result: list[tuple[str, list[str]]] = []
    for entry in algorithm_order or []:
        sub = str(entry.get("substring") or "")
        if not sub:
            continue
        matched = [name for name, value in anchor_defs if sub in value]
        if len(matched) > 1:
            result.append((sub, matched))
    return result


def scan_inputs(
    folder: Path, config: dict[str, Any]
) -> tuple[Path | None, list[Path]]:
    suffixes = list(config.get("model_file_suffixes") or [])
    target_name = str(
        config.get("model_descriptor_name") or "model_descriptor.yaml"
    ).lower()

    yaml_path: Path | None = None
    models: list[Path] = []

    try:
        candidates = sorted(folder.rglob("*"))
    except OSError:
        candidates = []

    for item in candidates:
        if not item.is_file():
            continue
        name_lower = item.name.lower()
        if name_lower == target_name and yaml_path is None:
            yaml_path = item
        elif is_model_file(item.name, suffixes):
            models.append(item)

    return yaml_path, models


def list_folder_contents(folder: Path, limit: int = 60) -> list[str]:
    try:
        items = sorted(folder.rglob("*"))
    except OSError:
        return []
    out: list[str] = []
    for p in items[:limit]:
        try:
            out.append(str(p.relative_to(folder)))
        except ValueError:
            out.append(str(p))
    return out