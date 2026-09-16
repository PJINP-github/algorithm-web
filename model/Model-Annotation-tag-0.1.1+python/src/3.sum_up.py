import os
import re
import sys
from ask_data_utils import (
    ASK_DATA_FILE,
    CLASS_OUT_FILE,
    DATA_CLASS_FILE,
    H_MODE_HISTORY_FILE,
    MAPPING_FILE,
    MODEL_FILE,
    OUTLOOK_FILE,
    excluded_task_ids,
    flow_records,
    history_assignment_for_task,
    history_priority_targets,
    load_category_mapping_exclude_substrings,
    load_category_mapping_rule_items,
    load_config,
    load_h_mode_history as load_history_file,
    match_category_mapping_items,
    model_line_matches_keyword,
    parse_ask_lines,
    record_exclude_reason,
    remove_excluded_task_refs,
    resolve_existing_work_file,
    resolve_mapping_file,
    resolve_work_dir,
)

base_dir=''
if getattr(sys, 'frozen', False):
    base_dir = os.path.dirname(sys.executable)
else:
    base_dir = os.path.dirname(os.path.abspath(__file__))

work_dir = str(resolve_work_dir(base_dir))
os.makedirs(work_dir, exist_ok=True)
CONFIG = load_config()
H_MODE_HISTORY_PATH = os.path.join(work_dir, H_MODE_HISTORY_FILE)

# ========== 配置与文件读取 ==========
# 1. 读取标准训练类别清单（由 2.model.py 生成）
class_out_path = os.path.join(work_dir, CLASS_OUT_FILE)
with open(class_out_path, "r", encoding="utf-8") as f:
    keywords_lines = [line.strip() for line in f if line.strip()]
str_01 = " ".join(keywords_lines)

# 2. 读取类别标准化映射规则，构建反向映射表
#    正向：原始类别名 -> 标准化类别名（如 "套管油位计" -> "表计"）
#    反向：标准化类别名 -> [所有原始类别名列表]
mapping_path = str(resolve_mapping_file(work_dir))
reverse_mapping = {}   # {标准化类别: [原始类别1, 原始类别2, ...]}
mapping_items = load_category_mapping_rule_items(mapping_path)
mapping_exclude_substrings = load_category_mapping_exclude_substrings(mapping_path)
if mapping_items:
    for item in mapping_items:
        raw_cat = item.get("task_source") or item.get("source")
        std_cat = item.get("model_target") or item.get("target")
        if raw_cat and std_cat:
            reverse_mapping.setdefault(std_cat, []).append(raw_cat)
    print(f"反向映射表已构建: {len(reverse_mapping)} 个标准化类别")
    for std_cat, raw_cats in reverse_mapping.items():
        print(f"  {std_cat} <- {raw_cats}")
else:
    print(f"警告: 未找到 {MAPPING_FILE}，路径为 {mapping_path}")

