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
    fornecedor_portal_acesso,
    fornecedor_acessos_list,
    fornecedor_acesso_acao,
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
    monitoracao_relatorio,
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
    api_item_devolucao_info,
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
    tecnico_desempenho,
    minhas_atividades,
    apontamentos_horas_export,
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
    dashboard_apresentacao_dados,
    cc_custos_dashboard,
    cc_custos_detalhe,
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

# ── Administrador do Sistema (perfil do usuário logado) ──────────────────────
from .admin_perfil import admin_perfil

# ── Alertas ──────────────────────────────────────────────────────────────────
from .alertas import (
    alertas_dashboard, alertas_enviar, alertas_toggle,
    alertas_notificacoes, alertas_notificacao_toggle, alertas_notificacao_destinatarios,
    alertas_notificacao_remover_email, alertas_notificacao_desvincular_perfil,
)

# ── NinjaOne RMM ──────────────────────────────────────────────────────────────
from .ninja import (
    ninja_dashboard,
    ninja_dispositivos,
    ninja_relatorio,
    ninja_nao_cadastrados,
    ninja_login_validacao,
    ninja_login_revalidar,
    ninja_login_detalhe,
    ninja_importar,
)

# ── Status Board ─────────────────────────────────────────────────────────────
from .status_board import status_board

# ── Módulo Quiosque (app Android) ─────────────────────────────────────────────
from .quiosque import (
    # API do dispositivo
    kiosk_enroll,
    kiosk_checkin,
    kiosk_config,
    kiosk_comando_ack,
    kiosk_instalador_download,
    # Dashboard interno
    quiosque_dashboard,
    quiosque_detalhe,
    quiosque_matriculas,
    quiosque_matricula_excluir,
    quiosque_matricula_renomear,
    quiosque_instalador_gerar,
    quiosque_instalador_revogar,
    quiosque_mapa,
    quiosque_config_editar,
    quiosque_comando_novo,
    quiosque_revogar,
    quiosque_excluir,
)

# ── Portal do Fornecedor (área isolada) ──────────────────────────────────────
from .portal_fornecedor import (
    portal_home,
    portal_equipamentos_list,
    portal_equipamento_detail,
    portal_equipamentos_export,
    portal_manutencao_list,
    portal_manutencao_detail,
    portal_lote_manutencao_criar,
    portal_lotes_manutencao_list,
    portal_lote_manutencao_detail,
    portal_troca_antecipada_list,
    portal_troca_antecipada_nova,
    portal_notificacoes_marcar_lidas,
    portal_licencas_list,
    portal_ajuda,
)

# ── Manutenção externa — lado TI (recebimentos) ──────────────────────────────
from .manutencao import (
    manutencao_recebimentos,
    manutencao_recebimento_detail,
    manutencao_recebimento_acao,
    manutencao_recebimentos_ajuda,
    manutencao_lotes_list,
    manutencao_lote_detail,
    manutencao_lote_excluir,
)

# ── Notificações internas (sino do topo) ─────────────────────────────────────
from .notificacoes import notificacoes_marcar_lidas

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
    prtg_monitor_export,
    item_search_api,
)
