"""
Tecnologia agrícola do Zelo Agro — o motor de instalação (instalar, retirar, transferir, substituir,
manutenção, material aplicado) e a composição tecnológica da máquina.

Cobre os cenários 3 (instalação em máquina), 5 (rádio de operador e transferência), 6 (substituição)
e a parte do 8 (integridade: instalação duplicada, equipamento indisponível ou em manutenção,
operações simultâneas) que depende da instalação.
"""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from ProjetoEstoque.models import (
    FrotaTipo,
    FrotaVeiculo,
    TecEquipamento,
    TecInstalacao,
    TecMovimentacao,
    TecSaldo,
    Usuario,
)
from ProjetoEstoque.tests.test_tec_catalogo_estoque import TecBase, erros
from services import frota_service
from services import tec_estoque_service as est
from services import tec_instalacao_service as ins

D = Decimal
Situacao = TecEquipamento.Situacao
Tipo = TecMovimentacao.Tipo


class InstalacaoBase(TecBase):
    def setUp(self):
        super().setUp()
        tipo = FrotaTipo.objects.get(nome="Trator")
        self.trator = self.veiculo("TR-025", "Trator 25", tipo)
        self.colhedora = self.veiculo("CO-010", "Colhedora 10", tipo)
        self.operadora = Usuario.objects.create(nome="Ana Operadora", status="ativo", pmb="nao")
        self.operador_b = Usuario.objects.create(nome="Bruno Operador", status="ativo", pmb="nao")
        self.g5 = self.modelo_individual("G5")

    def equipamento(self, modelo=None, serie="SN-001", local=None, **extra):
        return super().equipamento(modelo or self.g5, serie, local, **extra)

    def veiculo(self, codigo, nome, tipo, **extra):
        return frota_service.criar_veiculo({"codigo": codigo, "nome": nome, "tipo": tipo.pk, **extra}, self.usuario)

    def instalado(self, equipamento=None, destino=("veiculo", None), motivo="instalação inicial"):
        equipamento = equipamento or self.equipamento()
        tipo, alvo = destino
        return ins.instalar(equipamento, tipo, alvo or self.trator, self.usuario, motivo)

    def razao(self, equipamento):
        return list(TecMovimentacao.objects.filter(equipamento=equipamento).order_by("pk"))

    def atual(self, equipamento):
        return TecEquipamento.objects.get(pk=equipamento.pk)

    def assertConsistente(self):
        self.assertTrue(est.conferir_consistencia()["ok"], est.conferir_consistencia())
        self.assertTrue(ins.conferir_instalacoes()["ok"], ins.conferir_instalacoes())


# ═════════════════════════════════════════════════════════════
# Instalar
# ═════════════════════════════════════════════════════════════

