/* Frotas e Maquinários — comportamento mínimo da lista de equipamentos.

   Um .js em static/ é servido CRU: nenhuma tag de template funciona aqui dentro. Tudo o que a tela precisa vem na
   marcação (`data-*`). Sem este arquivo a lista continua utilizável: o código de cada linha é um link, e o
   formulário de validação em lote envia normalmente.

   1 · Linha clicável (`tr[data-href]`): clicar na linha abre o equipamento; clicar num link, botão ou campo da linha
       faz o que ele faz. Ctrl/Cmd+clique abre em outra aba.
   2 · Validação em lote: o "marcar todos" (`[data-agro-marcar-todos]`) marca os equipamentos a validar desta página e
       o botão `[data-agro-lote]` só vale com algum marcado.
*/
(function () {
  'use strict';

  document.addEventListener('click', function (ev) {
    var linha = ev.target.closest('tr[data-href]');
    if (!linha || ev.target.closest('a, button, input, label, select, textarea, summary')) return;
    if (window.getSelection && String(window.getSelection())) return;          // selecionando texto: não navega
    if (ev.ctrlKey || ev.metaKey) { window.open(linha.dataset.href, '_blank', 'noopener'); return; }
    window.location.href = linha.dataset.href;
  });

  var marcas = function () { return Array.prototype.slice.call(document.querySelectorAll('input[name="ids"][form="agro-lote"]')); };
  var botao = document.querySelector('[data-agro-lote]');
  function atualizar() {
    var marcados = marcas().filter(function (c) { return c.checked; }).length;
    if (botao) {
      botao.disabled = marcados === 0;
      botao.textContent = marcados ? 'Validar selecionados (' + marcados + ')' : 'Validar selecionados';
    }
    var todos = document.querySelector('[data-agro-marcar-todos]');
    if (todos) {
      var caixas = marcas();
      todos.checked = caixas.length > 0 && marcados === caixas.length;
      todos.indeterminate = marcados > 0 && marcados < caixas.length;
    }
  }
  document.addEventListener('change', function (ev) {
    if (ev.target.matches('[data-agro-marcar-todos]')) {
      marcas().forEach(function (c) { c.checked = ev.target.checked; });
      atualizar();
    } else if (ev.target.matches('input[name="ids"][form="agro-lote"]')) {
      atualizar();
    }
  });
  atualizar();
})();

/* 3 · Copiar o código (`[data-agro-copiar]`): o código original do equipamento fica à vista e copiável com um clique.
       Sem a API de área de transferência (página sem HTTPS, navegador antigo), o botão não faz nada de errado: seleciona
       o texto ao lado para o usuário copiar com Ctrl+C. */
(function () {
  'use strict';
  document.addEventListener('click', function (ev) {
    var botao = ev.target.closest('[data-agro-copiar]');
    if (!botao) return;
    var texto = botao.dataset.agroCopiar;
    function marcar() {
      botao.setAttribute('data-copiado', '1');
      botao.title = 'Copiado';
      window.setTimeout(function () { botao.removeAttribute('data-copiado'); botao.title = 'Copiar o código'; }, 1600);
    }
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(texto).then(marcar, function () { /* negado: segue sem marcar */ });
      return;
    }
    var alvo = botao.previousElementSibling;
    if (alvo && window.getSelection) {
      var faixa = document.createRange();
      faixa.selectNodeContents(alvo);
      var selecao = window.getSelection();
      selecao.removeAllRanges();
      selecao.addRange(faixa);
    }
  });
})();

/* 4 · Formulário do equipamento.

       a) Select2 com opção livre (`[data-agro-tags]`: modelo e setor): escolhe-se um valor que já existe no cadastro ou
          digita-se um novo. É texto de verdade — o servidor valida e grava o que veio.
       b) O que acompanha o quê (config entregue por `json_script` em `#agro-form-config`, nunca montada aqui):
          • escolher a CLASSE operacional sugere a CATEGORIA dela;
          • escolher a EMPRESA sugere a NATUREZA do ativo (próprio ou de terceiro);
          • a CATEGORIA diz se o equipamento costuma ter placa e chassi, e a dica do campo muda de acordo.
          É sugestão: quem preenche pode trocar, e o servidor valida o que vier.
       Sem este arquivo o formulário continua válido: todos os campos são controles comuns. */
