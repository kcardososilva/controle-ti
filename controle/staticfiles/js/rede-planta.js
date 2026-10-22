/* =============================================================
   rede-planta.js — a rede desenhada SOBRE O MAPA REAL.

   ── Por que esta tela não é a bancada ───────────────────────────
   A bancada (`rede-mapa.js`) desenha o DIAGRAMA: quem liga em quem,
   em que porta, sob uma transformação de pan/zoom própria sobre
   `.nx-mundo`. Aqui se desenha o TERRENO: onde o rack está, por onde
   a vala de fibra passa, quantos metros tem o lance.

   São duas telas porque são duas perguntas. Um diagrama de rede não
   tem escala — a caixa do switch é do tamanho que couber o nome. Uma
   planta tem escala, e é dela que sai o número que decide o projeto:
   90 m é o limite do lance permanente em par trançado, e um lance de
   140 m desenhado como UTP é um lance que não funciona depois de
   instalado.

   ── O fundo do mapa, e por que ele vem do Quiosque ──────────────
   `KQMapa.CAMADAS` (`front/quiosque/_kq_mapa_js.html`) é a fonte
   única das camadas de tile deste sistema — ver Architecture Rule 15
   do CLAUDE.md. Não é reaproveitamento por economia de código:
   `tile.openstreetmap.org` responde "403 Access blocked" ao IP desta
   empresa com status 200 e PNG válido, o CARTO passou a exigir chave
   do mesmo jeito silencioso, e nenhum dos dois dispara `tileerror`.
   A sonda `KQMapa._sondarPlaceholder` — que compara os BYTES de dois
   tiles distantes — é a única defesa que existe contra isso. Escolher
   provedor próprio aqui seria cair no mesmo buraco pela terceira vez.

   ── A medida ────────────────────────────────────────────────────
   O número que aparece enquanto se desenha é PREVIEW: sai de
   `map.distance()`, a haversine do Leaflet. Quem grava é o servidor
   (`rede_topologia_service.comprimento_rota`), com o mesmo raio médio
   WGS84 — então os dois concordam, mas a verdade é a do servidor. Um
   cliente que mandasse o comprimento junto do traçado poderia fazer
   os dois discordarem, e o relatório passaria a afirmar um metro que
   o desenho não tem.

   Dado do servidor entra por `json_script` e é lido UMA vez, abaixo:
   um `.js` em `static/` é servido cru, sem passar pelo motor de
   template, e uma tag de URL aqui viraria texto literal. Há teste
   varrendo isso.
   ============================================================= */
'use strict';

const CFG = JSON.parse(document.getElementById('__planta_config').textContent);

const PLANTA_URL     = CFG.planta_url;
const POSICAO_URL    = CFG.posicao_url;
const ROTA_URL       = CFG.rota_url;
const RACK_URL       = CFG.rack_url;
const ELEVACAO_URL   = CFG.elevacao_url;
const IMPACTO_URL    = CFG.impacto_url;
const INVENTARIO_URL = CFG.inventario_url;
const EXPORTAR_URL   = CFG.exportar_url;
const MAPA_URL       = CFG.mapa_url;
const CSRF_TOKEN     = CFG.csrf_token;
const PRTG_OK        = CFG.prtg_ok;
const CENTRO_PADRAO  = CFG.centro_padrao;
const LIMITE_UTP     = CFG.limite_utp;
const FOLGA_UTP      = CFG.folga_utp;

/* ── Estado ─────────────────────────────────────────────────────── */
let planta = { racks: [], equipamentos: [], rotas: [], kpis: {}, avisos: [], centro: CENTRO_PADRAO };
let mapa = null;

// Camadas do Leaflet, por natureza do objeto. Separadas para o
// liga/desliga do trilho poder remover um grupo inteiro de uma vez.
const camadas = {};
const marcadores = { rack: new Map(), equip: new Map() };
const tracados = new Map();          // rota id → { linha, vertices: [] }

let modo = 'selecao';                // 'selecao' | 'rota' | 'rack' | 'pino'
let rascunho = null;                 // { pontos: [], linha, marcas: [], tipo }
let selecionado = null;              // { tipo: 'rota'|'rack'|'equip', id }
let itemParaPosicionar = null;       // item_id esperando um clique no mapa
let rotaEmEdicao = null;             // id da rota com vértices arrastáveis

const filtros = { classes: new Set(), familias: new Set(), racks: true, equip: true };
const el = {};

/* =========================================================
   Utilidades
   ========================================================= */
function $(id) { return document.getElementById(id); }

function criar(tag, classe, texto) {
  const n = document.createElement(tag);
  if (classe) n.className = classe;
  if (texto !== undefined) n.textContent = texto;
  return n;
}

/* Todo texto do banco entra por `textContent`, nunca por `innerHTML`
   — nome de rack e de rota é digitado por gente, e um `<img onerror>`
   num rótulo seria XSS na tela de quem abrir a planta. O `divIcon` do
   Leaflet aceita um Element em `html` desde a 1.x, então o marcador
   também é montado no DOM, não por string. */
function texto(no, valor) { no.textContent = valor == null ? '' : String(valor); }

async function pedir(url, opcoes) {
  const resp = await fetch(url, {
    credentials: 'same-origin',
    headers: { 'X-Requested-With': 'XMLHttpRequest' },
    ...(opcoes || {}),
  });
  if (!resp.ok && resp.status !== 400) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

function enviar(url, corpo) {
  return pedir(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRFToken': CSRF_TOKEN,
      'X-Requested-With': 'XMLHttpRequest',
    },
    body: JSON.stringify(corpo),
  });
}

let timerToast = null;
function avisar(msg, tom) {
  const t = el.toast;
  texto(t, msg);
  t.dataset.tom = tom || '';
  t.classList.add('is-visivel');
  clearTimeout(timerToast);
  timerToast = setTimeout(() => t.classList.remove('is-visivel'), 4200);
}

/* Metros com a unidade que a pessoa usa: abaixo de mil, metros
   redondos; acima, quilômetro com uma casa. "1.240 m" é um número que
   ninguém lê de olho num mapa. */
function metros(m) {
  const n = Number(m) || 0;
  if (n >= 1000) return `${(n / 1000).toFixed(2)} km`;
  return `${Math.round(n)} m`;
}

/* =========================================================
   Vocabulário visual — o mesmo da bancada, de propósito
   ========================================================= */
const ICONES = {
  switch:    'M3 8h18v8H3zM6 11v2M9 11v2M12 11v2M15 11v2M18 11v2',
  wlc:       'M3 7h18v10H3zM6 10h4M6 13h4M15 10h3M15 13h3',
  ap:        'M12 18v2M8.5 14a5 5 0 0 1 7 0M5.5 11a9 9 0 0 1 13 0M12 16.5h.01',
  antena:    'M12 21V9m0 0-4-6m4 6 4-6M7 13h10',
  radio:     'M12 21V11m-5 10 5-10 5 10M9 7a3 3 0 0 1 6 0',
  starlink:  'M4 20c7 0 13-6 13-13M8 20a5 5 0 0 0 5-5M20 4l-4 4',
  firewall:  'M4 5h16v14H4zM4 10h16M4 15h16M9 5v5M15 10v5M9 15v4',
  roteador:  'M3 13h18v6H3zM7 16h.01M11 16h.01M12 10V4m0 0 3 3m-3-3L9 7',
  nvr:       'M3 8h18v9H3zM6 12.5h6M17 12.5h.01',
  camera:    'M4 7h10l2 3v4H4zM16 10l4-2v8l-4-2M7 17v2',
  desktop:   'M3 5h18v10H3zM9 19h6M12 15v4',
  notebook:  'M5 6h14v9H5zM3 18h18l-2-3H5z',
  impressora:'M7 9V4h10v5M5 9h14v7H5zM8 16h8v4H8z',
  nobreak:   'M6 3h12v18H6zM10 8l-2 5h3l-1 4 3-5h-3z',
  patch:     'M3 9h18v6H3zM7 9v6M11 9v6M15 9v6M19 9v6',
  injector:  'M5 10h9v4H5zM14 12h5M9 6v4M9 14v4',
  conversor: 'M4 10h7v4H4zM13 10h7v4h-7zM11 12h2',
  gbic:      'M5 10h11l3 2-3 2H5z',
  telefone:  'M7 4h10v16H7zM10 8h4M10 11h4M10 14h4',
  generico:  'M5 5h14v14H5zM9 9h6v6H9z',
};

