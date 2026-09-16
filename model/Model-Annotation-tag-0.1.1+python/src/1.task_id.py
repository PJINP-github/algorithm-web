import sys
import os
import atexit
import platform
import time
import shutil
import subprocess
import re
if platform.system() == 'Windows':
    import msvcrt
else:
    import tty
    import termios
from DrissionPage import Chromium, ChromiumOptions
from DrissionPage.common import Keys, Settings
from ask_data_utils import (
    ASK_DATA_FILE,
    ASK_DATA_T2_FILE,
    ASK_DATA_TEMP_FILE,
    DATA_CLASS_FILE,
    build_record,
    category_key,
    flow_records as get_flow_records,
    load_category_mapping_exclude_substrings,
    load_config,
    missing_task_ids,
    prioritize_missing_task_ids,
    record_exclude_reason,
    record_matches_task_filters,
    resolve_chrome_path,
    resolve_mapping_file,
    resolve_work_dir,
    sort_unique_records,
)

Settings.set_language('en')


def portal_automated():
    return (
        os.environ.get('RUST_PORTAL_AUTOMATED') == '1'
        or os.environ.get('RUST_PORTAL_HEADLESS') == '1'
    )


browser = None
tab = None
browser_started_by_script = False


def cleanup_browser():
    """Only close a browser instance started by this automation process."""
    if browser_started_by_script and browser is not None:
        try:
            browser.quit()
            print("已关闭本流程启动的专用浏览器")
        except Exception as exc:
            print(f"关闭本流程专用浏览器失败: {exc}")
        return
    if tab is not None:
        try:
            tab.close()
            print("标签页已关闭，保留已存在的自动化浏览器")
        except Exception:
            pass


atexit.register(cleanup_browser)

# ---------- 1. 确定项目目录 ----------
if getattr(sys, 'frozen', False):
    base_dir = os.path.dirname(sys.executable)
else:
    base_dir = os.path.dirname(os.path.abspath(__file__))

work_dir = str(resolve_work_dir(base_dir))
os.makedirs(work_dir, exist_ok=True)

# ---------- 2. 查找 Chrome 可执行文件 ----------
chrome_path = resolve_chrome_path(base_dir)
print(f"使用浏览器: {chrome_path}")

# ---------- 3. 浏览器用户数据目录（固定目录，保存登录状态）----------
user_data_dir = os.environ.get('RUST_PORTAL_USER_DATA_DIR', '').strip() or os.path.join(base_dir, '..', 'chromium_user_data')
user_data_dir = os.path.abspath(user_data_dir)
os.makedirs(user_data_dir, exist_ok=True)
print(f"用户数据目录: {user_data_dir}")

# ---------- 4. 调试端口 ----------
DEBUG_PORT = int(os.environ.get('RUST_PORTAL_DEBUG_PORT', '9222'))

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
    
    log_file = os.path.join(base_dir, 'chrome_startup.log')
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

# ---------- 7. 连接到现有浏览器并新建标签页 ----------
print(f"连接到浏览器 127.0.0.1:{DEBUG_PORT}...")
browser = Chromium(f'127.0.0.1:{DEBUG_PORT}')
print("浏览器连接成功")

tab = browser.new_tab()
print("已新建标签页")
minimize_browser_window(tab)

# 打开目标页面
tab.get('http://autoedge.jiangxingai.com/algorithm/collect/tasks')
minimize_browser_window(tab)

# ---------- 5. 页面和数据处理 ----------
ask_data_path = os.path.join(work_dir, ASK_DATA_FILE)
ask_data_temp_path = os.path.join(work_dir, ASK_DATA_TEMP_FILE)
ask_data_t2_path = os.path.join(work_dir, ASK_DATA_T2_FILE)
data_class_path = os.path.join(work_dir, DATA_CLASS_FILE)
MISSING_NEIGHBOR_THRESHOLD = 15
MISSING_TASK_SEARCH_TIMEOUT = 0.7
MISSING_TASK_RESET_TIMEOUT = 0.8
CONFIG = load_config()
MAPPING_EXCLUDE_SUBSTRINGS = load_category_mapping_exclude_substrings(
    resolve_mapping_file(work_dir)
)
ignored_task_ids = set()
task_id_search_input_cache = None
image_count_column_index = None
image_count_column_checked = False

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


def task_table_column_index(column_titles):
    normalized_titles = {
        " ".join(str(title).split())
        for title in column_titles
    }
    for header_row in displayed_elements(
        tab.eles('css:thead.ant-table-thead tr', timeout=0.5)
    ):
        for index, header in enumerate(header_row.children()):
            try:
                title_element = first_displayed(
                    header.eles('css:span.ant-table-column-title', timeout=0.1)
                )
                text = title_element.text if title_element else header.text
                if " ".join((text or "").split()) in normalized_titles:
                    return index
            except Exception:
                continue
    return None


