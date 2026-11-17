#!/usr/bin/env python3
"""hubstats — estatísticas de uso dos serviços do omaserver.

Lê o que já existe na máquina, sem agente em cada serviço:
  · docker stats  -> bytes rx/tx por container (delta por amostra)
  · docker logs   -> access log de quem loga requisição (nginx, http.server, uvicorn)
  · probe HTTP    -> no ar / fora do ar

Grava em SQLite (/home/server/hubstats/hubstats.db) e publica
/home/server/lan-hub/stats.json, servido pelo próprio hub (mesma origem).

Serviço: hubstats.service (systemd), amostra a cada 60 s.
"""
import json
import os
import re
import socket
import ssl
import sqlite3
import subprocess
import sys
import time
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "hubstats.db")
OUT = "/home/server/lan-hub/stats.json"
LAN_IP = "192.168.5.245"
HOST_IP = LAN_IP
PERIOD = 60
RETENTION_DAYS = 120

# kind: lan = só rede local · cf = sai pelo túnel Cloudflare
# log:  nginx | pyserv | uvicorn | None
SERVICES = [
    dict(id="hub",      name="Hub",           kind="lan", port=80,    url=f"http://{LAN_IP}:80/",        container="oma-hub",      log="pyserv"),
    dict(id="dash",     name="OMA//DASH",     kind="lan", port=1985,  url=f"http://{LAN_IP}:1985/health", container="oma-dash",     log=None),
    dict(id="arquivos", name="Arquivos",      kind="lan", port=8082,  url=f"http://{LAN_IP}:8082/",       container="filebrowser",  log=None),
    dict(id="qbit",     name="qBittorrent",   kind="lan", port=8081,  url=f"http://{LAN_IP}:8081/",       container="qbittorrent",  log=None),
    dict(id="gtavc",    name="GTA Vice City", kind="lan", port=8083,  url=f"https://{LAN_IP}:8083/",      container="gtavc-web",    log="nginx", tls=True),
    dict(id="jogos",    name="Jogos",         kind="lan", port=8008,  url=f"http://{LAN_IP}:8008/",       container="gtavc-page",   log="pyserv"),
    dict(id="retro",    name="Retro Hub",     kind="lan", port=8097,  url=f"http://{LAN_IP}:8097/",       container="romm",         log="nginx"),
    dict(id="cameras",  name="Câmeras",       kind="lan", port=1984,  url=f"http://{LAN_IP}:1984/",       container="cams",         log=None),
    dict(id="pericia",  name="Perícias",      kind="lan", port=8085,  url=f"http://{LAN_IP}:8085/",       container="pericia-site", log="nginx"),
    dict(id="omanet",   name="OMA//NET",      kind="lan", port=20211, url=f"http://{LAN_IP}:20211/",      container="omanet",       log=None),
    dict(id="mobipato", name="Mobipato",      kind="cf",  port=8095,  url=f"http://{LAN_IP}:8095/",       container="mobipato",     log="nginx",
         host="mobipato.stellacorp.com.br"),
    dict(id="patinho",  name="Patinho",       kind="lan", port=8090,  url=f"http://{LAN_IP}:8090/health", container="patinho",      log="uvicorn",
         host="patopro.stellacorp.com.br"),
    dict(id="memoria",  name="Memória do time", kind="lan", port=8100, url=f"http://{LAN_IP}:8100/ui",   container="memory-memory-api-1", log=None),
    dict(id="jellyfin", name="Jellyfin",      kind="cf",  port=8096,  url=f"http://{LAN_IP}:8096/health", container="jellyfin",     log=None,
         host="patoflix.stellacorp.com.br"),
    dict(id="mobibeltrao", name="Mobi//Beltrão", kind="cf", port=8098, url=f"http://{LAN_IP}:8098/", container="mobibeltrao", log="nginx",
         host="mobibeltrao.stellacorp.com.br"),
    dict(id="mobi",     name="Mobi portal",  kind="cf",  port=8099,  url=f"http://{LAN_IP}:8099/",       container="mobi-portal",  log="nginx",
         host="mobi.stellacorp.com.br"),
]
BY_ID = {s["id"]: s for s in SERVICES}

