"""
Frotas e Maquinários — a COMPOSIÇÃO TECNOLÓGICA da frota inteira: o que está instalado em cada máquina e quais máquinas não
têm nada instalado.

É uma LEITURA. Quem instala, retira e substitui é o estoque tecnológico (`tec_instalacao_service`), e quem vincula o chip é
`frota_vinculo_service`: aqui só se junta o que eles já registraram, sem criar componente, sem duplicar equipamento e sem
inventar instalação para preencher a tela. A composição de UMA máquina (a aba do detalhe) continua em
`tec_instalacao_service.composicao_do_veiculo`; esta é a mesma informação vista pela frota.

Componente é de dois tipos, e a tela diz qual: o EQUIPAMENTO TECNOLÓGICO (monitor, receptor GPS, modem, rádio…, por
`TecInstalacao` ativa) e o CHIP (por `FrotaVinculo` ativo). O chip não é equipamento do estoque tecnológico, mas é um
componente da máquina — o prompt o lista junto —, então entra na mesma lista, marcado como chip.
"""

from __future__ import annotations

from django.db.models import Count, F, Q

from ProjetoEstoque.models import FrotaNatureza, FrotaVeiculo, FrotaVinculo, TecInstalacao
from services import busca_service as busca
from services import frota_service as frota

Status = FrotaVeiculo.Status
CHIP = "chip"                       # a "categoria" dos chips no filtro (as outras são o id de `TecCategoria`)
ORIGEM_EQUIPAMENTO, ORIGEM_CHIP = "equipamento", "chip"


def _maquinas():
    """As máquinas que entram na conta: todas, menos as desativadas (arquivo morto do cadastro)."""
    return FrotaVeiculo.objects.exclude(status=Status.DESATIVADO)


def _instalacoes_ativas():
    return TecInstalacao.objects.filter(fim_em__isnull=True, veiculo__isnull=False)


def _vinculos_ativos():
    """Os chips que estão em MÁQUINA. O chip de um tablet/celular do estoque de TI (`veiculo` nulo) não
    é componente de máquina nenhuma — fica de fora da composição, das contagens e das linhas."""
    return FrotaVinculo.objects.filter(fim_em__isnull=True, veiculo__isnull=False)


def _com_tecnologia() -> Q:
    """As máquinas com ao menos um componente (equipamento instalado ou chip vinculado). Subconsulta, e não a lista de ids
    em Python: o `IN` com centenas de valores esbarra no limite de variáveis do SQLite."""
    return Q(pk__in=_instalacoes_ativas().values("veiculo_id")) | Q(pk__in=_vinculos_ativos().values("veiculo_id"))


def resumo() -> dict:
    """Os números do alto da página, tudo da base real:

        {"maquinas", "com_tecnologia", "sem_tecnologia", "componentes" (equipamentos instalados), "chips" (vínculos ativos),
         "por_categoria": [{"categoria_id", "nome", "grupo", "n"}] (equipamentos instalados, do mais ao menos comum)}"""
    maquinas = _maquinas()
    total = maquinas.count()
    com = maquinas.filter(_com_tecnologia()).count()
    por_categoria = list(
        _instalacoes_ativas()
        .values(categoria_id=F("equipamento__modelo__categoria_id"), nome=F("equipamento__modelo__categoria__nome"),
                grupo=F("equipamento__modelo__categoria__grupo"))
        .annotate(n=Count("pk")).order_by("-n", "nome")
    )
    return {
        "maquinas": total, "com_tecnologia": com, "sem_tecnologia": total - com, "componentes": _instalacoes_ativas().count(),
        "chips": _vinculos_ativos().count(), "por_categoria": por_categoria,
    }


def _natureza(filtros: dict) -> str:
    natureza = (filtros.get("natureza") or "").strip()
    return natureza if natureza in FrotaNatureza.values else ""


