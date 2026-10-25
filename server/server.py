"""Asystent głosowy – serwer na laptopie (v1.3).

Telefon -> nagranie -> Whisper (STT) -> lokalny LLM (LM Studio / Ollama) z narzędziami -> Piper (TTS) -> telefon.
Odpowiedź jest strumieniowana zdanie po zdaniu (/api/stream), więc asystent zaczyna mówić od razu.

Narzędzia modelu: wyszukiwanie w sieci, pamięć, akcje w telefonie (minutnik, budzik, apki, muzyka),
programy na Arduino/ESP (kompilacja przez arduino-cli, wgranie po potwierdzeniu w apce), Home Assistant.

Uruchom: python server.py
"""
import asyncio
import base64
import io
import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import wave
from datetime import datetime
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from faster_whisper import WhisperModel
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

BASE = Path(__file__).parent
DATA = BASE / "data"
SKETCHES = DATA / "sketches"
MEMORY_FILE = DATA / "memory.json"
SKETCHES.mkdir(parents=True, exist_ok=True)

# --- Konfiguracja (zmienne środowiskowe) ---
# LM Studio: http://localhost:1234/v1   |   Ollama: http://localhost:11434/v1
LLM_URL = os.getenv("LLM_URL", "http://localhost:1234/v1").rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL", "")  # puste = pierwszy załadowany model
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")  # tiny / base / small / medium
PIPER_VOICE = os.getenv("PIPER_VOICE", str(BASE / "pl_PL-gosia-medium.onnx"))
PORT = int(os.getenv("PORT", "8000"))
HA_URL = os.getenv("HA_URL", "").rstrip("/")  # np. http://homeassistant.local:8123
HA_TOKEN = os.getenv("HA_TOKEN", "")  # długoterminowy token z profilu HA
MAX_TURNS = 20
MAX_ROUNDS = 5
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "Jesteś osobistym asystentem głosowym. Odpowiadasz po polsku, krótko i konkretnie, "
    "zwykle w 1-3 zdaniach. Nie używaj markdownu, list ani emoji, bo odpowiedź jest czytana na głos.",
)


def find_arduino_cli() -> str | None:
    for cand in (os.getenv("ARDUINO_CLI", ""), "arduino-cli", str(BASE / "arduino-cli.exe"), str(BASE / "arduino-cli")):
        if cand and (shutil.which(cand) or Path(cand).is_file()):
            return shutil.which(cand) or cand
    return None


ARDUINO_CLI = find_arduino_cli()
BOARDS = [
    {"name": "ESP32 Dev Module", "fqbn": "esp32:esp32:esp32"},
    {"name": "ESP32-S3", "fqbn": "esp32:esp32:esp32s3"},
    {"name": "ESP32-C3", "fqbn": "esp32:esp32:esp32c3"},
    {"name": "ESP8266 NodeMCU", "fqbn": "esp8266:esp8266:nodemcuv2"},
    {"name": "Arduino Uno", "fqbn": "arduino:avr:uno"},
    {"name": "Arduino Nano", "fqbn": "arduino:avr:nano"},
    {"name": "Arduino Nano (stary bootloader)", "fqbn": "arduino:avr:nano:cpu=atmega328old"},
    {"name": "Arduino Mega 2560", "fqbn": "arduino:avr:mega"},
    {"name": "Arduino Leonardo", "fqbn": "arduino:avr:leonardo"},
]

# --- Mowa: Whisper ---
print(f"[i] Ładuję Whisper '{WHISPER_MODEL}' na CPU...")
whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")

# --- Głos: Piper ---
VOICE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/pl/pl_PL/gosia/medium/pl_PL-gosia-medium.onnx"


def ensure_voice() -> None:
    """Pobiera polski głos Piper przy pierwszym uruchomieniu (~60 MB)."""
    onnx = Path(PIPER_VOICE)
    if onnx.exists() or onnx.name != "pl_PL-gosia-medium.onnx":
        return
    print("[i] Pobieram polski głos Piper (gosia, ~60 MB)...")
    for url, dest in ((VOICE_URL, onnx), (VOICE_URL + ".json", Path(str(onnx) + ".json"))):
        tmp = Path(str(dest) + ".part")
        with httpx.stream("GET", url, follow_redirects=True, timeout=300) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
        tmp.replace(dest)


piper_voice = None
try:
    from piper import PiperVoice

    ensure_voice()
    if Path(PIPER_VOICE).exists():
        piper_voice = PiperVoice.load(PIPER_VOICE)
        print(f"[i] Piper załadowany: {PIPER_VOICE}")
    else:
        print(f"[!] Brak pliku głosu {PIPER_VOICE} – telefon użyje własnego TTS.")
except Exception as e:  # noqa: BLE001
    print(f"[!] Piper niedostępny ({e}) – telefon użyje własnego TTS.")

# --- Odtwarzanie na głośniku laptopa (opcjonalne) ---
try:
    import numpy as np
    import sounddevice as sd
except Exception:  # noqa: BLE001
    sd = None
_play_q: "queue.Queue[bytes]" = queue.Queue()
_playing = {"on": False}


def _player() -> None:
    while True:
        wav_bytes = _play_q.get()
        _playing["on"] = True
        try:
            with wave.open(io.BytesIO(wav_bytes)) as wf:
                rate, ch = wf.getframerate(), wf.getnchannels()
                data = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
            if ch > 1:
                data = data.reshape(-1, ch)
            sd.play(data, rate)
            sd.wait()
        except Exception as e:  # noqa: BLE001
            print(f"[!] Odtwarzanie na laptopie: {e}")
        finally:
            _playing["on"] = not _play_q.empty()


if sd is not None:
    threading.Thread(target=_player, daemon=True).start()

# --- Wyszukiwarka (DuckDuckGo, bez klucza) ---
try:
    from ddgs import DDGS
except ImportError:
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        DDGS = None

FORCE_SEARCH = re.compile(
    r"\b(wyszukaj|poszukaj|wygoogluj|sprawd[źz] w (internecie|sieci)|znajd[źz] w (internecie|sieci)|"
    r"co nowego|najnowsze|wiadomo[śs]ci|pogod\w*|kurs\w*)\b",
    re.I,
)
DNI = ["poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela"]


try:
    import trafilatura  # wyciąga właściwą treść artykułu ze strony
except ImportError:  # pragma: no cover
    trafilatura = None

NEWS_HINT = re.compile(r"\b(dzi[śs]|dzisiaj|wczoraj|najnowsz\w*|wiadomo[śs]\w*|news\w*|aktualn\w*|teraz|w tym tygodniu|wynik\w*)\b", re.I)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
_search_cache: dict[str, tuple[float, object]] = {}
_web: dict[str, httpx.AsyncClient] = {}


