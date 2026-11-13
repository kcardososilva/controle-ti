"""
Catálogo da tecnologia agrícola do Zelo Agro: fabricantes, categorias, modelos, unidades de
medida, locais de armazenamento e fornecedores. É a base sobre a qual o estoque
(`tec_estoque_service`) e o resto do módulo se apoiam.

Camadas: views -> este service -> models. Nada aqui toca o estoque de TI.

Decisões que moldam este módulo — e não são óbvias olhando o código:

  • NÃO SE APAGA CADASTRO DE APOIO, SE INATIVA. Fabricante, categoria, modelo, unidade, local e
    fornecedor são referenciados por equipamento, saldo e razão (todos com PROTECT). Apagar um
    deles ou falharia ou — pior — deixaria o histórico sem dizer a que se refere. Inativo some
    das listas de escolha e continua aparecendo no que já o usa. Por isso não há `excluir_*`.
  • QUEM DECIDE O CONTROLE É O MODELO (`TecModelo.controle`): `individual` = um equipamento por
    número de série; `quantidade` = saldo por local. A categoria só sugere. Mudar o controle de
    um modelo que já tem movimento é RECUSADO — equipamentos individuais não viram saldo e
    vice-versa; o caminho é inativar o modelo e criar outro.
  • NÃO HÁ MODELO DE FÁBRICA. Os fabricantes e categorias semeados (migration 0194) são só os
    que a spec nomeia; modelo é sempre o que a empresa de fato usa, cadastrado aqui.
  • A UNIDADE (fazenda) DO LOCAL É SÓ KARITEL OU RIO DO MEIO — a mesma restrição da tela de
    veículo (`frota_service.fazendas()`), conferida AQUI e não só na tela.
  • TODA ALTERAÇÃO É AUDITADA em `TecEvento` (de/para), na mesma transação do save.

Contratos de leitura para as telas: `listar_fabricantes`, `listar_categorias`, `listar_unidades`,
`listar_modelos`, `listar_locais`, `listar_fornecedores`. Cada docstring diz o que devolve.
"""

from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, models, transaction
from django.db.models import Count, Q
from django.db.models.functions import Floor

from ProjetoEstoque.models import (
    AgroFornecedor,
    AgroLocal,
    AgroUnidadeMedida,
    Localidade,
    TecCategoria,
    TecControle,
    TecEquipamento,
    TecEvento,
    TecFabricante,
    TecModelo,
    TecMovimentacao,
    TecSaldo,
)
from services import tec_base as base

Alvo = TecEvento.Alvo
Tipo = TecEvento.Tipo


# ════════════════════════════════════════════════════════
# Núcleo comum de gravação
# ════════════════════════════════════════════════════════

def _prim(valor):
    """O valor como vai para a linha do tempo (JSON): instância vira o texto dela."""
    if isinstance(valor, models.Model):
        return str(valor)
    if isinstance(valor, Decimal):
        return str(valor)
    return valor


def _nome_unico(modelo, valor, excluir_pk, *, informe: str, existe: str, maximo: int, campo: str = "nome", filtro: dict | None = None) -> str:
    """Texto único (sem distinguir caixa) dentro de `modelo` (e de `filtro`, se houver).
    `informe` completa "Informe …" e `existe` completa "Já existe …" nas mensagens."""
    limpo = base.texto(valor, maximo, campo, informe, obrigatorio=True)
    existentes = modelo.objects.filter(**{f"{campo}__iexact": limpo}, **(filtro or {}))
    if excluir_pk:
        existentes = existentes.exclude(pk=excluir_pk)
    if existentes.exists():
        raise ValidationError({campo: f"Já existe {existe}."})
    return limpo


def _ativo(dados: dict, criando: bool, valores: dict) -> None:
    if criando or "ativo" in dados:
        try:
            valores["ativo"] = base.booleano(dados.get("ativo"), True)
        except ValueError:
            raise ValidationError({"ativo": "Valor inválido."}) from None


