"""
Tela de Administrador do Sistema — perfil do usuário logado.

Acessível pelo nome no topbar (profile-chip). Permite ao administrador:
  • ver/editar os próprios dados (nome, e-mail);
  • trocar a senha (com validação do Django, mantendo a sessão ativa);
  • consultar informações do sistema (versão e contagens gerais).
"""
import django
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.shortcuts import render, redirect
from django.utils import timezone

from ..models import (
    Item,
    Usuario,
    Licenca,
    CentroCusto,
    Fornecedor,
    OrdemManutencao,
    MovimentacaoItem,
    StatusOrdemManutencaoChoices,
)

SISTEMA_VERSAO = "4.0.0"


@login_required
def admin_perfil(request):
    user = request.user

    if request.method == "POST":
        acao = request.POST.get("acao", "")

        if acao == "perfil":
            user.first_name = request.POST.get("first_name", "").strip()
            user.last_name = request.POST.get("last_name", "").strip()
            user.email = request.POST.get("email", "").strip()
            user.save(update_fields=["first_name", "last_name", "email"])
            messages.success(request, "Dados do administrador atualizados.")

        elif acao == "senha":
            atual = request.POST.get("senha_atual", "")
            nova = request.POST.get("nova_senha", "")
            conf = request.POST.get("conf_senha", "")
            if not user.check_password(atual):
                messages.error(request, "A senha atual está incorreta.")
            elif not nova:
                messages.error(request, "Informe a nova senha.")
            elif nova != conf:
                messages.error(request, "A confirmação da nova senha não confere.")
            else:
                try:
                    validate_password(nova, user)
                except ValidationError as exc:
                    messages.error(request, " ".join(exc.messages))
                else:
                    user.set_password(nova)
                    user.save()
                    update_session_auth_hash(request, user)  # mantém o login ativo
                    messages.success(request, "Senha alterada com sucesso.")

        return redirect("admin_perfil")

    # Movimentações realizadas por este usuário (substitui o antigo ranking
    # da tela de movimentações — agora é uma informação pessoal do perfil).
    mov_qs = MovimentacaoItem.objects.filter(criado_por=user)
    agora = timezone.localtime()
    minhas_mov = {
        "total": mov_qs.count(),
        "mes": mov_qs.filter(
            created_at__year=agora.year, created_at__month=agora.month
        ).count(),
        "recentes": list(
            mov_qs.select_related("item").order_by("-created_at")[:6]
        ),
    }

    # Informações do sistema (somente leitura)
    SOM = StatusOrdemManutencaoChoices
    sistema = {
        "versao": SISTEMA_VERSAO,
        "django": django.get_version(),
        "itens": Item.objects.count(),
        "colaboradores_ativos": Usuario.objects.filter(status="ativo").count(),
        "licencas": Licenca.objects.count(),
        "centros_custo": CentroCusto.objects.count(),
        "fornecedores": Fornecedor.objects.count(),
        "os_abertas": OrdemManutencao.objects
            .exclude(status__in=[SOM.CONCLUIDO, SOM.CANCELADO]).count(),
    }

    return render(request, "front/admin/admin_perfil.html", {
        "perfil": user,
        "sistema": sistema,
        "minhas_mov": minhas_mov,
    })
