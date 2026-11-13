"""
Monitoramento TIM M2M — backend (Fase A): transporte, interpretação, regras e ciclo.

O domínio da API não resolve no DNS e não há documentação pública dos endpoints,
então NENHUM teste toca a rede: o transporte recebe uma sessão HTTP falsa
(`Sessao`/`Resp`, no molde de `test_chamados_conversa.TransporteGraphTests`) e
`time.sleep` é substituído (as esperas de retentativa e a pausa do ciclo viram
chamadas contáveis).

O que estes testes protegem — e quase nenhum derruba tela:

  • HONESTIDADE — falha de consulta nunca vira "chip sem sessão" nem "indisponível";
    404 é `nao_encontrado`; sem mapeamento nada é interpretado.
  • SITUAÇÃO ATUAL ≠ HISTÓRICO — uma falha não substitui o último dado válido e uma
    consulta nova não apaga a anterior.
  • NÃO MARTELAR — retentativa só onde cabe, ciclo para no 401/429 e não volta com
    a credencial recusada.
  • SEGREDO — o token vai cifrado ao banco e não aparece em log, mensagem, evento,
    payload nem no admin.
  • ALERTA — uma falha isolada não gera ocorrência; N geram uma só, e ela se
    resolve sozinha.
"""

import io
import json
import logging
from datetime import timedelta
from unittest import mock

import requests
from cryptography.fernet import Fernet
from django.contrib.auth.models import Group, Permission, User
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models import ProtectedError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from ProjetoEstoque.models import (
    GRUPO_FROTAS,
    GRUPO_FROTAS_CONSULTA,
    CanalNotificacao,
    FrotaRegraAlerta,
    FrotaTipo,
    M2MChip,
    M2MConsulta,
    M2MEvento,
    M2MIntegracao,
    M2MOcorrencia,
    M2MOcorrenciaTratamento,
)
from services import email_alertas
from services import frota_service
from services import frota_vinculo_service as vinculos
from services import m2m_interpretacao as interp
from services import m2m_monitoramento_service as m2m
from services import tim_m2m_service as tim

TOKEN = "TOKEN-SECRETO-ABC123XYZ789"
CHAVE = Fernet.generate_key().decode()
BASE = "https://tim-m2m-platform.com"


def iccid_valido(n: int = 1) -> str:
    corpo = f"89551000000{n:07d}"          # 18 dígitos + 1 verificador = 19
    return corpo + tim.calcular_digito_luhn(corpo)


def imsi_valido(n: int = 1) -> str:
    return f"72410{n:010d}"


# ─────────────────────────────────────────────────────────────
# Sessão HTTP falsa
# ─────────────────────────────────────────────────────────────

class Resp:
    def __init__(self, status=200, json=None, content=None, headers=None):
        self.status_code = status
        self._json = json
        self.headers = headers or {}
        if content is None:
            content = b"" if json is None else __import__("json").dumps(json).encode()
        self.content = content

    def json(self):
        if self._json is None:
            raise ValueError("sem JSON")
        return self._json


class Sessao:
    """`respostas`: lista (uma por chamada, na ordem; exceção é levantada) ou função
    `fn(url) -> Resp`. Guarda cada chamada em `chamadas` como `(url, kwargs)`."""

    def __init__(self, respostas):
        self.respostas, self.chamadas = respostas, []

    def get(self, url, **kw):
        self.chamadas.append((url, kw))
        r = self.respostas(url) if callable(self.respostas) else self.respostas.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    @property
    def urls(self):
        return [c[0] for c in self.chamadas]


SESSAO_UP = {"sessao": {"estado": "UP", "ip": "10.0.0.5"}}
SESSAO_DOWN = {"sessao": {"estado": "DOWN"}}
USO_MB = {"uso": {"total": 250.5, "inicio": "2026-11-01", "fim": "2026-11-30"}}

MAPA = {
    "sessao": {
        "campo_estado": "sessao.estado", "valores_ativa": ["UP", "ONLINE"],
        "valores_sem_sessao": ["DOWN"], "campos_exibir": ["sessao.ip"],
    },
    "consumo": {
        "campo_volume": "uso.total", "unidade_fixa": "MB", "base": 1000,
        "campo_periodo_inicio": "uso.inicio", "campo_periodo_fim": "uso.fim",
    },
    "identidade": {"campo_iccid": "sim.iccid"},
}


def roteador(sessao=SESSAO_UP, uso=USO_MB, status=200):
    """`fn(url)`: devolve o JSON de sessão para `/session` e o de consumo para `/usage`."""
    def fn(url):
        return Resp(status, json=sessao if url.endswith("/session") else uso)
    return fn


@override_settings(TIM_M2M_BASE_URL=BASE, TIM_M2M_TOKEN=TOKEN, TIM_M2M_CHAVE_CIFRA=CHAVE)
class M2MBase(TestCase):
    """Token no ambiente, `sleep` falso, logs capturados (e fora do arquivo real)."""

    def setUp(self):
        self.usuario = User.objects.create(username="ti_m2m")        # sem senha: o hash PBKDF2 custaria ~0,3 s por teste
        M2MIntegracao.get()          # a linha única existe antes de qualquer `.update()` dos testes
        patch = mock.patch("time.sleep")
        self.sleep = patch.start()
        self.addCleanup(patch.stop)

        self.logs: list[str] = []
        teste = self

        class Captura(logging.Handler):
            def emit(self, registro):
                teste.logs.append(registro.getMessage() + (f" | {registro.exc_text}" if registro.exc_text else ""))

        captura = Captura()
        for nome in ("services.tim_m2m_service", "services.m2m_monitoramento_service"):
            logger = logging.getLogger(nome)
            guardado = (logger.propagate, logger.level)
            logger.propagate, logger.level = False, logging.DEBUG
            logger.addHandler(captura)
            self.addCleanup(self._restaurar_logger, logger, captura, guardado)

    @staticmethod
    def _restaurar_logger(logger, captura, guardado):
        logger.removeHandler(captura)
        logger.propagate, logger.level = guardado

    # ── fábricas ──
    def chip(self, n=1, **extra):
        dados = {"nome": f"Chip {n}", "iccid": iccid_valido(n)}
        dados.update(extra)
        return m2m.criar_chip(dados, self.usuario)

    def veiculo(self, n=1, **extra):
        """Um veículo ativo (do primeiro tipo semeado pela migration 0191)."""
        dados = {"codigo": f"VEI-{n:03d}", "nome": f"Veículo {n}", "tipo": FrotaTipo.objects.order_by("pk").first().pk}
        dados.update(extra)
        return frota_service.criar_veiculo(dados, self.usuario)

    def vincular(self, chip, veiculo=None):
        """Vincula o chip a um veículo (um novo, se não vier) e recarrega o chip. O `sem_sessao`
        só vale para chip VINCULADO."""
        vinculos.vincular(chip, veiculo or self.veiculo(), self.usuario, "teste")
        chip.refresh_from_db()
        return chip

    def cliente(self, respostas):
        sessao = Sessao(respostas)
        return tim.ClienteTIM(sessao), sessao

    def atualizar(self, chip, respostas, tipos=("sessao", "consumo"), origem="agendada", **kw):
        cliente, sessao = self.cliente(respostas)
        resultado = m2m.atualizar(chip, tipos, origem, self.usuario, cliente=cliente, **kw)
        chip.refresh_from_db()
        return resultado, sessao

    def falhar(self, chip, vezes, status=500):
        """`vezes` atualizações agendadas que falham (cada uma esgota as retentativas)."""
        for _ in range(vezes):
            self.atualizar(chip, roteador(status=status), avaliar=False)
        chip.refresh_from_db()

    def mapear(self, mapa=MAPA):
        m2m.salvar_mapeamento(mapa, self.usuario)

    def sleeps_de(self, valor):
        return [c for c in self.sleep.call_args_list if c.args == (valor,)]


# ═════════════════════════════════════════════════════════════
# Identificadores
# ═════════════════════════════════════════════════════════════

class IdentificadoresTests(SimpleTestCase):
    def test_iccid_valido_com_19_e_com_20_digitos(self):
        self.assertEqual(tim.normalizar_iccid(iccid_valido(7)), iccid_valido(7))
        corpo20 = "895510000000000000" + "12"[:1]  # 19 dígitos de corpo
        vinte = corpo20 + tim.calcular_digito_luhn(corpo20)
        self.assertEqual(len(vinte), 20)
        self.assertEqual(tim.normalizar_iccid(vinte), vinte)

    def test_iccid_aceita_espacos_pontos_e_hifens(self):
        bruto = iccid_valido(3)
        formatado = " ".join(bruto[i:i + 4] for i in range(0, len(bruto), 4))
        self.assertEqual(tim.normalizar_iccid(formatado), bruto)
        self.assertEqual(tim.normalizar_iccid(bruto[:6] + "-" + bruto[6:]), bruto)

    def test_iccid_recusa_com_mensagem_em_portugues(self):
        valido = iccid_valido(5)
        digito_errado = str((int(valido[-1]) + 1) % 10)
        casos = {
            "": "Informe o ICCID",
            "89AB5100000000000001": "apenas dígitos",
            valido[:-1] + digito_errado: "dígito verificador",       # Luhn
            valido[:-1]: "19 ou 20 dígitos",                          # 18 dígitos
            valido + "12": "19 ou 20 dígitos",                        # 21 dígitos
        }
        for entrada, trecho in casos.items():
            with self.subTest(entrada=entrada):
                with self.assertRaises(ValidationError) as ctx:
                    tim.normalizar_iccid(entrada)
                self.assertIn(trecho, ctx.exception.messages[0])

    def test_iccid_recusa_prefixo_diferente_de_89(self):
        corpo = "905510000000000001"
        iccid = corpo + tim.calcular_digito_luhn(corpo)           # Luhn certo, prefixo errado
        with self.assertRaises(ValidationError) as ctx:
            tim.normalizar_iccid(iccid)
        self.assertIn("começar com 89", ctx.exception.messages[0])

    def test_imsi_exige_15_digitos(self):
        self.assertEqual(tim.normalizar_imsi(" 724 100000000001 "), "724100000000001")
        for entrada in ("72410000000000", "7241000000000012", "72410000000000A", ""):
            with self.subTest(entrada=entrada):
                with self.assertRaises(ValidationError):
                    tim.normalizar_imsi(entrada)


# ═════════════════════════════════════════════════════════════
# Cadastro
# ═════════════════════════════════════════════════════════════

class CadastroTests(M2MBase):
    def test_criar_normaliza_grava_evento_e_nasce_vencido(self):
        bruto = iccid_valido(1)
        chip = m2m.criar_chip({"nome": "  Pivô 3 ", "iccid": f"{bruto[:5]} {bruto[5:]}"}, self.usuario)
        self.assertEqual(chip.nome, "Pivô 3")
        self.assertEqual(chip.iccid, bruto)
        self.assertEqual(chip.identificador_consulta, "iccid")
        self.assertTrue(chip.monitorado)
        self.assertIsNone(chip.proxima_consulta_em)
        self.assertEqual(chip.sessao_estado, "nao_consultado")
        evento = M2MEvento.objects.get(chip=chip, tipo="cadastro")
        self.assertEqual(evento.usuario, self.usuario)
        self.assertEqual(evento.dados["operadora"], "TIM")
        # todo chip nasce com a movimentação de ENTRADA (e o evento que a espelha), disponível
        self.assertEqual(list(M2MEvento.objects.filter(chip=chip).order_by("pk").values_list("tipo", flat=True)), ["cadastro", "entrada"])
        self.assertEqual((chip.operadora.nome, chip.situacao_uso), ("TIM", "disponivel"))

    def test_identificador_padrao_sai_do_que_foi_preenchido(self):
        so_imsi = m2m.criar_chip({"nome": "Só IMSI", "imsi": imsi_valido(1)}, self.usuario)
        self.assertEqual(so_imsi.identificador_consulta, "imsi")

    def test_identificador_de_consulta_precisa_estar_preenchido(self):
        with self.assertRaises(ValidationError) as ctx:
            m2m.criar_chip({"nome": "X", "imsi": imsi_valido(1), "identificador_consulta": "iccid"}, self.usuario)
        self.assertIn("iccid", ctx.exception.message_dict)
        with self.assertRaises(ValidationError):
            m2m.criar_chip({"nome": "Y"}, self.usuario)

    def test_validacao_de_iccid_e_imsi_no_cadastro(self):
        with self.assertRaises(ValidationError) as ctx:
            m2m.criar_chip({"nome": "Z", "iccid": "123", "imsi": "456"}, self.usuario)
        self.assertEqual(set(ctx.exception.message_dict), {"iccid", "imsi"})

    def test_duplicidade_de_nome_iccid_e_imsi(self):
        self.chip(1, imsi=imsi_valido(1))
        casos = {
            "nome": {"nome": "chip 1", "iccid": iccid_valido(2)},                  # nome sem diferenciar caixa
            "iccid": {"nome": "Outro", "iccid": iccid_valido(1)},
            "imsi": {"nome": "Outro", "iccid": iccid_valido(2), "imsi": imsi_valido(1)},
        }
        for campo, dados in casos.items():
            with self.subTest(campo=campo):
                with self.assertRaises(ValidationError) as ctx:
                    m2m.criar_chip(dados, self.usuario)
                self.assertIn(campo, ctx.exception.message_dict)
        self.assertEqual(M2MChip.objects.count(), 1)

    def test_varios_chips_sem_iccid_nao_colidem(self):
        """A unicidade é condicional: valor vazio não conta como duplicado."""
        m2m.criar_chip({"nome": "A", "imsi": imsi_valido(1)}, self.usuario)
        m2m.criar_chip({"nome": "B", "imsi": imsi_valido(2)}, self.usuario)
        self.assertEqual(M2MChip.objects.filter(iccid="").count(), 2)

    def test_limite_mensal_precisa_ser_positivo(self):
        for ruim in ("abc", "-5", "0"):
            with self.subTest(ruim=ruim), self.assertRaises(ValidationError):
                self.chip(9, nome=f"L{ruim}", limite_mensal_bytes=ruim)
        chip = self.chip(2, limite_mensal_bytes="1000000000")
        self.assertEqual(chip.limite_mensal_bytes, 1_000_000_000)

    def test_editar_grava_de_para_e_nada_quando_nada_mudou(self):
        chip = self.chip(1, setor="Irrigação")
        n = M2MEvento.objects.count()
        m2m.editar_chip(chip, {"nome": chip.nome, "setor": "Irrigação"}, self.usuario)
        self.assertEqual(M2MEvento.objects.count(), n)                    # nada mudou, nada gravado

        m2m.editar_chip(chip, {"setor": "Pivôs", "descricao": "Pivô 3"}, self.usuario)
        evento = M2MEvento.objects.filter(tipo="edicao").get()
        self.assertEqual(evento.dados["alteracoes"]["setor"], {"de": "Irrigação", "para": "Pivôs"})
        self.assertIn("descricao", evento.dados["alteracoes"])
        chip.refresh_from_db()
        self.assertEqual(chip.setor, "Pivôs")

    def test_editar_nao_atropela_a_situacao_do_monitoramento(self):
        chip = self.chip(1)
        self.atualizar(chip, roteador())
        m2m.editar_chip(chip, {"descricao": "novo texto"}, self.usuario)
        chip.refresh_from_db()
        self.assertEqual(chip.sessao_estado, "nao_interpretado")
        self.assertIsNotNone(chip.sessao_consulta_id)

    def test_trocar_o_identificador_marca_o_chip_para_a_proxima_rodada(self):
        chip = self.chip(1)
        self.atualizar(chip, roteador())
        self.assertIsNotNone(chip.proxima_consulta_em)
        m2m.editar_chip(chip, {"iccid": iccid_valido(8)}, self.usuario)
        chip.refresh_from_db()
        self.assertIsNone(chip.proxima_consulta_em)

    def test_editar_nao_aceita_duplicado_de_outro_chip(self):
        self.chip(1)
        outro = self.chip(2)
        with self.assertRaises(ValidationError):
            m2m.editar_chip(outro, {"iccid": iccid_valido(1)}, self.usuario)

    def test_desativar_exige_motivo_e_preserva_o_historico(self):
        chip = self.chip(1)
        self.atualizar(chip, roteador())
        consultas = chip.consultas.count()
        with self.assertRaises(ValidationError):
            m2m.desativar(chip, self.usuario, "   ")
        m2m.desativar(chip, self.usuario, "Chip devolvido")
        chip.refresh_from_db()
        self.assertFalse(chip.monitorado)
        self.assertEqual(chip.desativado_motivo, "Chip devolvido")
        self.assertEqual(chip.desativado_por, self.usuario)
        self.assertEqual(chip.consultas.count(), consultas)               # nada foi apagado
        self.assertTrue(M2MEvento.objects.filter(chip=chip, tipo="desativado").exists())
        with self.assertRaises(ValidationError):
            m2m.desativar(chip, self.usuario, "de novo")

    def test_reativar_zera_falhas_e_marca_para_a_proxima_rodada(self):
        chip = self.chip(1)
        self.falhar(chip, 2)
        m2m.desativar(chip, self.usuario, "teste")
        m2m.reativar(chip, self.usuario, "voltou")
        chip.refresh_from_db()
        self.assertTrue(chip.monitorado)
        self.assertEqual(chip.falhas_consecutivas, 0)
        self.assertIsNone(chip.proxima_consulta_em)
        self.assertIsNone(chip.desativado_em)
        self.assertEqual(chip.consultas.filter(resultado="indisponivel").count(), 4)   # histórico intacto
        self.assertTrue(M2MEvento.objects.filter(chip=chip, tipo="reativado").exists())

    def test_a_consulta_e_imutavel_e_o_chip_com_historico_nao_se_apaga(self):
        chip = self.chip(1)
        self.atualizar(chip, roteador())
        consulta = chip.consultas.first()
        consulta.resultado = "timeout"
        with self.assertRaises(ValidationError):
            consulta.save()
        with self.assertRaises(ValidationError):
            consulta.save(update_fields=["resultado"])
        with self.assertRaises(ValidationError):
            consulta.delete()
        with self.assertRaises(ProtectedError):
            chip.delete()
        self.assertTrue(M2MChip.objects.filter(pk=chip.pk).exists())

    def test_singleton_da_integracao(self):
        a, b = M2MIntegracao.get(), M2MIntegracao.get()
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(M2MIntegracao.objects.count(), 1)
        self.assertEqual((a.intervalo_minutos, a.limite_req_minuto, a.falhas_para_alerta, a.pct_consumo_alerta), (60, 30, 3, 80))


