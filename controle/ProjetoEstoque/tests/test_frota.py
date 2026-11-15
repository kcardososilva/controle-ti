"""
Frotas e Conectividade — backend (Fase D): veículos, vínculo chip-veículo, movimentações,
alertas novos, separação do estoque de TI, permissões e cadastros de apoio.

Reaproveita a base do `test_m2m.py` (sessão HTTP falsa, `time.sleep` substituído, token no
ambiente): NENHUM teste toca a rede.

O que estes testes protegem — quase tudo aqui falha em SILÊNCIO (a tela abre 200 e o dado
está errado):

  • UM CHIP, UM VÍNCULO ATIVO — a garantia é a restrição do banco, e o service converte o
    erro dela em mensagem; operações sobre vínculo existente recusam o vínculo que já foi
    encerrado por outra pessoa (cenário 8 da spec).
  • HISTÓRICO IMUTÁVEL — movimentação, evento e vínculo não se editam nem se apagam.
  • STATUS OPERACIONAL ≠ CONECTIVIDADE — nos dois sentidos.
  • SEPARAÇÃO DO ESTOQUE DE TI — só o vínculo e a movimentação do chip apontam para `Item` (a
    referência do tablet/celular, regra 62) e nenhuma operação mexe em `Item`, `MovimentacaoItem`
    ou `ItemStatusHistorico`.
  • ALERTA HONESTO — `sem_sessao` só para chip vinculado; regra desligada não gera
    ocorrência; o texto nunca diz "fora de cobertura" nem "defeito".
"""

import importlib
from datetime import timedelta
from unittest import mock

from django.apps import apps as django_apps
from django.contrib import admin as django_admin
from django.contrib.auth.models import Group, Permission, User
from django.core import mail
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models import ForeignKey, ProtectedError
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from ProjetoEstoque.models import (
    GRUPO_FROTAS,
    GRUPO_FROTAS_CONSULTA,
    PERMISSOES_FROTA_CHIP,
    PERMISSOES_FROTA_INVENTARIO,
    PERMISSOES_FROTA_VEICULO,
    REGRAS_DE_ALERTA_PADRAO,
    CanalNotificacao,
    ConectividadeOperadora,
    ConfiguracaoSistema,
    FrotaEvento,
    FrotaImportacaoChips,
    FrotaMovimentacao,
    FrotaRegraAlerta,
    FrotaTipo,
    FrotaVeiculo,
    FrotaVinculo,
    Item,
    ItemStatusHistorico,
    Localidade,
    M2MChip,
    M2MEvento,
    M2MOcorrencia,
    MovimentacaoItem,
)
from ProjetoEstoque.tests import test_m2m as base
from services import email_alertas
from services import frota_service as frota
from services import frota_vinculo_service as vinculos
from services import m2m_monitoramento_service as m2m


class FrotaBase(base.M2MBase):
    """O que `M2MBase` dá, mais fábricas de frota e um jeito de PÔR um chip num estado de conectividade."""

    def setUp(self):
        super().setUp()
        self.outro = User.objects.create(username="outro_usuario")
        self.trator = FrotaTipo.objects.get(nome="Trator")
        self.caminhao = FrotaTipo.objects.get(nome="Caminhão")
        self.pulverizador = FrotaTipo.objects.get(nome="Pulverizador")

    def veiculo_do_tipo(self, tipo, n=1, **extra):
        return self.veiculo(n, tipo=tipo.pk, **extra)

    def fixar(self, chip, estado, **extra):
        """Põe o chip direto no banco no estado de conectividade pedido (um de `frota.ROTULO_CHIP`)."""
        agora = timezone.now()
        campos = dict(
            falhas_consecutivas=0, ultima_tentativa_em=agora, ultima_valida_em=agora,
            sessao_estado="nao_consultado", monitorado=True,
        )
        if estado == "ativa":
            campos["sessao_estado"] = "ativa"
        elif estado == "sem_sessao":
            campos["sessao_estado"] = "sem_sessao"
        elif estado == "nao_interpretado":
            campos["sessao_estado"] = "nao_interpretado"
        elif estado == "falha":
            campos.update(falhas_consecutivas=2, sessao_estado="ativa", ultima_falha_em=agora, ultima_falha_tipo="timeout")
        elif estado == "pendente":
            campos.update(ultima_tentativa_em=None, ultima_valida_em=None)
        elif estado == "desatualizado":
            campos.update(sessao_estado="ativa", ultima_valida_em=agora - timedelta(hours=100))
        elif estado == "desativado":
            campos["monitorado"] = False
        campos.update(extra)
        M2MChip.objects.filter(pk=chip.pk).update(**campos)
        chip.refresh_from_db()
        return chip

    def chip_vinculado(self, n, veiculo, estado="ativa"):
        chip = self.chip(n)
        vinculos.vincular(chip, veiculo, self.usuario, "teste")
        return self.fixar(chip, estado)

    def sem_integracao(self):
        return ConectividadeOperadora.objects.create(nome="Vivo", integracao="", ativo=True)


# ═════════════════════════════════════════════════════════════
# Veículos
# ═════════════════════════════════════════════════════════════

class VeiculoTests(FrotaBase):
    def dados(self, **extra):
        valores = {"codigo": "tr-01", "nome": "Trator John Deere", "tipo": self.trator.pk}
        valores.update(extra)
        return valores

    def test_cria_normaliza_e_grava_o_evento_de_cadastro(self):
        v = frota.criar_veiculo(self.dados(placa="abc-1d23", chassi_serie=" 9bw zzz 123 ", fabricante="John Deere"), self.usuario)
        self.assertEqual((v.codigo, v.placa, v.chassi_serie), ("TR-01", "ABC1D23", "9BWZZZ123"))
        self.assertEqual(v.status, "ativo")
        self.assertEqual(v.criado_por, self.usuario)
        evento = FrotaEvento.objects.get(veiculo=v)
        self.assertEqual((evento.tipo, evento.usuario), ("cadastro", self.usuario))
        self.assertEqual(evento.dados["tipo"], "Trator")

    def test_trator_sem_placa_renavam_e_chassi_funciona(self):
        v = frota.criar_veiculo(self.dados(), self.usuario)
        self.assertEqual((v.placa, v.renavam, v.chassi_serie), ("", "", ""))
        # dois veículos sem documento não colidem (a unicidade só vale para o preenchido)
        w = frota.criar_veiculo(self.dados(codigo="TR-02"), self.usuario)
        self.assertEqual((w.placa, w.chassi_serie), ("", ""))

    def test_codigo_placa_e_chassi_sao_unicos(self):
        frota.criar_veiculo(self.dados(placa="ABC1234", chassi_serie="CH-1"), self.usuario)
        with self.assertRaises(ValidationError) as cm:
            frota.criar_veiculo(self.dados(codigo="TR-01", nome="outro"), self.usuario)
        self.assertIn("código", cm.exception.message_dict["codigo"][0])
        with self.assertRaises(ValidationError) as cm:
            frota.criar_veiculo(self.dados(codigo="TR-02", placa="abc 1234"), self.usuario)
        self.assertIn("placa", cm.exception.message_dict)
        with self.assertRaises(ValidationError) as cm:
            frota.criar_veiculo(self.dados(codigo="TR-03", chassi_serie="ch-1"), self.usuario)
        self.assertIn("chassi_serie", cm.exception.message_dict)
        self.assertEqual(FrotaVeiculo.objects.count(), 1)

    def test_a_restricao_do_banco_tambem_barra_a_duplicidade(self):
        frota.criar_veiculo(self.dados(placa="ABC1234"), self.usuario)
        with self.assertRaises(IntegrityError), transaction.atomic():
            FrotaVeiculo.objects.create(codigo="X", nome="X", tipo=self.trator, placa="ABC1234")

    def test_validacao_de_placa_renavam_ano_e_tipo(self):
        for placa in ("ABC1234", "ABC1D23", "abc-1234"):
            self.assertTrue(frota.normalizar_placa(placa))
        for placa in ("AB12345", "ABCD123", "1234ABC", "ABC12D3", "ABC-12"):
            with self.subTest(placa=placa), self.assertRaises(ValidationError):
                frota.normalizar_placa(placa)
        self.assertEqual(frota.normalizar_renavam("12345678901"), "12345678901")
        for ruim in ("1234567890", "123456789012", "1234567890a"):
            with self.subTest(renavam=ruim), self.assertRaises(ValidationError):
                frota.normalizar_renavam(ruim)
        ano_que_vem = timezone.localdate().year + 1
        self.assertEqual(frota.criar_veiculo(self.dados(codigo="A1", ano_fabricacao=ano_que_vem), self.usuario).ano_fabricacao, ano_que_vem)
        for ano in (1949, ano_que_vem + 1, "abc"):
            with self.subTest(ano=ano), self.assertRaises(ValidationError) as cm:
                frota.criar_veiculo(self.dados(codigo=f"A{ano}", ano_fabricacao=ano), self.usuario)
            self.assertIn("ano_fabricacao", cm.exception.message_dict)
        with self.assertRaises(ValidationError) as cm:
            frota.criar_veiculo({"codigo": "SEM-TIPO", "nome": "x"}, self.usuario)
        self.assertIn("tipo", cm.exception.message_dict)
        inativo = FrotaTipo.objects.create(nome="Antigo", ativo=False)
        with self.assertRaises(ValidationError) as cm:
            frota.criar_veiculo(self.dados(codigo="INA", tipo=inativo.pk), self.usuario)
        self.assertIn("inativo", cm.exception.message_dict["tipo"][0])

    def test_editar_grava_de_para_e_nada_quando_nada_mudou(self):
        v = frota.criar_veiculo(self.dados(), self.usuario)
        frota.editar_veiculo(v, {"nome": "Trator Novo", "modelo": "6110J"}, self.outro)
        v.refresh_from_db()
        self.assertEqual((v.nome, v.modelo, v.atualizado_por), ("Trator Novo", "6110J", self.outro))
        evento = FrotaEvento.objects.filter(veiculo=v, tipo="edicao").get()
        self.assertEqual(evento.dados["alteracoes"]["nome"], {"de": "Trator John Deere", "para": "Trator Novo"})
        antes = FrotaEvento.objects.count()
        frota.editar_veiculo(v, {"nome": "Trator Novo", "modelo": "6110J"}, self.outro)
        self.assertEqual(FrotaEvento.objects.count(), antes)

    def test_editar_nao_muda_o_status_nem_aceita_codigo_de_outro(self):
        a = frota.criar_veiculo(self.dados(), self.usuario)
        b = frota.criar_veiculo(self.dados(codigo="TR-02"), self.usuario)
        frota.editar_veiculo(a, {"status": "desativado", "nome": "Novo nome"}, self.usuario)
        a.refresh_from_db()
        self.assertEqual((a.status, a.nome), ("ativo", "Novo nome"))
        with self.assertRaises(ValidationError):
            frota.editar_veiculo(b, {"codigo": "tr-01"}, self.usuario)

    def test_exige_conectividade_herda_do_tipo_e_o_veiculo_sobrepoe(self):
        FrotaTipo.objects.filter(pk=self.trator.pk).update(exige_conectividade=True)
        v = frota.criar_veiculo(self.dados(), self.usuario)
        v.refresh_from_db()
        self.assertIsNone(v.exige_conectividade)
        self.assertTrue(frota.exige_conectividade(v))
        frota.editar_veiculo(v, {"exige_conectividade": "nao"}, self.usuario)
        self.assertFalse(frota.exige_conectividade(v))
        frota.editar_veiculo(v, {"exige_conectividade": ""}, self.usuario)
        self.assertTrue(frota.exige_conectividade(v))

    # ── status operacional ──
    def test_mudar_status_grava_evento_com_de_para_e_motivo(self):
        v = frota.criar_veiculo(self.dados(), self.usuario)
        frota.mudar_status(v, "manutencao", self.usuario, "Revisão das 500 h")
        v.refresh_from_db()
        self.assertEqual(v.status, "manutencao")
        evento = FrotaEvento.objects.filter(veiculo=v, tipo="status").get()
        self.assertEqual(evento.dados, {"de": "ativo", "para": "manutencao", "motivo": "Revisão das 500 h"})
        with self.assertRaises(ValidationError):
            frota.mudar_status(v, "manutencao", self.usuario)                       # já está
        with self.assertRaises(ValidationError):
            frota.mudar_status(v, "voando", self.usuario)

    def test_desativar_veiculo_com_chip_vinculado_e_recusado(self):
        v = frota.criar_veiculo(self.dados(), self.usuario)
        self.chip_vinculado(1, v)
        with self.assertRaises(ValidationError) as cm:
            frota.mudar_status(v, "desativado", self.usuario)
        self.assertIn("desvincule", cm.exception.message_dict["status"][0])
        v.refresh_from_db()
        self.assertEqual(v.status, "ativo")
        frota.mudar_status(v, "inativo", self.usuario)                              # inativo pode, com chip
        vinculos.desvincular(FrotaVinculo.objects.get(veiculo=v, fim_em__isnull=True).pk, self.usuario, "saiu")
        frota.mudar_status(v, "desativado", self.usuario)
        v.refresh_from_db()
        self.assertEqual(v.status, "desativado")

    def test_status_operacional_e_conectividade_sao_independentes_nos_dois_sentidos(self):
        v = frota.criar_veiculo(self.dados(), self.usuario)
        chip = self.chip_vinculado(1, v, "ativa")
        # 1) o status não mexe em chip nem em conectividade
        for status in ("manutencao", "inativo", "ativo"):
            frota.mudar_status(v, status, self.usuario)
            chip.refresh_from_db()
            self.assertEqual((chip.situacao_uso, chip.sessao_estado, chip.falhas_consecutivas), ("vinculado", "ativa", 0))
            self.assertEqual(frota.conectividade_do_veiculo(v)["estado"], "ativa")
        # 2) a conectividade (falha, sem sessão, nada) não mexe no status
        for estado in ("falha", "sem_sessao", "pendente", "desatualizado", "desativado"):
            self.fixar(chip, estado)
            self.assertEqual(frota.conectividade_do_veiculo(v)["estado"], estado)
            v.refresh_from_db()
            self.assertEqual(v.status, "ativo", f"conectividade {estado} não pode mudar o status")
        self.assertEqual(FrotaEvento.objects.filter(veiculo=v, tipo="status").count(), 3)    # só as mudanças manuais

    def test_evento_do_veiculo_e_imutavel(self):
        v = frota.criar_veiculo(self.dados(), self.usuario)
        evento = FrotaEvento.objects.get(veiculo=v)
        evento.dados = {"x": 1}
        with self.assertRaises(ValidationError):
            evento.save()
        with self.assertRaises(ValidationError):
            evento.delete()
        with self.assertRaises(ValidationError):
            FrotaEvento.objects.filter(veiculo=v).update(tipo="edicao")
        with self.assertRaises(ProtectedError):
            v.delete()


