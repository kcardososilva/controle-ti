"""
Planta de Rede — o desenho da infraestrutura SOBRE O MAPA REAL.

A bancada (`rede_mapa.py`) desenha o diagrama: quem liga em quem, em que
porta. Esta tela desenha o TERRENO: onde o rack está, por onde a vala de
fibra passa, quantos metros tem o lance. São dois desenhos porque são duas
perguntas, e juntá-los num só daria um mapa que não serve para nenhuma das
duas — diagrama de rede não tem escala, e planta de terreno não tem camada.

O que as duas telas compartilham e por que:

  • o GRAFO (`montar_grafo`) e as PORTAS, que são os mesmos fatos;
  • a ROTA (`RedePorta.rota`), que é o elo: o enlace do diagrama aponta para
    a vala da planta, e é isso que permite responder "quem cai se romper".

── O fundo do mapa ─────────────────────────────────────────────────────────
Os tiles vêm de `KQMapa.CAMADAS` (`front/quiosque/_kq_mapa_js.html`), que é
a fonte única das telas de mapa deste sistema — ver Architecture Rule 15.
Não é reaproveitamento por economia: `tile.openstreetmap.org` responde
"403 Access blocked" ao IP desta empresa com status 200 e PNG válido, o
CARTO passou a exigir chave do mesmo jeito silencioso, e a sonda de
placeholder do KQMapa é a única defesa que existe contra isso. Escolher
provedor próprio aqui seria cair no mesmo buraco pela terceira vez.
"""
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from services import prtg_service
from services import rede_topologia_service as rede

from ..models import (
    AreaOperacionalChoices,
    Item,
    Localidade,
    RedeDispositivo,
    RedePorta,
    RedeRack,
    RedeRota,
    RedeRotaInstalacaoChoices,
    RedeRotaTipoChoices,
)


# ──────────────────────────────────────────────────────────────────────────────
# Tela
# ──────────────────────────────────────────────────────────────────────────────

@login_required
def rede_planta(request):
    """A planta georreferenciada: racks, equipamentos e rotas sobre o mapa."""
    return render(request, 'front/rede/planta.html', {
        'localidades': Localidade.objects.order_by('local'),
        'localidade_sel': request.GET.get('localidade') or '',
        'area_sel': request.GET.get('area') or '',
        'areas': AreaOperacionalChoices.choices,
        'tipos_rota': RedeRotaTipoChoices.choices,
        'instalacoes': RedeRotaInstalacaoChoices.choices,
        'prtg_ok': prtg_service.is_configured(),
        'planta_config': _config_planta(request),
    })


def _config_planta(request) -> dict:
    """
    Tudo que o `static/js/rede-planta.js` precisa do servidor, num dict só.

    Mesmo contrato do `_config_mapa`: chave nova entra AQUI e chega por
    `json_script`. Um `.js` em `static/` é servido cru, e `{% url %}` ali
    viraria texto literal — falha silenciosa que o teste varre.
    """
    return {
        'planta_url':     reverse('rede_planta_api'),
        'posicao_url':    reverse('rede_posicao_api'),
        'rota_url':       reverse('rede_rota_api'),
        'rack_url':       reverse('rede_rack_api'),
        'elevacao_url':   reverse('rede_rack_elevacao_api'),
        'impacto_url':    reverse('rede_impacto_rota_api'),
        'inventario_url': reverse('rede_inventario_api'),
        'exportar_url':   reverse('rede_planta_export'),
        'mapa_url':       reverse('rede_mapa'),
        'csrf_token':     get_token(request),
        'prtg_ok':        bool(prtg_service.is_configured()),
        'centro_padrao':  list(rede.centro_padrao()),
        'limite_utp':     rede.LIMITE_UTP_M,
        'folga_utp':      rede.FOLGA_UTP_M,
    }


# ──────────────────────────────────────────────────────────────────────────────
# A planta
# ──────────────────────────────────────────────────────────────────────────────

