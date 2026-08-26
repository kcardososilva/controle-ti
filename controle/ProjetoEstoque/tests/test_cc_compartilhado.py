"""
Centro de custo de equipamento COMPARTILHADO — regra da maioria.

Um ativo compartilhado não tem detentor único cujo CC ele possa seguir: o CC
correto é o do departamento com MAIS colaboradores vinculados ativos. A regra
é revalidada em toda movimentação.

Cobertura:
  - maioria simples entre 3 centros de custo
  - a maioria muda quando um colaborador devolve o equipamento
  - empate preserva o CC atual (não fica oscilando)
  - empate sem CC atual entre os empatados desempata de forma determinística
  - colaborador sem CC não vota
  - sem vínculo ativo o CC é preservado (nunca zerado)
  - transferência de equipamento não deixa um único usuário ditar o CC
  - item NÃO compartilhado continua seguindo o detentor único
  - PMB acompanha o CC apurado
"""

from unittest.mock import MagicMock

from django.test import TestCase

from ProjetoEstoque.models import (
    CentroCusto,
    Item,
    ItemColaborador,
    MovimentacaoItem,
    SimNaoChoices,
    StatusItemChoices,
    TipoMovimentacaoChoices,
    TipoTransferenciaChoices,
    Usuario,
)
from services.movimentacao_service import MovimentacaoEstoqueService


def make_cc(numero, departamento):
    return CentroCusto.objects.create(numero=numero, departamento=departamento)


def make_colaborador(nome, cc=None):
    return Usuario.objects.create(
        nome=nome,
        status="ativo",
        pmb=SimNaoChoices.NAO,
        centro_custo=cc,
    )


def vincular(item, colaborador):
    return ItemColaborador.objects.create(item=item, colaborador=colaborador, ativo=True)


class CentroCustoMajoritarioTest(TestCase):
    """A apuração pura da maioria (`centro_custo_majoritario`)."""

    def setUp(self):
        self.cc_almox = make_cc("10100", "ALMOXARIFADO")
        self.cc_balanca = make_cc("20200", "BALANCA")
        self.cc_agricola = make_cc("30300", "OPERACOES AGRICOLAS")

        self.item = Item.objects.create(
            nome="Tablet Compartilhado",
            quantidade=1,
            status=StatusItemChoices.ATIVO,
            compartilhado=True,
            centro_custo=self.cc_almox,
        )

    def _vincular_n(self, cc, quantidade, prefixo):
        for i in range(quantidade):
            vincular(self.item, make_colaborador(f"{prefixo} {i}", cc))

    def test_maioria_entre_tres_centros_de_custo(self):
        # Cenário exato levantado pela operação: 5 almoxarifado, 6 balança,
        # 3 operações agrícolas -> vence a balança.
        self._vincular_n(self.cc_almox, 5, "Almox")
        self._vincular_n(self.cc_balanca, 6, "Balanca")
        self._vincular_n(self.cc_agricola, 3, "Agricola")

        cc, distribuicao = MovimentacaoEstoqueService.centro_custo_majoritario(self.item)

        self.assertEqual(cc, self.cc_balanca)
        self.assertEqual(distribuicao[self.cc_balanca], 6)
        self.assertEqual(distribuicao[self.cc_almox], 5)
        self.assertEqual(distribuicao[self.cc_agricola], 3)
        # distribuição vem ordenada do maior para o menor
        self.assertEqual(list(distribuicao)[0], self.cc_balanca)

    def test_empate_preserva_o_cc_atual_do_item(self):
        # Item já está no almoxarifado; 5 x 5 não deve movê-lo (estabilidade).
        self._vincular_n(self.cc_almox, 5, "Almox")
        self._vincular_n(self.cc_balanca, 5, "Balanca")

        cc, _ = MovimentacaoEstoqueService.centro_custo_majoritario(self.item)

        self.assertEqual(cc, self.cc_almox)

    def test_empate_sem_cc_atual_entre_os_lideres_e_deterministico(self):
        # CC atual (almoxarifado) não está empatado: desempata pelo menor número.
        self._vincular_n(self.cc_balanca, 4, "Balanca")
        self._vincular_n(self.cc_agricola, 4, "Agricola")

        cc, _ = MovimentacaoEstoqueService.centro_custo_majoritario(self.item)

        self.assertEqual(cc, self.cc_balanca)  # 20200 < 30300

    def test_colaborador_sem_centro_de_custo_nao_vota(self):
        self._vincular_n(self.cc_agricola, 2, "Agricola")
        for i in range(5):
            vincular(self.item, make_colaborador(f"Sem CC {i}", None))

        cc, distribuicao = MovimentacaoEstoqueService.centro_custo_majoritario(self.item)

        self.assertEqual(cc, self.cc_agricola)
        self.assertEqual(len(distribuicao), 1)

    def test_sem_vinculo_ativo_nao_ha_maioria(self):
        cc, distribuicao = MovimentacaoEstoqueService.centro_custo_majoritario(self.item)

        self.assertIsNone(cc)
        self.assertEqual(distribuicao, {})

    def test_vinculo_encerrado_nao_conta(self):
        self._vincular_n(self.cc_balanca, 3, "Balanca")
        encerrado = vincular(self.item, make_colaborador("Saiu", self.cc_agricola))
        encerrado.ativo = False
        encerrado.save(update_fields=["ativo"])

        cc, distribuicao = MovimentacaoEstoqueService.centro_custo_majoritario(self.item)

        self.assertEqual(cc, self.cc_balanca)
        self.assertNotIn(self.cc_agricola, distribuicao)

    def test_item_nao_compartilhado_nao_tem_maioria(self):
        self.item.compartilhado = False
        self.item.save(update_fields=["compartilhado"])
        self._vincular_n(self.cc_balanca, 5, "Balanca")

        cc, _ = MovimentacaoEstoqueService.centro_custo_majoritario(self.item)

        self.assertIsNone(cc)


