#!/usr/bin/env python3

import sys
import re
from pathlib import Path
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill
from openpyxl.utils import get_column_letter

############################################################
# 精简模式字段 —— 列顺序已按要求调整
############################################################
FILTER_FIELDS = [
    "站点",
    "算法小类",
    "任务日期",
    "准确率",          # 移到任务日期右侧
    "审核后准确率",    # 移到任务日期右侧
    "点位数量",
    "准确数量",
    "审核后准确数量",
    "拍照模糊数量",
    "审核后拍照模糊数量"
]

# 列索引
COL_SITE = 0
COL_ALGO_CAT = 1
COL_TASK_DATE = 2
COL_ACCURACY = 3
COL_AUDIT_ACCURACY = 4

# 颜色填充
FILL_LIGHT_YELLOW = PatternFill(start_color="FFFFE0", end_color="FFFFE0", fill_type="solid")   # 浅黄 <95
FILL_YELLOW = PatternFill(start_color="FFFF00", end_color="FFFF00", fill_type="solid")          # 黄 <80
FILL_RED = PatternFill(start_color="FF9999", end_color="FF9999", fill_type="solid")            # 红 <50
FILL_NONE = PatternFill(fill_type=None)

############################################################
# 站点名称清理：去掉末尾的日期（如 "坡渡风场 2026-08-31" → "坡渡风场"）
############################################################
def clean_site_name(name: str) -> str:
    if not name:
        return name
    parts = name.split()
    # 如果多于1个部分，且最后一个部分看起来像日期（YYYY-MM-DD），则移除它
    if len(parts) > 1 and re.fullmatch(r'\d{4}-\d{2}-\d{2}', parts[-1]):
        return " ".join(parts[:-1])
    return name

############################################################
# 文本清理
############################################################
def clean_text(node):
    if node is None:
        return ""
    for tag in node.find_all(["svg", "script", "style"]):
        tag.decompose()
    text = node.get_text(separator=" ", strip=True)
    return " ".join(text.split())

############################################################
# 获取table行
############################################################
def get_rows(table):
    rows = []
    for child in table.children:
        name = getattr(child, "name", None)
        if name == "tr":
            rows.append(child)
        elif name in ("thead", "tbody", "tfoot"):
            for tr in child.find_all("tr", recursive=False):
                rows.append(tr)
    return rows

############################################################
# 获取td/th
############################################################
def get_cells(tr):
    result = []
    for child in tr.children:
        name = getattr(child, "name", None)
        if name in ("td", "th"):
            result.append(child)
    return result

############################################################
# rowspan colspan
############################################################
def span_value(cell, name):
    try:
        value = int(cell.get(name, 1))
    except:
        value = 1
    if value < 1:
        value = 1
    return value

############################################################
# table解析
############################################################
def parse_table(table):
    rows = get_rows(table)
    data = []
    rowspan = {}

    for tr in rows:
        row = []
        col = 0
        # 填充上一行rowspan
        while col in rowspan:
            item = rowspan[col]
            row.append(item["value"])
            item["left"] -= 1
            if item["left"] <= 0:
                del rowspan[col]
            col += 1

        for cell in get_cells(tr):
            while col in rowspan:
                item = rowspan[col]
                row.append(item["value"])
                item["left"] -= 1
                if item["left"] <= 0:
                    del rowspan[col]
                col += 1

            value = clean_text(cell)
            colspan = span_value(cell, "colspan")
            rowspan_num = span_value(cell, "rowspan")

            for i in range(colspan):
                row.append(value)
                if rowspan_num > 1:
                    rowspan[col] = {
                        "value": value,
                        "left": rowspan_num - 1
                    }
                col += 1

        data.append(row)

    if not data:
        return []

    max_col = max(len(x) for x in data)
    for row in data:
        while len(row) < max_col:
            row.append("")

    return data

############################################################
# 判断表头
############################################################
def find_header_index(data):
    for i, row in enumerate(data):
        text = " ".join(row)
        if "站点" in text and "算法小类" in text:
            return i
    return 0

############################################################
# a模式字段过滤
############################################################
def filter_algorithm_data(data):
    if not data:
        return []

    header_index = find_header_index(data)
    header = data[header_index]

    # 建立字段索引
    index_map = {}
    for index, name in enumerate(header):
        name = name.strip()
        if name in FILTER_FIELDS:
            index_map[name] = index

    # 检查字段
    missing = []
    for field in FILTER_FIELDS:
        if field not in index_map:
            missing.append(field)
    if missing:
        print("[WARN] 缺少字段:", missing)

    # 新表头
    result = []
    result.append(FILTER_FIELDS)

    # 数据
    for row in data[header_index + 1:]:
        new_row = []
        for field in FILTER_FIELDS:
            idx = index_map.get(field, -1)
            if idx >= 0 and idx < len(row):
                new_row.append(row[idx])
            else:
                new_row.append("")
        # 空行过滤
        if any(x.strip() for x in new_row):
            result.append(new_row)

    return result

