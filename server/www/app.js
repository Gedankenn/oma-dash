const $ = (s) => document.querySelector(s);
const seen = { svc: {}, docker: {}, api: true };
let matrixCells = null;

function fmtBytes(b, d = 1) {
  if (b == null) return "--";
  const u = ["B", "K", "M", "G", "T"];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return b.toFixed(i ? d : 0) + u[i];
}
function fmtUp(s) {
  s = Math.floor(s);
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return (d ? d + "d " : "") + h + "h " + m + "m";
}
function pct(a, b) { return b ? Math.round(a / b * 1000) / 10 : 0; }

function feed(msg, level = "info") {
  const li = document.createElement("li");
  if (level !== "info") li.className = level;
  li.innerHTML = `<b>${new Date().toLocaleTimeString("pt-BR")}</b> ${msg}`;
  const l = $("#logList");
  l.prepend(li);
  while (l.children.length > 50) l.lastChild.remove();
}

function drawChart(cv, series, max) {
  if (!cv) return;
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, h = cv.clientHeight || 60;
  if (cv.width !== Math.floor(w * dpr)) { cv.width = Math.floor(w * dpr); cv.height = Math.floor(h * dpr); }
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  ctx.strokeStyle = "rgba(34,224,255,.08)";
  for (let y = 0; y < h; y += 16) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke(); }
  const n = Math.max(...series.map((s) => s.data.length), 2);
  series.forEach((s) => {
    if (s.data.length < 2) return;
    ctx.beginPath();
    s.data.forEach((v, i) => {
      const x = (i / (n - 1)) * w;
      const y = h - 2 - Math.min(v / (max || 100), 1) * (h - 6);
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    });
    ctx.strokeStyle = s.color; ctx.lineWidth = 1.6;
    ctx.shadowColor = s.color; ctx.shadowBlur = 6; ctx.stroke(); ctx.shadowBlur = 0;
    if (s.fill) {
      ctx.lineTo(w, h); ctx.lineTo(0, h); ctx.closePath();
      const g = ctx.createLinearGradient(0, 0, 0, h);
      g.addColorStop(0, s.color + "33"); g.addColorStop(1, s.color + "00");
      ctx.fillStyle = g; ctx.fill();
    }
    const last = s.data[s.data.length - 1];
    if (last != null) {
      const x = w, y = h - 2 - Math.min(last / (max || 100), 1) * (h - 6);
      ctx.fillStyle = "#fff"; ctx.fillRect(x - 2, y - 2, 3, 3);
    }
  });
}

