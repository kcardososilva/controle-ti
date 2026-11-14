"""
Importação da planilha de EQUIPAMENTOS MOTORIZADOS (ERP) — `services/frota_equipamento_import_service.py`.

A planilha de teste é uma MINIATURA da real: as mesmas colunas e os mesmos casos que o prompt mandou
revalidar (chassi repetido em dois equipamentos, compra em 1905, ano-modelo à frente, "Não Informado" no
setor, placa interna). No fim, o teste de ACEITE roda contra a planilha de verdade, quando ela está em `docs/`.

O que se trava aqui:
  • nada é gravado antes da confirmação — nem cadastro de apoio;
  • a mesma planilha, importada duas vezes, não cria equipamento nenhum na segunda;
  • nada suspeito é corrigido ou recusado: vira conferência, e o dado entra como veio;
  • a atualização só acontece autorizada e só nos campos que o ERP conhece.
"""
import io
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock, skipUnless

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from openpyxl import Workbook, load_workbook

from ProjetoEstoque.models import (
    FrotaCentroCusto,
    FrotaClasseMecanica,
    FrotaClasseOperacional,
    FrotaEmpresa,
    FrotaEvento,
    FrotaImportacaoEquipamentoLinha,
    FrotaImportacaoEquipamentos,
    FrotaTipo,
    FrotaVeiculo,
    Localidade,
    M2MEvento,
)
from ProjetoEstoque.tests import test_frota as base
from ProjetoEstoque.tests.test_planilha_leitura import NS_STRICT, REL_STRICT
from services import frota_classificacao_service as classes
from services import frota_equipamento_import_service as imp
from services import frota_service as frota

Situacao = FrotaImportacaoEquipamentoLinha.Situacao
Status = FrotaVeiculo.Status

CABECALHO = [
    "Cód.Empresa", "Empresa", "Equipamento", "Modelo", "Placa", "Cód. Operacional", "Classe Operacional", "Cód.", "Setor",
    "Cód.Custo", "Cent. Custo", "Chassi", "Ano Modelo", "Ano Fabr.", "Data Compra", "Situação", "Cód.2", "Classe Mecânica",
    "Data de Início",
]


def linha(equipamento, modelo, *, empresa="SANTA COLOMBA", placa=None, classe="TRATOR LEVE", setor="GRÃOS", centro="TRATOR LEVE - GRAOS",
          chassi=None, modelo_ano=2011, fabricacao=2011, compra=date(2018, 8, 15), situacao="Em Uso", mecanica="TRATOR LEVE",
          inicio=date(2025, 5, 19)):
    """Uma linha na ordem do cabeçalho do ERP, com os códigos que o ERP associa a cada nome."""
    codigos = {"empresa": {"SANTA COLOMBA": 1, "TERCEIROS": 2}, "classe": {"TRATOR LEVE": 34, "CAMINHÃO ROLL OFF": 13, "COLHEITADEIRA DE GRÃOS": 3,
               "CAMINHÃO APOIO": 24, "MOTOCICLETA": 27, "GERADOR": 35, "PA CARREGADEIRA": 25}, "setor": {"GRÃOS": 2, "Não Informado": 0, "TABACO": 1},
               "centro": {"TRATOR LEVE - GRAOS": 32107, "CAMINHAO - GRAOS": 32119, "CAMINHÕES GRÃOS": 32119, "COLHEITADEIRA - GRAOS": 32113,
                          "VEICULOS LEVES MOTOS - GRAOS": 32123, "OUTRAS MAQUINAS - GRAOS": 32122, "PA CARREGADEIRA - GRAOS": 32117},
               "mecanica": {"TRATOR LEVE": 18, "COLHEITADEIRA": 3, "CAMINHÃO": 1, "MOTOCICLETA": 11, "GERADOR": 16, "PA CARREGADEIRA": 14}}
    return [
        codigos["empresa"][empresa], empresa, equipamento, modelo, placa, codigos["classe"][classe], classe, codigos["setor"].get(setor, 9), setor,
        codigos["centro"][centro], centro, chassi, modelo_ano, fabricacao, compra, situacao, codigos["mecanica"][mecanica], mecanica, inicio,
    ]


def planilha_do_erp():
    """As linhas da miniatura, uma por caso a revalidar."""
    return [
        linha(8110001, "TRATOR NH 3880", chassi="1CQS660ALH0120143"),
        linha(8210001, "COLHEI. GRAOS JDS660", classe="COLHEITADEIRA DE GRÃOS", centro="COLHEITADEIRA - GRAOS", chassi="1CQS660ALH0120143",
              modelo_ano=2019, fabricacao=2019, compra=date(2019, 3, 21), mecanica="COLHEITADEIRA"),
        linha(8410008, "VW 24.250", placa="NZH4261", classe="CAMINHÃO ROLL OFF", centro="CAMINHAO - GRAOS", chassi="9534N8246BR157267",
              compra=date(1905, 7, 3), mecanica="CAMINHÃO"),
        linha(9100592, "VW 17.210", empresa="TERCEIROS", placa="VAY0B31", classe="CAMINHÃO APOIO", setor="Não Informado", centro="CAMINHÕES GRÃOS",
              chassi="953678TG8VR016233", modelo_ano=2027, fabricacao=2026, compra=date(2026, 10, 30), mecanica="CAMINHÃO", inicio=date(2026, 10, 30)),
        linha(8110009, "TRATOR MF 275", placa="C811009", chassi="2160035035", modelo_ano=2008, fabricacao=2008),
        linha(8720079, "HONDA NXR BROS 160", classe="MOTOCICLETA", centro="VEICULOS LEVES MOTOS - GRAOS", mecanica="MOTOCICLETA", modelo_ano=2024, fabricacao=2024),
        linha(8900001, "GERADOR STEMAC 40KVA", classe="GERADOR", centro="OUTRAS MAQUINAS - GRAOS", mecanica="GERADOR", modelo_ano=2016, fabricacao=2016),
        linha(8630001, "PA CARREG LIUGONG", classe="PA CARREGADEIRA", centro="PA CARREGADEIRA - GRAOS", mecanica="PA CARREGADEIRA", chassi="LIU1",
              compra=date(2026, 7, 21), inicio=date(2018, 8, 20)),
    ]


def xlsx_bytes(linhas, cabecalho=CABECALHO, titulo_acima=None):
    wb = Workbook()
    ws = wb.active
    ws.title = "equipamentos motorizados"
    if titulo_acima:
        ws.append([titulo_acima])
        ws.append([])
    ws.append(cabecalho)
    for l in linhas:
        ws.append(l)
    for celula in (c for fila in ws.iter_rows() for c in fila):
        if isinstance(celula.value, str) and celula.value[:1] in ("=", "+", "-", "@"):
            celula.data_type = "s"             # texto de verdade: sem isto o openpyxl grava uma FÓRMULA sem valor em cache
    # O openpyxl grava o horário de criação (segundo a segundo) dentro do arquivo: duas planilhas de MESMO conteúdo geradas
    # em segundos diferentes teriam SHA-256 diferente, e o teste de "reimportar o mesmo arquivo" falhava de vez em quando
    # (visto na suíte completa de 2026-11-14, sob carga). Hora fixa = o mesmo conteúdo é sempre o mesmo arquivo.
    wb.properties.created = wb.properties.modified = datetime(2026, 1, 1, 12, 0, 0)
    saida = io.BytesIO()
    wb.save(saida)
    return saida.getvalue()


