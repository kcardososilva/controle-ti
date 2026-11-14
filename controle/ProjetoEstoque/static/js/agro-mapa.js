/* Zelo Agro — o mapa (OpenStreetMap + Leaflet).

   Um .js em static/ é servido CRU: nenhuma tag de template funciona aqui. URL, permissões e filtros iniciais vêm de
   `json_script` (`#agro-mapa-config`). Os pontos vêm do servidor (`agro_mapa_dados`), já filtrados pela permissão.

   ── O que este arquivo NÃO faz ──────────────────────────────────
   Não decide nem deduz posição: cada ponto chega com coordenada, origem ("coordenada cadastral fixa", "GPS",
   "localização da estrutura"), data e ESTADO (GPS atualizado, antigo, sem atualização recente, cadastral fixa) já
   classificados pelo servidor, e é isso que o painel mostra. Não decide qual ícone usar: o servidor manda o id e a
   biblioteca (`ZeloIcones`, services/icone_service.py) desenha. Não chama a API da TIM. Nada vindo do servidor entra
   como HTML: o painel e as dicas são montados com `textContent`.

   ── Foco ────────────────────────────────────────────────────────
   `?veiculo=<id>` (o link "Ver no mapa" do detalhe do chip e do veículo): o veículo é destacado e o mapa centralizado
   na última posição, SE ela existe. Sem coordenada o servidor responde `foco.tem_localizacao = false` e a tela diz
   "localização indisponível" — nunca leva a uma posição inventada. O foco vale na primeira carga; depois o usuário
   mexe nos filtros e no zoom à vontade (o veículo segue destacado enquanto estiver no mapa).

   ── Fundo ───────────────────────────────────────────────────────
   `KQMapa.montarCamadas` (o módulo compartilhado dos mapas do sistema): OpenStreetMap (tiles de openstreetmap.de, o
   servidor que o IP da empresa não tem bloqueado), satélite e outras, com a atribuição obrigatória e a sonda de
   provedor "mudo". Sem ele, cai num OpenStreetMap simples. */
