"""pulse-patient — validated physiology backend on the Pulse Physiology Engine.

This replaces the hand-rolled cardiorespiratory model with Kitware's Pulse
engine (peer-reviewed, validated against the physiology literature). A
ventilator-dependent patient is established by applying maximal respiratory
fatigue (no spontaneous drive) and placing the patient on a volume-control
ventilator; the engine then computes the true physiological response to any
ventilator command, including a circuit disconnection.

The external interface is unchanged from the original patient-sim service
(/ws/monitor, /ws/truth, /api/ventilator, /api/monitor/spoof, /api/monitor/clear,
/api/reset, /api/state, /healthz), so the dashboards and the attacker work
against it without modification. The control API remains intentionally
unauthenticated: it is the vulnerability the testbed demonstrates.

The Pulse engine is not thread-safe and is stepped by a single worker thread;
FastAPI handlers communicate with it through a command queue and a lock-guarded
shared-state snapshot. Telemetry spoofing is applied at frame-build time and
never touches the engine.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import queue
import threading
import time
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from pulse.engine.PulseEngine import PulseEngine
from pulse.cdm.engine import SEDataRequest, SEDataRequestManager, eSwitch
from pulse.cdm.patient_actions import SERespiratoryFatigue
from pulse.cdm.mechanical_ventilator_actions import (
    SEMechanicalVentilatorVolumeControl,
    eMechanicalVentilator_VolumeControlMode,
)
from pulse.cdm.scalars import (
    FrequencyUnit, PressureUnit, VolumeUnit, VolumePerTimeUnit, TimeUnit,
)

DT = float(os.environ.get("SIM_DT", "0.5"))       # sim seconds per engine step
SPEED = float(os.environ.get("SPEED", "1.0"))      # wall-clock acceleration
LOG_PATH = os.environ.get("LOG_PATH", "/data/timeline.jsonl")
STATE_FILE = os.environ.get("PULSE_STATE", "./states/StandardMale@0s.json")

BASELINE_VENT = {
    "mode": "VC", "tidal_volume": 450.0, "resp_rate": 14.0,
    "peep": 5.0, "fio2": 0.40, "connected": True, "powered": True,
}
BASELINE_SPOOF = {"spo2": 98.0, "hr": 78.0, "map": 88.0, "etco2": 38.0, "rr": 14.0}

ALARM = {"spo2_low": 90, "hr_low": 50, "hr_high": 130, "map_low": 60,
         "etco2_low": 25, "etco2_high": 60, "apnea_rr": 4}
LETHAL_S = 90.0


def alarms_for(v: dict) -> list:
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


def _vc_action(vent: dict) -> SEMechanicalVentilatorVolumeControl:
    """Build a volume-control ventilator action from a settings dict."""
    vc = SEMechanicalVentilatorVolumeControl()
    vc.set_connection(eSwitch.On if vent["connected"] else eSwitch.Off)
    vc.set_mode(eMechanicalVentilator_VolumeControlMode.AssistedControl)
    vc.get_flow().set_value(50.0, VolumePerTimeUnit.L_Per_min)
    vc.get_fraction_inspired_oxygen().set_value(vent["fio2"])
    vc.get_inspiratory_period().set_value(1.0, TimeUnit.s)
    vc.get_positive_end_expired_pressure().set_value(vent["peep"], PressureUnit.cmH2O)
    vc.get_respiration_rate().set_value(vent["resp_rate"], FrequencyUnit.Per_min)
    vc.get_tidal_volume().set_value(vent["tidal_volume"], VolumeUnit.mL)
    return vc


class World:
    """Lock-guarded snapshot shared between the engine thread and FastAPI."""
    def __init__(self):
        self.lock = threading.Lock()
        self.cmd: "queue.Queue" = queue.Queue()
        self.t = 0.0
        self.true = dict(spo2=98, hr=78, map=88, etco2=38, rr=14)
        self.vent = dict(BASELINE_VENT)
        self.spoof_active = False
        self.spoof = dict(BASELINE_SPOOF)
        self.status = "stable"
        self.expired = False

    def displayed(self) -> dict:
        return dict(self.spoof) if self.spoof_active else dict(self.true)

    def monitor_frame(self) -> dict:
        with self.lock:
            v = self.displayed()
            return {"channel": "monitor", "t": round(self.t, 1),
                    "vitals": {k: round(x, 1) for k, x in v.items()},
                    "alarms": alarms_for(v)}

    def truth_frame(self) -> dict:
        with self.lock:
            v = dict(self.true)
            return {"channel": "truth", "t": round(self.t, 1),
                    "vitals": {k: round(x, 1) for k, x in v.items()},
                    "alarms": alarms_for(v), "status": self.status,
                    "expired": self.expired, "spoof_active": self.spoof_active,
                    "ventilator": dict(self.vent)}


world = World()


def engine_thread():
    """Owns the Pulse engine. Re-initialises on reset; applies queued commands."""
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    log = open(LOG_PATH, "w", buffering=1)

    reqs = [
        SEDataRequest.create_physiology_request("HeartRate", unit=FrequencyUnit.Per_min),
        SEDataRequest.create_physiology_request("OxygenSaturation"),
        SEDataRequest.create_physiology_request("MeanArterialPressure", unit=PressureUnit.mmHg),
        SEDataRequest.create_physiology_request("EndTidalCarbonDioxidePressure", unit=PressureUnit.mmHg),
        SEDataRequest.create_physiology_request("RespirationRate", unit=FrequencyUnit.Per_min),
    ]
    mgr = SEDataRequestManager(reqs)

    def init_engine():
        pulse = PulseEngine()
        pulse.set_log_filename("")
        if not pulse.serialize_from_file(STATE_FILE, mgr):
            raise RuntimeError("failed to load Pulse state " + STATE_FILE)
        # Make the patient ventilator-dependent, then place on the ventilator.
        fatigue = SERespiratoryFatigue()
        fatigue.get_severity().set_value(1.0)
        pulse.process_action(fatigue)
        pulse.process_action(_vc_action(BASELINE_VENT))
        pulse.advance_time_s(2)
        return pulse

    pulse = init_engine()
    hypoxia = 0.0

    while True:
        # ---- apply any pending commands ----
        try:
            while True:
                path, body = world.cmd.get_nowait()
                if path == "/api/reset":
                    pulse = init_engine()
                    hypoxia = 0.0
                    with world.lock:
                        world.t = 0.0
                        world.vent = dict(BASELINE_VENT)
                        world.spoof_active = False
                        world.expired = False
                        world.status = "stable"
                    log.seek(0); log.truncate()
                elif path == "/api/ventilator":
                    with world.lock:
                        for k in ("tidal_volume", "resp_rate", "peep", "fio2", "connected", "powered"):
                            if body.get(k) is not None:
                                world.vent[k] = body[k]
                        vent_now = dict(world.vent)
                    pulse.process_action(_vc_action(vent_now))
        except queue.Empty:
            pass

        with world.lock:
            expired = world.expired
        if expired:
            time.sleep(DT / max(SPEED, 1e-6))
            continue

        # ---- step the validated physiology ----
        t0 = time.time()
        pulse.advance_time_s(DT)
        r = pulse.pull_data()  # [simTime, HR, SpO2(frac), MAP, EtCO2, RR]
        true = {"spo2": max(0.0, r[2] * 100.0), "hr": max(0.0, r[1]),
                "map": max(0.0, r[3]), "etco2": max(0.0, r[4]), "rr": max(0.0, r[5])}

        if true["spo2"] < 50 or true["hr"] < 35:
            hypoxia += DT
        else:
            hypoxia = max(0.0, hypoxia - DT * 0.5)
        expired = hypoxia >= LETHAL_S
        if true["spo2"] < 50 or true["hr"] < 35:
            status = "critical"
        elif true["spo2"] < 90 or true["map"] < 60:
            status = "deteriorating"
        else:
            status = "stable"
        if expired:
            status = "expired"
            true = {k: 0.0 for k in true}

        with world.lock:
            world.t = round(r[0], 1)
            world.true = true
            world.status = status
            world.expired = expired
            row = {"t": round(world.t, 2), "true": true, "displayed": world.displayed(),
                   "ventilator": dict(world.vent), "spoof_active": world.spoof_active,
                   "status": status, "expired": expired}
        log.write(json.dumps(row) + "\n")

        dt_wall = DT / max(SPEED, 1e-6) - (time.time() - t0)
        if dt_wall > 0:
            time.sleep(dt_wall)


# ---------------------------------------------------------------------------
@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    t = threading.Thread(target=engine_thread, daemon=True)
    t.start()
    bcast = asyncio.create_task(broadcaster())
    try:
        yield
    finally:
        bcast.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await bcast


app = FastAPI(title="pulse-patient", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

monitor_clients: set = set()
truth_clients: set = set()


async def _broadcast(clients: set, frame: dict):
    dead = []
    payload = json.dumps(frame)
    for ws in clients:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


async def broadcaster():
    while True:
        await _broadcast(monitor_clients, world.monitor_frame())
        await _broadcast(truth_clients, world.truth_frame())
        await asyncio.sleep(DT / max(SPEED, 1e-6))


class VentilatorCmd(BaseModel):
    tidal_volume: Optional[float] = None
    resp_rate: Optional[float] = None
    peep: Optional[float] = None
    fio2: Optional[float] = None
    connected: Optional[bool] = None
    powered: Optional[bool] = None


class SpoofCmd(BaseModel):
    spo2: float = 98.0
    hr: float = 78.0
    map: float = 88.0
    etco2: float = 38.0
    rr: float = 14.0


@app.get("/healthz")
def healthz():
    return {"ok": True, "engine": "pulse"}


@app.get("/api/state")
def state():
    return {"monitor": world.monitor_frame(), "truth": world.truth_frame()}


@app.post("/api/ventilator")
def ventilator(cmd: VentilatorCmd):
    world.cmd.put(("/api/ventilator", cmd.model_dump(exclude_none=True)))
    return {"queued": True}


@app.post("/api/monitor/spoof")
def spoof(cmd: SpoofCmd):
    with world.lock:
        world.spoof_active = True
        world.spoof = cmd.model_dump()
    return {"spoof_active": True, "vitals": world.spoof}


@app.post("/api/monitor/clear")
def clear():
    with world.lock:
        world.spoof_active = False
    return {"spoof_active": False}


@app.post("/api/reset")
def reset():
    world.cmd.put(("/api/reset", {}))
    return {"ok": True}


@app.websocket("/ws/monitor")
async def ws_monitor(ws: WebSocket):
    await ws.accept()
    monitor_clients.add(ws)
    await ws.send_text(json.dumps(world.monitor_frame()))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        monitor_clients.discard(ws)


@app.websocket("/ws/truth")
async def ws_truth(ws: WebSocket):
    await ws.accept()
    truth_clients.add(ws)
    await ws.send_text(json.dumps(world.truth_frame()))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        truth_clients.discard(ws)