function iconeSvg(classe) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  p.setAttribute('d', ICONES[classe] || ICONES.generico);
  p.setAttribute('stroke-linecap', 'square');
  svg.appendChild(p);
  return svg;
}

const COR_CLASSE = {
  switch: 'var(--chart-1)',    wlc: 'var(--chart-6)',      ap: 'var(--chart-3)',
  antena: 'var(--chart-3)',    radio: 'var(--chart-6)',    starlink: 'var(--chart-5)',
  firewall: 'var(--chart-4)',  roteador: 'var(--chart-4)', nvr: 'var(--chart-7)',
  camera: 'var(--chart-7)',    desktop: 'var(--chart-8)',  notebook: 'var(--chart-8)',
  impressora: 'var(--chart-2)', nobreak: 'var(--chart-2)', patch: 'var(--chart-8)',
  injector: 'var(--chart-2)',  conversor: 'var(--chart-6)', gbic: 'var(--chart-6)',
  telefone: 'var(--chart-8)',  generico: 'var(--chart-8)',
};

const ROTULO_CLASSE = {
  switch: 'Switch', wlc: 'Controladora', ap: 'Access point', antena: 'Antena',
  radio: 'Rádio / enlace', starlink: 'Starlink', firewall: 'Firewall',
  roteador: 'Roteador', nvr: 'NVR / DVR', camera: 'Câmera', desktop: 'Desktop',
  notebook: 'Notebook', impressora: 'Impressora', nobreak: 'Nobreak',
  patch: 'Patch panel', injector: 'Injetor PoE', conversor: 'Conversor',
  gbic: 'GBIC', telefone: 'IP Phone', generico: 'Outro',
};

const ROTULO_FAMILIA = {
  fibra: 'Fibra óptica', utp: 'Par trançado (UTP)', radio: 'Enlace de rádio',
  eletrico: 'Alimentação elétrica', coaxial: 'Coaxial', duto: 'Duto / reserva',
};

/* Traço da rota no mapa. A cor é de DOMÍNIO (regra 33) e sai do
   token, lido do CSS: a folha é a fonte da paleta, e repetir o hex
   aqui faria o mapa discordar do resto do módulo no tema escuro. */
function corDaFamilia(familia) {
  const token = {
    fibra: '--nx-fibra', utp: '--nx-utp', radio: '--nx-radio',
    eletrico: '--nx-stack', coaxial: '--nx-logico', duto: '--nx-tx-3',
  }[familia] || '--nx-tx-3';
  const lido = getComputedStyle(el.app).getPropertyValue(token).trim();
  return lido || 'currentColor';
}

// Tracejado por forma de instalação: aéreo e subterrâneo rompem por
// motivos diferentes, e distinguir os dois no traço é o que permite
// ler a planta sem abrir cada rota.
const TRACO = {
  subterraneo: null, aereo: '9 5', embutido: '2 4',
  canaleta: '14 4', lancado: '4 4', visada: '1 6',
};

/* =========================================================
   Carga
   ========================================================= */
function paramsEscopo(extra) {
  const p = new URLSearchParams();
  if (el.localidade && el.localidade.value) p.set('localidade', el.localidade.value);
  if (el.area && el.area.value) p.set('area', el.area.value);
  Object.entries(extra || {}).forEach(([k, v]) => p.set(k, v));
  return p;
}

async function carregar(enquadrarDepois) {
  el.mapa.setAttribute('aria-busy', 'true');
  try {
    const p = paramsEscopo({ status: PRTG_OK ? '1' : '0' });
    const dados = await pedir(`${PLANTA_URL}?${p.toString()}`);
    if (!dados.ok) throw new Error(dados.erro || 'Falha ao montar a planta.');
    planta = dados;
    // O mapa pode não existir ainda: a primeira carga acontece ANTES de
    // `iniciarMapa`, porque o centro inicial sai da média do que está
    // posicionado — abrir no lugar errado para depois saltar é um piscar
    // que faz a tela parecer instável. Quem desenha a primeira vez é o
    // `init`, logo depois de montar o mapa.
    if (mapa) desenharTudo();
    montarListas();
    montarAvisos();
    montarStatus();
    if (enquadrarDepois) enquadrar();
  } catch (erro) {
    avisar(`Não foi possível carregar a planta: ${erro.message}`, 'ruim');
  } finally {
    el.mapa.removeAttribute('aria-busy');
  }
}

/* =========================================================
   O mapa
   ========================================================= */
function iniciarMapa() {
  mapa = L.map('mapaGeo', {
    zoomControl: true,
    attributionControl: true,
    // O Leaflet tem rolagem-para-zoom por padrão. Numa página que
    // rola, isso rouba a rolagem da página; aqui a bancada ocupa a
    // tela inteira e não rola, então a roda é do mapa.
    scrollWheelZoom: true,
    maxZoom: 21,
  }).setView(planta.centro || CENTRO_PADRAO, planta.tem_posicao ? 16 : 13);

  // Camadas de fundo, seletor e a sonda de placeholder: tudo do
  // KQMapa (Architecture Rule 15). Satélite como padrão porque é o
  // que mostra o galpão e o caminho da vala; a camada "Ruas" fica no
  // seletor para quem precisa do nome da estrada.
  KQMapa.montarCamadas(mapa, { padrao: 'satelite' });
  KQMapa.avisoNitidez(mapa);

  camadas.rotas = L.layerGroup().addTo(mapa);
  camadas.racks = L.layerGroup().addTo(mapa);
  camadas.equip = L.layerGroup().addTo(mapa);
  camadas.rascunho = L.layerGroup().addTo(mapa);
  camadas.vertices = L.layerGroup().addTo(mapa);

  mapa.on('click', clicarNoMapa);
  mapa.on('mousemove', moverNoMapa);
  mapa.on('zoomend moveend', montarStatus);
}

function enquadrar() {
  const pontos = [];
  planta.racks.forEach((r) => { if (r.lat !== null) pontos.push([r.lat, r.lon]); });
  planta.equipamentos.forEach((e) => { if (e.lat !== null) pontos.push([e.lat, e.lon]); });
  planta.rotas.forEach((r) => (r.pontos || []).forEach((p) => pontos.push(p)));
  if (!pontos.length) {
    mapa.setView(planta.centro || CENTRO_PADRAO, 13);
    return;
  }
  mapa.fitBounds(L.latLngBounds(pontos).pad(0.15));
}

/* =========================================================
   Desenho: rotas, racks e equipamentos
   ========================================================= */
function desenharTudo() {
  desenharRotas();
  desenharRacks();
  desenharEquipamentos();
}

function visivelRota(r) {
  if (filtros.familias.size && !filtros.familias.has(r.familia)) return false;
  return true;
}

function desenharRotas() {
  camadas.rotas.clearLayers();
  tracados.clear();

  planta.rotas.forEach((r) => {
    const pontos = r.pontos || [];
    if (pontos.length < 2 || !visivelRota(r)) return;

    const estourou = r.cobre && r.comprimento_m > LIMITE_UTP;
    const cor = estourou
      ? getComputedStyle(el.app).getPropertyValue('--nx-st-down').trim()
      : corDaFamilia(r.familia);

    // Duas linhas sobrepostas: a de baixo é alvo de clique (larga e
    // transparente), a de cima é o traço. Um traço de 3px é impossível
    // de acertar com o mouse num mapa — mesma solução do `.nx-fio-alvo`
    // da bancada.
    const alvo = L.polyline(pontos, {
      color: cor, weight: 16, opacity: 0, interactive: true,
    });
    const linha = L.polyline(pontos, {
      color: cor,
      weight: r.familia === 'fibra' ? 3 : 2.5,
      dashArray: TRACO[r.instalacao] || null,
      interactive: false,
      lineCap: 'butt',
    });

    // Conteúdo como ELEMENTO, não string: `bindTooltip` com string cai em
    // `innerHTML` por dentro do Leaflet, e nome de rota é digitado por gente.
    const rot = criar('span');
    texto(rot, `${r.nome} · ${metros(r.comprimento_m)}`);
    alvo.bindTooltip(rot, { className: 'nx-rota-rot', sticky: true, opacity: 1 });
    alvo.on('click', (ev) => {
      L.DomEvent.stopPropagation(ev);
      if (modo !== 'selecao') return;
      selecionarRota(r.id);
    });

    camadas.rotas.addLayer(alvo);
    camadas.rotas.addLayer(linha);
    tracados.set(r.id, { linha, alvo });
  });
}

