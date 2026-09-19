import sys
import os
import json
import platform
import shutil
import time
import subprocess
from DrissionPage import Chromium, ChromiumOptions
from DrissionPage.common import Settings

Settings.set_language('en')

TABLE_STATE_JS = r"""
return (() => {
  const table = document.querySelector('#antTable') || document.querySelector('.ant-table');
  const body = table
    ? (table.querySelector('.ant-table-body') || document.querySelector('.ant-table-body'))
    : document.querySelector('.ant-table-body');
  const root = table || document;
  const rows = root.querySelectorAll('tbody.ant-table-tbody tr.ant-table-row:not([aria-hidden="true"])');
  const plusIcons = root.querySelectorAll('.anticon-plus-square');
  const minusIcons = root.querySelectorAll('.anticon-minus-square');
  return {
    hasTable: !!table,
    hasBody: !!body,
    rows: rows.length,
    plus: plusIcons.length,
    minus: minusIcons.length,
    spinning: !!document.querySelector('.ant-spin-spinning'),
    virtual: !!document.querySelector('.rc-virtual-list-holder, .ant-table-tbody-virtual'),
    scrollTop: body ? body.scrollTop : 0,
    scrollHeight: body ? body.scrollHeight : 0,
    clientHeight: body ? body.clientHeight : 0
  };
})();
"""

CLICK_EXPAND_BUTTON_JS = r"""
return (() => {
  const normalize = text => (text || '').replace(/\s+/g, ' ').trim();
  const buttons = Array.from(document.querySelectorAll('button'));
  const button = buttons.find(btn => normalize(btn.innerText) === '一键展开' && !btn.disabled)
    || buttons.find(btn => Array.from(btn.querySelectorAll('span')).some(span => normalize(span.textContent) === '一键展开') && !btn.disabled);
  if (!button) {
    return {
      clicked: false,
      reason: 'not_found',
      buttons: buttons.map(btn => normalize(btn.innerText)).filter(Boolean).slice(0, 30)
    };
  }

  button.scrollIntoView({ block: 'center', inline: 'center' });
  button.focus({ preventScroll: true });
  const rect = button.getBoundingClientRect();
  const baseEvent = {
    bubbles: true,
    cancelable: true,
    view: window,
    clientX: rect.left + rect.width / 2,
    clientY: rect.top + rect.height / 2
  };

  for (const name of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
    let event;
    if (name.startsWith('pointer') && window.PointerEvent) {
      event = new PointerEvent(name, {
        ...baseEvent,
        pointerId: 1,
        pointerType: 'mouse',
        isPrimary: true,
        buttons: name.endsWith('down') ? 1 : 0
      });
    } else {
      event = new MouseEvent(name, baseEvent);
    }
    button.dispatchEvent(event);
  }

  return {
    clicked: true,
    text: normalize(button.innerText),
    className: String(button.className || '')
  };
})();
"""


def run_js(tab, script, default=None, timeout=10):
    try:
        result = tab.run_js(script, timeout=timeout)
        return default if result is None else result
    except Exception as exc:
        print(f"[WARN] JS执行失败: {exc}")
        return default


def get_table_state(tab):
    state = run_js(tab, TABLE_STATE_JS, default={}, timeout=10)
    if not isinstance(state, dict):
        return {
            "hasTable": False,
            "hasBody": False,
            "rows": 0,
            "plus": 0,
            "minus": 0,
            "spinning": False,
            "virtual": False,
            "scrollTop": 0,
            "scrollHeight": 0,
            "clientHeight": 0,
        }
    return state


def print_table_state(prefix, state):
    print(
        f"{prefix}: rows={state.get('rows', 0)}, "
        f"plus={state.get('plus', 0)}, minus={state.get('minus', 0)}, "
        f"scroll={state.get('scrollTop', 0)}/{state.get('scrollHeight', 0)}"
    )


def wait_for_table(tab, timeout=60):
    print("等待表格渲染...")
    end_time = time.time() + timeout
    last_state = {}
    while time.time() < end_time:
        state = get_table_state(tab)
        last_state = state
        if state.get("hasTable") and state.get("rows", 0) > 0 and not state.get("spinning"):
            print_table_state("表格已加载", state)
            return state
        time.sleep(1)
    print_table_state("表格等待超时", last_state)
    raise RuntimeError("表格未在限定时间内加载完成")


