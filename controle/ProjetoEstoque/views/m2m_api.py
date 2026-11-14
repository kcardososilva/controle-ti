"""
POSTs e JSON do módulo Monitoramento TIM M2M.

Todas as rotas passam por `exige_frota("<codename>")` (login + permissão; sem permissão, 403) e os
POSTs por `require_POST` — o CSRF é do middleware do Django (o JS manda
`X-CSRFToken`). Nenhuma regra de negócio mora aqui: cada função valida a entrada,
chama o service e devolve o resultado.

Segredo: o token só passa por `m2m_config_token`, que o recebe, entrega ao service
e NUNCA o devolve (nem em mensagem de erro). `sensitive_post_parameters` o tira
também dos relatórios de erro do Django. O JSON de `m2m_consulta_payload` é o que
já foi guardado — saneado na gravação — e ainda passa por `tim.sanear` na saída.
"""

import json

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_POST

from ProjetoEstoque.models import M2MChip, M2MConsulta, M2MOcorrencia

from services import m2m_monitoramento_service as m2m
from services import tim_m2m_service as tim

from .m2m import (
    contexto_de_configuracao,
    erros_de,
    exige_api,
    exige_frota,
    mapeamento_do_post,
    voltar_para,
)


# ═════════════════════════════════════════════════════════════
# Apoio
# ═════════════════════════════════════════════════════════════

def _corpo_json(request):
    """O corpo da requisição como `dict`, ou `None` se não for um objeto JSON."""
    try:
        corpo = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return None
    return corpo if isinstance(corpo, dict) else None


def _json_invalido():
    return JsonResponse({"ok": False, "mensagem": "Requisição inválida."}, status=400)


def _sanear_estrutura(valor, segredo: str):
    """`tim.sanear` em cada TEXTO de uma estrutura JSON (listas, objetos e chaves
    incluídas), sem mexer no formato: sanear o JSON serializado engoliria as aspas e
    chaves que seguem um `Bearer ...` e deixaria o resultado ilegível."""
    if isinstance(valor, str):
        return tim.sanear(valor, segredo)
    if isinstance(valor, list):
        return [_sanear_estrutura(v, segredo) for v in valor]
    if isinstance(valor, dict):
        return {tim.sanear(str(k), segredo): _sanear_estrutura(v, segredo) for k, v in valor.items()}
    return valor


def _tom_da_atualizacao(resultado) -> str:
    """`ok` (tudo certo) | `aviso` (parte falhou ou o ciclo foi interrompido) | `erro` (nada deu certo)."""
    if resultado.ok and not resultado.interrompida_por:
        return "ok"
    return "aviso" if any(c.valida for c in resultado.consultas) else "erro"


# ═════════════════════════════════════════════════════════════
# Chip: atualizar, ativar/desativar
# ═════════════════════════════════════════════════════════════

@exige_frota("consultar_conectividade")
@require_POST
def m2m_chip_atualizar(request, pk):
    """Consulta a API agora (`{"tipos": ["sessao", "consumo"], "contexto": "linha"|"detalhe"}`).

    200 — a tentativa aconteceu (mesmo que a API tenha falhado: a falha é um
    resultado, gravado como consulta). `ok` diz se TODAS as consultas deram certo;
    `tom` guia o aviso na tela. Com `contexto: "linha"` vai junto o `html_linha`
    da linha atualizada da lista.
    409 — a consulta nem foi tentada (sem token, API bloqueada, chip desativado,
    atualização repetida em menos de 60 s): `codigo` e `mensagem` explicam.
    400 — corpo ou tipos inválidos."""
    chip = get_object_or_404(M2MChip, pk=pk)
    corpo = _corpo_json(request)
    if corpo is None:
        return _json_invalido()
    tipos = corpo.get("tipos") or list(m2m.TIPOS_DE_CONSULTA)
    if not isinstance(tipos, list) or not all(isinstance(t, str) and t in m2m.TIPOS_DE_CONSULTA for t in tipos):
        return _json_invalido()

    try:
        resultado = m2m.atualizar(chip, tipos, "manual", request.user)
    except m2m.AtualizacaoRecusada as exc:
        return JsonResponse({
            "ok": False, "recusada": True, "codigo": exc.codigo, "tom": "aviso",
            "mensagem": " ".join(exc.messages), "aguardar_s": exc.aguardar_s,
        }, status=409)

    chip = m2m.listar_chips().get(pk=chip.pk)
    resposta = {
        "ok": resultado.ok and not resultado.interrompida_por,
        "recusada": False,
        "tom": _tom_da_atualizacao(resultado),
        "mensagem": resultado.resumo(),
        "interrompida_por": resultado.interrompida_por,
        "consultas": [
            {
                "id": c.pk, "tipo": c.tipo, "resultado": c.resultado, "rotulo": c.get_resultado_display(),
                "http_status": c.http_status, "valida": c.valida,
            }
            for c in resultado.consultas
        ],
        "chip": {
            "id": chip.pk, "sessao_estado": chip.sessao_estado, "falhas_consecutivas": chip.falhas_consecutivas,
            "consumo_bytes": chip.consumo_bytes,
        },
    }
    if corpo.get("contexto") == "linha":
        resposta["html_linha"] = render_to_string("front/m2m/_chip_linha.html", {
            "chip": m2m.anotar_chips([chip])[0],
            "pode_atualizar": request.user.has_perm("ProjetoEstoque.consultar_conectividade"),
            "pode_ver_api": True,        # esta rota já exige `consultar_conectividade`: quem chega aqui vê a API
            "pode_ver_frota": request.user.has_perm("ProjetoEstoque.ver_frota"),
            "pode_vincular": request.user.has_perm("ProjetoEstoque.vincular_chips"),
            "consumo_base": m2m.base_de_consumo(m2m.configuracao_atual()["mapeamento"]),
        }, request=request)
    return JsonResponse(resposta)


