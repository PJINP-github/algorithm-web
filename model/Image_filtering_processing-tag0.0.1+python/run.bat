@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
    where py >nul 2>nul && set "PY=py"
)
if not defined PY (
    echo [错误] 未检测到 python，请先安装 Python 并加入 PATH
    pause
    exit /b 1
)

:MAIN_LOOP
cls
echo ==========================================
echo   相似图分组 + 模糊图片排除
echo ==========================================
echo   输入要处理的文件夹路径，回车开始处理
echo   输入 q 或直接回车退出
echo ==========================================
echo.

set "INPUT="
set /p INPUT=请输入路径: 

rem 去掉可能带的首尾引号
set INPUT=%INPUT:"=%

rem 空或 q 退出
if "%INPUT%"=="" goto :END
if /i "%INPUT%"=="q" goto :END
if /i "%INPUT%"=="quit" goto :END
if /i "%INPUT%"=="exit" goto :END

if not exist "%INPUT%\" (
    echo.
    echo [错误] 路径不存在: %INPUT%
    echo.
    pause
    goto :MAIN_LOOP
)

echo.
echo [配置] %~dp0config.yaml
echo [输入] %INPUT%
echo [开始] 正在处理，请稍候...
echo.

%PY% "%~dp0similar_group.py" --input "%INPUT%" --config "%~dp0config.yaml"

echo.
echo ==========================================
echo   处理结束: %INPUT%
echo ==========================================
echo.
echo 按任意键返回主菜单继续处理下一个文件夹...
pause >nul
goto :MAIN_LOOP

:END
echo.
echo 已退出。
timeout /t 2 >nul
exit /b 0