def wait_for_table_stable(tab, timeout=45, stable_rounds=3):
    end_time = time.time() + timeout
    last_key = None
    stable_count = 0
    last_state = {}

    while time.time() < end_time:
        state = get_table_state(tab)
        last_state = state
        key = (
            state.get("rows", 0),
            state.get("plus", 0),
            state.get("minus", 0),
            state.get("scrollHeight", 0),
        )
        if key == last_key and not state.get("spinning"):
            stable_count += 1
        else:
            stable_count = 0
            last_key = key
        if stable_count >= stable_rounds and state.get("rows", 0) > 0:
            return state
        time.sleep(1)

    return last_state


def click_expand_button_with_locator(tab):
    locators = [
        'xpath://button[.//span[normalize-space()="一键展开"]]',
        'xpath://button[contains(normalize-space(.), "一键展开")]',
    ]
    for locator in locators:
        try:
            button = tab.ele(locator, timeout=5)
            button.click(by_js=False, timeout=5, wait_stop=False)
            print(f"通过页面元素点击'一键展开'成功: {locator}")
            return True
        except Exception as exc:
            print(f"页面元素点击失败 ({locator}): {exc}")
            try:
                button = tab.ele(locator, timeout=2)
                button.click(by_js=True, timeout=5, wait_stop=False)
                print(f"通过JS点击'一键展开'成功: {locator}")
                return True
            except Exception as js_exc:
                print(f"JS元素点击也失败 ({locator}): {js_exc}")
    return False


def click_expand_button_with_dom(tab):
    result = run_js(tab, CLICK_EXPAND_BUTTON_JS, default={"clicked": False}, timeout=10)
    if isinstance(result, dict) and result.get("clicked"):
        print("通过DOM事件点击'一键展开'成功")
        return True
    print(f"DOM事件点击'一键展开'失败: {result}")
    return False


def click_visible_plus_icons(tab, max_count=200):
    script = f"""
return (() => {{
  const table = document.querySelector('#antTable') || document;
  const icons = Array.from(table.querySelectorAll('.anticon-plus-square'))
    .filter(icon => !!(icon.offsetWidth || icon.offsetHeight || icon.getClientRects().length));
  const targets = icons.slice(0, {int(max_count)});
  for (const icon of targets) {{
    const target = icon.closest('[role="img"], button, .ant-flex') || icon;
    const rect = target.getBoundingClientRect();
    const eventOptions = {{
      bubbles: true,
      cancelable: true,
      view: window,
      clientX: rect.left + rect.width / 2,
      clientY: rect.top + rect.height / 2
    }};
    target.dispatchEvent(new MouseEvent('mousedown', eventOptions));
    target.dispatchEvent(new MouseEvent('mouseup', eventOptions));
    target.dispatchEvent(new MouseEvent('click', eventOptions));
  }}
  return {{ clicked: targets.length, totalVisible: icons.length }};
}})();
"""
    result = run_js(tab, script, default={"clicked": 0, "totalVisible": 0}, timeout=15)
    if not isinstance(result, dict):
        return 0
    return int(result.get("clicked", 0) or 0)


def ensure_table_expanded(tab):
    state = wait_for_table(tab)
    if int(state.get("plus", 0) or 0) == 0:
        print("表格当前没有折叠项，跳过展开点击。")
        return state

    for attempt in range(1, 4):
        print(f"尝试点击'一键展开'按钮，第 {attempt} 次...")
        clicked = click_expand_button_with_locator(tab) or click_expand_button_with_dom(tab)
        if not clicked:
            time.sleep(2)
            continue

        state = wait_for_table_stable(tab, timeout=45)
        print_table_state("展开后状态", state)
        if int(state.get("plus", 0) or 0) == 0:
            return state

    print("一键展开后仍有折叠项，尝试逐个展开可见折叠项...")
    for round_index in range(1, 11):
        state = get_table_state(tab)
        if int(state.get("plus", 0) or 0) == 0:
            return state
        clicked = click_visible_plus_icons(tab)
        print(f"逐个展开第 {round_index} 轮，点击 {clicked} 个折叠项")
        if clicked <= 0:
            break
        time.sleep(2)

    state = wait_for_table_stable(tab, timeout=30)
    print_table_state("最终展开状态", state)
    if int(state.get("plus", 0) or 0) > 0:
        raise RuntimeError(f"展开未完成，仍有 {state.get('plus')} 个折叠项")
    return state


