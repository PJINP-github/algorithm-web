"""xlsx 读取、站点筛选、百分比与数值归一化。"""
from __future__ import annotations

from typing import Any, Iterable

import openpyxl


def load_rows(xlsx_path: str) -> list[dict[str, Any]]:
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []

    header = [str(h).strip() if h is not None else "" for h in rows[0]]
    result: list[dict[str, Any]] = []
    for raw in rows[1:]:
        if raw is None:
            continue
        if all(v is None or str(v).strip() == "" for v in raw):
            continue
        item: dict[str, Any] = {}
        for idx, name in enumerate(header):
            if not name:
                continue
            item[name] = raw[idx] if idx < len(raw) else None
        result.append(item)
    return result


def filter_by_station(
    rows: Iterable[dict[str, Any]], station_keyword: str
) -> list[dict[str, Any]]:
    keyword = (station_keyword or "").strip()
    if not keyword:
        return []
    matched: list[dict[str, Any]] = []
    for row in rows:
        station = str(row.get("站点") or "")
        if keyword in station:
            matched.append(row)
    return matched


def normalize_percent(value: Any) -> float | None:
    """把 '95%' / 0.95 / 95 / '-' 统一为 0-100 的 float 或 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return round(v * 100.0, 4) if 0.0 < v <= 1.0 else round(v, 4)
    s = str(value).strip()
    if not s or s in {"-", "—", "N/A", "na", "NA"}:
        return None
    if s.endswith("%"):
        s = s[:-1].strip()
        try:
            return round(float(s), 4)
        except ValueError:
            return None
    try:
        v = float(s)
        return round(v * 100.0, 4) if 0.0 < v <= 1.0 else round(v, 4)
    except ValueError:
        return None


def to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip().replace(",", "")
    if not s or s in {"-", "—"}:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None