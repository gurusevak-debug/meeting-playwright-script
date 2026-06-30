# Deploying the Google Meet bot

The bot joins a Google Meet, streams the meeting audio to your hosted
voice-agent backend (`BACKEND_WS_URL`), and speaks the assistant's replies back
into the call. It needs no real sound card or display — PulseAudio runs in
software (null sinks) and Chrome runs headed inside Xvfb.

## Run as a service: POST a URL to launch a bot (Celery + API)

Instead of running one bot per `python playwright_app.py`, you can launch bots
on demand by POSTing a meeting URL. Components:

- **Redis** — Celery broker/result backend
- **Celery worker** (`tasks.py`) — runs the actual bot sessions (needs
  PulseAudio + Xvfb; uses the bot image)
- **API** (`api.py`) — `POST /join` enqueues a session

### Local
```bash
redis-server &                                   # broker
celery -A tasks worker --loglevel=info --concurrency=2 &
uvicorn api:app --port 8001 &

# launch a bot into a meeting:
curl -X POST http://localhost:8001/join \
     -H 'Content-Type: application/json' \
     -d '{"url":"https://meet.google.com/abc-defg-hij"}'
# -> {"task_id":"...","status":"dispatched","url":"..."}

curl http://localhost:8001/tasks/<task_id>       # PENDING/STARTED/SUCCESS/FAILURE
```

### Docker (full stack)
```bash
docker compose build
docker compose up            # starts redis + worker + api (api on :8001)

curl -X POST http://localhost:8001/join \
     -H 'Content-Type: application/json' \
     -d '{"url":"https://meet.google.com/abc-defg-hij"}'
```

Each `POST /join` runs the **exact same bot flow** as `playwright_app.py`
(join → bridge audio to `BACKEND_WS_URL` → record), as a background job. Raise
`--concurrency` (and host resources) to run multiple meetings at once; each
session gets a unique audio-device tag and its own Xvfb display.

## Files

| File                  | Purpose                                                        |
|-----------------------|----------------------------------------------------------------|
| `Dockerfile`          | Bot image: Playwright/Chromium + PulseAudio + Xvfb + ffmpeg    |
| `entrypoint.sh`       | Boots PulseAudio (null mode), then runs the bot under Xvfb     |
| `requirements-bot.txt`| Lean runtime deps for the bot                                  |
| `docker-compose.yml`  | Build/run with env + volumes                                   |
| `.dockerignore`       | Keeps the image small; never bakes `.env` or the profile       |

## 1. Configure environment (`.env`)

```
BACKEND_WS_URL=wss://unscrew-oblong-joyride.ngrok-free.dev/ws/voice
MEET_URL=https://meet.google.com/whg-brpt-wvz
```

`DEEPGRAM_KEY` / `OPENAI_API_KEY` are used by the **backend**, not the bot, so
they are not required in the bot container.

## 2. One-time: create an authenticated Chrome profile (`bot-profile/`)

Google requires a logged-in account to join meetings, and a bot account will
hit a login/2FA wall the first time. Do this once on a machine **with a
display**, then ship the resulting profile:

```bash
# headed, real display — log into the bot's Google account when Chrome opens
python playwright_app.py
```

This populates `./bot-profile/`. Mount that directory into the container
(compose already does). Keep it private — it contains a Google session.

## 3. Build & run

```bash
docker compose build
docker compose up            # joins MEET_URL, talks via BACKEND_WS_URL
```

Or with plain Docker:

```bash
docker build -t sevak-meetbot .
docker run --rm \
  --env-file .env \
  -e MEET_URL="https://meet.google.com/your-code" \
  -v "$PWD/bot-profile:/app/bot-profile" \
  -v "$PWD/recordings:/app/recordings" \
  --shm-size=1g \
  sevak-meetbot
```

Recordings (participants + assistant, mixed) land in `./recordings/`.

## How audio works in the container (no hardware)

- `entrypoint.sh` starts `pulseaudio` with no hardware sink.
- The app creates per-run **null sinks** + a **remap-source** (virtual mic) via
  `pactl`, and points Chrome at them with `PULSE_SINK` / `PULSE_SOURCE`.
- The app starts **Xvfb** and runs Chrome **headed** inside it — true Chrome
  `--headless` disables audio output, which is why we use Xvfb instead.

## Notes / gotchas

- One bot per container is cleanest (device names are per-PID, so multiple in
  one container also won't collide, but isolation is simpler).
- If joining fails with a login page, your `bot-profile` session expired —
  re-run step 2 to refresh it.
- `BACKEND_WS_URL` must be reachable from inside the container. A public
  (ngrok/HTTPS) URL works anywhere; for a co-located backend use compose
  networking (e.g. `ws://backend:8000/ws/voice`).