# ═════════════════════════════════════════════════════════════
# Transporte
# ═════════════════════════════════════════════════════════════

class TransporteTests(M2MBase):
    def test_url_headers_e_opcoes_de_seguranca(self):
        chip = self.chip(1)
        cliente, sessao = self.cliente([Resp(200, json={"a": 1}), Resp(200, json={"b": 2})])
        cliente.consultar(chip, "sessao", "manual")
        cliente.consultar(chip, "consumo", "manual")
        self.assertEqual(sessao.urls, [f"{BASE}/{chip.iccid}/session", f"{BASE}/{chip.iccid}/usage"])
        kw = sessao.chamadas[0][1]
        self.assertEqual(kw["headers"]["Authorization"], f"Bearer {TOKEN}")
        self.assertEqual(kw["headers"]["Accept"], "application/json")
        self.assertIs(kw["allow_redirects"], False)
        self.assertIs(kw["verify"], True)
        self.assertEqual(kw["timeout"], (5, 20))

    def test_usa_o_identificador_escolhido_no_cadastro(self):
        chip = self.chip(1, imsi=imsi_valido(1), identificador_consulta="imsi")
        cliente, sessao = self.cliente([Resp(200, json={})])
        cliente.consultar(chip, "sessao", "manual")
        self.assertEqual(sessao.urls, [f"{BASE}/{chip.imsi}/session"])

    def test_identificador_e_codificado_e_o_host_nao_muda(self):
        url = tim.ClienteTIM.url_da_consulta("89/../evil@x.com", "sessao")
        self.assertEqual(url, f"{BASE}/89%2F..%2Fevil%40x.com/session")
        self.assertEqual(requests.utils.urlparse(url).hostname, "tim-m2m-platform.com")
        with override_settings(TIM_M2M_BASE_URL="https://api.exemplo.com.br/v2/"):
            self.assertEqual(tim.ClienteTIM.url_da_consulta("123", "consumo"), "https://api.exemplo.com.br/v2/123/usage")

    def test_so_aceita_https_com_host(self):
        for ruim in ("http://tim-m2m-platform.com", "", "tim-m2m-platform.com", "https://u:s@host.com",
                     "https://host.com?x=1", "ftp://host.com"):
            with self.subTest(base=ruim), override_settings(TIM_M2M_BASE_URL=ruim):
                with self.assertRaises(tim.ConfiguracaoInvalida):
                    tim.ClienteTIM.url_da_consulta("123", "sessao")

    def test_identificador_invalido_gravado_no_banco_nao_chega_a_rede(self):
        chip = self.chip(1)
        M2MChip.objects.filter(pk=chip.pk).update(iccid="89/../x")
        chip.refresh_from_db()
        cliente, sessao = self.cliente([])
        r = cliente.consultar(chip, "sessao", "manual")
        self.assertEqual(r.resultado, "resposta_invalida")
        self.assertEqual(sessao.chamadas, [])
        self.assertIn("não foi enviada", r.mensagem_erro)

    def test_sem_token_levanta_semtoken(self):
        chip = self.chip(1)
        with override_settings(TIM_M2M_TOKEN=""):
            cliente, sessao = self.cliente([])
            with self.assertRaises(tim.SemToken):
                cliente.consultar(chip, "sessao")
        self.assertEqual(sessao.chamadas, [])

    def test_classificacao_das_respostas(self):
        chip = self.chip(1)
        casos = [
            (Resp(200, json={"x": 1}), "sucesso", 200),
            (Resp(204), "sucesso_sem_corpo", 204),
            (Resp(200, content=b""), "sucesso_sem_corpo", 200),
            (Resp(200, content=b"<html>oi</html>"), "resposta_invalida", 200),
            (Resp(401), "falha_autenticacao", 401),
            (Resp(403), "falha_autenticacao", 403),
            (Resp(404), "nao_encontrado", 404),
            (Resp(429), "limite_excedido", 429),
            (Resp(400, json={"erro": "x"}), "resposta_invalida", 400),
            (Resp(302, headers={"Location": "https://outro.exemplo"}), "resposta_invalida", 302),
        ]
        for resp, esperado, status in casos:
            with self.subTest(status=status, esperado=esperado):
                cliente, _ = self.cliente([resp])
                r = cliente.consultar(chip, "sessao", "agendada")
                self.assertEqual((r.resultado, r.http_status), (esperado, status))
                self.assertEqual(r.tentativas, 1)

    def test_404_nunca_vira_sem_sessao_e_diz_isso(self):
        cliente, _ = self.cliente([Resp(404)])
        r = cliente.consultar(self.chip(1), "sessao")
        self.assertEqual(r.resultado, "nao_encontrado")
        self.assertIn("não significa que o chip esteja sem sessão", r.mensagem_erro)

    def test_redirect_nao_e_seguido_para_nao_levar_o_token_a_outro_host(self):
        cliente, sessao = self.cliente([Resp(301, headers={"Location": "https://evil.example/"})])
        r = cliente.consultar(self.chip(1), "sessao")
        self.assertEqual(r.resultado, "resposta_invalida")
        self.assertEqual(len(sessao.chamadas), 1)
        self.assertNotIn("evil.example", r.mensagem_erro)

    def test_429_le_o_retry_after_e_tem_padrao_de_15_minutos(self):
        chip = self.chip(1)
        r = self.cliente([Resp(429, headers={"Retry-After": "120"})])[0].consultar(chip, "sessao")
        self.assertEqual(r.retry_after_s, 120)
        r = self.cliente([Resp(429)])[0].consultar(chip, "sessao")
        self.assertEqual(r.retry_after_s, 900)
        r = self.cliente([Resp(429, headers={"retry-after": "abc"})])[0].consultar(chip, "sessao")
        self.assertEqual(r.retry_after_s, 900)

    def test_retentativas_da_agendada_sao_duas_com_espera_de_1s_e_3s(self):
        chip = self.chip(1)
        cliente, sessao = self.cliente([Resp(500), Resp(502), Resp(200, json={"ok": 1})])
        r = cliente.consultar(chip, "sessao", "agendada")
        self.assertEqual((r.resultado, r.tentativas), ("sucesso", 3))
        self.assertEqual([c.args for c in self.sleep.call_args_list], [(1.0,), (3.0,)])
        self.assertEqual(len(sessao.chamadas), 3)

        self.sleep.reset_mock()
        cliente, sessao = self.cliente([Resp(500), Resp(500), Resp(503)])
        r = cliente.consultar(chip, "sessao", "agendada")
        self.assertEqual((r.resultado, r.tentativas), ("indisponivel", 3))
        self.assertEqual(len(sessao.chamadas), 3)                                # nunca mais que 2 retentativas

    def test_retentativa_da_manual_e_uma_so(self):
        chip = self.chip(1)
        for origem in ("manual", "teste"):
            with self.subTest(origem=origem):
                self.sleep.reset_mock()
                cliente, sessao = self.cliente([Resp(500), Resp(500), Resp(200, json={})])
                r = cliente.consultar(chip, "sessao", origem)
                self.assertEqual((r.resultado, r.tentativas), ("indisponivel", 2))
                self.assertEqual(len(sessao.chamadas), 2)
                self.assertEqual([c.args for c in self.sleep.call_args_list], [(1.0,)])

    def test_timeout_e_erro_de_rede_tambem_sao_retentados(self):
        chip = self.chip(1)
        cliente, _ = self.cliente([requests.Timeout("lento"), Resp(200, json={"a": 1})])
        r = cliente.consultar(chip, "sessao", "agendada")
        self.assertEqual((r.resultado, r.tentativas), ("sucesso", 2))

        cliente, _ = self.cliente([requests.Timeout()] * 3)
        r = cliente.consultar(chip, "sessao", "agendada")
        self.assertEqual((r.resultado, r.tentativas), ("timeout", 3))

        cliente, _ = self.cliente([requests.ConnectionError("conexão recusada")] * 3)
        r = cliente.consultar(chip, "sessao", "agendada")
        self.assertEqual((r.resultado, r.tentativas), ("erro_rede", 3))

    def test_dns_que_nao_resolve_diz_host_nao_encontrado(self):
        erro = requests.ConnectionError("HTTPSConnectionPool: NameResolutionError: Failed to resolve 'tim-m2m-platform.com'")
        r = self.cliente([erro] * 3)[0].consultar(self.chip(1), "sessao", "agendada")
        self.assertEqual(r.resultado, "erro_rede")
        self.assertIn("host não encontrado", r.mensagem_erro)
        self.assertIn("tim-m2m-platform.com", r.mensagem_erro)

    def test_401_404_e_429_nunca_sao_retentados(self):
        chip = self.chip(1)
        for status in (401, 404, 429):
            with self.subTest(status=status):
                self.sleep.reset_mock()
                cliente, sessao = self.cliente([Resp(status), Resp(200, json={}), Resp(200, json={})])
                r = cliente.consultar(chip, "sessao", "agendada")
                self.assertEqual(r.tentativas, 1)
                self.assertEqual(len(sessao.chamadas), 1)
                self.sleep.assert_not_called()

    def test_resposta_grande_e_truncada_com_marcacao(self):
        grande = {"itens": [{"id": i, "texto": "x" * 50} for i in range(3000)]}
        r = self.cliente([Resp(200, json=grande)])[0].consultar(self.chip(1), "sessao")
        self.assertEqual(r.resultado, "sucesso")
        self.assertTrue(r.payload["_truncado"])
        self.assertGreater(r.payload["_tamanho_original_bytes"], tim.PAYLOAD_MAX_BYTES)
        self.assertLessEqual(len(json.dumps(r.payload).encode()), tim.PAYLOAD_MAX_BYTES)

    def test_resposta_pequena_fica_intacta(self):
        r = self.cliente([Resp(200, json=SESSAO_UP)])[0].consultar(self.chip(1), "sessao")
        self.assertEqual(r.payload, SESSAO_UP)


# ═════════════════════════════════════════════════════════════
# Token
# ═════════════════════════════════════════════════════════════

