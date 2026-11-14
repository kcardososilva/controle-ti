"""
Conferências do cadastro de Frotas e Maquinários: o que merece olho de quem responde pelo inventário.

A regra do módulo é SINALIZAR, NUNCA CORRIGIR. A planilha do ERP traz um chassi repetido em dois
equipamentos, uma data de compra de 1905, um ano-modelo que ainda não chegou: nada disso é
corrigido, recusado ou escondido — vira uma CONFERÊNCIA, visível na importação, na ficha do
equipamento e nos relatórios. Quem decide o que está certo é o responsável pelo inventário.

Também não é erro faltar placa ou chassi: gerador, trator e implemento nem sempre têm. A falta só
vira pendência quando o TIPO do equipamento espera ter (`FrotaTipo.espera_placa`/`espera_chassi`).

Duas formas da MESMA regra, que não podem divergir (há teste cruzando as duas):

  • `conferir(valores)` — em Python, sobre os valores de UM equipamento. Serve à importação (antes de
    existir o registro) e à ficha. Cada conferência é `{"codigo", "gravidade", "titulo", "detalhe"}`.
  • `q_*()` e `filtrar_pendencia` — em SQL, para contar e filtrar a frota inteira sem carregar linha
    por linha (indicadores e a lista).

Gravidade: `aviso` (algo que provavelmente está errado ou incompleto) e `info` (uma nota; pode estar
tudo certo). Nenhuma das duas bloqueia nada.
"""

from __future__ import annotations

import re
from datetime import date

from django.db.models import Count, Exists, OuterRef, Q

ANO_MINIMO = 1950
AVISO = "aviso"
INFO = "info"

_PLACA_ANTIGA = re.compile(r"^[A-Z]{3}\d{4}$")
_PLACA_MERCOSUL = re.compile(r"^[A-Z]{3}\d[A-Z]\d{2}$")

# código -> (gravidade, título curto). O texto longo sai de `conferir`, com os valores do equipamento.
REGRAS = {
    "identificacao_incompleta": (AVISO, "Identificação incompleta"),
    "chassi_duplicado": (AVISO, "Chassi repetido"),
    "data_compra_fora_de_faixa": (AVISO, "Data de compra improvável"),
    "compra_antes_da_fabricacao": (AVISO, "Compra antes da fabricação"),
    "ano_modelo_antes_da_fabricacao": (AVISO, "Ano-modelo anterior à fabricação"),
    "sem_classe_operacional": (AVISO, "Sem classe operacional"),
    "inicio_antes_da_compra": (INFO, "Início anterior à compra"),
    "ano_modelo_no_futuro": (INFO, "Ano-modelo à frente do ano corrente"),
    "placa_fora_do_padrao": (INFO, "Placa fora do padrão"),
    "sem_setor": (INFO, "Setor não informado"),
    "validacao_pendente": (INFO, "Situação a validar"),
}


def placa_no_padrao(placa: str) -> bool:
    """AAA9999 (antiga) ou AAA9A99 (Mercosul), já sem hífen e em caixa alta."""
    return bool(_PLACA_ANTIGA.match(placa or "") or _PLACA_MERCOSUL.match(placa or ""))


def _data(valor) -> date | None:
    return valor if isinstance(valor, date) else None


def _br(valor: date) -> str:
    return valor.strftime("%d/%m/%Y")


def _c(codigo: str, detalhe: str) -> dict:
    gravidade, titulo = REGRAS[codigo]
    return {"codigo": codigo, "gravidade": gravidade, "titulo": titulo, "detalhe": detalhe}


def conferir(valores: dict, *, hoje: date | None = None, chassi_em_outros=()) -> list[dict]:
    """As conferências de UM equipamento, as de `aviso` primeiro.

    `valores` (chaves ausentes valem vazio): `placa`, `chassi_serie`, `ano_fabricacao`, `ano_modelo`,
    `data_compra`, `data_inicio` (`date`), `setor`, `tem_classe_operacional` (bool), `espera_placa`,
    `espera_chassi` (do tipo), `validacao_pendente`. `chassi_em_outros`: os outros equipamentos que
    têm o MESMO chassi, como textos ("8210001 · COLHEI. GRAOS JDS660") — quem chama os procura
    (no banco e, na importação, também no próprio arquivo), porque só ele os enxerga."""
    hoje = hoje or date.today()
    placa = (valores.get("placa") or "").strip()
    chassi = (valores.get("chassi_serie") or "").strip()
    fabricacao, modelo = valores.get("ano_fabricacao"), valores.get("ano_modelo")
    compra, inicio = _data(valores.get("data_compra")), _data(valores.get("data_inicio"))
    achados: list[dict] = []

    faltam = []
    if valores.get("espera_placa") and not placa:
        faltam.append("placa")
    if valores.get("espera_chassi") and not chassi:
        faltam.append("chassi / nº de série")
    if faltam:
        achados.append(_c("identificacao_incompleta", f"Este tipo de equipamento costuma ter {' e '.join(faltam)}, e o cadastro não tem."))

    outros = list(chassi_em_outros)
    if chassi and outros:
        achados.append(_c("chassi_duplicado", f"O chassi {chassi} também consta em: {'; '.join(outros)}."))

    if compra is not None:
        if compra.year < ANO_MINIMO or compra > hoje:
            achados.append(_c("data_compra_fora_de_faixa", f"A data de compra ({_br(compra)}) está fora do intervalo plausível."))
        elif fabricacao and compra.year < fabricacao:
            achados.append(_c(
                "compra_antes_da_fabricacao",
                f"Comprado em {_br(compra)}, mas fabricado em {fabricacao}. Em carga de ERP isso costuma ser uma data provisória.",
            ))
    if fabricacao and modelo and modelo < fabricacao:
        achados.append(_c("ano_modelo_antes_da_fabricacao", f"O ano-modelo ({modelo}) é anterior ao ano de fabricação ({fabricacao})."))
    if not valores.get("tem_classe_operacional", True):
        achados.append(_c("sem_classe_operacional", "O equipamento não tem classe operacional."))

    if compra is not None and inicio is not None and inicio < compra:
        achados.append(_c("inicio_antes_da_compra", f"O início ({_br(inicio)}) é anterior à compra ({_br(compra)})."))
    if modelo and modelo > hoje.year:
        achados.append(_c(
            "ano_modelo_no_futuro",
            f"O ano-modelo {modelo} é posterior ao ano corrente. É comum quando o modelo do ano seguinte já está à venda; confira.",
        ))
    if placa and not placa_no_padrao(placa):
        achados.append(_c("placa_fora_do_padrao", f"“{placa}” não está no formato de placa (AAA9999 ou Mercosul): pode ser uma identificação interna."))
    if "setor" in valores and not (valores.get("setor") or "").strip():
        achados.append(_c("sem_setor", "O setor não foi informado."))
    if valores.get("validacao_pendente"):
        achados.append(_c("validacao_pendente", "A situação veio da planilha do ERP e ainda não foi confirmada pelo responsável."))

    achados.sort(key=lambda a: (a["gravidade"] != AVISO, list(REGRAS).index(a["codigo"])))
    return achados