class InstalarTests(InstalacaoBase):
    def test_instalar_tira_do_estoque_e_abre_a_instalacao(self):
        eq = self.equipamento()
        antes = est.total_disponivel(eq.modelo)
        r = ins.instalar(eq, "veiculo", self.trator, self.usuario, "monitor da cabine", observacoes="lado direito")
        eq = self.atual(eq)
        self.assertEqual((eq.situacao, eq.local_id), ("instalado", None))
        self.assertEqual(est.total_disponivel(eq.modelo), antes - 1)                 # "instalação reduz a disponibilidade"
        self.assertEqual(r.instalacao.veiculo_id, self.trator.pk)
        self.assertIsNone(r.instalacao.fim_em)
        self.assertEqual(r.instalacao.inicio_por, self.usuario)
        mov = r.movimentacao
        self.assertEqual(mov.tipo, Tipo.INSTALACAO)
        self.assertEqual((mov.origem_tipo, mov.origem_id), ("estoque", self.deposito.pk))
        self.assertEqual((mov.destino_tipo, mov.destino_id), ("veiculo", self.trator.pk))
        self.assertIn("TR-025", mov.destino_rotulo)
        self.assertEqual((mov.motivo, mov.observacoes, mov.usuario_nome), ("monitor da cabine", "lado direito", "tec_operador"))
        self.assertConsistente()

    def test_motivo_e_autor_sao_obrigatorios(self):
        eq = self.equipamento()
        with self.assertRaises(ValidationError) as ctx:
            ins.instalar(eq, "veiculo", self.trator, self.usuario, "  ")
        self.assertIn("motivo", erros(ctx.exception))
        with self.assertRaises(ValidationError):
            ins.instalar(eq, "veiculo", self.trator, None, "sem autor")
        self.assertEqual(self.atual(eq).situacao, "disponivel")
        self.assertFalse(TecInstalacao.objects.exists())

    def test_instalacao_duplicada_e_recusada_dizendo_onde_ele_esta(self):
        eq = self.equipamento()
        self.instalado(eq)
        with self.assertRaises(ValidationError) as ctx:
            ins.instalar(eq, "veiculo", self.colhedora, self.usuario, "de novo")
        texto = str(ctx.exception)
        self.assertIn("já está instalado", texto)
        self.assertIn("TR-025", texto)
        self.assertEqual(TecInstalacao.objects.filter(equipamento=eq).count(), 1)

    def test_equipamento_em_manutencao_nao_se_instala_como_disponivel(self):
        eq = self.equipamento()
        ins.enviar_para_manutencao(eq, self.usuario, "tela rachada")
        with self.assertRaises(ValidationError) as ctx:
            ins.instalar(eq, "veiculo", self.trator, self.usuario, "tentativa")
        self.assertIn("em manutenção", str(ctx.exception))
        self.assertEqual(self.atual(eq).situacao, "em_manutencao")
        self.assertFalse(TecInstalacao.objects.exists())

    def test_equipamento_inativo_nao_se_instala(self):
        eq = self.equipamento()
        est.inativar_equipamento(eq, self.usuario, "baixa")
        with self.assertRaises(ValidationError) as ctx:
            ins.instalar(eq, "veiculo", self.trator, self.usuario, "tentativa")
        self.assertIn("inativo", str(ctx.exception))

    def test_destino_invalido_ou_inexistente(self):
        eq = self.equipamento()
        for tipo, alvo in (("torre", 1), ("veiculo", ""), ("veiculo", 999999), ("", self.trator)):
            with self.subTest(tipo=tipo, alvo=alvo), self.assertRaises(ValidationError):
                ins.instalar(eq, tipo, alvo, self.usuario, "x")
        self.assertEqual(self.atual(eq).situacao, "disponivel")

    def test_maquina_desativada_nao_recebe_e_em_manutencao_recebe_com_aviso(self):
        eq = self.equipamento()
        frota_service.mudar_status(self.trator, "desativado", self.usuario)
        with self.assertRaises(ValidationError) as ctx:
            ins.instalar(eq, "veiculo", self.trator, self.usuario, "x")
        self.assertIn("desativada", str(ctx.exception))
        frota_service.mudar_status(self.colhedora, "manutencao", self.usuario)
        r = ins.instalar(eq, "veiculo", self.colhedora, self.usuario, "x")
        self.assertEqual(len(r.avisos), 1)
        self.assertIn("em manutenção", r.avisos[0])

    def test_operador_desligado_nao_recebe(self):
        rad = self.equipamento(self.modelo_individual("DEP", self.motorola, self.cat_radio), "RAD-1")
        Usuario.objects.filter(pk=self.operadora.pk).update(status="desligado")
        with self.assertRaises(ValidationError) as ctx:
            ins.instalar(rad, "operador", self.operadora.pk, self.usuario, "x")
        self.assertIn("desligado", str(ctx.exception))

    def test_token_repetido_devolve_o_primeiro_resultado_sem_duplicar(self):
        eq = self.equipamento()
        token = "11111111-1111-4111-8111-111111111111"
        primeira = ins.instalar(eq, "veiculo", self.trator, self.usuario, "x", token=token)
        segunda = ins.instalar(eq, "veiculo", self.trator, self.usuario, "x", token=token)
        self.assertFalse(primeira.repetida)
        self.assertTrue(segunda.repetida)
        self.assertEqual(segunda.movimentacao.pk, primeira.movimentacao.pk)
        self.assertEqual(segunda.instalacao.pk, primeira.instalacao.pk)
        self.assertEqual(TecInstalacao.objects.count(), 1)
        self.assertEqual(TecMovimentacao.objects.filter(tipo="instalacao").count(), 1)

    def test_operacao_que_falha_nao_deixa_rastro(self):
        eq = self.equipamento()
        movimentos = TecMovimentacao.objects.count()
        with self.assertRaises(ValidationError):
            ins.instalar(eq, "veiculo", 999999, self.usuario, "x")
        self.assertEqual(TecMovimentacao.objects.count(), movimentos)
        self.assertEqual(self.atual(eq).situacao, "disponivel")

    def test_equipamento_de_outro_local_registra_a_origem_certa(self):
        eq = self.equipamento(local=self.oficina)
        r = ins.instalar(eq, "veiculo", self.trator, self.usuario, "x")
        self.assertEqual(r.movimentacao.origem_id, self.oficina.pk)
        self.assertConsistente()


# ═════════════════════════════════════════════════════════════
# Retirar e desvincular
# ═════════════════════════════════════════════════════════════

