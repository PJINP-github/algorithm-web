"""把 xlsx 数据子集渲染成表格图片，作为准确率证明。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import matplotlib

matplotlib.use("Agg")
import matplotlib.image as mpimg  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402

for _candidate in (
    "Microsoft YaHei",
    "SimHei",
    "Noto Sans CJK SC",
    "WenQuanYi Zen Hei",
    "Source Han Sans SC",
):
    try:
        font_manager.findfont(_candidate, fallback_to_default=False)
        plt.rcParams["font.sans-serif"] = [_candidate]
        break
    except Exception:
        continue
plt.rcParams["axes.unicode_minus"] = False


_TABLE_COLUMNS = [
    "算法小类",
    "点位数量",
    "准确数量",
    "拍照模糊数量",
    "审核后拍照模糊数量",
    "准确率",
]


def image_aspect_ratio(path: Path) -> float:
    img = mpimg.imread(str(path))
    h, w = img.shape[:2]
    if h <= 0:
        return 1.0
    return float(w) / float(h)


def _parse_accuracy(value: Any) -> float | None:
    """把 '88%' / 0.88 / 88 / '-' 解析为 0-100 的 float 或 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v * 100.0 if 0.0 < v <= 1.0 else v
    s = str(value).strip()
    if not s or s in {"-", "—", "N/A", "na", "NA"}:
        return None
    if s.endswith("%"):
        s = s[:-1].strip()
    try:
        v = float(s)
        return v * 100.0 if 0.0 < v <= 1.0 else v
    except ValueError:
        return None


def _sort_key(row: dict[str, Any]):
    """排序键：高准确率在前、低在后、'-' 排最下。

    返回 (group, value)：
      group=0 -> 有效数值，value 取负用于降序
      group=1 -> 无法解析，排最后
    """
    acc = _parse_accuracy(row.get("准确率"))
    if acc is None:
        return (1, 0.0)
    return (0, -acc)


def render_table_image(
    rows: Iterable[dict[str, Any]],
    output_path: Path,
    footer: str = "",
    *,
    fig_width_inch: float = 14.0,
    dpi: int = 200,
    font_size: int = 11,
    style: dict[str, Any] | None = None,
) -> Path:
    """渲染表格图片。

    - 数据行按「准确率」从高到低排序，'-' 排在最后。
    - style 支持：
        header_bg / header_fg / header_bold
        zebra_even_bg / zebra_odd_bg
        show_index / index_header
        cell_edge_color / cell_edge_width
    - footer 右对齐放在表格右下角，字号 = font_size - 1。
    """
    style = style or {}
    header_bg = str(style.get("header_bg", "#4F81BD"))
    header_fg = str(style.get("header_fg", "#FFFFFF"))
    header_bold = bool(style.get("header_bold", True))
    zebra_even_bg = str(style.get("zebra_even_bg", "#F2F2F2"))
    zebra_odd_bg = str(style.get("zebra_odd_bg", "#FFFFFF"))
    show_index = bool(style.get("show_index", True))
    index_header = str(style.get("index_header", "序号"))
    cell_edge_color = str(style.get("cell_edge_color", "#BFBFBF"))
    cell_edge_width = float(style.get("cell_edge_width", 0.6))

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # 按准确率排序
    sorted_rows = sorted(list(rows), key=_sort_key)

    cols = ([index_header] if show_index else []) + list(_TABLE_COLUMNS)

    display_rows: list[list[str]] = []
    for i, row in enumerate(sorted_rows, start=1):
        line = [_fmt(row.get(c)) for c in _TABLE_COLUMNS]
        if show_index:
            line = [str(i)] + line
        display_rows.append(line)

    if not display_rows:
        display_rows = [["（无数据）"] * len(cols)]

    row_count = len(display_rows)
    fig_height = max(2.0, 0.5 * row_count + 1.4)

    fig, ax = plt.subplots(figsize=(fig_width_inch, fig_height))
    ax.axis("off")

    table = ax.table(
        cellText=display_rows,
        colLabels=cols,
        loc="center",
        cellLoc="center",
        bbox=[0.0, 0.05, 1.0, 0.95],
    )
    table.auto_set_font_size(False)
    table.set_fontsize(font_size)
    table.scale(1.0, 1.5)

    for (r, _c), cell in table.get_celld().items():
        cell.set_edgecolor(cell_edge_color)
        cell.set_linewidth(cell_edge_width)
        if r == 0:
            cell.set_facecolor(header_bg)
            cell.get_text().set_color(header_fg)
            cell.get_text().set_weight("bold" if header_bold else "normal")
        else:
            bg = zebra_even_bg if r % 2 == 1 else zebra_odd_bg
            cell.set_facecolor(bg)

    if footer:
        footer_size = max(7, font_size - 1)
        fig.text(
            0.995, 0.006, footer,
            ha="right", va="bottom",
            fontsize=footer_size,
            color="#333333",
        )

    fig.subplots_adjust(left=0.0, right=1.0, top=1.0, bottom=0.0)
    fig.savefig(
        output_path, dpi=dpi, bbox_inches="tight",
        facecolor="white", pad_inches=0.04,
    )
    plt.close(fig)
    return output_path


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    return str(value)