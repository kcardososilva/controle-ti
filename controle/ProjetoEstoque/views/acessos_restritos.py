"""
Acessos restritos — quem entra no sistema para usar UMA tela só.

Uma tela de TI para conceder e revogar os perfis:
  • Portal de Contratações — RH/gestor/terceiro abre a vaga e acompanha;
  • Consulta Quiosque      — vê todos os aparelhos, não muda nenhum;
  • Colaborador do Agro    — usa o Zelo Agro (ver, movimentar, cadastrar), sem dado de API e sem o TI.

Os dois primeiros são independentes e se somam: o mesmo login pode ter os dois, e cada
um é concedido e revogado sozinho, sem mexer no outro. O do Agro é outra natureza (grupo
de permissões que prende o login no Agro — `services/colaborador_agro_service.py`) e NÃO
se soma a eles: quem já tem outro acesso restrito não o recebe. Conceder e revogar passam
pelas mesmas duas rotas; `_servico` escolhe o service do perfil.

Toda a regra (quem pode receber, o que acontece com o cache, por que staff é
recusado) vive em `services/acesso_restrito_service.py`. Aqui é só formulário,
mensagem e redirecionamento.

Não há item de menu para esta tela: ela é alcançada pelo CORPO do painel de
Contratações e do painel do Quiosque — os dois lugares onde a pergunta "quem
mais vê isso?" aparece naturalmente.
"""
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from services import acesso_restrito_service as ar
from services import colaborador_agro_service as agro


def _servico(chave: str):
    """O service que concede/revoga o perfil `chave`: o do Agro tem o seu; os demais são do `acesso_restrito_service`."""
    return agro if chave == agro.CHAVE else ar


def _rotulo(chave: str) -> str:
    return agro.ROTULO if chave == agro.CHAVE else ar.PERFIS[chave]["rotulo"]


def _so_ti(request) -> bool:
    """Conceder acesso é ato de administrador. Mesma cláusula de
    `planta_tv_gerenciar`, que resolve a mesma pergunta para o grupo TV."""
    return bool(request.user.is_staff or request.user.is_superuser)


@login_required
def acesso_restrito_gerenciar(request):
    if not _so_ti(request):
        messages.error(request, "Acesso restrito a administradores.")
        return redirect("dashboard")

    return render(request, "front/acessos/acesso_restrito.html", {
        "perfis": [*ar.painel_de_acessos(), agro.painel()],
    })


@require_POST
@login_required
def acesso_restrito_conceder(request):
    if not _so_ti(request):
        messages.error(request, "Acesso restrito a administradores.")
        return redirect("dashboard")

    chave = (request.POST.get("perfil") or "").strip()
    try:
        usuario, criado = _servico(chave).conceder(
            chave=chave,
            username=request.POST.get("username", ""),
            email=request.POST.get("email", ""),
            senha=request.POST.get("senha", ""),
            user=request.user,
        )
    except ValidationError as e:
        messages.error(request, "; ".join(e.messages))
        return redirect("acesso_restrito_gerenciar")

    rotulo = _rotulo(chave)
    outros = [ar.PERFIS[c]["rotulo"] for c in ar.perfis_de(usuario) if c != chave]
    if criado:
        messages.success(
            request,
            f"Login “{usuario.username}” criado com acesso “{rotulo}”. "
            "Passe a senha para a pessoa — ela entra pela tela de acesso normal.",
        )
    else:
        extra = f" (além de “{'”, “'.join(outros)}”)" if outros else ""
        messages.success(request, f"“{usuario.username}” agora tem o acesso “{rotulo}”{extra}.")
    return redirect("acesso_restrito_gerenciar")


@require_POST
@login_required
def acesso_restrito_revogar(request, pk: int):
    if not _so_ti(request):
        messages.error(request, "Acesso restrito a administradores.")
        return redirect("dashboard")

    usuario = get_object_or_404(get_user_model(), pk=pk)
    chave = (request.POST.get("perfil") or "").strip()
    try:
        _servico(chave).revogar(chave=chave, usuario=usuario)
    except ValidationError as e:
        messages.error(request, "; ".join(e.messages))
        return redirect("acesso_restrito_gerenciar")

    if chave == agro.CHAVE:
        # sem o grupo a pessoa deixaria de estar presa ao Agro e o TI é o padrão de todo login interno: o service desativa
        messages.success(
            request,
            f"Acesso “{agro.ROTULO}” de “{usuario.username}” revogado e o login foi desativado — sem o perfil, ele passaria "
            "a ser um login interno com acesso ao TI. Para devolver o acesso ao Agro, libere de novo (o login é reativado).",
        )
        return redirect("acesso_restrito_gerenciar")

    restantes = [ar.PERFIS[c]["rotulo"] for c in ar.perfis_de(usuario)]
    resto = f" A pessoa continua com o acesso “{'”, “'.join(restantes)}”." if restantes else ""
    messages.success(
        request,
        f"Acesso “{_rotulo(chave)}” de “{usuario.username}” revogado. "
        f"O login continua existindo — só não alcança mais essa área.{resto}",
    )
    return redirect("acesso_restrito_gerenciar")
