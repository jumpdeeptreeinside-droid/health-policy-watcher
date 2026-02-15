@echo off
REM Notionステータス自動更新スクリプトを実行
REM Windowsタスクスケジューラから実行されます

cd /d "%~dp0"
call venv\Scripts\activate.bat
python src\notion_status_automation.py

REM ログファイルに実行時刻を記録
echo. >> run_status_automation.log
echo ===================================== >> run_status_automation.log
echo 実行日時: %date% %time% >> run_status_automation.log
echo ===================================== >> run_status_automation.log

pause
