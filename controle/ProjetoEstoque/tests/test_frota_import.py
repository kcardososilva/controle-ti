"""
Frotas e Conectividade — importação de chips por planilha (Fase D).

O que estes testes protegem — a importação é a única porta por onde vem dado em lote, e
quase tudo que dá errado nela dá errado em SILÊNCIO:

  • NUNCA ATUALIZA — chip que já existe fica como está (reimportar não desfaz ajuste manual).
  • REVALIDA AO CONFIRMAR — a prévia pode estar velha; cada linha é conferida de novo e
    grava num `atomic()` próprio (uma linha ruim não derruba as outras).
  • CONFLITO ICCID x IMSI — o mesmo número em dois chips diferentes é rejeitado, citando os dois.
  • PLANILHA É ENTRADA HOSTIL — limites de tamanho e de linhas, zip inflado, e NENHUMA
    fórmula é executada: valor que começa com =, +, - ou @ é texto (e o Excel de relatório o escapa).
  • TOTAIS QUE FECHAM — o relatório sai das linhas, não de um contador.
"""

import csv
import io
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from openpyxl import Workbook, load_workbook

from ProjetoEstoque.models import (
    ConectividadeOperadora,
    FrotaImportacaoChips,
    FrotaMovimentacao,
    Localidade,
    M2MChip,
)
from ProjetoEstoque.tests import test_m2m as base
from services import frota_import_service as imp
from services import frota_vinculo_service as vinculos

CABECALHO = imp.ROTULOS     # ICCID, IMSI, Número da linha, Operadora, Identificação, Descrição, Unidade, Observações


def linha(n=None, *, iccid=None, imsi="", numero="", operadora="", nome="", descricao="", unidade="", obs=""):
    """Uma linha de dados; `n` gera um ICCID válido (a menos que `iccid` venha)."""
    if iccid is None:
        iccid = base.iccid_valido(n) if n else ""
    return [iccid, imsi, numero, operadora, nome, descricao, unidade, obs]


def xlsx_bytes(linhas, cabecalho=None, aba="Chips", como_texto=()):
    """Um .xlsx real. `como_texto`: (linha, coluna) cujo valor é gravado como TEXTO mesmo começando com `=`."""
    wb = Workbook()
    ws = wb.active
    ws.title = aba
    ws.append(cabecalho or CABECALHO)
    for valores in linhas:
        ws.append(list(valores))
    for r, c in como_texto:
        ws.cell(row=r, column=c).data_type = "s"
    saida = io.BytesIO()
    wb.save(saida)
    return saida.getvalue()


def csv_bytes(linhas, sep=";", cabecalho=None, codificacao="utf-8-sig"):
    saida = io.StringIO(newline="")
    escritor = csv.writer(saida, delimiter=sep, lineterminator="\r\n")
    escritor.writerow(cabecalho or CABECALHO)
    escritor.writerows([[str(c) for c in valores] for valores in linhas])
    return saida.getvalue().encode(codificacao)


def upload(nome, dados):
    return SimpleUploadedFile(nome, dados)


class ImportBase(base.M2MBase):
    def setUp(self):
        super().setUp()
        self.outro = User.objects.create(username="outro_usuario")
        self.fazenda = Localidade.objects.create(local="Fazenda Norte")

    def analisar_xlsx(self, linhas, **kw):
        return imp.analisar(upload("chips.xlsx", xlsx_bytes(linhas, **kw)), self.usuario)

    def situacoes(self, importacao):
        return {l.numero: l.situacao for l in importacao.itens.all()}

    def linha_de(self, importacao, numero):
        return importacao.itens.get(numero=numero)


# ═════════════════════════════════════════════════════════════
# Arquivos-modelo
# ═════════════════════════════════════════════════════════════

class ModeloTests(ImportBase):
    def test_modelo_xlsx_tem_as_tres_abas_o_cabecalho_a_lista_de_operadoras_e_colunas_de_texto(self):
        wb = load_workbook(io.BytesIO(imp.modelo_xlsx()))
        self.assertEqual(wb.sheetnames, ["Chips", "Instruções", "Listas"])
        chips = wb["Chips"]
        self.assertEqual([c.value for c in chips[1]], CABECALHO)
        self.assertEqual(chips.max_row, 501)                                                   # linhas pré-formatadas, sem dado
        self.assertEqual(chips["A2"].value, None)
        for coluna in "ABC":                                                                   # ICCID, IMSI e linha como TEXTO
            self.assertEqual(chips[f"{coluna}2"].number_format, "@")
        listas = wb["Listas"]
        self.assertEqual([c.value for c in listas["A"]][:2], ["Operadoras (ativas)", "TIM"])
        self.assertIn("Fazenda Norte", [c.value for c in listas["B"]])
        validacoes = {str(v.sqref): v for v in chips.data_validations.dataValidation}
        self.assertEqual(validacoes["D2:D501"].type, "list")                                   # operadora: lista
        self.assertIn("Listas!$A$2:$A$2", validacoes["D2:D501"].formula1)
        self.assertEqual(validacoes["G2:G501"].type, "list")                                   # unidade: lista
        instrucoes = " ".join(str(c.value or "") for c in wb["Instruções"]["A"])
        for trecho in ("ICCID", "Não atualiza chip que já existe", "5 MB", "5.000 linhas"):
            self.assertIn(trecho, instrucoes)

    def test_a_lista_de_operadoras_so_traz_as_ativas(self):
        ConectividadeOperadora.objects.create(nome="Vivo", integracao="", ativo=True)
        ConectividadeOperadora.objects.create(nome="Antiga", integracao="", ativo=False)
        wb = load_workbook(io.BytesIO(imp.modelo_xlsx()))
        nomes = [c.value for c in wb["Listas"]["A"] if c.value]
        self.assertEqual(nomes, ["Operadoras (ativas)", "TIM", "Vivo"])

    def test_o_modelo_xlsx_volta_para_dentro_do_sistema(self):
        """O arquivo-modelo, sem nenhuma linha, é lido como "só o cabeçalho" — o caminho que o usuário faz."""
        with self.assertRaises(ValidationError) as cm:
            imp.analisar(upload("modelo.xlsx", imp.modelo_xlsx()), self.usuario)
        self.assertIn("nenhuma linha de dados", cm.exception.messages[0])

    def test_modelo_csv_utf8_com_bom_e_ponto_e_virgula(self):
        dados = imp.modelo_csv()
        self.assertTrue(dados.startswith(b"\xef\xbb\xbf"))
        texto = dados.decode("utf-8-sig")
        self.assertEqual(texto.strip().split(";"), CABECALHO)
        self.assertIn("Número da linha", texto)


