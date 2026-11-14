"""
Sessões de dados do chip (o que a aba "Dados" do portal da Saitro mostra): leitura, gravação,
interpretação e tela.

Os eventos abaixo são os REAIS do primeiro chip online (13/11/2026, linha 5586998462939) e os números
do portal (703,70 KB, 14,50 MB, 15,19 MB, 26,32 KB) são os que a tela tem de reproduzir. O que o teste
fixa: o resumo do SIM Card dizia `nunca_conectou` / `0,00 KB` com o chip online e consumindo, e o sistema
tem de seguir as SESSÕES, não o resumo.
"""

import importlib
from datetime import date, datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from django.apps import apps as apps_globais
from django.contrib.auth.models import User
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from ProjetoEstoque.models import M2MChip, M2MConsulta, M2MEvento, M2MIntegracao, M2MSessaoDados
from ProjetoEstoque.templatetags import m2m_tags
from ProjetoEstoque.tests.test_m2m import M2MBase, Resp, iccid_valido, registro_gsim
from services import m2m_dados_service as dados
from services import m2m_interpretacao as interp
from services import m2m_monitoramento_service as m2m

SP = ZoneInfo("America/Sao_Paulo")
AGORA = datetime(2026, 11, 13, 22, 10, tzinfo=SP)          # a hora em que o chip foi conferido na plataforma
HOJE = date(2026, 11, 13)

EV_FECHADA = {
    "id_evento": 57991779402,
    "dt_inicio": {"date": "2026-11-13 21:46:47.000000", "timezone_type": 3, "timezone": "America/Sao_Paulo"},
    "dt_fim": "2026-11-13 21:47:16-03", "duracao_segundos": 29, "ip": "100.93.86.212",
    "upload": 10115, "download": 16838, "total": 26953, "apn": "m2m.pc.br", "msisdn": "5586998462939",
    "imsi": "724548701167463", "location_info": "0x8227f42093a727f4200773a30f", "tecnologia": "EUTRAN",
}
EV_ABERTA = {
    "id_evento": 346839426031,
    "dt_inicio": {"date": "2026-11-13 21:47:22.000000", "timezone_type": 3, "timezone": "America/Sao_Paulo"},
    "dt_fim": None, "duracao_segundos": 900, "ip": "100.77.34.181",
    "upload": 720593, "download": 15207610, "total": 15928203, "apn": "m2m.pc.br", "msisdn": "5586998462939",
    "imsi": "724548701167463", "location_info": "0x8227f42093a727f4200773a30f", "tecnologia": "EUTRAN",
}


def evento(id_evento, dia, hora="10:00:00", **extra):
    """Um evento fechado num dia qualquer (`dia`: 'AAAA-MM-DD')."""
    base = {
        "id_evento": id_evento, "dt_inicio": {"date": f"{dia} {hora}.000000", "timezone": "America/Sao_Paulo"},
        "dt_fim": f"{dia} 10:30:00-03", "duracao_segundos": 1800, "ip": "100.1.1.1", "upload": 100, "download": 900, "total": 1000,
        "apn": "m2m.pc.br", "tecnologia": "EUTRAN", "location_info": "x",
    }
    base.update(extra)
    return base


def roteador_com_eventos(por_dia=None, *, conexao="nunca_conectou", consumo="0,00 KB", status_eventos=200, falha_no_dia=None):
    """O resumo do SIM Card (sempre atrasado, como a API real) e os eventos por dia. `falha_no_dia`:
    `{'AAAA-MM-DD': status}` faz só aquele dia responder com erro."""
    por_dia = por_dia or {}
    falha_no_dia = falha_no_dia or {}

    def fn(url):
        if "/consumo/dados/" in url:
            dia = url.rstrip("/").rsplit("/", 1)[1]
            status = falha_no_dia.get(dia, status_eventos)
            if status != 200:
                return Resp(status, json={"erro": "x"})
            objetos = por_dia.get(dia)
            if not objetos:
                return Resp(200, json={"msg": "sem_historico_no_periodo"})
            meta = {"pagina": 1, "anterior": None, "proxima": None, "total_registros_pagina": len(objetos), "total_registros": len(objetos)}
            return Resp(200, json={"meta": meta, "objetos": objetos})
        return Resp(200, json=[registro_gsim(iccid_valido(1), "5586998462939", conexao, consumo)])
    return fn


