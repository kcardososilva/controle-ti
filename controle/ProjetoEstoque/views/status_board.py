from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from ..models import PlantaProjeto
from services import prtg_service

_SHAPE_TYPES = {'quadro', 'circulo', 'linha', 'texto'}

_TIPO_META = {
    'camera':       ('Câmera',        'camera',        '#5856d6'),
    'access_point': ('Access Point',  'wifi',          '#30b0c7'),
    'switch':       ('Switch',        'network-wired', '#0071e3'),
    'rack':         ('Rack',          'server',        '#8e8e93'),
    'desktop':      ('Desktop',       'desktop',       '#34c759'),
    'impressora':   ('Impressora',    'print',         '#ff9500'),
    'nobreak':      ('Nobreak',       'bolt',          '#ff6b35'),
    'servidor':     ('Servidor',      'cloud',         '#5ac8fa'),
    'ponto_rede':   ('Ponto de Rede', 'circle-dot',    '#6e6e73'),
}


@login_required
def status_board(request):
    tv_mode  = request.GET.get('tv', '') == '1'
    interval = max(15, min(300, int(request.GET.get('interval', 30))))

    devices   = []
    seen_prtg = set()

    for planta in PlantaProjeto.objects.select_related('localidade').order_by(
        'localidade__local', 'nome'
    ):
        loc = planta.localidade.local if planta.localidade else 'Sem localidade'
        for el in planta.layout.get('elements', []):
            if el.get('type') in _SHAPE_TYPES:
                continue
            prtg_objid = el.get('prtg_objid')
            # Deduplica pelo prtg_objid; elementos sem PRTG são sempre incluídos
            key = str(prtg_objid) if prtg_objid else f"noprtg_{el.get('id', '')}"
            if key in seen_prtg:
                continue
            seen_prtg.add(key)

            tipo = el.get('type', 'switch')
            tipo_label, icon, default_color = _TIPO_META.get(tipo, (tipo, 'circle', '#6e6e73'))
            devices.append({
                'id':          el.get('id', ''),
                'prtg_objid':  prtg_objid,
                'label':       el.get('label') or tipo_label,
                'type':        tipo,
                'type_label':  tipo_label,
                'icon':        icon,
                'color':       el.get('color') or default_color,
                'ip':          el.get('ip', ''),
                'item_id':     el.get('item_id'),
                'localidade':  loc,
                'planta_nome': planta.nome,
                'planta_pk':   planta.pk,
            })

    return render(request, 'front/status_board/status_board.html', {
        'devices_json': devices,
        'prtg_ok':      prtg_service.is_configured(),
        'tv_mode':      tv_mode,
        'interval':     interval,
    })
