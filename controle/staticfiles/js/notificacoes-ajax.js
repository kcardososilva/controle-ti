/* Painel de notificações (/alertas/notificacoes/): salvar sem recarregar a página.

   Antes, cada ação (ativar/desativar, salvar destinatários, tirar um e-mail, desvincular um login) era um POST com
   redirect: a página inteira recarregava e voltava ao topo, numa lista de ~20 canais. Agora o <form data-al-ajax> chama a
   MESMA URL por fetch; o servidor devolve JSON com a mensagem, os totais da faixa do topo e o cartão do canal já
   redesenhado (`_responder` em views/alertas.py), e aqui só se troca esse cartão. A regra é do servidor — este arquivo
   não decide nada.

   Sem JavaScript, ou se o script não carregar, o formulário continua funcionando como antes (POST + redirect).
   Arquivo estático: não passa pelo motor de template, então nada daqui usa tag de template — a URL é a do `action` do form. */
(function () {
  'use strict';

  var MENSAGEM_REDE = 'Não foi possível salvar agora. Confira a conexão e tente de novo.';
  var MENSAGEM_SESSAO = 'Sua sessão expirou. Recarregue a página e entre novamente.';
  var ICONE = { success: 'fa-circle-check', warning: 'fa-triangle-exclamation', error: 'fa-circle-xmark', info: 'fa-circle-info' };
  var TOM = { success: 'is-sucesso', warning: 'is-aviso', error: 'is-erro', info: 'is-info' };

  /* ── Aviso flutuante (o mesmo `.ui-toast` do design system): a mensagem aparece no canto, e não no topo da página,
        que numa lista rolada fica fora do campo de visão. O texto entra por textContent — vem de e-mails digitados. ── */
  function areaDeAvisos() {
    var area = document.getElementById('dsToasts');
    if (!area) {
      area = document.createElement('div');
      area.id = 'dsToasts';
      area.className = 'ui-toast-area';
      area.setAttribute('aria-live', 'polite');
      document.body.appendChild(area);
    }
    return area;
  }

  function avisar(nivel, texto) {
    if (!texto) return;
    var aviso = document.createElement('div');
    aviso.className = 'ui-toast ' + (TOM[nivel] || TOM.info);
    aviso.setAttribute('role', nivel === 'error' ? 'alert' : 'status');

    var icone = document.createElement('i');
    icone.className = 'fa-solid ' + (ICONE[nivel] || ICONE.info) + ' ui-toast-ico';
    icone.setAttribute('aria-hidden', 'true');
    var corpo = document.createElement('span');
    corpo.textContent = texto;
    var fechar = document.createElement('button');
    fechar.type = 'button';
    fechar.className = 'ui-toast-fechar';
    fechar.setAttribute('aria-label', 'Fechar');
    fechar.textContent = '✕';

    aviso.appendChild(icone);
    aviso.appendChild(corpo);
    aviso.appendChild(fechar);
    areaDeAvisos().appendChild(aviso);

    function sair() {
      aviso.classList.add('is-saindo');
      aviso.addEventListener('animationend', function () { aviso.remove(); });
    }
    fechar.addEventListener('click', sair);
    setTimeout(sair, nivel === 'error' || nivel === 'warning' ? 9000 : 5000);   // o que pede providência fica mais
  }

  /* ── Faixa de totais do topo ── */
  function atualizarTotais(kpi) {
    if (!kpi) return;
    Object.keys(kpi).forEach(function (chave) {
      document.querySelectorAll('[data-al-kpi="' + chave + '"]').forEach(function (el) { el.textContent = kpi[chave]; });
    });
    var inativos = document.querySelector('[data-al-metric="inativos"]');
    if (inativos) inativos.classList.toggle('is-danger', kpi.inativos > 0);
  }

  /* ── O cartão ── */
  function ocupar(cartao, ocupado) {
    cartao.classList.toggle('is-salvando', ocupado);
    if (ocupado) cartao.setAttribute('aria-busy', 'true'); else cartao.removeAttribute('aria-busy');
  }

  /* Troca o cartão pelo redesenhado. O foco volta ao controle equivalente: quem usa o teclado não pode perder o lugar
     porque o elemento em que estava deixou de existir (o "x" de um e-mail removido, por exemplo). */
  function trocar(cartao, html, action) {
    var molde = document.createElement('template');
    molde.innerHTML = html.trim();
    var novo = molde.content.firstElementChild;
    if (!novo) { ocupar(cartao, false); return; }
    cartao.replaceWith(novo);

    var mesmo = Array.prototype.find.call(novo.querySelectorAll('form[data-al-ajax]'), function (f) { return f.getAttribute('action') === action; });
    var destino = (mesmo && (mesmo.querySelector('input[type=text]') || mesmo.querySelector('button')))
      || novo.querySelector('.al-canal-dest input[type=text]') || novo.querySelector('button');
    if (destino) destino.focus({ preventScroll: true });
  }

  function enviar(form) {
    var cartao = form.closest('.al-canal');
    if (!cartao || cartao.getAttribute('aria-busy') === 'true') return;       // clique repetido não age duas vezes
    var action = form.getAttribute('action');
    ocupar(cartao, true);

    fetch(action, {
      method: 'POST',
      body: new FormData(form),
      credentials: 'same-origin',
      headers: { 'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json' }
    }).then(function (resposta) {
      if (resposta.redirected) throw new Error('sessao');                       // caiu na tela de login
      return resposta.json().then(function (dados) { return { dados: dados, ok: resposta.ok }; });
    }).then(function (r) {
      atualizarTotais(r.dados.kpi);
      avisar(r.dados.nivel || (r.ok ? 'success' : 'error'), r.dados.mensagem);
      if (r.dados.html) trocar(cartao, r.dados.html, action); else ocupar(cartao, false);
    }).catch(function (erro) {
      ocupar(cartao, false);
      avisar('error', erro && erro.message === 'sessao' ? MENSAGEM_SESSAO : MENSAGEM_REDE);
    });
  }

  /* Um só ouvinte no documento: o cartão é refeito a cada ação, e o que se prende a ele morreria junto. */
  document.addEventListener('submit', function (ev) {
    var form = ev.target;
    if (!form || !form.matches || !form.matches('form[data-al-ajax]')) return;
    if (ev.defaultPrevented) return;                     // o confirm() do próprio formulário ("Remover…?") foi recusado
    ev.preventDefault();
    enviar(form);
  });
})();
