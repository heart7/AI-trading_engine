/* UCHFE paper UI shell (spec §16). The client draws; it never computes or formats a material figure.
   Every figure arrives from the BFF with its rendering already decided (engine/ui/render.py). */
(function () {
"use strict";
var U = window.UCHFE = {screens: {}};

/* ---------- transport: GET projections only; the Reporter is the one POST ---------- */
U.get = function (path) {
  var cur = U.cursor ? (path.indexOf("?") < 0 ? "?" : "&") + "cursor=" + encodeURIComponent(U.cursor) : "";
  return fetch(path + cur, {method: "GET", headers: {"Accept": "application/json"}}).then(function (r) {
    if (!r.ok) throw new Error(path + " → " + r.status);
    return r.json();
  });
};
function ask(q) {
  return fetch("/v1/reporter/ask", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({question: q})}).then(function (r) { return r.json(); });
}

/* ---------- helpers ---------- */
var esc = U.esc = function (s) {
  return String(s === null || s === undefined ? "—" : s).replace(/[&<>"]/g, function (c) {
    return {"&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;"}[c];
  });
};
var tip = document.getElementById("tip");
U.showTip = function (html, x, y) {
  tip.innerHTML = html; tip.style.opacity = "1";
  var w = tip.offsetWidth, h = tip.offsetHeight, nx = x + 14, ny = y + 14;
  if (nx + w > innerWidth - 8) nx = x - w - 14;
  if (ny + h > innerHeight - 8) ny = y - h - 14;
  tip.style.left = Math.max(8, nx) + "px"; tip.style.top = Math.max(8, ny) + "px";
};
U.hideTip = function () { tip.style.opacity = "0"; };
U.bindTip = function (el, fn) {
  el.addEventListener("pointermove", function (e) { U.showTip(fn(), e.clientX, e.clientY); });
  el.addEventListener("pointerleave", U.hideTip);
  el.addEventListener("focus", function () { var r = el.getBoundingClientRect(); U.showTip(fn(), r.right, r.top); });
  el.addEventListener("blur", U.hideTip);
};

/* A figure: the BFF's render spec decides classes, text, band, chips and warnings. */
var fig = U.fig = function (f, opt) {
  if (!f) return '<span class="muted">—</span>';
  if (typeof f !== "object" || !f.render) return esc(f);
  var r = f.render, cls = r.style.map(function (s) { return "st-" + s; }).join(" ");
  var h = '<span class="f ' + cls + '" data-fid="' + esc(f.id) + '" data-class="' + esc(f["class"]) + '" data-style="' +
    esc(r.style.join(" ")) + '" title="' + esc(f.label + " · " + f["class"] + (f.source ? " · " + f.source : "")) + '">';
  if (!(opt && opt.nolabel) && opt && opt.label) h += '<span class="muted">' + esc(f.label) + '</span>';
  h += '<span class="v">' + esc(r.display) + '</span>';
  if (r.band) h += '<span class="bd">' + esc(r.band.display) + '</span>';
  r.chips.forEach(function (c) { if (c.kind !== "FIXTURE" || (opt && opt.chip)) h += '<span class="fchip ' + esc(c.kind) + '">' + esc(c.text) + '</span>'; });
  r.warnings.forEach(function (w) { h += '<span class="warnbox" role="note">' + esc(w.text) + '</span>'; });
  if (f.reason) h += '<span class="fchip">' + esc(f.reason) + '</span>';
  return h + '</span>';
};
U.panel = function (title, body, o) {
  o = o || {};
  return '<section class="panel pad' + (o.fixture !== false ? ' fixture' : '') + (o.stale ? ' stale' : '') + '"' +
    (o.id ? ' id="' + o.id + '"' : '') + '><h3>' + esc(title) + '</h3>' + (o.note ? '<div class="note">' + esc(o.note) + '</div>' : '') +
    '<div style="margin-top:8px">' + body + '</div></section>';
};
U.kv = function (rows) {
  return '<dl class="kv2">' + rows.map(function (r) { return '<dt>' + esc(r[0]) + '</dt><dd>' + r[1] + '</dd>'; }).join("") + '</dl>';
};
U.table = function (head, rows, label) {
  return '<div class="scroll-x"><table aria-label="' + esc(label || "") + '"><thead><tr>' + head.map(function (h) { return '<th>' + esc(h) + '</th>'; }).join("") +
    '</tr></thead><tbody>' + rows.map(function (r) { return '<tr>' + r.map(function (c) { return '<td>' + c + '</td>'; }).join("") + '</tr>'; }).join("") +
    '</tbody></table></div>';
};
U.verdict = function (v) {
  var cl = v === "PASS" ? "pass" : v === "FAIL" ? "fail" : "notrun", ic = v === "PASS" ? "✓" : v === "FAIL" ? "✗" : "○";
  return '<span class="' + cl + '"><span class="ico">' + ic + '</span>' + esc(v === "NOT_RUN" ? "NOT RUN" : v) + '</span>';
};
U.status = function (st) {  /* status colours are reserved for the loss ladder and always come with an icon + label */
  var m = {ok: ["✓", "within limit", "good"], warn: ["▲", "near limit", "warn"], breach: ["✗", "breached", "crit"]}[st] || ["○", st, ""];
  return '<span class="chip ' + m[2] + '"><span class="ico">' + m[0] + '</span>' + m[1] + '</span>';
};
U.ladder = function (rows) {
  return rows.map(function (l) {
    var u = Math.min(100, Math.max(1, l.utilisation * 100));
    var extra = l.rungs ? '<div class="resp">' + l.rungs.map(function (r) { return (r.hit ? "✗ " : "") + esc(r.display) + " " + esc(r.consequence); }).join(" · ") + '</div>' : "";
    return '<div class="lim"><span>' + esc(l.label) + '</span><div class="bar" role="img" aria-label="' + esc(l.label) + ' ' + esc(l.value.render.display) +
      ' of ' + esc(l.limit.render.display) + '"><div class="f' + (l.status !== "ok" ? " warn" : "") + '" style="width:' + u + '%"></div></div><span>' +
      fig(l.value) + ' / ' + fig(l.limit) + ' ' + U.status(l.status) + '</span><div class="resp">at breach: ' + esc(l.consequence) +
      (l.next_rung ? " · next rung " + esc(l.next_rung) : "") + '</div>' + extra + '</div>';
  }).join("");
};
U.banner = function () {
  return '<div class="banner fixture" role="note"><b>FIXTURE_*</b><span>Every figure here is generated sample data (certified: false) replayed ' +
    'through the real decision code in PAPER mode. Nothing here is a market observation or a performance claim.</span></div>';
};
U.sec = function (title, sub, body) {
  return '<section class="sec"><div class="sec-h"><div><h2>' + esc(title) + '</h2>' + (sub ? '<p>' + esc(sub) + '</p>' : '') + '</div></div>' + body + '</section>';
};

/* ---------- navigation (§16.1): 15 screens + Reporter ---------- */
var NAV = [["fund-room", "Fund Room"], ["activity", "Activity"], ["markets", "Markets"], ["strategy", "Strategy"], ["probability", "Probability"],
  ["pnl", "P&L & Accounting"], ["outcomes", "Outcomes"], ["validation", "Validation"], ["data", "Data"], ["bots", "Bots & Models"],
  ["risk", "Risk & Limits"], ["incidents", "Incidents"], ["governance", "Governance"], ["settings", "Settings"], ["exchanges", "Settings → Exchanges"]];
U.NAV = NAV;
var nav = document.getElementById("nav");
nav.innerHTML = NAV.map(function (n) { return '<a href="#/' + n[0] + '" data-s="' + n[0] + '">' + esc(n[1]) + '</a>'; }).join("");
var screen = document.getElementById("screen");
function route() {
  var h = (location.hash || "#/fund-room").slice(2).split("?")[0], parts = h.split("/"), name = parts[0] || "fund-room";
  nav.querySelectorAll("a").forEach(function (a) { a.setAttribute("aria-current", a.getAttribute("data-s") === name ? "page" : "false"); });
  var fn = U.screens[name] || U.screens["fund-room"];
  screen.innerHTML = '<div class="wrap">' + U.banner() + '<p class="note">Loading…</p></div>';
  Promise.resolve(fn(parts.slice(1))).then(function (html) {
    if (typeof html === "string") screen.innerHTML = '<div class="wrap">' + U.banner() + html + '</div>';
    if (U.after) { var a = U.after; U.after = null; a(); }
  }).catch(function (e) { screen.innerHTML = '<div class="wrap"><div class="stale-banner">Could not load: ' + esc(e.message) + '</div></div>'; });
}
U.route = route;
window.addEventListener("hashchange", route);

/* ---------- top bar (§16.2), refreshed every 10 s ---------- */
function topbar() {
  U.get("/v1/topbar").then(function (t) {
    document.getElementById("clock").textContent = "paper session · " + t.clock.replace("+00:00", "Z");
    var nv = t.nav.status === "reconciled" ? "good" : t.nav.status === "divergent" ? "crit" : "warn";
    document.getElementById("chips").innerHTML =
      '<span class="chip"><span class="dot" style="color:var(--muted)"></span>' + esc(t.mode) + '</span>' +
      '<span class="chip ' + (t.engine_state === "RUNNING" ? "good" : "warn") + '"><span class="dot"></span>' + esc(t.engine_state) + '</span>' +
      '<span class="chip strat">' + esc(t.strategy_chip) + '</span>' +
      (t.conflicts ? '<span class="chip crit"><span class="dot"></span>' + esc(t.conflicts) + ' conflicted</span>' : '') +
      '<span class="chip' + (t.stale ? ' warn' : '') + '"><span class="dot"></span>' + esc(t.stale) + ' stale</span>' +
      '<a class="chip' + (t.open_incidents ? ' crit' : '') + '" href="#/incidents">' + esc(t.open_incidents) + ' incidents</a>' +
      '<span class="chip ' + nv + '">' + esc(t.nav.text) + '</span>' +
      '<span class="chip fixture">FIXTURE</span>';
  }).catch(function () { document.getElementById("chips").innerHTML = '<span class="chip warn">BFF unreachable</span>'; });
}
setInterval(topbar, 10000);

/* ---------- "why not trading" within one tap from anywhere ---------- */
var whyBtn = document.getElementById("whyBtn"), why = document.getElementById("why");
whyBtn.addEventListener("click", function () {
  var open = why.hasAttribute("hidden");
  whyBtn.setAttribute("aria-expanded", open ? "true" : "false");
  if (!open) { why.setAttribute("hidden", ""); return; }
  why.removeAttribute("hidden"); why.innerHTML = '<p class="note">Loading…</p>';
  U.get("/v1/fund-room").then(function (d) { why.innerHTML = '<h2>Why not trading</h2>' + U.whyLadder(d.why_not_trading); });
});
U.whyLadder = function (rows) {
  return rows.map(function (w) {
    if (w.in_position) return '<h3 style="margin-top:12px">' + esc(w.pair) + '</h3><div class="note">In position: exits and stops are evaluated, not entries.</div>';
    return '<h3 style="margin-top:12px">' + esc(w.pair) + ' <span class="gate ' + (w.binding_gate ? "block" : "pass") + '">' + esc(w.binding_gate || "ELIGIBLE") +
      '</span></h3><div class="note">Gate ladder persisted at ' + esc(w.bar_close) + '</div>' +
      '<details><summary class="note">' + esc(w.ladder.filter(function (r) { return r.passed; }).length) + ' of ' + esc(w.ladder.length) + ' gates pass · show ladder</summary>' +
      U.table(["Gate", "Result", "Value", "Limit"], w.ladder.map(function (r) {
        var res = r.passed ? '<span class="pass">✓ pass</span>' : r.gate === w.binding_gate ? '<span class="fail">✗ binding</span>' : '<span class="fail">✗ fail</span>';
        return [esc(r.gate), res, esc(r.value), esc(r.limit)];
      }), "Gate ladder " + w.pair) + '</details>';
  }).join("");
};

/* ---------- theme (per-viewer convenience only) ---------- */
function setTheme(t) { if (t) document.documentElement.setAttribute("data-theme", t); else document.documentElement.removeAttribute("data-theme"); }
try { setTheme(localStorage.getItem("uchfe-theme")); } catch (e) { /* storage unavailable */ }
document.getElementById("themeBtn").addEventListener("click", function () {
  var cur = document.documentElement.getAttribute("data-theme") ||
    (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"), nxt = cur === "dark" ? "light" : "dark";
  setTheme(nxt); try { localStorage.setItem("uchfe-theme", nxt); } catch (e) { /* ignore */ }
});

/* ---------- Reporter side panel (§16.5) ---------- */
var rep = document.getElementById("reporter");
document.getElementById("repHead").addEventListener("click", function () { rep.classList.toggle("closed"); });
document.getElementById("repAsk").addEventListener("click", function () {
  var q = document.getElementById("repQ").value.trim(), out = document.getElementById("repOut");
  if (!q) return;
  out.innerHTML = '<p class="note">Retrieving…</p>';
  ask(q).then(function (a) {
    out.innerHTML = '<div class="answer st-hatched-muted"><div class="lbl">' + (a.refused ? "Refused · " + esc(a.refusal) : "REPORTED · not evidence") + '</div>' +
      '<p>' + esc(a.answer) + '</p>' + (a.citations.length ? '<div class="note">Cites: ' + a.citations.map(esc).join(", ") + '</div>' : '') +
      (a.stripped.length ? '<div class="warnbox">' + esc(a.stripped.length) + ' ungrounded number(s) removed; RECON_BREAK logged against the reporter.</div>' : '') + '</div>';
  });
});

/* ================= screens (Activity lives in activity.js) ================= */
var S = U.screens;

S["fund-room"] = function () {
  return U.get("/v1/fund-room").then(function (d) {
    var b = d.tier_banner;
    var h = '<div class="banner" role="note"><b>' + esc(b.tier + " " + b.name) + '</b><span>' + esc(b.text) + '</span></div>' +
      (b.micro_notice ? '<div class="stale-banner" role="alert">' + esc(b.micro_notice) + '</div>' : '');
    h += '<div class="sec grid4">' +
      U.panel("NAV", '<div class="big">' + fig(d.nav, {chip: true}) + '</div><div class="note">' + esc(d.nav.verification ? d.nav.verification.text : "") + '</div>') +
      U.panel("High-water mark", fig(d.hwm)) + U.panel("Drawdown", fig(d.drawdown)) +
      U.panel("Mode · engine", esc(d.mode) + " · " + esc(d.engine_state) + '<div class="note">' + esc(d.start_stop.text) + '</div>') + '</div>';
    h += '<div class="sec grid2"><div>' + U.panel("Net P&L", U.table(["Period", "Net"], d.pnl.map(function (p) { return [esc(p.period), fig(p.value)]; }), "Net P&L")) +
      '<div class="gap"></div>' + U.panel("Open positions", d.positions.length ? U.table(["Pair", "Sleeve", "Size", "R at risk", "Stop distance", "Protection", "m applied?"],
      d.positions.map(function (p) { return [esc(p.pair), esc(p.sleeve), fig(p.size), fig(p.r_at_risk), fig(p.stop_distance), esc(p.protection), esc(p.m_applied)]; }), "Open positions")
      : '<p class="note">No open positions.</p>') + '<div class="gap"></div>' +
      U.panel("Why not trading (per pair)", U.whyLadder(d.why_not_trading)) + '</div><div>' +
      U.panel("Loss ladder", U.ladder(d.loss_ladder), {fixture: false}) + '<div class="gap"></div>' +
      U.panel("Next rung", esc(d.next_rung.rung || "none") + ": " + esc(d.next_rung.consequence)) + '<div class="gap"></div>' +
      U.panel("Strategy pulse", U.kv([["Admissible pairs", fig(d.pulse.admissible)], ["Pairs with B > 0", fig(d.pulse.b_positive)],
        ["Cash", fig(d.pulse.cash)], ["Cost-gate passes", fig(d.pulse.cost_gate_passes)]].concat(d.pulse.T.map(function (t) { return ["T " + t.pair, fig(t)]; })))) +
      '<div class="gap"></div>' + U.panel("Abstentions per cycle", U.spark(d.abstention_sparkline.series), {note: d.abstention_sparkline.label}) +
      '<div class="gap"></div>' + U.panel("Incidents", d.incidents.length ? U.table(["Code", "Severity", "Scope"], d.incidents.map(function (i) { return [esc(i.code), esc(i.severity), esc(i.scope)]; })) : '<p class="note">No open incidents.</p>', {fixture: false}) +
      '</div></div>';
    return h;
  });
};
U.spark = function (series) {
  if (!series.length) return '<p class="note">No data.</p>';
  var w = 300, h = 40, mx = Math.max.apply(null, series.concat([1])), p = "";
  series.forEach(function (v, i) { p += (p ? "L" : "M") + (i / Math.max(1, series.length - 1) * w) + " " + (h - 2 - v / mx * (h - 4)); });
  return '<svg viewBox="0 0 ' + w + ' ' + h + '" role="img" aria-label="sparkline"><path d="' + p + '" fill="none" stroke="var(--c-abst)" stroke-width="1.5"/></svg>';
};

S.markets = function (args) {
  var pair = args[0] || "BTC", tf = (location.hash.split("tf=")[1] || "4h");
  return U.get("/v1/markets/" + pair + "?bars=360&tf=" + tf).then(function (d) {
    var sel = '<div class="seg" role="group" aria-label="Pair">' + d.compare.map(function (p) {
      var id = p.split("/")[0]; return '<a href="#/markets/' + id + '" aria-pressed="' + (d.pair === p) + '">' + esc(p) + '</a>';
    }).join("") + '</div> <div class="seg" role="group" aria-label="Timeframe">' + ["4h", "1d", "1h"].map(function (t) {
      if (t === "1h") return '<span class="off" title="' + esc(d.timeframes["1h"]) + '">1h (no data)</span>';
      return '<a href="#/markets/' + d.pair.split("/")[0] + '?tf=' + t + '" aria-pressed="' + (d.timeframe === t) + '">' + t + (t === "4h" ? " (decision)" : "") + '</a>';
    }).join("") + '</div>';
    U.after = function () { if (U.drawPrice) U.drawPrice(document.getElementById("mprice"), d, {markets: true}); };
    return U.sec("Markets · " + d.pair, "4h is the decision timeframe. Venue " + d.venue.venue + ".", '<div class="ctrl-row">' + sel + " " + fig(d.venue.data_age, {chip: true}) + '</div>' +
      '<div class="grid2" style="margin-top:12px"><div class="panel fixture"><div class="scroll-x"><div id="mprice" style="min-width:600px"></div></div></div>' +
      '<aside class="panel pad">' + U.rail(d.rail) + '</aside></div>' +
      U.panel("Funding", esc(d.funding.text)) + U.panel("Volume, OI, dispersion, spread at size", '<p class="note">MISSING: the fixture has no volume, open-interest or book data. These panels fill from certified venue data.</p>'));
  });
};

U.rail = function (r) {
  var h = r.stream && r.stream.state === "STALE" ? '<div class="stale-banner">STALE · last good ' + esc(r.stream.last_good) + '</div>' : '';
  h += '<dl class="kv">' + r.figures.map(function (f) { return '<dt>' + esc(f.label) + '</dt><dd>' + fig(f) + '</dd>'; }).join("") +
    '<dt>Next evaluation</dt><dd class="mono">' + esc(r.next_evaluation.replace("+00:00", "Z")) + '</dd></dl>';
  h += '<div class="lbl" style="margin-top:14px">Episodes in view</div>' + (r.episodes.length ? r.episodes.map(function (e) {
    return '<div class="mono" style="font-size:12px">' + esc(e.entry.slice(5, 10)) + " → " + esc(e.exit.slice(5, 10)) + " " + fig(e.R) + '</div>';
  }).join("") : '<div class="note">none closed</div>');
  return h;
};

S.strategy = function (args) {
  if (args[0] === "router") return S.router();
  return U.get("/v1/strategy").then(function (d) {
    var tabs = '<div class="seg"><a href="#/strategy" aria-pressed="true">Strategy</a><a href="#/strategy/router" aria-pressed="false">Router</a></div>';
    var cards = '<div class="cards">' + d.sleeves.map(function (s) {
      return U.panel(s.sleeve, U.kv([["Tier status", esc(s.status)], ["Validation", esc(s.validation)], ["Risk budget used", fig(s.risk_used)]]));
    }).join("") + '</div>';
    var rank = U.table(["Pair", "T", "B", "M", "Z", "m (regime claim)", "cost_R Kraken (paper)", "Binance", "Bybit"], d.ranking.map(function (r) {
      return [esc(r.pair), fig(r.T), fig(r.decomposition.B), fig(r.decomposition.M), fig(r.decomposition.Z), esc(r.m_regime) + ' <span class="muted">' + esc(r.m_claim_id) + '</span>',
        fig(r.cost_R["kraken (paper sim)"]), esc(r.cost_R.binance), esc(r.cost_R.bybit)];
    }), "Pair ranking");
    var e = d.expectancy;
    var exp = U.kv([["Win rate p", fig(e.p)], ["W*", fig(e.W_star)], ["Average R", fig(e.avg_R)], ["n_eff", esc(e.n_eff_note)]]);
    var params = U.table(["Parameter", "Value", "Class"], d.parameters.map(function (p) { return [esc(p.param), esc(p.value), esc(p["class"])]; }), "Parameter register");
    return '<div class="ctrl-row">' + tabs + '</div>' + U.sec("Sleeves", "", cards) + U.sec("Daily pair ranking and cost gate", "Ranking m equals the regime claim's m for the same bar.", U.panel("Ranking", rank)) +
      '<div class="grid2 even sec">' + U.panel("Expectancy tracker", exp) + U.panel("Component ablation", U.verdict(d.ablation.verdict) + ' <span class="muted">' + esc(d.ablation.run_id) + '</span>') + '</div>' +
      U.sec("Parameter register (read-only)", d.propose.text + " " + d.propose.route, U.panel("Parameters", params, {fixture: false}));
  });
};
S.router = function () {
  return U.get("/v1/strategy/router").then(function (d) {
    var tabs = '<div class="seg"><a href="#/strategy" aria-pressed="false">Strategy</a><a href="#/strategy/router" aria-pressed="true">Router</a></div>';
    var lad = d.ladder.map(function (t) {
      return U.panel(t.tier + " " + t.name + " · floor " + t.nav_floor, U.table(["Gate", "Result", "Detail"], t.gates.map(function (g) {
        return [esc(g.gate), g.passed ? '<span class="pass">✓ pass</span>' : '<span class="fail">✗ fail</span>', esc(g.detail)];
      })), {note: t.binding ? "binding gate: " + t.binding : "all gates pass"});
    }).join('<div class="gap"></div>');
    return '<div class="ctrl-row">' + tabs + '</div>' + U.sec("Tier ladder", "User-selected " + d.user_selected + " · eligible " + d.eligible + " · active " + d.active +
      " · paper tier " + d.paper_tier, lad) + '<div class="grid2 even sec">' + U.panel("Allocator", U.kv([["State", esc(d.allocator.state)], ["Dwell days", esc(d.allocator.dwell_days)],
      ["Changes left this year", esc(d.allocator.changes_left_this_year)]])) + U.panel("Request tier", esc(d.request_tier.text) + '<p><a href="#/governance">Open Governance</a></p>') + '</div>';
  });
};

S.probability = function (args) {
  var pair = args[0] || "BTC";
  return U.get("/v1/probability/" + pair).then(function (d) {
    if (d.abstain) return U.panel("Probability", "ABSTAIN: " + esc(d.abstain));
    var badge = '<span class="chip ' + (d.authority === "T0" ? "warn" : "good") + '">' + esc(d.authority_badge) + '</span>';
    var sel = '<div class="seg">' + ["BTC", "XRP", "ETH", "SOL"].map(function (p) { return '<a href="#/probability/' + p + '" aria-pressed="' + (d.pair.indexOf(p) === 0) + '">' + p + '</a>'; }).join("") + '</div>';
    var nxt = U.table(["Next state", "Probability (90% interval)"], d.next.map(function (f) { return [esc(f.label), fig(f)]; }), "Next-state probabilities");
    var mat = U.table(["from \\ to"].concat(d.matrix.states).concat(["ESS"]), d.matrix.states.map(function (s, i) {
      return ['<b>' + esc(s) + '</b>'].concat(d.matrix.mean[i].map(function (v) { return '<span style="opacity:' + (0.35 + 0.65 * Math.min(1, d.matrix.ess[i] / 30)) + '">' + esc(v) + '</span>'; })).concat([esc(d.matrix.ess[i])]);
    }), "Transition matrix");
    return '<div class="ctrl-row">' + sel + badge + '</div>' + U.sec(d.pair + " · confirmed state " + d.state, "Probability the next 4h state is each value. Claim " + d.claim_id + ".",
      '<div class="grid2 even">' + U.panel("Next-state probabilities", nxt) + U.panel("Transition matrix (posterior mean, shaded by ESS)", mat) + '</div>' +
      '<div class="grid3 sec">' + U.panel("m_regime", fig(d.m) + '<div class="note">' + esc(d.m_note) + '</div><div class="note">binding: ' + esc(d.binding_reasons.join(", ") || "none") + '</div>') +
      U.panel("Stationary π", U.kv(Object.keys(d.stationary).map(function (k) { return [k, esc(d.stationary[k])]; }))) +
      U.panel("Horizon", '<label class="lbl" for="kstar">k* = ' + esc(d.k_star) + ' bars</label><input id="kstar" type="range" min="1" max="' + d.k_slider.max + '" value="1" style="width:100%">' +
        '<div class="note">Disabled beyond k*.</div>') + '</div>' +
      '<div class="grid2 even">' + U.panel("Calibration", esc(d.calibration.text)) + U.panel("Contexts M1–M7", U.table(["Context", "Eligible", "Weight"], d.contexts.map(function (c) { return [esc(c.id), esc(c.eligible), esc(c.weight)]; }))) + '</div>' +
      U.panel("What invalidates this", esc(d.invalidates)));
  });
};

S.pnl = function () {
  return U.get("/v1/pnl").then(function (d) {
    var at = d.attribution;
    return U.sec("P&L & Accounting", "Net, never gross alone. Dual-path verified.", '<div class="grid3">' + U.panel("Net P&L", fig(d.net[0], {chip: true})) + U.panel("TWR", fig(d.twr)) + U.panel("IRR", fig(d.irr)) + '</div>') +
      U.sec("Equity curve", "Drawdown rungs from §7.3 drawn on the curve.", '<div class="panel fixture pad"><div id="eqc"></div></div>') +
      '<div class="grid2 even sec">' + U.panel("Cost stack (hurdle " + at.net + ")", U.table(["Item", "USD"], d.cost_stack.map(function (c) { return [esc(c.item), fig(c.value)]; })) + '<div class="note">Hurdle ' + fig(d.cost_hurdle) + '</div>') +
      U.panel("Attribution: components (sums to net " + esc(at.net) + (at.sums_to_net ? " ✓" : " ⚠") + ")", U.table(["Component", "USD"], Object.keys(at.components).map(function (k) { return [esc(k), fig(at.components[k])]; }).concat([["Funding (A)", fig(at.funding_row)]]))) + '</div>' +
      '<div class="grid2 even">' + U.panel("Attribution: process view", U.table(["Process", "USD"], Object.keys(at.process).map(function (k) { return [esc(k), fig(at.process[k])]; }))) +
      U.panel("Ledger", U.kv([["Entries", esc(d.ledger.entries)], ["Hash chain", d.ledger.chain_ok ? '<span class="pass">✓ intact</span>' : '<span class="fail">✗ LEDGER_CHAIN_BREAK</span>'], ["Head", '<span class="mono">' + esc(d.ledger.head.slice(0, 16)) + '…</span>']])) + '</div>' +
      '<div class="grid2 even sec">' + U.panel("Tax (UK)", U.kv([["Rule version", fig(d.tax.rule_version)], ["Report currency", esc(d.tax.report_currency)]]) + '<div class="note">' + esc(d.tax.note) + '</div>') +
      U.panel("Profit allocation", esc(d.profit_allocation.text)) + '</div>' + (function () { U.after = function () { U.drawEquity(document.getElementById("eqc"), d.equity); }; return ""; })();
  });
};
U.drawEquity = function (host, e) {
  if (!host || !e.nav.length) return;
  var W = 900, H = 220, X0 = 60, X1 = W - 10, lo = Math.min.apply(null, e.nav), hi = Math.max.apply(null, e.nav), p = "";
  var x = function (i) { return X0 + i / Math.max(1, e.nav.length - 1) * (X1 - X0); }, y = function (v) { return 10 + (hi - v) / Math.max(1e-9, hi - lo) * (H - 30); };
  e.nav.forEach(function (v, i) { p += (p ? "L" : "M") + x(i) + " " + y(v); });
  host.innerHTML = '<svg viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="Equity curve"><path d="' + p + '" fill="none" stroke="var(--accent)" stroke-width="1.5"/>' +
    '<text x="' + X0 + '" y="' + (H - 4) + '">' + esc(e.time[0].slice(0, 10)) + '</text><text x="' + X1 + '" y="' + (H - 4) + '" text-anchor="end">' + esc(e.time[e.time.length - 1].slice(0, 10)) + '</text></svg>';
};

S.outcomes = function () {
  return U.get("/v1/outcomes").then(function (d) {
    var pr = d.projection, nu = d.null_edge;
    var proj = pr ? U.table(["Scenario", "P(loss, 1y)", "P(DD ≥ 20%, 1y)", "DD at risk (95%)"], [["Replay returns (bootstrap)", fig(pr.p_loss), fig(pr.p_ruin), fig(pr.dd_at_risk_usd)],
      ["Null edge (always drawn)", fig(nu.p_loss), fig(nu.p_ruin), fig(nu.dd_at_risk_usd)]]) : '<p class="note">Not enough returns.</p>';
    var st = U.table(["Scenario", "Kind", "Loss", "Rungs hit", "Time to flatten", "Verdict"], d.stress.scenarios.map(function (x) {
      return [esc(x.id + " " + x.name), esc(x.kind), esc(x.loss_display), esc(x.rungs_hit.join(", ") || "—"), esc(x.time_to_flatten_min + " min"), U.verdict(x.verdict)];
    }));
    return U.sec("Outcomes", d.projection_note, U.panel("Projection with the null-edge scenario", proj)) +
      U.sec("Stress battery §7.8", "run " + d.stress.run_id + " · " + d.stress.verdict, U.panel("Scenarios", st)) +
      '<div class="grid2 even sec">' + U.panel("Episode playback", '<p class="note">Playback is masked on the server: the response for a cursor holds nothing after it.</p><p><a href="#/activity">Replay a cycle on Activity</a></p>') +
      U.panel("Counterfactual lab", esc(d.counterfactual.text)) + '</div>';
  });
};

S.validation = function () {
  return U.get("/v1/validation").then(function (d) {
    return d.sleeves.map(function (sl) {
      var mat = '<div class="ctrl-row">' + sl.maturity.map(function (m) { return '<span class="chip' + (m === sl.current ? " strat" : "") + '">' + esc(m) + '</span>'; }).join("→") + '</div>';
      var steps = sl.steps.length ? U.table(["Step", "Verdict", "run_id", "Deciding metric", "Value", "CI", "Falsification rule", "Gates SHADOW"], sl.steps.map(function (s) {
        return [esc(s.step), '<span class="ico ' + ({PASS: "pass", FAIL: "fail"}[s.verdict.value] || "notrun") + '">' + ({PASS: "✓", FAIL: "✗"}[s.verdict.value] || "○") + '</span>' + fig(s.verdict), '<span class="mono">' + esc(s.run_id) + '</span>', esc(s.metric), esc(s.value), esc(s.ci), esc(s.falsification), s.gates_shadow ? "yes" : "no (regime T0→T1 only)"];
      }), "Validation ladder") : '<p class="note">No steps run for this sleeve.</p>';
      return U.sec("Validation · " + sl.sleeve, sl.distance + " · allowed mode " + sl.allowed_mode, U.panel("Maturity", mat) + '<div class="gap"></div>' + U.panel("§9.3 ladder", steps));
    }).join("") + U.panel("Promotion", esc(d.promotion.reason), {fixture: false}) + '<div class="gap"></div>' +
      U.panel("Shadow record (P6) · " + esc(d.shadow.status), d.shadow.rungs.length ? U.table(["Rung", "Full days", "Cycles", "Recon", "Cost divergence", "H1/D1", "Class"], d.shadow.rungs.map(function (r) {
        return [esc(r.rung), esc(r.days), esc(r.cycles), esc(r.recon), esc(r.cost_divergence), esc(r.h1_d1), esc(r["class"])];
      }), "Shadow record") + '<p class="note">Days counted toward G2: ' + esc(d.shadow.g2_days) + ' of 90.</p>' : '<p class="note">' + esc(d.shadow.how) + '</p>', {fixture: false});
  });
};

S.data = function () {
  return U.get("/v1/data").then(function (d) {
    return U.sec("Feed status", "Freshness vs TTL, gaps, quarantine, certification.", U.panel("Feeds", U.table(["Venue", "Dataset", "Freshness", "Gaps", "Quarantine", "Certified", "Clock skew"], d.feeds.map(function (f) {
      return [esc(f.venue), esc(f.dataset), fig(f.freshness), esc(f.gaps), esc(f.quarantine), f.certified ? "✓" : "✗ certified: false", esc(f.clock_skew_ms)];
    })))) + '<div class="grid2 even sec">' + U.panel("Dataset builds", U.table(["Dataset", "Hash", "Quality report", "Certifiable"], d.builds.map(function (b) {
      return [esc(b.dataset), '<span class="mono">' + esc(b.hash.slice(0, 16)) + '…</span>', esc(b.quality_report || "none"), esc(b.certifiable) + '<div class="note">' + esc(b.reason) + '</div>'];
    }))) + U.panel("Coverage and lineage", U.kv([["Bars", esc(d.coverage.bars) + " (" + esc(d.coverage.days) + " days from " + esc(d.coverage.from.slice(0, 10)) + ")"], ["Consistent with 4h grid", esc(d.coverage.consistent)],
      ["Lineage root", esc(d.lineage.root)], ["Zero LLM ancestry", d.lineage.zero_llm_ancestry ? "✓" : "✗"], ["Drift monitors", esc(d.drift.status + " (" + d.drift["class"] + ")")]])) + '</div>' +
      U.panel("Drift monitors (PSI) · " + esc(d.drift.status), U.table(["Pair", "Feature", "PSI", "Status", "Detail"], d.drift.rows.map(function (r) {
        return [esc(r.pair), esc(r.feature), esc(r.psi), '<span class="' + ({OK: "pass", FAIL: "fail"}[r.status] || "") + '">' + esc(r.status) + '</span>', esc(r.detail)];
      }), "Drift monitors") + '<p class="note">' + esc(d.drift.reason) + '</p>') + '<div class="gap"></div>' +
      U.panel("Admissibility (service C1)", U.table(["Pair", "Admissible", "Binding reason", "Not yet checked"], d.admissibility.rows.map(function (a) {
        return [esc(a.pair), a.admissible ? "✓" : "✗", esc(a.binding_reason || "—"), esc(a.unchecked.join(", ") || "—")];
      }), "Admissibility") + '<p class="note">' + esc(d.admissibility.note) + '</p>' +
        '<p class="note">Blackout calendar: ' + (d.admissibility.blackout.length ? d.admissibility.blackout.map(function (w) { return esc(w.pairs.join(",") + " " + w.start + " → " + w.end + " (" + w.reason + ")"); }).join("; ") : "no windows declared (policy/blackout.yaml)") + '</p>');
  });
};

S.bots = function () {
  return U.get("/v1/bots").then(function (d) {
    var em = d.emissions;
    return U.sec("Agent authority matrix", "From §13.7. No Apply control exists anywhere.", U.panel("Authority", U.table(["Agent", "Authority", "May emit", "May never"], d.authority_matrix.map(function (a) {
      return [esc(a.agent), esc(a.authority), esc(a.may_emit), esc(a.never)];
    }), "Authority matrix"), {fixture: false})) + U.sec("Emissions, 30 days", "Same series as Activity P7. ABSTAIN is never merged.", '<div class="panel pad fixture"><div id="bemis"></div></div>') +
      '<div class="grid2 even sec">' + U.panel("Model registry", U.table(["Model", "Version", "Authority", "Rule"], d.model_registry.map(function (m) { return [esc(m.model), esc(m.version), esc(m.authority), esc(m.state_rule)]; }))) +
      U.panel("Modules vs active policy hash", U.table(["Module", "Status"], d.modules.map(function (m) { return [esc(m.module), m.conflicted ? '<span class="chip crit">CONFLICTED</span>' : '<span class="pass">✓ matches</span>']; }))) + '</div>' +
      '<div class="grid3">' + U.panel("Calibration", d.calibration.map(function (c) { return esc(c.model + ": " + c.text + (c.authoritative ? "" : " · non-authoritative")); }).join("<br>")) +
      U.panel("Verifier and watchdog", U.kv([["Heartbeat", esc(d.verifier.heartbeat)], ["Last kill-verifier drill", d.verifier.last_kill_drill.passed ? '<span class="pass">✓ entries blocked</span>' : '<span class="fail">✗</span>']])) +
      U.panel("Hypothesis budget", esc(d.hypothesis_budget.used) + " of " + esc(d.hypothesis_budget.budget) + " used in " + esc(d.hypothesis_budget.year)) + '</div>' +
      U.sec("Learning outcomes", d.learning.rule, U.panel("Trade episodes", U.kv([["Closed trades recorded", esc(d.learning.episodes.count + " (" + d.learning.episodes["class"] + ")")],
        ["Excluded from training", esc(d.learning.episodes.excluded)], ["Training set (OBSERVED)", esc(d.learning.episodes.training_set)],
        ["Process-error rate", esc(d.learning.episodes.process_error_rate)], ["Cost divergence", esc(d.learning.episodes.cost_divergence)],
        ["Learner budget", d.learning.halted ? '<span class="fail">halted: ' + esc(d.learning.halted) + '</span>' : "open"]])) + '<div class="gap"></div>' +
        U.panel("Learner proposals vs live parameters", U.table(["Parameter", "Live", "Proposed", "Verdict", "Evidence", "Applies"], d.learning.proposals.map(function (x) {
          return [esc(x.parameter), esc(x.live), esc(x.proposed), esc(x.verdict) + '<div class="note">' + esc(x.detail) + '</div>', esc(x.evidence), x.applies ? "yes" : "no"];
        }), "Learner proposals"))) +
      U.panel("Instruction log", d.instruction_log.length ? U.table(["At", "Question", "Refused"], d.instruction_log.map(function (x) { return [esc(x.at), esc(x.question), x.refused ? "refused" : "answered"]; })) : '<p class="note">No reporter instructions yet.</p>', {fixture: false}) +
      (function () { U.after = function () { if (U.drawEmissions) U.drawEmissions(document.getElementById("bemis"), em); }; return ""; })();
  });
};

S.risk = function () {
  return U.get("/v1/risk").then(function (d) {
    return U.sec("Loss ladder §7.3", "Single source: the Activity screen reads the same numbers.", U.panel("Loss ladder", U.ladder(d.loss_ladder), {fixture: false})) +
      '<div class="grid2 even sec">' + U.panel("Exposure", U.table(["Limit", "Now", "Limit", "At breach"], d.exposure.map(function (e) { return [esc(e.label), fig(e.value), fig(e.limit), esc(e.consequence)]; }))) +
      U.panel("Tail and sizing", U.kv([["ES97.5 multiplier", fig(d.es975)], ["P(DD ≥ 20%, 1y)", fig(d.p_ruin)], ["sigma*", fig(d.sigma_star)], ["Margin invariant", esc(d.margin_invariant.status + ": " + d.margin_invariant.reason)],
        ["ADL", esc(d.adl.status + ": " + d.adl.reason)], ["Circuit breakers", d.circuit_breakers.length ? "" : "none active"]])) + '</div>' +
      '<div class="grid2 even">' + U.panel("FLATTEN preview", U.kv([["Time to flatten", fig(d.flatten_preview.time_to_flatten_min)], ["Ceiling", esc(d.flatten_preview.ceiling_min + " min")]])) +
      U.panel("Kill switches", U.table(["Switch", "Scope", "Where"], d.kill_switches.map(function (k) { return [esc(k.switch), esc(k.scope), esc(k.control)]; })) + '<div class="note">' + esc(d.rearm_rule) + '</div>', {fixture: false}) + '</div>';
  });
};

S.incidents = function () {
  return U.get("/v1/incidents").then(function (d) {
    var row = function (i) { return [esc(i.code), esc(i.severity), esc(i.scope), esc(i.owner), esc(i.deadline || "—"), esc(i.what_system_did), esc(i.detail)]; };
    var hd = ["Code", "Severity", "Scope", "Owner", "Deadline", "What the system did", "Evidence"];
    return U.sec("Open incidents", d.resolution_rule + " There is no dismiss control.", U.panel("Open", d.open.length ? U.table(hd, d.open.map(row)) : '<p class="note">No open incidents.</p>', {fixture: false})) +
      U.sec("Resolved", "", U.panel("Resolved", d.resolved.length ? U.table(hd.concat(["Resolution"]), d.resolved.map(function (i) { return row(i).concat([esc(i.resolution)]); })) : '<p class="note">None.</p>', {fixture: false}));
  });
};

S.governance = function () {
  return U.get("/v1/governance").then(function (d) {
    return U.sec("Policy register", d.coherence_rule, U.panel("Active policy", U.kv([["Version", esc(d.policy.version)], ["Hash", '<span class="mono">' + esc(d.policy.hash.slice(0, 16)) + '…</span>'], ["Status", esc(d.policy.status)],
      ["Signers", d.signers.map(function (s) { return esc(s.signer_id + ": " + s.keys_enrolled + " hardware key(s) enrolled"); }).join("<br>")]]), {fixture: false})) +
      '<div class="grid2 even sec">' + U.panel("Pending proposals", d.proposals.length ? "" : '<p class="note">None.</p>', {fixture: false}) + U.panel("Tier requests and venue access records", '<p class="note">None on file.</p>', {fixture: false}) + '</div>' +
      U.sec("ASSUMED register", "Every ASSUMED fact has an owner and a review date.", U.panel("ASSUMED", U.table(["Id", "Value"], d.assumed.map(function (a) { return [esc(a.id), fig(a)]; })), {fixture: false})) +
      U.sec("Hypothesis registry", "", U.panel("Hypotheses", U.table(["Id", "Status", "Runs"], d.hypotheses.map(function (h) { return [esc(h.id), esc(h.status), esc(h.run_ids.join(", ") || "—")]; })), {fixture: false})) +
      U.panel("Mode ladder and reviews", U.kv([["Modes", esc(d.mode_ladder.join(" → "))], ["External review", esc(d.external_review)], ["On call", esc(d.on_call)]]), {fixture: false}) +
      U.sec("Go-live checklist", "Everything that stands between PAPER and LIVE, and who owns it.", U.panel("Go-live", U.table(["Item", "", "Requirement", "Detail", "Owner"], d.golive.map(function (g) {
        return [esc(g.item), '<span class="ico ' + (g.met ? "pass" : "fail") + '">' + (g.met ? "✓" : "✗") + '</span>', esc(g.label), esc(g.detail), esc(g.owner)];
      }), "Go-live checklist"), {fixture: false}));
  });
};

S.settings = function () {
  return U.get("/v1/settings").then(function (d) {
    return U.sec("Settings", "No trading parameter is editable here.", '<div class="grid2 even">' +
      U.panel("Profile", U.kv([["Principal", esc(d.profile.principal)], ["MFA", esc(d.profile.mfa)]]), {fixture: false}) +
      U.panel("Display", U.kv([["Time", esc(d.display.time)], ["Currency", esc(d.display.currency)], ["Themes", esc(d.display.themes.join(", "))]]), {fixture: false}) + '</div>' +
      '<div class="grid2 even sec">' + U.panel("Notification routing", U.table(["Class", "Route"], d.notifications.map(function (n) { return [esc(n["class"]), esc(n.route)]; })), {fixture: false}) +
      U.panel("Stated limits", U.kv([["Hard drawdown", esc(d.stated_limits.hard_drawdown)]]), {fixture: false}) + '</div>' + '<p><a href="#/exchanges">Settings → Exchanges</a></p>');
  });
};

S.exchanges = function () {
  return U.get("/v1/settings/exchanges").then(function (d) {
    return U.sec("Settings → Exchanges", d.rules.join(" "), U.panel("Connector types", U.table(["Connector", "Venue", "Products", "Environments", "Trade capable", "Trust cap", "Dead-man"], d.connector_types.map(function (c) {
      return [esc(c.connector_type), esc(c.venue), esc(c.products.join(", ")), esc(c.environments.join(", ")), c.trade_capable ? "yes" : "read-only", esc(c.trust_cap), esc(c.dead_man || "—")];
    })), {fixture: false}) + '<div class="gap"></div>' + U.panel("Instances", '<p class="note">No exchange instances yet. ' + esc(d.paper.note) + '</p><p class="note">' + esc(d.add_exchange.route) + '</p>', {fixture: false}));
  });
};

topbar();
window.addEventListener("load", route);  /* after activity.js has registered its screen */
})();