############################################################
# b 模式：站点排序
#
# 逻辑：
# 1. 收集每个站点下所有小类的任务日期
# 2. 过滤：排除带"静默"字样的日期；排除离"中心日期"超过两周的普通任务日期
# 3. 取剩余日期的中位数作为站点的"处理日期"
# 4. 超过今天的日期视为去年
# 5. 按处理日期排序：离今天越远越靠前（越早越靠前）
############################################################

def parse_date(date_str):
    """尝试解析日期字符串，返回datetime或None"""
    if not date_str or date_str == "-":
        return None
    date_str = date_str.strip()
    # 尝试多种格式
    for fmt in ["%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"]:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return None

def is_silent_date(date_str):
    """判断日期字符串中是否包含'静默'"""
    return "静默" in date_str if date_str else False

def get_center_date(dates):
    """获取一组日期的中间日期（中位数）"""
    if not dates:
        return None
    sorted_dates = sorted(dates)
    n = len(sorted_dates)
    mid = n // 2
    return sorted_dates[mid]

def get_site_process_date(site_rows, site_name):
    """
    获取站点的处理日期。
    site_rows: 属于该站点的所有数据行（每行是列表）
    返回: (process_date, site_name) - 用于排序
    """
    # 收集所有任务日期
    all_dates = []
    for row in site_rows:
        date_str = str(row[COL_TASK_DATE]).strip() if COL_TASK_DATE < len(row) else ""
        if not date_str or date_str == "-":
            continue
        # 跳过"静默"日期
        if is_silent_date(date_str):
            continue
        dt = parse_date(date_str)
        if dt:
            all_dates.append(dt)

    if not all_dates:
        # 无有效日期，放到最后
        return datetime(1900, 1, 1), site_name

    # 找到中心日期
    center_date = get_center_date(all_dates)
    if center_date is None:
        return datetime(1900, 1, 1), site_name

    # 过滤：排除距离中心日期超过两周（14天）的日期
    filtered_dates = []
    for dt in all_dates:
        diff = abs((dt - center_date).days)
        if diff <= 14:
            filtered_dates.append(dt)

    if not filtered_dates:
        # 所有日期都超过了14天，那就用全部日期
        filtered_dates = all_dates

    # 取过滤后的中位数作为站点处理日期
    process_date = get_center_date(filtered_dates)
    if process_date is None:
        return datetime(1900, 1, 1), site_name

    # 超过当天的日期视为去年的
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if process_date > today:
        # 将年份设为去年
        try:
            process_date = process_date.replace(year=process_date.year - 1)
        except ValueError:
            # 处理闰年2月29日的情况
            process_date = process_date.replace(year=process_date.year - 1, month=2, day=28)

    return process_date, site_name


def is_no_data(val):
    """判断一个值是否为无数据（空、-、无数据）"""
    if val is None:
        return True
    val = str(val).strip()
    return val in ("", "-", "无数据")


def remove_empty_sites_for_mode_b(data):
    """
    b 模式：删除所有数据字段（算法小类~审核后拍照模糊数量，即第2列~第10列）
    全为"无数据"或"-"的站点。
    
    注意：只要准确率或审核后准确率任一有有效数据，站点就保留。
    """
    if len(data) <= 1:
        return data

    header = data[0]
    rows = data[1:]

    # 按站点分组（先清理站点名，确保同一站点的不同日期后缀被正确合并）
    site_groups = {}       # cleaned_site_name -> [rows]
    site_order = []        # 保持站点出现顺序（清理后的名称）
    for row in rows:
        raw_name = str(row[COL_SITE]).strip() if COL_SITE < len(row) else ""
        site_name = clean_site_name(raw_name)
        if site_name not in site_groups:
            site_groups[site_name] = []
            site_order.append(site_name)
        site_groups[site_name].append(row)

    # 检查每个站点是否需要删除
    deleted_sites = set()
    for site_name in site_order:
        site_rows = site_groups[site_name]
        all_no_data = True
        for row in site_rows:
            # 检查第2列到第10列（索引1~9），即除站点外的所有数据字段
            for col in range(1, len(FILTER_FIELDS)):
                if col < len(row):
                    val = str(row[col]).strip()
                    if not is_no_data(val):
                        all_no_data = False
                        break
            if not all_no_data:
                break
        
        if all_no_data:
            deleted_sites.add(site_name)

    if deleted_sites:
        print("[INFO] b模式删除无数据站点: {} 个".format(len(deleted_sites)))
        for s in sorted(deleted_sites):
            print("  -", s)

    # 重新组装数据（排除被删除的站点）
    result = [header]
    for site_name in site_order:
        if site_name not in deleted_sites:
            result.extend(site_groups[site_name])

    return result


