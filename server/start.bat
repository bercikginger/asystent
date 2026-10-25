@echo off
cd /d "%~dp0"
if not exist .venv (
  echo [i] Tworze srodowisko i instaluje paczki - tylko za pierwszym razem...
  python -m venv .venv
  .venv\Scripts\python -m pip install --upgrade pip
)
rem doinstaluj brakujace paczki po aktualizacji
.venv\Scripts\python -m pip install -q -r requirements.txt
.venv\Scripts\python server.py
pause
