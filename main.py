"""
Browser-based Conversational AI Assistant (Deepgram + OpenAI).

Open the page, click the mic, and talk to the assistant in real time. All the
"talking logic" lives here on the backend:

    browser mic (PCM 16k) ─► Deepgram STT ─► OpenAI ─► Deepgram TTS ─► browser (PCM 48k)

The frontend (static/index.html + static/app.js) is intentionally tiny: it only
captures the microphone, streams raw PCM over a WebSocket, and plays back the
audio it receives. No SDKs are used — we talk to Deepgram's raw WebSocket
endpoints and OpenAI's REST API directly.

WIRE PROTOCOL  (ws://<host>/ws/voice)
  Browser -> Server : binary frames = mic audio, linear16 PCM, 16 kHz mono
  Server -> Browser :
      text  {"type":"ready"}
      text  {"type":"status","state": "...", "text": "..."}
      text  {"type":"user","text": ...}           final user transcript
      text  {"type":"assistant","text": ...}       assistant reply text
      text  {"type":"speaking","value": true|false}
      binary frames = assistant audio, linear16 PCM, 48 kHz mono

Env (.env or environment): DEEPGRAM_KEY, OPENAI_API_KEY, optional OPENAI_MODEL.
Run:  uvicorn main:app --host 0.0.0.0 --port 8000     (or: python main.py)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.request
from pathlib import Path

import uvicorn
from celery import Celery
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from websockets.asyncio.client import connect as ws_connect

import http.client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("assistant")

# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

STT_RATE = 16000          # uplink: browser mic -> Deepgram listen
TTS_RATE = 48000          # downlink: Deepgram speak -> browser
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
GREETING = "Hello I am Meet Bot"

SYSTEM_PROMPT = (
    "You are a friendly, concise voice assistant having a spoken conversation. "
    "Keep replies short and natural — usually one to three sentences. Do not use "
    "markdown, bullet points, or emojis, because your reply will be read aloud."
)


def _load_env() -> None:
    env = BASE_DIR / ".env"
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
CYGNUS_JWT_TOKEN = os.environ.get("CYGNUS_JWT_TOKEN", "")

app = FastAPI(title="Conversational AI Assistant", version="1.0.0")
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Celery producer: we only ENQUEUE the bot task here (by name). The worker
# (tasks.py) runs the actual meeting bot. This keeps the web backend free of the
# heavy browser/audio dependencies.
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/1")
celery_producer = Celery("sevak-producer", broker=CELERY_BROKER_URL, backend=CELERY_RESULT_BACKEND)
# Fail fast (a few seconds) if Redis is unreachable, instead of long retry loops.
celery_producer.conf.update(
    broker_connection_retry_on_startup=False,
    broker_transport_options={"socket_connect_timeout": 3, "socket_timeout": 3, "max_retries": 1},
    redis_socket_connect_timeout=3,
    redis_retry_on_timeout=False,
    result_backend_transport_options={
        "retry_policy": {"max_retries": 1, "interval_start": 0, "interval_step": 0.2, "interval_max": 1},
    },
)


class JoinRequest(BaseModel):
    url: str = Field(..., description="Google Meet URL to join")
    use_xvfb: bool = Field(True, description="Run Chrome inside a virtual display")


# ── OpenAI (stdlib HTTP, no SDK) ────────────────────────────────────────────────
def openai_reply(history: list[dict]):  # Returns a generator yielding strings
    body = json.dumps(
        {
            "model": OPENAI_MODEL,
            "messages": history,
            "max_tokens": 150,
            "temperature": 0.7,
            "stream": True,  # <-- Crucial: tells OpenAI to stream chunks
        }
    ).encode()

    req = urllib.request.Request(
        OPENAI_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
    )

    with urllib.request.urlopen(req, timeout=30) as r:
        for line in r:
            if not line:
                continue

            # Decode bytes and clean up whitespace
            line_str = line.decode("utf-8").strip()

            # OpenAI streams data using Server-Sent Events (SSE)
            if line_str.startswith("data: "):
                data_payload = line_str[6:]  # Strip out 'data: ' prefix

                # OpenAI signals the end of the stream with 'data: [DONE]'
                if data_payload == "[DONE]":
                    break

                try:
                    chunk = json.loads(data_payload)
                    # Safely navigate the nested dictionary for streaming
                    choices = chunk.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            yield content
                except json.JSONDecodeError:
                    continue

# ── CygnusAI (stdlib HTTP, no SDK) ────────────────────────────────────────────────
def cygnus_reply(message: str):
    conn = http.client.HTTPSConnection("api.cygnusalpha.one")

    payload = json.dumps({

        "mobile_number": "919887221100",
        "session_id": "session-guru",
        "client_session_id": "session-guru",
        "message": {
            "text": message,
            "media": [],
            "media_url": [],
            "metadata": {
            "language": "en",
            "timezone": "Asia/Calcutta",
            "device_type": "mobile",
            "browser": "Chrome",
            "location": {
                "latitude": 26.7976704,
                "longitude": 75.841536
                }
            }
        }
    })

    headers = {
        "Authorization" : CYGNUS_JWT_TOKEN,
        "Content-Type": "application/json",
        "Cookie": 'GCLB="cf41cd894e697a75"; sessionid=6jht34wfqoyct9t6hxfdvtx7v7vqlv4o'
    }

    conn.request("POST", "/api/v1/api-controller/invoke-service/guru-sevak-workflow/", payload, headers)
    res = conn.getresponse()

    while True:
        line = res.readline()

        if not line:
            break
        
        line = line.decode("utf-8").strip()

        if line:
            line = json.loads(line)
            if line.get("text_response"):
                words = line.get("text_response").split()
                for word in words:
                    yield word + " "
    
    conn.close()
# ── Deepgram TTS (WebSocket) ──────────────────────────────────────────────────
async def deepgram_tts_stream(text: str):
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


# ── Pages ────────────────────────────────────────────────────────────────────
@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/health")
async def health():
    return JSONResponse({
        "ok": True,
        "model": OPENAI_MODEL,
        "deepgram_key": bool(DEEPGRAM_KEY),
        "openai_key": bool(OPENAI_API_KEY),
    })


# ── Launch a meeting bot via Celery ─────────────────────────────────────────────
@app.post("/join")
def join(req: JoinRequest):
    """POST a meeting URL to send the bot into that Google Meet (background job)."""
    try:
        task = celery_producer.send_task("join_meeting", args=[req.url, req.use_xvfb])
    except Exception as exc:
        log.error("dispatch failed: %s", exc)
        raise HTTPException(
            status_code=503,
            detail=("Task queue unavailable — is Redis running? "
                    f"(broker={CELERY_BROKER_URL}). Error: {exc}"),
        )
    log.info("dispatched bot for %s (task %s)", req.url, task.id)
    return {"task_id": task.id, "status": "dispatched", "url": req.url}


@app.get("/tasks/{task_id}")
def task_status(task_id: str):
    try:
        res = celery_producer.AsyncResult(task_id)
        state, info = res.state, res.info
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Result backend unavailable: {exc}")
    return {
        "task_id": task_id,
        "state": state,
        "info": info if isinstance(info, dict) else str(info),
    }


# ── Conversation WebSocket ──────────────────────────────────────────────────────
@app.websocket("/ws/voice")
async def voice(client: WebSocket):
    await client.accept()
    log.info("client connected")

    history = [{"role": "system", "content": SYSTEM_PROMPT}]
    speaking = {"v": False}
    utterance: list[str] = []   # buffers finalized words until end-of-turn
    stop = asyncio.Event()

    # Add this right next to 'speaking = {"v": False}'
    current_turn_task: asyncio.Task | None = None

    listen_url = (
        "wss://api.deepgram.com/v1/listen"
        "?model=nova-3&language=en-US&smart_format=true&interim_results=true"
        "&endpointing=300&utterance_end_ms=1000"
        f"&encoding=linear16&sample_rate={STT_RATE}&channels=1"
    )

    try:
        dg = await ws_connect(listen_url, additional_headers={"Authorization": f"Token {DEEPGRAM_KEY}"})
    except Exception as exc:
        await client.send_text(json.dumps({"type": "status", "state": "error",
                                           "text": f"Deepgram connect failed: {exc}"}))
        await client.close()
        return

    async def speak(text: str) -> None:
        """Send assistant text + stream its synthesized audio to the browser."""
        speaking["v"] = True
        try:
            await client.send_text(json.dumps({"type": "assistant", "text": text}))
            await client.send_text(json.dumps({"type": "speaking", "value": True}))
            async for pcm in deepgram_tts_stream(text):
                await client.send_bytes(pcm)
        finally:
            await client.send_text(json.dumps({"type": "speaking", "value": False}))
            speaking["v"] = False

    async def handle_turn(user_text: str) -> None:
        history.append({"role": "user", "content" : user_text})

        full_reply = ""
        buffer = ""
        sentence_endings = (".", "?", "!", "\n")
        try:
            # 1. get the generator object from thread
            gen = await asyncio.to_thread(openai_reply, history)

            # gen = await asyncio.to_thread(cygnus_reply, user_text)

            # 2. we need a helper function to pull the chunks
            def get_next_chunk(g):
                try:
                    return next(g)
                except StopIteration:
                    return None

            # 3. Looping the chunks asynchronously.
            while True:
                # Give the event loop a microsecond to breathe and raise CancelledError if requested
                await asyncio.sleep(0)
                chunk = await asyncio.to_thread(get_next_chunk, gen)
                if chunk is None:
                    break
                
                full_reply += chunk
                buffer += chunk
                # 4. Speak the chunk immediately as it arrives!
                # Note: If `speak()` expects full sentences to sound natural, 
                if any(ending in buffer for ending in sentence_endings):
                    # Find the rightmost punctuation mark to split on safely
                    # (Handles cases where multiple punctuation marks or words come in at once)
                    split_index = max(buffer.rfind(ending) for ending in sentence_endings if ending in buffer)

                    # Extract the complete sentence (including the punctuation)
                    sentence = buffer[:split_index + 1].strip()
                    # Keep the remainder in the buffer for the next sentence
                    buffer = buffer[split_index + 1:]

                    if sentence:
                        await speak(sentence)
                
            # Flush any leftover text remaining in the buffer after the loop finishes
            # (e.g., if the LLM didn't end its final sentence with a period)
            if buffer.strip():
                await speak(buffer.strip())

        except asyncio.CancelledError:
            log.info("handle_turn task was explicitly cancelled.")
            raise

        except Exception as exc:
            log.error("OpenAI error: %s", exc)
            full_reply = "Sorry, I had trouble thinking of a response."
            await speak(full_reply)

        history.append({"role": "assistant", "content": full_reply})

    await client.send_text(json.dumps({"type": "ready"}))
    await speak(GREETING)  # greet the user out loud

    async def uplink() -> None:
        try:
            while not stop.is_set():
                msg = await client.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                data = msg.get("bytes")
                if data is not None:
                    await dg.send(data)
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
            etype = evt.get("type")

            # Fire a reply when the user has clearly finished talking.
            async def flush_turn() -> None:
                nonlocal current_turn_task
                if speaking["v"] or not utterance:
                    return
                full = " ".join(utterance).strip()
                utterance.clear()
                if full:
                    log.info("USER: %s", full)
                    await client.send_text(json.dumps({"type": "user", "text": full}))
                    
                    # Instead of awaiting directly, spawn it as a task and save a reference to it
                    current_turn_task = asyncio.create_task(handle_turn(full))

            if etype == "UtteranceEnd":
                await flush_turn()
                continue
            if etype != "Results":
                continue

            alt = evt["channel"]["alternatives"][0]
            text = alt.get("transcript", "").strip()

            if text:
                # The user started or is currently speaking!
                # If the assistant is currently running a task, cancel it immediately
                if current_turn_task and not current_turn_task.done():
                    current_turn_task.cancel()
                    log.info("Interrupted assistant generation because user started talking.")
                
                # Tell the browser client to stop playing whatever audio it has queued up
                if speaking["v"]:
                    await client.send_text(json.dumps({"type": "stop_audio"}))
                    # speaking["v"] = False

            if text and evt.get("is_final"):
                utterance.append(text)
            # speech_final = Deepgram detected end-of-speech via endpointing.
            if evt.get("speech_final"):
                await flush_turn()

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
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False, log_level="info")
