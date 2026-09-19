from html.parser import HTMLParser
import os   # 新增导入

class TreeNode:
    def __init__(self, tag):
        self.tag = tag
        self.children = []

class TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__()
        self.root = TreeNode('__root__')
        self.stack = [self.root]
        self.skip_tag = None

    def handle_starttag(self, tag, attrs):
        if tag == 'path' or self.skip_tag:
            if not self.skip_tag:
                self.skip_tag = tag
            return
        node = TreeNode(tag)
        self.stack[-1].children.append(node)
        self.stack.append(node)

    def handle_endtag(self, tag):
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_tag = None
            return
        if self.stack and self.stack[-1].tag == tag:
            self.stack.pop()

    def handle_data(self, data):
        if self.skip_tag:
            return
        text = data.strip()
        if text:
            self.stack[-1].children.append(f'"{text}"')

def find_nodes_by_tag(root, tag):
    """递归查找所有指定标签的节点"""
    result = []
    if isinstance(root, TreeNode) and root.tag == tag:
        result.append(root)
    if isinstance(root, TreeNode):
        for child in root.children:
            result.extend(find_nodes_by_tag(child, tag))
    return result

def tree_to_lines(node, prefix='', is_last=True):
    """生成树的每一行，返回字符串列表"""
    lines = []
    if isinstance(node, str):
        connector = '└── ' if is_last else '├── '
        lines.append(prefix + connector + node)
        return lines

    if node.tag == '__root__':
        for i, child in enumerate(node.children):
            lines.extend(tree_to_lines(child, '', i == len(node.children) - 1))
        return lines

    connector = '└── ' if is_last else '├── '
    lines.append(prefix + connector + node.tag)

    extension = '    ' if is_last else '│   '
    for i, child in enumerate(node.children):
        lines.extend(tree_to_lines(child, prefix + extension, i == len(node.children) - 1))
    return lines

def main():
    # 获取项目根目录
    script_dir = os.path.dirname(os.path.abspath(__file__))
    base_dir = os.path.dirname(script_dir)
    work_dir = os.path.join(base_dir, '0Work')
    os.makedirs(work_dir, exist_ok=True)
    
    # 构建输入文件路径
    html_path = os.path.join(work_dir, 'page.html')
    # 构建输出文件路径
    output_path = os.path.join(work_dir, 'Tree.txt')
    
    # 1. 读取 HTML 文件
    with open(html_path, 'r', encoding='utf-8') as f:
        html = f.read()

    # 2. 解析为完整树
    parser = TreeBuilder()
    parser.feed(html)

    # 3. 找到 <body> 下的所有 <tr>
    body_nodes = find_nodes_by_tag(parser.root, 'body')
    if not body_nodes:
        print('未找到 <body> 标签')
        return

    body = body_nodes[0]
    tr_nodes = find_nodes_by_tag(body, 'tr')

    # 4. 将每个 tr 的树形图写入文件
    with open(output_path, 'w', encoding='utf-8') as out:
        for i, tr in enumerate(tr_nodes):
            lines = tree_to_lines(tr)
            out.write('\n'.join(lines) + '\n')
            if i < len(tr_nodes) - 1:
                out.write('\n')

    print(f'已成功将 {len(tr_nodes)} 个 <tr> 节点写入 {output_path}')

if __name__ == '__main__':
    main()