function pinoRack(r) {
  const cx = criar('div', 'nx-geo-rack');
  cx.appendChild(criar('b', '', r.nome));
  cx.appendChild(criar('span', '',
    `${r.us_ocupados}/${r.us}U · ${r.equipamentos} eq.`));
  return cx;
}

function desenharRacks() {
  camadas.racks.clearLayers();
  marcadores.rack.clear();
  if (!filtros.racks) return;

  planta.racks.forEach((r) => {
    if (r.lat === null || r.lon === null) return;
    const marca = L.marker([r.lat, r.lon], {
      icon: L.divIcon({
        className: 'nx-geo-wrap',
        html: pinoRack(r),
        iconSize: null,
        // Âncora na BORDA ESQUERDA, no meio da altura: é a aresta marcada
        // do selo que cai sobre a coordenada. Com [0,0] o canto superior
        // esquerdo é que marcaria o ponto, e o selo inteiro ficaria
        // deslocado para baixo e para a direita do lugar real — numa
        // planta em que se mede metro, isso é erro de desenho.
        iconAnchor: [0, 15],
      }),
      draggable: true,
      title: `${r.nome}${r.codigo ? ` (${r.codigo})` : ''}\n${r.ambiente || ''}`,
      zIndexOffset: 400,
    });
    marca.on('click', (ev) => {
      L.DomEvent.stopPropagation(ev);
      selecionarRack(r.id);
    });
    marca.on('dragend', async () => {
      const ll = marca.getLatLng();
      try {
        const resp = await enviar(RACK_URL, {
          rack: r.id, latitude: ll.lat, longitude: ll.lng,
        });
        if (!resp.ok) throw new Error(resp.erro || 'Falha');
        avisar(`${r.nome} reposicionado.`, 'ok');
        carregar(false);
      } catch (erro) {
        // Devolve o pino ao lugar: deixá-lo onde o arrasto parou faria
        // a tela afirmar uma posição que o banco não tem.
        marca.setLatLng([r.lat, r.lon]);
        avisar(`Não foi possível gravar a posição: ${erro.message}`, 'ruim');
      }
    });
    camadas.racks.addLayer(marca);
    marcadores.rack.set(r.id, marca);
  });
}

function pinoEquip(e) {
  const cx = criar('div', 'nx-geo-pino');
  cx.dataset.st = e.status ? e.status.slug : 'nao_monitorado';
  cx.dataset.herdada = e.geo_herdada ? '1' : '0';
  cx.style.setProperty('--nx-classe', COR_CLASSE[e.classe] || COR_CLASSE.generico);
  cx.appendChild(iconeSvg(e.classe));
  cx.appendChild(criar('b', '', e.nome));
  cx.appendChild(criar('i'));
  return cx;
}

function visivelEquip(e) {
  if (!filtros.equip) return false;
  if (filtros.classes.size && !filtros.classes.has(e.classe)) return false;
  return true;
}

function desenharEquipamentos() {
  camadas.equip.clearLayers();
  marcadores.equip.clear();

  planta.equipamentos.forEach((e) => {
    if (e.lat === null || e.lon === null || !visivelEquip(e)) return;
    const marca = L.marker([e.lat, e.lon], {
      icon: L.divIcon({
        className: 'nx-geo-wrap',
        html: pinoEquip(e),
        iconSize: null,
        iconAnchor: [0, 10],      // ver o comentário em `desenharRacks`
      }),
      draggable: true,
      title: `${e.nome}\n${ROTULO_CLASSE[e.classe] || e.classe}`
        + (e.ip ? `\n${e.ip}` : '')
        + (e.rack_nome ? `\nRack ${e.rack_nome}${e.rack_u ? ` · U${e.rack_u}` : ''}` : '')
        + (e.geo_herdada ? '\n(posição herdada do rack)' : ''),
    });
    marca.on('click', (ev) => {
      L.DomEvent.stopPropagation(ev);
      selecionarEquip(e.item_id);
    });
    marca.on('dragend', async () => {
      const ll = marca.getLatLng();
      try {
        const resp = await enviar(POSICAO_URL, {
          item: e.item_id, latitude: ll.lat, longitude: ll.lng,
        });
        if (!resp.ok) throw new Error(resp.erro || 'Falha');
        avisar(`${e.nome} posicionado.`, 'ok');
        carregar(false);
      } catch (erro) {
        marca.setLatLng([e.lat, e.lon]);
        avisar(`Não foi possível gravar a posição: ${erro.message}`, 'ruim');
      }
    });
    camadas.equip.addLayer(marca);
    marcadores.equip.set(e.item_id, marca);
  });
}

/* =========================================================
   Desenhar rota: clique por clique, com a medida ao vivo
   ========================================================= */
function comecarRota() {
  trocarModo('rota');
  rascunho = { pontos: [], linha: null, marcas: [] };
  camadas.rascunho.clearLayers();
  mostrarMedida(0, el.rotaTipo ? el.rotaTipo.value : '');
  avisar('Clique no mapa para marcar o traçado. Enter fecha, Backspace desfaz o último ponto, Esc cancela.');
}

function pontoDoRascunho(ll) {
  rascunho.pontos.push([ll.lat, ll.lng]);
  const bola = L.circleMarker(ll, {
    radius: 4, weight: 2, color: corDaFamilia('fibra'),
    fillColor: getComputedStyle(el.app).getPropertyValue('--nx-panel-2').trim() || '#fff',
    fillOpacity: 1,
  });
  camadas.rascunho.addLayer(bola);
  rascunho.marcas.push(bola);
  redesenharRascunho();
}

function desfazerPonto() {
  if (!rascunho || !rascunho.pontos.length) return;
  rascunho.pontos.pop();
  const bola = rascunho.marcas.pop();
  if (bola) camadas.rascunho.removeLayer(bola);
  redesenharRascunho();
}

function redesenharRascunho(preview) {
  if (!rascunho) return;
  if (rascunho.linha) camadas.rascunho.removeLayer(rascunho.linha);
  const pontos = preview ? [...rascunho.pontos, preview] : rascunho.pontos;
  if (pontos.length >= 2) {
    rascunho.linha = L.polyline(pontos, {
      color: corDaFamilia(familiaDoTipo(el.rotaTipo.value)),
      weight: 3,
      dashArray: '6 4',
      interactive: false,
    });
    camadas.rascunho.addLayer(rascunho.linha);
  }
  mostrarMedida(comprimentoDe(pontos), el.rotaTipo.value);
}

/* Comprimento pelo Leaflet: mesma haversine e mesmo raio médio WGS84
   que o servidor usa. É PREVIEW — quem grava o número é o servidor. */
function comprimentoDe(pontos) {
  let total = 0;
  for (let i = 1; i < pontos.length; i += 1) {
    total += mapa.distance(L.latLng(pontos[i - 1]), L.latLng(pontos[i]));
  }
  return total;
}

function familiaDoTipo(tipo) {
  if (tipo === 'fibra_mm' || tipo === 'fibra_sm') return 'fibra';
  if (tipo === 'utp_cat5e' || tipo === 'utp_cat6' || tipo === 'utp_cat6a') return 'utp';
  if (tipo === 'radio') return 'radio';
  if (tipo === 'eletrico') return 'eletrico';
  if (tipo === 'coaxial') return 'coaxial';
  return 'duto';
}

function ehCobre(tipo) { return familiaDoTipo(tipo) === 'utp'; }

/* A leitura que faz esta tela valer: metros reais, atualizados a cada
   movimento do mouse, com o limite da norma no lugar onde a pessoa já
   está olhando. Sem isso, "cabe em UTP?" só é respondido depois de
   gravar e conferir a planilha. */
function mostrarMedida(m, tipo) {
  el.medida.hidden = false;
  texto(el.medidaM, metros(m));
  texto(el.medidaPontos, String(rascunho ? rascunho.pontos.length : 0));
  let estouro = '';
  if (ehCobre(tipo)) {
    if (m > LIMITE_UTP) estouro = '1';
    else if (m > FOLGA_UTP) estouro = 'limite';
  }
  el.medida.dataset.estouro = estouro;
  texto(el.medidaNota, estouro === '1'
    ? `passou do limite de ${LIMITE_UTP} m em par trançado`
    : (estouro === 'limite'
        ? `sem folga para os cordões (limite ${LIMITE_UTP} m)`
        : ''));
}

function esconderMedida() {
  el.medida.hidden = true;
  el.medida.dataset.estouro = '';
}