# ═════════════════════════════════════════════════════════════
# Leitura do que a plataforma devolve
# ═════════════════════════════════════════════════════════════

class LerEventoTests(SimpleTestCase):
    def test_evento_fechado_como_a_plataforma_manda(self):
        e = dados.ler_evento(EV_FECHADA)
        self.assertEqual(e["id_evento"], 57991779402)
        self.assertEqual(e["inicio"], datetime(2026, 11, 13, 21, 46, 47, tzinfo=SP))
        self.assertEqual(e["fim"], datetime(2026, 11, 13, 21, 47, 16, tzinfo=SP))        # "-03" no texto
        self.assertEqual((e["duracao_s"], e["ip"], e["apn"], e["tecnologia"]), (29, "100.93.86.212", "m2m.pc.br", "EUTRAN"))
        self.assertEqual((e["upload_bytes"], e["download_bytes"], e["total_bytes"]), (10115, 16838, 26953))

    def test_sessao_em_andamento_nao_tem_fim(self):
        e = dados.ler_evento(EV_ABERTA)
        self.assertIsNone(e["fim"])
        self.assertEqual(e["total_bytes"], 15928203)

    def test_sem_total_vale_a_soma_de_upload_e_download(self):
        e = dados.ler_evento({**EV_FECHADA, "total": None})
        self.assertEqual(e["total_bytes"], 10115 + 16838)

    def test_evento_sem_id_ou_sem_inicio_nao_e_confiavel(self):
        for ruim in ({**EV_FECHADA, "id_evento": None}, {**EV_FECHADA, "dt_inicio": None}, {**EV_FECHADA, "dt_inicio": "ontem"}, "texto", None):
            with self.subTest(ruim=ruim):
                self.assertIsNone(dados.ler_evento(ruim))

    def test_bytes_invalidos_nao_viram_numero_negativo_nem_booleano(self):
        e = dados.ler_evento({**EV_FECHADA, "upload": -5, "download": True, "total": "abc"})
        self.assertEqual((e["upload_bytes"], e["download_bytes"], e["total_bytes"]), (0, 0, 0))

    def test_inicio_sem_fuso_usa_o_fuso_informado_e_na_falta_dele_o_do_sistema(self):
        a = dados.ler_evento({**EV_FECHADA, "dt_inicio": {"date": "2026-11-13 12:00:00", "timezone": "UTC"}})
        self.assertEqual(a["inicio"], datetime(2026, 11, 13, 12, 0, tzinfo=ZoneInfo("UTC")))
        b = dados.ler_evento({**EV_FECHADA, "dt_inicio": "2026-11-13 12:00:00"})
        self.assertEqual(b["inicio"], datetime(2026, 11, 13, 12, 0, tzinfo=SP))


# ═════════════════════════════════════════════════════════════
# Interpretação (funções puras)
# ═════════════════════════════════════════════════════════════

RESUMO = registro_gsim(iccid_valido(1), "5586998462939")      # `nunca_conectou`, `0,00 KB`


def eventos(abertas=(), ultima=None, consultado="2026-11-13T22:10:00-03:00"):
    def linha(ev):
        return {"id": 1, "inicio": ev["inicio"], "fim": ev.get("fim"), "duracao_s": ev["duracao_s"], "total_bytes": 1, "ip": ev.get("ip", ""), "apn": ev.get("apn", "")}
    return {"fonte": "sessoes_de_dados", "consultado_em": consultado, "abertas": [linha(a) for a in abertas], "ultima": linha(ultima) if ultima else None}


