"""
Apoio compartilhado das telas da tecnologia agrícola do Zelo Agro (estoque, catálogo, e as áreas
que virão: máquinas, infraestrutura, radiocomunicação, manutenção).

O acesso é uma régua só: `exige_tec("<codename>")` — login + a permissão (`has_perm`); sem ela, 403.
Os 13 codenames estão em `models.PERMISSOES_TEC`. Esconder um botão NUNCA é autorização: toda view
decide pela permissão, e a tela só mostra o que a pessoa pode usar (`pode_*` do `contexto_base`).

Nada daqui tem regra de negócio: as views validam a entrada, chamam os services e renderizam.

Os formulários das OPERAÇÕES (entrada, saída, ajuste, mover, inativar…) e dos cadastros do catálogo
são todos descritos por uma lista de campos (`campo(...)`) e renderizados por UM template
(`front/agro/tec/form.html`) através de `formulario(...)`. Duas razões: cada formulário novo é só a
lista dos campos e a chamada ao service, e o comportamento que NÃO pode divergir entre eles —
token de envio único contra duplo clique, erro por campo, 400 quando há erro, preservação do que
a pessoa digitou — mora num lugar só.
"""

import uuid
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme

from ProjetoEstoque.models import PERMISSOES_TEC
from ProjetoEstoque.views.frota_comum import erros_de, filtros_ativos, filtros_do_get, paginar, rotulo_de_id, segmentos_de  # noqa: F401 (reexportados para as telas)

PERMISSOES = {codigo: f"ProjetoEstoque.{codigo}" for codigo, _ in PERMISSOES_TEC}
POR_PAGINA = 25

# `voltar` de um POST só aceita caminho do Agro que o módulo conhece (nunca endereço externo): o estoque
# e o detalhe das máquinas, de onde se instala e se retira.
PREFIXOS_DO_MODULO = ("/agro/estoque/", "/agro/frotas/")

TEMPLATE_FORM = "front/agro/tec/form.html"


def exige_tec(*codigos: str):
    """Login + a permissão `codigo` (um codename de `PERMISSOES`). Com mais de um código, basta UM
    deles. Sem permissão: `PermissionDenied` (a tela de erro 403 do sistema). Aplicar POR CIMA de
    `require_POST`/`require_GET`, para o método errado não mascarar o 403."""
    permissoes = tuple(PERMISSOES[c] for c in codigos)

    def decorador(view):
        @wraps(view)
        @login_required
        def embrulhada(request, *args, **kwargs):
            if not any(request.user.has_perm(p) for p in permissoes):
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return embrulhada

    return decorador


def pode(request, codigo: str) -> bool:
    return request.user.has_perm(PERMISSOES[codigo])


def contexto_base(request, aba: str, **extra) -> dict:
    """O que TODAS as telas da tecnologia recebem: a aba ativa (`tec_aba`: visao | equipamentos |
    materiais | movimentacoes | catalogo) e o que a pessoa pode fazer (`pode_*`)."""
    contexto = {
        "tec_aba": aba,
        "pode_ver": pode(request, "ver_tec_estoque"),
        "pode_cadastrar": pode(request, "cadastrar_tec_equipamento"),
        "pode_transferir": pode(request, "transferir_tec_equipamento"),
        "pode_instalar": pode(request, "instalar_tec_equipamento"),
        "pode_historico": pode(request, "ver_tec_historico"),
        "pode_catalogo": pode(request, "administrar_tec_catalogo"),
        "pode_config": pode(request, "administrar_tec_config"),
    }
    contexto.update(extra)
    return contexto


def voltar_seguro(request, padrao: str) -> str:
    """O `voltar` do formulário se for caminho do módulo; senão `padrao`."""
    destino = request.POST.get("voltar") or request.GET.get("voltar") or ""
    if (
        destino.startswith(PREFIXOS_DO_MODULO)
        and not destino.startswith("//")
        and url_has_allowed_host_and_scheme(destino, allowed_hosts={request.get_host()}, require_https=request.is_secure())
    ):
        return destino
    return padrao


# ═════════════════════════════════════════════════════════════
# Campos e opções
# ═════════════════════════════════════════════════════════════

