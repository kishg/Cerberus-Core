// Cerberus UI. No framework, no build step. Every value from the server is
// inserted with textContent (event summaries are data, not markup).
"use strict";

const SVGNS = "http://www.w3.org/2000/svg";
const NOW_POLL_MS = 5000;
const HISTORY_POLL_MS = 60000;
const state = { view: null, range: "24h", tz: undefined, nowTimer: null, histTimer: null, lastHistory: null, noteDrafts: {} };

// ---------------------------------------------------------------- helpers

function el(tag, attrs = {}, text) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  if (text !== undefined) n.textContent = text;
  return n;
}
function svg(tag, attrs = {}) {
  const n = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    // CSS variables go in style, not presentation attributes: var() in an
    // SVG attribute is not portable (Safari), and in style it also follows
    // a theme change live.
    if ((k === "fill" || k === "stroke") && String(v).startsWith("var(")) n.style.setProperty(k, v);
    else n.setAttribute(k, v);
  }
  return n;
}
function fmtNum(v, d = 0) {
  return v === null || v === undefined ? "–" : Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
}
function fmtTime(ts, opts) {
  const o = Object.assign({ timeZone: state.tz }, opts);
  try { return new Intl.DateTimeFormat(undefined, o).format(new Date(ts * 1000)); }
  catch (_) { delete o.timeZone; return new Intl.DateTimeFormat(undefined, o).format(new Date(ts * 1000)); }
}
const hhmm = (ts) => fmtTime(ts, { hour: "numeric", minute: "2-digit" });
const dayTime = (ts) => fmtTime(ts, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
const dayOnly = (ts) => fmtTime(ts, { month: "short", day: "numeric" });

function ago(ts, now) {
  const s = Math.max(0, now - ts);
  if (s < 90) return `${s} s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 172800) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}

// Status icons: shape carries the meaning, so color is never alone.
function statusIcon(kind) {
  const s = svg("svg", { width: 14, height: 14, viewBox: "0 0 14 14", "aria-hidden": "true" });
  if (kind === "good") {
    s.append(svg("circle", { cx: 7, cy: 7, r: 6.5, fill: "var(--good)" }),
             svg("path", { d: "M4 7.2l2 2 4-4.2", fill: "none", stroke: "#fff", "stroke-width": 1.6, "stroke-linecap": "round", "stroke-linejoin": "round" }));
  } else if (kind === "warning") {
    s.append(svg("path", { d: "M7 1l6.2 11.5H.8z", fill: "var(--warning)" }),
             svg("path", { d: "M7 5v3.6M7 10.4v.1", stroke: "#0b0b0b", "stroke-width": 1.6, "stroke-linecap": "round" }));
  } else if (kind === "critical") {
    s.append(svg("circle", { cx: 7, cy: 7, r: 6.5, fill: "var(--critical)" }),
             svg("path", { d: "M4.6 4.6l4.8 4.8M9.4 4.6l-4.8 4.8", stroke: "#fff", "stroke-width": 1.6, "stroke-linecap": "round" }));
  } else if (kind === "info") {
    s.append(svg("circle", { cx: 7, cy: 7, r: 6.5, fill: "var(--text-2)" }),
             svg("path", { d: "M7 6.2v3.8M7 4v.1", stroke: "var(--surface)", "stroke-width": 1.6, "stroke-linecap": "round" }));
  } else {
    s.append(svg("circle", { cx: 7, cy: 7, r: 4, fill: "var(--muted)" }));
  }
  return s;
}
function statusLine(node, kind, text) {
  node.replaceChildren(statusIcon(kind), el("span", {}, text));
}

// The API's base, from <meta name="cerberus-api">: same-origin "/api/v1" by
// default, another origin when wrapped as a native app or behind the relay.
const API = (document.querySelector('meta[name="cerberus-api"]')?.content || "/api/v1").replace(/\/$/, "");
const TOKEN_KEY = "cerberus.token";
// localStorage, not sessionStorage: a phone stays signed in across closed
// tabs and home-screen relaunches (the session itself lasts 30 days from
// last use, server side; signing out or a new PIN ends it).
function token() { try { return localStorage.getItem(TOKEN_KEY); } catch (_) { return null; } }
function setToken(t) {
  try { if (t) localStorage.setItem(TOKEN_KEY, t); else localStorage.removeItem(TOKEN_KEY); } catch (_) {}
}

async function getJSON(path) {
  const t = token();
  const r = await fetch(API + path, { cache: "no-store", headers: t ? { Authorization: `Bearer ${t}` } : {} });
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return r.json();
}
async function postJSON(path, body) {
  const t = token();
  const r = await fetch(API + path, {
    method: "POST", cache: "no-store", body: JSON.stringify(body),
    headers: Object.assign({ "Content-Type": "application/json" }, t ? { Authorization: `Bearer ${t}` } : {}),
  });
  let data = {};
  try { data = await r.json(); } catch (_) {}
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

// ---------------------------------------------------------------- Now

function tile(label, value, detail) {
  const t = el("div", { class: "card tile" });
  t.append(el("div", { class: "label" }, label), el("div", { class: "value" }, value));
  if (detail) t.append(el("div", { class: "detail" }, detail));
  return t;
}

function renderNow(d) {
  state.tz = d.tz || undefined;
  const snap = d.snapshot || {};
  const bank = snap.bank, solar = snap.solar, ac = snap.ac;
  const status = document.getElementById("status");
  if (!d.collector_up) statusLine(status, "critical", snap.ts ? `Collector stopped, last seen ${ago(snap.ts, d.now)}` : "Collector not running");
  else if (!d.cerbo_online) statusLine(status, "warning", "Cerbo offline");
  else if (!d.live) statusLine(status, "warning", bank ? `Battery data ${ago(bank.ts, d.now)}` : "No battery data yet");
  else statusLine(status, "good", `Live · ${hhmm(d.now)}`);

  document.getElementById("soc").textContent = bank ? `${fmtNum(bank.soc, 0)}%` : "–";
  const tr = d.soc_trust;
  statusLine(document.getElementById("trust"), tr.trusted ? "good" : "warning", tr.note);

  const tiles = document.getElementById("tiles");
  tiles.replaceChildren();
  if (bank) {
    const i = bank.current;
    const dir = i === null ? "" : i > 0.5 ? "Charging" : i < -0.5 ? "Discharging" : "Idle";
    tiles.append(tile("Net current", `${i > 0 ? "+" : ""}${fmtNum(i, 1)} A`, `${dir} · ${fmtNum(bank.power, 0)} W`));
    tiles.append(tile("Bank voltage", `${fmtNum(bank.voltage, 2)} V`, bank.source === "monitor" ? "Battery monitor" : `${bank.packs.length} packs`));
    const tte = d.time_to_empty_h;
    tiles.append(tile("Time to empty", tte === null ? "–" : `${fmtNum(tte, tte < 10 ? 1 : 0)} h`,
      tte !== null ? "At the current draw" : dir === "Charging" ? "Charging" : dir === "Idle" ? "Not discharging" : "Capacity unknown"));
  }
  if (solar) tiles.append(tile("Solar", `${fmtNum(solar.pv_w, 0)} W`, `${solar.state || "–"} · today ${fmtNum(solar.yield_today_kwh * 1000, 0)} Wh`));
  if (ac) tiles.append(tile("AC load", `${fmtNum(ac.consumption_w, 0)} W`,
    `${ac.shore_connected ? "Shore power" : "Inverter"}${ac.inverter_state ? " · " + ac.inverter_state : ""}`));
  if (d.cabin) tiles.append(tile("Cabin", `${fmtNum(d.cabin.air_temp_f, 0)}°F`, `${fmtNum(d.cabin.humidity_pct, 0)}% humidity · ${ago(d.cabin.ts, d.now)}`));
  if (d.outside) tiles.append(tile("Outside", `${fmtNum(d.outside.air_temp_f, 0)}°F`, `${fmtNum(d.outside.humidity_pct, 0)}% humidity · forecast`));

  const tbody = document.querySelector("#packs tbody");
  tbody.replaceChildren();
  for (const p of (bank ? bank.packs : [])) {
    const tr = el("tr");
    tr.append(el("td", {}, bank.source === "monitor" ? "Bank" : `Pack ${p.id}`),
      el("td", {}, `${fmtNum(p.soc, 0)}%`), el("td", {}, `${fmtNum(p.voltage, 2)} V`),
      el("td", {}, `${fmtNum(p.current, 1)} A`), el("td", {}, `${fmtNum(p.temperature_c, 0)}°C`));
    tbody.append(tr);
  }

  const list = document.getElementById("open-findings");
  list.replaceChildren();
  if (!d.findings.length) {
    const li = el("li");
    li.append(statusIcon("good"), el("div", { class: "what" }, "Nothing needs attention."));
    list.append(li);
  }
  if (d.known_count) {
    const li = el("li"), what = el("div", { class: "what" });
    what.append(el("a", { href: "#findings" }, `${d.known_count} marked known`));
    li.append(statusIcon("info"), what);
    list.append(li);
  }
  for (const f of d.findings) {
    const li = el("li"), what = el("div", { class: "what" }), a = el("a", { href: `#findings` }, f.title);
    what.append(a);
    li.append(statusIcon(SEV[f.severity].icon), what,
      el("div", { class: "when" }, f.first_seen ? `since ${dayOnly(f.first_seen)}` : ""));
    list.append(li);
  }
}

// ---------------------------------------------------------------- Chassis

const CHECK_LABEL = { coolant_low: "Coolant level", brake_fluid_low: "Brake fluid",
  washer_low: "Washer fluid", engine_light: "Engine light" };
// Lock status code (Mercedes doorlockstatusvehicle).
const LOCK_LABEL = { 0: "Unlocked", 1: "Locked inside", 2: "Locked", 3: "Partly unlocked" };

