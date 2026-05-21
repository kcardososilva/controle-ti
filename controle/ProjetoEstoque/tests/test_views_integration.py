"""
Teste de integração: bate em todas as URLs do sistema com usuário logado.
Verifica se nenhuma view levanta exceção inesperada (status != 500).
"""

from django.contrib.auth.models import User, Permission
from django.test import TestCase, Client
from django.urls import reverse

from ProjetoEstoque.models import (
    Categoria, CentroCusto, CicloManutencao, Comentario, Fornecedor,
    Funcao, Item, ItemLote, Licenca, LicencaLote, Localidade, LoteEstoque,
    Locacao, MovimentacaoItem, MovimentacaoLicenca, Preventiva,
    SimNaoChoices, StatusItemChoices, StatusUsuarioChoices,
    TipoMovimentacaoChoices, TipoTransferenciaChoices, TipoMovLicencaChoices,
    Subtipo, Usuario, CheckListModelo,
)
from decimal import Decimal
from datetime import date


def create_superuser():
    user = User.objects.create_superuser("admin_test", "admin@test.com", "admin123")
    return user


def setup_base_data(user):
    """Cria registros mínimos necessários para as views funcionarem."""
    cat = Categoria.objects.create(nome="Informática", criado_por=user, atualizado_por=user)
    subtipo = Subtipo.objects.create(nome="Notebook", alocado="sim", categoria=cat, criado_por=user, atualizado_por=user)
    localidade = Localidade.objects.create(local="TI Principal", criado_por=user, atualizado_por=user)
    cc = CentroCusto.objects.create(numero="1001", departamento="TI", criado_por=user, atualizado_por=user)
    funcao = Funcao.objects.create(nome="Analista TI", criado_por=user, atualizado_por=user)
    fornecedor = Fornecedor.objects.create(nome="Dell Brasil", cnpj="72.381.189/0001-10", criado_por=user, atualizado_por=user)

    item = Item.objects.create(
        nome="Notebook Dell", numero_serie="SN123456", marca="Dell",
        modelo="Latitude 5520", status=StatusItemChoices.ATIVO,
        quantidade=5, localidade=localidade, centro_custo=cc,
        fornecedor=fornecedor, criado_por=user, atualizado_por=user,
    )

    lote = LoteEstoque.objects.create(
        fornecedor=fornecedor, data_entrada=date.today(),
        numero_nf="NF-001", quantidade=5, custo_unitario=Decimal("4000.00"),
        criado_por=user, atualizado_por=user,
    )
    item_lote = ItemLote.objects.create(
        item=item, lote=lote, quantidade_entrada=5,
        quantidade_disponivel=5, custo_unitario=Decimal("4000.00"),
        criado_por=user, atualizado_por=user,
    )
    item.tem_lote = True
    item.save()

    colaborador = Usuario.objects.create(
        nome="Fulano Silva", status=StatusUsuarioChoices.ATIVO,
        centro_custo=cc, funcao=funcao, localidade=localidade,
        pmb=SimNaoChoices.NAO, criado_por=user, atualizado_por=user,
    )

    mov = MovimentacaoItem.objects.create(
        tipo_movimentacao=TipoMovimentacaoChoices.TRANSFERENCIA,
        tipo_transferencia=TipoTransferenciaChoices.ENTREGA,
        item=item, usuario=colaborador, quantidade=1,
        localidade_origem=localidade, localidade_destino=localidade,
        criado_por=user, atualizado_por=user,
    )

    licenca = Licenca.objects.create(nome="MS Office 365", centro_custo=cc, criado_por=user, atualizado_por=user)
    lote_lic = LicencaLote.objects.create(
        licenca=licenca, quantidade_total=10, quantidade_disponivel=10,
        custo_ciclo=Decimal("1200.00"), periodicidade="anual",
        criado_por=user, atualizado_por=user,
    )

    ciclo = CicloManutencao.objects.create(
        item=item, status_inicial="ativo", causa="Teste",
        custo=Decimal("200.00"), criado_por=user, atualizado_por=user,
    )

    comentario = Comentario.objects.create(texto="Observação de teste", item=item, criado_por=user, atualizado_por=user)

    locacao = Locacao.objects.create(
        equipamento=Item.objects.create(
            nome="Impressora Locada", status=StatusItemChoices.ATIVO,
            locado=SimNaoChoices.SIM, quantidade=1,
            criado_por=user, atualizado_por=user,
        ),
        tempo_locado=12, valor_mensal=Decimal("350.00"),
        data_entrada=date.today(), fornecedor=fornecedor,
        criado_por=user, atualizado_por=user,
    )

    return {
        "cat": cat, "subtipo": subtipo, "localidade": localidade,
        "cc": cc, "funcao": funcao, "fornecedor": fornecedor,
        "item": item, "lote": lote, "item_lote": item_lote,
        "colaborador": colaborador, "mov": mov,
        "licenca": licenca, "lote_lic": lote_lic,
        "ciclo": ciclo, "comentario": comentario, "locacao": locacao,
    }


