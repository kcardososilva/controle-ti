"""
Filtros e tags do módulo Monitoramento TIM M2M.

Existem para que a MESMA coisa tenha o MESMO desenho em todas as telas — o
identificador em blocos de 4, o indicador de estado (quadrado + texto) e o
rótulo de frescor. Cada um é montado com `format_html`, então nada vindo do banco
chega ao HTML sem escape (e nenhum template do módulo usa `|safe`).

    {% load m2m_tags %}
    {{ chip.iccid|m2m_blocos }}              8955 1000 0000 0010 1
    {{ chip.consumo_bytes|m2m_bytes:1000 }}  1,5 GB
    {% m2m_ind chip.sessao_estado %}         ■ Sessão ativa
    {% m2m_frescor chip.frescor_sessao %}    há 3 h · desatualizado
    {% frota_status veiculo.status %}        [ATIVO]  (status OPERACIONAL: etiqueta de texto com fio)
    {% frota_conect veiculo.estado_conectividade %}   ■ Sessão ativa  (CONECTIVIDADE: o indicador quadrado)

Dois vocabulários, de propósito distintos: o STATUS operacional (do veículo, do chip em uso) é uma
etiqueta de texto com fio e nenhuma cor; a CONECTIVIDADE é o indicador quadrado. Um nunca deriva do
outro, e nunca dividem a mesma célula.
"""

from django import template
from django.utils import timezone
from django.utils.html import format_html

from services import frota_service
from services import m2m_interpretacao as interp

register = template.Library()

# Rótulo de cada chave de estado. As chaves são as MESMAS que o CSS conhece
# (`.m2m-ind[data-estado="..."]` em ds-m2m.css): acrescentar um estado aqui exige a
# regra de forma/cor lá — senão ele sai sem marcador.
ROTULOS_DE_ESTADO = {
    # sessão do chip (a que o mapeamento confirmou — ou não)
    "ativa": "Sessão ativa",
    "sem_sessao": "Sem sessão",
    "nao_interpretado": "Não interpretado",
    "nao_consultado": "Não consultado",
    # monitoramento do chip (saúde da CONSULTA, não do chip)
    "normal": "Normal",
    "pendente": "Pendente",
    "erro_consulta": "Falha de consulta",
    "desatualizado": "Desatualizado",
    "desativado": "Desativado",
    "sem_integracao": "Sem integração",     # operadora sem API: o chip é controlado, nunca consultado
    "sem_chip": "Sem chip",                 # veículo sem nenhum chip vinculado
    # integração
    "autenticada": "Autenticada",
    "nao_testada": "Configurada, não testada",
    "nao_configurada": "Não configurada",
    "credencial_recusada": "Credencial recusada",
    "bloqueada": "Bloqueada por limite",
    "url_invalida": "URL da API inválida",
    "token_ilegivel": "Token salvo ilegível",
    # nível da ocorrência
    "info": "Informativo",
    "atencao": "Atenção",
    "critico": "Crítico",
    # resultado de uma consulta
    "sucesso": "Sucesso",
    "sem_corpo": "Sem conteúdo",
    "falha": "Falha",
}


@register.filter
def m2m_blocos(valor):
    """ICCID/IMSI em blocos de 4 dígitos separados por espaço (só exibição: o valor
    guardado e o enviado à API continuam sem espaço). Vazio devolve vazio."""
    texto = "".join(str(valor or "").split())
    return " ".join(texto[i:i + 4] for i in range(0, len(texto), 4))


@register.filter
def m2m_bytes(valor, base=1000):
    """Volume legível ("1,5 GB"); "—" quando não há valor."""
    try:
        base = int(base)
    except (TypeError, ValueError):
        base = 1000
    return interp.formatar_bytes(valor, base)


@register.filter
def m2m_bytes_fixo(valor, base=1000):
    """Como `m2m_bytes`, mas com os dois decimais sempre ("14,50 MB"): é como o portal da Saitro mostra."""
    try:
        base = int(base)
    except (TypeError, ValueError):
        base = 1000
    return interp.formatar_bytes(valor, base, casas_fixas=True)


@register.filter
def m2m_duracao(segundos):
    """`929` -> "15m, 29s"; `3725` -> "1h, 2m, 5s"; `0` -> "0s" (a forma do portal); "—" sem valor."""
    try:
        total = int(segundos)
    except (TypeError, ValueError):
        return "—"
    if total < 0:
        return "—"
    horas, resto = divmod(total, 3600)
    minutos, segs = divmod(resto, 60)
    partes = [f"{horas}h" if horas else "", f"{minutos}m" if minutos else "", f"{segs}s" if segs or not total else ""]
    return ", ".join(p for p in partes if p)


@register.filter
def m2m_faltam(lista, total=24):
    """`range` das posições que faltam para completar `total` (a fita sempre tem 24 casas)."""
    try:
        return range(max(0, int(total) - len(lista or [])))
    except (TypeError, ValueError):
        return range(0)


