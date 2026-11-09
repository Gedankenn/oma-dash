#!/usr/bin/env python3
"""OMA//DASH - omaserver telemetry API + cyberpunk dashboard host."""
import json
import os
import re
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 1985
LAN_IP = "192.168.5.245"
IFACE = "enp6s0"
STATE = "/home/server/dash/state.json"
STATIC = "/home/server/dash/www"
HIST_MAX = 120

_lock = threading.Lock()
_prev = {"cpu": None, "net": None, "ts": 0.0}
_cache = {"stats": (0.0, None), "network": (0.0, None), "docker": (0.0, None),
          "health": (0.0, None), "dstats": (0.0, {}), "procs": (0.0, None)}
HIST = {"ts": [], "cpu": [], "mem": [], "rx": [], "tx": []}
CORES = []

HOSTNAMES = {
    "192.168.5.1": "gateway",
    "192.168.5.27": "Roomba do aRoombado",
    "192.168.5.114": "archlord",
    "192.168.5.129": "iDBox V2 (dash)",
    "192.168.5.151": "tv da mae",
    "192.168.5.245": "omaserver",
    "192.168.5.16": "ThinkBook Fabio",
    "192.168.5.17": "ThinkBook Fabio",
    "192.168.5.157": "ThinkBook Fabio",
}
OURS = {
    "30:96:10": "Huawei (router)", "60:c7:27": "Digiboard",
    "d8:c0:a6": "AzureWave / iRobot", "10:3d:0a": "Gaoshengda / AndroidTV",
    "70:fd:46": "Samsung", "b2:72:33": "random MAC", "8a:92:56": "random MAC",
    "ba:7a:d6": "random MAC", "22:35:5c": "local MAC",
}
SERVICES = [
    ("oma-hub", f"http://{LAN_IP}:80/"),
    ("go2rtc", f"http://{LAN_IP}:1984/"),
    ("jellyfin", f"http://{LAN_IP}:8096/health"),
    ("qBittorrent", f"http://{LAN_IP}:8081/"),
    ("filebrowser", f"http://{LAN_IP}:8082/"),
    ("romm", f"http://{LAN_IP}:8097/"),
    ("gtavc", f"http://{LAN_IP}:8008/"),
    ("dash api", f"http://{LAN_IP}:1985/health"),
]


def sh(cmd, timeout=4):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=timeout).stdout.strip()
    except Exception:
        return ""


def cpu_lines():
    out = []
    with open("/proc/stat") as f:
        for line in f:
            if not line.startswith("cpu"):
                break
            parts = line.split()
            name = parts[0]
            v = list(map(int, parts[1:]))
            idle = v[3] + (v[4] if len(v) > 4 else 0)
            out.append((name, sum(v), idle))
    return out


def net_snapshot():
    out = {}
    with open("/proc/net/dev") as f:
        for line in f:
            if ":" not in line:
                continue
            name, data = line.split(":", 1)
            p = data.split()
            out[name.strip()] = {"rx": int(p[0]), "tx": int(p[8])}
    return out


def meminfo():
    d = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            d[k] = int(v.strip().split()[0]) * 1024
    return d


