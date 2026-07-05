/* ================================================================
   DADOS INICIAIS
   ================================================================ */

// Move o menu de contexto para body: position:fixed é relativo ao viewport
// apenas quando nenhum ancestral possui transform. O .main-container aplica
// transform via animação appleFade, quebrando a referência do viewport.
document.body.appendChild(document.getElementById('ctxMenu'));

const LAYOUT_INICIAL      = JSON.parse(document.getElementById('__layout_data').textContent);
const SALVAR_URL          = "x";
const CHECK_VERSION_URL   = "x";
const HISTORICO_URL       = "x";
const RESTAURAR_BASE      = `/plantas/x/restaurar/`;
const ITEM_SEARCH_URL     = "x";
const PRTG_SEARCH_URL     = "x";
const CSRF_TOKEN          = "x";
const BG_URL              = "xxx" || null;
const PRTG_OK             = true;

let layoutVersion   = 0;
let conflitoPausado = false;

/* ================================================================
   TIPOS DE ELEMENTOS
   ================================================================ */
const TIPOS = {
  camera:       { label:"Câmera",       emoji:"📷", cor:"#ef4444" },
  access_point: { label:"Access Point", emoji:"📡", cor:"#0ea5e9" },
  switch:       { label:"Switch",       emoji:"🔀", cor:"#2563eb" },
  rack:         { label:"Rack",         emoji:"🗄️",  cor:"#475569" },
  desktop:      { label:"Desktop",      emoji:"🖥️",  cor:"#10b981" },
  impressora:   { label:"Impressora",   emoji:"🖨️",  cor:"#f97316" },
  nobreak:      { label:"Nobreak",      emoji:"⚡",  cor:"#f59e0b" },
  servidor:     { label:"Servidor",     emoji:"☁️",  cor:"#8b5cf6" },
  ponto_rede:   { label:"Ponto de Rede",emoji:"📍",  cor:"#6366f1" },
  texto:        { label:"Texto",        emoji:"✏️",  cor:"#1d1d1f" },
  quadro:       { label:"Quadro/Área",  emoji:"□",   cor:"#2563eb" },
  circulo:      { label:"Círculo/Zona", emoji:"○",   cor:"#8b5cf6" },
  linha:        { label:"Linha",        emoji:"─",   cor:"#64748b" },
};

const LINE_CFG = {
  network:    { cor:"#2563eb", dash:[],     width:2, label:"Rede",       arrow:true  },
  fiber:      { cor:"#8b5cf6", dash:[8,4],  width:2, label:"Fibra",      arrow:true  },
  power:      { cor:"#f97316", dash:[],     width:2, label:"Energia",    arrow:false },
  dependencia:{ cor:"#10b981", dash:[],     width:2.5, label:"Hierarquia", arrow:true },
};

const FONT_MAP = {
  system: '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
  mono:   '"SF Mono", "Consolas", "Courier New", monospace',
  serif:  '"Georgia", "Times New Roman", serif',
};

const IS_FORMA = t => t === 'quadro' || t === 'circulo' || t === 'linha';

/* ================================================================
   ESTADO
   ================================================================ */
let state = { elements:[], connections:[], canvas:{width:1400, height:900} };

// Seleção
let selectionIds   = new Set();
let rubberBand     = null;   // {x0,y0,x1,y1} coords de tela
let rubberStart    = null;   // {cx,cy} ponto inicial do rubber band

// Drag de elementos
let isDragging     = false;
let dragOffsets    = [];     // [{id,dx,dy}]
let dragStarted    = false;  // Distingue click de drag
let dragStartCx    = 0;      // posição de tela no início do drag
let dragStartCy    = 0;

// Redimensionamento
let resizeHandle   = null;   // nome do handle ativo
let resizeEl       = null;   // elemento sendo redimensionado (referência)
let resizeSnap     = null;   // snapshot {x,y,w,h} antes do resize
let resizeMouse0   = null;   // {wx,wy} no início do resize

// Pan
let isPanning      = false;
let panStart       = null;
let panX = 0, panY = 0, zoom = 1;

// Modo linha
let modoLinha      = null;
let lineStart      = null;
let linePreviewMx  = null, linePreviewMy = null;

// Hover / seleção de conexão
let hoverId        = null;
let hoverCnId      = null;
let selectedCnId   = null;   // conexão selecionada (Delete remove)

// Drag de endpoint de conexão
let draggingCnEndpoint = null; // {cn, which:'from'|'to'}

// Drag de segmento de conexão ortogonal
let draggingOrthoSeg   = null; // {cn, segIdx}

// Fase 1–2 extras
let snapGrid       = false;  // Grid Snap toggle
let clipboard      = null;   // Ctrl+C buffer
let spacebarDown   = false;  // Spacebar pan
let prtgEditorMap  = {};     // objid → device (para colorir no editor)

// Smart guides
let dragGuides     = []; // {type:'v'|'h'|'distH'|'distV', ...} — world coords

// Misc
let bgImage        = null;
let bgVisible      = false;   // fundo oculto por padrão ao abrir o editor
let nextGroupId    = 1;       // contador incremental para IDs de grupo
let undoStack      = [];
let redoStack      = [];
let saveTimer      = null;
let paletteDrag    = null;

const canvas     = document.getElementById('plantaCanvas');
const ctx        = canvas.getContext('2d');
const canvasWrap = document.getElementById('canvasWrap');

/* Dimensões LÓGICAS da viewport (px CSS) e densidade de pixels.
   O backing store do canvas é escalado por DPR para nitidez retina;
   toda a matemática de render/coordenadas usa VW/VH (lógico). */
let VW = 0, VH = 0, DPR = 1;

/* ================================================================
   INICIALIZAÇÃO
   ================================================================ */
function init() {
  state.elements    = LAYOUT_INICIAL.elements    || [];
  state.connections = LAYOUT_INICIAL.connections || [];
  state.canvas      = LAYOUT_INICIAL.canvas      || {width:1400,height:900};

  if (BG_URL) {
    bgImage = new Image();
    bgImage.onload = render;
    bgImage.src = BG_URL;
    // Fundo existe mas começa oculto — usuário ativa com o botão de olho
    document.getElementById('btnLimparFundo').style.display = '';
    document.getElementById('btnToggleFundo').style.display = '';
  }
  // Inicializar nextGroupId a partir dos grupos já salvos
  state.elements.forEach(el => {
    if (el.groupId) {
      const n = parseInt(String(el.groupId).slice(1));
      if (!isNaN(n) && n >= nextGroupId) nextGroupId = n + 1;
    }
  });
  resizeCanvas();
  // Abre já enquadrado no conteúdo existente (nunca ampliando além de 100%),
  // para o usuário ver o projeto inteiro sem precisar dar zoom out manual.
  if (state.elements.length) fitToScreen(1);
  pushUndo();
}
function resizeCanvas() {
  const r = canvasWrap.getBoundingClientRect();
  DPR = Math.max(1, Math.min(3, window.devicePixelRatio || 1));
  VW  = Math.max(1, Math.round(r.width));
  VH  = Math.max(1, Math.round(r.height));
  // Backing store em pixels físicos → nitidez retina; CSS mantém tamanho lógico
  canvas.width        = Math.round(VW * DPR);
  canvas.height       = Math.round(VH * DPR);
  canvas.style.width  = VW + 'px';
  canvas.style.height = VH + 'px';
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = 'high';
  render();
}
new ResizeObserver(resizeCanvas).observe(canvasWrap);

/* ================================================================
   HELPERS DE FONTE
   ================================================================ */
function elFont(el, baseSize, weight) {
  const fam  = FONT_MAP[el.fontFamily] || FONT_MAP.system;
  const size = Math.max(6, el.fontSize || baseSize);
  const bold = el.fontBold   ? 'bold'   : (weight || '600');
  const ital = el.fontItalic ? 'italic' : 'normal';
  return `${ital} ${bold} ${size}px ${fam}`;
}

/* ================================================================
   TRANSFORMAÇÕES DE COORDENADAS
   ================================================================ */
// Tela → Mundo
function toWorld(cx, cy) {
  return {
    x: (cx - panX - VW/2)  / zoom + state.canvas.width/2,
    y: (cy - panY - VH/2) / zoom + state.canvas.height/2,
  };
}
// Mundo → Tela
function toScreen(wx, wy) {
  return {
    x: (wx - state.canvas.width/2)  * zoom + panX + VW/2,
    y: (wy - state.canvas.height/2) * zoom + panY + VH/2,
  };
}

/* ================================================================
   RENDERIZAÇÃO
   ================================================================ */
function render() {
  // Base: 1 unidade lógica = DPR pixels físicos (nitidez retina em toda a cena)
  ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  ctx.clearRect(0, 0, VW, VH);
  ctx.save();

  // Fundo pontilhado
  ctx.fillStyle = '#eff0f5';
  ctx.fillRect(0, 0, VW, VH);
  drawGrid();

  // Transformação global
  ctx.translate(panX + VW/2, panY + VH/2);
  ctx.scale(zoom, zoom);
  ctx.translate(-state.canvas.width/2, -state.canvas.height/2);

  // Imagem de fundo (só exibe se o toggle estiver ativo)
  if (bgVisible && bgImage && bgImage.complete) {
    ctx.globalAlpha = 0.5;
    ctx.drawImage(bgImage, 0, 0, state.canvas.width, state.canvas.height);
    ctx.globalAlpha = 1;
  }

  // Renderizar por ordem de camada (zIndex)
  const sortedEls = [...state.elements].sort((a,b) => (a.zIndex??0) - (b.zIndex??0));

  // Formas (backgrounds) — camadas baixas primeiro
  sortedEls.filter(e => IS_FORMA(e.type)).forEach(el => {
    if (el.type === 'linha') drawElementLinha(el); else drawElementShape(el);
  });

  // Conexões (sempre atrás dos cards)
  state.connections.forEach(drawConnection);

  // Preview de linha em progresso / drag de endpoint
  drawLinePreview();
  drawCnEndpointPreview();

  // Cards de equipamento/texto — em ordem de camada
  sortedEls.filter(e => !IS_FORMA(e.type)).forEach(drawElementCard);

  // Âncoras de conexão em modo linha
  if (modoLinha) {
    // Ports sutis em todos os elementos (para orientar onde conectar)
    state.elements.filter(e => e.type !== 'linha').forEach(el => {
      if (el.id !== hoverId) drawAnchorHintsSutil(el);
    });
    // Ports evidentes no elemento sob o cursor
    const hoverEl = state.elements.find(e => e.id === hoverId);
    if (hoverEl) {
      const { x: _wx, y: _wy } = toWorld(linePreviewMx ?? 0, linePreviewMy ?? 0);
      const activeEdge = hoverId ? hitAnchorEdge(hoverEl, _wx, _wy) : null;
      drawAnchorHints(hoverEl, activeEdge);
    }
  }

  // Âncoras editáveis nos elementos da conexão selecionada
  if (selectedCnId) {
    const selCn = state.connections.find(c => c.id === selectedCnId);
    if (selCn) {
      const fromEl = state.elements.find(e => e.id === selCn.from);
      const toEl   = state.elements.find(e => e.id === selCn.to);
      if (fromEl) drawAnchorHints(fromEl, selCn.fromEdge);
      if (toEl)   drawAnchorHints(toEl,   selCn.toEdge);
    }
  }

  // Handles de redimensionamento
  if (selectionIds.size === 1) {
    const el = state.elements.find(e => selectionIds.has(e.id));
    if (el) drawResizeHandles(el);
  }

  ctx.restore();

  // Rubber band (sobre a transformação, em coordenadas de tela)
  drawRubberBand();

  // Smart guides (em cima de tudo, coordenadas de tela)
  drawGuides();

  // Minimapa + estado de onboarding
  drawMinimap();
  updateOnboard();
}

function drawGrid() {
  const step = 30 * zoom;
  const ox = ((panX + VW/2) % step + step) % step;
  const oy = ((panY + VH/2) % step + step) % step;
  ctx.save();
  ctx.fillStyle = 'rgba(0,0,0,.10)';
  for (let x = ox; x < VW; x += step) {
    for (let y = oy; y < VH; y += step) {
      ctx.beginPath();
      ctx.arc(x, y, Math.max(0.8, 1.2 * Math.min(zoom, 1)), 0, Math.PI * 2);
      ctx.fill();
    }
  }
  ctx.restore();
}

/* ── Formas (Quadro / Círculo) ───────────────────────────────────── */
function drawElementShape(el) {
  const cor       = el.color || (el.type==='circulo'?'#5856d6':'#0071e3');
  const borderCor = el.borderColor || cor;
  const opacity   = el.fillOpacity ?? 0.12;
  const bw        = (el.borderWidth ?? 2) / zoom;
  const isSel     = selectionIds.has(el.id);
  const isHov     = hoverId === el.id;
  const isLock    = !!el.locked;
  // borderStyle: 'solid' | 'dashed' | 'dotted' | null (default = solid)
  const bStyle    = el.borderStyle || 'solid';
  const getBorderDash = () => {
    if (isSel) return [];
    if (bStyle === 'dashed') return [6/zoom, 3/zoom];
    if (bStyle === 'dotted') return [2/zoom, 4/zoom];
    return []; // solid (default)
  };

  ctx.save();
  // Rotação em torno do centro do elemento
  const _rotS = el.rotation || 0;
  if (_rotS) {
    const _sw = el.width  || (el.type==='circulo'?120:200);
    const _sh = el.height || (el.type==='circulo'?120:140);
    ctx.translate(el.x + _sw/2, el.y + _sh/2);
    ctx.rotate(_rotS * Math.PI / 180);
    ctx.translate(-(el.x + _sw/2), -(el.y + _sh/2));
  }

  if (el.type === 'quadro') {
    const rawR = el.cornerRadius ?? 10;
    const r  = rawR / zoom;
    const w  = el.width  || 200;
    const h  = el.height || 140;

    // Sombra
    ctx.shadowColor   = hexAlpha(cor, isSel ? .28 : .12);
    ctx.shadowBlur    = (isSel ? 18 : 10) / zoom;
    ctx.shadowOffsetY = 4 / zoom;

    // Fill — cor única e plana (sem gradiente / sem barra de título)
    ctx.fillStyle = hexAlpha(cor, opacity);
    ctx.beginPath(); ctx.roundRect(el.x, el.y, w, h, r); ctx.fill();
    ctx.shadowColor = 'transparent'; ctx.shadowBlur = 0; ctx.shadowOffsetY = 0;

    // Borda
    ctx.strokeStyle = isSel ? borderCor : hexAlpha(borderCor, isHov ? 0.95 : 0.80);
    ctx.lineWidth   = (isSel ? bw * 1.8 : bw);
    ctx.setLineDash(getBorderDash());
    ctx.beginPath(); ctx.roundRect(el.x, el.y, w, h, r); ctx.stroke();
    ctx.setLineDash([]);

    // Label — texto simples no topo (sem cabeçalho)
    if (el.label) {
      ctx.font = elFont(el, 13, '800');
      ctx.fillStyle    = isSel ? cor : hexAlpha(cor, .95);
      ctx.textAlign    = 'left';
      ctx.textBaseline = 'top';
      const pad = 10/zoom;
      ctx.fillText(el.label, el.x + pad, el.y + pad);
    }
    if (isSel) drawSelectionOutline(el.x, el.y, w, h, r, cor);
    if (isLock) { ctx.font=`${9/zoom}px serif`; ctx.textAlign='right'; ctx.textBaseline='top'; ctx.fillText('🔒',el.x+w-5/zoom,el.y+5/zoom); }

  } else {
    // CÍRCULO
    const w  = el.width  || 120;
    const h  = el.height || 120;
    const cx = el.x + w/2, cy = el.y + h/2;

    ctx.shadowColor   = hexAlpha(cor, isSel ? .28 : .12);
    ctx.shadowBlur    = (isSel ? 18 : 10) / zoom;
    ctx.shadowOffsetY = 4 / zoom;

    // Fill — cor única e plana (sem gradiente radial)
    ctx.fillStyle = hexAlpha(cor, opacity);
    ctx.beginPath(); ctx.ellipse(cx, cy, w/2, h/2, 0, 0, Math.PI*2); ctx.fill();
    ctx.shadowColor = 'transparent'; ctx.shadowBlur = 0; ctx.shadowOffsetY = 0;

    ctx.strokeStyle = isSel ? borderCor : hexAlpha(borderCor, isHov ? 0.95 : 0.80);
    ctx.lineWidth   = isSel ? bw * 1.8 : bw;
    ctx.setLineDash(getBorderDash());
    ctx.beginPath(); ctx.ellipse(cx, cy, w/2, h/2, 0, 0, Math.PI*2); ctx.stroke();
    ctx.setLineDash([]);

    if (el.label) {
      ctx.font = elFont(el, 13, '800');
      ctx.fillStyle    = isSel ? cor : hexAlpha(cor, .90);
      ctx.textAlign    = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(el.label, cx, cy);
    }
    if (isLock) { ctx.font=`${9/zoom}px serif`; ctx.textAlign='right'; ctx.textBaseline='top'; ctx.fillText('🔒',el.x+w-5/zoom,el.y+5/zoom); }
  }
  ctx.restore();
}

/* ── Linha como elemento de forma ───────────────────────────────────── */
function drawArrowHead(x, y, angle, size, fillColor) {
  const al = size / zoom;
  ctx.fillStyle = fillColor;
  ctx.beginPath();
  ctx.moveTo(x, y);
  ctx.lineTo(x - al*Math.cos(angle - 0.40), y - al*Math.sin(angle - 0.40));
  ctx.lineTo(x - al*Math.cos(angle + 0.40), y - al*Math.sin(angle + 0.40));
  ctx.closePath(); ctx.fill();
}

function drawElementLinha(el) {
  const x2 = el.x2 ?? el.x + 150;
  const y2 = el.y2 ?? el.y;
  const sw  = (el.strokeWidth || 2) / zoom;
  const cor = el.color || '#6e6e73';
  const isSel = selectionIds.has(el.id);
  const isHov = hoverId === el.id;
  const drawColor = isSel ? '#ff3b30' : cor;

  const dash = el.dash === 'dashed' ? [10/zoom, 5/zoom]
             : el.dash === 'dotted' ? [2/zoom,  4/zoom]  : [];

  ctx.save();
  ctx.lineCap    = 'round';
  ctx.lineJoin   = 'round';
  ctx.beginPath();
  ctx.moveTo(el.x, el.y);
  ctx.lineTo(x2, y2);
  ctx.strokeStyle = drawColor;
  ctx.lineWidth   = isSel ? sw * 2.2 : (isHov ? sw * 1.6 : sw);
  ctx.setLineDash(dash);
  ctx.globalAlpha = isSel ? 1 : 0.90;
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.globalAlpha = 1;

  const angle = Math.atan2(y2 - el.y, x2 - el.x);

  // Seta na ponta B (fim)
  if (el.arrowEnd) drawArrowHead(x2, y2, angle, 13, drawColor);

  // Seta na ponta A (origem)
  if (el.arrowStart) drawArrowHead(el.x, el.y, angle + Math.PI, 13, drawColor);

  // Label no meio do segmento
  if (el.label) {
    const mx = (el.x + x2) / 2;
    const my = (el.y + y2) / 2;
    ctx.font = elFont(el, 10, '600');
    const fs = parseFloat(ctx.font);
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    const tw = ctx.measureText(el.label).width + 8/zoom;
    const th = (fs || 10) * 1.5;
    ctx.fillStyle = hexAlpha(cor, .14);
    ctx.beginPath(); ctx.roundRect(mx - tw/2, my - th/2 - 8/zoom, tw, th, 4/zoom); ctx.fill();
    ctx.fillStyle = isSel ? '#ff3b30' : cor;
    ctx.fillText(el.label, mx, my - 8/zoom);
  }

  // Pontos de extremidade — mostrados ao selecionar ou ao passar o mouse
  if (isSel || isHov) {
    const epR = Math.max(4/zoom, sw * 1.8);
    ctx.setLineDash([]);
    for (const [px, py] of [[el.x, el.y], [x2, y2]]) {
      // Detecta se o endpoint está sobre (ou muito perto de) outro elemento
      const SNAP_T = 10 / zoom;
      const snapEl = state.elements.find(oe => {
        if (oe.id === el.id) return false;
        if (oe.type === 'linha') {
          const ox2 = oe.x2 ?? oe.x+150, oy2 = oe.y2 ?? oe.y;
          return Math.hypot(px-oe.x,  py-oe.y)  < SNAP_T
              || Math.hypot(px-ox2,   py-oy2)   < SNAP_T;
        }
        const ow = oe.width||(IS_FORMA(oe.type)?200:60);
        const oh = oe.height||(IS_FORMA(oe.type)?140:60);
        return px >= oe.x-SNAP_T && px <= oe.x+ow+SNAP_T
            && py >= oe.y-SNAP_T && py <= oe.y+oh+SNAP_T;
      });
      const epCor = snapEl ? '#34c759' : drawColor;
      // Anel externo branco
      ctx.beginPath(); ctx.arc(px, py, epR + 1.5/zoom, 0, Math.PI*2);
      ctx.fillStyle = '#fff'; ctx.globalAlpha = 0.9; ctx.fill(); ctx.globalAlpha = 1;
      // Círculo colorido
      ctx.beginPath(); ctx.arc(px, py, epR, 0, Math.PI*2);
      ctx.strokeStyle = epCor; ctx.lineWidth = sw * 1.2; ctx.stroke();
      if (snapEl) {
        // Ponto central verde = conectado
        ctx.beginPath(); ctx.arc(px, py, epR * 0.45, 0, Math.PI*2);
        ctx.fillStyle = '#34c759'; ctx.fill();
      }
    }
  }

  ctx.restore();
}

