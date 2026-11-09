const API = "http://192.168.5.245:1985";
const $ = (s) => document.querySelector(s);
const hist = { cpu: [], rtt: [], dev: [] };
const seen = { svc: {}, docker: {}, api: true, dev: true };
const HMAX = 60;

function fmtBytes(b, d = 1) {
  if (!b && b !== 0) return "--";
  const u = ["B", "K", "M", "G", "T"];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return b.toFixed(i ? d : 0) + u[i];
}
function fmtUp(s) {
  s = Math.floor(s);
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return (d ? d + "d " : "") + h + "h " + m + "m";
}
function pct(a, b) { return b ? Math.round(a / b * 1000) / 10 : 0; }

let feedCount = 0;
function feed(msg, level = "info") {
  const li = document.createElement("li");
  if (level === "warn") li.className = "warn";
  if (level === "err") li.className = "err";
  const t = new Date().toLocaleTimeString("pt-BR");
  li.innerHTML = `<b>${t}</b> ${msg}`;
  const list = $("#logList");
  list.prepend(li);
  while (list.children.length > 40) list.lastChild.remove();
  feedCount++;
}

function drawSpark(canvas, data, color) {
  if (!canvas) return;
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight || 40;
  if (canvas.width !== Math.floor(w * dpr) || canvas.height !== Math.floor(h * dpr)) {
    canvas.width = Math.floor(w * dpr); canvas.height = Math.floor(h * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  if (!data.length) return;
  const max = Math.max(1, ...data);
  ctx.beginPath();
  data.forEach((v, i) => {
    const x = (i / (HMAX - 1)) * w;
    const y = h - 2 - (v / max) * (h - 6);
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  });
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.shadowColor = color;
  ctx.shadowBlur = 6;
  ctx.stroke();
  ctx.shadowBlur = 0;
  ctx.lineTo(w, h); ctx.lineTo(0, h); ctx.closePath();
  const g = ctx.createLinearGradient(0, 0, 0, h);
  g.addColorStop(0, color + "44"); g.addColorStop(1, color + "00");
  ctx.fillStyle = g; ctx.fill();
}

async function getJSON(url) {
  const t0 = performance.now();
  const r = await fetch(url, { cache: "no-store" });
  const ms = performance.now() - t0;
  if (!r.ok) throw new Error("HTTP " + r.status);
  return { data: await r.json(), ms: Math.round(ms) };
}

function renderHost(s) {
  const cpu = Math.max(0, Math.min(100, s.cpu));
  $("#cpuVal").textContent = cpu.toFixed(0);
  $("#cpuArc").style.strokeDashoffset = 326 * (1 - cpu / 100);
  $("#load").textContent = s.load.map((x) => x.toFixed(2)).join(" / ");
  $("#uptime").textContent = fmtUp(s.uptime);
  $("#gpu").textContent = s.gpu === null ? "n/a" : s.gpu + "%";
  $("#cores").textContent = s.cpu_cores;
  const hot = s.temps.sort((a, b) => b.c - a.c)[0];
  $("#temp").textContent = hot ? hot.c.toFixed(0) + "°C" : "--";

  const memPct = pct(s.mem.used, s.mem.total);
  $("#memBar").style.width = memPct + "%";
  $("#memTxt").textContent = `${fmtBytes(s.mem.used)} / ${fmtBytes(s.mem.total)}`;
  const swPct = s.swap.total ? pct(s.swap.total - s.swap.free, s.swap.total) : 0;
  $("#swapBar").style.width = swPct + "%";
  $("#swapTxt").textContent = s.swap.total ? swPct + "%" : "off";

  $("#disks").innerHTML = s.disks.map((d) => `
    <div class="bar-row"><label>${d.mount === "/" ? "ROOT" : d.mount.replace("/", "").toUpperCase()}</label>
    <div class="bar"><i style="width:${d.pct}%"></i></div><span>${d.pct}%</span></div>`).join("");

  hist.cpu.push(cpu);
  if (hist.cpu.length > HMAX) hist.cpu.shift();
  drawSpark($("#cpuHist"), hist.cpu, "#22e0ff");

  if (hot && hot.c > 85) feed("temp alta em omaserver: " + hot.c.toFixed(0) + "°C", "warn");
  s.health.forEach((h) => {
    if (seen.svc[h.name] !== undefined && seen.svc[h.name] !== h.ok) {
      feed("serviço " + h.name + (h.ok ? " voltou" : " caiu"), h.ok ? "info" : "err");
    }
    seen.svc[h.name] = h.ok;
  });
  s.docker.forEach((c) => {
    if (seen.docker[c.name] !== undefined && seen.docker[c.name] !== c.up) {
      feed("container " + c.name + (c.up ? " subiu" : " caiu"), c.up ? "info" : "err");
    }
    seen.docker[c.name] = c.up;
  });
}

function renderNet(n) {
  $("#aliveCount").textContent = n.alive;
  const tb = $("#devTable tbody");
  tb.innerHTML = n.devices.map((d) => `
    <tr class="${d.alive ? "" : "dead"}">
      <td><span class="dot"></span>${d.ip}</td>
      <td>${d.host || "—"}</td>
      <td title="${(d.mac || "") + " " + (d.vendor || "")}">${(d.mac || "??").slice(0, 17)}</td>
      <td style="color:${d.rtt == null ? "#3c4a5a" : d.rtt < 5 ? "#7dff5e" : d.rtt < 25 ? "#ffe23d" : "#ff3b54"}">${d.rtt == null ? "—" : d.rtt.toFixed(1) + "ms"}</td>
    </tr>`).join("");
}

function renderDocker(list) {
  $("#dockerList").innerHTML = list.map((c) => `
    <li><span>${c.name}</span><span class="st ${c.up ? "" : "off"}">${c.up ? "UP" : "DOWN"}</span></li>`).join("");
}

function renderSvc(list) {
  $("#svcList").innerHTML = list.map((s) => `
    <li><a href="${s.url}" target="_blank" rel="noreferrer">${s.name}</a><span class="${s.ok ? "up" : "down"}">${s.ok ? "ONLINE" : "OFFLINE"} ${s.ok ? s.ms + "ms" : ""}</span></li>`).join("");
}

function renderProcs(list) {
  $("#procList").innerHTML = list.map((p) => `
    <li><span>${p.name}</span><span>${p.cpu.toFixed(1)}%</span><span>${p.mem.toFixed(1)}%</span></li>`).join("");
}

function renderDev(l) {
  $("#dCpu").textContent = l.cpu + "%";
  $("#dMem").textContent = pct(l.mem.used, l.mem.total) + "%";
  $("#dTemp").textContent = (l.temp / 1000).toFixed(0) + "°C";
  $("#dUp").textContent = fmtUp(l.uptime);
  $("#dRoot").style.width = pct(l.disk.root.used, l.disk.root.total) + "%";
  $("#dRootTxt").textContent = pct(l.disk.root.used, l.disk.root.total) + "%";
  $("#dUser").style.width = pct(l.disk.user.used, l.disk.user.total) + "%";
  $("#dUserTxt").textContent = pct(l.disk.user.used, l.disk.user.total) + "%";
  hist.dev.push(l.cpu);
  if (hist.dev.length > HMAX) hist.dev.shift();
  drawSpark($("#dHist"), hist.dev, "#ff2fd6");
  $("#dMeta").textContent = `${l.host} · fw ${l.fw} · ${l.procs} procs · rx ${fmtBytes(l.net[0])} tx ${fmtBytes(l.net[1])}`;
}

async function tickStats() {
  try {
    const { data, ms } = await getJSON(API + "/stats");
    $("#apiState").textContent = "LINK " + ms + "ms";
    $("#apiState").className = "pill ok";
    renderHost(data);
    renderDocker(data.docker);
    renderSvc(data.health);
    renderProcs(data.procs);
    const ifc = data.net["enp6s0"] || { rx_bps: 0, tx_bps: 0 };
    $("#rx").textContent = fmtBytes(ifc.rx_bps) + "/s";
    $("#tx").textContent = fmtBytes(ifc.tx_bps) + "/s";
    if (!seen.api) feed("uplink omaserver restabelecido", "info");
    seen.api = true;
    hist.rtt.push(ms);
    if (hist.rtt.length > HMAX) hist.rtt.shift();
    drawSpark($("#rttHist"), hist.rtt, "#7dff5e");
    $("#rtt").textContent = ms + "ms";
  } catch (e) {
    $("#apiState").textContent = "LINK LOST";
    $("#apiState").className = "pill bad";
    if (seen.api) feed("API omaserver inacessível: " + e.message, "err");
    seen.api = false;
  }
}

async function tickNet() {
  try {
    const { data } = await getJSON(API + "/network");
    renderNet(data);
  } catch (e) { /* logged by tickStats */ }
}

async function tickDev() {
  try {
    const r = await fetch("/api/local", { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    renderDev(await r.json());
    if (!seen.dev) feed("iDBox link restabelecido", "info");
    seen.dev = true;
  } catch (e) {
    $("#dMeta").textContent = "device link lost: " + e.message;
    if (seen.dev) feed("iDBox sem resposta: " + e.message, "err");
    seen.dev = false;
  }
}

function startFx() {
  const c = $("#fx"), ctx = c.getContext("2d");
  let w, h;
  const pts = Array.from({ length: 46 }, () => ({
    x: Math.random(), y: Math.random(), v: 0.0004 + Math.random() * 0.0012,
  }));
  function size() {
    const dpr = window.devicePixelRatio || 1;
    w = c.width = innerWidth * dpr; h = c.height = innerHeight * dpr;
    c.style.width = innerWidth + "px"; c.style.height = innerHeight + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  size(); addEventListener("resize", size);
  (function loop() {
    ctx.clearRect(0, 0, innerWidth, innerHeight);
    ctx.strokeStyle = "rgba(34,224,255,.35)";
    pts.forEach((p, i) => {
      p.y = (p.y + p.v) % 1;
      const x = p.x * innerWidth, y = p.y * innerHeight;
      ctx.fillStyle = i % 7 ? "rgba(34,224,255,.55)" : "rgba(255,47,214,.7)";
      ctx.fillRect(x, y, 1.4, 1.4);
      if (i && i % 9 === 0) {
        ctx.beginPath();
        ctx.moveTo(x, y);
        ctx.lineTo(x, y - 26);
        ctx.globalAlpha = 0.16;
        ctx.stroke();
        ctx.globalAlpha = 1;
      }
    });
    requestAnimationFrame(loop);
  })();
}

function clock() {
  $("#clock").textContent = new Date().toLocaleTimeString("pt-BR");
}
const TICKS = [
  "NEURAL LINK ESTABLISHED", "SOC: ALLWINNER H3 // 4x CORTEX-A7",
  "UPLINK: OMASERVER 192.168.5.245", "DASHBOARD NODE: IDBOX V2 192.168.5.129",
  "SCANNING 192.168.5.0/24", "PACKET FLOW NOMINAL",
];
function ticker() {
  $("#ticker").textContent = TICKS.join("   ///   ") + "   ///   ";
}

startFx();
clock(); setInterval(clock, 1000);
ticker(); setInterval(ticker, 20000);
tickStats(); setInterval(tickStats, 2000);
tickNet(); setInterval(tickNet, 15000);
tickDev(); setInterval(tickDev, 2500);
feed("dashboard online", "info");
