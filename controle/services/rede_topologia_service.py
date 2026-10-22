"""
Mapa de Rede — o grafo da infraestrutura da empresa.

Este service é a fonte única de três coisas que a tela, o relatório e o Excel
precisam concordar:

  1. **O grafo** (`montar_grafo`): quais equipamentos são rede, em que camada
     eles estão, e que cabo liga um ao outro. O enlace é lido das PORTAS
     (`RedePorta.destino_porta` / `destino_item`), nunca do desenho — ver o
     comentário no bloco de models. Um mapa, portanto, não pode discordar do
     inventário de cabeamento.

  2. **O catálogo de modelos** (`MODELOS`): quantas portas um switch tem, de
     que tipo, com que rótulo de fabricante. Sem isso, cadastrar as 24 portas
     de um Catalyst é digitação pura — e digitação pura em 81 switches não
     acontece. Os modelos vieram do catálogo do Plantas Pro (Cisco Catalyst
     1000 / 9200L / CBS350 / IE3300, Meraki MS120) mais os genéricos.

  3. **As validações técnicas** (`validar`): IP repetido, switch sem uplink,
     porta marcada como ativa e sem destino, AP pendurado em nada, ocupação
     no limite. É o que transforma o mapa de desenho em conferência — e é o
     que o gestor pergunta primeiro ao olhar a tela.

Nada aqui importa view nem template: a view monta a resposta, o service
responde as perguntas.
"""
from __future__ import annotations

import re
from collections import defaultdict
from math import asin, cos, radians, sin, sqrt

from django.db import transaction

from ProjetoEstoque.models import (
    AreaOperacionalChoices,
    Item,
    RedeDispositivo,
    RedeMeioChoices,
    RedePapelChoices,
    RedePorta,
    RedePortaStatusChoices,
    RedePortaTipoChoices,
    RedeRack,
    RedeRota,
    RedeRotaInstalacaoChoices,
    RedeRotaTipoChoices,
    RedeVelocidadeChoices,
)

from . import prtg_service

# ──────────────────────────────────────────────────────────────────────────────
# Que subtipo do inventário é equipamento de REDE, e em que camada ele entra
# ──────────────────────────────────────────────────────────────────────────────
# A chave é o nome do `Subtipo` como ele está cadastrado (a base tem acentuação
# e caixa irregulares — 'Switches', 'Access-Point', 'STARLINK'), então a
# comparação é feita por `_norm`. O valor é (classe no desenho, papel padrão).
#
# O papel aqui é só o PADRÃO de quem nunca foi classificado: `RedeDispositivo.
# papel` manda quando existe. Sem um padrão, todo switch novo nasceria na
# camada de acesso ou, pior, fora do desenho.
_CLASSES = {
    # Núcleo e borda
    'starlink':            ('starlink',  RedePapelChoices.BORDA),
    'firewall':            ('firewall',  RedePapelChoices.BORDA),
    'fortinet':            ('firewall',  RedePapelChoices.BORDA),
    'roteador':            ('roteador',  RedePapelChoices.BORDA),
    'algcom':              ('radio',     RedePapelChoices.DISTRIBUICAO),
    # Comutação
    'switches':            ('switch',    RedePapelChoices.ACESSO),
    'meraki':              ('switch',    RedePapelChoices.DISTRIBUICAO),
    'ciscowlc':            ('wlc',       RedePapelChoices.DISTRIBUICAO),
    'patchpanelpoe':       ('patch',     RedePapelChoices.ACESSO),
    'powerinjector':       ('injector',  RedePapelChoices.ACESSO),
    'conversoresdemidia':  ('conversor', RedePapelChoices.ACESSO),
    'gbic':                ('gbic',      RedePapelChoices.ACESSO),
    # Borda de acesso sem fio
    'accesspoint':         ('ap',        RedePapelChoices.TERMINAL),
    'antenapainel':        ('antena',    RedePapelChoices.TERMINAL),
    'antenapaineloutdor':  ('antena',    RedePapelChoices.TERMINAL),
    'antenaomnidirecional':('antena',    RedePapelChoices.TERMINAL),
    'antenabastao':        ('antena',    RedePapelChoices.TERMINAL),
    'antenaarspe':         ('antena',    RedePapelChoices.TERMINAL),
    # Terminais
    'camera':              ('camera',    RedePapelChoices.TERMINAL),
    'nvr':                 ('nvr',       RedePapelChoices.ACESSO),
    'dvr':                 ('nvr',       RedePapelChoices.ACESSO),
    'desktop':             ('desktop',   RedePapelChoices.TERMINAL),
    'notebook':            ('notebook',  RedePapelChoices.TERMINAL),
    'impressora':          ('impressora', RedePapelChoices.TERMINAL),
    'ipphone':             ('telefone',  RedePapelChoices.TERMINAL),
    'nobreak':             ('nobreak',   RedePapelChoices.TERMINAL),
    'estabilizador':       ('nobreak',   RedePapelChoices.TERMINAL),
}

# Camadas do desenho, de cima para baixo. O auto-arranjo usa esta ordem.
_ORDEM_PAPEL = [
    RedePapelChoices.BORDA,
    RedePapelChoices.CORE,
    RedePapelChoices.DISTRIBUICAO,
    RedePapelChoices.ACESSO,
    RedePapelChoices.TERMINAL,
]

# Subtipos que, por padrão, entram no mapa mesmo sem ter sido classificados.
# Fora desta lista o ativo só aparece se alguém criar o `RedeDispositivo`.
_SUBTIPOS_NUCLEO = {'switches', 'meraki', 'accesspoint', 'ciscowlc', 'firewall',
                    'fortinet', 'roteador', 'starlink', 'nvr', 'dvr'}


def _norm(texto: str) -> str:
    """Nome de subtipo comparável: sem acento, sem pontuação, minúsculo."""
    if not texto:
        return ''
    trocas = str.maketrans('áàâãäéèêëíìîïóòôõöúùûüçÁÀÂÃÄÉÈÊËÍÌÎÏÓÒÔÕÖÚÙÛÜÇ',
                           'aaaaaeeeeiiiiooooouuuucAAAAAEEEEIIIIOOOOOUUUUC')
    return re.sub(r'[^a-z0-9]', '', texto.translate(trocas).lower())


def classe_de(subtipo_nome: str) -> tuple[str, str]:
    """(classe de desenho, papel padrão) de um subtipo do inventário."""
    return _CLASSES.get(_norm(subtipo_nome), ('generico', RedePapelChoices.TERMINAL))


# ──────────────────────────────────────────────────────────────────────────────
# Catálogo de modelos — quantas portas, de que tipo, com que rótulo
# ──────────────────────────────────────────────────────────────────────────────
# `rotulo` é o padrão do fabricante com `{n}` no lugar do número: num Catalyst
# a porta 12 é "Gi1/0/12", e é assim que ela está escrita na etiqueta do rack.
# Sem o rótulo certo, conferir o mapa contra o equipamento vira adivinhação.
MODELOS: dict[str, dict] = {
    'cisco_c1000_8p': {
        'nome': 'Cisco Catalyst 1000 — 8p PoE+',
        'acessos': 8, 'uplinks': 2, 'poe': True,
        'velocidade': RedeVelocidadeChoices.GE,
        'rotulo': 'Gi1/0/{n}', 'rotulo_uplink': 'Gi1/1/{n}',
        'poe_watts': 67, 'fabricante': 'Cisco',
    },
    'cisco_c1000_24p': {
        'nome': 'Cisco Catalyst 1000 — 24p PoE+',
        'acessos': 24, 'uplinks': 4, 'poe': True,
        'velocidade': RedeVelocidadeChoices.GE,
        'rotulo': 'Gi1/0/{n}', 'rotulo_uplink': 'Gi1/1/{n}',
        'poe_watts': 195, 'fabricante': 'Cisco',
    },
    'cisco_c1000_48p': {
        'nome': 'Cisco Catalyst 1000 — 48p PoE+',
        'acessos': 48, 'uplinks': 4, 'poe': True,
        'velocidade': RedeVelocidadeChoices.GE,
        'rotulo': 'Gi1/0/{n}', 'rotulo_uplink': 'Gi1/1/{n}',
        'poe_watts': 370, 'fabricante': 'Cisco',
    },
    'cisco_c9200l_24p': {
        'nome': 'Cisco Catalyst 9200L — 24p PoE+',
        'acessos': 24, 'uplinks': 4, 'poe': True,
        'velocidade': RedeVelocidadeChoices.GE,
        'rotulo': 'Gi1/0/{n}', 'rotulo_uplink': 'Te1/1/{n}',
        'poe_watts': 370, 'fabricante': 'Cisco',
    },
    'cisco_c9200l_48p': {
        'nome': 'Cisco Catalyst 9200L — 48p PoE+',
        'acessos': 48, 'uplinks': 4, 'poe': True,
        'velocidade': RedeVelocidadeChoices.GE,
        'rotulo': 'Gi1/0/{n}', 'rotulo_uplink': 'Te1/1/{n}',
        'poe_watts': 740, 'fabricante': 'Cisco',
    },
    'cisco_cbs350_12p': {
        'nome': 'Cisco Business CBS350 — 12p multigigabit',
        'acessos': 12, 'uplinks': 4, 'poe': True,
        'velocidade': RedeVelocidadeChoices.MGE,
        'rotulo': 'Gi{n}', 'rotulo_uplink': 'Te{n}',
        'poe_watts': 375, 'fabricante': 'Cisco',
    },
    'cisco_cbs350_24p': {
        'nome': 'Cisco Business CBS350 — 24p PoE+',
        'acessos': 24, 'uplinks': 4, 'poe': True,
        'velocidade': RedeVelocidadeChoices.GE,
        'rotulo': 'Gi{n}', 'rotulo_uplink': 'Gi{n}',
        'poe_watts': 195, 'fabricante': 'Cisco',
    },
    'cisco_cbs350_48p': {
        'nome': 'Cisco Business CBS350 — 48p PoE+',
        'acessos': 48, 'uplinks': 4, 'poe': True,
        'velocidade': RedeVelocidadeChoices.GE,
        'rotulo': 'Gi{n}', 'rotulo_uplink': 'Gi{n}',
        'poe_watts': 370, 'fabricante': 'Cisco',
    },
    'cisco_ie3300_8p': {
        'nome': 'Cisco Catalyst IE3300 — 8p industrial',
        'acessos': 8, 'uplinks': 2, 'poe': True,
        'velocidade': RedeVelocidadeChoices.GE,
        'rotulo': 'Gi1/{n}', 'rotulo_uplink': 'Gi1/1{n}',
        'poe_watts': 240, 'fabricante': 'Cisco',
    },
    'meraki_ms120_8': {
        'nome': 'Meraki MS120-8LP — 8p PoE+',
        'acessos': 8, 'uplinks': 2, 'poe': True,
        'velocidade': RedeVelocidadeChoices.GE,
        'rotulo': '{n}', 'rotulo_uplink': 'SFP{n}',
        'poe_watts': 67, 'fabricante': 'Cisco Meraki',
    },
    'meraki_ms120_24': {
        'nome': 'Meraki MS120-24P — 24p PoE+',
        'acessos': 24, 'uplinks': 4, 'poe': True,
        'velocidade': RedeVelocidadeChoices.GE,
        'rotulo': '{n}', 'rotulo_uplink': 'SFP{n}',
        'poe_watts': 370, 'fabricante': 'Cisco Meraki',
    },
    'meraki_ms120_48': {
        'nome': 'Meraki MS120-48LP — 48p PoE+',
        'acessos': 48, 'uplinks': 4, 'poe': True,
        'velocidade': RedeVelocidadeChoices.GE,
        'rotulo': '{n}', 'rotulo_uplink': 'SFP{n}',
        'poe_watts': 370, 'fabricante': 'Cisco Meraki',
    },
    'generico_8': {
        'nome': 'Switch genérico — 8 portas',
        'acessos': 8, 'uplinks': 0, 'poe': False,
        'velocidade': RedeVelocidadeChoices.GE,
        'rotulo': '{n}', 'rotulo_uplink': 'SFP{n}',
        'poe_watts': 0, 'fabricante': '',
    },
    'generico_16': {
        'nome': 'Switch genérico — 16 portas',
        'acessos': 16, 'uplinks': 0, 'poe': False,
        'velocidade': RedeVelocidadeChoices.GE,
        'rotulo': '{n}', 'rotulo_uplink': 'SFP{n}',
        'poe_watts': 0, 'fabricante': '',
    },
    'generico_24': {
        'nome': 'Switch genérico — 24 portas + 2 SFP',
        'acessos': 24, 'uplinks': 2, 'poe': False,
        'velocidade': RedeVelocidadeChoices.GE,
        'rotulo': '{n}', 'rotulo_uplink': 'SFP{n}',
        'poe_watts': 0, 'fabricante': '',
    },
    'generico_48': {
        'nome': 'Switch genérico — 48 portas + 4 SFP',
        'acessos': 48, 'uplinks': 4, 'poe': False,
        'velocidade': RedeVelocidadeChoices.GE,
        'rotulo': '{n}', 'rotulo_uplink': 'SFP{n}',
        'poe_watts': 0, 'fabricante': '',
    },
    'patch_24': {
        'nome': 'Patch panel — 24 posições',
        'acessos': 24, 'uplinks': 0, 'poe': False,
        'velocidade': '',
        'rotulo': 'P{n}', 'rotulo_uplink': '',
        'poe_watts': 0, 'fabricante': '',
    },
    'terminal_1': {
        'nome': 'Terminal — 1 porta de rede',
        'acessos': 1, 'uplinks': 0, 'poe': False,
        'velocidade': RedeVelocidadeChoices.GE,
        'rotulo': 'ETH{n}', 'rotulo_uplink': '',
        'poe_watts': 0, 'fabricante': '',
    },
}


