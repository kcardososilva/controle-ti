"""
prtg_monitor_service.py — Coleta e histórico de status PRTG por device.

Objetivo: manter um HISTÓRICO de atividade (online/offline/instável) de TODOS os
equipamentos monitorados no PRTG — não apenas os que estão vinculados a um Item do
estoque numa planta. Por isso o histórico é identificado pelo `prtg_objid` (id do
device no PRTG); o vínculo com um Item é opcional.

Cada MUDANÇA de status vira um registro em ItemPRTGHistorico. O carimbo de tempo do
registro usa, sempre que possível, o MOMENTO REAL da transição reportado pelo PRTG
(uptimesince/downtimesince do sensor de ping) — assim, mesmo na primeira coleta, um
device que está fora do ar há 2 dias é registrado como "offline há 2 dias", e não
como "online" desde agora. Isso corrige o bug em que o tempo era contado a partir do
instante em que o coletor rodava.

A coleta roda de duas formas:
  • Continuamente, pelo management command `monitorar_prtg`, agendado no Agendador
    de Tarefas do Windows (schtasks) — captura as quedas mesmo sem ninguém abrir tela.
  • Sob demanda, pela view `item_monitoracao` (fallback ao abrir a tela do item).

O vínculo Item ↔ device PRTG vem dos elementos das plantas
(PlantaProjeto.layout → elements[{item_id, prtg_objid}]).
"""
import logging
from collections import defaultdict
from datetime import timedelta

logger = logging.getLogger(__name__)

# Slugs de status PRTG considerados "indisponível" (equipamento offline)
STATUS_DOWN = frozenset({"down"})
# Slugs considerados "instável" (atenção, mas ainda responde)
STATUS_WARNING = frozenset({"warning", "unusual"})


# ──────────────────────────────────────────────────────────────────────────────
# Mapas Item ↔ device PRTG (a partir das plantas)
# ──────────────────────────────────────────────────────────────────────────────

def mapa_itens_prtg() -> dict[int, int]:
    """{item_id: prtg_objid} a partir dos elementos de todas as plantas."""
    from ProjetoEstoque.models import PlantaProjeto

    mapa: dict[int, int] = {}
    for planta in PlantaProjeto.objects.all().only("layout"):
        layout = planta.layout or {}
        for el in layout.get("elements", []):
            item_id = el.get("item_id")
            objid = el.get("prtg_objid")
            if not item_id or not objid:
                continue
            try:
                mapa[int(item_id)] = int(objid)
            except (ValueError, TypeError):
                continue
    return mapa


def mapa_objid_item() -> dict[int, int]:
    """{prtg_objid: item_id} — inverso, para vincular um device a um Item.

    Combina o vínculo atual das plantas com os vínculos já registrados no histórico
    (cobre itens monitorados que saíram da planta). A planta tem prioridade.
    """
    from ProjetoEstoque.models import ItemPRTGHistorico

    rev: dict[int, int] = {}
    # Histórico primeiro (menor prioridade); planta sobrescreve depois.
    for row in (
        ItemPRTGHistorico.objects
        .filter(item_id__isnull=False)
        .order_by("-registrado_em")
        .values("prtg_objid", "item_id")
    ):
        rev.setdefault(row["prtg_objid"], row["item_id"])
    for item_id, objid in mapa_itens_prtg().items():
        rev[objid] = item_id
    return rev


def prtg_objid_do_item(item_id) -> int | None:
    """objid PRTG de um item: vínculo atual na planta ou, em fallback, o último
    objid conhecido no histórico (cobre itens monitorados que saíram da planta)."""
    try:
        iid = int(item_id)
    except (ValueError, TypeError):
        return None

    objid = mapa_itens_prtg().get(iid)
    if objid:
        return objid

    from ProjetoEstoque.models import ItemPRTGHistorico
    h = ItemPRTGHistorico.objects.filter(item_id=iid).order_by("-registrado_em").first()
    return h.prtg_objid if h else None


# ──────────────────────────────────────────────────────────────────────────────
# Gravação de eventos
# ──────────────────────────────────────────────────────────────────────────────

