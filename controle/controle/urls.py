
from django.contrib import admin
from django.urls import path, include, re_path
from django.contrib.auth import views as auth_views
from django.conf import settings
from django.views.static import serve as serve_media

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('ProjetoEstoque.urls')),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
]

# Uploads (fotos de itens, termos, arquivos do portal): servidos pelo Django
# tambem com DEBUG=False (app interno de baixo trafego). Os arquivos estaticos
# ficam a cargo do WhiteNoise; o media muda em runtime e nao pode ir pra ele.
urlpatterns += [
    re_path(r'^media/(?P<path>.*)$', serve_media, {'document_root': settings.MEDIA_ROOT}),
]