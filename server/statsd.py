#!/usr/bin/env python3
"""OMA//DASH - omaserver telemetry API + cyberpunk dashboard host."""
import ipaddress
import json
import os
import ssl
import re
import socket
import subprocess
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
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
          "health": (0.0, None), "dstats": (0.0, {}), "procs": (0.0, None),
          "memory": (0.0, None)}
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
# 16/11: nomes antigos de volta (so o hub continua NETWATCH); URL e porta iguais
SERVICES = [
    ("NETWATCH", f"http://{LAN_IP}:80/"),
    ("go2rtc", f"http://{LAN_IP}:1984/"),
    ("jellyfin", f"http://{LAN_IP}:8096/health"),
    ("qBittorrent", f"http://{LAN_IP}:8081/"),
    ("filebrowser", f"http://{LAN_IP}:8082/"),
    ("romm", f"http://{LAN_IP}:8097/"),
    ("gtavc", f"http://{LAN_IP}:8008/"),
    ("dash api", f"http://{LAN_IP}:1985/health"),
]

# MCP de memoria do time (docker no proprio omaserver). O token vive no .env do
# servico: o dash le na hora, nunca guarda copia.
MEM_BASE = f"http://{LAN_IP}:8100"
MEM_ENV = "/home/server/docker/memory/.env"
MEM_CONTAINER = "memory-memory-api-1"
MEM_CALL_RE = re.compile(r'^\S+\s+INFO:\s+(\S+):\d+ - "([A-Z]+) (\S+) HTTP/[\d.]+" (\d{3})')
MEM_TS_RE = re.compile(r"\.\d+")


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


def _fnum(valor, padrao=0.0):
    """float() que nao explode com '<defunct>', '--', '' ou '0.15%'."""
    try:
        txt = str(valor).strip().replace("%", "")
        return float(txt)
    except (TypeError, ValueError):
        return padrao


def docker_stats_loop():
    while True:
        try:
            raw = sh("docker stats --no-stream --format '{{.Name}}|{{.CPUPerc}}|{{.MemPerc}}|{{.MemUsage}}'", timeout=15)
            st = {}
            for line in raw.splitlines():
                p = line.split("|")
                if len(p) >= 4 and p[0].strip() and not p[0].startswith("<"):
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


SNAPRAID_LOG = "/var/log/snapraid-sync-final.log"
_snap_cache = {"ts": 0.0, "data": None}


def snapraid():
    """Progresso do sync de paridade (SnapRAID) lido do log do proprio sync."""
    now = time.time()
    if now - _snap_cache["ts"] < 5 and _snap_cache["data"] is not None:
        return _snap_cache["data"]
    info = {"running": False, "pct": None, "speed": "", "eta": "", "scanned": "",
            "state": "unknown", "updated": None, "line": ""}
    tail = ""
    try:
        st = os.stat(SNAPRAID_LOG)
        info["updated"] = int(st.st_mtime)
        with open(SNAPRAID_LOG, "rb") as fh:
            fh.seek(max(0, st.st_size - 4000))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        pass
    linhas = [ln.strip() for ln in tail.replace("\r", "\n").splitlines() if ln.strip()]
    info["line"] = linhas[-1][:120] if linhas else ""
    for ln in reversed(linhas):
        m = re.match(r"^(\d+)%,\s*([\d.]+)\s*(\w+),\s*([\d.]+)\s*(\w+/s),.*?([\d.:]+)\s*ETA", ln)
        if m:
            info.update(pct=int(m.group(1)), scanned=m.group(2) + " " + m.group(3),
                        speed=m.group(4) + " " + m.group(5), eta=m.group(6))
            break
    rodando = False
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/comm") as fh:
                    nome = fh.read().strip()
            except OSError:
                continue
            if nome == "snapraid":          # nome exato: nao confunde shell com o comando
                rodando = True
                break
    except OSError:
        pass
    info["running"] = rodando
    if rodando:
        info["state"] = "syncing"
    else:
        ultimas = linhas[-40:]
        corte_cancel = max((i for i, ln in enumerate(ultimas) if ln.startswith("cancelado")), default=-1)
        corte_fim = max((i for i, ln in enumerate(ultimas) if ln.startswith("fim ")), default=-1)
        if corte_cancel > corte_fim:
            info["state"] = "cancelled"
        elif any("fim 0" in ln for ln in ultimas) or any("Everything is OK" in ln for ln in ultimas):
            info["state"] = "ok"
            info["pct"] = 100 if info["pct"] is not None else None
        elif any(ln.startswith("fim ") for ln in ultimas):
            info["state"] = "error"
        else:
            info["state"] = "idle"
    _snap_cache.update(ts=now, data=info)
    return info


