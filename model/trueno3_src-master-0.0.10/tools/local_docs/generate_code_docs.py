"""批量生成 Python 注释和目录说明文档。

功能：
    1. 为项目中的 Python 文件补充中文模块功能注释。
    2. 为每个 ``def`` / ``async def`` 补充中文函数功能注释。
    3. 为每个包含代码文件的目录生成 ``Code_Readme.md``，说明文件、
       类、函数、导入关系和目录作用。

使用：
    python tools/local_docs/generate_code_docs.py
    python tools/local_docs/generate_code_docs.py --docs-only

说明：
    这是面向本仓库的文档生成工具。生成的注释使用普通 ``#`` 行注释，
    不改写函数体，也不依赖第三方包，因此适合在本地环境运行。
"""

from __future__ import annotations

import argparse
import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EXCLUDED_PARTS = {".git", "__pycache__", ".pytest_cache", ".venv", "venv"}
FUNCTION_PATTERN = re.compile(
    r"^(?P<indent>[ \t]*)(?P<async>async[ \t]+)?def[ \t]+(?P<name>[A-Za-z_]\w*)"
)
SHEBANG_PATTERN = re.compile(r"^#!")
ENCODING_PATTERN = re.compile(r"coding[:=][ \t]*[-\w.]+")


def iter_python_files() -> list[Path]:
    """返回需要处理的 Python 文件。"""
    files = []
    for path in ROOT.rglob("*.py"):
        relative_parts = set(path.relative_to(ROOT).parts)
        if relative_parts & EXCLUDED_PARTS:
            continue
        files.append(path)
    return sorted(files)


def relative_name(path: Path) -> str:
    """返回适合写入中文说明的相对路径。"""
    return path.relative_to(ROOT).as_posix()


def module_comment(path: Path) -> str:
    """根据文件路径生成稳定的模块功能注释。"""
    relative = relative_name(path)
    if path.name == "__init__.py":
        purpose = "包初始化、公共接口导出或注册入口"
    elif path.name == "test_local_inference.py":
        purpose = "本地推理适配器自动化测试"
    elif path.name == "run_local.py":
        purpose = "本地网页服务启动入口"
    else:
        purpose = "本目录中的功能实现"
    return (
        f"# 功能：{relative}，负责{purpose}；详细说明见同目录 Code_Readme.md。"
    )


def function_comment(name: str, is_async: bool) -> str:
    """为函数名生成简短中文功能注释。"""
    kind = "异步函数" if is_async else "函数"
    return f"# {kind}功能：执行 {name} 的模块逻辑。"


def insertion_index(lines: list[str]) -> int:
    """计算模块注释应插入的位置，保留 shebang 和编码声明有效。"""
    index = 0
    if lines and SHEBANG_PATTERN.match(lines[0]):
        index = 1
    while index < min(2, len(lines)) and ENCODING_PATTERN.search(lines[index]):
        index += 1
    return index


def add_comments(path: Path) -> bool:
    """为一个 Python 文件补充模块和函数行注释。"""
    try:
        original = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    lines = original.splitlines(keepends=True)
    if not lines:
        lines = [""]
    changed = False

    first_code_index = insertion_index(lines)
    existing_prefix = "".join(lines[: min(len(lines), first_code_index + 4)])
    marker = module_comment(path)
    if marker not in existing_prefix:
        newline = "\r\n" if "\r\n" in original else "\n"
        lines.insert(first_code_index, marker + newline)
        changed = True

    annotated_lines: list[str] = []
    previous_nonempty = ""
    for line in lines:
        match = FUNCTION_PATTERN.match(line)
        if match:
            indent = match.group("indent")
            is_async = bool(match.group("async"))
            comment = function_comment(match.group("name"), is_async)
            already_annotated = (
                previous_nonempty.lstrip().startswith("#")
                and "功能：" in previous_nonempty
            )
            if not already_annotated:
                newline = "\r\n" if "\r\n" in original else "\n"
                annotated_lines.append(indent + comment + newline)
                changed = True
        annotated_lines.append(line)
        if line.strip():
            previous_nonempty = line.rstrip("\r\n")

    if changed:
        path.write_text("".join(annotated_lines), encoding="utf-8", newline="")
    return changed


def safe_unparse(node: ast.AST) -> str:
    """将 AST 节点转换为短文本，解析失败时返回空字符串。"""
    try:
        return ast.unparse(node)
    except (AttributeError, ValueError):
        return ""


def node_description(node: ast.AST) -> str:
    """提取类或函数的第一行文档说明。"""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        docstring = ast.get_docstring(node) or ""
        if docstring:
            return docstring.splitlines()[0].strip()
    return ""