def csv_bytes(linhas, cabecalho=CABECALHO):
    def celula(v):
        if isinstance(v, date):
            return v.strftime("%d/%m/%Y")
        return "" if v is None else str(v)

    texto = "\r\n".join(";".join(celula(c) for c in l) for l in [cabecalho, *linhas]) + "\r\n"
    return texto.encode("cp1252")


def upload(nome, dados):
    return SimpleUploadedFile(nome, dados)


class ImportacaoBase(base.FrotaBase):
    def enviar(self, linhas=None, nome="equipamentos motorizados.xlsx", **extra):
        return imp.enviar(upload(nome, xlsx_bytes(planilha_do_erp() if linhas is None else linhas, **extra)), self.usuario)

    def importar(self, linhas=None, **kw):
        previa = self.conferido(self.enviar(linhas))
        return imp.confirmar(previa, self.usuario, **kw)

    def conferido(self, previa, **valores):
        """Passa pelo passo de mapeamento mantendo as sugestões (ou trocando as que `valores` traga)."""
        painel = imp.painel_do_mapeamento(previa)
        decididos = {campo: {i["valor"]: i["decisao"] for i in itens} for campo, itens in painel["valores"].items()}
        for campo, mudancas in valores.items():
            decididos[campo].update(mudancas)
        return imp.aplicar_mapeamento(previa, previa.mapeamento["colunas"], decididos)

    def situacoes(self, importacao):
        return {l.codigo_legado: l.situacao for l in importacao.itens.all()}

    def codigos_de_conferencia(self, importacao):
        return {l.codigo_legado: {c["codigo"] for c in l.conferencias} for l in importacao.itens.all()}


# ═════════════════════════════════════════════════════════════
# Passo 1: envio
# ═════════════════════════════════════════════════════════════

class EnvioTests(ImportacaoBase):
    def test_a_previa_le_a_planilha_e_nao_grava_nada_na_frota(self):
        antes = (FrotaVeiculo.objects.count(), FrotaEmpresa.objects.count(), FrotaClasseOperacional.objects.count(),
                 FrotaClasseMecanica.objects.count(), FrotaCentroCusto.objects.count(), FrotaTipo.objects.count())
        previa = self.enviar()
        depois = (FrotaVeiculo.objects.count(), FrotaEmpresa.objects.count(), FrotaClasseOperacional.objects.count(),
                  FrotaClasseMecanica.objects.count(), FrotaCentroCusto.objects.count(), FrotaTipo.objects.count())
        self.assertEqual(antes, depois)
        self.assertEqual((previa.estado, previa.linhas, previa.formato), ("previa", 8, "xlsx"))
        self.assertEqual(previa.criada_por, self.usuario)
        self.assertEqual(len(previa.sha256), 64)

    def test_o_xlsx_strict_do_excel_e_lido(self):
        """O formato da planilha real: `openpyxl` cru lê ZERO abas. Reescrevê-lo é o que a leitura faz."""
        comum = xlsx_bytes(planilha_do_erp())
        convertido = io.BytesIO()
        import zipfile
        with zipfile.ZipFile(io.BytesIO(comum)) as origem, zipfile.ZipFile(convertido, "w") as destino:
            for item in origem.infolist():
                corpo = origem.read(item.filename)
                if item.filename.endswith((".xml", ".rels")):
                    corpo = corpo.replace(b"http://schemas.openxmlformats.org/spreadsheetml/2006/main", NS_STRICT.encode())
                    corpo = corpo.replace(b"http://schemas.openxmlformats.org/officeDocument/2006/relationships", REL_STRICT.encode())
                destino.writestr(item.filename, corpo)
        previa = imp.enviar(upload("estrito.xlsx", convertido.getvalue()), self.usuario)
        self.assertEqual(previa.linhas, 8)

    def test_csv_do_excel_brasileiro(self):
        previa = imp.enviar(upload("equipamentos.csv", csv_bytes(planilha_do_erp())), self.usuario)
        self.assertEqual((previa.formato, previa.linhas), ("csv", 8))
        l = previa.itens.get(codigo_legado="8410008")
        self.assertEqual(l.dados["data_compra"], "1905-07-03")                 # dd/mm/aaaa -> ISO, sem mexer no ano

    def test_titulo_acima_do_cabecalho_e_pulado_e_a_numeracao_e_a_da_planilha(self):
        previa = self.enviar(titulo_acima="Relatório de equipamentos — 13/11/2026")
        self.assertEqual(previa.linhas, 8)
        self.assertEqual(previa.itens.order_by("numero").first().numero, 4)              # título, linha em branco, cabeçalho
        self.assertEqual(previa.mapeamento["numero_do_cabecalho"], 3)

    def test_cabecalho_sem_a_coluna_equipamento_e_recusado_e_diz_as_esperadas(self):
        with self.assertRaises(ValidationError) as cm:
            imp.enviar(upload("x.xlsx", xlsx_bytes([["a", "b"]], cabecalho=["Nome", "Valor"])), self.usuario)
        self.assertIn("Equipamento", cm.exception.messages[0])
        self.assertIn("Colunas esperadas: Cód.Empresa, Empresa, Equipamento", cm.exception.messages[0])
        self.assertEqual(FrotaImportacaoEquipamentos.objects.count(), 0)

    def test_arquivos_que_nao_servem(self):
        casos = {
            "extensão errada": (upload("e.xls", b"x"), "Envie uma planilha"),
            "vazio": (upload("e.csv", b""), "vazio"),
            "xlsx quebrado": (upload("e.xlsx", b"PK\x03\x04quebrado"), "planilha .xlsx válida"),
            "só cabeçalho": (upload("e.xlsx", xlsx_bytes([])), "nenhuma linha de dados"),
        }
        for rotulo, (arquivo, trecho) in casos.items():
            with self.subTest(caso=rotulo), self.assertRaises(ValidationError) as cm:
                imp.enviar(arquivo, self.usuario)
            self.assertIn(trecho, cm.exception.messages[0])
        self.assertEqual(FrotaImportacaoEquipamentos.objects.count(), 0)

    def test_limites_de_linhas_e_de_arquivo(self):
        with mock.patch.object(imp, "MAX_LINHAS", 3), self.assertRaises(ValidationError) as cm:
            self.enviar()
        self.assertIn("mais de 3 linhas", cm.exception.messages[0])
        with mock.patch.object(imp, "MAX_BYTES", 10), self.assertRaises(ValidationError) as cm:
            self.enviar()
        self.assertIn("limite", cm.exception.messages[0])

    def test_xlsx_inflado_e_recusado(self):
        with mock.patch.object(imp, "MAX_DESCOMPACTADO", 100), self.assertRaises(ValidationError) as cm:
            self.enviar()
        self.assertIn("grande demais", cm.exception.messages[0])

    def test_o_modelo_baixavel_e_lido_de_volta_pelo_proprio_importador(self):
        wb = load_workbook(io.BytesIO(imp.modelo_xlsx()))
        self.assertEqual(wb.sheetnames, ["Equipamentos", "Instruções"])
        self.assertEqual([c.value for c in wb["Equipamentos"][1]], imp.ROTULOS)
        self.assertEqual(imp.modelo_csv().decode("utf-8-sig").strip().split(";"), imp.ROTULOS)
        with self.assertRaises(ValidationError) as cm:
            imp.enviar(upload("modelo.xlsx", imp.modelo_xlsx()), self.usuario)         # só o cabeçalho: nada a importar
        self.assertIn("nenhuma linha de dados", cm.exception.messages[0])

    def test_toda_coluna_tem_destino_ou_justificativa(self):
        for c in imp.COLUNAS:
            self.assertTrue(c.destino, c.campo)
        self.assertTrue(imp.COLUNA["setor_codigo"].justificativa)
        self.assertEqual([c.campo for c in imp.COLUNAS if c.obrigatoria], ["codigo_legado", "modelo"])


