# -*- coding: utf-8 -*-
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, HRFlowable, KeepTogether
)
from reportlab.platypus.tableofcontents import TableOfContents
from reportlab.lib.colors import HexColor
from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate
from datetime import date

# ── Paleta ────────────────────────────────────────────────────────────────────
NAVY   = HexColor("#0F2044")
BLUE   = HexColor("#1D4ED8")
LBLUE  = HexColor("#3B82F6")
TEAL   = HexColor("#0891B2")
GREEN  = HexColor("#166534")
RED    = HexColor("#991B1B")
AMBER  = HexColor("#92400E")
GRAY   = HexColor("#374151")
LGRAY  = HexColor("#6B7280")
LLGRAY = HexColor("#F3F4F6")
WHITE  = colors.white
BLACK  = colors.black

ROW_A  = HexColor("#EFF6FF")
ROW_B  = HexColor("#FFFFFF")
HDR_BG = NAVY

W = A4[0] - 4*cm   # usable width

# ── Estilos ───────────────────────────────────────────────────────────────────
styles = getSampleStyleSheet()

def S(name, **kw):
    return ParagraphStyle(name, **kw)

sTitle   = S("sTitle",   fontSize=28, textColor=WHITE,  fontName="Helvetica-Bold",
             alignment=TA_CENTER, leading=34)
sSub     = S("sSub",     fontSize=14, textColor=HexColor("#BFDBFE"), fontName="Helvetica",
             alignment=TA_CENTER, leading=18)
sMeta    = S("sMeta",    fontSize=10, textColor=HexColor("#93C5FD"), fontName="Helvetica",
             alignment=TA_CENTER, leading=14)

sH1      = S("sH1",      fontSize=16, textColor=WHITE,  fontName="Helvetica-Bold",
             leading=20, spaceBefore=6, spaceAfter=4)
sH2      = S("sH2",      fontSize=13, textColor=NAVY,   fontName="Helvetica-Bold",
             leading=16, spaceBefore=10, spaceAfter=3,
             borderPad=4, backColor=HexColor("#DBEAFE"), borderColor=BLUE,
             borderWidth=0, leftIndent=0)
sH3      = S("sH3",      fontSize=11, textColor=BLUE,   fontName="Helvetica-Bold",
             leading=14, spaceBefore=7, spaceAfter=2)
sH4      = S("sH4",      fontSize=10, textColor=TEAL,   fontName="Helvetica-Bold",
             leading=13, spaceBefore=5, spaceAfter=1)
sBody    = S("sBody",    fontSize=9,  textColor=GRAY,   fontName="Helvetica",
             leading=13, spaceBefore=2, spaceAfter=2, alignment=TA_JUSTIFY)
sBullet  = S("sBullet",  fontSize=9,  textColor=GRAY,   fontName="Helvetica",
             leading=13, spaceBefore=1, spaceAfter=1, leftIndent=14, bulletIndent=4)
sCode    = S("sCode",    fontSize=8,  textColor=HexColor("#1E3A5F"), fontName="Courier",
             leading=11, spaceBefore=1, spaceAfter=1, leftIndent=10,
             backColor=HexColor("#F0F9FF"))
sTOC1    = S("sTOC1",    fontSize=10, textColor=NAVY,   fontName="Helvetica-Bold",
             leading=14, leftIndent=0)
sTOC2    = S("sTOC2",    fontSize=9,  textColor=GRAY,   fontName="Helvetica",
             leading=12, leftIndent=12)

def sp(h=0.2): return Spacer(1, h*cm)
def hr():      return HRFlowable(width="100%", thickness=0.5, color=HexColor("#CBD5E1"), spaceAfter=4)

# ── Tabela utilitária ─────────────────────────────────────────────────────────
def make_table(headers, rows, col_widths=None, hdr_color=NAVY):
    data = [[Paragraph(f"<b>{h}</b>", S("th", fontSize=9, textColor=WHITE,
             fontName="Helvetica-Bold", leading=11, alignment=TA_CENTER))
             for h in headers]]
    for ri, row in enumerate(rows):
        data.append([Paragraph(str(c), S(f"td{ri}", fontSize=8.5, textColor=GRAY,
                     fontName="Helvetica", leading=11)) for c in row])

    if col_widths is None:
        col_widths = [W / len(headers)] * len(headers)

    ts = TableStyle([
        ("BACKGROUND",  (0,0), (-1,0), hdr_color),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),[ROW_A, ROW_B]),
        ("GRID",        (0,0), (-1,-1), 0.3, HexColor("#CBD5E1")),
        ("VALIGN",      (0,0), (-1,-1), "TOP"),
        ("TOPPADDING",  (0,0), (-1,-1), 4),
        ("BOTTOMPADDING",(0,0),(-1,-1), 4),
        ("LEFTPADDING", (0,0), (-1,-1), 5),
        ("RIGHTPADDING",(0,0),(-1,-1), 5),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),[ROW_A, ROW_B]),
    ])
    t = Table(data, colWidths=col_widths, repeatRows=1)
    t.setStyle(ts)
    return t

# ── Caixa de seção colorida ───────────────────────────────────────────────────
def section_box(title, color=NAVY):
    data = [[Paragraph(title, sH1)]]
    t = Table(data, colWidths=[W])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0,0),(-1,-1), color),
        ("TOPPADDING", (0,0),(-1,-1), 8),
        ("BOTTOMPADDING",(0,0),(-1,-1), 8),
        ("LEFTPADDING",(0,0),(-1,-1), 12),
        ("RIGHTPADDING",(0,0),(-1,-1), 12),
        ("ROUNDEDCORNERS",[4]),
    ]))
    return t

def h2box(title):
    data = [[Paragraph(f"  {title}", S("h2b", fontSize=12, textColor=NAVY,
             fontName="Helvetica-Bold", leading=15))]]
    t = Table(data, colWidths=[W])
    t.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,-1), HexColor("#DBEAFE")),
        ("TOPPADDING",(0,0),(-1,-1), 6),
        ("BOTTOMPADDING",(0,0),(-1,-1), 6),
        ("LEFTPADDING",(0,0),(-1,-1), 8),
        ("LINEAFTER",(0,0),(0,-1), 3, BLUE),
    ]))
    return t

def bullet(text): return Paragraph(f"• {text}", sBullet)
def code(text):   return Paragraph(text, sCode)
def body(text):   return Paragraph(text, sBody)
def h3(text):     return Paragraph(text, sH3)
def h4(text):     return Paragraph(text, sH4)

# ══════════════════════════════════════════════════════════════════════════════
#  DOCUMENTO
# ══════════════════════════════════════════════════════════════════════════════
OUTPUT = r"C:\Users\kayque.silva\OneDrive - SANTA COLOMBA AGROPECUARIA LTDA\Área de Trabalho\Projeto Estoque\Projeto\docs\Arquitetura_Sistema_Estoque.pdf"

doc = SimpleDocTemplate(
    OUTPUT,
    pagesize=A4,
    leftMargin=2*cm, rightMargin=2*cm,
    topMargin=2.2*cm, bottomMargin=2*cm,
    title="Arquitetura do Sistema de Controle de Ativos",
    author="Santa Colomba Agropecuária",
)

story = []

# ════════════════════════════════════════════════════════════════════════════
#  CAPA
# ════════════════════════════════════════════════════════════════════════════
capa_data = [[Paragraph(
    "<b>ARQUITETURA COMPLETA DO SISTEMA</b><br/>"
    "Controle de Ativos — Projeto Estoque",
    sTitle)]]
capa = Table(capa_data, colWidths=[W])
capa.setStyle(TableStyle([
    ("BACKGROUND",(0,0),(-1,-1), NAVY),
    ("TOPPADDING",(0,0),(-1,-1), 40),
    ("BOTTOMPADDING",(0,0),(-1,-1), 40),
    ("LEFTPADDING",(0,0),(-1,-1), 20),
    ("RIGHTPADDING",(0,0),(-1,-1), 20),
]))
story.append(capa)
story.append(sp(0.6))

meta_data = [[Paragraph(
    f"Santa Colomba Agropecuária Ltda.<br/>"
    f"Departamento de Tecnologia da Informação<br/>"
    f"Data: {date.today().strftime('%d/%m/%Y')}  |  Versão: 1.0  |  Ambiente: Django + SQLite",
    sMeta)]]
meta = Table(meta_data, colWidths=[W])
meta.setStyle(TableStyle([
    ("BACKGROUND",(0,0),(-1,-1), HexColor("#1E3A5F")),
    ("TOPPADDING",(0,0),(-1,-1), 14),
    ("BOTTOMPADDING",(0,0),(-1,-1), 14),
]))
story.append(meta)
story.append(sp(0.8))

kpi_items = [
    ("19", "Módulos de\nViews"),
    ("24", "Modelos de\nDados"),
    ("98", "Rotas\nHTTP"),
    ("80+", "Templates\nHTML"),
    ("7",  "Serviços de\nNegócio"),
    ("33", "Forms\nDjango"),
]
kpi_data = [[
    Paragraph(f"<b><font size='18' color='#3B82F6'>{n}</font></b><br/>"
              f"<font size='8' color='#9CA3AF'>{lbl}</font>",
              S("kpi", fontSize=9, alignment=TA_CENTER, leading=14))
    for n, lbl in kpi_items
]]
kpi_t = Table(kpi_data, colWidths=[W/6]*6)
kpi_t.setStyle(TableStyle([
    ("BACKGROUND",(0,0),(-1,-1), HexColor("#0F172A")),
    ("TOPPADDING",(0,0),(-1,-1), 12),
    ("BOTTOMPADDING",(0,0),(-1,-1), 12),
    ("INNERGRID",(0,0),(-1,-1), 0.3, HexColor("#1E293B")),
    ("BOX",(0,0),(-1,-1), 0.3, HexColor("#1E293B")),
]))
story.append(kpi_t)
story.append(PageBreak())

# ════════════════════════════════════════════════════════════════════════════
#  1. VISÃO GERAL
# ════════════════════════════════════════════════════════════════════════════
story.append(section_box("1. VISÃO GERAL DO SISTEMA", NAVY))
story.append(sp(0.3))
story.append(body(
    "O <b>Sistema de Controle de Ativos</b> é uma aplicação web desenvolvida em <b>Django 4.x / Python 3.14</b> "
    "para gestão completa do patrimônio tecnológico da Santa Colomba Agropecuária Ltda. "
    "Cobre equipamentos físicos, licenças de software, usuários, movimentações, manutenção preventiva, "
    "locações, análise de inteligência e geração de relatórios."))
story.append(sp(0.3))

story.append(h2box("1.1 Stack Tecnológica"))
story.append(sp(0.15))
story.append(make_table(
    ["Camada", "Tecnologia", "Versão / Detalhe"],
    [
        ("Framework Web",    "Django",         "4.x"),
        ("Linguagem",        "Python",         "3.14"),
        ("Banco de Dados",   "SQLite3",        "db.sqlite3 (arquivo local)"),
        ("Frontend",         "HTML5 + CSS Grid/Flex", "Templates Django + Font Awesome 6.4"),
        ("PDF (relatórios)", "xhtml2pdf / ReportLab",  "Geração server-side"),
        ("Excel",            "openpyxl",       "Exportação e importação"),
        ("Word (termos)",    "python-docx",    "Geração de termos de entrega/devolução"),
        ("E-mail",           "SMTP Outlook",   "smtp.outlook.com:587 TLS"),
        ("Admin",            "Django Admin",   "Personalizado com AuditAdminMixin"),
    ],
    col_widths=[4.5*cm, 5*cm, 7.5*cm]
))
story.append(sp(0.3))

