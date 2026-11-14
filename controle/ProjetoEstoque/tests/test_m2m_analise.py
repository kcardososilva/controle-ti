"""A análise dos chips M2M (`services/m2m_analise_service.py`): utilização, consumo e inatividade.

Os critérios de aceite 2, 3 e 4 do pedido de 14/11, como teste:
  • UTILIZAÇÃO — o gráfico reflete os VÍNCULOS reais; chip que respondeu à API mas não tem vínculo NÃO é "em uso";
    inativo, em manutenção, monitoramento desativado e vínculo com equipamento fora de operação ficam separados e fora do
    percentual; a lista que o segmento abre tem o MESMO número do segmento;
  • CONSUMO — "zero" só é afirmado com histórico completo, dado recente e sem falha; sem isso é "sem informação",
    "falha de consulta" ou "dados desatualizados"; períodos diferentes não se comparam; unidade e período são ditos;
  • INATIVIDADE — conta a última ATIVIDADE (fim da sessão, ou o último avanço informado), nunca a hora da consulta;
    falha de comunicação não é inatividade; sem histórico suficiente é dito, não inventado."""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ProjetoEstoque.models import Categoria, FrotaTipo, FrotaVeiculo, Item, M2MChip, M2MConsulta, M2MSessaoDados, Subtipo
from services import frota_service
from services import frota_vinculo_service as vinculos
from services import m2m_analise_service as a
from services import m2m_monitoramento_service as m2m


def criar_chip(n, **extra):
    return M2MChip.objects.create(nome=f"Chip {n}", iccid=f"8955100000000{n:06d}"[:19], **extra)


def com_historico(chip, dias=30, sincronizado_min=5):
    """Histórico de sessões lido por `dias` dias e sincronizado há `sincronizado_min` min."""
    hoje = timezone.localdate()
    M2MChip.objects.filter(pk=chip.pk).update(
        dados_desde=hoje - timedelta(days=dias), dados_sincronizados_em=timezone.now() - timedelta(minutes=sincronizado_min),
    )
    chip.refresh_from_db()
    return chip


_proximo_evento = iter(range(1, 10_000))


def sessao(chip, minutos_atras, duracao_min=5, aberta=False, total=1024 * 1024):
    """Uma sessão que COMEÇOU há `minutos_atras`; fechada termina `duracao_min` depois do início."""
    inicio = timezone.now() - timedelta(minutes=minutos_atras)
    return M2MSessaoDados.objects.create(
        chip=chip, id_evento=next(_proximo_evento), inicio=inicio, fim=None if aberta else inicio + timedelta(minutes=duracao_min),
        duracao_s=duracao_min * 60, upload_bytes=total // 2, download_bytes=total - total // 2, total_bytes=total,
    )


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user("analise_admin", "a@x.com", "senha-boa-123", is_superuser=True)
        cls.karitel, cls.rio = frota_service.fazendas()
        cls.tipo = FrotaTipo.objects.get_or_create(nome="Trator")[0]

    def veiculo(self, codigo, fazenda=None, **extra):
        dados = {"codigo": codigo, "nome": f"Trator {codigo}", "tipo": self.tipo.pk, **extra}
        if fazenda is not None:
            dados["localidade"] = fazenda.pk
        return frota_service.criar_veiculo(dados, self.admin)

    def vincular(self, chip, veiculo):
        vinculos.vincular(chip, veiculo, self.admin, "teste")
        chip.refresh_from_db()
        return chip