def cache_get(key: str, ttl: float):
    v = _search_cache.get(key)
    return v[1] if v and time.time() - v[0] < ttl else None


def cache_put(key: str, val) -> None:
    if len(_search_cache) > 300:
        for k in sorted(_search_cache, key=lambda k: _search_cache[k][0])[:100]:
            _search_cache.pop(k, None)
    _search_cache[key] = (time.time(), val)


def web_client() -> httpx.AsyncClient:
    """Jeden klient dla wszystkich pobrań – ponowne użycie połączeń."""
    if "c" not in _web:
        _web["c"] = httpx.AsyncClient(timeout=httpx.Timeout(7, connect=4), follow_redirects=True,
                                      headers={"User-Agent": UA, "Accept-Language": "pl,en;q=0.8"},
                                      limits=httpx.Limits(max_connections=20))
    return _web["c"]


def ddg_hits(query: str, news: bool, n: int = 8) -> list[dict]:
    if DDGS is None:
        return []
    out = []
    try:
        with DDGS() as ddg:
            if news:
                for h in ddg.news(query, region="pl-pl", max_results=n):
                    out.append({"title": h.get("title", ""), "url": h.get("url") or h.get("href", ""),
                                "body": h.get("body", ""), "date": (h.get("date") or "")[:10], "source": h.get("source", "")})
            for h in ddg.text(query, region="pl-pl", max_results=n):
                out.append({"title": h.get("title", ""), "url": h.get("href") or h.get("url", ""),
                            "body": h.get("body", ""), "date": "", "source": ""})
    except Exception as e:  # noqa: BLE001
        print(f"[!] wyszukiwarka: {e}")
    return [h for h in out if h["url"].startswith("http")]


def domain(url: str) -> str:
    m = re.match(r"https?://(?:www\.)?([^/:]+)", url)
    return m.group(1) if m else url


def html_to_text(html: str) -> str:
    if trafilatura is not None:
        try:
            t = trafilatura.extract(html, include_comments=False, include_tables=True, favor_precision=True)
            if t:
                return t
        except Exception:  # noqa: BLE001
            pass
    html = re.sub(r"(?is)<(script|style|noscript|nav|footer|header|aside).*?</\1>", " ", html)
    t = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"\n\s*\n+", "\n", t)).strip()


