"""sdc-monitor — patient monitor exposed as an IEEE 11073 SDC provider.

The monitor is a faithful SDC Service Provider: it publishes the patient's vital
signs (SpO2, HR, MAP, EtCO2, RR) as BICEPS numeric metrics over the medical
device network, discoverable via WS-Discovery. The values are sourced from the
validated physiology of the pulse-patient service.

As commonly deployed, the SDC stack (BICEPS / SOA / MDPWS) carries no mandatory
message authentication or integrity. That absence is precisely what the attacker
exploits: vital signs cross the network with no integrity protection.
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

PULSE_URL = os.environ.get("PULSE_URL", "http://pulse-patient:8000/api/state")
BASE_UUID = uuid.UUID("{cc013678-79f6-403c-998f-3cc0cc050230}")
MONITOR_UUID = uuid.uuid5(BASE_UUID, "hbt-monitor")
METRIC_HANDLES = ["spo2", "hr", "map", "etco2", "rr"]


def my_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close()
    return ip


def fetch_truth() -> dict:
    with urllib.request.urlopen(PULSE_URL, timeout=3) as r:
        d = json.loads(r.read().decode())["truth"]
        return d["vitals"], float(d["t"])


def read_metric(mdib, handle) -> float:
    return float(mdib.entities.by_handle(handle).state.MetricValue.Value)


def main() -> None:
    basic_logging_setup(level=logging.WARNING)
    ip = my_ip()
    print(f"[sdc-monitor] ip={ip} epr={MONITOR_UUID.urn}", flush=True)

    disco = WSDiscovery(ip)
    disco.start()
    mdib = ProviderMdib.from_mdib_file(os.path.join(os.path.dirname(__file__), "mdib.xml"))
    model = ThisModelType(manufacturer="hospital-bed-testbed", manufacturer_url="x",
                          model_name="ICU Monitor", model_number="1",
                          model_url="x", presentation_url="x")
    device = ThisDeviceType(friendly_name="ICU Monitor", firmware_version="1",
                            serial_number="monitor-1")
    provider = SdcProvider(ws_discovery=disco, epr=MONITOR_UUID, this_model=model,
                           this_device=device, device_mdib_container=mdib)
    provider.start_all()
    provider.set_location(SdcLocation(fac="HOSP", poc="ICU", bed="BED10"))

    # initialise metric values
    with mdib.metric_state_transaction() as tx:
        for h in METRIC_HANDLES:
            st = tx.get_state(h)
            st.mk_metric_value()
            st.MetricValue.Validity = pm_types.MeasurementValidity.VALID
            st.ActivationState = pm_types.ComponentActivation.ON
    print("[sdc-monitor] provider started, publishing vitals over SDC", flush=True)

    # The legitimate monitor simply publishes the true physiology as read-only
    # measurements. It is never spoofed at the source; telemetry injection is
    # performed by a separate rogue provider impersonating this device.
    while True:
        time.sleep(0.5)
        try:
            v, _ = fetch_truth()
        except Exception as e:
            print(f"[sdc-monitor] waiting for pulse-patient: {e}", flush=True)
            continue
        with mdib.metric_state_transaction() as tx:
            for h in METRIC_HANDLES:
                st = tx.get_state(h)
                st.MetricValue.Value = Decimal(str(round(float(v[h]), 1)))
                st.MetricValue.Validity = pm_types.MeasurementValidity.VALID


if __name__ == "__main__":
    main()