/* ── Ícones vetoriais ─────────────────────────────────────────────── */
function drawIcon(ctx, tipo, cx, cy, r, cor) {
  ctx.save();
  ctx.strokeStyle = cor; ctx.fillStyle = cor;
  ctx.lineWidth = Math.max(0.8, r * 0.14) / zoom;
  ctx.lineCap = 'round'; ctx.lineJoin = 'round';
  const lw = r * 0.14 / zoom;

  switch (tipo) {
    case 'camera': {
      // Anel externo da lente
      ctx.beginPath(); ctx.arc(cx, cy, r * 0.80, 0, Math.PI*2); ctx.stroke();
      // Íris
      ctx.beginPath(); ctx.arc(cx, cy, r * 0.52, 0, Math.PI*2);
      ctx.fillStyle = hexAlpha(cor, 0.20); ctx.fill();
      ctx.strokeStyle = cor; ctx.stroke();
      // Pupila
      ctx.beginPath(); ctx.arc(cx, cy, r * 0.25, 0, Math.PI*2);
      ctx.fillStyle = cor; ctx.fill();
      // Reflexo
      ctx.beginPath(); ctx.arc(cx - r*0.16, cy - r*0.16, r*0.08, 0, Math.PI*2);
      ctx.fillStyle = 'rgba(255,255,255,.80)'; ctx.fill();
      break;
    }
    case 'access_point': {
      // Arcos de Wi-Fi
      for (let i = 1; i <= 3; i++) {
        const ri = r * i * 0.36;
        ctx.globalAlpha = 1 - (i - 1) * 0.28;
        ctx.beginPath(); ctx.arc(cx, cy + r * 0.25, ri, -Math.PI * 0.82, -Math.PI * 0.18); ctx.stroke();
      }
      ctx.globalAlpha = 1;
      // Ponto central
      ctx.beginPath(); ctx.arc(cx, cy + r * 0.25, r * 0.13, 0, Math.PI*2); ctx.fill();
      break;
    }
    case 'switch': {
      // Corpo
      const sw = r * 1.6, sh = r * 0.78;
      ctx.beginPath(); ctx.roundRect(cx - sw/2, cy - sh/2, sw, sh, r * 0.1); ctx.stroke();
      // Portas
      for (let i = 0; i < 4; i++) {
        const px = cx - sw/2 + sw * (i + 0.5) / 4;
        ctx.beginPath(); ctx.roundRect(px - r*0.13, cy + sh*0.05, r*0.26, r*0.3, r*0.04); ctx.fill();
      }
      // LED indicador
      ctx.fillStyle = hexAlpha(cor, 0.6);
      ctx.beginPath(); ctx.arc(cx + sw/2 - r*0.22, cy - sh/2 + r*0.18, r*0.09, 0, Math.PI*2); ctx.fill();
      break;
    }
    case 'rack': {
      // Chassi
      const rw = r * 1.25, rh = r * 1.6;
      ctx.beginPath(); ctx.roundRect(cx - rw/2, cy - rh/2, rw, rh, r * 0.1); ctx.stroke();
      // Unidades
      for (let i = 0; i < 3; i++) {
        const uy = cy - rh/2 + rh * (i + 0.5) / 3.5;
        ctx.beginPath(); ctx.roundRect(cx - rw/2 + r*0.13, uy - r*0.13, rw - r*0.26, r*0.26, r*0.05); ctx.stroke();
      }
      break;
    }
    case 'desktop': {
      // Monitor
      const mw = r * 1.6, mh = r * 1.05;
      const my = cy - mh/2 - r * 0.12;
      ctx.beginPath(); ctx.roundRect(cx - mw/2, my, mw, mh, r * 0.13); ctx.stroke();
      ctx.fillStyle = hexAlpha(cor, 0.14);
      ctx.beginPath(); ctx.roundRect(cx - mw/2 + r*0.1, my + r*0.1, mw - r*0.2, mh - r*0.18, r*0.08); ctx.fill();
      ctx.fillStyle = cor;
      // Base
      ctx.beginPath();
      ctx.moveTo(cx - r*0.32, my + mh); ctx.lineTo(cx - r*0.32, my + mh + r*0.3);
      ctx.moveTo(cx + r*0.32, my + mh); ctx.lineTo(cx + r*0.32, my + mh + r*0.3);
      ctx.moveTo(cx - r*0.52, my + mh + r*0.3); ctx.lineTo(cx + r*0.52, my + mh + r*0.3);
      ctx.stroke();
      break;
    }
    case 'impressora': {
      // Corpo
      const pw = r * 1.5, ph = r * 0.9;
      ctx.beginPath(); ctx.roundRect(cx - pw/2, cy - ph*0.1, pw, ph, r * 0.1); ctx.stroke();
      // Papel saindo pelo topo
      ctx.beginPath(); ctx.roundRect(cx - pw*0.32, cy - ph*0.1 - r*0.62, pw*0.64, r*0.68, r*0.05); ctx.stroke();
      // Slot de saída
      ctx.beginPath(); ctx.moveTo(cx - pw/2 + r*0.18, cy + ph*0.38); ctx.lineTo(cx + pw/2 - r*0.18, cy + ph*0.38); ctx.stroke();
      break;
    }
    case 'nobreak': {
      // Bateria
      const nbw = r * 1.2, nbh = r * 1.55;
      ctx.beginPath(); ctx.roundRect(cx - nbw/2, cy - nbh/2, nbw, nbh, r*0.1); ctx.stroke();
      // Terminal
      ctx.beginPath(); ctx.roundRect(cx - nbw*0.28, cy - nbh/2 - r*0.17, nbw*0.56, r*0.17, r*0.05); ctx.fill();
      // Relâmpago
      ctx.beginPath();
      ctx.moveTo(cx + r*0.17, cy - r*0.5);
      ctx.lineTo(cx - r*0.17, cy - r*0.05);
      ctx.lineTo(cx + r*0.06, cy - r*0.05);
      ctx.lineTo(cx - r*0.17, cy + r*0.5);
      ctx.lineTo(cx + r*0.17, cy + r*0.05);
      ctx.lineTo(cx - r*0.06, cy + r*0.05);
      ctx.closePath();
      ctx.fillStyle = hexAlpha(cor, 0.25); ctx.fill();
      ctx.stroke();
      break;
    }
    case 'servidor': {
      // Pilha de barras de servidor
      const svw = r * 1.5, svh = r * 0.38;
      for (let i = 0; i < 3; i++) {
        const sy = cy - r * 0.6 + i * (svh + r * 0.1);
        ctx.beginPath(); ctx.roundRect(cx - svw/2, sy, svw, svh, r*0.06); ctx.stroke();
        ctx.beginPath(); ctx.arc(cx + svw/2 - r*0.17, sy + svh/2, r*0.09, 0, Math.PI*2); ctx.fill();
      }
      break;
    }
    case 'ponto_rede': {
      // Círculo externo + ponto interno
      ctx.beginPath(); ctx.arc(cx, cy, r * 0.72, 0, Math.PI*2); ctx.stroke();
      ctx.beginPath(); ctx.arc(cx, cy, r * 0.28, 0, Math.PI*2); ctx.fill();
      break;
    }
    case 'texto': {
      // Letra "A" estilizada
      ctx.font = `bold ${r * 1.35 / zoom}px -apple-system, sans-serif`;
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillText('A', cx, cy);
      break;
    }
    case 'meraki': {
      // Appliance gerenciado em nuvem (Meraki): corpo + LEDs + nuvem
      const mw = r * 1.7, mh = r * 0.72;
      const by = cy + r * 0.12;
      ctx.beginPath(); ctx.roundRect(cx - mw/2, by - mh/2, mw, mh, r * 0.16); ctx.stroke();
      ctx.fillStyle = hexAlpha(cor, 0.12);
      ctx.beginPath(); ctx.roundRect(cx - mw/2, by - mh/2, mw, mh, r * 0.16); ctx.fill();
      // LEDs de status
      ctx.fillStyle = cor;
      for (let i = 0; i < 3; i++) {
        ctx.globalAlpha = i === 0 ? 1 : 0.45;
        ctx.beginPath(); ctx.arc(cx - mw/2 + r*0.32 + i*r*0.34, by + mh*0.18, r*0.08, 0, Math.PI*2); ctx.fill();
      }
      ctx.globalAlpha = 1;
      // Nuvem (cloud-managed) acima do corpo
      const ny = by - mh/2 - r * 0.5, s = r * 0.5;
      ctx.fillStyle = hexAlpha(cor, 0.16); ctx.strokeStyle = cor;
      ctx.beginPath();
      ctx.arc(cx - s*0.62, ny, s*0.42, Math.PI*0.5, Math.PI*1.5);
      ctx.arc(cx - s*0.08, ny - s*0.34, s*0.5, Math.PI*0.92, Math.PI*2.05);
      ctx.arc(cx + s*0.6, ny, s*0.42, Math.PI*1.5, Math.PI*0.5);
      ctx.closePath(); ctx.fill(); ctx.stroke();
      break;
    }
    case 'starlink': {
      // Antena Starlink: mastro + base + prato inclinado + sinal
      ctx.save();
      ctx.strokeStyle = cor; ctx.fillStyle = cor;
      ctx.lineWidth = Math.max(0.8, r * 0.13) / zoom;
      // Mastro e base
      ctx.beginPath(); ctx.moveTo(cx, cy + r*0.15); ctx.lineTo(cx, cy + r*0.85); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(cx - r*0.32, cy + r*0.85); ctx.lineTo(cx + r*0.32, cy + r*0.85); ctx.stroke();
      // Prato (elipse inclinada)
      ctx.save();
      ctx.translate(cx, cy - r*0.12); ctx.rotate(-0.34);
      ctx.beginPath(); ctx.ellipse(0, 0, r*0.86, r*0.40, 0, 0, Math.PI*2);
      ctx.fillStyle = hexAlpha(cor, 0.16); ctx.fill();
      ctx.strokeStyle = cor; ctx.stroke();
      ctx.beginPath(); ctx.arc(0, 0, r*0.11, 0, Math.PI*2); ctx.fillStyle = cor; ctx.fill();
      ctx.restore();
      // Ondas de sinal acima
      ctx.strokeStyle = cor;
      for (let i = 1; i <= 2; i++) {
        ctx.globalAlpha = 1 - (i - 1) * 0.4;
        ctx.beginPath(); ctx.arc(cx + r*0.42, cy - r*0.55, r * i * 0.3, -Math.PI*0.95, -Math.PI*0.35); ctx.stroke();
      }
      ctx.globalAlpha = 1;
      ctx.restore();
      break;
    }
    case 'caixa_emenda': {
      // Caixa de emenda óptica: cápsula + fibras em leque na base + bandejas
      const cw = r * 0.98, ch = r * 1.7;
      ctx.fillStyle = hexAlpha(cor, 0.13);
      ctx.beginPath(); ctx.roundRect(cx - cw/2, cy - ch/2, cw, ch, cw/2); ctx.fill();
      ctx.strokeStyle = cor;
      ctx.beginPath(); ctx.roundRect(cx - cw/2, cy - ch/2, cw, ch, cw/2); ctx.stroke();
      // Bandejas de emenda (linhas internas)
      for (let i = 0; i < 3; i++) {
        const ly = cy - ch*0.22 + i * ch*0.22;
        ctx.beginPath(); ctx.moveTo(cx - cw*0.28, ly); ctx.lineTo(cx + cw*0.28, ly); ctx.stroke();
      }
      // Fibras saindo pela base (leque)
      for (let i = -1; i <= 1; i++) {
        ctx.beginPath();
        ctx.moveTo(cx + i*cw*0.26, cy + ch/2 - r*0.05);
        ctx.lineTo(cx + i*r*0.72, cy + ch/2 + r*0.5);
        ctx.stroke();
      }
      break;
    }
    default: {
      ctx.beginPath(); ctx.arc(cx, cy, r * 0.6, 0, Math.PI*2); ctx.fill();
    }
  }
  ctx.restore();
}

/* ── Câmera circular com setor FOV ────────────────────────────────── */
function drawCamera(el) {
  const bw = el.width  || 56;
  const bh = el.height || 56;
  const r  = Math.min(bw, bh) / 2;
  const cx = el.x + bw / 2;
  const cy = el.y + bh / 2;
  const isSelected = selectionIds.has(el.id);
  const isHover    = hoverId === el.id;
  const isLocked   = !!el.locked;
  const cor = el.color || '#ef4444';
  const fovDeg    = el.fovAngle ?? 60;
  const fovDirDeg = el.fovDir   ?? 0;
  const fovHalf   = (fovDeg / 2) * Math.PI / 180;
  const fovDir    = fovDirDeg * Math.PI / 180;
  const fovLen    = r * 2.8;

  ctx.save();

  // Rotação
  const _rotC = el.rotation || 0;
  if (_rotC) {
    ctx.translate(cx, cy);
    ctx.rotate(_rotC * Math.PI / 180);
    ctx.translate(-cx, -cy);
  }

  // Glow PRTG
  let _glowCol = null, _glowBlur = 0;
  if (el.prtg_objid && prtgEditorMap[el.prtg_objid]) {
    const _dev = prtgEditorMap[el.prtg_objid];
    const _st  = parseInt(_dev.status ?? _dev.status_raw ?? 0);
    if (_st === 5)      { _glowCol = '#ff3b30'; _glowBlur = 22/zoom; }
    else if (_st === 4) { _glowCol = '#ff9500'; _glowBlur = 16/zoom; }
    else if (_st === 3) { _glowCol = '#34c759'; _glowBlur = 10/zoom; }
    else                { _glowCol = '#8e8e93'; _glowBlur =  6/zoom; }
  }

  // Setor FOV
  ctx.beginPath();
  ctx.moveTo(cx, cy);
  ctx.arc(cx, cy, fovLen, fovDir - fovHalf, fovDir + fovHalf);
  ctx.closePath();
  ctx.fillStyle   = hexAlpha(cor, isSelected ? 0.20 : 0.09);
  ctx.strokeStyle = hexAlpha(cor, isSelected ? 0.55 : 0.25);
  ctx.lineWidth   = 1.2 / zoom;
  ctx.setLineDash([4/zoom, 3/zoom]);
  ctx.fill(); ctx.stroke();
  ctx.setLineDash([]);

  // Sombra / glow
  ctx.shadowColor   = _glowCol || (isSelected ? hexAlpha(cor, .40) : (isHover ? 'rgba(0,0,0,.18)' : 'rgba(0,0,0,.14)'));
  ctx.shadowBlur    = _glowCol ? _glowBlur : (isSelected ? 16 : isHover ? 12 : 7) / zoom;
  ctx.shadowOffsetX = 0;
  ctx.shadowOffsetY = _glowCol ? 0 : 2/zoom;

  // Corpo circular
  ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI*2);
  ctx.fillStyle = isSelected ? hexAlpha(cor, 0.14) : '#ffffff';
  ctx.fill();
  ctx.shadowColor = 'transparent'; ctx.shadowBlur = 0; ctx.shadowOffsetY = 0;

  // Anel externo colorido
  ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI*2);
  ctx.strokeStyle = isSelected ? cor : (isHover ? hexAlpha(cor, 0.70) : hexAlpha(cor, 0.45));
  ctx.lineWidth   = (isSelected ? 2.5 : isHover ? 2 : 1.5) / zoom;
  ctx.stroke();

  // Indicador PRTG (anel exterior colorido)
  if (el.prtg_objid && prtgEditorMap[el.prtg_objid]) {
    const dev = prtgEditorMap[el.prtg_objid];
    const st  = parseInt(dev.status ?? dev.status_raw ?? 0);
    const stCor = st===3 ? '#34c759' : st===4 ? '#ff9500' : st===5 ? '#ff3b30' : '#8e8e93';
    ctx.beginPath(); ctx.arc(cx, cy, r + 2.5/zoom, 0, Math.PI*2);
    ctx.strokeStyle = stCor; ctx.lineWidth = 2.5/zoom; ctx.stroke();
  }

  // Ícone de lente dentro do círculo
  const iRad = r * 0.68;
  ctx.strokeStyle = cor; ctx.lineWidth = Math.max(0.8, r * 0.10) / zoom;
  // Anel externo
  ctx.beginPath(); ctx.arc(cx, cy, iRad * 0.80, 0, Math.PI*2); ctx.stroke();
  // Íris
  ctx.beginPath(); ctx.arc(cx, cy, iRad * 0.50, 0, Math.PI*2);
  ctx.fillStyle = hexAlpha(cor, 0.22); ctx.fill(); ctx.stroke();
  // Pupila
  ctx.beginPath(); ctx.arc(cx, cy, iRad * 0.24, 0, Math.PI*2);
  ctx.fillStyle = cor; ctx.fill();
  // Reflexo especular
  ctx.beginPath(); ctx.arc(cx - iRad*0.15, cy - iRad*0.15, iRad*0.09, 0, Math.PI*2);
  ctx.fillStyle = 'rgba(255,255,255,.80)'; ctx.fill();

  // Label abaixo
  if (el.label) {
    ctx.font = elFont(el, 9, '700');
    ctx.fillStyle = isSelected ? cor : '#3a3a3c';
    ctx.textAlign = 'center'; ctx.textBaseline = 'top';
    let txt = el.label;
    const maxW = r * 3.0;
    while (txt.length > 3 && ctx.measureText(txt).width > maxW) txt = txt.slice(0,-1);
    if (txt !== el.label) txt = txt.slice(0,-1) + '…';
    ctx.fillText(txt, cx, cy + r + 4/zoom);
  }

  // Cadeado
  if (isLocked) {
    ctx.font = `${Math.max(7, 8/zoom)}px serif`;
    ctx.textAlign = 'right'; ctx.textBaseline = 'top';
    ctx.globalAlpha = 0.85;
    ctx.fillText('🔒', cx + r - 1/zoom, cy - r + 1/zoom);
    ctx.globalAlpha = 1;
  }

  // Indicador de grupo (anel tracejado)
  if (el.groupId) {
    const gpad = 6/zoom;
    ctx.strokeStyle = 'rgba(88,86,214,.65)';
    ctx.lineWidth   = 1.5/zoom;
    ctx.setLineDash([5/zoom, 3/zoom]);
    ctx.beginPath(); ctx.arc(cx, cy, r + gpad, 0, Math.PI*2); ctx.stroke();
    ctx.setLineDash([]);
  }

  ctx.restore();
}

/* ── Equipamentos / Cards ─────────────────────────────────────────── */
/* ── Access Point: ícone Wi-Fi dentro de um círculo + nome ───────────── */
function drawAccessPoint(el) {
  const w = el.width  || 60;
  const h = el.height || 60;
  const cx = el.x + w/2;
  const cy = el.y + h/2;
  const R  = Math.min(w, h) * 0.46;   // raio do círculo (corpo)
  const isSelected = selectionIds.has(el.id);
  const isHover    = hoverId === el.id;
  const isLocked   = !!el.locked;
  const cor = elEffColor(el);

  ctx.save();
  const _rot = el.rotation || 0;
  if (_rot) {
    ctx.translate(cx, cy);
    ctx.rotate(_rot * Math.PI / 180);
    ctx.translate(-cx, -cy);
  }

  // Status PRTG
  let stCor = null, glowBlur = 0;
  if (el.prtg_objid && prtgEditorMap[el.prtg_objid]) {
    const st = parseInt(prtgEditorMap[el.prtg_objid].status ?? prtgEditorMap[el.prtg_objid].status_raw ?? 0);
    stCor = st===3 ? '#34c759' : st===4 ? '#ff9500' : st===5 ? '#ff3b30' : '#8e8e93';
    glowBlur = st===5 ? 22 : st===4 ? 16 : st===3 ? 10 : 6;
  }

  // Sombra/glow + corpo branco — destaca o ícone sobre qualquer fundo
  ctx.shadowColor   = stCor || (isSelected ? hexAlpha(cor, .40) : (isHover ? 'rgba(0,0,0,.20)' : 'rgba(0,0,0,.16)'));
  ctx.shadowBlur    = (stCor ? glowBlur : (isSelected ? 16 : isHover ? 12 : 8)) / zoom;
  ctx.shadowOffsetY = stCor ? 0 : 2/zoom;
  ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI*2);
  ctx.fillStyle = isSelected ? hexAlpha(cor, 0.14) : '#ffffff';
  ctx.fill();
  ctx.shadowColor = 'transparent'; ctx.shadowBlur = 0; ctx.shadowOffsetY = 0;

  // Anel do círculo
  ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI*2);
  ctx.strokeStyle = isSelected ? cor : (isHover ? hexAlpha(cor, 0.70) : hexAlpha(cor, 0.45));
  ctx.lineWidth   = (isSelected ? 2.5 : isHover ? 2 : 1.5) / zoom;
  ctx.stroke();

  // Anel de status PRTG (externo)
  if (stCor) {
    ctx.beginPath(); ctx.arc(cx, cy, R + 2.5/zoom, 0, Math.PI*2);
    ctx.strokeStyle = stCor; ctx.lineWidth = 2.5/zoom; ctx.stroke();
  }

  // Ícone Wi-Fi dentro do círculo
  drawIcon(ctx, 'access_point', cx, cy + R*0.06, R * 0.60, cor);

  // Nome (abaixo do círculo)
  if (el.label) {
    ctx.font = elFont(el, 9, '700');
    ctx.fillStyle = isSelected ? cor : '#3a3a3c';
    ctx.textAlign = 'center'; ctx.textBaseline = 'top';
    const maxW = w * 1.6;
    let txt = el.label;
    while (txt.length > 3 && ctx.measureText(txt).width > maxW) txt = txt.slice(0,-1);
    if (txt !== el.label) txt = txt.slice(0,-1) + '…';
    ctx.fillText(txt, cx, cy + R + 4/zoom);
  }

  if (isLocked) {
    ctx.font = `${Math.max(7, 8/zoom)}px serif`;
    ctx.textAlign = 'right'; ctx.textBaseline = 'top';
    ctx.globalAlpha = 0.85;
    ctx.fillText('🔒', cx + R - 1/zoom, cy - R + 1/zoom);
    ctx.globalAlpha = 1;
  }

  if (el.groupId) {
    ctx.strokeStyle = 'rgba(88,86,214,.65)';
    ctx.lineWidth   = 1.5/zoom;
    ctx.setLineDash([5/zoom, 3/zoom]);
    ctx.beginPath(); ctx.arc(cx, cy, R + 6/zoom, 0, Math.PI*2); ctx.stroke();
    ctx.setLineDash([]);
  }

  ctx.restore();
}