function cancelarRota() {
  rascunho = null;
  camadas.rascunho.clearLayers();
  esconderMedida();
  trocarModo('selecao');
}

function fecharRota() {
  if (!rascunho || rascunho.pontos.length < 2) {
    avisar('Uma rota precisa de pelo menos dois pontos.', 'ruim');
    return;
  }
  abrirModal('modalRota');
  el.rotaId.value = '';
  el.rotaNome.value = '';
  el.rotaFibras.value = '';
  el.rotaObs.value = '';
  encherSelectDeRacks(el.rotaRackA, null, 'Não declarado');
  encherSelectDeRacks(el.rotaRackB, null, 'Não declarado');
  texto(el.rotaMedida, metros(comprimentoDe(rascunho.pontos)));
  texto(el.rotaPontos, String(rascunho.pontos.length));
  el.rotaNome.focus();
}

async function gravarRota() {
  const nome = el.rotaNome.value.trim();
  if (!nome) { avisar('Informe o nome da rota.', 'ruim'); return; }
  const id = el.rotaId.value;
  // Rota existente só muda de traçado pelo editor de vértices; rascunho
  // abandonado na tela não pode sobrescrever o traçado dela.
  const pontos = id
    ? (rotaEmEdicao === Number(id)
        ? (tracados.get(Number(id)) || {}).pontosEditados
        : null)
    : (rascunho ? rascunho.pontos : null);
  try {
    const corpo = {
      acao: 'salvar',
      nome,
      tipo: el.rotaTipo.value,
      instalacao: el.rotaInstalacao.value,
      fibras: el.rotaFibras.value || 0,
      observacoes: el.rotaObs.value,
      rack_a: el.rotaRackA.value || null,
      rack_b: el.rotaRackB.value || null,
      localidade: el.localidade.value || null,
      area: el.area.value || '',
    };
    if (id) corpo.rota = Number(id);
    if (pontos) corpo.pontos = pontos;
    const r = await enviar(ROTA_URL, corpo);
    if (!r.ok) throw new Error(r.erro || 'Falha ao gravar a rota.');
    fecharModal('modalRota');
    cancelarRota();
    rotaEmEdicao = null;
    const m = r.rota.comprimento_m;
    avisar(r.criada
      ? `Rota "${r.rota.nome}" criada — ${metros(m)}.`
      : `Rota "${r.rota.nome}" atualizada — ${metros(m)}.`, 'ok');
    if (r.rota.cobre && m > LIMITE_UTP) {
      avisar(`Atenção: ${metros(m)} em ${r.rota.tipo_label} passa do limite de `
        + `${LIMITE_UTP} m. A conferência marcou isso como erro.`, 'ruim');
    }
    await carregar(false);
    selecionarRota(r.rota.id);
  } catch (erro) { avisar(erro.message, 'ruim'); }
}

/* ── Editar o traçado: vértices arrastáveis ──────────────────────── */
function editarTracado(rotaId) {
  const r = planta.rotas.find((x) => x.id === rotaId);
  if (!r) return;
  rotaEmEdicao = rotaId;
  camadas.vertices.clearLayers();
  const guarda = tracados.get(rotaId) || {};
  guarda.pontosEditados = (r.pontos || []).map((p) => [p[0], p[1]]);
  tracados.set(rotaId, guarda);

  // Alça = `L.marker` com `divIcon`, e não `L.circleMarker`: o
  // `circleMarker` simplesmente NÃO tem arrasto (a opção `draggable` é do
  // `Marker`), e fazê-lo arrastar exigiria mexer no `_path` e no
  // `_newPos` do Leaflet — campos privados que mudam de versão sem aviso.
  guarda.pontosEditados.forEach((ponto, i) => {
    const alca = L.marker(ponto, {
      draggable: true,
      icon: L.divIcon({ className: 'nx-geo-alca', iconSize: [13, 13] }),
      zIndexOffset: 900,
      title: `Ponto ${i + 1} — arraste para corrigir, botão direito remove`,
    });
    alca.on('drag', () => {
      const ll = alca.getLatLng();
      guarda.pontosEditados[i] = [ll.lat, ll.lng];
      if (guarda.linha) guarda.linha.setLatLngs(guarda.pontosEditados);
      if (guarda.alvo) guarda.alvo.setLatLngs(guarda.pontosEditados);
      texto(el.rotaInfoM, metros(comprimentoDe(guarda.pontosEditados)));
    });
    alca.on('dragend', () => { el.btnRotaGravarTracado.disabled = false; });
    alca.on('contextmenu', (ev) => {
      L.DomEvent.stopPropagation(ev);
      L.DomEvent.preventDefault(ev);
      if (guarda.pontosEditados.length <= 2) {
        avisar('Uma rota precisa de pelo menos dois pontos.', 'ruim');
        return;
      }
      guarda.pontosEditados.splice(i, 1);
      if (guarda.linha) guarda.linha.setLatLngs(guarda.pontosEditados);
      if (guarda.alvo) guarda.alvo.setLatLngs(guarda.pontosEditados);
      // Redesenha as alças para os índices voltarem a casar com a lista:
      // sem isso, remover o ponto 2 faria a alça 3 gravar na posição 2.
      const pendentes = guarda.pontosEditados;
      r.pontos = pendentes;
      editarTracado(rotaId);
      el.btnRotaGravarTracado.disabled = false;
    });
    camadas.vertices.addLayer(alca);
  });

  el.btnRotaGravarTracado.hidden = false;
  el.btnRotaGravarTracado.disabled = true;
  avisar('Arraste os pontos para corrigir o traçado. Botão direito num ponto o remove. ✓ grava.');
}

async function gravarTracado() {
  if (!rotaEmEdicao) return;
  const guarda = tracados.get(rotaEmEdicao);
  if (!guarda || !guarda.pontosEditados) return;
  try {
    const r = await enviar(ROTA_URL, {
      acao: 'salvar', rota: rotaEmEdicao, pontos: guarda.pontosEditados,
    });
    if (!r.ok) throw new Error(r.erro || 'Falha');
    avisar(`Traçado atualizado — ${metros(r.rota.comprimento_m)}.`, 'ok');
    const id = rotaEmEdicao;
    rotaEmEdicao = null;
    camadas.vertices.clearLayers();
    el.btnRotaGravarTracado.hidden = true;
    await carregar(false);
    selecionarRota(id);
  } catch (erro) { avisar(erro.message, 'ruim'); }
}

/* =========================================================
   Cliques no mapa
   ========================================================= */
function clicarNoMapa(ev) {
  if (modo === 'rota') { pontoDoRascunho(ev.latlng); return; }
  if (modo === 'rack') {
    abrirModal('modalRack');
    el.rackId.value = '';
    el.rackNome.value = '';
    el.rackCodigo.value = '';
    el.rackAmbiente.value = '';
    el.rackUs.value = '42';
    el.rackObs.value = '';
    el.rackLat.value = ev.latlng.lat.toFixed(7);
    el.rackLon.value = ev.latlng.lng.toFixed(7);
    el.rackNome.focus();
    trocarModo('selecao');
    return;
  }
  if (modo === 'pino' && itemParaPosicionar) {
    posicionarItem(itemParaPosicionar, ev.latlng);
    return;
  }
  limparSelecao();
}

function moverNoMapa(ev) {
  if (modo === 'rota' && rascunho && rascunho.pontos.length) {
    redesenharRascunho([ev.latlng.lat, ev.latlng.lng]);
  }
}

async function posicionarItem(itemId, ll) {
  try {
    const r = await enviar(POSICAO_URL, {
      item: itemId, latitude: ll.lat, longitude: ll.lng,
    });
    if (!r.ok) throw new Error(r.erro || 'Falha');
    avisar('Equipamento posicionado na planta.', 'ok');
    itemParaPosicionar = null;
    trocarModo('selecao');
    carregar(false);
  } catch (erro) { avisar(erro.message, 'ruim'); }
}

function trocarModo(novo) {
  modo = novo;
  el.geo.dataset.modo = novo;
  el.btnRota.setAttribute('aria-pressed', novo === 'rota' ? 'true' : 'false');
  el.btnRack.setAttribute('aria-pressed', novo === 'rack' ? 'true' : 'false');
  texto(el.lerModo, {
    rota: 'Desenhando rota · Enter fecha · Backspace desfaz · Esc cancela',
    rack: 'Clique onde o rack está',
    pino: 'Clique onde o equipamento está',
  }[novo] || '');
  if (novo !== 'rota') esconderMedida();
}

