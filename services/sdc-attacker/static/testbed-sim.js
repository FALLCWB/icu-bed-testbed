// testbed-sim.js — shared coordinator for the ICU bed testbed views.
//
// Two modes, chosen automatically:
//   LIVE  — a real patient-sim backend is reachable on :8000. Frames come from
//           the WebSocket channels (/ws/monitor, /ws/truth); commands are real
//           unauthenticated HTTP POSTs (the actual vulnerability surface).
//   SIM   — no backend. A faithful client-side port of the physiology model
//           runs, shared across browser tabs via BroadcastChannel + localStorage
//           leader election, so opening monitor + room + attacker together
//           reproduces the whole attack standalone.
//
// Physiology ported from services/patient-sim/models/{patient,ventilator}.py.

const HOST = location.hostname || "localhost";
const BASE = "http://" + HOST + ":8000";
const WSBASE = "ws://" + HOST + ":8000";

const AMV_REF = 4.2;
const DEAD_SPACE = 150.0;
const LETHAL_S = 90.0;

const clamp = (x, lo, hi) => Math.max(lo, Math.min(hi, x));

export function baselineVitals() { return { spo2: 98, hr: 78, map: 88, etco2: 38, rr: 14 }; }
export function baselineVent() {
  return { mode: "VC", tidal_volume: 450, resp_rate: 14, peep: 5, fio2: 0.40, connected: true, powered: true };
}
function amv(v) {
  if (!v.connected || !v.powered) return 0;
  return Math.max(0, v.tidal_volume - DEAD_SPACE) * v.resp_rate / 1000.0;
}

const LIMITS = { spo2_low: 90, hr_low: 50, hr_high: 130, map_low: 60, etco2_low: 25, etco2_high: 60, apnea_rr: 4 };

export function alarmsFor(v) {
  const out = [];
  if (v.rr <= LIMITS.apnea_rr) out.push({ label: "APNEA", priority: "high" });
  if (v.spo2 < LIMITS.spo2_low) out.push({ label: "SpO2 LOW", priority: "high" });
  if (v.hr < LIMITS.hr_low) out.push({ label: "HR LOW", priority: "high" });
  else if (v.hr > LIMITS.hr_high) out.push({ label: "HR HIGH", priority: "medium" });
  if (v.map < LIMITS.map_low) out.push({ label: "MAP LOW", priority: "high" });
  if (v.rr > LIMITS.apnea_rr && v.etco2 < LIMITS.etco2_low) out.push({ label: "EtCO2 LOW", priority: "medium" });
  else if (v.etco2 > LIMITS.etco2_high) out.push({ label: "EtCO2 HIGH", priority: "medium" });
  return out;
}

function targets(p, vent) {
  const a = amv(vent);
  const effective = (a / AMV_REF) * (vent.fio2 / 0.40);
  let spo2_t = clamp(20 + 80 * Math.tanh(1.8 * effective), 5, 100);
  const rr_meas = a > 0 ? vent.resp_rate : 0;
  let hr_t = p.spo2 >= 50 ? 78 + 55 * Math.tanh(2.0 * (95 - p.spo2) / 30) : Math.max(0, p.spo2 * 0.8);
  let map_t = 88 * clamp(p.spo2 / 90, 0, 1.05) * clamp(p.hr / 70, 0, 1.10);
  let etco2_t;
  if (rr_meas <= 0) etco2_t = 0;
  else {
    etco2_t = clamp(38 * (AMV_REF / Math.max(a, 0.3)), 0, 90) * clamp(p.map / 70, 0, 1.1);
  }
  return { spo2_t, hr_t, map_t, etco2_t, rr_meas };
}

function freshState() {
  return { t: 0, patient: { true: baselineVitals(), expired: false, hypoxia: 0 }, vent: baselineVent(), spoof: { active: false, vitals: baselineVitals() } };
}

