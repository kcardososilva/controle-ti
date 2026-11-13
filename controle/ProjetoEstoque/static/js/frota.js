/* =============================================================
   frota.js — o comportamento das telas de Frotas (veículo, movimentação de chip, importação).

   Carregado DEPOIS do `m2m.js` (os dois com `defer`, na ordem da marcação) e dependente dele:
   usa os avisos passageiros e a rede que o `m2m.js` publica em `window.M2M`. Cada bloco só age
   se achar o elemento dele na página.

   ── O que este arquivo NÃO faz ──────────────────────────────────
   Regra de negócio nenhuma. Quem decide se um chip pode ser transferido, se o vínculo já foi
   encerrado por outra pessoa ou se o motivo está faltando é o service; aqui só se envia o
   formulário e se MOSTRA a mensagem que o servidor devolveu — dentro do diálogo, sem fechá-lo.
   O caso que importa é "este chip já foi desvinculado por fulano em 12/11 14:05".

   ── Segredo e conteúdo ──────────────────────────────────────────
   Tudo que vem do servidor entra por `textContent` (nunca `innerHTML`). URL, CSRF e opções vêm do
   `json_script` (`#m2m-config`) e de `data-*` na marcação; este arquivo é servido cru.
   ============================================================= */
'use strict';

(function () {
  const M = window.M2M;
  if (!M) return;
  const CFG = M.cfg || {};
  const URLS = CFG.urls || {};
  const CSRF = CFG.csrf_token || '';

  /* ── os diálogos e o véu vão para o <body> ───────────────────── */
  // `position: fixed` dentro do conteúdo ancora no contêiner animado da página, não na janela.
  const veu = document.getElementById('frota-veu');
  const dialogos = Array.from(document.querySelectorAll('[data-frota-dialogo]'));
  if (veu) document.body.appendChild(veu);
  dialogos.forEach((d) => document.body.appendChild(d));

  let aberto = null;
  let focoAnterior = null;

  function limpar(dlg) {
    const caixa = dlg.querySelector('[data-frota-erro]');
    if (caixa) { caixa.hidden = true; caixa.textContent = ''; }
    dlg.querySelectorAll('[aria-invalid]').forEach((c) => c.removeAttribute('aria-invalid'));
  }

  function fechar() {
    if (!aberto) return;
    aberto.hidden = true;
    if (veu) veu.hidden = true;
    aberto = null;
    if (focoAnterior && focoAnterior.focus) focoAnterior.focus();
  }

  /* ── Select2 dentro do diálogo ───────────────────────────────── */
  function iniciarSelect2(dlg) {
    const jq = window.jQuery;
    if (!jq || !jq.fn || !jq.fn.select2) return;
    dlg.querySelectorAll('[data-frota-s2]').forEach((sel) => {
      if (sel.dataset.pronto) return;
      sel.dataset.pronto = '1';
      const campo = sel.dataset.frotaS2;
      jq(sel).select2({
        width: '100%',
        dropdownParent: jq(dlg),
        containerCssClass: 'm2m-s2',
        dropdownCssClass: 'm2m-s2',
        placeholder: sel.dataset.placeholder || '',
        allowClear: true,
        minimumInputLength: campo === 'chip' ? 0 : 2,
        language: {
          noResults: () => (campo === 'chip' ? 'Nenhum chip disponível' : 'Nada encontrado'),
          searching: () => 'Buscando…',
          inputTooShort: () => 'Digite ao menos 2 letras',
          errorLoading: () => 'Não foi possível buscar',
        },
        ajax: {
          url: URLS.frota_opcoes,
          dataType: 'json',
          delay: 250,
          data: (p) => ({ q: p.term || '', campo: campo }),
          processResults: (d) => ({ results: (d && d.results) || [] }),
        },
      });
    });
  }

  /* ── abrir ───────────────────────────────────────────────────── */
  function abrir(botao) {
    const dlg = document.getElementById('frota-dlg-' + botao.dataset.frotaAbrir);
    if (!dlg) return;
    const form = dlg.querySelector('form');
    form.reset();
    limpar(dlg);
    form.action = botao.dataset.url || '';
    const alvo = dlg.querySelector('[data-frota-alvo]');
    if (alvo) alvo.textContent = botao.dataset.alvo || '';
    // `data-campo-chip="7"` preenche o <input type="hidden" name="chip"> do diálogo.
    Object.keys(botao.dataset).forEach((chave) => {
      if (chave.indexOf('campo') !== 0 || chave.length <= 5) return;
      const nome = chave.charAt(5).toLowerCase() + chave.slice(6);
      const campo = form.elements[nome];
      if (campo) campo.value = botao.dataset[chave];
    });
    iniciarSelect2(dlg);
    if (window.jQuery) window.jQuery(dlg).find('[data-frota-s2]').val(null).trigger('change');

    const menu = botao.closest('[data-m2m-menu]');
    focoAnterior = menu ? menu.querySelector('summary') : document.activeElement;
    aberto = dlg;
    dlg.hidden = false;
    if (veu) veu.hidden = false;
    const primeiro = dlg.querySelector('select:not([type=hidden]), textarea, input:not([type=hidden])');
    if (primeiro) primeiro.focus();
  }

  document.addEventListener('click', (ev) => {
    const botao = ev.target.closest('[data-frota-abrir]');
    if (botao) { ev.preventDefault(); abrir(botao); return; }
    if (ev.target.closest('[data-frota-fechar]') || (veu && ev.target === veu)) fechar();
  });

  document.addEventListener('keydown', (ev) => {
    if (!aberto) return;
    if (ev.key === 'Escape') {
      // Escape com a lista do Select2 aberta fecha só a lista.
      if (document.querySelector('.select2-container--open')) return;
      fechar();
      return;
    }
    if (ev.key !== 'Tab') return;
    const focaveis = Array.from(aberto.querySelectorAll('button, [href], input:not([type=hidden]), select, textarea, [tabindex]:not([tabindex="-1"])'))
      .filter((e) => !e.disabled && e.offsetParent !== null);
    if (!focaveis.length) return;
    const primeiro = focaveis[0];
    const ultimo = focaveis[focaveis.length - 1];
    if (ev.shiftKey && document.activeElement === primeiro) { ev.preventDefault(); ultimo.focus(); }
    else if (!ev.shiftKey && document.activeElement === ultimo) { ev.preventDefault(); primeiro.focus(); }
  });

  /* ── enviar ──────────────────────────────────────────────────── */
  function mostrarErro(dlg, texto) {
    const caixa = dlg.querySelector('[data-frota-erro]');
    if (!caixa) { M.avisar(texto, 'erro'); return; }
    caixa.textContent = texto;
    caixa.hidden = false;
    caixa.setAttribute('tabindex', '-1');
    caixa.focus();
  }

  document.addEventListener('submit', async (ev) => {
    const form = ev.target;
    if (!form.matches || !form.matches('[data-frota-form]')) return;
    ev.preventDefault();
    const dlg = form.closest('[data-frota-dialogo]');
    const botao = form.querySelector('button[type="submit"]');
    if (botao && botao.disabled) return;
    limpar(dlg);
    if (botao) { botao.classList.add('is-carregando'); botao.disabled = true; }
    let navegando = false;
    try {
      const resp = await fetch(form.action, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'X-CSRFToken': CSRF, 'X-Requested-With': 'XMLHttpRequest' },
        body: new FormData(form),
      });
      let dados = null;
      try { dados = await resp.json(); } catch (e) { /* 403 e 500 voltam em HTML */ }
      if (!dados) { mostrarErro(dlg, M.mensagemDeFalha({ status: resp.status })); return; }
      if (dados.ok && form.hasAttribute('data-frota-inline')) {
        // Diálogo que NÃO recarrega a página (o "Novo tipo" do cadastro de veículo): quem abriu o
        // formulário ainda tem coisa digitada. Fecha, avisa e entrega o resultado por evento.
        fechar();
        M.avisar(dados.mensagem || 'Salvo.', 'ok');
        document.dispatchEvent(new CustomEvent('frota:salvo', { detail: { dialogo: dlg.id, dados: dados } }));
        return;
      }
      if (dados.ok) {
        const partes = [dados.mensagem].concat(dados.avisos || []).filter(Boolean);
        M.guardar('m2m.aviso', JSON.stringify({ mensagem: partes.join(' '), tom: (dados.avisos && dados.avisos.length) ? 'aviso' : 'ok' }));
        navegando = true;
        window.location.assign(dados.redirect || window.location.href);
        return;
      }
      Object.keys(dados.erros || {}).forEach((nome) => {
        const campo = form.elements[nome];
        if (campo && campo.setAttribute) campo.setAttribute('aria-invalid', 'true');
      });
      mostrarErro(dlg, dados.mensagem || 'Não foi possível concluir.');
    } catch (e) {
      mostrarErro(dlg, 'Não foi possível falar com o servidor. Tente de novo.');
    } finally {
      if (botao && !navegando) { botao.classList.remove('is-carregando'); botao.disabled = false; }
    }
  });

  /* ── "Novo tipo": a opção nova entra no select de Tipo, já escolhida ── */
  document.addEventListener('frota:salvo', (ev) => {
    if (!ev.detail || ev.detail.dialogo !== 'frota-dlg-novo_tipo') return;
    const select = document.querySelector('select[name="tipo"]');
    const tipo = ev.detail.dados;
    if (!select || !tipo) return;
    const opcao = new Option(tipo.nome, String(tipo.id), true, true);
    // Em ordem alfabética, como o servidor entrega a lista (a 1ª opção é o "Escolher…").
    const depois = Array.from(select.options).slice(1).find((o) => o.text.localeCompare(tipo.nome, 'pt-BR') > 0);
    select.add(opcao, depois || null);
    select.value = String(tipo.id);
    select.removeAttribute('aria-invalid');
    select.dispatchEvent(new Event('change', { bubbles: true }));
    select.focus();
  });

  /* ── formulários de página inteira: spinner só no botão ──────── */
  // Envio da planilha, confirmação da importação, exportações: a página recarrega, então o
  // botão fica com o girador até lá. Sem `disabled` (um botão desabilitado some do envio).
  document.addEventListener('submit', (ev) => {
    const form = ev.target;
    if (!form.matches || !form.matches('[data-frota-pagina]')) return;
    const mensagem = form.getAttribute('data-frota-confirma');
    if (mensagem && !window.confirm(mensagem)) { ev.preventDefault(); return; }
    const botao = form.querySelector('button[type="submit"]');
    if (botao) botao.classList.add('is-carregando');
  });
  // Voltar com a seta do navegador (bfcache) não pode deixar o botão girando para sempre.
  window.addEventListener('pageshow', (ev) => {
    if (!ev.persisted) return;
    document.querySelectorAll('.m2m-btn.is-carregando').forEach((b) => { b.classList.remove('is-carregando'); b.disabled = false; });
  });

  /* ── filtros avançados: recolhidos até o usuário pedir ───────── */
  // O botão "Filtros" abre e fecha o painel (`data-m2m-filtros="<id do painel>"`). O servidor já
  // entrega o painel aberto quando há filtro em uso; sem JS o painel aberto continua utilizável.
  document.addEventListener('click', (ev) => {
    const botao = ev.target.closest('[data-m2m-filtros]');
    if (!botao) return;
    const painel = document.getElementById(botao.dataset.m2mFiltros);
    if (!painel) return;
    const abrir = painel.hidden;
    painel.hidden = !abrir;
    botao.setAttribute('aria-expanded', abrir ? 'true' : 'false');
    if (abrir) {
      const primeiro = painel.querySelector('select, input');
      if (primeiro) primeiro.focus();
    }
  });

  /* ── seções em abas (detalhe do veículo e do chip) ───────────── */
  // A marcação traz todas as seções (`section[data-secao]`) empilhadas: sem JS, tudo aparece. Com JS,
  // as abas são montadas a partir dos títulos e só uma seção fica à vista. Um link com `#id` (ou a
  // query `?hist=` do filtro do histórico) abre a aba que contém o alvo.
  document.querySelectorAll('[data-m2m-secoes]').forEach((raiz) => {
    const paineis = Array.from(raiz.querySelectorAll(':scope > [data-secao]'));
    if (paineis.length < 2) return;
    const nav = document.createElement('div');
    nav.className = 'm2m-secoes-nav';
    nav.setAttribute('role', 'tablist');
    const botoes = paineis.map((painel, i) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.id = 'aba-' + painel.id;
      b.setAttribute('role', 'tab');
      b.setAttribute('aria-controls', painel.id);
      b.textContent = painel.dataset.secao;
      painel.setAttribute('role', 'tabpanel');
      painel.setAttribute('aria-labelledby', b.id);
      b.addEventListener('click', () => ativar(i));
      b.addEventListener('keydown', (ev) => {
        if (ev.key !== 'ArrowRight' && ev.key !== 'ArrowLeft') return;
        ev.preventDefault();
        const alvo = (i + (ev.key === 'ArrowRight' ? 1 : paineis.length - 1)) % paineis.length;
        ativar(alvo);
        botoes[alvo].focus();
      });
      nav.appendChild(b);
      return b;
    });
    raiz.insertBefore(nav, raiz.firstChild);

    function ativar(indice) {
      paineis.forEach((painel, j) => {
        painel.hidden = j !== indice;
        botoes[j].setAttribute('aria-selected', j === indice ? 'true' : 'false');
        botoes[j].tabIndex = j === indice ? 0 : -1;
      });
      // O gráfico de consumo foi medido com a seção à vista: ao reaparecer, ele se redimensiona.
      window.dispatchEvent(new Event('resize'));
    }

    function indiceDoHash() {
      const id = decodeURIComponent((window.location.hash || '').slice(1));
      const alvo = id ? document.getElementById(id) : null;
      const painel = alvo ? alvo.closest('[data-secao]') : null;
      return painel ? paineis.indexOf(painel) : -1;
    }

    function indiceDaQuery() {
      const params = new URLSearchParams(window.location.search);
      return paineis.findIndex((p) => p.dataset.secaoQuery && params.has(p.dataset.secaoQuery));
    }

    const doHash = indiceDoHash();
    ativar(doHash >= 0 ? doHash : Math.max(indiceDaQuery(), 0));
    if (doHash >= 0) {
      const alvo = document.getElementById(decodeURIComponent(window.location.hash.slice(1)));
      if (alvo && alvo.scrollIntoView) alvo.scrollIntoView();
    }
    window.addEventListener('hashchange', () => {
      const i = indiceDoHash();
      if (i >= 0) ativar(i);
    });
  });

  /* ── recolher e expandir, com o navegador lembrando a escolha ── */
  // `<details data-m2m-lembrar="chave">`: a escolha é por estado (`data-completo`), porque o
  // checklist aberto, enquanto falta etapa, e fechado, quando tudo está feito, são padrões diferentes.
  document.querySelectorAll('details[data-m2m-lembrar]').forEach((d) => {
    const chave = 'm2m.lembrar.' + d.dataset.m2mLembrar + (d.dataset.completo ? '.completo' : '.pendente');
    try {
      const salvo = window.localStorage.getItem(chave);
      if (salvo === '1') d.open = true;
      else if (salvo === '0') d.open = false;
    } catch (e) { /* armazenamento bloqueado: vale o padrão do servidor */ }
    d.addEventListener('toggle', () => {
      try { window.localStorage.setItem(chave, d.open ? '1' : '0'); } catch (e) { /* idem */ }
    });
  });

  /* ── menu "Movimentar" ───────────────────────────────────────── */
  // `<details data-m2m-menu>`: fecha ao escolher um item, ao clicar fora e com Escape.
  document.addEventListener('click', (ev) => {
    document.querySelectorAll('details[data-m2m-menu][open]').forEach((menu) => {
      if (!menu.contains(ev.target) || ev.target.closest('.m2m-menu-lista button')) menu.open = false;
    });
  });
  document.addEventListener('keydown', (ev) => {
    if (ev.key !== 'Escape') return;
    document.querySelectorAll('details[data-m2m-menu][open]').forEach((menu) => {
      menu.open = false;
      const resumo = menu.querySelector('summary');
      if (resumo) resumo.focus();
    });
  });
})();
