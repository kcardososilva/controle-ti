"""
Situação do chip (15/11/2026): reservado, pausado e com defeito, o "desde quando", a trilha de quem mudou e as sugestões.

O que estes testes protegem — quase tudo aqui falha em SILÊNCIO (a tela abre 200 e o dado está errado):

  • UMA PORTA PARA MUDAR (`mudar_situacao`) e um só escritor de `situacao_uso`: cada mudança grava movimentação
    imutável + evento com o de/para + `situacao_uso_em`, e não liga nem desliga o monitoramento.
  • NADA SE MUDA SOZINHO. As sugestões (`chip_situacao_service`) só LEEM: chip, item de TI e trilha ficam idênticos
    antes e depois de pedir as sugestões. O status do item do estoque de TI nunca é escrito.
  • O GRÁFICO DE UTILIZAÇÃO FECHA. Cada chip em UM grupo só e a soma dá o total — uma situação nova que caísse fora de
    todos os grupos faria o número sumir sem erro nenhum.
  • AS TROCAS (substituir, desvincular) mandam o chip que sai para a situação certa.
"""

import importlib
from datetime import timedelta

from django.apps import apps as django_apps
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from ProjetoEstoque.models import (
    Categoria,
    FrotaMovimentacao,
    Item,
    ItemStatusHistorico,
    M2MChip,
    M2MEvento,
    M2MOcorrencia,
    MovimentacaoItem,
    Subtipo,
)
from ProjetoEstoque.tests.test_frota import FrotaBase
from ProjetoEstoque.tests.test_m2m import iccid_valido
from ProjetoEstoque.tests.test_m2m_telas import TelasBase
from services import chip_situacao_service as sugestoes
from services import frota_vinculo_service as vinculos
from services import m2m_analise_service as analise
from services import m2m_monitoramento_service as m2m


def _tablet():
    categoria = Categoria.objects.create(nome="Equipamento de teste")
    return Subtipo.objects.create(nome="Tablet de teste", alocado="sim", categoria=categoria, aceita_chip=True)


def _equipamento(subtipo, nome="Tablet A", status="ativo"):
    return Item.objects.create(nome=nome, numero_serie=f"SN-{nome}", quantidade=1, status=status, subtipo=subtipo)


# ═════════════════════════════════════════════════════════════
# mudar_situacao
# ═════════════════════════════════════════════════════════════

