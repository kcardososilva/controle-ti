"""
Telas das OPERAÇÕES de instalação do Zelo Agro: instalar, retirar (ou desvincular), transferir e
substituir um equipamento, e aplicar ou devolver material por quantidade. Toda a regra mora em
`services/tec_instalacao_service.py`; aqui só se descreve o formulário (lista de `campo(...)`), se
chama o service e se manda a pessoa de volta de onde veio.

São formulários de PÁGINA INTEIRA, como os do estoque (`tec_comum.formulario`): token de envio único
contra o duplo clique, erro por campo, 400 com o que foi digitado preservado. A instalação a retirar,
transferir ou substituir vem NA URL (`/instalacoes/<id>/...`): a tela abriu com aquela instalação, e o
service recusa se outra pessoa a encerrou no meio (mostrando por quem e quando).

    instalar_tec_equipamento     instalar, retirar/desvincular, substituir, aplicar e devolver material
    transferir_tec_equipamento   transferir um equipamento instalado para outro destino

O `?voltar=` leva de volta ao detalhe da máquina de onde a operação saiu (`tec_comum.voltar_seguro`).
"""

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse

from ProjetoEstoque.models import FrotaVeiculo, TecControle, TecEquipamento, TecInstalacao
from ProjetoEstoque.views.tec_comum import campo, exige_tec, formulario, voltar_seguro
from ProjetoEstoque.views.tec_estoque import _campos_de_modelo, _locais_com_saldo, _locais_das_fazendas, _material_ou_404, _modelos_agrupados
from services import tec_base as base
from services import tec_estoque_service as estoque
from services import tec_instalacao_service as ins

Situacao = TecEquipamento.Situacao
TEXTO_DO_DESTINO = {"disponivel": "Volta ao estoque", "manutencao": "Vai para a manutenção"}


# ═════════════════════════════════════════════════════════════
# Apoio
# ═════════════════════════════════════════════════════════════

def _equipamentos_disponiveis_agrupados(excluir_pk=None) -> list:
    """`[("Fabricante · Modelo", [(pk, "EQ-00012 · S/N X — Almoxarifado")])]` dos equipamentos DISPONÍVEIS."""
    grupos: dict = {}
    itens = [e for e in estoque.listar_equipamentos({"situacao": Situacao.DISPONIVEL}) if e.pk != excluir_pk]
    for e in sorted(itens, key=lambda e: (e.modelo.fabricante.nome.lower(), e.modelo.nome.lower(), e.codigo or "")):
        ident = f"S/N {e.numero_serie}" if e.numero_serie else (f"Pat. {e.patrimonio}" if e.patrimonio else "sem identificação")
        local = f" — {e.local.nome}" if e.local_id else ""
        grupos.setdefault(f"{e.modelo.fabricante.nome} · {e.modelo.nome}", []).append((e.pk, f"{e.codigo} · {ident}{local}"))
    return list(grupos.items())


def _volta(request, padrao_url: str, padrao_rotulo: str) -> tuple[str, str]:
    """Para onde o Cancelar leva: o `?voltar=` do módulo (a máquina de onde saiu), senão o padrão."""
    alvo = voltar_seguro(request, padrao_url)
    return alvo, ("Máquina" if alvo.startswith("/agro/frotas/") else padrao_rotulo)


def _avisos(request, avisos) -> None:
    for aviso in avisos or ():
        messages.warning(request, aviso)


def _recusar_na_tela(request, texto: str, destino: str):
    """Quando a página nem deveria abrir (o equipamento mudou de situação desde a tela anterior), em vez de
    mostrar um formulário que o service recusaria: volta com o motivo."""
    messages.warning(request, texto)
    return redirect(destino)


def _instalacao_ou_404(pk) -> TecInstalacao:
    return get_object_or_404(
        TecInstalacao.objects.select_related("equipamento__modelo__fabricante", "equipamento__modelo__categoria", "veiculo", "operador", "fim_por", "mov_fim"),
        pk=pk,
    )


def _destino_atual(instalacao: TecInstalacao) -> tuple:
    return (instalacao.destino_tipo, instalacao.destino_id)


def _url_do_equipamento(equipamento) -> str:
    return reverse("tec_equipamento_detalhe", args=[equipamento.pk])


