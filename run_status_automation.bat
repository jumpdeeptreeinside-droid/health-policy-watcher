@echo off
cd /d "%~dp0"
call venv\Scripts\activate.bat
python src\notion_status_automation.py

echo. >> run_status_automation.log
echo ===================================== >> run_status_automation.log
echo %date% %time% >> run_status_automation.log
echo ===================================== >> run_status_automation.log

pause
