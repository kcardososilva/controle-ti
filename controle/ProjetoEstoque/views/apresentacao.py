"""
Apresentação do Zelo — a peça de vitrine, alcançada a partir da tela de acesso.

É a única tela do sistema que é PÚBLICA de propósito: quem chega nela ainda não
tem sessão (é o link "Conheça o Zelo" do login). Por isso ela não consulta o
banco. A versão logada disso é `/sobre/` (`sobre_plataforma`), que mostra os
números REAIS do acervo — e justamente por mostrar número real continua atrás
de `@login_required`. Contagem de equipamento, de colaborador e de licença diz
o tamanho da operação da empresa, e isso não vai para uma página aberta.

O conteúdo daqui é institucional (o que o sistema faz), não operacional (o que
existe dentro dele). Se um dia alguém quiser número nesta tela, o caminho certo
é um valor aprovado e fixo no contexto, nunca um `.count()`.
"""
from django.shortcuts import render

from .admin_perfil import SISTEMA_VERSAO


def apresentacao(request):
    """Filme de 30s + página de apresentação da plataforma. Sem login."""
    return render(request, "front/apresentacao.html", {"versao": SISTEMA_VERSAO})
