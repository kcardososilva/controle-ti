"""
Apoio compartilhado das telas de Frotas e Conectividade (`views/frota.py`, `views/frota_api.py`)
e das telas de monitoramento TIM M2M (`views/m2m.py`, `views/m2m_api.py`).

O módulo todo é UM só para o usuário (menu "Frotas e Conectividade"), então o acesso também é uma
régua só: `exige_frota("<codename>")` — login + a permissão (`has_perm`); sem ela, 403. Os nomes
são os 13 codenames granulares do módulo:

    ver_frota                 veículos e a visão geral da frota
    editar_frota              cadastrar, editar e mudar o status de veículo
    ver_chips                 lista e detalhe de chip
    importar_chips            cadastrar chip (individual ou por planilha) e ligar/desligar o monitoramento
    vincular_chips            vincular chip a veículo
    transferir_chips          desvincular, transferir, substituir, devolver à disponibilidade e inativar
    consultar_conectividade   sessões, consumo e o "Atualizar"
    ver_historico_frota       movimentações, alertas e ocorrências
    tratar_ocorrencias        assumir, resolver, descartar e comentar ocorrência
    administrar_frota_config  integração, tipos de veículo, categorias e classes, operadoras e regras de alerta

e as três do inventário de Frotas e Maquinários (planilha do ERP):

    importar_equipamentos_frota  importar a planilha de equipamentos motorizados
    validar_inventario_frota     confirmar a situação e o cadastro do que foi importado
    exportar_frota               exportar a lista e emitir os relatórios

Esconder um botão NUNCA é autorização: toda view decide pela permissão, e a tela só mostra o que o
usuário pode usar (`pode_*` do `contexto_base`).

Nada daqui tem regra de negócio: as views validam a entrada, chamam os services e renderizam.
"""

from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.middleware.csrf import get_token
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from ProjetoEstoque.models import PERMISSOES_FROTA_CHIP, PERMISSOES_FROTA_INVENTARIO, PERMISSOES_FROTA_VEICULO
from services import ambiente_service as amb
from services import frota_service as frota
from services import m2m_monitoramento_service as m2m

PERMISSOES = {
    codigo: f"ProjetoEstoque.{codigo}"
    for codigo, _ in (*PERMISSOES_FROTA_VEICULO, *PERMISSOES_FROTA_CHIP, *PERMISSOES_FROTA_INVENTARIO)
}

POR_PAGINA = 25
# `voltar` de um POST só aceita caminho do próprio módulo (nunca endereço externo). Os dois
# últimos são os endereços antigos (até 2026-11-13): uma aba velha ainda pode mandá-los, e eles
# respondem 308 para o endereço novo em /agro/ (urls_legado_frota.py).
PREFIXOS_DO_MODULO = ("/agro/frotas/", "/agro/conectividade/", "/frotas/", "/m2m/")

# As duas SEÇÕES do módulo no Zelo Agro — cada uma é um acesso próprio no menu lateral, e a barra
# de abas do topo (`_frota_topo.html`) mostra só as abas da seção da tela. Os tipos de veículo
# são cadastro da frota; o resto da configuração (TIM, operadoras, regras) é da conectividade.
SECAO_FROTA = "frota"
SECAO_CONECTIVIDADE = "conectividade"
_ABAS_DA_FROTA = {
    "visao_frota", "veiculos", "equipamentos", "categorias", "importar_equip", "composicao", "mov_equip", "historico_equip",
    "relatorios",
}


def secao_da_aba(aba: str, aba_config: str = "") -> str:
    if aba in _ABAS_DA_FROTA or (aba == "config" and aba_config == "tipos"):
        return SECAO_FROTA
    return SECAO_CONECTIVIDADE


def exige_frota(*codigos: str):
    """Login + a permissão `codigo` (um `codename` de `PERMISSOES`). Com mais de um código, basta
    UM deles (a visão geral serve a quem vê a frota OU os chips). Sem permissão:
    `PermissionDenied` (a tela de erro 403 do sistema). Aplicar POR CIMA de `require_POST` /
    `require_GET`, para o método errado não mascarar o 403."""
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