async def fetch_text(url: str, limit: int = 60000) -> str:
    hit = cache_get("page:" + url, 1800)
    if hit is not None:
        return hit
    try:
        async with web_client().stream("GET", url) as r:
            if r.status_code >= 400 or "html" not in r.headers.get("content-type", "html"):
                return ""
            buf = b""
            async for chunk in r.aiter_bytes():
                buf += chunk
                if len(buf) > 2_000_000:
                    break
        text = await run_in_threadpool(html_to_text, buf.decode(r.encoding or "utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return ""
    text = text[:limit]
    cache_put("page:" + url, text)
    return text


def stems(text: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[\wąćęłńóśźż]{3,}", text.lower())}


def best_passages(text: str, query: str, limit: int = 1400) -> str:
    """Wybiera z tekstu strony akapity najbardziej pasujące do pytania (w oryginalnej kolejności)."""
    paras = [p.strip() for p in re.split(r"\n+", text) if len(p.strip()) > 40]
    if not paras:
        return text[:limit]
    q = stems(query)
    scored = sorted(range(len(paras)), key=lambda i: -(len(q & stems(paras[i])) + (0.5 if re.search(r"\d", paras[i]) else 0)))
    chosen, total = [], 0
    for i in scored:
        if total > limit:
            break
        chosen.append(i)
        total += len(paras[i])
    return "\n".join(paras[i] for i in sorted(chosen))[: limit + 300]


async def deep_search(query: str, mode: str = "auto", pages: int = 4) -> tuple[str, list[dict]]:
    """Wyszukiwanie + równoległe czytanie najlepszych stron. Zwraca (kontekst dla modelu, źródła)."""
    news = mode == "news" or (mode == "auto" and bool(NEWS_HINT.search(query)))
    key = f"s:{news}:{query.lower().strip()}"
    hit = cache_get(key, 600 if news else 3600)
    if hit is not None:
        return hit
    hits = await run_in_threadpool(ddg_hits, query, news)
    if not hits:
        return ("Brak wyników (albo wyszukiwarka niedostępna).", [])
    seen, top = set(), []
    for h in hits:  # najlepsze wyniki, po jednym z domeny
        d = domain(h["url"])
        if d not in seen:
            seen.add(d)
            top.append(h)
    picked = top[:pages]
    texts = await asyncio.gather(*(fetch_text(h["url"]) for h in picked))
    parts = []
    for i, (h, t) in enumerate(zip(picked, texts), 1):
        body = best_passages(t, query) if len(t) > 200 else h["body"]
        meta = ", ".join(x for x in (domain(h["url"]), h["date"]) if x)
        parts.append(f"[{i}] {h['title']} ({meta})\n{body}")
    for h in top[pages:pages + 4]:
        parts.append(f"- {h['title']} ({domain(h['url'])}): {h['body']}")
    ctx = (f"Wyniki wyszukiwania dla: {query} (pobrano {datetime.now():%d.%m.%Y %H:%M})\n\n" + "\n\n".join(parts))[:9000]
    sources = [{"title": h["title"][:90], "url": h["url"]} for h in picked]
    cache_put(key, (ctx, sources))
    return ctx, sources


async def read_url(url: str) -> str:
    if not re.match(r"https?://", url):
        return "Podaj pełny adres strony (http/https)."
    t = await fetch_text(url, limit=12000)
    return t[:8000] if t else "Nie udało się pobrać strony."


# --- Pogoda (Open-Meteo, bez klucza) ---
WMO = {0: "bezchmurnie", 1: "prawie bezchmurnie", 2: "częściowe zachmurzenie", 3: "pochmurno", 45: "mgła", 48: "szadź",
       51: "lekka mżawka", 53: "mżawka", 55: "gęsta mżawka", 61: "słaby deszcz", 63: "deszcz", 65: "ulewa",
       66: "marznący deszcz", 67: "silny marznący deszcz", 71: "słaby śnieg", 73: "śnieg", 75: "intensywny śnieg",
       77: "ziarna śniegu", 80: "przelotny deszcz", 81: "przelotne opady", 82: "gwałtowne ulewy", 85: "przelotny śnieg",
       86: "intensywny przelotny śnieg", 95: "burza", 96: "burza z gradem", 99: "silna burza z gradem"}


async def weather(city: str, days: int = 3) -> tuple[str, list[dict]]:
    city = (city or os.getenv("HOME_CITY", "")).strip()
    if not city:
        return "Nie wiem, dla jakiego miasta – zapytaj użytkownika.", []
    key = f"w:{city.lower()}:{days}"
    hit = cache_get(key, 900)
    if hit is not None:
        return hit
    c = web_client()
    try:
        g = (await c.get("https://geocoding-api.open-meteo.com/v1/search",
                         params={"name": city, "count": 1, "language": "pl"})).json().get("results") or []
        if not g:
            return f"Nie znalazłem miejscowości „{city}”. Podaj nazwę w mianowniku, np. Kraków.", []
        loc = g[0]
        f = (await c.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": loc["latitude"], "longitude": loc["longitude"], "timezone": "auto", "forecast_days": max(1, min(days, 7)),
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,precipitation,relative_humidity_2m",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,weather_code",
        })).json()
    except Exception as e:  # noqa: BLE001
        return f"Serwis pogodowy niedostępny: {e}", []
    cur, d = f.get("current", {}), f.get("daily", {})
    lines = [f"Pogoda: {loc['name']}, {loc.get('admin1', '')} ({loc.get('country', '')}). "
             f"Teraz: {cur.get('temperature_2m')}°C (odczuwalna {cur.get('apparent_temperature')}°C), "
             f"{WMO.get(cur.get('weather_code'), '?')}, wiatr {cur.get('wind_speed_10m')} km/h, wilgotność {cur.get('relative_humidity_2m')}%."]
    for i, day in enumerate(d.get("time", [])):
        lines.append(f"{day}: {d['temperature_2m_min'][i]}…{d['temperature_2m_max'][i]}°C, "
                     f"{WMO.get(d['weather_code'][i], '?')}, szansa opadów {d['precipitation_probability_max'][i]}%, "
                     f"opad {d['precipitation_sum'][i]} mm")
    out = ("\n".join(lines), [{"title": f"Open-Meteo: {loc['name']}", "url": "https://open-meteo.com/"}])
    cache_put(key, out)
    return out


# --- Pamięć długoterminowa ---
_mem_lock = threading.Lock()


def load_memory() -> list[str]:
    try:
        return json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []


def save_memory(items: list[str]) -> None:
    MEMORY_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")


# --- Home Assistant (opcjonalne) ---
_ha_cache = {"t": 0.0, "list": ""}
HA_DOMAINS = ("light", "switch", "fan", "cover", "climate", "media_player", "scene", "script")


async def ha_entities(client: httpx.AsyncClient) -> str:
    if time.time() - _ha_cache["t"] < 300:
        return _ha_cache["list"]
    try:
        r = await client.get(f"{HA_URL}/api/states", headers={"Authorization": f"Bearer {HA_TOKEN}"}, timeout=10)
        r.raise_for_status()
        items = [
            f"{s['entity_id']} ({s.get('attributes', {}).get('friendly_name', '')}, {s.get('state')})"
            for s in r.json()
            if s["entity_id"].split(".")[0] in HA_DOMAINS
        ][:80]
        _ha_cache.update(t=time.time(), list="; ".join(items))
    except Exception as e:  # noqa: BLE001
        print(f"[!] Home Assistant: {e}")
    return _ha_cache["list"]


# --- Arduino / ESP przez arduino-cli ---
def arduino_run(args: list[str], timeout: int) -> tuple[bool, str]:
    try:
        p = subprocess.run([ARDUINO_CLI, *args], capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        return p.returncode == 0, (p.stdout + "\n" + p.stderr).strip()[-4000:]
    except subprocess.TimeoutExpired:
        return False, "Przekroczony czas."
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def arduino_ports() -> list[dict]:
    if not ARDUINO_CLI:
        return []
    ok, out = arduino_run(["board", "list", "--format", "json"], 30)
    if not ok:
        return []
    try:
        data = json.loads(out[out.find("{") if out.lstrip().startswith("{") else out.find("["):])
    except json.JSONDecodeError:
        return []
    entries = data.get("detected_ports", []) if isinstance(data, dict) else data
    ports = []
    for e in entries:
        port = e.get("port", {})
        if port.get("protocol") not in (None, "serial"):
            continue
        boards = e.get("matching_boards") or e.get("boards") or []
        ports.append({
            "port": port.get("address", ""),
            "label": port.get("protocol_label") or port.get("label", ""),
            "boards": [{"name": b.get("name", ""), "fqbn": b.get("fqbn", "")} for b in boards],
        })
    return [p for p in ports if p["port"]]


def sketch_dir(sid: str) -> Path:
    return SKETCHES / sid


def build_dir(sid: str, fqbn: str) -> Path:
    return sketch_dir(sid) / "build" / re.sub(r"[^A-Za-z0-9]+", "_", fqbn)


def arduino_save(code: str) -> str:
    sid = "s" + uuid.uuid4().hex[:8]
    d = sketch_dir(sid)
    d.mkdir(parents=True)
    (d / f"{sid}.ino").write_text(code, encoding="utf-8")
    return sid


def arduino_compile(sid: str, fqbn: str) -> tuple[bool, str]:
    return arduino_run(["compile", "--fqbn", fqbn, "--build-path", str(build_dir(sid, fqbn)), str(sketch_dir(sid))], 900)


def arduino_upload(sid: str, fqbn: str, port: str) -> tuple[bool, str]:
    ok, log = arduino_compile(sid, fqbn)
    if not ok:
        return False, "Kompilacja nie przeszła:\n" + log
    ok, log2 = arduino_run(["upload", "-p", port, "--fqbn", fqbn, "--input-dir", str(build_dir(sid, fqbn)),
                            str(sketch_dir(sid))], 300)
    return ok, log2


# --- Narzędzia modelu ---
def tool(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}}


async def tools_for(client: httpx.AsyncClient) -> list[dict]:
    t = [
        tool("remember", "Zapamiętuje na stałe fakt o użytkowniku, gdy o to prosi albo poda ważną informację o sobie.",
             {"fact": {"type": "string"}}, ["fact"]),
        tool("forget_memory", "Usuwa z pamięci fakty zawierające podany tekst.", {"text": {"type": "string"}}, ["text"]),
        tool("phone_action",
             "Wykonuje akcję w telefonie użytkownika: timer (minutnik, podaj seconds), alarm (budzik, hour i minute), "
             "open_app (otwórz aplikację, podaj app), media (sterowanie muzyką, command: play_pause, next, previous).",
             {"action": {"type": "string", "enum": ["timer", "alarm", "open_app", "media"]},
              "seconds": {"type": "integer"}, "hour": {"type": "integer"}, "minute": {"type": "integer"},
              "label": {"type": "string"}, "app": {"type": "string"},
              "command": {"type": "string", "enum": ["play_pause", "next", "previous"]}},
             ["action"]),
    ]
    if DDGS is not None:
        t.append(tool("web_search",
                      "Szuka w internecie i czyta treść najlepszych stron. Używaj do wszystkiego, co aktualne albo "
                      "niepewne: wiadomości, ceny, wyniki, specyfikacje, ludzie, firmy. Zadawaj konkretne zapytania; "
                      "przy złożonym pytaniu wywołaj kilka razy z różnymi zapytaniami. mode=news dla świeżych wiadomości.",
                      {"query": {"type": "string"}, "mode": {"type": "string", "enum": ["auto", "news"]}}, ["query"]))
    t.append(tool("read_url", "Pobiera i czyta treść konkretnej strony internetowej (pełny adres URL).",
                  {"url": {"type": "string"}}, ["url"]))
    t.append(tool("weather", "Aktualna pogoda i prognoza na kilka dni dla miejscowości (nazwa w mianowniku, np. Kraków).",
                  {"city": {"type": "string"}, "days": {"type": "integer"}}, ["city"]))
    if ARDUINO_CLI:
        t.append(tool("arduino_sketch",
                      "Pisze i kompiluje kompletny program (szkic .ino) na płytkę Arduino lub ESP podpiętą do laptopa. "
                      "Podaj pełny kod z setup() i loop(). Program wgrywa użytkownik przyciskiem w apce. "
                      "Jeśli kompilacja zwróci błąd, popraw kod i wywołaj narzędzie ponownie.",
                      {"code": {"type": "string"}, "summary": {"type": "string", "description": "Krótko, co robi program"}},
                      ["code"]))
    if HA_URL and HA_TOKEN:
        ents = await ha_entities(client)
        t.append(tool("home_assistant",
                      "Steruje domem przez Home Assistant (np. light/turn_on, switch/turn_off, scene/turn_on). "
                      f"Dostępne urządzenia: {ents or 'brak listy'}",
                      {"domain": {"type": "string"}, "service": {"type": "string"}, "entity_id": {"type": "string"}},
                      ["domain", "service", "entity_id"]))
    return t


class Ctx:
    def __init__(self, board: str):
        self.board = board
        self.events: list[dict] = []  # zdarzenia do wysłania telefonowi (akcje, karty Arduino)
        self.arduino_last: dict | None = None
        self.arduino_ok = False


TOOL_STATUS = {
    "web_search": "przeszukuję sieć i czytam strony…", "read_url": "czytam stronę…", "weather": "sprawdzam pogodę…",
    "arduino_sketch": "kompiluję program…", "remember": "zapisuję w pamięci…",
    "forget_memory": "czyszczę pamięć…", "phone_action": "wysyłam do telefonu…", "home_assistant": "steruję domem…",
}


async def exec_tool(name: str, args: dict, ctx: Ctx, client: httpx.AsyncClient) -> str:
    if name == "web_search":
        q = str(args.get("query") or "")
        res, src = await deep_search(q, str(args.get("mode") or "auto"))
        ctx.events.append({"type": "search", "query": q, "sources": src})
        return res
    if name == "read_url":
        url = str(args.get("url") or "")
        ctx.events.append({"type": "search", "query": "czytam " + domain(url), "sources": [{"title": domain(url), "url": url}]})
        return await read_url(url)
    if name == "weather":
        city = str(args.get("city") or "")
        res, src = await weather(city, int(args.get("days") or 3))
        ctx.events.append({"type": "search", "query": "pogoda " + city, "sources": src})
        return res
    if name == "remember":
        fact = str(args.get("fact") or "").strip()
        if fact:
            with _mem_lock:
                items = load_memory()
                if fact not in items:
                    items.append(fact)
                    save_memory(items[-200:])
            ctx.events.append({"type": "memory", "text": "zapamiętano: " + fact})
        return "Zapamiętane."
    if name == "forget_memory":
        txt = str(args.get("text") or "").lower()
        with _mem_lock:
            items = load_memory()
            left = [i for i in items if txt not in i.lower()] if txt else items
            save_memory(left)
        ctx.events.append({"type": "memory", "text": f"usunięto z pamięci: {len(items) - len(left)}"})
        return f"Usunięto {len(items) - len(left)} wpisów."
    if name == "phone_action":
        ctx.events.append({"type": "phone", **args})
        return "Polecenie wysłane do telefonu."
    if name == "home_assistant":
        try:
            r = await client.post(f"{HA_URL}/api/services/{args.get('domain')}/{args.get('service')}",
                                  headers={"Authorization": f"Bearer {HA_TOKEN}"},
                                  json={"entity_id": args.get("entity_id")}, timeout=15)
            r.raise_for_status()
            _ha_cache["t"] = 0
            return "Wykonano."
        except Exception as e:  # noqa: BLE001
            return f"Błąd Home Assistant: {e}"
    if name == "arduino_sketch":
        code = str(args.get("code") or "")
        if "setup" not in code:
            return "Kod musi zawierać setup() i loop()."
        sid = await run_in_threadpool(arduino_save, code)
        card = {"type": "arduino", "id": sid, "code": code, "summary": args.get("summary", ""),
                "fqbn": ctx.board, "ok": None, "log": ""}
        if not ctx.board:
            card["log"] = "Wybierz płytkę i wgraj – kompilacja nastąpi przy wgrywaniu."
            ctx.arduino_last = card
            return "Zapisano program. Płytka nie jest wybrana – użytkownik wybierze ją w apce przed wgraniem."
        ok, log = await run_in_threadpool(arduino_compile, sid, ctx.board)
        card.update(ok=ok, log=log[-1500:])
        ctx.arduino_last = card
        if ok:
            ctx.arduino_ok = True
            ctx.events.append(card)
            return "Kompilacja OK. Powiedz krótko, co robi program; użytkownik wgra go przyciskiem w apce."
        return "Błąd kompilacji, popraw kod:\n" + log[-1500:]
    return f"Nieznane narzędzie {name}."


def parse_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except json.JSONDecodeError:
        return {}


PERSONAS = {
    "standard": "",
    "wild": (
        "Tryb BEZ FILTRA: jesteś bezczelnym, wyszczekanym kumplem, a nie grzecznym asystentem. Mówisz luzem, "
        "jak ziomek z osiedla, przeklinasz, kiedy pasuje (bez przesady w każdym zdaniu), masz czarny humor, sarkazm "
        "i cięte riposty. Nie moralizujesz, nie dodajesz zbędnych ostrzeżeń ani formułek, nie owijasz w bawełnę. "
        "Na kontrowersyjne pytania odpowiadasz wprost i masz własne zdanie. Na prośbę robisz bezlitosny roast. "
        "Dalej jesteś pomocny i konkretny. Nie pomagasz realnie krzywdzić ludzi ani łamać prawa w poważny sposób – "
        "wtedy odmawiasz krótko, w swoim stylu, bez kazania."
    ),
}


def system_prompt(persona: str = "standard") -> str:
    now = datetime.now()
    mem = load_memory()
    s = (f"{SYSTEM_PROMPT}\nDziś jest {DNI[now.weekday()]}, {now:%d.%m.%Y}, godzina {now:%H:%M}. "
         "Gdy pytanie dotyczy rzeczy aktualnych albo nie masz pewności, użyj web_search (pogoda: narzędzie weather). "
         "Z wyników podawaj konkrety: liczby, daty, nazwy. Gdy źródła się różnią, powiedz to. "
         "Odpowiadaj własnymi słowami, nie czytaj adresów stron. "
         "Do minutników, budzików, otwierania aplikacji i muzyki używaj phone_action. "
         "Gdy użytkownik chce programu na Arduino lub ESP, użyj arduino_sketch, a w odpowiedzi nie czytaj kodu na głos.")
    if mem:
        s += "\nCo wiesz o użytkowniku: " + "; ".join(mem[-40:])
    if PERSONAS.get(persona):
        s += "\n" + PERSONAS[persona]
    return s


# --- Strumieniowanie modelu ---
_model_cache: dict[str, str] = {}
_tools_supported = {"ok": True}


async def pick_model(client: httpx.AsyncClient) -> str:
    if LLM_MODEL:
        return LLM_MODEL
    if "id" not in _model_cache:
        r = await client.get(f"{LLM_URL}/models")
        r.raise_for_status()
        _model_cache["id"] = r.json()["data"][0]["id"]
        print(f"[i] Używam modelu: {_model_cache['id']}")
    return _model_cache["id"]


async def stream_llm(client: httpx.AsyncClient, model: str, messages: list, tools: list | None):
    """Zwraca ("text", fragment) w trakcie i na końcu ("tools", lista_wywołań) jeśli model chce narzędzi."""
    body = {"model": model, "messages": messages, "temperature": 0.7, "stream": True}
    if tools:
        body["tools"] = tools
    async with client.stream("POST", f"{LLM_URL}/chat/completions", json=body) as r:
        if r.status_code >= 400:
            await r.aread()
            if tools and r.status_code in (400, 422, 500):
                _tools_supported["ok"] = False  # model/serwer nie umie narzędzi – dalej bez nich
                async for ev in stream_llm(client, model, messages, None):
                    yield ev
                return
            raise RuntimeError(f"LLM {r.status_code}: {r.text[:200]}")
        calls: dict[int, dict] = {}
        async for line in r.aiter_lines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            for ch in chunk.get("choices", []):
                delta = ch.get("delta") or ch.get("message") or {}
                if delta.get("content"):
                    yield ("text", delta["content"])
                for i, tc in enumerate(delta.get("tool_calls") or []):
                    idx = tc.get("index", i)
                    slot = calls.setdefault(idx, {"id": tc.get("id") or f"call_{idx}", "type": "function",
                                                  "function": {"name": "", "arguments": ""}})
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["function"]["name"] = fn["name"]
                    a = fn.get("arguments")
                    if isinstance(a, dict):
                        slot["function"]["arguments"] = json.dumps(a, ensure_ascii=False)
                    elif a:
                        slot["function"]["arguments"] += a
        if calls:
            yield ("tools", [calls[k] for k in sorted(calls)])


class Splitter:
    """Dzieli strumień tekstu na zdania, pomija bloki <think>."""

    def __init__(self):
        self.raw = ""
        self.sent = 0

    def clean(self) -> str:
        t = re.sub(r"<think>.*?</think>", "", self.raw, flags=re.S)
        if "<think>" in t:
            t = t[: t.index("<think>")]
        return t

    def feed(self, text: str) -> list[str]:
        self.raw += text
        out = []
        t = self.clean()
        while True:
            rest = t[self.sent:]
            m = re.search(r"[.!?…](?=\s)|\n", rest)
            if m and len(rest[: m.end()].strip()) > 1:
                out.append(rest[: m.end()])
                self.sent += m.end()
            elif len(rest) > 220 and (c := rest.rfind(", ", 0, 220)) > 40:
                out.append(rest[: c + 1])
                self.sent += c + 1
            else:
                break
        return [tidy(s) for s in out if tidy(s)]

    def flush(self) -> list[str]:
        rest = tidy(self.clean()[self.sent:])
        self.sent = len(self.clean())
        return [rest] if rest else []

    def text(self) -> str:
        return tidy(self.clean())


def tidy(s: str) -> str:
    s = re.sub(r"```.*?```", "", s, flags=re.S)
    s = re.sub(r"[*#`_]+", "", s)
    return re.sub(r"\s+", " ", s).strip()


def synthesize(text: str) -> bytes | None:
    if not piper_voice or not text:
        return None
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        if hasattr(piper_voice, "synthesize_wav"):  # piper-tts >= 1.3
            piper_voice.synthesize_wav(text, wf)
        else:  # starsze wersje
            piper_voice.synthesize(text, wf)
    return buf.getvalue()


async def sentence_event(text: str, speak_on: str) -> dict:
    wav = await run_in_threadpool(synthesize, text)
    if wav and speak_on == "laptop" and sd is not None:
        _play_q.put(wav)
        wav = None
    return {"type": "sentence", "text": text, "audio": base64.b64encode(wav).decode() if wav else None,
            "laptop": speak_on == "laptop"}


sessions: dict[str, list[dict]] = {}


# --- Łącze do Claude (Anthropic API) ---
ANTHROPIC_URL = "https://api.anthropic.com/v1"
CONFIG_FILE = DATA / "config.json"
CLAUDE_DEFAULT_MODEL = "claude-haiku-5-5"  # szybki do rozmowy głosowej; w ⚙ można wybrać mocniejszy


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=1), encoding="utf-8")