class RetirarTests(InstalacaoBase):
    def test_retirar_devolve_ao_estoque_no_local_escolhido_e_guarda_o_periodo(self):
        eq = self.equipamento()
        inst = self.instalado(eq).instalacao
        r = ins.retirar(inst.pk, self.oficina, self.usuario, "defeito na tela")
        eq = self.atual(eq)
        self.assertEqual((eq.situacao, eq.local_id), ("disponivel", self.oficina.pk))
        inst.refresh_from_db()
        self.assertIsNotNone(inst.fim_em)
        self.assertEqual((inst.fim_por, inst.motivo_fim), (self.usuario, "defeito na tela"))
        self.assertEqual(inst.mov_fim.tipo, Tipo.RETIRADA)
        mov = r.movimentacao
        self.assertEqual((mov.origem_tipo, mov.origem_id), ("veiculo", self.trator.pk))
        self.assertEqual((mov.destino_tipo, mov.destino_id), ("estoque", self.oficina.pk))
        self.assertEqual(est.total_disponivel(eq.modelo), 1)
        self.assertConsistente()

    def test_local_de_guarda_e_obrigatorio_e_precisa_estar_ativo(self):
        eq = self.equipamento()
        inst = self.instalado(eq).instalacao
        with self.assertRaises(ValidationError) as ctx:
            ins.retirar(inst.pk, None, self.usuario, "x")
        self.assertIn("local", erros(ctx.exception))
        from ProjetoEstoque.models import AgroLocal
        AgroLocal.objects.filter(pk=self.oficina.pk).update(ativo=False)
        with self.assertRaises(ValidationError):
            ins.retirar(inst.pk, self.oficina.pk, self.usuario, "x")
        self.assertEqual(self.atual(eq).situacao, "instalado")

    def test_retirar_duas_vezes_e_recusado_e_diz_por_quem_e_quando(self):
        eq = self.equipamento()
        inst = self.instalado(eq).instalacao
        ins.retirar(inst.pk, self.deposito, self.usuario, "primeira")
        with self.assertRaises(ValidationError) as ctx:
            ins.retirar(inst.pk, self.deposito, self.usuario, "segunda")
        self.assertIn("já foi retirado por", str(ctx.exception))
        self.assertIn("Tânia Operadora", str(ctx.exception))
        self.assertEqual(TecMovimentacao.objects.filter(tipo="retirada").count(), 1)

    def test_retirar_a_instalacao_antiga_nao_age_sobre_a_nova(self):
        """A tela abriu com a instalação 1; outra pessoa retirou e instalou de novo (instalação 2)."""
        eq = self.equipamento()
        velha = self.instalado(eq).instalacao
        ins.retirar(velha.pk, self.deposito, self.usuario, "tirou")
        nova = ins.instalar(eq, "veiculo", self.colhedora, self.usuario, "recolocou").instalacao
        with self.assertRaises(ValidationError):
            ins.retirar(velha.pk, self.deposito, self.usuario, "tela velha")
        self.assertEqual(self.atual(eq).situacao, "instalado")
        self.assertIsNone(TecInstalacao.objects.get(pk=nova.pk).fim_em)

    def test_sair_de_uma_pessoa_e_desvinculacao(self):
        rad = self.equipamento(self.modelo_individual("DEP", self.motorola, self.cat_radio), "RAD-1")
        inst = ins.instalar(rad, "operador", self.operadora, self.usuario, "entregue ao operador").instalacao
        r = ins.retirar(inst.pk, self.deposito, self.usuario, "devolvido")
        self.assertEqual(r.movimentacao.tipo, Tipo.DESVINCULACAO)
        self.assertEqual((r.movimentacao.origem_tipo, r.movimentacao.origem_rotulo), ("operador", "Ana Operadora"))
        self.assertConsistente()

    def test_retirar_de_maquina_desativada_funciona(self):
        eq = self.equipamento()
        inst = self.instalado(eq).instalacao
        FrotaVeiculo.objects.filter(pk=self.trator.pk).update(status="desativado")     # legado: já havia equipamento nela
        ins.retirar(inst.pk, self.deposito, self.usuario, "máquina baixada")
        self.assertEqual(self.atual(eq).situacao, "disponivel")

    def test_instalacao_inexistente(self):
        with self.assertRaises(ValidationError):
            ins.retirar(999999, self.deposito, self.usuario, "x")
        with self.assertRaises(ValidationError):
            ins.retirar("abc", self.deposito, self.usuario, "x")


# ═════════════════════════════════════════════════════════════
# Transferir
# ═════════════════════════════════════════════════════════════

