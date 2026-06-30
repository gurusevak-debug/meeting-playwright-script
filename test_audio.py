"""
Integration test for the new architecture (thin client bridge + backend agent),
without a live Google Meet.

  test_mic_injection  : tone -> virtual mic sink -> capture MIC_SRC (non-silent)
  test_bridge_to_agent: backend up + run_bridge running;
                        play a spoken QUESTION into OUT_SINK (as if the meeting
                        played it) -> bridge -> backend (STT+LLM+TTS) -> bridge
                        -> MIC_SRC carries the spoken answer (non-silent)

Run:  python test_audio.py
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import time
import wave
from pathlib import Path

import numpy as np
from websockets.asyncio.client import connect as ws_connect

import playwright_app as app

PORT = 8078


def load_key() -> str:
    for line in Path(".env").read_text().splitlines():
        if line.startswith("DEEPGRAM_KEY"):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("DEEPGRAM_KEY missing")


def rms_wav(path: str) -> float:
    with wave.open(path, "rb") as w:
        d = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(d ** 2))) if d.size else 0.0


async def synth_wav(text: str, key: str, path: str, rate: int = 48000) -> None:
    url = f"wss://api.deepgram.com/v1/speak?model=aura-2-thalia-en&encoding=linear16&sample_rate={rate}"
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
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(bytes(pcm))


def wait_port(port: int, timeout: float = 25) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            s.settimeout(1)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.3)
    return False


async def test_mic_injection() -> bool:
    dev = app.AudioDevices("t_mic_" + str(os.getpid()))
    dev.setup()
    out = "/tmp/_t_mic.wav"
    try:
        t = np.linspace(0, 3, app.TTS_RATE * 3, endpoint=False)
        tone = (0.3 * np.sin(2 * np.pi * 440 * t) * 32767).astype(np.int16).tobytes()
        rec = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "pulse", "-i", dev.mic_src, "-t", "5",
            "-ac", "1", "-ar", str(app.TTS_RATE), out)
        await asyncio.sleep(0.4)
        pac = await asyncio.create_subprocess_exec(
            "pacat", "--device", dev.mic_sink,
            "--format=s16le", f"--rate={app.TTS_RATE}", "--channels=1",
            stdin=subprocess.PIPE)
        pac.stdin.write(tone); await pac.stdin.drain(); pac.stdin.close(); await pac.wait()
        await rec.wait()
        r = rms_wav(out)
        ok = r > 30
        print(f"[test_mic_injection] rms={r:.1f} -> {'PASS ✅' if ok else 'FAIL ❌'}")
        return ok
    finally:
        dev.teardown()
        try:
            os.remove(out)
        except OSError:
            pass


async def test_bridge_to_agent() -> bool:
    key = load_key()
    q = "/tmp/_question.wav"
    await synth_wav("What is the capital of France? Please answer in one word.", key, q)

    backend = subprocess.Popen(
        ["./venv/bin/uvicorn", "app:app", "--port", str(PORT), "--log-level", "warning"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.environ["BACKEND_WS_URL"] = f"ws://localhost:{PORT}/ws/voice"
    # Reload module-level constant used by run_bridge.
    app.BACKEND_WS_URL = f"ws://localhost:{PORT}/ws/voice"

    dev = app.AudioDevices("t_bridge_" + str(os.getpid()))
    dev.setup()
    stop = asyncio.Event()
    mic_cap = "/tmp/_bridge_mic.wav"
    try:
        if not wait_port(PORT):
            print("[test_bridge_to_agent] backend did not start -> FAIL ❌")
            return False
        time.sleep(1)

        # Capture the virtual mic for the whole test (to catch the bot's reply).
        rec = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "pulse", "-i", dev.mic_src, "-t", "20",
            "-ac", "1", "-ar", str(app.TTS_RATE), mic_cap)

        bridge = asyncio.create_task(app.run_bridge(dev, stop))
        await asyncio.sleep(3)  # let bridge connect to backend

        # Play the spoken question into OUT_SINK (simulating meeting audio).
        proc = await asyncio.create_subprocess_exec(
            "paplay", "--device", dev.out_sink, q,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()

        await asyncio.sleep(10)  # STT -> LLM -> TTS -> mic
        stop.set()
        await asyncio.gather(bridge, return_exceptions=True)
        await rec.wait()

        r = rms_wav(mic_cap)
        ok = r > 30
        print(f"[test_bridge_to_agent] mic rms={r:.1f} -> {'PASS ✅' if ok else 'FAIL ❌'}")
        return ok
    finally:
        stop.set()
        dev.teardown()
        backend.terminate()
        try:
            backend.wait(timeout=10)
        except Exception:
            backend.kill()
        for f in (q, mic_cap):
            try:
                os.remove(f)
            except OSError:
                pass


async def main() -> None:
    results = {}
    results["mic_injection"] = await test_mic_injection()
    results["bridge_to_agent"] = await test_bridge_to_agent()
    print("\n=== SUMMARY ===")
    for k, v in results.items():
        print(f"  {k}: {'PASS ✅' if v else 'FAIL ❌'}")
    if not all(results.values()):
        raise SystemExit(1)
    print("ALL INTEGRATION TESTS PASSED ✅")


if __name__ == "__main__":
    asyncio.run(main())
