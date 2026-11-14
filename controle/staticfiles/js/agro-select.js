/* Zelo Agro — Select2 em TODOS os campos de seleção, de um jeito só.

   Um .js em static/ é servido CRU: nenhuma tag de template funciona aqui dentro. Tudo vem na marcação
   (`data-s2-*`). jQuery e Select2 são carregados UMA vez, pelo base.html, só no ambiente Agro.

   ── O que ele faz sozinho ───────────────────────────────────────
   Todo <select> simples de uma tela do Agro vira Select2: digita-se parte do nome para achar a opção, a
   seleção pode ser limpa (se o campo não é obrigatório e tem opção em branco), há mensagem para "nada
   encontrado" e o teclado funciona. Em lista curta (até LIMITE_BUSCA opções) a caixa de busca não aparece:
   não faz sentido pesquisar entre três itens. Não toca em data, valor, texto nem em <select multiple> nem em
   select com `size`.

   ── Atributos (todos opcionais) ─────────────────────────────────
   data-s2="off"            esta caixa não vira Select2 (o seletor de itens por página, p. ex.).
   [data-s2-off] ancestral  nada dentro dele vira Select2.
   data-placeholder="…"     texto do campo vazio (padrão: o texto da opção em branco).
   data-s2-url="…"          opções vindas do servidor, com pesquisa e paginação (`?q=&page=` → `{results:[{id,text}],
                            pagination:{more}}`); `data-s2-min="2"` = quantas letras antes de buscar.
   data-s2-pai="nome"       campo DEPENDENTE: as opções dependem do campo `nome` do mesmo formulário.
                              • com `data-s2-mapa='{"<valor da opção>": ["<valor do pai>", …]}'` cada opção diz de
                                quais pais ela é (uma opção sem entrada no mapa aparece sempre);
                              • sem mapa, o pai filtra os GRUPOS (<optgroup>) pelo texto: escolher "John Deere" no
                                pai deixa só o grupo "John Deere" no filho.
                            Mudar o pai refaz a lista e LIMPA a seleção que deixou de caber.
   data-s2-livre            aceita valor novo digitado (texto livre, validado no servidor).

   Dentro de diálogo/modal o dropdown abre DENTRO dele (`dropdownParent`), senão o foco é roubado e ele fica
   por baixo do véu. Campos criados depois (um diálogo montado, uma linha adicionada) são pegos por um
   MutationObserver; `window.AgroSelect.iniciar(raiz)` faz o mesmo sob demanda. Cada <select> é iniciado uma
   vez só (`data-s2-ok`).

   ── O que NÃO faz ───────────────────────────────────────────────
   Regra de negócio nenhuma: o servidor valida tudo o que chega (incluindo a compatibilidade entre os campos
   dependentes). Sem este arquivo as telas funcionam com os selects comuns do navegador. */
