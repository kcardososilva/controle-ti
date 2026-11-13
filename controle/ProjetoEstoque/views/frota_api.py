"""
POSTs e JSON de Frotas e Conectividade: status do veículo, as AÇÕES de movimentação do chip
(vincular, desvincular, transferir, substituir, devolver, inativar) e as opções do Select2.

Todas as rotas passam por `exige_frota("<codename>")` (login + permissão; sem permissão, 403) e os
POSTs por `require_POST` — o CSRF é do middleware do Django (o formulário leva `{% csrf_token %}` e o
`frota.js` manda também `X-CSRFToken`). Nenhuma regra de negócio mora aqui: cada função valida a
entrada, chama o service e responde.

    frota_veiculo_status   editar_frota
    frota_vincular         vincular_chips
    frota_desvincular      transferir_chips   (o `vinculo_id` da URL é o vínculo que a tela MOSTRAVA)
    frota_transferir       transferir_chips   (idem)
    frota_substituir       transferir_chips   (idem)
    frota_retornar         transferir_chips
    frota_inativar         transferir_chips
    frota_opcoes           vincular_chips | transferir_chips | editar_frota (por `campo`)
    frota_importacao_*     importar_chips   (confirmar, cancelar, relatório em Excel e modelo da planilha)
    frota_config_*_salvar  administrar_frota_config (tipos, operadoras e regras de alerta)
    frota_tipo_rapido      administrar_frota_config (cria um tipo pelo cadastro do veículo, em JSON)

As ações de movimentação recebem o id do vínculo ESPERADO, não o do chip: se a tela abriu com o vínculo
X e outra pessoa já o encerrou, o service recusa ("já desvinculado por fulano em 12/11 14:05") em vez de
agir sobre o vínculo novo. A recusa chega ao usuário DENTRO do diálogo.

Dois modos de resposta, pelo cabeçalho `X-Requested-With`:
  • fetch do `frota.js` -> JSON `{"ok", "mensagem", "avisos", "redirect"}` ou, em erro, `{"ok": false,
    "mensagem", "erros": {campo: texto}}` com 400;
  • formulário comum -> mensagem do Django e redirect (`voltar` só para caminho do módulo).
"""

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from ProjetoEstoque.models import (
    REGRAS_DE_ALERTA_PADRAO,
    ConectividadeOperadora,
    FrotaImportacaoChips,
    FrotaTipo,
    FrotaVeiculo,
    M2MChip,
)
from services import frota_import_service as imp
from services import frota_service as frota
from services import frota_vinculo_service as vinculos
from services import m2m_monitoramento_service as m2m

from .frota_comum import erros_de, exige_frota, mensagem_de, negar_sem, voltar_para


# ═════════════════════════════════════════════════════════════
# Apoio
# ═════════════════════════════════════════════════════════════

def _eh_ajax(request) -> bool:
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


def _achar(modelo, valor, campo: str, rotulo: str):
    """O objeto do id que veio do formulário, ou `ValidationError({campo: ...})`."""
    try:
        return modelo.objects.get(pk=int(valor))
    except (TypeError, ValueError, modelo.DoesNotExist):
        raise ValidationError({campo: f"Escolha {rotulo}."}) from None


def _executar(request, acao, sucesso, padrao: str):
    """Roda `acao()` e responde nos dois modos. `acao` devolve o que o service devolveu (os `avisos`,
    se houver, vão junto da confirmação); `sucesso` é o texto ou uma função do resultado."""
    try:
        resultado = acao()
    except ValidationError as exc:
        erros = erros_de(exc)
        mensagem = " ".join(erros.values())
        if _eh_ajax(request):
            return JsonResponse({"ok": False, "mensagem": mensagem, "erros": erros}, status=400)
        messages.error(request, mensagem)
        return redirect(voltar_para(request, padrao))
    texto = sucesso(resultado) if callable(sucesso) else sucesso
    avisos = list(getattr(resultado, "avisos", None) or [])
    destino = voltar_para(request, padrao)
    if _eh_ajax(request):
        return JsonResponse({"ok": True, "mensagem": texto, "avisos": avisos, "redirect": destino})
    messages.success(request, texto)
    for aviso in avisos:
        messages.warning(request, aviso)
    return redirect(destino)


