'''
爬取http://autoedge.jiangxingai.com/algorithm/train/model-trainingTask最新一百个 标注任务ID、状态等信息
用于审查哪种算法模型需要更新数据
nodes_str 从原始标注类别清单读取，每行一个类别，合并为空格分隔
'''

import sys
import os
import json
import platform
import time
import subprocess
import traceback
import re
from datetime import datetime
if platform.system() == 'Windows':
    import msvcrt
else:
    import tty
    import termios
from DrissionPage import Chromium, ChromiumOptions
from DrissionPage.common import Settings
from DrissionPage.errors import PageDisconnectedError
from ask_data_utils import (
    ASK_DATA_FILE,
    CLASS_OUT_FILE,
    DATA_CLASS_FILE,
    MAPPING_FILE,
    MODEL_FILE,
    category_key,
    excluded_task_ids,
    extract_task_dataset_ids,
    find_text_mappings,
    flow_records,
    history_assignment_for_task,
    load_category_mapping_exclude_substrings,
    load_category_mapping_rule_items,
    latest_task_summary,
    load_config,
    mapping_rules_to_first_target_mapping,
    match_category_mapping_items,
    parse_ask_lines,
    remove_excluded_task_refs,
    resolve_chrome_path,
    resolve_mapping_file,
    resolve_work_dir,
    text_exclude_reason,
    training_note_is_blocked,
    training_note_max_rows,
)

Settings.set_language('en')


def portal_automated():
    return (
        os.environ.get('RUST_PORTAL_AUTOMATED') == '1'
        or os.environ.get('RUST_PORTAL_HEADLESS') == '1'
    )

# ---------- 1. 确定项目目录 ----------
if getattr(sys, 'frozen', False):
    base_dir = os.path.dirname(sys.executable)
else:
    base_dir = os.path.dirname(os.path.abspath(__file__))

work_dir = str(resolve_work_dir(base_dir))
os.makedirs(work_dir, exist_ok=True)
H_MODE_HISTORY_FILE = "数字-0-h模式历史学习.json"
H_MODE_HISTORY_PATH = os.path.join(work_dir, H_MODE_HISTORY_FILE)


def current_excluded_task_ids():
    mapping_path = resolve_mapping_file(work_dir)
    exclude_substrings = load_category_mapping_exclude_substrings(mapping_path)
    ask_data_path = os.path.join(work_dir, ASK_DATA_FILE)
    if not os.path.exists(ask_data_path):
        return set()
    try:
        with open(ask_data_path, "r", encoding="utf-8") as f:
            records = parse_ask_lines(f)
    except OSError:
        return set()
    return excluded_task_ids(records, exclude_substrings)


# ---------- 2. 查找 Chrome 可执行文件 ----------
chrome_path = resolve_chrome_path(base_dir)
print(f"使用浏览器: {chrome_path}")

# ---------- 3. 浏览器用户数据目录 ----------
user_data_dir = os.environ.get('RUST_PORTAL_USER_DATA_DIR', '').strip() or os.path.join(base_dir, '..', 'chromium_user_data')
user_data_dir = os.path.abspath(user_data_dir)
os.makedirs(user_data_dir, exist_ok=True)

# ---------- 4. 调试端口 ----------
DEBUG_PORT = int(os.environ.get('RUST_PORTAL_DEBUG_PORT', '9222'))
CONFIG = load_config()
TRAINING_LIST_URL = 'http://autoedge.jiangxingai.com/algorithm/train/model-trainingTask'

# ---------- 5. 检查浏览器是否已在调试端口运行 ----------
def check_browser_running(port):
    try:
        import requests
        response = requests.get(f'http://127.0.0.1:{port}/json/version', timeout=2)
        return response.status_code == 200
    except Exception:
        return False


def remove_stale_browser_locks(data_dir):
    """清理已退出浏览器留下的锁文件，避免新调试实例直接退出。"""
    if platform.system() != 'Windows':
        return
    for name in ('SingletonLock', 'SingletonSocket', 'SingletonCookie'):
        path = os.path.join(data_dir, name)
        try:
            os.remove(path)
            print(f"已清理浏览器锁定文件: {path}")
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"清理浏览器锁定文件失败: {path}: {exc}")


def startup_log_text(log_file):
    try:
        with open(log_file, 'r', encoding='utf-8', errors='replace') as stream:
            return stream.read().strip()
    except OSError as exc:
        return f"读取启动日志失败: {exc}"


def browser_diagnostic_log(chrome_path):
    path = os.path.join(os.path.dirname(chrome_path), 'debug.log')
    return startup_log_text(path)


def browser_start_timeout():
    try:
        return max(5, int(os.environ.get('RUST_PORTAL_BROWSER_START_TIMEOUT', '45')))
    except ValueError:
        return 45


def minimize_browser_window(tab):
    """尽量把门户启动的浏览器窗口最小化，避免干扰当前桌面。"""
    if platform.system() != 'Windows':
        return False
    for attempt in range(3):
        try:
            window_info = tab.run_cdp('Browser.getWindowForTarget')
            window_id = window_info.get('windowId')
            if not window_id:
                time.sleep(0.5)
                continue
            tab.run_cdp(
                'Browser.setWindowBounds',
                windowId=window_id,
                bounds={'windowState': 'minimized'},
            )
            print("浏览器窗口已最小化")
            return True
        except Exception as exc:
            print(f"最小化浏览器窗口失败（第 {attempt + 1} 次）: {exc}")
            time.sleep(0.5)
    return False

browser_running = check_browser_running(DEBUG_PORT)
browser_started_by_script = False
print(f"浏览器运行状态: {'已运行' if browser_running else '未运行'}")