# ═════════════════════════════════════════════════════════════
# Passo 2: mapeamento
# ═════════════════════════════════════════════════════════════

class MapeamentoTests(ImportacaoBase):
    def test_as_colunas_do_erp_sao_reconhecidas_pelo_cabecalho(self):
        previa = self.enviar()
        painel = imp.painel_do_mapeamento(previa)
        por_campo = {c["campo"]: c for c in painel["colunas"]}
        self.assertEqual(len(painel["colunas"]), 19)
        self.assertEqual({c["situacao"] for c in painel["colunas"]}, {"mapeada"})
        self.assertEqual((por_campo["codigo_legado"]["origem"], por_campo["classe_mecanica_codigo"]["origem"]), ("Equipamento", "Cód.2"))
        self.assertEqual(painel["nao_usadas"], [])

    def test_o_mapeamento_nasce_nao_conferido_e_com_as_sugestoes_aplicadas(self):
        previa = self.enviar()
        self.assertFalse(previa.mapeamento["conferido"])
        painel = imp.painel_do_mapeamento(previa)
        situacao = {i["valor"]: i for i in painel["valores"]["situacao"]}
        self.assertEqual((situacao["Em Uso"]["decisao"], situacao["Em Uso"]["n"]), ("ativo", 8))
        empresa = {i["valor"]: i["decisao"] for i in painel["valores"]["empresa"]}
        self.assertEqual(empresa, {"SANTA COLOMBA": "proprio", "TERCEIROS": "terceiro"})
        classe = {i["valor"]: i["decisao"] for i in painel["valores"]["classe_operacional"]}
        self.assertEqual(classe["CAMINHÃO ROLL OFF"], "Caminhão")
        self.assertEqual(classe["MOTOCICLETA"], "Motocicleta")
        self.assertEqual(classe["COLHEITADEIRA DE GRÃOS"], "Colheitadeira")

    def test_confirmar_sem_ter_conferido_o_mapeamento_e_recusado(self):
        previa = self.enviar()
        with self.assertRaises(ValidationError) as cm:
            imp.confirmar(previa, self.usuario)
        self.assertIn("mapeamento", cm.exception.messages[0])
        self.assertEqual(FrotaVeiculo.objects.count(), 1 if FrotaVeiculo.objects.exists() else 0)

    def test_situacao_diferente_de_em_uso_pede_decisao_e_nao_vira_palpite(self):
        linhas = [linha(8110001, "A"), linha(8110002, "B", situacao="Vendido")]
        previa = self.enviar(linhas)
        self.assertEqual(self.situacoes(previa), {"8110001": Situacao.VALIDA, "8110002": Situacao.REJEITADA})
        rejeitada = previa.itens.get(codigo_legado="8110002")
        self.assertIn("Situação “Vendido” sem destino", rejeitada.motivos[0])
        previa = self.conferido(previa, situacao={"Vendido": "inativo"})
        self.assertEqual(self.situacoes(previa), {"8110001": Situacao.VALIDA, "8110002": Situacao.VALIDA})
        self.assertEqual(previa.itens.get(codigo_legado="8110002").dados["status"], "inativo")

    def test_o_status_do_mapeamento_e_o_que_o_equipamento_recebe(self):
        previa = self.conferido(self.enviar([linha(8110001, "A")]), situacao={"Em Uso": "manutencao"})
        imp.confirmar(previa, self.usuario)
        v = FrotaVeiculo.objects.get(codigo_legado="8110001")
        self.assertEqual((v.status, v.situacao_origem), (Status.MANUTENCAO, "Em Uso"))

    def test_empresa_desconhecida_nao_tem_natureza_ate_alguem_decidir(self):
        linhas = [linha(8110001, "A"), linha(8110002, "B", empresa="TERCEIROS")]
        linhas[1][1] = "FAZENDA DO VIZINHO"
        previa = self.enviar(linhas)
        self.assertEqual(self.situacoes(previa)["8110002"], Situacao.REJEITADA)
        self.assertIn("sem natureza", previa.itens.get(codigo_legado="8110002").motivos[0])
        previa = self.conferido(previa, empresa={"FAZENDA DO VIZINHO": "terceiro"})
        self.assertEqual(self.situacoes(previa)["8110002"], Situacao.VALIDA)
        self.assertEqual(previa.itens.get(codigo_legado="8110002").dados["natureza"], "terceiro")

    def test_a_categoria_da_classe_pode_ser_trocada_no_mapeamento(self):
        previa = self.conferido(self.enviar([linha(8110001, "A")]), classe_operacional={"TRATOR LEVE": "Linha amarela"})
        self.assertEqual(previa.itens.get().dados["categoria"], "Linha amarela")
        imp.confirmar(previa, self.usuario)
        self.assertEqual(FrotaVeiculo.objects.get(codigo_legado="8110001").tipo.nome, "Linha amarela")
        self.assertEqual(FrotaClasseOperacional.objects.get(nome="TRATOR LEVE").categoria.nome, "Linha amarela")

    def test_remapear_nao_pede_o_arquivo_de_novo_e_reanalisa(self):
        previa = self.enviar()
        antes = previa.itens.count()
        self.conferido(previa, situacao={"Em Uso": "inativo"})
        previa.refresh_from_db()
        self.assertEqual(previa.itens.count(), antes)
        self.assertEqual({l.dados["status"] for l in previa.itens.all()}, {"inativo"})
        self.assertTrue(previa.mapeamento["conferido"])

    def test_coluna_obrigatoria_sem_origem_e_recusada(self):
        previa = self.enviar()
        colunas = {k: v for k, v in previa.mapeamento["colunas"].items() if k != "modelo"}
        with self.assertRaises(ValidationError) as cm:
            imp.aplicar_mapeamento(previa, colunas, {})
        self.assertIn("Modelo", cm.exception.messages[0])

    def test_coluna_que_nao_existe_no_arquivo_e_campo_desconhecido_sao_recusados(self):
        previa = self.enviar()
        with self.assertRaises(ValidationError):
            imp.aplicar_mapeamento(previa, {**previa.mapeamento["colunas"], "placa": 99}, {})
        with self.assertRaises(ValidationError):
            imp.aplicar_mapeamento(previa, {**previa.mapeamento["colunas"], "voo": 1}, {})
        with self.assertRaises(ValidationError):
            imp.aplicar_mapeamento(previa, previa.mapeamento["colunas"], {"situacao": {"Em Uso": "voando"}})

    def test_arquivo_com_cabecalhos_proprios_mapeia_na_mao(self):
        cab = ["Código", "Nome da máquina", "Dono"]
        previa = imp.enviar(upload("x.xlsx", xlsx_bytes([[8110001, "TRATOR", "SANTA COLOMBA"]], cabecalho=["Equipamento", "Nome da máquina", "Dono"])), self.usuario)
        painel = imp.painel_do_mapeamento(previa)
        faltando = {c["campo"] for c in painel["colunas"] if c["situacao"] == "faltando"}
        self.assertEqual(faltando, {"modelo"})
        self.assertEqual(painel["nao_usadas"], ["Nome da máquina", "Dono"])
        previa = imp.aplicar_mapeamento(previa, {"codigo_legado": 0, "modelo": 1, "empresa": 2}, {"empresa": {"SANTA COLOMBA": "proprio"}})
        self.assertEqual(previa.itens.get().situacao, Situacao.VALIDA)
        self.assertEqual(cab[0], "Código")

    def test_o_mapeamento_so_vale_com_a_previa_aberta(self):
        previa = self.enviar()
        imp.cancelar(previa)
        with self.assertRaises(ValidationError) as cm:
            imp.aplicar_mapeamento(previa, previa.mapeamento["colunas"], {})
        self.assertIn("cancelada", cm.exception.messages[0])


