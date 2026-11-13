"""
Testes da integração com a API do NinjaOne (OAuth Authorization Code) — Architecture Rule 59.

Nada aqui fala com a rede: `requests.post/get` são substituídos por respostas de mentira.
O que se prova:
  • o fluxo de autorização é à prova do que o torna perigoso (state, segredo, redirect);
  • o token é cifrado no banco e nenhuma mensagem de erro repete o que o NinjaOne devolve;
  • a sincronização é idempotente, não duplica o que veio da planilha e não apaga o que a API
    não sabe;
  • o CSV continua gravando exatamente como antes do núcleo ter sido extraído.

IMPORTANTE: o formato dos campos da API (`system.biosSerialNumber`, `memory.capacity`...) é o que
a documentação do NinjaOne descreve, e ainda NÃO foi conferido contra uma resposta real — estes
testes provam o mapeamento CONTRA ESSE FORMATO, não contra o NinjaOne. A conferência real é
`manage.py sincronizar_ninja --diagnostico`, depois de conectar.
"""

import io
import time
from datetime import timedelta
from unittest import mock
from urllib.parse import parse_qs, urlparse

import requests
from cryptography.fernet import Fernet
from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from ProjetoEstoque.models import Item, NinjaConexao, NinjaDevice
from services import ninja_api_service as api
from services.ninja_service import importar_csv

BASE = "https://ninja.exemplo.test"
HOST = "sistema.exemplo.test:65300"
RETORNO = f"http://{HOST}/ninja/oauth/callback/"
SEGREDO = "segredo-que-nunca-pode-vazar"
CHAVE = Fernet.generate_key().decode()
CONFIG = dict(
    NINJA_BASE_URL=BASE, NINJA_CLIENT_ID="cid-123", NINJA_CLIENT_SECRET=SEGREDO,
    NINJA_REDIRECT_URI=RETORNO, NINJA_SCOPES="monitoring offline_access", NINJA_CHAVE_CIFRA=CHAVE,
    ALLOWED_HOSTS=["testserver", "sistema.exemplo.test"],
)


class Resp:
    """Resposta de mentira de `requests`."""

    def __init__(self, status=200, corpo=None):
        self.status_code = status
        self._corpo = corpo

    def json(self):
        if self._corpo is None:
            raise ValueError("sem json")
        return self._corpo


def tokens(access="AT-1", refresh="RT-1", expira=3600, scope="monitoring offline_access"):
    corpo = {"access_token": access, "expires_in": expira, "scope": scope, "token_type": "Bearer"}
    if refresh:
        corpo["refresh_token"] = refresh
    return Resp(200, corpo)


def dispositivo(**extra):
    d = {
        "id": 101, "organizationId": 1, "locationId": 5, "nodeClass": "WINDOWS_WORKSTATION",
        "approvalStatus": "APPROVED", "offline": False,
        "displayName": "NB-0101", "systemName": "NB-0101", "dnsName": "nb-0101.local",
        "lastContact": 1_700_000_000.5,
        "system": {"manufacturer": "Dell", "model": "Latitude 5420", "biosSerialNumber": "SN123"},
        "os": {"name": "Windows 11 Pro"},
        "memory": {"capacity": 17179869184},
        "processors": [{"name": "Intel Core i5"}],
        "ipAddresses": ["10.0.0.7"],
    }
    d.update(extra)
    return d


def roteador_get(devs=None, orgs=None, locais=None, logados=None, logados_status=200):
    """Substituto de requests.get: responde por caminho."""
    devs = [dispositivo()] if devs is None else devs
    orgs = [{"id": 1, "name": "Santa Colomba"}] if orgs is None else orgs
    locais = [{"id": 5, "name": "Karitel", "organizationId": 1}] if locais is None else locais
    logados = [{"deviceId": 101, "userName": "DOM\\ana.silva"}] if logados is None else logados

    def get(url, params=None, headers=None, **kw):
        caminho = url.replace(BASE, "")
        if caminho == "/v2/organizations":
            return Resp(200, orgs)
        if caminho == "/v2/locations":
            return Resp(200, locais)
        if caminho == "/v2/devices-detailed":
            return Resp(200, devs)
        if caminho == "/v2/queries/logged-on-users":
            return Resp(logados_status, {"results": logados} if logados_status == 200 else {})
        return Resp(404, {})

    return get


@override_settings(**CONFIG)
class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="ti.ninja", password="senha-de-teste-123", is_staff=True)
        cls.comum = User.objects.create_user(username="comum.ninja", password="senha-de-teste-123")

    def conectar_direto(self, access="AT-1", refresh="RT-1", expira_em=None):
        """Deixa o sistema conectado sem passar pelo fluxo (para testar o que vem depois)."""
        c = NinjaConexao.get()
        c.access_cifrado = api._cifrar(access)
        c.refresh_cifrado = api._cifrar(refresh) if refresh else ""
        c.expira_em = expira_em or (timezone.now() + timedelta(hours=1))
        c.conectado_por = self.staff
        c.conectado_em = timezone.now()
        c.save()
        return c

    def mensagens(self, resposta):
        return [str(m) for m in get_messages(resposta.wsgi_request)]


# ═════════════════════════════════════════════════════════════
# Configuração e autorização
# ═════════════════════════════════════════════════════════════

class ConfiguracaoTests(Base):
    def test_completa_nao_tem_motivo(self):
        self.assertEqual(api.motivo_nao_configurada(), "")

    def test_diz_o_que_falta_no_env(self):
        with override_settings(NINJA_CLIENT_ID=""):
            self.assertIn("NINJA_CLIENT_ID", api.motivo_nao_configurada())

    def test_base_http_e_recusada(self):
        # é para lá que vão o secret e os tokens: sem TLS, não
        with override_settings(NINJA_BASE_URL="http://ninja.exemplo.test"):
            self.assertIn("https", api.motivo_nao_configurada())

    def test_sem_chave_de_cifra_diz_como_gerar(self):
        with override_settings(NINJA_CHAVE_CIFRA=""):
            motivo = api.motivo_nao_configurada()
        self.assertIn("NINJA_CHAVE_CIFRA", motivo)
        self.assertIn("Fernet.generate_key()", motivo)

    def test_chave_invalida_e_recusada(self):
        with override_settings(NINJA_CHAVE_CIFRA="isto-nao-e-uma-chave"):
            self.assertIn("não é uma chave Fernet válida", api.motivo_nao_configurada())


