/* Activity screen (spec §16.8), ported from the engine-activity.html reference mock.
   The mock generated its own data; this port draws only what /v1/activity/* returns. */
(function () {
"use strict";
var U = window.UCHFE, esc = U.esc, fig = U.fig;
var st = {cycle: null, pair: "BTC", playing: null, cycles: null};

function px(n) { return Math.round(n * 10) / 10; }

/* ---------- P1 swimlane ---------- */
var W = 1000, LW = 180, RIGHT = W - 16, TOPH = 30, LH = 30;
function xOf(t, maxS) {
  var split = LW + (RIGHT - LW) * 0.7;
  if (t <= 180) return LW + (t / 180) * (split - LW);
  return split + ((t - 180) / (maxS - 180)) * (RIGHT - split);
}
function mark(cls, x, y, late) {
  var s;
  switch (cls) {
    case "INFORM": s = '<circle cx="' + x + '" cy="' + y + '" r="4.5" fill="var(--c-inform)" stroke="var(--panel)" stroke-width="2"/>'; break;
    case "RECOMMEND": s = '<path d="M' + x + ' ' + (y - 7) + ' L' + (x + 7) + ' ' + y + ' L' + x + ' ' + (y + 7) + ' L' + (x - 7) + ' ' + y + 'Z" fill="var(--c-rec)" stroke="var(--panel)" stroke-width="2"/>'; break;
    case "PROPOSE": s = '<rect x="' + (x - 5.5) + '" y="' + (y - 5.5) + '" width="11" height="11" fill="var(--c-prop)" stroke="var(--panel)" stroke-width="2"/>'; break;
    case "EXECUTE": s = '<path d="M' + x + ' ' + (y - 7) + ' L' + (x + 7) + ' ' + (y + 6) + ' L' + (x - 7) + ' ' + (y + 6) + 'Z" fill="var(--c-exec)" stroke="var(--panel)" stroke-width="2"/>'; break;
    default: s = '<circle cx="' + x + '" cy="' + y + '" r="5" fill="var(--panel)" stroke="var(--c-abst)" stroke-width="2"/>';
  }
  if (late) s += '<path d="M' + (x - 6) + ' ' + (y - 12) + ' L' + (x + 6) + ' ' + (y - 12) + ' L' + x + ' ' + (y - 4) + 'Z" fill="var(--crit)"/><text x="' + (x + 8) + '" y="' + (y - 8) + '" style="fill:var(--crit)">CYCLE_TIMEOUT</text>';
  return s;
}
function drawSwim(d, upTo) {
  var lanes = d.lanes, maxS = d.axis.max_s, H = TOPH + lanes.length * LH + 26;
  var s = '<svg viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="Agent emissions for cycle ' + esc(d.cycle_id) + '" data-events="' + d.events.length + '">';
  lanes.forEach(function (L, i) {
    var y = TOPH + i * LH;
    if (i % 2 === 0) s += '<rect x="0" y="' + y + '" width="' + W + '" height="' + LH + '" fill="var(--panel2)" opacity=".6"/>';
    s += '<text x="12" y="' + (y + 14) + '" class="lane-name"' + (L.active ? '' : ' opacity=".45"') + '>' + esc(L.name) + '</text><text x="12" y="' + (y + 25) + '"' +
      (L.active ? '' : ' opacity=".6"') + '>' + esc(L.tier_label) + '</text>';
    if (!L.active) s += '<line x1="' + LW + '" y1="' + (y + LH / 2) + '" x2="' + RIGHT + '" y2="' + (y + LH / 2) + '" stroke="var(--line)" stroke-dasharray="2 4" data-inactive="' + esc(L.id) + '"/>';
  });
  var split = LW + (RIGHT - LW) * 0.7;
  s += '<path d="M' + (split - 3) + ' ' + (H - 30) + ' l3 -6 l3 6" fill="none" stroke="var(--muted)"/>';  /* axis break */
  d.axis.ticks_s.concat(maxS > 900 ? [maxS] : []).forEach(function (t) {
    var x = xOf(t, maxS);
    s += '<line x1="' + x + '" y1="' + TOPH + '" x2="' + x + '" y2="' + (H - 26) + '" stroke="var(--grid)"/><text x="' + x + '" y="' + (H - 10) + '" text-anchor="middle">' + (t <= 180 ? t + "s" : Math.floor(t / 60) + "m") + '</text>';
  });
  d.deadlines.forEach(function (dl) {
    var t = dl.t_ms / 1000, x = xOf(t, maxS), end = t >= 900;
    s += '<line x1="' + x + '" y1="' + (TOPH - 6) + '" x2="' + x + '" y2="' + (H - 26) + '" stroke="var(--muted)" stroke-dasharray="3 3" opacity=".8"/><text x="' + (end ? x - 4 : x + 4) +
      '" y="' + (TOPH - 10) + '"' + (end ? ' text-anchor="end"' : '') + '>' + esc(dl.label) + '</text>';
  });
  var idx = {}; lanes.forEach(function (L, i) { idx[L.id] = i; });
  var off = {};
  d.events.forEach(function (e, k) {
    if (upTo !== undefined && e.t_s > upTo) return;
    var i = idx[e.agent], key = e.agent + Math.round(xOf(e.t_s, maxS) / 9), n = off[key] = (off[key] || 0) + 1;
    var x = px(xOf(e.t_s, maxS) + (n - 1) * 9), y = TOPH + i * LH + LH / 2;  /* true time, never clamped to a deadline */
    s += '<g class="hit mark" tabindex="0" data-k="' + k + '" data-class="' + e.emission_class + '" data-shape="' + e.shape + '" data-late="' + e.late + '" aria-label="' +
      esc(e.emission_class + " " + e.pair + " " + e.outcome + (e.reason_code ? " · " + e.reason_code : "")) + '"><rect x="' + (x - 9) + '" y="' + (y - 12) + '" width="18" height="24" fill="transparent"/>' +
      mark(e.emission_class, x, y, e.late) + '</g>';
  });
  if (upTo !== undefined) { var cx = xOf(upTo, maxS); s += '<line x1="' + cx + '" y1="' + TOPH + '" x2="' + cx + '" y2="' + (H - 26) + '" stroke="var(--accent)" stroke-width="2"/>'; }
  var host = document.getElementById("swim");
  host.innerHTML = s + '</svg>';
  host.querySelectorAll(".hit").forEach(function (g) {
    var e = d.events[+g.getAttribute("data-k")], L = lanes[idx[e.agent]];
    U.bindTip(g, function () {
      return '<div class="tt">' + esc(L.name) + ' · ' + e.emission_class + '</div>' + esc(e.t_label) + ' · ' + esc(e.pair) + '<br>' + esc(e.outcome) +
        (e.reason_code ? '<br><b>binding:</b> ' + esc(e.reason_code) : '') + '<br><span style="opacity:.7">' + esc(e.ref_kind) + ' ' + esc(e.ref_id) + '</span>';
    });
  });
}
/* ---------- P1a event log: the accessible equivalent, same list as P1 ---------- */
function drawTable(d) {
  var names = {}; d.lanes.forEach(function (L) { names[L.id] = L.name; });
  var f = st.filter || {}, rows = d.events.filter(function (e) {
    if (e.emission_class === "ABSTAIN") return true;  /* ABSTAIN cannot be filtered out of the default view */
    return (!f.agent || e.agent === f.agent) && (!f.cls || e.emission_class === f.cls) && (!f.pair || e.pair === f.pair);
  });
  var s = '<thead><tr><th>Time</th><th>Agent</th><th>Class</th><th>Pair</th><th>Output</th><th>Reason</th><th>Ref</th></tr></thead><tbody>';
  rows.forEach(function (e) {
    s += '<tr data-class="' + e.emission_class + '"><td>' + esc(e.t_label) + (e.late ? ' <span class="fail">CYCLE_TIMEOUT</span>' : '') + '</td><td class="txt">' + esc(names[e.agent]) +
      '</td><td><span class="cls cls-' + e.emission_class + '">' + e.emission_class + '</span></td><td>' + esc(e.pair) + '</td><td class="txt">' + esc(e.outcome) + '</td><td>' +
      esc(e.reason_code || "—") + '</td><td class="mono">' + esc(e.ref_id) + '</td></tr>';
  });
  document.getElementById("evTable").innerHTML = s + '</tbody>';
}
/* ---------- P1b message flow ---------- */
function drawFlow(d) {
  var cnt = {}; d.events.forEach(function (e) { cnt[e.agent] = (cnt[e.agent] || 0) + 1; });
  var abst = {}; d.events.forEach(function (e) { if (e.emission_class === "ABSTAIN") abst[e.agent] = true; });
  var nodes = {data_sentinel: [70, 50, "Data sentinel"], signal_engine: [230, 50, "Signal"], regime: [230, 140, "Regime"], strategy_router: [390, 95, "Router"],
    risk_authority: [540, 95, "Risk"], oms: [690, 50, "OMS"], protection_verifier: [690, 140, "Protection"], verifier: [540, 210, "Verifier"],
    watchdog: [390, 210, "Watchdog"], reporter: [110, 230, "Reporter"]};
  var s = '<svg viewBox="0 0 760 270" role="img" aria-label="Message flow between agents in this cycle"><defs><marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="var(--muted)"/></marker></defs>';
  d.edges.forEach(function (e) {
    var a = nodes[e.from], b = nodes[e.to], dx = b[0] - a[0], dy = b[1] - a[1], len = Math.hypot(dx, dy), ux = dx / len, uy = dy / len;
    var x1 = a[0] + ux * 46, y1 = a[1] + uy * 20, x2 = b[0] - ux * 48, y2 = b[1] - uy * 22, on = e.count > 0;
    s += '<line data-edge="' + e.from + '>' + e.to + '" data-dashed="' + e.dashed + '" x1="' + px(x1) + '" y1="' + px(y1) + '" x2="' + px(x2) + '" y2="' + px(y2) + '" stroke="' + (on ? "var(--ink2)" : "var(--line)") +
      '" stroke-width="' + (on ? 1.6 : 1) + '"' + (e.dashed ? ' stroke-dasharray="4 4"' : '') + ' marker-end="url(#ar)"/>';
    var mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
    s += '<rect x="' + px(mx - 11) + '" y="' + px(my - 9) + '" width="22" height="16" rx="3" fill="var(--panel)" stroke="var(--line)"/><text x="' + px(mx) + '" y="' + px(my + 3) + '" text-anchor="middle" class="t-ink">' + e.count + '</text>';
    if (e.label) s += '<text x="' + px(mx) + '" y="' + px(my + 18) + '" text-anchor="middle">' + esc(e.label) + '</text>';
  });
  var rep = nodes.reporter;
  s += '<text x="' + (rep[0] + 50) + '" y="' + (rep[1] + 16) + '">reads claims · no outbound path</text>';
  Object.keys(nodes).forEach(function (k) {
    var n = nodes[k], c = cnt[k] || 0, ab = abst[k];
    s += '<rect x="' + (n[0] - 46) + '" y="' + (n[1] - 20) + '" width="92" height="40" rx="6" fill="var(--panel2)" stroke="' + (ab ? "var(--c-abst)" : "var(--line)") + '" stroke-width="' + (ab ? 2 : 1) +
      '"/><text x="' + n[0] + '" y="' + (n[1] - 3) + '" text-anchor="middle" class="t-ink" style="font-size:12px;font-weight:600">' + n[2] + '</text><text x="' + n[0] + '" y="' + (n[1] + 12) +
      '" text-anchor="middle">' + c + ' emitted' + (ab ? ' · abstain' : '') + '</text>';
  });
  document.getElementById("flow").innerHTML = s + '</svg>';
}
function staleP1(d) {
  var stale = d.stream && d.stream.state === "STALE";
  var p = document.getElementById("swimPanel");
  if (p) p.classList.toggle("stale", !!stale);
  var b = document.getElementById("swimStale");
  if (b) b.innerHTML = stale ? '<div class="stale-banner" role="alert">STALE · activity stream down · last good ' + esc(d.stream.last_good) + '</div>' : '';
}
function loadCycle(id) {
  st.cycle = id;
  return U.get("/v1/activity/cycles/" + encodeURIComponent(id)).then(function (d) {
    st.cur = d; drawSwim(d); drawTable(d); drawFlow(d); staleP1(d);
    var f = document.getElementById("evFilter");
    if (f && !f.dataset.ready) {
      f.dataset.ready = "1";
      var agents = d.lanes.map(function (L) { return '<option value="' + esc(L.id) + '">' + esc(L.name) + '</option>'; }).join("");
      f.innerHTML = '<select aria-label="Filter agent" data-f="agent"><option value="">all agents</option>' + agents + '</select> ' +
        '<select aria-label="Filter class" data-f="cls"><option value="">all classes</option><option>INFORM</option><option>RECOMMEND</option><option>PROPOSE</option><option>EXECUTE</option></select>' +
        ' <span class="note">ABSTAIN rows always show.</span>';
      f.querySelectorAll("select").forEach(function (sel) {
        sel.addEventListener("change", function () { st.filter = st.filter || {}; st.filter[sel.dataset.f] = sel.value; drawTable(st.cur); });
      });
    }
  });
}
/* Replay: the cursor only moves through events already fetched for a past cycle; for playback the server masks by cursor_ms. */
function replay(btn) {
  if (st.playing) { cancelAnimationFrame(st.playing); st.playing = null; drawSwim(st.cur); btn.textContent = "▶ Replay"; return; }
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) { drawSwim(st.cur); return; }  /* jumps, no animation */
  btn.textContent = "■ Stop";
  var start = performance.now(), dur = 6000, maxS = st.cur.axis.max_s;
  (function step(now) {
    var f = Math.min(1, (now - start) / dur), t = f < 0.7 ? (f / 0.7) * 180 : 180 + ((f - 0.7) / 0.3) * (maxS - 180);
    drawSwim(st.cur, t);
    if (f < 1) st.playing = requestAnimationFrame(step); else { st.playing = null; btn.textContent = "▶ Replay"; drawSwim(st.cur); }
  })(start);
}

/* ---------- P2 pair movement (also used by Markets) ---------- */
U.drawPrice = function (host, d) {
  var b = d.bars, N = b.c.length, PW = 900, X0 = 58, X1 = PW - 12, Y0 = 16, PH = 250, RB = PH + Y0 + 6, RH = 12, TY = RB + RH + 26, TH = 110, H = TY + TH + 28;
  var lo = d.price_axis.lo, hi = d.price_axis.hi, bw = (X1 - X0) / N;
  var x = function (i) { return px(X0 + (i + 0.5) * bw); }, y = function (p) { return px(Y0 + (hi - p) / (hi - lo) * PH); }, ty = function (v) { return px(TY + (1 - v) / 2 * TH); };
  var tIdx = {}; b.time.forEach(function (t, i) { tIdx[t] = i; });
  var s = '<svg viewBox="0 0 ' + PW + ' ' + H + '" role="img" aria-label="' + esc(d.pair) + ' 4h price with Donchian channels, stops, markers, regime ribbon and trend score">';
  d.price_axis.ticks.forEach(function (tk) { var yy = y(tk.v); s += '<line x1="' + X0 + '" y1="' + yy + '" x2="' + X1 + '" y2="' + yy + '" stroke="var(--grid)"/><text x="' + (X0 - 6) + '" y="' + (yy + 3) + '" text-anchor="end">' + esc(tk.label) + '</text>'; });
  b.x_ticks.forEach(function (tk) { s += '<line x1="' + x(tk.i) + '" y1="' + Y0 + '" x2="' + x(tk.i) + '" y2="' + (TY + TH) + '" stroke="var(--grid)"/><text x="' + x(tk.i) + '" y="' + (H - 10) + '" text-anchor="middle">' + esc(tk.label) + '</text>'; });
  function path(arr) { var p = "", pen = false; arr.forEach(function (v, i) { if (v === null || v < lo || v > hi) { pen = false; return; } p += (pen ? "L" : "M") + x(i) + " " + y(v); pen = true; }); return p; }
  var styles = {"30": ["var(--don30)", ""], "90": ["var(--don90)", "4 3"], "180": ["var(--don180)", "1 3"]};
  Object.keys(d.donchian).forEach(function (L) {
    var sty = styles[L] || ["var(--muted)", "2 2"];
    ["hi", "lo"].forEach(function (k) { s += '<path d="' + path(d.donchian[L][k]) + '" fill="none" stroke="' + sty[0] + '" stroke-width="1.5"' + (sty[1] ? ' stroke-dasharray="' + sty[1] + '"' : '') + '/>'; });
  });
  for (var i = 0; i < N; i++) {
    if (b.c[i] === null) continue;  /* a missing bar is a gap, never interpolated */
    var up = b.c[i] >= b.o[i], col = up ? "var(--up)" : "var(--down)", cx = x(i), yt = y(Math.max(b.o[i], b.c[i])), yb = y(Math.min(b.o[i], b.c[i]));
    s += '<line x1="' + cx + '" y1="' + y(b.h[i]) + '" x2="' + cx + '" y2="' + y(b.l[i]) + '" stroke="' + col + '"/><rect x="' + px(cx - bw * 0.32) + '" y="' + yt + '" width="' + px(bw * 0.64) + '" height="' + Math.max(1, px(yb - yt)) + '" fill="' + col + '"/>';
  }
  d.stops.forEach(function (sg) {
    var p = ""; sg.points.forEach(function (pt, k) { var i2 = tIdx[pt[0]]; if (i2 === undefined) return; p += (p ? "L" : "M") + x(i2) + " " + y(pt[1]); });
    s += '<path class="stopline" d="' + p + '" fill="none" stroke="var(--trail)" stroke-width="2"/>';
  });
  d.markers.forEach(function (m) {
    var i3 = tIdx[m.time]; if (i3 === undefined) return;
    var mx = x(i3);
    if (m.kind === "entry") { var ey = y(b.l[i3]) + 12; s += '<path data-marker="entry" d="M' + mx + ' ' + (ey - 6) + ' L' + (mx + 6) + ' ' + (ey + 5) + ' L' + (mx - 6) + ' ' + (ey + 5) + 'Z" fill="var(--accent)" stroke="var(--panel)" stroke-width="1.5"><title>' + esc(m.label) + '</title></path>'; }
    else if (m.kind === "exit") { var xy = y(b.h[i3]) - 12; s += '<path data-marker="exit" d="M' + mx + ' ' + (xy + 6) + ' L' + (mx + 6) + ' ' + (xy - 5) + ' L' + (mx - 6) + ' ' + (xy - 5) + 'Z" fill="var(--ink2)" stroke="var(--panel)" stroke-width="1.5"><title>' + esc(m.label) + '</title></path>'; }
    else { s += '<circle data-marker="declined" cx="' + mx + '" cy="' + (y(b.h[i3]) - 12) + '" r="4.5" fill="var(--panel)" stroke="var(--c-abst)" stroke-width="2"><title>' + esc(m.label) + '</title></circle>'; }
  });
  s += '<text x="' + (X0 - 6) + '" y="' + (RB + RH - 2) + '" text-anchor="end">regime</text>';
  d.regime.forEach(function (r, i4) { if (r) s += '<rect x="' + px(X0 + i4 * bw) + '" y="' + RB + '" width="' + px(bw + 0.4) + '" height="' + RH + '" fill="var(--st-' + r + ')"/>'; });
  var T = d.T;
  s += '<text x="' + X0 + '" y="' + (TY - 8) + '" class="t-ink2">trend score T (ESTIMATED, banded) · stacked B, M, Z · entry at T ≥ ' + esc(T.entry) + ' with B &gt; 0</text>';
  [[1, "+1"], [0.5, "+0.5"], [0, "0"], [-0.5, "-0.5"], [-1, "-1"]].forEach(function (v) {
    s += '<line x1="' + X0 + '" y1="' + ty(v[0]) + '" x2="' + X1 + '" y2="' + ty(v[0]) + '" stroke="' + (v[0] === 0 ? "var(--muted)" : "var(--grid)") + '"' + (Math.abs(v[0]) === 0.5 ? ' stroke-dasharray="4 3"' : '') +
      '/><text x="' + (X0 - 6) + '" y="' + (ty(v[0]) + 3) + '" text-anchor="end">' + v[1] + '</text>';
  });
  var band = "", bandLo = "";
  for (var j = 0; j < N; j++) {
    if (T.T[j] === null) continue;
    var parts = [[T.B[j] / 3, "var(--b)"], [T.M[j] / 3, "var(--m)"], [T.Z[j] / 3, "var(--z)"]], pos = 0, neg = 0, bx = px(x(j) - bw * 0.35), bwid = px(bw * 0.7);
    parts.forEach(function (p) {  /* drawing the server's B, M, Z as stacked bars; T itself comes from the server */
      var v = p[0]; if (v === null || isNaN(v)) return;
      if (v >= 0) { s += '<rect x="' + bx + '" y="' + ty(pos + v) + '" width="' + bwid + '" height="' + px(ty(pos) - ty(pos + v)) + '" fill="' + p[1] + '" opacity=".55"/>'; pos += v; }
      else { s += '<rect x="' + bx + '" y="' + ty(neg) + '" width="' + bwid + '" height="' + px(ty(neg + v) - ty(neg)) + '" fill="' + p[1] + '" opacity=".55"/>'; neg += v; }
    });
    band += (band ? "L" : "M") + x(j) + " " + ty(T.hi[j]);
    bandLo = "L" + x(j) + " " + ty(T.lo[j]) + bandLo;
  }
  if (band) s += '<path class="tband" d="' + band + bandLo + 'Z" fill="var(--ink)" opacity=".12"/>';  /* T is never a bare line */
  var tl = ""; T.T.forEach(function (v, k) { if (v === null) return; tl += (tl ? "L" : "M") + x(k) + " " + ty(v); });
  s += '<path d="' + tl + '" fill="none" stroke="var(--ink)" stroke-width="1.5"/>';
  s += '<line id="xh" x1="0" y1="' + Y0 + '" x2="0" y2="' + (TY + TH) + '" stroke="var(--ink2)" stroke-dasharray="2 3" opacity="0"/><rect id="xhit" x="' + X0 + '" y="' + Y0 + '" width="' + (X1 - X0) + '" height="' + (TY + TH - Y0) + '" fill="transparent"/></svg>';
  host.innerHTML = s;
  var svg = host.querySelector("svg"), hit = svg.querySelector("#xhit"), xh = svg.querySelector("#xh");
  hit.addEventListener("pointermove", function (e) {
    var r = svg.getBoundingClientRect(), sx = (e.clientX - r.left) / r.width * PW, k = Math.max(0, Math.min(N - 1, Math.floor((sx - X0) / bw)));
    xh.setAttribute("x1", x(k)); xh.setAttribute("x2", x(k)); xh.setAttribute("opacity", "1");
    U.showTip('<div class="tt">' + esc(d.pair) + ' · ' + esc(b.time[k].replace("+00:00", "Z")) + '</div>' + b.tip[k].split("|").map(esc).join("<br>"), e.clientX, e.clientY);
  });
  hit.addEventListener("pointerleave", function () { xh.setAttribute("opacity", "0"); U.hideTip(); });
};
function loadPair(p) {
  st.pair = p;
  document.querySelectorAll("#pairSeg button").forEach(function (b) { b.setAttribute("aria-pressed", b.dataset.p === p ? "true" : "false"); });
  return U.get("/v1/activity/pairs/" + p + "?bars=180").then(function (d) {
    U.drawPrice(document.getElementById("price"), d);
    var r = document.getElementById("rail"), stale = d.rail.stream && d.rail.stream.state === "STALE";
    r.classList.toggle("stale", !!stale);
    r.innerHTML = '<div class="lbl">Current bar · ' + esc(d.bars.time[d.bars.time.length - 1].replace("+00:00", "Z")) + '</div><h3 style="margin-top:4px;font-size:18px">' + esc(d.pair) + '</h3>' + U.rail(d.rail);
  });
}

/* ---------- P3 universe cards (policy order, never a leaderboard) ---------- */
function cards(d) {
  return d.cards.map(function (c) {
    var w = 240, h = 64, lo = Math.min.apply(null, c.spark), hi = Math.max.apply(null, c.spark), p = "";
    c.spark.forEach(function (v, i) { p += (p ? "L" : "M") + px(i / (c.spark.length - 1) * w) + " " + px(4 + (hi - v) / Math.max(1e-12, hi - lo) * (h - 8)); });
    return '<article class="panel pad card fixture"><h3><span>' + esc(c.pair) + '</span>' + fig(c.change_30d) + '</h3><svg viewBox="0 0 ' + w + ' ' + h + '" style="margin-top:6px" role="img" aria-label="' + esc(c.pair) +
      ' 30 day closes"><path d="' + p + '" fill="none" stroke="var(--accent)" stroke-width="2"/></svg><div class="row"><span class="muted">T</span>' + fig(c.T) + '</div><div class="row"><span class="state"><i style="background:var(--st-' +
      esc(c.regime) + ')"></i>regime ' + esc(c.regime) + '</span><span class="gate ' + esc(c.gate.kind) + '">' + esc(c.gate.text) + '</span></div></article>';
  }).join("");
}
/* ---------- P4 funnel ---------- */
function funnel(d) {
  var rows = d.steps, total = Math.max(1, rows[0].count), W2 = 560, rowH = 34, H = rows.length * rowH + 6, x0 = 200, x1 = W2 - 110;
  var s = '<svg viewBox="0 0 ' + W2 + ' ' + H + '" role="img" aria-label="Gate funnel" data-sums="' + d.sums + '">';
  rows.forEach(function (r, k) {
    var y = k * rowH + 4, w = Math.max(2, (r.count / total) * (x1 - x0));
    s += '<text x="' + (x0 - 10) + '" y="' + (y + 17) + '" text-anchor="end" class="t-ink2" style="font-size:11.5px">' + esc(r.label) + '</text><rect x="' + x0 + '" y="' + (y + 5) + '" width="' + px(w) + '" height="18" rx="3" fill="var(--accent)" opacity="' +
      (k === rows.length - 1 ? 1 : 0.35 + 0.08 * k) + '"/><text x="' + px(x0 + w + 6) + '" y="' + (y + 18) + '" class="t-ink">' + esc(r.count) + '</text>' +
      (r.lost ? '<text x="' + (W2 - 4) + '" y="' + (y + 18) + '" text-anchor="end">−' + esc(r.lost) + ' ' + esc(r.top_reason) + '</text>' : '');
  });
  return s + '</svg><div class="note">Counts from signal_intent.gate_ladder, ' + esc((d.from || "").slice(0, 10)) + ' to ' + esc((d.to || "").slice(0, 10)) + '. Pairs in position are evaluated for exits, not entries.</div>';
}
/* ---------- P6 timing ---------- */
function latency(d) {
  var S2 = d.steps, W3 = 600, rh = 34, H = S2.length * rh + 30, x0 = 210, x1 = W3 - 20;
  function xs(t) { var sp = x0 + (x1 - x0) * 0.7; return px(t <= 180 ? x0 + t / 180 * (sp - x0) : sp + (Math.min(t, 900) - 180) / 720 * (x1 - sp)); }
  var s = '<svg viewBox="0 0 ' + W3 + ' ' + H + '" role="img" aria-label="Stage timing versus deadline">';
  [0, 60, 120, 180, 600, 900].forEach(function (t) { s += '<line x1="' + xs(t) + '" y1="0" x2="' + xs(t) + '" y2="' + (H - 22) + '" stroke="var(--grid)"/><text x="' + xs(t) + '" y="' + (H - 8) + '" text-anchor="middle">' + (t <= 180 ? t + "s" : t / 60 + "m") + '</text>'; });
  S2.forEach(function (r, k) {
    var y = k * rh + 8, dl = r.deadline_ms / 1000;
    s += '<text x="' + (x0 - 10) + '" y="' + (y + 13) + '" text-anchor="end" class="t-ink2" style="font-size:11.5px">' + esc(r.label) + (r.warn ? " ▲ WARN" : "") + '</text>';
    if (r.p50_ms !== null) s += '<rect x="' + xs(r.p50_ms / 1000) + '" y="' + (y + 4) + '" width="' + Math.max(2, xs(r.p95_ms / 1000) - xs(r.p50_ms / 1000)) + '" height="12" rx="3" fill="' + (r.warn ? "var(--warn)" : "var(--accent)") + '" opacity=".75"><title>' + esc(r.display) + '</title></rect>';
    s += '<line x1="' + xs(dl) + '" y1="' + y + '" x2="' + xs(dl) + '" y2="' + (y + 20) + '" stroke="var(--crit)" stroke-width="2"/>';
  });
  return s + '</svg><div class="note">' + esc(d.clock) + '</div>';
}
/* ---------- P7 emissions (shared with Bots & Models) ---------- */
U.drawEmissions = function (host, d) {
  var cls = [["INFORM", "var(--c-inform)"], ["RECOMMEND", "var(--c-rec)"], ["PROPOSE", "var(--c-prop)"], ["EXECUTE", "var(--c-exec)"], ["ABSTAIN", "var(--c-abst)"]];
  var A = d.agents, W4 = 560, rh = 30, H = A.length * rh + 10, x0 = 150, x1 = W4 - 60, max = 1;
  A.forEach(function (a) { if (a.total > max) max = a.total; });
  var s = '<svg viewBox="0 0 ' + W4 + ' ' + H + '" role="img" aria-label="Emissions by class per agent over 30 days">';
  A.forEach(function (a, k) {
    var y = k * rh + 6, x = x0;
    s += '<text x="' + (x0 - 10) + '" y="' + (y + 14) + '" text-anchor="end" class="t-ink2" style="font-size:11.5px"' + (a.active ? '' : ' opacity=".45"') + '>' + esc(a.name) + '</text>';
    cls.forEach(function (c, ci) {
      var v = a.counts[c[0]]; if (!v) return;
      var w = v / max * (x1 - x0);
      s += '<g class="hit" tabindex="0" data-t="' + esc(a.name + "|" + c[0] + "|" + v) + '"><rect x="' + px(x) + '" y="' + (y + 3) + '" width="' + Math.max(1, px(w - 2)) + '" height="16" ' +
        (ci === 4 ? 'fill="var(--panel)" stroke="' + c[1] + '" stroke-width="1.5"' : 'fill="' + c[1] + '"') + '/></g>';
      x += w;
    });
    s += '<text x="' + px(x + 6) + '" y="' + (y + 15) + '" class="t-ink">' + (a.active ? esc(a.total) : esc(a.tier_label)) + '</text>';
  });
  host.innerHTML = s + '</svg><div class="legend">' + cls.map(function (c, i) { return '<span><span class="sw" style="' + (i === 4 ? 'border:1.5px solid ' + c[1] : 'background:' + c[1]) + '"></span>' + c[0] + '</span>'; }).join("") +
    '</div><div class="note">' + esc(d.completeness) + '.</div>';
  host.querySelectorAll(".hit").forEach(function (g) { var p = g.getAttribute("data-t").split("|"); U.bindTip(g, function () { return '<div class="tt">' + esc(p[0]) + '</div>' + esc(p[1]) + ': ' + esc(p[2]) + ' in 30 days'; }); });
};

/* ---------- screen ---------- */
U.screens.activity = function () {
  return Promise.all([U.get("/v1/activity/cycles?limit=6"), U.get("/v1/activity/universe"), U.get("/v1/activity/funnel"), U.get("/v1/risk"),
    U.get("/v1/activity/timing?days=30"), U.get("/v1/activity/emissions?days=30")]).then(function (r) {
    var cyc = r[0], uni = r[1], fun = r[2], risk = r[3], tim = r[4], emi = r[5];
    st.cycles = cyc.cycles;
    var seg = cyc.cycles.slice().reverse().map(function (c) { return '<button type="button" data-c="' + esc(c.cycle_id) + '" aria-pressed="false">' + esc(c.cycle_id.slice(11, 16)) + (c.late ? " ▲" : "") + '</button>'; }).join("");
    var legend = '<div class="legend" aria-label="Emission classes"><span><svg width="14" height="14" viewBox="0 0 14 14"><circle cx="7" cy="7" r="4" fill="var(--c-inform)"/></svg>INFORM</span>' +
      '<span><svg width="14" height="14" viewBox="0 0 14 14"><path d="M7 1.5 12.5 7 7 12.5 1.5 7Z" fill="var(--c-rec)"/></svg>RECOMMEND</span><span><svg width="14" height="14" viewBox="0 0 14 14"><rect x="2.5" y="2.5" width="9" height="9" fill="var(--c-prop)"/></svg>PROPOSE</span>' +
      '<span><svg width="14" height="14" viewBox="0 0 14 14"><path d="M7 1.5 12.5 12 1.5 12Z" fill="var(--c-exec)"/></svg>EXECUTE</span><span><svg width="14" height="14" viewBox="0 0 14 14"><circle cx="7" cy="7" r="4.5" fill="var(--panel)" stroke="var(--c-abst)" stroke-width="2"/></svg>ABSTAIN (with binding reason)</span>' +
      '<span><svg width="14" height="14" viewBox="0 0 14 14"><path d="M1 2 13 2 7 11Z" fill="var(--crit)"/></svg>CYCLE_TIMEOUT (drawn at its true time)</span></div>';
    var pairs = uni.cards.map(function (c) { var id = c.pair.split("/")[0]; return '<button type="button" data-p="' + id + '" aria-pressed="false">' + esc(c.pair) + '</button>'; }).join("");
    var h = '<section class="sec" aria-labelledby="h-agents"><div class="sec-h"><div><h2 id="h-agents">Agent activity by decision cycle</h2><p>Every agent\'s emissions for one 4h bar close on the cycle\'s own clock. Dashed lines are the §13.4 deadlines. Hover or tap a mark for the record it points at.</p></div>' +
      '<div class="ctrl-row"><div class="seg" id="cycleSeg" role="group" aria-label="Decision cycle">' + seg + '</div><button class="btn" id="playBtn" type="button">▶ Replay</button></div></div>' +
      '<div id="swimStale"></div><div class="panel fixture" id="swimPanel"><div class="scroll-x"><div id="swim" style="min-width:760px"></div></div></div>' + legend +
      '<div class="grid2 even" style="margin-top:14px"><div class="panel"><div class="pad" style="padding-bottom:6px"><h3>Cycle event log</h3><div class="note">Same events as the swimlane. Every row cites the record it points at.</div><div id="evFilter" class="ctrl-row" style="margin-top:6px"></div></div>' +
      '<div class="scroll-x" style="max-height:340px;overflow-y:auto"><table id="evTable" aria-label="Cycle event log"></table></div></div>' +
      '<div class="panel pad"><h3>Message flow</h3><div class="note">Edge labels count messages. A dashed edge carries data its consumer may not act on.</div><div class="scroll-x"><div id="flow" style="min-width:340px;margin-top:8px"></div></div></div></div></section>' +
      '<section class="sec"><div class="sec-h"><div><h2>Pair movement</h2><p>Last 180 4h bars with close-based Donchian channels, entries, exits, declined signals, the active stop, the confirmed regime ribbon and T with its B, M and Z parts.</p></div>' +
      '<div class="ctrl-row"><div class="seg" id="pairSeg" role="group" aria-label="Pair">' + pairs + '</div><a class="btn" id="toMarkets" href="#/markets/BTC">Open in Markets</a></div></div>' +
      '<div class="grid2"><div class="panel fixture"><div class="scroll-x"><div id="price" style="min-width:600px"></div></div></div><aside class="panel pad fixture" id="rail" aria-label="Current bar summary"></aside></div></section>' +
      '<section class="sec"><div class="sec-h"><div><h2>Universe at a glance</h2><p>' + esc(uni.order) + '.</p></div></div><div class="grid4">' + cards(uni) + '</div></section>' +
      '<section class="sec grid2"><div><div class="sec-h"><div><h2>Why the engine did not trade</h2><p>Every evaluation in the window and how many survived each gate in ladder order (§7.1).</p></div></div><div class="panel pad fixture">' + funnel(fun) + '</div></div>' +
      '<div><div class="sec-h"><div><h2>Loss ladder</h2><p>Same numbers as Risk & Limits (§7.3).</p></div></div><div class="panel pad">' + U.ladder(risk.loss_ladder) + '</div></div></section>' +
      '<section class="sec grid2 even"><div><div class="sec-h"><div><h2>Cycle timing vs deadlines</h2><p>Last 30 days. Bar spans p50 to p95; the red tick is the deadline.</p></div></div><div class="panel pad fixture">' + latency(tim) + '</div></div>' +
      '<div><div class="sec-h"><div><h2>Emissions, 30 days</h2><p>Output classes per agent, ABSTAIN included.</p></div></div><div class="panel pad fixture"><div id="emis"></div></div></div></section>';
    U.after = function () {
      document.querySelectorAll("#cycleSeg button").forEach(function (b) {
        b.addEventListener("click", function () {
          document.querySelectorAll("#cycleSeg button").forEach(function (x) { x.setAttribute("aria-pressed", x === b ? "true" : "false"); });
          loadCycle(b.dataset.c);
        });
      });
      var btns = document.querySelectorAll("#cycleSeg button"), last = btns[btns.length - 1];
      if (last) { last.setAttribute("aria-pressed", "true"); loadCycle(last.dataset.c); }
      document.getElementById("playBtn").addEventListener("click", function () { if (st.cur) replay(this); });
      document.querySelectorAll("#pairSeg button").forEach(function (b) {
        b.addEventListener("click", function () { document.getElementById("toMarkets").setAttribute("href", "#/markets/" + b.dataset.p); loadPair(b.dataset.p); });
      });
      loadPair(st.pair);
      U.drawEmissions(document.getElementById("emis"), emi);
    };
    return h;
  });
};
})();