/* Cor do status PRTG do elemento (ou null se não vinculado/sem status). */
function elStatusColor(el) {
  if (!el.prtg_objid || !prtgEditorMap[el.prtg_objid]) return null;
  const st = parseInt(prtgEditorMap[el.prtg_objid].status ?? prtgEditorMap[el.prtg_objid].status_raw ?? 0);
  return st===3 ? '#34c759' : st===4 ? '#ff9500' : st===5 ? '#ff3b30' : '#8e8e93';
}
/* Cor efetiva: switch, Access Point e Meraki assumem a cor do status. */
function elEffColor(el) {
  if (el.type === 'switch' || el.type === 'access_point' || el.type === 'meraki') {
    const sc = elStatusColor(el);
    if (sc) return sc;
  }
  return el.color || '#0071e3';
}

/* ── Fonte de energia: badge pequeno e chamativo, SEM quadrado ───── */
function drawFonte(el) {
  const w = el.width  || 40;
  const h = el.height || 40;
  const cx = el.x + w/2, cy = el.y + h/2;
  const R  = Math.min(w, h) * 0.46;
  const isSelected = selectionIds.has(el.id);
  const isHover    = hoverId === el.id;
  const isLocked   = !!el.locked;
  const cor = el.color || '#f59e0b';

  ctx.save();
  const _rot = el.rotation || 0;
  if (_rot) { ctx.translate(cx, cy); ctx.rotate(_rot * Math.PI/180); ctx.translate(-cx, -cy); }

  // Status PRTG (glow tem prioridade)
  let stCor = null, glowBlur = 0;
  if (el.prtg_objid && prtgEditorMap[el.prtg_objid]) {
    const st = parseInt(prtgEditorMap[el.prtg_objid].status ?? prtgEditorMap[el.prtg_objid].status_raw ?? 0);
    stCor = st===3 ? '#34c759' : st===4 ? '#ff9500' : st===5 ? '#ff3b30' : '#8e8e93';
    glowBlur = st===5 ? 22 : st===4 ? 16 : st===3 ? 10 : 6;
  }

  // Disco preenchido (chamativo) com brilho
  ctx.shadowColor = stCor || hexAlpha(cor, isSelected ? 0.95 : (isHover ? 0.78 : 0.62));
  ctx.shadowBlur  = (stCor ? glowBlur : (isSelected ? 18 : isHover ? 14 : 11)) / zoom;
  ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI*2);
  ctx.fillStyle = cor; ctx.fill();
  ctx.shadowColor = 'transparent'; ctx.shadowBlur = 0;

  // Anel branco (destaca em qualquer fundo)
  ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI*2);
  ctx.strokeStyle = isSelected ? '#fff' : 'rgba(255,255,255,.8)';
  ctx.lineWidth   = (isSelected ? 2.4 : 1.6) / zoom; ctx.stroke();
  if (stCor) {
    ctx.beginPath(); ctx.arc(cx, cy, R + 2.5/zoom, 0, Math.PI*2);
    ctx.strokeStyle = stCor; ctx.lineWidth = 2.5/zoom; ctx.stroke();
  }

  // Raio (lightning) branco no centro
  const s = R * 0.64;
  ctx.beginPath();
  ctx.moveTo(cx + s*0.30, cy - s*0.95);
  ctx.lineTo(cx - s*0.40, cy + s*0.12);
  ctx.lineTo(cx + s*0.02, cy + s*0.12);
  ctx.lineTo(cx - s*0.26, cy + s*0.95);
  ctx.lineTo(cx + s*0.44, cy - s*0.18);
  ctx.lineTo(cx - s*0.02, cy - s*0.18);
  ctx.closePath();
  ctx.fillStyle = '#fff'; ctx.fill();

  // Nome (abaixo)
  if (el.label) {
    ctx.font = elFont(el, 9, '700');
    ctx.fillStyle = isSelected ? cor : '#3a3a3c';
    ctx.textAlign = 'center'; ctx.textBaseline = 'top';
    const maxW = w * 2.2;
    let txt = el.label;
    while (txt.length > 3 && ctx.measureText(txt).width > maxW) txt = txt.slice(0,-1);
    if (txt !== el.label) txt = txt.slice(0,-1) + '…';
    ctx.fillText(txt, cx, cy + R + 4/zoom);
  }

  if (isLocked) {
    ctx.font = `${Math.max(7, 8/zoom)}px serif`;
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.globalAlpha = 0.85; ctx.fillText('🔒', cx + R*0.7, cy - R*0.7); ctx.globalAlpha = 1;
  }
  if (el.groupId) {
    ctx.strokeStyle = 'rgba(88,86,214,.65)'; ctx.lineWidth = 1.5/zoom;
    ctx.setLineDash([5/zoom, 3/zoom]);
    ctx.beginPath(); ctx.arc(cx, cy, R + 6/zoom, 0, Math.PI*2); ctx.stroke(); ctx.setLineDash([]);
  }
  ctx.restore();
}

function drawElementCard(el) {
  if (el.type === 'camera') { drawCamera(el); return; }
  if (el.type === 'access_point') { drawAccessPoint(el); return; }
  if (el.type === 'fonte') { drawFonte(el); return; }
  const w = el.width  || 60;
  const h = el.height || 60;
  const isSelected = selectionIds.has(el.id);
  const isHover    = hoverId === el.id;
  const isLocked   = !!el.locked;
  const cor = elEffColor(el);
  const r = 10/zoom;

  ctx.save();
  // Rotação em torno do centro do card
  const _rotC = el.rotation || 0;
  if (_rotC) {
    ctx.translate(el.x + w/2, el.y + h/2);
    ctx.rotate(_rotC * Math.PI / 180);
    ctx.translate(-(el.x + w/2), -(el.y + h/2));
  }

  if (el.type === 'texto') {
    const txt   = el.label || 'Texto…';
    ctx.font    = elFont(el, 14, '600');
    const lines = txt.split('\n');
    const fs    = el.fontSize || 14;
    const lineH = fs * 1.38;
    const pad   = 9;
    // Mede a largura máxima do conteúdo
    const textW = Math.max(...lines.map(l => ctx.measureText(l).width));
    const boxW  = Math.max((el.width  || 0), textW + pad * 2);
    const boxH  = Math.max((el.height || 0), lines.length * lineH + pad * 2);
    // Sincroniza dimensões para hit test e drag correto
    el.width  = boxW;
    el.height = boxH;

    // Caixa de fundo
    const bgA = isSelected ? 0.10 : (isHover ? 0.06 : 0.03);
    ctx.fillStyle   = hexAlpha(cor, bgA);
    ctx.strokeStyle = isSelected ? cor : hexAlpha(cor, isHover ? 0.55 : 0.20);
    ctx.lineWidth   = (isSelected ? 1.8 : 1) / zoom;
    ctx.setLineDash(isSelected ? [] : [5/zoom, 3/zoom]);
    ctx.beginPath(); ctx.roundRect(el.x, el.y, boxW, boxH, 7/zoom);
    ctx.fill(); ctx.stroke();
    ctx.setLineDash([]);

    // Ícone de edição no canto superior direito (hint para duplo-clique)
    if (isHover || isSelected) {
      ctx.font = `${Math.max(8, 9/zoom)}px serif`;
      ctx.textAlign = 'right'; ctx.textBaseline = 'top';
      ctx.globalAlpha = 0.45;
      ctx.fillStyle = cor;
      ctx.fillText('✏️', el.x + boxW - 3/zoom, el.y + 2/zoom);
      ctx.globalAlpha = 1;
      ctx.font = elFont(el, 14, '600'); // restaurar fonte
    }

    // Texto linha a linha
    const _ta = el.textAlign || 'left';
    const _tx = _ta === 'center' ? el.x + boxW/2 : _ta === 'right' ? el.x + boxW - pad : el.x + pad;
    ctx.fillStyle    = cor;
    ctx.textAlign    = _ta;
    ctx.textBaseline = 'top';
    ctx.shadowColor  = isSelected ? hexAlpha(cor, 0.22) : 'transparent';
    ctx.shadowBlur   = isSelected ? 5/zoom : 0;
    lines.forEach((line, i) => ctx.fillText(line, _tx, el.y + pad + i * lineH));
    ctx.shadowColor = 'transparent'; ctx.shadowBlur = 0;

    // Cadeado
    if (isLocked) {
      ctx.font = `${Math.max(7, 8/zoom)}px serif`;
      ctx.textAlign = 'right'; ctx.textBaseline = 'bottom';
      ctx.globalAlpha = 0.75;
      ctx.fillText('🔒', el.x + boxW - 3/zoom, el.y + boxH - 3/zoom);
      ctx.globalAlpha = 1;
    }

    // Indicador de grupo (mesma lógica do card)
    if (el.groupId) {
      const _gpad = 4/zoom;
      ctx.strokeStyle = 'rgba(88,86,214,.60)';
      ctx.lineWidth   = 1.5/zoom;
      ctx.setLineDash([5/zoom, 3/zoom]);
      ctx.beginPath(); ctx.roundRect(el.x - _gpad, el.y - _gpad, boxW + _gpad*2, boxH + _gpad*2, 10/zoom);
      ctx.stroke(); ctx.setLineDash([]);
    }

    ctx.restore(); return;
  }

  // Glow neon por status PRTG
  let _glowCol = null, _glowBlur = 0;
  if (el.prtg_objid && prtgEditorMap[el.prtg_objid]) {
    const _dev = prtgEditorMap[el.prtg_objid];
    const _st  = parseInt(_dev.status ?? _dev.status_raw ?? 0);
    if (_st === 5)      { _glowCol = '#ff3b30'; _glowBlur = 22/zoom; }
    else if (_st === 4) { _glowCol = '#ff9500'; _glowBlur = 16/zoom; }
    else if (_st === 3) { _glowCol = '#34c759'; _glowBlur = 10/zoom; }
    else                { _glowCol = '#8e8e93'; _glowBlur =  6/zoom; }
  }

  // Sombra multicamada (glow PRTG tem prioridade sobre sombra padrão)
  ctx.shadowColor   = _glowCol || (isSelected ? hexAlpha(cor, .40) : (isHover ? 'rgba(0,0,0,.18)' : 'rgba(0,0,0,.14)'));
  ctx.shadowBlur    = _glowCol ? _glowBlur : (isSelected ? 16 : isHover ? 12 : 7) / zoom;
  ctx.shadowOffsetX = 0;
  ctx.shadowOffsetY = _glowCol ? 0 : (isSelected ? 0 : 3) / zoom;

  // Fundo do card — COR ÚNICA (tom suave da cor escolhida pelo usuário,
  // sem faixa/divisão de cores). O usuário controla a cor pela propriedade.
  const grad = ctx.createLinearGradient(el.x, el.y, el.x, el.y + h);
  if (isSelected) {
    grad.addColorStop(0, hexAlpha(cor, .24));
    grad.addColorStop(1, hexAlpha(cor, .12));
  } else {
    grad.addColorStop(0, hexAlpha(cor, .15));
    grad.addColorStop(1, hexAlpha(cor, .07));
  }
  ctx.beginPath(); ctx.roundRect(el.x, el.y, w, h, r);
  ctx.fillStyle = grad; ctx.fill();
  ctx.shadowColor = 'transparent'; ctx.shadowBlur = 0; ctx.shadowOffsetY = 0;

  // Borda (mesma cor única)
  ctx.beginPath(); ctx.roundRect(el.x, el.y, w, h, r);
  ctx.strokeStyle = isSelected ? cor : (isHover ? hexAlpha(cor, .60) : hexAlpha(cor, .35));
  ctx.lineWidth   = (isSelected ? 2.2 : isHover ? 1.6 : 1.2) / zoom;
  ctx.stroke();

  // Indicador PRTG (anel colorido + ponto no canto superior direito)
  if (el.prtg_objid && prtgEditorMap[el.prtg_objid]) {
    const dev = prtgEditorMap[el.prtg_objid];
    const st  = parseInt(dev.status ?? dev.status_raw ?? 0);
    const stCor = st===3 ? '#34c759' : st===4 ? '#ff9500' : st===5 ? '#ff3b30' : '#8e8e93';
    ctx.beginPath(); ctx.roundRect(el.x, el.y, w, h, r);
    ctx.strokeStyle = stCor; ctx.lineWidth = 2.5/zoom; ctx.stroke();
    const dotR = 4/zoom;
    ctx.beginPath(); ctx.arc(el.x + w - dotR*2.4, el.y + dotR*2.4, dotR, 0, Math.PI*2);
    ctx.fillStyle = stCor; ctx.fill();
    ctx.strokeStyle = '#fff'; ctx.lineWidth = 1/zoom; ctx.stroke();
  }

  // Ícone vetorial — MAIOR e centralizado (aparece bem mais dentro do quadrado)
  const icY  = el.y + h * 0.42;
  const icRad = Math.min(w, h) * 0.34;
  ctx.shadowColor = 'transparent';
  drawIcon(ctx, el.type, el.x + w/2, icY, icRad, cor);

  // Label
  if (el.label) {
    ctx.font = elFont(el, 9, '700');
    ctx.fillStyle = isSelected ? cor : '#3a3a3c';
    ctx.textAlign = 'center'; ctx.textBaseline = 'top';
    const maxW = w * 0.92;
    let txt = el.label;
    while (txt.length > 3 && ctx.measureText(txt).width > maxW) txt = txt.slice(0,-1);
    if (txt !== el.label) txt = txt.slice(0,-1) + '…';
    ctx.fillText(txt, el.x + w/2, el.y + h * 0.77);
  }

  // Cadeado (elemento travado)
  if (isLocked) {
    ctx.font = `${Math.max(7, 8/zoom)}px serif`;
    ctx.textAlign = 'left'; ctx.textBaseline = 'top';
    ctx.globalAlpha = 0.85;
    ctx.fillText('🔒', el.x + 4/zoom, el.y + 4/zoom);
    ctx.globalAlpha = 1;
  }

  // Indicador de grupo (anel tracejado roxo)
  if (el.groupId) {
    const pad = 4/zoom;
    ctx.strokeStyle = 'rgba(88,86,214,.65)';
    ctx.lineWidth   = 1.5/zoom;
    ctx.setLineDash([5/zoom, 3/zoom]);
    ctx.beginPath(); ctx.roundRect(el.x - pad, el.y - pad, w + pad*2, h + pad*2, r + pad); ctx.stroke();
    ctx.setLineDash([]);
  }

  ctx.restore();
}

/* ── Handles de resize ───────────────────────────────────────────── */
const HANDLES = ['nw','n','ne','e','se','s','sw','w'];
const HS = 8; // tamanho do handle em pixels de tela

function handlePos(el, name) {
  const w = el.width  || 60;
  const h = el.height || 60;
  const m = { nw:[0,0], n:[.5,0], ne:[1,0], e:[1,.5], se:[1,1], s:[.5,1], sw:[0,1], w:[0,.5] };
  const [fx, fy] = m[name];
  let wx = el.x + w*fx, wy = el.y + h*fy;
  const rot = el.rotation || 0;
  if (rot) {
    const cx = el.x + w/2, cy = el.y + h/2;
    const a  = rot * Math.PI / 180;
    const dx = wx - cx, dy = wy - cy;
    wx = cx + dx*Math.cos(a) - dy*Math.sin(a);
    wy = cy + dx*Math.sin(a) + dy*Math.cos(a);
  }
  return { wx, wy };
}

function drawResizeHandles(el) {
  if (el.locked) return;
  if (el.type === 'linha') {
    // Linha: apenas 2 handles circulares nos endpoints A e B
    const x2 = el.x2 ?? el.x+150, y2 = el.y2 ?? el.y;
    for (const [wx, wy] of [[el.x, el.y], [x2, y2]]) {
      const sc = toScreen(wx, wy);
      ctx.save(); ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
      ctx.beginPath(); ctx.arc(sc.x, sc.y, HS/2 + 2, 0, Math.PI*2);
      ctx.fillStyle = '#fff';
      ctx.strokeStyle = el.color || '#6e6e73';
      ctx.lineWidth = 2;
      ctx.fill(); ctx.stroke();
      ctx.restore();
    }
    return;
  }
  HANDLES.forEach(name => {
    const { wx, wy } = handlePos(el, name);
    const sc = toScreen(wx, wy);
    ctx.save();
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    ctx.fillStyle = '#fff';
    ctx.strokeStyle = el.color || '#0071e3';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.rect(sc.x - HS/2, sc.y - HS/2, HS, HS);
    ctx.fill(); ctx.stroke();
    ctx.restore();
  });
}

function drawSelectionOutline(x, y, w, h, r, cor) {
  ctx.strokeStyle = hexAlpha(cor, .45);
  ctx.lineWidth = 3.5/zoom;
  ctx.beginPath(); ctx.roundRect(x, y, w, h, r); ctx.stroke();
}

/* ── Âncoras e helpers de conexão ───────────────────────────────── */
function elCenter(el) {
  return {
    cx: el.x + (el.width||60)/2,
    cy: el.y + (el.height||60)/2,
  };
}

function anchorPoint(el, edge) {
  const w = el.width  || (el.type==='circulo'?120 : IS_FORMA(el.type)?200 : 60);
  const h = el.height || (el.type==='circulo'?120 : IS_FORMA(el.type)?140 : 60);
  let ax, ay;
  switch (edge) {
    case 'n': ax = el.x + w/2; ay = el.y;       break;
    case 's': ax = el.x + w/2; ay = el.y + h;   break;
    case 'e': ax = el.x + w;   ay = el.y + h/2; break;
    case 'w': ax = el.x;       ay = el.y + h/2; break;
    default:  ax = el.x + w/2; ay = el.y + h/2; break;
  }
  const rot = el.rotation || 0;
  if (rot) {
    const cx = el.x + w/2, cy = el.y + h/2;
    const a  = rot * Math.PI / 180;
    const dx = ax - cx, dy = ay - cy;
    ax = cx + dx*Math.cos(a) - dy*Math.sin(a);
    ay = cy + dx*Math.sin(a) + dy*Math.cos(a);
  }
  return { ax, ay };
}

function hitAnchorEdge(el, wx, wy) {
  const thresh = 14 / zoom;
  for (const edge of ['n','s','e','w']) {
    const {ax, ay} = anchorPoint(el, edge);
    if (Math.hypot(wx - ax, wy - ay) < thresh) return edge;
  }
  return null;
}

/* Borda do elemento voltada para um ponto-alvo (auto-escolha ao conectar). */
function bestEdge(el, tx, ty) {
  const w = el.width || 60, h = el.height || 60;
  const cx = el.x + w/2, cy = el.y + h/2;
  const dx = tx - cx, dy = ty - cy;
  if (Math.abs(dx) >= Math.abs(dy)) return dx >= 0 ? 'e' : 'w';
  return dy >= 0 ? 's' : 'n';
}

function drawAnchorHintsSutil(el) {
  for (const edge of ['n','s','e','w']) {
    const {ax, ay} = anchorPoint(el, edge);
    const sc = toScreen(ax, ay);
    ctx.save();
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    ctx.beginPath();
    ctx.arc(sc.x, sc.y, 4, 0, Math.PI*2);
    ctx.fillStyle   = 'rgba(0,113,227,0.18)';
    ctx.strokeStyle = 'rgba(0,113,227,0.40)';
    ctx.lineWidth   = 1.2;
    ctx.fill(); ctx.stroke();
    ctx.restore();
  }
}

