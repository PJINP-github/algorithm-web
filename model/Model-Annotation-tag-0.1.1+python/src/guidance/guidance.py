from DrissionPage import Chromium, ChromiumOptions
from DrissionPage.common import Settings
import sys

#中文报错以及连接本地浏览器
Settings.set_language('en')  # 设置为中文时，填入'zh_cn'
import os
# main.py 的目录 = .../web_robotization/src/
base_dir = os.path.dirname(os.path.abspath(__file__))
# 上级目录 = .../web_robotization/
project_dir = os.path.dirname(base_dir)
if project_dir not in sys.path:
    sys.path.insert(0, project_dir)
from ask_data_utils import resolve_chrome_path, resolve_chrome_user_data_dir

chrome_path = resolve_chrome_path(project_dir)
user_data_dir = resolve_chrome_user_data_dir(project_dir, 'annotation')
options = ChromiumOptions()
options.set_browser_path(chrome_path)
options.set_user_data_path(user_data_dir)
tab = Chromium(options).latest_tab


#连接自动化网址
tab.get('https://gitee.com/login')

# 定位到账号文本框，获取文本框元素
ele = tab.ele('#user_login')
# 输入对文本框输入账号
ele.input('您的账号')
# 定位到密码文本框并输入密码
tab.ele('#user_password').input('您的密码')
# 点击登录按钮
tab.ele('@value=登 录').click()
