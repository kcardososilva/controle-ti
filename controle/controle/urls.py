from django.contrib import admin
from django.urls import path, include, re_path
from django.contrib.auth import views as auth_views

from ProjetoEstoque.views import media_protegida

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('ProjetoEstoque.urls')),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
]

# Uploads (fotos de preventiva, termos assinados, notas fiscais do portal):
# servidos pelo Django tambem com DEBUG=False (app interno de baixo trafego).
# Os arquivos estaticos ficam a cargo do WhiteNoise; o media muda em runtime e
# nao pode ir pra ele.
#
# A entrega passa OBRIGATORIAMENTE por `media_protegida` (@login_required +
# regra de dono em services/media_acesso_service.py). Antes era o
# `django.views.static.serve` cru: qualquer um que alcancasse a porta do
# servidor baixava termo assinado e nota fiscal adivinhando a URL. Nunca
# voltar a apontar este padrao direto para o `serve`.
urlpatterns += [
    re_path(r'^media/(?P<path>.*)$', media_protegida, name='media_protegida'),
]