def registrar_evento(prtg_objid, status_novo, *, quando=None,
                     device_nome="", device_host="", device_grupo="",
                     item_id=None) -> bool:
    """
    Grava um ItemPRTGHistorico para o device SOMENTE quando o status muda em
    relação ao último registro do mesmo objid (evita inflar o histórico com
    leituras idênticas). Retorna True se um novo evento foi gravado.

    `quando` (datetime) define o carimbo do evento — passe o MOMENTO REAL da
    transição reportado pelo PRTG. É limitado para nunca ser anterior ao último
    evento nem futuro; na ausência de valor válido, usa agora.
    """
    from django.utils import timezone
    from ProjetoEstoque.models import ItemPRTGHistorico

    ultimo = (
        ItemPRTGHistorico.objects.filter(prtg_objid=prtg_objid)
        .order_by("-registrado_em").first()
    )
    if ultimo is not None and ultimo.status_novo == status_novo:
        # Status inalterado. Mas se este é o ÚNICO registro (seed inicial, gravado
        # no horário do poll) e o PRTG informa que o estado atual começou ANTES,
        # corrige a DATA do seed para o momento real — uma vez só (idempotente).
        # Não fabrica transições: só ajusta a primeira observação contínua.
        if (quando is not None and ultimo.status_anterior == ""
                and quando < ultimo.registrado_em):
            ultimo.registrado_em = quando
            if item_id and not ultimo.item_id:
                ultimo.item_id = item_id
            if device_nome:
                ultimo.device_nome = device_nome
            if device_host:
                ultimo.device_host = device_host
            if device_grupo:
                ultimo.device_grupo = device_grupo
            ultimo.save(update_fields=[
                "registrado_em", "item_id", "device_nome", "device_host", "device_grupo",
            ])
        return False

    agora = timezone.now()
    ts = quando or agora
    if ts > agora:
        ts = agora
    # Não pode ser anterior (ou igual) ao último evento, senão quebra a ordem.
    if ultimo is not None and ts <= ultimo.registrado_em:
        ts = agora

    ItemPRTGHistorico.objects.create(
        prtg_objid=prtg_objid,
        item_id=item_id,
        device_nome=device_nome or "",
        device_host=device_host or "",
        device_grupo=device_grupo or "",
        status_anterior=ultimo.status_novo if ultimo else "",
        status_novo=status_novo,
        registrado_em=ts,
    )
    return True


def _quando_transicao(dev, agora):
    """Datetime real em que o device entrou no status atual, a partir do
    uptimesince/downtimesince do PRTG. None quando o PRTG não informa."""
    since = dev.get("since_seconds")
    if since is None:
        return None
    try:
        since = float(since)
    except (ValueError, TypeError):
        return None
    if since <= 0:
        return None
    return agora - timedelta(seconds=since)


def coletar_status(devices_map=None) -> dict:
    """
    Consulta o PRTG (uma vez) e registra as mudanças de status de TODOS os devices
    monitorados. Pensado para rodar periodicamente (Agendador do Windows).
    Retorna estatísticas da coleta.
    """
    from django.utils import timezone
    from services.prtg_service import get_devices_map, is_configured

    stats = {
        "ok": True, "erro": None,
        "devices": 0, "eventos": 0, "vinculados": 0,
    }

    if not is_configured():
        stats["ok"] = False
        stats["erro"] = "PRTG não configurado (.env: PRTG_URL/USER/PASSHASH)."
        return stats

    if devices_map is None:
        try:
            devices_map = get_devices_map()
        except Exception as exc:  # noqa: BLE001
            logger.error("coletar_status: falha ao consultar PRTG — %s", exc)
            devices_map = {}
    if not devices_map:
        stats["ok"] = False
        stats["erro"] = "PRTG indisponível ou sem devices."
        return stats

    agora = timezone.now()
    rev = mapa_objid_item()  # {objid: item_id}
    stats["devices"] = len(devices_map)

    for objid, dev in devices_map.items():
        slug = dev.get("status_slug")
        if not slug:
            continue
        item_id = rev.get(objid)
        if item_id:
            stats["vinculados"] += 1
        try:
            criado = registrar_evento(
                objid, slug,
                quando=_quando_transicao(dev, agora),
                device_nome=dev.get("name", ""),
                device_host=dev.get("host", ""),
                device_grupo=dev.get("group", ""),
                item_id=item_id,
            )
            if criado:
                stats["eventos"] += 1
        except Exception as exc:  # noqa: BLE001 — um device não derruba a coleta
            logger.error("coletar_status: falha ao registrar objid %s — %s", objid, exc)

    return stats