# ═════════════════════════════════════════════════════════════
# Leitura: formatos, codificação, separadores, cabeçalho, limites
# ═════════════════════════════════════════════════════════════

class LeituraTests(ImportBase):
    def test_xlsx_gera_uma_previa_sem_gravar_nada_nos_chips(self):
        imp_ = self.analisar_xlsx([linha(1, nome="Pivô 1"), linha(2, nome="Pivô 2")])
        self.assertEqual(imp_.estado, "previa")
        self.assertEqual((imp_.formato, imp_.arquivo_nome, len(imp_.sha256)), ("xlsx", "chips.xlsx", 64))
        self.assertEqual((imp_.linhas, imp_.validas, imp_.importados, imp_.existentes, imp_.rejeitados), (2, 2, 0, 0, 0))
        self.assertEqual(imp_.criada_por, self.usuario)
        self.assertEqual(M2MChip.objects.count(), 0)
        self.assertEqual(FrotaMovimentacao.objects.count(), 0)
        self.assertEqual(self.situacoes(imp_), {2: "valida", 3: "valida"})                      # o número da linha é o da planilha

    def test_csv_em_utf8_com_ponto_e_virgula(self):
        dados = csv_bytes([linha(1, nome="Pivô São José", descricao="Pesagem"), linha(2, nome="Silo")])
        imp_ = imp.analisar(upload("chips.csv", dados), self.usuario)
        self.assertEqual((imp_.formato, imp_.validas), ("csv", 2))
        self.assertEqual(self.linha_de(imp_, 2).dados["identificacao"], "Pivô São José")

    def test_csv_em_cp1252_com_acentos(self):
        dados = csv_bytes([linha(1, nome="Pivô São José"), linha(2, nome="Açude")], codificacao="cp1252")
        imp_ = imp.analisar(upload("chips.csv", dados), self.usuario)
        self.assertEqual(imp_.validas, 2)
        self.assertEqual({l.dados["identificacao"] for l in imp_.itens.all()}, {"Pivô São José", "Açude"})

    def test_csv_com_virgula_e_com_tab(self):
        for sep, nome in ((",", "virgula.csv"), ("\t", "tab.csv")):
            with self.subTest(sep=repr(sep)):
                imp_ = imp.analisar(upload(nome, csv_bytes([linha(1, nome="A"), linha(2, nome="B")], sep=sep)), self.usuario)
                self.assertEqual(imp_.validas, 2)

    def test_csv_com_campo_entre_aspas_e_virgula_dentro(self):
        texto = "ICCID,IMSI,Número da linha,Operadora,Identificação,Descrição,Unidade,Observações\r\n"
        texto += f'{base.iccid_valido(1)},,,,Chip,"Pesagem, balança 2",,\r\n'
        imp_ = imp.analisar(upload("a.csv", texto.encode("utf-8")), self.usuario)
        self.assertEqual(self.linha_de(imp_, 2).dados["descricao"], "Pesagem, balança 2")

    def test_cabecalho_reconhecido_sem_distinguir_acento_e_caixa_e_em_qualquer_ordem(self):
        cabecalho = ["IDENTIFICACAO", "iccid", "numero DA linha", "OPERADORA", "Descricao", "imsi", "unidade", "observacoes"]
        imp_ = self.analisar_xlsx([["Pivô", base.iccid_valido(1), "77999991234", "tim", "d", "", "", ""]], cabecalho=cabecalho)
        d = self.linha_de(imp_, 2).dados
        self.assertEqual((imp_.validas, d["identificacao"], d["numero_linha"], d["operadora"]), (1, "Pivô", "77999991234", "TIM"))

    def test_so_a_coluna_imsi_basta_no_cabecalho(self):
        imp_ = self.analisar_xlsx([[base.imsi_valido(1)]], cabecalho=["IMSI"])
        self.assertEqual(imp_.validas, 1)
        self.assertEqual(self.linha_de(imp_, 2).dados["identificacao"], "Chip " + base.imsi_valido(1)[-6:])

    def test_cabecalho_invalido_e_recusado_com_a_lista_das_colunas_esperadas(self):
        for cabecalho in (["Nome", "Telefone", "Cidade"], ["Operadora", "Identificação"]):
            with self.subTest(cabecalho=cabecalho), self.assertRaises(ValidationError) as cm:
                imp.analisar(upload("c.xlsx", xlsx_bytes([["x"] * len(cabecalho)], cabecalho=cabecalho)), self.usuario)
            texto = cm.exception.messages[0]
            self.assertIn("Cabeçalho não reconhecido", texto)
            self.assertIn("Colunas esperadas: ICCID, IMSI, Número da linha, Operadora, Identificação, Descrição, Unidade, Observações", texto)
        self.assertEqual(FrotaImportacaoChips.objects.count(), 0)

    def test_arquivos_que_nao_servem(self):
        casos = {
            "extensão errada": (upload("chips.xls", b"x"), "Envie uma planilha"),
            "vazio": (upload("chips.csv", b""), "vazio"),
            "xlsx que não é zip": (upload("chips.xlsx", b"isto nao e uma planilha"), "planilha .xlsx válida"),
            "xlsx quebrado": (upload("chips.xlsx", b"PK\x03\x04quebrado"), "planilha .xlsx válida"),
            "excel com extensão csv": (upload("chips.csv", xlsx_bytes([linha(1)])), "extensão .csv"),
            "só cabeçalho": (upload("chips.csv", csv_bytes([])), "nenhuma linha de dados"),
        }
        for rotulo, (arquivo, trecho) in casos.items():
            with self.subTest(caso=rotulo), self.assertRaises(ValidationError) as cm:
                imp.analisar(arquivo, self.usuario)
            self.assertIn(trecho, cm.exception.messages[0])
        self.assertEqual(FrotaImportacaoChips.objects.count(), 0)

    def test_csv_que_nao_decodifica(self):
        # 0x81 não existe em cp1252 e quebra o UTF-8: nenhuma das duas leituras serve
        with self.assertRaises(ValidationError) as cm:
            imp.analisar(upload("c.csv", b"ICCID;IMSI\r\n\x81\x8d;\x8f\r\n"), self.usuario)
        self.assertIn("UTF-8", cm.exception.messages[0])

    def test_limite_de_5_mb(self):
        grande = b"ICCID;IMSI\r\n" + b"x" * (5 * 1024 * 1024)
        with self.assertRaises(ValidationError) as cm:
            imp.analisar(upload("grande.csv", grande), self.usuario)
        self.assertIn("5 MB", cm.exception.messages[0])

    def test_limite_de_5000_linhas_vale_no_xlsx_e_no_csv(self):
        linhas = [[base.iccid_valido(1)]] * 5001
        with self.assertRaises(ValidationError) as cm:
            imp.analisar(upload("m.csv", csv_bytes(linhas, cabecalho=["ICCID"])), self.usuario)
        self.assertIn("mais de 5.000 linhas", cm.exception.messages[0])
        with self.assertRaises(ValidationError):
            imp.analisar(upload("m.xlsx", xlsx_bytes(linhas, cabecalho=["ICCID"])), self.usuario)
        # 5.000 exatas passam do limite de leitura (aqui só conferimos que o teto não pega a de 5.000)
        arquivo = imp._carregar(upload("m.csv", csv_bytes([[base.iccid_valido(1)]] * 5000, cabecalho=["ICCID"])))
        self.assertEqual(len(arquivo.linhas), 5000)

    def test_xlsx_inflado_e_recusado(self):
        with mock.patch.object(imp, "MAX_DESCOMPACTADO", 100):
            with self.assertRaises(ValidationError) as cm:
                imp.analisar(upload("c.xlsx", xlsx_bytes([linha(1)])), self.usuario)
        self.assertIn("grande demais", cm.exception.messages[0])

    def test_so_a_aba_chips_e_lida_se_ela_existir(self):
        wb = Workbook()
        outra = wb.active
        outra.title = "Instruções"
        outra.append(["texto solto"])
        chips = wb.create_sheet("Chips")
        chips.append(["ICCID"])
        chips.append([base.iccid_valido(1)])
        saida = io.BytesIO()
        wb.save(saida)
        imp_ = imp.analisar(upload("c.xlsx", saida.getvalue()), self.usuario)
        self.assertEqual(imp_.validas, 1)

    def test_linhas_em_branco_no_meio_sao_ignoradas_e_a_numeracao_e_a_da_planilha(self):
        imp_ = self.analisar_xlsx([linha(1), [None] * 8, linha(2)])
        self.assertEqual(self.situacoes(imp_), {2: "valida", 4: "valida"})

    def test_a_previa_vale_24_horas(self):
        imp_ = self.analisar_xlsx([linha(1)])
        self.assertAlmostEqual((imp_.expira_em - imp_.criada_em).total_seconds(), 24 * 3600, delta=1)
        self.assertFalse(imp_.expirada)
        FrotaImportacaoChips.objects.filter(pk=imp_.pk).update(criada_em=timezone.now() - timedelta(hours=25))
        imp_.refresh_from_db()
        self.assertTrue(imp_.expirada)

    def test_analisar_nao_faz_uma_consulta_por_linha(self):
        with CaptureQueriesContext(connection) as poucas:
            self.analisar_xlsx([linha(n, nome=f"Chip A{n}") for n in range(1, 6)])
        with CaptureQueriesContext(connection) as muitas:
            self.analisar_xlsx([linha(n, nome=f"Chip B{n}") for n in range(20, 80)])
        self.assertEqual(len(poucas), len(muitas), "a prévia fez consultas proporcionais ao número de linhas")