/* =========================================================
   Seleção e inspetor
   ========================================================= */
function limparSelecao() {
  selecionado = null;
  rotaEmEdicao = null;
  camadas.vertices.clearLayers();
  el.btnRotaGravarTracado.hidden = true;
  el.inspVazio.hidden = false;
  el.inspRota.hidden = true;
  el.inspRack.hidden = true;
  el.inspEquip.hidden = true;
  document.querySelectorAll('.nx-rota-item.is-sel')
    .forEach((n) => n.classList.remove('is-sel'));
  montarStatus();
}

function selecionarRota(id) {
  const r = planta.rotas.find((x) => x.id === id);
  if (!r) return;
  limparSelecao();
  selecionado = { tipo: 'rota', id };
  el.inspVazio.hidden = true;
  el.inspRota.hidden = false;

  texto(el.rotaInfoNome, r.nome);
  texto(el.rotaInfoSub, `${r.tipo_label} · ${r.instalacao_label}`);
  texto(el.rotaInfoM, metros(r.comprimento_m));
  texto(el.rotaInfoPontos, String((r.pontos || []).length));
  texto(el.rotaInfoFibras, r.fibras ? String(r.fibras) : '—');
  texto(el.rotaInfoEnlaces, String(r.enlaces));
  texto(el.rotaInfoLocal, r.localidade || '—');
  texto(el.rotaInfoObs, r.observacoes || '—');

  const estourou = r.cobre && r.comprimento_m > LIMITE_UTP;
  el.rotaInfoSelo.hidden = !estourou && !(r.cobre && r.comprimento_m > FOLGA_UTP);
  if (!el.rotaInfoSelo.hidden) {
    el.rotaInfoSelo.dataset.tom = estourou ? 'ruim' : 'atencao';
    texto(el.rotaInfoSelo, estourou
      ? `Passa do limite de ${LIMITE_UTP} m`
      : `No limite · sem folga para os cordões`);
  }
  if (r.fibras && r.enlaces > r.fibras) {
    el.rotaInfoSelo.hidden = false;
    el.rotaInfoSelo.dataset.tom = 'ruim';
    texto(el.rotaInfoSelo, `${r.enlaces} enlaces num cabo de ${r.fibras}`);
  }

  document.querySelectorAll('.nx-rota-item').forEach((n) => {
    n.classList.toggle('is-sel', Number(n.dataset.rota) === id);
  });

  const guarda = tracados.get(id);
  if (guarda && guarda.linha) mapa.fitBounds(guarda.linha.getBounds().pad(0.3));
}

/* Enche um `<select>` com os racks do escopo. Usado em três lugares (as
   duas pontas da rota e o rack do equipamento), e por isso uma função:
   três cópias divergiriam no dia em que o rótulo mudar. */
function encherSelectDeRacks(select, valor, rotuloVazio) {
  select.replaceChildren();
  const vazio = criar('option', '', rotuloVazio);
  vazio.value = '';
  select.appendChild(vazio);
  planta.racks.forEach((r) => {
    const o = criar('option', '', `${r.nome}${r.codigo ? ` (${r.codigo})` : ''}`);
    o.value = String(r.id);
    select.appendChild(o);
  });
  select.value = valor ? String(valor) : '';
}

function selecionarRack(id) {
  const r = planta.racks.find((x) => x.id === id);
  if (!r) return;
  limparSelecao();
  selecionado = { tipo: 'rack', id };
  el.inspVazio.hidden = true;
  el.inspRack.hidden = false;
  texto(el.rackInfoNome, r.nome + (r.codigo ? ` · ${r.codigo}` : ''));
  texto(el.rackInfoSub, [r.localidade, r.ambiente].filter(Boolean).join(' · ') || '—');
  texto(el.rackInfoUs, `${r.us_ocupados}/${r.us}`);
  texto(el.rackInfoLivres, String(Math.max(0, r.us - r.us_ocupados)));
  texto(el.rackInfoEq, String(r.equipamentos));
  texto(el.rackInfoCoord, r.lat === null ? 'sem posição' : `${r.lat}, ${r.lon}`);
  texto(el.rackInfoObs, r.observacoes || '—');
}

function selecionarEquip(itemId) {
  const e = planta.equipamentos.find((x) => x.item_id === itemId);
  if (!e) return;
  limparSelecao();
  selecionado = { tipo: 'equip', id: itemId };
  el.inspVazio.hidden = true;
  el.inspEquip.hidden = false;
  texto(el.equipNome, e.nome);
  texto(el.equipSub, `${ROTULO_CLASSE[e.classe] || e.classe} · ${e.papel_label}`);
  texto(el.equipIp, e.ip || '—');
  texto(el.equipLocal, e.localidade || '—');
  texto(el.equipRack, e.rack_nome
    ? `${e.rack_nome}${e.rack_u ? ` · U${e.rack_u}` : ' · U não declarado'}`
    : 'fora de rack');
  texto(el.equipCoord, e.lat === null
    ? 'sem posição'
    : `${e.lat.toFixed(6)}, ${e.lon.toFixed(6)}${e.geo_herdada ? ' (do rack)' : ''}`);
  texto(el.equipStatus, e.status ? (e.status.texto || e.status.slug) : '—');
  el.btnEquipRack.disabled = !e.rack_id;
  el.btnEquipRack.dataset.rack = e.rack_id ? String(e.rack_id) : '';

  encherSelectDeRacks(el.equipRackSel, e.rack_id, 'Fora de rack');
  el.equipRackU.value = e.rack_u == null ? '' : String(e.rack_u);
  el.equipRackAlt.value = String(e.rack_altura_u || 1);
  el.btnEquipSalvar.dataset.item = String(itemId);
}

async function salvarPosicaoEquip() {
  const itemId = Number(el.btnEquipSalvar.dataset.item || 0);
  if (!itemId) return;
  try {
    const r = await enviar(POSICAO_URL, {
      item: itemId,
      rack: el.equipRackSel.value || null,
      rack_u: el.equipRackU.value || null,
      rack_altura_u: el.equipRackAlt.value || 1,
    });
    if (!r.ok) throw new Error(r.erro || 'Falha');
    avisar('Posição gravada.', 'ok');
    await carregar(false);
    selecionarEquip(itemId);
  } catch (erro) { avisar(erro.message, 'ruim'); }
}

/* =========================================================
   Trilho: inventário, rotas, racks e camadas
   ========================================================= */
let timerInventario = null;

function pedirInventario() {
  clearTimeout(timerInventario);
  timerInventario = setTimeout(async () => {
    try {
      const p = paramsEscopo({ q: el.buscaInv.value.trim() });
      const dados = await pedir(`${INVENTARIO_URL}?${p.toString()}`);
      montarInventario(dados.resultados || []);
    } catch (erro) {
      avisar(`Inventário indisponível: ${erro.message}`, 'ruim');
    }
  }, 220);
}

function montarInventario(itens) {
  el.inventario.replaceChildren();
  texto(el.invConta, `${itens.length} ${itens.length === 1 ? 'item' : 'itens'}`);
  const posicionados = new Set(
    planta.equipamentos.filter((e) => e.lat !== null).map((e) => e.item_id)
  );

  itens.forEach((i) => {
    const linha = criar('button', 'nx-lib');
    linha.type = 'button';
    linha.dataset.item = String(i.item_id);
    linha.dataset.noMapa = posicionados.has(i.item_id) ? '1' : '0';
    linha.style.setProperty('--nx-classe', COR_CLASSE[i.classe] || COR_CLASSE.generico);
    linha.title = posicionados.has(i.item_id)
      ? `${i.nome}\nJá está na planta — clique para centralizar`
      : `${i.nome}\nClique e depois toque no mapa onde ele está`;

    const ic = criar('span', 'nx-no-icone');
    ic.appendChild(iconeSvg(i.classe));
    linha.appendChild(ic);

    const corpo = criar('span', 'nx-lib-corpo');
    corpo.appendChild(criar('span', 'nx-lib-nome', i.nome));
    corpo.appendChild(criar('span', 'nx-lib-meta',
      [i.localidade, `${i.marca} ${i.modelo}`.trim()].filter(Boolean).join(' · ')));
    linha.appendChild(corpo);
    linha.appendChild(criar('span', 'nx-lib-portas', i.portas ? `${i.portas}p` : '—'));

    linha.onclick = () => {
      const marca = marcadores.equip.get(i.item_id);
      if (marca) {
        mapa.setView(marca.getLatLng(), Math.max(mapa.getZoom(), 18));
        selecionarEquip(i.item_id);
        return;
      }
      itemParaPosicionar = i.item_id;
      trocarModo('pino');
      avisar(`Clique no mapa onde "${i.nome}" está instalado.`);
    };
    el.inventario.appendChild(linha);
  });
}

