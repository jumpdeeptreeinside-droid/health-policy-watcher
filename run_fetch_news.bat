@echo off
cd /d "%~dp0"
call venv\Scripts\activate.bat
python src\fetch_news_to_notion.py

echo. >> run_fetch_news.log
echo ===================================== >> run_fetch_news.log
echo %date% %time% >> run_fetch_news.log
echo ===================================== >> run_fetch_news.log

pause