class TokenTests(M2MBase):
    def test_ida_e_volta_cifrada_e_nada_em_claro_no_banco(self):
        with override_settings(TIM_M2M_TOKEN=""):
            self.assertEqual(tim.token(), "")
            tim.salvar_token("  token-do-banco-0123456789  ", self.usuario)
            cfg = M2MIntegracao.get()
            self.assertNotIn("token-do-banco", cfg.token_cifrado)
            self.assertTrue(cfg.token_cifrado)
            self.assertEqual(cfg.token_sufixo, "6789")
            self.assertEqual(cfg.token_atualizado_por, self.usuario)
            self.assertEqual(tim.token(), "token-do-banco-0123456789")
            self.assertEqual(
                {k: tim.estado_token()[k] for k in ("origem", "sufixo", "legivel")},
                {"origem": "banco", "sufixo": "6789", "legivel": True},
            )
        evento = M2MEvento.objects.get(tipo="token_alterado")
        self.assertNotIn("token-do-banco", json.dumps(evento.dados))
        self.assertEqual(evento.dados["sufixo"], "6789")

    def test_o_token_do_env_vence_o_do_banco(self):
        tim.salvar_token("token-do-banco-0123456789", self.usuario)
        self.assertEqual(tim.token(), TOKEN)
        estado = tim.estado_token()
        self.assertEqual((estado["origem"], estado["sufixo"]), ("env", TOKEN[-4:]))
        self.assertTrue(estado["token_no_banco"])
        self.assertTrue(M2MEvento.objects.get(tipo="token_alterado").dados["token_do_env_vence"])

    def test_sem_a_chave_recusa_gravar_e_explica_como_gerar(self):
        with override_settings(TIM_M2M_CHAVE_CIFRA=""):
            with self.assertRaises(ValidationError) as ctx:
                tim.salvar_token("qualquer-token-123456", self.usuario)
        self.assertIn("TIM_M2M_CHAVE_CIFRA", ctx.exception.messages[0])
        self.assertIn("Fernet.generate_key()", ctx.exception.messages[0])
        self.assertEqual(M2MIntegracao.get().token_cifrado, "")             # nada foi gravado em claro
        self.assertFalse(M2MEvento.objects.filter(tipo="token_alterado").exists())

    def test_chave_invalida_tambem_recusa(self):
        with override_settings(TIM_M2M_CHAVE_CIFRA="isto-nao-e-uma-chave"):
            with self.assertRaises(ValidationError) as ctx:
                tim.salvar_token("qualquer-token-123456", self.usuario)
        self.assertIn("não é uma chave Fernet válida", ctx.exception.messages[0])

    def test_token_com_espaco_ou_bearer_ou_vazio_e_recusado(self):
        for ruim in ("", "   ", "Bearer abc.def.ghi", "abc def", "abc\ndef"):
            with self.subTest(ruim=ruim), self.assertRaises(ValidationError):
                tim.salvar_token(ruim, self.usuario)

    def test_chave_trocada_deixa_o_token_ilegivel_e_o_estado_diz_isso(self):
        with override_settings(TIM_M2M_TOKEN=""):
            tim.salvar_token("token-do-banco-0123456789", self.usuario)
            with override_settings(TIM_M2M_CHAVE_CIFRA=Fernet.generate_key().decode()):
                self.assertEqual(tim.token(), "")
                self.assertFalse(tim.estado_token()["legivel"])
                self.assertFalse(tim.configurada())
                self.assertIn("chave TIM_M2M_CHAVE_CIFRA", tim.motivo_nao_configurada())
                self.assertEqual(m2m.estado_integracao()["estado"], "token_ilegivel")

    def test_salvar_token_novo_libera_a_credencial_recusada(self):
        M2MIntegracao.objects.filter(pk=1).update(falha_autenticacao_em=timezone.now())
        tim.salvar_token("token-novo-0123456789", self.usuario)
        self.assertIsNone(M2MIntegracao.get().falha_autenticacao_em)

    def test_remover_token_apaga_so_o_do_banco(self):
        tim.salvar_token("token-do-banco-0123456789", self.usuario)
        tim.remover_token(self.usuario)
        cfg = M2MIntegracao.get()
        self.assertEqual((cfg.token_cifrado, cfg.token_sufixo), ("", ""))
        self.assertEqual(tim.token(), TOKEN)                                # o do .env segue valendo
        self.assertEqual(M2MEvento.objects.filter(tipo="token_alterado").count(), 2)

    def test_token_curto_nao_mostra_sufixo(self):
        tim.salvar_token("curto123", self.usuario)
        self.assertEqual(M2MIntegracao.get().token_sufixo, "")

    def test_configurada_exige_token_e_url(self):
        self.assertTrue(tim.configurada())
        with override_settings(TIM_M2M_TOKEN=""):
            self.assertFalse(tim.configurada())
            self.assertIn("Nenhum token", tim.motivo_nao_configurada())
        with override_settings(TIM_M2M_BASE_URL="http://inseguro.com"):
            self.assertFalse(tim.configurada())
            self.assertIn("https", tim.motivo_nao_configurada())


class SegredoTests(M2MBase):
    """O token não pode aparecer em lugar nenhum onde um humano ou um arquivo lê."""

    def test_o_token_nao_vai_para_log_mensagem_evento_payload_nem_configuracao(self):
        chip = self.chip(1)
        respostas = {
            "/session": Resp(400, json={"erro": f"token inválido: {TOKEN}", "dica": f"Authorization: Bearer {TOKEN}"}),
            "/usage": Resp(401, json={"detail": f"credencial {TOKEN} recusada"}),
        }
        cliente, _ = self.cliente(lambda url: respostas["/session" if url.endswith("/session") else "/usage"])
        m2m.atualizar(chip, ("sessao", "consumo"), "agendada", self.usuario, cliente=cliente)   # 401 interrompe
        with self.assertRaises(m2m.AtualizacaoRecusada):                                         # e a recusa também é caso
            m2m.atualizar(chip, ("sessao",), "manual", self.usuario, cliente=cliente, cfg=M2MIntegracao(bloqueado_ate=timezone.now() + timedelta(hours=1)))
        tim.salvar_token("outro-token-0123456789", self.usuario)

        tudo = list(self.logs)
        for c in M2MConsulta.objects.all():
            tudo += [c.mensagem_erro, json.dumps(c.payload), str(c.http_status)]
        for e in M2MEvento.objects.all():
            tudo.append(json.dumps(e.dados))
        cfg = M2MIntegracao.get()
        tudo += [cfg.ultimo_erro, json.dumps(cfg.ultimo_teste), json.dumps(cfg.mapeamento)]
        self.assertTrue(self.logs, "o teste precisa de pelo menos uma linha de log para valer")
        for texto in tudo:
            self.assertNotIn(TOKEN, texto)
        self.assertNotIn("outro-token-0123456789", " ".join(tudo))
        self.assertTrue(any("[token]" in json.dumps(c.payload) for c in M2MConsulta.objects.all()))

    def test_sanear_remove_o_token_e_qualquer_bearer(self):
        self.assertNotIn(TOKEN, tim.sanear(f"falhou com {TOKEN} aqui", TOKEN))
        self.assertEqual(tim.sanear("Authorization: Bearer abc.def-123"), "Authorization: Bearer [oculto]")
        self.assertEqual(tim.sanear(None), "")

    def test_excecao_de_rede_com_o_token_no_texto_nao_vaza(self):
        chip = self.chip(1)
        erro = requests.RequestException(f"falha com Bearer {TOKEN}")
        cliente, _ = self.cliente([erro] * 3)
        r = cliente.consultar(chip, "sessao", "agendada")
        self.assertNotIn(TOKEN, r.mensagem_erro)
        self.assertNotIn(TOKEN, " ".join(self.logs))

    def test_admin_nunca_mostra_o_token_cifrado(self):
        tim.salvar_token("token-do-banco-0123456789", self.usuario)
        cifrado = M2MIntegracao.get().token_cifrado
        admin = User.objects.create_superuser("admin_m2m", "a@a.com", "senha-forte-123")
        self.client.force_login(admin)
        for nome in ("changelist",):
            resp = self.client.get(reverse(f"admin:ProjetoEstoque_m2mintegracao_{nome}"))
            self.assertEqual(resp.status_code, 200)
            self.assertNotIn(cifrado, resp.content.decode())
        resp = self.client.get(reverse("admin:ProjetoEstoque_m2mintegracao_change", args=[1]))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertNotIn(cifrado, html)
        self.assertNotIn("token_cifrado", html)
        self.assertIn("6789", html)                                         # só os 4 últimos


# ═════════════════════════════════════════════════════════════
# Interpretação (funções puras)
# ═════════════════════════════════════════════════════════════

class InterpretacaoTests(SimpleTestCase):
    def test_valor_em_e_tolerante(self):
        p = {"a": {"b": [{"c": 7}, {"c": 8}]}, "lista": [1, 2], "nulo": None, "x y": 3}
        self.assertEqual(interp.valor_em(p, "a.b[0].c"), 7)
        self.assertEqual(interp.valor_em(p, "a.b[1].c"), 8)
        self.assertEqual(interp.valor_em(p, "lista[1]"), 2)
        self.assertEqual(interp.valor_em(p, "x y"), 3)
        self.assertEqual(interp.valor_em([{"k": 1}], "[0].k"), 1)
        for ruim in ("a.b[5].c", "a.z", "lista.x", "a[0]", "nulo", "a..b", "a.b[", "", "  ", None, 5):
            with self.subTest(caminho=ruim):
                self.assertIsNone(interp.valor_em(p, ruim))
        self.assertIsNone(interp.valor_em(None, "a"))
        self.assertIsNone(interp.valor_em("texto", "a"))

    def test_caminhos_achatados_para_a_tela_de_mapeamento(self):
        p = {"sessao": {"estado": "UP", "ips": ["1.1.1.1", "2.2.2.2"]}, "uso": 12, "ok": True, "vazio": None, "a.b": 1}
        achados = interp.caminhos(p)
        self.assertEqual(achados, ["sessao.estado", "sessao.ips[0]", "sessao.ips[1]", "uso", "ok", "vazio"])
        com_exemplo = {c["caminho"]: c for c in interp.caminhos_com_exemplo(p)}
        self.assertEqual(com_exemplo["uso"]["tipo"], "numero")
        self.assertEqual(com_exemplo["ok"]["tipo"], "booleano")
        self.assertEqual(com_exemplo["sessao.estado"]["exemplo"], "UP")
        for caminho in achados:                                              # todo caminho listado se resolve
            if caminho != "vazio":
                self.assertIsNotNone(interp.valor_em(p, caminho))

    def test_caminhos_limitam_listas_e_total(self):
        self.assertEqual(len(interp.caminhos({"l": list(range(100))})), 5)
        self.assertEqual(len(interp.caminhos({f"k{i}": i for i in range(1000)})), 300)
        self.assertEqual(interp.caminhos(None), [])

    def test_sessao_sem_mapeamento_nao_interpreta_nada(self):
        r = interp.interpretar_sessao(SESSAO_UP, {})
        self.assertEqual((r.estado, r.estado_bruto), ("nao_interpretado", ""))
        self.assertIn("Sem mapeamento", r.motivo)

    def test_sessao_com_mapeamento(self):
        mapa = interp.validar_mapeamento(MAPA)
        r = interp.interpretar_sessao(SESSAO_UP, mapa)
        self.assertEqual((r.estado, r.estado_bruto), ("ativa", "UP"))
        self.assertEqual(r.campos_exibir, [{"caminho": "sessao.ip", "valor": "10.0.0.5"}])
        self.assertEqual(interp.interpretar_sessao({"sessao": {"estado": "down "}}, mapa).estado, "sem_sessao")
        self.assertEqual(interp.interpretar_sessao({"sessao": {"estado": "online"}}, mapa).estado, "ativa")

    def test_valor_fora_das_listas_ou_campo_ausente_e_nao_interpretado_com_o_bruto_a_vista(self):
        mapa = interp.validar_mapeamento(MAPA)
        r = interp.interpretar_sessao({"sessao": {"estado": "SUSPENSA"}}, mapa)
        self.assertEqual((r.estado, r.estado_bruto), ("nao_interpretado", "SUSPENSA"))
        self.assertIn("SUSPENSA", r.motivo)
        r = interp.interpretar_sessao({"outra": 1}, mapa)
        self.assertEqual((r.estado, r.estado_bruto), ("nao_interpretado", ""))
        self.assertEqual(interp.interpretar_sessao(None, mapa).estado, "nao_interpretado")

    def test_consumo_converte_para_bytes_conforme_a_base(self):
        mapa = interp.validar_mapeamento(MAPA)
        r = interp.interpretar_consumo(USO_MB, mapa)
        self.assertEqual((r.valor_original, r.unidade_original, r.volume_bytes), ("250.5", "MB", 250_500_000))
        self.assertEqual(r.periodo_inicio.date().isoformat(), "2026-11-01")
        self.assertEqual(r.periodo_fim.date().isoformat(), "2026-11-30")
        binario = interp.validar_mapeamento({**MAPA, "consumo": {**MAPA["consumo"], "base": 1024}})
        self.assertEqual(interp.interpretar_consumo({"uso": {"total": 2}}, binario).volume_bytes, 2 * 1024 ** 2)

    def test_consumo_com_unidade_vinda_do_proprio_payload(self):
        mapa = interp.validar_mapeamento({"consumo": {"campo_volume": "v", "campo_unidade": "u"}})
        self.assertEqual(interp.interpretar_consumo({"v": "3", "u": "GB"}, mapa).volume_bytes, 3_000_000_000)
        self.assertEqual(interp.interpretar_consumo({"v": 512, "u": "KiB"}, mapa).volume_bytes, 512 * 1024)
        self.assertEqual(interp.interpretar_consumo({"v": 7, "u": "bytes"}, mapa).volume_bytes, 7)

    def test_consumo_nao_adivinha_valor_nem_unidade(self):
        mapa = interp.validar_mapeamento({"consumo": {"campo_volume": "v", "campo_unidade": "u"}})
        casos = [
            ({"v": 5, "u": "Mb"}, "megabits"),               # ambíguo (bits): não converte
            ({"v": 5, "u": "furlongs"}, "desconhecida"),
            ({"v": "12,5", "u": "MB"}, "ambíguo"),            # vírgula decimal: não se adivinha o formato
            ({"v": "1.234,5", "u": "MB"}, "milhar"),
            ({"v": -3, "u": "MB"}, "negativo"),
            ({"v": True, "u": "MB"}, "booleano"),
            ({"v": "12 MB", "u": "MB"}, "unidade colada"),
            ({"v": 5}, "sem unidade"),
        ]
        for payload, rotulo in casos:
            with self.subTest(rotulo):
                r = interp.interpretar_consumo(payload, mapa)
                self.assertIsNone(r.volume_bytes)
                self.assertTrue(r.valor_original)            # o original continua guardado
        self.assertEqual(interp.interpretar_consumo({"v": 5, "u": "Mb"}, mapa).unidade_original, "Mb")

    def test_consumo_sem_mapeamento_ou_sem_corpo(self):
        self.assertIsNone(interp.interpretar_consumo(USO_MB, {}).volume_bytes)
        self.assertEqual(interp.interpretar_consumo(USO_MB, {}).valor_original, "")
        self.assertIsNone(interp.interpretar_consumo(None, interp.validar_mapeamento(MAPA)).volume_bytes)

    def test_campos_normalizados_ficam_vazios_quando_a_consulta_nao_e_valida(self):
        vazio = interp.campos_normalizados("sessao", SESSAO_UP, interp.validar_mapeamento(MAPA), valida=False)
        self.assertEqual(set(vazio) , set(M2MConsulta.CAMPOS_NORMALIZADOS))
        self.assertFalse(any(v for v in vazio.values()))

    def test_validar_mapeamento(self):
        self.assertEqual(interp.validar_mapeamento(None), {})
        self.assertEqual(interp.validar_mapeamento({}), {})
        normal = interp.validar_mapeamento({"sessao": {"campo_estado": " a.b ", "valores_ativa": "UP, ON\nUP"}, "consumo": {}})
        self.assertEqual(normal, {"sessao": {"campo_estado": "a.b", "valores_ativa": ["UP", "ON"]}})
        erros = {
            "duas listas": ({"sessao": {"campo_estado": "e", "valores_ativa": ["X"], "valores_sem_sessao": ["x"]}}, "duas listas"),
            "unidade dupla": ({"consumo": {"campo_volume": "v", "campo_unidade": "u", "unidade_fixa": "MB"}}, "OU"),
            "unidade desconhecida": ({"consumo": {"campo_volume": "v", "unidade_fixa": "zzz"}}, "não reconhecida"),
            "bits": ({"consumo": {"campo_volume": "v", "unidade_fixa": "Mb"}}, "bits"),
            "base": ({"consumo": {"campo_volume": "v", "unidade_fixa": "MB", "base": 1500}}, "1000 ou 1024"),
            "caminho": ({"sessao": {"campo_estado": "a..b"}}, "caminho inválido"),
            "valores sem campo": ({"sessao": {"valores_ativa": ["UP"]}}, "campo do estado"),
            "unidade sem volume": ({"consumo": {"unidade_fixa": "MB"}}, "campo do volume"),
            "formato": ("texto", "objeto"),
        }
        for rotulo, (mapa, trecho) in erros.items():
            with self.subTest(rotulo), self.assertRaises(ValidationError) as ctx:
                interp.validar_mapeamento(mapa)
            self.assertIn(trecho, " ".join(ctx.exception.messages))

    def test_formatar_bytes(self):
        casos = [(None, "—"), (0, "0 B"), (999, "999 B"), (1500, "1,5 KB"), (250_500_000, "250,5 MB"),
                 (2_000_000_000, "2 GB"), (1_234_567_890_123, "1,23 TB")]
        for valor, esperado in casos:
            with self.subTest(valor=valor):
                self.assertEqual(interp.formatar_bytes(valor), esperado)
        self.assertEqual(interp.formatar_bytes(1536, base=1024), "1,5 KB")

    def test_identidade_do_payload(self):
        mapa = interp.validar_mapeamento(MAPA)
        self.assertEqual(interp.identidade_do_payload({"sim": {"iccid": "8955 1000-1"}}, mapa), {"iccid": "895510001", "imsi": ""})
        self.assertEqual(interp.identidade_do_payload({"sim": {}}, mapa), {"iccid": "", "imsi": ""})
        self.assertEqual(interp.identidade_do_payload({"sim": {"iccid": "1"}}, {}), {"iccid": "", "imsi": ""})

    def test_previa_do_que_seria_interpretado(self):
        p = interp.previa({**SESSAO_UP, **USO_MB}, MAPA)
        self.assertEqual(p["erros"], [])
        self.assertEqual(p["sessao"]["estado"], "ativa")
        self.assertEqual(p["consumo"]["volume_bytes"], 250_500_000)
        self.assertEqual(p["consumo"]["volume_formatado"], "250,5 MB")
        self.assertEqual(json.loads(json.dumps(p)), p)                       # serializável (vai para JSON)
        ruim = interp.previa(SESSAO_UP, {"consumo": {"campo_volume": "v", "base": 7}})
        self.assertTrue(ruim["erros"])


