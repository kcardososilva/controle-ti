"""
`services/busca_service.py` — busca sem acento nem diferença de caixa, também no SQLite.

O `LIKE` do SQLite só ignora a caixa das letras ASCII: "caminhões" não achava "CAMINHÕES TABACO".
Sem este teste a regressão seria silenciosa (a lista volta vazia e parece que o equipamento não existe).
"""
from django.test import SimpleTestCase, TestCase

from ProjetoEstoque.models import FrotaCentroCusto, FrotaTipo, FrotaVeiculo
from services import busca_service as busca
from services import frota_service as frota


class NormalizarBuscaTests(SimpleTestCase):
    def test_tira_acento_caixa_e_espacos(self):
        self.assertEqual(busca.normalizar_busca("  CAMINHÕES   Tabaco "), "caminhoes tabaco")
        self.assertEqual(busca.normalizar_busca("Ação · Grãos"), "acao · graos")

    def test_none_e_vazio(self):
        self.assertEqual(busca.normalizar_busca(None), "")
        self.assertEqual(busca.normalizar_busca(""), "")

    def test_numero_vira_texto(self):
        self.assertEqual(busca.normalizar_busca(8110001), "8110001")


class SemAcentoTests(TestCase):
    def setUp(self):
        self.tipo = FrotaTipo.objects.get(nome="Caminhão")
        self.v = FrotaVeiculo.objects.create(codigo="MQ-1", nome="CAMINHÃO ROLL OFF", tipo=self.tipo, setor="GRÃOS")

    def achados(self, texto):
        return list(
            FrotaVeiculo.objects.alias(n=busca.SemAcento("nome")).filter(n__contains=busca.normalizar_busca(texto))
        )

    def test_acha_com_ou_sem_acento_e_em_qualquer_caixa(self):
        for texto in ("CAMINHÃO", "caminhão", "caminhao", "CAMINHAO ROLL", "roll off", "ão"):
            with self.subTest(texto=texto):
                self.assertEqual(self.achados(texto), [self.v])

    def test_nao_acha_o_que_nao_existe(self):
        self.assertEqual(self.achados("trator"), [])

    def test_igualdade_tambem_funciona(self):
        achados = FrotaVeiculo.objects.alias(s=busca.SemAcento("setor")).filter(s=busca.normalizar_busca("grãos"))
        self.assertEqual(list(achados), [self.v])

    def test_campo_vazio_nao_quebra(self):
        FrotaVeiculo.objects.create(codigo="MQ-2", nome="X", tipo=self.tipo, modelo="")
        self.assertEqual(FrotaVeiculo.objects.alias(m=busca.SemAcento("modelo")).filter(m__contains="zzz").count(), 0)


class BuscaDaListaTests(TestCase):
    """A busca principal da lista de equipamentos usa isto: o centro de custo "CAMINHÕES TABACO"
    precisa ser achado por quem digita "caminhoes", "caminhões" ou "CAMINHÕES"."""

    def setUp(self):
        tipo = FrotaTipo.objects.get(nome="Caminhão")
        self.centro = FrotaCentroCusto.objects.create(codigo_erp="32120", nome="CAMINHÕES TABACO")
        self.v = FrotaVeiculo.objects.create(codigo="MQ-1", nome="VW 24.250", tipo=tipo, centro_custo=self.centro, modelo="ATEGO")
        FrotaVeiculo.objects.create(codigo="MQ-2", nome="OUTRO", tipo=tipo)

    def test_centro_de_custo_por_nome_sem_acento_e_em_minusculas(self):
        for termo in ("caminhoes", "caminhões", "CAMINHÕES TABACO", "tabaco"):
            with self.subTest(termo=termo):
                self.assertEqual([x.pk for x in frota.listar_veiculos({"q": termo})], [self.v.pk])

    def test_modelo_sem_diferenca_de_caixa(self):
        self.assertEqual([x.pk for x in frota.listar_veiculos({"q": "atego"})], [self.v.pk])

    def test_o_filtro_de_setor_ignora_acento_e_caixa(self):
        FrotaVeiculo.objects.filter(pk=self.v.pk).update(setor="GRÃOS")
        self.assertEqual([x.pk for x in frota.listar_veiculos({"setor": "graos"})], [self.v.pk])
