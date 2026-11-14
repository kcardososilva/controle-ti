"""
Frotas e Maquinários — o assistente de importação da planilha de equipamentos motorizados, em seis passos:

    1 Envio  ·  2 Mapeamento  ·  3 Pré-visualização  ·  4 Validação  ·  5 Confirmação  ·  6 Resultado

Os passos 2 a 6 são UMA página (`frota_equip_importacao`, `?etapa=`) sobre a MESMA importação: o estado dela diz até onde
se pode ir. Enquanto o responsável não passou pelo mapeamento (`mapeamento["conferido"]`), pré-visualização, validação e
confirmação ficam fechadas; depois de confirmada ou cancelada, só o resultado existe. Nada é gravado na frota antes do
passo 5, e o passo 5 é um POST que o serviço protege contra clique duplo e prévia vencida.

As views NÃO têm regra de negócio: validam a entrada, chamam `frota_equipamento_import_service` e renderizam.
Acesso: `importar_equipamentos_frota` em tudo.
"""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from ProjetoEstoque.models import FrotaImportacaoEquipamentoLinha, FrotaImportacaoEquipamentos
from services import frota_equipamento_import_service as imp
from services import frota_relatorio_service as relatorios
from services import frota_service as frota

from .frota_comum import contexto_base, exige_frota, filtros_do_get, mensagem_de, migalhas, paginar, pode

TIPO_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
Estado = FrotaImportacaoEquipamentos.Estado
Situacao = FrotaImportacaoEquipamentoLinha.Situacao

ETAPAS = (
    ("envio", "Envio", "arquivo e leitura"), ("mapeamento", "Mapeamento", "colunas e valores"),
    ("previa", "Pré-visualização", "o que será gravado"), ("validacao", "Validação", "o que conferir"),
    ("confirmacao", "Confirmação", "gravar na frota"), ("resultado", "Resultado", "relatório"),
)
ETAPAS_COM_MAPEAMENTO_CONFERIDO = ("previa", "validacao", "confirmacao")
SITUACOES_DA_PREVIA = (
    ("", "Todas"), (Situacao.VALIDA, "Válidas"), (Situacao.EXISTENTE, "Já cadastradas"), (Situacao.REJEITADA, "Rejeitadas"),
    (Situacao.IMPORTADA, "Importadas"), (Situacao.ATUALIZADA, "Atualizadas"),
)
# Os valores da planilha que pedem decisão, na ordem em que a tela os mostra. `vazio` é o texto da opção sem escolha
# e `rejeita`, se a linha sem escolha é rejeitada (situação e empresa) ou cai num padrão (classe operacional → "Outros").
GRUPOS_DE_VALORES = (
    {
        "campo": "situacao", "titulo": "Situação do equipamento", "destino": "Status", "rejeita": True, "vazio": "Escolher…",
        "ajuda": "“Em Uso” vira Em uso. Qualquer outro valor precisa da sua escolha; sem ela, a linha é rejeitada. "
                 "Em uso não quer dizer validado: o responsável pelo inventário confirma a situação depois.",
    },
    {
        "campo": "empresa", "titulo": "Empresa", "destino": "Natureza", "rejeita": True, "vazio": "Escolher…",
        "ajuda": "A natureza vem da empresa: SANTA COLOMBA é próprio e TERCEIROS é de terceiro. Outro nome precisa da sua "
                 "escolha, porque o sistema não adivinha de quem é o ativo; sem ela, a linha é rejeitada.",
    },
    {
        "campo": "classe_operacional", "titulo": "Classe operacional", "destino": "Categoria", "rejeita": False,
        "vazio": "Outros (padrão)",
        "ajuda": "A classe operacional continua com o nome do ERP; a categoria só a agrupa (Trator, Caminhão, Motocicleta…). "
                 "Sem escolha, o equipamento fica na categoria “Outros”.",
    },
)