def _escopo(request):
    area = request.GET.get('area') or None
    if area and area not in dict(AreaOperacionalChoices.choices):
        area = None
    return request.GET.get('localidade') or None, area


@login_required
def rede_planta_api(request):
    """Racks, equipamentos posicionados e rotas do escopo, com conferência."""
    localidade, area = _escopo(request)
    planta = rede.montar_planta(
        localidade_id=localidade,
        area=area,
        com_status=request.GET.get('status', '1') != '0',
    )
    return JsonResponse({'ok': True, **planta})


@login_required
def rede_posicao_api(request):
    """
    POST — onde o equipamento está: coordenada, rack e altura em U.

    O `RedeDispositivo` é criado sob demanda (`rede.dispositivo_de`): arrastar
    um switch do inventário para o mapa é o gesto que o põe na planta, e
    exigir cadastro prévio faria o gesto falhar justamente para o equipamento
    que acabou de ser instalado.
    """
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'erro': 'Método não permitido'}, status=405)
    try:
        dados = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'ok': False, 'erro': 'Corpo inválido'}, status=400)

    item = get_object_or_404(Item, pk=dados.get('item') or 0)
    disp = rede.dispositivo_de(item, request.user)
    rede.salvar_posicao(disp, dados, request.user)
    return JsonResponse({
        'ok': True,
        'item_id': item.pk,
        'dispositivo_id': disp.pk,
        'lat': float(disp.latitude) if disp.latitude is not None else None,
        'lon': float(disp.longitude) if disp.longitude is not None else None,
        'rack_id': disp.rack_id,
        'rack_u': disp.rack_u,
        'rack_altura_u': disp.rack_altura_u,
    })


# ──────────────────────────────────────────────────────────────────────────────
# Rotas de cabo
# ──────────────────────────────────────────────────────────────────────────────

@login_required
def rede_rota_api(request):
    """
    GET  → as rotas do escopo (para o painel e para o seletor da porta).
    POST → `acao='salvar'` (cria ou atualiza) / `'excluir'` / `'vincular'`.

    `'vincular'` é o que amarra o cabo à vala: grava `RedePorta.rota` nas
    duas pontas por `rede.vincular_rota`. Gravar numa perna só faria a
    ocupação do duto sair pela metade.
    """
    if request.method == 'GET':
        localidade, area = _escopo(request)
        qs = RedeRota.objects.select_related('localidade')
        if localidade:
            qs = qs.filter(localidade_id=localidade)
        if area:
            qs = qs.filter(area=area)
        return JsonResponse({'ok': True, 'rotas': [
            {
                'id': r.pk,
                'nome': r.nome,
                'tipo': r.tipo,
                'tipo_label': r.get_tipo_display(),
                'familia': rede.cor_de_rota(r.tipo),
                'comprimento_m': float(r.comprimento_m or 0),
                'fibras': r.fibras,
                'localidade': r.localidade.local if r.localidade else '',
            }
            for r in qs.order_by('nome')
        ]})

    if request.method != 'POST':
        return JsonResponse({'ok': False, 'erro': 'Método não permitido'}, status=405)
    try:
        dados = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'ok': False, 'erro': 'Corpo inválido'}, status=400)

    acao = dados.get('acao') or 'salvar'

    if acao == 'vincular':
        porta = get_object_or_404(
            RedePorta.objects.select_related('dispositivo', 'destino_porta'),
            pk=dados.get('porta') or 0,
        )
        rota = None
        if dados.get('rota'):
            rota = get_object_or_404(RedeRota, pk=dados['rota'])
        rede.vincular_rota(porta, rota, request.user)
        return JsonResponse({'ok': True, **rede.painel_portas(porta.dispositivo)})

    if acao == 'excluir':
        rota = get_object_or_404(RedeRota, pk=dados.get('rota') or 0)
        # Excluir a rota NÃO desfaz cabo: o enlace continua existindo, só
        # perde o traçado físico. É o mesmo contrato do mapa salvo — desenho
        # não manda no inventário (regra 49).
        n = RedePorta.objects.filter(rota=rota).update(rota=None)
        nome = rota.nome
        rota.delete()
        return JsonResponse({
            'ok': True,
            'mensagem': f'Rota "{nome}" excluída. {n} enlace(s) continuam cadastrados, '
                        f'agora sem traçado físico.',
        })

    rota = (
        get_object_or_404(RedeRota, pk=dados['rota'])
        if dados.get('rota') else RedeRota()
    )
    criando = rota.pk is None
    if criando and not str(dados.get('nome') or '').strip():
        return JsonResponse({'ok': False, 'erro': 'Informe o nome da rota.'}, status=400)
    rede.salvar_rota(rota, dados, request.user)
    return JsonResponse({
        'ok': True,
        'criada': criando,
        'rota': {
            'id': rota.pk,
            'nome': rota.nome,
            'tipo': rota.tipo,
            'tipo_label': rota.get_tipo_display(),
            'instalacao': rota.instalacao,
            'instalacao_label': rota.get_instalacao_display(),
            'familia': rede.cor_de_rota(rota.tipo),
            'cobre': rota.e_cobre,
            'pontos': rota.pontos or [],
            'comprimento_m': float(rota.comprimento_m or 0),
            'fibras': rota.fibras,
            'observacoes': rota.observacoes or '',
        },
    })


