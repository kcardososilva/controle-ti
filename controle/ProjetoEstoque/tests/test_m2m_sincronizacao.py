"""Sincronização AUTOMÁTICA dos chips (ciclo a cada minuto) — o que a tarefa do Agendador faz sem ninguém na tela.

Cobre o que o pedido de 14/11 exige e que NÃO depende do navegador: prioridade de quem tem sessão ativa,
tempo e limite de requisições do ciclo, backoff de quem falha, trava contra execução duplicada, registro de
cada execução (`M2MExecucao`), preservação do último dado válido e a faixa de estado (`estado_sincronizacao`).
A rede é a falsa de `test_m2m` (nenhuma chamada sai do computador)."""

from datetime import timedelta
from unittest import mock

from django.utils import timezone

from ProjetoEstoque.models import M2MChip, M2MExecucao, M2MIntegracao
from ProjetoEstoque.tests.test_m2m import BASE, M2MBase, Resp, Sessao, roteador
from services import m2m_monitoramento_service as m2m
from services import tim_m2m_service as tim


class CicloBase(M2MBase):
    def setUp(self):
        super().setUp()
        self.mapear()

    def ciclo(self, respostas, **kw):
        sessao = Sessao(respostas)
        resumo = m2m.executar_ciclo(cliente=tim.ClienteTIM(sessao), **kw)
        return resumo, sessao


class RitmoPorEstadoTests(CicloBase):
    def test_chip_com_sessao_ativa_volta_no_ritmo_curto_e_o_resto_no_de_repouso(self):
        M2MIntegracao.objects.filter(pk=1).update(intervalo_minutos=1, intervalo_repouso_minutos=20)
        ativo, parado = self.chip(1), self.chip(2)
        self.ciclo(roteador())                                                  # sessão de teste = online
        ativo.refresh_from_db()
        self.assertEqual(ativo.sessao_estado, "ativa")
        self.assertAlmostEqual((ativo.proxima_consulta_em - timezone.now()).total_seconds(), 60, delta=5)
        # o chip "sem sessão" segue o ritmo de repouso
        M2MChip.objects.filter(pk=parado.pk).update(sessao_estado="sem_sessao", proxima_consulta_em=None)
        self.atualizar(parado, roteador(sessao={"conexao": "offline"}), avaliar=False)
        self.assertAlmostEqual((parado.proxima_consulta_em - timezone.now()).total_seconds(), 20 * 60, delta=5)

    def test_backoff_dobra_a_espera_a_cada_falha_seguida_ate_o_teto(self):
        self.assertEqual([m2m.espera_apos_falha(1, n) for n in range(1, 8)], [1, 2, 4, 8, 16, 30, 30])
        self.assertEqual(m2m.espera_apos_falha(15, 1), 15)                      # nunca menos que o intervalo do chip
        self.assertEqual(m2m.espera_apos_falha(15, 2), 30)
        self.assertEqual(m2m.espera_apos_falha(15, 9), 30)                      # teto de 30 min

    def test_chip_que_falha_espera_mais_a_cada_tentativa(self):
        M2MIntegracao.objects.filter(pk=1).update(intervalo_minutos=1, intervalo_repouso_minutos=1)
        chip = self.chip(1)
        esperas = []
        for _ in range(4):
            self.atualizar(chip, roteador(status=500), avaliar=False)
            esperas.append(round((chip.proxima_consulta_em - timezone.now()).total_seconds() / 60))
        self.assertEqual(esperas, [1, 2, 4, 8])


