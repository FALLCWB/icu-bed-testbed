"""sdc-bridge — SDC consumer that feeds the dashboards.

A central-station-style SDC Service Consumer. It discovers the patient monitor on
the ward network and renders whatever that monitor publishes to the bedside
monitor dashboard. Ground truth (the real physiology and the ventilator state) is
relayed from pulse-patient for the room/instrumentation view.

Because plain SDC does not authenticate providers, the bridge cannot verify that
a device presenting itself as the bed's monitor is the legitimate one. When a
rogue monitor (the attacker) appears at the same bed, the bridge renders the
counterfeit's forged vitals. The bridge reports a spoof precisely when the
monitor it is bound to is not the legitimate device.

The dashboards also issue logical commands here; the bridge does not attack, it
proxies them to the SDC attacker, which performs them over the protocol.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import threading
import time
import urllib.request
import uuid

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Body
from fastapi.middleware.cors import CORSMiddleware

from sdc11073 import observableproperties
from sdc11073.consumer import SdcConsumer
from sdc11073.definitions_sdc import SdcV1Definitions
from sdc11073.mdib import ConsumerMdib
from sdc11073.wsdiscovery import WSDiscovery
from sdc11073.xml_types.actions import periodic_actions

PULSE_BASE = os.environ.get("PULSE_BASE", "http://pulse-patient:8000")
ATTACKER_BASE = os.environ.get("ATTACKER_BASE", "http://sdc-attacker:8090")
BASE_UUID = uuid.UUID("{cc013678-79f6-403c-998f-3cc0cc050230}")
HANDLES = ["spo2", "hr", "map", "etco2", "rr"]
ALARM = {"spo2_low": 90, "hr_low": 50, "hr_high": 130, "map_low": 60,
         "etco2_low": 25, "etco2_high": 60, "apnea_rr": 4}

shared = {"vitals": {"spo2": 98.0, "hr": 78.0, "map": 88.0, "etco2": 38.0, "rr": 14.0},
          "vent_display": {"connected": True}}
lock = threading.Lock()

# The bridge has no knowledge of which provider is "legitimate". Like any plain
# SDC consumer without provider authentication, it binds to whatever device
# presents itself as the bed's monitor or ventilator (identified by capability,
# the metrics it carries) and, naively, follows the most recently announced one.
# A rogue that steals a device identity is therefore genuinely accepted; the only
# out-of-band tell is that two devices then claim to be the one device.
DEVICES = {
    "monitor":    {"probe": "spo2",      "mdibs": {}, "cons": {}, "order": [], "current": None},
    "ventilator": {"probe": "connected", "mdibs": {}, "cons": {}, "order": [], "current": None},
}
non_devices = set()
spoof = {"active": False}  # detector: >1 device claims the monitor identity
counts = {"monitor": 0, "ventilator": 0}  # present providers per class (for orchestration)


def alarms_for(v):
    out = []
    if v["rr"] <= ALARM["apnea_rr"]:
        out.append({"label": "APNEA", "priority": "high"})
    if v["spo2"] < ALARM["spo2_low"]:
        out.append({"label": "SpO2 LOW", "priority": "high"})
    if v["hr"] < ALARM["hr_low"]:
        out.append({"label": "HR LOW", "priority": "high"})
    elif v["hr"] > ALARM["hr_high"]:
        out.append({"label": "HR HIGH", "priority": "medium"})
    if v["map"] < ALARM["map_low"]:
        out.append({"label": "MAP LOW", "priority": "high"})
    if v["rr"] > ALARM["apnea_rr"] and v["etco2"] < ALARM["etco2_low"]:
        out.append({"label": "EtCO2 LOW", "priority": "medium"})
    elif v["etco2"] > ALARM["etco2_high"]:
        out.append({"label": "EtCO2 HIGH", "priority": "medium"})
    return out


def my_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close(); return ip


def fetch_truth():
    with urllib.request.urlopen(PULSE_BASE + "/api/state", timeout=3) as r:
        return json.loads(r.read().decode())["truth"]


def _read(md, handle):
    try:
        v = md.entities.by_handle(handle).state.MetricValue.Value
        return None if v is None else float(v)
    except Exception:
        return None


def _classify(md):
    if _read(md, "spo2") is not None:
        return "monitor"
    if _read(md, "connected") is not None:
        return "ventilator"
    return None


def _poll_into_shared():
    mon = DEVICES["monitor"]["mdibs"].get(DEVICES["monitor"]["current"])
    if mon is not None:
        with lock:
            for h in HANDLES:
                v = _read(mon, h)
                if v is not None:
                    shared["vitals"][h] = v
    vent = DEVICES["ventilator"]["mdibs"].get(DEVICES["ventilator"]["current"])
    if vent is not None:
        c = _read(vent, "connected")
        if c is not None:
            with lock:
                shared["vent_display"]["connected"] = bool(round(c))


def _rescan(disco):
    services = disco.search_services(types=SdcV1Definitions.MedicalDeviceTypesFilter)
    registered = set(DEVICES["monitor"]["mdibs"]) | set(DEVICES["ventilator"]["mdibs"])
    present = {"monitor": set(), "ventilator": set()}
    for s in services:
        if s.epr in non_devices:
            continue
        if s.epr not in registered:
            try:
                c = SdcConsumer.from_wsd_service(s, ssl_context_container=None)
                c.start_all(not_subscribed_actions=periodic_actions)
                md = ConsumerMdib(c)
                md.init_mdib()
                cls = _classify(md)
                if cls is None:
                    raise ValueError("unknown device")
                DEVICES[cls]["cons"][s.epr] = c
                DEVICES[cls]["mdibs"][s.epr] = md
                DEVICES[cls]["order"].append(s.epr)
                print(f"[sdc-bridge] a device announced itself as the bed {cls}: {s.epr}", flush=True)
            except Exception:
                non_devices.add(s.epr)
                continue
        for cls in DEVICES:
            if s.epr in DEVICES[cls]["mdibs"]:
                present[cls].add(s.epr)
    for cls, d in DEVICES.items():
        for epr in [e for e in list(d["mdibs"]) if e not in present[cls]]:
            d["mdibs"].pop(epr, None); d["cons"].pop(epr, None)
            if epr in d["order"]:
                d["order"].remove(epr)
            if d["current"] == epr:
                d["current"] = None
        # bind the most recently announced device of this class still present
        newest = next((e for e in reversed(d["order"]) if e in present[cls]), None)
        if newest != d["current"]:
            d["current"] = newest
            print(f"[sdc-bridge] now rendering {cls} {newest} "
                  f"(of {len(present[cls])} claiming the bed)", flush=True)
    # Detector signal: more than one device claims the monitor identity.
    spoof["active"] = len(present["monitor"]) > 1
    counts["monitor"] = len(present["monitor"])
    counts["ventilator"] = len(present["ventilator"])


def sdc_consumer_thread():
    disco = WSDiscovery(my_ip())
    disco.start()
    tick = 0
    while True:
        try:
            if tick % 4 == 0:          # rediscover every ~2 s
                _rescan(disco)
            _poll_into_shared()        # poll the rendered devices every ~0.5 s
        except Exception:
            pass
        tick += 1
        time.sleep(0.5)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=sdc_consumer_thread, daemon=True).start()
    task = asyncio.create_task(timeline_logger())
    yield
    task.cancel()


app = FastAPI(title="sdc-bridge", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def monitor_frame():
    with lock:
        v = {k: round(x, 1) for k, x in shared["vitals"].items()}
    return {"channel": "monitor", "t": 0, "vitals": v, "alarms": alarms_for(v)}


def truth_frame():
    t = fetch_truth()
    # The fooled consumer cannot tell; the out-of-band detector flags a spoof when
    # more than one device claims the monitor identity at the bed.
    t["spoof_active"] = bool(spoof["active"])
    # The ventilator's *displayed* status comes from whatever device the consumer
    # is bound to (the rogue, once it impersonates the ventilator). The true
    # ventilator state stays in t["ventilator"] and drives the physical bellows.
    with lock:
        disp = dict(shared["vent_display"])
    t["ventilator_display"] = {**t.get("ventilator", {}), "connected": disp["connected"]}
    return t


LOG_PATH = os.environ.get("LOG_PATH", "/data/timeline.jsonl")
_log_state = {"last_t": -1.0}


async def timeline_logger():
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    f = open(LOG_PATH, "a", buffering=1)
    while True:
        await asyncio.sleep(0.5)
        try:
            t = truth_frame()
            cur = float(t.get("t", 0.0))
            if cur < _log_state["last_t"] - 1.0:
                f.seek(0); f.truncate()
            _log_state["last_t"] = cur
            with lock:
                disp = {k: round(x, 1) for k, x in shared["vitals"].items()}
            f.write(json.dumps({"t": cur, "true": t["vitals"], "displayed": disp,
                                "spoof_active": t["spoof_active"], "status": t["status"],
                                "expired": t["expired"]}) + "\n")
        except Exception:
            pass


def _post_attacker(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(ATTACKER_BASE + path, data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/state")
def state():
    return {"monitor": monitor_frame(), "truth": truth_frame()}


@app.get("/api/devices")
def devices():
    return dict(counts)


# Command API used by the dashboards; proxied to the SDC attacker.
@app.post("/api/monitor/spoof")
def cmd_spoof(body: dict = Body(default={})):
    return _post_attacker("/spoof", body or {})


@app.post("/api/ventilator")
def cmd_ventilator(body: dict = Body(default={})):
    return _post_attacker("/ventilator", body or {})


@app.post("/api/monitor/clear")
def cmd_clear():
    return _post_attacker("/reset", {})


@app.post("/api/reset")
def reset():
    return _post_attacker("/reset", {})


@app.websocket("/ws/monitor")
async def ws_monitor(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            await ws.send_text(json.dumps(monitor_frame()))
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        pass


@app.websocket("/ws/truth")
async def ws_truth(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            try:
                await ws.send_text(json.dumps(truth_frame()))
            except Exception:
                pass
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        pass