class TransferirTests(InstalacaoBase):
    def test_transferir_preserva_o_vinculo_anterior_no_historico(self):
        eq = self.equipamento()
        antiga = self.instalado(eq).instalacao
        r = ins.transferir(antiga.pk, "veiculo", self.colhedora, self.usuario, "máquina parada")
        antiga.refresh_from_db()
        self.assertIsNotNone(antiga.fim_em)
        self.assertEqual(antiga.mov_fim_id, r.movimentacao.pk)
        self.assertEqual(r.instalacao.veiculo_id, self.colhedora.pk)
        self.assertEqual(r.instalacao.mov_inicio_id, r.movimentacao.pk)
        self.assertEqual(TecInstalacao.objects.filter(equipamento=eq).count(), 2)
        self.assertEqual(TecInstalacao.objects.filter(equipamento=eq, fim_em__isnull=True).count(), 1)
        self.assertEqual(self.atual(eq).situacao, "instalado")                       # continua instalado: não passa pelo estoque
        mov = r.movimentacao
        self.assertEqual((mov.tipo, mov.origem_tipo, mov.origem_id, mov.destino_tipo, mov.destino_id), ("transferencia", "veiculo", self.trator.pk, "veiculo", self.colhedora.pk))
        self.assertEqual(est.total_disponivel(eq.modelo), 0)
        self.assertConsistente()

    def test_transferir_para_o_mesmo_destino_e_recusado(self):
        antiga = self.instalado().instalacao
        with self.assertRaises(ValidationError) as ctx:
            ins.transferir(antiga.pk, "veiculo", self.trator, self.usuario, "x")
        self.assertIn("destino", erros(ctx.exception))
        self.assertEqual(TecInstalacao.objects.count(), 1)

    def test_transferir_instalacao_ja_encerrada_e_recusado(self):
        antiga = self.instalado().instalacao
        ins.retirar(antiga.pk, self.deposito, self.usuario, "tirou")
        with self.assertRaises(ValidationError) as ctx:
            ins.transferir(antiga.pk, "veiculo", self.colhedora, self.usuario, "x")
        self.assertIn("já foi retirado", str(ctx.exception))

    def test_transferir_para_maquina_desativada_e_recusado_sem_efeito(self):
        antiga = self.instalado().instalacao
        frota_service.mudar_status(self.colhedora, "desativado", self.usuario)
        with self.assertRaises(ValidationError):
            ins.transferir(antiga.pk, "veiculo", self.colhedora, self.usuario, "x")
        self.assertIsNone(TecInstalacao.objects.get(pk=antiga.pk).fim_em)

    def test_ht_de_um_operador_para_outro_cenario_5(self):
        """Cenário 5: cadastrar um HT, vincular a um operador, transferir para outro e ver o histórico."""
        ht = self.equipamento(self.modelo_individual("DEP 450", self.motorola, self.cat_radio), "HT-001")
        primeira = ins.instalar(ht, "operador", self.operadora, self.usuario, "entregue na safra").instalacao
        r = ins.transferir(primeira.pk, "operador", self.operador_b, self.usuario, "troca de turno")
        self.assertEqual(r.instalacao.operador_id, self.operador_b.pk)
        self.assertEqual(ins.responsavel_do_equipamento(self.atual(ht)), "Bruno Operador")
        periodos = list(ins.instalacoes_do_equipamento(ht))
        self.assertEqual([p.destino_texto for p in periodos], ["Bruno Operador", "Ana Operadora"])      # ativa primeiro
        self.assertIsNone(periodos[0].fim_em)
        self.assertIsNotNone(periodos[1].fim_em)
        tipos = [m.tipo for m in self.razao(ht)]
        self.assertEqual(tipos, ["entrada", "instalacao", "transferencia"])
        self.assertConsistente()

    def test_de_maquina_para_operador(self):
        eq = self.equipamento(self.modelo_individual("DEP", self.motorola, self.cat_radio), "RAD-9")
        antiga = self.instalado(eq).instalacao
        r = ins.transferir(antiga.pk, "operador", self.operadora, self.usuario, "passou para a pessoa")
        self.assertEqual((r.movimentacao.origem_tipo, r.movimentacao.destino_tipo), ("veiculo", "operador"))
        self.assertConsistente()


# ═════════════════════════════════════════════════════════════
# Substituir
# ═════════════════════════════════════════════════════════════