def scroll_table_to_load_all(tab):
    print("滚动表格，确认懒加载内容已渲染...")
    last_key = None
    stable_count = 0
    state = get_table_state(tab)
    for _ in range(40):
        run_js(
            tab,
            """
const table = document.querySelector('#antTable') || document;
const body = table.querySelector('.ant-table-body') || document.querySelector('.ant-table-body');
if (body) {
  body.scrollTop = body.scrollHeight;
  body.dispatchEvent(new Event('scroll', { bubbles: true }));
}
""",
            timeout=5,
        )
        time.sleep(0.8)
        state = get_table_state(tab)
        key = (state.get("rows", 0), state.get("plus", 0), state.get("scrollHeight", 0))
        if key == last_key and not state.get("spinning"):
            stable_count += 1
        else:
            stable_count = 0
            last_key = key
        if stable_count >= 3:
            break

    if state.get("virtual"):
        print("[WARN] 检测到虚拟滚动表格，保存的HTML可能只包含当前DOM中的行。")
    else:
        run_js(
            tab,
            """
const table = document.querySelector('#antTable') || document;
const body = table.querySelector('.ant-table-body') || document.querySelector('.ant-table-body');
if (body) body.scrollTop = 0;
""",
            timeout=5,
        )
        state = get_table_state(tab)

    print_table_state("滚动确认完成", state)
    return state


def get_page_html(tab):
    html_content = run_js(
        tab,
        "return document.documentElement.outerHTML;",
        default=None,
        timeout=20,
    )
    return html_content or tab.html


def minimize_browser_window(tab):
    """尽量把自动采集用的浏览器窗口最小化，避免打断桌面操作。"""
    if wants_headless_browser():
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


def resolve_authority_browser_path(base_dir, model_key):
    return resolve_authority_config_path(base_dir, 'Browser_Path', model_key)


def resolve_authority_config_path(base_dir, section_name, model_key):
    current = os.path.abspath(base_dir)
    while True:
        authority_path = os.path.join(current, 'authority.yaml')
        if os.path.isfile(authority_path):
            try:
                import yaml
                with open(authority_path, 'r', encoding='utf-8') as stream:
                    config = yaml.safe_load(stream) or {}
                value = browser_path_value(config.get(section_name), model_key)
                if value:
                    value = os.path.expandvars(os.path.expanduser(value)).replace('\\', '/')
                    if not os.path.isabs(value):
                        project_root = os.path.dirname(authority_path)
                        probe = project_root
                        while probe:
                            if os.path.isfile(os.path.join(probe, 'Cargo.toml')):
                                project_root = probe
                                break
                            parent = os.path.dirname(probe)
                            if parent == probe:
                                break
                            probe = parent
                        value = os.path.join(project_root, value)
                    return os.path.normpath(value)
            except (ImportError, OSError, ValueError, AttributeError):
                pass
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def browser_path_value(configured, model_key):
    if isinstance(configured, dict):
        aliases = {
            'annotation': (
                'annotation',
                'Model-Annotation',
                'Model_Annotation',
                'Model-Annotation-tag-0.1.1+python',
            ),
            'weekly': (
                'weekly',
                'Weekly-Report-Print',
                'Weekly_Report_Print',
                'Weekly-Report-Print-tag-0.0.8+python',
            ),
        }.get(model_key, (model_key,))
        for alias in aliases:
            value = str(configured.get(alias) or '').strip()
            if value:
                return value
        return ''
    return str(configured or '').strip()


def seed_linux_chromium_profile(base_dir, configured_path=''):
    source_root = os.environ.get(
        'RUST_PORTAL_CHROMIUM_SOURCE_DIR',
        os.path.expanduser('~/.config/chromium'),
    )
    source_root = os.path.abspath(os.path.expanduser(os.path.expandvars(source_root)))
    source_profile = os.path.join(source_root, 'Default')
    if not os.path.isdir(source_profile):
        return None

    model_root = os.path.abspath(os.path.join(base_dir, '..', '..'))
    profile_root = (
        os.path.abspath(os.path.expanduser(os.path.expandvars(configured_path)))
        if configured_path
        else os.path.join(model_root, '.chromium_profile_linux')
    )
    target_profile = os.path.join(profile_root, 'Default')

    def ignore_profile(_directory, names):
        ignored = {
            'Cache',
            'Code Cache',
            'GPUCache',
            'ShaderCache',
            'GrShaderCache',
            'DawnCache',
            'Service Worker',
        }
        return {name for name in names if name in ignored}

    try:
        os.makedirs(profile_root, exist_ok=True)
        if not os.path.exists(target_profile):
            shutil.copytree(source_profile, target_profile, ignore=ignore_profile)
        source_state = os.path.join(source_root, 'Local State')
        target_state = os.path.join(profile_root, 'Local State')
        if os.path.isfile(source_state) and not os.path.exists(target_state):
            shutil.copy2(source_state, target_state)
        return profile_root
    except OSError as exc:
        print(f'复制系统 Chromium profile 失败，将使用现有 profile：{exc}')
        return profile_root if os.path.isdir(target_profile) else None

