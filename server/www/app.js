/* OMA//DASH web — hybrid skin.
   Data: /stats (2 s), /history (2 s), /network (25 s), /weather (5 min).
   Charts are hand-drawn on canvas with the same smoothing as the iDBox panel. */

const $ = (s) => document.querySelector(s);
const el = (tag, cls, txt) => { const n = document.createElement(tag); if (cls) n.className = cls; if (txt != null) n.textContent = txt; return n; };
const num = (v, d = 0) => (v == null || isNaN(v)) ? "--" : Number(v).toFixed(d);

function fmtBytes(b) {
  if (b == null) return "--";
  const u = ["B", "K", "M", "G", "T"];
  let i = 0, v = b;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (i ? v.toFixed(i > 1 ? 1 : 0) : v) + u[i];
}
function fmtBps(bps) {
  if (bps == null) return ["--", "B/s"];
  const u = ["B/s", "KB/s", "MB/s", "GB/s"];
  let v = bps, i = 0;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return [v < 10 ? v.toFixed(1) : Math.round(v).toString(), u[i]];
}
function fmtUp(s) {
  s = Math.floor(s || 0);
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return (d ? d + "d " : "") + h + "h " + m + "m";
}
function clock(ts) {
  const d = ts ? new Date(ts * 1000) : new Date();
  return d.toLocaleTimeString("pt-BR", { hour12: false });
}

/* ---------------- canvas chart ---------------- */
const charts = {};
function chart(id) {
  const cv = $(id);
  if (!cv) return null;
  const dpr = window.devicePixelRatio || 1;
  const w = Math.max(1, cv.clientWidth), h = cv.clientHeight || 80;
  if (cv.width !== Math.floor(w * dpr) || cv.height !== Math.floor(h * dpr)) {
    cv.width = Math.floor(w * dpr); cv.height = Math.floor(h * dpr);
  }
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w, h };
}

function smooth(ctx, pts) {
  ctx.beginPath();
  ctx.moveTo(pts[0].x, pts[0].y);
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[i - 1] || pts[i], p1 = pts[i], p2 = pts[i + 1], p3 = pts[i + 2] || p2;
    const c1 = { x: p1.x + (p2.x - p0.x) / 6, y: p1.y + (p2.y - p0.y) / 6 };
    const c2 = { x: p2.x - (p3.x - p1.x) / 6, y: p2.y - (p3.y - p1.y) / 6 };
    ctx.bezierCurveTo(c1.x, c1.y, c2.x, c2.y, p2.x, p2.y);
  }
}

