"""
Metas de preventiva por área e ciclo de 90 dias.

O que estes testes seguram é o DENOMINADOR e o CRÉDITO — as duas coisas que
erram em silêncio. Um equipamento que não deveria estar previsto (porque está
na prateleira, ou porque ninguém classificou a área dele) faz a meta parecer
pior do que é; uma execução creditada ao técnico errado faz a avaliação
individual do gestor apontar para a pessoa errada. Nenhuma das duas falhas
aparece na tela: o número simplesmente vem diferente.

Também travado aqui: a sobreposição de ciclos. Dois ciclos cobrindo o mesmo
dia fariam a mesma execução contar duas vezes, e as duas apurações
divergiriam sem que nada no sistema dissesse qual está certa.
"""
from datetime import date, datetime, timedelta

from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from ProjetoEstoque.models import (
    GRUPO_FORNECEDOR,
    AreaOperacionalChoices,
    Categoria,
    CheckListModelo,
    Item,
    Localidade,
    Preventiva,
    PreventivaAreaResponsavel,
    PreventivaCicloMeta,
    PreventivaExecucao,
    SimNaoChoices,
    StatusItemChoices,
    Subtipo,
)
from services import preventiva_meta_service as meta


class BaseMetaTests(TestCase):
    """Parque mínimo: 3 switches produtivos e 2 não produtivos, todos ativos."""

    def setUp(self):
        self.client = Client()
        self.gestor = User.objects.create_superuser("gestor_meta", "g@g.com", "x")
        self.tec_prod = User.objects.create_user(
            "tec_prod", password="x", first_name="Ana", last_name="Produtiva"
        )
        self.tec_apoio = User.objects.create_user(
            "tec_apoio", password="x", first_name="Bruno", last_name="Apoio"
        )
        self.client.force_login(self.gestor)

        cat = Categoria.objects.create(nome="Equipamento")
        self.subtipo = Subtipo.objects.create(nome="Switches", alocado="nao", categoria=cat)
        self.outro_subtipo = Subtipo.objects.create(nome="Access-Point", alocado="nao", categoria=cat)
        self.local = Localidade.objects.create(local="ALGODOEIRA")
        self.checklist = CheckListModelo.objects.create(nome="Preventiva de switch", intervalo_dias=90)

        self.produtivos = [
            self._switch(f"SW-PROD-{i}", AreaOperacionalChoices.PRODUTIVA) for i in range(3)
        ]
        self.nao_produtivos = [
            self._switch(f"SW-APOIO-{i}", AreaOperacionalChoices.NAO_PRODUTIVA) for i in range(2)
        ]

        self.hoje = date(2026, 10, 17)
        self.ciclo = PreventivaCicloMeta.objects.create(
            subtipo=self.subtipo,
            data_inicio=date(2026, 9, 1),
            duracao_dias=90,
        )

    # ── fábricas ───────────────────────────────────────────────────────────
    def _switch(self, nome, area, status=StatusItemChoices.ATIVO,
                precisa=SimNaoChoices.SIM, subtipo=None):
        return Item.objects.create(
            nome=nome,
            subtipo=subtipo or self.subtipo,
            localidade=self.local,
            status=status,
            precisa_preventiva=precisa,
            data_limite_preventiva=90,
            area_operacional=area,
        )

    def _executar(self, item, dia, tecnico=None, autor=None):
        preventiva, _ = Preventiva.objects.get_or_create(
            equipamento=item, checklist_modelo=self.checklist
        )
        return PreventivaExecucao.objects.create(
            preventiva=preventiva,
            data_execucao=dia,
            tecnico=tecnico,
            criado_por=autor or self.gestor,
        )

    def _area(self, apuracao, chave):
        return next(a for a in apuracao["areas"] if a["area"] == chave)


class UniversoDaMetaTests(BaseMetaTests):
    """Quem entra no denominador — e, principalmente, quem NÃO entra."""

    def test_universo_conta_apenas_ativos_classificados(self):
        self.assertEqual(meta.universo_qs(self.subtipo.pk).count(), 5)

    def test_item_fora_de_operacao_sai_do_previsto(self):
        """
        Switch em backup tem a contagem congelada pelo próprio modelo. Cobrar
        preventiva dele seria mandar o técnico visitar a prateleira.
        """
        self._switch("SW-NA-PRATELEIRA", AreaOperacionalChoices.PRODUTIVA,
                     status=StatusItemChoices.BACKUP)
        self.assertEqual(meta.universo_qs(self.subtipo.pk).count(), 5)
        self.assertEqual(meta.fora_do_ciclo_qs(self.subtipo.pk).count(), 1)

    def test_item_sem_area_nao_entra_na_meta_e_vira_pendencia_de_cadastro(self):
        """Sem área não há responsável nem comparativo: é falha de cadastro."""
        self._switch("SW-SEM-AREA", None)
        self.assertEqual(meta.universo_qs(self.subtipo.pk).count(), 5)
        self.assertEqual(meta.nao_classificados_qs(self.subtipo.pk).count(), 1)

    def test_area_vazia_como_string_conta_igual_a_nula(self):
        """
        O select devolve "" quando ninguém escolhe. Se "" e NULL fossem
        tratados de formas diferentes, metade dos não classificados ficaria
        invisível no aviso da tela.
        """
        item = self._switch("SW-AREA-VAZIA", None)
        Item.objects.filter(pk=item.pk).update(area_operacional="")
        self.assertEqual(meta.nao_classificados_qs(self.subtipo.pk).count(), 1)
        self.assertEqual(meta.universo_qs(self.subtipo.pk).count(), 5)

    def test_item_fora_do_programa_de_preventiva_nao_entra(self):
        self._switch("SW-FORA-DO-PROGRAMA", AreaOperacionalChoices.PRODUTIVA,
                     precisa=SimNaoChoices.NAO)
        self.assertEqual(meta.universo_qs(self.subtipo.pk).count(), 5)

    def test_item_excluido_logicamente_nao_entra(self):
        item = self._switch("SW-EXCLUIDO", AreaOperacionalChoices.PRODUTIVA)
        Item.all_objects.filter(pk=item.pk).update(excluido=True)
        self.assertEqual(meta.universo_qs(self.subtipo.pk).count(), 5)

    def test_outro_subtipo_nao_contamina_o_ciclo(self):
        self._switch("AP-PROD", AreaOperacionalChoices.PRODUTIVA, subtipo=self.outro_subtipo)
        self.assertEqual(meta.universo_qs(self.subtipo.pk).count(), 5)