def task_row_image_count(row):
    global image_count_column_index, image_count_column_checked
    if not image_count_column_checked:
        image_count_column_index = task_table_column_index(
            ("图片数量", "图片数", "图像数量")
        )
        image_count_column_checked = True
    image_count_index = image_count_column_index
    if image_count_index is None:
        return 0
    cols = row.children()
    if image_count_index >= len(cols):
        return 0
    match = re.search(r"\d[\d,]*", cols[image_count_index].text or "")
    if not match:
        return 0
    try:
        return int(match.group().replace(",", ""))
    except ValueError:
        return 0


def wait_for_page_ready(timeout=15):
    end_time = time.time() + timeout
    while time.time() < end_time:
        try:
            rows_ready = bool(tab.eles('css:tr.ant-table-row.ant-table-row-level-0', timeout=0.2))
            size_ready = bool(tab.eles('css:div.ant-pagination-options-size-changer', timeout=0.2))
            if rows_ready or size_ready:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def table_signature():
    try:
        return "|".join(
            row.attr("data-row-key") or row.text
            for row in tab.eles('css:tr.ant-table-row.ant-table-row-level-0', timeout=0.2)
        )
    except Exception:
        return ""


def table_is_loading():
    try:
        for spinner in tab.eles('css:div.ant-spin-spinning', timeout=0):
            if spinner.states.is_displayed and spinner.states.has_rect:
                return True
    except Exception:
        pass
    return False


def wait_for_table_refresh(previous_signature="", timeout=10):
    end_time = time.time() + timeout
    while time.time() < end_time:
        if table_is_loading():
            time.sleep(0.15)
            continue
        signature = table_signature()
        if signature != previous_signature or first_displayed(tab.eles('css:div.ant-empty', timeout=0.1)):
            return True
        time.sleep(0.2)
    return False


def page_has_task_id(task_id):
    try:
        rows = tab.eles(
            f'xpath://tr[contains(@class,"ant-table-row")][./td[4][normalize-space(text())="{task_id}"]]',
            timeout=0,
        )
        if rows:
            return True
        for row in tab.eles('css:tr.ant-table-row.ant-table-row-level-0', timeout=0):
            cols = row.children()
            if len(cols) >= 4 and cols[3].text.strip() == str(task_id):
                return True
    except Exception:
        pass
    return False


def page_has_empty_result():
    return bool(first_displayed(tab.eles('css:div.ant-empty', timeout=0)))


def wait_for_task_search_result(task_id, timeout=MISSING_TASK_SEARCH_TIMEOUT):
    """短轮询等待本次 Task ID 搜索结果，没结果时快速返回。"""
    end_time = time.time() + timeout
    while time.time() < end_time:
        if table_is_loading():
            time.sleep(0.05)
            continue
        if page_has_task_id(task_id) or page_has_empty_result():
            return True
        time.sleep(0.05)
    return False


def wait_for_unfiltered_table(timeout=MISSING_TASK_RESET_TIMEOUT):
    end_time = time.time() + timeout
    while time.time() < end_time:
        if table_is_loading():
            time.sleep(0.05)
            continue
        try:
            rows = tab.eles('css:tr.ant-table-row.ant-table-row-level-0', timeout=0)
            if rows or page_has_empty_result():
                return True
        except Exception:
            pass
        time.sleep(0.05)
    return False


def select_page_size_100():
    """将分页大小切换为 100 条/页。"""
    wait_for_page_ready()
    selectors = (
        'css:div.ant-pagination-options-size-changer',
        '@aria-label=页码',
    )
    changer = None
    for selector in selectors:
        changer = first_displayed(tab.eles(selector))
        if changer:
            break
    if not changer:
        raise RuntimeError("未找到分页大小选择器")

    current = changer.ele('css:span.ant-select-selection-item')
    if current and '100 条/页' in current.text:
        return

    changer.click()
    option = None
    for selector in ('text=100 条/页', 'css:div.ant-select-item-option'):
        candidates = tab.eles(selector)
        option = first_displayed(
            [item for item in candidates if '100 条/页' in item.text]
        )
        if option:
            break
    if not option:
        raise RuntimeError("分页选项中未找到 100 条/页")
    option.click()
    tab.wait(0.8)


def task_status_filter_trigger():
    """Return the filter trigger for the 任务状态 table column."""
    headers = displayed_elements(tab.eles(
        'xpath://thead//th[.//span[contains(@class,"ant-table-column-title") '
        'and normalize-space(text())="任务状态"]]',
        timeout=1,
    ))
    for header in headers:
        try:
            triggers = (
                header.eles('css:span.ant-table-filter-trigger', timeout=0.2)
                + header.eles(
                    'xpath:.//*[contains(@class,"ant-table-filter-trigger") '
                    'or (@role="button" and .//*[@aria-label="filter"])]',
                    timeout=0.2,
                )
            )
            trigger = first_displayed(triggers)
            if trigger:
                return trigger
        except Exception:
            continue
    return None


