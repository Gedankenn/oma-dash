#!/bin/sh
# snapraid-ctl.sh - inicia/cancela o sync de paridade do SnapRAID.
#
# Precisa de root (os arquivos de paridade sao do root) e nao ha sudo sem senha:
# usa o docker (grupo docker = root) para entrar nos namespaces do PID 1.
# Aceita apenas estas duas acoes fixas - nenhum comando livre vem da web.
set -u

LOG=/var/log/snapraid-sync-final.log
PROC="snapraid"

running() {
    pgrep -x "$PROC" >/dev/null 2>&1
}

case "${1:-}" in
    start)
        if running; then
            echo "ja-em-execucao"
            exit 2
        fi
        docker run -d --rm --name snapraid-sync --privileged --pid=host alpine:latest \
            nsenter -t 1 -m -u -i -n -p /bin/sh -c \
            "snapraid sync >> $LOG 2>&1; echo \"fim \$?\" >> $LOG" >/dev/null || {
                echo "falha-ao-iniciar"
                exit 3
            }
        echo "iniciado"
        exit 0
        ;;
    cancel)
        if ! running; then
            echo "nao-estava-rodando"
            exit 0
        fi
        docker run --rm --privileged --pid=host alpine:latest \
            nsenter -t 1 -m -u -i -n -p /bin/sh -c \
            "pkill -INT -x snapraid; i=0; while pgrep -x snapraid >/dev/null && [ \$i -lt 90 ]; do sleep 1; i=\$((i+1)); done; echo \"cancelado \$(date -Is)\" >> $LOG" || {
                echo "falha-ao-cancelar"
                exit 3
            }
        echo "cancelado"
        exit 0
        ;;
    status)
        if running; then echo "rodando"; else echo "parado"; fi
        exit 0
        ;;
    *)
        echo "uso: $0 start|cancel|status"
        exit 1
        ;;
esac