# ═════════════════════════════════════════════════════════════
# Validação por linha
# ═════════════════════════════════════════════════════════════

class ValidacaoTests(ImportBase):
    def motivos(self, importacao, numero=2):
        return " | ".join(self.linha_de(importacao, numero).motivos)

    def test_sem_iccid_nem_imsi_rejeita(self):
        imp_ = self.analisar_xlsx([linha(None, nome="Sem número")])
        self.assertEqual(self.situacoes(imp_), {2: "rejeitada"})
        self.assertIn("Informe o ICCID ou o IMSI", self.motivos(imp_))

    def test_iccid_imsi_e_numero_invalidos_rejeitam_com_o_motivo_de_cada_um(self):
        iccid_ruim = base.iccid_valido(1)[:-1] + str((int(base.iccid_valido(1)[-1]) + 1) % 10)
        imp_ = self.analisar_xlsx([
            linha(iccid=iccid_ruim),                                    # 2: dígito verificador
            linha(1, imsi="123"),                                       # 3: IMSI curto
            linha(2, numero="abc"),                                     # 4: número com letras
            linha(iccid="1234"),                                        # 5: ICCID curto
        ])
        self.assertEqual(set(self.situacoes(imp_).values()), {"rejeitada"})
        self.assertIn("dígito verificador", self.motivos(imp_, 2))
        self.assertIn("IMSI deve ter 15 dígitos", self.motivos(imp_, 3))
        self.assertIn("número da linha", self.motivos(imp_, 4))
        self.assertIn("19 ou 20 dígitos", self.motivos(imp_, 5))

    def test_so_imsi_e_aceito_e_vira_o_identificador_de_consulta(self):
        imp_ = self.analisar_xlsx([linha(None, imsi=base.imsi_valido(7))])
        self.assertEqual(self.situacoes(imp_), {2: "valida"})
        imp.confirmar(imp_, self.usuario)
        chip = M2MChip.objects.get()
        self.assertEqual((chip.iccid, chip.imsi, chip.identificador_consulta), ("", base.imsi_valido(7), "imsi"))

    def test_duplicidade_dentro_do_arquivo(self):
        imp_ = self.analisar_xlsx([
            linha(1, nome="A"),
            linha(1, nome="B"),                                                  # ICCID repetido da linha 2
            linha(2, imsi=base.imsi_valido(5), nome="C"),
            linha(3, imsi=base.imsi_valido(5), nome="D"),                        # IMSI repetido da linha 4
            linha(4, nome="a"),                                                  # identificação repetida (sem distinguir caixa) da linha 2
        ])
        self.assertEqual(self.situacoes(imp_), {2: "valida", 3: "rejeitada", 4: "valida", 5: "rejeitada", 6: "rejeitada"})
        self.assertIn("ICCID repetido: já aparece na linha 2", self.motivos(imp_, 3))
        self.assertIn("IMSI repetido: já aparece na linha 4", self.motivos(imp_, 5))
        self.assertIn("repetida: já aparece na linha 2", self.motivos(imp_, 6))
        self.assertEqual((imp_.validas, imp_.rejeitados), (2, 3))

    def test_identificacao_vazia_vira_chip_mais_os_seis_ultimos_digitos(self):
        imp_ = self.analisar_xlsx([linha(1), linha(None, imsi=base.imsi_valido(9))])
        d = self.linha_de(imp_, 2).dados
        self.assertEqual((d["identificacao"], d["identificacao_gerada"]), ("Chip " + base.iccid_valido(1)[-6:], True))
        self.assertEqual(self.linha_de(imp_, 3).dados["identificacao"], "Chip " + base.imsi_valido(9)[-6:])

    def test_nome_ja_usado_por_outro_chip_rejeita(self):
        self.chip(1, nome="Pivô 3")
        imp_ = self.analisar_xlsx([linha(2, nome="pivô 3")])
        self.assertEqual(self.situacoes(imp_), {2: "rejeitada"})
        self.assertIn("Já existe um chip com a identificação", self.motivos(imp_))

    def test_chip_existente_e_contado_e_nunca_sobrescrito(self):
        original = self.chip(1, nome="Original", descricao="Descrição que o TI ajustou", setor="Algodoeira")
        imp_ = self.analisar_xlsx([
            linha(1, nome="Nome da planilha", descricao="Outra descrição", unidade="Fazenda Norte", numero="77999991234"),
            linha(2, nome="Novo"),
        ])
        self.assertEqual(self.situacoes(imp_), {2: "existente", 3: "valida"})
        self.assertEqual(self.linha_de(imp_, 2).chip, original)
        self.assertEqual((imp_.existentes, imp_.validas), (1, 1))
        imp.confirmar(imp_, self.usuario)
        original.refresh_from_db()
        self.assertEqual((original.nome, original.descricao, original.setor, original.numero_linha, original.localidade_id),
                         ("Original", "Descrição que o TI ajustou", "Algodoeira", "", None))
        self.assertEqual(M2MChip.objects.count(), 2)
        self.assertEqual(self.linha_de(imp_, 2).situacao, "existente")

    def test_conflito_iccid_de_um_chip_e_imsi_de_outro_cita_os_dois(self):
        a = self.chip(1, nome="Chip A", imsi=base.imsi_valido(1))
        b = self.chip(2, nome="Chip B", imsi=base.imsi_valido(2))
        imp_ = self.analisar_xlsx([linha(1, imsi=base.imsi_valido(2), nome="Cruzado")])
        self.assertEqual(self.situacoes(imp_), {2: "rejeitada"})
        motivo = self.motivos(imp_)
        self.assertIn("o ICCID pertence ao chip \"Chip A\" e o IMSI ao chip \"Chip B\"", motivo)
        self.assertEqual((M2MChip.objects.count(), M2MChip.objects.get(pk=a.pk).imsi, M2MChip.objects.get(pk=b.pk).imsi),
                         (2, base.imsi_valido(1), base.imsi_valido(2)))

    def test_conflito_chip_existe_com_imsi_ou_iccid_diferente(self):
        self.chip(1, nome="Chip A", imsi=base.imsi_valido(1))
        self.chip(2, nome="Chip B", imsi=base.imsi_valido(2))
        imp_ = self.analisar_xlsx([
            linha(1, imsi=base.imsi_valido(9)),                        # 2: o ICCID é do chip A, que tem OUTRO IMSI
            linha(8, imsi=base.imsi_valido(2)),                        # 3: o IMSI é do chip B, que tem OUTRO ICCID
        ])
        self.assertEqual(self.situacoes(imp_), {2: "rejeitada", 3: "rejeitada"})
        self.assertIn("já está cadastrado com outro IMSI", self.motivos(imp_, 2))
        self.assertIn(base.imsi_valido(1), self.motivos(imp_, 2))
        self.assertIn("já está cadastrado com outro ICCID", self.motivos(imp_, 3))

    def test_chip_existente_sem_imsi_e_linha_com_imsi_nao_e_conflito(self):
        self.chip(1, nome="Chip A")
        imp_ = self.analisar_xlsx([linha(1, imsi=base.imsi_valido(1))])
        self.assertEqual(self.situacoes(imp_), {2: "existente"})

    def test_unidade_inexistente_rejeita_dizendo_qual_e_a_existente_casa_sem_acento_e_caixa(self):
        imp_ = self.analisar_xlsx([
            linha(1, unidade="Fazenda Inventada"),
            linha(2, unidade="  fazenda   NORTE "),
            linha(3),
        ])
        self.assertEqual(self.situacoes(imp_), {2: "rejeitada", 3: "valida", 4: "valida"})
        self.assertIn('Unidade "Fazenda Inventada" não está cadastrada', self.motivos(imp_, 2))
        self.assertEqual(self.linha_de(imp_, 3).dados["unidade_id"], self.fazenda.pk)
        self.assertEqual(Localidade.objects.count(), 1)                          # a unidade NÃO é criada
        imp.confirmar(imp_, self.usuario)
        self.assertEqual(M2MChip.objects.get(iccid=base.iccid_valido(2)).localidade, self.fazenda)

    def test_unidade_com_nome_repetido_no_cadastro_e_ambigua(self):
        Localidade.objects.create(local="Fazenda Norte")
        imp_ = self.analisar_xlsx([linha(1, unidade="Fazenda Norte")])
        self.assertEqual(self.situacoes(imp_), {2: "rejeitada"})
        self.assertIn("mais de uma unidade", self.motivos(imp_))

    def test_operadora_inexistente_ou_inativa_rejeita_vazia_vale_tim_e_sem_integracao_e_aceita(self):
        ConectividadeOperadora.objects.create(nome="Antiga", integracao="", ativo=False)
        vivo = ConectividadeOperadora.objects.create(nome="Vivo", integracao="", ativo=True)
        imp_ = self.analisar_xlsx([
            linha(1, operadora="Claro"),                  # 2: não existe
            linha(2, operadora="Antiga"),                 # 3: inativa
            linha(3),                                     # 4: vazia -> TIM
            linha(4, operadora="tim"),                    # 5: sem distinguir caixa
            linha(5, operadora="VIVO"),                   # 6: sem integração é permitido
        ])
        self.assertEqual(self.situacoes(imp_), {2: "rejeitada", 3: "rejeitada", 4: "valida", 5: "valida", 6: "valida"})
        self.assertIn('Operadora "Claro" não está cadastrada', self.motivos(imp_, 2))
        self.assertIn('A operadora "Antiga" está inativa', self.motivos(imp_, 3))
        self.assertEqual([self.linha_de(imp_, n).dados["operadora"] for n in (4, 5, 6)], ["TIM", "TIM", "Vivo"])
        imp.confirmar(imp_, self.usuario)
        self.assertEqual(M2MChip.objects.get(iccid=base.iccid_valido(5)).operadora, vivo)
        self.assertFalse(M2MChip.objects.get(iccid=base.iccid_valido(5)).consultavel_pela_api)

    def test_numero_da_linha_repetido_e_so_aviso(self):
        self.chip(9, nome="Já cadastrado", numero_linha="77988887777")
        imp_ = self.analisar_xlsx([
            linha(1, numero="(77) 99999-1234"),
            linha(2, numero="77999991234"),                           # repetido no arquivo
            linha(3, numero="77988887777"),                           # já em uso no cadastro
            linha(4),
        ])
        self.assertEqual(set(self.situacoes(imp_).values()), {"valida"})
        self.assertEqual(self.linha_de(imp_, 2).avisos, [])
        self.assertIn("se repete: já aparece na linha 2", " ".join(self.linha_de(imp_, 3).avisos))
        self.assertIn('já está em uso pelo chip "Já cadastrado"', " ".join(self.linha_de(imp_, 4).avisos))
        self.assertEqual(imp_.avisos, 2)                                  # linhas com aviso
        imp.confirmar(imp_, self.usuario)
        self.assertEqual(M2MChip.objects.filter(numero_linha="77999991234").count(), 2)          # número não é único

    def test_numero_do_excel_que_perdeu_digitos_rejeita_e_o_que_cabe_no_double_passa(self):
        """O Excel guarda número como double: ICCID de 19 dígitos NÃO cabe (perde dígitos) e vem como
        float; IMSI de 15 dígitos cabe e é lido como inteiro. A planilha de verdade (e o modelo) usa texto."""
        imp_ = self.analisar_xlsx([
            linha(iccid=float(base.iccid_valido(1))),                 # 2: a coluna estava como Número: perdeu dígitos
            linha(2, imsi=int(base.imsi_valido(2))),                  # 3: IMSI numérico de 15 dígitos cabe
            linha(3, imsi=float(base.imsi_valido(3))),                # 4: idem, vindo como ponto flutuante
        ])
        self.assertEqual(self.situacoes(imp_), {2: "rejeitada", 3: "valida", 4: "valida"})
        self.assertIn("perdeu dígitos", self.motivos(imp_, 2))
        self.assertEqual(self.linha_de(imp_, 3).dados["imsi"], base.imsi_valido(2))
        self.assertEqual(self.linha_de(imp_, 4).dados["imsi"], base.imsi_valido(3))

    def test_campos_longos_demais_rejeitam(self):
        imp_ = self.analisar_xlsx([linha(1, nome="n" * 121), linha(2, descricao="d" * 256), linha(3, obs="o" * 2001)])
        self.assertEqual(set(self.situacoes(imp_).values()), {"rejeitada"})
        self.assertIn("até 120 caracteres", self.motivos(imp_, 2))


