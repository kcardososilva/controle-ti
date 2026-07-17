"""
Testes automatizados dos serviços críticos do sistema.

Cobertura:
  - MovimentacaoEstoqueService: baixa com saldo insuficiente
  - MovimentacaoLicencaForm:    atribuição duplicada de licença
  - MovimentacaoEstoqueService: retorno de manutenção com status específico
  - get_usuario_atual_item:     lógica de entrega/devolução
  - _itens_ativos_do_usuario:   devolução desvincula item do colaborador
  - equipamento_detalhe:        "Detentor atual" não mostra quem devolveu
"""

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase, Client
from django.urls import reverse

from ProjetoEstoque.models import (
    CentroCusto,
    Fornecedor,
    Item,
    ItemLote,
    Licenca,
    LicencaLote,
    Localidade,
    LoteEstoque,
    MovimentacaoItem,
    MovimentacaoLicenca,
    SimNaoChoices,
    StatusItemChoices,
    TipoMovimentacaoChoices,
    TipoMovLicencaChoices,
    TipoTransferenciaChoices,
    Usuario,
)
from ProjetoEstoque.forms import MovimentacaoLicencaForm
from services.movimentacao_service import MovimentacaoEstoqueService
from services.termos import get_usuario_atual_item


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_user(username="testuser"):
    return User.objects.create_user(username, password="testpass123")


def make_item(user=None, **kwargs):
    defaults = dict(nome="Notebook Teste", quantidade=10, status=StatusItemChoices.ATIVO)
    if user:
        defaults["criado_por"] = user
        defaults["atualizado_por"] = user
    defaults.update(kwargs)
    return Item.objects.create(**defaults)


def make_lote(fornecedor, user=None, quantidade=10, custo=Decimal("500.00")):
    kwargs = dict(
        fornecedor=fornecedor,
        data_entrada=date.today(),
        numero_nf="NF-TEST-001",
        quantidade=quantidade,
        custo_unitario=custo,
    )
    if user:
        kwargs["criado_por"] = user
        kwargs["atualizado_por"] = user
    return LoteEstoque.objects.create(**kwargs)


def make_item_lote(item, lote, user=None, disponivel=None):
    kwargs = dict(
        item=item,
        lote=lote,
        quantidade_entrada=lote.quantidade,
        quantidade_disponivel=disponivel if disponivel is not None else lote.quantidade,
        custo_unitario=lote.custo_unitario,
    )
    if user:
        kwargs["criado_por"] = user
        kwargs["atualizado_por"] = user
    return ItemLote.objects.create(**kwargs)


def make_usuario(nome="Colaborador Teste"):
    return Usuario.objects.create(
        nome=nome,
        status="ativo",
        pmb=SimNaoChoices.NAO,
    )


# ---------------------------------------------------------------------------
# 1. Baixa com saldo insuficiente
# ---------------------------------------------------------------------------

class BaixaSaldoInsuficienteTest(TestCase):
    """
    Garante que _registrar_baixa levanta ValidationError quando a quantidade
    solicitada excede o saldo disponível no lote.
    """

    def setUp(self):
        self.user = make_user("baixa_user")
        self.fornecedor = Fornecedor.objects.create(nome="Fornecedor Teste", cnpj="00.000.000/0001-00")
        self.item = make_item(user=self.user, quantidade=5)
        self.lote = make_lote(self.fornecedor, user=self.user, quantidade=5)
        self.item_lote = make_item_lote(self.item, self.lote, user=self.user, disponivel=2)

    def _make_form(self, quantidade):
        form = MagicMock()
        form.cleaned_data = {
            "item": self.item,
            "lote": self.lote,
            "quantidade": quantidade,
            "usuario": None,
            "localidade_destino": None,
            "centro_custo_destino": None,
            "observacao": "teste baixa",
        }
        mov_mock = MagicMock(spec=MovimentacaoItem)
        mov_mock.item = self.item
        mov_mock.lote = self.lote
        mov_mock.quantidade = quantidade
        mov_mock.localidade_origem = None
        mov_mock.centro_custo_origem = None
        mov_mock.custo = None
        mov_mock.criado_por = None
        mov_mock.atualizado_por = None
        form.save.return_value = mov_mock
        return form

    def test_baixa_acima_do_saldo_do_lote_levanta_validation_error(self):
        form = self._make_form(quantidade=5)  # disponível é 2

        with self.assertRaises(ValidationError) as ctx:
            MovimentacaoEstoqueService._registrar_baixa(form=form, user=self.user)

        self.assertIn("Saldo insuficiente", str(ctx.exception))

    def test_baixa_dentro_do_saldo_nao_levanta(self):
        """Saldo disponível = 2, baixa = 2 — deve passar sem erro."""
        self.item.quantidade = 5
        self.item.save()

        form = self._make_form(quantidade=2)
        # Não deve levantar ValidationError
        MovimentacaoEstoqueService._registrar_baixa(form=form, user=self.user)

        self.item.refresh_from_db()
        self.assertEqual(self.item.quantidade, 3)  # 5 - 2