ABERTA = {"inicio": "2026-11-13T21:47:22-03:00", "fim": None, "duracao_s": 900, "ip": "100.77.34.181", "apn": "m2m.pc.br"}
FECHADA = {"inicio": "2026-11-13T21:46:47-03:00", "fim": "2026-11-13T21:47:16-03:00", "duracao_s": 29}


class InterpretacaoDeSessaoTests(SimpleTestCase):
    MAPA = interp.MAPEAMENTO_GSIM

    def sessao(self, ev, mapa=None, resumo=RESUMO):
        return interp.interpretar_sessao({**resumo, "_eventos": ev}, self.MAPA if mapa is None else mapa)

    def test_sessao_aberta_vale_online_mesmo_com_o_resumo_dizendo_nunca_conectou(self):
        r = self.sessao(eventos([ABERTA], FECHADA))
        self.assertEqual((r.estado, r.estado_bruto), (interp.ATIVA, "online"))
        campos = {c["caminho"]: c["valor"] for c in r.campos_exibir}
        self.assertEqual(campos["sessao_aberta_desde"], "13/11/2026 21:47")
        self.assertEqual(campos["ip_da_sessao"], "100.77.34.181")

    def test_sem_sessao_aberta_mas_com_sessao_passada_e_offline_e_nao_nunca_conectou(self):
        r = self.sessao(eventos([], FECHADA))
        self.assertEqual((r.estado, r.estado_bruto), (interp.SEM_SESSAO, "offline"))
        self.assertEqual({c["caminho"]: c["valor"] for c in r.campos_exibir}["ultima_sessao_ate"], "13/11/2026 21:47")

    def test_nenhuma_sessao_lida_fica_como_o_resumo_disse(self):
        r = self.sessao(eventos([], None))
        self.assertEqual((r.estado, r.estado_bruto), (interp.SEM_SESSAO, "nunca_conectou"))

    def test_resumo_online_continua_ativa_mesmo_sem_sessao_aberta(self):
        r = self.sessao(eventos([], FECHADA), resumo={**RESUMO, "conexao": "online"})
        self.assertEqual((r.estado, r.estado_bruto), (interp.ATIVA, "online"))

    def test_sessao_aberta_que_a_plataforma_nao_avanca_ha_mais_de_2h_nao_e_online(self):
        # a duração parou em 900 s (21:47 + 15 min = 22:02): às 00:10 já são mais de 2 h sem avanço
        r = self.sessao(eventos([ABERTA], FECHADA, consultado="2026-11-14T00:10:00-03:00"))
        self.assertEqual((r.estado, r.estado_bruto), (interp.SEM_SESSAO, "offline"))

    def test_sessao_aberta_dentro_da_margem_ainda_e_online(self):
        r = self.sessao(eventos([ABERTA], FECHADA, consultado="2026-11-13T23:50:00-03:00"))
        self.assertEqual(r.estado, interp.ATIVA)

    def test_o_estado_das_sessoes_nao_depende_do_mapeamento_estar_configurado(self):
        r = self.sessao(eventos([ABERTA]), mapa={})
        self.assertEqual((r.estado, r.estado_bruto), (interp.ATIVA, "online"))

    def test_sem_o_bloco_de_sessoes_so_vale_o_resumo(self):
        r = interp.interpretar_sessao(RESUMO, self.MAPA)
        self.assertEqual((r.estado, r.estado_bruto), (interp.SEM_SESSAO, "nunca_conectou"))


