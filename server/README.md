# Asystent głosowy – laptop + telefon

Telefon nagrywa głos → laptop rozpoznaje mowę (Whisper) → lokalny model odpowiada (LM Studio / Ollama) → laptop generuje głos (Piper) → telefon odtwarza.

## 1. Model na laptopie (LM Studio)
1. Załaduj model, na start mały 3–4B (szybka rozmowa).
2. Zakładka **Developer** → **Start Server** (port 1234).

Ollama zamiast LM Studio: przed startem wpisz w cmd `set LLM_URL=http://localhost:11434/v1`.

## 2. Serwer
1. Zainstaluj **Python 3.12** (zaznacz „Add to PATH”).
2. Kliknij dwa razy **start.bat**.
   Pierwsze uruchomienie instaluje paczki i pobiera Whisper (~500 MB) oraz polski głos (~60 MB).
3. Test na laptopie: otwórz `http://localhost:8000`.

## 3. Połączenie z telefonem (Tailscale)
Mikrofon w przeglądarce działa tylko przez HTTPS. Tailscale daje HTTPS i dostęp spoza domu.
1. Zainstaluj Tailscale na laptopie i telefonie, zaloguj się na to samo konto.
2. Na laptopie w cmd: `tailscale serve --bg 8000`
   (za pierwszym razem poprosi o włączenie HTTPS w panelu – kliknij link).
3. Dostaniesz adres `https://nazwa-laptopa.xxxx.ts.net`.

## 4. Apka na telefonie
1. Otwórz ten adres w Chrome na telefonie.
2. Menu ⋮ → **Zainstaluj aplikację** (albo „Dodaj do ekranu głównego”).
3. Ikona „Asystent” pojawi się na ekranie głównym.

## Używanie
- **Mikrofon:** stuknij i mów, sam wykryje koniec wypowiedzi.
- **Rozmowa:** po odpowiedzi automatycznie słucha dalej (prawie hands-free).
- Stuknięcie mikrofonu w trakcie mówienia przerywa asystenta.
- **Nowa:** czyści pamięć rozmowy.

## Ustawienia (zmienne środowiskowe przed `start.bat`)
| Zmienna | Domyślnie | Co robi |
|---|---|---|
| `LLM_URL` | `http://localhost:1234/v1` | adres LM Studio / Ollama |
| `LLM_MODEL` | pierwszy załadowany | nazwa modelu |
| `WHISPER_MODEL` | `small` | `base` = szybciej, `medium` = dokładniej |
| `SYSTEM_PROMPT` | krótki asystent PL | charakter asystenta |

## Później: prawdziwe APK
Ta sama strona da się opakować w APK (Bubblewrap / TWA albo Capacitor) bez zmian w kodzie serwera.
