"""
Google Meet audio bridge (thin client).

This script ONLY handles the browser + audio transport. All conversation logic
(STT -> LLM -> TTS) lives in the backend voice-agent service (app.py), reached
over a WebSocket. That keeps the brain reusable for other clients (phone calls,
etc.) — to change behaviour, edit app.py, not this file.

DATA FLOW
---------
  meeting audio  -> OUT_SINK.monitor -> ffmpeg(16k mono) -> WS uplink   -> backend
  backend TTS    -> WS downlink(48k mono PCM)            -> pacat       -> MIC_SINK
                 -> MIC_SRC (Chrome microphone) -> meeting participants hear it

Audio topology (PulseAudio):
  OUT_SINK            null sink, Chrome output (PULSE_SINK); we capture its monitor
  MIC_SINK + MIC_SRC  null sink + remap-source = a virtual microphone (PULSE_SOURCE)

USAGE
  # 1) start the backend:   uvicorn app:app --port 8000
  # 2) run the bridge:       python playwright_app.py <MEET_URL>
  #    invisible (Xvfb):     python playwright_app.py headless <MEET_URL>
  #    verify audio devices: python playwright_app.py selftest
The meeting URL is a required command-line argument (not an env var).
Env: BACKEND_WS_URL (default ws://localhost:8000/ws/voice)
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import uuid
import wave
from datetime import datetime
from pathlib import Path

from playwright.async_api import BrowserContext, Page, async_playwright
from websockets.asyncio.client import connect as ws_connect

from dotenv import load_dotenv

# override=True so the value in .env wins over any stale exported shell variable.
load_dotenv(override=True)


def _normalize_ws_url(url: str) -> str:
    """Repair a malformed URL like 'wss://https://host/p' -> 'wss://host/p'."""
    import re
    return re.sub(r"^(wss?://)https?://", r"\1", url.strip())

# ── Configuration ─────────────────────────────────────────────────────────────
# The meeting URL is NOT read from a constant or environment variable — it is
# passed in as a command-line argument (see __main__ / run_bot). This keeps the
# bot stateless: one process == one meeting supplied by the caller.
BOT_PROFILE_DIR = os.environ.get("BOT_PROFILE_DIR", "./bot-profile")
RECORDINGS_DIR = Path(os.environ.get("RECORDINGS_DIR", "recordings"))
RECORDINGS_DIR.mkdir(exist_ok=True)

BACKEND_WS_URL = _normalize_ws_url(os.environ.get("BACKEND_WS_URL", "ws://localhost:8000/ws/voice"))
print(BACKEND_WS_URL)

MEETING_MAX_SECONDS = 60 * 60
STT_RATE = 16000          # uplink rate to backend
TTS_RATE = 48000          # downlink rate from backend
RECORD_MEETING = True

# ── Solo-watchdog tuning ────────────────────────────────────────────────────────
# A background worker watches the participant count. If the bot ends up alone in
# the meeting for SOLO_ALONE_SECONDS, we leave, close the browser and let the
# process exit (which stops the Docker container when running as PID 1).
SOLO_CHECK_INTERVAL = 10   # seconds between participant-count checks
SOLO_ALONE_SECONDS = 60    # leave after being alone this long
SOLO_GRACE_SECONDS = 45    # wait this long after admission before watching

DISMISS_LABELS = ["Got it", "Dismiss", "Continue without microphone and camera"]

# --use-fake-device-for-media-stream is intentionally omitted so Chrome uses our
# virtual microphone (PULSE_SOURCE) instead of the fake beep device.
LAUNCH_ARGS = [
    "--use-fake-ui-for-media-stream",
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--autoplay-policy=no-user-gesture-required",
    # Keep media/timers alive when the window is invisible (Xvfb) or occluded,
    # so incoming meeting audio is always rendered and captured.
    "--disable-backgrounding-occluded-windows",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-features=CalculateNativeWinOcclusion",
]


class NotAdmittedError(RuntimeError):
    """Raised when the bot is never admitted to the meeting."""


# ── PulseAudio device management ────────────────────────────────────────────────
class AudioDevices:
    def __init__(self, tag: str):
        self.out_sink = f"meetbot_out_{tag}"
        self.mic_sink = f"meetbot_micsink_{tag}"
        self.mic_src = f"meetbot_mic_{tag}"
        self._modules: list[str] = []

    @staticmethod
    def _load(*args: str) -> str:
        return subprocess.check_output(["pactl", "load-module", *args]).decode().strip()

    def setup(self) -> None:
        self._modules.append(self._load(
            "module-null-sink", f"sink_name={self.out_sink}",
            f"sink_properties=device.description={self.out_sink}"))
        self._modules.append(self._load(
            "module-null-sink", f"sink_name={self.mic_sink}",
            f"sink_properties=device.description={self.mic_sink}"))
        self._modules.append(self._load(
            "module-remap-source", f"master={self.mic_sink}.monitor",
            f"source_name={self.mic_src}",
            f"source_properties=device.description={self.mic_src}"))
        print(f"[AUDIO] out_sink={self.out_sink} mic_src={self.mic_src}")

    def teardown(self) -> None:
        for mod in reversed(self._modules):
            subprocess.run(["pactl", "unload-module", mod],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._modules.clear()
        print("[AUDIO] devices removed")

    @property
    def out_monitor(self) -> str:
        return f"{self.out_sink}.monitor"

    @property
    def mic_monitor(self) -> str:
        return f"{self.mic_sink}.monitor"


# ── Force our bot's Chrome audio onto the capture sink ───────────────────────────
# PULSE_SINK only sets Chrome's *default* sink. Google Meet can pin its audio
# output to a specific device (setSinkId), so the incoming participant audio may
# play to another sink and our monitor sees silence. We actively move our bot's
# Chrome playback streams onto OUT_SINK. Scoped by the bot-profile user-data-dir
# (via the process tree) so the user's other Chrome windows are never touched.
def _our_chrome_pids() -> set[int]:
    marker = os.path.basename(os.path.normpath(BOT_PROFILE_DIR)) or "bot-profile"
    try:
        roots = [int(x) for x in subprocess.check_output(
            ["pgrep", "-f", marker]).decode().split()]
    except Exception:
        return set()
    if not roots:
        return set()
    children: dict[int, list[int]] = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            data = open(f"/proc/{d}/stat").read()
            after = data[data.rfind(")") + 2:].split()
            ppid = int(after[1])
            children.setdefault(ppid, []).append(int(d))
        except Exception:
            continue
    ours, stack = set(roots), list(roots)
    while stack:
        for c in children.get(stack.pop(), []):
            if c not in ours:
                ours.add(c)
                stack.append(c)
    return ours


def _route_chrome_sync(out_sink: str) -> int:
    pids = _our_chrome_pids()
    if not pids:
        return 0
    sinks = json.loads(subprocess.check_output(["pactl", "-f", "json", "list", "sinks"]).decode())
    target = {s["name"]: s["index"] for s in sinks}.get(out_sink)
    inputs = json.loads(subprocess.check_output(["pactl", "-f", "json", "list", "sink-inputs"]).decode())
    moved = 0
    for si in inputs:
        try:
            pid = int(si.get("properties", {}).get("application.process.id", -1))
        except (TypeError, ValueError):
            pid = -1
        if pid in pids and si.get("sink") != target:
            subprocess.run(["pactl", "move-sink-input", str(si["index"]), out_sink],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            moved += 1
    return moved


async def keep_chrome_audio_on_sink(out_sink: str, stop: asyncio.Event) -> None:
    while not stop.is_set():
        await asyncio.sleep(2)
        try:
            moved = await asyncio.to_thread(_route_chrome_sync, out_sink)
            if moved:
                print(f"[AUDIO] routed {moved} Chrome stream(s) -> {out_sink}")
        except Exception:
            pass


# ── Audio bridge to the backend voice agent ──────────────────────────────────────
async def run_bridge(devices: AudioDevices, stop: asyncio.Event) -> None:
    """Pipe meeting audio to the backend and play backend TTS into the mic."""
    ff = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "pulse", "-i", devices.out_monitor,
        "-ac", "1", "-ar", str(STT_RATE), "-f", "s16le", "pipe:1",
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    pac = await asyncio.create_subprocess_exec(
        "pacat", "--device", devices.mic_sink,
        "--format=s16le", f"--rate={TTS_RATE}", "--channels=1",
        stdin=subprocess.PIPE,
    )
    print(f"[BRIDGE] connecting to backend {BACKEND_WS_URL}")
    try:
        async with ws_connect(BACKEND_WS_URL, max_size=None) as ws:

            async def uplink() -> None:
                assert ff.stdout is not None
                import numpy as np
                chunk = STT_RATE * 2 // 10  # 100ms 16-bit mono
                acc = bytearray()
                last = asyncio.get_event_loop().time()
                while not stop.is_set():
                    data = await ff.stdout.read(chunk)
                    if not data:
                        break
                    await ws.send(data)
                    # Periodic level meter: tells you whether the meeting audio is
                    # actually being captured (helps diagnose headless/silence).
                    acc.extend(data)
                    now = asyncio.get_event_loop().time()
                    if now - last >= 5:
                        a = np.frombuffer(bytes(acc), dtype=np.int16).astype(np.float32)
                        rms = float(np.sqrt(np.mean(a ** 2))) if a.size else 0.0
                        print(f"[BRIDGE] meeting audio level rms={rms:.0f} "
                              + ("(hearing participants)" if rms > 30
                                 else "(silence — no incoming audio)"))
                        acc.clear()
                        last = now

            async def downlink() -> None:
                assert pac.stdin is not None
                while not stop.is_set():
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    except Exception:
                        break
                    if isinstance(msg, (bytes, bytearray)):
                        pac.stdin.write(msg)
                        await pac.stdin.drain()
                    else:
                        evt = json.loads(msg)
                        t = evt.get("type")
                        if t == "ready":
                            print("[BRIDGE] backend ready — conversation live")
                        elif t in ("user", "transcript"):
                            print(f"[USER ] {evt.get('text','')}")
                        elif t in ("assistant", "agent"):
                            print(f"[AGENT] {evt.get('text','')}")
                        elif t == "speaking":
                            print("[AGENT] (speaking...)" if evt.get("value") else "[AGENT] (done)")
                        elif t in ("status", "error"):
                            print(f"[BRIDGE] backend {t}: {evt.get('text') or evt.get('message')}")

            up = asyncio.create_task(uplink())
            dn = asyncio.create_task(downlink())
            await stop.wait()
            up.cancel(); dn.cancel()
            await asyncio.gather(up, dn, return_exceptions=True)
    except Exception as exc:
        print(f"[BRIDGE] connection error: {exc}")
        pass
    finally:
        try:
            ff.terminate(); await ff.wait()
        except Exception:
            pass
        try:
            if pac.stdin:
                pac.stdin.close()
            await pac.wait()
        except Exception:
            pass


# ── Browser ──────────────────────────────────────────────────────────────────
async def launch_browser(devices: AudioDevices, headless: bool = False):
    # IMPORTANT: always launch HEADED. Chrome's true headless mode routes audio
    # to a null backend, so the meeting audio never reaches PulseAudio and STT
    # gets silence. For an invisible run, use Xvfb (a virtual display) instead —
    # see run "headless" mode in __main__.
    env = dict(os.environ)
    env["PULSE_SINK"] = devices.out_sink
    env["PULSE_SOURCE"] = devices.mic_src

    pw = await async_playwright().start()
    context = await pw.chromium.launch_persistent_context(
        BOT_PROFILE_DIR,
        headless=headless,
        args=LAUNCH_ARGS,
        permissions=["microphone", "camera"],
        viewport={"width": 1280, "height": 720},
        env=env,
    )
    return pw, context


async def join_meeting(page: Page, url: str, admit_timeout_ms: int = 120_000) -> None:
    # NOTE: do NOT use wait_until="networkidle" — Google Meet keeps persistent
    # connections open, so the network never goes idle and page.goto() hangs.
    # "domcontentloaded" fires reliably; we then wait for specific elements.
    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    for label in DISMISS_LABELS:
        try:
            await page.get_by_role("button", name=label).click(timeout=2000)
        except Exception:
            pass

    # Keep the MICROPHONE ON. The agent's TTS is fed into Chrome's virtual mic
    # (PULSE_SOURCE), and Google Meet only transmits it to other participants
    # when the mic is unmuted. Muting here (Ctrl+D) is exactly why participants
    # heard nothing. We enforce mic-on / camera-off AFTER admission (below),
    # which also corrects any muted state remembered by the persistent profile.

    try:
        await page.get_by_label("Your name").fill("Meeting Notetaker", timeout=3000)
    except Exception:
        pass
    # for label in ["Join now", "Ask to join"]:
    #     try:
    #         await page.get_by_role("button", name=label).click(timeout=3000)
    #         break
    #     except Exception:
    #         continue

    try:
        await page.get_by_label("Your name").press("Enter")
    except Exception:
        pass

    try:
        await page.wait_for_selector('button[aria-label*="Leave call"]', timeout=admit_timeout_ms)
    except Exception as exc:
        print(f"[ERROR] not admitted url={url}\n[ERROR-REASON] {exc}")
        raise NotAdmittedError(f"Leave button never appeared for {url}") from exc
    print(f"[INFO] admitted to meeting url={url}")
    await ensure_av_state(page)


async def ensure_av_state(page: Page) -> None:
    """Force the bot's mic ON (so the agent is heard) and camera OFF."""
    await asyncio.sleep(2)  # let the in-call control bar render
    # Meet aria-labels describe the ACTION: "Turn on microphone" => mic is OFF.
    try:
        mic_off = await page.query_selector('button[aria-label*="Turn on microphone"]')
        if mic_off:
            await mic_off.click()
            print("[INFO] microphone was muted -> unmuted")
        else:
            print("[INFO] microphone already on")
    except Exception as exc:
        print(f"[WARN] could not ensure mic on: {exc}")
    # "Turn off camera" => camera is currently ON; click to turn it off.
    try:
        cam_on = await page.query_selector('button[aria-label*="Turn off camera"]')
        if cam_on:
            await cam_on.click()
            print("[INFO] camera turned off")
    except Exception:
        pass