def catalogo_modelos() -> list[dict]:
    """O catálogo em forma de lista, para o seletor da tela."""
    saida = []
    for chave, m in MODELOS.items():
        saida.append({
            'chave': chave,
            'nome': m['nome'],
            'portas': m['acessos'] + m['uplinks'],
            'acessos': m['acessos'],
            'uplinks': m['uplinks'],
            'poe': m['poe'],
            'poe_watts': m['poe_watts'],
            'fabricante': m['fabricante'],
        })
    return saida


def modelo_sugerido(item: Item) -> str:
    """
    Palpite de modelo a partir de marca/modelo escritos no inventário.

    Serve para o botão "gerar portas" já vir com o modelo certo marcado na
    maioria dos switches Cisco, que é o que a empresa tem. Palpite errado não
    estraga nada: o usuário troca no seletor antes de gerar.
    """
    texto = _norm(f"{item.marca or ''} {item.modelo or ''} {item.nome or ''}")
    if not texto:
        return ''
    if 'meraki' in texto or 'ms120' in texto:
        for n in ('48', '24', '8'):
            if n in texto:
                return f'meraki_ms120_{n}'
        return 'meraki_ms120_24'
    if 'ie3300' in texto:
        return 'cisco_ie3300_8p'
    if 'cbs350' in texto or 'business' in texto:
        for n in ('48', '24', '12'):
            if n in texto:
                return f'cisco_cbs350_{n}p'
        return 'cisco_cbs350_24p'
    if '9200' in texto:
        return 'cisco_c9200l_48p' if '48' in texto else 'cisco_c9200l_24p'
    if 'c1000' in texto or 'catalyst' in texto or 'cisco' in texto:
        for n in ('48', '24', '8'):
            if n in texto:
                return f'cisco_c1000_{n}p'
        return 'cisco_c1000_24p'
    for n in ('48', '24', '16', '8'):
        if n in texto:
            return f'generico_{n}'
    return ''


# ──────────────────────────────────────────────────────────────────────────────
# Dispositivo e portas
# ──────────────────────────────────────────────────────────────────────────────

def dispositivo_de(item: Item, usuario=None) -> RedeDispositivo:
    """
    O `RedeDispositivo` do ativo, criando-o na hora se ainda não existir.

    Criar sob demanda é deliberado: obrigar um cadastro prévio para pôr um
    switch no mapa deixaria o mapa vazio no dia em que ele mais precisa estar
    cheio. O papel padrão vem do subtipo (ver `_CLASSES`).
    """
    _, papel_padrao = classe_de(item.subtipo.nome if item.subtipo else '')
    disp, criado = RedeDispositivo.objects.get_or_create(
        item=item,
        defaults={
            'papel': papel_padrao,
            'modelo_portas': modelo_sugerido(item),
            'criado_por': usuario,
            'atualizado_por': usuario,
        },
    )
    return disp


@transaction.atomic
def gerar_portas(dispositivo: RedeDispositivo, modelo_chave: str,
                 usuario=None, substituir: bool = False) -> int:
    """
    Cria as portas de um modelo do catálogo. Devolve quantas foram criadas.

    `substituir=False` (o padrão) **preserva** o que já existe: as portas que
    já têm cabo ou observação são o trabalho de campo de alguém, e regerar por
    cima apagaria o mapeamento inteiro de um rack sem aviso. Só as posições que
    faltam são criadas. Com `substituir=True` as portas SEM enlace são
    recriadas; as com enlace continuam intactas de qualquer modo.
    """
    modelo = MODELOS.get(modelo_chave)
    if not modelo:
        raise ValueError(f"Modelo desconhecido: {modelo_chave}")

    existentes = {p.numero: p for p in dispositivo.portas.all()}
    if substituir:
        livres = [p.pk for p in existentes.values() if not p.tem_enlace]
        if livres:
            RedePorta.objects.filter(pk__in=livres).delete()
            existentes = {p.numero: p for p in dispositivo.portas.all()}

    novas = []
    total_acesso = modelo['acessos']
    for i in range(1, total_acesso + 1):
        if i in existentes:
            continue
        novas.append(RedePorta(
            dispositivo=dispositivo,
            numero=i,
            rotulo=modelo['rotulo'].format(n=i) if modelo['rotulo'] else str(i),
            tipo=RedePortaTipoChoices.RJ45,
            status=RedePortaStatusChoices.LIVRE,
            velocidade=modelo['velocidade'],
            poe=modelo['poe'],
            meio=RedeMeioChoices.UTP,
            criado_por=usuario,
            atualizado_por=usuario,
        ))
    for j in range(1, modelo['uplinks'] + 1):
        numero = total_acesso + j
        if numero in existentes:
            continue
        novas.append(RedePorta(
            dispositivo=dispositivo,
            numero=numero,
            rotulo=(modelo['rotulo_uplink'] or 'SFP{n}').format(n=j),
            tipo=RedePortaTipoChoices.SFP,
            status=RedePortaStatusChoices.LIVRE,
            velocidade=modelo['velocidade'],
            poe=False,
            meio=RedeMeioChoices.FIBRA,
            criado_por=usuario,
            atualizado_por=usuario,
        ))

    if novas:
        RedePorta.objects.bulk_create(novas)
    dispositivo.modelo_portas = modelo_chave
    dispositivo.atualizado_por = usuario
    dispositivo.save(update_fields=['modelo_portas', 'atualizado_por', 'updated_at'])
    return len(novas)


class EnlaceInvalido(ValueError):
    """Tentativa de ligar duas portas que não podem ser ligadas."""


@transaction.atomic
def conectar(porta: RedePorta, destino_porta: RedePorta | None = None,
             destino_item: Item | None = None,
             meio: str = RedeMeioChoices.UTP, usuario=None) -> None:
    """
    Liga uma porta ao outro lado do cabo, nos DOIS sentidos.

    É a única porta de entrada para criar enlace. Gravar `destino_porta` na mão
    deixaria o grafo com uma perna só: o mapa mostraria o cabo saindo do switch
    e o relatório de portas do outro equipamento diria que ele está livre.

    Com `destino_porta`, o par é simétrico e as duas portas vão para `ativa`.
    Com apenas `destino_item`, registra-se o destino sem exigir que ele tenha
    portas cadastradas (câmera, desktop, impressora).
    """
    if destino_porta is None and destino_item is None:
        raise EnlaceInvalido("Informe a porta ou o equipamento de destino.")

    if destino_porta is not None:
        if destino_porta.pk == porta.pk:
            raise EnlaceInvalido("Uma porta não se liga a si mesma.")
        if destino_porta.dispositivo_id == porta.dispositivo_id:
            raise EnlaceInvalido("As duas portas são do mesmo equipamento.")
        # Libera o que estiver no caminho antes de criar o par — sem isso a
        # restrição 1-1 do banco recusa a gravação e o usuário leva um erro
        # que não diz nada sobre o cabo que já estava ali.
        for alvo in (porta, destino_porta):
            _soltar(alvo, usuario)
        porta.destino_porta = destino_porta
        porta.destino_item = destino_porta.dispositivo.item
        porta.status = RedePortaStatusChoices.ATIVA
        porta.meio = meio
        porta.atualizado_por = usuario
        porta.save(update_fields=['destino_porta', 'destino_item', 'status',
                                  'meio', 'atualizado_por', 'updated_at'])
        destino_porta.destino_porta = porta
        destino_porta.destino_item = porta.dispositivo.item
        destino_porta.status = RedePortaStatusChoices.ATIVA
        destino_porta.meio = meio
        destino_porta.atualizado_por = usuario
        destino_porta.save(update_fields=['destino_porta', 'destino_item', 'status',
                                          'meio', 'atualizado_por', 'updated_at'])
        return

    _soltar(porta, usuario)
    porta.destino_item = destino_item
    porta.status = RedePortaStatusChoices.ATIVA
    porta.meio = meio
    porta.atualizado_por = usuario
    porta.save(update_fields=['destino_item', 'destino_porta', 'status',
                              'meio', 'atualizado_por', 'updated_at'])


def _soltar(porta: RedePorta, usuario=None) -> None:
    """Desfaz o par da porta (e o do outro lado), sem mexer na situação dela."""
    par = porta.destino_porta
    porta.destino_porta = None
    if par is not None:
        par.destino_porta = None
        par.destino_item = None
        par.status = RedePortaStatusChoices.LIVRE
        par.atualizado_por = usuario
        par.save(update_fields=['destino_porta', 'destino_item', 'status',
                                'atualizado_por', 'updated_at'])


@transaction.atomic
def desconectar(porta: RedePorta, usuario=None) -> None:
    """Retira o cabo da porta e devolve as duas pontas para 'livre'."""
    _soltar(porta, usuario)
    porta.destino_item = None
    porta.status = RedePortaStatusChoices.LIVRE
    porta.atualizado_por = usuario
    porta.save(update_fields=['destino_porta', 'destino_item', 'status',
                              'atualizado_por', 'updated_at'])


# ──────────────────────────────────────────────────────────────────────────────
# O grafo
# ──────────────────────────────────────────────────────────────────────────────

def _status_prtg(objid, mapa_prtg) -> dict:
    """Status efetivo do nó, já resolvido pelo prtg_service (regra 13)."""
    if not objid or not mapa_prtg:
        return {'slug': 'nao_monitorado', 'texto': 'Sem monitoramento', 'uptime': None}
    dev = mapa_prtg.get(int(objid))
    if not dev:
        return {'slug': 'nao_monitorado', 'texto': 'Objeto não encontrado no PRTG', 'uptime': None}
    return {
        'slug': dev['status_slug'],
        'texto': dev.get('statustext') or '',
        'uptime': dev.get('uptime_pct'),
        'nome_prtg': dev.get('name') or '',
        'host': dev.get('host') or '',
    }