def sort_data_for_mode_b(data):
    """
    b 模式：按站点处理日期排序（越远越靠前），
    同一站点内保持原顺序。
    
    站点名称会先清理日期后缀再进行分组，确保同一站点（仅日期不同）被正确合并。
    """
    if len(data) <= 1:
        return data

    header = data[0]
    rows = data[1:]

    # 按站点分组（先清理站点名）
    site_groups = {}       # cleaned_site_name -> {"original_names": set(), "rows": [...]}
    site_order = []        # 保持站点出现顺序（清理后的名称）
    for row in rows:
        raw_name = str(row[COL_SITE]).strip() if COL_SITE < len(row) else ""
        site_name = clean_site_name(raw_name)
        if site_name not in site_groups:
            site_groups[site_name] = []
            site_order.append(site_name)
        site_groups[site_name].append(row)

    # 计算每个站点的处理日期
    site_process_info = {}
    for site_name in site_order:
        site_process_info[site_name] = get_site_process_date(site_groups[site_name], site_name)

    # 按处理日期排序（越远越靠前 = 日期越早越靠前 = 升序）
    sorted_sites = sorted(site_order, key=lambda s: site_process_info[s][0])

    # 重新组装数据
    result = [header]
    for site_name in sorted_sites:
        result.extend(site_groups[site_name])

    return result


############################################################
# 判断准确率背景色
############################################################
def get_accuracy_fill(accuracy_str, audit_accuracy_str):
    """
    根据准确率和审核后准确率判断背景填充色。
    优先级：审核后准确率 > 准确率
    无数据（"-"/"无数据"）不参与判定。
    两个都无数据返回 None（不染色）。
    """
    def parse_percent(val):
        """解析百分比字符串，返回浮点数或None"""
        if not val or val == "-" or val == "无数据":
            return None
        val = str(val).strip().replace("%", "").replace("％", "")
        try:
            return float(val)
        except ValueError:
            return None

    audit_val = parse_percent(audit_accuracy_str)
    acc_val = parse_percent(accuracy_str)

    # 判断使用哪个值
    if audit_val is not None:
        target_val = audit_val
    elif acc_val is not None:
        target_val = acc_val
    else:
        return None  # 都无数据，不染色

    if target_val < 50:
        return FILL_RED
    elif target_val < 80:
        return FILL_YELLOW
    elif target_val < 95:
        return FILL_LIGHT_YELLOW
    else:
        return None  # >=95，不染色


############################################################
# 自动列宽
############################################################
def text_width(value):
    if value is None:
        return 0
    width = 0
    for ch in str(value):
        if ord(ch) > 255:
            width += 2
        else:
            width += 1
    return width

def auto_width(ws):
    for col in range(1, ws.max_column + 1):
        max_width = 0
        for row in range(1, ws.max_row + 1):
            value = ws.cell(row, col).value
            max_width = max(max_width, text_width(value))
        width = max_width + 3
        if width < 10:
            width = 10
        if width > 50:
            width = 50
        ws.column_dimensions[get_column_letter(col)].width = width

############################################################
# Excel格式（所有单元格水平居中 + 垂直居中）
############################################################
def format_excel(ws):
    # 表头加粗
    for cell in ws[1]:
        cell.font = Font(bold=True)
    # 所有单元格居中
    for row in ws.iter_rows():
        for cell in row:
            cell.alignment = Alignment(
                horizontal="center",
                vertical="center",
                wrap_text=True
            )
    # 冻结首行
    ws.freeze_panes = "A2"
    # 自动筛选
    if ws.max_row > 1:
        ws.auto_filter.ref = ws.dimensions

############################################################
# b 模式：准确率颜色标记
# 对"算法小类 任务日期 准确率 审核后准确率"四列染色
############################################################
def apply_accuracy_colors(ws):
    """
    对每行数据，根据准确率/审核后准确率的值给
    算法小类、任务日期、准确率、审核后准确率 四个单元格染色。
    """
    cols_to_color = [COL_ALGO_CAT + 1, COL_TASK_DATE + 1,
                     COL_ACCURACY + 1, COL_AUDIT_ACCURACY + 1]

    for row_idx in range(2, ws.max_row + 1):
        accuracy_val = str(ws.cell(row_idx, COL_ACCURACY + 1).value or "")
        audit_accuracy_val = str(ws.cell(row_idx, COL_AUDIT_ACCURACY + 1).value or "")

        fill = get_accuracy_fill(accuracy_val, audit_accuracy_val)
        if fill is None:
            continue

        for col_idx in cols_to_color:
            cell = ws.cell(row_idx, col_idx)
            cell.fill = fill

