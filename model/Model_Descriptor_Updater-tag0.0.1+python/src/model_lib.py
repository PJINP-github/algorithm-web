"""通用工具：配置、路径、文件分类、后缀处理。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

_MODEL_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _MODEL_ROOT / "config.yaml"

_DEFAULT_CONFIG: dict[str, Any] = {
    "model_file_suffixes": [".pt", ".om"],
    "model_descriptor_name": "model_descriptor.yaml",
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
    """去掉白名单后缀（不区分大小写），只去掉一个后缀。"""
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
    """检测文本中是否存在锚点定义（&name）。不把引用 *name 算作锚点。"""
    return "&" in yaml_text


def pick_latest_model(models: list[Path], substring: str) -> Path | None:
    """从 models 中筛选文件名包含 substring 的，取修改时间最新的那个。"""
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