story.append(h2box("1.2 Aplicativos Django (INSTALLED_APPS)"))
story.append(sp(0.15))
story.append(make_table(
    ["App", "Descrição"],
    [
        ("ProjetoEstoque",              "App principal — modelos, views, forms, URLs, admin"),
        ("about",                       "Módulo auxiliar sobre a plataforma"),
        ("users",                       "Gestão de autenticação Django (User)"),
        ("widget_tweaks",               "Filtros de template para customização de widgets"),
        ("django.contrib.humanize",     "Filtros de humanização (números, datas)"),
        ("django.contrib.admin",        "Interface administrativa"),
        ("django.contrib.auth",         "Autenticação nativa"),
        ("django.contrib.messages",     "Sistema de mensagens flash"),
        ("django.contrib.staticfiles",  "Arquivos estáticos"),
    ],
    col_widths=[5.5*cm, 11.5*cm]
))
story.append(PageBreak())

# ════════════════════════════════════════════════════════════════════════════
#  2. ESTRUTURA DE PASTAS
# ════════════════════════════════════════════════════════════════════════════
story.append(section_box("2. ESTRUTURA DE PASTAS E ARQUIVOS", NAVY))
story.append(sp(0.3))
story.append(body("Hierarquia completa do projeto a partir da pasta raiz <b>controle/</b>:"))
story.append(sp(0.15))

tree = [
    ("controle/", "Raiz do projeto Django", "PASTA"),
    ("├── manage.py", "Ponto de entrada CLI do Django", "ARQUIVO"),
    ("├── db.sqlite3", "Banco de dados SQLite", "BANCO"),
    ("├── controle/", "Pacote de configuração do projeto", "PASTA"),
    ("│   ├── settings.py", "Configurações globais (DB, apps, email, auth)", "CONFIG"),
    ("│   ├── urls.py", "URL raiz — inclui ProjetoEstoque.urls", "CONFIG"),
    ("│   ├── wsgi.py", "Ponto WSGI para produção", "CONFIG"),
    ("│   └── asgi.py", "Ponto ASGI para produção", "CONFIG"),
    ("├── ProjetoEstoque/", "App principal do sistema", "PASTA"),
    ("│   ├── models.py", "24 modelos de dados + choices", "MODELO"),
    ("│   ├── forms.py", "33 formulários Django", "FORM"),
    ("│   ├── urls.py", "98 rotas HTTP organizadas por domínio", "URL"),
    ("│   ├── admin.py", "Configuração do Admin Django", "ADMIN"),
    ("│   ├── apps.py", "Configuração do App", "CONFIG"),
    ("│   ├── views/", "Pacote de views — 19 módulos por domínio", "PASTA"),
    ("│   │   ├── __init__.py", "Re-exporta todas as funções dos módulos", "VIEW"),
    ("│   │   ├── categorias.py", "CRUD de categorias (4 funções)", "VIEW"),
    ("│   │   ├── subtipos.py", "CRUD + detalhe de subtipos (5 funções)", "VIEW"),
    ("│   │   ├── usuarios.py", "Gestão completa de usuários (10 funções)", "VIEW"),
    ("│   │   ├── fornecedores.py", "CRUD + PDF de fornecedores (6 funções)", "VIEW"),
    ("│   │   ├── localidades.py", "CRUD + detalhe de localidades (5 funções)", "VIEW"),
    ("│   │   ├── centrocusto.py", "CRUD + detalhe + PDF de centros de custo (6 funções)", "VIEW"),
    ("│   │   ├── funcoes.py", "CRUD de funções (3 funções)", "VIEW"),
    ("│   │   ├── equipamentos.py", "Gestão de itens — maior módulo (15+ funções)", "VIEW"),
    ("│   │   ├── locacoes.py", "CRUD de locações (4 funções)", "VIEW"),
    ("│   │   ├── comentarios.py", "CRUD de comentários (4 funções)", "VIEW"),
    ("│   │   ├── movimentacoes.py", "CRUD + export + API de movimentações (7 funções)", "VIEW"),
    ("│   │   ├── ciclos.py", "CRUD de ciclos de manutenção (4 funções)", "VIEW"),
    ("│   │   ├── preventivas.py", "Preventivas + checklists + perguntas (9 funções)", "VIEW"),
    ("│   │   ├── licencas.py", "Licenças, lotes, movimentações (8 funções)", "VIEW"),
    ("│   │   ├── dashboards.py", "Dashboard principal + custos CC (4 funções)", "VIEW"),
    ("│   │   ├── relatorios.py", "Toner, equipamentos, avisos, exportações (7 funções)", "VIEW"),
    ("│   │   ├── termos.py", "Geração de termos Word (2 funções)", "VIEW"),
    ("│   │   ├── inteligencia.py", "Sistema de inteligência + busca global (4 funções)", "VIEW"),
    ("│   │   └── home.py", "Página sobre a plataforma (1 função)", "VIEW"),
    ("│   ├── templates/", "80+ templates HTML organizados por domínio", "PASTA"),
    ("│   │   ├── base.html", "Layout mestre — nav, header, CSS global", "TEMPLATE"),
    ("│   │   ├── login.html", "Tela de autenticação", "TEMPLATE"),
    ("│   │   └── front/", "Todos os templates de telas do sistema", "PASTA"),
    ("│   ├── management/", "Comandos CLI personalizados", "PASTA"),
    ("│   │   └── commands/", "", "PASTA"),
    ("│   │       ├── enviar_alertas.py", "Envia e-mails de alerta de vencimento", "CMD"),
    ("│   │       └── importar_itens_planilha.py", "Importa itens via CLI de planilha", "CMD"),
    ("│   └── migrations/", "Histórico de migrações do banco de dados", "PASTA"),
    ("├── services/", "Camada de serviços — lógica de negócio isolada", "PASTA"),
    ("│   ├── importador_planilha.py", "Importação de equipamentos de Excel", "SERVICE"),
    ("│   ├── item_create_service.py", "Criação atômica de itens", "SERVICE"),
    ("│   ├── movimentacao_service.py", "Registro atômico de movimentações", "SERVICE"),
    ("│   ├── sistema_inteligencia_service.py", "Análise e detecção de problemas", "SERVICE"),
    ("│   ├── sistema_noticias_service.py", "KPIs, feed de atividades, painéis", "SERVICE"),
    ("│   ├── termos.py", "Geração de termos Word (entrega/devolução)", "SERVICE"),
    ("│   └── usuario_import_service.py", "Importação de usuários de Excel RH", "SERVICE"),
    ("├── docs_templates/", "Templates Word para geração de documentos", "PASTA"),
    ("│   └── termos/", "Termos de entrega e devolução (.docx)", "PASTA"),
    ("├── about/", "App auxiliar Django", "PASTA"),
    ("├── users/", "App de autenticação Django", "PASTA"),
    ("└── media/", "Uploads de arquivos (fotos, PDFs de termos)", "PASTA"),
]

color_map = {
    "PASTA": HexColor("#1E3A5F"), "ARQUIVO": HexColor("#065F46"),
    "BANCO": HexColor("#7F1D1D"), "CONFIG": HexColor("#713F12"),
    "MODELO": HexColor("#312E81"), "FORM": HexColor("#4C1D95"),
    "URL": HexColor("#064E3B"), "ADMIN": HexColor("#78350F"),
    "VIEW": HexColor("#0C4A6E"), "TEMPLATE": HexColor("#134E4A"),
    "CMD": HexColor("#1F2937"), "SERVICE": HexColor("#3B0764"),
}

tree_data = []
for path, desc, tipo in tree:
    bg = color_map.get(tipo, GRAY)
    tree_data.append([
        Paragraph(f"<font name='Courier' size='8'>{path}</font>",
                  S("tc", fontSize=8, fontName="Courier", textColor=GRAY, leading=10)),
        Paragraph(desc, S("td", fontSize=8, fontName="Helvetica", textColor=GRAY, leading=10)),
        Paragraph(f"<b>{tipo}</b>",
                  S("tt", fontSize=7.5, fontName="Helvetica-Bold", textColor=WHITE,
                    alignment=TA_CENTER, leading=9)),
    ])

tree_hdr = [[
    Paragraph("<b>Caminho</b>", S("th", fontSize=9, textColor=WHITE, fontName="Helvetica-Bold",
              leading=11, alignment=TA_CENTER)),
    Paragraph("<b>Descrição</b>", S("th2", fontSize=9, textColor=WHITE, fontName="Helvetica-Bold",
              leading=11, alignment=TA_CENTER)),
    Paragraph("<b>Tipo</b>", S("th3", fontSize=9, textColor=WHITE, fontName="Helvetica-Bold",
              leading=11, alignment=TA_CENTER)),
]]

full_tree = tree_hdr + tree_data
tree_ts = TableStyle([
    ("BACKGROUND",(0,0),(-1,0), NAVY),
    ("ROWBACKGROUNDS",(0,1),(-1,-1),[ROW_A, ROW_B]),
    ("GRID",(0,0),(-1,-1), 0.3, HexColor("#CBD5E1")),
    ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
    ("TOPPADDING",(0,0),(-1,-1), 3),
    ("BOTTOMPADDING",(0,0),(-1,-1), 3),
    ("LEFTPADDING",(0,0),(-1,-1), 4),
    ("RIGHTPADDING",(0,0),(-1,-1), 4),
])
# Color tipo column per type
for ri, (_, _, tipo) in enumerate(tree):
    bg = color_map.get(tipo, GRAY)
    tree_ts.add("BACKGROUND", (2, ri+1), (2, ri+1), bg)

tree_t = Table(full_tree, colWidths=[7.5*cm, 7.5*cm, 2*cm], repeatRows=1)
tree_t.setStyle(tree_ts)
story.append(tree_t)
story.append(PageBreak())

# ════════════════════════════════════════════════════════════════════════════
#  3. BANCO DE DADOS — MODELOS
# ════════════════════════════════════════════════════════════════════════════
story.append(section_box("3. BANCO DE DADOS — MODELOS E RELACIONAMENTOS", NAVY))
story.append(sp(0.2))
story.append(body(
    "O sistema usa <b>SQLite3</b> (arquivo db.sqlite3). Todos os modelos herdam de "
    "<b>AuditModel</b> que adiciona rastreamento automático de criação e atualização. "
    "Abaixo estão todos os 24 modelos com seus campos, tipos e relacionamentos."))
story.append(sp(0.2))

story.append(h2box("3.1 AuditModel — Modelo Base Abstrato"))
story.append(sp(0.1))
story.append(body("Todos os modelos herdam desta classe. Fornece auditoria automática:"))
story.append(make_table(
    ["Campo", "Tipo", "Descrição"],
    [
        ("criado_por",    "ForeignKey(User)",   "Usuário que criou o registro — auto-preenchido"),
        ("atualizado_por","ForeignKey(User)",   "Usuário que atualizou — auto-preenchido"),
        ("created_at",    "DateTimeField",      "Data/hora de criação — auto_now_add=True"),
        ("updated_at",    "DateTimeField",      "Data/hora de atualização — auto_now=True"),
    ],
    col_widths=[3.5*cm, 4*cm, 9.5*cm]
))
story.append(sp(0.25))