############################################################
# 合并相同单元格
############################################################
def merge_same(ws, columns=4):
    max_col = min(columns, ws.max_column)
    for col in range(1, max_col + 1):
        start = 2
        last = ws.cell(2, col).value
        for row in range(3, ws.max_row + 2):
            value = ws.cell(row, col).value if row <= ws.max_row else None
            if value != last:
                if last not in (None, "") and row - start > 1:
                    ws.merge_cells(start_row=start, start_column=col, end_row=row - 1, end_column=col)
                    ws.cell(start, col).alignment = Alignment(
                        horizontal="center",
                        vertical="center"
                    )
                start = row
                last = value

############################################################
# 输出文件名
############################################################
def output_name(filename, index):
    path = Path(filename)
    if index == 1:
        return path
    return path.with_name(f"{path.stem}-{index}{path.suffix}")

############################################################
# 删除旧文件
############################################################
def clean_old(output):
    path = Path(output)
    if path.exists():
        path.unlink()
    for f in path.parent.glob(path.stem + "-*" + path.suffix):
        if f.exists():
            f.unlink()

############################################################
# 创建Excel（含站点名清理 & "无数据"替换）
############################################################
def create_excel(data, filename, mode):
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"

    header_written = False
    for row in data:
        processed_row = []
        for idx, cell in enumerate(row):
            val = str(cell).strip()
            # 非表头行且为站点列（第1列，索引0）时，清理站点名
            if header_written and idx == 0:
                val = clean_site_name(val)
            # "无数据" 替换为 "-"
            if val == "无数据":
                val = "-"
            processed_row.append(val)
        ws.append(processed_row)
        header_written = True   # 第一行处理后标记表头已写

    format_excel(ws)

    # 合并规则
    if mode == "y":
        merge_same(ws, 4)          # 完整模式：合并前4列
    elif mode in ("a", "b"):
        merge_same(ws, 1)          # 精简模式：只合并站点列

    # b 模式：准确率颜色标记
    if mode == "b":
        apply_accuracy_colors(ws)

    auto_width(ws)
    wb.save(filename)

############################################################
# HTML转换
############################################################
def convert(html_file, output_file, mode):
    print()
    print("[INFO] 输入:", html_file)

    html = Path(html_file).read_text(encoding="utf-8", errors="ignore")
    soup = BeautifulSoup(html, "lxml")
    tables = soup.find_all("table")

    print("[INFO] table数量:", len(tables))

    clean_old(output_file)
    count = 0

    for i, table in enumerate(tables, start=1):
        print()
        print("[INFO] 处理table:", i)

        data = parse_table(table)
        if not data:
            continue

        # a/b 模式过滤
        if mode in ("a", "b"):
            data = filter_algorithm_data(data)
            print("[INFO] {}模式字段: {}".format(mode, len(data[0]) if data else 0))

        if len(data) <= 1:
            continue

        # b 模式：先删除无数据站点，再排序
        if mode == "b":
            data = remove_empty_sites_for_mode_b(data)
            if len(data) <= 1:
                continue
            data = sort_data_for_mode_b(data)
            print("[INFO] b模式站点排序完成")

        output = output_name(output_file, i)
        create_excel(data, output, mode)

        print("[INFO] 输出:", output)
        print("[INFO] 数据:", len(data), "行", len(data[0]) if data else 0, "列")
        count += 1

    print()
    print("================================")
    print("[INFO] 完成")
    print("[INFO] 生成:", count, "个文件")
    print("================================")

############################################################
# main
############################################################
def main():
    if len(sys.argv) < 4:
        print("""
用法:

python3 html_to_xlsx.py input.html output.xlsx mode

mode:
  y : 完整模式 + 合并
  n : 完整模式 + 不合并
  a : 算法审核精简模式
  b : 算法审核精简 + 排序 + 准确率可视化

例如:
  python3 html_to_xlsx.py page.html result.xlsx a
""")
        sys.exit(1)

    html = sys.argv[1]
    output = sys.argv[2]
    mode = sys.argv[3]

    if mode not in ("y", "n", "a", "b"):
        print("[ERROR] mode错误，只支持 y/n/a/b")
        sys.exit(1)

    convert(html, output, mode)

if __name__ == "__main__":
    main()