def montar_grafo(localidade_id=None, area=None, incluir_terminais=True,
                 com_status=True) -> dict:
    """
    Monta o grafo da rede no escopo pedido.

    Devolve `{'nos': [...], 'enlaces': [...], 'kpis': {...}, 'avisos': [...]}`.

    O universo são os `RedeDispositivo` existentes MAIS os ativos de subtipo de
    núcleo (`_SUBTIPOS_NUCLEO`) que ainda não têm faceta de rede — assim um
    switch recém-cadastrado aparece no mapa no mesmo dia, como nó "não
    mapeado", em vez de ficar invisível até alguém lembrar de cadastrá-lo.

    Desempenho: três consultas no total (dispositivos + portas + itens soltos),
    com `select_related` em tudo que a resposta lê. O mapa do PRTG vem do cache
    de 30s do `prtg_service`.
    """
    # ── 1. Dispositivos já com faceta de rede ────────────────────────────────
    disp_qs = (
        RedeDispositivo.objects
        .select_related('item', 'item__localidade', 'item__subtipo')
        .filter(item__excluido=False)
    )
    if localidade_id:
        disp_qs = disp_qs.filter(item__localidade_id=localidade_id)
    if area:
        disp_qs = disp_qs.filter(item__area_operacional=area)
    dispositivos = list(disp_qs)

    # ── 2. Ativos de núcleo ainda sem faceta ─────────────────────────────────
    soltos_qs = (
        Item.objects
        .select_related('localidade', 'subtipo')
        .filter(status='ativo')
        .exclude(rede__isnull=False)
    )
    if localidade_id:
        soltos_qs = soltos_qs.filter(localidade_id=localidade_id)
    if area:
        soltos_qs = soltos_qs.filter(area_operacional=area)
    soltos = [i for i in soltos_qs if _norm(i.subtipo.nome if i.subtipo else '') in _SUBTIPOS_NUCLEO]

    # ── 3. Portas de todos os dispositivos do escopo ─────────────────────────
    portas = list(
        RedePorta.objects
        .filter(dispositivo__in=dispositivos)
        .select_related('dispositivo', 'destino_item', 'destino_porta',
                        'destino_porta__dispositivo', 'rota')
        .order_by('dispositivo_id', 'numero')
    )
    portas_por_disp: dict[int, list[RedePorta]] = defaultdict(list)
    for p in portas:
        portas_por_disp[p.dispositivo_id].append(p)

    # ── 3b. A outra ponta do cabo entra no mapa, mesmo fora do escopo ────────
    # Uma porta pode apontar para equipamento que não é de subtipo de núcleo
    # (câmera, desktop, impressora) nem tem faceta de rede. Sem acrescentá-lo
    # aqui, o nó não existe e o ENLACE É DESCARTADO em silêncio mais abaixo:
    # a tela mostraria a porta como ativa e nenhum cabo saindo dela. O mesmo
    # vale para o equipamento de outra localidade — o cabo atravessa o recorte,
    # e esconder isso faria o mapa afirmar que ele não existe.
    ja_sao_no = {d.item_id for d in dispositivos} | {i.pk for i in soltos}
    faltam = {p.destino_item_id for p in portas if p.destino_item_id} - ja_sao_no
    pontas = list(
        Item.objects.select_related('localidade', 'subtipo').filter(pk__in=faltam)
    ) if faltam else []
    soltos = soltos + pontas

    mapa_prtg = {}
    if com_status and prtg_service.is_configured():
        try:
            mapa_prtg = prtg_service.get_devices_map()
        except Exception:
            # PRTG fora do ar não derruba o mapa: ele fica sem a camada de
            # status, que é exatamente o que "sem monitoramento" significa.
            mapa_prtg = {}

    # ── 4. Nós ───────────────────────────────────────────────────────────────
    nos = []
    item_para_no: dict[int, str] = {}
    for d in dispositivos:
        item = d.item
        classe, _ = classe_de(item.subtipo.nome if item.subtipo else '')
        lista = portas_por_disp.get(d.pk, [])
        ativas = sum(1 for p in lista if p.status == RedePortaStatusChoices.ATIVA)
        no_id = f"i{item.pk}"
        item_para_no[item.pk] = no_id
        nos.append({
            'id': no_id,
            'item_id': item.pk,
            'dispositivo_id': d.pk,
            'nome': item.nome,
            'classe': classe,
            'papel': d.papel,
            'papel_label': d.get_papel_display(),
            'marca': item.marca or '',
            'modelo': item.modelo or '',
            'serie': item.numero_serie or '',
            'localidade': item.localidade.local if item.localidade else '',
            'localidade_id': item.localidade_id,
            'area': item.area_operacional or '',
            'area_label': item.area_operacional_label,
            'subtipo': item.subtipo.nome if item.subtipo else '',
            'ip': d.ip_gerencia or '',
            'prtg_objid': d.prtg_objid,
            'modelo_portas': d.modelo_portas or '',
            'observacoes': d.observacoes or '',
            'status_item': item.status,
            'portas_total': len(lista),
            'portas_ativas': ativas,
            'portas_livres': sum(1 for p in lista if p.status == RedePortaStatusChoices.LIVRE),
            'ocupacao': round(ativas * 100 / len(lista)) if lista else 0,
            'mapeado': True,
            # O painel em miniatura desenhado DENTRO da caixa do switch na
            # prancha. Uma letra por porta (ver `mapa_compacto`): é o que
            # permite ver porta livre e porta em uso sem abrir o inspetor,
            # sem um segundo pedido por nó e sem inflar a carga do grafo.
            'portas_mapa': mapa_compacto(lista),
            'rack_id': d.rack_id,
            'rack_u': d.rack_u,
            'tem_geo': d.latitude is not None and d.longitude is not None,
            'status': _status_prtg(d.prtg_objid, mapa_prtg),
        })

    for item in soltos:
        classe, papel = classe_de(item.subtipo.nome if item.subtipo else '')
        no_id = f"i{item.pk}"
        item_para_no[item.pk] = no_id
        nos.append({
            'id': no_id,
            'item_id': item.pk,
            'dispositivo_id': None,
            'nome': item.nome,
            'classe': classe,
            'papel': papel,
            'papel_label': dict(RedePapelChoices.choices).get(papel, papel),
            'marca': item.marca or '',
            'modelo': item.modelo or '',
            'serie': item.numero_serie or '',
            'localidade': item.localidade.local if item.localidade else '',
            'localidade_id': item.localidade_id,
            'area': item.area_operacional or '',
            'area_label': item.area_operacional_label,
            'subtipo': item.subtipo.nome if item.subtipo else '',
            'ip': '',
            'prtg_objid': None,
            'modelo_portas': modelo_sugerido(item),
            'observacoes': '',
            'status_item': item.status,
            'portas_total': 0,
            'portas_ativas': 0,
            'portas_livres': 0,
            'ocupacao': 0,
            'mapeado': False,
            'portas_mapa': '',
            'rack_id': None,
            'rack_u': None,
            'tem_geo': False,
            'status': {'slug': 'nao_monitorado', 'texto': 'Sem monitoramento', 'uptime': None},
        })

    if not incluir_terminais:
        nos = [n for n in nos if n['papel'] != RedePapelChoices.TERMINAL]
        validos = {n['item_id'] for n in nos}
        item_para_no = {k: v for k, v in item_para_no.items() if k in validos}

    # ── 5. Enlaces ───────────────────────────────────────────────────────────
    # Cada cabo aparece UMA vez. Quando as duas pontas são portas cadastradas,
    # as duas apontam uma para a outra: a desempate é o menor id de porta.
    enlaces = []
    vistos = set()
    for p in portas:
        if p.dispositivo_id not in {d.pk for d in dispositivos}:
            continue
        origem_item = p.dispositivo.item_id
        if origem_item not in item_para_no:
            continue

        if p.destino_porta_id:
            if p.pk > p.destino_porta_id:
                continue                      # o par já entrou pela outra ponta
            destino_item = p.destino_porta.dispositivo.item_id
            porta_destino_rotulo = p.destino_porta.rotulo or str(p.destino_porta.numero)
            porta_destino_id = p.destino_porta_id
        elif p.destino_item_id:
            destino_item = p.destino_item_id
            porta_destino_rotulo = ''
            porta_destino_id = None
        else:
            continue

        if destino_item not in item_para_no:
            continue
        chave = (min(p.pk, porta_destino_id or 0), max(p.pk, porta_destino_id or 0), destino_item)
        if chave in vistos:
            continue
        vistos.add(chave)

        enlaces.append({
            'id': f"e{p.pk}",
            'de': item_para_no[origem_item],
            'para': item_para_no[destino_item],
            'porta_de_id': p.pk,
            'porta_de': p.rotulo or str(p.numero),
            'porta_para_id': porta_destino_id,
            'porta_para': porta_destino_rotulo,
            'meio': p.meio,
            'velocidade': p.velocidade,
            'vlan': p.vlan,
            'poe': p.poe,
            'descricao': p.descricao,
            # Por onde este cabo passa no terreno. É o que liga o diagrama
            # esquemático à planta georreferenciada: o mesmo enlace aparece
            # nos dois desenhos, e clicar nele num leva ao outro.
            'rota_id': p.rota_id,
            'rota_nome': p.rota.nome if p.rota_id else '',
            'rota_m': float(p.rota.comprimento_m or 0) if p.rota_id else None,
        })

    return {
        'nos': nos,
        'enlaces': enlaces,
        'kpis': kpis(nos, enlaces, portas),
        'avisos': validar(nos, enlaces, portas),
        'prtg_ativo': bool(mapa_prtg),
    }


def kpis(nos: list[dict], enlaces: list[dict], portas: list[RedePorta]) -> dict:
    """Os números que abrem a tela e encabeçam o Excel."""
    por_status = defaultdict(int)
    for n in nos:
        por_status[n['status']['slug']] += 1

    switches = [n for n in nos if n['classe'] == 'switch']
    portas_totais = sum(n['portas_total'] for n in nos)
    portas_ativas = sum(n['portas_ativas'] for n in nos)

    return {
        'nos': len(nos),
        'switches': len(switches),
        'aps': sum(1 for n in nos if n['classe'] == 'ap'),
        'enlaces': len(enlaces),
        'online': por_status.get('up', 0),
        'offline': por_status.get('down', 0),
        'instavel': por_status.get('warning', 0) + por_status.get('unusual', 0),
        'nao_monitorado': por_status.get('nao_monitorado', 0),
        'portas_totais': portas_totais,
        'portas_ativas': portas_ativas,
        'portas_livres': portas_totais - portas_ativas,
        'ocupacao': round(portas_ativas * 100 / portas_totais) if portas_totais else 0,
        'nao_mapeados': sum(1 for n in nos if not n['mapeado']),
        'fibra': sum(1 for e in enlaces if e['meio'] == RedeMeioChoices.FIBRA),
    }


# Severidade dos avisos: o que trava uma instalação vem antes do que só
# incomoda. A tela ordena por esta chave.
_GRAVIDADE = {'erro': 0, 'alerta': 1, 'info': 2}