function stepState(s, dt) {
  s.t += dt;
  const p = s.patient;
  if (p.expired) { p.true = { spo2: 0, hr: 0, map: 0, etco2: 0, rr: 0 }; return; }
  const T = targets(p.true, s.vent);
  p.true.spo2 += (T.spo2_t - p.true.spo2) * Math.min(1, dt / 20);
  p.true.hr += (T.hr_t - p.true.hr) * Math.min(1, dt / 8);
  p.true.map += (T.map_t - p.true.map) * Math.min(1, dt / 10);
  p.true.etco2 += (T.etco2_t - p.true.etco2) * Math.min(1, dt / 6);
  p.true.rr = T.rr_meas;
  if (p.true.spo2 < 50 || p.true.hr < 35) p.hypoxia += dt;
  else p.hypoxia = Math.max(0, p.hypoxia - dt * 0.5);
  if (p.hypoxia >= LETHAL_S) p.expired = true;
}

function statusOf(p) {
  if (p.expired) return "expired";
  if (p.true.spo2 < 50 || p.true.hr < 35) return "critical";
  if (p.true.spo2 < 90 || p.true.map < 60) return "deteriorating";
  return "stable";
}

const round1 = (v) => Math.round(v * 10) / 10;
function vitalsDict(v) { const o = {}; for (const k in v) o[k] = round1(v[k]); return o; }

function displayedVitals(s) { return s.spoof.active ? s.spoof.vitals : s.patient.true; }
function monitorFrame(s) {
  const v = displayedVitals(s);
  return { channel: "monitor", t: round1(s.t), vitals: vitalsDict(v), alarms: alarmsFor(v) };
}
function truthFrame(s) {
  const v = s.patient.true;
  return {
    channel: "truth", t: round1(s.t), vitals: vitalsDict(v), alarms: alarmsFor(v),
    status: statusOf(s.patient), expired: s.patient.expired, spoof_active: s.spoof.active,
    ventilator: { ...s.vent },
  };
}

function applyCommand(s, path, body) {
  body = body || {};
  if (path === "/api/ventilator") {
    for (const k of ["mode", "tidal_volume", "resp_rate", "peep", "fio2", "connected", "powered"])
      if (body[k] !== undefined && body[k] !== null) s.vent[k] = body[k];
    return { ...s.vent };
  }
  if (path === "/api/monitor/spoof") {
    s.spoof.active = true;
    s.spoof.vitals = {
      spo2: body.spo2 ?? 98, hr: body.hr ?? 78, map: body.map ?? 88,
      etco2: body.etco2 ?? 38, rr: body.rr ?? 14,
    };
    return { spoof_active: true, vitals: vitalsDict(s.spoof.vitals) };
  }
  if (path === "/api/monitor/clear") { s.spoof.active = false; return { spoof_active: false }; }
  if (path === "/api/reset") { Object.assign(s, freshState()); return { ok: true }; }
  return { ok: false };
}

// ---------------------------------------------------------------------------
export class Testbed {
  constructor() {
    this.live = false;
    this.monitorCbs = [];
    this.truthCbs = [];
    this.modeCbs = [];
    this.me = Math.random().toString(36).slice(2);
    this._wsMon = null;
    this._wsTruth = null;
    try { this.bc = new BroadcastChannel("icu-testbed"); } catch (_) { this.bc = null; }
    if (this.bc) this.bc.onmessage = (e) => this._onBus(e.data);
    window.addEventListener("storage", (e) => {
      if (e.key === "icu_frames" && e.newValue) { try { this._deliver(JSON.parse(e.newValue)); } catch (_) {} }
    });
    this._state = freshState();
    this._startSim();
    this._probeLive();
  }

  onMonitor(cb) { this.monitorCbs.push(cb); if (this.live) this._openMon(); return this; }
  onTruth(cb) { this.truthCbs.push(cb); if (this.live) this._openTruth(); return this; }
  onMode(cb) { this.modeCbs.push(cb); cb(this.live ? "live" : "sim"); return this; }

  _emitMode() { const m = this.live ? "live" : "sim"; this.modeCbs.forEach((c) => c(m)); }
  _deliver(frames) {
    if (frames.monitor) this.monitorCbs.forEach((c) => c(frames.monitor));
    if (frames.truth) this.truthCbs.forEach((c) => c(frames.truth));
  }

