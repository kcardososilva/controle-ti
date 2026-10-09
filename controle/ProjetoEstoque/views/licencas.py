from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO

from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.http import HttpResponse, JsonResponse
from django.template.loader import render_to_string
from django.db.models import Q, Count, Sum, F
from django.db.models.functions import Coalesce
from django.core.paginator import Paginator
from django.utils import timezone
from django.db import transaction
from django.views.decorators.http import require_POST
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

from ..models import (
    SimNaoChoices,
    Licenca, LicencaLote, MovimentacaoLicenca,
    TipoMovLicencaChoices, PeriodicidadeChoices,
    Usuario, CentroCusto, Fornecedor,
)
from ..forms import LicencaForm, MovimentacaoLicencaForm, LicencaLoteForm


def _normalizar_custos_lote(lote):
    """
    Rateio de custo do lote, como dicionário — adaptador fino para o
    `licenca_service.custos_do_lote`, que é a função única de cálculo.

    O corpo antigo vivia aqui e tinha uma ORDEM DE ARREDONDAMENTO diferente da
    do `MovimentacaoLicencaForm`: esta arredondava o custo mensal do lote antes
    de dividir pelos assentos, aquela dividia pelos assentos antes de converter
    para mensal. Duas contas sobre o mesmo lote fechavam com até um centavo de
    diferença, e o valor gravado em `MovimentacaoLicenca.valor_unitario` não
    batia com o custo mensal exibido na mesma linha da tela — divergência que
    aparece como centavo teimoso em relatório e não tem como ser explicada.

    Continua existindo como função porque o Excel e o detalhe consomem
    `dict`; o cálculo, não.
    """
    from services.licenca_service import custos_do_lote

    custo = custos_do_lote(lote)
    return {
        "unitario_ciclo": custo.unitario_ciclo,
        "mensal_unit": custo.mensal_unit,
        "anual_unit": custo.anual_unit,
        "mensal_total": custo.mensal_total,
        "anual_total": custo.anual_total,
    }


@login_required
def licenca_list(request):
    """
    Catálogo de licenças — os números vêm do RAZÃO, não do contador.

    Antes esta view anotava `estoque_real=Sum("lotes__quantidade_disponivel")`,
    ou seja, somava o CACHE. O cabeçalho do detalhe e a atribuição rápida já
    liam o razão, então o catálogo e o detalhe mostravam "assentos livres"
    diferentes para a mesma licença, a um clique de distância, e nenhum dos
    dois parecia errado. Hoje os dois saem de `licenca_service.catalogo()`;
    o contador continua lido para a linha poder APONTAR a divergência
    (`linha.divergente`), e não para passar por verdade.

    Trocar a anotação de SQL por apuração em Python é escolha consciente: são
    33 licenças e 47 lotes, o razão inteiro já é varrido pelo badge de saúde em
    toda tela do módulo, e a resposta certa num acervo deste tamanho vale mais
    do que a consulta mais barata com o número errado.
    """
    from services import licenca_service as ls

    q = (request.GET.get("q") or "").strip()
    fornecedor_id = (request.GET.get("fornecedor") or "").strip()
    pmb_filter = (request.GET.get("pmb") or "").strip()
    status_estoque = (request.GET.get("status") or "").strip()
    ordem = (request.GET.get("ordem") or "nome").strip()

    base = Licenca.objects.all()
    if q:
        base = base.filter(
            Q(nome__icontains=q)
            | Q(observacao__icontains=q)
            | Q(fornecedor__nome__icontains=q)
        )
    if fornecedor_id.isdigit():
        base = base.filter(fornecedor_id=fornecedor_id)
    if pmb_filter:
        base = base.filter(pmb=pmb_filter)

    linhas = ls.catalogo(base)

    # Os filtros de ESTADO agem sobre o apurado, porque "esgotada",
    # "divergente" e "ociosa" não existem como coluna — são leitura do razão.
    filtros_estado = {
        "com_estoque": lambda l: l.livre > 0,
        "sem_estoque": lambda l: l.esgotada,
        "sem_lote": lambda l: l.sem_lote,
        "divergente": lambda l: l.divergente,
        # Mais de um quarto dos assentos pagos e parados: é onde há dinheiro
        # para recuperar sem negociar preço com ninguém.
        "ociosa": lambda l: l.total > 0 and l.livre * 4 >= l.total,
    }
    if status_estoque in filtros_estado:
        linhas = [l for l in linhas if filtros_estado[status_estoque](l)]

    ordenacoes = {
        "nome": lambda l: ((l.licenca.nome or "").casefold(),),
        "custo": lambda l: (-l.mensal,),
        "desperdicio": lambda l: (-l.desperdicio_mensal,),
        "uso": lambda l: (-l.pct_uso,),
        "assentos": lambda l: (-l.total,),
        # Vencimento primeiro; licença sem data vai para o fim, não para o topo
        # (ordenar None como zero colocaria "não sei" acima de "vence amanhã").
        "vencimento": lambda l: (
            l.dias_vencimento is None,
            l.dias_vencimento if l.dias_vencimento is not None else 0,
        ),
    }
    if ordem in ordenacoes:
        linhas.sort(key=ordenacoes[ordem])

    resumo = ls.resumo_acervo(linhas)

    try:
        per_page = max(10, min(int(request.GET.get("pp", 20)), 100))
    except (TypeError, ValueError):
        per_page = 20

    paginator = Paginator(linhas, per_page)
    page_obj = paginator.get_page(request.GET.get("page", 1))

    get_copy = request.GET.copy()
    get_copy.pop("page", None)
    qs_keep = get_copy.urlencode()

    context = {
        "page_obj": page_obj,
        "qs_keep": qs_keep,
        "total_registros": len(linhas),
        "resumo": resumo,
        "filter_q": q,
        "filter_fornecedor": int(fornecedor_id) if fornecedor_id.isdigit() else "",
        "filter_pmb": pmb_filter,
        "filter_status": status_estoque,
        "filter_ordem": ordem,
        "per_page": per_page,
        "tem_filtro": bool(q or fornecedor_id or pmb_filter or status_estoque),
        "opt_fornecedores": Fornecedor.objects.values("id", "nome").order_by("nome"),
        "opt_pmb": SimNaoChoices.choices,
        "opt_status": [
            ("com_estoque", "Com assento livre"),
            ("sem_estoque", "Esgotada"),
            ("ociosa", "Ociosa (25%+ parada)"),
            ("divergente", "Contador divergente"),
            ("sem_lote", "Sem lote cadastrado"),
        ],
        "opt_ordem": [
            ("nome", "Nome (A–Z)"),
            ("custo", "Maior custo mensal"),
            ("desperdicio", "Maior desperdício"),
            ("uso", "Maior ocupação"),
            ("assentos", "Mais assentos"),
            ("vencimento", "Vence primeiro"),
        ],
    }

    # Alternador lista/galeria por AJAX: só a fatia muda, e a paginação é a
    # mesma partial nas duas visões — duas cópias divergiriam, e o sintoma
    # seria perder o filtro ao virar a página na galeria.
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        view_mode = request.GET.get("view", "list")
        data = {
            "count": context["total_registros"],
            "pagination": render_to_string(
                "front/licencas/_lic_pagination.html", context, request=request
            ),
        }
        chave = "gallery" if view_mode == "gallery" else "tbody"
        parcial = (
            "front/licencas/_lic_gallery.html" if view_mode == "gallery"
            else "front/licencas/_lic_rows.html"
        )
        data[chave] = render_to_string(parcial, context, request=request)
        return JsonResponse(data)

    return render(request, "front/licencas/licenca_list.html", context)


_LIMITE_BUSCA_COLAB = 30