function drawSeries(id, series, opts = {}) {
  const c = chart(id);
  if (!c) return;
  const { ctx, w, h } = c;
  ctx.clearRect(0, 0, w, h);

  const min = opts.min != null ? opts.min : 0;
  const max = opts.max != null ? opts.max : 100;

  // optional left gutter: value scale, so a curve tells you where you are.
  // Under 40 px tall the chart is a sparkline: a scale would be noise.
  const axisOn = !!opts.yAxis && h >= 40;
  const ax = axisOn ? 40 : 0;
  const plotW = w - ax;

  // grid
  ctx.strokeStyle = "rgba(34,224,255,.08)";
  ctx.lineWidth = 1;
  for (let i = 0; i <= 3; i++) {
    const y = Math.round(6 + (h - 12) * i / 3) + 0.5;
    ctx.beginPath(); ctx.moveTo(ax, y); ctx.lineTo(w, y); ctx.stroke();
  }
  if (axisOn) {
    const fmt = opts.fmt || ((v) => String(Math.round(v)));
    ctx.font = "9px ui-monospace,monospace";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    const nTicks = h < 64 ? 2 : 3;   // short charts: max and min only, no overlap
    for (let i = 0; i < nTicks; i++) {
      const v = max - (max - min) * i / (nTicks - 1);
      const y = Math.round(6 + (h - 12) * i / (nTicks - 1));
      ctx.fillStyle = i === 0 ? "rgba(140,166,189,.95)" : "rgba(140,166,189,.82)";
      ctx.fillText(fmt(v), ax - 6, y);
      if (i) {
        ctx.strokeStyle = "rgba(34,224,255,.05)";
        ctx.beginPath(); ctx.moveTo(ax, y + 0.5); ctx.lineTo(w, y + 0.5); ctx.stroke();
      }
    }
  }

  const n = Math.max(...series.map((s) => s.data.length), 2);
  const pad = 6;
  const bp = opts.bottomPad || 0;      /* room for axis labels under the plot */
  series.forEach((s) => {
    if (s.data.length < 2) return;
    const pts = s.data.map((v, i) => ({
      x: ax + (i / (n - 1)) * (plotW - pad),
      y: h - pad - bp - ((Math.max(min, Math.min(max, v)) - min) / (max - min)) * (h - pad * 2 - bp),
    }));

    if (s.fill) {
      smooth(ctx, pts);
      ctx.lineTo(pts[pts.length - 1].x, h - bp); ctx.lineTo(pts[0].x, h - bp); ctx.closePath();
      const g = ctx.createLinearGradient(0, 0, 0, h);
      g.addColorStop(0, s.glow || "rgba(34,224,255,.22)");
      g.addColorStop(1, "rgba(34,224,255,0)");
      ctx.fillStyle = g; ctx.fill();
    }

    smooth(ctx, pts);
    ctx.strokeStyle = s.color;
    ctx.lineWidth = s.width || 1.8;
    ctx.lineJoin = "round"; ctx.lineCap = "round";
    ctx.shadowColor = s.shadow || s.color;
    ctx.shadowBlur = s.blur != null ? s.blur : 7;
    ctx.stroke();
    ctx.shadowBlur = 0;

    const last = pts[pts.length - 1];
    ctx.fillStyle = "#fff";
    ctx.beginPath(); ctx.arc(last.x, last.y, 2.2, 0, Math.PI * 2); ctx.fill();
  });

  if (opts.labels) {
    ctx.font = "10px ui-monospace,monospace";
    ctx.fillStyle = "rgba(140,166,189,.75)";
    ctx.textAlign = "center";
    opts.labels.forEach((lb) => {
      if (!lb.text) return;
      ctx.fillText(lb.text, Math.min(w - 10, Math.max(10, lb.x)), h - 1);
    });
  }
  if (opts.ticks) {
    ctx.strokeStyle = "rgba(34,224,255,.16)";
    opts.ticks.forEach((x) => {
      ctx.beginPath(); ctx.moveTo(x + 0.5, 0); ctx.lineTo(x + 0.5, h - 12); ctx.stroke();
    });
  }
  if (opts.rain) {
    const n2 = opts.rain.length;
    for (let i = 0; i < n2; i++) {
      const p = opts.rain[i];
      if (!p) continue;
      const x = ax + (i / (n2 - 1)) * (plotW - pad);
      const bh = (p / 100) * (h - pad * 2 - bp) * 0.45;
      ctx.fillStyle = p >= 40 ? "rgba(96,164,226,.95)" : "rgba(54,116,172,.85)";
      const bw = Math.max(1.6, (w / n2) * 0.45);
      const y = h - pad - bp - bh;
      ctx.beginPath();
      if (ctx.roundRect) { ctx.roundRect(x - bw / 2, y, bw, bh, bw / 2); ctx.fill(); }
      else ctx.fillRect(x - bw / 2, y, bw, bh);
    }
  }
  charts[id] = { ctx, w, h };
}

/* ---------------- feed ---------------- */
function feed(msg, level = "") {
  const l = $("#logList");
  if (!l) return;              /* o bloco de eventos saiu do painel */
  const row = el("div", "row");
  row.appendChild(el("span", "n", msg));
  row.appendChild(el("span", "v" + (level ? " " + level : ""), clock()));
  l.prepend(row);
  while (l.children.length > 30) l.lastChild.remove();
}

/* ---------------- renderers ---------------- */
let lastAlive = null;
let devAll = false;
const stateCache = {};