def _opcao_de_destino(request, valor_post: str) -> str:
    return (request.POST.get(valor_post) or "").strip()


# ═════════════════════════════════════════════════════════════
# Veículo: status
# ═════════════════════════════════════════════════════════════

@exige_frota("editar_frota")
@require_POST
def frota_veiculo_status(request, pk):
    """Muda o status OPERACIONAL do veículo (`status`, `motivo` opcional). Não toca em chip: o service
    recusa só desativar com chip vinculado (desvincule ou transfira antes)."""
    veiculo = get_object_or_404(FrotaVeiculo, pk=pk)
    padrao = reverse("frota_veiculo_detalhe", args=[veiculo.pk])
    novo, motivo = _opcao_de_destino(request, "status"), request.POST.get("motivo", "")
    return _executar(
        request,
        lambda: frota.mudar_status(veiculo, novo, request.user, motivo),
        lambda v: f"Status de {veiculo.codigo} alterado para {veiculo.get_status_display().lower()}.",
        padrao,
    )


# ═════════════════════════════════════════════════════════════
# Movimentações do chip
# ═════════════════════════════════════════════════════════════

@exige_frota("vincular_chips")
@require_POST
def frota_vincular(request):
    """Vincula um chip DISPONÍVEL a um veículo (`chip`, `veiculo`, `motivo`). Vale para os dois pontos
    de partida: o botão do veículo (escolhe o chip) e o do chip (escolhe o veículo)."""
    def agir():
        chip = _achar(M2MChip, request.POST.get("chip"), "chip", "um chip")
        veiculo = _achar(FrotaVeiculo, request.POST.get("veiculo"), "veiculo", "um veículo")
        resultado = vinculos.vincular(chip, veiculo, request.user, request.POST.get("motivo", ""))
        resultado.chip_nome, resultado.veiculo_codigo = chip.nome, veiculo.codigo
        return resultado

    padrao = reverse("frota_veiculos")
    if str(request.POST.get("veiculo") or "").isdigit():
        padrao = reverse("frota_veiculo_detalhe", args=[int(request.POST["veiculo"])])
    return _executar(
        request, agir, lambda r: f"Chip {r.chip_nome} vinculado ao veículo {r.veiculo_codigo}.", padrao,
    )


@exige_frota("transferir_chips")
@require_POST
def frota_desvincular(request, vinculo_id):
    """Encerra o vínculo `vinculo_id` (o que a tela mostrava). `destino`: `disponivel` | `manutencao`."""
    destino = _opcao_de_destino(request, "destino") or "disponivel"
    return _executar(
        request,
        lambda: vinculos.desvincular(vinculo_id, request.user, request.POST.get("motivo", ""), destino),
        lambda r: f"Chip {r.vinculo_encerrado.chip.nome} desvinculado do veículo {r.vinculo_encerrado.veiculo.codigo}.",
        reverse("frota_veiculos"),
    )


@exige_frota("transferir_chips")
@require_POST
def frota_transferir(request, vinculo_id):
    """Passa o chip do vínculo `vinculo_id` para outro veículo (`veiculo_destino`, `motivo`)."""
    def agir():
        destino = _achar(FrotaVeiculo, request.POST.get("veiculo_destino"), "veiculo_destino", "o veículo de destino")
        return vinculos.transferir(vinculo_id, destino, request.user, request.POST.get("motivo", ""))

    return _executar(
        request, agir,
        lambda r: (
            f"Chip {r.vinculo.chip.nome} transferido de {r.vinculo_encerrado.veiculo.codigo} "
            f"para {r.vinculo.veiculo.codigo}."
        ),
        reverse("frota_veiculos"),
    )