class MudarSituacaoTests(FrotaBase):
    def test_cada_mudanca_grava_movimentacao_evento_com_de_para_e_a_data(self):
        chip = self.chip(1)
        antes = M2MChip.objects.get(pk=chip.pk).situacao_uso_em
        self.assertIsNotNone(antes, "o chip cadastrado já nasce com o 'desde quando'")

        resultado = vinculos.mudar_situacao(chip, "defeito", self.usuario, "SIM não lê no aparelho")
        chip.refresh_from_db()

        self.assertEqual(chip.situacao_uso, "defeito")
        self.assertGreaterEqual(chip.situacao_uso_em, antes)
        mov = resultado.movimentacao
        self.assertEqual((mov.tipo, mov.motivo, mov.usuario), ("situacao", "SIM não lê no aparelho", self.usuario))
        self.assertIn("De disponível para com defeito", mov.observacoes)
        evento = M2MEvento.objects.get(chip=chip, tipo="situacao_mudou")
        self.assertEqual(evento.usuario, self.usuario)
        self.assertEqual(
            (evento.dados["situacao_anterior"], evento.dados["situacao_uso"], evento.dados["motivo"]),
            ("disponivel", "defeito", "SIM não lê no aparelho"),
        )

    def test_percorre_todas_as_situacoes_livres_em_qualquer_ordem(self):
        chip = self.chip(1)
        for nova in ("reservado", "pausado", "defeito", "manutencao", "reservado", "manutencao"):
            vinculos.mudar_situacao(chip, nova, self.usuario, f"para {nova}")
            chip.refresh_from_db()
            self.assertEqual(chip.situacao_uso, nova)
        self.assertEqual(FrotaMovimentacao.objects.filter(chip=chip, tipo="situacao").count(), 6)

    def test_motivo_e_obrigatorio_e_a_situacao_nao_muda_sem_ele(self):
        chip = self.chip(1)
        with self.assertRaises(ValidationError) as cm:
            vinculos.mudar_situacao(chip, "defeito", self.usuario, "   ")
        self.assertIn("motivo", cm.exception.message_dict)
        chip.refresh_from_db()
        self.assertEqual(chip.situacao_uso, "disponivel")
        self.assertFalse(FrotaMovimentacao.objects.filter(chip=chip, tipo="situacao").exists())

    def test_recusa_a_mesma_situacao_uma_situacao_inexistente_e_vinculado_como_destino(self):
        chip = self.chip(1)
        vinculos.mudar_situacao(chip, "pausado", self.usuario, "x")
        with self.assertRaises(ValidationError) as cm:
            vinculos.mudar_situacao(chip, "pausado", self.usuario, "x")
        self.assertIn("já está pausado", cm.exception.messages[0])
        with self.assertRaises(ValidationError):
            vinculos.mudar_situacao(chip, "voando", self.usuario, "x")
        with self.assertRaises(ValidationError) as cm:
            vinculos.mudar_situacao(chip, "vinculado", self.usuario, "x")
        self.assertIn("Vincular", cm.exception.message_dict["situacao"][0])

    def test_chip_vinculado_nao_muda_por_aqui_e_a_mensagem_aponta_o_caminho(self):
        chip = self.chip(1)
        self.vincular(chip)
        with self.assertRaises(ValidationError) as cm:
            vinculos.mudar_situacao(chip, "defeito", self.usuario, "x")
        self.assertIn("Desvincule", cm.exception.messages[0])
        chip.refresh_from_db()
        self.assertEqual(chip.situacao_uso, "vinculado")

    def test_chip_inativo_volta_antes_a_disponibilidade(self):
        chip = self.chip(1)
        vinculos.inativar(chip, self.usuario, "linha cancelada")
        with self.assertRaises(ValidationError) as cm:
            vinculos.mudar_situacao(chip, "defeito", self.usuario, "x")
        self.assertIn("inativo", cm.exception.messages[0])

    def test_para_disponivel_e_para_inativo_usam_as_operacoes_que_ja_existiam(self):
        chip = self.chip(1)
        vinculos.mudar_situacao(chip, "defeito", self.usuario, "quebrou")
        volta = vinculos.mudar_situacao(chip, "disponivel", self.usuario, "testado")
        self.assertEqual(volta.movimentacao.tipo, "retorno")
        evento = M2MEvento.objects.filter(chip=chip, tipo="retorno_disponibilidade").latest("pk")
        self.assertEqual(evento.dados["situacao_anterior_rotulo"], "Com defeito")
        self.assertIn("estava com defeito", m2m.mudancas_de_situacao(chip)[0]["detalhe"])

        fim = vinculos.mudar_situacao(chip, "inativo", self.usuario, "descartado")
        self.assertEqual(fim.movimentacao.tipo, "inativacao")
        chip.refresh_from_db()
        self.assertEqual((chip.situacao_uso, chip.monitorado), ("inativo", False))

    def test_mudar_entre_situacoes_livres_nao_mexe_no_monitoramento(self):
        chip = self.chip(1)
        vinculos.mudar_situacao(chip, "defeito", self.usuario, "x")
        chip.refresh_from_db()
        self.assertTrue(chip.monitorado)
        self.assertIsNone(chip.desativado_em)
        self.assertFalse(M2MEvento.objects.filter(chip=chip, tipo__in=("desativado", "reativado")).exists())

    def test_transferir_nao_zera_o_desde_quando(self):
        chip = self.chip(1)
        self.vincular(chip, self.veiculo(1))
        chip.refresh_from_db()
        desde = chip.situacao_uso_em
        vinculo = chip.vinculos.get(fim_em__isnull=True)
        vinculos.transferir(vinculo.pk, self.veiculo(2), self.usuario, "mudou de máquina")
        chip.refresh_from_db()
        self.assertEqual(chip.situacao_uso_em, desde, "a situação é a mesma (vinculado): o relógio não anda")

    def test_o_historico_do_chip_mostra_quem_mudou(self):
        chip = self.chip(1)
        vinculos.mudar_situacao(chip, "pausado", self.usuario, "linha suspensa")
        item = m2m.linha_do_tempo(chip, ["evento"])[0]
        self.assertEqual(item["tipo"], "situacao_mudou")
        self.assertIn("De disponível para pausado.", item["detalhe"])
        self.assertIn("Motivo: linha suspensa", item["detalhe"])
        self.assertIn(f"Por {self.usuario.username}", item["detalhe"])

    def test_nada_do_estoque_de_ti_e_escrito(self):
        item = _equipamento(_tablet())
        chip = self.chip(1)
        M2MChip.objects.filter(pk=chip.pk).update(item_ti=item)
        antes = (Item.objects.filter(pk=item.pk).values().get(), ItemStatusHistorico.objects.count(), MovimentacaoItem.objects.count())
        vinculos.mudar_situacao(chip, "defeito", self.usuario, "x")
        vinculos.mudar_situacao(chip, "disponivel", self.usuario, "y")
        depois = (Item.objects.filter(pk=item.pk).values().get(), ItemStatusHistorico.objects.count(), MovimentacaoItem.objects.count())
        self.assertEqual(antes, depois)