class AutorizacaoTests(Base):
    def test_url_tem_state_escopo_e_redirect_e_nao_vaza_o_segredo(self):
        sessao = {}
        url = api.iniciar_autorizacao(sessao)
        p = urlparse(url)
        q = parse_qs(p.query)
        self.assertEqual(f"{p.scheme}://{p.netloc}{p.path}", f"{BASE}/ws/oauth/authorize")
        self.assertEqual(q["response_type"], ["code"])
        self.assertEqual(q["client_id"], ["cid-123"])
        self.assertEqual(q["redirect_uri"], [RETORNO])          # sempre o configurado
        self.assertEqual(q["scope"], ["monitoring offline_access"])
        self.assertEqual(q["state"], [sessao[api.SESSAO_STATE]["state"]])
        self.assertNotIn(SEGREDO, url)
        self.assertIn("monitoring%20offline_access", url)      # %20, e não "+"

    def test_state_e_aleatorio_a_cada_pedido(self):
        a, b = {}, {}
        api.iniciar_autorizacao(a)
        api.iniciar_autorizacao(b)
        self.assertNotEqual(a[api.SESSAO_STATE]["state"], b[api.SESSAO_STATE]["state"])

    def test_state_e_de_uso_unico(self):
        sessao = {}
        api.iniciar_autorizacao(sessao)
        state = sessao[api.SESSAO_STATE]["state"]
        self.assertTrue(api.validar_state(sessao, state))
        self.assertFalse(api.validar_state(sessao, state))

    def test_state_errado_e_recusado_e_consome_o_pedido(self):
        sessao = {}
        api.iniciar_autorizacao(sessao)
        state = sessao[api.SESSAO_STATE]["state"]
        self.assertFalse(api.validar_state(sessao, "outro-valor"))
        self.assertFalse(api.validar_state(sessao, state))  # já consumido: não dá para retentar

    def test_state_vencido_e_recusado(self):
        sessao = {}
        api.iniciar_autorizacao(sessao)
        state = sessao[api.SESSAO_STATE]["state"]
        sessao[api.SESSAO_STATE]["em"] = time.time() - api.STATE_VALIDADE_S - 5
        self.assertFalse(api.validar_state(sessao, state))

    def test_sem_pedido_previo_ou_sem_state_e_recusado(self):
        self.assertFalse(api.validar_state({}, "qualquer"))
        sessao = {}
        api.iniciar_autorizacao(sessao)
        self.assertFalse(api.validar_state(sessao, ""))

    def test_nao_inicia_se_faltar_configuracao(self):
        with override_settings(NINJA_CLIENT_SECRET=""):
            with self.assertRaises(api.NinjaErro):
                api.iniciar_autorizacao({})


# ═════════════════════════════════════════════════════════════
# Troca do código e guarda do token
# ═════════════════════════════════════════════════════════════

