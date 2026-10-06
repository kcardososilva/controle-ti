"""
Referência viva do design system (v5).

Uma folha de estilo não se revisa lendo CSS — se revisa olhando. Esta tela
monta todos os tokens e todos os componentes num lugar só, consumindo as
MESMAS folhas que o resto do sistema (tokens.css, base.css, ui-kit.css). Se um
token muda, muda aqui; se um componente quebra, quebra aqui primeiro.

Serve para três coisas concretas:

  • conferir contraste de texto e de chip nos dois temas, lado a lado;
  • descobrir que componente já existe antes de inventar o 7º estilo de
    paginação (hoje há 6 marcações diferentes para paginar);
  • ver o efeito de uma mudança de token sem abrir 174 telas.

É restrita a staff: não tem dado de ninguém, mas também não é tela de
operação e não deve aparecer para fornecedor.
"""
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render

# Ordem = ordem de exibição. Mantida à mão de propósito: é uma lista
# curada do que vale mostrar, não um dump de todos os tokens.
_CORES_SUPERFICIE = [
    ("--bg-page", "Canvas da página"),
    ("--bg-page-2", "Canvas rebaixado"),
    ("--bg-elevated", "Card, popover"),
    ("--bg-fill-tertiary", "Preenchimento sutil"),
    ("--bg-fill-secondary", "Preenchimento médio"),
    ("--bg-fill-primary", "Preenchimento forte"),
]

_CORES_TEXTO = [
    ("--text-primary", "Corpo, título"),
    ("--text-secondary", "Rótulo de campo"),
    ("--text-tertiary", "Legenda, meta"),
    ("--text-quaternary", "Placeholder"),
]

_CORES_ACAO = [
    ("--brand", "Ação principal, link"),
    ("--brand-hover", "Ação sob o cursor"),
    ("--brand-soft", "Fundo de seleção"),
    ("--brand-ring", "Anel de foco"),
]

_CORES_ESTADO = [
    ("--success", "Ativo, concluído, em dia"),
    ("--warning", "Vence em breve, atenção"),
    ("--danger", "Defeito, vencido, erro"),
    ("--info", "Informativo, neutro"),
    ("--system-indigo", "Categoria, agrupador"),
    ("--system-purple", "Categoria, agrupador"),
]

_CORES_GRAFICO = [(f"--chart-{i}", f"Série {i}") for i in range(1, 9)]

_ESCALA_TIPO = [
    ("--text-3xl", "36px", "Número de KPI"),
    ("--text-2xl", "28px", "Título de página"),
    ("--text-xl", "22px", "Título de seção"),
    ("--text-lg", "17px", "Título de card"),
    ("--text-md", "15px", "Corpo em leitura longa"),
    ("--text-base", "14px", "Corpo, botão, campo"),
    ("--text-sm", "13px", "Célula de tabela"),
    ("--text-xs", "12px", "Legenda, meta"),
    ("--text-2xs", "11px", "Rótulo de coluna, chip"),
]

_RAIOS = [
    ("--radius-xs", "5px"),
    ("--radius-sm", "8px"),
    ("--radius-md", "11px"),
    ("--radius-lg", "14px"),
    ("--radius-xl", "18px"),
    ("--radius-2xl", "24px"),
]

_SOMBRAS = [
    ("--shadow-xs", "Repouso: card, botão"),
    ("--shadow-sm", "Leve destaque"),
    ("--shadow-md", "Card sob o cursor"),
    ("--shadow-lg", "Menu, popover"),
    ("--shadow-xl", "Modal, aviso flutuante"),
]

_ESPACOS = [
    ("--space-1", "4px"),
    ("--space-2", "8px"),
    ("--space-3", "12px"),
    ("--space-4", "16px"),
    ("--space-5", "20px"),
    ("--space-6", "24px"),
    ("--space-8", "32px"),
    ("--space-10", "40px"),
    ("--space-12", "48px"),
]


@staff_member_required
def design_system(request):
    """Catálogo de tokens e componentes do design system v5."""
    return render(request, "front/sistema/design_system.html", {
        "grupos_cor": [
            ("Superfícies", "As camadas de profundidade: canvas → card → popover.",
             _CORES_SUPERFICIE),
            ("Texto", "Quatro níveis. Mais de quatro e a hierarquia deixa de ser lida.",
             _CORES_TEXTO),
            ("Ação", "Azul único do sistema. Era um por módulo — quatro no total.",
             _CORES_ACAO),
            ("Estado", "Cor com significado. Não usar por decoração.", _CORES_ESTADO),
            ("Séries de gráfico", "Ordem pensada para o 1º ao 3º serem distinguíveis "
                                  "também em daltonismo.", _CORES_GRAFICO),
        ],
        "escala_tipo": _ESCALA_TIPO,
        "raios": _RAIOS,
        "sombras": _SOMBRAS,
        "espacos": _ESPACOS,
    })