class InterpretacaoDeConsumoTests(SimpleTestCase):
    def periodo(self, total, **extra):
        return {**RESUMO, "_dados_periodo": {"inicio": "2026-11-01T00:00:00-03:00", "fim": "2026-11-13T22:10:00-03:00", "total_bytes": total, **extra}}

    def test_o_volume_e_a_soma_exata_em_bytes_e_nao_o_texto_do_resumo(self):
        r = interp.interpretar_consumo(self.periodo(15955156), interp.MAPEAMENTO_GSIM)         # o resumo dizia "0,00 KB"
        self.assertEqual((r.volume_bytes, r.valor_original, r.unidade_original), (15955156, "15955156", "B"))
        self.assertEqual((r.periodo_inicio.day, r.periodo_fim.hour), (1, 22))

    def test_sem_mapeamento_o_volume_das_sessoes_continua_valendo(self):
        self.assertEqual(interp.interpretar_consumo(self.periodo(5), {}).volume_bytes, 5)

    def test_total_invalido_cai_no_resumo(self):
        for ruim in (True, -1, "7", None):
            with self.subTest(ruim=ruim):
                r = interp.interpretar_consumo(self.periodo(ruim), interp.MAPEAMENTO_GSIM)
                self.assertEqual(r.volume_bytes, 0)                       # o "0,00 KB" do resumo

    def test_a_base_do_gsim_e_1024_como_o_portal(self):
        self.assertEqual(interp.MAPEAMENTO_GSIM["consumo"]["base"], 1024)
        r = interp.interpretar_consumo({"servicos": {"dados": {"consumo": "15,19 MB"}}}, interp.MAPEAMENTO_GSIM)
        self.assertEqual(r.volume_bytes, round(15.19 * 1024 * 1024))


class FormatoDoPortalTests(SimpleTestCase):
    def test_os_numeros_da_tela_do_portal_saem_dos_bytes_em_base_1024(self):
        casos = {720593: "703,70 KB", 15207610: "14,50 MB", 15928203: "15,19 MB", 26953: "26,32 KB", 10115: "9,88 KB", 16838: "16,44 KB"}
        for bytes_, esperado in casos.items():
            with self.subTest(bytes=bytes_):
                self.assertEqual(m2m_tags.m2m_bytes_fixo(bytes_, 1024), esperado)

    def test_em_base_1000_o_mesmo_total_daria_outro_numero(self):
        self.assertEqual(m2m_tags.m2m_bytes_fixo(15928203, 1000), "15,93 MB")

    def test_duracao_no_formato_do_portal(self):
        for segundos, esperado in ((29, "29s"), (900, "15m"), (929, "15m, 29s"), (3725, "1h, 2m, 5s"), (0, "0s"), (None, "—"), (-1, "—")):
            with self.subTest(segundos=segundos):
                self.assertEqual(m2m_tags.m2m_duracao(segundos), esperado)


# ═════════════════════════════════════════════════════════════
# Quais dias ler
# ═════════════════════════════════════════════════════════════

class DiasALerTests(M2MBase):
    def chip_(self, **campos):
        chip = self.chip(1)
        M2MChip.objects.filter(pk=chip.pk).update(**campos)
        chip.refresh_from_db()
        return chip

    def test_primeira_leitura_traz_hoje_e_comeca_o_historico_do_mes(self):
        self.assertEqual(dados.dias_a_ler(self.chip_(), HOJE), [HOJE, date(2026, 11, 12), date(2026, 11, 11), date(2026, 11, 10)])

    def test_em_regime_e_so_hoje(self):
        chip = self.chip_(dados_desde=date(2026, 11, 1), dados_sincronizados_em=AGORA - timedelta(hours=1))
        self.assertEqual(dados.dias_a_ler(chip, HOJE), [HOJE])

    def test_na_virada_do_dia_le_tambem_ontem(self):
        chip = self.chip_(dados_desde=date(2026, 11, 1), dados_sincronizados_em=AGORA - timedelta(days=1))
        self.assertEqual(dados.dias_a_ler(chip, HOJE), [HOJE, date(2026, 11, 12)])

    def test_sistema_parado_recupera_os_dias_perdidos_ate_o_teto(self):
        chip = self.chip_(dados_desde=date(2026, 11, 1), dados_sincronizados_em=AGORA - timedelta(days=4))
        self.assertEqual(dados.dias_a_ler(chip, HOJE), [HOJE, *[HOJE - timedelta(days=k) for k in (1, 2, 3, 4)]])

    def test_sessao_que_ficou_aberta_antes_de_hoje_manda_reler_o_dia_dela(self):
        chip = self.chip_(dados_desde=date(2026, 11, 1), dados_sincronizados_em=AGORA - timedelta(hours=1))
        M2MSessaoDados.objects.create(chip=chip, id_evento=1, inicio=datetime(2026, 11, 11, 23, 0, tzinfo=SP), fim=None, duracao_s=900)
        self.assertEqual(dados.dias_a_ler(chip, HOJE), [HOJE, date(2026, 11, 11)])

    def test_no_dia_1_nao_ha_historico_do_mes_para_baixar(self):
        self.assertEqual(dados.dias_a_ler(self.chip_(), date(2026, 11, 1)), [date(2026, 11, 1)])

    def test_historico_do_mes_nao_passa_do_dia_1(self):
        chip = self.chip_(dados_desde=date(2026, 11, 3), dados_sincronizados_em=AGORA - timedelta(hours=1))
        self.assertEqual(dados.dias_a_ler(chip, HOJE), [HOJE, date(2026, 11, 2), date(2026, 11, 1)])


