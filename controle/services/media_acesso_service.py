"""
Autorização de leitura dos arquivos de `MEDIA_ROOT`.

Antes desta camada, `/media/` era servido por `django.views.static.serve` sem
nenhum decorador: qualquer um que alcançasse a porta do servidor — inclusive
pelo domínio externo — baixava por URL os termos de responsabilidade
assinados, as notas fiscais e as fotos de preventiva. Este módulo é a regra
única de quem pode ler o quê; a view (`views/media_protegida.py`) só aplica.

Modelo de autorização (o mais restritivo que ainda não quebra tela existente):

  • Usuário INTERNO (não pertence a nenhum grupo de portal externo) — lê tudo.
    É o mesmo alcance que ele já tem navegando pelo sistema.

  • FORNECEDOR (grupo "Fornecedor") — lê APENAS as notas fiscais anexadas aos
    seus próprios registros, resolvidas pela mesma cadeia de dono que o Portal
    do Fornecedor usa nas queries. São os 3 anexos de NF do fluxo dele:
      - OrdemManutencaoAnexo        → ordem.fornecedor
      - LoteEnvioFornecedorAnexo    → lote.fornecedor
      - SeparacaoItemAnexo          → separacao.fornecedor
    Nada de termos, fotos de preventiva ou plantas.

  • PARCEIRO DE LICENÇAS e VISUALIZADOR TV — nenhum arquivo. Conferido tela por
    tela: nenhum template desses dois escopos referencia `MEDIA_URL` (o modo TV
    desenha a planta no canvas, não usa `imagem_fundo`). Se algum dia uma tela
    desses perfis precisar de arquivo, a liberação entra aqui — e só aqui.

A decisão é sempre tomada a partir do CAMINHO RELATIVO do arquivo dentro do
`MEDIA_ROOT` (ex.: "manutencao/nf/2026/10/nota.pdf"), que é exatamente o valor
gravado no `FileField` — por isso a conferência de dono é uma consulta direta
pelo campo, sem adivinhar nome de arquivo.
"""
from ProjetoEstoque.models import GRUPO_FORNECEDOR, GRUPO_PARCEIRO_LICENCA

# Grupos de portal externo: a presença de QUALQUER um deles faz o usuário
# deixar de ser tratado como interno. Mantido alinhado com os middlewares de
# isolamento (ProjetoEstoque/middleware.py).
GRUPO_TV = "Visualizador TV"
GRUPOS_EXTERNOS = (GRUPO_FORNECEDOR, GRUPO_PARCEIRO_LICENCA, GRUPO_TV)


def _grupos(user) -> set[str]:
    return set(user.groups.values_list("name", flat=True))


def _fornecedor_do_usuario(user):
    """Fornecedor vinculado ao login, ou None. Mesma regra do Portal."""
    perfil = getattr(user, "perfil_fornecedor", None)
    if perfil is not None and perfil.ativo:
        return perfil.fornecedor
    return None


def _e_nf_do_fornecedor(caminho: str, fornecedor_id: int) -> bool:
    """
    True se `caminho` for um anexo de NF pertencente a este fornecedor.

    Import local: este módulo é importado pela view, que é importada pelo
    pacote de views no carregamento do app — importar models no topo junto com
    as constantes já basta, mas as consultas ficam aqui para manter a função
    autocontida e fácil de estender com um 4º tipo de anexo.
    """
    from ProjetoEstoque.models import (
        LoteEnvioFornecedorAnexo,
        OrdemManutencaoAnexo,
        SeparacaoItemAnexo,
    )

    consultas = (
        OrdemManutencaoAnexo.objects.filter(
            arquivo=caminho, ordem__fornecedor_id=fornecedor_id
        ),
        LoteEnvioFornecedorAnexo.objects.filter(
            arquivo=caminho, lote__fornecedor_id=fornecedor_id
        ),
        # `SeparacaoItem.fornecedor` é FK direta — não ir por `lote`, que é
        # anulável (item em remessa pode existir antes de entrar num lote).
        SeparacaoItemAnexo.objects.filter(
            arquivo=caminho, separacao__fornecedor_id=fornecedor_id
        ),
    )
    return any(qs.exists() for qs in consultas)


def pode_ver_media(user, caminho: str) -> bool:
    """
    Decide se `user` pode baixar o arquivo de `MEDIA_ROOT` em `caminho`
    (relativo, com barras normais — ex.: "termos/termo_12.pdf").

    Não levanta exceção e não toca no disco: só responde sobre autorização.
    A existência do arquivo é problema de quem serve.
    """
    if not getattr(user, "is_authenticated", False):
        return False

    # Staff/superusuário nunca é afetado pelos middlewares de portal; aqui
    # também não, para o comportamento ser o mesmo nas duas camadas.
    if user.is_staff or user.is_superuser:
        return True

    externos = _grupos(user) & set(GRUPOS_EXTERNOS)
    if not externos:
        return True  # usuário interno comum

    if GRUPO_FORNECEDOR in externos:
        fornecedor = _fornecedor_do_usuario(user)
        if fornecedor is not None and _e_nf_do_fornecedor(caminho, fornecedor.id):
            return True

    # Parceiro de Licenças, Visualizador TV, ou fornecedor pedindo arquivo
    # que não é dele.
    return False
