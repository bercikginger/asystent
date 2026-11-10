@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ==================================================
echo   ASYSTENT - start serwera
echo ==================================================

where python >nul 2>nul
if errorlevel 1 (
  echo [!] Nie widze Pythona. Zainstaluj Python 3.12 z python.org
  echo     i ZAZNACZ "Add python.exe to PATH". Potem uruchom ten plik ponownie.
  pause
  exit /b
)
python --version

if not exist .venv\Scripts\python.exe (
  echo.
  echo [1/3] Tworze srodowisko Pythona...
  python -m venv .venv
  if errorlevel 1 (
    echo [!] Nie udalo sie utworzyc srodowiska.
    pause
    exit /b
  )
  .venv\Scripts\python -m pip install --upgrade pip
)

echo.
echo [2/3] Instaluje / sprawdzam paczki. Za pierwszym razem to trwa 5-10 minut,
echo       widac postep ponizej - nie zamykaj okna.
.venv\Scripts\python -m pip install -r requirements.txt
if errorlevel 1 (
  echo [!] Instalacja paczek nie przeszla - zrob zdjecie bledu powyzej.
  pause
  exit /b
)

echo.
echo [3/3] Uruchamiam serwer (pierwszy raz pobiera Whisper ~500 MB i glos ~60 MB)...
.venv\Scripts\python -u server.py
echo.
echo [!] Serwer sie zatrzymal - jesli widzisz blad powyzej, zrob zdjecie.
pause
