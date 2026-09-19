@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
set "SRC_DIR=%SCRIPT_DIR%src"
set "PYTHON_BIN="
set "PY_LAUNCHER="

if exist "%SCRIPT_DIR%venv\Scripts\python.exe" set "PYTHON_BIN=%SCRIPT_DIR%venv\Scripts\python.exe"
if not defined PYTHON_BIN (
    for %%P in (python.exe) do set "PYTHON_BIN=%%~$PATH:P"
)
if not defined PYTHON_BIN (
    for %%P in (py.exe) do set "PY_LAUNCHER=%%~$PATH:P"
)

if not defined PYTHON_BIN if not defined PY_LAUNCHER (
    echo ERROR: Python was not found. Please install Python or create a Windows venv.
    pause
    exit /b 1
)

cd /d "%SCRIPT_DIR%"

echo Script dir: %SRC_DIR%
if defined PYTHON_BIN (
    echo Python: %PYTHON_BIN%
) else (
    echo Python: %PY_LAUNCHER% -3
)
echo.

if "%~1"=="" goto :interactive

:args_loop
if "%~1"=="" goto :done
call :run_sequence "%~1"
if errorlevel 1 goto :failed
shift
goto :args_loop

:interactive
echo Type the number sequence to run:
echo   1     run 1.task_id.py
echo   2     run 2.model.py
echo   3     run 3.sum_up.py
echo   4     run 4.outlook_sort.py
echo   5     run 5.summary_table.py
echo   12    run 1 then 2
echo   12345 run 1 through 5
echo   q     quit
echo.

:prompt
set "USER_CHOICE="
set /p "USER_CHOICE=Enter number sequence [default 12345]: "
if /i "%USER_CHOICE%"=="q" goto :done
if "%USER_CHOICE%"=="" set "USER_CHOICE=12345"
call :run_sequence "%USER_CHOICE%"
if errorlevel 1 goto :failed
echo.
goto :prompt

:run_sequence
set "SEQUENCE=%~1"
set "INDEX=0"

:sequence_loop
set "SCRIPT_NUM=!SEQUENCE:~%INDEX%,1!"
if "!SCRIPT_NUM!"=="" exit /b 0
call :run_script "!SCRIPT_NUM!"
if errorlevel 1 exit /b 1
set /a INDEX+=1
goto :sequence_loop

:run_script
set "SCRIPT_NUM=%~1"
set "SCRIPT_PATH="
if "%SCRIPT_NUM%"=="1" set "SCRIPT_PATH=%SRC_DIR%\1.task_id.py"
if "%SCRIPT_NUM%"=="2" set "SCRIPT_PATH=%SRC_DIR%\2.model.py"
if "%SCRIPT_NUM%"=="3" set "SCRIPT_PATH=%SRC_DIR%\3.sum_up.py"
if "%SCRIPT_NUM%"=="4" set "SCRIPT_PATH=%SRC_DIR%\4.outlook_sort.py"
if "%SCRIPT_NUM%"=="5" set "SCRIPT_PATH=%SRC_DIR%\5.summary_table.py"

if not defined SCRIPT_PATH (
    echo WARNING: Unknown script number "%SCRIPT_NUM%"; skipped.
    exit /b 0
)

if not exist "%SCRIPT_PATH%" (
    echo ERROR: Script file does not exist: %SCRIPT_PATH%
    exit /b 1
)

echo ========================================
echo Starting script: %SCRIPT_NUM%
echo ========================================
if defined PYTHON_BIN (
    "%PYTHON_BIN%" "%SCRIPT_PATH%"
) else (
    "%PY_LAUNCHER%" -3 "%SCRIPT_PATH%"
)
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo Script %SCRIPT_NUM% finished, exit code: %EXIT_CODE%
echo.
exit /b %EXIT_CODE%

:failed
echo Failed. Stopped remaining scripts.
pause
exit /b 1

:done
echo Done.
pause
exit /b 0