@login_required
def licenca_colaboradores_api(request):
    """
    Busca remota de colaboradores para a atribuição rápida.

    Por que AJAX e não um `<select>` cheio: são 1.376 colaboradores ativos.
    Renderizá-los como `<option>` são ~80 KB de HTML em CADA abertura do
    detalhe da licença, que o navegador ainda tem de montar antes de pintar —
    e o filtro do Select2 local passaria a varrer um DOM gigante a cada tecla.
    Mesmo padrão já usado na assinatura de termos e na movimentação em lote.

    Exclui quem JÁ tem a licença (apurado no razão): oferecer essa pessoa só
    para o service recusar depois é mandar o operador descobrir o erro no
    caminho mais longo.
    """
    from services import licenca_service as ls

    termo = (request.GET.get("q") or "").strip()
    licenca = Licenca.objects.filter(pk=request.GET.get("licenca") or 0).first()

    qs = Usuario.objects.filter(status="ativo").select_related("centro_custo")

    if licenca is not None:
        qs = qs.exclude(pk__in=list(ls.alocacao_de(licenca).por_usuario.keys()))

    if termo:
        qs = qs.filter(Q(nome__icontains=termo) | Q(matricula__icontains=termo))

    return JsonResponse({"results": [
        {
            "id": colaborador.pk,
            "text": colaborador.nome,
            "matricula": colaborador.matricula or "",
            "centro_custo": getattr(colaborador.centro_custo, "departamento", "") or "",
        }
        for colaborador in qs.order_by("nome")[:_LIMITE_BUSCA_COLAB]
    ]})


@login_required
def licenca_saude(request):
    """
    Saúde do acervo de licenças — o que custa dinheiro ou está mentindo.

    Existe porque o catálogo respondia "quantas licenças existem" e nunca
    "o que está errado". Três perguntas que ninguém fazia até doer:

      • **assento no nome de quem já saiu** — a cascata de desligamento cobre
        quem foi desligado PELO fluxo; quem foi marcado como desligado direto
        na ficha deixava o assento preso e INVISÍVEL, porque nenhuma tela
        cruzava "assento ativo" com "status do colaborador". É despesa
        recorrente por licença de quem não trabalha mais aqui;
      • **contador divergente do razão** — `quantidade_disponivel` é cache;
        quando ele infla, a tela oferece assento que não existe;
      • **pool esgotado** — a próxima atribuição vai falhar na frente do
        operador, e dá para comprar antes.

    Tudo apurado pelo `licenca_service`, que é a fonte única. Esta view só
    escolhe o que mostrar.
    """
    from services import licenca_service as ls

    resumo = ls.resumo_saude()

    return render(request, "front/licencas/licenca_saude.html", {
        "resumo": resumo,
        "titulo": "Saúde das licenças",
        "subtitulo": (
            "Assentos presos em colaborador desligado, contadores fora do razão "
            "e pools sem assento livre."
        ),
    })


@login_required
@require_POST
def licenca_devolver_orfao(request):
    """
    Devolve ao estoque um assento preso em colaborador não ativo.

    Um POST por assento (licença + colaborador no corpo), e não um "devolver
    todos" de uma vez: cada devolução é uma decisão contábil — ela estorna
    custo de um centro de custo específico — e um botão único que mexe em
    dezenas de assentos de uma vez não dá ao operador como conferir o que vai
    acontecer antes de acontecer.
    """
    from services import licenca_service as ls

    licenca = get_object_or_404(Licenca, pk=request.POST.get("licenca") or 0)
    usuario = get_object_or_404(Usuario, pk=request.POST.get("usuario") or 0)

    try:
        ls.devolver(
            licenca=licenca, usuario=usuario, user=request.user,
            observacao=(
                f"Recuperação de assento preso em colaborador não ativo "
                f"({usuario.get_status_display() if hasattr(usuario, 'get_status_display') else usuario.status}). "
                f"Operador: {request.user}."
            ),
        )
    except ls.LicencaError as erro:
        messages.error(request, str(erro))
    else:
        messages.success(
            request,
            f"Assento de “{licenca.nome}” recuperado de {usuario.nome} "
            "e devolvido ao estoque.",
        )

    return redirect("licenca_saude")


@login_required
@require_POST
def licenca_reconciliar_lote(request, pk):
    """
    Alinha o contador de saldo de UM lote ao razão.

    Reparo explícito, nunca efeito colateral de leitura: o painel de saúde
    apenas APONTA a divergência (`conferir_consistencia` só lê), e corrigir é
    uma ação que o operador dispara e que fica no log. Uma tela que
    consertasse ao abrir apagaria a diferença entre "nunca divergiu" e
    "divergiu e alguém consertou sem registrar".

    Nenhuma movimentação é tocada — o razão é a verdade; o que muda é o cache.
    """
    from services import licenca_service as ls

    lote = get_object_or_404(LicencaLote.objects.select_related("licenca"), pk=pk)
    resultado = ls.reconciliar_lote(
        lote, user=request.user,
        observacao=f"Reconciliação manual pelo painel de saúde. Operador: {request.user}.",
    )

    if resultado["mudou"]:
        messages.success(
            request,
            f"Lote #{lote.pk} ({lote.licenca.nome}): saldo corrigido de "
            f"{resultado['antes']} para {resultado['depois']}, conforme as movimentações.",
        )
    else:
        messages.info(request, f"Lote #{lote.pk} já estava de acordo com o razão.")

    return redirect(request.POST.get("next") or "licenca_saude")


@login_required
@require_POST
def licenca_atribuir_rapido(request, pk):
    """
    Atribui um assento desta licença direto da tela de detalhe.

    Atalho para o caso mais comum do dia a dia — "dá o Office para essa
    pessoa" —, que antes exigia sair do detalhe, abrir a tela de movimentação,
    reencontrar a licença num select de 33 opções e voltar.
    """
    from services import licenca_service as ls

    licenca = get_object_or_404(Licenca, pk=pk)
    usuario = Usuario.objects.filter(pk=request.POST.get("usuario") or 0).first()

    if usuario is None:
        messages.error(request, "Selecione o colaborador que vai receber a licença.")
        return redirect("licenca_detail", pk=licenca.pk)

    lote = LicencaLote.objects.filter(
        pk=request.POST.get("lote") or 0, licenca=licenca,
    ).first()

    try:
        ls.atribuir(
            licenca=licenca, usuario=usuario, lote=lote, user=request.user,
            observacao=f"Atribuição rápida pelo detalhe da licença. Operador: {request.user}.",
        )
    except ls.LicencaError as erro:
        messages.error(request, str(erro))
    else:
        messages.success(
            request, f"Licença “{licenca.nome}” atribuída a {usuario.nome}.",
        )

    return redirect("licenca_detail", pk=licenca.pk)


@login_required
def licenca_form(request, pk=None):
    """
    View simplificada para Cadastro/Edição de Licença (4 campos).
    """
    # Se houver PK, é edição. Senão, criação.
    obj = get_object_or_404(Licenca, pk=pk) if pk else None

    if request.method == "POST":
        form = LicencaForm(request.POST, instance=obj)
        if form.is_valid():
            try:
                licenca = form.save(commit=False)
                
                # Preenche auditoria
                if not obj:
                    licenca.criado_por = request.user
                licenca.atualizado_por = request.user
                
                licenca.save()
                
                verb = "editada" if obj else "criada"
                messages.success(request, f"Licença '{licenca.nome}' {verb} com sucesso!")
                return redirect("licenca_list")
                
            except Exception as e:
                messages.error(request, f"Erro crítico ao salvar: {e}")
        else:
            messages.error(request, "Verifique os campos obrigatórios.")
    else:
        form = LicencaForm(instance=obj)

    return render(request, "front/licencas/licenca_form.html", {
        "form": form,
        "obj": obj
    })