def task_status_filter_dropdown():
    return first_displayed(
        displayed_elements(
            tab.eles('css:div.ant-table-filter-dropdown', timeout=0.3)
        )
    )


def task_status_filter_option():
    """Find the visible exact 已完成 option from the opened status selector."""
    candidates = []
    for selector in (
        'css:div.ant-select-item-option',
        'css:li[role="option"]',
        'text=已完成',
    ):
        try:
            candidates.extend(tab.eles(selector, timeout=0.2))
        except Exception:
            continue
    for candidate in displayed_elements(candidates):
        try:
            if " ".join((candidate.text or "").split()) == "已完成":
                return candidate
        except Exception:
            continue
    return None


def task_status_filter_option_selected(option):
    """Return whether Ant Design already selected the status option."""
    try:
        if (option.attr("aria-selected") or "").casefold() == "true":
            return True
        classes = option.attr("class") or ""
        return "ant-select-item-option-selected" in classes
    except Exception:
        return False


def task_status_filter_input(dropdown):
    """Find the status Select combobox, including Ant Design's hidden input."""
    if not dropdown:
        return None
    roots = [dropdown]
    selectors = (
        'css:input[role="combobox"]',
        'css:input.ant-select-selection-search-input',
    )
    for root in roots:
        for selector in selectors:
            try:
                candidates = root.eles(selector, timeout=0.2)
            except Exception:
                continue
            # This input can be opacity:0 and width:0, so do not require a
            # visible rectangle. It is still the controlled React input.
            if candidates:
                return candidates[0]
    return None


def set_status_filter_input_value(search_input, value):
    """Set the Ant Design status search value through React's native setter."""
    script = """
const value = arguments[0];
const input = this;
const proto = Object.getPrototypeOf(input);
const descriptor = Object.getOwnPropertyDescriptor(proto, 'value');
if (descriptor && descriptor.set) {
  descriptor.set.call(input, value);
} else {
  input.value = value;
}
input.dispatchEvent(new Event('input', {bubbles: true}));
input.dispatchEvent(new Event('change', {bubbles: true}));
"""
    search_input.run_js(script, str(value))


def press_status_filter_enter(search_input):
    """Trigger Enter on the status combobox without relying on element size."""
    script = """
const options = {
  key: 'Enter',
  code: 'Enter',
  keyCode: 13,
  which: 13,
  bubbles: true,
  cancelable: true
};
this.dispatchEvent(new KeyboardEvent('keydown', options));
this.dispatchEvent(new KeyboardEvent('keypress', options));
this.dispatchEvent(new KeyboardEvent('keyup', options));
"""
    search_input.run_js(script)


def task_status_filter_confirm_button(dropdown):
    roots = [dropdown] if dropdown else []
    roots.append(tab)
    for root in roots:
        try:
            buttons = root.eles('css:button', timeout=0.2)
        except Exception:
            continue
        for button in buttons:
            try:
                if not button.states.is_displayed or not button.states.has_rect:
                    continue
                text = "".join((button.text or "").split())
                if text in ("确定", "搜索"):
                    return button
            except Exception:
                continue
    return None


def table_contains_only_completed_tasks():
    """Verify that the visible task rows all have status 已完成."""
    rows = tab.eles(
        'css:tr.ant-table-row.ant-table-row-level-0',
        timeout=0.2,
    )
    if not rows:
        return page_has_empty_result()
    for row in rows:
        try:
            cols = row.children()
            if len(cols) < 8:
                return False
            status = " ".join((cols[7].text or "").split())
            if status != "已完成":
                return False
        except Exception:
            return False
    return True


def apply_completed_task_status_filter():
    """Filter the task table to exact status 已完成."""
    trigger = task_status_filter_trigger()
    if not trigger:
        raise RuntimeError("未找到任务状态筛选按钮")

    previous_signature = table_signature()
    trigger.click(by_js=None, timeout=1)
    tab.wait(0.2)
    dropdown = task_status_filter_dropdown()
    selector = None
    if dropdown:
        selector = first_displayed(
            dropdown.eles('css:div.ant-select-selector', timeout=0.2)
        )
    if not selector:
        selector = first_displayed(tab.eles('css:div.ant-select-selector', timeout=0.2))
    if not selector:
        raise RuntimeError("未找到任务状态选择器")

    selector.click(by_js=None, timeout=1)
    tab.wait(0.2)

    # 先按搜索框方式写入“已完成”并回车。部分版本会在 Enter 时直接
    # 选中唯一匹配项，部分版本只过滤选项，因此下面仍保留精确点击回退。
    status_input = task_status_filter_input(dropdown)
    if status_input:
        try:
            set_status_filter_input_value(status_input, "已完成")
            press_status_filter_enter(status_input)
        except Exception:
            try:
                status_input.input("已完成\n")
            except Exception:
                pass
        tab.wait(0.2)

    option = task_status_filter_option()
    if option and not task_status_filter_option_selected(option):
        option.click(by_js=None, timeout=1)
        tab.wait(0.1)

    confirm = task_status_filter_confirm_button(dropdown)
    if confirm:
        confirm.click(by_js=None, timeout=1)
    else:
        # Some Ant Design versions apply the selection when the option is chosen.
        # Pressing Enter is only a fallback for versions with a searchable select.
        try:
            selector.input("\n")
        except Exception:
            pass

    wait_for_table_refresh(previous_signature, timeout=10)
    if not table_contains_only_completed_tasks():
        raise RuntimeError("任务状态筛选未生效，当前表格仍包含非已完成任务")
    print("已自动筛选任务状态: 已完成")


