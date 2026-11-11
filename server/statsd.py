#!/usr/bin/env python3
"""OMA//DASH - omaserver telemetry API + cyberpunk dashboard host."""
import ipaddress
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.request
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


# So responde para a LAN e para a tailnet. Fecha a porta para qualquer origem publica,
# mesmo que um dia exista port-forward no roteador ou um proxy na frente.
ALLOW_CIDR = os.environ.get("DASH_ALLOW_CIDR", "127.0.0.0/8,192.168.5.0/24,100.64.0.0/10")
NETS = []
for _c in ALLOW_CIDR.split(","):
    _c = _c.strip()
    if not _c:
        continue
    try:
        NETS.append(ipaddress.ip_network(_c, strict=False))
    except ValueError:
        pass


def allowed_ip(ip):
    try:
        a = ipaddress.ip_address(str(ip).split("%")[0])
    except ValueError:
        return False
    return any(a in n for n in NETS)


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
    seen = set()
    for mnt in ("/", "/data"):      # /home vive no mesmo fs que /
        if not os.path.ismount(mnt):
            continue
        try:
            dev_id = os.stat(mnt).st_dev
            if dev_id in seen:      # /home no mesmo fs que /: nao repete
                continue
            seen.add(dev_id)
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


GPU_NAMES = {"1002:699f": "Radeon RX 550", "1002:731f": "RX 5600 XT"}


def gpu_pci_id():
    for card in ("/sys/class/drm/card1", "/sys/class/drm/card0"):
        try:
            for line in open(f"{card}/device/uevent"):
                if line.startswith("PCI_ID="):
                    return line.split("=", 1)[1].strip().lower()
        except Exception:
            continue
    return ""


GPU_NAME = GPU_NAMES.get(gpu_pci_id(), "AMD GPU")


def gpu_temp():
    for t in temps():
        if t["label"] == "amdgpu":
            return t["c"]
    return None


def disk_health():
    """Le o resultado do disks-health.py (1x/dia)."""
    try:
        with open("/home/server/dash/disks.json") as f:
            return json.load(f)
    except Exception:
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


def docker_ps_all():
    """Todos os containers, inclusive parados (o painel precisa mostrar o que esta off)."""
    now = time.time()
    prev = _cache.get("docker_all") or (0.0, None)
    if now - prev[0] < 4 and prev[1] is not None:
        return prev[1]
    raw = sh("docker ps -a --format '{{.Names}}|{{.State}}|{{.Status}}|{{.Image}}'", timeout=6)
    out = []
    for line in raw.splitlines():
        p = line.split("|")
        if len(p) == 4:
            out.append({"name": p[0], "state": p[1], "status": p[2], "image": p[3],
                        "up": p[1] == "running"})
    out.sort(key=lambda c: (c["up"], c["name"]))   # parados primeiro: e o que precisa de acao
    _cache["docker_all"] = (now, out)
    return out


ACTIONS = {"start", "stop", "restart"}


def container_action(name, action, ip=""):
    """Liga/desliga um container existente. Registra quem pediu, quando."""
    if action not in ACTIONS:
        return {"ok": False, "error": "acao invalida"}
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$", name or ""):
        return {"ok": False, "error": "nome invalido"}
    if name not in {c["name"] for c in docker_ps_all()}:
        return {"ok": False, "error": "container nao existe"}
    out = sh(f"docker {action} {name}", timeout=30).strip()
    _cache["docker_all"] = (0.0, None)
    _cache["docker"] = (0.0, None)
    _cache["stats"] = (0.0, None)
    try:
        with open("/home/server/dash/actions.log", "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {ip or '-'} {action} {name}\n")
    except Exception:
        pass
    ok = "error" not in out.lower() and "no such" not in out.lower()
    return {"ok": ok, "name": name, "action": action, "out": out[:200]}


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
        "docker_all": docker_ps_all(),
        "disk_health": disk_health(),
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



# ---------------- cameras + LCD screen feed ----------------
CAMCONF = "/home/server/dash/cam.conf"
_camcache = {}
PC = {"ts": 0}


def pc_link():
    while True:
        try:
            pr = subprocess.Popen(
                ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
                 "sabinho@192.168.5.114", "/home/sabinho/bin/pcstats-loop"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
            for line in pr.stdout:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    d = json.loads(line)
                    d["ts"] = time.time()
                    PC.update(d)
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(5)


def cam_cfg():
    cfg = {}
    try:
        for line in open(CAMCONF):
            if "=" in line:
                k, v = line.strip().split("=", 1)
                cfg[k] = v
    except Exception:
        pass
    return cfg


def cam_jpeg(src, timeout=2):
    c = cam_cfg()
    url = c.get("GO2RTC_URL", "http://127.0.0.1:1984")
    auth = f'{c.get("GO2RTC_USER","")}:{c.get("GO2RTC_PASS","")}'
    return subprocess.run(
        ["curl", "-s", "-u", auth, "--max-time", str(timeout),
         f"{url}/api/frame.jpeg?src={src}"],
        capture_output=True).stdout


CAMSRCS = ["cam", "samsung", "cam3", "cam4"]
_camthumb = {}
_camwant = {}


def cam_gen(src, w, h):
    jpg = cam_jpeg(src, timeout=15)
    if not jpg:
        return b""
    try:
        return subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-i", "pipe:0",
             "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
            input=jpg, capture_output=True, timeout=12).stdout
    except Exception:
        return b""


