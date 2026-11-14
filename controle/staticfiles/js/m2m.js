/* =============================================================
   m2m.js — o comportamento do módulo Monitoramento TIM M2M.

   Um arquivo para as sete telas: cada bloco só age se achar o elemento
   dele na página (`data-m2m-*` ou o id), então o mesmo script serve a
   lista, o detalhe, o formulário e a configuração.

   ── O que este arquivo NÃO faz ──────────────────────────────────
   Regra de negócio nenhuma. Ele não decide o que é "sessão ativa", não
   formata volume, não agrupa o ICCID, não calcula frescor: tudo isso o
   servidor entrega pronto — a linha da lista atualizada, por exemplo,
   volta como HTML já renderizado pelo MESMO template da lista
   (`_chip_linha.html`), e aqui só se troca a linha. Se o JS
   reimplementasse a formatação, ela divergiria da do servidor.

   ── De onde vêm URL e CSRF ──────────────────────────────────────
   Um .js em `static/` é servido cru, sem passar pelo motor de template:
   uma tag de URL aqui viraria texto literal e quebraria em silêncio. O
   que é geral entra por `json_script` (`#m2m-config`) e é lido UMA vez,
   abaixo; o que é por chip/consulta vem em `data-url` na marcação.

   ── Segredo ─────────────────────────────────────────────────────
   O token nunca passa por aqui: o campo de token é um formulário comum
   (POST, sem JS) e a resposta do servidor nunca o devolve.
   ============================================================= */
'use strict';

