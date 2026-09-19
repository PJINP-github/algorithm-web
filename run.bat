@echo off
setlocal

cd /d "%~dp0"
set "RUST_PORTAL_PROJECT_ROOT=%~dp0"
if not defined OPEN_BROWSER set "OPEN_BROWSER=1"
if not defined PORT set "PORT=3000"

echo Starting algorithm web release service on port %PORT% ...
cargo run --release --manifest-path "%~dp0Cargo.toml"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo Service stopped with exit code %EXIT_CODE%.
    pause >nul
)
exit /b %EXIT_CODE%