# Modelo Categoria
story.append(h2box("3.2 Entidades Base (Cadastros)"))
story.append(sp(0.1))

modelos_base = [
    ("Categoria", [
        ("nome", "CharField(100)", "Nome da categoria de equipamento"),
    ], "Agrupa subtipos de equipamentos. Ex: Informática, Periférico."),
    ("Subtipo", [
        ("nome",      "CharField(100)",     "Nome do subtipo"),
        ("alocado",   "SimNaoChoices",      "Indica se pode ser alocado a usuário"),
        ("categoria", "FK → Categoria",     "Categoria pai (CASCADE delete)"),
    ], "Classifica equipamentos dentro de uma categoria. Ex: Notebook, Impressora."),
    ("Localidade", [
        ("codigo", "LocalidadeChoices",  "Código da localidade (Karitel, Rio do Meio, etc.)"),
        ("local",  "CharField(100)",     "Nome descritivo do local"),
    ], "Representa os estabelecimentos físicos da empresa."),
    ("Fornecedor", [
        ("nome",     "CharField(100)", "Razão social do fornecedor"),
        ("cnpj",     "CharField(18)",  "CNPJ formatado"),
        ("contrato", "TextField",      "Texto livre com dados do contrato"),
    ], "Cadastro de fornecedores de equipamentos e licenças."),
    ("CentroCusto", [
        ("numero",      "CharField(10)",   "Número do centro de custo"),
        ("departamento","CharField(100)",  "Nome do departamento"),
        ("pmb",         "SimNaoChoices",   "Pertence ao Projeto Mudança Brasil?"),
    ], "Centros de custo para rateio de despesas. Usado em todos os ativos."),
    ("Funcao", [
        ("nome", "CharField(100)", "Nome do cargo/função"),
    ], "Cargos dos colaboradores. Usado no cadastro de usuários."),
]

for nome_modelo, campos, descricao in modelos_base:
    story.append(h3(f"Modelo: {nome_modelo}"))
    story.append(body(descricao))
    story.append(make_table(
        ["Campo", "Tipo / Relação", "Descrição"],
        campos,
        col_widths=[3.5*cm, 4*cm, 9.5*cm]
    ))
    story.append(sp(0.2))

story.append(PageBreak())
story.append(h2box("3.3 Gestão de Usuários"))
story.append(sp(0.1))
story.append(h3("Modelo: Usuario"))
story.append(body("Colaboradores da empresa. Vinculado a centro de custo, localidade e função. "
                  "Controla quem tem ativos e licenças ativos."))
story.append(make_table(
    ["Campo", "Tipo", "Restrição / Detalhe"],
    [
        ("matricula",     "CharField(30)",          "unique=True — identificador RH (opcional)"),
        ("nome",          "CharField(100)",          "Nome completo"),
        ("status",        "StatusUsuarioChoices",    "ATIVO | DESLIGADO"),
        ("data_inicio",   "DateField",               "Data de admissão (default: hoje)"),
        ("data_termino",  "DateField",               "Data de desligamento (nullable)"),
        ("pmb",           "SimNaoChoices",           "Pertence ao PMB?"),
        ("email",         "EmailField",              "E-mail corporativo (nullable)"),
        ("centro_custo",  "FK → CentroCusto",        "Centro de custo do colaborador (SET_NULL)"),
        ("localidade",    "FK → Localidade",         "Localidade/filial (SET_NULL)"),
        ("funcao",        "FK → Funcao",             "Cargo (SET_NULL)"),
    ],
    col_widths=[3.5*cm, 4*cm, 9.5*cm]
))
story.append(body("<b>Índices:</b> matricula, nome, status"))
story.append(sp(0.3))

story.append(h2box("3.4 Equipamentos e Itens"))
story.append(sp(0.1))
story.append(h3("Modelo: Item (principal)"))
story.append(body("Modelo central do sistema. Representa qualquer ativo físico ou consumível. "
                  "Possui validações complexas (não pode ser consumível E locado simultaneamente)."))
story.append(make_table(
    ["Campo", "Tipo", "Detalhe"],
    [
        ("nome",                  "CharField(100)",       "Nome do equipamento/item"),
        ("numero_serie",          "CharField(100)",       "Número de série (nullable)"),
        ("marca",                 "CharField(100)",       "Fabricante"),
        ("modelo",                "CharField(100)",       "Modelo do equipamento"),
        ("status",                "StatusItemChoices",    "ATIVO | BACKUP | MANUTENCAO | DEFEITO | PAUSADO | DESCARTE"),
        ("quantidade",            "PositiveIntegerField", "Quantidade em estoque (default=1)"),
        ("valor",                 "DecimalField(10,2)",   "Valor unitário de compra (nullable)"),
        ("data_compra",           "DateField",            "Data de aquisição (nullable)"),
        ("numero_pedido",         "CharField(100)",       "NF / pedido de compra (nullable)"),
        ("item_consumo",          "SimNaoChoices",        "É item de consumo com controle de lote?"),
        ("tem_lote",              "BooleanField",         "Possui controle por lote? (default=False)"),
        ("locado",                "SimNaoChoices",        "É equipamento locado (alugado)?"),
        ("pmb",                   "SimNaoChoices",        "Pertence ao PMB?"),
        ("precisa_preventiva",    "SimNaoChoices",        "Requer manutenção preventiva?"),
        ("data_limite_preventiva","IntegerField",         "Intervalo em dias para preventiva"),
        ("observacoes",           "TextField",            "Observações gerais"),
        ("subtipo",               "FK → Subtipo",         "Classificação do item (SET_NULL)"),
        ("categoria",             "FK → Categoria",       "Categoria (SET_NULL)"),
        ("localidade",            "FK → Localidade",      "Localização atual (SET_NULL)"),
        ("centro_custo",          "FK → CentroCusto",     "Centro de custo responsável (SET_NULL)"),
        ("fornecedor",            "FK → Fornecedor",      "Fornecedor principal (SET_NULL)"),
    ],
    col_widths=[4.5*cm, 4*cm, 8.5*cm]
))
story.append(body("<b>Índices:</b> nome, status, item_consumo, localidade, centro_custo"))
story.append(sp(0.2))

story.append(h3("Modelo: Locacao"))
story.append(body("Informações de locação vinculadas a um Item (OneToOne). Só existe se Item.locado=SIM."))
story.append(make_table(
    ["Campo", "Tipo", "Detalhe"],
    [
        ("equipamento",   "OneToOne → Item",    "Item locado (CASCADE delete)"),
        ("tempo_locado",  "IntegerField",        "Duração do contrato em meses"),
        ("valor_mensal",  "DecimalField(10,2)",  "Valor mensal do aluguel"),
        ("data_entrada",  "DateField",           "Data de início da locação"),
        ("contrato",      "CharField(200)",      "Número/referência do contrato"),
        ("observacoes",   "TextField",           "Observações do contrato"),
        ("fornecedor",    "FK → Fornecedor",     "Fornecedor/locador (SET_NULL)"),
    ],
    col_widths=[3.5*cm, 4*cm, 9.5*cm]
))
story.append(sp(0.2))

story.append(h3("Modelo: LoteEstoque"))
story.append(body("Lote de entrada de estoque para itens consumíveis. Controla NF, custo e fornecedor."))
story.append(make_table(
    ["Campo", "Tipo", "Detalhe"],
    [
        ("fornecedor",       "FK → Fornecedor",    "Fornecedor do lote (PROTECT)"),
        ("data_entrada",     "DateField",           "Data de recebimento"),
        ("numero_nf",        "CharField(60)",       "Número da nota fiscal"),
        ("quantidade",       "PositiveIntegerField","Quantidade recebida (>= 1)"),
        ("custo_unitario",   "DecimalField(12,2)",  "Custo por unidade (>= 0.01)"),
        ("observacao_tecnica","TextField",          "Notas técnicas"),
    ],
    col_widths=[3.5*cm, 4*cm, 9.5*cm]
))
story.append(body("<b>Índices:</b> numero_nf, data_entrada, fornecedor"))
story.append(sp(0.2))

story.append(h3("Modelo: ItemLote"))
story.append(body("Vínculo entre Item e LoteEstoque. Controla saldo disponível por item por lote."))
story.append(make_table(
    ["Campo", "Tipo", "Detalhe"],
    [
        ("item",                 "FK → Item",          "Item associado (PROTECT)"),
        ("lote",                 "FK → LoteEstoque",   "Lote associado (PROTECT)"),
        ("quantidade_entrada",   "PositiveIntegerField","Quantidade recebida neste vínculo"),
        ("quantidade_disponivel","PositiveIntegerField","Saldo atual (>= 0, <= quantidade_entrada)"),
        ("custo_unitario",       "DecimalField(12,2)",  "Custo congelado no momento da entrada"),
    ],
    col_widths=[3.5*cm, 4*cm, 9.5*cm]
))
story.append(sp(0.2))

story.append(h3("Modelo: Comentario"))
story.append(make_table(
    ["Campo", "Tipo", "Detalhe"],
    [
        ("texto", "TextField",    "Texto do comentário"),
        ("item",  "FK → Item",   "Item relacionado — CASCADE delete (nullable)"),
    ],
    col_widths=[3.5*cm, 4*cm, 9.5*cm]
))
story.append(sp(0.2))

story.append(PageBreak())
story.append(h2box("3.5 Movimentações"))
story.append(sp(0.1))
story.append(h3("Modelo: MovimentacaoItem"))
story.append(body("Registro de toda movimentação de equipamento: entradas, baixas, transferências, manutenções. "
                  "É o histórico completo do ciclo de vida de cada item."))
story.append(make_table(
    ["Campo", "Tipo", "Detalhe"],
    [
        ("tipo_movimentacao",    "TipoMovimentacaoChoices", "TRANSFERENCIA | BAIXA | ENTRADA | ENVIO_MANUTENCAO | RETORNO_MANUTENCAO | OUTROS"),
        ("tipo_transferencia",   "TipoTransferenciaChoices","ENTREGA | DEVOLUCAO (nullable)"),
        ("item",                 "FK → Item",               "Item movimentado (CASCADE)"),
        ("lote",                 "FK → LoteEstoque",        "Lote de origem (PROTECT, nullable)"),
        ("usuario",              "FK → Usuario",            "Usuário destinatário/solicitante (SET_NULL)"),
        ("quantidade",           "PositiveIntegerField",    "Quantidade movimentada (default=1)"),
        ("localidade_origem",    "FK → Localidade",         "Local de saída (SET_NULL)"),
        ("localidade_destino",   "FK → Localidade",         "Local de chegada (SET_NULL)"),
        ("centro_custo_origem",  "FK → CentroCusto",        "CC de saída (SET_NULL)"),
        ("centro_custo_destino", "FK → CentroCusto",        "CC de chegada (SET_NULL)"),
        ("fornecedor_manutencao","FK → Fornecedor",         "Fornecedor da manutenção (SET_NULL)"),
        ("status_retorno",       "StatusItemChoices",       "Status após retorno de manutenção"),
        ("status_transferencia", "StatusItemChoices",       "Novo status após transferência"),
        ("numero_pedido",        "CharField(100)",          "NF ou pedido de compra"),
        ("chamado",              "CharField(100)",          "Número do chamado de TI"),
        ("custo",                "DecimalField(12,2)",      "Custo da operação (default=0)"),
        ("observacao",           "TextField",               "Justificativa/observação"),
        ("termo_pdf",            "FileField",               "PDF do termo de responsabilidade"),
    ],
    col_widths=[4.5*cm, 4.5*cm, 8*cm]
))
story.append(sp(0.2))

