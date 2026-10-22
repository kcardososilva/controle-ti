"""
Mapa de Rede — a bancada de topologia do módulo de Plantas.

Divisão de trabalho desta camada: a view **monta a resposta**, o
`services/rede_topologia_service.py` **responde as perguntas**. Nenhuma regra
de rede (o que é switch, o que é enlace, quantas portas tem um Catalyst,
quando um aviso técnico dispara) nasce aqui.

Contrato com o front: tudo que o `static/js/rede-mapa.js` precisa do servidor
entra em `_config_mapa()` e chega por `json_script` — um `.js` em `static/` é
servido cru e `{% url %}` ali viraria texto literal, quebrando em silêncio
(ver a seção "Telas grandes" do CLAUDE.md).
"""
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Count, Q
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
    RedeMeioChoices,
    RedePapelChoices,
    RedePorta,
    RedePortaStatusChoices,
    RedePortaTipoChoices,
    RedeRack,
    RedeRota,
    RedeTopologia,
    RedeTopologiaHistorico,
    RedeVelocidadeChoices,
)

_MAX_HISTORICO = 20


# ──────────────────────────────────────────────────────────────────────────────
# Tela
# ──────────────────────────────────────────────────────────────────────────────

@login_required
def rede_mapa(request):
    """
    A bancada. Abre um mapa salvo (`?mapa=<pk>`) ou a visão corporativa.

    A visão sem mapa salvo não é um estado de erro: é o modo de leitura do
    parque inteiro, com arranjo automático. Exigir criar um mapa antes de ver
    qualquer coisa deixaria a tela vazia justamente para quem abriu pela
    primeira vez.
    """
    mapas = list(
        RedeTopologia.objects.select_related('localidade').order_by('localidade__local', 'nome')
    )

    mapa = None
    mapa_id = request.GET.get('mapa')
    if mapa_id:
        mapa = next((m for m in mapas if str(m.pk) == str(mapa_id)), None)

    localidade_sel = request.GET.get('localidade') or (str(mapa.localidade_id) if mapa and mapa.localidade_id else '')
    area_sel = request.GET.get('area') or (mapa.area if mapa else '')

    return render(request, 'front/rede/mapa.html', {
        'mapa': mapa,
        'mapas': mapas,
        'localidades': Localidade.objects.order_by('local'),
        'localidade_sel': localidade_sel,
        'area_sel': area_sel,
        'areas': AreaOperacionalChoices.choices,
        'papeis': RedePapelChoices.choices,
        'tipos_porta': RedePortaTipoChoices.choices,
        'status_porta': RedePortaStatusChoices.choices,
        'velocidades': RedeVelocidadeChoices.choices,
        'meios': RedeMeioChoices.choices,
        'catalogo': rede.catalogo_modelos(),
        'prtg_ok': prtg_service.is_configured(),
        'mapa_config': _config_mapa(request, mapa),
    })


def _config_mapa(request, mapa) -> dict:
    """
    Tudo que o JS da bancada precisa do servidor, num dict só.

    Chave nova para o front: **acrescente aqui**, não interpole no `.js`.
    `test_rede_mapa.py` trava o contrato nos dois sentidos — chave entregue que
    ninguém lê e chave lida que ninguém entrega falham o teste.
    """
    return {
        'grafo_url':       reverse('rede_grafo_api'),
        'salvar_url':      reverse('rede_mapa_salvar'),
        'dispositivo_url': reverse('rede_dispositivo_api'),
        'portas_url':      reverse('rede_portas_api'),
        'porta_url':       reverse('rede_porta_api'),
        'enlace_url':      reverse('rede_enlace_api'),
        'impacto_url':     reverse('rede_impacto_api'),
        'historico_url':   reverse('rede_historico_api'),
        'inventario_url':  reverse('rede_inventario_api'),
        'criar_url':       reverse('rede_mapa_criar'),
        'prtg_busca_url':  reverse('prtg_search_api'),
        'exportar_url':    reverse('rede_mapa_export'),
        # A planta georreferenciada é tela irmã (ver `rede_planta.py`): o
        # mesmo enlace aparece nos dois desenhos, e a rota é o elo. A bancada
        # precisa da lista de rotas para o seletor da porta e do link para a
        # planta — sem isso o vínculo cabo↔vala só poderia ser feito de um
        # lado, e o diagrama nunca saberia por onde o cabo passa.
        'planta_url':      reverse('rede_planta'),
        'rota_url':        reverse('rede_rota_api'),
        'elevacao_url':    reverse('rede_rack_elevacao_api'),
        'csrf_token':      get_token(request),
        'prtg_ok':         bool(prtg_service.is_configured()),
        'mapa_id':         mapa.pk if mapa else None,
        'mapa_nome':       mapa.nome if mapa else 'Visão corporativa',
        'mapa_versao':     mapa.versao if mapa else 0,
        'layout':          (mapa.layout or {}) if mapa else {},
        'catalogo':        rede.catalogo_modelos(),
    }