def componentes(filtros: dict | None = None, *, com_chips: bool = True) -> list[dict]:
    """Uma linha por componente instalado, da máquina de menor código à maior (e, na máquina, por categoria).

    `com_chips=False` deixa os chips de fora (quem não vê chips não vê o ICCID de ninguém). `filtros`: `q` (máquina,
    componente, série, patrimônio, fabricante, modelo — sem acento nem caixa), `categoria` (o id de uma categoria
    tecnológica, ou `chip`) e `natureza` da máquina. Cada linha: `origem` (`equipamento` | `chip`), `veiculo`,
    `categoria`, `grupo`, `fabricante`, `modelo`, `codigo`, `identificacao` (série, ou patrimônio; o ICCID no chip),
    `desde`, `por` (login) e `objeto` (o `TecEquipamento` ou o `M2MChip`, para o link). Sem resultado, lista vazia: nada
    é inventado."""
    filtros = filtros or {}
    categoria = str(filtros.get("categoria") or "").strip()
    natureza = _natureza(filtros)
    linhas: list[dict] = []

    if categoria == "" or categoria.isdigit():
        consulta = _instalacoes_ativas().select_related(
            "veiculo", "inicio_por", "equipamento__modelo__fabricante", "equipamento__modelo__categoria",
        )
        if categoria:
            consulta = consulta.filter(equipamento__modelo__categoria_id=int(categoria))
        if natureza:
            consulta = consulta.filter(veiculo__natureza=natureza)
        for i in consulta:
            e = i.equipamento
            linhas.append({
                "origem": ORIGEM_EQUIPAMENTO, "veiculo": i.veiculo, "categoria": e.modelo.categoria.nome, "grupo": e.modelo.categoria.get_grupo_display(),
                "fabricante": e.modelo.fabricante.nome, "modelo": e.modelo.nome, "codigo": e.codigo or "",
                "identificacao": e.numero_serie or e.patrimonio, "desde": i.inicio_em, "por": i.inicio_por.username if i.inicio_por_id else "",
                "objeto": e,
            })

    if com_chips and categoria in ("", CHIP):
        consulta = _vinculos_ativos().select_related("veiculo", "inicio_por", "chip__operadora")
        if natureza:
            consulta = consulta.filter(veiculo__natureza=natureza)
        for v in consulta:
            c = v.chip
            linhas.append({
                "origem": ORIGEM_CHIP, "veiculo": v.veiculo, "categoria": "Chip", "grupo": "Conectividade",
                "fabricante": c.operadora.nome, "modelo": "", "codigo": c.nome, "identificacao": c.iccid or c.imsi,
                "desde": v.inicio_em, "por": v.inicio_por.username if v.inicio_por_id else "", "objeto": c,
            })

    termo = busca.normalizar_busca(filtros.get("q") or "")
    if termo:
        def texto(l):
            v = l["veiculo"]
            return busca.normalizar_busca(" ".join(str(x) for x in (
                v.codigo_legado, v.codigo, v.nome, v.placa, l["categoria"], l["fabricante"], l["modelo"], l["codigo"], l["identificacao"],
                getattr(l["objeto"], "patrimonio", ""),
            )))
        linhas = [l for l in linhas if termo in texto(l)]

    linhas.sort(key=lambda l: (l["veiculo"].codigo_legado or "~", l["veiculo"].codigo, l["origem"] == ORIGEM_CHIP, l["categoria"].casefold(), l["codigo"]))
    return linhas


def maquinas_sem_tecnologia(filtros: dict | None = None):
    """As máquinas SEM componente nenhum (nem equipamento instalado, nem chip), como QuerySet, do menor código ao maior.
    `filtros`: `q` (a busca da tela principal: código, nome, modelo, placa, chassi…) e `natureza`. É a resposta a "o que ainda não tem tecnologia?",
    e é só uma LEITURA: máquina sem tecnologia não é erro (um implemento não tem monitor), por isso a tela não a alarma."""
    filtros = filtros or {}
    # A lista é a MESMA da tela principal (busca sem acento, natureza, e `chip=sem` = nenhum vínculo ativo): só falta tirar
    # quem tem equipamento instalado. Duas implementações da busca de equipamento divergiriam na primeira regra nova.
    consulta = frota.listar_veiculos({"q": filtros.get("q") or "", "natureza": _natureza(filtros), "chip": "sem", "ordem": "legado"})
    return consulta.exclude(status=Status.DESATIVADO).exclude(pk__in=_instalacoes_ativas().values("veiculo_id"))
