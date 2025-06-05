from django.urls import path
from . import views
from .views import home
from django.contrib.auth import views as auth_views

urlpatterns = [
    path('', home, name="home"),
    path('equipamento/<int:pk>/', views.equipamento_detalhe, name='equipamento_detalhe'),
    path('cadastrar-categoria/', views.cadastrar_categoria, name='cadastrar_categoria'),
    path('cadastrar-subtipo/', views.cadastrar_subtipo, name='cadastrar_subtipo'),
    path('cadastrar-equipamento/', views.cadastrar_equipamento, name='cadastrar_equipamento'),
    path('editar-equipamento/<int:pk>/', views.editar_equipamento, name='editar_equipamento'),
    path('excluir-equipamento/<int:pk>/', views.excluir_equipamento, name='excluir_equipamento'),
    path('exportar-equipamentos/', views.exportar_equipamentos_excel, name='exportar_equipamentos'),

    path('login/', auth_views.LoginView.as_view(template_name='login.html'), name='login'),
    
]