# ──────────────────────────────────────────────────────────────────────────────
# CRUD de mapa
# ──────────────────────────────────────────────────────────────────────────────

@login_required
def rede_mapa_criar(request):
    """Cria um mapa a partir do escopo em uso. Responde JSON (modal da bancada)."""
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'erro': 'Método não permitido'}, status=405)
    try:
        dados = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'ok': False, 'erro': 'Corpo inválido'}, status=400)

    nome = str(dados.get('nome') or '').strip()[:160]
    if not nome:
        return JsonResponse({'ok': False, 'erro': 'Informe o nome do mapa.'}, status=400)

    area = str(dados.get('area') or '')
    if area and area not in dict(AreaOperacionalChoices.choices):
        area = ''

    localidade_id = dados.get('localidade') or None
    if localidade_id and not Localidade.objects.filter(pk=localidade_id).exists():
        localidade_id = None

    mapa = RedeTopologia.objects.create(
        nome=nome,
        descricao=str(dados.get('descricao') or '')[:2000],
        localidade_id=localidade_id,
        area=area,
        layout=dados.get('layout') or {},
        criado_por=request.user,
        atualizado_por=request.user,
    )
    return JsonResponse({
        'ok': True,
        'id': mapa.pk,
        'url': f"{reverse('rede_mapa')}?mapa={mapa.pk}",
    })


@login_required
def rede_mapa_excluir(request, pk):
    mapa = get_object_or_404(RedeTopologia, pk=pk)
    if request.method == 'POST':
        nome = mapa.nome
        mapa.delete()
        messages.success(request, f'Mapa "{nome}" excluído.')
        return redirect('rede_mapa')
    return render(request, 'front/rede/mapa_confirm_delete.html', {'mapa': mapa})


@login_required
def rede_mapa_salvar(request):
    """
    Grava a GEOMETRIA do mapa (posições, zonas, notas) — nunca enlace.

    Enlace é fato da porta e tem rota própria (`rede_enlace_api`). Aceitar
    enlace aqui recriaria a divergência que o modelo existe para impedir: o
    desenho afirmando um cabeamento e o inventário de portas, outro.
    """
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'erro': 'Método não permitido'}, status=405)
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'ok': False, 'erro': 'Corpo inválido'}, status=400)

    mapa = get_object_or_404(RedeTopologia, pk=payload.get('mapa'))

    versao_cliente = payload.get('versao')
    if versao_cliente is not None:
        try:
            versao_cliente = int(versao_cliente)
        except (TypeError, ValueError):
            versao_cliente = None
    if versao_cliente is not None and versao_cliente != mapa.versao:
        quem = ''
        if mapa.atualizado_por:
            quem = mapa.atualizado_por.get_full_name() or mapa.atualizado_por.username
        return JsonResponse({
            'ok': False,
            'conflito': True,
            'versao_atual': mapa.versao,
            'editado_por': quem or 'outro usuário',
        })

    layout = _limpar_layout(payload.get('layout') or {})

    if payload.get('nova_versao'):
        RedeTopologiaHistorico.objects.create(
            topologia=mapa,
            versao=mapa.versao,
            layout=mapa.layout,
            salvo_por=request.user,
            descricao=str(payload.get('descricao') or '')[:200],
        )
        antigos = (
            RedeTopologiaHistorico.objects
            .filter(topologia=mapa).order_by('-versao')
            .values_list('id', flat=True)[_MAX_HISTORICO:]
        )
        if antigos:
            RedeTopologiaHistorico.objects.filter(id__in=list(antigos)).delete()

    mapa.layout = layout
    mapa.versao = mapa.versao + 1
    mapa.atualizado_por = request.user
    mapa.save(update_fields=['layout', 'versao', 'atualizado_por', 'updated_at'])
    return JsonResponse({'ok': True, 'versao': mapa.versao})