RE_NGINX = re.compile(
    r'^(?P<ip>\S+) \S+ \S+ \[(?P<ts>[^\]]+)\] "(?P<req>[^"]*)" (?P<code>\d{3}) (?P<bytes>\S+)'
    r'(?: "(?P<ref>[^"]*)" "(?P<ua>[^"]*)")?')
RE_PYSERV = re.compile(
    r'^(?P<ip>\S+) - - \[(?P<ts>[^\]]+)\] "(?P<req>[^"]*)" (?P<code>\d{3}) (?P<bytes>\S+)')
RE_UVICORN = re.compile(
    r'^(?:INFO:\s+)?(?P<ip>[0-9a-fA-F.:]+):\d+ - "(?P<req>[^"]*)" (?P<code>\d{3})')
RE_DOCKERTS = re.compile(r'^(?P<ts>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+)Z? (?P<rest>.*)$', re.S)
PARSE = {"nginx": RE_NGINX, "pyserv": RE_PYSERV, "uvicorn": RE_UVICORN}


def db():
    conn = sqlite3.connect(DB, timeout=20)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS sample(
        ts INTEGER NOT NULL, svc TEXT NOT NULL, up INTEGER NOT NULL,
        rx INTEGER NOT NULL DEFAULT 0, tx INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(ts, svc))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS access(
        hr INTEGER NOT NULL, svc TEXT NOT NULL, ip TEXT NOT NULL, cf INTEGER NOT NULL DEFAULT 0,
        reqs INTEGER NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(hr, svc, ip, cf))""")
    conn.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sample)")}
    if "cpu" not in cols:
        conn.execute("ALTER TABLE sample ADD COLUMN cpu REAL NOT NULL DEFAULT -1")
    if "mem" not in cols:
        conn.execute("ALTER TABLE sample ADD COLUMN mem INTEGER NOT NULL DEFAULT -1")
    return conn


def meta_get(conn, k, default=None):
    row = conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row[0] if row else default


def meta_set(conn, k, v):
    conn.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                 (k, str(v)))


def sh(cmd, timeout=25):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def parse_size(s):
    m = re.match(r'^\s*([0-9.]+)\s*([a-zA-Z]*)\s*$', s)
    if not m:
        return 0
    v = float(m.group(1))
    u = m.group(2).lower()
    return int(v * {"b": 1, "": 1, "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3,
                    "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3}.get(u, 1))


def docker_netio():
    """Bytes rx/tx por container, exatos: soma /proc/<pid>/net/dev do netns dele.

    docker stats arredonda para 3 dígitos (20.3MB), o que zera deltas pequenos.
    Containers em network_mode=host compartilham o netns do host: aí não dá para
    atribuir o tráfego ao serviço, então ficam de fora (a UI mostra "—").
    """
    raw = sh("docker ps -q | xargs docker inspect --format '{{.Name}}|{{.HostConfig.NetworkMode}}|{{.State.Pid}}'",
             timeout=15)
    res = {}
    for line in raw.splitlines():
        parts = line.split("|")
        if len(parts) != 3:
            continue
        name, mode, pid = parts[0].lstrip("/"), parts[1], parts[2]
        if mode == "host":
            res[name] = None
            continue
        try:
            with open(f"/proc/{int(pid)}/net/dev") as f:
                lines = f.readlines()[2:]
            rx = tx = 0
            for ln in lines:
                iface, data = ln.split(":", 1)
                if iface.strip() == "lo":
                    continue
                f2 = data.split()
                rx += int(f2[0])
                tx += int(f2[8])
            res[name] = (rx, tx)
        except Exception:
            res[name] = None
    return res


def docker_usage():
    """CPU% e memória (bytes) por container, na janela de ~1 s do docker stats."""
    out = sh("docker stats --no-stream --format '{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}'", timeout=30)
    res = {}
    for line in out.splitlines():
        p = line.split("|")
        if len(p) < 3:
            continue
        name = p[0].strip()
        try:
            cpu = float(p[1].strip().rstrip("%"))
        except Exception:
            cpu = -1.0
        memtxt = p[2].split("/")[0].strip()
        try:
            mem = parse_size(memtxt)
        except Exception:
            mem = -1
        res[name] = (cpu, mem)
    return res


def probe(url, marked=False, timeout=2.0, tls=False):
    if marked:
        url += ("&" if "?" in url else "?") + "probe=1"
    ctx = ssl._create_unverified_context() if tls else None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "hubstats/1"})
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return 200 <= r.status < 500
    except urllib.error.HTTPError as e:
        return e.code < 500
    except Exception:
        return False


def from_tunnel(ip):
    """cloudflared entrega a partir do host, do gateway docker ou de 127.0.0.1."""
    return ip == HOST_IP or ip.startswith("172.") or ip.startswith("127.") or ip == "::1"


def scan_logs(conn, svc):
    if not svc.get("log"):
        return
    key = "cursor:" + svc["id"]
    cursor = meta_get(conn, key)
    cmd = "docker logs --timestamps"
    if cursor:
        cmd += f" --since '{cursor}'"
    cmd += f" {svc['container']} 2>&1 | tail -n 8000"
    out = sh(cmd)
    if not out:
        return
    rx = PARSE[svc["log"]]
    newest, n = None, 0
    for raw in out.splitlines():
        m = RE_DOCKERTS.match(raw)
        if not m:
            continue                       # linha sem timestamp do docker = continuação
        ts, line = m.group("ts"), m.group("rest")
        if cursor and ts <= cursor:
            continue                       # --since é inclusivo
        a = rx.search(line.strip())
        if not a:
            continue
        path = a.group("req").split(" ")[1] if " " in a.group("req") else ""
        if "probe=1" in path or path.startswith("/health"):
            continue                       # nosso próprio probe / healthcheck
        ip = a.group("ip").strip()
        b = a.groupdict().get("bytes") or "-"
        nbytes = 0 if b in ("-", "") else int(b)
        cf = 1 if (svc["kind"] == "cf" and from_tunnel(ip)) else 0
        try:
            hr = int(datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
                     .replace(tzinfo=timezone.utc).timestamp()) // 3600
        except Exception:
            hr = int(time.time()) // 3600
        conn.execute("""INSERT INTO access(hr,svc,ip,cf,reqs,bytes) VALUES(?,?,?,?,1,?)
            ON CONFLICT(hr,svc,ip,cf) DO UPDATE SET
              reqs=reqs+1, bytes=bytes+excluded.bytes""", (hr, svc["id"], ip, cf, nbytes))
        n += 1
        if newest is None or ts > newest:
            newest = ts
    if newest:
        meta_set(conn, key, newest)
    if n:
        sys.stderr.write(f"{svc['id']}: +{n} requisições\n")


def hour_of(ts):
    return int(ts) // 3600


def aggregate(conn, svc_id, since_ts):
    rx, tx, ups, total, net_samples, cpu_avg, cpu_max, mem_last, mem_max = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN rx>=0 THEN rx END),0),"
        " COALESCE(SUM(CASE WHEN tx>=0 THEN tx END),0),"
        " COALESCE(SUM(up),0), COUNT(*),"
        " COALESCE(SUM(CASE WHEN rx>=0 THEN 1 ELSE 0 END),0),"
        " COALESCE(AVG(CASE WHEN cpu>=0 THEN cpu END),-1),"
        " COALESCE(MAX(CASE WHEN cpu>=0 THEN cpu END),-1),"
        " COALESCE((SELECT mem FROM sample WHERE svc=? AND mem>=0 ORDER BY ts DESC LIMIT 1),-1),"
        " COALESCE(MAX(CASE WHEN mem>=0 THEN mem END),-1)"
        " FROM sample WHERE svc=? AND ts>=?", (svc_id, svc_id, since_ts)).fetchone()
    reqs, nbytes, uniq = conn.execute(
        "SELECT COALESCE(SUM(reqs),0), COALESCE(SUM(bytes),0), COUNT(DISTINCT ip)"
        " FROM access WHERE svc=? AND hr>=?", (svc_id, hour_of(since_ts))).fetchone()
    cf_reqs, cf_bytes, cf_uniq = conn.execute(
        "SELECT COALESCE(SUM(reqs),0), COALESCE(SUM(bytes),0), COUNT(DISTINCT ip)"
        " FROM access WHERE svc=? AND hr>=? AND cf=1", (svc_id, hour_of(since_ts))).fetchone()
    return {"rx": rx, "tx": tx, "samples": total, "netSamples": net_samples,
            "cpu": round(cpu_avg, 1), "cpuMax": round(cpu_max, 1),
            "mem": mem_last, "memMax": mem_max,
            "uptime": round(100.0 * ups / total, 2) if total else None,
            "reqs": reqs, "bytes": nbytes, "uniq": uniq,
            "cf_reqs": cf_reqs, "cf_bytes": cf_bytes, "cf_uniq": cf_uniq}


def publish(conn):
    now = time.time()
    out = {"ts": int(now), "host": socket.gethostname(), "period": PERIOD,
           "services": [], "totals": {}}
    for svc in SERVICES:
        row = conn.execute("SELECT ts, up FROM sample WHERE svc=? ORDER BY ts DESC LIMIT 1",
                           (svc["id"],)).fetchone()
        status = None
        if row:
            streak = conn.execute(
                "SELECT MIN(ts) FROM sample WHERE svc=? AND ts > "
                "COALESCE((SELECT MAX(ts) FROM sample WHERE svc=? AND up<>?),0)",
                (svc["id"], svc["id"], row[1])).fetchone()
            status = {"up": bool(row[1]), "checked": row[0], "since": streak[0] if streak else row[0]}
        item = {"id": svc["id"], "name": svc["name"], "kind": svc["kind"], "port": svc["port"],
                "host": svc.get("host"), "hasLog": bool(svc.get("log")), "status": status, "w": {}}
        for w, span in (("d1", 86400), ("d7", 7 * 86400), ("d30", 30 * 86400)):
            item["w"][w] = aggregate(conn, svc["id"], now - span)
        out["services"].append(item)
    for w in ("d1", "d7", "d30"):
        out["totals"][w] = {
            "tx": sum(s["w"][w]["tx"] for s in out["services"]),
            "rx": sum(s["w"][w]["rx"] for s in out["services"]),
            "reqs": sum(s["w"][w]["reqs"] for s in out["services"])}
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, OUT)
    os.chmod(OUT, 0o644)


def sample_once(conn):
    now = int(time.time())
    netio = docker_netio()
    usage = docker_usage()
    for svc in SERVICES:
        up = 1 if probe(svc["url"], marked=bool(svc.get("log")), tls=bool(svc.get("tls"))) else 0
        rx = tx = -1                      # -1 = não atribuível (container em rede host)
        cur = netio.get(svc["container"])
        if cur:
            cum = meta_get(conn, "cum:" + svc["id"])
            if cum:
                crx, ctx = (int(x) for x in cum.split(":"))
                rx, tx = max(0, cur[0] - crx), max(0, cur[1] - ctx)
            else:
                rx = tx = 0               # primeira amostra: só grava a base
            meta_set(conn, "cum:" + svc["id"], f"{cur[0]}:{cur[1]}")
        cpu, mem = usage.get(svc["container"], (-1.0, -1))
        conn.execute("INSERT OR REPLACE INTO sample(ts,svc,up,rx,tx,cpu,mem) VALUES(?,?,?,?,?,?,?)",
                     (now, svc["id"], up, rx, tx, cpu, mem))
        try:
            scan_logs(conn, svc)
        except Exception as e:
            sys.stderr.write(f"log {svc['id']}: {e}\n")
    conn.execute("DELETE FROM sample WHERE ts < ?", (now - RETENTION_DAYS * 86400,))
    conn.execute("DELETE FROM access WHERE hr < ?",
                 (int(now - RETENTION_DAYS * 86400) // 3600,))
    conn.commit()


def main():
    conn = db()
    if len(sys.argv) > 1 and sys.argv[1] == "--once":
        sample_once(conn)
        publish(conn)
        data = json.load(open(OUT))
        print(json.dumps({"ts": data["ts"], "servicos": len(data["services"]),
                          "status": {s["id"]: s["status"]["up"] for s in data["services"]}},
                         ensure_ascii=False))
        return
    while True:
        t0 = time.time()
        try:
            sample_once(conn)
            publish(conn)
        except Exception as e:
            sys.stderr.write(f"erro no ciclo: {e}\n")
        time.sleep(max(5, PERIOD - (time.time() - t0)))


if __name__ == "__main__":
    main()