function montarListas() {
  // ── Rotas ──────────────────────────────────────────────────────────
  el.listaRotas.replaceChildren();
  texto(el.rotasConta, String(planta.rotas.length));
  const ordenadas = [...planta.rotas].sort((a, b) => b.comprimento_m - a.comprimento_m);
  if (!ordenadas.length) {
    const vazio = criar('p', 'nx-aviso-d',
      'Nenhuma rota desenhada. Use "Desenhar rota" na barra de ferramentas.');
    vazio.style.margin = '0';
    el.listaRotas.appendChild(vazio);
  }
  ordenadas.forEach((r) => {
    const b = criar('button', 'nx-rota-item');
    b.type = 'button';
    b.dataset.rota = String(r.id);
    b.dataset.fam = r.familia;
    b.dataset.estouro = (r.cobre && r.comprimento_m > LIMITE_UTP) ? '1' : '0';
    const corpo = criar('span');
    corpo.appendChild(criar('span', 'nx-rota-nome', r.nome));
    corpo.appendChild(criar('span', 'nx-rota-meta',
      `${r.tipo_label} · ${r.enlaces} enlace(s)`
      + (r.fibras ? ` · ${r.fibras} fibras` : '')));
    b.appendChild(corpo);
    b.appendChild(criar('span', 'nx-rota-m', metros(r.comprimento_m)));
    b.onclick = () => selecionarRota(r.id);
    el.listaRotas.appendChild(b);
  });

  // ── Racks ──────────────────────────────────────────────────────────
  el.listaRacks.replaceChildren();
  texto(el.racksConta, String(planta.racks.length));
  if (!planta.racks.length) {
    const vazio = criar('p', 'nx-aviso-d',
      'Nenhum rack cadastrado. Use "Novo rack" e clique onde ele está.');
    vazio.style.margin = '0';
    el.listaRacks.appendChild(vazio);
  }
  planta.racks.forEach((r) => {
    const b = criar('button', 'nx-rota-item');
    b.type = 'button';
    b.dataset.fam = 'duto';
    const corpo = criar('span');
    corpo.appendChild(criar('span', 'nx-rota-nome', r.nome));
    corpo.appendChild(criar('span', 'nx-rota-meta',
      [r.localidade, r.ambiente, r.lat === null ? 'sem posição' : null]
        .filter(Boolean).join(' · ')));
    b.appendChild(corpo);
    b.appendChild(criar('span', 'nx-rota-m', `${r.us_ocupados}/${r.us}U`));
    b.onclick = () => {
      const marca = marcadores.rack.get(r.id);
      if (marca) mapa.setView(marca.getLatLng(), Math.max(mapa.getZoom(), 18));
      selecionarRack(r.id);
    };
    el.listaRacks.appendChild(b);
  });

  montarCamadas();
}

/* Conjunto vazio = "tudo ligado" — mesma convenção da bancada: guardar
   as classes marcadas daria o mesmo resultado com mais estado para
   manter em sincronia. */
function alternarFiltro(conjunto, chave, ligado, universo) {
  if (!conjunto.size) universo.forEach((k) => conjunto.add(k));
  if (ligado) conjunto.add(chave); else conjunto.delete(chave);
  if (conjunto.size === universo.length) conjunto.clear();
}

function montarCamadas() {
  const contaFam = new Map();
  planta.rotas.forEach((r) => contaFam.set(r.familia, (contaFam.get(r.familia) || 0) + 1));
  el.camadasFam.replaceChildren();
  [...contaFam.entries()].sort((a, b) => b[1] - a[1]).forEach(([fam, n]) => {
    const rot = criar('label', 'nx-camada');
    rot.style.setProperty('--nx-cor', corDaFamilia(fam));
    const cb = criar('input');
    cb.type = 'checkbox';
    cb.checked = !filtros.familias.size || filtros.familias.has(fam);
    cb.addEventListener('change', () => {
      alternarFiltro(filtros.familias, fam, cb.checked, [...contaFam.keys()]);
      desenharRotas();
      montarStatus();
    });
    rot.appendChild(cb);
    rot.appendChild(criar('span', 'nx-marcador'));
    rot.appendChild(criar('span', '', ROTULO_FAMILIA[fam] || fam));
    rot.appendChild(criar('span', 'nx-conta', String(n)));
    el.camadasFam.appendChild(rot);
  });

  const contaClasse = new Map();
  planta.equipamentos.forEach((e) => {
    if (e.lat === null) return;
    contaClasse.set(e.classe, (contaClasse.get(e.classe) || 0) + 1);
  });
  el.camadasClasse.replaceChildren();
  [...contaClasse.entries()].sort((a, b) => b[1] - a[1]).forEach(([classe, n]) => {
    const rot = criar('label', 'nx-camada');
    rot.style.setProperty('--nx-cor', COR_CLASSE[classe] || COR_CLASSE.generico);
    const cb = criar('input');
    cb.type = 'checkbox';
    cb.checked = !filtros.classes.size || filtros.classes.has(classe);
    cb.addEventListener('change', () => {
      alternarFiltro(filtros.classes, classe, cb.checked, [...contaClasse.keys()]);
      desenharEquipamentos();
      montarStatus();
    });
    rot.appendChild(cb);
    rot.appendChild(criar('span', 'nx-marcador'));
    rot.appendChild(criar('span', '', ROTULO_CLASSE[classe] || classe));
    rot.appendChild(criar('span', 'nx-conta', String(n)));
    el.camadasClasse.appendChild(rot);
  });
}

/* =========================================================
   Conferência
   ========================================================= */
function montarAvisos() {
  el.avisos.replaceChildren();
  texto(el.avisosConta, String(planta.avisos.length));
  if (!planta.avisos.length) {
    el.avisos.appendChild(criar('p', 'nx-aviso-d',
      'Nenhuma inconsistência na planta do escopo atual.'));
    return;
  }
  planta.avisos.forEach((a) => {
    const b = criar('button', 'nx-aviso');
    b.type = 'button';
    b.dataset.nivel = a.nivel;
    const corpo = criar('span');
    corpo.appendChild(criar('span', 'nx-aviso-t', a.titulo));
    corpo.appendChild(criar('span', 'nx-aviso-d', a.detalhe));
    b.appendChild(corpo);
    const quantos = (a.rotas || a.racks || a.itens || []).length;
    b.appendChild(criar('span', 'nx-conta', String(quantos)));
    b.onclick = () => {
      if (a.rotas && a.rotas.length) { selecionarRota(a.rotas[0]); return; }
      if (a.racks && a.racks.length) {
        const marca = marcadores.rack.get(a.racks[0]);
        if (marca) mapa.setView(marca.getLatLng(), 18);
        selecionarRack(a.racks[0]);
        return;
      }
      avisar(a.detalhe);
    };
    el.avisos.appendChild(b);
  });
}

async function verImpactoRota() {
  if (!selecionado || selecionado.tipo !== 'rota') {
    avisar('Selecione uma rota primeiro.', 'ruim');
    return;
  }
  try {
    const p = paramsEscopo({ rota: selecionado.id });
    const dados = await pedir(`${IMPACTO_URL}?${p.toString()}`);
    if (!dados.ok) throw new Error(dados.erro || 'Falha');
    abrirModal('modalImpacto');
    texto(el.impactoTitulo, dados.nome);
    texto(el.impactoM, metros(dados.comprimento_m));
    texto(el.impactoCortados, String(dados.enlaces_cortados));
    texto(el.impactoTotal, String(dados.total));
    texto(el.impactoJa, String(dados.ja_isolados || 0));
    el.impactoLista.replaceChildren();
    if (!dados.isolados.length) {
      el.impactoLista.appendChild(criar('div', 'nx-escolha',
        'Nada fica isolado: há outro caminho para todos os equipamentos do escopo.'));
    }
    dados.isolados.forEach((i) => {
      const linha = criar('div', 'nx-escolha');
      linha.appendChild(criar('span', '', i.nome));
      linha.appendChild(criar('span', 'nx-conta', ROTULO_CLASSE[i.classe] || i.classe));
      el.impactoLista.appendChild(linha);
    });
  } catch (erro) { avisar(erro.message, 'ruim'); }
}