# ═════════════════════════════════════════════════════════════
# Conectividade do veículo
# ═════════════════════════════════════════════════════════════

class ConectividadeDoVeiculoTests(FrotaBase):
    def test_sem_chip(self):
        v = self.veiculo(1)
        c = frota.conectividade_do_veiculo(v)
        self.assertEqual((c["estado"], c["rotulo"], c["chips"], c["contagem"]["total"]), ("sem_chip", "Sem chip", [], 0))

    def test_um_chip_com_sessao_ativa_basta_e_a_contagem_aparece_com_mais_de_um(self):
        v = self.veiculo(1)
        self.chip_vinculado(1, v, "ativa")
        self.chip_vinculado(2, v, "sem_sessao")
        c = frota.conectividade_do_veiculo(v)
        self.assertEqual(c["estado"], "ativa")
        self.assertEqual(c["contagem"]["texto"], "1 de 2 com sessão ativa")
        self.assertEqual(c["contagem"]["por_estado"], {"ativa": 1, "sem_sessao": 1})
        self.assertEqual({i["rotulo"] for i in c["chips"]}, {"Sessão ativa", "Sem sessão"})
        # com um chip só, sem texto de contagem
        solo = self.veiculo(2)
        self.chip_vinculado(3, solo, "ativa")
        self.assertEqual(frota.conectividade_do_veiculo(solo)["contagem"]["texto"], "")

    def test_sem_sessao_so_quando_todos_os_chips_estao_sem_sessao(self):
        v = self.veiculo(1)
        self.chip_vinculado(1, v, "sem_sessao")
        self.chip_vinculado(2, v, "sem_sessao")
        self.assertEqual(frota.conectividade_do_veiculo(v)["estado"], "sem_sessao")
        self.chip_vinculado(3, v, "nao_interpretado")
        self.assertEqual(frota.conectividade_do_veiculo(v)["estado"], "nao_interpretado")     # não são "todos"

    def test_precedencia_falha_pendente_desatualizado_nao_interpretado(self):
        casos = [
            (["falha", "pendente", "desatualizado"], "falha"),
            (["pendente", "desatualizado", "nao_interpretado"], "pendente"),
            (["desatualizado", "nao_interpretado", "sem_sessao"], "desatualizado"),
            (["nao_interpretado", "sem_sessao"], "nao_interpretado"),
            (["sem_sessao", "falha"], "falha"),
        ]
        for n, (estados, esperado) in enumerate(casos, start=1):
            with self.subTest(estados=estados):
                v = self.veiculo(n)
                for i, estado in enumerate(estados):
                    self.chip_vinculado(n * 10 + i, v, estado)
                self.assertEqual(frota.conectividade_do_veiculo(v)["estado"], esperado)

    def test_chip_com_falha_nao_conta_como_ativo_mesmo_com_sessao_antiga_ativa(self):
        v = self.veiculo(1)
        chip = self.chip_vinculado(1, v, "falha")
        self.assertEqual(chip.sessao_estado, "ativa")                                        # o último dado válido, velho
        self.assertEqual(frota.conectividade_do_veiculo(v)["estado"], "falha")

    def test_chip_de_operadora_sem_integracao_e_chip_desativado(self):
        v = self.veiculo(1)
        vivo = self.sem_integracao()
        chip = self.chip(1, operadora=vivo.pk)
        vinculos.vincular(chip, v, self.usuario, "teste")
        self.assertEqual(frota.conectividade_do_veiculo(v)["estado"], "sem_integracao")
        w = self.veiculo(2)
        self.chip_vinculado(2, w, "desativado")
        self.assertEqual(frota.conectividade_do_veiculo(w)["estado"], "desativado")

    def test_o_sql_da_lista_e_a_funcao_em_python_concordam(self):
        estados = ["ativa", "sem_sessao", "falha", "pendente", "desatualizado", "nao_interpretado", "desativado"]
        n = 0
        for e1 in estados:
            for e2 in (None, "sem_sessao", "ativa", "falha"):
                n += 1
                v = self.veiculo(n)
                self.chip_vinculado(n * 2, v, e1)
                if e2:
                    self.chip_vinculado(n * 2 + 1, v, e2)
        vivo = self.sem_integracao()
        v = self.veiculo(900)
        chip = self.chip(900, operadora=vivo.pk)
        vinculos.vincular(chip, v, self.usuario, "teste")
        self.veiculo(901)                                                                     # sem chip
        lista = list(frota.listar_veiculos())
        frota.anotar_veiculos(lista)
        self.assertEqual(len(lista), n + 2)
        for veiculo in lista:
            with self.subTest(veiculo=veiculo.codigo):
                self.assertEqual(veiculo.estado_conectividade, veiculo.conectividade["estado"])

    def test_lista_sem_n_mais_1(self):
        def montar(de, ate):
            for n in range(de, ate):
                self.chip_vinculado(n, self.veiculo(n), "ativa")

        montar(1, 4)
        with CaptureQueriesContext(connection) as poucas:
            frota.anotar_veiculos(frota.listar_veiculos())
        montar(4, 12)
        with CaptureQueriesContext(connection) as muitas:
            lista = frota.anotar_veiculos(frota.listar_veiculos())
        self.assertEqual(len(lista), 11)
        self.assertEqual(len(poucas), len(muitas), "a lista de veículos fez uma consulta por linha")
        self.assertLessEqual(len(muitas), 5)


class ListarVeiculosTests(FrotaBase):
    def setUp(self):
        super().setUp()
        self.loc = Localidade.objects.create(local="Fazenda Norte")
        self.v1 = self.veiculo_do_tipo(self.trator, 1, codigo="TR-01", nome="Trator Verde", placa="ABC1234", localidade=self.loc.pk)
        self.v2 = self.veiculo_do_tipo(self.caminhao, 2, codigo="CM-02", nome="Caminhão Pipa", modelo="Atego")
        self.v3 = self.veiculo_do_tipo(self.pulverizador, 3, codigo="PU-03", nome="Pulverizador Jacto")
        self.chip1 = self.chip_vinculado(1, self.v1, "ativa")

    def codigos(self, **filtros):
        return [v.codigo for v in frota.listar_veiculos(filtros)]

    def test_busca_por_codigo_nome_modelo_placa_e_dados_do_chip(self):
        self.assertEqual(self.codigos(q="tr-01"), ["TR-01"])
        self.assertEqual(self.codigos(q="pipa"), ["CM-02"])
        self.assertEqual(self.codigos(q="atego"), ["CM-02"])
        self.assertEqual(self.codigos(q="abc-1234"), ["TR-01"])
        self.assertEqual(self.codigos(q=base.iccid_valido(1)[8:]), ["TR-01"])                # ICCID do chip ativo
        self.assertEqual(self.codigos(q="Chip 1"), ["TR-01"])
        self.assertEqual(self.codigos(q="nada disso"), [])

    def test_filtros_de_tipo_unidade_status_chip_e_operadora(self):
        frota.mudar_status(self.v3, "manutencao", self.usuario)
        self.assertEqual(self.codigos(tipo=self.caminhao.pk), ["CM-02"])
        self.assertEqual(self.codigos(localidade=self.loc.pk), ["TR-01"])
        self.assertEqual(self.codigos(status="manutencao"), ["PU-03"])
        self.assertEqual(self.codigos(chip="com"), ["TR-01"])
        self.assertCountEqual(self.codigos(chip="sem"), ["CM-02", "PU-03"])
        tim = ConectividadeOperadora.objects.get(nome="TIM")
        self.assertEqual(self.codigos(operadora=tim.pk), ["TR-01"])
        self.assertEqual(self.codigos(operadora=tim.pk + 999), [])

    def test_filtro_de_conectividade_e_de_periodo_da_ultima_consulta(self):
        self.assertEqual(self.codigos(conectividade="ativa"), ["TR-01"])
        self.assertCountEqual(self.codigos(conectividade="sem_chip"), ["CM-02", "PU-03"])
        self.assertEqual(self.codigos(conectividade="falha"), [])
        hoje = timezone.localdate().isoformat()
        ontem = (timezone.localdate() - timedelta(days=1)).isoformat()
        self.assertEqual(self.codigos(de=hoje, ate=hoje), ["TR-01"])
        self.assertEqual(self.codigos(de=ontem, ate=ontem), [])

    def test_ordem_padrao_traz_primeiro_quem_precisa_de_atencao(self):
        FrotaTipo.objects.filter(pk=self.caminhao.pk).update(exige_conectividade=True)
        v4 = self.veiculo_do_tipo(self.trator, 4, codigo="TR-04", nome="Trator com falha")
        self.chip_vinculado(4, v4, "falha")
        frota.mudar_status(self.v3, "desativado", self.usuario)
        # CM-02: ativo, exige chip e não tem  ->  TR-04: falha  ->  TR-01: ativa  ->  PU-03 desativado por último
        self.assertEqual(self.codigos(), ["CM-02", "TR-04", "TR-01", "PU-03"])
        self.assertEqual(self.codigos(ordem="codigo"), ["CM-02", "PU-03", "TR-01", "TR-04"])


# ═════════════════════════════════════════════════════════════
# Vínculo e movimentações
# ═════════════════════════════════════════════════════════════

class VincularTests(FrotaBase):
    def test_vincula_grava_movimentacao_evento_vinculo_e_situacao_de_uso(self):
        chip, v = self.chip(1), self.veiculo(1)
        r = vinculos.vincular(chip, v, self.usuario, "Instalado no cabine")
        chip.refresh_from_db()
        self.assertEqual(chip.situacao_uso, "vinculado")
        self.assertEqual((r.vinculo.chip, r.vinculo.veiculo, r.vinculo.inicio_por), (chip, v, self.usuario))
        self.assertIsNone(r.vinculo.fim_em)
        mov = r.movimentacao
        self.assertEqual((mov.tipo, mov.chip, mov.veiculo_destino, mov.veiculo_origem), ("vinculacao", chip, v, None))
        self.assertEqual((mov.motivo, mov.usuario, mov.usuario_nome), ("Instalado no cabine", self.usuario, "ti_m2m"))
        self.assertEqual(r.vinculo.mov_inicio, mov)
        evento = M2MEvento.objects.get(chip=chip, tipo="vinculado")
        self.assertEqual(evento.dados["movimentacao_id"], mov.pk)
        self.assertIn(v.codigo, evento.dados["veiculo_destino"])
        self.assertEqual(r.avisos, [])

    def test_motivo_e_usuario_sao_obrigatorios_e_nada_e_gravado_sem_eles(self):
        chip, v = self.chip(1), self.veiculo(1)
        with self.assertRaises(ValidationError) as cm:
            vinculos.vincular(chip, v, self.usuario, "   ")
        self.assertIn("motivo", cm.exception.message_dict)
        with self.assertRaises(ValidationError):
            vinculos.vincular(chip, v, None, "x")
        with self.assertRaises(ValidationError):
            vinculos.vincular(chip, v, self.usuario, "x" * 256)
        self.assertEqual(FrotaVinculo.objects.count(), 0)
        self.assertEqual(FrotaMovimentacao.objects.filter(tipo="vinculacao").count(), 0)

    def test_o_veiculo_pode_ter_varios_chips(self):
        v = self.veiculo(1)
        for n in (1, 2, 3):
            vinculos.vincular(self.chip(n), v, self.usuario, "teste")
        self.assertEqual(FrotaVinculo.objects.filter(veiculo=v, fim_em__isnull=True).count(), 3)

    def test_chip_ja_vinculado_e_recusado_dizendo_onde(self):
        chip, v1, v2 = self.chip(1), self.veiculo(1), self.veiculo(2)
        vinculos.vincular(chip, v1, self.usuario, "teste")
        with self.assertRaises(ValidationError) as cm:
            vinculos.vincular(chip, v2, self.usuario, "teste")
        texto = " ".join(cm.exception.messages)
        self.assertIn("já está vinculado", texto)
        self.assertIn(v1.codigo, texto)
        self.assertEqual(FrotaVinculo.objects.filter(chip=chip).count(), 1)

    def test_chip_em_manutencao_ou_inativo_nao_vincula(self):
        v = self.veiculo(1)
        for n, uso in ((1, "manutencao"), (2, "inativo")):
            chip = self.chip(n)
            M2MChip.objects.filter(pk=chip.pk).update(situacao_uso=uso)
            with self.assertRaises(ValidationError) as cm:
                vinculos.vincular(chip, v, self.usuario, "teste")
            self.assertIn("Devolva-o à disponibilidade", cm.exception.messages[0])

    def test_veiculo_desativado_recusa_e_manutencao_ou_inativo_vincula_com_aviso(self):
        chip = self.chip(1)
        v = self.veiculo(1)
        FrotaVeiculo.objects.filter(pk=v.pk).update(status="desativado")
        with self.assertRaises(ValidationError) as cm:
            vinculos.vincular(chip, v, self.usuario, "teste")
        self.assertIn("desativado", cm.exception.message_dict["veiculo"][0])
        FrotaVeiculo.objects.filter(pk=v.pk).update(status="manutencao")
        r = vinculos.vincular(chip, v, self.usuario, "teste")
        self.assertEqual(len(r.avisos), 1)
        self.assertIn("em manutenção", r.avisos[0])
        w = self.veiculo(2)
        FrotaVeiculo.objects.filter(pk=w.pk).update(status="inativo")
        r = vinculos.vincular(self.chip(2), w, self.usuario, "teste")
        self.assertIn("inativo", r.avisos[0])

    def test_a_restricao_do_banco_impede_dois_vinculos_ativos_do_mesmo_chip(self):
        chip, v1, v2 = self.chip(1), self.veiculo(1), self.veiculo(2)
        vinculos.vincular(chip, v1, self.usuario, "teste")
        with self.assertRaises(IntegrityError), transaction.atomic():
            FrotaVinculo.objects.create(chip=chip, veiculo=v2)

    def test_o_service_converte_a_corrida_perdida_em_mensagem_amigavel_e_nao_grava_nada(self):
        chip, v1, v2 = self.chip(1), self.veiculo(1), self.veiculo(2)
        vinculos.vincular(chip, v1, self.usuario, "primeira pessoa")
        # A segunda pessoa leu o chip ANTES e a conferência não enxerga o vínculo: só a restrição do banco barra.
        M2MChip.objects.filter(pk=chip.pk).update(situacao_uso="disponivel")
        with mock.patch.object(vinculos, "_vinculo_ativo_do_chip", return_value=None):
            with self.assertRaises(ValidationError) as cm:
                vinculos.vincular(chip, v2, self.outro, "segunda pessoa")
        self.assertIn("acabou de ser vinculado por outra pessoa", cm.exception.messages[0])
        self.assertEqual(FrotaVinculo.objects.filter(chip=chip, fim_em__isnull=True).get().veiculo, v1)
        self.assertEqual(FrotaMovimentacao.objects.filter(chip=chip, tipo="vinculacao").count(), 1)      # a da segunda foi desfeita

    def test_tudo_ou_nada_se_algo_falha_no_meio(self):
        chip, v = self.chip(1), self.veiculo(1)
        with mock.patch.object(vinculos, "_gravar_evento", side_effect=RuntimeError("falhou")):
            with self.assertRaises(RuntimeError):
                vinculos.vincular(chip, v, self.usuario, "teste")
        chip.refresh_from_db()
        self.assertEqual(chip.situacao_uso, "disponivel")
        self.assertEqual((FrotaVinculo.objects.count(), FrotaMovimentacao.objects.filter(tipo="vinculacao").count()), (0, 0))