# ═════════════════════════════════════════════════════════════
# Injeção de fórmula
# ═════════════════════════════════════════════════════════════

class InjecaoDeFormulaTests(ImportBase):
    PERIGOSOS = ("=HYPERLINK(\"http://x\";\"clique\")", "+cmd|' /C calc'!A0", "-2+3", "@SUM(A1:A9)")

    def test_valor_que_comeca_com_sinal_de_formula_e_texto_e_avisa(self):
        linhas = [linha(n + 1, nome=perigoso, descricao=perigoso) for n, perigoso in enumerate(self.PERIGOSOS)]
        como_texto = [(r, c) for r in range(2, 6) for c in (5, 6)]
        for nome_arquivo, dados in (
            ("f.xlsx", xlsx_bytes(linhas, como_texto=como_texto)),
            ("f.csv", csv_bytes([[str(c) for c in l] for l in linhas])),
        ):
            with self.subTest(arquivo=nome_arquivo):
                imp_ = imp.analisar(upload(nome_arquivo, dados), self.usuario)
                self.assertEqual(set(self.situacoes(imp_).values()), {"valida"})
                for n, perigoso in enumerate(self.PERIGOSOS, start=2):
                    linha_ = self.linha_de(imp_, n)
                    self.assertEqual(linha_.dados["identificacao"], perigoso)                    # tratado como texto, sem tocar
                    self.assertIn("foi tratado como texto", " ".join(linha_.avisos))

    def test_o_relatorio_em_excel_escapa_toda_celula_de_texto_perigosa(self):
        perigoso = self.PERIGOSOS[0]
        imp_ = imp.analisar(upload("f.csv", csv_bytes([[str(c) for c in linha(1, nome=perigoso, descricao="=1+1", obs="@x")]])), self.usuario)
        imp.confirmar(imp_, self.usuario)
        wb = load_workbook(io.BytesIO(imp.exportar_relatorio_xlsx(imp_)))
        formulas = 0
        for ws in wb.worksheets:
            for linha_ in ws.iter_rows():
                for celula in linha_:
                    if celula.data_type == "f":
                        formulas += 1
                    if isinstance(celula.value, str) and celula.value[:1] in ("=", "+", "-", "@"):
                        self.fail(f"célula {ws.title}!{celula.coordinate} começa com {celula.value[:1]!r} sem escape")
        self.assertEqual(formulas, 0)
        linhas = wb["Linhas"]
        self.assertEqual(linhas["F2"].value, "'" + perigoso)                                     # a identificação, com o apóstrofo
        self.assertEqual(linhas["F2"].data_type, "s")
        # o dado no banco segue o original, sem apóstrofo
        self.assertEqual(M2MChip.objects.get().nome, perigoso)

    def test_nomes_de_arquivo_perigosos_tambem_sao_escapados_no_resumo(self):
        imp_ = imp.analisar(upload("=cmd.csv", csv_bytes([linha(1)])), self.usuario)
        wb = load_workbook(io.BytesIO(imp.exportar_relatorio_xlsx(imp_)))
        resumo = {wb["Resumo"].cell(row=r, column=1).value: wb["Resumo"].cell(row=r, column=2) for r in range(1, 12)}
        self.assertEqual(resumo["Arquivo"].value, "'=cmd.csv")
        self.assertEqual(resumo["Arquivo"].data_type, "s")


