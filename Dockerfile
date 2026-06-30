# ─────────────────────────────────────────────────────────────────────────────
# Google Meet conversational bot (playwright_app.py)
#
# Base image already includes Chromium + all browser OS libraries, matched to
# the installed Playwright version (1.61.0). On top we add:
#   - pulseaudio + utils : virtual audio sinks/sources (no hardware needed)
#   - xvfb               : virtual display (headed Chrome runs invisibly here;
#                          true --headless disables audio, so we avoid it)
#   - ffmpeg             : capture + mix the conversation
#
# The bot connects to your hosted voice-agent backend over BACKEND_WS_URL.
# ─────────────────────────────────────────────────────────────────────────────
FROM mcr.microsoft.com/playwright/python:v1.61.0-jammy

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        pulseaudio pulseaudio-utils \
        xvfb \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements-bot.txt .
RUN pip install --no-cache-dir -r requirements-bot.txt

COPY playwright_app.py tasks.py api.py entrypoint.sh ./
RUN chmod +x entrypoint.sh \
    && mkdir -p /app/bot-profile /app/recordings

# Run as the non-root user provided by the Playwright image (clean PulseAudio
# per-user session; Chromium is launched with --no-sandbox anyway).
RUN chown -R pwuser:pwuser /app
USER pwuser

# PulseAudio per-user runtime location.
ENV XDG_RUNTIME_DIR=/tmp/pulse-run

ENTRYPOINT ["./entrypoint.sh"]
