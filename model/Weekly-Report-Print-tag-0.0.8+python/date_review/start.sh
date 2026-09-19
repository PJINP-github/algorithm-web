#!/bin/bash

# ============================================================
# AutoEdge Data View 启动脚本
# 功能：完整运行数据采集和分析流程
# ============================================================

# 设置颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# 获取脚本所在目录
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SRC_DIR="$SCRIPT_DIR/src"
VENV_DIR="$SCRIPT_DIR/venv_linux"
WORK_DIR="$SCRIPT_DIR/0Work"

# 打印信息函数
info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

# ============================================================
# 1. 检查环境
# ============================================================
info "检查运行环境..."

# 检查虚拟环境
if [ ! -d "$VENV_DIR" ]; then
    error "虚拟环境目录不存在: $VENV_DIR"
    exit 1
fi

# 检查 Python
PYTHON="$VENV_DIR/bin/python3"
if [ ! -f "$PYTHON" ]; then
    error "Python 解释器不存在: $PYTHON"
    exit 1
fi

# 检查 src 目录
if [ ! -d "$SRC_DIR" ]; then
    error "源码目录不存在: $SRC_DIR"
    exit 1
fi

# ============================================================
# 2. 清理浏览器锁定文件
# ============================================================
info "清理浏览器锁定文件..."
USER_DATA_DIR="$SCRIPT_DIR/chromium_user_data"
if [ -d "$USER_DATA_DIR" ]; then
    rm -f "$USER_DATA_DIR/SingletonLock" 2>/dev/null
    rm -f "$USER_DATA_DIR/SingletonSocket" 2>/dev/null
    rm -f "$USER_DATA_DIR/SingletonCookie" 2>/dev/null
    info "已清理浏览器锁定文件"
else
    warn "浏览器数据目录不存在: $USER_DATA_DIR"
fi

# ============================================================
# 3. 激活虚拟环境并运行脚本
# ============================================================
info "激活虚拟环境..."
source "$VENV_DIR/bin/activate"

# 运行步骤1: 打开浏览器采集数据
info "===== 步骤1: 打开浏览器采集页面 ====="
info "正在运行 1.open.py..."
cd "$SCRIPT_DIR"
$PYTHON "$SRC_DIR/1.open.py"
if [ $? -ne 0 ]; then
    error "步骤1失败: 浏览器采集数据失败"
    exit 1
fi

# 检查页面文件是否生成
PAGE_FILE="$WORK_DIR/page.html"
if [ ! -f "$PAGE_FILE" ]; then
    error "步骤1失败: 页面文件未生成"
    exit 1
fi
info "页面文件已生成: $PAGE_FILE"

# 运行步骤2: 解析HTML生成树结构
info ""
info "===== 步骤2: 解析HTML生成树结构 ====="
info "正在运行 2.tree.py..."
$PYTHON "$SRC_DIR/2.tree.py"
if [ $? -ne 0 ]; then
    error "步骤2失败: HTML解析失败"
    exit 1
fi

# 运行步骤2.5: 生成表格化数据
info ""
info "===== 步骤2.5: 生成表格化数据 ====="
info "正在运行 z.table_format.py..."
$PYTHON "$SRC_DIR/z.table_format.py"
if [ $? -ne 0 ]; then
    error "步骤2.5失败: 表格化数据生成失败"
    exit 1
fi

# 运行步骤3: 总结树结构数据
info ""
info "===== 步骤3: 总结树结构数据 ====="
info "正在运行 3.test2_summary.py..."
$PYTHON "$SRC_DIR/3.test2_summary.py"
if [ $? -ne 0 ]; then
    error "步骤3失败: 数据总结失败"
    exit 1
fi

# 运行步骤4: 生成统计数据
info ""
info "===== 步骤4: 生成统计数据 ====="
info "正在运行 4.statistics.py..."
$PYTHON "$SRC_DIR/4.statistics.py"
if [ $? -ne 0 ]; then
    error "步骤4失败: 统计生成失败"
    exit 1
fi

# ============================================================
# 4. 完成
# ============================================================
info ""
success "========================================"
success "所有步骤已完成！"
success "========================================"
info "生成的文件:"
info "  - $WORK_DIR/page.html (原始页面)"
info "  - $WORK_DIR/Tree.txt (树结构数据)"
info "  - $WORK_DIR/z_树结构数据.txt (总结数据)"
info "  - $WORK_DIR/Statistics.txt (统计结果-按最早日期)"
info "  - $WORK_DIR/average_statistics.txt (统计结果-按密集簇平均)"
success "========================================"

# 退出虚拟环境
deactivate
