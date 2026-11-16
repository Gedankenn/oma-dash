#!/usr/bin/env python3
"""Mantem as listas do LAN hub (http://192.168.5.245/) atualizadas.

O hub tem duas listas dentro do index.html:
  SERVICES -> servicos da LAN (nome, papel, porta, tls)
  CF       -> hostnames publicos atras do tunnel Cloudflare

Antes isso era editado a mao e ficava velho. Agora:
  - LAN: descobre as portas TCP escutando em 0.0.0.0 (docker ou systemd) e
    acrescenta as que faltam, preservando nome/papel que ja estavam curados.
  - Publico: le o ingress do tunnel patoflix no Cloudflare e monta a lista.

Roda por timer systemd (--user) a cada 15 min. Faz backup antes de escrever e
so grava se as duas listas continuarem validas.
"""
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time

HUB = "/home/server/lan-hub/index.html"
ALERTS = "/home/server/dash/alerts.json"
CFAPI = "/home/server/.local/bin/cfapi"
ACCT = "08e9fba150dbca457e8addcc51d7d72d"
TUNNEL = "6b647d9e-42b3-4a95-91e7-589fc3c0ef66"

# nome e papel curados por porta (o que o dono ja tinha + o que faltava)
# 16/11: o dono pediu de volta os nomes antigos; so o hub continua NETWATCH
META = {
    8096: ("Jellyfin", "cinema em casa", False),
    8006: ("Windows", "dockurr · viewer", False),
    8082: ("Arquivos", "file manager · downloads", False),
    8083: ("GTA Vice City", "no navegador", True),
    8008: ("Jogos", "biblioteca oma:// neste PC", False),
    8097: ("Retro Hub", "joguinhos no navegador", False),
    8081: ("qBittorrent", "torrents", False),
    1984: ("Câmeras", "go2rtc", False),
    1985: ("OMA // DASH", "console do servidor", False),
    20211: ("OMA // NET", "dispositivos · presença", False),
    8085: ("PERÍCIAS", "casos · laudos · autos", False),
}
ORDEM = [8096, 1985, 1984, 20211, 8085, 8081, 8082, 8083, 8008, 8097, 8006]
# portas que nunca entram na lista (o proprio hub, ssh, bancos, relay interno)
FORA = {22, 53, 80, 443, 111, 3306, 5432, 6379, 6881, 8080, 8554, 8555, 8787, 2049,
        8090, 8095, 8098, 8099}   # 8090/95/98/99 = coluna Cloudflare
# porta efemera (37610, 49536...) nao e servico
EFEMERA = 30000

NOMES_HOST = {
    "mobipato": "Mobipato",
    "mobibeltrao": "Beltrão",
    "mobi": "Portal",
    "patopro": "Patinho",
    "patoprodemo": "Patinho demo",
    "patoflix": "Jellyfin",
    "qtorrent": "qBittorrent",
    "drive": "Arquivos",
    "stellacorp": "stellacorp",
    "www": "stellacorp",
}
ORDEM_HOST = ["mobipato", "mobibeltrao", "mobi", "patopro", "patoprodemo",
              "patoflix", "qtorrent", "drive", "stellacorp"]


def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return ""


def portas_lan():
    """portas TCP escutando fora do loopback"""
    out = run(["ss", "-ltnH"])
    portas = set()
    for linha in out.splitlines():
        partes = linha.split()
        if len(partes) < 4:
            continue
        local = partes[3]
        if local.startswith("127.") or local.startswith("[::1]"):
            continue
        m = re.search(r":(\d+)$", local)
        if m:
            portas.add(int(m.group(1)))
    return {p for p in portas if p not in FORA and p < EFEMERA}


def dono_das_portas():
    """porta publicada -> nome do container"""
    mapa = {}
    out = run(["docker", "ps", "--format", "{{.Names}}\t{{.Ports}}"])
    for linha in out.splitlines():
        if "\t" not in linha:
            continue
        nome, ports = linha.split("\t", 1)
        for m in re.finditer(r"(?:0\.0\.0\.0|\[::\]):(\d+)->", ports):
            mapa[int(m.group(1))] = nome
    return mapa