def claude_key() -> str:
    return os.getenv("ANTHROPIC_API_KEY") or load_config().get("anthropic_key", "")


def claude_model() -> str:
    return load_config().get("claude_model") or CLAUDE_DEFAULT_MODEL


def claude_headers(key: str | None = None) -> dict:
    h = {"Authorization": f"Bearer {key or claude_key()}", "anthropic-version": "2023-06-01",
         "content-type": "application/json"}
    ws = os.getenv("ANTHROPIC_WORKSPACE_ID") or load_config().get("workspace_id", "")
    if ws:
        h["anthropic-workspace-id"] = ws  # tylko dla kluczy bez przypisanego workspace
    return h


class ClaudeError(Exception):
    def __init__(self, status: int, text: str):
        super().__init__(f"Claude {status}: {text[:200]}")
        self.status, self.text = status, text


_claude_search = {"ok": True}  # wyszukiwarka po stronie Anthropic (może być wyłączona w organizacji)


def claude_tools(local_tools: list[dict]) -> list[dict]:
    out = []
    for t in local_tools:
        f = t["function"]
        if f["name"] == "web_search" and _claude_search["ok"]:
            continue  # zamiast DuckDuckGo – wyszukiwarka Claude
        out.append({"name": f["name"], "description": f["description"], "input_schema": f["parameters"]})
    if _claude_search["ok"]:
        out.append({"type": "web_search_20250305", "name": "web_search", "max_uses": 3,
                    "user_location": {"type": "approximate", "country": "PL", "timezone": "Europe/Warsaw"}})
    return out