class TrocaDoCodigoTests(Base):
    def test_guarda_os_tokens_cifrados(self):
        with mock.patch.object(api.requests, "post", return_value=tokens("AT-SECRETO", "RT-SECRETO")) as post:
            res = api.concluir_autorizacao("cod-1", self.staff)

        self.assertTrue(res["refresh_token"])
        c = NinjaConexao.get()
        # em claro nunca: nem access, nem refresh
        self.assertNotIn("AT-SECRETO", c.access_cifrado)
        self.assertNotIn("RT-SECRETO", c.refresh_cifrado)
        # e dá para ler de volta
        self.assertEqual(api._decifrar(c.access_cifrado), "AT-SECRETO")
        self.assertEqual(api._decifrar(c.refresh_cifrado), "RT-SECRETO")
        self.assertTrue(c.conectada)
        self.assertEqual(c.conectado_por, self.staff)
        self.assertAlmostEqual((c.expira_em - timezone.now()).total_seconds(), 3600, delta=30)

        args, kw = post.call_args
        self.assertEqual(args[0], f"{BASE}/ws/oauth/token")
        self.assertEqual(kw["data"]["grant_type"], "authorization_code")
        self.assertEqual(kw["data"]["code"], "cod-1")
        self.assertEqual(kw["data"]["redirect_uri"], RETORNO)
        self.assertEqual(kw["data"]["client_secret"], SEGREDO)
        # o corpo leva o secret: NÃO pode seguir redirecionamento
        self.assertIs(kw["allow_redirects"], False)

    def test_resposta_sem_refresh_token_e_sinalizada(self):
        with mock.patch.object(api.requests, "post", return_value=tokens("AT-1", refresh=None)):
            res = api.concluir_autorizacao("cod-1", self.staff)
        self.assertFalse(res["refresh_token"])
        self.assertEqual(NinjaConexao.get().refresh_cifrado, "")

    def test_conexao_nova_nao_herda_o_refresh_antigo(self):
        self.conectar_direto(refresh="RT-ANTIGO")
        with mock.patch.object(api.requests, "post", return_value=tokens("AT-2", refresh=None)):
            api.concluir_autorizacao("cod-1", self.staff)
        self.assertEqual(NinjaConexao.get().refresh_cifrado, "")

    def test_codigo_recusado_nao_grava_nada_e_nao_repete_o_corpo(self):
        with mock.patch.object(api.requests, "post", return_value=Resp(400, {"error": "invalid_token"})):
            with self.assertRaises(api.NinjaErro) as ctx:
                api.concluir_autorizacao("cod-ruim", self.staff)
        self.assertIn("Conectar de novo", str(ctx.exception))
        self.assertFalse(NinjaConexao.get().conectada)

    def test_sem_chave_de_cifra_nao_gasta_o_codigo(self):
        # o código é de uso único: gastá-lo sem ter onde guardar o token obrigaria a refazer tudo
        with override_settings(NINJA_CHAVE_CIFRA=""):
            with mock.patch.object(api.requests, "post") as post:
                with self.assertRaises(api.NinjaErro):
                    api.concluir_autorizacao("cod-1", self.staff)
        post.assert_not_called()

    def test_codigo_vazio_nem_chama_o_ninja(self):
        with mock.patch.object(api.requests, "post") as post:
            with self.assertRaises(api.NinjaErro):
                api.concluir_autorizacao("  ", self.staff)
        post.assert_not_called()

    def test_erro_que_ecoa_o_token_nao_vaza_na_mensagem(self):
        # O NinjaOne devolve o refresh token enviado DENTRO do errorMessage.
        eco = Resp(400, {"resultCode": "FAILURE", "errorMessage": "Invalid UUID string: RT-ECOADO-123"})
        self.conectar_direto(expira_em=timezone.now() - timedelta(minutes=5))
        with mock.patch.object(api.requests, "post", return_value=eco):
            with self.assertRaises(api.NinjaErro) as ctx:
                api._access_token()
        self.assertNotIn("RT-ECOADO-123", str(ctx.exception))
        self.assertNotIn("RT-ECOADO-123", NinjaConexao.get().ultimo_erro)

    def test_erro_generico_do_ninja_mostra_so_o_status(self):
        corpo = {"error": "server_error", "errorMessage": "ECO-DO-TOKEN-123"}
        with mock.patch.object(api.requests, "post", return_value=Resp(500, corpo)):
            with self.assertRaises(api.NinjaErro) as ctx:
                api.concluir_autorizacao("cod-1", self.staff)
        self.assertIn("HTTP 500", str(ctx.exception))
        self.assertNotIn("ECO-DO-TOKEN-123", str(ctx.exception))

    def test_falha_de_rede_nao_vaza_nem_encadeia_a_causa(self):
        boom = requests.ConnectionError(f"falhou {BASE} client_secret={SEGREDO}")
        with mock.patch.object(api.requests, "post", side_effect=boom):
            with self.assertRaises(api.NinjaErro) as ctx:
                api.concluir_autorizacao("cod-1", self.staff)
        self.assertNotIn(SEGREDO, str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)

    def test_mensagens_por_tipo_de_erro(self):
        casos = {
            "invalid_client": "Client ID/secret",
            "unauthorized_client": "tipo Web",
            "invalid_scope": "NINJA_SCOPES",
        }
        for codigo, trecho in casos.items():
            with self.subTest(codigo=codigo):
                with mock.patch.object(api.requests, "post", return_value=Resp(400, {"error": codigo})):
                    with self.assertRaises(api.NinjaErro) as ctx:
                        api.concluir_autorizacao("cod-1", self.staff)
                self.assertIn(trecho, str(ctx.exception))


# ═════════════════════════════════════════════════════════════
# Token em uso e renovação
# ═════════════════════════════════════════════════════════════

