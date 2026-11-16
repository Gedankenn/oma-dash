#!/usr/bin/env bash
# backup-dumps.sh — extrai para /home/server/dash/db-dumps o que o rsync do
# backup diario NAO consegue ler (arquivos 600 de root/uid interno do container).
#
# Motivo (QA 17/11): omanet.db (root:root 600) e patinho/data/{admin.json,
# admin-password.txt,session.key} (uid 10001, 600) ficavam FORA do snapshot,
# e o rsync rc=23 era tolerado em silencio.
#
# O docker roda como root, entao o arquivo e lido por dentro do container e
# escrito aqui pelo usuario `server` (docker exec ... cat > arquivo). Para o
# SQLite o dump usa a API .backup do proprio python do container — snapshot
# consistente mesmo com WAL aberto.
#
# Concorrencia (QA 17/11, regressao R2): flock para nao rodar dois dumps ao
# mesmo tempo (o backup das 04:40 + uma execucao manual derrubavam o do dia) e
# escrita atomica (arquivo .tmp + mv) para o pull nunca ler arquivo pela metade.
set -uo pipefail

OUT=/home/server/dash/db-dumps
LOCK=$OUT/.lock

mkdir -p "$OUT/secrets"
chmod 700 "$OUT" "$OUT/secrets"
log() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }

exec 9>"$LOCK"
if ! flock -n 9; then
    # outro dump em andamento: os arquivos publicados continuam validos (mv atomico)
    log "ja existe dump em andamento — nada a fazer"
    exit 0
fi

publish() {  # tmp destino
    mv -f -- "$1" "$2" && chmod 600 "$2"
}

dump_db() {  # container caminho nome
    local c=$1 p=$2 n=$3 tmpc="/tmp/__dump.$$.db"
    local out="$OUT/$n.db" tmp="$OUT/.$n.db.$$"
    if docker exec "$c" python -c \
        "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close()" \
        "$p" "$tmpc" >/dev/null 2>&1 \
       && docker exec "$c" cat "$tmpc" >"$tmp" 2>/dev/null \
       && [ -s "$tmp" ]; then
        docker exec "$c" rm -f "$tmpc" >/dev/null 2>&1
        publish "$tmp" "$out"
        log "ok $n.db $(stat -c%s "$out") B"
        return 0
    fi
    docker exec "$c" rm -f "$tmpc" >/dev/null 2>&1
    rm -f "$tmp"
    log "FALHA $c:$p"
    return 1
}

dump_file() {  # container caminho nome
    local c=$1 p=$2 n=$3 out="$OUT/secrets/$3" tmp="$OUT/secrets/.$3.$$"
    if docker exec "$c" cat "$p" >"$tmp" 2>/dev/null && [ -s "$tmp" ]; then
        publish "$tmp" "$out"
        log "ok secrets/$n"
        return 0
    fi
    rm -f "$tmp"
    log "ausente $c:$p"
    return 0                      # arquivo opcional: nao derruba o backup
}

rc=0
dump_db omanet /data/omanet.db omanet || rc=1
dump_db patinho /data/patinho.db patinho || rc=1
dump_file patinho /data/admin.json admin.json
dump_file patinho /data/admin-password.txt admin-password.txt
dump_file patinho /data/session.key session.key

# restos de execucao interrompida
rm -f "$OUT"/.*.db.* "$OUT"/secrets/.*.??* 2>/dev/null || true

log "fim rc=$rc"
exit "$rc"