# ═════════════════════════════════════════════════════════════
# Atualização de ponta a ponta
# ═════════════════════════════════════════════════════════════

class SessoesDeDadosTests(M2MBase):
    def setUp(self):
        super().setUp()
        # Em produção o mapeamento do GSIM já está gravado (migration 0199); sem ele o resumo seria "não interpretado".
        M2MIntegracao.objects.filter(pk=1).update(dados_por_eventos=True, mapeamento=interp.MAPEAMENTO_GSIM)
        agora = mock.patch("django.utils.timezone.now", return_value=AGORA)
        agora.start()
        self.addCleanup(agora.stop)

    def chip_online(self, **extra):
        chip = self.chip(1, numero_linha="5586998462939", **extra)
        return self.vincular(chip)

    def rodar(self, chip, roteador, **kw):
        return self.atualizar(chip, roteador, origem="manual", **kw)

    HOJE_ISO = "2026-11-13"

    def test_chip_online_aparece_com_sessao_ativa_e_consumo_do_portal(self):
        chip = self.chip_online()
        resultado, sessao = self.rodar(chip, roteador_com_eventos({self.HOJE_ISO: [EV_FECHADA, EV_ABERTA]}))
        self.assertTrue(resultado.ok, resultado.resumo())
        self.assertEqual(chip.sessao_estado, "ativa")
        self.assertEqual(chip.consumo_bytes, 26953 + 15928203)                   # 15.955.156 bytes = 15,22 MB no portal
        self.assertEqual(M2MSessaoDados.objects.filter(chip=chip).count(), 2)
        self.assertEqual(chip.sessao_consulta.estado_interpretado, "ativa")
        self.assertEqual(chip.sessao_consulta.estado_bruto, "online")
        self.assertEqual(chip.consumo_consulta.volume_bytes, 26953 + 15928203)

    def test_o_resumo_atrasado_nao_derruba_o_que_as_sessoes_mostram(self):
        """O resumo diz `nunca_conectou` e `0,00 KB`; o payload guardado o mantém CRU e acrescenta as sessões."""
        chip = self.chip_online()
        self.rodar(chip, roteador_com_eventos({self.HOJE_ISO: [EV_ABERTA]}, conexao="nunca_conectou", consumo="0,00 KB"))
        payload = chip.sessao_consulta.payload
        self.assertEqual((payload["conexao"], payload["servicos"]["dados"]["consumo"]), ("nunca_conectou", "0,00 KB"))
        self.assertEqual(payload["_eventos"]["abertas"][0]["id"], EV_ABERTA["id_evento"])
        self.assertEqual(chip.consumo_consulta.payload["_dados_periodo"]["total_bytes"], 15928203)

    def test_mudanca_de_estado_fica_no_historico_do_chip(self):
        chip = self.chip_online()
        self.rodar(chip, roteador_com_eventos({self.HOJE_ISO: [EV_ABERTA]}))
        mudou = M2MEvento.objects.filter(chip=chip, tipo=M2MEvento.Tipo.ESTADO_SESSAO_MUDOU).latest("pk")
        self.assertEqual((mudou.dados["de"], mudou.dados["para"], mudou.dados["estado_bruto"]), ("nao_consultado", "ativa", "online"))

    def test_ler_de_novo_atualiza_a_sessao_aberta_e_nao_duplica(self):
        chip = self.chip_online()
        self.rodar(chip, roteador_com_eventos({self.HOJE_ISO: [EV_ABERTA]}))
        depois = {**EV_ABERTA, "duracao_segundos": 1800, "total": 20_000_000, "upload": 1_000_000, "download": 19_000_000,
                  "dt_fim": "2026-11-13 22:17:22-03"}
        M2MChip.objects.filter(pk=chip.pk).update(ultima_tentativa_em=None)
        self.rodar(chip, roteador_com_eventos({self.HOJE_ISO: [depois]}))
        sessao = M2MSessaoDados.objects.get(id_evento=EV_ABERTA["id_evento"])
        self.assertEqual((sessao.duracao_s, sessao.total_bytes), (1800, 20_000_000))
        self.assertIsNotNone(sessao.fim)
        self.assertEqual(M2MSessaoDados.objects.count(), 1)
        self.assertEqual(chip.sessao_estado, "sem_sessao")                 # a sessão fechou: agora é offline, e não "nunca conectou"
        self.assertEqual(chip.sessao_consulta.estado_bruto, "offline")
        self.assertEqual(chip.consumo_bytes, 20_000_000)

    def test_dia_sem_sessao_e_dia_lido_e_nao_falha(self):
        chip = self.chip_online()
        resultado, _ = self.rodar(chip, roteador_com_eventos({}))
        self.assertTrue(resultado.ok)
        self.assertEqual((chip.sessao_estado, chip.consumo_bytes), ("sem_sessao", 0))
        self.assertEqual(chip.sessao_consulta.estado_bruto, "nunca_conectou")
        self.assertEqual(chip.dados_sincronizados_em, AGORA)

    def test_falha_na_leitura_de_hoje_falha_a_consulta_inteira_e_preserva_o_ultimo_dado(self):
        chip = self.chip_online()
        self.rodar(chip, roteador_com_eventos({self.HOJE_ISO: [EV_ABERTA]}))
        M2MChip.objects.filter(pk=chip.pk).update(ultima_tentativa_em=None)
        resultado, _ = self.rodar(chip, roteador_com_eventos({}, status_eventos=500))
        self.assertTrue(resultado.houve_falha)
        self.assertTrue(all(not c.valida for c in resultado.consultas))
        self.assertIn("sessões de dados", resultado.consultas[0].mensagem_erro)
        self.assertEqual(chip.sessao_estado, "ativa")                       # o resumo atrasado NÃO derruba o estado
        self.assertEqual(chip.consumo_bytes, 15928203)
        self.assertEqual(chip.falhas_consecutivas, 1)

    def test_falha_num_dia_antigo_so_atrasa_o_historico(self):
        chip = self.chip_online()
        resultado, _ = self.rodar(chip, roteador_com_eventos({self.HOJE_ISO: [EV_ABERTA], "2026-11-11": [evento(11, "2026-11-11")]}, falha_no_dia={"2026-11-12": 500}))
        self.assertTrue(resultado.ok)
        self.assertEqual(chip.sessao_estado, "ativa")
        chip.refresh_from_db()
        self.assertEqual(chip.dados_desde, HOJE)                             # 12/11 falhou: nada de "histórico completo" além dele
        self.assertEqual(M2MSessaoDados.objects.filter(chip=chip).count(), 2)

    def test_429_na_leitura_das_sessoes_interrompe_e_bloqueia_como_qualquer_consulta(self):
        chip = self.chip_online()
        resultado, _ = self.rodar(chip, roteador_com_eventos({}, status_eventos=429))
        self.assertEqual(resultado.interrompida_por, "limite_excedido")
        self.assertIsNotNone(M2MIntegracao.get().bloqueado_ate)
        self.assertEqual(len(resultado.consultas), 1)

    def test_desligada_a_opcao_nada_de_sessoes_e_so_o_resumo(self):
        M2MIntegracao.objects.filter(pk=1).update(dados_por_eventos=False)
        chip = self.chip_online()
        resultado, sessao = self.rodar(chip, roteador_com_eventos({self.HOJE_ISO: [EV_ABERTA]}))
        self.assertTrue(resultado.ok)
        self.assertEqual(chip.sessao_estado, "sem_sessao")
        self.assertFalse(any("/consumo/dados/" in u for u in sessao.urls))
        self.assertEqual(M2MSessaoDados.objects.count(), 0)

    def test_o_historico_do_mes_e_baixado_aos_poucos_ate_o_dia_1(self):
        chip = self.chip_online()
        rot = roteador_com_eventos({f"2026-11-{d:02d}": [evento(d, f"2026-11-{d:02d}")] for d in range(1, 14)})
        passos = []
        for _ in range(4):
            M2MChip.objects.filter(pk=chip.pk).update(ultima_tentativa_em=None)
            self.rodar(chip, rot)
            passos.append(chip.dados_desde)
        self.assertEqual(passos, [date(2026, 11, 10), date(2026, 11, 7), date(2026, 11, 4), date(2026, 11, 1)])
        self.assertEqual(M2MSessaoDados.objects.filter(chip=chip).count(), 13)
        self.assertEqual(chip.consumo_bytes, 13 * 1000)
        self.assertTrue(chip.consumo_consulta.payload["_dados_periodo"]["completo"])

    def test_enquanto_o_historico_nao_completa_o_consumo_avisa_que_e_parcial(self):
        chip = self.chip_online()
        self.rodar(chip, roteador_com_eventos({self.HOJE_ISO: [EV_ABERTA]}))
        periodo = chip.consumo_consulta.payload["_dados_periodo"]
        self.assertFalse(periodo["completo"])
        self.assertEqual(periodo["dados_desde"], "2026-11-10")

    def test_o_consumo_do_mes_ignora_sessao_de_mes_anterior(self):
        chip = self.chip_online()
        M2MSessaoDados.objects.create(chip=chip, id_evento=99, inicio=datetime(2026, 10, 31, 23, 0, tzinfo=SP), fim=datetime(2026, 10, 31, 23, 30, tzinfo=SP), total_bytes=5_000_000)
        self.rodar(chip, roteador_com_eventos({self.HOJE_ISO: [EV_ABERTA]}))
        self.assertEqual(chip.consumo_bytes, 15928203)

    def test_ritmo_entre_leituras_respeita_o_limite_de_requisicoes(self):
        chip = self.chip_online()
        self.rodar(chip, roteador_com_eventos({}))
        esperas = [c.args[0] for c in self.sleep.call_args_list if c.args and isinstance(c.args[0], float) and c.args[0] > 0.5]
        self.assertGreaterEqual(len(esperas), 3)                              # 4 dias na 1ª leitura = 3 intervalos entre eles
        self.assertTrue(all(e <= m2m.pausa_efetiva_s(M2MIntegracao.get()) + 0.01 for e in esperas))

    def test_evento_de_outro_chip_com_o_mesmo_id_nao_e_roubado(self):
        outro = self.chip(2)
        M2MSessaoDados.objects.create(chip=outro, id_evento=EV_ABERTA["id_evento"], inicio=AGORA - timedelta(days=2), total_bytes=1)
        chip = self.chip_online()
        self.rodar(chip, roteador_com_eventos({self.HOJE_ISO: [EV_ABERTA]}))
        self.assertEqual(M2MSessaoDados.objects.get(id_evento=EV_ABERTA["id_evento"]).chip_id, outro.pk)

    def test_reinterpretar_o_historico_mantem_o_que_as_sessoes_disseram(self):
        chip = self.chip_online()
        self.rodar(chip, roteador_com_eventos({self.HOJE_ISO: [EV_ABERTA]}))
        m2m.reinterpretar_historico(self.usuario)
        consulta = M2MConsulta.objects.filter(chip=chip, tipo="sessao").latest("pk")
        self.assertEqual((consulta.estado_interpretado, consulta.estado_bruto), ("ativa", "online"))