# ═════════════════════════════════════════════════════════════
# Atualização: situação atual × histórico
# ═════════════════════════════════════════════════════════════

class AtualizacaoTests(M2MBase):
    def test_sucesso_sem_mapeamento_guarda_cru_e_nao_interpreta(self):
        chip = self.chip(1)
        r, sessao = self.atualizar(chip, roteador(), origem="manual")
        self.assertTrue(r.ok)
        self.assertEqual(len(sessao.chamadas), 2)
        sess = chip.sessao_consulta
        self.assertEqual(sess.payload, SESSAO_UP)                              # cru, intocado
        self.assertEqual(sess.resultado, "sucesso")
        self.assertEqual(sess.estado_interpretado, "nao_interpretado")
        self.assertEqual(chip.sessao_estado, "nao_interpretado")               # nunca "ativa" por palpite
        self.assertEqual(sess.estado_bruto, "")
        self.assertIsNone(chip.consumo_bytes)                                   # nem volume por palpite
        self.assertEqual(chip.consumo_consulta.payload, USO_MB)
        self.assertEqual(chip.falhas_consecutivas, 0)
        self.assertIsNotNone(chip.ultima_valida_em)
        self.assertAlmostEqual(
            (chip.proxima_consulta_em - timezone.now()).total_seconds(), 3600, delta=30,
        )

    def test_com_mapeamento_interpreta_e_registra_a_mudanca_de_estado(self):
        self.mapear()
        chip = self.chip(1)
        self.atualizar(chip, roteador())
        self.assertEqual(chip.sessao_estado, "ativa")
        self.assertEqual(chip.consumo_bytes, 250_500_000)
        self.assertEqual(chip.sessao_consulta.estado_bruto, "UP")
        def eventos():
            return M2MEvento.objects.filter(chip=chip, tipo="estado_sessao_mudou").order_by("em", "pk")

        self.assertEqual([(e.dados["de"], e.dados["para"]) for e in eventos()], [("nao_consultado", "ativa")])

        self.atualizar(chip, roteador(sessao=SESSAO_UP))                         # mesmo estado: sem evento novo
        self.assertEqual(eventos().count(), 1)
        self.atualizar(chip, roteador(sessao=SESSAO_DOWN))
        self.assertEqual(chip.sessao_estado, "sem_sessao")
        self.assertEqual([(e.dados["de"], e.dados["para"]) for e in eventos()],
                         [("nao_consultado", "ativa"), ("ativa", "sem_sessao")])

    def test_valor_desconhecido_vira_nao_interpretado_e_guarda_o_bruto(self):
        self.mapear()
        chip = self.chip(1)
        self.atualizar(chip, roteador(sessao={"sessao": {"estado": "SUSPENSA"}}))
        self.assertEqual(chip.sessao_estado, "nao_interpretado")
        self.assertEqual(chip.sessao_consulta.estado_bruto, "SUSPENSA")

    def test_uma_falha_nao_substitui_o_ultimo_dado_valido(self):
        self.mapear()
        chip = self.chip(1)
        self.atualizar(chip, roteador())
        sessao_antes, consumo_antes = chip.sessao_consulta_id, chip.consumo_consulta_id
        valida_em = chip.ultima_valida_em
        self.assertEqual((chip.sessao_estado, chip.consumo_bytes), ("ativa", 250_500_000))

        r, _ = self.atualizar(chip, roteador(status=500))
        self.assertTrue(r.houve_falha)
        falha = r.consultas[0]
        self.assertEqual((falha.resultado, falha.tentativas), ("indisponivel", 3))
        self.assertEqual(falha.estado_interpretado, "")                          # falha não carrega interpretação
        self.assertIsNone(falha.volume_bytes)
        self.assertEqual(
            (chip.sessao_consulta_id, chip.consumo_consulta_id, chip.sessao_estado, chip.consumo_bytes, chip.ultima_valida_em),
            (sessao_antes, consumo_antes, "ativa", 250_500_000, valida_em),
        )
        self.assertEqual((chip.falhas_consecutivas, chip.ultima_falha_tipo), (1, "indisponivel"))
        self.assertIsNotNone(chip.ultima_falha_em)

    def test_404_nao_vira_sem_sessao(self):
        self.mapear()
        chip = self.chip(1)
        self.atualizar(chip, roteador())
        r, _ = self.atualizar(chip, roteador(status=404))
        self.assertEqual(r.consultas[0].resultado, "nao_encontrado")
        self.assertEqual(chip.sessao_estado, "ativa")                            # continua o último dado válido
        self.assertNotEqual(chip.sessao_estado, "sem_sessao")
        self.assertEqual(chip.ultima_falha_tipo, "nao_encontrado")

    def test_consulta_nova_nao_apaga_a_anterior(self):
        self.mapear()
        chip = self.chip(1)
        self.atualizar(chip, roteador(sessao=SESSAO_UP))
        primeira = chip.sessao_consulta
        self.atualizar(chip, roteador(sessao=SESSAO_DOWN))
        self.assertEqual(chip.consultas.filter(tipo="sessao").count(), 2)
        primeira.refresh_from_db()
        self.assertEqual((primeira.payload, primeira.estado_interpretado), (SESSAO_UP, "ativa"))
        self.assertNotEqual(chip.sessao_consulta_id, primeira.pk)

    def test_sucesso_sem_corpo_conta_como_valido_mas_nao_interpretado(self):
        self.mapear()
        chip = self.chip(1)
        self.atualizar(chip, lambda url: Resp(204), tipos=("sessao",))
        self.assertEqual(chip.sessao_consulta.resultado, "sucesso_sem_corpo")
        self.assertIsNone(chip.sessao_consulta.payload)
        self.assertEqual(chip.sessao_estado, "nao_interpretado")
        self.assertIsNotNone(chip.ultima_valida_em)
        self.assertEqual(chip.falhas_consecutivas, 0)

    def test_falhas_consecutivas_contam_por_atualizacao_e_zeram_no_sucesso(self):
        chip = self.chip(1)
        self.atualizar(chip, roteador(status=500))                               # 2 consultas falham (sessão e consumo)
        self.assertEqual(chip.consultas.count(), 2)
        self.assertEqual(chip.falhas_consecutivas, 1)
        self.atualizar(chip, roteador(status=500))
        self.assertEqual(chip.falhas_consecutivas, 2)
        self.atualizar(chip, roteador())
        self.assertEqual(chip.falhas_consecutivas, 0)

    def test_401_interrompe_na_hora_e_marca_a_credencial_como_recusada(self):
        chip = self.chip(1)
        r, sessao = self.atualizar(chip, roteador(status=401))
        self.assertEqual(r.interrompida_por, "falha_autenticacao")
        self.assertEqual(len(sessao.chamadas), 1)                                # não consultou o consumo
        cfg = M2MIntegracao.get()
        self.assertIsNotNone(cfg.falha_autenticacao_em)
        self.assertEqual(cfg.ultimo_status_http, 401)
        self.assertEqual(chip.falhas_consecutivas, 1)

    def test_429_grava_bloqueado_ate_e_recusa_novas_consultas_sem_chamar_a_api(self):
        chip = self.chip(1)
        cliente, sessao = self.cliente(lambda url: Resp(429, headers={"Retry-After": "600"}))
        r = m2m.atualizar(chip, ("sessao", "consumo"), "agendada", self.usuario, cliente=cliente)
        self.assertEqual(r.interrompida_por, "limite_excedido")
        self.assertEqual(len(sessao.chamadas), 1)
        bloqueio = M2MIntegracao.get().bloqueado_ate
        self.assertAlmostEqual((bloqueio - timezone.now()).total_seconds(), 600, delta=30)

        with self.assertRaises(m2m.AtualizacaoRecusada) as ctx:
            m2m.atualizar(chip, ("sessao",), "manual", self.usuario, cliente=cliente)
        self.assertEqual(ctx.exception.codigo, "bloqueada")
        self.assertIn("pausadas até", ctx.exception.messages[0])
        self.assertEqual(len(sessao.chamadas), 1)                                # nenhuma chamada nova

    def test_sem_token_a_atualizacao_e_recusada_sem_gravar_nada(self):
        chip = self.chip(1)
        with override_settings(TIM_M2M_TOKEN=""):
            with self.assertRaises(m2m.AtualizacaoRecusada) as ctx:
                m2m.atualizar(chip, ("sessao",), "manual", self.usuario, cliente=self.cliente([])[0])
        self.assertEqual(ctx.exception.codigo, "configuracao")
        self.assertEqual(M2MConsulta.objects.count(), 0)

    def test_atualizacao_manual_do_mesmo_chip_espera_60_segundos(self):
        chip = self.chip(1)
        cliente, sessao = self.cliente(roteador())
        m2m.atualizar(chip, ("sessao",), "manual", self.usuario, cliente=cliente)
        with self.assertRaises(m2m.AtualizacaoRecusada) as ctx:
            m2m.atualizar(chip, ("sessao",), "manual", self.usuario, cliente=cliente)
        self.assertEqual(ctx.exception.codigo, "aguardar")
        self.assertLessEqual(ctx.exception.aguardar_s, 60)
        self.assertEqual(len(sessao.chamadas), 1)
        # a agendada não tem essa trava; outro chip também não
        m2m.atualizar(chip, ("sessao",), "agendada", None, cliente=cliente)
        m2m.atualizar(self.chip(2), ("sessao",), "manual", self.usuario, cliente=cliente)
        self.assertEqual(len(sessao.chamadas), 3)
        # passados os 60 s, volta a valer
        M2MChip.objects.filter(pk=chip.pk).update(ultima_tentativa_em=timezone.now() - timedelta(seconds=61))
        chip.refresh_from_db()
        m2m.atualizar(chip, ("sessao",), "manual", self.usuario, cliente=cliente)
        self.assertEqual(len(sessao.chamadas), 4)

    def test_chip_desativado_nao_e_consultado_mas_o_teste_de_conexao_vale(self):
        chip = self.chip(1)
        m2m.desativar(chip, self.usuario, "parado")
        with self.assertRaises(m2m.AtualizacaoRecusada) as ctx:
            m2m.atualizar(chip, ("sessao",), "manual", self.usuario, cliente=self.cliente(roteador())[0])
        self.assertEqual(ctx.exception.codigo, "desativado")
        consulta = m2m.testar_conexao(chip, self.usuario, cliente=self.cliente(roteador())[0])
        self.assertEqual(consulta.origem, "teste")

    def test_saude_da_integracao_acompanha_as_respostas(self):
        chip = self.chip(1)
        self.atualizar(chip, roteador())
        cfg = M2MIntegracao.get()
        self.assertIsNotNone(cfg.autenticada_em)
        self.assertEqual(cfg.ultimo_status_http, 200)
        self.assertEqual(m2m.estado_integracao()["estado"], "autenticada")

    def test_tipos_invalidos_sao_erro_de_programacao(self):
        with self.assertRaises(ValueError):
            m2m.atualizar(self.chip(1), ("sessao", "cobertura"), "agendada", cliente=self.cliente([])[0])

    def test_testar_conexao_grava_consulta_resumo_e_evento(self):
        chip = self.chip(1)
        consulta = m2m.testar_conexao(chip, self.usuario, cliente=self.cliente([Resp(200, json=SESSAO_UP)])[0])
        self.assertEqual((consulta.tipo, consulta.origem, consulta.resultado), ("sessao", "teste", "sucesso"))
        cfg = M2MIntegracao.get()
        self.assertEqual(cfg.ultimo_teste["resultado"], "sucesso")
        self.assertEqual(cfg.ultimo_teste["chip_id"], chip.pk)
        self.assertEqual(M2MEvento.objects.filter(tipo="teste_conexao", chip=chip).count(), 1)

    def test_testar_conexao_com_dns_quebrado_diz_erro_de_rede_e_nao_inventa_sucesso(self):
        chip = self.chip(1)
        erro = requests.ConnectionError("Failed to resolve 'tim-m2m-platform.com' (NameResolutionError)")
        consulta = m2m.testar_conexao(chip, self.usuario, cliente=self.cliente([erro, erro])[0])
        self.assertEqual(consulta.resultado, "erro_rede")
        self.assertIn("host não encontrado", consulta.mensagem_erro)
        self.assertEqual(M2MIntegracao.get().ultimo_teste["resultado"], "erro_rede")
        self.assertIsNone(M2MIntegracao.get().autenticada_em)
        self.assertEqual(m2m.estado_integracao()["estado"], "nao_testada")

    def test_resumo_da_atualizacao_em_texto(self):
        chip = self.chip(1)
        r, _ = self.atualizar(chip, roteador())
        self.assertEqual(r.resumo(), "Sessão: sucesso. Consumo: sucesso.")


