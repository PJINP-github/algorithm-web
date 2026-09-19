#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import re
import sys
import os                          # 新增：用于获取脚本所在目录

class Node:
    __slots__ = ('name', 'children', 'parent')
    def __init__(self, name):
        self.name = name
        self.children = []
        self.parent = None

    def add_child(self, node):
        node.parent = self
        self.children.append(node)

def parse_tree(lines):
    """解析树形文本，返回根节点。"""
    root = Node('root')
    stack = []          # 元素为 (indent_len, node)

    for raw_line in lines:
        line = raw_line.rstrip('\n')
        if not line.strip():
            continue

        # 查找第一个树枝符号
        m = re.search(r'(├──|└──)', line)
        if not m:
            continue

        indent_len = m.start()                     # 缩进字符串长度
        node_name = line[m.end():].strip()         # 节点内容

        # 处理带引号的文字或短横线
        if node_name.startswith('"') and node_name.endswith('"'):
            node_name = node_name[1:-1]
        elif node_name == '-':
            node_name = '-'

        node = Node(node_name)

        # 弹出所有缩进长度 ≥ 当前缩进长度的节点
        while stack and stack[-1][0] >= indent_len:
            stack.pop()

        # 确定父节点
        parent = stack[-1][1] if stack else root
        parent.add_child(node)

        # 当前节点入栈
        stack.append((indent_len, node))

    return root

def collect_text(node):
    """收集叶子节点的文本（一个td下通常只有一个文本）"""
    if not node.children:
        return node.name
    for child in node.children:
        text = collect_text(child)
        if text:
            return text
    return ''

def extract_tr_data(root):
    """提取所有tr下的td数据，返回列表的列表"""
    results = []

    def traverse(node):
        if node.name == 'tr':
            row = []
            for child in node.children:
                if child.name == 'td':
                    row.append(collect_text(child))
            results.append(row)
        for child in node.children:
            traverse(child)

    traverse(root)
    return results

def main():
    # ---- 修改处：固定文件路径为脚本所在目录 ----
    script_dir = os.path.dirname(os.path.abspath(__file__))
    input_file = os.path.join(script_dir, 'Tree.txt')
    output_file = os.path.join(script_dir, 'z_表格化数据.txt')
    # --------------------------------------------

    try:
        with open(input_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except FileNotFoundError:
        print(f"错误：文件 {input_file} 未找到！")
        sys.exit(1)

    root = parse_tree(lines)
    data_rows = extract_tr_data(root)

    with open(output_file, 'w', encoding='utf-8') as f:
        for i, row in enumerate(data_rows):
            # 用制表符分隔字段
            f.write('\t'.join(row))
            # 每条记录后空两行
            f.write('\n\n')

    print(f"处理完成！共提取 {len(data_rows)} 行记录，已保存到 {output_file}")

if __name__ == '__main__':
    main()