async def wait_until_meeting_ends(page: Page, max_seconds: int, stop: asyncio.Event) -> None:
    elapsed = 0
    while elapsed < max_seconds and not stop.is_set():
        await asyncio.sleep(5)
        elapsed += 5
        try:
            if await page.query_selector('button[aria-label*="Leave call"]') is None:
                print("[INFO] meeting ended")
                return
        except Exception:
            print("[INFO] page closed")
            return
    print("[INFO] max duration reached")


# ── Solo watchdog: leave when the bot is the only participant left ───────────────
def _in_docker() -> bool:
    """Best-effort detection of running inside a container."""
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup") as f:
            return any(k in f.read() for k in ("docker", "kubepods", "containerd"))
    except Exception:
        return False


async def _participant_count(page: Page) -> int:
    """Return the number of people in the call, or -1 if it can't be determined.

    Meet renders one element per participant carrying a data-participant-id
    (this includes the bot itself). We count the unique ids; a value of 1 means
    the bot is alone. -1 signals "unknown" so the watchdog stays conservative.
    """
    try:
        return await page.evaluate(
            """() => {
                const ids = new Set();
                document.querySelectorAll('[data-participant-id]').forEach(el => {
                    const id = el.getAttribute('data-participant-id');
                    if (id) ids.add(id);
                });
                return ids.size ? ids.size : -1;
            }"""
        )
    except Exception:
        return -1