def bloco(s, nome):
    m = re.search(r"(?:const|let|var)\s+" + nome + r"\s*=\s*\[", s)
    if not m:
        return None
    ini = m.end() - 1
    nivel = 0
    for i in range(ini, len(s)):
        if s[i] == "[":
            nivel += 1
        elif s[i] == "]":
            nivel -= 1
            if nivel == 0:
                return (ini, i + 1)
    return None


def ler_servicos(s):
    r = bloco(s, "SERVICES")
    if not r:
        return []
    trecho = s[r[0]:r[1]]
    itens = []
    for m in re.finditer(
            r'\{\s*name:\s*"([^"]*)"\s*,\s*role:\s*"([^"]*)"\s*,\s*port:\s*(\d+)\s*,\s*tls:\s*(true|false)\s*\}',
            trecho):
        itens.append(dict(name=m.group(1), role=m.group(2), port=int(m.group(3)),
                          tls=(m.group(4) == "true")))
    return itens


def montar_servicos(existentes):
    atual = {i["port"]: i for i in existentes}
    dono = dono_das_portas()
    vivos = portas_lan()
    final = {}
    # 1) curados: entram sempre (mesmo parados, ex. VM do Windows)
    for porta, (nome, papel, tls) in META.items():
        final[porta] = dict(name=nome, role=papel, port=porta, tls=tls)
    # 2) o que ja estava na lista e ainda escuta, sem meta: preserva como esta
    for porta, item in atual.items():
        if porta in final:
            continue
        if porta in vivos:
            final[porta] = item
    # 3) porta nova escutando, sem meta: entra sozinha
    for porta in sorted(vivos):
        if porta in final:
            continue
        c = dono.get(porta) or "porta %d" % porta
        final[porta] = dict(name=c, role="novo serviço", port=porta, tls=False)
    ordem = [p for p in ORDEM if p in final] + sorted(p for p in final if p not in ORDEM)
    return [final[p] for p in ordem]


def ler_cf(s):
    r = bloco(s, "CF")
    if not r:
        return []
    return [dict(id=m.group(1), name=m.group(2), host=m.group(3)) for m in re.finditer(
        r'\{\s*id:\s*"([^"]*)",\s*name:\s*"([^"]*)",\s*host:\s*"([^"]*)"\s*\}', s[r[0]:r[1]])]


def montar_cf(existentes):
    saida = run([CFAPI, "GET", "/accounts/%s/cfd_tunnel/%s/configurations" % (ACCT, TUNNEL)])
    hosts = []
    try:
        d = json.loads(saida)
        for r in d["result"]["config"]["ingress"]:
            h = (r.get("hostname") or "").strip()
            if h:
                hosts.append(h)
    except Exception:
        return existentes          # sem API: nao mexe
    vistos, final = set(), []
    for h in hosts:
        sub = h.split(".")[0]
        chave = "stellacorp" if sub in ("stellacorp", "www") else sub
        if chave in vistos:
            continue
        vistos.add(chave)
        final.append(dict(id=chave, name=NOMES_HOST.get(chave, chave), host=h))
    ordem = [h for h in ORDEM_HOST if any(f["id"] == h for f in final)]
    resto = [f for f in final if f["id"] not in ordem]
    return [f for h in ordem for f in final if f["id"] == h] + resto


def fmt_servicos(itens):
    linhas = []
    for i in itens:
        linhas.append('  { name: %-18s role: %-34s port: %-4d, tls: %s }'
                      % ('"%s",' % i["name"], '"%s",' % i["role"], i["port"],
                         "true " if i["tls"] else "false"))
    return "[\n" + ",\n".join(linhas) + "\n]"


def fmt_cf(itens):
    linhas = []
    for i in itens:
        linhas.append('    { id: %-14s name: %-16s host: "%s" }'
                      % ('"%s",' % i["id"], '"%s",' % i["name"], i["host"]))
    return "[\n" + ",\n".join(linhas) + "\n  ]"


ALERTA_INI = "<!-- ALERTAS:inicio -->"
ALERTA_FIM = "<!-- ALERTAS:fim -->"