class PrioridadeETempoTests(CicloBase):
    def test_quem_tem_sessao_ativa_e_consultado_primeiro(self):
        M2MIntegracao.objects.filter(pk=1).update(max_chips_execucao=1)
        antigo = self.chip(1)                                                   # mais atrasado, mas em repouso
        ativo = self.chip(2)
        M2MChip.objects.filter(pk=antigo.pk).update(proxima_consulta_em=timezone.now() - timedelta(hours=2))
        M2MChip.objects.filter(pk=ativo.pk).update(
            sessao_estado="ativa", proxima_consulta_em=timezone.now() - timedelta(minutes=1),
        )
        with mock.patch.object(m2m, "LIMIAR_LOTE", 99):
            resumo, sessao = self.ciclo(roteador())
        self.assertEqual(resumo.consultados, 1)
        self.assertEqual(sessao.urls, [f"{BASE}/simcards/{ativo.iccid}/"])

    def test_o_ciclo_para_quando_o_tempo_acaba_e_o_resto_fica_na_fila(self):
        for n in range(1, 6):
            self.chip(n)
        relogio = iter([0.0, 0.0, 5.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0])
        with mock.patch.object(m2m, "LIMIAR_LOTE", 99), mock.patch.object(m2m.time, "monotonic", side_effect=lambda: next(relogio)):
            resumo, _ = self.ciclo(roteador(), tique_minutos=1)                 # orçamento: 60 - 10 = 50 s
        self.assertTrue(0 < resumo.consultados < 5)
        self.assertEqual(resumo.pendentes, 5 - resumo.consultados)
        restantes = M2MChip.objects.filter(proxima_consulta_em__isnull=True).count()
        self.assertEqual(restantes, resumo.pendentes)                           # quem sobrou continua vencido para o próximo ciclo

    def test_o_orcamento_acompanha_o_tique_da_tarefa(self):
        self.assertEqual(m2m.orcamento_do_ciclo_s(1), 50)
        self.assertEqual(m2m.orcamento_do_ciclo_s(5), 290)
        self.assertEqual(m2m.orcamento_do_ciclo_s(15), 890)
        self.assertEqual(m2m.orcamento_do_ciclo_s(10_000), 20 * 60)             # teto
        self.assertEqual(m2m.orcamento_do_ciclo_s(None), 50)                    # sem tique informado: o de 1 min

    def test_a_trava_de_execucao_acompanha_o_tique(self):
        self.assertEqual(m2m.trava_expira(1), timedelta(minutes=3))
        self.assertEqual(m2m.trava_expira(5), timedelta(minutes=15))
        self.assertEqual(m2m.trava_expira(15), timedelta(minutes=30))           # teto
        self.assertEqual(m2m.trava_expira(None), timedelta(minutes=3))

    def test_execucao_travada_ha_mais_de_tres_tiques_nao_bloqueia_a_proxima(self):
        self.chip(1)
        M2MIntegracao.objects.filter(pk=1).update(executando_desde=timezone.now() - timedelta(minutes=4))
        resumo, sessao = self.ciclo(roteador(), tique_minutos=1)                # 4 min > 3 tiques de 1 min: morreu no meio
        self.assertTrue(resumo.executou)
        self.assertEqual(resumo.consultados, 1)

    def test_execucao_em_andamento_dentro_do_prazo_bloqueia_a_duplicada(self):
        self.chip(1)
        M2MIntegracao.objects.filter(pk=1).update(executando_desde=timezone.now() - timedelta(seconds=40))
        resumo, sessao = self.ciclo(roteador(), tique_minutos=1)
        self.assertFalse(resumo.executou)
        self.assertEqual(resumo.motivo_pulo, "em_andamento")
        self.assertEqual(sessao.chamadas, [])                                   # nenhuma requisição duplicada
        self.assertEqual(M2MExecucao.objects.count(), 0)                        # ciclo que pulou não gera registro