@login_required
def rede_impacto_rota_api(request):
    """O que fica isolado se esta rota for rompida."""
    rota = get_object_or_404(RedeRota, pk=request.GET.get('rota') or 0)
    localidade, area = _escopo(request)
    grafo = rede.montar_grafo(localidade_id=localidade, area=area, com_status=False)
    return JsonResponse({
        'ok': True,
        **rede.impacto_rota(rota, grafo['nos'], grafo['enlaces']),
    })


# ──────────────────────────────────────────────────────────────────────────────
# Racks
# ──────────────────────────────────────────────────────────────────────────────

@login_required
def rede_rack_api(request):
    """GET → racks do escopo. POST → `acao='salvar'` / `'excluir'`."""
    if request.method == 'GET':
        localidade, area = _escopo(request)
        qs = RedeRack.objects.select_related('localidade').prefetch_related('equipamentos')
        if localidade:
            qs = qs.filter(localidade_id=localidade)
        if area:
            qs = qs.filter(area=area)
        return JsonResponse({'ok': True, 'racks': [
            {
                'id': r.pk,
                'nome': r.nome,
                'codigo': r.codigo or '',
                'ambiente': r.ambiente or '',
                'localidade': r.localidade.local if r.localidade else '',
                'us': r.us,
                'us_ocupados': r.us_ocupados,
                'lat': float(r.latitude) if r.latitude is not None else None,
                'lon': float(r.longitude) if r.longitude is not None else None,
            }
            for r in qs.order_by('nome')
        ]})

    if request.method != 'POST':
        return JsonResponse({'ok': False, 'erro': 'Método não permitido'}, status=405)
    try:
        dados = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'ok': False, 'erro': 'Corpo inválido'}, status=400)

    if dados.get('acao') == 'excluir':
        rack = get_object_or_404(RedeRack, pk=dados.get('rack') or 0)
        # Excluir o armário não apaga equipamento: o `on_delete=SET_NULL` da
        # FK solta os ativos, que continuam no inventário sem posição.
        n = RedeDispositivo.objects.filter(rack=rack).update(rack=None, rack_u=None)
        nome = rack.nome
        rack.delete()
        return JsonResponse({
            'ok': True,
            'mensagem': f'Rack "{nome}" excluído. {n} equipamento(s) ficaram sem rack.',
        })

    rack = (
        get_object_or_404(RedeRack, pk=dados['rack'])
        if dados.get('rack') else RedeRack()
    )
    criando = rack.pk is None
    if criando and not str(dados.get('nome') or '').strip():
        return JsonResponse({'ok': False, 'erro': 'Informe o nome do rack.'}, status=400)
    rede.salvar_rack(rack, dados, request.user)
    return JsonResponse({
        'ok': True,
        'criado': criando,
        'rack': {
            'id': rack.pk,
            'nome': rack.nome,
            'codigo': rack.codigo or '',
            'ambiente': rack.ambiente or '',
            'us': rack.us,
            'us_ocupados': rack.us_ocupados,
            'lat': float(rack.latitude) if rack.latitude is not None else None,
            'lon': float(rack.longitude) if rack.longitude is not None else None,
            'observacoes': rack.observacoes or '',
        },
    })


