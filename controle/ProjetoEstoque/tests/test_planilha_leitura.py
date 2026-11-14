"""
`services/planilha_leitura.py` — a leitura de planilhas compartilhada pelas importações.

O ponto principal é o .xlsx em formato **Strict** (o "Salvar como → Pasta de Trabalho Strict Open
XML" do Excel): o `openpyxl` o abre com ZERO abas e sem erro, e a importação responderia "planilha
vazia" para um arquivo cheio. É o formato da planilha de equipamentos motorizados do ERP.
"""
import datetime
import io
import zipfile

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase
from openpyxl import Workbook

from services import planilha_leitura as leitura

NS_STRICT = "http://purl.oclc.org/ooxml/spreadsheetml/main"
REL_STRICT = "http://purl.oclc.org/ooxml/officeDocument/relationships"


def xlsx_estrito(linhas_xml: str, nome_da_aba: str = "dados") -> bytes:
    """Um .xlsx Strict mínimo, montado peça por peça: namespaces `purl.oclc.org`, texto em linha
    (`inlineStr`) e data como `t="d"` com texto ISO — exatamente como o Excel grava no Strict."""
    pacote = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{REL_STRICT}/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>"
        ),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<workbook xmlns="{NS_STRICT}" xmlns:r="{REL_STRICT}" conformance="strict">'
            f'<sheets><sheet name="{nome_da_aba}" sheetId="1" r:id="rId1"/></sheets></workbook>'
        ),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{REL_STRICT}/worksheet" Target="worksheets/sheet1.xml"/>'
            "</Relationships>"
        ),
        "xl/worksheets/sheet1.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<worksheet xmlns="{NS_STRICT}"><sheetData>{linhas_xml}</sheetData></worksheet>'
        ),
    }
    saida = io.BytesIO()
    with zipfile.ZipFile(saida, "w", zipfile.ZIP_DEFLATED) as z:
        for nome, conteudo in pacote.items():
            z.writestr(nome, conteudo)
    return saida.getvalue()


def texto(ref: str, valor: str) -> str:
    return f'<c r="{ref}" t="inlineStr"><is><t>{valor}</t></is></c>'


class XlsxStrictTests(SimpleTestCase):
    def test_o_arquivo_strict_e_lido_com_texto_numero_e_data(self):
        dados = xlsx_estrito(
            '<row r="1">' + texto("A1", "Equipamento") + texto("B1", "Data Compra") + "</row>"
            '<row r="2"><c r="A2"><v>8110001</v></c><c r="B2" t="d"><v>2018-08-15</v></c></row>'
            '<row r="3"><c r="A3"><v>8410008</v></c><c r="B3" t="d"><v>1905-07-03</v></c></row>'
        )
        linhas = leitura.linhas_do_xlsx(dados)
        self.assertEqual(linhas[0], (1, ["Equipamento", "Data Compra"]))
        self.assertEqual(linhas[1], (2, [8110001, datetime.date(2018, 8, 15)]))
        # a data suspeita chega como está: corrigir é decisão de quem confere, nunca da leitura
        self.assertEqual(linhas[2], (3, [8410008, datetime.date(1905, 7, 3)]))

    def test_sem_o_tratamento_o_openpyxl_veria_zero_abas(self):
        """A prova de que o tratamento é necessário (e não zelo): o `openpyxl` cru lê o Strict
        como uma pasta de trabalho SEM ABAS."""
        from openpyxl import load_workbook

        dados = xlsx_estrito('<row r="1">' + texto("A1", "x") + "</row>")
        with self.assertWarns(UserWarning):
            wb = load_workbook(io.BytesIO(dados), read_only=True)
        self.assertEqual(wb.sheetnames, [])

    def test_a_aba_preferida_vale_so_se_existir(self):
        dados = xlsx_estrito('<row r="1">' + texto("A1", "ok") + "</row>", nome_da_aba="Equipamentos")
        self.assertEqual(leitura.linhas_do_xlsx(dados, aba_preferida="Nao existe"), [(1, ["ok"])])
        self.assertEqual(leitura.linhas_do_xlsx(dados, aba_preferida="Equipamentos"), [(1, ["ok"])])

    def test_o_strict_e_o_comum_dao_as_mesmas_linhas(self):
        wb = Workbook()
        ws = wb.active
        ws.append(["Equipamento", "Modelo", "Data"])
        ws.append([8110001, "TRATOR NH 3880", datetime.date(2018, 8, 15)])
        saida = io.BytesIO()
        wb.save(saida)
        comum = saida.getvalue()
        # o mesmo arquivo com os namespaces do Strict
        convertido = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(comum)) as origem, zipfile.ZipFile(convertido, "w") as destino:
            for item in origem.infolist():
                corpo = origem.read(item.filename)
                if item.filename.endswith((".xml", ".rels")):
                    corpo = corpo.replace(b"http://schemas.openxmlformats.org/spreadsheetml/2006/main", NS_STRICT.encode())
                    corpo = corpo.replace(b"http://schemas.openxmlformats.org/officeDocument/2006/relationships", REL_STRICT.encode())
                destino.writestr(item.filename, corpo)
        esperado = leitura.linhas_do_xlsx(comum)
        self.assertEqual(leitura.linhas_do_xlsx(convertido.getvalue()), esperado)
        self.assertEqual(esperado[1][1][0], 8110001)

    def test_arquivo_comum_nao_e_reescrito(self):
        wb = Workbook()
        wb.active.append(["a"])
        saida = io.BytesIO()
        wb.save(saida)
        dados = saida.getvalue()
        self.assertIs(leitura._para_formato_comum(dados), dados)


