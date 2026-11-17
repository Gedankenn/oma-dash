#!/usr/bin/env python3
"""watch-health.py — alertas de armazenamento, roda a cada 5 min (user timer).

Nasceu do incidente de 16/11: o sdb caiu as 05:35, o md127 (raid0) ficou `broken`,
/data virou EIO e nada avisou ninguem por 3h30. Aqui a checagem e barata e nao
precisa de root: /proc/mdstat, /proc/mounts, statvfs + listdir controlado,
disks.json (SMART diario) e espaco usado.

Grava /home/server/dash/alerts.json:
  {"ts": ..., "ok": bool, "alerts": [{"id","level","text"}]}
e uma linha em stderr/stdout por alerta (vai para o journal do timer).
"""
import json
import os
import signal
import sys
import time

OUT = "/home/server/dash/alerts.json"
DISKS_JSON = "/home/server/dash/disks.json"
MOUNTS = ("/", "/data", "/boot")
SPACE_WARN = 85
SPACE_CRIT = 92
TEMP_WARN = 55
# ids silenciados de proposito (o dono decidiu nao ver o banner deste HD)
MUTE = {"smart-sdd-fail"}
SMART_STALE_H = 48


def timeout_handler(signum, frame):
    raise TimeoutError("timeout")


class Checker:
    def __init__(self):
        self.alerts = []

    def add(self, ident, level, text):
        if ident in MUTE:
            return
        self.alerts.append({"id": ident, "level": level, "text": text})

    # --- RAID / md -------------------------------------------------------
    def md(self):
        try:
            with open("/proc/mdstat") as f:
                lines = f.read().splitlines()
        except Exception as e:
            self.add("mdstat", "warn", f"nao consegui ler /proc/mdstat: {e}")
            return
        for line in lines:
            if not line.startswith("md") or ":" not in line:
                continue
            name, rest = line.split(":", 1)
            name = name.strip()
            rest = rest.strip()
            low = rest.lower()
            if low.startswith("broken"):
                self.add(f"md-{name}-broken", "crit",
                         f"{name} esta BROKEN (array nao confiavel) — {rest.split(' ', 1)[-1]}")
            elif "degraded" in low:
                self.add(f"md-{name}-degraded", "crit",
                         f"{name} degradado — {rest.split(' ', 1)[-1]}")
            # membro faltando aparece como "sdX1[2](F)" ou sem o dispositivo
            members = [t for t in rest.split() if t.startswith(("sd", "nvme", "hd"))]
            for m in members:
                if "(F)" in m:
                    self.add(f"md-{name}-fail-{m.split('[')[0]}", "crit",
                             f"{m} e membro FALHO de {name}")
                dev = "/dev/" + m.split("[")[0]
                if not os.path.exists(dev):
                    self.add(f"md-{name}-missing-{m.split('[')[0]}", "crit",
                             f"{m.split('[')[0]} consta no {name} mas /dev nao existe "
                             f"(disco caiu ou foi removido)")
            # estado por sysfs (mais confiavel que o texto)
            state = f"/sys/block/{name}/md/array_state"
            try:
                with open(state) as f:
                    st = f.read().strip()
                if st in ("broken", "inactive", "suspended"):
                    self.add(f"md-{name}-state", "crit", f"{name} array_state={st}")
            except Exception:
                pass

    # --- sistemas de arquivos montados ------------------------------------
    def origin_ok(self, mount):
        """statvfs + listdir com timeout: e o teste que pegaria o /data EIO."""
        signal.alarm(8)
        try:
            os.statvfs(mount)
            os.listdir(mount)
            return None
        except TimeoutError:
            return "travou (>8s)"
        except OSError as e:
            # EACCES/EPERM = so falta permissao, nao disco ruim
            if e.errno in (1, 13):
                return None
            return f"{e.strerror or e} (errno {e.errno})"
        finally:
            signal.alarm(0)

    def mounts(self):
        try:
            with open("/proc/mounts") as f:
                mounted = {ln.split()[1] for ln in f if len(ln.split()) > 2}
        except Exception:
            mounted = set()
        for m in MOUNTS:
            if m not in mounted:
                if m != "/boot":
                    self.add(f"mount-{m}-absent", "crit", f"{m} nao esta montado")
                continue
            err = self.origin_ok(m)
            if err:
                self.add(f"mount-{m}-unreadable", "crit",
                         f"{m} ilegivel ({err}) — conteudo inacessivel")
                continue
            try:
                u = os.statvfs(m)
                used = 100.0 * (u.f_blocks - u.f_bfree) / u.f_blocks if u.f_blocks else 0
            except Exception:
                continue
            pct = round(used)
            if pct >= SPACE_CRIT:
                self.add(f"space-{m}", "crit", f"{m} com {pct}% usado")
            elif pct >= SPACE_WARN:
                self.add(f"space-{m}", "warn", f"{m} com {pct}% usado")

    # --- SMART (disks-health.py, 1x/dia) ----------------------------------
    def smart(self):
        try:
            with open(DISKS_JSON) as f:
                payload = json.load(f)
        except Exception as e:
            self.add("smart-missing", "warn", f"disks.json ilegivel: {e}")
            return
        age_h = (time.time() - payload.get("ts", 0)) / 3600
        if age_h > SMART_STALE_H:
            self.add("smart-stale", "warn",
                     f"SMART sem atualizar ha {age_h:.0f}h (disks-health.timer parado?)")
        for d in payload.get("disks", []):
            dev = d.get("device", "?")
            model = d.get("model") or ""
            if d.get("ok") is False:
                extra = ""
                if d.get("reallocated"):
                    extra += f", {d['reallocated']} setores realocados"
                self.add(f"smart-{dev}-fail", "crit",
                         f"{dev} {model} com SMART FALHANDO{extra}")
            elif d.get("ok") is None:
                self.add(f"smart-{dev}-nodata", "warn",
                         f"{dev} sem leitura SMART ({d.get('error', 'sem dados')})")
            t = d.get("temp")
            if isinstance(t, int) and t >= TEMP_WARN:
                self.add(f"temp-{dev}", "warn", f"{dev} a {t}C")

    def run(self):
        signal.signal(signal.SIGALRM, timeout_handler)
        for step in (self.md, self.mounts, self.smart):
            try:
                step()
            except Exception as e:
                self.add(f"check-{step.__name__}", "warn", f"checagem {step.__name__} falhou: {e}")
        payload = {"ts": int(time.time()), "ok": not self.alerts, "alerts": self.alerts}
        tmp = OUT + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(payload, f, indent=1)
            os.replace(tmp, OUT)
        except Exception as e:
            print(f"watch-health: nao consegui gravar {OUT}: {e}", file=sys.stderr)
        for a in self.alerts:
            print(f"[{a['level'].upper()}] {a['text']}", file=sys.stderr)
        print(f"watch-health: {len(self.alerts)} alerta(s)")
        return 0


if __name__ == "__main__":
    sys.exit(Checker().run())