def sampler():
    """2s telemetry sampler feeding history + per-core values."""
    prev = None
    prev_net = None
    prev_t = time.time()
    while True:
        time.sleep(2)
        try:
            now = time.time()
            lines = cpu_lines()
            total, idle = lines[0][1], lines[0][2]
            cores = []
            if prev:
                for (name, t, i), (_, pt, pi) in zip(lines[1:], prev[1:]):
                    dt = t - pt
                    cores.append(round(100.0 * (dt - (i - pi)) / dt, 1) if dt > 0 else 0.0)
                global CORES
                with _lock:
                    CORES = cores
            net = net_snapshot()
            rx = tx = 0
            if prev_net and IFACE in net and IFACE in prev_net:
                dt = max(now - prev_t, 0.001)
                rx = max(0, int((net[IFACE]["rx"] - prev_net[IFACE]["rx"]) / dt))
                tx = max(0, int((net[IFACE]["tx"] - prev_net[IFACE]["tx"]) / dt))
            cpu_pct = 0.0
            if prev:
                dt = total - prev[0][1]
                di = idle - prev[0][2]
                cpu_pct = round(100.0 * (dt - di) / dt, 1) if dt > 0 else 0.0
            m = meminfo()
            mem_pct = round(100.0 * (m["MemTotal"] - m.get("MemAvailable", m["MemFree"])) / m["MemTotal"], 1)
            with _lock:
                HIST["ts"].append(now)
                HIST["cpu"].append(cpu_pct)
                HIST["mem"].append(mem_pct)
                HIST["rx"].append(rx)
                HIST["tx"].append(tx)
                for k in HIST:
                    if len(HIST[k]) > HIST_MAX:
                        del HIST[k][0]
                _prev.update(cpu=cpu_pct, net={"rx": rx, "tx": tx}, ts=now)
            prev = lines
            prev_net = net
            prev_t = now
        except Exception:
            pass


def docker_stats_loop():
    while True:
        try:
            raw = sh("docker stats --no-stream --format '{{.Name}}|{{.CPUPerc}}|{{.MemPerc}}|{{.MemUsage}}'", timeout=15)
            st = {}
            for line in raw.splitlines():
                p = line.split("|")
                if len(p) >= 4:
                    st[p[0]] = {"cpu": p[1].strip(), "mem_pct": p[2].strip(), "mem": p[3].strip()}
            _cache["dstats"] = (time.time(), st)
        except Exception:
            pass
        time.sleep(6)


def disks():
    out = []
    for mnt in ("/", "/home", "/data"):
        if not os.path.ismount(mnt):
            continue
        try:
            s = os.statvfs(mnt)
            total = s.f_blocks * s.f_frsize
            used = (s.f_blocks - s.f_bfree) * s.f_frsize
            out.append({"mount": mnt, "total": total, "used": used,
                        "avail": s.f_bavail * s.f_frsize,
                        "pct": round(used / total * 100, 1)})
        except Exception:
            pass
    return out


def temps():
    out = []
    for hw in sorted(os.listdir("/sys/class/hwmon")) if os.path.isdir("/sys/class/hwmon") else []:
        base = f"/sys/class/hwmon/{hw}"
        try:
            label = open(f"{base}/name").read().strip()
        except Exception:
            continue
        for f in sorted(os.listdir(base)):
            if re.fullmatch(r"temp\d+_input", f):
                try:
                    c = int(open(f"{base}/{f}").read().strip()) / 1000.0
                    out.append({"label": label, "c": round(c, 1)})
                except Exception:
                    pass
    return out[:10]


def gpu():
    for card in ("/sys/class/drm/card0", "/sys/class/drm/card1"):
        b = f"{card}/device/gpu_busy_percent"
        if os.path.exists(b):
            try:
                return int(open(b).read().strip())
            except Exception:
                pass
    return None


def docker_ps():
    now = time.time()
    if now - _cache["docker"][0] < 4 and _cache["docker"][1] is not None:
        return _cache["docker"][1]
    raw = sh("docker ps --format '{{.Names}}|{{.Status}}|{{.Image}}'", timeout=6)
    out = []
    for line in raw.splitlines():
        p = line.split("|")
        if len(p) == 3:
            out.append({"name": p[0], "status": p[1], "image": p[2],
                        "up": p[1].lower().startswith("up")})
    _cache["docker"] = (now, out)
    return out


def _check(item):
    name, url = item
    t0 = time.time()
    code = sh(f"curl -sk -o /dev/null -w '%{{http_code}}' --max-time 1.2 '{url}'", timeout=2)
    ok = code.startswith(("2", "3")) or code == "401"
    return {"name": name, "url": url, "ok": ok, "code": code or "---",
            "ms": round((time.time() - t0) * 1000)}


