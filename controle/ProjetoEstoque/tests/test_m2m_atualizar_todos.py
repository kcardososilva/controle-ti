"""
"Atualizar todos" (15/11/2026): uma execução que atualiza todos os chips consultáveis, no limite de requisições da API.

O que estes testes protegem — quase tudo aqui falha em SILÊNCIO:

  • TODOS, E NÃO SÓ OS VENCIDOS — o lote ignora o vencimento e o corte `max_chips_execucao`; o ciclo comum continua como era.
  • QUEM PEDIU FICA GRAVADO — as consultas do lote saem `manual` e com o usuário, não como agendadas e sem autor.
  • UMA LEITURA DA LISTA PARA TODOS — a conta inteira numa chamada; nada de uma requisição por chip só para o resumo.
  • A TRAVA PROTEGE O LOTE — o ciclo agendado de 1 minuto julgaria "morta" uma execução de 8 minutos e atropelaria
    o lote, estourando juntos o limite de requisições. Quem manda é a execução manual em andamento.
  • A TELA SÓ CONFERE E ABRE O PROCESSO — a view nunca consulta a API; o andamento sai do banco.
  • NENHUM TESTE TOCA A REDE: sessão HTTP falsa, `Popen` trocado.
"""

import subprocess
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse
from django.utils import timezone

from ProjetoEstoque.models import M2MChip, M2MConsulta, M2MExecucao, M2MIntegracao
from ProjetoEstoque.tests.test_m2m import Sessao, roteador
from ProjetoEstoque.tests.test_m2m_sincronizacao import CicloBase
from ProjetoEstoque.tests.test_m2m_telas import TelasBase
from services import m2m_monitoramento_service as m2m
from services import tim_m2m_service as tim


class LoteBase(CicloBase):
    def todos(self, respostas=None, **kw):
        sessao = Sessao(respostas or roteador())
        resumo = m2m.executar_ciclo(
            m2m.ORIGEM_DO_LOTE, cliente=tim.ClienteTIM(sessao), ciclo_minutos=m2m.CICLO_DO_LOTE_MINUTOS,
            todos=True, usuario=self.usuario, **kw,
        )
        return resumo, sessao

    def chips_no_futuro(self, quantos):
        """`quantos` chips que o ciclo comum NÃO consultaria agora: a próxima consulta é daqui a uma hora."""
        chips = [self.chip(n) for n in range(1, quantos + 1)]
        M2MChip.objects.update(proxima_consulta_em=timezone.now() + timedelta(hours=1))
        return chips


class QuemEConsultadoTests(LoteBase):
    def test_o_ciclo_comum_pula_quem_nao_venceu_e_o_lote_nao(self):
        self.chips_no_futuro(3)
        comum, _ = self.ciclo(roteador())
        self.assertEqual(comum.consultados, 0)
        lote, _ = self.todos()
        self.assertEqual((lote.executou, lote.consultados, lote.vencidos), (True, 3, 3))

    def test_o_lote_nao_tem_o_corte_de_chips_por_execucao(self):
        M2MIntegracao.objects.filter(pk=1).update(max_chips_execucao=1)
        for n in range(1, 4):
            self.chip(n)
        comum, _ = self.ciclo(roteador())
        self.assertEqual(comum.consultados, 1)                      # o ciclo comum respeita o corte
        M2MChip.objects.update(proxima_consulta_em=timezone.now() + timedelta(hours=1))
        lote, _ = self.todos()
        self.assertEqual(lote.consultados, 3)

    def test_so_entra_chip_consultavel(self):
        self.chip(1)
        desativado = self.chip(2)
        m2m.desativar(desativado, self.usuario, "devolvido")
        lote, _ = self.todos()
        self.assertEqual((lote.vencidos, lote.consultados), (1, 1))

    def test_quem_tem_sessao_ativa_vem_primeiro(self):
        antigo, ativo = self.chip(1), self.chip(2)
        M2MChip.objects.filter(pk=ativo.pk).update(sessao_estado="ativa")
        ordem = []
        original = m2m.atualizar

        def espiar(chip, *a, **kw):
            ordem.append(chip.pk)
            return original(chip, *a, **kw)

        with mock.patch.object(m2m, "atualizar", side_effect=espiar):
            self.todos()
        self.assertEqual(ordem, [ativo.pk, antigo.pk])


