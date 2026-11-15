"""
Responsáveis por Área: a lista de equipamentos de cada técnico.

Pedido de 15/11/2026: mostrar a cada técnico, de forma minimalista, os
equipamentos que ele responde — com o que ele precisa saber para decidir a
próxima visita.

O que erra em silêncio aqui:

1. **De quem é o ativo.** A tela tem de usar a MESMA regra de
   `responsaveis_map`/`responsavel_do_item` (linha específica do subtipo vence
   a padrão da área; linha inativa não conta). Uma segunda regra, ainda que
   "quase igual", mostraria o switch na lista de um técnico e cobraria a meta
   de outro.
2. **Feito ou pendente.** Com ciclo vigente quem diz é `apurar_ciclo`
   (regras 44 e 47). A lista não pode ter conta própria de previsto/realizado.
3. **Fora de operação não é visita.** O ativo parado continua sendo do
   técnico, mas não entra como pendência (o modelo congela a contagem).
4. **Custo fixo.** Montar a lista por linha seria uma consulta por
   equipamento numa tela que existe para mostrar o parque inteiro.
5. **Só leitura.** Abrir a tela não grava nada.
"""
from datetime import date, timedelta

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from ProjetoEstoque.models import (
    AreaOperacionalChoices,
    Item,
    Localidade,
    Preventiva,
    PreventivaAreaResponsavel,
    PreventivaExecucao,
    SimNaoChoices,
    StatusItemChoices,
)
from services import preventiva_meta_service as meta

from .test_preventiva_metas import BaseMetaTests

PROD = AreaOperacionalChoices.PRODUTIVA
APOIO = AreaOperacionalChoices.NAO_PRODUTIVA


class _Parque(BaseMetaTests):
    """Ana (produtiva) e Bruno (não produtiva) respondem pelas duas áreas, padrão."""

    def setUp(self):
        super().setUp()
        self.linha_prod = PreventivaAreaResponsavel.objects.create(area=PROD, tecnico=self.tec_prod)
        self.linha_apoio = PreventivaAreaResponsavel.objects.create(area=APOIO, tecnico=self.tec_apoio)

    def _parque(self, **kw):
        return meta.parque_por_responsavel(kw.pop("hoje", self.hoje), **kw)

    def _bucket(self, parque, tecnico):
        return next(b for b in parque["tecnicos"] if b["id"] == tecnico.pk)

    def _linhas(self, bucket):
        """Todas as linhas de equipamento do técnico, em qualquer grupo."""
        pend = [l for g in bucket["locais"] for l in g["itens"]]
        return pend + bucket["em_dia"] + bucket["fora_de_operacao"]

    def _linha(self, parque, tecnico, nome):
        return next(l for l in self._linhas(self._bucket(parque, tecnico)) if l["nome"] == nome)