# ═════════════════════════════════════════════════════════════
# Ocorrências
# ═════════════════════════════════════════════════════════════

class OcorrenciasTests(M2MBase):
    def abertas(self, chip, tipo=None):
        qs = M2MOcorrencia.objects.filter(chip=chip, estado__in=("aberta", "em_tratamento"))
        return qs.filter(tipo=tipo) if tipo else qs

    def test_uma_falha_isolada_nao_gera_ocorrencia_e_tres_geram_uma(self):
        chip = self.chip(1)
        for n in (1, 2):
            self.falhar(chip, 1)
            self.assertEqual(m2m.avaliar_ocorrencias(chip), [], f"{n} falha(s) não bastam")
        self.assertFalse(M2MOcorrencia.objects.exists())
        self.falhar(chip, 1)
        novas = m2m.avaliar_ocorrencias(chip)
        self.assertEqual([o.tipo for o in novas], ["falha_recorrente"])
        oc = novas[0]
        self.assertEqual((oc.nivel, oc.estado, oc.deteccoes), ("atencao", "aberta", 1))
        self.assertEqual(oc.tratamentos.get().acao, "detectada")

    def test_o_texto_diz_que_e_falha_de_consulta_e_nao_chip_indisponivel(self):
        chip = self.chip(1)
        self.falhar(chip, 3, status=404)
        oc = m2m.avaliar_ocorrencias(chip)[0]
        self.assertIn("falha de CONSULTA", oc.descricao)
        self.assertIn("não indica que o chip esteja indisponível", oc.descricao)
        self.assertIn("não encontrado", oc.descricao.lower())
        self.assertIn("Nenhuma consulta deste chip teve sucesso", oc.descricao)

    def test_deduplica_atualiza_deteccoes_e_a_constraint_garante(self):
        chip = self.chip(1)
        self.falhar(chip, 3)
        primeira = m2m.avaliar_ocorrencias(chip)[0]
        self.falhar(chip, 1)
        self.assertEqual(m2m.avaliar_ocorrencias(chip), [])                      # nada novo
        primeira.refresh_from_db()
        self.assertEqual(primeira.deteccoes, 2)
        self.assertEqual(self.abertas(chip).count(), 1)
        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError), transaction.atomic():
            M2MOcorrencia.objects.create(chip=chip, tipo="falha_recorrente")

    def test_resolve_sozinha_quando_a_condicao_some_e_reabre_em_nova_serie(self):
        chip = self.chip(1)
        self.falhar(chip, 3)
        oc = m2m.avaliar_ocorrencias(chip)[0]
        self.atualizar(chip, roteador(), avaliar=False)                          # consulta voltou
        self.assertEqual(m2m.avaliar_ocorrencias(chip), [])
        oc.refresh_from_db()
        self.assertEqual(oc.estado, "resolvida")
        self.assertIsNotNone(oc.resolvida_em)
        self.assertIsNone(oc.resolvida_por)
        self.assertEqual(oc.tratamentos.last().acao, "resolvida_automatica")
        self.falhar(chip, 3)
        nova = m2m.avaliar_ocorrencias(chip)[0]
        self.assertNotEqual(nova.pk, oc.pk)

    def test_avaliar_dentro_de_atualizar_abre_a_ocorrencia(self):
        chip = self.chip(1)
        self.falhar(chip, 2)
        self.atualizar(chip, roteador(status=500))                               # 3a falha, avaliando de dentro
        self.assertEqual(self.abertas(chip, "falha_recorrente").count(), 1)

    def test_limite_de_falhas_e_configuravel(self):
        chip = self.chip(1)
        m2m.salvar_configuracao({"falhas_para_alerta": 1}, self.usuario)
        self.falhar(chip, 1)
        self.assertEqual(len(m2m.avaliar_ocorrencias(chip)), 1)

    def test_sem_sessao_so_depois_de_x_horas_confirmado_pelo_mapeamento(self):
        self.mapear()
        chip = self.vincular(self.chip(1))
        self.atualizar(chip, roteador(sessao=SESSAO_DOWN), avaliar=False)
        self.assertEqual(chip.sessao_estado, "sem_sessao")
        self.assertEqual(m2m.avaliar_ocorrencias(chip), [])                      # agora mesmo: < 24 h
        M2MConsulta.objects.filter(chip=chip, tipo="sessao").update(iniciada_em=timezone.now() - timedelta(hours=30))
        novas = m2m.avaliar_ocorrencias(chip)
        self.assertEqual([o.tipo for o in novas], ["sem_sessao"])
        self.assertIn("confirma que o chip está sem sessão", novas[0].descricao)
        self.atualizar(chip, roteador(sessao=SESSAO_UP), avaliar=False)          # voltou
        m2m.avaliar_ocorrencias(chip)
        self.assertEqual(self.abertas(chip, "sem_sessao").count(), 0)

    def test_sem_mapeamento_nunca_ha_ocorrencia_de_sem_sessao(self):
        chip = self.chip(1)
        self.atualizar(chip, roteador(sessao=SESSAO_DOWN), avaliar=False)
        M2MConsulta.objects.update(iniciada_em=timezone.now() - timedelta(hours=100))
        M2MChip.objects.filter(pk=chip.pk).update(ultima_valida_em=timezone.now())
        chip.refresh_from_db()
        self.assertNotIn("sem_sessao", [o.tipo for o in m2m.avaliar_ocorrencias(chip)])

    def test_a_contagem_de_horas_sem_sessao_parte_do_inicio_da_sequencia_atual(self):
        self.mapear()
        chip = self.vincular(self.chip(1))
        agora = timezone.now()
        def consulta(estado, horas_atras):
            M2MConsulta.objects.create(
                chip=chip, tipo="sessao", origem="agendada", iniciada_em=agora - timedelta(hours=horas_atras),
                identificador_tipo="iccid", resultado="sucesso", estado_interpretado=estado,
            )
        consulta("sem_sessao", 60)
        consulta("ativa", 40)          # voltou
        consulta("sem_sessao", 10)     # caiu de novo há 10 h
        consulta("sem_sessao", 2)
        M2MChip.objects.filter(pk=chip.pk).update(sessao_estado="sem_sessao", ultima_tentativa_em=agora, ultima_valida_em=agora)
        chip.refresh_from_db()
        self.assertEqual(m2m.avaliar_ocorrencias(chip), [])                      # 10 h < 24 h: a queda antiga não conta
        M2MConsulta.objects.filter(iniciada_em__gte=agora - timedelta(hours=11), estado_interpretado="sem_sessao").update(
            iniciada_em=agora - timedelta(hours=30))
        self.assertEqual([o.tipo for o in m2m.avaliar_ocorrencias(chip)], ["sem_sessao"])

    def test_desatualizado_quando_o_ultimo_dado_valido_passou_do_limite(self):
        chip = self.chip(1)
        self.assertEqual(m2m.avaliar_ocorrencias(chip), [])                      # nunca consultado: pendente, não desatualizado
        self.atualizar(chip, roteador(), avaliar=False)
        self.assertEqual(m2m.avaliar_ocorrencias(chip), [])
        M2MChip.objects.filter(pk=chip.pk).update(ultima_valida_em=timezone.now() - timedelta(hours=30))
        chip.refresh_from_db()
        novas = m2m.avaliar_ocorrencias(chip)
        self.assertEqual([o.tipo for o in novas], ["desatualizado"])
        self.atualizar(chip, roteador(), avaliar=False)
        m2m.avaliar_ocorrencias(chip)
        self.assertEqual(self.abertas(chip).count(), 0)

    def test_desatualizado_tambem_vale_para_chip_que_nunca_teve_sucesso(self):
        chip = self.chip(1)
        self.falhar(chip, 1)
        M2MChip.objects.filter(pk=chip.pk).update(created_at=timezone.now() - timedelta(hours=30))
        chip.refresh_from_db()
        novas = m2m.avaliar_ocorrencias(chip)
        self.assertEqual([o.tipo for o in novas], ["desatualizado"])
        self.assertIn("nenhuma consulta teve sucesso", novas[0].descricao)

    def test_consumo_elevado_so_com_limite_mensal_cadastrado(self):
        self.mapear()
        chip = self.chip(1)
        self.atualizar(chip, roteador(uso={"uso": {"total": 850}}), avaliar=False)   # 850 MB
        self.assertEqual(m2m.avaliar_ocorrencias(chip), [])                          # sem limite cadastrado: nada
        m2m.editar_chip(chip, {"limite_mensal_bytes": 1_000_000_000}, self.usuario)   # 1 GB: 85 %
        chip.refresh_from_db()
        novas = m2m.avaliar_ocorrencias(chip)
        self.assertEqual([(o.tipo, o.nivel) for o in novas], [("consumo_elevado", "atencao")])
        self.assertIn("85%", novas[0].descricao)
        self.atualizar(chip, roteador(uso={"uso": {"total": 1200}}), avaliar=False)  # passou de 100 %
        self.assertEqual(m2m.avaliar_ocorrencias(chip), [])                          # a mesma ocorrência
        oc = self.abertas(chip, "consumo_elevado").get()
        self.assertEqual(oc.nivel, "critico")
        self.atualizar(chip, roteador(uso={"uso": {"total": 100}}), avaliar=False)   # 10 %: some
        m2m.avaliar_ocorrencias(chip)
        self.assertEqual(self.abertas(chip).count(), 0)

    def test_consumo_nao_interpretado_nunca_dispara_consumo_elevado(self):
        chip = self.chip(1, limite_mensal_bytes=1)
        self.atualizar(chip, roteador(), avaliar=False)                              # sem mapeamento
        self.assertIsNone(chip.consumo_bytes)
        self.assertEqual(m2m.avaliar_ocorrencias(chip), [])

    def test_identificador_divergente_so_com_mapeamento_de_identidade(self):
        chip = self.chip(1)
        diferente = {**SESSAO_UP, "sim": {"iccid": iccid_valido(99)}}
        self.atualizar(chip, roteador(sessao=diferente), avaliar=False)
        self.assertEqual(m2m.avaliar_ocorrencias(chip), [])                          # sem mapeamento de identidade
        self.mapear()
        novas = m2m.avaliar_ocorrencias(chip)
        self.assertEqual([o.tipo for o in novas], ["identificador_divergente"])
        self.assertIn(iccid_valido(99), novas[0].descricao)
        igual = {**SESSAO_UP, "sim": {"iccid": chip.iccid}}
        self.atualizar(chip, roteador(sessao=igual), avaliar=False)
        m2m.avaliar_ocorrencias(chip)
        self.assertEqual(self.abertas(chip).count(), 0)

    def test_chip_desativado_nao_abre_ocorrencia_e_desativar_encerra_as_abertas(self):
        chip = self.chip(1)
        self.falhar(chip, 3)
        oc = m2m.avaliar_ocorrencias(chip)[0]
        m2m.desativar(chip, self.usuario, "parado")
        oc.refresh_from_db()
        self.assertEqual(oc.estado, "resolvida")
        self.assertIn("desativado", oc.tratamentos.last().comentario)
        chip.refresh_from_db()
        self.assertEqual(m2m.avaliar_ocorrencias(chip), [])

    def test_tratamento_manual_assumir_resolver_descartar_e_comentar(self):
        chip = self.chip(1)
        self.falhar(chip, 3)
        oc = m2m.avaliar_ocorrencias(chip)[0]
        m2m.tratar_ocorrencia(oc, "assumir", self.usuario)
        oc.refresh_from_db()
        self.assertEqual(oc.estado, "em_tratamento")
        with self.assertRaises(ValidationError):
            m2m.tratar_ocorrencia(oc, "assumir", self.usuario)                       # já está
        with self.assertRaises(ValidationError):
            m2m.tratar_ocorrencia(oc, "resolver", self.usuario, "  ")                # comentário obrigatório
        with self.assertRaises(ValidationError):
            m2m.tratar_ocorrencia(oc, "dançar", self.usuario, "x")
        m2m.tratar_ocorrencia(oc, "comentar", self.usuario, "Liguei para a TIM")
        m2m.tratar_ocorrencia(oc, "resolver", self.usuario, "API voltou")
        oc.refresh_from_db()
        self.assertEqual((oc.estado, oc.resolvida_por), ("resolvida", self.usuario))
        self.assertEqual([t.acao for t in oc.tratamentos.all()], ["detectada", "assumir", "comentar", "resolver"])
        with self.assertRaises(ValidationError):
            m2m.tratar_ocorrencia(oc, "descartar", self.usuario, "tarde demais")     # já encerrada
        m2m.tratar_ocorrencia(oc, "comentar", self.usuario, "depois de encerrar ainda dá para comentar")

    def test_descartada_nao_volta_enquanto_a_condicao_persiste_mas_volta_se_ela_reaparecer(self):
        chip = self.chip(1)
        self.falhar(chip, 3)
        oc = m2m.avaliar_ocorrencias(chip)[0]
        m2m.tratar_ocorrencia(oc, "descartar", self.usuario, "Já sei: chip em manutenção na TIM")
        self.falhar(chip, 1)
        self.assertEqual(m2m.avaliar_ocorrencias(chip), [])                          # condição persiste: não reabre
        self.assertEqual(M2MOcorrencia.objects.filter(chip=chip).count(), 1)
        self.atualizar(chip, roteador(), avaliar=False)                              # condição some
        m2m.avaliar_ocorrencias(chip)
        self.assertTrue(oc.tratamentos.filter(acao="condicao_encerrada").exists())
        self.falhar(chip, 3)
        novas = m2m.avaliar_ocorrencias(chip)                                         # reaparece: novo alerta
        self.assertEqual(len(novas), 1)
        self.assertNotEqual(novas[0].pk, oc.pk)


