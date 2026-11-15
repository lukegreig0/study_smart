# node

The node sits between the placemat devices and the host:

```
device (placemat sims) → node → MQTT broker → host → app
```

It subscribes to its placemats' status messages (southbound), keeps the
current state of every device, and publishes a node-level picture of the room
(northbound) for the host. Device heartbeats are never forwarded; the node only
publishes what they *change*.

## Layout

```
node/
  node/
    __main__.py   entry point (python -m node): logging, signals, run
    config.py     Config dataclass loaded from env vars, with validation
    messages.py   parse() for southbound; topic/payload builders for northbound
    registry.py   pure state logic: devices, liveness, snapshot (no MQTT, no clock)
    mqtt_node.py  paho wiring: will, connect/retry, subscribe, main loop, publish
  tests/          pytest, no broker needed
```

`registry.py` and `messages.py` never import paho and never read the clock;
time is passed in as `now`. That's why the logic tests need no broker and no
sleeps.

**Threading:** paho's network thread runs the callbacks. `on_message` (and
`on_connect`/`on_disconnect`) only put an item on a `queue.Queue`. The main
thread does all parsing, registry updates and publishing, so no locks are
needed.

## Southbound contract (input)

Topic (configurable via `DEVICE_SUB`): `studyspace/library/floor2/+/+/status`.
The device id is the **second-to-last** topic level.

| type      | payload |
|-----------|---------|
| birth     | `{"type":"birth","schema":1,"id","space_id","device_type","firmware","occupied":bool,"timestamp"}` |
| heartbeat | `{"type":"heartbeat","id","timestamp"}`, plus `"occupied":bool` **only when it changed** |
| death     | `{"type":"death","id"}` |

These are rejected and logged, without crashing: invalid JSON, empty payloads
(cleared retained messages), an empty device id, a topic id that doesn't match
the payload id, unknown types, missing or mistyped required fields, and
`schema` other than 1.

On startup the broker sends stale retained messages:
- A retained **death** for a device not yet born this session is ignored.
- A retained **birth** registers the device. If it's really gone, it stops
  heartbeating and times out.
- A birth that exactly repeats the one already processed (same timestamp) is
  ignored. The broker re-sends it on every reconnect, and its `occupied` value
  is from boot time, so applying it would undo later changes.

## Northbound contract (output)

Base topic: `studyspace/v1/{SITE}/{NODE_ID}`. Every northbound publish is
**retained, QoS 1**.

### `{base}/status`: node lifecycle

Birth is published on connect, on every reconnect, and again whenever the
device list changes (a new device, or a known device re-born with different
metadata). It lists every known device, online or not:

```json
{"type":"birth","id":"node-floor2","timestamp":"2026-11-10T12:00:00Z",
 "devices":[{"id":"placemat-0007","space_id":"desk-07","device_type":"placemat","firmware":"0.1.0"}]}
```

Death is registered as the MQTT Last Will before connecting, and also published
explicitly on clean shutdown (a clean disconnect doesn't trigger the will):

```json
{"type":"death","id":"node-floor2"}
```

### `{base}/data`: device changes

Each message contains **only the devices that changed**. If only
placemat-0007 flips, that's all you get:

```json
{"id":"node-floor2","timestamp":"2026-11-10T12:00:04Z","changed":["placemat-0007"],
 "devices":{"placemat-0007":{"online":true,"occupied":true,"since":"2026-11-10T12:00:04Z"}}}
```

- Published only when something changes (occupancy flip, a device seen for
  the first time, a device going online or offline). There is no periodic
  re-publish.
- `since` is the device's timestamp of the last occupancy change (its birth
  timestamp at first).
- Offline devices are reported only as `{"online": false}`.
- After (re)connecting: birth, then **one full snapshot** of every known
  device, marked with `"changed": []`:

  ```json
  {"id":"node-floor2","timestamp":"2026-11-10T12:00:00Z","changed":[],
   "devices":{"placemat-0007":{"online":true,"occupied":false,"since":"2026-11-10T11:58:00Z"},
              "placemat-0009":{"online":false}}}
  ```

  So `"changed": []` means "this is the whole room"; otherwise it's a delta.
  A host keeps its own table of devices and merges each delta into it.
- **Retained caveat:** the retained message is just the latest delta (or the
  connect snapshot if nothing has changed since). A host that subscribes late
  does not get the whole room from it; it gets the complete picture on the
  node's next (re)connect.

## Device liveness

- `last_seen` uses the node's `time.monotonic()`, never the device timestamp.
- No message for more than `DEVICE_TIMEOUT_S` → offline, and data is published.
- A heartbeat from an offline device → online again, and data is published.
- A heartbeat or death from a device that was never born → ignored and logged.

## Configuration

All settings come from environment variables:

| variable           | default                                  | notes |
|--------------------|------------------------------------------|-------|
| `NODE_ID`          | `node-floor2`                            | lowercase letters, digits, hyphens |
| `SITE`             | `library`                                | lowercase letters, digits, hyphens |
| `BROKER_HOST`      | `localhost`                              | |
| `BROKER_PORT`      | `1883`                                   | |
| `DEVICE_SUB`       | `studyspace/library/floor2/+/+/status`   | southbound subscription |
| `DEVICE_TIMEOUT_S` | `6`                                      | 3× the sim's 2 s heartbeat |
| `KEEPALIVE_S`      | `30`                                     | MQTT keepalive |
| `LOG_LEVEL`        | `INFO`                                   | DEBUG shows every publish |

Invalid values stop the node with a clear error and exit code 2.

## Run locally

```sh
cd node
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m node
```

In another terminal, watch the northbound output, then start the simulator:

```sh
mosquitto_sub -h localhost -t 'studyspace/v1/#' -v
python device_sim/sim.py
```

If the broker isn't up yet, the node retries the first connection (1 s, 2 s,
4 s … up to 10 s). Later drops are handled by paho's automatic reconnect
(1 s to 30 s backoff). Ctrl+C or SIGTERM shuts down cleanly: it publishes
death, disconnects, then stops the network loop.

## Test

```sh
cd node
.venv/bin/python -m pytest
```

## Docker

```sh
docker build -t study-node ./node
docker run --rm --network host -e NODE_ID=node-floor2 study-node
```

`--network host` lets the container reach a broker on the host's `localhost`.
Without it, set `-e BROKER_HOST=<broker address>`. `docker stop` sends SIGTERM,
which triggers the same clean shutdown.