def validar(nos: list[dict], enlaces: list[dict], portas: list[RedePorta]) -> list[dict]:
    """
    Conferência técnica do mapa (seção 7.8 do escopo do projeto).

    Cada aviso diz o QUE está errado e em QUAL nó — um aviso que não aponta
    para um equipamento não dá para resolver, e vira ruído na tela.
    """
    avisos: list[dict] = []
    por_no = {n['id']: n for n in nos}

    # IP de gerência repetido — duas máquinas com o mesmo IP é queda
    # intermitente que ninguém relaciona com cadastro.
    por_ip = defaultdict(list)
    for n in nos:
        if n['ip']:
            por_ip[n['ip']].append(n)
    for ip, lista in por_ip.items():
        if len(lista) > 1:
            avisos.append({
                'nivel': 'erro',
                'codigo': 'ip_duplicado',
                'titulo': f"IP {ip} repetido em {len(lista)} equipamentos",
                'detalhe': ', '.join(n['nome'] for n in lista[:4]),
                'nos': [n['id'] for n in lista],
            })

    # Switch sem uplink: ele está no desenho sem caminho para o resto da rede.
    graus = defaultdict(int)
    for e in enlaces:
        graus[e['de']] += 1
        graus[e['para']] += 1
    # Um aviso por switch isolado daria 89 linhas iguais no dia em que o mapa
    # começa vazio — e uma lista de 89 avisos idênticos não é lida, é fechada.
    # Agrupar mantém o aviso acionável (ele carrega os nós) sem afogar os
    # outros, que são os que apontam defeito de verdade.
    isolados = [n for n in nos if n['classe'] in ('switch', 'wlc') and graus[n['id']] == 0]
    if isolados:
        avisos.append({
            'nivel': 'alerta',
            'codigo': 'sem_enlace',
            'titulo': f"{len(isolados)} switch(es) sem nenhum enlace cadastrado",
            'detalhe': 'Aparecem isolados no mapa — falta mapear o uplink. '
                       + ', '.join(n['nome'] for n in isolados[:5])
                       + ('…' if len(isolados) > 5 else ''),
            'nos': [n['id'] for n in isolados],
        })

    # Porta marcada como ativa e sem destino — o inverso de um cabo esquecido.
    orfas = defaultdict(int)
    for p in portas:
        if p.status == RedePortaStatusChoices.ATIVA and not p.tem_enlace:
            orfas[f"i{p.dispositivo.item_id}"] += 1
    for no_id, n_portas in orfas.items():
        no = por_no.get(no_id)
        if not no:
            continue
        avisos.append({
            'nivel': 'alerta',
            'codigo': 'porta_ativa_sem_destino',
            'titulo': f"{no['nome']}: {n_portas} porta(s) ativa(s) sem destino",
            'detalhe': 'A porta está marcada como em uso, mas o outro lado do cabo não foi informado.',
            'nos': [no_id],
        })

    # AP / câmera pendurada em nada.
    soltos = [n for n in nos
              if n['classe'] in ('ap', 'camera') and graus[n['id']] == 0 and n['mapeado']]
    if soltos:
        avisos.append({
            'nivel': 'info',
            'codigo': 'terminal_solto',
            'titulo': f"{len(soltos)} equipamento(s) de ponta sem switch de origem",
            'detalhe': 'Nenhuma porta de switch aponta para eles. '
                       + ', '.join(n['nome'] for n in soltos[:5])
                       + ('…' if len(soltos) > 5 else ''),
            'nos': [n['id'] for n in soltos],
        })

    # Ocupação no limite: planejar antes de a próxima instalação travar.
    for n in nos:
        if n['portas_total'] >= 8 and n['ocupacao'] >= 90:
            avisos.append({
                'nivel': 'alerta' if n['ocupacao'] >= 100 else 'info',
                'codigo': 'ocupacao_limite',
                'titulo': f"{n['nome']} com {n['ocupacao']}% das portas em uso",
                'detalhe': f"{n['portas_ativas']} de {n['portas_total']} portas. "
                           f"Restam {n['portas_livres']} livres.",
                'nos': [n['id']],
            })

    # Equipamento de rede sem vínculo de monitoramento.
    sem_prtg = [n for n in nos
                if n['classe'] in ('switch', 'ap', 'wlc', 'firewall', 'roteador')
                and not n['prtg_objid']]
    if sem_prtg:
        avisos.append({
            'nivel': 'info',
            'codigo': 'sem_prtg',
            'titulo': f"{len(sem_prtg)} equipamento(s) de rede sem vínculo com o PRTG",
            'detalhe': 'Sem o vínculo, a queda deles não acende no mapa.',
            'nos': [n['id'] for n in sem_prtg],
        })

    # Enlace de fibra sem rota física apontada. É a ponte entre os dois
    # desenhos: o cabo existe no cadastro, mas ninguém sabe por onde ele
    # passa — e no dia do rompimento a pergunta "quem cai?" não tem resposta.
    fibra_solta = [e for e in enlaces
                   if e['meio'] == RedeMeioChoices.FIBRA and not e.get('rota_id')]
    if fibra_solta:
        avisos.append({
            'nivel': 'info',
            'codigo': 'fibra_sem_rota',
            'titulo': f"{len(fibra_solta)} enlace(s) de fibra sem rota física",
            'detalhe': 'Aponte a rota na ficha da porta para o lance entrar na planta '
                       'e no cálculo de metros.',
            'nos': sorted({e['de'] for e in fibra_solta} | {e['para'] for e in fibra_solta}),
        })

    # Switch no mapa sem nenhuma porta cadastrada — não há o que conferir nele.
    sem_portas = [n for n in nos if n['classe'] == 'switch' and n['portas_total'] == 0]
    if sem_portas:
        avisos.append({
            'nivel': 'info',
            'codigo': 'sem_portas',
            'titulo': f"{len(sem_portas)} switch(es) sem portas cadastradas",
            'detalhe': 'Use "Gerar portas" na ficha do equipamento para criar o painel.',
            'nos': [n['id'] for n in sem_portas],
        })

    avisos.sort(key=lambda a: _GRAVIDADE.get(a['nivel'], 3))
    return avisos


# ──────────────────────────────────────────────────────────────────────────────
# Auto-arranjo
# ──────────────────────────────────────────────────────────────────────────────
# Geometria do arranjo automático. São os únicos números "mágicos" do service,
# e estão aqui e não no JS porque o Excel e o PNG do servidor usam os mesmos.
_LARG_NO   = 190
_ALT_NO    = 76
_GAP_X     = 60
_GAP_Y     = 150
_MARGEM    = 80


def auto_arranjo(nos: list[dict], enlaces: list[dict], agrupar_por: str = 'papel') -> dict:
    """
    Posição de cada nó, em camadas.

    `agrupar_por='papel'` empilha por camada da rede (borda em cima, terminal
    embaixo) — é a leitura de diagrama. `'localidade'` faz uma faixa por local,
    que é como o TI conversa sobre o parque ("o switch do alojamento 4").
    `'arvore'` segue o CABO (ver `arranjo_arvore`) e é o arranjo mais limpo
    quando a rede é hierárquica, que é o caso aqui.

    Dentro da faixa, os nós são ordenados pelo PAI quando ele existe: filho
    embaixo do pai evita a teia de linhas atravessando o desenho, que é o que
    faz um diagrama automático parecer errado mesmo estando certo.
    """
    if agrupar_por == 'arvore':
        return arranjo_arvore(nos, enlaces)

    if agrupar_por == 'localidade':
        chaves = sorted({n['localidade'] or 'Sem localidade' for n in nos})
        def chave_de(n):
            return n['localidade'] or 'Sem localidade'
    else:
        presentes = {n['papel'] for n in nos}
        chaves = [p for p in _ORDEM_PAPEL if p in presentes]
        def chave_de(n):
            return n['papel']

    # pai = o primeiro vizinho de camada superior
    nivel = {p: i for i, p in enumerate(_ORDEM_PAPEL)}
    vizinhos = defaultdict(list)
    for e in enlaces:
        vizinhos[e['de']].append(e['para'])
        vizinhos[e['para']].append(e['de'])
    por_id = {n['id']: n for n in nos}

    def pai_de(n):
        meu = nivel.get(n['papel'], 9)
        candidatos = [por_id[v] for v in vizinhos.get(n['id'], []) if v in por_id]
        acima = [c for c in candidatos if nivel.get(c['papel'], 9) < meu]
        if acima:
            return sorted(acima, key=lambda c: c['nome'])[0]['id']
        return ''

    faixas: dict[str, list[dict]] = defaultdict(list)
    for n in nos:
        faixas[chave_de(n)].append(n)

    posicoes: dict[str, dict] = {}
    zonas: list[dict] = []
    y = _MARGEM
    largura_max = 0
    for chave in chaves:
        grupo = sorted(faixas.get(chave, []), key=lambda n: (pai_de(n), n['nome']))
        if not grupo:
            continue
        # Mais de 14 nós numa faixa viram duas linhas: uma fileira de 40 caixas
        # obriga a rolar de lado para ler um diagrama, que é o oposto do que
        # um diagrama serve.
        por_linha = min(max(len(grupo), 1), 14)
        linhas = (len(grupo) + por_linha - 1) // por_linha
        for idx, n in enumerate(grupo):
            col = idx % por_linha
            lin = idx // por_linha
            posicoes[n['id']] = {
                'x': _MARGEM + col * (_LARG_NO + _GAP_X),
                'y': y + lin * (_ALT_NO + 46),
            }
        altura_faixa = linhas * (_ALT_NO + 46)
        largura_faixa = por_linha * (_LARG_NO + _GAP_X)
        largura_max = max(largura_max, largura_faixa)
        rotulo = (dict(RedePapelChoices.choices).get(chave, chave)
                  if agrupar_por == 'papel' else chave)
        zonas.append({
            'id': f"z-{_norm(str(chave)) or 'faixa'}",
            'nome': rotulo,
            'x': _MARGEM - 34,
            'y': y - 38,
            'w': largura_faixa + 20,
            'h': altura_faixa + 44,
            'nos': len(grupo),
        })
        y += altura_faixa + _GAP_Y

    return {
        'nos': posicoes,
        'zonas': zonas,
        'canvas': {
            'width': max(1400, largura_max + _MARGEM * 2),
            'height': max(900, y),
        },
    }


