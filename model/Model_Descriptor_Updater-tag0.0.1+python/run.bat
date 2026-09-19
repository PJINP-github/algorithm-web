@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] 未找到 python 命令，请安装 Python 3.9+ 并加入 PATH。
  pause
  exit /b 1
)

python portal_entry.py
set EXITCODE=%ERRORLEVEL%

echo.
if "%EXITCODE%"=="0" (
  echo 处理完成。
) else (
  echo 处理失败，退出码 %EXITCODE%。
)
pause
exit /b %EXITCODE%