# ═════════════════════════════════════════════════════════════
# Tela
# ═════════════════════════════════════════════════════════════

class TelaDeSessoesTests(M2MBase):
    def setUp(self):
        super().setUp()
        M2MIntegracao.objects.filter(pk=1).update(dados_por_eventos=True, mapeamento=interp.MAPEAMENTO_GSIM)
        agora = mock.patch("django.utils.timezone.now", return_value=AGORA)
        agora.start()
        self.addCleanup(agora.stop)
        self.admin = User.objects.create_superuser("sessoes_admin", "a@a.com", "x")
        self.client = Client()
        self.client.force_login(self.admin)
        self.chip_ = self.vincular(self.chip(1, numero_linha="5586998462939"))

    def abrir(self):
        r = self.client.get(reverse("m2m_chip_detalhe", args=[self.chip_.pk]))
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def test_a_tela_reproduz_a_tabela_do_portal(self):
        self.atualizar(self.chip_, roteador_com_eventos({"2026-11-13": [EV_FECHADA, EV_ABERTA]}), origem="manual")
        html = self.abrir()
        for esperado in ("Sessões de dados", "13/11/2026 21:47:22", "13/11/2026 21:47:16", "<b>Online</b>", "15m", "29s", "100.77.34.181",
                         "703,70 KB", "14,50 MB", "15,19 MB", "26,32 KB", "9,88 KB", "16,44 KB", "100.93.86.212"):
            self.assertIn(esperado, html)
        self.assertIn("Total no mês, desde 01/11 (2 sessões)", html)

    def test_total_do_mes_e_o_consumo_do_chip_usam_a_mesma_conta(self):
        self.atualizar(self.chip_, roteador_com_eventos({"2026-11-13": [EV_FECHADA, EV_ABERTA]}), origem="manual")
        html = self.abrir()
        self.assertIn("15,22 MB", html)                               # 15.955.156 bytes em base 1024
        self.assertIn("Soma das sessões de dados", html)
        self.assertIn("ainda está sendo baixado", html)               # só 4 dias lidos: o total do mês é parcial e a tela diz

    def test_chip_sem_sessao_lida_mostra_o_estado_vazio_e_nao_uma_tabela_vazia(self):
        html = self.abrir()
        self.assertIn("Nenhuma sessão de dados lida", html)
        self.assertNotIn("m2m-tabela--sessoes", html)

    def test_sessao_aberta_que_parou_de_avancar_nao_e_chamada_de_online_na_tabela(self):
        M2MSessaoDados.objects.create(chip=self.chip_, id_evento=5, inicio=AGORA - timedelta(days=1), fim=None, duracao_s=900)
        html = self.abrir()
        self.assertNotIn("<b>Online</b>", html)


class MigracaoDaBaseTests(TestCase):
    def test_so_o_mapeamento_do_gsim_que_ainda_esta_em_1000_vai_para_1024(self):
        modulo = importlib.import_module("ProjetoEstoque.migrations.0203_m2m_sessoes_de_dados")
        cfg = M2MIntegracao.get()
        casos = [
            ({"consumo": {"campo_volume": "servicos.dados.consumo", "unidade_no_valor": True, "base": 1000}}, 1024),
            ({"consumo": {"campo_volume": "servicos.dados.consumo", "unidade_no_valor": True, "base": 1024}}, 1024),
            ({"consumo": {"campo_volume": "uso.total", "unidade_fixa": "MB", "base": 1000}}, 1000),      # mapeamento de outra API: intacto
            ({}, None),
        ]
        for mapa, esperado in casos:
            with self.subTest(mapa=mapa):
                M2MIntegracao.objects.filter(pk=cfg.pk).update(mapeamento=mapa)
                modulo.base_do_portal(apps_globais, None)
                depois = M2MIntegracao.objects.get(pk=cfg.pk).mapeamento
                self.assertEqual((depois.get("consumo") or {}).get("base"), esperado)