# ═════════════════════════════════════════════════════════════
# Vínculo e trocas com as novas situações
# ═════════════════════════════════════════════════════════════

class VinculoComAsNovasSituacoesTests(FrotaBase):
    def test_chip_reservado_pode_ser_vinculado_o_parado_nao(self):
        reservado = self.chip(1)
        vinculos.mudar_situacao(reservado, "reservado", self.usuario, "para o trator novo")
        vinculos.vincular(reservado, self.veiculo(1), self.usuario, "chegou o trator")
        reservado.refresh_from_db()
        self.assertEqual(reservado.situacao_uso, "vinculado")

        for n, situacao in ((2, "defeito"), (3, "pausado"), (4, "manutencao")):
            chip = self.chip(n)
            vinculos.mudar_situacao(chip, situacao, self.usuario, "x")
            with self.assertRaises(ValidationError) as cm:
                vinculos.vincular(chip, self.veiculo(n), self.usuario, "x")
            self.assertIn("Devolva-o à disponibilidade", cm.exception.messages[0])

    def test_desvincular_manda_o_chip_para_qualquer_situacao_livre(self):
        for n, destino in enumerate(("disponivel", "reservado", "pausado", "defeito", "manutencao"), start=1):
            chip = self.chip(n)
            self.vincular(chip, self.veiculo(n))
            vinculo = chip.vinculos.get(fim_em__isnull=True)
            vinculos.desvincular(vinculo.pk, self.usuario, "teste", destino)
            chip.refresh_from_db()
            self.assertEqual(chip.situacao_uso, destino)

    def test_desvincular_recusa_inativo_e_lixo_como_destino(self):
        chip = self.chip(1)
        self.vincular(chip)
        vinculo = chip.vinculos.get(fim_em__isnull=True)
        for destino in ("inativo", "vinculado", "xyz"):
            with self.assertRaises(ValidationError) as cm:
                vinculos.desvincular(vinculo.pk, self.usuario, "x", destino)
            self.assertIn("destino", cm.exception.message_dict)
        chip.refresh_from_db()
        self.assertEqual(chip.situacao_uso, "vinculado")

    def test_substituir_deixa_o_chip_que_saiu_com_defeito_e_guarda_de_onde_veio(self):
        velho, novo = self.chip(1), self.chip(2)
        self.vincular(velho, self.veiculo(1))
        vinculo = velho.vinculos.get(fim_em__isnull=True)
        vinculos.substituir(vinculo.pk, novo, self.usuario, "o SIM parou de ler", destino_antigo="defeito")
        velho.refresh_from_db()
        novo.refresh_from_db()
        self.assertEqual((velho.situacao_uso, novo.situacao_uso), ("defeito", "vinculado"))
        evento = M2MEvento.objects.get(chip=velho, tipo="substituido")
        self.assertEqual((evento.dados["situacao_anterior"], evento.dados["situacao_uso"]), ("vinculado", "defeito"))

    def test_devolver_a_disponibilidade_vale_para_chip_parado_e_reservado(self):
        for n, situacao in enumerate(("defeito", "pausado", "reservado", "manutencao"), start=1):
            chip = self.chip(n)
            vinculos.mudar_situacao(chip, situacao, self.usuario, "x")
            vinculos.retornar_disponibilidade(chip, self.usuario, "ok")
            chip.refresh_from_db()
            self.assertEqual(chip.situacao_uso, "disponivel")

    def test_o_desde_quando_do_vinculo_e_da_desvinculacao(self):
        chip = self.chip(1)
        self.vincular(chip)
        vinculo = chip.vinculos.get(fim_em__isnull=True)
        antes = timezone.now()
        vinculos.desvincular(vinculo.pk, self.usuario, "x", "manutencao")
        chip.refresh_from_db()
        self.assertGreaterEqual(chip.situacao_uso_em, antes)