async def monitor_solo(page: Page, stop: asyncio.Event) -> None:
    """Background worker: end the session once the bot is left alone.

    Runs alongside the audio bridge. After an initial grace period (so we don't
    bail out before anyone joins), it polls the participant count. If the bot is
    the only one present for SOLO_ALONE_SECONDS, it sets `stop`, which tears the
    session down and lets the process exit (stopping the Docker container).
    """
    # Wait out the grace period, but stay responsive to an early stop.
    try:
        await asyncio.wait_for(stop.wait(), timeout=SOLO_GRACE_SECONDS)
        return  # stop fired during grace -> nothing to do
    except asyncio.TimeoutError:
        pass

    alone_for = 0
    while not stop.is_set():
        await asyncio.sleep(SOLO_CHECK_INTERVAL)
        try:
            if await page.query_selector('button[aria-label*="Leave call"]') is None:
                return  # meeting already ended; wait_until_meeting_ends handles it
        except Exception:
            return  # page/browser gone

        count = await _participant_count(page)
        if count < 0:
            continue  # unknown -> assume the meeting is still active

        if count <= 1:
            alone_for += SOLO_CHECK_INTERVAL
            print(f"[SOLO] bot appears alone ({alone_for}s/{SOLO_ALONE_SECONDS}s)")
            if alone_for >= SOLO_ALONE_SECONDS:
                print("[SOLO] alone too long -> leaving meeting and shutting down")
                stop.set()
                return
        else:
            if alone_for:
                print(f"[SOLO] others present again (participants={count})")
            alone_for = 0


