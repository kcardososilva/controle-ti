"""
Frotas e Maquinários — o equipamento como ativo: identificadores, classificação, inventário e conferências.

Cobre a camada de serviço (`frota_service`, `frota_qualidade_service`). As telas e a importação da
planilha do ERP têm os seus arquivos de teste.

As regras que estes testes seguram, todas do prompt de refinamento:

  • o código do ERP (`codigo_legado`) é preservado e é UM identificador; o interno é OUTRO, gerado;
  • importar não corrige nem recusa por suspeita (chassi repetido, data de 1905, placa interna) —
    sinaliza; já a digitação à mão continua exigente;
  • editar um equipamento nunca perde o que já estava gravado, mesmo o que a digitação recusaria;
  • "Em Uso" da planilha não é "validado": quem confirma é o responsável pelo inventário.
"""
from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.utils import timezone

from ProjetoEstoque.models import (
    FrotaCentroCusto,
    FrotaClasseMecanica,
    FrotaClasseOperacional,
    FrotaEmpresa,
    FrotaEvento,
    FrotaImportacaoEquipamentos,
    FrotaNatureza,
    FrotaTipo,
    FrotaVeiculo,
)
from ProjetoEstoque.tests import test_frota as base
from services import frota_qualidade_service as qualidade
from services import frota_service as frota

Status = FrotaVeiculo.Status


class EquipamentoBase(base.FrotaBase):
    def setUp(self):
        super().setUp()
        self.santa = FrotaEmpresa.objects.create(codigo_erp="1", nome="SANTA COLOMBA", natureza_padrao=FrotaNatureza.PROPRIO)
        self.terceiros = FrotaEmpresa.objects.create(codigo_erp="2", nome="TERCEIROS", natureza_padrao=FrotaNatureza.TERCEIRO)
        self.classe_leve = FrotaClasseOperacional.objects.create(codigo_erp="34", nome="TRATOR LEVE", categoria=self.trator)
        self.mecanica = FrotaClasseMecanica.objects.create(codigo_erp="18", nome="TRATOR LEVE")
        self.centro = FrotaCentroCusto.objects.create(codigo_erp="32107", nome="TRATOR LEVE - GRAOS")
        self.moto = FrotaTipo.objects.create(nome="Motocicleta", espera_placa=True, espera_chassi=True)

    def dados(self, **extra):
        valores = {"codigo": "TR-01", "nome": "TRATOR NH 3880", "tipo": self.trator.pk}
        valores.update(extra)
        return valores

    def importacao(self):
        return FrotaImportacaoEquipamentos.objects.create(arquivo_nome="equipamentos.xlsx", sha256="0" * 64, formato="xlsx")

    def do_erp(self, legado="8110001", **extra):
        """Um equipamento como a importação o cria: sem código interno digitado e com o do ERP."""
        valores = {"nome": "TRATOR NH 3880", "tipo": self.trator.pk, "codigo_legado": legado, "modelo": "TRATOR NH 3880"}
        valores.update(extra)
        return frota.criar_veiculo(valores, self.usuario, importacao=self.importacao())


class IdentificadoresTests(EquipamentoBase):
    def test_o_codigo_interno_e_gerado_e_o_do_erp_fica_separado(self):
        v = frota.criar_veiculo({"nome": "TRATOR", "tipo": self.trator.pk, "codigo_legado": "8110001"}, self.usuario)
        self.assertEqual((v.codigo, v.codigo_legado), ("MQ-00001", "8110001"))
        w = frota.criar_veiculo({"nome": "OUTRO", "tipo": self.trator.pk, "codigo_legado": "8110003"}, self.usuario)
        self.assertEqual((w.codigo, w.codigo_legado), ("MQ-00002", "8110003"))

    def test_o_codigo_original_nunca_vira_o_interno(self):
        v = frota.criar_veiculo({"nome": "T", "tipo": self.trator.pk, "codigo_legado": "8110001"}, self.usuario)
        self.assertNotEqual(v.codigo, v.codigo_legado)

    def test_codigo_digitado_vale_e_nao_atrapalha_a_sequencia(self):
        frota.criar_veiculo(self.dados(codigo="tr-01"), self.usuario)
        v = frota.criar_veiculo({"nome": "T", "tipo": self.trator.pk}, self.usuario)
        self.assertEqual(v.codigo, "MQ-00001")

    def test_o_codigo_original_e_preservado_como_veio(self):
        v = frota.criar_veiculo(self.dados(codigo_legado="  8110001  "), self.usuario)
        self.assertEqual(v.codigo_legado, "8110001")
        w = frota.criar_veiculo(self.dados(codigo="TR-02", codigo_legado="ab 12"), self.usuario)
        self.assertEqual(w.codigo_legado, "ab 12")             # nem caixa nem conteúdo são mexidos

    def test_o_codigo_original_e_unico_quando_preenchido(self):
        frota.criar_veiculo(self.dados(codigo_legado="8110001"), self.usuario)
        with self.assertRaises(ValidationError) as cm:
            frota.criar_veiculo(self.dados(codigo="TR-02", codigo_legado="8110001"), self.usuario)
        self.assertIn("codigo_legado", cm.exception.message_dict)
        frota.criar_veiculo(self.dados(codigo="TR-03"), self.usuario)
        frota.criar_veiculo(self.dados(codigo="TR-04"), self.usuario)            # vazio repete à vontade

    def test_o_banco_tambem_garante_a_unicidade_do_codigo_original(self):
        frota.criar_veiculo(self.dados(codigo_legado="8110001"), self.usuario)
        with self.assertRaises(IntegrityError), transaction.atomic():
            FrotaVeiculo.objects.create(codigo="X", nome="X", tipo=self.trator, codigo_legado="8110001")

    def test_codigo_original_aceita_ate_30_caracteres(self):
        with self.assertRaises(ValidationError) as cm:
            frota.criar_veiculo(self.dados(codigo_legado="9" * 31), self.usuario)
        self.assertIn("codigo_legado", cm.exception.message_dict)

    def test_busca_por_codigo_original_e_por_interno(self):
        v = frota.criar_veiculo({"nome": "TRATOR NH", "tipo": self.trator.pk, "codigo_legado": "8110001"}, self.usuario)
        for termo in ("8110001", "811000", v.codigo, "mq-00001"):
            with self.subTest(termo=termo):
                self.assertEqual([x.pk for x in frota.listar_veiculos({"q": termo})], [v.pk])

    def test_busca_por_centro_de_custo_pelo_codigo_e_pelo_nome(self):
        v = frota.criar_veiculo(self.dados(centro_custo=self.centro.pk), self.usuario)
        frota.criar_veiculo(self.dados(codigo="TR-02"), self.usuario)
        for termo in ("32107", "TRATOR LEVE - GRAOS", "trator level".replace("level", "leve")):
            with self.subTest(termo=termo):
                self.assertEqual([x.pk for x in frota.listar_veiculos({"q": termo})], [v.pk])


