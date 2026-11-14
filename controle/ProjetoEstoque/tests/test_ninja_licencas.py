"""
Licenças do NinjaOne presas a máquinas paradas (pedido de 2026-11-14).

O NinjaOne cobra por dispositivo; máquina em Backup, Defeito, Manutenção ou Pausado não trabalha, mas o agente continua
ocupando licença até o TI removê-la lá. A lista (`ninja_licenca_service`), a tela e o e-mail leem a MESMA fonte.
Travado aqui:

  • entra só o que o pedido cita (backup, defeito, manutenção, pausado) E tem dispositivo do NinjaOne LIGADO ao
    equipamento; dispositivo sem equipamento não entra (não se sabe o status) — só é contado;
  • o recado "já removi" tira a máquina da lista, mas se o aparelho voltar a falar com o NinjaOne DEPOIS dele a máquina
    reaparece (a remoção não pegou); marcar usa `update()` e NÃO mexe em `last_sync`;
  • custo fixo de 3 consultas, qualquer que seja o parque;
  • o e-mail vai pelo canal `ninja_licencas_liberar` (destinatários e liga/desliga da Central de Alertas), leva o CSV
    completo, escapa o que vem do NinjaOne e NÃO sai quando não há nada a remover;
  • a tela lista, filtra, exporta, marca e reenvia; o redirecionamento pós-ação só volta para a própria tela;
  • o agendador semanal é o MESMO do de computadores parados, com outro nome de tarefa e outro alerta.
"""
import datetime
from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ProjetoEstoque.models import (
    CanalNotificacao, Categoria, Item, ItemStatusHistorico, NinjaDevice, StatusItemChoices, Subtipo,
)
from services import email_alertas as alertas
from services import ninja_licenca_service as nl


class Base(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="ti_ninja", password="x", first_name="Ana", last_name="Souza")
        self.client.force_login(self.user)
        self.categoria = Categoria.objects.create(nome="Computadores")
        self.subtipo = Subtipo.objects.create(nome="Notebook", alocado="sim", categoria=self.categoria)
        self._n = 0

    def maquina(self, status, *, nome=None, ligada=True, **ninja):
        """Equipamento no `status` e (se `ligada`) o dispositivo do NinjaOne ligado a ele."""
        self._n += 1
        nome = nome or f"NB-{self._n:03d}"
        item = Item.objects.create(nome=nome, subtipo=self.subtipo, categoria=self.categoria, status=status, numero_serie=f"SN{self._n:04d}")
        campos = {"display_name": f"DEV-{nome}", "ninja_id": 1000 + self._n, "serial_number": f"SN{self._n:04d}",
                  "last_contact": timezone.now() - datetime.timedelta(days=30), "is_online": False, **ninja}
        return NinjaDevice.objects.create(item=item if ligada else None, **campos)


