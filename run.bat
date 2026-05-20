@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating Python virtual environment...
    py -m venv .venv
    if errorlevel 1 (
        echo.
        echo Failed to create the virtual environment. Make sure Python is installed.
        pause
        exit /b 1
    )
)

".venv\Scripts\python.exe" -c "import PySide6" >nul 2>nul
if errorlevel 1 (
    echo Installing Python dependencies...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Failed to install dependencies. Check your Python and internet connection.
        pause
        exit /b 1
    )
)

".venv\Scripts\python.exe" start_gui.py
pause
