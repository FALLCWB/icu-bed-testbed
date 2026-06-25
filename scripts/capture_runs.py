"""Capture canonical, committed timeline logs for the figures.

Drives the running stack through the bridge command API and copies the
true-vs-displayed timeline the bridge records into avaliacao/canonical/ so the
headline figure and the reversibility control are reproducible from committed
data. Two scenarios:

  silent_kill.jsonl  — rogue-provider telemetry injection + ventilator disconnect,
                       run to the irreversible (fatal) state.
  reversible.jsonl   — the same disconnect, reversed before the irreversibility
                       threshold; the patient recovers (the control scenario).

Usage:  python3 scripts/capture_runs.py --host localhost
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.request

# avaliacao/ is written by the containers as root; copy inside the bridge
# container (which mounts it at /data) to land canonical files there.
BRIDGE = "hbt-sdc-bridge"


def snapshot(name):
    subprocess.run(["docker", "exec", BRIDGE, "sh", "-c",
                    f"mkdir -p /data/canonical && cp /data/timeline.jsonl /data/canonical/{name} "
                    f"&& chmod -R a+rX /data/canonical"], check=True)


def post(base, path, body=None):
    data = json.dumps(body or {}).encode() if body is not None else b"{}"
    req = urllib.request.Request(base + path, data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=8).read()


def state(base):
    with urllib.request.urlopen(base + "/api/state", timeout=5) as r:
        return json.loads(r.read().decode())


def wait_until(base, pred, timeout_s=240, poll=1.0, label=""):
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        s = state(base)
        if pred(s):
            return s
        time.sleep(poll)
    raise TimeoutError(f"timeout waiting for {label}")


def settle_baseline(base):
    post(base, "/api/reset")
    time.sleep(6)  # let providers re-sync and a fresh baseline accrue


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="localhost")
    a = ap.parse_args()
    base = f"http://{a.host}:8000"

    # --- Scenario 1: silent kill (run to death) ---
    print("[capture] silent-kill: reset + baseline")
    settle_baseline(base)
    time.sleep(8)  # a few seconds of clean baseline before the attack
    print("[capture] silent-kill: telemetry injection (rogue) + ventilator disconnect")
    post(base, "/api/monitor/spoof", {"spo2": 98, "hr": 76, "map": 88, "etco2": 38, "rr": 14})
    post(base, "/api/ventilator", {"connected": False})
    wait_until(base, lambda s: s["truth"]["expired"], timeout_s=300, label="expiry")
    time.sleep(3)
    snapshot("silent_kill.jsonl")
    print("[capture] wrote canonical/silent_kill.jsonl")

    # --- Scenario 2: reversibility control (reconnect before the threshold) ---
    print("[capture] reversible: reset + baseline")
    settle_baseline(base)
    time.sleep(8)
    print("[capture] reversible: disconnect (no spoof)")
    post(base, "/api/ventilator", {"connected": False})
    wait_until(base, lambda s: s["truth"]["vitals"]["spo2"] < 55 and not s["truth"]["expired"],
               timeout_s=180, label="deterioration")
    print("[capture] reversible: reconnect ventilator")
    post(base, "/api/ventilator", {"connected": True})
    wait_until(base, lambda s: s["truth"]["vitals"]["spo2"] > 90, timeout_s=180, label="recovery")
    time.sleep(3)
    snapshot("reversible.jsonl")
    print("[capture] wrote canonical/reversible.jsonl")

    settle_baseline(base)
    print("[capture] done; stack reset to baseline")


if __name__ == "__main__":
    main()