class TransferirDesvincularTests(FrotaBase):
    def test_transferir_caminhao_para_pulverizador_preserva_os_dois_historicos(self):
        caminhao = self.veiculo_do_tipo(self.caminhao, 1, codigo="CM-01")
        pulv = self.veiculo_do_tipo(self.pulverizador, 2, codigo="PU-02")
        chip = self.chip(1)
        antigo = vinculos.vincular(chip, caminhao, self.usuario, "Instalação").vinculo
        r = vinculos.transferir(antigo.pk, pulv, self.outro, "Trocou de máquina")
        chip.refresh_from_db()
        antigo.refresh_from_db()
        self.assertEqual(chip.situacao_uso, "vinculado")                                       # continua vinculado
        self.assertEqual((antigo.veiculo, antigo.fim_por, antigo.motivo_fim), (caminhao, self.outro, "Trocou de máquina"))
        self.assertIsNotNone(antigo.fim_em)                                                    # o antigo foi ENCERRADO...
        novo = r.vinculo
        self.assertEqual((novo.veiculo, novo.inicio_por), (pulv, self.outro))
        self.assertIsNone(novo.fim_em)                                                         # ...e o novo criado
        self.assertEqual(FrotaVinculo.objects.filter(chip=chip).count(), 2)                    # os dois ficam
        # UMA movimentação, com origem e destino
        movs = FrotaMovimentacao.objects.filter(chip=chip, tipo="transferencia")
        self.assertEqual(movs.count(), 1)
        self.assertEqual((movs[0].veiculo_origem, movs[0].veiculo_destino), (caminhao, pulv))
        self.assertEqual((antigo.mov_fim, novo.mov_inicio), (movs[0], movs[0]))
        self.assertEqual(M2MEvento.objects.filter(chip=chip, tipo="transferido").count(), 1)
        # o histórico do chip mostra os dois períodos, o ativo primeiro
        periodos = vinculos.historico_chip(chip)
        self.assertEqual([p["veiculo"].codigo for p in periodos], ["PU-02", "CM-01"])
        self.assertEqual([p["ativo"] for p in periodos], [True, False])
        self.assertEqual(periodos[1]["fim_por"], "outro_usuario")
        self.assertEqual(periodos[1]["fim_tipo"], "transferencia")
        # e cada veículo só enxerga o seu período
        self.assertEqual([v.fim_em is None for v in caminhao.vinculos.all()], [False])
        self.assertEqual([v.fim_em is None for v in pulv.vinculos.all()], [True])

    def test_destino_igual_a_origem_ou_desativado_e_recusado(self):
        v1, v2 = self.veiculo(1), self.veiculo(2)
        vinculo = vinculos.vincular(self.chip(1), v1, self.usuario, "teste").vinculo
        with self.assertRaises(ValidationError) as cm:
            vinculos.transferir(vinculo.pk, v1, self.usuario, "teste")
        self.assertIn("mesmo de origem", cm.exception.message_dict["veiculo_destino"][0])
        FrotaVeiculo.objects.filter(pk=v2.pk).update(status="desativado")
        with self.assertRaises(ValidationError):
            vinculos.transferir(vinculo.pk, v2, self.usuario, "teste")
        vinculo.refresh_from_db()
        self.assertIsNone(vinculo.fim_em)                                                      # nada mudou
        self.assertEqual(FrotaMovimentacao.objects.filter(tipo="transferencia").count(), 0)

    def test_desvincular_deixa_o_chip_cadastrado_e_disponivel(self):
        chip, v = self.chip(1), self.veiculo(1)
        vinculo = vinculos.vincular(chip, v, self.usuario, "teste").vinculo
        r = vinculos.desvincular(vinculo.pk, self.outro, "Veículo vendido")
        chip.refresh_from_db()
        self.assertEqual(chip.situacao_uso, "disponivel")
        self.assertTrue(M2MChip.objects.filter(pk=chip.pk).exists())                           # continua cadastrado
        self.assertEqual(r.vinculo_encerrado.fim_por, self.outro)
        mov = r.movimentacao
        self.assertEqual((mov.tipo, mov.veiculo_origem, mov.veiculo_destino, mov.motivo), ("desvinculacao", v, None, "Veículo vendido"))
        self.assertIn(chip, [c for c in M2MChip.objects.filter(situacao_uso="disponivel")])
        # pode ser vinculado de novo
        vinculos.vincular(chip, v, self.usuario, "Voltou")
        self.assertEqual(FrotaVinculo.objects.filter(chip=chip).count(), 2)

    def test_desvincular_para_manutencao_e_destino_invalido(self):
        chip, v = self.chip(1), self.veiculo(1)
        vinculo = vinculos.vincular(chip, v, self.usuario, "teste").vinculo
        with self.assertRaises(ValidationError) as cm:
            vinculos.desvincular(vinculo.pk, self.usuario, "x", destino="inativo")
        self.assertIn("destino", cm.exception.message_dict)
        vinculos.desvincular(vinculo.pk, self.usuario, "x", destino="manutencao")
        chip.refresh_from_db()
        self.assertEqual(chip.situacao_uso, "manutencao")

    def test_cenario_8_vinculo_ja_desvinculado_por_outra_pessoa_e_recusado_dizendo_quem_e_quando(self):
        v1, v2 = self.veiculo(1), self.veiculo(2)
        vinculo = vinculos.vincular(self.chip(1), v1, self.usuario, "teste").vinculo
        vinculos.desvincular(vinculo.pk, self.outro, "primeiro a clicar")
        quando = timezone.localtime(FrotaVinculo.objects.get(pk=vinculo.pk).fim_em).strftime("%d/%m %H:%M")
        for operacao in (
            lambda: vinculos.transferir(vinculo.pk, v2, self.usuario, "tarde demais"),
            lambda: vinculos.desvincular(vinculo.pk, self.usuario, "tarde demais"),
            lambda: vinculos.substituir(vinculo.pk, self.chip(2), self.usuario, "tarde demais"),
        ):
            with self.assertRaises(ValidationError) as cm:
                operacao()
            texto = " ".join(cm.exception.messages)
            self.assertIn("já foi desvinculado por outro_usuario", texto)
            self.assertIn(quando, texto)
        # nada do segundo clique foi gravado
        self.assertEqual(FrotaMovimentacao.objects.filter(tipo__in=["transferencia", "substituicao"]).count(), 0)
        self.assertEqual(FrotaMovimentacao.objects.filter(tipo="desvinculacao").count(), 1)

    def test_a_recusa_diz_o_verbo_certo_para_transferencia_e_inativacao(self):
        v1, v2 = self.veiculo(1), self.veiculo(2)
        a = vinculos.vincular(self.chip(1), v1, self.usuario, "teste").vinculo
        vinculos.transferir(a.pk, v2, self.outro, "foi para outro")
        with self.assertRaises(ValidationError) as cm:
            vinculos.desvincular(a.pk, self.usuario, "x")
        self.assertIn("já foi transferido para outro veículo por outro_usuario", cm.exception.messages[0])
        b = vinculos.vincular(self.chip(2), v1, self.usuario, "teste").vinculo
        vinculos.inativar(M2MChip.objects.get(nome="Chip 2"), self.outro, "chip queimado")
        with self.assertRaises(ValidationError) as cm:
            vinculos.desvincular(b.pk, self.usuario, "x")
        self.assertIn("já foi inativado por outro_usuario", cm.exception.messages[0])

    def test_vinculo_inexistente_ou_invalido(self):
        for ruim in (999999, "abc", None):
            with self.subTest(vinculo=ruim), self.assertRaises(ValidationError):
                vinculos.desvincular(ruim, self.usuario, "x")


