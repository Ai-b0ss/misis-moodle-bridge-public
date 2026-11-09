@echo off
chcp 65001 >nul
cd /d "%~dp0"
"C:\Users\user\AppData\Local\Programs\Python\Python313\python.exe" "%~dp0moodle_client.py" browser-login
exit /b %errorlevel%