class UtilizacaoTests(Base):
    def grupo(self, util, chave):
        return next(g for g in util["principais"] + util["fora"] if g["chave"] == chave)

    def test_em_uso_e_vinculo_nao_resposta_da_api(self):
        usado = self.vincular(criar_chip(1), self.veiculo("MQ-1"))
        disponivel = criar_chip(2)
        # o chip DISPONÍVEL respondeu à API, tem sessão e consumo — e continua "não utilizado"
        M2MChip.objects.filter(pk=disponivel.pk).update(sessao_estado="ativa", consumo_bytes=5_000_000)
        sessao(disponivel, 10)
        util = a.utilizacao()
        self.assertEqual(self.grupo(util, "em_uso")["n"], 1)
        self.assertEqual(self.grupo(util, "nao_utilizado")["n"], 1)
        self.assertEqual(util["considerados"], 2)
        self.assertEqual([g["pct"] for g in util["principais"]], [50.0, 50.0])
        self.assertTrue(usado.pk)

    def test_estados_do_cadastro_ficam_separados_e_fora_do_percentual(self):
        self.vincular(criar_chip(1), self.veiculo("MQ-1"))                                  # em uso
        criar_chip(2)                                                                       # não utilizado
        M2MChip.objects.filter(pk=criar_chip(3).pk).update(situacao_uso="inativo")
        M2MChip.objects.filter(pk=criar_chip(4).pk).update(situacao_uso="manutencao")
        M2MChip.objects.filter(pk=criar_chip(5).pk).update(monitorado=False)                # monitoramento desativado
        desativado = self.veiculo("MQ-2")
        self.vincular(criar_chip(6), desativado)
        FrotaVeiculo.objects.filter(pk=desativado.pk).update(status="desativado")           # vínculo com equipamento fora de operação
        util = a.utilizacao()
        n = {g["chave"]: g["n"] for g in util["principais"] + util["fora"]}
        self.assertEqual(n, {
            "em_uso": 1, "nao_utilizado": 1, "inativo": 1, "manutencao": 1, "desativado": 1, "vinculo_inativo": 1,
        })
        self.assertEqual(util["total"], 6)
        self.assertEqual(util["considerados"], 2)
        self.assertEqual(util["fora_do_calculo"], 4)
        # cada chip em UM grupo só: a soma fecha com o total
        self.assertEqual(sum(g["n"] for g in util["principais"] + util["fora"]), util["total"])

    def test_equipamento_em_manutencao_continua_dono_do_chip_no_estoque_como_no_veiculo(self):
        """Regressão (14/11): dois chips vinculados, o gráfico mostrava UM "em uso". O veículo em manutenção contava como
        em operação e o equipamento do estoque de TI em manutenção não — o mesmo chip, duas réguas. Hoje é uma só: Ativo
        ou Em manutenção; Backup (ou outro estado parado) segue separado, com o número e o link, fora do percentual."""
        categoria = Categoria.objects.create(nome="Equipamento de teste")
        tablet = Subtipo.objects.create(nome="Tablet de teste", alocado="sim", categoria=categoria, aceita_chip=True)

        def equipamento(nome):
            return Item.objects.create(nome=nome, numero_serie=f"SN-{nome}", quantidade=1, status="ativo", subtipo=tablet)

        na_oficina, guardado, no_trator = equipamento("A"), equipamento("B"), self.veiculo("MQ-1")
        self.vincular(criar_chip(1), na_oficina)
        self.vincular(criar_chip(2), guardado)
        self.vincular(criar_chip(3), no_trator)
        # o status muda DEPOIS do vínculo, como acontece na vida real (o equipamento vai para a oficina com o chip dentro)
        Item.objects.filter(pk=na_oficina.pk).update(status="manutencao")
        Item.objects.filter(pk=guardado.pk).update(status="backup")
        FrotaVeiculo.objects.filter(pk=no_trator.pk).update(status=FrotaVeiculo.Status.MANUTENCAO)
        criar_chip(4)                                                                        # disponível

        util = a.utilizacao()
        n = {g["chave"]: g["n"] for g in util["principais"] + util["fora"]}
        self.assertEqual((n["em_uso"], n["nao_utilizado"], n["vinculo_inativo"]), (2, 1, 1))
        # o indicador "Em uso" do topo e o gráfico leem a mesma conta
        self.assertEqual(a.indicadores()["em_uso"], n["em_uso"])
        self.assertEqual(m2m.listar_chips({"uso": "em_uso"}).count(), n["em_uso"])

    def test_o_arco_do_donut_soma_100_e_cada_arco_comeca_onde_o_anterior_termina(self):
        for n in range(3):
            self.vincular(criar_chip(n), self.veiculo(f"MQ-{n}"))
        criar_chip(9)
        usados, livres = a.utilizacao()["principais"]
        self.assertEqual((usados["inicio"], round(usados["traco"] + livres["traco"], 1)), (0, 100.0))
        self.assertEqual(livres["inicio"], usados["traco"])
        self.assertEqual(round(usados["traco"] + usados["resto"], 1), 100.0)

    def test_sem_chips_o_estado_e_vazio_e_nao_um_donut_de_zeros(self):
        util = a.utilizacao()
        self.assertTrue(util["vazio"])
        self.assertEqual(util["considerados"], 0)

    def test_o_numero_do_segmento_e_o_da_lista_que_ele_abre(self):
        self.vincular(criar_chip(1), self.veiculo("MQ-1"))
        self.vincular(criar_chip(2), self.veiculo("MQ-2"))
        criar_chip(3)
        criar_chip(4)
        criar_chip(5)
        util = a.utilizacao()
        for g in util["principais"]:
            lista = m2m.listar_chips({"uso": g["chave"]})
            self.assertEqual(lista.count(), g["n"], g["chave"])
            self.assertIn(f"uso={g['chave']}", g["url"])

    def test_unidade_do_chip_vinculado_e_a_do_equipamento_e_sem_vinculo_e_a_do_chip(self):
        no_karitel = self.vincular(criar_chip(1), self.veiculo("MQ-K", fazenda=self.karitel))
        no_rio = self.vincular(criar_chip(2), self.veiculo("MQ-R", fazenda=self.rio))
        solto = criar_chip(3, localidade=self.karitel)                       # sem vínculo: vale o cadastro do próprio chip
        # chip vinculado a um veículo do Rio com o cadastro do chip dizendo Karitel: vale o EQUIPAMENTO (uma unidade só)
        M2MChip.objects.filter(pk=no_rio.pk).update(localidade=self.karitel)
        em_karitel = set(M2MChip.objects.filter(m2m.q_unidade(self.karitel.pk)).values_list("pk", flat=True))
        em_rio = set(M2MChip.objects.filter(m2m.q_unidade(self.rio.pk)).values_list("pk", flat=True))
        self.assertEqual(em_karitel, {no_karitel.pk, solto.pk})
        self.assertEqual(em_rio, {no_rio.pk})
        self.assertFalse(em_karitel & em_rio)
        self.assertEqual(a.utilizacao(self.rio.pk)["total"], 1)