class LimitesETextoTests(SimpleTestCase):
    def test_recusa_o_que_nao_e_xlsx(self):
        for dados in (b"isto nao e uma planilha", b"PK\x03\x04quebrado"):
            with self.subTest(dados=dados[:6]), self.assertRaises(ValidationError) as cm:
                leitura.linhas_do_xlsx(dados)
            self.assertIn("planilha .xlsx válida", cm.exception.messages[0])

    def test_recusa_o_xlsx_inflado(self):
        dados = xlsx_estrito('<row r="1">' + texto("A1", "x") + "</row>")
        with self.assertRaises(ValidationError) as cm:
            leitura.linhas_do_xlsx(dados, max_descompactado=100)
        self.assertIn("grande demais", cm.exception.messages[0])

    def test_o_limite_de_tamanho_e_o_vazio(self):
        with self.assertRaises(ValidationError) as cm:
            leitura.ler_bytes(io.BytesIO(b"x" * 11), max_bytes=10)
        self.assertIn("limite", cm.exception.messages[0])
        with self.assertRaises(ValidationError) as cm:
            leitura.ler_bytes(io.BytesIO(b""))
        self.assertIn("vazio", cm.exception.messages[0])

    def test_csv_do_excel_brasileiro_em_cp1252_com_ponto_e_virgula(self):
        dados = "Equipamento;Situação;Setor\r\n8110001;Em Uso;GRÃOS\r\n".encode("cp1252")
        self.assertEqual(
            leitura.linhas_do_csv(dados), [(1, ["Equipamento", "Situação", "Setor"]), (2, ["8110001", "Em Uso", "GRÃOS"])],
        )

    def test_xlsx_com_extensao_csv_e_recusado(self):
        with self.assertRaises(ValidationError) as cm:
            leitura.linhas_do_csv(b"PK\x03\x04")
        self.assertIn("extensão .csv", cm.exception.messages[0])

    def test_celula_como_texto(self):
        self.assertEqual(leitura.celula_como_texto(8110001), "8110001")
        self.assertEqual(leitura.celula_como_texto(8110001.0), "8110001")
        self.assertEqual(leitura.celula_como_texto("  TRATOR   NH 3880 "), "TRATOR NH 3880")
        self.assertEqual(leitura.celula_como_texto(None), "")
        self.assertIsNone(leitura.celula_como_texto(1.5))              # já perdeu dígitos: a linha é recusada
        self.assertIsNone(leitura.celula_como_texto(1e16))

    def test_texto_seguro_escapa_formula(self):
        for perigoso in ("=1+1", "+1", "-1", "@SOMA(A1)", "\tx"):
            self.assertTrue(leitura.texto_seguro(perigoso).startswith("'"), perigoso)
        self.assertEqual(leitura.texto_seguro("normal"), "normal")

    def test_normalizar_tira_acento_caixa_e_espacos(self):
        self.assertEqual(leitura.normalizar("  Situação  Cadastral "), "situacao cadastral")
