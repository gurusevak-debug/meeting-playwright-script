"""
End-to-end tests for the browser conversational assistant (main.py).

  test_ws_client  : synthetic WebSocket client — greeting, then send a spoken
                    question (16k PCM) and assert user transcript + assistant
                    reply + TTS audio come back.
  test_browser    : real Playwright Chromium with a fake microphone fed from a
                    WAV file; click the mic and assert a transcript appears.

Run:  python test_main.py
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np
from websockets.asyncio.client import connect as ws_connect

PORT = 8090
BASE = f"localhost:{PORT}"
QUESTION = "What is the capital of France? Please answer in one word."


def load_key() -> str:
    for line in Path(".env").read_text().splitlines():
        if line.startswith("DEEPGRAM_KEY"):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("DEEPGRAM_KEY missing")


async def synth_pcm(text: str, key: str, rate: int) -> bytes:
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
    return bytes(pcm)


def write_wav(path: str, pcm: bytes, rate: int) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(pcm)


def wait_port(port: int, timeout: float = 25) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            s.settimeout(1)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.3)
    return False


def start_backend() -> subprocess.Popen:
    return subprocess.Popen(
        ["./venv/bin/uvicorn", "main:app", "--port", str(PORT), "--log-level", "warning"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


# ── Test 1: synthetic WebSocket client ───────────────────────────────────────────
async def test_ws_client(key: str) -> bool:
    pcm16 = await synth_pcm(QUESTION, key, 16000)
    state = {"users": [], "assistants": [], "tts": 0}

    async with ws_connect(f"ws://{BASE}/ws/voice", max_size=None) as ws:
        async def receiver():
            while True:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=30)
                except (asyncio.TimeoutError, Exception):
                    break
                if isinstance(msg, (bytes, bytearray)):
                    state["tts"] += len(msg)
                    continue
                m = json.loads(msg)
                if m.get("type") == "user":
                    state["users"].append(m["text"]); print(f"[USER ] {m['text']}")
                elif m.get("type") == "assistant":
                    state["assistants"].append(m["text"]); print(f"[ASSIST] {m['text']}")

        rx = asyncio.create_task(receiver())
        await asyncio.sleep(5)  # let the greeting play out (speaking guard active)

        chunk = 16000 * 2 // 10
        for i in range(0, len(pcm16), chunk):
            await ws.send(pcm16[i:i + chunk])
            await asyncio.sleep(0.05)
        await ws.send(b"\x00\x00" * 16000)  # 1s trailing silence to trigger endpointing

        # wait until a user transcript + a reply (beyond greeting) arrive
        deadline = time.time() + 40
        while time.time() < deadline:
            if state["users"] and len(state["assistants"]) >= 2:
                break
            await asyncio.sleep(0.5)
        rx.cancel()
        try:
            await rx
        except (asyncio.CancelledError, Exception):
            pass

    ok_user = bool(state["users"])
    ok_reply = len(state["assistants"]) >= 2
    ok_tts = state["tts"] > 1000
    print(f"[test_ws_client] users={state['users']} assistants={len(state['assistants'])} tts_bytes={state['tts']}")
    for label, ok in [("user transcript", ok_user), ("assistant reply", ok_reply), ("tts audio", ok_tts)]:
        print(f"   {label}: {'PASS ✅' if ok else 'FAIL ❌'}")
    return ok_user and ok_reply and ok_tts


# ── Test 2: real browser with a fake microphone ───────────────────────────────────
async def test_browser(key: str) -> bool:
    wav = os.path.abspath("/tmp/_fake_mic.wav")
    # Append ~2s of silence so the looped fake mic has a natural pause -> the
    # backend's endpointing fires and a reply is produced.
    pcm48 = await synth_pcm(QUESTION, key, 48000) + (b"\x00\x00" * (48000 * 2))
    write_wav(wav, pcm48, 48000)

    from playwright.async_api import async_playwright

    ok = False
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                f"--use-file-for-fake-audio-capture={wav}",
                "--autoplay-policy=no-user-gesture-required",
            ],
        )
        page = await browser.new_page()
        logs = []
        page.on("console", lambda m: logs.append(m.text))
        await page.goto(f"http://{BASE}/")
        await page.click("#micBtn")

        # Wait up to 45s for a USER transcript bubble to appear.
        try:
            await page.wait_for_selector(".msg.user", timeout=45000)
            user_text = await page.inner_text(".msg.user")
            await page.wait_for_selector(".msg.assistant", timeout=10000)
            assistants = await page.locator(".msg.assistant").count()
            print(f"[test_browser] user bubble: {user_text!r}; assistant bubbles: {assistants}")
            ok = bool(user_text.strip()) and assistants >= 1
        except Exception as exc:
            print(f"[test_browser] no transcript appeared: {exc}")
            print("   console tail:", logs[-5:])
        await browser.close()
    try:
        os.remove(wav)
    except OSError:
        pass
    print(f"[test_browser] -> {'PASS ✅' if ok else 'FAIL ❌'}")
    return ok


async def main() -> None:
    key = load_key()
    backend = start_backend()
    results = {}
    try:
        if not wait_port(PORT):
            raise RuntimeError("backend did not start")
        time.sleep(1)
        results["ws_client"] = await test_ws_client(key)
        results["browser"] = await test_browser(key)
    finally:
        backend.terminate()
        try:
            backend.wait(timeout=10)
        except Exception:
            backend.kill()

    print("\n=== SUMMARY ===")
    for k, v in results.items():
        print(f"  {k}: {'PASS ✅' if v else 'FAIL ❌'}")
    if not all(results.values()):
        sys.exit(1)
    print("ALL MAIN TESTS PASSED ✅")


if __name__ == "__main__":
    asyncio.run(main())