class QuemEntraNaListaTests(Base):
    def test_entram_so_os_quatro_status_do_pedido(self):
        for s in ("backup", "defeito", "manutencao", "pausado"):
            self.maquina(s)
        for s in ("ativo", "estoque", "reservado", "descarte", "devolvido"):
            self.maquina(s)
        self.assertEqual({l.status for l in nl.pendentes()}, {"backup", "defeito", "manutencao", "pausado"})
        self.assertEqual(len(nl.pendentes()), 4)

    def test_dispositivo_sem_equipamento_ligado_nao_entra_mas_e_contado(self):
        self.maquina("pausado", ligada=False)
        self.maquina("pausado", ligada=False)
        self.maquina("pausado")
        self.assertEqual(len(nl.pendentes()), 1)
        self.assertEqual(nl.sem_vinculo(), 2)

    def test_a_ordem_e_status_depois_o_mais_antigo_depois_o_nome(self):
        a = self.maquina("pausado", nome="B-PAUSADO-NOVO")
        b = self.maquina("pausado", nome="A-PAUSADO-ANTIGO")
        c = self.maquina("backup", nome="C-BACKUP")
        ItemStatusHistorico.objects.filter(item=b.item, status_novo="pausado").update(alterado_em=timezone.now() - datetime.timedelta(days=90))
        ItemStatusHistorico.objects.filter(item=a.item, status_novo="pausado").update(alterado_em=timezone.now() - datetime.timedelta(days=5))
        self.assertEqual([l.item.nome for l in nl.pendentes()], ["C-BACKUP", "A-PAUSADO-ANTIGO", "B-PAUSADO-NOVO"])
        self.assertTrue(c)

    def test_parado_ha_quantos_dias_e_none_quando_o_historico_nao_diz(self):
        d = self.maquina("pausado")
        ItemStatusHistorico.objects.filter(item=d.item, status_novo="pausado").update(alterado_em=timezone.now() - datetime.timedelta(days=40))
        sem = self.maquina("pausado")
        ItemStatusHistorico.objects.filter(item=sem.item).delete()
        por_item = {l.item.pk: l for l in nl.pendentes()}
        self.assertEqual(por_item[d.item.pk].dias, 40)
        self.assertIsNone(por_item[sem.item.pk].dias)             # "não sei desde quando" não vira "parou hoje"

    def test_o_custo_e_fixo_qualquer_que_seja_o_parque(self):
        """Dispositivos+equipamentos numa consulta e o histórico de status noutra; o painel soma a contagem dos sem vínculo."""
        self.maquina("backup")
        with self.assertNumQueries(2):
            nl.linhas()
        with self.assertNumQueries(3):
            nl.panorama()
        for _ in range(12):
            self.maquina("pausado")
        with self.assertNumQueries(2):
            self.assertEqual(len(nl.linhas()), 13)
        with self.assertNumQueries(3):
            nl.panorama()


class RecadoDeRemocaoTests(Base):
    def test_marcar_tira_da_lista_sem_mexer_em_last_sync(self):
        d = self.maquina("pausado")
        antes = NinjaDevice.objects.get(pk=d.pk).last_sync
        self.assertEqual(nl.marcar_removidas([d.pk], self.user), 1)
        d.refresh_from_db()
        self.assertEqual(nl.pendentes(), [])
        self.assertIsNotNone(d.licenca_liberada_em)
        self.assertEqual(d.licenca_liberada_por, self.user)
        self.assertEqual(d.last_sync, antes)                      # `save()` mudaria "Última importação"; isto não é importação

    def test_so_vale_para_maquina_da_lista(self):
        ativa = self.maquina("ativo")
        self.assertEqual(nl.marcar_removidas([ativa.pk], self.user), 0)
        ativa.refresh_from_db()
        self.assertIsNone(ativa.licenca_liberada_em)

    def test_se_o_aparelho_volta_a_falar_depois_do_recado_a_maquina_reaparece(self):
        d = self.maquina("pausado", last_contact=timezone.now() - datetime.timedelta(days=60))
        nl.marcar_removidas([d.pk], self.user)
        self.assertEqual(nl.pendentes(), [])
        NinjaDevice.objects.filter(pk=d.pk).update(last_contact=timezone.now() + datetime.timedelta(minutes=1))
        volta = nl.pendentes()
        self.assertEqual(len(volta), 1)
        self.assertTrue(volta[0].voltou)

    def test_contato_anterior_ao_recado_nao_faz_voltar(self):
        d = self.maquina("pausado", last_contact=timezone.now() - datetime.timedelta(days=3))
        nl.marcar_removidas([d.pk], self.user)
        self.assertEqual(nl.pendentes(), [])
        self.assertEqual([l.item.pk for l in nl.linhas() if not l.pendente], [d.item.pk])

    def test_reabrir_devolve_a_maquina_para_a_lista(self):
        d = self.maquina("pausado")
        nl.marcar_removidas([d.pk], self.user)
        self.assertEqual(nl.reabrir([d.pk]), 1)
        d.refresh_from_db()
        self.assertEqual((len(nl.pendentes()), d.licenca_liberada_em, d.licenca_liberada_por), (1, None, None))


