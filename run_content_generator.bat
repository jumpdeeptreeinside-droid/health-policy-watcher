@echo off
REM Notion監視型コンテンツ自動生成スクリプトを実行
REM Windowsタスクスケジューラから実行されます

cd /d "%~dp0"
call venv\Scripts\activate.bat
python src\notion_content_generator.py

REM ログファイルに実行時刻を記録
echo. >> run_content_generator.log
echo ===================================== >> run_content_generator.log
echo 実行日時: %date% %time% >> run_content_generator.log
echo ===================================== >> run_content_generator.log

pause