class AtribuicaoTests(_Parque):
    def test_cada_equipamento_vai_para_o_tecnico_da_area(self):
        p = self._parque()
        self.assertEqual({l["nome"] for l in self._linhas(self._bucket(p, self.tec_prod))},
                         {"SW-PROD-0", "SW-PROD-1", "SW-PROD-2"})
        self.assertEqual({l["nome"] for l in self._linhas(self._bucket(p, self.tec_apoio))},
                         {"SW-APOIO-0", "SW-APOIO-1"})
        self.assertEqual(p["sem_responsavel"], [])

    def test_mesma_regra_de_responsavel_do_item(self):
        """
        Linha específica vence a padrão; linha inativa não conta. A tela e
        `responsavel_do_item` (que o Plano de Agendamento usa) não podem divergir.
        """
        # Access-Point não produtivo é do Ana (específica), por cima do padrão do Bruno.
        PreventivaAreaResponsavel.objects.create(area=APOIO, subtipo=self.outro_subtipo, tecnico=self.tec_prod)
        # Linha INATIVA: não pode decidir nada.
        PreventivaAreaResponsavel.objects.create(
            area=PROD, subtipo=self.outro_subtipo, tecnico=self.tec_apoio, ativo=False
        )
        ap_apoio = self._switch("AP-APOIO", APOIO, subtipo=self.outro_subtipo)
        ap_prod = self._switch("AP-PROD", PROD, subtipo=self.outro_subtipo)

        p = self._parque()
        dono_na_tela = {}
        for b in p["tecnicos"]:
            for l in self._linhas(b):
                dono_na_tela[l["pk"]] = b["id"]

        for item in Item.objects.filter(area_operacional__in=[PROD, APOIO]):
            esperado = meta.responsavel_do_item(item)
            self.assertEqual(dono_na_tela.get(item.pk), esperado.pk if esperado else None, item.nome)
        # os dois casos que a regra existe para resolver:
        self.assertEqual(dono_na_tela[ap_apoio.pk], self.tec_prod.pk)    # específica vence
        self.assertEqual(dono_na_tela[ap_prod.pk], self.tec_prod.pk)     # inativa ignorada → padrão

    def test_sem_responsavel_vai_para_o_aviso_com_a_contagem(self):
        self.linha_apoio.delete()
        p = self._parque()
        self.assertEqual(p["sem_responsavel_total"], 2)
        self.assertEqual(len(p["sem_responsavel"]), 1)
        vago = p["sem_responsavel"][0]
        self.assertEqual((vago["area"], vago["subtipo"], vago["total"]), (APOIO, "Switches", 2))

    def test_responsabilidade_inativa_nao_cria_cartao(self):
        self.linha_apoio.ativo = False
        self.linha_apoio.save()
        p = self._parque()
        self.assertNotIn(self.tec_apoio.pk, [b["id"] for b in p["tecnicos"]])
        self.assertEqual(p["sem_responsavel_total"], 2)

    def test_so_entra_quem_esta_no_programa_e_classificado(self):
        self._switch("SW-FORA-DO-PROGRAMA", PROD, precisa=SimNaoChoices.NAO)
        self._switch("SW-SEM-AREA", None)
        nomes = {l["nome"] for b in self._parque()["tecnicos"] for l in self._linhas(b)}
        self.assertNotIn("SW-FORA-DO-PROGRAMA", nomes)
        self.assertNotIn("SW-SEM-AREA", nomes)

    def test_tecnico_sem_equipamento_aparece_com_zero(self):
        novo = User.objects.create_user("tec_novo", password="x", first_name="Caio", last_name="Novo")
        PreventivaAreaResponsavel.objects.create(area=PROD, subtipo=self.outro_subtipo, tecnico=novo)
        b = self._bucket(self._parque(), novo)
        self.assertEqual((b["total"], b["pendentes"]), (0, 0))
        self.assertEqual(b["locais"], [])

    def test_escopos_dizem_area_e_subtipo(self):
        PreventivaAreaResponsavel.objects.create(area=APOIO, subtipo=self.outro_subtipo, tecnico=self.tec_prod)
        escopos = {(e["area"], e["subtipo"]) for e in self._bucket(self._parque(), self.tec_prod)["escopos"]}
        self.assertEqual(escopos, {(PROD, None), (APOIO, "Access-Point")})
        self.assertTrue(self._bucket(self._parque(), self.tec_prod)["varias_areas"])

    def test_viewer_vem_primeiro(self):
        self.assertEqual(self._parque(primeiro_id=self.tec_apoio.pk)["tecnicos"][0]["id"], self.tec_apoio.pk)
        self.assertTrue(self._parque(primeiro_id=self.tec_apoio.pk)["tecnicos"][0]["voce"])
        self.assertFalse(any(b["voce"] for b in self._parque()["tecnicos"]))