function drawAnchorHints(el, activeEdge) {
  for (const edge of ['n','s','e','w']) {
    const {ax, ay} = anchorPoint(el, edge);
    const sc = toScreen(ax, ay);
    ctx.save();
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    ctx.beginPath();
    ctx.arc(sc.x, sc.y, edge === activeEdge ? 7 : 6, 0, Math.PI*2);
    ctx.fillStyle  = edge === activeEdge ? '#0071e3' : '#fff';
    ctx.strokeStyle = '#0071e3';
    ctx.lineWidth = 2;
    ctx.fill(); ctx.stroke();
    if (edge === activeEdge) {
      // Ponto branco central para indicar ativo
      ctx.beginPath(); ctx.arc(sc.x, sc.y, 2.5, 0, Math.PI*2);
      ctx.fillStyle = '#fff'; ctx.fill();
    }
    ctx.restore();
  }
}

function hitTestConnectionEndpoint(wx, wy) {
  const thresh = 12 / zoom;
  for (const cn of state.connections) {
    const a = state.elements.find(e => e.id === cn.from);
    const b = state.elements.find(e => e.id === cn.to);
    if (!a || !b) continue;
    const {ax, ay} = anchorPoint(a, cn.fromEdge);
    const {ax: bx, ay: by} = anchorPoint(b, cn.toEdge);
    if (Math.hypot(wx-ax, wy-ay) < thresh) return { cn, which: 'from' };
    if (Math.hypot(wx-bx, wy-by) < thresh) return { cn, which: 'to'   };
  }
  return null;
}

function drawCnEndpointPreview() {
  if (!draggingCnEndpoint || linePreviewMx === null) return;
  const { cn, which } = draggingCnEndpoint;
  const fixed = state.elements.find(e => e.id === (which==='from' ? cn.to : cn.from));
  if (!fixed) return;
  const edge = which === 'from' ? cn.toEdge : cn.fromEdge;
  const {ax, ay} = anchorPoint(fixed, edge);
  const cfg = LINE_CFG[cn.type] || LINE_CFG.network;
  ctx.save();
  ctx.strokeStyle = cfg.cor;
  ctx.lineWidth   = cfg.width / zoom;
  ctx.setLineDash([6/zoom, 4/zoom]);
  ctx.globalAlpha = 0.55;
  ctx.beginPath();
  if (which === 'from') { ctx.moveTo(linePreviewMx, linePreviewMy); ctx.lineTo(ax, ay); }
  else                  { ctx.moveTo(ax, ay); ctx.lineTo(linePreviewMx, linePreviewMy); }
  ctx.stroke();
  ctx.restore();
}

function drawConnection(cn) {
  const a = state.elements.find(e => e.id === cn.from);
  const b = state.elements.find(e => e.id === cn.to);
  if (!a || !b) return;
  const cfg       = LINE_CFG[cn.type] || LINE_CFG.network;
  const isHov     = hoverCnId    === cn.id;
  const isSel     = selectedCnId === cn.id;
  // Overrides por conexão
  const cnCor     = cn.color || cfg.cor;
  const cnWidth   = cn.strokeWidth || cfg.width;
  const cnArrow   = cn.arrow !== undefined && cn.arrow !== null ? cn.arrow : cfg.arrow;
  const cnDash    = cn.dash ? (
    cn.dash === 'solid'  ? [] :
    cn.dash === 'dotted' ? [2/zoom, 4/zoom] :
    [8/zoom, 4/zoom]   // dashed
  ) : cfg.dash.map(d => d/zoom);
  const drawColor = isSel ? '#ff3b30' : cnCor;

  const pts = getConnectionPts(cn);
  if (pts.length < 2) return;

  // Ponto médio (para label e badge)
  const mIdx = Math.floor((pts.length - 1) / 2);
  const mx   = (pts[mIdx].x + pts[mIdx+1].x) / 2;
  const my   = (pts[mIdx].y + pts[mIdx+1].y) / 2;

  ctx.save();
  ctx.lineCap  = 'round'; ctx.lineJoin = 'round';
  ctx.strokeStyle = drawColor;
  ctx.lineWidth   = (isSel ? cnWidth*2.5 : isHov ? cnWidth*2 : cnWidth) / zoom;
  ctx.setLineDash(isSel ? [] : cnDash);
  ctx.globalAlpha = (isSel || isHov) ? 1 : 0.82;

  // Polyline ortogonal com cantos arredondados
  const R = 8 / zoom;
  ctx.beginPath();
  ctx.moveTo(pts[0].x, pts[0].y);
  for (let i = 1; i < pts.length - 1; i++) {
    const P = pts[i-1], C = pts[i], N = pts[i+1];
    const d1 = Math.hypot(C.x-P.x, C.y-P.y);
    const d2 = Math.hypot(N.x-C.x, N.y-C.y);
    const r  = Math.min(R, d1/2, d2/2);
    if (r < 0.5/zoom || d1 < 0.1 || d2 < 0.1) {
      ctx.lineTo(C.x, C.y);
    } else {
      const dx1 = (C.x-P.x)/d1, dy1 = (C.y-P.y)/d1;
      const dx2 = (N.x-C.x)/d2, dy2 = (N.y-C.y)/d2;
      ctx.lineTo(C.x - dx1*r, C.y - dy1*r);
      ctx.quadraticCurveTo(C.x, C.y, C.x + dx2*r, C.y + dy2*r);
    }
  }
  ctx.lineTo(pts[pts.length-1].x, pts[pts.length-1].y);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.globalAlpha = 1;

  // Seta na ponta B
  if (cnArrow) {
    const last = pts[pts.length-1], prev = pts[pts.length-2];
    const angle = Math.atan2(last.y - prev.y, last.x - prev.x);
    ctx.fillStyle = drawColor; ctx.globalAlpha = isHov ? 1 : 0.90;
    const al = 11/zoom;
    ctx.beginPath();
    ctx.moveTo(last.x, last.y);
    ctx.lineTo(last.x - al*Math.cos(angle-0.40), last.y - al*Math.sin(angle-0.40));
    ctx.lineTo(last.x - al*Math.cos(angle+0.40), last.y - al*Math.sin(angle+0.40));
    ctx.closePath(); ctx.fill();
    ctx.globalAlpha = 1;
  }

  // Label
  const labelCn = cn.label || (isHov || isSel ? cfg.label : '');
  if (labelCn) {
    const fs = 9;
    ctx.font = `600 ${fs}px -apple-system,sans-serif`;
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    const tw = ctx.measureText(labelCn).width + 8/zoom;
    const th = fs * 1.4;
    ctx.fillStyle = isSel ? 'rgba(255,59,48,.15)' : hexAlpha(cnCor, .13);
    ctx.beginPath(); ctx.roundRect(mx - tw/2, my - th/2, tw, th, 4/zoom); ctx.fill();
    ctx.fillStyle = drawColor;
    ctx.fillText(labelCn, mx, my);
  }

  // Handles de segmento quando selecionado — círculos nos midpoints de cada segmento
  if (isSel) {
    for (let i = 0; i < pts.length - 1; i++) {
      const segLen = Math.hypot(pts[i+1].x-pts[i].x, pts[i+1].y-pts[i].y);
      if (segLen < 10/zoom) continue;
      const smx = (pts[i].x + pts[i+1].x) / 2;
      const smy = (pts[i].y + pts[i+1].y) / 2;
      ctx.beginPath(); ctx.arc(smx, smy, 5/zoom, 0, Math.PI*2);
      ctx.fillStyle = '#fff'; ctx.fill();
      ctx.strokeStyle = cnCor; ctx.lineWidth = 1.5/zoom; ctx.stroke();
      ctx.beginPath(); ctx.arc(smx, smy, 2/zoom, 0, Math.PI*2);
      ctx.fillStyle = cnCor; ctx.fill();
    }
  }

  ctx.restore();

  // Badge em coordenadas de tela quando selecionada
  if (isSel) {
    const sc = toScreen(mx, my);
    ctx.save();
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    ctx.font = '600 11px -apple-system,sans-serif';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    const badge = '× Delete para excluir  |  Duplo-clique para auto-rotear';
    const bwid  = ctx.measureText(badge).width + 12;
    const bht   = 20;
    ctx.fillStyle = 'rgba(255,59,48,.92)';
    ctx.beginPath(); ctx.roundRect(sc.x - bwid/2, sc.y - 22 - bht/2, bwid, bht, 5); ctx.fill();
    ctx.fillStyle = '#fff';
    ctx.fillText(badge, sc.x, sc.y - 22);
    ctx.restore();
  }
}

/* ── Preview de linha em progresso ──────────────────────────────── */
function drawLinePreview() {
  if (!modoLinha || !lineStart || linePreviewMx === null) return;
  const el = state.elements.find(e => e.id === lineStart.id);
  if (!el) return;
  const { ax: cx, ay: cy } = anchorPoint(el, lineStart.fromEdge);
  const cfg = LINE_CFG[modoLinha];
  ctx.save();
  ctx.strokeStyle = cfg.cor;
  ctx.lineWidth   = cfg.width/zoom;
  ctx.setLineDash([6/zoom, 4/zoom]);
  ctx.globalAlpha = 0.55;
  ctx.beginPath();
  ctx.moveTo(cx, cy); ctx.lineTo(linePreviewMx, linePreviewMy);
  ctx.stroke();
  ctx.restore();
}

/* ── Rubber band ─────────────────────────────────────────────────── */
function drawRubberBand() {
  if (!rubberBand) return;
  const {x0,y0,x1,y1} = rubberBand;
  ctx.save();
  ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  ctx.strokeStyle = '#0071e3';
  ctx.fillStyle   = 'rgba(0,113,227,.06)';
  ctx.lineWidth   = 1.5;
  ctx.setLineDash([5,3]);
  ctx.beginPath();
  ctx.rect(Math.min(x0,x1), Math.min(y0,y1), Math.abs(x1-x0), Math.abs(y1-y0));
  ctx.fill(); ctx.stroke();
  ctx.restore();
}

/* ================================================================
   HIT TESTS
   ================================================================ */
function hitTestElement(wx, wy) {
  // Padding de toque (maior área de clique, especialmente útil em touch)
  const PAD = 6 / zoom;
  const sorted = [...state.elements].sort((a,b) => (b.zIndex??0) - (a.zIndex??0));
  for (const el of sorted) {
    if (el.type === 'linha') {
      const x2 = el.x2 ?? el.x+150, y2 = el.y2 ?? el.y;
      const thresh = ((el.strokeWidth||2)/2 + 8) / zoom;
      const dx = x2-el.x, dy = y2-el.y, len2 = dx*dx+dy*dy;
      if (len2 === 0) { if (Math.hypot(wx-el.x,wy-el.y)<thresh) return el; continue; }
      const t = Math.max(0, Math.min(1, ((wx-el.x)*dx+(wy-el.y)*dy)/len2));
      if (Math.hypot(wx-(el.x+t*dx), wy-(el.y+t*dy)) < thresh) return el;
      continue;
    }
    const w = el.width  || (el.type==='circulo'?120 : el.type==='camera'?56 : IS_FORMA(el.type)?200 : 60);
    const h = el.height || (el.type==='circulo'?120 : el.type==='camera'?56 : IS_FORMA(el.type)?140 : 60);
    if (el.type === 'camera') {
      const r   = Math.min(w, h) / 2;
      const _cx = el.x + w/2, _cy = el.y + h/2;
      // Corpo circular
      if (Math.hypot(wx - _cx, wy - _cy) <= r + PAD) return el;
      // Setor FOV
      const fovHalf = ((el.fovAngle ?? 60) / 2) * Math.PI / 180;
      const fovDir  = (el.fovDir ?? 0) * Math.PI / 180;
      const fovLen  = r * 2.8;
      const dist    = Math.hypot(wx - _cx, wy - _cy);
      if (dist <= fovLen + PAD) {
        let diff = Math.atan2(wy - _cy, wx - _cx) - fovDir;
        while (diff >  Math.PI) diff -= Math.PI * 2;
        while (diff < -Math.PI) diff += Math.PI * 2;
        if (Math.abs(diff) <= fovHalf) return el;
      }
    } else if (el.type === 'circulo') {
      // círculo: rotação não altera hit (simétrico)
      const cx = el.x+w/2, cy = el.y+h/2;
      const rx = w/2 + PAD, ry = h/2 + PAD;
      if (((wx-cx)/rx)**2 + ((wy-cy)/ry)**2 <= 1) return el;
    } else {
      // retângulo/card: rotacionar o ponto de teste de volta para o espaço local
      const rot = el.rotation || 0;
      let lx = wx, ly = wy;
      if (rot) {
        const cx = el.x + w/2, cy = el.y + h/2;
        const a  = -rot * Math.PI / 180;
        const dx = wx - cx, dy = wy - cy;
        lx = cx + dx*Math.cos(a) - dy*Math.sin(a);
        ly = cy + dx*Math.sin(a) + dy*Math.cos(a);
      }
      if (lx >= el.x-PAD && lx <= el.x+w+PAD && ly >= el.y-PAD && ly <= el.y+h+PAD) return el;
    }
  }
  return null;
}

/* ── Roteamento Ortogonal ──────────────────────────────────────── */
function computeOrthoRoute(ax, ay, fromEdge, bx, by, toEdge) {
  // Retorna waypoints intermediários (sem incluir as âncoras ax,ay / bx,by)
  const STUB = 28;
  const dirs = { e:[1,0], w:[-1,0], s:[0,1], n:[0,-1] };
  const fd   = dirs[fromEdge] || (bx >= ax ? [1,0] : [-1,0]);
  const td   = dirs[toEdge]   || (ax >= bx ? [1,0] : [-1,0]);
  const p1x  = ax + fd[0]*STUB, p1y = ay + fd[1]*STUB;
  const p2x  = bx + td[0]*STUB, p2y = by + td[1]*STUB;
  const hout = fd[1] === 0;   // saída horizontal (e/w)
  const hin  = td[1] === 0;   // entrada horizontal (e/w)
  const bends = [];
  if (hout && hin) {
    const midX = (p1x + p2x) / 2;
    bends.push({x: midX, y: p1y}, {x: midX, y: p2y});
  } else if (!hout && !hin) {
    const midY = (p1y + p2y) / 2;
    bends.push({x: p1x, y: midY}, {x: p2x, y: midY});
  } else if (hout) {
    bends.push({x: p2x, y: p1y});
  } else {
    bends.push({x: p1x, y: p2y});
  }
  return [{x:p1x,y:p1y}, ...bends, {x:p2x,y:p2y}];
}

/* ── Inteligência de feixe: várias conexões saindo da MESMA borda do MESMO
   elemento são distribuídas em leque ao longo da borda, para que fibras
   paralelas (ex.: várias saindo do CPD) fiquem lado a lado sem sobrepor. ── */
const FAN_GAP = 12;  // distância (mundo) entre fibras paralelas
function anchorSpread(el, edge, cnId, side) {
  const base = anchorPoint(el, edge);
  const sibs = [];
  for (const c of state.connections) {
    if (c.from === el.id && c.fromEdge === edge) sibs.push(c.id + '|from');
    if (c.to   === el.id && c.toEdge   === edge) sibs.push(c.id + '|to');
  }
  if (sibs.length <= 1) return base;
  sibs.sort();
  const idx = sibs.indexOf(cnId + '|' + side);
  if (idx < 0) return base;
  const w = el.width || 60, h = el.height || 60;
  const span = (edge === 'n' || edge === 's') ? w : h;
  const gap  = Math.min(FAN_GAP, (span * 0.78) / sibs.length);
  const off  = (idx - (sibs.length - 1) / 2) * gap;
  return (edge === 'n' || edge === 's')
    ? { ax: base.ax + off, ay: base.ay }
    : { ax: base.ax, ay: base.ay + off };
}

function getConnectionPts(cn) {
  const a = state.elements.find(e => e.id === cn.from);
  const b = state.elements.find(e => e.id === cn.to);
  if (!a || !b) return [];
  const {ax, ay}      = anchorSpread(a, cn.fromEdge, cn.id, 'from');
  const {ax:bx,ay:by} = anchorSpread(b, cn.toEdge,   cn.id, 'to');
  const wps = cn.waypoints || computeOrthoRoute(ax, ay, cn.fromEdge, bx, by, cn.toEdge);
  return [{x:ax,y:ay}, ...wps, {x:bx,y:by}];
}

function hitTestOrthoSegment(wx, wy) {
  if (!selectedCnId) return null;
  const cn = state.connections.find(c => c.id === selectedCnId);
  if (!cn) return null;
  const pts = getConnectionPts(cn);
  if (pts.length < 2) return null;
  const thresh = 9 / zoom;
  for (let i = 0; i < pts.length - 1; i++) {
    const segLen = Math.hypot(pts[i+1].x-pts[i].x, pts[i+1].y-pts[i].y);
    if (segLen < 10/zoom) continue;
    const smx = (pts[i].x + pts[i+1].x) / 2;
    const smy = (pts[i].y + pts[i+1].y) / 2;
    if (Math.hypot(wx - smx, wy - smy) < thresh) return { cn, segIdx: i };
  }
  return null;
}

function hitTestHandle(wx, wy) {
  if (selectionIds.size !== 1) return null;
  const el = state.elements.find(e => selectionIds.has(e.id));
  if (!el || el.locked) return null;
  const thresh = (HS + 4) / zoom;
  if (el.type === 'linha') {
    const x2 = el.x2 ?? el.x+150, y2 = el.y2 ?? el.y;
    if (Math.hypot(wx-el.x, wy-el.y) < thresh) return 'linha_a';
    if (Math.hypot(wx-x2,   wy-y2)   < thresh) return 'linha_b';
    return null;
  }
  for (const name of HANDLES) {
    const {wx: hx, wy: hy} = handlePos(el, name);
    if (Math.abs(wx-hx) < thresh && Math.abs(wy-hy) < thresh) return name;
  }
  return null;
}

function hitTestConnection(wx, wy) {
  const thresh = 9/zoom;
  for (let i = state.connections.length-1; i >= 0; i--) {
    const cn  = state.connections[i];
    const pts = getConnectionPts(cn);
    if (pts.length < 2) continue;
    for (let s = 0; s < pts.length - 1; s++) {
      const A = pts[s], B = pts[s+1];
      const dx = B.x-A.x, dy = B.y-A.y, len2 = dx*dx+dy*dy;
      if (len2 < 0.001) continue;
      const t = Math.max(0, Math.min(1, ((wx-A.x)*dx + (wy-A.y)*dy) / len2));
      if (Math.hypot(wx-(A.x+t*dx), wy-(A.y+t*dy)) < thresh) return cn;
    }
  }
  return null;
}

/* ================================================================
   EVENTOS DO CANVAS
   ================================================================ */
