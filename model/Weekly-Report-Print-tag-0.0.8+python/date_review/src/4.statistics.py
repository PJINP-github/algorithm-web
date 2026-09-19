#!/usr/bin/env python3
"""
Filter and sort station entries from z_树结构数据.txt based on class.txt.

Configuration:
  Set CUTOFF_DATE below to a string like "05-20" to only consider this-year
  dates from that day up to today for the dense cluster average.
  Set to None to disable filtering.
"""

import os
import re
import json
import datetime
import math

# ========== 用户配置 ==========
CUTOFF_DATE = "05-04"   # 例如 "05-20" 表示仅考虑 5月20日 至今的今年日期；设为 None 则不过滤
# =============================


def load_project_config(project_root):
    """读取项目 config.yaml；缺少 PyYAML 或配置文件时返回空配置。"""
    config_path = os.path.join(project_root, "config.yaml")
    if not os.path.exists(config_path):
        return {}, config_path
    try:
        import yaml
    except ImportError:
        print("[WARNING] 未安装 PyYAML，跳过 config.yaml 中的 date_review 配置")
        return {}, config_path
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}, config_path
    except Exception as exc:
        print(f"[WARNING] 读取 config.yaml 失败: {exc}")
        return {}, config_path


def resolve_config_path(path_value, project_root):
    path_text = str(path_value or "").strip()
    if not path_text:
        return ""
    path_text = os.path.expandvars(os.path.expanduser(path_text))
    if os.path.isabs(path_text):
        return os.path.normpath(path_text)
    return os.path.normpath(os.path.join(project_root, path_text))


def is_empty_recognition(value):
    if value is None:
        return True
    return str(value).strip() in ("", "-", "--", "无数据")


def normalize_task_date_key(value):
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text in ("-", "--", "无数据"):
        return ""
    for pattern in (
        r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$",
        r"^(\d{1,2})[-/.](\d{1,2})$",
    ):
        match = re.match(pattern, text)
        if match:
            groups = match.groups()
            month, day = groups[-2], groups[-1]
            return f"{int(month)}-{int(day)}"
    return text


def first_record_value(record, names):
    for name in names:
        if name in record:
            return record.get(name)
    return None


def iter_json_records(node, parent_key=""):
    if isinstance(node, dict):
        yield node, parent_key
        for key, value in node.items():
            yield from iter_json_records(value, str(key))
    elif isinstance(node, list):
        for item in node:
            yield from iter_json_records(item, parent_key)


def collect_empty_recognition_keys(config, project_root):
    accuracy_cfg = (
        (config.get("date_review", {}) or {})
        .get("accuracy_json", {}) or {}
    )
    if not accuracy_cfg.get("enabled", True):
        return set()
    if not accuracy_cfg.get("empty_recognition_sort_last", True):
        return set()

    json_path = resolve_config_path(
        accuracy_cfg.get("path", r"date_review\0Work\accuracy.json"),
        project_root,
    )
    if not json_path or not os.path.exists(json_path):
        return set()

    recognition_field = str(
        accuracy_cfg.get("recognition_field", "recognition") or "recognition"
    )
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        print(f"[WARNING] 读取 accuracy.json 失败: {exc}")
        return set()

    empty_keys = set()
    for record, parent_key in iter_json_records(data):
        if recognition_field not in record:
            continue
        if not is_empty_recognition(record.get(recognition_field)):
            continue

        algo_name = first_record_value(
            record,
            ("算法小类", "算法名称", "algorithmClass", "algorithm_class", "algorithm", "algo", "name"),
        )
        date_value = first_record_value(
            record,
            ("任务日期", "taskDate", "task_date", "date", "task_time", "time"),
        )
        if (not algo_name or not date_value) and parent_key and "_" in parent_key:
            date_part, algo_part = parent_key.split("_", 1)
            date_value = date_value or date_part
            algo_name = algo_name or algo_part

        date_key = normalize_task_date_key(date_value)
        algo_text = str(algo_name or "").strip()
        if date_key and algo_text:
            empty_keys.add(f"{date_key}_{algo_text}")

    if empty_keys:
        print(f"[INFO] accuracy.json 中 recognition 为空的记录: {len(empty_keys)} 条，将按无效日期排到最后")
    return empty_keys

