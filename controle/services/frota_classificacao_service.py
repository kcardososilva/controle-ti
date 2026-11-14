"""
Categorias e classes de Frotas e Maquinários.

O ERP classifica o mesmo equipamento de quatro jeitos, cada um com um CÓDIGO e um nome: a empresa
("SANTA COLOMBA"), a classe operacional ("TRATOR LEVE"), a classe mecânica ("LINHA AMARELA") e o
centro de custo ("32107 · TRATOR LEVE - GRAOS"). Aqui ficam os cadastros desses quatro, mais a
organização que o ERP não tem: a CATEGORIA superior (`FrotaTipo`) que agrupa classes operacionais.

  • O NOME ORIGINAL SE PRESERVA. Agrupar "CAMINHÃO ROLL OFF" e "CAMINHÃO MUNCK" em "Caminhão" não
    apaga as duas classes: o equipamento continua sabendo qual é a dele, e o relatório pode ler pelo
    nome do ERP ou pela categoria.
  • O CÓDIGO DO ERP É A IDENTIDADE. A importação reconhece uma classe/empresa/centro pelo código
    (`obter_ou_criar`); o nome só entra se o código é novo. Se a planilha traz outro nome para um
    código que já existe, o cadastro NÃO é renomeado em silêncio — o aviso volta para quem confere.
  • SUGESTÃO NÃO É DECISÃO. `sugerir_categoria` e `sugerir_natureza` dizem o que a planilha parece
    pedir, e quem importa confere e muda antes de confirmar. Classe que nenhuma regra reconhece fica
    "sem categoria" (a tela mostra), e vai para "Outros" só porque o equipamento exige um tipo.
  • NADA SE APAGA. `ativo=False` tira das PRÓXIMAS escolhas e preserva quem já usa.

Tudo o que muda é auditado em `M2MEvento(config_frota)`, como os tipos e as operadoras.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Q

from ProjetoEstoque.models import (
    FrotaCentroCusto,
    FrotaClasseMecanica,
    FrotaClasseOperacional,
    FrotaEmpresa,
    FrotaEvento,
    FrotaNatureza,
    FrotaTipo,
    FrotaVeiculo,
)
from services import busca_service as busca
from services import frota_service as frota


@dataclass(frozen=True)
class Catalogo:
    chave: str
    modelo: type
    rotulo: str                  # "classe operacional" (singular, minúsculas)
    plural: str                  # "Classes operacionais"
    artigo: str                  # "a" | "o": concorda o texto das mensagens
    extras: tuple = ()           # campos próprios além de codigo_erp, nome e ativo


CATALOGOS = {
    c.chave: c for c in (
        Catalogo("classe_operacional", FrotaClasseOperacional, "classe operacional", "Classes operacionais", "a", ("categoria",)),
        Catalogo("classe_mecanica", FrotaClasseMecanica, "classe mecânica", "Classes mecânicas", "a"),
        Catalogo("empresa", FrotaEmpresa, "empresa", "Empresas", "a", ("natureza_padrao",)),
        Catalogo("centro_custo", FrotaCentroCusto, "centro de custo", "Centros de custo", "o"),
    )
}
LIMITE_NOME = 120
LIMITE_CODIGO = 20

# Como o ERP nomeia → a categoria superior que a organização sugere. A chave é o INÍCIO do nome da
# classe, sem acento nem caixa; a primeira que casar vale. Só sugere: quem importa confere.
CATEGORIA_SUGERIDA = (
    ("motocicleta", "Motocicleta"),
    ("utilitario", "Veículo utilitário"),
    ("automovel", "Veículo utilitário"),
    ("trator", "Trator"),
    ("gerador", "Gerador"),
    ("motor estacionario", "Gerador"),
    ("colheitadeira", "Colheitadeira"),
    ("caminhao", "Caminhão"),
    ("cavalo mecanico", "Caminhão"),
    ("pulverizador", "Pulverizador"),
    ("empilhadeira", "Empilhadeira"),
    ("pa carregadeira", "Linha amarela"),
    ("motoniveladora", "Linha amarela"),
    ("conservacao", "Linha amarela"),
    ("distr", "Distribuidor"),
    ("adubacao", "Distribuidor"),
    ("despontadeira", "Implemento agrícola"),
)
CATEGORIA_DE_RESERVA = "Outros"
# O que uma categoria NOVA, criada pela importação, já nasce esperando (as demais nascem sem expectativa:
# quem administra liga depois). Moto é emplacada e tem chassi; a falta deles é pendência.
ESPERA_DA_CATEGORIA_NOVA = {"Motocicleta": {"espera_placa": True, "espera_chassi": True}}


def sugerir_categoria(nome_da_classe: str) -> str:
    """O nome da categoria que a classe do ERP sugere, ou `""` se nenhuma regra a reconhece."""
    texto = busca.normalizar_busca(nome_da_classe)
    return next((categoria for inicio, categoria in CATEGORIA_SUGERIDA if texto.startswith(inicio)), "")


def sugerir_natureza(nome_da_empresa: str) -> str:
    """`proprio`, `terceiro` ou `""` (nome que nenhuma regra reconhece: quem importa escolhe)."""
    texto = busca.normalizar_busca(nome_da_empresa)
    if texto.startswith("santa colomba"):
        return FrotaNatureza.PROPRIO
    if texto.startswith("terceiro"):
        return FrotaNatureza.TERCEIRO
    return ""


# ═════════════════════════════════════════════════════════════
# Leitura
# ═════════════════════════════════════════════════════════════

def listar_cadastro(chave: str, busca_texto: str = ""):
    """O cadastro `chave` por nome, com `n_equipamentos` (quantos equipamentos o usam). `busca_texto`
    filtra por nome (sem acento nem caixa) ou código do ERP."""
    catalogo = CATALOGOS[chave]
    qs = catalogo.modelo.objects.annotate(n_equipamentos=Count("veiculos")).order_by("nome")
    if catalogo.chave == "classe_operacional":
        qs = qs.select_related("categoria")
    termo = busca.normalizar_busca(busca_texto)
    if termo:
        qs = qs.alias(nome_busca=busca.SemAcento("nome")).filter(
            Q(nome_busca__contains=termo) | Q(codigo_erp__icontains=busca_texto.strip())
        )
    return qs


def listar_categorias():
    """As categorias (`FrotaTipo`) com `n_veiculos` e `n_classes`."""
    return FrotaTipo.objects.annotate(n_veiculos=Count("veiculos", distinct=True), n_classes=Count("classes_operacionais", distinct=True)).order_by("nome")


def classes_sem_categoria() -> int:
    return FrotaClasseOperacional.objects.filter(categoria__isnull=True, ativo=True).count()


# ═════════════════════════════════════════════════════════════
# Gravação
# ═════════════════════════════════════════════════════════════

def _nome_livre(catalogo: Catalogo, nome, excluir_pk) -> str:
    nome = " ".join(str(nome or "").split())
    if not nome:
        raise ValidationError({"nome": f"Informe o nome d{catalogo.artigo} {catalogo.rotulo}."})
    if len(nome) > LIMITE_NOME:
        raise ValidationError({"nome": f"O nome aceita até {LIMITE_NOME} caracteres."})
    # comparação sem acento nem caixa, feita aqui: o `LOWER` do SQLite não iguala "É" a "é"
    alvo = busca.normalizar_busca(nome)
    existentes = catalogo.modelo.objects.exclude(pk=excluir_pk) if excluir_pk else catalogo.modelo.objects.all()
    if any(busca.normalizar_busca(outro) == alvo for outro in existentes.values_list("nome", flat=True)):
        raise ValidationError({"nome": f"Já existe {catalogo.artigo} {catalogo.rotulo} com este nome."})
    return nome


def _codigo_livre(catalogo: Catalogo, codigo, excluir_pk) -> str:
    codigo = " ".join(str(codigo or "").split())
    if len(codigo) > LIMITE_CODIGO:
        raise ValidationError({"codigo_erp": f"O código aceita até {LIMITE_CODIGO} caracteres."})
    if codigo:
        outros = catalogo.modelo.objects.filter(codigo_erp=codigo)
        if excluir_pk:
            outros = outros.exclude(pk=excluir_pk)
        if outros.exists():
            raise ValidationError({"codigo_erp": f"Já existe {catalogo.artigo} {catalogo.rotulo} com este código."})
    return codigo


def _valor_do_campo_extra(catalogo: Catalogo, campo: str, bruto, atual):
    if campo == "natureza_padrao":
        if bruto not in FrotaNatureza.values:
            raise ValidationError({campo: "Escolha Próprio ou Terceiro."})
        return bruto
    if campo == "categoria":
        if bruto in (None, "", 0, "0"):
            return None
        categoria = bruto if isinstance(bruto, FrotaTipo) else FrotaTipo.objects.filter(pk=int(bruto) if str(bruto).isdigit() else 0).first()
        if categoria is None:
            raise ValidationError({campo: "Categoria não encontrada."})
        if not categoria.ativo and getattr(atual, "pk", None) != categoria.pk:
            raise ValidationError({campo: f"A categoria {categoria.nome} está inativa."})
        return categoria
    return bruto


def _texto_do_valor(valor):
    return getattr(valor, "nome", valor)


def salvar_cadastro(chave: str, dados: dict, usuario=None, objeto=None):
    """Cria (`objeto=None`) ou edita um item do cadastro `chave` (`CATALOGOS`). `dados`: `nome` (único,
    sem acento nem caixa), `codigo_erp` (único quando preenchido), `ativo` e o campo próprio do cadastro
    (`categoria` da classe operacional, `natureza_padrao` da empresa). Só as chaves presentes mudam na
    edição; na criação o padrão é ativo. Audita em `M2MEvento(config_frota)` com o de/para. Não
    reclassifica equipamento: isso é `reclassificar_equipamentos`, uma ação explícita.
    `ValidationError({campo: msg})`."""
    catalogo = CATALOGOS[chave]
    criando = objeto is None
    alvo = catalogo.modelo() if criando else objeto
    campos = ("nome", "codigo_erp", "ativo", *catalogo.extras)
    antes = {} if criando else {c: getattr(alvo, c) for c in campos}
    novos: dict = {}
    if criando or "nome" in dados:
        novos["nome"] = _nome_livre(catalogo, dados.get("nome"), None if criando else alvo.pk)
    if "codigo_erp" in dados:
        novos["codigo_erp"] = _codigo_livre(catalogo, dados["codigo_erp"], None if criando else alvo.pk)
    if "ativo" in dados:
        novos["ativo"] = bool(dados["ativo"])
    elif criando:
        novos["ativo"] = True
    for campo in catalogo.extras:
        if campo in dados:
            novos[campo] = _valor_do_campo_extra(catalogo, campo, dados[campo], antes.get(campo))
    alteracoes = {c: {"de": _texto_do_valor(antes.get(c)), "para": _texto_do_valor(v)} for c, v in novos.items() if antes.get(c) != v}
    if not criando and not alteracoes:
        return alvo
    for campo, valor in novos.items():
        setattr(alvo, campo, valor)
    try:
        with transaction.atomic():
            alvo.save()
            frota.auditar_config(chave, alvo.pk, usuario, alteracoes, criado=criando)
    except IntegrityError:
        raise ValidationError({"nome": f"Já existe {catalogo.artigo} {catalogo.rotulo} com este nome ou código."}) from None
    return alvo


def reclassificar_equipamentos(classe: FrotaClasseOperacional, usuario=None) -> int:
    """Leva para a categoria da CLASSE todos os equipamentos dela que estão em outra. É a ação
    explícita "aplicar também aos equipamentos já cadastrados" (mudar a categoria de uma classe, sozinho,
    só vale para os próximos): cada equipamento ganha o seu `FrotaEvento(edicao)` com o de/para.
    Devolve quantos mudaram; classe sem categoria não reclassifica ninguém."""
    classe = FrotaClasseOperacional.objects.select_related("categoria").get(pk=classe.pk)
    if classe.categoria_id is None:
        return 0
    alvos = list(FrotaVeiculo.objects.filter(classe_operacional=classe).exclude(tipo=classe.categoria).select_related("tipo"))
    if not alvos:
        return 0
    usuario = usuario if getattr(usuario, "pk", None) else None
    with transaction.atomic():
        for veiculo in alvos:
            anterior = veiculo.tipo.nome
            FrotaVeiculo.objects.filter(pk=veiculo.pk).update(tipo=classe.categoria)
            FrotaEvento.objects.create(
                veiculo=veiculo, tipo=FrotaEvento.Tipo.EDICAO, usuario=usuario,
                dados={
                    "alteracoes": {"tipo": {"de": anterior, "para": classe.categoria.nome}},
                    "origem": "reclassificacao", "classe_operacional": classe.nome,
                },
            )
    return len(alvos)


# ═════════════════════════════════════════════════════════════
# Para a importação
# ═════════════════════════════════════════════════════════════

def obter_ou_criar(chave: str, codigo_erp: str, nome: str, usuario=None, **extras):
    """O item do cadastro `chave` com este código do ERP — criado se não existe. Devolve
    `(objeto, criado, aviso)`. O que já existe NUNCA é alterado: se o nome da planilha difere do
    cadastrado, `aviso` diz isso e o cadastro fica como está. Sem código (planilha sem a coluna),
    reconhece pelo nome."""
    catalogo = CATALOGOS[chave]
    codigo_erp = " ".join(str(codigo_erp or "").split())[:LIMITE_CODIGO]
    nome = " ".join(str(nome or "").split())
    existente = catalogo.modelo.objects.filter(codigo_erp=codigo_erp).first() if codigo_erp else None
    if existente is None and nome:
        alvo = busca.normalizar_busca(nome)
        existente = next(
            (o for o in catalogo.modelo.objects.all() if busca.normalizar_busca(o.nome) == alvo and (not codigo_erp or not o.codigo_erp)),
            None,
        )
    if existente is not None:
        aviso = ""
        if nome and busca.normalizar_busca(existente.nome) != busca.normalizar_busca(nome):
            aviso = f"{catalogo.rotulo.capitalize()} {codigo_erp}: a planilha traz “{nome}”, o cadastro tem “{existente.nome}” (mantido)."
        return existente, False, aviso
    dados = {"nome": nome, "codigo_erp": codigo_erp, **extras}
    return salvar_cadastro(chave, dados, usuario), True, ""


def obter_ou_criar_categoria(nome: str, usuario=None) -> tuple[FrotaTipo, bool]:
    """A categoria (`FrotaTipo`) de nome `nome`, sem acento nem caixa; criada se não existe, com a
    expectativa de identificação de `ESPERA_DA_CATEGORIA_NOVA`. Devolve `(categoria, criada)`."""
    alvo = busca.normalizar_busca(nome)
    existente = next((t for t in FrotaTipo.objects.all() if busca.normalizar_busca(t.nome) == alvo), None)
    if existente is not None:
        return existente, False
    return frota.salvar_tipo({"nome": nome, **ESPERA_DA_CATEGORIA_NOVA.get(nome, {})}, usuario), True