class ApuracaoTests(BaseMetaTests):
    """Previsto, realizado, pendente e os percentuais."""

    def test_ciclo_sem_execucao_e_zero_por_cento(self):
        ap = meta.apurar_ciclo(self.ciclo, self.hoje)
        self.assertEqual(ap["previstos"], 5)
        self.assertEqual(ap["realizados"], 0)
        self.assertEqual(ap["pendentes"], 5)
        self.assertEqual(ap["pct"], 0)
        self.assertEqual(ap["pct_pendente"], 100)

    def test_execucao_dentro_da_janela_conta(self):
        self._executar(self.produtivos[0], date(2026, 9, 15))
        ap = meta.apurar_ciclo(self.ciclo, self.hoje)
        self.assertEqual(ap["realizados"], 1)
        self.assertEqual(ap["pendentes"], 4)
        self.assertEqual(ap["pct"], 20)

    def test_execucao_antes_do_ciclo_nao_conta(self):
        """Preventiva feita no ciclo passado não paga a meta deste."""
        self._executar(self.produtivos[0], date(2026, 8, 31))
        ap = meta.apurar_ciclo(self.ciclo, self.hoje)
        self.assertEqual(ap["realizados"], 0)

    def test_execucao_depois_do_fim_nao_conta(self):
        self._executar(self.produtivos[0], self.ciclo.data_fim + timedelta(days=1))
        ap = meta.apurar_ciclo(self.ciclo, self.hoje)
        self.assertEqual(ap["realizados"], 0)

    def test_ultimo_dia_do_ciclo_conta(self):
        """A janela é inclusiva nas duas pontas — 90 dias = início + 89."""
        self.assertEqual(self.ciclo.data_fim, date(2026, 11, 29))
        self._executar(self.produtivos[0], self.ciclo.data_fim)
        ap = meta.apurar_ciclo(self.ciclo, self.hoje)
        self.assertEqual(ap["realizados"], 1)

    def test_duas_execucoes_do_mesmo_equipamento_contam_uma_vez(self):
        """
        A meta é COBERTURA do parque, não volume de serviço. Contar duas
        visitas ao mesmo switch como 2/5 deixaria a meta ser fechada sem
        ninguém visitar os outros quatro.
        """
        self._executar(self.produtivos[0], date(2026, 9, 10))
        self._executar(self.produtivos[0], date(2026, 9, 20))
        ap = meta.apurar_ciclo(self.ciclo, self.hoje)
        self.assertEqual(ap["realizados"], 1)
        self.assertEqual(ap["pct"], 20)

    def test_meta_concluida_quando_todos_foram_executados(self):
        for item in self.produtivos + self.nao_produtivos:
            self._executar(item, date(2026, 9, 10))
        ap = meta.apurar_ciclo(self.ciclo, self.hoje)
        self.assertEqual(ap["pct"], 100)
        self.assertEqual(ap["pendentes"], 0)
        self.assertEqual(ap["risco"], "concluido")

    def test_separacao_por_area(self):
        self._executar(self.produtivos[0], date(2026, 9, 10))
        self._executar(self.nao_produtivos[0], date(2026, 9, 11))
        self._executar(self.nao_produtivos[1], date(2026, 9, 12))

        ap = meta.apurar_ciclo(self.ciclo, self.hoje)
        prod = self._area(ap, AreaOperacionalChoices.PRODUTIVA)
        apoio = self._area(ap, AreaOperacionalChoices.NAO_PRODUTIVA)

        self.assertEqual((prod["previstos"], prod["realizados"], prod["pct"]), (3, 1, 33))
        self.assertEqual((apoio["previstos"], apoio["realizados"], apoio["pct"]), (2, 2, 100))
        self.assertEqual(apoio["risco"], "concluido")

    def test_equipamentos_que_faltam_sao_nominais(self):
        self._executar(self.produtivos[0], date(2026, 9, 10))
        ap = meta.apurar_ciclo(self.ciclo, self.hoje)
        prod = self._area(ap, AreaOperacionalChoices.PRODUTIVA)
        faltam = {i.nome for i in prod["itens_pendentes"]}
        self.assertEqual(faltam, {"SW-PROD-1", "SW-PROD-2"})

    def test_resumo_bate_com_a_apuracao_completa(self):
        """
        O painel usa `apurar_ciclo` e a lista de ciclos usa `resumo_ciclo`. Se
        os dois divergirem, a mesma linha mostra números diferentes em duas
        telas e ninguém sabe qual acreditar.
        """
        self._executar(self.produtivos[0], date(2026, 9, 10))
        self._executar(self.nao_produtivos[0], date(2026, 9, 11))
        ap = meta.apurar_ciclo(self.ciclo, self.hoje)
        resumo = meta.resumo_ciclo(self.ciclo, self.hoje)
        for campo in ("previstos", "realizados", "pendentes", "pct", "risco"):
            self.assertEqual(ap[campo], resumo[campo], campo)


class CreditoAoTecnicoTests(BaseMetaTests):
    """De quem é o realizado e de quem é a pendência."""

    def test_realizado_vai_para_quem_executou(self):
        self._executar(self.produtivos[0], date(2026, 9, 10), tecnico=self.tec_prod)
        ap = meta.apurar_ciclo(self.ciclo, self.hoje)
        linha = next(t for t in ap["tecnicos"] if t["id"] == self.tec_prod.pk)
        self.assertEqual(linha["realizados"], 1)

    def test_sem_snapshot_de_tecnico_credita_o_autor_do_registro(self):
        """
        Mesmo critério da tela de Desempenho dos Técnicos. Divergir ali faria
        as duas telas brigarem sobre o mesmo fato.
        """
        self._executar(self.produtivos[0], date(2026, 9, 10), tecnico=None, autor=self.tec_apoio)
        ap = meta.apurar_ciclo(self.ciclo, self.hoje)
        linha = next(t for t in ap["tecnicos"] if t["id"] == self.tec_apoio.pk)
        self.assertEqual(linha["realizados"], 1)

    def test_pendencia_cai_no_responsavel_da_area(self):
        """É isso que dá dono à pendência ANTES de existir agendamento."""
        PreventivaAreaResponsavel.objects.create(
            area=AreaOperacionalChoices.PRODUTIVA, tecnico=self.tec_prod
        )
        ap = meta.apurar_ciclo(self.ciclo, self.hoje)
        linha = next(t for t in ap["tecnicos"] if t["id"] == self.tec_prod.pk)
        self.assertEqual(linha["pendentes"], 3)
        self.assertEqual(linha["previstos"], 3)

    def test_tecnico_da_preventiva_vence_o_responsavel_da_area(self):
        """Atribuição feita à mão é decisão de alguém; a área é só o padrão."""
        PreventivaAreaResponsavel.objects.create(
            area=AreaOperacionalChoices.PRODUTIVA, tecnico=self.tec_prod
        )
        Preventiva.objects.create(
            equipamento=self.produtivos[0],
            checklist_modelo=self.checklist,
            tecnico=self.tec_apoio,
        )
        ap = meta.apurar_ciclo(self.ciclo, self.hoje)
        do_apoio = next(t for t in ap["tecnicos"] if t["id"] == self.tec_apoio.pk)
        do_prod = next(t for t in ap["tecnicos"] if t["id"] == self.tec_prod.pk)
        self.assertEqual(do_apoio["pendentes"], 1)
        self.assertEqual(do_prod["pendentes"], 2)

    def test_responsavel_especifico_do_subtipo_vence_o_padrao_da_area(self):
        PreventivaAreaResponsavel.objects.create(
            area=AreaOperacionalChoices.PRODUTIVA, tecnico=self.tec_apoio
        )
        PreventivaAreaResponsavel.objects.create(
            area=AreaOperacionalChoices.PRODUTIVA, subtipo=self.subtipo, tecnico=self.tec_prod
        )
        mapa = meta.responsaveis_map(self.subtipo.pk)
        self.assertEqual(mapa[AreaOperacionalChoices.PRODUTIVA], self.tec_prod)

    def test_responsavel_inativo_nao_e_considerado(self):
        PreventivaAreaResponsavel.objects.create(
            area=AreaOperacionalChoices.PRODUTIVA, tecnico=self.tec_prod, ativo=False
        )
        self.assertEqual(meta.responsaveis_map(self.subtipo.pk), {})

    def test_responsavel_de_outro_subtipo_nao_vaza(self):
        PreventivaAreaResponsavel.objects.create(
            area=AreaOperacionalChoices.PRODUTIVA, subtipo=self.outro_subtipo, tecnico=self.tec_apoio
        )
        self.assertEqual(meta.responsaveis_map(self.subtipo.pk), {})


