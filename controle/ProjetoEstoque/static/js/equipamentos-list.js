/* =============================================================
   equipamentos-list.js — Lista de equipamentos — filtros AJAX, ordenação, galeria, canvas de partículas

   Extraido do <script> inline de equipamentos_list.html. Nenhuma linha de
   logica alterada. Nao tinha (e nao pode ter) tag de template:
   dado do servidor entra por json_script, nunca interpolado
   aqui dentro.

   Ao mudar, suba ASSETS_VERSION em context_processors.py.
   ============================================================= */
(() => {
    'use strict';

    const dom = {
        form: document.getElementById('filtrosForm'),
        sizeForm: document.getElementById('sizeForm'),
        tbody: document.getElementById('tbodyEquipamentos'),
        pagWrap: document.getElementById('paginationWrap'),
        kpis: document.getElementById('kpisWrap'),
        count: document.getElementById('countResults'),
        loader: document.getElementById('tableLoader'),
        chips: document.getElementById('chipsWrap'),
        filtrosBadge: document.getElementById('filtrosBadge'),
        filtrosBadgeText: document.getElementById('filtrosBadgeText'),
        listView: document.getElementById('listView'),
        galleryView: document.getElementById('galleryView'),
        galleryWrap: document.getElementById('galleryWrap'),
        btnViewList: document.getElementById('btnViewList'),
        btnViewGallery: document.getElementById('btnViewGallery'),
        deleteModal: document.getElementById('deleteModal'),
        deleteMessage: document.getElementById('deleteMessage'),
        deleteForm: document.getElementById('deleteForm'),
        importModal: document.getElementById('importResultModal'),
        importForm: document.getElementById('formImportar'),
        inputArquivo: document.getElementById('inputArquivo'),
        btnImportar: document.getElementById('btnImportar'),
        btnCloseImportModal: document.getElementById('btnCloseImportModal'),
        importSummary: document.getElementById('importSummary'),
        importCreatedList: document.getElementById('importCreatedList'),
        importUpdatedList: document.getElementById('importUpdatedList'),
        importIgnoredList: document.getElementById('importIgnoredList'),
        importErrorWrap: document.getElementById('importErrorWrap'),
        importErrorList: document.getElementById('importErrorList'),
    };

    let currentController = null;
    let importInProgress = false;

    // Modo de visualização: 'list' | 'gallery'
    let viewMode = localStorage.getItem('eq_view_mode') || 'list';

    function setViewMode(mode) {
        viewMode = mode;
        localStorage.setItem('eq_view_mode', mode);

        const isGallery = mode === 'gallery';

        if (dom.listView)    dom.listView.style.display    = isGallery ? 'none' : '';
        if (dom.galleryView) dom.galleryView.style.display = isGallery ? '' : 'none';

        if (dom.btnViewList) dom.btnViewList.classList.toggle('is-active', !isGallery);
        if (dom.btnViewGallery) dom.btnViewGallery.classList.toggle('is-active', isGallery);

        refreshList({ page: 1 });
    }

    // Aplica modo salvo na inicialização
    (function initViewMode() {
        if (viewMode === 'gallery') {
            if (dom.listView)    dom.listView.style.display    = 'none';
            if (dom.galleryView) dom.galleryView.style.display = '';
            if (dom.btnViewList)    dom.btnViewList.classList.remove('is-active');
            if (dom.btnViewGallery) dom.btnViewGallery.classList.add('is-active');
            // Carrega galeria de forma assíncrona após DOM pronto
            setTimeout(() => refreshList(), 0);
        }
    })();

    function escapeHtml(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }

    function debounce(func, wait) {
        let timeout;

        return function(...args) {
            clearTimeout(timeout);
            timeout = setTimeout(() => func.apply(this, args), wait);
        };
    }

    function setLoading(isLoading) {
        if (!dom.loader) return;
        dom.loader.classList.toggle('is-active', isLoading);
        const target = viewMode === 'gallery' ? dom.galleryWrap : dom.tbody;
        if (target) target.style.opacity = isLoading ? '0.45' : '1';
    }

    function buildListParams(extraParams = {}) {
        const formData = new FormData(dom.form);
        const params = new URLSearchParams();

        for (const [key, val] of formData.entries()) {
            const cleanValue = String(val || '').trim();

            if (cleanValue !== '') {
                params.append(key, cleanValue);
            }
        }

        const pp = dom.sizeForm?.querySelector('[name="pp"]')?.value;

        if (pp) {
            params.set('pp', pp);
        }

        for (const [key, val] of Object.entries(extraParams)) {
            if (val === null || val === undefined || val === '') {
                params.delete(key);
            } else {
                params.set(key, val);
            }
        }

        params.set('partial', '1');
        params.set('view', viewMode);

        return params;
    }

    // Mesma lista de campos contada em `_build_queryset_and_context`
    // (equipamentos.py) — mantém o badge "N filtros ativos" do resumo em
    // sincronia com o que o backend de fato aplica. Um campo multi-valorado
    // (ex.: subtipo com 3 IDs marcados) conta como 1 filtro ativo, não 3.
    const FILTRO_CAMPOS = [
        'nome', 'numero_serie', 'modelo', 'fornecedor', 'status',
        'tipo_item', 'estoque', 'subtipo', 'localidade', 'centro_custo',
    ];

    function updateFiltersBadge(params) {
        if (!dom.filtrosBadge || !dom.filtrosBadgeText) return;

        const count = FILTRO_CAMPOS.reduce((acc, campo) => acc + (params.has(campo) ? 1 : 0), 0);

        dom.filtrosBadge.hidden = count === 0;
        dom.filtrosBadgeText.textContent = `${count} filtro${count === 1 ? '' : 's'} ativo${count === 1 ? '' : 's'}`;
    }

    async function refreshList(extraParams = {}) {
        if (!dom.form) return;

        if (currentController) {
            currentController.abort();
        }

        currentController = new AbortController();
        setLoading(true);

        try {
            const params = buildListParams(extraParams);
            updateFiltersBadge(params);
            const url = `${window.location.pathname}?${params.toString()}`;

            const response = await fetch(url, {
                headers: {
                    'X-Requested-With': 'XMLHttpRequest',
                },
                signal: currentController.signal,
            });

            if (!response.ok) {
                throw new Error('Erro na atualização da listagem.');
            }

            const data = await response.json();

            if (data.tbody !== undefined && dom.tbody) dom.tbody.innerHTML = data.tbody;
            if (data.gallery !== undefined && dom.galleryWrap) {
                dom.galleryWrap.innerHTML = data.gallery;
                bindGalleryEvents();
            }
            if (data.pagination !== undefined) dom.pagWrap.innerHTML = data.pagination;
            if (data.kpis !== undefined) dom.kpis.innerHTML = data.kpis;
            if (data.count !== undefined) dom.count.textContent = `${data.count} resultado(s)`;

            params.delete('partial');
            params.delete('view');

            const finalQuery = params.toString();
            const finalUrl = finalQuery ? `${window.location.pathname}?${finalQuery}` : window.location.pathname;

            window.history.replaceState({}, '', finalUrl);

            const expBtn = document.getElementById('exportBtn');
            if (expBtn) expBtn.href = finalQuery ? `${expBtn.dataset.exportBase}?${finalQuery}` : expBtn.dataset.exportBase;

            bindTableEvents();

        } catch (error) {
            if (error.name !== 'AbortError') {
                console.error('[Equipamentos] Erro ao atualizar listagem:', error);
            }
        } finally {
            if (!currentController?.signal.aborted) {
                setLoading(false);
                currentController = null;
            }
        }
    }

    function updateChipsVisual(value) {
        if (!dom.chips) return;

        dom.chips.querySelectorAll('.eq-chip').forEach((chip) => {
            chip.classList.toggle('is-active', (chip.dataset.val || '') === (value || ''));
        });
    }

    function resetFilters() {
        dom.form.reset();

        $('.select2-enable').val(null).trigger('change.select2');
        updateChipsVisual('');

        dom.form.querySelectorAll('.eq-exc-toggle input[type="checkbox"]').forEach((cb) => {
            cb.checked = false;
            cb.closest('.eq-filter-field')?.classList.remove('is-exceto');
        });

        refreshList({
            page: 1,
            nome: null,
            status: null,
            subtipo: null,
            subtipo_exc: null,
            fornecedor: null,
            localidade: null,
            localidade_exc: null,
            centro_custo: null,
            centro_custo_exc: null,
            numero_serie: null,
            modelo: null,
            tipo_item: null,
            estoque: null,
        });
    }

    function sortTableByColumn(th) {
        const col = Number(th.dataset.col);

        if (!Number.isFinite(col)) return;

        const rows = Array.from(dom.tbody.querySelectorAll('tr')).filter((row) => row.children.length > col);
        const isAsc = th.dataset.order !== 'asc';

        th.dataset.order = isAsc ? 'asc' : 'desc';

        rows.sort((a, b) => {
            const valA = (a.children[col]?.innerText || '').trim().toLowerCase();
            const valB = (b.children[col]?.innerText || '').trim().toLowerCase();

            return isAsc ? valA.localeCompare(valB, 'pt-BR') : valB.localeCompare(valA, 'pt-BR');
        });

        rows.forEach((row) => dom.tbody.appendChild(row));
    }

    function bindTableEvents() {
        document.querySelectorAll('tr.eq-clickable').forEach((row) => {
            row.onclick = (event) => {
                if (event.target.closest('a, button')) return;

                const href = row.dataset.href;

                if (href) {
                    window.location.href = href;
                }
            };
        });

        document.querySelectorAll('th[data-col]').forEach((th) => {
            th.onclick = () => sortTableByColumn(th);
        });
    }

    function bindGalleryEvents() {
        document.querySelectorAll('.eq-gc[data-href]').forEach((card) => {
            card.onclick = (event) => {
                if (event.target.closest('a, button')) return;
                window.location.href = card.dataset.href;
            };
            card.onkeydown = (event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    window.location.href = card.dataset.href;
                }
            };
        });
    }

    function openImportResultModal() {
        dom.importModal.classList.add('is-open');
        dom.importModal.setAttribute('aria-hidden', 'false');
    }

    function closeImportResultModal() {
        dom.importModal.classList.remove('is-open');
        dom.importModal.setAttribute('aria-hidden', 'true');
    }

    function renderImportList(lista, type = 'default') {
        if (!Array.isArray(lista) || !lista.length) {
            return '<div style="color: var(--eq-muted);">Nenhum registro.</div>';
        }

        return lista.map((item) => {
            if (type === 'ignored') {
                return `
                    <div class="eq-import-row">
                        <strong>Linha ${escapeHtml(item.linha ?? '-')}</strong><br>
                        <small>${escapeHtml(item.motivo || 'Registro ignorado')}</small>
                    </div>
                `;
            }

            return `
                <div class="eq-import-row">
                    <strong>${escapeHtml(item.nome || '-')}</strong><br>
                    <small>S/N: ${escapeHtml(item.numero_serie || '-')}</small>
                </div>
            `;
        }).join('');
    }

    function renderErrorList(lista) {
        if (!Array.isArray(lista) || !lista.length) {
            return '';
        }

        return lista.map((item) => `
            <div class="eq-import-row">
                <small>${escapeHtml(item)}</small>
            </div>
        `).join('');
    }

    function fillImportModal(resultado) {
        const totais = resultado?.totais || {};

        dom.importSummary.innerHTML = `
            <strong>Criados:</strong> ${totais.criados || 0}
            &nbsp;|&nbsp;
            <strong>Atualizados:</strong> ${totais.atualizados || 0}
            &nbsp;|&nbsp;
            <strong>Ignorados:</strong> ${totais.ignorados || 0}
        `;

        dom.importCreatedList.innerHTML = renderImportList(resultado?.criados || []);
        dom.importUpdatedList.innerHTML = renderImportList(resultado?.atualizados || []);
        dom.importIgnoredList.innerHTML = renderImportList(resultado?.ignorados || [], 'ignored');

        const erros = resultado?.erros || [];

        if (Array.isArray(erros) && erros.length) {
            dom.importErrorWrap.style.display = 'block';
            dom.importErrorList.innerHTML = renderErrorList(erros);
        } else {
            dom.importErrorWrap.style.display = 'none';
            dom.importErrorList.innerHTML = '';
        }
    }

    function setImportButtonLoading(isLoading) {
        importInProgress = isLoading;

        dom.btnImportar.disabled = isLoading;
        dom.inputArquivo.disabled = isLoading;

        dom.btnImportar.innerHTML = isLoading
            ? '<i class="fa fa-circle-notch fa-spin" aria-hidden="true"></i><span class="btn-import-text">Importando...</span>'
            : '<i class="fa fa-upload" aria-hidden="true"></i><span class="btn-import-text">Importar</span>';
    }

    async function sendImport() {
        if (!dom.inputArquivo.files || !dom.inputArquivo.files.length || importInProgress) return;

        const arquivo = dom.inputArquivo.files[0];
        const nomeArquivo = (arquivo.name || '').toLowerCase();

        if (!nomeArquivo.endsWith('.xlsx')) {
            alert('Selecione um arquivo no formato .xlsx');
            dom.inputArquivo.value = '';
            return;
        }

        const formData = new FormData(dom.importForm);

        setImportButtonLoading(true);

        try {
            const response = await fetch(dom.importForm.action, {
                method: 'POST',
                body: formData,
                headers: {
                    'X-Requested-With': 'XMLHttpRequest',
                },
            });

            const data = await response.json();

            if (!response.ok || !data.ok) {
                throw new Error(data.mensagem || 'Erro ao importar a planilha.');
            }

            fillImportModal(data.resultado || {});
            openImportResultModal();

            await refreshList({ page: 1 });

        } catch (error) {
            console.error('[Equipamentos] Erro na importação:', error);
            alert(error.message || 'Erro ao processar a importação.');
        } finally {
            setImportButtonLoading(false);
            dom.inputArquivo.value = '';
        }
    }

    const mainScroller = document.getElementById('mainContent') || document.scrollingElement || document.body;

    function lockShellScroll() {
        mainScroller.style.overflow = 'hidden';
    }

    function unlockShellScroll() {
        mainScroller.style.overflow = '';
    }

    window.openDeleteModalFromButton = function(button) {
        const nome = button.dataset.nome || 'este equipamento';
        const url = button.dataset.url || '';

        // `.main-container` tem animation com transform (fill-mode: both), o que
        // vira containing block de qualquer position:fixed dentro dela. Reparentar
        // para <body> garante que o overlay cubra a viewport de verdade.
        if (dom.deleteModal.parentNode !== document.body) {
            document.body.appendChild(dom.deleteModal);
        }

        dom.deleteMessage.innerHTML =
            `Tem certeza que deseja excluir o equipamento <strong>${escapeHtml(nome)}</strong>? ` +
            `Ele deixará de aparecer nas listagens, mas todo o histórico vinculado ` +
            `(movimentações, ordens de manutenção, preventivas, locações e lotes) será preservado ` +
            `e a exclusão pode ser desfeita depois.`;

        dom.deleteForm.action = url;

        dom.deleteModal.classList.add('is-open');
        dom.deleteModal.setAttribute('aria-hidden', 'false');
        lockShellScroll();
    };

    window.closeDeleteModal = function() {
        dom.deleteModal.classList.remove('is-open');
        dom.deleteModal.setAttribute('aria-hidden', 'true');
        unlockShellScroll();
    };

    window.refreshList  = refreshList;
    window.setViewMode  = setViewMode;

    function bindEvents() {
        // Select2 renderiza o "×" de limpar tudo (allowClear) como um botão de
        // BLOCO extra nos campos `multiple` — soma ~20px de altura fantasma
        // acima dos chips (estourava a caixa de 34px e sobrava aquela barra
        // cinza de scroll). Nos multi-selects cada chip já tem o próprio "×",
        // então o allowClear geral não faz falta — só liga nos single-selects,
        // onde ele já funciona bem (Status/Tipo/Estoque).
        const select2Base = {
            width: '100%',
            placeholder: function() {
                return $(this).data('placeholder') || 'Selecione...';
            },
            language: {
                noResults: () => 'Nenhum resultado encontrado',
                searching: () => 'Buscando...'
            },
        };

        $('.select2-enable:not([multiple])').select2({ ...select2Base, allowClear: true });
        $('.select2-enable[multiple]').select2({ ...select2Base, allowClear: false });

        $('.select2-enable').on('change', function() {
            if (this.id === 'filtro-status') {
                updateChipsVisual(this.value || '');
            }

            refreshList({ page: 1 });
        });

        // Toggle "Exceto" — reflexo visual (borda/etiqueta vermelha no grupo)
        // e recarrega a lista: quem decide incluir x excluir é o backend, a
        // partir do mesmo par campo/campo_exc enviado pelo form.
        document.querySelectorAll('.eq-exc-toggle input[type="checkbox"]').forEach((cb) => {
            cb.addEventListener('change', () => {
                cb.closest('.eq-filter-field')?.classList.toggle('is-exceto', cb.checked);
                refreshList({ page: 1 });
            });
        });

        $('#filtrosForm input[type="text"]').on('input', debounce(() => {
            refreshList({ page: 1 });
        }, 450));

        document.getElementById('pp')?.addEventListener('change', () => {
            refreshList({ page: 1 });
        });

        document.getElementById('btnLimpar')?.addEventListener('click', resetFilters);

        dom.chips?.addEventListener('click', (event) => {
            const chip = event.target.closest('.eq-chip');

            if (!chip) return;

            event.preventDefault();

            const statusValue = chip.dataset.val || '';

            updateChipsVisual(statusValue);
            $('#filtro-status').val(statusValue).trigger('change');
        });

        dom.pagWrap?.addEventListener('click', (event) => {
            const link = event.target.closest('a[data-page], a[href]');

            if (!link) return;

            event.preventDefault();

            const page = link.dataset.page || new URL(link.href, window.location.origin).searchParams.get('page') || 1;

            refreshList({ page });
        });

        dom.btnImportar?.addEventListener('click', () => {
            if (importInProgress) return;
            dom.inputArquivo.click();
        });

        dom.inputArquivo?.addEventListener('change', sendImport);
        dom.btnCloseImportModal?.addEventListener('click', closeImportResultModal);

        dom.importModal?.addEventListener('click', (event) => {
            if (event.target === dom.importModal) closeImportResultModal();
        });

        dom.deleteModal?.addEventListener('click', (event) => {
            if (event.target === dom.deleteModal) closeDeleteModal();
        });

        document.addEventListener('keydown', (event) => {
            if (event.key !== 'Escape') return;

            if (dom.deleteModal?.classList.contains('is-open')) {
                closeDeleteModal();
            }

            if (dom.importModal?.classList.contains('is-open')) {
                closeImportResultModal();
            }
        });
    }

    document.addEventListener('DOMContentLoaded', () => {
        bindEvents();
        bindTableEvents();
    });
})();