function renderChassis(d) {
  state.tz = d.tz || undefined;
  const v = d.vehicle || {}, tiles = document.getElementById("chassis-tiles");
  tiles.replaceChildren();
  const eng = v.engine;
  const asOf = (x) => (eng && eng.state !== "off" ? "Live" : `As of ${ago(x.ts, d.now)}`);
  if (eng) {
    const label = { off: "Off", idling: "Idling", driving: "Driving" }[eng.state] || "–";
    tiles.append(tile("Engine", label, eng.state === "off" ? `Last ran ${ago(eng.ts, d.now)}`
      : eng.state === "driving" ? `${fmtNum(eng.speed_mph, 0)} mph · ${fmtNum(eng.rpm, 0)} rpm` : `${fmtNum(eng.rpm, 0)} rpm`));
  }
  if (v.chassis || d.starter_battery) {
    const sb = d.starter_battery;
    tiles.append(tile("Chassis battery", v.chassis ? `${fmtNum(v.chassis.voltage, 2)} V` : "–",
      [v.chassis ? (v.chassis.linked ? "Joined to the house bank" : "Separate from the house bank") : null,
       sb ? `Van's check: ${sb.label}` : null].filter(Boolean).join(" · ")));
  }
  if (v.fuel) tiles.append(tile("Fuel", `${fmtNum(v.fuel.pct, 0)}%`,
    asOf(v.fuel) + (v.fuel.range_mi != null ? ` · ${fmtNum(v.fuel.range_mi, 0)} mi range` : "")));
  if (v.def) tiles.append(tile("DEF (AdBlue)", `${fmtNum(v.def.pct, 0)}%`, asOf(v.def)));
  if (v.odometer) tiles.append(tile("Odometer", `${fmtNum(v.odometer.mi, 0)} mi`, asOf(v.odometer)));
  if (d.lock_state) tiles.append(tile("Doors", LOCK_LABEL[d.lock_state.value] || "–", `As of ${ago(d.lock_state.ts, d.now)}`));

  const checks = document.getElementById("chassis-checks");
  checks.replaceChildren();
  for (const f of d.findings) {
    const li = el("li"), what = el("div", { class: "what" });
    what.append(el("a", { href: "#findings" }, f.title));
    li.append(statusIcon(f.status === "resolved" ? "good" : SEV[f.severity].icon), what,
      el("div", { class: "when" }, f.first_seen ? `since ${dayOnly(f.first_seen)}` : ""));
    checks.append(li);
  }
  for (const [k, w] of Object.entries(d.warnings || {})) {
    const li = el("li");
    li.append(statusIcon(w.on ? "warning" : "good"),
      el("div", { class: "what" }, `${CHECK_LABEL[k] || k}: ${w.on ? "warning" : "OK"}`),
      el("div", { class: "when" }, ago(w.ts, d.now)));
    checks.append(li);
  }
  document.getElementById("chassis-checks-card").hidden = !checks.children.length;

  const ty = d.tyres, tyreBox = document.getElementById("chassis-tyres");
  tyreBox.replaceChildren();
  document.getElementById("chassis-tyres-card").hidden = !ty;
  if (ty) {
    tyreBox.append(tyreDiagram(ty));
    const newest = Math.max(...["fl", "fr", "rl", "rr"].map((p) => (ty[p] ? ty[p].ts : 0)));
    document.getElementById("chassis-tyres-sub").textContent =
      `${ty.warning ? "Tyre-pressure warning on. " : ""}Last reported ${ago(newest, d.now)} (the van sends pressures after driving).`;
  }

  const db = document.querySelector("#chassis-drives tbody");
  db.replaceChildren();
  for (const r of d.drives) {
    const tr = el("tr");
    tr.append(el("td", {}, `${dayOnly(r.start_ts)} ${hhmm(r.start_ts)}`),
      el("td", {}, `${fmtNum((r.end_ts - r.start_ts) / 60, 0)} min`),
      el("td", {}, r.miles == null ? "–" : fmtNum(r.miles, 0)),
      el("td", {}, r.alt_ah == null ? "–" : `${fmtNum(r.alt_ah, 0)} Ah`),
      el("td", {}, r.peak_bank_v == null ? "–" : `${fmtNum(r.peak_bank_v, 2)} V`),
      el("td", {}, r.coolant_max_c == null ? "–" : `${fmtNum(r.coolant_max_c, 0)}°C`));
    db.append(tr);
  }
  document.getElementById("chassis-empty").hidden = !!(tiles.children.length || d.drives.length || ty);
}

// The van from above, front at the top, each tyre's pressure beside it.
const TYRE_SPOTS = [
  { pos: "fl", name: "Front left", y: 62, side: -1 },
  { pos: "fr", name: "Front right", y: 62, side: 1 },
  { pos: "rl", name: "Rear left", y: 204, side: -1 },
  { pos: "rr", name: "Rear right", y: 204, side: 1 },
];

function tyreDiagram(ty) {
  const psi = (t) => (t ? `${fmtNum(t.value, 0)} psi` : "–");
  const s = svg("svg", {
    class: "tyre-diagram", viewBox: "0 0 300 300", role: "img",
    "aria-label": "Tyre pressures from above: " + TYRE_SPOTS.map(({ pos, name }) =>
      `${name.toLowerCase()} ${psi(ty[pos])}${ty[pos] && ty[pos].low_from != null ? " (low)" : ""}`).join(", "),
  });
  const label = (x, y, anchor, cls, fill, text) => {
    const t = svg("text", { x, y, "text-anchor": anchor, class: cls, fill });
    t.textContent = text;
    return t;
  };
  s.append(label(150, 14, "middle", "tyre-note", "var(--text-2)", "Front"),
    svg("rect", { x: 105, y: 22, width: 90, height: 266, rx: 24, fill: "var(--grid)", stroke: "var(--axis)", "stroke-width": 1.5 }),
    svg("path", { d: "M117 86q33-14 66 0l-6 24q-27-9-54 0z", fill: "var(--axis)" }));
  for (const { pos, name, y, side } of TYRE_SPOTS) {
    const t = ty[pos], low = !!t && t.low_from != null;
    const x = side < 0 ? 85 : 215, anchor = side < 0 ? "end" : "start";
    s.append(svg("rect", { x: side < 0 ? 93 : 195, y, width: 12, height: 44, rx: 3, fill: low ? "var(--warning)" : "var(--text-2)" }),
      label(x, y + 18, anchor, "tyre-psi", "var(--text)", psi(t)),
      label(x, y + 35, anchor, "tyre-note", "var(--text-2)", name));
    if (low) s.append(label(x, y + 51, anchor, "tyre-note", "var(--text)", `Low, was ${fmtNum(t.low_from, 0)}`));
  }
  return s;
}

async function loadChassis() {
  try { renderChassis(await getJSON("/chassis")); } catch (err) { console.error(err); }
}

// ---------------------------------------------------------------- Findings

const SEV = {
  urgent: { icon: "critical", label: "Urgent" },
  attention: { icon: "warning", label: "Needs attention" },
  info: { icon: "info", label: "For information" },
};

function findingCard(f) {
  const resolved = f.status === "resolved", known = !resolved && !!f.known;
  const card = el("section", { class: `card finding${resolved ? " resolved" : ""}${known ? " known" : ""}` });
  const sev = el("div", { class: "sev" });
  sev.append(statusIcon(resolved ? "good" : known ? "info" : SEV[f.severity].icon),
    el("span", {}, resolved ? "Resolved" : known ? `Known · ${SEV[f.severity].label.toLowerCase()}` : SEV[f.severity].label));
  if (f.first_seen) {
    const a = dayOnly(f.first_seen), b = f.last_seen ? dayOnly(f.last_seen) : a;
    sev.append(el("span", { class: "since" }, resolved ? (a === b ? a : `${a} – ${b}`) : `since ${a}`));
  }
  const todo = el("p", { class: "todo" });
  todo.append(el("b", {}, "What to do: "), document.createTextNode(f.action));
  card.append(sev, el("h2", {}, f.title), el("p", {}, f.explain), todo);
  const ev = f.evidence;
  if (ev && ev.kind === "series" && ev.points.length) {
    const key = el("div", { class: "legend" });
    const series = el("span"), si = el("i"), lim = el("span"), li = el("i");
    si.style.background = "var(--series-1)";
    series.append(si, document.createTextNode(ev.label));
    key.append(series);
    if (ev.limit !== null && ev.limit !== undefined) {
      li.style.background = "var(--text-2)"; li.style.height = "1px";
      lim.append(li, document.createTextNode(`${ev.limit_label || "Limit"} ${fmtNum(ev.limit, 0)}${ev.unit}`));
      key.append(lim);
    }
    const box = el("div", { class: "chart", tabindex: "0" });
    card.append(key, box);
    // Laid out after insertion, so the chart can measure its width.
    const marks = (f.notes || []).map((n) => n.ts);
    const last = ev.points[ev.points.length - 1][0];
    requestAnimationFrame(() => lineChart(box, [{ name: ev.label, color: "--series-1", pts: ev.points }],
      { t0: ev.points[0][0], now: Math.max(last, ...marks), bucket: 86400,
        unit: ev.unit, decimals: 0, limit: ev.limit, label: ev.label, marks }));
  } else if (ev && ev.kind === "events" && ev.items.length) {
    const ul = el("ul", { class: "events" });
    for (const [t, text] of ev.items.slice().reverse()) {
      const li = el("li");
      li.append(el("div", { class: "what" }, text), el("div", { class: "when" }, dayTime(t)));
      ul.append(li);
    }
    card.append(ul);
  }
  card.append(notesBlock(f));
  return card;
}

