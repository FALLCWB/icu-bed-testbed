# icu-bed-testbed

[![DOI](https://zenodo.org/badge/1279769496.svg)](https://zenodo.org/badge/latestdoi/1279769496)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

A reproducible, containerized testbed that emulates an intensive-care bed for
medical-device cybersecurity research and teaching. Validated physiology drives
ventilator and patient-monitor **digital twins** exposed over the real
point-of-care interoperability protocol (IEEE 11073 SDC); an unauthenticated
attacker on that network performs a clinically silent, lethal attack.

The worked attack is a **double false-data injection**:

- **Actuation injection** — the attacker, an unauthenticated SDC consumer, issues
  a `SetValue` that commands the ventilator into a non-ventilating state.
- **Telemetry injection** — the attacker stands up **rogue SDC providers that
  impersonate both the patient monitor and the ventilator** (SDC does not
  authenticate providers), so every networked readout, the monitor *and* the
  ventilator screen, keeps showing "all normal".

The result is a **silent killer**: every digital display reads normal and no
network-sourced alarm fires while the patient deteriorates to cardiac arrest. The
one thing the attack cannot forge is a physical quantity, the ventilator's
**bellows**, driven by the true delivered gas, simply stops. That stopped pump,
and the duplicate device identities visible on the wire, are the only honest
signals left. The point: hospital infrastructure must be managed as critical
infrastructure, with integrity protection (and monitoring) on both device
actuation and device telemetry.

> The patient physiology is provided by the Pulse Physiology Engine (validated
> against the literature). The SDC stack is for testing/demonstration only and
> must never be connected to clinical equipment.

## Architecture

| Service | Role | Port |
|---|---|---|
| `pulse-patient` | Pulse Physiology Engine: ventilator-dependent patient + control API | (internal) |
| `sdc-ventilator` | Ventilator **digital twin** — SDC provider; settings settable (actuation surface) | (SDC) |
| `sdc-monitor` | Patient-monitor **digital twin** — SDC provider publishing read-only vital metrics | (SDC) |
| `sdc-bridge` | SDC consumer → WebSocket feed for the dashboards; true-vs-displayed logger | 8000 |
| `monitor` | Bedside monitor dashboard (renders the SDC-displayed, spoofable vitals) | 8083 |
| `room` | Room / digital-twin view (ground-truth panel + physical bellows) | 8082 |
| `sdc-attacker` | Unauthenticated SDC consumer + rogue providers + attack console | 8090 |

Data flow: **Pulse** (truth) → `sdc-monitor` / `sdc-ventilator` (device twins, SDC
providers) → `sdc-bridge` (SDC consumer) → dashboards. The `sdc-bridge` has no
notion of which provider is legitimate: like any unauthenticated consumer it binds
to whichever device announces itself as the bed's monitor/ventilator, so a rogue
that steals an identity is genuinely accepted. Actuation is attacked with an
unauthenticated `SetValue`; telemetry is attacked by the rogue providers. The
room's bellows is animated from the **true** delivery, so it is the one display
element the attack cannot spoof.

## Quick start

```bash
make up
#  room     http://localhost:8082   (digital twin: bellows + ground-truth panel)
#  monitor  http://localhost:8083   (bedside monitor, fed over SDC — spoofable)
#  attacker http://localhost:8090   (attack console)
```

Open `room` and `monitor` side by side and click **Silent kill** in the attacker
console (it arms the rogue providers, then disconnects). The monitor and the
ventilator screen both keep reading normal with no alarm, while the room's
ground-truth panel shows the patient deteriorating and the bellows stops.

## Reproduce the headline results

```bash
make demo        # capture canonical silent-kill + reversibility logs, render the figure
make figure      # re-render figures/timeline.pdf from avaliacao/canonical/
make smoke-test  # check the device twins are reachable over SDC
```

`SPEED` (in `docker-compose.yml`, default 4.0) accelerates wall-clock for a
watchable demo; set it to 1.0 for clinically real-time deterioration.

## Why the combination is lethal

Disconnection alone triggers alarms and is survivable if reversed in time
(`make demo` also captures this reversibility control). Telemetry injection alone
is a false picture with no physical harm. Together they remove the feedback loop
clinical safety depends on: the patient is harmed while every networked display
says otherwise, and only the physical bellows betrays it.

## How to cite

This testbed is archived on Zenodo; the DOI badge above resolves to the latest
release. Please cite the version you used (metadata also in [`CITATION.cff`](CITATION.cff)):

> de Faria, R. A.; Lemos, F. A. da L.; Oroski, E. *icu-bed-testbed: A Reproducible
> Containerized Testbed for Demonstrating Cybersecurity Risk in Hospital Bedside
> Devices* (v0.1.0). Zenodo. https://doi.org/10.5281/zenodo.<resolved by the badge above>

## License

Code is released under the Apache License 2.0; see [`LICENSE`](LICENSE).
