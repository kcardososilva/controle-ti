"""
Prazo da sessão por tipo de login.

`SESSION_COOKIE_AGE` é global (12h — ver settings.py), e isso é o certo para
gente: quem larga a estação logada perde a sessão. Só que o painel de TV fica
numa tela de parede, logado por dias, sem ninguém para digitar senha. Com o
prazo global ele cairia na tela de login no meio do expediente e ficaria assim
até alguém notar.

Esticar o `SESSION_COOKIE_AGE` de todo mundo por causa da TV seria resolver
pelo lado errado. A sessão de TV é esticada aqui, no login, uma por uma — e só
ela. O login da TV é de leitura (TVAccessMiddleware o prende a um punhado de
URLs), então o prazo longo não carrega o risco de uma sessão administrativa
aberta.

Conectado em ProjetoEstoque/apps.py (ready()).
"""
import logging

from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver

logger = logging.getLogger("seguranca")

# Mesmo literal usado por TVAccessMiddleware, media_acesso_service e
# perfil_acesso_service.
GRUPO_TV = "Visualizador TV"

# 30 dias. A tela de parede é religada quando cai energia ou o navegador é
# reiniciado; um mês cobre folgadamente o intervalo entre esses eventos.
SESSAO_TV_SEGUNDOS = 30 * 24 * 3600


@receiver(user_logged_in)
def esticar_sessao_de_painel(sender, request, user, **kwargs):
    """Dá prazo longo à sessão do painel de TV; não toca nas outras."""
    if request is None:
        return
    try:
        if user.groups.filter(name=GRUPO_TV).exists():
            request.session.set_expiry(SESSAO_TV_SEGUNDOS)
            logger.info(
                "sessao_tv_estendida usuario=%s dias=%s",
                user.username, SESSAO_TV_SEGUNDOS // 86400,
            )
    except Exception:
        # Um erro aqui não pode impedir o login: sem o ajuste a sessão apenas
        # segue o prazo global.
        logger.exception("falha ao estender sessao de painel")
