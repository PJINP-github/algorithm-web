@echo off
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
echo Python not found. Please install Python or add it to PATH.
  exit /b 1
)

:menu
echo Select mode:
echo   1        Choose local or LAN interactive review without SQLite audit reuse
echo   2        Choose local or LAN interactive review with SQLite audit reuse
echo   3        Build one merged package without algoType/split_limit splitting
echo   4        Use current split mode
echo   5        Generate collection point lists from the latest 3 checklists
set "mode="
set /p mode=Select mode [1/2/3/4/5, Enter=1]

if "%mode%"=="" (
  python rebuild_sources.py m
) else if "%mode%"=="1" (
  python rebuild_sources.py m
) else if "%mode%"=="2" (
  python rebuild_sources.py h
) else if "%mode%"=="3" (
  python rebuild_sources.py y
) else if "%mode%"=="4" (
  python rebuild_sources.py n
) else if "%mode%"=="5" (
  python rebuild_sources.py r
) else if /i "%mode%"=="m" (
  python rebuild_sources.py m
) else if /i "%mode%"=="h" (
  python rebuild_sources.py h
) else if /i "%mode%"=="y" (
  python rebuild_sources.py y
) else if /i "%mode%"=="n" (
  python rebuild_sources.py n
) else if /i "%mode%"=="r" (
  python rebuild_sources.py --collection-reports
) else (
  echo Invalid mode: %mode%
)

echo.
goto menu