# Chaves aceitas na geometria. Qualquer outra é descartada — o layout vai para
# um JSONField, e aceitar o que o cliente mandar é aceitar carga arbitrária
# numa coluna que a tela depois renderiza.
_CHAVES_NO = {'x', 'y', 'fixo', 'oculto', 'cor'}
_CHAVES_ZONA = {'id', 'nome', 'x', 'y', 'w', 'h', 'cor'}
_CHAVES_NOTA = {'id', 'texto', 'x', 'y', 'w'}


def _num(valor, padrao=0):
    try:
        n = float(valor)
    except (TypeError, ValueError):
        return padrao
    # Coordenada fora de escala trava o navegador ao desenhar; a prancha tem
    # 20.000 px de folga, que é muito mais do que qualquer mapa real usa.
    return max(-20000.0, min(20000.0, n))


def _limpar_layout(bruto: dict) -> dict:
    nos = {}
    for chave, valor in (bruto.get('nos') or {}).items():
        if not isinstance(valor, dict):
            continue
        limpo = {k: v for k, v in valor.items() if k in _CHAVES_NO}
        limpo['x'] = _num(limpo.get('x'))
        limpo['y'] = _num(limpo.get('y'))
        nos[str(chave)[:40]] = limpo

    zonas = []
    for z in (bruto.get('zonas') or [])[:120]:
        if not isinstance(z, dict):
            continue
        limpa = {k: v for k, v in z.items() if k in _CHAVES_ZONA}
        limpa['nome'] = str(limpa.get('nome') or '')[:80]
        for eixo, padrao in (('x', 0), ('y', 0), ('w', 200), ('h', 120)):
            limpa[eixo] = _num(limpa.get(eixo), padrao)
        zonas.append(limpa)

    notas = []
    for n in (bruto.get('notas') or [])[:120]:
        if not isinstance(n, dict):
            continue
        limpa = {k: v for k, v in n.items() if k in _CHAVES_NOTA}
        limpa['texto'] = str(limpa.get('texto') or '')[:400]
        for eixo, padrao in (('x', 0), ('y', 0), ('w', 220)):
            limpa[eixo] = _num(limpa.get(eixo), padrao)
        notas.append(limpa)

    canvas = bruto.get('canvas') or {}
    return {
        'nos': nos,
        'zonas': zonas,
        'notas': notas,
        'canvas': {
            'width': int(_num(canvas.get('width'), 1400)),
            'height': int(_num(canvas.get('height'), 900)),
        },
        'agrupar': str(bruto.get('agrupar') or 'papel')[:20],
    }


@login_required
def rede_historico_api(request):
    """Versões da geometria de um mapa; com `?restaurar=<pk>` volta para ela."""
    mapa = get_object_or_404(RedeTopologia, pk=request.GET.get('mapa') or 0)

    restaurar = request.GET.get('restaurar')
    if restaurar:
        if request.method != 'POST':
            return JsonResponse({'ok': False, 'erro': 'Método não permitido'}, status=405)
        entrada = get_object_or_404(RedeTopologiaHistorico, pk=restaurar, topologia=mapa)
        RedeTopologiaHistorico.objects.get_or_create(
            topologia=mapa, versao=mapa.versao,
            defaults={
                'layout': mapa.layout,
                'salvo_por': request.user,
                'descricao': f'Backup antes de restaurar v{entrada.versao}',
            },
        )
        mapa.layout = entrada.layout
        mapa.versao = mapa.versao + 1
        mapa.atualizado_por = request.user
        mapa.save(update_fields=['layout', 'versao', 'atualizado_por', 'updated_at'])
        return JsonResponse({'ok': True, 'versao': mapa.versao, 'layout': mapa.layout})

    entradas = (
        RedeTopologiaHistorico.objects
        .filter(topologia=mapa).select_related('salvo_por').order_by('-versao')[:_MAX_HISTORICO]
    )
    return JsonResponse({'ok': True, 'historico': [
        {
            'id': e.id,
            'versao': e.versao,
            'salvo_por': (e.salvo_por.get_full_name() or e.salvo_por.username) if e.salvo_por else '—',
            'salvo_em': e.salvo_em.strftime('%d/%m/%Y %H:%M'),
            'descricao': e.descricao,
            'nos': len((e.layout or {}).get('nos') or {}),
        }
        for e in entradas
    ]})