class RegistroDaExecucaoTests(CicloBase):
    def test_cada_ciclo_deixa_um_registro_com_os_numeros_e_as_requisicoes(self):
        for n in range(1, 4):
            self.chip(n)
        resumo, sessao = self.ciclo(roteador(), tique_minutos=1)
        execucao = M2MExecucao.objects.get()
        self.assertEqual(execucao.pk, resumo.execucao_id)
        self.assertEqual(execucao.estado, M2MExecucao.Estado.CONCLUIDA)
        self.assertEqual((execucao.vencidos, execucao.consultados, execucao.sucessos, execucao.falhas), (3, 3, 3, 0))
        self.assertEqual(execucao.chamadas, 1)                                  # a lista da conta: UMA requisição para os 3 chips
        self.assertEqual(execucao.pendentes, 0)
        self.assertIsNotNone(execucao.concluida_em)
        self.assertEqual(execucao.mudancas["estado_sessao"], 3)                 # os 3 saíram de "não consultado"

    def test_execucao_com_falha_de_chip_fica_marcada_e_o_ultimo_dado_valido_continua(self):
        chip = self.chip(1)
        self.ciclo(roteador(), tique_minutos=1)
        chip.refresh_from_db()
        valida = chip.sessao_consulta_id
        self.assertEqual(chip.sessao_estado, "ativa")
        M2MChip.objects.filter(pk=chip.pk).update(proxima_consulta_em=timezone.now() - timedelta(seconds=1))
        resumo, _ = self.ciclo(roteador(status=500), tique_minutos=1)
        chip.refresh_from_db()
        ultima = M2MExecucao.objects.order_by("-pk").first()
        self.assertEqual(ultima.estado, M2MExecucao.Estado.ERRO)               # todas as consultas falharam
        self.assertEqual((chip.sessao_consulta_id, chip.sessao_estado), (valida, "ativa"))   # o dado bom não foi sobrescrito
        self.assertEqual(chip.falhas_consecutivas, 1)

    def test_a_poda_apaga_so_o_registro_de_execucoes_antigas(self):
        self.chip(1)
        self.ciclo(roteador())
        antiga = M2MExecucao.objects.create(iniciada_em=timezone.now() - timedelta(days=8))
        self.assertEqual(m2m.podar_execucoes(), 1)
        self.assertFalse(M2MExecucao.objects.filter(pk=antiga.pk).exists())
        self.assertEqual(M2MExecucao.objects.count(), 1)

    def test_falha_ao_gravar_o_registro_nao_derruba_o_ciclo(self):
        chip = self.chip(1)
        with mock.patch.object(M2MExecucao.objects, "filter", side_effect=RuntimeError("banco")):
            m2m._fechar_execucao(M2MExecucao(pk=1), m2m.ResumoCiclo(), 0.0)      # só não levanta
        self.assertTrue(chip.pk)


