from django.urls import path
from django.contrib.auth import views as auth_views
from . import views

urlpatterns = [

    # ── Home & Autenticação ──────────────────────────────────────────────────
    path("", views.sistema_noticias, name="home"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("dashboard/dados/", views.dashboard_apresentacao_dados, name="dashboard_apresentacao_dados"),
    path("sobre/", views.sobre_plataforma, name="sobre_plataforma"),
    path("login/", auth_views.LoginView.as_view(template_name="login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(next_page="login"), name="logout"),

    # ── Categorias ──────────────────────────────────────────────────────────
    path("categorias/", views.categorias_list, name="categorias_list"),
    path("categorias/novo/", views.categoria_create, name="categoria_create"),
    path("categorias/<int:pk>/editar/", views.categoria_update, name="categoria_update"),
    path("categorias/<int:pk>/excluir/", views.categoria_delete, name="categoria_delete"),

    # ── Subtipos ────────────────────────────────────────────────────────────
    path("subtipos/", views.subtipo_list, name="subtipo_list"),
    path("subtipos/novo/", views.subtipo_create, name="subtipo_create"),
    path("subtipos/<int:pk>/", views.subtipo_detail, name="subtipo_detail"),
    path("subtipos/<int:pk>/editar/", views.subtipo_update, name="subtipo_update"),
    path("subtipos/<int:pk>/excluir/", views.subtipo_delete, name="subtipo_delete"),

    # ── Funções ─────────────────────────────────────────────────────────────
    path("funcoes/", views.funcao_list, name="funcoes_list"),
    path("funcoes/novo/", views.funcao_form, name="funcao_create"),
    path("funcoes/<int:pk>/editar/", views.funcao_form, name="funcao_edit"),
    path("funcoes/<int:pk>/excluir/", views.funcao_delete, name="funcao_delete"),

    # ── Localidades ─────────────────────────────────────────────────────────
    path("localidades/", views.localidade_list, name="localidade_list"),
    path("localidades/novo/", views.localidade_create, name="localidade_create"),
    path("localidades/<int:pk>/", views.localidade_detail, name="localidade_detail"),
    path("localidades/<int:pk>/editar/", views.localidade_update, name="localidade_update"),
    path("localidades/<int:pk>/excluir/", views.localidade_delete, name="localidade_delete"),

    # ── Centros de Custo ────────────────────────────────────────────────────
    path("centros-custo/", views.centrocusto_list, name="centrocusto_list"),
    path("centros-custo/novo/", views.centrocusto_create, name="centrocusto_create"),
    path("centros-custo/pdf/", views.centrocusto_export_pdf, name="centrocusto_export_pdf"),
    path("centros-custo/<int:pk>/", views.centrocusto_detail, name="centrocusto_detail"),
    path("centros-custo/<int:pk>/editar/", views.centrocusto_update, name="centrocusto_update"),
    path("centros-custo/<int:pk>/excluir/", views.centrocusto_delete, name="centrocusto_delete"),

    # ── Fornecedores ────────────────────────────────────────────────────────
    path("fornecedores/", views.fornecedor_list, name="fornecedor_list"),
    path("fornecedores/novo/", views.fornecedor_create, name="fornecedor_create"),
    path("fornecedores/pdf/", views.fornecedor_export_pdf, name="fornecedor_export_pdf"),
    path("fornecedores/acessos/", views.fornecedor_acessos_list, name="fornecedor_acessos_list"),
    path("fornecedores/acessos/<int:pk>/acao/", views.fornecedor_acesso_acao, name="fornecedor_acesso_acao"),
    path("fornecedores/<int:pk>/", views.fornecedor_detail, name="fornecedor_detail"),
    path("fornecedores/<int:pk>/editar/", views.fornecedor_update, name="fornecedor_update"),
    path("fornecedores/<int:pk>/acesso-portal/", views.fornecedor_portal_acesso, name="fornecedor_portal_acesso"),
    path("fornecedores/<int:pk>/excluir/", views.fornecedor_delete, name="fornecedor_delete"),

    # ── Usuários ────────────────────────────────────────────────────────────
    path("usuarios/", views.usuario_list, name="usuario_list"),
    path("usuarios/cadastrar/", views.usuario_create, name="usuario_create"),
    path("usuarios/importar/", views.usuario_importar, name="usuario_importar"),
    path("usuarios/dashboard/", views.usuario_dashboard, name="usuario_dashboard"),
    path("usuarios/hierarquia/", views.hierarquia_usuarios, name="hierarquia_usuarios"),
    path("usuarios/organograma/", views.organograma_usuarios, name="organograma_usuarios"),
    path("usuarios/organograma/membros/", views.organograma_membros_supervisor, name="organograma_membros_supervisor"),
    path("usuarios/<int:pk>/", views.usuario_detail, name="usuario_detail"),
    path("usuarios/<int:pk>/editar/", views.usuario_update, name="usuario_update"),
    path("usuarios/<int:pk>/excluir/", views.usuario_delete, name="usuario_delete"),
    path("usuarios/<int:pk>/desligar/", views.usuario_desligar, name="usuario_desligar"),
    path("usuarios/<int:pk>/remover-todas-licencas/", views.usuario_remover_todas_licencas, name="usuario_remover_todas_licencas"),

    # ── Equipamentos / Itens ────────────────────────────────────────────────
    path("equipamentos/", views.equipamentos_list, name="equipamentos_list"),
    path("equipamentos/cadastrar/", views.item_create, name="cadastrar_equipamento"),
    path("equipamentos/importar/", views.importar_planilha, name="importar_planilha"),
    path("equipamentos/exportar/", views.equipamentos_exportar, name="equipamentos_exportar"),
    path("equipamentos/<int:pk>/", views.equipamento_detalhe, name="equipamento_detalhe"),
    path("equipamentos/<int:pk>/qr/", views.equipamento_qr, name="equipamento_qr"),
    path("equipamentos/<int:pk>/editar/", views.item_update, name="item_update"),
    path("equipamentos/<int:pk>/excluir/", views.equipamento_excluir, name="equipamento_excluir"),
    path("equipamentos/<int:pk>/termo/entrega/", views.termo_entrega_form, name="termo_entrega_form"),
    path("equipamentos/<int:pk>/termo/devolucao/", views.termo_devolucao_form, name="termo_devolucao_form"),
    path("equipamentos/<int:pk>/monitoracao/", views.item_monitoracao, name="item_monitoracao"),
    path("monitoracao/relatorio/", views.monitoracao_relatorio, name="monitoracao_relatorio"),

    # ── Locações ────────────────────────────────────────────────────────────
    path("locacoes/", views.locacoes_list, name="locacoes_list"),
    path("locacoes/novo/", views.locacao_create, name="locacao_create"),
    path("locacoes/<int:pk>/editar/", views.locacao_update, name="locacao_update"),
    path("locacoes/<int:pk>/excluir/", views.locacao_delete, name="locacao_delete"),

    # ── Comentários ─────────────────────────────────────────────────────────
    path("comentarios/", views.comentarios_list, name="comentarios_list"),
    path("comentarios/novo/", views.comentario_create, name="comentario_create"),
    path("comentarios/<int:pk>/editar/", views.comentario_update, name="comentario_update"),
    path("comentarios/<int:pk>/excluir/", views.comentario_delete, name="comentario_delete"),

    # ── Movimentações ───────────────────────────────────────────────────────
    path("movimentacoes/", views.movimentacao_list, name="movimentacao_list"),
    path("movimentacoes/nova/", views.movimentacao_create, name="movimentacao_create"),
    path("movimentacoes/pdf/", views.movimentacao_export_pdf, name="movimentacao_export_pdf"),
    path("movimentacoes/api/lotes-por-item/", views.api_lotes_por_item, name="api_lotes_por_item"),
    path("movimentacoes/api/item-devolucao-info/", views.api_item_devolucao_info, name="api_item_devolucao_info"),
    path("movimentacoes/<int:pk>/", views.movimentacao_detail, name="movimentacao_detail"),
    path("movimentacoes/<int:pk>/editar/", views.movimentacao_update, name="movimentacao_update"),
    path("movimentacoes/<int:pk>/excluir/", views.movimentacao_delete, name="movimentacao_delete"),

    # ── Ciclos de Manutenção ────────────────────────────────────────────────
    path("ciclos/", views.ciclos_list, name="ciclos_list"),
    path("ciclos/novo/<int:item_pk>/", views.ciclo_create, name="ciclo_create"),
    path("ciclos/<int:pk>/editar/", views.ciclo_update, name="ciclo_update"),
    path("ciclos/<int:pk>/excluir/", views.ciclo_delete, name="ciclo_delete"),

    # ── Preventivas ─────────────────────────────────────────────────────────
    path("preventiva/", views.preventiva_list, name="preventiva_list"),
    path("preventiva/iniciar/", views.preventiva_start, name="preventiva_start"),
    path("preventiva/item/<int:item_id>/iniciar/", views.preventiva_start, name="preventiva_start_item"),
    path("preventiva/<int:pk>/", views.preventiva_detail, name="preventiva_detail"),
    path("preventiva/<int:pk>/executar/", views.preventiva_exec, name="preventiva_exec"),
    path("preventiva/<int:pk>/agendar/", views.preventiva_agendar, name="preventiva_agendar"),
    path("preventiva/plano/", views.preventiva_plano, name="preventiva_plano"),
    path("preventiva/agendadas/", views.preventiva_agendadas, name="preventiva_agendadas"),
    path("preventiva/desempenho/", views.tecnico_desempenho, name="tecnico_desempenho"),
    path("preventiva/desempenho/apontamentos/exportar/", views.apontamentos_horas_export, name="apontamentos_horas_export"),
    path("preventiva/minhas-atividades/", views.minhas_atividades, name="minhas_atividades"),
    # Checklists de preventiva
    path("preventiva/checklists/", views.checklist_list, name="checklist_list"),
    path("preventiva/checklists/novo/", views.checklist_form, name="checklist_create"),
    path("preventiva/checklists/<int:pk>/editar/", views.checklist_form, name="checklist_form"),
    path("preventiva/checklists/<int:pk>/excluir/", views.checklist_delete, name="checklist_delete"),
    path("preventiva/checklists/<int:checklist_pk>/pergunta/novo/", views.pergunta_form, name="pergunta_create"),
    path("preventiva/checklists/<int:checklist_pk>/pergunta/<int:pk>/editar/", views.pergunta_form, name="pergunta_form"),
    path("preventiva/checklists/<int:checklist_pk>/pergunta/<int:pk>/excluir/", views.pergunta_delete, name="pergunta_delete"),

    # ── Licenças ────────────────────────────────────────────────────────────
    path("licencas/", views.licenca_list, name="licenca_list"),
    path("licencas/nova/", views.licenca_form, name="licenca_create"),
    path("licencas/mov/", views.mov_licenca_list, name="mov_licenca_list"),
    path("licencas/mov/nova/", views.mov_licenca_form, name="mov_licenca_form"),
    path("licencas/lotes/", views.licenca_lote_list, name="licenca_lote_list"),
    path("licencas/lotes/novo/", views.licenca_lote_form, name="licenca_lote_novo"),
    path("licencas/lotes/<int:pk>/", views.licenca_lote_form, name="licenca_lote_edit"),
    path("licencas/<int:pk>/", views.licenca_detail, name="licenca_detail"),
    path("licencas/<int:pk>/editar/", views.licenca_form, name="licenca_update"),
    path("licencas/<int:pk>/exportar-excel/", views.licenca_export_excel, name="licenca_export_excel"),
    path("licencas/devolver-rapido/<int:usuario_id>/<int:licenca_id>/", views.licenca_devolver_rapido, name="licenca_devolver_rapido"),

    # ── Dashboards ──────────────────────────────────────────────────────────
    path("dashboards/custos-cc/", views.cc_custos_dashboard, name="cc_custos_dashboard"),
    path("dashboards/custos-diretoria/", views.custos_diretoria_dashboard, name="custos_diretoria_dashboard"),
    path("dashboards/custos-diretoria/detalhe/", views.custos_diretoria_detalhe, name="custos_diretoria_detalhe"),
    path("dashboards/custos-cc/pdf/", views.cc_custos_export_pdf, name="cc_custos_export_pdf"),
    path("dashboards/custos-cc/exportar-excel/", views.custo_cc_export_excel, name="custo_cc_export_excel"),
    path("dashboards/toner/", views.toner_cc_dashboard, name="dashboard_toner"),
    path("dashboards/toner/exportar-excel/", views.toner_cc_export_excel, name="toner_cc_export_excel"),
    path("dashboards/licencas/", views.licencas_dashboard, name="licencas_dashboard"),
    path("dashboards/preventivas/", views.preventiva_dashboard, name="preventiva_dashboard"),
    path("preventiva/sincronizar-programacao/", views.preventiva_sincronizar_programacao, name="preventiva_sincronizar_programacao"),

    # ── Status Board ───────────────────────────────────────────────────────
    path("status-board/",                   views.status_board,         name="status_board"),

    # ── Plantas / Mapa de Infraestrutura ───────────────────────────────────
    path("plantas/",                        views.planta_list,          name="planta_list"),
    path("plantas/nova/",                   views.planta_create,        name="planta_create"),
    path("plantas/api/prtg-status/",        views.prtg_status_api,      name="prtg_status_api"),
    path("plantas/api/prtg-buscar/",        views.prtg_search_api,      name="prtg_search_api"),
    path("plantas/api/itens-buscar/",       views.item_search_api,      name="item_search_api"),
    path("plantas/<int:pk>/",               views.planta_viewer,        name="planta_viewer"),
    path("plantas/<int:pk>/editar/",        views.planta_update,        name="planta_update"),
    path("plantas/<int:pk>/excluir/",       views.planta_delete,        name="planta_delete"),
    path("plantas/<int:pk>/editor/",        views.planta_editor,        name="planta_editor"),
    path("plantas/<int:pk>/salvar/",                      views.planta_salvar_layout,  name="planta_salvar_layout"),
    path("plantas/<int:pk>/check-version/",               views.planta_check_version,  name="planta_check_version"),
    path("plantas/<int:pk>/historico/",                   views.planta_historico_api,  name="planta_historico_api"),
    path("plantas/<int:pk>/restaurar/<int:hist_pk>/",     views.planta_restaurar_versao, name="planta_restaurar_versao"),
    path("plantas/<int:pk>/tv/",                          views.planta_tv,                  name="planta_tv"),
    path("plantas/tv/",                                   views.planta_tv_lista,             name="planta_tv_lista"),
    path("plantas/tv/gerenciar/",                         views.planta_tv_gerenciar,         name="planta_tv_gerenciar"),
    path("plantas/tv/gerenciar/acao/",                    views.planta_tv_gerenciar_acao,    name="planta_tv_gerenciar_acao"),
    path("plantas/monitor/",                              views.prtg_monitor,                name="prtg_monitor"),
    path("plantas/monitor/exportar/",                     views.prtg_monitor_export,         name="prtg_monitor_export"),

    # ── Avisos & Relatórios ─────────────────────────────────────────────────
    path("avisos/contratos-a-vencer/", views.avisos_contratos_vencer, name="avisos_contratos_vencer"),
    path("avisos/contratos-a-vencer/exportar-excel/", views.avisos_contratos_vencer_export_excel, name="avisos_contratos_vencer_export_excel"),

    # ── Alertas por E-mail ──────────────────────────────────────────────────
    path("alertas/", views.alertas_dashboard, name="alertas_dashboard"),
    path("alertas/enviar/", views.alertas_enviar, name="alertas_enviar"),
    path("alertas/toggle/", views.alertas_toggle, name="alertas_toggle"),

    # ── NinjaOne RMM (via importação de CSV) ───────────────────────────────
    path("ninja/",                    views.ninja_dashboard,     name="ninja_dashboard"),
    path("ninja/dispositivos/",       views.ninja_dispositivos,  name="ninja_dispositivos"),
    path("ninja/nao-cadastrados/",    views.ninja_nao_cadastrados, name="ninja_nao_cadastrados"),
    path("ninja/login-validacao/",    views.ninja_login_validacao, name="ninja_login_validacao"),
    path("ninja/login-validacao/revalidar/", views.ninja_login_revalidar, name="ninja_login_revalidar"),
    path("ninja/login/<int:pk>/",     views.ninja_login_detalhe, name="ninja_login_detalhe"),
    path("ninja/relatorio/",          views.ninja_relatorio,     name="ninja_relatorio"),
    path("ninja/importar/",           views.ninja_importar,      name="ninja_importar"),

    # ── Módulo Quiosque — API do dispositivo (app Android) ──────────────────
    path("api/quiosque/enroll/",              views.kiosk_enroll,       name="kiosk_enroll"),
    path("api/quiosque/checkin/",             views.kiosk_checkin,      name="kiosk_checkin"),
    path("api/quiosque/config/",              views.kiosk_config,       name="kiosk_config"),
    path("api/quiosque/comando/<int:pk>/ack/", views.kiosk_comando_ack, name="kiosk_comando_ack"),

    # ── Módulo Quiosque — Dashboard interno (TI) ────────────────────────────
    path("quiosque/",                  views.quiosque_dashboard,     name="quiosque_dashboard"),
    path("quiosque/mapa/",             views.quiosque_mapa,          name="quiosque_mapa"),
    path("quiosque/matriculas/",       views.quiosque_matriculas,    name="quiosque_matriculas"),
    path("quiosque/matriculas/<int:pk>/excluir/", views.quiosque_matricula_excluir, name="quiosque_matricula_excluir"),
    path("quiosque/<int:pk>/",         views.quiosque_detalhe,       name="quiosque_detalhe"),
    path("quiosque/<int:pk>/config/",  views.quiosque_config_editar, name="quiosque_config_editar"),
    path("quiosque/<int:pk>/comando/", views.quiosque_comando_novo,  name="quiosque_comando_novo"),
    path("quiosque/<int:pk>/revogar/", views.quiosque_revogar,       name="quiosque_revogar"),
    path("quiosque/<int:pk>/excluir/", views.quiosque_excluir,       name="quiosque_excluir"),

    # ── Portal do Fornecedor (área isolada) ─────────────────────────────────
    path("portal/", views.portal_home, name="portal_home"),
    path("portal/equipamentos/", views.portal_equipamentos_list, name="portal_equipamentos_list"),
    path("portal/equipamentos/exportar/", views.portal_equipamentos_export, name="portal_equipamentos_export"),
    path("portal/equipamentos/<int:pk>/", views.portal_equipamento_detail, name="portal_equipamento_detail"),
    path("portal/manutencao/", views.portal_manutencao_list, name="portal_manutencao_list"),
    path("portal/manutencao/<int:pk>/", views.portal_manutencao_detail, name="portal_manutencao_detail"),
    path("portal/licencas/", views.portal_licencas_list, name="portal_licencas_list"),

    # ── Manutenção externa — recebimentos (lado TI) ─────────────────────────
    path("manutencao/recebimentos/", views.manutencao_recebimentos, name="manutencao_recebimentos"),
    path("manutencao/recebimentos/<int:pk>/", views.manutencao_recebimento_detail, name="manutencao_recebimento_detail"),
    path("manutencao/recebimentos/<int:pk>/concluir/", views.manutencao_recebimento_acao, name="manutencao_recebimento_acao"),

    # ── Inteligência de Sistema ─────────────────────────────────────────────
    path("inteligencia/", views.sistema_inteligencia_dashboard, name="sistema_inteligencia_dashboard"),
    path("inteligencia/busca-global/", views.sistema_inteligencia_busca_global, name="sistema_inteligencia_busca_global"),
    path("inteligencia/exportar-csv/", views.sistema_inteligencia_export_csv, name="sistema_inteligencia_export_csv"),
    path("noticias/", views.sistema_noticias, name="sistema_noticias"),

]
