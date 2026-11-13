"""
Peças comuns dos services da tecnologia agrícola (`tec_catalogo_service`,
`tec_estoque_service`, e os que vierem: instalação, infraestrutura, manutenção).

Não tem regra de negócio de nenhum cadastro: só o que os três repetiriam — normalizar texto
e número, exigir usuário, gravar a linha do tempo de cadastro (`TecEvento`) e descrever
um PONTO (de onde saiu / para onde foi) do razão. Fica separado para os services não
importarem uns aos outros em círculo.

Nada aqui toca o estoque de TI (`Item`, `MovimentacaoItem`, `Fornecedor`).
"""

from __future__ import annotations

import datetime
import re
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.utils import timezone

from ProjetoEstoque.models import TecEvento, TecMovimentacao

Ponto = TecMovimentacao.Ponto


# ════════════════════════════════════════════════════════
# Entrada de dados
# ════════════════════════════════════════════════════════

def usuario_ou_none(usuario):
    """O usuário se autenticado; senão `None` (o campo de auditoria aceita nulo)."""
    return usuario if getattr(usuario, "is_authenticated", False) else None


def exigir_usuario(usuario):
    """Movimentação sem autor não é registrável: é a trilha de auditoria."""
    if not getattr(usuario, "is_authenticated", False):
        raise ValidationError("Não foi possível identificar quem está fazendo esta operação.")
    return usuario


def nome_do_usuario(usuario) -> str:
    """O login no momento (sobrevive à exclusão do usuário no razão)."""
    return (getattr(usuario, "username", "") or "")[:150]


def quem(usuario) -> str:
    """Nome para mensagem: o nome completo se houver, senão o login."""
    if usuario is None:
        return "alguém"
    return (usuario.get_full_name() or usuario.username or "alguém").strip()


def texto(valor, maximo: int, campo: str, rotulo: str, *, obrigatorio: bool = False) -> str:
    """Texto aparado, com espaços internos colapsados. Levanta `ValidationError({campo: …})`."""
    limpo = " ".join(str(valor or "").split())
    if obrigatorio and not limpo:
        raise ValidationError({campo: f"Informe {rotulo}."})
    if len(limpo) > maximo:
        raise ValidationError({campo: f"{rotulo.capitalize()} aceita até {maximo} caracteres."})
    return limpo


def texto_livre(valor, campo: str, rotulo: str, maximo: int = 4000) -> str:
    """Texto de várias linhas (observações): só apara as pontas."""
    limpo = str(valor or "").strip()
    if len(limpo) > maximo:
        raise ValidationError({campo: f"{rotulo.capitalize()} aceita até {maximo} caracteres."})
    return limpo


def booleano(valor, padrao: bool = False) -> bool:
    """`True`/`False` a partir de bool, 'on', 'true', '1', 'sim'… Valor desconhecido = erro."""
    if valor is None or valor == "":
        return padrao
    if isinstance(valor, bool):
        return valor
    texto_ = str(valor).strip().lower()
    if texto_ in ("1", "true", "on", "sim", "s", "yes"):
        return True
    if texto_ in ("0", "false", "off", "nao", "não", "n", "no"):
        return False
    raise ValueError(valor)


def decimal(valor, campo: str, rotulo: str, *, casas: int = 3, minimo: Decimal | None = None) -> Decimal:
    """Número decimal (aceita vírgula). Recusa vazio, texto, infinito e casas além de `casas`.
    Levanta `ValidationError({campo: …})`."""
    bruto = str(valor if valor is not None else "").strip().replace(" ", "")
    if "," in bruto and "." in bruto:
        bruto = bruto.replace(".", "")       # 1.234,5 → 1234,5
    bruto = bruto.replace(",", ".")
    try:
        numero = Decimal(bruto)
    except (InvalidOperation, ValueError):
        raise ValidationError({campo: f"Informe {rotulo} como número."}) from None
    if not numero.is_finite():
        raise ValidationError({campo: f"Informe {rotulo} como número."})
    if numero != numero.quantize(Decimal(1).scaleb(-casas)):
        raise ValidationError({campo: f"{rotulo.capitalize()} aceita até {casas} casas decimais."})
    if minimo is not None and numero < minimo:
        raise ValidationError({campo: f"{rotulo.capitalize()} não pode ser menor que {minimo}."})
    return numero