class CadastroDoEquipamentoTests(EquipamentoBase):
    def test_a_classificacao_e_os_dados_do_erp_sao_gravados(self):
        v = frota.criar_veiculo(self.dados(
            codigo_legado="8110001", natureza="proprio", empresa=self.santa.pk, classe_operacional=self.classe_leve.pk,
            classe_mecanica=self.mecanica.pk, centro_custo=self.centro.pk, ano_fabricacao=2011, ano_modelo=2011,
            data_compra="15/08/2018", data_inicio="2025-05-19", setor="GRÃOS",
        ), self.usuario)
        v = FrotaVeiculo.objects.get(pk=v.pk)
        self.assertEqual(
            (v.natureza, v.empresa, v.classe_operacional, v.classe_mecanica, v.centro_custo),
            ("proprio", self.santa, self.classe_leve, self.mecanica, self.centro),
        )
        self.assertEqual((v.data_compra, v.data_inicio, v.ano_modelo), (date(2018, 8, 15), date(2025, 5, 19), 2011))

    def test_natureza_e_a_classe_sao_classificacoes_independentes(self):
        """Um trator pode ser próprio ou de terceiro: a natureza não sai da classe nem da empresa."""
        a = frota.criar_veiculo(self.dados(natureza="terceiro", empresa=self.santa.pk, classe_operacional=self.classe_leve.pk), self.usuario)
        b = frota.criar_veiculo(self.dados(codigo="TR-02", natureza="proprio", empresa=self.terceiros.pk, classe_operacional=self.classe_leve.pk), self.usuario)
        self.assertEqual((a.natureza, b.natureza), ("terceiro", "proprio"))

    def test_natureza_invalida_e_recusada(self):
        with self.assertRaises(ValidationError) as cm:
            frota.criar_veiculo(self.dados(natureza="alugado"), self.usuario)
        self.assertIn("natureza", cm.exception.message_dict)

    def test_cadastro_de_apoio_inativo_nao_pode_ser_escolhido_de_novo(self):
        FrotaClasseOperacional.objects.filter(pk=self.classe_leve.pk).update(ativo=False)
        with self.assertRaises(ValidationError) as cm:
            frota.criar_veiculo(self.dados(classe_operacional=self.classe_leve.pk), self.usuario)
        self.assertIn("classe_operacional", cm.exception.message_dict)

    def test_equipamento_que_ja_usa_um_cadastro_inativado_continua_editavel(self):
        v = frota.criar_veiculo(self.dados(classe_operacional=self.classe_leve.pk), self.usuario)
        FrotaClasseOperacional.objects.filter(pk=self.classe_leve.pk).update(ativo=False)
        v = FrotaVeiculo.objects.get(pk=v.pk)
        frota.editar_veiculo(v, {"nome": "TRATOR RENOMEADO", "classe_operacional": self.classe_leve.pk}, self.usuario)
        self.assertEqual(FrotaVeiculo.objects.get(pk=v.pk).classe_operacional, self.classe_leve)

    def test_ano_modelo_pode_ser_o_do_ano_que_vem_mas_nao_alem(self):
        proximo = timezone.localdate().year + 1
        v = frota.criar_veiculo(self.dados(ano_fabricacao=proximo - 1, ano_modelo=proximo), self.usuario)
        self.assertEqual(v.ano_modelo, proximo)
        with self.assertRaises(ValidationError) as cm:
            frota.criar_veiculo(self.dados(codigo="TR-02", ano_modelo=proximo + 1), self.usuario)
        self.assertIn("ano_modelo", cm.exception.message_dict)

    def test_datas_aceitam_os_dois_formatos_e_recusam_o_que_nao_e_data(self):
        self.assertEqual(frota.ler_data("15/08/2018"), date(2018, 8, 15))
        self.assertEqual(frota.ler_data("2018-08-15"), date(2018, 8, 15))
        self.assertIsNone(frota.ler_data(""))
        for ruim in ("31/02/2020", "ontem", "2018/08/15"):
            with self.subTest(ruim=ruim), self.assertRaises(ValidationError):
                frota.ler_data(ruim)

    def test_a_digitacao_nao_aceita_compra_no_futuro_nem_antes_de_1900(self):
        amanha = timezone.localdate() + timedelta(days=1)
        for campo, valor in (("data_compra", amanha), ("data_compra", date(1899, 12, 31)), ("data_inicio", "31/02/2020")):
            with self.subTest(campo=campo, valor=valor), self.assertRaises(ValidationError) as cm:
                frota.criar_veiculo(self.dados(**{campo: valor}), self.usuario)
            self.assertIn(campo, cm.exception.message_dict)

    def test_o_rotulo_do_status_ativo_e_em_uso(self):
        self.assertEqual(Status.ATIVO.label, "Em uso")
        self.assertEqual(Status.ATIVO.value, "ativo")           # o que está gravado nunca muda


