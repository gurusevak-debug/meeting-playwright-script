"""
Celery tasks for the meeting bot.

A POST to the API (see api.py) enqueues `join_meeting_task`, which runs the
EXACT same bot flow as `python playwright_app.py` — joins the meeting, bridges
audio to the voice-agent backend, records the conversation — but as a managed
background job. Run multiple meetings concurrently by raising worker
concurrency; each task uses a unique audio-device tag + its own Xvfb display.

Start a worker:
    celery -A tasks worker --loglevel=info --concurrency=2
"""

from __future__ import annotations

import os

from celery import Celery

import playwright_app as bot

BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/1")

celery_app = Celery("sevak", broker=BROKER_URL, backend=RESULT_BACKEND)
celery_app.conf.update(
    task_track_started=True,       # expose a STARTED state
    task_acks_late=True,           # re-deliver if a worker dies mid-task
    worker_prefetch_multiplier=1,  # one meeting per worker slot at a time
    task_time_limit=60 * 60 + 120,  # hard cap slightly above the bot's max
)


@celery_app.task(name="ping")
def ping() -> str:
    """Health check for the Celery pipeline."""
    return "pong"


@celery_app.task(bind=True, name="join_meeting")
def join_meeting_task(self, meet_url: str, use_xvfb: bool = True) -> dict:
    """Run one full bot session for `meet_url` (blocks until the meeting ends)."""
    self.update_state(state="STARTED", meta={"meet_url": meet_url})
    bot.run_bot(meet_url, use_xvfb=use_xvfb)
    return {"meet_url": meet_url, "status": "completed"}