class SubstituirRetornarInativarTests(FrotaBase):
    def test_substituir_grava_o_par_com_o_mesmo_uuid_e_troca_as_situacoes(self):
        v = self.veiculo(1)
        velho, novo = self.chip(1), self.chip(2)
        vinculo = vinculos.vincular(velho, v, self.usuario, "teste").vinculo
        r = vinculos.substituir(vinculo.pk, novo, self.outro, "Chip velho sem sinal", destino_antigo="manutencao")
        velho.refresh_from_db(), novo.refresh_from_db()
        self.assertEqual((velho.situacao_uso, novo.situacao_uso), ("manutencao", "vinculado"))
        saida, entrada = r.movimentacoes
        self.assertEqual(saida.operacao, entrada.operacao)
        self.assertEqual(r.operacao, saida.operacao)
        self.assertEqual((saida.tipo, saida.chip, saida.chip_relacionado, saida.veiculo_origem, saida.veiculo_destino), ("substituicao", velho, novo, v, None))
        self.assertEqual((entrada.tipo, entrada.chip, entrada.chip_relacionado, entrada.veiculo_origem, entrada.veiculo_destino), ("substituicao", novo, velho, None, v))
        self.assertEqual(FrotaMovimentacao.objects.filter(operacao=saida.operacao).count(), 2)
        vinculo.refresh_from_db()
        self.assertEqual(vinculo.mov_fim, saida)
        self.assertEqual((r.vinculo.chip, r.vinculo.veiculo, r.vinculo.mov_inicio), (novo, v, entrada))
        # a tela de histórico mostra a substituição como UMA ação
        grupos = vinculos.agrupar_por_operacao(vinculos.historico_movimentacoes({"tipo": "substituicao"}))
        self.assertEqual(len(grupos), 1)
        self.assertEqual(len(grupos[0]["movimentacoes"]), 2)

    def test_substituir_recusa_chip_novo_igual_vinculado_em_manutencao_ou_inativo(self):
        v1, v2 = self.veiculo(1), self.veiculo(2)
        a, b = self.chip(1), self.chip(2)
        vinculo = vinculos.vincular(a, v1, self.usuario, "teste").vinculo
        vinculos.vincular(b, v2, self.usuario, "teste")
        with self.assertRaises(ValidationError) as cm:
            vinculos.substituir(vinculo.pk, a, self.usuario, "x")
        self.assertIn("chip_novo", cm.exception.message_dict)
        with self.assertRaises(ValidationError) as cm:
            vinculos.substituir(vinculo.pk, b, self.usuario, "x")                              # já está em outro veículo
        self.assertIn("já está vinculado", cm.exception.message_dict["chip_novo"][0])
        for n, uso in ((3, "manutencao"), (4, "inativo")):
            chip = self.chip(n)
            M2MChip.objects.filter(pk=chip.pk).update(situacao_uso=uso)
            with self.assertRaises(ValidationError):
                vinculos.substituir(vinculo.pk, chip, self.usuario, "x")
        self.assertEqual(FrotaMovimentacao.objects.filter(tipo="substituicao").count(), 0)
        vinculo.refresh_from_db()
        self.assertIsNone(vinculo.fim_em)

    def test_retornar_disponibilidade_de_manutencao_e_de_inativo(self):
        v = self.veiculo(1)
        chip = self.chip(1)
        vinculo = vinculos.vincular(chip, v, self.usuario, "teste").vinculo
        vinculos.desvincular(vinculo.pk, self.usuario, "revisão", destino="manutencao")
        r = vinculos.retornar_disponibilidade(chip, self.outro, "Voltou da revisão")
        chip.refresh_from_db()
        self.assertEqual(chip.situacao_uso, "disponivel")
        self.assertEqual((r.movimentacao.tipo, r.movimentacao.motivo), ("retorno", "Voltou da revisão"))
        with self.assertRaises(ValidationError) as cm:
            vinculos.retornar_disponibilidade(chip, self.usuario, "x")                          # já disponível
        self.assertIn("volta à disponibilidade (este está disponível)", cm.exception.messages[0])
        vinculos.inativar(chip, self.usuario, "descartado")
        chip.refresh_from_db()
        self.assertFalse(chip.monitorado)
        vinculos.retornar_disponibilidade(chip, self.usuario, "reativado")
        chip.refresh_from_db()
        self.assertEqual((chip.situacao_uso, chip.monitorado), ("disponivel", True))           # retoma o monitoramento
        self.assertEqual(M2MEvento.objects.filter(chip=chip, tipo="reativado").count(), 1)

    def test_retornar_sem_retomar_o_monitoramento(self):
        chip = self.chip(1)
        vinculos.inativar(chip, self.usuario, "descartado")
        vinculos.retornar_disponibilidade(chip, self.usuario, "voltou", retomar_monitoramento=False)
        chip.refresh_from_db()
        self.assertEqual((chip.situacao_uso, chip.monitorado), ("disponivel", False))

    def test_inativar_encerra_o_vinculo_desliga_o_monitoramento_e_encerra_alertas(self):
        v = self.veiculo(1)
        chip = self.chip(1)
        vinculo = vinculos.vincular(chip, v, self.usuario, "teste").vinculo
        self.falhar(chip, 3)
        m2m.avaliar_ocorrencias(chip, notificar=False)
        self.assertEqual(M2MOcorrencia.objects.filter(chip=chip, estado="aberta").count(), 1)
        r = vinculos.inativar(chip, self.outro, "Chip queimado")
        chip.refresh_from_db()
        vinculo.refresh_from_db()
        self.assertEqual((chip.situacao_uso, chip.monitorado), ("inativo", False))             # inativação desliga o monitoramento
        self.assertIsNotNone(vinculo.fim_em)
        self.assertEqual(vinculo.mov_fim, r.movimentacao)
        self.assertEqual((r.movimentacao.tipo, r.movimentacao.veiculo_origem), ("inativacao", v))
        self.assertEqual(M2MOcorrencia.objects.filter(chip=chip, estado__in=("aberta", "em_tratamento")).count(), 0)
        self.assertEqual(M2MEvento.objects.filter(chip=chip, tipo="desativado").count(), 1)    # reaproveita m2m.desativar
        self.assertTrue(M2MChip.objects.filter(pk=chip.pk).exists())                           # nunca apaga
        with self.assertRaises(ValidationError):
            vinculos.inativar(chip, self.usuario, "de novo")
        with self.assertRaises(ValidationError):
            vinculos.vincular(chip, v, self.usuario, "teste")                                  # inativo não vincula

    def test_inativar_chip_disponivel_sem_vinculo(self):
        chip = self.chip(1)
        r = vinculos.inativar(chip, self.usuario, "sobra")
        self.assertIsNone(r.vinculo_encerrado)
        chip.refresh_from_db()
        self.assertEqual(chip.situacao_uso, "inativo")


class ImutabilidadeTests(FrotaBase):
    def setUp(self):
        super().setUp()
        self.v = self.veiculo(1)
        self.chip_ = self.chip(1)
        self.vinculo = vinculos.vincular(self.chip_, self.v, self.usuario, "teste").vinculo
        self.mov = FrotaMovimentacao.objects.filter(tipo="vinculacao").get()

    def test_a_movimentacao_nao_se_edita_nem_se_apaga(self):
        self.mov.motivo = "alterado"
        with self.assertRaises(ValidationError):
            self.mov.save()
        with self.assertRaises(ValidationError):
            self.mov.delete()
        with self.assertRaises(ValidationError):
            FrotaMovimentacao.objects.all().update(motivo="alterado")
        with self.assertRaises(ValidationError):
            FrotaMovimentacao.objects.all().delete()
        with self.assertRaises(ValidationError):
            FrotaMovimentacao.objects.bulk_update([self.mov], ["motivo"])
        self.assertEqual(FrotaMovimentacao.objects.get(pk=self.mov.pk).motivo, "teste")

    def test_o_vinculo_nao_se_apaga_e_so_o_encerramento_e_gravavel(self):
        with self.assertRaises(ValidationError):
            self.vinculo.delete()
        self.vinculo.veiculo = self.veiculo(2)
        with self.assertRaises(ValidationError):
            self.vinculo.save()
        with self.assertRaises(ValidationError):
            self.vinculo.save(update_fields=["veiculo"])
        vinculos.desvincular(self.vinculo.pk, self.usuario, "fim")
        self.vinculo.refresh_from_db()
        self.vinculo.motivo_fim = "reescrito"
        with self.assertRaises(ValidationError):                                               # já encerrado: nunca mais muda
            self.vinculo.save(update_fields=["motivo_fim"])
        self.assertEqual(FrotaVinculo.objects.get(pk=self.vinculo.pk).motivo_fim, "fim")

    def test_chip_e_veiculo_com_historico_nao_se_apagam(self):
        with self.assertRaises(ProtectedError):
            self.chip_.delete()
        with self.assertRaises(ProtectedError):
            self.v.delete()

    def test_o_admin_dos_modelos_novos_e_somente_leitura(self):
        super_user = User.objects.create(username="super_frota_leitura", is_superuser=True, is_staff=True)
        requisicao = RequestFactory().get("/")
        requisicao.user = super_user
        for modelo in (
            ConectividadeOperadora, FrotaTipo, FrotaVeiculo, FrotaVinculo, FrotaMovimentacao, FrotaEvento,
            FrotaImportacaoChips, FrotaRegraAlerta,
        ):
            with self.subTest(modelo=modelo.__name__):
                ma = django_admin.site._registry[modelo]
                self.assertTrue(ma.has_view_permission(requisicao))
                self.assertFalse(ma.has_add_permission(requisicao))
                self.assertFalse(ma.has_change_permission(requisicao))
                self.assertFalse(ma.has_delete_permission(requisicao))


class LeiturasDeMovimentacaoTests(FrotaBase):
    def test_historico_de_movimentacoes_filtra_e_vem_do_mais_novo(self):
        v1, v2 = self.veiculo(1, codigo="TR-01"), self.veiculo(2, codigo="CM-02", placa="ABC1234")
        a, b = self.chip(1, nome="Pivô 1"), self.chip(2, nome="Pivô 2")
        va = vinculos.vincular(a, v1, self.usuario, "Instalação do pivô").vinculo
        vinculos.vincular(b, v2, self.outro, "Instalação")
        vinculos.transferir(va.pk, v2, self.usuario, "Mudou")
        todas = list(vinculos.historico_movimentacoes())
        self.assertEqual(todas[0].tipo, "transferencia")                                       # a mais nova primeiro
        self.assertEqual(len(todas), 5)                                                        # 2 entradas + 2 vinculações + 1 transferência
        filtrar = lambda **f: [m.pk for m in vinculos.historico_movimentacoes(f)]
        self.assertEqual(len(filtrar(tipo="vinculacao")), 2)
        self.assertEqual(len(filtrar(chip="Pivô 1")), 3)
        self.assertEqual(len(filtrar(chip=str(b.pk))), 2)
        self.assertEqual(len(filtrar(veiculo="CM-02")), 2)                                     # destino da transferência + vinculação de b
        self.assertEqual(len(filtrar(veiculo="abc-1234")), 2)                                  # por placa
        self.assertEqual(len(filtrar(veiculo="TR-01")), 2)                                     # vinculação de a + origem da transferência
        self.assertEqual(len(filtrar(usuario="outro")), 1)
        self.assertEqual(len(filtrar(usuario=str(self.outro.pk))), 1)
        self.assertEqual(len(filtrar(q="instalação do pivô")), 1)
        hoje = timezone.localdate().isoformat()
        self.assertEqual(len(filtrar(de=hoje, ate=hoje)), 5)
        self.assertEqual(filtrar(de=(timezone.localdate() + timedelta(days=1)).isoformat()), [])
        self.assertEqual(filtrar(tipo="nao-existe"), filtrar())                                # filtro inválido é ignorado

    def test_historico_sem_n_mais_1(self):
        for n in range(1, 4):
            vinculos.vincular(self.chip(n), self.veiculo(n), self.usuario, "teste")
        with CaptureQueriesContext(connection) as q:
            lista = list(vinculos.historico_movimentacoes())
            for m in lista:
                m.chip.nome, m.veiculo_destino, m.usuario
        self.assertEqual(len(lista), 6)
        self.assertEqual(len(q), 1)

    def test_chips_disponiveis_so_traz_os_disponiveis_e_busca(self):
        v = self.veiculo(1)
        livre = self.chip(1, nome="Livre", numero_linha="(77) 99999-1234")
        preso = self.chip(2, nome="Preso")
        vinculos.vincular(preso, v, self.usuario, "teste")
        quebrado = self.chip(3, nome="Quebrado")
        M2MChip.objects.filter(pk=quebrado.pk).update(situacao_uso="manutencao")
        nomes = lambda busca="": [c["nome"] for c in vinculos.chips_disponiveis(busca)]
        self.assertEqual(nomes(), ["Livre"])
        self.assertEqual(nomes("liv"), ["Livre"])
        self.assertEqual(nomes("99999"), ["Livre"])                                            # por número da linha
        self.assertEqual(nomes(base.iccid_valido(1)[10:]), ["Livre"])
        self.assertEqual(nomes("preso"), [])
        item = vinculos.chips_disponiveis()[0]
        self.assertEqual((item["id"], item["operadora"], item["linha"]), (livre.pk, "TIM", "77999991234"))
        self.assertIn("Livre", item["text"])

    def test_duracao_formatada(self):
        f = vinculos.formatar_duracao
        self.assertEqual(f(timedelta(seconds=20)), "menos de 1 min")
        self.assertEqual(f(timedelta(minutes=12)), "12 min")
        self.assertEqual(f(timedelta(hours=3, minutes=20)), "3 h 20 min")
        self.assertEqual(f(timedelta(hours=5)), "5 h")
        self.assertEqual(f(timedelta(days=2, hours=4)), "2 dias 4 h")
        self.assertEqual(f(timedelta(days=1)), "1 dia")
        self.assertEqual(f(None), "—")


# ═════════════════════════════════════════════════════════════
# Cadastro de chip: operadora, número da linha, veículo
# ═════════════════════════════════════════════════════════════