# ──────────────────────────────────────────────────────────────────────────────
# APIs do grafo
# ──────────────────────────────────────────────────────────────────────────────

@login_required
def rede_grafo_api(request):
    """
    O grafo no escopo pedido, já com status PRTG e avisos técnicos.

    `?arranjo=1` devolve também a geometria calculada — é o que a bancada pede
    quando o mapa não tem posição salva para um nó novo. O cliente nunca
    calcula arranjo por conta própria: o Excel e o relatório usam o mesmo.
    """
    localidade = request.GET.get('localidade') or None
    area = request.GET.get('area') or None
    if area and area not in dict(AreaOperacionalChoices.choices):
        area = None
    incluir_terminais = request.GET.get('terminais', '1') != '0'

    grafo = rede.montar_grafo(
        localidade_id=localidade,
        area=area,
        incluir_terminais=incluir_terminais,
        com_status=request.GET.get('status', '1') != '0',
    )

    if request.GET.get('arranjo') == '1':
        agrupar = request.GET.get('agrupar') or 'papel'
        grafo['arranjo'] = rede.auto_arranjo(grafo['nos'], grafo['enlaces'], agrupar)

    return JsonResponse({'ok': True, **grafo})


@login_required
def rede_impacto_api(request):
    """O que fica isolado se um nó cair."""
    no_id = request.GET.get('no') or ''
    grafo = rede.montar_grafo(
        localidade_id=request.GET.get('localidade') or None,
        area=request.GET.get('area') or None,
        com_status=False,
    )
    return JsonResponse({'ok': True, **rede.impacto(no_id, grafo['nos'], grafo['enlaces'])})


# ──────────────────────────────────────────────────────────────────────────────
# APIs de dispositivo e portas
# ──────────────────────────────────────────────────────────────────────────────

@login_required
def rede_dispositivo_api(request):
    """
    GET  `?item=<pk>` → ficha de rede + painel de portas.
    POST → grava papel, IP de gerência, vínculo PRTG e observações.

    O `RedeDispositivo` é criado sob demanda (`rede.dispositivo_de`): a faceta
    de rede de um ativo só passa a existir quando alguém olha para ele como
    rede, e exigir cadastro prévio deixaria a bancada sem o switch que o
    técnico acabou de instalar.
    """
    if request.method == 'GET':
        item = get_object_or_404(Item, pk=request.GET.get('item') or 0)
        disp = rede.dispositivo_de(item, request.user)
        painel = rede.painel_portas(disp)
        painel['modelo_sugerido'] = disp.modelo_portas or rede.modelo_sugerido(item)
        painel['item'] = {
            'id': item.pk,
            'nome': item.nome,
            'marca': item.marca or '',
            'modelo': item.modelo or '',
            'serie': item.numero_serie or '',
            'localidade': item.localidade.local if item.localidade else '',
            'area': item.area_operacional_label,
            'subtipo': item.subtipo.nome if item.subtipo else '',
            'status': item.get_status_display(),
        }
        # Listas dos seletores da ficha: o rack onde o equipamento está e a
        # rota por onde cada cabo passa. Vão na MESMA resposta de propósito —
        # dois pedidos a mais por clique de nó, num mapa de 90 switches, é
        # latência que aparece (mesma decisão do catálogo em `_config_mapa`).
        painel['racks'] = [
            {'id': r.pk, 'nome': r.nome, 'codigo': r.codigo or '',
             'us': r.us, 'localidade': r.localidade.local if r.localidade else ''}
            for r in RedeRack.objects.select_related('localidade').order_by('nome')
        ]
        painel['rotas'] = [
            {'id': r.pk, 'nome': r.nome, 'tipo_label': r.get_tipo_display(),
             'familia': rede.cor_de_rota(r.tipo),
             'comprimento_m': float(r.comprimento_m or 0)}
            for r in RedeRota.objects.order_by('nome')
        ]
        return JsonResponse({'ok': True, **painel})

    if request.method != 'POST':
        return JsonResponse({'ok': False, 'erro': 'Método não permitido'}, status=405)

    try:
        dados = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'ok': False, 'erro': 'Corpo inválido'}, status=400)

    item = get_object_or_404(Item, pk=dados.get('item') or 0)
    disp = rede.dispositivo_de(item, request.user)

    papel = str(dados.get('papel') or '')
    if papel in dict(RedePapelChoices.choices):
        disp.papel = papel

    ip = str(dados.get('ip') or '').strip()
    disp.ip_gerencia = ip or None

    objid = dados.get('prtg_objid')
    try:
        disp.prtg_objid = int(objid) if objid not in (None, '', 'null') else None
    except (TypeError, ValueError):
        disp.prtg_objid = None

    disp.observacoes = str(dados.get('observacoes') or '')[:2000]
    disp.atualizado_por = request.user

    # Posição física (rack, altura em U, coordenada) passa pelo service, que é
    # quem sabe que 0U não existe e que meia coordenada não é posição.
    if any(c in dados for c in ('rack', 'rack_u', 'rack_altura_u', 'latitude', 'longitude')):
        rede.salvar_posicao(disp, dados, request.user)

    try:
        disp.full_clean()
    except ValidationError as exc:
        return JsonResponse({'ok': False, 'erro': '; '.join(
            f"{campo}: {' '.join(msgs)}" for campo, msgs in exc.message_dict.items()
        )}, status=400)
    disp.save()
    return JsonResponse({'ok': True, 'dispositivo_id': disp.pk})