@exige_frota("transferir_chips")
@require_POST
def frota_substituir(request, vinculo_id):
    """Troca o chip do vínculo `vinculo_id` por `chip_novo` no MESMO veículo. `destino_antigo`:
    `disponivel` | `manutencao` (para onde vai o chip que sai)."""
    def agir():
        novo = _achar(M2MChip, request.POST.get("chip_novo"), "chip_novo", "o chip novo")
        destino = _opcao_de_destino(request, "destino_antigo") or "disponivel"
        return vinculos.substituir(vinculo_id, novo, request.user, request.POST.get("motivo", ""), destino)

    return _executar(
        request, agir,
        lambda r: (
            f"Chip {r.vinculo_encerrado.chip.nome} substituído pelo chip {r.vinculo.chip.nome} "
            f"no veículo {r.vinculo.veiculo.codigo}."
        ),
        reverse("frota_veiculos"),
    )


@exige_frota("transferir_chips")
@require_POST
def frota_retornar(request, pk):
    """Devolve à disponibilidade um chip em manutenção ou inativo (`motivo`; `retomar_monitoramento=1`
    religa a consulta à API)."""
    def agir():
        chip = _achar(M2MChip, pk, "chip", "um chip")
        retomar = request.POST.get("retomar_monitoramento") == "1"
        resultado = vinculos.retornar_disponibilidade(chip, request.user, request.POST.get("motivo", ""), retomar)
        resultado.chip_nome = chip.nome
        return resultado

    return _executar(
        request, agir, lambda r: f"Chip {r.chip_nome} devolvido à disponibilidade.", reverse("m2m_chip_detalhe", args=[pk]),
    )


@exige_frota("transferir_chips")
@require_POST
def frota_inativar(request, pk):
    """Inativa o chip (`motivo`): encerra o vínculo, se houver, e desliga o monitoramento. Nunca apaga."""
    def agir():
        chip = _achar(M2MChip, pk, "chip", "um chip")
        resultado = vinculos.inativar(chip, request.user, request.POST.get("motivo", ""))
        resultado.chip_nome = chip.nome
        return resultado

    return _executar(
        request, agir, lambda r: f"Chip {r.chip_nome} inativado. O histórico foi mantido.", reverse("m2m_chip_detalhe", args=[pk]),
    )


# ═════════════════════════════════════════════════════════════
# Opções do Select2
# ═════════════════════════════════════════════════════════════

@exige_frota("vincular_chips", "transferir_chips", "editar_frota")
@require_GET
def frota_opcoes(request):
    """Opções do Select2 (`campo`, `q`): `{"results": [{"id", "text"}]}`.

        campo=chip         chips DISPONÍVEIS (vincular_chips ou transferir_chips); `q` opcional
        campo=veiculo      veículos que recebem chip (vincular_chips ou transferir_chips); `q` com 2+ letras
        campo=responsavel  colaboradores ativos (editar_frota); `q` com 2+ letras

    Cada `campo` confere a permissão dele: ter UMA das três não dá as outras."""
    campo = request.GET.get("campo")
    q = request.GET.get("q", "")
    if campo in ("chip", "veiculo"):
        if not (request.user.has_perm("ProjetoEstoque.vincular_chips") or request.user.has_perm("ProjetoEstoque.transferir_chips")):
            raise PermissionDenied
        if campo == "chip":
            resultados = [{"id": c["id"], "text": c["text"]} for c in vinculos.chips_disponiveis(q, limite=20)]
        else:
            resultados = m2m.buscar_veiculos(q)
    elif campo == "responsavel":
        negar_sem(request, "editar_frota")
        resultados = m2m.buscar_responsaveis(q)
    else:
        return JsonResponse({"results": []}, status=400)
    return JsonResponse({"results": resultados})


# ═════════════════════════════════════════════════════════════
# Importação de chips: confirmar, cancelar e baixar (modelo e relatório)
# ═════════════════════════════════════════════════════════════