class PanoramaECsvTests(Base):
    def setUp(self):
        super().setUp()
        self.a = self.maquina("backup", nome="NB-ALFA", is_online=True)
        self.b = self.maquina("pausado", nome="NB-BETA")
        self.c = self.maquina("defeito", nome="NB-GAMA")
        nl.marcar_removidas([self.c.pk], self.user)

    def nomes(self, **filtros):
        return [l.item.nome for l in nl.panorama(**filtros)["linhas"]]

    def test_os_totais_sao_do_universo_inteiro_e_nao_mudam_com_o_filtro(self):
        p = nl.panorama(status="backup")
        self.assertEqual((p["total"], p["a_remover"], p["removidas"], p["online"]), (3, 2, 1, 1))
        self.assertEqual({s["valor"]: s["n"] for s in p["por_status"]}, {"backup": 1, "defeito": 0, "manutencao": 0, "pausado": 1})

    def test_filtros_de_situacao_status_e_busca(self):
        self.assertEqual(sorted(self.nomes()), ["NB-ALFA", "NB-BETA"])                                  # padrão: a remover
        self.assertEqual(self.nomes(situacao="liberadas"), ["NB-GAMA"])
        self.assertEqual(sorted(self.nomes(situacao="todas")), ["NB-ALFA", "NB-BETA", "NB-GAMA"])
        self.assertEqual(self.nomes(status="pausado"), ["NB-BETA"])
        self.assertEqual(self.nomes(busca="dev-nb-alfa"), ["NB-ALFA"])                                  # dispositivo, sem caixa
        self.assertEqual(self.nomes(busca=self.b.serial_number), ["NB-BETA"])
        self.assertEqual(sorted(self.nomes(situacao="inventada", status="inventado")), ["NB-ALFA", "NB-BETA"])   # valor estranho cai no padrão

    def test_csv_completo_com_bom_e_ponto_e_virgula(self):
        bruto = nl.csv_da_lista(nl.panorama(situacao="todas")["linhas"])
        self.assertTrue(bruto.startswith(b"\xef\xbb\xbf"))                                            # o Excel do Windows lê sem BOM como ANSI
        linhas = bruto.decode("utf-8-sig").strip().splitlines()
        self.assertEqual(len(linhas), 4)
        self.assertEqual(linhas[0].split(";"), nl.CABECALHO_CSV)
        gama = next(l for l in linhas if "NB-GAMA" in l)
        self.assertTrue(gama.endswith("Removida"))
        alfa = next(l for l in linhas if "NB-ALFA" in l)
        self.assertIn(";Sim;", alfa)
        self.assertTrue(alfa.endswith("A remover"))


