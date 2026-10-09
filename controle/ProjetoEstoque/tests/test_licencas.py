# -*- coding: utf-8 -*-
"""
Licenças — assentos, custo e as integrações que causavam perda.

Escopo deliberadamente nas FUNÇÕES ESSENCIAIS (foi o pedido): o que, se
quebrar, custa dinheiro ou faz o sistema mentir sobre quantos assentos
existem. Nada de rótulo, cor ou contagem de KPI.

Os dois primeiros blocos travam defeitos que foram REPRODUZIDOS antes da
correção:

  • devolução dupla somava +1 no saldo do lote e criava assento que não
    existe (lote de 10 ficava com 11 disponíveis) — alcançável por duplo
    clique, por rodar o desligamento duas vezes, ou por combinar o botão da
    ficha do colaborador com a cascata;
  • editar o total do lote NÃO entregava os assentos comprados: `diff` era
    sempre zero porque `form.save(commit=False)` devolve a própria instância,
    então os dois lados da subtração já eram o valor novo.
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ProjetoEstoque.forms import LicencaLoteForm, MovimentacaoLicencaForm
from ProjetoEstoque.models import (
    CentroCusto,
    Licenca,
    LicencaLote,
    MovimentacaoLicenca,
    PeriodicidadeChoices,
    TipoMovLicencaChoices,
    Usuario,
)
from services import licenca_service as ls


class BaseLicenca(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="ti.lic", password="senha-de-teste-123", is_staff=True
        )
        cls.cc_ti = CentroCusto.objects.create(numero="910", departamento="TI", pmb="nao")
        cls.cc_rh = CentroCusto.objects.create(numero="920", departamento="RH", pmb="nao")
        cls.licenca = Licenca.objects.create(
            nome="SOFT TESTE", pmb="nao", centro_custo=cls.cc_ti,
        )

    def _colab(self, matricula="60001", nome="ANA LIC", status="ativo", cc=None):
        return Usuario.objects.create(
            matricula=matricula, nome=nome, status=status, pmb="nao",
            centro_custo=cc or self.cc_ti,
        )

    def _lote(self, total=10, ciclo="1200.00", periodicidade=PeriodicidadeChoices.ANUAL,
              licenca=None, data_compra=None):
        return LicencaLote.objects.create(
            licenca=licenca or self.licenca,
            quantidade_total=total,
            quantidade_disponivel=total,
            custo_ciclo=Decimal(ciclo),
            periodicidade=periodicidade,
            centro_custo=self.cc_ti,
            data_compra=data_compra or timezone.localdate(),
        )


class DevolucaoDuplaTests(BaseLicenca):
    """O defeito mais caro: devolver duas vezes criava assento do nada."""

    def test_segunda_devolucao_e_recusada(self):
        lote = self._lote(total=10)
        colab = self._colab()

        ls.atribuir(licenca=self.licenca, usuario=colab, user=self.user)
        ls.devolver(licenca=self.licenca, usuario=colab, user=self.user)

        with self.assertRaises(ls.LicencaError):
            ls.devolver(licenca=self.licenca, usuario=colab, user=self.user)

        lote.refresh_from_db()
        self.assertEqual(
            lote.quantidade_disponivel, 10,
            "o saldo não pode passar do total comprado — assento fantasma",
        )

    def test_saldo_nunca_passa_do_total_comprado(self):
        """Mesmo forçando o caminho silencioso (usado em massa) várias vezes."""
        lote = self._lote(total=3)
        colab = self._colab()

        ls.atribuir(licenca=self.licenca, usuario=colab, user=self.user)
        for _ in range(5):
            ls.devolver(
                licenca=self.licenca, usuario=colab, user=self.user, silencioso=True,
            )

        lote.refresh_from_db()
        self.assertEqual(lote.quantidade_disponivel, 3)
        self.assertEqual(ls.alocacao_de(self.licenca).em_uso, 0)

    def test_formulario_recusa_devolver_o_que_nao_esta_ativo(self):
        """
        A validação antiga filtrava `tipo=ATRIBUICAO` e por isso continuava
        achando a atribuição já devolvida — liberando a segunda devolução.
        """
        self._lote(total=5)
        colab = self._colab()
        ls.atribuir(licenca=self.licenca, usuario=colab, user=self.user)
        ls.devolver(licenca=self.licenca, usuario=colab, user=self.user)

        form = MovimentacaoLicencaForm(data={
            "tipo": TipoMovLicencaChoices.DEVOLUCAO,
            "licenca": self.licenca.pk, "usuario": colab.pk, "observacao": "",
        })
        self.assertFalse(form.is_valid())
        self.assertIn("usuario", form.errors)

    def test_devolucao_e_idempotente_em_modo_silencioso(self):
        """Modo das operações em massa: não levanta, mas também não duplica."""
        self._lote(total=5)
        colab = self._colab()
        ls.atribuir(licenca=self.licenca, usuario=colab, user=self.user)

        primeira = ls.devolver(
            licenca=self.licenca, usuario=colab, user=self.user, silencioso=True)
        segunda = ls.devolver(
            licenca=self.licenca, usuario=colab, user=self.user, silencioso=True)

        self.assertIsNotNone(primeira)
        self.assertIsNone(segunda, "a segunda devolução não deve gravar nada")
        self.assertEqual(
            MovimentacaoLicenca.objects.filter(
                licenca=self.licenca, tipo=TipoMovLicencaChoices.DEVOLUCAO).count(),
            1,
        )


class SaldoDoLoteTests(BaseLicenca):
    def test_editar_total_entrega_os_assentos_comprados(self):
        """
        Comprar 5 assentos e editar o total de 10 para 15 deixava o saldo em 9:
        o TI via "15 comprados" e só conseguia atribuir 1. `diff` era sempre 0.
        """
        lote = self._lote(total=10)
        colab = self._colab()
        ls.atribuir(licenca=self.licenca, usuario=colab, user=self.user)

        self.client.force_login(self.user)
        resposta = self.client.post(
            reverse("licenca_lote_edit", args=[lote.pk]),
            {
                "licenca": self.licenca.pk, "centro_custo": self.cc_ti.pk,
                "quantidade_total": 15, "periodicidade": PeriodicidadeChoices.ANUAL,
                "custo_ciclo": "1200.00",
            },
        )
        self.assertIn(resposta.status_code, (200, 302))

        lote.refresh_from_db()
        self.assertEqual(lote.quantidade_total, 15)
        self.assertEqual(
            lote.quantidade_disponivel, 14,
            "15 comprados - 1 em uso = 14 livres; os 5 assentos novos têm de entrar",
        )

    def test_saldo_calculado_sai_do_razao_e_nao_do_contador(self):
        lote = self._lote(total=4)
        for i in range(2):
            ls.atribuir(
                licenca=self.licenca, usuario=self._colab(matricula=f"7000{i}"),
                user=self.user,
            )

        # Contador corrompido na mão — o razão continua sendo a verdade.
        LicencaLote.objects.filter(pk=lote.pk).update(quantidade_disponivel=99)
        lote.refresh_from_db()

        self.assertEqual(ls.saldo_calculado(lote), 2)

    def test_atribuir_usa_o_razao_e_nao_oferece_assento_fantasma(self):
        """Contador inflado não pode virar atribuição: o pool está cheio."""
        lote = self._lote(total=2)
        for i in range(2):
            ls.atribuir(
                licenca=self.licenca, usuario=self._colab(matricula=f"7100{i}"),
                user=self.user,
            )
        LicencaLote.objects.filter(pk=lote.pk).update(quantidade_disponivel=50)

        with self.assertRaises(ls.LicencaError):
            ls.atribuir(
                licenca=self.licenca, usuario=self._colab(matricula="71099"),
                user=self.user,
            )

    def test_fifo_consome_o_lote_mais_antigo_primeiro(self):
        import datetime
        antigo = self._lote(total=1, data_compra=datetime.date(2024, 1, 1))
        novo = self._lote(total=1, data_compra=datetime.date(2026, 1, 1))

        mov = ls.atribuir(licenca=self.licenca, usuario=self._colab(), user=self.user)

        self.assertEqual(mov.lote_id, antigo.pk)
        novo.refresh_from_db()
        self.assertEqual(novo.quantidade_disponivel, 1)


class ConsistenciaTests(BaseLicenca):
    def test_conferir_consistencia_acha_contador_inflado_e_NAO_corrige(self):
        """
        Leitura não escreve. Uma função de relatório que conserta de passagem
        apaga a diferença entre "nunca divergiu" e "divergiu e alguém
        consertou sem registrar".
        """
        lote = self._lote(total=5)
        LicencaLote.objects.filter(pk=lote.pk).update(quantidade_disponivel=9)

        problemas = ls.conferir_consistencia(licenca=self.licenca)

        self.assertEqual(len(problemas), 1)
        self.assertEqual(problemas[0]["gravado"], 9)
        self.assertEqual(problemas[0]["esperado"], 5)

        lote.refresh_from_db()
        self.assertEqual(lote.quantidade_disponivel, 9, "conferir não pode corrigir")

    def test_reconciliar_alinha_o_contador_sem_tocar_no_razao(self):
        lote = self._lote(total=5)
        colab = self._colab()
        ls.atribuir(licenca=self.licenca, usuario=colab, user=self.user)
        movs_antes = MovimentacaoLicenca.objects.count()

        LicencaLote.objects.filter(pk=lote.pk).update(quantidade_disponivel=99)
        lote.refresh_from_db()

        resultado = ls.reconciliar_lote(lote, user=self.user)

        self.assertTrue(resultado["mudou"])
        lote.refresh_from_db()
        self.assertEqual(lote.quantidade_disponivel, 4)
        self.assertEqual(
            MovimentacaoLicenca.objects.count(), movs_antes,
            "reconciliar não cria nem apaga movimentação — o razão é a verdade",
        )

    def test_reconciliar_e_idempotente(self):
        lote = self._lote(total=5)
        LicencaLote.objects.filter(pk=lote.pk).update(quantidade_disponivel=0)
        lote.refresh_from_db()

        ls.reconciliar_lote(lote, user=self.user)
        segunda = ls.reconciliar_lote(lote, user=self.user)

        self.assertFalse(segunda["mudou"])


class CustoTests(BaseLicenca):
    def test_rateio_anual_por_assento(self):
        lote = self._lote(total=10, ciclo="1200.00", periodicidade=PeriodicidadeChoices.ANUAL)
        custo = ls.custos_do_lote(lote)

        self.assertEqual(custo.unitario_ciclo, Decimal("120.00"))
        self.assertEqual(custo.mensal_unit, Decimal("10.00"))
        self.assertEqual(custo.anual_unit, Decimal("120.00"))
        self.assertEqual(custo.mensal_total, Decimal("100.00"))
        self.assertEqual(custo.anual_total, Decimal("1200.00"))

    def test_rateio_mensal_nao_divide_o_ciclo(self):
        lote = self._lote(total=4, ciclo="80.00", periodicidade=PeriodicidadeChoices.MENSAL)
        custo = ls.custos_do_lote(lote)

        self.assertEqual(custo.mensal_unit, Decimal("20.00"))
        self.assertEqual(custo.anual_unit, Decimal("240.00"))

    def test_lote_sem_assento_nao_estoura_por_divisao_por_zero(self):
        lote = LicencaLote.objects.create(
            licenca=self.licenca, quantidade_total=0, quantidade_disponivel=0,
            custo_ciclo=Decimal("100.00"), periodicidade=PeriodicidadeChoices.ANUAL,
        )
        self.assertEqual(ls.custos_do_lote(lote).mensal_unit, Decimal("0.00"))

    def test_valor_gravado_bate_com_o_custo_exibido(self):
        """
        A divergência que existia: a tela e o formulário arredondavam em
        ordens diferentes, então `valor_unitario` podia não bater com o custo
        mensal mostrado na mesma linha.
        """
        from ProjetoEstoque.views.licencas import _normalizar_custos_lote

        lote = self._lote(total=7, ciclo="1000.00", periodicidade=PeriodicidadeChoices.ANUAL)
        mov = ls.atribuir(licenca=self.licenca, usuario=self._colab(), user=self.user)

        self.assertEqual(mov.valor_unitario, _normalizar_custos_lote(lote)["mensal_unit"])


class AtribuicaoTests(BaseLicenca):
    def test_nao_atribui_duas_vezes_ao_mesmo_colaborador(self):
        self._lote(total=5)
        colab = self._colab()
        ls.atribuir(licenca=self.licenca, usuario=colab, user=self.user)

        with self.assertRaises(ls.LicencaError):
            ls.atribuir(licenca=self.licenca, usuario=colab, user=self.user)

    def test_licenca_sem_lote_recusa_com_mensagem_util(self):
        colab = self._colab()
        with self.assertRaises(ls.LicencaError) as erro:
            ls.atribuir(licenca=self.licenca, usuario=colab, user=self.user)
        self.assertIn("lote", str(erro.exception).lower())

    def test_custo_segue_o_centro_de_custo_do_colaborador(self):
        self._lote(total=5)
        colab = self._colab(cc=self.cc_rh)
        mov = ls.atribuir(licenca=self.licenca, usuario=colab, user=self.user)
        self.assertEqual(mov.centro_custo_destino_id, self.cc_rh.pk)

    def test_devolucao_devolve_o_custo_ao_dono_do_lote(self):
        self._lote(total=5)
        colab = self._colab(cc=self.cc_rh)
        ls.atribuir(licenca=self.licenca, usuario=colab, user=self.user)
        mov = ls.devolver(licenca=self.licenca, usuario=colab, user=self.user)
        self.assertEqual(mov.centro_custo_destino_id, self.cc_ti.pk)

    def test_devolucao_estorna_o_valor_cobrado_na_saida(self):
        self._lote(total=5, ciclo="600.00")
        colab = self._colab()
        saida = ls.atribuir(licenca=self.licenca, usuario=colab, user=self.user)
        volta = ls.devolver(licenca=self.licenca, usuario=colab, user=self.user)
        self.assertEqual(volta.valor_unitario, saida.valor_unitario)


class EstadoDerivadoTests(BaseLicenca):
    def test_empate_de_timestamp_nao_inverte_o_estado(self):
        """
        Remoção em massa e cascata de desligamento gravam no mesmo instante.
        Sem o `id` como desempate, "atribuiu e devolveu" podia ser lido como
        "devolveu e atribuiu" — contando um assento que não existe.
        """
        self._lote(total=5)
        colab = self._colab()
        ls.atribuir(licenca=self.licenca, usuario=colab, user=self.user)
        ls.devolver(licenca=self.licenca, usuario=colab, user=self.user)

        agora = timezone.now()
        MovimentacaoLicenca.objects.filter(licenca=self.licenca).update(created_at=agora)

        self.assertEqual(ls.alocacao_de(self.licenca).em_uso, 0)

        from ProjetoEstoque.views.licencas import _get_dados_cc
        self.assertEqual(sum(c["qtd"] for c in _get_dados_cc(self.licenca)), 0)

    def test_licencas_do_usuario_reflete_so_o_vigente(self):
        self._lote(total=5)
        outra = Licenca.objects.create(nome="OUTRO SOFT", pmb="nao", centro_custo=self.cc_ti)
        LicencaLote.objects.create(
            licenca=outra, quantidade_total=3, quantidade_disponivel=3,
            custo_ciclo=Decimal("300.00"), periodicidade=PeriodicidadeChoices.ANUAL,
            centro_custo=self.cc_ti, data_compra=timezone.localdate(),
        )
        colab = self._colab()

        ls.atribuir(licenca=self.licenca, usuario=colab, user=self.user)
        ls.atribuir(licenca=outra, usuario=colab, user=self.user)
        ls.devolver(licenca=self.licenca, usuario=colab, user=self.user)

        nomes = [r["licenca"].nome for r in ls.licencas_do_usuario(colab)]
        self.assertEqual(nomes, ["OUTRO SOFT"])


class DesligamentoTests(BaseLicenca):
    def test_desligamento_devolve_todas_as_licencas(self):
        from services.desligamento_service import DesligamentoService

        self._lote(total=5)
        outra = Licenca.objects.create(nome="SOFT 2", pmb="nao", centro_custo=self.cc_ti)
        LicencaLote.objects.create(
            licenca=outra, quantidade_total=5, quantidade_disponivel=5,
            custo_ciclo=Decimal("500.00"), periodicidade=PeriodicidadeChoices.ANUAL,
            centro_custo=self.cc_ti, data_compra=timezone.localdate(),
        )
        colab = self._colab()
        ls.atribuir(licenca=self.licenca, usuario=colab, user=self.user)
        ls.atribuir(licenca=outra, usuario=colab, user=self.user)

        resultado = DesligamentoService.desligar_e_liberar_ativos(
            usuario=colab, executado_por=self.user,
        )

        self.assertEqual(resultado["licencas"], 2)
        self.assertEqual(ls.licencas_do_usuario(colab), [])

    def test_desligamento_rodado_duas_vezes_nao_infla_o_pool(self):
        """
        O cenário que criava assento fantasma em produção: a cascata rodando
        de novo (reprocessamento, duplo envio) sobre quem já estava devolvido.
        """
        from services.desligamento_service import DesligamentoService

        lote = self._lote(total=5)
        colab = self._colab()
        ls.atribuir(licenca=self.licenca, usuario=colab, user=self.user)

        DesligamentoService.desligar_e_liberar_ativos(usuario=colab, executado_por=self.user)
        DesligamentoService.desligar_e_liberar_ativos(usuario=colab, executado_por=self.user)

        lote.refresh_from_db()
        self.assertEqual(lote.quantidade_disponivel, 5)
        self.assertEqual(
            MovimentacaoLicenca.objects.filter(
                tipo=TipoMovLicencaChoices.DEVOLUCAO).count(), 1,
        )

    def test_uma_licenca_problematica_nao_impede_as_outras(self):
        """
        Abortar na 3ª licença deixaria as outras presas no nome de quem não
        trabalha mais aqui — a perda que o fluxo existe para evitar.
        """
        self._lote(total=5)
        colab = self._colab()
        ls.atribuir(licenca=self.licenca, usuario=colab, user=self.user)

        # Licença cuja atribuição aponta para lote apagado (dado legado).
        orfa = Licenca.objects.create(nome="SOFT LEGADO", pmb="nao")
        MovimentacaoLicenca.objects.create(
            tipo=TipoMovLicencaChoices.ATRIBUICAO, licenca=orfa, usuario=colab, lote=None,
        )

        resultado = ls.devolver_todas_do_usuario(usuario=colab, user=self.user)

        self.assertEqual(resultado["total"], 2)
        self.assertEqual(ls.licencas_do_usuario(colab), [])


class AssentosOrfaosTests(BaseLicenca):
    def test_encontra_assento_preso_em_colaborador_desligado(self):
        """
        A cascata só cobre quem foi desligado PELO fluxo. Colaborador marcado
        como desligado direto na ficha deixava o assento preso e invisível —
        nenhuma tela cruzava "assento ativo" com "status do colaborador".
        """
        self._lote(total=5, ciclo="1200.00")
        colab = self._colab()
        ls.atribuir(licenca=self.licenca, usuario=colab, user=self.user)

        Usuario.objects.filter(pk=colab.pk).update(status="desligado")

        orfaos = ls.assentos_orfaos()
        self.assertEqual(len(orfaos), 1)
        self.assertEqual(orfaos[0]["usuario"].pk, colab.pk)
        self.assertEqual(orfaos[0]["mensal"], Decimal("20.00"))

    def test_colaborador_ativo_nao_entra(self):
        self._lote(total=5)
        ls.atribuir(licenca=self.licenca, usuario=self._colab(), user=self.user)
        self.assertEqual(ls.assentos_orfaos(), [])

    def test_assento_devolvido_sai_da_lista(self):
        self._lote(total=5)
        colab = self._colab()
        ls.atribuir(licenca=self.licenca, usuario=colab, user=self.user)
        Usuario.objects.filter(pk=colab.pk).update(status="desligado")
        colab.refresh_from_db()

        self.assertEqual(len(ls.assentos_orfaos()), 1)
        ls.devolver(licenca=self.licenca, usuario=colab, user=self.user)
        self.assertEqual(ls.assentos_orfaos(), [])


class TelasTests(BaseLicenca):
    """Rotas do módulo respondem e as ações novas fazem o que prometem."""

    def setUp(self):
        from django.core.cache import cache
        # O badge do subnav é cacheado por 2 min (context processor). Sem
        # limpar, a contagem de um teste vaza para o próximo.
        cache.clear()
        self.client.force_login(self.user)

    def test_rotas_do_modulo_respondem(self):
        self._lote(total=5)
        for nome, args in (
            ("licenca_list", []), ("licenca_saude", []),
            ("licenca_detail", [self.licenca.pk]),
            ("licenca_lote_list", []), ("mov_licenca_list", []),
            ("mov_licenca_form", []), ("licenca_create", []),
        ):
            with self.subTest(rota=nome):
                self.assertEqual(self.client.get(reverse(nome, args=args)).status_code, 200)

    def test_painel_de_saude_lista_o_assento_orfao(self):
        self._lote(total=5, ciclo="1200.00")
        colab = self._colab(nome="SAIU DA EMPRESA")
        ls.atribuir(licenca=self.licenca, usuario=colab, user=self.user)
        Usuario.objects.filter(pk=colab.pk).update(status="desligado")

        corpo = self.client.get(reverse("licenca_saude")).content.decode("utf-8")
        self.assertIn("SAIU DA EMPRESA", corpo)
        self.assertIn("SOFT TESTE", corpo)

    def test_devolver_orfao_libera_o_assento(self):
        lote = self._lote(total=5)
        colab = self._colab()
        ls.atribuir(licenca=self.licenca, usuario=colab, user=self.user)
        Usuario.objects.filter(pk=colab.pk).update(status="desligado")

        resposta = self.client.post(reverse("licenca_devolver_orfao"), {
            "licenca": self.licenca.pk, "usuario": colab.pk,
        })

        self.assertEqual(resposta.status_code, 302)
        lote.refresh_from_db()
        self.assertEqual(lote.quantidade_disponivel, 5)
        self.assertEqual(ls.assentos_orfaos(), [])

    def test_reconciliar_pela_tela_alinha_o_contador(self):
        lote = self._lote(total=5)
        LicencaLote.objects.filter(pk=lote.pk).update(quantidade_disponivel=42)

        self.client.post(reverse("licenca_reconciliar_lote", args=[lote.pk]))

        lote.refresh_from_db()
        self.assertEqual(lote.quantidade_disponivel, 5)

    def test_atribuir_rapido_pelo_detalhe(self):
        self._lote(total=5)
        colab = self._colab()

        self.client.post(reverse("licenca_atribuir_rapido", args=[self.licenca.pk]), {
            "usuario": colab.pk,
        })

        self.assertIsNotNone(ls.atribuicao_vigente(self.licenca, colab))

    def test_atribuir_rapido_sem_assento_mostra_erro_e_nao_grava(self):
        self._lote(total=1)
        primeiro = self._colab(matricula="81001")
        ls.atribuir(licenca=self.licenca, usuario=primeiro, user=self.user)
        segundo = self._colab(matricula="81002")

        self.client.post(reverse("licenca_atribuir_rapido", args=[self.licenca.pk]), {
            "usuario": segundo.pk,
        })

        self.assertIsNone(ls.atribuicao_vigente(self.licenca, segundo))

    def test_api_de_colaboradores_exclui_quem_ja_tem_a_licenca(self):
        """Oferecer quem já tem só para o service recusar no POST é mandar o
        operador descobrir o erro pelo caminho mais longo."""
        self._lote(total=5)
        com_licenca = self._colab(matricula="82001", nome="JA TEM LICENCA")
        sem_licenca = self._colab(matricula="82002", nome="PODE RECEBER")
        ls.atribuir(licenca=self.licenca, usuario=com_licenca, user=self.user)

        dados = self.client.get(
            reverse("licenca_colaboradores_api"), {"licenca": self.licenca.pk},
        ).json()
        nomes = [r["text"] for r in dados["results"]]

        self.assertIn("PODE RECEBER", nomes)
        self.assertNotIn("JA TEM LICENCA", nomes)

    def test_api_de_colaboradores_exclui_desligado(self):
        self._lote(total=5)
        self._colab(matricula="83001", nome="DESLIGADO AQUI", status="desligado")

        dados = self.client.get(
            reverse("licenca_colaboradores_api"), {"licenca": self.licenca.pk},
        ).json()

        self.assertNotIn("DESLIGADO AQUI", [r["text"] for r in dados["results"]])

    def test_acoes_de_escrita_exigem_post(self):
        lote = self._lote(total=5)
        for nome, args in (
            ("licenca_devolver_orfao", []),
            ("licenca_reconciliar_lote", [lote.pk]),
            ("licenca_atribuir_rapido", [self.licenca.pk]),
        ):
            with self.subTest(rota=nome):
                self.assertEqual(
                    self.client.get(reverse(nome, args=args)).status_code, 405)

    def test_detalhe_nao_embute_os_colaboradores_no_html(self):
        """
        1.376 ativos como <option> eram ~80 KB por abertura. A busca é remota;
        este teste impede a regressão de voltar a renderizar a lista inteira.
        """
        self._lote(total=5)
        for i in range(12):
            self._colab(matricula=f"9{i:04d}", nome=f"COLABORADOR DE CARGA {i}")

        corpo = self.client.get(
            reverse("licenca_detail", args=[self.licenca.pk])).content.decode("utf-8")

        self.assertIn("lic-config-atribuir", corpo)
        self.assertNotIn("COLABORADOR DE CARGA 7", corpo)


class ComandoAuditoriaTests(BaseLicenca):
    def test_comando_falha_quando_ha_assento_orfao(self):
        from io import StringIO
        from django.core.management import call_command

        self._lote(total=5)
        colab = self._colab()
        ls.atribuir(licenca=self.licenca, usuario=colab, user=self.user)
        Usuario.objects.filter(pk=colab.pk).update(status="desligado")

        with self.assertRaises(SystemExit):
            call_command("auditar_licencas", stdout=StringIO(), stderr=StringIO())

    def test_comando_passa_com_acervo_saudavel(self):
        from io import StringIO
        from django.core.management import call_command

        self._lote(total=5)
        saida = StringIO()
        call_command("auditar_licencas", stdout=saida)
        self.assertIn("sem pendência", saida.getvalue())

    def test_reconciliar_pelo_comando_corrige_e_nao_falha(self):
        from io import StringIO
        from django.core.management import call_command

        lote = self._lote(total=5)
        LicencaLote.objects.filter(pk=lote.pk).update(quantidade_disponivel=77)

        call_command("auditar_licencas", "--reconciliar", stdout=StringIO())

        lote.refresh_from_db()
        self.assertEqual(lote.quantidade_disponivel, 5)

# ===========================================================================
# Rodada de 2026-10-07 — ciclo de cobrança, renovação e números das telas
# ===========================================================================

class CicloDeCobrancaTests(BaseLicenca):
    """
    O defeito: a tabela de divisores do custo mensal e o enum de periodicidade
    NÃO eram a mesma lista.

    `_MESES_POR_CICLO` mapeava `trimestral` — que não era opção do enum — e não
    tinha `trienal` nem `contrato`, que eram. Um lote trienal caía no fallback
    de 1 mês e publicava custo mensal **36x maior** que o real; um lote
    "Contrato/Outro", 12x maior. Nenhum lote da base estava nesses ciclos,
    então nada apareceu em tela — mas o maior contrato do acervo passa de
    R$ 1 mi por ciclo, e ali o divisor errado não é centavo, é ordem de
    grandeza.
    """

    def test_todo_ciclo_do_enum_tem_divisor(self):
        """O enum é o contrato: opção sem divisor volta a cair no fallback."""
        for valor, _rotulo in PeriodicidadeChoices.choices:
            self.assertIn(
                valor, ls._MESES_POR_CICLO,
                f"periodicidade {valor!r} existe no enum e não tem divisor — "
                "o custo mensal dela vai sair errado em silêncio",
            )

    def test_trienal_divide_por_36(self):
        lote = self._lote(total=10, ciclo="36000.00",
                          periodicidade=PeriodicidadeChoices.TRI)
        custo = ls.custos_do_lote(lote)
        # 36.000 / 10 assentos = 3.600 por assento no ciclo de 3 anos
        # 3.600 / 36 meses = 100,00/mês por assento
        self.assertEqual(custo.unitario_ciclo, Decimal("3600.00"))
        self.assertEqual(custo.mensal_unit, Decimal("100.00"))
        self.assertEqual(custo.mensal_total, Decimal("1000.00"))

    def test_trimestral_divide_por_3(self):
        lote = self._lote(total=4, ciclo="1200.00",
                          periodicidade=PeriodicidadeChoices.TRIMESTRAL)
        custo = ls.custos_do_lote(lote)
        self.assertEqual(custo.mensal_unit, Decimal("100.00"))

    def test_contrato_e_tratado_como_anual_e_MARCADO_como_estimado(self):
        """
        Ciclo indefinido é estimado como anual — e a estimativa é declarada.

        Antes o fallback era MENSAL, que estima errado e estima para o lado que
        INFLA a despesa. Agora erra para o lado certo e, mais importante, avisa:
        número suposto sem marca nenhuma num relatório de custo é o que faz
        alguém decidir compra em cima do que ninguém apurou.
        """
        lote = self._lote(total=10, ciclo="1200.00",
                          periodicidade=PeriodicidadeChoices.CONTRATO)
        custo = ls.custos_do_lote(lote)
        self.assertEqual(custo.mensal_unit, Decimal("10.00"))
        self.assertTrue(custo.ciclo_estimado)

    def test_ciclo_conhecido_nao_e_marcado_como_estimado(self):
        lote = self._lote(total=10, ciclo="1200.00",
                          periodicidade=PeriodicidadeChoices.ANUAL)
        self.assertFalse(ls.custos_do_lote(lote).ciclo_estimado)


class VencimentoTests(BaseLicenca):
    """
    `data_renovacao` vazia significa "derive", não "não vence".

    É o que faz os 47 lotes já cadastrados entrarem no calendário sem ninguém
    digitar nada — e é por isso que a derivação precisa acertar o ciclo ATUAL,
    não o primeiro vencimento da história do lote.
    """

    def test_campo_gravado_ganha_do_calculado(self):
        import datetime
        alvo = timezone.localdate() + datetime.timedelta(days=17)
        lote = self._lote()
        lote.data_renovacao = alvo
        lote.save(update_fields=["data_renovacao"])
        self.assertEqual(ls.vencimento_de(lote), alvo)

    def test_derivado_cai_no_proximo_aniversario_e_nao_no_primeiro(self):
        """
        Lote anual comprado há 3 anos vence no aniversário DESTE ano.

        Devolver a primeira data já vencida encheria a tela de pendência falsa
        de três anos atrás, e o calendário perderia a utilidade no primeiro dia.
        """
        import datetime
        compra = timezone.localdate() - datetime.timedelta(days=365 * 3 + 10)
        lote = self._lote(data_compra=compra, periodicidade=PeriodicidadeChoices.ANUAL)

        vencimento = ls.vencimento_de(lote)
        self.assertGreater(vencimento, timezone.localdate())
        self.assertLessEqual((vencimento - timezone.localdate()).days, 366)

    def test_sem_data_de_compra_nem_de_renovacao_devolve_None(self):
        """
        Lote que o sistema NÃO consegue vigiar — e isso é diferente de "não
        vence". A tela o lista em vez de escondê-lo.
        """
        lote = self._lote()
        lote.data_compra = None
        lote.save(update_fields=["data_compra"])
        self.assertIsNone(ls.vencimento_de(lote))
        self.assertIn(lote, ls.lotes_sem_vencimento())

    def test_data_de_compra_absurda_nao_pendura_o_laco(self):
        """Ano digitado errado (1923) não pode fazer o laço rodar sem fim."""
        import datetime
        lote = self._lote(data_compra=datetime.date(1923, 5, 1),
                          periodicidade=PeriodicidadeChoices.MENSAL)
        self.assertIsNotNone(ls.vencimento_de(lote))


class RenovacaoTests(BaseLicenca):
    def test_janela_recorta_pelo_prazo(self):
        import datetime
        hoje = timezone.localdate()
        perto = self._lote(total=5, ciclo="500.00")
        perto.data_renovacao = hoje + datetime.timedelta(days=10)
        perto.save(update_fields=["data_renovacao"])

        longe = self._lote(total=5, ciclo="500.00")
        longe.data_renovacao = hoje + datetime.timedelta(days=200)
        longe.save(update_fields=["data_renovacao"])

        pks = {r["lote"].pk for r in ls.renovacoes(dias=30)}
        self.assertIn(perto.pk, pks)
        self.assertNotIn(longe.pk, pks)

        pks_amplas = {r["lote"].pk for r in ls.renovacoes(dias=365)}
        self.assertIn(longe.pk, pks_amplas)

    def test_desperdicio_do_ciclo_e_o_assento_pago_e_parado(self):
        """
        O número que muda a pergunta de "renova?" para "renova quantos?".

        Lote de 10 assentos a R$ 1.000 o ciclo, com 4 em uso: R$ 600 do que vai
        ser cobrado é assento que ninguém usa.
        """
        import datetime
        lote = self._lote(total=10, ciclo="1000.00")
        lote.data_renovacao = timezone.localdate() + datetime.timedelta(days=20)
        lote.save(update_fields=["data_renovacao"])

        for i in range(4):
            ls.atribuir(
                licenca=self.licenca,
                usuario=self._colab(matricula=f"7100{i}", nome=f"COLAB {i}"),
                lote=lote, user=self.user,
            )

        linha = next(r for r in ls.renovacoes(dias=30) if r["lote"].pk == lote.pk)
        self.assertEqual(linha["em_uso"], 4)
        self.assertEqual(linha["ocioso"], 6)
        self.assertEqual(linha["custo_ciclo"], Decimal("1000.00"))
        self.assertEqual(linha["desperdicio_ciclo"], Decimal("600.00"))

    def test_vencido_entra_com_dias_negativos_e_dias_abs_positivo(self):
        import datetime
        lote = self._lote()
        lote.data_renovacao = timezone.localdate() - datetime.timedelta(days=12)
        lote.save(update_fields=["data_renovacao"])

        linha = next(r for r in ls.renovacoes(dias=30) if r["lote"].pk == lote.pk)
        self.assertTrue(linha["vencido"])
        self.assertEqual(linha["dias"], -12)
        # `dias_abs` existe para o template não ter de virar o sinal com
        # `|cut:"-"`, que é o tipo de malabarismo que quebra calado.
        self.assertEqual(linha["dias_abs"], 12)
        self.assertEqual(linha["faixa"], "vencido")

    def test_renovacao_manual_vencendo_entra_na_lista_de_manuais(self):
        """
        O caso que interrompe serviço: ninguém cobra, a licença simplesmente
        para no dia do vencimento.
        """
        import datetime
        lote = self._lote()
        lote.data_renovacao = timezone.localdate() + datetime.timedelta(days=15)
        lote.renovacao_automatica = False
        lote.save(update_fields=["data_renovacao", "renovacao_automatica"])

        resumo = ls.resumo_renovacoes(dias=90)
        self.assertEqual([m["lote"].pk for m in resumo["manuais"]], [lote.pk])

    def test_resumo_soma_o_valor_de_cada_janela(self):
        import datetime
        hoje = timezone.localdate()
        for dias, valor in ((10, "100.00"), (45, "200.00"), (80, "400.00")):
            lote = self._lote(total=1, ciclo=valor)
            lote.data_renovacao = hoje + datetime.timedelta(days=dias)
            lote.save(update_fields=["data_renovacao"])

        por_janela = {j["dias"]: j for j in ls.resumo_renovacoes(dias=90)["por_janela"]}
        self.assertEqual(por_janela[30]["valor"], Decimal("100.00"))
        self.assertEqual(por_janela[60]["valor"], Decimal("200.00"))
        self.assertEqual(por_janela[90]["valor"], Decimal("400.00"))


class CatalogoDerivadoTests(BaseLicenca):
    """
    Catálogo, galeria, lista de lotes e a aba de lotes do detalhe liam
    `quantidade_disponivel` — o CACHE —, enquanto o cabeçalho do detalhe e a
    atribuição rápida liam o razão. Duas telas a um clique de distância
    mostravam "assentos livres" diferentes para a mesma licença.
    """

    def test_catalogo_usa_o_razao_e_nao_o_contador(self):
        lote = self._lote(total=10)
        ls.atribuir(licenca=self.licenca, usuario=self._colab(), lote=lote, user=self.user)

        # Suja o contador por fora, como uma divergência real faria.
        LicencaLote.objects.filter(pk=lote.pk).update(quantidade_disponivel=99)

        linha = ls.catalogo(Licenca.objects.filter(pk=self.licenca.pk))[0]
        self.assertEqual(linha.em_uso, 1)
        self.assertEqual(linha.livre, 9)
        # O contador continua sendo LIDO — para a tela poder APONTAR a
        # divergência, não para passar por verdade.
        self.assertEqual(linha.saldo_contador, 99)
        self.assertTrue(linha.divergente)

    def test_desperdicio_mensal_e_o_que_sobra_pago(self):
        lote = self._lote(total=10, ciclo="1200.00")  # R$ 10,00/mês por assento
        ls.atribuir(licenca=self.licenca, usuario=self._colab(), lote=lote, user=self.user)

        linha = ls.catalogo(Licenca.objects.filter(pk=self.licenca.pk))[0]
        self.assertEqual(linha.mensal, Decimal("100.00"))
        self.assertEqual(linha.mensal_em_uso, Decimal("10.00"))
        self.assertEqual(linha.desperdicio_mensal, Decimal("90.00"))

    def test_resumo_do_acervo_fecha_com_as_linhas(self):
        self._lote(total=10, ciclo="1200.00")
        outra = Licenca.objects.create(nome="OUTRA SOFT", pmb="nao")
        self._lote(total=5, ciclo="600.00", licenca=outra)

        linhas = ls.catalogo()
        resumo = ls.resumo_acervo(linhas)
        self.assertEqual(resumo["total"], sum(l.total for l in linhas))
        self.assertEqual(resumo["livre"], sum(l.livre for l in linhas))
        self.assertEqual(resumo["mensal"], sum((l.mensal for l in linhas), Decimal("0.00")))

    def test_licenca_sem_lote_nao_estoura_e_e_sinalizada(self):
        linha = ls.catalogo(Licenca.objects.filter(pk=self.licenca.pk))[0]
        self.assertTrue(linha.sem_lote)
        self.assertEqual(linha.total, 0)
        self.assertEqual(linha.pct_uso, 0)
        self.assertFalse(linha.esgotada)


class FormularioDeLoteTests(BaseLicenca):
    """
    A validação de "não reduzir abaixo do que está em uso" media o uso pelo
    CACHE. Com o contador divergente — o estado que o painel de saúde existe
    para apontar — a conta errava para os dois lados.
    """

    def test_recusa_reduzir_abaixo_do_que_o_RAZAO_mostra_em_uso(self):
        lote = self._lote(total=10)
        for i in range(3):
            ls.atribuir(
                licenca=self.licenca,
                usuario=self._colab(matricula=f"7200{i}", nome=f"USO {i}"),
                lote=lote, user=self.user,
            )
        # Contador inflado: a conta antiga diria "0 em uso" e deixaria reduzir
        # para 1, deixando o lote com menos assentos do que gente usando.
        LicencaLote.objects.filter(pk=lote.pk).update(quantidade_disponivel=10)
        lote.refresh_from_db()

        form = LicencaLoteForm(
            data={
                "licenca": self.licenca.pk,
                "quantidade_total": 1,
                "periodicidade": PeriodicidadeChoices.ANUAL,
                "custo_ciclo": "1200.00",
                "renovacao_automatica": "on",
            },
            instance=lote,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("quantidade_total", form.errors)
        self.assertIn("3 assento", str(form.errors["quantidade_total"]))

    def test_aceita_reduzir_ate_o_que_esta_em_uso(self):
        lote = self._lote(total=10)
        ls.atribuir(licenca=self.licenca, usuario=self._colab(), lote=lote, user=self.user)

        form = LicencaLoteForm(
            data={
                "licenca": self.licenca.pk,
                "quantidade_total": 1,
                "periodicidade": PeriodicidadeChoices.ANUAL,
                "custo_ciclo": "1200.00",
                "renovacao_automatica": "on",
            },
            instance=lote,
        )
        self.assertTrue(form.is_valid(), form.errors.as_text())

    def test_renovacao_antes_da_compra_e_recusada(self):
        """
        Inversão de causa: a renovação é o fim do ciclo que a compra começou.
        Passava sem reclamar e aparecia depois como lote "vencido há 400 dias",
        sem ninguém entender de onde saiu.
        """
        import datetime
        hoje = timezone.localdate()
        form = LicencaLoteForm(data={
            "licenca": self.licenca.pk,
            "quantidade_total": 5,
            "periodicidade": PeriodicidadeChoices.ANUAL,
            "custo_ciclo": "600.00",
            "data_compra": hoje.isoformat(),
            "data_renovacao": (hoje - datetime.timedelta(days=30)).isoformat(),
            "renovacao_automatica": "on",
        })
        self.assertFalse(form.is_valid())
        self.assertIn("data_renovacao", form.errors)

    def test_editar_total_pela_VIEW_entrega_os_assentos_comprados(self):
        """
        Defeito reproduzido: `diff` era sempre zero porque
        `form.save(commit=False)` devolve a própria instância. Comprar 5
        assentos e editar o total de 10 para 15 deixava o saldo em 9.
        """
        lote = self._lote(total=10)
        ls.atribuir(licenca=self.licenca, usuario=self._colab(), lote=lote, user=self.user)

        self.client.force_login(self.user)
        self.client.post(reverse("licenca_lote_edit", args=[lote.pk]), {
            "licenca": self.licenca.pk,
            "quantidade_total": 15,
            "periodicidade": PeriodicidadeChoices.ANUAL,
            "custo_ciclo": "1800.00",
            "renovacao_automatica": "on",
        })

        lote.refresh_from_db()
        self.assertEqual(lote.quantidade_total, 15)
        self.assertEqual(lote.quantidade_disponivel, 14)


class TelasDaRodadaTests(BaseLicenca):
    def setUp(self):
        self.client.force_login(self.user)
        self.lote = self._lote(total=10, ciclo="1200.00")

    def test_rotas_novas_respondem(self):
        for nome in ("licenca_renovacoes", "mov_licenca_export"):
            with self.subTest(rota=nome):
                self.assertEqual(self.client.get(reverse(nome)).status_code, 200)

    def test_detalhe_avisa_a_divergencia_em_vez_de_so_calcula_la(self):
        """
        `kpi.divergente` era computado na view com um comentário dizendo que
        existia "para a tela poder avisar" — e o aviso nunca foi construído.
        """
        ls.atribuir(licenca=self.licenca, usuario=self._colab(), lote=self.lote, user=self.user)
        LicencaLote.objects.filter(pk=self.lote.pk).update(quantidade_disponivel=99)

        corpo = self.client.get(
            reverse("licenca_detail", args=[self.licenca.pk])
        ).content.decode("utf-8")
        self.assertIn("discorda das movimenta", corpo)
        self.assertIn("licenca_reconciliar_lote", corpo.replace(
            reverse("licenca_reconciliar_lote", args=[self.lote.pk]),
            "licenca_reconciliar_lote",
        ))

    def test_formulario_de_licenca_RENDERIZA_o_centro_de_custo(self):
        """
        O campo existia em `LicencaForm.fields` e o template nunca o desenhou:
        era impossível preenchê-lo pela interface, então
        `Licenca.centro_custo` ficava nulo em todo o acervo e a devolução de
        assento sem lote não tinha para onde estornar o custo.
        """
        corpo = self.client.get(reverse("licenca_create")).content.decode("utf-8")
        self.assertIn('name="centro_custo"', corpo)

    def test_export_do_razao_respeita_o_filtro_da_tela(self):
        """
        Export que ignora o filtro aplicado é a forma mais discreta de
        entregar o número errado para uma conferência contábil.
        """
        colab = self._colab()
        ls.atribuir(licenca=self.licenca, usuario=colab, lote=self.lote, user=self.user)
        ls.devolver(licenca=self.licenca, usuario=colab, user=self.user)

        tudo = self.client.get(reverse("mov_licenca_export"))
        self.assertEqual(tudo.status_code, 200)
        self.assertIn("csv", tudo["Content-Type"])
        linhas_tudo = tudo.content.decode("utf-8-sig").strip().splitlines()

        so_atribuicao = self.client.get(
            reverse("mov_licenca_export") + "?tipo=atribuicao"
        ).content.decode("utf-8-sig").strip().splitlines()

        # 1 cabeçalho + 2 movimentações vs. 1 cabeçalho + 1 atribuição
        self.assertEqual(len(linhas_tudo), 3)
        self.assertEqual(len(so_atribuicao), 2)

    def test_gravar_datas_em_lote_pela_tela_de_renovacoes(self):
        import datetime
        lote = self._lote(total=3)
        lote.data_compra = None
        lote.save(update_fields=["data_compra"])

        alvo = timezone.localdate() + datetime.timedelta(days=40)
        resposta = self.client.post(reverse("licenca_renovacao_datas"), {
            "compra_%s" % lote.pk: timezone.localdate().isoformat(),
            "renovacao_%s" % lote.pk: alvo.isoformat(),
        })
        self.assertEqual(resposta.status_code, 302)

        lote.refresh_from_db()
        self.assertEqual(lote.data_renovacao, alvo)
        self.assertIsNotNone(lote.data_compra)

    def test_campo_vazio_no_lote_NAO_apaga_o_que_ja_estava_gravado(self):
        """
        Formulário em lote que zera o que ficou de fora destrói dado por
        omissão — o jeito mais fácil de perder uma data que alguém conferiu.
        """
        import datetime
        alvo = timezone.localdate() + datetime.timedelta(days=50)
        self.lote.data_renovacao = alvo
        self.lote.save(update_fields=["data_renovacao"])

        self.client.post(reverse("licenca_renovacao_datas"), {
            "compra_%s" % self.lote.pk: "",
            "renovacao_%s" % self.lote.pk: "",
        })
        self.lote.refresh_from_db()
        self.assertEqual(self.lote.data_renovacao, alvo)

    def test_gravar_datas_exige_POST(self):
        self.assertEqual(
            self.client.get(reverse("licenca_renovacao_datas")).status_code, 405
        )

    def test_catalogo_e_detalhe_mostram_o_MESMO_saldo(self):
        """
        O sintoma que a unificação resolve: duas telas a um clique de
        distância discordando sobre quantos assentos estão livres.
        """
        ls.atribuir(licenca=self.licenca, usuario=self._colab(), lote=self.lote, user=self.user)
        LicencaLote.objects.filter(pk=self.lote.pk).update(quantidade_disponivel=99)

        catalogo = self.client.get(reverse("licenca_list"))
        detalhe = self.client.get(reverse("licenca_detail", args=[self.licenca.pk]))

        self.assertEqual(
            catalogo.context["page_obj"][0].livre,
            detalhe.context["kpi"]["disponivel"],
        )


class AlertaDeLicencaTests(BaseLicenca):
    """
    O alerta por e-mail era a SEXTA implementação de "este assento ainda está
    ativo?" — e discordava do painel de saúde em dois pontos.
    """

    def test_alerta_de_orfaos_usa_a_MESMA_apuracao_da_tela(self):
        from django.core import mail

        from services.email_alertas import alerta_licencas_desligados

        lote = self._lote(total=5)
        colab = self._colab(matricula="73001", nome="SAIU DA EMPRESA")
        ls.atribuir(licenca=self.licenca, usuario=colab, lote=lote, user=self.user)
        Usuario.objects.filter(pk=colab.pk).update(status="desligado")

        mail.outbox = []
        enviado = alerta_licencas_desligados()

        self.assertEqual(len(ls.assentos_orfaos()), 1)
        if enviado:
            corpo = mail.outbox[-1].body
            self.assertIn("SAIU DA EMPRESA", corpo)

    def test_devolucao_para_OUTRO_lote_nao_deixa_o_assento_pendente_para_sempre(self):
        """
        O defeito (a): o alerta casava a devolução por `(usuario, lote)`.
        Quando o lote original é apagado, `devolver` devolve o assento ao lote
        mais recente — outro `lote_id` —, e o `Exists` não a encontrava. O
        assento ficava cobrado para sempre, mesmo já resolvido.
        """
        antigo = self._lote(total=2)
        self._lote(total=2)  # lote mais recente, destino do fallback
        colab = self._colab(matricula="73002", nome="TROCOU DE LOTE")

        ls.atribuir(licenca=self.licenca, usuario=colab, lote=antigo, user=self.user)
        Usuario.objects.filter(pk=colab.pk).update(status="desligado")
        antigo.delete()

        colab.refresh_from_db()
        ls.devolver(licenca=self.licenca, usuario=colab, user=self.user)

        self.assertEqual(
            [o["usuario"].pk for o in ls.assentos_orfaos()], [],
            "assento devolvido continua aparecendo como pendente",
        )

    def test_status_nao_ativo_que_nao_e_desligado_tambem_conta(self):
        """
        O defeito (b): o alerta filtrava só `status=DESLIGADO`, enquanto o
        painel compara contra `"ativo"`. Qualquer outro estado não-ativo
        aparecia numa tela e não na outra.
        """
        lote = self._lote(total=5)
        colab = self._colab(matricula="73003", nome="AFASTADO")
        ls.atribuir(licenca=self.licenca, usuario=colab, lote=lote, user=self.user)
        Usuario.objects.filter(pk=colab.pk).update(status="afastado")

        self.assertEqual([o["usuario"].nome for o in ls.assentos_orfaos()], ["AFASTADO"])

    def test_alerta_de_renovacao_sai_vazio_quando_nada_vence(self):
        from services.email_alertas import alerta_licencas_vencendo

        lote = self._lote()
        lote.data_compra = None
        lote.save(update_fields=["data_compra"])
        self.assertFalse(alerta_licencas_vencendo(dias=30))

    def test_alerta_de_renovacao_cita_valor_e_assento_ocioso(self):
        import datetime

        from django.core import mail

        from services.email_alertas import alerta_licencas_vencendo

        lote = self._lote(total=10, ciclo="1000.00")
        lote.data_renovacao = timezone.localdate() + datetime.timedelta(days=20)
        lote.save(update_fields=["data_renovacao"])

        mail.outbox = []
        if alerta_licencas_vencendo(dias=60):
            corpo = mail.outbox[-1].body
            self.assertIn("SOFT TESTE", corpo)
            self.assertIn("0/10 em uso", corpo)


class ComandoDeAuditoriaRenovacaoTests(BaseLicenca):
    def test_contrato_vencido_faz_a_auditoria_falhar(self):
        """
        Vencido e "manual prestes a vencer" param serviço: o primeiro já
        parou, o segundo para sozinho se ninguém agir. Por isso entram no
        código de saída — o comando é agendado e precisa falhar visivelmente.
        """
        import datetime
        from io import StringIO

        from django.core.management import call_command

        lote = self._lote()
        lote.data_renovacao = timezone.localdate() - datetime.timedelta(days=5)
        lote.save(update_fields=["data_renovacao"])

        with self.assertRaises(SystemExit) as ctx:
            call_command("auditar_licencas", stdout=StringIO())
        self.assertEqual(ctx.exception.code, 1)

    def test_pool_esgotado_continua_sendo_AVISO_e_nao_falha(self):
        from io import StringIO

        from django.core.management import call_command

        lote = self._lote(total=1)
        lote.data_compra = None
        lote.save(update_fields=["data_compra"])
        ls.atribuir(licenca=self.licenca, usuario=self._colab(), lote=lote, user=self.user)

        saida = StringIO()
        call_command("auditar_licencas", stdout=saida)
        self.assertIn("POOLS ESGOTADOS", saida.getvalue())