TIPO_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@exige_frota("importar_chips")
@require_POST
def frota_importacao_confirmar(request, pk):
    """Grava os chips VÁLIDOS da prévia. O service revalida cada linha (outra pessoa pode ter cadastrado
    o chip no meio-tempo), grava cada uma por conta e NUNCA atualiza chip que já existe. Prévia vencida
    (24 h) ou já confirmada é recusada com o motivo."""
    importacao = get_object_or_404(FrotaImportacaoChips, pk=pk)
    try:
        concluida = imp.confirmar(importacao, request.user)
    except ValidationError as exc:
        messages.error(request, mensagem_de(exc))
    else:
        messages.success(
            request,
            f"Importação concluída: {concluida.importados} chip{'s' if concluida.importados != 1 else ''} importado"
            f"{'s' if concluida.importados != 1 else ''}, {concluida.existentes} já cadastrado"
            f"{'s' if concluida.existentes != 1 else ''} e {concluida.rejeitados} rejeitado"
            f"{'s' if concluida.rejeitados != 1 else ''}.",
        )
    return redirect("frota_importacao", pk=importacao.pk)


@exige_frota("importar_chips")
@require_POST
def frota_importacao_cancelar(request, pk):
    """Descarta a prévia. As linhas ficam como registro; nenhum chip foi gravado."""
    importacao = get_object_or_404(FrotaImportacaoChips, pk=pk)
    try:
        imp.cancelar(importacao, request.user)
    except ValidationError as exc:
        messages.error(request, mensagem_de(exc))
        return redirect("frota_importacao", pk=importacao.pk)
    messages.success(request, "Prévia descartada. Nenhum chip foi importado.")
    return redirect("frota_importar")


@exige_frota("importar_chips")
@require_GET
def frota_importacao_relatorio(request, pk):
    """O relatório auditado em Excel (resumo e linha a linha). Texto que começa com `=`, `+`, `-` ou `@`
    sai como TEXTO: abrir o arquivo não executa fórmula nenhuma."""
    importacao = get_object_or_404(FrotaImportacaoChips, pk=pk)
    resposta = HttpResponse(imp.exportar_relatorio_xlsx(importacao), content_type=TIPO_XLSX)
    resposta["Content-Disposition"] = f'attachment; filename="relatorio_importacao_chips_{importacao.pk}.xlsx"'
    return resposta


@exige_frota("importar_chips")
@require_GET
def frota_modelo_planilha(request):
    """O arquivo-modelo (`?formato=xlsx|csv`; o padrão é xlsx)."""
    if request.GET.get("formato") == "csv":
        resposta = HttpResponse(imp.modelo_csv(), content_type="text/csv; charset=utf-8")
        resposta["Content-Disposition"] = 'attachment; filename="modelo_importacao_chips.csv"'
    else:
        resposta = HttpResponse(imp.modelo_xlsx(), content_type=TIPO_XLSX)
        resposta["Content-Disposition"] = 'attachment; filename="modelo_importacao_chips.xlsx"'
    return resposta


# ═════════════════════════════════════════════════════════════
# Configuração: tipos, operadoras e regras (administrar_frota_config)
# ═════════════════════════════════════════════════════════════

def _marcado(request, nome: str) -> bool:
    """Checkbox de formulário: só marcado ele vem no POST."""
    return request.POST.get(nome) == "1"


def _salvar_e_voltar(request, acao, sucesso: str, destino: str):
    """Roda o service da configuração; erro de validação vira mensagem (com o campo) e volta à tela."""
    try:
        acao()
    except ValidationError as exc:
        erros = erros_de(exc)
        messages.error(request, " ".join(erros.values()))
    else:
        messages.success(request, sucesso)
    return redirect(destino)