async def claude_stream(client: httpx.AsyncClient, body: dict):
    """Strumień Messages API. Zwraca ("text", s), ("block", blok) i na końcu ("end", (bloki, stop_reason))."""
    async with client.stream("POST", f"{ANTHROPIC_URL}/messages", headers=claude_headers(), json=body) as r:
        if r.status_code >= 400:
            raise ClaudeError(r.status_code, (await r.aread()).decode("utf-8", "replace"))
        blocks: dict[int, dict] = {}
        partial: dict[int, str] = {}
        stop = None
        async for ln in r.aiter_lines():
            if not ln.startswith("data:"):
                continue
            try:
                ev = json.loads(ln[5:])
            except json.JSONDecodeError:
                continue
            t = ev.get("type")
            if t == "content_block_start":
                i = ev["index"]
                blocks[i] = dict(ev["content_block"])
                if blocks[i].get("type") in ("tool_use", "server_tool_use"):
                    partial[i] = ""
            elif t == "content_block_delta":
                i, d = ev["index"], ev["delta"]
                if d["type"] == "text_delta":
                    blocks[i]["text"] = blocks[i].get("text", "") + d["text"]
                    yield ("text", d["text"])
                elif d["type"] == "input_json_delta":
                    partial[i] = partial.get(i, "") + d.get("partial_json", "")
                elif d["type"] == "citations_delta":
                    blocks[i].setdefault("citations", []).append(d["citation"])
            elif t == "content_block_stop":
                i = ev["index"]
                if i in partial:
                    blocks[i]["input"] = parse_args(partial.pop(i) or "{}")
                yield ("block", blocks[i])
            elif t == "message_delta":
                stop = ev.get("delta", {}).get("stop_reason") or stop
            elif t == "error":
                raise ClaudeError(500, json.dumps(ev.get("error", {})))
        content = [b for k, b in sorted(blocks.items()) if not (b.get("type") == "text" and not b.get("text"))]
        yield ("end", (content, stop))


