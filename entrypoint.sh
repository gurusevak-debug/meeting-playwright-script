#!/usr/bin/env bash
# Boot a hardware-free PulseAudio daemon, then run the bot.
# The bot creates its own null sinks / virtual mic via pactl and starts Xvfb
# itself (the "headless" run mode = headed Chrome inside a virtual display).
set -e

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/pulse-run}"
mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"

# Start PulseAudio (no real hardware required; null sinks are made by the app).
pulseaudio -D \
    --exit-idle-time=-1 \
    --disable-shm=true \
    --log-target=stderr || true

# Wait until the daemon answers.
for _ in $(seq 1 20); do
    if pactl info >/dev/null 2>&1; then
        echo "[entrypoint] PulseAudio ready"
        break
    fi
    sleep 0.5
done

export PULSE_SERVER="unix:${XDG_RUNTIME_DIR}/pulse/native"

# If a command was provided (e.g. the Celery worker or the API), run it.
# Otherwise default to a single bot session ('headless' = headed Chrome + Xvfb).
if [ "$#" -gt 0 ]; then
    exec "$@"
else
    exec python playwright_app.py "${RUN_MODE:-headless}"
fi