class RenovacaoTests(Base):
    def test_token_valido_nao_chama_o_ninja(self):
        self.conectar_direto(access="AT-VIVO")
        with mock.patch.object(api.requests, "post") as post:
            self.assertEqual(api._access_token(), "AT-VIVO")
        post.assert_not_called()

    def test_sem_conexao_diz_para_conectar(self):
        with self.assertRaises(api.NinjaErro) as ctx:
            api._access_token()
        self.assertIn("Conectar ao NinjaOne", str(ctx.exception))

    def test_perto_de_vencer_renova_e_guarda_o_refresh_novo(self):
        self.conectar_direto(access="AT-VELHO", refresh="RT-1",
                             expira_em=timezone.now() + timedelta(seconds=30))  # dentro da margem
        with mock.patch.object(api.requests, "post", return_value=tokens("AT-NOVO", "RT-2")) as post:
            self.assertEqual(api._access_token(), "AT-NOVO")
        self.assertEqual(post.call_args.kwargs["data"]["grant_type"], "refresh_token")
        self.assertEqual(post.call_args.kwargs["data"]["refresh_token"], "RT-1")
        c = NinjaConexao.get()
        self.assertEqual(api._decifrar(c.access_cifrado), "AT-NOVO")
        self.assertEqual(api._decifrar(c.refresh_cifrado), "RT-2")

    def test_resposta_de_renovacao_sem_refresh_novo_mantem_o_anterior(self):
        self.conectar_direto(refresh="RT-1", expira_em=timezone.now() - timedelta(minutes=1))
        with mock.patch.object(api.requests, "post", return_value=tokens("AT-NOVO", refresh=None)):
            api._access_token()
        self.assertEqual(api._decifrar(NinjaConexao.get().refresh_cifrado), "RT-1")

    def test_vencido_e_sem_refresh_pede_reconexao_sem_chamar_o_ninja(self):
        self.conectar_direto(refresh=None, expira_em=timezone.now() - timedelta(minutes=1))
        with mock.patch.object(api.requests, "post") as post:
            with self.assertRaises(api.NinjaErro) as ctx:
                api._access_token()
        post.assert_not_called()
        self.assertIn("offline_access", str(ctx.exception))

    def test_refresh_recusado_encerra_a_conexao_e_registra_o_motivo(self):
        self.conectar_direto(expira_em=timezone.now() - timedelta(minutes=1))
        with mock.patch.object(api.requests, "post", return_value=Resp(400, {"error": "invalid_grant"})):
            with self.assertRaises(api.NinjaErro):
                api._access_token()
        c = NinjaConexao.get()
        self.assertFalse(c.conectada)
        self.assertIn("Conecte de novo", c.ultimo_erro)

    def test_corrida_outro_processo_ja_renovou_nao_derruba_a_conexao(self):
        """O refresh daqui queimou porque a tarefa agendada renovou primeiro: o token novo dela já
        está no banco. Encerrar a conexão por isso seria desfazer o trabalho do outro processo."""
        self.conectar_direto(access="AT-VELHO", refresh="RT-1", expira_em=timezone.now() - timedelta(minutes=1))

        def outro_processo_renova_antes(*a, **kw):
            NinjaConexao.objects.filter(pk=1).update(
                access_cifrado=api._cifrar("AT-DO-OUTRO"), refresh_cifrado=api._cifrar("RT-2"),
                expira_em=timezone.now() + timedelta(hours=1))
            return Resp(400, {"error": "invalid_grant"})

        with mock.patch.object(api.requests, "post", side_effect=outro_processo_renova_antes):
            self.assertEqual(api._access_token(), "AT-DO-OUTRO")
        self.assertTrue(NinjaConexao.get().conectada)

    def test_chave_de_cifra_trocada_pede_reconexao(self):
        self.conectar_direto()
        with override_settings(NINJA_CHAVE_CIFRA=Fernet.generate_key().decode()):
            with self.assertRaises(api.NinjaErro) as ctx:
                api._access_token()
        self.assertIn("NINJA_CHAVE_CIFRA", str(ctx.exception))

    def test_401_da_api_renova_uma_vez_e_repete(self):
        self.conectar_direto(access="AT-REVOGADO", refresh="RT-1")
        chamadas = []

        def get(url, params=None, headers=None, **kw):
            chamadas.append(headers["Authorization"])
            return Resp(401, {}) if len(chamadas) == 1 else Resp(200, [{"id": 1}])

        with mock.patch.object(api.requests, "post", return_value=tokens("AT-NOVO", "RT-2")) as post, \
                mock.patch.object(api.requests, "get", side_effect=get):
            cliente = api._Cliente()
            self.assertEqual(cliente.get("/v2/organizations"), [{"id": 1}])
        self.assertEqual(chamadas, ["Bearer AT-REVOGADO", "Bearer AT-NOVO"])
        self.assertEqual(post.call_count, 1)

    def test_get_nao_segue_redirecionamento(self):
        self.conectar_direto()
        with mock.patch.object(api.requests, "get", return_value=Resp(200, [])) as get:
            api._Cliente().get("/v2/organizations")
        self.assertIs(get.call_args.kwargs["allow_redirects"], False)

    def test_erros_http_da_api_viram_mensagem_sem_corpo(self):
        self.conectar_direto()
        for status, trecho in ((403, "negou acesso"), (404, "não tem o recurso"),
                               (429, "limite"), (500, "HTTP 500")):
            with self.subTest(status=status):
                with mock.patch.object(api.requests, "get", return_value=Resp(status, {"errorMessage": "CORPO-SECRETO"})):
                    with self.assertRaises(api.NinjaErro) as ctx:
                        api._Cliente().get("/v2/x")
                self.assertIn(trecho, str(ctx.exception))
                self.assertNotIn("CORPO-SECRETO", str(ctx.exception))


class PaginacaoTests(Base):
    def test_pagina_que_nao_traz_novidade_encerra_o_laco(self):
        """Se o NinjaOne ignorasse `after`, devolveria sempre a mesma página cheia: sem a guarda o
        laço só pararia no teto de páginas e o resultado sairia duplicado centenas de vezes."""
        cheia = [{"id": i} for i in range(1, api.PAGINA + 1)]

        class Cliente:
            chamadas = 0

            def get(self, caminho, params=None):
                Cliente.chamadas += 1
                return cheia

        itens = api._listar_por_id(Cliente(), "/v2/devices-detailed")
        self.assertEqual(len(itens), api.PAGINA)
        self.assertEqual(Cliente.chamadas, 2)

    def test_segue_o_cursor_do_id_ate_a_pagina_curta(self):
        p1 = [{"id": i} for i in range(1, api.PAGINA + 1)]
        p2 = [{"id": api.PAGINA + 1}, {"id": api.PAGINA + 2}]
        pedidos = []

        class Cliente:
            def get(self, caminho, params=None):
                pedidos.append(dict(params))
                return p1 if "after" not in params else p2

        itens = api._listar_por_id(Cliente(), "/v2/devices-detailed")
        self.assertEqual(len(itens), api.PAGINA + 2)
        self.assertEqual(pedidos[1]["after"], api.PAGINA)

    def test_consulta_por_cursor_segue_ate_acabar(self):
        respostas = iter([
            {"cursor": {"name": "c1"}, "results": [{"deviceId": 1}]},
            {"cursor": {"name": "c2"}, "results": [{"deviceId": 2}]},
            {"results": [{"deviceId": 3}]},
        ])

        class Cliente:
            def get(self, caminho, params=None):
                return next(respostas)

        self.assertEqual(len(api._listar_por_cursor(Cliente(), "/v2/queries/x")), 3)


# ═════════════════════════════════════════════════════════════
# Mapeamento API -> dispositivo
# ═════════════════════════════════════════════════════════════