def pagination_page_item(page_number):
    page_number = str(page_number)
    selectors = (
        f'css:ul.ant-pagination li[title="{page_number}"]',
        f'xpath://ul[contains(@class,"ant-pagination")]//li[@title="{page_number}"]',
    )
    for selector in selectors:
        item = first_displayed(tab.eles(selector, timeout=0.3))
        if item:
            return item
    return None


def pagination_quick_jumper_input():
    """Return the table pagination's 跳至 input."""
    selectors = (
        'css:ul.ant-pagination div.ant-pagination-options-quick-jumper input',
        'xpath://ul[contains(@class,"ant-pagination")]'
        '//div[contains(@class,"ant-pagination-options-quick-jumper")]//input',
    )
    for selector in selectors:
        item = first_displayed(tab.eles(selector, timeout=0.3))
        if item:
            return item
    return None


def jump_to_page_with_quick_jumper(page_number):
    """Use Ant Design's quick jumper for pages not currently visible."""
    jumper = pagination_quick_jumper_input()
    if not jumper:
        return False

    previous_signature = table_signature()
    try:
        set_filter_input_value(jumper, str(page_number))
        press_filter_enter(jumper)
    except Exception:
        try:
            jumper.click(by_js=True, timeout=0)
            jumper.input(Keys.CTRL_A)
            jumper.input(Keys.DELETE)
            jumper.input(str(page_number))
            jumper.input("\n")
        except Exception:
            return False

    if not wait_for_table_refresh(previous_signature, timeout=10):
        return False
    return current_pagination_page() in ("", str(page_number))


def pagination_jump_next():
    selectors = (
        'css:ul.ant-pagination li.ant-pagination-jump-next',
        'xpath://ul[contains(@class,"ant-pagination")]'
        '//li[contains(@class,"ant-pagination-jump-next")]',
    )
    for selector in selectors:
        item = first_displayed(tab.eles(selector, timeout=0.3))
        if item:
            return item
    return None


def current_pagination_page():
    item = first_displayed(
        tab.eles('css:ul.ant-pagination li.ant-pagination-item-active', timeout=0.2)
    )
    if not item:
        return ""
    return item.attr("title") or "" 


def go_to_page(page_number):
    page_number = str(page_number)
    if current_pagination_page() == page_number:
        return True
    item = pagination_page_item(page_number)
    if item:
        previous_signature = table_signature()
        item.click(by_js=None, timeout=1)
        if not wait_for_table_refresh(previous_signature, timeout=10):
            raise RuntimeError(f"切换到第 {page_number} 页后表格未刷新")
    elif not jump_to_page_with_quick_jumper(page_number):
        # 某些页面没有可用的 quick jumper，逐次点击“向后 5 页”直到目标页可见。
        while not pagination_page_item(page_number):
            jump_next = pagination_jump_next()
            if not jump_next:
                return False
            previous_signature = table_signature()
            jump_next.click(by_js=None, timeout=1)
            if not wait_for_table_refresh(previous_signature, timeout=10):
                raise RuntimeError(f"跳转分页后表格未刷新，目标页为 {page_number}")
            if current_pagination_page() == page_number:
                break
        item = pagination_page_item(page_number)
        if not item and current_pagination_page() != page_number:
            return False
        if item and current_pagination_page() != page_number:
            previous_signature = table_signature()
            item.click(by_js=None, timeout=1)
            if not wait_for_table_refresh(previous_signature, timeout=10):
                raise RuntimeError(f"切换到第 {page_number} 页后表格未刷新")
    if current_pagination_page() not in ("", page_number):
        raise RuntimeError(f"分页切换失败，当前页为 {current_pagination_page()}")
    return True