story.append(h3("Modelo: CicloManutencao"))
story.append(body("Ciclo de manutenção corretiva de um item. Quando iniciado, item muda para MANUTENCAO."))
story.append(make_table(
    ["Campo", "Tipo", "Detalhe"],
    [
        ("item",           "FK → Item",           "Item em manutenção (CASCADE)"),
        ("status_inicial", "CharField(20)",        "Status do item quando entrou em manutenção"),
        ("data_inicio",    "DateField",            "Data de início (default: hoje)"),
        ("data_fim",       "DateField",            "Data de encerramento (nullable = em andamento)"),
        ("causa",          "TextField",            "Descrição do problema/causa"),
        ("custo",          "DecimalField(10,2)",   "Custo total da manutenção"),
    ],
    col_widths=[3.5*cm, 4*cm, 9.5*cm]
))
story.append(sp(0.2))

story.append(PageBreak())
story.append(h2box("3.6 Manutenção Preventiva"))
story.append(sp(0.1))

prev_modelos = [
    ("CheckListModelo", "Template de checklist reutilizável. Define o conjunto de perguntas e intervalo padrão.", [
        ("nome",          "CharField(120)",       "Nome do checklist"),
        ("ativo",         "SimNaoChoices",        "Checklist ativo para uso?"),
        ("subtipo",       "FK → Subtipo",         "Restrito a subtipo de equipamento (nullable)"),
        ("intervalo_dias","PositiveIntegerField",  "Intervalo padrão em dias (default=0)"),
    ]),
    ("CheckListPergunta", "Perguntas do checklist. Suporta texto, número, booleano e escolha única.", [
        ("checklist_modelo","FK → CheckListModelo","Checklist pai (CASCADE)"),
        ("texto_pergunta",  "CharField(255)",      "Texto da pergunta"),
        ("tipo_resposta",   "TipoRespostaChoices", "TEXTO | NUMERO | BOOLEANO | ESCOLHA"),
        ("obrigatorio",     "SimNaoChoices",       "Resposta obrigatória?"),
        ("opcoes",          "CharField(400)",      "Opções separadas por vírgula (para ESCOLHA)"),
        ("ordem",           "PositiveIntegerField","Ordem de exibição"),
    ]),
    ("Preventiva", "Preventiva agendada para um equipamento. Rastreia última e próxima execução.", [
        ("equipamento",    "FK → Item",            "Equipamento (CASCADE)"),
        ("checklist_modelo","FK → CheckListModelo","Checklist aplicado (nullable)"),
        ("data_ultima",    "DateField",            "Data da última execução (nullable)"),
        ("data_proxima",   "DateField",            "Data da próxima execução (nullable)"),
        ("dentro_do_prazo","BooleanField",         "Dentro do prazo? (default=True)"),
        ("observacao",     "TextField",            "Observações"),
        ("foto_antes",     "ImageField",           "Foto antes da manutenção (upload)"),
        ("foto_depois",    "ImageField",           "Foto depois da manutenção (upload)"),
    ]),
    ("PreventivaExecucao", "Histórico de cada execução de preventiva realizada.", [
        ("preventiva",     "FK → Preventiva",      "Preventiva executada (CASCADE)"),
        ("data_execucao",  "DateField",            "Data de execução"),
        ("observacao",     "TextField",            "Observações da execução"),
        ("foto_antes",     "ImageField",           "Foto antes"),
        ("foto_depois",    "ImageField",           "Foto depois"),
    ]),
    ("PreventivaResposta", "Resposta individual para cada pergunta em uma execução.", [
        ("preventiva", "FK → Preventiva",        "Preventiva (CASCADE)"),
        ("execucao",   "FK → PreventivaExecucao","Execução específica (CASCADE)"),
        ("pergunta",   "FK → CheckListPergunta", "Pergunta respondida (CASCADE)"),
        ("resposta",   "TextField",              "Valor da resposta"),
    ]),
]

for nome, desc, campos in prev_modelos:
    story.append(h3(f"Modelo: {nome}"))
    story.append(body(desc))
    story.append(make_table(
        ["Campo", "Tipo", "Detalhe"],
        campos,
        col_widths=[3.5*cm, 4*cm, 9.5*cm]
    ))
    story.append(sp(0.2))

story.append(PageBreak())
story.append(h2box("3.7 Licenças de Software"))
story.append(sp(0.1))

lic_modelos = [
    ("Licenca", "Licença de software. Pode ter múltiplos lotes de compra.", [
        ("nome",        "CharField(160)",  "Nome da licença (ex: Microsoft 365)"),
        ("fornecedor",  "FK → Fornecedor", "Fornecedor da licença (nullable)"),
        ("pmb",         "SimNaoChoices",   "É licença PMB?"),
        ("centro_custo","FK → CentroCusto","CC proprietário — licença retorna aqui na devolução"),
        ("observacao",  "TextField",       "Observações"),
    ]),
    ("LicencaLote", "Lote de compra de uma licença. Controla estoque de assentos disponíveis.", [
        ("licenca",               "FK → Licenca",    "Licença pai (CASCADE)"),
        ("quantidade_total",      "PositiveIntegerField","Total de assentos comprados"),
        ("quantidade_disponivel", "PositiveIntegerField","Assentos disponíveis (auto-inicializado)"),
        ("custo_ciclo",           "DecimalField(12,2)","Custo total do lote por ciclo"),
        ("periodicidade",         "PeriodicidadeChoices","MENSAL | SEMESTRAL | ANUAL | TRIENAL | CONTRATO"),
        ("data_compra",           "DateField",        "Data de compra (nullable)"),
        ("numero_pedido",         "CharField(50)",    "NF / pedido"),
        ("fornecedor",            "FK → Fornecedor",  "Fornecedor do lote (nullable)"),
        ("centro_custo",          "FK → CentroCusto", "CC deste lote (nullable)"),
        ("observacao",            "TextField",        "Observações"),
    ]),
    ("MovimentacaoLicenca", "Atribuição ou devolução de assento de licença a um usuário.", [
        ("tipo",                "TipoMovLicencaChoices","ATRIBUICAO | DEVOLUCAO"),
        ("licenca",             "FK → Licenca",         "Licença (CASCADE)"),
        ("usuario",             "FK → Usuario",         "Usuário que recebe/devolve (nullable)"),
        ("lote",                "FK → LicencaLote",     "Lote de origem (nullable)"),
        ("centro_custo_destino","FK → CentroCusto",     "CC cobrado pela licença (nullable)"),
        ("valor_unitario",      "DecimalField(12,2)",   "Custo unitário congelado no momento"),
        ("observacao",          "TextField",            "Observações"),
    ]),
]

for nome, desc, campos in lic_modelos:
    story.append(h3(f"Modelo: {nome}"))
    story.append(body(desc))
    story.append(make_table(
        ["Campo", "Tipo", "Detalhe"],
        campos,
        col_widths=[3.5*cm, 4*cm, 9.5*cm]
    ))
    story.append(sp(0.2))

story.append(PageBreak())

# ════════════════════════════════════════════════════════════════════════════
#  4. DIAGRAMA DE RELACIONAMENTOS (tabular)
# ════════════════════════════════════════════════════════════════════════════
story.append(section_box("4. MAPA DE RELACIONAMENTOS ENTRE MODELOS", NAVY))
story.append(sp(0.2))
story.append(make_table(
    ["Modelo Origem", "Tipo", "Modelo Destino", "Comportamento", "Campo"],
    [
        ("Subtipo",             "N→1",    "Categoria",          "CASCADE",   "categoria"),
        ("Item",                "N→1",    "Subtipo",            "SET_NULL",  "subtipo"),
        ("Item",                "N→1",    "Categoria",          "SET_NULL",  "categoria"),
        ("Item",                "N→1",    "Localidade",         "SET_NULL",  "localidade"),
        ("Item",                "N→1",    "CentroCusto",        "SET_NULL",  "centro_custo"),
        ("Item",                "N→1",    "Fornecedor",         "SET_NULL",  "fornecedor"),
        ("Locacao",             "1→1",    "Item",               "CASCADE",   "equipamento"),
        ("Locacao",             "N→1",    "Fornecedor",         "SET_NULL",  "fornecedor"),
        ("LoteEstoque",         "N→1",    "Fornecedor",         "PROTECT",   "fornecedor"),
        ("ItemLote",            "N→1",    "Item",               "PROTECT",   "item"),
        ("ItemLote",            "N→1",    "LoteEstoque",        "PROTECT",   "lote"),
        ("Comentario",          "N→1",    "Item",               "CASCADE",   "item"),
        ("CicloManutencao",     "N→1",    "Item",               "CASCADE",   "item"),
        ("MovimentacaoItem",    "N→1",    "Item",               "CASCADE",   "item"),
        ("MovimentacaoItem",    "N→1",    "LoteEstoque",        "PROTECT",   "lote"),
        ("MovimentacaoItem",    "N→1",    "Usuario",            "SET_NULL",  "usuario"),
        ("MovimentacaoItem",    "N→1",    "Localidade (ori)",   "SET_NULL",  "localidade_origem"),
        ("MovimentacaoItem",    "N→1",    "Localidade (dest)",  "SET_NULL",  "localidade_destino"),
        ("MovimentacaoItem",    "N→1",    "CentroCusto (ori)",  "SET_NULL",  "centro_custo_origem"),
        ("MovimentacaoItem",    "N→1",    "CentroCusto (dest)", "SET_NULL",  "centro_custo_destino"),
        ("MovimentacaoItem",    "N→1",    "Fornecedor",         "SET_NULL",  "fornecedor_manutencao"),
        ("CheckListModelo",     "N→1",    "Subtipo",            "SET_NULL",  "subtipo"),
        ("CheckListPergunta",   "N→1",    "CheckListModelo",    "CASCADE",   "checklist_modelo"),
        ("Preventiva",          "N→1",    "Item",               "CASCADE",   "equipamento"),
        ("Preventiva",          "N→1",    "CheckListModelo",    "SET_NULL",  "checklist_modelo"),
        ("PreventivaExecucao",  "N→1",    "Preventiva",         "CASCADE",   "preventiva"),
        ("PreventivaResposta",  "N→1",    "Preventiva",         "CASCADE",   "preventiva"),
        ("PreventivaResposta",  "N→1",    "PreventivaExecucao", "CASCADE",   "execucao"),
        ("PreventivaResposta",  "N→1",    "CheckListPergunta",  "CASCADE",   "pergunta"),
        ("Usuario",             "N→1",    "CentroCusto",        "SET_NULL",  "centro_custo"),
        ("Usuario",             "N→1",    "Localidade",         "SET_NULL",  "localidade"),
        ("Usuario",             "N→1",    "Funcao",             "SET_NULL",  "funcao"),
        ("Licenca",             "N→1",    "Fornecedor",         "SET_NULL",  "fornecedor"),
        ("Licenca",             "N→1",    "CentroCusto",        "SET_NULL",  "centro_custo"),
        ("LicencaLote",         "N→1",    "Licenca",            "CASCADE",   "licenca"),
        ("LicencaLote",         "N→1",    "Fornecedor",         "SET_NULL",  "fornecedor"),
        ("LicencaLote",         "N→1",    "CentroCusto",        "SET_NULL",  "centro_custo"),
        ("MovimentacaoLicenca", "N→1",    "Licenca",            "CASCADE",   "licenca"),
        ("MovimentacaoLicenca", "N→1",    "Usuario",            "SET_NULL",  "usuario"),
        ("MovimentacaoLicenca", "N→1",    "LicencaLote",        "SET_NULL",  "lote"),
        ("MovimentacaoLicenca", "N→1",    "CentroCusto",        "SET_NULL",  "centro_custo_destino"),
    ],
    col_widths=[4*cm, 1.5*cm, 4*cm, 2.5*cm, 5*cm]
))
story.append(PageBreak())

