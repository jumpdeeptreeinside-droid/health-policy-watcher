@echo off

echo =====================================================
echo  Notion to WordPress Auto Uploader
echo =====================================================
echo.

cd /d "%~dp0"
echo [INFO] Working directory: %CD%
echo.

if not exist "venv\Scripts\activate.bat" (
    echo [ERROR] venv not found: %CD%\venv\Scripts\activate.bat
    echo.
    echo Please create venv first:
    echo   python -m venv venv
    echo   venv\Scripts\activate
    echo   pip install -r requirements.txt
    goto :END
)

call venv\Scripts\activate.bat
echo [INFO] venv activated
echo.

if not exist "src\notion_wordpress_uploader.py" (
    echo [ERROR] Script not found: %CD%\src\notion_wordpress_uploader.py
    goto :END
)

echo [INFO] Checking required libraries...
python -c "import requests, markdown; print('[INFO] Libraries OK')" 2>&1
if errorlevel 1 (
    echo [WARN] Some libraries missing. Installing from requirements.txt...
    pip install -r requirements.txt
    echo.
)

echo.
echo [INFO] Running script...
echo =====================================================
echo.

python src\notion_wordpress_uploader.py 2>&1
set PYTHON_EXIT=%errorlevel%

echo.
echo =====================================================
if %PYTHON_EXIT% neq 0 (
    echo [ERROR] Script exited with code %PYTHON_EXIT%
) else (
    echo [INFO] Script completed successfully
)

echo %date% %time% ExitCode=%PYTHON_EXIT% >> run_wordpress_uploader.log

:END
echo.
echo =====================================================
echo  Press any key to close this window.
echo =====================================================
pause > nul
