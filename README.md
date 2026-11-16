# study_smart

Live desk occupancy for study spaces. Sensors on each seat ("placemats")
report whether the seat is in use, and a web page shows which seats are
free, occupied, or have no data.

## Quick start

You need Docker with Compose, and an MQTT broker (e.g. Mosquitto) running on
`localhost:1883`. The broker isn't part of the stack.

```sh
./restart.sh
```

Then open **http://localhost:8000** for the seat page, or
**http://localhost:8000/docs** to try the API.

`restart.sh` stops the stack, rebuilds any changed images and starts
everything again in the background. It works from any directory, and the
database is kept between restarts.

Other useful commands:

```sh
docker compose -p floor2 logs -f          # live logs from every service
docker compose -p floor2 down             # stop everything (database kept)
docker compose -p floor2 down -v          # stop and delete the database
```

## Architecture

```
placemats (sim) → node → MQTT broker → host → SQLite ← api → browser
```

| component | role |
|---|---|
| [`device_sim/`](device_sim/sim.py) | Simulates placemats: publishes a birth, heartbeats, and occupancy changes over MQTT. |
| [`node/`](node/README.md) | Gateway for one floor: tracks its placemats and publishes a summary of the room (only what changed) northbound. |
| [`host/`](host/README.md) | System of record: subscribes to every node and writes current seat state and change history to SQLite. The only writer. |
| [`api/`](api/README.md) | Reads the database (read-only) and serves a JSON API plus the seat web page. |

Devices and nodes talk only MQTT; the web side talks only HTTP. The database
is the boundary between them. Each component's README has the details:
message formats, processing rules, configuration and how to run it on its own.

## Tests

Each component has its own tests (no broker needed):

```sh
cd host && python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/python -m pytest
```

Same for `node/` and `api/`. GitHub Actions runs all of them, and checks every
Docker image builds, on each pull request and push to `main`.
