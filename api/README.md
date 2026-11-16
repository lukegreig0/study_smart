# api

A read-only HTTP API and web page over the host's database:

```
device (placemat sims) → node → MQTT broker → host → SQLite ← api → browser
```

The host is the only writer. The API opens the same SQLite file **read-only**
and turns it into JSON. It never talks to MQTT. The web page is a static
file, served by the API at `/`, that polls the JSON endpoints every 3 seconds.

## Layout

```
api/
  api/
    __main__.py   entry point (python -m api): config, logging, run uvicorn
    config.py     Config dataclass loaded from env vars, with validation
    app.py        FastAPI app: endpoints, response models, the web page route
    db.py         every SQL query (all SELECTs), read-only connections
    static/
      index.html  the web page: plain HTML/CSS/JS, no libraries, no build step
  tests/          pytest against a temp SQLite file, no host or broker needed
```

**FastAPI** maps URLs to Python functions and turns their return values into
JSON. The response models in `app.py` describe that JSON, and FastAPI uses
them to generate the interactive docs. **uvicorn** is the web server that
runs the app.

## Endpoints

| method & path | returns |
|---|---|
| `GET /` | the web page |
| `GET /docs` | interactive API docs (try every endpoint in the browser) |
| `GET /api/health` | `{"status":"ok"}`, or 503 if the database isn't readable yet |
| `GET /api/devices` | every device and its current state, sorted by space |
| `GET /api/devices/{device_id}` | one device, or 404 |
| `GET /api/devices/{device_id}/history?since=&limit=` | its state changes, newest first |
| `GET /api/events?limit=` | latest changes across all devices (default 20) |
| `GET /api/nodes` | each node, online or not, and how many devices it has |
| `GET /api/summary` | `{"free":n,"occupied":n,"unknown":n,"total":n}` |

A device looks like this:

```json
{"device_id": "placemat-0007", "space_id": "desk-07", "node_id": "node-floor2",
 "node_online": true, "device_type": "placemat", "firmware": "0.1.0",
 "state": "occupied", "since": "2026-11-16T12:05:00Z"}
```

- `state` is `free`, `occupied` or `unknown`. `unknown` means no data (the
  device or its node is offline), **not** "free". The page shows it as
  "No data".
- `since` is when the device entered that state. It's `null` for a device the
  node has announced but not reported on yet.
- `since` on `/history` is an ISO 8601 time **with** a timezone, e.g.
  `2026-11-16T12:00:00Z`. Times without one are rejected (422) rather than
  guessed.
- Devices are addressed by `device_id`, not `space_id`, because the simulator
  currently puts every placemat on `desk-07`, so `space_id` isn't unique.

**503 Service Unavailable** means the database doesn't exist yet or has no
tables. That's normal for a few seconds while the stack starts, before the
host has created it. The page keeps retrying.

## How it reads the database

- **Read-only:** opened with `mode=ro`, so a bug here can't change data, and a
  missing file is never created by accident.
- **One connection per request:** FastAPI runs plain `def` endpoints on
  worker threads, and a sqlite3 connection must not be shared across threads.
  Opening a local SQLite file takes well under a millisecond.
- **WAL:** the host's WAL mode means our reads never block its writes. Each
  request sees a consistent snapshot, and changes show up on the next request.

## Configuration

| variable    | default                 | notes |
|-------------|-------------------------|-------|
| `DB_PATH`   | `../host/studyspace.db` | `/data/studyspace.db` in Docker (the shared volume) |
| `API_HOST`  | `127.0.0.1`             | only this machine; `0.0.0.0` = every interface |
| `API_PORT`  | `8000`                  | |
| `LOG_LEVEL` | `INFO`                  | |

Invalid values stop the API with a clear error and exit code 2.

## Run locally

Run the host first (`cd host && .venv/bin/python -m host`) so the database
exists. Then:

```sh
cd api
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m api
```

Open http://localhost:8000 for the page and http://localhost:8000/docs for the API.

```sh
curl -s localhost:8000/api/summary
curl -s localhost:8000/api/devices/placemat-0009/history?limit=5
```

## Test

```sh
cd api
.venv/bin/python -m pytest
```

The tests build a small database in a temp file, with a copy of the host's
schema. They don't import the host package, because the two are separate
services. If the schema in `host/host/store.py` changes, update
`tests/conftest.py` too.

## Docker

In `compose.yaml` the `api` service mounts the same `host-data` volume as the
host. Two details matter:

- The container runs as **uid 10001, the same as the host container**. In WAL
  mode even a reader writes to the shared `-shm` file the host created, so the
  two need the same user. For the same reason the volume isn't mounted `:ro`;
  read-only is enforced by `mode=ro` instead.
- It has a Docker **healthcheck** on `/api/health`, so `docker compose ps`
  shows `healthy` once it can read the database.