class ChipCadastroNovoTests(FrotaBase):
    def test_operadora_padrao_e_a_tim_e_o_numero_da_linha_so_tem_digitos(self):
        chip = m2m.criar_chip({"nome": "A", "iccid": base.iccid_valido(1), "numero_linha": "+55 (77) 99999-1234"}, self.usuario)
        self.assertEqual((chip.operadora.nome, chip.numero_linha, chip.situacao_uso), ("TIM", "5577999991234", "disponivel"))

    def test_numero_da_linha_nao_e_unico_e_e_validado(self):
        a = m2m.criar_chip({"nome": "A", "iccid": base.iccid_valido(1), "numero_linha": "77999991234"}, self.usuario)
        b = m2m.criar_chip({"nome": "B", "iccid": base.iccid_valido(2), "numero_linha": "(77) 99999-1234"}, self.usuario)
        self.assertEqual(a.numero_linha, b.numero_linha)
        for ruim in ("abc", "123", "7799999123456789", "77 99999-12a4"):
            with self.subTest(numero=ruim), self.assertRaises(ValidationError) as cm:
                m2m.criar_chip({"nome": f"X{ruim}", "iccid": base.iccid_valido(3), "numero_linha": ruim}, self.usuario)
            self.assertIn("numero_linha", cm.exception.message_dict)

    def test_operadora_escolhida_inativa_ou_inexistente(self):
        vivo = self.sem_integracao()
        chip = m2m.criar_chip({"nome": "V", "iccid": base.iccid_valido(1), "operadora": vivo.pk}, self.usuario)
        self.assertEqual(chip.operadora, vivo)
        self.assertFalse(chip.consultavel_pela_api)
        ConectividadeOperadora.objects.filter(pk=vivo.pk).update(ativo=False)
        with self.assertRaises(ValidationError) as cm:
            m2m.criar_chip({"nome": "W", "iccid": base.iccid_valido(2), "operadora": vivo.pk}, self.usuario)
        self.assertIn("inativa", cm.exception.message_dict["operadora"][0])
        with self.assertRaises(ValidationError) as cm:
            m2m.criar_chip({"nome": "Z", "iccid": base.iccid_valido(3), "operadora": 99999}, self.usuario)
        self.assertIn("operadora", cm.exception.message_dict)
        # editar mantém o chip na operadora inativa (só não aceita ESCOLHER uma inativa)
        m2m.editar_chip(chip, {"descricao": "continua"}, self.usuario)

    def test_cadastro_com_veiculo_vincula_na_mesma_transacao(self):
        v = self.veiculo(1)
        chip = m2m.criar_chip({"nome": "A", "iccid": base.iccid_valido(1), "veiculo": v.pk}, self.usuario)
        self.assertEqual(chip.situacao_uso, "vinculado")
        vinculo = FrotaVinculo.objects.get(chip=chip)
        self.assertEqual((vinculo.veiculo, vinculo.fim_em), (v, None))
        self.assertEqual(
            list(FrotaMovimentacao.objects.filter(chip=chip).order_by("pk").values_list("tipo", flat=True)),
            ["entrada", "vinculacao"],
        )

    def test_cadastro_com_veiculo_desativado_nao_grava_nada(self):
        v = self.veiculo(1)
        FrotaVeiculo.objects.filter(pk=v.pk).update(status="desativado")
        with self.assertRaises(ValidationError) as cm:
            m2m.criar_chip({"nome": "A", "iccid": base.iccid_valido(1), "veiculo": v.pk}, self.usuario)
        self.assertIn("desativado", cm.exception.message_dict["veiculo"][0])
        self.assertEqual(M2MChip.objects.count(), 0)
        self.assertEqual(FrotaMovimentacao.objects.count(), 0)

    def test_todo_chip_nasce_com_a_movimentacao_de_entrada(self):
        chip = self.chip(1)
        entrada = FrotaMovimentacao.objects.get(chip=chip)
        self.assertEqual((entrada.tipo, entrada.motivo, entrada.usuario, entrada.importacao), ("entrada", "", self.usuario, None))
        with self.assertRaises(ValidationError):                                               # usuário é obrigatório
            m2m.criar_chip({"nome": "Sem usuario", "iccid": base.iccid_valido(2)}, None)
        self.assertFalse(M2MChip.objects.filter(nome="Sem usuario").exists())

    def test_editar_nao_muda_a_situacao_de_uso_nem_o_veiculo(self):
        v1, v2 = self.veiculo(1), self.veiculo(2)
        chip = m2m.criar_chip({"nome": "A", "iccid": base.iccid_valido(1), "veiculo": v1.pk}, self.usuario)
        m2m.editar_chip(chip, {"descricao": "nova", "veiculo": v1.pk, "numero_linha": "77999991234"}, self.usuario)   # mesmo veículo: ok
        chip.refresh_from_db()
        self.assertEqual((chip.descricao, chip.numero_linha, chip.situacao_uso), ("nova", "77999991234", "vinculado"))
        evento = M2MEvento.objects.filter(chip=chip, tipo="edicao").get()
        self.assertEqual(set(evento.dados["alteracoes"]), {"descricao", "numero_linha"})
        with self.assertRaises(ValidationError) as cm:
            m2m.editar_chip(chip, {"veiculo": v2.pk}, self.usuario)
        self.assertIn("vincular, transferir ou desvincular", cm.exception.message_dict["veiculo"][0])
        m2m.editar_chip(chip, {"situacao_uso": "inativo"}, self.usuario)                        # chave desconhecida: ignorada
        chip.refresh_from_db()
        self.assertEqual(chip.situacao_uso, "vinculado")

    def test_listar_chips_filtra_por_veiculo_operadora_e_situacao_de_uso(self):
        v = self.veiculo(1, codigo="TR-01", nome="Trator Verde", placa="ABC1234")
        preso = self.chip(1, nome="Preso")
        vinculos.vincular(preso, v, self.usuario, "teste")
        livre = self.chip(2, nome="Livre", numero_linha="77988887777")
        vivo = self.sem_integracao()
        outro = self.chip(3, nome="Da Vivo", operadora=vivo.pk)
        nomes = lambda **f: [c.nome for c in m2m.listar_chips(f)]
        self.assertEqual(nomes(veiculo="tr-01"), ["Preso"])
        self.assertEqual(nomes(veiculo=str(v.pk)), ["Preso"])
        self.assertEqual(nomes(veiculo="abc-1234"), ["Preso"])
        self.assertEqual(nomes(q="verde"), ["Preso"])
        self.assertEqual(nomes(q="77988887777"), ["Livre"])
        self.assertEqual(nomes(operadora=vivo.pk), ["Da Vivo"])
        self.assertEqual(nomes(q="vivo"), ["Da Vivo"])
        self.assertEqual(nomes(situacao_uso="vinculado"), ["Preso"])
        self.assertCountEqual(nomes(situacao_uso="disponivel"), ["Livre", "Da Vivo"])
        self.assertEqual(nomes(situacao="sem_integracao"), ["Da Vivo"])
        anotados = m2m.anotar_chips(m2m.listar_chips({"situacao_uso": "vinculado"}))
        self.assertEqual(anotados[0].veiculo_atual, v)
        self.assertIsNone(m2m.anotar_chips(m2m.listar_chips({"q": "Livre"}))[0].veiculo_atual)

    def test_buscar_veiculos_para_o_select2(self):
        a = self.veiculo(1, codigo="TR-01", nome="Trator Verde", placa="ABC1234", modelo="6110J")
        b = self.veiculo(2, codigo="TR-02", nome="Trator Velho")
        FrotaVeiculo.objects.filter(pk=b.pk).update(status="desativado")
        ids = lambda q, **kw: [r["id"] for r in m2m.buscar_veiculos(q, **kw)]
        self.assertEqual(ids("trator"), [a.pk])                                                # desativado não aparece
        self.assertEqual(ids("trator", incluir_desativados=True), [a.pk, b.pk])
        self.assertEqual(ids("abc-1234"), [a.pk])
        self.assertEqual(ids("6110"), [a.pk])
        self.assertEqual(ids("t"), [])                                                         # mínimo de 2 letras
        self.assertEqual(m2m.buscar_veiculos("tr-01")[0]["text"], "TR-01 · Trator Verde · 6110J · ABC1234")


# ═════════════════════════════════════════════════════════════
# Indicadores e área de atenção
# ═════════════════════════════════════════════════════════════

class IndicadoresDaFrotaTests(FrotaBase):
    def montar(self):
        FrotaTipo.objects.filter(pk=self.trator.pk).update(exige_conectividade=True)
        a = self.veiculo_do_tipo(self.trator, 1, codigo="TR-01")            # com chip, sessão ativa
        b = self.veiculo_do_tipo(self.trator, 2, codigo="TR-02")            # com chip com falha
        c = self.veiculo_do_tipo(self.trator, 3, codigo="TR-03")            # sem chip, exige -> atenção
        d = self.veiculo_do_tipo(self.caminhao, 4, codigo="CM-04")          # em manutenção, sem chip
        e = self.veiculo_do_tipo(self.caminhao, 5, codigo="CM-05")          # desativado
        frota.mudar_status(d, "manutencao", self.usuario)
        frota.mudar_status(e, "desativado", self.usuario)
        self.chip_vinculado(1, a, "ativa")
        self.chip_vinculado(2, b, "falha")
        self.chip_vinculado(3, a, "sem_sessao")
        self.chip(4, nome="Livre")                                          # disponível
        quebrado = self.chip(5)
        vinculos.inativar(quebrado, self.usuario, "x")
        return dict(a=a, b=b, c=c, d=d, e=e)

    def test_blocos_separados_de_frota_e_conectividade(self):
        self.montar()
        ind = frota.indicadores_frota()
        self.assertEqual(
            ind["frota"],
            dict(total=4, ativos=3, em_manutencao=1, inativos=0, desativados=1, com_chip=2, sem_chip=2),
        )
        self.assertEqual(ind["frota"]["com_chip"] + ind["frota"]["sem_chip"], ind["frota"]["total"])
        self.assertEqual(
            ind["conectividade"],
            dict(
                chips_total=5, chips_disponiveis=1, chips_vinculados=3, chips_manutencao=0, chips_inativos=1,
                com_sessao_ativa=1, sem_sessao=1, com_falha=1,
            ),
        )

    def test_chip_disponivel_sem_sessao_nao_conta_como_problema(self):
        livre = self.chip(1)
        self.fixar(livre, "sem_sessao")
        ind = frota.indicadores_frota()["conectividade"]
        self.assertEqual((ind["sem_sessao"], ind["com_falha"], ind["chips_disponiveis"]), (0, 0, 1))

    def test_area_de_atencao(self):
        itens = self.montar()
        area = frota.area_de_atencao()
        self.assertEqual([v.codigo for v in area["veiculos_sem_chip"]["itens"]], ["TR-03"])
        self.assertEqual(area["veiculos_sem_chip"]["total"], 1)
        self.assertEqual([c.nome for c in area["falhas_de_comunicacao"]["itens"]], ["Chip 2"])
        self.assertEqual(area["falhas_de_comunicacao"]["itens"][0].veiculo_atual, itens["b"])
        self.assertEqual(area["chips_sem_atualizacao"]["total"], 0)
        self.assertEqual(area["conectividade_pendente"]["total"], 0)
        self.assertGreaterEqual(area["movimentacoes_recentes"]["total"], 5)
        self.assertLessEqual(len(area["movimentacoes_recentes"]["itens"]), 8)
        chip = M2MChip.objects.get(nome="Chip 3")
        self.fixar(chip, "desatualizado")
        self.fixar(M2MChip.objects.get(nome="Chip 1"), "pendente")
        area = frota.area_de_atencao()
        self.assertEqual([c.nome for c in area["chips_sem_atualizacao"]["itens"]], ["Chip 3"])
        self.assertEqual([c.nome for c in area["conectividade_pendente"]["itens"]], ["Chip 1"])
        self.assertEqual(frota.area_de_atencao(limite=1)["movimentacoes_recentes"]["itens"].__len__(), 1)

    def test_linha_do_tempo_do_veiculo_mistura_as_quatro_fontes(self):
        v = self.veiculo(1)
        frota.editar_veiculo(v, {"modelo": "X"}, self.usuario)
        chip = self.chip(1)
        vinculo = vinculos.vincular(chip, v, self.usuario, "Instalação").vinculo
        vinculos.desvincular(vinculo.pk, self.outro, "Saiu")
        M2MOcorrencia.objects.create(veiculo=v, tipo="veiculo_sem_chip", descricao="sem chip")
        linha = frota.linha_do_tempo_veiculo(v)
        self.assertEqual({i["fonte"] for i in linha}, {"evento", "vinculo", "movimentacao", "ocorrencia"})
        for item in linha:
            self.assertEqual(set(item), {"quando", "fonte", "tipo", "titulo", "detalhe", "nivel", "ref_id"})
        quando = [i["quando"] for i in linha]
        self.assertEqual(quando, sorted(quando, reverse=True))
        self.assertEqual({i["fonte"] for i in frota.linha_do_tempo_veiculo(v, ["evento"])}, {"evento"})
        self.assertEqual(len(frota.linha_do_tempo_veiculo(v, limite=2)), 2)
        self.assertTrue(any("Saiu" in i["detalhe"] for i in linha if i["fonte"] == "movimentacao"))


# ═════════════════════════════════════════════════════════════
# Alertas novos
# ═════════════════════════════════════════════════════════════