def configured_max_collection_pages():
    """Read and validate the automatic collection page limit."""
    collection_config = (CONFIG or {}).get("collection", {})
    raw_value = collection_config.get("max_pages", 5)
    try:
        max_pages = int(raw_value)
    except (TypeError, ValueError):
        raise ValueError(
            "config.yaml 的 collection.max_pages 必须是大于 1 的整数"
        )
    if max_pages <= 1:
        raise ValueError(
            "config.yaml 的 collection.max_pages 必须是大于 1 的整数"
        )
    return max_pages


def collect_first_pages(max_pages):
    """Collect every visible task row from pages 1 through max_pages."""
    all_records = []
    for page_number in range(1, max_pages + 1):
        if not go_to_page(page_number):
            print(f"未找到第 {page_number} 页，已结束采集")
            break
        records = collect_page_data()
        all_records.extend(records)
        print(f"第 {page_number} 页采集完成: {len(records)} 条")
    return all_records


def collect_page_data(task_id_filter=None):
    """读取当前表格中的任务记录，task_id_filter 用于搜索补录。"""
    records = []
    try:
        rows = tab.eles('css:tr.ant-table-row.ant-table-row-level-0', timeout=0.2)
        for row in rows:
            try:
                cols = row.children()
                if len(cols) < 12:
                    continue
                task_id = cols[3].text.strip()
                if task_id_filter and task_id != str(task_id_filter):
                    continue
                record = build_record(
                    task_name=cols[1].text,
                    task_id=task_id,
                    task_status=cols[7].text,
                    pre_label_status=cols[8].text,
                    note=cols[11].text,
                    image_count=task_row_image_count(row),
                )
                if record.task_id.isdigit():
                    if record_matches_task_filters(record, CONFIG):
                        ignored_task_ids.add(record.task_id)
                        print(f"按 config.yaml 过滤 Task {record.task_id}")
                        continue
                    exclude_reason = record_exclude_reason(
                        record,
                        MAPPING_EXCLUDE_SUBSTRINGS,
                    )
                    if exclude_reason:
                        ignored_task_ids.add(record.task_id)
                        print(
                            f"按映射 YAML 全局排除字段过滤 Task "
                            f"{record.task_id}: {exclude_reason}"
                        )
                        continue
                    records.append(record)
            except Exception as exc:
                print(f"解析行数据时出错: {exc}")
    except Exception as exc:
        print(f"获取表格数据时出错: {exc}")
    return records


def task_id_filter_trigger():
    headers = displayed_elements(tab.eles(
        'xpath://thead//th[.//span[contains(@class,"ant-table-column-title") '
        'and normalize-space(text())="Task ID"]]',
        timeout=1,
    ))
    for header in headers:
        try:
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
        except Exception:
            continue
    return None


def task_id_filter_input(use_cache=True):
    global task_id_search_input_cache
    if use_cache and task_id_search_input_cache:
        try:
            if task_id_search_input_cache.states.is_displayed and task_id_search_input_cache.states.has_rect:
                return task_id_search_input_cache
        except Exception:
            task_id_search_input_cache = None

    dropdown_inputs = []
    for dropdown_selector in (
        'css:div.ant-table-filter-dropdown',
        'css:div.ant-dropdown',
    ):
        for dropdown in displayed_elements(tab.eles(dropdown_selector, timeout=0.3)):
            try:
                dropdown_inputs.extend(dropdown.eles('css:input', timeout=0.2))
            except Exception:
                continue
    visible_inputs = displayed_elements(dropdown_inputs)
    for element in visible_inputs:
        placeholder = element.attr("placeholder") or ""
        if "task" in placeholder.casefold() or "请输入" in placeholder:
            task_id_search_input_cache = element
            return element
    if visible_inputs:
        task_id_search_input_cache = visible_inputs[0]
        return visible_inputs[0]

    for selector in (
        'css:input[placeholder*="task"]',
        'css:input[placeholder*="Task"]',
        'css:input[placeholder*="请输入"]',
    ):
        element = first_displayed(tab.eles(selector, timeout=0.3))
        if element:
            task_id_search_input_cache = element
            return element
    return None


def task_id_filter_confirm_button():
    dropdowns = displayed_elements(tab.eles('css:div.ant-table-filter-dropdown', timeout=0.3))
    buttons = []
    for dropdown in dropdowns:
        try:
            buttons.extend(dropdown.eles('css:button', timeout=0.2))
        except Exception:
            continue
    if not buttons:
        buttons = tab.eles('css:button', timeout=0.3)

    for button in buttons:
        try:
            if not button.states.is_displayed or not button.states.has_rect:
                continue
            text = button.text.replace(" ", "")
            if text in ("搜索", "确定"):
                return button
        except Exception:
            continue
    return None


def task_id_filter_dropdown(search_input):
    for locator in (
        'xpath:./ancestor::div[contains(@class,"ant-table-filter-dropdown")][1]',
        'xpath:./ancestor::div[contains(@class,"ant-dropdown")][1]',
    ):
        try:
            dropdown = search_input.ele(locator, timeout=0)
            if dropdown:
                return dropdown
        except Exception:
            continue
    return None