// The owner's notes on a finding: what they know that the data can't show
// ("tightened the lugs"). The server uses the latest to say what changed since.
function noteItem(n, onDelete) {
  const li = el("li"), what = el("div", { class: "what" }, n.text);
  if (n.known) what.append(el("span", { class: "src known-tag" }, "Marked known"));
  li.append(what, el("div", { class: "when" }, dayTime(n.ts)));
  if (onDelete) {
    const tog = el("button", { type: "button", class: "link-btn" }, n.known ? "Flag again" : "Mark known");
    tog.addEventListener("click", async () => {
      tog.disabled = true;
      try { await postJSON("/notes/known", { id: n.id, known: !n.known }); loadFindings(); }
      catch (err) { tog.disabled = false; tog.textContent = err.message; }
    });
    li.append(tog);
    const del = el("button", { type: "button", class: "link-btn", "aria-label": `Delete note from ${dayTime(n.ts)}` }, "Delete");
    del.addEventListener("click", async () => {
      del.disabled = true;
      try { await postJSON("/notes/delete", { id: n.id }); loadFindings(); }
      catch (err) { del.disabled = false; del.textContent = err.message; }
    });
    li.append(del);
  }
  return li;
}

function notesBlock(f) {
  const box = el("div", { class: "notes" });
  const signed = !!token();
  box.append(el("h3", {}, "Your notes"));
  if (f.notes && f.notes.length) {
    const ul = el("ul", { class: "events" });
    f.notes.slice().reverse().forEach((n) => ul.append(noteItem(n, signed)));
    box.append(ul);
  }
  if (!signed) {
    const p = el("p", { class: "help" });
    p.append(el("a", { href: "#settings" }, "Sign in"), document.createTextNode(
      " to add a note: what you checked or changed, so Cerberus can show whether it helped."));
    box.append(p);
    return box;
  }
  const ta = el("textarea", { rows: "2", maxlength: "1000", class: "note-text", "data-key": f.key,
    "aria-label": `Note on: ${f.title}`, placeholder: "e.g. Tightened the charge controller's battery lugs" });
  ta.value = state.noteDrafts[f.key] || "";
  ta.addEventListener("input", () => { state.noteDrafts[f.key] = ta.value; });
  const btn = el("button", { type: "button", class: "btn quiet" }, "Add note");
  const msg = el("div", { class: "msg", role: "status" });
  const knownBox = el("input", { type: "checkbox" });
  const knownLbl = el("label", { class: "known-opt" });
  knownLbl.append(knownBox, document.createTextNode(
    " Known: stop flagging this until it happens again"));
  btn.addEventListener("click", async () => {
    if (!ta.value.trim()) return;
    btn.disabled = true; msg.className = "msg"; msg.textContent = "";
    try {
      await postJSON("/notes", { key: f.key, title: f.title, text: ta.value, known: knownBox.checked });
      delete state.noteDrafts[f.key];
      loadFindings();
    } catch (err) {
      btn.disabled = false; msg.className = "msg err"; msg.textContent = err.message;
      if (/sign in/i.test(err.message)) setToken(null);
    }
  });
  const row = el("div", { class: "note-row" });
  row.append(ta, btn);
  box.append(row);
  if (f.status === "open") box.append(knownLbl);
  box.append(msg);
  return box;
}

function renderFindings(d) {
  state.tz = d.tz || state.tz;
  const holder = document.getElementById("findings");
  holder.replaceChildren();
  const open = d.findings.filter((f) => f.status === "open" && !f.known);
  const known = d.findings.filter((f) => f.status === "open" && f.known);
  const done = d.findings.filter((f) => f.status === "resolved");
  if (!open.length) {
    const c = el("section", { class: "card" });
    const line = el("div", { class: "trust" });
    statusLine(line, "good", "Nothing needs attention right now.");
    c.append(line);
    holder.append(c);
  }
  open.forEach((f) => holder.append(findingCard(f)));
  if (known.length) {
    holder.append(el("div", { class: "section-label" }, "Known: flagged again if they happen again"));
    known.forEach((f) => holder.append(findingCard(f)));
  }
  if (done.length) {
    holder.append(el("div", { class: "section-label" }, "Recently resolved"));
    done.forEach((f) => holder.append(findingCard(f)));
  }
  // Notes whose finding has gone (resolved more than two weeks ago): kept, not lost.
  const other = d.other_notes || [];
  if (other.length) {
    holder.append(el("div", { class: "section-label" }, "Notes on earlier findings"));
    const c = el("section", { class: "card notes" }), ul = el("ul", { class: "events" });
    for (const n of other) {
      const li = noteItem(n, !!token());
      li.firstChild.prepend(el("b", {}, `${n.title || n.key}: `));
      ul.append(li);
    }
    c.append(ul);
    holder.append(c);
  }
}

async function loadFindings() {
  // Not while a note is being typed: the re-render would take its focus.
  if (document.activeElement && document.activeElement.classList.contains("note-text")) return;
  try { renderFindings(await getJSON("/findings")); }
  catch (err) { console.error(err); }
}

async function pollNow() {
  try { renderNow(await getJSON("/now")); }
  catch (err) {
    // A box not set up yet has no database, so /now fails; say so, not "unreachable".
    let setUp = true;
    try { setUp = (await getJSON("/about")).set_up !== false; } catch (_) {}
    statusLine(document.getElementById("status"), setUp ? "critical" : "info",
      setUp ? "UI cannot reach its server" : "Not set up yet");
    if (setUp) console.error(err);
  }
}

// ---------------------------------------------------------------- charts

function niceTicks(lo, hi, n = 4) {
  if (lo === hi) { lo -= 1; hi += 1; }
  const raw = (hi - lo) / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
  // Outer ticks round outward past the data: the first at or below lo, the
  // last at or above hi, or a 99.6% peak draws above a top gridline of 80.
  const ticks = [], first = Math.floor(lo / step), last = Math.ceil(hi / step - 1e-9);
  for (let k = first; k <= last; k++) ticks.push(+(k * step).toFixed(10));
  return { ticks, lo: ticks[0], hi: ticks[ticks.length - 1], step };
}
// Tick labels carry as many decimals as the tick step needs, so 13.0 / 13.2 /
// 13.4 never collapse to "13 13 13"; compact from 1,000 up.
function tickLabel(v, step) {
  if (Math.abs(v) >= 1000 && step >= 100) return `${+(v / 1000).toFixed(1)}K`;
  const digits = (String(+step.toPrecision(6)).split(".")[1] || "").length;   // 0.25 -> 2, 0.5 -> 1
  return v.toFixed(digits);
}
function xTickLabel(ts, span) {
  return span <= 2 * 86400 ? hhmm(ts) : dayOnly(ts);
}

// X ticks on round van-local times: every 6 h up to two days, else local
// midnights thinned to at most maxTicks. Walks whole hours, so a DST day
// still lands on its real midnight.
let hourFmt = null, hourFmtTz = null;
function localHour(ts) {
  if (!hourFmt || hourFmtTz !== state.tz) {
    try { hourFmt = new Intl.DateTimeFormat("en-US", { hour: "numeric", hourCycle: "h23", timeZone: state.tz }); }
    catch (_) { hourFmt = new Intl.DateTimeFormat("en-US", { hour: "numeric", hourCycle: "h23" }); }
    hourFmtTz = state.tz;
  }
  return Number(hourFmt.format(new Date(ts * 1000))) % 24;
}
function timeTicks(t0, t1, maxTicks) {
  const short = t1 - t0 <= 2 * 86400, out = [];
  for (let t = Math.ceil(t0 / 3600) * 3600; t <= t1; t += 3600) {
    const h = localHour(t);
    if (short ? h % 6 === 0 : h === 0) out.push(t);
  }
  const every = Math.max(1, Math.ceil(out.length / maxTicks));
  return out.filter((_, i) => i % every === 0);
}

function chartCard(title, sub, legend) {
  const card = el("section", { class: "card" });
  const head = el("div", { class: "chart-head" });
  const titles = el("div");
  titles.append(el("h2", {}, title), el("p", { class: "sub" }, sub));
  const toggle = el("button", { class: "table-toggle", type: "button", "aria-expanded": "false" }, "Table");
  head.append(titles, toggle);
  card.append(head);
  if (legend && legend.length > 1) {
    const lg = el("div", { class: "legend" });
    for (const s of legend) { const sp = el("span"); const i = el("i"); i.style.background = `var(${s.color})`; sp.append(i, document.createTextNode(s.name)); lg.append(sp); }
    card.append(lg);
  }
  const box = el("div", { class: "chart", tabindex: "0" });
  const tableWrap = el("div", { class: "table-wrap", hidden: "" });
  card.append(box, tableWrap);
  toggle.addEventListener("click", () => {
    const open = tableWrap.hidden;
    tableWrap.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    toggle.textContent = open ? "Chart only" : "Table";
  });
  return { card, box, tableWrap };
}

function fillTable(wrap, headers, rows) {
  const t = el("table"), th = el("tr");
  for (const h of headers) th.append(el("th", {}, h));
  t.append(el("thead"), el("tbody"));
  t.tHead.append(th);
  for (const r of rows.slice().reverse()) {
    const tr = el("tr");
    for (const c of r) tr.append(el("td", {}, c));
    t.tBodies[0].append(tr);
  }
  wrap.replaceChildren(t);
}