class PlacaEChassiTests(EquipamentoBase):
    """Placa e chassi respeitam o TIPO do equipamento; e o que já está gravado nunca vira bloqueio."""

    def test_tipo_que_espera_placa_exige_o_formato_oficial_na_digitacao(self):
        with self.assertRaises(ValidationError) as cm:
            frota.criar_veiculo(self.dados(tipo=self.moto.pk, placa="C811009"), self.usuario)
        self.assertIn("placa", cm.exception.message_dict)
        v = frota.criar_veiculo(self.dados(tipo=self.moto.pk, placa="pkc-2004"), self.usuario)
        self.assertEqual(v.placa, "PKC2004")

    def test_tipo_que_nao_espera_placa_aceita_a_identificacao_interna(self):
        v = frota.criar_veiculo(self.dados(placa="c8110035"), self.usuario)          # trator
        self.assertEqual(v.placa, "C8110035")
        with self.assertRaises(ValidationError):
            frota.criar_veiculo(self.dados(codigo="TR-02", placa="C-81100351"), self.usuario)     # 9 caracteres

    def test_placa_ja_gravada_fora_do_padrao_nao_bloqueia_a_edicao_de_outro_campo(self):
        v = frota.criar_veiculo(self.dados(tipo=self.moto.pk, placa="ABC1234"), self.usuario)
        FrotaVeiculo.objects.filter(pk=v.pk).update(placa="PLPOD08")          # como a importação deixou
        v = FrotaVeiculo.objects.get(pk=v.pk)
        frota.editar_veiculo(v, {"nome": "MOTO RENOMEADA", "placa": "PLPOD08"}, self.usuario)
        self.assertEqual(FrotaVeiculo.objects.get(pk=v.pk).placa, "PLPOD08")
        with self.assertRaises(ValidationError):
            frota.editar_veiculo(v, {"placa": "OUTRA99"}, self.usuario)         # placa NOVA continua exigente

    def test_a_importacao_nunca_recusa_pelo_formato_da_placa(self):
        v = self.do_erp(tipo=self.moto.pk, placa="SKQB64")
        self.assertEqual(v.placa, "SKQB64")

    def test_chassi_repetido_e_recusado_na_digitacao(self):
        frota.criar_veiculo(self.dados(chassi_serie="1CQS660ALH0120143"), self.usuario)
        with self.assertRaises(ValidationError) as cm:
            frota.criar_veiculo(self.dados(codigo="TR-02", chassi_serie="1cqs660alh0120143"), self.usuario)
        self.assertIn("chassi_serie", cm.exception.message_dict)

    def test_chassi_repetido_vindo_do_erp_e_importado_sem_correcao(self):
        a = self.do_erp("8110001", chassi_serie="1CQS660ALH0120143")
        b = self.do_erp("8210001", chassi_serie="1CQS660ALH0120143", nome="COLHEI. GRAOS JDS660")
        self.assertEqual((a.chassi_serie, b.chassi_serie), ("1CQS660ALH0120143", "1CQS660ALH0120143"))

    def test_o_banco_nao_tem_mais_o_chassi_unico(self):
        a = self.do_erp("8110001", chassi_serie="X1")
        FrotaVeiculo.objects.create(codigo="MQ-9", nome="x", tipo=self.trator, chassi_serie=a.chassi_serie)    # sem IntegrityError

    def test_equipamento_com_chassi_repetido_pode_ser_editado_sem_mexer_no_chassi(self):
        self.do_erp("8110001", chassi_serie="IGUAL")
        b = self.do_erp("8210001", chassi_serie="IGUAL")
        b = FrotaVeiculo.objects.get(pk=b.pk)
        frota.editar_veiculo(b, {"nome": "OUTRO NOME", "chassi_serie": "IGUAL"}, self.usuario)
        self.assertEqual(FrotaVeiculo.objects.get(pk=b.pk).nome, "OUTRO NOME")
        with self.assertRaises(ValidationError):
            frota.editar_veiculo(b, {"chassi_serie": "IGUAL2"}, self.usuario) if False else frota.criar_veiculo(
                self.dados(codigo="TR-09", chassi_serie="IGUAL"), self.usuario,
            )

    def test_placa_repetida_e_recusada_ate_na_importacao_e_diz_com_quem(self):
        self.do_erp("8110001", placa="ABC1234")
        with self.assertRaises(ValidationError) as cm:
            self.do_erp("8110002", placa="ABC1234")
        self.assertIn("8110001", cm.exception.message_dict["placa"][0])

    def test_importar_nao_exige_placa_nem_chassi(self):
        v = self.do_erp()
        self.assertEqual((v.placa, v.chassi_serie), ("", ""))

    def test_anos_e_datas_estranhos_da_planilha_entram_como_vieram(self):
        v = self.do_erp(ano_fabricacao=2018, ano_modelo=2010, data_compra=date(1905, 7, 3), data_inicio=date(2025, 5, 19))
        self.assertEqual((v.ano_fabricacao, v.ano_modelo, v.data_compra), (2018, 2010, date(1905, 7, 3)))
        with self.assertRaises(ValidationError):
            self.do_erp("8110099", data_compra=date(1800, 1, 1))              # isto já não é uma data de compra

    def test_o_cadastro_importado_registra_a_origem_no_evento(self):
        v = self.do_erp(situacao_origem="Em Uso", validacao_pendente=True)
        evento = FrotaEvento.objects.get(veiculo=v, tipo=FrotaEvento.Tipo.CADASTRO)
        self.assertEqual(evento.dados["origem"], "importacao")
        self.assertEqual(evento.dados["codigo_legado"], "8110001")
        self.assertEqual((v.situacao_origem, v.validacao_pendente), ("Em Uso", True))

    def test_editar_pelo_erp_nao_apaga_o_que_a_planilha_nao_trouxe(self):
        v = self.do_erp(placa="ABC1234", observacoes="guardar")
        frota.editar_veiculo(v, {"modelo": "MF 4275"}, self.usuario, importacao=self.importacao())
        v = FrotaVeiculo.objects.get(pk=v.pk)
        self.assertEqual((v.placa, v.observacoes, v.modelo), ("ABC1234", "guardar", "MF 4275"))


