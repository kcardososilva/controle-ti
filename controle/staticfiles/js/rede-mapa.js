/* =============================================================
   rede-mapa.js — a bancada de topologia do módulo de Plantas.

   ── Arquitetura, e por que ela não é um Canvas ──────────────────
   O editor de plantas (`planta-editor.js`) desenha tudo num Canvas
   2D. Aqui a escolha é outra, de propósito: **SVG para os fios e
   HTML para os nós**, as duas camadas dentro de um único elemento
   `.nx-mundo` que recebe UMA transformação de pan/zoom.

   O motivo é o conteúdo. Um nó deste mapa não é um ícone: é um
   cartão com nome que precisa de reticências, IP monoespaçado,
   régua de ocupação e ponto de situação — tudo que o CSS já resolve
   e que, no Canvas, viraria medição de texto à mão. E o painel de
   portas tem de ser clicável porta por porta: 48 alvos de 24px num
   Canvas significam `hitTest` próprio para cada um.

   Pan e zoom aplicados ao MUNDO, e não a cada nó, é o que mantém
   fio e caixa em registro: com transformação por elemento, um
   arredondamento diferente entre SVG e HTML descola a ponta do cabo
   da borda do switch, e o erro só aparece em zoom alto.

   ── O que este arquivo NÃO decide ───────────────────────────────
   Nada de regra de rede. O que é switch, em que camada ele entra,
   quantas portas tem um Catalyst, quando um aviso técnico dispara e
   onde cada nó fica no arranjo automático: tudo vem do servidor
   (`services/rede_topologia_service.py`). Aqui se desenha, arrasta e
   edita — e todo enlace criado volta pela API de porta, porque o
   cabo é fato do inventário, não do desenho.

   Dado do servidor entra por `json_script` e é lido UMA vez, abaixo:
   um `.js` em `static/` é servido cru, sem passar pelo motor de
   template, e uma tag de URL aqui viraria texto literal — quebrando
   em silêncio (CLAUDE.md, "Telas grandes"). Há teste varrendo isso.
   ============================================================= */
'use strict';

const CFG = JSON.parse(document.getElementById('__rede_config').textContent);

const GRAFO_URL       = CFG.grafo_url;
const SALVAR_URL      = CFG.salvar_url;
const DISPOSITIVO_URL = CFG.dispositivo_url;
const PORTAS_URL      = CFG.portas_url;
const PORTA_URL       = CFG.porta_url;
const ENLACE_URL      = CFG.enlace_url;
const IMPACTO_URL     = CFG.impacto_url;
const HISTORICO_URL   = CFG.historico_url;
const INVENTARIO_URL  = CFG.inventario_url;
const CRIAR_URL       = CFG.criar_url;
const PRTG_BUSCA_URL  = CFG.prtg_busca_url;
const EXPORTAR_URL    = CFG.exportar_url;
const PLANTA_URL      = CFG.planta_url;
const ROTA_URL        = CFG.rota_url;
const ELEVACAO_URL    = CFG.elevacao_url;
const CSRF_TOKEN      = CFG.csrf_token;
const PRTG_OK         = CFG.prtg_ok;
const CATALOGO        = CFG.catalogo || [];
const MAPA_NOME       = CFG.mapa_nome;

const MAPA_ID = CFG.mapa_id;

/* ── Geometria do nó ───────────────────────────────────────────────
   A largura é fixada no CSS (`.nx-no { width: 190px }`) e repetida
   aqui porque o roteamento do fio precisa dela antes de o nó existir
   no DOM. A altura é MEDIDA depois do primeiro desenho: ela depende
   da fonte do sistema, e um valor chutado descola a ponta do cabo da
   borda da caixa. */
const NO_W = 190;
let NO_H = 52;

const PASSO = 10;          // malha: o arrasto encaixa de 10 em 10
const ZOOM_MIN = 0.18;
const ZOOM_MAX = 2.6;

/* ── Estado ─────────────────────────────────────────────────────── */
let grafo   = { nos: [], enlaces: [], kpis: {}, avisos: [], prtg_ativo: false };
let porId   = new Map();                 // id do nó → nó
let layout  = normalizarLayout(CFG.layout);
let versao  = CFG.mapa_versao || 0;
let arranjo = null;                      // geometria calculada pelo servidor

let vista = { x: 0, y: 0, z: 1 };
let selecao = new Set();
let enlaceSel = null;
let noFoco = null;

let modo = 'selecao';                    // 'selecao' | 'cabo'
let caboOrigem = null;                   // { portaId, itemId, rotulo }

let dispAtual = null;                    // painel de portas aberto
let portaSel = null;

const filtros = { classes: new Set(), situacoes: new Set(), busca: '' };
let classesConhecidas = [];

let sujo = false;
let salvando = false;
let timerSalvar = null;

const el = {};                           // cache de elementos do DOM

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

/* Todo texto que vem do banco entra por `textContent`/`setAttribute`,
   nunca por `innerHTML` — nome de equipamento é digitado por gente e
   um `<img onerror>` num rótulo seria XSS na tela de quem abrir o
   mapa. É a mesma razão de o layout ir por `json_script`. */
function texto(no, valor) { no.textContent = valor == null ? '' : String(valor); }

function normalizarLayout(bruto) {
  const b = bruto || {};
  return {
    nos: b.nos && typeof b.nos === 'object' ? { ...b.nos } : {},
    zonas: Array.isArray(b.zonas) ? b.zonas.slice() : [],
    notas: Array.isArray(b.notas) ? b.notas.slice() : [],
    canvas: b.canvas && b.canvas.width ? { ...b.canvas } : { width: 1400, height: 900 },
    agrupar: b.agrupar || 'papel',
  };
}

