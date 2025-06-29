from django.urls import path
from . import views
from .views import home
from django.contrib.auth import views as auth_views
from django.contrib.auth.views import LogoutView

urlpatterns = [
    path('', home, name="home"),
    path('equipamento/<int:pk>/', views.equipamento_detalhe, name='equipamento_detalhe'),

    ## Cadastros ##
    path('cadastrar-categoria/', views.cadastrar_categoria, name='cadastrar_categoria'),
    path('cadastrar-subtipo/', views.cadastrar_subtipo, name='cadastrar_subtipo'),
    path('cadastrar-equipamento/', views.cadastrar_equipamento, name='cadastrar_equipamento'),

    # Crud Equipamentos  
    path('editar-equipamento/<int:pk>/', views.editar_equipamento, name='editar_equipamento'),
    path('excluir-equipamento/<int:pk>/', views.excluir_equipamento, name='excluir_equipamento'),
    path('exportar-equipamentos/', views.exportar_equipamentos_excel, name='exportar_equipamentos'),
    path('equipamento-local/', views.equipamentos_por_local, name='equipamento_por_local'),
    path('exportar-por-local/', views.exportar_por_local, name='exportar_por_local'),
  

    ## Preventivas 
    path('equipamento/<int:equipamento_id>/preventivas/nova/', views.cadastrar_preventiva, name='cadastrar_preventiva'),
    path('equipamento/<int:equipamento_id>/preventivas/', views.visualizar_preventivas, name='visualizar_preventivas'),
    path('preventivas/', views.todas_preventivas, name='todas_preventivas'),
    path('preventiva/<int:pk>/', views.preventiva_detalhe, name='preventiva_detalhe'),
    path('exportar_preventivas/', views.exportar_preventivas_excel, name='exportar_preventivas'),

    
    ## Login
    path('login/', auth_views.LoginView.as_view(template_name='login.html'), name='login'),
    
]