# ════════════════════════════════════════════════════════════════════════════
#  5. ROTAS HTTP
# ════════════════════════════════════════════════════════════════════════════
story.append(section_box("5. ROTAS HTTP — URLs COMPLETAS", NAVY))
story.append(sp(0.2))

grupos_urls = [
    ("Home & Autenticação", TEAL, [
        ("GET",  "/",                                   "dashboard",              "Painel principal do sistema"),
        ("GET",  "/dashboard/",                         "dashboard",              "Painel principal (alias)"),
        ("GET",  "/sobre/",                             "sobre_plataforma",       "Informações da plataforma"),
        ("GET",  "/login/",                             "LoginView (Django)",     "Tela de autenticação"),
    ]),
    ("Categorias", NAVY, [
        ("GET",  "/categorias/",                        "categorias_list",        "Lista todas as categorias"),
        ("GET/POST","/categorias/novo/",                "categoria_create",       "Cria nova categoria"),
        ("GET/POST","/categorias/<pk>/editar/",         "categoria_update",       "Edita categoria existente"),
        ("POST", "/categorias/<pk>/excluir/",           "categoria_delete",       "Remove categoria"),
    ]),
    ("Subtipos", NAVY, [
        ("GET",  "/subtipos/",                          "subtipo_list",           "Lista subtipos com filtros"),
        ("GET/POST","/subtipos/novo/",                  "subtipo_create",         "Cria subtipo"),
        ("GET",  "/subtipos/<pk>/",                     "subtipo_detail",         "Detalhe do subtipo"),
        ("GET/POST","/subtipos/<pk>/editar/",           "subtipo_update",         "Edita subtipo"),
        ("POST", "/subtipos/<pk>/excluir/",             "subtipo_delete",         "Remove subtipo"),
    ]),
    ("Funções / Localidades / Centros de Custo / Fornecedores", HexColor("#1E3A5F"), [
        ("GET",  "/funcoes/",                           "funcao_list",            "Lista funções"),
        ("GET/POST","/funcoes/novo/",                   "funcao_form",            "Cria/edita função"),
        ("POST", "/funcoes/<pk>/excluir/",              "funcao_delete",          "Remove função (@require_POST)"),
        ("GET",  "/localidades/",                       "localidade_list",        "Lista localidades"),
        ("GET",  "/localidades/<pk>/",                  "localidade_detail",      "Detalhe da localidade"),
        ("GET",  "/centros-custo/",                     "centrocusto_list",       "Lista centros de custo"),
        ("GET",  "/centros-custo/pdf/",                 "centrocusto_export_pdf", "Exporta PDF de CC"),
        ("GET",  "/centros-custo/<pk>/",                "centrocusto_detail",     "Detalhe do CC"),
        ("GET",  "/fornecedores/",                      "fornecedor_list",        "Lista fornecedores"),
        ("GET",  "/fornecedores/pdf/",                  "fornecedor_export_pdf",  "Exporta PDF de fornecedores"),
        ("GET",  "/fornecedores/<pk>/",                 "fornecedor_detail",      "Detalhe do fornecedor"),
    ]),
    ("Usuários", GREEN, [
        ("GET",  "/usuarios/",                          "usuario_list",           "Lista usuários com filtros"),
        ("GET/POST","/usuarios/cadastrar/",             "usuario_create",         "Cadastra novo usuário"),
        ("POST", "/usuarios/importar/",                 "usuario_importar",       "Importa usuários de Excel RH"),
        ("GET",  "/usuarios/dashboard/",                "usuario_dashboard",      "Dashboard KPIs de usuários"),
        ("GET",  "/usuarios/<pk>/",                     "usuario_detail",         "Detalhe completo do usuário"),
        ("GET/POST","/usuarios/<pk>/editar/",           "usuario_update",         "Edita dados do usuário"),
        ("POST", "/usuarios/<pk>/excluir/",             "usuario_delete",         "Remove usuário (@require_POST)"),
        ("POST", "/usuarios/<pk>/desligar/",            "usuario_desligar",       "Marca usuário como desligado"),
        ("POST", "/usuarios/<pk>/remover-todas-licencas/","usuario_remover_todas_licencas","Remove todas as licenças"),
    ]),
    ("Equipamentos / Itens", BLUE, [
        ("GET",  "/equipamentos/",                      "equipamentos_list",      "Lista equipamentos com filtros avançados"),
        ("GET/POST","/equipamentos/cadastrar/",         "item_create",            "Cadastra equipamento (suporta locação e lote)"),
        ("POST", "/equipamentos/importar/",             "importar_planilha",      "Importa equipamentos de Excel"),
        ("GET",  "/equipamentos/exportar/",             "equipamentos_exportar",  "Exporta lista para Excel"),
        ("GET",  "/equipamentos/<pk>/",                 "equipamento_detalhe",    "Detalhe completo do equipamento"),
        ("GET/POST","/equipamentos/<pk>/editar/",       "item_update",            "Edita equipamento"),
        ("POST", "/equipamentos/<pk>/excluir/",         "equipamento_excluir",    "Remove equipamento"),
        ("GET/POST","/equipamentos/<pk>/termo/entrega/","termo_entrega_form",     "Gera termo de entrega Word"),
        ("GET/POST","/equipamentos/<pk>/termo/devolucao/","termo_devolucao_form", "Gera termo de devolução Word"),
    ]),
    ("Locações / Comentários / Ciclos", HexColor("#374151"), [
        ("GET",  "/locacoes/",                          "locacoes_list",          "Lista contratos de locação"),
        ("GET/POST","/locacoes/novo/",                  "locacao_create",         "Cria contrato de locação"),
        ("GET",  "/comentarios/",                       "comentarios_list",       "Lista comentários"),
        ("GET/POST","/comentarios/novo/",               "comentario_create",      "Cria comentário"),
        ("GET",  "/ciclos/",                            "ciclos_list",            "Lista ciclos de manutenção"),
        ("GET/POST","/ciclos/novo/",                    "ciclo_create",           "Cria ciclo de manutenção corretiva"),
    ]),
    ("Movimentações", HexColor("#064E3B"), [
        ("GET",  "/movimentacoes/",                     "movimentacao_list",      "Lista movimentações com filtros"),
        ("GET/POST","/movimentacoes/nova/",             "movimentacao_create",    "Registra movimentação"),
        ("GET",  "/movimentacoes/pdf/",                 "movimentacao_export_pdf","Exporta PDF de movimentações"),
        ("GET",  "/movimentacoes/api/lotes-por-item/",  "api_lotes_por_item",     "API JSON: lotes do item"),
        ("GET",  "/movimentacoes/<pk>/",                "movimentacao_detail",    "Detalhe da movimentação"),
    ]),
    ("Preventivas", HexColor("#4C1D95"), [
        ("GET",  "/preventiva/",                        "preventiva_list",        "Lista preventivas e status"),
        ("GET/POST","/preventiva/iniciar/",             "preventiva_start",       "Inicia nova preventiva"),
        ("GET/POST","/preventiva/item/<id>/iniciar/",   "preventiva_start",       "Inicia preventiva para item específico"),
        ("GET",  "/preventiva/<pk>/",                   "preventiva_detail",      "Detalhe da preventiva"),
        ("GET/POST","/preventiva/<pk>/executar/",       "preventiva_exec",        "Executa preventiva com checklist"),
        ("GET",  "/preventiva/checklists/",             "checklist_list",         "Lista modelos de checklist"),
        ("GET/POST","/preventiva/checklists/novo/",     "checklist_form",         "Cria checklist"),
        ("POST", "/preventiva/checklists/<pk>/excluir/","checklist_delete",       "Remove checklist (@require_POST)"),
        ("GET/POST","/preventiva/checklists/<cpk>/pergunta/novo/","pergunta_form","Adiciona pergunta ao checklist"),
        ("POST", "/preventiva/checklists/<cpk>/pergunta/<pk>/excluir/","pergunta_delete","Remove pergunta"),
    ]),
    ("Licenças de Software", HexColor("#7F1D1D"), [
        ("GET",  "/licencas/",                          "licenca_list",           "Dashboard de licenças com KPIs"),
        ("GET/POST","/licencas/nova/",                  "licenca_form",           "Cria/edita licença"),
        ("GET",  "/licencas/<pk>/",                     "licenca_detail",         "Detalhe com lotes e movimentações"),
        ("GET",  "/licencas/<pk>/exportar-excel/",      "licenca_export_excel",   "Exporta dados da licença para Excel"),
        ("GET",  "/licencas/mov/",                      "mov_licenca_list",       "Lista movimentações de licença"),
        ("GET/POST","/licencas/mov/nova/",              "mov_licenca_form",       "Atribui/devolve licença"),
        ("GET",  "/licencas/lotes/",                    "licenca_lote_list",      "Lista lotes de licença"),
        ("GET/POST","/licencas/lotes/novo/",            "licenca_lote_form",      "Cria lote de licença"),
        ("POST", "/licencas/devolver-rapido/<uid>/<lid>/","licenca_devolver_rapido","Devolução rápida"),
    ]),
    ("Dashboards & Relatórios", HexColor("#134E4A"), [
        ("GET",  "/dashboards/custos-cc/",              "cc_custos_dashboard",    "Custo por centro de custo"),
        ("GET",  "/dashboards/custos-cc/pdf/",          "cc_custos_export_pdf",   "Exporta PDF de custos CC"),
        ("GET",  "/dashboards/custos-cc/exportar-excel/","custo_cc_export_excel", "Exporta Excel de custos CC"),
        ("GET",  "/dashboards/toner/",                  "toner_cc_dashboard",     "Consumo de toner por CC"),
        ("GET",  "/dashboards/toner/exportar-excel/",   "toner_cc_export_excel",  "Exporta toner para Excel"),
        ("GET",  "/dashboards/licencas/",               "licencas_dashboard",     "Dashboard de licenças e custos"),
        ("GET",  "/dashboards/preventivas/",            "preventiva_dashboard",   "Dashboard de preventivas"),
        ("GET",  "/avisos/contratos-a-vencer/",         "avisos_contratos_vencer","Contratos próximos do vencimento"),
        ("GET",  "/avisos/contratos-a-vencer/exportar-excel/","avisos_contratos_vencer_export_excel","Exporta avisos"),
    ]),
    ("Inteligência & Notícias", HexColor("#1F2937"), [
        ("GET",  "/inteligencia/",                      "sistema_inteligencia_dashboard","Análise de problemas do sistema"),
        ("GET",  "/inteligencia/busca-global/",         "sistema_inteligencia_busca_global","Busca global JSON"),
        ("GET",  "/inteligencia/exportar-csv/",         "sistema_inteligencia_export_csv","Exporta issues para CSV"),
        ("GET",  "/noticias/",                          "sistema_noticias",       "Feed de atividades e KPIs"),
    ]),
]