async function pedir(url, opcoes) {
  const resp = await fetch(url, {
    credentials: 'same-origin',
    headers: { 'X-Requested-With': 'XMLHttpRequest' },
    ...(opcoes || {}),
  });
  if (!resp.ok && resp.status !== 400) {
    throw new Error(`HTTP ${resp.status}`);
  }
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

/* =========================================================
   Ícones — um traço por classe de dispositivo
   ========================================================= */
/* Desenho em `viewBox="0 0 24 24"`, só contorno: o menu lateral do
   sistema é todo outline (regra 32) e glifo preenchido ao lado de
   traço de 1,6 lê como dois conjuntos na mesma tela. */
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

/* Cor da classe — escala de domínio, servida pelos tokens `--chart-*`
   do sistema (regra 33 isenta cor de domínio da proibição de literal,
   e usar os tokens mantém a mesma categoria com a mesma cor em todo
   lugar). */
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

const ROTULO_SITUACAO = {
  up: 'Online', down: 'Offline', warning: 'Instável', unusual: 'Instável',
  unknown: 'Desconhecido', collecting: 'Coletando',
  nao_monitorado: 'Sem monitoramento',
};

const COR_SITUACAO = {
  up: 'var(--nx-st-up)', down: 'var(--nx-st-down)', warning: 'var(--nx-st-warn)',
  unusual: 'var(--nx-st-warn)', nao_monitorado: 'var(--nx-st-none)',
};

const ROTULO_MEIO = {
  utp: 'Cabo UTP', fibra: 'Fibra óptica', radio: 'Enlace de rádio',
  stack: 'Empilhamento', logico: 'Lógico',
};

/* =========================================================
   Carga do grafo
   ========================================================= */
function paramsEscopo(extra) {
  const p = new URLSearchParams();
  if (el.localidade && el.localidade.value) p.set('localidade', el.localidade.value);
  if (el.area && el.area.value) p.set('area', el.area.value);
  if (el.terminais && !el.terminais.checked) p.set('terminais', '0');
  Object.entries(extra || {}).forEach(([k, v]) => p.set(k, v));
  return p;
}

async function carregar(comArranjo) {
  el.prancha.setAttribute('aria-busy', 'true');
  try {
    const p = paramsEscopo({
      arranjo: comArranjo ? '1' : '0',
      agrupar: layout.agrupar,
      status: PRTG_OK ? '1' : '0',
    });
    const dados = await pedir(`${GRAFO_URL}?${p.toString()}`);
    if (!dados.ok) throw new Error(dados.erro || 'Falha ao montar o grafo.');

    grafo = dados;
    porId = new Map(grafo.nos.map((n) => [n.id, n]));
    if (dados.arranjo) arranjo = dados.arranjo;

    // Nó novo entra no mapa na posição que o servidor calculou: sem
    // isso ele nasceria em 0,0, empilhado com todos os outros novos.
    if (!arranjo) {
      const semPosicao = grafo.nos.some((n) => !layout.nos[n.id]);
      if (semPosicao) return carregar(true);
    }

    montarClasses();
    montarCamadas();
    desenhar();
    montarAvisos();
    montarStatus();
  } catch (erro) {
    avisar(`Não foi possível carregar o mapa: ${erro.message}`, 'ruim');
  } finally {
    el.prancha.removeAttribute('aria-busy');
  }
}

function posicaoDe(no) {
  const salva = layout.nos[no.id];
  if (salva && typeof salva.x === 'number') return salva;
  const auto = arranjo && arranjo.nos ? arranjo.nos[no.id] : null;
  return auto || { x: 60, y: 60 };
}

function visivel(no) {
  if (filtros.classes.size && !filtros.classes.has(no.classe)) return false;
  if (filtros.situacoes.size && !filtros.situacoes.has(no.status.slug)) return false;
  if (filtros.busca) {
    const alvo = `${no.nome} ${no.ip} ${no.localidade} ${no.marca} ${no.modelo} ${no.serie}`.toLowerCase();
    if (!alvo.includes(filtros.busca)) return false;
  }
  const g = layout.nos[no.id];
  if (g && g.oculto) return false;
  return true;
}

/* =========================================================
   Desenho
   ========================================================= */
function desenhar() {
  desenharZonas();
  desenharNos();
  medirAltura();
  desenharFios();
  destacarCaboDaPorta();
  aplicarVista();
  desenharMini();
  el.vazio.hidden = grafo.nos.length > 0;
}

function desenharZonas() {
  el.zonas.replaceChildren();
  const zonas = (layout.zonas && layout.zonas.length)
    ? layout.zonas
    : (arranjo ? arranjo.zonas : []);
  (zonas || []).forEach((z) => {
    const box = criar('div', 'nx-zona');
    box.style.left = `${z.x}px`;
    box.style.top = `${z.y}px`;
    box.style.width = `${z.w}px`;
    box.style.height = `${z.h}px`;
    const rot = criar('span', 'nx-zona-rot', z.nome || '');
    box.appendChild(rot);
    el.zonas.appendChild(box);
  });
}

function desenharNos() {
  el.nos.replaceChildren(el.zonas);
  const frag = document.createDocumentFragment();

  grafo.nos.forEach((no) => {
    const pos = posicaoDe(no);
    const caixa = criar('button', 'nx-no');
    caixa.type = 'button';
    caixa.dataset.no = no.id;
    caixa.dataset.st = no.status.slug;
    caixa.dataset.mapeado = no.mapeado ? '1' : '0';
    caixa.dataset.lotado = no.ocupacao >= 90 ? '1' : '0';
    caixa.style.left = `${pos.x}px`;
    caixa.style.top = `${pos.y}px`;
    caixa.style.setProperty('--nx-classe', COR_CLASSE[no.classe] || COR_CLASSE.generico);
    caixa.style.setProperty('--nx-ocup', String(no.ocupacao || 0));
    caixa.title = `${no.nome}\n${ROTULO_CLASSE[no.classe] || no.classe} · ${no.papel_label}`
      + `\n${no.localidade || 'sem localidade'}`
      + (no.portas_total ? `\n${no.portas_ativas}/${no.portas_total} portas em uso` : '')
      + `\n${ROTULO_SITUACAO[no.status.slug] || no.status.slug}`;
    if (selecao.has(no.id)) caixa.classList.add('is-sel');
    if (!visivel(no)) caixa.classList.add('is-apagado');

    const icone = criar('span', 'nx-no-icone');
    icone.appendChild(iconeSvg(no.classe));
    caixa.appendChild(icone);

    const corpo = criar('span');
    corpo.appendChild(criar('span', 'nx-no-nome', no.nome));
    const meta = criar('span', 'nx-no-meta');
    if (no.ip) meta.appendChild(criar('span', 'nx-no-ip', no.ip));
    meta.appendChild(criar('span', '', no.portas_total
      ? `${no.portas_ativas}/${no.portas_total}p`
      : (ROTULO_CLASSE[no.classe] || '')));
    corpo.appendChild(meta);
    caixa.appendChild(corpo);

    if (no.portas_total) {
      const reg = criar('span', 'nx-no-reg');
      reg.appendChild(criar('i'));
      caixa.appendChild(reg);
    }

    // Painel em miniatura: uma porta por bloco de 3px, direto na caixa.
    // Responde "sobra porta naquele switch?" olhando o mapa, sem clique —
    // o mapa compacto já veio junto do nó (ver `mapa_compacto` no service),
    // então isto não custa pedido nenhum.
    if (no.portas_mapa) {
      caixa.appendChild(faceMini(no.portas_mapa));
    }

    caixa.appendChild(criar('span', 'nx-no-st'));

    frag.appendChild(caixa);
  });

  (layout.notas || []).forEach((nota, i) => {
    const box = criar('div', 'nx-nota', nota.texto || '');
    box.dataset.nota = String(i);
    box.style.left = `${nota.x}px`;
    box.style.top = `${nota.y}px`;
    box.style.width = `${nota.w || 220}px`;
    frag.appendChild(box);
  });

  el.nos.appendChild(frag);
}

/* Mapa compacto (`'AALLsRD…'`) virando blocos. Maiúscula é RJ45,
   minúscula é SFP — uma letra por porta, na ordem da numeração. */
function faceMini(mapa) {
  const face = criar('span', 'nx-no-face');
  for (const ch of mapa) {
    const bloco = criar('i');
    bloco.dataset.p = ch;
    if (ch !== ch.toUpperCase()) bloco.dataset.sfp = '1';
    face.appendChild(bloco);
  }
  return face;
}

function medirAltura() {
  const primeiro = el.nos.querySelector('.nx-no');
  if (primeiro) {
    const h = primeiro.offsetHeight;
    if (h > 10) NO_H = h;
  }
}

function centroDe(no) {
  const p = posicaoDe(no);
  return { x: p.x + NO_W / 2, y: p.y + NO_H / 2 };
}

/* ── Ancoragem dos cabos na borda da caixa ─────────────────────────
   Até aqui todo cabo saía do CENTRO do nó. Num switch de
   distribuição com doze filhos, os doze cabos nascem no mesmo ponto e
   os primeiros 40px do desenho são um feixe sólido: dá para contar
   quantos chegam embaixo, não quantos saem de cima.

   A correção é tratar a face da caixa como régua. Os cabos que saem
   pela mesma face são ordenados pela posição do DESTINO e distribuídos
   ao longo dela — assim eles saem na ordem em que vão chegar, e dois
   cabos vizinhos nunca se cruzam logo na saída. O passo é limitado a
   14px para o leque não ultrapassar a largura da caixa.

   Medido UMA vez por desenho (`prepararRotas`), não por cabo: a
   ordenação é por nó, e refazê-la dentro do laço dos 200 enlaces seria
   trabalho quadrático a cada redesenho. */
let ancoragem = new Map();

function prepararRotas() {
  ancoragem = new Map();
  const porNo = new Map();

  grafo.enlaces.forEach((e) => {
    const a = porId.get(e.de);
    const b = porId.get(e.para);
    if (!a || !b) return;
    [[a, b], [b, a]].forEach(([no, outro]) => {
      const cn = centroDe(no);
      const co = centroDe(outro);
      const dx = co.x - cn.x;
      const dy = co.y - cn.y;
      const vertical = Math.abs(dy) >= Math.abs(dx);
      const face = vertical ? (dy > 0 ? 'S' : 'N') : (dx > 0 ? 'L' : 'O');
      if (!porNo.has(no.id)) porNo.set(no.id, new Map());
      const faces = porNo.get(no.id);
      if (!faces.has(face)) faces.set(face, []);
      faces.get(face).push({ id: e.id, ordem: vertical ? co.x : co.y });
    });
  });

  porNo.forEach((faces, noId) => {
    faces.forEach((lista, face) => {
      lista.sort((p, q) => p.ordem - q.ordem);
      const vertical = face === 'N' || face === 'S';
      const extensao = (vertical ? NO_W : NO_H) - 16;
      const passo = lista.length > 1
        ? Math.min(14, extensao / (lista.length - 1))
        : 0;
      const base = -passo * (lista.length - 1) / 2;
      lista.forEach((item, i) => {
        ancoragem.set(`${item.id}|${noId}`, { face, desvio: base + i * passo });
      });
    });
  });
}

/* Roteamento ortogonal em três segmentos. É o desenho de diagrama de
   rede: o cabo sai pela face voltada ao destino, anda reto e entra
   pela face oposta. Curva livre fica bonita e some num mapa de 90
   switches, porque dois cabos quase paralelos viram um borrão. */
function rotaDe(a, b, enlace) {
  const ca = centroDe(a);
  const cb = centroDe(b);
  const dx = cb.x - ca.x;
  const dy = cb.y - ca.y;
  const da = enlace ? ancoragem.get(`${enlace.id}|${a.id}`) : null;
  const db = enlace ? ancoragem.get(`${enlace.id}|${b.id}`) : null;

  if (Math.abs(dy) >= Math.abs(dx)) {
    const sx = ca.x + (da && (da.face === 'N' || da.face === 'S') ? da.desvio : 0);
    const ex = cb.x + (db && (db.face === 'N' || db.face === 'S') ? db.desvio : 0);
    const sy = ca.y + Math.sign(dy || 1) * (NO_H / 2);
    const ey = cb.y - Math.sign(dy || 1) * (NO_H / 2);
    const my = (sy + ey) / 2;
    return {
      d: `M ${sx} ${sy} L ${sx} ${my} L ${ex} ${my} L ${ex} ${ey}`,
      rot: { x: (sx + ex) / 2, y: my - 4 },
    };
  }
  const sy = ca.y + (da && (da.face === 'L' || da.face === 'O') ? da.desvio : 0);
  const ey = cb.y + (db && (db.face === 'L' || db.face === 'O') ? db.desvio : 0);
  const sx = ca.x + Math.sign(dx || 1) * (NO_W / 2);
  const ex = cb.x - Math.sign(dx || 1) * (NO_W / 2);
  const mx = (sx + ex) / 2;
  return {
    d: `M ${sx} ${sy} L ${mx} ${sy} L ${mx} ${ey} L ${ex} ${ey}`,
    rot: { x: mx, y: (sy + ey) / 2 - 4 },
  };
}

const SVGNS = 'http://www.w3.org/2000/svg';

function desenharFios() {
  prepararRotas();
  el.fios.replaceChildren();
  const largura = Math.max(layout.canvas.width, 2000);
  const altura = Math.max(layout.canvas.height, 1200);
  el.fios.setAttribute('viewBox', `0 0 ${largura} ${altura}`);
  el.fios.setAttribute('width', String(largura));
  el.fios.setAttribute('height', String(altura));

  grafo.enlaces.forEach((e) => {
    const a = porId.get(e.de);
    const b = porId.get(e.para);
    if (!a || !b) return;
    const apagado = !visivel(a) || !visivel(b);
    const rota = rotaDe(a, b, e);

    const alvo = document.createElementNS(SVGNS, 'path');
    alvo.setAttribute('class', 'nx-fio-alvo');
    alvo.setAttribute('d', rota.d);
    alvo.dataset.enlace = e.id;
    el.fios.appendChild(alvo);

    const fio = document.createElementNS(SVGNS, 'path');
    fio.setAttribute('class', `nx-fio${enlaceSel === e.id ? ' is-sel' : ''}${apagado ? ' is-apagado' : ''}`);
    fio.setAttribute('d', rota.d);
    fio.dataset.meio = e.meio;
    fio.dataset.enlace = e.id;
    el.fios.appendChild(fio);

    if (!apagado && e.porta_de) {
      const rot = document.createElementNS(SVGNS, 'text');
      rot.setAttribute('class', 'nx-fio-rot');
      rot.setAttribute('x', String(rota.rot.x));
      rot.setAttribute('y', String(rota.rot.y));
      rot.setAttribute('text-anchor', 'middle');
      rot.textContent = e.porta_para ? `${e.porta_de} ⇄ ${e.porta_para}` : e.porta_de;
      el.fios.appendChild(rot);
    }
  });
}

/* Só os fios de um nó — é o que o arrasto redesenha. Redesenhar os
   200 enlaces a cada `pointermove` trava a prancha. */
function redesenharFiosDe(noId) {
  grafo.enlaces.forEach((e) => {
    if (e.de !== noId && e.para !== noId) return;
    const a = porId.get(e.de);
    const b = porId.get(e.para);
    if (!a || !b) return;
    const rota = rotaDe(a, b, e);
    el.fios.querySelectorAll(`[data-enlace="${CSS.escape(e.id)}"]`).forEach((n) => {
      if (n.tagName === 'path') n.setAttribute('d', rota.d);
    });
  });
}

function aplicarVista() {
  el.mundo.style.transform = `translate(${vista.x}px, ${vista.y}px) scale(${vista.z})`;
  el.prancha.style.setProperty('--nx-z', String(vista.z));
  el.prancha.style.setProperty('--nx-px', `${vista.x}px`);
  el.prancha.style.setProperty('--nx-py', `${vista.y}px`);
  texto(el.lerZoom, `${Math.round(vista.z * 100)}%`);
  desenharVistaMini();
}

/* =========================================================
   Pan, zoom e enquadramento
   ========================================================= */
function paraMundo(cx, cy) {
  const r = el.prancha.getBoundingClientRect();
  return {
    x: (cx - r.left - vista.x) / vista.z,
    y: (cy - r.top - vista.y) / vista.z,
  };
}

function zoomPara(fator, cx, cy) {
  const alvo = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, vista.z * fator));
  if (alvo === vista.z) return;
  const r = el.prancha.getBoundingClientRect();
  const px = (cx === undefined ? r.width / 2 : cx - r.left);
  const py = (cy === undefined ? r.height / 2 : cy - r.top);
  // Mantém sob o cursor o mesmo ponto do mundo: sem isso o zoom
  // "foge" e reenquadrar passa a ser necessário a cada aproximação.
  vista.x = px - (px - vista.x) * (alvo / vista.z);
  vista.y = py - (py - vista.y) * (alvo / vista.z);
  vista.z = alvo;
  aplicarVista();
}

function limitesDoDesenho() {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  grafo.nos.forEach((n) => {
    if (!visivel(n)) return;
    const p = posicaoDe(n);
    x0 = Math.min(x0, p.x); y0 = Math.min(y0, p.y);
    x1 = Math.max(x1, p.x + NO_W); y1 = Math.max(y1, p.y + NO_H);
  });
  if (x0 === Infinity) return { x0: 0, y0: 0, x1: 1400, y1: 900 };
  return { x0, y0, x1, y1 };
}

function enquadrar() {
  const b = limitesDoDesenho();
  const r = el.prancha.getBoundingClientRect();
  const margem = 40;
  const z = Math.min(
    (r.width - margem * 2) / Math.max(1, b.x1 - b.x0),
    (r.height - margem * 2) / Math.max(1, b.y1 - b.y0),
  );
  vista.z = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, z));
  vista.x = margem - b.x0 * vista.z + (r.width - margem * 2 - (b.x1 - b.x0) * vista.z) / 2;
  vista.y = margem - b.y0 * vista.z + (r.height - margem * 2 - (b.y1 - b.y0) * vista.z) / 2;
  aplicarVista();
}

function centralizarEm(noId) {
  const no = porId.get(noId);
  if (!no) return;
  const c = centroDe(no);
  const r = el.prancha.getBoundingClientRect();
  vista.x = r.width / 2 - c.x * vista.z;
  vista.y = r.height / 2 - c.y * vista.z;
  aplicarVista();
}

/* =========================================================
   Minimapa
   ========================================================= */
function desenharMini() {
  const b = limitesDoDesenho();
  el.mini.dataset.x0 = String(b.x0);
  el.mini.dataset.y0 = String(b.y0);
  el.mini.dataset.w = String(Math.max(1, b.x1 - b.x0));
  el.mini.dataset.h = String(Math.max(1, b.y1 - b.y0));

  const r = el.mini.getBoundingClientRect();
  const esc = Math.min(r.width / (b.x1 - b.x0 || 1), r.height / (b.y1 - b.y0 || 1));
  el.mini.dataset.esc = String(esc);

  el.miniNos.replaceChildren();
  grafo.nos.forEach((n) => {
    if (!visivel(n)) return;
    const p = posicaoDe(n);
    const ponto = criar('i', 'nx-mini-no');
    ponto.style.left = `${(p.x - b.x0) * esc}px`;
    ponto.style.top = `${(p.y - b.y0) * esc}px`;
    ponto.style.width = `${Math.max(2, NO_W * esc)}px`;
    ponto.style.height = `${Math.max(2, NO_H * esc)}px`;
    ponto.style.setProperty('--nx-cor', COR_SITUACAO[n.status.slug] || COR_SITUACAO.nao_monitorado);
    el.miniNos.appendChild(ponto);
  });
  desenharVistaMini();
}

function desenharVistaMini() {
  const esc = parseFloat(el.mini.dataset.esc || '0');
  if (!esc) return;
  const x0 = parseFloat(el.mini.dataset.x0 || '0');
  const y0 = parseFloat(el.mini.dataset.y0 || '0');
  const r = el.prancha.getBoundingClientRect();
  const vx = (-vista.x / vista.z - x0) * esc;
  const vy = (-vista.y / vista.z - y0) * esc;
  el.miniVista.style.left = `${vx}px`;
  el.miniVista.style.top = `${vy}px`;
  el.miniVista.style.width = `${(r.width / vista.z) * esc}px`;
  el.miniVista.style.height = `${(r.height / vista.z) * esc}px`;
}

