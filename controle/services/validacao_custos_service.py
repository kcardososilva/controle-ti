"""
Serviço de dados da tela "Validação de Custos por Centro de Custo / PMB"
(painel gerencial estilo planilha — ver ProjetoEstoque/views/dashboards.py:
validacao_custos_planilha / validacao_custos_export_excel).

Junta em uma única lista "linha a linha" (como uma planilha de inventário)
os três tipos de custo já reconhecidos pelo sistema:

  - Locação        → Locacao.valor_mensal (equipamento locado, recorrente)
  - Licença        → custo mensal por assento atribuído (LicencaLote)
  - Custo Patrimônio → Item.valor (equipamento próprio/ativo, custo único)

e agrupa por Centro de Custo, permitindo validar o gasto por CC e cruzar
com a flag PMB.

O PMB do Centro de Custo NESTA tela é efetivo por nome (ver `_pmb_efetivo`):
CC com "Tabaco" no nome do departamento é PMB, todo o resto é Fazenda —
regra pedida explicitamente (o campo `CentroCusto.pmb` cadastrado manualmente
ficava divergente). O PMB do item/licença continua vindo do campo cadastrado
(`Item.pmb` / `Licenca.pmb`); a tela sinaliza como "divergência" quando ele
não bate com o PMB efetivo do CC.
"""
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Prefetch
from django.utils import timezone

from ProjetoEstoque.models import (
    CentroCusto,
    Fornecedor,
    Item,
    ItemColaborador,
    Locacao,
    MovimentacaoItem,
    MovimentacaoLicenca,
    SimNaoChoices,
    StatusItemChoices,
    Subtipo,
    TipoMovLicencaChoices,
    TipoMovimentacaoChoices,
    TipoTransferenciaChoices,
)

TIPOS_CUSTO_VALIDOS = ("locacao", "licenca", "patrimonio")

TIPO_CUSTO_LABELS = {
    "locacao": "Locação",
    "licenca": "Licença",
    "patrimonio": "Custo Patrimônio",
}


def _pmb_efetivo(cc):
    """
    PMB efetivo do Centro de Custo NESTA tela — calculado pelo nome do
    departamento, não pelo campo `CentroCusto.pmb` (cadastro manual sujeito
    a ficar desatualizado): CC com "Tabaco" no nome é PMB; todo o resto é
    Fazenda. Regra de negócio pedida explicitamente para esta validação.
    """
    nome = (cc.departamento or "").lower()
    return SimNaoChoices.SIM if "tabaco" in nome else SimNaoChoices.NAO


def _aplicar_filtro_pmb(queryset, prefixo, pmb_filtro):
    """Filtra um queryset pelo PMB efetivo (nome contém 'tabaco'), via `prefixo` até o CC."""
    if not pmb_filtro:
        return queryset
    campo = f"{prefixo}departamento__icontains"
    if pmb_filtro == SimNaoChoices.SIM:
        return queryset.filter(**{campo: "tabaco"})
    return queryset.exclude(**{campo: "tabaco"})


