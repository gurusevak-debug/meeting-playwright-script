"""
Orchestrator API: POST a meeting URL to launch the bot as a background job.

  POST /join         {"url": "https://meet.google.com/abc-defg-hij"}  -> {task_id}
  GET  /tasks/{id}   -> task state/result
  GET  /health       -> liveness

Run:
    uvicorn api:app --host 0.0.0.0 --port 8001
(plus a Celery worker: `celery -A tasks worker --loglevel=info`)
"""

from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel, Field

from tasks import celery_app, join_meeting_task

app = FastAPI(title="Meet Bot Orchestrator", version="1.0.0")


class JoinRequest(BaseModel):
    url: str = Field(..., description="Google Meet URL to join")
    use_xvfb: bool = Field(True, description="Run Chrome inside a virtual display")


@app.post("/join")
def join(req: JoinRequest):
    """Enqueue a bot session for the given meeting URL."""
    task = join_meeting_task.delay(req.url, req.use_xvfb)
    return {"task_id": task.id, "status": "dispatched", "url": req.url}


@app.get("/tasks/{task_id}")
def task_status(task_id: str):
    res = celery_app.AsyncResult(task_id)
    return {
        "task_id": task_id,
        "state": res.state,                       # PENDING/STARTED/SUCCESS/FAILURE
        "info": res.info if isinstance(res.info, dict) else str(res.info),
    }


@app.get("/health")
def health():
    return {"ok": True}