# ═════════════════════════════════════════════════════════════
# O gráfico de utilização fecha com as novas situações
# ═════════════════════════════════════════════════════════════

class GruposDeUsoTests(FrotaBase):
    def test_cada_chip_cai_em_um_grupo_so_e_a_soma_fecha_com_o_total(self):
        self.vincular(self.chip(1), self.veiculo(1))                       # em uso
        self.chip(2)                                                        # não utilizado
        for n, situacao in ((3, "reservado"), (4, "pausado"), (5, "defeito"), (6, "manutencao")):
            vinculos.mudar_situacao(self.chip(n), situacao, self.usuario, "x")
        vinculos.inativar(self.chip(7), self.usuario, "x")
        util = analise.utilizacao()
        n = {g["chave"]: g["n"] for g in util["principais"] + util["fora"]}
        self.assertEqual(n, {
            "em_uso": 1, "nao_utilizado": 1, "reservado": 1, "pausado": 1, "defeito": 1, "manutencao": 1, "inativo": 1,
        })
        self.assertEqual(sum(g["n"] for g in util["principais"] + util["fora"]), util["total"])

    def test_o_filtro_da_lista_por_situacao_aceita_as_novas(self):
        vinculos.mudar_situacao(self.chip(1), "defeito", self.usuario, "x")
        self.chip(2)
        achados = m2m.listar_chips({"situacao_uso": "defeito"})
        self.assertEqual([c.nome for c in achados], ["Chip 1"])
        self.assertEqual([c.nome for c in m2m.listar_chips({"uso": "defeito"})], ["Chip 1"])


# ═════════════════════════════════════════════════════════════
# Sugestões: só leitura
# ═════════════════════════════════════════════════════════════

