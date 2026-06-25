"""sdc-ventilator — mechanical ventilator as an IEEE 11073 SDC provider.

The ventilator settings (tidal volume, respiratory rate, PEEP, FiO2, and the
patient-circuit connection) are exposed as settable BICEPS numeric metrics with
SetValueOperations in the SCO. The default SDC provider handles incoming
SetValue operations and writes the new value to the target metric; a background
loop then forwards any change to the pulse-patient physiology engine.

Because plain SDC carries no authentication on the SetService, any consumer on
the network can invoke these operations. That is the actuation attack surface:
an attacker disconnects the circuit (connected -> 0) or sabotages the settings,
and the validated physiology responds.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
import urllib.request
import uuid
from decimal import Decimal

from sdc11073.location import SdcLocation
from sdc11073.loghelper import basic_logging_setup
from sdc11073.mdib import ProviderMdib
from sdc11073.provider import SdcProvider
from sdc11073.wsdiscovery import WSDiscovery
from sdc11073.xml_types import pm_types
from sdc11073.xml_types.dpws_types import ThisDeviceType, ThisModelType

PULSE_BASE = os.environ.get("PULSE_BASE", "http://pulse-patient:8000")
BASE_UUID = uuid.UUID("{cc013678-79f6-403c-998f-3cc0cc050230}")
VENT_UUID = uuid.uuid5(BASE_UUID, "hbt-ventilator")

SETTINGS = ["tidal_volume", "resp_rate", "peep", "fio2", "connected"]
BASELINE = {"tidal_volume": 450.0, "resp_rate": 14.0, "peep": 5.0, "fio2": 0.40, "connected": 1.0}


def my_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close()
    return ip


def post(path: str, body: dict) -> None:
    req = urllib.request.Request(PULSE_BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=3).read()


def pulse_t() -> float:
    with urllib.request.urlopen(PULSE_BASE + "/api/state", timeout=3) as r:
        return float(json.loads(r.read().decode())["truth"]["t"])


def read_metric(mdib, handle) -> float:
    return float(mdib.entities.by_handle(handle).state.MetricValue.Value)


def set_metrics(mdib, values: dict) -> None:
    with mdib.metric_state_transaction() as tx:
        for h, val in values.items():
            st = tx.get_state(h)
            if st.MetricValue is None:
                st.mk_metric_value()
            st.MetricValue.Value = Decimal(str(val))
            st.MetricValue.Validity = pm_types.MeasurementValidity.VALID
            st.ActivationState = pm_types.ComponentActivation.ON


def main() -> None:
    basic_logging_setup(level=logging.WARNING)
    ip = my_ip()
    print(f"[sdc-ventilator] ip={ip} epr={VENT_UUID.urn}", flush=True)

    disco = WSDiscovery(ip); disco.start()
    mdib = ProviderMdib.from_mdib_file(os.path.join(os.path.dirname(__file__), "mdib.xml"))
    model = ThisModelType(manufacturer="hospital-bed-testbed", manufacturer_url="x",
                          model_name="ICU Ventilator", model_number="1",
                          model_url="x", presentation_url="x")
    device = ThisDeviceType(friendly_name="ICU Ventilator", firmware_version="1",
                            serial_number="ventilator-1")
    provider = SdcProvider(ws_discovery=disco, epr=VENT_UUID, this_model=model,
                           this_device=device, device_mdib_container=mdib)
    provider.start_all()
    provider.set_location(SdcLocation(fac="HOSP", poc="ICU", bed="BED10"))

    set_metrics(mdib, BASELINE)
    last = dict(BASELINE)
    last_t = 0.0
    print("[sdc-ventilator] provider started; settings settable over SDC (no auth)", flush=True)

    while True:
        time.sleep(0.5)
        # detect a physiology reset and restore baseline settings
        try:
            t = pulse_t()
            if t < last_t - 1.0:
                set_metrics(mdib, BASELINE)
                last = dict(BASELINE)
            last_t = t
        except Exception:
            pass

        # forward any setting changed by a (possibly malicious) SetValue operation
        try:
            cur = {h: read_metric(mdib, h) for h in SETTINGS}
        except Exception:
            continue
        changed = {h: cur[h] for h in SETTINGS if abs(cur[h] - last.get(h, cur[h])) > 1e-6}
        if changed:
            body = {}
            for h, val in changed.items():
                if h == "connected":
                    body["connected"] = bool(round(val))
                else:
                    body[h] = val
            try:
                post("/api/ventilator", body)
                print(f"[sdc-ventilator] forwarded SDC set -> pulse: {body}", flush=True)
                last.update(cur)
            except Exception as e:
                print(f"[sdc-ventilator] forward failed: {e}", flush=True)


if __name__ == "__main__":
    main()