function renderStats(d) {
  if (!d) return;
  $("#cpuVal").textContent = Math.round(d.cpu);
  $("#coresLabel").textContent = (d.cpu_cores || "?") + " THREADS · LOAD " + (d.load || []).map((v) => v.toFixed(2)).join(" ");

  const cb = $("#coreBars");
  const cores = d.cores || [];
  if (cb.children.length !== cores.length) {
    cb.innerHTML = "";
    cores.forEach(() => { const c = el("div", "core"); c.appendChild(el("i")); cb.appendChild(c); });
  }
  cores.forEach((v, i) => {
    const c = cb.children[i];
    c.firstChild.style.height = Math.max(3, Math.min(100, v)) + "%";
    c.className = "core" + (v > 85 ? " crit" : v > 60 ? " hot" : "");
  });

  const memPct = d.mem ? Math.round(d.mem.used / d.mem.total * 100) : 0;
  $("#memVal").textContent = memPct;
  $("#memBar").style.width = memPct + "%";
  $("#memTag").textContent = fmtBytes(d.mem.used) + " / " + fmtBytes(d.mem.total);
  const swPct = d.swap && d.swap.total ? Math.round((d.swap.total - d.swap.free) / d.swap.total * 100) : 0;
  $("#swapVal").textContent = swPct;
  $("#swapTxt").textContent = fmtBytes(d.swap ? d.swap.total - d.swap.free : null) + " of " + fmtBytes(d.swap ? d.swap.total : null);

  const kv = $("#memKv");
  kv.innerHTML = "";
  const add = (k, v) => { kv.appendChild(el("dt", null, k)); kv.appendChild(el("dd", null, v)); };
  add("used", fmtBytes(d.mem.used));
  add("avail", fmtBytes(d.mem.available));

  const t = d.temps || [];
  const hot = t.reduce((a, b) => (b.c > (a ? a.c : -99) ? b : a), null);
  $("#tempVal").textContent = hot ? Math.round(hot.c) : "--";

  $("#tempTag").textContent = hot ? hot.label : "--";
  $("#gpuVal").textContent = d.gpu == null ? "--" : Math.round(d.gpu);
  $("#gpuState").textContent = d.gpu == null ? "no sensor" : (d.gpu > 80 ? "load" : "idle");
  const tl = $("#tempList");
  tl.innerHTML = "";
  t.slice(0, 5).forEach((x) => {
    const r = el("div", "row");
    r.appendChild(el("span", "n sub", x.label));
    const v = Math.round(x.c);
    r.appendChild(el("span", "v" + (v > 80 ? " bad" : v > 65 ? " warn" : " ok"), v + " °C"));
    tl.appendChild(r);
  });

  const net = d.net || {};
  const nic = net.enp6s0 || net[Object.keys(net)[0]] || {};
  const [rv, ru] = fmtBps(nic.rx_bps);
  const [tv, tu] = fmtBps(nic.tx_bps);
  $("#rxVal").textContent = rv; $("#rxUnit").textContent = ru;
  $("#txVal").textContent = tv; $("#txUnit").textContent = tu;

  $("#chipUptime").innerHTML = "<i>UP</i>" + fmtUp(d.uptime);
  $("#chipLoad").innerHTML = "<i>LOAD</i>" + (d.load || []).map((v) => v.toFixed(2)).join(" ");

  const dk = $("#diskList");
  dk.innerHTML = "";
  (d.disks || []).forEach((x) => {
    const r = el("div", "disk");
    const top = el("div", "disk-top");
    top.appendChild(el("span", "n", x.mount));
    top.appendChild(el("span", "v" + (x.pct > 90 ? " bad" : x.pct > 75 ? " warn" : ""), x.pct.toFixed(1) + "%"));
    r.appendChild(top);
    const m = el("div", "meter thin");
    const i = el("i");
    i.style.width = x.pct + "%";
    i.style.background = x.pct > 90 ? "linear-gradient(90deg,#ff3b54,#ff8a9b)"
      : x.pct > 75 ? "linear-gradient(90deg,#ffb020,#ffd27a)"
      : "linear-gradient(90deg,#22e0ff,#8ef3ff)";
    m.appendChild(i);
    r.appendChild(m);
    r.appendChild(el("div", "sub", fmtBytes(x.used) + " of " + fmtBytes(x.total)));
    r.title = x.mount + ": " + fmtBytes(x.used) + " de " + fmtBytes(x.total);
    dk.appendChild(r);
  });

  const sh = d.disk_health;
  const sml = $("#smartList");
  if (sml) {
    sml.innerHTML = "";
    const list = (sh && sh.disks) || [];
    const bad = list.filter((x) => x.ok === false);
    const st = $("#smartTag");
    if (st) {
      st.textContent = list.length ? (bad.length ? bad.length + " FALHA" : list.length + " OK") : "--";
      st.className = "tag" + (bad.length ? " bad" : "");
    }
    const ord = list.slice().sort((a, b) =>
      (a.ok === false ? 0 : 1) - (b.ok === false ? 0 : 1) || String(a.device).localeCompare(String(b.device)));
    ord.forEach((x) => {
      const r = el("div", "row");
      r.appendChild(el("span", "n", x.device + " " + (x.model || "").slice(0, 10)));
      let txt;
      if (x.ok === false) txt = "FAIL " + (x.reallocated ? x.reallocated + "r" : (x.temp || "") + "\u00b0C");
      else if (x.ok == null) txt = "n/d";
      else txt = "OK " + (x.temp ? x.temp + "\u00b0C" : "");
      const cls = x.ok === false ? "v bad" : x.ok == null ? "v warn" : "v ok";
      r.appendChild(el("span", cls, txt));
      r.title = (x.model || "") + " \u2014 " + (x.hours || "?") + " h" + (x.wear != null ? ", wear " + x.wear + "%" : "");
      sml.appendChild(r);
    });
  }

  const sv = d.health || [];
  const okN = sv.filter((s) => s.ok).length;
  sv.forEach((s) => {
    const k = "svc:" + s.name, prev = stateCache[k];
    if (prev !== undefined && prev !== s.ok) feed(s.name + (s.ok ? " back up" : " DOWN " + (s.code || "")), s.ok ? "" : "bad");
    stateCache[k] = s.ok;
  });
  $("#svcTag").textContent = okN + "/" + sv.length + " UP";
  const sl = $("#svcList");
  sl.innerHTML = "";
  sv.forEach((s) => {
    const r = el("div", "row");
    r.appendChild(el("span", "n", s.name));
    r.appendChild(el("span", "v" + (s.ok ? " ok" : " bad"), s.ok ? s.ms + " ms" : "DOWN"));
    sl.appendChild(r);
  });

  const dk2 = d.docker_all || d.docker || [];
  const upN = dk2.filter((c) => c.up).length;
  dk2.forEach((c) => {
    const k = "ct:" + c.name, prev = stateCache[k];
    if (prev !== undefined && prev !== c.up) feed(c.name + (c.up ? " started" : " stopped"), c.up ? "" : "warn");
    stateCache[k] = c.up;
  });
  $("#dockTag").textContent = upN + "/" + dk2.length + " UP";
  const dl = $("#dockerList");
  dl.innerHTML = "";
  dk2.slice(0, 24).forEach((c) => {
    const r = el("div", "row ct");
    r.appendChild(el("span", "n", c.name));
    const v = el("span", "v" + (c.up ? " ok" : " bad"), c.up ? "UP" : "OFF");
    v.title = c.status || c.state || "";
    r.appendChild(v);
    const b = el("button", "act", c.up ? "stop" : "start");
    b.type = "button";
    b.title = c.up ? "parar " + c.name : "iniciar " + c.name;
    b.addEventListener("click", async () => {
      const acao = c.up ? "stop" : "start";
      if (acao === "stop" && !confirm("Parar o container " + c.name + "?")) return;
      b.disabled = true;
      b.textContent = "...";
      try {
        const resp = await fetch("/api/containers/" + encodeURIComponent(c.name) + "/" + acao,
                                 { method: "POST" });
        const j = await resp.json();
        feed(c.name + " " + (j.ok ? acao + " ok" : "erro: " + (j.error || acao)),
             j.ok ? "" : "warn");
      } catch (e) {
        feed(c.name + " erro de rede", "warn");
      }
      setTimeout(tickStats, 500);
    });
    r.appendChild(b);
    dl.appendChild(r);
  });

  const pc = $("#procList");
  pc.innerHTML = "";
  (d.procs || []).slice(0, 8).forEach((p) => {
    const r = el("div", "row");
    r.appendChild(el("span", "n", p.name));
    r.appendChild(el("span", "v", p.cpu.toFixed(0) + "% cpu · " + p.mem.toFixed(0) + "% mem"));
    pc.appendChild(r);
  });

  $("#lastSync").textContent = "SYNC " + clock(d.ts);
  /* bloco EVENTS removido: uptime e load ja estao nos chips do topo */
}

