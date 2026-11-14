"""
Cadastro rápido de dados de apoio (subtipo e localidade) a partir do formulário de item.

Quem cadastra um equipamento e descobre que o subtipo ou a localidade ainda não existe, antes, tinha de sair do
formulário, cadastrar na tela própria e começar de novo — perdendo o que já havia digitado. O formulário agora abre um
popup, grava por aqui e devolve o registro para o campo já selecionado.

Regras que este módulo segura (e que a view e o JS NÃO repetem):

  • A VALIDAÇÃO é a dos formulários de cadastro (`SubtipoForm`, `LocalidadeForm`): o popup e a tela própria nunca
    divergem sobre o que é um subtipo ou uma localidade válida.
  • NÃO DUPLICA. O banco não tem unicidade por nome, e num popup de "criar rápido" a duplicidade é o erro mais provável
    (a pessoa não achou na lista, digita de novo "Notebook" e o catálogo ganha o segundo). Nome igual — sem diferenciar
    maiúscula, acento nem espaço repetido — devolve o registro que JÁ existe, marcado `ja_existia`, e o formulário o
    seleciona. Para subtipo a comparação é dentro da mesma categoria: "Cabo" em Rede e "Cabo" em Energia podem coexistir.
  • Só o essencial. Os campos raros (`aceita_chip`, `e_chip`, o código da localidade) ficam nos padrões e podem ser
    ajustados depois na tela própria — o popup existe para não interromper o cadastro, não para substituí-la.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from django.db import transaction

from ProjetoEstoque.forms import LocalidadeForm, SubtipoForm
from ProjetoEstoque.models import Localidade, Subtipo


@dataclass
class Resultado:
    ok: bool
    id: int | None = None
    texto: str = ""                 # o que aparece no campo (mesmo texto que o <option> teria)
    ja_existia: bool = False
    erros: dict[str, list[str]] = field(default_factory=dict)


def _chave(texto: str) -> str:
    """Forma de comparar nomes: sem acento, sem caixa, espaços colapsados."""
    sem_acento = "".join(c for c in unicodedata.normalize("NFD", texto or "") if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", sem_acento).strip().casefold()


def _erros(form) -> dict[str, list[str]]:
    return {campo: [str(e) for e in lista] for campo, lista in form.errors.items()}


def criar_subtipo(dados, user) -> Resultado:
    """`dados`: nome, categoria (id) e alocado ("sim"/"nao"). Devolve o subtipo novo — ou o que já existia na categoria."""
    form = SubtipoForm({
        "nome": (dados.get("nome") or "").strip(),
        "categoria": dados.get("categoria") or "",
        "alocado": dados.get("alocado") or "",
    })
    if not form.is_valid():
        return Resultado(False, erros=_erros(form))

    nome, categoria = form.cleaned_data["nome"], form.cleaned_data["categoria"]
    with transaction.atomic():
        existente = next(
            (s for s in Subtipo.objects.filter(categoria=categoria).select_related("categoria") if _chave(s.nome) == _chave(nome)),
            None,
        )
        if existente:
            return Resultado(True, existente.pk, str(existente), ja_existia=True)
        subtipo = form.save(commit=False)
        subtipo.criado_por = user
        subtipo.atualizado_por = user
        subtipo.save()
    return Resultado(True, subtipo.pk, str(subtipo))


def criar_localidade(dados, user) -> Resultado:
    """`dados`: local (o nome). Devolve a localidade nova — ou a que já existia com o mesmo nome."""
    form = LocalidadeForm({"local": (dados.get("local") or "").strip()})
    if not form.is_valid():
        return Resultado(False, erros=_erros(form))

    nome = form.cleaned_data["local"]
    with transaction.atomic():
        existente = next((l for l in Localidade.objects.all() if _chave(l.local) == _chave(nome)), None)
        if existente:
            return Resultado(True, existente.pk, str(existente), ja_existia=True)
        localidade = form.save(commit=False)
        localidade.criado_por = user
        localidade.atualizado_por = user
        localidade.save()
    return Resultado(True, localidade.pk, str(localidade))