  // ---- live backend -------------------------------------------------------
  async _probeLive() {
    try {
      const ctrl = new AbortController();
      const to = setTimeout(() => ctrl.abort(), 1200);
      const r = await fetch(BASE + "/healthz", { signal: ctrl.signal });
      clearTimeout(to);
      if (r.ok) { this.live = true; this._emitMode(); this._openMon(); this._openTruth(); }
    } catch (_) { /* stay in sim */ }
  }
  _openMon() {
    if (!this.live || this._wsMon || !this.monitorCbs.length) return;
    const ws = new WebSocket(WSBASE + "/ws/monitor"); this._wsMon = ws;
    ws.onmessage = (e) => { try { this.monitorCbs.forEach((c) => c(JSON.parse(e.data))); } catch (_) {} };
    ws.onclose = () => { this._wsMon = null; setTimeout(() => this._openMon(), 1500); };
  }
  _openTruth() {
    if (!this.live || this._wsTruth || !this.truthCbs.length) return;
    const ws = new WebSocket(WSBASE + "/ws/truth"); this._wsTruth = ws;
    ws.onmessage = (e) => { try { this.truthCbs.forEach((c) => c(JSON.parse(e.data))); } catch (_) {} };
    ws.onclose = () => { this._wsTruth = null; setTimeout(() => this._openTruth(), 1500); };
  }

  // ---- command surface ----------------------------------------------------
  async command(path, body) {
    if (this.live) {
      try {
        const r = await fetch(BASE + path, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: body ? JSON.stringify(body) : undefined,
        });
        return { ok: true, status: r.status, json: await r.json() };
      } catch (e) { return { ok: false, status: 0, json: { error: String(e) } }; }
    }
    // SIM: leader applies; others broadcast to the leader.
    let json;
    if (this._amLeader()) json = applyCommand(this._state, path, body);
    else { json = applyCommand(freshState(), path, body); this._bus({ type: "cmd", path, body }); }
    return { ok: true, status: 200, json };
  }
  silentKill() { return this.command("/api/monitor/spoof", { spo2: 98, hr: 76, map: 88, etco2: 38, rr: 14 }).then(() => this.command("/api/ventilator", { connected: false })); }

  // ---- standalone simulation (leader-elected across tabs) ------------------
  _bus(msg) { if (this.bc) this.bc.postMessage(msg); }
  _onBus(msg) {
    if (!msg) return;
    if (msg.type === "frames" && !this.live) this._deliver(msg.frames);
    else if (msg.type === "cmd" && this._amLeader()) applyCommand(this._state, msg.path, msg.body);
  }
  _leaderRec() { try { return JSON.parse(localStorage.getItem("icu_leader") || "null"); } catch (_) { return null; } }
  _amLeader() { const l = this._leaderRec(); return !!l && l.id === this.me; }
  _claim() {
    if (this.live) return;
    const l = this._leaderRec(), now = Date.now();
    if (!l || now - l.ts > 700) localStorage.setItem("icu_leader", JSON.stringify({ id: this.me, ts: now }));
  }
  _startSim() {
    // seed shared state from storage so a new leader continues smoothly
    try { const saved = JSON.parse(localStorage.getItem("icu_state") || "null"); if (saved) this._state = saved; } catch (_) {}
    setInterval(() => {
      if (this.live) return;
      this._claim();
      if (!this._amLeader()) return;
      localStorage.setItem("icu_leader", JSON.stringify({ id: this.me, ts: Date.now() }));
      stepState(this._state, 0.5);
      try { localStorage.setItem("icu_state", JSON.stringify(this._state)); } catch (_) {}
      const frames = { monitor: monitorFrame(this._state), truth: truthFrame(this._state) };
      try { localStorage.setItem("icu_frames", JSON.stringify(frames)); } catch (_) {}
      this._bus({ type: "frames", frames });
      this._deliver(frames); // leader renders its own frames too
    }, 250);
  }
}
