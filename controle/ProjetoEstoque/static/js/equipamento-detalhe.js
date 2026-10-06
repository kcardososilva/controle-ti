/* =============================================================
   equipamento-detalhe.js — Ficha do equipamento — navegação por abas, lightbox e gráficos

   Extraido do <script> inline de equipamento_detalhe.html. Nenhuma linha de
   logica alterada. Nao tinha (e nao pode ter) tag de template:
   dado do servidor entra por json_script, nunca interpolado
   aqui dentro.

   Ao mudar, suba ASSETS_VERSION em context_processors.py.
   ============================================================= */
(function () {
  'use strict';

  /* ── Navegação por tabs ─────────────────────────────────────────────────── */
  var tabBtns = document.querySelectorAll('.ad-tab-btn');
  tabBtns.forEach(function (btn) {
    btn.addEventListener('click', function () {
      tabBtns.forEach(function (b) {
        b.classList.remove('is-active');
        b.setAttribute('aria-selected', 'false');
      });
      document.querySelectorAll('.ad-tab-panel').forEach(function (p) {
        p.classList.remove('is-active');
      });
      btn.classList.add('is-active');
      btn.setAttribute('aria-selected', 'true');
      var panel = document.getElementById('tab-' + btn.dataset.tab);
      if (panel) {
        panel.classList.add('is-active');
        if (btn.dataset.tab === 'monitoracao' && !MON.loaded) {
          MON.carregar(30);
        }
      }
    });
  });

  /* ── Módulo de Monitoração PRTG ────────────────────────────────────────── */
  var MON = (function () {
    var _loading  = false;
    var _loaded   = false;

    /* Cores e rótulos por status PRTG */
    var CFG = {
      up:              { cor: '#34c759', label: 'Online',         cls: 'is-up' },
      down:            { cor: '#ff3b30', label: 'Offline',        cls: 'is-down' },
      warning:         { cor: '#ff9500', label: 'Atenção',        cls: 'is-warning' },
      unusual:         { cor: '#ff9500', label: 'Incomum',        cls: 'is-warning' },
      unknown:         { cor: '#8e8e93', label: 'Desconhecido',   cls: '' },
      collecting:      { cor: '#5ac8fa', label: 'Coletando',      cls: '' },
      paused_by_dep:   { cor: '#00cfff', label: 'Pausado (dep.)', cls: '' },
      paused_by_user:  { cor: '#8e8e93', label: 'Pausado',        cls: '' },
      paused_by_sched: { cor: '#8e8e93', label: 'Prog. pausado',  cls: '' },
      paused_until:    { cor: '#8e8e93', label: 'Pausado até',    cls: '' },
      no_probe:        { cor: '#8e8e93', label: 'Sem sonda',      cls: '' },
      not_licensed:    { cor: '#8e8e93', label: 'Sem licença',    cls: '' },
      sem_dados:       { cor: '#c7c7cc', label: 'Sem monitoração',cls: '' },
    };

    function cfg(slug) {
      return CFG[slug] || { cor: '#c7c7cc', label: slug || '—', cls: '' };
    }

    function diasTexto(d) {
      if (!d || d <= 0) return '0h';
      if (d < 1)        return Math.round(d * 24) + 'h';
      if (d < 2)        return '1 dia';
      return Math.round(d) + ' dias';
    }

    function pctTexto(pct) {
      return pct >= 1 ? '(' + Math.round(pct) + '%)' : '';
    }

    function el(id)          { return document.getElementById(id); }
    function show(id)        { var e = el(id); if (e) e.style.display = ''; }
    function hide(id)        { var e = el(id); if (e) e.style.display = 'none'; }
    function setVis(id, v)   { v ? show(id) : hide(id); }

    function showLoading() {
      show('ad-mon-loading');
      hide('ad-mon-error');
      hide('ad-prtg-nolink');
      hide('ad-prtg-header');
      hide('ad-prtg-kpis');
      hide('ad-prtg-timeline-card');
      hide('ad-prtg-hist-card');
    }

    function showError() {
      hide('ad-mon-loading');
      show('ad-mon-error');
      hide('ad-prtg-nolink');
      hide('ad-prtg-header');
      hide('ad-prtg-kpis');
      hide('ad-prtg-timeline-card');
      hide('ad-prtg-hist-card');
    }

    function showNoLink() {
      hide('ad-mon-loading');
      hide('ad-mon-error');
      show('ad-prtg-nolink');
      hide('ad-prtg-header');
      hide('ad-prtg-kpis');
      hide('ad-prtg-timeline-card');
      hide('ad-prtg-hist-card');
    }

    function showContent() {
      hide('ad-mon-loading');
      hide('ad-mon-error');
      hide('ad-prtg-nolink');
      show('ad-prtg-header');
      show('ad-prtg-kpis');
    }

    /* ── Renderização ──────────────────────────────────────────────────── */

    function renderHeader(data) {
      var hdrEl = el('ad-prtg-header');
      if (!hdrEl) return;
      var info = data.info;
      if (!info) {
        hdrEl.innerHTML =
          '<i class="fa fa-circle-question" style="color:var(--ad-muted);" aria-hidden="true"></i>'
          + '<span style="font-size:13px;color:var(--ad-muted);">Device não encontrado no PRTG agora — último status registrado pode estar desatualizado.</span>';
        return;
      }
      var c = cfg(info.status_slug);
      /* Alertar se ping sensor está down mas device-level mostra up */
      var pingWarn = '';
      if (info.ping_status !== null && info.ping_status !== undefined) {
        if (info.ping_status === 5 && info.status_slug !== 'down') {
          pingWarn = ' <span class="ad-prtg-ping-warn">'
            + '<i class="fa fa-circle-exclamation" aria-hidden="true"></i>'
            + 'Ping sensor: Offline</span>';
        }
      }
      hdrEl.innerHTML =
        '<span class="ad-prtg-badge ' + c.cls + '" style="background:' + c.cor + '22;color:' + c.cor + ';">'
        + '<i class="fa fa-circle" style="font-size:7px;" aria-hidden="true"></i> ' + c.label
        + '</span>'
        + '<span style="font-size:13px;font-weight:600;">' + info.nome + '</span>'
        + pingWarn
        + '<span class="ad-prtg-host">' + (info.host || '') + '</span>';
    }

    function renderKpis(data) {
      var kpiEl = el('ad-prtg-kpis');
      if (!kpiEl) return;
      var ORDER = ['up','down','warning','unusual','collecting','paused_by_dep','paused_by_user','paused_by_sched','paused_until','unknown'];
      var total  = Object.values(data.totais).reduce(function(a, b){ return a + b; }, 0);
      var html   = '';
      var semDados = true;
      ORDER.forEach(function (slug) {
        var val = data.totais[slug];
        if (!val) return;
        semDados = false;
        var c = cfg(slug);
        html += '<div class="ad-mon-kpi" style="color:' + c.cor + ';">'
          + '<div class="ad-mon-kpi-val">' + diasTexto(val) + '</div>'
          + '<div class="ad-mon-kpi-lbl">' + c.label + '</div>'
          + '<div class="ad-mon-kpi-pct">' + pctTexto(total > 0 ? val / total * 100 : 0) + '</div>'
          + '</div>';
      });
      if (semDados) {
        html = '<div class="ad-mon-empty" style="width:100%;padding:20px;">'
          + '<i class="fa fa-hourglass-half" aria-hidden="true"></i>'
          + '<p>Sem registros no período — o histórico começa a ser gravado a partir desta consulta.</p>'
          + '</div>';
      }
      kpiEl.innerHTML = html;
    }

    function renderTimeline(data) {
      var ganttEl  = el('ad-prtg-gantt');
      var legendEl = el('ad-prtg-legend');
      setVis('ad-prtg-timeline-card', data.periodos && data.periodos.length > 0);
      if (!ganttEl || !data.periodos || data.periodos.length === 0) return;

      var segsHtml = '', lgHtml = '', lgSeen = {};
      data.periodos.forEach(function (p) {
        var c   = cfg(p.status);
        var pct = Math.max(p.pct, 0.4);
        var lbl = p.pct >= 8 ? diasTexto(p.dias) : '';
        var tip = c.label + ': ' + diasTexto(p.dias) + ' (' + p.inicio + ' → ' + p.fim + ')';
        segsHtml += '<div class="ad-mon-seg" style="width:' + pct + '%;background:' + c.cor + ';"'
          + ' title="' + tip.replace(/"/g, '&quot;') + '">'
          + '<span class="ad-mon-seg-lbl">' + lbl + '</span></div>';
        if (!lgSeen[p.status]) {
          lgSeen[p.status] = true;
          lgHtml += '<div class="ad-mon-legend-item">'
            + '<span class="ad-mon-legend-dot" style="background:' + c.cor + ';"></span>'
            + c.label + '</div>';
        }
      });
      ganttEl.innerHTML  = segsHtml;
      if (legendEl) legendEl.innerHTML = lgHtml;
    }

    function renderHistorico(data) {
      var histEl = el('ad-prtg-hist-body');
      show('ad-prtg-hist-card');
      if (!histEl) return;

      if (!data.historico || data.historico.length === 0) {
        histEl.innerHTML = '<div class="ad-mon-empty">'
          + '<i class="fa fa-clock-rotate-left" aria-hidden="true"></i>'
          + '<p>Nenhuma mudança de conectividade registrada ainda.</p>'
          + '<small>O registro é criado automaticamente quando o status PRTG muda.</small>'
          + '</div>';
        return;
      }

      var html = '<table class="ad-mon-hist-table"><thead><tr>'
        + '<th>De</th><th></th><th>Para</th><th>Data / Hora</th>'
        + '</tr></thead><tbody>';
      data.historico.forEach(function (ev) {
        var cA = cfg(ev.status_anterior);
        var cN = cfg(ev.status_novo);
        var bA = ev.status_anterior
          ? '<span class="ad-mon-badge" style="background:' + cA.cor + '22;color:' + cA.cor + ';">' + cA.label + '</span>'
          : '<span class="ad-mon-badge" style="background:#f2f2f7;color:#8e8e93;">(inicial)</span>';
        var bN = '<span class="ad-mon-badge" style="background:' + cN.cor + '22;color:' + cN.cor + ';">' + cN.label + '</span>';
        html += '<tr>'
          + '<td>' + bA + '</td>'
          + '<td><i class="fa fa-arrow-right ad-mon-arrow" aria-hidden="true"></i></td>'
          + '<td>' + bN + '</td>'
          + '<td style="white-space:nowrap;font-size:12px;">' + ev.registrado_em + '</td>'
          + '</tr>';
      });
      html += '</tbody></table>';
      histEl.innerHTML = html;
    }

    /* ── Carregamento principal ─────────────────────────────────────────── */
    function carregar(dias) {
      if (_loading) return;
      _loading = true;

      document.querySelectorAll('.ad-mon-period').forEach(function (b) {
        b.classList.toggle('is-active', parseInt(b.dataset.days, 10) === dias);
      });
      var labelEl = el('ad-mon-period-label');
      if (labelEl) labelEl.textContent = dias === 0 ? 'Histórico completo' : 'Últimos ' + dias + ' dias';

      var panel  = el('tab-monitoracao');
      var monUrl = panel ? (panel.dataset.monUrl || '') : '';
      if (!monUrl) { showError(); _loading = false; return; }

      showLoading();

      fetch(monUrl + '?periodo=' + dias, { credentials: 'same-origin' })
        .then(function (r) {
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.json();
        })
        .then(function (data) {
          if (!data.ok) throw new Error('Resposta inválida');
          if (!data.encontrado) { showNoLink(); _loaded = true; return; }
          renderHeader(data);
          renderKpis(data);
          renderTimeline(data);
          renderHistorico(data);
          showContent();
          _loaded = true;
        })
        .catch(function (err) {
          console.error('[Monitoração PRTG]', err);
          showError();
        })
        .finally(function () { _loading = false; });
    }

    return {
      get loaded() { return _loaded; },
      set loaded(v) { _loaded = v; },
      carregar: carregar,
    };
  }());

  /* Botões de período */
  document.querySelectorAll('.ad-mon-period').forEach(function (btn) {
    btn.addEventListener('click', function () {
      MON.loaded = false;
      MON.carregar(parseInt(btn.dataset.days, 10));
    });
  });

  /* Abrir aba via hash na URL */
  if (window.location.hash === '#monitoracao') {
    var monBtn = document.querySelector('.ad-tab-btn[data-tab="monitoracao"]');
    if (monBtn) monBtn.click();
  }

})();
