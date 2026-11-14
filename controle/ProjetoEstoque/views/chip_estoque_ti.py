"""
Chips do Agro × estoque de TI — as telas e as ações (regra 66). A régua é `services/chip_estoque_ti_service.py`; aqui só
validar a entrada, chamar o service e responder.

As rotas moram em `/equipamentos/chips/…` (ambiente TI): quem administra o patrimônio do chip é o TI, e o middleware já
barra quem não tem acesso ao estoque de TI — por isso um usuário só do Agro, mesmo com permissão de chips, NÃO liga nem
cadastra nada aqui. A ação é decidida pela permissão do módulo de chips (`vincular_chips`), conferida na view: botão
escondido não vale como autorização.

    chips_conciliacao             GET   ver_chips        a tela: o que o sistema propõe, o que sobra e o que diverge
    chip_estoque_conciliar        POST  vincular_chips   liga todos os pares 1 × 1 de mesmo ICCID
    chip_estoque_ligar            POST  vincular_chips   liga UM chip a UM item (`chip`, `item`, `forcar`)
    chip_estoque_desligar         POST  vincular_chips   desliga (`chip`, `motivo` obrigatório)
    chip_estoque_criar_chip       POST  vincular_chips   cria o chip de conectividade a partir de um item (`item`)
    chip_estoque_cadastrar_itens  POST  vincular_chips   cria no estoque o item de vários chips, com NF e valor
    chip_estoque_opcoes           GET   vincular_chips   JSON: chips sem item (seletor da ficha do item)
    chip_estoque_opcoes_itens     GET   vincular_chips   JSON: itens de chip sem chip (seletor da tela do chip)

`voltar` (o formulário diz para onde voltar) só aceita caminho do sistema (`/equipamentos/…` ou o módulo de chips), nunca
endereço externo. Erro de validação vira mensagem e nada é gravado; só o cadastro em lote devolve a própria tela, com o que
foi digitado preservado.
"""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from services import ambiente_service as amb
from services import chip_estoque_ti_service as estoque

from ..models import CentroCusto, Fornecedor, Item, Localidade, M2MChip, StatusItemChoices
from .frota_comum import exige_frota, pode

PREFIXOS_DE_RETORNO = ("/equipamentos/", "/agro/frotas/", "/agro/conectividade/")
LIMITE_DE_LINHAS = 100                # linhas por bloco na tela (a busca e o "todos os filtrados" cobrem o resto)
LIMITE_DO_LOTE = 500                  # chips por cadastro em lote
CAMPOS_DO_ITEM = (
    "nome", "marca", "modelo", "valor", "numero_nf", "numero_pedido", "data_compra", "fornecedor", "centro_custo",
    "localidade", "status",
)


def _voltar(request, padrao: str) -> str:
    destino = request.POST.get("voltar") or ""
    if (
        destino.startswith(PREFIXOS_DE_RETORNO) and not destino.startswith("//")
        and url_has_allowed_host_and_scheme(destino, allowed_hosts={request.get_host()}, require_https=request.is_secure())
    ):
        return destino
    return padrao


def _responder(request, acao, sucesso, padrao: str):
    """Roda `acao()`; sucesso e erro viram mensagem do Django e a volta é o `voltar` do formulário."""
    try:
        resultado = acao()
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, sucesso(resultado) if callable(sucesso) else sucesso)
        for aviso in getattr(resultado, "avisos", None) or []:
            messages.warning(request, aviso)
    return redirect(_voltar(request, padrao))


def _achar(modelo, valor, rotulo, *, manager=None):
    try:
        return (manager or modelo.objects).get(pk=int(valor))
    except (TypeError, ValueError, modelo.DoesNotExist):
        raise ValidationError(f"Escolha {rotulo}.") from None


def _tela(nome: str = "chips_conciliacao") -> str:
    return reverse(nome)


# ═════════════════════════════════════════════════════════════
# A tela de conciliação
# ═════════════════════════════════════════════════════════════

def _chips_sem_item_filtrados(conc, busca: str) -> list:
    termo = (busca or "").strip().lower()
    if not termo:
        return conc.chips_sem_item
    digitos = "".join(ch for ch in termo if ch.isdigit())
    return [
        c for c in conc.chips_sem_item
        if termo in c.nome.lower() or (len(digitos) >= 3 and (digitos in c.iccid or digitos in c.numero_linha))
    ]


