"""Os números e as telas dos dashboards do Zelo Agro (`agro_dash_service` + as três "Visão geral").

O que se prova: cada número é contado do cadastro ou das sessões de dados lidas, sem estimativa; sem dado a tela diz
isso (estado vazio, nunca gráfico de zeros); o consumo avisa a cobertura do histórico; não existe franquia nem
percentual de uso; os filtros de período e de fazenda mudam o que é somado; e o comando da tarefa agendada roda sob
`pythonw` (sem stdout)."""

import io
import sys
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import Permission, User
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from ProjetoEstoque.models import FrotaTipo, M2MChip, M2MSessaoDados
from services import agro_dash_service as dash
from services import frota_service


def usuario_super(nome="dash_admin"):
    return User.objects.create_user(nome, f"{nome}@x.com", "senha-boa-123", is_superuser=True)


def chip(n, **extra):
    return M2MChip.objects.create(nome=f"Chip {n}", iccid=f"8955100000000{n:06d}"[:19], **extra)


def sessao(c, dias_atras, total, id_evento):
    agora = timezone.now()
    inicio = (agora - timedelta(days=dias_atras)).replace(hour=10, minute=0, second=0, microsecond=0)
    return M2MSessaoDados.objects.create(
        chip=c, id_evento=id_evento, inicio=inicio, fim=inicio + timedelta(minutes=5), duracao_s=300,
        upload_bytes=total // 2, download_bytes=total - total // 2, total_bytes=total,
    )


class BarrasTests(TestCase):
    def test_ordena_do_maior_para_o_menor_e_a_maior_ocupa_a_largura_toda(self):
        b = dash.barras([("B", 10, None), ("A", 40, "/x/"), ("C", 20, None)])
        self.assertEqual([(i["rotulo"], i["n"], i["pct"]) for i in b], [("A", 40, 100.0), ("C", 20, 50.0), ("B", 10, 25.0)])
        self.assertEqual(b[0]["url"], "/x/")

    def test_o_que_passa_do_limite_vira_outros_sem_perder_a_soma(self):
        pares = [(f"T{n}", n, None) for n in range(1, 11)]                       # 10 itens, soma 55
        b = dash.barras(pares, limite=4)
        self.assertEqual(len(b), 4)
        self.assertEqual(b[-1]["rotulo"], "Outros")
        self.assertEqual(sum(i["n"] for i in b), 55)
        self.assertIsNone(b[-1]["url"])

    def test_zero_nao_vira_barra_e_rotulo_vazio_e_dito(self):
        b = dash.barras([("", 3, None), ("X", 0, None)], vazio_rotulo="Sem fazenda")
        self.assertEqual([(i["rotulo"], i["n"]) for i in b], [("Sem fazenda", 3)])
        self.assertEqual(dash.barras([]), [])

    def test_periodo_so_aceita_7_30_ou_90(self):
        for pedido, esperado in (("7", 7), ("30", 30), ("90", 90), ("15", 30), ("abc", 30), (None, 30), ("-7", 30)):
            self.assertEqual(dash.periodo_pedido(pedido), esperado, pedido)