canvas.addEventListener('mousedown', e => {
  const rect = canvas.getBoundingClientRect();
  const cx = e.clientX - rect.left;
  const cy = e.clientY - rect.top;
  const { x: wx, y: wy } = toWorld(cx, cy);

  /* ── Modo Linha ── */
  if (modoLinha) {
    const hit = hitTestElement(wx, wy);
    if (hit) {
      const edge = hitAnchorEdge(hit, wx, wy);
      if (!lineStart) {
        lineStart = { id: hit.id, fromEdge: edge };
        document.getElementById('modeLabel').textContent = 'Clique no 2º elemento';
      } else if (lineStart.id !== hit.id) {
        const fromEl = state.elements.find(e => e.id === lineStart.id);
        const toEl   = hit;
        // Auto-escolhe a borda voltada para o outro equipamento quando o clique
        // não caiu exatamente sobre uma âncora (n/s/e/w).
        const fc = anchorPoint(fromEl, null);
        const tc = anchorPoint(toEl, null);
        const fromEdge = lineStart.fromEdge || bestEdge(fromEl, tc.ax, tc.ay);
        const toEdge   = edge               || bestEdge(toEl,  fc.ax, fc.ay);
        // Permite MÚLTIPLAS conexões entre os mesmos equipamentos (ex.: 2 fibras
        // redundantes ligando ao mesmo equipamento). O leque (anchorSpread)
        // mantém todas lado a lado, sem sobrepor.
        state.connections.push({ id:'c'+Date.now()+Math.random().toString(36).slice(2,5), from:lineStart.id, fromEdge:fromEdge, to:hit.id, toEdge:toEdge, type:modoLinha });
        pushUndo(); agendarSave();
        lineStart = null;
        document.getElementById('modeLabel').textContent = 'Clique no 1º elemento';
      }
      render();
    }
    return;
  }

  /* ── Drag de endpoint de conexão ── */
  if (!modoLinha && e.button === 0) {
    const epHit = hitTestConnectionEndpoint(wx, wy);
    if (epHit) {
      draggingCnEndpoint = epHit;
      canvas.style.cursor = 'grabbing';
      return;
    }
  }

  /* ── Drag de segmento ortogonal ── */
  if (!modoLinha && e.button === 0) {
    const segHit = hitTestOrthoSegment(wx, wy);
    if (segHit) {
      draggingOrthoSeg = segHit;
      canvas.style.cursor = 'grabbing';
      return;
    }
  }

  /* ── Handle de resize ── */
  const handle = hitTestHandle(wx, wy);
  if (handle) {
    resizeHandle = handle;
    resizeEl = state.elements.find(e => selectionIds.has(e.id));
    resizeSnap = { x: resizeEl.x, y: resizeEl.y, w: resizeEl.width||60, h: resizeEl.height||60 };
    resizeMouse0 = { wx, wy };
    return;
  }

  /* ── Clique em âncora de conexão selecionada → muda fromEdge/toEdge ── */
  if (selectedCnId && !e.shiftKey) {
    const selCn = state.connections.find(c => c.id === selectedCnId);
    if (selCn) {
      const fromEl = state.elements.find(e => e.id === selCn.from);
      const toEl   = state.elements.find(e => e.id === selCn.to);
      for (const [el, which] of [[fromEl, 'from'], [toEl, 'to']]) {
        if (!el) continue;
        const edge = hitAnchorEdge(el, wx, wy);
        if (edge) {
          if (which === 'from') selCn.fromEdge = edge;
          else                  selCn.toEdge   = edge;
          pushUndo(); agendarSave(); render(); return;
        }
      }
    }
  }

  /* ── Elemento ── */
  const hit = hitTestElement(wx, wy);

  /* Quando a forma é o alvo mas há conexão por cima, a conexão tem prioridade */
  if (hit && IS_FORMA(hit.type)) {
    const cnSobreForma = hitTestConnection(wx, wy);
    if (cnSobreForma) {
      if (e.shiftKey) {
        state.connections = state.connections.filter(c => c.id !== cnSobreForma.id);
        selectedCnId = null; pushUndo(); agendarSave();
      } else {
        selectedCnId = (selectedCnId === cnSobreForma.id) ? null : cnSobreForma.id;
        selectionIds.clear(); fecharPainel(); updateSelBar();
        if (selectedCnId) atualizarPainelConexao(cnSobreForma);
        else fecharPainelConexao();
      }
      hoverCnId = cnSobreForma.id;
      render(); return;
    }
  }

  if (hit) {
    if (e.shiftKey) {
      // Shift+click: toggle seleção individual (ignora grupo)
      if (selectionIds.has(hit.id)) selectionIds.delete(hit.id);
      else selectionIds.add(hit.id);
    } else {
      if (!selectionIds.has(hit.id)) {
        selectionIds.clear();
        // Clique num membro de grupo → seleciona todos do grupo
        if (hit.groupId) {
          state.elements.filter(e => e.groupId === hit.groupId).forEach(e => selectionIds.add(e.id));
        } else {
          selectionIds.add(hit.id);
        }
      }
    }
    atualizarPainel(hit);
    // Preparar multi-drag (ignora elementos travados)
    selectedCnId = null;
    isDragging = true; dragStarted = false;
    dragStartCx = cx; dragStartCy = cy;
    dragOffsets = [];
    selectionIds.forEach(id => {
      const el = state.elements.find(e => e.id === id);
      if (el && !el.locked) dragOffsets.push({ id, dx: wx - el.x, dy: wy - el.y });
    });
    if (!dragOffsets.length) { isDragging = false; }
    updateSelBar();
    render();
    return;
  }

  /* ── Conexão (clique perto de uma linha) ── */
  const hitCn = hitTestConnection(wx, wy);
  if (hitCn) {
    if (e.shiftKey) {
      // Shift+click: excluir imediatamente
      state.connections = state.connections.filter(c => c.id !== hitCn.id);
      selectedCnId = null;
      pushUndo(); agendarSave();
    } else {
      // Clique simples: selecionar conexão
      selectedCnId = (selectedCnId === hitCn.id) ? null : hitCn.id;
      selectionIds.clear(); fecharPainel(); updateSelBar();
      if (selectedCnId) atualizarPainelConexao(hitCn);
      else fecharPainelConexao();
    }
    hoverCnId = hitCn.id;
    render(); return;
  }

  /* ── Área vazia → rubber band ── */
  selectionIds.clear();
  selectedCnId = null;
  fecharPainel(); fecharPainelConexao();
  rubberBand = { x0: cx, y0: cy, x1: cx, y1: cy };
  rubberStart = { cx, cy };
  updateSelBar();
  render();
});

canvas.addEventListener('mousemove', e => {
  const rect = canvas.getBoundingClientRect();
  const cx = e.clientX - rect.left;
  const cy = e.clientY - rect.top;
  const { x: wx, y: wy } = toWorld(cx, cy);
  linePreviewMx = wx; linePreviewMy = wy;

  /* Drag endpoint de conexão */
  if (draggingCnEndpoint) { render(); return; }

  /* Drag de segmento ortogonal */
  if (draggingOrthoSeg) {
    const { cn, segIdx } = draggingOrthoSeg;
    // Garantir que cn.waypoints está populado a partir do auto-roteamento
    if (!cn.waypoints) {
      const _a = state.elements.find(e => e.id === cn.from);
      const _b = state.elements.find(e => e.id === cn.to);
      if (_a && _b) {
        const {ax:_ax,ay:_ay} = anchorPoint(_a, cn.fromEdge);
        const {ax:_bx,ay:_by} = anchorPoint(_b, cn.toEdge);
        cn.waypoints = computeOrthoRoute(_ax, _ay, cn.fromEdge, _bx, _by, cn.toEdge).map(p=>({...p}));
      }
    }
    if (cn.waypoints) {
      const pts  = getConnectionPts(cn);
      const A    = pts[segIdx], B = pts[segIdx+1];
      const isH  = Math.abs(A.y - B.y) <= Math.abs(A.x - B.x);
      // pts[k] → cn.waypoints[k-1] para k = 1 .. pts.length-2
      if (isH) {
        const sy = snapVal(wy);
        if (segIdx > 0 && segIdx - 1 < cn.waypoints.length)
          cn.waypoints[segIdx - 1].y = sy;
        if (segIdx < pts.length - 2 && segIdx < cn.waypoints.length)
          cn.waypoints[segIdx].y = sy;
      } else {
        const sx = snapVal(wx);
        if (segIdx > 0 && segIdx - 1 < cn.waypoints.length)
          cn.waypoints[segIdx - 1].x = sx;
        if (segIdx < pts.length - 2 && segIdx < cn.waypoints.length)
          cn.waypoints[segIdx].x = sx;
      }
    }
    render(); return;
  }

  /* Resize */
  if (resizeHandle) {
    applyResize(resizeHandle, wx, wy);
    updatePropSizeFields();
    agendarSave(); render(); return;
  }

  /* Drag */
  if (isDragging && dragOffsets.length) {
    const moved = Math.hypot(cx - dragStartCx, cy - dragStartCy);
    if (moved > 3) dragStarted = true;
    if (dragStarted) {
      const movedIds = new Set();
      dragOffsets.forEach(({ id, dx, dy, dx2, dy2 }) => {
        const el = state.elements.find(e => e.id === id);
        if (!el) return;
        const nx = snapVal(wx - dx), ny = snapVal(wy - dy);
        if (el.type === 'linha') {
          el.x2 = (el.x2 ?? el.x+150) + (nx - el.x);
          el.y2 = (el.y2 ?? el.y)      + (ny - el.y);
        }
        el.x = nx; el.y = ny;
        movedIds.add(id);
      });
      state.connections.forEach(cn => {
        if (movedIds.has(cn.from) || movedIds.has(cn.to)) cn.waypoints = null;
      });
      computeGuides();
      render(); return;
    }
  }

  /* Rubber band */
  if (rubberBand) {
    rubberBand.x1 = cx; rubberBand.y1 = cy;
    render(); return;
  }

  /* Pan (botão do meio ou Alt) */
  if (isPanning) {
    panX = e.clientX - panStart.x;
    panY = e.clientY - panStart.y;
    render(); return;
  }

  /* Hover */
  const handle = hitTestHandle(wx, wy);
  if (handle) { canvas.style.cursor = cursorForHandle(handle); return; }
  const hit = hitTestElement(wx, wy);
  const newHover = hit ? hit.id : null;
  if (newHover !== hoverId) { hoverId = newHover; render(); }
  const hitCn = hitTestConnection(wx, wy);
  const newHoverCn = hitCn ? hitCn.id : null;
  if (newHoverCn !== hoverCnId) { hoverCnId = newHoverCn; render(); }
  const epHover  = !modoLinha ? hitTestConnectionEndpoint(wx, wy) : null;
  const segHover = !modoLinha ? hitTestOrthoSegment(wx, wy) : null;
  canvas.style.cursor = handle    ? cursorForHandle(handle)
                      : segHover  ? 'grab'
                      : epHover   ? 'grab'
                      : hit       ? (modoLinha ? 'crosshair' : 'move')
                      : hitCn     ? 'pointer'
                      : modoLinha ? 'crosshair' : (isPanning ? 'grabbing' : 'default');
  if (modoLinha && lineStart) render();
});

canvas.addEventListener('mouseup', e => {
  const rect2 = canvas.getBoundingClientRect();
  const { x: wux, y: wuy } = toWorld(e.clientX - rect2.left, e.clientY - rect2.top);

  /* Finalizar drag de segmento ortogonal */
  if (draggingOrthoSeg) {
    pushUndo(); agendarSave();
    draggingOrthoSeg = null;
    canvas.style.cursor = 'default';
    render(); return;
  }

  /* Finalizar drag endpoint de conexão */
  if (draggingCnEndpoint) {
    const { cn, which } = draggingCnEndpoint;
    draggingCnEndpoint = null;
    const target = hitTestElement(wux, wuy);
    if (target && target.id !== (which==='from' ? cn.to : cn.from)) {
      const edge = hitAnchorEdge(target, wux, wuy);
      if (which === 'from') { cn.from = target.id; cn.fromEdge = edge; cn.waypoints = null; }
      else                  { cn.to   = target.id; cn.toEdge   = edge; cn.waypoints = null; }
      pushUndo(); agendarSave();
    }
    canvas.style.cursor = 'default';
    render(); return;
  }

  /* Finalizar resize */
  if (resizeHandle) {
    pushUndo(); agendarSave();
    resizeHandle = null; resizeEl = null; resizeSnap = null;
    return;
  }

  /* Finalizar drag */
  if (isDragging && dragStarted) {
    pushUndo(); agendarSave();
  }
  isDragging = false; dragStarted = false;
  dragOffsets = []; dragGuides = [];

  /* Finalizar rubber band */
  if (rubberBand) {
    const x0w = Math.min(rubberBand.x0, rubberBand.x1);
    const x1w = Math.max(rubberBand.x0, rubberBand.x1);
    const y0w = Math.min(rubberBand.y0, rubberBand.y1);
    const y1w = Math.max(rubberBand.y0, rubberBand.y1);
    if (Math.abs(x1w - x0w) > 5 || Math.abs(y1w - y0w) > 5) {
      state.elements.forEach(el => {
        let sx, sy, ex, ey;
        if (el.type === 'linha') {
          const x2 = el.x2 ?? el.x+150, y2 = el.y2 ?? el.y;
          const s1 = toScreen(el.x, el.y), s2 = toScreen(x2, y2);
          sx=Math.min(s1.x,s2.x); sy=Math.min(s1.y,s2.y);
          ex=Math.max(s1.x,s2.x); ey=Math.max(s1.y,s2.y);
        } else {
          const s = toScreen(el.x, el.y), e2 = toScreen(el.x+(el.width||60), el.y+(el.height||60));
          sx=s.x; sy=s.y; ex=e2.x; ey=e2.y;
        }
        if (sx < x1w && ex > x0w && sy < y1w && ey > y0w) selectionIds.add(el.id);
      });
      updateSelBar();
    }
    rubberBand = null; rubberStart = null;
    render();
  }

  isPanning = false;
});

/* Botão do meio / Alt+drag / Spacebar+drag = pan */
canvas.addEventListener('mousedown', e => {
  if (e.button === 1 || e.altKey || spacebarDown) {
    e.stopPropagation();
    isPanning = true;
    panStart = { x: e.clientX - panX, y: e.clientY - panY };
  }
}, true);

canvas.addEventListener('wheel', e => {
  if (_textareaEl) _textareaEl.blur(); // fecha edição antes de zoomar
  e.preventDefault();
  const rect = canvas.getBoundingClientRect();
  const cx = e.clientX - rect.left;
  const cy = e.clientY - rect.top;
  const wx = (cx - panX - VW/2) / zoom + state.canvas.width/2;
  const wy = (cy - panY - VH/2) / zoom + state.canvas.height/2;
  const factor = e.deltaY < 0 ? 1.1 : 0.9;
  zoom = Math.max(0.15, Math.min(4, zoom * factor));
  panX = cx - VW/2  - (wx - state.canvas.width/2)  * zoom;
  panY = cy - VH/2 - (wy - state.canvas.height/2) * zoom;
  document.getElementById('zoomLabel').textContent = Math.round(zoom*100)+'%';
  render();
}, { passive: false });

canvas.addEventListener('mouseleave', () => {
  isPanning = false; isDragging = false; dragGuides = [];
  if (rubberBand) { rubberBand = null; render(); }
});

/* Duplo clique = edição rápida */
canvas.addEventListener('dblclick', e => {
  const rect = canvas.getBoundingClientRect();
  const { x: wx, y: wy } = toWorld(e.clientX-rect.left, e.clientY-rect.top);

  // Duplo-clique no midpoint handle → reseta rota para auto-roteamento
  if (selectedCnId) {
    const segHit = hitTestOrthoSegment(wx, wy);
    if (segHit) {
      segHit.cn.waypoints = null;
      pushUndo(); agendarSave(); render(); return;
    }
  }

  // Verificar conexão antes de elemento
  const hitCn = hitTestConnection(wx, wy);
  if (hitCn) {
    const novoLabel = prompt('Rótulo da conexão:', hitCn.label || '');
    if (novoLabel !== null) { hitCn.label = novoLabel; pushUndo(); agendarSave(); render(); }
    return;
  }
  const hit = hitTestElement(wx, wy);
  if (hit) {
    if (hit.type === 'texto') { iniciarEdicaoTexto(hit); return; }
    atualizarPainel(hit);
    document.getElementById('propLabel').focus();
  }
});

/* Menu de contexto (clique direito) */
canvas.addEventListener('contextmenu', e => {
  e.preventDefault();
  fecharCtx();
  const rect = canvas.getBoundingClientRect();
  const { x: wx, y: wy } = toWorld(e.clientX-rect.left, e.clientY-rect.top);
  const hit = hitTestElement(wx, wy);
  if (hit && !selectionIds.has(hit.id)) {
    selectionIds.clear(); selectionIds.add(hit.id);
    atualizarPainel(hit); updateSelBar(); render();
  }
  const ctxLocked = hit && hit.locked;
  document.getElementById('ctxLockLabel').textContent = ctxLocked ? 'Destravar' : 'Travar';
  document.getElementById('ctxLockIcon').className = ctxLocked ? 'fa-solid fa-unlock' : 'fa-solid fa-lock';
  const menu = document.getElementById('ctxMenu');
  const px = Math.min(e.clientX, window.innerWidth  - 180);
  const py = Math.min(e.clientY, window.innerHeight - 200);
  menu.style.left = px + 'px'; menu.style.top = py + 'px';
  menu.style.display = 'block';
});