class EstadoDaSincronizacaoTests(CicloBase):
    def estado(self):
        return m2m.estado_sincronizacao()

    def test_sem_tarefa_agendada_diz_que_nada_atualiza_sozinho(self):
        self.chip(1)
        e = self.estado()
        self.assertEqual(e["estado"], "desatualizada")
        self.assertIn("nunca rodou", e["motivo"])
        self.assertTrue(e["tarefa_parada"])

    def test_depois_de_um_ciclo_bem_sucedido_esta_atualizada(self):
        self.chip(1)
        self.ciclo(roteador(), tique_minutos=1)
        e = self.estado()
        self.assertEqual((e["estado"], e["tom"]), ("atualizada", "ok"))
        self.assertIsNotNone(e["ultima_sincronizacao"])
        self.assertEqual(e["execucao"].consultados, 1)
        self.assertEqual(e["tique_minutos"], 1)

    def test_distingue_a_hora_do_ciclo_da_hora_da_ultima_resposta_valida(self):
        chip = self.chip(1)
        self.ciclo(roteador(), tique_minutos=1)
        antiga = timezone.now() - timedelta(hours=3)
        M2MChip.objects.filter(pk=chip.pk).update(ultima_valida_em=antiga)
        e = self.estado()
        self.assertEqual(e["ultima_resposta_valida"], antiga)
        self.assertGreater(e["ultima_tentativa"], antiga + timedelta(hours=2))  # o ciclo rodou agora; a API respondeu há 3 h

    def test_ciclo_com_erro_aparece_como_falha(self):
        self.chip(1)
        self.ciclo(roteador(status=500), tique_minutos=1)
        e = self.estado()
        self.assertEqual((e["estado"], e["tom"]), ("falha", "erro"))
        self.assertIn("falharam", e["motivo"])

    def test_credencial_recusada_e_integracao_indisponivel(self):
        self.chip(1)
        M2MIntegracao.objects.filter(pk=1).update(
            falha_autenticacao_em=timezone.now(), autenticada_em=None, ultima_execucao=timezone.now(),
        )
        self.assertEqual(self.estado()["estado"], "indisponivel")

    def test_execucao_em_andamento_aparece_como_em_andamento(self):
        self.chip(1)
        M2MIntegracao.objects.filter(pk=1).update(
            ultima_execucao=timezone.now(), tique_minutos=1, executando_desde=timezone.now() - timedelta(seconds=5),
        )
        self.assertEqual(self.estado()["estado"], "em_andamento")

    def test_fila_atrasada_aparece_como_aguardando_a_proxima_atualizacao(self):
        chip = self.chip(1)
        self.ciclo(roteador(), tique_minutos=1)
        M2MChip.objects.filter(pk=chip.pk).update(proxima_consulta_em=timezone.now() - timedelta(minutes=5))
        e = self.estado()
        self.assertEqual(e["estado"], "aguardando")
        self.assertEqual(e["vencidos"], 1)

    def test_o_plano_informa_desejado_e_efetivo(self):
        self.chip(1)
        self.assertIn("chip(s) com sessão ativa", self.estado()["plano"]["texto"])


class SemTokenNoNavegadorTests(CicloBase):
    def test_o_estado_da_sincronizacao_nunca_traz_a_credencial(self):
        self.chip(1)
        self.ciclo(roteador(), tique_minutos=1)
        e = self.estado_texto()
        self.assertNotIn("Bearer", e)

    def estado_texto(self):
        e = m2m.estado_sincronizacao()
        return repr({k: v for k, v in e.items() if k not in ("integracao", "execucao")}) + repr(e["integracao"])