# ═════════════════════════════════════════════════════════════
# Passos 3 e 4: pré-visualização e validação
# ═════════════════════════════════════════════════════════════

class PrevisualizacaoTests(ImportacaoBase):
    def setUp(self):
        super().setUp()
        self.previa = self.conferido(self.enviar())

    def test_os_totais_da_previa(self):
        r = imp.resumo(self.previa)
        self.assertEqual((r["novos"], r["rejeitados"], r["ja_cadastrados"], r["repetidos_na_planilha"]), (8, 0, 0, 0))
        self.assertEqual(r["por_natureza"], {"proprio": 7, "terceiro": 1})
        self.assertTrue(r["pronta"])

    def test_quatro_casos_que_o_prompt_mandou_revalidar_viram_conferencia_e_nenhum_e_corrigido(self):
        achados = self.codigos_de_conferencia(self.previa)
        self.assertIn("chassi_duplicado", achados["8110001"])
        self.assertIn("chassi_duplicado", achados["8210001"])
        self.assertIn("data_compra_fora_de_faixa", achados["8410008"])
        self.assertIn("ano_modelo_no_futuro", achados["9100592"])
        # ... e as linhas SEGUEM VÁLIDAS, com o dado como veio
        self.assertEqual({self.situacoes(self.previa)[c] for c in ("8110001", "8210001", "8410008", "9100592")}, {Situacao.VALIDA})
        dados = {l.codigo_legado: l.dados for l in self.previa.itens.all()}
        self.assertEqual(dados["8110001"]["chassi_serie"], dados["8210001"]["chassi_serie"])
        self.assertEqual((dados["8410008"]["data_compra"], dados["9100592"]["ano_modelo"]), ("1905-07-03", 2027))

    def test_o_chassi_repetido_cita_o_outro_equipamento(self):
        detalhe = next(c["detalhe"] for c in self.previa.itens.get(codigo_legado="8110001").conferencias if c["codigo"] == "chassi_duplicado")
        self.assertIn("8210001", detalhe)
        self.assertIn("COLHEI. GRAOS JDS660", detalhe)

    def test_sem_placa_nem_chassi_nao_e_erro_e_so_pendencia_onde_o_tipo_espera(self):
        achados = self.codigos_de_conferencia(self.previa)
        self.assertNotIn("identificacao_incompleta", achados["8900001"])                  # gerador: nada é esperado
        self.assertIn("identificacao_incompleta", achados["8720079"])                    # moto: espera placa e chassi
        r = imp.resumo(self.previa)
        self.assertEqual(r["sem_placa"], 5)                                               # fato informado, não erro
        self.assertEqual(self.situacoes(self.previa)["8900001"], Situacao.VALIDA)

    def test_placa_interna_e_so_uma_nota(self):
        c = [x for x in self.previa.itens.get(codigo_legado="8110009").conferencias if x["codigo"] == "placa_fora_do_padrao"]
        self.assertEqual([x["gravidade"] for x in c], ["info"])
        self.assertEqual(self.previa.itens.get(codigo_legado="8110009").dados["placa"], "C811009")

    def test_setor_nao_informado_entra_sem_setor_e_o_original_fica_guardado(self):
        d = self.previa.itens.get(codigo_legado="9100592").dados
        self.assertEqual((d["setor"], d["setor_original"], d["setor_codigo"]), ("", "Não Informado", "0"))
        self.assertIn("sem_setor", self.codigos_de_conferencia(self.previa)["9100592"])

    def test_inicio_antes_da_compra(self):
        self.assertIn("inicio_antes_da_compra", self.codigos_de_conferencia(self.previa)["8630001"])

    def test_conferencias_agrupadas_por_regra_com_os_equipamentos(self):
        grupos = {g["codigo"]: g for g in imp.conferencias_agrupadas(self.previa)}
        self.assertEqual(grupos["chassi_duplicado"]["n"], 2)
        self.assertEqual({e["codigo_legado"] for e in grupos["chassi_duplicado"]["equipamentos"]}, {"8110001", "8210001"})
        self.assertEqual([e["codigo_legado"] for e in grupos["data_compra_fora_de_faixa"]["equipamentos"]], ["8410008"])
        gravidades = [g["gravidade"] for g in imp.conferencias_agrupadas(self.previa)]
        self.assertEqual(gravidades, sorted(gravidades, key=lambda g: g != "aviso"))

    def test_filtros_da_listagem_da_previa(self):
        self.assertEqual({l.codigo_legado for l in imp.linhas_da_importacao(self.previa, regra="chassi_duplicado")}, {"8110001", "8210001"})
        self.assertEqual([l.codigo_legado for l in imp.linhas_da_importacao(self.previa, busca_texto="honda")], ["8720079"])
        self.assertEqual([l.codigo_legado for l in imp.linhas_da_importacao(self.previa, busca_texto="gerador")], ["8900001"])
        self.assertEqual(imp.linhas_da_importacao(self.previa, situacao="rejeitada").count(), 0)

    def test_codigo_repetido_na_propria_planilha_conta_como_duplicidade_e_nao_cria_dois(self):
        linhas = [linha(8110001, "A"), linha(8110001, "A DE NOVO")]
        previa = self.conferido(self.enviar(linhas))
        self.assertEqual([l.situacao for l in previa.itens.order_by("numero")], [Situacao.VALIDA, Situacao.EXISTENTE])
        self.assertIn("linha 2", previa.itens.order_by("numero")[1].motivos[0])
        self.assertEqual(imp.resumo(previa)["repetidos_na_planilha"], 1)
        imp.confirmar(previa, self.usuario)
        self.assertEqual(FrotaVeiculo.objects.filter(codigo_legado="8110001").count(), 1)

    def test_linha_sem_codigo_ou_com_dado_impossivel_e_rejeitada_com_o_motivo(self):
        linhas = [
            linha("", "SEM CODIGO"), linha(8110002, ""), linha(8110003, "DATA RUIM", compra="31/02/2020"),
            linha(8110004, "ANO RUIM", modelo_ano="abcd"), linha(8110005, "ANTIGA", compra=date(1850, 1, 1)),
        ]
        previa = self.conferido(self.enviar(linhas))
        todos = {l.numero: l for l in previa.itens.all()}
        self.assertEqual({l.situacao for l in todos.values()}, {Situacao.REJEITADA})
        self.assertIn("código do equipamento", todos[2].motivos[0])
        self.assertIn("modelo está vazio", todos[3].motivos[0])
        self.assertIn("31/02/2020", todos[4].motivos[0])
        self.assertIn("ano válido", todos[5].motivos[0])
        self.assertIn("1850", todos[6].motivos[0] + todos[6].dados.get("data_compra", "") or "1850")

    def test_placa_de_outro_equipamento_do_cadastro_rejeita_e_diz_de_quem(self):
        frota.criar_veiculo({"nome": "DONO", "tipo": self.trator.pk, "codigo_legado": "7000001", "placa": "VAY0B31"}, self.usuario)
        previa = self.conferido(self.enviar())
        rejeitada = previa.itens.get(codigo_legado="9100592")
        self.assertEqual(rejeitada.situacao, Situacao.REJEITADA)
        self.assertIn("7000001", rejeitada.motivos[0])

    def test_a_previa_nao_cria_nem_os_cadastros_de_apoio(self):
        self.assertEqual(FrotaEmpresa.objects.count() + FrotaClasseOperacional.objects.count() + FrotaCentroCusto.objects.count(), 0)

    def test_texto_com_prefixo_de_formula_e_so_texto(self):
        previa = self.conferido(self.enviar([linha(8110001, "=1+1", setor="@SOMA(A1)")]))
        l = previa.itens.get()
        self.assertEqual(l.situacao, Situacao.VALIDA)
        self.assertEqual(l.dados["modelo"], "=1+1")
        self.assertIn("texto_com_formula", {c["codigo"] for c in l.conferencias})

    def test_datas_repetidas_em_lote_avisam_uma_vez(self):
        linhas = [linha(8000000 + n, f"T{n}", compra=date(2024, 1, 1)) for n in range(imp.LIMITE_DE_DATA_EM_LOTE)]
        previa = self.conferido(self.enviar(linhas))
        lote = imp.resumo(previa)["datas_em_lote"]
        self.assertEqual([(a["campo"], a["data"], a["n"]) for a in lote if a["campo"] == "data_compra"], [("data_compra", "2024-01-01", imp.LIMITE_DE_DATA_EM_LOTE)])
        self.assertIn("01/01/2024", lote[0]["texto"])
        self.assertEqual(imp.resumo(self.previa)["datas_em_lote"], [])