class SubstituirTests(InstalacaoBase):
    def novo_monitor(self, serie="SN-002", local=None):
        return self.equipamento(self.g5, serie, local=local)

    def test_substituir_troca_no_mesmo_destino_com_duas_linhas_e_o_historico_dos_dois(self):
        velho = self.equipamento(serie="SN-001")
        antiga = self.instalado(velho).instalacao
        novo = self.novo_monitor()
        r = ins.substituir(antiga.pk, novo, self.usuario, "tela rachada", local=self.oficina)
        self.assertEqual(self.atual(velho).situacao, "disponivel")
        self.assertEqual(self.atual(velho).local_id, self.oficina.pk)
        self.assertEqual(self.atual(novo).situacao, "instalado")
        self.assertEqual(len(r.movimentacoes), 2)
        saida, entrada = r.movimentacoes
        self.assertEqual({saida.tipo, entrada.tipo}, {"substituicao"})
        self.assertEqual(saida.operacao, entrada.operacao)
        self.assertEqual((saida.equipamento_id, entrada.equipamento_id), (velho.pk, novo.pk))
        self.assertEqual(r.instalacao.veiculo_id, self.trator.pk)
        self.assertEqual(r.instalacao.equipamento_id, novo.pk)
        self.assertIsNotNone(TecInstalacao.objects.get(pk=antiga.pk).fim_em)
        self.assertConsistente()

    def test_cenario_6_retirar_registrar_motivo_instalar_outro_e_ambos_ficam_no_historico(self):
        velho = self.equipamento(serie="SN-001")
        antiga = self.instalado(velho).instalacao
        ins.retirar(antiga.pk, self.deposito, self.usuario, "monitor com defeito")
        novo = self.novo_monitor()
        ins.instalar(novo, "veiculo", self.trator, self.usuario, "reposição do monitor")
        historico = ins.historico_do_veiculo(self.trator)
        self.assertEqual([h["equipamento"].pk for h in historico], [velho.pk])
        self.assertEqual(historico[0]["motivo_fim"], "monitor com defeito")
        composicao = ins.composicao_do_veiculo(self.trator)
        self.assertEqual([l["equipamento"].pk for l in composicao["equipamentos"]], [novo.pk])
        self.assertEqual(len(self.razao(velho)), 3)                                  # entrada, instalação, retirada
        self.assertConsistente()

    def test_o_antigo_pode_ir_para_a_manutencao(self):
        velho = self.equipamento(serie="SN-001")
        antiga = self.instalado(velho).instalacao
        novo = self.novo_monitor()
        r = ins.substituir(antiga.pk, novo, self.usuario, "vai para a assistência", destino_do_antigo="manutencao")
        self.assertEqual((self.atual(velho).situacao, self.atual(velho).local_id), ("em_manutencao", None))
        self.assertEqual(r.movimentacoes[0].destino_tipo, "manutencao")
        self.assertConsistente()

    def test_o_antigo_que_volta_ao_estoque_exige_local(self):
        antiga = self.instalado().instalacao
        with self.assertRaises(ValidationError) as ctx:
            ins.substituir(antiga.pk, self.novo_monitor(), self.usuario, "x")
        self.assertIn("local", erros(ctx.exception))

    def test_novo_igual_ao_antigo_ou_indisponivel_e_recusado_sem_efeito(self):
        velho = self.equipamento(serie="SN-001")
        antiga = self.instalado(velho).instalacao
        with self.assertRaises(ValidationError):
            ins.substituir(antiga.pk, velho, self.usuario, "x", local=self.deposito)
        em_manutencao = self.novo_monitor("SN-M")
        ins.enviar_para_manutencao(em_manutencao, self.usuario, "x")
        with self.assertRaises(ValidationError) as ctx:
            ins.substituir(antiga.pk, em_manutencao, self.usuario, "x", local=self.deposito)
        self.assertIn("equipamento_novo", erros(ctx.exception))
        self.assertEqual(self.atual(velho).situacao, "instalado")
        self.assertIsNone(TecInstalacao.objects.get(pk=antiga.pk).fim_em)

    def test_substituir_com_token_repetido_devolve_as_duas_linhas(self):
        antiga = self.instalado().instalacao
        novo = self.novo_monitor()
        token = "22222222-2222-4222-8222-222222222222"
        primeira = ins.substituir(antiga.pk, novo, self.usuario, "x", local=self.deposito, token=token)
        segunda = ins.substituir(antiga.pk, novo, self.usuario, "x", local=self.deposito, token=token)
        self.assertTrue(segunda.repetida)
        self.assertEqual([m.pk for m in segunda.movimentacoes], [m.pk for m in primeira.movimentacoes])
        self.assertEqual(TecMovimentacao.objects.filter(tipo="substituicao").count(), 2)

    def test_substituir_instalacao_ja_encerrada_e_recusado(self):
        antiga = self.instalado().instalacao
        ins.retirar(antiga.pk, self.deposito, self.usuario, "tirou")
        with self.assertRaises(ValidationError):
            ins.substituir(antiga.pk, self.novo_monitor(), self.usuario, "x", local=self.deposito)


# ═════════════════════════════════════════════════════════════
# Manutenção (estado e lugar)
# ═════════════════════════════════════════════════════════════

class ManutencaoTests(InstalacaoBase):
    def test_enviar_o_disponivel_tira_do_estoque(self):
        eq = self.equipamento()
        r = ins.enviar_para_manutencao(eq, self.usuario, "não liga")
        eq = self.atual(eq)
        self.assertEqual((eq.situacao, eq.local_id), ("em_manutencao", None))
        self.assertEqual((r.movimentacao.tipo, r.movimentacao.origem_tipo, r.movimentacao.destino_tipo), ("envio_manutencao", "estoque", "manutencao"))
        self.assertEqual(est.total_disponivel(eq.modelo), 0)
        self.assertConsistente()

    def test_enviar_o_instalado_encerra_a_instalacao_na_mesma_transacao(self):
        eq = self.equipamento()
        inst = self.instalado(eq).instalacao
        r = ins.enviar_para_manutencao(eq, self.usuario, "tela rachada")
        inst.refresh_from_db()
        self.assertIsNotNone(inst.fim_em)
        self.assertEqual(inst.mov_fim.tipo, Tipo.ENVIO_MANUTENCAO)
        self.assertEqual(r.encerrada.pk, inst.pk)
        self.assertEqual((r.movimentacao.origem_tipo, r.movimentacao.origem_id), ("veiculo", self.trator.pk))
        self.assertEqual(ins.composicao_do_veiculo(self.trator)["total"], 0)
        self.assertConsistente()

    def test_retorno_devolve_ao_estoque_e_so_entao_pode_ser_instalado(self):
        eq = self.equipamento()
        ins.enviar_para_manutencao(eq, self.usuario, "x")
        with self.assertRaises(ValidationError):
            ins.instalar(eq, "veiculo", self.trator, self.usuario, "antes do retorno")
        r = ins.retornar_da_manutencao(eq, self.oficina, self.usuario, "consertado")
        eq = self.atual(eq)
        self.assertEqual((eq.situacao, eq.local_id), ("disponivel", self.oficina.pk))
        self.assertEqual((r.movimentacao.tipo, r.movimentacao.origem_tipo, r.movimentacao.destino_id), ("retorno_manutencao", "manutencao", self.oficina.pk))
        ins.instalar(eq, "veiculo", self.trator, self.usuario, "de volta")
        self.assertConsistente()

    def test_estados_invalidos(self):
        eq = self.equipamento()
        with self.assertRaises(ValidationError) as ctx:
            ins.retornar_da_manutencao(eq, self.deposito, self.usuario, "x")
        self.assertIn("só equipamento em manutenção", str(ctx.exception))
        ins.enviar_para_manutencao(eq, self.usuario, "x")
        with self.assertRaises(ValidationError):
            ins.enviar_para_manutencao(eq, self.usuario, "de novo")
        est.inativar_equipamento(eq, self.usuario, "baixa")
        with self.assertRaises(ValidationError):
            ins.enviar_para_manutencao(eq, self.usuario, "inativo")

    def test_inativar_equipamento_instalado_continua_recusado(self):
        eq = self.equipamento()
        self.instalado(eq)
        with self.assertRaises(ValidationError):
            est.inativar_equipamento(eq, self.usuario, "x")


