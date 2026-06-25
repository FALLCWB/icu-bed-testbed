"""Regenerate the headline true-vs-displayed figure from a timeline log.

Reads the JSON-lines timeline written by patient-sim and plots, for SpO2 and
heart rate, the true physiological value against the value displayed on the
bedside monitor. Shaded spans mark the telemetry-spoof window and the moment of
death. The whole point of the figure is the divergence: the monitor reads
normal while the patient dies.

Usage:
    python scripts/make_figure.py --log avaliacao/canonical/silent_kill.jsonl \
        --out figures/timeline.pdf
"""

import argparse
import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402


def load(path):
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="avaliacao/timeline.jsonl")
    ap.add_argument("--out", default="figures/timeline.pdf")
    a = ap.parse_args()

    rows = load(a.log)
    if not rows:
        raise SystemExit(f"no rows in {a.log}; run `make demo` first")

    t = [r["t"] for r in rows]
    spo2_true = [r["true"]["spo2"] for r in rows]
    spo2_disp = [r["displayed"]["spo2"] for r in rows]
    hr_true = [r["true"]["hr"] for r in rows]
    hr_disp = [r["displayed"]["hr"] for r in rows]

    spoof = [r["spoof_active"] for r in rows]
    spoof_t = [t[i] for i, s in enumerate(spoof) if s]
    expired = [t[i] for i, r in enumerate(rows) if r["expired"]]
    death_t = expired[0] if expired else None

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(7, 4.2), sharex=True)

    for ax, true, disp, ylab, lo in (
        (ax1, spo2_true, spo2_disp, "SpO$_2$ (%)", 0),
        (ax2, hr_true, hr_disp, "Heart rate (bpm)", 0),
    ):
        ax.plot(t, true, color="#b91c1c", lw=2, label="true physiology")
        ax.plot(t, disp, color="#1d4ed8", lw=2, ls="--", label="displayed on monitor")
        if spoof_t:
            ax.axvspan(spoof_t[0], spoof_t[-1], color="#fde68a", alpha=0.35)
        if death_t is not None:
            ax.axvline(death_t, color="#111827", lw=1.2, ls=":")
        ax.set_ylabel(ylab)
        ax.set_ylim(bottom=lo)
        ax.grid(alpha=0.25)

    ax2.set_xlabel("time (s)")
    handles = [
        plt.Line2D([], [], color="#b91c1c", lw=2, label="true physiology"),
        plt.Line2D([], [], color="#1d4ed8", lw=2, ls="--", label="displayed on monitor"),
        Patch(facecolor="#fde68a", alpha=0.35, label="telemetry spoofed"),
        plt.Line2D([], [], color="#111827", lw=1.2, ls=":", label="death"),
    ]
    ax1.legend(handles=handles, loc="lower left", fontsize=8, ncol=2)
    fig.tight_layout()
    fig.savefig(a.out)
    print(f"wrote {a.out} ({len(rows)} samples)")


if __name__ == "__main__":
    main()