/* ── Teclado ─────────────────────────────────────────────────────── */
document.addEventListener('keydown', e => {
  const inp = ['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName);

  if (e.key === 'Escape') {
    if (modoLinha) sairModoLinha();
    else { limparSelecao(); selectedCnId = null; fecharPainel(); fecharCtx(); render(); }
  }

  // Spacebar pan (apenas quando não está num input)
  if (!inp && e.key === ' ' && !spacebarDown) {
    e.preventDefault(); spacebarDown = true;
    canvas.style.cursor = 'grab';
  }

  if (!inp) {
    // Delete / Backspace
    if (e.key === 'Delete' || e.key === 'Backspace') {
      if (selectedCnId) {
        state.connections = state.connections.filter(c => c.id !== selectedCnId);
        selectedCnId = null; pushUndo(); agendarSave(); render();
      } else { excluirSelecao(); }
    }
    // Ctrl+A
    if (e.key === 'a' && e.ctrlKey) { e.preventDefault(); state.elements.forEach(el => selectionIds.add(el.id)); updateSelBar(); render(); }
    // G — toggle snap
    if (e.key === 'g') { e.preventDefault(); toggleSnap(); }
    // R — girar +90°
    if (e.key === 'r' && selectionIds.size) { e.preventDefault(); rotarElemento(90); }
    // F — fit to screen
    if (e.key === 'f') { e.preventDefault(); fitToScreen(); }
    // Teclas de seta — mover seleção
    if (selectionIds.size > 0 && !modoLinha) {
      const step = e.shiftKey ? 10 : 1;
      if (e.key === 'ArrowLeft')  { e.preventDefault(); moverSelecao(-step, 0); }
      if (e.key === 'ArrowRight') { e.preventDefault(); moverSelecao( step, 0); }
      if (e.key === 'ArrowUp')    { e.preventDefault(); moverSelecao(0, -step); }
      if (e.key === 'ArrowDown')  { e.preventDefault(); moverSelecao(0,  step); }
    }
  }

  if (e.ctrlKey && e.key === 'z') { e.preventDefault(); undo(); }
  if (e.ctrlKey && e.key === 'y') { e.preventDefault(); redo(); }
  if (e.ctrlKey && e.shiftKey && e.key === 'S') { e.preventDefault(); salvarNovaVersao(); }
  else if (e.ctrlKey && e.key === 's') { e.preventDefault(); salvarLayout(); }
  if (e.ctrlKey && e.key === 'd') { e.preventDefault(); duplicarSelecao(); }
  if (e.ctrlKey && e.key === 'c') { e.preventDefault(); copiarSelecao(); }
  if (e.ctrlKey && e.key === 'v') { e.preventDefault(); colarSelecao(); }
});
document.addEventListener('keyup', e => {
  if (e.key === ' ') {
    spacebarDown = false;
    if (!isPanning) canvas.style.cursor = modoLinha ? 'crosshair' : 'default';
  }
});

/* ================================================================
   RESIZE DE ELEMENTOS
   ================================================================ */
function applyResize(handle, wx, wy) {
  if (!resizeEl || !resizeSnap) return;
  if (handle === 'linha_a' || handle === 'linha_b') {
    // Snap inteligente: grid → endpoints de outras linhas → arestas de elementos
    let sx = snapVal(wx), sy = snapVal(wy);
    const SNAP_T = 11 / zoom;
    for (const oe of state.elements) {
      if (oe.id === resizeEl.id) continue;
      if (oe.type === 'linha') {
        // Snap ao endpoint de outra linha
        const ox2 = oe.x2 ?? oe.x+150, oy2 = oe.y2 ?? oe.y;
        for (const [ex, ey] of [[oe.x,oe.y],[ox2,oy2]]) {
          if (Math.hypot(wx-ex, wy-ey) < SNAP_T) { sx = ex; sy = ey; break; }
        }
      } else {
        // Snap a arestas / centro de card ou forma
        const { xs: oX, ys: oY } = elSnapEdges(oe);
        for (const ox of oX) if (Math.abs(wx-ox) < SNAP_T) { sx = ox; break; }
        for (const oy of oY) if (Math.abs(wy-oy) < SNAP_T) { sy = oy; break; }
      }
    }
    if (handle === 'linha_a') { resizeEl.x  = sx; resizeEl.y  = sy; }
    else                       { resizeEl.x2 = sx; resizeEl.y2 = sy; }
    return;
  }
  const { x: ox, y: oy, w: ow, h: oh } = resizeSnap;
  const dw = wx - resizeMouse0.wx;
  const dh = wy - resizeMouse0.wy;
  const MIN = 40;
  let nx = ox, ny = oy, nw = ow, nh = oh;
  if (handle.includes('e')) { nw = Math.max(MIN, ow + dw); }
  if (handle.includes('w')) { const delta = Math.min(dw, ow-MIN); nx = ox+delta; nw = ow-delta; }
  if (handle.includes('s')) { nh = Math.max(MIN, oh + dh); }
  if (handle.includes('n')) { const delta = Math.min(dh, oh-MIN); ny = oy+delta; nh = oh-delta; }
  resizeEl.x = nx; resizeEl.y = ny; resizeEl.width = nw; resizeEl.height = nh;
}

function cursorForHandle(name) {
  if (name === 'linha_a' || name === 'linha_b') return 'crosshair';
  const map = { nw:'nw-resize', n:'n-resize', ne:'ne-resize', e:'e-resize', se:'se-resize', s:'s-resize', sw:'sw-resize', w:'w-resize' };
  return map[name] || 'default';
}

/* ================================================================
   ADICIONAR ELEMENTOS
   ================================================================ */
function addFromPalette(tipo, cor, label) {
  // Novo elemento aparece no centro da área visível (não no centro fixo do mundo),
  // para acompanhar onde o usuário está construindo — sem precisar "achar" o item.
  const c = toWorld(VW/2, VH/2);
  const halfW = IS_FORMA(tipo) ? (tipo==='circulo'?60:100) : 30;
  const halfH = IS_FORMA(tipo) ? (tipo==='circulo'?60:70)  : 30;
  const x = c.x - halfW + (Math.random()-.5)*30;
  const y = c.y - halfH + (Math.random()-.5)*30;
  adicionarElemento(tipo, cor, label, x, y);
  fecharPaletteMobile();
}

function adicionarElemento(tipo, cor, label, x, y) {
  const isLinha = tipo === 'linha';
  const w = isLinha ? 0 : (IS_FORMA(tipo) ? (tipo==='circulo'?120:200) : (tipo==='camera'?56 : tipo==='fonte'?40 : 60));
  const h = isLinha ? 0 : (IS_FORMA(tipo) ? (tipo==='circulo'?120:140) : (tipo==='camera'?56 : tipo==='fonte'?40 : 60));
  const allZ = state.elements.map(e => e.zIndex ?? 0);
  const nextZ = allZ.length ? (IS_FORMA(tipo) ? Math.min(...allZ) - 1 : Math.max(...allZ) + 1) : 0;
  const el = {
    id: 'e' + Date.now() + Math.random().toString(36).slice(2,6),
    type: tipo, label: label, x, y, width: w, height: h, zIndex: nextZ,
    color: cor, item_id: null, prtg_objid: null, ip: '', observacoes: '',
    fillOpacity: IS_FORMA(tipo) && !isLinha ? 0.12 : undefined,
    borderStyle: IS_FORMA(tipo) && !isLinha ? 'solid' : undefined,
  };
  if (isLinha) { el.x2 = x + 150; el.y2 = y; el.strokeWidth = 2; el.dash = 'solid'; el.arrowEnd = false; }
  state.elements.push(el);
  selectionIds.clear(); selectionIds.add(el.id);
  atualizarPainel(el); updateSelBar();
  pushUndo(); agendarSave(); render();
}

/* Drag da paleta */
function paletteDragStart(e) {
  paletteDrag = { tipo: e.currentTarget.dataset.tipo, cor: e.currentTarget.dataset.cor,
                  label: TIPOS[e.currentTarget.dataset.tipo]?.label || '' };
}
function paletteDrop(e) {
  if (!paletteDrag) return;
  const rect = canvas.getBoundingClientRect();
  const { x: wx, y: wy } = toWorld(e.clientX-rect.left, e.clientY-rect.top);
  adicionarElemento(paletteDrag.tipo, paletteDrag.cor, paletteDrag.label, wx, wy);
  paletteDrag = null;
}

/* ================================================================
   SELEÇÃO MÚLTIPLA
   ================================================================ */
function limparSelecao() {
  selectionIds.clear();
  updateSelBar();
}
function excluirSelecao() {
  if (!selectionIds.size) return;
  state.elements    = state.elements.filter(e => !selectionIds.has(e.id));
  state.connections = state.connections.filter(c => !selectionIds.has(c.from) && !selectionIds.has(c.to));
  selectionIds.clear(); fecharPainel(); updateSelBar();
  pushUndo(); agendarSave(); render();
}
function updateSelBar() {
  const bar = document.getElementById('selBar');
  const alignGroup = document.getElementById('alignGroup');
  const n = selectionIds.size;
  if (n > 1) {
    bar.classList.add('visible');
    document.getElementById('selCount').textContent = `${n} elementos selecionados`;
    alignGroup.style.display = '';
  } else {
    bar.classList.remove('visible');
    alignGroup.style.display = 'none';
  }
  document.getElementById('distribGroup').style.display = (n >= 3) ? '' : 'none';
}

/* ================================================================
   MODO LINHA
   ================================================================ */
function iniciarModoLinha(tipo) {
  modoLinha = tipo;
  lineStart  = null;
  limparSelecao(); fecharPainel();
  document.getElementById('lineTypeGroup').style.display = '';
  document.getElementById('modeBadge').classList.add('visible');
  document.getElementById('modeLabel').textContent = 'Clique no 1º elemento';
  canvas.style.cursor = 'crosshair';
  document.querySelectorAll('.pe-line-type-btn').forEach(b => { b.classList.remove('active'); b.style.background=''; b.style.color=''; });
  const btn = document.getElementById('lt-' + tipo);
  if (btn) { btn.classList.add('active'); btn.style.background = LINE_CFG[tipo].cor; btn.style.color='#fff'; }
}
function setLineTipo(tipo, btn) {
  modoLinha = tipo; lineStart = null;
  document.querySelectorAll('.pe-line-type-btn').forEach(b => { b.classList.remove('active'); b.style.background=''; b.style.color=''; });
  btn.classList.add('active'); btn.style.background = LINE_CFG[tipo].cor; btn.style.color='#fff';
}
function sairModoLinha() {
  modoLinha = null; lineStart = null;
  document.getElementById('lineTypeGroup').style.display = 'none';
  document.getElementById('modeBadge').classList.remove('visible');
  canvas.style.cursor = 'default'; render();
}

/* ================================================================
   PAINEL DE PROPRIEDADES (drawer oculto)
   ================================================================ */
let propsPinned = false;   // true quando o usuário abre manualmente (não fecha sozinho)
function _propsPanelEl() { return document.getElementById('propsPanel'); }
function syncPropsToggle() {
  const aberto = !!_propsPanelEl()?.classList.contains('open');
  document.getElementById('btnPropsToggle')?.classList.toggle('active', aberto);
}
function abrirPropsDrawer() { _propsPanelEl()?.classList.add('open'); syncPropsToggle(); }
function fecharPropsDrawer(forcar) {
  if (propsPinned && !forcar) return;     // não fecha sozinho se foi fixado pelo usuário
  if (forcar) propsPinned = false;
  _propsPanelEl()?.classList.remove('open'); syncPropsToggle();
}
function togglePropsDrawer() {
  const p = _propsPanelEl(); if (!p) return;
  if (p.classList.contains('open')) { p.classList.remove('open'); propsPinned = false; }
  else { p.classList.add('open'); propsPinned = true; }
  syncPropsToggle();
}

function atualizarPainel(el) {
  abrirPropsDrawer();
  document.getElementById('propsEmpty').style.display = 'none';
  document.getElementById('propsBody').style.display  = '';
  document.getElementById('btnDeleteEl').style.display = '';
  document.getElementById('propLabel').value = el.label || '';
  document.getElementById('propColor').value = el.color || '#0071e3';
  document.getElementById('propColorHex').textContent  = el.color || '#0071e3';

  // Estado de travamento
  const isLocked = !!el.locked;
  const btnLock  = document.getElementById('btnLock');
  btnLock.classList.toggle('locked', isLocked);
  document.getElementById('lockIcon').className  = isLocked ? 'fa-solid fa-lock' : 'fa-solid fa-unlock';
  document.getElementById('lockLabel').textContent = isLocked ? 'Destravar Posição' : 'Travar Posição';

  // Ocultar painel de conexão quando elemento é selecionado
  document.getElementById('cnPropsBody').style.display = 'none';

  const isForma = IS_FORMA(el.type);
  const isLinha = el.type === 'linha';
  const isArea  = isForma && !isLinha;
  document.getElementById('fieldOpacity').style.display = isArea ? '' : 'none';
  document.getElementById('fieldBorda').style.display   = isArea ? '' : 'none';
  document.getElementById('fieldSize').style.display    = isArea ? '' : 'none';
  document.getElementById('fieldRadius').style.display  = (el.type === 'quadro') ? '' : 'none';
  document.getElementById('fieldItem').style.display    = isForma ? 'none' : '';
  document.getElementById('fieldPrtg').style.display    = isForma ? 'none' : '';
  document.getElementById('fieldIp').style.display      = isForma ? 'none' : '';
  document.getElementById('fieldLinha').style.display   = isLinha ? '' : 'none';
  document.getElementById('fieldFov').style.display     = el.type === 'camera' ? '' : 'none';

  // FOV da câmera
  if (el.type === 'camera') {
    const fa = el.fovAngle ?? 60;
    const fd = el.fovDir   ?? 0;
    document.getElementById('propFovAngle').value = fa;
    document.getElementById('fovAngleVal').textContent  = fa + '°';
    document.getElementById('propFovDir').value   = fd;
    const dirs = ['→','↗','↑','↖','←','↙','↓','↘'];
    document.getElementById('fovDirVal').textContent = fd + '° ' + dirs[Math.round(fd/45)%8];
  }

  // Fonte (disponível para todos os tipos)
  const ff = el.fontFamily || 'system';
  document.getElementById('propFontFamily').value = ff;
  document.getElementById('propFontSize').value   = el.fontSize || (el.type==='texto'?14:9);
  document.getElementById('btnBold').classList.toggle('active', !!el.fontBold);
  document.getElementById('btnItalic').classList.toggle('active', !!el.fontItalic);
  document.getElementById('fieldTextAlign').style.display = el.type === 'texto' ? '' : 'none';
  if (el.type === 'texto') syncAlignBtns(el);

  if (isLinha) {
    document.getElementById('propStroke').value          = el.strokeWidth || 2;
    document.getElementById('propDash').value            = el.dash || 'solid';
    document.getElementById('propArrow').checked         = !!el.arrowEnd;
    document.getElementById('propArrowStart').checked    = !!el.arrowStart;
  } else if (isForma) {
    document.getElementById('propOpacity').value = el.fillOpacity ?? 0.12;
    updatePropSizeFields(el);
    if (el.type === 'quadro') {
      const rv = el.cornerRadius ?? 10;
      document.getElementById('propRadius').value = rv;
      document.getElementById('radiusVal').textContent = rv;
    }
    // Borda
    if (isArea) {
      const bc = el.borderColor || el.color || '#0071e3';
      document.getElementById('propBorderColor').value     = bc;
      document.getElementById('propBorderColorHex').textContent = bc;
      const bw2 = el.borderWidth ?? 2;
      document.getElementById('propBorderWidth').value     = bw2;
      document.getElementById('borderWidthVal').textContent = bw2;
      document.getElementById('propBorderStyle').value     = el.borderStyle || 'solid';
    }
  } else {
    document.getElementById('propIp').value  = el.ip || '';
    if (el.item_id) {
      document.getElementById('propItemSelecionado').style.display = '';
      document.getElementById('propItemNome').textContent = el._item_nome || `Item #${el.item_id}`;
      document.getElementById('propItemSearch').value = '';
    } else {
      document.getElementById('propItemSelecionado').style.display = 'none';
      document.getElementById('propItemSearch').value = '';
    }
    if (el.prtg_objid) {
      document.getElementById('propPrtgSelecionado').style.display = '';
      document.getElementById('propPrtgNome').textContent = el._prtg_nome || `Device #${el.prtg_objid}`;
      document.getElementById('propPrtgSearch').value = '';
    } else {
      document.getElementById('propPrtgSelecionado').style.display = 'none';
      document.getElementById('propPrtgSearch').value = '';
    }
  }
  document.getElementById('propObs').value = el.observacoes || '';
  // Rotação
  const _rv = el.rotation || 0;
  document.getElementById('propRotation').value       = _rv;
  document.getElementById('rotVal').textContent       = _rv + '°';
  document.getElementById('fieldRotation').style.display = (el.type === 'linha') ? 'none' : '';
  atualizarCamadaNum();
}
function updatePropSizeFields(elArg) {
  const el = elArg || state.elements.find(e => selectionIds.has(e.id));
  if (!el) return;
  document.getElementById('propWidth').value  = Math.round(el.width  || 60);
  document.getElementById('propHeight').value = Math.round(el.height || 60);
}
function fecharPainel() {
  document.getElementById('propsEmpty').style.display = '';
  document.getElementById('propsBody').style.display  = 'none';
  document.getElementById('btnDeleteEl').style.display = 'none';
  fecharPropsDrawer();   // some ao desselecionar (a menos que fixado pelo usuário)
}
function updateProp(campo, valor) {
  selectionIds.forEach(id => {
    const el = state.elements.find(e => e.id === id);
    if (el) el[campo] = valor;
  });
  if (campo === 'color') document.getElementById('propColorHex').textContent = valor;
  agendarSave(); render();
}
function excluirSelecionado() { excluirSelecao(); }

/* ── Painel de Conexão ───────────────────────────────────────────── */
function atualizarPainelConexao(cn) {
  abrirPropsDrawer();
  document.getElementById('propsEmpty').style.display  = 'none';
  document.getElementById('propsBody').style.display   = 'none';
  document.getElementById('btnDeleteEl').style.display = 'none';
  document.getElementById('cnPropsBody').style.display = '';

  const cfg = LINE_CFG[cn.type] || LINE_CFG.network;
  const cor = cn.color || cfg.cor;

  document.getElementById('cnPropLabel').value     = cn.label || '';
  document.getElementById('cnPropColor').value     = cor;
  document.getElementById('cnPropColorHex').textContent = cor;
  document.getElementById('cnPropWidth').value     = cn.strokeWidth || cfg.width;
  document.getElementById('cnWidthVal').textContent = cn.strokeWidth || cfg.width;
  document.getElementById('cnPropDash').value      = cn.dash || '';

  const arrowEfetivo = cn.arrow !== undefined && cn.arrow !== null ? cn.arrow : cfg.arrow;
  document.getElementById('cnPropArrow').checked    = arrowEfetivo === true;
  document.getElementById('cnPropArrowOff').checked = cn.arrow === false;
}

function fecharPainelConexao() {
  document.getElementById('cnPropsBody').style.display = 'none';
  document.getElementById('propsEmpty').style.display  = '';
}

function updateCnProp(campo, valor) {
  const cn = state.connections.find(c => c.id === selectedCnId);
  if (!cn) return;
  if (valor === null) {
    delete cn[campo];
  } else {
    cn[campo] = valor;
  }
  if (campo === 'color') document.getElementById('cnPropColorHex').textContent = valor || '';
  // Sincronizar checkboxes de seta (mutuamente exclusivos)
  if (campo === 'arrow') {
    if (valor === true)  document.getElementById('cnPropArrowOff').checked = false;
    if (valor === false) document.getElementById('cnPropArrow').checked    = false;
  }
  agendarSave(); render();
}

function excluirConexaoSelecionada() {
  if (!selectedCnId) return;
  state.connections = state.connections.filter(c => c.id !== selectedCnId);
  selectedCnId = null;
  fecharPainelConexao();
  pushUndo(); agendarSave(); render();
}

function adjustFontSize(delta) {
  const inp = document.getElementById('propFontSize');
  const val = Math.max(6, Math.min(96, (+inp.value || 14) + delta));
  inp.value = val;
  updateProp('fontSize', val);
}
function toggleFontProp(campo) {
  const el = state.elements.find(e => selectionIds.has(e.id));
  if (!el) return;
  el[campo] = !el[campo];
  const btn = document.getElementById(campo === 'fontBold' ? 'btnBold' : 'btnItalic');
  btn.classList.toggle('active', !!el[campo]);
  agendarSave(); render();
}

function setTextAlign(align) {
  selectionIds.forEach(id => {
    const el = state.elements.find(e => e.id === id);
    if (el && el.type === 'texto') el.textAlign = align;
  });
  const el = state.elements.find(e => selectionIds.has(e.id));
  if (el) syncAlignBtns(el);
  agendarSave(); render();
}
function syncAlignBtns(el) {
  const align = el.textAlign || 'left';
  ['Left','Center','Right'].forEach(a => {
    const btn = document.getElementById('btnAlign' + a);
    if (btn) btn.classList.toggle('active', align === a.toLowerCase());
  });
}

/* ================================================================
   AUTOCOMPLETE — ITEM
   ================================================================ */
let acItemTimer = null;
function buscarItem(q) {
  clearTimeout(acItemTimer);
  const ac = document.getElementById('acItems');
  if (q.length < 2) { ac.style.display='none'; return; }
  acItemTimer = setTimeout(async () => {
    const d = await (await fetch(`${ITEM_SEARCH_URL}?q=${encodeURIComponent(q)}`)).json();
    if (!d.ok || !d.results.length) { ac.style.display='none'; return; }
    ac.innerHTML = d.results.map(i => `
      <div class="pe-autocomplete-item" onclick="selecionarItem(${i.id},'${jsesc(i.nome)}')">
        <div class="pe-ac-name">${i.nome}</div>
        <div class="pe-ac-sub">${i.marca||''} ${i.modelo||''} · ${i.serie||'s/série'}</div>
      </div>`).join('');
    ac.style.display = 'block';
  }, 280);
}
function selecionarItem(id, nome) {
  const el = state.elements.find(e => selectionIds.has(e.id));
  if (!el) return;
  el.item_id = id; el._item_nome = nome;
  // Preenche label automaticamente para equipamentos (formas e texto mantêm label livre)
  if (!IS_FORMA(el.type) && el.type !== 'texto') {
    const labelPadrao = TIPOS[el.type]?.label || '';
    if (!el.label || el.label === labelPadrao) {
      el.label = nome;
      document.getElementById('propLabel').value = nome;
    }
  }
  document.getElementById('propItemSelecionado').style.display = '';
  document.getElementById('propItemNome').textContent = nome;
  document.getElementById('propItemSearch').value = '';
  document.getElementById('acItems').style.display = 'none';
  render();
  agendarSave();
}
function limparItem(e) {
  e.preventDefault();
  const el = state.elements.find(e => selectionIds.has(e.id));
  if (el) { el.item_id = null; el._item_nome = null; }
  document.getElementById('propItemSelecionado').style.display = 'none';
  agendarSave();
}

/* ================================================================
   AUTOCOMPLETE — PRTG
   ================================================================ */
let acPrtgTimer = null;
function buscarPrtg(q) {
  clearTimeout(acPrtgTimer);
  const ac = document.getElementById('acPrtg');
  if (q.length < 2) { ac.style.display='none'; return; }
  acPrtgTimer = setTimeout(async () => {
    const d = await (await fetch(`${PRTG_SEARCH_URL}?q=${encodeURIComponent(q)}`)).json();
    if (!d.ok || !d.results.length) { ac.innerHTML='<div class="pe-autocomplete-item"><div class="pe-ac-sub">Nenhum dispositivo</div></div>'; ac.style.display='block'; return; }
    ac.innerHTML = d.results.map(dev => `
      <div class="pe-autocomplete-item" onclick="selecionarPrtg(${dev.objid},'${jsesc(dev.name)}')">
        <div class="pe-ac-name">${dev.name} <span class="pe-ac-badge ${dev.status_slug}">${dev.statustext||dev.status_slug}</span></div>
        <div class="pe-ac-sub">${dev.host||'sem IP'} · ${dev.group||''}</div>
      </div>`).join('');
    ac.style.display = 'block';
  }, 280);
}
function selecionarPrtg(objid, nome) {
  const el = state.elements.find(e => selectionIds.has(e.id));
  if (!el) return;
  el.prtg_objid = objid; el._prtg_nome = nome;
  // Preenche label automaticamente com o nome do dispositivo PRTG
  const labelPadrao = TIPOS[el.type]?.label || '';
  if (!el.label || el.label === labelPadrao) {
    el.label = nome;
    document.getElementById('propLabel').value = nome;
  }
  document.getElementById('propPrtgSelecionado').style.display = '';
  document.getElementById('propPrtgNome').textContent = nome;
  document.getElementById('propPrtgSearch').value = '';
  document.getElementById('acPrtg').style.display = 'none';
  render();
  agendarSave();
}
function limparPrtg(e) {
  e.preventDefault();
  const el = state.elements.find(e => selectionIds.has(e.id));
  if (el) { el.prtg_objid = null; el._prtg_nome = null; }
  document.getElementById('propPrtgSelecionado').style.display = 'none';
  agendarSave();
}

document.addEventListener('click', e => {
  if (!e.target.closest('#propItemSearch') && !e.target.closest('#acItems'))
    document.getElementById('acItems').style.display = 'none';
  if (!e.target.closest('#propPrtgSearch') && !e.target.closest('#acPrtg'))
    document.getElementById('acPrtg').style.display = 'none';
  if (!e.target.closest('#ctxMenu')) fecharCtx();
});
function fecharCtx() { document.getElementById('ctxMenu').style.display = 'none'; }

/* ── Paleta mobile ───────────────────────────────────────────────── */
function togglePaletteMobile() {
  const p = document.querySelector('.pe-palette');
  const o = document.getElementById('paletteOverlay');
  const open = p.classList.toggle('open');
  o.classList.toggle('open', open);
}
function fecharPaletteMobile() {
  document.querySelector('.pe-palette').classList.remove('open');
  document.getElementById('paletteOverlay').classList.remove('open');
}

/* ================================================================
   FUNDO
   ================================================================ */
function carregarFundo(input) {
  if (!input.files?.[0]) return;
  const reader = new FileReader();
  reader.onload = e => {
    bgImage = new Image();
    bgImage.onload = render;
    bgImage.src = e.target.result;
    bgVisible = true;
    document.getElementById('btnLimparFundo').style.display = '';
    document.getElementById('btnToggleFundo').style.display = '';
    document.getElementById('btnToggleFundo').classList.add('active');
    document.getElementById('btnToggleFundo').querySelector('i').className = 'fa-solid fa-eye';
  };
  reader.readAsDataURL(input.files[0]);
}
function limparFundo() {
  bgImage = null; bgVisible = false;
  document.getElementById('btnLimparFundo').style.display  = 'none';
  document.getElementById('btnToggleFundo').style.display  = 'none';
  render();
}
function toggleFundo() {
  bgVisible = !bgVisible;
  const btn  = document.getElementById('btnToggleFundo');
  btn.querySelector('i').className = bgVisible ? 'fa-solid fa-eye' : 'fa-solid fa-eye-slash';
  btn.classList.toggle('active', bgVisible);
  render();
}

/* ================================================================
   ZOOM
   ================================================================ */
function ajustarZoom(delta) {
  zoom = Math.max(0.15, Math.min(4, zoom + delta));
  document.getElementById('zoomLabel').textContent = Math.round(zoom*100)+'%';
  render();
}
function resetZoom() { zoom=1; panX=0; panY=0; ajustarZoom(0); }

/* ================================================================
   UNDO / REDO
   ================================================================ */
function pushUndo() {
  undoStack.push(JSON.stringify({ elements:state.elements, connections:state.connections }));
  if (undoStack.length > 40) undoStack.shift();
  redoStack = [];
}
function undo() {
  if (undoStack.length < 2) return;
  redoStack.push(undoStack.pop());
  const s = JSON.parse(undoStack[undoStack.length-1]);
  state.elements = s.elements; state.connections = s.connections;
  limparSelecao(); fecharPainel(); render(); agendarSave();
}
function redo() {
  if (!redoStack.length) return;
  const s = JSON.parse(redoStack.pop());
  state.elements = s.elements; state.connections = s.connections;
  undoStack.push(JSON.stringify(s));
  limparSelecao(); fecharPainel(); render(); agendarSave();
}

/* ================================================================
   CONTROLE DE CAMADAS
   ================================================================ */
function normalizarCamadas() {
  const sorted = [...state.elements].sort((a,b) => (a.zIndex??0) - (b.zIndex??0));
  sorted.forEach((el, i) => { el.zIndex = i; });
}
function camadaFrente() {
  const maxZ = Math.max(...state.elements.map(e => e.zIndex??0));
  selectionIds.forEach(id => { const el = state.elements.find(e => e.id===id); if(el) el.zIndex = maxZ+1; });
  normalizarCamadas(); atualizarCamadaNum(); pushUndo(); agendarSave(); render();
}
function camadaFundo() {
  const minZ = Math.min(...state.elements.map(e => e.zIndex??0));
  selectionIds.forEach(id => { const el = state.elements.find(e => e.id===id); if(el) el.zIndex = minZ-1; });
  normalizarCamadas(); atualizarCamadaNum(); pushUndo(); agendarSave(); render();
}
function camadaAvancar() {
  const sorted = [...state.elements].sort((a,b) => (a.zIndex??0)-(b.zIndex??0));
  selectionIds.forEach(id => {
    const el = state.elements.find(e => e.id===id); if(!el) return;
    const above = sorted.find(e => (e.zIndex??0) > (el.zIndex??0) && !selectionIds.has(e.id));
    if(above) { const tmp=above.zIndex??0; above.zIndex=el.zIndex??0; el.zIndex=tmp; }
  });
  normalizarCamadas(); atualizarCamadaNum(); pushUndo(); agendarSave(); render();
}
function camadaRecuar() {
  const sorted = [...state.elements].sort((a,b) => (b.zIndex??0)-(a.zIndex??0));
  selectionIds.forEach(id => {
    const el = state.elements.find(e => e.id===id); if(!el) return;
    const below = sorted.find(e => (e.zIndex??0) < (el.zIndex??0) && !selectionIds.has(e.id));
    if(below) { const tmp=below.zIndex??0; below.zIndex=el.zIndex??0; el.zIndex=tmp; }
  });
  normalizarCamadas(); atualizarCamadaNum(); pushUndo(); agendarSave(); render();
}
function atualizarCamadaNum() {
  if (selectionIds.size !== 1) return;
  const el = state.elements.find(e => selectionIds.has(e.id));
  if (!el) return;
  const sorted = [...state.elements].sort((a,b) => (a.zIndex??0)-(b.zIndex??0));
  // Contagem do topo: Camada 1 = frente/topo, Camada N = fundo (padrão Photoshop/Figma)
  const pos = sorted.length - sorted.findIndex(e => e.id === el.id);
  document.getElementById('camadaNum').textContent = `Camada ${pos} de ${state.elements.length}`;
}

/* ================================================================
   SALVAR / VERSÕES / HISTÓRICO
   ================================================================ */
function setIndicator(estado, msg) {
  const el = document.getElementById('saveIndicator');
  el.className = 'pe-save-indicator ' + estado;
  const icons = { saving:'fa-spinner fa-spin', saved:'fa-circle-check', error:'fa-triangle-exclamation' };
  el.innerHTML = `<i class="fa-solid ${icons[estado]||'fa-circle-check'}"></i> <span>${msg}</span>`;
}

function agendarSave() {
  if (conflitoPausado) return;
  clearTimeout(saveTimer);
  setIndicator('saving', 'Salvando...');
  saveTimer = setTimeout(() => salvarLayout(), 1500);
}

async function salvarLayout(opcoes = {}) {
  if (conflitoPausado && !opcoes.force) return;
  clearTimeout(saveTimer);
  setIndicator('saving', 'Salvando...');

  // Validação: remover conexões cujos endpoints não existem mais
  const ids = new Set(state.elements.map(e => e.id));
  state.connections = state.connections.filter(cn => ids.has(cn.from) && ids.has(cn.to));

  try {
    const r = await fetch(SALVAR_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF_TOKEN },
      body: JSON.stringify({
        elements:       state.elements,
        connections:    state.connections,
        canvas:         state.canvas,
        client_version: opcoes.force ? null : layoutVersion,
        nova_versao:    opcoes.novaVersao  || false,
        descricao:      opcoes.descricao   || '',
      }),
    });

    // Tratar respostas não-JSON (403, 500, páginas de erro HTML)
    const ct = r.headers.get('content-type') || '';
    if (!ct.includes('application/json')) {
      if (r.status === 403)      setIndicator('error', 'Sem permissão (403)');
      else if (r.status === 500) setIndicator('error', 'Erro interno (500)');
      else                       setIndicator('error', `Erro HTTP ${r.status}`);
      return;
    }

    let d;
    try { d = await r.json(); }
    catch { setIndicator('error', 'Resposta inválida do servidor'); return; }

    if (d.ok) {
      layoutVersion   = d.versao;
      conflitoPausado = false;
      document.getElementById('conflitoBanner').style.display = 'none';
      setIndicator('saved', `v${layoutVersion} — Tudo salvo`);
      if (opcoes.novaVersao) mostrarToast('Nova versão salva no histórico!');
    } else if (d.conflito) {
      conflitoPausado = true;
      mostrarConflito(d.versao_atual, d.editado_por);
    } else {
      setIndicator('error', d.erro || 'Erro ao salvar');
    }
  } catch {
    setIndicator('error', 'Falha de conexão');
  }
}