class EstadoComCicloTests(_Parque):
    """O subtipo Switches tem ciclo vigente (set-nov): `apurar_ciclo` decide."""

    def test_feita_e_pendente(self):
        self._executar(self.produtivos[0], date(2026, 10, 1), tecnico=self.tec_prod)
        b = self._bucket(self._parque(), self.tec_prod)
        self.assertEqual((b["total"], b["feitos"], b["pendentes"]), (3, 1, 2))
        feita = self._linha(self._parque(), self.tec_prod, "SW-PROD-0")
        self.assertEqual(feita["etiqueta"], "Feita em 01/10")
        self.assertEqual((feita["grupo"], feita["tom"]), ("em_dia", "ok"))

    def test_feita_por_outro_tecnico_diz_quem(self):
        self._executar(self.produtivos[0], date(2026, 10, 1), tecnico=self.tec_apoio)
        feita = self._linha(self._parque(), self.tec_prod, "SW-PROD-0")
        self.assertEqual(feita["etiqueta"], "Feita em 01/10 · Bruno Apoio")

    def test_pendente_sem_preventiva(self):
        pend = self._linha(self._parque(), self.tec_prod, "SW-PROD-1")
        self.assertEqual((pend["grupo"], pend["tom"], pend["etiqueta"]), ("pendente", "aviso", "Sem preventiva"))

    def test_pendente_agendada_venceu_e_sem_agenda(self):
        Preventiva.objects.create(equipamento=self.produtivos[0], checklist_modelo=self.checklist,
                                  data_agendamento=date(2026, 10, 25))
        Preventiva.objects.create(equipamento=self.produtivos[1], checklist_modelo=self.checklist,
                                  data_agendamento=date(2026, 10, 10))
        Preventiva.objects.create(equipamento=self.produtivos[2], checklist_modelo=self.checklist)
        p = self._parque()
        agendada = self._linha(p, self.tec_prod, "SW-PROD-0")
        venceu = self._linha(p, self.tec_prod, "SW-PROD-1")
        sem = self._linha(p, self.tec_prod, "SW-PROD-2")
        self.assertEqual((agendada["tom"], agendada["etiqueta"]), ("info", "Agendada 25/10"))
        self.assertEqual((venceu["tom"], venceu["etiqueta"]), ("perigo", "Venceu 10/10"))
        self.assertEqual((sem["tom"], sem["etiqueta"]), ("neutro", "Sem agenda"))
        self.assertEqual(self._bucket(p, self.tec_prod)["atrasados"], 1)

    def test_mesmos_textos_do_painel_de_metas(self):
        """O painel diz 'Sem preventiva' / 'Venceu dd/mm' / 'dd/mm' / 'Sem agenda'.
        A mesma pendência não pode ter duas linguagens."""
        Preventiva.objects.create(equipamento=self.produtivos[1], checklist_modelo=self.checklist,
                                  data_agendamento=date(2026, 10, 10))
        ap = meta.apurar_ciclo(self.ciclo, self.hoje)
        no_painel = {i.nome: i for i in self._area(ap, PROD)["itens_pendentes"]}
        self.assertTrue(no_painel["SW-PROD-1"].meta_agendamento_vencido)
        self.assertTrue(no_painel["SW-PROD-0"].meta_sem_preventiva)
        p = self._parque()
        self.assertEqual(self._linha(p, self.tec_prod, "SW-PROD-1")["etiqueta"], "Venceu 10/10")
        self.assertEqual(self._linha(p, self.tec_prod, "SW-PROD-0")["etiqueta"], "Sem preventiva")

    def test_totais_batem_com_o_painel_de_metas(self):
        """Previstos/realizados do técnico = os da área que ele responde no painel."""
        self._executar(self.produtivos[0], date(2026, 10, 1), tecnico=self.tec_prod)
        self._executar(self.nao_produtivos[0], date(2026, 10, 2), tecnico=self.tec_apoio)
        ap = meta.apurar_ciclo(self.ciclo, self.hoje)
        p = self._parque()
        for tecnico, area in ((self.tec_prod, PROD), (self.tec_apoio, APOIO)):
            b = self._bucket(p, tecnico)
            a = self._area(ap, area)
            self.assertEqual(b["total"], a["previstos"], tecnico.username)
            self.assertEqual(b["feitos"], a["realizados"], tecnico.username)
            self.assertEqual(b["pendentes"], a["pendentes"], tecnico.username)

    def test_fora_de_operacao_nao_e_pendencia(self):
        self._switch("SW-QUEIMADO", PROD, status=StatusItemChoices.DEFEITO)
        self._switch("SW-OFICINA", PROD, status=StatusItemChoices.MANUTENCAO)
        p = self._parque()
        b = self._bucket(p, self.tec_prod)
        self.assertEqual(b["fora"], 2)
        self.assertEqual(b["pendentes"], 3)                 # só os 3 ativos
        self.assertEqual(b["total"], 5)                     # mas continuam sendo dele
        self.assertEqual({l["nome"] for l in b["fora_de_operacao"]}, {"SW-QUEIMADO", "SW-OFICINA"})
        self.assertEqual(self._linha(p, self.tec_prod, "SW-QUEIMADO")["etiqueta"], "Defeito")
        self.assertEqual(self._linha(p, self.tec_prod, "SW-OFICINA")["etiqueta"], "Manutenção")

    def test_ciclo_encerrado_cai_na_cadencia_individual(self):
        """Fora da janela do ciclo não há prazo comum: vale a data de cada ativo."""
        depois = date(2027, 1, 20)
        pend = self._linha(self._parque(hoje=depois), self.tec_prod, "SW-PROD-0")
        self.assertEqual(pend["etiqueta"], "Sem preventiva")


