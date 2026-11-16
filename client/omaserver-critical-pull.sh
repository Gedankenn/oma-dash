#!/usr/bin/env bash
# omaserver-critical-pull.sh — copia do omaserver o que NAO e regeneravel.
#
# Nasceu do incidente de 16/11 (sdb caiu, raid0 do /data ficou broken, 4,1 TB
# inacessiveis). O /data nao tem backup nenhum e esta em RAID0 sem paridade;
# entao a regra e: o que nao da para refazer sai da maquina todo dia.
#
# Modelo: snapshots com hardlink (rsync --link-dest). Cada dia parece uma copia
# completa, mas ocupa so o que mudou. 14 dias guardados.
#
# Correcoes da QA 17/11 (2a rodada):
#  - dumps de banco/segredos que o rsync nao le (600 de root/uid do container)
#    via /home/server/dash/backup-dumps.sh (flock + escrita atomica);
#  - /home/server/backups fica FORA (la mora o firmware de 2,7 GB do projetor);
#  - rsync rc=23 nao e "ok silencioso": arquivos pulados vao para o log;
#  - snapshot incompleto nao vira referencia: escreve em <stamp>.partial, so
#    renomeia no fim e marca .complete. PREV e a retencao olham so .complete.
set -uo pipefail

SRC="server@192.168.5.245"
DEST="/home/sabinho/backups/omaserver"
KEEP=14
LOG="$DEST/pull.log"
STAMP=$(date +%Y%m%d-%H%M%S)
WORK="$DEST/$STAMP.partial"
FINAL="$DEST/$STAMP"

PATHS=$(mktemp)
ERRLOG=""
cat >"$PATHS" <<'EOF'
pericia
dash
lan-hub
mobi-seo
mobipato/app/deploy
mobipato/www
mobi-portal/www
mobibeltrao/www
docker/cams/config
docker/memory/data
docker/memory/docker-compose.yml
docker/memory/.env
docker/patinho/data
docker/patinho/docker-compose.yml
docker/patinho/.env
docker/omanet/data
docker/omanet/docker-compose.yml
docker/omanet/.env
docker/romm/.env
docker/romm/docker-compose.yml
docker/qbittorrent/docker-compose.yml
docker/jellyfin/docker-compose.yml
docker/filebrowser/docker-compose.yml
docker/gtavc-web/docker-compose.yml
docker/windows/docker-compose.yml
docker/patoflix-guard/docker-compose.yml
EOF

mkdir -p "$DEST"

# parcial esquecido por execucao morta (crash, reboot) nao fica para sempre
find "$DEST" -maxdepth 1 -type d -name '*.partial' -mtime +1 -exec rm -rf -- {} + 2>/dev/null || true

# copia de referencia: a mais nova que terminou (tem .complete)
LAST=$(for d in "$DEST"/20*/; do [ -f "${d}.complete" ] && printf '%s\n' "${d%/}"; done 2>/dev/null | sort | tail -1)
PREV=0
[[ -n "$LAST" ]] && PREV=$(find "$LAST" -type f ! -name '.complete' 2>/dev/null | wc -l)

# parcial nunca sobrevive a uma saida sem sucesso (um unico trap: o que estava
# aqui antes era sobrescrito pelo trap do ERRLOG e deixava .partial para tras)
on_exit() {
    local rc=$?
    rm -f "$PATHS" ${ERRLOG:+"$ERRLOG"}
    if [[ -d "$WORK" && ! -d "$FINAL" ]]; then
        echo "   parcial descartado: $WORK" >>"$LOG"
        rm -rf -- "$WORK"
    fi
    return $rc
}
trap on_exit EXIT