# ═════════════════════════════════════════════════════════════
# E-mail
# ═════════════════════════════════════════════════════════════

class EmailTests(M2MBase):
    def setUp(self):
        super().setUp()
        email_alertas.sincronizar_catalogo_notificacoes()
        # Os destinatários moram na REGRA do tipo (o canal é `dinamico`).
        FrotaRegraAlerta.objects.filter(tipo="falha_recorrente").update(
            envia_email=True, destinatarios_emails="ti@teste.invalid",
        )
        mail.outbox = []

    def emails(self):
        return [m for m in mail.outbox if "Frotas e Conectividade" in m.subject]

    def test_o_canal_esta_no_catalogo_e_sincroniza(self):
        self.assertIn("m2m_ocorrencias", {c["codigo"] for c in email_alertas.CATALOGO_NOTIFICACOES})
        canal = CanalNotificacao.objects.get(codigo="m2m_ocorrencias")
        self.assertEqual((canal.categoria, canal.tipo_destinatarios), ("Monitoramento", "dinamico"))
        self.assertEqual(canal.destino_gerenciado_em, "Frotas e Conectividade › Configuração")
        self.assertLessEqual(len(canal.descricao), 255)

    def test_ocorrencia_nova_manda_um_email_e_a_repeticao_nao(self):
        chip = self.chip(1, nome="Pivô 3")
        self.falhar(chip, 2)
        with self.captureOnCommitCallbacks(execute=True):
            self.atualizar(chip, roteador(status=500))                               # 3a falha: abre
        self.assertEqual(len(self.emails()), 1)
        msg = self.emails()[0]
        self.assertEqual(msg.to, ["ti@teste.invalid"])
        self.assertIn("Pivô 3", msg.body)
        self.assertIn("falha de CONSULTA", msg.body)
        self.assertIn("Falha de consulta", msg.body)                                 # o aviso "Como ler"
        with self.captureOnCommitCallbacks(execute=True):
            self.atualizar(chip, roteador(status=500))                               # 4a: mesma ocorrência
        self.assertEqual(len(self.emails()), 1)

    def test_canal_desativado_nao_envia(self):
        CanalNotificacao.objects.filter(codigo="m2m_ocorrencias").update(ativo=False)
        chip = self.chip(1)
        self.falhar(chip, 2)
        with self.captureOnCommitCallbacks(execute=True):
            self.atualizar(chip, roteador(status=500))
        self.assertEqual(self.emails(), [])

    def test_regra_com_email_desligado_nao_envia(self):
        FrotaRegraAlerta.objects.filter(tipo="desatualizado").update(envia_email=False)
        chip = self.chip(1)
        oc = M2MOcorrencia.objects.create(chip=chip, tipo="desatualizado", nivel="atencao", descricao="x")
        with self.captureOnCommitCallbacks(execute=True):
            m2m.notificar_ocorrencias([oc])
        self.assertEqual(self.emails(), [])

    def test_falha_no_envio_nao_derruba_o_monitoramento(self):
        chip = self.chip(1)
        self.falhar(chip, 2)
        with mock.patch.object(email_alertas, "alerta_m2m_ocorrencias", side_effect=RuntimeError("smtp fora")):
            with self.captureOnCommitCallbacks(execute=True):
                self.atualizar(chip, roteador(status=500))
        self.assertEqual(M2MOcorrencia.objects.filter(chip=chip, estado="aberta").count(), 1)

    def test_o_ciclo_manda_um_unico_email_consolidado(self):
        for n in (1, 2):
            chip = self.chip(n)
            self.falhar(chip, 2)
        M2MChip.objects.update(proxima_consulta_em=None)                             # os dois vencem neste ciclo
        with self.captureOnCommitCallbacks(execute=True):
            with mock.patch("services.tim_m2m_service.requests.Session", return_value=Sessao(roteador(status=500))):
                m2m.executar_ciclo()
        self.assertEqual(len(self.emails()), 1)
        self.assertIn("2 ocorrência(s) nova(s)", self.emails()[0].subject)
        self.assertIn("Chip 1", self.emails()[0].body)
        self.assertIn("Chip 2", self.emails()[0].body)


# ═════════════════════════════════════════════════════════════
# Ciclo
# ═════════════════════════════════════════════════════════════

class CicloTests(M2MBase):
    def ciclo(self, respostas, **kw):
        sessao = Sessao(respostas)
        resumo = m2m.executar_ciclo(cliente=tim.ClienteTIM(sessao), **kw)
        return resumo, sessao

    def test_sem_token_pula_sem_consultar(self):
        self.chip(1)
        with override_settings(TIM_M2M_TOKEN=""):
            resumo, sessao = self.ciclo(roteador())
        self.assertFalse(resumo.executou)
        self.assertEqual(resumo.motivo_pulo, "sem_configuracao")
        self.assertTrue(resumo.exige_atencao)
        self.assertEqual(sessao.chamadas, [])
        self.assertIsNotNone(M2MIntegracao.get().ultima_execucao)                     # a tarefa está viva
        self.assertIn("token", M2MIntegracao.get().ultimo_erro)

    def test_consulta_so_os_vencidos_os_mais_atrasados_primeiro_e_respeita_o_maximo(self):
        futuro = self.chip(1)
        nunca = self.chip(2)
        atrasado = self.chip(3)
        recente = self.chip(4)
        agora = timezone.now()
        M2MChip.objects.filter(pk=futuro.pk).update(proxima_consulta_em=agora + timedelta(hours=1))
        M2MChip.objects.filter(pk=atrasado.pk).update(proxima_consulta_em=agora - timedelta(hours=5))
        M2MChip.objects.filter(pk=recente.pk).update(proxima_consulta_em=agora - timedelta(minutes=1))
        M2MIntegracao.objects.filter(pk=1).update(max_chips_execucao=2)
        resumo, sessao = self.ciclo(roteador())
        self.assertEqual(resumo.consultados, 2)
        ordem = [u.split("/")[-2] for u in sessao.urls if u.endswith("/session")]
        self.assertEqual(ordem, [nunca.iccid, atrasado.iccid])                         # nunca consultado, depois o mais atrasado
        self.assertNotIn(futuro.iccid, " ".join(sessao.urls))
        self.assertNotIn(recente.iccid, " ".join(sessao.urls))                         # o limite de 2 deixou para a próxima

    def test_chips_desativados_sao_ignorados(self):
        chip = self.chip(1)
        m2m.desativar(chip, self.usuario, "parado")
        resumo, sessao = self.ciclo(roteador())
        self.assertEqual((resumo.executou, resumo.consultados, sessao.chamadas), (True, 0, []))

    def test_a_pausa_entre_as_chamadas_e_a_configurada(self):
        M2MIntegracao.objects.filter(pk=1).update(pausa_entre_chamadas_ms=2500)
        for n in (1, 2, 3):
            self.chip(n)
        resumo, sessao = self.ciclo(roteador())
        self.assertEqual(len(sessao.chamadas), 6)                                       # 3 chips x 2 consultas
        self.assertEqual(len(self.sleeps_de(2.5)), 5)                                   # entre as 6 chamadas
        self.assertEqual(self.sleep.call_count, 5)                                      # (nenhuma retentativa aqui)

    def test_a_pausa_nunca_e_menor_que_o_teto_de_requisicoes_por_minuto(self):
        cfg = M2MIntegracao.get()
        cfg.pausa_entre_chamadas_ms, cfg.limite_req_minuto = 100, 30
        self.assertEqual(m2m.pausa_efetiva_s(cfg), 2.0)                                 # 60 / 30
        cfg.pausa_entre_chamadas_ms = 5000
        self.assertEqual(m2m.pausa_efetiva_s(cfg), 5.0)
        cfg.pausa_entre_chamadas_ms, cfg.limite_req_minuto = 0, 120
        self.assertEqual(m2m.pausa_efetiva_s(cfg), 0.5)

    def test_para_no_primeiro_429_e_nao_volta_a_consultar_ate_o_bloqueio_passar(self):
        chips = [self.chip(n) for n in (1, 2, 3)]
        chamadas = []

        def fn(url):
            chamadas.append(url)
            return Resp(429, headers={"Retry-After": "900"}) if chips[1].iccid in url else Resp(200, json=SESSAO_UP)

        resumo, sessao = self.ciclo(fn)
        self.assertEqual((resumo.consultados, resumo.interrompido_por), (2, "limite_excedido"))
        self.assertFalse(any(chips[2].iccid in u for u in chamadas))                    # o terceiro nem foi tentado
        self.assertIsNotNone(M2MIntegracao.get().bloqueado_ate)
        self.assertFalse(resumo.exige_atencao)                                          # pausa por limite sai com 0
        self.assertIn("pausadas até", resumo.erro)
        self.assertEqual(M2MChip.objects.get(pk=chips[2].pk).consultas.count(), 0)

        resumo2, sessao2 = self.ciclo(roteador())
        self.assertEqual((resumo2.executou, resumo2.motivo_pulo, sessao2.chamadas), (False, "bloqueada", []))
        M2MIntegracao.objects.filter(pk=1).update(bloqueado_ate=timezone.now() - timedelta(seconds=1))
        resumo3, _ = self.ciclo(roteador())
        self.assertTrue(resumo3.executou)                                               # passado o bloqueio, retoma

    def test_para_no_401_e_nao_martela_a_credencial_recusada(self):
        for n in (1, 2, 3):
            self.chip(n)
        resumo, sessao = self.ciclo(roteador(status=401))
        self.assertEqual((resumo.consultados, resumo.interrompido_por), (1, "falha_autenticacao"))
        self.assertEqual(len(sessao.chamadas), 1)
        self.assertTrue(resumo.exige_atencao)
        self.assertEqual(m2m.estado_integracao()["estado"], "credencial_recusada")

        resumo2, sessao2 = self.ciclo(roteador())                                        # próximo tique: nem tenta
        self.assertEqual((resumo2.executou, resumo2.motivo_pulo, sessao2.chamadas), (False, "credencial_recusada", []))
        self.assertTrue(resumo2.exige_atencao)

    def test_token_novo_ou_teste_com_sucesso_reabilitam_o_ciclo(self):
        chip = self.chip(1)
        self.ciclo(roteador(status=401))
        with override_settings(TIM_M2M_TOKEN=""):
            tim.salvar_token("token-corrigido-0123456789", self.usuario)                 # troca a credencial
            resumo, _ = self.ciclo(roteador())
        self.assertTrue(resumo.executou)

        M2MIntegracao.objects.filter(pk=1).update(falha_autenticacao_em=timezone.now())   # recusada de novo
        M2MChip.objects.filter(pk=chip.pk).update(ultima_tentativa_em=None)
        self.assertFalse(self.ciclo(roteador())[0].executou)
        m2m.testar_conexao(chip, self.usuario, cliente=self.cliente(roteador())[0])       # teste com sucesso: autenticada_em > falha
        self.assertTrue(self.ciclo(roteador())[0].executou)

    def test_trava_contra_execucao_sobreposta_e_expira_em_30_minutos(self):
        self.chip(1)
        M2MIntegracao.objects.filter(pk=1).update(executando_desde=timezone.now() - timedelta(minutes=5))
        resumo, sessao = self.ciclo(roteador())
        self.assertEqual((resumo.executou, resumo.motivo_pulo, sessao.chamadas), (False, "em_andamento", []))
        self.assertIsNotNone(M2MIntegracao.get().executando_desde)                       # a execução real não foi desarmada
        self.assertFalse(resumo.exige_atencao)

        M2MIntegracao.objects.filter(pk=1).update(executando_desde=timezone.now() - timedelta(minutes=31))
        resumo, sessao = self.ciclo(roteador())
        self.assertTrue(resumo.executou)                                                 # trava velha não segura para sempre
        self.assertGreater(len(sessao.chamadas), 0)

    def test_a_trava_e_liberada_ao_fim_e_tambem_quando_o_ciclo_quebra(self):
        self.chip(1)
        self.ciclo(roteador())
        self.assertIsNone(M2MIntegracao.get().executando_desde)

        M2MChip.objects.update(proxima_consulta_em=None)
        class Quebrado:
            def consultar(self, *a, **k):
                raise RuntimeError(f"boom com Bearer {TOKEN}")
        resumo = m2m.executar_ciclo(cliente=Quebrado())
        self.assertIn("Erro inesperado", resumo.erro)
        self.assertTrue(any("erro inesperado" in linha for linha in self.logs))         # o traceback vai para o log...
        self.assertFalse(any(TOKEN in linha for linha in self.logs))                     # ...sem o token
        self.assertNotIn(TOKEN, resumo.erro)
        self.assertTrue(resumo.exige_atencao)
        self.assertIsNone(M2MIntegracao.get().executando_desde)
        self.assertIn("Erro inesperado", M2MIntegracao.get().ultimo_erro)

    def test_saude_gravada_no_singleton(self):
        self.chip(1)
        antes = timezone.now()
        resumo, _ = self.ciclo(roteador(), tique_minutos=10)
        cfg = M2MIntegracao.get()
        self.assertEqual((resumo.consultados, resumo.sucessos, resumo.falhas), (1, 1, 0))
        self.assertGreaterEqual(cfg.ultima_execucao, antes)
        self.assertGreaterEqual(cfg.ultimo_sucesso, antes)
        self.assertEqual((cfg.ultimo_erro, cfg.tique_minutos), ("", 10))
        self.assertIsNotNone(cfg.proxima_execucao)
        self.assertGreater(cfg.proxima_execucao, antes + timedelta(minutes=50))          # intervalo de 60 min
        self.ciclo(roteador())
        self.assertEqual(M2MIntegracao.get().tique_minutos, 10)                           # sem --tique, não muda

    def test_se_todas_as_consultas_falham_o_ciclo_diz_erro_e_nao_marca_sucesso(self):
        for n in (1, 2):
            self.chip(n)
        resumo, _ = self.ciclo(roteador(status=500))
        self.assertEqual((resumo.consultados, resumo.falhas, resumo.sucessos), (2, 2, 0))
        self.assertIn("Todas as 2 consultas falharam", resumo.erro)
        self.assertTrue(resumo.exige_atencao)
        cfg = M2MIntegracao.get()
        self.assertIsNone(cfg.ultimo_sucesso)
        self.assertIn("Todas as 2", cfg.ultimo_erro)

    def test_o_ciclo_avalia_ocorrencias_de_todos_inclusive_dos_nao_vencidos(self):
        chip = self.chip(1)
        self.atualizar(chip, roteador(), avaliar=False)
        M2MChip.objects.filter(pk=chip.pk).update(
            ultima_valida_em=timezone.now() - timedelta(hours=30),
            proxima_consulta_em=timezone.now() + timedelta(hours=1),                      # não está vencido
        )
        resumo, sessao = self.ciclo(roteador())
        self.assertEqual((resumo.consultados, sessao.chamadas), (0, []))
        self.assertEqual(resumo.novas_ocorrencias, 1)
        self.assertEqual(M2MOcorrencia.objects.get(chip=chip).tipo, "desatualizado")

    def test_ciclo_mapeia_o_que_o_mapeamento_manda(self):
        self.mapear()
        chip = self.chip(1)
        self.ciclo(roteador())
        chip.refresh_from_db()
        self.assertEqual((chip.sessao_estado, chip.consumo_bytes), ("ativa", 250_500_000))
        self.assertEqual(chip.consultas.filter(origem="agendada").count(), 2)

    def test_ciclo_sem_chips_roda_e_registra_saude(self):
        resumo, sessao = self.ciclo(roteador())
        self.assertTrue(resumo.executou)
        self.assertEqual((resumo.consultados, sessao.chamadas), (0, []))
        self.assertIsNotNone(M2MIntegracao.get().ultimo_sucesso)
        self.assertIsNone(M2MIntegracao.get().proxima_execucao)


