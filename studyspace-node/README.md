# studyspace-node

End-node software for a study-space availability system. One Raspberry Pi with a touchscreen sits
in the middle of a desk and serves any number of seats. It works out whether each seat is free or
occupied, lets students mark seats by hand, and publishes each seat's state to an MQTT broker
(Mosquitto).

Requires Python 3.11+ (Raspberry Pi OS Bookworm ships 3.11).

## How it fits together

```
 load cells ──► node process ◄──── local socket ────► GUI process (touchscreen)
                     │                                  shows state, sends commands
                     └── MQTT ──► broker ──► backend / student app
```

The node and the GUI are **separate programs**:

- The GUI holds no occupancy logic and imports no sensor or MQTT code. It only displays what the
  node reports and asks the node to do things, and the node can refuse.
- If the GUI crashes, the node keeps reporting.
- If the node stops, the screen says "Not available" and reconnects when it returns. It never shows
  old availability as if it were current.
- The local socket means the screen still works while the network or broker is down.

## Configuring a desk

Everything about a desk's layout is in the config, so the same software runs any arrangement.
`[[seats]]` has one entry per seat, and each seat can use any combination of sensors:

| Scenario | Config |
|---|---|
| Prototype: one node, three chairs with load cells | `config.example.toml` as it is |
| Single seat | one `[[seats]]` entry |
| Larger shared table | more `[[seats]]` entries; the screen diagram redraws itself |
| Seat without a load cell | `[seats.motion]` and/or `[seats.distance]` in place of `[seats.weight]` |

| Section           | Fields |
|-------------------|--------|
| `[node]`          | `id`, `desk_id`, `building`, `room` (all required) |
| `[[seats]]`       | `space_id` (required, unique across the site), `label`, `position_deg` (clockwise from the top of the diagram; evenly spaced if omitted) |
| `[seats.weight]`  | `data_pin`, `clock_pin`, `tare_offset`, `scale_per_kg` |
| `[seats.motion]`  | `pin` |
| `[seats.distance]`| `trigger_pin`, `echo_pin` |
| `[sensors]`       | `poll_interval_s` |
| `[occupancy]`     | `presence_weight_kg`, `weight_window`, `min_valid_weight_kg`, `max_valid_weight_kg`, `presence_distance_m`, `distance_window`, `min_valid_distance_m`, `max_valid_distance_m`, `confirm_readings`, `vacancy_timeout_s`, `manual_hold_s`, `max_consecutive_faults` |
| `[ui]`            | `socket_path` |
| `[mqtt]`          | `host` (required), `port` (default 1883, or 8883 with TLS), `topic_prefix`, `username`, `password`, `tls`, `heartbeat_s`, `client_id` (defaults to node `id`, must be unique per broker) |

Unknown fields are rejected so typos don't pass silently. Keep the password out of the file by setting
the `STUDYSPACE_MQTT_PASSWORD` environment variable, which overrides `password`. With `tls = true`
the system CA store is used to verify the broker.

## Hardware

### Load cells (prototype)

Each chair has a load cell and its own HX711 amplifier. Mount the HX711 on the chair: the raw load
cell signal is too weak to run far. Pins are BCM GPIO numbers and are set per seat in the config.

| Seat | HX711 pin | Raspberry Pi | Physical pin |
|------|-----------|--------------|--------------|
| A    | DT        | GPIO5        | 29           |
| A    | SCK       | GPIO6        | 31           |
| B    | DT        | GPIO13       | 33           |
| B    | SCK       | GPIO19       | 35           |
| C    | DT        | GPIO20       | 38           |
| C    | SCK       | GPIO21       | 40           |
| All  | VCC       | **3.3 V**    | 1 or 17      |
| All  | GND       | GND          | 30, 34, 39   |

> **Warning: power the HX711 from 3.3 V, not 5 V.** Its DT output swings to its supply voltage and Pi
> GPIOs are 3.3 V only, so a board powered from 5 V can damage the Pi.

One load cell measures one point, so put a rigid plate under the seat resting on the cell(s), or
someone sitting off-centre may be misread.

Keep cables short and tidy. Long runs from a chair can pick up noise, and cables to chairs that move
get pulled and are a trip hazard. That's acceptable for a prototype with chairs kept close; wider use
would probably need a wireless link.

### Calibrating each chair

Every chair needs its own values. You'll need a known weight, such as 10 kg:

```sh
studyspace-calibrate --config config.toml --seat desk-07-a --known-kg 10
```

Follow the prompts, then copy the printed `tare_offset` and `scale_per_kg` into that seat's
`[seats.weight]`. Recalibrate if a chair is rebuilt or readings drift. Check `presence_weight_kg`
(default 20 kg) against the people and bags you actually see: at or above it counts as a person, and
below it doesn't.

### Touchscreen

Use a DSI or HDMI display with USB touch, which needs no GPIO pins. Avoid SPI "HAT" screens, which
take over many GPIOs and can clash with the pins above. The layout adapts to the screen size and is
designed around 800×480 or larger.