function niceMax(v) {
  if (!(v > 0)) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  const n = v / p;
  const m = n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10;
  return m * p;
}
function renderHistory(h) {
  if (!h || !h.cpu) return;
  const peak = (arr) => Math.max(1, ...arr);
  const span = (arr) => Math.min(100, Math.max(10, niceMax(peak(arr) * 1.25)));
  drawSeries("#cpuHist", [{ data: h.cpu, color: "#22e0ff", fill: true, blur: 8 }],
    { max: span(h.cpu), yAxis: true, fmt: (v) => Math.round(v) + "%" });
  drawSeries("#memHist", [{ data: h.mem, color: "#9d7bff", fill: true, glow: "rgba(157,123,255,.20)", blur: 8 }],
    { max: span(h.mem), yAxis: true, fmt: (v) => Math.round(v) + "%" });
  const rx = h.rx || [], tx = h.tx || [];
  const mx = niceMax(Math.max(1, ...rx, ...tx) * 1.1);
  drawSeries("#netChart", [
    { data: rx, color: "#22e0ff", fill: true, blur: 7 },
    { data: tx, color: "#9d7bff", blur: 5 },
  ], { max: mx, yAxis: true, fmt: (v) => v === 0 ? "0" : fmtBps(v)[0] + fmtBps(v)[1].replace("/s", "") });
}