@exige_frota("importar_chips")
@exige_api            # ligar/desligar a consulta à API é controle da API, não cadastro do chip
@require_POST
def m2m_chip_monitoramento(request, pk):
    """Desativa (`acao=desativar`, com `motivo` obrigatório) ou reativa
    (`acao=reativar`, motivo opcional) o monitoramento. O histórico não é apagado."""
    chip = get_object_or_404(M2MChip, pk=pk)
    acao = request.POST.get("acao")
    motivo = request.POST.get("motivo", "")
    try:
        if acao == "desativar":
            m2m.desativar(chip, request.user, motivo)
            messages.success(request, f"Monitoramento de {chip.nome} desativado. O histórico foi mantido.")
        elif acao == "reativar":
            m2m.reativar(chip, request.user, motivo)
            messages.success(request, f"Monitoramento de {chip.nome} reativado. Ele entra na próxima rodada.")
        else:
            messages.error(request, "Ação desconhecida.")
    except ValidationError as exc:
        messages.error(request, " ".join(erros_de(exc).values()))
    return redirect(voltar_para(request, reverse("m2m_chip_detalhe", args=[chip.pk])))


@exige_frota("consultar_conectividade")
@require_GET
def m2m_consulta_payload(request, pk):
    """A consulta guardada, com a resposta completa da API (`payload`) — só leitura,
    nada é consultado agora. O payload foi saneado ao ser gravado e passa de novo
    por `tim.sanear` na saída (defesa em profundidade)."""
    consulta = get_object_or_404(M2MConsulta.objects.select_related("chip"), pk=pk)
    segredo = ""
    try:
        segredo = tim.token()
    except Exception:  # noqa: BLE001 — sem token utilizável, só o `Bearer ...` é removido
        pass
    payload = _sanear_estrutura(consulta.payload, segredo)
    return JsonResponse({
        "id": consulta.pk,
        "chip": {"id": consulta.chip_id, "nome": consulta.chip.nome},
        "tipo": consulta.tipo, "tipo_rotulo": consulta.get_tipo_display(),
        "origem_rotulo": consulta.get_origem_display(),
        "iniciada_em": timezone.localtime(consulta.iniciada_em).strftime("%d/%m/%Y às %H:%M:%S"),
        "duracao_ms": consulta.duracao_ms, "tentativas": consulta.tentativas, "http_status": consulta.http_status,
        "resultado": consulta.resultado, "resultado_rotulo": consulta.get_resultado_display(), "valida": consulta.valida,
        "mensagem_erro": tim.sanear(consulta.mensagem_erro, segredo),
        "estado_bruto": consulta.estado_bruto, "estado_interpretado": consulta.estado_interpretado,
        "valor_original": consulta.valor_original, "unidade_original": consulta.unidade_original,
        "volume_bytes": consulta.volume_bytes,
        "payload": payload,
    })


@exige_frota("importar_chips")
@require_GET
def m2m_opcoes(request):
    """Opções do Select2 do cadastro (`campo=veiculo|responsavel`, `q` com 2+ letras):
    `{"results": [{"id", "text"}]}`. (O antigo `campo=item`, equipamento do estoque de TI, saiu.)"""
    campo = request.GET.get("campo")
    q = request.GET.get("q", "")
    if campo == "veiculo":
        resultados = m2m.buscar_veiculos(q)
    elif campo == "responsavel":
        resultados = m2m.buscar_responsaveis(q)
    else:
        return JsonResponse({"results": []}, status=400)
    return JsonResponse({"results": resultados})


# ═════════════════════════════════════════════════════════════
# Alertas
# ═════════════════════════════════════════════════════════════