class CentroCustoCompartilhadoNaMovimentacaoTest(TestCase):
    """A aplicação da regra dentro do fluxo real de movimentação."""

    def setUp(self):
        self.cc_ti = make_cc("12105", "TI")
        self.cc_balanca = make_cc("20200", "BALANCA")
        self.cc_tabaco = make_cc("39601", "PRODUCAO E CURA - TABACO")

        self.item = Item.objects.create(
            nome="Tablet Compartilhado",
            quantidade=1,
            status=StatusItemChoices.BACKUP,
            compartilhado=True,
            centro_custo=self.cc_ti,
            pmb=SimNaoChoices.NAO,
        )

    def _form_transferencia(self, *, colaborador, acao, tipo=None):
        """Espelha o `MovimentacaoItemForm` usado pela tela de movimentações."""
        mov = MovimentacaoItem(
            tipo_movimentacao=tipo or TipoMovimentacaoChoices.TRANSFERENCIA,
            tipo_transferencia=acao,
            item=self.item,
            usuario=colaborador,
            quantidade=1,
        )
        form = MagicMock()
        form.save.return_value = mov
        form.cleaned_data = {"tipo_movimentacao": mov.tipo_movimentacao, "novo_nome": ""}
        return form

    def _entregar(self, colaborador):
        return MovimentacaoEstoqueService.registrar(
            form=self._form_transferencia(
                colaborador=colaborador, acao=TipoTransferenciaChoices.ENTREGA
            ),
            user=None,
        )

    def _devolver(self, colaborador):
        return MovimentacaoEstoqueService.registrar(
            form=self._form_transferencia(
                colaborador=colaborador, acao=TipoTransferenciaChoices.DEVOLUCAO
            ),
            user=None,
        )

    def test_entrega_move_o_item_para_o_cc_majoritario(self):
        for i in range(3):
            self._entregar(make_colaborador(f"Tabaco {i}", self.cc_tabaco))

        self.item.refresh_from_db()
        self.assertEqual(self.item.centro_custo, self.cc_tabaco)

    def test_pmb_acompanha_o_cc_apurado(self):
        # CC de destino tem "TABACO" no nome -> PMB vira "sim" automaticamente.
        self.assertEqual(self.item.pmb, SimNaoChoices.NAO)

        self._entregar(make_colaborador("Tabaco A", self.cc_tabaco))

        self.item.refresh_from_db()
        self.assertEqual(self.item.centro_custo, self.cc_tabaco)
        self.assertEqual(self.item.pmb, SimNaoChoices.SIM)

    def test_devolucao_que_inverte_a_maioria_move_o_cc(self):
        balanca = [make_colaborador(f"Balanca {i}", self.cc_balanca) for i in range(2)]
        tabaco = [make_colaborador(f"Tabaco {i}", self.cc_tabaco) for i in range(3)]

        for c in balanca + tabaco:
            self._entregar(c)

        self.item.refresh_from_db()
        self.assertEqual(self.item.centro_custo, self.cc_tabaco)  # 3 x 2

        # Dois do tabaco devolvem -> balança passa a ser maioria (2 x 1).
        self._devolver(tabaco[0])
        self._devolver(tabaco[1])

        self.item.refresh_from_db()
        self.assertEqual(self.item.centro_custo, self.cc_balanca)

    def test_ultima_devolucao_preserva_o_cc_em_vez_de_zerar(self):
        colaborador = make_colaborador("Unico", self.cc_tabaco)
        self._entregar(colaborador)

        self.item.refresh_from_db()
        self.assertEqual(self.item.centro_custo, self.cc_tabaco)

        self._devolver(colaborador)

        self.item.refresh_from_db()
        # Sem vínculo ativo não há maioria a apurar: mantém o último CC
        # conhecido para o item não sumir do rateio de custos.
        self.assertEqual(self.item.centro_custo, self.cc_tabaco)
        self.assertEqual(self.item.status, StatusItemChoices.BACKUP)

    def test_transferencia_equipamento_nao_deixa_um_usuario_ditar_o_cc(self):
        # 3 vínculos ativos no tabaco definem o CC do ativo compartilhado.
        for i in range(3):
            self._entregar(make_colaborador(f"Tabaco {i}", self.cc_tabaco))

        self.item.refresh_from_db()
        self.assertEqual(self.item.centro_custo, self.cc_tabaco)

        # Uma transferência de equipamento informando um colaborador da balança
        # NÃO pode arrastar o CC do ativo coletivo para a balança.
        intruso = make_colaborador("Intruso Balanca", self.cc_balanca)
        mov = MovimentacaoItem(
            tipo_movimentacao=TipoMovimentacaoChoices.TRANSFERENCIA_EQUIPAMENTO,
            item=self.item,
            usuario=intruso,
            quantidade=1,
        )
        form = MagicMock()
        form.save.return_value = mov
        form.cleaned_data = {
            "tipo_movimentacao": TipoMovimentacaoChoices.TRANSFERENCIA_EQUIPAMENTO,
            "novo_nome": "",
        }

        MovimentacaoEstoqueService.registrar(form=form, user=None)

        self.item.refresh_from_db()
        self.assertEqual(self.item.centro_custo, self.cc_tabaco)

    def test_item_nao_compartilhado_continua_seguindo_o_detentor(self):
        self.item.compartilhado = False
        self.item.save(update_fields=["compartilhado"])

        colaborador = make_colaborador("Detentor Unico", self.cc_balanca)
        self._entregar(colaborador)

        self.item.refresh_from_db()
        self.assertEqual(self.item.centro_custo, self.cc_balanca)