function renderNetwork(n) {
  if (!n) return;
  const devs = (n.devices || []).slice().sort((a, b) => (b.alive - a.alive) || (a.ip > b.ip ? 1 : -1));
  $("#aliveCount").textContent = n.alive != null ? n.alive : devs.filter((d) => d.alive).length;

  const m = $("#matrix");
  if (m.children.length !== 254) {
    m.innerHTML = "";
    for (let i = 1; i <= 254; i++) {
      const c = el("div", "cell");
      c.style.setProperty("--c", (i - 1) % 16);   // column: drives the sweep delay
      c.title = "192.168.5." + i;
      m.appendChild(c);
    }
  }
  const map = new Map(devs.map((d) => [Number(d.ip.split(".").pop()), d]));
  [...m.children].forEach((c, i) => {
    const d = map.get(i + 1);
    c.className = "cell" + (d ? (d.alive ? " alive" : " seen") : "");
    /* last octet in the box: identify the address without counting columns */
    c.textContent = d ? String(i + 1) : "";
    if (d) c.title = d.ip + "  " + (d.host || d.vendor || "") + (d.rtt != null ? "  " + d.rtt.toFixed(1) + " ms" : "");
  });

  const tb = $("#devTable").querySelector("tbody");
  tb.innerHTML = "";
  devs.slice(0, devAll ? 200 : 8).forEach((d) => {
    const tr = el("tr");
    const td = (v) => { const c = el("td", null, v == null ? "--" : v); tr.appendChild(c); };
    td(d.ip);
    const host = el("td");
    host.appendChild(document.createTextNode(d.host || (d.alive ? "—" : "offline")));
    if (d.vendor && d.vendor !== "unknown") host.appendChild(el("span", "tdsub", d.vendor));
    tr.appendChild(host);
    td(d.mac || "--");
    td(d.rtt != null ? d.rtt.toFixed(1) + " ms" : "--");
    tb.appendChild(tr);
  });
  $("#devCount").textContent = devs.length + " HOSTS";
  $("#devMore").textContent = devAll ? "menos" : "ver todos";
}

let wxGood = null;      /* last payload with values, so a failed poll never blanks the row */

function wxDash(w) {
  if (!w || !w.ok || !w.current || w.current.temp == null) {
    $("#skyTag").textContent = wxGood ? "SEM RESPOSTA · últimos dados" : "SEM DADOS · Open-Meteo";
    if (!wxGood) {
      $("#wxTemp").textContent = "-";
      $("#wxCond").textContent = "AGUARDANDO";
      $("#wxHum").textContent = "conectando ao Open-Meteo…";
      $("#wxWind").textContent = "";
      $("#wxFeels").textContent = "";
    }
    return;
  }
  const age = Math.round((Date.now() / 1000) - (w.ts || Date.now() / 1000));
  if (age > 900) $("#skyTag").textContent = (w.location || "").toUpperCase() + " · DEFASADO " + Math.round(age / 60) + " MIN";
  else $("#skyTag").textContent = (w.location || "").toUpperCase() + " · UP " + (w.sunrise || "--") + " DN " + (w.sunset || "--");
}