# ═════════════════════════════════════════════════════════════
# Leituras para as telas
# ═════════════════════════════════════════════════════════════

class LeiturasTests(M2MBase):
    def test_indicadores_contam_tudo_em_poucas_consultas(self):
        self.mapear()
        ativo = self.chip(1)
        sem = self.chip(2)
        falho = self.chip(3)
        pendente = self.chip(4)
        parado = self.chip(5)
        self.atualizar(ativo, roteador(), avaliar=False)
        self.atualizar(sem, roteador(sessao=SESSAO_DOWN, uso={"uso": {"total": 1}}), avaliar=False)
        self.falhar(falho, 3)
        m2m.avaliar_ocorrencias(falho)
        m2m.desativar(parado, self.usuario, "parado")
        with self.assertNumQueries(3):                                                  # configuração + agregado + alertas
            ind = m2m.indicadores()
        self.assertEqual(
            ind,
            dict(
                total=5, monitorados=4, desativados=1, sem_integracao=0,
                sessao_ativa=1, sem_sessao=1, nao_interpretado=0, nao_consultado=2,
                com_consumo=2, falha_consulta=1, desatualizados=0, precisam_atencao=1, alertas_abertos=1,
            ),
        )
        self.assertEqual(ind["sessao_ativa"] + ind["sem_sessao"] + ind["nao_interpretado"] + ind["nao_consultado"], ind["monitorados"])
        self.assertEqual(m2m.contar_alertas_abertos(), 1)

    def test_sem_mapeamento_nenhum_chip_consultado_conta_como_sessao_ativa(self):
        chip = self.chip(1)
        self.atualizar(chip, roteador(), avaliar=False)
        ind = m2m.indicadores()
        self.assertEqual((ind["sessao_ativa"], ind["nao_interpretado"]), (0, 1))

    def test_indicadores_sem_chips(self):
        ind = m2m.indicadores()
        self.assertTrue(all(v == 0 for v in ind.values()))

    def test_estado_da_integracao_em_cada_situacao(self):
        agora = timezone.now()
        with override_settings(TIM_M2M_TOKEN=""):
            self.assertEqual(m2m.estado_integracao()["estado"], "nao_configurada")
            self.assertEqual(m2m.estado_integracao()["token_origem"], "nenhum")
        e = m2m.estado_integracao()
        self.assertEqual((e["estado"], e["rotulo"], e["token_origem"], e["token_sufixo"]),
                         ("nao_testada", "Configurada, não testada", "env", TOKEN[-4:]))
        M2MIntegracao.objects.filter(pk=1).update(autenticada_em=agora)
        e = m2m.estado_integracao()
        self.assertEqual(e["estado"], "autenticada")
        self.assertTrue(e["rotulo"].startswith("Autenticada às "))
        M2MIntegracao.objects.filter(pk=1).update(falha_autenticacao_em=agora + timedelta(seconds=1))
        self.assertEqual(m2m.estado_integracao()["estado"], "credencial_recusada")
        M2MIntegracao.objects.filter(pk=1).update(bloqueado_ate=agora + timedelta(minutes=20))
        e = m2m.estado_integracao()
        self.assertEqual(e["estado"], "bloqueada")
        self.assertIn("Bloqueada por limite até", e["rotulo"])
        with override_settings(TIM_M2M_BASE_URL="http://x.com"):
            self.assertEqual(m2m.estado_integracao()["estado"], "url_invalida")

    def test_estado_da_integracao_nunca_traz_o_token(self):
        tim.salvar_token("token-do-banco-0123456789", self.usuario)
        for env in (TOKEN, ""):
            with override_settings(TIM_M2M_TOKEN=env):
                texto = json.dumps(m2m.estado_integracao(), default=str)
                self.assertNotIn(TOKEN, texto)
                self.assertNotIn("token-do-banco-0123456789", texto)
                self.assertNotIn(M2MIntegracao.get().token_cifrado, texto)

    def test_tarefa_parada_depois_de_dois_tiques(self):
        agora = timezone.now()
        self.assertFalse(m2m.estado_integracao()["tarefa_parada"])                       # nunca rodou: não dá para dizer que parou
        M2MIntegracao.objects.filter(pk=1).update(ultima_execucao=agora - timedelta(minutes=20), tique_minutos=15)
        self.assertFalse(m2m.estado_integracao()["tarefa_parada"])
        M2MIntegracao.objects.filter(pk=1).update(ultima_execucao=agora - timedelta(minutes=31))
        e = m2m.estado_integracao()
        self.assertTrue(e["tarefa_parada"])
        self.assertEqual(e["ultima_sincronizacao"], M2MIntegracao.get().ultima_execucao)
        M2MIntegracao.objects.filter(pk=1).update(tique_minutos=30)                       # tique maior: o mesmo atraso é normal
        self.assertFalse(m2m.estado_integracao()["tarefa_parada"])

    def test_proxima_execucao_prevista(self):
        self.assertIsNone(m2m.estado_integracao()["proxima"])                              # sem chips
        chip = self.chip(1)
        self.assertLessEqual(m2m.estado_integracao()["proxima"], timezone.now())            # nunca consultado: já vencido
        self.atualizar(chip, roteador(), avaliar=False)
        self.assertGreater(m2m.estado_integracao()["proxima"], timezone.now() + timedelta(minutes=50))

    def test_fita_das_ultimas_consultas_mais_antiga_primeiro(self):
        self.mapear()
        chip = self.chip(1)
        base = timezone.now() - timedelta(hours=40)
        for i in range(30):
            M2MConsulta.objects.create(
                chip=chip, tipo="sessao", origem="agendada", iniciada_em=base + timedelta(hours=i),
                identificador_tipo="iccid", resultado="indisponivel" if i == 29 else "sucesso",
                estado_interpretado="ativa" if i != 28 else "nao_interpretado", http_status=500 if i == 29 else 200,
            )
        M2MConsulta.objects.create(
            chip=chip, tipo="consumo", origem="manual", iniciada_em=base + timedelta(hours=40),
            identificador_tipo="iccid", resultado="sucesso_sem_corpo", http_status=204,
        )
        with self.assertNumQueries(1):
            fita = m2m.fita_consultas(chip)
        self.assertEqual(len(fita), 24)
        self.assertEqual([f["em"] for f in fita], sorted(f["em"] for f in fita))             # esquerda -> direita, antiga -> nova
        self.assertEqual(fita[-1]["classe"], "sem_corpo")
        self.assertEqual(fita[-2]["classe"], "falha")
        self.assertEqual(fita[-3]["classe"], "nao_interpretado")
        self.assertEqual(fita[-4]["classe"], "sucesso")
        self.assertIn("HTTP 500", fita[-2]["rotulo"])
        self.assertEqual(len(m2m.fita_consultas(chip, n=5)), 5)

    def test_linha_do_tempo_mistura_consultas_eventos_e_ocorrencias(self):
        self.mapear()
        chip = self.chip(1)
        self.atualizar(chip, roteador(), avaliar=False)
        self.falhar(chip, 3)
        m2m.avaliar_ocorrencias(chip)
        linha = m2m.linha_do_tempo(chip)
        self.assertEqual({i["fonte"] for i in linha}, {"consulta", "evento", "ocorrencia"})
        self.assertEqual([i["quando"] for i in linha], sorted((i["quando"] for i in linha), reverse=True))
        for item in linha:
            self.assertEqual(set(item), {"quando", "fonte", "tipo", "titulo", "detalhe", "nivel", "ref_id"})
        so_eventos = m2m.linha_do_tempo(chip, tipos=["evento"])
        self.assertEqual({i["fonte"] for i in so_eventos}, {"evento"})
        self.assertIn("estado_sessao_mudou", {i["tipo"] for i in so_eventos})
        falha = next(i for i in linha if i["fonte"] == "consulta" and i["nivel"] == "erro")
        self.assertTrue(falha["detalhe"])
        self.assertEqual(len(m2m.linha_do_tempo(chip, limite=3)), 3)

    def test_serie_de_consumo(self):
        self.mapear()
        chip = self.chip(1)
        self.assertEqual(m2m.serie_consumo(chip)["pontos"], [])
        self.assertFalse(m2m.serie_consumo(chip)["suficiente"])
        for total in (100, 250, 400):
            self.atualizar(chip, roteador(uso={"uso": {"total": total}}), tipos=("consumo",), avaliar=False)
        self.falhar(chip, 1)                                                                    # falha não entra na série
        serie = m2m.serie_consumo(chip)
        self.assertTrue(serie["suficiente"])
        self.assertEqual([p["bytes"] for p in serie["pontos"]], [100_000_000, 250_000_000, 400_000_000])
        self.assertEqual((serie["unidade"], serie["divisor"]), ("MB", 1_000_000))
        self.assertEqual([p["valor"] for p in serie["pontos"]], [100.0, 250.0, 400.0])
        self.assertEqual([p["em"] for p in serie["pontos"]], sorted(p["em"] for p in serie["pontos"]))
        self.atualizar(chip, roteador(uso={"uso": {"total": 5000}}), tipos=("consumo",), avaliar=False)
        self.assertEqual(m2m.serie_consumo(chip)["unidade"], "GB")

    def test_serie_sem_mapeamento_fica_vazia(self):
        chip = self.chip(1)
        for _ in range(3):
            self.atualizar(chip, roteador(), tipos=("consumo",), avaliar=False)
        self.assertEqual(m2m.serie_consumo(chip)["pontos"], [])

    def test_ultima_resposta_valida_para_o_mapeamento(self):
        chip = self.chip(1)
        self.assertIsNone(m2m.ultima_resposta_valida())
        self.atualizar(chip, roteador(), avaliar=False)
        self.falhar(chip, 1)
        achada = m2m.ultima_resposta_valida("sessao")
        self.assertEqual(achada.payload, SESSAO_UP)
        self.assertEqual(m2m.ultima_resposta_valida("consumo").payload, USO_MB)
        self.assertIn("sessao.estado", interp.caminhos(achada.payload))

    def test_frescor(self):
        agora = timezone.now()
        self.assertEqual(m2m.frescor(None)["rotulo"], "nunca consultado")
        self.assertEqual(m2m.frescor(agora - timedelta(minutes=5), 24)["rotulo"], "há 5 min")
        f = m2m.frescor(agora - timedelta(hours=3), 24)
        self.assertEqual((f["rotulo"], f["desatualizado"]), ("há 3 h", False))
        f = m2m.frescor(agora - timedelta(hours=30), 24)
        self.assertEqual((f["rotulo"], f["desatualizado"]), ("há 30 h", True))
        self.assertEqual(m2m.frescor(agora - timedelta(days=4), 24)["rotulo"], "há 4 dias")

    def test_situacao_do_monitoramento_do_chip(self):
        chip = self.chip(1)
        self.assertEqual(chip.situacao_monitoramento, "pendente")
        self.atualizar(chip, roteador(), avaliar=False)
        self.assertEqual(chip.situacao_monitoramento, "normal")
        self.falhar(chip, 1)
        self.assertEqual(chip.situacao_monitoramento, "erro_consulta")
        self.atualizar(chip, roteador(), avaliar=False)
        M2MChip.objects.filter(pk=chip.pk).update(ultima_valida_em=timezone.now() - timedelta(hours=30))
        chip.refresh_from_db()
        self.assertEqual(chip.situacao_monitoramento_com(24), "desatualizado")
        self.assertEqual(chip.situacao_monitoramento_com(48), "normal")
        m2m.desativar(chip, self.usuario, "x")
        self.assertEqual(chip.situacao_monitoramento, "desativado")


# ═════════════════════════════════════════════════════════════
# Configuração
# ═════════════════════════════════════════════════════════════

