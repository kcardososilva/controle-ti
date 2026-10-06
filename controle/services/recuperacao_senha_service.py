"""
Recuperação de senha: resolução do usuário, limite de pedidos e trilha.

Até aqui o sistema não tinha "esqueci minha senha": `templates/registration/`
estava vazio e `django.contrib.auth.urls` não era incluído. Trocar a senha
estando logado existia (admin_perfil); recuperar sem conseguir entrar, não.
Com fornecedores e parceiro de licenças usando login próprio, cada esquecimento
virava ligação para o TI.

Três decisões que valem explicação:

1. O campo aceita USUÁRIO ou E-MAIL. O padrão do Django pede e-mail, mas quem
   entra aqui decora o login, não necessariamente qual e-mail está no cadastro
   — e vários logins externos foram criados pelo TI.

2. A resposta é SEMPRE a mesma, exista o usuário ou não. É o que impede usar a
   tela como oráculo de "este login existe?" — informação de graça para quem
   está sondando o portal exposto na internet.

3. Há limite por IP e por identificador. Sem isso a tela é um disparador de
   e-mail: um laço de requisições enche a caixa de um funcionário e queima a
   cota de envio do SMTP.
"""
import logging

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db.models import Q

logger = logging.getLogger("seguranca")

# Teto de pedidos aceitos por janela de 1h.
#
# O limite por IDENTIFICADOR é o que protege uma pessoa: 4 pedidos por hora
# para o mesmo usuário barra quem quer entupir a caixa de alguém, e sobra
# margem para quem tentou e não recebeu.
#
# O limite por IP é deliberadamente FROUXO. O acesso externo passa por um
# túnel (dynamic-m.com) e, se ele não repassar o X-Forwarded-For, TODA a
# empresa chega com o mesmo REMOTE_ADDR — um teto baixo aqui viraria "o
# décimo colega que esqueceu a senha hoje não consegue recuperar". Ele existe
# só para cortar disparo automatizado, não para racionar uso legítimo.
_LIMITE_POR_IP = 40
_LIMITE_POR_IDENTIFICADOR = 4
_JANELA_SEGUNDOS = 3600


def _ip(request) -> str:
    encaminhado = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if encaminhado:
        return encaminhado.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "") or "desconhecido"


def _bater(chave: str, limite: int) -> bool:
    """Incrementa o contador da chave e diz se ainda está dentro do limite."""
    atual = cache.get(chave, 0)
    if atual >= limite:
        return False
    # `set` com o TTL inteiro a cada pedido faria a janela nunca fechar para
    # quem insiste; só o primeiro pedido define o prazo.
    if atual == 0:
        cache.set(chave, 1, _JANELA_SEGUNDOS)
    else:
        try:
            cache.incr(chave)
        except ValueError:
            # A chave expirou entre o get e o incr.
            cache.set(chave, 1, _JANELA_SEGUNDOS)
    return True


def pedido_dentro_do_limite(request, identificador: str) -> bool:
    """
    `True` se este pedido de recuperação pode seguir.

    Conta nas duas pontas: o IP (um script batendo) e o identificador (alguém
    mirando a caixa de uma pessoa específica). Estourar qualquer um dos dois
    interrompe o envio — a tela continua respondendo igual.
    """
    ip = _ip(request)
    alvo = (identificador or "").strip().lower()[:254]

    ok_ip = _bater(f"reset_senha_ip_{ip}", _LIMITE_POR_IP)
    ok_alvo = _bater(f"reset_senha_id_{alvo}", _LIMITE_POR_IDENTIFICADOR)

    if not (ok_ip and ok_alvo):
        logger.warning(
            "reset_senha_bloqueado ip=%s identificador=%s motivo=%s",
            ip, alvo, "ip" if not ok_ip else "identificador",
        )
        return False
    return True


def usuarios_para_identificador(identificador: str):
    """
    Usuários que podem receber o link para o texto informado.

    Aceita username ou e-mail (`iexact` nos dois: ninguém deve perder o acesso
    por ter digitado com maiúscula). Só entram contas ATIVAS e com e-mail
    preenchido — sem e-mail não há para onde mandar, e uma conta desativada não
    deve poder ser reativada por fora.

    `has_usable_password()` exclui quem entra por outro meio (conta de serviço
    com senha inutilizável); para essas, redefinir por link não faria sentido.
    """
    texto = (identificador or "").strip()
    if not texto:
        return []

    candidatos = User.objects.filter(
        Q(username__iexact=texto) | Q(email__iexact=texto),
        is_active=True,
    ).exclude(email="")

    return [u for u in candidatos if u.has_usable_password()]


def registrar_pedido(request, identificador: str, *, enviados: int) -> None:
    """
    Grava o pedido na trilha de segurança (`RegistroSeguranca`).

    Fica junto de login/logout/falha porque é o mesmo tipo de evento: alguém
    tentando obter acesso. `enviados=0` é o caso interessante na auditoria —
    pedido para um identificador que não resolveu em ninguém.
    """
    try:
        from ProjetoEstoque.models import (
            RegistroSeguranca,
            TipoEventoSegurancaChoices,
        )

        RegistroSeguranca.objects.create(
            tipo=TipoEventoSegurancaChoices.SENHA_RESET_PEDIDO,
            username=(identificador or "")[:254],
            ip=_ip(request) if _ip(request) != "desconhecido" else None,
            user_agent=request.META.get("HTTP_USER_AGENT", "")[:400],
            caminho=request.path[:200],
            detalhe=f"{enviados} e-mail(s) enviado(s)",
            suspeito=False,
        )
    except Exception:
        # A trilha não pode impedir a recuperação de senha.
        logger.exception("falha ao registrar pedido de recuperacao")

    logger.info(
        "reset_senha_pedido ip=%s identificador=%s enviados=%s",
        _ip(request), identificador, enviados,
    )


def registrar_conclusao(request, user) -> None:
    """Grava a troca efetiva de senha pelo link."""
    try:
        from ProjetoEstoque.models import (
            RegistroSeguranca,
            TipoEventoSegurancaChoices,
        )

        RegistroSeguranca.objects.create(
            tipo=TipoEventoSegurancaChoices.SENHA_RESET_OK,
            username=getattr(user, "username", "")[:254],
            usuario=user if getattr(user, "pk", None) else None,
            ip=_ip(request) if _ip(request) != "desconhecido" else None,
            user_agent=request.META.get("HTTP_USER_AGENT", "")[:400],
            caminho=request.path[:200],
            detalhe="senha redefinida por link",
            suspeito=False,
        )
    except Exception:
        logger.exception("falha ao registrar conclusao de recuperacao")

    logger.info(
        "reset_senha_concluido ip=%s usuario=%s",
        _ip(request), getattr(user, "username", "?"),
    )