def campo(nome: str, rotulo: str, tipo: str = "text", *, valor="", opcoes=None, grupos=None, obrigatorio: bool = False,
          dica: str = "", maximo: int | None = None, mono: bool = False, placeholder: str = "", vazio: str = "",
          so_leitura: bool = False, largo: bool = False, dados: dict | None = None, auxiliar: bool = False) -> dict:
    """Descreve UM campo do formulário. `tipo`: text | textarea | select | date | number | checkbox.
    `opcoes`: `[(valor, rótulo)]`; `grupos`: `[(rótulo do grupo, [(valor, rótulo)])]` (optgroup);
    `vazio`: rótulo da opção em branco do select ("" = sem opção em branco). `largo` ocupa a linha toda.
    `dados`: atributos `data-*` do controle (`{"m2m-s2": "local"}` vira `data-m2m-s2="local"`): é como um select
    ganha Select2 sem que o renderizador conheça o Select2. `auxiliar`: campo que só ajuda a preencher outro (o
    FABRICANTE que filtra os modelos): vai na tela e volta preenchido se o envio falhar, mas NÃO entra nos `dados`
    que o service recebe — o que ele grava é o modelo, e o fabricante vem dele."""
    return {
        "nome": nome, "rotulo": rotulo, "tipo": tipo, "valor": "" if valor is None else valor, "opcoes": opcoes or [],
        "grupos": grupos or [], "obrigatorio": obrigatorio, "dica": dica, "maximo": maximo, "mono": mono,
        "placeholder": placeholder, "vazio": vazio, "so_leitura": so_leitura, "largo": largo, "erro": "", "dados": dados or {},
        "auxiliar": auxiliar,
    }


def opcoes_de(queryset, rotulo=str, *, atual=None) -> list:
    """`[(pk, rótulo)]` de um queryset; `atual` (uma instância) entra se não estiver nele — um item
    inativo já escolhido não pode sumir da edição."""
    itens = list(queryset)
    if atual is not None and atual.pk not in {i.pk for i in itens}:
        itens.append(atual)
    return [(i.pk, rotulo(i)) for i in itens]


# ═════════════════════════════════════════════════════════════
# O formulário genérico
# ═════════════════════════════════════════════════════════════

def formulario(request, *, aba: str, titulo: str, descricao: str, voltar_url: str, voltar_rotulo: str, campos: list,
               executar, rotulo_enviar: str = "Salvar", perigo: bool = False, aviso: str = "", secoes: list | None = None,
               tem_token: bool = True, **extra):
    """Renderiza e processa um formulário descrito por `campos`.

    GET: mostra os valores iniciais. POST: lê o valor de cada campo pelo nome (checkbox vira bool),
    chama `executar(dados, token)` e, se der certo, grava a mensagem e redireciona. `executar` devolve
    `(mensagem, url_de_destino)` ou `(mensagem, url, repetida)`; com `repetida=True` (o mesmo token já
    tinha sido registrado) a mensagem vira um aviso, não um sucesso — e nada novo foi gravado.
    `ValidationError` volta para a tela com o erro no campo certo (ou no topo, se não é de campo) e
    status 400. `secoes`: `[(título, [nomes dos campos])]` para dividir em painéis; sem ele, um painel só."""
    token = (request.POST.get("token") if request.method == "POST" else "") or str(uuid.uuid4())
    erros: dict = {}
    if request.method == "POST":
        dados = {}
        for c in campos:
            if c["so_leitura"]:
                continue
            if c["tipo"] == "checkbox":
                c["valor"] = c["nome"] in request.POST
            else:
                c["valor"] = (request.POST.get(c["nome"]) or "").strip()
            if not c["auxiliar"]:
                dados[c["nome"]] = c["valor"]
        try:
            resultado = executar(dados, token)
            mensagem, destino = resultado[0], resultado[1]
            repetida = len(resultado) > 2 and resultado[2]
            (messages.info if repetida else messages.success)(request, ("Esta operação já tinha sido registrada. " if repetida else "") + mensagem)
            return redirect(voltar_seguro(request, destino) if request.POST.get("voltar") else destino)
        except ValidationError as exc:
            erros = erros_de(exc)
        nomes = {c["nome"] for c in campos}
        for nome, texto in erros.items():
            for c in campos:
                if c["nome"] == nome:
                    c["erro"] = texto
        erros_gerais = " ".join(texto for nome, texto in erros.items() if nome not in nomes)
        messages.error(request, "Revise os campos destacados.")
    else:
        erros_gerais = ""
    por_nome = {c["nome"]: c for c in campos}
    paineis = (
        [{"titulo": t, "campos": [por_nome[n] for n in nomes if n in por_nome]} for t, nomes in secoes]
        if secoes else [{"titulo": "", "campos": campos}]
    )
    contexto = contexto_base(
        request, aba, titulo=titulo, descricao=descricao, voltar_url=voltar_url, voltar_rotulo=voltar_rotulo,
        paineis=paineis, rotulo_enviar=rotulo_enviar, perigo=perigo, aviso=aviso, token=token if tem_token else "",
        erros_gerais=erros_gerais, voltar_post=request.GET.get("voltar", ""), **extra,
    )
    return render(request, TEMPLATE_FORM, contexto, status=400 if (request.method == "POST" and (erros or erros_gerais)) else 200)