for grupo, cor, rotas in grupos_urls:
    story.append(h2box(grupo))
    story.append(sp(0.1))
    story.append(make_table(
        ["Método", "URL", "View Function", "Descrição"],
        rotas,
        col_widths=[1.8*cm, 6*cm, 5*cm, 4.2*cm],
        hdr_color=cor,
    ))
    story.append(sp(0.15))

story.append(PageBreak())

# ════════════════════════════════════════════════════════════════════════════
#  6. VIEWS — FUNÇÕES POR MÓDULO
# ════════════════════════════════════════════════════════════════════════════
story.append(section_box("6. VIEWS — FUNÇÕES POR MÓDULO", NAVY))
story.append(sp(0.2))

modulos_views = [
    ("categorias.py", TEAL, [
        ("categorias_list",    "Lista todas as categorias do banco"),
        ("categoria_create",   "Cria nova categoria via formulário POST"),
        ("categoria_update",   "Edita categoria existente (GET mostra form, POST salva)"),
        ("categoria_delete",   "Remove categoria após confirmação POST"),
    ]),
    ("subtipos.py", TEAL, [
        ("subtipo_list",   "Lista subtipos com filtro por nome e categoria; paginação"),
        ("subtipo_create", "Cria subtipo"),
        ("subtipo_detail", "Exibe detalhe do subtipo com itens vinculados"),
        ("subtipo_update", "Edita subtipo"),
        ("subtipo_delete", "Remove subtipo"),
    ]),
    ("usuarios.py", GREEN, [
        ("usuario_list",                 "Lista usuários com filtros (status, CC, localidade, nome); paginação HTMX"),
        ("usuario_detail",               "Detalhe do usuário: itens, licenças, histórico de movimentações"),
        ("usuario_create",               "Cadastra novo colaborador"),
        ("usuario_update",               "Edita dados do colaborador"),
        ("usuario_delete",               "Remove usuário (@require_POST)"),
        ("usuario_importar",             "Importa planilha Excel RH via UsuarioImportService"),
        ("usuario_desligar",             "Marca status=DESLIGADO e define data_termino (@require_POST)"),
        ("usuario_remover_todas_licencas","Devolve todas as licenças ativas do usuário atomicamente"),
        ("usuario_dashboard",            "KPIs de usuários: ativos, desligados, PMB, por CC"),
        ("licenca_devolver_rapido",      "Devolução rápida de licença específica de um usuário"),
    ]),
    ("fornecedores.py", NAVY, [
        ("fornecedor_list",       "Lista fornecedores com busca"),
        ("fornecedor_create",     "Cria fornecedor"),
        ("fornecedor_detail",     "Detalhe: itens e licenças vinculados"),
        ("fornecedor_update",     "Edita fornecedor"),
        ("fornecedor_delete",     "Remove fornecedor"),
        ("fornecedor_export_pdf", "Gera PDF da lista de fornecedores (xhtml2pdf)"),
    ]),
    ("equipamentos.py", BLUE, [
        ("equipamentos_list",    "Lista com filtros avançados (nome, subtipo, status, CC, local); HTMX partial"),
        ("item_create",          "Cria item suportando locação (LocacaoForm) e lote (LoteEstoqueCreateForm)"),
        ("equipamento_detalhe",  "Detalhe completo: movimentações, preventivas, lotes, financeiro, auditoria"),
        ("item_update",          "Edita item com lógica de LocacaoForm condicional"),
        ("equipamento_excluir",  "Remove item (@require_POST)"),
        ("importar_planilha",    "Importa equipamentos de Excel via ImportadorPlanilhaService (JSON response)"),
        ("equipamentos_exportar","Exporta lista filtrada para Excel (openpyxl)"),
    ]),
    ("movimentacoes.py", HexColor("#064E3B"), [
        ("movimentacao_list",       "Lista movimentações com filtros (tipo, item, usuário, período)"),
        ("movimentacao_create",     "Registra movimentação via MovimentacaoEstoqueService"),
        ("movimentacao_detail",     "Detalhe da movimentação com dados relacionados"),
        ("movimentacao_update",     "Edita movimentação"),
        ("movimentacao_delete",     "Remove movimentação"),
        ("movimentacao_export_pdf", "Exporta PDF de movimentações filtradas"),
        ("api_lotes_por_item",      "API JSON: retorna lotes disponíveis para um item (uso em formulário)"),
    ]),
    ("preventivas.py", HexColor("#4C1D95"), [
        ("preventiva_list",   "Lista preventivas com status (vencida/no prazo/sem data)"),
        ("preventiva_start",  "Inicia preventiva: seleciona item e checklist"),
        ("preventiva_detail", "Detalhe com histórico de execuções e respostas"),
        ("preventiva_exec",   "Executa preventiva: preenche respostas do checklist, salva fotos"),
        ("checklist_list",    "Lista modelos de checklist"),
        ("checklist_form",    "Cria/edita modelo de checklist"),
        ("checklist_delete",  "Remove checklist (@require_POST)"),
        ("pergunta_form",     "Adiciona/edita pergunta de checklist"),
        ("pergunta_delete",   "Remove pergunta"),
    ]),
    ("licencas.py", HexColor("#7F1D1D"), [
        ("licenca_list",          "Dashboard de licenças: saldo por lote, KPIs globais, filtros"),
        ("licenca_form",          "Cria/edita licença"),
        ("licenca_detail",        "Detalhe: lotes, movimentações, burn rate mensal/anual, por CC"),
        ("licenca_export_excel",  "Exporta dados da licença para Excel (openpyxl)"),
        ("mov_licenca_list",      "Lista movimentações de licença"),
        ("mov_licenca_form",      "Atribui/devolve licença via MovimentacaoLicencaForm"),
        ("licenca_lote_list",     "Lista lotes com saldo"),
        ("licenca_lote_form",     "Cria/edita lote de licença"),
    ]),
    ("dashboards.py", HexColor("#134E4A"), [
        ("dashboard",             "Painel principal: KPIs gerais via SistemaNoticiasService"),
        ("cc_custos_dashboard",   "Custo total por CC: itens, licenças, baixas, gráficos"),
        ("cc_custos_export_pdf",  "Exporta dashboard de custos para PDF"),
        ("preventiva_dashboard",  "Dashboard de preventivas: status, agenda crítica"),
    ]),
    ("relatorios.py", HexColor("#134E4A"), [
        ("toner_cc_dashboard",              "Dashboard de consumo de toner por CC e usuário"),
        ("toner_cc_export_excel",           "Exporta consumo de toner para Excel"),
        ("custo_cc_export_excel",           "Exporta custos CC para Excel (mesmos filtros do dashboard)"),
        ("equipamentos_exportar",           "Exporta lista de equipamentos para Excel"),
        ("licencas_dashboard",              "Dashboard de licenças com gráficos de evolução"),
        ("avisos_contratos_vencer",         "Lista contratos de locação próximos do vencimento"),
        ("avisos_contratos_vencer_export_excel","Exporta avisos de vencimento para Excel"),
    ]),
    ("termos.py", GRAY, [
        ("termo_entrega_form",   "Formulário + geração de termo de entrega Word (.docx)"),
        ("termo_devolucao_form", "Formulário + geração de termo de devolução Word (.docx)"),
    ]),
    ("inteligencia.py", GRAY, [
        ("sistema_inteligencia_dashboard",  "Análise: detecta duplicatas, divergências, pendências (filtros + HTMX)"),
        ("sistema_inteligencia_busca_global","Busca global JSON em todos os domínios"),
        ("sistema_inteligencia_export_csv", "Exporta relatório de problemas para CSV"),
        ("sistema_noticias",                "Feed de atividades, KPIs, painéis e notícias do sistema"),
    ]),
]

for modulo, cor, funcs in modulos_views:
    story.append(KeepTogether([
        h2box(modulo),
        sp(0.1),
        make_table(
            ["Função", "Responsabilidade"],
            funcs,
            col_widths=[5*cm, 12*cm],
            hdr_color=cor,
        ),
        sp(0.2),
    ]))

story.append(PageBreak())

# ════════════════════════════════════════════════════════════════════════════
#  7. CAMADA DE SERVIÇOS
# ════════════════════════════════════════════════════════════════════════════
story.append(section_box("7. CAMADA DE SERVIÇOS (services/)", NAVY))
story.append(sp(0.2))
story.append(body(
    "Os serviços encapsulam a lógica de negócio complexa, separando-a das views. "
    "Utilizam transações atômicas (transaction.atomic) para garantir consistência."))
story.append(sp(0.2))