class CadenciaIndividualTests(_Parque):
    """Access-Point não tem ciclo: vale `Preventiva.data_proxima`."""

    def _ap(self, nome, proxima="__sem__"):
        item = self._switch(nome, PROD, subtipo=self.outro_subtipo)
        if proxima != "__sem__":
            Preventiva.objects.create(equipamento=item, checklist_modelo=self.checklist, data_proxima=proxima)
        return item

    def _etiqueta(self, nome):
        return self._linha(self._parque(), self.tec_prod, nome)["etiqueta"]

    def test_textos_e_tons_de_cada_situacao(self):
        h = self.hoje
        self._ap("AP-VENCIDA", h - timedelta(days=5))
        self._ap("AP-VENCIDA-1", h - timedelta(days=1))
        self._ap("AP-HOJE", h)
        self._ap("AP-BREVE", h + timedelta(days=3))
        self._ap("AP-AMANHA", h + timedelta(days=1))
        self._ap("AP-EM-DIA", h + timedelta(days=40))
        self._ap("AP-SEM-PREVENTIVA")
        self._ap("AP-SEM-DATA", None)
        p = self._parque()
        esperado = {
            "AP-VENCIDA": ("perigo", "Vencida há 5 dias"),
            "AP-VENCIDA-1": ("perigo", "Vencida há 1 dia"),
            "AP-HOJE": ("aviso", "Vence hoje"),
            "AP-BREVE": ("aviso", "Vence em 3 dias"),
            "AP-AMANHA": ("aviso", "Vence em 1 dia"),
            "AP-EM-DIA": ("ok", f"Em dia · próxima {(h + timedelta(days=40)):%d/%m}"),
            "AP-SEM-PREVENTIVA": ("aviso", "Sem preventiva"),
            "AP-SEM-DATA": ("aviso", "Sem data prevista"),
        }
        for nome, (tom, texto) in esperado.items():
            linha = self._linha(p, self.tec_prod, nome)
            self.assertEqual((linha["tom"], linha["etiqueta"]), (tom, texto), nome)
        # em dia sai do grupo de pendentes; os outros ficam
        self.assertEqual(self._linha(p, self.tec_prod, "AP-EM-DIA")["grupo"], "em_dia")
        self.assertEqual(self._linha(p, self.tec_prod, "AP-BREVE")["grupo"], "pendente")

    def test_janela_de_vence_em_breve_e_a_constante(self):
        j = meta._JANELA_VENCE_EM_BREVE_DIAS
        self._ap("AP-NO-LIMITE", self.hoje + timedelta(days=j))
        self._ap("AP-DEPOIS", self.hoje + timedelta(days=j + 1))
        self.assertEqual(self._linha(self._parque(), self.tec_prod, "AP-NO-LIMITE")["grupo"], "pendente")
        self.assertEqual(self._linha(self._parque(), self.tec_prod, "AP-DEPOIS")["grupo"], "em_dia")


