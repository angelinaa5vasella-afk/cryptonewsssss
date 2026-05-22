@echo off
cd /d %~dp0
if not exist venv (
    echo Создаю виртуальное окружение...
    python -m venv venv
    venv\Scripts\pip install -r requirements.txt
)
echo Запускаю бота...
venv\Scripts\python bot.py
pause
