"""The FastAPI application: HTTP endpoints over db.py, plus the web page.

    GET /                              the web page (static/index.html)
    GET /api/health                    is the API up, and can it read the database?
    GET /api/devices                   every device (desk sensor) and its current state
    GET /api/devices/{id}              one device
    GET /api/devices/{id}/history      its state changes, newest first
    GET /api/events                    latest state changes across all devices
    GET /api/nodes                     every node and whether it's online
    GET /api/summary                   how many desks are free / occupied / unknown

Interactive docs for all of these: http://localhost:8000/docs

Resources are keyed by device_id (placemat-0007), not space_id (desk-07),
because space_id isn't unique yet: the simulator puts every placemat on
desk-07. Each device still carries its space_id for display.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import db
from .config import Config

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

State = Literal["free", "occupied", "unknown"]


# --------------------------------------------------------------------------
# Response models. These only describe the JSON: FastAPI uses them to check
# our output and to generate the /docs page.
# --------------------------------------------------------------------------

class Device(BaseModel):
    device_id: str
    space_id: str
    node_id: str
    node_online: bool
    device_type: str | None
    firmware: str | None
    state: State
    since: str | None  # when it entered this state; null for a device never seen in data


class HistoryEntry(BaseModel):
    state: State
    changed_at: str


class Event(BaseModel):
    device_id: str
    space_id: str
    state: State
    changed_at: str


class Node(BaseModel):
    node_id: str
    site: str
    online: bool
    last_seen: str | None
    device_count: int


class Summary(BaseModel):
    free: int
    occupied: int
    unknown: int
    total: int


class Health(BaseModel):
    status: Literal["ok"]


# --------------------------------------------------------------------------

def normalize_since(value: str) -> str:
    """Turn a user-supplied ISO 8601 time into the database's format
    ('YYYY-MM-DDTHH:MM:SSZ', UTC), so a plain string comparison in SQL is a
    time comparison. Times without a timezone are rejected, not guessed."""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"since={value!r} is not an ISO 8601 time") from None
    if dt.tzinfo is None:
        raise HTTPException(422, "since must include a timezone, e.g. 2026-11-16T12:00:00Z")
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def create_app(config: Config) -> FastAPI:
    database = db.Database(config.db_path)
    app = FastAPI(
        title="Study space API",
        summary="Read-only view of desk occupancy, from the host's database.",
    )

    @app.exception_handler(db.DatabaseUnavailable)
    def database_unavailable(request: Request, exc: db.DatabaseUnavailable):
        # Normal while the stack is starting: the host hasn't created the
        # database yet. 503 tells clients "try again shortly".
        log.warning("%s %s: %s", request.method, request.url.path, exc)
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    # Endpoints are plain `def`, not `async def`: sqlite3 calls block, and
    # FastAPI runs `def` endpoints on worker threads so they don't hold up
    # other requests. See db.py for why each request opens its own connection.

    @app.get("/api/health", response_model=Health, tags=["status"])
    def health():
        with database.connect() as conn:
            db.check(conn)
        return {"status": "ok"}

    @app.get("/api/devices", response_model=list[Device], tags=["devices"])
    def list_devices():
        with database.connect() as conn:
            return db.list_devices(conn)

    @app.get("/api/devices/{device_id}", response_model=Device, tags=["devices"])
    def get_device(device_id: str):
        with database.connect() as conn:
            device = db.get_device(conn, device_id)
        if device is None:
            raise HTTPException(404, f"no device {device_id!r}")
        return device

    @app.get("/api/devices/{device_id}/history", response_model=list[HistoryEntry],
             tags=["devices"])
    def device_history(
        device_id: str,
        since: str | None = Query(None, description="only changes at or after this ISO 8601 time"),
        limit: int = Query(100, ge=1, le=1000),
    ):
        since_norm = None if since is None else normalize_since(since)
        with database.connect() as conn:
            if db.get_device(conn, device_id) is None:
                raise HTTPException(404, f"no device {device_id!r}")
            return db.device_history(conn, device_id, since_norm, limit)

    @app.get("/api/events", response_model=list[Event], tags=["devices"])
    def recent_events(limit: int = Query(20, ge=1, le=1000)):
        with database.connect() as conn:
            return db.recent_events(conn, limit)

    @app.get("/api/nodes", response_model=list[Node], tags=["nodes"])
    def list_nodes():
        with database.connect() as conn:
            return db.list_nodes(conn)

    @app.get("/api/summary", response_model=Summary, tags=["status"])
    def summary():
        with database.connect() as conn:
            counts = db.state_counts(conn)
        return {**counts, "total": sum(counts.values())}

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    return app
