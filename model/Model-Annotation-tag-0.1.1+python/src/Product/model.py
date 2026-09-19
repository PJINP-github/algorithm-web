''' 
爬取http://autoedge.jiangxingai.com/algorithm/train/model-trainingTask最新一百个 标注任务ID、状态等信息
用于审查哪种算法模型需要更新数据

nodes_str = '数 屏柜 表记 旋钮 接地 分合'
'''
nodes_str = '数 屏柜 分合'


import sys
import os
SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)
from DrissionPage import Chromium, ChromiumOptions
from DrissionPage.common import Settings
from ask_data_utils import resolve_chrome_path, resolve_chrome_user_data_dir
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
tab.get('http://autoedge.jiangxingai.com/algorithm/train/model-trainingTask')
'''
ele=tab.ele('tag:thead@class=ant-table-thead').ele('tag:tr').children()[-2].children()[0].children()[1].click(by_js=True)
ele=tab.ele('@id=SvgjsSvg1001').next().children()[0].children()[0].children()[0].children()[0].children()[0].children()[0].children()[0]
ele.input('数\n')
tab.ele('查看详情').click()
ele=tab.ele('训练数据集').parent().parent().next().children()[0].children()[0]
print(ele.text)
tree(ele)

tab.refresh()

'''
nodes = nodes_str.split()
# 获取当前 .py 文件所在目录
script_dir = os.path.dirname(os.path.abspath(__file__))
file_path = os.path.join(script_dir, "数字-4-模型训练记录.txt")

# 清空文件（若不存在则创建空文件）
with open(file_path, 'w', encoding='utf-8') as f:
    pass

# 循环处理每个节点
for node in nodes:
    ele=tab.ele('tag:thead@class=ant-table-thead').ele('tag:tr').children()[-2].children()[0].children()[1].click(by_js=True)
    ele=tab.ele('@id=SvgjsSvg1001').next().children()[0].children()[0].children()[0].children()[0].children()[0].children()[0].children()[0]
    ele.input(node + '\n')
    #
    ele2=tab.ele('查看详情').parent().parent().parent().prev()
    test_02=ele2.text
    #
    tab.ele('查看详情').click()
    ele=tab.ele('训练数据集').parent().parent().next().children()[0].children()[0]
    
    content = ele.text
    items = content.split(',')
    # 2. 提取前3个元素中 @ 后面的数字（转为整数）
    numbers = [int(item.split('@')[1]) for item in items[:3]]
    # 3. 从大到小排序
    numbers_sorted = sorted(numbers, reverse=True)
    # 4. 转为字符串并用空格连接
    content_01 = ' '.join(map(str, numbers_sorted))
    # 追加写入文件
    with open(file_path, 'a', encoding='utf-8') as f:
        f.write("训练备注: "+test_02+"  最新Task: "+content_01 + "  完整Task:"+content+'\n')

    tab.refresh()