# 3. 读取原始标注类别清单（由 1.task_id.py 生成）
data_class_path = os.path.join(work_dir, DATA_CLASS_FILE)
all_raw_categories = set()
if os.path.exists(data_class_path):
    with open(data_class_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                all_raw_categories.add(line)

ask_records = []

output_file = os.path.join(work_dir, OUTLOOK_FILE)

# 手动映射表（保留，用于处理特殊的一对一映射，如"数显"在 ask_data 中实际存为"数字显示"）
ask_keyword_mapping = {
    "数显": "数字显示",
}
# ===============================


def extract_trained_ids(model_line):
    """提取完整Task:后所有 autoedge@数字，返回集合"""
    task_match = re.search(r'完整Task:(.*)', model_line)
    if not task_match:
        return set()
    content = task_match.group(1)
    ids = re.findall(r'autoedge@(\d+)', content, re.IGNORECASE)
    return set(ids)


def truncate_task_in_line(line):
    """将完整Task:部分截断为前5个autoedge@XXXX，其余用,...代替（仅用于显示）"""
    match = re.search(r'完整Task:(.*)', line)
    if not match:
        return line
    content = match.group(1)
    items = re.findall(r'autoedge@\d+', content, re.IGNORECASE)
    if len(items) <= 5:
        return line
    truncated = ','.join(items[:5]) + ',...'
    return re.sub(r'完整Task:.*', '完整Task:' + truncated, line)


def build_search_keywords(std_keyword):
    """
    为标准化关键词构建用于搜索标注任务明细的关键词列表。
    包含：
    1. std_keyword 本身
    2. ask_keyword_mapping 中的映射值（如"数显" -> "数字显示"）
    3. reverse_mapping 中该 std_keyword 对应的所有原始类别名
    4. 若 std_keyword 本身就是一个原始类别名，也在 all_raw_categories 中
    """
    search_terms = set()

    # 1. 标准化关键词本身
    search_terms.add(std_keyword)

    # 2. ask_keyword_mapping 中的手动映射
    mapped = ask_keyword_mapping.get(std_keyword)
    if mapped:
        search_terms.add(mapped)

    # 3. 反向映射：该标准化类别对应的所有原始类别名
    raw_cats = reverse_mapping.get(std_keyword, [])
    for rc in raw_cats:
        search_terms.add(rc)

    # 4. 如果 std_keyword 恰好也在 all_raw_categories 中（即它本身就是一个原始类别）
    #    这一步实际上已被步骤1覆盖，但保留以明确语义

    return list(search_terms)


def match_ask_category(category, search_terms):
    """检查任务类别是否包含 search_terms 中的任一关键词。"""
    for term in search_terms:
        if term in category:
            return True
    return False


H_MODE_HISTORY = load_history_file(H_MODE_HISTORY_PATH)


def history_targets_for_record(record):
    assignment = history_assignment_for_task(
        record.task_id,
        H_MODE_HISTORY,
        current_line=record.to_line(),
    )
    return assignment["accepted"], assignment["rejected"]


def mapped_targets_for_record(record):
    """Return model keywords this Task can feed, respecting strong/reusable rules."""
    match_config = ((CONFIG or {}).get("model") or {}).get("category_match", {})
    matched_items = match_category_mapping_items(
        record.category,
        mapping_items,
        match_config,
        field="task_source",
        reusable_all=False,
    )
    if not matched_items:
        matched_items = match_category_mapping_items(
            record.to_line(),
            mapping_items,
            match_config,
            field="task_source",
            reusable_all=True,
        )
    targets = []
    seen = set()
    for item in matched_items:
        target = item.get("model_target") or item.get("target")
        if target and target not in seen:
            seen.add(target)
            targets.append(target)
    priority_targets, _assignment = history_priority_targets(
        record.task_id,
        targets,
        H_MODE_HISTORY,
        current_line=record.to_line(),
    )
    return priority_targets


def record_matches_keyword(record, std_keyword, search_terms):
    mapped_targets = mapped_targets_for_record(record)
    assignment = history_assignment_for_task(
        record.task_id,
        H_MODE_HISTORY,
        current_line=record.to_line(),
    )
    if mapped_targets or assignment["has_instruction"]:
        return std_keyword in mapped_targets
    return match_ask_category(record.category, search_terms)


# ========== 主逻辑 ==========
ask_path = str(resolve_existing_work_file(work_dir, ASK_DATA_FILE, base_dir))
if not os.path.isfile(ask_path):
    raise FileNotFoundError(
        f"未找到标注任务明细：{ask_path}；"
        f"用户目录和模型共享目录均不存在 {ASK_DATA_FILE}"
    )
with open(ask_path, "r", encoding="utf-8") as f:
    raw_ask_records = parse_ask_lines(f)
ask_records = flow_records(
    raw_ask_records,
    CONFIG,
    mapping_exclude_substrings,
)
excluded_ids = excluded_task_ids(raw_ask_records, mapping_exclude_substrings)

file_path = os.path.join(work_dir, MODEL_FILE)
with open(file_path, "r", encoding="utf-8") as f:
    raw_model_lines = f.readlines()

model_lines = []
for line in raw_model_lines:
    if any(
        substring and substring in line
        for substring in mapping_exclude_substrings
    ):
        continue
    refs = re.findall(r"(?:autoedge|cvat)@(\d+)", line, flags=re.IGNORECASE)
    if refs and all(task_id in excluded_ids for task_id in refs):
        continue
    model_lines.append(remove_excluded_task_refs(line, excluded_ids))

keywords = str_01.split()
blocks = []

for kw in keywords:
    # ---------- 构建搜索关键词列表 ----------
    search_terms = build_search_keywords(kw)
    print(f"\n关键词 [{kw}] 搜索词列表: {search_terms}")

    # ---------- 匹配 model 行（完整训练备注包含关键词）----------
    model_matched = []
    for line in model_lines:
        # 训练备注可能包含多个项目名，不能依赖空白分割后的固定列号。
        if model_line_matches_keyword(
            line,
            kw,
            case_sensitive=(
                ((CONFIG or {}).get("model") or {})
                .get("category_match", {})
                .get("case_sensitive", False)
            ),
        ):
            model_matched.append(line.rstrip('\n'))

    # ---------- 匹配 ask 行（只使用“任务状态=已完成”的记录）----------
    ask_matched = []   # 元素为 (task_id, rest_of_line)
    for record in ask_records:
        if record_matches_keyword(record, kw, search_terms):
            tid = record.task_id
            if re.fullmatch(r'\d+', tid):
                ask_matched.append((tid, record.flow_text))

    if not model_matched or not ask_matched:
        blocks.append(f"# 关键词 [{kw}] 无匹配的 model 或 ask 行")
        print(f"  警告: 关键词 [{kw}] model_matched={len(model_matched)}, ask_matched={len(ask_matched)}")
        continue

    # 生成输出块
    lines_out = []
    for m_line in model_matched:
        # 显示用行（截断完整Task）
        display_line = truncate_task_in_line(m_line)
        lines_out.append(display_line)

        # 获取训练状态判断用的完整任务ID集合
        trained_ids = extract_trained_ids(m_line)

        for tid, rest in ask_matched:
            status = "已训练" if tid in trained_ids else "未训练"
            # 新格式：---Task XXXX      状态      后续列
            line_out = f"---Task {tid}      {status}      {rest}"
            lines_out.append(line_out)

    blocks.append("\n".join(lines_out))

# 用两个空行连接所有关键词块
final_output = "\n\n\n".join(blocks)

print(f"\n{'='*60}")
print("预览输出（前100行）:")
print("="*60)
preview_lines = final_output.split('\n')
for i, line in enumerate(preview_lines[:100]):
    print(line)
if len(preview_lines) > 100:
    print(f"... (共 {len(preview_lines)} 行，仅显示前100行)")

with open(output_file, "w", encoding="utf-8") as f:
    f.write(final_output)

print(f"\n输出已写入: {output_file}")
print(f"共 {len(blocks)} 个区块")
