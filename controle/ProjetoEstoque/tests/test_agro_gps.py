"""A localização GPS dos veículos (`services/agro_localizacao_service.py`) e o mapa do Agro.

Os critérios de aceite 5, 6 e 7 do pedido de 14/11, como teste. O fato que molda tudo: NÃO EXISTE fonte de GPS de veículo
ligada ao sistema e a TIM não informa posição. Então o que se prova é (a) que a estrutura para a fonte futura funciona —
histórico imutável, idempotência, validação, estados e limites configuráveis — e (b) que, sem fonte, nada é inventado:
veículo sem coordenada fica "indisponível", não vira marcador e o chip não empresta posição.

  • Cenário 5 — chip em veículo com GPS: aparece no mapa, o detalhe do chip leva ao veículo, a TRANSFERÊNCIA leva a
    localização atual junto com o vínculo e o histórico do veículo anterior permanece;
  • Cenário 6 — veículo sem fonte: informa a ausência, nenhum marcador em posição fictícia;
  • Cenário 7 — `/agro/mapa/?camada=veiculos&camada=estruturas&camada=enlaces`: as três camadas continuam carregando."""

import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import Permission, User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ProjetoEstoque.models import FrotaEvento, FrotaLocalizacaoConfig, FrotaPosicao, FrotaTipo, FrotaVeiculo, M2MChip
from services import agro_infra_service as infra
from services import agro_localizacao_service as gps
from services import agro_mapa_service as mapa
from services import frota_service
from services import frota_vinculo_service as vinculos

FONTE = "Rastreador de teste"


def usuario(nome, *codenames, superuser=False):
    u = User.objects.create_user(nome, f"{nome}@x.com", "senha-boa-123", is_superuser=superuser)
    for c in codenames:
        u.user_permissions.add(Permission.objects.get(codename=c, content_type__app_label="ProjetoEstoque"))
    return u


def erros(fn, *a, **kw) -> dict:
    try:
        fn(*a, **kw)
    except ValidationError as exc:
        return exc.message_dict
    raise AssertionError("A chamada deveria ter levantado ValidationError.")


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = usuario("gps_admin", superuser=True)
        cls.tipo = FrotaTipo.objects.get_or_create(nome="Trator")[0]

    def veiculo(self, codigo, **extra):
        return frota_service.criar_veiculo({"codigo": codigo, "nome": f"Trator {codigo}", "tipo": self.tipo.pk, **extra}, self.admin)

    def chip(self, n):
        return M2MChip.objects.create(nome=f"Chip {n}", iccid=f"8955100000000{n:06d}"[:19])

    def ponto(self, veiculo, minutos_atras=1, lat="-14,80", lon="-45,60", **extra):
        return gps.registrar_posicao(
            veiculo, lat, lon, fonte=extra.pop("fonte", FONTE), medida_em=timezone.now() - timedelta(minutes=minutos_atras), **extra,
        )