# ═════════════════════════════════════════════════════════════
# Instalar
# ═════════════════════════════════════════════════════════════

@exige_tec("instalar_tec_equipamento")
def tec_equipamento_instalar(request, pk):
    """Instala UM equipamento disponível numa máquina ou num operador: a pessoa parte do equipamento e
    escolhe o destino."""
    equipamento = get_object_or_404(TecEquipamento.objects.select_related("modelo__fabricante", "local__fazenda"), pk=pk)
    padrao = _url_do_equipamento(equipamento)
    if request.method == "GET" and equipamento.situacao != Situacao.DISPONIVEL:
        return _recusar_na_tela(request, f"{equipamento.codigo} está {equipamento.get_situacao_display().lower()}: só equipamento disponível se instala.", padrao)

    def executar(dados, token):
        tipo, ident = ins.separar_destino(dados.get("destino"))
        resultado = ins.instalar(equipamento, tipo, ident, request.user, dados.get("motivo"), observacoes=dados.get("observacoes", ""), token=token)
        _avisos(request, resultado.avisos)
        onde = resultado.instalacao.destino_texto if resultado.instalacao else "o destino escolhido"
        return f"{equipamento.codigo} instalado em {onde}.", padrao, resultado.repetida

    voltar_url, voltar_rotulo = _volta(request, padrao, equipamento.codigo)
    return formulario(
        request, aba="equipamentos", titulo=f"Instalar {equipamento.codigo}", rotulo_enviar="Instalar",
        descricao=f"{equipamento.modelo} · disponível em {equipamento.local}. Sai do estoque e passa a constar no destino, com o histórico guardado.",
        voltar_url=voltar_url, voltar_rotulo=voltar_rotulo, executar=executar,
        campos=[
            campo("destino", "Instalar em", "select", obrigatorio=True, grupos=ins.opcoes_de_destino(), vazio="Escolher a máquina ou o operador",
                  valor=request.GET.get("destino", ""), largo=True, dica="Máquina desativada e colaborador desligado não aparecem."),
            campo("motivo", "Motivo", obrigatorio=True, maximo=255, largo=True, placeholder="Ex.: monitor da cabine, reposição de receptor"),
            campo("observacoes", "Observações", "textarea", largo=True),
        ],
    )


@exige_tec("instalar_tec_equipamento")
def tec_veiculo_instalar(request, pk):
    """Instala um equipamento disponível NESTA máquina: a pessoa parte da máquina (o botão da Composição
    tecnológica) e escolhe o equipamento do estoque."""
    veiculo = get_object_or_404(FrotaVeiculo, pk=pk)
    padrao = f"{reverse('frota_veiculo_detalhe', args=[veiculo.pk])}#m2m-v-d-comp"
    grupos = _equipamentos_disponiveis_agrupados()

    def executar(dados, token):
        equipamento = base.entidade(dados.get("equipamento"), TecEquipamento, "equipamento", "o equipamento")
        resultado = ins.instalar(equipamento, "veiculo", veiculo, request.user, dados.get("motivo"), observacoes=dados.get("observacoes", ""), token=token)
        _avisos(request, resultado.avisos)
        return f"{equipamento.codigo} instalado em {veiculo.codigo} · {veiculo.nome}.", padrao, resultado.repetida

    voltar_url, voltar_rotulo = _volta(request, padrao, f"{veiculo.codigo}")
    return formulario(
        request, aba="equipamentos", titulo=f"Instalar em {veiculo.codigo}", rotulo_enviar="Instalar",
        descricao=f"{veiculo.nome} · escolha um equipamento disponível no estoque tecnológico.",
        voltar_url=voltar_url, voltar_rotulo=voltar_rotulo, executar=executar,
        aviso="" if grupos else "Não há equipamento disponível no estoque. Cadastre o equipamento em Estoque tecnológico › Equipamentos.",
        campos=[
            campo("equipamento", "Equipamento", "select", obrigatorio=True, grupos=grupos, vazio="Escolher o equipamento", valor=request.GET.get("equipamento", ""), largo=True),
            campo("motivo", "Motivo", obrigatorio=True, maximo=255, largo=True, placeholder="Ex.: monitor da cabine, reposição de receptor"),
            campo("observacoes", "Observações", "textarea", largo=True),
        ],
    )