def _passos(importacao, etapa: str) -> list[dict]:
    """O alto do assistente: cada passo concluído, atual ou pendente, com o link quando se pode ir até ele.

    Em importação cancelada ou expirada só o que de fato aconteceu aparece como concluído: marcar a prévia e a
    confirmação como feitas, sem nada ter sido gravado, faria o alto da tela afirmar o contrário do que houve."""
    conferido = bool(importacao and importacao.mapeamento.get("conferido"))
    aberta = bool(importacao and importacao.estado == Estado.PREVIA and not importacao.expirada)
    terminada = bool(importacao and importacao.estado != Estado.PREVIA)
    confirmada = bool(importacao and importacao.estado == Estado.CONFIRMADA)
    posicao_atual = [chave for chave, _, _ in ETAPAS].index(etapa)
    passos = []
    for posicao, (chave, rotulo, sub) in enumerate(ETAPAS):
        if chave == "envio":
            url, liberado = reverse("frota_equip_importar"), True
        else:
            liberado = (
                (chave == "mapeamento" and aberta) or (chave in ETAPAS_COM_MAPEAMENTO_CONFERIDO and aberta and conferido)
                or (chave == "resultado" and terminada)
            )
            url = reverse("frota_equip_importacao", args=[importacao.pk]) + f"?etapa={chave}" if importacao and liberado else ""
        if chave == etapa:
            estado = "atual"
        elif chave == "envio":
            estado = "concluido"
        elif etapa == "resultado":
            estado = "concluido" if confirmada or (chave == "mapeamento" and conferido) else "pendente"
        else:
            estado = "concluido" if posicao < posicao_atual else "pendente"
        passos.append({"chave": chave, "rotulo": rotulo, "sub": sub, "numero": posicao + 1, "estado": estado, "url": url if liberado else ""})
    return passos


def _grupos_de_valores(painel: dict) -> list[dict]:
    """Os três quadros de "o que cada valor vira" do mapeamento: cada um com os valores da planilha (e quantos equipamentos
    têm cada um), a decisão atual, a sugestão e as opções. A categoria vira par (valor, rótulo) como as outras opções."""
    grupos = []
    for modelo in GRUPOS_DE_VALORES:
        itens = painel["valores"].get(modelo["campo"], [])
        opcoes = painel["opcoes"][modelo["campo"]]
        if modelo["campo"] == "classe_operacional":
            opcoes = [(nome, nome) for nome in opcoes]
        grupos.append({
            **modelo, "itens": itens, "opcoes": opcoes,
            "pendentes": sum(1 for i in itens if not i["decisao"]) if modelo["rejeita"] else 0,
            "equipamentos_pendentes": sum(i["n"] for i in itens if not i["decisao"]) if modelo["rejeita"] else 0,
        })
    return grupos


def _linha_da_tela(linha) -> dict:
    """A linha como a tabela a mostra: a descrição do relatório mais as diferenças (quando a planilha mudaria um
    equipamento já cadastrado) com o nome de cada campo em português."""
    def rotular(mudancas: dict) -> list[dict]:
        return [
            {"rotulo": frota.rotulo_do_campo(campo), "de": m["de"], "para": m["para"]}
            for campo, m in sorted(mudancas.items(), key=lambda item: frota.rotulo_do_campo(item[0]))
        ]

    descrita = imp.linha_do_relatorio(linha)
    descrita["mudancas"] = rotular(descrita["diferencas"])
    descrita["aplicadas_lista"] = rotular(descrita["aplicadas"])
    return descrita