def pode_ver_api(request) -> bool:
    """A pessoa enxerga dado de API — sessão, consumo, consultas, sincronização, alertas de conectividade, integração?
    A régua é `ambiente_service.pode_ver_api` (a mesma do menu). Toda tela que mostra o que a operadora respondeu
    pergunta isto; `ver_chips` mostra o chip como cadastro e vínculo, e nada além."""
    return amb.pode_ver_api(request.user)


def exige_api(view):
    """Para a view que é INTEIRA da API: sem permissão de API, 403 (a tela de erro do sistema). Aplicar ABAIXO de
    `exige_frota`, que já garante o login e a permissão da tela; aqui é o corte de API, que `ver_historico_frota` (também
    usada em movimentações) não faz. Esconder o item do menu não é autorização."""
    @wraps(view)
    def embrulhada(request, *args, **kwargs):
        if not pode_ver_api(request):
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return embrulhada


def erro_de_fazenda(escolhida, veiculo=None) -> str:
    """O texto do erro, ou `""` se a fazenda escolhida vale. A tela de equipamento escolhe entre as DUAS fazendas;
    quem já tem outra unidade (cadastrada antes desta regra) pode continuar com ela, e trocar para qualquer outra
    que não seja uma das duas, não. `escolhida` vazia vale (sem fazenda)."""
    escolhida = str(escolhida or "").strip()
    if not escolhida:
        return ""
    permitidas = {f.pk for f in frota.fazendas()} | {getattr(veiculo, "localidade_id", None)}
    if not escolhida.isdigit() or int(escolhida) not in permitidas:
        return "Escolha Fazenda Karitel ou Fazenda Rio do Meio."
    return ""


def migalhas(*finais) -> list:
    """O caminho do alto da página: `Zelo Agro / Frotas e Maquinários / …`. `finais` são `(rótulo, url|None)`;
    o último, sem URL, é a página em que se está."""
    return [("Zelo Agro", reverse("agro_visao_geral")), ("Frotas e Maquinários", reverse("frota_visao_geral")), *finais]


def negar_sem(request, codigo: str) -> None:
    """Dentro de uma view já decorada, exige uma permissão a mais para UM caminho dela."""
    if not pode(request, codigo):
        raise PermissionDenied


# ═════════════════════════════════════════════════════════════
# Contexto comum
# ═════════════════════════════════════════════════════════════