def _persistir(instancia, valores: dict, alvo: str, usuario, erro_unico: dict, *, criando: bool):
    """Aplica `valores`, grava e audita (de/para) numa transação. Se nada mudou na edição, não grava."""
    antes = {c: _prim(getattr(instancia, c)) for c in valores} if not criando else {}
    depois = {c: _prim(v) for c, v in valores.items()}
    alteracoes = base.de_para(antes, depois)
    if not criando and not alteracoes:
        return instancia
    for campo, valor in valores.items():
        setattr(instancia, campo, valor)
    autor = base.usuario_ou_none(usuario)
    if hasattr(instancia, "atualizado_por"):
        instancia.atualizado_por = autor
        if criando:
            instancia.criado_por = autor
    try:
        with transaction.atomic():
            instancia.save()
            base.evento(
                alvo, instancia.pk, Tipo.CADASTRO if criando else Tipo.EDICAO, usuario,
                {"criado": criando, "alteracoes": alteracoes} if not criando else {"criado": True, "dados": depois},
            )
    except IntegrityError:
        raise ValidationError(erro_unico) from None
    return instancia


# ════════════════════════════════════════════════════════
# Fabricantes
# ════════════════════════════════════════════════════════

def listar_fabricantes(somente_ativos: bool = False):
    """QuerySet de `TecFabricante` por nome, com `n_modelos` e `n_equipamentos` anotados."""
    qs = TecFabricante.objects.annotate(
        n_modelos=Count("modelos", distinct=True), n_equipamentos=Count("modelos__equipamentos", distinct=True),
    ).order_by("nome")
    return qs.filter(ativo=True) if somente_ativos else qs


def salvar_fabricante(dados: dict, usuario=None, fabricante: TecFabricante | None = None) -> TecFabricante:
    """Cria (`fabricante=None`) ou edita um fabricante. `dados`: `nome` (único, sem distinguir
    caixa) e `ativo` (só as chaves presentes mudam na edição). Inativo some das listas de escolha
    mas os modelos dele ficam. Audita em `TecEvento`. `ValidationError({campo: msg})`."""
    criando = fabricante is None
    alvo = TecFabricante() if criando else fabricante
    valores: dict = {}
    if criando or "nome" in dados:
        valores["nome"] = _nome_unico(TecFabricante, dados.get("nome"), alvo.pk, informe="o nome do fabricante", existe="um fabricante com este nome", maximo=80)
    _ativo(dados, criando, valores)
    return _persistir(alvo, valores, Alvo.FABRICANTE, usuario, {"nome": "Já existe um fabricante com este nome."}, criando=criando)


# ════════════════════════════════════════════════════════
# Categorias
# ════════════════════════════════════════════════════════

def listar_categorias(somente_ativas: bool = False):
    """QuerySet de `TecCategoria` por grupo e nome, com `n_modelos` anotado."""
    qs = TecCategoria.objects.annotate(n_modelos=Count("modelos", distinct=True)).order_by("grupo", "nome")
    return qs.filter(ativo=True) if somente_ativas else qs


def salvar_categoria(dados: dict, usuario=None, categoria: TecCategoria | None = None) -> TecCategoria:
    """Cria ou edita uma categoria. `dados`: `nome` (único), `grupo` (um de `TecCategoria.Grupo` —
    é ele que roteia as áreas do módulo), `controle_padrao` (só sugere o controle do modelo) e `ativo`."""
    criando = categoria is None
    alvo = TecCategoria() if criando else categoria
    valores: dict = {}
    if criando or "nome" in dados:
        valores["nome"] = _nome_unico(TecCategoria, dados.get("nome"), alvo.pk, informe="o nome da categoria", existe="uma categoria com este nome", maximo=80)
    if criando or "grupo" in dados:
        grupo = dados.get("grupo") or (TecCategoria.Grupo.OUTRO if criando else alvo.grupo)
        if grupo not in TecCategoria.Grupo.values:
            raise ValidationError({"grupo": "Escolha um grupo válido."})
        valores["grupo"] = grupo
    if criando or "controle_padrao" in dados:
        controle = dados.get("controle_padrao") or TecControle.INDIVIDUAL
        if controle not in TecControle.values:
            raise ValidationError({"controle_padrao": "Escolha um tipo de controle válido."})
        valores["controle_padrao"] = controle
    _ativo(dados, criando, valores)
    return _persistir(alvo, valores, Alvo.CATEGORIA, usuario, {"nome": "Já existe uma categoria com este nome."}, criando=criando)