class ConfiguracaoTests(M2MBase):
    def test_validar_frequencia_mostra_a_conta(self):
        v = m2m.validar_frequencia(60, 40, 30)
        self.assertTrue(v["ok"])
        self.assertAlmostEqual(v["req_por_minuto"], 1.3333, places=3)
        self.assertEqual(v["conta"], "2 consultas x 40 chips / 60 min = 1,33 req/min (limite: 30 req/min)")

    def test_validar_frequencia_recusa_o_que_estoura_o_limite_e_sugere_o_minimo(self):
        v = m2m.validar_frequencia(15, 400, 30)                       # 800 / 15 = 53,33 req/min
        self.assertFalse(v["ok"])
        self.assertEqual(v["intervalo_minimo_sugerido"], 27)           # ceil(800 / 30)
        self.assertIn("53,33", v["motivo"])
        self.assertTrue(m2m.validar_frequencia(27, 400, 30)["ok"])
        self.assertFalse(m2m.validar_frequencia(14, 1, 30)["ok"])      # mínimo de 15 min
        self.assertIn("mínimo", m2m.validar_frequencia(14, 1, 30)["motivo"])

    def test_validar_frequencia_usa_os_chips_monitorados_e_o_limite_configurado(self):
        self.chip(1)
        parado = self.chip(2)
        m2m.desativar(parado, self.usuario, "x")
        v = m2m.validar_frequencia(60)
        self.assertEqual((v["chips"], v["limite"]), (1, 30))

    def test_salvar_configuracao_grava_e_audita(self):
        cfg = m2m.salvar_configuracao(
            {"intervalo_minutos": "30", "pausa_entre_chamadas_ms": 500, "falhas_para_alerta": 5, "pct_consumo_alerta": 90},
            self.usuario,
        )
        self.assertEqual((cfg.intervalo_minutos, cfg.pausa_entre_chamadas_ms, cfg.falhas_para_alerta, cfg.pct_consumo_alerta),
                         (30, 500, 5, 90))
        evento = M2MEvento.objects.get(tipo="config_alterada")
        self.assertEqual(evento.dados["alteracoes"]["intervalo_minutos"], {"de": 60, "para": 30})
        self.assertNotIn("max_chips_execucao", evento.dados["alteracoes"])                 # só o que mudou
        m2m.salvar_configuracao({"intervalo_minutos": 30}, self.usuario)                   # sem mudança: sem evento novo
        self.assertEqual(M2MEvento.objects.filter(tipo="config_alterada").count(), 1)

    def test_salvar_configuracao_recusa_valores_ruins_sem_gravar_nada(self):
        ruins = [{"intervalo_minutos": 10}, {"intervalo_minutos": "abc"}, {"pct_consumo_alerta": 150},
                 {"falhas_para_alerta": 0}, {"max_chips_execucao": 5000}, {"limite_req_minuto": 0}]
        for dados in ruins:
            with self.subTest(dados=dados), self.assertRaises(ValidationError) as ctx:
                m2m.salvar_configuracao(dados, self.usuario)
            self.assertEqual(set(ctx.exception.message_dict), set(dados))
        self.assertEqual(M2MIntegracao.get().intervalo_minutos, 60)
        self.assertFalse(M2MEvento.objects.filter(tipo="config_alterada").exists())

    def test_salvar_configuracao_recusa_frequencia_acima_do_limite_com_a_conta_na_mensagem(self):
        for n in range(1, 11):
            self.chip(n)                                                                    # 10 chips
        with self.assertRaises(ValidationError) as ctx:
            m2m.salvar_configuracao({"intervalo_minutos": 15, "limite_req_minuto": 1}, self.usuario)   # 20/15 = 1,33 > 1
        mensagem = ctx.exception.message_dict["intervalo_minutos"][0]
        self.assertIn("2 consultas x 10 chips / 15 min = 1,33 req/min", mensagem)
        self.assertEqual(M2MIntegracao.get().limite_req_minuto, 30)                          # nada foi gravado

    def test_salvar_mapeamento_normaliza_audita_e_nao_reescreve_o_passado(self):
        chip = self.chip(1)
        self.atualizar(chip, roteador(), avaliar=False)
        antes = chip.sessao_consulta
        self.assertEqual(antes.estado_interpretado, "nao_interpretado")
        normal = m2m.salvar_mapeamento(MAPA, self.usuario)
        self.assertEqual(M2MIntegracao.get().mapeamento, normal)
        evento = M2MEvento.objects.get(tipo="mapeamento_alterado")
        self.assertEqual((evento.dados["de"], evento.dados["para"]), ({}, normal))
        antes.refresh_from_db()
        self.assertEqual(antes.estado_interpretado, "nao_interpretado")                        # o passado não mudou sozinho
        m2m.salvar_mapeamento(MAPA, self.usuario)                                              # igual: sem evento novo
        self.assertEqual(M2MEvento.objects.filter(tipo="mapeamento_alterado").count(), 1)

    def test_mapeamento_invalido_e_recusado_com_todas_as_mensagens(self):
        with self.assertRaises(ValidationError) as ctx:
            m2m.salvar_mapeamento(
                {"sessao": {"campo_estado": "a..b"}, "consumo": {"campo_volume": "v", "base": 7}}, self.usuario,
            )
        self.assertEqual(len(ctx.exception.messages), 2)
        self.assertEqual(M2MIntegracao.get().mapeamento, {})

    def test_reinterpretar_historico_recalcula_so_os_normalizados_e_nunca_o_payload(self):
        chip = self.chip(1)
        self.atualizar(chip, roteador(), avaliar=False)
        self.atualizar(chip, roteador(sessao=SESSAO_DOWN, uso={"uso": {"total": 3}}), avaliar=False)
        self.atualizar(chip, roteador(status=500), avaliar=False)
        payloads = {c.pk: c.payload for c in M2MConsulta.objects.all()}
        self.assertTrue(all(c.estado_interpretado in ("", "nao_interpretado") for c in M2MConsulta.objects.filter(tipo="sessao")))
        self.assertEqual((chip.sessao_estado, chip.consumo_bytes), ("nao_interpretado", None))

        m2m.salvar_mapeamento(MAPA, self.usuario)
        resultado = m2m.reinterpretar_historico(self.usuario)

        self.assertEqual(resultado["consultas"], 4)                                   # só as válidas (2 sessão + 2 consumo)
        self.assertEqual(resultado["alteradas"], 4)
        self.assertEqual(resultado["chips"], 1)
        self.assertEqual({c.pk: c.payload for c in M2MConsulta.objects.all()}, payloads)   # payload intocado
        estados = list(M2MConsulta.objects.filter(tipo="sessao", resultado="sucesso").order_by("iniciada_em", "pk")
                       .values_list("estado_interpretado", flat=True))
        self.assertEqual(estados, ["ativa", "sem_sessao"])
        volumes = list(M2MConsulta.objects.filter(tipo="consumo", resultado="sucesso").order_by("iniciada_em", "pk")
                       .values_list("volume_bytes", flat=True))
        self.assertEqual(volumes, [250_500_000, 3_000_000])
        falhas = M2MConsulta.objects.exclude(resultado="sucesso")
        self.assertTrue(all(f.estado_interpretado == "" and f.volume_bytes is None for f in falhas))
        chip.refresh_from_db()
        self.assertEqual((chip.sessao_estado, chip.consumo_bytes), ("sem_sessao", 3_000_000))      # a situação atual acompanha
        self.assertTrue(M2MEvento.objects.filter(chip=chip, tipo="estado_sessao_mudou", dados__motivo="reinterpretacao_do_historico").exists())
        self.assertEqual(m2m.reinterpretar_historico(self.usuario)["alteradas"], 0)               # idempotente


# ═════════════════════════════════════════════════════════════
# Comandos
# ═════════════════════════════════════════════════════════════

class ComandosTests(M2MBase):
    def rodar(self, resposta, *args):
        saida = io.StringIO()
        with mock.patch("services.tim_m2m_service.requests.Session", return_value=Sessao(resposta)):
            call_command("monitorar_m2m", *args, stdout=saida)
        return saida.getvalue()

    def test_sem_token_termina_com_erro_claro_e_sem_traceback(self):
        with override_settings(TIM_M2M_TOKEN=""):
            with self.assertRaises(CommandError) as ctx:
                call_command("monitorar_m2m", stdout=io.StringIO())
        mensagem = str(ctx.exception)
        self.assertIn("Nenhum token configurado", mensagem)
        self.assertIn("TIM_M2M_TOKEN", mensagem)
        mensagem.encode("cp1252")                                                       # o console do Windows aguenta

    def test_ciclo_bem_sucedido_imprime_resumo_so_com_cp1252(self):
        self.chip(1, nome="Pivô — 3 ✓ 🚜")
        saida = self.rodar(roteador())
        self.assertIn("Chips consultados: 1", saida)
        saida.encode("cp1252")

    def test_falha_de_autenticacao_vira_codigo_de_saida_diferente_de_zero(self):
        self.chip(1)
        with self.assertRaises(CommandError) as ctx:
            self.rodar(roteador(status=401))
        self.assertIn("recusou a credencial", str(ctx.exception))
        with self.assertRaises(CommandError) as ctx:                                    # e o próximo tique também avisa
            self.rodar(roteador())
        self.assertIn("parado", str(ctx.exception))

    def test_pausa_por_limite_nao_e_erro_do_agendador(self):
        self.chip(1)
        saida = self.rodar(lambda url: Resp(429, headers={"Retry-After": "600"}))
        self.assertIn("pausadas até", saida)
        saida = self.rodar(roteador())                                                  # segue bloqueado
        self.assertIn("pausadas até", saida)

    def test_todas_as_consultas_falhando_e_erro(self):
        self.chip(1)
        with self.assertRaises(CommandError) as ctx:
            self.rodar(roteador(status=500))
        self.assertIn("Todas as 1 consultas falharam", str(ctx.exception))

    def test_tique_e_gravado_e_validado(self):
        self.rodar(roteador(), "--tique", "5")
        self.assertEqual(M2MIntegracao.get().tique_minutos, 5)
        with self.assertRaises(CommandError):
            call_command("monitorar_m2m", "--tique", "0", stdout=io.StringIO())

    def test_agendar_m2m_registra_o_monitorar_com_o_tique(self):
        from ProjetoEstoque.management.commands import agendar_m2m
        self.assertEqual(agendar_m2m.NOME_TAREFA, "ControleEstoque_MonitorarM2M")
        chamadas = []
        comando = agendar_m2m.Command(stdout=io.StringIO())
        comando._schtasks = lambda args: (chamadas.append(args) or (0, "", ""))
        comando._criar(15)
        criar = next(a for a in chamadas if a[0] == "/Create")
        tr = criar[criar.index("/TR") + 1]
        self.assertIn("monitorar_m2m --tique 15", tr)
        self.assertEqual(criar[criar.index("/TN") + 1], "ControleEstoque_MonitorarM2M")
        self.assertEqual(criar[criar.index("/MO") + 1], "15")
        with self.assertRaises(CommandError):
            comando._criar(0)

    def test_agendar_m2m_executar_roda_o_monitorar(self):
        with mock.patch("django.core.management.call_command") as chamada:
            from ProjetoEstoque.management.commands import agendar_m2m
            agendar_m2m.Command().handle(acao="executar", intervalo=15)
        chamada.assert_called_once_with("monitorar_m2m")


# ═════════════════════════════════════════════════════════════
# Permissões e grupos (migrations 0189 e 0191)
# ═════════════════════════════════════════════════════════════

class PermissoesTests(TestCase):
    """Os dois grupos e as 10 permissões criados pela 0191 (o mapeamento antigo -> novo, em
    `test_frota.MigracaoDePermissoesTests`)."""

    VER = {"ver_frota", "ver_chips", "consultar_conectividade", "ver_historico_frota"}
    OPERAR = {"editar_frota", "importar_chips", "vincular_chips", "transferir_chips", "tratar_ocorrencias"}

    def codigos(self, grupo):
        return set(
            grupo.permissions.filter(content_type__app_label="ProjetoEstoque").values_list("codename", flat=True)
        )

    def test_as_dez_permissoes_novas_existem_e_as_tres_antigas_nao(self):
        codigos = set(
            Permission.objects.filter(content_type__app_label="ProjetoEstoque").values_list("codename", flat=True)
        )
        self.assertTrue(self.VER | self.OPERAR | {"administrar_frota_config"} <= codigos)
        self.assertFalse({"ver_m2m", "operar_m2m", "configurar_m2m"} & codigos)

    def test_grupo_frotas_tem_as_nove_e_nao_tem_administrar(self):
        self.assertEqual((GRUPO_FROTAS, GRUPO_FROTAS_CONSULTA), ("Frotas e Conectividade", "Frotas — Consulta"))
        grupo = Group.objects.get(name=GRUPO_FROTAS)
        self.assertEqual(self.codigos(grupo), self.VER | self.OPERAR)
        self.assertFalse(Group.objects.filter(name="Monitoramento M2M").exists())     # foi renomeado

    def test_grupo_de_consulta_so_le(self):
        self.assertEqual(self.codigos(Group.objects.get(name=GRUPO_FROTAS_CONSULTA)), self.VER)

    def test_quem_esta_no_grupo_opera_mas_nao_administra(self):
        usuario = User.objects.create_user("operador_m2m", password="x")
        usuario.groups.add(Group.objects.get(name=GRUPO_FROTAS))
        usuario = User.objects.get(pk=usuario.pk)
        for codigo in self.VER | self.OPERAR:
            self.assertTrue(usuario.has_perm(f"ProjetoEstoque.{codigo}"), codigo)
        self.assertFalse(usuario.has_perm("ProjetoEstoque.administrar_frota_config"))
        sem_grupo = User.objects.create_user("curioso", password="x")
        self.assertFalse(sem_grupo.has_perm("ProjetoEstoque.ver_chips"))

    def test_quem_so_esta_no_grupo_de_consulta_le_e_nao_opera(self):
        usuario = User.objects.create_user("leitor_m2m", password="x")
        usuario.groups.add(Group.objects.get(name=GRUPO_FROTAS_CONSULTA))
        usuario = User.objects.get(pk=usuario.pk)
        self.assertTrue(usuario.has_perm("ProjetoEstoque.ver_chips"))
        for codigo in self.OPERAR | {"administrar_frota_config"}:
            self.assertFalse(usuario.has_perm(f"ProjetoEstoque.{codigo}"), codigo)

    def test_superusuario_tem_todas(self):
        admin = User.objects.create_superuser("super_m2m", "s@s.com", "x")
        for p in self.VER | self.OPERAR | {"administrar_frota_config"}:
            self.assertTrue(admin.has_perm(f"ProjetoEstoque.{p}"))

    def test_admin_dos_modelos_do_modulo_e_somente_leitura(self):
        from django.contrib import admin as django_admin
        from django.test import RequestFactory

        from ProjetoEstoque.models import M2MChip, M2MConsulta, M2MEvento, M2MIntegracao, M2MOcorrencia

        super_user = User.objects.create(username="super_m2m_leitura", is_superuser=True, is_staff=True)
        requisicao = RequestFactory().get("/")
        requisicao.user = super_user
        for modelo in (M2MConsulta, M2MEvento, M2MChip, M2MOcorrencia, M2MIntegracao):
            with self.subTest(modelo=modelo.__name__):
                ma = django_admin.site._registry[modelo]
                self.assertTrue(ma.has_view_permission(requisicao))
                self.assertFalse(ma.has_add_permission(requisicao))
                self.assertFalse(ma.has_change_permission(requisicao))
                self.assertFalse(ma.has_delete_permission(requisicao))