@login_required
def rede_portas_api(request):
    """
    POST — gera as portas de um modelo do catálogo, ou acrescenta uma avulsa.

    `acao='gerar'` usa o catálogo; `acao='nova'` cria uma porta no fim da
    numeração, para o switch que ganhou um módulo fora do padrão.
    """
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'erro': 'Método não permitido'}, status=405)
    try:
        dados = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'ok': False, 'erro': 'Corpo inválido'}, status=400)

    item = get_object_or_404(Item, pk=dados.get('item') or 0)
    disp = rede.dispositivo_de(item, request.user)
    acao = dados.get('acao') or 'gerar'

    if acao == 'gerar':
        modelo = str(dados.get('modelo') or '')
        if modelo not in rede.MODELOS:
            return JsonResponse({'ok': False, 'erro': 'Modelo não encontrado no catálogo.'}, status=400)
        criadas = rede.gerar_portas(
            disp, modelo, usuario=request.user, substituir=bool(dados.get('substituir'))
        )
        return JsonResponse({'ok': True, 'criadas': criadas, **rede.painel_portas(disp)})

    if acao == 'nova':
        ultimo = disp.portas.order_by('-numero').values_list('numero', flat=True).first() or 0
        tipo = str(dados.get('tipo') or RedePortaTipoChoices.RJ45)
        if tipo not in dict(RedePortaTipoChoices.choices):
            tipo = RedePortaTipoChoices.RJ45
        RedePorta.objects.create(
            dispositivo=disp,
            numero=ultimo + 1,
            rotulo=str(dados.get('rotulo') or '')[:40] or str(ultimo + 1),
            tipo=tipo,
            criado_por=request.user,
            atualizado_por=request.user,
        )
        return JsonResponse({'ok': True, **rede.painel_portas(disp)})

    return JsonResponse({'ok': False, 'erro': 'Ação desconhecida.'}, status=400)


