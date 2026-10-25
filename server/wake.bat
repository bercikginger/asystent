@echo off
cd /d "%~dp0"
if not exist .venv (
  echo [!] Najpierw uruchom start.bat
  pause
  exit /b
)
if not exist .venv\wake-ok (
  echo [i] Instaluje slowo wybudzajace - tylko za pierwszym razem...
  .venv\Scripts\python -m pip install -r requirements-wake.txt && echo ok> .venv\wake-ok
)
.venv\Scripts\python wake.py
pause
