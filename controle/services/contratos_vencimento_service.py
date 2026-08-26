"""
Serviço de dados do painel "Avisos de Contratos a Vencer" (estilo planilha —
ver ProjetoEstoque/views/relatorios.py: avisos_contratos_vencer /
avisos_contratos_vencer_export_excel).

Cruza itens locados (Item.locado == 'sim') com o contrato de Locação
(data_entrada + tempo_locado) para achar o que já venceu ou vence dentro da
janela de alerta, resolve o usuário atual do equipamento (última
movimentação de estoque com usuário preenchido) e o login/último contato
coletado via NinjaOne — tudo numa única passada, compartilhada pela tela e
pela exportação Excel para as duas nunca divergirem.
"""
from datetime import date

from dateutil.relativedelta import relativedelta
from django.utils import timezone

from ProjetoEstoque.models import Fornecedor, Item, Localidade, MovimentacaoItem, Subtipo

DIAS_ALERTA_CONTRATO = 150

STATUS_OPERACIONAIS_VENCIMENTO = ["ativo", "backup", "estoque", "manutencao", "defeito", "queimado"]
STATUS_PAUSADO_VENCIMENTO = "pausado"


def _usuario_atual_item(item):
    """Usuário atual do equipamento, pela última movimentação com usuário preenchido."""
    ultima_mov = (
        MovimentacaoItem.objects
        .filter(item=item, usuario__isnull=False)
        .select_related("usuario")
        .order_by("-created_at")
        .first()
    )

    if not ultima_mov or not ultima_mov.usuario:
        return {"nome": "-", "username": "-", "email": "-"}

    usuario = ultima_mov.usuario
    if getattr(usuario, "first_name", None) or getattr(usuario, "last_name", None):
        nome = f"{usuario.first_name} {usuario.last_name}".strip()
    elif getattr(usuario, "nome", None):
        nome = f"{usuario.nome} {getattr(usuario, 'last_name', '')}".strip()
    else:
        nome = getattr(usuario, "username", "-") or "-"

    return {
        "nome": nome or "-",
        "username": getattr(usuario, "username", "-") or "-",
        "email": getattr(usuario, "email", "-") or "-",
    }


def _anexar_info_ninja(item):
    """
    Anexa ao item (in-memory, não persiste) os dados de login coletados via
    NinjaOne — usuário logado agora (ou último visto) e o instante do último
    contato do agente. Requer que `item` tenha vindo de um queryset com
    select_related("ninja_device").
    """
    ninja = getattr(item, "ninja_device", None)
    if ninja and (ninja.last_user or ninja.last_contact):
        item.ninja_login = ninja.last_user or ""
        item.ninja_online = bool(ninja.is_online)
        item.ninja_last_contact = ninja.last_contact
    else:
        item.ninja_login = ""
        item.ninja_online = False
        item.ninja_last_contact = None