class AlertasNovosTests(FrotaBase):
    def setUp(self):
        super().setUp()
        self.mapear()

    def abertas(self, **filtro):
        return M2MOcorrencia.objects.filter(estado__in=("aberta", "em_tratamento"), **filtro)

    def derrubar(self, chip, horas=30):
        """Sessão confirmada 'sem sessão' há `horas` h."""
        self.atualizar(chip, base.roteador(sessao=base.SESSAO_DOWN), avaliar=False)
        M2MConsulta = m2m.M2MConsulta
        M2MConsulta.objects.filter(chip=chip, tipo="sessao").update(iniciada_em=timezone.now() - timedelta(hours=horas))
        chip.refresh_from_db()

    # ── sem_sessao só para chip vinculado ──
    def test_sem_sessao_nao_abre_para_chip_disponivel_mas_abre_quando_vinculado(self):
        chip = self.chip(1)
        self.derrubar(chip)
        self.assertEqual(chip.sessao_estado, "sem_sessao")
        self.assertEqual(chip.situacao_uso, "disponivel")
        self.assertEqual(m2m.avaliar_ocorrencias(chip), [])                                    # esperado: não é problema
        self.assertEqual(self.abertas(chip=chip).count(), 0)
        v = self.veiculo(1)
        vinculos.vincular(chip, v, self.usuario, "teste")
        chip.refresh_from_db()
        novas = m2m.avaliar_ocorrencias(chip)
        self.assertEqual([o.tipo for o in novas], ["sem_sessao"])
        self.assertEqual(novas[0].veiculo, v)                                                  # o veículo vai de contexto
        # desvinculou: a condição some e a ocorrência se resolve sozinha
        vinculos.desvincular(FrotaVinculo.objects.get(chip=chip, fim_em__isnull=True).pk, self.usuario, "saiu")
        chip.refresh_from_db()
        m2m.avaliar_ocorrencias(chip)
        self.assertEqual(self.abertas(chip=chip).count(), 0)

    def test_chip_manutencao_ou_inativo_sem_sessao_tambem_nao_alerta(self):
        for n, uso in ((1, "manutencao"), (2, "inativo")):
            chip = self.chip(n)
            self.derrubar(chip)
            M2MChip.objects.filter(pk=chip.pk).update(situacao_uso=uso)
            chip.refresh_from_db()
            self.assertEqual(m2m.avaliar_ocorrencias(chip), [])

    # ── regra ──
    def test_regra_desligada_nao_gera_ocorrencia(self):
        chip = self.chip(1)
        FrotaRegraAlerta.objects.filter(tipo="falha_recorrente").update(gera_ocorrencia=False)
        self.falhar(chip, 3)
        self.assertEqual(m2m.avaliar_ocorrencias(chip), [])
        self.assertEqual(self.abertas(chip=chip).count(), 0)
        FrotaRegraAlerta.objects.filter(tipo="falha_recorrente").update(gera_ocorrencia=True)
        self.assertEqual([o.tipo for o in m2m.avaliar_ocorrencias(chip)], ["falha_recorrente"])

    def test_regra_desligada_depois_de_aberta_continua_o_ciclo_normal_da_ocorrencia(self):
        chip = self.chip(1)
        self.falhar(chip, 3)
        m2m.avaliar_ocorrencias(chip, notificar=False)
        FrotaRegraAlerta.objects.filter(tipo="falha_recorrente").update(gera_ocorrencia=False)
        m2m.avaliar_ocorrencias(chip, notificar=False)
        self.assertEqual(self.abertas(chip=chip).get().deteccoes, 2)                          # ainda acompanha...
        self.atualizar(chip, base.roteador(), avaliar=False)
        m2m.avaliar_ocorrencias(chip, notificar=False)
        self.assertEqual(self.abertas(chip=chip).count(), 0)                                   # ...e se resolve sozinha

    def test_o_nivel_vem_da_regra_e_o_consumo_acima_de_100_sobe_para_critico(self):
        FrotaRegraAlerta.objects.filter(tipo="falha_recorrente").update(nivel="critico")
        chip = self.chip(1)
        self.falhar(chip, 3)
        self.assertEqual(m2m.avaliar_ocorrencias(chip)[0].nivel, "critico")
        FrotaRegraAlerta.objects.filter(tipo="consumo_elevado").update(nivel="info")
        m2m.editar_chip(self.chip(2), {"limite_mensal_bytes": 100_000_000}, self.usuario)
        c2 = M2MChip.objects.get(nome="Chip 2")
        self.atualizar(c2, base.roteador(uso={"uso": {"total": 90, "inicio": "2026-11-01", "fim": "2026-11-30"}}), avaliar=False)
        self.assertEqual(m2m.avaliar_ocorrencias(c2)[0].nivel, "info")                         # 90% < 100%: o da regra
        self.atualizar(c2, base.roteador(uso={"uso": {"total": 120, "inicio": "2026-11-01", "fim": "2026-11-30"}}), avaliar=False)
        m2m.avaliar_ocorrencias(c2)
        self.assertEqual(self.abertas(chip=c2, tipo="consumo_elevado").get().nivel, "critico")

    def test_toda_regra_tem_uma_linha_e_a_apagada_e_recriada_com_o_padrao(self):
        self.assertEqual(set(FrotaRegraAlerta.objects.values_list("tipo", flat=True)), set(M2MOcorrencia.Tipo.values))
        FrotaRegraAlerta.objects.filter(tipo="veiculo_sem_chip").delete()
        regra = m2m.regra_do_tipo("veiculo_sem_chip")
        self.assertEqual((regra.gera_ocorrencia, regra.nivel, regra.envia_email), (True, "atencao", True))
        self.assertEqual(set(REGRAS_DE_ALERTA_PADRAO), set(M2MOcorrencia.Tipo.values))

    # ── veiculo_sem_chip ──
    def test_veiculo_sem_chip_exige_ativo_exige_conectividade_e_nenhum_vinculo(self):
        FrotaTipo.objects.filter(pk=self.trator.pk).update(exige_conectividade=True)
        v = self.veiculo_do_tipo(self.trator, 1, codigo="TR-01")
        livre = self.veiculo_do_tipo(self.caminhao, 2, codigo="CM-02")                         # tipo não exige
        sobreposto = self.veiculo_do_tipo(self.caminhao, 3, codigo="CM-03", exige_conectividade=True)
        manut = self.veiculo_do_tipo(self.trator, 4, codigo="TR-04")
        frota.mudar_status(manut, "manutencao", self.usuario)
        isento = self.veiculo_do_tipo(self.trator, 5, codigo="TR-05", exige_conectividade=False)
        novas = m2m.avaliar_ocorrencias_de_veiculos(notificar=False)
        self.assertCountEqual([o.veiculo.codigo for o in novas], ["TR-01", "CM-03"])
        for o in novas:
            self.assertEqual((o.tipo, o.chip_id, o.nivel), ("veiculo_sem_chip", None, "atencao"))
            self.assertIn("exige conectividade e não tem nenhum chip vinculado", o.descricao)
        # reavaliar não duplica (a deduplicação por veículo)
        self.assertEqual(m2m.avaliar_ocorrencias_de_veiculos(notificar=False), [])
        self.assertEqual(self.abertas(tipo="veiculo_sem_chip").count(), 2)
        self.assertEqual(self.abertas(tipo="veiculo_sem_chip").get(veiculo=v).deteccoes, 2)
        # vinculou um chip: resolve sozinha
        vinculos.vincular(self.chip(1), v, self.usuario, "teste")
        m2m.avaliar_ocorrencias_de_veiculos(notificar=False)
        self.assertEqual([o.veiculo.codigo for o in self.abertas(tipo="veiculo_sem_chip")], ["CM-03"])
        resolvida = M2MOcorrencia.objects.get(veiculo=v, tipo="veiculo_sem_chip")
        self.assertEqual(resolvida.estado, "resolvida")

    def test_veiculo_sem_chip_respeita_a_regra_desligada(self):
        FrotaTipo.objects.filter(pk=self.trator.pk).update(exige_conectividade=True)
        self.veiculo_do_tipo(self.trator, 1)
        FrotaRegraAlerta.objects.filter(tipo="veiculo_sem_chip").update(gera_ocorrencia=False)
        self.assertEqual(m2m.avaliar_ocorrencias_de_veiculos(notificar=False), [])
        self.assertEqual(M2MOcorrencia.objects.count(), 0)

    def test_a_restricao_do_banco_deduplica_ocorrencia_de_veiculo(self):
        v = self.veiculo(1)
        M2MOcorrencia.objects.create(veiculo=v, tipo="veiculo_sem_chip", descricao="a")
        with self.assertRaises(IntegrityError), transaction.atomic():
            M2MOcorrencia.objects.create(veiculo=v, tipo="veiculo_sem_chip", descricao="b")
        with self.assertRaises(IntegrityError), transaction.atomic():
            M2MOcorrencia.objects.create(tipo="veiculo_sem_chip", descricao="sem alvo nenhum")   # nem chip nem veículo

    # ── veiculo_sem_atualizacao ──
    def test_veiculo_sem_atualizacao_quando_nenhum_chip_tem_consulta_valida_no_prazo(self):
        v = self.veiculo(1)
        c1 = self.chip_vinculado(1, v, "ativa")
        c2 = self.chip_vinculado(2, v, "ativa")
        # recém-vinculados: dentro do prazo (não se cobra dado de antes do vínculo)
        self.fixar(c1, "desatualizado"), self.fixar(c2, "desatualizado")
        self.assertEqual(m2m.avaliar_ocorrencias_de_veiculos(notificar=False), [])
        FrotaVinculo.objects.filter(veiculo=v).update(inicio_em=timezone.now() - timedelta(hours=200))
        novas = m2m.avaliar_ocorrencias_de_veiculos(notificar=False)
        self.assertEqual([o.tipo for o in novas], ["veiculo_sem_atualizacao"])
        self.assertEqual((novas[0].veiculo, novas[0].chip_id), (v, None))
        self.assertIn("Nenhum dos 2 chip(s)", novas[0].descricao)
        # UM chip com consulta válida recente já tira o alerta
        self.fixar(c1, "ativa")
        m2m.avaliar_ocorrencias_de_veiculos(notificar=False)
        self.assertEqual(self.abertas(tipo="veiculo_sem_atualizacao").count(), 0)

    def test_veiculo_sem_atualizacao_ignora_chip_sem_integracao_e_veiculo_fora_de_operacao(self):
        v = self.veiculo(1)
        vivo = self.sem_integracao()
        chip = self.chip(1, operadora=vivo.pk)
        vinculos.vincular(chip, v, self.usuario, "teste")
        FrotaVinculo.objects.update(inicio_em=timezone.now() - timedelta(hours=200))
        self.assertEqual(m2m.avaliar_ocorrencias_de_veiculos(notificar=False), [])             # não há consulta a esperar
        w = self.veiculo(2)
        c = self.chip_vinculado(2, w, "desatualizado")
        FrotaVinculo.objects.filter(chip=c).update(inicio_em=timezone.now() - timedelta(hours=200))
        frota.mudar_status(w, "manutencao", self.usuario)
        self.assertEqual(m2m.avaliar_ocorrencias_de_veiculos(notificar=False), [])

    # ── operadora sem integração ──
    def test_chip_de_operadora_sem_integracao_nunca_e_consultado_nem_alerta(self):
        vivo = self.sem_integracao()
        chip = self.chip(1, operadora=vivo.pk)
        with self.assertRaises(m2m.AtualizacaoRecusada) as cm:
            m2m.atualizar(chip, origem="manual", usuario=self.usuario)
        self.assertEqual(cm.exception.codigo, "sem_integracao")
        self.assertEqual(chip.situacao_monitoramento, "sem_integracao")
        M2MChip.objects.filter(pk=chip.pk).update(falhas_consecutivas=9, ultima_tentativa_em=timezone.now())
        chip.refresh_from_db()
        self.assertEqual(m2m.avaliar_ocorrencias(chip), [])
        # o ciclo consulta só quem tem integração
        tim_chip = self.chip(2)
        M2MChip.objects.update(proxima_consulta_em=None)
        sessao = base.Sessao(base.roteador())
        with mock.patch("services.tim_m2m_service.requests.Session", return_value=sessao):
            resumo = m2m.executar_ciclo()
        self.assertEqual(resumo.consultados, 1)
        self.assertTrue(all(tim_chip.iccid in url for url in sessao.urls))
        self.assertEqual(m2m.contar_monitorados(), 1)
        self.assertEqual(m2m.indicadores()["sem_integracao"], 1)

    def test_o_ciclo_avalia_as_ocorrencias_de_veiculo(self):
        FrotaTipo.objects.filter(pk=self.trator.pk).update(exige_conectividade=True)
        self.veiculo_do_tipo(self.trator, 1)
        with mock.patch("services.tim_m2m_service.requests.Session", return_value=base.Sessao(base.roteador())):
            resumo = m2m.executar_ciclo()
        self.assertEqual(resumo.novas_ocorrencias, 1)
        self.assertEqual(self.abertas(tipo="veiculo_sem_chip").count(), 1)

    # ── o texto ──
    def test_o_texto_dos_alertas_nunca_diz_fora_de_cobertura_nem_defeito(self):
        FrotaTipo.objects.filter(pk=self.trator.pk).update(exige_conectividade=True)
        sem_chip = self.veiculo_do_tipo(self.trator, 1)
        v = self.veiculo(2)
        a, b, c = self.chip_vinculado(1, v, "falha"), self.chip(2), self.chip(3)
        FrotaRegraAlerta.objects.filter(tipo__in=("chip_desvinculado", "alteracao_vinculo")).update(gera_ocorrencia=True)
        self.derrubar(b)
        vinculos.vincular(b, v, self.usuario, "teste")
        b.refresh_from_db()
        self.falhar(a, 3)
        m2m.avaliar_ocorrencias(a, notificar=False)
        m2m.avaliar_ocorrencias(b, notificar=False)
        m2m.avaliar_ocorrencias_de_veiculos(notificar=False)
        vinculos.desvincular(FrotaVinculo.objects.get(chip=b, fim_em__isnull=True).pk, self.usuario, "saiu")
        textos = [o.descricao for o in M2MOcorrencia.objects.all()] + list(m2m.ACAO_NECESSARIA.values())
        self.assertGreaterEqual(len(textos), 9)
        for texto in textos:
            for proibida in ("fora de cobertura", "defeito", "Defeito"):
                self.assertNotIn(proibida, texto)
        self.assertEqual(set(m2m.ACAO_NECESSARIA), set(M2MOcorrencia.Tipo.values))            # todo tipo tem ação necessária