// series: [{name, color (css var), pts: [[t, v], ...]}]
function lineChart(box, series, o) {
  const W = box.clientWidth || 320, H = box.clientHeight || 180;
  const m = { l: 40, r: 52, t: 16, b: 22 };
  const all = series.flatMap((s) => s.pts.filter((p) => p[1] !== null));
  box.replaceChildren();
  if (!all.length) { box.append(el("p", { class: "empty" }, "No data in this range.")); return; }
  // One window for every chart (set by the range filter), so they line up.
  const t0 = o.t0, t1 = o.now;
  let lo = Math.min(...all.map((p) => p[1])), hi = Math.max(...all.map((p) => p[1]));
  if (o.zero) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
  if (o.limit !== undefined && o.limit !== null) { lo = Math.min(lo, o.limit); hi = Math.max(hi, o.limit); }
  const y = niceTicks(lo, hi);
  const X = (t) => m.l + (t - t0) / Math.max(1, t1 - t0) * (W - m.l - m.r);
  const Y = (v) => m.t + (y.hi - v) / (y.hi - y.lo) * (H - m.t - m.b);
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": o.label });

  for (const v of y.ticks) {
    s.append(svg("line", { class: v === 0 && o.zero ? "baseline" : "grid", x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v) }));
    const tx = svg("text", { x: m.l - 6, y: Y(v) + 4, "text-anchor": "end" }); tx.textContent = tickLabel(v, y.step); s.append(tx);
  }
  for (const t of timeTicks(t0, t1, W < 420 ? 4 : 7)) {
    const x = X(t);
    const tx = svg("text", { x, y: H - 6, "text-anchor": x < m.l + 24 ? "start" : x > W - m.r - 24 ? "end" : "middle" });
    tx.textContent = xTickLabel(t, t1 - t0); s.append(tx);
  }

  // A threshold the finding is judged against: a solid hairline in secondary
  // ink (never dashed, never a status color). Its label lives in the key row
  // above the chart, because data can cross the line anywhere.
  if (o.limit !== undefined && o.limit !== null) {
    s.append(svg("line", { class: "limit", x1: m.l, x2: W - m.r, y1: Y(o.limit), y2: Y(o.limit) }));
  }
  // The owner's notes: a vertical hairline at each, so "before" and "after"
  // a fix they wrote down can be read straight off the chart.
  for (const t of o.marks || []) {
    if (t < t0 || t > t1) continue;
    s.append(svg("line", { class: "note-mark", x1: X(t), x2: X(t), y1: m.t, y2: H - m.b }));
    const tx = svg("text", { class: "note-label", x: X(t) + 3, y: m.t - 4 }); tx.textContent = "Note"; s.append(tx);
  }

  // A line breaks across a real outage, not between samples that are merely
  // sparser than the bucket: weather arrives every 15 min, so against 5-min
  // buckets every point would start its own one-point (invisible) segment.
  // The threshold follows each series' own typical spacing.
  function gapFor(pts) {
    const ts = pts.filter((p) => p[1] !== null).map((p) => p[0]);
    const dts = ts.slice(1).map((t, i) => t - ts[i]).sort((a, b) => a - b);
    const typical = dts.length ? dts[dts.length >> 1] : 0;
    return Math.max(o.bucket, typical) * 2.5;
  }
  const ends = [];
  for (const sr of series) {
    const gap = gapFor(sr.pts);
    let d = "", prev = null;
    for (const [t, v] of sr.pts) {
      if (v === null) { prev = null; continue; }
      d += `${prev === null || t - prev > gap ? "M" : "L"}${X(t).toFixed(1)},${Y(v).toFixed(1)}`;
      prev = t;
    }
    s.append(svg("path", { class: "line", d, stroke: `var(${sr.color})` }));
    const last = sr.pts.filter((p) => p[1] !== null).pop();
    if (last) ends.push({ sr, last });
  }
  // Direct end labels only when they cannot collide; otherwise the legend and
  // tooltip carry identity (never nudge labels off their lines).
  const labelEnds = ends.length < 2 || Math.abs(Y(ends[0].last[1]) - Y(ends[1].last[1])) >= 14;
  for (const { sr, last } of ends) {
    s.append(svg("circle", { cx: X(last[0]), cy: Y(last[1]), r: 4, fill: `var(${sr.color})`, stroke: "var(--surface)", "stroke-width": 2 }));
    if (labelEnds) {
      const tx = svg("text", { class: "end-label", x: X(last[0]) + 8, y: Y(last[1]) + 4 });
      tx.textContent = `${fmtNum(last[1], o.decimals)}${o.unit}`; s.append(tx);
    }
  }

  const cross = svg("line", { class: "crosshair", y1: m.t, y2: H - m.b, visibility: "hidden" });
  s.append(cross);
  box.append(s);
  const tip = el("div", { class: "tip" });
  box.append(tip);

  const times = [...new Set(all.map((p) => p[0]))].sort((a, b) => a - b);
  const lookup = series.map((sr) => new Map(sr.pts.map((p) => [p[0], p[1]])));
  let idx = times.length - 1;
  function show(i) {
    idx = Math.max(0, Math.min(times.length - 1, i));
    const t = times[idx], x = X(t);
    cross.setAttribute("x1", x); cross.setAttribute("x2", x); cross.setAttribute("visibility", "visible");
    tip.replaceChildren(el("div", { class: "t" }, (t1 - t0) <= 2 * 86400 ? dayTime(t) : (o.bucket >= 86400 ? dayOnly(t) : dayTime(t))));
    series.forEach((sr, k) => {
      const v = lookup[k].get(t);
      const row = el("div", { class: "row" }), key = el("span", { class: "key" });
      key.style.background = `var(${sr.color})`;
      row.append(key, el("b", {}, v === undefined || v === null ? "–" : `${fmtNum(v, o.decimals)}${o.unit}`), el("span", {}, sr.name));
      tip.append(row);
    });
    tip.style.display = "block";
    const left = Math.min(Math.max(0, x + 10), box.clientWidth - tip.offsetWidth);
    tip.style.left = `${x + 10 + tip.offsetWidth > box.clientWidth ? x - tip.offsetWidth - 10 : left}px`;
    tip.style.top = "0px";
  }
  function hide() { cross.setAttribute("visibility", "hidden"); tip.style.display = "none"; }
  function nearest(clientX) {
    const r = s.getBoundingClientRect();
    const t = t0 + ((clientX - r.left) * (W / r.width) - m.l) / (W - m.l - m.r) * (t1 - t0);
    let lo2 = 0, hi2 = times.length - 1;
    while (hi2 - lo2 > 1) { const mid = (lo2 + hi2) >> 1; if (times[mid] < t) lo2 = mid; else hi2 = mid; }
    return Math.abs(times[lo2] - t) <= Math.abs(times[hi2] - t) ? lo2 : hi2;
  }
  s.addEventListener("pointermove", (e) => show(nearest(e.clientX)));
  s.addEventListener("pointerleave", hide);
  box.onfocus = () => show(idx);
  box.onblur = hide;
  box.onkeydown = (e) => {
    if (e.key === "ArrowLeft") { show(idx - 1); e.preventDefault(); }
    if (e.key === "ArrowRight") { show(idx + 1); e.preventDefault(); }
  };
}

