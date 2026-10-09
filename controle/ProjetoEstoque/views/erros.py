# -*- coding: utf-8 -*-
"""
Telas de erro do sistema (400 / 403 / 404 / 500).

Por que handlers próprios em vez de só largar os templates na raiz
-------------------------------------------------------------------
O Django já acha `404.html` e companhia sozinho, mas renderiza o **500 sem
contexto de request** (`django.views.defaults.server_error` usa
`Context()` puro). Resultado: `{{ assets_version }}` sai vazio e o cache-busting
do CSS some justamente na tela que mais precisa abrir certa. Os handlers abaixo
passam o request, então as quatro telas usam o mesmo caminho e o mesmo visual.

Quem vê isto
------------
Qualquer um, inclusive **sem sessão**: é o que o colaborador encontra se sair
do link de assinatura e tentar outra URL (ver regra 34). Por isso a tela é
autossuficiente — carrega `tokens.css` por conta, não depende do `base.css`, e
a ilustração é SVG inline, sem nada para baixar.

O 500 é o único que não pode confiar em nada
---------------------------------------------
Se o erro veio do banco, os context processors (`perfil_acesso`,
`sistema_noticias`) fazem query e falhariam de novo DENTRO do handler — o
usuário veria a tela branca do servidor, que é exatamente o que se quer evitar.
Daí o `try/except` com uma resposta mínima embutida: feia, mas que sempre abre.
"""

import logging

from django.http import HttpResponse
from django.shortcuts import render

logger = logging.getLogger(__name__)


def erro_404(request, exception=None):
    return render(request, "404.html", status=404)


def erro_403(request, exception=None):
    return render(request, "403.html", status=403)


def erro_400(request, exception=None):
    return render(request, "400.html", status=400)


def erro_500(request):
    try:
        return render(request, "500.html", status=500)
    except Exception:
        # Último recurso. Sem template, sem static, sem banco — só o suficiente
        # para a pessoa não encarar uma página em branco e saber o que fazer.
        logger.exception("Falha ao renderizar a própria tela de erro 500.")
        return HttpResponse(
            "<!DOCTYPE html><html lang='pt-br'><head><meta charset='UTF-8'>"
            "<title>Erro no servidor</title></head>"
            "<body style='font-family:system-ui,sans-serif;display:grid;"
            "place-items:center;min-height:100vh;margin:0;text-align:center'>"
            "<div><h1 style='margin:0 0 .5rem'>Alguma coisa quebrou do nosso lado</h1>"
            "<p style='color:#5b6475'>Tente de novo em alguns instantes ou procure o setor de TI.</p>"
            "<p><a href='/'>Ir para o início</a></p></div></body></html>",
            status=500,
        )


def erro_preview(request, codigo):
    """
    Abre a tela de erro em desenvolvimento, onde os handlers não entram.

    Com `DEBUG=True` o Django mostra o traceback de propósito — e é isso que se
    quer ali —, então sem esta rota não haveria como conferir o visual das
    quatro telas sem desligar o DEBUG. Restrita a staff: não expõe dado nenhum,
    mas também não é tela de operação.
    """
    from django.contrib.admin.views.decorators import staff_member_required

    @staff_member_required
    def _render(req):
        valido = {"400", "403", "404", "500"}
        alvo = str(codigo) if str(codigo) in valido else "404"
        # Status 200 de propósito: aqui é PREVIEW. Devolver 500 de verdade faria
        # o navegador e qualquer proxy no caminho tratarem a página como falha.
        return render(req, f"{alvo}.html", status=200)

    return _render(request)
