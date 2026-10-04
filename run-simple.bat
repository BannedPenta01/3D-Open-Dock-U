@echo off
cd /d "%~dp0"
echo ============================================
echo  3D Open Dock U - Easy Mode
echo ============================================
echo.

if not exist ".venv\Scripts\python.exe" goto MAKEVENV
echo [1/3] Virtual environment found.
goto CHECKDEPS

:MAKEVENV
echo [1/3] Creating Python virtual environment...
py -m venv .venv
if errorlevel 1 goto NOVENV
goto CHECKDEPS

:NOVENV
echo.
echo Could not create the Python environment.
echo Install Python 3.11 or newer from python.org
echo and tick "Add python.exe to PATH" during setup.
pause
exit /b 1

:CHECKDEPS
".venv\Scripts\python.exe" -c "import PySide6" >nul 2>nul
if errorlevel 1 goto INSTALLDEPS
echo [2/3] Dependencies OK.
goto STARTAPP

:INSTALLDEPS
echo [2/3] Installing needed files - first run only, please wait...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto NODEPS
goto STARTAPP

:NODEPS
echo.
echo Could not install dependencies. Check your internet connection.
pause
exit /b 1

:STARTAPP
echo [3/3] Starting Easy Mode...
echo If no window appears, read the error text below.
echo.
".venv\Scripts\python.exe" -m src.easy_mode 2>&1
echo.
echo The app closed.
pause
