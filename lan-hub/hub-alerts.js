/* hub-alerts.js - faixa de alertas do hub NETWATCH, lida da API em tempo real.
   Nada de alerta escrito no HTML: o sync.py so deixa o <div id="hubAlert"> vazio. */
(function () {
  "use strict";
  var API = location.protocol + "//" + location.hostname + ":1985/api/alerts";
  var CSS = ''
    + '.hubalert{display:flex;gap:10px;align-items:flex-start;flex-wrap:wrap;'
    + 'margin:0 0 14px;padding:10px 14px;border:1px solid #ffb020;border-radius:8px;'
    + 'background:#2a1f06;color:#fff;font-size:13px;line-height:1.45}'
    + '.hubalert.crit{border-color:#ff3b54;background:#2a0b12}'
    + '.hubalert b{letter-spacing:.14em;font-size:11px;text-transform:uppercase;color:#ffb020}'
    + '.hubalert.crit b{color:#ff3b54}'
    + '.hubalert ul{margin:0;padding-left:18px}'
    + '.hubalert li{white-space:normal}'
    + '.hubalert[hidden]{display:none}';

  function estilo() {
    if (document.getElementById("hubAlertStyle")) return;
    var st = document.createElement("style");
    st.id = "hubAlertStyle";
    st.textContent = CSS;
    document.head.appendChild(st);
  }

  function desenha(dados) {
    var box = document.getElementById("hubAlert");
    if (!box) return;
    var lista = (dados && dados.alerts) || [];
    box.textContent = "";
    if (!lista.length) {
      box.hidden = true;
      box.className = "hubalert";
      return;
    }
    var crit = lista.filter(function (a) { return a.level === "crit"; }).length;
    box.className = "hubalert" + (crit ? " crit" : "");
    box.hidden = false;

    var titulo = document.createElement("b");
    titulo.textContent = crit
      ? lista.length + " ALERTA (S) · " + crit + " CRITICO (S)"
      : lista.length + " AVISO (S)";

    var ul = document.createElement("ul");
    lista.slice(0, 5).forEach(function (a) {
      var li = document.createElement("li");
      li.textContent = String(a.text || "");
      ul.appendChild(li);
    });
    if (lista.length > 5) {
      var mais = document.createElement("li");
      mais.textContent = "+" + (lista.length - 5) + " …";
      ul.appendChild(mais);
    }
    box.appendChild(titulo);
    box.appendChild(ul);
  }

  function carrega() {
    fetch(API, { cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(desenha)
      .catch(function () {
        var box = document.getElementById("hubAlert");
        if (box) box.hidden = true;      /* API fora do ar: nao inventa alerta */
      });
  }

  function inicia() {
    if (!document.getElementById("hubAlert")) return;
    estilo();
    carrega();
    setInterval(carrega, 60000);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", inicia);
  else inicia();
})();
