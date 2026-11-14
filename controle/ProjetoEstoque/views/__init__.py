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
  licencas_office.py — Licenças Office (chave por equipamento) + vínculo com Item
  portal_licencas.py — Portal de Licenças Office (área isolada p/ parceiros externos, ex.: Routerlink)
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

# ── Cadastro rápido (popup do formulário de item) ───────────────────────────
from .cadastro_rapido import (
    subtipo_cadastro_rapido,
    localidade_cadastro_rapido,
)

# ── Usuários ────────────────────────────────────────────────────────────────
from .usuarios import (
    usuario_list,
    usuario_export_excel,
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
    fornecedor_quick_create,
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
    equipamento_restaurar,
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
    movimentacao_lote_create,
    movimentacao_lote_termo,
    movimentacao_detail,
    movimentacao_update,
    movimentacao_delete,
    movimentacao_reverter,
    movimentacao_export_pdf,
    api_lotes_por_item,
    api_item_devolucao_info,
    api_item_reserva,
    api_itens_disponiveis_lote,
    repositorio_termos,
)

# ── Separação de Itens (Envio / Devolução) ───────────────────────────────────
from .separacoes import (
    separacao_envio_list,
    separacao_devolucao_list,
    separacao_lote_create,
    separacao_lote_vincular_soltos,
    separacao_lote_detail,
    separacao_lote_excluir,
    separacao_lote_enviar,
    separacao_lote_item_adicionar,
    separacao_item_remover,
    separacao_item_enviar,
    separacao_lote_item_desvincular,
    separacao_item_nota_fiscal_anexar,
    separacao_nota_fiscal_excluir,
    documento_fiscal_gerar,
    documento_fiscal_pdf_view,
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
    preventiva_execucao_editar,
    preventiva_execucao_excluir,
    preventiva_detail,
    preventiva_agendar,
    preventiva_plano,
    preventiva_agendadas,
    preventiva_sincronizar_programacao,
    preventiva_metas,
    preventiva_metas_config,
    preventiva_metas_export,
    tecnico_desempenho,
    minhas_atividades,
    preventiva_minha_agenda,
    apontamentos_horas_export,
)

# ── Licenças ─────────────────────────────────────────────────────────────────
from .licencas import (
    licenca_list,
    licenca_form,
    licenca_detail,
    licenca_export_excel,
    licenca_saude,
    licenca_colaboradores_api,
    licenca_devolver_orfao,
    licenca_reconciliar_lote,
    licenca_atribuir_rapido,
    licenca_renovacoes,
    licenca_renovacao_datas,
    mov_licenca_list,
    mov_licenca_form,
    mov_licenca_export,
    licenca_lote_list,
    licenca_lote_form,
)

# ── Licenças Office (chave por equipamento) ───────────────────────────────────
from .licencas_office import (
    licenca_office_list,
    licenca_office_form,
    licenca_office_delete,
    licenca_office_importar,
    licenca_office_vincular,
    licenca_office_desvincular,
)

# ── Portal de Licenças Office (área isolada p/ parceiros externos) ───────────
# Nomes com sufixo "_office" de propósito: já existe `portal_licencas_list`
# (Portal do Fornecedor, módulo antigo de pool de licenças em portal_fornecedor.py)
# — sem o sufixo, este import silenciosamente sobrescreveria aquele símbolo aqui.
from .portal_licencas import (
    portal_licencas_office_list,
    portal_licencas_office_editar,
    portal_licencas_office_vincular,
    portal_licencas_office_desvincular,
    portal_licencas_colaboradores_list,
)

# ── Dashboards ───────────────────────────────────────────────────────────────
from .dashboards import (
    dashboard,
    dashboard_apresentacao_dados,
    cc_custos_dashboard,
    cc_custos_detalhe,
    cc_custos_export_pdf,
    preventiva_dashboard,
    preventiva_dashboard_export,
    custos_diretoria_dashboard,
    custos_diretoria_detalhe,
    validacao_custos_planilha,
    validacao_custos_export_excel,
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
    requisicoes_export_excel,
    itens_padrao_export_excel,
)