def parse_python(path: Path) -> dict[str, object]:
    """解析一个 Python 文件的结构信息。"""
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        return {
            "imports": [],
            "classes": [],
            "functions": [],
            "error": f"{type(exc).__name__}: {exc}",
        }

    imports: list[str] = []
    classes: list[dict[str, str]] = []
    functions: list[dict[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imports.append("." * node.level + module)
        elif isinstance(node, ast.ClassDef):
            classes.append(
                {
                    "name": node.name,
                    "signature": safe_unparse(node)[:160],
                    "description": node_description(node),
                }
            )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(
                {
                    "name": node.name,
                    "signature": safe_unparse(node)[:160],
                    "description": node_description(node),
                }
            )
    return {
        "imports": sorted(set(imports)),
        "classes": classes,
        "functions": functions,
        "error": "",
    }


def render_file_section(path: Path, info: dict[str, object]) -> list[str]:
    """渲染单个 Python 文件的中文说明。"""
    lines = [f"### `{path.name}`", ""]
    error = str(info.get("error", ""))
    if error:
        lines.extend([f"- 解析状态：{error}", ""])
        return lines

    classes = info.get("classes", [])
    functions = info.get("functions", [])
    imports = info.get("imports", [])
    lines.append(
        f"- 作用：实现 `{path.relative_to(ROOT).as_posix()}` 对应的模块逻辑。"
    )
    if classes:
        lines.append("- 类：")
        for item in classes:
            description = item["description"] or "负责该模块的一组相关行为。"
            lines.append(f"  - `{item['name']}`：{description}")
    if functions:
        lines.append("- 函数：")
        for item in functions:
            description = item["description"] or "执行该模块中的一个处理步骤。"
            lines.append(f"  - `{item['name']}`：{description}")
    if imports:
        shown_imports = ", ".join(f"`{item}`" for item in imports[:16])
        suffix = " 等" if len(imports) > 16 else ""
        lines.append(f"- 依赖/调用入口：{shown_imports}{suffix}")
    if not classes and not functions:
        lines.append("- 结构：主要包含常量、配置、数据定义或包导出。")
    lines.append("")
    return lines


def render_directory_doc(directory: Path, python_files: list[Path]) -> str:
    """生成一个目录的 Code_Readme.md 内容。"""
    relative = directory.relative_to(ROOT).as_posix() if directory != ROOT else "."
    title = "Trueno 3 代码说明" if relative == "." else f"`{relative}` 代码说明"
    lines = [
        f"# {title}",
        "",
        "<!-- 由 tools/local_docs/generate_code_docs.py 自动生成，请勿手工编辑。 -->",
        "",
        "## 目录作用",
        "",
        (
            f"本目录包含 {len(python_files)} 个 Python 文件。"
            "文件之间的调用通常由包导入、基类继承、描述文件流水线或启动入口完成。"
        ),
        "",
        "## 文件说明",
        "",
    ]
    for path in python_files:
        lines.extend(render_file_section(path, parse_python(path)))
    lines.extend(
        [
            "## 调用约定",
            "",
            "- 服务器部署入口位于 `src/main.py`，使用硬件/服务器运行时配置。",
            "- 本机验证入口为根目录 `run_local.py`，调用 `local_inference` 和 `local_web`。",
            "- 模型能力和流水线配置以 `storage/model_descriptor.yaml` 为准。",
            "- 服务器测试目录 `tests/test_algo` 依赖远程服务；本地适配器测试位于 `tests/test_local_inference.py`。",
            "",
        ]
    )
    return "\n".join(lines)


def generate_docs(files: list[Path]) -> int:
    """为包含 Python 文件的每个目录生成说明文档。"""
    by_directory: dict[Path, list[Path]] = {}
    for path in files:
        by_directory.setdefault(path.parent, []).append(path)
    generated = 0
    for directory, python_files in sorted(by_directory.items()):
        document_path = directory / "Code_Readme.md"
        document_path.write_text(
            render_directory_doc(directory, sorted(python_files)),
            encoding="utf-8",
            newline="\n",
        )
        generated += 1
    return generated


def build_parser() -> argparse.ArgumentParser:
    """创建文档生成器命令行参数。"""
    parser = argparse.ArgumentParser(description="生成 Trueno 3 中文代码文档")
    parser.add_argument(
        "--docs-only",
        action="store_true",
        help="只生成 Code_Readme.md，不修改 Python 文件",
    )
    return parser


def main() -> int:
    """执行注释补充和目录文档生成。"""
    args = build_parser().parse_args()
    files = iter_python_files()
    changed = 0
    if not args.docs_only:
        for path in files:
            changed += int(add_comments(path))
    generated = generate_docs(files)
    print(f"Python 文件: {len(files)}")
    print(f"补充注释: {changed}")
    print(f"生成 Code_Readme.md: {generated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