async def claude_turn(hist: list[dict], ctx: "Ctx", split: "Splitter", speak_on: str, client: httpx.AsyncClient,
                      persona: str = "standard"):
    msgs = [{"role": m["role"], "content": m["content"]} for m in hist[-MAX_TURNS * 2:] if m.get("content")]
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    system = system_prompt(persona).replace("Jesteś osobistym asystentem głosowym.",
                                     "Jesteś Claude, modelem firmy Anthropic, działającym jako osobisty asystent głosowy.")
    local = await tools_for(client)
    for _ in range(MAX_ROUNDS + 3):
        body = {"model": claude_model(), "max_tokens": 4096, "system": system, "messages": msgs,
                "tools": claude_tools(local), "stream": True}
        try:
            stream = claude_stream(client, body)
            content, stop = [], None
            async for kind, val in stream:
                if kind == "text":
                    for s in split.feed(val):
                        yield await sentence_event(s, speak_on)
                elif kind == "block" and val.get("type") == "server_tool_use":
                    yield {"type": "search", "query": str(val.get("input", {}).get("query", ""))}
                elif kind == "block" and val.get("type") == "web_search_tool_result" and isinstance(val.get("content"), list):
                    yield {"type": "sources", "items": [{"title": (r.get("title") or "")[:90], "url": r.get("url", "")}
                                                        for r in val["content"][:5] if r.get("url")]}
                elif kind == "end":
                    content, stop = val
        except ClaudeError as e:
            if e.status == 400 and "web_search" in e.text and _claude_search["ok"]:
                _claude_search["ok"] = False  # wyszukiwarka Anthropic wyłączona – DuckDuckGo
                continue
            raise
        msgs.append({"role": "assistant", "content": content})
        if stop == "pause_turn":
            continue
        if stop != "tool_use":
            return
        results = []
        for b in content:
            if b.get("type") != "tool_use":
                continue
            yield {"type": "status", "text": TOOL_STATUS.get(b["name"], "pracuję…")}
            res = await exec_tool(b["name"], b.get("input") or {}, ctx, client)
            results.append({"type": "tool_result", "tool_use_id": b["id"], "content": res})
            for ev in ctx.events:
                yield ev
            ctx.events.clear()
        msgs.append({"role": "user", "content": results})


async def local_turn(hist: list[dict], text: str, ctx: "Ctx", split: "Splitter", speak_on: str,
                     client: httpx.AsyncClient, persona: str = "standard"):
    messages = [{"role": "system", "content": system_prompt(persona)}] + hist[-MAX_TURNS * 2:]
    if DDGS is not None and FORCE_SEARCH.search(text):
        yield {"type": "status", "text": "przeszukuję sieć i czytam strony…"}
        res, src = await deep_search(text)
        yield {"type": "search", "query": text, "sources": src}
        messages[-1] = {"role": "user", "content": f"{text}\n\n[Wyniki wyszukiwania w internecie]\n{res}"}

    model = await pick_model(client)
    tools = await tools_for(client)
    for rnd in range(MAX_ROUNDS + 1):
        use = tools if _tools_supported["ok"] and rnd < MAX_ROUNDS else None
        calls = None
        async for kind, val in stream_llm(client, model, messages, use):
            if kind == "text":
                for s in split.feed(val):
                    yield await sentence_event(s, speak_on)
            else:
                calls = val
        if not calls:
            return
        messages.append({"role": "assistant", "content": "", "tool_calls": calls})
        for c in calls:
            name = c["function"]["name"]
            yield {"type": "status", "text": TOOL_STATUS.get(name, "pracuję…")}
            res = await exec_tool(name, parse_args(c["function"]["arguments"]), ctx, client)
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": res})
            for ev in ctx.events:
                yield ev
            ctx.events.clear()