# ── Termos ───────────────────────────────────────────────────────────────────
from .termos import (
    termo_entrega_form,
    termo_devolucao_form,
    termo_desligamento_form,
)

# ── Termos — assinatura digital em tablet ────────────────────────────────────
# `termo_assinar` e `termo_assinar_enviar` são as DUAS únicas views anônimas
# deste bloco (o colaborador que assina não tem login); a autorização delas é
# o token opaco. As demais são `@login_required`.
from .termos_assinatura import (
    termo_assinatura_preparar,
    termo_assinatura_desligamento,
    termo_assinatura_painel,
    termo_assinatura_status,
    termo_assinatura_novo_link,
    termo_assinatura_cancelar,
    termo_assinatura_reenviar_via,
    termo_assinar,
    termo_assinar_enviar,
    termo_assinar_concluido,
    termo_recibo_pdf,
    termo_recibo_email,
    termo_assinatura_colaboradores_api,
    termo_assinatura_equipamentos_api,
    termo_assinatura_list,
    termo_assinatura_detalhe,
    termo_assinatura_pdf,
    termo_assinatura_verificar,
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

# ── Arquivos de MEDIA_ROOT (entrega autenticada) ─────────────────────────────
from .media_protegida import media_protegida

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
    ninja_oauth_conectar,
    ninja_oauth_callback,
    ninja_sincronizar,
    ninja_desconectar,
    ninja_licencas_liberar,
    ninja_licencas_marcar,
    ninja_licencas_enviar,
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
    kiosk_atualizacao_apk,
    kiosk_instalador_download,
    # Dashboard interno
    quiosque_dashboard,
    quiosque_apresentacao,
    quiosque_sobre,
    quiosque_indicadores,
    quiosque_detalhe,
    quiosque_mapa_atualizar,
    quiosque_checkins_exportar,
    quiosque_mapa_exportar,
    quiosque_mapa_sinal,
    quiosque_matriculas,
    quiosque_matricula_excluir,
    quiosque_matricula_renomear,
    quiosque_matricula_qrcode,
    quiosque_apk_upload,
    quiosque_apk_versao_anterior_baixar,
    quiosque_instalador_gerar,
    quiosque_instalador_status,
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
    portal_troca_antecipada_list,
    portal_troca_antecipada_nova,
    portal_notificacoes_marcar_lidas,
    portal_licencas_list,
    portal_ajuda,
    portal_ajuda_diagrama,
    portal_separacao_envio_list,
    portal_separacao_devolucao_list,
    portal_separacao_lote_detail,
)

# ── Portal do Fornecedor — Lotes de Envio (troca antecipada / equipamento novo) ─
from .portal_lote_envio_fornecedor import (
    portal_lote_envio_list,
    portal_lote_envio_detail,
    portal_lote_envio_item_enviar,
    portal_lote_envio_item_excluir,
    portal_lote_envio_item_editar,
    portal_lote_envio_item_equipamento_novo,
    portal_lote_envio_item_reparo_concluido,
)