def cam_worker(src, w, h):
    key = (src, w, h)
    while True:
        if time.time() - _camwant.get(key, 0) < 10:      # only while a viewer is asking
            data = cam_gen(src, w, h)
            if data:
                _camthumb[key] = (time.time(), data)
                time.sleep(2)
                continue
        time.sleep(5)


def start_cam_workers(w=320, h=180):
    for src in CAMSRCS:
        threading.Thread(target=cam_worker, args=(src, w, h), daemon=True).start()


def cam_rgb(src, w, h):
    key = (src, w, h)
    _camwant[key] = time.time()
    hit = _camthumb.get(key)
    if hit and time.time() - hit[0] < 10:
        return hit[1]
    return b""


# ---------------------------------------------------------------- weather
# Same source and labels as the omaweather Omarchy plugin: Open-Meteo (no key).
# Location comes from weather.json next to this script, falling back to the
# Omarchy default (Pato Branco). One cached request feeds /weather (JSON, for
# the web dashboard) and /weather.txt (plain lines, for the iDBox LCD).
WX_TTL = 600
WX_NAME = "Pato Branco"
WX_LAT, WX_LON = -26.22861, -52.67056
try:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "weather.json")) as _wf:
        _wj = json.load(_wf)
    WX_LAT = float(_wj.get("latitude", WX_LAT))
    WX_LON = float(_wj.get("longitude", WX_LON))
    WX_NAME = str(_wj.get("name") or WX_NAME)[:40]
except Exception:
    pass

_wx = {"ts": 0.0, "data": None}

# (lo, hi, text) on the WMO weather_code scale, mirroring Model.js wmoText().
WMO_RANGES = [
    (0, 0, "Clear"), (1, 1, "Mainly clear"), (2, 2, "Partly cloudy"),
    (3, 3, "Overcast"), (45, 48, "Fog"), (51, 57, "Drizzle"),
    (61, 67, "Rain"), (71, 77, "Snow"), (80, 82, "Showers"),
    (85, 86, "Snow showers"), (95, 99, "Thunderstorm"),
]


def wmo_text(code):
    try:
        c = int(code)
    except Exception:
        return "Cloudy"
    for lo, hi, txt in WMO_RANGES:
        if lo <= c <= hi:
            return txt
    return "Cloudy"


def _num(value, digits=0):
    try:
        v = float(value)
    except Exception:
        return None
    if v != v:
        return None
    return int(round(v)) if digits == 0 else round(v, digits)


def _day_label(iso):
    names = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
    try:
        import datetime
        return names[datetime.date.fromisoformat(str(iso)[:10]).weekday()]
    except Exception:
        return ""


def _hour_label(iso):
    s = str(iso or "")
    return s[11:16] if len(s) >= 16 else ""


def _wx_build(raw):
    hourly = raw.get("hourly") or {}
    daily = raw.get("daily") or {}
    cur = raw.get("current") or {}
    times = hourly.get("time") or []
    if not times or not (daily.get("time") or []):
        raise ValueError("incomplete Open-Meteo payload")

    now_iso = str(cur.get("time") or times[0])
    start = 0
    for i, ts in enumerate(times):
        if str(ts)[:13] >= now_iso[:13]:
            start = i
            break

    hours = []
    htemp = hourly.get("temperature_2m") or []
    hprec = hourly.get("precipitation_probability") or []
    hcode = hourly.get("weather_code") or []
    for i in range(start, len(times)):
        t = _num(htemp[i] if i < len(htemp) else None)
        if t is None:
            continue
        hours.append({"time": str(times[i]), "label": _hour_label(times[i]),
                      "date": str(times[i])[:10], "temp": t,
                      "precip": max(0, min(100, _num(hprec[i] if i < len(hprec) else 0) or 0)),
                      "code": hcode[i] if i < len(hcode) else None})
        if len(hours) >= 72:
            break
    if not hours:
        raise ValueError("no hourly data")

    days = []
    dt = daily.get("time") or []
    for i in range(min(3, len(dt))):
        days.append({
            "date": str(dt[i]),
            "label": _day_label(dt[i]),
            "code": (daily.get("weather_code") or [None])[i] if daily.get("weather_code") else None,
            "text": wmo_text((daily.get("weather_code") or [None])[i]) if daily.get("weather_code") else "",
            "max": _num((daily.get("temperature_2m_max") or [None])[i]),
            "min": _num((daily.get("temperature_2m_min") or [None])[i]),
            "precip": max(0, min(100, _num((daily.get("precipitation_probability_max") or [0])[i]) or 0)),
            "sunrise": _hour_label((daily.get("sunrise") or [""])[i]),
            "sunset": _hour_label((daily.get("sunset") or [""])[i]),
        })

    code = cur.get("weather_code")
    return {
        "ok": True,
        "location": WX_NAME,
        "ts": int(time.time()),
        "source": "open-meteo",
        "current": {
            "time": now_iso,
            "temp": _num(cur.get("temperature_2m")),
            "apparent": _num(cur.get("apparent_temperature")),
            "humidity": _num(cur.get("relative_humidity_2m")),
            "wind": _num(cur.get("wind_speed_10m")),
            "code": code,
            "text": wmo_text(code),
        },
        "hours": hours,
        "days": days,
        "sunrise": days[0]["sunrise"] if days else "",
        "sunset": days[0]["sunset"] if days else "",
    }


