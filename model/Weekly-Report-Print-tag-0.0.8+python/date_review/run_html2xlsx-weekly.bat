@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

REM ============================================================
REM HTML -> XLSX
REM
REM 模式：
REM
REM y:   完整数据 + 自动格式
REM n:   完整数据 + 不合并
REM a:   算法审核精简模式
REM b:   算法审核精简模式 + 站点排序 + 准确率可视化
REM ============================================================

set "BASE_DIR=%~dp0"
set "BASE_DIR=%BASE_DIR:~0,-1%"
set "DEFAULT_HTML=%BASE_DIR%\0Work\page.html"
set "PYTHON_SCRIPT=%BASE_DIR%\html_to_xlsx-abnm.py"

REM ============================================================
REM 输入参数
REM ============================================================
if "%~1"=="" (
    set "INPUT=%DEFAULT_HTML%"
) else (
    set "INPUT=%~1"
    if not "%~2"=="" (
        echo.
        echo 用法:
        echo.
        echo run_html2xlsx-weekly.bat
        echo run_html2xlsx-weekly.bat xxx.html
        echo.
        pause
        exit /b 1
    )
)

REM ============================================================
REM 文件检查
REM ============================================================
if not exist "%INPUT%" (
    echo.
    echo [ERROR] HTML不存在:
    echo %INPUT%
    echo.
    pause
    exit /b 1
)

if not exist "%PYTHON_SCRIPT%" (
    echo.
    echo [ERROR] Python脚本不存在:
    echo %PYTHON_SCRIPT%
    echo.
    pause
    exit /b 1
)

REM ============================================================
REM 模式选择
REM ============================================================
echo.
echo ==============================================
echo              HTML -> XLSX
echo ==============================================
echo.
echo 输入文件:
echo %INPUT%
echo.
echo 请选择生成模式:
echo.
echo  y : 完整数据 + 合并展示
echo  n : 完整数据 + 不合并
echo  a : 算法审核精简模式
echo  b : 算法审核精简 + 排序 + 准确率可视化
echo.

:CHOOSE_MODE
set "MODE_INPUT="
set /p "MODE_INPUT=请选择 [y/n/a/b] (默认b): "

if "%MODE_INPUT%"=="" set "MODE_INPUT=b"

if /i "%MODE_INPUT%"=="y" (
    set "MODE=y"
    goto MODE_SELECTED
)
if /i "%MODE_INPUT%"=="n" (
    set "MODE=n"
    goto MODE_SELECTED
)
if /i "%MODE_INPUT%"=="a" (
    set "MODE=a"
    goto MODE_SELECTED
)
if /i "%MODE_INPUT%"=="b" (
    set "MODE=b"
    goto MODE_SELECTED
)

echo.
echo [ERROR] 请输入 y / n / a / b
echo.
goto CHOOSE_MODE

:MODE_SELECTED

REM ============================================================
REM 输出
REM ============================================================
for %%F in ("%INPUT%") do set "OUTPUT=%%~dpFupload.xlsx"

echo.
echo ==============================================
echo 配置
echo ==============================================
echo.
if "%MODE%"=="y" echo 模式: 完整合并模式
if "%MODE%"=="n" echo 模式: 完整普通模式
if "%MODE%"=="a" echo 模式: 算法审核精简模式
if "%MODE%"=="b" echo 模式: 算法审核精简 + 排序 + 准确率可视化
echo.
echo 输出:
echo %OUTPUT%
echo.
echo ==============================================
echo.

REM ============================================================
REM 执行
REM ============================================================

python "%PYTHON_SCRIPT%" "%INPUT%" "%OUTPUT%" "%MODE%"
if errorlevel 1 (
    echo.
    echo [ERROR] 执行失败
    echo.
    pause
    exit /b 1
)

echo.
echo ==============================================
echo 完成
echo ==============================================

pause
endlocal