### PIR and ultrasonic (optional, per seat)

| Sensor  | Sensor pin | Raspberry Pi                 | Physical pin |
|---------|------------|------------------------------|--------------|
| PIR     | VCC        | 5 V                          | 2            |
| PIR     | OUT        | GPIO17                       | 11           |
| PIR     | GND        | GND                          | 6            |
| HC-SR04 | VCC        | 5 V                          | 4            |
| HC-SR04 | TRIG       | GPIO23                       | 16           |
| HC-SR04 | ECHO       | GPIO24 **through a divider** | 18           |
| HC-SR04 | GND        | GND                          | 14           |

> **Warning: the HC-SR04 ECHO output is 5 V.** Connecting it directly can damage the Pi. Use a voltage
> divider: ECHO to 1 kΩ to GPIO24, and GPIO24 to 2 kΩ to GND (5 V × 2/3 ≈ 3.3 V).

For an HC-SR501 style PIR, check that OUT is 3.3 V and set the hold-time trimmer to its minimum (about
3 s), or one person walking past can look like several "present" readings.

## Install

On the Pi (`lgpio` is the GPIO backend gpiozero needs on Bookworm, and comes from apt, which is why
the venv uses system site packages):

```sh
sudo apt install python3-venv python3-lgpio
python3 -m venv --system-site-packages .venv
.venv/bin/pip install ".[pi,gui]"
cp config.example.toml config.toml    # then edit it
.venv/bin/python -m studyspace_node --config config.toml
.venv/bin/python -m studyspace_node.gui --socket /tmp/studyspace-node-ui.sock
```

The user running the node must be in the `gpio` group.

For development on any machine, no GPIO needed:

```sh
python3 -m venv .venv
.venv/bin/pip install -e ".[dev,gui]"
```

## Run in simulate mode

`--simulate` replaces the sensors with scripted, repeating free → occupied → free scenarios, staggered
per seat. MQTT stays real, so you can develop a backend against it without hardware. With no broker
running, the node logs its state changes and buffers messages until one appears.

The default 300 s vacancy timeout makes for a slow demo, so shorten it in a copy of the config:

```toml
[sensors]
poll_interval_s = 1.0

[occupancy]
vacancy_timeout_s = 10
```

```sh
mosquitto -v &                                   # or point [mqtt] host at your broker
mosquitto_sub -t 'studyspace/#' -v &
python -m studyspace_node --config config.toml --simulate --log-level DEBUG &
python -m studyspace_node.gui --socket /tmp/studyspace-node-ui.sock --windowed
```

## How occupancy is decided

For each seat, on each poll:

```
present = motion OR median(recent weights) >= presence_weight_kg OR median(recent distances) < presence_distance_m
```

A seat only uses the sensors it has.

| From            | To       | Condition |
|-----------------|----------|-----------|
| UNKNOWN or FREE | OCCUPIED | `confirm_readings` consecutive present readings |
| UNKNOWN         | FREE     | `confirm_readings` consecutive absent readings |
| OCCUPIED        | FREE     | an absent reading at least `vacancy_timeout_s` after presence was last seen |

- If none of a seat's sensors give a usable reading, its state is held and a fault is counted. `healthy`
  goes false after `max_consecutive_faults` in a row and recovers on the next good reading. On a seat
  with several sensors, one dead sensor does *not* clear `healthy`, but it is logged.
- A brief dropout keeps the median window. After a full window of invalid readings it is cleared, so a
  dead sensor can't hold a seat OCCUPIED indefinitely.
- The vacancy timeout runs from the last observed presence, including time when sensors were down, so
  the first absent reading after a long outage can free the seat straight away.

### Marking a seat on the touchscreen

Tap a seat on the diagram, then a button. The node makes the decision:

| Action | Effect |
|---|---|
| **Mark occupied** | Holds the seat occupied for `manual_hold_s` (default 30 min), even if empty, for someone stepping away. Ends early once someone is confirmed sitting down after the seat was seen empty. Pressing it again restarts the time. |
| **Mark vacant** | Frees the seat at once, skipping the vacancy timeout, and cancels any hold. **Refused while the sensor detects someone**, so a passer-by can't free a seat someone is sitting in. |

Anyone at the desk can press these. A hold can be restarted indefinitely by someone standing at the desk.

## MQTT message contract

This is the interface for the backend. Breaking changes will increment `schema`.

### Topics

```
{prefix}/{building}/{room}/{space_id}/state     per seat, e.g. studyspace/library/floor2/desk-07-a/state
{prefix}/{building}/{room}/{node_id}/status     per node, e.g. studyspace/library/floor2/node-07/status
```

Status is per node because an MQTT client can only have one Last Will. To relate a seat to its status,
use `node_id` in its state messages.

