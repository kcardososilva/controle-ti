from django.urls import path, re_path
from django.contrib.auth import views as auth_views
from . import views

urlpatterns = [

    # ── Home & Autenticação ──────────────────────────────────────────────────
    # `/` abre o ambiente que a pessoa usou por último (Zelo TI ou Zelo Agro);
    # `/dashboard/` é sempre o painel do TI. Ver views/ambiente.py.
    path("", views.inicio, name="home"),
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
    path("subtipos/novo-rapido/", views.subtipo_cadastro_rapido, name="subtipo_cadastro_rapido"),
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
    path("localidades/novo-rapido/", views.localidade_cadastro_rapido, name="localidade_cadastro_rapido"),
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
    path("equipamentos/<int:pk>/reservar/", views.equipamento_reservar, name="equipamento_reservar"),
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
    path("movimentacoes/api/item-reserva/", views.api_item_reserva, name="api_item_reserva"),
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

    # ── Chip no equipamento de TI (tablet, celular) — ambiente TI, permissão de Frotas e Conectividade (regra 62) ──
    path("equipamentos/chip/opcoes/", views.equipamento_chip_opcoes, name="equipamento_chip_opcoes"),
    # Chips do Agro × estoque de TI (regra 66): conciliação e as ações de ligar, desligar e cadastrar o patrimônio do chip.
    path("equipamentos/chips/conciliacao/", views.chips_conciliacao, name="chips_conciliacao"),
    path("equipamentos/chips/conciliar/", views.chip_estoque_conciliar, name="chip_estoque_conciliar"),
    path("equipamentos/chips/ligar/", views.chip_estoque_ligar, name="chip_estoque_ligar"),
    path("equipamentos/chips/desligar/", views.chip_estoque_desligar, name="chip_estoque_desligar"),
    path("equipamentos/chips/criar-chip/", views.chip_estoque_criar_chip, name="chip_estoque_criar_chip"),
    path("equipamentos/chips/cadastrar-itens/", views.chip_estoque_cadastrar_itens, name="chip_estoque_cadastrar_itens"),
    path("equipamentos/chips/opcoes/", views.chip_estoque_opcoes, name="chip_estoque_opcoes"),
    path("equipamentos/chips/opcoes-itens/", views.chip_estoque_opcoes_itens, name="chip_estoque_opcoes_itens"),
    path("equipamentos/<int:pk>/chip/vincular/", views.equipamento_chip_vincular, name="equipamento_chip_vincular"),
    path("equipamentos/<int:pk>/chip/<int:vinculo_id>/desvincular/", views.equipamento_chip_desvincular, name="equipamento_chip_desvincular"),
    path("equipamentos/<int:pk>/chip/<int:vinculo_id>/substituir/", views.equipamento_chip_substituir, name="equipamento_chip_substituir"),

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

    # ── NinjaOne RMM (importação de CSV + API via OAuth) ───────────────────
    path("ninja/",                    views.ninja_dashboard,     name="ninja_dashboard"),
    path("ninja/dispositivos/",       views.ninja_dispositivos,  name="ninja_dispositivos"),
    path("ninja/nao-cadastrados/",    views.ninja_nao_cadastrados, name="ninja_nao_cadastrados"),
    path("ninja/login-validacao/",    views.ninja_login_validacao, name="ninja_login_validacao"),
    path("ninja/login-validacao/revalidar/", views.ninja_login_revalidar, name="ninja_login_revalidar"),
    path("ninja/login/<int:pk>/",     views.ninja_login_detalhe, name="ninja_login_detalhe"),
    path("ninja/relatorio/",          views.ninja_relatorio,     name="ninja_relatorio"),
    path("ninja/importar/",           views.ninja_importar,      name="ninja_importar"),
    path("ninja/oauth/conectar/",     views.ninja_oauth_conectar, name="ninja_oauth_conectar"),
    path("ninja/oauth/callback/",     views.ninja_oauth_callback, name="ninja_oauth_callback"),
    path("ninja/sincronizar/",        views.ninja_sincronizar,   name="ninja_sincronizar"),
    path("ninja/desconectar/",        views.ninja_desconectar,   name="ninja_desconectar"),
    path("ninja/licencas-a-liberar/", views.ninja_licencas_liberar, name="ninja_licencas_liberar"),
    path("ninja/licencas-a-liberar/marcar/", views.ninja_licencas_marcar, name="ninja_licencas_marcar"),
    path("ninja/licencas-a-liberar/enviar/", views.ninja_licencas_enviar, name="ninja_licencas_enviar"),

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
    path("quiosque/sobre/",            views.quiosque_sobre,         name="quiosque_sobre"),
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

    # ── Contratações (admissão / onboarding de TI) ───────────────────
    path("contratacoes/", views.contratacao_painel, name="contratacao_painel"),
    path("contratacoes/nova/", views.contratacao_create, name="contratacao_create"),
    path("contratacoes/sistemas/", views.sistema_corporativo_list, name="sistema_corporativo_list"),
    path("contratacoes/sistemas/novo/", views.sistema_corporativo_create, name="sistema_corporativo_create"),
    path("contratacoes/sistemas/<int:pk>/editar/", views.sistema_corporativo_update, name="sistema_corporativo_update"),
    path("contratacoes/sistemas/<int:pk>/excluir/", views.sistema_corporativo_delete, name="sistema_corporativo_delete"),
    path("contratacoes/<int:pk>/", views.contratacao_detail, name="contratacao_detail"),
    path("contratacoes/<int:pk>/editar/", views.contratacao_update, name="contratacao_update"),
    path("contratacoes/<int:pk>/excluir/", views.contratacao_delete, name="contratacao_delete"),
    path("contratacoes/<int:pk>/etapa/", views.contratacao_etapa, name="contratacao_etapa"),
    path("contratacoes/<int:pk>/sistema/<int:linha_pk>/", views.contratacao_sistema_toggle, name="contratacao_sistema_toggle"),
    path("contratacoes/<int:pk>/licenca/<int:linha_pk>/", views.contratacao_licenca_toggle, name="contratacao_licenca_toggle"),
    path("contratacoes/chamados/", views.chamados_lista, name="chamados_lista"),
    path("contratacoes/chamados/sincronizacao/", views.chamados_sincronizacao, name="chamados_sincronizacao"),
    path("contratacoes/chamados/sincronizacao/acao/", views.chamados_sincronizacao_acao, name="chamados_sincronizacao_acao"),
    path("contratacoes/<int:pk>/preencher/", views.contratacao_preencher, name="contratacao_preencher"),
    path("contratacoes/<int:pk>/colaborador/", views.contratacao_colaborador_corrigir, name="contratacao_colaborador_corrigir"),
    path("contratacoes/<int:pk>/reabrir-vaga/", views.contratacao_vaga_reabrir, name="contratacao_vaga_reabrir"),
    path("contratacoes/<int:pk>/cadastrar-colaborador/", views.contratacao_colaborador_cadastrar, name="contratacao_colaborador_cadastrar"),
    path("contratacoes/<int:pk>/integracao/", views.contratacao_integracao, name="contratacao_integracao"),
    path("contratacoes/<int:pk>/concluir/", views.contratacao_concluir, name="contratacao_concluir"),
    path("contratacoes/<int:pk>/etapas/", views.contratacao_etapa_processo, name="contratacao_etapa_processo"),
    path("contratacoes/<int:pk>/reservar/", views.contratacao_reservar, name="contratacao_reservar"),
    path("contratacoes/<int:pk>/reservas/<int:reserva_pk>/liberar/", views.contratacao_reserva_liberar, name="contratacao_reserva_liberar"),
    path("contratacoes/<int:pk>/reservas/<int:reserva_pk>/alerta/", views.contratacao_reserva_alerta, name="contratacao_reserva_alerta"),
    path("contratacoes/<int:pk>/chamados/", views.contratacao_chamados_solicitar, name="contratacao_chamados_solicitar"),
    path("contratacoes/<int:pk>/chamados/<int:chamado_pk>/", views.chamado_detail, name="chamado_detail"),
    path("contratacoes/<int:pk>/chamados/<int:chamado_pk>/responder/", views.chamado_responder, name="chamado_responder"),
    path("contratacoes/<int:pk>/chamados/<int:chamado_pk>/atendimento/", views.chamado_atendimento, name="chamado_atendimento"),
    path("contratacoes/<int:pk>/chamados/<int:chamado_pk>/mensagens/<int:mensagem_pk>/reenviar/", views.chamado_mensagem_reenviar, name="chamado_mensagem_reenviar"),
    path("contratacoes/<int:pk>/chamados/<int:chamado_pk>/anexos/<int:anexo_pk>/", views.chamado_anexo_download, name="chamado_anexo_download"),
    path("contratacoes/<int:pk>/chamados/<int:chamado_pk>/reenviar/", views.contratacao_chamado_reenviar, name="contratacao_chamado_reenviar"),
    path("contratacoes/<int:pk>/chamados/<int:chamado_pk>/identificador/", views.contratacao_chamado_identificador, name="contratacao_chamado_identificador"),

    # ─── Acessos restritos (perfis de uma tela só) ────────────────────────────
    # Fora de /contratacoes/ e de /quiosque/ de proposito: e tela de TI, e o
    # AcessoRestritoMiddleware barra qualquer perfil restrito aqui.
    path("acessos/restritos/", views.acesso_restrito_gerenciar, name="acesso_restrito_gerenciar"),
    path("acessos/restritos/conceder/", views.acesso_restrito_conceder, name="acesso_restrito_conceder"),
    path("acessos/restritos/<int:pk>/revogar/", views.acesso_restrito_revogar, name="acesso_restrito_revogar"),

    # ─── ZELO AGRO ────────────────────────────────────────────────────────────
    # Tudo do Agro mora sob /agro/: é o caminho que define o ambiente
    # (services/ambiente_service.py + AmbienteMiddleware). Frotas e Conectividade
    # saíram de /frotas/ e /m2m/ em 2026-11-13 com os MESMOS nomes de rota — nenhum
    # {% url %}, JS ou teste por nome mudou. Os endereços antigos respondem 308
    # (rota_legada_frota + urls_legado_frota.py), no fim deste bloco.
    path("agro/", views.agro_visao_geral, name="agro_visao_geral"),

    # ─── Agro › Frotas (veículos e equipamentos) ──────────────────────────────
    # ver_frota nas telas de veículo, editar_frota no cadastro e no status (403 sem elas,
    # views/frota.py e views/frota_api.py). Os tipos de veículo (administrar_frota_config)
    # são cadastro da frota, por isso ficam aqui e não na configuração da TIM.
    path("agro/frotas/", views.frota_visao_geral, name="frota_visao_geral"),
    path("agro/frotas/proprios/", views.frota_equipamentos_proprios, name="frota_equipamentos_proprios"),
    path("agro/frotas/terceiros/", views.frota_equipamentos_terceiros, name="frota_equipamentos_terceiros"),
    path("agro/frotas/validar/", views.frota_equipamentos_validar_lote, name="frota_equipamentos_validar_lote"),
    path("agro/frotas/movimentacoes/", views.frota_equip_movimentacoes, name="frota_equip_movimentacoes"),
    path("agro/frotas/historico/", views.frota_equip_historico, name="frota_equip_historico"),
    path("agro/frotas/composicao/", views.frota_composicao, name="frota_composicao"),
    path("agro/frotas/relatorios/", views.frota_relatorios, name="frota_relatorios"),
    path("agro/frotas/veiculos/", views.frota_veiculos, name="frota_veiculos"),
    path("agro/frotas/veiculos/novo/", views.frota_veiculo_novo, name="frota_veiculo_novo"),
    path("agro/frotas/veiculos/<int:pk>/", views.frota_veiculo_detalhe, name="frota_veiculo_detalhe"),
    path("agro/frotas/veiculos/<int:pk>/editar/", views.frota_veiculo_editar, name="frota_veiculo_editar"),
    path("agro/frotas/veiculos/<int:pk>/status/", views.frota_veiculo_status, name="frota_veiculo_status"),
    path("agro/frotas/veiculos/<int:pk>/exclusao/", views.frota_veiculo_exclusao, name="frota_veiculo_exclusao"),
    path("agro/frotas/veiculos/<int:pk>/validar/", views.frota_veiculo_validar, name="frota_veiculo_validar"),
    path("agro/frotas/veiculos/<int:pk>/movimentar/", views.frota_veiculo_movimentar, name="frota_veiculo_movimentar"),
    path("agro/frotas/opcoes/", views.frota_opcoes, name="frota_opcoes"),
    # Importação da planilha de equipamentos motorizados (importar_equipamentos_frota), em seis
    # passos: envio -> mapeamento -> prévia -> validação -> confirmação -> resultado. Os passos 2 a
    # 6 são UMA tela (`?etapa=`) sobre a mesma importação; só a confirmação grava na frota.
    path("agro/frotas/importar/", views.frota_equip_importar, name="frota_equip_importar"),
    path("agro/frotas/importar/modelo/", views.frota_equip_importar_modelo, name="frota_equip_importar_modelo"),
    path("agro/frotas/importar/historico/", views.frota_equip_importacoes, name="frota_equip_importacoes"),
    path("agro/frotas/importar/<int:pk>/", views.frota_equip_importacao, name="frota_equip_importacao"),
    path("agro/frotas/importar/<int:pk>/mapear/", views.frota_equip_importacao_mapear, name="frota_equip_importacao_mapear"),
    path("agro/frotas/importar/<int:pk>/confirmar/", views.frota_equip_importacao_confirmar, name="frota_equip_importacao_confirmar"),
    path("agro/frotas/importar/<int:pk>/cancelar/", views.frota_equip_importacao_cancelar, name="frota_equip_importacao_cancelar"),
    path("agro/frotas/importar/<int:pk>/relatorio/", views.frota_equip_importacao_relatorio, name="frota_equip_importacao_relatorio"),
    path("agro/frotas/categorias/", views.frota_categorias, name="frota_categorias"),
    path("agro/frotas/categorias/salvar/", views.frota_cadastro_salvar, name="frota_cadastro_salvar"),
    path("agro/frotas/configuracao/tipos/", views.frota_config_tipos, name="frota_config_tipos"),
    path("agro/frotas/configuracao/tipos/salvar/", views.frota_config_tipo_salvar, name="frota_config_tipo_salvar"),
    path("agro/frotas/tipos/rapido/", views.frota_tipo_rapido, name="frota_tipo_rapido"),

    # ─── Agro › Chips e Conectividade (TIM M2M) ───────────────────────────────
    # ver_chips nas telas de chip; consultar_conectividade em sessões, consumo, no JSON da
    # resposta guardada e no Atualizar; importar_chips no cadastro e na importação;
    # vincular_chips em vincular/ e transferir_chips nas demais ações sobre o vínculo (que
    # levam o id do vínculo que a tela mostrava: o service recusa se ele mudou);
    # ver_historico_frota em movimentações e alertas; tratar_ocorrencias no tratamento;
    # administrar_frota_config em tudo de configuracao/ (ver views/m2m.py e views/frota*.py).
    path("agro/conectividade/", views.m2m_visao_geral, name="m2m_visao_geral"),
    path("agro/conectividade/chips/", views.m2m_chips, name="m2m_chips"),
    path("agro/conectividade/chips/novo/", views.m2m_chip_novo, name="m2m_chip_novo"),
    path("agro/conectividade/chips/<int:pk>/", views.m2m_chip_detalhe, name="m2m_chip_detalhe"),
    path("agro/conectividade/chips/<int:pk>/editar/", views.m2m_chip_editar, name="m2m_chip_editar"),
    path("agro/conectividade/chips/<int:pk>/monitoramento/", views.m2m_chip_monitoramento, name="m2m_chip_monitoramento"),
    path("agro/conectividade/chips/<int:pk>/atualizar/", views.m2m_chip_atualizar, name="m2m_chip_atualizar"),
    path("agro/conectividade/chips/<int:pk>/retornar/", views.frota_retornar, name="frota_retornar"),
    path("agro/conectividade/chips/<int:pk>/inativar/", views.frota_inativar, name="frota_inativar"),
    path("agro/conectividade/consultas/<int:pk>/", views.m2m_consulta_payload, name="m2m_consulta_payload"),
    path("agro/conectividade/opcoes/", views.m2m_opcoes, name="m2m_opcoes"),
    path("agro/conectividade/sincronizacao/", views.m2m_sincronizacao, name="m2m_sincronizacao"),
    path("agro/conectividade/sessoes/", views.m2m_sessoes, name="m2m_sessoes"),
    path("agro/conectividade/consumo/", views.m2m_consumo, name="m2m_consumo"),
    path("agro/conectividade/movimentacoes/", views.frota_movimentacoes, name="frota_movimentacoes"),
    path("agro/conectividade/vincular/", views.frota_vincular, name="frota_vincular"),
    path("agro/conectividade/vinculos/<int:vinculo_id>/desvincular/", views.frota_desvincular, name="frota_desvincular"),
    path("agro/conectividade/vinculos/<int:vinculo_id>/transferir/", views.frota_transferir, name="frota_transferir"),
    path("agro/conectividade/vinculos/<int:vinculo_id>/substituir/", views.frota_substituir, name="frota_substituir"),
    path("agro/conectividade/alertas/", views.m2m_alertas, name="m2m_alertas"),
    path("agro/conectividade/alertas/<int:pk>/tratar/", views.m2m_alerta_tratar, name="m2m_alerta_tratar"),
    # Importação de chips por planilha (importar_chips): modelo -> envio -> prévia -> resultado.
    path("agro/conectividade/importar/", views.frota_importar, name="frota_importar"),
    path("agro/conectividade/importar/modelo/", views.frota_modelo_planilha, name="frota_modelo_planilha"),
    path("agro/conectividade/importar/<int:pk>/", views.frota_importacao, name="frota_importacao"),
    path("agro/conectividade/importar/<int:pk>/confirmar/", views.frota_importacao_confirmar, name="frota_importacao_confirmar"),
    path("agro/conectividade/importar/<int:pk>/cancelar/", views.frota_importacao_cancelar, name="frota_importacao_cancelar"),
    path("agro/conectividade/importar/<int:pk>/relatorio/", views.frota_importacao_relatorio, name="frota_importacao_relatorio"),
    path("agro/conectividade/importacoes/", views.frota_importacoes, name="frota_importacoes"),
    # Configuração (administrar_frota_config): integração TIM, operadoras e regras de alerta.
    path("agro/conectividade/configuracao/", views.m2m_config, name="m2m_config"),
    path("agro/conectividade/configuracao/token/", views.m2m_config_token, name="m2m_config_token"),
    path("agro/conectividade/configuracao/frequencia/", views.m2m_config_frequencia, name="m2m_config_frequencia"),
    path("agro/conectividade/configuracao/mapeamento/", views.m2m_config_mapeamento, name="m2m_config_mapeamento"),
    path("agro/conectividade/configuracao/previa/", views.m2m_config_previa, name="m2m_config_previa"),
    path("agro/conectividade/configuracao/testar/", views.m2m_config_testar, name="m2m_config_testar"),
    path("agro/conectividade/configuracao/reinterpretar/", views.m2m_reinterpretar, name="m2m_reinterpretar"),
    path("agro/conectividade/configuracao/operadoras/", views.frota_config_operadoras, name="frota_config_operadoras"),
    path("agro/conectividade/configuracao/operadoras/salvar/", views.frota_config_operadora_salvar, name="frota_config_operadora_salvar"),
    path("agro/conectividade/configuracao/regras/", views.frota_config_regras, name="frota_config_regras"),
    path("agro/conectividade/configuracao/regras/<str:tipo>/salvar/", views.frota_config_regra_salvar, name="frota_config_regra_salvar"),
    path("agro/conectividade/configuracao/regras/<str:tipo>/restaurar/", views.frota_config_regra_restaurar, name="frota_config_regra_restaurar"),

    # Endereços ANTIGOS do módulo (/frotas/… e /m2m/…, até 2026-11-13): 308 para o novo,
    # traduzidos pelo NOME da rota (ProjetoEstoque/urls_legado_frota.py). Favorito, link de
    # e-mail antigo e aba aberta (inclusive POST) continuam chegando ao mesmo registro.
    # ── Tecnologia agrícola: estoque tecnológico (2026-11-13) ──
    path("agro/estoque/", views.tec_visao_geral, name="tec_visao_geral"),
    path("agro/estoque/equipamentos/", views.tec_equipamentos, name="tec_equipamentos"),
    path("agro/estoque/equipamentos/novo/", views.tec_equipamento_novo, name="tec_equipamento_novo"),
    path("agro/estoque/equipamentos/<int:pk>/", views.tec_equipamento_detalhe, name="tec_equipamento_detalhe"),
    path("agro/estoque/equipamentos/<int:pk>/editar/", views.tec_equipamento_editar, name="tec_equipamento_editar"),
    path("agro/estoque/equipamentos/<int:pk>/mover/", views.tec_equipamento_mover, name="tec_equipamento_mover"),
    path("agro/estoque/equipamentos/<int:pk>/inativar/", views.tec_equipamento_inativar, name="tec_equipamento_inativar"),
    path("agro/estoque/equipamentos/<int:pk>/instalar/", views.tec_equipamento_instalar, name="tec_equipamento_instalar"),
    path("agro/estoque/maquinas/<int:pk>/instalar/", views.tec_veiculo_instalar, name="tec_veiculo_instalar"),
    path("agro/estoque/instalacoes/<int:pk>/retirar/", views.tec_instalacao_retirar, name="tec_instalacao_retirar"),
    path("agro/estoque/instalacoes/<int:pk>/transferir/", views.tec_instalacao_transferir, name="tec_instalacao_transferir"),
    path("agro/estoque/instalacoes/<int:pk>/substituir/", views.tec_instalacao_substituir, name="tec_instalacao_substituir"),
    path("agro/estoque/materiais/", views.tec_materiais, name="tec_materiais"),
    path("agro/estoque/materiais/entrada/", views.tec_material_entrada, name="tec_material_entrada_nova"),
    path("agro/estoque/materiais/<int:pk>/", views.tec_material_detalhe, name="tec_material_detalhe"),
    path("agro/estoque/materiais/<int:pk>/entrada/", views.tec_material_entrada, name="tec_material_entrada"),
    path("agro/estoque/materiais/<int:pk>/saida/", views.tec_material_saida, name="tec_material_saida"),
    path("agro/estoque/materiais/<int:pk>/ajuste/", views.tec_material_ajuste, name="tec_material_ajuste"),
    path("agro/estoque/materiais/<int:pk>/transferir/", views.tec_material_transferir, name="tec_material_transferir"),
    path("agro/estoque/materiais/aplicar/", views.tec_material_aplicar, name="tec_material_aplicar_novo"),
    path("agro/estoque/materiais/<int:pk>/aplicar/", views.tec_material_aplicar, name="tec_material_aplicar"),
    path("agro/estoque/materiais/<int:pk>/devolver/", views.tec_material_devolver, name="tec_material_devolver"),
    path("agro/estoque/movimentacoes/", views.tec_movimentacoes, name="tec_movimentacoes"),
    path("agro/estoque/catalogo/", views.tec_catalogo, {"tipo": "modelos"}, name="tec_catalogo_inicio"),
    path("agro/estoque/catalogo/<slug:tipo>/", views.tec_catalogo, name="tec_catalogo"),
    path("agro/estoque/catalogo/<slug:tipo>/novo/", views.tec_catalogo_novo, name="tec_catalogo_novo"),
    path("agro/estoque/catalogo/<slug:tipo>/<int:pk>/editar/", views.tec_catalogo_editar, name="tec_catalogo_editar"),
    # Infraestrutura georreferenciada e o mapa (2026-11-14). Rota nova nasce em /agro/, nunca na tabela legada.
    path("agro/mapa/", views.agro_mapa, name="agro_mapa"),
    path("agro/mapa/dados/", views.agro_mapa_dados, name="agro_mapa_dados"),
    path("agro/mapa/configuracao/", views.agro_mapa_config, name="agro_mapa_config"),
    path("agro/infraestrutura/", views.infra_visao_geral, name="infra_visao_geral"),
    path("agro/infraestrutura/estruturas/", views.infra_estruturas, name="infra_estruturas"),
    path("agro/infraestrutura/estruturas/nova/", views.infra_estrutura_nova, name="infra_estrutura_nova"),
    path("agro/infraestrutura/estruturas/<int:pk>/editar/", views.infra_estrutura_editar, name="infra_estrutura_editar"),
    path("agro/infraestrutura/enlaces/", views.infra_enlaces, name="infra_enlaces"),
    path("agro/infraestrutura/enlaces/novo/", views.infra_enlace_novo, name="infra_enlace_novo"),
    path("agro/infraestrutura/enlaces/<int:pk>/editar/", views.infra_enlace_editar, name="infra_enlace_editar"),
    re_path(r"^(?P<antigo>(?:frotas|m2m)/.*)$", views.rota_legada_frota, name="rota_legada_frota"),

]