# ════════════════════════════════════════════════════════
# Unidades de medida
# ════════════════════════════════════════════════════════

def listar_unidades(somente_ativas: bool = False):
    """QuerySet de `AgroUnidadeMedida` por sigla, com `n_modelos` anotado."""
    qs = AgroUnidadeMedida.objects.annotate(n_modelos=Count("modelos", distinct=True)).order_by("sigla")
    return qs.filter(ativo=True) if somente_ativas else qs


def salvar_unidade(dados: dict, usuario=None, unidade: AgroUnidadeMedida | None = None) -> AgroUnidadeMedida:
    """Cria ou edita uma unidade de medida. `dados`: `sigla` (única, até 8), `nome`, `fracionavel`
    (aceita casas decimais) e `ativo`. Tornar NÃO fracionável uma unidade que já tem material em
    saldo fracionado é recusado: o saldo existente deixaria de ser um valor válido."""
    criando = unidade is None
    alvo = AgroUnidadeMedida() if criando else unidade
    valores: dict = {}
    if criando or "sigla" in dados:
        valores["sigla"] = _nome_unico(AgroUnidadeMedida, dados.get("sigla"), alvo.pk, informe="a sigla da unidade", existe="uma unidade com esta sigla", maximo=8, campo="sigla")
    if criando or "nome" in dados:
        valores["nome"] = base.texto(dados.get("nome"), 40, "nome", "o nome da unidade", obrigatorio=True)
    if criando or "fracionavel" in dados:
        try:
            valores["fracionavel"] = base.booleano(dados.get("fracionavel"), False)
        except ValueError:
            raise ValidationError({"fracionavel": "Valor inválido."}) from None
        if not criando and alvo.fracionavel and not valores["fracionavel"]:
            fracionados = TecSaldo.objects.filter(modelo__unidade=alvo).exclude(quantidade=Floor("quantidade"))
            if fracionados.exists():
                raise ValidationError({"fracionavel": "Há saldo com casas decimais nesta unidade; ajuste-o antes de torná-la inteira."})
    _ativo(dados, criando, valores)
    return _persistir(alvo, valores, Alvo.UNIDADE, usuario, {"sigla": "Já existe uma unidade com esta sigla."}, criando=criando)


# ════════════════════════════════════════════════════════
# Modelos
# ════════════════════════════════════════════════════════

def _modelo_tem_historico(modelo: TecModelo) -> bool:
    return (
        TecEquipamento.objects.filter(modelo=modelo).exists()
        or TecMovimentacao.objects.filter(modelo=modelo).exists()
        or TecSaldo.objects.filter(modelo=modelo).exists()
    )


def listar_modelos(filtros: dict | None = None):
    """QuerySet de `TecModelo` com fabricante, categoria e unidade já carregados e `n_equipamentos`
    anotado. `filtros` (todos opcionais, valor vazio é ignorado): `q` (nome do modelo, do fabricante
    ou da categoria), `fabricante`, `categoria`, `grupo`, `controle`, `ativo` ('1'/'0')."""
    filtros = {k: v for k, v in (filtros or {}).items() if v not in (None, "")}
    qs = TecModelo.objects.select_related("fabricante", "categoria", "unidade").annotate(n_equipamentos=Count("equipamentos", distinct=True))
    if filtros.get("q"):
        termo = str(filtros["q"]).strip()
        qs = qs.filter(Q(nome__icontains=termo) | Q(fabricante__nome__icontains=termo) | Q(categoria__nome__icontains=termo))
    for campo, caminho in (("fabricante", "fabricante_id"), ("categoria", "categoria_id")):
        if filtros.get(campo):
            try:
                qs = qs.filter(**{caminho: int(filtros[campo])})
            except (TypeError, ValueError):
                qs = qs.none()
    if filtros.get("grupo"):
        qs = qs.filter(categoria__grupo=filtros["grupo"])
    if filtros.get("controle") in TecControle.values:
        qs = qs.filter(controle=filtros["controle"])
    if filtros.get("ativo") in ("1", "0", 1, 0, True, False):
        qs = qs.filter(ativo=str(filtros["ativo"]) in ("1", "True"))
    return qs.order_by("fabricante__nome", "nome")


