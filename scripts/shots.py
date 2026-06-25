"""Capture screenshots of the three browser views.

Console at baseline; the bedside monitor and the room/ground-truth view during a
live silent-kill, so the monitor shows the spoofed normal vitals while the room's
ground-truth panel shows the real deterioration. Requires the stack running and
`playwright install chromium`.
"""

import json
import time
import urllib.request
from playwright.sync_api import sync_playwright

BASE = "http://localhost:8000"
OUT = "figures"


def post(path, body=None):
    data = json.dumps(body or {}).encode() if body is not None else b"{}"
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=8).read()


def truth():
    return json.load(urllib.request.urlopen(BASE + "/api/state", timeout=5))["truth"]


with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1320, "height": 860}, device_scale_factor=2)

    post("/api/reset"); time.sleep(6)

    # 1) attacker console at baseline
    pg = ctx.new_page(); pg.goto("http://localhost:8090/"); time.sleep(5)
    pg.screenshot(path=f"{OUT}/ui_attacker.png"); pg.close()

    # launch the full attack (rogue monitor + rogue ventilator + disconnect)
    urllib.request.urlopen(urllib.request.Request("http://localhost:8090/silent-kill", method="POST"), timeout=8).read()
    for _ in range(80):
        t = truth()
        if t["vitals"]["spo2"] < 60 or t["expired"]:
            break
        time.sleep(2)

    # 2) bedside monitor (rendering the spoofed normal vitals)
    pg = ctx.new_page(); pg.goto("http://localhost:8083/"); time.sleep(6)
    pg.screenshot(path=f"{OUT}/ui_monitor.png"); pg.close()

    # 3) room / ground truth (reveal the truth panel)
    pg = ctx.new_page(); pg.goto("http://localhost:8082/"); time.sleep(4)
    for label in ("SHOW GROUND TRUTH", "GROUND TRUTH"):
        try:
            pg.get_by_text(label, exact=False).first.click(timeout=2500)
            break
        except Exception:
            pass
    time.sleep(2)
    pg.screenshot(path=f"{OUT}/ui_room.png"); pg.close()

    browser.close()

post("/api/reset")
print("shots done")