(function () {
  const cfgEl = document.getElementById('m2m-config');
  const CFG = cfgEl ? JSON.parse(cfgEl.textContent) : {};
  const CSRF = CFG.csrf_token || '';
  const URLS = CFG.urls || {};

  /* ── utilidades ─────────────────────────────────────────────── */
  function guardar(chave, valor) { try { sessionStorage.setItem(chave, valor); } catch (e) { /* sem storage: segue sem o recurso */ } }
  function ler(chave) { try { return sessionStorage.getItem(chave); } catch (e) { return null; } }
  function apagar(chave) { try { sessionStorage.removeItem(chave); } catch (e) { /* idem */ } }

  function criar(tag, atributos, texto) {
    const e = document.createElement(tag);
    Object.keys(atributos || {}).forEach((k) => e.setAttribute(k, atributos[k]));
    if (texto !== undefined && texto !== null) e.textContent = texto;
    return e;
  }

  function atrasar(fn, ms) {
    let t = null;
    return function () {
      const args = arguments;
      clearTimeout(t);
      t = setTimeout(() => fn.apply(null, args), ms);
    };
  }

  /* ── aviso passageiro ───────────────────────────────────────── */
  function avisar(mensagem, tom) {
    let area = document.querySelector('.m2m-toast-area');
    if (!area) {
      area = criar('div', { class: 'm2m-toast-area' });
      document.body.appendChild(area);
    }
    const t = criar('div', { class: 'm2m-toast', 'data-tom': tom || 'info', role: tom === 'erro' ? 'alert' : 'status' }, mensagem);
    area.appendChild(t);
    setTimeout(() => t.remove(), tom === 'erro' || tom === 'aviso' ? 9000 : 5500);
  }

  // Depois de um reload (detalhe do chip), o aviso do que acabou de acontecer.
  (function avisoPendente() {
    const bruto = ler('m2m.aviso');
    if (!bruto) return;
    apagar('m2m.aviso');
    try {
      const a = JSON.parse(bruto);
      avisar(a.mensagem, a.tom);
    } catch (e) { /* aviso corrompido: ignora */ }
  })();

  /* ── rede ───────────────────────────────────────────────────── */
  async function enviar(url, corpo, comoForm) {
    const cabecalhos = { 'X-CSRFToken': CSRF, 'X-Requested-With': 'XMLHttpRequest' };
    let body;
    if (comoForm) {
      body = new URLSearchParams(corpo);
    } else {
      cabecalhos['Content-Type'] = 'application/json';
      body = JSON.stringify(corpo || {});
    }
    const resp = await fetch(url, { method: 'POST', credentials: 'same-origin', headers: cabecalhos, body: body });
    let dados = null;
    try { dados = await resp.json(); } catch (e) { /* 403/500 voltam em HTML */ }
    return { status: resp.status, ok: resp.ok, dados: dados };
  }

  async function buscar(url) {
    const resp = await fetch(url, { credentials: 'same-origin', headers: { 'X-Requested-With': 'XMLHttpRequest' } });
    let dados = null;
    try { dados = await resp.json(); } catch (e) { /* idem */ }
    return { status: resp.status, ok: resp.ok, dados: dados };
  }

  function mensagemDeFalha(r) {
    if (r.status === 403) return 'Você não tem permissão para esta ação.';
    if (r.status === 401 || r.status === 302) return 'Sua sessão expirou. Entre de novo.';
    return 'O servidor respondeu algo inesperado (HTTP ' + r.status + ').';
  }

  // O `frota.js` (diálogos de movimentação, importação) usa os mesmos avisos e a mesma rede: ele é
  // carregado depois (ambos com `defer`, na ordem da marcação) e lê isto daqui em vez de duplicar.
  window.M2M = { avisar: avisar, criar: criar, guardar: guardar, enviar: enviar, buscar: buscar, mensagemDeFalha: mensagemDeFalha, cfg: CFG };

  /* ── 1 · Atualizar o chip (lista e detalhe) ─────────────────── */
  function trocarLinha(linhaAtual, html) {
    const molde = document.createElement('template');
    molde.innerHTML = html.trim();
    const nova = molde.content.firstElementChild;
    if (nova && linhaAtual) linhaAtual.replaceWith(nova);
  }

  document.addEventListener('click', async (ev) => {
    const botao = ev.target.closest('[data-m2m-atualizar]');
    if (!botao || botao.disabled) return;
    ev.preventDefault();

    const tipos = (botao.dataset.tipos || 'sessao,consumo').split(',');
    const contexto = botao.dataset.contexto || 'linha';
    botao.classList.add('is-carregando');
    botao.disabled = true;
    let recarregar = false;
    try {
      const r = await enviar(botao.dataset.url, { tipos: tipos, contexto: contexto });
      if (!r.dados) {
        avisar(mensagemDeFalha(r), 'erro');
        return;
      }
      const d = r.dados;
      if (d.recusada || r.status === 409) {
        avisar(d.mensagem, 'aviso');
        return;
      }
      if (!r.ok) {
        avisar(d.mensagem || mensagemDeFalha(r), 'erro');
        return;
      }
      if (contexto === 'linha' && d.html_linha) trocarLinha(botao.closest('tr'), d.html_linha);
      if (contexto === 'detalhe') {
        guardar('m2m.aviso', JSON.stringify({ mensagem: d.mensagem, tom: d.tom }));
        recarregar = true;
        return;
      }
      avisar(d.mensagem, d.tom);
    } catch (e) {
      avisar('Não foi possível falar com o servidor. Tente de novo.', 'erro');
    } finally {
      botao.classList.remove('is-carregando');
      botao.disabled = false;
      if (recarregar) window.location.reload();
    }
  });

  /* ── 2 · Gaveta com a resposta completa ─────────────────────── */
  const gaveta = document.getElementById('m2m-gaveta');
  const veu = document.getElementById('m2m-veu');
  let focoAnterior = null;
  // `position: fixed` dentro do conteúdo ancora no contêiner animado da página, não na janela:
  // a gaveta (e o véu) vão para o <body> para cobrir a tela inteira.
  if (gaveta && veu) { document.body.appendChild(veu); document.body.appendChild(gaveta); }

  function fecharGaveta() {
    if (!gaveta || gaveta.hidden) return;
    gaveta.hidden = true;
    if (veu) veu.hidden = true;
    if (focoAnterior && focoAnterior.focus) focoAnterior.focus();
  }

  function abrirGaveta() {
    focoAnterior = document.activeElement;
    gaveta.hidden = false;
    if (veu) veu.hidden = false;
    const fechar = gaveta.querySelector('[data-m2m-fechar]');
    if (fechar) fechar.focus();
  }

  function preencherGaveta(d) {
    const meta = document.getElementById('m2m-gaveta-meta');
    const corpo = document.getElementById('m2m-gaveta-corpo');
    const aviso = document.getElementById('m2m-gaveta-aviso');
    document.getElementById('m2m-gaveta-titulo').textContent = d.tipo_rotulo + ' · ' + d.chip.nome;

    meta.replaceChildren();
    [
      ['Consulta de', d.iniciada_em],
      ['Resultado', d.resultado_rotulo + (d.http_status ? ' (HTTP ' + d.http_status + ')' : '')],
      ['Origem', d.origem_rotulo],
      ['Duração', d.duracao_ms === null ? '—' : d.duracao_ms + ' ms'],
      ['Tentativas', String(d.tentativas)],
      ['Estado lido', d.estado_interpretado ? (d.estado_bruto + ' → ' + d.estado_interpretado) : (d.estado_bruto || '—')],
      ['Valor lido', d.valor_original ? (d.valor_original + ' ' + d.unidade_original) : '—'],
    ].forEach((par) => {
      const linha = criar('div');
      linha.appendChild(criar('dt', {}, par[0]));
      linha.appendChild(criar('dd', {}, par[1]));
      meta.appendChild(linha);
    });

    let nota = '';
    if (!d.valida) nota = 'Esta consulta FALHOU: ' + (d.mensagem_erro || d.resultado_rotulo) + ' Não há resposta da API para mostrar.';
    else if (d.payload === null) nota = 'A API respondeu sem conteúdo JSON.';
    else if (d.payload && d.payload._truncado) nota = 'A resposta passava de 64 KB: só o início foi guardado.';
    aviso.hidden = !nota;
    aviso.textContent = nota;

    corpo.textContent = d.payload === null ? '' : JSON.stringify(d.payload, null, 2);
  }

  document.addEventListener('click', async (ev) => {
    const abrir = ev.target.closest('[data-m2m-payload]');
    if (abrir && gaveta) {
      ev.preventDefault();
      abrir.classList.add('is-carregando');
      try {
        const r = await buscar(abrir.dataset.url);
        if (!r.dados) { avisar(mensagemDeFalha(r), 'erro'); return; }
        preencherGaveta(r.dados);
        abrirGaveta();
      } catch (e) {
        avisar('Não foi possível carregar a resposta.', 'erro');
      } finally {
        abrir.classList.remove('is-carregando');
      }
      return;
    }
    if (ev.target.closest('[data-m2m-fechar]') || ev.target === veu) { fecharGaveta(); return; }

    const copiar = ev.target.closest('[data-m2m-copiar]');
    if (copiar) {
      const texto = document.getElementById('m2m-gaveta-corpo').textContent;
      try {
        await navigator.clipboard.writeText(texto);
        avisar('JSON copiado.', 'ok');
      } catch (e) {
        const area = document.getElementById('m2m-gaveta-corpo');
        const faixa = document.createRange();
        faixa.selectNodeContents(area);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(faixa);
        avisar('Selecionei o texto: copie com Ctrl+C.', 'aviso');
      }
    }
  });

  document.addEventListener('keydown', (ev) => {
    if (!gaveta || gaveta.hidden) return;
    if (ev.key === 'Escape') { fecharGaveta(); return; }
    if (ev.key !== 'Tab') return;
    // Prende o foco dentro da gaveta enquanto ela está aberta.
    const focaveis = Array.from(gaveta.querySelectorAll('button, [href], [tabindex]:not([tabindex="-1"])')).filter((e) => !e.disabled);
    if (!focaveis.length) return;
    const primeiro = focaveis[0];
    const ultimo = focaveis[focaveis.length - 1];
    if (ev.shiftKey && document.activeElement === primeiro) { ev.preventDefault(); ultimo.focus(); }
    else if (!ev.shiftKey && document.activeElement === ultimo) { ev.preventDefault(); primeiro.focus(); }
  });

  /* ── 3 · Fita de consultas: a leitura da marca ──────────────── */
  function lerMarca(ev) {
    const marca = ev.target.closest('.m2m-marca[data-leitura]');
    if (!marca) return;
    const saida = marca.closest('.m2m-fita').querySelector('.m2m-fita-leitura');
    if (saida) saida.textContent = marca.dataset.leitura;
  }
  document.addEventListener('click', lerMarca);
  document.addEventListener('mouseover', lerMarca);
  document.addEventListener('focusin', lerMarca);

  /* ── 4 · Filtros: busca com debounce e selects automáticos ──── */
  const busca = document.querySelector('[data-m2m-busca]');
  if (busca && busca.form) {
    const enviarBusca = atrasar(() => {
      guardar('m2m.busca', '1');
      busca.form.submit();
    }, 450);
    busca.addEventListener('input', enviarBusca);
    if (ler('m2m.busca')) {
      apagar('m2m.busca');
      busca.focus();
      const fim = busca.value.length;
      try { busca.setSelectionRange(fim, fim); } catch (e) { /* tipo search em alguns navegadores */ }
    }
  }
  document.querySelectorAll('[data-m2m-auto]').forEach((s) => {
    s.addEventListener('change', () => s.form && s.form.submit());
  });

  document.addEventListener('submit', (ev) => {
    const mensagem = ev.target.getAttribute && ev.target.getAttribute('data-m2m-confirma');
    if (mensagem && !window.confirm(mensagem)) ev.preventDefault();
  });

  /* ── 5 · Select2 do cadastro (equipamento, responsável, unidade) ── */
  if (window.jQuery && window.jQuery.fn.select2) {
    document.querySelectorAll('[data-m2m-s2]').forEach((sel) => {
      const opcoes = {
        width: '100%',
        containerCssClass: 'm2m-s2',
        dropdownCssClass: 'm2m-s2',
        placeholder: sel.dataset.placeholder || '',
        allowClear: true,
        language: {
          noResults: () => 'Nada encontrado',
          searching: () => 'Buscando…',
          inputTooShort: () => 'Digite ao menos 2 letras',
          errorLoading: () => 'Não foi possível buscar',
        },
      };
      // Opções (ou resultados remotos) com ícone do tipo de equipamento: `data-icone` na <option>, `icone` no resultado.
      if (window.ZeloIcones && (sel.dataset.m2mS2 === 'ajax' || window.ZeloIcones.selectTemIcones(sel))) {
        opcoes.templateResult = window.ZeloIcones.select2.result;
        opcoes.templateSelection = window.ZeloIcones.select2.selection;
      }
      // `data-fonte` escolhe o endpoint de opções (`opcoes` = chip, `frota_opcoes` = veículo): cada um
      // tem a permissão dele, e o cadastro do veículo não tem a de importar chips.
      const fonte = URLS[sel.dataset.fonte || 'opcoes'];
      if (sel.dataset.m2mS2 === 'ajax' && fonte) {
        opcoes.minimumInputLength = 2;
        opcoes.ajax = {
          url: fonte,
          dataType: 'json',
          delay: 250,
          data: (p) => ({ q: p.term || '', campo: sel.dataset.campo }),
          processResults: (d) => ({ results: (d && d.results) || [] }),
        };
      }
      window.jQuery(sel).select2(opcoes);
    });
  }

  /* ── 6 · Gráfico de consumo (detalhe do chip) ───────────────── */
  const tela = document.getElementById('m2m-grafico-consumo');
  const serieEl = document.getElementById('m2m-serie');
  if (tela && serieEl) {
    if (!window.Chart) {
      tela.replaceWith(criar('p', { class: 'm2m-sub' }, 'O gráfico não carregou (biblioteca indisponível). Os valores estão na linha do tempo abaixo.'));
    } else {
      const serie = JSON.parse(serieEl.textContent);
      const raiz = document.querySelector('.m2m-page');
      // Um canvas não herda CSS: as cores são LIDAS dos tokens, senão o gráfico
      // ignoraria o tema escuro. Sem cor escrita à mão.
      const token = (nome) => getComputedStyle(raiz).getPropertyValue(nome).trim();
      const formatoData = new Intl.DateTimeFormat('pt-BR', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
      Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;

      const grafico = new Chart(tela, {
        type: 'line',
        data: {
          labels: serie.pontos.map((p) => formatoData.format(new Date(p.em))),
          datasets: [{
            label: 'Volume informado pela TIM (' + serie.unidade + ')',
            data: serie.pontos.map((p) => p.valor),
            borderColor: token('--chart-1'),
            backgroundColor: token('--chart-1'),
            borderWidth: 1.5,
            pointStyle: 'rect',
            pointRadius: 3,
            tension: 0,
            fill: false,
          }],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { display: false },
            tooltip: { callbacks: { label: (c) => ' ' + c.parsed.y.toLocaleString('pt-BR') + ' ' + serie.unidade } },
          },
          scales: {
            x: { ticks: { color: token('--m2m-tx-3'), maxTicksLimit: 8 }, grid: { display: false } },
            y: {
              ticks: { color: token('--m2m-tx-3') },
              grid: { color: token('--m2m-ln-2') },
              title: { display: true, text: serie.unidade, color: token('--m2m-tx-3') },
            },
          },
        },
      });

      // O tema muda sem recarregar a página: repinta o gráfico com os tokens novos.
      new MutationObserver(() => {
        const o = grafico.options.scales;
        o.x.ticks.color = token('--m2m-tx-3');
        o.y.ticks.color = token('--m2m-tx-3');
        o.y.grid.color = token('--m2m-ln-2');
        o.y.title.color = token('--m2m-tx-3');
        grafico.data.datasets[0].borderColor = token('--chart-1');
        grafico.data.datasets[0].backgroundColor = token('--chart-1');
        grafico.update('none');
      }).observe(document.body, { attributes: true, attributeFilter: ['class'] });
    }
  }

  /* ── 7 · Configuração: teste de conexão ─────────────────────── */
  const botaoTeste = document.getElementById('m2m-teste-botao');
  if (botaoTeste) {
    botaoTeste.addEventListener('click', async () => {
      const seletor = document.getElementById('m2m-teste-chip');
      const saida = document.getElementById('m2m-teste-resultado');
      if (!seletor.value) { avisar('Escolha um chip para o teste.', 'aviso'); return; }
      botaoTeste.classList.add('is-carregando');
      botaoTeste.disabled = true;
      saida.dataset.tom = '';
      saida.textContent = 'Consultando a API…';
      try {
        const r = await enviar(URLS.testar, { chip_id: seletor.value });
        if (!r.dados) { saida.textContent = mensagemDeFalha(r); saida.dataset.tom = 'erro'; return; }
        const d = r.dados;
        saida.dataset.tom = d.tom || '';
        if (d.recusada) {
          saida.textContent = d.mensagem;
          return;
        }
        saida.replaceChildren();
        saida.appendChild(document.createTextNode('Teste com o chip ' + d.chip + ': '));
        saida.appendChild(criar('b', {}, d.rotulo));
        saida.appendChild(document.createTextNode((d.http_status ? ' (HTTP ' + d.http_status + ')' : '') + ' em ' + d.duracao_ms + ' ms. ' + d.mensagem + ' '));
        if (d.ok) {
          const recarregar = criar('a', { href: '#mapeamento' }, 'Recarregar para usar esta resposta no mapeamento');
          recarregar.addEventListener('click', (e) => { e.preventDefault(); window.location.hash = 'mapeamento'; window.location.reload(); });
          saida.appendChild(recarregar);
        }
      } catch (e) {
        saida.textContent = 'Não foi possível falar com o servidor.';
        saida.dataset.tom = 'erro';
      } finally {
        botaoTeste.classList.remove('is-carregando');
        botaoTeste.disabled = false;
      }
    });
  }

  /* ── 8 · Configuração: a conta de requisições por minuto ────── */
  const camposConta = document.querySelectorAll('[data-m2m-simular]');
  if (camposConta.length && URLS.simular) {
    const caixa = document.getElementById('m2m-conta');
    const form = camposConta[0].form;
    const simular = atrasar(async () => {
      try {
        const r = await enviar(URLS.simular, {
          simular: '1',
          intervalo_minutos: form.elements.intervalo_minutos.value,
          intervalo_repouso_minutos: form.elements.intervalo_repouso_minutos.value,
          limite_req_minuto: form.elements.limite_req_minuto.value,
        }, true);
        if (!r.dados) return;
        document.getElementById('m2m-conta-texto').textContent = r.dados.conta || '';
        document.getElementById('m2m-conta-motivo').textContent = r.dados.motivo || '';
        caixa.dataset.ok = (r.dados.ok && !r.dados.limitado) ? '1' : '0';
      } catch (e) { /* a conta é só informativa: o servidor valida ao salvar */ }
    }, 300);
    camposConta.forEach((c) => c.addEventListener('input', simular));
  }

  /* ── 8b · A faixa de sincronização se atualiza sozinha ───────── */
  /* O servidor só LÊ do banco o que a tarefa do Agendador gravou: esta pergunta nunca dispara consulta na
     API, e a página aberta ou fechada não muda a frequência da sincronização. Pausa com a aba escondida. */
  const faixaSync = document.querySelector('[data-sync-url]');
  if (faixaSync) {
    const INTERVALO_MS = 30000;
    const campo = (nome) => faixaSync.querySelector('[data-sync-campo="' + nome + '"]');
    function texto(nome, valor) { const e = campo(nome); if (e) e.textContent = valor; }
    function aplicar(d) {
      faixaSync.dataset.sync = d.estado;
      const ind = faixaSync.querySelector('[data-sync-ind] .m2m-ind');
      if (ind) {
        ind.dataset.estado = d.ind;
        ind.title = d.motivo || '';
        const rotulo = ind.querySelector('span');
        if (rotulo) rotulo.textContent = d.rotulo;
      }
      texto('ultima', d.ultima_sincronizacao.texto || '—');
      texto('ultima-rotulo', d.ultima_sincronizacao.rotulo || 'nunca concluiu');
      texto('resposta', d.ultima_resposta_valida.texto || '—');
      texto('proxima', d.proxima.texto || '—');
      if (d.ciclo) {
        texto('ciclo', d.ciclo.consultados + ' chip' + (d.ciclo.consultados === 1 ? '' : 's') + ' · ' + d.ciclo.falhas +
          ' falha' + (d.ciclo.falhas === 1 ? '' : 's') + ' · ' + d.ciclo.chamadas + ' req.');
      }
    }
    async function perguntar() {
      if (document.hidden) return;
      try {
        const r = await fetch(faixaSync.dataset.syncUrl, { headers: { 'X-Requested-With': 'XMLHttpRequest' }, credentials: 'same-origin' });
        if (r.ok) aplicar(await r.json());
      } catch (e) { /* sem rede: a faixa fica como estava e a próxima pergunta tenta de novo */ }
    }
    setInterval(perguntar, INTERVALO_MS);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) perguntar(); });
  }

  /* ── 9 · Configuração: prévia ao vivo do mapeamento ─────────── */
  const formMapa = document.getElementById('m2m-form-mapa');
  const previa = document.getElementById('m2m-previa-corpo');
  if (formMapa && previa && URLS.previa) {
    const ROTULO_ESTADO = { ativa: 'Sessão ativa', sem_sessao: 'Sem sessão', nao_interpretado: 'Não interpretado' };
    const SECOES = {
      sessao: ['campo_estado', 'valores_ativa', 'valores_sem_sessao', 'campos_exibir'],
      consumo: ['campo_volume', 'campo_unidade', 'unidade_fixa', 'base', 'campo_periodo_inicio', 'campo_periodo_fim'],
      identidade: ['campo_iccid', 'campo_imsi'],
    };

    function mapaDoFormulario() {
      const mapa = {};
      Object.keys(SECOES).forEach((secao) => {
        mapa[secao] = {};
        SECOES[secao].forEach((campo) => {
          const nome = secao + '__' + campo;
          const marcado = formMapa.querySelector('[name="' + nome + '"]:checked');
          const campoEl = marcado || formMapa.querySelector('[name="' + nome + '"]');
          mapa[secao][campo] = campoEl ? campoEl.value : '';
        });
      });
      return mapa;
    }

    function linha(rotulo, valor) {
      const p = criar('p', { class: 'm2m-sub' });
      p.appendChild(document.createTextNode(rotulo + ' '));
      p.appendChild(criar('b', { class: 'm2m-num' }, valor));
      return p;
    }

    function desenharPrevia(d) {
      previa.replaceChildren();
      if (d.erros && d.erros.length) {
        const ul = criar('ul', { class: 'm2m-previa-erros' });
        d.erros.forEach((e) => ul.appendChild(criar('li', {}, e)));
        previa.appendChild(ul);
        return;
      }
      // Sessão
      previa.appendChild(criar('h5', { class: 'm2m-sub-t' }, 'Sessão'));
      if (!d.sessao) {
        previa.appendChild(criar('p', { class: 'm2m-sub' }, 'Sem resposta de sessão guardada para testar. Faça um teste de conexão.'));
      } else {
        const ind = criar('span', { class: 'm2m-ind', 'data-estado': d.sessao.estado });
        ind.appendChild(criar('i', { class: 'm2m-ind-forma', 'aria-hidden': 'true' }));
        ind.appendChild(criar('span', {}, ROTULO_ESTADO[d.sessao.estado] || d.sessao.estado));
        previa.appendChild(ind);
        if (d.sessao.estado_bruto) previa.appendChild(linha('Valor da API:', d.sessao.estado_bruto));
        if (d.sessao.motivo) previa.appendChild(criar('p', { class: 'm2m-sub' }, d.sessao.motivo));
        (d.sessao.campos_exibir || []).forEach((c) => previa.appendChild(linha(c.caminho + ':', c.valor)));
        if (d.amostras.sessao) previa.appendChild(criar('p', { class: 'm2m-sub' }, 'Sobre a resposta do chip ' + d.amostras.sessao.chip + '.'));
      }
      // Consumo
      previa.appendChild(criar('h5', { class: 'm2m-sub-t' }, 'Consumo'));
      if (!d.consumo) {
        previa.appendChild(criar('p', { class: 'm2m-sub' }, 'Sem resposta de consumo guardada para testar.'));
      } else {
        previa.appendChild(criar('p', { class: 'm2m-num m2m-grande' }, d.consumo.volume_bytes === null ? '—' : d.consumo.volume_formatado));
        if (d.consumo.valor_original) previa.appendChild(linha('Valor original:', d.consumo.valor_original + ' ' + d.consumo.unidade_original));
        if (d.consumo.periodo_inicio || d.consumo.periodo_fim) previa.appendChild(linha('Período:', (d.consumo.periodo_inicio || '?').slice(0, 10) + ' a ' + (d.consumo.periodo_fim || '?').slice(0, 10)));
        if (d.consumo.motivo) previa.appendChild(criar('p', { class: 'm2m-sub' }, d.consumo.motivo));
        if (d.amostras.consumo) previa.appendChild(criar('p', { class: 'm2m-sub' }, 'Sobre a resposta do chip ' + d.amostras.consumo.chip + '.'));
      }
      // Identidade
      if (d.identidade && (d.identidade.iccid || d.identidade.imsi)) {
        previa.appendChild(criar('h5', { class: 'm2m-sub-t' }, 'Identidade na resposta'));
        if (d.identidade.iccid) previa.appendChild(linha('ICCID:', d.identidade.iccid));
        if (d.identidade.imsi) previa.appendChild(linha('IMSI:', d.identidade.imsi));
      }
    }

    const atualizarPrevia = atrasar(async () => {
      try {
        const r = await enviar(URLS.previa, { mapeamento: mapaDoFormulario() });
        if (r.dados) desenharPrevia(r.dados);
        else previa.textContent = mensagemDeFalha(r);
      } catch (e) {
        previa.textContent = 'Não foi possível calcular a prévia.';
      }
    }, 400);

    formMapa.addEventListener('input', atualizarPrevia);
    formMapa.addEventListener('change', atualizarPrevia);
    const m = mapaDoFormulario();
    if (m.sessao.campo_estado || m.consumo.campo_volume) atualizarPrevia();
  }
})();
