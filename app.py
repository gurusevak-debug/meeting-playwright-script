"""
Conversational Voice-Agent backend (reusable across clients).

A single WebSocket endpoint turns an incoming audio stream into a spoken
conversation:

    client audio (PCM) -> Deepgram STT -> OpenAI LLM -> Deepgram TTS -> client audio (PCM)

The transport is a plain WebSocket, so ANY client can use it — the Google Meet
bot (playwright_app.py), a phone-call media stream (e.g. Twilio), a CLI, etc.
All the "talking logic" lives here so future upgrades happen in one place.

WIRE PROTOCOL  (endpoint: /ws/voice)
------------------------------------
  Client -> Server:
    * binary frames  = uplink mic audio, linear16 PCM, 16 kHz, mono
    * text  {"type":"end"}   (optional) to signal end of input
  Server -> Client:
    * text  {"type":"ready"}                      connection established
    * text  {"type":"transcript","text": ...}     final user transcript
    * text  {"type":"agent","text": ...}          assistant reply text
    * text  {"type":"tts_start"} / {"type":"tts_end"}
    * binary frames  = downlink TTS audio, linear16 PCM, 48 kHz, mono

Env (.env or environment): DEEPGRAM_KEY, OPENAI_API_KEY, optional OPENAI_MODEL.

Run:  uvicorn app:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.request
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, HTMLResponse
from websockets.asyncio.client import connect as ws_connect

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("voice-agent")

# ── Config ────────────────────────────────────────────────────────────────────
STT_RATE = 16000          # uplink rate (client -> Deepgram listen)
TTS_RATE = 48000          # downlink rate (Deepgram speak -> client)
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

SYSTEM_PROMPT = (
    "You are a friendly, concise voice assistant participating in a live "
    "conversation. Keep answers short and natural — usually one to three "
    "sentences — since they will be spoken aloud. Do not use markdown, lists, "
    "or emojis."
)


def _load_env() -> None:
    env = Path(".env")
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_env()
DEEPGRAM_KEY = os.environ.get("DEEPGRAM_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

app = FastAPI(title="Conversational Voice Agent", version="2.0.0")


# ── OpenAI (stdlib HTTP, no SDK) ────────────────────────────────────────────────
def openai_reply(history: list[dict]) -> str:
    body = json.dumps({
        "model": OPENAI_MODEL,
        "messages": history,
        "max_tokens": 150,
        "temperature": 0.7,
    }).encode()
    req = urllib.request.Request(
        OPENAI_URL, data=body,
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    return data["choices"][0]["message"]["content"].strip()


# ── Deepgram TTS (WebSocket) ──────────────────────────────────────────────────
async def deepgram_tts_stream(text: str):
    """Yield linear16 mono PCM frames (TTS_RATE) for `text`."""
    url = (
        "wss://api.deepgram.com/v1/speak"
        f"?model=aura-2-thalia-en&encoding=linear16&sample_rate={TTS_RATE}"
    )
    async with ws_connect(url, additional_headers={"Authorization": f"Token {DEEPGRAM_KEY}"}) as ws:
        await ws.send(json.dumps({"type": "Speak", "text": text}))
        await ws.send(json.dumps({"type": "Flush"}))
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=20)
            if isinstance(msg, (bytes, bytearray)):
                yield bytes(msg)
            elif json.loads(msg).get("type") == "Flushed":
                break
        await ws.send(json.dumps({"type": "Close"}))


# ── Health / info ───────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return JSONResponse({
        "service": "conversational-voice-agent",
        "endpoint": "/ws/voice",
        "uplink": f"linear16 {STT_RATE}Hz mono",
        "downlink": f"linear16 {TTS_RATE}Hz mono",
        "model": OPENAI_MODEL,
        "deepgram_key": bool(DEEPGRAM_KEY),
        "openai_key": bool(OPENAI_API_KEY),
    })
 

# ── The voice-agent WebSocket ────────────────────────────────────────────────────
@app.websocket("/ws/voice")
async def voice(client: WebSocket):
    await client.accept()
    log.info("client connected")

    history = [{"role": "system", "content": SYSTEM_PROMPT}]
    speaking = {"v": False}  # while True we ignore inbound audio (avoid self-talk)
    stop = asyncio.Event()

    listen_url = (
        "wss://api.deepgram.com/v1/listen"
        "?model=nova-3&language=en-US&smart_format=true&interim_results=true"
        f"&encoding=linear16&sample_rate={STT_RATE}&channels=1"
    )

    try:
        dg = await ws_connect(listen_url, additional_headers={"Authorization": f"Token {DEEPGRAM_KEY}"})
    except Exception as exc:
        await client.send_text(json.dumps({"type": "error", "message": f"deepgram connect failed: {exc}"}))
        await client.close()
        return

    await client.send_text(json.dumps({"type": "ready"}))

    async def handle_turn(user_text: str) -> None:
        """Run one LLM turn and stream the spoken reply back to the client."""
        speaking["v"] = True
        try:
            history.append({"role": "user", "content": user_text})
            try:
                reply = await asyncio.to_thread(openai_reply, history)
            except Exception as exc:
                log.error("OpenAI error: %s", exc)
                reply = "Sorry, I had trouble thinking of a response."
            history.append({"role": "assistant", "content": reply})
            log.info("AGENT: %s", reply)
            await client.send_text(json.dumps({"type": "agent", "text": reply}))

            await client.send_text(json.dumps({"type": "tts_start"}))
            async for pcm in deepgram_tts_stream(reply):
                await client.send_bytes(pcm)
            await client.send_text(json.dumps({"type": "tts_end"}))
        finally:
            speaking["v"] = False

    async def uplink() -> None:
        """Forward client mic audio to Deepgram STT."""
        try:
            while not stop.is_set():
                msg = await client.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                data = msg.get("bytes")
                if data is not None:
                    if not speaking["v"]:
                        await dg.send(data)
                elif msg.get("text"):
                    evt = json.loads(msg["text"])
                    if evt.get("type") == "end":
                        break
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            stop.set()
            try:
                await dg.send(json.dumps({"type": "CloseStream"}))
            except Exception:
                pass

    async def keepalive() -> None:
        while not stop.is_set():
            await asyncio.sleep(8)
            try:
                await dg.send(json.dumps({"type": "KeepAlive"}))
            except Exception:
                break

    async def downlink() -> None:
        """Read Deepgram transcripts; drive the conversation."""
        while not stop.is_set():
            try:
                msg = await asyncio.wait_for(dg.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except Exception:
                break
            if isinstance(msg, (bytes, bytearray)):
                continue
            evt = json.loads(msg)
            if evt.get("type") != "Results":
                continue
            alt = evt["channel"]["alternatives"][0]
            text = alt.get("transcript", "").strip()
            if not text or not evt.get("is_final"):
                continue
            if speaking["v"]:
                continue
            log.info("USER: %s", text)
            await client.send_text(json.dumps({"type": "transcript", "text": text}))
            await handle_turn(text)

    try:
        await asyncio.gather(uplink(), downlink(), keepalive())
    except Exception as exc:
        log.error("session error: %s", exc)
    finally:
        stop.set()
        try:
            await dg.close()
        except Exception:
            pass
        try:
            await client.close()
        except Exception:
            pass
        log.info("client disconnected")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, log_level="info")