def banner_alertas():
    """Faixa de alerta do oma-dash no topo do hub.

    O hub e a primeira pagina que o dono abre na LAN, entao o alerta de
    armazenamento aparece aqui tambem (o dash :1985 e o painel do iDBox ja
    mostram). Lido do alerts.json que o watch-health.py grava a cada 5 min;
    o hub e regerado a cada 15 min, entao a defasagem maxima e isso.
    """
    try:
        with open(ALERTS) as f:
            al = (json.load(f) or {}).get("alerts") or []
    except Exception:
        return ""
    if not al:
        return ""
    crit = [a for a in al if a.get("level") == "crit"]
    cor = ("#ff3b54", "#2a0b12") if crit else ("#ffb020", "#2a1f06")
    titulo = ("%d ALERTA (S) · %d CRITICO (S)" % (len(al), len(crit)) if crit
              else "%d AVISO (S)" % len(al))
    itens = "".join(
        '<li>%s</li>' % html.escape(str(a.get("text") or ""))[:160] for a in al[:5])
    resto = ('<li>+%d ...</li>' % (len(al) - 5)) if len(al) > 5 else ""
    return (
        ALERTA_INI + "\n"
        "<style>"
        ".hubalert{display:flex;gap:10px;align-items:flex-start;flex-wrap:wrap;"
        "margin:0 0 14px;padding:10px 14px;border:1px solid %s;border-radius:8px;"
        "background:%s;color:#fff;font-size:13px;line-height:1.45}"
        ".hubalert b{letter-spacing:.14em;font-size:11px;color:%s;text-transform:uppercase}"
        ".hubalert ul{margin:0;padding-left:18px}"
        "</style>\n"
        '<div class="hubalert"><b>%s</b><ul>%s%s</ul></div>\n'
        % (cor[0], cor[1], cor[0], html.escape(titulo), itens, resto)
        + ALERTA_FIM + "\n")


def aplica_alertas(s):
    """Troca (ou remove) o bloco de alertas, sem tocar no resto do HTML."""
    novo = banner_alertas()
    ini = s.find(ALERTA_INI)
    if ini >= 0:
        fim = s.find(ALERTA_FIM, ini)
        if fim < 0:
            return s
        fim += len(ALERTA_FIM) + 1
        return s[:ini] + novo + s[fim:]
    if not novo:
        return s
    # primeira vez: entra logo depois do </header> (antes do <main>)
    pos = s.find("</header>")
    if pos < 0:
        return s
    pos = s.find(">", pos) + 1
    return s[:pos] + "\n" + novo + s[pos:]


def main():
    if not os.path.exists(HUB):
        print("hub nao encontrado:", HUB)
        return 1
    s = open(HUB, encoding="utf-8").read()
    serv_antes = ler_servicos(s)
    cf_antes = ler_cf(s)
    serv = montar_servicos(serv_antes)
    cf = montar_cf(cf_antes)

    r1, r2 = bloco(s, "SERVICES"), bloco(s, "CF")
    if not r1 or not r2:
        print("blocos SERVICES/CF nao encontrados")
        return 1
    novo = s[:r1[0]] + fmt_servicos(serv) + s[r1[1]:]
    r2b = bloco(novo, "CF")
    novo = novo[:r2b[0]] + fmt_cf(cf) + novo[r2b[1]:]
    novo = aplica_alertas(novo)

    if novo == s:
        print("sem mudanca (%d servicos, %d publicos)" % (len(serv), len(cf)))
        return 0

    shutil.copy2(HUB, HUB + ".bak-sync-" + time.strftime("%Y%m%d-%H%M%S"))
    tmp = HUB + ".tmp"
    open(tmp, "w", encoding="utf-8").write(novo)
    if not (bloco(open(tmp, encoding="utf-8").read(), "SERVICES") and
            bloco(open(tmp, encoding="utf-8").read(), "CF")):
        os.unlink(tmp)
        print("abortado: bloco invalido depois da edicao")
        return 1
    os.replace(tmp, HUB)
    print("ok: %d servicos (%d antes), %d publicos (%d antes)"
          % (len(serv), len(serv_antes), len(cf), len(cf_antes)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
