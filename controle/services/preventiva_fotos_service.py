"""
preventiva_fotos_service.py — Galeria de evidências fotográficas das preventivas.

Ponto único onde as fotos de uma execução são gravadas, removidas e lidas para
exibição. As views só repassam `request.FILES`/`request.POST`; nenhuma regra de
foto mora em view ou template.

Contexto do modelo de dados (importa para entender as funções abaixo):

  - `PreventivaFoto` é a galeria nova: N fotos por execução, cada uma marcada
    como `antes`, `depois` ou `ambos`.
  - Os 4 campos antigos (`foto_antes`, `foto_depois`, `foto_antes_2`,
    `foto_depois_2`) continuam existindo em `Preventiva` e `PreventivaExecucao`.
    NÃO são mais a fonte de verdade da exibição — a galeria é —, mas seguem
    sendo preenchidos com as primeiras fotos de cada momento (ver
    `_espelhar_em_campos_legados`) para que qualquer consulta, relatório ou
    integração que ainda leia aqueles campos continue funcionando enquanto a
    remoção não acontece.
"""
from django.db import transaction


# Teto de fotos por execução. Não é limitação de produto (o pedido era
# justamente "quantas eu quiser"): é defesa contra um envio acidental de
# centenas de arquivos travar a requisição e encher o disco.
MAX_FOTOS_POR_EXECUCAO = 40

# Tamanho máximo por arquivo. Fotos de celular moderno passam de 5 MB; 12 MB dá
# folga para originais sem virar vetor de esgotamento de disco.
MAX_BYTES_POR_FOTO = 12 * 1024 * 1024

_EXTENSOES_ACEITAS = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".gif", ".bmp")


class FotoInvalida(ValueError):
    """Arquivo recusado na validação (tipo ou tamanho)."""


def momentos_validos() -> list:
    """[(valor, rótulo)] dos momentos, para montar o seletor no template."""
    from ProjetoEstoque.models import PreventivaFoto
    return list(PreventivaFoto.Momento.choices)


def _normalizar_momento(valor: str) -> str:
    from ProjetoEstoque.models import PreventivaFoto
    valor = (valor or "").strip().lower()
    if valor in PreventivaFoto.Momento.values:
        return valor
    # Default seguro: "antes". Marcar como "ambos" por engano faria a foto
    # aparecer em dois lugares na comparação, distorcendo a evidência.
    return PreventivaFoto.Momento.ANTES


def validar_arquivo(arquivo) -> None:
    """Valida tipo e tamanho. Levanta FotoInvalida com mensagem em pt-BR."""
    nome = (getattr(arquivo, "name", "") or "").lower()
    if not nome.endswith(_EXTENSOES_ACEITAS):
        raise FotoInvalida(
            f"“{getattr(arquivo, 'name', 'arquivo')}” não é uma imagem aceita. "
            f"Formatos: {', '.join(e.lstrip('.').upper() for e in _EXTENSOES_ACEITAS)}."
        )
    tamanho = getattr(arquivo, "size", 0) or 0
    if tamanho > MAX_BYTES_POR_FOTO:
        mb = MAX_BYTES_POR_FOTO // (1024 * 1024)
        raise FotoInvalida(
            f"“{arquivo.name}” tem {tamanho / (1024 * 1024):.1f} MB e excede o limite de {mb} MB por foto."
        )


def coletar_do_request(request, campo_arquivos: str = "fotos", campo_momentos: str = "momentos") -> list:
    """
    Lê os arquivos enviados pela área de upload e casa cada um com o momento
    marcado no formulário.

    O template envia um `<input type="file" multiple name="fotos">` e um
    `<input type="hidden" name="momentos">` POR ARQUIVO, na mesma ordem — é
    assim que cada foto carrega a sua própria marcação Antes/Depois/Ambos.
    Se as listas vierem desalinhadas (JS desligado, submit manual), o que
    faltar cai no default de `_normalizar_momento`, nunca em erro.

    Devolve [(arquivo, momento)], já validados.
    """
    arquivos = request.FILES.getlist(campo_arquivos)
    momentos = request.POST.getlist(campo_momentos)
    saida = []
    for indice, arquivo in enumerate(arquivos):
        if not arquivo:
            continue
        validar_arquivo(arquivo)
        momento = momentos[indice] if indice < len(momentos) else ""
        saida.append((arquivo, _normalizar_momento(momento)))
    if len(saida) > MAX_FOTOS_POR_EXECUCAO:
        raise FotoInvalida(
            f"São aceitas no máximo {MAX_FOTOS_POR_EXECUCAO} fotos por execução "
            f"(foram enviadas {len(saida)})."
        )
    return saida


