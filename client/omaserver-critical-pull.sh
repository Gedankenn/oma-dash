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
# Correcoes da QA de 17/11:
#  - dumps de banco/segredos que o rsync nao le (600 de root/uid do container)
#    via /home/server/dash/backup-dumps.sh (docker + .backup do sqlite);
#  - /home/server/backups fica FORA (la mora o firmware de 2,7 GB do projetor,
#    que ja tem copia propria e inflava o snapshot);
#  - rsync rc=23 nao e mais "ok silencioso": os arquivos pulados vao para o log
#    e a contagem de arquivos precisa passar de 90% da copia anterior;
#  - falha de tamanho/leitura sai com codigo != 0 para o systemd marcar failed.
set -uo pipefail

SRC="server@192.168.5.245"
DEST="/home/sabinho/backups/omaserver"
KEEP=14
LOG="$DEST/pull.log"
STAMP=$(date +%Y%m%d-%H%M%S)

PATHS=$(mktemp)
trap 'rm -f "$PATHS"' EXIT
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
LAST=$(ls -1d "$DEST"/20* 2>/dev/null | tail -1 || true)
PREV=$( [ -n "$LAST" ] && find "$LAST" -type f 2>/dev/null | wc -l || echo 0 )

{
  echo "== $(date '+%F %T') pull -> $DEST/$STAMP (link-dest: ${LAST:-nenhum}, arquivos antes: $PREV)"

  # 1. dumps que o rsync nao alcanca (banco 600 de root, secrets do patinho)
  if ssh -o BatchMode=yes "$SRC" 'bash /home/server/dash/backup-dumps.sh' >>"$LOG" 2>&1; then
    echo "   dumps: ok"
  else
    echo "   dumps: FALHOU (ver linhas acima) — seguir com o resto"
  fi

  # os arquivos 600 de root nao sao lidos pelo rsync (uid `server`): eles vem
  # consistentes do backup-dumps.sh, entao ficam fora da copia direta.
  opts=(-a -r --numeric-ids --delete-excluded
        --exclude='__pycache__' --exclude='*.tmp' --exclude='*.bak-*'
        --exclude='patinho/data/admin.json' --exclude='patinho/data/admin-password.txt'
        --exclude='patinho/data/session.key' --exclude='omanet/data/omanet.db')
  # link-dest aponta para a copia anterior, nunca para o proprio destino
  [[ -n "$LAST" && "$LAST" != "$DEST/$STAMP" ]] && opts+=("--link-dest=$LAST")

  rc=0
  ERRLOG=$(mktemp); trap 'rm -f "$PATHS" "$ERRLOG"' EXIT
  rsync "${opts[@]}" --files-from="$PATHS" "$SRC:/home/server/" "$DEST/$STAMP/" \
    2>"$ERRLOG" || rc=$?
  [[ -s "$ERRLOG" ]] && cat "$ERRLOG" >>"$LOG" && cat "$ERRLOG" >&2
  skipped=$(grep -c "failed to open\|Permission denied" "$ERRLOG" 2>/dev/null || true)

  if [[ $rc -ne 0 && $rc -ne 23 && $rc -ne 24 ]]; then
    echo "   ERRO rsync rc=$rc"; exit "$rc"
  fi
  [[ $rc -ne 0 ]] && echo "   AVISO rsync rc=$rc (arquivos ilegiveis: ver linhas 'failed to open' acima)"

  # segredos do patinho: entram no snapshot com permissao fechada
  if [[ -d "$DEST/$STAMP/dash/db-dumps/secrets" ]]; then
    chmod 700 "$DEST/$STAMP/dash/db-dumps" "$DEST/$STAMP/dash/db-dumps/secrets"
    chmod 600 "$DEST/$STAMP/dash/db-dumps/secrets/"* 2>/dev/null || true
  fi

  # 2. os bancos tem que estar no snapshot e abrir
  for db in omanet patinho; do
    f="$DEST/$STAMP/dash/db-dumps/$db.db"
    if [[ ! -s "$f" ]]; then
      echo "   ERRO: $db.db ausente no snapshot"; exit 3
    fi
    if ! python3 -c "import sqlite3,sys; sys.exit(0 if sqlite3.connect(sys.argv[1]).execute('pragma integrity_check').fetchone()[0]=='ok' else 1)" "$f" 2>/dev/null; then
      echo "   ERRO: $db.db corrompido no snapshot"; exit 3
    fi
  done

  # 3. guarda de sanidade: copia que encolheu demais e falha, nao "ok"
  today=$(find "$DEST/$STAMP" -type f 2>/dev/null | wc -l)
  bytes=$(du -sh --apparent-size "$DEST/$STAMP" | cut -f1)
  echo "   ok: $bytes aparentes, $today arquivos em $DEST/$STAMP (rsync rc=$rc, pulados=$skipped)"
  if [[ $PREV -gt 0 ]] && [[ $today -lt $(( PREV * 9 / 10 )) ]]; then
    echo "   ERRO: $today arquivos contra $PREV na copia anterior (<90%) — snapshot incompleto"
    exit 2
  fi
} >>"$LOG" 2>&1

# retencao
ls -1d "$DEST"/20* 2>/dev/null | head -n -"$KEEP" | while read -r old; do
  echo "   retencao: removendo $old" >>"$LOG"
  rm -rf -- "$old"
done

tail -c 200000 "$LOG" > "$LOG.new" && mv "$LOG.new" "$LOG"
exit 0