def _contexto_da_conciliacao(request, valores=None, erros=None) -> dict:
    conc = estoque.conciliacao()
    divergencias = estoque.divergencias_dos_ligados(conc.ligados)
    com_divergencia = [(c, i, divergencias[c.pk]) for c, i in conc.ligados if divergencias[c.pk]]
    busca = (request.GET.get("q") or request.POST.get("q") or "").strip()
    sem_item = _chips_sem_item_filtrados(conc, busca)
    padroes = {**estoque.padroes_de_cadastro(), **(valores or {})}
    resumo = estoque.resumo_do_patrimonio()
    return {
        "conc": conc, "resumo": resumo, "busca": busca,
        "automaticos": conc.automaticos[:LIMITE_DE_LINHAS], "total_automaticos": len(conc.automaticos),
        "sem_item": sem_item[:LIMITE_DE_LINHAS], "total_sem_item_filtrados": len(sem_item), "total_sem_item": len(conc.chips_sem_item),
        "itens_sem_chip": conc.itens_sem_chip[:LIMITE_DE_LINHAS], "total_itens_sem_chip": len(conc.itens_sem_chip),
        "ambiguos": conc.ambiguos, "com_divergencia": com_divergencia[:LIMITE_DE_LINHAS], "total_divergencias": len(com_divergencia),
        "padroes": padroes, "erros": erros or {},
        "fornecedores": Fornecedor.objects.order_by("nome"),
        "centros_de_custo": CentroCusto.objects.order_by("departamento", "numero"),
        "localidades": Localidade.objects.order_by("local"),
        "status_de_cadastro": [(v, rotulo) for v, rotulo in StatusItemChoices.choices if v in estoque.STATUS_DE_CADASTRO],
        "tipos_de_chip": estoque.subtipos_de_chip(),
        "pode_ligar": pode(request, "vincular_chips"),
        "pode_ver_agro": amb.pode_acessar(request.user, amb.AGRO),
        "voltar": request.get_full_path(),
    }


@exige_frota("ver_chips")
@require_GET
def chips_conciliacao(request):
    """Conciliação: o que o sistema propõe ligar (mesmo ICCID), os chips sem patrimônio no estoque, os itens sem chip, o
    que é ambíguo e o que diverge entre os dois cadastros. Leitura pura: a página nunca grava."""
    return render(request, "front/equipamentos/chips_conciliacao.html", _contexto_da_conciliacao(request))


# ═════════════════════════════════════════════════════════════
# Ações
# ═════════════════════════════════════════════════════════════

@exige_frota("vincular_chips")
@require_POST
def chip_estoque_conciliar(request):
    """Liga, de uma vez, todos os pares 1 × 1 de mesmo ICCID. Cada par é uma transação com o seu evento."""
    resumo = estoque.conciliar_automaticamente(request.user)
    if resumo.ligados:
        messages.success(request, f"{resumo.ligados} chip{'s' if resumo.ligados != 1 else ''} ligado{'s' if resumo.ligados != 1 else ''} ao estoque de TI pelo ICCID.")
    else:
        messages.info(request, "Nenhum par novo para ligar.")
    for chip, texto in resumo.falhas[:5]:
        messages.warning(request, f"{chip.nome}: {texto}")
    return redirect(_voltar(request, _tela()))


@exige_frota("vincular_chips")
@require_POST
def chip_estoque_ligar(request):
    """Liga UM chip a UM item. Identificadores diferentes só passam com `forcar=1` (a caixa “é o mesmo SIM”)."""
    def agir():
        chip = _achar(M2MChip, request.POST.get("chip"), "um chip")
        item = _achar(Item, request.POST.get("item"), "um item", manager=Item.all_objects)
        resultado = estoque.vincular(chip, item, request.user, forcar=request.POST.get("forcar") == "1")
        resultado.nomes = (chip.nome, item.nome)
        return resultado

    return _responder(request, agir, lambda r: f"Chip {r.nomes[0]} ligado ao item “{r.nomes[1]}” do estoque de TI.", _tela())


@exige_frota("vincular_chips")
@require_POST
def chip_estoque_desligar(request):
    """Desliga o chip do item (os dois continuam existindo). O motivo é obrigatório."""
    def agir():
        chip = _achar(M2MChip, request.POST.get("chip"), "um chip")
        return estoque.desvincular(chip, request.user, request.POST.get("motivo", ""))

    return _responder(request, agir, lambda c: f"Chip {c.nome} desligado do estoque de TI.", _tela())