function salvarNovaVersao() {
  const desc = prompt('Descrição desta versão (opcional):', '');
  if (desc === null) return;
  salvarLayout({ novaVersao: true, descricao: desc.trim() });
}

function mostrarConflito(versaoAtual, editadoPor) {
  document.getElementById('conflitoMsg').textContent =
    `Versão ${versaoAtual} salva por "${editadoPor}". Você está editando a v${layoutVersion}. Autosave pausado.`;
  document.getElementById('conflitoBanner').style.display = 'flex';
  setIndicator('error', 'Conflito de versão');
}

function fecharConflito() {
  conflitoPausado = false;
  document.getElementById('conflitoBanner').style.display = 'none';
}

function forceSalvar() {
  conflitoPausado = false;
  salvarLayout({ force: true });
}

function descartarAlteracoes() {
  if (confirm('Recarregar a página e perder alterações não salvas?')) location.reload();
}

async function abrirHistorico() {
  const modal = document.getElementById('historicoModal');
  const lista = document.getElementById('historicoLista');
  modal.style.display = 'flex';
  lista.innerHTML = '<p style="color:#6e6e73;font-size:.85rem;text-align:center;padding:24px">Carregando...</p>';
  try {
    const r = await fetch(HISTORICO_URL);
    const d = await r.json();
    if (!d.ok || !d.historico.length) {
      lista.innerHTML = '<p style="color:#6e6e73;font-size:.85rem;text-align:center;padding:24px">Nenhuma versão salva ainda.<br>Use <i class="fa-solid fa-bookmark"></i> para criar entradas no histórico.</p>';
      return;
    }
    lista.innerHTML = d.historico.map(h => `
      <div class="pe-hist-row">
        <div class="pe-hist-badge">v${h.versao}</div>
        <div class="pe-hist-meta">
          <strong>${h.descricao || `Versão ${h.versao}`}</strong>
          <small>${h.salvo_em} · ${h.salvo_por} · ${h.n_elementos} elem · ${h.n_conexoes} conex.</small>
        </div>
        <button class="pe-hist-restore" onclick="restaurarVersao(${h.id})">
          <i class="fa-solid fa-rotate-left"></i> Restaurar
        </button>
      </div>
    `).join('');
  } catch {
    lista.innerHTML = '<p style="color:#ff3b30;font-size:.85rem;text-align:center;padding:24px">Erro ao carregar histórico.</p>';
  }
}

function fecharHistoricoModal(e) {
  if (!e || e.target === document.getElementById('historicoModal')) {
    document.getElementById('historicoModal').style.display = 'none';
  }
}

async function restaurarVersao(histId) {
  if (!confirm('Restaurar esta versão?\nO estado atual será salvo como nova versão antes de restaurar.')) return;
  try {
    const r = await fetch(`${RESTAURAR_BASE}${histId}/`, {
      method: 'POST',
      headers: { 'X-CSRFToken': CSRF_TOKEN },
    });
    const d = await r.json();
    if (d.ok) { mostrarToast('Versão restaurada! Recarregando...'); setTimeout(() => location.reload(), 1200); }
    else       mostrarToast('Erro ao restaurar versão.');
  } catch { mostrarToast('Falha de conexão.'); }
}

function mostrarToast(msg) {
  const t = document.getElementById('peToast');
  t.textContent = msg;
  t.style.opacity = '1';
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.style.opacity = '0'; }, 2800);
}

// Polling a cada 30s para detectar edições concorrentes
setInterval(async () => {
  if (conflitoPausado) return;
  try {
    const r = await fetch(CHECK_VERSION_URL);
    if (!r.ok) return;
    const d = await r.json();
    if (d.versao !== layoutVersion) {
      conflitoPausado = true;
      mostrarConflito(d.versao, d.editado_por);
    }
  } catch {}
}, 30000);

/* ================================================================
   UTILS
   ================================================================ */
function hexAlpha(hex, a) {
  try {
    const r = parseInt(hex.slice(1,3),16), g = parseInt(hex.slice(3,5),16), b = parseInt(hex.slice(5,7),16);
    return `rgba(${r},${g},${b},${a})`;
  } catch { return `rgba(0,113,227,${a})`; }
}
function jsesc(s) { return String(s).replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/"/g,'\\"'); }

/* ================================================================
   FASE 1.1 — ATALHOS DE TECLADO
   ================================================================ */
function duplicarSelecao() {
  if (!selectionIds.size) return;
  const newIds = new Set();
  const allZ = state.elements.map(e => e.zIndex ?? 0);
  const maxZ = allZ.length ? Math.max(...allZ) : 0;
  const minZ = allZ.length ? Math.min(...allZ) : 0;

  // Original vai para o fundo
  let offset = 0;
  state.elements.filter(e => selectionIds.has(e.id)).forEach(el => {
    el.zIndex = minZ - 1 - offset++;
  });

  // Cópia sobe para a frente
  state.elements.filter(e => selectionIds.has(e.id)).forEach(el => {
    const newEl = { ...el,
      id: 'e'+Date.now()+Math.random().toString(36).slice(2,6),
      x: el.x+20, y: el.y+20, zIndex: maxZ + 1 };
    if (el.type === 'linha') { newEl.x2 = (el.x2 ?? el.x+150)+20; newEl.y2 = (el.y2 ?? el.y)+20; }
    state.elements.push(newEl);
    newIds.add(newEl.id);
  });
  normalizarCamadas();
  selectionIds = newIds;
  if (selectionIds.size === 1) {
    const el = state.elements.find(e => selectionIds.has(e.id));
    if (el) atualizarPainel(el);
  }
  updateSelBar(); pushUndo(); agendarSave(); render();
}

function moverSelecao(dx, dy) {
  selectionIds.forEach(id => {
    const el = state.elements.find(e => e.id === id);
    if (!el) return;
    el.x += dx; el.y += dy;
    if (el.type === 'linha') { el.x2 = (el.x2 ?? el.x + 150) + dx; el.y2 = (el.y2 ?? el.y) + dy; }
  });
  render(); agendarSave();
}

function fitToScreen(maxZoom = 3) {
  if (!state.elements.length) { zoom=1; panX=0; panY=0; document.getElementById('zoomLabel').textContent='100%'; render(); return; }
  const pad = 60;
  const xs = state.elements.flatMap(e => e.type==='linha' ? [e.x, e.x2??e.x+150] : [e.x]);
  const ys = state.elements.flatMap(e => e.type==='linha' ? [e.y, e.y2??e.y]      : [e.y]);
  const xe = state.elements.flatMap(e => e.type==='linha' ? [e.x, e.x2??e.x+150] : [e.x+(e.width||60)]);
  const ye = state.elements.flatMap(e => e.type==='linha' ? [e.y, e.y2??e.y]      : [e.y+(e.height||60)]);
  const minX=Math.min(...xs), minY=Math.min(...ys), maxX=Math.max(...xe), maxY=Math.max(...ye);
  const zx = (VW - pad*2) / Math.max(1, maxX - minX);
  const zy = (VH - pad*2) / Math.max(1, maxY - minY);
  zoom = Math.max(0.15, Math.min(maxZoom, Math.min(zx, zy)));
  const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
  panX = -(cx - state.canvas.width/2)  * zoom;
  panY = -(cy - state.canvas.height/2) * zoom;
  document.getElementById('zoomLabel').textContent = Math.round(zoom*100)+'%';
  render();
}

/* ================================================================
   FASE 2.1 — GRID SNAP
   ================================================================ */
function toggleSnap() {
  snapGrid = !snapGrid;
  document.getElementById('btnSnap').classList.toggle('active', snapGrid);
}
function snapVal(v, grid=20) { return snapGrid ? Math.round(v/grid)*grid : v; }

/* ================================================================
   FASE 2.2 — COPIAR / COLAR
   ================================================================ */
function copiarSelecao() {
  if (!selectionIds.size) return;
  clipboard = state.elements.filter(e => selectionIds.has(e.id)).map(e => ({...e}));
}
function colarSelecao() {
  if (!clipboard?.length) return;
  const newIds = new Set();
  const allZ = state.elements.map(e => e.zIndex ?? 0);
  const maxZ = allZ.length ? Math.max(...allZ) : 0;
  clipboard.forEach(el => {
    const newEl = {...el, id:'e'+Date.now()+Math.random().toString(36).slice(2,6), x:el.x+30, y:el.y+30, zIndex: maxZ+1};
    if (el.type === 'linha') { newEl.x2 = (el.x2 ?? el.x+150)+30; newEl.y2 = (el.y2 ?? el.y)+30; }
    state.elements.push(newEl);
    newIds.add(newEl.id);
  });
  normalizarCamadas();
  selectionIds = newIds;
  if (selectionIds.size === 1) {
    const el = state.elements.find(e => selectionIds.has(e.id));
    if (el) atualizarPainel(el);
  }
  updateSelBar(); pushUndo(); agendarSave(); render();
}

/* ================================================================
   FASE 2.3 — ALINHAMENTO
   ================================================================ */
function alinhar(tipo) {
  const els = state.elements.filter(e => selectionIds.has(e.id));
  if (els.length < 2) return;
  const xs = els.map(e => e.x);
  const ys = els.map(e => e.y);
  const xe = els.map(e => e.x+(e.width||60));
  const ye = els.map(e => e.y+(e.height||60));
  const minX=Math.min(...xs), maxX=Math.max(...xe);
  const minY=Math.min(...ys), maxY=Math.max(...ye);
  const cX=(minX+maxX)/2, cY=(minY+maxY)/2;
  els.forEach(el => {
    const w=el.width||60, h=el.height||60;
    if (tipo==='left')    el.x = minX;
    if (tipo==='right')   el.x = maxX - w;
    if (tipo==='top')     el.y = minY;
    if (tipo==='bottom')  el.y = maxY - h;
    if (tipo==='centerH') el.x = cX - w/2;
    if (tipo==='centerV') el.y = cY - h/2;
  });
  pushUndo(); agendarSave(); render();
}

/* Distribuir com espaçamento igual entre as bordas (mantém 1º e último fixos) */
function distribuir(eixo) {
  const els = state.elements.filter(e => selectionIds.has(e.id) && e.type !== 'linha');
  if (els.length < 3) return;
  if (eixo === 'h') {
    els.sort((a,b) => a.x - b.x);
    const start = els[0].x;
    const end   = els[els.length-1].x + (els[els.length-1].width || 60);
    const soma  = els.reduce((s,e) => s + (e.width || 60), 0);
    const gap   = (end - start - soma) / (els.length - 1);
    let cur = start;
    els.forEach(el => { el.x = Math.round(cur); cur += (el.width || 60) + gap; });
  } else {
    els.sort((a,b) => a.y - b.y);
    const start = els[0].y;
    const end   = els[els.length-1].y + (els[els.length-1].height || 60);
    const soma  = els.reduce((s,e) => s + (e.height || 60), 0);
    const gap   = (end - start - soma) / (els.length - 1);
    let cur = start;
    els.forEach(el => { el.y = Math.round(cur); cur += (el.height || 60) + gap; });
  }
  pushUndo(); agendarSave(); render();
}

/* ================================================================
   FASE 1.2 — STATUS PRTG NO EDITOR
   ================================================================ */
const PRTG_STATUS_URL = "x";
async function carregarPrtgEditor() {
  if (!PRTG_OK) return;
  try {
    const d = await (await fetch(PRTG_STATUS_URL)).json();
    if (d.ok) { prtgEditorMap = d.devices_map; render(); }
  } catch {}
}

/* ================================================================
   FASE 3.1 — EXPORTAR PNG
   ================================================================ */
const PLANTA_NOME_EXPORT = "x";
async function exportarPNG() {
  const savedPanX=panX, savedPanY=panY, savedZoom=zoom;
  fitToScreen();
  try {
    const link = document.createElement('a');
    link.download = PLANTA_NOME_EXPORT + '.png';
    link.href = canvas.toDataURL('image/png');
    link.click();
  } catch {
    alert('Não foi possível exportar: imagem de fundo com restrição de origem. Remova o fundo para exportar.');
  }
  panX=savedPanX; panY=savedPanY; zoom=savedZoom;
  document.getElementById('zoomLabel').textContent = Math.round(savedZoom*100)+'%';
  render();
}

/* ================================================================
   TRAVAR / DESTRAVAR ELEMENTOS
   ================================================================ */
function toggleTravado() {
  selectionIds.forEach(id => {
    const el = state.elements.find(e => e.id === id);
    if (el) el.locked = !el.locked;
  });
  if (selectionIds.size === 1) {
    const el = state.elements.find(e => selectionIds.has(e.id));
    if (el) atualizarPainel(el);
  }
  pushUndo(); agendarSave(); render();
}

/* ================================================================
   EDIÇÃO INLINE DE TEXTO (duplo-clique em elemento Texto)
   ================================================================ */
let _textareaEl = null;

function iniciarEdicaoTexto(el) {
  if (_textareaEl) { _textareaEl.blur(); return; }
  selectionIds.clear(); selectionIds.add(el.id);
  atualizarPainel(el); updateSelBar(); render();

  const labelAntes = el.label || '';
  const sc  = toScreen(el.x, el.y);
  const fs  = Math.max(10, (el.fontSize || 14) * zoom);
  const pad = Math.round(9 * zoom);

  const ta = document.createElement('textarea');
  ta.value = labelAntes;
  Object.assign(ta.style, {
    position:    'absolute',
    left:        sc.x + 'px',
    top:         sc.y + 'px',
    minWidth:    Math.max(80, (el.width  || 60) * zoom) + 'px',
    minHeight:   Math.max(36, (el.height || 40) * zoom) + 'px',
    background:  'rgba(255,255,255,.97)',
    border:      `2px solid ${el.color || '#0071e3'}`,
    borderRadius: Math.round(7 * zoom) + 'px',
    padding:     pad + 'px',
    font:        elFont(el, 14, '600'),
    fontSize:    fs + 'px',
    color:       el.color || '#1d1d1f',
    lineHeight:  '1.38',
    resize:      'both',
    outline:     'none',
    zIndex:      '500',
    boxShadow:   '0 4px 22px rgba(0,0,0,.18)',
    boxSizing:   'border-box',
    overflow:    'hidden',
    whiteSpace:  'pre-wrap',
  });
  canvasWrap.appendChild(ta);
  _textareaEl = ta;
  ta.focus(); ta.select();

  function finalizar() {
    if (!_textareaEl) return;
    el.label = ta.value;
    document.getElementById('propLabel').value = ta.value;
    ta.remove();
    _textareaEl = null;
    pushUndo(); agendarSave(); render();
  }

  ta.addEventListener('blur', finalizar);
  ta.addEventListener('keydown', ev => {
    ev.stopPropagation(); // não acionar atalhos do editor enquanto edita
    if (ev.key === 'Escape') { ta.value = labelAntes; ta.blur(); }
    if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); finalizar(); }
  });
  ta.addEventListener('input', () => { el.label = ta.value; render(); });
}