class SugestoesTests(FrotaBase):
    def codigos(self, chip, **kw):
        chip.refresh_from_db()
        return [s.codigo for s in sugestoes.sugestoes_para(chip, **kw)]

    def test_equipamento_de_ti_parado_sugere_desvincular(self):
        tablet = _equipamento(_tablet())
        chip = self.chip(1)
        vinculos.vincular(chip, tablet, self.usuario, "no tablet")
        self.assertEqual(self.codigos(chip), [])
        Item.objects.filter(pk=tablet.pk).update(status="defeito")
        achada = sugestoes.sugestoes_para(chip)[0]
        self.assertEqual((achada.codigo, achada.acao), ("equipamento_parado", sugestoes.ACAO_DESVINCULAR))
        self.assertIn("Desvincular", achada.rotulo_da_acao)

    def test_item_do_estoque_em_outra_situacao_sugere_aplicar_no_agro(self):
        item = _equipamento(_tablet(), nome="Chip TIM", status="defeito")
        chip = self.chip(1)
        M2MChip.objects.filter(pk=chip.pk).update(item_ti=item)
        achada = sugestoes.sugestoes_para(M2MChip.objects.get(pk=chip.pk))[0]
        self.assertEqual((achada.codigo, achada.acao, achada.situacao), ("diverge_do_estoque", "mudar", "defeito"))
        # item "ativo" no TI não tem equivalente: nada a sugerir
        Item.objects.filter(pk=item.pk).update(status="ativo")
        self.assertEqual(self.codigos(chip), [])
        # item parado e chip já na mesma situação: nada a sugerir
        Item.objects.filter(pk=item.pk).update(status="defeito")
        vinculos.mudar_situacao(chip, "defeito", self.usuario, "x")
        self.assertEqual(self.codigos(chip), [])

    def test_chip_inativo_ou_vinculado_ou_item_excluido_nao_recebem_a_sugestao_do_estoque(self):
        subtipo = _tablet()
        item = _equipamento(subtipo, nome="Chip TIM", status="backup")
        chip = self.chip(1)
        M2MChip.objects.filter(pk=chip.pk).update(item_ti=item)
        vinculos.inativar(chip, self.usuario, "linha cancelada")
        self.assertEqual(self.codigos(chip), [], "inativar é decisão deliberada; o item velho no TI não a desfaz")

        outro = self.chip(2)
        outro_item = _equipamento(subtipo, nome="Chip B", status="defeito")
        M2MChip.objects.filter(pk=outro.pk).update(item_ti=outro_item)
        self.vincular(outro, self.veiculo(1))
        self.assertEqual(self.codigos(outro), [], "chip vinculado sai pelo desvincular/substituir, não por esta sugestão")

        terceiro = self.chip(3)
        excluido = _equipamento(subtipo, nome="Chip C", status="defeito")
        M2MChip.objects.filter(pk=terceiro.pk).update(item_ti=excluido)
        Item.objects.filter(pk=excluido.pk).update(excluido=True)
        self.assertEqual(self.codigos(terceiro), [], "item excluído tem a divergência própria, não esta sugestão")

    def test_sem_sessao_so_vale_para_vinculado_e_para_quem_ve_a_api(self):
        chip = self.chip(1)
        self.vincular(chip, self.veiculo(1))
        M2MOcorrencia.objects.create(chip=chip, tipo="sem_sessao", nivel="atencao", estado="aberta")
        self.assertEqual(self.codigos(chip, com_api=True), ["sem_sessao_vinculado"])
        self.assertEqual(self.codigos(chip, com_api=False), [], "dado da operadora não vaza para quem não vê API")
        M2MOcorrencia.objects.filter(chip=chip).update(estado="resolvida")
        self.assertEqual(self.codigos(chip, com_api=True), [])

    def test_parado_ha_muito_respeita_o_prazo(self):
        chip = self.chip(1)
        vinculos.mudar_situacao(chip, "manutencao", self.usuario, "revisão")
        self.assertEqual(self.codigos(chip), [])
        agora = timezone.now()
        M2MChip.objects.filter(pk=chip.pk).update(situacao_uso_em=agora - timedelta(days=sugestoes.DIAS_PARA_REAVALIAR + 3))
        achada = sugestoes.sugestoes_para(M2MChip.objects.get(pk=chip.pk))[0]
        self.assertEqual(achada.codigo, "parado_ha_muito")
        self.assertIn("há 33 dias", achada.titulo)
        # disponível e vinculado nunca "estão parados": é o estado normal deles
        M2MChip.objects.filter(pk=chip.pk).update(situacao_uso="disponivel")
        self.assertEqual(self.codigos(chip), [])

    def test_pedir_as_sugestoes_nao_escreve_nada(self):
        tablet = _equipamento(_tablet())
        chip = self.chip(1)
        vinculos.vincular(chip, tablet, self.usuario, "no tablet")
        Item.objects.filter(pk=tablet.pk).update(status="defeito")
        M2MOcorrencia.objects.create(chip=chip, tipo="sem_sessao", nivel="atencao", estado="aberta")
        M2MChip.objects.filter(pk=chip.pk).update(situacao_uso_em=timezone.now() - timedelta(days=90))
        antes = (
            M2MChip.objects.filter(pk=chip.pk).values().get(), Item.objects.filter(pk=tablet.pk).values().get(),
            M2MEvento.objects.count(), FrotaMovimentacao.objects.count(), M2MOcorrencia.objects.count(),
        )
        self.assertTrue(sugestoes.sugestoes_para(M2MChip.objects.get(pk=chip.pk), com_api=True))
        depois = (
            M2MChip.objects.filter(pk=chip.pk).values().get(), Item.objects.filter(pk=tablet.pk).values().get(),
            M2MEvento.objects.count(), FrotaMovimentacao.objects.count(), M2MOcorrencia.objects.count(),
        )
        self.assertEqual(antes, depois)

    def test_dias_na_situacao(self):
        chip = self.chip(1)
        self.assertEqual(sugestoes.dias_na_situacao(chip), 0)
        M2MChip.objects.filter(pk=chip.pk).update(situacao_uso_em=timezone.now() - timedelta(days=4, hours=2))
        self.assertEqual(sugestoes.dias_na_situacao(M2MChip.objects.get(pk=chip.pk)), 4)
        M2MChip.objects.filter(pk=chip.pk).update(situacao_uso_em=None)
        self.assertIsNone(sugestoes.dias_na_situacao(M2MChip.objects.get(pk=chip.pk)))


