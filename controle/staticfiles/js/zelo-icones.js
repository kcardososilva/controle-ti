/* Biblioteca de ícones do Zelo, do lado do navegador.

   Um .js em static/ é servido CRU (nenhuma tag de template funciona aqui): a URL do sprite chega pelo `data-sprite` da
   própria <script> que carrega este arquivo (base.html). A decisão de QUAL ícone usar nunca é tomada aqui — o servidor
   manda o id (`icone`) e este arquivo só desenha: a classificação mora em `services/icone_service.py`.

   Tudo é montado com DOM e `textContent`: nada que veio do servidor entra como HTML.

     ZeloIcones.elemento('trator', 'zi--lg')     -> <svg class="zi zi--lg"> pronto para anexar
     ZeloIcones.rotulo('trator', 'MQ-00001')     -> <span class="zi-rotulo"> ícone + texto
     ZeloIcones.select2.result / .selection      -> templates do Select2: a <option> traz `data-icone="…"`; o resultado de
                                                    busca remota traz `icone` no objeto ({id, text, icone}) */
(function () {
  'use strict';
  var tag = document.currentScript;
  var SPRITE = (tag && tag.getAttribute('data-sprite')) || '';
  var NS = 'http://www.w3.org/2000/svg';

  function elemento(id, classe) {
    if (!SPRITE || !id) return null;
    var svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('class', ('zi ' + (classe || '')).trim());
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    svg.setAttribute('data-icone', id);
    var uso = document.createElementNS(NS, 'use');
    uso.setAttribute('href', SPRITE + '#zi-' + id);
    svg.appendChild(uso);
    return svg;
  }

  function rotulo(id, texto, classe) {
    var span = document.createElement('span');
    span.className = 'zi-rotulo';
    var icone = elemento(id, classe);
    if (icone) span.appendChild(icone);
    var t = document.createElement('span');
    t.textContent = texto == null ? '' : String(texto);
    span.appendChild(t);
    return span;
  }

  function idDoEstado(estado) {
    if (!estado) return '';
    if (estado.icone) return estado.icone;
    var el = estado.element;
    return el && el.getAttribute ? (el.getAttribute('data-icone') || '') : '';
  }

  function modelo(estado, selecao) {
    if (!estado || estado.loading) return estado && estado.text;
    var id = idDoEstado(estado);
    if (!id) return estado.text;
    return window.jQuery ? window.jQuery(rotulo(id, estado.text, selecao ? 'zi--sm' : '')) : estado.text;
  }

  window.ZeloIcones = {
    sprite: SPRITE,
    elemento: elemento,
    rotulo: rotulo,
    select2: {
      result: function (estado) { return modelo(estado, false); },
      selection: function (estado) { return modelo(estado, true); },
    },
    /* Um <select> traz ícones se alguma <option> tem `data-icone`; só então os templates são ligados (sem eles o
       Select2 mostra o texto puro, como sempre mostrou). */
    selectTemIcones: function (sel) {
      return !!(SPRITE && sel && sel.querySelector && sel.querySelector('option[data-icone]'));
    },
  };
})();