class OrdemDeVisitaTests(_Parque):
    def setUp(self):
        super().setUp()
        self.sede = Localidade.objects.create(local="SEDE")
        self.fazenda = Localidade.objects.create(local="FAZENDA NORTE")

    def _em(self, nome, local, **kw):
        item = self._switch(nome, PROD, subtipo=self.outro_subtipo)
        item.localidade = local
        item.save()
        return item

    def test_pendentes_agrupados_por_local_com_mais_pendencia_primeiro(self):
        for i in range(3):
            self._em(f"AP-N{i}", self.fazenda)
        self._em("AP-S0", self.sede)
        b = self._bucket(self._parque(), self.tec_prod)
        locais = [(g["local"], g["total"]) for g in b["locais"]]
        # FAZENDA NORTE (3) antes de SEDE (1); ALGODOEIRA (3 switches, sem preventiva) empata em 3
        self.assertEqual(locais[0][1], 3)
        self.assertEqual(locais[-1], ("SEDE", 1))
        self.assertEqual(sum(t for _, t in locais), b["pendentes"])

    def test_dentro_do_local_o_mais_urgente_vem_primeiro(self):
        h = self.hoje
        sem_agenda = self._em("AP-A-EM-DIA-BREVE", self.sede)
        vencida = self._em("AP-B-VENCIDA", self.sede)
        sem_prev = self._em("AP-C-SEM-PREVENTIVA", self.sede)
        Preventiva.objects.create(equipamento=sem_agenda, checklist_modelo=self.checklist,
                                  data_proxima=h + timedelta(days=5))
        Preventiva.objects.create(equipamento=vencida, checklist_modelo=self.checklist,
                                  data_proxima=h - timedelta(days=9))
        grupo = next(g for g in self._bucket(self._parque(), self.tec_prod)["locais"] if g["local"] == "SEDE")
        self.assertEqual([l["nome"] for l in grupo["itens"]],
                         ["AP-B-VENCIDA", "AP-C-SEM-PREVENTIVA", "AP-A-EM-DIA-BREVE"])

    def test_item_sem_localidade_nao_some(self):
        item = self._em("AP-SEM-LOCAL", self.sede)
        item.localidade = None
        item.save()
        nomes_de_local = [g["local"] for g in self._bucket(self._parque(), self.tec_prod)["locais"]]
        self.assertIn("Sem localidade", nomes_de_local)


class CustoESomenteLeituraTests(_Parque):
    def test_numero_de_consultas_nao_cresce_com_o_numero_de_equipamentos(self):
        """
        O custo é por SUBTIPO (um `ciclo_vigente` cada, e um `apurar_ciclo` se
        houver ciclo), não por equipamento. Por isso os dois subtipos já existem
        antes da primeira medição: o que se mede é o crescimento com o parque.
        """
        self._switch("AP-PRIMEIRO", PROD, subtipo=self.outro_subtipo)
        self._parque()
        with CaptureQueriesContext(connection) as poucos:
            self._parque()
        for i in range(12):
            self._switch(f"SW-EXTRA-{i}", PROD if i % 2 else APOIO)
            self._switch(f"AP-EXTRA-{i}", PROD, subtipo=self.outro_subtipo)
        with CaptureQueriesContext(connection) as muitos:
            self._parque()
        self.assertEqual(len(poucos), len(muitos))

    def test_nao_grava_nada(self):
        antes = (Item.objects.count(), Preventiva.objects.count(),
                 PreventivaExecucao.objects.count(), PreventivaAreaResponsavel.objects.count())
        self._parque()
        self.client.get(reverse("preventiva_metas_config"))
        depois = (Item.objects.count(), Preventiva.objects.count(),
                  PreventivaExecucao.objects.count(), PreventivaAreaResponsavel.objects.count())
        self.assertEqual(antes, depois)