# ═════════════════════════════════════════════════════════════
# Telas e endpoint
# ═════════════════════════════════════════════════════════════

class TelaDaSituacaoTests(TelasBase):
    def post(self, cliente, chip, **dados):
        return cliente.post(
            reverse("frota_situacao", args=[chip.pk]), dados, HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

    def test_endpoint_muda_a_situacao_e_responde_em_json(self):
        chip = self.chip(1)
        r = self.post(self.client, chip, situacao="defeito", motivo="SIM não lê")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertIn("agora está com defeito", r.json()["mensagem"])
        chip.refresh_from_db()
        self.assertEqual(chip.situacao_uso, "defeito")

    def test_endpoint_devolve_o_erro_do_service_dentro_do_dialogo(self):
        chip = self.chip(1)
        r = self.post(self.client, chip, situacao="defeito", motivo="")
        self.assertEqual(r.status_code, 400)
        self.assertIn("motivo", r.json()["erros"])
        chip.refresh_from_db()
        self.assertEqual(chip.situacao_uso, "disponivel")

    def test_so_quem_pode_transferir_chips_muda_a_situacao(self):
        chip = self.chip(1)
        so_ve = self.cliente_de(self.usuario_com("so_ve", "ver"))
        self.assertEqual(self.post(so_ve, chip, situacao="defeito", motivo="x").status_code, 403)
        pode = self.cliente_de(self.usuario_com("pode", "transferir_chips"))
        self.assertEqual(self.post(pode, chip, situacao="defeito", motivo="x").status_code, 200)

    def test_get_nao_muda_nada_e_anonimo_vai_para_o_login(self):
        chip = self.chip(1)
        self.assertEqual(self.client.get(reverse("frota_situacao", args=[chip.pk])).status_code, 405)
        anonimo = self.client_class()
        r = anonimo.post(reverse("frota_situacao", args=[chip.pk]), {"situacao": "defeito", "motivo": "x"})
        self.assertEqual(r.status_code, 302)
        chip.refresh_from_db()
        self.assertEqual(chip.situacao_uso, "disponivel")

    def test_o_detalhe_mostra_a_situacao_o_desde_quando_as_sugestoes_e_a_trilha(self):
        chip = self.chip(1)
        vinculos.mudar_situacao(chip, "manutencao", self.usuario, "revisão geral")
        M2MChip.objects.filter(pk=chip.pk).update(situacao_uso_em=timezone.now() - timedelta(days=45))
        html = self.client.get(reverse("m2m_chip_detalhe", args=[chip.pk])).content.decode()
        self.assertIn("Situação e acompanhamento", html)
        self.assertIn("há 45 dias", html)
        self.assertIn("Em manutenção há 45 dias", html)                      # a sugestão "parado há muito"
        self.assertIn("São sugestões: o sistema não muda a situação do chip sozinho.", html)
        self.assertIn("Por onde o chip passou", html)
        self.assertIn("revisão geral", html)
        self.assertIn(reverse("frota_situacao", args=[chip.pk]), html)
        self.assertIn('data-status="manutencao"', html)

    def test_o_dialogo_e_o_menu_oferecem_as_situacoes_novas(self):
        chip = self.chip(1)
        html = self.client.get(reverse("m2m_chip_detalhe", args=[chip.pk])).content.decode()
        self.assertIn('id="frota-dlg-situacao"', html)
        for valor in ("reservado", "pausado", "defeito"):
            self.assertIn(f'<option value="{valor}">', html)
        self.assertIn("Mudar situação", html)

    def test_chip_reservado_ainda_mostra_vincular_o_parado_nao(self):
        reservado, parado = self.chip(1), self.chip(2)
        vinculos.mudar_situacao(reservado, "reservado", self.usuario, "x")
        vinculos.mudar_situacao(parado, "defeito", self.usuario, "x")
        com = self.client.get(reverse("m2m_chip_detalhe", args=[reservado.pk])).content.decode()
        sem = self.client.get(reverse("m2m_chip_detalhe", args=[parado.pk])).content.decode()
        self.assertIn("Vincular a um veículo", com)
        self.assertNotIn("Vincular a um veículo", sem)
        self.assertIn("Devolver à disponibilidade", sem)

    def test_a_lista_ganha_os_segmentos_das_situacoes_que_existem(self):
        vinculos.mudar_situacao(self.chip(1), "defeito", self.usuario, "x")
        vinculos.mudar_situacao(self.chip(2), "reservado", self.usuario, "x")
        html = self.client.get(reverse("m2m_chips")).content.decode()
        self.assertIn("Com defeito", html)
        self.assertIn("Reservados", html)
        self.assertNotIn("Pausados", html, "situação sem nenhum chip não vira segmento")

    def test_quem_nao_ve_api_ve_a_situacao_mas_nao_a_sugestao_da_sessao(self):
        chip = self.chip(1)
        self.vincular(chip, self.veiculo(1))
        M2MOcorrencia.objects.create(chip=chip, tipo="sem_sessao", nivel="atencao", estado="aberta")
        so_chips = self.cliente_de(self.usuario_com("so_chips", "ver_chips", "ver_historico_frota"))
        html = so_chips.get(reverse("m2m_chip_detalhe", args=[chip.pk])).content.decode()
        self.assertIn("Situação e acompanhamento", html)
        self.assertNotIn("Vinculado e sem sessão", html)


# ═════════════════════════════════════════════════════════════
# Migration 0210
# ═════════════════════════════════════════════════════════════

class MigracaoDaSituacaoTests(FrotaBase):
    def test_o_preenchimento_usa_a_ultima_movimentacao_e_o_cadastro_como_reserva(self):
        migracao = importlib.import_module("ProjetoEstoque.migrations.0210_chip_situacao_acompanhamento")
        movido = self.chip(1)
        self.vincular(movido)
        ultima = movido.movimentacoes.order_by("-em", "-pk").first().em
        sem_movimento = M2MChip.objects.create(nome="Sem movimento", iccid=iccid_valido(50))
        M2MChip.objects.filter(pk__in=[movido.pk, sem_movimento.pk]).update(situacao_uso_em=None)

        migracao.preencher_situacao_uso_em(django_apps, None)

        movido.refresh_from_db()
        sem_movimento.refresh_from_db()
        self.assertEqual(movido.situacao_uso_em, ultima)
        self.assertEqual(sem_movimento.situacao_uso_em, sem_movimento.created_at)

    def test_o_preenchimento_so_toca_o_que_esta_vazio(self):
        migracao = importlib.import_module("ProjetoEstoque.migrations.0210_chip_situacao_acompanhamento")
        chip = self.chip(1)
        marca = timezone.now() - timedelta(days=9)
        M2MChip.objects.filter(pk=chip.pk).update(situacao_uso_em=marca)
        migracao.preencher_situacao_uso_em(django_apps, None)
        chip.refresh_from_db()
        self.assertEqual(chip.situacao_uso_em, marca)