def contexto_base(request, aba: str, *, faixa: bool = True, **extra) -> dict:
    """O que TODAS as telas do módulo recebem: a aba ativa, o que o usuário pode fazer e, nas
    telas de CONECTIVIDADE (`faixa=True`), a faixa de saúde da integração TIM (sem o token).
    As telas de frota pura (visão geral, veículos, movimentações, importação) passam
    `faixa=False`: a saúde da API da TIM não diz nada sobre cadastro de veículo.

    `aba`: visao_frota | veiculos (seção Frotas) · visao | chips | importar | movimentacoes |
    sessoes | consumo | alertas | config (seção Conectividade; `config` com `aba_config="tipos"` é
    da Frota). A seção (`m2m_secao`) decide quais abas o topo mostra.

    Flags `pode_*` — cada botão aparece só para quem pode usá-lo:
        pode_ver_frota, pode_editar_frota, pode_ver_chips, pode_importar (cadastrar chip e ligar/
        desligar o monitoramento), pode_vincular, pode_transferir (desvincular, transferir,
        substituir, devolver, inativar), pode_atualizar (consultar_conectividade), pode_historico,
        pode_tratar, pode_configurar e, do inventário de Frotas e Maquinários, pode_importar_equip,
        pode_validar e pode_exportar."""
    # A faixa de saúde da integração TIM é API pura: quem não vê API não a recebe (nem a consulta é feita).
    api = pode_ver_api(request)
    faixa = faixa and api
    contexto = {
        "m2m_aba": aba,
        "m2m_secao": secao_da_aba(aba, extra.get("aba_config", "")),
        "m2m_com_faixa": faixa,
        "pode_ver_api": api,
        "pode_ver_frota": pode(request, "ver_frota"),
        "pode_editar_frota": pode(request, "editar_frota"),
        "pode_ver_chips": pode(request, "ver_chips"),
        "pode_importar": pode(request, "importar_chips"),
        "pode_vincular": pode(request, "vincular_chips"),
        "pode_transferir": pode(request, "transferir_chips"),
        "pode_atualizar": pode(request, "consultar_conectividade"),
        "pode_historico": pode(request, "ver_historico_frota"),
        "pode_tratar": pode(request, "tratar_ocorrencias"),
        "pode_configurar": pode(request, "administrar_frota_config"),
        "pode_importar_equip": pode(request, "importar_equipamentos_frota"),
        "pode_validar": pode(request, "validar_inventario_frota"),
        "pode_exportar": pode(request, "exportar_frota"),
    }
    contexto["m2m_alertas_abertos"] = m2m.contar_alertas_abertos() if (contexto["pode_historico"] and api) else 0
    if faixa:
        agora = timezone.now()
        dados = m2m.estado_integracao(agora)
        contexto["m2m_faixa"] = dados
        contexto["m2m_sync"] = m2m.frescor(dados["ultima_sincronizacao"], 10 ** 6, agora)
        contexto["m2m_proxima_devida"] = bool(dados["proxima"] and dados["proxima"] <= agora)
        # A sincronização automática (lida do banco: quem abre a tela não dispara consulta nenhuma).
        contexto["m2m_sinc"] = m2m.estado_sincronizacao(agora)

    # Lido UMA vez pelo `m2m.js`/`frota.js` (json_script): o JS é servido cru, então URL e CSRF
    # entram por aqui. As URLs por chip/vínculo/consulta vão em `data-url` na marcação.
    pode_buscar_opcoes = contexto["pode_vincular"] or contexto["pode_transferir"] or contexto["pode_editar_frota"]
    contexto["m2m_js"] = {
        "csrf_token": get_token(request),
        "pode_operar": contexto["pode_importar"],
        "pode_configurar": contexto["pode_configurar"],
        "urls": {
            "opcoes": reverse("m2m_opcoes") if contexto["pode_importar"] else "",
            "frota_opcoes": reverse("frota_opcoes") if pode_buscar_opcoes else "",
            "previa": reverse("m2m_config_previa") if contexto["pode_configurar"] else "",
            "testar": reverse("m2m_config_testar") if contexto["pode_configurar"] else "",
            "simular": reverse("m2m_config_frequencia") if contexto["pode_configurar"] else "",
            "sincronizacao": reverse("m2m_sincronizacao") if (contexto["pode_ver_chips"] and api) else "",
        },
    }
    contexto.update(extra)
    return contexto


# ═════════════════════════════════════════════════════════════
# Entrada e saída das views
# ═════════════════════════════════════════════════════════════

def voltar_para(request, padrao: str) -> str:
    """Para onde um POST volta: o `voltar` do formulário, se for um caminho do próprio módulo
    (`/agro/frotas/…`, `/agro/conectividade/…` ou um endereço antigo); senão `padrao`. Nunca
    segue endereço externo."""
    destino = request.POST.get("voltar") or ""
    if (
        destino.startswith(PREFIXOS_DO_MODULO)
        and not destino.startswith("//")
        and url_has_allowed_host_and_scheme(destino, allowed_hosts={request.get_host()}, require_https=request.is_secure())
    ):
        return destino
    return padrao


def erros_de(exc: ValidationError) -> dict:
    """`{campo: mensagem}` de uma `ValidationError`; sem campo, fica em `geral`."""
    if hasattr(exc, "error_dict"):
        # `__all__` é o "sem campo" do Django; o template não aceita nome com sublinhado na frente.
        return {
            ("geral" if campo == "__all__" else campo): " ".join(mensagens)
            for campo, mensagens in exc.message_dict.items()
        }
    return {"geral": " ".join(exc.messages)}


def mensagem_de(exc: ValidationError) -> str:
    """Uma frase só com tudo o que a `ValidationError` disse."""
    return " ".join(erros_de(exc).values())