class ConsumoTests(Base):
    def linha(self, resultado, chip):
        return next(l for l in resultado["linhas"] if l["pk"] == chip.pk)

    def test_zero_confirmado_exige_historico_completo_dado_recente_e_sem_falha(self):
        ok = com_historico(criar_chip(1), dias=30)
        r = a.classificar_consumo("7", "todos")
        self.assertEqual(self.linha(r, ok)["categoria"], "zero_confirmado")

    def test_sem_historico_que_cubra_a_janela_nao_e_zero_e_sim_sem_informacao(self):
        curto = com_historico(criar_chip(1), dias=2)                    # só leu os últimos 2 dias
        r = a.classificar_consumo("7", "todos")
        self.assertEqual(self.linha(r, curto)["categoria"], "sem_informacao")

    def test_nunca_sincronizado_e_dado_desatualizado_nao_sao_zero(self):
        nunca = criar_chip(1)                                           # sem dados_desde nem sincronização
        velho = com_historico(criar_chip(2), dias=30, sincronizado_min=60 * 30)     # sincronizado há 30 h (limite: 24 h)
        r = a.classificar_consumo("7", "todos")
        self.assertEqual(self.linha(r, nunca)["categoria"], "desatualizado")
        self.assertEqual(self.linha(r, velho)["categoria"], "desatualizado")

    def test_falha_de_consulta_e_diferente_de_zero(self):
        c = com_historico(criar_chip(1), dias=30)
        M2MChip.objects.filter(pk=c.pk).update(falhas_consecutivas=3)
        self.assertEqual(self.linha(a.classificar_consumo("7", "todos"), c)["categoria"], "falha_consulta")

    def test_consumo_acima_de_zero_aparece_nos_maiores_mesmo_com_historico_parcial_mas_nao_nos_menores(self):
        completo = com_historico(criar_chip(1), dias=30)
        parcial = com_historico(criar_chip(2), dias=2)
        sessao(completo, 60 * 24, total=10 * 1024 * 1024)
        sessao(parcial, 60 * 24, total=1 * 1024 * 1024)
        r = a.consumo("7", "todos")
        self.assertEqual([m["nome"] for m in r["maiores"]], ["Chip 1", "Chip 2"])
        self.assertTrue(r["maiores"][1]["parcial"], "a soma do histórico parcial é um piso e a linha diz isso")
        self.assertEqual([m["nome"] for m in r["menores"]], ["Chip 1"], "um piso baixo não prova consumo baixo")

    def test_ranking_ordena_e_diz_unidade_e_barra_relativa(self):
        for n, mb in ((1, 1), (2, 8), (3, 4)):
            sessao(com_historico(criar_chip(n), dias=30), 60 * 24, total=mb * 1024 * 1024)
        r = a.consumo("7", "todos")
        self.assertEqual([(m["nome"], m["texto"]) for m in r["maiores"]], [("Chip 2", "8 MB"), ("Chip 3", "4 MB"), ("Chip 1", "1 MB")])
        self.assertEqual([m["pct"] for m in r["maiores"]], [100.0, 50.0, 12.5])
        self.assertEqual([m["nome"] for m in r["menores"]], ["Chip 1", "Chip 3", "Chip 2"])
        self.assertIn("base 1024", r["unidade_de_medida"])

    def test_zero_confirmado_e_sem_informacao_sao_contados_a_parte_com_link_para_a_lista(self):
        com_historico(criar_chip(1), dias=30)
        com_historico(criar_chip(2), dias=30)
        com_historico(criar_chip(3), dias=2)
        r = a.consumo("7", "todos")
        resumo = {x["chave"]: x for x in r["resumo"]}
        self.assertEqual((resumo["zero_confirmado"]["n"], resumo["sem_informacao"]["n"]), (2, 1))
        for chave in ("zero_confirmado", "sem_informacao"):
            lista = m2m.listar_chips({"consumo": chave, "periodo": "7", "escopo": "todos"})
            self.assertEqual(lista.count(), resumo[chave]["n"], chave)

    def test_sem_nenhuma_sessao_na_janela_a_tela_nao_tem_ranking(self):
        com_historico(criar_chip(1), dias=30)
        r = a.consumo("7", "todos")
        self.assertTrue(r["sem_numero"])
        self.assertEqual((r["maiores"], r["menores"]), ([], []))

    def test_periodo_da_tim_e_o_que_a_tim_informou_e_chip_com_outro_periodo_nao_entra_na_comparacao(self):
        hoje = timezone.localdate()
        inicio_a = timezone.make_aware(timezone.datetime.combine(hoje.replace(day=1), timezone.datetime.min.time()))
        inicio_b = inicio_a - timedelta(days=31)

        def com_periodo(chip, inicio):
            consulta = M2MConsulta.objects.create(
                chip=chip, tipo="consumo", origem="agendada", identificador_tipo="iccid", resultado="sucesso", periodo_inicio=inicio,
            )
            M2MChip.objects.filter(pk=chip.pk).update(consumo_consulta=consulta)

        c1, c2, c3 = (com_historico(criar_chip(n), dias=60) for n in (1, 2, 3))
        for c in (c1, c2):
            com_periodo(c, inicio_a)
        com_periodo(c3, inicio_b)                                       # a TIM mandou OUTRO período para este chip
        sessao(c1, 60, total=2 * 1024 * 1024)
        r = a.classificar_consumo("api", "todos")
        self.assertEqual(r["janela"]["inicio_dt"], inicio_a)
        self.assertEqual(self.linha(r, c3)["categoria"], "periodo_diferente")
        self.assertEqual(self.linha(r, c1)["categoria"], "com_consumo")
        self.assertIn("informado pela TIM", r["janela"]["texto"])

    def test_30_e_90_dias_so_valem_com_historico_suficiente(self):
        com_historico(criar_chip(1), dias=10)
        valores = {p["valor"]: p["disponivel"] for p in a.periodos_disponiveis("todos")}
        self.assertEqual(valores, {"api": True, "7": True, "30": False, "90": False})
        com_historico(M2MChip.objects.get(nome="Chip 1"), dias=40)
        self.assertTrue({p["valor"]: p["disponivel"] for p in a.periodos_disponiveis("todos")}["30"])