@exige_frota("vincular_chips")
@require_POST
def chip_estoque_criar_chip(request):
    """O item existe no estoque e o chip de conectividade não: cria o chip (ICCID = série do item) já ligado."""
    def agir():
        item = _achar(Item, request.POST.get("item"), "um item", manager=Item.all_objects)
        return estoque.criar_chip_do_item(item, request.user)

    return _responder(
        request, agir, lambda c: f"Chip {c.nome} criado em Frotas e Conectividade a partir do item de TI e já ligado a ele.", _tela(),
    )


@exige_frota("vincular_chips")
@require_POST
def chip_estoque_cadastrar_itens(request):
    """Cria no estoque de TI o item de cada chip marcado (ou de todos os da busca, com `todos=1`), com os dados comuns
    do lote: nome, marca, modelo, valor, NF, pedido, data de compra, fornecedor, centro de custo, localidade e status.
    Dado inválido devolve a tela com 400 e o que foi digitado."""
    conc_chips = M2MChip.objects.filter(item_ti__isnull=True)
    if request.POST.get("todos") == "1":
        conc = estoque.conciliacao()
        ids = [c.pk for c in _chips_sem_item_filtrados(conc, request.POST.get("q", ""))]
    else:
        ids = [int(v) for v in request.POST.getlist("ids") if str(v).isdigit()]
    chips = list(conc_chips.filter(pk__in=ids)[:LIMITE_DO_LOTE + 1])
    dados = {campo: request.POST.get(campo, "") for campo in CAMPOS_DO_ITEM}
    try:
        if not chips:
            raise ValidationError({"ids": "Marque ao menos um chip sem item no estoque de TI."})
        if len(chips) > LIMITE_DO_LOTE:
            raise ValidationError({"ids": f"Cadastre até {LIMITE_DO_LOTE} chips por vez."})
        tipo = None
        if str(request.POST.get("subtipo") or "").isdigit():
            from ..models import Subtipo
            tipo = Subtipo.objects.filter(pk=int(request.POST["subtipo"])).first()
        resumo = estoque.cadastrar_itens_dos_chips(chips, dados, request.user, subtipo=tipo)
    except ValidationError as exc:
        erros = {c: " ".join(m) for c, m in exc.message_dict.items()} if hasattr(exc, "error_dict") else {"geral": " ".join(exc.messages)}
        contexto = _contexto_da_conciliacao(request, valores=dados, erros=erros)
        contexto["abrir_cadastro"] = True
        return render(request, "front/equipamentos/chips_conciliacao.html", contexto, status=400)
    n = len(resumo.criados)
    if n:
        valor_total = sum((i.valor or 0) for _, i in resumo.criados)
        messages.success(
            request, f"{n} chip{'s' if n != 1 else ''} cadastrado{'s' if n != 1 else ''} no estoque de TI e ligado{'s' if n != 1 else ''} ao chip de conectividade"
            + (f" (R$ {valor_total:,.2f} em valor de aquisição)." if valor_total else ".")
        )
    for chip, texto in resumo.falhas[:5]:
        messages.warning(request, f"{chip.nome}: {texto}")
    if len(resumo.falhas) > 5:
        messages.warning(request, f"E mais {len(resumo.falhas) - 5} chip(s) não cadastrado(s).")
    return redirect(_voltar(request, _tela()))


# ═════════════════════════════════════════════════════════════
# Seletores (JSON)
# ═════════════════════════════════════════════════════════════

@exige_frota("vincular_chips")
@require_GET
def chip_estoque_opcoes(request):
    """Chips SEM item no estoque de TI: `{"results": [{"id", "text"}]}`. `q` busca em nome, ICCID, IMSI e linha."""
    return JsonResponse({"results": estoque.opcoes_de_chips_sem_item(request.GET.get("q", ""))}, headers={"Cache-Control": "no-store"})