function renderWeather(w) {
  if (!w || !w.ok || !w.current || w.current.temp == null) { wxDash(w); return; }
  wxGood = w;
  const c = w.current;
  $("#wxTemp").textContent = c.temp == null ? "-" : c.temp;
  $("#wxCond").textContent = (c.text || "").toUpperCase();
  $("#wxHum").textContent = "HUM " + (c.humidity == null ? "-" : c.humidity + "%");
  $("#wxWind").textContent = "WIND " + (c.wind == null ? "-" : c.wind + " km/h");
  $("#wxFeels").textContent = "FEELS " + (c.apparent == null ? "-" : c.apparent + "°");
  wxDash(w);

  const days = $("#wxDays");
  days.innerHTML = "";
  (w.days || []).forEach((d) => {
    const box = el("div", "day");
    box.appendChild(el("b", null, d.label));
    box.appendChild(el("div", "d-t", (d.max == null ? "--" : d.max + "°") + " / " + (d.min == null ? "--" : d.min + "°")));
    box.appendChild(el("div", "d-r", (d.text || "") + (d.precip ? " · " + d.precip + "%" : "")));
    days.appendChild(box);
  });

  const hours = w.hours || [];
  const step = 100 / Math.max(1, hours.length - 1);
  const wxGutter = 40;                     // left gutter for the temperature scale
  const wxPlot = Math.max(40, ($("#wxChart").clientWidth - wxGutter - 6));
  const labels = hours.map((h, i) => ({
    x: wxGutter + i * step * (wxPlot / 100),
    text: (i % 6 === 0) ? h.label.slice(0, 2) : "",
  }));
  const ticks = [];
  hours.forEach((h, i) => {
    if (i > 0 && h.date && h.date !== hours[i - 1].date)
      ticks.push(wxGutter + i * step * (wxPlot / 100));
  });
  drawSeries("#wxChart", [{
    data: hours.map((h) => h.temp),
    color: "#ffb020",
    fill: true,
    glow: "rgba(255,176,32,.22)",
    blur: 9,
  }], {
    min: Math.min(...hours.map((h) => h.temp)) - 1,
    max: Math.max(...hours.map((h) => h.temp)) + 1,
    rain: hours.map((h) => h.precip),
    labels, ticks, bottomPad: 14,
    yAxis: true, fmt: (v) => Math.round(v) + "\u00b0",
  });
}

/* ---------------- loop ---------------- */
let statsOk = false;

async function jget(path) {
  const r = await fetch(path, { cache: "no-store" });
  if (!r.ok) throw new Error(path + " " + r.status);
  return r.json();
}

async function tickStats() {
  try {
    const d = await jget("/stats");
    renderStats(d);
    const h = await jget("/history");
    renderHistory(h);
    if (!statsOk) { statsOk = true; feed("link established"); }
    $("#apiState").textContent = "LINK OK";
    $("#apiState").className = "pill";
    $("#statusDot").className = "dot";
    $("#statusText").textContent = "TELEMETRY STREAM LIVE";
  } catch (e) {
    statsOk = false;
    $("#apiState").textContent = "LINK LOST";
    $("#apiState").className = "pill bad";
    $("#statusDot").className = "dot bad";
    $("#statusText").textContent = "NO TELEMETRY — " + e.message;
  }
  $("#clock").textContent = clock();
}

async function tickNet() {
  try { renderNetwork(await jget("/network")); }
  catch (e) { /* keep last */ }
}

let wxTimer = null;
async function tickWeather() {
  let ok = false;
  try { const w = await jget("/weather"); renderWeather(w); ok = !!(w && w.ok); }
  catch (e) { wxDash(null); }
  clearTimeout(wxTimer);
  wxTimer = setTimeout(tickWeather, ok ? 300000 : 30000);   /* 5 min, or 30 s while down */
}

tickStats(); tickNet(); tickWeather();
$("#devMore").addEventListener("click", () => { devAll = !devAll; tickNet(); });
setInterval(tickStats, 2000);
setInterval(() => { $("#clock").textContent = clock(); }, 1000);
setInterval(tickNet, 25000);

window.addEventListener("resize", () => {
  clearTimeout(window.__rz);
  window.__rz = setTimeout(() => { tickStats(); tickWeather(); }, 250);
});