# ── Optional meeting recorder ───────────────────────────────────────────────────
def start_recorder(out_monitor: str, mic_monitor: str, out_path: Path) -> subprocess.Popen:
    # Record the FULL conversation: mix the participants' audio (out_monitor)
    # with the agent's spoken replies (mic_monitor, i.e. the TTS we inject).
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
           "-f", "pulse", "-i", out_monitor,
           "-f", "pulse", "-i", mic_monitor,
           "-filter_complex",
           "[0:a][1:a]amix=inputs=2:duration=longest:normalize=0[a]",
           "-map", "[a]", "-ac", "2", "-ar", "48000", str(out_path)]
    print(f"[REC] recording full conversation -> {out_path}")
    return subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop_recorder(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.communicate(input=b"q", timeout=10)
    except Exception:
        try:
            proc.send_signal(signal.SIGINT); proc.wait(timeout=10)
        except Exception:
            proc.kill()


# ── Virtual display (for invisible runs WITH working audio) ──────────────────────
def _free_display_num(start: int = 99) -> int:
    n = start
    while os.path.exists(f"/tmp/.X{n}-lock"):
        n += 1
    return n


def start_virtual_display(width: int = 1280, height: int = 720) -> subprocess.Popen:
    """Start an Xvfb virtual display and point DISPLAY at it.

    Chrome's true --headless mode disables real audio output, so STT hears
    nothing. Running HEADED inside Xvfb gives an invisible window that still
    produces/consumes audio normally.
    """
    disp = f":{_free_display_num()}"
    proc = subprocess.Popen(
        ["Xvfb", disp, "-screen", "0", f"{width}x{height}x24", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(1.2)
    os.environ["DISPLAY"] = disp
    print(f"[XVFB] virtual display {disp} (pid {proc.pid})")
    return proc


# ── Main ─────────────────────────────────────────────────────────────────────
async def main(meet_url: str, use_xvfb: bool = False) -> None:
    if not meet_url:
        raise ValueError("meet_url is required (pass it as a command-line argument)")
    tag = uuid.uuid4().hex[:8]
    devices = AudioDevices(tag)
    stop = asyncio.Event()
    pw = context = None
    recorder = None
    xvfb = None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = RECORDINGS_DIR / f"recording_{timestamp}_{tag}.wav"

    try:
        if use_xvfb:
            xvfb = start_virtual_display()
        devices.setup()
        pw, context = await launch_browser(devices)
        page = context.pages[0] if context.pages else await context.new_page()
        await join_meeting(page, meet_url)

        if RECORD_MEETING:
            recorder = start_recorder(devices.out_monitor, devices.mic_monitor, out_path)

        bridge = asyncio.create_task(run_bridge(devices, stop))
        router = asyncio.create_task(keep_chrome_audio_on_sink(devices.out_sink, stop))
        # Separate worker: watch the participant count and end the session early
        # if the bot is left alone (regardless of the 1h/2h max duration).
        solo = asyncio.create_task(monitor_solo(page, stop))
        await wait_until_meeting_ends(page, MEETING_MAX_SECONDS, stop)
        stop.set()
        await asyncio.gather(bridge, router, solo, return_exceptions=True)
    except Exception as exc:
        print(f"[ERROR] {exc}")
    finally:
        stop.set()
        stop_recorder(recorder)
        if context is not None:
            await context.close()
        if pw is not None:
            await pw.stop()
        devices.teardown()
        if xvfb is not None:
            xvfb.terminate()
        if RECORD_MEETING and out_path.exists():
            report_recording(out_path)


def run_bot(meet_url: str, use_xvfb: bool = True) -> str:
    """Synchronous entrypoint for Celery: run one full bot session.

    Each call uses a unique device tag and its own Xvfb display, so multiple
    sessions can run concurrently in separate worker processes.
    """
    asyncio.run(main(meet_url=meet_url, use_xvfb=use_xvfb))
    return meet_url


def report_recording(path: Path) -> None:
    try:
        import numpy as np
        with wave.open(str(path), "rb") as w:
            n, sr = w.getnframes(), w.getframerate()
            raw = w.readframes(n)
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        rms = float(np.sqrt(np.mean(data ** 2))) if data.size else 0.0
        print(f"[REC] {path.name}: {n/sr:.1f}s rms={rms:.1f} \n{'NON-SILENT' if rms > 30 else 'SILENT'}")
    except Exception as exc:
        print(f"[REC] analyze failed: {exc}")
        pass


# ── Self-test: verify audio devices without backend/meeting ─────────────────────
async def selftest() -> None:
    import numpy as np
    devices = AudioDevices("selftest_" + str(os.getpid()))
    pw = context = None
    try:
        devices.setup()

        # 1) Inject a tone into the virtual mic, capture MIC_SRC.
        print("[SELFTEST] injecting tone into virtual mic ...")
        t = np.linspace(0, 3, TTS_RATE * 3, endpoint=False)
        tone = (0.3 * np.sin(2 * np.pi * 440 * t) * 32767).astype(np.int16).tobytes()
        rec = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "pulse", "-i", devices.mic_src, "-t", "5",
            "-ac", "1", "-ar", str(TTS_RATE), "/tmp/_mic_selftest.wav")
        await asyncio.sleep(0.4)
        pac = await asyncio.create_subprocess_exec(
            "pacat", "--device", devices.mic_sink,
            "--format=s16le", f"--rate={TTS_RATE}", "--channels=1",
            stdin=subprocess.PIPE)
        pac.stdin.write(tone); await pac.stdin.drain(); pac.stdin.close(); await pac.wait()
        await rec.wait()
        _verify("/tmp/_mic_selftest.wav", "virtual mic injection")

        # 2) Chrome tone -> OUT_SINK monitor.
        print("[SELFTEST] capturing Chrome output ...")
        html = ("data:text/html,<html><body><script>"
                "const c=new AudioContext();const o=c.createOscillator();"
                "o.frequency.value=440;const g=c.createGain();g.gain.value=0.3;"
                "o.connect(g).connect(c.destination);o.start();c.resume();"
                "</script></body></html>")
        pw, context = await launch_browser(devices)
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(html)
        await asyncio.sleep(1)
        rec2 = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "pulse", "-i", devices.out_monitor, "-t", "3",
            "-ac", "2", "-ar", "48000", "/tmp/_out_selftest.wav")
        await rec2.wait()
        _verify("/tmp/_out_selftest.wav", "output sink capture")
    finally:
        if context is not None:
            await context.close()
        if pw is not None:
            await pw.stop()
        devices.teardown()
        for f in ("/tmp/_mic_selftest.wav", "/tmp/_out_selftest.wav"):
            try:
                os.remove(f)
            except OSError:
                pass


def _verify(path: str, label: str) -> None:
    import numpy as np
    with wave.open(path, "rb") as w:
        n = w.getnframes()
        d = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32)
    rms = float(np.sqrt(np.mean(d ** 2))) if d.size else 0.0
    print(f"[SELFTEST] {label}: rms={rms:.1f} -> {'OK ✅' if rms > 30 else 'FAIL ❌'}")