def salvar_modelo(dados: dict, usuario=None, modelo: TecModelo | None = None) -> TecModelo:
    """Cria ou edita um modelo do catálogo. `dados`: `fabricante`, `categoria` (instância ou id),
    `nome` (único dentro do fabricante), `controle` ('individual' | 'quantidade'; vazio na criação =
    o padrão da categoria), `unidade` (obrigatória no controle por quantidade; no individual é
    descartada), `estoque_minimo` (≥ 0; inteiro no individual), `observacoes`, `ativo`.

    Na EDIÇÃO, `fabricante`, `controle` e `unidade` só mudam enquanto o modelo não tem equipamento,
    saldo nem movimentação — depois disso mudar reescreveria o que o histórico significa.
    Fabricante e categoria INATIVOS não aceitam modelo novo. `ValidationError({campo: msg})`."""
    criando = modelo is None
    alvo = TecModelo() if criando else modelo
    historico = (not criando) and _modelo_tem_historico(alvo)
    erros: dict = {}
    valores: dict = {}

    def tentar(passo):
        try:
            return passo()
        except ValidationError as exc:
            erros.update(exc.message_dict if hasattr(exc, "error_dict") else {"__all__": exc.messages})
            return None

    if criando or "fabricante" in dados:
        fab = tentar(lambda: base.entidade(dados.get("fabricante"), TecFabricante, "fabricante", "o fabricante"))
        if fab is not None:
            if criando and not fab.ativo:
                erros["fabricante"] = "Este fabricante está inativo."
            elif historico and fab.pk != alvo.fabricante_id:
                erros["fabricante"] = "Este modelo já tem movimentação: não dá para trocar o fabricante."
            else:
                valores["fabricante"] = fab
    if criando or "categoria" in dados:
        cat = tentar(lambda: base.entidade(dados.get("categoria"), TecCategoria, "categoria", "a categoria"))
        if cat is not None:
            if criando and not cat.ativo:
                erros["categoria"] = "Esta categoria está inativa."
            else:
                valores["categoria"] = cat
    controle_atual = alvo.controle if not criando else None
    if criando or "controle" in dados:
        controle = dados.get("controle") or (valores["categoria"].controle_padrao if "categoria" in valores else TecControle.INDIVIDUAL)
        if controle not in TecControle.values:
            erros["controle"] = "Escolha um tipo de controle válido."
        elif historico and controle != controle_atual:
            erros["controle"] = "Este modelo já tem equipamentos, saldo ou movimentação: não dá para mudar o tipo de controle. Inative-o e crie outro."
        else:
            valores["controle"] = controle
    controle_final = valores.get("controle", controle_atual)
    if criando or "nome" in dados:
        fab_ref = valores.get("fabricante") or (alvo.fabricante if not criando else None)
        def _nome():
            if fab_ref is None:
                return base.texto(dados.get("nome"), 120, "nome", "o nome do modelo", obrigatorio=True)
            return _nome_unico(TecModelo, dados.get("nome"), alvo.pk, informe="o nome do modelo", existe="um modelo com este nome para o fabricante", maximo=120, filtro={"fabricante": fab_ref})
        nome = tentar(_nome)
        if nome is not None:
            valores["nome"] = nome
    if criando or "unidade" in dados or "controle" in valores:
        if controle_final == TecControle.QUANTIDADE:
            un = tentar(lambda: base.entidade(
                dados.get("unidade", None if criando else alvo.unidade_id), AgroUnidadeMedida, "unidade", "a unidade de medida",
            ))
            if un is not None:
                if criando and not un.ativo:
                    erros["unidade"] = "Esta unidade está inativa."
                elif historico and (alvo.unidade_id is not None) and un.pk != alvo.unidade_id:
                    erros["unidade"] = "Este modelo já tem saldo ou movimentação: não dá para trocar a unidade."
                else:
                    valores["unidade"] = un
        elif controle_final == TecControle.INDIVIDUAL:
            valores["unidade"] = None
    if criando or "estoque_minimo" in dados:
        minimo = tentar(lambda: base.decimal(dados.get("estoque_minimo", "0") or "0", "estoque_minimo", "o estoque mínimo", minimo=Decimal("0")))
        if minimo is not None:
            un_ref = valores.get("unidade") or (alvo.unidade if not criando else None)
            inteiro = controle_final == TecControle.INDIVIDUAL or (un_ref is not None and not un_ref.fracionavel)
            if inteiro and minimo != minimo.to_integral_value():
                erros["estoque_minimo"] = "O estoque mínimo deste modelo é em unidades inteiras."
            else:
                valores["estoque_minimo"] = minimo
    if criando or "observacoes" in dados:
        valores["observacoes"] = tentar(lambda: base.texto_livre(dados.get("observacoes"), "observacoes", "as observações técnicas")) or ""
    _ativo(dados, criando, valores)
    if erros:
        raise ValidationError(erros)
    return _persistir(alvo, valores, Alvo.MODELO, usuario, {"nome": "Já existe um modelo com este nome para o fabricante."}, criando=criando)