class QuemPediuFicaGravadoTests(LoteBase):
    def test_as_consultas_do_lote_saem_manuais_e_com_o_usuario(self):
        chip = self.chip(1)
        self.chip(2)
        self.todos()
        consultas = M2MConsulta.objects.filter(chip=chip)
        self.assertTrue(consultas.exists())
        self.assertEqual({(c.origem, c.usuario_id) for c in consultas}, {("manual", self.usuario.pk)})

    def test_o_ciclo_comum_continua_agendado_e_sem_autor(self):
        chip = self.chip(1)
        self.ciclo(roteador())
        consultas = M2MConsulta.objects.filter(chip=chip)
        self.assertEqual({(c.origem, c.usuario_id) for c in consultas}, {("agendada", None)})

    def test_a_execucao_fica_registrada_como_manual_com_o_total(self):
        for n in range(1, 4):
            self.chip(n)
        resumo, _ = self.todos()
        execucao = M2MExecucao.objects.get(pk=resumo.execucao_id)
        self.assertEqual(
            (execucao.origem, execucao.estado, execucao.vencidos, execucao.consultados, execucao.pendentes),
            ("manual", M2MExecucao.Estado.CONCLUIDA, 3, 3, 0),
        )


class LimiteDaApiTests(LoteBase):
    def test_a_lista_da_conta_e_lida_uma_vez_para_todos_os_chips(self):
        for n in range(1, 6):
            self.chip(n)
        resumo, sessao = self.todos()
        self.assertEqual(resumo.consultados, 5)
        self.assertEqual(len(sessao.chamadas), 1, "sessão e consumo dos 5 chips saem de UMA chamada à lista da conta")
        self.assertTrue(sessao.urls[0].split("?")[0].rstrip("/").endswith("/simcards"))

    def test_quando_o_tempo_do_ciclo_acaba_o_resto_fica_pendente_e_volta_para_a_fila(self):
        for n in range(1, 6):
            self.chip(n)
        relogio = iter([0.0, 0.0, 5.0] + [10_000.0] * 40)
        with mock.patch.object(m2m.time, "monotonic", side_effect=lambda: next(relogio)):
            resumo, _ = self.todos()
        self.assertTrue(0 < resumo.consultados < 5)
        self.assertEqual(resumo.pendentes, 5 - resumo.consultados)
        # quem não foi consultado continua vencido para o ciclo normal
        self.assertEqual(M2MChip.objects.filter(proxima_consulta_em__isnull=True).count(), resumo.pendentes)

    def test_o_progresso_e_gravado_de_poucos_em_poucos_chips_durante_a_execucao(self):
        for n in range(1, 6):
            self.chip(n)
        vistos = []
        original = m2m.atualizar

        def espiar(chip, *a, **kw):
            execucao = M2MExecucao.objects.get()
            vistos.append((execucao.consultados, execucao.vencidos, execucao.estado))
            return original(chip, *a, **kw)

        with mock.patch.object(m2m, "PASSO_DO_PROGRESSO_DO_LOTE", 2), mock.patch.object(m2m, "atualizar", side_effect=espiar):
            self.todos()
        andamento = M2MExecucao.Estado.EM_ANDAMENTO
        self.assertEqual(vistos, [(0, 5, andamento), (0, 5, andamento), (2, 5, andamento), (2, 5, andamento), (4, 5, andamento)])
        self.assertEqual(M2MExecucao.objects.get().consultados, 5)


