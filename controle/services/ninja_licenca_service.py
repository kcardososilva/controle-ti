"""
Licenças do NinjaOne que ainda estão presas a máquinas paradas.

O NinjaOne cobra por dispositivo gerenciado. Uma máquina que foi para Backup, Defeito, Manutenção ou Pausado deixou
de trabalhar, mas o agente instalado nela continua contando como licença até alguém removê-la LÁ. Isso vivia na cabeça
de quem movimentou (mesmo problema das licenças do Office, regra 52) e a conta chegava na fatura. Este módulo responde
"quais máquinas paradas ainda ocupam licença do NinjaOne?" — a tela e o e-mail leem a MESMA lista daqui.

O que conta como "ainda ocupa":
  • o dispositivo do NinjaOne está LIGADO a um equipamento do estoque (`NinjaDevice.item`), e esse equipamento está num
    dos `STATUS_SEM_USO`. Dispositivo sem equipamento ligado não entra: sem o item não se sabe o status, e adivinhar
    "esta máquina está parada" a partir do nome seria dizer ao TI para remover o agente de uma máquina que trabalha.
    Eles são só CONTADOS (`sem_vinculo`), para a tela dizer que existe uma parte do parque que ela não consegue julgar.
  • o TI ainda não deu o recado de que removeu a licença (`licenca_liberada_em`). Importação e sincronização nunca
    apagam dispositivo daqui (regra 59-d), então "sumiu do NinjaOne" não é detectável — sem este recado a mesma lista
    voltaria toda semana para sempre. Se o aparelho voltar a falar com o NinjaOne DEPOIS do recado, a remoção não
    pegou (ou ele foi reinstalado) e ele volta à lista (`voltou`).

Custo: 2 consultas para a lista (dispositivos com o equipamento; histórico de status) e +1 no painel (a contagem dos sem
vínculo), qualquer que seja o tamanho do parque.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime

from django.utils import timezone

from ProjetoEstoque.models import ItemStatusHistorico, NinjaDevice, StatusItemChoices

# Na ordem em que o pedido os cita (e em que a tela os agrupa).
STATUS_SEM_USO = ("backup", "defeito", "manutencao", "pausado")

SITUACOES = (("pendentes", "A remover"), ("liberadas", "Já removidas"), ("todas", "Todas"))


@dataclass
class Linha:
    device: object
    item: object
    status: str
    status_label: str
    desde: datetime | None            # quando chegou ao status atual (None = o histórico não diz)
    dias: int | None                  # None não é zero: "não sei desde quando" ≠ "parou hoje"
    localidade: str
    centro_custo: str
    ultimo_contato: datetime | None
    dias_sem_contato: int | None
    online: bool
    liberada_em: datetime | None
    liberada_por: str
    voltou: bool                      # falou com o NinjaOne depois de o TI dizer que removeu
    pendente: bool                    # ainda precisa de ação


def voltou_a_contatar(device) -> bool:
    return bool(device.licenca_liberada_em and device.last_contact and device.last_contact > device.licenca_liberada_em)


def pendente(device) -> bool:
    return not device.licenca_liberada_em or voltou_a_contatar(device)


def _universo():
    return (
        NinjaDevice.objects
        .filter(item__status__in=STATUS_SEM_USO)
        .select_related("item", "item__subtipo", "item__localidade", "item__centro_custo", "licenca_liberada_por")
    )


def _parado_desde(itens) -> dict[int, datetime]:
    """Quando cada item chegou ao status em que está: a entrada mais recente do histórico com esse `status_novo`.
    Uma consulta para todos — resolver item a item seria N+1 numa lista que existe justamente por ser grande."""
    por_item: dict[int, list] = {}
    for linha in (
        ItemStatusHistorico.objects.filter(item_id__in=[i.pk for i in itens])
        .order_by("item_id", "-alterado_em").values("item_id", "status_novo", "alterado_em")
    ):
        por_item.setdefault(linha["item_id"], []).append(linha)
    desde = {}
    for item in itens:
        for linha in por_item.get(item.pk, []):
            if linha["status_novo"] == item.status:
                desde[item.pk] = linha["alterado_em"]
                break
    return desde


def linhas(agora: datetime | None = None) -> list[Linha]:
    """TODAS as máquinas paradas com dispositivo no NinjaOne (pendentes e já removidas), na ordem
    status → há mais tempo parada → nome."""
    agora = agora or timezone.now()
    dispositivos = list(_universo())
    if not dispositivos:
        return []
    desde = _parado_desde([d.item for d in dispositivos])
    ordem = {s: n for n, s in enumerate(STATUS_SEM_USO)}

    saida = []
    for d in dispositivos:
        item = d.item
        inicio = desde.get(item.pk)
        volta = voltou_a_contatar(d)
        saida.append(Linha(
            device=d, item=item, status=item.status, status_label=item.get_status_display(),
            desde=inicio, dias=(agora - inicio).days if inicio else None,
            localidade=item.localidade.local if item.localidade_id else (d.local or ""),
            centro_custo=item.centro_custo.departamento if item.centro_custo_id else "",
            ultimo_contato=d.last_contact,
            dias_sem_contato=(agora - d.last_contact).days if d.last_contact else None,
            online=d.is_online,
            liberada_em=d.licenca_liberada_em,
            liberada_por=(d.licenca_liberada_por.get_full_name() or d.licenca_liberada_por.username) if d.licenca_liberada_por_id else "",
            voltou=volta, pendente=pendente(d),
        ))
    # `dias` None vai para o fim: sem histórico não dá para dizer que é a mais antiga
    saida.sort(key=lambda l: (ordem[l.status], -(l.dias if l.dias is not None else -1), l.item.nome.lower()))
    return saida


def pendentes(agora: datetime | None = None) -> list[Linha]:
    return [l for l in linhas(agora) if l.pendente]


def sem_vinculo() -> int:
    """Dispositivos do NinjaOne sem equipamento ligado: não dá para saber se estão parados."""
    return NinjaDevice.objects.filter(item__isnull=True).count()


def panorama(*, status: str = "", situacao: str = "pendentes", busca: str = "", agora: datetime | None = None) -> dict:
    """O que a tela lê. Os totais são do universo inteiro (não mudam com o filtro); `linhas` é o recorte pedido."""
    todas = linhas(agora)
    situacao = situacao if situacao in dict(SITUACOES) else "pendentes"
    status = status if status in STATUS_SEM_USO else ""
    termo = (busca or "").strip().lower()

    a_remover = [l for l in todas if l.pendente]
    rotulos = dict(StatusItemChoices.choices)
    por_status = [{"valor": s, "rotulo": rotulos[s], "n": sum(1 for l in a_remover if l.status == s)} for s in STATUS_SEM_USO]

    def _cabe(l: Linha) -> bool:
        if situacao != "todas" and (situacao == "pendentes") != l.pendente:
            return False
        if status and l.status != status:
            return False
        if termo:
            texto = " ".join((l.item.nome, l.item.numero_serie or "", l.device.display_name, l.device.hostname or "",
                              l.device.last_user or "", l.localidade))
            return termo in texto.lower()
        return True

    recorte = [l for l in todas if _cabe(l)]
    return {
        "linhas": recorte,
        "situacao": situacao, "status": status, "busca": busca or "",
        "total": len(todas),
        "a_remover": len(a_remover),
        "removidas": sum(1 for l in todas if not l.pendente),
        "online": sum(1 for l in a_remover if l.online),
        "voltaram": sum(1 for l in a_remover if l.voltou),
        "por_status": por_status,
        "sem_vinculo": sem_vinculo(),
        "situacoes": SITUACOES,
    }


def marcar_removidas(ids, user) -> int:
    """O TI diz que removeu a licença dessas máquinas no NinjaOne. Só vale para dispositivo da lista (equipamento
    parado): marcar o de uma máquina que trabalha esconderia o aviso certo no dia em que ela parasse. Usa `update()`
    de propósito — `save()` mexeria em `last_sync` ("Última importação"), e esta não é uma importação."""
    return _universo().filter(pk__in=list(ids)).update(licenca_liberada_em=timezone.now(), licenca_liberada_por=user)


def reabrir(ids) -> int:
    """Desfaz o recado: a máquina volta a constar como "a remover"."""
    return _universo().filter(pk__in=list(ids)).update(licenca_liberada_em=None, licenca_liberada_por=None)


CABECALHO_CSV = [
    "Equipamento", "Número de série", "Tipo", "Situação", "Parado desde", "Dias parado", "Localidade", "Centro de custo",
    "Dispositivo NinjaOne", "Hostname", "Último contato com o NinjaOne", "Online agora", "Último usuário logado", "Licença",
]


def csv_da_lista(itens: list[Linha]) -> bytes:
    """A lista COMPLETA em CSV (anexo do e-mail e botão da tela). O corpo do e-mail corta em 50 linhas por tabela e vários
    clientes truncam HTML grande sem avisar; o anexo leva todas e serve de lista de conferência. `utf-8-sig` e `;` porque o
    Excel do Windows lê CSV sem BOM como ANSI (os acentos do cabeçalho saem corrompidos)."""
    buffer = io.StringIO()
    w = csv.writer(buffer, delimiter=";")
    w.writerow(CABECALHO_CSV)
    for l in itens:
        d = l.device
        w.writerow([
            l.item.nome, l.item.numero_serie or "", l.item.subtipo.nome if l.item.subtipo_id else "", l.status_label,
            timezone.localtime(l.desde).strftime("%d/%m/%Y") if l.desde else "", l.dias if l.dias is not None else "",
            l.localidade, l.centro_custo, d.display_name, d.hostname or "",
            timezone.localtime(l.ultimo_contato).strftime("%d/%m/%Y %H:%M") if l.ultimo_contato else "",
            "Sim" if l.online else "Não", d.last_user or "",
            "Removida" if not l.pendente else ("Voltou a se comunicar após a remoção" if l.voltou else "A remover"),
        ])
    return buffer.getvalue().encode("utf-8-sig")