# ---------- 6. 若未运行，启动浏览器 ----------
if not browser_running:
    print("正在启动浏览器...")
    remove_stale_browser_locks(user_data_dir)
    args = [
        chrome_path,
        f'--remote-debugging-port={DEBUG_PORT}',
        '--remote-debugging-address=127.0.0.1',
        f'--user-data-dir={user_data_dir}',
        '--disable-background-networking',
        '--disable-component-update',
        '--disable-breakpad',
        '--disable-features=TranslateUI,MediaRouter,OptimizationHints',
        '--no-first-run',
        '--no-default-browser-check',
        '--start-minimized',
        '--window-size=1920,1080',
    ]
    if platform.system() != 'Windows':
        args.extend(['--no-sandbox', '--disable-dev-shm-usage'])
    if platform.system() == 'Windows':
        args.extend([
            '--disable-gpu',
            '--disable-software-rasterizer',
            '--in-process-gpu',
            '--disable-gpu-compositing',
        ])
    
    if os.environ.get('RUST_PORTAL_HEADLESS') == '1' or (platform.system() == 'Linux' and not os.environ.get('DISPLAY')):
        args.append('--headless=new')
        args.append('--disable-gpu')

    
    print(f"启动命令: {' '.join(args)}")
    
    log_file = os.path.join(base_dir, 'chrome_startup_model.log')
    startupinfo = None
    creationflags = 0
    if platform.system() == 'Windows':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 6  # SW_MINIMIZE
        creationflags = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
    with open(log_file, 'w', encoding='utf-8') as f:
        try:
            process = subprocess.Popen(
                args,
                stdout=f,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                startupinfo=startupinfo,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise RuntimeError(f"启动指定浏览器失败: {chrome_path}: {exc}") from exc
    
    print(f"浏览器进程ID: {process.pid}")
    
    max_wait = browser_start_timeout()
    start_time = time.time()
    while time.time() - start_time < max_wait:
        if check_browser_running(DEBUG_PORT):
            print("浏览器启动成功")
            break
        exit_code = process.poll()
        if exit_code is not None:
            startup_log = startup_log_text(log_file)
            browser_log = browser_diagnostic_log(chrome_path)
            raise RuntimeError(
                f"指定浏览器启动失败，进程已退出（退出码 {exit_code}）: "
                f"{chrome_path}\n启动日志:\n{startup_log or '(空)'}"
                f"\n浏览器诊断日志:\n{browser_log or '(空)'}"
            )
        time.sleep(0.5)
        print(f"等待浏览器启动... {int(time.time() - start_time)}s")
    else:
        startup_log = startup_log_text(log_file)
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        browser_log = browser_diagnostic_log(chrome_path)
        raise RuntimeError(
            f"浏览器启动超时（{max_wait}s），端口 {DEBUG_PORT} 未开放: "
            f"{chrome_path}\n启动日志:\n{startup_log or '(空)'}"
            f"\n浏览器诊断日志:\n{browser_log or '(空)'}"
        )
    browser_started_by_script = True


# ========== 交互式处理无效类别的函数 ==========

def get_char():
    """获取单个字符输入（用于交互式选择）"""
    if portal_automated():
        return '\n'
    if platform.system() == 'Windows':
        return msvcrt.getwch()

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(sys.stdin.fileno())
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    return ch


def get_line(default=None):
    """获取一行输入（普通模式）"""
    value = sys.stdin.readline().strip()
    if value == "" and default is not None:
        return default
    return value


def get_choice(default=None):
    """获取单字符选择；直接回车时使用默认值。"""
    if portal_automated() and default is not None:
        print(f"'{default}' (门户默认)")
        return default
    ch = get_char()
    if ch in ('\n', '\r') and default is not None:
        print(f"'{default}' (默认)")
        return default
    print(f"'{ch}'")
    return ch


def wait_for_enter():
    if portal_automated():
        print("门户自动化模式：跳过等待 Enter")
        return
    while True:
        ch = get_char()
        if ch in ('\n', '\r'):
            return


def load_h_mode_history():
    if not os.path.exists(H_MODE_HISTORY_PATH):
        return {"version": 1, "items": []}
    try:
        with open(H_MODE_HISTORY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"警告: h 模式历史学习文件读取失败，将本次忽略: {exc}")
        return {"version": 1, "items": []}
    if not isinstance(data, dict):
        return {"version": 1, "items": []}
    items = data.get("items", [])
    if not isinstance(items, list):
        items = []
    return {"version": data.get("version", 1), "items": items}


def save_h_mode_history(history):
    os.makedirs(work_dir, exist_ok=True)
    data = {
        "version": 1,
        "items": history.get("items", []),
    }
    with open(H_MODE_HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def history_index(history):
    indexed = {}
    for item in history.get("items", []):
        if not isinstance(item, dict):
            continue
        key = (str(item.get("task_id", "")), str(item.get("target", "")))
        if key[0] and key[1]:
            indexed[key] = item
    return indexed


def history_forced_target(indexed_history, task_id):
    """Return an explicitly accepted strong target for this Task."""
    assignment = history_assignment_for_task(
        task_id,
        indexed_history.values(),
    )
    if not assignment["exclusive"]:
        return ""
    return sorted(assignment["accepted"])[0] if assignment["accepted"] else ""


def history_decision(indexed_history, task_id, target, line):
    item = indexed_history.get((str(task_id), str(target)))
    if not item:
        return "", None
    if item.get("line") == line:
        return item.get("decision", ""), item
    return "changed", item


def update_h_mode_history(history, rows, target, decision):
    indexed = history_index(history)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for cat, tid, line, item in rows:
        key = (str(tid), str(target))
        record = {
            "task_id": str(tid),
            "target": str(target),
            "decision": decision,
            "line": line,
            "category": cat,
            "match": item.get("task_source") or item.get("source", ""),
            "association": item.get("association", ""),
            "updated_at": now,
        }
        existing = indexed.get(key)
        if existing:
            existing.update(record)
        else:
            history.setdefault("items", []).append(record)
            indexed[key] = record


def history_targets_for_records(records, mapping_items, exclude_substrings=None):
    history = load_h_mode_history()
    indexed = history_index(history)
    accepted_targets = set()
    accepted_rows = []
    rejected_rows = []
    changed_rows = []
    for cat, tid, _record, line in records:
        if line_exclude_reason(line, exclude_substrings):
            continue
        assignment = history_assignment_for_task(
            tid,
            indexed.values(),
            current_line=line,
        )
        if assignment["exclusive"]:
            for target in sorted(assignment["accepted"]):
                accepted_targets.add(target)
                accepted_rows.append((cat, tid, line, target))
            continue
        for target in sorted(assignment["accepted"]):
            accepted_targets.add(target)
            accepted_rows.append((cat, tid, line, target))
        for item in _best_rule_item_matches(line, mapping_items):
            target = item.get("model_target") or item.get("target", "")
            if target in assignment["accepted"]:
                continue
            if target in assignment["rejected"]:
                rejected_rows.append((cat, tid, line, target))
                continue
            decision, history_item = history_decision(indexed, tid, target, line)
            if decision == "accepted":
                accepted_targets.add(target)
                accepted_rows.append((cat, tid, line, target))
            elif decision == "rejected":
                rejected_rows.append((cat, tid, line, target))
            elif decision == "changed":
                changed_rows.append((cat, tid, line, target, history_item))
    return sorted(accepted_targets), accepted_rows, rejected_rows, changed_rows


def has_h_mode_pending_records(
    invalid_categories,
    mapping_items,
    exclude_substrings=None,
):
    """Whether h mode has any new or changed Task requiring a decision."""
    records = collect_invalid_task_records(
        invalid_categories,
        exclude_substrings,
    )
    if not records:
        return False

    history = history_index(load_h_mode_history())
    for _cat, tid, _record, line in records:
        if line_exclude_reason(line, exclude_substrings):
            continue
        assignment = history_assignment_for_task(
            tid,
            history.values(),
            current_line=line,
        )
        if assignment["exclusive"]:
            continue
        matches = _best_rule_item_matches(line, mapping_items or [])
        if assignment["accepted"] and not matches:
            continue
        if not matches:
            return True
        for item in matches:
            target = item.get("model_target") or item.get("target", "")
            if target in assignment["accepted"] or target in assignment["rejected"]:
                continue
            decision, _history_item = history_decision(history, tid, target, line)
            if decision not in ("accepted", "rejected"):
                return True
    return False


def handle_invalid_categories(
    invalid_categories,
    mapping,
    mapping_entries,
    mapping_items=None,
    exclude_substrings=None,
):
    """
    交互式处理无法匹配类别标准化映射规则的类别。
    
    参数:
        invalid_categories: list[str] - 无法匹配的原始类别名列表
        mapping: dict[str, str] - {匹配关键词: 输出类别名}
        mapping_entries: list[tuple[int, str, str]] - [(行号, 匹配关键词, 输出类别名), ...]
    
    返回:
        tuple: (updated_mapping, accepted_categories, deleted_categories, skipped)
        - updated_mapping: 可能被更新的 mapping（新增的映射关系）
        - accepted_categories: 用户接受进入后续流程的类别及其映射目标
        - deleted_categories: 用户选择删除（不进入后续流程）的类别
        - skipped: 是否跳过（用户选择不执行后续流程而直接退出）
    """
    mapping_items = mapping_items or [
        {
            "lineno": lineno,
            "source": match_str,
            "target": output_str,
            "association": "reusable",
        }
        for lineno, match_str, output_str in mapping_entries
    ]
    exclude_substrings = exclude_substrings or []
    if not invalid_categories:
        return mapping, [], [], False
    h_mode_available = has_h_mode_pending_records(
        invalid_categories,
        mapping_items,
        exclude_substrings,
    )

    print("\n" + "=" * 60)
    print(f"⚠️  检测到以下类别在 {MAPPING_FILE} 中无法匹配：")
    print("=" * 60)
    for i, cat in enumerate(invalid_categories, 1):
        print(f"  {i}. {cat}")
    print(f"\n共 {len(invalid_categories)} 个未匹配类别")
    print("\n" + "-" * 40)
    print("这些类别对应的 task 数据可能丢失！")
    print("请选择处理模式：")
    print("=" * 60)
    print("  [1] 模式1 - 快速模式")
    print("       直接显示将要丢失的 task 信息，确认后继续/返回")
    print()
    print("  [2] 模式2 - 精细模式（按 Task 数字归类到 mapping 行）")
    print(f"       逐类选择要归类到 {MAPPING_FILE} 的哪一行（按行号数字选择）")
    print("       或按 [d] 删除该类别（不进入后续归纳）")
    print("       完成后确认执行/返回")
    if h_mode_available:
        print()
        print("  [h] 模式h - 全行匹配模式")
        print("       使用完整 Task 行匹配 yaml 规则，命中后按标准类别确认归类")
        print()
    print("  [q] 退出，不执行后续流程")
    print("=" * 60)

    if portal_automated():
        default_choice = "h" if h_mode_available else "1"
        print(f"\n门户自动化模式：直接使用默认 {default_choice}")
        if default_choice == "h":
            return _mode_h_full_line(
                invalid_categories,
                mapping,
                mapping_entries,
                mapping_items,
                exclude_substrings,
            )
        return _mode1_quick(
            invalid_categories,
            mapping,
            mapping_entries,
            mapping_items,
            exclude_substrings,
        )

    while True:
        choices = "1/2/h/q" if h_mode_available else "1/2/q"
        default_choice = "h" if h_mode_available else "1"
        print(f"\n请输入选择 [{choices}，默认 {default_choice}]: ", end='', flush=True)
        ch = get_choice(default_choice)

        if ch == 'q' or ch == 'Q':
            print("\n用户选择退出，跳过后续流程。")
            return mapping, [], [], True

        elif ch == '1':
            return _mode1_quick(
                invalid_categories,
                mapping,
                mapping_entries,
                mapping_items,
                exclude_substrings,
            )

        elif ch == '2':
            return _mode2_fine(
                invalid_categories,
                mapping,
                mapping_entries,
                mapping_items,
                exclude_substrings,
            )

        elif (ch == 'h' or ch == 'H') and h_mode_available:
            return _mode_h_full_line(
                invalid_categories,
                mapping,
                mapping_entries,
                mapping_items,
                exclude_substrings,
            )

        else:
            print(f"无效输入: '{ch}'，请重新选择")


def collect_invalid_task_records(invalid_categories, exclude_substrings=None):
    ask_data_path = os.path.join(work_dir, ASK_DATA_FILE)
    invalid_task_lines = []

    if os.path.exists(ask_data_path):
        with open(ask_data_path, 'r', encoding='utf-8') as f:
            ask_records = flow_records(
                parse_ask_lines(f),
                CONFIG,
                exclude_substrings,
            )
        for cat in invalid_categories:
            for record in ask_records:
                if cat in record.category or cat in record.project:
                    if line_exclude_reason(record.to_line(), exclude_substrings):
                        continue
                    invalid_task_lines.append((cat, record.task_id, record, record.to_line()))
    else:
        print(f"警告: 未找到 {ASK_DATA_FILE}，无法列出具体 task")

    seen = set()
    deduped = []
    for cat, tid, record, line in invalid_task_lines:
        key = (cat, tid)
        if key not in seen:
            seen.add(key)
            deduped.append((cat, tid, record, line))
    return deduped


def _mode1_quick(
    invalid_categories,
    mapping,
    mapping_entries=None,
    mapping_items=None,
    exclude_substrings=None,
):
    """模式1：快速模式 - 列出将丢失的具体 task 行，yes/no 执行"""
    deduped = collect_invalid_task_records(
        invalid_categories,
        exclude_substrings,
    )
    history_targets, history_accepted_rows, history_rejected_rows, history_changed_rows = history_targets_for_records(
        deduped,
        mapping_items or [],
        exclude_substrings,
    )
    history_accepted_keys = {(row[0], row[1]) for row in history_accepted_rows}
    history_rejected_keys = {(row[0], row[1]) for row in history_rejected_rows}
    history_changed_keys = {(row[0], row[1]) for row in history_changed_rows}
    unresolved_rows = [
        row for row in deduped
        if (row[0], row[1]) not in history_accepted_keys
    ]

    print("\n" + "=" * 60)
    print("模式1 - 快速模式")
    print("=" * 60)
    if unresolved_rows:
        print(f"以下 {len(unresolved_rows)} 个 task 尚未完成历史归类：")
        print(f"确认模式 1 后将显示这些 Task 的未划分原因。")
        print(f"涉及 {len(invalid_categories)} 个未匹配类别")
    elif deduped:
        print("这些 Task 已全部命中历史归类，不再重复询问。")
    else:
        print(f"未在 {ASK_DATA_FILE} 中找到对应这些类别的 task 行。")
        print(f"未匹配类别: {', '.join(invalid_categories)}")
    if history_targets:
        print(f"\n历史学习文件已归类 target: {', '.join(history_targets)}")
        print(f"历史已确认 Task: {len(history_accepted_rows)} 条")
    print()
    print("确认后，历史已归类 target 会与正常分类一起进入后续 Task 群。")
    print("是否继续执行？")
    print("  [y] 是的，跳过这些类别，继续执行后续流程")
    print("  [n] 不，返回重新选择")

    while True:
        print("\n请输入 [y/n，默认 y]: ", end='', flush=True)
        ch = get_choice("y")

        if ch == 'y' or ch == 'Y':
            print("\n用户确认，跳过未匹配类别，继续执行...")
            if unresolved_rows:
                print("\n模式1确认后，以下 Task 仍未作划分：")
                for cat, tid, _record, line in unresolved_rows:
                    key = (cat, tid)
                    if key in history_rejected_keys:
                        reason = "历史已拒绝"
                    elif key in history_changed_keys:
                        reason = "历史行内容变化/有异议"
                    else:
                        reason = "未明确归类"
                    print(f"  [{reason}] {line}")
            accepted = [
                (f"历史学习:{target}", target, 0)
                for target in history_targets
            ]
            return mapping, accepted, invalid_categories, False
        elif ch == 'n' or ch == 'N':
            print("\n用户取消，返回模式选择...")
            return handle_invalid_categories(
                invalid_categories,
                mapping,
                mapping_entries,
                mapping_items,
                exclude_substrings,
            )
        else:
            print(f"无效输入: '{ch}'，请输入 y 或 n")


def _mode2_fine(
    invalid_categories,
    mapping,
    mapping_entries,
    mapping_items=None,
    exclude_substrings=None,
):
    """模式2：精细模式 - 按类别标准化映射规则行号逐类选择归类或删除"""
    print("\n" + "=" * 60)
    print(f"模式2 - 精细模式（按 {MAPPING_FILE} 行号归类）")
    print("=" * 60)
    print("逐类处理，为每个未匹配的类别选择：")
    print(f"  - 输入 {MAPPING_FILE} 中的行号数字，将该类别归入对应行")
    print("  - 或输入 'd' 删除此类别（不进入后续归纳）")
    print("-" * 40)

    # 显示类别标准化映射规则完整内容（含行号）
    print(f"\n{MAPPING_FILE} 当前内容：")
    print("-" * 40)
    for lineno, match_str, output_str in mapping_entries:
        print(f"  行 {lineno:2d}: {match_str} → {output_str}")
    print("-" * 40)

    accepted = []   # [(原始类别, 选择的标准化类别, 行号)]
    deleted = []    # [被删除的原始类别]
    new_mapping_entries = []  # 新增的映射条目 [(match_str, output_str)]

    # 构建行号映射: {行号: (match_str, output_str)}
    lineno_map = {lineno: (match_str, output_str) for lineno, match_str, output_str in mapping_entries}
    valid_linenos = sorted(lineno_map.keys())
    max_lineno = max(valid_linenos) if valid_linenos else 0

    for cat in invalid_categories:
        print(f"\n{'─' * 50}")
        print(f"当前类别: 「{cat}」")
        print(f"{'─' * 50}")
        print(f"请选择要归入 {MAPPING_FILE} 的哪一行（行号范围: 1~{max_lineno}）")
        print(f"  [d] 删除此类别（不进入后续归纳）")
        print(f"  [s] 跳过此类别保留到下次处理（暂不决定）")

        while True:
            prompt = f"\n请为「{cat}」选择 {MAPPING_FILE} 行号 [1~{max_lineno}/d/s]: "
            print(prompt, end='', flush=True)
            ch = get_line()
            print(f"'{ch}'")

            if ch == 'd' or ch == 'D':
                deleted.append(cat)
                print(f"  → 已删除: 「{cat}」")
                break
            elif ch == 's' or ch == 'S':
                print(f"  → 已跳过: 「{cat}」（将保留在无效列表中）")
                break
            else:
                try:
                    lineno = int(ch)
                    if lineno in lineno_map:
                        match_str, output_str = lineno_map[lineno]
                        accepted.append((cat, output_str, lineno))
                        new_mapping_entries.append((cat, output_str))
                        print(f"  → 「{cat}」归入 {MAPPING_FILE} 行 {lineno}: {match_str} → {output_str}")
                        break
                    else:
                        print(f"  无效行号，请输入 {', '.join(str(l) for l in valid_linenos)}")
                except ValueError:
                    print(f"  无效输入: '{ch}'，请输入数字行号")

    # 总结
    print("\n" + "=" * 60)
    print("模式2 - 处理结果汇总")
    print("=" * 60)
    if accepted:
        print(f"\n已归类 ({len(accepted)} 个):")
        for raw, target, lineno in accepted:
            print(f"  ✓ 「{raw}」→ 行 {lineno} → 「{target}」")
    if deleted:
        print(f"\n已删除 ({len(deleted)} 个):")
        for cat in deleted:
            print(f"  ✗ 「{cat}」")

    print()
    print("是否执行？")
    print("  [y] 执行 - 应用上述选择，继续后续流程")
    print("  [n] 返回 - 回到模式选择")

    while True:
        print("\n请输入 [y/n，默认 y]: ", end='', flush=True)
        ch = get_choice("y")

        if ch == 'y' or ch == 'Y':
            # 将新增的映射合并到 mapping 中（仅本次运行有效）
            updated_mapping = dict(mapping)
            for match_str, output_str in new_mapping_entries:
                if match_str not in updated_mapping:
                    updated_mapping[match_str] = output_str
            print(f"\n新增 {len(new_mapping_entries)} 条映射，继续执行...")
            return updated_mapping, accepted, deleted, False
        elif ch == 'n' or ch == 'N':
            print("\n用户取消，返回模式选择...")
            return handle_invalid_categories(
                invalid_categories,
                mapping,
                mapping_entries,
                mapping_items,
                exclude_substrings,
            )
        else:
            print(f"无效输入: '{ch}'，请输入 y 或 n")


def _rule_item_matches_text(text, item, match_config):
    pattern = item.get("source", "")
    if not pattern:
        return False
    case_sensitive = match_config.get("case_sensitive", False)
    mode = match_config.get("mode", "longest_substring")
    source_text = text if case_sensitive else text.casefold()
    pattern_text = pattern if case_sensitive else pattern.casefold()
    if mode == "exact":
        return source_text == pattern_text
    if mode == "regex":
        flags = 0 if case_sensitive else re.IGNORECASE
        return re.search(pattern, text, flags) is not None
    return pattern_text in source_text


def _best_rule_item_matches(text, mapping_items):
    match_config = ((CONFIG or {}).get("model") or {}).get("category_match", {})
    return match_category_mapping_items(
        text,
        mapping_items,
        match_config,
        field="task_source",
        reusable_all=True,
    )


def line_exclude_reason(line, exclude_substrings):
    for substring in exclude_substrings or []:
        if substring and substring in line:
            return substring
    return ""


def _mode_h_full_line(
    invalid_categories,
    mapping,
    mapping_entries,
    mapping_items,
    exclude_substrings=None,
):
    print("\n" + "=" * 60)
    print("模式h - 全行匹配模式")
    print("=" * 60)
    invalid_records = collect_invalid_task_records(
        invalid_categories,
        exclude_substrings,
    )
    if not invalid_records:
        print(f"未在 {ASK_DATA_FILE} 中找到对应这些类别的 task 行。")
        print("模式h无法继续匹配，将跳过这些未匹配类别并继续后续流程。")
        return mapping, [], invalid_categories, False

    matched_by_target = {}
    unmatched_rows = []
    excluded_rows = []
    history = load_h_mode_history()
    indexed_history = history_index(history)
    for cat, tid, _record, line in invalid_records:
        exclude_reason = line_exclude_reason(line, exclude_substrings)
        if exclude_reason:
            excluded_rows.append((cat, tid, line, exclude_reason))
            continue
        assignment = history_assignment_for_task(
            tid,
            indexed_history.values(),
            current_line=line,
        )
        if assignment["exclusive"]:
            for target in sorted(assignment["accepted"]):
                matched_by_target.setdefault(target, []).append(
                    (
                        cat,
                        tid,
                        line,
                        {
                            "task_source": f"历史指定:{tid}",
                            "model_target": target,
                            "association": "strong",
                        },
                    )
                )
            continue
        matches = _best_rule_item_matches(line, mapping_items)
        for target in sorted(assignment["accepted"]):
            matched_by_target.setdefault(target, []).append(
                (
                    cat,
                    tid,
                    line,
                    {
                        "task_source": f"历史指定:{tid}",
                        "model_target": target,
                        "association": "reusable",
                    },
                )
            )
        if not matches:
            if assignment["accepted"]:
                continue
            unmatched_rows.append((cat, tid, line))
            continue
        for item in matches:
            target = item.get("model_target") or item.get("target", "")
            if target in assignment["accepted"] or target in assignment["rejected"]:
                continue
            matched_by_target.setdefault(target, []).append((cat, tid, line, item))

    if not matched_by_target:
        print("没有 Task 行通过全行匹配命中映射规则。")
        if unmatched_rows:
            print("\n以下 Task 全行都不对应任何映射规则：")
            for cat, _tid, line in unmatched_rows:
                print(f"  ▌类别: 「{cat}」")
                print(f"      {line}")
        if excluded_rows:
            print("\n以下 Task 命中 yaml 全局排除字段，已优先跳过：")
            for cat, _tid, line, reason in excluded_rows:
                print(f"  ▌类别: 「{cat}」")
                print(f"      {line}\n        排除命中: {reason}")
        print("\n模式h没有可归类的命中项，将跳过这些未匹配类别并继续后续流程。")
        return mapping, [], invalid_categories, False

    if excluded_rows:
        print("\n" + "=" * 60)
        print("以下 Task 命中 yaml 全局排除字段，已优先跳过：")
        print("=" * 60)
        current_cat = None
        for cat, _tid, line, reason in excluded_rows:
            if cat != current_cat:
                if current_cat is not None:
                    print()
                print(f"  ▌类别: 「{cat}」")
                current_cat = cat
            print(f"      {line}\n        排除命中: {reason}")

    history_dirty = False
    accepted = []
    accepted_targets = set()
    history_auto_accepted = []
    history_auto_rejected = []
    history_changed = []
    for target, rows in matched_by_target.items():
        pending_rows = []
        for row in rows:
            cat, tid, line, item = row
            if history_forced_target(indexed_history, tid) == target:
                decision, history_item = "accepted", indexed_history.get(
                    (str(tid), str(target))
                )
            else:
                decision, history_item = history_decision(
                    indexed_history,
                    tid,
                    target,
                    line,
                )
            if decision == "accepted":
                if target not in accepted_targets:
                    accepted_targets.add(target)
                    accepted.append((f"历史学习:{target}", target, 0))
                history_auto_accepted.append((cat, tid, line, target))
            elif decision == "rejected":
                history_auto_rejected.append((cat, tid, line, target))
            else:
                if decision == "changed":
                    history_changed.append((cat, tid, line, target, history_item))
                pending_rows.append(row)

        if not pending_rows:
            continue

        print("\n" + "-" * 60)
        print(f"以下 {len(pending_rows)} 个新 Task 全行匹配到「{target}」：")
        print("-" * 60)
        current_cat = None
        for cat, _tid, line, item in pending_rows:
            if cat != current_cat:
                if current_cat is not None:
                    print()
                print(f"  ▌类别: 「{cat}」")
                current_cat = cat
            relation = "强关联" if item.get("association") == "strong" else "可复用"
            print(
                f"      {line}\n"
                f"        Task命中: {item.get('task_source') or item.get('source')} -> model:{target} ({relation})"
            )

        while True:
            print(f"\n是否将以上 Task 对应到「{target}」？[y/n，默认 y]: ", end='', flush=True)
            ch = get_choice("y")
            if ch == 'y' or ch == 'Y':
                if target not in accepted_targets:
                    accepted_targets.add(target)
                    accepted.append((f"全行匹配:{target}", target, 0))
                update_h_mode_history(history, pending_rows, target, "accepted")
                history_dirty = True
                print(f"  → 已归类到「{target}」")
                break
            if ch == 'n' or ch == 'N':
                update_h_mode_history(history, pending_rows, target, "rejected")
                history_dirty = True
                print(f"  → 已跳过「{target}」")
                break
            print(f"无效输入: '{ch}'，请输入 y 或 n")

    if history_dirty:
        save_h_mode_history(history)
        print(f"\nh 模式历史学习文件已更新: {H_MODE_HISTORY_PATH}")

    if history_auto_accepted:
        print("\n" + "=" * 60)
        print("历史学习自动归类：")
        print("=" * 60)
        print(f"已按历史自动归类 {len(history_auto_accepted)} 条 Task。")
        for target in sorted({row[3] for row in history_auto_accepted}):
            print(f"  ✓ {target}")

    if history_auto_rejected:
        print("\n" + "=" * 60)
        print("历史学习自动跳过：")
        print("=" * 60)
        print(f"已按历史自动跳过 {len(history_auto_rejected)} 条 Task。")

    if history_changed:
        print("\n" + "=" * 60)
        print("历史中存在行内容变化，已重新询问：")
        print("=" * 60)
        for cat, _tid, line, target, _history_item in history_changed:
            print(f"  ▌类别: 「{cat}」 target: {target}")
            print(f"      {line}")

    if unmatched_rows:
        print("\n" + "=" * 60)
        print("以下 Task 全行都不对应任何映射规则：")
        print("=" * 60)
        current_cat = None
        for cat, _tid, line in unmatched_rows:
            if cat != current_cat:
                if current_cat is not None:
                    print()
                print(f"  ▌类别: 「{cat}」")
                current_cat = cat
            print(f"      {line}")

    if excluded_rows:
        print("\n" + "=" * 60)
        print("全局排除汇总：")
        print("=" * 60)
        print(f"已排除 {len(excluded_rows)} 条 Task，不进入后续流程。")

    print("\n" + "=" * 60)
    print("模式h - 处理结果汇总")
    print("=" * 60)
    if accepted:
        print(f"已归类 {len(accepted)} 个标准类别：")
        for _raw, target, _lineno in accepted:
            print(f"  ✓ {target}")
    else:
        print("没有新增归类。")

    print("\n是否执行？")
    print("  [y] 执行 - 应用上述选择，继续后续流程")
    print("  [n] 返回 - 回到模式选择")
    while True:
        print("\n请输入 [y/n，默认 y]: ", end='', flush=True)
        ch = get_choice("y")
        if ch == 'y' or ch == 'Y':
            return mapping, accepted, [], False
        if ch == 'n' or ch == 'N':
            return handle_invalid_categories(
                invalid_categories,
                mapping,
                mapping_entries,
                mapping_items,
                exclude_substrings,
            )
        print(f"无效输入: '{ch}'，请输入 y 或 n")


# ========== 解析原始类别的主流程 ==========

def parse_categories(data_class_path, mapping_path, config):
    """
    读取原始标注类别清单和类别标准化映射规则，进行匹配浓缩。
    如果遇到无法匹配的类别，调用交互式终端处理。
    
    返回: (nodes: list[str], updated_mapping: dict, skipped: bool)
    """
    # 读取类别标准化映射规则并建立映射表 + 行号列表
    mapping_items = load_category_mapping_rule_items(mapping_path)
    exclude_substrings = load_category_mapping_exclude_substrings(mapping_path)
    mapping_entries = [
        (item["lineno"], item["source"], item["target"])
        for item in mapping_items
    ]
    mapping = mapping_rules_to_first_target_mapping(mapping_entries)
    actual_mapping_path = resolve_mapping_file(work_dir)
    if mapping_entries:
        print(f"读取到映射规则: {len(mapping)} 个关键词 (共 {len(mapping_entries)} 行)")
        for lineno, match_str, output_str in mapping_entries:
            print(f"  [行{lineno:2d}] {match_str} -> {output_str}")
    else:
        print(f"警告: 未找到 {MAPPING_FILE} 文件，路径为 {actual_mapping_path}")
    if exclude_substrings:
        print(f"读取到 h 模式全局排除字段: {len(exclude_substrings)} 个")

    # 优先从标注任务明细的“任务状态=已完成”记录生成类别。
    categories = []
    completed_records = []
    history_resolved_categories = set()
    history_nodes = []
    ask_data_path = os.path.join(work_dir, ASK_DATA_FILE)
    if os.path.exists(ask_data_path):
        with open(ask_data_path, 'r', encoding='utf-8') as f:
            completed_records = flow_records(
                parse_ask_lines(f),
                config,
                exclude_substrings,
            )
        categories = list(
            dict.fromkeys(
                category_key(record)
                for record in completed_records
                if record.category
            )
        )
        print(f"从 {ASK_DATA_FILE} 读取到 {len(categories)} 个任务状态已完成类别")

        history_items = load_h_mode_history().get("items", [])
        records_by_category = {}
        for record in completed_records:
            if record.category:
                records_by_category.setdefault(record.category, []).append(record)

        for category, category_records in records_by_category.items():
            assignments = [
                history_assignment_for_task(
                    record.task_id,
                    history_items,
                    current_line=record.to_line(),
                )
                for record in category_records
            ]
            for assignment in assignments:
                history_nodes.extend(sorted(assignment["accepted"]))
            if assignments and all(assignment["accepted"] for assignment in assignments):
                history_resolved_categories.add(category)
        history_nodes = list(dict.fromkeys(history_nodes))

    if not categories:
        if os.path.exists(data_class_path):
            with open(data_class_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not text_exclude_reason(line, exclude_substrings):
                        categories.append(line)
        else:
            print(f"警告: 未找到 {DATA_CLASS_FILE} 文件，路径为 {data_class_path}")

    # 对每个类别进行匹配浓缩
    nodes = history_nodes
    invalid_categories = []

    for cat in categories:
        if cat in history_resolved_categories:
            print(f"历史学习优先: '{cat}' 下的 Task 已有明确归属，跳过子字符串匹配")
            continue
        matched_rule_items = match_category_mapping_items(
            cat,
            mapping_items,
            ((config or {}).get("model") or {}).get("category_match", {}),
            field="task_source",
            reusable_all=False,
        )
        if matched_rule_items:
            outputs = []
            for item in matched_rule_items:
                match_str = item.get("task_source") or item.get("source", "")
                output_str = item.get("model_target") or item.get("target", "")
                nodes.append(output_str)
                outputs.append(output_str)
            hit_names = "、".join(
                dict.fromkeys(
                    item.get("task_source") or item.get("source", "")
                    for item in matched_rule_items
                )
            )
            output_names = "、".join(dict.fromkeys(outputs))
            print(f"匹配成功: '{cat}' -> '{output_names}' (命中: {hit_names})")
        else:
            invalid_categories.append(cat)
            print(f"匹配失败(无效): '{cat}'")

    # 如果存在无效类别，进入交互式处理
    if invalid_categories:
        updated_mapping, accepted, deleted, skipped = handle_invalid_categories(
            invalid_categories,
            mapping,
            mapping_entries,
            mapping_items,
            exclude_substrings,
        )
        
        if skipped:
            # 用户选择不继续
            return nodes, mapping, True

        # 处理用户选择的归类
        for raw_cat, target_cat, _lineno in accepted:
            nodes.append(target_cat)
            print(f"用户归类: '{raw_cat}' -> '{target_cat}'")

        # 处理已删除的类别：不加入 nodes
        for del_cat in deleted:
            print(f"用户删除: '{del_cat}' (不进入后续归纳)")

        mapping = updated_mapping
    else:
        print("所有类别匹配成功，无需交互式处理。")

    # 去重并保持顺序
    nodes = list(dict.fromkeys(nodes))

    print("\n" + "="*60)
    print("最终有效类别:")
    print("="*60)
    for i, node in enumerate(nodes, 1):
        print(f"  {i}. {node}")

    return nodes, mapping, False


def first_displayed(elements):
    for element in elements:
        try:
            if element.states.is_displayed and element.states.has_rect:
                return element
        except Exception:
            continue
    return None


def displayed_elements(elements):
    visible = []
    for element in elements:
        try:
            if element.states.is_displayed and element.states.has_rect:
                visible.append(element)
        except Exception:
            continue
    return visible


def training_table_is_loading():
    try:
        return bool(
            first_displayed(tab.eles('css:div.ant-spin-spinning', timeout=0.1))
            or first_displayed(tab.eles('css:div.ant-spin-blur', timeout=0.1))
        )
    except Exception:
        return False


def wait_for_training_table(timeout=15):
    end_time = time.time() + timeout
    while time.time() < end_time:
        try:
            if training_table_is_loading():
                time.sleep(0.2)
                continue
            if displayed_elements(tab.eles('css:tr.ant-table-row.ant-table-row-level-0', timeout=0.2)):
                return True
            if first_displayed(tab.eles('css:div.ant-empty', timeout=0.2)):
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def training_table_signature():
    try:
        return "|".join(
            row.attr("data-row-key") or row.text
            for row in tab.eles('css:tr.ant-table-row.ant-table-row-level-0', timeout=0.2)
        )
    except Exception:
        return ""


def wait_for_training_table_refresh(previous_signature="", timeout=10):
    end_time = time.time() + timeout
    while time.time() < end_time:
        if training_table_is_loading():
            time.sleep(0.2)
            continue
        signature = training_table_signature()
        if signature != previous_signature or first_displayed(tab.eles('css:div.ant-empty', timeout=0.1)):
            return True
        time.sleep(0.2)
    return False


def training_note_filter_trigger():
    for header in training_note_header_candidates():
        triggers = (
            header.eles('css:span.ant-table-filter-trigger', timeout=0.2)
            + header.eles(
                'xpath:.//*[contains(@class,"ant-table-filter-trigger") '
                'or (@role="button" and .//*[@aria-label="search"])]',
                timeout=0.2,
            )
        )
        trigger = first_displayed(triggers)
        if trigger:
            return trigger
    triggers = training_table_filter_triggers()
    return triggers[-1] if triggers else None


def training_note_header_candidates():
    candidates = []
    for header_row in tab.eles('css:thead.ant-table-thead tr', timeout=1):
        try:
            if header_row.attr('aria-hidden') == 'true':
                continue
            headers = header_row.children()
        except Exception:
            continue
        if len(headers) >= 11:
            candidates.append(headers[-2])
            candidates.append(headers[9])
    return candidates


def training_table_filter_triggers():
    triggers = []
    for header_row in tab.eles('css:thead.ant-table-thead tr', timeout=1):
        try:
            if header_row.attr('aria-hidden') == 'true':
                continue
            row_triggers = header_row.eles('css:span.ant-table-filter-trigger', timeout=0.2)
        except Exception:
            continue
        triggers.extend(displayed_elements(row_triggers))
    return triggers


def model_filter_input():
    inputs = []
    for dropdown in displayed_elements(tab.eles('css:div.ant-table-filter-dropdown', timeout=0.3)):
        try:
            inputs.extend(dropdown.eles('css:input', timeout=0.2))
        except Exception:
            continue
    visible_inputs = displayed_elements(inputs)
    return visible_inputs[0] if visible_inputs else None


def model_filter_confirm_button():
    buttons = []
    for dropdown in displayed_elements(tab.eles('css:div.ant-table-filter-dropdown', timeout=0.3)):
        try:
            buttons.extend(dropdown.eles('css:button', timeout=0.2))
        except Exception:
            continue
    for button in displayed_elements(buttons or tab.eles('css:button', timeout=0.3)):
        text = button.text.replace(" ", "")
        if text in ("搜索", "确定"):
            return button
    return None


def apply_training_note_filter(node):
    trigger = training_note_filter_trigger()
    if trigger:
        try:
            trigger.click(by_js=None, timeout=2)
        except Exception:
            trigger.click(by_js=True, timeout=2)
        tab.wait(0.2)

    search_input = None
    for _ in range(10):
        search_input = model_filter_input()
        if search_input:
            break
        if not trigger:
            break
        tab.wait(0.2)
    if not search_input:
        raise RuntimeError("training note filter input was not found")
    previous_signature = training_table_signature()
    search_input.click()
    search_input.input(node, clear=True)
    try:
        search_input.run_js(
            "this.dispatchEvent(new Event('input', {bubbles: true}));"
            "this.dispatchEvent(new Event('change', {bubbles: true}));"
        )
    except Exception:
        pass

    confirm = model_filter_confirm_button()
    if confirm:
        confirm.click(by_js=None, timeout=2)
    else:
        search_input.input('\n')
    wait_for_training_table_refresh(previous_signature, timeout=10)


MODEL_ALLOWED_STATUSES = {"成功", "执行完成", "运行中"}
TASK_REF_RE = re.compile(r'\b(?:autoedge|cvat)@\d+\b', re.IGNORECASE)


def model_column_index(column_title):
    for header_row in displayed_elements(tab.eles('css:thead.ant-table-thead tr', timeout=0.5)):
        for index, header in enumerate(header_row.children()):
            try:
                title_element = first_displayed(
                    header.eles('css:span.ant-table-column-title', timeout=0.1)
                )
                text = title_element.text if title_element else header.text
                if " ".join((text or "").split()) == column_title:
                    return index
            except Exception:
                continue
    return None


def text_from_model_row_column(row, column_title, fallback_index):
    cols = row.children()
    index = model_column_index(column_title)
    if index is not None and index < len(cols):
        return cols[index].text.strip()
    if len(cols) >= abs(fallback_index):
        return cols[fallback_index].text.strip()
    return ""


def note_from_model_row(row):
    return text_from_model_row_column(row, "训练备注", -2)


def end_time_from_model_row(row):
    return text_from_model_row_column(row, "结束时间", -3)


def start_time_from_model_row(row):
    for column_title in ("开始时间", "训练开始时间", "创建时间"):
        index = model_column_index(column_title)
        cols = row.children()
        if index is not None and index < len(cols):
            value = cols[index].text.strip()
            if value:
                return value
    return text_from_model_row_column(row, "开始时间", -4)


def status_from_model_row(row):
    return text_from_model_row_column(row, "状态", -6)


def model_status_is_allowed(status):
    return " ".join((status or "").split()) in MODEL_ALLOWED_STATUSES


def parse_datetime_value(value):
    value = (value or "").strip()
    if not value or value == "-":
        return datetime.min
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return datetime.min


def task_refs_from_dataset(content):
    """Return ordered task refs such as autoedge@123 from a dataset field."""
    return [match.group(0).lower() for match in TASK_REF_RE.finditer(content or "")]


def merge_dataset_contents(base_content, other_contents):
    refs = []
    seen = set()
    for content in [base_content] + list(other_contents):
        for ref in task_refs_from_dataset(content):
            if ref not in seen:
                seen.add(ref)
                refs.append(ref)
    return ",".join(refs)


def serialize_fill_sources(fill_sources):
    parts = []
    for note, start_time, refs in fill_sources:
        label = f"{note}[{start_time}]" if start_time else note
        parts.append(f"{label}={','.join(refs)}")
    return ";".join(parts)


def detail_button_from_model_row(row):
    for button in row.eles('xpath:.//button[.//span[normalize-space(text())="查看详情"]]', timeout=0.2):
        if first_displayed([button]):
            return button
    for link in row.eles('xpath:.//*[normalize-space(text())="查看详情"]', timeout=0.2):
        if first_displayed([link]):
            return link
    return None


def read_training_dataset_from_detail():
    label = tab.ele('xpath://*[normalize-space(text())="训练数据集"]', timeout=8)
    if not label:
        raise RuntimeError("详情页未找到训练数据集")
    candidates = [
        'xpath:./ancestor::*[contains(@class,"ant-form-item")][1]//*[contains(@class,"ant-form-item-control")]',
        'xpath:./ancestor::*[contains(@class,"ant-descriptions-item")][1]//*[contains(@class,"ant-descriptions-item-content")]',
    ]
    for locator in candidates:
        try:
            content_ele = label.ele(locator, timeout=0.5)
            if content_ele and content_ele.text.strip():
                return content_ele.text.strip()
        except Exception:
            pass
    try:
        return label.parent().parent().next().children()[0].children()[0].text.strip()
    except Exception as exc:
        raise RuntimeError("无法读取训练数据集内容") from exc


def close_or_return_to_training_list():
    for locator in (
        'css:button.ant-drawer-close',
        'css:button.ant-modal-close',
        'xpath://button[.//span[@aria-label="close"]]',
        'xpath://button[normalize-space(.)="返回"]',
    ):
        close_button = first_displayed(tab.eles(locator, timeout=0.2))
        if close_button:
            close_button.click(by_js=None, timeout=1)
            tab.wait(0.3)
            return
    try:
        tab.back()
        wait_for_training_table(timeout=5)
    except Exception:
        tab.get(TRAINING_LIST_URL)
        wait_for_training_table(timeout=10)


def fetch_complete_model_result(node, config):
    wait_for_training_table(timeout=10)
    rows = displayed_elements(tab.eles('css:tr.ant-table-row.ant-table-row-level-0', timeout=1))
    # 按网页顺序扫描；跳过的行不计入有效行数。
    max_rows = training_note_max_rows(config)

    valid_rows = []
    for row_index, row in enumerate(rows, start=1):
        if len(valid_rows) >= max_rows:
            break

        status = status_from_model_row(row)
        if not model_status_is_allowed(status):
            print(f"第 {row_index} 行状态不是成功/运行中，跳过: {status or '-'}")
            continue

        note = note_from_model_row(row)
        if training_note_is_blocked(note, config):
            print(f"第 {row_index} 行训练备注命中黑名单，跳过: {note}")
            continue

        start_time = start_time_from_model_row(row)
        end_time = end_time_from_model_row(row)
        valid_rows.append((row_index, row, note, start_time, end_time, status))

    row_results = []
    for row_index, row, note, start_time, end_time, status in valid_rows:
        detail_button = detail_button_from_model_row(row)
        if not detail_button:
            print(f"类别 [{node}] 第 {row_index} 行未找到查看详情按钮，继续下一行")
            continue

        print(
            f"类别 [{node}] 扫描第 {row_index} 行，状态: {status or '-'}，"
            f"开始时间: {start_time or '-'}，结束时间: {end_time or '-'}，训练备注: {note}"
        )
        detail_button.click(by_js=None, timeout=2)
        tab.wait(0.5)
        try:
            content = read_training_dataset_from_detail()
            content = remove_excluded_task_refs(
                content,
                current_excluded_task_ids(),
            )
        finally:
            close_or_return_to_training_list()

        if not extract_task_dataset_ids(content):
            print(f"类别 [{node}] 第 {row_index} 行训练数据集无 Task ID，继续下一行")
            continue
        row_results.append((row_index, note, start_time, end_time, status, content))
        if max_rows == 1:
            return (
                note,
                content,
                (start_time or end_time),
                [],
            )

    if not row_results:
        return None, None, None, []

    base_row = row_results[0]
    merged_content = merge_dataset_contents(base_row[5], [item[5] for item in row_results[1:]])
    base_refs_count = len(task_refs_from_dataset(base_row[5]))
    merged_refs_count = len(task_refs_from_dataset(merged_content))
    added_count = merged_refs_count - base_refs_count
    seen_refs = set(task_refs_from_dataset(base_row[5]))
    fill_sources = []
    for _row_index, note, start_time, _end_time, _status, content in row_results[1:]:
        fill_refs = []
        for ref in task_refs_from_dataset(content):
            if ref in seen_refs:
                continue
            seen_refs.add(ref)
            fill_refs.append(ref)
        if fill_refs:
            fill_sources.append((note, start_time or "", fill_refs))
    print(
        f"类别 [{node}] 采用第 {base_row[0]} 行作为最新开始时间模板，"
        f"补齐 {max(added_count, 0)} 个 Task，最终 {merged_refs_count} 个 Task"
    )
    return base_row[1], merged_content, (base_row[2] or base_row[3]), fill_sources


# ========== 主程序 ==========
def fetch_complete_model_result_after_search(node, config, timeout=10):
    end_time = time.time() + timeout
    last_result = (None, None, None, [])
    while time.time() < end_time:
        wait_for_training_table(timeout=1)
        result = fetch_complete_model_result(node, config)
        if result[1]:
            return result
        last_result = result
        if first_displayed(tab.eles('css:div.ant-empty', timeout=0.2)):
            break
        time.sleep(0.5)
    print(f"Category [{node}] search had no usable model record after {timeout}s; skipped.")
    return last_result



# ---------- 7. 连接到现有浏览器 ----------
browser = None
try:
    print(f"连接到浏览器 127.0.0.1:{DEBUG_PORT}...")
    browser = Chromium(f'127.0.0.1:{DEBUG_PORT}')
    print("浏览器连接成功")

    tab = browser.new_tab()
    minimize_browser_window(tab)

    # ---------- 解析类别（含交互式处理）----------
    data_class_path = os.path.join(work_dir, DATA_CLASS_FILE)
    mapping_path = str(resolve_mapping_file(work_dir))

    nodes, mapping, skipped = parse_categories(data_class_path, mapping_path, CONFIG)

    if skipped:
        print("用户选择退出，程序结束。")
        sys.exit(0)

    if not nodes:
        print("没有有效类别，程序结束。")
        sys.exit(0)

    # ========== 生成标准训练类别清单 ==========
    class_out_path = os.path.join(work_dir, CLASS_OUT_FILE)
    with open(class_out_path, 'w', encoding='utf-8') as f:
        for node in nodes:
            f.write(node + '\n')
    print(f"\n已生成有效类别文件: {class_out_path}")
    # ============================================

    print("\n按 Enter 键开始爬取 model 数据...")
    wait_for_enter()

    # 打开训练任务页面
    tab.get(TRAINING_LIST_URL)
    minimize_browser_window(tab)

    file_path = os.path.join(work_dir, MODEL_FILE)

    # 清空模型训练记录
    with open(file_path, 'w', encoding='utf-8') as f:
        pass

    failed_nodes = []

    # 循环处理每个节点
    for node in nodes:
        try:
            tab.get(TRAINING_LIST_URL)
            minimize_browser_window(tab)
            wait_for_training_table(timeout=15)
            apply_training_note_filter(node)
            test_02, content, start_time, fill_sources = fetch_complete_model_result_after_search(node, CONFIG, timeout=10)
            if not content:
                failed_nodes.append(node)
                continue
            content_01 = latest_task_summary(content)
            with open(file_path, 'a', encoding='utf-8') as f:
                f.write(
                    "最新Task  "
                    + content_01
                    + "  备注 "
                    + test_02
                    + "  训练开始时间 "
                    + (start_time or "")
                    + (
                        "  补齐Task来源:"
                        + serialize_fill_sources(fill_sources)
                        if fill_sources
                        else ""
                    )
                    + "  完整Task:"
                    + content
                    + '\n'
                )

        except Exception as e:
            print(f"处理类别 [{node}] 时出错: {e}")
            traceback.print_exc()
            failed_nodes.append(node)
            # 尝试恢复页面
            try:
                tab.get(TRAINING_LIST_URL)
                minimize_browser_window(tab)
            except:
                pass
            continue

    if failed_nodes:
        print("\n以下类别处理失败：")
        for n in failed_nodes:
            print(f"  - {n}")
    else:
        print("所有类别处理成功。")

except Exception as e:
    print(f"主流程出错: {e}")
    traceback.print_exc()

finally:
    if browser is not None:
        try:
            if browser_started_by_script:
                browser.quit()
                print("已关闭本流程启动的专用浏览器")
            elif tab is not None:
                tab.close()
                print("标签页已关闭，保留已存在的自动化浏览器")
        except Exception:
            pass
    print("清理完成")
