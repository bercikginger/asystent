"""Asystent głosowy – serwer na laptopie.

Telefon (aplikacja webowa) -> nagranie -> Whisper (STT) -> lokalny LLM (LM Studio / Ollama) -> Piper (TTS) -> telefon.
Uruchom: python server.py
"""
import base64
import io
import json
import os
import re
import tempfile
import wave
from datetime import datetime
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from faster_whisper import WhisperModel
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

BASE = Path(__file__).parent

# --- Konfiguracja (zmienne środowiskowe) ---
# LM Studio: http://localhost:1234/v1   |   Ollama: http://localhost:11434/v1
LLM_URL = os.getenv("LLM_URL", "http://localhost:1234/v1").rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL", "")  # puste = pierwszy załadowany model
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")  # tiny / base / small / medium
PIPER_VOICE = os.getenv("PIPER_VOICE", str(BASE / "pl_PL-gosia-medium.onnx"))
PORT = int(os.getenv("PORT", "8000"))
MAX_TURNS = 20
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "Jesteś osobistym asystentem głosowym. Odpowiadasz po polsku, krótko i konkretnie, "
    "zwykle w 1-3 zdaniach. Nie używaj markdownu, list ani emoji, bo odpowiedź jest czytana na głos.",
)

print(f"[i] Ładuję Whisper '{WHISPER_MODEL}' na CPU...")
whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")

VOICE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/pl/pl_PL/gosia/medium/pl_PL-gosia-medium.onnx"


def ensure_voice() -> None:
    """Pobiera polski głos Piper przy pierwszym uruchomieniu (~60 MB)."""
    onnx = Path(PIPER_VOICE)
    if onnx.exists() or onnx.name != "pl_PL-gosia-medium.onnx":
        return
    print("[i] Pobieram polski głos Piper (gosia, ~60 MB)...")
    for url, dest in ((VOICE_URL, onnx), (VOICE_URL + ".json", Path(str(onnx) + ".json"))):
        with httpx.stream("GET", url, follow_redirects=True, timeout=300) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)


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

app = FastAPI()
# Apka Android ma interfejs wbudowany i łączy się z innego adresu – stąd CORS.
# Serwer jest dostępny tylko w Twojej sieci Tailscale.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
sessions: dict[str, list[dict]] = {}
_model_cache: dict[str, str] = {}


async def pick_model(client: httpx.AsyncClient) -> str:
    if LLM_MODEL:
        return LLM_MODEL
    if "id" not in _model_cache:
        r = await client.get(f"{LLM_URL}/models")
        r.raise_for_status()
        _model_cache["id"] = r.json()["data"][0]["id"]
        print(f"[i] Używam modelu: {_model_cache['id']}")
    return _model_cache["id"]


# --- wyszukiwanie w internecie (DuckDuckGo, bez klucza API) ---
try:
    from ddgs import DDGS  # nowa nazwa paczki
except ImportError:  # pragma: no cover
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        DDGS = None

SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Szuka aktualnych informacji w internecie (wiadomości, pogoda, ceny, wyniki, fakty po dacie treningu).",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Krótkie zapytanie do wyszukiwarki"}},
            "required": ["query"],
        },
    },
}
FORCE_SEARCH = re.compile(
    r"\b(wyszukaj|poszukaj|wygoogluj|sprawd[źz] w (internecie|sieci)|znajd[źz] w (internecie|sieci)|"
    r"co nowego|najnowsze|aktualn\w*|dzisiaj|pogod\w*|kurs\w*)\b",
    re.I,
)
_tools_supported = {"ok": DDGS is not None}
DNI = ["poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela"]


def web_search(query: str, n: int = 5) -> str:
    if DDGS is None:
        return "Wyszukiwarka niedostępna (zainstaluj paczkę ddgs)."
    try:
        with DDGS() as ddg:
            hits = list(ddg.text(query, region="pl-pl", max_results=n))
    except Exception as e:  # noqa: BLE001
        return f"Błąd wyszukiwania: {e}"
    if not hits:
        return "Brak wyników."
    return "\n".join(f"- {h.get('title','')}: {h.get('body','')} ({h.get('href','')})" for h in hits)


def system_prompt() -> str:
    now = datetime.now()
    return (
        f"{SYSTEM_PROMPT}\nDziś jest {DNI[now.weekday()]}, {now:%d.%m.%Y}, godzina {now:%H:%M}. "
        "Gdy pytanie dotyczy rzeczy aktualnych, użyj narzędzia web_search. "
        "Korzystając z wyników, odpowiadaj własnymi słowami i nie czytaj adresów stron."
    )