class MapeamentoTests(Base):
    ORGS = {1: "Santa Colomba"}
    LOCAIS = {5: "Karitel"}

    def mapear(self, d, usuarios=None):
        return api._mapear_dispositivo(d, self.ORGS, self.LOCAIS, usuarios or {})

    def test_campos_do_formato_documentado(self):
        r = self.mapear(dispositivo(), {101: "ana.silva"})
        self.assertEqual(r["display"], "NB-0101")
        self.assertEqual(r["ninja_id"], 101)
        self.assertEqual(r["serial"], "SN123")
        self.assertEqual(r["model"], "Latitude 5420")
        self.assertEqual(r["manufacturer"], "Dell")
        self.assertEqual(r["org"], "Santa Colomba")
        self.assertEqual(r["local"], "Karitel")
        self.assertEqual(r["os_name"], "Windows 11 Pro")
        self.assertEqual(r["processor"], "Intel Core i5")
        self.assertEqual(r["total_memory_mb"], 16384)
        self.assertEqual(r["ip_address"], "10.0.0.7")
        self.assertEqual(r["hostname"], "nb-0101.local")
        self.assertEqual(r["last_user"], "ana.silva")
        self.assertTrue(r["is_online"])
        self.assertTrue(r["preservar_vazios"])

    def test_instante_em_segundos_ms_e_texto(self):
        em_s = self.mapear(dispositivo(lastContact=1_700_000_000.5))["last_contact"]
        em_ms = self.mapear(dispositivo(lastContact=1_700_000_000_500))["last_contact"]
        self.assertEqual(em_s, em_ms)
        self.assertEqual(em_s.year, 2023)
        self.assertEqual(em_s.utcoffset(), timedelta(0))
        texto = self.mapear(dispositivo(lastContact="2026-11-10T10:00:00Z"))["last_contact"]
        self.assertEqual((texto.year, texto.month), (2026, 11))

    def test_sem_o_campo_offline_nao_afirma_que_esta_online(self):
        d = dispositivo()
        del d["offline"]
        self.assertFalse(self.mapear(d)["is_online"])
        self.assertFalse(self.mapear(dispositivo(offline=True))["is_online"])

    def test_serie_lixo_de_bios_e_descartada(self):
        d = dispositivo(system={"biosSerialNumber": "To Be Filled By O.E.M."})
        self.assertEqual(self.mapear(d)["serial"], "")

    def test_cai_para_serialNumber_quando_nao_ha_serie_de_bios(self):
        d = dispositivo(system={"serialNumber": "SN-FALLBACK"})
        self.assertEqual(self.mapear(d)["serial"], "SN-FALLBACK")

    def test_nao_entra_pendente_nem_sem_id_nem_sem_nome(self):
        self.assertIsNone(self.mapear(dispositivo(approvalStatus="PENDING")))
        self.assertIsNone(self.mapear(dispositivo(id=None)))
        self.assertIsNone(self.mapear(dispositivo(id="101")))
        self.assertIsNone(self.mapear(dispositivo(displayName="", systemName="", dnsName="")))

    def test_formato_inesperado_nao_estoura(self):
        d = dispositivo(system="texto", os=None, memory=[], processors="x", ipAddresses=None)
        r = self.mapear(d)
        self.assertEqual(r["serial"], "")
        self.assertIsNone(r["total_memory_mb"])
        self.assertEqual(r["ip_address"], "")

    def test_texto_muito_longo_e_truncado_no_limite_do_campo(self):
        r = self.mapear(dispositivo(nodeClass="X" * 500))
        self.assertEqual(len(r["node_class"]), 60)


# ═════════════════════════════════════════════════════════════
# Sincronização
# ═════════════════════════════════════════════════════════════