async def run_turn(session: str, text: str, board: str = "", speak_on: str = "phone", brain: str = "local",
                   persona: str = "standard"):
    persona = persona if persona in PERSONAS else "standard"
    hist = sessions.setdefault(session, [])
    hist.append({"role": "user", "content": text})
    ctx, split = Ctx(board), Splitter()
    used = "local"

    async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10)) as client:
        if brain == "claude" and claude_key():
            spoke = False
            try:
                async for ev in claude_turn(hist, ctx, split, speak_on, client, persona):
                    spoke = spoke or ev["type"] == "sentence"
                    yield ev
                used = "claude"
            except (ClaudeError, httpx.HTTPError) as e:
                if spoke:
                    raise
                print(f"[!] {e}")
                why = "zły klucz" if isinstance(e, ClaudeError) and e.status in (401, 403) else \
                      "limit lub brak środków" if isinstance(e, ClaudeError) and e.status in (402, 429) else "brak połączenia"
                yield {"type": "brain", "brain": "local", "note": f"Claude niedostępny ({why}) — odpowiada model lokalny"}
                ctx, split = Ctx(board), Splitter()
        elif brain == "claude":
            yield {"type": "brain", "brain": "local", "note": "brak klucza Claude — odpowiada model lokalny (⚙)"}

        if used == "local":
            async for ev in local_turn(hist, text, ctx, split, speak_on, client, persona):
                yield ev

    if ctx.arduino_last and not ctx.arduino_ok:
        yield ctx.arduino_last  # nieudana kompilacja albo brak płytki – karta z logiem
    for s in split.flush():
        yield await sentence_event(s, speak_on)
    reply = split.text()
    hist.append({"role": "assistant", "content": reply})
    yield {"type": "done", "reply": reply, "brain": used}


def transcribe(path: str) -> str:
    segments, _ = whisper.transcribe(path, language="pl", beam_size=1, vad_filter=True)
    return " ".join(s.text.strip() for s in segments).strip()


async def save_upload(audio: UploadFile) -> str:
    suffix = Path(audio.filename or "rec.webm").suffix or ".webm"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await audio.read())
        return tmp.name


async def transcribe_upload(path: str) -> str:
    try:
        return await run_in_threadpool(transcribe, path)
    finally:
        os.unlink(path)