class TravaTests(LoteBase):
    def test_lote_ja_em_andamento_e_pulado_sem_nenhuma_requisicao(self):
        self.chip(1)
        M2MIntegracao.objects.filter(pk=1).update(executando_desde=timezone.now() - timedelta(seconds=30))
        resumo, sessao = self.todos()
        self.assertEqual((resumo.executou, resumo.motivo_pulo), (False, "em_andamento"))
        self.assertEqual(sessao.chamadas, [])

    def test_o_ciclo_de_1_minuto_nao_toma_a_trava_de_um_lote_que_dura_mais_que_3_minutos(self):
        self.chip(1)
        inicio = timezone.now() - timedelta(minutes=10)
        M2MIntegracao.objects.filter(pk=1).update(executando_desde=inicio)
        M2MExecucao.objects.create(origem="manual", iniciada_em=inicio, vencidos=205, consultados=80)
        resumo, sessao = self.ciclo(roteador(), tique_minutos=1)
        self.assertEqual((resumo.executou, resumo.motivo_pulo), (False, "em_andamento"))
        self.assertEqual(sessao.chamadas, [], "duas execuções ao mesmo tempo estourariam juntas o limite da API")

    def test_lote_que_morreu_ha_mais_de_30_minutos_nao_segura_a_trava_para_sempre(self):
        self.chip(1)
        inicio = timezone.now() - timedelta(minutes=40)
        M2MIntegracao.objects.filter(pk=1).update(executando_desde=inicio)
        M2MExecucao.objects.create(origem="manual", iniciada_em=inicio, vencidos=205, consultados=80)
        resumo, _ = self.ciclo(roteador(), tique_minutos=1)
        self.assertTrue(resumo.executou)

    def test_execucao_agendada_travada_continua_expirando_em_3_minutos(self):
        """Regressão: a proteção é só do lote. O ciclo comum que morreu no meio libera a vez como sempre."""
        self.chip(1)
        M2MIntegracao.objects.filter(pk=1).update(executando_desde=timezone.now() - timedelta(minutes=4))
        resumo, _ = self.ciclo(roteador(), tique_minutos=1)
        self.assertTrue(resumo.executou)

    def test_a_trava_e_solta_no_fim_do_lote(self):
        self.chip(1)
        self.todos()
        self.assertIsNone(M2MIntegracao.get().executando_desde)


class ConferenciaPreviaTests(LoteBase):
    def recusa(self):
        with self.assertRaises(m2m.AtualizacaoRecusada) as cm:
            m2m.conferir_atualizar_todos()
        return cm.exception

    def test_devolve_quantos_chips_serao_atualizados(self):
        self.chip(1)
        self.chip(2)
        self.assertEqual(m2m.conferir_atualizar_todos(), 2)

    def test_sem_configuracao(self):
        self.chip(1)
        with mock.patch.object(tim, "motivo_nao_configurada", return_value="Sem token."):
            self.assertEqual(self.recusa().codigo, "configuracao")

    def test_api_limitando(self):
        self.chip(1)
        M2MIntegracao.objects.filter(pk=1).update(bloqueado_ate=timezone.now() + timedelta(minutes=5))
        recusa = self.recusa()
        self.assertEqual(recusa.codigo, "bloqueada")
        self.assertGreater(recusa.aguardar_s, 0)

    def test_credencial_recusada(self):
        self.chip(1)
        M2MIntegracao.objects.filter(pk=1).update(falha_autenticacao_em=timezone.now(), autenticada_em=None)
        self.assertEqual(self.recusa().codigo, "credencial_recusada")

    def test_ja_em_andamento_e_nenhum_chip_consultavel(self):
        self.assertEqual(self.recusa().codigo, "sem_chips")
        self.chip(1)
        M2MIntegracao.objects.filter(pk=1).update(executando_desde=timezone.now())
        self.assertEqual(self.recusa().codigo, "em_andamento")