@transaction.atomic
def adicionar(execucao, fotos: list, usuario=None) -> int:
    """
    Anexa `fotos` ([(arquivo, momento)]) à execução. Devolve quantas entraram.

    A `ordem` continua a numeração já existente na execução, para que fotos
    adicionadas numa edição posterior fiquem DEPOIS das originais em vez de se
    embaralharem com elas.
    """
    from ProjetoEstoque.models import PreventivaFoto

    if not fotos:
        return 0

    existentes = execucao.fotos.count()
    total_final = existentes + len(fotos)
    if total_final > MAX_FOTOS_POR_EXECUCAO:
        raise FotoInvalida(
            f"Esta execução ficaria com {total_final} fotos e o limite é {MAX_FOTOS_POR_EXECUCAO}. "
            f"Remova alguma antes de enviar mais."
        )

    ultima_ordem = existentes
    criadas = []
    for deslocamento, (arquivo, momento) in enumerate(fotos):
        criadas.append(PreventivaFoto.objects.create(
            preventiva=execucao.preventiva,
            execucao=execucao,
            imagem=arquivo,
            momento=momento,
            ordem=ultima_ordem + deslocamento,
            criado_por=usuario,
            atualizado_por=usuario,
        ))

    _espelhar_em_campos_legados(execucao)
    return len(criadas)


@transaction.atomic
def remover(execucao, ids: list, usuario=None) -> int:
    """
    Remove fotos da execução pelos ids informados. Devolve quantas saíram.

    Só apaga fotos QUE PERTENCEM a esta execução — o id vem do formulário e não
    é confiável; sem esse filtro, um id forjado apagaria evidência de outra
    preventiva.

    Os arquivos em MEDIA_ROOT NÃO são apagados de propósito: uma evidência
    removida por engano da tela ainda pode ser recuperada no servidor, e fotos
    migradas do modelo antigo podem estar referenciadas também pelos campos
    legados, que continuam existindo.
    """
    if not ids:
        return 0
    inteiros = []
    for valor in ids:
        try:
            inteiros.append(int(valor))
        except (TypeError, ValueError):
            continue
    if not inteiros:
        return 0

    apagadas, _ = execucao.fotos.filter(id__in=inteiros).delete()
    if apagadas:
        _espelhar_em_campos_legados(execucao)
    return apagadas


def _quatro_primeiras(fotos: list) -> tuple:
    """(antes, antes_2, depois, depois_2) a partir de uma lista de PreventivaFoto.

    É a tradução da galeria (N fotos) para os 4 campos legados. Uma foto
    marcada como `ambos` entra nas duas pontas — exatamente o que os campos
    fixos nunca souberam representar. Devolve "" onde não há foto.
    """
    from ProjetoEstoque.models import PreventivaFoto

    antes = [f for f in fotos if f.momento in (PreventivaFoto.Momento.ANTES, PreventivaFoto.Momento.AMBOS)]
    depois = [f for f in fotos if f.momento in (PreventivaFoto.Momento.DEPOIS, PreventivaFoto.Momento.AMBOS)]

    def nome(lista, indice):
        return lista[indice].imagem.name if len(lista) > indice else ""

    return nome(antes, 0), nome(antes, 1), nome(depois, 0), nome(depois, 1)


def _gravar_legados(alvo, quatro: tuple) -> None:
    """Escreve os 4 campos legados em uma Preventiva ou PreventivaExecucao."""
    alvo.foto_antes, alvo.foto_antes_2, alvo.foto_depois, alvo.foto_depois_2 = quatro
    alvo.save(update_fields=[
        "foto_antes", "foto_antes_2", "foto_depois", "foto_depois_2", "updated_at",
    ])


