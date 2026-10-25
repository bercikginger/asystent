# Asystent głosowy – serwer na laptopie (v1.4)

Telefon nagrywa głos → laptop rozpoznaje mowę (Whisper) → lokalny model odpowiada i używa narzędzi → laptop generuje głos (Piper) → telefon mówi **zdanie po zdaniu**.

Model sam korzysta z narzędzi: wyszukiwanie w internecie, pamięć o Tobie, minutnik / budzik / otwieranie aplikacji / muzyka w telefonie, programy na Arduino i ESP, Home Assistant.

## 1. Model (LM Studio)
1. Załaduj model, najlepiej taki, który obsługuje narzędzia (np. Qwen 3/3.5, 4B lub większy).
2. **Developer** → **Start Server** (port 1234).

Ollama zamiast LM Studio: `set LLM_URL=http://localhost:11434/v1` przed startem.

## 2. Serwer
1. Zainstaluj **Python 3.12** (zaznacz „Add to PATH”).
2. Kliknij **start.bat**. Za pierwszym razem instaluje paczki, Whisper (~500 MB) i polski głos (~60 MB).
3. Test: `http://localhost:8000`.

## 3. Telefon (Tailscale)
1. Tailscale na laptopie i telefonie, to samo konto.
2. Na laptopie: `tailscale serve --bg 8000` → adres `https://nazwa-laptopa.xxxx.ts.net`.
3. W apce: ⚙ → wpisz adres → **ZAPISZ**.

## 4. Arduino / ESP
Laptop kompiluje programy przez **arduino-cli**.

1. Pobierz arduino-cli dla Windows (arduino.cc → Arduino CLI) i wrzuć `arduino-cli.exe` do tego folderu.
2. W cmd w tym folderze:
   ```
   arduino-cli core update-index
   arduino-cli core install arduino:avr
   arduino-cli config init
   arduino-cli config add board_manager.additional_urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
   arduino-cli config add board_manager.additional_urls https://arduino.esp8266.com/stable/package_esp8266com_index.json
   arduino-cli core update-index
   arduino-cli core install esp32:esp32
   arduino-cli core install esp8266:esp8266
   ```
   (ESP8266 tylko jeśli go używasz. Rdzenie z Arduino IDE 2 też działają.)
3. Biblioteki: `arduino-cli lib install "Nazwa Biblioteki"`.
4. W apce wybierz płytkę w ⚙ → ARDUINO / ESP.

**Wgrywanie z telefonu (OTG):** podłącz płytkę do telefonu kablem/przejściówką OTG, w karcie programu kliknij **⚡ WGRAJ PRZEZ USB** i zezwól na dostęp do USB.
Obsługiwane: Arduino Uno, Nano (też stary bootloader), ESP32, ESP8266, ESP32-S3 i ESP32-C3 (próbnie).
Jeśli ESP nie wchodzi w tryb wgrywania: przytrzymaj **BOOT** podczas startu wgrywania.
Mega i Leonardo – tylko przez laptopa (**WGRAJ Z LAPTOPA**, płytka w USB laptopa).

## 5. Claude jako mózg (opcjonalnie)
1. Załóż klucz API na console.anthropic.com (płatne za zużycie).
2. W apce: ⚙ → **CLAUDE · UPLINK** → wklej klucz → **POŁĄCZ**. Klucz zapisuje się tylko na laptopie (`data/config.json`).
3. Przełączasz mózg, stukając **◈ LOKAL / ◈ CLAUDE** pod nazwą w nagłówku.

Domyślny model to szybki Haiku (dobry do rozmowy głosowej); mocniejszy wybierzesz w ⚙.
Claude ma własną wyszukiwarkę internetową (osobno płatną za wyszukanie) i te same narzędzia: pamięć, akcje telefonu, Arduino, dom.
Gdy Claude jest niedostępny (brak sieci, limit, zły klucz), odpowiada model lokalny.
Zamiast wklejania klucza możesz ustawić `set ANTHROPIC_API_KEY=sk-ant-...` przed startem.

## 6. Słowo wybudzające (opcjonalnie)
Kliknij **wake.bat**, powiedz **„hey Jarvis”** i pytanie – odpowiedź z głośnika laptopa.
Polskiego „hej asystent” nie ma gotowego; trzeba by wytrenować własny model openWakeWord.

## 7. Home Assistant (opcjonalnie)
Przed startem:
```
set HA_URL=http://homeassistant.local:8123
set HA_TOKEN=twój_token_z_profilu_HA
```
Potem mówisz np. „zapal światło w salonie”.

## Apka – słuchawki i auto
- **Przycisk na słuchawkach / kierownicy** = mów (gdy apka jest otwarta).
- **Mikrofon BT** w ⚙ – mówisz do słuchawek lub zestawu w aucie.
- **Asystent systemowy:** Ustawienia telefonu → Aplikacje → Domyślne aplikacje → Asystent cyfrowy → Asystent.
  Potem przytrzymanie home/zasilania otwiera apkę i od razu słucha (zależy od producenta telefonu).
- **Głos z laptopa** w ⚙ – odpowiedzi z głośnika laptopa.

## Ustawienia (zmienne środowiskowe przed `start.bat`)
| Zmienna | Domyślnie | Co robi |
|---|---|---|
| `LLM_URL` | `http://localhost:1234/v1` | adres LM Studio / Ollama |
| `LLM_MODEL` | pierwszy załadowany | nazwa modelu |
| `WHISPER_MODEL` | `small` | `base` = szybciej, `medium` = dokładniej |
| `ARDUINO_CLI` | szuka w PATH i w tym folderze | ścieżka do arduino-cli |
| `HA_URL`, `HA_TOKEN` | puste | Home Assistant |
| `ANTHROPIC_API_KEY` | pusty | klucz Claude (zamiast wklejania w apce) |
| `ANTHROPIC_WORKSPACE_ID` | pusty | tylko dla klucza bez przypisanego workspace |
| `SYSTEM_PROMPT` | krótki asystent PL | charakter asystenta |

Pamięć asystenta, programy Arduino i klucz Claude są w folderze `data/` – nie udostępniaj go.