class VariosCiclosNoTiqueTests(CicloBase):
    """A tarefa de 15 em 15 min (a que já está registrada) roda um ciclo por minuto até o fim do tique."""

    def rodar(self, respostas, tique, **kw):
        sessao = Sessao(respostas)
        cliente = tim.ClienteTIM(sessao)
        dormidas = []
        relogio = {"t": 1000.0}

        def dormir(s):
            dormidas.append(s)
            relogio["t"] += s

        resumos = m2m.executar_varios_ciclos(
            tique_minutos=tique, cliente=cliente, relogio=lambda: relogio["t"], dormir=dormir, **kw,
        )
        return resumos, dormidas, sessao

    def test_a_cadencia_e_o_intervalo_desejado_limitado_ao_tique(self):
        cfg = M2MIntegracao.get()
        self.assertEqual(m2m.cadencia_dos_ciclos(cfg, 15), 1)                       # pede 1 min, a tarefa é de 15
        M2MIntegracao.objects.filter(pk=1).update(intervalo_minutos=5)
        self.assertEqual(m2m.cadencia_dos_ciclos(M2MIntegracao.get(), 15), 5)
        M2MIntegracao.objects.filter(pk=1).update(intervalo_minutos=30)
        self.assertEqual(m2m.cadencia_dos_ciclos(M2MIntegracao.get(), 15), 15)      # nunca mais espaçado que o tique
        M2MIntegracao.objects.filter(pk=1).update(intervalo_minutos=1)
        self.assertEqual(m2m.cadencia_dos_ciclos(M2MIntegracao.get(), 1), 1)

    def test_tarefa_de_um_minuto_roda_um_ciclo_so_sem_laco(self):
        self.chip(1)
        resumos, dormidas, _ = self.rodar(roteador(), tique=1)
        self.assertEqual(len(resumos), 1)
        self.assertEqual(dormidas, [])

    def test_tarefa_de_quinze_minutos_repete_um_ciclo_por_minuto_ate_o_fim_do_tique(self):
        self.chip(1)
        resumos, dormidas, _ = self.rodar(roteador(), tique=15)
        # 15 min = 900 s; termina 20 s antes do próximo disparo e só começa ciclo que cabe inteiro
        self.assertEqual(len(resumos), 14)
        self.assertTrue(all(r.executou for r in resumos))
        self.assertTrue(all(abs(d - 60) < 1 for d in dormidas))                      # espera o minuto inteiro entre os inícios
        self.assertEqual(len(dormidas), len(resumos) - 1)

    def test_credencial_recusada_para_o_laco_sem_insistir(self):
        self.chip(1)
        M2MIntegracao.objects.filter(pk=1).update(
            falha_autenticacao_em=timezone.now(), autenticada_em=timezone.now() - timedelta(days=1),
        )
        resumos, dormidas, sessao = self.rodar(roteador(), tique=15)
        self.assertEqual(len(resumos), 1)
        self.assertEqual(resumos[0].motivo_pulo, "credencial_recusada")
        self.assertEqual(sessao.chamadas, [])
        self.assertEqual(dormidas, [])

    def test_sem_token_para_o_laco(self):
        self.chip(1)
        from django.test import override_settings
        with override_settings(TIM_M2M_TOKEN=""):
            resumos, dormidas, _ = self.rodar(roteador(), tique=15)
        self.assertEqual((len(resumos), resumos[0].motivo_pulo), (1, "sem_configuracao"))

    def test_o_tique_gravado_e_o_da_tarefa_e_nao_o_da_cadencia(self):
        self.chip(1)
        self.rodar(roteador(), tique=15)
        self.assertEqual(M2MIntegracao.get().tique_minutos, 15)

    def test_o_painel_nao_chama_de_parada_a_tarefa_que_roda_um_ciclo_por_minuto(self):
        self.chip(1)
        self.ciclo(roteador(), tique_minutos=15)
        M2MIntegracao.objects.filter(pk=1).update(ultima_execucao=timezone.now() - timedelta(seconds=100))
        self.assertFalse(m2m.estado_sincronizacao()["tarefa_parada"])               # 100 s: cabe no minuto + a pausa entre ciclos
        M2MIntegracao.objects.filter(pk=1).update(ultima_execucao=timezone.now() - timedelta(minutes=4))
        self.assertTrue(m2m.estado_sincronizacao()["tarefa_parada"])                # 4 min sem ciclo: parou de verdade


class ComandoManualNaoPrendeOTerminalTests(CicloBase):
    """Sem `--tique` (terminal, `agendar_m2m executar`) o comando faz UM ciclo; só a tarefa, com `--tique`, repete."""

    def rodar(self, *args):
        import io
        from django.core.management import call_command
        saida = io.StringIO()
        with mock.patch("services.tim_m2m_service.requests.Session", return_value=Sessao(roteador())):
            call_command("monitorar_m2m", *args, stdout=saida)
        return saida.getvalue()

    def test_execucao_manual_faz_um_ciclo_so_mesmo_com_o_intervalo_de_um_minuto_e_a_tarefa_de_quinze(self):
        self.chip(1)
        M2MIntegracao.objects.filter(pk=1).update(intervalo_minutos=1, tique_minutos=15)
        self.rodar()
        self.assertEqual(M2MExecucao.objects.count(), 1)

    def test_com_tique_a_tarefa_de_um_minuto_tambem_faz_um_ciclo_so(self):
        self.chip(1)
        self.rodar("--tique", "1")
        self.assertEqual(M2MExecucao.objects.count(), 1)
