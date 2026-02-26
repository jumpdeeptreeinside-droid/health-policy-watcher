@echo off
cd /d "%~dp0"
call venv\Scripts\activate.bat
python src\notion_content_generator.py

echo. >> run_content_generator.log
echo ===================================== >> run_content_generator.log
echo %date% %time% >> run_content_generator.log
echo ===================================== >> run_content_generator.log

pause