# ════════════════════════════════════════════════════════
# Locais de armazenamento
# ════════════════════════════════════════════════════════

def listar_locais(somente_ativos: bool = False):
    """QuerySet de `AgroLocal` por fazenda e nome, com a fazenda carregada e `n_equipamentos`
    (equipamentos guardados lá) anotado."""
    qs = AgroLocal.objects.select_related("fazenda").annotate(n_equipamentos=Count("equipamentos", distinct=True)).order_by("fazenda__local", "nome")
    return qs.filter(ativo=True) if somente_ativos else qs


def salvar_local(dados: dict, usuario=None, local: AgroLocal | None = None) -> AgroLocal:
    """Cria ou edita um local de armazenamento. `dados`: `nome` (único dentro da fazenda), `fazenda`
    (só Karitel ou Rio do Meio — conferido aqui), `observacoes`, `ativo`. Inativar um local que
    ainda guarda equipamento ou saldo é recusado: o material ficaria num lugar que ninguém escolhe."""
    from services import frota_service  # tardio: o frota_service importa m2m e este arquivo não precisa dele no import

    criando = local is None
    alvo = AgroLocal() if criando else local
    erros: dict = {}
    valores: dict = {}
    if criando or "fazenda" in dados:
        try:
            fazenda = base.entidade(dados.get("fazenda"), Localidade, "fazenda", "a unidade / fazenda")
            if fazenda.pk not in {f.pk for f in frota_service.fazendas()}:
                erros["fazenda"] = "Escolha a Fazenda Karitel ou a Fazenda Rio do Meio."
            else:
                valores["fazenda"] = fazenda
        except ValidationError as exc:
            erros.update(exc.message_dict)
    if criando or "nome" in dados:
        try:
            fazenda_ref = valores.get("fazenda") or (alvo.fazenda if not criando else None)
            if fazenda_ref is None:
                valores["nome"] = base.texto(dados.get("nome"), 100, "nome", "o nome do local", obrigatorio=True)
            else:
                valores["nome"] = _nome_unico(AgroLocal, dados.get("nome"), alvo.pk, informe="o nome do local", existe="um local com este nome nesta fazenda", maximo=100, filtro={"fazenda": fazenda_ref})
        except ValidationError as exc:
            erros.update(exc.message_dict)
    if criando or "observacoes" in dados:
        try:
            valores["observacoes"] = base.texto(dados.get("observacoes"), 255, "observacoes", "as observações")
        except ValidationError as exc:
            erros.update(exc.message_dict)
    _ativo(dados, criando, valores)
    if erros:
        raise ValidationError(erros)
    if not criando and alvo.ativo and valores.get("ativo") is False:
        guarda = TecEquipamento.objects.filter(local=alvo).exists() or TecSaldo.objects.filter(local=alvo, quantidade__gt=0).exists()
        if guarda:
            raise ValidationError({"ativo": "Este local ainda guarda equipamentos ou materiais. Transfira-os antes de inativá-lo."})
    return _persistir(alvo, valores, Alvo.LOCAL, usuario, {"nome": "Já existe um local com este nome nesta fazenda."}, criando=criando)