class FaixaDeSincronizacaoTests(LoteBase):
    def test_lote_em_andamento_aparece_com_feitos_e_total(self):
        self.chip(1)
        agora = timezone.now()
        M2MIntegracao.objects.filter(pk=1).update(executando_desde=agora, ultima_execucao=agora)
        M2MExecucao.objects.create(origem="manual", iniciada_em=agora, vencidos=205, consultados=42)
        estado = m2m.estado_sincronizacao()
        self.assertEqual(estado["lote"], {"em_andamento": True, "feitos": 42, "total": 205})
        self.assertEqual(estado["estado"], "em_andamento")
        self.assertIn("42 de 205", estado["motivo"])

    def test_sem_lote_a_chave_e_vazia(self):
        self.chip(1)
        self.assertIsNone(m2m.estado_sincronizacao()["lote"])

    def test_execucao_em_andamento_de_um_processo_que_morreu_nao_conta_como_lote(self):
        """A linha `em_andamento` fica para sempre se o processo morre; sem a trava de pé, não é lote."""
        self.chip(1)
        M2MExecucao.objects.create(origem="manual", iniciada_em=timezone.now() - timedelta(minutes=5), vencidos=205, consultados=10)
        self.assertIsNone(m2m.estado_sincronizacao()["lote"])

    def test_ciclo_agendado_em_andamento_nao_e_lote(self):
        self.chip(1)
        agora = timezone.now()
        M2MIntegracao.objects.filter(pk=1).update(executando_desde=agora)
        M2MExecucao.objects.create(origem="agendada", iniciada_em=agora, vencidos=3, consultados=1)
        self.assertIsNone(m2m.estado_sincronizacao()["lote"])


class ComandoTests(LoteBase):
    def rodar(self, *args, respostas=None):
        sessao = Sessao(respostas or roteador())
        saida = StringIO()
        with mock.patch.object(tim, "ClienteTIM", return_value=tim.ClienteTIM(sessao)):
            call_command("monitorar_m2m", *args, stdout=saida)
        return saida.getvalue(), sessao

    def test_todos_atualiza_tudo_e_grava_quem_pediu(self):
        chips = self.chips_no_futuro(3)
        texto, _ = self.rodar("--todos", "--usuario", self.usuario.username)
        self.assertIn("Chips consultados: 3", texto)
        for chip in chips:
            consulta = M2MConsulta.objects.filter(chip=chip).first()
            self.assertEqual((consulta.origem, consulta.usuario), ("manual", self.usuario))

    def test_usuario_inexistente_e_erro_e_nada_e_consultado(self):
        self.chips_no_futuro(2)
        with self.assertRaises(CommandError) as cm:
            self.rodar("--todos", "--usuario", "ninguem")
        self.assertIn("ninguem", str(cm.exception))
        self.assertEqual(M2MConsulta.objects.count(), 0)

    def test_usuario_sem_todos_e_recusado(self):
        with self.assertRaises(CommandError):
            self.rodar("--usuario", self.usuario.username)

    def test_sem_usuario_o_lote_roda_do_mesmo_jeito_sem_autor(self):
        self.chip(1)
        self.rodar("--todos")
        self.assertIsNone(M2MConsulta.objects.first().usuario)

    def test_sem_todos_o_comando_continua_sendo_o_ciclo_de_sempre(self):
        self.chips_no_futuro(2)
        texto, _ = self.rodar()
        self.assertIn("Chips consultados: 0", texto)

    def test_a_saida_so_usa_caracteres_do_cp1252(self):
        self.chip(1)
        texto, _ = self.rodar("--todos")
        texto.encode("cp1252")