/* =========================================================
   Rack: gravar e ver a elevação
   ========================================================= */
async function gravarRack() {
  const nome = el.rackNome.value.trim();
  if (!nome) { avisar('Informe o nome do rack.', 'ruim'); return; }
  try {
    const corpo = {
      acao: 'salvar',
      nome,
      codigo: el.rackCodigo.value,
      ambiente: el.rackAmbiente.value,
      us: el.rackUs.value || 42,
      latitude: el.rackLat.value || null,
      longitude: el.rackLon.value || null,
      localidade: el.localidade.value || null,
      area: el.area.value || '',
      observacoes: el.rackObs.value,
    };
    if (el.rackId.value) corpo.rack = Number(el.rackId.value);
    const r = await enviar(RACK_URL, corpo);
    if (!r.ok) throw new Error(r.erro || 'Falha ao gravar o rack.');
    fecharModal('modalRack');
    avisar(r.criado ? `Rack "${r.rack.nome}" criado.` : `Rack "${r.rack.nome}" atualizado.`, 'ok');
    await carregar(false);
    selecionarRack(r.rack.id);
  } catch (erro) { avisar(erro.message, 'ruim'); }
}

function editarRackSelecionado() {
  if (!selecionado || selecionado.tipo !== 'rack') return;
  const r = planta.racks.find((x) => x.id === selecionado.id);
  if (!r) return;
  abrirModal('modalRack');
  el.rackId.value = String(r.id);
  el.rackNome.value = r.nome;
  el.rackCodigo.value = r.codigo || '';
  el.rackAmbiente.value = r.ambiente || '';
  el.rackUs.value = String(r.us);
  el.rackLat.value = r.lat === null ? '' : String(r.lat);
  el.rackLon.value = r.lon === null ? '' : String(r.lon);
  el.rackObs.value = r.observacoes || '';
  el.rackNome.focus();
}

async function excluirRackSelecionado() {
  if (!selecionado || selecionado.tipo !== 'rack') return;
  const r = planta.racks.find((x) => x.id === selecionado.id);
  if (!r) return;
  if (!confirm(`Excluir o rack "${r.nome}"? Os equipamentos continuam no `
    + `inventário, apenas sem rack.`)) return;
  try {
    const resp = await enviar(RACK_URL, { acao: 'excluir', rack: r.id });
    if (!resp.ok) throw new Error(resp.erro || 'Falha');
    avisar(resp.mensagem || 'Rack excluído.', 'ok');
    limparSelecao();
    carregar(false);
  } catch (erro) { avisar(erro.message, 'ruim'); }
}

async function excluirRotaSelecionada() {
  if (!selecionado || selecionado.tipo !== 'rota') return;
  const r = planta.rotas.find((x) => x.id === selecionado.id);
  if (!r) return;
  if (!confirm(`Excluir a rota "${r.nome}"? Os ${r.enlaces} enlace(s) continuam `
    + `cadastrados — eles só perdem o traçado físico.`)) return;
  try {
    const resp = await enviar(ROTA_URL, { acao: 'excluir', rota: r.id });
    if (!resp.ok) throw new Error(resp.erro || 'Falha');
    avisar(resp.mensagem || 'Rota excluída.', 'ok');
    limparSelecao();
    carregar(false);
  } catch (erro) { avisar(erro.message, 'ruim'); }
}

function editarRotaSelecionada() {
  if (!selecionado || selecionado.tipo !== 'rota') return;
  const r = planta.rotas.find((x) => x.id === selecionado.id);
  if (!r) return;
  abrirModal('modalRota');
  el.rotaId.value = String(r.id);
  el.rotaNome.value = r.nome;
  el.rotaTipo.value = r.tipo;
  el.rotaInstalacao.value = r.instalacao;
  el.rotaFibras.value = r.fibras ? String(r.fibras) : '';
  el.rotaObs.value = r.observacoes || '';
  encherSelectDeRacks(el.rotaRackA, r.rack_a, 'Não declarado');
  encherSelectDeRacks(el.rotaRackB, r.rack_b, 'Não declarado');
  texto(el.rotaMedida, metros(r.comprimento_m));
  texto(el.rotaPontos, String((r.pontos || []).length));
  el.rotaNome.focus();
}

async function abrirElevacao(rackId) {
  try {
    const dados = await pedir(`${ELEVACAO_URL}?rack=${encodeURIComponent(rackId)}`);
    if (!dados.ok) throw new Error(dados.erro || 'Falha');
    const r = dados.rack;
    abrirModal('modalElevacao');
    texto(el.elevTitulo, r.nome + (r.codigo ? ` · ${r.codigo}` : ''));
    texto(el.elevSub, [r.localidade, r.ambiente].filter(Boolean).join(' · ') || '—');
    texto(el.elevUs, String(r.us));
    texto(el.elevOcup, String(r.us_ocupados));
    texto(el.elevLivres, String(r.us_livres));

    const num = criar('div', 'nx-rack-num');
    const trilha = criar('div', 'nx-rack-trilha');
    trilha.style.gridTemplateRows = `repeat(${r.us}, 17px)`;
    dados.regua.forEach((faixa) => {
      const marca = criar('i', '', `${faixa.u}`);
      if (faixa.u % 5 === 0) marca.dataset.marco = '1';
      num.appendChild(marca);
      const ocup = faixa.ocupante;
      if (!ocup) { trilha.appendChild(criar('div', 'nx-rack-vago')); return; }
      if (!ocup.primeiro) return;
      const bloco = criar('button', 'nx-rack-eq');
      bloco.type = 'button';
      // A régua desce e o U sobe: conversão que faz a tela bater com a
      // etiqueta do fabricante.
      const linhaTopo = r.us - (ocup.base_u + ocup.altura_u - 1) + 1;
      bloco.style.gridRow = `${linhaTopo} / span ${ocup.altura_u}`;
      bloco.style.setProperty('--nx-classe', COR_CLASSE[ocup.classe] || COR_CLASSE.generico);
      bloco.appendChild(iconeSvg(ocup.classe));
      bloco.appendChild(criar('b', '', ocup.nome));
      bloco.appendChild(criar('span', '',
        `U${ocup.base_u}${ocup.altura_u > 1 ? `–${ocup.base_u + ocup.altura_u - 1}` : ''}`
        + (ocup.ip ? ` · ${ocup.ip}` : '')));
      bloco.onclick = () => {
        fecharModal('modalElevacao');
        selecionarEquip(ocup.item_id);
      };
      trilha.appendChild(bloco);
    });
    el.elevRegua.replaceChildren(num, trilha);

    el.elevSemPos.replaceChildren();
    el.elevSemPosBloco.hidden = !dados.sem_posicao.length;
    dados.sem_posicao.forEach((s) => {
      const linha = criar('div', 'nx-escolha');
      linha.appendChild(criar('span', '', s.nome));
      linha.appendChild(criar('span', 'nx-conta', `${s.altura_u}U`));
      el.elevSemPos.appendChild(linha);
    });
  } catch (erro) { avisar(erro.message, 'ruim'); }
}

/* =========================================================
   Barra de leitura
   ========================================================= */
function montarStatus() {
  const k = planta.kpis || {};
  texto(el.lerRacks, String(k.racks || 0));
  texto(el.lerEquip, `${k.posicionados || 0}/${k.equipamentos || 0}`);
  texto(el.lerRotas, String(k.rotas || 0));
  texto(el.lerMetros, metros(k.metros || 0));
  texto(el.lerFibra, metros(k.metros_fibra || 0));
  texto(el.lerUtp, metros(k.metros_utp || 0));
  texto(el.lerZoom, mapa ? `z${mapa.getZoom()}` : '—');
  texto(el.lerSel, selecionado
    ? `${selecionado.tipo === 'rota' ? 'rota' : (selecionado.tipo === 'rack' ? 'rack' : 'equipamento')} selecionado`
    : '');
}

/* =========================================================
   Modais, teclado e ligações
   ========================================================= */
function abrirModal(id) { $(id).hidden = false; }
function fecharModal(id) { $(id).hidden = true; }