(function () {
  'use strict';
  var form = document.querySelector('form.agro-form');
  if (!form) return;
  var jq = window.jQuery;

  if (jq && jq.fn && jq.fn.select2) {
    form.querySelectorAll('[data-agro-tags]').forEach(function (sel) {
      jq(sel).select2({
        width: '100%',
        tags: true,
        containerCssClass: 'm2m-s2',
        dropdownCssClass: 'm2m-s2',
        placeholder: sel.dataset.placeholder || '',
        allowClear: true,
        createTag: function (p) {
          var termo = (p.term || '').replace(/\s+/g, ' ').trim();
          return termo ? { id: termo, text: termo, newTag: true } : null;
        },
        language: { noResults: function () { return 'Nada encontrado'; } },
      });
    });
  }

  var bruto = document.getElementById('agro-form-config');
  var config = {};
  try { config = bruto ? JSON.parse(bruto.textContent) : {}; } catch (e) { config = {}; }

  function campo(nome) { return form.elements[nome]; }
  function mudou(el) {
    if (jq) jq(el).trigger('change.select2');
    el.dispatchEvent(new Event('input', { bubbles: true }));
  }
  function ouvir(nome, fn) {
    var el = campo(nome);
    if (!el) return;
    el.addEventListener('change', fn);
    if (jq) jq(el).on('select2:select select2:clear', fn);
  }

  /* O FABRICANTE filtra os MODELOS: com um fabricante escolhido, a lista de modelos mostra só os que o cadastro já
     tem com ele (config.modelo_fabricante, entregue por json_script). Um modelo novo continua podendo ser digitado
     (é texto livre) e o que está escolhido nunca some da lista. Sem fabricante, todos os modelos. */
  (function () {
    var fab = campo('fabricante'), mod = campo('modelo');
    var mapa = config.modelo_fabricante || {};
    if (!fab || !mod) return;
    var todas = Array.prototype.map.call(mod.options, function (o) { return o.cloneNode(true); });
    function semAcento(s) { return String(s || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase().trim(); }
    function refazer() {
      var escolhido = semAcento(fab.value);
      var atual = mod.value;
      while (mod.firstChild) mod.removeChild(mod.firstChild);
      todas.forEach(function (o) {
        if (o.value === '' || !escolhido || o.value === atual) { mod.appendChild(o.cloneNode(true)); return; }
        var deste = (mapa[o.value] || []).some(function (f) { return semAcento(f) === escolhido; });
        if (deste || !(o.value in mapa)) mod.appendChild(o.cloneNode(true));
      });
      mod.value = atual;
      if (jq) jq(mod).trigger('change.select2');
    }
    fab.addEventListener('change', refazer);
    if (jq) jq(fab).on('select2:select select2:clear', refazer);
    refazer();
  })();

  ouvir('classe_operacional', function () {
    var categoria = (config.classe_categoria || {})[campo('classe_operacional').value];
    var tipo = campo('tipo');
    if (categoria && tipo && tipo.value !== categoria) { tipo.value = categoria; mudou(tipo); atualizarDicas(); }
  });
  ouvir('empresa', function () {
    var natureza = (config.empresa_natureza || {})[campo('empresa').value];
    var alvo = campo('natureza');
    if (natureza && alvo && alvo.value !== natureza) { alvo.value = natureza; mudou(alvo); }
  });

  function atualizarDicas() {
    var tipo = campo('tipo');
    var espera = (tipo && (config.tipo_espera || {})[tipo.value]) || {};
    [['placa', 'placa'], ['chassi', 'chassi_serie']].forEach(function (par) {
      var el = campo(par[1]);
      var rotulo = el && el.closest('label');
      var dica = rotulo && rotulo.querySelector('.m2m-dica');
      if (!dica) return;
      if (dica.dataset.base === undefined) dica.dataset.base = dica.textContent;
      dica.textContent = espera[par[0]] ? 'Equipamentos desta categoria costumam ter. Sem ' + (par[0] === 'placa' ? 'placa' : 'chassi') + ', o cadastro entra em “Sem identificação completa” — mas salva.' : dica.dataset.base;
    });
  }
  ouvir('tipo', atualizarDicas);
  atualizarDicas();
})();

/* 4 · "Excluir" na lista: o diálogo é um só (`#frota-dlg-excluir_lista`) e o `frota.js` o abre e dá a ele a rota do
       status. Aqui ele pergunta ao servidor (`data-verificar`, JSON) o que há em volta do equipamento de AGORA — chip
       vinculado e equipamento instalado IMPEDEM; o histórico é só o que fica guardado — e só libera o botão
       "Excluir" e o motivo quando nada impede. O servidor confere de novo no POST: isto é informação, não autorização.
       Tudo que vem do servidor entra por textContent. */
(function () {
  'use strict';
  var plural = function (n, um, varios) { return n + ' ' + (n === 1 ? um : varios); };

  function linha(texto, ok) {
    var li = document.createElement('li');
    li.setAttribute('data-ok', ok ? '1' : '0');
    li.textContent = texto;
    return li;
  }

  document.addEventListener('click', function (ev) {
    var botao = ev.target.closest('[data-frota-abrir="excluir_lista"]');
    if (!botao) return;
    var dlg = document.getElementById('frota-dlg-excluir_lista');
    if (!dlg) return;
    var lista = dlg.querySelector('[data-excluir-verificacao]');
    var aviso = dlg.querySelector('[data-excluir-impedimento]');
    var confirmar = dlg.querySelector('[data-excluir-confirmar]');
    var motivo = dlg.querySelector('input[name="motivo"]');
    lista.replaceChildren(linha('Conferindo…', true));
    aviso.hidden = true; aviso.textContent = '';
    confirmar.disabled = true; motivo.disabled = true;

    fetch(botao.dataset.verificar, { credentials: 'same-origin', headers: { 'X-Requested-With': 'XMLHttpRequest' } })
      .then(function (r) { if (!r.ok) throw new Error(String(r.status)); return r.json(); })
      .then(function (d) {
        var h = d.historico || {};
        lista.replaceChildren(
          linha(plural(d.chips_ativos, 'chip vinculado', 'chips vinculados'), !d.chips_ativos),
          linha(plural(d.equipamentos_instalados, 'equipamento tecnológico instalado', 'equipamentos tecnológicos instalados'), !d.equipamentos_instalados),
          linha(plural(h.eventos || 0, 'evento do cadastro', 'eventos do cadastro') + ' · ' + plural(h.vinculos_de_chip || 0, 'vínculo de chip', 'vínculos de chip') +
                ' · ' + plural(h.instalacoes || 0, 'instalação', 'instalações') + ' — preservados', true)
        );
        if (d.ja_desativado) {
          aviso.textContent = 'Este equipamento já está excluído. Recarregue a lista.';
          aviso.hidden = false;
        } else if (d.pode) {
          confirmar.disabled = false; motivo.disabled = false; motivo.focus();
        } else {
          aviso.textContent = 'Não dá para excluir agora. ' + (d.impedimentos || []).join(' ');
          aviso.hidden = false;
        }
      })
      .catch(function () {
        lista.replaceChildren(linha('Não foi possível conferir agora. Feche e tente de novo.', false));
      });
  });
})();