def arranjo_arvore(nos: list[dict], enlaces: list[dict]) -> dict:
    """
    Arranjo que segue o CABO, não a etiqueta — o diagrama limpo.

    O arranjo por camada (`'papel'`) põe cada equipamento na faixa do papel
    dele e ordena pelo nome. Está certo e lê mal: dois switches de acesso
    pendurados em distribuições diferentes ficam vizinhos na faixa, e seus
    uplinks cruzam o desenho inteiro para chegar ao pai. Num mapa de 90
    switches isso é uma teia, e teia faz um diagrama correto parecer errado.

    Aqui a largura é do FILHO. Percurso em pós-ordem: a folha ganha a
    próxima coluna livre, e o pai fica CENTRADO sobre os filhos dele. É o
    arranjo clássico de árvore (Reingold–Tilford sem a compactação de
    subárvore, que a esta escala não paga o custo), e a propriedade que
    importa é esta: o uplink vira um traço curto e vertical, porque o pai
    está exatamente em cima.

    Profundidade vem do cabo — distância em saltos até a borda —, não do
    campo `papel`. Switch classificado como "acesso" pendurado direto no
    firewall aparece onde ele realmente está, e a divergência entre a
    etiqueta e a ligação fica VISÍVEL em vez de corrigida em silêncio.
    """
    por_id = {n['id']: n for n in nos}
    adj: dict[str, set] = defaultdict(set)
    for e in enlaces:
        if e['de'] in por_id and e['para'] in por_id:
            adj[e['de']].add(e['para'])
            adj[e['para']].add(e['de'])

    nivel_papel = {p: i for i, p in enumerate(_ORDEM_PAPEL)}

    # ── Raízes: a borda da rede, ou quem tem mais cabo quando não há borda ──
    raizes = [n['id'] for n in nos
              if n['papel'] in (RedePapelChoices.BORDA, RedePapelChoices.CORE)]
    if not raizes:
        candidatos = sorted(
            nos,
            key=lambda n: (-len(adj[n['id']]), nivel_papel.get(n['papel'], 9), n['nome']),
        )
        raizes = [n['id'] for n in candidatos[:1]]
    raizes.sort(key=lambda i: (nivel_papel.get(por_id[i]['papel'], 9), por_id[i]['nome']))

    # ── Árvore geradora por largura: o primeiro a alcançar vira pai ─────────
    # Largura e não profundidade de propósito: a busca em largura dá a
    # distância MÍNIMA em saltos até a borda, que é a profundidade real do
    # equipamento na rede. Em profundidade, um anel faria um switch de acesso
    # aparecer a sete níveis da borda por acidente da ordem da lista.
    filhos: dict[str, list[str]] = defaultdict(list)
    profundidade: dict[str, int] = {}
    visto: set[str] = set()
    fila = []
    for r in raizes:
        if r in visto:
            continue
        visto.add(r)
        profundidade[r] = 0
        fila.append(r)
    cursor = 0
    while cursor < len(fila):
        atual = fila[cursor]
        cursor += 1
        vizinhos = sorted(adj[atual], key=lambda i: (
            nivel_papel.get(por_id[i]['papel'], 9), por_id[i]['nome']
        ))
        for v in vizinhos:
            if v in visto:
                continue
            visto.add(v)
            profundidade[v] = profundidade[atual] + 1
            filhos[atual].append(v)
            fila.append(v)

    # ── Órfãos: nada liga a eles, e esconder isso seria mentir ─────────────
    # Hoje são 89 switches sem enlace cadastrado. Eles vão para uma faixa
    # própria no pé do desenho, visivelmente separada — misturá-los à árvore
    # os faria parecer ligados a algo.
    orfaos = [n for n in nos if n['id'] not in visto]

    posicoes: dict[str, dict] = {}
    passo_x = _LARG_NO + _GAP_X
    passo_y = _ALT_NO + _GAP_Y
    coluna_livre = [0]

    def colocar(no_id: str) -> float:
        """Pós-ordem: devolve a coluna (em x) em que este nó ficou."""
        meus = filhos.get(no_id) or []
        if not meus:
            x = _MARGEM + coluna_livre[0] * passo_x
            coluna_livre[0] += 1
        else:
            xs = [colocar(f) for f in meus]
            x = (min(xs) + max(xs)) / 2
        posicoes[no_id] = {
            'x': round(x),
            'y': _MARGEM + profundidade[no_id] * passo_y,
        }
        return x

    for r in raizes:
        if r in profundidade:
            colocar(r)
        # Uma raiz isolada da outra ganha respiro: sem o vão, duas árvores
        # vizinhas parecem uma só com o tronco partido.
        coluna_livre[0] += 1

    # ── Faixa dos órfãos, embaixo de tudo ──────────────────────────────────
    zonas: list[dict] = []
    fundo_y = _MARGEM + (max(profundidade.values(), default=0) + 1) * passo_y
    if orfaos:
        por_linha = 10
        for idx, n in enumerate(sorted(orfaos, key=lambda n: (n['localidade'], n['nome']))):
            posicoes[n['id']] = {
                'x': _MARGEM + (idx % por_linha) * passo_x,
                'y': fundo_y + (idx // por_linha) * (_ALT_NO + 46),
            }
        linhas = (len(orfaos) + por_linha - 1) // por_linha
        zonas.append({
            'id': 'z-sem-enlace',
            'nome': f'Sem enlace cadastrado ({len(orfaos)})',
            'x': _MARGEM - 34,
            'y': fundo_y - 38,
            'w': por_linha * passo_x + 20,
            'h': linhas * (_ALT_NO + 46) + 44,
            'nos': len(orfaos),
        })
        fundo_y += linhas * (_ALT_NO + 46) + _GAP_Y

    # ── Zona por profundidade, rotulada pelo papel predominante ────────────
    # O rótulo sai do que está NA faixa, não de uma lista fixa de nomes de
    # camada: a faixa 2 de um parque pode ser toda de acesso e, noutro, de
    # distribuição. Nomear pelo conteúdo descreve; nomear pela posição chuta.
    largura_max = max(
        (p['x'] for p in posicoes.values()), default=_MARGEM
    ) + _LARG_NO
    por_nivel: dict[int, list[dict]] = defaultdict(list)
    for no_id, d in profundidade.items():
        por_nivel[d].append(por_id[no_id])
    rotulos_papel = dict(RedePapelChoices.choices)
    for d in sorted(por_nivel):
        grupo = por_nivel[d]
        conta: dict[str, int] = defaultdict(int)
        for n in grupo:
            conta[n['papel']] += 1
        dominante = max(conta.items(), key=lambda kv: kv[1])[0]
        zonas.insert(d, {
            'id': f'z-nivel-{d}',
            'nome': f"{rotulos_papel.get(dominante, dominante)} · {d} salto(s) da borda"
                    if d else rotulos_papel.get(dominante, dominante),
            'x': _MARGEM - 34,
            'y': _MARGEM + d * passo_y - 30,
            'w': largura_max - _MARGEM + 54,
            'h': _ALT_NO + 46,
            'nos': len(grupo),
        })

    return {
        'nos': posicoes,
        'zonas': zonas,
        'canvas': {
            'width': max(1400, int(largura_max + _MARGEM * 2)),
            'height': max(900, int(fundo_y + _MARGEM)),
        },
    }


# ──────────────────────────────────────────────────────────────────────────────
# Painel de portas de um equipamento
# ──────────────────────────────────────────────────────────────────────────────

# Quantas COLUNAS de porta formam um bloco no painel. Num Catalyst de 24p as
# portas vêm em quatro grupos de seis colunas (doze portas), separados por um
# vão no metal. O vão não é decoração: é por ele que o técnico conta "terceiro
# grupo, segunda coluna" sem percorrer as 24 com o dedo. Um painel na tela em
# fileira corrida obriga a contar, que é justo o que a etiqueta evita.
_COLS_POR_BLOCO = 6

# Código de uma letra por situação de porta, para o mapa compacto que viaja
# junto do nó (ver `montar_grafo`). Maiúscula = RJ45, minúscula = SFP: com 90
# switches de 24 portas, mandar o objeto inteiro de cada porta só para
# desenhar o painel miniatura dentro da caixa seriam ~2 MB de JSON por carga.
_COD_STATUS = {
    RedePortaStatusChoices.ATIVA: 'A',
    RedePortaStatusChoices.LIVRE: 'L',
    RedePortaStatusChoices.RESERVADA: 'R',
    RedePortaStatusChoices.DEFEITO: 'D',
    RedePortaStatusChoices.DESLIGADA: 'X',
}


def mapa_compacto(portas) -> str:
    """Situação de cada porta em uma letra, na ordem da numeração."""
    letras = []
    for p in portas:
        c = _COD_STATUS.get(p.status, 'L')
        letras.append(c.lower() if p.tipo == RedePortaTipoChoices.SFP else c)
    return ''.join(letras)


def painel_portas(dispositivo: RedeDispositivo) -> dict:
    """
    As portas de um equipamento em forma de painel, como se olha o switch.

    `fileira` separa par de ímpar porque é assim que a porta está no
    equipamento: a 1 e a 2 ficam uma sobre a outra, não lado a lado. Sem isso o
    painel da tela não bate com a etiqueta do rack e conferir vira contagem.

    `face` entrega o painel já MONTADO em blocos e colunas, e não uma lista
    plana para a tela agrupar. O arranjo é conhecimento do equipamento (par
    embaixo de ímpar, seis colunas por bloco, gaiola de SFP à parte), e
    conhecimento de equipamento é deste service: com a regra no JS, o Excel
    e qualquer outra saída teriam de reimplementá-la para desenhar o mesmo
    painel — e divergiriam na primeira correção.
    """
    portas = list(
        dispositivo.portas
        .select_related('destino_item', 'destino_porta',
                        'destino_porta__dispositivo__item', 'rota')
        .order_by('numero')
    )
    acessos = [p for p in portas if p.tipo != RedePortaTipoChoices.SFP]
    uplinks = [p for p in portas if p.tipo == RedePortaTipoChoices.SFP]

    def serializar(p: RedePorta, indice: int) -> dict:
        destino_nome = ''
        destino_item_id = p.destino_item_id
        if p.destino_porta_id:
            destino_nome = p.destino_porta.dispositivo.item.nome
            destino_item_id = p.destino_porta.dispositivo.item_id
        elif p.destino_item_id:
            destino_nome = p.destino_item.nome
        coluna = indice // 2
        return {
            'id': p.pk,
            'numero': p.numero,
            'rotulo': p.rotulo or str(p.numero),
            'tipo': p.tipo,
            'tipo_label': p.get_tipo_display(),
            'status': p.status,
            'status_label': p.get_status_display(),
            'velocidade': p.velocidade,
            'velocidade_label': p.get_velocidade_display() if p.velocidade else '',
            'poe': p.poe,
            'vlan': p.vlan,
            'meio': p.meio,
            'meio_label': p.get_meio_display(),
            'descricao': p.descricao,
            'destino_item_id': destino_item_id,
            'destino_porta_id': p.destino_porta_id,
            'destino_nome': destino_nome,
            'destino_porta_rotulo': (p.destino_porta.rotulo or str(p.destino_porta.numero))
                                    if p.destino_porta_id else '',
            'rota_id': p.rota_id,
            'rota_nome': p.rota.nome if p.rota_id else '',
            'rota_m': float(p.rota.comprimento_m or 0) if p.rota_id else None,
            'fileira': indice % 2,
            'coluna': coluna,
            'bloco': coluna // _COLS_POR_BLOCO,
        }

    serie_acesso = [serializar(p, i) for i, p in enumerate(acessos)]
    serie_uplink = [serializar(p, i) for i, p in enumerate(uplinks)]

    # ── O painel montado: blocos → colunas → (topo, base) ────────────────
    blocos: list[dict] = []
    por_coluna: dict[int, dict] = {}
    for p in serie_acesso:
        slot = por_coluna.setdefault(p['coluna'], {'coluna': p['coluna'], 'topo': None, 'base': None})
        slot['topo' if p['fileira'] == 0 else 'base'] = p
    for coluna in sorted(por_coluna):
        indice_bloco = coluna // _COLS_POR_BLOCO
        if not blocos or blocos[-1]['indice'] != indice_bloco:
            blocos.append({'indice': indice_bloco, 'colunas': []})
        blocos[-1]['colunas'].append(por_coluna[coluna])

    # ── Resumo por situação: a régua segmentada do inspetor ──────────────
    resumo = {chave: 0 for chave, _ in RedePortaStatusChoices.choices}
    for p in portas:
        resumo[p.status] = resumo.get(p.status, 0) + 1

    # ── Vizinhos: para onde os cabos deste equipamento vão ───────────────
    # Sai das PORTAS, não de uma segunda montagem do grafo: a lista de
    # "quem está pendurado aqui" é exatamente o conjunto dos destinos das
    # portas, e remontar o grafo inteiro para descobrir isso seria pagar
    # quatro consultas para ler o que já está na mão.
    vistos: dict[int, dict] = {}
    for p in portas:
        if not (p.destino_porta_id or p.destino_item_id):
            continue
        if p.destino_porta_id:
            alvo = p.destino_porta.dispositivo.item
            porta_remota = p.destino_porta.rotulo or str(p.destino_porta.numero)
        else:
            alvo = p.destino_item
            porta_remota = ''
        if alvo is None:
            continue
        atual = vistos.setdefault(alvo.pk, {
            'item_id': alvo.pk,
            'nome': alvo.nome,
            'no': f'i{alvo.pk}',
            'enlaces': [],
        })
        atual['enlaces'].append({
            'porta_id': p.pk,
            'porta': p.rotulo or str(p.numero),
            'porta_remota': porta_remota,
            'meio': p.meio,
            'meio_label': p.get_meio_display(),
        })

    return {
        'dispositivo_id': dispositivo.pk,
        'item_id': dispositivo.item_id,
        'nome': dispositivo.item.nome,
        'papel': dispositivo.papel,
        'ip': dispositivo.ip_gerencia or '',
        'prtg_objid': dispositivo.prtg_objid,
        'modelo_portas': dispositivo.modelo_portas or '',
        'observacoes': dispositivo.observacoes or '',
        'rack_id': dispositivo.rack_id,
        'rack_nome': dispositivo.rack.nome if dispositivo.rack_id else '',
        'rack_u': dispositivo.rack_u,
        'rack_altura_u': dispositivo.rack_altura_u or 1,
        'latitude': float(dispositivo.latitude) if dispositivo.latitude is not None else None,
        'longitude': float(dispositivo.longitude) if dispositivo.longitude is not None else None,
        'acessos': serie_acesso,
        'uplinks': serie_uplink,
        'face': {'blocos': blocos, 'uplinks': serie_uplink,
                 'cols_por_bloco': _COLS_POR_BLOCO},
        'resumo': resumo,
        'vizinhos': sorted(vistos.values(), key=lambda v: v['nome']),
        'total': len(portas),
        'ativas': sum(1 for p in portas if p.status == RedePortaStatusChoices.ATIVA),
        'livres': sum(1 for p in portas if p.status == RedePortaStatusChoices.LIVRE),
        'poe_ativas': sum(1 for p in portas
                          if p.poe and p.status == RedePortaStatusChoices.ATIVA),
        'sem_rota': sum(1 for p in portas
                        if (p.destino_porta_id or p.destino_item_id) and not p.rota_id),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Impacto de uma queda (correlação de falhas — seção 7.12 do escopo)
# ──────────────────────────────────────────────────────────────────────────────

def _alcancaveis(nos: list[dict], enlaces: list[dict], sem_no: str = '',
                 sem_portas: set | None = None) -> tuple[set, dict]:
    """
    Quem a borda alcança, podendo tirar um nó ou um feixe de cabos do caminho.

    Devolve `(conjunto alcançado, adjacência)`. A adjacência volta junto
    porque quem chama precisa dela para contar as ligações diretas, e
    remontá-la seria percorrer os enlaces duas vezes.
    """
    adj = defaultdict(set)
    for e in enlaces:
        if sem_portas and (e.get('porta_de_id') in sem_portas
                           or e.get('porta_para_id') in sem_portas):
            continue
        adj[e['de']].add(e['para'])
        adj[e['para']].add(e['de'])

    bordas = [n['id'] for n in nos
              if n['papel'] in (RedePapelChoices.BORDA, RedePapelChoices.CORE)
              and n['id'] != sem_no]
    if not bordas:
        # Sem borda declarada, a raiz é quem tem mais enlaces — melhor que
        # devolver "nada depende de nada" num mapa ainda em montagem.
        ordenados = sorted(nos, key=lambda n: -len(adj[n['id']]))
        bordas = [n['id'] for n in ordenados[:1] if n['id'] != sem_no]

    alcancados = set()
    fila = list(bordas)
    while fila:
        atual = fila.pop()
        if atual in alcancados or atual == sem_no:
            continue
        alcancados.add(atual)
        fila.extend(adj[atual] - alcancados)
    return alcancados, adj


def impacto(no_id: str, nos: list[dict], enlaces: list[dict]) -> dict:
    """
    O que deixa de funcionar se este nó cair.

    Percorre o grafo a partir dos nós de BORDA sem passar pelo nó em questão:
    o que não é alcançável depende dele. Responde a pergunta que o escopo pede
    ("a causa provável está no switch, não nas 12 câmeras") sem precisar de
    histórico nem de regra por equipamento.

    **A conta é a DIFERENÇA entre antes e depois, não "quem a borda não
    alcança".** Hoje há 89 switches sem nenhum enlace cadastrado: eles não
    são alcançáveis pela borda em hipótese nenhuma, e a primeira versão desta
    função os listava como vítimas de qualquer queda — o relatório culpava um
    switch de acesso por 89 equipamentos que já estavam fora do grafo antes
    dele cair. Quem já estava isolado não fica isolado por causa de ninguém.
    """
    antes, adj = _alcancaveis(nos, enlaces)
    depois, _ = _alcancaveis(nos, enlaces, sem_no=no_id)

    isolados = [n for n in nos
                if n['id'] != no_id and n['id'] in antes and n['id'] not in depois]
    return {
        'no': no_id,
        'isolados': [{'id': n['id'], 'nome': n['nome'], 'classe': n['classe']} for n in isolados],
        'total': len(isolados),
        'diretos': len(adj[no_id]),
        # Quantos já estavam fora do alcance da borda antes desta queda. Não
        # entra em `isolados`, mas dizer o número evita a pergunta "e os
        # outros?" na frente do gestor.
        'ja_isolados': sum(1 for n in nos if n['id'] not in antes and n['id'] != no_id),
    }


# ──────────────────────────────────────────────────────────────────────────────
# A PLANTA GEORREFERENCIADA — rack, rota e medida em metros
# ──────────────────────────────────────────────────────────────────────────────
# O grafo acima responde COMO está ligado. Esta parte responde POR ONDE o cabo
# passa — e, principalmente, QUANTOS METROS ele tem. O comprimento é o número
# que decide o projeto: 90 m é o limite do cabeamento horizontal em par
# trançado (TIA-568 / ISO 11801), e um lance de 140 m desenhado como UTP é um
# lance que não vai funcionar depois de instalado.
#
# Medida em WGS84 pela fórmula de haversine. Não é a mais exata que existe
# (Vincenty erra menos em distância continental), mas num raio de fazenda o
# erro do haversine é de centímetros, e ele não depende de biblioteca: o
# projeto não tem GeoDjango nem GDAL instalados, e exigir isso para medir um
# lance de fibra seria trocar um requisito de sistema por três casas decimais
# que ninguém usa.

_RAIO_TERRA_M = 6371008.8          # raio médio WGS84

# Limite do cabeamento horizontal em par trançado. Os 90 m são o lance
# permanente (parede a parede); os 10 m restantes do total de 100 m são os
# patch cords das duas pontas. Por isso o alerta começa antes do limite: um
# lance de 88 m já não deixa folga para os cordões.
LIMITE_UTP_M = 90
FOLGA_UTP_M = 80

# Centro do mapa quando NADA tem posição ainda. É a coordenada real da
# fazenda — a mesma que a regra 15 do CLAUDE.md manda usar para testar
# provedor de tile, porque no meio do nada todo provedor devolve tile vazio e
# a tela parece quebrada estando certa. `settings.REDE_PLANTA_CENTRO` troca.
CENTRO_PADRAO = (-14.7763, -45.5634)


def centro_padrao() -> tuple[float, float]:
    """O centro inicial do mapa, com override por settings."""
    from django.conf import settings
    bruto = getattr(settings, 'REDE_PLANTA_CENTRO', None)
    try:
        lat, lon = float(bruto[0]), float(bruto[1])
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return (lat, lon)
    except (TypeError, ValueError, IndexError):
        pass
    return CENTRO_PADRAO


def _haversine(lat1, lon1, lat2, lon2) -> float:
    """Distância em metros entre dois pontos geográficos."""
    f1, f2 = radians(float(lat1)), radians(float(lat2))
    dlat = f2 - f1
    dlon = radians(float(lon2) - float(lon1))
    a = sin(dlat / 2) ** 2 + cos(f1) * cos(f2) * sin(dlon / 2) ** 2
    return 2 * _RAIO_TERRA_M * asin(min(1.0, sqrt(a)))


def limpar_pontos(bruto) -> list[list[float]]:
    """
    O traçado em forma confiável: lista de `[lat, lon]` dentro do globo.

    Saneamento de DOMÍNIO, não de HTTP: latitude 95 não é um ataque, é um
    ponto impossível — e deixá-lo passar faria o comprimento da rota sair
    errado em silêncio, que é pior que recusar. A view ainda limita tamanho
    de corpo; aqui se decide o que é coordenada.
    """
    saida: list[list[float]] = []
    if not isinstance(bruto, (list, tuple)):
        return saida
    for par in bruto[:600]:
        if not isinstance(par, (list, tuple)) or len(par) < 2:
            continue
        try:
            lat, lon = float(par[0]), float(par[1])
        except (TypeError, ValueError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        # Ponto repetido não acrescenta traçado e ainda soma zero metro a
        # cada clique duplo de quem está desenhando.
        if saida and abs(saida[-1][0] - lat) < 1e-9 and abs(saida[-1][1] - lon) < 1e-9:
            continue
        saida.append([round(lat, 7), round(lon, 7)])
    return saida


def comprimento_rota(pontos) -> float:
    """Comprimento do traçado em metros, somando segmento por segmento."""
    limpos = limpar_pontos(pontos)
    total = 0.0
    for i in range(1, len(limpos)):
        total += _haversine(limpos[i - 1][0], limpos[i - 1][1], limpos[i][0], limpos[i][1])
    return round(total, 2)


@transaction.atomic
def salvar_rota(rota: RedeRota, dados: dict, usuario=None) -> RedeRota:
    """
    Única porta de entrada da rota — e é ela que calcula o comprimento.

    `comprimento_m` é coluna gravada (entra em soma, ordenação e na
    conferência dos 90 m), mas NUNCA vem do cliente: quem manda o traçado
    não pode mandar também a medida dele, senão as duas podem discordar e o
    relatório passa a afirmar um metro que o desenho não tem.
    """
    if 'nome' in dados:
        rota.nome = str(dados.get('nome') or '').strip()[:160] or rota.nome or 'Rota sem nome'
    if 'tipo' in dados:
        tipo = str(dados.get('tipo') or '')
        if tipo in dict(RedeRotaTipoChoices.choices):
            rota.tipo = tipo
    if 'instalacao' in dados:
        inst = str(dados.get('instalacao') or '')
        if inst in dict(RedeRotaInstalacaoChoices.choices):
            rota.instalacao = inst
    if 'area' in dados:
        area = str(dados.get('area') or '')
        rota.area = area if area in dict(AreaOperacionalChoices.choices) else ''
    if 'localidade' in dados:
        rota.localidade_id = dados.get('localidade') or None
    if 'fibras' in dados:
        try:
            rota.fibras = max(0, min(576, int(dados.get('fibras') or 0)))
        except (TypeError, ValueError):
            rota.fibras = 0
    if 'observacoes' in dados:
        rota.observacoes = str(dados.get('observacoes') or '')[:2000]
    for campo in ('rack_a', 'rack_b'):
        if campo in dados:
            setattr(rota, f'{campo}_id', dados.get(campo) or None)

    if 'pontos' in dados:
        rota.pontos = limpar_pontos(dados.get('pontos'))

    rota.comprimento_m = comprimento_rota(rota.pontos)
    rota.atualizado_por = usuario
    if rota.pk is None:
        rota.criado_por = usuario
    rota.save()
    return rota


def cor_de_rota(tipo: str) -> str:
    """
    Classe de cor da rota — slug, não cor.

    O service não escolhe cor: ele diz a que família o lance pertence e o
    `ds-rede.css` resolve pelo token (regra 33). Devolver `#6b4ad6` daqui
    seria paleta escrita em Python, que nenhum tema consegue trocar depois.
    """
    if tipo in (RedeRotaTipoChoices.FIBRA_MM, RedeRotaTipoChoices.FIBRA_SM):
        return 'fibra'
    if tipo in (RedeRotaTipoChoices.UTP_CAT5E, RedeRotaTipoChoices.UTP_CAT6,
                RedeRotaTipoChoices.UTP_CAT6A):
        return 'utp'
    if tipo == RedeRotaTipoChoices.RADIO:
        return 'radio'
    if tipo == RedeRotaTipoChoices.ELETRICO:
        return 'eletrico'
    if tipo == RedeRotaTipoChoices.COAXIAL:
        return 'coaxial'
    return 'duto'


def _serializar_rota(r: RedeRota, enlaces_por_rota: dict) -> dict:
    return {
        'id': r.pk,
        'nome': r.nome,
        'tipo': r.tipo,
        'tipo_label': r.get_tipo_display(),
        'instalacao': r.instalacao,
        'instalacao_label': r.get_instalacao_display(),
        'familia': cor_de_rota(r.tipo),
        'cobre': r.e_cobre,
        'pontos': r.pontos or [],
        'comprimento_m': float(r.comprimento_m or 0),
        'fibras': r.fibras,
        'enlaces': enlaces_por_rota.get(r.pk, 0),
        'localidade': r.localidade.local if r.localidade else '',
        'localidade_id': r.localidade_id,
        'area': r.area or '',
        'rack_a': r.rack_a_id,
        'rack_b': r.rack_b_id,
        'rack_a_nome': r.rack_a.nome if r.rack_a_id else '',
        'rack_b_nome': r.rack_b.nome if r.rack_b_id else '',
        'observacoes': r.observacoes or '',
    }


def montar_planta(localidade_id=None, area=None, com_status=True) -> dict:
    """
    A planta georreferenciada do escopo: racks, equipamentos posicionados e
    rotas de cabo, com os números e a conferência que o projeto pede.

    Posição do equipamento: a dele quando existe, senão a do RACK dele. Herdar
    do rack é o que torna o cadastro viável — ninguém vai apontar no mapa a
    coordenada de 24 switches que estão todos no mesmo armário, mas apontar a
    do armário uma vez é trabalho de um minuto.
    """
    racks = list(
        RedeRack.objects
        .select_related('localidade')
        .prefetch_related('equipamentos__item')
    )
    if localidade_id:
        racks = [r for r in racks if str(r.localidade_id) == str(localidade_id)]
    if area:
        racks = [r for r in racks if r.area == area]
    racks_por_id = {r.pk: r for r in racks}

    disp_qs = (
        RedeDispositivo.objects
        .select_related('item', 'item__localidade', 'item__subtipo', 'rack')
        .filter(item__excluido=False)
    )
    if localidade_id:
        disp_qs = disp_qs.filter(item__localidade_id=localidade_id)
    if area:
        disp_qs = disp_qs.filter(item__area_operacional=area)

    rotas_qs = RedeRota.objects.select_related('localidade', 'rack_a', 'rack_b')
    if localidade_id:
        rotas_qs = rotas_qs.filter(localidade_id=localidade_id)
    if area:
        rotas_qs = rotas_qs.filter(area=area)
    rotas = list(rotas_qs)

    # Quantos enlaces declaram viajar por cada rota — é a ocupação do duto.
    enlaces_por_rota: dict[int, int] = defaultdict(int)
    for rota_id in (
        RedePorta.objects.filter(rota__isnull=False)
        .values_list('rota_id', flat=True)
    ):
        enlaces_por_rota[rota_id] += 1

    mapa_prtg = {}
    if com_status and prtg_service.is_configured():
        try:
            mapa_prtg = prtg_service.get_devices_map()
        except Exception:
            mapa_prtg = {}

    equipamentos = []
    contagem_rack: dict[int, int] = defaultdict(int)
    for d in disp_qs:
        lat, lon = d.latitude, d.longitude
        herdado = False
        if (lat is None or lon is None) and d.rack_id and d.rack_id in racks_por_id:
            rack = racks_por_id[d.rack_id]
            if rack.tem_geo:
                lat, lon, herdado = rack.latitude, rack.longitude, True
        if d.rack_id:
            contagem_rack[d.rack_id] += 1
        classe, _ = classe_de(d.item.subtipo.nome if d.item.subtipo else '')
        equipamentos.append({
            'item_id': d.item_id,
            'dispositivo_id': d.pk,
            'nome': d.item.nome,
            'classe': classe,
            'papel': d.papel,
            'papel_label': d.get_papel_display(),
            'ip': d.ip_gerencia or '',
            'localidade': d.item.localidade.local if d.item.localidade else '',
            'lat': float(lat) if lat is not None else None,
            'lon': float(lon) if lon is not None else None,
            'geo_herdada': herdado,
            'rack_id': d.rack_id,
            'rack_nome': d.rack.nome if d.rack_id else '',
            'rack_u': d.rack_u,
            'rack_altura_u': d.rack_altura_u or 1,
            'status': _status_prtg(d.prtg_objid, mapa_prtg),
        })

    lista_racks = [{
        'id': r.pk,
        'nome': r.nome,
        'codigo': r.codigo or '',
        'localidade': r.localidade.local if r.localidade else '',
        'localidade_id': r.localidade_id,
        'area': r.area or '',
        'ambiente': r.ambiente or '',
        'us': r.us,
        'us_ocupados': r.us_ocupados,
        'equipamentos': contagem_rack.get(r.pk, 0),
        'lat': float(r.latitude) if r.latitude is not None else None,
        'lon': float(r.longitude) if r.longitude is not None else None,
        'observacoes': r.observacoes or '',
    } for r in racks]

    lista_rotas = [_serializar_rota(r, enlaces_por_rota) for r in rotas]

    # Centro: média do que tem posição. Sem nada posicionado, a coordenada da
    # empresa — abrir o mapa no meio do Atlântico faria a tela parecer quebrada.
    pontos = [(e['lat'], e['lon']) for e in equipamentos if e['lat'] is not None]
    pontos += [(r['lat'], r['lon']) for r in lista_racks if r['lat'] is not None]
    for r in lista_rotas:
        pontos += [(p[0], p[1]) for p in r['pontos']]
    if pontos:
        centro = [sum(p[0] for p in pontos) / len(pontos),
                  sum(p[1] for p in pontos) / len(pontos)]
    else:
        centro = list(centro_padrao())

    return {
        'racks': lista_racks,
        'equipamentos': equipamentos,
        'rotas': lista_rotas,
        'centro': centro,
        'tem_posicao': bool(pontos),
        'kpis': kpis_planta(lista_racks, equipamentos, lista_rotas),
        'avisos': validar_planta(lista_racks, equipamentos, lista_rotas),
        'prtg_ativo': bool(mapa_prtg),
    }


def kpis_planta(racks: list[dict], equipamentos: list[dict], rotas: list[dict]) -> dict:
    """Os números que encabeçam a planta e a aba de rotas do Excel."""
    metros_por_familia: dict[str, float] = defaultdict(float)
    for r in rotas:
        metros_por_familia[r['familia']] += r['comprimento_m']
    posicionados = sum(1 for e in equipamentos if e['lat'] is not None)
    return {
        'racks': len(racks),
        'racks_sem_posicao': sum(1 for r in racks if r['lat'] is None),
        'us_totais': sum(r['us'] for r in racks),
        'us_ocupados': sum(r['us_ocupados'] for r in racks),
        'equipamentos': len(equipamentos),
        'posicionados': posicionados,
        'sem_posicao': len(equipamentos) - posicionados,
        'em_rack': sum(1 for e in equipamentos if e['rack_id']),
        'rotas': len(rotas),
        'metros': round(sum(r['comprimento_m'] for r in rotas), 2),
        'metros_fibra': round(metros_por_familia.get('fibra', 0), 2),
        'metros_utp': round(metros_por_familia.get('utp', 0), 2),
        'rotas_sem_enlace': sum(1 for r in rotas if not r['enlaces']),
        'maior_lance': round(max((r['comprimento_m'] for r in rotas), default=0), 2),
    }


def validar_planta(racks: list[dict], equipamentos: list[dict], rotas: list[dict]) -> list[dict]:
    """
    Conferência da planta — o que impede a instalação de funcionar.

    Diferente da conferência do grafo, que olha cadastro: aqui se olha
    FÍSICA. O lance de 112 m em Cat6 passa em qualquer validação de
    cadastro e falha no campo; é esse o erro que esta função existe para
    pegar antes de alguém abrir a vala.
    """
    avisos: list[dict] = []

    # 1. Lance de cobre acima do limite da norma.
    estourados = [r for r in rotas if r['cobre'] and r['comprimento_m'] > LIMITE_UTP_M]
    for r in sorted(estourados, key=lambda x: -x['comprimento_m']):
        avisos.append({
            'nivel': 'erro',
            'codigo': 'utp_acima_do_limite',
            'titulo': f"{r['nome']}: {r['comprimento_m']:.0f} m em {r['tipo_label']}",
            'detalhe': f"O cabeamento horizontal em par trançado permite {LIMITE_UTP_M} m "
                       f"de lance permanente. Este lance precisa de fibra, ou de um "
                       f"equipamento intermediário no meio do caminho.",
            'rotas': [r['id']],
        })

    # 2. Lance de cobre no limite: passa, mas sem folga para os patch cords.
    apertados = [r for r in rotas
                 if r['cobre'] and FOLGA_UTP_M < r['comprimento_m'] <= LIMITE_UTP_M]
    if apertados:
        avisos.append({
            'nivel': 'alerta',
            'codigo': 'utp_sem_folga',
            'titulo': f"{len(apertados)} lance(s) de cobre entre {FOLGA_UTP_M} e {LIMITE_UTP_M} m",
            'detalhe': 'Cabe na norma, mas os 10 m restantes do total de 100 m são os '
                       'cordões das duas pontas. '
                       + ', '.join(f"{r['nome']} ({r['comprimento_m']:.0f} m)" for r in apertados[:4]),
            'rotas': [r['id'] for r in apertados],
        })

    # 3. Traçado que não fecha: um ponto só não é caminho.
    incompletas = [r for r in rotas if len(r['pontos']) < 2]
    if incompletas:
        avisos.append({
            'nivel': 'alerta',
            'codigo': 'rota_sem_tracado',
            'titulo': f"{len(incompletas)} rota(s) sem traçado no mapa",
            'detalhe': 'Com menos de dois pontos não há comprimento para medir. '
                       + ', '.join(r['nome'] for r in incompletas[:5]),
            'rotas': [r['id'] for r in incompletas],
        })

    # 4. Mais cabos declarados do que fibras no cabo.
    for r in rotas:
        if r['fibras'] and r['enlaces'] > r['fibras']:
            avisos.append({
                'nivel': 'erro',
                'codigo': 'rota_sobrecarregada',
                'titulo': f"{r['nome']}: {r['enlaces']} enlaces em um cabo de {r['fibras']}",
                'detalhe': 'Há mais lances apontando para esta rota do que ela tem '
                           'capacidade. Um dos enlaces está na rota errada.',
                'rotas': [r['id']],
            })

    # 5. Rack com mais equipamento do que altura útil.
    for k in racks:
        if k['us_ocupados'] > k['us']:
            avisos.append({
                'nivel': 'erro',
                'codigo': 'rack_sem_espaco',
                'titulo': f"{k['nome']}: {k['us_ocupados']}U ocupados em um rack de {k['us']}U",
                'detalhe': 'A soma da altura dos equipamentos posicionados passa da altura '
                           'do rack. Confira a posição (U) de quem está nele.',
                'racks': [k['id']],
            })

    # 6. Colisão de posição no rack — dois equipamentos no mesmo U.
    ocupacao: dict[int, dict[int, str]] = defaultdict(dict)
    for e in equipamentos:
        if not (e['rack_id'] and e['rack_u']):
            continue
        for u in range(e['rack_u'], e['rack_u'] + max(1, e['rack_altura_u'])):
            if u in ocupacao[e['rack_id']]:
                avisos.append({
                    'nivel': 'erro',
                    'codigo': 'rack_colisao',
                    'titulo': f"U{u} do rack ocupado por dois equipamentos",
                    'detalhe': f"{ocupacao[e['rack_id']][u]} e {e['nome']} estão na mesma "
                               f"altura. Um dos dois está com a posição errada.",
                    'racks': [e['rack_id']],
                })
            ocupacao[e['rack_id']][u] = e['nome']

    # 7. Rack sem posição no mapa — a rota de fibra não tem onde chegar.
    sem_geo = [k for k in racks if k['lat'] is None]
    if sem_geo:
        avisos.append({
            'nivel': 'info',
            'codigo': 'rack_sem_posicao',
            'titulo': f"{len(sem_geo)} rack(s) sem posição no mapa",
            'detalhe': 'Sem coordenada o rack não aparece na planta, e o equipamento '
                       'dentro dele também não herda posição. '
                       + ', '.join(k['nome'] for k in sem_geo[:5]),
            'racks': [k['id'] for k in sem_geo],
        })

    # 8. Equipamento de rede sem posição nem rack.
    orfaos = [e for e in equipamentos
              if e['lat'] is None and not e['rack_id']
              and e['papel'] != RedePapelChoices.TERMINAL]
    if orfaos:
        avisos.append({
            'nivel': 'info',
            'codigo': 'equipamento_sem_posicao',
            'titulo': f"{len(orfaos)} equipamento(s) de rede fora da planta",
            'detalhe': 'Nem coordenada própria, nem rack de onde herdar. '
                       + ', '.join(e['nome'] for e in orfaos[:5]),
            'itens': [e['item_id'] for e in orfaos],
        })

    avisos.sort(key=lambda a: _GRAVIDADE.get(a['nivel'], 3))
    return avisos


def elevacao_rack(rack: RedeRack) -> dict:
    """
    O rack visto de frente: a régua de U, de cima para baixo.

    É a vista que o técnico usa para achar o equipamento no armário, e a que
    responde "cabe o switch novo?" sem contar parafuso. A régua vem de cima
    para baixo porque é assim que se olha o rack aberto; o NÚMERO do U, no
    entanto, conta de baixo para cima, que é como a etiqueta do fabricante
    está impressa. Inverter um dos dois faria a tela discordar do ferro.
    """
    equipamentos = list(
        rack.equipamentos.select_related('item', 'item__subtipo').all()
    )
    mapa_u: dict[int, dict] = {}
    for d in equipamentos:
        if not d.rack_u:
            continue
        classe, _ = classe_de(d.item.subtipo.nome if d.item.subtipo else '')
        altura = max(1, d.rack_altura_u or 1)
        for passo in range(altura):
            u = d.rack_u + passo
            if u > rack.us:
                continue
            mapa_u[u] = {
                'item_id': d.item_id,
                'dispositivo_id': d.pk,
                'nome': d.item.nome,
                'classe': classe,
                'papel': d.papel,
                'ip': d.ip_gerencia or '',
                'altura_u': altura,
                'base_u': d.rack_u,
                'primeiro': passo == 0,
            }

    regua = []
    for u in range(rack.us, 0, -1):
        regua.append({'u': u, 'ocupante': mapa_u.get(u)})

    sem_posicao = [{
        'item_id': d.item_id,
        'dispositivo_id': d.pk,
        'nome': d.item.nome,
        'altura_u': max(1, d.rack_altura_u or 1),
    } for d in equipamentos if not d.rack_u]

    return {
        'rack': {
            'id': rack.pk,
            'nome': rack.nome,
            'codigo': rack.codigo or '',
            'ambiente': rack.ambiente or '',
            'localidade': rack.localidade.local if rack.localidade else '',
            'us': rack.us,
            'us_ocupados': rack.us_ocupados,
            'us_livres': max(0, rack.us - rack.us_ocupados),
            'lat': float(rack.latitude) if rack.latitude is not None else None,
            'lon': float(rack.longitude) if rack.longitude is not None else None,
            'observacoes': rack.observacoes or '',
        },
        'regua': regua,
        'sem_posicao': sem_posicao,
    }


def impacto_rota(rota: RedeRota, nos: list[dict], enlaces: list[dict]) -> dict:
    """
    O que fica isolado se esta vala for rompida.

    Mesmo percurso do `impacto` de um nó, com a diferença que importa: aqui
    não se remove um equipamento, removem-se os ENLACES que declaram viajar
    por esta rota. É a pergunta que o gestor faz quando a retroescavadeira
    passa no lugar errado, e ela não tem resposta sem a rota como objeto.
    """
    portas_na_rota = set(
        RedePorta.objects.filter(rota=rota).values_list('pk', flat=True)
    )
    cortados = sum(
        1 for e in enlaces
        if e.get('porta_de_id') in portas_na_rota
        or e.get('porta_para_id') in portas_na_rota
    )

    # Mesma disciplina do `impacto` de um nó: a conta é a DIFERENÇA entre
    # antes e depois. Sem a linha de base, uma rota que não corta cabo
    # nenhum ainda listaria como vítimas os equipamentos que nunca tiveram
    # enlace cadastrado — atribuindo à vala um estrago que não é dela.
    antes, _ = _alcancaveis(nos, enlaces)
    depois, _ = _alcancaveis(nos, enlaces, sem_portas=portas_na_rota)

    isolados = [n for n in nos if n['id'] in antes and n['id'] not in depois]
    return {
        'rota': rota.pk,
        'nome': rota.nome,
        'comprimento_m': float(rota.comprimento_m or 0),
        'enlaces_cortados': cortados,
        'isolados': [{'id': n['id'], 'nome': n['nome'], 'classe': n['classe']} for n in isolados],
        'total': len(isolados),
        'ja_isolados': sum(1 for n in nos if n['id'] not in antes),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Gravação de posição — rack, coordenada e vínculo de rota
# ──────────────────────────────────────────────────────────────────────────────
# Todas as escritas de posição passam por aqui pela mesma razão que `conectar`
# existe: a coordenada e a altura em rack são lidas por três telas (planta,
# bancada e elevação) e pelo Excel. Com a conversão espalhada pelas views,
# basta uma delas aceitar latitude 95 ou altura 0 para a conferência passar a
# acusar erro em cadastro que a própria tela gravou.


def coordenada(valor):
    """Latitude/longitude em `Decimal` com 7 casas, ou `None`."""
    from decimal import Decimal, InvalidOperation
    if valor in (None, '', 'null'):
        return None
    try:
        n = Decimal(str(valor)).quantize(Decimal('0.0000001'))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not (Decimal('-180') <= n <= Decimal('180')):
        return None
    return n


def _entre(valor, minimo, maximo) -> bool:
    """`minimo <= valor <= maximo` sem misturar Decimal com float."""
    from decimal import Decimal
    return Decimal(str(minimo)) <= valor <= Decimal(str(maximo))


@transaction.atomic
def salvar_posicao(dispositivo: RedeDispositivo, dados: dict, usuario=None) -> RedeDispositivo:
    """
    Onde o equipamento está: coordenada, rack e altura no rack.

    Altura mínima de 1U, sempre. Um equipamento de 0U não existe e zeraria a
    soma de ocupação do rack — o armário passaria a parecer vazio com vinte
    switches dentro, e o aviso de "rack sem espaço" nunca dispararia.
    """
    if 'latitude' in dados or 'longitude' in dados:
        lat = coordenada(dados.get('latitude'))
        lon = coordenada(dados.get('longitude'))
        # Meia coordenada não é posição: um ponto com latitude e sem longitude
        # cairia no meridiano de Greenwich, a 4.000 km da fazenda, e apareceria
        # no mapa como se fosse dado bom.
        if lat is None or lon is None:
            dispositivo.latitude = None
            dispositivo.longitude = None
        elif _entre(lat, -90, 90):
            dispositivo.latitude = lat
            dispositivo.longitude = lon

    if 'rack' in dados:
        rack_id = dados.get('rack') or None
        dispositivo.rack_id = rack_id if (
            rack_id and RedeRack.objects.filter(pk=rack_id).exists()
        ) else None
        if dispositivo.rack_id is None:
            dispositivo.rack_u = None

    if 'rack_u' in dados:
        try:
            u = int(dados.get('rack_u') or 0)
        except (TypeError, ValueError):
            u = 0
        dispositivo.rack_u = u if u > 0 else None

    if 'rack_altura_u' in dados:
        try:
            alt = int(dados.get('rack_altura_u') or 1)
        except (TypeError, ValueError):
            alt = 1
        dispositivo.rack_altura_u = max(1, min(48, alt))

    dispositivo.atualizado_por = usuario
    dispositivo.save(update_fields=[
        'latitude', 'longitude', 'rack', 'rack_u', 'rack_altura_u',
        'atualizado_por', 'updated_at',
    ])
    return dispositivo


@transaction.atomic
def salvar_rack(rack: RedeRack, dados: dict, usuario=None) -> RedeRack:
    """Cria ou atualiza um rack. Única porta de entrada."""
    if 'nome' in dados:
        rack.nome = str(dados.get('nome') or '').strip()[:120] or rack.nome or 'Rack sem nome'
    if 'codigo' in dados:
        rack.codigo = str(dados.get('codigo') or '').strip()[:40]
    if 'ambiente' in dados:
        rack.ambiente = str(dados.get('ambiente') or '').strip()[:120]
    if 'localidade' in dados:
        rack.localidade_id = dados.get('localidade') or None
    if 'area' in dados:
        area = str(dados.get('area') or '')
        rack.area = area if area in dict(AreaOperacionalChoices.choices) else ''
    if 'us' in dados:
        try:
            rack.us = max(1, min(60, int(dados.get('us') or 42)))
        except (TypeError, ValueError):
            rack.us = 42
    if 'latitude' in dados or 'longitude' in dados:
        lat = coordenada(dados.get('latitude'))
        lon = coordenada(dados.get('longitude'))
        if lat is None or lon is None or not _entre(lat, -90, 90):
            rack.latitude = None
            rack.longitude = None
        else:
            rack.latitude = lat
            rack.longitude = lon
    if 'observacoes' in dados:
        rack.observacoes = str(dados.get('observacoes') or '')[:2000]

    rack.atualizado_por = usuario
    if rack.pk is None:
        rack.criado_por = usuario
    rack.save()
    return rack


@transaction.atomic
def vincular_rota(porta: RedePorta, rota: RedeRota | None, usuario=None) -> RedePorta:
    """
    Declara por qual rota física este cabo passa — nas DUAS pontas.

    As duas pontas porque o cabo é um só: com o vínculo em uma perna, a
    ocupação do duto sairia pela metade e o "quem cai se romper" perderia
    metade dos enlaces. É o mesmo motivo de `conectar` gravar os dois lados.
    """
    porta.rota = rota
    porta.atualizado_por = usuario
    porta.save(update_fields=['rota', 'atualizado_por', 'updated_at'])
    par = porta.destino_porta
    if par is not None and par.rota_id != (rota.pk if rota else None):
        par.rota = rota
        par.atualizado_por = usuario
        par.save(update_fields=['rota', 'atualizado_por', 'updated_at'])
    return porta