# ---------- 1. 确定项目目录 ----------
if getattr(sys, 'frozen', False):
    base_dir = os.path.dirname(sys.executable)
else:
    base_dir = os.path.dirname(os.path.abspath(__file__))

work_dir = os.environ.get('RUST_PORTAL_DATA_DIR', '').strip() or os.path.join(os.path.dirname(base_dir), '0Work')
work_dir = os.path.abspath(work_dir)
os.makedirs(work_dir, exist_ok=True)

# ---------- 2. 查找 Chrome 可执行文件 ----------
configured_chrome = os.environ.get('RUST_PORTAL_CHROME_PATH', '').strip()
if configured_chrome and os.path.isfile(configured_chrome):
    chrome_path = os.path.normpath(configured_chrome)
elif platform.system() == 'Windows':
    authority_chrome = resolve_authority_browser_path(base_dir, 'weekly')
    if authority_chrome and os.path.isfile(authority_chrome):
        chrome_path = authority_chrome
    else:
        chrome_path = os.path.normpath(os.path.join(base_dir, '..', 'chrome', 'chrome.exe'))
        if not os.path.isfile(chrome_path):
            raise RuntimeError(f"未找到Chrome浏览器: {chrome_path}")
else:
    linux_chrome_paths = ['/usr/lib/chromium/chromium'] + [
        shutil.which(name)
        for name in (
            'google-chrome-stable',
            'google-chrome',
            'chromium',
            'chromium-browser',
            'brave-browser',
            'microsoft-edge',
            'microsoft-edge-stable',
        )
    ] + [
        '/usr/bin/google-chrome-stable',
        '/usr/bin/google-chrome',
        '/usr/bin/chromium',
        '/snap/bin/chromium',
        '/usr/local/bin/google-chrome',
        '/usr/bin/chromium-browser',
    ]
    chrome_path = next(
        (os.path.abspath(path) for path in linux_chrome_paths if path and os.path.isfile(path)),
        None,
    )
    if not chrome_path:
        raise RuntimeError("未找到Chrome/Chromium浏览器，请安装或指定路径")
print(f"使用浏览器: {chrome_path}")

# ---------- 3. 浏览器用户数据目录（固定目录，保存登录状态）----------
if platform.system() == 'Linux':
    user_data_dir = seed_linux_chromium_profile(
        base_dir,
        os.environ.get('RUST_PORTAL_USER_DATA_DIR', '').strip(),
    ) or os.path.join(base_dir, '..', 'chrome', 'Data')
else:
    authority_user_data_dir = resolve_authority_config_path(
        base_dir,
        'Browser_User_Data_Dir',
        'weekly',
    )
    user_data_dir = authority_user_data_dir or (
        os.environ.get('RUST_PORTAL_USER_DATA_DIR', '').strip()
        or os.path.join(base_dir, '..', 'chrome', 'Data')
    )
user_data_dir = os.path.abspath(os.path.expanduser(os.path.expandvars(user_data_dir)))
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


def wants_headless_browser():
    override = os.environ.get('RUST_PORTAL_HEADLESS')
    if override is not None:
        return override.strip().lower() in ('1', 'true', 'yes', 'on')
    return (
        platform.system() == 'Linux'
        and not os.environ.get('DISPLAY')
        and not os.environ.get('WAYLAND_DISPLAY')
    )


def browser_metadata(port):
    try:
        import requests
        response = requests.get(
            f'http://127.0.0.1:{port}/json/version',
            headers={'Connection': 'close'},
            timeout=2,
        )
        response.raise_for_status()
        metadata = response.json()
        return metadata if metadata.get('webSocketDebuggerUrl') else None
    except Exception:
        return None


def metadata_is_headless(metadata):
    return 'headless' in (
        f"{metadata.get('Browser', '')} {metadata.get('User-Agent', '')}"
    ).lower()


def close_headless_browser(port):
    metadata = browser_metadata(port)
    if not metadata or wants_headless_browser() or not metadata_is_headless(metadata):
        return False
    from websocket import create_connection

    try:
        connection = create_connection(
            metadata['webSocketDebuggerUrl'],
            timeout=3,
            suppress_origin=True,
        )
        connection.send(json.dumps({'id': 1, 'method': 'Browser.close'}))
        connection.close()
    except Exception as exc:
        print(f'关闭旧headless浏览器失败: {exc}')
        return False
    deadline = time.time() + 5
    while time.time() < deadline:
        if browser_metadata(port) is None:
            return True
        time.sleep(0.2)
    return False