# ════════════════════════════════════════════════════════
# Fornecedores (enxuto — o módulo completo vem depois)
# ════════════════════════════════════════════════════════

def listar_fornecedores(somente_ativos: bool = False, busca: str = ""):
    """QuerySet de `AgroFornecedor` por nome, com `n_equipamentos` anotado. `busca` filtra por nome,
    documento (só dígitos) ou contato."""
    qs = AgroFornecedor.objects.annotate(n_equipamentos=Count("equipamentos", distinct=True)).order_by("nome")
    if busca:
        termo = str(busca).strip()
        digitos = base.so_digitos(termo)
        filtro = Q(nome__icontains=termo) | Q(contato__icontains=termo)
        if digitos:
            filtro |= Q(documento__contains=digitos)
        qs = qs.filter(filtro)
    return qs.filter(ativo=True) if somente_ativos else qs


def salvar_fornecedor(dados: dict, usuario=None, fornecedor: AgroFornecedor | None = None) -> AgroFornecedor:
    """Cria ou edita um fornecedor do Agro. `dados`: `nome`, `documento` (CNPJ ou CPF com os dígitos
    verificadores corretos; gravado só com dígitos; único quando preenchido), `contato`, `telefone`,
    `email`, `observacoes`, `ativo`. É um cadastro SEPARADO do `Fornecedor` do TI (que é a âncora
    dos portais externos)."""
    criando = fornecedor is None
    alvo = AgroFornecedor() if criando else fornecedor
    erros: dict = {}
    valores: dict = {}

    def tentar(campo, passo):
        try:
            valores[campo] = passo()
        except ValidationError as exc:
            erros.update(exc.message_dict if hasattr(exc, "error_dict") else {campo: exc.messages[0]})

    if criando or "nome" in dados:
        tentar("nome", lambda: base.texto(dados.get("nome"), 120, "nome", "o nome do fornecedor", obrigatorio=True))
    if criando or "documento" in dados:
        digitos = base.so_digitos(dados.get("documento"))
        if digitos and not base.documento_valido(digitos):
            erros["documento"] = "CNPJ ou CPF inválido."
        elif digitos and AgroFornecedor.objects.filter(documento=digitos).exclude(pk=alvo.pk).exists():
            erros["documento"] = "Já existe um fornecedor com este documento."
        else:
            valores["documento"] = digitos
    for campo, maximo, rotulo in (("contato", 120, "o contato"), ("telefone", 30, "o telefone")):
        if criando or campo in dados:
            tentar(campo, lambda c=campo, m=maximo, r=rotulo: base.texto(dados.get(c), m, c, r))
    if criando or "email" in dados:
        email = base.texto(dados.get("email"), 254, "email", "o e-mail")
        try:
            if email:
                validate_email(email)
            valores["email"] = email
        except ValidationError:
            erros["email"] = "Informe um e-mail válido."
    if criando or "observacoes" in dados:
        tentar("observacoes", lambda: base.texto_livre(dados.get("observacoes"), "observacoes", "as observações"))
    _ativo(dados, criando, valores)
    if erros:
        raise ValidationError(erros)
    return _persistir(alvo, valores, Alvo.FORNECEDOR, usuario, {"documento": "Já existe um fornecedor com este documento."}, criando=criando)
