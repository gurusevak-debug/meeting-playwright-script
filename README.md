# Sevak — Google Meet Voice-Agent Bot

Sevak is a real-time voice assistant that joins Google Meet calls, listens to
participants via STT (Deepgram), generates responses via an LLM (OpenAI), and
speaks them back into the meeting using TTS (Deepgram). It also records the
full conversation (participants + bot audio mixed) as a WAV file.

The system is split into two layers so the "brain" is reusable across different
transports (Meet, phone calls, browser widget, etc.):

```
┌──────────────────────────────────────────────────────────────────┐
│  Meeting Bot (playwright_app.py)                                 │
│  - Joins Meet via Chromium (Playwright, no login required)       │
│  - Captures meeting audio via PulseAudio null sinks              │
│  - Streams audio to/from the backend over a WebSocket            │
│  - Leaves automatically when alone or when the meeting ends      │
└────────────────────────────┬─────────────────────────────────────┘
                             │ WebSocket (PCM audio + JSON events)
┌────────────────────────────▼─────────────────────────────────────┐
│  Voice-Agent Backend (main.py / app.py)                          │
│  - Deepgram STT (streaming, Nova-3)                              │
│  - OpenAI GPT-4o-mini (streaming chat completions)               │
│  - Deepgram TTS (streaming, Aura-2)                              │
│  - Sentence-level chunking for low-latency spoken replies        │
│  - User interruption support (cancels current generation)        │
└──────────────────────────────────────────────────────────────────┘
```

---

## How It Works

### Audio topology (inside the bot container)

The bot runs **headed** Chromium inside **Xvfb** (a virtual display) so that
Chrome's audio engine stays active. PulseAudio runs in software mode with no
real hardware:

```
Google Meet
  ↓ (participants' audio)
Chrome output → OUT_SINK (null sink) → .monitor → ffmpeg (16kHz mono PCM)
                                                     ↓
                                              WebSocket uplink → Backend STT
Backend TTS
  ↓ (48kHz mono PCM via WebSocket)
pacat → MIC_SINK (null sink) → .monitor → MIC_SRC (remap-source = virtual mic)
                                                     ↓
                                              Chrome microphone input
                                                     ↓
                                              Meeting participants hear it
```

A background task (`keep_chrome_audio_on_sink`) continuously moves Chrome's
playback streams onto `OUT_SINK` so that even if Meet tries to pin audio to
another device, the capture always works.

### Solo watchdog

A separate async worker (`monitor_solo`) runs alongside the audio bridge. After
a grace period (45s), it polls the participant count every 10s via the Meet DOM.
If the bot is the only one in the call for 60s, it:
1. Sets the `stop` event → tears down the audio bridge + recorder.
2. Closes the browser.
3. Exits the process → stops the Docker container (since Python is PID 1).

### Meeting URL as a CLI argument

The bot does **not** read the meeting URL from a hardcoded variable or
environment. It must be passed as a command-line argument:

```bash
python playwright_app.py <MEET_URL>
python playwright_app.py headless <MEET_URL>   # invisible (Xvfb)
python playwright_app.py selftest              # verify audio devices
```

This makes each run stateless — one process, one meeting.

### No login required

The bot joins as a guest ("Meeting Notetaker") using Chrome's persistent profile
directory. The profile is created fresh inside the container (no host volume
needed) and the bot enters via the "Your name" field + Enter — no Google
account login.

---

## Project Structure

| File / Dir            | Purpose                                                       |
|-----------------------|---------------------------------------------------------------|
| `playwright_app.py`  | Meeting bot: browser + audio bridge + solo watchdog           |
| `main.py`            | Full backend: voice WebSocket + web UI + Celery job dispatch  |
| `app.py`             | Minimal voice-agent backend (WebSocket only, no UI)           |
| `api.py`             | REST API to launch bot sessions via Celery                    |
| `tasks.py`           | Celery worker that runs `playwright_app.run_bot()`            |
| `static/index.html`  | Browser-based voice assistant UI (talk from the browser)      |
| `static/app.js`      | Frontend JS: mic capture, WebSocket, audio playback           |
| `dockerfile`         | Bot image: Playwright + PulseAudio + Xvfb + ffmpeg           |
| `requirements.txt`   | Python dependencies                                           |
| `DEPLOY.md`          | Deployment details (Docker, Celery, profile setup)            |

---

## Quick Start

### Prerequisites

