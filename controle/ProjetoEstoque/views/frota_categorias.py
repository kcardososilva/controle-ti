"""
Frotas e Maquinários — Categorias e classes: como a frota é classificada.

UMA tela com cinco abas sobre os cadastros de apoio (não cinco páginas): as CATEGORIAS (os tipos de equipamento, que agrupam as
classes), as CLASSES OPERACIONAIS e MECÂNICAS do ERP, as EMPRESAS e os CENTROS DE CUSTO. O nome original do ERP se preserva;
a categoria só agrupa. Nada se exclui: inativar tira o item das próximas escolhas e preserva quem já o usa.

As views validam a entrada e chamam os services (`frota_service.salvar_tipo`, `frota_classificacao_service`). Acesso:
`administrar_frota_config`.
"""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from ProjetoEstoque.models import FrotaNatureza, FrotaTipo
from services import frota_classificacao_service as classificacao

from .frota_comum import contexto_base, erros_de, exige_frota, migalhas

ABAS = (
    ("categorias", "Categorias"), ("classes-operacionais", "Classes operacionais"), ("classes-mecanicas", "Classes mecânicas"),
    ("empresas", "Empresas"), ("centros-de-custo", "Centros de custo"),
)
# aba -> chave de `frota_classificacao_service.CATALOGOS`
CHAVE_DA_ABA = {
    "classes-operacionais": "classe_operacional", "classes-mecanicas": "classe_mecanica", "empresas": "empresa",
    "centros-de-custo": "centro_custo",
}
ABA_DA_CHAVE = {chave: aba for aba, chave in CHAVE_DA_ABA.items()}
AJUDAS = {
    "categorias": (
        "A categoria é o agrupamento superior (Trator, Caminhão, Motocicleta…). “Exige chip” é o padrão dela: equipamento em uso dessa "
        "categoria, sem chip, gera o alerta “Veículo sem chip”. “Costuma ter placa/chassi” só marca o que vira pendência cadastral — "
        "nunca bloqueia o cadastro: gerador, trator e implemento nem sempre têm."
    ),
    "classes-operacionais": (
        "A classe operacional é a do ERP, com o nome original. Escolha a categoria que a agrupa: o equipamento continua sabendo qual é "
        "a classe dele. Mudar a categoria vale para os próximos equipamentos; marque “aplicar também aos já cadastrados” para "
        "reclassificar os atuais (cada um fica registrado no histórico)."
    ),
    "classes-mecanicas": "A classe mecânica é outra leitura do ERP (não é um nível da classe operacional): uma pá carregadeira operacional pode ser “linha amarela” na mecânica.",
    "empresas": "A natureza padrão da empresa só SUGERE a natureza do equipamento: no cadastro dele, ela é um campo próprio e pode ser trocada.",
    "centros-de-custo": "O centro de custo é o do ERP e é cadastro próprio da frota (não é o de departamentos do estoque de TI). O código do ERP identifica o centro: a importação o reconhece por ele.",
}


def _aba_pedida(request, padrao: str) -> str:
    aba = (request.GET.get("aba") or padrao).strip()
    if aba not in dict(ABAS):
        raise Http404("Aba desconhecida.")
    return aba


def _pagina(request, aba: str):
    busca = (request.GET.get("q") or "").strip()
    chave = CHAVE_DA_ABA.get(aba)
    contagens = {
        "categorias": FrotaTipo.objects.count(),
        **{a: classificacao.CATALOGOS[c].modelo.objects.count() for a, c in CHAVE_DA_ABA.items()},
    }
    itens = classificacao.listar_categorias() if aba == "categorias" else classificacao.listar_cadastro(chave, busca)
    return render(request, "front/agro/frota/categorias.html", contexto_base(
        request, "categorias", faixa=False,
        migalhas=migalhas(("Categorias e classes", None)), aba_cadastro=aba, abas=[(a, r, contagens[a]) for a, r in ABAS],
        chave=chave, itens=itens, ajuda=AJUDAS[aba], busca=busca,
        categorias=FrotaTipo.objects.filter(ativo=True).order_by("nome"), naturezas=FrotaNatureza.choices,
        classes_sem_categoria=classificacao.classes_sem_categoria(),
        rotulo_do_cadastro=classificacao.CATALOGOS[chave].rotulo if chave else "categoria",
    ))


@exige_frota("administrar_frota_config")
def frota_categorias(request):
    """Categorias e classes (`/agro/frotas/categorias/?aba=…`): as cinco abas dos cadastros de classificação."""
    return _pagina(request, _aba_pedida(request, "categorias"))


@exige_frota("administrar_frota_config")
def frota_config_tipos(request):
    """O endereço de antes (Configuração › Tipos de veículo): abre a aba de categorias."""
    return _pagina(request, "categorias")


def _marcado(request, nome: str) -> bool:
    return request.POST.get(nome) == "1"


@exige_frota("administrar_frota_config")
@require_POST
def frota_cadastro_salvar(request):
    """Cria (sem `id`) ou edita um item de classificação — `chave`: classe_operacional, classe_mecanica, empresa ou
    centro_custo (as categorias vão por `frota_config_tipo_salvar`). Campos: `nome`, `codigo_erp`, `ativo` (só na edição), e
    o próprio do cadastro (`categoria` da classe operacional, `natureza_padrao` da empresa). Na classe operacional,
    `reclassificar=1` leva à categoria dela os equipamentos já cadastrados da classe."""
    chave = request.POST.get("chave", "")
    if chave not in classificacao.CATALOGOS:
        raise Http404("Cadastro desconhecido.")
    catalogo = classificacao.CATALOGOS[chave]
    pk = request.POST.get("id", "")
    objeto = get_object_or_404(catalogo.modelo, pk=int(pk)) if pk.isdigit() else None
    dados = {"nome": request.POST.get("nome", ""), "codigo_erp": request.POST.get("codigo_erp", "")}
    if objeto is not None:
        dados["ativo"] = _marcado(request, "ativo")
    if chave == "classe_operacional":
        dados["categoria"] = request.POST.get("categoria", "")
    elif chave == "empresa" and request.POST.get("natureza_padrao"):
        dados["natureza_padrao"] = request.POST["natureza_padrao"]
    destino = reverse("frota_categorias") + f"?aba={ABA_DA_CHAVE[chave]}"
    try:
        salvo = classificacao.salvar_cadastro(chave, dados, request.user, objeto)
        mensagem = f"{catalogo.rotulo.capitalize()} salv{'a' if catalogo.artigo == 'a' else 'o'}." if objeto else f"{catalogo.rotulo.capitalize()} cri{'ada' if catalogo.artigo == 'a' else 'ado'}."
        if chave == "classe_operacional" and _marcado(request, "reclassificar"):
            n = classificacao.reclassificar_equipamentos(salvo, request.user)
            mensagem += f" {n} equipamento{'s' if n != 1 else ''} reclassificado{'s' if n != 1 else ''}."
    except ValidationError as exc:
        messages.error(request, " ".join(erros_de(exc).values()))
    else:
        messages.success(request, mensagem)
    return redirect(destino)
