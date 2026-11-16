# host

The host is the system of record (a Sparkplug-style "primary host"):

```
device (placemat sims) → node → MQTT broker → host → app
```

It subscribes to every node's northbound messages, keeps the current state of
every desk in SQLite, and records each state change in a history table. It has
no web API. A later API will read the same database.

## Layout

```
host/
  host/
    __main__.py   entry point (python -m host): config, logging, signals, run
    config.py     Config dataclass loaded from env vars, with validation
    messages.py   parse(topic, payload) -> NodeBirth | NodeDeath | NodeData | None
    store.py      every SQL statement: schema, upserts, state changes, queries
    ingest.py     processing rules + node liveness (no paho, no clock reads)
    mqtt_host.py  paho wiring: connect/retry, subscribe, main loop, shutdown
    show.py       python -m host.show: table of current desks
  tests/          pytest, in-memory SQLite, no broker, no sleeps
```

`messages.py` and `ingest.py` never import paho and never read the clock.
Time is passed in as `now_utc` (stored timestamps) and `now_mono`
(`time.monotonic()`, used only for liveness timing).

**Threading:** paho's network thread runs the callbacks. `on_message` only
puts `(topic, payload)` on a `queue.Queue`. The main thread owns the SQLite
connection and does all parsing and writing. sqlite3 connections must not be
shared across threads, so nothing on the network thread touches the database.

## Input contract

Subscriptions (QoS 1, renewed in `on_connect` so they survive reconnects):
`studyspace/v1/+/+/status` and `studyspace/v1/+/+/data`.
Topic: `studyspace/v1/{site}/{node_id}/{status|data}`.

| topic  | payload |
|--------|---------|
| status | birth: `{"type":"birth","id","timestamp","devices":[{"id","space_id","device_type","firmware"}]}` |
| status | death: `{"type":"death","id"}` (also the node's Last Will) |
| data   | `{"id","timestamp","changed":[...],"devices":{id: {"online":true,"occupied":bool,"since"} \| {"online":false}}}` |

A data message is a **delta** (only the devices that changed). The exception
is the one the node sends right after it (re)connects: that one contains every
device and has `"changed": []`. All northbound messages are retained.

Rejected and logged, never crashing: invalid JSON, empty payloads (cleared
retained messages), a topic node id that differs from the payload `id`, unknown
types or topic kinds, missing or mistyped fields, non-UTC or unparsable
timestamps, and ids (site, node, device, space) not matching `^[a-z0-9-]+$`.

## Processing rules

Snapshot entry → state: `{"online":false}` → `unknown`; online + occupied →
`occupied`; online + not occupied → `free`.

- **Birth:** upsert the node (`online=1`, `last_seen`=host now) and every
  listed device's metadata. New devices start as `unknown`, `since` NULL, with
  no event. Existing devices keep their state, and devices missing from the
  list are left alone.
- **Data:** the state is diffed against the database. `changed` is only
  logged, so a missed message is corrected by the next full snapshot.
  - Data from an **unknown** node, or from a node that is **offline**, is
    ignored. After a death, a node's next message is always a birth. This
    stops the broker's retained data, replayed after a retained death, from
    bringing a dead room back.
  - Unknown devices, or devices belonging to another node, are skipped with a
    warning.
  - **Stale/out of order:** if the message `timestamp` is older than the
    stored `since`, the entry is ignored.
  - **changed_at** is the entry's `since`. It falls back to the message
    `timestamp` if `since` is absent or older than the stored `since`. Example:
    a desk occupied since 12:00 drops offline at 12:05 and comes back at 12:06
    still saying `since: 12:00`. It became `occupied` again at 12:06.
  - On a change, `devices.state/since` is updated and an `occupancy_events`
    row is added with `INSERT OR IGNORE`.
- **Death:** node `online=0`. Each of its devices that isn't already
  `unknown` becomes `unknown` (since = host now), and each gets an event.
- **Liveness (`NODE_TIMEOUT_S`, off by default):** an online node that has
  been silent for longer than the timeout is treated as dead. The node only
  publishes when something changes and sends no periodic refresh, so a quiet
  room would look dead. The broker's Last Will (within ~1.5× the node's
  keepalive) is the real death signal. Enable the timeout only once nodes
  publish periodically.
- Each message is one transaction. Processing a message twice changes nothing
  except `nodes.last_seen`. This matters because every (re)subscribe replays
  the retained messages.

All timestamps are stored as `YYYY-MM-DDTHH:MM:SSZ`. That fixed format sorts
correctly as a string, which the comparisons rely on.

## Schema

```sql
CREATE TABLE nodes (node_id TEXT PRIMARY KEY, site TEXT NOT NULL,
                    online INTEGER NOT NULL, last_seen TEXT);
CREATE TABLE devices (device_id TEXT PRIMARY KEY,
                      node_id TEXT NOT NULL REFERENCES nodes(node_id),
                      space_id TEXT NOT NULL, device_type TEXT, firmware TEXT,
                      state TEXT NOT NULL CHECK (state IN ('free','occupied','unknown')),
                      since TEXT);
CREATE TABLE occupancy_events (id INTEGER PRIMARY KEY,
                      device_id TEXT NOT NULL REFERENCES devices(device_id),
                      state TEXT NOT NULL CHECK (state IN ('free','occupied','unknown')),
                      changed_at TEXT NOT NULL,
                      UNIQUE (device_id, state, changed_at));
```

On startup: `PRAGMA journal_mode=WAL` (readers aren't blocked by the writer)
and `PRAGMA foreign_keys=ON`.

## Configuration

| variable         | default           | notes |
|------------------|-------------------|-------|
| `BROKER_HOST`    | `localhost`       | |
| `BROKER_PORT`    | `1883`            | 1..65535 |
| `CLIENT_ID`      | `host-primary`    | |
| `DB_PATH`        | `./studyspace.db` | `/data/studyspace.db` in Docker |
| `NODE_TIMEOUT_S` | `0` (off)         | seconds; see Liveness |
| `KEEPALIVE_S`    | `30`              | MQTT keepalive |
| `LOG_LEVEL`      | `INFO`            | DEBUG shows every data message |

Invalid values stop the host with a clear error and exit code 2.

## Run locally

```sh
cd host
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m host          # Ctrl+C to stop
.venv/bin/python -m host.show     # in another terminal
```

The first connection is retried with backoff (1 s, 2 s, 4 s … 10 s). Later
drops are handled by paho's reconnect (1 s to 30 s). SIGINT/SIGTERM →
`disconnect()`, `loop_stop()`, then the database is closed.

## Test

```sh
cd host
.venv/bin/python -m pytest
```

## Example queries

```sh
sqlite3 studyspace.db "SELECT space_id, device_id, state, since FROM devices ORDER BY space_id;"
sqlite3 studyspace.db "SELECT * FROM occupancy_events ORDER BY id DESC LIMIT 10;"
sqlite3 studyspace.db "SELECT node_id, online, last_seen FROM nodes;"
# Minutes each desk was occupied today (approximate: pairs each event with the next one)
sqlite3 studyspace.db "
  SELECT device_id, ROUND(SUM((julianday(next_at) - julianday(changed_at)) * 1440)) AS minutes
  FROM (SELECT device_id, state, changed_at,
               LEAD(changed_at, 1, strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                 OVER (PARTITION BY device_id ORDER BY changed_at) AS next_at
        FROM occupancy_events)
  WHERE state = 'occupied' AND changed_at >= date('now')
  GROUP BY device_id;"
```

## Docker

```sh
docker build -t study-host ./host
docker run --rm --network host -v studyspace-data:/data study-host
```