servicos = [
    ("ImportadorPlanilhaService", "importador_planilha.py", HexColor("#1E3A5F"),
     "Importa equipamentos/itens de planilha Excel. Suporta 3 tipos de item: normal, locado e consumível com lote.",
     [
         ("executar()",                  "Processa todas as linhas da planilha; retorna criados/atualizados/erros"),
         ("_processar_linha(row)",       "Roteia cada linha para o fluxo correto (normal/locação/consumo)"),
         ("_criar_ou_atualizar_lote_consumo()", "Cria/atualiza LoteEstoque para item consumível"),
         ("_criar_ou_atualizar_item_lote()",    "Vincula ItemLote ao LoteEstoque"),
         ("_criar_ou_atualizar_locacao()",      "Cria/atualiza registro de Locacao"),
     ]),
    ("ItemCreateService", "item_create_service.py", HexColor("#064E3B"),
     "Criação atômica de Item com suporte a locação (Locacao) e controle de lote (LoteEstoque + ItemLote).",
     [
         ("criar_item(item_form, locacao_form, lote_form, user)", "Cria atomicamente Item + dependentes"),
         ("preencher_auditoria(obj, user, criando)",              "Define criado_por / atualizado_por"),
     ]),
    ("MovimentacaoEstoqueService", "movimentacao_service.py", HexColor("#4C1D95"),
     "Registro atômico de movimentações. Atualiza saldos de ItemLote, status e localidade do Item.",
     [
         ("registrar(form, user)",           "Ponto de entrada — roteia para handler específico"),
         ("_registrar_entrada()",            "Cria LoteEstoque + ItemLote; atualiza Item.quantidade e fornecedor"),
         ("_registrar_baixa()",              "Debita ItemLote.quantidade_disponivel; valida saldo suficiente"),
         ("_registrar_movimentacao_padrao()","Atualiza status/localidade/CC do Item conforme tipo de movimento"),
     ]),
    ("SistemaInteligenciaService", "sistema_inteligencia_service.py", HexColor("#7F1D1D"),
     "Análise completa do sistema: detecta 8 categorias de problemas com severidade e gera KPIs.",
     [
         ("build_report(filters)",            "Relatório completo com issues filtradas e KPIs"),
         ("detect_cadastro_duplicates()",     "Duplicatas: usuários, itens, fornecedores, licenças"),
         ("detect_usuario_issues()",          "Pendências de usuários: sem matrícula, desligados com ativos"),
         ("detect_item_issues()",             "Problemas em itens: conflitos consumível/locado, sem série"),
         ("detect_lote_issues()",             "Divergências em lotes: saldo > entrada, NF duplicadas"),
         ("detect_movimentacao_issues()",     "Movimentações inválidas: sem lote, sem usuário em entrega"),
         ("detect_licenca_issues()",          "Licenças: atribuições sem valor, desligados com licença ativa"),
         ("detect_preventiva_issues()",       "Preventivas vencidas, item requer preventiva mas não tem"),
         ("global_search(q)",                 "Busca full-text em todos os domínios; retorna JSON"),
     ]),
    ("SistemaNoticiasService", "sistema_noticias_service.py", HexColor("#0C4A6E"),
     "Constrói o painel de notícias e dashboard principal com KPIs, slides, feed e painéis.",
     [
         ("build()",                  "Ponto de entrada — retorna contexto completo para template"),
         ("build_kpis()",             "KPIs: usuários, itens, licenças, preventivas, custos mensais"),
         ("build_hero_slides()",      "4 slides rotativos: preventivas vencidas, manutenções, etc."),
         ("build_ticker()",           "6 itens do feed rotativo de atividades recentes"),
         ("build_news_feed()",        "Timeline com 18 eventos recentes (movs, licenças, preventivas)"),
         ("build_panels()",           "Painéis: distribuição de status, top subtipos, agenda crítica"),
         ("get_active_items_by_user()","Snapshot: qual usuário tem qual item (última movimentação)"),
         ("get_active_licenses_by_user()","Snapshot: usuário × licença × lote ativo"),
     ]),
    ("TermosService", "termos.py", HexColor("#374151"),
     "Gera documentos Word (.docx) de termos de entrega e devolução a partir de templates.",
     [
         ("gerar_termo_docx(item, tipo, form_data)", "Gera o .docx e retorna (BytesIO, nome_arquivo)"),
         ("_build_dados()",                          "Extrai dados do item e formulário para substituição"),
         ("_fill_intro()",                           "Preenche o preâmbulo com nome do colaborador"),
         ("_fill_main_table()",                      "Preenche tabela de equipamento: série, acessórios, local"),
         ("_fill_signatures()",                      "Adiciona linhas de assinatura com datas"),
         ("_ajustar_layout_documento()",             "Formata margens, alinhamento e espaçamento"),
     ]),
    ("UsuarioImportService", "usuario_import_service.py", HexColor("#065F46"),
     "Importa colaboradores de planilha Excel do RH. Cria automaticamente Funcao, CentroCusto e Localidade.",
     [
         ("executar()",              "Processa todas as abas conforme modo_importacao selecionado"),
         ("_processar_aba(sheet)",   "Processa cada linha da aba; usa fuzzy matching por matrícula/nome"),
         ("_criar_ou_atualizar(row)","Cria ou atualiza Usuario; aplica regra PMB automática"),
         ("_desligar_ausentes()",    "Desliga usuários ativos que não apareceram na planilha"),
         ("_normalizar_cols(headers)","Mapeia nomes de colunas com aliases flexíveis"),
     ]),
]

for nome, arquivo, cor, descricao, metodos in servicos:
    story.append(KeepTogether([
        h2box(f"{nome}  ·  {arquivo}"),
        sp(0.1),
        body(descricao),
        make_table(
            ["Método / Função", "Responsabilidade"],
            metodos,
            col_widths=[6*cm, 11*cm],
            hdr_color=cor,
        ),
        sp(0.25),
    ]))

story.append(PageBreak())

# ════════════════════════════════════════════════════════════════════════════
#  8. TEMPLATES
# ════════════════════════════════════════════════════════════════════════════
story.append(section_box("8. TEMPLATES HTML", NAVY))
story.append(sp(0.2))
story.append(body(
    "Todos os templates ficam em <b>ProjetoEstoque/templates/</b>. "
    "Herdam de <b>base.html</b> via <code>{% extends 'base.html' %}</code>. "
    "Templates com prefixo _ são partials (componentes reutilizáveis sem layout próprio)."))
story.append(sp(0.2))

templates_grupos = [
    ("Raiz / Base", [
        ("base.html",             "Layout mestre: nav lateral, header, CSS global, mensagens flash"),
        ("login.html",            "Tela de login Django"),
    ]),
    ("front/home.html", [
        ("home.html",             "Painel principal (dashboard) — usa SistemaNoticiasService"),
        ("sobre_plataforma.html", "Informações da plataforma, versão, totais de registros"),
    ]),
    ("front/categorias/", [
        ("categoria_list.html",           "Lista categorias com botão de criar e ações"),
        ("categoria_form.html",           "Formulário de criação/edição"),
        ("categoria_confirm_delete.html", "Confirmação de exclusão"),
    ]),
    ("front/ (subtipos, localidades — raiz front)", [
        ("subtipo_list.html",           "Lista subtipos com filtros"),
        ("subtipo_detail.html",         "Detalhe do subtipo e itens vinculados"),
        ("subtipo_form.html",           "Formulário de subtipo"),
        ("subtipo_confirm_delete.html", "Confirmação de exclusão"),
        ("localidade_list.html",        "Lista localidades"),
        ("localidade_detail.html",      "Detalhe de localidade"),
        ("localidade_form.html",        "Formulário de localidade"),
        ("localidade_confirm_delete.html","Confirmação de exclusão"),
    ]),
    ("front/centrocusto/", [
        ("centrocusto_list.html",   "Lista CC com KPIs de custo"),
        ("centrocusto_detail.html", "Detalhe do CC com itens e licenças"),
        ("centrocusto_form.html",   "Formulário de CC"),
        ("centrocusto_pdf.html",    "Layout para exportação PDF de CCs"),
    ]),
    ("front/fornecedores/", [
        ("fornecedor_list.html",   "Lista fornecedores"),
        ("fornecedor_detail.html", "Detalhe com itens e licenças do fornecedor"),
        ("fornecedor_form.html",   "Formulário de fornecedor"),
        ("fornecedor_pdf.html",    "Layout para PDF de fornecedores"),
    ]),
    ("front/funcoes/", [
        ("funcao_list.html", "Lista funções com busca e ações inline"),
        ("funcao_form.html", "Formulário de função"),
    ]),
    ("front/usuarios/", [
        ("usuario_list.html",        "Lista com filtros avançados, HTMX partial reload"),
        ("_usuario_table.html",      "Partial: tabela de usuários (HTMX target)"),
        ("_usuario_kpis.html",       "Partial: cards de KPIs de usuários"),
        ("_usuario_pagination.html", "Partial: paginação de usuários"),
        ("usuario_detail.html",      "Detalhe completo: itens, licenças, histórico"),
        ("usuario_form.html",        "Formulário de cadastro/edição com select2"),
        ("usuario_importar.html",    "Upload de planilha Excel RH"),
        ("usuario_dashboard.html",   "Dashboard analítico de usuários"),
    ]),
    ("front/equipamentos/", [
        ("equipamentos_list.html",    "Lista com filtros avançados e HTMX"),
        ("_tbody.html",               "Partial: corpo da tabela de equipamentos"),
        ("_kpis.html",                "Partial: KPIs de equipamentos"),
        ("_pagination.html",          "Partial: paginação"),
        ("equipamento_detalhe.html",  "Detalhe completo: ficha técnica, movimentos, preventivas, lotes, financeiro"),
        ("cadastrar_equipamento.html","Formulário de criação com campos dinâmicos (JS)"),
        ("editar_equipamento.html",   "Formulário de edição"),
        ("equipamento_delete.html",   "Confirmação de exclusão"),
        ("termo_form.html",           "Formulário para gerar termo Word"),
    ]),
    ("front/locacoes/ | front/comentarios/ | front/ciclos/", [
        ("locacao_list.html",             "Lista contratos de locação"),
        ("locacao_form.html",             "Formulário de locação"),
        ("locacao_confirm_delete.html",   "Confirmação de exclusão de locação"),
        ("comentario_list.html",          "Lista comentários por item"),
        ("comentario_form.html",          "Formulário de comentário"),
        ("comentario_confirm_delete.html","Confirmação de exclusão"),
        ("ciclo_list.html",               "Lista ciclos de manutenção corretiva"),
        ("ciclo_form.html",               "Formulário de ciclo (início/encerramento)"),
        ("ciclo_confirm_delete.html",     "Confirmação de exclusão"),
    ]),
    ("front/ (movimentações — raiz front)", [
        ("movimentacao_list.html",   "Lista movimentações com filtros"),
        ("movimentacao_form.html",   "Formulário complexo com campos dinâmicos por tipo"),
        ("movimentacao_detail.html", "Detalhe da movimentação"),
        ("movimentacao_pdf.html",    "Layout para exportação PDF"),
    ]),
    ("front/preventivas/", [
        ("preventiva_list.html",       "Lista preventivas com status (vencida/no prazo/sem data)"),
        ("preventiva_list_print.html", "Layout de impressão da lista"),
        ("preventiva_detail.html",     "Detalhe com histórico de execuções"),
        ("preventiva_exec.html",       "Formulário de execução do checklist"),
        ("preventiva_start.html",      "Formulário de início de preventiva"),
        ("preventiva_print.html",      "Layout para impressão da preventiva"),
        ("checklist_list.html",        "Lista modelos de checklist"),
        ("checklist_form.html",        "Formulário de checklist"),
        ("pergunta_form.html",         "Formulário de pergunta do checklist"),
    ]),
    ("front/licencas/", [
        ("licenca_list.html",      "Dashboard de licenças com KPIs e saldos"),
        ("licenca_detail.html",    "Detalhe: lotes, movimentações, burn rate, por CC"),
        ("licenca_form.html",      "Formulário de licença"),
        ("licenca_pdf.html",       "Layout PDF de licença"),
        ("licenca_atribuir.html",  "Formulário de atribuição rápida"),
        ("licenca_lote_list.html", "Lista lotes de licença com saldo"),
        ("licenca_lote_form.html", "Formulário de lote"),
        ("mov_licenca_list.html",  "Lista movimentações de licença"),
        ("mov_licenca_form.html",  "Formulário de atribuição/devolução"),
    ]),
    ("front/dashboards/", [
        ("dashboard.html",               "Painel principal com KPIs, slides, feed"),
        ("dashboard_toner.html",         "Dashboard de consumo de toner"),
        ("cc_custos_dashboard.html",     "Custos por CC com gráficos"),
        ("cc_custos_pdf.html",           "Layout PDF de custos CC"),
        ("licencas_dashboard.html",      "Dashboard de licenças e evolução"),
        ("preventiva_dashboard.html",    "Dashboard de preventivas"),
        ("avisos_contrato_vencer.html",  "Avisos de vencimento de contratos"),
    ]),
    ("front/inteligencia/", [
        ("sistema_inteligencia.html",          "Dashboard de inteligência com filtros"),
        ("_sistema_inteligencia_issues.html",  "Partial: tabela de problemas detectados (HTMX)"),
        ("_sistema_inteligencia_kpis.html",    "Partial: KPIs de severidade (HTMX)"),
    ]),
    ("front/noticias/", [
        ("sistema_noticias.html", "Feed completo: KPIs, slides, ticker, atividades, painéis"),
    ]),
]