# ═════════════════════════════════════════════════════════════
# Material por quantidade aplicado
# ═════════════════════════════════════════════════════════════

class MaterialAplicadoTests(InstalacaoBase):
    def setUp(self):
        super().setUp()
        self.cabo = self.modelo_material("Cabo UTP")
        est.registrar_entrada_material(self.cabo, self.deposito, 100, self.usuario)

    def saldo(self):
        return TecSaldo.objects.get(modelo=self.cabo, local=self.deposito).quantidade

    def test_aplicar_debita_o_estoque_e_aparece_na_composicao(self):
        r = ins.aplicar_material(self.cabo, self.deposito, 30, "veiculo", self.trator, self.usuario, "chicote da cabine")
        self.assertEqual(self.saldo(), D("70"))
        self.assertEqual((r.movimentacao.tipo, r.movimentacao.destino_tipo, r.movimentacao.destino_id), ("instalacao", "veiculo", self.trator.pk))
        self.assertEqual(r.movimentacao.saldo_depois, D("70"))
        aplicados = ins.composicao_do_veiculo(self.trator)["materiais"]
        self.assertEqual([(a["modelo"].pk, a["quantidade"]) for a in aplicados], [(self.cabo.pk, D("30"))])
        self.assertConsistente()

    def test_aplicar_mais_do_que_ha_e_recusado(self):
        with self.assertRaises(ValidationError) as ctx:
            ins.aplicar_material(self.cabo, self.deposito, 101, "veiculo", self.trator, self.usuario, "x")
        self.assertIn("quantidade", erros(ctx.exception))
        self.assertEqual(self.saldo(), D("100"))
        self.assertEqual(ins.composicao_do_veiculo(self.trator)["materiais"], [])

    def test_modelo_individual_nao_se_aplica_por_quantidade(self):
        with self.assertRaises(ValidationError):
            ins.aplicar_material(self.modelo_individual(), self.deposito, 1, "veiculo", self.trator, self.usuario, "x")

    def test_devolver_volta_ao_estoque_e_so_devolve_o_que_esta_aplicado(self):
        ins.aplicar_material(self.cabo, self.deposito, 30, "veiculo", self.trator, self.usuario, "x")
        r = ins.devolver_material(self.cabo, "veiculo", self.trator, self.oficina, 10, self.usuario, "sobrou")
        self.assertEqual(r.movimentacao.tipo, "retirada")
        self.assertEqual(TecSaldo.objects.get(modelo=self.cabo, local=self.oficina).quantidade, D("10"))
        self.assertEqual(ins.composicao_do_veiculo(self.trator)["materiais"][0]["quantidade"], D("20"))
        self.assertConsistente()

    def test_devolver_mais_do_que_foi_aplicado_desfaz_tudo(self):
        ins.aplicar_material(self.cabo, self.deposito, 30, "veiculo", self.trator, self.usuario, "x")
        movimentos = TecMovimentacao.objects.count()
        with self.assertRaises(ValidationError) as ctx:
            ins.devolver_material(self.cabo, "veiculo", self.trator, self.deposito, 31, self.usuario, "x")
        self.assertIn("Só há 30", str(ctx.exception))
        self.assertEqual(TecMovimentacao.objects.count(), movimentos)
        self.assertEqual(self.saldo(), D("70"))                                      # o crédito foi desfeito junto
        self.assertConsistente()

    def test_devolver_de_um_destino_onde_nunca_houve_aplicacao_e_recusado(self):
        with self.assertRaises(ValidationError):
            ins.devolver_material(self.cabo, "veiculo", self.colhedora, self.deposito, 1, self.usuario, "x")

    def test_unidade_inteira_recusa_fracao(self):
        conector = self.modelo_material("Conector RJ45", unidade=self.un)
        est.registrar_entrada_material(conector, self.deposito, 10, self.usuario)
        with self.assertRaises(ValidationError):
            ins.aplicar_material(conector, self.deposito, "2,5", "veiculo", self.trator, self.usuario, "x")

    def test_token_repetido_nao_debita_duas_vezes(self):
        token = "33333333-3333-4333-8333-333333333333"
        ins.aplicar_material(self.cabo, self.deposito, 5, "veiculo", self.trator, self.usuario, "x", token=token)
        r = ins.aplicar_material(self.cabo, self.deposito, 5, "veiculo", self.trator, self.usuario, "x", token=token)
        self.assertTrue(r.repetida)
        self.assertEqual(self.saldo(), D("95"))


