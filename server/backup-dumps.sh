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
set -uo pipefail

OUT=/home/server/dash/db-dumps

mkdir -p "$OUT/secrets"
chmod 700 "$OUT" "$OUT/secrets"
log() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }

dump_db() {  # container caminho nome
    local c=$1 p=$2 n=$3
    if docker exec "$c" python -c \
        "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect('/tmp/__dump.db'); s.backup(d); d.close()" \
        "$p" >/dev/null 2>&1 \
       && docker exec "$c" cat /tmp/__dump.db >"$OUT/$n.db" 2>/dev/null \
       && [ -s "$OUT/$n.db" ]; then
        docker exec "$c" rm -f /tmp/__dump.db >/dev/null 2>&1
        chmod 600 "$OUT/$n.db"
        log "ok $n.db $(stat -c%s "$OUT/$n.db") B"
        return 0
    fi
    rm -f "$OUT/$n.db"
    log "FALHA $c:$p"
    return 1
}

dump_file() {  # container caminho nome
    local c=$1 p=$2 n=$3
    if docker exec "$c" cat "$p" >"$OUT/secrets/$n" 2>/dev/null && [ -s "$OUT/secrets/$n" ]; then
        chmod 600 "$OUT/secrets/$n"
        log "ok secrets/$n"
        return 0
    fi
    rm -f "$OUT/secrets/$n"
    log "ausente $c:$p"
    return 0                      # arquivo opcional: nao derruba o backup
}

rc=0
dump_db omanet /data/omanet.db omanet || rc=1
dump_db patinho /data/patinho.db patinho || rc=1
dump_file patinho /data/admin.json admin.json
dump_file patinho /data/admin-password.txt admin-password.txt
dump_file patinho /data/session.key session.key

log "fim rc=$rc"
exit "$rc"