def chinese_ordinal(n):
    """Convert an integer 1-99 to Chinese ordinal like '第一位', '第十二位'."""
    if n < 1 or n > 99:
        return f'第{n}位'
    digits = ['', '一', '二', '三', '四', '五', '六', '七', '八', '九', '十']
    if n <= 10:
        return f'第{digits[n]}位'
    elif n < 20:
        return f'第十{digits[n - 10]}位'
    else:
        tens = n // 10
        ones = n % 10
        if ones == 0:
            return f'第{digits[tens]}十位'
        else:
            return f'第{digits[tens]}十{digits[ones]}位'


def date_to_doy(month, day):
    """Convert month, day to day-of-year (using non-leap year)."""
    days_in_month = [0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return sum(days_in_month[:month]) + day


def doy_to_date(doy):
    """Convert day-of-year back to (month, day)."""
    days_in_month = [0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    month = 1
    while month <= 12 and doy > days_in_month[month]:
        doy -= days_in_month[month]
        month += 1
    return month, doy


def compute_dense_average(dates):
    """
    Given a list of (month, day) tuples (all this-year),
    find the largest cluster where max-min <= 15 days,
    and return the average day-of-year (float) of that cluster.
    If multiple clusters have the same size, pick the one with the
    smallest average (earliest).  If no dates, return None.
    Also returns cluster size and the average float.
    """
    if not dates:
        return None, 0, None

    doys = sorted(date_to_doy(m, d) for m, d in dates)

    best_start = 0
    best_end = 0
    best_len = 1

    left = 0
    for right in range(len(doys)):
        while doys[right] - doys[left] > 15:
            left += 1
        cur_len = right - left + 1
        if cur_len > best_len:
            best_len = cur_len
            best_start = left
            best_end = right
        elif cur_len == best_len:
            cur_avg = sum(doys[left:right+1]) / cur_len
            best_avg = sum(doys[best_start:best_end+1]) / best_len
            if cur_avg < best_avg:
                best_start = left
                best_end = right

    cluster = doys[best_start:best_end+1]
    avg_doy = sum(cluster) / len(cluster)
    return avg_doy, best_len, cluster


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    base_dir = os.path.dirname(script_dir)
    project_root = os.path.dirname(base_dir)
    work_dir = os.path.join(base_dir, '0Work')
    os.makedirs(work_dir, exist_ok=True)
    project_config, _ = load_project_config(project_root)
    empty_recognition_keys = collect_empty_recognition_keys(project_config, project_root)

    # 解析配置的截止日期
    cutoff_doy = None
    if CUTOFF_DATE is not None:
        try:
            m = re.match(r'^(\d{1,2})-(\d{1,2})$', CUTOFF_DATE)
            if m:
                month = int(m.group(1))
                day = int(m.group(2))
                cutoff_doy = date_to_doy(month, day)
                print(f"[INFO] 代码配置的截止日期: {CUTOFF_DATE} (年内第{cutoff_doy}天)")
            else:
                raise ValueError
        except:
            print(f"[WARNING] 配置的 CUTOFF_DATE 格式无效: {CUTOFF_DATE}，应为 MM-DD。将禁用过滤。")
            cutoff_doy = None

    today = datetime.date.today()
    cur_month = today.month
    cur_day = today.day
    today_doy = date_to_doy(cur_month, cur_day)

    # Read class.txt
    class_path = os.path.join(script_dir, 'class.txt')
    with open(class_path, 'r', encoding='utf-8') as f:
        target_stations = set(line.strip() for line in f if line.strip())

    # Read z_表格化数据.txt for accuracy data
    table_data = {}
    table_path = os.path.join(script_dir, 'z_表格化数据.txt')
    if os.path.exists(table_path):
        with open(table_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split('\t')
                if len(parts) >= 10:
                    if parts[0] in ('-', 'svg') and len(parts) >= 10:
                        algo_name = parts[1]
                        date_str = parts[2]
                        accuracy = parts[8] if len(parts) > 8 else '无数据'
                        review_accuracy = parts[9] if len(parts) > 9 else '无数据'
                        key = f"{date_str}_{algo_name}"
                        table_data[key] = {
                            'accuracy': accuracy,
                            'review_accuracy': review_accuracy
                        }
                    elif parts[0] not in ('发电', '电网', 'div') and len(parts) >= 10:
                        algo_name = parts[1]
                        date_str = parts[2]
                        accuracy = parts[8] if len(parts) > 8 else '无数据'
                        review_accuracy = parts[9] if len(parts) > 9 else '无数据'
                        key = f"{date_str}_{algo_name}"
                        table_data[key] = {
                            'accuracy': accuracy,
                            'review_accuracy': review_accuracy
                        }
        print(f"[INFO] 已加载 {len(table_data)} 条准确率数据")
    else:
        print(f"[WARNING] 未找到 {table_path}，将不添加准确率信息")

    # Read z_树结构数据.txt
    data_path = os.path.join(work_dir, 'z_树结构数据.txt')
    with open(data_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    stations_data = []
    current_station = None
    current_entries = []
    in_target_station = False

    def format_accuracy_suffix(accuracy, review_accuracy):
        acc = accuracy.strip() if accuracy else ''
        rev_acc = review_accuracy.strip() if review_accuracy else ''
        
        if acc == '无数据' and rev_acc == '无数据':
            return '/'
        elif acc == '无数据':
            return f'/{rev_acc}'
        elif rev_acc == '无数据':
            return f'{acc}/'
        else:
            return f'{acc}/{rev_acc}'

    def save_station():
        nonlocal current_entries, current_station
        if not in_target_station or not current_entries:
            current_entries = []
            return

        current_entries.sort(key=lambda e: e[0])

        # 最早有效键（用于 Statistics.txt）
        valid_keys = [k for k, _, _, _ in current_entries if k[0] != 2]
        earliest_key = min(valid_keys) if valid_keys else (2, 0, 0)

        # 收集今年日期
        all_this_year_dates = []
        for k, _, _, _ in current_entries:
            if k[0] == 1:
                _, m, d = k
                all_this_year_dates.append((m, d))

        # 应用截止过滤
        if cutoff_doy is not None:
            filtered_dates = []
            for m, d in all_this_year_dates:
                doy = date_to_doy(m, d)
                if cutoff_doy <= doy <= today_doy:
                    filtered_dates.append((m, d))
            this_year_dates = filtered_dates
        else:
            this_year_dates = all_this_year_dates

        avg_doy, cluster_size, _ = compute_dense_average(this_year_dates)

        if avg_doy is not None:
            avg_sort_key = (0, avg_doy)
        else:
            avg_sort_key = (1, 0)

        stations_data.append({
            'name': current_station,
            'entries': current_entries,
            'earliest_key': earliest_key,
            'avg_sort_key': avg_sort_key,
            'avg_doy': avg_doy,
            'cluster_size': cluster_size,
            'all_this_year_count': len(all_this_year_dates),
            'filtered_count': len(this_year_dates) if cutoff_doy is not None else None,
        })
        current_entries = []

    # --- 解析输入（同原逻辑）---
    i = 0
    while i < len(lines):
        line = lines[i].rstrip('\n')
        if not line.strip():
            save_station()
            current_station = None
            in_target_station = False
            i += 1
            continue
        if line.startswith('-----'):
            if in_target_station and current_station is not None:
                m = re.match(r'^-----\s+(\S+)\s+(.*)', line)
                if m:
                    date_str = m.group(1)
                    rest = m.group(2)
                    date_match = re.match(r'^(\d{1,2})-(\d{1,2})$', date_str)
                    
                    algo_name_match = re.search(r'^\s*(\S+)', rest)
                    algo_name = algo_name_match.group(1) if algo_name_match else ''
                    
                    accuracy_info = table_data.get(f"{date_str}_{algo_name}", {})
                    accuracy = accuracy_info.get('accuracy', '无数据')
                    review_accuracy = accuracy_info.get('review_accuracy', '无数据')
                    no_recognition = (
                        f"{normalize_task_date_key(date_str)}_{algo_name}"
                        in empty_recognition_keys
                    )
                    
                    if date_match and not no_recognition:
                        month = int(date_match.group(1))
                        day = int(date_match.group(2))
                        is_last_year = (month, day) > (cur_month, cur_day)
                        year_flag = 0 if is_last_year else 1
                        display_date = f"{month:02d}-{day:02d}" + ("(去年)" if is_last_year else "")
                        sort_key = (year_flag, month, day)
                        output_line = f'-----  {display_date}  {rest}'
                        current_entries.append((sort_key, output_line, accuracy, review_accuracy))
                    else:
                        current_entries.append(((2, 0, 0), line, accuracy, review_accuracy))
            i += 1
            continue
        if line.startswith('---'):
            save_station()
            current_station = line[3:].strip()
            current_entries = []
            in_target_station = current_station in target_stations
            i += 1
            continue
        save_station()
        current_station = None
        in_target_station = False
        current_entries = []
        i += 1
    save_station()

    # ========== 输出 Statistics.txt ==========
    stations_earliest = sorted(stations_data, key=lambda s: s['earliest_key'])
    out1_lines = []
    for rank, st in enumerate(stations_earliest, 1):
        rank_str = chinese_ordinal(rank)
        out1_lines.append(f'---{st["name"]} @：{rank_str}')
        for _, out_line, accuracy, review_accuracy in st['entries']:
            suffix = format_accuracy_suffix(accuracy, review_accuracy)
            out1_lines.append(f'{out_line}  -----{suffix}')
        if rank < len(stations_earliest):
            out1_lines.append('')
    out1_path = os.path.join(work_dir, 'Statistics.txt')
    with open(out1_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(out1_lines) + '\n')

    # ========== 输出 average_statistics.txt ==========
    stations_avg = sorted(stations_data, key=lambda s: s['avg_sort_key'])
    out2_lines = []
    for rank, st in enumerate(stations_avg, 1):
        rank_str = chinese_ordinal(rank)
        mid_date_str = ""
        if st['avg_doy'] is not None:
            floor_doy = math.floor(st['avg_doy'])
            m, d = doy_to_date(floor_doy)
            mid_date_str = f" #：{m:02d}-{d:02d}"
        out2_lines.append(f'---{st["name"]} @：{rank_str}{mid_date_str}')
        for _, out_line, accuracy, review_accuracy in st['entries']:
            suffix = format_accuracy_suffix(accuracy, review_accuracy)
            out2_lines.append(f'{out_line}  -----{suffix}')
        if rank < len(stations_avg):
            out2_lines.append('')
    out2_path = os.path.join(work_dir, 'average_statistics.txt')
    with open(out2_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(out2_lines) + '\n')

    # ========== 终端日志 ==========
    print("\n" + "=" * 60)
    print("Statistics.txt (按最早有效日期排序)")
    print("=" * 60)
    for rank, st in enumerate(stations_earliest, 1):
        key = st['earliest_key']
        if key[0] == 0:
            _, m, d = key
            reason = f"最早有效日期: {m:02d}-{d:02d} (去年)"
        elif key[0] == 1:
            _, m, d = key
            reason = f"最早有效日期: {m:02d}-{d:02d}"
        else:
            reason = "无有效日期数据"
        print(f"{rank:2d}. {st['name']} -> {reason}")

    print("\n" + "=" * 60)
    print("average_statistics.txt (按密集簇平均排序)")
    if cutoff_doy is not None:
        cm, cd = doy_to_date(cutoff_doy)
        print(f"过滤配置：仅考虑今年 {cm:02d}-{cd:02d} 至 {cur_month:02d}-{cur_day:02d} 之间的日期")
    else:
        print("无日期过滤（使用所有今年日期）")
    print("=" * 60)
    for rank, st in enumerate(stations_avg, 1):
        if st['avg_doy'] is not None:
            floor_doy = math.floor(st['avg_doy'])
            m, d = doy_to_date(floor_doy)
            mid_date = f"{m:02d}-{d:02d}"
            if cutoff_doy is not None:
                reason = f"密集簇平均: {mid_date} (簇大小={st['cluster_size']}, 今年原始数={st['all_this_year_count']}, 过滤后={st['filtered_count']})"
            else:
                reason = f"密集簇平均: {mid_date} (簇大小={st['cluster_size']}, 今年条目数={st['all_this_year_count']})"
        else:
            if cutoff_doy is not None:
                reason = f"无有效簇 (今年原始数={st['all_this_year_count']}, 过滤后={st['filtered_count']}) → 排最后"
            else:
                reason = f"无有效簇 (今年条目数={st['all_this_year_count']}) → 排最后"
        print(f"{rank:2d}. {st['name']} -> {reason}")

    print(f"\n[INFO] Statistics.txt 已生成 – {len(stations_earliest)} 个站点")
    print(f"[INFO] average_statistics.txt 已生成 – {len(stations_avg)} 个站点")


if __name__ == '__main__':
    main()