// rows: [["YYYY-MM-DD", value], ...]; o.unit / o.decimals / o.max format it.
function barChart(box, rows, o) {
  const unit = o.unit ?? " kWh", dec = o.decimals ?? 2;
  const W = box.clientWidth || 320, H = box.clientHeight || 180;
  const m = { l: 40, r: 12, t: 18, b: 22 };
  box.replaceChildren();
  if (!rows.length) { box.append(el("p", { class: "empty" }, "No data in this range.")); return; }
  const y = niceTicks(0, o.max ?? Math.max(...rows.map((r) => r[1])));
  const Y = (v) => m.t + (y.hi - v) / (y.hi - y.lo) * (H - m.t - m.b);
  const band = (W - m.l - m.r) / rows.length;
  const bw = Math.max(2, Math.min(24, band - 2));
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": o.label });
  for (const v of y.ticks) {
    s.append(svg("line", { class: v === 0 ? "baseline" : "grid", x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v) }));
    const tx = svg("text", { x: m.l - 6, y: Y(v) + 4, "text-anchor": "end" }); tx.textContent = tickLabel(v, y.step); s.append(tx);
  }
  const tip = el("div", { class: "tip" });
  const label = (day) => new Date(`${day}T12:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  const every = Math.ceil(rows.length / (W < 420 ? 4 : 7));
  rows.forEach(([day, v], i) => {
    const cx = m.l + band * (i + 0.5), x = cx - bw / 2, top = Y(v), base = Y(0), r = Math.min(4, bw / 2, base - top);
    const d = `M${x},${base}V${top + r}Q${x},${top} ${x + r},${top}H${x + bw - r}Q${x + bw},${top} ${x + bw},${top + r}V${base}Z`;
    const bar = svg("path", { class: "bar", d, fill: "var(--series-1)", tabindex: "0", "aria-label": `${label(day)}: ${fmtNum(v, dec)}${unit}` });
    const show = () => {
      tip.replaceChildren(el("div", { class: "t" }, label(day)));
      const row = el("div", { class: "row" }); row.append(el("b", {}, `${fmtNum(v, dec)}${unit}`)); tip.append(row);
      tip.style.display = "block";
      tip.style.left = `${Math.min(Math.max(0, cx - tip.offsetWidth / 2), box.clientWidth - tip.offsetWidth)}px`;
      tip.style.top = "0px";
    };
    const hide = () => { tip.style.display = "none"; };
    bar.addEventListener("pointerenter", show); bar.addEventListener("pointerleave", hide);
    bar.addEventListener("focus", show); bar.addEventListener("blur", hide);
    s.append(bar);
    if (i % every === 0 || i === rows.length - 1) {
      const tx = svg("text", { x: cx, y: H - 6, "text-anchor": "middle" }); tx.textContent = label(day); s.append(tx);
    }
  });
  const last = rows[rows.length - 1];
  const lt = svg("text", { class: "end-label", x: m.l + band * (rows.length - 0.5), y: Y(last[1]) - 6, "text-anchor": "middle" });
  lt.textContent = `${fmtNum(last[1], o.lastDecimals ?? 1)}${o.lastUnit ?? ""}`; s.append(lt);
  box.append(s, tip);
}

// ---------------------------------------------------------------- Health

const SOURCE_STATUS = {
  fresh: ["good", "Live"], stale: ["warning", "Stale"],
  away: ["muted", "Out of range"], idle: ["muted", "Idle"],
};
const GAP_ICON = { cerbo: "warning", network: "warning", partial: "info", restart: "info", asleep: "info", stopped: "info" };

function hours(s) { return s < 3600 ? `${Math.round(s / 60)} min` : `${fmtNum(s / 3600, s < 36000 ? 1 : 0)} h`; }

function renderHealth(d) {
  const tbody = document.querySelector("#sources tbody");
  tbody.replaceChildren();
  for (const s of d.sources) {
    const tr = el("tr"), name = el("td");
    name.append(document.createTextNode(s.label));
    if (s.note) name.append(el("span", { class: "note" }, s.note));
    const [icon, text] = SOURCE_STATUS[s.status];
    const st = el("span", { class: "st" });
    st.append(statusIcon(icon), document.createTextNode(text));
    const stc = el("td"); stc.append(st);
    tr.append(name, el("td", {}, s.age_s < 120 ? "just now" : ago(s.last_ts, d.now)), stc);
    tbody.append(tr);
  }

  const cov = chartCard("Collected, last 7 days", "% of each day with live battery data");
  document.getElementById("coverage").replaceChildren(cov.card);
  requestAnimationFrame(() => barChart(cov.box, d.coverage,
    { label: "Collected per day", unit: "%", decimals: 0, max: 100, lastDecimals: 0, lastUnit: "%" }));
  fillTable(cov.tableWrap, ["Day", "Collected"], d.coverage.map(([day, v]) => [day, `${fmtNum(v, 0)}%`]));

  document.getElementById("gaps-sub").textContent = d.episodes.length
    ? `${hours(d.missing_s)} without battery data.` : "";
  const list = document.getElementById("gaps");
  list.replaceChildren();
  if (!d.episodes.length) {
    const li = el("li");
    li.append(statusIcon("good"), el("div", { class: "what" }, "No gaps: data arrived the whole time."));
    list.append(li);
  }
  for (const e of d.episodes) {
    const li = el("li"), what = el("div", { class: "what" });
    const end = e.end === null ? "now (ongoing)" : (dayOnly(e.start) === dayOnly(e.end) ? hhmm(e.end) : dayTime(e.end));
    const span = `${dayTime(e.start)} – ${end} · ${e.count > 1 ? `${e.count} gaps, ` : ""}${hours(e.missing_s)}`;
    what.append(el("div", {}, e.title), el("p", {}, e.explain), el("p", {}, span));
    li.append(statusIcon(GAP_ICON[e.cause] || "info"), what);
    list.append(li);
  }
}

async function loadHealth() {
  try { renderHealth(await getJSON("/health")); }
  catch (err) { console.error(err); }
}

// ---------------------------------------------------------------- Settings

const SOURCE_LABEL = { setup: "Set here", config: "config.yaml", default: "Default" };
let settingsData = null;

function shownValue(s, v, display) {
  if (display) return display;
  if (v === null || v === undefined || v === "") return s.key === "system.timezone" ? "Follow the Cerbo" : "Not set";
  if (s.kind === "bool") return v ? "On" : "Off";
  return `${v}${s.unit}`;
}
function settingValue(s) { return shownValue(s, s.value, s.display); }

// A detected value and the ones that must change with it (a bank layout).
function detectedChanges(s) {
  const changes = { [s.key]: s.detected.value };
  for (const k of s.detected.together || []) {
    const o = settingsData.settings.find((x) => x.key === k);
    if (o && o.detected) changes[k] = o.detected.value;
  }
  return changes;
}

async function saveChanges(changes, msg) {
  msg.className = "msg"; msg.textContent = "Saving…";
  try {
    const r = await postJSON("/settings", { changes });
    renderSettings({ ...settingsData, settings: r.settings, signed_in: true });
    // At the top: the button pressed may have been anywhere on the page.
    document.getElementById("settings-form").prepend(el("div", { class: "msg saved", role: "status" },
      `Saved. ${r.notice ? r.notice + " " : ""}Cerberus is restarting to apply it; data resumes in a few seconds.`));
  } catch (err) { msg.className = "msg err"; msg.textContent = err.message; }
}

function detectedLine(s, signedIn) {
  const d = s.detected, line = el("div", { class: "detected" });
  if (d.learning) {
    statusLine(line, "none", d.reason);
    return line;
  }
  if (d.matches) {
    statusLine(line, "good", d.learned
      ? `Matches what Cerberus has learned: ${shownValue(s, d.value, d.display)}. ${d.reason}`
      : "Matches what the Cerbo reports.");
    return line;
  }
  const text = el("div", { class: "trust" });
  statusLine(text, "info", `${d.learned ? "Learned" : "Cerbo reports"}: ${shownValue(s, d.value, d.display)}. ${d.reason}`);
  line.append(text);
  if (signedIn) {
    const use = el("button", { type: "button", class: "btn quiet" }, "Use");
    const msg = el("div", { class: "msg", role: "status" });
    use.addEventListener("click", () => saveChanges(detectedChanges(s), msg));
    line.append(use, msg);
  }
  return line;
}

function renderDetect(form, d) {
  const det = d.detect || {}, notes = det.notes || [];
  const differ = d.settings.filter((s) => s.detected && s.detected.matches === false);
  if (!notes.length && !differ.length) return;
  const card = el("section", { class: "card set-group" });
  card.append(el("h2", {}, "Detected and learned"));
  const detected = d.settings.some((s) => s.detected && !s.detected.learning);
  if (differ.length || detected) {
    card.append(el("p", { class: "sub" }, differ.length
      ? `${differ.length} setting${differ.length > 1 ? "s differ" : " differs"} from what the Cerbo reports or Cerberus has learned.`
      : "Every detected setting matches."));
  }
  for (const n of notes) card.append(el("p", { class: "help" }, n));
  if (d.signed_in && differ.length) {
    const row = el("div", { class: "lock-row" });
    const all = el("button", { type: "button", class: "btn" }, differ.length > 1 ? "Use all detected" : "Use detected");
    const msg = el("div", { class: "msg", role: "status" });
    all.addEventListener("click", () => {
      const changes = {};
      for (const s of differ) Object.assign(changes, detectedChanges(s));
      saveChanges(changes, msg);
    });
    row.append(all);
    card.append(row, msg);
  }
  form.append(card);
}

function renderLock(d, aboutPinSet) {
  const lock = document.getElementById("lock");
  lock.replaceChildren();
  if (d.signed_in) {
    const row = el("div", { class: "lock-row" });
    const line = el("div", { class: "trust" });
    statusLine(line, "good", "Signed in: you can change settings.");
    const out = el("button", { type: "button", class: "btn quiet" }, "Sign out");
    out.addEventListener("click", async () => { try { await postJSON("/auth/logout", {}); } catch (_) {} setToken(null); stopPhone(); loadSettings(); });
    row.append(line, out);
    lock.append(row);
    return;
  }
  const first = !aboutPinSet;
  lock.append(el("h2", {}, first ? "Set a settings PIN" : "Sign in to change settings"),
    el("p", { class: "sub" }, first
      ? "Viewing is open to anyone on the van's network; changing settings needs a PIN. The first PIN can only be set on the box itself (or with `python -m cerberus.ui --set-pin`)."
      : "Enter the settings PIN."));
  const row = el("div", { class: "lock-row" });
  const pin = el("input", { type: "password", inputmode: "numeric", autocomplete: first ? "new-password" : "current-password", "aria-label": "PIN", placeholder: "PIN" });
  const btn = el("button", { type: "button", class: "btn" }, first ? "Set PIN" : "Sign in");
  const msg = el("div", { class: "msg", role: "status" });
  const go = async () => {
    msg.className = "msg"; msg.textContent = "";
    try {
      const r = await postJSON(first ? "/auth/pin" : "/auth/login", { pin: pin.value });
      setToken(r.token); loadSettings(); startPhone();
    } catch (err) { msg.className = "msg err"; msg.textContent = err.message; }
  };
  btn.addEventListener("click", go);
  pin.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
  row.append(pin, btn);
  lock.append(row, msg);
}

function renderSettings(d) {
  settingsData = d;
  const form = document.getElementById("settings-form");
  form.replaceChildren();
  if (lastAccess) renderAccess(lastAccess);
  renderDetect(form, d);
  const groups = [...new Set(d.settings.map((s) => s.group))];
  for (const g of groups) {
    const card = el("section", { class: "card set-group" });
    card.append(el("h2", {}, g));
    for (const s of d.settings.filter((x) => x.group === g)) {
      const row = el("div", { class: "set-row" }), head = el("div", { class: "set-head" });
      head.append(el("span", { class: "name" }, s.label), el("span", { class: "src" }, SOURCE_LABEL[s.source]),
        el("span", { class: "val" }, settingValue(s)));
      row.append(head, el("p", { class: "help" }, s.help));
      if (s.detected) row.append(detectedLine(s, d.signed_in));
      if (d.signed_in && s.kind !== "packs" && s.kind !== "map") {
        let input;
        if (s.kind === "bool") {
          input = el("select", { name: s.key, "aria-label": s.label });
          for (const [val, text] of [["true", "On"], ["false", "Off"]]) {
            const o = el("option", { value: val }, text);
            if (String(s.value) === val) o.selected = true;
            input.append(o);
          }
        } else if (s.kind === "choice") {
          input = el("select", { name: s.key, "aria-label": s.label });
          for (const c of s.choices) {
            const o = el("option", { value: String(c) }, `${c}${s.unit}`);
            if (String(c) === String(s.value)) o.selected = true;
            input.append(o);
          }
        } else {
          input = el("input", { name: s.key, "aria-label": s.label, type: "text",
            inputmode: s.kind === "number" ? "decimal" : "text",
            value: s.value === null || s.value === undefined ? "" : String(s.value),
            placeholder: s.optional ? "Leave empty" : "" });
        }
        input.dataset.orig = input.value;
        row.append(input);
      }
      card.append(row);
    }
    form.append(card);
  }
  if (d.signed_in) {
    const actions = el("div", { class: "actions" });
    const save = el("button", { type: "submit", class: "btn" }, "Save changes");
    const msg = el("div", { class: "msg", role: "status" });
    actions.append(save, msg);
    form.append(actions);
    form.onsubmit = async (e) => {
      e.preventDefault();
      const changes = {};
      form.querySelectorAll("[name]").forEach((i) => { if (i.value !== i.dataset.orig) changes[i.name] = i.value; });
      if (!Object.keys(changes).length) { msg.className = "msg"; msg.textContent = "Nothing changed."; return; }
      saveChanges(changes, msg);
    };
  }
}

async function loadSettings() {
  try {
    const [d, about, status] = await Promise.all([getJSON("/settings"), getJSON("/about"), getJSON("/setup/status")]);
    if (!d.signed_in && token()) setToken(null);          // expired session
    renderLock(d, about.pin_set);
    if (d.set_up === false) return renderWizard(d.signed_in);
    renderSettings(d);
    getJSON("/access").then(renderAccess).catch(() => {});
    renderConnection(status.connection, d.signed_in);
    renderBluetooth(d.signed_in);
    renderPhone(d.signed_in);
  } catch (err) { console.error(err); }
}

// ------------------------------------------------------------ First-run setup

// One row per decision: settings that change together (a bank layout) are
// shown and applied as one.
const BANK_KEYS = ["bank.source", "bank.monitor_instance", "packs"];
function proposalRows(props) {
  const rows = [], taken = new Set();
  for (const [key, p] of Object.entries(props)) {
    if (taken.has(key)) continue;
    const keys = [...new Set([key, ...(p.together || [])])].filter((k) => props[k]);
    keys.forEach((k) => taken.add(k));
    let label = p.label, value;
    if (BANK_KEYS.includes(key)) {
      label = "Battery setup";
      const inst = props["bank.monitor_instance"];
      value = props.packs ? props.packs.display
        : `One battery monitor${inst && inst.value !== null ? ` (instance ${inst.value})` : ""}`;
    } else {
      value = p.display || (p.value === null ? "None" : `${p.value}${p.unit || ""}`);
    }
    rows.push({ keys, label, value, reason: (props["bank.source"] && BANK_KEYS.includes(key)) ? props["bank.source"].reason : p.reason });
  }
  return rows;
}

function renderWizard(signedIn) {
  const form = document.getElementById("settings-form");
  form.replaceChildren();
  form.onsubmit = (e) => e.preventDefault();
  const card = el("section", { class: "card set-group wizard" });
  card.append(el("h2", {}, "Set up Cerberus"),
    el("p", { class: "sub" }, "Cerberus reads your van's Victron Cerbo GX over the local network. Connect it once; everything else is detected."));
  form.append(card);
  if (!signedIn) {
    card.append(el("p", { class: "help" }, "Sign in with the settings PIN above to start."));
    return;
  }
  const w = { address: "", host: "" };

  // Step 1: find the Cerbo.
  const s1 = el("div", { class: "step" });
  s1.append(el("h3", {}, "1. Find the Cerbo"));
  const list = el("div", { class: "candidates", role: "radiogroup", "aria-label": "Cerbo" });
  const manual = el("input", { type: "text", "aria-label": "Cerbo address", placeholder: "Or type its address, e.g. 192.168.1.50" });
  manual.addEventListener("input", () => { w.address = w.host = manual.value.trim(); list.querySelectorAll("input").forEach((i) => { i.checked = false; }); });
  const search = el("button", { type: "button", class: "btn quiet" }, "Search the network");
  const msg1 = el("div", { class: "msg", role: "status" });
  search.addEventListener("click", async () => {
    msg1.className = "msg"; msg1.textContent = "Searching… (a few seconds)"; list.replaceChildren();
    try {
      const r = await postJSON("/setup/discover", {});
      const gx = r.candidates.filter((c) => c.gx);
      msg1.textContent = gx.length ? "" : (r.candidates.length
        ? "Found MQTT brokers but no Cerbo. Pick one if you're sure, or type the Cerbo's address."
        : "No Cerbo found. Check it's on the same network, then type its address.");
      for (const c of (gx.length ? gx : r.candidates)) {
        const id = `cand-${c.address}`;
        const radio = el("input", { type: "radio", name: "cerbo", id, value: c.address });
        radio.addEventListener("change", () => { w.address = c.address; w.host = c.host; manual.value = ""; });
        const text = c.host === c.address ? c.address : `${c.host} (${c.address})`;
        list.append(el("label", { class: "cand", for: id }, ""));
        list.lastChild.append(radio, document.createTextNode(` ${text}${c.gx ? " · Victron GX" : ""}`));
      }
      const first = list.querySelector("input");
      if (first && gx.length) { first.checked = true; first.dispatchEvent(new Event("change")); }
    } catch (err) { msg1.className = "msg err"; msg1.textContent = err.message; }
  });
  s1.append(el("div", { class: "lock-row" }), list, manual, msg1);
  s1.children[1].append(search);

  // Step 2: password, checked on the Cerbo itself.
  const s2 = el("div", { class: "step" });
  s2.append(el("h3", {}, "2. Connect"),
    el("p", { class: "help" }, "The Cerbo's Remote Console password (Settings › General on the GX). It's kept in .env on this box only, never in the configuration file."));
  const pw = el("input", { type: "password", autocomplete: "off", "aria-label": "Cerbo password", placeholder: "Cerbo password" });
  const connect = el("button", { type: "button", class: "btn" }, "Connect");
  const msg2 = el("div", { class: "msg", role: "status" });
  const row2 = el("div", { class: "lock-row" });
  row2.append(pw, connect);
  s2.append(row2, msg2);

  // Step 3: review what was detected, then finish.
  const s3 = el("div", { class: "step", hidden: "" });

  connect.addEventListener("click", async () => {
    if (!w.address) { msg2.className = "msg err"; msg2.textContent = "Pick or type the Cerbo's address first."; return; }
    msg2.className = "msg"; msg2.textContent = "Connecting… (up to 15 seconds)";
    s3.hidden = true;
    try {
      const r = await postJSON("/setup/probe", { address: w.address, password: pw.value });
      msg2.className = "msg trust"; msg2.replaceChildren();
      statusLine(msg2, "good", `Connected to Cerbo ${r.portal_id}.`);
      renderReview(s3, r, w, pw);
    } catch (err) { msg2.className = "msg err"; msg2.textContent = err.message; }
  });
  card.append(s1, s2, s3);
}

function renderReview(s3, r, w, pw) {
  s3.hidden = false;
  s3.replaceChildren(el("h3", {}, "3. Check what was detected"));
  const rows = proposalRows(r.proposals);
  const boxes = [];
  for (const row of rows) {
    const id = `prop-${row.keys[0]}`;
    const box = el("input", { type: "checkbox", id });
    box.checked = true;
    boxes.push([box, row]);
    const line = el("label", { class: "prop", for: id });
    const text = el("span", {});
    text.append(el("b", {}, `${row.label}: `), document.createTextNode(row.value), el("br"), el("span", { class: "help" }, row.reason));
    line.append(box, text);
    s3.append(line);
  }
  for (const n of r.notes) s3.append(el("p", { class: "help" }, n));
  let zip = null;
  if (!r.proposals["weather.latitude"]) {
    // No GPS to say where home is: a ZIP code gives the weather forecast a place.
    const zl = el("label", { class: "prop", for: "setup-zip" });
    const zt = el("span", {});
    zt.append(el("b", {}, "Home ZIP code (optional): "), el("br"),
      el("span", { class: "help" }, "For the weather forecast. US only; looked up once at zippopotam.us."));
    zl.append(zt);
    zip = el("input", { type: "text", id: "setup-zip", inputmode: "numeric", autocomplete: "postal-code", placeholder: "e.g. 07960", maxlength: "10" });
    s3.append(zl, zip);
  }
  s3.append(el("p", { class: "help" }, "Anything else (solar panels, alternator, home location) can be changed under Settings afterwards."));
  const finish = el("button", { type: "button", class: "btn" }, "Finish setup");
  const msg = el("div", { class: "msg", role: "status" });
  finish.addEventListener("click", async () => {
    const changes = {};
    for (const [box, row] of boxes) if (box.checked) for (const k of row.keys) changes[k] = r.proposals[k].value;
    if (zip && zip.value.trim()) changes["weather.zip_code"] = zip.value.trim();
    msg.className = "msg"; msg.textContent = "Saving…";
    try {
      await postJSON("/setup/complete", { address: w.address, host: w.host || w.address, password: pw.value, changes });
      pw.value = "";
      const done = el("section", { class: "card set-group" });
      done.append(el("h2", {}, "Cerberus is set up"));
      const ok = el("div", { class: "trust" });
      statusLine(ok, "good", "The collector is starting. Live data appears on Now within a minute.");
      const go = el("p", {});
      go.append(el("a", { href: "#now" }, "Go to Now"));
      done.append(ok, go);
      document.getElementById("settings-form").replaceChildren(done);
    } catch (err) { msg.className = "msg err"; msg.textContent = err.message; }
  });
  const row = el("div", { class: "actions" });
  row.append(finish, msg);
  s3.append(row);
}

// ------------------------------------------------------------ Open on a phone

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch (_) {}
  // Plain-HTTP pages have no clipboard API: the old way still works there.
  const t = el("textarea", { readonly: "", style: "position:fixed;opacity:0" });
  t.value = text;
  document.body.append(t);
  t.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (_) {}
  t.remove();
  return ok;
}

function addressRow(label, url, help) {
  const row = el("div", { class: "set-row" }), head = el("div", { class: "set-head" });
  const link = el("a", { href: url, class: "addr" }, url);
  const copy = el("button", { type: "button", class: "btn quiet" }, "Copy");
  copy.addEventListener("click", async () => {
    copy.textContent = (await copyText(url)) ? "Copied" : "Copy failed";
    setTimeout(() => { copy.textContent = "Copy"; }, 2000);
  });
  head.append(el("span", { class: "name" }, label));
  const line = el("div", { class: "lock-row" });
  line.append(link, copy);
  row.append(head, line, el("p", { class: "help" }, help));
  return row;
}

let lastAccess = null;
function renderAccess(a) {
  lastAccess = a;
  const form = document.getElementById("settings-form");
  if (!form || form.querySelector(".access")) return;
  const card = el("section", { class: "card set-group access" });
  card.append(el("h2", {}, "Open Cerberus on your phone"));
  const ts = a.tailscale;
  if (ts && ts.url) {
    card.append(addressRow("From anywhere (Tailscale)", ts.url,
      "With Tailscale on, on any of your devices. HTTPS, so phone location sharing works. Add it to your home screen."));
  } else if (ts) {
    card.append(el("p", { class: "help" }, `Tailscale is on (${ts.name}) but not serving Cerberus over HTTPS. On this box, run: ${ts.command}`));
  }
  if (a.lan) card.append(addressRow("On the van's network", a.lan, "From a phone or laptop on the van's WiFi."));
  if (card.children.length > 1) form.prepend(card);
}

// ------------------------------------------------------------ Bluetooth devices

const ROLE_LABEL = { pack: "Battery pack", acr: "ACR", sensor: "Temperature sensor" };

function renderBluetooth(signedIn) {
  if (!signedIn) return;
  const form = document.getElementById("settings-form");
  const card = el("section", { class: "card set-group bt" });
  card.append(el("h2", {}, "Find Bluetooth devices"),
    el("p", { class: "help" }, "Looks for Lithionics batteries and the ACR, and for Bluetooth thermometers (Govee, Xiaomi, SwitchBot, BTHome and others). Takes up to a minute. Devices Cerberus is already connected to don't show up, since they stop advertising; the Bluetooth settings above list those."));
  const go = el("button", { type: "button", class: "btn quiet" }, "Scan");
  const msg = el("div", { class: "msg", role: "status" });
  const results = el("div", { class: "bt-results" });
  const row = el("div", { class: "lock-row" });
  row.append(go);
  card.append(row, msg, results);
  go.addEventListener("click", async () => {
    go.disabled = true; results.replaceChildren();
    msg.className = "msg"; msg.textContent = "Scanning… (up to a minute)";
    try {
      const r = await postJSON("/setup/ble/scan", {});
      msg.textContent = r.found.length ? `Found ${r.found.length} device${r.found.length > 1 ? "s" : ""}.` : "Nothing new found nearby.";
      showBluetooth(results, r);
    } catch (err) { msg.className = "msg err"; msg.textContent = err.message; }
    go.disabled = false;
  });
  form.append(card);
}

function showBluetooth(box, r) {
  const packs = (r.proposals["ble.pack_addresses"] || {}).value || {};
  const numberOf = Object.fromEntries(Object.entries(packs).map(([n, a]) => [a, n]));
  const cabin = Object.keys((r.proposals["ble.cabin_sensors"] || {}).value || {})[0];
  const picks = [];
  for (const f of r.found) {
    const id = `bt-${f.address}`;
    const pick = el("input", { type: "checkbox", id });
    pick.checked = f.role !== "sensor" || f.address === cabin;
    const label = el("label", { class: "prop", for: id });
    const text = el("span", {});
    const what = f.role === "pack" && numberOf[f.address] ? `Battery pack ${numberOf[f.address]}` : ROLE_LABEL[f.role] || f.role;
    text.append(el("b", {}, `${what}: `), document.createTextNode(`${f.name || "(no name)"}${f.detail ? " · " + f.detail : ""}`),
      el("br"), el("span", { class: "help" }, `${f.address}${f.rssi != null ? ` · signal ${f.rssi} dBm` : ""}`));
    label.append(pick, text);
    box.append(label);
    let name = null;
    if (f.role === "sensor") {
      name = el("input", { type: "text", class: "bt-name", "aria-label": "Sensor name", placeholder: "Name: cabin, fridge, outside…" });
      if (f.address === cabin) name.value = "cabin";
      box.append(name);
    }
    picks.push({ f, pick, name });
  }
  if (!picks.length) return;
  const use = el("button", { type: "button", class: "btn" }, "Use selected");
  const msg = el("div", { class: "msg", role: "status" });
  use.addEventListener("click", () => {
    const chosen = picks.filter((p) => p.pick.checked);
    const changes = { "ble.enabled": true };
    const pk = chosen.filter((p) => p.f.role === "pack");
    if (pk.length) changes["ble.pack_addresses"] = Object.fromEntries(pk.map((p, i) => [numberOf[p.f.address] || String(i + 1), p.f.address]));
    const acr = chosen.find((p) => p.f.role === "acr");
    if (acr) changes["ble.acr_address"] = acr.f.address;
    const sensors = chosen.filter((p) => p.f.role === "sensor");
    if (sensors.some((p) => !p.name.value.trim())) { msg.className = "msg err"; msg.textContent = "Give each selected sensor a name (cabin, fridge, outside…)."; return; }
    if (sensors.length) changes["ble.cabin_sensors"] = Object.fromEntries(sensors.map((p) => [p.f.address, p.name.value.trim()]));
    saveChanges(changes, msg);
  });
  const actions = el("div", { class: "actions" });
  actions.append(use, msg);
  box.append(actions);
}

// ------------------------------------------------------------ Phone location
// Opt-in per phone (localStorage): while the app is open, send this phone's
// position; the box uses it for the van's only while the phone is with the van
// (cerberus.phonefix). Browsers share location only on HTTPS or localhost.

const PHONE_KEY = "cerberus.phoneLocation";
const PHONE_SEND_S = 120, PHONE_MOVE_M = 250;
const phone = { watch: null, last: null, result: null };

function phoneOn() { try { return localStorage.getItem(PHONE_KEY) === "on"; } catch (_) { return false; } }
function setPhoneOn(on) { try { localStorage.setItem(PHONE_KEY, on ? "on" : "off"); } catch (_) {} }
function phoneCapable() { return window.isSecureContext && "geolocation" in navigator; }

function metres(a, b) {
  const r = Math.PI / 180, x = (b.lon - a.lon) * r * Math.cos(((a.lat + b.lat) / 2) * r), y = (b.lat - a.lat) * r;
  return Math.sqrt(x * x + y * y) * 6371000;
}

async function sendPhoneFix(pos) {
  const fix = { lat: pos.coords.latitude, lon: pos.coords.longitude, accuracy_m: pos.coords.accuracy, ts_ms: pos.timestamp };
  const due = !phone.last || (Date.now() - phone.last.at) / 1000 >= PHONE_SEND_S || metres(phone.last, fix) >= PHONE_MOVE_M;
  if (!due) return;
  phone.last = { ...fix, at: Date.now() };
  try {
    phone.result = { ...(await postJSON("/location/phone", fix)), at: Date.now() };
  } catch (err) {
    phone.result = { accepted: false, reason: err.message, at: Date.now() };
    if (/PIN|401/.test(err.message)) stopPhone();
  }
  const line = document.getElementById("phone-status");
  if (line) phoneStatus(line);
}

function startPhone() {
  if (phone.watch !== null || !phoneOn() || !phoneCapable() || !token()) return;
  phone.watch = navigator.geolocation.watchPosition(sendPhoneFix, (err) => {
    phone.result = { accepted: false, reason: err.code === 1 ? "Location permission was refused for this site." : `No position: ${err.message}`, at: Date.now() };
    const line = document.getElementById("phone-status");
    if (line) phoneStatus(line);
  }, { enableHighAccuracy: false, maximumAge: 60000, timeout: 30000 });
}
function stopPhone() {
  if (phone.watch !== null) navigator.geolocation.clearWatch(phone.watch);
  phone.watch = null;
}

function phoneStatus(line) {
  const r = phone.result;
  if (!phoneOn()) return statusLine(line, "none", "Off on this phone.");
  if (!r) return statusLine(line, "none", "Waiting for this phone's position…");
  statusLine(line, r.accepted ? "good" : "info", `${r.reason} (${ago(Math.round(r.at / 1000), Math.round(Date.now() / 1000))})`);
}

function renderPhone(signedIn) {
  const form = document.getElementById("settings-form");
  const card = el("section", { class: "card set-group" });
  card.append(el("h2", {}, "This phone's location"),
    el("p", { class: "help" }, "For vans without GPS on the Cerbo: while Cerberus is open on this phone, it sends the phone's position, and the van uses it for the weather when the phone is with the van (on the van's network, or while driving). Positions stay on the box."));
  if (!phoneCapable()) {
    card.append(el("p", { class: "help" }, "This browser shares location only over HTTPS. Open Cerberus at its https://….ts.net address (set up with tailscale serve), or on the box at http://localhost:8088."));
  } else if (!signedIn) {
    card.append(el("p", { class: "help" }, "Sign in above to turn it on."));
  } else {
    const id = "phone-on";
    const box = el("input", { type: "checkbox", id });
    box.checked = phoneOn();
    const label = el("label", { class: "prop", for: id });
    label.append(box, el("span", {}, "Use this phone's location for the van"));
    const line = el("div", { class: "detected", id: "phone-status" });
    box.addEventListener("change", () => {
      setPhoneOn(box.checked);
      phone.last = null; phone.result = null;
      if (box.checked) startPhone(); else stopPhone();
      phoneStatus(line);
    });
    phoneStatus(line);
    card.append(label, line);
  }
  form.append(card);
}

function renderConnection(conn, signedIn) {
  if (!conn) return;
  const form = document.getElementById("settings-form");
  const card = el("section", { class: "card set-group" });
  card.append(el("h2", {}, "Cerbo connection"));
  const row = el("div", { class: "set-row" }), head = el("div", { class: "set-head" });
  head.append(el("span", { class: "name" }, "Cerbo"), el("span", { class: "val" }, conn.portal_id));
  row.append(head, el("p", { class: "help" }, `Addresses tried in order: ${conn.hosts.join(", ")}. Password ${conn.password_set ? "stored in .env" : "not set"}.`));
  card.append(row);
  if (signedIn) {
    const r2 = el("div", { class: "set-row" });
    r2.append(el("span", { class: "name" }, "Change the Cerbo password"),
      el("p", { class: "help" }, "After changing it on the GX. Checked on the Cerbo before it's saved."));
    const lr = el("div", { class: "lock-row" });
    const pw = el("input", { type: "password", autocomplete: "off", "aria-label": "New Cerbo password", placeholder: "New password" });
    const go = el("button", { type: "button", class: "btn quiet" }, "Check and save");
    const msg = el("div", { class: "msg", role: "status" });
    go.addEventListener("click", async () => {
      msg.className = "msg"; msg.textContent = "Checking on the Cerbo…";
      try {
        await postJSON("/setup/password", { password: pw.value });
        pw.value = ""; msg.textContent = "Saved. Cerberus is restarting to use it.";
      } catch (err) { msg.className = "msg err"; msg.textContent = err.message; }
    });
    lr.append(pw, go);
    r2.append(lr, msg);
    card.append(r2);
  }
  form.append(card);
}

// ---------------------------------------------------------------- History

function renderHistory(h) {
  state.tz = h.tz || state.tz;
  const holder = document.getElementById("charts");
  holder.replaceChildren();
  // "All" starts at the oldest sample of any series; other ranges at their cutoff.
  const firsts = [h.bank, h.solar_w, h.ac_w, h.cabin_f, h.outside_f].filter((r) => r.length).map((r) => r[0][0]);
  const t0 = h.since > 0 ? h.since : (firsts.length ? Math.min(...firsts) : h.now - 86400);
  const common = { t0, now: h.now, bucket: h.bucket_s };
  const when = (t) => (h.bucket_s >= 86400 ? dayOnly(t) : dayTime(t));
  const col = (rows, k) => rows.map((r) => [r[0], r[k]]);

  const defs = [
    { title: "Bank charge", sub: "State of charge, %", pts: col(h.bank, 1), unit: "%", d: 0 },
    { title: "Bank voltage", sub: "Volts, mean of packs", pts: col(h.bank, 2), unit: " V", d: 2 },
    { title: "Net current", sub: "Amps into (+) or out of (−) the bank", pts: col(h.bank, 3), unit: " A", d: 1, zero: true },
    { title: "Solar power", sub: "Watts from the array", pts: h.solar_w, unit: " W", d: 0, zero: true },
    { title: "AC load", sub: "Watts on the inverter output", pts: h.ac_w, unit: " W", d: 0, zero: true },
  ];
  for (const c of defs) {
    const { card, box, tableWrap } = chartCard(c.title, c.sub);
    holder.append(card);
    lineChart(box, [{ name: c.title, color: "--series-1", pts: c.pts }], { ...common, unit: c.unit, decimals: c.d, zero: c.zero, label: c.title });
    fillTable(tableWrap, ["Time", c.title], c.pts.map(([t, v]) => [when(t), `${fmtNum(v, c.d)}${c.unit}`]));
  }

  const temps = [{ name: "Cabin", color: "--series-1", pts: h.cabin_f }, { name: "Outside", color: "--series-2", pts: h.outside_f }];
  const tc = chartCard("Temperature", "°F, cabin sensor and outside forecast", temps);
  holder.append(tc.card);
  lineChart(tc.box, temps, { ...common, unit: "°F", decimals: 0, label: "Temperature" });
  const outside = new Map(h.outside_f.map((p) => [p[0], p[1]]));
  const ts = [...new Set([...h.cabin_f.map((p) => p[0]), ...h.outside_f.map((p) => p[0])])].sort((a, b) => a - b);
  const cabin = new Map(h.cabin_f.map((p) => [p[0], p[1]]));
  fillTable(tc.tableWrap, ["Time", "Cabin", "Outside"], ts.map((t) => [when(t), `${fmtNum(cabin.get(t), 0)}°F`, `${fmtNum(outside.get(t), 0)}°F`]));

  if (h.range !== "24h") {
    const yc = chartCard("Daily solar yield", "kWh per day");
    holder.append(yc.card);
    barChart(yc.box, h.daily_yield_kwh, { label: "Daily solar yield" });
    fillTable(yc.tableWrap, ["Day", "Yield"], h.daily_yield_kwh.map(([d, v]) => [d, `${fmtNum(v, 2)} kWh`]));
  }
}

async function loadHistory() {
  document.querySelectorAll("#charts .chart").forEach((c) => c.classList.add("loading"));
  try {
    state.lastHistory = await getJSON(`/history?range=${encodeURIComponent(state.range)}`);
    renderHistory(state.lastHistory);
  } catch (err) { console.error(err); }
}

// ---------------------------------------------------------------- theme

// Auto follows the OS; Dark and Light stamp data-theme on <html>, which the
// stylesheet honours both ways. The choice is a per-device convenience, so
// it lives in localStorage and the page works without it.
const THEMES = ["auto", "dark", "light"];
const THEME_KEY = "cerberus.theme";
function themeIcon(mode) {
  const s = svg("svg", { width: 12, height: 12, viewBox: "0 0 12 12", "aria-hidden": "true" });
  if (mode === "dark") s.append(svg("path", { d: "M8.5 1.5A5 5 0 1 0 10.5 8 4 4 0 0 1 8.5 1.5z", fill: "currentColor" }));
  else if (mode === "light") {
    s.append(svg("circle", { cx: 6, cy: 6, r: 2.4, fill: "currentColor" }));
    for (let k = 0; k < 8; k++) {
      const a = k * Math.PI / 4;
      s.append(svg("line", { x1: 6 + 3.6 * Math.cos(a), y1: 6 + 3.6 * Math.sin(a), x2: 6 + 5 * Math.cos(a), y2: 6 + 5 * Math.sin(a), stroke: "currentColor", "stroke-width": 1.2, "stroke-linecap": "round" }));
    }
  } else {
    s.append(svg("circle", { cx: 6, cy: 6, r: 5, fill: "none", stroke: "currentColor", "stroke-width": 1.2 }),
             svg("path", { d: "M6 1a5 5 0 0 1 0 10z", fill: "currentColor" }));
  }
  return s;
}
function readTheme() {
  try { const t = localStorage.getItem(THEME_KEY); return THEMES.includes(t) ? t : "auto"; }
  catch (_) { return "auto"; }
}
function applyTheme(mode) {
  if (mode === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = mode;
  try { if (mode === "auto") localStorage.removeItem(THEME_KEY); else localStorage.setItem(THEME_KEY, mode); }
  catch (_) { /* private window: the choice just lasts this visit */ }
  const btn = document.getElementById("theme");
  const label = { auto: "Auto", dark: "Dark", light: "Light" }[mode];
  btn.replaceChildren(themeIcon(mode), document.createTextNode(label));
  btn.setAttribute("aria-label", `Theme: ${label}. Change theme`);
}
let theme = readTheme();
document.getElementById("theme").addEventListener("click", () => {
  theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
  applyTheme(theme);
});

// ---------------------------------------------------------------- shell

function setView(view) {
  state.view = view;
  for (const v of VIEWS) document.getElementById(`view-${v}`).hidden = v !== view;
  document.querySelectorAll("nav a").forEach((a) => {
    if (a.dataset.view === view) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  clearInterval(state.histTimer);
  if (view === "history") { loadHistory(); state.histTimer = setInterval(loadHistory, HISTORY_POLL_MS); }
  if (view === "chassis") { loadChassis(); state.histTimer = setInterval(loadChassis, NOW_POLL_MS); }
  if (view === "findings") { loadFindings(); state.histTimer = setInterval(loadFindings, HISTORY_POLL_MS); }
  if (view === "health") { loadHealth(); state.histTimer = setInterval(loadHealth, HISTORY_POLL_MS); }
  if (view === "settings") loadSettings();          // no polling: it would wipe edits
}

document.querySelectorAll(".filters button").forEach((b) => {
  b.setAttribute("aria-pressed", String(b.dataset.range === state.range));
  b.addEventListener("click", () => {
    state.range = b.dataset.range;
    document.querySelectorAll(".filters button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    loadHistory();
  });
});
const VIEWS = ["now", "chassis", "findings", "history", "health", "settings"];
const viewFromHash = () => (VIEWS.includes(location.hash.slice(1)) ? location.hash.slice(1) : "now");
window.addEventListener("hashchange", () => setView(viewFromHash()));
let resizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { if (state.view === "history" && state.lastHistory) renderHistory(state.lastHistory); }, 150);
});

// AGPL-3.0 section 13: offer the source to everyone using this UI.
getJSON("/about").then((a) => {
  if (!a.source_url) return;
  const f = document.getElementById("about");
  f.replaceChildren(document.createTextNode("Cerberus is free software under the AGPL-3.0. "),
    el("a", { href: a.source_url, target: "_blank", rel: "noopener noreferrer" }, "Source code"));
}).catch(() => {});

applyTheme(theme);
setView(viewFromHash());
getJSON("/about").then((a) => { if (a.set_up === false && state.view === "now") location.hash = "#settings"; }).catch(() => {});
startPhone();
pollNow();
state.nowTimer = setInterval(pollNow, NOW_POLL_MS);