class InatividadeTests(Base):
    def faixa(self, chip, escopo="todos"):
        return next(l for l in a.classificar_inatividade(escopo) if l["pk"] == chip.pk)

    def test_as_faixas_seguem_a_ultima_atividade_conhecida(self):
        casos = ((20, "menos_1h"), (3 * 60, "de_1_a_6h"), (10 * 60, "de_6_a_24h"), (3 * 24 * 60, "de_1_a_7d"), (10 * 24 * 60, "mais_7d"))
        for n, (minutos, esperado) in enumerate(casos):
            with self.subTest(minutos=minutos):
                c = com_historico(criar_chip(n), dias=30)
                sessao(c, minutos + 5, duracao_min=5)                   # terminou há `minutos`
                self.assertEqual(self.faixa(c)["faixa"], esperado)

    def test_sessao_aberta_conta_pelo_ultimo_avanco_da_plataforma_nao_pela_hora_da_leitura(self):
        c = com_historico(criar_chip(1), dias=30, sincronizado_min=1)   # lido há 1 min…
        s = sessao(c, 5 * 60, duracao_min=60, aberta=True)              # …mas a sessão começou há 5 h e só avançou 1 h
        M2MSessaoDados.objects.filter(pk=s.pk).update(lido_em=timezone.now())
        l = self.faixa(c)
        self.assertEqual(l["faixa"], "de_1_a_6h", "o último avanço foi há 4 h; a leitura de agora não é atividade do chip")
        self.assertEqual(l["fonte"], "sessao")

    def test_sem_sessao_com_historico_de_7_dias_ou_mais_e_mais_de_7_dias_sem_inventar_o_tempo(self):
        c = com_historico(criar_chip(1), dias=14)
        l = self.faixa(c)
        self.assertEqual((l["faixa"], l["ultima_atividade"]), ("mais_7d", None))

    def test_sem_sessao_e_historico_curto_e_sem_historico_suficiente(self):
        c = com_historico(criar_chip(1), dias=2)
        self.assertEqual(self.faixa(c)["faixa"], "insuficiente")

    def test_falha_de_comunicacao_nao_e_inatividade(self):
        c = com_historico(criar_chip(1), dias=30)
        sessao(c, 10 * 24 * 60)                                         # última atividade há 10 dias…
        M2MChip.objects.filter(pk=c.pk).update(falhas_consecutivas=2)  # …mas a leitura está falhando
        self.assertEqual(self.faixa(c)["faixa"], "comunicacao")

    def test_dado_desatualizado_nao_e_classificado_por_tempo(self):
        c = com_historico(criar_chip(1), dias=30, sincronizado_min=60 * 30)
        sessao(c, 60)
        self.assertEqual(self.faixa(c)["faixa"], "comunicacao")

    def test_sem_sessoes_lidas_vale_a_ultima_consulta_valida_que_viu_sessao_ativa(self):
        c = com_historico(criar_chip(1), dias=30)
        M2MConsulta.objects.create(
            chip=c, tipo="sessao", origem="agendada", identificador_tipo="iccid", resultado="sucesso", estado_interpretado="ativa",
            iniciada_em=timezone.now() - timedelta(hours=3),
        )
        l = self.faixa(c)
        self.assertEqual((l["faixa"], l["fonte"]), ("de_1_a_6h", "consulta"))

    def test_consulta_sem_sessao_ativa_nao_e_atividade(self):
        """A hora da última consulta NÃO é a hora da última atividade do chip."""
        c = com_historico(criar_chip(1), dias=14)
        M2MConsulta.objects.create(
            chip=c, tipo="sessao", origem="agendada", identificador_tipo="iccid", resultado="sucesso", estado_interpretado="sem_sessao",
            iniciada_em=timezone.now() - timedelta(minutes=2),
        )
        l = self.faixa(c)
        self.assertIsNone(l["ultima_atividade"])
        self.assertEqual(l["faixa"], "mais_7d")

    def test_o_escopo_em_uso_so_olha_chips_vinculados(self):
        usado = com_historico(self.vincular(criar_chip(1), self.veiculo("MQ-1")), dias=30)
        livre = com_historico(criar_chip(2), dias=30)
        self.assertEqual([l["pk"] for l in a.classificar_inatividade("uso")], [usado.pk])
        self.assertEqual({l["pk"] for l in a.classificar_inatividade("todos")}, {usado.pk, livre.pk})

    def test_o_numero_da_faixa_e_o_da_lista_e_o_resumo_separa_o_que_tem_tempo_do_que_nao_tem(self):
        c = com_historico(criar_chip(1), dias=30)
        sessao(c, 3 * 60)
        com_historico(criar_chip(2), dias=14)                           # sem sessão: "mais de 7 dias" sem tempo exato
        com_historico(criar_chip(3), dias=2)                            # histórico curto
        r = a.inatividade("todos")
        faixas = {f["chave"]: f for f in r["faixas"]}
        self.assertEqual((faixas["de_1_a_6h"]["n"], faixas["mais_7d"]["n"], faixas["insuficiente"]["n"]), (1, 1, 1))
        for chave in ("de_1_a_6h", "mais_7d", "insuficiente"):
            self.assertEqual(m2m.listar_chips({"inatividade": chave, "escopo": "todos"}).count(), faixas[chave]["n"], chave)
        self.assertIn("1 de 3 chips com última atividade conhecida", r["resumo"])
        self.assertIn("1 sem nenhuma sessão no histórico lido", r["resumo"])
        self.assertAlmostEqual(sum(f["pct_total"] for f in r["faixas"]), 100.0, delta=0.2)

    def test_valor_de_filtro_invalido_nao_traz_chip_nenhum(self):
        com_historico(criar_chip(1), dias=14)
        self.assertEqual(m2m.listar_chips({"inatividade": "invalida"}).count(), 0)
        self.assertEqual(m2m.listar_chips({"consumo": "invalida"}).count(), 0)