class SincronizacaoTests(Base):
    def setUp(self):
        self.conectar_direto()

    def sincronizar(self, **kw):
        with mock.patch.object(api.requests, "get", side_effect=roteador_get(**kw)):
            return api.sincronizar(user=self.staff)

    def test_cria_o_dispositivo_e_vincula_o_item_pela_serie(self):
        item = Item.objects.create(nome="Notebook Ana", numero_serie="sn123", quantidade=1)  # caixa diferente
        res = self.sincronizar()

        self.assertEqual((res["total"], res["criados"], res["atualizados"], res["vinculados"]), (1, 1, 0, 1))
        d = NinjaDevice.objects.get()
        self.assertEqual(d.ninja_id, 101)
        self.assertEqual(d.item, item)
        self.assertEqual(d.serial_number, "SN123")
        self.assertEqual(d.model_name, "Latitude 5420")
        self.assertEqual(d.manufacturer, "Dell")
        self.assertEqual(d.os_name, "Windows 11 Pro")
        self.assertEqual(d.total_memory_mb, 16384)
        self.assertEqual(d.ip_address, "10.0.0.7")
        self.assertEqual(d.node_class, "WINDOWS_WORKSTATION")
        self.assertEqual(d.local, "Karitel")
        self.assertEqual(d.organization_name, "Santa Colomba")
        self.assertEqual(d.last_user, "ana.silva")
        self.assertTrue(d.is_online)

    def test_e_idempotente(self):
        self.sincronizar()
        res = self.sincronizar()
        self.assertEqual(NinjaDevice.objects.count(), 1)
        self.assertEqual((res["criados"], res["atualizados"]), (0, 1))

    def test_adota_o_dispositivo_vindo_da_planilha_em_vez_de_duplicar(self):
        NinjaDevice.objects.create(display_name="NB-0101", serial_number="SN123", last_user="joao")
        self.sincronizar()
        d = NinjaDevice.objects.get()          # um só
        self.assertEqual(d.ninja_id, 101)

    def test_renome_no_ninja_acompanha_o_mesmo_registro(self):
        NinjaDevice.objects.create(display_name="NOME-ANTIGO", ninja_id=101)
        self.sincronizar()
        d = NinjaDevice.objects.get()
        self.assertEqual(d.display_name, "NB-0101")

    def test_fonte_parcial_nao_apaga_o_que_a_planilha_trouxe(self):
        NinjaDevice.objects.create(display_name="NB-0101", serial_number="SN-DA-PLANILHA",
                                   last_user="maria", model_name="Modelo da planilha", local="Pinheiros")
        pobre = dispositivo(system={}, locationId=None)
        self.sincronizar(devs=[pobre], logados=[])
        d = NinjaDevice.objects.get()
        self.assertEqual(d.serial_number, "SN-DA-PLANILHA")
        self.assertEqual(d.last_user, "maria")
        self.assertEqual(d.model_name, "Modelo da planilha")
        self.assertEqual(d.local, "Pinheiros")

    def test_usuario_logado_indisponivel_nao_derruba_a_sincronizacao(self):
        NinjaDevice.objects.create(display_name="NB-0101", last_user="maria")
        res = self.sincronizar(logados_status=403)
        self.assertEqual(res["total"], 1)
        self.assertTrue(any("Usuário logado não atualizado" in a for a in res["avisos"]))
        self.assertEqual(NinjaDevice.objects.get().last_user, "maria")

    def test_pendente_de_aprovacao_fica_de_fora(self):
        res = self.sincronizar(devs=[dispositivo(), dispositivo(id=102, displayName="NB-PEND", approvalStatus="PENDING")])
        self.assertEqual(res["total"], 1)
        self.assertFalse(NinjaDevice.objects.filter(display_name="NB-PEND").exists())

    def test_api_vazia_nao_grava_nem_apaga_nada(self):
        NinjaDevice.objects.create(display_name="EXISTENTE", ninja_id=7)
        with self.assertRaises(api.NinjaErro) as ctx:
            self.sincronizar(devs=[])
        self.assertIn("Nada foi alterado", str(ctx.exception))
        self.assertEqual(NinjaDevice.objects.count(), 1)
        self.assertIn("Nada foi alterado", NinjaConexao.get().ultimo_erro)

    def test_registros_que_nao_sao_dispositivo_nao_viram_sucesso_silencioso(self):
        with self.assertRaises(api.NinjaErro) as ctx:
            self.sincronizar(devs=[{"foo": "bar"}, {"id": "x"}])
        self.assertIn("formato inesperado", str(ctx.exception))
        self.assertEqual(NinjaDevice.objects.count(), 0)

    def test_sucesso_limpa_o_erro_anterior_e_marca_a_hora(self):
        NinjaConexao.objects.filter(pk=1).update(ultimo_erro="erro velho", ultimo_erro_em=timezone.now())
        self.sincronizar()
        c = NinjaConexao.get()
        self.assertEqual(c.ultimo_erro, "")
        self.assertIsNotNone(c.ultima_sincronizacao_em)

    def test_cria_snapshot_como_a_importacao_de_planilha(self):
        from ProjetoEstoque.models import NinjaDeviceSnapshot
        self.sincronizar()
        self.assertEqual(NinjaDeviceSnapshot.objects.count(), 1)

    # ── trava ──

    def test_trava_impede_duas_sincronizacoes_ao_mesmo_tempo(self):
        NinjaConexao.objects.filter(pk=1).update(sincronizando_desde=timezone.now())
        with mock.patch.object(api.requests, "get") as get:
            with self.assertRaises(api.NinjaErro) as ctx:
                api.sincronizar()
        get.assert_not_called()
        self.assertIn("em andamento", str(ctx.exception))
        # a trava de quem está rodando não pode ser solta por quem foi barrado
        self.assertIsNotNone(NinjaConexao.get().sincronizando_desde)

    def test_trava_esquecida_expira_sozinha(self):
        NinjaConexao.objects.filter(pk=1).update(
            sincronizando_desde=timezone.now() - timedelta(minutes=api.TRAVA_SYNC_MIN + 1))
        self.assertEqual(self.sincronizar()["total"], 1)

    def test_trava_e_solta_ao_terminar_e_ao_falhar(self):
        self.sincronizar()
        self.assertIsNone(NinjaConexao.get().sincronizando_desde)
        with self.assertRaises(api.NinjaErro):
            self.sincronizar(devs=[])
        self.assertIsNone(NinjaConexao.get().sincronizando_desde)

    def test_falha_inesperada_vira_mensagem_segura_e_solta_a_trava(self):
        with mock.patch.object(api, "gravar_dispositivos", side_effect=RuntimeError("detalhe-interno")):
            with mock.patch.object(api.requests, "get", side_effect=roteador_get()):
                with self.assertRaises(api.NinjaErro) as ctx:
                    api.sincronizar()
        self.assertNotIn("detalhe-interno", str(ctx.exception))
        self.assertNotIn("detalhe-interno", NinjaConexao.get().ultimo_erro)
        self.assertIsNone(NinjaConexao.get().sincronizando_desde)

    def test_nao_sincroniza_desconectado(self):
        NinjaConexao.objects.filter(pk=1).update(access_cifrado="", refresh_cifrado="")
        with self.assertRaises(api.NinjaErro) as ctx:
            api.sincronizar()
        self.assertIn("Conectar ao NinjaOne", str(ctx.exception))

    def test_diagnostico_mostra_nomes_de_campo_e_nunca_valores(self):
        with mock.patch.object(api.requests, "get", side_effect=roteador_get()):
            d = api.diagnosticar()
        self.assertEqual(d["dispositivos"], 1)
        self.assertIn("biosSerialNumber", d["campos_system"])
        self.assertEqual(d["mapeados"], 1)
        achatado = repr(d)
        for valor in ("SN123", "ana.silva", "10.0.0.7", "NB-0101"):
            self.assertNotIn(valor, achatado)


# ═════════════════════════════════════════════════════════════
# O CSV não pode ter mudado de comportamento com a extração do núcleo
# ═════════════════════════════════════════════════════════════