# ═════════════════════════════════════════════════════════════
# Retirar / desvincular, transferir, substituir
# ═════════════════════════════════════════════════════════════

@exige_tec("instalar_tec_equipamento")
def tec_instalacao_retirar(request, pk):
    """Tira o equipamento do destino e o devolve ao estoque (retirada de uma máquina; desvinculação de um
    operador). O local de guarda é obrigatório: "disponível" é um lugar."""
    instalacao = _instalacao_ou_404(pk)
    equipamento = instalacao.equipamento
    padrao = _url_do_equipamento(equipamento)
    if request.method == "GET" and instalacao.fim_em is not None:
        return _recusar_na_tela(request, ins.mensagem_ja_encerrada(instalacao), padrao)
    desvincular = bool(instalacao.operador_id)
    verbo = "Desvincular" if desvincular else "Retirar"

    def executar(dados, token):
        resultado = ins.retirar(instalacao.pk, dados.get("local"), request.user, dados.get("motivo"), observacoes=dados.get("observacoes", ""), token=token)
        onde = resultado.equipamento.local if resultado.equipamento and resultado.equipamento.local_id else "o estoque"
        return f"{equipamento.codigo} {'desvinculado de' if desvincular else 'retirado de'} {instalacao.destino_texto}; agora está em {onde}.", padrao, resultado.repetida

    voltar_url, voltar_rotulo = _volta(request, padrao, equipamento.codigo)
    return formulario(
        request, aba="equipamentos", titulo=f"{verbo} {equipamento.codigo}", rotulo_enviar=f"{verbo} equipamento",
        descricao=f"{equipamento.modelo} · instalado em {instalacao.destino_texto} desde {instalacao.inicio_em:%d/%m/%Y}.",
        voltar_url=voltar_url, voltar_rotulo=voltar_rotulo, executar=executar,
        aviso="O equipamento volta ao estoque no local escolhido e fica disponível. O período em que esteve aqui continua no histórico.",
        campos=[
            campo("local", "Guardar em", "select", obrigatorio=True, opcoes=_locais_das_fazendas(), vazio="Escolher o local de armazenamento"),
            campo("motivo", "Motivo", obrigatorio=True, maximo=255, largo=True, placeholder="Ex.: defeito, fim da safra, troca de máquina"),
            campo("observacoes", "Observações", "textarea", largo=True),
        ],
    )


@exige_tec("transferir_tec_equipamento")
def tec_instalacao_transferir(request, pk):
    """Passa o equipamento instalado para OUTRO destino, sem passar pelo estoque: encerra o período e abre o
    novo, e os dois ficam no histórico."""
    instalacao = _instalacao_ou_404(pk)
    equipamento = instalacao.equipamento
    padrao = _url_do_equipamento(equipamento)
    if request.method == "GET" and instalacao.fim_em is not None:
        return _recusar_na_tela(request, ins.mensagem_ja_encerrada(instalacao), padrao)

    def executar(dados, token):
        tipo, ident = ins.separar_destino(dados.get("destino"))
        resultado = ins.transferir(instalacao.pk, tipo, ident, request.user, dados.get("motivo"), observacoes=dados.get("observacoes", ""), token=token)
        _avisos(request, resultado.avisos)
        onde = resultado.instalacao.destino_texto if resultado.instalacao else "o novo destino"
        return f"{equipamento.codigo} transferido de {instalacao.destino_texto} para {onde}.", padrao, resultado.repetida

    voltar_url, voltar_rotulo = _volta(request, padrao, equipamento.codigo)
    return formulario(
        request, aba="equipamentos", titulo=f"Transferir {equipamento.codigo}", rotulo_enviar="Transferir",
        descricao=f"{equipamento.modelo} · hoje em {instalacao.destino_texto}. Não passa pelo estoque; o vínculo anterior fica no histórico.",
        voltar_url=voltar_url, voltar_rotulo=voltar_rotulo, executar=executar,
        campos=[
            campo("destino", "Transferir para", "select", obrigatorio=True, grupos=ins.opcoes_de_destino(excluir=_destino_atual(instalacao)),
                  vazio="Escolher a máquina ou o operador", valor=request.GET.get("destino", ""), largo=True),
            campo("motivo", "Motivo", obrigatorio=True, maximo=255, largo=True, placeholder="Ex.: máquina parada, troca de turno"),
            campo("observacoes", "Observações", "textarea", largo=True),
        ],
    )