SNAPRAID_CTL = "/home/server/dash/snapraid-ctl.sh"


def snapraid_action(action, ip=""):
    """start/cancel do sync de paridade. Comando fixo no helper; nada livre vem da web."""
    try:
        out = subprocess.run([SNAPRAID_CTL, action], capture_output=True, text=True, timeout=25)
    except Exception as exc:                      # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}
    detalhe = ((out.stdout or "") + (out.stderr or "")).strip()[:200]
    try:
        with open("/home/server/dash/actions.log", "a") as fh:
            fh.write("%s %s snapraid %s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), ip, action, detalhe))
    except OSError:
        pass
    _snap_cache.update(ts=0.0, data=None)
    return {"ok": out.returncode == 0, "action": action, "detail": detalhe}


ALERTS_FILE = "/home/server/dash/alerts.json"
NETWATCH_FILE = "/home/server/netwatch/alerts.jsonl"
SERVICES_JSON = "/home/server/services.json"          # catalogo unico (tools/catalogo.py)
SERVICES_STATE = "/home/server/dash/services-state.json"
SERVICES_PERIOD = 300                                # s entre sondagens (5 min)
LAN_IP = os.environ.get("DASH_LAN_IP", "192.168.5.245")
INVENTORY = "/home/server/dash/services-auto.json"        # inventario de portas (regra do dono)
INVENTORY_HUB = "/home/server/lan-hub/lan-auto.json"      # mesma coisa para o hub :80
PORTA_MIN, PORTA_MAX = 1024, 32768                        # ignora sistema e efemeras
PORTA_FORA = {6881}                                       # P2P do qBittorrent nao e "servico"
INVENTORY_HIST = "/home/server/dash/services-auto-hist.json"
NETWATCH_WINDOW = 6 * 3600          # s: so o que e recente vira alerta de tela


def alerts():
    """Alertas gravados pelo watch-health.py (timer de 5 min)."""
    try:
        with open(ALERTS_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def netwatch_alerts(now=None, window=NETWATCH_WINDOW):
    """Sensor netwatch (ARP/portas/UFW): so high/critical recentes entram.

    Um alerta de 3 dias atras no banner e ruido; aqui a janela e curta e cada
    regra+chave aparece uma vez, com a ocorrencia mais recente.
    """
    now = now or time.time()
    nivel = {"critical": "crit", "high": "warn"}
    ultimo = {}
    try:
        with open(NETWATCH_FILE) as f:
            for linha in f:
                try:
                    a = json.loads(linha)
                except ValueError:
                    continue
                lv = nivel.get(a.get("sev"))
                if not lv or now - (a.get("ts") or 0) > window:
                    continue
                chave = f"{a.get('rule')}:{a.get('key')}"
                ultimo[chave] = {"id": "netwatch:" + chave, "src": "netwatch",
                                 "level": lv, "text": a.get("msg") or "",
                                 "rule": a.get("rule"), "ts": a.get("ts")}
    except OSError:
        pass
    return list(ultimo.values())


def _probe_opener():
    """Abre URL sem seguir redirect: o catalogo espera o codigo cru (302/303)."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE              # gtavc-web usa certificado proprio

    class _SemRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    return urllib.request.build_opener(_SemRedirect,
                                       urllib.request.HTTPSHandler(context=ctx))


def _probe_code(opener, url):
    try:
        with opener.open(urllib.request.Request(url, headers={"User-Agent": "oma-dash-probe/1"}),
                         timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code                          # 401 do cams, 302 do jellyfin etc.
    except Exception:
        return 0                               # sem resposta


def probe_services(catalogo=SERVICES_JSON, estado=SERVICES_STATE):
    """Sonda cada servico catalogado e conta falhas consecutivas.

    Regra do url_local: tenta o endereco como esta (o proprio servidor roda o
    dash, entao 127.0.0.1 vale); se falhar e for loopback, tenta de novo pelo
    IP da LAN. O stellacorp so escuta em loopback — trocar sempre pelo IP daria
    falso positivo.
    """
    try:
        with open(catalogo) as f:
            servicos = (json.load(f) or {}).get("servicos") or []
    except Exception:
        return None
    try:
        with open(estado) as f:
            antes = (json.load(f) or {}).get("probes") or {}
    except Exception:
        antes = {}
    opener = _probe_opener()
    agora = int(time.time())
    probes = {}
    for s in servicos:
        ident = (s.get("id") or "").strip()
        url = (s.get("url_local") or "").strip()
        if not ident or not url:
            continue
        esperado = int(s.get("http") or 200)
        code = _probe_code(opener, url)
        if code != esperado and "127.0.0.1" in url:
            code = _probe_code(opener, url.replace("127.0.0.1", LAN_IP))
        ok = code == esperado
        fails = 0 if ok else int((antes.get(ident) or {}).get("fails") or 0) + 1
        probes[ident] = {"nome": s.get("nome") or ident, "url": url,
                         "esperado": esperado, "code": code, "ok": ok,
                         "fails": fails, "publico": (s.get("publico") or "").strip(),
                         "checked": agora}
    dados = {"ts": agora, "probes": probes}
    try:
        tmp = estado + ".tmp"
        with open(tmp, "w") as f:
            json.dump(dados, f)
        os.replace(tmp, estado)
    except Exception:
        pass
    return dados


def _portas_escutando():
    """Portas TCP fora do loopback que o host esta servindo (sem root, ss basta)."""
    out = sh("ss -ltnH 2>/dev/null") or ""
    portas = {}
    for linha in out.splitlines():
        p = linha.split()
        if len(p) < 4:
            continue
        local = p[3]
        if local.startswith("127.") or local.startswith("[::1]"):
            continue
        m = re.search(r":(\d+)$", local)
        if not m:
            continue
        porta = int(m.group(1))
        if PORTA_MIN <= porta < PORTA_MAX and porta not in PORTA_FORA:
            portas[porta] = local
    return portas


def _dono_das_portas():
    """container -> portas publicadas, via docker (sem root)."""
    dono = {}
    out = sh("docker ps --format '{{.Names}}|{{.Ports}}' 2>/dev/null") or ""
    for linha in out.splitlines():
        if "|" not in linha:
            continue
        nome, ports = linha.split("|", 1)
        for m in re.finditer(r":(\d+)->", ports):
            dono.setdefault(int(m.group(1)), nome)
    return dono


def _tcp_abre(porta, host=LAN_IP, timeout=1.5):
    import socket
    t0 = time.time()
    try:
        with socket.create_connection((host, porta), timeout=timeout):
            return True, int((time.time() - t0) * 1000)
    except Exception:
        return False, 0


def inventario_portas(catalogo=SERVICES_JSON, hist_path=INVENTORY_HIST,
                      saida=INVENTORY, espelho=INVENTORY_HUB):
    """REGRA DO DONO: toda porta rodando na LAN entra na dashboard, sempre.

    Descobre sozinho o que esta escutando (nada de lista manual): sonda HTTP
    sem seguir redirect e, se nao for HTTP, TCP connect. Guarda primeira vez
    visto, amostras e uptime para o hub/dash mostrarem estatistica de servico
    que ninguem cadastrou.
    """
    try:
        with open(catalogo) as f:
            servicos = (json.load(f) or {}).get("servicos") or []
    except Exception:
        servicos = []
    catalogo_url = {}
    for s in servicos:
        u = (s.get("url_local") or "")
        m = re.search(r":(\d+)(/|$)", u)
        if m:
            catalogo_url[int(m.group(1))] = s
    try:
        with open(hist_path) as f:
            hist = json.load(f) or {}
    except Exception:
        hist = {}

    opener = _probe_opener()
    agora = int(time.time())
    dono = _dono_das_portas()
    itens = {}
    for porta, local in sorted(_portas_escutando().items()):
        alvo = "https://%s:%d/" % (LAN_IP, porta) if porta == 8083 else "http://%s:%d/" % (LAN_IP, porta)
        t0 = time.time()
        code = _probe_code(opener, alvo)
        modo = "http"
        ms = int((time.time() - t0) * 1000)
        if not code:
            ok_tcp, ms = _tcp_abre(porta)
            modo = "tcp" if ok_tcp else "fechado"
            code = 0
        ok = bool(code) or modo == "tcp"
        h = hist.get(str(porta)) or {}
        s_hist = (h.get("s") or "") + ("1" if ok else "0")
        s_hist = s_hist[-2016:]
        hist[str(porta)] = {"s": s_hist, "first_seen": h.get("first_seen") or agora,
                            "nome": h.get("nome") or (catalogo_url.get(porta) or {}).get("nome")
                                    or dono.get(porta) or ""}
        base = catalogo_url.get(porta) or {}
        itens[str(porta)] = {
            "porta": porta, "bind": local, "nome": base.get("nome") or dono.get(porta) or ("porta %d" % porta),
            "container": dono.get(porta) or "", "catalogo": base.get("id") or "",
            "url": alvo, "modo": modo, "code": code or 0, "ok": ok, "ms": ms,
            "fails": 0 if ok else int((h.get("fails") or 0)) + 1,
            "first_seen": hist[str(porta)]["first_seen"],
            "samples": len(s_hist),
            "uptime": round(100.0 * s_hist.count("1") / len(s_hist), 1) if s_hist else 0.0,
            "checked": agora,
        }
        hist[str(porta)]["fails"] = itens[str(porta)]["fails"]

    # porta que ja existia e nao esta mais escutando: continua no inventario
    # (com falha acumulada) para virar alerta; so sai quando volta ou em 30 dias.
    escutando = set(itens)
    for porta, h in list(hist.items()):
        if porta in escutando or not h.get("first_seen"):
            continue
        if agora - int(h.get("first_seen") or 0) < 300:
            continue                                  # visto ha <5 min: ainda subindo
        if agora - int(h.get("checked") or agora) > 30 * 86400:
            hist.pop(porta, None)
            continue
        s_hist = (h.get("s") or "") + "0"
        s_hist = s_hist[-2016:]
        fails = int(h.get("fails") or 0) + 1
        h.update({"s": s_hist, "fails": fails, "checked": agora})
        itens[porta] = {"porta": int(porta), "bind": "—", "nome": h.get("nome") or ("porta %s" % porta),
                        "container": "", "catalogo": "", "url": "", "modo": "fechado",
                        "code": 0, "ok": False, "ms": 0, "fails": fails,
                        "first_seen": h.get("first_seen"), "samples": len(s_hist),
                        "uptime": round(100.0 * s_hist.count("1") / len(s_hist), 1) if s_hist else 0.0,
                        "checked": agora}

    dados = {"ts": agora, "host": LAN_IP, "total": len(itens),
             "up": sum(1 for i in itens.values() if i["ok"]), "portas": itens}
    for destino in (saida, espelho):
        try:
            tmp = destino + ".tmp"
            with open(tmp, "w") as f:
                json.dump(dados, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, destino)
        except Exception:
            pass
    try:
        tmp = hist_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(hist, f)
        os.replace(tmp, hist_path)
    except Exception:
        pass
    return dados


def inventario_alerts(now=None):
    """Porta que existia e sumiu vira achado (2 falhas warn, 3+ crit)."""
    try:
        with open(INVENTORY) as f:
            d = json.load(f) or {}
    except Exception:
        return []
    out = []
    for porta, p in (d.get("portas") or {}).items():
        fails = int(p.get("fails") or 0)
        if fails < 2 or p.get("ok"):
            continue
        out.append({"id": "port:" + porta, "src": "services",
                    "level": "crit" if fails >= 3 else "warn",
                    "text": "porta %s (%s) parou de responder há %d rodadas" % (
                        porta, p.get("nome") or "sem nome", fails),
                    "ts": d.get("ts") or int(now or time.time()), "banner": True})
    return out


def services_alerts(now=None, estado=SERVICES_STATE):
    """Achados da fonte `services`.

    2 falhas seguidas = warn; 3+ ou servico com hostname publico = crit.
    Quando o servico responde de novo o contador zera e o achado desaparece
    (mesma semantica dos outros: sem estado "resolvido", some da lista).
    """
    try:
        with open(estado) as f:
            dados = json.load(f) or {}
    except Exception:
        return []
    out = []
    for ident, p in (dados.get("probes") or {}).items():
        fails = int(p.get("fails") or 0)
        if fails < 2:
            continue
        publico = bool(p.get("publico"))
        crit = fails >= 3 or publico
        detalhe = "sem resposta" if not p.get("code") else "HTTP %s" % p.get("code")
        complemento = ""
        if publico:
            complemento = " · publicado (%s)" % p["publico"].split(" + ")[0]
        out.append({
            "id": "svc:" + ident,
            "src": "services",
            "level": "crit" if crit else "warn",
            "text": "%s fora do ar: %s (esperado %s)%s" % (
                p.get("nome") or ident, detalhe, p.get("esperado"), complemento),
            "ts": dados.get("ts") or int(now or time.time()),
            "fails": fails,
            "banner": True,
        })
    return out


def _services_loop():
    while True:
        # falha de sonda nao derruba o loop: a proxima rodada tenta de novo
        for etapa in (probe_services, inventario_portas):
            try:
                etapa()
            except Exception:
                pass
        time.sleep(SERVICES_PERIOD)


def alerts_banner(include_hidden=False, now=None):
    """Feed do banner e da API /api/alerts — fonte unica, nada escrito em HTML.

    Alerta com card proprio (SMART, temperatura) fica fora do banner: o card ja
    mostra o mesmo fato. include_hidden=True devolve tudo, para diagnostico.
    """
    now = now or time.time()
    bruto = alerts() or {}
    itens = []
    for a in bruto.get("alerts") or []:
        if not include_hidden and (a.get("banner") is False or a.get("card")):
            continue
        itens.append({"id": a.get("id"), "src": a.get("src") or "storage",
                      "level": a.get("level") or "warn",
                      "text": a.get("text") or "", "ts": bruto.get("ts")})
    itens += netwatch_alerts(now)
    itens += services_alerts(now)
    itens += inventario_alerts(now)
    return {"ts": int(now), "ok": not itens, "alerts": itens,
            "counts": {lv: sum(1 for a in itens if a["level"] == lv)
                       for lv in ("crit", "warn", "info")}}


def api_alerts(qs):
    """GET /api/alerts — filtros: level=crit,warn  src=storage,netwatch,services  limit=N  all=1."""
    tudo = alerts_banner(include_hidden=qs.get("all") in ("1", "true", "yes"))
    itens = tudo["alerts"]
    niveis = [x.strip() for x in (qs.get("level") or "").split(",") if x.strip()]
    if niveis:
        itens = [a for a in itens if a["level"] in niveis]
    fontes = [x.strip() for x in (qs.get("src") or "").split(",") if x.strip()]
    if fontes:
        itens = [a for a in itens if a["src"] in fontes]
    limite = qs.get("limit")
    if limite and str(limite).isdigit():
        itens = itens[:int(limite)]
    return {"ts": tudo["ts"], "ok": not itens,
            "counts": {lv: sum(1 for a in itens if a["level"] == lv)
                       for lv in ("crit", "warn", "info")},
            "sources": sorted({a["src"] for a in itens}),
            "alerts": itens}


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
    """Card SERVICES: lista curada + TODA porta descoberta pelo inventario.

    Regra do dono (17/11): servico novo aparece sem ninguem cadastrar. O que
    esta na lista curada mantem o nome bonito; o resto entra como "porta NNNN"
    (ou nome do container) com estado e latencia.
    """
    now = time.time()
    if now - _cache["health"][0] < 6 and _cache["health"][1] is not None:
        return _cache["health"][1]
    with ThreadPoolExecutor(max_workers=len(SERVICES)) as ex:
        out = list(ex.map(_check, SERVICES))
    cobertas = set()
    for s in SERVICES:
        m = re.search(r":(\d+)(/|$)", s[1])
        if m:
            cobertas.add(int(m.group(1)))
    try:
        with open(INVENTORY) as f:
            inv = (json.load(f) or {}).get("portas") or {}
    except Exception:
        inv = {}
    for porta, p in sorted(inv.items(), key=lambda kv: int(kv[0])):
        if int(porta) in cobertas:
            continue
        out.append({"name": "%s :%s" % (p.get("nome") or "porta", porta),
                    "url": p.get("url") or "", "ok": bool(p.get("ok")),
                    "code": str(p.get("code") or ("tcp" if p.get("modo") == "tcp" else "--")),
                    "ms": p.get("ms") or 0, "auto": True})
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
            out.append({"pid": int(p[0]), "name": p[1], "cpu": _fnum(p[2]), "mem": _fnum(p[3])})
    _cache["procs"] = (now, out)
    return out


def _mem_token():
    """Le o token do .env do servico de memoria (sem copiar para outro arquivo)."""
    try:
        with open(MEM_ENV, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == "MEMORY_TOKEN" and v.strip():
                    return v.strip().strip('"').strip("'")
        with open(MEM_ENV, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("MEMORY_ADMIN_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        return ""
    return ""


def _mem_get(path, token="", timeout=3):
    req = urllib.request.Request(MEM_BASE + path, headers={"User-Agent": "oma-dash/2.0"})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read(65536).decode("utf-8", "replace"))


def _mem_ts(txt):
    try:
        return datetime.fromisoformat(MEM_TS_RE.sub("", txt).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def memory_mcp():
    """Status do MCP de memoria: saude, acervo e chamadas desde o start do container."""
    now = time.time()
    if now - _cache["memory"][0] < 15 and _cache["memory"][1]:
        return _cache["memory"][1]
    out = {"ts": now, "ok": False, "health": {}, "calls": {}}
    try:
        h = _mem_get("/healthz", timeout=2.5)
        out["health"] = h
        out["ok"] = h.get("status") == "ok" and bool(h.get("db")) and bool(h.get("embed"))
    except Exception as e:
        out["error"] = str(e)[:140]
    token = _mem_token()
    if token:
        try:
            out["stats"] = _mem_get("/v1/stats", token)
        except Exception as e:
            out["stats_error"] = str(e)[:140]

    started = sh(f"docker inspect -f '{{{{.State.StartedAt}}}}' {MEM_CONTAINER}", timeout=3)
    out["started_at"] = started
    out["uptime_s"] = max(0, int(now - _mem_ts(started))) if started else None

    raw = sh(f"docker logs -t {MEM_CONTAINER} 2>&1", timeout=8)
    total = reads = writes = hour = day = 0
    last_at, last_path = 0.0, ""
    by_path, clients = {}, {}
    cutoff_h, cutoff_d = now - 3600, now - 86400
    for line in raw.splitlines():
        m = MEM_CALL_RE.match(line)
        if not m:
            continue
        ip, method, path = m.group(1), m.group(2), m.group(3).split("?")[0]
        # o proprio painel fala com 127.0.0.1: nao conta como uso do time
        if ip in ("127.0.0.1", "::1", "localhost"):
            continue
        if not (path.startswith("/v1/") or path.startswith("/mcp")):
            continue
        # /v1/stats e o proprio painel lendo o acervo: nao e uso do time
        if path == "/v1/stats":
            continue
        ts = _mem_ts(line.split(" ", 1)[0])
        key = method + " " + path
        by_path[key] = by_path.get(key, 0) + 1
        clients[ip] = clients.get(ip, 0) + 1
        total += 1
        if path in ("/v1/search", "/v1/recent") or path.startswith("/v1/memory/"):
            reads += 1
        elif path in ("/v1/remember", "/v1/forget"):
            writes += 1
        if ts >= cutoff_h:
            hour += 1
        if ts >= cutoff_d:
            day += 1
        if ts > last_at:
            last_at, last_path = ts, key
    out["calls"] = {
        "total": total, "reads": reads, "writes": writes,
        "last_at": last_at, "last_path": last_path,
        "last_hour": hour, "last_day": day,
        "clients": dict(sorted(clients.items(), key=lambda kv: -kv[1])[:8]),
        "by_path": dict(sorted(by_path.items(), key=lambda kv: -kv[1])[:6]),
    }
    _cache["memory"] = (now, out)
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
        "alerts": alerts_banner(),
        "snapraid": snapraid(),
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


def cam_list():
    """Cameras que existem de verdade: chaves do /api/streams do go2rtc.

    Antes o feed anunciava "cam", "cam2" e "samsung" fixos; "cam2" nao existe,
    e o painel desenhava 4 tiles com dois OFF.
    """
    cfg = cam_cfg()
    base = cfg.get("GO2RTC_URL", "http://127.0.0.1:1984")
    auth = (cfg.get("GO2RTC_USER", ""), cfg.get("GO2RTC_PASS", ""))
    try:
        r = requests.get(base + "/api/streams", auth=auth, timeout=3)
        if r.ok:
            nomes = [n for n in (r.json() or {}) if n]
            if nomes:
                return sorted(nomes)[:4]
    except Exception:
        pass
    return ["cam", "samsung"]


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


# --- video fluido: um ffmpeg persistente por camera+tamanho -----------------
# Antes cada quadro custava curl+ffmpeg (0,4 a 3 s) e o cache devolvia o mesmo
# quadro por ate 3 s -> ~0,5 fps, que na tela vira "travado". Agora um ffmpeg
# fica lendo o relay RTSP do go2rtc (127.0.0.1:8554) e o ultimo quadro decodificado
# e servido na hora, sem processo novo por requisicao.
_camlive = {}
_camlive_ts = {}
_camlive_th = {}
_camlive_lock = threading.Lock()
CAM_FPS = 15


def cam_live_loop(src, w, h):
    key = (src, w, h)
    need = w * h * 3
    url = "rtsp://127.0.0.1:8554/" + src
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
           "-fflags", "nobuffer", "-flags", "low_delay",
           "-rtsp_transport", "tcp", "-i", url, "-an",
           "-vf", "fps=%d,scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d"
                  % (CAM_FPS, w, h, w, h),
           "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
    while time.time() - _camwant.get(key, 0) < 20:        # para quando ninguem pede
        p = None
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            while time.time() - _camwant.get(key, 0) < 20:
                buf = p.stdout.read(need)
                if not buf or len(buf) < need:
                    break
                with _camlive_lock:
                    _camlive[key] = buf
                    _camlive_ts[key] = time.time()
        except Exception:
            pass
        finally:
            if p is not None:
                try:
                    p.kill()
                    p.wait(timeout=3)
                except Exception:
                    pass
        time.sleep(1)
    with _camlive_lock:
        _camlive.pop(key, None)
        _camlive_ts.pop(key, None)
        _camlive_th.pop(key, None)


def cam_live_start(src, w, h):
    key = (src, w, h)
    _camwant[key] = time.time()
    if key not in _camlive_th:
        th = threading.Thread(target=cam_live_loop, args=(src, w, h), daemon=True)
        _camlive_th[key] = th
        th.start()


def start_cam_workers(w=320, h=180):
    # nada a pre-aquecer: o ffmpeg persistente sobe no primeiro pedido do painel
    return


def cam_rgb(src, w, h, espera=4.0):
    key = (src, w, h)
    cam_live_start(src, w, h)
    with _camlive_lock:
        buf = _camlive.get(key)
        ts = _camlive_ts.get(key, 0)
    if buf and time.time() - ts < 2.0:
        return buf
    hit = _camthumb.get(key)
    if hit and time.time() - hit[0] < 3:
        return hit[1]
    # partida fria: espera o fluxo produzir o primeiro quadro
    fim = time.time() + espera
    while time.time() < fim:
        time.sleep(0.05)
        _camwant[key] = time.time()
        with _camlive_lock:
            buf = _camlive.get(key)
        if buf:
            return buf
    data = cam_gen(src, w, h)     # ultimo recurso: snapshot do go2rtc
    if data:
        _camthumb[key] = (time.time(), data)
        return data
    return hit[1] if hit else b""


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



# ---------------------------------------------------------------- audio level
# Nivel do audio das cameras, em dBFS, lido com ffmpeg do RTSP do go2rtc.
# Publicado no /screen.txt:
#   aud=<fonte>:<dB>:<alto>,...   alto = 1/0, com histerese de AUDIO_HOLD
#   db=<dB>|<fonte>               (compatibilidade: painel antigo)
AUDIO_LOUD = -28          # dBFS: a partir daqui o som conta como "relativamente alto"
AUDIO_HOLD = 1.2          # s que o alerta fica aceso depois do som
_AUDIO = {}               # src -> {"db": int|None, "ts": float, "until": float}
_AUDIO_RE = re.compile(r"(?:RMS level dB:|Overall\.RMS_level=)\s*(-?\d+(?:\.\d+)?)")


def audio_srcs():
    cfg = cam_cfg()
    raw = (cfg.get("AUDIO_SRCS") or "cam,samsung").strip()
    return [x.strip() for x in raw.split(",") if x.strip()]


def audio_url(src):
    cfg = cam_cfg()
    tpl = (cfg.get("AUDIO_URL") or "rtsp://127.0.0.1:8554/{src}").strip()
    return tpl.replace("{src}", src)


def audio_state(src):
    return _AUDIO.setdefault(src, {"db": None, "ts": 0.0, "until": 0.0})


def audio_worker(src):
    """Le o audio de uma camera e guarda o RMS em dBFS."""
    import subprocess
    while True:
        st = audio_state(src)
        try:
            url = audio_url(src)
            # -rtsp_transport so existe no demuxer RTSP; em arquivo/teste quebra o comando
            extra = ["-rtsp_transport", "tcp"] if url.lower().startswith("rtsp") else []
            # astats so imprime o resumo no fim do stream; com ametadata=print os
            # valores saem a cada quadro no stdout (foi por isso que aud= ficava em "x").
            proc = subprocess.Popen(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", *extra,
                 "-i", url, "-vn", "-af", "astats=metadata=1:reset=1,ametadata=print:file=-",
                 "-f", "null", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        except Exception:
            st["db"], st["ts"] = None, time.time()
            time.sleep(6)
            continue
        last = 0.0
        try:
            for line in proc.stdout:
                m = _AUDIO_RE.search(line)
                if m:
                    valor = float(m.group(1))
                    if -120 <= valor <= 0:
                        st["db"] = int(round(valor))
                        st["ts"] = time.time()
                        last = time.time()
                        if valor >= AUDIO_LOUD:
                            st["until"] = time.time() + AUDIO_HOLD
                if last and time.time() - last > 12:      # sem leitura: stream morreu
                    break
        except Exception:
            pass
        finally:
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:
                pass
        # nao zera o nivel ao fim do stream: mantem a ultima leitura por alguns
        # segundos (audio_state_all descarta depois de 8 s) e evita piscar "--"
        st["ts"] = time.time()
        time.sleep(5)


def start_audio_workers():
    for src in audio_srcs():
        threading.Thread(target=audio_worker, args=(src,), daemon=True).start()


def audio_state_all():
    out = {}
    for src in audio_srcs():
        st = audio_state(src)
        fresco = st["db"] is not None and time.time() - st["ts"] < 8
        out[src] = {
            "db": st["db"] if fresco else None,
            "loud": 1 if (fresco and time.time() < st["until"]) else 0,
            "ts": round(st["ts"], 1),
        }
    return out


def audio_feed_lines():
    # AUDIO_FAKE_DB=-12 injeta um nivel fixo (so para testar o painel sem camera no ar)
    fake = os.environ.get("AUDIO_FAKE_DB")
    if fake:
        try:
            nivel = int(float(fake))
        except ValueError:
            nivel = -60
        return [f"aud=cam:{nivel}:{1 if nivel >= AUDIO_LOUD else 0}", f"db={nivel}|cam"]
    partes, lidos = [], []
    for src, v in audio_state_all().items():
        if v["db"] is not None:
            partes.append(f"{src}:{v['db']}:{v['loud']}")
            lidos.append((v["db"], src))
    if not partes:
        return ["aud=x", "db=x|cam"]
    lidos.sort(reverse=True)
    return ["aud=" + ",".join(partes), f"db={lidos[0][0]}|{lidos[0][1]}"]


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
    # resumo do alerta para o painel do iDBox (fonte bitmap: so ASCII, curto).
    # O painel esta na sala: cobre o caso "o PC do alerta esta desligado".
    _al = (alerts_banner() or {}).get("alerts") or []
    _crit = [a for a in _al if a.get("level") == "crit"]
    # contrato com o painel (Devlelino): SEM alerta de banner nenhuma das duas
    # linhas existe; altx so aparece quando ha critico (e o texto da faixa).
    if _al:
        lines.append("alt=%d|%d" % (len(_al), len(_crit)))
    if _crit:
        _txt = "".join(ch for ch in str(_crit[0].get("text") or "")
                       if 32 <= ord(ch) < 127 and ch != "|")[:46]
        lines.append("altx=" + (_txt or "ver dash"))
    for src in cam_list():
        lines.append(f"c={src}")
    lines.extend(audio_feed_lines())
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

    def _cam_stream(self, src, w, h):
        """Manda quadro cru (rgb24) continuamente na mesma conexao.

        O painel lia um wget por quadro: fork+exec 15x/s naquele ARM fraco, com
        falhas e ~5 fps. Aqui ele abre um socket e recebe o quadro mais novo.
        """
        key = (src, w, h)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        cam_live_start(src, w, h)
        last = 0.0
        started = time.time()
        try:
            while True:
                _camwant[key] = time.time()
                with _camlive_lock:
                    buf = _camlive.get(key)
                    ts = _camlive_ts.get(key, 0)
                if buf and ts > last:
                    self.wfile.write(buf)
                    self.wfile.flush()
                    last = ts
                elif time.time() - max(last, started) > 15:
                    break
                time.sleep(0.008)
        except Exception:
            pass

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
            elif p == "/api/alerts":
                qs = {}
                if "?" in self.path:
                    for par in self.path.split("?", 1)[1].split("&"):
                        if "=" in par:
                            k, v = par.split("=", 1)
                            qs[k] = v
                self._json(api_alerts(qs))
            elif p in ("/financeiro", "/financeiro/"):
                self._file("financeiro.html")
            elif p == "/alerts.json":
                self._json(alerts() or {"ts": time.time(), "ok": True, "alerts": []})
            elif p == "/history":
                with _lock:
                    self._json({k: list(HIST[k]) for k in HIST})
            elif p == "/network":
                self._json(network())
            elif p == "/memory":
                self._json(memory_mcp())
            elif p == "/health":
                self._json({"ok": True, "ts": time.time()})
            elif p == "/screen.txt":
                self._raw(screen_feed(), "text/plain; charset=utf-8")
            elif p == "/weather":
                self._json(weather())
            elif p == "/weather.txt":
                self._raw(weather_feed(), "text/plain; charset=utf-8")
            elif p == "/api/audio":
                self._json({"ts": time.time(), "srcs": audio_state_all()})
            elif p.startswith("/cam/"):
                src = p[5:].split(".")[0] or "cam"
                ext = p.rsplit(".", 1)[-1] if "." in p else ""
                q = self.path.split("?")[1] if "?" in self.path else ""
                w = h = None
                for kv in q.split("&"):
                    if kv.startswith("w="):
                        w = int(kv[2:])
                    if kv.startswith("h="):
                        h = int(kv[2:])
                w = w or 136
                h = h or 76
                if ext == "raw":
                    self._cam_stream(src, w, h)      # fluxo continuo, uma conexao
                else:
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
            m = re.match(r"^/api/snapraid/(start|cancel)$", p)
            if m:
                res = snapraid_action(m.group(1), self.client_address[0])
                self._json(res, 200 if res.get("ok") else 409)
                return
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
    start_audio_workers()
    threading.Thread(target=docker_stats_loop, daemon=True).start()
    threading.Thread(target=_services_loop, daemon=True).start()
    threading.Thread(target=_run_scan, daemon=True).start()
    threading.Thread(target=lambda: [(_run_scan(), time.sleep(25)) for _ in iter(int, 1)], daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    print(f"oma-dash on :{PORT}", flush=True)
    srv.serve_forever()