# ═════════════════════════════════════════════════════════════
# Passos 5 e 6: confirmação e resultado
# ═════════════════════════════════════════════════════════════

class ConfirmacaoTests(ImportacaoBase):
    def test_importa_preservando_o_codigo_original_e_gerando_o_interno(self):
        importacao = self.importar()
        self.assertEqual((importacao.estado, importacao.importados, importacao.rejeitados, importacao.existentes), ("confirmada", 8, 0, 0))
        self.assertEqual(importacao.confirmada_por, self.usuario)
        self.assertIsNotNone(importacao.confirmada_em)
        v = FrotaVeiculo.objects.get(codigo_legado="8110001")
        self.assertEqual((v.codigo, v.nome, v.modelo), ("MQ-00001", "TRATOR NH 3880", "TRATOR NH 3880"))
        self.assertEqual(sorted(FrotaVeiculo.objects.values_list("codigo_legado", flat=True)), sorted(str(l[2]) for l in planilha_do_erp()))
        self.assertEqual(FrotaVeiculo.objects.filter(codigo__startswith="MQ-").count(), 8)

    def test_todo_equipamento_importado_nasce_com_a_validacao_pendente(self):
        self.importar()
        self.assertEqual(FrotaVeiculo.objects.filter(validacao_pendente=True).count(), 8)
        self.assertEqual(set(FrotaVeiculo.objects.values_list("situacao_origem", flat=True)), {"Em Uso"})

    def test_natureza_e_a_empresa_de_cada_equipamento(self):
        self.importar()
        self.assertEqual(FrotaVeiculo.objects.filter(natureza="proprio").count(), 7)
        terceiro = FrotaVeiculo.objects.get(natureza="terceiro")
        self.assertEqual((terceiro.codigo_legado, terceiro.empresa.nome, terceiro.empresa.codigo_erp), ("9100592", "TERCEIROS", "2"))
        self.assertEqual(FrotaEmpresa.objects.get(nome="TERCEIROS").natureza_padrao, "terceiro")

    def test_os_dados_do_erp_chegam_intactos(self):
        self.importar()
        v = FrotaVeiculo.objects.get(codigo_legado="8410008")
        self.assertEqual((v.placa, v.chassi_serie, v.data_compra, v.ano_modelo, v.ano_fabricacao), ("NZH4261", "9534N8246BR157267", date(1905, 7, 3), 2011, 2011))
        self.assertEqual((v.classe_operacional.nome, v.classe_operacional.codigo_erp), ("CAMINHÃO ROLL OFF", "13"))
        self.assertEqual((v.classe_mecanica.nome, v.centro_custo.nome, v.centro_custo.codigo_erp, v.setor), ("CAMINHÃO", "CAMINHAO - GRAOS", "32119", "GRÃOS"))
        v2 = FrotaVeiculo.objects.get(codigo_legado="9100592")
        self.assertEqual((v2.ano_fabricacao, v2.ano_modelo, v2.setor, v2.data_inicio), (2026, 2027, "", date(2026, 10, 30)))
        v3 = FrotaVeiculo.objects.get(codigo_legado="8110001")
        self.assertEqual(FrotaVeiculo.objects.get(codigo_legado="8210001").chassi_serie, v3.chassi_serie)           # o repetido entra como veio

    def test_cria_so_o_que_faltava_e_uma_vez_cada(self):
        self.importar()
        self.assertEqual(FrotaEmpresa.objects.count(), 2)
        self.assertEqual(FrotaClasseOperacional.objects.count(), 7)
        self.assertEqual(FrotaCentroCusto.objects.count(), 6)               # 32119 tem duas grafias na planilha e vira UM cadastro
        self.assertEqual(FrotaClasseMecanica.objects.count(), 6)
        self.assertEqual(FrotaCentroCusto.objects.get(codigo_erp="32119").nome, "CAMINHAO - GRAOS")             # a primeira grafia fica

    def test_a_divergencia_de_nome_do_centro_de_custo_e_avisada_e_nao_resolvida_em_silencio(self):
        importacao = self.importar()
        avisos = imp.resumo(importacao)["avisos_de_cadastro"]
        self.assertEqual(len(avisos), 1)
        self.assertIn("32119", avisos[0])
        self.assertIn("CAMINHÕES GRÃOS", avisos[0])

    def test_categorias_novas_nascem_so_se_nao_existiam(self):
        antes = set(FrotaTipo.objects.values_list("nome", flat=True))
        self.importar()
        novas = set(FrotaTipo.objects.values_list("nome", flat=True)) - antes
        self.assertEqual(novas, {"Motocicleta", "Gerador", "Linha amarela"})
        self.assertTrue(FrotaTipo.objects.get(nome="Motocicleta").espera_placa)
        self.assertEqual(FrotaVeiculo.objects.get(codigo_legado="8210001").tipo.nome, "Colheitadeira")           # já existia: reaproveitada
        self.assertEqual(FrotaVeiculo.objects.get(codigo_legado="8410008").tipo.nome, "Caminhão")

    def test_cada_equipamento_tem_o_evento_de_cadastro_com_a_origem(self):
        importacao = self.importar()
        e = FrotaEvento.objects.get(veiculo__codigo_legado="8110001", tipo=FrotaEvento.Tipo.CADASTRO)
        self.assertEqual((e.dados["origem"], e.dados["importacao"], e.dados["codigo_legado"]), ("importacao", importacao.pk, "8110001"))
        self.assertEqual(e.usuario, self.usuario)
        self.assertEqual(M2MEvento.objects.filter(tipo=M2MEvento.Tipo.CONFIG_FROTA, dados__criado=True).count(), 2 + 7 + 6 + 6 + 3)

    def test_o_chassi_repetido_continua_sendo_apontado_depois_de_importar(self):
        self.importar()
        from services import frota_qualidade_service as qualidade
        v = FrotaVeiculo.objects.select_related("tipo").get(codigo_legado="8210001")
        self.assertIn("chassi_duplicado", [c["codigo"] for c in qualidade.conferencias_do_veiculo(v)])
        self.assertEqual(frota.indicadores_equipamentos()["a_validar"], 8)

    def test_a_mesma_planilha_duas_vezes_nao_duplica_nada(self):
        primeira = self.importar()
        segunda = self.importar()
        self.assertEqual((primeira.importados, segunda.importados, segunda.existentes), (8, 0, 8))
        self.assertEqual(FrotaVeiculo.objects.count(), 8)
        self.assertEqual(FrotaEmpresa.objects.count(), 2)
        self.assertEqual(FrotaVeiculo.objects.filter(codigo__startswith="MQ-").count(), 8)
        self.assertEqual(self.situacoes(segunda), {str(l[2]): Situacao.EXISTENTE for l in planilha_do_erp()})
        self.assertEqual(segunda.itens.filter(veiculo__isnull=False).count(), 8)

    def test_aviso_de_arquivo_ja_importado(self):
        arquivo = xlsx_bytes(planilha_do_erp())           # os MESMOS bytes: o .xlsx recém-gerado muda a cada execução
        primeira = self.conferido(imp.enviar(upload("a.xlsx", arquivo), self.usuario))
        imp.confirmar(primeira, self.usuario)
        segunda = imp.enviar(upload("a.xlsx", arquivo), self.usuario)
        self.assertEqual(imp.arquivo_ja_importado(segunda), primeira)
        self.assertIsNone(imp.arquivo_ja_importado(primeira))
        self.assertIsNone(imp.arquivo_ja_importado(self.enviar([linha(1, "OUTRO")])))

    def test_so_o_que_esta_valido_e_gravado_e_o_resto_fica_no_relatorio(self):
        linhas = [linha(8110001, "A"), linha("", "SEM CODIGO"), linha(8110003, "C")]
        importacao = self.importar(linhas)
        self.assertEqual((importacao.importados, importacao.rejeitados, importacao.linhas), (2, 1, 3))
        self.assertEqual(FrotaVeiculo.objects.count(), 2)
        motivos = dict(imp.motivos_de_rejeicao(importacao))
        self.assertEqual(sum(motivos.values()), 1)

    def test_confirmar_duas_vezes_nao_importa_duas(self):
        previa = self.conferido(self.enviar())
        imp.confirmar(previa, self.usuario)
        with self.assertRaises(ValidationError) as cm:
            imp.confirmar(previa, self.usuario)
        self.assertIn("já foi confirmada", cm.exception.messages[0])
        self.assertEqual(FrotaVeiculo.objects.count(), 8)

    def test_importacao_cancelada_ou_expirada_nao_confirma(self):
        previa = self.conferido(self.enviar())
        FrotaImportacaoEquipamentos.objects.filter(pk=previa.pk).update(criada_em=timezone.now() - timedelta(hours=25))
        with self.assertRaises(ValidationError) as cm:
            imp.confirmar(previa, self.usuario)
        self.assertIn("expirou", cm.exception.messages[0])
        cancelada = self.conferido(self.enviar())
        imp.cancelar(cancelada)
        with self.assertRaises(ValidationError) as cm:
            imp.confirmar(cancelada, self.usuario)
        self.assertIn("cancelada", cm.exception.messages[0])
        with self.assertRaises(ValidationError):
            imp.cancelar(cancelada)
        self.assertEqual(FrotaVeiculo.objects.count(), 0)

    def test_confirmar_exige_quem_confirma(self):
        previa = self.conferido(self.enviar())
        with self.assertRaises(ValidationError):
            imp.confirmar(previa, None)

    def test_revalidacao_ao_confirmar_alguem_cadastrou_o_codigo_no_meio_tempo(self):
        previa = self.conferido(self.enviar())
        frota.criar_veiculo({"nome": "ANTECIPADO", "tipo": self.trator.pk, "codigo_legado": "8110001"}, self.usuario)
        importacao = imp.confirmar(previa, self.usuario)
        self.assertEqual((importacao.importados, importacao.existentes), (7, 1))
        self.assertEqual(FrotaVeiculo.objects.get(codigo_legado="8110001").nome, "ANTECIPADO")               # intacto
        self.assertIn("Revalidação ao confirmar", previa.itens.get(codigo_legado="8110001").motivos[0])

    def test_revalidacao_ao_confirmar_placa_tomada_no_meio_tempo(self):
        previa = self.conferido(self.enviar())
        frota.criar_veiculo({"nome": "PLACA", "tipo": self.trator.pk, "placa": "NZH4261"}, self.usuario)
        importacao = imp.confirmar(previa, self.usuario)
        self.assertEqual((importacao.importados, importacao.rejeitados), (7, 1))
        self.assertIn("placa", previa.itens.get(codigo_legado="8410008").motivos[0])

    def test_a_falha_de_uma_linha_nao_derruba_as_outras(self):
        previa = self.conferido(self.enviar())
        real = frota.criar_veiculo

        def falha_na_terceira(dados, usuario=None, **kw):
            if dados.get("codigo_legado") == "8410008":
                raise ValidationError({"nome": "quebrou"})
            return real(dados, usuario, **kw)

        with mock.patch.object(frota, "criar_veiculo", side_effect=falha_na_terceira):
            importacao = imp.confirmar(previa, self.usuario)
        self.assertEqual((importacao.importados, importacao.rejeitados), (7, 1))
        self.assertFalse(FrotaVeiculo.objects.filter(codigo_legado="8410008").exists())

    def test_o_codigo_interno_continua_a_sequencia_do_que_ja_existe(self):
        frota.criar_veiculo({"nome": "MANUAL", "tipo": self.trator.pk}, self.usuario)                 # MQ-00001
        self.importar([linha(8110001, "A")])
        self.assertEqual(FrotaVeiculo.objects.get(codigo_legado="8110001").codigo, "MQ-00002")

    def test_o_veiculo_cadastrado_a_mao_nao_e_tocado(self):
        v = frota.criar_veiculo({"codigo": "9239322", "nome": "COTONN", "tipo": self.trator.pk}, self.usuario)
        self.importar()
        v.refresh_from_db()
        self.assertEqual((v.codigo, v.codigo_legado, v.nome, v.validacao_pendente), ("9239322", "", "COTONN", False))