def connect_to_browser(port):
    """Connect to the exact CDP endpoint exposed by the running browser."""
    import requests
    from websocket import create_connection

    address = f'127.0.0.1:{port}'
    last_error = None
    websocket_url = None
    browser_is_headless = wants_headless_browser()
    for _ in range(8):
        try:
            response = requests.get(
                f'http://{address}/json/version',
                headers={'Connection': 'close'},
                timeout=2,
            )
            response.raise_for_status()
            metadata = response.json()
            candidate = metadata.get('webSocketDebuggerUrl')
            if not isinstance(candidate, str) or not candidate.startswith(('ws://', 'wss://')):
                raise RuntimeError('浏览器未返回有效的 webSocketDebuggerUrl')

            probe = create_connection(
                candidate,
                timeout=3,
                suppress_origin=True,
            )
            probe.close()
            websocket_url = candidate
            browser_is_headless = browser_is_headless or metadata_is_headless(metadata)
            break
        except Exception as exc:
            last_error = exc
            time.sleep(0.5)
    if websocket_url is None:
        raise RuntimeError(f'连接浏览器失败（端口 {port}）：{last_error}') from last_error
    options = ChromiumOptions(read_file=False)
    options.set_address(websocket_url).existing_only(True).new_env(False)
    if browser_is_headless:
        options.headless(True)
    return Chromium(options)


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


browser_running = check_browser_running(DEBUG_PORT)
if browser_running and not wants_headless_browser():
    metadata = browser_metadata(DEBUG_PORT)
    if metadata and metadata_is_headless(metadata):
        print('检测到旧headless浏览器，正在关闭并切换为可见模式...')
        if not close_headless_browser(DEBUG_PORT):
            raise RuntimeError(
                f'无法关闭端口 {DEBUG_PORT} 上的旧headless浏览器，请先手动关闭该浏览器进程'
            )
        browser_running = False
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
        '--window-size=1920,1080',
    ]
    if platform.system() == 'Linux':
        args.append('--profile-directory=Default')
    if not wants_headless_browser():
        args.append('--start-minimized')
    if platform.system() != 'Windows':
        args.extend(['--no-sandbox', '--disable-dev-shm-usage'])
    if platform.system() == 'Windows':
        args.extend([
            '--disable-gpu',
            '--disable-software-rasterizer',
            '--in-process-gpu',
            '--disable-gpu-compositing',
        ])
    
    if wants_headless_browser():
        args.append('--headless=new')
        args.append('--disable-gpu')
        args.append('--disable-software-rasterizer')
    
    print(f"启动命令: {' '.join(args)}")
    
    log_file = os.path.join(base_dir, 'chrome_startup.log')
    startupinfo = None
    if platform.system() == 'Windows':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 6  # SW_MINIMIZE
    with open(log_file, 'w', encoding='utf-8') as f:
        try:
            process = subprocess.Popen(
                args,
                stdout=f,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                startupinfo=startupinfo,
                creationflags=getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0),
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
browser = None  # 提前初始化，避免 finally 中引用未定义变量
tab = None
try:
    browser = connect_to_browser(DEBUG_PORT)
    print("浏览器连接成功")
    capture_success = False

    tab = browser.new_tab()
    print("已新建标签页")
    minimize_browser_window(tab)

    # ---------- 业务操作（放入 try 块）----------
    try:
        print("打开页面...")
        tab.get('http://autoedge.jiangxingai.com/board/board-overview')
        minimize_browser_window(tab)
        
        print("等待页面加载...")
        time.sleep(10)

        ensure_table_expanded(tab)
        final_state = scroll_table_to_load_all(tab)
        if int(final_state.get("rows", 0) or 0) <= 0:
            raise RuntimeError("表格行数为0，未保存页面")

        print("读取展开后的完整页面HTML...")
        html_content = get_page_html(tab)

        # work_dir 已在开头创建，直接使用
        html_path = os.path.join(work_dir, 'page.html')
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(html_content)
        print(f"页面已保存至: {html_path}")
        capture_success = True

    except Exception as e:
        import traceback
        print(f"业务操作出错: {e}")
        traceback.print_exc()

except Exception as e:
    import traceback
    print(f"浏览器连接或操作出错: {e}")
    traceback.print_exc()

finally:
    if browser_started_by_script and browser is not None:
        try:
            browser.quit()
            print("已关闭本流程启动的专用浏览器")
        except Exception:
            pass
    elif tab is not None:
        try:
            tab.close()
            print("标签页已关闭，保留已存在的自动化浏览器")
        except Exception:
            pass
    print("清理完成")

if not locals().get("capture_success", False):
    sys.exit(1)
