"""FastAPI webhook receiver.

The receiver's only jobs: authenticate, deduplicate, and hand off — fast.

Decisions:
- Return 202 within milliseconds and process in a background task. GitHub gives a webhook
  ~10 s; fetching logs plus an LLM call takes longer, and a timed-out delivery is NOT retried.
- FastAPI BackgroundTasks (runs in-process after the response) is enough for an MVP. Its
  weakness: work in flight is lost if the process dies. The production answer is a durable
  queue (Redis/SQS) plus a periodic reconciliation poll for failed runs we never processed.
- Idempotency on X-GitHub-Delivery: redeliveries (manual or from a relay) must not trigger a
  second rerun or a second PR.
"""

from __future__ import annotations

import json
import logging

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

from .config import Settings
from .db import Database
from .github import verify_signature
from .pipeline import Healer

log = logging.getLogger("ci_healer")


def create_app(healer: Healer | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or (healer.settings if healer else Settings.from_env())
    healer = healer or Healer.from_settings(settings)
    db: Database = healer.db
    app = FastAPI(title="ci-healer")

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "dry_run": settings.dry_run,
                "allowed_repos": sorted(settings.allowed_repos),
                "llm": getattr(getattr(healer.analyzer, "backend", None), "label", None)}

    @app.post("/webhook")
    async def webhook(request: Request, background: BackgroundTasks) -> JSONResponse:
        body = await request.body()
        if not verify_signature(settings.webhook_secret, body,
                                request.headers.get("X-Hub-Signature-256")):
            return JSONResponse({"error": "bad signature"}, status_code=401)

        event = request.headers.get("X-GitHub-Event", "")
        delivery = request.headers.get("X-GitHub-Delivery", "")
        if event == "ping":
            return JSONResponse({"pong": True})
        if not delivery or not db.claim_delivery(delivery, event):
            return JSONResponse({"status": "duplicate"}, status_code=200)
        if event != "workflow_run":
            return JSONResponse({"status": "ignored", "event": event}, status_code=202)

        payload = json.loads(body)
        background.add_task(_safe_handle, healer, payload)
        return JSONResponse({"status": "accepted"}, status_code=202)

    @app.get("/analyses")
    def analyses(limit: int = 50) -> list[dict]:
        return db.recent_analyses(limit)

    @app.get("/actions")
    def actions(limit: int = 50) -> list[dict]:
        return db.recent_actions(limit)

    return app


def _safe_handle(healer: Healer, payload: dict) -> None:
    try:
        result = healer.handle_workflow_run(payload)
        log.info("handled run: %s", result)
    except Exception:  # a background task has nobody to report to — log loudly
        log.exception("failed to handle workflow_run")