@login_required
def rede_porta_api(request):
    """POST grava os atributos de uma porta; `acao='excluir'` a remove."""
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'erro': 'Método não permitido'}, status=405)
    try:
        dados = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'ok': False, 'erro': 'Corpo inválido'}, status=400)

    porta = get_object_or_404(
        RedePorta.objects.select_related('dispositivo'), pk=dados.get('porta') or 0
    )

    if dados.get('acao') == 'excluir':
        if porta.tem_enlace:
            rede.desconectar(porta, request.user)
        disp = porta.dispositivo
        porta.delete()
        return JsonResponse({'ok': True, **rede.painel_portas(disp)})

    if 'rotulo' in dados:
        porta.rotulo = str(dados.get('rotulo') or '')[:40]
    if 'descricao' in dados:
        porta.descricao = str(dados.get('descricao') or '')[:160]
    if 'vlan' in dados:
        porta.vlan = str(dados.get('vlan') or '')[:40]
    if 'poe' in dados:
        porta.poe = bool(dados.get('poe'))
    for campo, opcoes in (
        ('tipo', RedePortaTipoChoices), ('velocidade', RedeVelocidadeChoices),
        ('meio', RedeMeioChoices),
    ):
        if campo in dados:
            valor = str(dados.get(campo) or '')
            if valor in dict(opcoes.choices) or valor == '':
                setattr(porta, campo, valor)

    # Rota física: por onde este lance passa no terreno. `rede.vincular_rota`
    # grava nas duas pontas — com o vínculo em uma perna só, a ocupação do
    # duto sairia pela metade na planta.
    if 'rota' in dados:
        rota = None
        if dados.get('rota'):
            rota = RedeRota.objects.filter(pk=dados['rota']).first()
            if rota is None:
                return JsonResponse({'ok': False, 'erro': 'Rota não encontrada.'}, status=400)
        rede.vincular_rota(porta, rota, request.user)
        porta.refresh_from_db()

    if 'status' in dados:
        novo = str(dados.get('status') or '')
        if novo in dict(RedePortaStatusChoices.choices):
            # Marcar como livre é soltar o cabo: deixar o destino gravado numa
            # porta "livre" é a inconsistência que o aviso técnico
            # `porta_ativa_sem_destino` existe para apontar, ao contrário.
            if novo != RedePortaStatusChoices.ATIVA and porta.tem_enlace:
                rede.desconectar(porta, request.user)
                porta.refresh_from_db()
            porta.status = novo

    porta.atualizado_por = request.user
    porta.save()
    return JsonResponse({'ok': True, **rede.painel_portas(porta.dispositivo)})


@login_required
def rede_enlace_api(request):
    """
    GET  `?item=<pk>` → portas do equipamento, para escolher o outro lado.
    POST → `acao='conectar'` / `'desconectar'`.

    Toda criação de cabo passa por `rede.conectar`, que grava os dois sentidos.
    """
    if request.method == 'GET':
        item = get_object_or_404(Item, pk=request.GET.get('item') or 0)
        disp = RedeDispositivo.objects.filter(item=item).first()
        if not disp:
            return JsonResponse({'ok': True, 'portas': [], 'nome': item.nome, 'sem_portas': True})
        portas = (
            disp.portas
            .select_related('destino_item', 'destino_porta__dispositivo__item')
            .order_by('numero')
        )
        return JsonResponse({'ok': True, 'nome': item.nome, 'portas': [
            {
                'id': p.pk,
                'rotulo': p.rotulo or str(p.numero),
                'tipo': p.tipo,
                'status': p.status,
                'livre': not p.tem_enlace,
                'destino': (
                    p.destino_porta.dispositivo.item.nome if p.destino_porta_id
                    else (p.destino_item.nome if p.destino_item_id else '')
                ),
            }
            for p in portas
        ]})

    if request.method != 'POST':
        return JsonResponse({'ok': False, 'erro': 'Método não permitido'}, status=405)
    try:
        dados = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'ok': False, 'erro': 'Corpo inválido'}, status=400)

    porta = get_object_or_404(
        RedePorta.objects.select_related('dispositivo'), pk=dados.get('porta') or 0
    )

    if dados.get('acao') == 'desconectar':
        rede.desconectar(porta, request.user)
        return JsonResponse({'ok': True, **rede.painel_portas(porta.dispositivo)})

    meio = str(dados.get('meio') or RedeMeioChoices.UTP)
    if meio not in dict(RedeMeioChoices.choices):
        meio = RedeMeioChoices.UTP

    destino_porta = None
    destino_item = None
    if dados.get('destino_porta'):
        destino_porta = get_object_or_404(
            RedePorta.objects.select_related('dispositivo'), pk=dados['destino_porta']
        )
    elif dados.get('destino_item'):
        destino_item = get_object_or_404(Item, pk=dados['destino_item'])

    try:
        rede.conectar(porta, destino_porta, destino_item, meio=meio, usuario=request.user)
    except rede.EnlaceInvalido as exc:
        return JsonResponse({'ok': False, 'erro': str(exc)}, status=400)

    return JsonResponse({'ok': True, **rede.painel_portas(porta.dispositivo)})