class AllViewsStatusTest(TestCase):
    """
    Testa que todas as views GET retornam status 200 ou 302 (redirect).
    Status 500 é falha. Status 403/404 aceitáveis em alguns casos.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = create_superuser()
        cls.data = setup_base_data(cls.user)

    def setUp(self):
        self.client = Client()
        self.client.login(username="admin_test", password="admin123")

    def _get(self, url_name, *args, expected=(200, 302), **kwargs):
        try:
            url = reverse(url_name, args=args, kwargs=kwargs)
        except Exception as e:
            self.fail(f"reverse('{url_name}', {args}) falhou: {e}")
        resp = self.client.get(url, follow=False)
        self.assertIn(
            resp.status_code, expected,
            f"GET {url} retornou {resp.status_code} (esperado {expected})"
        )
        return resp

    # ── Dashboard ──────────────────────────────────────────────────────────
    def test_dashboard(self):           self._get("dashboard")
    def test_sobre(self):               self._get("sobre_plataforma")

    # ── Categorias ────────────────────────────────────────────────────────
    def test_categorias_list(self):     self._get("categorias_list")
    def test_categoria_create(self):    self._get("categoria_create")
    def test_categoria_update(self):    self._get("categoria_update", self.data["cat"].pk)

    # ── Subtipos ──────────────────────────────────────────────────────────
    def test_subtipo_list(self):        self._get("subtipo_list")
    def test_subtipo_create(self):      self._get("subtipo_create")
    def test_subtipo_detail(self):      self._get("subtipo_detail", self.data["subtipo"].pk)
    def test_subtipo_update(self):      self._get("subtipo_update", self.data["subtipo"].pk)

    # ── Funções ───────────────────────────────────────────────────────────
    def test_funcoes_list(self):        self._get("funcoes_list")
    def test_funcao_create(self):       self._get("funcao_create")
    def test_funcao_edit(self):         self._get("funcao_edit", self.data["funcao"].pk)

    # ── Localidades ───────────────────────────────────────────────────────
    def test_localidade_list(self):     self._get("localidade_list")
    def test_localidade_create(self):   self._get("localidade_create")
    def test_localidade_detail(self):   self._get("localidade_detail", self.data["localidade"].pk)
    def test_localidade_update(self):   self._get("localidade_update", self.data["localidade"].pk)

    # ── Centros de Custo ──────────────────────────────────────────────────
    def test_cc_list(self):             self._get("centrocusto_list")
    def test_cc_create(self):           self._get("centrocusto_create")
    def test_cc_detail(self):           self._get("centrocusto_detail", self.data["cc"].pk)
    def test_cc_update(self):           self._get("centrocusto_update", self.data["cc"].pk)
    def test_cc_pdf(self):              self._get("centrocusto_export_pdf")

    # ── Fornecedores ──────────────────────────────────────────────────────
    def test_fornecedor_list(self):     self._get("fornecedor_list")
    def test_fornecedor_create(self):   self._get("fornecedor_create")
    def test_fornecedor_detail(self):   self._get("fornecedor_detail", self.data["fornecedor"].pk)
    def test_fornecedor_update(self):   self._get("fornecedor_update", self.data["fornecedor"].pk)
    def test_fornecedor_pdf(self):      self._get("fornecedor_export_pdf")

    # ── Usuários ──────────────────────────────────────────────────────────
    def test_usuario_list(self):        self._get("usuario_list")
    def test_usuario_create(self):      self._get("usuario_create")
    def test_usuario_detail(self):      self._get("usuario_detail", self.data["colaborador"].pk)
    def test_usuario_update(self):      self._get("usuario_update", self.data["colaborador"].pk)
    def test_usuario_dashboard(self):   self._get("usuario_dashboard")

    # ── Equipamentos ──────────────────────────────────────────────────────
    def test_equipamentos_list(self):   self._get("equipamentos_list")
    def test_equipamento_create(self):  self._get("cadastrar_equipamento")
    def test_equipamento_detalhe(self): self._get("equipamento_detalhe", self.data["item"].pk)
    def test_equipamento_update(self):  self._get("item_update", self.data["item"].pk)
    def test_equipamento_exportar(self):self._get("equipamentos_exportar")
    def test_equipamento_importar(self):self._get("importar_planilha", expected=(200, 302, 400, 405))
    def test_termo_entrega_form(self):  self._get("termo_entrega_form", self.data["item"].pk)
    def test_termo_devolucao_form(self):self._get("termo_devolucao_form", self.data["item"].pk)

    # ── Movimentações ─────────────────────────────────────────────────────
    def test_mov_list(self):            self._get("movimentacao_list")
    def test_mov_create(self):          self._get("movimentacao_create")
    def test_mov_detail(self):          self._get("movimentacao_detail", self.data["mov"].pk)
    def test_mov_update(self):          self._get("movimentacao_update", self.data["mov"].pk)
    def test_mov_pdf(self):             self._get("movimentacao_export_pdf")
    def test_mov_delete_confirm(self):  self._get("movimentacao_delete", self.data["mov"].pk)

    # ── Ciclos ────────────────────────────────────────────────────────────
    def test_ciclos_list(self):         self._get("ciclos_list")
    def test_ciclo_create(self):        self._get("ciclo_create", self.data["item"].pk)
    def test_ciclo_update(self):        self._get("ciclo_update", self.data["ciclo"].pk)

    # ── Locações ──────────────────────────────────────────────────────────
    def test_locacoes_list(self):       self._get("locacoes_list")
    def test_locacao_create(self):      self._get("locacao_create")
    def test_locacao_update(self):      self._get("locacao_update", self.data["locacao"].pk)

    # ── Comentários ───────────────────────────────────────────────────────
    def test_comentarios_list(self):    self._get("comentarios_list")
    def test_comentario_create(self):   self._get("comentario_create")
    def test_comentario_update(self):   self._get("comentario_update", self.data["comentario"].pk)

    # ── Preventivas ───────────────────────────────────────────────────────
    def test_preventiva_list(self):     self._get("preventiva_list")
    def test_preventiva_start(self):    self._get("preventiva_start")
    def test_preventiva_start_item(self): self._get("preventiva_start_item", self.data["item"].pk)
    def test_checklist_list(self):      self._get("checklist_list")
    def test_checklist_create(self):    self._get("checklist_create")

    # ── Licenças ──────────────────────────────────────────────────────────
    def test_licenca_list(self):        self._get("licenca_list")
    def test_licenca_create(self):      self._get("licenca_create")
    def test_licenca_detail(self):      self._get("licenca_detail", self.data["licenca"].pk)
    def test_licenca_update(self):      self._get("licenca_update", self.data["licenca"].pk)
    def test_licenca_export_excel(self):self._get("licenca_export_excel", self.data["licenca"].pk)
    def test_mov_licenca_list(self):    self._get("mov_licenca_list")
    def test_mov_licenca_form(self):    self._get("mov_licenca_form")
    def test_licenca_lote_list(self):   self._get("licenca_lote_list")
    def test_licenca_lote_novo(self):   self._get("licenca_lote_novo")
    def test_licenca_lote_edit(self):   self._get("licenca_lote_edit", self.data["lote_lic"].pk)

    # ── Dashboards ────────────────────────────────────────────────────────
    def test_cc_custos_dashboard(self): self._get("cc_custos_dashboard")
    def test_cc_custos_pdf(self):       self._get("cc_custos_export_pdf")
    def test_cc_custos_excel(self):     self._get("custo_cc_export_excel")
    def test_toner_dashboard(self):     self._get("dashboard_toner")
    def test_toner_excel(self):         self._get("toner_cc_export_excel")
    def test_licencas_dashboard(self):  self._get("licencas_dashboard")
    def test_preventiva_dashboard(self):self._get("preventiva_dashboard")

    # ── Avisos & Inteligência ─────────────────────────────────────────────
    def test_avisos_contratos(self):    self._get("avisos_contratos_vencer")
    def test_avisos_excel(self):        self._get("avisos_contratos_vencer_export_excel")
    def test_inteligencia(self):        self._get("sistema_inteligencia_dashboard")
    def test_inteligencia_busca(self):  self._get("sistema_inteligencia_busca_global")
    def test_inteligencia_csv(self):    self._get("sistema_inteligencia_export_csv")
    def test_noticias(self):            self._get("sistema_noticias")