def montar_ranking_contratos_vencimento(request):
    """
    Aplica os filtros da querystring e devolve os rankings (operacional e
    pausado) de itens locados com contrato vencido ou a até
    DIAS_ALERTA_CONTRATO dias do vencimento.
    """
    hoje = date.today()

    f_nome = (request.GET.get("nome") or "").strip()
    f_ns = (request.GET.get("ns") or "").strip()
    f_subtipo = [v for v in request.GET.getlist("subtipo") if v]
    f_status = [v for v in request.GET.getlist("status") if v]
    f_fornecedor = [v for v in request.GET.getlist("fornecedor") if v]
    f_localidade = [v for v in request.GET.getlist("localidade") if v]

    qs = (
        Item.objects
        .filter(
            locado="sim",
            locacao__isnull=False,
            locacao__data_entrada__isnull=False,
            locacao__tempo_locado__isnull=False,
        )
        .select_related("subtipo", "fornecedor", "centro_custo", "localidade", "locacao", "ninja_device")
        .order_by("nome")
    )

    if f_nome:
        qs = qs.filter(nome__icontains=f_nome)
    if f_ns:
        qs = qs.filter(numero_serie__icontains=f_ns)
    if f_subtipo:
        qs = qs.filter(subtipo_id__in=f_subtipo)
    if f_status:
        qs = qs.filter(status__in=f_status)
    if f_fornecedor:
        qs = qs.filter(fornecedor_id__in=f_fornecedor)
    if f_localidade:
        qs = qs.filter(localidade_id__in=f_localidade)

    itens_alerta = []

    for item in qs:
        loc = getattr(item, "locacao", None)
        if not loc or not loc.data_entrada or not loc.tempo_locado:
            continue

        try:
            data_vencimento = loc.data_entrada + relativedelta(months=int(loc.tempo_locado))
        except Exception:
            continue

        dias_restantes = (data_vencimento - hoje).days

        # traz vencidos e próximos do vencimento
        if dias_restantes <= DIAS_ALERTA_CONTRATO:
            item.data_vencimento_contrato = data_vencimento
            item.dias_restantes_contrato = dias_restantes
            item.valor_mensal_calc = loc.valor_mensal or 0
            item.usuario_atual = _usuario_atual_item(item)
            _anexar_info_ninja(item)
            itens_alerta.append(item)

    # Ordenação do ranking: vencidos primeiro, depois os mais próximos
    itens_alerta.sort(
        key=lambda x: (x.dias_restantes_contrato > 0, x.dias_restantes_contrato, x.nome.lower())
    )

    ranking_operacional = [
        i for i in itens_alerta if (i.status or "").lower() in STATUS_OPERACIONAIS_VENCIMENTO
    ]
    ranking_pausados = [
        i for i in itens_alerta if (i.status or "").lower() == STATUS_PAUSADO_VENCIMENTO
    ]

    vencidos = [i for i in itens_alerta if i.dias_restantes_contrato < 0]

    status_opcoes = (
        Item.objects.exclude(status__isnull=True)
        .exclude(status__exact="")
        .values_list("status", flat=True)
        .distinct()
        .order_by("status")
    )

    filtros = {
        "nome": f_nome,
        "ns": f_ns,
        "subtipo": f_subtipo,
        "status": f_status,
        "fornecedor": f_fornecedor,
        "localidade": f_localidade,
    }

    return {
        "ranking_operacional": ranking_operacional,
        "ranking_pausados": ranking_pausados,
        "itens_alerta": itens_alerta,
        "subtipos": Subtipo.objects.order_by("nome"),
        "fornecedores": Fornecedor.objects.order_by("nome"),
        "localidades": Localidade.objects.order_by("local"),
        "status_opcoes": status_opcoes,
        "filtros": filtros,
        "filtros_ativos": sum(1 for v in filtros.values() if v),
        "querystring": request.GET.urlencode(),
        "kpi": {
            "total_alertas": len(itens_alerta),
            "total_operacionais": len(ranking_operacional),
            "total_pausados": len(ranking_pausados),
            "vencidos": len(vencidos),
            "a_vencer": len(itens_alerta) - len(vencidos),
            "valor_mensal_total": sum((i.valor_mensal_calc or 0) for i in itens_alerta),
            "valor_mensal_operacional": sum((i.valor_mensal_calc or 0) for i in ranking_operacional),
            "valor_mensal_pausados": sum((i.valor_mensal_calc or 0) for i in ranking_pausados),
            "count_ativo": len([i for i in itens_alerta if (i.status or "").lower() == "ativo"]),
            "count_backup": len([i for i in itens_alerta if (i.status or "").lower() == "backup"]),
            "count_defeito": len([i for i in itens_alerta if (i.status or "").lower() == "defeito"]),
            "dias_alerta": DIAS_ALERTA_CONTRATO,
        },
        "gerado_em": timezone.localtime(),
    }