def weather():
    now = time.time()
    if _wx["data"] and now - _wx["ts"] < WX_TTL:
        return _wx["data"]
    url = ("https://api.open-meteo.com/v1/forecast?latitude=%.5f&longitude=%.5f"
           "&current=temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m"
           "&hourly=temperature_2m,precipitation_probability,weather_code"
           "&daily=weather_code,temperature_2m_max,temperature_2m_min,"
           "precipitation_probability_max,sunrise,sunset"
           "&timezone=auto&forecast_days=3" % (WX_LAT, WX_LON))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "oma-dash/2.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = _wx_build(json.loads(resp.read(262144).decode("utf-8", "replace")))
        _wx["data"] = data
        _wx["ts"] = now
        return data
    except Exception as e:
        if _wx["data"]:
            d = dict(_wx["data"])
            d["age"] = int(now - _wx["ts"])
            return d
        return {"ok": False, "location": WX_NAME, "error": str(e)}


def weather_feed():
    w = weather()
    if not w.get("ok"):
        return ("ver=1\nok=0\nloc=%s\n" % WX_NAME.upper().replace("|", " ")).encode()
    c = w["current"]
    lines = ["ver=1", "ok=1", "loc=" + str(w["location"]).upper().replace("|", " "),
             "txt=" + str(c.get("text") or "").upper(),
             "now=%s|%s|%s|%s|%s" % (c.get("temp"), c.get("code"), c.get("humidity"),
                                     c.get("wind"), c.get("apparent")),
             "sun=%s|%s" % (w.get("sunrise", ""), w.get("sunset", ""))]
    for h in w["hours"][:72]:
        lines.append("h=%s|%d|%d|%s" % (h["label"], h["temp"], h["precip"], h.get("date", "")))
    for d in w["days"][:3]:
        lines.append("d=%s|%s|%s|%s|%s|%s|%s" % (d["label"], d["code"], d["max"], d["min"],
                                                  d["precip"], d["sunrise"], d["sunset"]))
    return ("\n".join(lines) + "\n").encode()