# ═════════════════════════════════════════════════════════════
# Integridade (cenário 8) — a garantia é o banco
# ═════════════════════════════════════════════════════════════

class IntegridadeTests(InstalacaoBase):
    def test_duas_telas_abertas_so_uma_instala(self):
        """Simula duas pessoas: as duas viram o equipamento disponível; a primeira instala, a segunda é
        recusada com a situação ATUAL, e só existe uma instalação."""
        eq = self.equipamento()
        tela_da_segunda = TecEquipamento.objects.get(pk=eq.pk)
        ins.instalar(eq, "veiculo", self.trator, self.usuario, "primeira")
        with self.assertRaises(ValidationError) as ctx:
            ins.instalar(tela_da_segunda, "veiculo", self.colhedora, self.usuario, "segunda")
        self.assertIn("já está instalado", str(ctx.exception))
        self.assertEqual(TecInstalacao.objects.filter(equipamento=eq, fim_em__isnull=True).count(), 1)
        self.assertEqual(TecMovimentacao.objects.filter(tipo="instalacao").count(), 1)

    def test_equipamento_que_mudou_de_local_no_meio_recusa_em_vez_de_gravar_origem_falsa(self):
        eq = self.equipamento()
        velho = est.listar_equipamentos().get(pk=eq.pk)
        est.mover_equipamento_de_local(eq, self.oficina, self.usuario, "moveu")
        r = ins.instalar(velho, "veiculo", self.trator, self.usuario, "x")           # relê dentro da transação: usa o local novo
        self.assertEqual(r.movimentacao.origem_id, self.oficina.pk)
        self.assertConsistente()

    def test_banco_recusa_duas_instalacoes_ativas_do_mesmo_equipamento(self):
        eq = self.equipamento()
        self.instalado(eq)
        with self.assertRaises(IntegrityError), transaction.atomic():
            TecInstalacao.objects.create(equipamento=eq, veiculo=self.colhedora)

    def test_banco_recusa_instalacao_sem_destino_ou_com_dois(self):
        eq = self.equipamento(serie="A")
        with self.assertRaises(IntegrityError), transaction.atomic():
            TecInstalacao.objects.create(equipamento=eq)
        with self.assertRaises(IntegrityError), transaction.atomic():
            TecInstalacao.objects.create(equipamento=eq, veiculo=self.trator, operador=self.operadora)

    def test_banco_recusa_fim_antes_do_inicio(self):
        eq = self.equipamento()
        inst = self.instalado(eq).instalacao
        with self.assertRaises(IntegrityError), transaction.atomic():
            TecInstalacao.objects.filter(pk=inst.pk).update(fim_em=inst.inicio_em.replace(year=2000))

    def test_instalacao_e_historico_imutavel(self):
        inst = self.instalado().instalacao
        inst.motivo_fim = "alterado"
        with self.assertRaises(ValidationError):
            inst.save()
        with self.assertRaises(ValidationError):
            inst.delete()
        ins.retirar(inst.pk, self.deposito, self.usuario, "tirou")
        encerrada = TecInstalacao.objects.get(pk=inst.pk)
        with self.assertRaises(ValidationError):
            encerrada.save(update_fields=["motivo_fim"])                              # encerrada não muda mais

    def test_protegido_contra_exclusao_do_equipamento_e_da_maquina(self):
        from django.db.models import ProtectedError
        eq = self.equipamento()
        self.instalado(eq)
        with self.assertRaises(ProtectedError):
            eq.delete()
        with self.assertRaises(ProtectedError):
            self.trator.delete()

    def test_desativar_maquina_com_equipamento_instalado_e_recusado(self):
        eq = self.equipamento()
        inst = self.instalado(eq).instalacao
        with self.assertRaises(ValidationError) as ctx:
            frota_service.mudar_status(self.trator, "desativado", self.usuario)
        self.assertIn("equipamento tecnológico instalado", str(ctx.exception))
        ins.retirar(inst.pk, self.deposito, self.usuario, "tirou")
        frota_service.mudar_status(self.trator, "desativado", self.usuario)           # sem equipamento, desativa

    def test_a_tabela_do_ti_nao_e_tocada(self):
        from ProjetoEstoque.models import Item, MovimentacaoItem
        antes = (Item.objects.count(), MovimentacaoItem.objects.count())
        eq = self.equipamento()
        inst = self.instalado(eq).instalacao
        ins.transferir(inst.pk, "veiculo", self.colhedora, self.usuario, "x")
        self.assertEqual((Item.objects.count(), MovimentacaoItem.objects.count()), antes)