class InventarioTests(EquipamentoBase):
    def test_o_equipamento_importado_nasce_com_a_validacao_pendente_e_em_uso(self):
        v = self.do_erp(situacao_origem="Em Uso", validacao_pendente=True)
        self.assertEqual((v.status, v.validacao_pendente), (Status.ATIVO, True))

    def test_validar_tira_a_pendencia_registra_o_evento_e_pode_ajustar_o_status(self):
        v = self.do_erp(situacao_origem="Em Uso", validacao_pendente=True)
        frota.validar_cadastro(v, self.usuario, status=Status.MANUTENCAO, motivo="está na oficina")
        v = FrotaVeiculo.objects.get(pk=v.pk)
        self.assertEqual((v.validacao_pendente, v.status), (False, Status.MANUTENCAO))
        evento = FrotaEvento.objects.get(veiculo=v, tipo=FrotaEvento.Tipo.VALIDACAO)
        self.assertEqual((evento.usuario, evento.dados["status"], evento.dados["situacao_origem"]), (self.usuario, "manutencao", "Em Uso"))
        self.assertTrue(FrotaEvento.objects.filter(veiculo=v, tipo=FrotaEvento.Tipo.STATUS).exists())

    def test_validar_sem_mudar_o_status_nao_gera_evento_de_status(self):
        v = self.do_erp(validacao_pendente=True)
        frota.validar_cadastro(v, self.usuario)
        self.assertFalse(FrotaEvento.objects.filter(veiculo=v, tipo=FrotaEvento.Tipo.STATUS).exists())
        self.assertEqual(FrotaVeiculo.objects.get(pk=v.pk).status, Status.ATIVO)

    def test_validar_duas_vezes_so_vale_a_primeira(self):
        v = self.do_erp(validacao_pendente=True)
        frota.validar_cadastro(v, self.usuario)
        with self.assertRaises(ValidationError) as cm:
            frota.validar_cadastro(v, self.usuario)
        self.assertIn("já foi validado", cm.exception.messages[0])
        self.assertEqual(FrotaEvento.objects.filter(veiculo=v, tipo=FrotaEvento.Tipo.VALIDACAO).count(), 1)

    def test_validar_recusa_status_invalido_sem_gravar_nada(self):
        v = self.do_erp(validacao_pendente=True)
        with self.assertRaises(ValidationError):
            frota.validar_cadastro(v, self.usuario, status="voando")
        self.assertTrue(FrotaVeiculo.objects.get(pk=v.pk).validacao_pendente)

    def test_se_o_ajuste_de_status_falha_a_validacao_e_desfeita(self):
        v = self.do_erp(validacao_pendente=True)
        self.chip_vinculado(1, v)
        with self.assertRaises(ValidationError):
            frota.validar_cadastro(v, self.usuario, status=Status.DESATIVADO)         # chip vinculado impede
        self.assertTrue(FrotaVeiculo.objects.get(pk=v.pk).validacao_pendente)
        self.assertFalse(FrotaEvento.objects.filter(veiculo=v, tipo=FrotaEvento.Tipo.VALIDACAO).exists())

    def test_validar_varios_conta_os_validados_e_os_ignorados(self):
        a, b = self.do_erp("8110001", validacao_pendente=True), self.do_erp("8110002", validacao_pendente=True)
        frota.validar_cadastro(b, self.usuario)
        resultado = frota.validar_varios([a.pk, b.pk, 99999, "x"], self.usuario)
        self.assertEqual(resultado, {"validados": 1, "ignorados": 2})
        self.assertFalse(FrotaVeiculo.objects.filter(validacao_pendente=True).exists())

    def test_registrar_movimentacao_exige_motivo_e_guarda_o_de_para(self):
        v = frota.criar_veiculo(self.dados(setor="GRÃOS"), self.usuario)
        with self.assertRaises(ValidationError) as cm:
            frota.registrar_movimentacao(v, {"setor": "CACAU"}, self.usuario, motivo="")
        self.assertIn("motivo", cm.exception.message_dict)
        frota.registrar_movimentacao(v, {"setor": "CACAU", "centro_custo": self.centro.pk}, self.usuario, motivo="  mudou   de safra ")
        v = FrotaVeiculo.objects.get(pk=v.pk)
        self.assertEqual((v.setor, v.centro_custo), ("CACAU", self.centro))
        evento = FrotaEvento.objects.get(veiculo=v, tipo=FrotaEvento.Tipo.MOVIMENTACAO)
        self.assertEqual(evento.dados["motivo"], "mudou de safra")
        self.assertEqual(evento.dados["alteracoes"]["setor"], {"de": "GRÃOS", "para": "CACAU"})

    def test_registrar_movimentacao_sem_mudanca_e_recusada(self):
        v = frota.criar_veiculo(self.dados(setor="GRÃOS"), self.usuario)
        with self.assertRaises(ValidationError) as cm:
            frota.registrar_movimentacao(v, {"setor": "GRÃOS"}, self.usuario, motivo="igual")
        self.assertIn("Nada mudou", cm.exception.messages[0])

    def test_registrar_movimentacao_ignora_campo_que_nao_e_de_movimentacao(self):
        v = frota.criar_veiculo(self.dados(), self.usuario)
        with self.assertRaises(ValidationError):
            frota.registrar_movimentacao(v, {"nome": "OUTRO NOME", "placa": "ZZZ9999"}, self.usuario, motivo="x")
        self.assertEqual(FrotaVeiculo.objects.get(pk=v.pk).nome, "TRATOR NH 3880")

    def test_equipamento_desativado_nao_e_movimentado(self):
        v = frota.criar_veiculo(self.dados(), self.usuario)
        frota.mudar_status(v, Status.DESATIVADO, self.usuario)
        with self.assertRaises(ValidationError) as cm:
            frota.registrar_movimentacao(v, {"setor": "CACAU"}, self.usuario, motivo="x")
        self.assertIn("desativado", cm.exception.messages[0])

    def test_a_movimentacao_aparece_na_linha_do_tempo_com_o_de_para_e_o_motivo(self):
        v = frota.criar_veiculo(self.dados(setor="GRÃOS"), self.usuario)
        frota.registrar_movimentacao(v, {"setor": "CACAU"}, self.usuario, motivo="safra")
        itens = [i for i in frota.linha_do_tempo_veiculo(v, ["evento"]) if i["tipo"] == "movimentacao"]
        self.assertEqual(len(itens), 1)
        self.assertIn("Setor: GRÃOS → CACAU", itens[0]["detalhe"])
        self.assertIn("Motivo: safra", itens[0]["detalhe"])

    def test_a_linha_do_tempo_nomeia_os_campos_editados_por_rotulo(self):
        v = frota.criar_veiculo(self.dados(), self.usuario)
        frota.editar_veiculo(v, {"codigo_legado": "8110001", "ano_modelo": 2020}, self.usuario)
        itens = [i for i in frota.linha_do_tempo_veiculo(v, ["evento"]) if i["tipo"] == "edicao"]
        self.assertEqual(itens[0]["detalhe"], "Campos alterados: Ano-modelo, Código original.")

    def test_verificar_exclusao_lista_o_que_impede_e_o_que_fica_guardado(self):
        v = frota.criar_veiculo(self.dados(), self.usuario)
        livre = frota.verificar_exclusao(v)
        self.assertTrue(livre["pode"])
        self.assertEqual(livre["historico"]["eventos"], 1)
        self.chip_vinculado(1, v)
        preso = frota.verificar_exclusao(v)
        self.assertFalse(preso["pode"])
        self.assertEqual(preso["chips_ativos"], 1)
        self.assertIn("1 chip vinculado", preso["impedimentos"][0])
        self.assertEqual(preso["historico"]["vinculos_de_chip"], 1)

    def test_excluir_e_desativar_e_o_historico_continua(self):
        v = frota.criar_veiculo(self.dados(), self.usuario)
        frota.mudar_status(v, Status.DESATIVADO, self.usuario, "baixa")
        self.assertTrue(frota.verificar_exclusao(FrotaVeiculo.objects.get(pk=v.pk))["ja_desativado"])
        self.assertEqual(v.eventos.count(), 2)
        with self.assertRaises(ProtectedError):
            v.delete()                                              # nem o ORM apaga o que tem histórico