# ──────────────────────────────────────────────────────────────────────────────
# Busca de equipamento de rede (painel esquerdo da bancada)
# ──────────────────────────────────────────────────────────────────────────────

@login_required
def rede_inventario_api(request):
    """
    Inventário de rede para o painel da bancada, agrupado por subtipo.

    Busca remota e não lista pronta no HTML: são 298 ativos de rede e 1.670
    itens no acervo — como `<option>` isso seriam centenas de KB a cada
    page-load (mesma decisão da busca remota das licenças e dos termos).
    """
    q = (request.GET.get('q') or '').strip()
    localidade = request.GET.get('localidade') or None
    area = request.GET.get('area') or None

    qs = (
        Item.objects
        .select_related('localidade', 'subtipo')
        .filter(status='ativo')
        .annotate(n_portas=Count('rede__portas', distinct=True))
    )
    if q:
        qs = qs.filter(
            Q(nome__icontains=q) | Q(marca__icontains=q)
            | Q(modelo__icontains=q) | Q(numero_serie__icontains=q)
        )
    if localidade:
        qs = qs.filter(localidade_id=localidade)
    if area:
        qs = qs.filter(area_operacional=area)

    resultados = []
    for item in qs.order_by('subtipo__nome', 'nome')[:400]:
        classe, papel = rede.classe_de(item.subtipo.nome if item.subtipo else '')
        if classe == 'generico' and not q:
            continue        # sem busca, a lista mostra só o que é rede
        resultados.append({
            'item_id': item.pk,
            'nome': item.nome,
            'classe': classe,
            'papel': papel,
            'grupo': item.subtipo.nome if item.subtipo else 'Sem subtipo',
            'marca': item.marca or '',
            'modelo': item.modelo or '',
            'localidade': item.localidade.local if item.localidade else '',
            'portas': item.n_portas,
        })
    return JsonResponse({'ok': True, 'resultados': resultados, 'total': len(resultados)})


# ──────────────────────────────────────────────────────────────────────────────
# Exportação — o documento que vai ao gestor
# ──────────────────────────────────────────────────────────────────────────────

