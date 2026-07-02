FROM mcr.microsoft.com/playwright/python:v1.61.0-jammy

RUN apt-get update && apt-get install -y --no-install-recommends \
    pulseaudio pulseaudio-utils ffmpeg xvfb libasound2-plugins \
    pkg-config build-essential python3-dev \
    libavformat-dev libavcodec-dev libavdevice-dev \
    libavutil-dev libswscale-dev libavfilter-dev \
    libopus-dev libvpx-dev libsrtp2-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN printf 'pcm.!default { type pulse }\nctl.!default { type pulse }\n' > /etc/asound.conf

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app
COPY requirements.txt .

RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/bot-profile /app/recordings && \
    chown -R pwuser:pwuser /app

RUN cat > /entrypoint.sh <<'EOF'
#!/bin/bash
set -e
pulseaudio -D --exit-idle-time=-1 --disallow-exit
sleep 1
exec "$@"
EOF
RUN chmod +x /entrypoint.sh

USER pwuser
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "playwright_app.py", "headless"]

# # Run command
# # 1) confirm the existing user's name/UID inside the base image
# sudo docker run --rm mcr.microsoft.com/playwright/python:v1.48.0-jammy cat /etc/passwd | tail -5

# # 2) build + run
# sudo docker build -t meetbot . && \
# sudo docker run --rm -it --shm-size=1gb \
#   --add-host=host.docker.internal:host-gateway \
#   -e BACKEND_WS_URL="ws://host.docker.internal:8000/ws/voice" \
#   -v "$(pwd)/bot-profile:/app/bot-profile" \
#   meetbot


# # Just To enter inside the docker container
# docker run --rm -it --shm-size=1gb \
#   --add-host=host.docker.internal:host-gateway \
#   -e BACKEND_WS_URL="ws://host.docker.internal:8000/ws/voice" \
#   -v "$(pwd):/app" \
#   --entrypoint /bin/bash \
#   meetbot