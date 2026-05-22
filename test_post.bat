@echo off
cd /d %~dp0
echo Тестовый пост (публикует сейчас)...
venv\Scripts\python bot.py --test
pause