/* =========================================================
   Seleção e arrasto
   ========================================================= */
function selecionar(noId, aditivo) {
  if (!aditivo) selecao.clear();
  if (noId) {
    if (aditivo && selecao.has(noId)) selecao.delete(noId);
    else selecao.add(noId);
  }
  enlaceSel = null;
  el.nos.querySelectorAll('.nx-no').forEach((n) => {
    n.classList.toggle('is-sel', selecao.has(n.dataset.no));
  });
  el.fios.querySelectorAll('.nx-fio').forEach((f) => f.classList.remove('is-sel'));
  montarStatus();
  if (selecao.size === 1) abrirInspetor([...selecao][0]);
  else if (selecao.size === 0) limparInspetor();
  else montarMulti();
}

function selecionarEnlace(id) {
  selecao.clear();
  enlaceSel = id;
  el.nos.querySelectorAll('.nx-no.is-sel').forEach((n) => n.classList.remove('is-sel'));
  el.fios.querySelectorAll('.nx-fio').forEach((f) => {
    f.classList.toggle('is-sel', f.dataset.enlace === id);
  });
  montarEnlace(id);
  montarStatus();
}

/* =========================================================
   Alinhamento: guias, encaixe e os comandos de arrumação
   ========================================================= */
/* Tolerância do encaixe, em px de MUNDO. Fixa de propósito: em px de
   tela ela mudaria com o zoom, e o encaixe ficaria grudento em 30% e
   inalcançável em 200%. */
const TOL_ALINHA = 7;

/* As três bordas de cada nó visível fora da seleção: esquerda, centro
   e direita (e topo, meio, base). Centro incluído porque alinhar pelo
   MEIO é o que faz um switch ficar sob o pai dele, e é o alinhamento
   que mais limpa o desenho. */
function alvosDeAlinhamento(excluir) {
  const x = new Set();
  const y = new Set();
  grafo.nos.forEach((n) => {
    if (excluir.has(n.id) || !visivel(n)) return;
    const p = posicaoDe(n);
    x.add(p.x); x.add(p.x + NO_W / 2); x.add(p.x + NO_W);
    y.add(p.y); y.add(p.y + NO_H / 2); y.add(p.y + NO_H);
  });
  return { x: [...x], y: [...y] };
}

function encaixarEm(bordas, candidatos) {
  let melhor = null;
  for (const b of bordas) {
    for (const c of candidatos) {
      const d = c - b;
      if (Math.abs(d) <= TOL_ALINHA
          && (melhor === null || Math.abs(d) < Math.abs(melhor.ajuste))) {
        melhor = { ajuste: d, linha: c };
      }
    }
  }
  return melhor;
}

function desenharGuias(gx, gy) {
  el.guias.replaceChildren();
  const b = limitesDoDesenho();
  const folga = 400;
  if (gx) {
    const linha = criar('div', 'nx-guia');
    linha.dataset.eixo = 'x';
    linha.style.left = `${gx.linha}px`;
    linha.style.top = `${b.y0 - folga}px`;
    linha.style.height = `${(b.y1 - b.y0) + folga * 2}px`;
    el.guias.appendChild(linha);
  }
  if (gy) {
    const linha = criar('div', 'nx-guia');
    linha.dataset.eixo = 'y';
    linha.style.top = `${gy.linha}px`;
    linha.style.left = `${b.x0 - folga}px`;
    linha.style.width = `${(b.x1 - b.x0) + folga * 2}px`;
    el.guias.appendChild(linha);
  }
}

/* Alinhar e distribuir a seleção. É o par de comandos que falta em
   todo editor que só tem malha: a malha deixa tudo em múltiplos de
   10, e "em múltiplos de 10" não é o mesmo que "na mesma linha". */
function alinhar(como) {
  if (selecao.size < 2) {
    avisar('Selecione dois ou mais equipamentos para alinhar.', 'ruim');
    return;
  }
  const caixas = [...selecao].map((id) => ({ id, p: { ...posicaoDe(porId.get(id)) } }));
  const x0 = Math.min(...caixas.map((c) => c.p.x));
  const x1 = Math.max(...caixas.map((c) => c.p.x + NO_W));
  const y0 = Math.min(...caixas.map((c) => c.p.y));
  const y1 = Math.max(...caixas.map((c) => c.p.y + NO_H));

  caixas.forEach(({ id, p }) => {
    const novo = { ...p };
    if (como === 'esq')     novo.x = x0;
    if (como === 'dir')     novo.x = x1 - NO_W;
    if (como === 'centroX') novo.x = (x0 + x1) / 2 - NO_W / 2;
    if (como === 'topo')    novo.y = y0;
    if (como === 'base')    novo.y = y1 - NO_H;
    if (como === 'centroY') novo.y = (y0 + y1) / 2 - NO_H / 2;
    layout.nos[id] = { ...(layout.nos[id] || {}), x: Math.round(novo.x), y: Math.round(novo.y) };
  });
  marcarSujo();
  desenhar();
  avisar(`${caixas.length} equipamentos alinhados.`, 'ok');
}

function distribuir(eixo) {
  if (selecao.size < 3) {
    avisar('Distribuir pede três ou mais equipamentos selecionados.', 'ruim');
    return;
  }
  const chave = eixo === 'x' ? 'x' : 'y';
  const tamanho = eixo === 'x' ? NO_W : NO_H;
  const caixas = [...selecao]
    .map((id) => ({ id, p: { ...posicaoDe(porId.get(id)) } }))
    .sort((a, b) => a.p[chave] - b.p[chave]);

  const inicio = caixas[0].p[chave];
  const fim = caixas[caixas.length - 1].p[chave] + tamanho;
  // Vão igual entre caixas, não centros igualmente espaçados: com
  // centros, caixas de alturas diferentes ficariam com folgas
  // visivelmente desiguais, que é o defeito que se quer corrigir.
  const ocupado = caixas.length * tamanho;
  const vao = (fim - inicio - ocupado) / (caixas.length - 1);
  let cursor = inicio;
  caixas.forEach(({ id, p }) => {
    const novo = { ...p };
    novo[chave] = Math.round(cursor);
    cursor += tamanho + vao;
    layout.nos[id] = { ...(layout.nos[id] || {}), x: novo.x, y: novo.y };
  });
  marcarSujo();
  desenhar();
  avisar(`${caixas.length} equipamentos distribuídos ${eixo === 'x' ? 'na horizontal' : 'na vertical'}.`, 'ok');
}

function ligarPrancha() {
  let arrastandoVista = null;
  let arrastandoNo = null;
  let laco = null;

  el.prancha.addEventListener('wheel', (ev) => {
    ev.preventDefault();
    if (ev.ctrlKey || ev.metaKey || !ev.shiftKey) {
      zoomPara(ev.deltaY < 0 ? 1.12 : 1 / 1.12, ev.clientX, ev.clientY);
    } else {
      vista.x -= ev.deltaX;
      vista.y -= ev.deltaY;
      aplicarVista();
    }
  }, { passive: false });

  el.prancha.addEventListener('pointerdown', (ev) => {
    const alvoFio = ev.target.closest('.nx-fio-alvo, .nx-fio');
    const alvoNo = ev.target.closest('.nx-no');

    // Botão do meio, espaço ou Alt arrastam a prancha — o botão
    // esquerdo sobre o vazio é laço de seleção, não pan: num mapa
    // grande, selecionar uma faixa é mais frequente que deslocar.
    const querPan = ev.button === 1 || ev.altKey || espacoApertado;
    if (querPan || (ev.button === 0 && !alvoNo && !alvoFio && modo === 'cabo')) {
      arrastandoVista = { cx: ev.clientX, cy: ev.clientY, x: vista.x, y: vista.y };
      el.prancha.dataset.arrastando = '1';
      el.prancha.setPointerCapture(ev.pointerId);
      ev.preventDefault();
      return;
    }

    if (alvoFio && alvoFio.dataset.enlace) {
      selecionarEnlace(alvoFio.dataset.enlace);
      return;
    }

    if (alvoNo) {
      const noId = alvoNo.dataset.no;
      if (modo === 'cabo') {
        clicarNoModoCabo(noId);
        return;
      }
      if (!selecao.has(noId)) selecionar(noId, ev.shiftKey || ev.ctrlKey);
      const inicio = paraMundo(ev.clientX, ev.clientY);
      arrastandoNo = {
        pointerId: ev.pointerId,
        inicio,
        mexeu: false,
        base: [...selecao].map((id) => ({ id, pos: { ...posicaoDe(porId.get(id)) } })),
        // O nó sob o cursor é a referência do alinhamento; os alvos são
        // medidos UMA vez, no início do arrasto. Remedi-los a cada
        // `pointermove` seria varrer 300 nós sessenta vezes por segundo.
        guia: { ...posicaoDe(porId.get(noId)) },
        alvos: alvosDeAlinhamento(selecao),
      };
      alvoNo.setPointerCapture(ev.pointerId);
      alvoNo.classList.add('is-arrastando');
      ev.preventDefault();
      return;
    }

    if (ev.button === 0) {
      const p = paraMundo(ev.clientX, ev.clientY);
      laco = { x0: p.x, y0: p.y, x1: p.x, y1: p.y };
      el.laco.hidden = false;
      el.prancha.setPointerCapture(ev.pointerId);
    }
  });

  el.prancha.addEventListener('pointermove', (ev) => {
    if (arrastandoVista) {
      vista.x = arrastandoVista.x + (ev.clientX - arrastandoVista.cx);
      vista.y = arrastandoVista.y + (ev.clientY - arrastandoVista.cy);
      aplicarVista();
      return;
    }
    if (arrastandoNo) {
      const agora = paraMundo(ev.clientX, ev.clientY);
      let dx = agora.x - arrastandoNo.inicio.x;
      let dy = agora.y - arrastandoNo.inicio.y;
      if (!arrastandoNo.mexeu && Math.hypot(dx, dy) < 3) return;
      arrastandoNo.mexeu = true;

      // ── Encaixe por ALINHAMENTO, antes do encaixe na malha ──────────
      // A malha alinha com a malha; isto alinha com os VIZINHOS, que é o
      // que faz o desenho parecer arrumado. Três px de diferença entre
      // duas caixas passam por alinhadas em zoom 40% e saltam aos olhos
      // em 120% — a guia é o que torna o alinhamento verificável.
      let guiaX = null;
      let guiaY = null;
      if (!ev.altKey && arrastandoNo.alvos) {
        const guia = arrastandoNo.guia;
        const bx = [guia.x + dx, guia.x + dx + NO_W / 2, guia.x + dx + NO_W];
        const by = [guia.y + dy, guia.y + dy + NO_H / 2, guia.y + dy + NO_H];
        guiaX = encaixarEm(bx, arrastandoNo.alvos.x);
        guiaY = encaixarEm(by, arrastandoNo.alvos.y);
        if (guiaX) dx += guiaX.ajuste;
        if (guiaY) dy += guiaY.ajuste;
      }
      desenharGuias(guiaX, guiaY);

      arrastandoNo.base.forEach(({ id, pos }) => {
        // Alt durante o arrasto solta o encaixe (malha e alinhamento) —
        // ajuste fino sem ter de desligar nada na barra.
        const encaixado = !ev.altKey && !guiaX && !guiaY;
        const nx = encaixado ? Math.round((pos.x + dx) / PASSO) * PASSO : pos.x + dx;
        const ny = encaixado ? Math.round((pos.y + dy) / PASSO) * PASSO : pos.y + dy;
        layout.nos[id] = { ...(layout.nos[id] || {}), x: nx, y: ny };
        const caixa = el.nos.querySelector(`[data-no="${CSS.escape(id)}"]`);
        if (caixa) { caixa.style.left = `${nx}px`; caixa.style.top = `${ny}px`; }
        redesenharFiosDe(id);
      });
      return;
    }
    if (laco) {
      const p = paraMundo(ev.clientX, ev.clientY);
      laco.x1 = p.x; laco.y1 = p.y;
      const x = Math.min(laco.x0, laco.x1);
      const y = Math.min(laco.y0, laco.y1);
      el.laco.style.left = `${x}px`;
      el.laco.style.top = `${y}px`;
      el.laco.style.width = `${Math.abs(laco.x1 - laco.x0)}px`;
      el.laco.style.height = `${Math.abs(laco.y1 - laco.y0)}px`;
    }
  });

  function encerrar(ev) {
    if (arrastandoVista) {
      arrastandoVista = null;
      delete el.prancha.dataset.arrastando;
    }
    if (arrastandoNo) {
      el.nos.querySelectorAll('.is-arrastando').forEach((n) => n.classList.remove('is-arrastando'));
      if (arrastandoNo.mexeu) {
        marcarSujo();
        // Redesenho COMPLETO ao soltar: mover um no muda a face por onde
        // os cabos dos vizinhos saem, e so o redesenho refaz a ancoragem.
        // Durante o arrasto isso seria caro; uma vez, no fim, nao e.
        desenharFios();
        desenharMini();
      }
      arrastandoNo = null;
      desenharGuias(null, null);
    }
    if (laco) {
      const x0 = Math.min(laco.x0, laco.x1);
      const x1 = Math.max(laco.x0, laco.x1);
      const y0 = Math.min(laco.y0, laco.y1);
      const y1 = Math.max(laco.y0, laco.y1);
      const pegou = (Math.abs(x1 - x0) > 6 || Math.abs(y1 - y0) > 6);
      if (pegou) {
        const aditivo = ev && (ev.shiftKey || ev.ctrlKey);
        if (!aditivo) selecao.clear();
        grafo.nos.forEach((n) => {
          if (!visivel(n)) return;
          const p = posicaoDe(n);
          if (p.x + NO_W >= x0 && p.x <= x1 && p.y + NO_H >= y0 && p.y <= y1) selecao.add(n.id);
        });
        selecionar(null, true);
      } else {
        selecionar(null, false);
      }
      laco = null;
      el.laco.hidden = true;
    }
  }

  el.prancha.addEventListener('pointerup', encerrar);
  el.prancha.addEventListener('pointercancel', encerrar);
  el.prancha.addEventListener('dblclick', (ev) => {
    const no = ev.target.closest('.nx-no');
    if (no) { abrirInspetor(no.dataset.no); el.insp.dataset.aberto = '1'; }
  });

  // Soltar equipamento do inventário sobre a prancha
  el.prancha.addEventListener('dragover', (ev) => { ev.preventDefault(); });
  el.prancha.addEventListener('drop', (ev) => {
    ev.preventDefault();
    const itemId = ev.dataTransfer.getData('text/x-item');
    if (!itemId) return;
    const p = paraMundo(ev.clientX, ev.clientY);
    porNoMapa(itemId, Math.round(p.x / PASSO) * PASSO, Math.round(p.y / PASSO) * PASSO);
  });
}

