"""Infraestrutura georreferenciada: coordenadas, estruturas, enlaces PTP e a localização do veículo.

Os pontos que o pedido de 14/11 e a regra 63 exigem: coordenada é a que alguém cadastrou (nunca deduzida),
implausível é recusada com o motivo, enlace só é "operacional" se alguém registrou, e a origem e a hora da
localização acompanham o ponto."""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from ProjetoEstoque.models import FrotaTipo, FrotaVeiculo, TecEnlace, TecEstrutura
from services import agro_infra_service as infra
from services import frota_service


def erros(fn, *a, **kw) -> dict:
    """O `message_dict` da `ValidationError` que a chamada TEM de levantar."""
    try:
        fn(*a, **kw)
    except ValidationError as exc:
        return exc.message_dict
    raise AssertionError("A chamada deveria ter levantado ValidationError.")


class CoordenadaTests(TestCase):
    def test_aceita_virgula_ponto_e_seis_casas(self):
        self.assertEqual(infra.ler_coordenada("-14,7763", "-45.5634"), (Decimal("-14.776300"), Decimal("-45.563400")))
        self.assertEqual(infra.ler_coordenada(Decimal("-14.1234567"), "-45,1"), (Decimal("-14.123457"), Decimal("-45.100000")))

    def test_vazio_nos_dois_campos_e_localizacao_desconhecida(self):
        self.assertEqual(infra.ler_coordenada("", ""), (None, None))
        self.assertEqual(infra.ler_coordenada(None, None), (None, None))

    def test_so_uma_das_duas_e_erro_no_campo_que_falta(self):
        self.assertIn("longitude", erros(infra.ler_coordenada, "-14,7", ""))
        self.assertIn("latitude", erros(infra.ler_coordenada, "", "-45,5"))

    def test_texto_que_nao_e_coordenada_e_recusado(self):
        for ruim in ("abc", "14,77,63", "--14", "1e3", "14 S"):
            with self.subTest(ruim=ruim):
                self.assertIn("latitude", erros(infra.ler_coordenada, ruim, "-45"))

    def test_fora_do_globo_e_recusado(self):
        self.assertIn("A latitude vai de -90 a 90", erros(infra.ler_coordenada, "95", "-45")["latitude"][0])
        self.assertIn("A longitude vai de -180 a 180", erros(infra.ler_coordenada, "-14", "190")["longitude"][0])

    def test_o_ponto_nulo_e_recusado(self):
        self.assertIn("ponto nulo", erros(infra.ler_coordenada, "0", "0")["latitude"][0])

    def test_latitude_e_longitude_trocadas_ou_sinal_esquecido_caem_na_caixa_implausivel(self):
        # longitude sem o sinal (Brasil é todo a oeste) e um ponto de verdade em outro continente (Nova York)
        self.assertIn("América do Sul", erros(infra.ler_coordenada, "-14,7763", "45,5634")["latitude"][0])
        self.assertIn("América do Sul", erros(infra.ler_coordenada, "40,7", "-74,0")["latitude"][0])
        # as duas trocadas: -45,56 / -14,77 tem longitude fora da caixa
        self.assertIn("América do Sul", erros(infra.ler_coordenada, "-45,5634", "-14,7763")["latitude"][0])

    def test_distancia_em_linha_reta(self):
        # 1 grau de latitude = 111,19 km
        self.assertAlmostEqual(infra.distancia_km((-14, -45), (-15, -45)), 111.19, delta=0.1)
        self.assertAlmostEqual(infra.distancia_km((-14.7, -45.5), (-14.7, -45.5)), 0.0, places=6)


class BaseInfra(TestCase):
    def setUp(self):
        self.usuario = User.objects.create(username="agro_infra")
        self.fazenda = frota_service.fazendas()[0]

    def estrutura(self, nome="Torre Norte", **extra):
        dados = {"nome": nome, "tipo": "torre", "latitude": "-14,7763", "longitude": "-45,5634", **extra}
        return infra.salvar_estrutura(dados, self.usuario)