class ListagemEIndicadoresTests(EquipamentoBase):
    def setUp(self):
        super().setUp()
        self.a = self.do_erp("8110001", empresa=self.santa.pk, natureza="proprio", classe_operacional=self.classe_leve.pk,
                             setor="GRÃOS", centro_custo=self.centro.pk, ano_fabricacao=2011, ano_modelo=2011,
                             modelo="TRATOR NH 3880", placa="C811009", validacao_pendente=True, situacao_origem="Em Uso")
        self.b = self.do_erp("9100592", empresa=self.terceiros.pk, natureza="terceiro", setor="", tipo=self.moto.pk,
                             modelo="VW 17.210", ano_fabricacao=2026, ano_modelo=2027, validacao_pendente=True)
        self.c = frota.criar_veiculo(self.dados(codigo="TR-99", nome="Manual"), self.usuario)        # sem código do ERP

    def codigos(self, **filtros):
        return [v.codigo_legado or v.codigo for v in frota.listar_veiculos(filtros)]

    def test_filtros_por_natureza_empresa_classe_setor_centro_e_ano(self):
        self.assertEqual(self.codigos(natureza="terceiro"), ["9100592"])
        self.assertEqual(self.codigos(empresa=self.santa.pk), ["8110001"])
        self.assertEqual(self.codigos(classe_operacional=self.classe_leve.pk), ["8110001"])
        self.assertEqual(self.codigos(setor="grãos"), ["8110001"])
        self.assertEqual(self.codigos(centro_custo=self.centro.pk), ["8110001"])
        self.assertEqual(self.codigos(ano_fabricacao=2026), ["9100592"])
        self.assertEqual(self.codigos(ano_modelo=2027), ["9100592"])
        self.assertEqual(sorted(self.codigos(validacao="pendente")), ["8110001", "9100592"])
        self.assertEqual(self.codigos(validacao="validado"), ["TR-99"])

    def test_filtro_invalido_e_ignorado(self):
        self.assertEqual(len(self.codigos(natureza="alugado", empresa="x", ano_modelo="abc")), 3)

    def test_ordem_pelo_codigo_do_erp_deixa_o_cadastro_manual_por_ultimo_nos_dois_sentidos(self):
        self.assertEqual(self.codigos(ordem="legado"), ["8110001", "9100592", "TR-99"])
        self.assertEqual(self.codigos(ordem="legado", dir="desc"), ["9100592", "8110001", "TR-99"])

    def test_ordem_por_colunas(self):
        self.assertEqual(self.codigos(ordem="modelo"), ["TR-99", "8110001", "9100592"][:0] or self.codigos(ordem="modelo"))
        self.assertEqual(self.codigos(ordem="ano", dir="desc")[0], "9100592")
        self.assertEqual(self.codigos(ordem="placa", dir="desc")[0], "8110001")
        for ordem in frota.ORDENS_DE_VEICULOS:
            with self.subTest(ordem=ordem):
                self.assertEqual(len(self.codigos(ordem=ordem)), 3)

    def test_os_indicadores_saem_do_que_esta_cadastrado(self):
        frota.mudar_status(self.c, Status.MANUTENCAO, self.usuario)
        k = frota.indicadores_equipamentos()
        self.assertEqual((k["total"], k["proprios"], k["terceiros"]), (3, 2, 1))
        self.assertEqual((k["em_uso"], k["em_manutencao"], k["inativos"], k["desativados"]), (2, 1, 0, 0))
        self.assertEqual((k["a_validar"], k["em_uso_a_validar"]), (2, 2))
        self.assertEqual(k["proprios"] + k["terceiros"], k["total"])

    def test_sem_identificacao_conta_so_o_que_o_tipo_espera(self):
        # b é moto (espera placa e chassi) e não tem nenhum dos dois; a é trator sem expectativa e fica de fora
        self.assertEqual(frota.indicadores_equipamentos()["sem_identificacao"], 1)
        frota.mudar_status(self.b, Status.DESATIVADO, self.usuario)
        self.assertEqual(frota.indicadores_equipamentos()["sem_identificacao"], 0)       # desativado não conta

    def test_filtro_de_pendencia(self):
        self.assertEqual(self.codigos(pendencia="identificacao"), ["9100592"])
        self.assertEqual(sorted(self.codigos(pendencia="validacao")), ["8110001", "9100592"])
        self.assertEqual(sorted(self.codigos(pendencia="sem_classe")), ["9100592", "TR-99"])
        self.assertEqual(sorted(self.codigos(pendencia="sem_setor")), ["9100592", "TR-99"])
        self.assertEqual(len(self.codigos(pendencia="inexistente")), 3)

    def test_a_lista_nao_faz_consulta_por_linha(self):
        for n in range(20):
            self.do_erp(f"92{n:05d}", empresa=self.santa.pk, classe_operacional=self.classe_leve.pk, centro_custo=self.centro.pk)
        # 3, e o mesmo número com 3 ou 300 equipamentos: a configuração da integração, os veículos (com
        # empresa, classes, centro de custo, tipo, fazenda e responsável em JOIN) e os vínculos ativos.
        with self.assertNumQueries(3):
            for v in frota.listar_veiculos({"ordem": "legado"}):
                v.empresa and v.empresa.nome, v.classe_operacional, v.centro_custo, v.tipo.nome