class AtualizacaoAutorizadaTests(ImportacaoBase):
    """Importar de novo NÃO mexe no que já existe — a menos que o responsável autorize, e então só nos
    campos que o ERP conhece."""

    def setUp(self):
        super().setUp()
        self.importar([linha(8110001, "TRATOR NH 3880", placa="C811009", chassi="ANTIGO", fabricacao=2011)])
        self.v = FrotaVeiculo.objects.get(codigo_legado="8110001")
        Localidade.objects.get_or_create(local="Fazenda Karitel", defaults={"codigo": "Karitel"})
        frota.editar_veiculo(self.v, {"nome": "MEU NOME", "observacoes": "anotação do campo", "localidade": Localidade.objects.get(local="Fazenda Karitel").pk}, self.usuario)
        frota.mudar_status(self.v, Status.MANUTENCAO, self.usuario, "oficina")

    def segunda(self, **kw):
        nova = [linha(8110001, "TRATOR NH 3880 NOVO", placa="C811009", chassi="NOVO", fabricacao=2012, classe="CAMINHÃO ROLL OFF", centro="CAMINHAO - GRAOS", mecanica="CAMINHÃO")]
        previa = self.conferido(self.enviar(nova))
        return previa, imp.confirmar(previa, self.usuario, **kw)

    def test_sem_autorizacao_o_cadastrado_fica_como_esta(self):
        previa, importacao = self.segunda()
        self.assertEqual((importacao.importados, importacao.atualizados, importacao.existentes), (0, 0, 1))
        self.v.refresh_from_db()
        self.assertEqual((self.v.modelo, self.v.chassi_serie, self.v.ano_fabricacao), ("TRATOR NH 3880", "ANTIGO", 2011))

    def test_a_previa_mostra_as_diferencas_que_a_atualizacao_aplicaria(self):
        previa = self.conferido(self.enviar([linha(8110001, "TRATOR NH 3880 NOVO", placa="C811009", chassi="NOVO", fabricacao=2012)]))
        diferencas = previa.itens.get().dados["diferencas"]
        self.assertEqual(set(diferencas), {"modelo", "chassi_serie", "ano_fabricacao"})
        self.assertEqual(diferencas["chassi_serie"], {"de": "ANTIGO", "para": "NOVO"})
        self.assertEqual(imp.resumo(previa)["com_diferencas"], 1)

    def test_com_autorizacao_muda_so_os_campos_do_erp(self):
        previa, importacao = self.segunda(atualizar_existentes=True)
        self.assertEqual((importacao.atualizados, importacao.importados, importacao.atualizar_existentes), (1, 0, True))
        self.v.refresh_from_db()
        self.assertEqual((self.v.modelo, self.v.chassi_serie, self.v.ano_fabricacao), ("TRATOR NH 3880 NOVO", "NOVO", 2012))
        self.assertEqual(self.v.classe_operacional.nome, "CAMINHÃO ROLL OFF")
        # o que o Zelo gerencia nunca é tocado pela planilha
        self.assertEqual((self.v.nome, self.v.observacoes, self.v.status, self.v.localidade.local), ("MEU NOME", "anotação do campo", Status.MANUTENCAO, "Fazenda Karitel"))
        self.assertEqual(self.v.tipo.nome, "Trator")

    def test_a_atualizacao_grava_o_evento_com_a_origem_e_o_de_para(self):
        self.segunda(atualizar_existentes=True)
        e = FrotaEvento.objects.filter(veiculo=self.v, tipo=FrotaEvento.Tipo.EDICAO).latest("pk")
        self.assertEqual(e.dados["origem"], "importacao")
        self.assertEqual(e.dados["alteracoes"]["chassi_serie"], {"de": "ANTIGO", "para": "NOVO"})

    def test_celula_vazia_nunca_apaga_dado(self):
        nova = [linha(8110001, "TRATOR NH 3880", placa=None, chassi=None, fabricacao=2011)]
        previa = self.conferido(self.enviar(nova))
        imp.confirmar(previa, self.usuario, atualizar_existentes=True)
        self.v.refresh_from_db()
        self.assertEqual((self.v.placa, self.v.chassi_serie), ("C811009", "ANTIGO"))

    def test_atualizar_de_novo_nao_faz_nada(self):
        self.segunda(atualizar_existentes=True)
        previa, importacao = self.segunda(atualizar_existentes=True)
        self.assertEqual((importacao.atualizados, importacao.existentes), (0, 1))

    def test_a_atualizacao_so_alcanca_quem_ja_estava_cadastrado(self):
        previa = self.conferido(self.enviar([linha(8110001, "OUTRO", chassi="NOVO"), linha(8110002, "NOVO EQUIPAMENTO")]))
        importacao = imp.confirmar(previa, self.usuario, atualizar_existentes=True)
        self.assertEqual((importacao.importados, importacao.atualizados), (1, 1))