# ──────────────────────────────────────────────────────────────────────────────
# Reconstrução de períodos / disponibilidade
# ──────────────────────────────────────────────────────────────────────────────

def periodos_e_totais(prtg_objid, inicio, agora):
    """
    Reconstrói os períodos contínuos de cada status a partir dos eventos gravados
    para o device, dentro da janela [inicio, agora] (inicio=None = histórico todo).

    REGRA IMPORTANTE: o status só é considerado conhecido a partir da PRIMEIRA
    observação real. O tempo anterior à primeira observação NÃO é contado como
    "online" — é marcado como `sem_dados`. Isso evita inventar disponibilidade de
    um período que nunca foi observado.

    Retorna (periodos, totais):
      periodos = [{status, inicio(datetime), fim(datetime), dias(float), pct(float)}]
      totais   = {status: dias(float)}   (inclui 'sem_dados' quando aplicável)
    """
    from ProjetoEstoque.models import ItemPRTGHistorico

    prtg_qs = ItemPRTGHistorico.objects.filter(prtg_objid=prtg_objid)
    periodos: list[dict] = []

    if inicio is not None:
        ult_ant = prtg_qs.filter(registrado_em__lt=inicio).order_by("-registrado_em").first()
        evs = list(prtg_qs.filter(registrado_em__gte=inicio, registrado_em__lte=agora).order_by("registrado_em"))
        if ult_ant:
            # Conhecemos o status no início da janela (havia observação antes dela)
            entry_status, dt0 = ult_ant.status_novo, inicio
        elif evs:
            # Sem observação antes da janela: trecho até a 1ª observação é "sem dados"
            primeiro = evs[0]
            if primeiro.registrado_em > inicio:
                dur = (primeiro.registrado_em - inicio).total_seconds() / 86400
                periodos.append({"status": "sem_dados", "inicio": inicio,
                                 "fim": primeiro.registrado_em, "dias": round(dur, 4), "pct": 0})
            entry_status, dt0 = primeiro.status_novo, primeiro.registrado_em
            evs = evs[1:]
        else:
            # Nenhuma observação na janela inteira
            entry_status, dt0 = "sem_dados", inicio
    else:
        prim = prtg_qs.order_by("registrado_em").first()
        if prim:
            entry_status, dt0 = prim.status_novo, prim.registrado_em
            evs = list(prtg_qs.filter(registrado_em__gt=prim.registrado_em, registrado_em__lte=agora).order_by("registrado_em"))
        else:
            entry_status, dt0, evs = "sem_dados", agora, []

    cur_dt, cur_st = dt0, entry_status
    for ev in evs:
        fim = ev.registrado_em
        if fim > cur_dt:
            dur = (fim - cur_dt).total_seconds() / 86400
            periodos.append({"status": cur_st, "inicio": cur_dt, "fim": fim, "dias": round(dur, 4), "pct": 0})
        cur_dt, cur_st = fim, ev.status_novo
    if cur_dt < agora:
        dur = (agora - cur_dt).total_seconds() / 86400
        periodos.append({"status": cur_st, "inicio": cur_dt, "fim": agora, "dias": round(dur, 4), "pct": 0})

    total = sum(p["dias"] for p in periodos)
    if total > 0:
        for p in periodos:
            p["pct"] = round(p["dias"] / total * 100, 2)

    totais: dict[str, float] = defaultdict(float)
    for p in periodos:
        totais[p["status"]] += p["dias"]

    return periodos, dict(totais)