def _parse_cli(argv: list[str]) -> tuple[str | None, bool]:
    """Parse CLI args -> (meet_url, use_xvfb).

    Accepted forms (order-independent for the flag):
      python playwright_app.py <MEET_URL>
      python playwright_app.py headless <MEET_URL>   # invisible (Xvfb)
      python playwright_app.py <MEET_URL> --headless
    """
    use_xvfb = False
    meet_url: str | None = None
    for a in argv:
        if a in ("headless", "--headless", "--xvfb"):
            use_xvfb = True
        elif not a.startswith("-"):
            meet_url = a  # last positional wins
    return meet_url, use_xvfb


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "selftest":
        asyncio.run(selftest())
        sys.exit(0)

    url, xvfb_mode = _parse_cli(args)
    if not url:
        print("[FATAL] no meeting URL provided.\n"
              "Usage: python playwright_app.py [headless] <MEET_URL>")
        sys.exit(2)

    try:
        asyncio.run(main(meet_url=url, use_xvfb=xvfb_mode))
    finally:
        # main() has already closed the browser and torn down audio. Exiting the
        # process here stops the Docker container (python runs as PID 1 via the
        # entrypoint's `exec`), which is exactly what we want once the bot is
        # alone or the meeting has ended.
        if _in_docker():
            print("[DOCKER] session finished -> stopping container")
    sys.exit(0)
