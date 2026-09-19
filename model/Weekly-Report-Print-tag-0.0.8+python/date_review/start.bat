@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

REM ============================================================
REM AutoEdge Data View 启动脚本 (Windows)
REM 功能：完整运行数据采集和分析流程
REM ============================================================

set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "SRC_DIR=%SCRIPT_DIR%\src"
set "WORK_DIR=%SCRIPT_DIR%\0Work"
set "NO_PAUSE=0"
set "FULL_ANALYSIS=0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

:PARSE_ARGS
if "%~1"=="" goto PARSE_ARGS_DONE
if /I "%~1"=="--no-pause" set "NO_PAUSE=1"
if /I "%~1"=="--full-analysis" set "FULL_ANALYSIS=1"
shift
goto PARSE_ARGS

:PARSE_ARGS_DONE

echo.
echo ============================================================
echo    AutoEdge Data View
echo ============================================================
echo.

echo [INFO] 检查运行环境...

REM 检查 Python
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] 未找到 Python，请安装 Python 并加入 PATH
    if not "%NO_PAUSE%"=="1" pause
    exit /b 1
)

REM 检查 src 目录
if not exist "%SRC_DIR%" (
    echo [ERROR] 源码目录不存在: %SRC_DIR%
    if not "%NO_PAUSE%"=="1" pause
    exit /b 1
)

REM ============================================================
REM 2. 清理浏览器锁定文件
REM ============================================================
echo [INFO] 清理浏览器锁定文件...
set "USER_DATA_DIR=%SCRIPT_DIR%\chrome\Data"
if exist "%USER_DATA_DIR%" (
    if exist "%USER_DATA_DIR%\SingletonLock" del /q "%USER_DATA_DIR%\SingletonLock" >nul 2>&1
    if exist "%USER_DATA_DIR%\SingletonSocket" del /q "%USER_DATA_DIR%\SingletonSocket" >nul 2>&1
    if exist "%USER_DATA_DIR%\SingletonCookie" del /q "%USER_DATA_DIR%\SingletonCookie" >nul 2>&1
    echo [INFO] 已清理浏览器锁定文件
) else (
    echo [WARN] 浏览器数据目录不存在: %USER_DATA_DIR%
)

REM ============================================================
REM 3. 运行脚本
REM ============================================================

REM 运行步骤1: 打开浏览器采集数据
echo.
echo [INFO] ===== 步骤1: 打开浏览器采集页面 =====
echo [INFO] 正在运行 1.open.py...
python "%SRC_DIR%\1.open.py"
if errorlevel 1 (
    echo [ERROR] 步骤1失败: 浏览器采集数据失败
    if not "%NO_PAUSE%"=="1" pause
    exit /b 1
)

REM 检查页面文件是否生成
set "PAGE_FILE=%WORK_DIR%\page.html"
if not exist "%PAGE_FILE%" (
    echo [ERROR] 步骤1失败: 页面文件未生成
    if not "%NO_PAUSE%"=="1" pause
    exit /b 1
)
echo [INFO] 页面文件已生成: %PAGE_FILE%

if not "%FULL_ANALYSIS%"=="1" goto done_capture

REM 运行步骤2: 解析HTML生成树结构
echo.
echo [INFO] ===== 步骤2: 解析HTML生成树结构 =====
echo [INFO] 正在运行 2.tree.py...
python "%SRC_DIR%\2.tree.py"
if errorlevel 1 (
    echo [ERROR] 步骤2失败: HTML解析失败
    if not "%NO_PAUSE%"=="1" pause
    exit /b 1
)

REM 运行步骤2.5: 生成表格化数据
echo.
echo [INFO] ===== 步骤2.5: 生成表格化数据 =====
echo [INFO] 正在运行 z.table_format.py...
python "%SRC_DIR%\z.table_format.py"
if errorlevel 1 (
    echo [ERROR] 步骤2.5失败: 表格化数据生成失败
    if not "%NO_PAUSE%"=="1" pause
    exit /b 1
)

REM 运行步骤3: 总结树结构数据
echo.
echo [INFO] ===== 步骤3: 总结树结构数据 =====
echo [INFO] 正在运行 3.test2_summary.py...
python "%SRC_DIR%\3.test2_summary.py"
if errorlevel 1 (
    echo [ERROR] 步骤3失败: 数据总结失败
    if not "%NO_PAUSE%"=="1" pause
    exit /b 1
)

REM 运行步骤4: 生成统计数据
echo.
echo [INFO] ===== 步骤4: 生成统计数据 =====
echo [INFO] 正在运行 4.statistics.py...
python "%SRC_DIR%\4.statistics.py"
if errorlevel 1 (
    echo [ERROR] 步骤4失败: 统计生成失败
    if not "%NO_PAUSE%"=="1" pause
    exit /b 1
)

REM ============================================================
REM 4. 完成
REM ============================================================
:done_capture
echo.
echo [SUCCESS] ========================================
if "%FULL_ANALYSIS%"=="1" (
    echo [SUCCESS] 所有步骤已完成！
) else (
    echo [SUCCESS] 页面采集已完成！
)
echo [SUCCESS] ========================================
echo [INFO] 生成的文件:
echo [INFO]   - %WORK_DIR%\page.html (原始页面)
if "%FULL_ANALYSIS%"=="1" (
    echo [INFO]   - %WORK_DIR%\Tree.txt (树结构数据)
    echo [INFO]   - %WORK_DIR%\z_树结构数据.txt (总结数据)
    echo [INFO]   - %WORK_DIR%\Statistics.txt (统计结果-按最早日期)
    echo [INFO]   - %WORK_DIR%\average_statistics.txt (统计结果-按密集簇平均)
)
echo [SUCCESS] ========================================
echo.

if not "%NO_PAUSE%"=="1" pause
endlocal