@register.simple_tag
def m2m_ind(chave, rotulo=None, titulo=None):
    """Indicador de estado: quadrado de 8px + texto. A cor NUNCA vai sozinha — a
    forma do quadrado também muda (cheio, meio, tracejado, contorno), e é por
    isso que "falha de consulta" (contorno) não se confunde com "desativado" nem
    com "sem sessão". `rotulo` substitui o texto padrão; `titulo` vira tooltip."""
    chave = str(chave or "nao_consultado")
    texto = rotulo or ROTULOS_DE_ESTADO.get(chave, chave)
    if titulo:
        return format_html(
            '<span class="m2m-ind" data-estado="{}" title="{}"><i class="m2m-ind-forma" aria-hidden="true"></i>'
            "<span>{}</span></span>", chave, titulo, texto,
        )
    return format_html(
        '<span class="m2m-ind" data-estado="{}"><i class="m2m-ind-forma" aria-hidden="true"></i><span>{}</span></span>',
        chave, texto,
    )


@register.simple_tag
def m2m_frescor(f):
    """O rótulo de frescor que acompanha todo dado vindo da API, a partir do dict de
    `m2m_monitoramento_service.frescor()`: "há 3 h" e, se passou do limite
    configurado, "· desatualizado". Sem hora, "nunca consultado"."""
    if not f:
        return ""
    momento = f.get("momento")
    if momento is None:
        return format_html('<span class="m2m-frescor" data-velho="0">{}</span>', f.get("rotulo", "nunca consultado"))
    quando = timezone.localtime(momento).strftime("%d/%m/%Y às %H:%M")
    if f.get("desatualizado"):
        return format_html(
            '<span class="m2m-frescor" data-velho="1" title="Consulta de {}. Informação desatualizada.">{}'
            '<span class="m2m-frescor-velho"> · desatualizado</span></span>',
            quando, f.get("rotulo", ""),
        )
    return format_html(
        '<span class="m2m-frescor" data-velho="0" title="Consulta de {}.">{}</span>', quando, f.get("rotulo", ""),
    )


# ── Frotas: status operacional e conectividade do veículo ──────────────────────────────

# Status OPERACIONAL — do veículo (`FrotaVeiculo.Status`) e do chip em uso (`M2MChip.SituacaoUso`).
# As chaves são as do CSS (`.m2m-status[data-status="..."]`): acrescentar uma aqui exige a regra lá.
ROTULOS_DE_STATUS = {
    "ativo": "Em uso",                       # o valor gravado continua `ativo`; o ERP chama de "Em Uso"
    "manutencao": "Em manutenção",
    "inativo": "Inativo",
    "desativado": "Desativado",
    "disponivel": "Disponível",
    "vinculado": "Vinculado",
    # importação de planilha (a decisão de gente também): prévia, confirmada, cancelada
    "previa": "Prévia",
    "confirmada": "Confirmada",
    "cancelada": "Cancelada",
}

# Rótulo CURTO do resumo de conectividade do veículo (cabe numa célula); o texto longo do service
# (`ROTULO_VEICULO`) vai no tooltip. As chaves são as de `frota_service.ESTADOS_DE_CONECTIVIDADE`
# e todas já têm forma em `.m2m-ind[data-estado]`.
ROTULOS_CURTOS_DE_CONECTIVIDADE = {
    "sem_chip": "Sem chip",
    "ativa": "Sessão ativa",
    "sem_sessao": "Sem sessão",
    "falha": "Falha de consulta",
    "pendente": "Pendente",
    "desatualizado": "Desatualizado",
    "nao_interpretado": "Não interpretado",
    "sem_integracao": "Sem integração",
    "desativado": "Monitoramento desativado",
}


@register.simple_tag
def frota_status(chave):
    """Etiqueta do status OPERACIONAL: texto em caixa-alta dentro de um fio de 1px. Sem cor de estado
    — a conectividade já usa verde, âmbar e vermelho, e as duas leituras não podem se confundir."""
    chave = str(chave or "")
    return format_html(
        '<span class="m2m-status" data-status="{}">{}</span>', chave, ROTULOS_DE_STATUS.get(chave, chave),
    )


@register.simple_tag
def frota_conect(chave, titulo=None):
    """Indicador de conectividade de um VEÍCULO (ou de um chip, com as chaves de `ROTULO_CHIP`):
    o `m2m_ind` com o rótulo curto e, no tooltip, o texto completo do service."""
    chave = str(chave or "")
    longo = frota_service.ROTULO_VEICULO.get(chave, "")
    curto = ROTULOS_CURTOS_DE_CONECTIVIDADE.get(chave, longo or chave)
    return m2m_ind(chave, curto, titulo or (longo if longo != curto else None))