class TelaDoDashboardTests(Base):
    def setUp(self):
        self.client.force_login(self.admin)

    def pagina(self, **params):
        r = self.client.get(reverse("m2m_visao_geral"), params)
        self.assertEqual(r.status_code, 200)
        return r

    def test_a_tela_traz_os_tres_graficos_a_faixa_de_indicadores_e_a_data_da_leitura(self):
        usado = com_historico(self.vincular(criar_chip(1), self.veiculo("MQ-1", fazenda=self.karitel)), dias=30)
        sessao(usado, 90, total=3 * 1024 * 1024)
        criar_chip(2)
        html = self.pagina().content.decode()
        for trecho in ("Situação dos chips", "Chips utilizados e não utilizados", "Tempo de inatividade", "Consumo de dados: maiores e menores",
                       "Chips sem atividade recente", "Consumo de dados por dia", "Dados da TIM lidos em", "considerados"):
            self.assertIn(trecho, html)
        self.assertIn("<svg", html)
        self.assertIn("dash-donut-arco--em_uso", html)
        for marcador in ("{{", "{%", "{#"):
            self.assertNotIn(marcador, html)

    def test_cada_segmento_do_donut_e_um_link_para_a_lista_filtrada(self):
        self.vincular(criar_chip(1), self.veiculo("MQ-1"))
        criar_chip(2)
        html = self.pagina().content.decode()
        self.assertIn(reverse("m2m_chips") + "?uso=em_uso", html)
        self.assertIn(reverse("m2m_chips") + "?uso=nao_utilizado", html)

    def test_filtros_de_periodo_unidade_e_escopo_chegam_ao_painel(self):
        com_historico(self.vincular(criar_chip(1), self.veiculo("MQ-K", fazenda=self.karitel)), dias=30)
        com_historico(self.vincular(criar_chip(2), self.veiculo("MQ-R", fazenda=self.rio)), dias=30)
        com_historico(criar_chip(3), dias=30)
        r = self.pagina(unidade=str(self.karitel.pk), escopo="todos", periodo="7")
        painel = r.context["analise"]
        self.assertEqual((painel["periodo"], painel["escopo"], painel["unidade"]), ("7", "todos", self.karitel.pk))
        self.assertEqual(painel["utilizacao"]["total"], 1)
        self.assertEqual(painel["indicadores"]["total"], 1)
        # o nome antigo do filtro continua valendo
        self.assertEqual(self.pagina(fazenda=str(self.rio.pk)).context["analise"]["unidade"], self.rio.pk)

    def test_periodo_sem_historico_aparece_desabilitado_e_diz_porque(self):
        com_historico(criar_chip(1), dias=5)
        html = self.pagina().content.decode()
        self.assertIn('aria-disabled="true"', html)
        self.assertIn("histórico ainda não cobre esta janela", html)

    def test_periodo_padrao_e_o_da_tim_e_o_escopo_padrao_e_em_uso(self):
        r = self.pagina()
        self.assertEqual((r.context["analise"]["periodo"], r.context["analise"]["escopo"]), ("api", "uso"))
        self.assertEqual(self.pagina(periodo="999", escopo="x").context["analise"]["periodo"], "api")

    def test_sem_dado_a_tela_diz_estado_vazio_e_nao_inventa_franquia(self):
        html = self.pagina().content.decode()
        self.assertIn("Nenhuma sessão de dados registrada no período", html)
        self.assertIn("o limite contratado de cada chip não é conhecido", html)
        self.assertNotIn("% da franquia", html)

    def test_a_lista_de_chips_abre_com_o_filtro_do_grafico_e_o_rotulo_legivel(self):
        com_historico(criar_chip(1), dias=14)
        r = self.client.get(reverse("m2m_chips"), {"inatividade": "mais_7d", "escopo": "todos"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.context["chips"]), 1)
        self.assertIn("Mais de 7 dias", r.content.decode())

    def test_o_painel_nao_consulta_a_api_da_tim(self):
        from unittest import mock

        from services import tim_m2m_service

        com_historico(criar_chip(1), dias=30)
        with mock.patch.object(tim_m2m_service.ClienteTIM, "_enviar", side_effect=AssertionError("abrir o painel não chama a API")):
            self.pagina()

    def test_nao_ha_consulta_por_linha_o_custo_nao_cresce_com_o_numero_de_chips(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        for n in range(3):
            com_historico(criar_chip(n), dias=30)
        self.pagina()                                                   # aquecimento (linhas únicas de configuração)
        with CaptureQueriesContext(connection) as poucos:
            self.pagina()
        for n in range(3, 23):
            c = com_historico(criar_chip(n), dias=30)
            sessao(c, 60, total=1024 * 1024)
        with CaptureQueriesContext(connection) as muitos:
            self.pagina()
        self.assertLessEqual(len(muitos), len(poucos) + 2)
