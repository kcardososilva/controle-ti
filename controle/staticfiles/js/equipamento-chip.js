/* =============================================================
   equipamento-chip.js — busca do chip no cartão "Chip (SIM)" da ficha do equipamento de TI.

   A ficha não carrega jQuery nem Select2; esta busca é só um campo de texto que alimenta um <select>
   com os chips DISPONÍVEIS (JSON de `equipamento_chip_opcoes`). Nada vindo do servidor entra como HTML:
   cada opção é criada com textContent. A URL vem do atributo data-url do bloco — este arquivo é
   servido cru, sem passar pelo motor de template.

   Ao mudar, suba ASSETS_VERSION em context_processors.py.
   ============================================================= */
(function () {
  'use strict';

  var ESPERA_MS = 250;

  function iniciar(bloco) {
    var url = bloco.getAttribute('data-url');
    var campo = bloco.querySelector('[data-chip-busca-texto]');
    var lista = bloco.querySelector('select');
    var aviso = bloco.querySelector('[data-chip-busca-status]');
    if (!url || !campo || !lista) return;

    var ultimaBusca = null;
    var temporizador = null;
    var ordem = 0;

    function dizer(texto) {
      if (aviso) aviso.textContent = texto;
    }

    function preencher(resultados) {
      var escolhido = lista.value;
      lista.innerHTML = '';
      var cabeca = document.createElement('option');
      cabeca.value = '';
      cabeca.textContent = resultados.length
        ? 'Escolha o chip (' + resultados.length + (resultados.length === 20 ? ' primeiros' : '') + ')'
        : 'Nenhum chip disponível com essa busca';
      lista.appendChild(cabeca);
      resultados.forEach(function (r) {
        var opcao = document.createElement('option');
        opcao.value = String(r.id);
        opcao.textContent = r.text;
        if (opcao.value === escolhido) opcao.selected = true;
        lista.appendChild(opcao);
      });
    }

    function buscar(texto) {
      if (texto === ultimaBusca) return;
      ultimaBusca = texto;
      var minha = ++ordem;                       // resposta velha nunca sobrescreve a mais nova
      dizer('Buscando…');
      fetch(url + '?q=' + encodeURIComponent(texto), {
        credentials: 'same-origin',
        headers: { 'X-Requested-With': 'XMLHttpRequest' },
      })
        .then(function (resposta) {
          return resposta.ok ? resposta.json() : Promise.reject(new Error('http ' + resposta.status));
        })
        .then(function (dados) {
          if (minha !== ordem) return;
          preencher(dados.results || []);
          dizer('');
        })
        .catch(function () {
          if (minha !== ordem) return;
          ultimaBusca = null;                    // deixa tentar de novo
          dizer('Não foi possível buscar os chips agora. Tente de novo.');
        });
    }

    campo.addEventListener('input', function () {
      clearTimeout(temporizador);
      temporizador = setTimeout(function () { buscar(campo.value.trim()); }, ESPERA_MS);
    });

    // A lista nasce vazia e só consulta quando alguém a usa: a ficha abre sem pedir nada ao servidor.
    var primeiraVez = function () { buscar(campo.value.trim()); };
    lista.addEventListener('focus', primeiraVez, { once: true });
    campo.addEventListener('focus', primeiraVez, { once: true });
    var caixa = bloco.closest('details');
    if (caixa) {
      caixa.addEventListener('toggle', function () { if (caixa.open) primeiraVez(); });
      if (caixa.open) primeiraVez();
    }
  }

  document.querySelectorAll('[data-chip-busca]').forEach(iniciar);
})();