class TelaDosResponsaveisTests(_Parque):
    def _get(self, usuario=None, **params):
        c = Client()
        c.force_login(usuario or self.gestor)
        resp = c.get(reverse("preventiva_metas_config"), params)
        self.assertEqual(resp.status_code, 200)
        return resp

    def test_mostra_um_cartao_por_tecnico_com_os_equipamentos(self):
        html = self._get().content.decode("utf-8")
        self.assertEqual(html.count('class="pvc-tec"'), 2)
        for nome in ("Ana Produtiva", "Bruno Apoio", "SW-PROD-0", "SW-APOIO-1"):
            self.assertIn(nome, html)

    def test_cartao_de_quem_esta_logado_vem_primeiro_e_aberto(self):
        html = self._get(usuario=self.tec_apoio).content.decode("utf-8")
        self.assertEqual(html.count('<details class="pvc-tec" open>'), 1)
        primeiro = html.index('<details class="pvc-tec"')
        self.assertTrue(html.startswith('<details class="pvc-tec" open>', primeiro))
        self.assertLess(html.index("Bruno Apoio"), html.index("Ana Produtiva"))
        self.assertIn('class="pvc-voce"', html)

    def test_quem_nao_e_responsavel_ve_todos_recolhidos(self):
        html = self._get().content.decode("utf-8")
        self.assertEqual(html.count('<details class="pvc-tec" open>'), 0)
        self.assertNotIn('class="pvc-voce"', html)

    def test_cada_equipamento_leva_a_ficha_e_a_iniciar_a_preventiva(self):
        html = self._get(usuario=self.tec_prod).content.decode("utf-8")
        item = self.produtivos[0]
        self.assertIn(reverse("equipamento_detalhe", args=[item.pk]), html)
        self.assertIn(reverse("preventiva_start_item", args=[item.pk]), html)

    def test_fora_de_operacao_nao_oferece_iniciar_preventiva(self):
        parado = self._switch("SW-PARADO", PROD, status=StatusItemChoices.DEFEITO)
        html = self._get(usuario=self.tec_prod).content.decode("utf-8")
        self.assertIn("SW-PARADO", html)
        self.assertNotIn(reverse("preventiva_start_item", args=[parado.pk]), html)

    def test_aviso_de_equipamento_sem_responsavel(self):
        self.linha_apoio.delete()
        html = self._get().content.decode("utf-8")
        self.assertIn("2 equipamentos", html)
        self.assertIn("sem técnico responsável", html)

    def test_sem_nenhum_responsavel_mostra_o_estado_vazio(self):
        PreventivaAreaResponsavel.objects.all().delete()
        html = self._get().content.decode("utf-8")
        self.assertIn("Nenhum responsável definido", html)
        self.assertEqual(html.count('class="pvc-tec"'), 0)

    def test_tecnico_sem_equipamento_explica_onde_classificar(self):
        novo = User.objects.create_user("tec_vazio", password="x", first_name="Dani", last_name="Vazio")
        PreventivaAreaResponsavel.objects.create(area=PROD, subtipo=self.outro_subtipo, tecnico=novo)
        html = self._get().content.decode("utf-8")
        self.assertIn("Nenhum equipamento classificado nesta responsabilidade", html)

    # ── "Gerenciar responsáveis" ───────────────────────────────────────────
    def test_gerenciar_vem_recolhido(self):
        html = self._get().content.decode("utf-8")
        self.assertIn('<details class="pvc-gerenciar">', html)
        self.assertNotIn('<details class="pvc-gerenciar" open>', html)

    def test_formulario_com_erro_reabre_o_gerenciar(self):
        """Senão o erro fica dentro de um bloco fechado e a pessoa acha que nada aconteceu."""
        resp = self.client.post(reverse("preventiva_metas_config"), {
            "acao": "salvar_responsavel", "area": PROD, "tecnico": self.tec_apoio.pk,
            "subtipo": "", "ativo": "on",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["form_responsavel"].errors)
        self.assertIn('<details class="pvc-gerenciar" open>', resp.content.decode("utf-8"))

    def test_salvar_e_remover_continuam_funcionando(self):
        resp = self.client.post(reverse("preventiva_metas_config"), {
            "acao": "salvar_responsavel", "area": PROD, "tecnico": self.tec_apoio.pk,
            "subtipo": self.outro_subtipo.pk, "ativo": "on",
        })
        self.assertEqual(resp.status_code, 302)
        nova = PreventivaAreaResponsavel.objects.get(area=PROD, subtipo=self.outro_subtipo)
        resp = self.client.post(reverse("preventiva_metas_config"), {
            "acao": "remover_responsavel", "responsavel_id": nova.pk,
        })
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(PreventivaAreaResponsavel.objects.filter(pk=nova.pk).exists())

    def test_ids_dos_formularios_continuam_unicos(self):
        corpo = self._get().content.decode("utf-8")
        self.assertEqual(corpo.count('id="id_resp_subtipo"'), 1)
        self.assertEqual(corpo.count('id="id_ciclo_subtipo"'), 1)

    def test_como_a_meta_e_montada_virou_recolhido(self):
        corpo = self._get().content.decode("utf-8")
        self.assertIn('<details class="pvc-ajuda">', corpo)
        self.assertIn("Como a meta é montada", corpo)

    def test_consultas_da_tela_nao_crescem_com_os_equipamentos(self):
        self._get()
        with CaptureQueriesContext(connection) as poucos:
            self._get()
        for i in range(10):
            self._switch(f"SW-MAIS-{i}", PROD if i % 2 else APOIO)
        with CaptureQueriesContext(connection) as muitos:
            self._get()
        self.assertEqual(len(poucos), len(muitos))