# --- API ---
app = FastAPI()
# Apka Android ma interfejs wbudowany i łączy się z innego adresu – stąd CORS.
# Serwer jest dostępny tylko w Twojej sieci Tailscale.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.post("/api/stream")
async def stream(text: str = Form(""), session: str = Form("default"), board: str = Form(""),
                 speak_on: str = Form("phone"), brain: str = Form("local"), persona: str = Form("standard"),
                 audio: UploadFile | None = File(None)):
    path = await save_upload(audio) if audio is not None else None

    async def gen():
        try:
            q = text.strip()
            if path:
                yield line({"type": "status", "text": "rozpoznaję mowę…"})
                q = await transcribe_upload(path)
                yield line({"type": "heard", "text": q})
            if not q:
                yield line(await sentence_event("Nic nie usłyszałem, spróbuj jeszcze raz.", speak_on))
                yield line({"type": "done", "reply": ""})
                return
            async for ev in run_turn(session, q, board, speak_on, brain, persona):
                yield line(ev)
        except Exception as e:  # noqa: BLE001
            print(f"[!] {e!r}")
            yield line({"type": "error", "message": str(e)[:300]})

    return StreamingResponse(gen(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def line(ev: dict) -> str:
    return json.dumps(ev, ensure_ascii=False) + "\n"


class ChatIn(BaseModel):
    text: str = ""
    session: str = "default"


@app.post("/api/chat")
async def chat(body: ChatIn):
    """Prosta wersja bez strumienia (zgodność ze starszą apką)."""
    reply, searched = "", []
    async for ev in run_turn(body.session, body.text):
        if ev["type"] == "done":
            reply = ev["reply"]
        elif ev["type"] == "search":
            searched.append(ev["query"])
    wav = await run_in_threadpool(synthesize, reply)
    return {"heard": body.text, "reply": reply, "searched": searched,
            "audio": base64.b64encode(wav).decode() if wav else None}


@app.post("/api/reset")
async def reset(body: ChatIn):
    sessions.pop(body.session, None)
    return {"ok": True}


@app.get("/api/memory")
async def memory_get():
    return {"items": load_memory()}


@app.delete("/api/memory")
async def memory_clear():
    with _mem_lock:
        save_memory([])
    return {"ok": True}


@app.get("/api/arduino/ports")
async def ard_ports():
    return {"cli": bool(ARDUINO_CLI), "ports": await run_in_threadpool(arduino_ports), "boards": BOARDS}


class UploadIn(BaseModel):
    id: str
    port: str
    fqbn: str


@app.post("/api/arduino/upload")
async def ard_upload(body: UploadIn):
    if not ARDUINO_CLI:
        return JSONResponse({"ok": False, "log": "arduino-cli nie jest zainstalowane na laptopie."}, 400)
    if not re.fullmatch(r"s[0-9a-f]{8}", body.id) or not sketch_dir(body.id).exists():
        return JSONResponse({"ok": False, "log": "Nie ma takiego programu."}, 404)
    if not re.fullmatch(r"[\w:=,.-]+", body.fqbn) or not re.fullmatch(r"[\w/.:-]+", body.port):
        return JSONResponse({"ok": False, "log": "Nieprawidłowa płytka albo port."}, 400)
    ok, log = await run_in_threadpool(arduino_upload, body.id, body.fqbn, body.port)
    return {"ok": ok, "log": log[-2500:]}


# --- Firmware do wgrania z telefonu (OTG) ---
def otg_target(fqbn: str) -> dict | None:
    """Jaki protokół i prędkość użyje telefon dla danej płytki."""
    if fqbn.startswith("arduino:avr:nano") and "atmega328old" in fqbn:
        return {"chip": "avr", "baud": 57600}
    if fqbn.split(":")[:3] in (["arduino", "avr", "uno"], ["arduino", "avr", "nano"]):
        return {"chip": "avr", "baud": 115200}
    if fqbn.startswith("esp32:esp32:esp32s3"):
        return {"chip": "esp32s3", "baud": 115200}
    if fqbn.startswith("esp32:esp32:esp32c3"):
        return {"chip": "esp32c3", "baud": 115200}
    if fqbn.startswith("esp32:esp32:"):
        return {"chip": "esp32", "baud": 115200}
    if fqbn.startswith("esp8266:esp8266:"):
        return {"chip": "esp8266", "baud": 115200}
    return None


def hex_to_bin(text: str) -> bytes:
    mem: dict[int, int] = {}
    base = 0
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln.startswith(":"):
            continue
        rec = bytes.fromhex(ln[1:])
        n, addr, typ = rec[0], (rec[1] << 8) | rec[2], rec[3]
        data = rec[4:4 + n]
        if typ == 0:
            for i, b in enumerate(data):
                mem[base + addr + i] = b
        elif typ == 2:
            base = int.from_bytes(data, "big") << 4
        elif typ == 4:
            base = int.from_bytes(data, "big") << 16
        elif typ == 1:
            break
    if not mem:
        return b""
    out = bytearray(b"\xff" * (max(mem) + 1))
    for a, b in mem.items():
        out[a] = b
    return bytes(out)


def firmware_parts(sid: str, fqbn: str, chip: str) -> list[tuple[int, bytes]]:
    b = build_dir(sid, fqbn)
    name = f"{sid}.ino"
    if chip == "avr":
        hx = b / f"{name}.hex"
        return [(0, hex_to_bin(hx.read_text()))] if hx.exists() else []
    if chip == "esp8266":
        f = b / f"{name}.bin"
        return [(0, f.read_bytes())] if f.exists() else []
    merged = b / f"{name}.merged.bin"
    if merged.exists():
        return [(0, merged.read_bytes())]
    parts = []
    boot_off = 0x1000 if chip == "esp32" else 0x0
    for fname, off in ((f"{name}.bootloader.bin", boot_off), (f"{name}.partitions.bin", 0x8000), (f"{name}.bin", 0x10000)):
        f = b / fname
        if f.exists():
            parts.append((off, f.read_bytes()))
    return parts


@app.get("/api/arduino/firmware/{sid}")
async def ard_firmware(sid: str, fqbn: str):
    if not ARDUINO_CLI:
        return JSONResponse({"ok": False, "log": "arduino-cli nie jest zainstalowane na laptopie."}, 400)
    if not re.fullmatch(r"s[0-9a-f]{8}", sid) or not sketch_dir(sid).exists():
        return JSONResponse({"ok": False, "log": "Nie ma takiego programu."}, 404)
    if not re.fullmatch(r"[\w:=,.-]+", fqbn):
        return JSONResponse({"ok": False, "log": "Nieprawidłowa płytka."}, 400)
    target = otg_target(fqbn)
    if not target:
        return JSONResponse({"ok": False, "log": "Ta płytka nie jest obsługiwana przez OTG – wgraj przez laptopa."}, 400)
    ok, log = await run_in_threadpool(arduino_compile, sid, fqbn)
    if not ok:
        return {"ok": False, "log": "Kompilacja nie przeszła:\n" + log[-2000:]}
    parts = await run_in_threadpool(firmware_parts, sid, fqbn, target["chip"])
    if not parts:
        return {"ok": False, "log": "Nie znalazłem skompilowanego pliku w katalogu budowania."}
    return {"ok": True, **target, "size": sum(len(d) for _, d in parts),
            "parts": [{"offset": off, "data": base64.b64encode(d).decode()} for off, d in parts]}


class ClaudeIn(BaseModel):
    key: str = ""
    model: str = ""


@app.get("/api/claude")
async def claude_get():
    key = claude_key()
    out = {"configured": bool(key), "model": claude_model(), "models": [], "env": bool(os.getenv("ANTHROPIC_API_KEY"))}
    if key:
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(f"{ANTHROPIC_URL}/models?limit=50", headers=claude_headers())
                if r.status_code == 200:
                    out["models"] = [{"id": m["id"], "name": m.get("display_name", m["id"])} for m in r.json().get("data", [])]
                else:
                    out["error"] = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            out["error"] = str(e)[:120]
    return out


@app.post("/api/claude")
async def claude_set(body: ClaudeIn):
    cfg = load_config()
    key = body.key.strip()
    if key:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{ANTHROPIC_URL}/models?limit=1", headers=claude_headers(key))
        if r.status_code != 200:
            msg = "Klucz odrzucony (HTTP %d)." % r.status_code
            if "workspace" in r.text:
                msg = "Klucz nie jest przypisany do workspace – utwórz nowy klucz z wybranym workspace (np. Default)."
            return JSONResponse({"ok": False, "error": msg}, 400)
        cfg["anthropic_key"] = key
    if body.model and re.fullmatch(r"[\w.-]+", body.model):
        cfg["claude_model"] = body.model
    save_config(cfg)
    return {"ok": True, "model": cfg.get("claude_model", CLAUDE_DEFAULT_MODEL)}


@app.delete("/api/claude")
async def claude_forget():
    cfg = load_config()
    cfg.pop("anthropic_key", None)
    save_config(cfg)
    return {"ok": True}


@app.get("/api/health")
async def health():
    return {"llm": LLM_URL, "whisper": WHISPER_MODEL, "piper": piper_voice is not None,
            "search": DDGS is not None, "model": _model_cache.get("id", LLM_MODEL),
            "arduino": bool(ARDUINO_CLI), "ha": bool(HA_URL and HA_TOKEN), "laptop_audio": sd is not None,
            "memory": len(load_memory()), "stream": True, "version": "1.5",
            "speaking": _playing["on"] or not _play_q.empty(), "claude": bool(claude_key())}


@app.get("/")
async def index():
    return FileResponse(BASE / "static" / "index.html")


app.mount("/", StaticFiles(directory=BASE / "static"), name="static")

def open_window() -> None:
    """Otwiera interfejs w osobnym oknie (jak aplikacja) – Edge albo Chrome w trybie --app."""
    url = f"http://localhost:{PORT}/"
    if os.name == "nt":
        for exe in ("msedge", "chrome"):
            try:
                subprocess.Popen(f'start "" {exe} --app={url} --window-size=460,900', shell=True)
                return
            except Exception:  # noqa: BLE001
                continue
    import webbrowser

    webbrowser.open(url)


if __name__ == "__main__":
    print(f"[i] arduino-cli: {ARDUINO_CLI or 'brak'} | Home Assistant: {'tak' if HA_URL else 'nie'} | "
          f"głośnik laptopa: {'tak' if sd else 'nie'} | wyszukiwarka: {'tak' if DDGS else 'nie'}")
    if os.getenv("OPEN_WINDOW", "1") == "1":
        threading.Timer(2.5, open_window).start()
    uvicorn.run(app, host="0.0.0.0", port=PORT)