# --- HELPER: Calcula Alocação por Centro de Custo ---
def _get_dados_cc(licenca):
    """
    Assentos ativos agrupados por centro de custo.

    O estado ativo vem do `licenca_service.alocacao_de` — fonte única, com a
    ordenação `("created_at", "id")`. Este helper ordenava só por
    `created_at`: sem o `id` como desempate, duas movimentações gravadas no
    mesmo instante (o que acontece de verdade na remoção em massa e na cascata
    de desligamento) ficavam em ordem indefinida, e "atribuiu e devolveu" podia
    ser lido como "devolveu e atribuiu" — invertendo o estado e contando um
    assento que não existe.

    O valor somado é o `valor_unitario` GRAVADO na atribuição, ou seja o custo
    mensal cobrado na época. É de propósito: este bloco responde "quanto cada
    centro de custo está pagando pelo que pegou", e recalcular pelo preço
    atual do lote reescreveria o passado a cada renovação de contrato.
    """
    from services.licenca_service import alocacao_de

    cc_stats = defaultdict(lambda: {'nome': 'Não Definido', 'qtd': 0, 'total': Decimal(0)})

    for mov in alocacao_de(licenca).por_usuario.values():
        cc = mov.centro_custo_destino
        cc_id = cc.id if cc else 'na'
        cc_name = cc.departamento if cc else 'Sem Centro de Custo'

        cc_stats[cc_id]['nome'] = cc_name
        cc_stats[cc_id]['qtd'] += 1
        cc_stats[cc_id]['total'] += (mov.valor_unitario or Decimal(0))

    return sorted(cc_stats.values(), key=lambda x: x['total'], reverse=True)

@login_required
def licenca_detail(request, pk):
    """
    Ficha da licença: custo, ocupação, lotes, renovação e razão completo.

    **Um número por pergunta.** Antes esta view calculava os KPIs do contador
    (`quantidade_disponivel`) e, mais abaixo, recalculava os mesmos assentos
    pelo razão para a atribuição rápida — então a aba "Lotes" mostrava o saldo
    do cache enquanto o cabeçalho mostrava o do razão, na MESMA tela. Hoje
    tudo sai de `licenca_service.catalogo()`, que já anota cada lote.

    A divergência do contador era calculada (`kpi.divergente`) e **nunca
    exibida** — o comentário dizia que existia "para a tela poder avisar", e o
    aviso não tinha sido construído. Agora ela vira faixa no topo, com link
    para reconciliar.
    """
    from services import licenca_service as ls

    licenca = get_object_or_404(
        Licenca.objects.select_related("fornecedor", "centro_custo"), pk=pk
    )

    linha = ls.catalogo(Licenca.objects.filter(pk=licenca.pk))[0]
    lotes = linha.lotes
    alocacao = ls.alocacao_de(licenca)

    lotes_com_saldo = [l for l in lotes if l.saldo_real > 0]

    # Razão completo, paginado. O custo exibido é o do lote de origem na época
    # da movimentação, não o preço atual: recalcular pelo preço de hoje
    # reescreveria o passado a cada renovação de contrato.
    movimentacoes_qs = (
        MovimentacaoLicenca.objects.filter(licenca=licenca)
        .select_related("usuario", "usuario__centro_custo", "lote",
                        "centro_custo_destino", "criado_por")
        .order_by("-created_at", "-id")
    )

    tipo_mov = (request.GET.get("tipo") or "").strip()
    if tipo_mov in dict(TipoMovLicencaChoices.choices):
        movimentacoes_qs = movimentacoes_qs.filter(tipo=tipo_mov)

    busca_mov = (request.GET.get("hq") or "").strip()
    if busca_mov:
        movimentacoes_qs = movimentacoes_qs.filter(
            Q(usuario__nome__icontains=busca_mov)
            | Q(usuario__matricula__icontains=busca_mov)
            | Q(observacao__icontains=busca_mov)
        )

    movimentacoes_page = Paginator(movimentacoes_qs, 15).get_page(request.GET.get("page"))
    for mov in movimentacoes_page:
        custo_mov = ls.custos_do_lote(mov.lote) if mov.lote else ls.CustoLote.zero()
        mov.custo_mensal_exibicao = custo_mov.mensal_unit
        mov.custo_anual_exibicao = custo_mov.anual_unit

    # Quem detém a licença AGORA — a pergunta mais feita na ficha e a única que
    # o razão respondia só se a pessoa lesse 1.500 linhas de histórico de trás
    # para frente.
    detentores = []
    for mov in alocacao.por_usuario.values():
        detentores.append({
            "usuario": mov.usuario,
            "movimentacao": mov,
            "lote": mov.lote,
            "desde": mov.created_at,
            "mensal": Decimal(mov.valor_unitario or 0),
            "centro_custo": mov.centro_custo_destino,
            "inativo": mov.usuario is not None
            and (mov.usuario.status or "").lower() != "ativo",
        })
    detentores.sort(key=lambda d: (d["usuario"].nome or "").casefold() if d["usuario"] else "")

    qs_keep_hist = request.GET.copy()
    qs_keep_hist.pop("page", None)

    context = {
        "obj": licenca,
        "linha": linha,
        "kpi": {
            "total": linha.total,
            "disponivel": linha.livre,
            "em_uso": linha.em_uso,
            "pct_uso": linha.pct_uso,
            "investimento_total": linha.anual,
            "investido_ciclo": linha.investido_ciclo,
            "gasto_mensal": linha.mensal_em_uso,
            "gasto_anual": (linha.mensal_em_uso * Decimal("12")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            ),
            "desperdicio_mensal": linha.desperdicio_mensal,
            "saldo_contador": linha.saldo_contador,
            "divergente": linha.divergente,
            "ciclo_estimado": linha.ciclo_estimado,
            "vencimento": linha.vencimento,
            "dias_vencimento": linha.dias_vencimento,
            "faixa_vencimento": linha.faixa_vencimento,
        },
        "lotes": lotes,
        "lotes_com_saldo": lotes_com_saldo,
        "lotes_divergentes": [l for l in lotes if l.divergente],
        "movimentacoes": movimentacoes_page,
        "detentores": detentores,
        "cc_list": _get_dados_cc(licenca),
        "filter_tipo_mov": tipo_mov,
        "filter_hq": busca_mov,
        "qs_keep_hist": qs_keep_hist.urlencode(),
        "tipos_mov": TipoMovLicencaChoices.choices,
        "config_atribuir": {
            "urlColaboradores": reverse("licenca_colaboradores_api"),
            "licenca": licenca.pk,
            "jaComLicenca": list(alocacao.por_usuario.keys()),
        },
    }

    return render(request, "front/licencas/licenca_detail.html", context)