@exige_frota("tratar_ocorrencias")
@require_POST
def m2m_alerta_tratar(request, pk):
    """Andamento de uma ocorrência: `acao` = assumir | resolver | descartar | comentar.
    Resolver e descartar exigem comentário (o service recusa sem ele)."""
    ocorrencia = get_object_or_404(M2MOcorrencia, pk=pk)
    acao = request.POST.get("acao", "")
    try:
        m2m.tratar_ocorrencia(ocorrencia, acao, request.user, request.POST.get("comentario", ""))
        messages.success(request, {
            "assumir": "Ocorrência assumida.", "resolver": "Ocorrência resolvida.",
            "descartar": "Ocorrência descartada.", "comentar": "Comentário registrado.",
        }.get(acao, "Feito."))
    except ValidationError as exc:
        messages.error(request, " ".join(erros_de(exc).values()))
    padrao = reverse("m2m_alertas") + f"?abrir={ocorrencia.pk}"
    return redirect(voltar_para(request, padrao))


# ═════════════════════════════════════════════════════════════
# Configuração (todas exigem `configurar`)
# ═════════════════════════════════════════════════════════════

def _renderizar_configuracao_com_erros(request, erros: dict, mapa_form: dict | None = None, valores: dict | None = None):
    """Re-renderiza a tela de configuração com a mensagem de cada campo recusado (400) e o
    que o usuário digitou. O erro aparece no próprio formulário: não vai também para `messages`."""
    return render(
        request, "front/m2m/config.html",
        contexto_de_configuracao(request, erros=erros, mapa_form=mapa_form, valores=valores), status=400,
    )


@exige_frota("administrar_frota_config")
@require_POST
@sensitive_post_parameters("token")
def m2m_config_token(request):
    """`acao=salvar` (campo `token`) cifra e grava o token; `acao=remover` apaga o do
    banco (o do `.env`, se houver, continua valendo). O token não volta para a tela."""
    acao = request.POST.get("acao")
    try:
        if acao == "salvar":
            tim.salvar_token(request.POST.get("token", ""), request.user)
            messages.success(request, "Token gravado (cifrado). Faça um teste de conexão para confirmar.")
        elif acao == "remover":
            tim.remover_token(request.user)
            messages.success(request, "Token removido do banco.")
        else:
            messages.error(request, "Ação desconhecida.")
    except ValidationError as exc:
        return _renderizar_configuracao_com_erros(request, {"token": " ".join(exc.messages)})
    return redirect("m2m_config")


@exige_frota("administrar_frota_config")
@require_POST
def m2m_config_frequencia(request):
    """Grava frequência e limites de alerta (qualquer subconjunto dos campos de
    `m2m.LIMITES_CONFIG`). Com `simular=1` NÃO grava: devolve em JSON a conta de
    requisições por minuto (`validar_frequencia`) para a tela mostrar ao vivo."""
    dados = {campo: request.POST[campo] for campo in m2m.LIMITES_CONFIG if campo in request.POST}
    if request.POST.get("simular"):
        try:
            intervalo = int(str(dados.get("intervalo_minutos", "")).strip())
            repouso = int(str(dados.get("intervalo_repouso_minutos", "")).strip()) if dados.get("intervalo_repouso_minutos") else None
            limite = int(str(dados.get("limite_req_minuto", "")).strip()) if dados.get("limite_req_minuto") else None
            if intervalo < 1 or (repouso is not None and repouso < 1) or (limite is not None and limite < 1):
                raise ValueError
        except ValueError:
            return JsonResponse({"ok": False, "motivo": "Informe números inteiros maiores que zero.", "conta": ""})
        plano = m2m.plano_atual(intervalo_ativo=intervalo, intervalo_repouso=repouso, limite=limite)
        return JsonResponse({
            "ok": True, "motivo": "", "conta": plano["texto"], "limitado": plano["limitado"],
        })
    try:
        m2m.salvar_configuracao(dados, request.user)
    except ValidationError as exc:
        return _renderizar_configuracao_com_erros(request, erros_de(exc), valores=dados)
    messages.success(request, "Configuração salva.")
    return redirect("m2m_config")