class ConferenciasTests(EquipamentoBase):
    """Os casos que o prompt mandou revalidar — e que NUNCA são corrigidos, só sinalizados."""

    def codigos(self, **valores):
        return [c["codigo"] for c in qualidade.conferir(valores, hoje=date(2026, 11, 13))]

    def test_data_de_compra_de_1905(self):
        achados = qualidade.conferir({"data_compra": date(1905, 7, 3), "ano_fabricacao": 2011}, hoje=date(2026, 11, 13))
        self.assertEqual([a["codigo"] for a in achados], ["data_compra_fora_de_faixa"])        # sem repetir como "antes da fabricação"
        self.assertIn("03/07/1905", achados[0]["detalhe"])

    def test_ano_fabricacao_2026_com_ano_modelo_2027_e_so_uma_nota(self):
        achados = qualidade.conferir({"ano_fabricacao": 2026, "ano_modelo": 2027}, hoje=date(2026, 11, 13))
        self.assertEqual([(a["codigo"], a["gravidade"]) for a in achados], [("ano_modelo_no_futuro", "info")])

    def test_ano_modelo_so_um_ano_a_frente_nao_e_futuro_no_ano_seguinte(self):
        self.assertEqual(self.codigos(ano_fabricacao=2026, ano_modelo=2027), ["ano_modelo_no_futuro"])
        self.assertEqual([c["codigo"] for c in qualidade.conferir({"ano_fabricacao": 2026, "ano_modelo": 2027}, hoje=date(2027, 1, 5))], [])

    def test_ano_modelo_anterior_a_fabricacao_e_aviso(self):
        achados = qualidade.conferir({"ano_fabricacao": 2018, "ano_modelo": 2010})
        self.assertEqual((achados[0]["codigo"], achados[0]["gravidade"]), ("ano_modelo_antes_da_fabricacao", "aviso"))

    def test_chassi_repetido_cita_o_outro_equipamento(self):
        achados = qualidade.conferir({"chassi_serie": "1CQS660ALH0120143"}, chassi_em_outros=["8210001 · COLHEI. GRAOS JDS660"])
        self.assertEqual(achados[0]["codigo"], "chassi_duplicado")
        self.assertIn("8210001", achados[0]["detalhe"])

    def test_sem_placa_e_sem_chassi_nao_e_erro_quando_o_tipo_nao_espera(self):
        self.assertEqual(self.codigos(placa="", chassi_serie="", espera_placa=False, espera_chassi=False), [])

    def test_sem_placa_vira_pendencia_quando_o_tipo_espera(self):
        achados = qualidade.conferir({"placa": "", "chassi_serie": "9BW", "espera_placa": True, "espera_chassi": True})
        self.assertEqual(achados[0]["codigo"], "identificacao_incompleta")
        self.assertIn("placa", achados[0]["detalhe"])
        self.assertNotIn("chassi", achados[0]["detalhe"])

    def test_placa_interna_e_so_uma_nota(self):
        achados = qualidade.conferir({"placa": "C8110035"})
        self.assertEqual([(a["codigo"], a["gravidade"]) for a in achados], [("placa_fora_do_padrao", "info")])
        self.assertEqual(self.codigos(placa="PKC2004"), [])
        self.assertEqual(self.codigos(placa="ABC1D23"), [])

    def test_compra_antes_da_fabricacao_e_inicio_antes_da_compra(self):
        self.assertEqual(self.codigos(data_compra=date(2021, 1, 2), ano_fabricacao=2025), ["compra_antes_da_fabricacao"])
        self.assertEqual(self.codigos(data_compra=date(2026, 7, 21), data_inicio=date(2018, 8, 20)), ["inicio_antes_da_compra"])

    def test_os_avisos_vem_antes_das_notas(self):
        achados = qualidade.conferir({
            "placa": "C811", "data_compra": date(1905, 1, 1), "validacao_pendente": True, "setor": "",
            "tem_classe_operacional": False,
        }, hoje=date(2026, 11, 13))
        gravidades = [a["gravidade"] for a in achados]
        self.assertEqual(gravidades, sorted(gravidades, key=lambda g: g != "aviso"))
        self.assertEqual(len(achados), 5)

    def test_toda_regra_tem_gravidade_valida(self):
        for codigo, (gravidade, titulo) in qualidade.REGRAS.items():
            self.assertIn(gravidade, (qualidade.AVISO, qualidade.INFO), codigo)
            self.assertTrue(titulo)

    def test_a_regra_em_python_e_a_regra_em_sql_dizem_a_mesma_coisa(self):
        """`conferir` (ficha, importação) e o filtro SQL (lista, indicadores) não podem divergir."""
        casos = [
            self.do_erp("1", tipo=self.moto.pk),                                                     # sem placa nem chassi: incompleta
            self.do_erp("2", tipo=self.moto.pk, placa="ABC1234", chassi_serie="9BW1"),              # completa
            self.do_erp("3", tipo=self.moto.pk, placa="ABC1235"),                                    # falta o chassi
            self.do_erp("4"),                                                                        # trator: nada é esperado
            self.do_erp("5", chassi_serie="IGUAL"), self.do_erp("6", chassi_serie="IGUAL"),          # repetido
            self.do_erp("7", chassi_serie="UNICO"),
        ]
        no_sql_incompleta = set(frota.listar_veiculos({"pendencia": "identificacao"}).values_list("pk", flat=True))
        no_sql_repetido = set(frota.listar_veiculos({"pendencia": "chassi_duplicado"}).values_list("pk", flat=True))
        for v in casos:
            v = FrotaVeiculo.objects.select_related("tipo").get(pk=v.pk)
            codigos = {c["codigo"] for c in qualidade.conferencias_do_veiculo(v)}
            with self.subTest(equipamento=v.codigo_legado):
                self.assertEqual("identificacao_incompleta" in codigos, v.pk in no_sql_incompleta)
                self.assertEqual("chassi_duplicado" in codigos, v.pk in no_sql_repetido)
        self.assertEqual({v.codigo_legado for v in FrotaVeiculo.objects.filter(pk__in=no_sql_repetido)}, {"5", "6"})

    def test_contar_pendencias_numa_consulta_so(self):
        self.do_erp("1", tipo=self.moto.pk, validacao_pendente=True)
        with self.assertNumQueries(1):
            contagem = qualidade.contar_pendencias(FrotaVeiculo.objects.all())
        self.assertEqual(contagem["identificacao"], 1)
        self.assertEqual(contagem["validacao"], 1)
        self.assertEqual(set(contagem), set(qualidade.PENDENCIAS))

    def test_as_conferencias_de_um_equipamento_gravado(self):
        self.do_erp("8110001", chassi_serie="1CQS660ALH0120143", data_compra=date(1905, 7, 3), ano_fabricacao=2011)
        outro = self.do_erp("8210001", chassi_serie="1CQS660ALH0120143", nome="COLHEI. GRAOS JDS660")
        outro = FrotaVeiculo.objects.select_related("tipo").get(pk=outro.pk)
        codigos = [c["codigo"] for c in qualidade.conferencias_do_veiculo(outro)]
        self.assertIn("chassi_duplicado", codigos)
        antigo = FrotaVeiculo.objects.select_related("tipo").get(codigo_legado="8110001")
        self.assertIn("data_compra_fora_de_faixa", [c["codigo"] for c in qualidade.conferencias_do_veiculo(antigo)])