@login_required
def rede_rack_elevacao_api(request):
    """O rack visto de frente — a régua de U com quem ocupa cada altura."""
    rack = get_object_or_404(RedeRack, pk=request.GET.get('rack') or 0)
    return JsonResponse({'ok': True, **rede.elevacao_rack(rack)})


@login_required
def rede_rack_excluir(request, pk):
    """Confirmação em página, para quem chegou pelo admin ou por link."""
    rack = get_object_or_404(RedeRack, pk=pk)
    if request.method == 'POST':
        nome = rack.nome
        RedeDispositivo.objects.filter(rack=rack).update(rack=None, rack_u=None)
        rack.delete()
        messages.success(request, f'Rack "{nome}" excluído. Os equipamentos seguem no inventário.')
        return redirect('rede_planta')
    return render(request, 'front/rede/rack_confirm_delete.html', {
        'rack': rack,
        'equipamentos': rack.equipamentos.select_related('item').order_by('item__nome'),
    })


# ──────────────────────────────────────────────────────────────────────────────
# Exportação — a planta que vai ao gestor
# ──────────────────────────────────────────────────────────────────────────────

@login_required
def rede_planta_export(request):
    """
    Excel da planta: Resumo, Racks, Elevação, Rotas e Lances.

    A aba "Lances" é a que o eletricista leva para o campo: cada enlace com
    a rota por onde passa e o comprimento dela. É o documento que falta hoje
    e que a planilha paralela nunca teve, porque ninguém media os metros.
    """
    from io import BytesIO

    from django.http import HttpResponse
    from django.utils import timezone
    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter

    from services.excel_theme import (
        ALIGN_CENTER as center, ALIGN_LEFT_IND as left_ind, BORDA as borda,
        FILL_HEADER as fill_header, FILL_SUBTITULO as fill_sub,
        FILL_TITULO as fill_titulo, FILL_ZEBRA as fill_zebra,
        FONT_CELL as font_cell, FONT_HEADER as font_header,
        FONT_RODAPE as font_rodape, FONT_SUBTITULO as font_sub,
        FONT_TITULO as font_titulo, INT_FMT,
    )

    localidade, area = _escopo(request)
    planta = rede.montar_planta(localidade_id=localidade, area=area, com_status=False)
    k = planta['kpis']

    escopo = 'Corporativo'
    if localidade:
        loc = Localidade.objects.filter(pk=localidade).first()
        if loc:
            escopo = loc.local
    if area:
        escopo += f" · {dict(AreaOperacionalChoices.choices).get(area, area)}"

    wb = Workbook()

    def montar_aba(ws, titulo, colunas, larguras, linhas):
        ws.sheet_view.showGridLines = False
        ncol = len(colunas)
        for i, w in enumerate(larguras, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w

        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncol)
        c = ws.cell(row=1, column=1, value=f"PLANTA DE REDE — {titulo.upper()}")
        c.font = font_titulo
        c.fill = fill_titulo
        c.alignment = left_ind
        ws.row_dimensions[1].height = 26

        ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=ncol)
        c = ws.cell(row=2, column=1, value=(
            f"Escopo: {escopo}  ·  Emitido em "
            f"{timezone.localtime().strftime('%d/%m/%Y %H:%M')}"
        ))
        c.font = font_sub
        c.fill = fill_sub
        c.alignment = left_ind

        for j, nome in enumerate(colunas, start=1):
            c = ws.cell(row=4, column=j, value=nome)
            c.font = font_header
            c.fill = fill_header
            c.alignment = center
            c.border = borda

        linha = 5
        for idx, dados in enumerate(linhas):
            for j, valor in enumerate(dados, start=1):
                c = ws.cell(row=linha, column=j, value=valor)
                c.font = font_cell
                c.border = borda
                c.alignment = center if j > 2 else left_ind
                if idx % 2:
                    c.fill = fill_zebra
            linha += 1
        ws.freeze_panes = 'A5'
        return linha

    # ── Resumo ───────────────────────────────────────────────────────────────
    ws = wb.active
    ws.title = 'Resumo'
    fim = montar_aba(ws, 'Resumo', ['Indicador', 'Valor'], [46, 16], [
        ('Racks cadastrados', k['racks']),
        ('Racks sem posição no mapa', k['racks_sem_posicao']),
        ('Altura útil total (U)', k['us_totais']),
        ('Altura ocupada (U)', k['us_ocupados']),
        ('Equipamentos com ficha de rede', k['equipamentos']),
        ('Posicionados no mapa', k['posicionados']),
        ('Sem posição', k['sem_posicao']),
        ('Instalados em rack', k['em_rack']),
        ('Rotas de cabo', k['rotas']),
        ('Metros de cabo lançados', k['metros']),
        ('Metros em fibra óptica', k['metros_fibra']),
        ('Metros em par trançado', k['metros_utp']),
        ('Maior lance (m)', k['maior_lance']),
        ('Rotas sem enlace apontado', k['rotas_sem_enlace']),
    ])
    for r in range(5, fim):
        ws.cell(row=r, column=2).number_format = INT_FMT

    # ── Racks ────────────────────────────────────────────────────────────────
    ws = wb.create_sheet('Racks')
    montar_aba(
        ws, 'Racks e armários',
        ['Rack', 'Código', 'Localidade', 'Ambiente', 'Altura (U)', 'Ocupado (U)',
         'Livre (U)', 'Equipamentos', 'Latitude', 'Longitude'],
        [30, 16, 26, 24, 11, 12, 10, 13, 14, 14],
        [
            (r['nome'], r['codigo'], r['localidade'], r['ambiente'], r['us'],
             r['us_ocupados'], max(0, r['us'] - r['us_ocupados']), r['equipamentos'],
             r['lat'] if r['lat'] is not None else '',
             r['lon'] if r['lon'] is not None else '')
            for r in planta['racks']
        ],
    )

    # ── Elevação: o que está em que U, rack por rack ─────────────────────────
    linhas_elev = []
    for rack in RedeRack.objects.filter(
        pk__in=[r['id'] for r in planta['racks']]
    ).prefetch_related('equipamentos__item__subtipo'):
        elev = rede.elevacao_rack(rack)
        for faixa in elev['regua']:
            ocup = faixa['ocupante']
            if not (ocup and ocup['primeiro']):
                continue
            linhas_elev.append((
                rack.nome, f"U{faixa['u']}", ocup['nome'], ocup['classe'],
                ocup['altura_u'], ocup['ip'],
            ))
        for sem in elev['sem_posicao']:
            linhas_elev.append((
                rack.nome, '—', sem['nome'], 'posição não declarada',
                sem['altura_u'], '',
            ))
    ws = wb.create_sheet('Elevação')
    montar_aba(
        ws, 'Elevação dos racks',
        ['Rack', 'Posição (U)', 'Equipamento', 'Classe', 'Altura (U)', 'IP de gerência'],
        [28, 13, 34, 18, 11, 17],
        linhas_elev,
    )

    # ── Rotas ────────────────────────────────────────────────────────────────
    ws = wb.create_sheet('Rotas')
    montar_aba(
        ws, 'Rotas de cabo',
        ['Rota', 'Localidade', 'Tipo de cabo', 'Instalação', 'Comprimento (m)',
         'Fibras / pares', 'Enlaces', 'Sai do rack', 'Chega no rack',
         'Pontos do traçado', 'Observações'],
        [34, 24, 28, 24, 16, 14, 10, 24, 24, 17, 40],
        [
            (r['nome'], r['localidade'], r['tipo_label'], r['instalacao_label'],
             r['comprimento_m'], r['fibras'] or '', r['enlaces'],
             r['rack_a_nome'], r['rack_b_nome'],
             len(r['pontos']), r['observacoes'])
            for r in sorted(planta['rotas'], key=lambda x: -x['comprimento_m'])
        ],
    )

    # ── Lances: o papel que vai para o campo ─────────────────────────────────
    lances = (
        RedePorta.objects
        .filter(rota__isnull=False)
        .select_related('dispositivo__item', 'destino_item',
                        'destino_porta__dispositivo__item', 'rota')
        .order_by('rota__nome', 'dispositivo__item__nome', 'numero')
    )
    if localidade:
        lances = lances.filter(dispositivo__item__localidade_id=localidade)
    if area:
        lances = lances.filter(dispositivo__item__area_operacional=area)

    linhas_lances = []
    vistas = set()
    for p in lances:
        # Um cabo, uma linha: as duas pontas apontam uma para a outra, e o
        # desempate é o menor id — mesma regra do grafo.
        if p.destino_porta_id:
            if p.pk > p.destino_porta_id:
                continue
            destino = p.destino_porta.dispositivo.item.nome
            porta_destino = p.destino_porta.rotulo or str(p.destino_porta.numero)
        elif p.destino_item_id:
            destino = p.destino_item.nome
            porta_destino = ''
        else:
            continue
        chave = (p.pk, p.destino_porta_id)
        if chave in vistas:
            continue
        vistas.add(chave)
        linhas_lances.append((
            p.rota.nome, p.dispositivo.item.nome, p.rotulo or str(p.numero),
            destino, porta_destino, p.get_meio_display(),
            float(p.rota.comprimento_m or 0), p.rota.get_tipo_display(),
            p.vlan, p.descricao,
        ))
    ws = wb.create_sheet('Lances')
    montar_aba(
        ws, 'Lances por rota',
        ['Rota', 'Origem', 'Porta', 'Destino', 'Porta destino', 'Meio',
         'Comprimento (m)', 'Tipo de cabo', 'VLAN', 'Descrição'],
        [30, 30, 12, 30, 13, 15, 16, 26, 11, 30],
        linhas_lances,
    )

    # ── Conferência ──────────────────────────────────────────────────────────
    ws = wb.create_sheet('Conferência')
    montar_aba(
        ws, 'Conferência da planta',
        ['Gravidade', 'Verificação', 'Detalhe'],
        [14, 56, 78],
        [(a['nivel'].upper(), a['titulo'], a['detalhe']) for a in planta['avisos']],
    )
    ws.cell(row=len(planta['avisos']) + 6, column=1, value=(
        f'O limite de {rede.LIMITE_UTP_M} m vale para o lance permanente em par '
        f'trançado (TIA-568); os 10 m restantes do total de 100 m são os cordões '
        f'das duas pontas. Comprimentos medidos sobre o traçado desenhado na planta.'
    )).font = font_rodape

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    nome = f"planta_rede_{timezone.localtime().strftime('%Y%m%d_%H%M')}.xlsx"
    resp = HttpResponse(
        buf.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    resp['Content-Disposition'] = f'attachment; filename="{nome}"'
    return resp
