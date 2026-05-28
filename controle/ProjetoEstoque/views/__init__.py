"""
ProjetoEstoque views package — organização por domínio de negócio.

  categorias.py    — CRUD de categorias
  subtipos.py      — CRUD de subtipos
  usuarios.py      — CRUD, dashboard e operações de usuários
  fornecedores.py  — CRUD e dashboard de fornecedores
  localidades.py   — CRUD de localidades
  centrocusto.py   — CRUD e dashboard de centros de custo
  equipamentos.py  — CRUD, listagem e importação de itens/equipamentos
  locacoes.py      — CRUD de locações
  funcoes.py       — CRUD de funções
  comentarios.py   — CRUD de comentários
  movimentacoes.py — CRUD e listagem de movimentações de itens
  ciclos.py        — CRUD de ciclos de manutenção
  preventivas.py   — Checklists e execuções de preventivas
  licencas.py      — CRUD, lotes e movimentações de licenças
  dashboards.py    — Dashboard principal e preventiva/CC dashboard
  relatorios.py    — Exportações (toner, equipamentos, avisos, custos CC)
  termos.py        — Geração de termos de entrega/devolução
  inteligencia.py  — Sistema de inteligência e busca global
  home.py          — Sobre a plataforma
"""

# ── Categorias ──────────────────────────────────────────────────────────────
from .categorias import (
    categorias_list,
    categoria_create,
    categoria_update,
    categoria_delete,
)

# ── Subtipos ────────────────────────────────────────────────────────────────
from .subtipos import (
    subtipo_list,
    subtipo_create,
    subtipo_update,
    subtipo_delete,
    subtipo_detail,
)

# ── Usuários ────────────────────────────────────────────────────────────────
from .usuarios import (
    usuario_list,
    usuario_detail,
    usuario_create,
    usuario_update,
    usuario_delete,
    usuario_importar,
    usuario_desligar,
    usuario_remover_todas_licencas,
    usuario_dashboard,
    licenca_devolver_rapido,
    hierarquia_usuarios,
    organograma_usuarios,
    organograma_membros_supervisor,
)

# ── Fornecedores ─────────────────────────────────────────────────────────────
from .fornecedores import (
    fornecedor_list,
    fornecedor_create,
    fornecedor_update,
    fornecedor_detail,
    fornecedor_delete,
    fornecedor_export_pdf,
)

# ── Localidades ──────────────────────────────────────────────────────────────
from .localidades import (
    localidade_list,
    localidade_create,
    localidade_update,
    localidade_delete,
    localidade_detail,
)

# ── Centro de Custo ──────────────────────────────────────────────────────────
from .centrocusto import (
    centrocusto_list,
    centrocusto_create,
    centrocusto_update,
    centrocusto_detail,
    centrocusto_delete,
    centrocusto_export_pdf,
)

# ── Funções ──────────────────────────────────────────────────────────────────
from .funcoes import (
    funcao_list,
    funcao_form,
    funcao_delete,
)

# ── Equipamentos / Itens ─────────────────────────────────────────────────────
from .equipamentos import (
    item_create,
    equipamentos_list,
    equipamento_detalhe,
    equipamento_qr,
    item_update,
    equipamento_excluir,
    importar_planilha,
    item_monitoracao,
)

# ── Locações ─────────────────────────────────────────────────────────────────
from .locacoes import (
    locacoes_list,
    locacao_create,
    locacao_update,
    locacao_delete,
)

# ── Comentários ──────────────────────────────────────────────────────────────
from .comentarios import (
    comentarios_list,
    comentario_create,
    comentario_update,
    comentario_delete,
)

# ── Movimentações ────────────────────────────────────────────────────────────
from .movimentacoes import (
    movimentacao_list,
    movimentacao_create,
    movimentacao_detail,
    movimentacao_update,
    movimentacao_delete,
    movimentacao_export_pdf,
    api_lotes_por_item,
)

# ── Ciclos de Manutenção ─────────────────────────────────────────────────────
from .ciclos import (
    ciclos_list,
    ciclo_create,
    ciclo_update,
    ciclo_delete,
)

# ── Preventivas ──────────────────────────────────────────────────────────────
from .preventivas import (
    checklist_list,
    checklist_form,
    checklist_delete,
    pergunta_form,
    pergunta_delete,
    preventiva_list,
    preventiva_start,
    preventiva_exec,
    preventiva_detail,
    preventiva_agendar,
    preventiva_plano,
    preventiva_agendadas,
    preventiva_sincronizar_programacao,
)

# ── Licenças ─────────────────────────────────────────────────────────────────
from .licencas import (
    licenca_list,
    licenca_form,
    licenca_detail,
    licenca_export_excel,
    mov_licenca_list,
    mov_licenca_form,
    licenca_lote_list,
    licenca_lote_form,
)

# ── Dashboards ───────────────────────────────────────────────────────────────
from .dashboards import (
    dashboard,
    cc_custos_dashboard,
    cc_custos_export_pdf,
    preventiva_dashboard,
    custos_diretoria_dashboard,
    custos_diretoria_detalhe,
)


# ── Relatórios / Exportações ─────────────────────────────────────────────────
from .relatorios import (
    toner_cc_dashboard,
    toner_cc_export_excel,
    custo_cc_export_excel,
    equipamentos_exportar,
    licencas_dashboard,
    avisos_contratos_vencer,
    avisos_contratos_vencer_export_excel,
)

# ── Termos ───────────────────────────────────────────────────────────────────
from .termos import (
    termo_entrega_form,
    termo_devolucao_form,
)

# ── Inteligência ─────────────────────────────────────────────────────────────
from .inteligencia import (
    sistema_inteligencia_dashboard,
    sistema_inteligencia_busca_global,
    sistema_inteligencia_export_csv,
    sistema_noticias,
)

# ── Home / Sobre ─────────────────────────────────────────────────────────────
from .home import sobre_plataforma

# ── Alertas ──────────────────────────────────────────────────────────────────
from .alertas import alertas_dashboard, alertas_enviar

# ── Status Board ─────────────────────────────────────────────────────────────
from .status_board import status_board

# ── Plantas / Mapa de Infraestrutura ─────────────────────────────────────────
from .plantas import (
    planta_list,
    planta_create,
    planta_update,
    planta_delete,
    planta_editor,
    planta_viewer,
    planta_salvar_layout,
    planta_check_version,
    planta_historico_api,
    planta_restaurar_versao,
    planta_tv,
    planta_tv_lista,
    planta_tv_gerenciar,
    planta_tv_gerenciar_acao,
    prtg_status_api,
    prtg_search_api,
    prtg_monitor,
    item_search_api,
)