# ═════════════════════════════════════════════════════════════
# Confirmação
# ═════════════════════════════════════════════════════════════

class ConfirmacaoTests(ImportBase):
    def test_confirmar_grava_os_chips_com_entrada_operadora_linha_e_unidade(self):
        imp_ = self.analisar_xlsx([
            linha(1, nome="Pivô 1", numero="(77) 99999-1234", descricao="Pesagem", unidade="Fazenda Norte", obs="Instalar em jan"),
            linha(2, imsi=base.imsi_valido(2)),
        ])
        r = imp.confirmar(imp_, self.outro)
        self.assertEqual((r.estado, r.confirmada_por), ("confirmada", self.outro))
        self.assertIsNotNone(r.confirmada_em)
        self.assertEqual((r.linhas, r.importados, r.existentes, r.rejeitados, r.validas), (2, 2, 0, 0, 0))
        chip = M2MChip.objects.get(nome="Pivô 1")
        self.assertEqual(
            (chip.iccid, chip.numero_linha, chip.descricao, chip.observacoes, chip.localidade, chip.operadora.nome, chip.situacao_uso),
            (base.iccid_valido(1), "77999991234", "Pesagem", "Instalar em jan", self.fazenda, "TIM", "disponivel"),
        )
        self.assertTrue(chip.monitorado)
        self.assertEqual(chip.criado_por, self.outro)
        entrada = FrotaMovimentacao.objects.get(chip=chip)
        self.assertEqual((entrada.tipo, entrada.importacao, entrada.usuario), ("entrada", r, self.outro))
        linhas = {l.numero: l for l in r.itens.all()}
        self.assertEqual((linhas[2].situacao, linhas[2].chip), ("importada", chip))
        self.assertEqual(FrotaMovimentacao.objects.filter(importacao=r).count(), 2)

    def test_ninguem_confirma_duas_vezes(self):
        imp_ = self.analisar_xlsx([linha(1)])
        imp.confirmar(imp_, self.usuario)
        with self.assertRaises(ValidationError) as cm:
            imp.confirmar(imp_, self.usuario)                                    # a instância velha ainda diz "previa": o banco manda
        self.assertIn("já foi confirmada", cm.exception.messages[0])
        self.assertEqual(M2MChip.objects.count(), 1)

    def test_a_reserva_da_importacao_e_condicional(self):
        """Duas pessoas clicam juntas: a que perde a corrida não importa nada."""
        imp_ = self.analisar_xlsx([linha(1)])
        FrotaImportacaoChips.objects.filter(pk=imp_.pk).update(estado="confirmada")        # a outra pessoa chegou primeiro
        with self.assertRaises(ValidationError):
            imp.confirmar(imp_, self.usuario)
        self.assertEqual(M2MChip.objects.count(), 0)

    def test_previa_expirada_e_recusada(self):
        imp_ = self.analisar_xlsx([linha(1)])
        FrotaImportacaoChips.objects.filter(pk=imp_.pk).update(criada_em=timezone.now() - timedelta(hours=25))
        with self.assertRaises(ValidationError) as cm:
            imp.confirmar(imp_, self.usuario)
        self.assertIn("expirou", cm.exception.messages[0])
        self.assertEqual(M2MChip.objects.count(), 0)
        imp_.refresh_from_db()
        self.assertEqual(imp_.estado, "previa")

    def test_cancelada_nao_confirma_e_so_previa_cancela(self):
        imp_ = self.analisar_xlsx([linha(1)])
        imp.cancelar(imp_, self.usuario)
        imp_.refresh_from_db()
        self.assertEqual(imp_.estado, "cancelada")
        with self.assertRaises(ValidationError):
            imp.confirmar(imp_, self.usuario)
        with self.assertRaises(ValidationError):
            imp.cancelar(imp_, self.usuario)
        self.assertEqual(M2MChip.objects.count(), 0)

    def test_confirmar_exige_usuario(self):
        imp_ = self.analisar_xlsx([linha(1)])
        with self.assertRaises(ValidationError):
            imp.confirmar(imp_, None)
        imp_.refresh_from_db()
        self.assertEqual(imp_.estado, "previa")

    def test_revalida_cada_linha_outra_pessoa_cadastrou_o_chip_no_meio_tempo(self):
        imp_ = self.analisar_xlsx([linha(1, nome="A"), linha(2, nome="B"), linha(3, nome="C")])
        self.assertEqual(imp_.validas, 3)
        chip_da_outra_pessoa = self.chip(2, nome="Cadastrado pela outra pessoa")           # mesmo ICCID da linha 3
        self.chip(99, nome="C")                                                            # e o nome "C" da linha 4 também foi tomado
        r = imp.confirmar(imp_, self.usuario)
        self.assertEqual(self.situacoes(r), {2: "importada", 3: "existente", 4: "rejeitada"})
        self.assertEqual(self.linha_de(r, 3).chip, chip_da_outra_pessoa)
        self.assertIn("Revalidação ao confirmar", self.linha_de(r, 3).motivos[0])
        self.assertIn("Já existe um chip com a identificação", self.linha_de(r, 4).motivos[0])
        self.assertEqual((r.importados, r.existentes, r.rejeitados, r.validas, r.linhas), (1, 1, 1, 0, 3))
        self.assertEqual(M2MChip.objects.get(pk=chip_da_outra_pessoa.pk).nome, "Cadastrado pela outra pessoa")    # intocado
        self.assertTrue(M2MChip.objects.filter(nome="A").exists())

    def test_revalida_operadora_inativada_e_unidade_apagada(self):
        vivo = ConectividadeOperadora.objects.create(nome="Vivo", integracao="", ativo=True)
        sul = Localidade.objects.create(local="Fazenda Sul")
        imp_ = self.analisar_xlsx([linha(1, operadora="Vivo"), linha(2, unidade="Fazenda Sul"), linha(3)])
        ConectividadeOperadora.objects.filter(pk=vivo.pk).update(ativo=False)
        sul.delete()
        r = imp.confirmar(imp_, self.usuario)
        self.assertEqual(self.situacoes(r), {2: "rejeitada", 3: "rejeitada", 4: "importada"})
        self.assertIn("inativada", self.linha_de(r, 2).motivos[0])
        self.assertIn("unidade deixou de existir", self.linha_de(r, 3).motivos[0])

    def test_cada_linha_grava_num_atomic_proprio_uma_linha_com_erro_nao_derruba_as_outras(self):
        imp_ = self.analisar_xlsx([linha(1, nome="Chip A"), linha(2, nome="Chip B"), linha(3, nome="Chip C")])
        original = vinculos.registrar_entrada

        def falha_no_b(chip, usuario, *args, **kwargs):
            if chip.nome == "Chip B":
                raise ValidationError("A movimentação de entrada não pôde ser gravada.")
            return original(chip, usuario, *args, **kwargs)

        with mock.patch.object(vinculos, "registrar_entrada", side_effect=falha_no_b):
            r = imp.confirmar(imp_, self.usuario)
        self.assertEqual(self.situacoes(r), {2: "importada", 3: "rejeitada", 4: "importada"})
        self.assertIn("Revalidação ao confirmar", self.linha_de(r, 3).motivos[0])
        self.assertEqual(sorted(M2MChip.objects.values_list("nome", flat=True)), ["Chip A", "Chip C"])      # o B foi desfeito por inteiro
        self.assertEqual(M2MChip.objects.filter(nome="Chip B").count(), 0)
        self.assertEqual((r.importados, r.rejeitados), (2, 1))
        self.assertEqual(FrotaMovimentacao.objects.count(), 2)

    def test_linha_rejeitada_e_existente_nao_sao_tocadas_na_confirmacao(self):
        self.chip(1, nome="Existente")
        imp_ = self.analisar_xlsx([linha(1), linha(None, nome="rejeitada"), linha(2, nome="Boa")])
        r = imp.confirmar(imp_, self.usuario)
        self.assertEqual(self.situacoes(r), {2: "existente", 3: "rejeitada", 4: "importada"})
        self.assertEqual((r.linhas, r.existentes, r.rejeitados, r.importados), (3, 1, 1, 1))

    def test_confirmar_nao_registra_veiculo_nem_vinculo(self):
        imp.confirmar(self.analisar_xlsx([linha(1), linha(2)]), self.usuario)
        self.assertEqual(M2MChip.objects.filter(situacao_uso="disponivel").count(), 2)
        self.assertEqual(FrotaMovimentacao.objects.exclude(tipo="entrada").count(), 0)