# ---------------------------------------------------------------------------
# 2. Atribuição duplicada de licença
# ---------------------------------------------------------------------------

class AtribuicaoDuplicadaLicencaTest(TestCase):
    """
    Garante que o form rejeita uma segunda atribuição da mesma licença ao
    mesmo usuário, quando a última movimentação já é uma atribuição.
    """

    def setUp(self):
        self.user = make_user("licenca_user")
        self.fornecedor = Fornecedor.objects.create(nome="Soft Corp", cnpj="11.111.111/0001-11")
        self.licenca = Licenca.objects.create(nome="Microsoft 365")
        self.lote = LicencaLote.objects.create(
            licenca=self.licenca,
            quantidade_total=5,
            quantidade_disponivel=5,
            custo_ciclo=Decimal("1200.00"),
            periodicidade="anual",
        )
        self.colaborador = make_usuario("João Silva")

        # Primeira atribuição já registrada no banco
        MovimentacaoLicenca.objects.create(
            tipo=TipoMovLicencaChoices.ATRIBUICAO,
            licenca=self.licenca,
            usuario=self.colaborador,
            lote=self.lote,
        )

    def test_segunda_atribuicao_para_mesmo_usuario_invalida(self):
        data = {
            "tipo": TipoMovLicencaChoices.ATRIBUICAO,
            "licenca": self.licenca.pk,
            "usuario": self.colaborador.pk,
            "observacao": "",
            "lote_id_select": "",
        }
        form = MovimentacaoLicencaForm(data=data)
        form.is_valid()

        self.assertIn("usuario", form.errors)
        self.assertIn("já possui esta licença", form.errors["usuario"][0])

    def test_atribuicao_apos_devolucao_valida(self):
        """Após devolução, nova atribuição deve ser aceita."""
        MovimentacaoLicenca.objects.create(
            tipo=TipoMovLicencaChoices.DEVOLUCAO,
            licenca=self.licenca,
            usuario=self.colaborador,
            lote=self.lote,
        )

        data = {
            "tipo": TipoMovLicencaChoices.ATRIBUICAO,
            "licenca": self.licenca.pk,
            "usuario": self.colaborador.pk,
            "observacao": "",
            "lote_id_select": "",
        }
        form = MovimentacaoLicencaForm(data=data)
        form.is_valid()

        self.assertNotIn("usuario", form.errors)


# ---------------------------------------------------------------------------
# 3. Retorno de manutenção com status específico
# ---------------------------------------------------------------------------

class RetornoManutencaoStatusTest(TestCase):
    """
    Garante que _registrar_movimentacao_padrao aplica mov.status_retorno
    como novo status do item no retorno de manutenção.
    """

    def setUp(self):
        self.user = make_user("retorno_user")
        self.localidade = Localidade.objects.create(local="Almoxarifado")
        self.item = make_item(status=StatusItemChoices.MANUTENCAO, quantidade=0)

    def _registrar_retorno(self, status_retorno):
        mov = MovimentacaoItem(
            tipo_movimentacao=TipoMovimentacaoChoices.RETORNO_MANUTENCAO,
            item=self.item,
            item_id=self.item.pk,
            status_retorno=status_retorno,
            localidade_destino=self.localidade,
            quantidade=1,
        )
        form = MagicMock()
        form.save.return_value = mov
        MovimentacaoEstoqueService._registrar_movimentacao_padrao(form=form, user=self.user)
        self.item.refresh_from_db()

    def test_retorno_com_status_ativo(self):
        self._registrar_retorno(StatusItemChoices.ATIVO)
        self.assertEqual(self.item.status, StatusItemChoices.ATIVO)

    def test_retorno_com_status_defeito(self):
        self._registrar_retorno(StatusItemChoices.DEFEITO)
        self.assertEqual(self.item.status, StatusItemChoices.DEFEITO)

    def test_retorno_sem_status_usa_backup_como_fallback(self):
        self._registrar_retorno(None)
        self.assertEqual(self.item.status, StatusItemChoices.BACKUP)

    def test_retorno_incrementa_quantidade(self):
        self.item.quantidade = 0
        self.item.save()
        self._registrar_retorno(StatusItemChoices.ATIVO)
        self.assertEqual(self.item.quantidade, 1)


# ---------------------------------------------------------------------------
# 4. get_usuario_atual_item — entrega e devolução
# ---------------------------------------------------------------------------

