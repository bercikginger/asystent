@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ==================================================
echo   ASYSTENT - start serwera
echo ==================================================

rem Paczki do mowy (faster-whisper / av) maja gotowe pliki dla Pythona 3.12 - na nowszym kompilacja sie wywala.
set PY=
py -3.12 --version >nul 2>nul && set PY=py -3.12
if not defined PY (
  where python >nul 2>nul && set PY=python
)
if not defined PY (
  echo [!] Nie widze Pythona. Zainstaluj Python 3.12 z python.org
  echo     i ZAZNACZ "Add python.exe to PATH". Potem uruchom ten plik ponownie.
  pause
  exit /b
)
for /f "tokens=2" %%v in ('%PY% --version') do set PYV=%%v
echo Python: %PYV%
echo %PYV% | findstr /b "3.12." >nul
if errorlevel 1 (
  echo.
  echo [!] Potrzebny jest Python 3.12, a masz %PYV%.
  echo     Pobierz z python.org: Downloads - Windows - Python 3.12.x - Windows installer 64-bit,
  echo     zainstaluj obok obecnego, potem usun folder .venv w tym folderze i uruchom ten plik ponownie.
  pause
  exit /b
)

rem stare srodowisko z innym Pythonem -> usun i zrob od nowa
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python --version 2>nul | findstr /c:"Python 3.12." >nul
  if errorlevel 1 (
    echo [i] Usuwam stare srodowisko z inna wersja Pythona...
    rmdir /s /q .venv
  )
)

if not exist .venv\Scripts\python.exe (
  echo.
  echo [1/3] Tworze srodowisko Pythona...
  %PY% -m venv .venv
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
.venv\Scripts\python -m pip install --prefer-binary --only-binary=av,numpy,onnxruntime -r requirements.txt
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