function renderHost(s) {
  $("#cpuVal").textContent = Math.round(s.cpu);
  $("#cpuArc").style.strokeDashoffset = 326 * (1 - Math.min(s.cpu, 100) / 100);
  $("#load").textContent = s.load.map((x) => x.toFixed(2)).join(" / ");
  $("#uptime").textContent = fmtUp(s.uptime);
  $("#coresLabel").textContent = s.cpu_cores + " threads";
  $("#gpu").textContent = s.gpu == null ? "n/a" : s.gpu + "%";
  const hot = [...s.temps].sort((a, b) => b.c - a.c)[0];
  $("#temp").textContent = hot ? hot.c.toFixed(0) + "°C" : "--";

  const cores = s.cores || [];
  const cb = $("#coreBars");
  if (cb.children.length !== cores.length) {
    cb.innerHTML = cores.map(() => "<i><b></b></i>").join("");
  }
  cores.forEach((v, i) => {
    const b = cb.children[i]?.firstElementChild;
    if (b) b.style.height = Math.min(100, Math.max(6, v)) + "%";
  });

  const memPct = pct(s.mem.used, s.mem.total);
  $("#memBar").style.width = memPct + "%";
  $("#memTxt").textContent = `${fmtBytes(s.mem.used)} / ${fmtBytes(s.mem.total)}`;
  const swPct = s.swap.total ? pct(s.swap.total - s.swap.free, s.swap.total) : 0;
  $("#swapBar").style.width = swPct + "%";
  $("#swapTxt").textContent = s.swap.total ? swPct + "%" : "off";
  $("#memKv").innerHTML = `
    <div><label>USED</label><b>${fmtBytes(s.mem.used)}</b></div>
    <div><label>AVAIL</label><b>${fmtBytes(s.mem.available)}</b></div>
    <div><label>SWAP</label><b>${fmtBytes(s.swap.total - s.swap.free)}</b></div>
    <div><label>CORES</label><b>${s.cpu_cores}</b></div>`;

  $("#diskBars").innerHTML = s.disks.map((d) => `
    <div class="bar-row"><label>${d.mount === "/" ? "ROOT" : d.mount.slice(1).toUpperCase()}</label>
    <div class="bar"><i style="width:${d.pct}%"></i></div>
    <span>${fmtBytes(d.used)} / ${fmtBytes(d.total)}</span></div>`).join("");
  $("#temps").innerHTML = s.temps.slice(0, 6).map((t) =>
    `<div><label>${t.label.toUpperCase()}</label><b style="color:${t.c > 80 ? "#ff3b54" : "#cfe9f5"}">${t.c.toFixed(0)}°C</b></div>`).join("");

  const h = s.hist;
  drawChart($("#cpuHist"), [{ data: h.cpu, color: "#22e0ff", fill: true }], Math.max(10, Math.max(...h.cpu) * 1.4));
  drawChart($("#memHist"), [{ data: h.mem, color: "#ff2fd6", fill: true }], Math.max(10, Math.max(...h.mem) * 1.15));
  const nmax = Math.max(1, ...h.rx, ...h.tx);
  drawChart($("#netChart"), [
    { data: h.rx, color: "#7dff5e", fill: true },
    { data: h.tx, color: "#ff2fd6" },
  ], nmax);

  const ifc = { rx: h.rx[h.rx.length - 1] || 0, tx: h.tx[h.tx.length - 1] || 0 };
  $("#rx").textContent = fmtBytes(ifc.rx) + "/s";
  $("#tx").textContent = fmtBytes(ifc.tx) + "/s";

  s.health.forEach((sv) => {
    if (seen.svc[sv.name] !== undefined && seen.svc[sv.name] !== sv.ok) {
      feed("serviço " + sv.name + (sv.ok ? " voltou" : " caiu"), sv.ok ? "info" : "err");
    }
    seen.svc[sv.name] = sv.ok;
  });
}

function renderDocker(list, dstats) {
  $("#dockCount").textContent = `${list.filter((c) => c.up).length}/${list.length}`;
  $("#dockerList").innerHTML = list.map((c) => {
    const st = dstats[c.name] || {};
    const cpu = parseFloat((st.cpu || "0").replace("%", "")) || 0;
    const mem = (st.mem_pct || "0%").trim();
    return `<li>
      <span><span class="dot" style="${c.up ? "" : "background:#3c4a5a;box-shadow:none"}"></span>${c.name}</span>
      <div class="bar"><i style="width:${Math.min(100, cpu)}%"></i></div>
      <em>${cpu.toFixed(1)}% · ${mem}</em></li>`;
  }).join("");
}

function renderProcs(list) {
  const max = Math.max(1, ...list.map((p) => p.cpu));
  $("#procList").innerHTML = list.map((p) => `
    <li><span>${p.name} <em style="color:#3c4a5a">#${p.pid}</em></span>
    <div class="bar"><i style="width:${Math.max(2, p.cpu / max * 100)}%"></i></div>
    <em>${p.cpu.toFixed(1)}% cpu</em></li>`).join("");
}

function renderSvc(list) {
  $("#svcList").innerHTML = list.map((s) => `
    <li><a href="${s.url}" target="_blank" rel="noreferrer">${s.name}</a>
    <span class="${s.ok ? "up" : "down"}">${s.ok ? "ONLINE " + s.ms + "ms" : "OFFLINE"}</span></li>`).join("");
}

function buildMatrix() {
  const m = $("#matrix");
  m.innerHTML = "";
  matrixCells = [];
  for (let i = 1; i <= 254; i++) {
    const el = document.createElement("i");
    el.title = "192.168.5." + i;
    m.appendChild(el);
    matrixCells[i] = el;
  }
}

