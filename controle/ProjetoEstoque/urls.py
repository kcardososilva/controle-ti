from django.urls import path
from django.contrib.auth import views as auth_views
from . import views

urlpatterns = [

    # ── Home & Autenticação ──────────────────────────────────────────────────
    path("", views.dashboard, name="home"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("dashboard/dados/", views.dashboard_apresentacao_dados, name="dashboard_apresentacao_dados"),
    path("sobre/", views.sobre_plataforma, name="sobre_plataforma"),
    # Vitrine PÚBLICA (sem login) — é o link "Conheça o Zelo" da tela de
    # acesso, então quem abre ainda não tem sessão. Não consulta o banco: a
    # versão com números reais é a `/sobre/` acima, que exige login.
    path("apresentacao/", views.apresentacao, name="apresentacao"),
    # Catálogo de tokens e componentes do design system. Restrita a staff:
    # não tem dado do negócio, mas não é tela de operação.
    path("sistema/design/", views.design_system, name="design_system"),
    # Preview das telas de erro (pivô irrigando). Em dev os handlers não
    # entram — o Django mostra o traceback —, então é por aqui que se
    # confere o visual. Staff apenas.
    path("sistema/erro/<str:codigo>/", views.erro_preview, name="erro_preview"),
    path("login/", auth_views.LoginView.as_view(template_name="login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(next_page="login"), name="logout"),
    path("perfil/", views.admin_perfil, name="admin_perfil"),

    # ── Recuperação de senha ("esqueci minha senha") ─────────────────────────
    # Os NOMES das rotas são os do Django (`password_reset*`): o template de
    # e-mail nativo e o `PasswordResetConfirmView` montam o link por
    # `{% url 'password_reset_confirm' %}`, então renomear quebra o link do
    # e-mail sem quebrar nenhuma tela — falha silenciosa. Ver
    # views/autenticacao.py e services/recuperacao_senha_service.py.
    path("senha/recuperar/",
         views.RecuperarSenhaView.as_view(), name="password_reset"),
    path("senha/recuperar/enviado/",
         views.RecuperarSenhaEnviadoView.as_view(), name="password_reset_done"),
    path("senha/nova/<uidb64>/<token>/",
         views.RecuperarSenhaConfirmarView.as_view(), name="password_reset_confirm"),
    path("senha/nova/concluido/",
         views.RecuperarSenhaConcluidoView.as_view(), name="password_reset_complete"),

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
    path("fornecedores/rapido/", views.fornecedor_quick_create, name="fornecedor_quick_create"),
    path("fornecedores/pdf/", views.fornecedor_export_pdf, name="fornecedor_export_pdf"),
    path("fornecedores/acessos/", views.fornecedor_acessos_list, name="fornecedor_acessos_list"),
    path("fornecedores/acessos/<int:pk>/acao/", views.fornecedor_acesso_acao, name="fornecedor_acesso_acao"),
    path("fornecedores/<int:pk>/", views.fornecedor_detail, name="fornecedor_detail"),
    path("fornecedores/<int:pk>/editar/", views.fornecedor_update, name="fornecedor_update"),
    path("fornecedores/<int:pk>/acesso-portal/", views.fornecedor_portal_acesso, name="fornecedor_portal_acesso"),
    path("fornecedores/<int:pk>/excluir/", views.fornecedor_delete, name="fornecedor_delete"),

    # ── Usuários ────────────────────────────────────────────────────────────
    path("usuarios/", views.usuario_list, name="usuario_list"),
    path("usuarios/exportar-excel/", views.usuario_export_excel, name="usuario_export_excel"),
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
    path("usuarios/<int:usuario_id>/termo/desligamento/", views.termo_desligamento_form, name="termo_desligamento_form"),

    # ── Equipamentos / Itens ────────────────────────────────────────────────
    path("equipamentos/", views.equipamentos_list, name="equipamentos_list"),
    path("equipamentos/cadastrar/", views.item_create, name="cadastrar_equipamento"),
    path("equipamentos/importar/", views.importar_planilha, name="importar_planilha"),
    path("equipamentos/exportar/", views.equipamentos_exportar, name="equipamentos_exportar"),
    path("equipamentos/<int:pk>/", views.equipamento_detalhe, name="equipamento_detalhe"),
    path("equipamentos/<int:pk>/qr/", views.equipamento_qr, name="equipamento_qr"),
    path("equipamentos/<int:pk>/editar/", views.item_update, name="item_update"),
    path("equipamentos/<int:pk>/excluir/", views.equipamento_excluir, name="equipamento_excluir"),
    path("equipamentos/<int:pk>/restaurar/", views.equipamento_restaurar, name="equipamento_restaurar"),
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
    path("movimentacoes/lote/", views.movimentacao_lote_create, name="movimentacao_lote_create"),
    path("movimentacoes/lote/termo/", views.movimentacao_lote_termo, name="movimentacao_lote_termo"),
    path("movimentacoes/pdf/", views.movimentacao_export_pdf, name="movimentacao_export_pdf"),
    path("movimentacoes/api/lotes-por-item/", views.api_lotes_por_item, name="api_lotes_por_item"),
    path("movimentacoes/api/item-devolucao-info/", views.api_item_devolucao_info, name="api_item_devolucao_info"),
    path("movimentacoes/api/itens-disponiveis-lote/", views.api_itens_disponiveis_lote, name="api_itens_disponiveis_lote"),
    path("movimentacoes/termos/", views.repositorio_termos, name="repositorio_termos"),

    # ── Termos — assinatura digital em tablet ───────────────────────────────
    # `termos/assinar/<token>/` e o `/enviar/` dela são as ÚNICAS rotas
    # anônimas do módulo: quem assina é o colaborador, que não tem login
    # (ver views/termos_assinatura.py). Ficam ANTES das rotas por `<int:pk>`
    # porque o token é uma string com `-` e `_` — não colidem, mas manter a
    # ordem evita surpresa se algum dia o conversor mudar.
    path("termos/assinaturas/", views.termo_assinatura_list, name="termo_assinatura_list"),
    path("termos/assinaturas/api/colaboradores/", views.termo_assinatura_colaboradores_api, name="termo_assinatura_colaboradores_api"),
    path("termos/assinaturas/api/equipamentos/", views.termo_assinatura_equipamentos_api, name="termo_assinatura_equipamentos_api"),
    path("termos/assinaturas/verificar/", views.termo_assinatura_verificar, name="termo_assinatura_verificar"),
    # Desligamento tem rota PRÓPRIA, antes da genérica por `<str:tipo>`: o
    # fluxo não escolhe equipamento (o termo cobre tudo o que o colaborador
    # tem) e o efeito ao assinar é outro — encerra o vínculo inteiro.
    path("termos/assinaturas/novo/desligamento/", views.termo_assinatura_desligamento, name="termo_assinatura_desligamento"),
    path("termos/assinaturas/novo/<str:tipo>/", views.termo_assinatura_preparar, name="termo_assinatura_preparar"),
    path("termos/assinaturas/<int:pk>/", views.termo_assinatura_detalhe, name="termo_assinatura_detalhe"),
    path("termos/assinaturas/<int:pk>/painel/", views.termo_assinatura_painel, name="termo_assinatura_painel"),
    path("termos/assinaturas/<int:pk>/status/", views.termo_assinatura_status, name="termo_assinatura_status"),
    path("termos/assinaturas/<int:pk>/novo-link/", views.termo_assinatura_novo_link, name="termo_assinatura_novo_link"),
    path("termos/assinaturas/<int:pk>/cancelar/", views.termo_assinatura_cancelar, name="termo_assinatura_cancelar"),
    path("termos/assinaturas/<int:pk>/reenviar-via/", views.termo_assinatura_reenviar_via, name="termo_assinatura_reenviar_via"),
    path("termos/assinaturas/<int:pk>/pdf/", views.termo_assinatura_pdf, name="termo_assinatura_pdf"),
    path("termos/assinar/<str:token>/", views.termo_assinar, name="termo_assinar"),
    path("termos/assinar/<str:token>/enviar/", views.termo_assinar_enviar, name="termo_assinar_enviar"),
    path("termos/assinar/<str:token>/concluido/", views.termo_assinar_concluido, name="termo_assinar_concluido"),
    # Via do colaborador. Sem id na URL de propósito: quem autoriza é a marca
    # de sessão deixada pela assinatura, com prazo (ver views/termos_assinatura.py).
    path("termos/recibo/pdf/", views.termo_recibo_pdf, name="termo_recibo_pdf"),
    path("termos/recibo/email/", views.termo_recibo_email, name="termo_recibo_email"),
    path("movimentacoes/<int:pk>/", views.movimentacao_detail, name="movimentacao_detail"),
    path("movimentacoes/<int:pk>/editar/", views.movimentacao_update, name="movimentacao_update"),
    path("movimentacoes/<int:pk>/excluir/", views.movimentacao_delete, name="movimentacao_delete"),
    path("movimentacoes/<int:pk>/reverter/", views.movimentacao_reverter, name="movimentacao_reverter"),

    # ── Separação de Itens (Envio / Devolução) ──────────────────────────────
    path("separacao/envio/", views.separacao_envio_list, name="separacao_envio_list"),
    path("separacao/devolucao/", views.separacao_devolucao_list, name="separacao_devolucao_list"),
    path("separacao/lote/novo/", views.separacao_lote_create, name="separacao_lote_create"),
    path("separacao/lote/vincular-soltos/", views.separacao_lote_vincular_soltos, name="separacao_lote_vincular_soltos"),
    path("separacao/lote/<int:pk>/", views.separacao_lote_detail, name="separacao_lote_detail"),
    path("separacao/lote/<int:pk>/excluir/", views.separacao_lote_excluir, name="separacao_lote_excluir"),
    path("separacao/lote/<int:pk>/enviar/", views.separacao_lote_enviar, name="separacao_lote_enviar"),
    path("separacao/lote/<int:pk>/adicionar-item/", views.separacao_lote_item_adicionar, name="separacao_lote_item_adicionar"),
    path("separacao/item/<int:pk>/remover/", views.separacao_item_remover, name="separacao_item_remover"),
    path("separacao/item/<int:pk>/enviar/", views.separacao_item_enviar, name="separacao_item_enviar"),
    path("separacao/item/<int:pk>/desvincular/", views.separacao_lote_item_desvincular, name="separacao_lote_item_desvincular"),
    path("separacao/item/<int:pk>/nota-fiscal/anexar/", views.separacao_item_nota_fiscal_anexar, name="separacao_item_nota_fiscal_anexar"),
    path("separacao/nota-fiscal/<int:pk>/excluir/", views.separacao_nota_fiscal_excluir, name="separacao_nota_fiscal_excluir"),
    path("separacao/documento-fiscal/gerar/", views.documento_fiscal_gerar, name="documento_fiscal_gerar"),
    path("separacao/documento-fiscal/<int:pk>/pdf/", views.documento_fiscal_pdf_view, name="documento_fiscal_pdf_view"),

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
    path("preventiva/execucao/<int:execucao_pk>/editar/", views.preventiva_execucao_editar, name="preventiva_execucao_editar"),
    path("preventiva/execucao/<int:execucao_pk>/excluir/", views.preventiva_execucao_excluir, name="preventiva_execucao_excluir"),
    path("preventiva/<int:pk>/agendar/", views.preventiva_agendar, name="preventiva_agendar"),
    path("preventiva/plano/", views.preventiva_plano, name="preventiva_plano"),
    path("preventiva/agendadas/", views.preventiva_agendadas, name="preventiva_agendadas"),
    path("preventiva/desempenho/", views.tecnico_desempenho, name="tecnico_desempenho"),
    path("preventiva/desempenho/apontamentos/exportar/", views.apontamentos_horas_export, name="apontamentos_horas_export"),
    path("preventiva/minhas-atividades/", views.minhas_atividades, name="minhas_atividades"),
    path("preventiva/minha-agenda/", views.preventiva_minha_agenda, name="preventiva_minha_agenda"),
    # Metas de preventiva por área e ciclo de 90 dias
    path("preventiva/metas/", views.preventiva_metas, name="preventiva_metas"),
    path("preventiva/metas/configurar/", views.preventiva_metas_config, name="preventiva_metas_config"),
    path("preventiva/metas/exportar/", views.preventiva_metas_export, name="preventiva_metas_export"),
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
    # Saúde do acervo: assento preso em desligado, contador fora do razão,
    # pool esgotado. Rota antes de `licencas/<int:pk>/` não é necessária
    # (o conversor int não casa com "saude"), mas fica junto das irmãs.
    path("licencas/saude/", views.licenca_saude, name="licenca_saude"),
    path("licencas/api/colaboradores/", views.licenca_colaboradores_api, name="licenca_colaboradores_api"),
    path("licencas/saude/devolver-orfao/", views.licenca_devolver_orfao, name="licenca_devolver_orfao"),
    path("licencas/lotes/<int:pk>/reconciliar/", views.licenca_reconciliar_lote, name="licenca_reconciliar_lote"),
    path("licencas/<int:pk>/atribuir-rapido/", views.licenca_atribuir_rapido, name="licenca_atribuir_rapido"),
    # Calendario de renovacao: rota ANTES de `licencas/<int:pk>/` por seguranca
    # de leitura (o conversor `int` ja impediria o conflito, mas a ordem deixa
    # obvio que "renovacoes" nunca cai no detalhe).
    path("licencas/renovacoes/", views.licenca_renovacoes, name="licenca_renovacoes"),
    path("licencas/renovacoes/datas/", views.licenca_renovacao_datas, name="licenca_renovacao_datas"),
    path("licencas/mov/", views.mov_licenca_list, name="mov_licenca_list"),
    path("licencas/mov/nova/", views.mov_licenca_form, name="mov_licenca_form"),
    path("licencas/mov/exportar/", views.mov_licenca_export, name="mov_licenca_export"),
    path("licencas/lotes/", views.licenca_lote_list, name="licenca_lote_list"),
    path("licencas/lotes/novo/", views.licenca_lote_form, name="licenca_lote_novo"),
    path("licencas/lotes/<int:pk>/", views.licenca_lote_form, name="licenca_lote_edit"),
    path("licencas/<int:pk>/", views.licenca_detail, name="licenca_detail"),
    path("licencas/<int:pk>/editar/", views.licenca_form, name="licenca_update"),
    path("licencas/<int:pk>/exportar-excel/", views.licenca_export_excel, name="licenca_export_excel"),
    path("licencas/devolver-rapido/<int:usuario_id>/<int:licenca_id>/", views.licenca_devolver_rapido, name="licenca_devolver_rapido"),

    # ── Licenças Office (chave por equipamento) ────────────────────────────
    path("licencas-office/", views.licenca_office_list, name="licenca_office_list"),
    path("licencas-office/novo/", views.licenca_office_form, name="licenca_office_create"),
    path("licencas-office/importar/", views.licenca_office_importar, name="licenca_office_importar"),
    path("licencas-office/<int:pk>/editar/", views.licenca_office_form, name="licenca_office_update"),
    path("licencas-office/<int:pk>/excluir/", views.licenca_office_delete, name="licenca_office_delete"),
    path("equipamentos/<int:item_pk>/licenca-office/vincular/", views.licenca_office_vincular, name="licenca_office_vincular"),
    path("equipamentos/<int:item_pk>/licenca-office/desvincular/", views.licenca_office_desvincular, name="licenca_office_desvincular"),

    # ── Dashboards ──────────────────────────────────────────────────────────
    path("dashboards/custos-cc/", views.cc_custos_dashboard, name="cc_custos_dashboard"),
    path("dashboards/custos-cc/detalhe/", views.cc_custos_detalhe, name="cc_custos_detalhe"),
    path("dashboards/custos-diretoria/", views.custos_diretoria_dashboard, name="custos_diretoria_dashboard"),
    path("dashboards/custos-diretoria/detalhe/", views.custos_diretoria_detalhe, name="custos_diretoria_detalhe"),
    path("dashboards/validacao-custos/", views.validacao_custos_planilha, name="validacao_custos_planilha"),
    path("dashboards/validacao-custos/exportar-excel/", views.validacao_custos_export_excel, name="validacao_custos_export_excel"),
    path("dashboards/custos-cc/pdf/", views.cc_custos_export_pdf, name="cc_custos_export_pdf"),
    path("dashboards/custos-cc/exportar-excel/", views.custo_cc_export_excel, name="custo_cc_export_excel"),
    path("dashboards/toner/", views.toner_cc_dashboard, name="dashboard_toner"),
    path("dashboards/toner/exportar-excel/", views.toner_cc_export_excel, name="toner_cc_export_excel"),
    path("dashboards/licencas/", views.licencas_dashboard, name="licencas_dashboard"),
    path("dashboards/preventivas/", views.preventiva_dashboard, name="preventiva_dashboard"),
    path("dashboards/preventivas/exportar/", views.preventiva_dashboard_export, name="preventiva_dashboard_export"),
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

    # -- Mapa de Rede (topologia + portas de switch) --------------------------
    path("plantas/rede/",                      views.rede_mapa,            name="rede_mapa"),
    path("plantas/rede/novo/",                 views.rede_mapa_criar,      name="rede_mapa_criar"),
    path("plantas/rede/salvar/",               views.rede_mapa_salvar,     name="rede_mapa_salvar"),
    path("plantas/rede/exportar-excel/",       views.rede_mapa_export,     name="rede_mapa_export"),
    path("plantas/rede/<int:pk>/excluir/",     views.rede_mapa_excluir,    name="rede_mapa_excluir"),
    path("plantas/rede/api/grafo/",            views.rede_grafo_api,       name="rede_grafo_api"),
    path("plantas/rede/api/impacto/",          views.rede_impacto_api,     name="rede_impacto_api"),
    path("plantas/rede/api/historico/",        views.rede_historico_api,   name="rede_historico_api"),
    path("plantas/rede/api/dispositivo/",      views.rede_dispositivo_api, name="rede_dispositivo_api"),
    path("plantas/rede/api/portas/",           views.rede_portas_api,      name="rede_portas_api"),
    path("plantas/rede/api/porta/",            views.rede_porta_api,       name="rede_porta_api"),
    path("plantas/rede/api/enlace/",           views.rede_enlace_api,      name="rede_enlace_api"),
    path("plantas/rede/api/inventario/",       views.rede_inventario_api,  name="rede_inventario_api"),

    # Planta georreferenciada da rede (racks, rotas, metros) — ver rede_planta.py
    path("plantas/rede/planta/",               views.rede_planta,          name="rede_planta"),
    path("plantas/rede/planta/exportar-excel/", views.rede_planta_export,  name="rede_planta_export"),
    path("plantas/rede/rack/<int:pk>/excluir/", views.rede_rack_excluir,   name="rede_rack_excluir"),
    path("plantas/rede/api/planta/",           views.rede_planta_api,      name="rede_planta_api"),
    path("plantas/rede/api/posicao/",          views.rede_posicao_api,     name="rede_posicao_api"),
    path("plantas/rede/api/rota/",             views.rede_rota_api,        name="rede_rota_api"),
    path("plantas/rede/api/impacto-rota/",     views.rede_impacto_rota_api, name="rede_impacto_rota_api"),
    path("plantas/rede/api/rack/",             views.rede_rack_api,        name="rede_rack_api"),
    path("plantas/rede/api/rack-elevacao/",    views.rede_rack_elevacao_api, name="rede_rack_elevacao_api"),

    # ── Avisos & Relatórios ─────────────────────────────────────────────────
    path("avisos/contratos-a-vencer/", views.avisos_contratos_vencer, name="avisos_contratos_vencer"),
    path("avisos/contratos-a-vencer/exportar-excel/", views.avisos_contratos_vencer_export_excel, name="avisos_contratos_vencer_export_excel"),

    # ── Alertas por E-mail ──────────────────────────────────────────────────
    path("alertas/", views.alertas_dashboard, name="alertas_dashboard"),
    path("alertas/enviar/", views.alertas_enviar, name="alertas_enviar"),
    path("alertas/toggle/", views.alertas_toggle, name="alertas_toggle"),
    path("alertas/notificacoes/", views.alertas_notificacoes, name="alertas_notificacoes"),
    path("alertas/notificacoes/<int:pk>/toggle/", views.alertas_notificacao_toggle, name="alertas_notificacao_toggle"),
    path("alertas/notificacoes/<int:pk>/destinatarios/", views.alertas_notificacao_destinatarios, name="alertas_notificacao_destinatarios"),
    path("alertas/notificacoes/<int:pk>/remover-email/", views.alertas_notificacao_remover_email, name="alertas_notificacao_remover_email"),
    path("alertas/notificacoes/<int:pk>/desvincular/<int:perfil_id>/", views.alertas_notificacao_desvincular_perfil, name="alertas_notificacao_desvincular_perfil"),

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
    path("api/quiosque/atualizacao/apk/",     views.kiosk_atualizacao_apk, name="kiosk_atualizacao_apk"),

    # Download do instalador (.apk): rota pública de propósito (protegida por
    # token de validade curta, não por login) — quem baixa é o celular ainda
    # sem o app, escaneando o QR Code gerado em /quiosque/matriculas/.
    path("quiosque/instalador/baixar/<str:token>/", views.kiosk_instalador_download, name="kiosk_instalador_download"),

    # ── Módulo Quiosque — Dashboard interno (TI) ────────────────────────────
    path("quiosque/",                  views.quiosque_dashboard,     name="quiosque_dashboard"),
    path("quiosque/apresentacao/",     views.quiosque_apresentacao,  name="quiosque_apresentacao"),
    path("quiosque/indicadores/",      views.quiosque_indicadores,   name="quiosque_indicadores"),
    path("quiosque/mapa/",             views.quiosque_mapa,          name="quiosque_mapa"),
    path("quiosque/matriculas/",       views.quiosque_matriculas,    name="quiosque_matriculas"),
    path("quiosque/matriculas/<int:pk>/excluir/",   views.quiosque_matricula_excluir,  name="quiosque_matricula_excluir"),
    path("quiosque/matriculas/<int:pk>/renomear/",  views.quiosque_matricula_renomear, name="quiosque_matricula_renomear"),
    path("quiosque/matriculas/<int:pk>/qrcode/",    views.quiosque_matricula_qrcode,   name="quiosque_matricula_qrcode"),
    path("quiosque/apk/upload/",                    views.quiosque_apk_upload,         name="quiosque_apk_upload"),
    path("quiosque/apk/versoes/<str:nome_arquivo>/baixar/", views.quiosque_apk_versao_anterior_baixar, name="quiosque_apk_versao_anterior_baixar"),
    path("quiosque/instalador/gerar/",              views.quiosque_instalador_gerar,   name="quiosque_instalador_gerar"),
    path("quiosque/instalador/<int:pk>/status/",    views.quiosque_instalador_status,  name="quiosque_instalador_status"),
    path("quiosque/instalador/<int:pk>/revogar/",   views.quiosque_instalador_revogar, name="quiosque_instalador_revogar"),
    path("quiosque/<int:pk>/",         views.quiosque_detalhe,       name="quiosque_detalhe"),
    path("quiosque/<int:pk>/mapa/atualizar/",     views.quiosque_mapa_atualizar,     name="quiosque_mapa_atualizar"),
    path("quiosque/<int:pk>/checkins/exportar/",  views.quiosque_checkins_exportar,  name="quiosque_checkins_exportar"),
    path("quiosque/<int:pk>/mapa/exportar/",     views.quiosque_mapa_exportar,      name="quiosque_mapa_exportar"),
    path("quiosque/<int:pk>/sinal/",             views.quiosque_mapa_sinal,         name="quiosque_mapa_sinal"),
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
    path("portal/troca-antecipada/", views.portal_troca_antecipada_list, name="portal_troca_antecipada_list"),
    path("portal/troca-antecipada/nova/", views.portal_troca_antecipada_nova, name="portal_troca_antecipada_nova"),
    path("portal/lotes-envio/", views.portal_lote_envio_list, name="portal_lote_envio_list"),
    path("portal/lotes-envio/<int:pk>/", views.portal_lote_envio_detail, name="portal_lote_envio_detail"),
    path("portal/lotes-envio/item/novo-equipamento/", views.portal_lote_envio_item_equipamento_novo, name="portal_lote_envio_item_equipamento_novo"),
    path("portal/lotes-envio/item/retorno-reparo/", views.portal_lote_envio_item_reparo_concluido, name="portal_lote_envio_item_reparo_concluido"),
    path("portal/lotes-envio/item/<int:pk>/editar/", views.portal_lote_envio_item_editar, name="portal_lote_envio_item_editar"),
    path("portal/lotes-envio/item/<int:pk>/excluir/", views.portal_lote_envio_item_excluir, name="portal_lote_envio_item_excluir"),
    path("portal/lotes-envio/item/<int:pk>/enviar/", views.portal_lote_envio_item_enviar, name="portal_lote_envio_item_enviar"),
    path("portal/separacao/envio/", views.portal_separacao_envio_list, name="portal_separacao_envio_list"),
    path("portal/separacao/devolucao/", views.portal_separacao_devolucao_list, name="portal_separacao_devolucao_list"),
    path("portal/separacao/lote/<int:pk>/", views.portal_separacao_lote_detail, name="portal_separacao_lote_detail"),
    path("portal/notificacoes/marcar-lidas/", views.portal_notificacoes_marcar_lidas, name="portal_notificacoes_marcar_lidas"),
    path("portal/licencas/", views.portal_licencas_list, name="portal_licencas_list"),
    path("portal/ajuda/", views.portal_ajuda, name="portal_ajuda"),
    path("portal/ajuda/diagrama/", views.portal_ajuda_diagrama, name="portal_ajuda_diagrama"),

    # ── Portal de Licenças Office (área isolada p/ parceiros externos, ex.: Routerlink) ──
    path("portal-licencas/", views.portal_licencas_office_list, name="portal_licencas_office_list"),
    path("portal-licencas/colaboradores/", views.portal_licencas_colaboradores_list, name="portal_licencas_colaboradores_list"),
    path("portal-licencas/<int:pk>/editar/", views.portal_licencas_office_editar, name="portal_licencas_office_editar"),
    path("portal-licencas/equipamentos/<int:item_pk>/vincular/", views.portal_licencas_office_vincular, name="portal_licencas_office_vincular"),
    path("portal-licencas/equipamentos/<int:item_pk>/desvincular/", views.portal_licencas_office_desvincular, name="portal_licencas_office_desvincular"),

    # ── Notificações (sino do topo) ─────────────────────────────────────────
    path("notificacoes/marcar-lidas/", views.notificacoes_marcar_lidas, name="notificacoes_marcar_lidas"),

    # ── Manutenção externa — recebimentos (lado TI) ─────────────────────────
    path("manutencao/painel/", views.manutencao_painel, name="manutencao_painel"),
    path("manutencao/recebimentos/", views.manutencao_recebimentos, name="manutencao_recebimentos"),
    path("manutencao/recebimentos/ajuda/", views.manutencao_recebimentos_ajuda, name="manutencao_recebimentos_ajuda"),
    path("manutencao/recebimentos/<int:pk>/", views.manutencao_recebimento_detail, name="manutencao_recebimento_detail"),
    path("manutencao/recebimentos/<int:pk>/concluir/", views.manutencao_recebimento_acao, name="manutencao_recebimento_acao"),
    path("manutencao/envio-fornecedor/", views.envio_fornecedor_list, name="envio_fornecedor_list"),
    path("manutencao/envio-fornecedor/<int:pk>/", views.envio_fornecedor_detail, name="envio_fornecedor_detail"),
    path("manutencao/envio-fornecedor/item/<int:pk>/confirmar/", views.envio_fornecedor_item_confirmar, name="envio_fornecedor_item_confirmar"),
    path("manutencao/envio-fornecedor/<int:pk>/confirmar-lote/", views.envio_fornecedor_confirmar_lote, name="envio_fornecedor_confirmar_lote"),

    # ── Inteligência de Sistema ─────────────────────────────────────────────
    path("inteligencia/", views.sistema_inteligencia_dashboard, name="sistema_inteligencia_dashboard"),
    path("inteligencia/busca-global/", views.sistema_inteligencia_busca_global, name="sistema_inteligencia_busca_global"),
    path("inteligencia/exportar-csv/", views.sistema_inteligencia_export_csv, name="sistema_inteligencia_export_csv"),
    path("noticias/", views.sistema_noticias, name="sistema_noticias"),

    # ── Solicitações de Compra (Kanban) ──────────────────────────────────────
    path("requisicoes/", views.requisicoes_kanban, name="requisicoes_kanban"),
    path("requisicoes/lista/", views.requisicoes_list, name="requisicoes_list"),
    path("requisicoes/itens/", views.requisicao_itens_list, name="requisicao_itens_list"),
    path("requisicoes/dashboard/", views.requisicoes_dashboard, name="requisicoes_dashboard"),
    path("requisicoes/exportar-excel/", views.requisicoes_export_excel, name="requisicoes_export_excel"),
    path("requisicoes/agrupar/", views.requisicao_create_from_itens, name="requisicao_create_from_itens"),
    path("requisicoes/item/novo/", views.requisicao_item_create, name="requisicao_item_create"),
    path("requisicoes/item/<int:pk>/", views.requisicao_item_detail, name="requisicao_item_detail"),
    path("requisicoes/item/<int:pk>/editar/", views.requisicao_item_update, name="requisicao_item_update"),
    path("requisicoes/item/<int:pk>/excluir/", views.requisicao_item_delete, name="requisicao_item_delete"),
    path("requisicoes/item/<int:pk>/mover/", views.requisicao_item_mover, name="requisicao_item_mover"),
    path("requisicoes/item/<int:pk>/acao/", views.requisicao_item_acao, name="requisicao_item_acao"),
    path("requisicoes/item/<int:pk>/receber/", views.requisicao_item_receber_compra, name="requisicao_item_receber_compra"),
    path("requisicoes/item/<int:pk>/comentar/", views.requisicao_item_comentar, name="requisicao_item_comentar"),
    path("requisicoes/comentario/<int:pk>/excluir/", views.requisicao_comentario_excluir, name="requisicao_comentario_excluir"),
    path("requisicoes/<int:pk>/", views.requisicao_detail, name="requisicao_detail"),
    path("requisicoes/<int:pk>/acao/", views.requisicao_acao, name="requisicao_acao"),
    path("requisicoes/<int:pk>/vincular-itens/", views.requisicao_vincular_itens, name="requisicao_vincular_itens"),
    path("requisicoes/catalogo/", views.itens_padrao_list, name="itens_padrao_list"),
    path("requisicoes/catalogo/exportar-excel/", views.itens_padrao_export_excel, name="itens_padrao_export_excel"),
    path("requisicoes/catalogo/novo/", views.itens_padrao_create, name="itens_padrao_create"),
    path("requisicoes/catalogo/importar/", views.itens_padrao_importar, name="itens_padrao_importar"),
    path("requisicoes/catalogo/<int:pk>/editar/", views.itens_padrao_update, name="itens_padrao_update"),
    path("requisicoes/catalogo/<int:pk>/excluir/", views.itens_padrao_delete, name="itens_padrao_delete"),

]