def valores_do_veiculo(veiculo) -> dict:
    """Os valores de um `FrotaVeiculo` no formato de `conferir` (o `tipo` precisa estar carregado)."""
    return {
        "placa": veiculo.placa, "chassi_serie": veiculo.chassi_serie,
        "ano_fabricacao": veiculo.ano_fabricacao, "ano_modelo": veiculo.ano_modelo,
        "data_compra": veiculo.data_compra, "data_inicio": veiculo.data_inicio, "setor": veiculo.setor,
        "tem_classe_operacional": veiculo.classe_operacional_id is not None,
        "espera_placa": veiculo.tipo.espera_placa, "espera_chassi": veiculo.tipo.espera_chassi,
        "validacao_pendente": veiculo.validacao_pendente,
    }


def outros_com_o_mesmo_chassi(veiculo) -> list[str]:
    """Os outros equipamentos do cadastro com o chassi deste, como "código · nome"."""
    from ProjetoEstoque.models import FrotaVeiculo

    if not veiculo.chassi_serie:
        return []
    return [
        f"{o.codigo_legado or o.codigo} · {o.nome}"
        for o in FrotaVeiculo.objects.filter(chassi_serie=veiculo.chassi_serie).exclude(pk=veiculo.pk).order_by("codigo")
    ]


def conferencias_do_veiculo(veiculo, *, hoje: date | None = None) -> list[dict]:
    """As conferências de um equipamento já cadastrado (uma consulta a mais, só se ele tem chassi)."""
    return conferir(valores_do_veiculo(veiculo), hoje=hoje, chassi_em_outros=outros_com_o_mesmo_chassi(veiculo))


# ═════════════════════════════════════════════════════════════
# A mesma regra em SQL (indicadores e filtros da lista)
# ═════════════════════════════════════════════════════════════

def q_identificacao_incompleta() -> Q:
    """Falta o que o TIPO espera: placa onde se espera placa, chassi onde se espera chassi."""
    return Q(tipo__espera_placa=True, placa="") | Q(tipo__espera_chassi=True, chassi_serie="")


def q_chassi_duplicado() -> Q:
    """O chassi (preenchido) aparece em mais de um equipamento. Subconsulta correlata: serve à
    lista inteira sem carregar linha por linha."""
    from ProjetoEstoque.models import FrotaVeiculo

    outros = FrotaVeiculo.objects.filter(chassi_serie=OuterRef("chassi_serie")).exclude(pk=OuterRef("pk"))
    return ~Q(chassi_serie="") & Exists(outros)


PENDENCIAS = {
    "identificacao": ("Identificação incompleta", q_identificacao_incompleta),
    "chassi_duplicado": ("Chassi repetido", q_chassi_duplicado),
    "sem_classe": ("Sem classe operacional", lambda: Q(classe_operacional__isnull=True)),
    "sem_setor": ("Sem setor", lambda: Q(setor="")),
    "validacao": ("Situação a validar", lambda: Q(validacao_pendente=True)),
}


def filtrar_pendencia(queryset, chave: str):
    """Restringe a lista à pendência `chave` (uma de `PENDENCIAS`); chave desconhecida não filtra."""
    if chave not in PENDENCIAS:
        return queryset
    return queryset.filter(PENDENCIAS[chave][1]())


def contar_pendencias(queryset) -> dict:
    """`{chave: quantos}` de cada pendência sobre `queryset`, numa consulta só."""
    campos = {chave: Count("pk", filter=builder()) for chave, (_, builder) in PENDENCIAS.items()}
    return queryset.aggregate(**campos)