function renderNet(n) {
  $("#aliveCount").textContent = n.alive;
  const alive = new Set();
  n.devices.forEach((d) => { if (d.alive) alive.add(d.ip); });
  if (!matrixCells) buildMatrix();
  for (let i = 1; i <= 254; i++) {
    const ip = "192.168.5." + i;
    const el = matrixCells[i];
    if (!el) continue;
    el.className = alive.has(ip) ? "on" : "";
  }
  const tb = $("#devTable tbody");
  tb.innerHTML = n.devices.sort((a, b) => b.alive - a.alive).slice(0, 14).map((d) => `
    <tr class="${d.alive ? "" : "dead"}">
      <td><span class="dot"></span>${d.ip}</td>
      <td>${d.host || "—"}</td>
      <td title="${d.vendor}">${(d.mac || "??").slice(0, 17)}</td>
      <td style="color:${d.rtt == null ? "#3c4a5a" : d.rtt < 5 ? "#7dff5e" : d.rtt < 25 ? "#ffe23d" : "#ff3b54"}">${d.rtt == null ? "—" : d.rtt.toFixed(1) + "ms"}</td>
    </tr>`).join("");
}

async function getJSON(url) {
  const t0 = performance.now();
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return { data: await r.json(), ms: Math.round(performance.now() - t0) };
}

async function tickStats() {
  try {
    const { data, ms } = await getJSON("/stats");
    $("#apiState").textContent = "LINK " + ms + "ms";
    $("#apiState").className = "pill ok";
    $("#rtt").textContent = ms + "ms";
    renderHost(data);
    renderDocker(data.docker, data.dstats || {});
    renderProcs(data.procs);
    renderSvc(data.health);
    if (!seen.api) feed("uplink restabelecido", "info");
    seen.api = true;
  } catch (e) {
    $("#apiState").textContent = "LINK LOST";
    $("#apiState").className = "pill bad";
    if (seen.api) feed("API inacessível: " + e.message, "err");
    seen.api = false;
  }
}

async function tickNet() {
  try {
    const { data } = await getJSON("/network");
    renderNet(data);
  } catch (e) { /* handled in stats */ }
}

function startFx() {
  const c = $("#fx"), ctx = c.getContext("2d");
  const pts = Array.from({ length: 60 }, () => ({ x: Math.random(), y: Math.random(), v: 0.0004 + Math.random() * 0.001 }));
  function size() {
    const dpr = window.devicePixelRatio || 1;
    c.width = innerWidth * dpr; c.height = innerHeight * dpr;
    c.style.width = innerWidth + "px"; c.style.height = innerHeight + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  size(); addEventListener("resize", size);
  (function loop() {
    ctx.clearRect(0, 0, innerWidth, innerHeight);
    pts.forEach((p, i) => {
      p.y = (p.y + p.v) % 1;
      const x = p.x * innerWidth, y = p.y * innerHeight;
      ctx.fillStyle = i % 6 ? "rgba(34,224,255,.5)" : "rgba(255,47,214,.7)";
      ctx.fillRect(x, y, 1.4, 1.4);
      if (i % 11 === 0) {
        ctx.globalAlpha = .12; ctx.strokeStyle = "#22e0ff";
        ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x, y - 30); ctx.stroke(); ctx.globalAlpha = 1;
      }
    });
    requestAnimationFrame(loop);
  })();
}

function clock() { $("#clock").textContent = new Date().toLocaleTimeString("pt-BR"); }
const TICKS = [
  "OMASERVER ONLINE", "ALL SYSTEMS NOMINAL", "SCANNING 192.168.5.0/24",
  "DOCKER ORCHESTRATION ACTIVE", "NEURAL LINK ESTABLISHED",
];
function ticker() { $("#ticker").textContent = TICKS.join("   ///   ") + "   ///   "; }

startFx();
clock(); setInterval(clock, 1000);
ticker(); setInterval(ticker, 22000);
tickStats(); setInterval(tickStats, 2000);
tickNet(); setInterval(tickNet, 15000);
feed("dashboard online", "info");