# ── Manutenção externa — lado TI (recebimentos) ──────────────────────────────
from .manutencao import (
    manutencao_recebimentos,
    manutencao_recebimento_detail,
    manutencao_recebimento_acao,
    manutencao_recebimentos_ajuda,
    envio_fornecedor_list,
    envio_fornecedor_detail,
    envio_fornecedor_item_confirmar,
    envio_fornecedor_confirmar_lote,
    manutencao_painel,
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

# -- Mapa de Rede (topologia + portas de switch) ------------------------------
from .rede_mapa import (
    rede_mapa,
    rede_mapa_criar,
    rede_mapa_excluir,
    rede_mapa_salvar,
    rede_mapa_export,
    rede_grafo_api,
    rede_impacto_api,
    rede_historico_api,
    rede_dispositivo_api,
    rede_portas_api,
    rede_porta_api,
    rede_enlace_api,
    rede_inventario_api,
)

from .rede_planta import (
    rede_planta,
    rede_planta_api,
    rede_planta_export,
    rede_posicao_api,
    rede_rota_api,
    rede_impacto_rota_api,
    rede_rack_api,
    rede_rack_elevacao_api,
    rede_rack_excluir,
)

# ── Solicitações de Compra (Kanban) ───────────────────────────────────────────
from .requisicoes import (
    requisicoes_kanban,
    requisicao_item_mover,
    requisicao_item_create,
    requisicao_item_update,
    requisicao_item_delete,
    requisicao_item_detail,
    requisicao_item_acao,
    requisicao_item_receber_compra,
    requisicao_item_comentar,
    requisicao_comentario_excluir,
    requisicao_create_from_itens,
    requisicao_detail,
    requisicao_vincular_itens,
    requisicao_acao,
    requisicoes_list,
    requisicao_itens_list,
    requisicoes_dashboard,
    itens_padrao_list,
    itens_padrao_create,
    itens_padrao_update,
    itens_padrao_delete,
    itens_padrao_importar,
)

# -- Contratacoes (admissao / onboarding de TI) ------------------------------
from .contratacoes import (  # noqa: E402
    contratacao_painel,
    contratacao_create,
    contratacao_update,
    contratacao_detail,
    contratacao_delete,
    contratacao_etapa,
    contratacao_sistema_toggle,
    contratacao_licenca_toggle,
    contratacao_preencher,
    contratacao_colaborador_corrigir,
    contratacao_vaga_reabrir,
    contratacao_colaborador_cadastrar,
    contratacao_integracao,
    contratacao_concluir,
    contratacao_etapa_processo,
    contratacao_reservar,
    contratacao_reserva_liberar,
    contratacao_reserva_alerta,
    equipamento_reservar,
    contratacao_chamados_solicitar,
    contratacao_chamado_reenviar,
    contratacao_chamado_identificador,
    chamados_lista,
    chamado_detail,
    chamado_responder,
    chamado_mensagem_reenviar,
    chamado_atendimento,
    chamado_anexo_download,
    chamados_sincronizacao,
    chamados_sincronizacao_acao,
    sistema_corporativo_list,
    sistema_corporativo_create,
    sistema_corporativo_update,
    sistema_corporativo_delete,
)

from .acessos_restritos import (  # noqa: E402
    acesso_restrito_gerenciar,
    acesso_restrito_conceder,
    acesso_restrito_revogar,
)

# ── Recuperação de senha ("esqueci minha senha") ─────────────────────────────
from .autenticacao import (  # noqa: E402
    RecuperarSenhaView,
    RecuperarSenhaEnviadoView,
    RecuperarSenhaConfirmarView,
    RecuperarSenhaConcluidoView,
)

# ── Referência viva do design system (tokens + componentes) ──────────────────
from .design_system import design_system  # noqa: E402

# ── Telas de erro (preview em desenvolvimento) ───────────────────────────────
from .erros import erro_preview  # noqa: E402

# ── Apresentação pública (vitrine alcançada pela tela de acesso) ─────────────
from .apresentacao import apresentacao  # noqa: E402

# ── Monitoramento TIM M2M (chips, consumo, sessões, alertas e configuração) ──
from .m2m import (  # noqa: E402
    m2m_visao_geral,
    m2m_chips,
    m2m_chip_detalhe,
    m2m_chip_novo,
    m2m_chip_editar,
    m2m_consumo,
    m2m_sessoes,
    m2m_alertas,
    m2m_config,
)
from .m2m_api import (  # noqa: E402
    m2m_chip_atualizar,
    m2m_chip_monitoramento,
    m2m_consulta_payload,
    m2m_opcoes,
    m2m_sincronizacao,
    m2m_alerta_tratar,
    m2m_config_token,
    m2m_config_frequencia,
    m2m_config_mapeamento,
    m2m_config_previa,
    m2m_config_testar,
    m2m_reinterpretar,
)

# ── Frotas e Conectividade (veículos, movimentações do chip, importação, configuração) ──
from .frota_equipamentos import (  # noqa: E402
    frota_visao_geral,
    frota_veiculos,
    frota_equipamentos_proprios,
    frota_equipamentos_terceiros,
    frota_equipamentos_validar_lote,
)
from .frota_categorias import (  # noqa: E402
    frota_categorias,
    frota_config_tipos,
    frota_cadastro_salvar,
)
from .frota_consultas import (  # noqa: E402
    frota_equip_movimentacoes,
    frota_equip_historico,
)
from .frota_composicao import frota_composicao  # noqa: E402
from .frota_relatorios import frota_relatorios  # noqa: E402
from .frota_importacao_equipamentos import (  # noqa: E402
    frota_equip_importar,
    frota_equip_importar_modelo,
    frota_equip_importacoes,
    frota_equip_importacao,
    frota_equip_importacao_mapear,
    frota_equip_importacao_confirmar,
    frota_equip_importacao_cancelar,
    frota_equip_importacao_relatorio,
)
from .frota import (  # noqa: E402
    frota_movimentacoes,
    frota_importar,
    frota_importacoes,
    frota_importacao,
    frota_config_operadoras,
    frota_config_regras,
    frota_veiculo_detalhe,
    frota_veiculo_novo,
    frota_veiculo_editar,
)
from .frota_api import (  # noqa: E402
    frota_veiculo_status,
    frota_veiculo_exclusao,
    frota_veiculo_validar,
    frota_veiculo_movimentar,
    frota_vincular,
    frota_desvincular,
    frota_transferir,
    frota_substituir,
    frota_retornar,
    frota_inativar,
    frota_opcoes,
    frota_importacao_confirmar,
    frota_importacao_cancelar,
    frota_importacao_relatorio,
    frota_modelo_planilha,
    frota_config_tipo_salvar,
    frota_tipo_rapido,
    frota_config_operadora_salvar,
    frota_config_regra_salvar,
    frota_config_regra_restaurar,
)
# Chip na ficha do equipamento de TI (tablet, celular): rotas do ambiente TI, permissão do módulo de chips (regra 62).
from .chip_estoque_ti import (  # noqa: E402
    chips_conciliacao,
    chip_estoque_conciliar,
    chip_estoque_ligar,
    chip_estoque_desligar,
    chip_estoque_criar_chip,
    chip_estoque_cadastrar_itens,
    chip_estoque_opcoes,
    chip_estoque_opcoes_itens,
)
from .equipamento_chip import (  # noqa: E402
    equipamento_chip_vincular,
    equipamento_chip_desvincular,
    equipamento_chip_substituir,
    equipamento_chip_opcoes,
)

# ── Tecnologia agrícola do Zelo Agro: estoque tecnológico e catálogo ──
from .tec_estoque import (  # noqa: E402
    tec_visao_geral,
    tec_equipamentos,
    tec_equipamento_novo,
    tec_equipamento_detalhe,
    tec_equipamento_editar,
    tec_equipamento_mover,
    tec_equipamento_inativar,
    tec_materiais,
    tec_material_detalhe,
    tec_material_entrada,
    tec_material_saida,
    tec_material_ajuste,
    tec_material_transferir,
    tec_movimentacoes,
)
from .tec_instalacao import (  # noqa: E402
    tec_equipamento_instalar,
    tec_veiculo_instalar,
    tec_instalacao_retirar,
    tec_instalacao_transferir,
    tec_instalacao_substituir,
    tec_material_aplicar,
    tec_material_devolver,
)
from .agro_mapa import agro_mapa, agro_mapa_config, agro_mapa_dados  # noqa: E402
from .agro_infra import (  # noqa: E402
    infra_visao_geral,
    infra_estruturas,
    infra_estrutura_nova,
    infra_estrutura_editar,
    infra_enlaces,
    infra_enlace_novo,
    infra_enlace_editar,
)
from .tec_catalogo import (  # noqa: E402
    tec_catalogo,
    tec_catalogo_novo,
    tec_catalogo_editar,
)

# Ambientes do Zelo (TI · Agro) — services/ambiente_service.py
from .ambiente import (  # noqa: E402
    agro_visao_geral,
    inicio,
    rota_legada_frota,
)
