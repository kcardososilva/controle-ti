/* Zelo Agro — comportamento mínimo das telas de estoque tecnológico.

   Um .js em static/ é servido CRU: nenhuma tag de template funciona aqui dentro. Tudo o que a tela precisa vem na
   marcação (`data-agro-*`). Sem este arquivo as telas continuam utilizáveis (o painel de filtros
   entregue aberto, o formulário enviando normalmente): ele só tira atrito.

   1 · Filtros avançados: o botão `[data-agro-filtros="<id>"]` abre e fecha o painel. O servidor já
       entrega o painel aberto quando há filtro em uso.
   2 · Busca com debounce (`[data-agro-busca]`) e selects que filtram ao mudar (`[data-agro-auto]`).
   3 · Envio único (`form[data-agro-envio-unico]`): o segundo clique no botão não reenvia — é a
       primeira trava contra o duplo registro; a segunda é o token do formulário, no servidor.
*/
(function () {
  'use strict';

  /* ── 1 · filtros ───────────────────────────────────────────── */
  document.addEventListener('click', function (ev) {
    var botao = ev.target.closest('[data-agro-filtros]');
    if (!botao) return;
    var painel = document.getElementById(botao.dataset.agroFiltros);
    if (!painel) return;
    var abrir = painel.hidden;
    painel.hidden = !abrir;
    botao.setAttribute('aria-expanded', abrir ? 'true' : 'false');
    if (abrir) {
      var primeiro = painel.querySelector('select, input');
      if (primeiro) primeiro.focus();
    }
  });

  /* ── 2 · busca e selects automáticos ───────────────────────── */
  var CHAVE_BUSCA = 'agro.busca';
  function guardar(chave, valor) { try { window.sessionStorage.setItem(chave, valor); } catch (e) { /* modo privado */ } }
  function ler(chave) { try { return window.sessionStorage.getItem(chave); } catch (e) { return null; } }
  function apagar(chave) { try { window.sessionStorage.removeItem(chave); } catch (e) { /* idem */ } }

  var busca = document.querySelector('[data-agro-busca]');
  if (busca && busca.form) {
    var temporizador = null;
    busca.addEventListener('input', function () {
      window.clearTimeout(temporizador);
      temporizador = window.setTimeout(function () {
        guardar(CHAVE_BUSCA, '1');
        busca.form.submit();
      }, 450);
    });
    // a página recarrega a cada busca: devolve o foco ao campo, com o cursor no fim
    if (ler(CHAVE_BUSCA)) {
      apagar(CHAVE_BUSCA);
      busca.focus();
      var fim = busca.value.length;
      try { busca.setSelectionRange(fim, fim); } catch (e) { /* type=search em alguns navegadores */ }
    }
  }
  document.querySelectorAll('[data-agro-auto]').forEach(function (s) {
    s.addEventListener('change', function () { if (s.form) s.form.submit(); });
  });

  /* ── 3 · envio único ───────────────────────────────────────── */
  document.addEventListener('submit', function (ev) {
    var form = ev.target;
    if (!form.matches || !form.matches('form[data-agro-envio-unico]')) return;
    if (form.dataset.enviando === '1') { ev.preventDefault(); return; }
    form.dataset.enviando = '1';
    form.querySelectorAll('button[type="submit"]').forEach(function (b) {
      b.classList.add('is-carregando');
      b.setAttribute('aria-busy', 'true');
    });
  });
  // voltar pelo bfcache (seta do navegador) não pode deixar o botão preso em "enviando"
  window.addEventListener('pageshow', function (ev) {
    if (!ev.persisted) return;
    document.querySelectorAll('form[data-agro-envio-unico]').forEach(function (f) {
      f.dataset.enviando = '';
      f.querySelectorAll('button[type="submit"]').forEach(function (b) {
        b.classList.remove('is-carregando');
        b.removeAttribute('aria-busy');
      });
    });
  });
})();
