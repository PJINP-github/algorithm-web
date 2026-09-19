#!/bin/bash

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
SRC_DIR="$SCRIPT_DIR/src"

PYTHON_BIN=""

if [ -f "$SCRIPT_DIR/venv/bin/python3" ]; then
    PYTHON_BIN="$SCRIPT_DIR/venv/bin/python3"
elif [ -f "$SCRIPT_DIR/venv/bin/python" ]; then
    PYTHON_BIN="$SCRIPT_DIR/venv/bin/python"
elif command -v python3 &>/dev/null; then
    PYTHON_BIN="$(command -v python3)"
elif command -v python &>/dev/null; then
    PYTHON_BIN="$(command -v python)"
fi

if [ -z "$PYTHON_BIN" ]; then
    echo "错误: 未找到 Python 解释器"
    exit 1
fi

echo "使用 Python: $PYTHON_BIN"
echo "脚本目录: $SRC_DIR"
echo ""

run_script() {
    local script_num=$1
    local script_path=""
    
    case $script_num in
        1)
            script_path="$SRC_DIR/1.task_id.py"
            ;;
        2)
            script_path="$SRC_DIR/2.model.py"
            ;;
        3)
            script_path="$SRC_DIR/3.sum_up.py"
            ;;
        4)
            script_path="$SRC_DIR/4.outlook_sort.py"   # 新增
            ;;
        5)
            script_path="$SRC_DIR/5.summary_table.py"
            ;;
        *)
            echo "错误: 未知脚本编号 - $script_num"
            return 1
            ;;
    esac
    
    if [ ! -f "$script_path" ]; then
        echo "错误: 脚本文件不存在 - $script_path"
        return 1
    fi
    
    echo "========================================"
    echo "启动脚本: ${script_num}.py"
    echo "========================================"
    
    "$PYTHON_BIN" "$script_path"
    local exit_code=$?
    
    echo ""
    echo "脚本 ${script_num}.py 执行完成, 退出码: $exit_code"
    echo ""
    
    return $exit_code
}

if [ $# -eq 0 ]; then
    echo "用法: $0 <参数>"
    echo "参数说明:"
    echo "  1        - 启动 1.task_id.py"
    echo "  2        - 启动 2.model.py"
    echo "  3        - 启动 3.sum_up.py"
    echo "  4        - 启动 4.outlook_sort.py"          # 新增
    echo "  5        - 启动 5.summary_table.py"
    echo "  12       - 先启动 1.task_id.py, 完成后再启动 2.model.py"
    echo "  123      - 按顺序启动 1 → 2 → 3"
    echo "  1234     - 按顺序启动 1 → 2 → 3 → 4"      # 新增示例
    echo "  12345    - 按顺序启动 1 → 2 → 3 → 4 → 5"
    echo "  12 4     - 按顺序启动多个脚本（空格分隔）"
    exit 1
fi

for arg in "$@"; do
    echo "处理参数: $arg"
    
    for (( i=0; i<${#arg}; i++ )); do
        script_num=${arg:i:1}
        
        case $script_num in
            1|2|3|4|5)                               # 新增 5
                run_script "$script_num"
                if [ $? -ne 0 ]; then
                    echo "脚本 ${script_num}.py 执行失败, 终止后续执行"
                    exit 1
                fi
                ;;
            *)
                echo "警告: 未知脚本编号 '$script_num', 跳过"
                ;;
        esac
    done
done

echo "所有脚本执行完成!"