async def llm_call(client: httpx.AsyncClient, model: str, messages: list, use_tools: bool) -> dict:
    body = {"model": model, "messages": messages, "temperature": 0.7}
    if use_tools:
        body["tools"] = [SEARCH_TOOL]
    r = await client.post(f"{LLM_URL}/chat/completions", json=body)
    if use_tools and r.status_code in (400, 422, 500):
        # model / serwer nie obsługuje narzędzi – dalej bez nich
        _tools_supported["ok"] = False
        return await llm_call(client, model, messages, False)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]


async def ask_llm(session: str, text: str) -> tuple[str, list[str]]:
    hist = sessions.setdefault(session, [])
    hist.append({"role": "user", "content": text})
    messages = [{"role": "system", "content": system_prompt()}] + hist[-MAX_TURNS * 2 :]
    searched: list[str] = []

    # słowa-klucze wymuszają wyszukanie także dla modeli, które nie umieją narzędzi
    if DDGS is not None and FORCE_SEARCH.search(text):
        searched.append(text)
        results = await run_in_threadpool(web_search, text)
        # dołączone do pytania (część szablonów czatu nie przyjmuje drugiej wiadomości systemowej)
        messages[-1] = {"role": "user", "content": f"{text}\n\n[Wyniki wyszukiwania w internecie]\n{results}"}

    reply = ""
    async with httpx.AsyncClient(timeout=300) as client:
        model = await pick_model(client)
        for _ in range(3):
            msg = await llm_call(client, model, messages, _tools_supported["ok"])
            calls = msg.get("tool_calls") or []
            if not calls:
                reply = msg.get("content") or ""
                break
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for c in calls:
                try:
                    q = json.loads(c["function"].get("arguments") or "{}").get("query") or text
                except (json.JSONDecodeError, KeyError, AttributeError):
                    q = text
                searched.append(q)
                res = await run_in_threadpool(web_search, q)
                messages.append({"role": "tool", "tool_call_id": c.get("id", "call"), "content": res})
        else:
            msg = await llm_call(client, model, messages, False)
            reply = msg.get("content") or ""

    reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.S).strip()  # modele "myślące"
    reply = re.sub(r"[*#`_]+", "", reply)  # bez markdownu – idzie do syntezy mowy
    hist.append({"role": "assistant", "content": reply})
    return reply, searched


def transcribe(path: str) -> str:
    segments, _ = whisper.transcribe(path, language="pl", beam_size=1, vad_filter=True)
    return " ".join(s.text.strip() for s in segments).strip()


def synthesize(text: str) -> str | None:
    if not piper_voice or not text:
        return None
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        if hasattr(piper_voice, "synthesize_wav"):  # piper-tts >= 1.3
            piper_voice.synthesize_wav(text, wf)
        else:  # starsze wersje
            piper_voice.synthesize(text, wf)
    return base64.b64encode(buf.getvalue()).decode()


class ChatIn(BaseModel):
    text: str
    session: str = "default"


@app.post("/api/voice")
async def voice(audio: UploadFile = File(...), session: str = Form("default")):
    suffix = Path(audio.filename or "rec.webm").suffix or ".webm"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await audio.read())
        path = tmp.name
    try:
        heard = await run_in_threadpool(transcribe, path)
    finally:
        os.unlink(path)
    if not heard:
        return {"heard": "", "reply": "Nic nie usłyszałem, spróbuj jeszcze raz.", "audio": None}
    reply, searched = await ask_llm(session, heard)
    return {"heard": heard, "reply": reply, "searched": searched,
            "audio": await run_in_threadpool(synthesize, reply)}


@app.post("/api/chat")
async def chat(body: ChatIn):
    reply, searched = await ask_llm(body.session, body.text)
    return {"heard": body.text, "reply": reply, "searched": searched,
            "audio": await run_in_threadpool(synthesize, reply)}


@app.post("/api/reset")
async def reset(body: ChatIn):
    sessions.pop(body.session, None)
    return {"ok": True}


@app.get("/api/health")
async def health():
    return {"llm": LLM_URL, "whisper": WHISPER_MODEL, "piper": piper_voice is not None,
            "search": DDGS is not None, "model": _model_cache.get("id", LLM_MODEL)}


@app.get("/")
async def index():
    return FileResponse(BASE / "static" / "index.html")


app.mount("/", StaticFiles(directory=BASE / "static"), name="static")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