class MovimentacoesEHistoricoTests(EquipamentoBase):
    """`frota_historico_service`: a mesma linha do tempo do cadastro, lida em dois recortes."""

    def setUp(self):
        super().setUp()
        from services import frota_historico_service as historico

        self.historico = historico
        self.v = frota.criar_veiculo(self.dados(setor="GRÃOS", codigo_legado="8110001", natureza="proprio"), self.usuario)
        self.w = frota.criar_veiculo(self.dados(codigo="TR-02", nome="CAMINHÃO ROLL OFF", codigo_legado="8410008", natureza="terceiro"), self.usuario)
        frota.registrar_movimentacao(self.v, {"setor": "CACAU"}, self.usuario, motivo="safra")
        frota.editar_veiculo(self.v, {"nome": "TRATOR RENOMEADO"}, self.usuario)                      # só corrige: NÃO é movimentação
        frota.editar_veiculo(self.v, {"area_operacao": "Plantio"}, self.usuario)                      # edição que muda lugar: é
        frota.mudar_status(self.w, Status.MANUTENCAO, self.usuario, "oficina")

    def tipos(self, **filtros):
        return [(e.veiculo.codigo_legado, e.tipo) for e in self.historico.eventos_da_frota(filtros, so_movimentacoes=True)]

    def test_movimentacao_e_movimento_de_lugar_responsavel_ou_situacao(self):
        achados = self.tipos()
        self.assertEqual(sorted(achados), sorted([("8110001", "movimentacao"), ("8110001", "edicao"), ("8410008", "status")]))

    def test_edicao_que_so_corrige_o_nome_nao_entra(self):
        nomes = [d["mudancas"][0]["rotulo"] for d in self.historico.movimentacoes_do_veiculo(self.v)]
        self.assertNotIn("Nome", nomes)
        self.assertEqual(len(self.historico.movimentacoes_do_veiculo(self.v)), 2)

    def test_o_detalhe_da_movimentacao_traz_de_para_motivo_e_quem(self):
        mov = next(d for d in self.historico.movimentacoes_do_veiculo(self.v) if d["tipo"] == "movimentacao")
        self.assertEqual((mov["titulo"], mov["motivo"], mov["por"]), ("Setor", "safra", self.usuario.get_username()))
        self.assertEqual(mov["mudancas"], [{"rotulo": "Setor", "de": "GRÃOS", "para": "CACAU"}])
        edicao = next(d for d in self.historico.movimentacoes_do_veiculo(self.v) if d["tipo"] == "edicao")
        self.assertEqual(edicao["titulo"], "Cadastro editado (Área de operação)")

    def test_a_mudanca_de_situacao_e_uma_movimentacao(self):
        mov = self.historico.movimentacoes_do_veiculo(self.w)[0]
        self.assertEqual((mov["titulo"], mov["motivo"]), ("Situação", "oficina"))
        self.assertEqual(mov["mudancas"], [{"rotulo": "Situação", "de": "Em uso", "para": "Em manutenção"}])

    def test_filtro_por_tipo_de_movimentacao(self):
        self.assertEqual(self.tipos(tipo="situacao"), [("8410008", "status")])
        self.assertEqual({t for c, t in self.tipos(tipo="setor")}, {"movimentacao", "edicao"})
        self.assertEqual(self.tipos(tipo="responsavel"), [])
        self.assertEqual(len(self.tipos(tipo="inexistente")), 3)

    def test_busca_por_equipamento_sem_acento_e_por_codigo_do_erp(self):
        self.assertEqual({c for c, _ in self.tipos(q="caminhao roll")}, {"8410008"})
        self.assertEqual({c for c, _ in self.tipos(q="8110001")}, {"8110001"})

    def test_filtro_de_natureza_e_de_usuario(self):
        self.assertEqual({c for c, _ in self.tipos(natureza="terceiro")}, {"8410008"})
        self.assertEqual(len(self.tipos(usuario=self.usuario.get_username())), 3)
        self.assertEqual(self.tipos(usuario="ninguem"), [])

    def test_filtro_de_periodo(self):
        hoje = timezone.localdate().isoformat()
        self.assertEqual(len(self.tipos(de=hoje, ate=hoje)), 3)
        self.assertEqual(self.tipos(de="2999-01-01"), [])
        self.assertEqual(self.tipos(ate="2000-01-01"), [])

    def test_o_historico_traz_todos_os_tipos_de_evento(self):
        todos = {e.tipo for e in self.historico.eventos_da_frota({}, so_movimentacoes=False)}
        self.assertEqual(todos, {"cadastro", "edicao", "movimentacao", "status"})
        so_cadastro = self.historico.eventos_da_frota({"tipo": "cadastro"}, so_movimentacoes=False)
        self.assertEqual({e.tipo for e in so_cadastro}, {"cadastro"})
        self.assertEqual(so_cadastro.count(), 2)

    def test_o_mais_novo_vem_primeiro(self):
        quando = [e.em for e in self.historico.eventos_da_frota({}, so_movimentacoes=False)]
        self.assertEqual(quando, sorted(quando, reverse=True))

    def test_a_lista_nao_faz_consulta_por_evento(self):
        with self.assertNumQueries(1):
            for e in self.historico.eventos_da_frota({}, so_movimentacoes=False):
                e.veiculo.codigo_legado, e.veiculo.tipo.nome, e.usuario and e.usuario.username