@exige_tec("instalar_tec_equipamento")
def tec_instalacao_substituir(request, pk):
    """Troca o equipamento instalado por outro disponível, NO MESMO destino: o que sai volta ao estoque (ou
    vai para a manutenção) e o que entra assume o lugar. Os dois ficam no histórico."""
    instalacao = _instalacao_ou_404(pk)
    equipamento = instalacao.equipamento
    padrao = _url_do_equipamento(equipamento)
    if request.method == "GET" and instalacao.fim_em is not None:
        return _recusar_na_tela(request, ins.mensagem_ja_encerrada(instalacao), padrao)
    grupos = _equipamentos_disponiveis_agrupados(excluir_pk=equipamento.pk)

    def executar(dados, token):
        novo = base.entidade(dados.get("equipamento_novo"), TecEquipamento, "equipamento_novo", "o equipamento que entra")
        resultado = ins.substituir(
            instalacao.pk, novo, request.user, dados.get("motivo"), destino_do_antigo=dados.get("destino_do_antigo") or "disponivel",
            local=dados.get("local") or None, observacoes=dados.get("observacoes", ""), token=token,
        )
        return f"{novo.codigo} entrou no lugar de {equipamento.codigo} em {instalacao.destino_texto}.", padrao, resultado.repetida

    voltar_url, voltar_rotulo = _volta(request, padrao, equipamento.codigo)
    return formulario(
        request, aba="equipamentos", titulo=f"Substituir {equipamento.codigo}", rotulo_enviar="Substituir",
        descricao=f"{equipamento.modelo} · instalado em {instalacao.destino_texto}. Escolha o que entra e o que acontece com o que sai.",
        voltar_url=voltar_url, voltar_rotulo=voltar_rotulo, executar=executar,
        aviso="" if grupos else "Não há outro equipamento disponível no estoque para entrar no lugar. Cadastre-o em Estoque tecnológico › Equipamentos.",
        campos=[
            campo("equipamento_novo", "Equipamento que entra", "select", obrigatorio=True, grupos=grupos, vazio="Escolher o equipamento", valor=request.GET.get("equipamento", ""), largo=True),
            campo("destino_do_antigo", "O que sai", "select", obrigatorio=True, opcoes=list(TEXTO_DO_DESTINO.items()), valor="disponivel"),
            campo("local", "Guardar o que sai em", "select", opcoes=_locais_das_fazendas(), vazio="Escolher o local", dica="Obrigatório quando o equipamento volta ao estoque."),
            campo("motivo", "Motivo da troca", obrigatorio=True, maximo=255, largo=True, placeholder="Ex.: tela rachada, defeito no receptor"),
            campo("observacoes", "Observações", "textarea", largo=True),
        ],
    )


# ═════════════════════════════════════════════════════════════
# Material por quantidade: aplicar numa máquina ou operador, e devolver
# ═════════════════════════════════════════════════════════════