class RitmoERiscoTests(BaseMetaTests):
    """A leitura é sempre execução CONTRA prazo decorrido."""

    def test_pct_tempo_acompanha_o_dia_do_ciclo(self):
        # 01/09 + 46 dias = 17/10 → dia 47 de 90.
        ap = meta.apurar_ciclo(self.ciclo, self.hoje)
        self.assertEqual(ap["dias_decorridos"], 47)
        self.assertEqual(ap["dias_restantes"], 44)
        self.assertEqual(ap["pct_tempo"], 52)

    def test_execucao_abaixo_do_ritmo_e_apontada(self):
        """0% executado com 52% do prazo gasto é atraso, não 'em dia'."""
        ap = meta.apurar_ciclo(self.ciclo, self.hoje)
        self.assertEqual(ap["risco"], "atrasado")

    def test_execucao_no_ritmo_nao_acende_alerta(self):
        for item in self.produtivos:
            self._executar(item, date(2026, 9, 10))
        ap = meta.apurar_ciclo(self.ciclo, self.hoje)
        self.assertEqual(ap["pct"], 60)
        self.assertEqual(ap["risco"], "em_dia")

    def test_reta_final_com_pendencia_e_risco_de_nao_cumprir(self):
        """Com menos de 1/4 do prazo restante não há mais como recuperar ritmo."""
        quase_fim = self.ciclo.data_fim - timedelta(days=5)
        ap = meta.apurar_ciclo(self.ciclo, quase_fim)
        self.assertEqual(ap["risco"], "risco")

    def test_ciclo_vencido_com_pendencia_e_nao_cumprido(self):
        depois = self.ciclo.data_fim + timedelta(days=1)
        ap = meta.apurar_ciclo(self.ciclo, depois)
        self.assertEqual(ap["situacao"], "encerrado")
        self.assertEqual(ap["risco"], "nao_cumprida")

    def test_ciclo_futuro_nao_cobra_nada(self):
        futuro = PreventivaCicloMeta.objects.create(
            subtipo=self.outro_subtipo, data_inicio=date(2027, 1, 1), duracao_dias=90
        )
        ap = meta.apurar_ciclo(futuro, self.hoje)
        self.assertEqual(ap["situacao"], "futuro")
        self.assertEqual(ap["pct_tempo"], 0)

    def test_ritmo_necessario_distribui_a_pendencia_no_prazo_restante(self):
        ap = meta.apurar_ciclo(self.ciclo, self.hoje)
        self.assertEqual(ap["pendentes"], 5)
        self.assertEqual(ap["dias_restantes"], 44)
        self.assertAlmostEqual(ap["ritmo_necessario"], round(5 / 44, 2), places=2)

    def test_evolucao_em_porcentagem_da_meta_de_cada_area(self):
        """
        A curva é em % porque os parques têm tamanhos diferentes (3 contra 2):
        comparar contagens puniria a área maior por ser maior.
        """
        self._executar(self.nao_produtivos[0], date(2026, 9, 2))
        ap = meta.apurar_ciclo(self.ciclo, self.hoje)
        serie = next(
            s for s in ap["evolucao"]["series"]
            if s["area"] == AreaOperacionalChoices.NAO_PRODUTIVA
        )
        self.assertEqual(serie["acumulado_pct"][0], 50)   # 1 de 2
        self.assertEqual(ap["evolucao"]["ritmo_ideal"][-1], 100)

    def test_semanas_futuras_ficam_sem_ponto(self):
        """Linha cheia até a semana 13 faria o ciclo parecer estagnado."""
        ap = meta.apurar_ciclo(self.ciclo, self.hoje)
        serie = ap["evolucao"]["series"][0]
        self.assertIsNone(serie["acumulado_pct"][-1])


class CicloTests(BaseMetaTests):
    """Integridade da janela."""

    def test_ciclos_do_mesmo_subtipo_nao_podem_se_sobrepor(self):
        conflitante = PreventivaCicloMeta(
            subtipo=self.subtipo, data_inicio=date(2026, 10, 1), duracao_dias=90
        )
        with self.assertRaises(ValidationError):
            conflitante.full_clean()

    def test_subtipos_diferentes_podem_ter_ciclos_simultaneos(self):
        paralelo = PreventivaCicloMeta(
            subtipo=self.outro_subtipo, data_inicio=date(2026, 9, 1), duracao_dias=90
        )
        paralelo.full_clean()   # não levanta

    def test_proximo_ciclo_encadeia_sem_lacuna(self):
        """Dia descoberto é execução que não conta para meta nenhuma."""
        # Planejando ainda dentro do ciclo atual: o próximo começa no dia
        # seguinte ao fim deste.
        seguinte = meta.proximo_inicio(self.subtipo.pk, date(2026, 11, 20))
        self.assertEqual(seguinte, self.ciclo.data_fim + timedelta(days=1))

    def test_ciclo_nunca_comeca_no_passado(self):
        """
        Quando o ciclo anterior terminou há tempos, encadear literalmente
        abriria uma janela que já venceu — e ela nasceria "não cumprida", com
        pendência que ninguém teve chance de executar. Nesse caso começa hoje,
        aceitando a lacuna que o abandono do programa já criou.
        """
        hoje = date(2027, 3, 1)
        self.assertEqual(meta.proximo_inicio(self.subtipo.pk, hoje), hoje)

    def test_ciclo_vigente_e_o_que_cobre_hoje(self):
        self.assertEqual(meta.ciclo_vigente(self.subtipo.pk, self.hoje), self.ciclo)
        self.assertIsNone(meta.ciclo_vigente(self.subtipo.pk, date(2027, 6, 1)))

    def test_abrir_ciclo_recusa_sobreposicao(self):
        with self.assertRaises(ValidationError):
            meta.abrir_ciclo(self.subtipo.pk, data_inicio=date(2026, 10, 1))