class GetUsuarioAtualItemTest(TestCase):
    """
    Garante que get_usuario_atual_item retorna o usuário correto considerando
    o histórico de entregas e devoluções.
    """

    def setUp(self):
        self.user = make_user("termo_user")
        self.item = make_item()
        self.colaborador = make_usuario("Maria Souza")

    def _entrega(self):
        return MovimentacaoItem.objects.create(
            tipo_movimentacao=TipoMovimentacaoChoices.TRANSFERENCIA,
            tipo_transferencia=TipoTransferenciaChoices.ENTREGA,
            item=self.item,
            usuario=self.colaborador,
            quantidade=1,
        )

    def _devolucao(self):
        return MovimentacaoItem.objects.create(
            tipo_movimentacao=TipoMovimentacaoChoices.TRANSFERENCIA,
            tipo_transferencia=TipoTransferenciaChoices.DEVOLUCAO,
            item=self.item,
            usuario=self.colaborador,
            quantidade=1,
        )

    def test_sem_movimentacoes_retorna_none(self):
        self.assertIsNone(get_usuario_atual_item(self.item))

    def test_apos_entrega_retorna_colaborador(self):
        self._entrega()
        resultado = get_usuario_atual_item(self.item)
        self.assertEqual(resultado, self.colaborador)

    def test_apos_entrega_e_devolucao_retorna_none(self):
        self._entrega()
        self._devolucao()
        self.assertIsNone(get_usuario_atual_item(self.item))

    def test_segunda_entrega_apos_devolucao_retorna_colaborador(self):
        self._entrega()
        self._devolucao()
        self._entrega()
        resultado = get_usuario_atual_item(self.item)
        self.assertEqual(resultado, self.colaborador)

    def test_movimentacao_nao_transferencia_ignorada(self):
        """Baixa/entrada não devem interferir na lógica de posse."""
        MovimentacaoItem.objects.create(
            tipo_movimentacao=TipoMovimentacaoChoices.BAIXA,
            item=self.item,
            quantidade=1,
        )
        self.assertIsNone(get_usuario_atual_item(self.item))


# ---------------------------------------------------------------------------
# 5. _itens_ativos_do_usuario — devolução deve desvincular o item do colaborador
# ---------------------------------------------------------------------------

class ItensAtivosDoUsuarioTest(TestCase):
    """
    Regressão: uma "Transferência de dispositivo" com tipo_transferencia=
    devolução não deve mais listar o item entre os itens ativos do
    colaborador (ver views.usuarios._itens_ativos_do_usuario).
    """

    def setUp(self):
        self.item = make_item()
        self.colaborador = make_usuario("João da Silva")

    def _entrega(self):
        return MovimentacaoItem.objects.create(
            tipo_movimentacao=TipoMovimentacaoChoices.TRANSFERENCIA,
            tipo_transferencia=TipoTransferenciaChoices.ENTREGA,
            item=self.item,
            usuario=self.colaborador,
            quantidade=1,
        )

    def _devolucao(self):
        return MovimentacaoItem.objects.create(
            tipo_movimentacao=TipoMovimentacaoChoices.TRANSFERENCIA,
            tipo_transferencia=TipoTransferenciaChoices.DEVOLUCAO,
            item=self.item,
            usuario=self.colaborador,
            quantidade=1,
        )

    def test_apos_entrega_item_aparece_ativo(self):
        from ProjetoEstoque.views.usuarios import _itens_ativos_do_usuario

        self._entrega()
        itens = _itens_ativos_do_usuario(self.colaborador)
        self.assertIn(self.item, itens)

    def test_apos_devolucao_item_nao_aparece_mais_ativo(self):
        from ProjetoEstoque.views.usuarios import _itens_ativos_do_usuario

        self._entrega()
        self._devolucao()
        itens = _itens_ativos_do_usuario(self.colaborador)
        self.assertNotIn(self.item, itens)


# ---------------------------------------------------------------------------
# 6. equipamento_detalhe — "Detentor atual" não deve mostrar o colaborador
#    que acabou de devolver o item (mov.usuario é preenchido só para
#    auditoria na devolução, ver movimentacao_service.py)
# ---------------------------------------------------------------------------

class EquipamentoDetalheDetentorAtualTest(TestCase):
    def setUp(self):
        self.django_user = make_user("detentor_user")
        self.client = Client()
        self.client.force_login(self.django_user)

        self.item = make_item()
        self.colaborador = make_usuario("Maria Devolvente")

    def _entrega(self):
        return MovimentacaoItem.objects.create(
            tipo_movimentacao=TipoMovimentacaoChoices.TRANSFERENCIA,
            tipo_transferencia=TipoTransferenciaChoices.ENTREGA,
            item=self.item,
            usuario=self.colaborador,
            quantidade=1,
        )

    def _devolucao(self):
        return MovimentacaoItem.objects.create(
            tipo_movimentacao=TipoMovimentacaoChoices.TRANSFERENCIA,
            tipo_transferencia=TipoTransferenciaChoices.DEVOLUCAO,
            item=self.item,
            usuario=self.colaborador,
            quantidade=1,
        )

    def test_apos_entrega_detentor_mostra_colaborador(self):
        self._entrega()
        response = self.client.get(reverse("equipamento_detalhe", args=[self.item.pk]))
        self.assertIn(self.colaborador.nome, response.context["ultimo_resp"])

    def test_apos_devolucao_detentor_nao_mostra_colaborador(self):
        self._entrega()
        self._devolucao()
        response = self.client.get(reverse("equipamento_detalhe", args=[self.item.pk]))
        self.assertNotIn(self.colaborador.nome, response.context["ultimo_resp"])