def _tabela_de_linhas(request, importacao) -> dict:
    """O contexto da tabela de linhas (prévia e resultado): filtros por situação, conferência e busca, e a paginação."""
    filtros = filtros_do_get(request, ("situacao", "regra", "q"))
    if filtros["situacao"] not in dict(SITUACOES_DA_PREVIA):
        filtros["situacao"] = ""
    page_obj, paginas, querystring = paginar(
        request, imp.linhas_da_importacao(importacao, filtros["situacao"] or None, filtros["regra"] or None, filtros["q"]), por_pagina=50,
    )
    contagem = {
        "": importacao.linhas, Situacao.VALIDA: importacao.validas, Situacao.EXISTENTE: importacao.existentes,
        Situacao.REJEITADA: importacao.rejeitados, Situacao.IMPORTADA: importacao.importados, Situacao.ATUALIZADA: importacao.atualizados,
    }
    return {
        "linhas": [_linha_da_tela(l) for l in page_obj.object_list], "page_obj": page_obj, "paginas": paginas,
        "querystring": querystring, "filtros": filtros,
        "situacoes": [(c, r, contagem[c]) for c, r in SITUACOES_DA_PREVIA if c == "" or contagem[c]],
        "regras": [(g["codigo"], g["titulo"], g["n"]) for g in imp.conferencias_agrupadas(importacao)],
    }


def _etapa_pedida(request, importacao) -> str:
    """A etapa que a tela mostra: a pedida, se o estado da importação a permite; senão, a que cabe agora."""
    pedida = (request.GET.get("etapa") or "").strip()
    if importacao.estado != Estado.PREVIA or importacao.expirada:
        return "resultado"
    conferido = bool(importacao.mapeamento.get("conferido"))
    if not conferido:
        return "mapeamento"
    if pedida in ("mapeamento", *ETAPAS_COM_MAPEAMENTO_CONFERIDO):
        return pedida
    return "previa"


@exige_frota("importar_equipamentos_frota")
def frota_equip_importar(request):
    """Passo 1 — Envio. O POST lê e VALIDA a planilha (nada é gravado na frota) e leva ao mapeamento. Arquivo ilegível,
    grande demais, de formato errado ou sem a coluna Equipamento volta aqui com o motivo e a lista das colunas esperadas."""
    erro = ""
    if request.method == "POST":
        arquivo = request.FILES.get("arquivo")
        if arquivo is None:
            erro = "Escolha a planilha (.xlsx ou .csv) para enviar."
        elif arquivo.size > imp.MAX_BYTES:
            erro = f"O arquivo passa do limite de {imp.MAX_BYTES // (1024 * 1024)} MB."
        else:
            try:
                importacao = imp.enviar(arquivo, request.user)
            except ValidationError as exc:
                erro = " ".join(exc.messages)
            else:
                messages.success(request, "Planilha lida. Confira o mapeamento: nada foi gravado na frota ainda.")
                return redirect(reverse("frota_equip_importacao", args=[importacao.pk]) + "?etapa=mapeamento")
    return render(request, "front/agro/frota/importar.html", contexto_base(
        request, "importar_equip", faixa=False,
        migalhas=migalhas(("Importação de planilha", None)), etapa="envio", passos=_passos(None, "envio"), erro=erro,
        recentes=imp.listar_importacoes(limite=6), colunas=imp.COLUNAS, max_mb=imp.MAX_BYTES // (1024 * 1024), max_linhas=imp.MAX_LINHAS,
    ), status=400 if erro else 200)


@exige_frota("importar_equipamentos_frota")
@require_GET
def frota_equip_importar_modelo(request):
    """O arquivo-modelo (`?formato=csv` para CSV; padrão, Excel): as colunas esperadas e as instruções."""
    if request.GET.get("formato") == "csv":
        resposta = HttpResponse(imp.modelo_csv(), content_type="text/csv; charset=utf-8")
        resposta["Content-Disposition"] = 'attachment; filename="modelo_equipamentos_motorizados.csv"'
    else:
        resposta = HttpResponse(imp.modelo_xlsx(), content_type=TIPO_XLSX)
        resposta["Content-Disposition"] = 'attachment; filename="modelo_equipamentos_motorizados.xlsx"'
    return resposta