class TelasTests(BaseMetaTests):
    """As três rotas abrem, e abrem com conteúdo."""

    def test_painel_abre_com_o_ciclo_vigente(self):
        resp = self.client.get(reverse("preventiva_metas"), {"subtipo": self.subtipo.pk})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["ciclo"], self.ciclo)
        self.assertEqual(resp.context["apuracao"]["previstos"], 5)

    def test_painel_exige_login(self):
        self.client.logout()
        resp = self.client.get(reverse("preventiva_metas"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.url)

    def test_painel_avisa_equipamento_sem_area(self):
        self._switch("SW-SEM-AREA", None)
        resp = self.client.get(reverse("preventiva_metas"), {"subtipo": self.subtipo.pk})
        self.assertContains(resp, "sem área de operação")

    def test_painel_sem_ciclo_oferece_abrir(self):
        resp = self.client.get(reverse("preventiva_metas"), {"subtipo": self.outro_subtipo.pk})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.context["ciclo"])
        self.assertContains(resp, "Abrir ciclo de")

    def test_abrir_ciclo_por_post(self):
        resp = self.client.post(
            reverse("preventiva_metas"),
            {"acao": "abrir_ciclo", "subtipo": self.outro_subtipo.pk, "duracao_dias": 90},
        )
        self.assertEqual(resp.status_code, 302)
        novo = PreventivaCicloMeta.objects.get(subtipo=self.outro_subtipo)
        self.assertEqual(novo.duracao_dias, 90)

    def test_abrir_ciclo_com_um_vigente_encadeia_em_vez_de_sobrepor(self):
        """
        O botão não duplica a janela atual: abre a SEGUINTE, começando no dia
        após o fim desta. É o que permite planejar o próximo ciclo sem
        esperar o atual fechar — e sem dia descoberto entre os dois.
        """
        resp = self.client.post(
            reverse("preventiva_metas"),
            {"acao": "abrir_ciclo", "subtipo": self.subtipo.pk, "duracao_dias": 90},
        )
        self.assertEqual(resp.status_code, 302)
        novo_ciclo = (
            PreventivaCicloMeta.objects
            .filter(subtipo=self.subtipo)
            .exclude(pk=self.ciclo.pk)
            .get()
        )
        self.assertEqual(novo_ciclo.data_inicio, self.ciclo.data_fim + timedelta(days=1))

    def test_abrir_ciclo_sobreposto_nao_cria_e_avisa(self):
        """
        Data explícita que invade o ciclo existente é recusada com mensagem —
        não com 500, e não criando a linha.
        """
        meta_ciclo = PreventivaCicloMeta(
            subtipo=self.subtipo, data_inicio=date(2026, 10, 1), duracao_dias=90
        )
        with self.assertRaises(ValidationError):
            meta_ciclo.full_clean()

        resp = self.client.post(reverse("preventiva_metas_config"), {
            "acao": "salvar_ciclo",
            "subtipo": self.subtipo.pk,
            "data_inicio": "2026-10-01",
            "duracao_dias": 90,
        }, follow=True)
        self.assertEqual(PreventivaCicloMeta.objects.filter(subtipo=self.subtipo).count(), 1)
        self.assertTrue(
            resp.context["form_ciclo"].errors,
            "A tela precisa dizer no campo por que o ciclo não foi aberto.",
        )

    def test_configuracao_abre(self):
        resp = self.client.get(reverse("preventiva_metas_config"))
        self.assertEqual(resp.status_code, 200)

    def test_salvar_responsavel_pela_tela(self):
        resp = self.client.post(reverse("preventiva_metas_config"), {
            "acao": "salvar_responsavel",
            "area": AreaOperacionalChoices.PRODUTIVA,
            "tecnico": self.tec_prod.pk,
            "subtipo": "",
            "ativo": "on",
        })
        self.assertEqual(resp.status_code, 302)
        linha = PreventivaAreaResponsavel.objects.get(area=AreaOperacionalChoices.PRODUTIVA)
        self.assertEqual(linha.tecnico, self.tec_prod)
        self.assertIsNone(linha.subtipo)

    def test_responsavel_duplicado_vira_erro_de_campo_e_nao_500(self):
        PreventivaAreaResponsavel.objects.create(
            area=AreaOperacionalChoices.PRODUTIVA, tecnico=self.tec_prod
        )
        resp = self.client.post(reverse("preventiva_metas_config"), {
            "acao": "salvar_responsavel",
            "area": AreaOperacionalChoices.PRODUTIVA,
            "tecnico": self.tec_apoio.pk,
            "subtipo": "",
            "ativo": "on",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["form_responsavel"].errors)
        self.assertEqual(PreventivaAreaResponsavel.objects.count(), 1)

    def test_fornecedor_nao_aparece_como_tecnico(self):
        """Login de portal externo não executa preventiva na fazenda."""
        from ProjetoEstoque.forms import PreventivaAreaResponsavelForm

        grupo, _ = Group.objects.get_or_create(name=GRUPO_FORNECEDOR)
        externo = User.objects.create_user("fornecedor_x", password="x")
        externo.groups.add(grupo)
        oferecidos = set(PreventivaAreaResponsavelForm().fields["tecnico"].queryset)
        self.assertNotIn(externo, oferecidos)
        self.assertIn(self.tec_prod, oferecidos)

    def test_exportacao_responde_planilha(self):
        self._executar(self.produtivos[0], date(2026, 9, 10), tecnico=self.tec_prod)
        resp = self.client.get(
            reverse("preventiva_metas_export"),
            {"subtipo": self.subtipo.pk, "ciclo": self.ciclo.pk},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("spreadsheetml", resp["Content-Type"])
        self.assertIn("attachment", resp["Content-Disposition"])

    def test_painel_nao_dispara_consulta_por_equipamento(self):
        """
        O painel monta a lista de pendentes, o crédito por técnico e a curva
        de evolução. Feito por linha, isso é N+1 e a tela degrada junto com o
        parque — que é exatamente o que ela existe para medir.

        O teste mede a PROPRIEDADE (custo constante), não um número mágico:
        travar "são 7 consultas" quebraria a cada `select_related` legítimo e
        ninguém saberia se o N+1 voltou ou se alguém só otimizou algo.
        """
        PreventivaAreaResponsavel.objects.create(
            area=AreaOperacionalChoices.PRODUTIVA, tecnico=self.tec_prod
        )
        self._executar(self.produtivos[0], date(2026, 9, 10), tecnico=self.tec_prod)

        with CaptureQueriesContext(connection) as pequeno:
            meta.apurar_ciclo(self.ciclo, self.hoje)
        base = len(pequeno)

        for i in range(40):
            self._switch(f"SW-EXTRA-{i}", AreaOperacionalChoices.PRODUTIVA)
            self._executar(Item.objects.get(nome=f"SW-EXTRA-{i}"), date(2026, 9, 12))

        with CaptureQueriesContext(connection) as grande:
            apuracao = meta.apurar_ciclo(self.ciclo, self.hoje)

        self.assertEqual(apuracao["previstos"], 45)
        self.assertEqual(
            len(grande), base,
            f"O parque passou de 5 para 45 ativos e o custo foi de {base} para "
            f"{len(grande)} consultas — há N+1 na apuração.",
        )


class PlanoDeAgendamentoTests(BaseMetaTests):
    """Integração com o fluxo que já existia."""

    def test_agendamento_em_lote_herda_o_responsavel_da_area(self):
        PreventivaAreaResponsavel.objects.create(
            area=AreaOperacionalChoices.PRODUTIVA, tecnico=self.tec_prod
        )
        PreventivaAreaResponsavel.objects.create(
            area=AreaOperacionalChoices.NAO_PRODUTIVA, tecnico=self.tec_apoio
        )
        CheckListModelo.objects.filter(pk=self.checklist.pk).update(subtipo=self.subtipo)

        resp = self.client.post(reverse("preventiva_plano"), {
            "acao": "agendar",
            "data_agendamento": "2026-10-20",
            "item_ids": [self.produtivos[0].pk, self.nao_produtivos[0].pk],
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(
            Preventiva.objects.get(equipamento=self.produtivos[0]).tecnico, self.tec_prod
        )
        self.assertEqual(
            Preventiva.objects.get(equipamento=self.nao_produtivos[0]).tecnico, self.tec_apoio
        )

    def test_tecnico_escolhido_no_formulario_vence_a_area(self):
        PreventivaAreaResponsavel.objects.create(
            area=AreaOperacionalChoices.PRODUTIVA, tecnico=self.tec_prod
        )
        CheckListModelo.objects.filter(pk=self.checklist.pk).update(subtipo=self.subtipo)

        self.client.post(reverse("preventiva_plano"), {
            "acao": "agendar",
            "data_agendamento": "2026-10-20",
            "tecnico_id": self.tec_apoio.pk,
            "item_ids": [self.produtivos[0].pk],
        })
        self.assertEqual(
            Preventiva.objects.get(equipamento=self.produtivos[0]).tecnico, self.tec_apoio
        )

    def test_atribuicao_existente_nao_e_sobrescrita_pela_area(self):
        """O cadastro de área é padrão, não carimbo: não apaga decisão de alguém."""
        PreventivaAreaResponsavel.objects.create(
            area=AreaOperacionalChoices.PRODUTIVA, tecnico=self.tec_prod
        )
        CheckListModelo.objects.filter(pk=self.checklist.pk).update(subtipo=self.subtipo)
        Preventiva.objects.create(
            equipamento=self.produtivos[0],
            checklist_modelo=self.checklist,
            tecnico=self.tec_apoio,
        )

        self.client.post(reverse("preventiva_plano"), {
            "acao": "agendar",
            "data_agendamento": "2026-10-20",
            "item_ids": [self.produtivos[0].pk],
        })
        self.assertEqual(
            Preventiva.objects.get(equipamento=self.produtivos[0]).tecnico, self.tec_apoio
        )

    def test_filtro_por_area_no_plano(self):
        resp = self.client.get(reverse("preventiva_plano"), {"area": AreaOperacionalChoices.PRODUTIVA})
        self.assertEqual(resp.status_code, 200)
        nomes = {r.equipamento.nome for r in resp.context["page_obj"]}
        self.assertEqual(nomes, {"SW-PROD-0", "SW-PROD-1", "SW-PROD-2"})

    def test_filtro_sem_area_no_plano(self):
        self._switch("SW-SEM-AREA", None)
        resp = self.client.get(reverse("preventiva_plano"), {"area": "sem"})
        nomes = {r.equipamento.nome for r in resp.context["page_obj"]}
        self.assertEqual(nomes, {"SW-SEM-AREA"})

    def test_ficha_do_equipamento_mostra_o_responsavel(self):
        PreventivaAreaResponsavel.objects.create(
            area=AreaOperacionalChoices.PRODUTIVA, tecnico=self.tec_prod
        )
        resp = self.client.get(
            reverse("equipamento_detalhe", args=[self.produtivos[0].pk])
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["area_responsavel"], self.tec_prod)


class FormularioDoItemTests(BaseMetaTests):
    """A classificação é feita no cadastro do equipamento."""

    def test_area_e_opcional_e_a_opcao_vazia_tem_nome(self):
        from ProjetoEstoque.forms import ItemForm

        form = ItemForm()
        campo = form.fields["area_operacional"]
        self.assertFalse(campo.required)
        self.assertEqual(list(campo.choices)[0], ("", "Não classificada"))

    def test_select_vazio_grava_nulo_e_nao_string_vazia(self):
        """
        Duas representações de "não classificado" obrigariam todo filtro de
        área a conhecer as duas — e a primeira que esquecesse erraria a conta.
        """
        from ProjetoEstoque.forms import ItemForm

        form = ItemForm(instance=self.produtivos[0], data={
            "nome": "SW-PROD-0",
            "locado": SimNaoChoices.NAO,
            "item_consumo": SimNaoChoices.NAO,
            "pmb": SimNaoChoices.NAO,
            "status": StatusItemChoices.ATIVO,
            "quantidade": 1,
            "subtipo": self.subtipo.pk,
            "localidade": self.local.pk,
            "precisa_preventiva": SimNaoChoices.SIM,
            "data_limite_preventiva": 90,
            "area_operacional": "",
            "compartilhado": "False",
        })
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.cleaned_data["area_operacional"])
        item = form.save()
        item.refresh_from_db()
        self.assertIsNone(item.area_operacional)


class MovimentacaoDoParqueTests(BaseMetaTests):
    """
    Equipamento parado não conta, e quem entrou no meio do ciclo não é cobrado
    como se estivesse ali desde o primeiro dia.
    """

    def _mover_status(self, item, de, para, quando):
        """
        Move o status gerando o `ItemStatusHistorico` REAL (via signal) e depois
        reposiciona o carimbo, que é `auto_now_add` e sempre nasce em "agora".

        O status de ORIGEM é gravado sem passar pelo signal (`.update()`): a
        primeira versão fazia dois `save()` e a preparação do cenário gerava
        uma transição fantasma — um "reserva" criado como ativo e movido para
        backup aparecia como SAÍDA do ciclo, e os testes de troca mediam um
        movimento que o teste tinha inventado.
        """
        from ProjetoEstoque.models import ItemStatusHistorico

        Item.objects.filter(pk=item.pk).update(status=de)
        item.refresh_from_db()
        item.status = para
        item.save(update_fields=["status"])
        linha = (
            ItemStatusHistorico.objects
            .filter(item=item, status_anterior=de, status_novo=para)
            .order_by("-alterado_em")
            .first()
        )
        assert linha is not None, "o signal nao gravou o historico de status"
        ItemStatusHistorico.objects.filter(pk=linha.pk).update(
            alterado_em=timezone.make_aware(datetime(quando.year, quando.month, quando.day, 12, 0))
        )
        return linha

    # ── Entrada em operação ──────────────────────────────────────────────
    def test_entrada_em_operacao_e_detectada(self):
        self._mover_status(
            self.produtivos[0], StatusItemChoices.BACKUP,
            StatusItemChoices.ATIVO, date(2026, 10, 10),
        )
        entradas = meta.entradas_em_operacao(self.subtipo.pk, self.ciclo)
        self.assertEqual(entradas.get(self.produtivos[0].pk), date(2026, 10, 10))

    def test_cadastro_novo_nao_conta_como_entrada(self):
        """
        O signal grava `status_anterior=''` ao criar o Item. Tratar isso como
        "entrou em operação" marcaria TODO equipamento cadastrado durante o
        ciclo como recém-chegado, e a expectativa da área despencaria sem
        nenhuma troca ter acontecido.
        """
        novo = self._switch("SW-RECEM-CADASTRADO", AreaOperacionalChoices.PRODUTIVA)
        entradas = meta.entradas_em_operacao(self.subtipo.pk, self.ciclo)
        self.assertNotIn(novo.pk, entradas)

    def test_entrada_fora_da_janela_nao_conta(self):
        self._mover_status(
            self.produtivos[0], StatusItemChoices.BACKUP,
            StatusItemChoices.ATIVO, date(2026, 8, 1),
        )
        self.assertEqual(meta.entradas_em_operacao(self.subtipo.pk, self.ciclo), {})

    def test_dias_em_operacao_conta_so_a_partir_da_entrada(self):
        self._mover_status(
            self.produtivos[0], StatusItemChoices.BACKUP,
            StatusItemChoices.ATIVO, date(2026, 10, 15),
        )
        ap = meta.apurar_ciclo(self.ciclo, self.hoje)
        novo = next(
            i for a in ap["areas"] for i in a["itens_pendentes"]
            if i.pk == self.produtivos[0].pk
        )
        self.assertTrue(novo.meta_novo_no_ciclo)
        self.assertEqual(novo.meta_entrou_em, date(2026, 10, 15))
        # 15, 16 e 17 de outubro.
        self.assertEqual(novo.meta_dias_operacao, 3)

    def test_quem_estava_ativo_desde_o_inicio_tem_o_ciclo_inteiro(self):
        ap = meta.apurar_ciclo(self.ciclo, self.hoje)
        item = next(
            i for a in ap["areas"] for i in a["itens_pendentes"]
            if i.pk == self.produtivos[1].pk
        )
        self.assertFalse(item.meta_novo_no_ciclo)
        self.assertEqual(item.meta_dias_operacao, ap["dias_decorridos"])

    # ── Expectativa ajustada ─────────────────────────────────────────────
    def test_sem_movimentacao_a_expectativa_e_o_prazo_decorrido(self):
        """O caso comum não muda: sem troca, expectativa == pct_tempo."""
        ap = meta.apurar_ciclo(self.ciclo, self.hoje)
        self.assertEqual(ap["expectativa"], ap["pct_tempo"])
        self.assertFalse(ap["expectativa_ajustada"])
        for area in ap["areas"]:
            self.assertEqual(area["expectativa"], ap["pct_tempo"])

    def test_entrada_recente_derruba_a_expectativa_da_area(self):
        self._mover_status(
            self.produtivos[0], StatusItemChoices.BACKUP,
            StatusItemChoices.ATIVO, date(2026, 10, 15),
        )
        ap = meta.apurar_ciclo(self.ciclo, self.hoje)
        prod = self._area(ap, AreaOperacionalChoices.PRODUTIVA)
        apoio = self._area(ap, AreaOperacionalChoices.NAO_PRODUTIVA)
        self.assertLess(prod["expectativa"], ap["pct_tempo"])
        # A área que não se mexeu continua medida pelo calendário.
        self.assertEqual(apoio["expectativa"], ap["pct_tempo"])
        self.assertTrue(ap["expectativa_ajustada"])
        self.assertEqual(ap["entraram"], 1)

    def test_equipamento_que_subiu_ontem_nao_faz_a_area_parecer_atrasada(self):
        """
        O caso que motivou a regra: a área está em dia, um reserva sobe no dia
        85 e, medido pelo calendário, o técnico aparece atrasado por um
        equipamento que não existia na operação até ontem.
        """
        for item in self.produtivos:
            self._executar(item, date(2026, 9, 10))
        em_dia = meta.apurar_ciclo(self.ciclo, self.hoje)
        prod_antes = self._area(em_dia, AreaOperacionalChoices.PRODUTIVA)
        self.assertEqual(prod_antes["risco"], "concluido")

        novo = self._switch("SW-RESERVA-QUE-SUBIU", AreaOperacionalChoices.PRODUTIVA)
        self._mover_status(
            novo, StatusItemChoices.BACKUP, StatusItemChoices.ATIVO, date(2026, 10, 16),
        )
        ap = meta.apurar_ciclo(self.ciclo, self.hoje)
        prod = self._area(ap, AreaOperacionalChoices.PRODUTIVA)

        self.assertEqual(prod["previstos"], 4)      # ele PRECISA de preventiva
        self.assertEqual(prod["pendentes"], 1)
        self.assertEqual(prod["pct"], 75)
        # Medido pelo calendário (52%), 75% seria "em dia" — mas o ponto é que
        # a expectativa caiu junto, então o veredito não vira atraso.
        self.assertLess(prod["expectativa"], em_dia["pct_tempo"])
        self.assertIn(prod["risco"], ("em_dia", "concluido"))

    # ── Saídas e trocas ──────────────────────────────────────────────────
    def test_saida_de_operacao_e_listada(self):
        self._mover_status(
            self.produtivos[0], StatusItemChoices.ATIVO,
            StatusItemChoices.DEFEITO, date(2026, 10, 12),
        )
        saidas = meta.saidas_de_operacao(self.subtipo.pk, self.ciclo)
        self.assertEqual(len(saidas), 1)
        self.assertEqual(saidas[0]["item"].pk, self.produtivos[0].pk)
        self.assertEqual(saidas[0]["data"], date(2026, 10, 12))
        self.assertEqual(saidas[0]["status"], StatusItemChoices.DEFEITO)

    def test_item_que_saiu_deixa_o_universo_da_meta(self):
        """Regra 2 do pedido: equipamento fora de operação não é cobrado."""
        antes = meta.universo_qs(self.subtipo.pk).count()
        self._mover_status(
            self.produtivos[0], StatusItemChoices.ATIVO,
            StatusItemChoices.DEFEITO, date(2026, 10, 12),
        )
        self.assertEqual(meta.universo_qs(self.subtipo.pk).count(), antes - 1)

    def test_troca_e_identificada_na_mesma_localidade(self):
        reserva = self._switch("SW-RESERVA", AreaOperacionalChoices.PRODUTIVA)
        self._mover_status(
            self.produtivos[0], StatusItemChoices.ATIVO,
            StatusItemChoices.DEFEITO, date(2026, 10, 12),
        )
        self._mover_status(
            reserva, StatusItemChoices.BACKUP, StatusItemChoices.ATIVO, date(2026, 10, 13),
        )
        ap = meta.apurar_ciclo(self.ciclo, self.hoje)
        self.assertEqual(len(ap["trocas"]), 1)
        troca = ap["trocas"][0]
        self.assertEqual(troca["saiu"].pk, self.produtivos[0].pk)
        self.assertEqual(troca["entrou"].pk, reserva.pk)
        self.assertEqual(troca["dias"], 1)
        # Casada, a saída não aparece de novo como "saiu sem substituto".
        self.assertEqual(ap["saidas"], [])

    def test_localidade_diferente_nao_vira_troca(self):
        """
        Sem a localidade, duas mudanças independentes na mesma semana virariam
        uma troca inventada — e o painel afirmaria algo que não aconteceu.
        """
        outro_local = Localidade.objects.create(local="ESCRITORIO")
        reserva = self._switch("SW-DE-OUTRO-LUGAR", AreaOperacionalChoices.PRODUTIVA)
        Item.objects.filter(pk=reserva.pk).update(localidade=outro_local)
        reserva.refresh_from_db()

        self._mover_status(
            self.produtivos[0], StatusItemChoices.ATIVO,
            StatusItemChoices.DEFEITO, date(2026, 10, 12),
        )
        self._mover_status(
            reserva, StatusItemChoices.BACKUP, StatusItemChoices.ATIVO, date(2026, 10, 13),
        )
        ap = meta.apurar_ciclo(self.ciclo, self.hoje)
        self.assertEqual(ap["trocas"], [])
        self.assertEqual(len(ap["saidas"]), 1)

    def test_distancia_maior_que_a_janela_nao_vira_troca(self):
        reserva = self._switch("SW-RESERVA-TARDE", AreaOperacionalChoices.PRODUTIVA)
        self._mover_status(
            self.produtivos[0], StatusItemChoices.ATIVO,
            StatusItemChoices.DEFEITO, date(2026, 10, 1),
        )
        self._mover_status(
            reserva, StatusItemChoices.BACKUP, StatusItemChoices.ATIVO, date(2026, 10, 15),
        )
        ap = meta.apurar_ciclo(self.ciclo, self.hoje)
        self.assertEqual(ap["trocas"], [])

    def test_equipamento_que_oscila_nao_substitui_a_si_mesmo(self):
        """
        Um ativo que sai e volta na mesma semana (manutenção rápida) gera uma
        saída E uma entrada no mesmo lugar, a poucos dias de distância — a
        assinatura exata de uma troca. Sem a guarda, o painel afirmaria que o
        equipamento substituiu a si mesmo.
        """
        from ProjetoEstoque.models import ItemStatusHistorico

        alvo = self.produtivos[0]
        self._mover_status(
            alvo, StatusItemChoices.ATIVO, StatusItemChoices.MANUTENCAO, date(2026, 10, 10),
        )
        self._mover_status(
            alvo, StatusItemChoices.MANUTENCAO, StatusItemChoices.ATIVO, date(2026, 10, 13),
        )
        ap = meta.apurar_ciclo(self.ciclo, self.hoje)
        self.assertEqual(ap["trocas"], [])
        # Ele voltou: continua previsto, e contado só pelo tempo disponível.
        self.assertEqual(ap["previstos"], 5)
        self.assertEqual(ap["entraram"], 1)

    def test_resumo_concorda_com_a_apuracao_apos_troca(self):
        """
        Com a expectativa ajustada, `resumo_ciclo` tinha de aprender a mesma
        conta — senão a lista de ciclos classificaria como atrasado um ciclo
        que o painel mostra em dia.
        """
        self._mover_status(
            self.produtivos[0], StatusItemChoices.BACKUP,
            StatusItemChoices.ATIVO, date(2026, 10, 15),
        )
        ap = meta.apurar_ciclo(self.ciclo, self.hoje)
        resumo = meta.resumo_ciclo(self.ciclo, self.hoje)
        self.assertEqual(ap["expectativa"], resumo["expectativa"])
        self.assertEqual(ap["risco"], resumo["risco"])

    def test_painel_mostra_a_movimentacao(self):
        reserva = self._switch("SW-RESERVA", AreaOperacionalChoices.PRODUTIVA)
        self._mover_status(
            self.produtivos[0], StatusItemChoices.ATIVO,
            StatusItemChoices.DEFEITO, date(2026, 10, 12),
        )
        self._mover_status(
            reserva, StatusItemChoices.BACKUP, StatusItemChoices.ATIVO, date(2026, 10, 13),
        )
        resp = self.client.get(reverse("preventiva_metas"), {"subtipo": self.subtipo.pk})
        self.assertContains(resp, "Movimentação do parque no ciclo")
        self.assertContains(resp, "SW-RESERVA")
        self.assertContains(resp, "Esperado agora")


class EdicaoDeCicloTests(BaseMetaTests):
    """Mexer na janela mexe no placar — e isso precisa ser dito antes."""

    def test_impacto_detecta_execucao_que_sai_da_janela(self):
        self._executar(self.produtivos[0], date(2026, 9, 5))
        # Encurtar o início para depois da execução a tira da apuração.
        impacto = meta.impacto_de_editar_ciclo(self.ciclo, date(2026, 9, 20), 90)
        self.assertEqual(impacto["saem"], 1)
        self.assertEqual(impacto["entram"], 0)
        self.assertTrue(impacto["muda_placar"])

    def test_impacto_zero_quando_a_janela_ainda_cobre_tudo(self):
        self._executar(self.produtivos[0], date(2026, 9, 5))
        impacto = meta.impacto_de_editar_ciclo(self.ciclo, date(2026, 9, 1), 120)
        self.assertFalse(impacto["muda_placar"])

    def test_editar_ciclo_altera_a_janela(self):
        meta.editar_ciclo(self.ciclo, date(2026, 9, 10), 60, usuario=self.gestor)
        self.ciclo.refresh_from_db()
        self.assertEqual(self.ciclo.data_inicio, date(2026, 9, 10))
        self.assertEqual(self.ciclo.duracao_dias, 60)
        self.assertEqual(self.ciclo.data_fim, date(2026, 11, 8))

    def test_editar_ciclo_recusa_sobreposicao(self):
        PreventivaCicloMeta.objects.create(
            subtipo=self.subtipo, data_inicio=date(2027, 1, 1), duracao_dias=90
        )
        with self.assertRaises(ValidationError):
            meta.editar_ciclo(self.ciclo, date(2026, 11, 1), 90, usuario=self.gestor)

    def test_tela_abre_o_formulario_preenchido(self):
        resp = self.client.get(
            reverse("preventiva_metas_config"), {"editar_ciclo": self.ciclo.pk}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["editando_ciclo"], self.ciclo)
        self.assertContains(resp, "Editando o ciclo de")

    def test_subtipo_fica_travado_na_edicao(self):
        """
        Trocar o subtipo reapontaria o ciclo para outro parque, e a apuração
        histórica passaria a falar de equipamentos que nunca estiveram nele.
        """
        from ProjetoEstoque.forms import PreventivaCicloMetaForm

        self.assertTrue(PreventivaCicloMetaForm(instance=self.ciclo).fields["subtipo"].disabled)
        self.assertFalse(PreventivaCicloMetaForm().fields["subtipo"].disabled)

    def test_edicao_que_muda_o_placar_pede_confirmacao(self):
        self._executar(self.produtivos[0], date(2026, 9, 5))
        resp = self.client.post(reverse("preventiva_metas_config"), {
            "acao": "salvar_ciclo",
            "ciclo_id": self.ciclo.pk,
            "data_inicio": "2026-09-20",
            "duracao_dias": 90,
        })
        self.assertEqual(resp.status_code, 200)         # não redirecionou: não salvou
        self.assertIsNotNone(resp.context["impacto"])
        self.assertEqual(resp.context["impacto"]["saem"], 1)
        self.ciclo.refresh_from_db()
        self.assertEqual(self.ciclo.data_inicio, date(2026, 9, 1))
        self.assertContains(resp, "muda a apuração do ciclo")

    def test_edicao_confirmada_e_aplicada(self):
        self._executar(self.produtivos[0], date(2026, 9, 5))
        resp = self.client.post(reverse("preventiva_metas_config"), {
            "acao": "salvar_ciclo",
            "ciclo_id": self.ciclo.pk,
            "data_inicio": "2026-09-20",
            "duracao_dias": 90,
            "confirmar_impacto": "1",
        })
        self.assertEqual(resp.status_code, 302)
        self.ciclo.refresh_from_db()
        self.assertEqual(self.ciclo.data_inicio, date(2026, 9, 20))

    def test_edicao_sem_impacto_salva_direto(self):
        resp = self.client.post(reverse("preventiva_metas_config"), {
            "acao": "salvar_ciclo",
            "ciclo_id": self.ciclo.pk,
            "data_inicio": "2026-09-01",
            "duracao_dias": 120,
        })
        self.assertEqual(resp.status_code, 302)
        self.ciclo.refresh_from_db()
        self.assertEqual(self.ciclo.duracao_dias, 120)

    def test_criar_ciclo_continua_funcionando_sem_ciclo_id(self):
        # O seletor do formulário só oferece subtipo COM parque no programa de
        # preventiva (é o que mantém a lista de 69 subtipos utilizável), então
        # o cenário precisa de um access point elegível.
        self._switch("AP-PROD", AreaOperacionalChoices.PRODUTIVA, subtipo=self.outro_subtipo)
        resp = self.client.post(reverse("preventiva_metas_config"), {
            "acao": "salvar_ciclo",
            "subtipo": self.outro_subtipo.pk,
            "data_inicio": "2026-10-01",
            "duracao_dias": 90,
        })
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(
            PreventivaCicloMeta.objects.filter(subtipo=self.outro_subtipo).exists()
        )


class InterfaceDasTelasNovasTests(BaseMetaTests):
    """Contratos de interface que falham em silêncio."""

    def test_campo_de_area_aparece_no_cadastro_do_equipamento(self):
        """
        O campo esteve no ModelForm e FORA do template: a tela abria sem ele e
        não havia como classificar equipamento nenhum. O form validava, o
        sistema não acusava nada, e a meta ficava permanentemente em 0%.
        """
        resp = self.client.get(reverse("item_update", args=[self.produtivos[0].pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'name="area_operacional"')
        self.assertContains(resp, "Área de Operação")

    def test_ids_dos_dois_formularios_nao_colidem(self):
        """
        Os dois formulários da tela de configuração têm campo `subtipo`. Com o
        auto_id padrão os dois sairiam como `id_subtipo`: o <label for> aponta
        para o campo errado e o seletor do JS acerta só o primeiro.
        """
        resp = self.client.get(reverse("preventiva_metas_config"))
        corpo = resp.content.decode("utf-8")
        self.assertEqual(corpo.count('id="id_resp_subtipo"'), 1)
        self.assertEqual(corpo.count('id="id_ciclo_subtipo"'), 1)
        self.assertNotIn('id="id_subtipo"', corpo)

    def test_select2_carregado_nas_duas_telas(self):
        for rota in ("preventiva_metas", "preventiva_metas_config"):
            resp = self.client.get(reverse(rota))
            corpo = resp.content.decode("utf-8")
            self.assertIn("select2.min.js", corpo, rota)
            self.assertIn("select2.min.css", corpo, rota)
            # A classe é o que liga a folha do módulo ao componente, inclusive
            # no dropdown, que o Select2 injeta fora da página.
            self.assertIn("pvm-s2", corpo, rota)

    def test_folha_do_modulo_vem_depois_do_select2(self):
        """Invertida a ordem, o CSS do CDN vence e o campo sai sem os tokens."""
        resp = self.client.get(reverse("preventiva_metas_config"))
        corpo = resp.content.decode("utf-8")
        self.assertLess(
            corpo.index("select2.min.css"), corpo.index("ds-preventivas.css"),
            "ds-preventivas.css precisa ser carregado DEPOIS do select2.min.css",
        )