class EstruturaTests(BaseInfra):
    def test_cadastra_com_coordenada_e_hora_da_localizacao(self):
        e = self.estrutura(altura_m="42,5", fazenda=self.fazenda.pk)
        self.assertEqual((e.latitude, e.longitude, e.altura_m), (Decimal("-14.776300"), Decimal("-45.563400"), Decimal("42.5")))
        self.assertIsNotNone(e.localizacao_em)
        self.assertEqual((e.fazenda, e.criado_por), (self.fazenda, self.usuario))
        self.assertTrue(e.tem_coordenada)

    def test_pode_nascer_sem_coordenada_e_diz_localizacao_desconhecida(self):
        e = infra.salvar_estrutura({"nome": "Base Sul", "tipo": "base"}, self.usuario)
        self.assertFalse(e.tem_coordenada)
        self.assertIsNone(e.localizacao_em)

    def test_nome_obrigatorio_e_unico_sem_diferenca_de_caixa(self):
        self.assertIn("nome", erros(infra.salvar_estrutura, {"nome": " "}, self.usuario))
        self.estrutura("Torre Norte")
        self.assertIn("Já existe", erros(infra.salvar_estrutura, {"nome": "TORRE norte"}, self.usuario)["nome"][0])

    def test_coordenada_implausivel_nao_grava_nada(self):
        antes = TecEstrutura.objects.count()
        self.assertIn("latitude", erros(infra.salvar_estrutura, {"nome": "X", "latitude": "0", "longitude": "0"}, self.usuario))
        self.assertEqual(TecEstrutura.objects.count(), antes)

    def test_altura_fora_da_faixa_e_recusada(self):
        self.assertIn("altura_m", erros(infra.salvar_estrutura, {"nome": "Alta", "altura_m": "500"}, self.usuario))
        self.assertIn("altura_m", erros(infra.salvar_estrutura, {"nome": "Alta", "altura_m": "abc"}, self.usuario))

    def test_corrigir_a_coordenada_atualiza_a_hora_e_so_ela(self):
        e = self.estrutura()
        antes = e.localizacao_em
        TecEstrutura.objects.filter(pk=e.pk).update(localizacao_em=antes - timedelta(days=3))
        e.refresh_from_db()
        infra.salvar_estrutura({"altura_m": "30"}, self.usuario, e)
        e.refresh_from_db()
        self.assertEqual(e.localizacao_em, antes - timedelta(days=3))                 # outro campo: a hora da localização não mexe
        infra.salvar_estrutura({"latitude": "-14,80", "longitude": "-45,56"}, self.usuario, e)
        e.refresh_from_db()
        self.assertGreater(e.localizacao_em, antes - timedelta(days=1))

    def test_sem_mudanca_nao_grava(self):
        e = self.estrutura()
        atualizado = e.updated_at
        infra.salvar_estrutura({"nome": "Torre Norte", "altura_m": ""}, self.usuario, e)
        e.refresh_from_db()
        self.assertEqual(e.updated_at, atualizado)

    def test_inativar_sai_do_filtro_de_ativas(self):
        e = self.estrutura()
        infra.salvar_estrutura({"ativo": False}, self.usuario, e)
        self.assertEqual(infra.listar_estruturas({"ativo": "1"}).count(), 0)
        self.assertEqual(infra.listar_estruturas({"ativo": "0"}).count(), 1)

    def test_exige_usuario(self):
        from django.contrib.auth.models import AnonymousUser
        self.assertRaises(ValidationError, infra.salvar_estrutura, {"nome": "A"}, AnonymousUser())

    def test_so_uma_coordenada_no_banco_e_barrada_pela_restricao(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            TecEstrutura.objects.create(nome="Quebrada", latitude=Decimal("-14.7"))

    def test_filtros(self):
        self.estrutura("Torre A", fazenda=self.fazenda.pk)
        infra.salvar_estrutura({"nome": "Base B", "tipo": "base"}, self.usuario)
        self.assertEqual(infra.listar_estruturas({"tipo": "base"}).count(), 1)
        self.assertEqual(infra.listar_estruturas({"coordenada": "sem"}).count(), 1)
        self.assertEqual(infra.listar_estruturas({"coordenada": "com"}).count(), 1)
        self.assertEqual(infra.listar_estruturas({"fazenda": str(self.fazenda.pk)}).count(), 1)
        self.assertEqual(infra.listar_estruturas({"q": "torre"}).count(), 1)


class EnlaceTests(BaseInfra):
    def setUp(self):
        super().setUp()
        self.a = self.estrutura("Torre A")
        self.b = self.estrutura("Torre B", latitude="-14,80", longitude="-45,60")

    def enlace(self, ident="PTP-01", **extra):
        return infra.salvar_enlace({"identificacao": ident, "estrutura_a": self.a.pk, "estrutura_b": self.b.pk, **extra}, self.usuario)

    def test_nasce_nao_informado_sem_data_de_estado(self):
        e = self.enlace()
        self.assertEqual(e.estado, TecEnlace.Estado.NAO_INFORMADO)
        self.assertIsNone(e.estado_em)
        self.assertTrue(e.tem_geometria)

    def test_estado_registrado_na_criacao_leva_autor_e_hora(self):
        e = self.enlace(estado="operacional")
        self.assertEqual((e.estado, e.estado_por), ("operacional", self.usuario))
        self.assertIsNotNone(e.estado_em)

    def test_mudar_o_estado_registra_quem_e_quando_e_so_quando_muda(self):
        e = self.enlace()
        infra.salvar_enlace({"estado": "manutencao"}, self.usuario, e)
        e.refresh_from_db()
        primeira = e.estado_em
        self.assertEqual((e.estado, e.estado_por), ("manutencao", self.usuario))
        infra.salvar_enlace({"estado": "manutencao", "observacoes": "troca de antena"}, self.usuario, e)
        e.refresh_from_db()
        self.assertEqual(e.estado_em, primeira)

    def test_as_pontas_precisam_ser_diferentes(self):
        self.assertIn("diferentes", erros(infra.salvar_enlace, {"identificacao": "X", "estrutura_a": self.a.pk, "estrutura_b": self.a.pk}, self.usuario)["estrutura_b"][0])

    def test_identificacao_unica_sem_diferenca_de_caixa(self):
        self.enlace("PTP-01")
        self.assertIn("Já existe", erros(infra.salvar_enlace, {"identificacao": "ptp-01", "estrutura_a": self.a.pk, "estrutura_b": self.b.pk}, self.usuario)["identificacao"][0])

    def test_ponta_inativa_nao_pode_ser_escolhida(self):
        infra.salvar_estrutura({"ativo": False}, self.usuario, self.b)
        self.assertIn("inativa", erros(infra.salvar_enlace, {"identificacao": "Y", "estrutura_a": self.a.pk, "estrutura_b": self.b.pk}, self.usuario)["estrutura_b"][0])

    def test_distancia_so_com_as_duas_coordenadas(self):
        e = self.enlace()
        self.assertGreater(infra.distancia_do_enlace_km(e), 1)
        sem = infra.salvar_estrutura({"nome": "Sem ponto"}, self.usuario)
        e2 = infra.salvar_enlace({"identificacao": "PTP-02", "estrutura_a": self.a.pk, "estrutura_b": sem.pk}, self.usuario)
        self.assertIsNone(infra.distancia_do_enlace_km(e2))
        self.assertFalse(e2.tem_geometria)

    def test_frequencia(self):
        self.assertEqual(self.enlace(frequencia_ghz="5,8").frequencia_ghz, Decimal("5.80"))
        self.assertIn("frequencia_ghz", erros(infra.salvar_enlace, {"identificacao": "F", "estrutura_a": self.a.pk, "estrutura_b": self.b.pk, "frequencia_ghz": "500"}, self.usuario))

    def test_restricao_do_banco_barra_enlace_de_uma_estrutura_para_ela_mesma(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            TecEnlace.objects.create(identificacao="loop", estrutura_a=self.a, estrutura_b=self.a)

    def test_indicadores_contam_o_registrado(self):
        self.enlace("PTP-01", estado="operacional")
        self.enlace("PTP-02", estado="manutencao")
        self.enlace("PTP-03")
        infra.salvar_estrutura({"nome": "Estação X", "tipo": "estacao"}, self.usuario)
        i = infra.indicadores_infra()
        self.assertEqual((i["torres"], i["estacoes"], i["sem_coordenada"]), (2, 1, 1))
        self.assertEqual((i["enlaces"], i["operacionais"], i["manutencao"], i["nao_informados"]), (3, 1, 1, 1))

    def test_listar_enlaces_filtra_por_estado_e_ponta(self):
        self.enlace("PTP-01", estado="operacional")
        self.enlace("PTP-02")
        self.assertEqual(infra.listar_enlaces({"estado": "operacional"}).count(), 1)
        self.assertEqual(infra.listar_enlaces({"estrutura": str(self.a.pk)}).count(), 2)
        self.assertEqual(infra.listar_enlaces({"q": "Torre B"}).count(), 2)


class LocalizacaoDoVeiculoTests(TestCase):
    def setUp(self):
        self.usuario = User.objects.create(username="frota_loc")
        self.tipo = FrotaTipo.objects.order_by("pk").first()

    def veiculo(self, **extra):
        dados = {"codigo": "MQ-LOC-1", "nome": "Trator", "tipo": self.tipo.pk, **extra}
        return frota_service.criar_veiculo(dados, self.usuario)

    def test_sem_coordenada_a_localizacao_e_desconhecida(self):
        v = self.veiculo()
        self.assertEqual((v.latitude, v.longitude, v.localizacao_origem, v.localizacao_em), (None, None, "", None))

    def test_coordenada_digitada_e_cadastral_e_leva_a_hora(self):
        v = self.veiculo(latitude="-14,7763", longitude="-45,5634")
        self.assertEqual((v.localizacao_origem, v.latitude), ("cadastral", Decimal("-14.776300")))
        self.assertIsNotNone(v.localizacao_em)

    def test_coordenada_implausivel_nao_cria_o_veiculo(self):
        with self.assertRaises(ValidationError) as ctx:
            self.veiculo(latitude="40,7", longitude="-74,0")
        self.assertIn("latitude", ctx.exception.message_dict)
        self.assertFalse(FrotaVeiculo.objects.filter(codigo="MQ-LOC-1").exists())

    def test_editar_a_coordenada_atualiza_origem_e_hora_e_audita(self):
        v = self.veiculo()
        frota_service.editar_veiculo(v, {"latitude": "-14,8", "longitude": "-45,6"}, self.usuario)
        v.refresh_from_db()
        self.assertEqual((v.localizacao_origem, v.latitude), ("cadastral", Decimal("-14.800000")))
        evento = v.eventos.order_by("-pk").first()
        self.assertEqual(evento.dados["alteracoes"]["latitude"]["para"], "-14.800000")

    def test_apagar_a_coordenada_volta_a_localizacao_desconhecida(self):
        v = self.veiculo(latitude="-14,7763", longitude="-45,5634")
        frota_service.editar_veiculo(v, {"latitude": "", "longitude": ""}, self.usuario)
        v.refresh_from_db()
        self.assertEqual((v.latitude, v.localizacao_origem, v.localizacao_em), (None, "", None))

    def test_editar_outro_campo_nao_mexe_na_localizacao(self):
        v = self.veiculo(latitude="-14,7763", longitude="-45,5634")
        antes = v.localizacao_em
        frota_service.editar_veiculo(v, {"setor": "Colheita"}, self.usuario)
        v.refresh_from_db()
        self.assertEqual(v.localizacao_em, antes)

    def test_filtros_por_fabricante_e_localizacao(self):
        self.veiculo(codigo="MQ-A", fabricante="John Deere", latitude="-14,7", longitude="-45,5")
        self.veiculo(codigo="MQ-B", fabricante="Stara")
        self.assertEqual({v.codigo for v in frota_service.listar_veiculos({"fabricante": "john deere"})}, {"MQ-A"})
        self.assertEqual({v.codigo for v in frota_service.listar_veiculos({"localizacao": "com"})}, {"MQ-A"})
        self.assertEqual({v.codigo for v in frota_service.listar_veiculos({"localizacao": "sem"})}, {"MQ-B"})

    def test_a_restricao_do_banco_barra_so_uma_coordenada(self):
        v = self.veiculo()
        with self.assertRaises(IntegrityError), transaction.atomic():
            FrotaVeiculo.objects.filter(pk=v.pk).update(latitude=Decimal("-14.7"))