class CsvContinuaIgualTests(Base):
    CSV = (
        "Dispositivo,Organização,Local,Modelo,Número de série BIOS,Último login,Status de atividade,"
        "Último tempo de atividade\n"
        "NB-CSV-1,Santa Colomba,Karitel,Latitude,SN-CSV-1,DOM\\joao,Online,2026-11-10T10:00:00Z\n"
        "NB-CSV-2,Santa Colomba,Karitel,Latitude,SN-CSV-2,,Offline,\n"
    )

    def importar(self, texto):
        return importar_csv(io.BytesIO(texto.encode("utf-8")), user=self.staff)

    def test_importa_vincula_e_conta(self):
        item = Item.objects.create(nome="Notebook João", numero_serie="SN-CSV-1", quantidade=1)
        res = self.importar(self.CSV)
        self.assertTrue(res["ok"])
        self.assertEqual((res["total"], res["criados"], res["atualizados"], res["vinculados"]), (2, 2, 0, 1))
        self.assertEqual(res["locais"], {"Karitel": 2})

        d1 = NinjaDevice.objects.get(display_name="NB-CSV-1")
        self.assertEqual(d1.item, item)
        self.assertEqual(d1.last_user, "joao")
        self.assertTrue(d1.is_online)
        self.assertEqual(d1.hostname, "NB-CSV-1")
        self.assertIsNone(d1.ninja_id)         # a planilha nunca informa id
        d2 = NinjaDevice.objects.get(display_name="NB-CSV-2")
        self.assertFalse(d2.is_online)
        self.assertEqual(d2.last_user, "")

    def test_reimportar_atualiza_e_a_planilha_continua_podendo_zerar_o_login(self):
        """Semântica antiga preservada: na planilha a coluna vem inteira, então vazio SIM sobrescreve."""
        self.importar(self.CSV)
        novo = self.CSV.replace("DOM\\joao", "")
        res = self.importar(novo)
        self.assertEqual((res["criados"], res["atualizados"]), (0, 2))
        self.assertEqual(NinjaDevice.objects.count(), 2)
        self.assertEqual(NinjaDevice.objects.get(display_name="NB-CSV-1").last_user, "")

    def test_csv_nao_apaga_o_que_so_a_api_sabe(self):
        NinjaDevice.objects.create(display_name="NB-CSV-1", ninja_id=55, os_name="Windows 11 Pro",
                                   total_memory_mb=8192, ip_address="10.1.1.1", node_class="MAC")
        self.importar(self.CSV)
        d = NinjaDevice.objects.get(display_name="NB-CSV-1")
        self.assertEqual((d.ninja_id, d.os_name, d.total_memory_mb, d.ip_address, d.node_class),
                         (55, "Windows 11 Pro", 8192, "10.1.1.1", "MAC"))

    def test_planilha_sem_coluna_dispositivo_e_recusada(self):
        res = self.importar("Nome,Outro\nx,y\n")
        self.assertFalse(res["ok"])
        self.assertIn("Dispositivo", res["erro"])


# ═════════════════════════════════════════════════════════════
# Views
# ═════════════════════════════════════════════════════════════