- Python 3.10+
- [Deepgram](https://deepgram.com/) API key
- [OpenAI](https://platform.openai.com/) API key
- PulseAudio, ffmpeg, Xvfb (for the bot; all included in Docker)
- Redis (for Celery-based multi-meeting orchestration)

### 1. Environment

```bash
cp .env.example .env
# Add your keys:
#   DEEPGRAM_KEY=...
#   OPENAI_API_KEY=...
#   BACKEND_WS_URL=ws://localhost:8000/ws/voice   (or your public URL)
```

### 2. Run the voice-agent backend

```bash
pip install -r requirements.txt
python main.py
# Listening on http://0.0.0.0:8000
# Web UI at http://localhost:8000 (browser voice assistant)
# Voice WebSocket at ws://localhost:8000/ws/voice
```

### 3. Run the meeting bot (local, with display)

```bash
# Install Playwright browsers once:
playwright install chromium

# Join a meeting:
python playwright_app.py "https://meet.google.com/abc-defg-hij"
```

### 4. Run the meeting bot (Docker, headless)

```bash
docker build -t meetbot .

docker run --rm --shm-size=1gb \
  --add-host=host.docker.internal:host-gateway \
  -e BACKEND_WS_URL="ws://host.docker.internal:8000/ws/voice" \
  meetbot python playwright_app.py headless "https://meet.google.com/abc-defg-hij"
```

No volume mount is needed for the profile — the container creates its own
`/app/bot-profile` and the bot joins as a guest.

### 5. Multi-meeting orchestration (Celery)

```bash
# Start Redis:
redis-server &

# Start the Celery worker (inside the bot environment/container):
celery -A tasks worker --loglevel=info --concurrency=2 &

# Start the API:
uvicorn main:app --host 0.0.0.0 --port 8000 &

# Dispatch a bot into a meeting:
curl -X POST http://localhost:8000/join \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://meet.google.com/abc-defg-hij"}'
# → {"task_id": "...", "status": "dispatched", "url": "..."}

# Check status:
curl http://localhost:8000/tasks/<task_id>
```

---

## Wire Protocol (`/ws/voice`)

The WebSocket carries both binary audio and JSON control messages:

**Client → Server:**
- Binary frames: linear16 PCM, 16 kHz, mono (microphone audio)

**Server → Client:**
- `{"type": "ready"}` — connection established
- `{"type": "user", "text": "..."}` — finalized user transcript
- `{"type": "assistant", "text": "..."}` — assistant reply text (per sentence)
- `{"type": "speaking", "value": true|false}` — TTS playback state
- `{"type": "stop_audio"}` — interrupt: discard queued playback
- Binary frames: linear16 PCM, 48 kHz, mono (TTS audio)

---

## Configuration (Environment Variables)

| Variable              | Default                              | Description                        |
|-----------------------|--------------------------------------|------------------------------------|
| `DEEPGRAM_KEY`        | —                                    | Deepgram API key (STT + TTS)       |
| `OPENAI_API_KEY`      | —                                    | OpenAI API key (LLM)               |
| `OPENAI_MODEL`        | `gpt-4o-mini`                        | OpenAI model for chat completions  |
| `BACKEND_WS_URL`      | `ws://localhost:8000/ws/voice`       | Voice backend WebSocket URL        |
| `BOT_PROFILE_DIR`     | `./bot-profile`                      | Chrome profile directory           |
| `RECORDINGS_DIR`      | `recordings`                         | Where WAV recordings are saved     |
| `CELERY_BROKER_URL`   | `redis://localhost:6379/0`           | Celery broker (Redis)              |
| `CELERY_RESULT_BACKEND`| `redis://localhost:6379/1`           | Celery result backend              |

---

## Recordings

When `RECORD_MEETING=True` (default), the bot records a stereo WAV mixing both
the participants' audio and the agent's spoken replies. Files land in
`./recordings/` as `recording_<timestamp>_<tag>.wav`. After the meeting ends,
the bot prints an RMS level check to confirm the recording contains audio.

---

## Troubleshooting

- **Bot hears silence (RMS=0):** Chrome's audio is not reaching `OUT_SINK`.
  Run `python playwright_app.py selftest` to verify the audio topology.
- **Participants can't hear the bot:** The microphone is probably muted. The bot
  forces mic-on after admission, but if the profile remembers a muted state the
  toggle may fail. Delete `bot-profile/` and retry.
- **"Not admitted" error:** The meeting requires the host to let the bot in.
  Increase the admit timeout or make sure someone accepts the join request.
- **Container exits immediately:** No meeting URL was passed. The bot prints
  usage and exits with code 2.