class RegistrarPosicaoTests(Base):
    def test_grava_o_historico_e_atualiza_o_espelho_da_posicao_atual_como_gps(self):
        v = self.veiculo("MQ-1")
        p = self.ponto(v, 3, precisao_m="4,5", velocidade_kmh="12,3", direcao_graus=270)
        v.refresh_from_db()
        self.assertEqual((float(v.latitude), float(v.longitude), v.localizacao_origem), (-14.8, -45.6, "gps"))
        self.assertEqual((p.fonte, float(p.precisao_m), float(p.velocidade_kmh), p.direcao_graus), (FONTE, 4.5, 12.3, 270))
        self.assertEqual(FrotaPosicao.objects.filter(veiculo=v).count(), 1)

    def test_a_mesma_leitura_reenviada_nao_duplica_nem_mexe_em_nada(self):
        v = self.veiculo("MQ-1")
        instante = timezone.now() - timedelta(minutes=5)
        gps.registrar_posicao(v, "-14,80", "-45,60", fonte=FONTE, medida_em=instante)
        gps.registrar_posicao(v, "-14,99", "-45,99", fonte=FONTE, medida_em=instante)       # retentativa com valor diferente: ignorada
        self.assertEqual(FrotaPosicao.objects.filter(veiculo=v).count(), 1)
        v.refresh_from_db()
        self.assertEqual(float(v.latitude), -14.8)

    def test_resposta_atrasada_vai_para_o_historico_mas_nao_volta_o_veiculo_no_tempo(self):
        v = self.veiculo("MQ-1")
        self.ponto(v, 1, "-14,81", "-45,61")
        self.ponto(v, 120, "-14,70", "-45,50")
        v.refresh_from_db()
        self.assertEqual(float(v.latitude), -14.81)
        self.assertEqual(FrotaPosicao.objects.filter(veiculo=v).count(), 2, "a posição antiga é fato e fica no histórico")

    def test_recusa_o_que_nao_e_posicao_valida(self):
        v = self.veiculo("MQ-1")
        agora = timezone.now()
        self.assertIn("América do Sul", erros(gps.registrar_posicao, v, "40,7", "-74,0", fonte=FONTE)["latitude"][0])
        self.assertIn("ponto nulo", erros(gps.registrar_posicao, v, "0", "0", fonte=FONTE)["latitude"][0])
        self.assertIn("latitude", erros(gps.registrar_posicao, v, "", "", fonte=FONTE))
        self.assertIn("fonte", erros(gps.registrar_posicao, v, "-14,8", "-45,6", fonte="  "))
        self.assertIn("futuro", erros(gps.registrar_posicao, v, "-14,8", "-45,6", fonte=FONTE, medida_em=agora + timedelta(hours=2))["medida_em"][0])
        self.assertIn("direcao_graus", erros(gps.registrar_posicao, v, "-14,8", "-45,6", fonte=FONTE, direcao_graus=360))
        self.assertIn("velocidade_kmh", erros(gps.registrar_posicao, v, "-14,8", "-45,6", fonte=FONTE, velocidade_kmh=-1))
        self.assertIn("precisao_m", erros(gps.registrar_posicao, v, "-14,8", "-45,6", fonte=FONTE, precisao_m="abc"))
        self.assertEqual(FrotaPosicao.objects.count(), 0, "nada do que foi recusado pode ter sido gravado")

    def test_o_historico_e_imutavel(self):
        v = self.veiculo("MQ-1")
        p = self.ponto(v)
        p.latitude = 0
        with self.assertRaises(ValidationError):
            p.save()
        with self.assertRaises(ValidationError):
            p.delete()
        with self.assertRaises(ValidationError):
            FrotaPosicao.objects.filter(pk=p.pk).update(fonte="x")
        with self.assertRaises(ValidationError):
            FrotaPosicao.objects.filter(pk=p.pk).delete()

    def test_a_restricao_do_banco_barra_leitura_duplicada_e_coordenada_fora_do_globo(self):
        v = self.veiculo("MQ-1")
        p = self.ponto(v)
        with self.assertRaises(IntegrityError), transaction.atomic():
            FrotaPosicao.objects.create(veiculo=v, latitude=-14, longitude=-45, medida_em=p.medida_em, fonte=FONTE)
        with self.assertRaises(IntegrityError), transaction.atomic():
            FrotaPosicao.objects.create(veiculo=v, latitude=95, longitude=-45, medida_em=timezone.now(), fonte="outra")

    def test_a_posicao_guarda_o_chip_so_quando_era_exatamente_um(self):
        v = self.veiculo("MQ-1")
        sem_chip = self.ponto(v, 30)
        self.assertIsNone(sem_chip.chip_id)
        c1 = self.chip(1)
        vinculos.vincular(c1, v, self.admin, "teste")
        com_um = self.ponto(v, 20)
        self.assertEqual(com_um.chip_id, c1.pk)
        vinculos.vincular(self.chip(2), v, self.admin, "teste")
        com_dois = self.ponto(v, 10)
        self.assertIsNone(com_dois.chip_id, "com dois chips a pergunta \"qual deles?\" não tem resposta: a linha não finge")

    def test_a_primeira_posicao_gps_registra_o_ponto_cadastrado_que_deixou_de_valer(self):
        v = self.veiculo("MQ-1", latitude="-14,70", longitude="-45,50")
        self.ponto(v)
        evento = FrotaEvento.objects.filter(veiculo=v, tipo=FrotaEvento.Tipo.EDICAO).first()
        self.assertEqual(evento.dados["alteracoes"]["latitude"], {"de": "-14.700000", "para": "-14.800000"})
        self.assertEqual(evento.dados["origem"], "gps")

    def test_o_gancho_antigo_continua_funcionando_e_passa_pela_mesma_porta(self):
        v = self.veiculo("MQ-1")
        mapa.registrar_posicao_gps(v, "-14,80", "-45,60", em=timezone.now() - timedelta(minutes=2))
        self.assertEqual(FrotaPosicao.objects.get(veiculo=v).fonte, gps.FONTE_PADRAO_DO_GANCHO_ANTIGO)