class ViewDoLoteTests(TelasBase):
    def setUp(self):
        super().setUp()
        self.mapear()
        self.url = reverse("m2m_atualizar_todos")

    def abrir_processo(self):
        patch = mock.patch("ProjetoEstoque.views.m2m_api.subprocess.Popen")
        popen = patch.start()
        self.addCleanup(patch.stop)
        return popen

    def test_abre_o_processo_do_comando_e_responde_202_sem_consultar_a_api(self):
        self.chip(1)
        self.chip(2)
        sessao = self.api_falsa(roteador())
        popen = self.abrir_processo()
        r = self.client.post(self.url)
        self.assertEqual(r.status_code, 202)
        corpo = r.json()
        self.assertTrue(corpo["ok"])
        self.assertEqual(corpo["total"], 2)
        self.assertIn("Atualizando 2 chips", corpo["mensagem"])
        popen.assert_called_once()
        comando = popen.call_args.args[0]
        self.assertTrue(comando[1].endswith("manage.py"))
        self.assertEqual(comando[2:], ["monitorar_m2m", "--todos", "--usuario", "admin_telas"])
        self.assertEqual(sessao.chamadas, [], "a view só confere e abre o processo: a API é consultada por ele")
        self.assertEqual(M2MConsulta.objects.count(), 0)

    def test_409_com_a_razao_quando_ja_ha_atualizacao_em_andamento(self):
        self.chip(1)
        M2MIntegracao.objects.filter(pk=1).update(executando_desde=timezone.now())
        popen = self.abrir_processo()
        r = self.client.post(self.url)
        self.assertEqual(r.status_code, 409)
        self.assertEqual((r.json()["recusada"], r.json()["codigo"]), (True, "em_andamento"))
        popen.assert_not_called()

    def test_409_sem_chip_consultavel_e_com_a_api_limitando(self):
        popen = self.abrir_processo()
        self.assertEqual(self.client.post(self.url).json()["codigo"], "sem_chips")
        self.chip(1)
        M2MIntegracao.objects.filter(pk=1).update(bloqueado_ate=timezone.now() + timedelta(minutes=3))
        r = self.client.post(self.url)
        self.assertEqual((r.status_code, r.json()["codigo"]), (409, "bloqueada"))
        popen.assert_not_called()

    def test_exige_login_permissao_de_consultar_e_post(self):
        self.chip(1)
        popen = self.abrir_processo()
        self.assertEqual(self.client_class().post(self.url).status_code, 302)
        so_ve = self.cliente_de(self.usuario_com("so_ve", "ver_chips"))
        self.assertEqual(so_ve.post(self.url).status_code, 403)
        self.assertEqual(self.client.get(self.url).status_code, 405)
        popen.assert_not_called()

    def test_o_processo_sai_do_servidor_sem_prender_a_requisicao(self):
        self.chip(1)
        popen = self.abrir_processo()
        self.client.post(self.url)
        opcoes = popen.call_args.kwargs
        for canal in ("stdin", "stdout", "stderr"):
            self.assertEqual(opcoes[canal], subprocess.DEVNULL)
        self.assertTrue(opcoes.get("creationflags") or opcoes.get("start_new_session"))

    def test_o_json_da_sincronizacao_traz_o_lote(self):
        self.chip(1)
        agora = timezone.now()
        M2MIntegracao.objects.filter(pk=1).update(executando_desde=agora, ultima_execucao=agora)
        M2MExecucao.objects.create(origem="manual", iniciada_em=agora, vencidos=10, consultados=4)
        corpo = self.client.get(reverse("m2m_sincronizacao")).json()
        self.assertEqual(corpo["lote"], {"em_andamento": True, "feitos": 4, "total": 10})
        M2MExecucao.objects.all().delete()
        M2MIntegracao.objects.filter(pk=1).update(executando_desde=None)
        self.assertIsNone(self.client.get(reverse("m2m_sincronizacao")).json()["lote"])

    def test_a_faixa_mostra_o_botao_so_para_quem_pode_consultar_a_api(self):
        self.chip(1)
        html = self.client.get(reverse("m2m_chips")).content.decode()
        self.assertIn("data-m2m-atualizar-todos", html)
        self.assertIn(self.url, html)
        self.assertIn('data-lote="0"', html)
        # em andamento: o botão nasce desabilitado e o texto do andamento já vem do servidor
        agora = timezone.now()
        M2MIntegracao.objects.filter(pk=1).update(executando_desde=agora, ultima_execucao=agora)
        M2MExecucao.objects.create(origem="manual", iniciada_em=agora, vencidos=7, consultados=3)
        html = self.client.get(reverse("m2m_chips")).content.decode()
        self.assertIn('data-lote="1"', html)
        self.assertIn("Atualizando 3 de 7", html)

    def test_quem_nao_ve_a_api_nao_ve_o_botao(self):
        self.chip(1)
        so_chips = self.cliente_de(self.usuario_com("so_chips", "ver_chips"))
        html = so_chips.get(reverse("m2m_chips")).content.decode()
        self.assertNotIn("data-m2m-atualizar-todos", html)
