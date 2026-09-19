#!/bin/bash


set -e


############################################################
# HTML -> XLSX
#
# 模式：
#
# y:
#   完整数据 + 自动格式
#
# n:
#   完整数据 + 不合并
#
# a:
#   算法审核精简模式
#
# b:
#   算法审核精简模式 + 站点排序 + 准确率可视化
#
############################################################



BASE_DIR="$(cd "$(dirname "$0")" && pwd)"


DEFAULT_HTML="${BASE_DIR}/0Work/page.html"


PYTHON_SCRIPT="${BASE_DIR}/html_to_xlsx.py"



############################################################
# 输入参数
############################################################


if [ $# -eq 0 ]; then


    INPUT="$DEFAULT_HTML"



elif [ $# -eq 1 ]; then


    INPUT="$1"



else


    echo
    echo "用法:"
    echo
    echo "./run_html2xlsx.sh"
    echo "./run_html2xlsx.sh xxx.html"
    echo


    exit 1


fi



############################################################
# 文件检查
############################################################


if [ ! -f "$INPUT" ]; then


    echo
    echo "[ERROR] HTML不存在:"
    echo "$INPUT"
    echo


    exit 1

fi



if [ ! -f "$PYTHON_SCRIPT" ]; then


    echo
    echo "[ERROR] Python脚本不存在:"
    echo "$PYTHON_SCRIPT"
    echo


    exit 1

fi



############################################################
# 模式选择
############################################################


echo

echo "=============================================="

echo "              HTML -> XLSX"

echo "=============================================="

echo


echo "输入文件:"
echo "$INPUT"


echo


echo "请选择生成模式:"
echo


echo " y : 完整数据 + 合并展示"

echo " n : 完整数据 + 不合并"

echo " a : 算法审核精简模式"

echo " b : 算法审核精简 + 排序 + 准确率可视化"

echo



while true
do


    read -r -p "请选择 [y/n/a/b] (默认n): " MODE_INPUT



    if [ -z "$MODE_INPUT" ]; then

        MODE_INPUT="n"

    fi



    case "$MODE_INPUT" in



        y|Y)

            MODE="y"

            break

            ;;


        n|N)

            MODE="n"

            break

            ;;


        a|A)

            MODE="a"

            break

            ;;


        b|B)

            MODE="b"

            break

            ;;


        *)

            echo

            echo "[ERROR] 请输入 y / n / a / b"

            echo

            ;;


    esac


done




############################################################
# 输出
############################################################


OUTPUT="$(dirname "$INPUT")/result.xlsx"



echo

echo "=============================================="

echo "配置"

echo "=============================================="

echo


case "$MODE" in


y)

echo "模式: 完整合并模式"

;;


n)

echo "模式: 完整普通模式"

;;


a)

echo "模式: 算法审核精简模式"

;;


b)

echo "模式: 算法审核精简 + 排序 + 准确率可视化"

;;

esac



echo

echo "输出:"
echo "$OUTPUT"


echo

echo "=============================================="

echo



############################################################
# 执行
############################################################


python3 \
    "$PYTHON_SCRIPT" \
    "$INPUT" \
    "$OUTPUT" \
    "$MODE"



echo


echo "=============================================="

echo "完成"

echo "=============================================="