def _custo_mensal_unitario_lote(lote):
    """
    Custo mensal por assento de um LicencaLote.

    Mesma fórmula de `_get_cc_custos_data` (ProjetoEstoque/views/dashboards.py)
    — duplicada aqui (função pequena) para este serviço não depender de
    `views/`, o que inverteria a direção de importação do projeto.
    """
    if not lote:
        return Decimal("0.00")

    qtd = Decimal(lote.quantidade_total or 0)
    if qtd <= 0:
        return Decimal("0.00")

    custo_ciclo = Decimal(lote.custo_ciclo or 0)
    periodicidade = str(lote.periodicidade or "").lower()
    divisor = {"mensal": 1, "trimestral": 3, "semestral": 6, "anual": 12}.get(periodicidade, 1)

    custo_mensal_lote = (custo_ciclo / Decimal(divisor)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return (custo_mensal_lote / qtd).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _detentor_atual(item):
    """Réplica simplificada do cálculo de 'detentor atual' usado em equipamento_detalhe."""
    movs = getattr(item, "_movs_desc", None)
    if movs is None:
        movs = list(
            item.movimentacoes
            .select_related("usuario", "centro_custo_destino", "localidade_destino", "fornecedor_manutencao")
            .order_by("-created_at")[:1]
        )
    if not movs:
        return "Em estoque / Não definido"

    ultima = movs[0]
    eh_devolucao = (
        ultima.tipo_movimentacao == TipoMovimentacaoChoices.TRANSFERENCIA
        and ultima.tipo_transferencia == TipoTransferenciaChoices.DEVOLUCAO
    )
    if ultima.usuario and not eh_devolucao:
        return ultima.usuario.nome
    if ultima.centro_custo_destino:
        return f"Setor: {ultima.centro_custo_destino.departamento}"
    if ultima.localidade_destino:
        return f"Local: {ultima.localidade_destino.local}"
    if ultima.fornecedor_manutencao:
        return f"Externo: {ultima.fornecedor_manutencao.nome}"
    return "Em estoque / Não definido"


def _colaborador_label(item):
    if item.compartilhado:
        vinculos = getattr(item, "_vinculos_ativos", None)
        if vinculos is None:
            vinculos = list(item.vinculos_colaborador.filter(ativo=True).select_related("colaborador"))
        if vinculos:
            return ", ".join(v.colaborador.nome for v in vinculos)
        return "Compartilhado / Sem vínculo ativo"
    return _detentor_atual(item)


def _prefetch_item_extras(queryset, related_name):
    """Prefetch de última movimentação + vínculos compartilhados ativos, sem N+1."""
    return queryset.prefetch_related(
        Prefetch(
            f"{related_name}movimentacoes",
            queryset=MovimentacaoItem.objects.select_related(
                "usuario", "centro_custo_destino", "localidade_destino", "fornecedor_manutencao"
            ).order_by("-created_at"),
            to_attr="_movs_desc",
        ),
        Prefetch(
            f"{related_name}vinculos_colaborador",
            queryset=ItemColaborador.objects.filter(ativo=True).select_related("colaborador"),
            to_attr="_vinculos_ativos",
        ),
    )


STATUS_ITEM_VALIDOS = [c[0] for c in StatusItemChoices.choices]


def _parse_filtros(request):
    subtipo_ids = [int(v) for v in request.GET.getlist("subtipo") if v.isdigit()]
    cc_ids = [int(v) for v in request.GET.getlist("centro_custo") if v.isdigit()]
    fornecedor_ids = [int(v) for v in request.GET.getlist("fornecedor") if v.isdigit()]

    pmb_filtro = (request.GET.get("pmb") or "").strip().lower()
    if pmb_filtro not in ("sim", "nao"):
        pmb_filtro = ""

    tipos_sel = [t for t in request.GET.getlist("tipo_custo") if t in TIPOS_CUSTO_VALIDOS]
    if not tipos_sel:
        tipos_sel = list(TIPOS_CUSTO_VALIDOS)

    # Sem filtro explícito, mantém o comportamento histórico da tela: só Ativo.
    # Selecionando outros (Backup, Manutenção, Defeito...) amplia a visão —
    # útil pra ver custo de locação/patrimônio de equipamento fora de uso.
    status_sel = [s for s in request.GET.getlist("status") if s in STATUS_ITEM_VALIDOS]
    if not status_sel:
        status_sel = [StatusItemChoices.ATIVO]

    return subtipo_ids, cc_ids, fornecedor_ids, pmb_filtro, tipos_sel, status_sel


def _linha_base(tipo_custo, cc, subtipo, categoria, descricao, marca_modelo, numero_serie,
                 colaborador, localidade, status, valor, pmb_origem, fornecedor, obj_id):
    pmb_origem = pmb_origem or SimNaoChoices.NAO
    pmb_cc = _pmb_efetivo(cc)
    cc.pmb_efetivo = pmb_cc  # anotado na instância p/ o template (evita reler cc.pmb cru)
    return {
        "tipo_custo": tipo_custo,
        "tipo_label": TIPO_CUSTO_LABELS[tipo_custo],
        "recorrencia": "Único" if tipo_custo == "patrimonio" else "Mensal",
        "cc": cc,
        "subtipo": subtipo,
        "categoria": categoria,
        "descricao": descricao,
        "marca_modelo": marca_modelo,
        "numero_serie": numero_serie,
        "colaborador": colaborador,
        "localidade": localidade,
        "status": status,
        "valor": Decimal(valor or 0).quantize(Decimal("0.01")),
        "fornecedor": fornecedor,
        "fornecedor_nome": fornecedor.nome if fornecedor else "-",
        "pmb_origem": pmb_origem,
        "pmb_cc": pmb_cc,
        "diverge_pmb": pmb_origem != pmb_cc,
        "obj_id": obj_id,
    }


def montar_dados_validacao_custos(request):
    subtipo_ids, cc_ids, fornecedor_ids, pmb_filtro, tipos_sel, status_sel = _parse_filtros(request)

    linhas = []

    # ================= Locação (equipamento locado, custo recorrente) =================
    # Fornecedor da linha = Locacao.fornecedor (quem recebe o pagamento mensal), não o
    # Item.fornecedor (que pode ser o fabricante/vendedor original do equipamento).
    if "locacao" in tipos_sel:
        loc_qs = (
            Locacao.objects
            .select_related(
                "equipamento", "equipamento__centro_custo", "equipamento__subtipo",
                "equipamento__subtipo__categoria", "equipamento__localidade", "fornecedor",
            )
            .filter(
                equipamento__status__in=status_sel,
                valor_mensal__gt=0,
                equipamento__centro_custo__isnull=False,
            )
        )
        loc_qs = _prefetch_item_extras(loc_qs, "equipamento__")

        if subtipo_ids:
            loc_qs = loc_qs.filter(equipamento__subtipo_id__in=subtipo_ids)
        if cc_ids:
            loc_qs = loc_qs.filter(equipamento__centro_custo_id__in=cc_ids)
        if fornecedor_ids:
            loc_qs = loc_qs.filter(fornecedor_id__in=fornecedor_ids)
        loc_qs = _aplicar_filtro_pmb(loc_qs, "equipamento__centro_custo__", pmb_filtro)

        for loc in loc_qs:
            item = loc.equipamento
            linhas.append(_linha_base(
                tipo_custo="locacao",
                cc=item.centro_custo,
                subtipo=item.subtipo,
                categoria=item.subtipo.categoria if item.subtipo else None,
                descricao=item.nome,
                marca_modelo=" / ".join(filter(None, [item.marca, item.modelo])) or "-",
                numero_serie=item.numero_serie or "-",
                colaborador=_colaborador_label(item),
                localidade=item.localidade.local if item.localidade else "-",
                status=item.get_status_display(),
                valor=loc.valor_mensal,
                pmb_origem=item.pmb,
                fornecedor=loc.fornecedor,
                obj_id=item.id,
            ))

    # ================= Custo Patrimônio (equipamento próprio, custo único) =============
    if "patrimonio" in tipos_sel:
        itens_qs = (
            Item.objects
            .select_related("centro_custo", "subtipo", "subtipo__categoria", "localidade", "fornecedor")
            .filter(
                status__in=status_sel,
                item_consumo=SimNaoChoices.NAO,
                locado=SimNaoChoices.NAO,
                valor__gt=0,
                centro_custo__isnull=False,
            )
        )
        itens_qs = _prefetch_item_extras(itens_qs, "")

        if subtipo_ids:
            itens_qs = itens_qs.filter(subtipo_id__in=subtipo_ids)
        if cc_ids:
            itens_qs = itens_qs.filter(centro_custo_id__in=cc_ids)
        if fornecedor_ids:
            itens_qs = itens_qs.filter(fornecedor_id__in=fornecedor_ids)
        itens_qs = _aplicar_filtro_pmb(itens_qs, "centro_custo__", pmb_filtro)

        for item in itens_qs:
            linhas.append(_linha_base(
                tipo_custo="patrimonio",
                cc=item.centro_custo,
                subtipo=item.subtipo,
                categoria=item.subtipo.categoria if item.subtipo else None,
                descricao=item.nome,
                marca_modelo=" / ".join(filter(None, [item.marca, item.modelo])) or "-",
                numero_serie=item.numero_serie or "-",
                colaborador=_colaborador_label(item),
                localidade=item.localidade.local if item.localidade else "-",
                status=item.get_status_display(),
                valor=item.valor,
                pmb_origem=item.pmb,
                fornecedor=item.fornecedor,
                obj_id=item.id,
            ))

    # ================= Licença (assento atribuído, custo recorrente) ===================
    # Licença não tem subtipo — ao filtrar por subtipo, estas linhas ficam de fora
    # (o filtro de subtipo é uma classificação exclusiva de equipamento).
    if "licenca" in tipos_sel and not subtipo_ids:
        movs_lic = (
            MovimentacaoLicenca.objects
            .select_related(
                "licenca", "licenca__centro_custo", "licenca__fornecedor", "usuario",
                "usuario__centro_custo", "usuario__localidade", "centro_custo_destino", "lote",
            )
            .filter(usuario__isnull=False)
            .order_by("licenca_id", "usuario_id", "created_at")
        )
        estado_atual = {}
        for mov in movs_lic:
            estado_atual[(mov.licenca_id, mov.usuario_id)] = mov

        for mov in estado_atual.values():
            if mov.tipo != TipoMovLicencaChoices.ATRIBUICAO or not mov.licenca:
                continue

            cc = (mov.usuario.centro_custo if mov.usuario else None) or mov.centro_custo_destino or mov.licenca.centro_custo
            if not cc:
                continue
            if cc_ids and cc.id not in cc_ids:
                continue
            if fornecedor_ids and mov.licenca.fornecedor_id not in fornecedor_ids:
                continue
            if pmb_filtro and _pmb_efetivo(cc) != pmb_filtro:
                continue

            custo = _custo_mensal_unitario_lote(mov.lote) if mov.lote else Decimal("0.00")
            usuario_localidade = mov.usuario.localidade.local if mov.usuario and mov.usuario.localidade else "-"

            linhas.append(_linha_base(
                tipo_custo="licenca",
                cc=cc,
                subtipo=None,
                categoria=None,
                descricao=mov.licenca.nome,
                marca_modelo="Licença de Software",
                numero_serie="-",
                colaborador=mov.usuario.nome if mov.usuario else "-",
                localidade=usuario_localidade,
                status="Atribuída",
                valor=custo,
                pmb_origem=mov.licenca.pmb,
                fornecedor=mov.licenca.fornecedor,
                obj_id=mov.licenca_id,
            ))

    # ================= Resumo por Centro de Custo (pivot) ==============================
    resumo_map = {}
    for l in linhas:
        cc = l["cc"]
        if cc.id not in resumo_map:
            resumo_map[cc.id] = {
                "cc": cc,
                "qtd": 0,
                "custo_locacao": Decimal("0.00"),
                "custo_licenca": Decimal("0.00"),
                "custo_patrimonio": Decimal("0.00"),
                "divergencias": 0,
            }
        r = resumo_map[cc.id]
        r["qtd"] += 1
        if l["tipo_custo"] == "locacao":
            r["custo_locacao"] += l["valor"]
        elif l["tipo_custo"] == "licenca":
            r["custo_licenca"] += l["valor"]
        else:
            r["custo_patrimonio"] += l["valor"]
        if l["diverge_pmb"]:
            r["divergencias"] += 1

    resumo_cc = []
    for r in resumo_map.values():
        r["total_mensal"] = (r["custo_locacao"] + r["custo_licenca"]).quantize(Decimal("0.01"))
        r["total_geral"] = (r["total_mensal"] + r["custo_patrimonio"]).quantize(Decimal("0.01"))
        resumo_cc.append(r)
    resumo_cc.sort(key=lambda r: r["total_geral"], reverse=True)

    # ================= Agrupamento linha-a-linha por CC (na mesma ordem do resumo) =====
    linhas_por_cc = {}
    for l in linhas:
        linhas_por_cc.setdefault(l["cc"].id, []).append(l)
    for lst in linhas_por_cc.values():
        lst.sort(key=lambda l: (l["tipo_label"], l["descricao"] or ""))

    grupos = [
        {"resumo": r, "linhas": linhas_por_cc.get(r["cc"].id, [])}
        for r in resumo_cc
    ]

    # numeração sequencial das linhas na ordem final de exibição (efeito "planilha")
    contador = 0
    for grupo in grupos:
        for l in grupo["linhas"]:
            contador += 1
            l["linha_num"] = contador

    # ================= KPIs gerais =======================================================
    total_locacao = sum((l["valor"] for l in linhas if l["tipo_custo"] == "locacao"), Decimal("0.00"))
    total_licenca = sum((l["valor"] for l in linhas if l["tipo_custo"] == "licenca"), Decimal("0.00"))
    total_patrimonio = sum((l["valor"] for l in linhas if l["tipo_custo"] == "patrimonio"), Decimal("0.00"))
    total_mensal = (total_locacao + total_licenca).quantize(Decimal("0.01"))
    total_geral = (total_mensal + total_patrimonio).quantize(Decimal("0.01"))
    total_divergencias = sum(r["divergencias"] for r in resumo_cc)

    qtd_cc_pmb = len([r for r in resumo_cc if r["cc"].pmb_efetivo == SimNaoChoices.SIM])
    qtd_cc_fazenda = len(resumo_cc) - qtd_cc_pmb

    ccs_opts = list(CentroCusto.objects.order_by("numero"))
    for cc in ccs_opts:
        cc.pmb_efetivo = _pmb_efetivo(cc)

    return {
        # filtros ativos (para pré-marcar os selects e montar a querystring do export)
        "subtipo_ids": subtipo_ids,
        "cc_ids": cc_ids,
        "fornecedor_ids": fornecedor_ids,
        "pmb_filtro": pmb_filtro,
        "tipos_sel": tipos_sel,
        "status_sel": status_sel,
        "querystring": request.GET.urlencode(),

        # opções para os filtros
        "subtipos_opts": Subtipo.objects.select_related("categoria").order_by("categoria__nome", "nome"),
        "ccs_opts": ccs_opts,
        "fornecedores_opts": Fornecedor.objects.order_by("nome"),
        "status_opts": StatusItemChoices.choices,

        # dados
        "linhas": linhas,
        "grupos": grupos,
        "resumo_cc": resumo_cc,

        # KPIs
        "kpi_total_linhas": len(linhas),
        "kpi_total_cc": len(resumo_cc),
        "kpi_total_locacao": total_locacao.quantize(Decimal("0.01")),
        "kpi_total_licenca": total_licenca.quantize(Decimal("0.01")),
        "kpi_total_patrimonio": total_patrimonio.quantize(Decimal("0.01")),
        "kpi_total_mensal": total_mensal,
        "kpi_total_geral": total_geral,
        "kpi_divergencias_pmb": total_divergencias,
        "kpi_cc_pmb": qtd_cc_pmb,
        "kpi_cc_fazenda": qtd_cc_fazenda,

        "gerado_em": timezone.localtime(),
    }