# ═════════════════════════════════════════════════════════════
# Relatório
# ═════════════════════════════════════════════════════════════

class RelatorioTests(ImportBase):
    def importar(self):
        self.chip(1, nome="Já existia")
        imp_ = self.analisar_xlsx([
            linha(1),                                                    # 2: existente
            linha(2, nome="Boa", numero="77999991234"),                  # 3: importada
            linha(3, nome="Outra", numero="77999991234"),                # 4: importada, com aviso
            linha(None, nome="Sem número"),                              # 5: rejeitada
            linha(4, unidade="Inventada"),                               # 6: rejeitada
        ])
        return imp.confirmar(imp_, self.outro)

    def test_totais_do_relatorio_saem_das_linhas_e_fecham(self):
        r = self.importar()
        rel = imp.relatorio(r)
        self.assertEqual(
            rel["totais"], dict(linhas=5, validas=0, importados=2, existentes=1, rejeitados=2, avisos=1),
        )
        t = rel["totais"]
        self.assertEqual(t["importados"] + t["existentes"] + t["rejeitados"] + t["validas"], t["linhas"])
        self.assertEqual((rel["arquivo"], rel["estado"], rel["criada_por"], rel["confirmada_por"]), ("chips.xlsx", "confirmada", "ti_m2m", "outro_usuario"))
        self.assertEqual([l["numero"] for l in rel["linhas"]], [2, 3, 4, 5, 6])
        self.assertEqual([l["situacao"] for l in rel["linhas"]], ["existente", "importada", "importada", "rejeitada", "rejeitada"])
        self.assertEqual(rel["linhas"][1]["chip_nome"], "Boa")
        self.assertIn("Informe o ICCID ou o IMSI", rel["linhas"][3]["motivos"][0])
        self.assertTrue(rel["linhas"][2]["avisos"])
        # o relatório dos totais gravados na importação bate
        self.assertEqual((r.linhas, r.importados, r.existentes, r.rejeitados, r.avisos), (5, 2, 1, 2, 1))

    def test_totais_gravados_que_ficaram_para_tras_nao_enganam_o_relatorio(self):
        r = self.importar()
        FrotaImportacaoChips.objects.filter(pk=r.pk).update(importados=999)
        self.assertEqual(imp.relatorio(r)["totais"]["importados"], 2)

    def test_excel_do_relatorio_tem_resumo_e_linha_a_linha_com_motivo_data_e_usuario(self):
        r = self.importar()
        wb = load_workbook(io.BytesIO(imp.exportar_relatorio_xlsx(r)))
        self.assertEqual(wb.sheetnames, ["Resumo", "Linhas"])
        resumo = {wb["Resumo"].cell(row=i, column=1).value: wb["Resumo"].cell(row=i, column=2).value for i in range(1, 17)}
        self.assertEqual(resumo["Arquivo"], "chips.xlsx")
        self.assertEqual(resumo["SHA-256"], r.sha256)
        self.assertEqual((resumo["Prévia criada por"], resumo["Confirmada por"]), ("ti_m2m", "outro_usuario"))
        self.assertEqual(resumo["Confirmada em"], timezone.localtime(r.confirmada_em).strftime("%d/%m/%Y %H:%M"))
        self.assertEqual(
            (resumo["Linhas"], resumo["Importadas"], resumo["Já cadastradas"], resumo["Rejeitadas"], resumo["Linhas com aviso"]),
            (5, 2, 1, 2, 1),
        )
        linhas = wb["Linhas"]
        self.assertEqual([c.value for c in linhas[1]][:3], ["Linha", "Situação", "ICCID"])
        self.assertEqual(linhas.max_row, 6)
        self.assertEqual((linhas["A5"].value, linhas["B5"].value), (5, "Rejeitada"))
        self.assertIn("Informe o ICCID ou o IMSI", linhas["I5"].value)
        self.assertIn('Unidade "Inventada"', linhas["I6"].value)
        self.assertIn("se repete", linhas["J4"].value)

    def test_listar_linhas_filtrar_e_arquivo_ja_importado(self):
        r = self.importar()
        self.assertEqual([l.numero for l in imp.linhas_da_importacao(r)], [2, 3, 4, 5, 6])
        self.assertEqual([l.numero for l in imp.linhas_da_importacao(r, "rejeitada")], [5, 6])
        self.assertEqual(len(list(imp.linhas_da_importacao(r, "qualquer"))), 5)                # filtro inválido é ignorado
        self.assertEqual(list(imp.listar_importacoes()), [r])
        self.assertIsNone(imp.arquivo_ja_importado(r))
        outra = FrotaImportacaoChips.objects.create(arquivo_nome="de novo.xlsx", sha256=r.sha256, formato="xlsx", estado="previa")
        self.assertEqual(imp.arquivo_ja_importado(outra), r)                                   # o mesmo arquivo já foi confirmado
        self.assertIsNone(imp.arquivo_ja_importado(r))