def _espelhar_em_campos_legados(execucao) -> None:
    """
    Reflete as primeiras fotos da galeria nos 4 campos antigos da execução e da
    preventiva.

    Por que isto existe: as colunas legadas continuam no banco (a remoção ficou
    para depois de validar em produção) e qualquer código que ainda as leia
    passaria a ver evidência desatualizada assim que a galeria virasse a via
    principal de upload. Espelhar mantém os dois lados coerentes durante a
    transição, sem duplicar arquivo nenhum: os campos recebem apenas o CAMINHO
    já gravado pela galeria.

    Uma foto marcada como `ambos` conta para os dois lados — é exatamente o
    caso que os campos fixos não sabiam representar.
    """
    quatro = _quatro_primeiras(list(execucao.fotos.all().order_by("ordem", "id")))
    _gravar_legados(execucao, quatro)

    # A preventiva guarda a "última evidência": só espelha se ESTA execução for
    # a mais recente dela — senão, editar uma execução antiga sobrescreveria a
    # vitrine da preventiva com fotos velhas.
    mais_recente = (
        execucao.preventiva.execucoes
        .order_by("-data_execucao", "-created_at")
        .values_list("id", flat=True)
        .first()
    )
    if mais_recente == execucao.id:
        _gravar_legados(execucao.preventiva, quatro)


@transaction.atomic
def reespelhar_preventiva(preventiva) -> None:
    """
    Recalcula a "última evidência" da preventiva a partir da execução mais
    recente que existe AGORA.

    Chamado após excluir uma execução: as fotos dela saem em cascata, mas os 4
    campos legados da preventiva continuariam apontando para arquivos que não
    pertencem mais a nenhuma execução. Sem isso, a última evidência da
    preventiva ficaria referenciando fotos órfãs.

    Três casos, nesta ordem:

      1. Sobrou execução com fotos → espelha a mais recente delas.
      2. Não sobrou, mas a preventiva tem evidências SEM execução (o snapshot
         anterior ao histórico de execuções, preservado na galeria) → volta a
         apontar para elas. É o registro histórico legítimo da preventiva.
      3. Não há foto nenhuma → limpa os campos. Deixá-los com o valor antigo
         seria manter uma evidência fantasma, apontando para a foto de uma
         execução que acabou de ser excluída.
    """
    ultima = (
        preventiva.execucoes
        .filter(fotos__isnull=False)
        .order_by("-data_execucao", "-created_at")
        .distinct()
        .first()
    )
    if ultima is not None:
        # Escreve direto, sem passar por _espelhar_em_campos_legados: aquela
        # função só toca na preventiva quando a execução é a mais recente de
        # TODAS, e aqui a mais recente pode ser justamente uma sem fotos.
        _gravar_legados(preventiva, _quatro_primeiras(list(ultima.fotos.all().order_by("ordem", "id"))))
        return

    orfas = list(preventiva.fotos.filter(execucao__isnull=True).order_by("ordem", "id"))
    _gravar_legados(preventiva, _quatro_primeiras(orfas))


def galeria_da_execucao(execucao) -> dict:
    """
    Agrupa as fotos de UMA execução para a comparação antes × depois.

    Uma foto `ambos` aparece nas DUAS colunas (é o que ela significa), marcada
    para o usuário entender que é o mesmo arquivo e não uma duplicata.
    """
    from ProjetoEstoque.models import PreventivaFoto

    fotos = list(execucao.fotos.all().order_by("ordem", "id"))
    return {
        "todas": fotos,
        "antes": [f for f in fotos if f.momento in (PreventivaFoto.Momento.ANTES, PreventivaFoto.Momento.AMBOS)],
        "depois": [f for f in fotos if f.momento in (PreventivaFoto.Momento.DEPOIS, PreventivaFoto.Momento.AMBOS)],
        "ambos": [f for f in fotos if f.momento == PreventivaFoto.Momento.AMBOS],
        "total": len(fotos),
    }


def fotos_orfas(preventiva) -> list:
    """
    Evidências da preventiva sem execução vinculada.

    São as fotos que existiam no snapshot de `Preventiva` antes de o histórico
    de execuções existir. Não têm data nem técnico — aparecem numa seção
    própria na ficha, identificadas como registro anterior, para que o histórico
    fotográfico antigo continue visível em vez de sumir da tela.
    """
    return list(preventiva.fotos.filter(execucao__isnull=True).order_by("ordem", "id"))
