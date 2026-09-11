@echo off
echo Starting Telegram YouTube Gate...
cd /d "%~dp0"
call .venv\Scripts\activate
python run.py
pause