@exige_frota("importar_equipamentos_frota")
def frota_equip_importacoes(request):
    """Todas as importações já feitas (a lista de auditoria): quem enviou o quê, e o que aconteceu."""
    page_obj, paginas, querystring = paginar(
        request, FrotaImportacaoEquipamentos.objects.select_related("criada_por", "confirmada_por").order_by("-criada_em", "-pk"), por_pagina=30,
    )
    return render(request, "front/agro/frota/importacoes.html", contexto_base(
        request, "importar_equip", faixa=False,
        migalhas=migalhas(("Importação de planilha", reverse("frota_equip_importar")), ("Importações anteriores", None)),
        importacoes=page_obj.object_list, page_obj=page_obj, paginas=paginas, querystring=querystring,
    ))


@exige_frota("importar_equipamentos_frota")
def frota_equip_importacao(request, pk):
    """Passos 2 a 6, pela etapa pedida e pelo estado da importação."""
    importacao = get_object_or_404(FrotaImportacaoEquipamentos.objects.select_related("criada_por", "confirmada_por"), pk=pk)
    etapa = _etapa_pedida(request, importacao)
    pedida = (request.GET.get("etapa") or "").strip()
    if pedida and pedida != etapa and pedida in {chave for chave, _, _ in ETAPAS}:
        if importacao.estado == Estado.PREVIA and not importacao.expirada and not importacao.mapeamento.get("conferido"):
            messages.info(request, "Confira o mapeamento primeiro: ele decide o que cada coluna e cada valor da planilha vira.")
        return redirect(reverse("frota_equip_importacao", args=[pk]) + f"?etapa={etapa}")

    contexto = contexto_base(
        request, "importar_equip", faixa=False,
        migalhas=migalhas(("Importação de planilha", reverse("frota_equip_importar")), (importacao.arquivo_nome, None)),
        importacao=importacao, etapa=etapa, passos=_passos(importacao, etapa), resumo=imp.resumo(importacao),
        expirada=importacao.expirada, ja_importado=imp.arquivo_ja_importado(importacao),
    )
    if etapa == "mapeamento":
        painel = imp.painel_do_mapeamento(importacao)
        contexto.update(
            painel=painel, indices_do_arquivo=list(enumerate(painel["cabecalho"], start=1)), grupos_de_valores=_grupos_de_valores(painel),
            # sem a coluna Empresa o sistema não tem de onde tirar a natureza (próprio ou terceiro): toda linha seria rejeitada
            sem_empresa=not any(c["campo"] == "empresa" and c["indice"] is not None for c in painel["colunas"]),
        )
    elif etapa == "previa":
        contexto.update(_tabela_de_linhas(request, importacao))
    elif etapa == "validacao":
        contexto.update(grupos=imp.conferencias_agrupadas(importacao), motivos=imp.motivos_de_rejeicao(importacao))
    elif etapa == "confirmacao":
        contexto.update(
            com_diferencas=[
                _linha_da_tela(l) for l in imp.linhas_da_importacao(importacao, Situacao.EXISTENTE) if (l.dados or {}).get("diferencas")
            ][:10],
            grupos=imp.conferencias_agrupadas(importacao),
        )
    else:
        contexto.update(
            _tabela_de_linhas(request, importacao), motivos=imp.motivos_de_rejeicao(importacao),
            avisos_de_cadastro=importacao.mapeamento.get("avisos_de_cadastro", []), pode_ver_equipamentos=pode(request, "ver_frota"),
        )
    return render(request, "front/agro/frota/importacao.html", contexto)


def _mapa_de_valores(request, campo: str) -> dict:
    """O que o formulário decidiu para cada valor distinto de `campo`: dois campos de mesmo nome e mesma ordem —
    `<campo>_valor` (o texto como está na planilha) e `<campo>_decisao` (o que ele vira)."""
    return dict(zip(request.POST.getlist(f"{campo}_valor"), request.POST.getlist(f"{campo}_decisao")))


