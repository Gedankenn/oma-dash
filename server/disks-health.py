#!/usr/bin/env python3
"""disks-health.py — saude SMART dos discos, 1x/dia.

Roda smartctl dentro de um container (cap SYS_RAWIO + device), porque smartctl
precisa de root e o servico do oma-dash roda como usuario `server`.
Grava /home/server/dash/disks.json com o resultado.
"""
import json
import os
import subprocess
import sys
import time

OUT = "/home/server/dash/disks.json"
IMAGE = os.environ.get("SMART_IMAGE", "smart-tools:1")


def disks():
    try:
        out = subprocess.run(["lsblk", "-d", "-n", "-o", "NAME,TYPE"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    keep = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2 or parts[1] != "disk":
            continue
        name = parts[0]
        if name.startswith(("sd", "nvme", "hd", "vd")):
            keep.append(name)
    return keep


def smart(dev):
    """smartctl -j dentro do container; devolve dict ou None."""
    cmd = ["docker", "run", "--rm", "--cap-add", "SYS_RAWIO",
           "--device", f"/dev/{dev}:/dev/{dev}", IMAGE,
           "smartctl", "-j", "-H", "-i", "-A", "-n", "standby", f"/dev/{dev}"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
    except Exception as e:
        return {"device": dev, "error": str(e)[:120]}
    raw = (p.stdout or "").strip()
    if not raw.startswith("{"):
        return {"device": dev, "error": (p.stderr or raw or "sem resposta")[:120]}
    try:
        return json.loads(raw)
    except Exception as e:
        return {"device": dev, "error": f"json: {e}"[:120]}


def summarize(dev, j):
    if not j or j.get("error"):
        return {"device": dev, "ok": None, "error": (j or {}).get("error", "sem dados")}
    passed = (j.get("smart_status") or {}).get("passed")
    model = (j.get("model_name") or j.get("device", {}).get("name") or "").strip()
    hours = (j.get("power_on_time") or {}).get("hours")
    temp = (j.get("temperature") or {}).get("current")
    wear = (j.get("nvme_smart_health_information_log") or {}).get("percentage_used")
    realloc = None
    for a in ((j.get("ata_smart_attributes") or {}).get("table") or []):
        if a.get("id") == 5:
            realloc = a.get("raw", {}).get("value")
    return {"device": dev, "model": model[:28], "ok": bool(passed) if passed is not None else None,
            "temp": temp, "hours": hours, "wear": wear, "reallocated": realloc}


def main():
    devs = disks()
    out = [summarize(d, smart(d)) for d in devs]
    payload = {"ts": int(time.time()), "disks": out}
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, OUT)
    bad = [d for d in out if d.get("ok") is False]
    print(f"disks-health: {len(out)} discos, {len(bad)} com falha")
    return 0        # falha de disco aparece no painel, nao como erro de servico


if __name__ == "__main__":
    sys.exit(main())