def click_task_filter_clear_button(search_input):
    """优先点击输入框右侧清空按钮，避免多个缺失 ID 串在一起。"""
    dropdown = task_id_filter_dropdown(search_input)
    roots = [item for item in (dropdown, search_input.parent(1)) if item]
    locators = (
        'css:span.ant-input-clear-icon',
        'css:span.anticon-close-circle',
        'xpath:.//*[contains(@class,"ant-input-clear-icon")]',
        'xpath:.//*[@aria-label="close-circle"]',
        'xpath:.//*[@data-icon="close-circle"]/ancestor::*[contains(@class,"anticon")][1]',
    )
    for root in roots:
        for locator in locators:
            try:
                clear_button = first_displayed(root.eles(locator, timeout=0))
                if clear_button:
                    clear_button.click(by_js=True, timeout=0)
                    tab.wait(0.05)
                    return True
            except Exception:
                continue
    return False


def filter_input_value(search_input):
    try:
        value = search_input.property('value')
        return "" if value is None else str(value)
    except Exception:
        return ""


def set_filter_input_value(search_input, value):
    """用原生 value setter 同步 React 输入框，再让 Enter 触发查询。"""
    script = """
const value = arguments[0];
const input = this;
const proto = Object.getPrototypeOf(input);
const descriptor = Object.getOwnPropertyDescriptor(proto, 'value');
if (descriptor && descriptor.set) {
  descriptor.set.call(input, value);
} else {
  input.value = value;
}
input.dispatchEvent(new Event('input', {bubbles: true}));
input.dispatchEvent(new Event('change', {bubbles: true}));
"""
    search_input.run_js(script, str(value))


def press_filter_enter(search_input):
    """用 JS 触发 Enter 查询，避免物理按键要求元素必须有可点击尺寸。"""
    script = """
const options = {
  key: 'Enter',
  code: 'Enter',
  keyCode: 13,
  which: 13,
  bubbles: true,
  cancelable: true
};
this.dispatchEvent(new KeyboardEvent('keydown', options));
this.dispatchEvent(new KeyboardEvent('keypress', options));
this.dispatchEvent(new KeyboardEvent('keyup', options));
"""
    try:
        search_input.run_js(script)
        return True
    except Exception:
        pass
    try:
        search_input.input('\n')
        return True
    except Exception:
        pass

    confirm = task_id_filter_confirm_button()
    if confirm:
        try:
            confirm.click(by_js=True, timeout=0)
            return True
        except Exception:
            pass
    return False


def replace_filter_input(search_input, task_id):
    """清空并覆盖筛选框内容，防止多个 Task ID 连续拼接。"""
    value = str(task_id)
    click_task_filter_clear_button(search_input)
    try:
        search_input.run_js("this.focus();")
    except Exception:
        pass
    try:
        set_filter_input_value(search_input, "")
        set_filter_input_value(search_input, value)
    except Exception:
        search_input.click(by_js=True)
        search_input.input(Keys.CTRL_A)
        search_input.input(Keys.DELETE)
        search_input.input(value)
    tab.wait(0.1)

    try:
        actual_value = filter_input_value(search_input)
    except Exception:
        actual_value = value
    if actual_value != value:
        # React 输入框偶发未同步第一次键盘事件时，再覆盖一次。
        click_task_filter_clear_button(search_input)
        try:
            search_input.run_js("this.focus();")
        except Exception:
            pass
        try:
            set_filter_input_value(search_input, "")
            set_filter_input_value(search_input, value)
        except Exception:
            search_input.click(by_js=True)
            search_input.input(Keys.CTRL_A)
            search_input.input(Keys.DELETE)
            search_input.input(value)


def reset_task_id_filter(search_input=None):
    """清空 Task ID 筛选并回车恢复原表格，避免下一个 ID 接在空结果页上失败。"""
    global task_id_search_input_cache
    try:
        search_input = search_input or ensure_task_id_filter_input()
        click_task_filter_clear_button(search_input)
        try:
            set_filter_input_value(search_input, "")
        except Exception:
            search_input.click(by_js=True)
            search_input.input(Keys.CTRL_A)
            search_input.input(Keys.DELETE)
        press_filter_enter(search_input)
        wait_for_unfiltered_table()
    except Exception:
        task_id_search_input_cache = None


def confirm_missing_task_search(candidate_count):
    """询问是否补充缺失 Task ID，Enter 默认不补充。"""
    if portal_automated():
        print(f"\n门户自动化模式：跳过 {candidate_count} 个缺失 Task ID 补充。")
        return False
    print(
        f"\n相邻 ID 校验后剩余 {candidate_count} 个缺失 Task ID，"
        "是否进行补充？[y/N]: ",
        end="",
        flush=True,
    )
    answer = sys.stdin.readline().strip().lower()
    if answer == "y":
        print("已确认，开始补充缺失 Task ID。")
        return True
    print("未确认，跳过本次缺失 Task ID 补充。")
    return False