Segments never contain `/`, `+`, `#` or whitespace. To follow everything, subscribe to
`studyspace/+/+/+/state` and `studyspace/+/+/+/status`. Both are published with **QoS 1, retain = true**,
so a new subscriber immediately receives the latest values. QoS 1 means duplicates are possible, so
consumers must be idempotent.

### `.../state`: JSON

```json
{
  "schema": 1,
  "node_id": "node-07",
  "desk_id": "desk-07",
  "space_id": "desk-07-a",
  "state": "occupied",
  "healthy": true,
  "reason": "change",
  "source": "sensor",
  "timestamp": "2026-11-06T09:30:15.123456Z"
}
```

| Field       | Type    | Meaning |
|-------------|---------|---------|
| `schema`    | integer | Payload version, currently `1`. Reject versions you don't support. |
| `node_id`   | string  | The publishing device. |
| `desk_id`   | string  | The desk, for grouping seats (for example "2 of 3 free"). |
| `space_id`  | string  | The seat. |
| `state`     | string  | `"free"` or `"occupied"`. `unknown` is never published; before a node has decided about a seat, no state message is sent for it. |
| `healthy`   | boolean | `false` means all of the seat's sensors have been unusable for `max_consecutive_faults` polls, so `state` is a held value and may be out of date. |
| `reason`    | string  | `"change"` or `"heartbeat"`. |
| `source`    | string  | `"manual"` while a "mark occupied" hold is active, otherwise `"sensor"`. A seat freed with "mark vacant" reports `"sensor"`, as nothing is overriding the sensor from then on. |
| `timestamp` | string  | When the message was created. ISO-8601 UTC, always in this `Z` format with microseconds. |

Consumers should ignore unknown extra fields; optional fields may be added without a schema bump.
No raw sensor data (weights, distances, motion events) is ever sent, for privacy.

### When messages are sent

- `reason: "change"`: immediately when a seat's published state changes, including the first decision
  after boot and any change made on the touchscreen.
- `reason: "heartbeat"`: every `heartbeat_s` (default 60 s) since that seat's last message, and
  immediately when `healthy` or `source` changes without the state changing.

Backend guidance:

- **Detect transitions by comparing `state` with the last value, not from `reason`.** While offline,
  a node buffers only the latest message per topic, so a `change` can be superseded by a
  `heartbeat`, which is then the first message showing the new state.
- **Timestamps can be old.** A buffered message keeps the time it was created, and a retained message
  can be arbitrarily old. Pis have no battery-backed clock, so timestamps can also be wrong until NTP
  syncs after boot.
- **Treat a seat as unavailable** if its node's status is `offline`, or if no state message has
  arrived for about 3 × `heartbeat_s`.

### `.../status`: plain string

`online` or `offline` (not JSON). `online` is sent on every connect, before any buffered state.
`offline` is sent on clean shutdown, and otherwise by the broker as the MQTT Last Will once it
notices the connection has gone (this can take up to about 1.5 × the 60 s keepalive).

### Parsing in Python

`studyspace_node.messages.parse_payload()` validates a payload and returns a typed `StateMessage`, or
raises `PayloadError`.

## Local screen protocol

Newline-delimited JSON over the Unix socket at `[ui] socket_path`, documented in
[`ui_protocol.py`](src/studyspace_node/ui_protocol.py). It is local to the Pi and not a public interface.
The node pushes a `snapshot` on connect and every poll. The GUI sends `command` messages
(`mark_occupied`, `mark_vacant`), and each gets a `result` with the reason if refused. Snapshots
contain only what the screen shows, with no raw sensor readings.

## Run on boot

The two sample systemd units in [`deploy/`](deploy/) are deliberately not tied together, so either can
restart without the other. The GUI unit is written for Raspberry Pi OS Lite, drawing directly to the
display with no desktop running.

```sh
sudo useradd --system --groups gpio studyspace
sudo useradd --system --groups studyspace,video,render,input studyspace-gui
sudo python3 -m venv --system-site-packages /opt/studyspace-node/.venv
sudo /opt/studyspace-node/.venv/bin/pip install ".[pi,gui]"
# In this copy, remove the [ui] socket_path line so it uses the /run path the units expect:
sudo install -D -m 640 -g studyspace config.toml /etc/studyspace-node/config.toml
# Optional: a root-only file containing STUDYSPACE_MQTT_PASSWORD=...
sudo install -m 600 /dev/null /etc/studyspace-node/env
sudo cp deploy/studyspace-node.service deploy/studyspace-gui.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now studyspace-node studyspace-gui
journalctl -u studyspace-node -u studyspace-gui -f
```

## Tests

No hardware or network needed. Sensors, time and MQTT are faked, and socket tests use a temporary Unix
socket on the local machine.

```sh
pip install -e ".[dev,gui]"
pytest --cov
```

Rendering and GUI loop tests run on SDL's headless driver and are skipped without pygame-ce. Three
tests in `tests/test_sensors.py` also exercise real gpiozero through its mock pin factory, and are
skipped unless gpiozero is installed (`pip install -e ".[dev,gui,pi]"`).