def screen_feed():
    st = stats()
    nw = network()
    lines = ["ver=2", f"ts={int(time.time())}",
             f"cpu={st['cpu']:.1f}",
             f"mem={100.0*st['mem']['used']/st['mem']['total']:.1f}",
             f"temp={max([t['c'] for t in st['temps']] or [0]):.0f}",
             f"load={st['load'][0]:.2f},{st['load'][1]:.2f},{st['load'][2]:.2f}",
             f"up={int(st['uptime'])}",
             f"gpu={-1 if st['gpu'] is None else st['gpu']}",
             f"gt={-1 if gpu_temp() is None else round(gpu_temp())}",
             f"rx={sum(v.get('rx_bps', 0) for v in (st.get('net') or {}).values()) // 1024}",
             f"tx={sum(v.get('tx_bps', 0) for v in (st.get('net') or {}).values()) // 1024}",
             "disk=%d|%d" % (
                 next((round(d["pct"]) for d in st.get("disks", []) if d["mount"] == "/"), 0),
                 next((round(d["pct"]) for d in st.get("disks", []) if d["mount"] == "/data"), 0)),
             f"gname={GPU_NAME}",
             "dh=" + (lambda dh: "%d|%d|%d|%s" % (
                 0 if any(x.get("ok") is False for x in (dh.get("disks") or [])) else 1,
                 len(dh.get("disks") or []),
                 max([x.get("temp") or 0 for x in (dh.get("disks") or [])] or [0]),
                 next((x["device"] for x in (dh.get("disks") or []) if x.get("ok") is False), "")
             ))(disk_health() or {"disks": []}),
             f"dock={len(st['docker'])}|{sum(1 for c in st['docker'] if c['up'])}",
             f"alive={nw.get('alive',0)}|{nw.get('total',254)}"]
    for dev in nw.get("devices", []):
        if dev.get("alive"):
            lines.append("n=" + dev["ip"].split(".")[-1])
    dst = st.get("dstats") or {}
    for c in st["docker"][:10]:
        d = dst.get(c["name"], {})
        cpu = d.get("cpu", "0%").replace("%", "") or 0
        mem = d.get("mem_pct", "0%").replace("%", "") or 0
        lines.append(f"d={c['name']}|{1 if c['up'] else 0}|{cpu}|{mem}")
    for i, sv in enumerate(st["health"][:6]):
        lines.append(f"s={sv['name']}|{1 if sv['ok'] else 0}|{sv['ms']}")
    ranked = sorted([d for d in nw.get("devices", []) if d.get("alive") and d.get("rtt") is not None],
                    key=lambda x: x["rtt"])[:16]
    for dev in ranked:
        lines.append(f"v={dev['ip'].split('.')[-1]}|{dev['rtt']:.1f}")
    if PC.get("ts", 0) and time.time() - PC["ts"] < 12:
        f = (PC.get("f") or [0, 0, 0, 0, 0])[:5]
        lines.append("p=%.1f|%.1f|%.1f|%.1f|%.1f|%d|%d|%d|%d|%d|%d|%.1f|%.2f|%d" % (
            PC.get("c", 0), PC.get("m", 0), PC.get("g", 0), PC.get("gt", 0) or 0,
            PC.get("ct", 0) or 0, f[0], f[1], f[2], f[3], f[4],
            PC.get("gf", 0), PC.get("pw", 0) or 0, PC.get("l", 0), PC.get("u", 0)))
        # nome da GPU, linha propria: binario antigo do painel simplesmente ignora
        gn = "".join(ch for ch in str(PC.get("gn") or "") if 32 <= ord(ch) < 127 and ch != "|")[:18]
        if gn:
            lines.append("pg=" + gn)
        cn = "".join(ch for ch in str(PC.get("cn") or "") if 32 <= ord(ch) < 127 and ch != "|")[:18]
        if cn:
            lines.append("pcn=" + cn)
    else:
        lines.append("p=off")
    for src in ("cam", "cam2", "samsung"):
        lines.append(f"c={src}")
    return ("\n".join(lines) + "\n").encode()


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

    def _raw(self, body, ctype="application/octet-stream"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
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

    def _gate(self):
        ip = self.client_address[0]
        if allowed_ip(ip):
            return True
        try:
            with open("/home/server/dash/actions.log", "a") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {ip} REFUSED {self.path[:80]}\n")
        except Exception:
            pass
        self._json({"error": "acesso restrito a LAN"}, 403)
        return False

    def do_GET(self):
        if not self._gate():
            return
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
            elif p == "/screen.txt":
                self._raw(screen_feed(), "text/plain; charset=utf-8")
            elif p == "/weather":
                self._json(weather())
            elif p == "/weather.txt":
                self._raw(weather_feed(), "text/plain; charset=utf-8")
            elif p.startswith("/cam/"):
                src = p[5:].split(".")[0] or "cam"
                q = self.path.split("?")[1] if "?" in self.path else ""
                w = h = None
                for kv in q.split("&"):
                    if kv.startswith("w="):
                        w = int(kv[2:])
                    if kv.startswith("h="):
                        h = int(kv[2:])
                w = w or 136
                h = h or 76
                self._raw(cam_rgb(src, w, h))
            elif p == "/all":
                self._json({"stats": stats(), "network": network()})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        if not self._gate():
            return
        p = self.path.split("?")[0]
        try:
            m = re.match(r"^/api/containers/([^/]+)/(start|stop|restart)$", p)
            if not m:
                self._json({"ok": False, "error": "not found"}, 404)
                return
            res = container_action(m.group(1), m.group(2), self.client_address[0])
            self._json(res, 200 if res.get("ok") else 400)
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    threading.Thread(target=sampler, daemon=True).start()
    threading.Thread(target=pc_link, daemon=True).start()
    start_cam_workers()
    threading.Thread(target=docker_stats_loop, daemon=True).start()
    threading.Thread(target=_run_scan, daemon=True).start()
    threading.Thread(target=lambda: [(_run_scan(), time.sleep(25)) for _ in iter(int, 1)], daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    print(f"oma-dash on :{PORT}", flush=True)
    srv.serve_forever()
