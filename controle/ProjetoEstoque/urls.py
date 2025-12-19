from django.urls import path
from . import views
from .views import equipamentos_list
from django.contrib.auth import views as auth_views
from django.contrib.auth.views import LogoutView

urlpatterns = [
    path('', equipamentos_list, name="home"),
    #path('equipamento/<int:pk>/', views.equipamento_detalhe, name='equipamento_detalhe'),

    #sobre
    path("sobre/", views.sobre_plataforma, name="sobre_plataforma"),
    path("dashboard/", views.dashboard, name='dashboard'),
    path("custos/centros/", views.cc_custos_dashboard, name="cc_custos_dashboard"),
    path("custos/toner/", views.toner_cc_dashboard, name="dashboard_toner"),
    path("preventivas/dashboard/", views.preventiva_dashboard, name="preventiva_dashboard"),
    path("custo-cc/exportar-excel/", views.custo_cc_export_excel, name="custo_cc_export_excel"),
    path("dashboards/licencas/", views.licencas_dashboard, name="licencas_dashboard"),
    # opcional: alias dedicado para export (usa a mesma view)
    path("dashboards/licencas/exportar/", views.licencas_dashboard, name="licencas_dashboard_export"),
    ### CRUD - Categoria ###

    ### exportações

    path("equipamentos/exportar/", views.equipamentos_exportar, name="equipamentos_exportar"),
    path("custos/toner/exportar/", views.toner_cc_export_excel, name="toner_cc_export_excel"),

    path('categorias/', views.categorias_list, name='categorias_list'),
    path('categorias/novo/', views.categoria_create, name='categoria_create'),
    path('categorias/editar/<int:pk>/', views.categoria_update, name='categoria_update'),
    path('categorias/deletar/<int:pk>/', views.categoria_delete, name='categoria_delete'),

    ### CRUD - Subtipo ###

    path("subtipos/", views.subtipo_list, name="subtipo_list"),
    path("subtipos/novo/", views.subtipo_create, name="subtipo_create"),
    path("subtipos/<int:pk>/editar/", views.subtipo_update, name="subtipo_update"),
    path("subtipos/<int:pk>/excluir/", views.subtipo_delete, name="subtipo_delete"),
    path("subtipos/<int:pk>/", views.subtipo_detail, name="subtipo_detail"),


    ### CRUD - Usuários ###

    path("usuarios/", views.usuario_list, name="usuario_list"),
    path("usuarios/novo/", views.usuario_create, name="usuario_create"),
    path("usuarios/<int:pk>/editar/", views.usuario_update, name="usuario_update"),
    path("usuarios/<int:pk>/", views.usuario_detail, name="usuario_detail"),
    path("usuarios/<int:pk>/excluir/", views.usuario_delete, name="usuario_delete"),
    
    ### CRUD - Fornecedores ###

    path("fornecedores/", views.fornecedor_list, name="fornecedor_list"),
    path("fornecedores/novo/", views.fornecedor_create, name="fornecedor_create"),
    path("fornecedores/<int:pk>/editar/", views.fornecedor_update, name="fornecedor_update"),
    path("fornecedores/<int:pk>/", views.fornecedor_detail, name="fornecedor_detail"),
    path("fornecedores/<int:pk>/excluir/", views.fornecedor_delete, name="fornecedor_delete"),

    ### CRUD - Localidade ###

    path("localidades/", views.localidade_list, name="localidade_list"),
    path("localidades/novo/", views.localidade_create, name="localidade_create"),
    path("localidades/<int:pk>/editar/", views.localidade_update, name="localidade_update"),
    path("localidades/<int:pk>/", views.localidade_detail, name="localidade_detail"),
    path("localidades/<int:pk>/excluir/", views.localidade_delete, name="localidade_delete"),

    ### CRUD - CENTRO DE CUSTO ###

    path("centros-custo/", views.centrocusto_list, name="centrocusto_list"),
    path("centros-custo/novo/", views.centrocusto_create, name="centrocusto_create"),
    path("centros-custo/<int:pk>/editar/", views.centrocusto_update, name="centrocusto_update"),
    path("centros-custo/<int:pk>/", views.centrocusto_detail, name="centrocusto_detail"),
    path("centros-custo/<int:pk>/excluir/", views.centrocusto_delete, name="centrocusto_delete"),

    ### CRUD - FUNÇÃO ###

    path('funcoes/', views.funcao_list, name='funcoes_list'),
    path('funcoes/novo/', views.funcao_form, name='funcao_create'),
    path('funcoes/editar/<int:pk>/', views.funcao_form, name='funcao_edit'),
    path('funcoes/deletar/<int:pk>/', views.funcao_delete, name='funcao_delete'),

    ### CRUD - ITEM ###

    path('equipamentos/cadastrar/', views.item_create, name='cadastrar_equipamento'),
    path("equipamentos/", views.equipamentos_list, name="equipamentos_list"),
    path("equipamentos/<int:pk>/", views.equipamento_detalhe, name="equipamento_detalhe"),
    path("equipamentos/<int:pk>/editar/", views.editar_equipamento, name="editar_equipamento"),
    path("equipamentos/<int:pk>/excluir/", views.equipamento_excluir, name="equipamento_excluir"),

   

    ### LOCADO ###

    path('locacoes/', views.locacoes_list, name='locacoes_list'),
    path('locacoes/novo/', views.locacao_create, name='locacao_create'),
    path('locacoes/editar/<int:pk>/', views.locacao_update, name='locacao_update'),
    path('locacoes/deletar/<int:pk>/', views.locacao_delete, name='locacao_delete'),

    ### LICENCA ###

    # Licenças
    path("licencas/", views.licenca_list, name="licenca_list"),
    path("licencas/nova/", views.licenca_form, name="licenca_create"),
    path("licencas/<int:pk>/editar/", views.licenca_form, name="licenca_update"),
    path("licencas/<int:pk>/", views.licenca_detail, name="licenca_detail"),

    # Movimentações de licença
    path("licencas/mov/", views.mov_licenca_list, name="mov_licenca_list"),
    path("licencas/mov/nova/", views.mov_licenca_form, name="mov_licenca_form"),
    
    # MOV lOTE licenças

    path("licencas/lotes/", views.licenca_lote_list, name="licenca_lote_list"),
    path("licencas/lotes/novo/", views.licenca_lote_form, name="licenca_lote_novo"),
    path("licencas/lotes/<int:pk>/", views.licenca_lote_form, name="licenca_lote_edit"),

    ### COMENTARIO ###

    path('comentarios/', views.comentarios_list, name='comentarios_list'),
    path('comentarios/novo/', views.comentario_create, name='comentario_create'),
    path('comentarios/editar/<int:pk>/', views.comentario_update, name='comentario_update'),
    path('comentarios/deletar/<int:pk>/', views.comentario_delete, name='comentario_delete'),

    ### MOVIMENTAÇÃo ###

    path("movimentacoes/", views.movimentacao_list, name="movimentacao_list"),
    path("movimentacoes/nova/", views.movimentacao_create, name="movimentacao_create"),
    path("movimentacoes/<int:pk>/", views.movimentacao_detail, name="movimentacao_detail"),

    path('movimentacoes/editar/<int:pk>/', views.movimentacao_update, name='movimentacao_update'),
    path('movimentacoes/deletar/<int:pk>/', views.movimentacao_delete, name='movimentacao_delete'),

    ### MOVIMENTAÇÃO ITEM ###

    path('ciclos/', views.ciclos_list, name='ciclos_list'),
    path('ciclos/novo/', views.ciclo_create, name='ciclo_create'),
    path('ciclos/editar/<int:pk>/', views.ciclo_update, name='ciclo_update'),
    path('ciclos/deletar/<int:pk>/', views.ciclo_delete, name='ciclo_delete'),

    #### Preventivas #######
        # Checklists
    path("preventiva/checklists/", views.checklist_list, name="checklist_list"),
    path("preventiva/checklists/novo/", views.checklist_form, name="checklist_create"),
    path("preventiva/checklists/<int:pk>/editar/", views.checklist_form, name="checklist_form"),
    path("preventiva/checklists/<int:pk>/excluir/", views.checklist_delete, name="checklist_delete"),

    path("preventiva/checklists/<int:checklist_pk>/pergunta/novo/", views.pergunta_form, name="pergunta_create"),
    path("preventiva/checklists/<int:checklist_pk>/pergunta/<int:pk>/editar/", views.pergunta_form, name="pergunta_form"),
    path("preventiva/checklists/<int:checklist_pk>/pergunta/<int:pk>/excluir/", views.pergunta_delete, name="pergunta_delete"),

    # Preventiva
    path("preventiva/", views.preventiva_list, name="preventiva_list"),
    path("preventiva/iniciar/", views.preventiva_start, name="preventiva_start"),
    path("preventiva/item/<int:item_id>/iniciar/", views.preventiva_start, name="preventiva_start_item"),
    path("preventiva/<int:pk>/executar/", views.preventiva_exec, name="preventiva_exec"),
    path("preventiva/<int:pk>/", views.preventiva_detail, name="preventiva_detail"),
    
    ## Cadastros ##
    #path('cadastrar-categoria/', views.cadastrar_categoria, name='cadastrar_categoria'),
    #path('cadastrar-subtipo/', views.cadastrar_subtipo, name='cadastrar_subtipo'),
    #path('cadastrar-equipamento/', views.cadastrar_equipamento, name='cadastrar_equipamento'),

    # Crud Equipamentos  
    #path('editar-equipamento/<int:pk>/', views.editar_equipamento, name='editar_equipamento'),
    #path('excluir-equipamento/<int:pk>/', views.excluir_equipamento, name='excluir_equipamento'),
    #path('exportar-equipamentos/', views.exportar_equipamentos_excel, name='exportar_equipamentos'),
    #path('equipamento-local/', views.equipamentos_por_local, name='equipamento_por_local'),
    #path('exportar-por-local/', views.exportar_por_local, name='exportar_por_local'),

    # Custo com equipamentos
    #path('equipamento/<int:equipamento_id>/historico/', views.historico_manutencao, name='historico_manutencao'),
    #path('equipamento/<int:equipamento_id>/exportar_excel/', views.exportar_historico_excel, name='exportar_historico_excel'),
    #path('custos-por-area/', views.mapa_custos_por_area, name='mapa_custos_area'),
    #path('exportar-custos-por-area/', views.exportar_custos_por_area_excel, name='exportar_custos_area'),


    ## Preventivas 
    #path('equipamento/<int:equipamento_id>/preventivas/nova/', views.cadastrar_preventiva, name='cadastrar_preventiva'),
    #path('equipamento/<int:equipamento_id>/preventivas/', views.visualizar_preventivas, name='visualizar_preventivas'),
    #path('preventivas/', views.todas_preventivas, name='todas_preventivas'),
    #path('preventiva/<int:pk>/', views.preventiva_detalhe, name='preventiva_detalhe'),
    #path('exportar_preventivas/', views.exportar_preventivas_excel, name='exportar_preventivas'),

    
    ## Login
    path('login/', auth_views.LoginView.as_view(template_name='login.html'), name='login'),
    
]