class OcorrenciaDeEventoTests(FrotaBase):
    def test_as_regras_de_evento_nascem_desligadas(self):
        for tipo in ("chip_desvinculado", "alteracao_vinculo"):
            regra = FrotaRegraAlerta.objects.get(tipo=tipo)
            self.assertEqual((regra.gera_ocorrencia, regra.nivel, regra.envia_email), (False, "info", False))
        chip, v = self.chip(1), self.veiculo(1)
        vinculo = vinculos.vincular(chip, v, self.usuario, "teste").vinculo
        vinculos.desvincular(vinculo.pk, self.usuario, "saiu")
        self.assertEqual(M2MOcorrencia.objects.count(), 0)

    def test_com_a_regra_ligada_desvincular_e_transferir_abrem_ocorrencia_informativa(self):
        FrotaRegraAlerta.objects.filter(tipo__in=("chip_desvinculado", "alteracao_vinculo")).update(gera_ocorrencia=True)
        v1, v2 = self.veiculo(1), self.veiculo(2)
        chip = self.chip(1, nome="Pivô 9")
        vinculo = vinculos.vincular(chip, v1, self.usuario, "teste").vinculo
        vinculo2 = vinculos.transferir(vinculo.pk, v2, self.outro, "Mudou de máquina").vinculo
        alteracao = M2MOcorrencia.objects.filter(chip=chip, tipo="alteracao_vinculo").get()
        self.assertEqual((alteracao.nivel, alteracao.estado, alteracao.veiculo), ("info", "aberta", v2))
        self.assertEqual(alteracao.deteccoes, 2)                                               # vincular + transferir: uma só, atualizada
        self.assertIn("transferido do veículo", alteracao.descricao)
        self.assertIn("outro_usuario", alteracao.descricao)
        vinculos.desvincular(vinculo2.pk, self.usuario, "Veículo vendido")
        desvinculada = M2MOcorrencia.objects.filter(chip=chip, tipo="chip_desvinculado").get()
        self.assertEqual((desvinculada.nivel, desvinculada.veiculo), ("info", v2))
        self.assertIn("Veículo vendido", desvinculada.descricao)
        self.assertEqual(desvinculada.tratamentos.get().acao, "detectada")
        # evento não se resolve sozinho quando o ciclo avalia o chip
        m2m.avaliar_ocorrencias(chip, notificar=False)
        desvinculada.refresh_from_db()
        self.assertEqual(desvinculada.estado, "aberta")

    def test_falha_ao_abrir_a_ocorrencia_nao_desfaz_a_movimentacao(self):
        FrotaRegraAlerta.objects.filter(tipo="alteracao_vinculo").update(gera_ocorrencia=True)
        chip, v = self.chip(1), self.veiculo(1)
        with mock.patch.object(m2m, "abrir_ocorrencia_de_evento", side_effect=RuntimeError("alerta quebrou")):
            vinculos.vincular(chip, v, self.usuario, "teste")
        chip.refresh_from_db()
        self.assertEqual(chip.situacao_uso, "vinculado")
        self.assertEqual(FrotaMovimentacao.objects.filter(tipo="vinculacao").count(), 1)

    def test_abrir_ocorrencia_de_evento_recusa_tipo_de_condicao(self):
        with self.assertRaises(ValueError):
            m2m.abrir_ocorrencia_de_evento("sem_sessao", self.chip(1), None, "x")

    def test_o_email_do_evento_sai_por_on_commit_para_os_destinatarios_da_regra(self):
        email_alertas.sincronizar_catalogo_notificacoes()
        FrotaRegraAlerta.objects.filter(tipo="chip_desvinculado").update(
            gera_ocorrencia=True, envia_email=True, destinatarios_emails="frota@teste.invalid",
        )
        chip, v = self.chip(1), self.veiculo(1)
        vinculo = vinculos.vincular(chip, v, self.usuario, "teste").vinculo
        mail.outbox = []
        with self.captureOnCommitCallbacks(execute=True):
            vinculos.desvincular(vinculo.pk, self.usuario, "saiu")
        mensagens = [m for m in mail.outbox if "Frotas e Conectividade" in m.subject]
        self.assertEqual(len(mensagens), 1)
        self.assertEqual(mensagens[0].to, ["frota@teste.invalid"])
        self.assertIn(v.codigo, mensagens[0].body)
        self.assertIn("Conferir se a desvinculação foi intencional", mensagens[0].body)       # ação necessária


class EmailDasRegrasTests(FrotaBase):
    def setUp(self):
        super().setUp()
        email_alertas.sincronizar_catalogo_notificacoes()
        mail.outbox = []

    def emails(self):
        return [m for m in mail.outbox if "Frotas e Conectividade" in m.subject]

    def ocorrencia(self, chip, tipo="falha_recorrente", **extra):
        dados = dict(chip=chip, tipo=tipo, nivel="atencao", descricao="Descrição da detecção")
        dados.update(extra)
        return M2MOcorrencia.objects.create(**dados)

    def enviar(self, *ocorrencias):
        with self.captureOnCommitCallbacks(execute=True):
            m2m.notificar_ocorrencias(list(ocorrencias))

    def test_o_email_traz_veiculo_chip_ocorrencia_data_e_acao_necessaria(self):
        v = self.veiculo(1, codigo="TR-77", nome="Trator Esmeralda")
        chip = self.chip(1, nome="Chip do Trator")
        FrotaRegraAlerta.objects.filter(tipo="falha_recorrente").update(envia_email=True, destinatarios_emails="a@teste.invalid")
        oc = self.ocorrencia(chip, veiculo=v)
        self.enviar(oc)
        (msg,) = self.emails()
        for esperado in ("TR-77", "Trator Esmeralda", "Chip do Trator", "Falhas de consulta seguidas", "Descrição da detecção",
                         "Conferir a credencial e a conexão com a API da operadora"):
            self.assertIn(esperado, msg.body)
        self.assertIn(timezone.localtime(oc.detectada_em).strftime("%d/%m/%Y %H:%M"), msg.body)
        for proibida in ("fora de cobertura", "defeito"):
            self.assertNotIn(proibida, msg.body.lower())

    def test_ocorrencia_de_veiculo_sem_chip_sai_so_com_o_veiculo(self):
        v = self.veiculo(1, codigo="TR-77")
        FrotaRegraAlerta.objects.filter(tipo="veiculo_sem_chip").update(envia_email=True, destinatarios_emails="a@teste.invalid")
        self.enviar(M2MOcorrencia.objects.create(veiculo=v, tipo="veiculo_sem_chip", nivel="atencao", descricao="sem chip"))
        (msg,) = self.emails()
        self.assertIn("TR-77", msg.body)
        self.assertIn("Vincular um chip disponível ao veículo", msg.body)

    def test_destinatarios_emails_e_usuarios_da_regra_sem_repeticao(self):
        chip = self.chip(1)
        ana = User.objects.create(username="ana", email="ana@teste.invalid")
        inativo = User.objects.create(username="inativo", email="inativo@teste.invalid", is_active=False)
        sem_email = User.objects.create(username="sem_email")
        regra = FrotaRegraAlerta.objects.get(tipo="falha_recorrente")
        regra.envia_email = True
        regra.destinatarios_emails = "b@teste.invalid; C@teste.invalid,\nb@teste.invalid  invalido  ana@teste.invalid"
        regra.save()
        regra.destinatarios_usuarios.set([ana, inativo, sem_email])
        self.assertEqual(m2m.destinatarios_da_regra(regra), ["b@teste.invalid", "C@teste.invalid", "ana@teste.invalid"])
        self.enviar(self.ocorrencia(chip))
        (msg,) = self.emails()
        self.assertEqual(msg.to, ["b@teste.invalid", "C@teste.invalid", "ana@teste.invalid"])

    def test_regras_com_destinatarios_diferentes_saem_em_emails_separados(self):
        chip = self.chip(1)
        FrotaRegraAlerta.objects.filter(tipo="falha_recorrente").update(envia_email=True, destinatarios_emails="a@teste.invalid")
        FrotaRegraAlerta.objects.filter(tipo="desatualizado").update(envia_email=True, destinatarios_emails="b@teste.invalid")
        FrotaRegraAlerta.objects.filter(tipo="sem_sessao").update(envia_email=True, destinatarios_emails="a@teste.invalid")
        self.enviar(self.ocorrencia(chip), self.ocorrencia(chip, "desatualizado"), self.ocorrencia(chip, "sem_sessao"))
        self.assertEqual(sorted(tuple(m.to) for m in self.emails()), [("a@teste.invalid",), ("b@teste.invalid",)])
        da = next(m for m in self.emails() if m.to == ["a@teste.invalid"])
        self.assertIn("Falhas de consulta seguidas", da.body)
        self.assertIn("Sem sessão", da.body)
        self.assertNotIn("Dados desatualizados", da.body)

    def test_regra_sem_destinatario_cai_na_lista_padrao_do_sistema(self):
        chip = self.chip(1)
        FrotaRegraAlerta.objects.filter(tipo="falha_recorrente").update(envia_email=True, destinatarios_emails="")
        self.enviar(self.ocorrencia(chip))
        (msg,) = self.emails()
        self.assertEqual(msg.to, email_alertas.DESTINATARIOS)

    def test_canal_desativado_e_chave_mestra_continuam_mandando(self):
        chip = self.chip(1)
        FrotaRegraAlerta.objects.filter(tipo="falha_recorrente").update(envia_email=True, destinatarios_emails="a@teste.invalid")
        CanalNotificacao.objects.filter(codigo="m2m_ocorrencias").update(ativo=False)
        self.enviar(self.ocorrencia(chip))
        self.assertEqual(self.emails(), [])
        CanalNotificacao.objects.filter(codigo="m2m_ocorrencias").update(ativo=True)
        cfg = ConfiguracaoSistema.get()
        cfg.alertas_email_ativos = False
        cfg.save()
        self.enviar(self.ocorrencia(chip, "desatualizado"))
        self.assertEqual(self.emails(), [])
        cfg.alertas_email_ativos = True
        cfg.save()
        FrotaRegraAlerta.objects.filter(tipo="consumo_elevado").update(envia_email=False)
        self.enviar(self.ocorrencia(chip, "consumo_elevado"))                                   # essa regra: e-mail desligado
        self.assertEqual(self.emails(), [])
        FrotaRegraAlerta.objects.filter(tipo="consumo_elevado").update(envia_email=True, destinatarios_emails="a@teste.invalid")
        self.enviar(self.ocorrencia(chip, "identificador_divergente"))                          # tudo ligado: sai
        self.assertEqual(len(self.emails()), 1)

    def test_o_painel_de_notificacoes_lista_os_destinatarios_das_regras(self):
        FrotaRegraAlerta.objects.filter(tipo="falha_recorrente").update(envia_email=True, destinatarios_emails="a@teste.invalid")
        canal = CanalNotificacao.objects.get(codigo="m2m_ocorrencias")
        pessoas, nota = email_alertas.resolver_destinatarios_atuais(canal)
        self.assertIn("a@teste.invalid", [p["email"] for p in pessoas])
        self.assertFalse(any(p["removivel"] for p in pessoas))
        self.assertIn("Regras de alerta", nota)


# ═════════════════════════════════════════════════════════════
# Separação do estoque de TI
# ═════════════════════════════════════════════════════════════

class SeparacaoDoEstoqueDeTITests(FrotaBase):
    # As ÚNICAS pontes para o estoque de TI. Regra 62: o vínculo de chip pode apontar para o tablet/celular
    # onde o chip está, e a movimentação do chip lembra de onde veio e para onde foi. Regra 66: o chip aponta
    # para o seu PATRIMÔNIO no estoque (`item_ti`, pelo ICCID). Todas são só REFERÊNCIA — nenhuma operação
    # grava no `Item` existente (`test_frota_equipamento_ti` e `test_chip_estoque_ti`). Qualquer outra FK para
    # `Item` nos models do módulo é acoplamento novo e tem de derrubar este teste.
    PONTES_PERMITIDAS = {
        ("FrotaVinculo", "item"),
        ("FrotaMovimentacao", "item_origem"),
        ("FrotaMovimentacao", "item_destino"),
        ("M2MChip", "item_ti"),
    }

    def test_so_as_pontes_do_vinculo_apontam_para_item(self):
        prefixos = ("Frota", "M2M", "Conectividade")
        modelos = [m for m in django_apps.get_app_config("ProjetoEstoque").get_models() if m.__name__.startswith(prefixos)]
        self.assertGreaterEqual(len(modelos), 14)
        achadas = set()
        for modelo in modelos:
            for campo in modelo._meta.get_fields():
                if campo.is_relation and campo.concrete and campo.related_model is Item:
                    achadas.add((modelo.__name__, campo.name))
        self.assertEqual(achadas, self.PONTES_PERMITIDAS, "FK nova para Item no módulo de Frotas e Conectividade")
        self.assertFalse(hasattr(M2MChip, "item"))                                  # o FK DIRETO antigo (0190) não voltou; a ponte de hoje é `item_ti`

    def test_o_ciclo_completo_da_frota_nao_toca_no_estoque_de_ti(self):
        contagens = lambda: (Item.objects.count(), MovimentacaoItem.objects.count(), ItemStatusHistorico.objects.count())
        antes = contagens()
        v1 = self.veiculo(1, localidade=Localidade.objects.create(local="Fazenda Sul").pk)
        v2 = self.veiculo(2)
        chip = m2m.criar_chip({"nome": "A", "iccid": base.iccid_valido(1), "veiculo": v1.pk}, self.usuario)
        outro = self.chip(2)
        vinculo = FrotaVinculo.objects.get(chip=chip, fim_em__isnull=True)
        r = vinculos.transferir(vinculo.pk, v2, self.usuario, "teste")
        vinculos.substituir(r.vinculo.pk, outro, self.usuario, "teste")
        vinculos.desvincular(FrotaVinculo.objects.get(chip=outro, fim_em__isnull=True).pk, self.usuario, "teste")
        vinculos.inativar(chip, self.usuario, "teste")
        frota.mudar_status(v1, "manutencao", self.usuario)
        self.assertGreaterEqual(FrotaMovimentacao.objects.count(), 8)
        self.assertEqual(contagens(), antes)


# ═════════════════════════════════════════════════════════════
# Permissões e a migration 0191
# ═════════════════════════════════════════════════════════════

MIGRACAO = importlib.import_module("ProjetoEstoque.migrations.0191_frota_dados_permissoes")