# ═════════════════════════════════════════════════════════════
# Relatório
# ═════════════════════════════════════════════════════════════

class RelatorioTests(ImportacaoBase):
    def test_o_relatorio_em_dados(self):
        importacao = self.importar([*planilha_do_erp(), linha("", "SEM CODIGO")])
        r = imp.relatorio(importacao)
        self.assertEqual(r["totais"], {"linhas": 9, "validas": 0, "importados": 8, "atualizados": 0, "existentes": 0, "rejeitados": 1, "avisos": 8})
        self.assertEqual((r["criada_por"], r["confirmada_por"], r["estado"]), (self.usuario.username, self.usuario.username, "confirmada"))
        self.assertEqual(len(r["linhas"]), 9)
        self.assertEqual(r["linhas"][0]["veiculo_codigo"], "MQ-00001")

    def test_o_excel_tem_as_quatro_abas_e_as_linhas(self):
        importacao = self.importar()
        wb = load_workbook(io.BytesIO(imp.exportar_relatorio_xlsx(importacao)))
        self.assertEqual(wb.sheetnames, ["Resumo", "Linhas", "Inconsistências", "Mapeamento"])
        self.assertEqual(wb["Linhas"].max_row, 9)
        self.assertEqual(wb["Resumo"]["B1"].value, "equipamentos motorizados.xlsx")
        textos = [c.value for row in wb["Inconsistências"].iter_rows(min_row=2) for c in row if c.value]
        self.assertTrue(any("1905" in str(t) for t in textos))

    def test_o_relatorio_de_inconsistencias_so_leva_resumo_e_inconsistencias(self):
        previa = self.conferido(self.enviar())
        wb = load_workbook(io.BytesIO(imp.exportar_relatorio_xlsx(previa, so_inconsistencias=True)))
        self.assertEqual(wb.sheetnames, ["Resumo", "Inconsistências"])

    def test_o_excel_nao_executa_formula(self):
        importacao = self.importar([linha(8110001, "=HYPERLINK(\"http://x\")", setor="+1")])
        wb = load_workbook(io.BytesIO(imp.exportar_relatorio_xlsx(importacao)))
        celulas = [c for row in wb["Linhas"].iter_rows(min_row=2) for c in row if c.value and str(c.value).startswith(("'", "="))]
        self.assertTrue(celulas)
        for c in celulas:
            self.assertTrue(str(c.value).startswith("'"), c.value)
            self.assertEqual(c.data_type, "s")

    def test_listar_importacoes_traz_a_mais_recente_primeiro(self):
        a, b = self.enviar(), self.enviar()
        self.assertEqual([i.pk for i in imp.listar_importacoes()], [b.pk, a.pk])