for grupo, tmpls in templates_grupos:
    story.append(h3(grupo))
    story.append(make_table(
        ["Template", "Função"],
        tmpls,
        col_widths=[6.5*cm, 10.5*cm],
    ))
    story.append(sp(0.15))

story.append(PageBreak())

# ════════════════════════════════════════════════════════════════════════════
#  9. REGRAS DE NEGÓCIO
# ════════════════════════════════════════════════════════════════════════════
story.append(section_box("9. REGRAS DE NEGÓCIO PRINCIPAIS", NAVY))
story.append(sp(0.2))

regras = [
    ("Classificação de Itens", BLUE, [
        "Item NÃO pode ser simultaneamente consumível (item_consumo=SIM) E locado (locado=SIM).",
        "Item consumível: tem_lote=True, sem data_compra/numero_pedido no modelo Item (esses dados ficam no LoteEstoque).",
        "Item locado: sem data_compra/numero_pedido no Item; exige registro em Locacao (OneToOne).",
        "Item normal: todos os campos de compra no próprio Item.",
        "Preventiva só pode ser ativada se data_limite_preventiva > 0 dias.",
    ]),
    ("Controle de Estoque (Lotes)", TEAL, [
        "ItemLote.quantidade_disponivel SEMPRE deve ser ≤ ItemLote.quantidade_entrada.",
        "Movimentação BAIXA: debita ItemLote.quantidade_disponivel E Item.quantidade.",
        "Movimentação ENTRADA: cria novo LoteEstoque + ItemLote; incrementa Item.quantidade.",
        "Custo da baixa = ItemLote.custo_unitario × quantidade (congelado no momento da entrada).",
        "Lote só pode ter quantidade reduzida no lote se disponível >= novo mínimo (validação no LicencaLoteForm).",
    ]),
    ("Ciclo de Vida de Movimentações", HexColor("#4C1D95"), [
        "ENVIO_MANUTENCAO → Item.status = MANUTENCAO; Item.quantidade -= 1.",
        "RETORNO_MANUTENCAO → Item.status = BACKUP; Item.quantidade += 1.",
        "TRANSFERENCIA (entrega) → Item.status = ATIVO se era BACKUP; atualiza localidade/CC.",
        "TRANSFERENCIA (devolucao) → Item.status = BACKUP se era ATIVO; atualiza localidade/CC.",
        "TRANSFERENCIA_EQUIPAMENTO → aplica status_transferencia se informado.",
        "BAIXA → Item.quantidade -= quantidade; ItemLote.quantidade_disponivel -= quantidade.",
    ]),
    ("Licenças de Software", HexColor("#7F1D1D"), [
        "ATRIBUICAO: valida que usuário não tem licença ativa; debita LicencaLote.quantidade_disponivel.",
        "ATRIBUICAO: valor_unitario congelado = custo_ciclo / qtd_total / meses(periodicidade).",
        "DEVOLUCAO: incrementa LicencaLote.quantidade_disponivel; retorna CC ao CC da Licenca.",
        "Sistema detecta desligados com licenças ativas como governança crítica.",
        "Burn rate mensal = soma de (custo_mensal_unitario × assentos em uso) por CC.",
    ]),
    ("Usuários e PMB", GREEN, [
        "Usuário em CC com 'TABACO' no nome → pmb=SIM automaticamente na importação.",
        "E-mail gerado automaticamente se ausente: primeironome.ultimonome@santacolomba.com.",
        "Desligar usuário: status=DESLIGADO + data_termino=hoje. Itens/licenças devem ser devolvidos manualmente.",
        "usuario_remover_todas_licencas: devolve todas as licenças ativas atomicamente.",
        "Fuzzy matching na importação: aceita nome com 92%+ de similaridade para atualizar registro existente.",
    ]),
    ("Auditoria Automática", GRAY, [
        "AuditModel: criado_por/atualizado_por/created_at/updated_at em todos os modelos.",
        "Service layer e Admin preenchem automaticamente esses campos com request.user.",
        "Admin usa AuditAdminMixin: campos de auditoria são readonly, auto-preenchidos ao salvar.",
        "Histórico de movimentações rastreia quem fez o quê e quando em cada item.",
    ]),
    ("Termos de Responsabilidade", HexColor("#374151"), [
        "Gerado em Word (.docx) via python-docx a partir de template em docs_templates/termos/.",
        "Termo de Entrega: colaborador assina que recebe o equipamento em boas condições.",
        "Termo de Devolução: colaborador devolve; TI recebe de volta.",
        "Campos substituídos: nome, matrícula, equipamento, série, acessórios, local, data, responsável TI.",
    ]),
]

for titulo, cor, itens in regras:
    story.append(h2box(titulo))
    story.append(sp(0.1))
    for item in itens:
        story.append(bullet(item))
    story.append(sp(0.15))

story.append(PageBreak())

# ════════════════════════════════════════════════════════════════════════════
#  10. CONFIGURAÇÃO DO SISTEMA
# ════════════════════════════════════════════════════════════════════════════
story.append(section_box("10. CONFIGURAÇÃO DO SISTEMA (settings.py)", NAVY))
story.append(sp(0.2))

story.append(h2box("10.1 Banco de Dados"))
story.append(make_table(
    ["Parâmetro", "Valor"],
    [
        ("ENGINE",   "django.db.backends.sqlite3"),
        ("NAME",     "BASE_DIR / 'db.sqlite3'"),
        ("Tipo",     "SQLite3 — arquivo local, sem servidor externo"),
        ("Localização", "controle/db.sqlite3"),
    ],
    col_widths=[5*cm, 12*cm]
))
story.append(sp(0.2))

story.append(h2box("10.2 Configurações de Autenticação"))
story.append(make_table(
    ["Parâmetro", "Valor"],
    [
        ("LOGIN_URL",           "/login/"),
        ("LOGIN_REDIRECT_URL",  "home (dashboard)"),
        ("LOGOUT_REDIRECT_URL", "login"),
        ("Proteção de views",   "@login_required em todas as views exceto login"),
    ],
    col_widths=[5*cm, 12*cm]
))
story.append(sp(0.2))

story.append(h2box("10.3 E-mail (Alertas)"))
story.append(make_table(
    ["Parâmetro", "Valor"],
    [
        ("EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend"),
        ("EMAIL_HOST",    "smtp.outlook.com (via env EMAIL_HOST)"),
        ("EMAIL_PORT",    "587 (via env EMAIL_PORT)"),
        ("EMAIL_USE_TLS", "True"),
        ("EMAIL_HOST_USER","datasul@santacolomba.com.br"),
        ("ALERTA_EMAIL",  "ti@santacolomba.com.br"),
    ],
    col_widths=[5*cm, 12*cm]
))
story.append(sp(0.2))

story.append(h2box("10.4 Internacionalização"))
story.append(make_table(
    ["Parâmetro", "Valor"],
    [
        ("LANGUAGE_CODE", "pt-br"),
        ("TIME_ZONE",     "America/Sao_Paulo"),
        ("USE_I18N",      "True"),
        ("USE_TZ",        "True"),
    ],
    col_widths=[5*cm, 12*cm]
))
story.append(sp(0.2))

story.append(h2box("10.5 Middleware"))
story.append(make_table(
    ["Middleware", "Função"],
    [
        ("SecurityMiddleware",     "Headers de segurança HTTP"),
        ("SessionMiddleware",      "Gerenciamento de sessões"),
        ("CommonMiddleware",       "Redirecionamentos e trailing slash"),
        ("CsrfViewMiddleware",     "Proteção CSRF em formulários POST"),
        ("AuthenticationMiddleware","Popula request.user"),
        ("MessageMiddleware",      "Sistema de mensagens flash"),
        ("XFrameOptionsMiddleware","Proteção contra clickjacking"),
    ],
    col_widths=[6*cm, 11*cm]
))
story.append(sp(0.2))

story.append(h2box("10.6 Comandos CLI Personalizados"))
story.append(make_table(
    ["Comando", "Arquivo", "Função"],
    [
        ("python manage.py enviar_alertas",        "management/commands/enviar_alertas.py",
         "Envia e-mails de alerta para itens com contratos a vencer"),
        ("python manage.py importar_itens_planilha","management/commands/importar_itens_planilha.py",
         "Importa itens de planilha Excel via CLI sem interface web"),
    ],
    col_widths=[5.5*cm, 5.5*cm, 6*cm]
))

story.append(sp(0.3))
story.append(PageBreak())

# ════════════════════════════════════════════════════════════════════════════
#  11. SEGURANÇA
# ════════════════════════════════════════════════════════════════════════════
story.append(section_box("11. SEGURANÇA E BOAS PRÁTICAS", NAVY))
story.append(sp(0.2))
story.append(make_table(
    ["Aspecto", "Implementação"],
    [
        ("Autenticação",       "@login_required em 100% das views — redireciona para /login/ se não autenticado"),
        ("CSRF",               "CsrfViewMiddleware ativo; todos os formulários POST incluem {% csrf_token %}"),
        ("Exclusão por POST",  "@require_POST em todas as views de delete — GET retorna 405 Method Not Allowed"),
        ("Auditoria",          "Todos os registros têm criado_por/atualizado_por rastreados automaticamente"),
        ("Transações atômicas","transaction.atomic() em criação/movimentação de itens e licenças"),
        ("Uploads",            "Fotos e PDFs armazenados em media/ com caminhos controlados"),
        ("Variáveis sensíveis","SECRET_KEY, DEBUG, ALLOWED_HOSTS, senhas por variáveis de ambiente"),
        ("Admin restrito",     "Interface /admin/ acessível apenas para staff/superuser Django"),
        ("Integridade referencial","PROTECT em FKs críticas (Fornecedor, LoteEstoque) — previne exclusão acidental"),
        ("Validação de formulários","Validações server-side em todos os forms; clean() com regras de negócio"),
    ],
    col_widths=[4.5*cm, 12.5*cm]
))

story.append(sp(0.4))
hr_final = HRFlowable(width="100%", thickness=1, color=NAVY, spaceAfter=6)
story.append(hr_final)
story.append(Paragraph(
    f"Documento gerado em {date.today().strftime('%d/%m/%Y')} — Santa Colomba Agropecuária Ltda. — TI",
    S("footer", fontSize=8, textColor=LGRAY, alignment=TA_CENTER, leading=10)
))

# ══ Build ═════════════════════════════════════════════════════════════════════
print("Gerando PDF...")
doc.build(story)
print(f"PDF salvo: {OUTPUT}")
