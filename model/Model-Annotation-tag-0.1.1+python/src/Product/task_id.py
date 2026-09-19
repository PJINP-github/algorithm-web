''' 
爬取autoEdge上http://autoedge.jiangxingai.com/algorithm/collect/tasks最新一百个 标注任务ID、状态等信息
用于审查哪种算法模型需要更新数据

'''



import sys
import os
SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)
from DrissionPage import Chromium, ChromiumOptions
from DrissionPage.common import Settings
from ask_data_utils import resolve_chrome_path, resolve_chrome_user_data_dir
from DrissionPage.common import tree
import logging
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
tab.get('http://autoedge.jiangxingai.com/algorithm/collect/tasks')
#实现点击已完成 (可能需要点击 下拉)
ele_01=tab.ele('@class=ant-spin-container ant-spin-blur')
ele_02=ele_01.children()[0].children()[0].children()[0].children()[0].children()[1]
ele_03=ele_02.children()[0].children()[7].children()[0].children()[1]
ele_03.click()
ele=tab.ele('@class=ant-dropdown ant-table-css-var css-var-r0 ant-dropdown-css-var ant-dropdown-placement-bottomRight').children()[0].children()[0].children()[0].children()[1].children()[0]
#ele=tab.ele('@class=ant-select-dropdown ant-select-dropdown-hidden css-var-r0 ant-select-css-var ant-select-dropdown-placement-bottomLeft').children()[0].children()[1].children()[0].children()[0]
ele=tab.ele('t:svg@@id=SvgjsSvg1001').next()
ele=ele.children()[0].children()[0]
ele=ele('请选择任务状态').prev().children()[0].children()[0].children()[0].click()
ele.run_js("this.setAttribute('aria-activedescendant', 'rc_select_1_list_2');")
ele.input('已完成\n')

#实现翻100页
ele=tab.ele('@class=ant-pagination ant-table-pagination ant-table-pagination-right css-var-r0').children()[0]
ele=ele.parent().children()[-1].children()[0].children()[0].click()
ele=tab.ele('@class=ant-select-dropdown css-var-r0 ant-select-css-var ant-select-dropdown-placement-topLeft').children()[0].children()[0].children()[0].children()[0].children()[0]
tab.ele('100 条/页').click() 

ele=tab.ele('@class=ant-table-row ant-table-row-level-0').parent()
# 1. 获取所有普通数据行（class 同时包含 ant-table-row 和 ant-table-row-level-0）



'''
for page in range(2,100+1):
    test_0=tab.ele('xpath://*[@id="antTable"]/div/div/table/tbody/tr['+str(page)+']').children()[1].text.split('/')
    test_1=f"{test_0[1]} {test_0[0]}&"
    test_2=tab.ele('xpath://*[@id="antTable"]/div/div/table/tbody/tr['+str(page)+']').children()[3].text
    test_3=tab.ele('xpath://*[@id="antTable"]/div/div/table/tbody/tr['+str(page)+']').children()[7].text
    print("Task "+test_2 +"      "+test_3+"      "+test_1)

'''
lines_to_write = []

for page in range(2, 101):   # 2 到 100  inclusive
    test_0=tab.ele('xpath://*[@id="antTable"]/div/div/table/tbody/tr['+str(page)+']').children()[1].text.split('/')
    test_1=f"{test_0[1]} {test_0[0]}&"
    test_2=tab.ele('xpath://*[@id="antTable"]/div/div/table/tbody/tr['+str(page)+']').children()[3].text
    test_3=tab.ele('xpath://*[@id="antTable"]/div/div/table/tbody/tr['+str(page)+']').children()[7].text
    # ============================================================================

    # 按照原始 print 的格式构造行内容
    line = "Task " + test_2 + "      " + test_3 + "      " + test_1
    lines_to_write.append(line)

# 1. 将数据写入 tmp.txt（覆盖写入）
with open('tmp.txt', 'w', encoding='utf-8') as tmp_file:
    for line in lines_to_write:
        tmp_file.write(line + '\n')

# 2. 读取 tmp.txt，按分类分组
categories = {}   # {分类键: [行字符串列表]}

with open('tmp.txt', 'r', encoding='utf-8') as tmp_file:
    for raw_line in tmp_file:
        line = raw_line.rstrip('\n')
        if not line:          # 跳过空行
            continue
        parts = line.split()  # 按任意空白字符分割
        # 确保至少有4列
        if len(parts) < 4:
            key = 'unknown'
        else:
            fourth = parts[3]
            # 判断第4列是否为纯数字（整数）
            if fourth.isdigit():
                # 使用第5列作为分类键
                if len(parts) >= 5:
                    key = parts[4]
                else:
                    key = 'unknown'
            else:
                key = fourth
        categories.setdefault(key, []).append(line)

# 3. 将分组结果写入标注任务明细并同时打印到终端
with open('数字-1-标注任务明细.txt', 'w', encoding='utf-8') as out_file:
    first_cat = True
    for cat, lines in categories.items():
        if not first_cat:
            out_file.write('\n')   # 分类之间空一行
            print()                # 终端输出也空一行
        first_cat = False

        for line in lines:
            out_file.write(line + '\n')
            print(line)

# 可选：若需要删除临时文件 tmp.txt，取消下一行注释
# os.remove('tmp.txt')