def data(valor, campo: str, rotulo: str, *, permitir_futuro: bool = False, obrigatoria: bool = False):
    """`date` a partir de date/datetime, 'AAAA-MM-DD' ou 'DD/MM/AAAA'. Vazio = `None` (ou erro, se
    `obrigatoria`). Data no futuro é recusada, a não ser que `permitir_futuro`."""
    if valor in (None, ""):
        if obrigatoria:
            raise ValidationError({campo: f"Informe {rotulo}."})
        return None
    if isinstance(valor, datetime.datetime):
        dia = valor.date()
    elif isinstance(valor, datetime.date):
        dia = valor
    else:
        bruto = str(valor).strip()
        for formato in ("%Y-%m-%d", "%d/%m/%Y"):
            try:
                dia = datetime.datetime.strptime(bruto, formato).date()
                break
            except ValueError:
                continue
        else:
            raise ValidationError({campo: f"Informe {rotulo} como data válida."})
    if not permitir_futuro and dia > timezone.localdate():
        raise ValidationError({campo: f"{rotulo.capitalize()} não pode ser no futuro."})
    return dia


def so_digitos(valor) -> str:
    return re.sub(r"\D", "", str(valor or ""))


def documento_valido(digitos: str) -> bool:
    """CPF (11) ou CNPJ (14) com os dígitos verificadores corretos. Sequência repetida é inválida."""
    if len(digitos) not in (11, 14) or len(set(digitos)) == 1:
        return False
    numeros = [int(d) for d in digitos]
    if len(numeros) == 11:
        for tamanho in (9, 10):
            soma = sum(n * p for n, p in zip(numeros[:tamanho], range(tamanho + 1, 1, -1)))
            if (soma * 10 % 11) % 10 != numeros[tamanho]:
                return False
        return True
    pesos = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    for tamanho in (12, 13):
        soma = sum(n * p for n, p in zip(numeros[:tamanho], pesos[-tamanho:]))
        resto = soma % 11
        if (0 if resto < 2 else 11 - resto) != numeros[tamanho]:
            return False
    return True


def entidade(valor, modelo, campo: str, rotulo: str, *, obrigatorio: bool = True):
    """Instância de `modelo` a partir de instância ou id. Levanta `ValidationError({campo: …})`."""
    if valor in (None, ""):
        if obrigatorio:
            raise ValidationError({campo: f"Escolha {rotulo}."})
        return None
    if isinstance(valor, modelo):
        return valor
    try:
        return modelo.objects.get(pk=int(valor))
    except (ValueError, TypeError, modelo.DoesNotExist):
        raise ValidationError({campo: f"Escolha {rotulo} válido."}) from None


# ════════════════════════════════════════════════════════
# Auditoria e razão
# ════════════════════════════════════════════════════════

def evento(alvo: str, alvo_id: int, tipo: str, usuario, dados: dict) -> TecEvento:
    """Uma linha na linha do tempo de cadastro (imutável)."""
    return TecEvento.objects.create(alvo=alvo, alvo_id=alvo_id, tipo=tipo, usuario=usuario_ou_none(usuario), dados=dados)


def de_para(antes: dict, depois: dict) -> dict:
    """Só o que mudou: `{campo: {"de": x, "para": y}}`."""
    return {c: {"de": antes.get(c), "para": v} for c, v in depois.items() if antes.get(c) != v}


def ponto(tipo: str, objeto_id: int | None, rotulo: str) -> tuple[str, int | None, str]:
    """Um extremo de uma movimentação: (tipo, id, rótulo congelado)."""
    return (tipo, objeto_id, (rotulo or "")[:160])


def ponto_estoque(local) -> tuple[str, int | None, str]:
    return ponto(Ponto.ESTOQUE, local.pk, str(local))


def ponto_externo(rotulo: str) -> tuple[str, int | None, str]:
    return ponto(Ponto.EXTERNO, None, rotulo)


def agora():
    return timezone.now()
