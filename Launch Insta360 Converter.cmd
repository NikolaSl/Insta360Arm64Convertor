@echo off
setlocal
where pythonw.exe >nul 2>&1
if not errorlevel 1 (
    start "" pythonw.exe "%~dp0insta360_gui.py"
    exit /b
)
python "%~dp0insta360_gui.py"
if errorlevel 1 pause
