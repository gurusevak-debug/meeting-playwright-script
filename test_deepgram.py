"""
Deepgram WebSocket connectivity test (no SDK).

Round-trips through both APIs to prove they work with the configured key:
  1. TTS  : text  -> Deepgram /v1/speak  -> linear16 PCM
  2. STT  : that PCM -> Deepgram /v1/listen -> transcript text

Run:  python test_deepgram.py
"""

from __future__ import annotations

import asyncio
import json
import os
import wave
from pathlib import Path

import numpy as np
from websockets.asyncio.client import connect

TTS_RATE = 48000
STT_RATE = 48000


def load_key() -> str:
    for line in Path(".env").read_text().splitlines():
        if line.startswith("DEEPGRAM_KEY"):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("DEEPGRAM_KEY not found in .env")


async def tts(text: str, key: str) -> bytes:
    url = (
        "wss://api.deepgram.com/v1/speak"
        "?model=aura-2-thalia-en&encoding=linear16&sample_rate=" + str(TTS_RATE)
    )
    pcm = bytearray()
    async with connect(url, additional_headers={"Authorization": f"Token {key}"}) as ws:
        await ws.send(json.dumps({"type": "Speak", "text": text}))
        await ws.send(json.dumps({"type": "Flush"}))
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=15)
            if isinstance(msg, (bytes, bytearray)):
                pcm.extend(msg)
            else:
                evt = json.loads(msg)
                if evt.get("type") == "Flushed":
                    break
        await ws.send(json.dumps({"type": "Close"}))
    return bytes(pcm)


async def stt(pcm: bytes, key: str) -> str:
    url = (
        "wss://api.deepgram.com/v1/listen"
        "?model=nova-3&language=en-US&smart_format=true"
        "&encoding=linear16&sample_rate=" + str(STT_RATE) + "&channels=1"
    )
    transcripts: list[str] = []
    async with connect(url, additional_headers={"Authorization": f"Token {key}"}) as ws:
        async def sender():
            chunk = STT_RATE * 2 // 10  # 100ms of 16-bit mono
            for i in range(0, len(pcm), chunk):
                await ws.send(pcm[i:i + chunk])
                await asyncio.sleep(0.02)
            await ws.send(json.dumps({"type": "CloseStream"}))

        send_task = asyncio.create_task(sender())
        try:
            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=15)
                if isinstance(msg, (bytes, bytearray)):
                    continue
                evt = json.loads(msg)
                if evt.get("type") == "Results":
                    alt = evt["channel"]["alternatives"][0]
                    t = alt.get("transcript", "")
                    if t and evt.get("is_final"):
                        transcripts.append(t)
        except (asyncio.TimeoutError, Exception):
            pass
        finally:
            await send_task
    return " ".join(transcripts).strip()


def rms(pcm: bytes) -> float:
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(a ** 2))) if a.size else 0.0


async def main() -> None:
    key = load_key()
    text = "Hello, this is a Deepgram websocket round trip test."
    print(f"[TTS] synthesizing: {text!r}")
    pcm = await tts(text, key)
    print(f"[TTS] received {len(pcm)} PCM bytes, rms={rms(pcm):.1f}")
    assert len(pcm) > 1000, "TTS returned too little audio"
    assert rms(pcm) > 30, "TTS audio is silent"

    # Save for inspection.
    with wave.open("tts_out.wav", "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(TTS_RATE)
        w.writeframes(pcm)
    print("[TTS] wrote tts_out.wav")

    print("[STT] transcribing the synthesized audio ...")
    transcript = await stt(pcm, key)
    print(f"[STT] transcript: {transcript!r}")
    assert transcript, "STT returned empty transcript"

    print("RESULT: Deepgram STT + TTS round-trip OK ✅")


if __name__ == "__main__":
    asyncio.run(main())