(function () {
  'use strict';
  var cfgEl = document.getElementById('agro-mapa-config');
  var el = document.getElementById('agro-mapa');
  if (!cfgEl || !el || !window.L) return;
  var CFG = JSON.parse(cfgEl.textContent);
  var form = document.getElementById('agro-mapa-form');

  /* ── utilidades ─────────────────────────────────────────────── */
  function criar(tag, atributos, texto) {
    var e = document.createElement(tag);
    Object.keys(atributos || {}).forEach(function (k) { e.setAttribute(k, atributos[k]); });
    if (texto !== undefined && texto !== null && texto !== '') e.textContent = texto;
    return e;
  }

  /* ── o mapa ─────────────────────────────────────────────────── */
  var map = L.map(el, { center: CFG.centro, zoom: CFG.zoom, zoomControl: true, worldCopyJump: true });
  if (window.KQMapa && KQMapa.montarCamadas) {
    KQMapa.montarCamadas(map, { padrao: 'osm' });
  } else {
    L.tileLayer('https://tile.openstreetmap.de/{z}/{x}/{y}.png', {
      maxZoom: 19, attribution: '&copy; colaboradores do <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    }).addTo(map);
  }
  L.control.scale({ metric: true, imperial: false, position: 'bottomleft' }).addTo(map);

  var grupoVeiculos = L.markerClusterGroup({
    showCoverageOnHover: false, maxClusterRadius: 48, spiderfyOnMaxZoom: true, chunkedLoading: true,
    iconCreateFunction: function (cluster) {
      var n = cluster.getChildCount();
      return L.divIcon({ html: '<span class="agro-cluster-n">' + n + '</span>', className: 'agro-cluster', iconSize: [38, 38] });
    },
  });
  var grupoEstruturas = L.markerClusterGroup({
    showCoverageOnHover: false, maxClusterRadius: 40,
    iconCreateFunction: function (cluster) {
      return L.divIcon({ html: '<span class="agro-cluster-n">' + cluster.getChildCount() + '</span>', className: 'agro-cluster agro-cluster--infra', iconSize: [38, 38] });
    },
  });
  var grupoEnlaces = L.layerGroup();
  map.addLayer(grupoEnlaces); map.addLayer(grupoEstruturas); map.addLayer(grupoVeiculos);

  /* ── ícones ─────────────────────────────────────────────────── */
  // `extra.loc` = estado da localização (atualizada | antiga | sem_atualizacao | cadastral); `extra.chip` = tem chip M2M.
  function pino(classe, icone, status, extra) {
    var corpo = criar('span', { 'class': 'agro-pin-corpo', 'data-status': status || '' });
    if (extra && extra.loc) corpo.setAttribute('data-loc', extra.loc);
    var desenho = window.ZeloIcones ? window.ZeloIcones.elemento(icone) : null;
    if (desenho) corpo.appendChild(desenho);
    if (extra && extra.chip) corpo.appendChild(criar('span', { 'class': 'agro-pin-chip', title: 'Tem chip M2M vinculado' }));
    return L.divIcon({ className: 'agro-pin ' + classe, html: corpo, iconSize: [34, 34], iconAnchor: [17, 17], popupAnchor: [0, -15] });
  }
  var ESTADO_ROTULO = {
    atualizada: 'GPS atualizado', antiga: 'GPS antigo', sem_atualizacao: 'GPS sem atualização recente', cadastral: 'Coordenada cadastral fixa',
  };

  var ESTILO_ENLACE = {
    operacional: { color: '#1f6b47', weight: 4, dashArray: null },
    manutencao: { color: '#b45309', weight: 4, dashArray: '8 6' },
    indisponivel: { color: '#b91c1c', weight: 4, dashArray: '2 6' },
    planejado: { color: '#2563eb', weight: 3, dashArray: '10 8' },
    nao_informado: { color: '#6b7280', weight: 3, dashArray: '2 5' },
  };

  /* ── painel lateral ─────────────────────────────────────────── */
  var painel = document.getElementById('agro-mapa-painel');
  var painelCorpo = document.getElementById('agro-mapa-painel-corpo');
  function linha(dl, rotulo, valor, destaque) {
    if (valor === '' || valor === null || valor === undefined) return;
    var d = criar('div', { class: 'agro-mapa-par' });
    d.appendChild(criar('dt', {}, rotulo));
    d.appendChild(criar('dd', destaque ? { class: 'is-destaque' } : {}, valor));
    dl.appendChild(d);
  }
  function abrirPainel(tipoRotulo, titulo, url, preencher) {
    document.getElementById('agro-mapa-painel-tipo').textContent = tipoRotulo;
    document.getElementById('agro-mapa-painel-titulo').textContent = titulo;
    var link = document.getElementById('agro-mapa-painel-link');
    if (url) { link.href = url; link.hidden = false; } else { link.hidden = true; }
    painelCorpo.textContent = '';
    preencher(painelCorpo);
    painel.hidden = false;
  }
  document.getElementById('agro-mapa-painel-fechar').addEventListener('click', function () { painel.hidden = true; });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') painel.hidden = true; });

  function localizacaoLinhas(dl, loc, lat, lon) {
    if (loc.estado_rotulo) linha(dl, 'Estado da localização', loc.estado_rotulo, true);
    linha(dl, 'Origem da localização', loc.rotulo + (loc.fonte ? ' · ' + loc.fonte : ''), !loc.estado_rotulo);
    if (loc.atualizada_em && loc.atualizada_em.texto) {
      linha(dl, loc.origem === 'cadastral' ? 'Cadastrada em' : (loc.origem === 'gps' ? 'Última atualização GPS' : 'Atualizada em'), loc.atualizada_em.texto + (loc.atualizada_em.rotulo ? ' (' + loc.atualizada_em.rotulo + ')' : ''));
    }
    if (loc.precisao_m !== null && loc.precisao_m !== undefined) linha(dl, 'Precisão', String(loc.precisao_m).replace('.', ',') + ' m');
    if (loc.velocidade_kmh !== null && loc.velocidade_kmh !== undefined) linha(dl, 'Velocidade', String(loc.velocidade_kmh).replace('.', ',') + ' km/h');
    if (loc.direcao_graus !== null && loc.direcao_graus !== undefined) linha(dl, 'Direção', loc.direcao_graus + '°');
    if (loc.aviso) dl.appendChild(criar('p', { 'class': 'agro-mapa-aviso' }, loc.aviso));
    linha(dl, 'Coordenada', lat.toFixed(6) + ', ' + lon.toFixed(6));
  }

  function painelDoVeiculo(v) {
    abrirPainel(v.tipo, v.codigo + ' · ' + v.nome, v.url, function (corpo) {
      var dl = criar('dl', { class: 'agro-mapa-dl' });
      linha(dl, 'Fabricante', v.fabricante);
      linha(dl, 'Modelo', v.modelo);
      linha(dl, 'Fazenda / unidade', v.fazenda);
      linha(dl, 'Placa', v.placa);
      linha(dl, 'Status operacional', v.status_rotulo, true);
      if (v.conectividade) linha(dl, 'Conectividade', v.conectividade.rotulo);
      localizacaoLinhas(dl, v.localizacao, v.lat, v.lon);
      corpo.appendChild(dl);
      if (v.chips && v.chips.length) {
        corpo.appendChild(criar('h4', { 'class': 'agro-mapa-sub' }, v.chips.length > 1 ? 'Chips vinculados' : 'Chip vinculado'));
        v.chips.forEach(function (c) {
          var d = criar('dl', { 'class': 'agro-mapa-dl agro-mapa-dl--chip' });
          linha(d, 'Chip', c.nome);
          linha(d, 'ICCID', c.iccid);
          // O que a API disse sobre o chip (sessão, atividade, consulta) só vem no JSON para quem tem permissão de API
          // (`agro_mapa_service._ponto_de_veiculo`): sem a chave, a linha nem é desenhada — não se mostra "nunca consultado"
          // para quem simplesmente não vê esse dado.
          if (c.sessao !== undefined) {
            linha(d, 'Estado da sessão', c.sessao === c.rotulo ? c.sessao : c.sessao + ' · ' + c.rotulo);
            // a atividade é a da SESSÃO (fim da sessão ou último avanço da plataforma); a hora da consulta vem à parte
            linha(d, 'Última atividade conhecida', c.atividade && c.atividade.texto ? c.atividade.texto + ' (' + c.atividade.rotulo + ')' : 'nenhuma atividade de sessão conhecida');
            linha(d, 'Última consulta válida', c.ultima_atualizacao && c.ultima_atualizacao.texto ? c.ultima_atualizacao.texto + ' (' + c.ultima_atualizacao.rotulo + ')' : 'nunca consultado');
          }
          corpo.appendChild(d);
          if (c.url) corpo.appendChild(criar('a', { 'class': 'm2m-btn m2m-btn--sm', href: c.url }, 'Detalhes do chip'));
        });
      } else if (v.tem_chip === false) {
        corpo.appendChild(criar('p', { 'class': 'agro-mapa-aviso' }, 'Nenhum chip M2M vinculado a este equipamento.'));
      }
    });
  }
  function painelDaEstrutura(e) {
    abrirPainel(e.tipo_rotulo, e.nome, e.url, function (corpo) {
      var dl = criar('dl', { class: 'agro-mapa-dl' });
      linha(dl, 'Fazenda / unidade', e.fazenda);
      linha(dl, 'Situação', e.situacao_rotulo, true);
      linha(dl, 'Altura', e.altura_m ? e.altura_m + ' m' : '');
      localizacaoLinhas(dl, e.localizacao, e.lat, e.lon);
      corpo.appendChild(dl);
    });
  }
  function painelDoEnlace(n) {
    abrirPainel('Enlace PTP', n.identificacao, n.url, function (corpo) {
      var dl = criar('dl', { class: 'agro-mapa-dl' });
      linha(dl, 'Ponta A', n.a.nome);
      linha(dl, 'Ponta B', n.b.nome);
      linha(dl, 'Rádio da ponta A', n.radio_a);
      linha(dl, 'Rádio da ponta B', n.radio_b);
      linha(dl, 'Estado registrado', n.estado_rotulo, true);
      if (n.estado_registrado_em && n.estado_registrado_em.texto) {
        linha(dl, 'Registrado em', n.estado_registrado_em.texto + (n.estado_registrado_por ? ' por ' + n.estado_registrado_por : ''));
      }
      linha(dl, 'Distância em linha reta', n.distancia_km.toString().replace('.', ',') + ' km');
      linha(dl, 'Frequência', n.frequencia_ghz ? n.frequencia_ghz.replace('.', ',') + ' GHz' : '');
      corpo.appendChild(dl);
      corpo.appendChild(criar('p', { class: 'agro-mapa-aviso' }, 'A linha mostra onde estão as duas pontas. Não é um teste de comunicação nem mede a qualidade do enlace.'));
    });
  }

  /* ── desenhar ───────────────────────────────────────────────── */
  var tiposVistos = {};
  var marcadores = {};          // id do veículo -> { marker, ponto }
  var estadosDaLegenda = {};
  var temChipNoMapa = false;
  var focoAplicado = false;
  function desenhar(d) {
    grupoVeiculos.clearLayers(); grupoEstruturas.clearLayers(); grupoEnlaces.clearLayers();
    tiposVistos = {};
    var limites = [];

    marcadores = {};
    var estadosVistos = {};
    var comChip = false;
    d.veiculos.forEach(function (v) {
      var estado = (v.localizacao && v.localizacao.estado) || '';
      var m = L.marker([v.lat, v.lon], {
        icon: pino('agro-pin--veiculo', v.icone, v.status, { loc: estado, chip: v.tem_chip }),
        title: v.codigo + ' · ' + v.nome + (estado ? ' — ' + (ESTADO_ROTULO[estado] || '') : ''), keyboard: true,
      });
      m.on('click', function () { painelDoVeiculo(v); });
      grupoVeiculos.addLayer(m);
      marcadores[v.id] = { marker: m, ponto: v };
      limites.push([v.lat, v.lon]);
      tiposVistos['v:' + v.tipo] = { rotulo: v.tipo, icone: v.icone, classe: 'agro-pin--veiculo' };
      if (estado) estadosVistos[estado] = true;
      if (v.tem_chip) comChip = true;
    });
    d.estruturas.forEach(function (e) {
      var m = L.marker([e.lat, e.lon], { icon: pino('agro-pin--infra', e.icone, e.situacao), title: e.nome, keyboard: true });
      m.on('click', function () { painelDaEstrutura(e); });
      grupoEstruturas.addLayer(m);
      limites.push([e.lat, e.lon]);
      tiposVistos['e:' + e.tipo] = { rotulo: e.tipo_rotulo, icone: e.icone, classe: 'agro-pin--infra' };
    });
    d.enlaces.forEach(function (n) {
      var estilo = ESTILO_ENLACE[n.estado] || ESTILO_ENLACE.nao_informado;
      var linhaMapa = L.polyline([[n.a.lat, n.a.lon], [n.b.lat, n.b.lon]], { color: estilo.color, weight: estilo.weight, dashArray: estilo.dashArray, opacity: 0.9 });
      linhaMapa.bindTooltip(n.identificacao + ' · ' + n.distancia_km.toString().replace('.', ',') + ' km · ' + n.estado_rotulo, { sticky: true });
      linhaMapa.on('click', function () { painelDoEnlace(n); });
      grupoEnlaces.addLayer(linhaMapa);
      limites.push([n.a.lat, n.a.lon], [n.b.lat, n.b.lon]);
    });

    ['veiculos', 'estruturas', 'enlaces'].forEach(function (c) {
      var t = document.querySelector('[data-mapa-total="' + c + '"]');
      if (t) t.textContent = d.totais[c];
    });
    estadosDaLegenda = estadosVistos;
    temChipNoMapa = comChip;
    legenda(d);
    avisoSem(d);
    listaSemChip(d);
    var estado = document.getElementById('agro-mapa-estado');
    if (!limites.length) {
      var comChipSem = d.semLocalizacaoComChip && d.semLocalizacaoComChip.total;
      estado.textContent = 'Nenhum ponto com coordenada para estes filtros: o mapa só desenha o que tem coordenada cadastrada ou GPS, e a posição nunca é deduzida do chip.'
        + (comChipSem ? ' ' + comChipSem + ' veículo(s) com chip M2M ainda não têm localização — veja a faixa acima do mapa.' : '');
      estado.hidden = false;
    } else {
      estado.hidden = true;
      var vaiFocar = CFG.foco && !focoAplicado && d.foco && d.foco.tem_localizacao;
      if (!d._semAjuste && !vaiFocar) map.fitBounds(L.latLngBounds(limites), { padding: [48, 48], maxZoom: 15 });
    }
    if (d.limite) {
      estado.textContent = 'Há mais veículos com coordenada do que o mapa carrega por vez: refine os filtros.';
      estado.hidden = false;
    }
    aplicarFoco(d);
  }

  function legenda(d) {
    var alvo = document.getElementById('agro-mapa-legenda');
    alvo.textContent = '';
    Object.keys(tiposVistos).sort().forEach(function (k) {
      var t = tiposVistos[k];
      var item = criar('span', { 'class': 'agro-legenda-item' });
      var simbolo = criar('span', { 'class': 'agro-legenda-pino ' + t.classe });
      var desenho = window.ZeloIcones ? window.ZeloIcones.elemento(t.icone) : null;
      if (desenho) simbolo.appendChild(desenho);
      item.appendChild(simbolo);
      item.appendChild(document.createTextNode(t.rotulo));
      alvo.appendChild(item);
    });
    // O estado da localização: o contorno do marcador diz se a posição é atual, antiga, sem atualização ou cadastrada.
    Object.keys(ESTADO_ROTULO).forEach(function (estado) {
      if (!estadosDaLegenda[estado]) return;
      var item = criar('span', { 'class': 'agro-legenda-item' });
      item.appendChild(criar('span', { 'class': 'agro-legenda-estado', 'data-loc': estado }));
      item.appendChild(document.createTextNode(ESTADO_ROTULO[estado]));
      alvo.appendChild(item);
    });
    if (temChipNoMapa) {
      var comChipItem = criar('span', { 'class': 'agro-legenda-item' });
      comChipItem.appendChild(criar('span', { 'class': 'agro-legenda-chip' }));
      comChipItem.appendChild(document.createTextNode('Com chip M2M'));
      alvo.appendChild(comChipItem);
    }
    if (d.enlaces.length) {
      var e = criar('span', { class: 'agro-legenda-item' }, 'Linha = enlace PTP (traço = estado registrado)');
      alvo.appendChild(e);
    }
  }

  /* O veículo em foco (link "Ver no mapa" do chip): destaca e centraliza na última posição VÁLIDA. Sem coordenada, avisa. */
  function aplicarFoco(d) {
    var estado = document.getElementById('agro-mapa-estado');
    if (!CFG.foco || !d.foco) return;
    if (!d.foco.tem_localizacao) {
      estado.textContent = (d.foco.rotulo ? d.foco.rotulo + ': ' : '') + d.foco.mensagem;
      estado.hidden = false;
      return;
    }
    var reg = marcadores[d.foco.veiculo];
    if (!reg) return;
    var destacar = function () {
      var el = reg.marker.getElement && reg.marker.getElement();
      if (el) el.classList.add('agro-pin--foco');
      painelDoVeiculo(reg.ponto);
    };
    if (focoAplicado) { destacar(); return; }          // refiltrou: mantém o destaque, sem mexer no zoom
    focoAplicado = true;
    map.setView([reg.ponto.lat, reg.ponto.lon], Math.max(map.getZoom(), 16));
    grupoVeiculos.zoomToShowLayer(reg.marker, function () {
      destacar();
      // O painel de detalhes cobre parte do mapa (a metade de baixo no celular, a lateral direita no desktop): puxa o
      // veículo para a parte que sobra, senão ele fica ESCONDIDO atrás do painel que acabou de abrir.
      var painelEl = document.getElementById('agro-mapa-painel');
      if (painelEl && !painelEl.hidden) {
        var cobreBaixo = painelEl.offsetWidth > el.clientWidth * 0.7;
        map.panBy(cobreBaixo ? [0, Math.round(painelEl.offsetHeight / 2)] : [Math.round(painelEl.offsetWidth / 2), 0], { animate: false });
      }
    });
  }

  /* Veículos que TÊM chip M2M e não têm localização: o mapa não os desenha (a posição nunca é deduzida do chip), mas
     o usuário precisa ver QUAIS são. Cada um leva ao cadastro, onde a coordenada se registra. */
  function listaSemChip(d) {
    var bloco = document.getElementById('agro-mapa-sem-chip');
    if (!bloco) return;
    var s = d.semLocalizacaoComChip;
    if (!s || !s.total) { bloco.hidden = true; return; }
    document.getElementById('agro-mapa-sem-chip-t').textContent = s.total + (s.total === 1
      ? ' veículo com chip M2M ainda não tem localização e por isso não aparece no mapa'
      : ' veículos com chip M2M ainda não têm localização e por isso não aparecem no mapa');
    bloco.open = s.total <= 5;           // poucos: já abertos; muitos: recolhidos (a faixa não empurra o mapa para baixo)
    var ul = document.getElementById('agro-mapa-sem-chip-lista');
    ul.textContent = '';
    s.itens.forEach(function (v) {
      var li = criar('li');
      var a = criar('a', { href: v.url, title: 'Abrir o equipamento e registrar a localização' });
      var icone = window.ZeloIcones ? window.ZeloIcones.elemento(v.icone) : null;
      if (icone) a.appendChild(icone);
      a.appendChild(criar('span', {}, v.codigo + ' · ' + v.nome));
      li.appendChild(a);
      if (v.chips && v.chips.length) li.appendChild(criar('small', {}, v.chips.join(', ')));
      ul.appendChild(li);
    });
    if (s.total > s.itens.length) ul.appendChild(criar('li', { 'class': 'agro-mapa-sem-chip-mais' }, 'e mais ' + (s.total - s.itens.length) + '…'));
    bloco.hidden = false;
  }

  function avisoSem(d) {
    var p = document.getElementById('agro-mapa-sem');
    var partes = [];
    if (d.semLocalizacao.veiculos) partes.push(d.semLocalizacao.veiculos + ' veículo(s) sem coordenada');
    if (d.semLocalizacao.estruturas) partes.push(d.semLocalizacao.estruturas + ' estrutura(s) sem coordenada');
    if (d.semLocalizacao.enlaces) partes.push(d.semLocalizacao.enlaces + ' enlace(s) com uma ponta sem coordenada');
    if (!partes.length) { p.hidden = true; return; }
    p.textContent = 'Localização desconhecida: ' + partes.join('; ') + '. Não aparecem no mapa — a posição não é deduzida. Cadastre a coordenada no cadastro do ativo.';
    p.hidden = false;
  }

  /* ── dados e filtros ────────────────────────────────────────── */
  var emCurso = 0;
  function parametros() {
    var dados = new FormData(form);
    var p = new URLSearchParams();
    dados.forEach(function (v, k) { if (String(v).trim() !== '') p.append(k, v); });
    if (CFG.foco) p.set('veiculo', CFG.foco);
    var caixas = form.querySelectorAll('input[name="camada"]');
    if (caixas.length && !p.has('camada')) p.append('camada', 'nenhuma');     // tudo desmarcado = mapa vazio
    return p;
  }
  function carregar(semAjuste) {
    var p = parametros();
    var minha = ++emCurso;
    var estado = document.getElementById('agro-mapa-estado');
    el.classList.add('is-carregando');
    estado.textContent = 'Carregando…'; estado.hidden = false;
    var url = CFG.url_dados + (p.toString() ? '?' + p.toString() : '');
    // A URL da página acompanha os filtros: dá para copiar o link ou voltar da lista com a mesma seleção.
    try { window.history.replaceState(null, '', window.location.pathname + (p.toString() ? '?' + p.toString() : '')); } catch (e) { /* sem history */ }
    return fetch(url, { credentials: 'same-origin', headers: { 'X-Requested-With': 'XMLHttpRequest' } })
      .then(function (r) { if (!r.ok) throw new Error('http ' + r.status); return r.json(); })
      .then(function (d) {
        if (minha !== emCurso) return;      // chegou uma resposta mais nova no meio-tempo
        d._semAjuste = !!semAjuste;
        desenhar(d);
      })
      .catch(function () {
        if (minha !== emCurso) return;
        estado.textContent = 'Não foi possível carregar o mapa agora. Os pontos anteriores continuam na tela; tente de novo.';
        estado.hidden = false;
      })
      .then(function () { if (minha === emCurso) el.classList.remove('is-carregando'); });
  }
  var adiar = null;
  function aoMudar() { window.clearTimeout(adiar); adiar = window.setTimeout(function () { carregar(false); }, 250); }
  form.addEventListener('change', aoMudar);
  form.addEventListener('input', function (e) { if (e.target && e.target.type === 'search') aoMudar(); });
  form.addEventListener('submit', function (e) { e.preventDefault(); carregar(false); });
  var limpar = document.querySelector('[data-mapa-limpar]');
  if (limpar) limpar.addEventListener('click', function (e) {
    e.preventDefault();
    form.reset();
    // `reset` volta ao que o servidor entregou; "limpar" é esvaziar.
    form.querySelectorAll('input[type="search"]').forEach(function (i) { i.value = ''; });
    form.querySelectorAll('select').forEach(function (s) { s.value = ''; if (window.jQuery) window.jQuery(s).trigger('change.select2'); });
    form.querySelectorAll('input[name="camada"]').forEach(function (c) { c.checked = true; });
    carregar(false);
  });

  // No celular os filtros ocupam a tela antes do mapa: começam recolhidos (o botão abre), e no desktop ficam abertos.
  var corpoFiltros = document.getElementById('agro-mapa-filtros-corpo');
  var botaoRecolher = document.getElementById('agro-mapa-recolher');
  function recolher(sim) {
    corpoFiltros.hidden = sim;
    botaoRecolher.textContent = sim ? 'Mostrar filtros' : 'Recolher';
    botaoRecolher.setAttribute('aria-expanded', sim ? 'false' : 'true');
    window.setTimeout(function () { map.invalidateSize(); }, 50);
  }
  if (corpoFiltros && botaoRecolher) {
    botaoRecolher.addEventListener('click', function () { recolher(!corpoFiltros.hidden); });
    if (window.matchMedia && window.matchMedia('(max-width: 960px)').matches) recolher(true);
  }

  // camadas pedidas pela URL (`?camada=veiculos`): o servidor entrega todas marcadas; desmarca o que não foi pedido
  if (CFG.camadas && CFG.camadas.length) {
    form.querySelectorAll('input[name="camada"]').forEach(function (c) { c.checked = CFG.camadas.indexOf(c.value) > -1; });
  }
  // O Leaflet mede o contêiner ao nascer; se o layout ainda está assentando (fontes, painel de filtros), o fundo sai
  // numa faixa só da área. Reavalia o tamanho quando a página termina de carregar e sempre que o contêiner muda.
  window.addEventListener('load', function () { map.invalidateSize(); });
  if (window.ResizeObserver) new ResizeObserver(function () { map.invalidateSize(); }).observe(el);
  map.whenReady(function () { carregar(false); });
})();