def ensure_task_id_filter_input():
    search_input = task_id_filter_input()
    if search_input:
        return search_input

    trigger = task_id_filter_trigger()
    if not trigger:
        raise RuntimeError("未找到 Task ID 搜索按钮")
    trigger.click(by_js=None, timeout=1)
    tab.wait(0.1)

    search_input = task_id_filter_input(use_cache=False)
    if not search_input:
        raise RuntimeError("未找到 Task ID 搜索输入框")
    return search_input


def search_task_id(task_id, retries=1):
    """通过 Task ID 表头筛选当前任务，返回命中的记录。"""
    global task_id_search_input_cache
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            search_input = ensure_task_id_filter_input()
            replace_filter_input(search_input, task_id)

            press_filter_enter(search_input)
            wait_for_task_search_result(
                task_id,
                timeout=MISSING_TASK_SEARCH_TIMEOUT,
            )
            records = collect_page_data(task_id_filter=task_id)
            if not records:
                reset_task_id_filter(search_input)
            return records
        except Exception as exc:
            last_error = exc
            task_id_search_input_cache = None
            try:
                reset_task_id_filter()
            except Exception:
                pass
            if attempt < retries:
                print(
                    f"Task {task_id} 查询第 {attempt} 次失败，"
                    f"准备重新定位筛选器: {exc}"
                )
                tab.wait(0.8)
    raise last_error


def write_ask_data(records):
    """按 Task ID 降序、Task ID 去重后写出展示文件。"""
    filtered_records = []
    for record in records:
        if record_matches_task_filters(record, CONFIG):
            ignored_task_ids.add(record.task_id)
            continue
        exclude_reason = record_exclude_reason(record, MAPPING_EXCLUDE_SUBSTRINGS)
        if exclude_reason:
            ignored_task_ids.add(record.task_id)
            print(
                f"按映射 YAML 全局排除字段过滤 Task "
                f"{record.task_id}: {exclude_reason}"
            )
            continue
        filtered_records.append(record)
    records = sort_unique_records(filtered_records)
    with open(ask_data_path, 'w', encoding='utf-8') as out_file:
        for record in records:
            out_file.write(record.to_line() + '\n')
            print(record.to_line())
    return records


def write_flow_categories(records):
    categories = sorted({category_key(record) for record in records if record.category})
    with open(data_class_path, 'w', encoding='utf-8') as class_file:
        for category in categories:
            class_file.write(category + '\n')
            print(f"分类: {category}")


def search_missing_task_ids(records):
    """生成缺失 ID 文件，并通过网页筛选补录真实存在的任务。"""
    missing_ids = missing_task_ids(records, ignored_ids=ignored_task_ids)
    with open(ask_data_temp_path, 'w', encoding='utf-8') as temp_file:
        for task_id in missing_ids:
            temp_file.write(task_id + '\n')

    present_ids = {
        record.task_id for record in records if record.task_id.isdigit()
    }
    present_ids.update(ignored_task_ids)
    candidate_ids, should_abort = prioritize_missing_task_ids(
        missing_ids,
        present_ids,
        threshold=MISSING_NEIGHBOR_THRESHOLD,
    )

    if len(missing_ids) > 15:
        print(
            f"缺失 Task ID 共 {len(missing_ids)} 个，"
            f"相邻 ID 校验后剩余 {len(candidate_ids)} 个候选"
        )
        if should_abort:
            print(f"候选缺失 ID 数量不符合补充条件，放弃本次补充，继续使用现有 {ASK_DATA_FILE}")
            return []
        if candidate_ids:
            both_neighbors = [
                task_id for task_id in candidate_ids
                if int(task_id) - 1 in {int(item) for item in present_ids}
                and int(task_id) + 1 in {int(item) for item in present_ids}
            ]
            if both_neighbors:
                print(f"优先检查上下相邻 ID 均存在的任务: {both_neighbors}")
        if not confirm_missing_task_search(len(candidate_ids)):
            return []

    found_records = []
    if candidate_ids:
        print(f"开始逐个回查 {len(candidate_ids)} 个候选缺失 Task ID...")
    candidate_count = len(candidate_ids)
    for index, task_id in enumerate(candidate_ids, 1):
        try:
            found = search_task_id(task_id)
            if found:
                allowed_found = []
                with open(ask_data_t2_path, 'a', encoding='utf-8') as t2_file:
                    for record in found:
                        exclude_reason = record_exclude_reason(
                            record,
                            MAPPING_EXCLUDE_SUBSTRINGS,
                        )
                        if exclude_reason:
                            ignored_task_ids.add(record.task_id)
                            print(
                                f"Task {record.task_id} 命中映射 YAML 全局排除字段，"
                                f"不写入补录文件: {exclude_reason}"
                            )
                            continue
                        allowed_found.append(record)
                        t2_file.write(record.to_line() + '\n')
                found_records.extend(allowed_found)
                print(
                    f"[{index}/{candidate_count}] Task {task_id}: "
                    f"找到 {len(allowed_found)} 条可用记录"
                )
            elif index % 100 == 0:
                print(f"[{index}/{candidate_count}] 已完成回查")
        except Exception as exc:
            print(f"回查 Task {task_id} 失败: {exc}")

    return found_records


