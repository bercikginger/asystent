@echo off
cd /d "%~dp0"
if not exist .venv (
  echo [i] Tworze srodowisko i instaluje paczki - tylko za pierwszym razem...
  python -m venv .venv
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\python -m pip install -r requirements.txt
)
.venv\Scripts\python server.py
pause
