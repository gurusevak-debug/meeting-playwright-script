"""
End-to-end test of the conversational voice-agent backend (app.py).

Flow:
  1. start uvicorn app:app on a test port
  2. synthesize a spoken QUESTION (Deepgram TTS @16k) — the simulated user
  3. connect to /ws/voice, stream the question PCM
  4. assert we get: a transcript, an LLM agent reply, and non-silent TTS audio

Run:  python test_backend.py
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from websockets.asyncio.client import connect as ws_connect

PORT = 8077
WS_URL = f"ws://localhost:{PORT}/ws/voice"
QUESTION = "What is the capital of France? Please answer in one word."


def load_key() -> str:
    for line in Path(".env").read_text().splitlines():
        if line.startswith("DEEPGRAM_KEY"):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("DEEPGRAM_KEY missing")


async def synth_question_16k(text: str, key: str) -> bytes:
    """Deepgram TTS at 16 kHz mono — used as the simulated user's mic audio."""
    url = "wss://api.deepgram.com/v1/speak?model=aura-2-thalia-en&encoding=linear16&sample_rate=16000"
    pcm = bytearray()
    async with ws_connect(url, additional_headers={"Authorization": f"Token {key}"}) as ws:
        await ws.send(json.dumps({"type": "Speak", "text": text}))
        await ws.send(json.dumps({"type": "Flush"}))
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=20)
            if isinstance(msg, (bytes, bytearray)):
                pcm.extend(msg)
            elif json.loads(msg).get("type") == "Flushed":
                break
        await ws.send(json.dumps({"type": "Close"}))
    return bytes(pcm)


def wait_port(port: int, timeout: float = 25) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            s.settimeout(1)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.3)
    return False


async def converse(pcm: bytes) -> dict:
    result = {"transcript": "", "agent": "", "tts_bytes": bytearray()}
    async with ws_connect(WS_URL, max_size=None) as ws:
        # wait for ready
        ready = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        assert ready.get("type") == "ready", ready

        async def sender():
            chunk = 16000 * 2 // 10  # 100ms
            for i in range(0, len(pcm), chunk):
                await ws.send(pcm[i:i + chunk])
                await asyncio.sleep(0.05)
            # a little trailing silence to flush endpointing
            await ws.send(b"\x00\x00" * (16000 // 2))

        send_task = asyncio.create_task(sender())
        try:
            deadline = time.time() + 40
            got_tts_end = False
            while time.time() < deadline and not got_tts_end:
                msg = await asyncio.wait_for(ws.recv(), timeout=20)
                if isinstance(msg, (bytes, bytearray)):
                    result["tts_bytes"].extend(msg)
                else:
                    evt = json.loads(msg)
                    t = evt.get("type")
                    if t == "transcript":
                        result["transcript"] = evt["text"]
                        print(f"[USER ] {evt['text']}")
                    elif t == "agent":
                        result["agent"] = evt["text"]
                        print(f"[AGENT] {evt['text']}")
                    elif t == "tts_end":
                        got_tts_end = True
        finally:
            await send_task
    return result


def rms(b: bytes) -> float:
    a = np.frombuffer(bytes(b), dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(a ** 2))) if a.size else 0.0


async def main() -> None:
    key = load_key()
    print(f"[TEST] synthesizing question: {QUESTION!r}")
    pcm = await synth_question_16k(QUESTION, key)
    print(f"[TEST] question audio: {len(pcm)} bytes")

    print("[TEST] starting backend ...")
    proc = subprocess.Popen(
        ["./venv/bin/uvicorn", "app:app", "--port", str(PORT), "--log-level", "warning"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        if not wait_port(PORT):
            raise RuntimeError("backend did not start")
        time.sleep(1)
        res = await converse(pcm)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    tts_rms = rms(res["tts_bytes"])
    print(f"[TEST] tts audio bytes={len(res['tts_bytes'])} rms={tts_rms:.1f}")

    ok_transcript = bool(res["transcript"].strip())
    ok_agent = bool(res["agent"].strip())
    ok_tts = tts_rms > 30
    ok_paris = "paris" in res["agent"].lower()

    print("\n=== SUMMARY ===")
    print(f"  transcript received : {'PASS ✅' if ok_transcript else 'FAIL ❌'}")
    print(f"  agent reply received: {'PASS ✅' if ok_agent else 'FAIL ❌'}")
    print(f"  tts audio non-silent: {'PASS ✅' if ok_tts else 'FAIL ❌'}")
    print(f"  reply mentions Paris: {'PASS ✅' if ok_paris else 'WARN ⚠️ (LLM phrasing)'}")

    if not (ok_transcript and ok_agent and ok_tts):
        sys.exit(1)
    print("BACKEND CONVERSATION TEST PASSED ✅")


if __name__ == "__main__":
    asyncio.run(main())