OPCOES_POR_PAGINA = (10, 25, 50, 100)


def por_pagina_pedido(request, padrao: int = POR_PAGINA) -> int:
    """Quantos registros por página: o `por_pagina` da URL se for uma das opções oferecidas, senão o `padrao`.
    Valor fora da lista nunca passa (`?por_pagina=100000` carregaria a tabela inteira de uma vez)."""
    pedido = (request.GET.get("por_pagina") or "").strip()
    if pedido.isdigit() and int(pedido) in OPCOES_POR_PAGINA:
        return int(pedido)
    return padrao


def paginar(request, consulta, por_pagina: int = POR_PAGINA):
    """`(page_obj, paginas, querystring)`: a página pedida, os números a mostrar
    (com reticências) e a querystring SEM `page`, para os links do `ui-pager`.

    A paginação é do BANCO: o `Paginator` faz um `COUNT` e um `LIMIT/OFFSET`, nunca carrega a tabela para
    fatiar na memória. `por_pagina` é o padrão da tela; o usuário troca por 10/25/50/100 com `?por_pagina=`
    (`por_pagina_pedido`) e a escolha acompanha os links de página. O `page_obj` leva `opcoes_por_pagina` e
    `qs_base` (os filtros SEM `page` e SEM `por_pagina`) para o seletor de tamanho do `_paginacao.html`."""
    por_pagina = por_pagina_pedido(request, por_pagina)
    paginador = Paginator(consulta, por_pagina)
    page_obj = paginador.get_page(request.GET.get("page"))
    paginas = list(paginador.get_elided_page_range(page_obj.number, on_each_side=1, on_ends=1))
    parametros = request.GET.copy()
    parametros.pop("page", None)
    base = parametros.copy()
    base.pop("por_pagina", None)
    page_obj.opcoes_por_pagina = sorted({*OPCOES_POR_PAGINA, por_pagina})
    page_obj.qs_base = base.urlencode()
    return page_obj, paginas, parametros.urlencode()


def filtros_do_get(request, campos) -> dict:
    return {campo: (request.GET.get(campo) or "").strip() for campo in campos}


def filtros_ativos(request, filtros: dict, rotulos: dict) -> list[dict]:
    """Os filtros em uso como `[{"rotulo": "Sessão: Sem sessão", "remover": "?..."}]`:
    sem isso, a lista curta não explica por que está curta."""
    ativos = []
    for campo, valor in filtros.items():
        if not valor or campo == "ordem":
            continue
        resto = request.GET.copy()
        resto.pop(campo, None)
        resto.pop("page", None)
        nome, texto = rotulos.get(campo, (campo, None))
        ativos.append({
            "rotulo": f"{nome}: {texto(valor) if callable(texto) else valor}",
            "remover": "?" + resto.urlencode(),
        })
    return ativos


def segmentos_de(request, campo: str, opcoes) -> list[dict]:
    """Os segmentos de UM filtro (o "Todos · Vinculados · Disponíveis" no alto de uma lista).

    `opcoes`: `[(valor, rotulo, quantidade)]`, com `valor=""` para o "Todos". Devolve
    `[{"rotulo", "n", "url", "ativo"}]`: cada `url` troca só o `campo`, mantém os OUTROS filtros em uso
    e volta para a página 1. O ativo é o que bate com o valor recebido (sem valor, o "Todos")."""
    atual = (request.GET.get(campo) or "").strip()
    segmentos = []
    for valor, rotulo, n in opcoes:
        parametros = request.GET.copy()
        parametros.pop("page", None)
        parametros.pop(campo, None)
        if valor:
            parametros[campo] = valor
        segmentos.append({
            "rotulo": rotulo, "n": n, "url": "?" + parametros.urlencode(), "ativo": valor == atual,
        })
    return segmentos


def rotulo_de_id(mapa, valor):
    """O nome de um id vindo da querystring (`mapa`: `[(id, nome)]`); o próprio valor se não achar."""
    nomes = dict(mapa)
    return nomes.get(int(valor), valor) if str(valor).isdigit() else valor
