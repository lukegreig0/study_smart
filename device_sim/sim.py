# sim.py
import json
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

BROKER = "localhost"
PORT = 1883
DEVICE_1_ID = "placemat-0007"
DEVICE_2_ID = "placemat-0008"
DEVICE_3_ID = "placemat-0009"
DEVICE_IDS = [DEVICE_1_ID, DEVICE_2_ID, DEVICE_3_ID]
SPACE_ID = "desk-07"
HEARTBEAT_EVERY = 2  # seconds (short for testing; ~60 in real use)
RUN_FOR = 80  # seconds before the simulator shuts down


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def status_topic(device_id: str) -> str:
    return f"studyspace/library/floor2/{SPACE_ID}/{device_id}/status"


def death_payload(device_id: str) -> str:
    return json.dumps({"type": "death", "id": device_id})


def on_connect(client, device_id, flags, reason_code, properties):
    print(f"{device_id} connected: {reason_code}")
    birth = {
        "type": "birth",
        "schema": 1,
        "id": device_id,
        "space_id": SPACE_ID,
        "device_type": "placemat",
        "firmware": "0.1.0",
        "occupied": False if device_id is not DEVICE_2_ID else True,
        "timestamp": now_utc(),
    }
    client.publish(status_topic(device_id), json.dumps(birth), qos=1, retain=True)


def start_client(device_id: str) -> mqtt.Client:
    # device_id is passed as userdata, so on_connect knows which device it is
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2, client_id=device_id, userdata=device_id
    )
    client.on_connect = on_connect

    # Last Will: the broker publishes this for us if we vanish without saying goodbye
    client.will_set(status_topic(device_id), death_payload(device_id), qos=1, retain=True)

    client.connect(BROKER, PORT, keepalive=30)
    client.loop_start()  # runs the network loop in a background thread
    return client


clients = {device_id: start_client(device_id) for device_id in DEVICE_IDS}

deadline = time.monotonic() + RUN_FOR
# Device 1 to remain unoccupied
tick = 0
while time.monotonic() < deadline:
    time.sleep(HEARTBEAT_EVERY)
    for device_id, client in clients.items():
        heartbeat = {"type": "heartbeat", "id": device_id, "timestamp": now_utc()}


        if device_id == DEVICE_2_ID:
            if tick == 3:
                heartbeat["occupied"] = False
            if tick == 7:
                heartbeat["occupied"] = True
            if tick == 12:
                heartbeat["occupied"] = False
            if tick == 13:
                heartbeat["occupied"] = True
        if device_id == DEVICE_3_ID:
            if tick == 4:
                heartbeat["occupied"] = True
            if tick == 8:
                heartbeat["occupied"] = False
            if tick == 12:
                heartbeat["occupied"] = True
            if tick == 16:
                heartbeat["occupied"] = False



        client.publish(status_topic(device_id), json.dumps(heartbeat), qos=1, retain=False)
        print(f"{device_id} heartbeat sent")
    tick += 1

# A clean disconnect does NOT trigger the Last Will, so send death ourselves
for device_id, client in clients.items():
    client.publish(
        status_topic(device_id), death_payload(device_id), qos=1, retain=True
    ).wait_for_publish()
    client.disconnect()
    client.loop_stop()
print("stopped")