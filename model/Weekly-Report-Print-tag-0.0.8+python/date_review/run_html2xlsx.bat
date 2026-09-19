@echo off
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"
python "%~dp0run_html2xlsx.py" %*
exit /b %errorlevel%