class ViewsTests(Base):
    def entrar(self, usuario):
        self.client.force_login(usuario)

    def conectar_get(self, **extra):
        return self.client.get(reverse("ninja_oauth_conectar"), **extra)

    # ── conectar ──

    def test_conectar_manda_ao_ninja_com_state_na_sessao(self):
        self.entrar(self.staff)
        r = self.conectar_get(HTTP_HOST=HOST)
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r["Location"].startswith(f"{BASE}/ws/oauth/authorize?"))
        state = parse_qs(urlparse(r["Location"]).query)["state"][0]
        self.assertEqual(self.client.session[api.SESSAO_STATE]["state"], state)

    def test_conectar_em_host_diferente_do_cadastrado_e_recusado_com_explicacao(self):
        self.entrar(self.staff)
        r = self.conectar_get()  # host "testserver"
        self.assertRedirects(r, reverse("ninja_dashboard"), fetch_redirect_response=False)
        self.assertIn(HOST, self.mensagens(r)[0])
        self.assertNotIn(api.SESSAO_STATE, self.client.session)

    def test_conectar_exige_staff(self):
        self.entrar(self.comum)
        r = self.conectar_get(HTTP_HOST=HOST)
        self.assertRedirects(r, reverse("ninja_dashboard"), fetch_redirect_response=False)
        self.assertNotIn(api.SESSAO_STATE, self.client.session)

    def test_conectar_sem_login_vai_para_o_login(self):
        r = self.conectar_get(HTTP_HOST=HOST)
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r["Location"])

    def test_conectar_sem_configuracao_explica(self):
        self.entrar(self.staff)
        with override_settings(NINJA_CLIENT_ID=""):
            r = self.conectar_get(HTTP_HOST=HOST)
        self.assertIn("NINJA_CLIENT_ID", self.mensagens(r)[0])

    # ── callback ──

    def callback(self, **params):
        return self.client.get(reverse("ninja_oauth_callback"), params, HTTP_HOST=HOST)

    def preparar_state(self):
        r = self.conectar_get(HTTP_HOST=HOST)
        return parse_qs(urlparse(r["Location"]).query)["state"][0]

    def test_callback_feliz_conecta(self):
        self.entrar(self.staff)
        state = self.preparar_state()
        with mock.patch.object(api.requests, "post", return_value=tokens("AT-9", "RT-9")):
            r = self.callback(code="cod-1", state=state)
        self.assertRedirects(r, reverse("ninja_dashboard"), fetch_redirect_response=False)
        self.assertTrue(NinjaConexao.get().conectada)
        self.assertIn("Conectado ao NinjaOne", self.mensagens(r)[0])
        self.assertNotIn(api.SESSAO_STATE, self.client.session)

    def test_callback_sem_refresh_avisa_que_a_conexao_e_curta(self):
        self.entrar(self.staff)
        state = self.preparar_state()
        with mock.patch.object(api.requests, "post", return_value=tokens("AT-9", refresh=None)):
            r = self.callback(code="cod-1", state=state)
        self.assertIn("SEM refresh token", self.mensagens(r)[0])

    def test_callback_com_state_forjado_nao_troca_codigo_algum(self):
        self.entrar(self.staff)
        self.preparar_state()
        with mock.patch.object(api.requests, "post") as post:
            r = self.callback(code="cod-do-atacante", state="state-forjado")
        post.assert_not_called()
        self.assertFalse(NinjaConexao.get().conectada)
        self.assertIn("Não foi possível confirmar", self.mensagens(r)[0])

    def test_callback_sem_pedido_previo_nao_troca_codigo_algum(self):
        self.entrar(self.staff)
        with mock.patch.object(api.requests, "post") as post:
            self.callback(code="cod-1", state="qualquer")
        post.assert_not_called()

    def test_callback_com_erro_do_ninja_mostra_motivo_conhecido(self):
        self.entrar(self.staff)
        state = self.preparar_state()
        with mock.patch.object(api.requests, "post") as post:
            r = self.callback(error="invalid_scope", state=state)
        post.assert_not_called()
        self.assertIn("NINJA_SCOPES", self.mensagens(r)[0])

    def test_callback_nao_repete_texto_de_erro_vindo_da_url(self):
        self.entrar(self.staff)
        state = self.preparar_state()
        r = self.callback(error="<script>alert(1)</script>", state=state)
        self.assertNotIn("script", self.mensagens(r)[0])

    def test_callback_exige_staff(self):
        self.entrar(self.comum)
        with mock.patch.object(api.requests, "post") as post:
            self.callback(code="cod-1", state="x")
        post.assert_not_called()

    def test_callback_com_state_ok_mas_codigo_recusado_mostra_mensagem(self):
        self.entrar(self.staff)
        state = self.preparar_state()
        with mock.patch.object(api.requests, "post", return_value=Resp(400, {"error": "invalid_token"})):
            r = self.callback(code="velho", state=state)
        self.assertIn("Conectar de novo", self.mensagens(r)[0])
        self.assertFalse(NinjaConexao.get().conectada)

    # ── sincronizar / desconectar ──

    def test_sincronizar_so_aceita_post(self):
        self.entrar(self.staff)
        self.assertEqual(self.client.get(reverse("ninja_sincronizar")).status_code, 405)

    def test_sincronizar_sem_conexao_diz_para_conectar(self):
        self.entrar(self.staff)
        r = self.client.post(reverse("ninja_sincronizar"))
        self.assertIn("Conectar ao NinjaOne", self.mensagens(r)[0])

    def test_sincronizar_grava_e_avisa(self):
        self.entrar(self.comum)  # sincronizar é leitura: qualquer interno
        c = NinjaConexao.get()
        c.access_cifrado, c.refresh_cifrado = api._cifrar("AT-1"), api._cifrar("RT-1")
        c.expira_em = timezone.now() + timedelta(hours=1)
        c.save()
        with mock.patch.object(api.requests, "get", side_effect=roteador_get()):
            r = self.client.post(reverse("ninja_sincronizar"))
        self.assertRedirects(r, reverse("ninja_dashboard"), fetch_redirect_response=False)
        self.assertIn("Sincronização concluída: 1 dispositivo(s)", self.mensagens(r)[0])
        self.assertEqual(NinjaDevice.objects.count(), 1)

    def test_desconectar_limpa_os_tokens(self):
        self.entrar(self.staff)
        self.conectar_direto()
        r = self.client.post(reverse("ninja_desconectar"))
        self.assertFalse(NinjaConexao.get().conectada)
        self.assertIn("não foi revogado", self.mensagens(r)[0])

    def test_desconectar_exige_staff_e_post(self):
        self.conectar_direto()
        self.entrar(self.comum)
        self.client.post(reverse("ninja_desconectar"))
        self.assertTrue(NinjaConexao.get().conectada)
        self.entrar(self.staff)
        self.assertEqual(self.client.get(reverse("ninja_desconectar")).status_code, 405)
        self.assertTrue(NinjaConexao.get().conectada)

    # ── dashboard ──

    def dashboard(self):
        r = self.client.get(reverse("ninja_dashboard"))
        self.assertEqual(r.status_code, 200)
        return r.content.decode("utf-8")

    def test_dashboard_nao_configurado_diz_o_que_falta(self):
        self.entrar(self.staff)
        with override_settings(NINJA_CLIENT_ID=""):
            html = self.dashboard()
        self.assertIn("Ainda não configurada", html)
        self.assertIn("NINJA_CLIENT_ID", html)
        self.assertNotIn("Conectar ao NinjaOne", html)

    def test_dashboard_desconectado_oferece_conectar_so_a_staff(self):
        self.entrar(self.staff)
        self.assertIn("Conectar ao NinjaOne", self.dashboard())
        self.entrar(self.comum)
        html = self.dashboard()
        self.assertNotIn(reverse("ninja_oauth_conectar"), html)
        self.assertIn("Peça a um administrador", html)

    def test_dashboard_conectado_oferece_sincronizar_e_nunca_mostra_token(self):
        self.entrar(self.staff)
        self.conectar_direto(access="AT-NAO-PODE-APARECER", refresh="RT-NAO-PODE-APARECER")
        html = self.dashboard()
        self.assertIn("Sincronizar agora", html)
        self.assertIn("Desconectar", html)
        self.assertNotIn("AT-NAO-PODE-APARECER", html)
        self.assertNotIn("RT-NAO-PODE-APARECER", html)
        self.assertNotIn(SEGREDO, html)
        self.assertNotIn(CHAVE, html)

    def test_dashboard_conectado_sem_refresh_avisa(self):
        self.entrar(self.staff)
        self.conectar_direto(refresh=None)
        self.assertIn("offline_access", self.dashboard())

    def test_dashboard_expirado_oferece_reconectar_e_nao_sincronizar(self):
        self.entrar(self.staff)
        self.conectar_direto(refresh=None, expira_em=timezone.now() - timedelta(minutes=1))
        html = self.dashboard()
        self.assertIn("Reconectar", html)
        self.assertNotIn("Sincronizar agora", html)

    def test_dashboard_mostra_o_ultimo_erro(self):
        self.entrar(self.staff)
        self.conectar_direto()
        NinjaConexao.objects.filter(pk=1).update(ultimo_erro="O NinjaOne respondeu HTTP 500.",
                                                 ultimo_erro_em=timezone.now())
        self.assertIn("HTTP 500", self.dashboard())