function ligarMenus() {
  document.querySelectorAll('[data-menu]').forEach((botao) => {
    const pop = $(botao.dataset.menu);
    botao.addEventListener('click', (ev) => {
      ev.stopPropagation();
      const abrindo = pop.hidden;
      document.querySelectorAll('.nx-menu-pop').forEach((p) => { p.hidden = true; });
      pop.hidden = !abrindo;
      botao.setAttribute('aria-expanded', abrindo ? 'true' : 'false');
    });
  });
  document.addEventListener('click', () => {
    document.querySelectorAll('.nx-menu-pop').forEach((p) => { p.hidden = true; });
    document.querySelectorAll('[data-menu]').forEach((b) => b.setAttribute('aria-expanded', 'false'));
  });
  document.querySelectorAll('.nx-menu-pop').forEach((p) => {
    p.addEventListener('click', (ev) => ev.stopPropagation());
  });
}

function ligarTeclado() {
  document.addEventListener('keydown', (ev) => {
    const emCampo = /^(INPUT|TEXTAREA|SELECT)$/.test(ev.target.tagName);
    if (ev.key === 'Escape') {
      const abertos = document.querySelectorAll('.nx-modal:not([hidden])');
      if (abertos.length) { abertos.forEach((m) => { m.hidden = true; }); return; }
      if (modo !== 'selecao') cancelarRota();
      return;
    }
    if (emCampo) return;

    if (ev.key === 'Enter' && modo === 'rota') { ev.preventDefault(); fecharRota(); return; }
    if (ev.key === 'Backspace' && modo === 'rota') { ev.preventDefault(); desfazerPonto(); return; }
    if (ev.key === 'r' || ev.key === 'R') { comecarRota(); return; }
    if (ev.key === 'k' || ev.key === 'K') { trocarModo(modo === 'rack' ? 'selecao' : 'rack'); return; }
    if (ev.key === 'f' || ev.key === 'F') { enquadrar(); return; }
  });
}

function cachearElementos() {
  [
    'app', 'geo', 'mapa', 'toast',
    'localidade', 'area', 'buscaInv', 'inventario', 'invConta',
    'listaRotas', 'rotasConta', 'listaRacks', 'racksConta',
    'camadasFam', 'camadasClasse',
    'medida', 'medidaM', 'medidaPontos', 'medidaNota',
    'btnRota', 'btnRack', 'btnRotaGravarTracado',
    'inspVazio', 'inspRota', 'inspRack', 'inspEquip',
    'rotaInfoNome', 'rotaInfoSub', 'rotaInfoM', 'rotaInfoPontos', 'rotaInfoFibras',
    'rotaInfoEnlaces', 'rotaInfoLocal', 'rotaInfoObs', 'rotaInfoSelo',
    'rackInfoNome', 'rackInfoSub', 'rackInfoUs', 'rackInfoLivres', 'rackInfoEq',
    'rackInfoCoord', 'rackInfoObs',
    'equipNome', 'equipSub', 'equipIp', 'equipLocal', 'equipRack', 'equipCoord',
    'equipStatus', 'btnEquipRack', 'equipRackSel', 'equipRackU', 'equipRackAlt',
    'btnEquipSalvar',
    'avisos', 'avisosConta',
    'rotaId', 'rotaNome', 'rotaTipo', 'rotaInstalacao', 'rotaFibras', 'rotaObs',
    'rotaMedida', 'rotaPontos', 'rotaRackA', 'rotaRackB',
    'rackId', 'rackNome', 'rackCodigo', 'rackAmbiente', 'rackUs', 'rackLat',
    'rackLon', 'rackObs',
    'elevTitulo', 'elevSub', 'elevUs', 'elevOcup', 'elevLivres', 'elevRegua',
    'elevSemPos', 'elevSemPosBloco',
    'impactoTitulo', 'impactoM', 'impactoCortados', 'impactoTotal', 'impactoJa',
    'impactoLista',
    'lerRacks', 'lerEquip', 'lerRotas', 'lerMetros', 'lerFibra', 'lerUtp',
    'lerZoom', 'lerSel', 'lerModo',
  ].forEach((id) => { el[id] = $(id); });
}

function ligarControles() {
  [el.localidade, el.area].forEach((c) => {
    if (c) c.addEventListener('change', () => { carregar(true); pedirInventario(); });
  });
  el.buscaInv.addEventListener('input', pedirInventario);

  document.querySelectorAll('[data-trilho]').forEach((b) => {
    b.addEventListener('click', () => {
      const trilho = $(b.dataset.trilho);
      const aberto = trilho.dataset.aberto !== '0';
      trilho.dataset.aberto = aberto ? '0' : '1';
      b.setAttribute('aria-expanded', aberto ? 'false' : 'true');
      // O Leaflet mede o container na montagem: sem avisá-lo da nova
      // largura, metade do mapa fica cinza até o próximo zoom.
      requestAnimationFrame(() => mapa.invalidateSize());
    });
  });

  el.btnRota.onclick = () => (modo === 'rota' ? cancelarRota() : comecarRota());
  el.btnRack.onclick = () => trocarModo(modo === 'rack' ? 'selecao' : 'rack');
  el.btnRotaGravarTracado.onclick = gravarTracado;
  $('btnEnquadrar').onclick = enquadrar;

  $('mnNovoRack').onclick = () => trocarModo('rack');
  $('mnNovaRota').onclick = comecarRota;
  $('mnExcel').onclick = () => {
    const p = paramsEscopo({});
    const q = p.toString();
    window.location.href = q ? `${EXPORTAR_URL}?${q}` : EXPORTAR_URL;
  };
  $('mnImprimir').onclick = () => window.print();
  $('mnBancada').onclick = () => {
    const p = paramsEscopo({});
    const q = p.toString();
    window.location.href = q ? `${MAPA_URL}?${q}` : MAPA_URL;
  };

  $('btnRotaEditar').onclick = editarRotaSelecionada;
  $('btnRotaTracado').onclick = () => {
    if (selecionado && selecionado.tipo === 'rota') editarTracado(selecionado.id);
  };
  $('btnRotaImpacto').onclick = verImpactoRota;
  $('btnRotaExcluir').onclick = excluirRotaSelecionada;
  $('btnRackEditar').onclick = editarRackSelecionado;
  $('btnRackElevacao').onclick = () => {
    if (selecionado && selecionado.tipo === 'rack') abrirElevacao(selecionado.id);
  };
  $('btnRackExcluir').onclick = excluirRackSelecionado;
  el.btnEquipSalvar.onclick = salvarPosicaoEquip;
  el.btnEquipRack.onclick = () => {
    if (el.btnEquipRack.dataset.rack) abrirElevacao(Number(el.btnEquipRack.dataset.rack));
  };

  $('btnRotaGravar').onclick = gravarRota;
  $('btnRackGravar').onclick = gravarRack;
  el.rotaTipo.addEventListener('change', () => {
    if (rascunho) redesenharRascunho();
  });

  document.querySelectorAll('[data-fechar]').forEach((b) => {
    b.addEventListener('click', () => fecharModal(b.dataset.fechar));
  });

  document.querySelectorAll('.nx-aba').forEach((aba) => {
    aba.addEventListener('click', () => {
      document.querySelectorAll('.nx-aba').forEach((a) => {
        const sel = a === aba;
        a.setAttribute('aria-selected', sel ? 'true' : 'false');
        $(a.dataset.painel).hidden = !sel;
      });
    });
  });

  new ResizeObserver(() => { if (mapa) mapa.invalidateSize(); }).observe(el.mapa);
}

/* O status do PRTG é cacheado 30s no servidor; 45s mantém a planta
   viva sem multiplicar a carga — mesma cadência da bancada. */
function ligarRelogio() {
  if (!PRTG_OK) return;
  setInterval(() => {
    if (document.visibilityState === 'visible' && modo === 'selecao' && !rotaEmEdicao) {
      carregar(false);
    }
  }, 45000);
}

async function init() {
  cachearElementos();
  if (!el.mapa) return;
  if (typeof L === 'undefined' || typeof KQMapa === 'undefined') {
    avisar('A biblioteca do mapa não carregou. Confira a conexão e recarregue a página.', 'ruim');
    return;
  }
  // Carrega os dados ANTES de montar o mapa: o centro inicial sai da
  // média do que está posicionado, e abrir no centro errado para depois
  // saltar é um piscar que faz a tela parecer instável.
  await carregar(false);
  iniciarMapa();
  desenharTudo();
  enquadrar();
  ligarControles();
  ligarMenus();
  ligarTeclado();
  limparSelecao();
  pedirInventario();
  montarStatus();
  ligarRelogio();
}

init();