class EstadosELimitesTests(Base):
    def test_os_quatro_estados_pelo_limite_configurado(self):
        agora = timezone.now()
        lim = {"minutos_atualizada": 30, "horas_sem_atualizacao": 24}
        estado = lambda origem, em: gps.classificar(origem, em, agora, lim)["estado"]       # noqa: E731
        self.assertEqual(estado("gps", agora - timedelta(minutes=10)), "atualizada")
        self.assertEqual(estado("gps", agora - timedelta(hours=3)), "antiga")
        self.assertEqual(estado("gps", agora - timedelta(hours=30)), "sem_atualizacao")
        self.assertEqual(estado("cadastral", agora - timedelta(days=90)), "cadastral")
        self.assertEqual(estado("", None), "indisponivel")

    def test_coordenada_cadastral_nunca_e_chamada_de_atual_nem_de_gps(self):
        c = gps.classificar("cadastral", timezone.now())
        self.assertNotIn("atualizada", c["estado"])
        self.assertIn("não a posição atual", c["aviso"])
        self.assertFalse(c["desatualizada"])

    def test_gps_antigo_diz_ha_quanto_tempo_e_nao_se_passa_por_posicao_atual(self):
        agora = timezone.now()
        c = gps.classificar("gps", agora - timedelta(hours=3), agora, {"minutos_atualizada": 30, "horas_sem_atualizacao": 24})
        self.assertEqual(c["estado"], "antiga")
        self.assertIn("há 3 h", c["aviso"])
        self.assertIn("pode já ter se movido", c["aviso"])

    def test_gps_sem_atualizacao_recente_continua_sendo_dito_informacao_desatualizada(self):
        agora = timezone.now()
        c = gps.classificar("gps", agora - timedelta(hours=30), agora, {"minutos_atualizada": 30, "horas_sem_atualizacao": 24})
        self.assertTrue(c["desatualizada"])
        self.assertIn("desatualizada", c["rotulo"])

    def test_sem_origem_e_localizacao_indisponivel_e_sem_coordenada(self):
        c = gps.classificar("", None)
        self.assertEqual((c["estado"], c["tem_coordenada"]), ("indisponivel", False))

    def test_o_limite_e_configuravel_e_muda_o_estado(self):
        v = self.veiculo("MQ-1")
        self.ponto(v, 45)
        self.assertEqual(gps.localizacao_do_veiculo(v)["estado"], "antiga")           # padrão: atual só até 30 min
        gps.salvar_configuracao({"minutos_atualizada": 60, "horas_sem_atualizacao": 24}, self.admin)
        self.assertEqual(gps.localizacao_do_veiculo(v)["estado"], "atualizada")

    def test_a_configuracao_valida_e_audita(self):
        e = erros(gps.salvar_configuracao, {"minutos_atualizada": "abc", "horas_sem_atualizacao": "24"}, self.admin)
        self.assertIn("minutos_atualizada", e)
        self.assertIn("minutos_atualizada", erros(gps.salvar_configuracao, {"minutos_atualizada": 0, "horas_sem_atualizacao": 24}, self.admin))
        self.assertIn("horas_sem_atualizacao", erros(gps.salvar_configuracao, {"minutos_atualizada": 120, "horas_sem_atualizacao": 1}, self.admin))
        cfg = gps.salvar_configuracao({"minutos_atualizada": 15, "horas_sem_atualizacao": 12}, self.admin)
        self.assertEqual((cfg.minutos_atualizada, cfg.horas_sem_atualizacao, cfg.atualizado_por), (15, 12, self.admin))
        from ProjetoEstoque.models import M2MEvento
        self.assertTrue(M2MEvento.objects.filter(tipo=M2MEvento.Tipo.CONFIG_FROTA, dados__alvo="localizacao_gps").exists())
        # sem mudança não grava de novo
        antes = M2MEvento.objects.count()
        gps.salvar_configuracao({"minutos_atualizada": 15, "horas_sem_atualizacao": 12}, self.admin)
        self.assertEqual(M2MEvento.objects.count(), antes)

    def test_independe_da_sincronizacao_da_tim(self):
        """A posição não depende de nenhum campo de sincronização do chip, e o contrário também."""
        v = self.veiculo("MQ-1")
        c = self.chip(1)
        vinculos.vincular(c, v, self.admin, "teste")
        self.ponto(v, 5)
        M2MChip.objects.filter(pk=c.pk).update(falhas_consecutivas=9, ultima_valida_em=None)
        self.assertEqual(gps.localizacao_do_veiculo(v)["estado"], "atualizada")