# ═════════════════════════════════════════════════════════════
# Aceite: a planilha de verdade
# ═════════════════════════════════════════════════════════════

PLANILHA_REAL = Path(settings.BASE_DIR).parent / "docs" / "equipamentos motorizados.xlsx"


@skipUnless(PLANILHA_REAL.exists(), "a planilha equipamentos motorizados.xlsx não está em docs/")
class PlanilhaRealTests(ImportacaoBase):
    """Os números da análise do prompt, recalculados NA LEITURA da planilha — e os cenários de aceite 1, 2, 3, 10 e 11."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.bytes_da_planilha = PLANILHA_REAL.read_bytes()

    def previa(self):
        return self.conferido(imp.enviar(upload(PLANILHA_REAL.name, self.bytes_da_planilha), self.usuario))

    def test_os_numeros_da_analise_batem(self):
        previa = self.previa()
        self.assertEqual((previa.linhas, previa.validas, previa.rejeitados, previa.existentes), (395, 395, 0, 0))
        dados = [l.dados for l in previa.itens.all()]
        self.assertEqual(len({d["modelo"] for d in dados}), 96)
        self.assertEqual(len({d["classe_operacional"]["nome"] for d in dados}), 28)
        self.assertEqual(len({d["setor_original"] for d in dados}), 7)
        self.assertEqual(imp.resumo(previa)["por_natureza"], {"proprio": 227, "terceiro": 168})
        self.assertEqual(sum(1 for d in dados if not d["placa"]), 193)
        self.assertEqual(sum(1 for d in dados if not d["chassi_serie"]), 35)

    def test_as_ocorrencias_do_prompt_sao_reencontradas_e_nenhuma_e_corrigida(self):
        previa = self.previa()
        achados = self.codigos_de_conferencia(previa)
        duplicados = {c for c, regras in achados.items() if "chassi_duplicado" in regras}
        self.assertEqual(duplicados, {"8110001", "8210001"})                                       # o chassi 1CQS660ALH0120143
        self.assertEqual(previa.itens.get(codigo_legado="8110001").dados["chassi_serie"], "1CQS660ALH0120143")
        self.assertIn("data_compra_fora_de_faixa", achados["8410008"])                             # 03/07/1905
        self.assertEqual(previa.itens.get(codigo_legado="8410008").dados["data_compra"], "1905-07-03")
        self.assertIn("ano_modelo_no_futuro", achados["9100592"])                                  # 2026 / 2027
        d = previa.itens.get(codigo_legado="9100592").dados
        self.assertEqual((d["ano_fabricacao"], d["ano_modelo"]), (2026, 2027))
        self.assertEqual({d["situacao_origem"] for d in (l.dados for l in previa.itens.all())}, {"Em Uso"})

    def test_importa_395_preserva_os_codigos_e_nao_duplica_na_segunda_vez(self):
        previa = self.previa()
        importacao = imp.confirmar(previa, self.usuario)
        self.assertEqual((importacao.importados, importacao.rejeitados), (395, 0))
        self.assertEqual(FrotaVeiculo.objects.count(), 395)
        self.assertEqual(FrotaVeiculo.objects.exclude(codigo_legado="").count(), 395)
        self.assertEqual(FrotaVeiculo.objects.filter(natureza="proprio").count(), 227)
        self.assertEqual(FrotaVeiculo.objects.filter(natureza="terceiro").count(), 168)
        self.assertEqual(FrotaVeiculo.objects.filter(validacao_pendente=True).count(), 395)
        self.assertEqual(FrotaClasseOperacional.objects.count(), 28)
        self.assertEqual(FrotaClasseMecanica.objects.count(), 18)
        self.assertEqual(FrotaCentroCusto.objects.count(), 30)
        self.assertEqual(len(imp.resumo(importacao)["avisos_de_cadastro"]), 7)                    # 7 códigos de custo com duas grafias
        segunda = imp.confirmar(self.previa(), self.usuario)
        self.assertEqual((segunda.importados, segunda.existentes), (0, 395))
        self.assertEqual(FrotaVeiculo.objects.count(), 395)

    def test_a_busca_e_os_filtros_funcionam_sobre_a_base_importada(self):
        imp.confirmar(self.previa(), self.usuario)
        self.assertEqual([v.codigo_legado for v in frota.listar_veiculos({"q": "8110001"})], ["8110001"])
        self.assertEqual(len(list(frota.listar_veiculos({"q": "1CQS660ALH0120143"}))), 2)
        self.assertEqual(len(list(frota.listar_veiculos({"q": "NZH4261"}))), 1)
        self.assertEqual(frota.listar_veiculos({"natureza": "terceiro"}).count(), 168)
        self.assertEqual(frota.listar_veiculos({"setor": "graos"}).count(), 201)
        self.assertEqual(frota.listar_veiculos({"classe_operacional": FrotaClasseOperacional.objects.get(nome="MOTOCICLETA").pk}).count(), 85)
        k = frota.indicadores_equipamentos()
        self.assertEqual((k["total"], k["proprios"], k["terceiros"], k["em_uso"], k["em_manutencao"], k["a_validar"]), (395, 227, 168, 395, 0, 395))
