#!/usr/bin/env python3
"""cf-probe.py — mede os hostnames publicos (Cloudflare) para o rack do hub.

Motivo: no hub :80 o rack "cloudflare · tunel" mostrava 6 de 9 hostnames em
"n/d" — o /stats.json do hubstats so conhece servico local, e os ids do rack
(mobipato, patopro, patoflex, qtorrent, drive, stellacorp…) nao existiam la.

Aqui cada hostname e testado de verdade pela internet (HTTPS pelo Cloudflare,
timeout curto) e o resultado sai no formato que o hub ja sabe pintar
(services[] no mesmo shape do stats.json, + "ms").

- status.up .......... a URL respondeu (2xx, 3xx, 401/403 = alcancavel)
- w.d1.uptime ........ % de sucesso nas ultimas 7 dias (2016 amostras de 5 min)
- ms ................. tempo da requisicao; o hub mostra "443 · N ms"

Grava /home/server/lan-hub/cf.json (servido em http://<hub>/cf.json) e o
historico em cf-history.json. Roda por cf-probe.timer (5 min, usuario server).
"""
import json
import os
import ssl
import time
import urllib.request

BASE = "/home/server/lan-hub"
OUT = os.path.join(BASE, "cf.json")
HIST = os.path.join(BASE, "cf-history.json")
MAXS = 2016                      # 7 dias x 288 amostras/dia
TIMEOUT = 8

HOSTS = [
    ("mobipato", "Mobipato", "mobipato.stellacorp.com.br"),
    ("mobibeltrao", "Beltrão", "mobibeltrao.stellacorp.com.br"),
    ("mobi", "Portal", "mobi.stellacorp.com.br"),
    ("patopro", "Patinho", "patopro.stellacorp.com.br"),
    ("patoprodemo", "Patinho demo", "patoprodemo.stellacorp.com.br"),
    ("patoflix", "Jellyfin", "patoflix.stellacorp.com.br"),
    ("qtorrent", "qBittorrent", "qtorrent.stellacorp.com.br"),
    ("drive", "Arquivos", "drive.stellacorp.com.br"),
    ("stellacorp", "stellacorp", "www.stellacorp.com.br"),
]


def probe(host):
    """Devolve (ok, code, ms). Cloudflare pode dar 403/302: conta como ok."""
    ctx = ssl.create_default_context()
    req = urllib.request.Request("https://%s/" % host,
                                 headers={"User-Agent": "oma-hub-probe/1"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
            return True, r.status, int((time.time() - t0) * 1000)
    except urllib.error.HTTPError as e:
        # respondeu: servidor existe atras do Cloudflare
        return True, e.code, int((time.time() - t0) * 1000)
    except Exception:
        return False, 0, int((time.time() - t0) * 1000)


def main():
    try:
        hist = json.load(open(HIST))
    except Exception:
        hist = {}
    ts = int(time.time())
    services = []
    for ident, name, host in HOSTS:
        ok, code, ms = probe(host)
        h = hist.get(ident) or {"s": ""}
        s = (h.get("s") or "") + ("1" if ok else "0")
        s = s[-MAXS:]
        hist[ident] = {"s": s, "host": host, "last": {"ok": ok, "code": code,
                        "ms": ms, "ts": ts}}
        total = len(s)
        uptime = round(100.0 * s.count("1") / total, 2) if total else (100.0 if ok else 0.0)
        services.append({
            "id": ident, "name": name, "kind": "cf", "port": 443, "host": host,
            "hasLog": False, "ms": ms, "code": code,
            "status": {"up": ok, "checked": ts},
            "w": {"d1": {"samples": max(total, 1), "uptime": uptime,
                         "rx": 0, "tx": 0, "netSamples": 0,
                         "cpu": -1, "mem": -1, "reqs": 0, "bytes": 0,
                         "uniq": 0, "cf_reqs": 0, "cf_bytes": 0, "cf_uniq": 0}},
        })
    for path, data in ((OUT, {"ts": ts, "period": 300, "services": services}),
                       (HIST, hist)):
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    ok = sum(1 for s in services if s["status"]["up"])
    print("cf-probe: %d/%d hostnames respondendo (%s)" % (
        ok, len(services),
        ", ".join("%s=%s" % (s["id"], s["ms"]) for s in services if s["status"]["up"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
