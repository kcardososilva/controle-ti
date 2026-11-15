"""
Chip na FICHA do equipamento de TI (tablet, celular) — a ponta do TI do vínculo de chip (regra 62).

O chip é cadastrado e monitorado em Frotas e Conectividade (ambiente Agro); o tablet e o celular são
itens do estoque de TI. Quem liga um ao outro é o `FrotaVinculo` (`item`), e quem opera isso no dia a
dia é o TI — por isso estas rotas moram em `/equipamentos/…` (ambiente TI) e a ação é decidida pela
PERMISSÃO do módulo de chips, não por quem pode entrar no Agro. Sem `vincular_chips`/`transferir_chips`
o usuário vê o cartão (se tiver `ver_chips`), mas não os botões.

Nenhuma regra de negócio mora aqui: cada função confere que o vínculo é MESMO deste equipamento (a URL
não pode servir para mexer no chip de outro aparelho), chama `frota_vinculo_service` e responde. O
service é o mesmo do módulo de chips — o histórico, o motivo obrigatório e a constraint de um vínculo
ativo por chip valem igual nas duas pontas. O `Item` nunca é gravado.

    equipamento_chip_vincular     vincular_chips     (chip, motivo)
    equipamento_chip_desvincular  transferir_chips   (motivo, destino do chip)
    equipamento_chip_substituir   transferir_chips   (chip_novo, motivo, destino do chip antigo)
    equipamento_chip_opcoes       vincular_chips | transferir_chips   (JSON do seletor, `q`)

Resposta: mensagem do Django + redirect para a ficha (o cartão já se redesenha com o chip novo). Erro de
validação vira mensagem de erro na ficha; nada é gravado.
"""

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from services import frota_vinculo_service as vinculos

from ..models import FrotaVinculo, Item, M2MChip
from .frota_api import _achar, _executar
from .frota_comum import exige_frota, pode


def _item_do_ti(pk) -> Item:
    """O equipamento da URL. `all_objects`: o excluído (soft delete) também abre — o service recusa com o motivo certo."""
    return get_object_or_404(Item.all_objects.select_related("subtipo"), pk=pk)


def _vinculo_deste_item(item: Item, vinculo_id) -> int:
    """O id do vínculo, SE ele é deste equipamento. Evita mexer no chip de outro aparelho trocando o id na URL."""
    if not FrotaVinculo.objects.filter(pk=vinculo_id, item=item).exists():
        raise ValidationError("Este chip não está vinculado a este equipamento.")
    return vinculo_id


def _ficha(item: Item) -> str:
    return reverse("equipamento_detalhe", args=[item.pk])


# ═════════════════════════════════════════════════════════════
# Contexto da ficha (o cartão "Chip")
# ═════════════════════════════════════════════════════════════

def contexto_do_chip(request, item: Item) -> dict:
    """O que o cartão "Chip" da ficha precisa. O cartão só aparece para quem tem `ver_chips` e quando faz
    sentido: o tipo do equipamento aceita chip, OU ele já teve chip (um tipo que deixou de aceitar não
    apaga a história). Leitura pura — nada é gravado, nada é reinterpretado: a sessão e o consumo vêm
    do que a API disse, pelo mesmo `anotar_chips` da lista de chips."""
    invisivel = {"chip_card_visivel": False}
    if item.item_consumo == "sim" or not pode(request, "ver_chips"):
        return invisivel
    aceita = vinculos.equipamento_aceita_chip(item)
    dados = vinculos.chips_do_equipamento(item)
    if not (aceita or dados["ativos"] or dados["anteriores"]):
        return invisivel
    return {
        "chip_card_visivel": True,
        "chip_aceita": aceita,
        "chips_ativos": dados["ativos"],
        "chips_anteriores": dados["anteriores"],
        "chips_total_anteriores": dados["total_anteriores"],
        "chip_pode_vincular": aceita and pode(request, "vincular_chips"),
        "chip_pode_transferir": pode(request, "transferir_chips"),
        "chip_url_opcoes": reverse("equipamento_chip_opcoes"),
    }


# ═════════════════════════════════════════════════════════════
# Ações
# ═════════════════════════════════════════════════════════════

@exige_frota("vincular_chips")
@require_POST
def equipamento_chip_vincular(request, pk):
    """Vincula um chip DISPONÍVEL a este equipamento (`chip`, `motivo`). O service recusa tipo que não aceita
    chip, equipamento excluído/descartado e chip que já está em outro lugar."""
    item = _item_do_ti(pk)

    def agir():
        chip = _achar(M2MChip, request.POST.get("chip"), "chip", "um chip")
        resultado = vinculos.vincular(chip, item, request.user, request.POST.get("motivo", ""))
        resultado.chip_nome = chip.nome
        return resultado

    return _executar(request, agir, lambda r: f"Chip {r.chip_nome} vinculado a {item.nome}.", _ficha(item))


@exige_frota("transferir_chips")
@require_POST
def equipamento_chip_desvincular(request, pk, vinculo_id):
    """Encerra o vínculo `vinculo_id` deste equipamento. `destino`: uma das situações livres do chip (`frota_vinculo_service.SITUACOES_LIVRES`)."""
    item = _item_do_ti(pk)
    destino = (request.POST.get("destino") or "").strip() or "disponivel"

    def agir():
        return vinculos.desvincular(_vinculo_deste_item(item, vinculo_id), request.user, request.POST.get("motivo", ""), destino)

    return _executar(
        request, agir,
        lambda r: f"Chip {r.vinculo_encerrado.chip.nome} desvinculado de {item.nome}.",
        _ficha(item),
    )


@exige_frota("transferir_chips")
@require_POST
def equipamento_chip_substituir(request, pk, vinculo_id):
    """Troca o chip do vínculo `vinculo_id` por `chip_novo` NESTE equipamento. `destino_antigo`: uma das situações livres do chip."""
    item = _item_do_ti(pk)

    def agir():
        antigo = _vinculo_deste_item(item, vinculo_id)
        novo = _achar(M2MChip, request.POST.get("chip_novo"), "chip_novo", "o chip novo")
        destino = (request.POST.get("destino_antigo") or "").strip() or "disponivel"
        return vinculos.substituir(antigo, novo, request.user, request.POST.get("motivo", ""), destino)

    return _executar(
        request, agir,
        lambda r: f"Chip {r.vinculo_encerrado.chip.nome} substituído pelo chip {r.vinculo.chip.nome} em {item.nome}.",
        _ficha(item),
    )


@exige_frota("vincular_chips", "transferir_chips")
@require_GET
def equipamento_chip_opcoes(request):
    """Chips DISPONÍVEIS para o seletor da ficha: `{"results": [{"id", "text"}]}`. `q` busca em nome, ICCID, IMSI e
    número da linha. Sem busca, os primeiros por nome (o seletor abre com uma lista curta, não com os 200 chips)."""
    if not (pode(request, "vincular_chips") or pode(request, "transferir_chips")):
        raise PermissionDenied
    q = request.GET.get("q", "")
    resultados = [{"id": c["id"], "text": c["text"]} for c in vinculos.chips_disponiveis(q, limite=20)]
    return JsonResponse({"results": resultados})