(function () {
  'use strict';

  var jq = window.jQuery;
  if (!jq || !jq.fn || !jq.fn.select2) return;

  var LIMITE_BUSCA = 8;
  var LINGUAGEM = {
    noResults: function () { return 'Nada encontrado'; },
    searching: function () { return 'Buscando…'; },
    inputTooShort: function (a) { return 'Digite ao menos ' + (a.minimum - a.input.length) + ' letra(s) para buscar'; },
    errorLoading: function () { return 'Não foi possível buscar agora'; },
    loadingMore: function () { return 'Carregando mais resultados…'; },
    removeAllItems: function () { return 'Limpar'; },
  };

  function semAcento(s) {
    return String(s || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
  }

  /* Busca por trecho, sem acento nem maiúscula; o texto do GRUPO também conta (digitar "deere" acha os
     modelos do grupo "John Deere"). */
  function corresponde(params, dado) {
    if (!params.term || !params.term.trim()) return dado;
    var termo = semAcento(params.term);
    if (dado.children) {
      if (semAcento(dado.text).indexOf(termo) > -1) return dado;
      var filhos = dado.children.filter(function (f) { return semAcento(f.text).indexOf(termo) > -1; });
      if (!filhos.length) return null;
      var copia = jq.extend({}, dado, true);
      copia.children = filhos;
      return copia;
    }
    return semAcento(dado.text).indexOf(termo) > -1 ? dado : null;
  }

  function elegivel(sel) {
    if (sel.dataset.s2Ok) return false;
    if (sel.dataset.s2 === 'off' || sel.closest('[data-s2-off]')) return false;
    if (sel.multiple || (sel.size && sel.size > 1)) return false;
    // Já têm iniciação própria (m2m.js e frota.js, frota-equipamentos.js): não se duplica.
    if (sel.hasAttribute('data-frota-s2') || sel.hasAttribute('data-agro-tags') || sel.hasAttribute('data-m2m-s2')) return false;
    if (sel.classList.contains('select2-hidden-accessible')) return false;
    if (sel.closest('.select2-container')) return false;
    return true;
  }

  function pai(sel) {
    var recipiente = sel.closest('dialog, .m2m-modal, [role="dialog"], .agro-dialogo');
    return recipiente ? jq(recipiente) : jq(document.body);
  }

  function temEmBranco(sel) {
    for (var i = 0; i < sel.options.length; i++) { if (sel.options[i].value === '') return sel.options[i]; }
    return null;
  }

  function configuracao(sel) {
    var vazio = temEmBranco(sel);
    var placeholder = sel.dataset.placeholder || (vazio ? vazio.textContent.trim() : '');
    var cfg = {
      width: '100%',
      dropdownParent: pai(sel),
      containerCssClass: 'm2m-s2',
      dropdownCssClass: 'm2m-s2',
      language: LINGUAGEM,
      matcher: corresponde,
      placeholder: placeholder ? placeholder : undefined,
      allowClear: !!vazio && !sel.required,
      minimumResultsForSearch: sel.options.length > LIMITE_BUSCA ? 0 : Infinity,
    };
    // Opções com `data-icone` (tipo/categoria de equipamento): o ícone da biblioteca do Zelo vai junto do texto.
    if (window.ZeloIcones && window.ZeloIcones.selectTemIcones(sel)) {
      cfg.templateResult = window.ZeloIcones.select2.result;
      cfg.templateSelection = window.ZeloIcones.select2.selection;
    }
    if (sel.hasAttribute('data-s2-livre')) {
      cfg.tags = true;
      cfg.createTag = function (p) {
        var termo = (p.term || '').replace(/\s+/g, ' ').trim();
        return termo ? { id: termo, text: termo, newTag: true } : null;
      };
    }
    if (sel.dataset.s2Url) {
      var minimo = parseInt(sel.dataset.s2Min || '0', 10);
      cfg.minimumInputLength = isNaN(minimo) ? 0 : minimo;
      cfg.minimumResultsForSearch = 0;
      cfg.ajax = {
        url: sel.dataset.s2Url, dataType: 'json', delay: 250, cache: false,
        data: function (p) { return { q: p.term || '', page: p.page || 1 }; },
        processResults: function (d) {
          return { results: (d && d.results) || [], pagination: { more: !!(d && d.pagination && d.pagination.more) } };
        },
      };
    }
    return cfg;
  }

  function iniciarUm(sel) {
    if (!elegivel(sel)) return;
    sel.dataset.s2Ok = '1';
    jq(sel).select2(configuracao(sel));
    // O Select2 avisa a mudança só por jQuery; quem escuta com addEventListener (filtros que enviam ao mudar,
    // campos dependentes, o resto do código do Agro) precisa do evento nativo.
    jq(sel).on('select2:select select2:clear', function () { sel.dispatchEvent(new Event('change', { bubbles: true })); });
    // Campo inválido (required vazio) precisa aparecer no Select2: marca o contêiner.
    sel.addEventListener('invalid', function () {
      var cont = jq(sel).next('.select2-container');
      cont.addClass('is-invalido');
    });
    jq(sel).on('change', function () { jq(sel).next('.select2-container').removeClass('is-invalido'); });
    ligarPai(sel);
  }

  /* ── campos dependentes ───────────────────────────────────── */
  var MESTRES = new WeakMap();   // select filho -> [{ el, grupo }] com TODAS as opções originais

  function guardarMestre(sel) {
    if (MESTRES.has(sel)) return MESTRES.get(sel);
    var itens = [];
    Array.prototype.forEach.call(sel.children, function (no) {
      if (no.tagName === 'OPTGROUP') {
        Array.prototype.forEach.call(no.children, function (o) { itens.push({ el: o.cloneNode(true), grupo: no.label }); });
      } else {
        itens.push({ el: no.cloneNode(true), grupo: null });
      }
    });
    MESTRES.set(sel, itens);
    return itens;
  }

  function valorDoPai(form, nome) {
    var campo = form && form.elements ? form.elements[nome] : null;
    if (!campo) return { valor: '', texto: '' };
    var opcao = campo.options ? campo.options[campo.selectedIndex] : null;
    return { valor: campo.value || '', texto: opcao && campo.value ? opcao.textContent.trim() : '' };
  }

  function refazer(sel) {
    var form = sel.form || sel.closest('form');
    var pai_ = valorDoPai(form, sel.dataset.s2Pai);
    var mapa = null;
    try { mapa = sel.dataset.s2Mapa ? JSON.parse(sel.dataset.s2Mapa) : null; } catch (e) { mapa = null; }
    var atual = sel.value;
    var itens = guardarMestre(sel);
    var grupos = {};
    var vazio = [];
    while (sel.firstChild) sel.removeChild(sel.firstChild);
    itens.forEach(function (item) {
      var o = item.el;
      var serve;
      if (o.value === '') { vazio.push(o.cloneNode(true)); return; }
      if (!pai_.valor) serve = true;                                           // sem pai escolhido: mostra tudo
      else if (mapa) serve = !(o.value in mapa) || (mapa[o.value] || []).map(String).indexOf(String(pai_.valor)) > -1;
      else serve = item.grupo === null || semAcento(item.grupo) === semAcento(pai_.texto);
      if (!serve) return;
      if (item.grupo === null) { sel.appendChild(o.cloneNode(true)); return; }
      if (!grupos[item.grupo]) {
        var g = document.createElement('optgroup');
        g.label = item.grupo;
        grupos[item.grupo] = g;
        sel.appendChild(g);
      }
      grupos[item.grupo].appendChild(o.cloneNode(true));
    });
    vazio.forEach(function (o) { sel.insertBefore(o, sel.firstChild); });
    var cabe = Array.prototype.some.call(sel.options, function (o) { return o.value === atual && atual !== ''; });
    sel.value = cabe ? atual : '';                                              // o que deixou de caber é LIMPO
    jq(sel).trigger('change.select2');
    if (!cabe && atual) sel.dispatchEvent(new Event('change', { bubbles: true }));   // avisa quem depende deste filho
  }

  function ligarPai(sel) {
    var nome = sel.dataset.s2Pai;
    if (!nome) return;
    var form = sel.form || sel.closest('form');
    var campoPai = form && form.elements ? form.elements[nome] : null;
    if (!campoPai) return;
    guardarMestre(sel);
    var feito = function () { refazer(sel); };
    campoPai.addEventListener('change', feito);
    // Na abertura (edição de um registro): o pai já tem valor, e o filho mostra só o que cabe, sem perder o que está escolhido.
    if (campoPai.value) {
      var guardado = sel.value;
      refazer(sel);
      if (guardado) { sel.value = guardado; jq(sel).trigger('change.select2'); }
    }
  }

  /* ── varredura ────────────────────────────────────────────── */
  function iniciar(raiz) {
    var base = raiz && raiz.querySelectorAll ? raiz : document;
    if (base.matches && base.matches('select')) iniciarUm(base);
    // Pais antes dos filhos: o filho lê o valor do pai ao nascer.
    base.querySelectorAll('select').forEach(function (sel) { if (!sel.dataset.s2Pai) iniciarUm(sel); });
    base.querySelectorAll('select[data-s2-pai]').forEach(iniciarUm);
  }

  function observar() {
    if (!window.MutationObserver) return;
    var pendente = null;
    var fila = [];
    new MutationObserver(function (mudancas) {
      mudancas.forEach(function (m) {
        m.addedNodes.forEach(function (no) { if (no.nodeType === 1) fila.push(no); });
      });
      if (pendente) return;
      pendente = window.setTimeout(function () {
        var lote = fila; fila = []; pendente = null;
        lote.forEach(function (no) { if (document.contains(no)) iniciar(no); });
      }, 30);
    }).observe(document.body, { childList: true, subtree: true });
  }

  window.AgroSelect = { iniciar: iniciar };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { iniciar(document); observar(); });
  } else {
    iniciar(document);
    observar();
  }
})();