class PainelDaFrotaTests(TestCase):
    def setUp(self):
        self.usuario = usuario_super()
        self.karitel, self.rio = frota_service.fazendas()
        self.trator = FrotaTipo.objects.get_or_create(nome="Trator")[0]
        self.caminhao = FrotaTipo.objects.get_or_create(nome="Caminhão")[0]

    def v(self, codigo, tipo=None, **extra):
        return frota_service.criar_veiculo({"codigo": codigo, "nome": codigo, "tipo": (tipo or self.trator).pk, **extra}, self.usuario)

    def test_sem_equipamento_e_vazio(self):
        p = dash.painel_da_frota()
        self.assertTrue(p["vazio"])
        self.assertEqual((p["por_tipo"], p["por_fabricante"], p["por_fazenda"]), ([], [], []))

    def test_distribuicoes_e_cadastro_incompleto_sao_contagens_do_cadastro(self):
        self.v("A", fabricante="John Deere", modelo="6110", localidade=self.karitel.pk, latitude="-14,7", longitude="-45,5")
        self.v("B", tipo=self.caminhao, fabricante="Stara", modelo="Imperador", localidade=self.rio.pk)
        self.v("C", tipo=self.caminhao)                                            # sem fabricante, modelo e fazenda
        p = dash.painel_da_frota()
        self.assertEqual(p["total"], 3)
        self.assertEqual({(i["rotulo"], i["n"]) for i in p["por_tipo"]}, {("Trator", 1), ("Caminhão", 2)})
        self.assertEqual({(i["rotulo"], i["n"]) for i in p["por_fabricante"]}, {("John Deere", 1), ("Stara", 1), ("Não informado", 1)})
        self.assertEqual({(i["rotulo"], i["n"]) for i in p["por_fazenda"]}, {("Fazenda Karitel", 1), ("Fazenda Rio do Meio", 1), ("Sem fazenda", 1)})
        i = p["incompletos"]
        self.assertEqual((i["sem_fabricante"], i["sem_modelo"], i["sem_fazenda"], i["total"]), (1, 1, 1, 1))
        self.assertEqual((i["sem_localizacao"], p["com_localizacao"]), (2, 1))

    def test_desativado_nao_entra_na_distribuicao(self):
        v = self.v("A", fabricante="Stara")
        frota_service.mudar_status(v, "desativado", self.usuario, "vendido")
        self.assertTrue(dash.painel_da_frota()["vazio"])

    def test_cada_barra_leva_para_a_lista_ja_filtrada(self):
        self.v("A", localidade=self.karitel.pk)
        p = dash.painel_da_frota()
        self.assertEqual(p["por_tipo"][0]["url"], f"{reverse('frota_visao_geral')}?tipo={self.trator.pk}")
        self.assertEqual(p["por_fazenda"][0]["url"], f"{reverse('frota_visao_geral')}?localidade={self.karitel.pk}")

    def test_o_custo_do_painel_nao_cresce_com_o_numero_de_equipamentos(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        self.v("A")
        with CaptureQueriesContext(connection) as q1:
            dash.painel_da_frota()
        for n in range(30):
            self.v(f"M{n}", fabricante=f"F{n % 3}")
        with CaptureQueriesContext(connection) as q2:
            dash.painel_da_frota()
        self.assertEqual(len(q1), len(q2))


class ConsumoTests(TestCase):
    def test_sem_sessao_de_dados_a_serie_e_vazia_e_nao_inventa_zeros(self):
        chip(1)
        s = dash.serie_de_consumo(30)
        self.assertTrue(s["vazio"])
        self.assertEqual(s["total_bytes"], 0)
        self.assertEqual(len(s["pontos"]), 30)

    def test_soma_as_sessoes_por_dia_com_bytes_exatos_e_base_1024(self):
        c = chip(1)
        sessao(c, 1, 1024 * 1024, 1)
        sessao(c, 1, 1024 * 1024, 2)
        sessao(c, 3, 512 * 1024, 3)
        s = dash.serie_de_consumo(7)
        por_dia = {p["iso"]: p for p in s["pontos"]}
        ontem = (timezone.localdate() - timedelta(days=1)).isoformat()
        self.assertEqual(por_dia[ontem]["bytes"], 2 * 1024 * 1024)
        self.assertEqual(por_dia[ontem]["texto"], "2 MB")
        self.assertEqual(s["total_bytes"], 2 * 1024 * 1024 + 512 * 1024)
        self.assertEqual(por_dia[ontem]["altura"], 100.0)
        self.assertFalse(s["vazio"])

    def test_o_periodo_corta_o_que_ficou_para_tras(self):
        c = chip(1)
        sessao(c, 2, 1000, 1)
        sessao(c, 20, 9000, 2)
        self.assertEqual(dash.serie_de_consumo(7)["total_bytes"], 1000)
        self.assertEqual(dash.serie_de_consumo(30)["total_bytes"], 10000)

    def test_o_filtro_de_fazenda_so_soma_os_chips_dela(self):
        karitel, rio = frota_service.fazendas()
        a, b = chip(1, localidade=karitel), chip(2, localidade=rio)
        sessao(a, 1, 1000, 1)
        sessao(b, 1, 7000, 2)
        self.assertEqual(dash.serie_de_consumo(7, fazenda=str(karitel.pk))["total_bytes"], 1000)
        self.assertEqual(dash.serie_de_consumo(7, fazenda=str(rio.pk))["total_bytes"], 7000)
        self.assertEqual(dash.serie_de_consumo(7)["total_bytes"], 8000)

    def test_a_cobertura_diz_quantos_chips_tem_o_periodo_completo(self):
        completo, parcial = chip(1), chip(2)
        hoje = timezone.localdate()
        M2MChip.objects.filter(pk=completo.pk).update(dados_sincronizados_em=timezone.now(), dados_desde=hoje - timedelta(days=40))
        M2MChip.objects.filter(pk=parcial.pk).update(dados_sincronizados_em=timezone.now(), dados_desde=hoje - timedelta(days=2))
        c = dash.serie_de_consumo(30)["cobertura"]
        self.assertEqual((c["completos"], c["com_historico"], c["total"], c["parcial"]), (1, 2, 2, True))
        M2MChip.objects.filter(pk=parcial.pk).update(dados_desde=hoje - timedelta(days=40))
        self.assertFalse(dash.serie_de_consumo(30)["cobertura"]["parcial"])

    def test_os_maiores_consumos_vem_em_ordem_e_com_a_barra_relativa(self):
        a, b, c = chip(1), chip(2), chip(3)
        sessao(a, 1, 1000, 1)
        sessao(b, 1, 4000, 2)
        sessao(c, 1, 2000, 3)
        m = dash.maiores_consumos(7, limite=2)
        self.assertEqual([(x["chip"].pk, x["bytes"]) for x in m], [(b.pk, 4000), (c.pk, 2000)])
        self.assertEqual([x["pct"] for x in m], [100.0, 50.0])


class TelasDosDashboardsTests(TestCase):
    def setUp(self):
        self.admin = usuario_super("dash_telas")
        self.client.force_login(self.admin)

    def test_a_conectividade_vazia_diz_que_nao_ha_sessao_e_nao_mostra_franquia(self):
        html = self.client.get(reverse("m2m_visao_geral")).content.decode()
        self.assertIn("Consumo de dados por dia", html)
        self.assertIn("Nenhuma sessão de dados registrada no período", html)
        self.assertIn("o limite contratado de cada chip não é conhecido", html)
        self.assertNotIn("% da franquia", html)
        for marcador in ("{{", "{%", "{#"):
            self.assertNotIn(marcador, html)

    def test_periodo_e_fazenda_chegam_ao_dashboard(self):
        karitel, _ = frota_service.fazendas()
        c = chip(1, localidade=karitel)
        sessao(c, 20, 3 * 1024 * 1024, 1)
        r30 = self.client.get(reverse("m2m_visao_geral"), {"periodo": "30"})
        r7 = self.client.get(reverse("m2m_visao_geral"), {"periodo": "7"})
        self.assertEqual(r30.context["painel_conect"]["consumo"]["total_bytes"], 3 * 1024 * 1024)
        self.assertEqual(r7.context["painel_conect"]["consumo"]["total_bytes"], 0)
        outra = self.client.get(reverse("m2m_visao_geral"), {"periodo": "30", "fazenda": str(frota_service.fazendas()[1].pk)})
        self.assertEqual(outra.context["painel_conect"]["consumo"]["total_bytes"], 0)
        self.assertEqual(r7.context["periodo"], 7)

    def test_a_conectividade_mostra_as_ultimas_execucoes_e_o_historico_de_falhas(self):
        from ProjetoEstoque.models import M2MExecucao
        M2MExecucao.objects.create(estado="concluida", consultados=9, chamadas=12, duracao_ms=21500, concluida_em=timezone.now())
        html = self.client.get(reverse("m2m_visao_geral")).content.decode()
        self.assertIn("Últimas sincronizações", html)
        self.assertIn("21,5 s", html)
        self.assertIn("Nenhuma consulta com falha registrada", html)

    def test_a_lista_de_frotas_tem_resumo_toggle_para_o_mapa_e_leva_os_filtros(self):
        r = self.client.get(reverse("frota_visao_geral"), {"status": "ativo", "q": "trator"})
        html = r.content.decode()
        self.assertIn("Resumo da frota", html)
        self.assertIn("Por categoria", html)
        self.assertIn("Por fabricante", html)
        self.assertIn("Por fazenda", html)
        self.assertIn(reverse("agro_mapa") + "?status=ativo&amp;q=trator", html)
        self.assertIn("Fabricante", html)
        self.assertIn('name="localizacao"', html)

    def test_proprios_e_terceiros_nao_repetem_o_resumo(self):
        html = self.client.get(reverse("frota_equipamentos_proprios")).content.decode()
        self.assertNotIn("Resumo da frota", html)

    def test_o_tamanho_da_pagina_e_um_so_e_fica_no_rodape(self):
        for n in range(12):
            frota_service.criar_veiculo({"codigo": f"P{n:02d}", "nome": f"P{n}", "tipo": FrotaTipo.objects.get_or_create(nome="Trator")[0].pk}, self.admin)
        html = self.client.get(reverse("frota_visao_geral"), {"por_pagina": "10"}).content.decode()
        self.assertEqual(html.count("Por página"), 1)                         # o seletor antigo de cima saiu
        self.assertIn('name="por_pagina" value="10"', html)                   # e o valor escolhido segue nos envios do filtro

    def test_a_home_do_agro_mostra_infraestrutura_sincronizacao_e_o_mapa(self):
        html = self.client.get(reverse("agro_visao_geral")).content.decode()
        self.assertIn("Infraestrutura", html)
        self.assertIn("Sincronização com a TIM", html)
        self.assertIn(reverse("agro_mapa"), html)


def usuario_com(nome, *codenames):
    u = User.objects.create_user(nome, f"{nome}@x.com", "senha-boa-123")
    for c in codenames:
        u.user_permissions.add(Permission.objects.get(codename=c, content_type__app_label="ProjetoEstoque"))
    return u


class PortaDoAgroTests(TestCase):
    """O dashboard de `/agro/`: o que pede ação, o resumo, os gráficos e a atividade recente — só com o que a pessoa
    pode ver, sempre com número real e com estado vazio quando não há registro."""

    def setUp(self):
        self.admin = usuario_super("porta_admin")
        self.trator = FrotaTipo.objects.get_or_create(nome="Trator")[0]

    def porta(self, usuario=None, **params):
        self.client.force_login(usuario or self.admin)
        return self.client.get(reverse("agro_visao_geral"), params)

    def v(self, codigo, **extra):
        return frota_service.criar_veiculo({"codigo": codigo, "nome": codigo, "tipo": self.trator.pk, **extra}, self.admin)

    def test_sem_nenhum_registro_a_porta_mostra_vazios_e_nenhum_grafico_de_zeros(self):
        r = self.porta()
        html = r.content.decode()
        self.assertEqual(r.status_code, 200)
        for titulo in ("Requer atenção", "Equipamentos por categoria", "Consumo de dados por dia", "Cadastro e mapa",
                       "Enlaces por estado registrado", "Últimas movimentações de equipamentos", "Sincronização com a TIM"):
            self.assertIn(titulo, html)
        self.assertIn("Nenhum equipamento cadastrado.", html)
        self.assertIn("Nenhuma sessão de dados registrada no período", html)
        self.assertIn("Nenhum enlace cadastrado.", html)
        self.assertIn("Nenhuma movimentação registrada.", html)
        self.assertNotIn("% da franquia", html)
        for marcador in ("{{", "{%", "{#"):
            self.assertNotIn(marcador, html)

    def test_o_resumo_de_equipamentos_e_o_do_cadastro_sao_contados_do_cadastro(self):
        karitel, _ = frota_service.fazendas()
        self.v("T1", localidade=karitel.pk, fabricante="John Deere", modelo="6130", latitude="-14.77", longitude="-45.56")
        self.v("T2", fabricante="Stara")
        self.v("T3", status="manutencao")
        painel = self.porta().context["painel"]
        self.assertEqual(painel["kpis_frota"]["total"], 3)
        self.assertEqual(painel["kpis_frota"]["em_manutencao"], 1)
        self.assertEqual(painel["frota"]["total"], 3)
        self.assertEqual(painel["frota"]["com_localizacao"], 1)
        self.assertEqual(painel["frota"]["resumo_mapa"], "1 de 3 equipamentos têm coordenada cadastrada e aparecem no mapa.")
        faltas = {b["rotulo"]: b["n"] for b in painel["frota"]["cadastro"]}
        self.assertEqual(faltas["Sem localização no mapa"], 2)
        self.assertEqual(faltas["Sem fazenda"], 2)
        self.assertEqual(faltas["Sem fabricante"], 1)
        html = self.porta().content.decode()
        self.assertIn("1 de 3 equipamentos têm coordenada cadastrada", html)
        self.assertIn(reverse("frota_visao_geral") + "?localizacao=sem", html)

    def test_cada_item_de_atencao_abre_a_lista_que_tem_o_mesmo_numero(self):
        from ProjetoEstoque.models import TecEnlace, TecEstrutura
        a = TecEstrutura.objects.create(nome="Torre A", tipo="torre")
        b = TecEstrutura.objects.create(nome="Torre B", tipo="torre", latitude="-14.7", longitude="-45.5")
        TecEnlace.objects.create(identificacao="E1", estrutura_a=a, estrutura_b=b, estado="indisponivel")
        TecEnlace.objects.create(identificacao="E2", estrutura_a=a, estrutura_b=b, estado="nao_informado")
        self.v("T1", validacao_pendente=True)
        atencao = {i["rotulo"]: i for i in self.porta().context["painel"]["atencao"]}
        self.assertEqual(atencao["Enlaces indisponíveis"]["n"], 1)
        self.assertEqual(atencao["Enlaces sem estado"]["n"], 1)
        self.assertEqual(atencao["Estruturas sem coordenada"]["n"], 1)
        self.assertEqual(atencao["Aguardando validação"]["n"], 1)
        self.assertEqual(atencao["Enlaces indisponíveis"]["url"], reverse("infra_enlaces") + "?estado=indisponivel")
        # o número do cartão é o que a lista filtrada mostra
        lista = self.client.get(reverse("infra_enlaces"), {"estado": "indisponivel"})
        self.assertEqual(lista.context["page_obj"].paginator.count, 1)
        sem_estado = self.client.get(reverse("infra_enlaces"), {"estado": "nao_informado"})
        self.assertEqual(sem_estado.context["page_obj"].paginator.count, 1)
        sem_coord = self.client.get(reverse("infra_estruturas"), {"coordenada": "sem"})
        self.assertEqual(sem_coord.context["page_obj"].paginator.count, 1)
        validacao = self.client.get(reverse("frota_visao_geral"), {"validacao": "pendente"})
        self.assertEqual(validacao.context["page_obj"].paginator.count, 1)

    def test_o_que_esta_quebrado_vem_antes_do_que_esta_por_cuidar(self):
        from ProjetoEstoque.models import TecEnlace, TecEstrutura
        a = TecEstrutura.objects.create(nome="Torre A", tipo="torre")
        b = TecEstrutura.objects.create(nome="Torre B", tipo="torre")
        TecEnlace.objects.create(identificacao="E1", estrutura_a=a, estrutura_b=b, estado="manutencao")
        TecEnlace.objects.create(identificacao="E2", estrutura_a=a, estrutura_b=b, estado="indisponivel")
        tons = [i["tom"] for i in self.porta().context["painel"]["atencao"]]
        self.assertEqual(tons, sorted(tons, key=lambda t: {"erro": 0, "atencao": 1}[t]))
        self.assertEqual(tons[0], "erro")

    def test_o_consumo_segue_o_periodo_pedido_e_um_periodo_invalido_volta_ao_padrao(self):
        c = chip(1)
        sessao(c, 20, 3 * 1024 * 1024, 1)
        r30, r7 = self.porta(periodo="30"), self.porta(periodo="7")
        self.assertEqual(r30.context["painel"]["consumo"]["total_bytes"], 3 * 1024 * 1024)
        self.assertEqual(r7.context["painel"]["consumo"]["total_bytes"], 0)
        self.assertEqual(self.porta(periodo="999").context["periodo"], 30)
        html = r30.content.decode()
        self.assertIn("3 MB", html)
        self.assertIn("Chips com maior consumo", html)
        self.assertIn(reverse("m2m_chip_detalhe", args=[c.pk]), html)

    def test_a_porta_nao_dispara_consulta_nenhuma_a_api(self):
        with mock.patch("services.tim_m2m_service.ClienteTIM._enviar") as enviar:
            self.porta()
        enviar.assert_not_called()

    def test_quem_so_ve_frotas_nao_recebe_conectividade_nem_infraestrutura(self):
        painel = self.porta(usuario_com("so_frota_porta", "ver_frota", "acessar_zelo_agro")).context["painel"]
        self.assertIsNotNone(painel["kpis_frota"])
        for chave in ("kpis_conectividade", "consumo", "maiores", "sincronizacao", "infra", "infra_estados", "alertas_abertos"):
            self.assertIsNone(painel[chave], chave)
        self.assertFalse(painel["vazio"])
        html = self.porta(usuario_com("so_frota_porta2", "ver_frota", "acessar_zelo_agro")).content.decode()
        self.assertIn("Equipamentos por categoria", html)
        for ausente in ("Consumo de dados por dia", "Enlaces por estado registrado", "Sincronização com a TIM"):
            self.assertNotIn(ausente, html)

    def test_quem_so_ve_chips_nao_recebe_a_frota(self):
        # com permissão de API (`consultar_conectividade`) a porta traz o consumo e a sincronização
        painel = self.porta(usuario_com("so_chips_porta", "ver_chips", "consultar_conectividade", "acessar_zelo_agro")).context["painel"]
        self.assertIsNone(painel["frota"])
        self.assertIsNone(painel["kpis_frota"])
        self.assertIsNone(painel["movimentacoes"])
        self.assertIsNotNone(painel["consumo"])
        self.assertTrue(all(i["modulo"] == "Conectividade" for i in painel["atencao"]))

    def test_quem_so_ve_chips_sem_permissao_de_api_recebe_o_chip_so_como_cadastro_e_vinculo(self):
        """Portal do Colaborador (regra 71): `ver_chips` mostra o chip; o que a operadora respondeu pede permissão de API."""
        painel = self.porta(usuario_com("so_chips_sem_api", "ver_chips", "acessar_zelo_agro")).context["painel"]
        self.assertIsNone(painel["frota"])
        for chave in ("consumo", "maiores", "sincronizacao", "alertas_abertos"):
            self.assertIsNone(painel[chave], chave)
        self.assertIn("chips_total", painel["kpis_conectividade"])
        self.assertNotIn("com_sessao_ativa", painel["kpis_conectividade"])

    def test_quem_so_ve_infraestrutura_nao_recebe_frota_nem_chips(self):
        painel = self.porta(usuario_com("so_infra_porta", "ver_tec_estoque", "acessar_zelo_agro")).context["painel"]
        self.assertIsNotNone(painel["infra"])
        for chave in ("frota", "kpis_frota", "kpis_conectividade", "consumo", "sincronizacao", "movimentacoes"):
            self.assertIsNone(painel[chave], chave)
        self.assertTrue(all(i["modulo"] == "Infraestrutura" for i in painel["atencao"]))

    def test_sem_permissao_de_modulo_nenhum_diz_que_nada_foi_liberado(self):
        r = self.porta(usuario_com("sem_modulo_porta", "acessar_zelo_agro"))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.context["painel"]["vazio"])
        self.assertIn("Nenhum módulo do Agro liberado", r.content.decode())

    def test_a_sincronizacao_parada_aparece_em_atencao(self):
        with mock.patch("services.m2m_monitoramento_service.estado_sincronizacao", return_value={
            "estado": "desatualizada", "rotulo": "Dados desatualizados", "tom": "erro", "motivo": "A tarefa do Agendador não rodou há 12 min.",
            "ultima_sincronizacao": None, "ultima_resposta_valida": None, "proxima": None,
        }):
            r = self.porta()
        html = r.content.decode()
        self.assertTrue(r.context["painel"]["sincronizacao_pede_atencao"])
        self.assertIn("A tarefa do Agendador não rodou há 12 min.", html)
        self.assertNotIn("Nenhuma pendência", html)


class ComandoSobPythonwTests(TestCase):
    """A tarefa de 1 em 1 minuto roda com `pythonw`: `sys.stdout` e `sys.stderr` são None, e o comando não pode estourar."""

    def test_o_comando_nasce_sem_stdout_nem_stderr(self):
        from ProjetoEstoque.management.commands.monitorar_m2m import Command
        with mock.patch.object(sys, "stdout", None), mock.patch.object(sys, "stderr", None):
            comando = Command()
            comando.stdout.write("não estoura")
            comando.stderr.write("nem este")

    def test_sem_token_o_comando_ainda_sai_com_erro_claro_mesmo_sem_console(self):
        from django.core.management.base import CommandError
        with override_settings(TIM_M2M_TOKEN="", TIM_M2M_CLIENT_ID="", TIM_M2M_CLIENT_SECRET=""):
            with self.assertRaises(CommandError):
                call_command("monitorar_m2m", stdout=io.StringIO())
