/* Cadastro rápido (subtipo e localidade) a partir do formulário de item.

   O botão "+" ao lado do campo abre um <dialog> (`data-cadastro-rapido="<id do dialog>"`); o dialog grava em
   `data-url` e, no sucesso, o registro entra no <select> de `data-alvo` JÁ SELECIONADO — a pessoa não sai do
   formulário nem perde o que já digitou. A regra (validação, nome repetido) é do servidor
   (`services/cadastro_rapido_service.py`); aqui só a conversa com ele.

   Arquivo estático: não passa pelo motor de template, então a URL e o alvo chegam por atributo `data-*`.
   Sem JavaScript o botão não faz nada e o campo funciona como antes. */
(function () {
  'use strict';

  var MENSAGEM_REDE = 'Não foi possível cadastrar agora. Confira a conexão e tente de novo.';
  var MENSAGEM_SESSAO = 'Sua sessão expirou. Entre novamente e repita o cadastro.';

  function csrf() {
    var campo = document.querySelector('#itemForm [name=csrfmiddlewaretoken]') || document.querySelector('[name=csrfmiddlewaretoken]');
    if (campo) return campo.value;
    var m = document.cookie.match(/csrftoken=([^;]+)/);   // CSRF_COOKIE_HTTPONLY fica desligado (regra 28)
    return m ? decodeURIComponent(m[1]) : '';
  }

  function limpar(dlg) {
    var geral = dlg.querySelector('[data-erro-geral]');
    geral.hidden = true;
    geral.textContent = '';
    dlg.querySelectorAll('[data-erro]').forEach(function (p) { p.hidden = true; p.textContent = ''; });
    dlg.querySelectorAll('[aria-invalid]').forEach(function (c) { c.removeAttribute('aria-invalid'); });
  }

  function mostrarErros(dlg, erros) {
    limpar(dlg);
    var geral = dlg.querySelector('[data-erro-geral]');
    var sobra = [];
    Object.keys(erros || {}).forEach(function (campo) {
      var texto = (erros[campo] || []).join(' ');
      var p = dlg.querySelector('[data-erro="' + campo + '"]');
      var entrada = dlg.querySelector('[name="' + campo + '"]');
      if (p) {
        p.textContent = texto;
        p.hidden = false;
        if (entrada) entrada.setAttribute('aria-invalid', 'true');
      } else {
        sobra.push(texto);
      }
    });
    if (sobra.length) {
      geral.textContent = sobra.join(' ');
      geral.hidden = false;
    }
    var primeiro = dlg.querySelector('[aria-invalid="true"]');
    if (primeiro) primeiro.focus();
  }

  function erroGeral(dlg, texto) {
    limpar(dlg);
    var geral = dlg.querySelector('[data-erro-geral]');
    geral.textContent = texto;
    geral.hidden = false;
  }

  /* O registro novo entra no <select> já selecionado. O `change` é nativo e com bolha: o Select2 (que escuta pelo
     jQuery, e o jQuery registra por addEventListener) redesenha o valor, e o formulário recalcula o que depende dele. */
  function aplicar(select, id, texto) {
    var valor = String(id);
    var opcao = Array.prototype.find.call(select.options, function (o) { return o.value === valor; });
    if (!opcao) {
      opcao = new Option(texto, valor, true, true);
      select.add(opcao);
    }
    select.value = valor;
    select.dispatchEvent(new Event('change', { bubbles: true }));
  }

  /* Recado ao lado do campo — o <dialog> já fechou, então é aqui que a pessoa confirma o que aconteceu. */
  function avisarNoCampo(select, texto) {
    var campo = select.closest('.if-field') || select.parentNode;
    var nota = campo.querySelector('.ui-campo-nota');
    if (!nota) {
      nota = document.createElement('p');
      nota.className = 'ui-campo-nota';
      nota.setAttribute('role', 'status');
      var ancora = select.closest('.ui-campo-acao') || select;
      ancora.insertAdjacentElement('afterend', nota);
    }
    nota.textContent = texto;
  }

  function enviar(dlg, form) {
    var botao = form.querySelector('[data-enviar]');
    if (botao.disabled) return;                       // duplo clique/Enter repetido não cadastra duas vezes
    var rotulo = botao.textContent;
    botao.disabled = true;
    botao.textContent = 'Cadastrando…';
    limpar(dlg);

    fetch(dlg.dataset.url, {
      method: 'POST',
      body: new FormData(form),
      credentials: 'same-origin',
      headers: { 'X-CSRFToken': csrf(), 'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json' }
    }).then(function (resposta) {
      if (resposta.redirected) throw new Error('sessao');
      return resposta.json().then(function (dados) { return { dados: dados, ok: resposta.ok }; });
    }).then(function (r) {
      if (!r.ok || !r.dados.ok) { mostrarErros(dlg, r.dados.erros); return; }
      var select = document.getElementById(dlg.dataset.alvo);
      if (select) {
        aplicar(select, r.dados.id, r.dados.texto);
        avisarNoCampo(select, r.dados.ja_existia
          ? '“' + r.dados.texto + '” já estava cadastrado e foi selecionado.'
          : '“' + r.dados.texto + '” cadastrado e selecionado.');
      }
      form.reset();
      dlg.close();
    }).catch(function (e) {
      erroGeral(dlg, e && e.message === 'sessao' ? MENSAGEM_SESSAO : MENSAGEM_REDE);
    }).then(function () {
      botao.disabled = false;
      botao.textContent = rotulo;
    });
  }

  function ligarDialogo(dlg) {
    var form = dlg.querySelector('[data-cadastro-rapido-form]');
    if (!form) return;
    form.addEventListener('submit', function (ev) { ev.preventDefault(); enviar(dlg, form); });
    dlg.querySelectorAll('[data-fechar]').forEach(function (b) { b.addEventListener('click', function () { dlg.close(); }); });
    // clique no fundo escuro fecha; clique dentro do formulário não (o alvo do clique é o próprio <dialog> só no fundo)
    dlg.addEventListener('click', function (ev) { if (ev.target === dlg) dlg.close(); });
    dlg.addEventListener('close', function () { limpar(dlg); });
  }

  function abrir(id) {
    var dlg = document.getElementById(id);
    if (!dlg || typeof dlg.showModal !== 'function') return;
    limpar(dlg);
    dlg.showModal();
    var primeiro = dlg.querySelector('input, select');
    if (primeiro) primeiro.focus();
  }

  document.querySelectorAll('dialog.ui-dialogo').forEach(ligarDialogo);
  document.querySelectorAll('[data-cadastro-rapido]').forEach(function (b) {
    b.addEventListener('click', function () { abrir(b.getAttribute('data-cadastro-rapido')); });
  });
})();