@exige_frota("importar_equipamentos_frota")
@require_POST
def frota_equip_importacao_mapear(request, pk):
    """Passo 2 → 3: grava o mapeamento (qual coluna é cada campo e o que cada valor vira) e reanalisa as linhas. As
    células da planilha não são lidas de novo; só o que foi decidido muda o resultado."""
    importacao = get_object_or_404(FrotaImportacaoEquipamentos, pk=pk)
    colunas = {c.campo: request.POST.get(f"coluna_{c.campo}", "") for c in imp.COLUNAS}
    valores = {campo: _mapa_de_valores(request, campo) for campo in ("situacao", "empresa", "classe_operacional")}
    try:
        imp.aplicar_mapeamento(importacao, colunas, valores)
    except ValidationError as exc:
        messages.error(request, mensagem_de(exc))
        return redirect(reverse("frota_equip_importacao", args=[pk]) + "?etapa=mapeamento")
    messages.success(request, "Mapeamento conferido. Veja o que será importado: nada foi gravado na frota ainda.")
    return redirect(reverse("frota_equip_importacao", args=[pk]) + "?etapa=previa")


@exige_frota("importar_equipamentos_frota")
@require_POST
def frota_equip_importacao_confirmar(request, pk):
    """Passo 5: grava os equipamentos. O serviço revalida cada linha, cria os cadastros de apoio que faltam e NUNCA
    altera equipamento já cadastrado — a menos que `atualizar=1` autorize, e então só os campos do ERP."""
    importacao = get_object_or_404(FrotaImportacaoEquipamentos, pk=pk)
    try:
        concluida = imp.confirmar(importacao, request.user, atualizar_existentes=request.POST.get("atualizar") == "1")
    except ValidationError as exc:
        messages.error(request, mensagem_de(exc))
        return redirect(reverse("frota_equip_importacao", args=[pk]))
    messages.success(
        request,
        f"Importação concluída: {concluida.importados} importado{'s' if concluida.importados != 1 else ''}, "
        f"{concluida.atualizados} atualizado{'s' if concluida.atualizados != 1 else ''}, {concluida.existentes} ignorado"
        f"{'s' if concluida.existentes != 1 else ''} por duplicidade e {concluida.rejeitados} rejeitado{'s' if concluida.rejeitados != 1 else ''}.",
    )
    return redirect(reverse("frota_equip_importacao", args=[pk]) + "?etapa=resultado")


@exige_frota("importar_equipamentos_frota")
@require_POST
def frota_equip_importacao_cancelar(request, pk):
    """Descarta a prévia. As linhas ficam como registro; nada foi gravado na frota."""
    importacao = get_object_or_404(FrotaImportacaoEquipamentos, pk=pk)
    try:
        imp.cancelar(importacao, request.user)
    except ValidationError as exc:
        messages.error(request, mensagem_de(exc))
    else:
        messages.info(request, "Importação cancelada. Nada foi gravado na frota.")
    return redirect(reverse("frota_equip_importacao", args=[pk]))


@exige_frota("importar_equipamentos_frota")
@require_GET
def frota_equip_importacao_relatorio(request, pk):
    """O relatório da importação em Excel (`?so=inconsistencias` leva só o resumo e as inconsistências). Vale em qualquer
    estado: antes de confirmar é o "relatório de inconsistências"; depois, o do processamento."""
    importacao = get_object_or_404(FrotaImportacaoEquipamentos, pk=pk)
    so = request.GET.get("so") == "inconsistencias"
    resposta = HttpResponse(imp.exportar_relatorio_xlsx(importacao, so_inconsistencias=so), content_type=TIPO_XLSX)
    prefixo = "inconsistencias_importacao" if so else "relatorio_importacao"
    resposta["Content-Disposition"] = f'attachment; filename="{relatorios.nome_do_arquivo(f"{prefixo}_{importacao.pk}")}"'
    return resposta
