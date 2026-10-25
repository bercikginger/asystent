"""Słowo wybudzające na laptopie: powiedz „hey Jarvis”, a asystent wysłucha i odpowie z głośnika laptopa.

Działa obok server.py (musi być uruchomiony). Uruchom: wake.bat albo python wake.py
Ustawienia: WAKE_MODEL (domyślnie hey_jarvis), WAKE_THRESHOLD (0.5), SERVER (http://localhost:8000)
"""
import io
import json
import os
import time
import wave

import httpx
import numpy as np
import sounddevice as sd

SERVER = os.getenv("SERVER", "http://localhost:8000").rstrip("/")
WAKE_MODEL = os.getenv("WAKE_MODEL", "hey_jarvis")
THRESHOLD = float(os.getenv("WAKE_THRESHOLD", "0.5"))
RATE = 16000
FRAME = 1280  # 80 ms – rozmiar ramki wymagany przez openWakeWord


def load_model():
    import openwakeword
    from openwakeword.model import Model

    try:
        openwakeword.utils.download_models([WAKE_MODEL])
    except TypeError:
        openwakeword.utils.download_models()
    return Model(wakeword_models=[WAKE_MODEL], inference_framework="onnx")


def beep(freq=880, dur=0.12, vol=0.25):
    t = np.linspace(0, dur, int(RATE * dur), False)
    tone = (np.sin(2 * np.pi * freq * t) * vol * np.hanning(t.size)).astype(np.float32)
    sd.play(tone, RATE)
    sd.wait()


def record_command(stream) -> bytes:
    """Nagrywa do 1,2 s ciszy po wypowiedzi (max 12 s)."""
    frames, spoke, silent, start = [], False, 0.0, time.time()
    while time.time() - start < 12:
        data, _ = stream.read(FRAME)
        chunk = data[:, 0].copy()
        frames.append(chunk)
        rms = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))
        if rms > 600:
            spoke, silent = True, 0.0
        elif spoke:
            silent += FRAME / RATE
            if silent > 1.2:
                break
        elif time.time() - start > 6:
            return b""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(RATE)
        wf.writeframes(np.concatenate(frames).tobytes())
    return buf.getvalue()


def ask(wav: bytes) -> None:
    files = {"audio": ("cmd.wav", wav, "audio/wav")}
    data = {"session": "laptop", "speak_on": "laptop"}
    with httpx.stream("POST", f"{SERVER}/api/stream", data=data, files=files, timeout=600) as r:
        for ln in r.iter_lines():
            if not ln.strip():
                continue
            ev = json.loads(ln)
            if ev["type"] == "heard":
                print(f"  ty: {ev['text']}")
            elif ev["type"] == "sentence":
                print(f"  asystent: {ev['text']}")
            elif ev["type"] == "error":
                print(f"  [błąd] {ev['message']}")


def wait_until_quiet() -> None:
    """Czeka, aż laptop skończy mówić, żeby nie słuchać samego siebie."""
    for _ in range(600):
        try:
            if not httpx.get(f"{SERVER}/api/health", timeout=3).json().get("speaking"):
                return
        except Exception:  # noqa: BLE001
            return
        time.sleep(0.25)


def main() -> None:
    print(f"[i] Ładuję słowo wybudzające '{WAKE_MODEL}'...")
    model = load_model()
    print(f"[i] Gotowe. Powiedz „hey Jarvis” (próg {THRESHOLD}). Ctrl+C kończy.")
    with sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME) as stream:
        while True:
            data, _ = stream.read(FRAME)
            scores = model.predict(data[:, 0])
            if max(scores.values(), default=0) < THRESHOLD:
                continue
            print("[>] Słucham...")
            beep(880)
            wav = record_command(stream)
            beep(660, 0.08)
            if wav:
                try:
                    ask(wav)
                except Exception as e:  # noqa: BLE001
                    print(f"  [błąd] {e}")
                wait_until_quiet()
            model.reset()
            # wyrzuć dźwięk nagrany w trakcie odpowiedzi
            while stream.read_available >= FRAME:
                stream.read(FRAME)


if __name__ == "__main__":
    main()