class ConferirInstalacoesTests(InstalacaoBase):
    def test_tudo_certo_depois_de_um_ciclo_completo(self):
        eq = self.equipamento()
        inst = self.instalado(eq).instalacao
        inst = ins.transferir(inst.pk, "veiculo", self.colhedora, self.usuario, "x").instalacao
        ins.retirar(inst.pk, self.deposito, self.usuario, "y")
        self.assertConsistente()

    def test_instalado_sem_instalacao_ativa_e_apontado(self):
        eq = self.equipamento()
        TecEquipamento.objects.filter(pk=eq.pk).update(situacao="instalado", local=None)
        r = ins.conferir_instalacoes()
        self.assertFalse(r["ok"])
        self.assertEqual([e.pk for e in r["instalado_sem_instalacao"]], [eq.pk])

    def test_instalacao_ativa_de_equipamento_que_nao_esta_instalado_e_apontada(self):
        eq = self.equipamento()
        self.instalado(eq)
        TecEquipamento.objects.filter(pk=eq.pk).update(situacao="disponivel", local=self.deposito)
        r = ins.conferir_instalacoes()
        self.assertFalse(r["ok"])
        self.assertEqual([e.pk for e in r["instalacao_sem_instalado"]], [eq.pk])

    def test_razao_que_discorda_das_instalacoes_e_apontado(self):
        eq = self.equipamento()
        self.instalado(eq)
        TecInstalacao.objects.filter(equipamento=eq).update(veiculo=self.colhedora)       # a instalação "mudou" sem razão
        r = ins.conferir_instalacoes()
        self.assertFalse(r["ok"])
        self.assertTrue(r["razao"])

    def test_comando_de_auditoria_enxerga_a_divergencia_de_instalacao(self):
        from io import StringIO

        from django.core.management import call_command

        eq = self.equipamento()
        TecEquipamento.objects.filter(pk=eq.pk).update(situacao="instalado", local=None)
        saida = StringIO()
        with self.assertRaises(SystemExit) as ctx:
            call_command("auditar_tec_estoque", stdout=saida)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("INSTALADO SEM INSTALACAO", saida.getvalue())


# ═════════════════════════════════════════════════════════════
# Leitura: composição, histórico e responsável
# ═════════════════════════════════════════════════════════════

class ComposicaoTests(InstalacaoBase):
    def test_cenario_3_instalar_monitor_e_receptor_aparecem_na_composicao(self):
        monitor = self.equipamento(self.g5, "MON-1")
        receptor = self.equipamento(self.modelo_individual("StarFire", categoria=self.cat_radio), "REC-1")
        self.instalado(monitor)
        self.instalado(receptor)
        comp = ins.composicao_do_veiculo(self.trator)
        self.assertEqual(comp["total"], 2)
        self.assertEqual({l["equipamento"].pk for l in comp["equipamentos"]}, {monitor.pk, receptor.pk})
        linha = next(l for l in comp["equipamentos"] if l["equipamento"].pk == monitor.pk)
        self.assertEqual((linha["fabricante"].nome, linha["categoria"].nome, linha["por"]), ("John Deere", "Monitor / display", "tec_operador"))
        self.assertEqual(ins.composicao_do_veiculo(self.colhedora)["total"], 0)

    def test_responsavel_e_derivado_do_operador_ou_da_maquina(self):
        eq = self.equipamento()
        self.assertEqual(ins.responsavel_do_equipamento(self.atual(eq)), "")                      # no estoque
        FrotaVeiculo.objects.filter(pk=self.trator.pk).update(responsavel=self.operador_b)
        self.instalado(eq)
        self.assertEqual(ins.responsavel_do_equipamento(self.atual(eq)), "Bruno Operador")        # o da máquina
        rad = self.equipamento(self.modelo_individual("DEP", self.motorola, self.cat_radio), "RAD-1")
        ins.instalar(rad, "operador", self.operadora, self.usuario, "x")
        self.assertEqual(ins.responsavel_do_equipamento(self.atual(rad)), "Ana Operadora")

    def test_a_lista_de_equipamentos_traz_a_instalacao_sem_consulta_por_linha(self):
        for n in range(4):
            self.instalado(self.equipamento(serie=f"S-{n}"))
        with self.assertNumQueries(2):                                                           # equipamentos (com os joins) + instalações ativas
            linhas = list(est.listar_equipamentos())
            destinos = [l.instalacao_ativa.destino_texto for l in linhas]
        self.assertEqual(set(destinos), {"TR-025 · Trator 25"})