@exige_tec("instalar_tec_equipamento")
def tec_material_aplicar(request, pk=None):
    """Aplica material por quantidade (cabo, conector, fonte…) numa máquina ou operador: sai do estoque do
    local e o histórico guarda o destino. Sem `pk`, a pessoa parte da máquina e escolhe também o material."""
    fixo = _material_ou_404(pk) if pk else None
    padrao = reverse("tec_material_detalhe", args=[fixo.pk]) if fixo else reverse("tec_materiais")

    def executar(dados, token):
        modelo = fixo or dados.get("modelo")
        tipo, ident = ins.separar_destino(dados.get("destino"))
        resultado = ins.aplicar_material(
            modelo, dados.get("local"), dados.get("quantidade"), tipo, ident, request.user, dados.get("motivo"),
            observacoes=dados.get("observacoes", ""), token=token,
        )
        _avisos(request, resultado.avisos)
        mov = resultado.movimentacoes[0]
        return f"{mov.quantidade.normalize():f} {mov.modelo.unidade.sigla} de {mov.modelo} aplicados em {mov.destino_rotulo}.", padrao, resultado.repetida

    campos = [
        campo("local", "Sai de qual local", "select", obrigatorio=True, opcoes=_locais_com_saldo(fixo, so_com_saldo=True) if fixo else _locais_das_fazendas(), vazio="Escolher o local"),
        campo("quantidade", "Quantidade", "number", obrigatorio=True, mono=True, placeholder="0", dica=f"Em {fixo.unidade.nome.lower()}s." if fixo else ""),
        campo("destino", "Aplicar em", "select", obrigatorio=True, grupos=ins.opcoes_de_destino(), vazio="Escolher a máquina ou o operador", valor=request.GET.get("destino", ""), largo=True),
        campo("motivo", "Motivo", obrigatorio=True, maximo=255, largo=True, placeholder="Ex.: chicote da cabine, cabo do receptor"),
        campo("observacoes", "Observações", "textarea", largo=True),
    ]
    if fixo is None:
        for posicao, c in enumerate(_campos_de_modelo(TecControle.QUANTIDADE, "Material", vazio="Escolher o material", valor=request.GET.get("modelo", ""))):
            campos.insert(posicao, c)
    voltar_url, voltar_rotulo = _volta(request, padrao, str(fixo) if fixo else "Materiais")
    return formulario(
        request, aba="materiais", titulo="Aplicar material", rotulo_enviar="Aplicar material",
        descricao=(f"{fixo} · sai do estoque do local e passa a constar no destino." if fixo else "Sai do estoque do local e passa a constar na máquina ou no operador."),
        voltar_url=voltar_url, voltar_rotulo=voltar_rotulo, campos=campos, executar=executar,
    )


@exige_tec("instalar_tec_equipamento")
def tec_material_devolver(request, pk):
    """Tira material aplicado de uma máquina ou operador e o devolve ao estoque de um local. Só devolve o
    que está aplicado ali."""
    modelo = _material_ou_404(pk)
    padrao = reverse("tec_material_detalhe", args=[modelo.pk])
    aplicacoes = ins.aplicacoes_do_material(modelo)
    sigla = modelo.unidade.sigla if modelo.unidade_id else "un"

    def executar(dados, token):
        tipo, ident = ins.separar_destino(dados.get("destino"))
        resultado = ins.devolver_material(
            modelo, tipo, ident, dados.get("local"), dados.get("quantidade"), request.user, dados.get("motivo"),
            observacoes=dados.get("observacoes", ""), token=token,
        )
        mov = resultado.movimentacoes[0]
        return f"{mov.quantidade.normalize():f} {sigla} de {modelo} devolvidos de {mov.origem_rotulo} ao estoque.", padrao, resultado.repetida

    voltar_url, voltar_rotulo = _volta(request, padrao, str(modelo))
    return formulario(
        request, aba="materiais", titulo="Devolver material ao estoque", rotulo_enviar="Devolver",
        descricao=f"{modelo} · tira de uma máquina ou operador e soma ao saldo do local escolhido.",
        voltar_url=voltar_url, voltar_rotulo=voltar_rotulo, executar=executar,
        aviso="" if aplicacoes else f"{modelo} não está aplicado em nenhuma máquina ou operador.",
        campos=[
            campo("destino", "Devolver de", "select", obrigatorio=True, vazio="Escolher a máquina ou o operador", largo=True,
                  opcoes=[(f"{a['tipo']}:{a['id']}", f"{a['rotulo']} — {a['quantidade'].normalize():f} {sigla}") for a in aplicacoes], valor=request.GET.get("destino", "")),
            campo("quantidade", "Quantidade", "number", obrigatorio=True, mono=True, placeholder="0"),
            campo("local", "Guardar em", "select", obrigatorio=True, opcoes=_locais_das_fazendas(), vazio="Escolher o local de armazenamento"),
            campo("motivo", "Motivo", obrigatorio=True, maximo=255, largo=True, placeholder="Ex.: sobrou da instalação, aplicado por engano"),
            campo("observacoes", "Observações", "textarea", largo=True),
        ],
    )