@exige_frota("ver_chips")
@exige_api
@require_GET
def m2m_sincronizacao(request):
    """O estado da sincronização automática, em JSON, para a faixa do topo se atualizar sozinha (o `m2m.js`
    pergunta a cada 30 s). SÓ LÊ do banco o que a tarefa do Agendador gravou: abrir ou consultar esta rota
    nunca dispara uma consulta à API. Não leva credencial, nem o token, nem a resposta da TIM."""
    agora = timezone.now()
    e = m2m.estado_sincronizacao(agora)

    def quando(momento):
        if not momento:
            return {"texto": "", "iso": "", "rotulo": ""}
        local = timezone.localtime(momento)
        return {
            "texto": local.strftime("%H:%M:%S") if local.date() == timezone.localdate() else local.strftime("%d/%m %H:%M"),
            "iso": momento.isoformat(), "rotulo": m2m.frescor(momento, 10 ** 6, agora)["rotulo"],
        }

    exec_ = e["execucao"]
    return JsonResponse({
        "estado": e["estado"], "rotulo": e["rotulo"], "tom": e["tom"], "motivo": e["motivo"], "ind": e["ind"],
        "ultima_sincronizacao": quando(e["ultima_sincronizacao"]),
        "ultima_tentativa": quando(e["ultima_tentativa"]),
        "ultima_resposta_valida": quando(e["ultima_resposta_valida"]),
        "proxima": quando(e["proxima"]),
        "vencidos": e["vencidos"],
        "ciclo": ({
            "iniciado": quando(exec_.iniciada_em), "estado": exec_.get_estado_display(), "consultados": exec_.consultados,
            "sucessos": exec_.sucessos, "falhas": exec_.falhas, "chamadas": exec_.chamadas,
            "pendentes": exec_.pendentes, "duracao_s": round(exec_.duracao_ms / 1000, 1), "mudancas": exec_.mudancas or {},
        } if exec_ else None),
        "plano": e["plano"]["texto"], "limitado": e["plano"]["limitado"],
        "tique_minutos": e["tique_minutos"],
    })


@exige_frota("administrar_frota_config")
@require_POST
def m2m_config_mapeamento(request):
    """Grava o mapeamento da resposta (campos `secao__campo`). Não reescreve as
    consultas antigas — para isso, `m2m_reinterpretar`."""
    mapa_form = mapeamento_do_post(request.POST)
    try:
        m2m.salvar_mapeamento(mapa_form, request.user)
    except ValidationError as exc:
        return _renderizar_configuracao_com_erros(request, {"mapeamento": " ".join(exc.messages)}, mapa_form)
    messages.success(
        request, "Mapeamento salvo. As consultas antigas só mudam se você usar \"Reinterpretar histórico\".",
    )
    return redirect(reverse("m2m_config") + "#mapeamento")


@exige_frota("administrar_frota_config")
@require_POST
def m2m_config_previa(request):
    """Prévia ao vivo (JSON): `{"mapeamento": {...}}` -> o que ele interpretaria nas
    últimas respostas reais. Nada é gravado."""
    corpo = _corpo_json(request)
    if corpo is None or not isinstance(corpo.get("mapeamento"), dict):
        return _json_invalido()
    return JsonResponse(m2m.previa_do_mapeamento(corpo["mapeamento"]))


@exige_frota("administrar_frota_config")
@require_POST
def m2m_config_testar(request):
    """Teste de conexão (JSON `{"chip_id": N}`): UMA consulta de sessão do chip
    escolhido. Devolve o resultado (200, mesmo quando a API falha) ou 409 se a
    consulta nem foi tentada."""
    corpo = _corpo_json(request)
    chip_id = corpo.get("chip_id") if corpo else None
    if not str(chip_id or "").isdigit():
        return JsonResponse({"ok": False, "tom": "aviso", "mensagem": "Escolha um chip para o teste."}, status=400)
    chip = get_object_or_404(M2MChip, pk=int(chip_id))
    try:
        consulta = m2m.testar_conexao(chip, request.user)
    except m2m.AtualizacaoRecusada as exc:
        return JsonResponse({
            "ok": False, "recusada": True, "codigo": exc.codigo, "tom": "aviso",
            "mensagem": " ".join(exc.messages), "aguardar_s": exc.aguardar_s,
        }, status=409)
    return JsonResponse({
        "ok": consulta.valida, "recusada": False, "tom": "ok" if consulta.valida else "erro",
        "resultado": consulta.resultado, "rotulo": consulta.get_resultado_display(),
        "http_status": consulta.http_status, "duracao_ms": consulta.duracao_ms,
        "mensagem": consulta.mensagem_erro or "A API respondeu.", "consulta_id": consulta.pk,
        "chip": chip.nome,
    })


@exige_frota("administrar_frota_config")
@require_POST
def m2m_reinterpretar(request):
    """"Reinterpretar histórico": recalcula os campos normalizados das consultas
    válidas com o mapeamento atual. O payload nunca é tocado."""
    resultado = m2m.reinterpretar_historico(request.user)
    messages.success(
        request,
        f"Histórico reinterpretado: {resultado['alteradas']} de {resultado['consultas']} consultas "
        f"e {resultado['chips']} chips atualizados.",
    )
    return redirect(reverse("m2m_config") + "#mapeamento")