@login_required
def rede_mapa_export(request):
    """
    Excel com quatro abas: resumo, equipamentos, portas e conferência.

    É o entregável da apresentação: o mapa responde na tela, a planilha
    responde no e-mail. As duas leem o MESMO `montar_grafo`, então não podem
    divergir.
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
        FONT_CELL as font_cell, FONT_CELL_BOLD as font_bold,
        FONT_HEADER as font_header, FONT_RODAPE as font_rodape,
        FONT_SUBTITULO as font_sub, FONT_TITULO as font_titulo,
        INT_FMT, PCT_FMT,
    )

    localidade = request.GET.get('localidade') or None
    area = request.GET.get('area') or None
    grafo = rede.montar_grafo(localidade_id=localidade, area=area)
    nos = grafo['nos']
    kpis = grafo['kpis']

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
        c = ws.cell(row=1, column=1, value=f"MAPA DE REDE — {titulo.upper()}")
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
    resumo = [
        ('Equipamentos no mapa', kpis['nos']),
        ('Switches', kpis['switches']),
        ('Access points', kpis['aps']),
        ('Enlaces mapeados', kpis['enlaces']),
        ('Enlaces em fibra', kpis['fibra']),
        ('Portas cadastradas', kpis['portas_totais']),
        ('Portas em uso', kpis['portas_ativas']),
        ('Portas livres', kpis['portas_livres']),
        ('Online (PRTG)', kpis['online']),
        ('Offline (PRTG)', kpis['offline']),
        ('Instável (PRTG)', kpis['instavel']),
        ('Sem monitoramento', kpis['nao_monitorado']),
        ('Sem faceta de rede cadastrada', kpis['nao_mapeados']),
    ]
    fim = montar_aba(ws, 'Resumo', ['Indicador', 'Valor'], [44, 16],
                     [(nome, valor) for nome, valor in resumo])
    for r in range(5, fim):
        ws.cell(row=r, column=2).number_format = INT_FMT
    c = ws.cell(row=fim + 1, column=1, value='Ocupação geral das portas')
    c.font = font_bold
    c = ws.cell(row=fim + 1, column=2, value=(kpis['ocupacao'] / 100) if kpis['ocupacao'] else 0)
    c.font = font_bold
    c.number_format = PCT_FMT
    c.alignment = center

    # ── Equipamentos ─────────────────────────────────────────────────────────
    ws = wb.create_sheet('Equipamentos')
    montar_aba(
        ws, 'Equipamentos',
        ['Equipamento', 'Localidade', 'Classe', 'Papel', 'Marca / Modelo',
         'Nº de série', 'IP de gerência', 'Portas', 'Em uso', 'Livres',
         'Ocupação %', 'Status PRTG', 'Área'],
        [34, 26, 13, 15, 28, 20, 16, 9, 9, 9, 11, 15, 16],
        [
            (
                n['nome'], n['localidade'], n['classe'], n['papel_label'],
                f"{n['marca']} {n['modelo']}".strip(), n['serie'], n['ip'],
                n['portas_total'], n['portas_ativas'], n['portas_livres'],
                n['ocupacao'], n['status']['slug'], n['area_label'],
            )
            for n in sorted(nos, key=lambda x: (x['localidade'], x['nome']))
        ],
    )

    # ── Portas ───────────────────────────────────────────────────────────────
    portas = (
        RedePorta.objects
        .select_related('dispositivo__item__localidade', 'destino_item',
                        'destino_porta__dispositivo__item')
        .order_by('dispositivo__item__nome', 'numero')
    )
    if localidade:
        portas = portas.filter(dispositivo__item__localidade_id=localidade)
    if area:
        portas = portas.filter(dispositivo__item__area_operacional=area)

    linhas_portas = []
    for p in portas:
        destino = ''
        porta_destino = ''
        if p.destino_porta_id:
            destino = p.destino_porta.dispositivo.item.nome
            porta_destino = p.destino_porta.rotulo or str(p.destino_porta.numero)
        elif p.destino_item_id:
            destino = p.destino_item.nome
        linhas_portas.append((
            p.dispositivo.item.nome,
            p.dispositivo.item.localidade.local if p.dispositivo.item.localidade else '',
            p.rotulo or str(p.numero), p.get_tipo_display(), p.get_status_display(),
            p.get_velocidade_display() if p.velocidade else '',
            'Sim' if p.poe else 'Não', p.vlan, p.get_meio_display(),
            destino, porta_destino, p.descricao,
        ))
    ws = wb.create_sheet('Portas')
    montar_aba(
        ws, 'Portas de switch',
        ['Equipamento', 'Localidade', 'Porta', 'Tipo', 'Situação', 'Velocidade',
         'PoE', 'VLAN', 'Meio', 'Destino', 'Porta destino', 'Descrição'],
        [32, 24, 12, 16, 18, 13, 7, 11, 15, 32, 13, 30],
        linhas_portas,
    )

    # ── Conferência ──────────────────────────────────────────────────────────
    ws = wb.create_sheet('Conferência')
    montar_aba(
        ws, 'Conferência técnica',
        ['Gravidade', 'Verificação', 'Detalhe', 'Equipamentos'],
        [14, 52, 64, 14],
        [
            (a['nivel'].upper(), a['titulo'], a['detalhe'], len(a['nos']))
            for a in grafo['avisos']
        ],
    )
    ws.cell(row=len(grafo['avisos']) + 6, column=1, value=(
        'Conferência apurada na emissão desta planilha, pelos mesmos critérios '
        'da tela do Mapa de Rede.'
    )).font = font_rodape

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    nome = f"mapa_rede_{timezone.localtime().strftime('%Y%m%d_%H%M')}.xlsx"
    resp = HttpResponse(
        buf.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    resp['Content-Disposition'] = f'attachment; filename="{nome}"'
    return resp