class MigracaoDePermissoesTests(TestCase):
    """A 0191 roda no banco de teste ANTES dos testes (já criou os grupos e as permissões novas
    e apagou as antigas). Aqui o estado ANTIGO é recriado à mão e a função da migration roda de novo
    — o mesmo código, sobre dados que ela precisa mapear."""

    VER = set(MIGRACAO.VER)
    OPERAR_EXTRA = set(MIGRACAO.OPERAR_EXTRA)

    def antigas(self):
        tipo = Permission.objects.get(codename="ver_chips").content_type
        return {
            codigo: Permission.objects.get_or_create(content_type=tipo, codename=codigo, defaults={"name": nome})[0]
            for codigo, nome in MIGRACAO.ANTIGAS
        }

    def codigos(self, user):
        return set(User.objects.get(pk=user.pk).user_permissions.values_list("codename", flat=True))

    def cenario(self):
        antigas = self.antigas()
        u_ver = User.objects.create(username="m_ver")
        u_ver.user_permissions.add(antigas["ver_m2m"])
        u_op = User.objects.create(username="m_operar")
        u_op.user_permissions.add(antigas["ver_m2m"], antigas["operar_m2m"])
        u_so_op = User.objects.create(username="m_so_operar")
        u_so_op.user_permissions.add(antigas["operar_m2m"])
        u_cfg = User.objects.create(username="m_config")
        u_cfg.user_permissions.add(antigas["configurar_m2m"])
        u_nada = User.objects.create(username="m_nada")
        return antigas, dict(ver=u_ver, op=u_op, so_op=u_so_op, cfg=u_cfg, nada=u_nada)

    def test_o_mapeamento_das_permissoes_antigas_para_as_novas_nos_usuarios(self):
        _, u = self.cenario()
        MIGRACAO.migrar_permissoes(django_apps, None)
        self.assertEqual(self.codigos(u["ver"]), self.VER)
        self.assertEqual(self.codigos(u["op"]), self.VER | self.OPERAR_EXTRA)
        self.assertEqual(self.codigos(u["so_op"]), self.VER | self.OPERAR_EXTRA)               # `operar` carrega as de `ver`
        self.assertEqual(self.codigos(u["cfg"]), {"administrar_frota_config"})
        self.assertEqual(self.codigos(u["nada"]), set())

    def test_as_tres_antigas_sao_apagadas_e_as_dez_novas_existem(self):
        self.cenario()
        MIGRACAO.migrar_permissoes(django_apps, None)
        self.assertFalse(Permission.objects.filter(codename__in=["ver_m2m", "operar_m2m", "configurar_m2m"]).exists())
        novas = set(Permission.objects.filter(content_type__app_label="ProjetoEstoque").values_list("codename", flat=True))
        self.assertTrue({c for _, c, _ in MIGRACAO.NOVAS} <= novas)
        self.assertEqual(len(MIGRACAO.NOVAS), 10)

    def test_grupo_antigo_e_renomeado_e_leva_os_membros_e_as_permissoes(self):
        antigas = self.antigas()
        Group.objects.filter(name__in=[GRUPO_FROTAS, GRUPO_FROTAS_CONSULTA]).delete()
        antigo = Group.objects.create(name="Monitoramento M2M")
        antigo.permissions.add(antigas["ver_m2m"], antigas["operar_m2m"])
        membro = User.objects.create(username="m_membro")
        membro.groups.add(antigo)
        pk = antigo.pk
        MIGRACAO.migrar_permissoes(django_apps, None)
        self.assertFalse(Group.objects.filter(name="Monitoramento M2M").exists())
        grupo = Group.objects.get(name=GRUPO_FROTAS)
        self.assertEqual(grupo.pk, pk)                                                         # renomeado, não recriado
        self.assertEqual(set(grupo.permissions.values_list("codename", flat=True)), self.VER | self.OPERAR_EXTRA)
        self.assertEqual(list(grupo.user_set.values_list("username", flat=True)), ["m_membro"])
        consulta = Group.objects.get(name=GRUPO_FROTAS_CONSULTA)
        self.assertEqual(set(consulta.permissions.values_list("codename", flat=True)), self.VER)
        self.assertNotIn("administrar_frota_config", set(grupo.permissions.values_list("codename", flat=True)))

    def test_com_os_dois_grupos_existindo_junta_os_membros(self):
        antigas = self.antigas()
        antigo = Group.objects.create(name="Monitoramento M2M")
        antigo.permissions.add(antigas["ver_m2m"])
        membro = User.objects.create(username="m_membro2")
        membro.groups.add(antigo)
        MIGRACAO.migrar_permissoes(django_apps, None)
        self.assertFalse(Group.objects.filter(name="Monitoramento M2M").exists())
        self.assertIn("m_membro2", Group.objects.get(name=GRUPO_FROTAS).user_set.values_list("username", flat=True))

    def test_idempotente(self):
        self.cenario()
        MIGRACAO.migrar_permissoes(django_apps, None)
        MIGRACAO.migrar_permissoes(django_apps, None)
        self.assertEqual(Group.objects.filter(name=GRUPO_FROTAS).count(), 1)
        MIGRACAO.semear_tipos(django_apps, None)
        MIGRACAO.semear_regras(django_apps, None)
        self.assertEqual(FrotaTipo.objects.count(), 7)
        self.assertEqual(FrotaRegraAlerta.objects.count(), 9)

    def test_o_reverso_recria_as_antigas_e_mapeia_de_volta(self):
        MIGRACAO.migrar_permissoes(django_apps, None)
        grupo = Group.objects.get(name=GRUPO_FROTAS)
        # O banco de teste já passou pela 0198, que acrescentou as 3 permissões do inventário a este
        # grupo. A 0191 não as conhece, então o cenário volta ao estado DELA: o que se testa aqui é o
        # reverso da 0191 sobre o que a 0191 criou.
        grupo.permissions.remove(*Permission.objects.filter(codename__in=[c for c, _ in PERMISSOES_FROTA_INVENTARIO]))
        membro = User.objects.create(username="r_membro")
        membro.groups.add(grupo)
        consulta_membro = User.objects.create(username="r_consulta")
        consulta_membro.groups.add(Group.objects.get(name=GRUPO_FROTAS_CONSULTA))
        so_vincula = User.objects.create(username="r_vincula")
        so_vincula.user_permissions.add(Permission.objects.get(codename="vincular_chips"))
        admin = User.objects.create(username="r_admin")
        admin.user_permissions.add(Permission.objects.get(codename="administrar_frota_config"))
        MIGRACAO.desfazer_permissoes(django_apps, None)
        antigas = {p.codename: p for p in Permission.objects.filter(codename__in=["ver_m2m", "operar_m2m", "configurar_m2m"])}
        self.assertEqual(set(antigas), {"ver_m2m", "operar_m2m", "configurar_m2m"})
        self.assertFalse(Permission.objects.filter(codename__in=[c for _, c, _ in MIGRACAO.NOVAS]).exists())
        self.assertEqual(self.codigos(so_vincula), {"operar_m2m"})
        self.assertEqual(self.codigos(admin), {"configurar_m2m"})
        self.assertEqual(self.codigos(consulta_membro), {"ver_m2m"})                          # não ficou sem nada
        self.assertFalse(Group.objects.filter(name=GRUPO_FROTAS_CONSULTA).exists())
        self.assertEqual(set(Group.objects.get(name="Monitoramento M2M").permissions.values_list("codename", flat=True)), {"ver_m2m", "operar_m2m"})
        self.assertFalse(Group.objects.filter(name=GRUPO_FROTAS).exists())

    def test_a_migration_e_o_models_nao_divergem(self):
        no_model = {c for c, _ in PERMISSOES_FROTA_VEICULO} | {c for c, _ in PERMISSOES_FROTA_CHIP}
        self.assertEqual(no_model, {c for _, c, _ in MIGRACAO.NOVAS})
        self.assertEqual({n for _, n in PERMISSOES_FROTA_CHIP} | {n for _, n in PERMISSOES_FROTA_VEICULO}, {n for _, _, n in MIGRACAO.NOVAS})
        self.assertEqual(GRUPO_FROTAS, MIGRACAO.GRUPO_FROTAS)
        self.assertEqual(GRUPO_FROTAS_CONSULTA, MIGRACAO.GRUPO_FROTAS_CONSULTA)
        self.assertEqual(set(MIGRACAO.REGRAS), set(REGRAS_DE_ALERTA_PADRAO))
        for tipo, (gera, nivel, email) in MIGRACAO.REGRAS.items():
            self.assertEqual(REGRAS_DE_ALERTA_PADRAO[tipo], dict(gera_ocorrencia=gera, nivel=nivel, envia_email=email))

    def test_dados_semeados_pela_migration(self):
        self.assertEqual(
            set(FrotaTipo.objects.values_list("nome", flat=True)),
            {"Trator", "Caminhão", "Pulverizador", "Colheitadeira", "Plantadeira", "Veículo utilitário", "Outros"},
        )
        self.assertFalse(FrotaTipo.objects.filter(exige_conectividade=True).exists())
        tim = ConectividadeOperadora.objects.get(nome="TIM")
        self.assertEqual((tim.integracao, tim.ativo), ("tim_m2m", True))


# ═════════════════════════════════════════════════════════════
# Cadastros de apoio (tipos, operadoras, regras)
# ═════════════════════════════════════════════════════════════

class CadastrosDeApoioTests(FrotaBase):
    def test_tipo_de_veiculo_cria_edita_audita_e_nome_e_unico_sem_distinguir_caixa(self):
        tipo = frota.salvar_tipo({"nome": "Colhedora de Café", "exige_conectividade": "sim"}, self.usuario)
        self.assertEqual((tipo.nome, tipo.exige_conectividade, tipo.ativo), ("Colhedora de Café", True, True))
        evento = M2MEvento.objects.filter(tipo="config_frota").get()
        self.assertEqual((evento.dados["alvo"], evento.dados["criado"]), ("tipo_veiculo", True))
        with self.assertRaises(ValidationError) as cm:
            frota.salvar_tipo({"nome": "colhedora de café"}, self.usuario)
        self.assertIn("nome", cm.exception.message_dict)
        frota.salvar_tipo({"exige_conectividade": False, "ativo": False}, self.usuario, tipo)
        tipo.refresh_from_db()
        self.assertEqual((tipo.exige_conectividade, tipo.ativo), (False, False))
        edicao = M2MEvento.objects.filter(tipo="config_frota").order_by("-pk").first()
        self.assertEqual(edicao.dados["alteracoes"]["ativo"], {"de": True, "para": False})
        antes = M2MEvento.objects.count()
        frota.salvar_tipo({"nome": "Colhedora de Café"}, self.usuario, tipo)                  # nada mudou: nada auditado
        self.assertEqual(M2MEvento.objects.count(), antes)
        with self.assertRaises(ValidationError):
            frota.salvar_tipo({"nome": "  "}, self.usuario)
        self.assertIn(tipo.pk, [t.pk for t in frota.listar_tipos()])
        self.assertNotIn(tipo.pk, [t.pk for t in frota.listar_tipos(somente_ativos=True)])

    def test_operadora_com_e_sem_integracao(self):
        vivo = frota.salvar_operadora({"nome": "Vivo"}, self.usuario)
        self.assertEqual((vivo.integracao, vivo.ativo, vivo.tem_integracao), ("", True, False))
        with self.assertRaises(ValidationError) as cm:
            frota.salvar_operadora({"nome": "Claro", "integracao": "inventada"}, self.usuario)
        self.assertIn("integracao", cm.exception.message_dict)
        with self.assertRaises(ValidationError):
            frota.salvar_operadora({"nome": "tim"}, self.usuario)                              # duplicada (caixa)
        frota.salvar_operadora({"integracao": "tim_m2m"}, self.usuario, vivo)
        vivo.refresh_from_db()
        self.assertTrue(vivo.tem_integracao)
        self.assertEqual(frota.listar_operadoras().get(pk=vivo.pk).n_chips, 0)

    def test_regra_de_alerta_valida_audita_e_aceita_usuarios(self):
        ana = User.objects.create(username="ana2", email="ana2@teste.invalid")
        regra = frota.salvar_regra_alerta(
            "sem_sessao",
            {"gera_ocorrencia": "sim", "nivel": "critico", "envia_email": "on", "destinatarios_emails": "a@b.invalid; c@d.invalid",
             "destinatarios_usuarios": [str(ana.pk)]},
            self.usuario,
        )
        regra.refresh_from_db()
        self.assertEqual((regra.nivel, regra.envia_email, regra.destinatarios_emails), ("critico", True, "a@b.invalid, c@d.invalid"))
        self.assertEqual(list(regra.destinatarios_usuarios.all()), [ana])
        evento = M2MEvento.objects.filter(tipo="config_frota").order_by("-pk").first()
        self.assertEqual(evento.dados["alvo"], "regra_alerta")
        self.assertEqual(evento.dados["alteracoes"]["nivel"], {"de": "atencao", "para": "critico"})
        with self.assertRaises(ValidationError) as cm:
            frota.salvar_regra_alerta("sem_sessao", {"destinatarios_emails": "ok@b.invalid, quebrado"}, self.usuario)
        self.assertIn("quebrado", cm.exception.message_dict["destinatarios_emails"][0])
        for ruim in ({"nivel": "urgente"}, {"destinatarios_usuarios": ["x"]}, {"destinatarios_usuarios": [99999]}):
            with self.subTest(dados=ruim), self.assertRaises(ValidationError):
                frota.salvar_regra_alerta("sem_sessao", ruim, self.usuario)
        with self.assertRaises(ValidationError):
            frota.salvar_regra_alerta("tipo_que_nao_existe", {}, self.usuario)
        regra.refresh_from_db()
        self.assertEqual(regra.nivel, "critico")                                               # as recusas não gravaram nada
        padrao = frota.restaurar_regra_padrao("sem_sessao", self.usuario)
        self.assertEqual((padrao.nivel, padrao.destinatarios_emails, padrao.destinatarios_usuarios.count()), ("atencao", "", 0))

    def test_listar_regras_traz_todas_com_rotulo_acao_e_destinatarios(self):
        FrotaRegraAlerta.objects.filter(tipo="falha_recorrente").update(destinatarios_emails="a@b.invalid")
        regras = frota.listar_regras()
        self.assertEqual([r.tipo for r in regras], list(M2MOcorrencia.Tipo.values))
        falha = next(r for r in regras if r.tipo == "falha_recorrente")
        self.assertEqual((falha.rotulo, falha.destinatarios), ("Falhas de consulta seguidas", ["a@b.invalid"]))
        self.assertTrue(falha.acao_necessaria)