def health():
    now = time.time()
    if now - _cache["health"][0] < 6 and _cache["health"][1] is not None:
        return _cache["health"][1]
    with ThreadPoolExecutor(max_workers=len(SERVICES)) as ex:
        out = list(ex.map(_check, SERVICES))
    _cache["health"] = (now, out)
    return out


def procs():
    now = time.time()
    if now - _cache["procs"][0] < 2 and _cache["procs"][1] is not None:
        return _cache["procs"][1]
    raw = sh("ps -eo pid,comm,pcpu,pmem --sort=-pcpu --no-headers | head -n 12", timeout=4)
    out = []
    for line in raw.splitlines():
        p = line.split(None, 3)
        if len(p) >= 4:
            out.append({"pid": int(p[0]), "name": p[1], "cpu": float(p[2]), "mem": float(p[3])})
    _cache["procs"] = (now, out)
    return out


def stats():
    now = time.time()
    if now - _cache["stats"][0] < 1.5 and _cache["stats"][1]:
        return _cache["stats"][1]
    m = meminfo()
    with _lock:
        his = {k: list(HIST[k]) for k in HIST}
        cores = list(CORES)
    data = {
        "ts": now,
        "host": socket.gethostname(),
        "uptime": float(open("/proc/uptime").read().split()[0]),
        "load": [float(x) for x in open("/proc/loadavg").read().split()[:3]],
        "cpu": his["cpu"][-1] if his["cpu"] else 0.0,
        "cpu_cores": os.cpu_count(),
        "cores": cores,
        "mem": {"total": m["MemTotal"], "available": m.get("MemAvailable", m["MemFree"]),
                "used": m["MemTotal"] - m.get("MemAvailable", m["MemFree"])},
        "swap": {"total": m.get("SwapTotal", 0), "free": m.get("SwapFree", 0)},
        "disks": disks(),
        "temps": temps(),
        "gpu": gpu(),
        "docker": docker_ps(),
        "dstats": _cache["dstats"][1],
        "health": health(),
        "procs": procs(),
        "hist": his,
        "net": {IFACE: {"rx_bps": his["rx"][-1] if his["rx"] else 0,
                        "tx_bps": his["tx"][-1] if his["tx"] else 0}},
    }
    _cache["stats"] = (now, data)
    return data


def vendor(mac):
    if not mac:
        return "?"
    return OURS.get(mac.lower()[:8], "unknown")


def ping(ip):
    r = subprocess.run(["ping", "-c", "1", "-W", "1", ip], capture_output=True, text=True)
    ok = r.returncode == 0
    m = re.search(r"time=([\d.]+)", r.stdout or "")
    return ip, ok, (float(m.group(1)) if m else None)


def probe_ports(ip, ports):
    openp = []
    for p in ports:
        s = socket.socket()
        s.settimeout(0.35)
        try:
            if s.connect_ex((ip, p)) == 0:
                openp.append(p)
        finally:
            s.close()
    return ip, openp