def relatorio_monitoracao(dias: int = 30) -> tuple[list, dict]:
    """
    Monta o relatório de disponibilidade de TODOS os devices monitorados no PRTG
    (ao vivo + os que possuem histórico), agregando os eventos na janela escolhida.

    Retorna (linhas, resumo):
      linhas = lista de dicts por device (ordenada por mais indisponível);
      resumo = KPIs agregados do parque monitorado.
    """
    from django.utils import timezone
    from django.db.models import Min, Max
    from ProjetoEstoque.models import Item, ItemPRTGHistorico

    agora = timezone.now()
    inicio = agora - timedelta(days=dias) if dias > 0 else None

    devices_map = {}
    try:
        from services.prtg_service import get_devices_map
        devices_map = get_devices_map()
    except Exception:  # noqa: BLE001 — relatório funciona mesmo com PRTG offline
        devices_map = {}

    # Universo de devices: os que estão ao vivo no PRTG + os que têm histórico.
    objids_hist = set(
        ItemPRTGHistorico.objects.values_list("prtg_objid", flat=True).distinct()
    )
    universo = set(devices_map.keys()) | objids_hist

    rev = mapa_objid_item()  # {objid: item_id}
    itens = {
        it.pk: it for it in
        Item.objects.filter(pk__in=set(rev.values()))
        .select_related("localidade", "centro_custo")
    }

    linhas = []
    for objid in universo:
        dev = devices_map.get(objid)
        item = itens.get(rev.get(objid))

        periodos, totais = periodos_e_totais(objid, inicio, agora)

        up = totais.get("up", 0.0)
        down = totais.get("down", 0.0)
        warning = sum(totais.get(s, 0.0) for s in STATUS_WARNING)
        sem_dados = totais.get("sem_dados", 0.0)
        total = sum(totais.values())
        observado = total - sem_dados
        pct_up = round(up / observado * 100, 1) if observado > 0 else None

        evento_qs = ItemPRTGHistorico.objects.filter(prtg_objid=objid, status_novo="down")
        if inicio is not None:
            evento_qs = evento_qs.filter(registrado_em__gte=inicio, registrado_em__lte=agora)
        quedas = evento_qs.count()

        agg = ItemPRTGHistorico.objects.filter(prtg_objid=objid).aggregate(
            primeiro=Min("registrado_em"), ultimo=Max("registrado_em")
        )
        primeiro_reg, ultimo_reg = agg["primeiro"], agg["ultimo"]

        # Identidade do device: ao vivo (PRTG) ou último snapshot do histórico.
        if dev:
            nome_dev = dev.get("name") or ""
            host_dev = dev.get("host") or ""
            grupo_dev = dev.get("group") or ""
            status_efetivo = dev.get("status_slug")
            uptime_prtg = dev.get("uptime_pct")
            estado_desde = _quando_transicao(dev, agora)
        else:
            ult = ItemPRTGHistorico.objects.filter(prtg_objid=objid).order_by("-registrado_em").first()
            nome_dev = (ult.device_nome if ult else "") or ""
            host_dev = (ult.device_host if ult else "") or ""
            grupo_dev = (ult.device_grupo if ult else "") or ""
            status_efetivo = ult.status_novo if ult else None
            uptime_prtg = None
            estado_desde = None

        # Rótulo principal: nome do item se vinculado, senão o nome do device.
        if item is not None:
            titulo = item.nome
            subtitulo = item.numero_serie or host_dev
            localidade = item.localidade.local if item.localidade else (grupo_dev or "—")
        else:
            titulo = nome_dev or f"Device {objid}"
            subtitulo = host_dev
            localidade = grupo_dev or "—"

        linhas.append({
            "objid": objid,
            "item": item,
            "titulo": titulo,
            "subtitulo": subtitulo,
            "localidade": localidade,
            "device_nome": nome_dev,
            "device_host": host_dev,
            "status_atual": status_efetivo,
            "online": status_efetivo == "up",
            "offline": status_efetivo in STATUS_DOWN,
            "pct_up": pct_up,
            "uptime_prtg": uptime_prtg,
            "dias_up": round(up, 2),
            "dias_down": round(down, 2),
            "dias_warning": round(warning, 2),
            "horas_down": round(down * 24, 1),
            "dias_observados": round(observado, 2),
            "quedas": quedas,
            "estado_desde": estado_desde,
            "monitorado_desde": primeiro_reg,
            "ultimo_evento": ultimo_reg,
            "tem_dados": observado > 0,
        })

    # Ordena: offline agora → mais quedas → menor disponibilidade → nome
    linhas.sort(key=lambda r: (
        0 if r["offline"] else 1,
        -r["quedas"],
        r["pct_up"] if r["pct_up"] is not None else 101,
        (r["titulo"] or "").lower(),
    ))

    resumo = {
        "total": len(linhas),
        "online": sum(1 for r in linhas if r["online"]),
        "offline": sum(1 for r in linhas if r["offline"]),
        "com_quedas": sum(1 for r in linhas if r["quedas"] > 0),
        "sem_dados": sum(1 for r in linhas if not r["tem_dados"]),
        "vinculados": sum(1 for r in linhas if r["item"] is not None),
        "total_quedas": sum(r["quedas"] for r in linhas),
        "dias": dias,
    }
    return linhas, resumo
