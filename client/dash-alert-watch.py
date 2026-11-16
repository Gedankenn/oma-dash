#!/usr/bin/env python3
"""dash-alert-watch.py — mostra no desktop os alertas do oma-dash.

Le http://192.168.5.245:1985/alerts.json (gravado pelo watch-health.py no
omaserver a cada 5 min) e dispara notify-send quando aparece alerta novo ou
quando um alerta critico continua ativo (lembrete a cada 6 h).

Roda por dash-alert-watch.timer (10 min). Estado em ~/.cache/dash-alert-watch.json.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

URL = "http://192.168.5.245:1985/alerts.json"
STATE = os.path.expanduser("~/.cache/dash-alert-watch.json")
LOG = os.path.expanduser("~/.cache/dash-alert-watch.log")
REMIND_S = 6 * 3600
STALE_S = 30 * 60          # watch-health parado/ha 30 min = alerta tambem


def log(msg):
    try:
        with open(LOG, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
        if os.path.getsize(LOG) > 200_000:
            os.replace(LOG, LOG + ".1")
    except Exception:
        pass


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        tmp = STATE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f)
        os.replace(tmp, STATE)
    except Exception as e:
        log(f"state: {e}")


def notify(title, body, urgency="normal"):
    try:
        subprocess.run(["notify-send", "-a", "OMA//DASH", "-u", urgency,
                        "-i", "drive-harddisk", title, body],
                       check=False, timeout=10)
        return True
    except Exception as e:
        log(f"notify-send falhou: {e}")
        return False


def fetch():
    req = urllib.request.Request(URL, headers={"User-Agent": "dash-alert-watch/1"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode())


def main():
    try:
        payload = fetch()
    except Exception as e:
        log(f"sem resposta do dash: {e}")
        return 0                      # servidor fora nao e alerta de disco
    active = {a["id"]: a for a in payload.get("alerts", []) if a.get("id")}
    # QA 17/11: produtor morto era silencio absoluto (o alerta mais importante
    # e justamente "a vigilancia parou"). Se o alerts.json esta velho, avisa.
    age = time.time() - float(payload.get("ts") or 0)
    if age > STALE_S:
        active["producer-stale"] = {
            "id": "producer-stale", "level": "crit",
            "text": f"watch-health sem atualizar ha {int(age / 60)} min — os alertas podem estar parados",
        }
    st = load_state()
    seen = {k: v for k, v in (st.get("seen") or {}).items() if k in active}
    now = time.time()
    novos, lembretes = [], []
    for ident, a in active.items():
        last = seen.get(ident)
        if last is None:
            novos.append(a)
            seen[ident] = now
        elif a.get("level") == "crit" and now - last > REMIND_S:
            lembretes.append(a)
            seen[ident] = now
    if novos or lembretes:
        crit = [a for a in novos + lembretes if a.get("level") == "crit"]
        head = []
        if novos:
            head.append(f"{len(novos)} novo(s)")
        if lembretes:
            head.append(f"{len(lembretes)} ainda ativo(s)")
        title = "OMA//DASH — " + " · ".join(head)
        body = "\n".join(a["text"] for a in (crit + [a for a in novos + lembretes if a not in crit])[:6])
        ok = notify(title, body, "critical" if crit else "normal")
        log(f"{title} | {body.replace(chr(10), ' ; ')} | notify={ok}")
    save_state({"seen": seen, "ts": now})
    return 0


if __name__ == "__main__":
    sys.exit(main())