@exige_frota("administrar_frota_config")
@require_POST
def frota_config_tipo_salvar(request):
    """Cria (sem `id`) ou edita um tipo de veículo: `nome`, `exige_conectividade`, `ativo`."""
    pk = request.POST.get("id", "")
    tipo = get_object_or_404(FrotaTipo, pk=int(pk)) if pk.isdigit() else None
    dados = {"nome": request.POST.get("nome", ""), "exige_conectividade": _marcado(request, "exige_conectividade")}
    if tipo is not None:
        dados["ativo"] = _marcado(request, "ativo")
    return _salvar_e_voltar(
        request, lambda: frota.salvar_tipo(dados, request.user, tipo),
        "Tipo de veículo salvo." if tipo else "Tipo de veículo criado.", reverse("frota_config_tipos"),
    )


@exige_frota("administrar_frota_config")
@require_POST
def frota_tipo_rapido(request):
    """Cria um tipo de veículo SEM sair do cadastro do veículo (o botão "Novo tipo" do formulário).
    Mesma permissão e mesmo service da tela de Configuração (`frota_config_tipo_salvar`): aqui só muda
    a resposta, que é JSON — `{"ok": true, "id", "nome", "exige_conectividade", "mensagem"}` ou, em erro,
    `{"ok": false, "mensagem", "erros": {campo: texto}}` com 400 — para o formulário aberto ganhar a
    opção nova sem recarregar e sem perder o que já foi digitado."""
    dados = {"nome": request.POST.get("nome", ""), "exige_conectividade": _marcado(request, "exige_conectividade")}
    try:
        tipo = frota.salvar_tipo(dados, request.user)
    except ValidationError as exc:
        erros = erros_de(exc)
        return JsonResponse({"ok": False, "mensagem": " ".join(erros.values()), "erros": erros}, status=400)
    return JsonResponse({
        "ok": True, "id": tipo.pk, "nome": tipo.nome, "exige_conectividade": tipo.exige_conectividade,
        "mensagem": f"Tipo “{tipo.nome}” criado e selecionado.",
    })


@exige_frota("administrar_frota_config")
@require_POST
def frota_config_operadora_salvar(request):
    """Cria (sem `id`) ou edita uma operadora: `nome`, `integracao` (vazio = sem integração), `ativo`."""
    pk = request.POST.get("id", "")
    operadora = get_object_or_404(ConectividadeOperadora, pk=int(pk)) if pk.isdigit() else None
    dados = {"nome": request.POST.get("nome", ""), "integracao": request.POST.get("integracao", "")}
    if operadora is not None:
        dados["ativo"] = _marcado(request, "ativo")
    return _salvar_e_voltar(
        request, lambda: frota.salvar_operadora(dados, request.user, operadora),
        "Operadora salva." if operadora else "Operadora criada.", reverse("frota_config_operadoras"),
    )


@exige_frota("administrar_frota_config")
@require_POST
def frota_config_regra_salvar(request, tipo):
    """Grava a regra do tipo de ocorrência `tipo`: `gera_ocorrencia`, `nivel`, `envia_email`,
    `destinatarios_emails` (texto) e `destinatarios_usuarios` (ids)."""
    dados = {
        "gera_ocorrencia": _marcado(request, "gera_ocorrencia"),
        "nivel": request.POST.get("nivel", ""),
        "envia_email": _marcado(request, "envia_email"),
        "destinatarios_emails": request.POST.get("destinatarios_emails", ""),
        "destinatarios_usuarios": request.POST.getlist("destinatarios_usuarios"),
    }
    return _salvar_e_voltar(
        request, lambda: frota.salvar_regra_alerta(tipo, dados, request.user),
        "Regra de alerta salva.", reverse("frota_config_regras") + f"#regra-{tipo}",
    )


@exige_frota("administrar_frota_config")
@require_POST
def frota_config_regra_restaurar(request, tipo):
    """Devolve a regra aos valores de fábrica, sem destinatários."""
    return _salvar_e_voltar(
        request, lambda: frota.restaurar_regra_padrao(tipo, request.user) if tipo in REGRAS_DE_ALERTA_PADRAO else _tipo_desconhecido(),
        "Regra restaurada ao padrão.", reverse("frota_config_regras") + f"#regra-{tipo}",
    )


def _tipo_desconhecido():
    raise ValidationError({"tipo": "Tipo de ocorrência desconhecido."})
