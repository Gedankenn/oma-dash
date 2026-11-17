# API de alertas do hub (:1985)

Regra do dono: **alerta nunca é escrita permanente no HTML.** O `index.html` só tem
`<div id="alertBar" hidden>` — a barra existe vazia e só aparece se a API devolver algo.
Quem desenha é o `app.js`, a cada atualização do `/stats`.

## Endpoint

```
GET /api/alerts
GET /api/alerts?level=crit,warn          # só gravidade
GET /api/alerts?src=netwatch             # só uma fonte
GET /api/alerts?limit=5                  # corta a lista
GET /api/alerts?all=1                    # inclui o que tem card próprio (SMART) e info
```

Resposta (200, JSON, mesma regra de acesso LAN do hub):

```json
{
  "ts": 1794934884,
  "ok": true,
  "counts": {"crit": 0, "warn": 0, "info": 0},
  "sources": [],
  "alerts": [
    {"id": "netwatch:PORT_SCAN:scan:203.0.113.9", "src": "netwatch",
     "level": "warn", "text": "203.0.113.9 tentou 12 portas TCP (12 conexoes bloqueadas)",
     "rule": "PORT_SCAN", "ts": 1794934700}
  ]
}
```

`ok` = nenhum alerta no banner. `counts` conta exatamente a lista devolvida.

## Fontes

| src | origem | janela | entra no banner |
|---|---|---|---|
| `storage` | `/home/server/dash/alerts.json` (watch-health.py, timer 5 min) | atual | sim, **exceto** item com `card`/`banner:false` |
| `netwatch` | `/home/server/netwatch/alerts.jsonl` (sensor de ARP/portas/UFW) | 6 h | só `high` e `critical` |
| `services` | `/home/server/dash/services-state.json` (sonda do catálogo `services.json`, thread do dash a cada 5 min) | atual | sim (é queda de serviço) |

Alerta com card próprio no painel (`card:"smart"`: SMART falhando, sem leitura, disco
quente, leitura velha) **não vai ao banner** — o card já mostra o fato. Ele continua
disponível em `?all=1` para diagnóstico e para a API externa.

### Fonte `services` (catalogado que caiu)

O `tools/catalogo.py` (ManNager) gera `/home/server/services.json` com 18 serviços, a URL
local e o código HTTP esperado. O dash sonda cada um a cada 5 min, sem seguir redirect
(o `302` do Jellyfin e o `303` do patinho são o resultado esperado, não falha) e com
certificado aceito no `gtavc-web` (TLS próprio).

```
1 falha ........ nada (evita ruído de restart)
2 falhas ....... warn   se o serviço só existe na LAN
3+ falhas ...... crit
qualquer falha . crit    se o serviço tem hostname público (`publico`)
volta a responder ... contador zera e o achado sai da lista
```

`url_local` com `127.0.0.1` é testada como está e, se falhar, pelo IP da LAN
(`DASH_LAN_IP`, padrão 192.168.5.245) — o `stellacorp` só escuta em loopback.

Prova (fixture com porta morta): rodada 1 sem alerta, rodada 2 `warn`/`crit` conforme o
caso, rodada 3 `crit`, e voltando a responder o achado fecha. IDs são estáveis
(`svc:<id do catálogo>`), então o dedupe do banner funciona como nas outras fontes.

## Por que isso não repete o problema do banner

```
antes: watch-health escrevia alerts.json -> index lia -> barra mostrava o mesmo
       alerta de SMART para sempre, sem contexto e sem saída
agora: SMART vira item de card (banner:false) · banner só recebe o que é
       acionável e recente · lista vazia => barra hidden, sem texto no HTML
```

## Como consumir

```bash
curl -s http://192.168.5.245:1985/api/alerts | python3 -m json.tool
curl -s "http://192.168.5.245:1985/api/alerts?level=crit&all=1"
# de outro serviço (iDBox, omanet, patinho): mesmo host, mesma porta 1985, sem porta nova
```

## Adicionar uma fonte nova (ex.: patinho)

1. no `statsd.py`, uma função que devolve lista de `{id, src, level, text, ts}`
   (espelho de `netwatch_alerts()`);
2. somar em `alerts_banner()`;
3. pronto: banner, `/stats.alerts` e `/api/alerts` passam a ver a fonte, sem tocar em HTML.

## Limites

- o feed é **estado atual**, não histórico: alerta que sai da janela/condição desaparece;
- `netwatch` só publica high/critical no banner por decisão; `medium`/`info` ficam no
  `alerts.jsonl` e em `?all=1`;
- a API é gated pela LAN, como o resto do hub (origem de fora = 403).