class SemFonteDeGpsTests(Base):
    """Cenário 6: nada é inventado."""

    def test_veiculo_sem_coordenada_e_indisponivel_e_a_tela_diz_a_ausencia(self):
        v = self.veiculo("MQ-1")
        loc = gps.localizacao_do_veiculo(v)
        self.assertEqual((loc["estado"], loc["lat"], loc["lon"], loc["url_mapa"]), ("indisponivel", None, None, ""))
        self.client.force_login(self.admin)
        html = self.client.get(reverse("frota_veiculo_detalhe", args=[v.pk])).content.decode()
        self.assertIn("Localização indisponível", html)
        self.assertNotIn("Ver no mapa", html, "sem coordenada não há para onde levar")

    def test_nenhum_marcador_em_posicao_ficticia_e_a_ausencia_e_contada(self):
        sem_a, sem_b = self.veiculo("MQ-1"), self.veiculo("MQ-2")
        d = mapa.montar_mapa({}, ver_frota=True, ver_infra=False, ver_chips=True)
        self.assertEqual(d["veiculos"], [])
        self.assertEqual(d["semLocalizacao"]["veiculos"], 2)
        self.assertTrue(sem_a.pk and sem_b.pk)

    def test_chip_com_sessao_ativa_e_sem_coordenada_nao_ganha_ponto(self):
        v = self.veiculo("MQ-1")
        c = self.chip(1)
        vinculos.vincular(c, v, self.admin, "teste")
        M2MChip.objects.filter(pk=c.pk).update(sessao_estado="ativa", consumo_bytes=5_000_000)
        d = mapa.montar_mapa({}, ver_frota=True, ver_infra=False, ver_chips=True)
        self.assertEqual(d["veiculos"], [])

    def test_foco_em_veiculo_sem_coordenada_diz_indisponivel_e_nao_desenha(self):
        v = self.veiculo("MQ-1")
        d = mapa.montar_mapa({"veiculo": str(v.pk)}, ver_frota=True, ver_infra=False, ver_chips=True)
        self.assertEqual(d["veiculos"], [])
        self.assertFalse(d["foco"]["tem_localizacao"])
        self.assertEqual(d["foco"]["estado"], "indisponivel")
        self.assertIn("indisponível", d["foco"]["mensagem"])

    def test_a_faixa_lista_quem_tem_chip_e_nao_tem_localizacao_sem_inventar_ponto(self):
        """O mapa vazio com chip cadastrado confundia: a faixa diz QUAIS veículos têm chip e por que não aparecem."""
        com_chip_sem_coord = self.veiculo("MQ-1")
        vinculos.vincular(self.chip(1), com_chip_sem_coord, self.admin, "teste")
        sem_chip = self.veiculo("MQ-2")
        com_chip_e_coord = self.veiculo("MQ-3", latitude="-14,7", longitude="-45,5")
        vinculos.vincular(self.chip(3), com_chip_e_coord, self.admin, "teste")
        d = mapa.montar_mapa({}, ver_frota=True, ver_infra=False, ver_chips=True)
        faixa = d["semLocalizacaoComChip"]
        self.assertEqual(faixa["total"], 1)
        self.assertEqual([i["codigo"] for i in faixa["itens"]], ["MQ-1"])
        self.assertEqual(faixa["itens"][0]["chips"], ["Chip 1"])
        self.assertEqual(faixa["itens"][0]["icone"], "trator")
        self.assertEqual(faixa["itens"][0]["url"], reverse("frota_veiculo_detalhe", args=[com_chip_sem_coord.pk]))
        self.assertEqual([p["codigo"] for p in d["veiculos"]], ["MQ-3"], "a faixa não cria marcador nenhum")
        self.assertTrue(sem_chip.pk)

    def test_a_faixa_so_nomeia_os_chips_para_quem_ve_chips_e_so_existe_para_quem_ve_a_frota(self):
        v = self.veiculo("MQ-1")
        vinculos.vincular(self.chip(1), v, self.admin, "teste")
        sem_permissao_de_chips = mapa.montar_mapa({}, ver_frota=True, ver_infra=False, ver_chips=False)["semLocalizacaoComChip"]
        self.assertEqual((sem_permissao_de_chips["total"], sem_permissao_de_chips["itens"][0]["chips"]), (1, []))
        so_infra = mapa.montar_mapa({}, ver_frota=False, ver_infra=True, ver_chips=True)["semLocalizacaoComChip"]
        self.assertEqual((so_infra["total"], so_infra["itens"]), (0, []))

    def test_a_faixa_traz_o_total_inteiro_mas_nomeia_so_os_primeiros(self):
        for n in range(mapa.LIMITE_SEM_LOCALIZACAO + 3):
            vinculos.vincular(self.chip(n), self.veiculo(f"MQ-{n:03d}"), self.admin, "teste")
        faixa = mapa.montar_mapa({}, ver_frota=True, ver_infra=False, ver_chips=True)["semLocalizacaoComChip"]
        self.assertEqual(faixa["total"], mapa.LIMITE_SEM_LOCALIZACAO + 3)
        self.assertEqual(len(faixa["itens"]), mapa.LIMITE_SEM_LOCALIZACAO)

    def test_a_pagina_do_mapa_traz_a_faixa_acima_do_mapa(self):
        self.client.force_login(self.admin)
        html = self.client.get("/agro/mapa/?camada=veiculos&camada=estruturas&camada=enlaces").content.decode()
        self.assertIn('id="agro-mapa-sem-chip"', html)
        self.assertLess(html.index('id="agro-mapa-sem-chip"'), html.index('class="agro-mapa-layout"'))

    def test_foco_em_veiculo_inexistente_diz_que_nao_encontrou(self):
        d = mapa.montar_mapa({"veiculo": "999999"}, ver_frota=True, ver_infra=False, ver_chips=False)
        self.assertFalse(d["foco"]["encontrado"])

    def test_chip_em_tablet_de_ti_ou_sem_vinculo_nao_tem_fonte_de_gps_e_a_resposta_diz_isso(self):
        livre = self.chip(1)
        achado = gps.localizacao_do_chip(livre)
        self.assertEqual((achado["vinculo"], achado["localizacao"]), ("nenhum", None))
        self.assertIn("não está vinculado", achado["mensagem"])