{
  echo "== $(date '+%F %T') pull -> $FINAL (link-dest: ${LAST:-nenhum}, arquivos antes: $PREV)"

  # 1. dumps que o rsync nao alcanca (banco 600 de root, secrets do patinho)
  DLOG=$(mktemp)
  if ssh -o BatchMode=yes "$SRC" 'bash /home/server/dash/backup-dumps.sh' >"$DLOG" 2>&1; then
    cat "$DLOG" >>"$LOG"
    # o flock do servidor faz o segundo dump sair com rc=0 e sem tocar em nada:
    # registra para nao parecer que houve dump novo
    if grep -q "ja existe dump em andamento" "$DLOG"; then
      echo "   dumps: pulado (outro dump em andamento no servidor)"
    else
      echo "   dumps: ok"
    fi
  else
    cat "$DLOG" >>"$LOG"
    echo "   dumps: FALHOU (ver linhas acima) — seguir com o resto"
  fi
  rm -f "$DLOG"

  # os arquivos 600 de root nao sao lidos pelo rsync (uid `server`): eles vem
  # consistentes do backup-dumps.sh, entao ficam fora da copia direta.
  opts=(-a -r --numeric-ids --delete-excluded
        --exclude='__pycache__' --exclude='*.tmp' --exclude='*.bak-*'
        --exclude='patinho/data/admin.json' --exclude='patinho/data/admin-password.txt'
        --exclude='patinho/data/session.key' --exclude='omanet/data/omanet.db')
  [[ -n "$LAST" ]] && opts+=("--link-dest=$LAST")

  rc=0
  ERRLOG=$(mktemp)
  rsync "${opts[@]}" --files-from="$PATHS" "$SRC:/home/server/" "$WORK/" \
    2>"$ERRLOG" || rc=$?
  [[ -s "$ERRLOG" ]] && cat "$ERRLOG" >>"$LOG" && cat "$ERRLOG" >&2
  skipped=$(grep -c "failed to open\|Permission denied" "$ERRLOG" 2>/dev/null || true)

  if [[ $rc -ne 0 && $rc -ne 23 && $rc -ne 24 ]]; then
    echo "   ERRO rsync rc=$rc"; exit "$rc"
  fi
  [[ $rc -ne 0 ]] && echo "   AVISO rsync rc=$rc (arquivos ilegiveis: ver linhas 'failed to open' acima)"

  # segredos do patinho: entram no snapshot com permissao fechada
  if [[ -d "$WORK/dash/db-dumps/secrets" ]]; then
    chmod 700 "$WORK/dash/db-dumps" "$WORK/dash/db-dumps/secrets"
    chmod 600 "$WORK/dash/db-dumps/secrets/"* 2>/dev/null || true
  fi

  # 2. os bancos tem que estar no snapshot e abrir
  for db in omanet patinho; do
    f="$WORK/dash/db-dumps/$db.db"
    if [[ ! -s "$f" ]]; then
      echo "   ERRO: $db.db ausente no snapshot"; exit 3
    fi
    if ! python3 -c "import sqlite3,sys; sys.exit(0 if sqlite3.connect(sys.argv[1]).execute('pragma integrity_check').fetchone()[0]=='ok' else 1)" "$f" 2>/dev/null; then
      echo "   ERRO: $db.db corrompido no snapshot"; exit 3
    fi
  done

  # 3. guarda de sanidade: copia que encolheu demais falha antes de publicar
  today=$(find "$WORK" -type f ! -name '.complete' 2>/dev/null | wc -l)
  bytes=$(du -sh --apparent-size "$WORK" | cut -f1)
  if [[ $PREV -gt 0 ]] && [[ $today -lt $(( PREV * 9 / 10 )) ]]; then
    echo "   ERRO: $today arquivos contra $PREV na copia anterior (<90%) — snapshot incompleto"
    exit 2
  fi

  # 4. so agora ele vira referencia para a proxima execucao
  : > "$WORK/.complete"
  mv -- "$WORK" "$FINAL"
  echo "   ok: $bytes aparentes, $today arquivos em $FINAL (rsync rc=$rc, pulados=$skipped)"
} >>"$LOG" 2>&1

# retencao: so snapshots completos contam como dias guardados
n=0
while IFS= read -r d; do
  [[ -n "$d" ]] || continue
  n=$(( n + 1 ))
  if [[ $n -gt $KEEP ]]; then
    echo "   retencao: removendo $d" >>"$LOG"
    rm -rf -- "$d"
  fi
done < <(for x in "$DEST"/20*/; do [ -f "${x}.complete" ] && printf '%s\n' "${x%/}"; done 2>/dev/null | sort -r)

tail -c 200000 "$LOG" > "$LOG.new" && mv "$LOG.new" "$LOG"
exit 0
