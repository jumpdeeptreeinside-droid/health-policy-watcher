@echo off
REM ニュース自動収集スクリプトを実行
REM Windowsタスクスケジューラから実行されます

cd /d "%~dp0"
call venv\Scripts\activate.bat
python src\fetch_news_to_notion.py

REM ログファイルに実行時刻を記録
echo. >> run_fetch_news.log
echo ===================================== >> run_fetch_news.log
echo 実行日時: %date% %time% >> run_fetch_news.log
echo ===================================== >> run_fetch_news.log

pause