def scan_network():
    hosts = [f"192.168.5.{i}" for i in range(1, 255)]
    alive, rtt = [], {}
    with ThreadPoolExecutor(max_workers=128) as ex:
        for ip, ok, ms in ex.map(ping, hosts):
            if ok:
                alive.append(ip)
                rtt[ip] = ms
    neigh = {}
    for line in sh(f"ip neigh show dev {IFACE}").splitlines():
        p = line.split()
        if len(p) >= 4 and p[1] == "lladdr":
            neigh[p[0]] = p[2].upper()
    ports = [22, 53, 80, 443, 445, 554, 631, 1883, 3389, 5000, 8008, 8080, 8096, 8443, 9100]
    live_map = {}
    with ThreadPoolExecutor(max_workers=64) as ex:
        futs = {ex.submit(probe_ports, ip, ports): ip for ip in alive}
        for f in futs:
            try:
                ip, op = f.result()
                live_map[ip] = op
            except Exception:
                pass
    prev = {}
    try:
        with open(STATE) as f:
            prev = json.load(f)
    except Exception:
        pass
    now = time.time()
    devices = []
    for ip in sorted(set(list(alive) + list(prev)), key=lambda x: tuple(map(int, x.split(".")))):
        rec = prev.get(ip, {})
        is_alive = ip in alive
        mac = neigh.get(ip) or rec.get("mac")
        cur = rtt.get(ip)
        rh = (rec.get("rtt_hist") or [])[-29:]
        if cur is not None:
            rh = rh + [round(cur, 1)]
        devices.append({"ip": ip, "mac": mac or "", "vendor": vendor(mac),
                        "host": rec.get("host", ""),
                        "ports": live_map.get(ip, rec.get("ports", [])) if is_alive else rec.get("ports", []),
                        "alive": is_alive, "last_seen": now if is_alive else rec.get("last_seen", 0),
                        "rtt": cur, "rtt_hist": rh})
    own_mac = ""
    try:
        own_mac = open(f"/sys/class/net/{IFACE}/address").read().strip().upper()
    except Exception:
        pass
    for d in devices:
        if d["ip"] == LAN_IP:
            d.update(mac=own_mac or d["mac"], vendor=vendor(d["mac"]), host="omaserver",
                     alive=True, last_seen=now)
    for d in devices:
        if d["alive"]:
            if d["ip"] in HOSTNAMES:
                d["host"] = HOSTNAMES[d["ip"]]
                continue
            try:
                d["host"] = socket.gethostbyaddr(d["ip"])[0].split(".")[0]
            except Exception:
                d["host"] = d["host"] or ""
    try:
        with open(STATE, "w") as f:
            json.dump({d["ip"]: d for d in devices}, f)
    except Exception:
        pass
    return {"ts": now, "alive": len(alive), "total": 254, "devices": devices}


_bg = {"net": None}


def network():
    now = time.time()
    if now - _cache["network"][0] < 25 and _cache["network"][1]:
        return _cache["network"][1]
    if _bg["net"] is None or not _bg["net"].is_alive():
        _bg["net"] = threading.Thread(target=_run_scan, daemon=True)
        _bg["net"].start()
    return _cache["network"][1] or {"ts": now, "alive": 0, "total": 254, "devices": []}


def _run_scan():
    try:
        _cache["network"] = (time.time(), scan_network())
    except Exception as e:
        _cache["network"] = (time.time(), {"ts": time.time(), "alive": 0, "devices": [], "error": str(e)})


CTYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
          ".js": "application/javascript; charset=utf-8", ".png": "image/png",
          ".svg": "image/svg+xml", ".ico": "image/x-icon"}


class Handler(BaseHTTPRequestHandler):
    server_version = "oma-dash/2.0"

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, name):
        path = os.path.join(STATIC, name)
        if not os.path.isfile(path):
            self._json({"error": "not found"}, 404)
            return
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", CTYPES.get(os.path.splitext(name)[1], "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path.split("?")[0]
        try:
            if p in ("/", "/index.html"):
                self._file("index.html")
            elif p in ("/style.css", "/app.js", "/favicon.ico"):
                self._file(p.lstrip("/"))
            elif p == "/stats":
                self._json(stats())
            elif p == "/history":
                with _lock:
                    self._json({k: list(HIST[k]) for k in HIST})
            elif p == "/network":
                self._json(network())
            elif p == "/health":
                self._json({"ok": True, "ts": time.time()})
            elif p == "/all":
                self._json({"stats": stats(), "network": network()})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    threading.Thread(target=sampler, daemon=True).start()
    threading.Thread(target=docker_stats_loop, daemon=True).start()
    threading.Thread(target=_run_scan, daemon=True).start()
    threading.Thread(target=lambda: [(_run_scan(), time.sleep(25)) for _ in iter(int, 1)], daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    print(f"oma-dash on :{PORT}", flush=True)
    srv.serve_forever()