@exige_frota("vincular_chips")
@require_GET
def chip_estoque_opcoes_itens(request):
    """Itens de chip do estoque de TI SEM chip: `{"results": [{"id", "text"}]}`, para quem liga a partir do chip."""
    ligados = set(M2MChip.objects.filter(item_ti__isnull=False).values_list("item_ti_id", flat=True))
    termo = (request.GET.get("q") or "").strip().lower()
    resultados = [
        {"id": i.pk, "text": f"{i.nome} · {i.numero_serie or 's/ nº'} · {i.get_status_display()}"}
        for i in estoque.itens_de_chip().order_by("nome", "pk")
        if i.pk not in ligados and (not termo or termo in i.nome.lower() or termo in (i.numero_serie or "").lower())
    ][:20]
    return JsonResponse({"results": resultados}, headers={"Cache-Control": "no-store"})


# ═════════════════════════════════════════════════════════════
# O que as duas fichas (item de TI e chip do Agro) precisam
# ═════════════════════════════════════════════════════════════

def contexto_da_linha(request, item: Item) -> dict:
    """O cartão "Linha de conectividade" da ficha do ITEM de chip no estoque de TI. Só para quem tem `ver_chips`, e só
    em item de tipo "É chip (SIM)" ou que já tem chip ligado. Leitura pura: a sessão e o consumo vêm do que a API disse."""
    from services import m2m_monitoramento_service as m2m

    invisivel = {"linha_card_visivel": False}
    if item.item_consumo == "sim" or not pode(request, "ver_chips"):
        return invisivel
    chip = M2MChip.objects.select_related("operadora", "sessao_consulta", "consumo_consulta").filter(item_ti=item).first()
    eh_chip = item.subtipo_id is not None and item.subtipo.e_chip
    if not (chip or eh_chip):
        return invisivel
    contexto = {
        "linha_card_visivel": True, "linha_chip": None, "linha_divergencias": [], "linha_onde": "", "linha_sugestoes": [],
        "linha_pode_ligar": pode(request, "vincular_chips"),
        "linha_pode_ver_agro": amb.pode_acessar(request.user, amb.AGRO),
        "linha_url_opcoes": reverse("chip_estoque_opcoes"), "linha_serie_valida": bool(estoque.chave_iccid(item.numero_serie)),
    }
    if chip:
        m2m.anotar_chips([chip])
        contexto["linha_chip"] = chip
        contexto["linha_onde"] = estoque.onde_esta(chip)
        contexto["linha_divergencias"] = estoque.divergencias_do_par(chip, item, destino=contexto["linha_onde"])
    else:
        contexto["linha_sugestoes"] = estoque.sugestoes_para_item(item)
    return contexto


def contexto_do_estoque_ti(request, chip: M2MChip) -> dict:
    """A seção "Estoque de TI" da tela do CHIP (Agro). O dado do patrimônio (valor, NF, termo, responsável) só chega a
    quem também tem acesso ao ambiente TI — um usuário só do Agro vê que existe a ligação e o status, nada financeiro."""
    ti = amb.pode_acessar(request.user, amb.TI)
    contexto = {
        "estoque_ti_acesso": ti, "estoque_ti_pode_ligar": ti and pode(request, "vincular_chips"),
        "estoque_ti_item": None, "estoque_ti_ficha": None, "estoque_ti_divergencias": [], "estoque_ti_itens_livres": [],
        "estoque_ti_url_itens": reverse("chip_estoque_opcoes_itens"), "estoque_ti_conciliacao": reverse("chips_conciliacao") if ti else "",
    }
    if not chip.item_ti_id and contexto["estoque_ti_pode_ligar"]:
        chave = estoque.chave_iccid(chip.iccid)
        ligados = set(M2MChip.objects.filter(item_ti__isnull=False).values_list("item_ti_id", flat=True))
        livres = [i for i in estoque.itens_de_chip().order_by("nome", "pk") if i.pk not in ligados]
        livres.sort(key=lambda i: estoque.chave_iccid(i.numero_serie) != chave)          # o de mesmo ICCID primeiro
        contexto["estoque_ti_itens_livres"] = livres[:50]
    if chip.item_ti_id:
        item = Item.all_objects.select_related("subtipo", "localidade", "centro_custo", "fornecedor").get(pk=chip.item_ti_id)
        contexto["estoque_ti_item"] = item
        if ti:
            contexto["estoque_ti_ficha"] = estoque.ficha_do_item(item)
            contexto["estoque_ti_divergencias"] = estoque.divergencias_do_par(chip, item, contexto["estoque_ti_ficha"]["detentor"])
    return contexto