class EmailTests(Base):
    def canal(self, **campos):
        alertas.sincronizar_catalogo_notificacoes()
        CanalNotificacao.objects.filter(codigo="ninja_licencas_liberar").update(**campos)

    def test_o_canal_esta_no_catalogo_e_e_fixo(self):
        meta = next(m for m in alertas.CATALOGO_NOTIFICACOES if m["codigo"] == "ninja_licencas_liberar")
        self.assertEqual(meta["tipo_destinatarios"], "fixo")                # destinatários customizáveis na Central de Alertas
        self.assertIn("agendar_ninja_licencas", meta["origem_disparo"])

    def test_sem_nada_a_remover_nenhum_email_sai(self):
        self.maquina("ativo")
        self.assertIsNone(alertas.enviar_ninja_licencas_detalhado())
        self.assertFalse(alertas.alerta_ninja_licencas())
        self.assertEqual(mail.outbox, [])

    def test_a_lista_toda_ja_removida_tambem_nao_gera_email(self):
        d = self.maquina("pausado")
        nl.marcar_removidas([d.pk], self.user)
        self.assertIsNone(alertas.enviar_ninja_licencas_detalhado())
        self.assertEqual(mail.outbox, [])

    def test_envia_para_os_destinatarios_cadastrados_em_notificacoes_com_o_csv_completo(self):
        self.maquina("backup", nome="NB-ALFA", is_online=True)
        self.maquina("pausado", nome="NB-BETA")
        self.maquina("manutencao", nome="NB-DELTA")
        self.canal(destinatarios_customizados="ti@exemplo.com.br, gestor@exemplo.com.br", destinatarios_customizados_ativo=True)

        r = alertas.enviar_ninja_licencas_detalhado()

        self.assertTrue(r.ok)
        self.assertEqual(sorted(r.destinatarios), ["gestor@exemplo.com.br", "ti@exemplo.com.br"])
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(sorted(msg.to), ["gestor@exemplo.com.br", "ti@exemplo.com.br"])
        self.assertIn("3 máquina(s) parada(s) ainda com licença NinjaOne", msg.subject)
        corpo = msg.alternatives[0][0]
        for trecho in ("Backup (1)", "Pausado (1)", "Manutenção (1)", "NB-ALFA", "ONLINE"):
            self.assertIn(trecho, corpo)
        self.assertNotIn("Defeito (", corpo)                                   # status sem máquina não vira seção vazia
        nome, conteudo, tipo = msg.attachments[0]
        self.assertTrue(nome.startswith("ninja_licencas_a_remover_") and nome.endswith(".csv"))
        self.assertEqual(tipo, "text/csv")
        texto = conteudo.decode("utf-8-sig") if isinstance(conteudo, bytes) else conteudo.lstrip("﻿")   # o Django decodifica text/* ao anexar
        self.assertEqual(len(texto.strip().splitlines()), 4)
        canal = CanalNotificacao.objects.get(codigo="ninja_licencas_liberar")
        self.assertEqual(canal.total_envios, 1)                                 # a Central de Alertas conta o envio

    def test_canal_desligado_na_central_de_alertas_nao_envia_e_diz_por_que(self):
        self.maquina("pausado")
        self.canal(ativo=False)
        r = alertas.enviar_ninja_licencas_detalhado()
        self.assertFalse(r.ok)
        self.assertIn("desativado", r.motivo)
        self.assertEqual(mail.outbox, [])

    def test_nome_vindo_do_ninjaone_e_escapado_no_html(self):
        self.maquina("pausado", display_name='<script>alert(1)</script>')
        self.canal(destinatarios_customizados="ti@exemplo.com.br", destinatarios_customizados_ativo=True)
        alertas.enviar_ninja_licencas_detalhado()
        corpo = mail.outbox[0].alternatives[0][0]
        self.assertNotIn("<script>alert(1)</script>", corpo)
        self.assertIn("&lt;script&gt;", corpo)

    def test_maquina_que_voltou_apos_a_remocao_e_destacada(self):
        d = self.maquina("pausado", last_contact=timezone.now() - datetime.timedelta(days=9))
        nl.marcar_removidas([d.pk], self.user)
        NinjaDevice.objects.filter(pk=d.pk).update(last_contact=timezone.now() + datetime.timedelta(minutes=1))
        self.canal(destinatarios_customizados="ti@exemplo.com.br", destinatarios_customizados_ativo=True)
        alertas.enviar_ninja_licencas_detalhado()
        self.assertIn("VOLTOU APÓS A REMOÇÃO", mail.outbox[0].alternatives[0][0])

    def test_alerta_bool_para_o_agendador(self):
        self.maquina("defeito")
        self.canal(destinatarios_customizados="ti@exemplo.com.br", destinatarios_customizados_ativo=True)
        self.assertTrue(alertas.alerta_ninja_licencas())