class ChipEVeiculoTests(Base):
    """Cenário 5."""

    def test_o_chip_localiza_o_veiculo_onde_esta_instalado(self):
        v = self.veiculo("MQ-1")
        c = self.chip(1)
        vinculos.vincular(c, v, self.admin, "teste")
        self.ponto(v, 2)
        achado = gps.localizacao_do_chip(c)
        self.assertEqual((achado["vinculo"], achado["veiculo"].pk, achado["localizacao"]["estado"]), ("veiculo", v.pk, "atualizada"))

    def test_o_veiculo_com_chip_aparece_no_mapa_marcado_e_com_a_atividade_do_chip(self):
        v = self.veiculo("MQ-1")
        c = self.chip(1)
        vinculos.vincular(c, v, self.admin, "teste")
        self.ponto(v, 2)
        d = mapa.montar_mapa({}, ver_frota=True, ver_infra=False, ver_chips=True)
        p = d["veiculos"][0]
        self.assertTrue(p["tem_chip"])
        self.assertEqual(p["icone"], "trator")
        self.assertEqual(p["localizacao"]["estado"], "atualizada")
        self.assertEqual(p["localizacao"]["fonte"], FONTE)
        self.assertEqual(p["chips"][0]["url"], reverse("m2m_chip_detalhe", args=[c.pk]))
        self.assertIn("atividade", p["chips"][0])

    def test_veiculo_sem_chip_nao_tem_o_selo(self):
        v = self.veiculo("MQ-1", latitude="-14,7", longitude="-45,5")
        self.assertFalse(mapa.montar_mapa({}, ver_frota=True, ver_infra=False, ver_chips=True)["veiculos"][0]["tem_chip"])
        self.assertTrue(v.pk)

    def test_filtro_de_chip_separa_quem_tem_de_quem_nao_tem(self):
        com, sem = self.veiculo("MQ-C", latitude="-14,7", longitude="-45,5"), self.veiculo("MQ-S", latitude="-14,7", longitude="-45,5")
        vinculos.vincular(self.chip(1), com, self.admin, "teste")
        codigos = lambda **f: {p["codigo"] for p in mapa.montar_mapa(f, ver_frota=True, ver_infra=False, ver_chips=True)["veiculos"]}   # noqa: E731
        self.assertEqual(codigos(chip="com"), {"MQ-C"})
        self.assertEqual(codigos(chip="sem"), {"MQ-S"})
        self.assertTrue(sem.pk)

    def test_transferir_o_chip_leva_a_localizacao_atual_junto_e_preserva_o_historico_do_veiculo_anterior(self):
        a, b = self.veiculo("MQ-A"), self.veiculo("MQ-B")
        c = self.chip(1)
        vinculos.vincular(c, a, self.admin, "teste")
        self.ponto(a, 30, "-14,80", "-45,60")
        self.ponto(b, 20, "-14,90", "-45,70")
        antes = gps.localizacao_do_chip(c)
        self.assertEqual((antes["veiculo"].pk, antes["localizacao"]["lat"]), (a.pk, -14.8))
        historico_de_a = list(FrotaPosicao.objects.filter(veiculo=a).values_list("pk", "latitude", "longitude"))

        vinculo = c.vinculos.get(fim_em__isnull=True)
        vinculos.transferir(vinculo.pk, b, self.admin, "troca de máquina")

        depois = gps.localizacao_do_chip(c)
        self.assertEqual((depois["veiculo"].pk, depois["localizacao"]["lat"]), (b.pk, -14.9), "a localização acompanha o novo vínculo")
        self.assertEqual(list(FrotaPosicao.objects.filter(veiculo=a).values_list("pk", "latitude", "longitude")), historico_de_a,
                         "o histórico do veículo anterior NÃO é sobrescrito")
        self.assertEqual(FrotaPosicao.objects.filter(veiculo=b).count(), 1)
        # e o mapa abre no veículo NOVO (o foco vem do vínculo atual)
        self.assertTrue(depois["localizacao"]["url_mapa"].endswith(f"veiculo={b.pk}"))

    def test_foco_destaca_o_veiculo_mesmo_quando_os_filtros_o_deixariam_de_fora(self):
        v = self.veiculo("MQ-1")
        outro = self.veiculo("MQ-2", latitude="-14,7", longitude="-45,5")
        self.ponto(v, 3)
        d = mapa.montar_mapa({"q": "MQ-2", "veiculo": str(v.pk)}, ver_frota=True, ver_infra=False, ver_chips=False)
        self.assertEqual({p["codigo"] for p in d["veiculos"]}, {"MQ-2", "MQ-1"})
        self.assertTrue(d["foco"]["tem_localizacao"])
        self.assertEqual(d["foco"]["veiculo"], v.pk)
        self.assertTrue(outro.pk)

    def test_historico_de_posicoes_do_veiculo_vem_do_mais_novo_para_o_mais_antigo(self):
        v = self.veiculo("MQ-1")
        for minutos in (50, 5, 25):
            self.ponto(v, minutos, lat=f"-14,{minutos:02d}")
        hist = gps.historico_do_veiculo(v)
        self.assertEqual([h["lat"] for h in hist], [-14.05, -14.25, -14.5])
        self.assertEqual(hist[0]["fonte"], FONTE)

    def test_ultimas_posicoes_numa_consulta_so(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        vs = [self.veiculo(f"MQ-{n}") for n in range(4)]
        for v in vs:
            self.ponto(v, 10)
            self.ponto(v, 2)
        with CaptureQueriesContext(connection) as q:
            achadas = gps.ultimas_posicoes([v.pk for v in vs])
        self.assertEqual(len(q), 1)
        self.assertEqual(len(achadas), 4)
        self.assertTrue(all(p.medida_em > timezone.now() - timedelta(minutes=5) for p in achadas.values()))


class TelasEPermissoesTests(Base):
    def entrar(self, u):
        self.client.force_login(u)
        return self.client

    def test_a_rota_do_pedido_abre_e_as_tres_camadas_carregam(self):
        """Cenário 7."""
        v = self.veiculo("MQ-1")
        self.ponto(v, 3)
        a = infra.salvar_estrutura({"nome": "Torre A", "tipo": "torre", "latitude": "-14,7763", "longitude": "-45,5634"}, self.admin)
        b = infra.salvar_estrutura({"nome": "Torre B", "tipo": "torre", "latitude": "-14,78", "longitude": "-45,57"}, self.admin)
        infra.salvar_enlace({"identificacao": "PTP A-B", "estrutura_a": a.pk, "estrutura_b": b.pk}, self.admin)
        c = self.entrar(self.admin)
        url = "/agro/mapa/?camada=veiculos&camada=estruturas&camada=enlaces"
        r = c.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["config_mapa"]["camadas"], ["veiculos", "estruturas", "enlaces"])
        dados = json.loads(c.get(reverse("agro_mapa_dados") + "?camada=veiculos&camada=estruturas&camada=enlaces").content)
        self.assertEqual((len(dados["veiculos"]), len(dados["estruturas"]), len(dados["enlaces"])), (1, 2, 1))
        self.assertEqual(dados["camadas"], ["veiculos", "estruturas", "enlaces"])

    def test_o_parametro_veiculo_chega_a_configuracao_do_mapa_e_a_url_do_detalhe_do_chip_o_usa(self):
        v = self.veiculo("MQ-1")
        self.ponto(v, 3)
        chip = self.chip(1)
        vinculos.vincular(chip, v, self.admin, "teste")
        c = self.entrar(self.admin)
        self.assertEqual(c.get(reverse("agro_mapa"), {"veiculo": v.pk}).context["config_mapa"]["foco"], v.pk)
        detalhe = c.get(reverse("m2m_chip_detalhe", args=[chip.pk])).content.decode()
        self.assertIn("Localização do equipamento", detalhe)
        self.assertIn("camada=veiculos&amp;camada=estruturas&amp;camada=enlaces&amp;veiculo=" + str(v.pk), detalhe)
        self.assertIn("Ver no mapa", detalhe)

    def test_detalhe_do_veiculo_mostra_estado_origem_fonte_e_o_historico_de_posicoes(self):
        v = self.veiculo("MQ-1")
        self.ponto(v, 3, precisao_m="4,5", velocidade_kmh="12,3", direcao_graus=90)
        html = self.entrar(self.admin).get(reverse("frota_veiculo_detalhe", args=[v.pk])).content.decode()
        for trecho in ("Localização GPS atualizada", FONTE, "Última atualização GPS", "Precisão", "Velocidade", "Direção", "Últimas posições GPS recebidas", "Ver no mapa"):
            self.assertIn(trecho, html)

    def test_chip_sem_veiculo_nao_oferece_o_mapa(self):
        chip = self.chip(1)
        html = self.entrar(self.admin).get(reverse("m2m_chip_detalhe", args=[chip.pk])).content.decode()
        self.assertIn("não está vinculado a nenhum equipamento", html)
        self.assertNotIn("Ver no mapa", html)

    def test_quem_so_ve_chips_nao_ve_a_localizacao_do_veiculo(self):
        """Privacidade: a posição é dado do módulo de frotas (`ver_frota`), não do de chips."""
        v = self.veiculo("MQ-1")
        self.ponto(v, 3)
        chip = self.chip(1)
        vinculos.vincular(chip, v, self.admin, "teste")
        so_chips = usuario("so_chips", "ver_chips", "acessar_zelo_agro")
        html = self.entrar(so_chips).get(reverse("m2m_chip_detalhe", args=[chip.pk])).content.decode()
        self.assertNotIn("Localização do equipamento", html)
        self.assertNotIn("-14.8", html)
        self.assertNotIn("-45.6", html)

    def test_quem_so_ve_infra_nao_recebe_veiculo_nem_o_foco(self):
        v = self.veiculo("MQ-1")
        self.ponto(v, 3)
        so_infra = usuario("so_infra", "ver_tec_estoque", "acessar_zelo_agro")
        c = self.entrar(so_infra)
        dados = json.loads(c.get(reverse("agro_mapa_dados"), {"veiculo": v.pk}).content)
        self.assertEqual(dados["veiculos"], [])
        self.assertIsNone(dados["foco"])
        self.assertIsNone(c.get(reverse("agro_mapa"), {"veiculo": v.pk}).context["config_mapa"]["foco"])

    def test_o_json_do_mapa_nao_e_cacheavel_porque_e_localizacao_de_ativos(self):
        r = self.entrar(self.admin).get(reverse("agro_mapa_dados"))
        self.assertIn("no-store", r["Cache-Control"])

    def test_anonimo_nao_ve_nada(self):
        self.assertEqual(self.client.get(reverse("agro_mapa_dados")).status_code, 302)
        self.assertEqual(self.client.get(reverse("agro_mapa_config")).status_code, 302)

    def test_a_configuracao_so_para_quem_administra_e_valida_o_formulario(self):
        sem = usuario("sem_cfg", "ver_frota", "acessar_zelo_agro")
        self.assertEqual(self.entrar(sem).get(reverse("agro_mapa_config")).status_code, 403)
        c = self.entrar(self.admin)
        self.assertEqual(c.get(reverse("agro_mapa_config")).status_code, 200)
        ruim = c.post(reverse("agro_mapa_config"), {"minutos_atualizada": "120", "horas_sem_atualizacao": "1"})
        self.assertEqual(ruim.status_code, 200)
        self.assertIn("maior que o de", ruim.content.decode())
        ok = c.post(reverse("agro_mapa_config"), {"minutos_atualizada": "10", "horas_sem_atualizacao": "6"})
        self.assertEqual(ok.status_code, 302)
        self.assertEqual(FrotaLocalizacaoConfig.get().minutos_atualizada, 10)

    def test_abrir_o_mapa_nao_chama_a_api_da_tim_nem_servico_externo(self):
        from services import tim_m2m_service

        v = self.veiculo("MQ-1")
        self.ponto(v, 3)
        vinculos.vincular(self.chip(1), v, self.admin, "teste")
        with mock.patch.object(tim_m2m_service.ClienteTIM, "_enviar", side_effect=AssertionError("o mapa só lê o banco")):
            self.entrar(self.admin).get(reverse("agro_mapa_dados"))

    def test_o_chip_nao_vira_fonte_de_posicao(self):
        """Regressão da regra 64: ICCID, sessão e consumo não são localização."""
        v = self.veiculo("MQ-1")
        c = self.chip(1)
        vinculos.vincular(c, v, self.admin, "teste")
        M2MChip.objects.filter(pk=c.pk).update(sessao_estado="ativa", consumo_bytes=9_000_000)
        v.refresh_from_db()
        self.assertEqual((v.latitude, v.localizacao_origem), (None, ""))
        self.assertEqual(FrotaPosicao.objects.count(), 0)

    def test_o_mapa_de_gps_nao_cresce_em_consultas_com_o_numero_de_veiculos(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        mapa.montar_mapa({}, ver_frota=True, ver_infra=False, ver_chips=True)      # aquecimento
        for n in range(3):
            self.ponto(self.veiculo(f"MQ-A{n}"), 5)
        with CaptureQueriesContext(connection) as poucos:
            mapa.montar_mapa({}, ver_frota=True, ver_infra=False, ver_chips=True)
        for n in range(3, 20):
            v = self.veiculo(f"MQ-A{n}")
            vinculos.vincular(self.chip(n), v, self.admin, "teste")
            self.ponto(v, 5)
        with CaptureQueriesContext(connection) as muitos:
            mapa.montar_mapa({}, ver_frota=True, ver_infra=False, ver_chips=True)
        self.assertLessEqual(len(muitos), len(poucos) + 3)