page_size_ready = False


def ensure_page_size_100():
    """保证当前页是 100 条/页。"""
    global page_size_ready
    if page_size_ready:
        return
    select_page_size_100()
    page_size_ready = True


def get_char():
    """获取单个字符输入（支持空格、回车和模式选择）。"""
    if portal_automated():
        return '\n'
    if platform.system() == 'Windows':
        return msvcrt.getwch()

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def choose_collection_mode():
    """Choose configured automatic mode (y) or the original manual mode (n)."""
    max_pages = configured_max_collection_pages()
    if portal_automated():
        print("=" * 60)
        print("数据采集模式")
        print(f"  [y] 自动模式：筛选已完成，自动采集第 1-{max_pages} 页")
        print("门户自动化模式：直接使用自动模式 [y]")
        print("=" * 60)
        return "y"
    print("=" * 60)
    print("数据采集模式")
    print(f"  [y] 自动模式：筛选已完成，自动采集第 1-{max_pages} 页")
    print("  [n] 手动模式：保留原有空格采集、回车结束流程")
    print("直接按 Enter 默认使用自动模式 [y]")
    print("=" * 60)
    while True:
        ch = get_char()
        if ch in ('\n', '\r'):
            print("y (默认)")
            return "y"
        if ch in ('y', 'Y'):
            print("y")
            return "y"
        if ch in ('n', 'N'):
            print("n")
            return "n"
        if ch == '\x03':
            print("\n程序已终止")
            sys.exit(0)
        print(f"未知输入: '{ch}'，请输入 y 或 n")


def manual_collect_pages():
    """Run the original interactive collection flow without status filtering."""
    ensure_page_size_100()
    print("=" * 60)
    print("手动数据采集模式")
    print("  按 空格键(space) - 采集当前页面数据并追加到文件")
    print("  按 回车键(enter) - 采集最后一次数据并结束程序")
    print("=" * 60)

    all_records = []
    count = 0
    while True:
        print(f"\n等待输入 (已采集 {count} 次)...")
        ch = get_char()
        if ch == ' ':
            print("正在采集当前页面数据...")
            records = collect_page_data()
            if records:
                all_records.extend(records)
                count += 1
                print(f"成功采集 {len(records)} 条数据")
            else:
                print("未采集到数据")
        elif ch in ('\n', '\r'):
            print("正在采集最后一次数据...")
            records = collect_page_data()
            if records:
                all_records.extend(records)
                count += 1
                print(f"成功采集 {len(records)} 条数据")
            else:
                print("未采集到数据")
            print(f"\n共采集 {count} 次，准备写入文件...")
            return all_records
        elif ch == '\x03':
            print("\n程序已终止")
            sys.exit(0)
        else:
            print(f"未知输入: '{ch}'，请按空格或回车")


collection_mode = choose_collection_mode()
if collection_mode == "y":
    max_pages = configured_max_collection_pages()
    # 自动模式设置分页大小和任务状态，再从第 1 页采集到配置页数。
    ensure_page_size_100()
    apply_completed_task_status_filter()
    all_records = collect_first_pages(max_pages)
    print(f"\n自动采集第 1-{max_pages} 页结束，共读取 {len(all_records)} 条记录。")
else:
    all_records = manual_collect_pages()

# ---------- 6. 排序并保存 ----------
open(ask_data_t2_path, 'w', encoding='utf-8').close()
records = write_ask_data(all_records)
completed_records = get_flow_records(
    records,
    CONFIG,
    MAPPING_EXCLUDE_SUBSTRINGS,
)
write_flow_categories(completed_records)

if collection_mode == "n":
    # 保留原手动流程的缺失 Task ID 补录逻辑；自动五页模式只使用五页数据。
    found_records = search_missing_task_ids(records)
    if found_records:
        records = write_ask_data(records + found_records)
        completed_records = get_flow_records(
            records,
            CONFIG,
            MAPPING_EXCLUDE_SUBSTRINGS,
        )
        write_flow_categories(completed_records)

print(f"\n数据已写入 {ask_data_path} 和 {data_class_path}")
print("程序结束，浏览器资源开始清理。")
