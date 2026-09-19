import sys
import os
SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)
from DrissionPage import Chromium, ChromiumOptions
from DrissionPage.common import Settings
from ask_data_utils import resolve_chrome_path, resolve_chrome_user_data_dir

Settings.set_language('en')  # 可选

# 获取 exe/脚本所在目录，并处理开发时 src 层
if getattr(sys, 'frozen', False):
    # 打包后，exe 所在目录（例如 发布文件夹/）
    base_dir = os.path.dirname(sys.executable)
else:
    # 开发环境，main.py 在 src/ 下，需要回到项目根目录
    base_dir = os.path.dirname(os.path.abspath(__file__))
    base_dir = os.path.dirname(base_dir)   # web_robotization/

chrome_path = resolve_chrome_path(base_dir)
user_data_dir = resolve_chrome_user_data_dir(base_dir, 'annotation')



options = ChromiumOptions()
options.set_browser_path(chrome_path)
options.set_user_data_path(user_data_dir)
tab = Chromium(options).latest_tab

# 后续操作
tab.get('https://www.runoob.com')
tab.wait(0.8)
tab.ele('text:计算机基础').click(by_js=True)
tab.wait(0.8)
tab.ele('text:【学习 TCP/IP】').click(by_js=True)