/* ================================================================
   ROTAÇÃO
   ================================================================ */
function rotarElemento(delta) {
  selectionIds.forEach(id => {
    const el = state.elements.find(e => e.id === id);
    if (!el || el.type === 'linha') return;
    el.rotation = ((el.rotation || 0) + delta + 360) % 360;
  });
  if (selectionIds.size === 1) {
    const el = state.elements.find(e => selectionIds.has(e.id));
    if (el) {
      document.getElementById('propRotation').value = el.rotation || 0;
      document.getElementById('rotVal').textContent  = (el.rotation || 0) + '°';
    }
  }
  pushUndo(); agendarSave(); render();
}

/* ================================================================
   AGRUPAMENTO
   ================================================================ */
function agrupar() {
  if (selectionIds.size < 2) return;
  const gid = 'g' + (nextGroupId++);
  selectionIds.forEach(id => {
    const el = state.elements.find(e => e.id === id);
    if (el) el.groupId = gid;
  });
  pushUndo(); agendarSave(); render();
  updateSelBar();
}
function desagrupar() {
  selectionIds.forEach(id => {
    const el = state.elements.find(e => e.id === id);
    if (el) el.groupId = null;
  });
  pushUndo(); agendarSave(); render();
  updateSelBar();
}

/* ================================================================
   GUIAS INTELIGENTES (Smart Guides + Distâncias)
   ================================================================ */
// Retorna os pontos de snap (xs, ys) de qualquer elemento, incluindo linhas
function elSnapEdges(el) {
  if (el.type === 'linha') {
    const x2 = el.x2 ?? el.x+150, y2 = el.y2 ?? el.y;
    return { xs: [el.x, (el.x+x2)/2, x2], ys: [el.y, (el.y+y2)/2, y2] };
  }
  const w = el.width  || (el.type==='circulo'?120 : IS_FORMA(el.type)?200 : 60);
  const h = el.height || (el.type==='circulo'?120 : IS_FORMA(el.type)?140 : 60);
  return { xs: [el.x, el.x+w/2, el.x+w], ys: [el.y, el.y+h/2, el.y+h] };
}

function computeGuides() {
  dragGuides = [];
  if (!isDragging || !dragStarted || !selectionIds.size) return;
  const THRESH = 6 / zoom;
  // Linhas agora participam das guias de alinhamento
  const dragged = state.elements.filter(e => selectionIds.has(e.id));
  const others  = state.elements.filter(e => !selectionIds.has(e.id));
  if (!dragged.length || !others.length) return;

  const vSet = new Set(), hSet = new Set();
  dragged.forEach(del => {
    const { xs: dX, ys: dY } = elSnapEdges(del);
    others.forEach(oel => {
      const { xs: oX, ys: oY } = elSnapEdges(oel);
      for (const dx of dX) for (const ox of oX)
        if (Math.abs(dx - ox) < THRESH) vSet.add(ox);
      for (const dy of dY) for (const oy of oY)
        if (Math.abs(dy - oy) < THRESH) hSet.add(oy);
    });
  });
  vSet.forEach(x => dragGuides.push({ type: 'v', pos: x }));
  hSet.forEach(y => dragGuides.push({ type: 'h', pos: y }));

  // Distâncias entre o elemento arrastado e vizinhos próximos (apenas para cards/formas)
  const del = dragged[0];
  if (del.type === 'linha') return;
  const dw = del.width || 60, dh = del.height || 60;
  const dL = del.x, dR = del.x + dw, dT = del.y, dB = del.y + dh;
  const dCY = del.y + dh/2, dCX = del.x + dw/2;
  let bL=null, bR=null, bT=null, bBot=null;
  let dDistL=Infinity, dDistR=Infinity, dDistT=Infinity, dDistBot=Infinity;
  const MAX = 160;
  others.forEach(oel => {
    const ow = oel.width||60, oh = oel.height||60;
    const oR = oel.x+ow, oB = oel.y+oh;
    const vOvlp = oel.y < dB && oB > dT;
    const hOvlp = oel.x < dR && oR > dL;
    if (vOvlp) {
      if (oR <= dL && dL-oR < dDistL) { dDistL=dL-oR; bL={x1:oR,x2:dL,midY:dCY,gap:Math.round(dL-oR)}; }
      if (oel.x >= dR && oel.x-dR < dDistR) { dDistR=oel.x-dR; bR={x1:dR,x2:oel.x,midY:dCY,gap:Math.round(oel.x-dR)}; }
    }
    if (hOvlp) {
      if (oB <= dT && dT-oB < dDistT)   { dDistT=dT-oB;   bT={y1:oB,y2:dT,midX:dCX,gap:Math.round(dT-oB)}; }
      if (oel.y >= dB && oel.y-dB < dDistBot) { dDistBot=oel.y-dB; bBot={y1:dB,y2:oel.y,midX:dCX,gap:Math.round(oel.y-dB)}; }
    }
  });
  if (bL   && bL.gap   < MAX) dragGuides.push({ type:'distH', ...bL });
  if (bR   && bR.gap   < MAX) dragGuides.push({ type:'distH', ...bR });
  if (bT   && bT.gap   < MAX) dragGuides.push({ type:'distV', ...bT });
  if (bBot && bBot.gap < MAX) dragGuides.push({ type:'distV', ...bBot });
}

function drawGuides() {
  if (!dragGuides.length) return;
  ctx.save();
  ctx.setTransform(DPR, 0, 0, DPR, 0, 0);

  // Linhas de alinhamento (vermelho)
  ctx.strokeStyle = 'rgba(255,59,48,.8)';
  ctx.lineWidth = 1;
  ctx.setLineDash([5, 3]);
  dragGuides.filter(g => g.type === 'v' || g.type === 'h').forEach(g => {
    if (g.type === 'v') {
      const sc = toScreen(g.pos, 0);
      ctx.beginPath(); ctx.moveTo(sc.x, 0); ctx.lineTo(sc.x, VH); ctx.stroke();
    } else {
      const sc = toScreen(0, g.pos);
      ctx.beginPath(); ctx.moveTo(0, sc.y); ctx.lineTo(VW, sc.y); ctx.stroke();
    }
  });
  ctx.setLineDash([]);

  // Setas de distância horizontal (azul)
  ctx.strokeStyle = '#0071e3';
  ctx.fillStyle   = '#0071e3';
  ctx.lineWidth   = 1.5;
  dragGuides.filter(g => g.type === 'distH').forEach(g => {
    if (g.x2 - g.x1 < 4) return;
    const s1 = toScreen(g.x1, g.midY), s2 = toScreen(g.x2, g.midY);
    ctx.beginPath(); ctx.moveTo(s1.x, s1.y); ctx.lineTo(s2.x, s2.y); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(s1.x, s1.y-4); ctx.lineTo(s1.x, s1.y+4); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(s2.x, s2.y-4); ctx.lineTo(s2.x, s2.y+4); ctx.stroke();
    const mx = (s1.x+s2.x)/2, my = s1.y;
    const lbl = g.gap + 'px';
    ctx.font = 'bold 10px -apple-system,sans-serif';
    const tw = ctx.measureText(lbl).width + 6;
    ctx.fillStyle = 'rgba(255,255,255,.92)';
    ctx.fillRect(mx - tw/2, my - 15, tw, 13);
    ctx.fillStyle = '#0071e3';
    ctx.textAlign = 'center'; ctx.textBaseline = 'bottom';
    ctx.fillText(lbl, mx, my - 3);
  });

  // Setas de distância vertical
  dragGuides.filter(g => g.type === 'distV').forEach(g => {
    if (g.y2 - g.y1 < 4) return;
    const s1 = toScreen(g.midX, g.y1), s2 = toScreen(g.midX, g.y2);
    ctx.strokeStyle = '#0071e3'; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(s1.x, s1.y); ctx.lineTo(s2.x, s2.y); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(s1.x-4, s1.y); ctx.lineTo(s1.x+4, s1.y); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(s2.x-4, s2.y); ctx.lineTo(s2.x+4, s2.y); ctx.stroke();
    const mx = s1.x, my = (s1.y+s2.y)/2;
    const lbl = g.gap + 'px';
    ctx.font = 'bold 10px -apple-system,sans-serif';
    const tw = ctx.measureText(lbl).width + 6;
    ctx.fillStyle = 'rgba(255,255,255,.92)';
    ctx.fillRect(mx + 5, my - 7, tw, 14);
    ctx.fillStyle = '#0071e3';
    ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
    ctx.fillText(lbl, mx + 8, my);
  });

  ctx.restore();
}

/* ================================================================
   MINIMAPA / NAVEGADOR
   ================================================================ */
const MM      = document.getElementById('miniMap');
const mmCtx   = MM.getContext('2d');
const MM_W = 196, MM_H = 132;
let _mmView   = null;     // {minX,minY,s,offX,offY} — definido a cada draw
let _mmDrag   = false;

(function mmSetup() {
  const dpr = window.devicePixelRatio || 1;
  MM.width  = MM_W * dpr;
  MM.height = MM_H * dpr;
  mmCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
})();

function contentBounds() {
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  state.elements.forEach(el => {
    const isL = el.type === 'linha';
    const ex2 = isL ? (el.x2 ?? el.x + 150)
                    : el.x + (el.width  || (IS_FORMA(el.type) ? (el.type==='circulo'?120:200) : 60));
    const ey2 = isL ? (el.y2 ?? el.y)
                    : el.y + (el.height || (IS_FORMA(el.type) ? (el.type==='circulo'?120:140) : 60));
    minX = Math.min(minX, el.x, ex2); minY = Math.min(minY, el.y, ey2);
    maxX = Math.max(maxX, el.x, ex2); maxY = Math.max(maxY, el.y, ey2);
  });
  if (!isFinite(minX)) { minX = 0; minY = 0; maxX = state.canvas.width; maxY = state.canvas.height; }
  return { minX, minY, maxX, maxY };
}

function drawMinimap() {
  const box = document.getElementById('miniMapBox');
  const cnt = document.getElementById('miniMapCount');
  if (cnt) cnt.textContent = state.elements.length;
  if (!box || box.classList.contains('collapsed')) return;

  // Limites = conteúdo unido à área visível atual (mantém o viewport sempre dentro)
  const b  = contentBounds();
  const tl = toWorld(0, 0);
  const br = toWorld(VW, VH);
  let minX = Math.min(b.minX, tl.x), minY = Math.min(b.minY, tl.y);
  let maxX = Math.max(b.maxX, br.x), maxY = Math.max(b.maxY, br.y);
  const PAD = 30;
  minX -= PAD; minY -= PAD; maxX += PAD; maxY += PAD;
  const cw = Math.max(1, maxX - minX), ch = Math.max(1, maxY - minY);
  const ip = 8;
  const s  = Math.min((MM_W - ip*2) / cw, (MM_H - ip*2) / ch);
  const offX = (MM_W - cw*s) / 2, offY = (MM_H - ch*s) / 2;
  _mmView = { minX, minY, s, offX, offY };
  const X = wx => offX + (wx - minX) * s;
  const Y = wy => offY + (wy - minY) * s;

  mmCtx.clearRect(0, 0, MM_W, MM_H);
  mmCtx.fillStyle = '#eef0f5';
  mmCtx.fillRect(0, 0, MM_W, MM_H);

  // Moldura da área da planta
  mmCtx.strokeStyle = 'rgba(0,0,0,.08)';
  mmCtx.lineWidth = 1;
  mmCtx.strokeRect(X(0), Y(0), state.canvas.width * s, state.canvas.height * s);

  const sorted = [...state.elements].sort((a, b) => (a.zIndex??0) - (b.zIndex??0));
  sorted.forEach(el => {
    const cor = el.color || '#0071e3';
    if (el.type === 'linha') {
      const x2 = el.x2 ?? el.x + 150, y2 = el.y2 ?? el.y;
      mmCtx.strokeStyle = hexAlpha(cor, .65); mmCtx.lineWidth = 1;
      mmCtx.beginPath(); mmCtx.moveTo(X(el.x), Y(el.y)); mmCtx.lineTo(X(x2), Y(y2)); mmCtx.stroke();
      return;
    }
    const w = el.width  || (IS_FORMA(el.type) ? (el.type==='circulo'?120:200) : 60);
    const h = el.height || (IS_FORMA(el.type) ? (el.type==='circulo'?120:140) : 60);
    const rx = X(el.x), ry = Y(el.y), rw = Math.max(2, w*s), rh = Math.max(2, h*s);
    let fill = cor;
    if (el.prtg_objid && prtgEditorMap[el.prtg_objid]) {
      const st = parseInt(prtgEditorMap[el.prtg_objid].status ?? prtgEditorMap[el.prtg_objid].status_raw ?? 0);
      fill = st===5 ? '#ff3b30' : st===4 ? '#ff9500' : st===3 ? '#34c759' : cor;
    }
    if (IS_FORMA(el.type)) {
      mmCtx.fillStyle = hexAlpha(fill, .22); mmCtx.strokeStyle = hexAlpha(fill, .5); mmCtx.lineWidth = .6;
      mmCtx.beginPath();
      if (el.type === 'circulo') mmCtx.ellipse(rx+rw/2, ry+rh/2, rw/2, rh/2, 0, 0, Math.PI*2);
      else mmCtx.rect(rx, ry, rw, rh);
      mmCtx.fill(); mmCtx.stroke();
    } else {
      mmCtx.fillStyle = fill;
      mmCtx.beginPath();
      if (mmCtx.roundRect) mmCtx.roundRect(rx, ry, rw, rh, Math.min(2.5, rw/3));
      else mmCtx.rect(rx, ry, rw, rh);
      mmCtx.fill();
    }
  });

  // Retângulo do viewport
  const vx = X(tl.x), vy = Y(tl.y), vw = (br.x - tl.x) * s, vh = (br.y - tl.y) * s;
  mmCtx.fillStyle   = 'rgba(0,113,227,.10)';
  mmCtx.strokeStyle = '#0071e3';
  mmCtx.lineWidth   = 1.5;
  mmCtx.beginPath(); mmCtx.rect(vx, vy, vw, vh); mmCtx.fill(); mmCtx.stroke();
}

function mmCenterOn(e) {
  if (!_mmView) return;
  const r = MM.getBoundingClientRect();
  const px = (e.clientX - r.left) * (MM_W / r.width);
  const py = (e.clientY - r.top)  * (MM_H / r.height);
  const { minX, minY, s, offX, offY } = _mmView;
  const wx = minX + (px - offX) / s;
  const wy = minY + (py - offY) / s;
  panX = -(wx - state.canvas.width/2)  * zoom;
  panY = -(wy - state.canvas.height/2) * zoom;
  render();
}
MM.addEventListener('mousedown', e => { e.preventDefault(); e.stopPropagation(); _mmDrag = true; mmCenterOn(e); });
window.addEventListener('mousemove', e => { if (_mmDrag) mmCenterOn(e); });
window.addEventListener('mouseup',   () => { _mmDrag = false; });

function toggleMinimap() {
  const box = document.getElementById('miniMapBox');
  box.classList.toggle('collapsed');
  const collapsed = box.classList.contains('collapsed');
  document.getElementById('miniMapChevron').className = collapsed ? 'fa-solid fa-chevron-up' : 'fa-solid fa-chevron-down';
  if (!collapsed) render();
}

/* ================================================================
   ONBOARDING / ESTADO VAZIO
   ================================================================ */
function updateOnboard() {
  const ob = document.getElementById('peOnboard');
  if (!ob) return;
  ob.classList.toggle('visible', state.elements.length === 0 && !modoLinha);
}

const MODELOS = {
  rack: (cx, cy) => {
    const area   = { t:'quadro', l:'Sala de Rack', x:cx-200, y:cy-160, ex:{ width:400, height:320, color:'#475569', fillOpacity:0.12, borderStyle:'solid' } };
    const switchEl = { t:'switch',  l:'Switch Core', x:cx-120, y:cy-110 };
    const rack     = { t:'rack',    l:'Rack 42U',    x:cx-30,  y:cy-20  };
    const nobreak  = { t:'nobreak', l:'Nobreak',     x:cx+90,  y:cy+70  };
    return { els:[area, switchEl, rack, nobreak],
             links:[['switchEl','rack','network'], ['nobreak','rack','power']],
             names:{switchEl, rack, nobreak} };
  },
  rede: (cx, cy) => {
    const sw = { t:'switch',  l:'Switch',    x:cx-30,  y:cy-100 };
    const d1 = { t:'desktop', l:'PC 01',     x:cx-140, y:cy+40  };
    const d2 = { t:'desktop', l:'PC 02',     x:cx-30,  y:cy+40  };
    const d3 = { t:'desktop', l:'PC 03',     x:cx+80,  y:cy+40  };
    return { els:[sw, d1, d2, d3],
             links:[['d1','sw','network'], ['d2','sw','network'], ['d3','sw','network']],
             names:{sw, d1, d2, d3} };
  },
  cftv: (cx, cy) => {
    const sw = { t:'switch', l:'Switch PoE', x:cx-30,  y:cy-20  };
    const c1 = { t:'camera', l:'CAM 01',     x:cx-150, y:cy-120 };
    const c2 = { t:'camera', l:'CAM 02',     x:cx+90,  y:cy-120 };
    const c3 = { t:'camera', l:'CAM 03',     x:cx-150, y:cy+110 };
    const c4 = { t:'camera', l:'CAM 04',     x:cx+90,  y:cy+110 };
    return { els:[sw, c1, c2, c3, c4],
             links:[['c1','sw','network'], ['c2','sw','network'], ['c3','sw','network'], ['c4','sw','network']],
             names:{sw, c1, c2, c3, c4} };
  },
  vazio: (cx, cy) => {
    const area = { t:'quadro', l:'Nova Área', x:cx-200, y:cy-130, ex:{ width:400, height:260, color:'#8b5cf6', fillOpacity:0.12, borderStyle:'solid' } };
    return { els:[area], links:[], names:{} };
  },
};

function inserirModelo(tipo) {
  const fn = MODELOS[tipo]; if (!fn) return;
  const cx = state.canvas.width/2, cy = state.canvas.height/2;
  const def = fn(cx, cy);
  const baseZ = state.elements.map(e => e.zIndex ?? 0);
  let z = baseZ.length ? Math.max(...baseZ) + 1 : 0;

  def.els.forEach(spec => {
    const t = spec.t, isL = t === 'linha';
    const w = isL ? 0 : (IS_FORMA(t) ? (t==='circulo'?120:200) : (t==='camera'?56:60));
    const h = isL ? 0 : (IS_FORMA(t) ? (t==='circulo'?120:140) : (t==='camera'?56:60));
    const el = {
      id: 'e' + Date.now().toString(36) + Math.random().toString(36).slice(2,6),
      type: t, label: spec.l, x: spec.x, y: spec.y,
      width: w, height: h, zIndex: IS_FORMA(t) ? (z - 50) : z++,
      color: (TIPOS[t] && TIPOS[t].cor) || '#0071e3',
      item_id: null, prtg_objid: null, ip: '', observacoes: '',
      ...(spec.ex || {}),
    };
    spec._id = el.id;
    state.elements.push(el);
  });

  def.links.forEach(([a, b, ty]) => {
    const elA = def.names[a], elB = def.names[b];
    if (!elA || !elB) return;
    state.connections.push({
      id: 'c' + Date.now().toString(36) + Math.random().toString(36).slice(2,6),
      from: elA._id, to: elB._id, type: ty,
    });
  });

  normalizarCamadas();
  limparSelecao(); fecharPainel();
  pushUndo(); agendarSave();
  fitToScreen();
  mostrarToast('Modelo inserido — personalize do seu jeito');
}

/* ================================================================
   ATALHOS DE TECLADO (painel)
   ================================================================ */
function abrirAtalhos() { document.getElementById('atalhosOverlay').classList.add('open'); }
function fecharAtalhos(e) {
  if (e && e.target && e.target.id !== 'atalhosOverlay' && !e.target.closest('.pe-shortcuts-close')) return;
  document.getElementById('atalhosOverlay').classList.remove('open');
}
document.addEventListener('keydown', e => {
  const inp = ['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName);
  if (inp) return;
  if (e.key === '?' || (e.key === '/' && e.shiftKey)) {
    e.preventDefault();
    const ov = document.getElementById('atalhosOverlay');
    ov.classList.toggle('open');
  } else if (e.key === 'Escape') {
    document.getElementById('atalhosOverlay').classList.remove('open');
  }
});

/* ================================================================
   START
   ================================================================ */
window.addEventListener('DOMContentLoaded', () => {
  init();
  carregarPrtgEditor();
});