let espacoApertado = false;

/* =========================================================
   Inventário (trilho esquerdo)
   ========================================================= */
let timerInventario = null;

function pedirInventario() {
  clearTimeout(timerInventario);
  timerInventario = setTimeout(async () => {
    const p = paramsEscopo({ q: el.buscaInv.value.trim() });
    try {
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

  const porGrupo = new Map();
  itens.forEach((i) => {
    if (!porGrupo.has(i.grupo)) porGrupo.set(i.grupo, []);
    porGrupo.get(i.grupo).push(i);
  });

  const noMapa = new Set(grafo.nos.filter((n) => layout.nos[n.id]).map((n) => n.item_id));

  [...porGrupo.entries()].sort((a, b) => a[0].localeCompare(b[0], 'pt-BR')).forEach(([grupo, lista]) => {
    const det = criar('details', 'nx-grupo');
    det.open = porGrupo.size <= 4;
    const sum = criar('summary');
    sum.appendChild(criar('span', '', grupo));
    sum.appendChild(criar('span', 'nx-conta', String(lista.length)));
    det.appendChild(sum);

    lista.forEach((i) => {
      const linha = criar('button', 'nx-lib');
      linha.type = 'button';
      linha.draggable = true;
      linha.dataset.item = String(i.item_id);
      linha.dataset.noMapa = noMapa.has(i.item_id) ? '1' : '0';
      linha.style.setProperty('--nx-classe', COR_CLASSE[i.classe] || COR_CLASSE.generico);
      linha.title = `${i.nome}\n${i.marca} ${i.modelo}`.trim()
        + `\n${i.localidade || 'sem localidade'}`
        + (noMapa.has(i.item_id) ? '\nJá está no mapa' : '\nArraste para a prancha');

      const ic = criar('span', 'nx-no-icone');
      ic.appendChild(iconeSvg(i.classe));
      linha.appendChild(ic);

      const corpo = criar('span', 'nx-lib-corpo');
      corpo.appendChild(criar('span', 'nx-lib-nome', i.nome));
      corpo.appendChild(criar('span', 'nx-lib-meta',
        [i.localidade, `${i.marca} ${i.modelo}`.trim()].filter(Boolean).join(' · ')));
      linha.appendChild(corpo);

      linha.appendChild(criar('span', 'nx-lib-portas', i.portas ? `${i.portas}p` : '—'));
      det.appendChild(linha);
    });
    el.inventario.appendChild(det);
  });
}

function porNoMapa(itemId, x, y) {
  const no = grafo.nos.find((n) => String(n.item_id) === String(itemId));
  if (no) {
    layout.nos[no.id] = { ...(layout.nos[no.id] || {}), x, y, oculto: false };
    marcarSujo();
    desenhar();
    selecionar(no.id, false);
    avisar(`${no.nome} posicionado no mapa.`, 'ok');
    return;
  }
  // Fora do escopo carregado: o nó existe no inventário mas o grafo
  // atual não o traz. Dizer isso é melhor que posicionar um fantasma.
  avisar('Este equipamento está fora do escopo carregado. Ajuste localidade/área e tente de novo.', 'ruim');
}

/* =========================================================
   Camadas e filtros
   ========================================================= */
function montarClasses() {
  const conta = new Map();
  grafo.nos.forEach((n) => conta.set(n.classe, (conta.get(n.classe) || 0) + 1));
  classesConhecidas = [...conta.entries()].sort((a, b) => b[1] - a[1]);
}

function montarCamadas() {
  el.camadasClasse.replaceChildren();
  classesConhecidas.forEach(([classe, n]) => {
    const rot = criar('label', 'nx-camada');
    rot.style.setProperty('--nx-cor', COR_CLASSE[classe] || COR_CLASSE.generico);
    const cb = criar('input');
    cb.type = 'checkbox';
    cb.checked = !filtros.classes.size || filtros.classes.has(classe);
    cb.addEventListener('change', () => {
      alternarFiltro(filtros.classes, classe, cb.checked, classesConhecidas.map((c) => c[0]));
      aplicarFiltros();
    });
    rot.appendChild(cb);
    rot.appendChild(criar('span', 'nx-marcador'));
    rot.appendChild(criar('span', '', ROTULO_CLASSE[classe] || classe));
    rot.appendChild(criar('span', 'nx-conta', String(n)));
    el.camadasClasse.appendChild(rot);
  });

  const conta = new Map();
  grafo.nos.forEach((n) => conta.set(n.status.slug, (conta.get(n.status.slug) || 0) + 1));
  el.camadasSit.replaceChildren();
  [...conta.entries()].forEach(([slug, n]) => {
    const rot = criar('label', 'nx-camada');
    rot.style.setProperty('--nx-cor', COR_SITUACAO[slug] || COR_SITUACAO.nao_monitorado);
    const cb = criar('input');
    cb.type = 'checkbox';
    cb.checked = !filtros.situacoes.size || filtros.situacoes.has(slug);
    cb.addEventListener('change', () => {
      alternarFiltro(filtros.situacoes, slug, cb.checked, [...conta.keys()]);
      aplicarFiltros();
    });
    rot.appendChild(cb);
    rot.appendChild(criar('span', 'nx-marcador'));
    rot.appendChild(criar('span', '', ROTULO_SITUACAO[slug] || slug));
    rot.appendChild(criar('span', 'nx-conta', String(n)));
    el.camadasSit.appendChild(rot);
  });
}

/* Conjunto vazio = "tudo ligado". Guardar as 19 classes marcadas
   daria o mesmo resultado com mais estado para manter em sincronia —
   e um desmarcar seguido de um recarregar perderia a conta. */
function alternarFiltro(conjunto, chave, ligado, universo) {
  if (!conjunto.size) universo.forEach((k) => conjunto.add(k));
  if (ligado) conjunto.add(chave); else conjunto.delete(chave);
  if (conjunto.size === universo.length) conjunto.clear();
}

function aplicarFiltros() {
  el.nos.querySelectorAll('.nx-no').forEach((n) => {
    const no = porId.get(n.dataset.no);
    n.classList.toggle('is-apagado', !(no && visivel(no)));
  });
  desenharFios();
  desenharMini();
  montarStatus();
}

/* =========================================================
   Inspetor — ficha, portas e conferência
   ========================================================= */
function limparInspetor() {
  dispAtual = null;
  portaSel = null;
  el.inspVazio.hidden = false;
  el.inspFicha.hidden = true;
  el.inspMulti.hidden = true;
  el.inspEnlace.hidden = true;
}

function montarMulti() {
  el.inspVazio.hidden = true;
  el.inspFicha.hidden = true;
  el.inspEnlace.hidden = true;
  el.inspMulti.hidden = false;
  texto(el.multiConta, String(selecao.size));
}

function montarEnlace(id) {
  const e = grafo.enlaces.find((x) => x.id === id);
  if (!e) return;
  el.inspVazio.hidden = true;
  el.inspFicha.hidden = true;
  el.inspMulti.hidden = true;
  el.inspEnlace.hidden = false;

  const a = porId.get(e.de);
  const b = porId.get(e.para);
  const kv = el.enlaceKv;
  kv.replaceChildren();
  const par = (rotulo, valor) => {
    kv.appendChild(criar('dt', '', rotulo));
    kv.appendChild(criar('dd', '', valor || '—'));
  };
  par('Origem', a ? a.nome : '—');
  par('Porta de origem', e.porta_de);
  par('Destino', b ? b.nome : '—');
  par('Porta de destino', e.porta_para || 'não mapeada');
  par('Meio', ROTULO_MEIO[e.meio] || e.meio);
  par('Velocidade', e.velocidade || '—');
  par('VLAN', e.vlan || '—');
  par('PoE', e.poe ? 'Sim' : 'Não');
  par('Descrição', e.descricao || '—');

  el.enlaceSoltar.onclick = async () => {
    if (!confirm('Retirar o cabo desta porta? As duas pontas voltam para "livre".')) return;
    try {
      const r = await enviar(ENLACE_URL, { porta: e.porta_de_id, acao: 'desconectar' });
      if (!r.ok) throw new Error(r.erro || 'Falha');
      avisar('Cabo retirado.', 'ok');
      enlaceSel = null;
      limparInspetor();
      carregar(false);
    } catch (erro) { avisar(erro.message, 'ruim'); }
  };
}

async function abrirInspetor(noId) {
  const no = porId.get(noId);
  if (!no) return;
  noFoco = noId;
  el.inspVazio.hidden = true;
  el.inspMulti.hidden = true;
  el.inspEnlace.hidden = true;
  el.inspFicha.hidden = false;
  texto(el.fichaNome, no.nome);
  texto(el.fichaSub, `${ROTULO_CLASSE[no.classe] || no.classe} · ${no.localidade || 'sem localidade'}`);

  try {
    const dados = await pedir(`${DISPOSITIVO_URL}?item=${encodeURIComponent(no.item_id)}`);
    if (!dados.ok) throw new Error(dados.erro || 'Falha');
    dispAtual = dados;
    portaSel = null;
    montarFicha(no, dados);
    montarPortas(dados);
  } catch (erro) {
    avisar(`Não foi possível abrir a ficha: ${erro.message}`, 'ruim');
  }
}

function montarFicha(no, dados) {
  el.fichaPapel.value = no.papel || 'acesso';
  el.fichaIp.value = dados.ip || '';
  el.fichaPrtg.value = dados.prtg_objid == null ? '' : String(dados.prtg_objid);
  el.fichaObs.value = dados.observacoes || '';

  const kv = el.fichaKv;
  kv.replaceChildren();
  const par = (rotulo, valor) => {
    kv.appendChild(criar('dt', '', rotulo));
    kv.appendChild(criar('dd', '', valor || '—'));
  };
  const it = dados.item || {};
  par('Marca / modelo', `${it.marca || ''} ${it.modelo || ''}`.trim());
  par('Nº de série', it.serie);
  par('Subtipo', it.subtipo);
  par('Localidade', it.localidade);
  par('Área', it.area);
  par('Situação do ativo', it.status);

  const sit = criar('span', 'nx-chip');
  const slug = no.status.slug;
  sit.dataset.tom = slug === 'up' ? 'ok' : (slug === 'down' ? 'ruim' : (slug === 'warning' || slug === 'unusual' ? 'atencao' : ''));
  texto(sit, ROTULO_SITUACAO[slug] || slug);
  kv.appendChild(criar('dt', '', 'Monitoramento'));
  const ddSit = criar('dd');
  ddSit.appendChild(sit);
  if (no.status.uptime != null) {
    ddSit.appendChild(document.createTextNode(' '));
    const up = criar('span', 'nx-num', `${Number(no.status.uptime).toFixed(1)}%`);
    ddSit.appendChild(up);
  }
  kv.appendChild(ddSit);

  // ── Onde ele está fisicamente: rack, altura em U e coordenada ──────
  el.fichaRack.replaceChildren();
  const semRack = criar('option', '', 'Fora de rack');
  semRack.value = '';
  el.fichaRack.appendChild(semRack);
  (dados.racks || []).forEach((r) => {
    const o = criar('option', '', `${r.nome}${r.codigo ? ` (${r.codigo})` : ''} — ${r.us}U`);
    o.value = String(r.id);
    el.fichaRack.appendChild(o);
  });
  el.fichaRack.value = dados.rack_id ? String(dados.rack_id) : '';
  el.fichaRackU.value = dados.rack_u == null ? '' : String(dados.rack_u);
  el.fichaRackAlt.value = String(dados.rack_altura_u || 1);
  el.fichaLat.value = dados.latitude == null ? '' : String(dados.latitude);
  el.fichaLon.value = dados.longitude == null ? '' : String(dados.longitude);
  el.btnElevacao.disabled = !dados.rack_id;

  // Seletor de modelo do catálogo, já no palpite do servidor
  el.fichaModelo.replaceChildren();
  const vazio = criar('option', '', 'Escolha o modelo…');
  vazio.value = '';
  el.fichaModelo.appendChild(vazio);
  CATALOGO.forEach((m) => {
    const o = criar('option', '', `${m.nome} (${m.portas}p)`);
    o.value = m.chave;
    el.fichaModelo.appendChild(o);
  });
  el.fichaModelo.value = dados.modelo_portas || dados.modelo_sugerido || '';
}

/* ── O FACEPLATE ───────────────────────────────────────────────────
   O painel desenhado como o equipamento é: blocos de seis colunas
   separados pelo vão do metal, par embaixo de ímpar, número do grupo
   impresso em cima, gaiola de SFP destacada à direita.

   O arranjo (quem é topo, quem é base, que bloco) vem MONTADO do
   servidor em `dados.face` — é conhecimento do equipamento, e deixá-lo
   aqui obrigaria o Excel e qualquer outra saída a reimplementar a
   mesma regra para desenhar o mesmo painel. */
const ROTULO_PORTA_ST = {
  ativa: 'Ativa (em uso)', livre: 'Livre', reservada: 'Reservada',
  defeito: 'Com defeito', desligada: 'Desligada',
};

function botaoPorta(p) {
  const b = criar('button', 'nx-face-porta');
  b.type = 'button';
  b.dataset.porta = String(p.id);
  b.dataset.st = p.status;
  b.dataset.tipo = p.tipo;
  b.dataset.poe = p.poe ? '1' : '0';
  b.dataset.cabo = (p.destino_item_id || p.destino_porta_id) ? '1' : '0';
  if (portaSel === p.id) b.classList.add('is-sel');
  // O número cabe; o rótulo do fabricante inteiro ("Gi1/0/12") não —
  // ele vai no `title` e no detalhe, onde há largura para ele.
  texto(b, String(p.numero));
  b.title = `${p.rotulo} · ${p.tipo_label} · ${p.status_label}`
    + (p.destino_nome
        ? `\n→ ${p.destino_nome}${p.destino_porta_rotulo ? ` (${p.destino_porta_rotulo})` : ' (porta não mapeada)'}`
        : '\nsem cabo')
    + (p.vlan ? `\nVLAN ${p.vlan}` : '')
    + (p.poe ? '\nAlimenta PoE' : '')
    + (p.velocidade_label ? `\n${p.velocidade_label}` : '')
    + (p.rota_nome ? `\nRota: ${p.rota_nome}` : '')
    + (p.descricao ? `\n${p.descricao}` : '');
  b.setAttribute('aria-label', `Porta ${p.rotulo}, ${p.status_label}`);
  return b;
}

function montarFace(dados) {
  const face = dados.face || { blocos: [], uplinks: [] };
  el.faceChassi.replaceChildren();

  if (!dados.total) {
    const vazio = criar('p', 'nx-aviso-d',
      'Nenhuma porta cadastrada. Escolha o modelo do catálogo abaixo e use "Gerar".');
    vazio.style.margin = '0';
    el.faceChassi.appendChild(vazio);
    return;
  }

  const blocos = criar('div', 'nx-face-blocos');
  (face.blocos || []).forEach((bloco) => {
    const cx = criar('div', 'nx-face-bloco');
    const cols = criar('div', 'nx-face-cols');
    let primeiro = null;
    let ultimo = null;
    bloco.colunas.forEach((col) => {
      const coluna = criar('div', 'nx-face-col');
      [col.topo, col.base].forEach((p) => {
        if (p) {
          coluna.appendChild(botaoPorta(p));
          if (primeiro === null || p.numero < primeiro) primeiro = p.numero;
          if (ultimo === null || p.numero > ultimo) ultimo = p.numero;
        } else {
          // Coluna sem par (painel de número ímpar de portas): o vazio
          // guarda o lugar, senão a coluna seguinte sobe e o painel
          // deixa de bater com o equipamento.
          coluna.appendChild(criar('span', 'nx-face-vago'));
        }
      });
      cols.appendChild(coluna);
    });
    cx.appendChild(cols);
    cx.appendChild(criar('div', 'nx-face-rot',
      primeiro === ultimo ? `${primeiro}` : `${primeiro}–${ultimo}`));
    blocos.appendChild(cx);
  });
  el.faceChassi.appendChild(blocos);

  if ((face.uplinks || []).length) {
    const cage = criar('div', 'nx-face-cage');
    const cols = criar('div', 'nx-face-cols');
    face.uplinks.forEach((p) => {
      const coluna = criar('div', 'nx-face-col');
      coluna.appendChild(botaoPorta(p));
      cols.appendChild(coluna);
    });
    cage.appendChild(cols);
    cage.appendChild(criar('div', 'nx-face-rot', 'UPLINK'));
    el.faceChassi.appendChild(cage);
  }
}

/* Régua segmentada: a COMPOSIÇÃO das portas, não só a ocupação. Dez
   livres e dez com defeito dão a mesma "ocupação de 50%" e são
   problemas opostos — uma barra só não distingue os dois. */
function montarReguaSegmentada(dados) {
  el.faceReg.replaceChildren();
  const total = dados.total || 0;
  if (!total) return;
  ['ativa', 'reservada', 'defeito', 'desligada', 'livre'].forEach((st) => {
    const n = (dados.resumo || {})[st] || 0;
    if (!n) return;
    const seg = criar('i');
    seg.dataset.st = st;
    seg.style.flex = `${n} 1 0`;
    seg.title = `${n} ${ROTULO_PORTA_ST[st] || st}`;
    el.faceReg.appendChild(seg);
  });
}

function montarVizinhos(dados) {
  const lista = dados.vizinhos || [];
  el.vizinhos.replaceChildren();
  el.vizinhosBloco.hidden = lista.length === 0;
  texto(el.vizinhosConta, String(lista.length));
  lista.forEach((v) => {
    const b = criar('button', 'nx-face-vizinho');
    b.type = 'button';
    b.appendChild(criar('b', '', v.nome));
    b.appendChild(criar('span', 'nx-conta', `${v.enlaces.length} cabo(s)`));
    b.appendChild(criar('span', '', v.enlaces
      .map((e) => `${e.porta}${e.porta_remota ? ` ⇄ ${e.porta_remota}` : ''}`)
      .join('  ·  ')));
    b.title = `Centralizar ${v.nome} no mapa`;
    b.onclick = () => {
      if (porId.has(v.no)) {
        selecionar(v.no, false);
        centralizarEm(v.no);
      } else {
        avisar(`${v.nome} está fora do escopo carregado.`, 'ruim');
      }
    };
    el.vizinhos.appendChild(b);
  });
}

function montarPortas(dados) {
  texto(el.portasTotal, String(dados.total));
  texto(el.portasAtivas, String(dados.ativas));
  texto(el.portasLivres, String(dados.livres));
  texto(el.portasPoe, String(dados.poe_ativas || 0));
  const ocup = dados.total ? Math.round(dados.ativas * 100 / dados.total) : 0;
  texto(el.portasOcup, `${ocup}%`);

  montarFace(dados);
  montarReguaSegmentada(dados);
  montarVizinhos(dados);

  el.semRota.hidden = !dados.sem_rota;
  texto(el.semRotaConta, String(dados.sem_rota || 0));

  el.portaDet.hidden = portaSel == null;
  if (portaSel != null) montarPorta(portaSel);
  destacarCaboDaPorta();
}

function portaPorId(id) {
  if (!dispAtual) return null;
  return [...(dispAtual.acessos || []), ...(dispAtual.uplinks || [])].find((p) => p.id === id) || null;
}

function montarPorta(id) {
  const p = portaPorId(id);
  if (!p) { el.portaDet.hidden = true; return; }
  el.portaDet.hidden = false;
  texto(el.portaTitulo, p.rotulo);
  texto(el.portaSit, p.status_label);
  el.portaSit.dataset.tom = p.status === 'ativa' ? 'ok'
    : (p.status === 'defeito' ? 'ruim' : (p.status === 'reservada' ? 'acento' : ''));
  el.portaRotulo.value = p.rotulo;
  el.portaTipo.value = p.tipo;
  el.portaStatus.value = p.status;
  el.portaVel.value = p.velocidade || '';
  el.portaMeio.value = p.meio;
  el.portaVlan.value = p.vlan || '';
  el.portaPoe.checked = !!p.poe;
  el.portaDesc.value = p.descricao || '';

  // Seletor de rota física: por onde este lance passa no terreno. A
  // lista veio junto da ficha (ver `rede_dispositivo_api`).
  el.portaRota.replaceChildren();
  const semRota = criar('option', '', 'Sem rota declarada');
  semRota.value = '';
  el.portaRota.appendChild(semRota);
  (dispAtual.rotas || []).forEach((r) => {
    const o = criar('option', '', `${r.nome} — ${r.tipo_label} · ${Math.round(r.comprimento_m)} m`);
    o.value = String(r.id);
    el.portaRota.appendChild(o);
  });
  el.portaRota.value = p.rota_id ? String(p.rota_id) : '';

  texto(el.portaDestino, p.destino_nome
    ? `${p.destino_nome}${p.destino_porta_rotulo ? ` · ${p.destino_porta_rotulo}` : ' · porta não mapeada'}`
    : 'Sem cabo');
  texto(el.portaRotaLer, p.rota_nome
    ? `${p.rota_nome} · ${Math.round(p.rota_m || 0)} m`
    : (p.destino_nome ? 'traçado físico não declarado' : '—'));
  el.portaSoltar.disabled = !p.destino_nome;
  el.portaIdentificar.disabled = !p.destino_nome;
  el.portaLigar.textContent = p.destino_nome ? 'Trocar destino' : 'Ligar cabo';
}

/* ── IDENTIFICAÇÃO ─────────────────────────────────────────────────
   Escolher a porta e ver, no mapa, QUAL cabo é o dela e onde ele
   termina. É a pergunta que se faz de pé na frente do rack ("a 12 vai
   para onde?"), e até agora a resposta era ler o texto do destino e
   procurar o nome na prancha com os olhos.

   O destaque é derivado, não guardado: ele sai da porta selecionada a
   cada redesenho. Guardar "qual fio está aceso" num estado próprio
   criaria a chance de ele apontar para um enlace que já foi desfeito. */
function destacarCaboDaPorta() {
  el.fios.querySelectorAll('.nx-fio.is-destaque')
    .forEach((f) => f.classList.remove('is-destaque'));
  el.nos.querySelectorAll('.nx-no.is-ponta')
    .forEach((n) => n.classList.remove('is-ponta'));
  if (portaSel == null) return;
  const p = portaPorId(portaSel);
  if (!p || !p.destino_item_id) return;

  const enlace = grafo.enlaces.find(
    (e) => e.porta_de_id === p.id || e.porta_para_id === p.id
  );
  if (enlace) {
    el.fios.querySelectorAll(`.nx-fio[data-enlace="${enlace.id}"]`)
      .forEach((f) => f.classList.add('is-destaque'));
  }
  [dispAtual && `i${dispAtual.item_id}`, `i${p.destino_item_id}`]
    .filter(Boolean)
    .forEach((noId) => {
      const caixa = el.nos.querySelector(`[data-no="${CSS.escape(noId)}"]`);
      if (caixa) caixa.classList.add('is-ponta');
    });
}

/* Pisca a porta no painel e leva a vista até a outra ponta do cabo. */
function identificarPorta() {
  if (portaSel == null) return;
  const p = portaPorId(portaSel);
  if (!p) return;
  const botao = el.faceChassi.querySelector(`[data-porta="${p.id}"]`);
  if (botao) {
    botao.classList.remove('is-piscando');
    // Releitura forçada: sem ela, remover e recolocar a classe no mesmo
    // quadro não reinicia a animação, e identificar a mesma porta duas
    // vezes seguidas não pisca na segunda.
    void botao.offsetWidth;
    botao.classList.add('is-piscando');
  }
  destacarCaboDaPorta();
  if (p.destino_item_id && porId.has(`i${p.destino_item_id}`)) {
    centralizarEm(`i${p.destino_item_id}`);
    avisar(`${p.rotulo} → ${p.destino_nome}`
      + (p.rota_nome ? ` · pela rota ${p.rota_nome}` : ''), 'ok');
  } else if (p.destino_nome) {
    avisar(`${p.rotulo} → ${p.destino_nome} (fora do escopo carregado).`);
  } else {
    avisar(`${p.rotulo} está sem cabo.`);
  }
}

/* =========================================================
   Ações de porta
   ========================================================= */
async function salvarPorta() {
  if (!portaSel) return;
  try {
    const r = await enviar(PORTA_URL, {
      porta: portaSel,
      rotulo: el.portaRotulo.value,
      tipo: el.portaTipo.value,
      status: el.portaStatus.value,
      velocidade: el.portaVel.value,
      meio: el.portaMeio.value,
      vlan: el.portaVlan.value,
      poe: el.portaPoe.checked,
      descricao: el.portaDesc.value,
      rota: el.portaRota.value || null,
    });
    if (!r.ok) throw new Error(r.erro || 'Falha ao gravar a porta.');
    dispAtual = { ...dispAtual, ...r };
    montarPortas(dispAtual);
    avisar('Porta atualizada.', 'ok');
    carregar(false);
  } catch (erro) { avisar(erro.message, 'ruim'); }
}

async function excluirPorta() {
  if (!portaSel) return;
  if (!confirm('Excluir esta porta do cadastro?')) return;
  try {
    const r = await enviar(PORTA_URL, { porta: portaSel, acao: 'excluir' });
    if (!r.ok) throw new Error(r.erro || 'Falha');
    portaSel = null;
    dispAtual = { ...dispAtual, ...r };
    montarPortas(dispAtual);
    avisar('Porta excluída.', 'ok');
    carregar(false);
  } catch (erro) { avisar(erro.message, 'ruim'); }
}

async function gerarPortas(substituir) {
  if (!dispAtual) return;
  const modelo = el.fichaModelo.value;
  if (!modelo) { avisar('Escolha o modelo do catálogo primeiro.', 'ruim'); return; }
  try {
    const r = await enviar(PORTAS_URL, {
      item: dispAtual.item_id, acao: 'gerar', modelo, substituir: !!substituir,
    });
    if (!r.ok) throw new Error(r.erro || 'Falha');
    dispAtual = { ...dispAtual, ...r };
    montarPortas(dispAtual);
    avisar(r.criadas
      ? `${r.criadas} porta(s) criada(s). As que já tinham cabo foram preservadas.`
      : 'Nenhuma porta nova — o painel já estava completo.', 'ok');
    carregar(false);
  } catch (erro) { avisar(erro.message, 'ruim'); }
}

async function novaPorta() {
  if (!dispAtual) return;
  try {
    const r = await enviar(PORTAS_URL, { item: dispAtual.item_id, acao: 'nova' });
    if (!r.ok) throw new Error(r.erro || 'Falha');
    dispAtual = { ...dispAtual, ...r };
    montarPortas(dispAtual);
  } catch (erro) { avisar(erro.message, 'ruim'); }
}

async function soltarPorta() {
  if (!portaSel) return;
  try {
    const r = await enviar(ENLACE_URL, { porta: portaSel, acao: 'desconectar' });
    if (!r.ok) throw new Error(r.erro || 'Falha');
    dispAtual = { ...dispAtual, ...r };
    montarPortas(dispAtual);
    avisar('Cabo retirado.', 'ok');
    carregar(false);
  } catch (erro) { avisar(erro.message, 'ruim'); }
}

/* ── Vínculo com o PRTG ────────────────────────────────────────────
   O campo é o `objid`, que ninguém decora. A busca existe para o
   vínculo ser feito pelo NOME do dispositivo no PRTG — que é o que o
   TI conhece — e a credencial continua no servidor: o que chega aqui
   é só o JSON filtrado de `/plantas/api/prtg-buscar/` (regra 12). */
let timerPrtg = null;

function ligarBuscaPrtg() {
  if (!PRTG_OK) { el.prtgBloco.hidden = true; return; }
  el.prtgBusca.addEventListener('input', () => {
    clearTimeout(timerPrtg);
    const termo = el.prtgBusca.value.trim();
    if (termo.length < 2) { el.prtgLista.replaceChildren(); return; }
    timerPrtg = setTimeout(async () => {
      try {
        const dados = await pedir(`${PRTG_BUSCA_URL}?q=${encodeURIComponent(termo)}`);
        el.prtgLista.replaceChildren();
        const achados = dados.results || dados.resultados || [];
        if (!achados.length) {
          el.prtgLista.appendChild(criar('div', 'nx-escolha', 'Nenhum dispositivo com esse nome no PRTG.'));
          return;
        }
        achados.forEach((d) => {
          const b = criar('button', 'nx-escolha');
          b.type = 'button';
          const corpo = criar('span');
          corpo.appendChild(criar('span', '', d.name || ''));
          corpo.appendChild(criar('span', 'nx-aviso-d', `${d.host || ''} · ${d.group || ''}`));
          b.appendChild(corpo);
          b.appendChild(criar('span', 'nx-conta', String(d.objid)));
          b.onclick = () => {
            el.fichaPrtg.value = String(d.objid);
            el.prtgLista.replaceChildren();
            el.prtgBusca.value = '';
            avisar(`Vínculo apontado para "${d.name}". Salve a ficha para gravar.`);
          };
          el.prtgLista.appendChild(b);
        });
      } catch (erro) { avisar(`Busca no PRTG falhou: ${erro.message}`, 'ruim'); }
    }, 280);
  });
}

async function salvarDispositivo() {
  if (!dispAtual) return;
  try {
    const r = await enviar(DISPOSITIVO_URL, {
      item: dispAtual.item_id,
      papel: el.fichaPapel.value,
      ip: el.fichaIp.value,
      prtg_objid: el.fichaPrtg.value,
      observacoes: el.fichaObs.value,
      rack: el.fichaRack.value || null,
      rack_u: el.fichaRackU.value || null,
      rack_altura_u: el.fichaRackAlt.value || 1,
      latitude: el.fichaLat.value || null,
      longitude: el.fichaLon.value || null,
    });
    if (!r.ok) throw new Error(r.erro || 'Falha');
    avisar('Ficha de rede atualizada.', 'ok');
    if (noFoco) abrirInspetor(noFoco);
    carregar(false);
  } catch (erro) { avisar(erro.message, 'ruim'); }
}

/* ── Elevação do rack ──────────────────────────────────────────────
   O armário visto de frente. É a vista que responde "cabe o switch
   novo?" e "em que altura está o que eu procuro" — e é o anexo que o
   gestor entende sem precisar de legenda. */
async function abrirElevacao() {
  const rackId = el.fichaRack.value;
  if (!rackId) { avisar('Este equipamento não está em rack.', 'ruim'); return; }
  try {
    const dados = await pedir(`${ELEVACAO_URL}?rack=${encodeURIComponent(rackId)}`);
    if (!dados.ok) throw new Error(dados.erro || 'Falha');
    const r = dados.rack;
    abrirModal('modalRack');
    texto(el.rackTitulo, r.nome + (r.codigo ? ` · ${r.codigo}` : ''));
    texto(el.rackSub, [r.localidade, r.ambiente].filter(Boolean).join(' · ') || '—');
    texto(el.rackUs, String(r.us));
    texto(el.rackOcup, String(r.us_ocupados));
    texto(el.rackLivres, String(r.us_livres));

    const num = criar('div', 'nx-rack-num');
    const trilha = criar('div', 'nx-rack-trilha');
    trilha.style.gridTemplateRows = `repeat(${r.us}, 17px)`;
    dados.regua.forEach((faixa) => {
      const marca = criar('i', '', `${faixa.u}`);
      if (faixa.u % 5 === 0) marca.dataset.marco = '1';
      num.appendChild(marca);

      const ocup = faixa.ocupante;
      if (!ocup) {
        trilha.appendChild(criar('div', 'nx-rack-vago'));
        return;
      }
      if (!ocup.primeiro) return;      // o bloco anterior já cobre este U
      const bloco = criar('button', 'nx-rack-eq');
      bloco.type = 'button';
      // A régua desce e o número sobe: a linha do grid é contada do topo,
      // o U é contado do chão. É a conversão que faz a tela bater com a
      // etiqueta do fabricante.
      const linhaTopo = r.us - (ocup.base_u + ocup.altura_u - 1) + 1;
      bloco.style.gridRow = `${linhaTopo} / span ${ocup.altura_u}`;
      bloco.style.setProperty('--nx-classe', COR_CLASSE[ocup.classe] || COR_CLASSE.generico);
      bloco.appendChild(iconeSvg(ocup.classe));
      bloco.appendChild(criar('b', '', ocup.nome));
      bloco.appendChild(criar('span', '',
        `U${ocup.base_u}${ocup.altura_u > 1 ? `–${ocup.base_u + ocup.altura_u - 1}` : ''}`
        + (ocup.ip ? ` · ${ocup.ip}` : '')));
      bloco.title = `${ocup.nome}\nU${ocup.base_u} · ${ocup.altura_u}U`
        + (ocup.ip ? `\n${ocup.ip}` : '');
      bloco.onclick = () => {
        const noId = `i${ocup.item_id}`;
        if (porId.has(noId)) {
          fecharModal('modalRack');
          selecionar(noId, false);
          centralizarEm(noId);
        } else {
          avisar(`${ocup.nome} está fora do escopo carregado.`, 'ruim');
        }
      };
      trilha.appendChild(bloco);
    });
    el.rackRegua.replaceChildren(num, trilha);

    el.rackSemPos.replaceChildren();
    el.rackSemPosBloco.hidden = !dados.sem_posicao.length;
    dados.sem_posicao.forEach((s) => {
      const linha = criar('div', 'nx-escolha');
      linha.appendChild(criar('span', '', s.nome));
      linha.appendChild(criar('span', 'nx-conta', `${s.altura_u}U`));
      el.rackSemPos.appendChild(linha);
    });
  } catch (erro) { avisar(erro.message, 'ruim'); }
}

/* ── Ligar cabo: escolher destino ──────────────────────────────────
   Dois passos num modal só (equipamento → porta), porque escolher a
   porta antes de saber o equipamento não faz sentido e dois modais
   encadeados perdem o contexto de onde o cabo começou. */
function abrirLigacao() {
  if (!portaSel) return;
  const p = portaPorId(portaSel);
  abrirModal('modalCabo');
  texto(el.caboOrigemRot, `${dispAtual.nome} · ${p ? p.rotulo : ''}`);
  el.caboBusca.value = '';
  el.caboLista.replaceChildren();
  el.caboPortas.replaceChildren();
  el.caboPortasBloco.hidden = true;
  el.caboMeio.value = p ? p.meio : 'utp';
  caboOrigem = { portaId: portaSel, itemId: dispAtual.item_id };
  montarCandidatos('');
  el.caboBusca.focus();
}

function montarCandidatos(termo) {
  const t = (termo || '').toLowerCase();
  el.caboLista.replaceChildren();
  const candidatos = grafo.nos
    .filter((n) => n.item_id !== caboOrigem.itemId)
    .filter((n) => !t || `${n.nome} ${n.localidade} ${n.ip}`.toLowerCase().includes(t))
    .slice(0, 60);
  if (!candidatos.length) {
    el.caboLista.appendChild(criar('div', 'nx-escolha', 'Nenhum equipamento no escopo atual.'));
    return;
  }
  candidatos.forEach((n) => {
    const b = criar('button', 'nx-escolha');
    b.type = 'button';
    b.appendChild(criar('span', '', n.nome));
    b.appendChild(criar('span', 'nx-conta', n.portas_total ? `${n.portas_livres} livres` : 'sem portas'));
    b.onclick = () => escolherDestino(n);
    el.caboLista.appendChild(b);
  });
}

async function escolherDestino(no) {
  el.caboPortasBloco.hidden = false;
  texto(el.caboDestinoRot, no.nome);
  el.caboPortas.replaceChildren();
  el.caboSemPorta.onclick = () => confirmarCabo({ destino_item: no.item_id });

  try {
    const dados = await pedir(`${ENLACE_URL}?item=${encodeURIComponent(no.item_id)}`);
    const portas = dados.portas || [];
    if (!portas.length) {
      el.caboPortas.appendChild(criar('div', 'nx-escolha',
        'Este equipamento não tem portas cadastradas. Use "ligar sem porta" para registrar só o destino.'));
      return;
    }
    portas.forEach((p) => {
      const b = criar('button', 'nx-escolha');
      b.type = 'button';
      b.disabled = !p.livre;
      b.appendChild(criar('span', '', p.rotulo));
      b.appendChild(criar('span', 'nx-conta', p.livre ? 'livre' : `ocupada → ${p.destino}`));
      b.onclick = () => confirmarCabo({ destino_porta: p.id });
      el.caboPortas.appendChild(b);
    });
  } catch (erro) { avisar(erro.message, 'ruim'); }
}

async function confirmarCabo(destino) {
  try {
    const r = await enviar(ENLACE_URL, {
      porta: caboOrigem.portaId, meio: el.caboMeio.value, ...destino,
    });
    if (!r.ok) throw new Error(r.erro || 'Falha ao ligar o cabo.');
    fecharModal('modalCabo');
    dispAtual = { ...dispAtual, ...r };
    montarPortas(dispAtual);
    avisar('Cabo registrado nas duas pontas.', 'ok');
    carregar(false);
  } catch (erro) { avisar(erro.message, 'ruim'); }
}

/* Modo cabo direto na prancha: clica no nó de origem, clica no de
   destino. Só funciona quando a origem tem porta livre — e, se não
   tiver, a tela diz isso em vez de abrir um modal vazio. */
async function clicarNoModoCabo(noId) {
  const no = porId.get(noId);
  if (!no) return;
  if (!caboOrigem) {
    if (!no.portas_livres) {
      avisar(`${no.nome} não tem porta livre cadastrada. Gere as portas na ficha primeiro.`, 'ruim');
      return;
    }
    try {
      const dados = await pedir(`${ENLACE_URL}?item=${encodeURIComponent(no.item_id)}`);
      const livre = (dados.portas || []).find((p) => p.livre);
      if (!livre) { avisar('Sem porta livre neste equipamento.', 'ruim'); return; }
      caboOrigem = { portaId: livre.id, itemId: no.item_id, rotulo: livre.rotulo };
      texto(el.lerModo, `Cabo de ${no.nome} (${livre.rotulo}) — clique no destino`);
    } catch (erro) { avisar(erro.message, 'ruim'); }
    return;
  }
  if (no.item_id === caboOrigem.itemId) { avisar('Escolha outro equipamento como destino.', 'ruim'); return; }
  try {
    const dados = await pedir(`${ENLACE_URL}?item=${encodeURIComponent(no.item_id)}`);
    const livre = (dados.portas || []).find((p) => p.livre);
    const corpo = livre ? { destino_porta: livre.id } : { destino_item: no.item_id };
    const r = await enviar(ENLACE_URL, { porta: caboOrigem.portaId, meio: 'utp', ...corpo });
    if (!r.ok) throw new Error(r.erro || 'Falha');
    avisar(livre
      ? `Cabo ligado em ${no.nome} (${livre.rotulo}).`
      : `Destino ${no.nome} registrado — ele não tem portas cadastradas.`, 'ok');
  } catch (erro) {
    avisar(erro.message, 'ruim');
  } finally {
    caboOrigem = null;
    trocarModo('selecao');
    carregar(false);
  }
}

function trocarModo(novo) {
  modo = novo;
  el.prancha.dataset.modo = novo;
  el.btnCabo.setAttribute('aria-pressed', novo === 'cabo' ? 'true' : 'false');
  if (novo !== 'cabo') caboOrigem = null;
  texto(el.lerModo, novo === 'cabo'
    ? 'Modo cabo: clique na origem e depois no destino · Esc sai'
    : '');
}

/* =========================================================
   Conferência técnica
   ========================================================= */
function montarAvisos() {
  el.avisos.replaceChildren();
  texto(el.avisosConta, String(grafo.avisos.length));
  if (!grafo.avisos.length) {
    el.avisos.appendChild(criar('p', 'nx-aviso-d', 'Nenhuma inconsistência encontrada no escopo atual.'));
    return;
  }
  grafo.avisos.forEach((a) => {
    const b = criar('button', 'nx-aviso');
    b.type = 'button';
    b.dataset.nivel = a.nivel;
    const corpo = criar('span');
    corpo.appendChild(criar('span', 'nx-aviso-t', a.titulo));
    corpo.appendChild(criar('span', 'nx-aviso-d', a.detalhe));
    b.appendChild(corpo);
    b.appendChild(criar('span', 'nx-conta', String(a.nos.length)));
    b.onclick = () => {
      selecao.clear();
      a.nos.forEach((id) => selecao.add(id));
      selecionar(null, true);
      if (a.nos.length) centralizarEm(a.nos[0]);
      avisar(`${a.nos.length} equipamento(s) destacado(s) no mapa.`);
    };
    el.avisos.appendChild(b);
  });
}

async function verImpacto() {
  const alvo = noFoco || [...selecao][0];
  if (!alvo) { avisar('Selecione um equipamento primeiro.', 'ruim'); return; }
  try {
    const p = paramsEscopo({ no: alvo });
    const dados = await pedir(`${IMPACTO_URL}?${p.toString()}`);
    const no = porId.get(alvo);
    abrirModal('modalImpacto');
    texto(el.impactoTitulo, no ? no.nome : alvo);
    texto(el.impactoTotal, String(dados.total));
    texto(el.impactoDiretos, String(dados.diretos));
    el.impactoLista.replaceChildren();
    if (!dados.isolados.length) {
      el.impactoLista.appendChild(criar('div', 'nx-escolha',
        'Nada depende exclusivamente deste equipamento no mapa atual.'));
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
   Barra de leitura
   ========================================================= */
function montarStatus() {
  const k = grafo.kpis || {};
  const visiveis = grafo.nos.filter(visivel).length;
  texto(el.lerNos, String(visiveis));
  texto(el.lerEnlaces, String(k.enlaces || 0));
  texto(el.lerPortas, `${k.portas_ativas || 0}/${k.portas_totais || 0}`);
  texto(el.lerOcup, `${k.ocupacao || 0}%`);
  texto(el.lerOnline, String(k.online || 0));
  texto(el.lerOffline, String(k.offline || 0));
  texto(el.lerSel, selecao.size ? `${selecao.size} selecionado(s)` : '');
  texto(el.lerSalvo, MAPA_ID
    ? (sujo ? 'alterações não salvas' : `v${versao} salva`)
    : 'visão corporativa (sem mapa salvo)');
}

/* =========================================================
   Gravação do mapa
   ========================================================= */
function marcarSujo() {
  if (!MAPA_ID) {
    // Sem mapa salvo, mover nó é exploração: a posição vale para a
    // sessão e a barra diz que nada será gravado. Salvar em silêncio
    // num mapa que não existe é o pior dos dois.
    montarStatus();
    return;
  }
  sujo = true;
  montarStatus();
  clearTimeout(timerSalvar);
  timerSalvar = setTimeout(() => salvarMapa(false), 1400);
}

async function salvarMapa(novaVersao, descricao) {
  if (!MAPA_ID || salvando) return;
  salvando = true;
  try {
    const r = await enviar(SALVAR_URL, {
      mapa: MAPA_ID,
      versao,
      layout,
      nova_versao: !!novaVersao,
      descricao: descricao || '',
    });
    if (r.conflito) {
      avisar(`Este mapa foi alterado por ${r.editado_por} (versão ${r.versao_atual}). `
        + 'Recarregue a página para não sobrescrever o trabalho dele.', 'ruim');
      return;
    }
    if (!r.ok) throw new Error(r.erro || 'Falha ao salvar.');
    versao = r.versao;
    sujo = false;
    montarStatus();
    if (novaVersao) avisar(`Versão ${versao} gravada no histórico.`, 'ok');
  } catch (erro) {
    avisar(`Não foi possível salvar: ${erro.message}`, 'ruim');
  } finally {
    salvando = false;
  }
}

async function autoArranjo(agrupar) {
  layout.agrupar = agrupar || layout.agrupar;
  const p = paramsEscopo({ arranjo: '1', agrupar: layout.agrupar, status: '0' });
  try {
    const dados = await pedir(`${GRAFO_URL}?${p.toString()}`);
    if (!dados.ok || !dados.arranjo) throw new Error('Arranjo indisponível.');
    arranjo = dados.arranjo;
    layout.nos = {};
    layout.zonas = dados.arranjo.zonas || [];
    layout.canvas = dados.arranjo.canvas || layout.canvas;
    Object.entries(arranjo.nos).forEach(([id, pos]) => { layout.nos[id] = { ...pos }; });
    desenhar();
    enquadrar();
    marcarSujo();
    avisar(`Mapa rearranjado por ${layout.agrupar === 'papel' ? 'camada da rede' : 'localidade'}.`, 'ok');
  } catch (erro) { avisar(erro.message, 'ruim'); }
}

async function abrirHistorico() {
  if (!MAPA_ID) { avisar('O histórico existe para mapa salvo. Crie um mapa primeiro.', 'ruim'); return; }
  abrirModal('modalHistorico');
  el.histLista.replaceChildren();
  try {
    const dados = await pedir(`${HISTORICO_URL}?mapa=${MAPA_ID}`);
    if (!dados.historico.length) {
      el.histLista.appendChild(criar('div', 'nx-escolha', 'Nenhuma versão gravada ainda.'));
      return;
    }
    dados.historico.forEach((h) => {
      const b = criar('button', 'nx-escolha');
      b.type = 'button';
      const corpo = criar('span');
      corpo.appendChild(criar('span', 'nx-aviso-t', `v${h.versao} · ${h.salvo_em}`));
      corpo.appendChild(criar('span', 'nx-aviso-d',
        `${h.salvo_por} · ${h.nos} nó(s)${h.descricao ? ` · ${h.descricao}` : ''}`));
      b.appendChild(corpo);
      b.appendChild(criar('span', 'nx-conta', 'restaurar'));
      b.onclick = async () => {
        if (!confirm(`Restaurar a versão ${h.versao}? A geometria atual vai para o histórico antes.`)) return;
        try {
          const r = await enviar(`${HISTORICO_URL}?mapa=${MAPA_ID}&restaurar=${h.id}`, {});
          if (!r.ok) throw new Error(r.erro || 'Falha');
          layout = normalizarLayout(r.layout);
          versao = r.versao;
          sujo = false;
          fecharModal('modalHistorico');
          desenhar();
          enquadrar();
          montarStatus();
          avisar(`Versão ${h.versao} restaurada.`, 'ok');
        } catch (erro) { avisar(erro.message, 'ruim'); }
      };
      el.histLista.appendChild(b);
    });
  } catch (erro) { avisar(erro.message, 'ruim'); }
}

async function criarMapa() {
  const nome = el.novoNome.value.trim();
  if (!nome) { avisar('Informe o nome do mapa.', 'ruim'); return; }
  try {
    const r = await enviar(CRIAR_URL, {
      nome,
      descricao: el.novoDesc.value,
      localidade: el.localidade.value || null,
      area: el.area.value || '',
      layout,
    });
    if (!r.ok) throw new Error(r.erro || 'Falha');
    window.location.href = r.url;
  } catch (erro) { avisar(erro.message, 'ruim'); }
}

/* Exportação do desenho em JSON: é o que permite levar o mapa para
   outra instalação e o que dá ao gestor um anexo auditável do que
   estava na tela no dia da apresentação. O Excel sai do servidor,
   onde o grafo é apurado. */
function exportarJson() {
  const pacote = {
    emitido_em: new Date().toISOString(),
    mapa: MAPA_NOME,
    escopo: { localidade: el.localidade.value || null, area: el.area.value || null },
    kpis: grafo.kpis,
    nos: grafo.nos.map((n) => ({ ...n, geometria: posicaoDe(n) })),
    enlaces: grafo.enlaces,
    avisos: grafo.avisos,
  };
  const blob = new Blob([JSON.stringify(pacote, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `mapa_rede_${new Date().toISOString().slice(0, 10)}.json`;
  a.click();
  URL.revokeObjectURL(a.href);
}

/* =========================================================
   Modais e menus
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

/* =========================================================
   Teclado
   ========================================================= */
function ligarTeclado() {
  document.addEventListener('keydown', (ev) => {
    const emCampo = /^(INPUT|TEXTAREA|SELECT)$/.test(ev.target.tagName);
    if (ev.code === 'Space' && !emCampo) { espacoApertado = true; }

    if (ev.key === 'Escape') {
      document.querySelectorAll('.nx-modal:not([hidden])').forEach((m) => { m.hidden = true; });
      if (modo === 'cabo') trocarModo('selecao');
      return;
    }
    if (emCampo) return;

    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 's') {
      ev.preventDefault();
      salvarMapa(true, 'Gravado pelo atalho');
      return;
    }
    // Setas andam pelo PAINEL quando há porta selecionada: conferir 48
    // portas uma a uma com o mouse é 48 miradas; com a seta é um dedo.
    if (portaSel != null && dispAtual
        && /^Arrow(Left|Right|Up|Down)$/.test(ev.key)) {
      const todas = [...(dispAtual.acessos || []), ...(dispAtual.uplinks || [])];
      const i = todas.findIndex((p) => p.id === portaSel);
      if (i >= 0) {
        ev.preventDefault();
        // Esquerda/direita andam de coluna (duas portas), cima/baixo
        // trocam de fileira — é o movimento do painel, não da lista.
        const salto = (ev.key === 'ArrowLeft' ? -2 : ev.key === 'ArrowRight' ? 2
          : ev.key === 'ArrowUp' ? -1 : 1);
        const alvo = todas[Math.max(0, Math.min(todas.length - 1, i + salto))];
        if (alvo) { portaSel = alvo.id; montarPortas(dispAtual); }
        return;
      }
    }
    if (ev.key === 'i' || ev.key === 'I') { identificarPorta(); return; }
    if (ev.key === 'f' || ev.key === 'F') { enquadrar(); return; }
    if (ev.key === '+' || ev.key === '=') { zoomPara(1.2); return; }
    if (ev.key === '-' || ev.key === '_') { zoomPara(1 / 1.2); return; }
    if (ev.key === '0') { vista.z = 1; aplicarVista(); return; }
    if (ev.key === 'c' || ev.key === 'C') { trocarModo(modo === 'cabo' ? 'selecao' : 'cabo'); return; }
    if (ev.key === 'a' && (ev.ctrlKey || ev.metaKey)) {
      ev.preventDefault();
      grafo.nos.filter(visivel).forEach((n) => selecao.add(n.id));
      selecionar(null, true);
      return;
    }
    if (ev.key === 'Delete' || ev.key === 'Backspace') {
      if (!selecao.size) return;
      // Delete tira do DESENHO, nunca do inventário: apagar um ativo
      // daqui seria excluir equipamento pelo mapa, o que nenhuma tela
      // deste módulo pode fazer.
      selecao.forEach((id) => { layout.nos[id] = { ...(layout.nos[id] || {}), oculto: true }; });
      marcarSujo();
      desenhar();
      avisar(`${selecao.size} nó(s) retirado(s) do desenho. O equipamento segue no inventário.`);
      selecao.clear();
      selecionar(null, false);
    }
  });
  document.addEventListener('keyup', (ev) => {
    if (ev.code === 'Space') espacoApertado = false;
  });
}

/* =========================================================
   Ligações do DOM
   ========================================================= */
function cachearElementos() {
  [
    'prancha', 'mundo', 'fios', 'nos', 'zonas', 'laco', 'vazio',
    'mini', 'miniNos', 'miniVista',
    'localidade', 'area', 'terminais', 'buscaInv', 'inventario', 'invConta',
    'camadasClasse', 'camadasSit', 'buscaMapa',
    'insp', 'inspVazio', 'inspFicha', 'inspMulti', 'inspEnlace',
    'fichaNome', 'fichaSub', 'fichaKv', 'fichaPapel', 'fichaIp', 'fichaPrtg',
    'fichaObs', 'fichaModelo', 'prtgBloco', 'prtgBusca', 'prtgLista',
    'faceChassi', 'faceReg', 'portasTotal', 'portasAtivas',
    'portasLivres', 'portasPoe', 'portasOcup', 'portaDet', 'portaTitulo', 'portaSit',
    'portaRotulo', 'portaTipo', 'portaStatus', 'portaVel', 'portaMeio',
    'portaVlan', 'portaPoe', 'portaDesc', 'portaDestino', 'portaLigar', 'portaSoltar',
    'portaRota', 'portaRotaLer', 'portaIdentificar',
    'vizinhos', 'vizinhosBloco', 'vizinhosConta', 'semRota', 'semRotaConta',
    'fichaRack', 'fichaRackU', 'fichaRackAlt', 'fichaLat', 'fichaLon', 'btnElevacao',
    'guias',
    'rackTitulo', 'rackSub', 'rackUs', 'rackOcup', 'rackLivres', 'rackRegua',
    'rackSemPos', 'rackSemPosBloco',
    'avisos', 'avisosConta', 'multiConta', 'enlaceKv', 'enlaceSoltar',
    'lerNos', 'lerEnlaces', 'lerPortas', 'lerOcup', 'lerOnline', 'lerOffline',
    'lerZoom', 'lerSel', 'lerSalvo', 'lerModo',
    'btnCabo', 'toast',
    'caboOrigemRot', 'caboBusca', 'caboLista', 'caboPortas', 'caboPortasBloco',
    'caboDestinoRot', 'caboMeio', 'caboSemPorta',
    'impactoTitulo', 'impactoTotal', 'impactoDiretos', 'impactoJa', 'impactoLista',
    'histLista', 'novoNome', 'novoDesc',
  ].forEach((id) => { el[id] = $(id); });
}

function ligarControles() {
  // Escopo
  [el.localidade, el.area, el.terminais].forEach((c) => {
    if (c) c.addEventListener('change', () => { arranjo = null; carregar(true); pedirInventario(); });
  });

  el.buscaInv.addEventListener('input', pedirInventario);
  el.buscaMapa.addEventListener('input', () => {
    filtros.busca = el.buscaMapa.value.trim().toLowerCase();
    aplicarFiltros();
  });

  // Trilhos
  document.querySelectorAll('[data-trilho]').forEach((b) => {
    b.addEventListener('click', () => {
      const trilho = $(b.dataset.trilho);
      const aberto = trilho.dataset.aberto !== '0';
      trilho.dataset.aberto = aberto ? '0' : '1';
      b.setAttribute('aria-expanded', aberto ? 'false' : 'true');
      requestAnimationFrame(desenharVistaMini);
    });
  });

  // Ferramentas
  $('btnEnquadrar').onclick = enquadrar;
  $('btnMais').onclick = () => zoomPara(1.2);
  $('btnMenos').onclick = () => zoomPara(1 / 1.2);
  $('btnUm').onclick = () => { vista.z = 1; aplicarVista(); };
  $('btnMalha').onclick = (ev) => {
    const ligada = el.prancha.dataset.malha !== '0';
    el.prancha.dataset.malha = ligada ? '0' : '1';
    ev.currentTarget.setAttribute('aria-pressed', ligada ? 'false' : 'true');
  };
  $('btnRotulos').onclick = (ev) => {
    const ligados = el.prancha.dataset.rotulos !== '0';
    el.prancha.dataset.rotulos = ligados ? '0' : '1';
    ev.currentTarget.setAttribute('aria-pressed', ligados ? 'false' : 'true');
  };
  el.btnCabo.onclick = () => trocarModo(modo === 'cabo' ? 'selecao' : 'cabo');
  $('btnMiniAlterna').onclick = () => { el.mini.hidden = !el.mini.hidden; if (!el.mini.hidden) desenharMini(); };

  $('btnPaineis').onclick = (ev) => {
    const ligados = el.prancha.dataset.paineis !== '0';
    el.prancha.dataset.paineis = ligados ? '0' : '1';
    ev.currentTarget.setAttribute('aria-pressed', ligados ? 'false' : 'true');
    // A miniatura muda a ALTURA do nó, e a altura entra no roteamento do
    // cabo: sem redesenhar, a ponta do fio fica descolada da caixa.
    desenhar();
  };

  // Menus
  $('mnArranjoArvore').onclick = () => autoArranjo('arvore');
  $('mnArranjoPapel').onclick = () => autoArranjo('papel');
  $('mnArranjoLocal').onclick = () => autoArranjo('localidade');
  $('mnAlinharEsq').onclick = () => alinhar('esq');
  $('mnAlinharCentroX').onclick = () => alinhar('centroX');
  $('mnAlinharDir').onclick = () => alinhar('dir');
  $('mnAlinharTopo').onclick = () => alinhar('topo');
  $('mnAlinharCentroY').onclick = () => alinhar('centroY');
  $('mnAlinharBase').onclick = () => alinhar('base');
  $('mnDistribuirX').onclick = () => distribuir('x');
  $('mnDistribuirY').onclick = () => distribuir('y');
  $('mnSalvar').onclick = () => salvarMapa(false);
  $('mnVersao').onclick = () => {
    const d = prompt('Descreva esta versão (opcional):', '');
    if (d === null) return;
    salvarMapa(true, d);
  };
  $('mnHistorico').onclick = abrirHistorico;
  $('mnNovo').onclick = () => { abrirModal('modalNovo'); el.novoNome.focus(); };
  $('mnJson').onclick = exportarJson;
  $('mnImprimir').onclick = () => window.print();
  $('mnExcel').onclick = () => {
    const p = paramsEscopo({});
    window.location.href = `${EXPORTAR_URL}?${p.toString()}`;
  };
  $('mnImpacto').onclick = verImpacto;

  // Inspetor — ficha
  $('btnFichaSalvar').onclick = salvarDispositivo;
  $('btnGerar').onclick = () => gerarPortas(false);
  $('btnRegerar').onclick = () => gerarPortas(true);
  $('btnPortaNova').onclick = novaPorta;
  $('btnVerImpacto').onclick = verImpacto;

  el.btnElevacao.onclick = abrirElevacao;

  // Inspetor — porta
  el.portaLigar.onclick = abrirLigacao;
  el.portaSoltar.onclick = soltarPorta;
  el.portaIdentificar.onclick = identificarPorta;
  $('btnPortaSalvar').onclick = salvarPorta;
  $('btnPortaExcluir').onclick = excluirPorta;
  $('btnAbrirPlanta').onclick = () => {
    const p = new URLSearchParams();
    if (el.localidade.value) p.set('localidade', el.localidade.value);
    if (el.area.value) p.set('area', el.area.value);
    const q = p.toString();
    window.location.href = q ? `${PLANTA_URL}?${q}` : PLANTA_URL;
  };

  // Painel de portas: delegação, porque o painel é redesenhado a cada
  // gravação e listener por botão vazaria a cada rodada.
  el.faceChassi.addEventListener('click', (ev) => {
    const b = ev.target.closest('.nx-face-porta');
    if (!b) return;
    portaSel = Number(b.dataset.porta);
    montarPortas(dispAtual);
  });

  // Abas do inspetor
  document.querySelectorAll('.nx-aba').forEach((aba) => {
    aba.addEventListener('click', () => {
      document.querySelectorAll('.nx-aba').forEach((a) => {
        const sel = a === aba;
        a.setAttribute('aria-selected', sel ? 'true' : 'false');
        $(a.dataset.painel).hidden = !sel;
      });
    });
  });

  // Inventário: arrastar e clicar
  el.inventario.addEventListener('dragstart', (ev) => {
    const linha = ev.target.closest('.nx-lib');
    if (!linha) return;
    ev.dataTransfer.setData('text/x-item', linha.dataset.item);
    ev.dataTransfer.effectAllowed = 'copy';
  });
  el.inventario.addEventListener('click', (ev) => {
    const linha = ev.target.closest('.nx-lib');
    if (!linha) return;
    const no = grafo.nos.find((n) => String(n.item_id) === linha.dataset.item);
    if (no && layout.nos[no.id] && !layout.nos[no.id].oculto) {
      selecionar(no.id, false);
      centralizarEm(no.id);
    } else {
      const r = el.prancha.getBoundingClientRect();
      const p = paraMundo(r.left + r.width / 2, r.top + r.height / 3);
      porNoMapa(linha.dataset.item, Math.round(p.x / PASSO) * PASSO, Math.round(p.y / PASSO) * PASSO);
    }
  });

  // Modais
  document.querySelectorAll('[data-fechar]').forEach((b) => {
    b.addEventListener('click', () => fecharModal(b.dataset.fechar));
  });
  el.caboBusca.addEventListener('input', () => montarCandidatos(el.caboBusca.value));
  $('btnNovoCriar').onclick = criarMapa;

  // Minimapa: clicar move a vista
  el.mini.addEventListener('click', (ev) => {
    const esc = parseFloat(el.mini.dataset.esc || '0');
    if (!esc) return;
    const r = el.mini.getBoundingClientRect();
    const x0 = parseFloat(el.mini.dataset.x0 || '0');
    const y0 = parseFloat(el.mini.dataset.y0 || '0');
    const mundoX = (ev.clientX - r.left) / esc + x0;
    const mundoY = (ev.clientY - r.top) / esc + y0;
    const rp = el.prancha.getBoundingClientRect();
    vista.x = rp.width / 2 - mundoX * vista.z;
    vista.y = rp.height / 2 - mundoY * vista.z;
    aplicarVista();
  });

  // Sair com alteração pendente
  window.addEventListener('beforeunload', (ev) => {
    if (sujo && MAPA_ID) { ev.preventDefault(); ev.returnValue = ''; }
  });

  // Reenquadra o minimapa quando a janela muda
  new ResizeObserver(() => { desenharVistaMini(); }).observe(el.prancha);

  // Pausa o relógio de status quando a aba está escondida: pedir PRTG
  // para uma aba que ninguém olha é carga sem leitor (mesmo princípio
  // do `visibilitychange` das animações do sistema).
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && PRTG_OK) carregar(false);
  });
}

/* O status do PRTG é cacheado 30s no servidor; pedir de 45 em 45
   segundos mantém a tela viva sem multiplicar a carga. */
function ligarRelogio() {
  if (!PRTG_OK) return;
  setInterval(() => {
    if (document.visibilityState === 'visible' && !salvando) carregar(false);
  }, 45000);
}

function init() {
  cachearElementos();
  if (!el.prancha) return;
  ligarControles();
  ligarPrancha();
  ligarMenus();
  ligarTeclado();
  ligarBuscaPrtg();
  limparInspetor();
  carregar(true).then(() => { enquadrar(); pedirInventario(); });
  ligarRelogio();
}

init();