@login_required
@permission_required("ProjetoEstoque.view_licenca", raise_exception=True)
def licenca_export_excel(request, pk):
    licenca = get_object_or_404(
        Licenca.objects.select_related("fornecedor", "centro_custo"),
        pk=pk
    )

    lotes_qs = (
        LicencaLote.objects.filter(licenca=licenca)
        .select_related("fornecedor", "centro_custo")
        .order_by("-data_compra", "-id")
    )

    movimentacoes_qs = (
        MovimentacaoLicenca.objects.filter(licenca=licenca)
        .select_related("usuario", "lote", "centro_custo_destino", "criado_por")
        .order_by("-created_at")
    )

    cc_list = _get_dados_cc(licenca)
    lotes = list(lotes_qs)
    movs = list(movimentacoes_qs)

    # =========================================================================
    # KPIs consolidados (mesma base do detalhe da licença)
    # =========================================================================
    qtd_total = sum(int(l.quantidade_total or 0) for l in lotes)
    qtd_disp = sum(int(l.quantidade_disponivel or 0) for l in lotes)
    qtd_em_uso = max(0, qtd_total - qtd_disp)
    pct_uso = int((qtd_em_uso / qtd_total) * 100) if qtd_total > 0 else 0

    investimento_total = Decimal("0.00")
    gasto_mensal = Decimal("0.00")
    gasto_anual = Decimal("0.00")
    for l in lotes:
        custos = _normalizar_custos_lote(l)
        investimento_total += custos["anual_total"]
        em_uso = max(0, int(l.quantidade_total or 0) - int(l.quantidade_disponivel or 0))
        if em_uso > 0:
            gasto_mensal += (custos["mensal_unit"] * Decimal(em_uso)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            gasto_anual += (custos["anual_unit"] * Decimal(em_uso)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    colaboradores_ativos = len({
        m.usuario_id for m in movs if m.tipo == "atribuicao" and m.usuario_id
    } - {
        m.usuario_id for m in movs if m.tipo != "atribuicao" and m.usuario_id
    })

    # =========================================================================
    # PALETA / ESTILOS PROFISSIONAIS (tema oficial da empresa)
    # =========================================================================
    from services.excel_theme import (
        BORDA as border, FONT_TITULO as f_title, FONT_SUBTITULO as f_sub,
        FONT_HEADER as f_header, FONT_CELL_BOLD as f_bold, FONT_CELL as f_cell,
        FILL_TITULO as fill_title, FILL_SUBTITULO as fill_sub,
        FILL_HEADER as fill_header, FILL_ZEBRA as fill_zebra,
        ALIGN_CENTER as a_center, ALIGN_LEFT as a_left, ALIGN_RIGHT as a_right,
        ALIGN_LEFT_IND as a_left_ind, MONEY_FMT as BRL,
        AZUL_ABUNDANCIA, AZUL_VASTIDAO, VERDE_VITAL, MARROM_CAFE, LARANJA_SOLAR,
    )

    GREEN = "1E8E3E"
    RED = "D93025"
    GRAY = "6B7280"

    def faixa_titulo(ws, ncols, titulo, subtitulo):
        last = get_column_letter(ncols)
        ws.merge_cells(f"A1:{last}1")
        c = ws["A1"]
        c.value = titulo
        c.font = f_title
        c.fill = fill_title
        c.alignment = a_left_ind
        ws.row_dimensions[1].height = 34
        ws.merge_cells(f"A2:{last}2")
        c2 = ws["A2"]
        c2.value = subtitulo
        c2.font = f_sub
        c2.fill = fill_sub
        c2.alignment = a_left_ind
        ws.row_dimensions[2].height = 18
        ws.sheet_view.showGridLines = False

    def cabecalho_tabela(ws, row, headers, center_cols=()):
        for ci, h in enumerate(headers, 1):
            c = ws.cell(row=row, column=ci, value=h)
            c.fill = fill_header
            c.font = f_header
            c.border = border
            c.alignment = a_center if ci in center_cols else a_left
        ws.row_dimensions[row].height = 22

    gerado = timezone.localtime().strftime("%d/%m/%Y às %H:%M")

    wb = Workbook()

    # =========================================================================
    # ABA 1 — RESUMO (capa executiva com KPIs)
    # =========================================================================
    wsr = wb.active
    wsr.title = "Resumo"
    faixa_titulo(wsr, 6, f"LICENÇA — {licenca.nome.upper()}",
                 f"Santa Colomba Agropecuária  ·  Relatório gerado em {gerado}")

    # Bloco de informações
    info = [
        ("Fornecedor", licenca.fornecedor.nome if licenca.fornecedor else "—"),
        ("Centro de Custo padrão", licenca.centro_custo.departamento if licenca.centro_custo else "—"),
        ("Nº de lotes", len(lotes)),
        ("Colaboradores com licença ativa", colaboradores_ativos),
    ]
    r = 4
    for label, val in info:
        cl = wsr.cell(row=r, column=1, value=label)
        cl.font = f_bold
        cl.alignment = a_left
        wsr.merge_cells(start_row=r, start_column=2, end_row=r, end_column=3)
        cv = wsr.cell(row=r, column=2, value=val)
        cv.font = f_cell
        cv.alignment = a_left
        r += 1
    r += 1

    # KPIs (cartões 2 linhas: rótulo + valor)
    kpis = [
        ("ASSENTOS TOTAIS", qtd_total, AZUL_ABUNDANCIA, None),
        ("EM USO", qtd_em_uso, VERDE_VITAL, None),
        ("DISPONÍVEIS", qtd_disp, AZUL_VASTIDAO, None),
        ("OCUPAÇÃO", pct_uso / 100, LARANJA_SOLAR, "0%"),
        ("INVESTIMENTO TOTAL", float(investimento_total), MARROM_CAFE, BRL),
        ("GASTO MENSAL", float(gasto_mensal), AZUL_ABUNDANCIA, BRL),
    ]
    for idx, (lbl, val, color, fmt) in enumerate(kpis):
        col = idx + 1
        cl = wsr.cell(row=r, column=col, value=lbl)
        cl.font = Font(name="Calibri", size=8, bold=True, color="FFFFFF")
        cl.fill = PatternFill("solid", fgColor=color)
        cl.alignment = a_center
        cl.border = border
        cv = wsr.cell(row=r + 1, column=col, value=val)
        cv.font = Font(name="Calibri", size=16, bold=True, color=color)
        cv.fill = PatternFill("solid", fgColor="F4F6F9")
        cv.alignment = a_center
        cv.border = border
        if fmt:
            cv.number_format = fmt
    wsr.row_dimensions[r].height = 16
    wsr.row_dimensions[r + 1].height = 30
    for c in range(1, 7):
        wsr.column_dimensions[get_column_letter(c)].width = 20

    # =========================================================================
    # ABA 2 — USUÁRIOS E ALOCAÇÕES
    # =========================================================================
    ws1 = wb.create_sheet(title="Usuarios e Alocacoes")
    headers_ws1 = [
        "Ação", "Colaborador", "E-mail", "Matrícula", "Lote",
        "Periodicidade", "Centro de Custo Destino", "Nº CC",
        "Custo Mensal", "Custo Anual", "Responsável", "Data", "Hora",
    ]
    faixa_titulo(ws1, len(headers_ws1), f"USUÁRIOS E ALOCAÇÕES — {licenca.nome.upper()}",
                 f"{len(movs)} movimentação(ões) registrada(s)")
    hr = 4
    cabecalho_tabela(ws1, hr, headers_ws1, center_cols=(1, 5, 6, 8, 9, 10, 12, 13))
    row = hr + 1
    tot_mensal_mov = Decimal("0.00")
    tot_anual_mov = Decimal("0.00")
    for i, mov in enumerate(movs):
        mensal = anual = Decimal("0.00")
        if mov.lote:
            custos = _normalizar_custos_lote(mov.lote)
            mensal, anual = custos["mensal_unit"], custos["anual_unit"]
        is_saida = mov.tipo == "atribuicao"
        valores = [
            "Saída" if is_saida else "Devolução",
            mov.usuario.nome if mov.usuario else "—",
            (mov.usuario.email or "—") if mov.usuario else "—",
            (mov.usuario.matricula or "—") if mov.usuario else "—",
            f"#{mov.lote.pk}" if mov.lote else "—",
            mov.lote.get_periodicidade_display() if mov.lote else "—",
            mov.centro_custo_destino.departamento if mov.centro_custo_destino else "—",
            mov.centro_custo_destino.numero if mov.centro_custo_destino else "—",
            float(mensal), float(anual),
            mov.criado_por.username if mov.criado_por else "—",
            mov.created_at.strftime("%d/%m/%Y") if mov.created_at else "—",
            mov.created_at.strftime("%H:%M") if mov.created_at else "—",
        ]
        zebra = (i % 2 == 1)
        for ci, val in enumerate(valores, 1):
            c = ws1.cell(row=row, column=ci, value=val)
            c.border = border
            c.font = f_cell
            if ci == 1:  # badge de ação colorido
                c.fill = PatternFill("solid", fgColor=GREEN if is_saida else GRAY)
                c.font = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
                c.alignment = a_center
            elif ci in (9, 10):
                c.number_format = BRL
                c.alignment = a_right
                if zebra:
                    c.fill = fill_zebra
            elif ci in (5, 6, 8, 12, 13):
                c.alignment = a_center
                if zebra:
                    c.fill = fill_zebra
            else:
                c.alignment = a_left
                if zebra:
                    c.fill = fill_zebra
        if is_saida:
            tot_mensal_mov += mensal
            tot_anual_mov += anual
        row += 1
    # rodapé de totais
    if movs:
        ws1.merge_cells(start_row=row, start_column=1, end_row=row, end_column=8)
        ct = ws1.cell(row=row, column=1, value="TOTAL (saídas ativas)")
        ct.font = f_bold
        ct.alignment = a_right
        ct.fill = fill_sub
        cm = ws1.cell(row=row, column=9, value=float(tot_mensal_mov))
        ca = ws1.cell(row=row, column=10, value=float(tot_anual_mov))
        for cc_ in (cm, ca):
            cc_.font = f_bold
            cc_.number_format = BRL
            cc_.alignment = a_right
            cc_.fill = fill_sub
        for col in range(1, len(headers_ws1) + 1):
            ws1.cell(row=row, column=col).border = border
    widths_ws1 = [12, 28, 30, 14, 9, 15, 28, 10, 15, 15, 18, 12, 9]
    for i, w in enumerate(widths_ws1, 1):
        ws1.column_dimensions[get_column_letter(i)].width = w
    ws1.freeze_panes = f"A{hr + 1}"

    # =========================================================================
    # ABA 3 — CENTROS DE CUSTO
    # =========================================================================
    ws2 = wb.create_sheet(title="Centros de Custo")
    headers_ws2 = ["Departamento / Centro de Custo", "Qtd. Assentos", "Gasto Consolidado"]
    faixa_titulo(ws2, len(headers_ws2), f"CENTROS DE CUSTO — {licenca.nome.upper()}",
                 "Distribuição de assentos e custo por centro de custo")
    hr = 4
    cabecalho_tabela(ws2, hr, headers_ws2, center_cols=(2, 3))
    row = hr + 1
    total_assentos = 0
    total_gasto = Decimal("0.00")
    for i, cc in enumerate(cc_list):
        qtd = cc.get("qtd", 0) or 0
        total = Decimal(cc.get("total", 0) or 0)
        zebra = (i % 2 == 1)
        c1 = ws2.cell(row=row, column=1, value=cc.get("nome", "—"))
        c2 = ws2.cell(row=row, column=2, value=qtd)
        c3 = ws2.cell(row=row, column=3, value=float(total))
        c1.alignment, c2.alignment, c3.alignment = a_left, a_center, a_right
        c3.number_format = BRL
        for c in (c1, c2, c3):
            c.border = border
            c.font = f_cell
            if zebra:
                c.fill = fill_zebra
        total_assentos += qtd
        total_gasto += total
        row += 1
    c1 = ws2.cell(row=row, column=1, value="TOTAL")
    c2 = ws2.cell(row=row, column=2, value=total_assentos)
    c3 = ws2.cell(row=row, column=3, value=float(total_gasto))
    c1.alignment, c2.alignment, c3.alignment = a_left, a_center, a_right
    c3.number_format = BRL
    for c in (c1, c2, c3):
        c.font = f_bold
        c.border = border
        c.fill = fill_sub
    ws2.column_dimensions["A"].width = 40
    ws2.column_dimensions["B"].width = 16
    ws2.column_dimensions["C"].width = 22
    ws2.freeze_panes = f"A{hr + 1}"

    # =========================================================================
    # ABA 4 — LOTES
    # =========================================================================
    ws3 = wb.create_sheet(title="Lotes")
    headers_ws3 = [
        "Lote", "Data Compra", "Pedido / NF", "Periodicidade",
        "Qtd. Total", "Qtd. Disponível", "Custo Ciclo/Lic.",
        "Mensal/Lic.", "Anual/Lic.", "Mensal do Lote", "Anual do Lote",
    ]
    faixa_titulo(ws3, len(headers_ws3), f"LOTES — {licenca.nome.upper()}",
                 f"{len(lotes)} lote(s) · investimento anual total R$ {investimento_total:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."))
    hr = 4
    cabecalho_tabela(ws3, hr, headers_ws3, center_cols=(1, 4, 5, 6, 7, 8, 9, 10, 11))
    row = hr + 1
    for i, lote in enumerate(lotes):
        custos = _normalizar_custos_lote(lote)
        zebra = (i % 2 == 1)
        valores = [
            f"#{lote.pk}",
            lote.data_compra.strftime("%d/%m/%Y") if lote.data_compra else "—",
            lote.numero_pedido or "—",
            lote.get_periodicidade_display() if lote.periodicidade else "—",
            lote.quantidade_total or 0,
            lote.quantidade_disponivel or 0,
            float(custos["unitario_ciclo"]),
            float(custos["mensal_unit"]),
            float(custos["anual_unit"]),
            float(custos["mensal_total"]),
            float(custos["anual_total"]),
        ]
        for ci, val in enumerate(valores, 1):
            c = ws3.cell(row=row, column=ci, value=val)
            c.border = border
            c.font = f_cell
            if ci in (7, 8, 9, 10, 11):
                c.number_format = BRL
                c.alignment = a_right
            elif ci in (1, 4, 5, 6):
                c.alignment = a_center
            else:
                c.alignment = a_left
            if zebra and ci not in ():
                c.fill = fill_zebra
        row += 1
    widths_ws3 = [10, 14, 18, 16, 12, 14, 16, 15, 15, 16, 16]
    for i, w in enumerate(widths_ws3, 1):
        ws3.column_dimensions[get_column_letter(i)].width = w
    ws3.freeze_panes = f"A{hr + 1}"

    # =========================================================================
    # RESPOSTA
    # =========================================================================
    output = BytesIO()
    wb.save(output)
    output.seek(0)

    slug = licenca.nome.replace(" ", "_").replace("/", "-")
    nome_arquivo = f"licenca_{licenca.pk}_{slug}_{timezone.localtime():%Y%m%d}.xlsx"
    response = HttpResponse(
        output.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response["Content-Disposition"] = f'attachment; filename="{nome_arquivo}"'
    return response

# ============ MOVIMENTAÇÕES ============

def _mov_licenca_filtradas(request):
    """
    Razão filtrado — fonte única da tela de movimentações E da exportação.

    As duas respondiam a mesma pergunta com dois filtros diferentes: a tela
    tinha busca e tipo, a exportação não existia. Separar aqui garante que o
    CSV baixado seja exatamente o que está na tela, inclusive os filtros — um
    export que ignora o filtro aplicado é a forma mais discreta de entregar o
    número errado para uma auditoria.
    """
    from datetime import datetime

    q = (request.GET.get("q") or "").strip()
    tipo = (request.GET.get("tipo") or "").strip()
    licenca_id = (request.GET.get("licenca") or "").strip()
    cc_id = (request.GET.get("cc") or "").strip()
    lote_id = (request.GET.get("lote") or "").strip()
    inicio = (request.GET.get("inicio") or "").strip()
    fim_periodo = (request.GET.get("fim") or "").strip()

    qs = (
        MovimentacaoLicenca.objects
        .select_related(
            "licenca", "licenca__fornecedor", "usuario", "usuario__centro_custo",
            "lote", "centro_custo_destino", "criado_por",
        )
        # Desempate por `id` por obrigação, não por capricho (regra 44): duas
        # movimentações gravadas no mesmo instante — o que acontece de verdade
        # na remoção em massa e na cascata de desligamento — sairiam em ordem
        # indefinida, e "atribuiu e devolveu" podia ser lido ao contrário.
        .order_by("-created_at", "-id")
    )

    licenca_filtro = None
    if licenca_id.isdigit():
        licenca_filtro = Licenca.objects.filter(pk=licenca_id).first()
        if licenca_filtro:
            qs = qs.filter(licenca_id=licenca_filtro.pk)

    if q:
        qs = qs.filter(
            Q(licenca__nome__icontains=q)
            | Q(usuario__nome__icontains=q)
            | Q(usuario__matricula__icontains=q)
            | Q(observacao__icontains=q)
        )

    if tipo in dict(TipoMovLicencaChoices.choices):
        qs = qs.filter(tipo=tipo)

    if cc_id.isdigit():
        qs = qs.filter(centro_custo_destino_id=cc_id)

    if lote_id.isdigit():
        qs = qs.filter(lote_id=lote_id)

    def _dia(valor):
        try:
            return datetime.strptime(valor, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return None

    d_ini = _dia(inicio)
    d_fim = _dia(fim_periodo)
    if d_ini:
        qs = qs.filter(created_at__date__gte=d_ini)
    if d_fim:
        qs = qs.filter(created_at__date__lte=d_fim)

    return qs, {
        "q": q,
        "tipo": tipo,
        "licenca_filtro": licenca_filtro,
        "filter_cc": int(cc_id) if cc_id.isdigit() else "",
        "filter_lote": lote_id if lote_id.isdigit() else "",
        "filter_inicio": inicio,
        "filter_fim": fim_periodo,
        "tem_filtro": bool(q or tipo or cc_id or lote_id or inicio or fim_periodo),
    }


@login_required
def mov_licenca_list(request):
    """
    Razão de licenças: atribuições e devoluções, com filtro por período e CC.

    Antes a tela tinha busca e tipo e nada mais — para responder "o que o CC
    de TI movimentou em setembro" era preciso baixar nada e contar na mão,
    porque também não havia exportação. Agora o período, o centro de custo e o
    lote são filtros, e o CSV sai com os MESMOS filtros aplicados.

    **Semântica de cor unificada.** Atribuição aparecia em VERDE aqui e em
    VERMELHO ("Saída") no detalhe da licença e no formulário de lote — a mesma
    operação com dois significados visuais opostos em telas a um clique de
    distância. Hoje atribuição é saída de estoque (tom de alerta) e devolução é
    entrada (tom positivo) em todo o módulo.
    """
    qs, filtros = _mov_licenca_filtradas(request)

    try:
        per_page = max(10, min(int(request.GET.get("pp", 25)), 100))
    except (TypeError, ValueError):
        per_page = 25

    paginator = Paginator(qs, per_page)
    page_obj = paginator.get_page(request.GET.get("page", 1))

    # Totais do RECORTE, não do acervo: o número ao lado do filtro tem de
    # responder pelo que o filtro selecionou.
    agregado = qs.aggregate(
        atribuicoes=Count("pk", filter=Q(tipo=TipoMovLicencaChoices.ATRIBUICAO)),
        devolucoes=Count("pk", filter=Q(tipo=TipoMovLicencaChoices.DEVOLUCAO)),
        valor=Coalesce(Sum("valor_unitario"), Decimal("0.00")),
    )

    get_copy = request.GET.copy()
    get_copy.pop("page", None)

    context = {
        "movs": page_obj.object_list,
        "page_obj": page_obj,
        "paginator": paginator,
        "qs_keep": get_copy.urlencode(),
        "tipos": TipoMovLicencaChoices.choices,
        "total": paginator.count,
        "total_registros": paginator.count,
        "agregado": agregado,
        "per_page": per_page,
        "opt_cc": CentroCusto.objects.values("id", "numero", "departamento").order_by("numero"),
    }
    context.update(filtros)

    return render(request, "front/licencas/mov_licenca_list.html", context)


@login_required
def mov_licenca_export(request):
    """
    Razão filtrado em CSV — para conferência contábil e auditoria de contrato.

    CSV e não XLSX de propósito: é o razão cru, uma linha por fato, para abrir
    no Excel ou cruzar com a fatura do fornecedor. O detalhe da licença já tem
    o XLSX formatado com os KPIs; aqui o que se quer é o dado, sem enfeite.

    Separador `;` e BOM UTF-8 porque o Excel em pt-BR abre `,` como coluna
    única e come os acentos sem o BOM — exportação que precisa de ajuste manual
    para abrir não é exportação.
    """
    import csv

    qs, _filtros = _mov_licenca_filtradas(request)

    resposta = HttpResponse(content_type="text/csv; charset=utf-8")
    resposta["Content-Disposition"] = (
        'attachment; filename="razao-licencas-%s.csv"'
        % timezone.localdate().strftime("%Y-%m-%d")
    )
    resposta.write("\ufeff")

    escritor = csv.writer(resposta, delimiter=";", lineterminator="\r\n")
    escritor.writerow([
        "Data", "Hora", "Tipo", "Licenca", "Fornecedor", "Colaborador",
        "Matricula", "Centro de custo", "Lote", "Custo mensal do assento (R$)",
        "Observacao", "Registrado por",
    ])

    for mov in qs.iterator(chunk_size=500):
        criado = timezone.localtime(mov.created_at) if mov.created_at else None
        escritor.writerow([
            criado.strftime("%d/%m/%Y") if criado else "",
            criado.strftime("%H:%M") if criado else "",
            mov.get_tipo_display(),
            mov.licenca.nome if mov.licenca else "",
            mov.licenca.fornecedor.nome if mov.licenca and mov.licenca.fornecedor else "",
            mov.usuario.nome if mov.usuario else "",
            (mov.usuario.matricula or "") if mov.usuario else "",
            mov.centro_custo_destino.departamento if mov.centro_custo_destino else "",
            ("#%s" % mov.lote_id) if mov.lote_id else "",
            # Decimal com vírgula: o Excel pt-BR lê ponto como texto e a coluna
            # deixa de somar.
            str(mov.valor_unitario or Decimal("0.00")).replace(".", ","),
            (mov.observacao or "").replace("\n", " ").replace("\r", " "),
            getattr(mov.criado_por, "username", "") or "",
        ])

    return resposta


def _lic_safe_next(request, url):
    """Valida que a URL de retorno é local/segura antes de redirecionar (evita open redirect)."""
    if url and url_has_allowed_host_and_scheme(
        url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return url
    return None


@login_required
def mov_licenca_form(request):
    initial = {}
    if "licenca" in request.GET: initial["licenca"] = request.GET.get("licenca")
    if "usuario" in request.GET: initial["usuario"] = request.GET.get("usuario")

    if request.method == "POST":
        # Retorno inteligente: volta para onde o usuário estava antes de abrir o
        # formulário (tela de origem capturada no GET via next/HTTP_REFERER).
        next_url = _lic_safe_next(request, request.POST.get("next_url")) or reverse("licenca_list")

        form = MovimentacaoLicencaForm(request.POST)
        if form.is_valid():
            try:
                mov = form.save(user=request.user)

                # Feedback detalhado
                lote_txt = f"Lote #{mov.lote.pk}" if mov.lote else "N/A"
                cc_txt = mov.centro_custo_destino.departamento if mov.centro_custo_destino else "N/A"

                messages.success(request, f"{mov.get_tipo_display()} realizada. Estoque: {lote_txt} | Custo: {cc_txt}")
                return redirect(next_url)
            except Exception as e:
                messages.error(request, f"Erro: {e}")
    else:
        form = MovimentacaoLicencaForm(initial=initial)
        next_url = (
            _lic_safe_next(request, request.GET.get("next"))
            or _lic_safe_next(request, request.META.get("HTTP_REFERER"))
            or reverse("licenca_list")
        )

    # Lotes oferecidos no seletor — saldo pelo RAZÃO, não pelo contador.
    #
    # A versão anterior filtrava `quantidade_disponivel__gt=0`, ou seja, o
    # CACHE. Com o contador inflado por divergência, o seletor oferecia um lote
    # sem assento livre e o POST recusava depois; com o contador deflacionado,
    # escondia um lote que tinha assento pago e disponível. Nos dois casos o
    # operador descobria pelo caminho mais longo.
    from services import licenca_service as ls

    lotes_dict = {}
    for linha in ls.catalogo():
        for lote in linha.lotes:
            if lote.saldo_real < 1:
                continue
            data = lote.data_compra.strftime("%d/%m/%Y") if lote.data_compra else "sem data"
            lotes_dict.setdefault(str(lote.licenca_id), []).append({
                "id": lote.pk,
                "text": "Lote #%s — %s livre(s) de %s · %s" % (
                    lote.pk, lote.saldo_real, lote.quantidade_total, data,
                ),
            })

    context = {
        "form": form,
        "lotes_json": lotes_dict,
        "pre_selected_lote": request.POST.get("lote_id_select") or "",
        "next_url": next_url,
    }
    return render(request, "front/licencas/mov_licenca_form.html", context)
# --- LISTA DE LOTES ---
@login_required
def licenca_lote_list(request):
    """
    Lotes de compra: saldo pelo razão, vencimento e custo do ciclo.

    Três correções em relação à versão anterior:

    **Paginação.** A view devolvia o queryset inteiro e o template chamava
    `{{ lotes.count }}` — um COUNT a mais por page-load e a tabela inteira
    montada sempre. São 47 lotes hoje; nada na tela a impedia de crescer.

    **Saldo do razão.** Mostrava `quantidade_disponivel` (cache). Agora mostra
    o apurado, com marca no lote divergente.

    **O botão Filtrar passou a existir de verdade.** Com busca ativa, o
    template SUBSTITUÍA "Filtrar" por "Limpar" — depois de buscar uma vez não
    havia como refinar a busca sem apagar tudo e começar de novo.
    """
    from services import licenca_service as ls

    q = (request.GET.get("q") or "").strip()
    licenca_id = (request.GET.get("licenca") or "").strip()
    estado = (request.GET.get("estado") or "").strip()
    vence = (request.GET.get("vence") or "").strip()

    licencas_qs = Licenca.objects.all()
    licenca_filtro = None
    if licenca_id.isdigit():
        licenca_filtro = Licenca.objects.filter(pk=licenca_id).first()
        if licenca_filtro:
            licencas_qs = licencas_qs.filter(pk=licenca_filtro.pk)

    # `catalogo` já anota saldo, custo e vencimento em cada lote — é a MESMA
    # apuração do catálogo e do detalhe, então as três telas não divergem.
    lotes = []
    for linha in ls.catalogo(licencas_qs):
        lotes.extend(linha.lotes)

    if q:
        termo = q.casefold()

        def _casa(lote):
            campos = (
                lote.licenca.nome if lote.licenca else "",
                lote.numero_pedido or "",
                lote.observacao or "",
                lote.fornecedor.nome if lote.fornecedor else "",
                "#%s" % lote.pk,
            )
            return any(termo in (campo or "").casefold() for campo in campos)

        lotes = [l for l in lotes if _casa(l)]

    filtros_estado = {
        "esgotado": lambda l: l.saldo_real == 0,
        "intacto": lambda l: l.em_uso_real == 0,
        "em_uso": lambda l: 0 < l.em_uso_real < int(l.quantidade_total or 0),
        "divergente": lambda l: l.divergente,
        "sem_custo": lambda l: not l.custo_ciclo,
    }
    if estado in filtros_estado:
        lotes = [l for l in lotes if filtros_estado[estado](l)]

    if vence == "vencido":
        lotes = [l for l in lotes if l.dias_vencimento is not None and l.dias_vencimento < 0]
    elif vence == "sem_data":
        lotes = [l for l in lotes if l.vencimento is None]
    elif vence.isdigit():
        limite = int(vence)
        lotes = [
            l for l in lotes
            if l.dias_vencimento is not None and l.dias_vencimento <= limite
        ]

    ordem = (request.GET.get("ordem") or "recentes").strip()
    ordenacoes = {
        "recentes": lambda l: (-l.pk,),
        "licenca": lambda l: ((l.licenca.nome or "").casefold() if l.licenca else "", -l.pk),
        # Lote sem data vai para o FIM: ordenar `None` como zero colocaria
        # "não sei quando vence" acima de "vence amanhã".
        "vencimento": lambda l: (
            l.dias_vencimento is None,
            l.dias_vencimento if l.dias_vencimento is not None else 0,
        ),
        "valor": lambda l: (-Decimal(l.custo_ciclo or 0),),
        "saldo": lambda l: (-l.saldo_real,),
    }
    lotes.sort(key=ordenacoes.get(ordem, ordenacoes["recentes"]))

    totais = {
        "lotes": len(lotes),
        "assentos": sum(int(l.quantidade_total or 0) for l in lotes),
        "livres": sum(l.saldo_real for l in lotes),
        "ciclo": sum((Decimal(l.custo_ciclo or 0) for l in lotes), Decimal("0.00")),
        "mensal": sum((l.custo.mensal_total for l in lotes), Decimal("0.00")),
        "divergentes": sum(1 for l in lotes if l.divergente),
    }

    try:
        per_page = max(10, min(int(request.GET.get("pp", 25)), 100))
    except (TypeError, ValueError):
        per_page = 25

    page_obj = Paginator(lotes, per_page).get_page(request.GET.get("page", 1))

    get_copy = request.GET.copy()
    get_copy.pop("page", None)

    return render(request, "front/licencas/licenca_lote_list.html", {
        "page_obj": page_obj,
        "lotes": page_obj.object_list,
        "qs_keep": get_copy.urlencode(),
        "total_registros": len(lotes),
        "totais": totais,
        "q": q,
        "licenca_filtro": licenca_filtro,
        "filter_estado": estado,
        "filter_vence": vence,
        "filter_ordem": ordem,
        "per_page": per_page,
        "tem_filtro": bool(q or estado or vence),
        "opt_estado": [
            ("intacto", "Intacto (nada em uso)"),
            ("em_uso", "Parcialmente em uso"),
            ("esgotado", "Esgotado"),
            ("divergente", "Contador divergente"),
            ("sem_custo", "Sem custo informado"),
        ],
        "opt_vence": [
            ("vencido", "Já vencido"),
            ("30", "Vence em 30 dias"),
            ("60", "Vence em 60 dias"),
            ("90", "Vence em 90 dias"),
            ("sem_data", "Sem data de vencimento"),
        ],
        "opt_ordem": [
            ("recentes", "Cadastro mais recente"),
            ("licenca", "Licença (A-Z)"),
            ("vencimento", "Vence primeiro"),
            ("valor", "Maior valor do ciclo"),
            ("saldo", "Mais assentos livres"),
        ],
    })


@login_required
def licenca_renovacoes(request):
    """
    Calendário de renovação — o que vence, quanto custa e quanto disso está parado.

    **O módulo não respondia isto.** Guardava `data_compra` e `periodicidade` e
    nunca cruzava as duas: a renovação chegava pela fatura do fornecedor, ou
    seja, a empresa descobria o vencimento DEPOIS de renovar, quando já não
    havia decisão a tomar. São ~R$ 3,5 mi/ano em contrato.

    Cada linha mostra o valor do CICLO (o que será cobrado) ao lado dos
    assentos ociosos daquele lote. É a única tela em que as duas informações
    aparecem juntas — e juntas elas mudam a pergunta de "renova?" para "renova
    quantos?". Hoje são 190 assentos pagos e parados, R$ 83 mil/mês.

    **Nada é escrito ao abrir.** A única escrita é a gravação de datas, que o
    operador dispara (`licenca_renovacao_datas`).
    """
    from services import licenca_service as ls

    try:
        dias = max(7, min(int(request.GET.get("dias", 90)), 730))
    except (TypeError, ValueError):
        dias = 90

    resumo = ls.resumo_renovacoes(dias=dias)

    return render(request, "front/licencas/licenca_renovacoes.html", {
        "resumo": resumo,
        "dias": dias,
        "opt_dias": [30, 60, 90, 180, 365],
        # Sem data de compra NEM de renovação o sistema não tem como vigiar o
        # lote. Hoje são todos os 47 (a base foi carregada sem data de compra),
        # e é por isso que a tela traz o preenchimento em lote em vez de só
        # informar que não sabe.
        "sem_data": resumo["sem_data"],
    })


@login_required
@require_POST
def licenca_renovacao_datas(request):
    """
    Grava data de compra / renovação de vários lotes de uma vez.

    Existe porque a tela de renovação nasceu **vazia**: nenhum dos 47 lotes
    tinha data de compra, então não havia o que derivar e a tela só sabia dizer
    que não vigiava nada. Mandar o TI abrir 47 formulários de lote, um a um,
    para destravar a tela é quase garantir que ela continue vazia.

    Só grava o que veio preenchido. Campo vazio é "não mexer", nunca "apagar":
    um formulário em lote que zera o que ficou de fora destrói dado por
    omissão, que é o jeito mais fácil de perder uma data que alguém já havia
    conferido.
    """
    from datetime import datetime

    def _data(valor):
        valor = (valor or "").strip()
        if not valor:
            return None
        try:
            return datetime.strptime(valor, "%Y-%m-%d").date()
        except ValueError:
            return None

    gravados = 0
    invalidos = 0

    with transaction.atomic():
        pks = {
            chave.split("_", 1)[1]
            for chave in request.POST
            if chave.startswith(("compra_", "renovacao_"))
            and chave.split("_", 1)[1].isdigit()
        }

        for lote in LicencaLote.objects.select_for_update().filter(pk__in=pks):
            bruto_compra = (request.POST.get("compra_%s" % lote.pk) or "").strip()
            bruto_renov = (request.POST.get("renovacao_%s" % lote.pk) or "").strip()

            compra = _data(bruto_compra)
            renovacao = _data(bruto_renov)

            # Texto preenchido que não virou data é erro do operador, não
            # ausência de dado — contar separado evita o "salvou" silencioso
            # que some com o que a pessoa acabou de digitar.
            if (bruto_compra and compra is None) or (bruto_renov and renovacao is None):
                invalidos += 1
                continue

            campos = []
            if compra is not None and compra != lote.data_compra:
                lote.data_compra = compra
                campos.append("data_compra")
            if renovacao is not None and renovacao != lote.data_renovacao:
                lote.data_renovacao = renovacao
                campos.append("data_renovacao")

            if campos:
                lote.atualizado_por = request.user
                lote.save(update_fields=campos + ["atualizado_por", "updated_at"])
                gravados += 1

    if gravados:
        messages.success(
            request,
            "%s lote(s) atualizado(s). O vencimento já aparece no calendário "
            "de renovação." % gravados,
        )
    if invalidos:
        messages.error(
            request,
            "%s lote(s) com data em formato inválido foram ignorados — use o "
            "seletor de data do campo." % invalidos,
        )
    if not gravados and not invalidos:
        messages.info(request, "Nenhuma data informada — nada foi alterado.")

    return redirect(
        _lic_safe_next(request, request.POST.get("next")) or "licenca_renovacoes"
    )


@login_required
@transaction.atomic
def licenca_lote_form(request, pk=None):
    """
    Cadastro/edição do lote de compra.

    **O saldo é DERIVADO, nunca digitado nem somado.** O cálculo antigo era:

        diff = lote.quantidade_total - obj.quantidade_total
        lote.quantidade_disponivel = obj.quantidade_disponivel + diff

    ...e `diff` era SEMPRE ZERO. `form.save(commit=False)` devolve a PRÓPRIA
    `instance`, então `lote is obj`: os dois lados da subtração eram o valor
    novo. Defeito reproduzido — comprar 5 assentos e editar o total de 10 para
    15 deixava o saldo em 9: o TI via "15 comprados" e só conseguia atribuir 1.
    Os 5 assentos pagos simplesmente não existiam para o sistema.

    Derivar do razão resolve sem depender de capturar o valor anterior.
    """
    from services import licenca_service as ls

    obj = get_object_or_404(
        LicencaLote.objects.select_related("licenca"), pk=pk
    ) if pk else None

    if request.method == "POST":
        form = LicencaLoteForm(request.POST, instance=obj)
        if form.is_valid():
            lote = form.save(commit=False)
            if not obj:
                lote.criado_por = request.user
            lote.atualizado_por = request.user
            lote.save()

            # Alinha o contador ao razão DEPOIS de gravar o total novo. Num
            # lote novo isso devolve o total inteiro (nada atribuído ainda); num
            # lote editado, o total menos o que o razão mostra em uso.
            saldo = ls.saldo_calculado(lote)
            if int(lote.quantidade_disponivel or 0) != saldo:
                lote.quantidade_disponivel = saldo
                lote.save(update_fields=["quantidade_disponivel", "updated_at"])

            messages.success(
                request,
                "Lote #%s %s. %s assento(s) livre(s)."
                % (lote.pk, "atualizado" if obj else "criado", saldo),
            )

            destino = _lic_safe_next(request, request.POST.get("next"))
            if destino:
                return redirect(destino)
            if lote.licenca_id:
                return redirect("licenca_detail", pk=lote.licenca_id)
            return redirect("licenca_lote_list")
        else:
            messages.error(request, "Verifique os erros no formulário abaixo.")
    else:
        initial = {}
        licenca_id = (request.GET.get("licenca") or "").strip()
        if not obj and licenca_id.isdigit():
            initial["licenca"] = licenca_id
        form = LicencaLoteForm(instance=obj, initial=initial)

    situacao = None
    movimentacoes_lote = []
    if obj:
        alocacao = ls.alocacao_de(obj.licenca)
        em_uso = alocacao.por_lote.get(obj.pk, 0)
        total = int(obj.quantidade_total or 0)
        livre = max(0, total - em_uso)
        contador = int(obj.quantidade_disponivel or 0)

        situacao = {
            "total": total,
            "em_uso": em_uso,
            "livre": livre,
            "saldo_contador": contador,
            "divergente": contador != livre,
            "custo": ls.custos_do_lote(obj),
        }
        # Vencimento calculado, para o campo de renovação poder MOSTRAR o que o
        # sistema deduziria se o operador deixar em branco — em vez de ele ter
        # de adivinhar o que "deixe em branco para calcular" vai produzir.
        obj.vencimento_calculado = ls.vencimento_de(obj)

        movimentacoes_lote = list(
            MovimentacaoLicenca.objects.filter(lote=obj)
            .select_related("usuario", "centro_custo_destino", "criado_por")
            .order_by("-created_at", "-id")
        )

    return render(request, "front/licencas/licenca_lote_form.html", {
        "form": form,
        "obj": obj,
        "situacao": situacao,
        "movimentacoes_lote": movimentacoes_lote,
        # Divisores do ciclo entregues ao JS da prévia. A tabela autoritativa é
        # a do service; uma segunda cópia escrita no template divergiria dela
        # na próxima periodicidade nova — que é exatamente o defeito que fazia
        # um lote trienal publicar custo mensal 36x maior que o real.
        "meses_por_ciclo": {k: int(v) for k, v in ls._MESES_POR_CICLO.items()},
    })





### exportações 