class TelaTests(Base):
    def setUp(self):
        super().setUp()
        self.d1 = self.maquina("backup", nome="NB-ALFA", is_online=True)
        self.d2 = self.maquina("pausado", nome="NB-BETA")
        self.url = reverse("ninja_licencas_liberar")

    def test_lista_a_remover_com_os_indicadores_e_o_destino_do_aviso(self):
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        for trecho in ("NB-ALFA", "NB-BETA", "DEV-NB-ALFA", "Licenças a liberar", "Aviso por e-mail", "Enviar e-mail agora",
                       reverse("ninja_licencas_enviar"), reverse("ninja_licencas_marcar")):
            self.assertIn(trecho, html)
        self.assertEqual(r.context["p"]["a_remover"], 2)

    def test_exige_login(self):
        self.client.logout()
        for nome in ("ninja_licencas_liberar", "ninja_licencas_marcar", "ninja_licencas_enviar"):
            r = self.client.get(reverse(nome)) if nome == "ninja_licencas_liberar" else self.client.post(reverse(nome))
            self.assertEqual(r.status_code, 302, nome)
            self.assertIn("login", r["Location"])

    def test_marcar_e_enviar_so_aceitam_post(self):
        for nome in ("ninja_licencas_marcar", "ninja_licencas_enviar"):
            self.assertEqual(self.client.get(reverse(nome)).status_code, 405, nome)

    def test_filtra_pela_situacao_do_equipamento_e_pela_licenca(self):
        html = self.client.get(self.url, {"status": "pausado"}).content.decode()
        self.assertIn("NB-BETA", html)
        self.assertNotIn("NB-ALFA", html)
        nl.marcar_removidas([self.d2.pk], self.user)
        liberadas = self.client.get(self.url, {"situacao": "liberadas"}).content.decode()
        self.assertIn("NB-BETA", liberadas)                                        # a já removida só aparece no filtro "Já removidas"
        self.assertNotIn("NB-ALFA", liberadas)
        self.assertIn("Nenhuma máquina para os filtros aplicados", self.client.get(self.url, {"situacao": "liberadas", "status": "defeito"}).content.decode())

    def test_exporta_csv_respeitando_o_filtro(self):
        r = self.client.get(self.url, {"export": "csv", "status": "backup"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/csv", r["Content-Type"])
        self.assertIn("attachment", r["Content-Disposition"])
        linhas = r.content.decode("utf-8-sig").strip().splitlines()
        self.assertEqual(len(linhas), 2)
        self.assertIn("NB-ALFA", linhas[1])

    def test_marcar_como_removida_grava_e_volta_para_a_lista_com_os_filtros(self):
        voltar = self.url + "?status=pausado"
        r = self.client.post(reverse("ninja_licencas_marcar"), {"acao": "remover", "ids": [self.d2.pk], "voltar": voltar})
        self.assertRedirects(r, voltar, fetch_redirect_response=False)
        self.d2.refresh_from_db()
        self.assertEqual(self.d2.licenca_liberada_por, self.user)
        self.assertEqual(len(nl.pendentes()), 1)
        r = self.client.post(reverse("ninja_licencas_marcar"), {"acao": "reabrir", "ids": [self.d2.pk]})
        self.d2.refresh_from_db()
        self.assertIsNone(self.d2.licenca_liberada_em)

    def test_sem_marcar_nada_avisa_e_nao_grava(self):
        r = self.client.post(reverse("ninja_licencas_marcar"), {"acao": "remover"}, follow=True)
        self.assertIn("Marque ao menos uma máquina", [str(m) for m in r.context["messages"]][0])
        self.assertEqual(NinjaDevice.objects.filter(licenca_liberada_em__isnull=False).count(), 0)

    def test_acao_desconhecida_e_recusada(self):
        r = self.client.post(reverse("ninja_licencas_marcar"), {"acao": "apagar", "ids": [self.d1.pk]}, follow=True)
        self.assertIn("Ação desconhecida", str(list(r.context["messages"])[0]))
        self.d1.refresh_from_db()
        self.assertIsNone(self.d1.licenca_liberada_em)

    def test_o_retorno_so_aceita_a_propria_tela_nunca_endereco_de_fora(self):
        base = self.url
        for hostil in ("https://exemplo.com/roubo/", "//exemplo.com/roubo/", "/equipamentos/", "javascript:alert(1)"):
            r = self.client.post(reverse("ninja_licencas_marcar"), {"acao": "remover", "ids": [self.d1.pk], "voltar": hostil})
            self.assertRedirects(r, base, fetch_redirect_response=False)

    def test_enviar_agora_conta_ao_usuario_o_que_aconteceu(self):
        CanalNotificacao.objects.filter(codigo="ninja_licencas_liberar").delete()
        alertas.sincronizar_catalogo_notificacoes()
        CanalNotificacao.objects.filter(codigo="ninja_licencas_liberar").update(
            destinatarios_customizados="ti@exemplo.com.br", destinatarios_customizados_ativo=True)
        r = self.client.post(reverse("ninja_licencas_enviar"), follow=True)
        self.assertIn("E-mail enviado para: ti@exemplo.com.br", str(list(r.context["messages"])[0]))
        self.assertEqual(len(mail.outbox), 1)

        CanalNotificacao.objects.filter(codigo="ninja_licencas_liberar").update(ativo=False)
        r = self.client.post(reverse("ninja_licencas_enviar"), follow=True)
        self.assertIn("desativado", str(list(r.context["messages"])[0]))
        self.assertEqual(len(mail.outbox), 1)

        NinjaDevice.objects.update(licenca_liberada_em=timezone.now() + datetime.timedelta(days=1))
        r = self.client.post(reverse("ninja_licencas_enviar"), follow=True)
        self.assertIn("Nenhuma máquina parada", str(list(r.context["messages"])[0]))

    def test_o_painel_do_ninjaone_aponta_para_a_tela(self):
        html = self.client.get(reverse("ninja_dashboard")).content.decode()
        self.assertIn(self.url, html)
        self.assertIn("2 máquinas paradas", html)
        NinjaDevice.objects.update(licenca_liberada_em=timezone.now() + datetime.timedelta(days=1))
        self.assertNotIn(self.url, self.client.get(reverse("ninja_dashboard")).content.decode())   # nada a liberar: a faixa some


class AgendadorTests(TestCase):
    def test_o_agendador_semanal_e_o_mesmo_com_outro_nome_e_outro_alerta(self):
        from ProjetoEstoque.management.commands import agendar_computadores_parados as comp
        from ProjetoEstoque.management.commands import agendar_ninja_licencas as ninja

        self.assertEqual((comp.Command.nome_tarefa, comp.Command.tipo_alerta), ("ControleEstoque_ComputadoresParados", "computadores"))
        self.assertEqual((ninja.Command.nome_tarefa, ninja.Command.tipo_alerta), ("ControleEstoque_NinjaLicencas", "ninja"))
        self.assertTrue(issubclass(ninja.Command, comp.Command))

    def test_enviar_alertas_tipo_ninja_chama_so_o_aviso_do_ninja(self):
        with mock.patch("services.email_alertas.alerta_ninja_licencas", return_value=True) as alerta, \
                mock.patch("services.email_alertas.alerta_computadores_parados") as outro:
            saida = StringIO()
            call_command("enviar_alertas", "--tipo", "ninja", stdout=saida)
        alerta.assert_called_once()
        outro.assert_not_called()
        self.assertIn("NinjaOne", saida.getvalue())

    def test_executar_pelo_agendador_dispara_o_aviso_do_ninja(self):
        with mock.patch("services.email_alertas.alerta_ninja_licencas", return_value=False) as alerta:
            saida = StringIO()
            call_command("agendar_ninja_licencas", "executar", stdout=saida)
        alerta.assert_called_once()
        self.assertIn("nenhuma máquina parada tem licença do NinjaOne", saida.getvalue())
