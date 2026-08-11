@echo off
setlocal
cd /d "%~dp0"
if not exist .venv (
  py -m venv .venv
)
call .venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
start "" cmd /c "timeout /t 2 /nobreak >nul & start http://127.0.0.1:8000"
python run.py
