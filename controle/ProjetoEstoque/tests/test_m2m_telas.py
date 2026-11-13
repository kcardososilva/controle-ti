"""
Monitoramento TIM M2M — telas (Fase B): acesso, renderização, filtros, JSON e segredo.

Reaproveita a base do `test_m2m.py` (sessão HTTP falsa, `time.sleep` substituído,
token no ambiente): NENHUM teste toca a rede. As chamadas que as views fazem à API
passam por uma sessão falsa injetada no lugar de `requests.Session`.

O que estes testes protegem — todas as falhas aqui passam em SILÊNCIO, porque a
tela continua abrindo 200 para quem testa logado como administrador:

  • ACESSO — cada rota exige login (302) e a permissão do nível (403): quem só
    `ver` não opera nem configura; esconder o botão não é autorização.
  • HONESTIDADE DA TELA — sessão ativa confirmada, sem sessão, não interpretado,
    consulta pendente, falha de consulta e desatualizado são seis coisas
    diferentes e cada uma tem marcação própria; falha NUNCA aparece como "sem
    sessão" nem como "inativo".
  • SEGREDO — o token (do ambiente e do banco, e o texto cifrado) não aparece no
    HTML de nenhuma tela nem em nenhum JSON.
  • TEMPLATE — nenhuma tag crua vaza para a tela (`{{`, `{%`, `{#`): include com
    caminho errado e comentário multilinha não dão erro, só renderizam lixo.
"""

import json
import re
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import Group, Permission, User
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import connection
from django.template import Context, Template
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from ProjetoEstoque.models import (
    GRUPO_FROTAS,
    GRUPO_FROTAS_CONSULTA,
    GRUPO_PORTAL_CONTRATACAO,
    Categoria,
    CentroCusto,
    Item,
    Localidade,
    M2MChip,
    M2MConsulta,
    M2MEvento,
    M2MIntegracao,
    M2MOcorrencia,
    SimNaoChoices,
    Subtipo,
    Usuario,
)
from ProjetoEstoque.tests import test_m2m as base
from services import m2m_interpretacao as interp
from services import m2m_monitoramento_service as m2m
from services import tim_m2m_service as tim

_RAIZ = Path(settings.BASE_DIR) / "ProjetoEstoque"
_TOKEN_DO_BANCO = "TOKEN-DO-BANCO-QWERTY54321"


# ─────────────────────────────────────────────────────────────
# Base
# ─────────────────────────────────────────────────────────────

class TelasBase(base.M2MBase):
    """Token no ambiente (herdado), um superusuário logado e fábricas de cenário."""

    def setUp(self):
        super().setUp()
        # Os middlewares guardam os grupos do usuário no cache por 60 s, e o id de usuário
        # se repete de um teste para outro: sem limpar, o grupo de um teste vaza no seguinte.
        cache.clear()
        self.admin = User.objects.create(username="admin_telas", is_superuser=True, is_staff=True)
        self.client = Client()
        self.client.force_login(self.admin)

    # ── usuários ──
    # Os três níveis antigos, como a migration 0191 os mapeou. "operar" aqui é só o que ele
    # ACRESCENTA ao "ver" (as permissões são independentes: quem só opera não vê as telas).
    NIVEIS = {
        "ver": ("ver_frota", "ver_chips", "consultar_conectividade", "ver_historico_frota"),
        "operar": ("editar_frota", "importar_chips", "vincular_chips", "transferir_chips", "tratar_ocorrencias"),
        "configurar": ("administrar_frota_config",),
    }

    def usuario_com(self, nome, *chaves):
        """Usuário comum (sem staff) com o que se pede: um nível antigo (`ver`, `operar`,
        `configurar`) ou o `codename` de uma permissão granular."""
        user = User.objects.create(username=nome)
        for chave in chaves:
            for codigo in self.NIVEIS.get(chave, (chave,)):
                user.user_permissions.add(Permission.objects.get(codename=codigo))
        return User.objects.get(pk=user.pk)

    def cliente_de(self, user):
        cliente = Client()
        cliente.force_login(user)
        return cliente

    # ── rede falsa ──
    def api_falsa(self, respostas):
        """Toda `requests.Session()` criada pelas views passa a ser esta sessão falsa."""
        sessao = base.Sessao(respostas)
        patch = mock.patch.object(tim.requests, "Session", return_value=sessao)
        patch.start()
        self.addCleanup(patch.stop)
        return sessao

    # ── cenário com um chip em cada situação ──
    def cenario(self):
        """Cinco chips que cobrem as distinções que a tela nunca pode borrar. Devolve um dict."""
        self.mapear()
        c = {}
        c["ativo"] = self.chip(1, nome="Chip Ativo")
        self.atualizar(c["ativo"], base.roteador(), origem="manual")

        c["sem_sessao"] = self.chip(2, nome="Chip Sem Sessao")
        self.atualizar(c["sem_sessao"], base.roteador(sessao=base.SESSAO_DOWN), origem="manual")

        c["nao_lido"] = self.chip(3, nome="Chip Nao Lido")
        self.atualizar(c["nao_lido"], base.roteador(sessao={"sessao": {"estado": "XYZ"}}), origem="manual")

        c["falha"] = self.chip(4, nome="Chip Com Falha")
        self.atualizar(c["falha"], base.roteador(), origem="manual")      # tem dado válido...
        self.falhar(c["falha"], 3)                                         # ...e as 3 consultas seguintes falham
        m2m.avaliar_ocorrencias(c["falha"], notificar=False)

        c["desativado"] = self.chip(5, nome="Chip Desativado")
        m2m.desativar(c["desativado"], self.usuario, "Chip devolvido")
        c["pendente"] = self.chip(6, nome="Chip Pendente")
        for chip in c.values():
            chip.refresh_from_db()
        return c

    def get(self, nome, *args, **params):
        return self.client.get(reverse(nome, args=args), params)

    def html(self, resposta):
        self.assertEqual(resposta.status_code, 200, f"{resposta.request['PATH_INFO']} respondeu {resposta.status_code}")
        return resposta.content.decode("utf-8")

    def sem_lixo_de_template(self, html, onde=""):
        for marca in ("{{", "{%", "{#", "%}", "#}"):
            self.assertNotIn(marca, html, f"'{marca}' vazou para a tela {onde}")


# ═════════════════════════════════════════════════════════════
# 1. Acesso
# ═════════════════════════════════════════════════════════════

class AcessoTests(TelasBase):
    """Cada rota exige login (302) e a permissão do nível (403)."""

    def setUp(self):
        super().setUp()
        self.chip_ = self.chip(1)
        self.atualizar(self.chip_, base.roteador(), origem="manual")
        self.consulta = self.chip_.sessao_consulta
        self.ocorrencia = M2MOcorrencia.objects.create(chip=self.chip_, tipo="sem_sessao", nivel="atencao", descricao="x")

    def rotas(self):
        pk, oc = self.chip_.pk, self.ocorrencia.pk
        # (nome, args, método, permissão exigida)
        return [
            ("m2m_visao_geral", [], "get", "ver_chips"),
            ("m2m_chips", [], "get", "ver_chips"),
            ("m2m_chip_detalhe", [pk], "get", "ver_chips"),
            ("m2m_consumo", [], "get", "consultar_conectividade"),
            ("m2m_sessoes", [], "get", "consultar_conectividade"),
            ("m2m_alertas", [], "get", "ver_historico_frota"),
            ("m2m_consulta_payload", [self.consulta.pk], "get", "consultar_conectividade"),
            ("m2m_chip_novo", [], "get", "importar_chips"),
            ("m2m_chip_editar", [pk], "get", "importar_chips"),
            ("m2m_opcoes", [], "get", "importar_chips"),
            ("m2m_chip_monitoramento", [pk], "post", "importar_chips"),
            ("m2m_chip_atualizar", [pk], "post", "consultar_conectividade"),
            ("m2m_alerta_tratar", [oc], "post", "tratar_ocorrencias"),
            ("m2m_config", [], "get", "administrar_frota_config"),
            ("m2m_config_token", [], "post", "administrar_frota_config"),
            ("m2m_config_frequencia", [], "post", "administrar_frota_config"),
            ("m2m_config_mapeamento", [], "post", "administrar_frota_config"),
            ("m2m_config_previa", [], "post", "administrar_frota_config"),
            ("m2m_config_testar", [], "post", "administrar_frota_config"),
            ("m2m_reinterpretar", [], "post", "administrar_frota_config"),
        ]

    def chamar(self, cliente, nome, args, metodo):
        url = reverse(nome, args=args)
        return cliente.post(url, {}) if metodo == "post" else cliente.get(url)

    def test_anonimo_vai_para_o_login_em_toda_rota(self):
        anonimo = Client()
        for nome, args, metodo, _ in self.rotas():
            with self.subTest(rota=nome):
                r = self.chamar(anonimo, nome, args, metodo)
                self.assertEqual(r.status_code, 302)
                self.assertIn("/login/", r["Location"])

    def test_usuario_sem_permissao_recebe_403_em_toda_rota(self):
        cliente = self.cliente_de(self.usuario_com("sem_nada"))
        for nome, args, metodo, _ in self.rotas():
            with self.subTest(rota=nome):
                self.assertEqual(self.chamar(cliente, nome, args, metodo).status_code, 403)

    def test_cada_rota_exige_exatamente_a_sua_permissao(self):
        """Quem tem SÓ a permissão da rota passa (não leva 403); quem tem todas as OUTRAS leva 403."""
        todas = {c for c in {r[3] for r in self.rotas()}}
        for nome, args, metodo, exigida in self.rotas():
            with self.subTest(rota=nome):
                so_ela = self.cliente_de(self.usuario_com(f"so_{nome}", exigida))
                self.assertNotEqual(self.chamar(so_ela, nome, args, metodo).status_code, 403, f"{nome} barrou quem tem {exigida}")
                todas_menos_ela = self.cliente_de(self.usuario_com(f"menos_{nome}", *sorted(todas - {exigida})))
                self.assertEqual(
                    self.chamar(todas_menos_ela, nome, args, metodo).status_code, 403,
                    f"{nome} deixou passar quem não tem {exigida}",
                )

    def test_quem_so_ve_chips_le_a_lista_e_nao_opera_nem_atualiza_nem_configura(self):
        cliente = self.cliente_de(self.usuario_com("so_ve", "ver_chips"))
        for nome, args, metodo, exigida in self.rotas():
            with self.subTest(rota=nome):
                r = self.chamar(cliente, nome, args, metodo)
                if exigida == "ver_chips":
                    self.assertEqual(r.status_code, 200)
                else:
                    self.assertEqual(r.status_code, 403, f"{nome} deixou passar quem só pode ver os chips")

    def test_o_nivel_ver_antigo_agora_tambem_atualiza_a_consulta(self):
        """Consequência da troca de permissões, por decisão do plano: `ver_m2m` virou
        `ver_frota + ver_chips + consultar_conectividade + ver_historico_frota`, e o "Atualizar"
        é de `consultar_conectividade` — quem só via passou a poder atualizar."""
        cliente = self.cliente_de(self.usuario_com("ver_antigo", "ver"))
        M2MChip.objects.filter(pk=self.chip_.pk).update(ultima_tentativa_em=None)      # sem a trava de 60 s
        sessao = self.api_falsa(base.roteador())
        r = cliente.post(reverse("m2m_chip_atualizar", args=[self.chip_.pk]), data="{}", content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(sessao.chamadas)
        self.assertEqual(
            cliente.post(reverse("m2m_chip_monitoramento", args=[self.chip_.pk]), {"acao": "desativar", "motivo": "x"}).status_code, 403,
        )

    def test_quem_so_opera_ou_so_configura_nao_ve_as_telas(self):
        """As permissões são independentes: operar não implica ver."""
        for nivel in ("operar", "configurar"):
            cliente = self.cliente_de(self.usuario_com(f"so_{nivel}", nivel))
            for nome, args, metodo, exigido in self.rotas():
                if exigido in self.NIVEIS["ver"]:
                    with self.subTest(nivel=nivel, rota=nome):
                        self.assertEqual(self.chamar(cliente, nome, args, metodo).status_code, 403)

    def test_grupo_frotas_opera_mas_nao_administra_a_configuracao(self):
        user = User.objects.create(username="do_grupo")
        user.groups.add(Group.objects.get(name=GRUPO_FROTAS))
        cliente = self.cliente_de(User.objects.get(pk=user.pk))
        for nome, args, metodo, exigida in self.rotas():
            with self.subTest(rota=nome):
                r = self.chamar(cliente, nome, args, metodo)
                if exigida == "administrar_frota_config":
                    self.assertEqual(r.status_code, 403)
                else:
                    self.assertNotEqual(r.status_code, 403, f"{nome}: o grupo deveria passar")

    def test_grupo_de_consulta_le_mas_nao_cadastra_nem_trata_nem_configura(self):
        user = User.objects.create(username="do_grupo_consulta")
        user.groups.add(Group.objects.get(name=GRUPO_FROTAS_CONSULTA))
        cliente = self.cliente_de(User.objects.get(pk=user.pk))
        for nome, args, metodo, exigida in self.rotas():
            with self.subTest(rota=nome):
                r = self.chamar(cliente, nome, args, metodo)
                if exigida in ("importar_chips", "tratar_ocorrencias", "administrar_frota_config"):
                    self.assertEqual(r.status_code, 403)
                else:
                    self.assertNotEqual(r.status_code, 403)

    def test_superusuario_abre_todas_as_telas_de_leitura(self):
        for nome, args, metodo, _ in self.rotas():
            if metodo == "get" and nome != "m2m_opcoes":
                with self.subTest(rota=nome):
                    self.assertEqual(self.chamar(self.client, nome, args, "get").status_code, 200)

    def test_rota_de_post_recusa_get(self):
        for nome, args, metodo, _ in self.rotas():
            if metodo == "post":
                with self.subTest(rota=nome):
                    self.assertEqual(self.client.get(reverse(nome, args=args)).status_code, 405)

    def test_post_sem_token_csrf_e_barrado(self):
        cliente = Client(enforce_csrf_checks=True)
        cliente.force_login(self.admin)
        r = cliente.post(
            reverse("m2m_chip_atualizar", args=[self.chip_.pk]),
            data=json.dumps({"tipos": ["sessao"]}), content_type="application/json",
        )
        self.assertEqual(r.status_code, 403)
        self.assertEqual(M2MConsulta.objects.filter(chip=self.chip_).count(), 2)   # só as do cenário

    def test_perfil_de_acesso_restrito_nao_alcanca_o_modulo_nem_com_a_permissao(self):
        restrito = User.objects.create(username="rh_restrito")
        restrito.groups.add(Group.objects.get(name=GRUPO_PORTAL_CONTRATACAO))
        restrito.user_permissions.add(Permission.objects.get(codename="ver_chips"))
        restrito = User.objects.get(pk=restrito.pk)
        r = self.cliente_de(restrito).get(reverse("m2m_visao_geral"))
        self.assertNotEqual(r.status_code, 200)


# ═════════════════════════════════════════════════════════════
# 2. Menu lateral
# ═════════════════════════════════════════════════════════════

class MenuTests(TelasBase):
    """Desde 2026-11-13 o módulo mora no Zelo Agro: o menu é o do Agro (`ambiente_service.NAV_AGRO`),
    com Frotas e Chips e Conectividade como acessos independentes — e o menu do TI não tem mais o grupo."""

    def menu(self, cliente, nome="agro_visao_geral"):
        r = cliente.get(reverse(nome))
        self.assertEqual(r.status_code, 200)
        return r.content.decode("utf-8")

    def test_grupo_aparece_para_quem_tem_ver_e_o_ti_nao_tem_o_grupo(self):
        com = self.menu(self.cliente_de(self.usuario_com("menu_ver", "ver")))
        self.assertIn('data-submenu-target="sub-agro-conectividade"', com)
        self.assertIn('data-submenu-target="sub-agro-frotas"', com)
        ti = self.menu(self.cliente_de(self.usuario_com("menu_ver_ti", "ver")), "dashboard")
        self.assertNotIn("sub-agro-", ti)
        self.assertNotIn("sub-m2m", ti)
        # sem permissão nenhuma do Agro, nem o ambiente abre
        self.assertEqual(self.cliente_de(self.usuario_com("menu_nada")).get(reverse("agro_visao_geral")).status_code, 403)

    def test_cada_item_so_aparece_com_a_permissao_da_tela(self):
        so_opera = self.menu(self.cliente_de(self.usuario_com("menu_operar", "operar")))
        self.assertIn(f'href="{reverse("frota_importar")}"', so_opera)    # importar_chips abre a importação
        self.assertNotIn(f'href="{reverse("m2m_chips")}"', so_opera)      # mas não a lista (ver_chips)
        self.assertNotIn("sub-agro-frotas", so_opera)                     # nem nada da frota (ver_frota)
        so_config = self.menu(self.cliente_de(self.usuario_com("menu_config", "configurar")))
        self.assertIn(f'href="{reverse("m2m_config")}"', so_config)
        self.assertIn(f'href="{reverse("frota_config_tipos")}"', so_config)
        self.assertNotIn(f'href="{reverse("m2m_chips")}"', so_config)

    def test_o_menu_tem_as_telas_e_a_configuracao_so_com_a_permissao(self):
        so_ve = self.menu(self.cliente_de(self.usuario_com("menu_a", "ver")))
        for nome in ("m2m_visao_geral", "m2m_chips", "m2m_consumo", "m2m_sessoes", "m2m_alertas",
                     "frota_movimentacoes", "frota_visao_geral", "frota_veiculos"):
            self.assertIn(f'href="{reverse(nome)}" class="sub-link', so_ve)
        self.assertNotIn(f'href="{reverse("m2m_config")}"', so_ve)
        completo = self.menu(self.cliente_de(self.usuario_com("menu_b", "ver", "configurar")))
        self.assertIn(f'href="{reverse("m2m_config")}" class="sub-link', completo)
        for rotulo in ("Visão geral", "Chips cadastrados", "Sessões", "Consumo",
                       "Movimentações", "Alertas", "Configuração", "Veículos e equipamentos", "Tipos de veículo"):
            self.assertIn(rotulo, completo)

    def test_grupo_acende_nas_telas_do_modulo(self):
        chip = self.chip(1)
        for nome, args in (("m2m_visao_geral", []), ("m2m_chips", []), ("m2m_chip_detalhe", [chip.pk]),
                           ("m2m_consumo", []), ("m2m_sessoes", []), ("m2m_alertas", []), ("m2m_config", [])):
            with self.subTest(tela=nome):
                html = self.client.get(reverse(nome, args=args)).content.decode("utf-8")
                botao = re.search(r'<button[^>]*data-submenu-target="sub-agro-conectividade"', html, re.S).group(0)
                self.assertIn("is-active", botao)
        html = self.client.get(reverse("frota_veiculos")).content.decode("utf-8")
        botao = re.search(r'<button[^>]*data-submenu-target="sub-agro-frotas"', html, re.S).group(0)
        self.assertIn("is-active", botao)

    def test_os_icones_do_menu_existem_no_sprite_e_sao_contorno(self):
        from services.ambiente_service import NAV_AGRO
        html = (_RAIZ / "templates" / "base.html").read_text(encoding="utf-8")
        for entrada in NAV_AGRO:
            icone = entrada["icone"]
            simbolo = re.search(rf'<symbol id="{icone}"[^>]*>(.*?)</symbol>', html, re.S)
            self.assertIsNotNone(simbolo, f"falta <symbol id=\"{icone}\"> no sprite")
            self.assertNotIn("fill=", simbolo.group(1), "o ícone do menu é de traço, não de preenchimento")

    def test_o_menu_do_modulo_fica_fora_do_bloco_so_staff(self):
        """Quem tem a permissão mas não é staff precisa ver o grupo (e o que é só-staff continua fora)."""
        html = self.menu(self.cliente_de(self.usuario_com("nao_staff", "ver")))
        self.assertIn("sub-agro-conectividade", html)
        self.assertNotIn('data-submenu-target="sub-ninja"', html)


# ═════════════════════════════════════════════════════════════
# 3. As telas renderizam — vazias e com dados
# ═════════════════════════════════════════════════════════════

class TelasVaziasTests(TelasBase):
    def test_todas_as_telas_abrem_sem_nenhum_dado_e_sem_lixo_de_template(self):
        for nome in ("m2m_visao_geral", "m2m_chips", "m2m_chip_novo", "m2m_consumo", "m2m_sessoes", "m2m_alertas", "m2m_config"):
            with self.subTest(tela=nome):
                html = self.html(self.get(nome))
                self.sem_lixo_de_template(html, nome)
                self.assertIn("m2m-faixa", html)            # a faixa de saúde está em TODA tela
                self.assertIn("m2m-abas", html)
                self.assertIn("Telecom · TIM M2M", html)

    def test_visao_geral_vazia_diz_que_nao_ha_chip_e_aponta_o_proximo_passo(self):
        html = self.html(self.get("m2m_visao_geral"))
        self.assertIn("Nenhum chip cadastrado", html)
        self.assertIn(reverse("m2m_chip_novo"), html)
        self.assertIn("Para começar", html)

    def test_lista_vazia_tem_estado_vazio(self):
        html = self.html(self.get("m2m_chips"))
        self.assertIn("Nenhum chip cadastrado", html)
        self.assertNotIn("<table", html)

    def test_alertas_vazio_explica_por_que_uma_falha_isolada_nao_aparece(self):
        html = self.html(self.get("m2m_alertas"))
        self.assertIn("Nenhuma ocorrência em aberto", html)
        self.assertIn("Falhas isoladas", html)

    def test_consumo_e_sessoes_vazios_tem_estado_vazio(self):
        self.assertIn("Nenhuma consulta de consumo", self.html(self.get("m2m_consumo")))
        self.assertIn("Nenhum chip cadastrado", self.html(self.get("m2m_sessoes")))      # reformulação 12/11: texto do estado vazio

    def test_config_sem_token_diz_que_nao_ha_credencial(self):
        with override_settings(TIM_M2M_TOKEN=""):
            html = self.html(self.get("m2m_config"))
        self.assertIn("Não configurada", html)
        self.assertIn("Sem token, nenhuma consulta é feita", html)


class TelasComDadosTests(TelasBase):
    def setUp(self):
        super().setUp()
        self.c = self.cenario()

    def test_todas_as_telas_abrem_com_dados_e_sem_lixo(self):
        pk = self.c["ativo"].pk
        for nome, args in (("m2m_visao_geral", []), ("m2m_chips", []), ("m2m_chip_detalhe", [pk]),
                           ("m2m_chip_editar", [pk]), ("m2m_consumo", []), ("m2m_sessoes", []),
                           ("m2m_alertas", []), ("m2m_config", [])):
            with self.subTest(tela=nome):
                html = self.html(self.get(nome, *args))
                self.sem_lixo_de_template(html, nome)

    def test_detalhe_de_cada_chip_abre(self):
        for chave, chip in self.c.items():
            with self.subTest(chip=chave):
                self.sem_lixo_de_template(self.html(self.get("m2m_chip_detalhe", chip.pk)), chave)

    def test_lista_mostra_cada_situacao_com_a_marcacao_propria(self):
        html = self.html(self.get("m2m_chips"))
        # "normal" (monitoramento saudável) não repete um indicador ao lado da sessão: só o que foge do
        # normal ganha uma segunda marca (reformulação de 12/11/2026, lista de chips mais compacta).
        for estado in ("ativa", "sem_sessao", "nao_interpretado", "erro_consulta", "desativado", "nao_consultado", "pendente"):
            self.assertIn(f'data-estado="{estado}"', html, f"a lista não tem o indicador '{estado}'")
        for nome in ("Chip Ativo", "Chip Sem Sessao", "Chip Nao Lido", "Chip Com Falha", "Chip Desativado", "Chip Pendente"):
            self.assertIn(nome, html)

    def test_iccid_aparece_agrupado_em_blocos_de_4(self):
        html = self.html(self.get("m2m_chips"))
        agrupado = base.iccid_valido(1)
        agrupado = " ".join(agrupado[i:i + 4] for i in range(0, len(agrupado), 4))
        self.assertIn(agrupado, html)
        self.assertNotIn(f">{base.iccid_valido(1)}<", html)      # o valor cru não é o que se lê

    def test_cada_linha_e_cartao_no_celular_todo_td_tem_rotulo(self):
        html = self.html(self.get("m2m_chips"))
        linha = re.search(r'<tr class="m2m-linha".*?</tr>', html, re.S).group(0)
        tds = re.findall(r"<td([^>]*)>", linha)
        self.assertGreaterEqual(len(tds), 6)      # chip, vínculo, conectividade, consumo, última atualização, ações
        for atributos in tds:
            self.assertIn("data-rotulo", atributos)
        css = (_RAIZ / "static" / "css" / "ds-m2m.css").read_text(encoding="utf-8")
        self.assertIn("attr(data-rotulo)", css)

    def test_falha_de_consulta_nunca_aparece_como_sem_sessao_nem_inativo(self):
        """O chip que tem dado válido ATIVO e 3 consultas falhas continua 'ativa' (a última
        resposta válida) E é marcado como falha de consulta — são duas colunas, e nenhuma vira a outra."""
        falha = self.c["falha"]
        self.assertEqual(falha.sessao_estado, "ativa")
        self.assertEqual(falha.falhas_consecutivas, 3)
        html = self.html(self.get("m2m_chips"))
        linha = re.search(rf'<tr class="m2m-linha" data-chip-id="{falha.pk}".*?</tr>', html, re.S).group(0)
        self.assertIn('data-estado="ativa"', linha)
        self.assertIn('data-estado="erro_consulta"', linha)
        self.assertNotIn('data-estado="sem_sessao"', linha)
        self.assertNotIn('data-estado="desativado"', linha)
        self.assertIn("3 falhas seguidas", linha)

    def test_detalhe_do_chip_com_falha_separa_o_estado_da_consulta(self):
        html = self.html(self.get("m2m_chip_detalhe", self.c["falha"].pk))
        self.assertIn("A última consulta falhou", html)
        self.assertIn("não diz que o chip está fora do ar", html)
        self.assertIn("continua sendo o da última resposta válida", html)
        self.assertIn("Sessão ativa", html)

    def test_detalhe_do_chip_nunca_consultado_diz_que_nao_ha_resposta_valida(self):
        html = self.html(self.get("m2m_chip_detalhe", self.c["pendente"].pk))
        self.assertIn("ainda não teve uma consulta de sessão com resposta válida", html)
        self.assertIn("nunca consultado", html)
        self.assertNotIn("Ver resposta completa", html)

    def test_detalhe_nao_interpretado_mostra_o_valor_bruto_e_aponta_o_mapeamento(self):
        html = self.html(self.get("m2m_chip_detalhe", self.c["nao_lido"].pk))
        self.assertIn("não sabe lê-la", html)
        self.assertIn("XYZ", html)
        self.assertIn(reverse("m2m_config") + "#mapeamento", html)

    def test_detalhe_do_chip_desativado_mostra_motivo_e_oferece_reativar(self):
        html = self.html(self.get("m2m_chip_detalhe", self.c["desativado"].pk))
        self.assertIn("Monitoramento desativado", html)
        self.assertIn("Chip devolvido", html)
        self.assertIn("Reativar monitoramento", html)
        self.assertNotIn("Atualizar tudo", html)

    def test_detalhe_do_chip_ativo_tem_as_quatro_secoes_e_as_acoes(self):
        html = self.html(self.get("m2m_chip_detalhe", self.c["ativo"].pk))
        for secao in ("Conectividade e sessão", "Consumo", "Histórico", "Dados cadastrais"):
            self.assertIn(secao, html)
        for botao in ("Atualizar sessão", "Atualizar consumo", "Atualizar tudo", "Ver resposta completa"):
            self.assertIn(botao, html)
        self.assertIn("m2m-fita", html)
        self.assertIn("250,5 MB", html)

    def test_quem_so_ve_nao_recebe_os_botoes_de_operar(self):
        cliente = self.cliente_de(self.usuario_com("so_olha", "ver_chips"))
        html = cliente.get(reverse("m2m_chip_detalhe", args=[self.c["ativo"].pk])).content.decode("utf-8")
        for botao in ("Atualizar tudo", "Editar cadastro", "Desativar o monitoramento"):
            self.assertNotIn(botao, html)
        lista = cliente.get(reverse("m2m_chips")).content.decode("utf-8")
        self.assertNotIn("data-m2m-atualizar", lista)
        self.assertNotIn("Novo chip", lista)

    def test_grafico_so_com_tres_consultas_lidas(self):
        chip = self.c["ativo"]
        self.assertNotIn("m2m-grafico-consumo", self.html(self.get("m2m_chip_detalhe", chip.pk)))
        for _ in range(2):
            self.atualizar(chip, base.roteador(), tipos=("consumo",), origem="agendada")
        html = self.html(self.get("m2m_chip_detalhe", chip.pk))
        self.assertIn('id="m2m-grafico-consumo"', html)
        self.assertIn('id="m2m-serie"', html)
        self.assertIn("chart.js@4.4.3", html)
        dados = json.loads(re.search(r'<script id="m2m-serie" type="application/json">(.*?)</script>', html, re.S).group(1))
        self.assertTrue(dados["suficiente"])
        self.assertEqual(len(dados["pontos"]), 3)

    def test_visao_geral_traz_a_regua_a_conectividade_e_o_consumo_com_a_origem_dita(self):
        html = self.html(self.get("m2m_visao_geral"))
        for rotulo in ("Chips monitorados", "Sessão ativa confirmada", "Sem sessão identificada", "Não interpretado",
                       "Consulta pendente", "Falha de consulta", "Desatualizados", "Precisam de atenção"):
            self.assertIn(rotulo, html)
        self.assertIn("m2m-empilhada", html)
        self.assertIn("Chips com falha de consulta", html)
        self.assertIn("Últimas consultas", html)
        self.assertIn("Eventos recentes", html)
        self.assertIn("último volume informado", html)
        self.assertIn("Não é o consumo do mês, nem saldo", html)
        self.assertNotIn("% da franquia", html)

    def test_visao_geral_os_numeros_da_regua_sao_os_do_servico(self):
        k = m2m.indicadores()
        html = self.html(self.get("m2m_visao_geral"))
        self.assertEqual(k["monitorados"], 5)
        for chave in ("monitorados", "sessao_ativa", "sem_sessao", "nao_interpretado", "falha_consulta"):
            self.assertRegex(html, rf'm2m-kpi-n m2m-num">{k[chave]}<')

    def test_faixa_de_saude_aparece_com_o_estado_da_integracao(self):
        html = self.html(self.get("m2m_visao_geral"))
        self.assertIn('data-estado="autenticada"', html)
        self.assertIn("Autenticada às", html)
        self.assertIn("Última sincronização", html)
        self.assertIn("Tarefa agendada", html)

    def test_faixa_avisa_quando_a_credencial_foi_recusada(self):
        M2MIntegracao.objects.filter(pk=1).update(falha_autenticacao_em=timezone.now(), autenticada_em=timezone.now() - timedelta(days=1))
        html = self.html(self.get("m2m_chips"))
        self.assertIn('data-estado="credencial_recusada"', html)
        self.assertIn("A API recusou o token", html)

    def test_faixa_avisa_quando_a_tarefa_agendada_parou(self):
        M2MIntegracao.objects.filter(pk=1).update(ultima_execucao=timezone.now() - timedelta(hours=5), tique_minutos=15)
        self.assertIn("Parada", self.html(self.get("m2m_visao_geral")))

    def test_sem_mapeamento_a_tela_diz_isso_e_aponta_a_configuracao(self):
        M2MIntegracao.objects.filter(pk=1).update(mapeamento={})
        for nome, trecho in (
            ("m2m_visao_geral", "Nenhum mapeamento da resposta configurado"),
            ("m2m_chips", "Sem o mapeamento da sessão"),
            ("m2m_sessoes", "O estado da sessão ainda não está mapeado"),
            ("m2m_consumo", "O volume de consumo ainda não está mapeado"),
        ):
            with self.subTest(tela=nome):
                html = self.html(self.get(nome))
                self.assertIn(trecho, html)
                self.assertIn(reverse("m2m_config") + "#mapeamento", html)

    def test_sem_mapeamento_quem_nao_configura_nao_ganha_link_para_a_configuracao(self):
        M2MIntegracao.objects.filter(pk=1).update(mapeamento={})
        html = self.cliente_de(self.usuario_com("sem_cfg", "ver")).get(reverse("m2m_chips")).content.decode("utf-8")
        self.assertIn("Sem o mapeamento da sessão", html)
        self.assertNotIn(reverse("m2m_config"), html)

    def test_sessoes_separa_falha_de_consulta_de_sem_sessao(self):
        html = self.html(self.get("m2m_sessoes"))
        self.assertIn("Estado atual por chip", html)
        self.assertIn("Mudanças de estado recentes", html)
        self.assertIn("Falhas de consulta de sessão", html)
        self.assertIn("Não são “sem sessão”", html)
        self.assertIn("Chip Sem Sessao", html)

    def test_consumo_mostra_valor_original_normalizado_e_a_comparacao_sem_franquia(self):
        html = self.html(self.get("m2m_consumo"))
        self.assertIn("250,5 MB", html)             # o volume normalizado, em pt-BR
        self.assertIn("250.5 MB", html)             # e o valor ORIGINAL, como a API mandou
        self.assertIn("Comparação entre períodos", html)
        self.assertIn("não calcula saldo nem percentual", html)
        self.assertNotIn("% da franquia", html)

    def test_alertas_lista_a_ocorrencia_aberta_com_historico_e_acoes(self):
        html = self.html(self.get("m2m_alertas"))
        self.assertIn("Falhas de consulta seguidas", html)
        self.assertIn("Chip Com Falha", html)
        self.assertIn("É uma falha de CONSULTA", html)
        self.assertIn("Detectada automaticamente pelo monitoramento", html)
        for botao in ("Assumir", "Resolver", "Descartar"):
            self.assertIn(botao, html)

    def test_config_mostra_a_amostra_real_e_os_campos_como_datalist(self):
        html = self.html(self.get("m2m_config"))
        self.assertIn('<datalist id="m2m-caminhos">', html)
        self.assertIn("sessao.estado", html)
        self.assertIn("uso.total", html)
        self.assertIn('value="sessao.estado"', html)           # o mapeamento salvo preenche o formulário
        self.assertIn("Prévia ao vivo", html)

    def test_config_sem_resposta_real_diz_que_nao_ha_amostra(self):
        M2MConsulta.objects.update(payload=None)   # o histórico é imutável, mas este teste só apaga o corpo
        html = self.html(self.get("m2m_config"))
        self.assertIn("nenhuma resposta com conteúdo guardada ainda", html)


# ═════════════════════════════════════════════════════════════
# 4. Lista: filtros, ordenação e paginação
# ═════════════════════════════════════════════════════════════

class ListaDeChipsTests(TelasBase):
    def nomes(self, resposta):
        return [c.nome for c in resposta.context["chips"]]

    def test_ordena_primeiro_quem_precisa_de_atencao(self):
        self.cenario()
        nomes = self.nomes(self.get("m2m_chips"))
        self.assertEqual(nomes[0], "Chip Com Falha")            # falha de consulta vem na frente
        self.assertEqual(nomes[-1], "Chip Desativado")          # desativado, por último
        self.assertLess(nomes.index("Chip Com Falha"), nomes.index("Chip Ativo"))

    def test_filtro_de_sessao_e_de_situacao(self):
        self.cenario()
        self.assertEqual(self.nomes(self.get("m2m_chips", sessao="sem_sessao")), ["Chip Sem Sessao"])
        self.assertEqual(self.nomes(self.get("m2m_chips", sessao="nao_interpretado")), ["Chip Nao Lido"])
        self.assertEqual(self.nomes(self.get("m2m_chips", situacao="erro_consulta")), ["Chip Com Falha"])
        self.assertEqual(self.nomes(self.get("m2m_chips", situacao="desativado")), ["Chip Desativado"])
        self.assertEqual(self.nomes(self.get("m2m_chips", situacao="pendente")), ["Chip Pendente"])
        self.assertCountEqual(self.nomes(self.get("m2m_chips", situacao="normal")), ["Chip Ativo", "Chip Sem Sessao", "Chip Nao Lido"])

    def test_filtro_de_conectividade_e_exclusivo(self):
        """O chip com falha tem sessão 'ativa' guardada, mas fica SÓ em 'falha_consulta'."""
        self.cenario()
        self.assertEqual(self.nomes(self.get("m2m_chips", conectividade="ativa")), ["Chip Ativo"])
        self.assertEqual(self.nomes(self.get("m2m_chips", conectividade="falha_consulta")), ["Chip Com Falha"])
        self.assertEqual(self.nomes(self.get("m2m_chips", conectividade="nao_consultado")), ["Chip Pendente"])

    def test_busca_por_nome_iccid_agrupado_e_imsi(self):
        self.cenario()
        self.assertEqual(self.nomes(self.get("m2m_chips", q="sem sessao")), ["Chip Sem Sessao"])
        agrupado = base.iccid_valido(2)
        agrupado = " ".join(agrupado[i:i + 4] for i in range(0, len(agrupado), 4))
        self.assertEqual(self.nomes(self.get("m2m_chips", q=agrupado)), ["Chip Sem Sessao"])
        self.assertEqual(self.nomes(self.get("m2m_chips", iccid=base.iccid_valido(3)[11:])), ["Chip Nao Lido"])
        m2m.editar_chip(M2MChip.objects.get(nome="Chip Ativo"), {"imsi": base.imsi_valido(77)}, self.usuario)
        self.assertEqual(self.nomes(self.get("m2m_chips", imsi=base.imsi_valido(77))), ["Chip Ativo"])

    def test_filtro_por_veiculo_unidade_e_setor(self):
        self.cenario()
        loc = Localidade.objects.create(local="Fazenda Norte")
        veiculo = self.veiculo(1, codigo="TR-01", nome="Trator Pesagem", placa="ABC1D23")
        chip = M2MChip.objects.get(nome="Chip Ativo")
        m2m.editar_chip(chip, {"localidade": loc.pk, "setor": "Pesagem"}, self.usuario)
        self.vincular(chip, veiculo)
        self.assertEqual(self.nomes(self.get("m2m_chips", veiculo="tr-01")), ["Chip Ativo"])
        self.assertEqual(self.nomes(self.get("m2m_chips", veiculo="pesagem")), ["Chip Ativo"])
        self.assertEqual(self.nomes(self.get("m2m_chips", veiculo="ABC-1D23")), ["Chip Ativo"])
        self.assertEqual(self.nomes(self.get("m2m_chips", veiculo=str(veiculo.pk))), ["Chip Ativo"])
        self.assertEqual(self.nomes(self.get("m2m_chips", q="TR-01")), ["Chip Ativo"])
        self.assertEqual(self.nomes(self.get("m2m_chips", localidade=loc.pk)), ["Chip Ativo"])
        self.assertEqual(self.nomes(self.get("m2m_chips", setor="pesag")), ["Chip Ativo"])
        self.assertEqual(self.nomes(self.get("m2m_chips", localidade=loc.pk + 999)), [])
        self.assertEqual(self.nomes(self.get("m2m_chips", veiculo="nao-existe")), [])
        html = self.html(self.get("m2m_chips"))
        self.assertIn("TR-01 · Trator Pesagem", html)                    # a coluna Veículo mostra o veículo atual

    def test_filtro_por_periodo_da_ultima_consulta(self):
        self.cenario()
        antigo = M2MChip.objects.get(nome="Chip Ativo")
        M2MChip.objects.filter(pk=antigo.pk).update(ultima_tentativa_em=timezone.now() - timedelta(days=10))
        hoje = timezone.localdate()
        self.assertNotIn("Chip Ativo", self.nomes(self.get("m2m_chips", de=hoje.isoformat())))
        self.assertIn("Chip Ativo", self.nomes(self.get("m2m_chips", ate=(hoje - timedelta(days=5)).isoformat())))
        self.assertEqual(self.nomes(self.get("m2m_chips", de=(hoje + timedelta(days=3)).isoformat())), [])

    def test_valores_invalidos_nos_filtros_sao_ignorados_sem_quebrar(self):
        self.cenario()
        r = self.get("m2m_chips", sessao="lixo", situacao="lixo", localidade="abc", de="31/02/2026", ate="x", ordem="xyz", page="zzz")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.context["chips"]), 6)

    def test_ordenacao_por_nome_consumo_e_consulta(self):
        self.cenario()
        nomes = self.nomes(self.get("m2m_chips", ordem="nome"))
        self.assertEqual(nomes, sorted(nomes))
        self.assertEqual(self.nomes(self.get("m2m_chips", ordem="consumo"))[0], "Chip Ativo")   # só ele (e o com falha) têm volume
        self.assertEqual(self.nomes(self.get("m2m_chips", ordem="consulta"))[-1], "Chip Pendente")

    def test_paginacao_25_por_pagina_e_filtros_nos_links(self):
        for n in range(1, 31):
            self.chip(n, nome=f"Lote {n:02d}")
        r1 = self.get("m2m_chips", q="Lote")
        self.assertEqual(len(r1.context["chips"]), 25)
        self.assertEqual(r1.context["page_obj"].paginator.num_pages, 2)
        html = r1.content.decode("utf-8")
        self.assertIn("1–25 de 30", html)
        self.assertIn("?q=Lote&amp;page=2", html)                   # o filtro acompanha a página
        r2 = self.get("m2m_chips", q="Lote", page=2)
        self.assertEqual(len(r2.context["chips"]), 5)
        self.assertIn("26–30 de 30", r2.content.decode("utf-8"))
        self.assertEqual(len(self.get("m2m_chips", q="Lote", page=99).context["chips"]), 5)   # além do fim = última página

    def test_filtros_ativos_viram_etiquetas_removiveis(self):
        self.cenario()
        html = self.html(self.get("m2m_chips", sessao="ativa", q="chip"))
        self.assertIn("Sessão: Sessão ativa", html)
        self.assertIn("Busca: chip", html)
        self.assertIn("Remover o filtro", html)

    def test_lista_sem_resultado_tem_estado_vazio_com_saida(self):
        self.cenario()
        html = self.html(self.get("m2m_chips", q="nada-disso-existe"))
        self.assertIn("Nenhum resultado para os filtros aplicados", html)
        self.assertIn("Limpar filtros", html)

    def test_numero_de_consultas_ao_banco_nao_cresce_com_o_numero_de_linhas(self):
        """Sem N+1: a lista com 3 chips e a com 20 fazem o mesmo número de consultas."""
        for n in range(1, 4):
            self.chip(n)
        self.get("m2m_chips")                      # aquecimento: a 1ª requisição carrega o que as outras reaproveitam
        with CaptureQueriesContext(connection) as poucas:
            self.get("m2m_chips")
        for n in range(4, 21):
            self.chip(n)
        with CaptureQueriesContext(connection) as muitas:
            self.get("m2m_chips")
        self.assertEqual(len(poucas), len(muitas))


# ═════════════════════════════════════════════════════════════
# 5. Atualizar (JSON)
# ═════════════════════════════════════════════════════════════

class AtualizarJsonTests(TelasBase):
    def setUp(self):
        super().setUp()
        self.mapear()
        self.chip_ = self.chip(1, nome="Chip JSON")

    def atualizar_json(self, corpo=None, chip=None, **kw):
        return self.client.post(
            reverse("m2m_chip_atualizar", args=[(chip or self.chip_).pk]),
            data=json.dumps(corpo if corpo is not None else {"tipos": ["sessao", "consumo"]}),
            content_type="application/json", **kw,
        )

    def test_devolve_o_json_esperado_e_grava_as_consultas(self):
        sessao = self.api_falsa(base.roteador())
        r = self.atualizar_json({"tipos": ["sessao", "consumo"], "contexto": "detalhe"})
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertTrue(d["ok"])
        self.assertFalse(d["recusada"])
        self.assertEqual(d["tom"], "ok")
        self.assertEqual(d["mensagem"], "Sessão: sucesso. Consumo: sucesso.")
        self.assertEqual([c["tipo"] for c in d["consultas"]], ["sessao", "consumo"])
        self.assertTrue(all(c["valida"] and c["http_status"] == 200 for c in d["consultas"]))
        self.assertEqual(d["chip"]["sessao_estado"], "ativa")
        self.assertEqual(d["chip"]["consumo_bytes"], 250_500_000)
        self.assertEqual(d["chip"]["falhas_consecutivas"], 0)
        self.assertNotIn("html_linha", d)                          # só a lista pede a linha
        self.assertEqual(len(sessao.chamadas), 2)
        self.assertEqual(M2MConsulta.objects.filter(chip=self.chip_, origem="manual").count(), 2)

    def test_na_lista_devolve_a_linha_ja_renderizada(self):
        self.api_falsa(base.roteador())
        d = self.atualizar_json({"tipos": ["sessao", "consumo"], "contexto": "linha"}).json()
        linha = d["html_linha"]
        self.assertTrue(linha.lstrip().startswith("<tr"))
        self.assertIn(f'data-chip-id="{self.chip_.pk}"', linha)
        self.assertIn('data-estado="ativa"', linha)
        self.assertIn("250,5 MB", linha)
        self.sem_lixo_de_template(linha, "html_linha")

    def test_atualizar_so_um_tipo(self):
        sessao = self.api_falsa(base.roteador())
        d = self.atualizar_json({"tipos": ["consumo"]}).json()
        self.assertEqual([c["tipo"] for c in d["consultas"]], ["consumo"])
        self.assertTrue(sessao.urls[0].endswith("/usage"))
        self.chip_.refresh_from_db()
        self.assertEqual(self.chip_.sessao_estado, "nao_consultado")

    def test_sem_tipos_consulta_os_dois(self):
        self.api_falsa(base.roteador())
        self.assertEqual(len(self.atualizar_json({}).json()["consultas"]), 2)

    def test_a_api_falhando_e_resultado_nao_erro_de_http(self):
        self.api_falsa(base.roteador(status=500))
        r = self.atualizar_json()
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertFalse(d["ok"])
        self.assertEqual(d["tom"], "erro")
        self.assertEqual(d["chip"]["falhas_consecutivas"], 1)
        self.assertEqual(d["chip"]["sessao_estado"], "nao_consultado")     # falha não inventa estado
        self.assertIn("api indisponível", d["mensagem"])

    def test_falha_nao_apaga_o_ultimo_dado_valido(self):
        self.api_falsa(base.roteador())
        self.atualizar_json()
        self.client.force_login(self.admin)
        M2MChip.objects.filter(pk=self.chip_.pk).update(ultima_tentativa_em=timezone.now() - timedelta(minutes=5))
        self.api_falsa(base.roteador(status=500))
        d = self.atualizar_json().json()
        self.assertFalse(d["ok"])
        self.assertEqual(d["chip"]["sessao_estado"], "ativa")
        self.assertEqual(d["chip"]["consumo_bytes"], 250_500_000)

    def test_segunda_atualizacao_em_menos_de_60s_e_recusada(self):
        self.api_falsa(base.roteador())
        self.assertEqual(self.atualizar_json().status_code, 200)
        r = self.atualizar_json()
        self.assertEqual(r.status_code, 409)
        d = r.json()
        self.assertFalse(d["ok"])
        self.assertTrue(d["recusada"])
        self.assertEqual(d["codigo"], "aguardar")
        self.assertGreater(d["aguardar_s"], 0)
        self.assertIn("Aguarde", d["mensagem"])
        self.assertEqual(M2MConsulta.objects.filter(chip=self.chip_).count(), 2)       # a segunda nem foi tentada

    def test_sem_token_a_consulta_nem_e_tentada(self):
        sessao = self.api_falsa(base.roteador())
        with override_settings(TIM_M2M_TOKEN=""):
            r = self.atualizar_json()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["codigo"], "configuracao")
        self.assertIn("Nenhum token configurado", r.json()["mensagem"])
        self.assertEqual(sessao.chamadas, [])

    def test_api_bloqueada_por_limite_recusa(self):
        M2MIntegracao.objects.filter(pk=1).update(bloqueado_ate=timezone.now() + timedelta(minutes=10))
        self.api_falsa(base.roteador())
        r = self.atualizar_json()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["codigo"], "bloqueada")

    def test_chip_desativado_recusa(self):
        m2m.desativar(self.chip_, self.usuario, "teste")
        self.api_falsa(base.roteador())
        r = self.atualizar_json()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["codigo"], "desativado")

    def test_corpo_ou_tipos_invalidos_dao_400(self):
        self.api_falsa(base.roteador())
        for corpo in ("isto nao e json", json.dumps([1]), json.dumps({"tipos": "sessao"}), json.dumps({"tipos": ["sessao", "outro"]}),
                      json.dumps({"tipos": [1]})):
            with self.subTest(corpo=corpo):
                r = self.client.post(reverse("m2m_chip_atualizar", args=[self.chip_.pk]), data=corpo, content_type="application/json")
                self.assertEqual(r.status_code, 400)
                self.assertFalse(r.json()["ok"])

    def test_chip_inexistente_da_404(self):
        self.assertEqual(self.client.post(reverse("m2m_chip_atualizar", args=[999999]), data="{}", content_type="application/json").status_code, 404)

    def test_quem_so_ve_nao_consegue_atualizar(self):
        sessao = self.api_falsa(base.roteador())
        cliente = self.cliente_de(self.usuario_com("so_ve_json", "ver_chips", "ver_historico_frota"))
        r = cliente.post(reverse("m2m_chip_atualizar", args=[self.chip_.pk]), data="{}", content_type="application/json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(sessao.chamadas, [])


class PayloadJsonTests(TelasBase):
    def test_devolve_a_resposta_guardada_e_nao_consulta_a_api(self):
        self.mapear()
        chip = self.chip(1)
        sessao = self.api_falsa(base.roteador())
        self.atualizar(chip, base.roteador(), origem="manual")
        antes = len(sessao.chamadas)
        consulta = M2MChip.objects.get(pk=chip.pk).sessao_consulta
        r = self.client.get(reverse("m2m_consulta_payload", args=[consulta.pk]))
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["payload"], base.SESSAO_UP)
        self.assertEqual(d["chip"]["nome"], chip.nome)
        self.assertEqual(d["tipo"], "sessao")
        self.assertTrue(d["valida"])
        self.assertEqual(d["estado_interpretado"], "ativa")
        self.assertEqual(len(sessao.chamadas), antes)

    def test_consulta_que_falhou_vem_sem_payload_e_com_a_mensagem(self):
        import requests

        chip = self.chip(1)
        self.atualizar(chip, lambda url: requests.Timeout("lento"), origem="manual")
        consulta = chip.consultas.first()
        d = self.client.get(reverse("m2m_consulta_payload", args=[consulta.pk])).json()
        self.assertFalse(d["valida"])
        self.assertIsNone(d["payload"])
        self.assertIn("Tempo esgotado", d["mensagem_erro"])

    def test_consulta_inexistente_da_404(self):
        self.assertEqual(self.client.get(reverse("m2m_consulta_payload", args=[999999])).status_code, 404)


# ═════════════════════════════════════════════════════════════
# 6. Cadastro, edição e ativar/desativar
# ═════════════════════════════════════════════════════════════

class ChipFormularioTests(TelasBase):
    def dados(self, **extra):
        valores = {
            "nome": "Chip Novo", "iccid": " ".join([base.iccid_valido(9)[i:i + 4] for i in range(0, 19, 4)]),
            "imsi": "", "identificador_consulta": "iccid", "descricao": "Pesagem", "setor": "Algodoeira",
            "localidade": "", "responsavel": "", "limite_valor": "", "limite_unidade": "GB", "observacoes": "",
        }
        valores.update(extra)
        return valores

    def test_cadastra_normalizando_o_iccid_e_grava_o_evento(self):
        r = self.client.post(reverse("m2m_chip_novo"), self.dados())
        chip = M2MChip.objects.get(nome="Chip Novo")
        self.assertRedirects(r, reverse("m2m_chip_detalhe", args=[chip.pk]))
        self.assertEqual(chip.iccid, base.iccid_valido(9))
        self.assertTrue(chip.monitorado)
        self.assertEqual(chip.criado_por, self.admin)
        self.assertTrue(M2MEvento.objects.filter(chip=chip, tipo="cadastro", usuario=self.admin).exists())

    def test_iccid_invalido_volta_com_a_mensagem_e_o_que_foi_digitado(self):
        r = self.client.post(reverse("m2m_chip_novo"), self.dados(iccid="8955100000000000019"))
        self.assertEqual(r.status_code, 400)
        html = r.content.decode("utf-8")
        self.assertIn("dígito verificador não confere", html)
        self.assertIn('value="Chip Novo"', html)
        self.assertIn('value="8955100000000000019"', html)
        self.assertFalse(M2MChip.objects.filter(nome="Chip Novo").exists())

    def test_nome_duplicado_e_recusado(self):
        self.chip(1, nome="Chip Novo")
        r = self.client.post(reverse("m2m_chip_novo"), self.dados(iccid=base.iccid_valido(30)))
        self.assertEqual(r.status_code, 400)
        self.assertIn("Já existe um chip com este nome", r.content.decode("utf-8"))

    def test_identificador_de_consulta_exige_o_campo_preenchido(self):
        r = self.client.post(reverse("m2m_chip_novo"), self.dados(identificador_consulta="imsi", imsi=""))
        self.assertEqual(r.status_code, 400)
        self.assertIn("Informe o IMSI", r.content.decode("utf-8"))

    def test_limite_mensal_e_convertido_para_bytes_com_a_base_do_mapeamento(self):
        self.client.post(reverse("m2m_chip_novo"), self.dados(limite_valor="1,5", limite_unidade="GB"))
        self.assertEqual(M2MChip.objects.get(nome="Chip Novo").limite_mensal_bytes, 1_500_000_000)
        self.mapear({"consumo": {"campo_volume": "uso.total", "unidade_fixa": "MB", "base": 1024}})
        self.client.post(reverse("m2m_chip_novo"), self.dados(nome="Chip Binario", iccid=base.iccid_valido(11), limite_valor="2", limite_unidade="GB"))
        self.assertEqual(M2MChip.objects.get(nome="Chip Binario").limite_mensal_bytes, 2 * 1024 ** 3)

    def test_limite_invalido_e_recusado(self):
        for valor in ("abc", "0", "-3"):
            with self.subTest(valor=valor):
                r = self.client.post(reverse("m2m_chip_novo"), self.dados(limite_valor=valor))
                self.assertEqual(r.status_code, 400)
        self.assertFalse(M2MChip.objects.filter(nome="Chip Novo").exists())

    def test_edicao_mostra_o_limite_na_unidade_certa_e_grava_a_diferenca(self):
        chip = self.chip(1, nome="Chip Edit", limite_mensal_bytes=5_000_000_000)
        html = self.html(self.get("m2m_chip_editar", chip.pk))
        self.assertIn('name="limite_valor" value="5"', html)
        self.assertIn('<option value="GB" selected>', html)
        r = self.client.post(reverse("m2m_chip_editar", args=[chip.pk]), self.dados(
            nome="Chip Edit", iccid=chip.iccid, descricao="Mudou", limite_valor="5", limite_unidade="GB",
        ))
        self.assertRedirects(r, reverse("m2m_chip_detalhe", args=[chip.pk]))
        chip.refresh_from_db()
        self.assertEqual(chip.descricao, "Mudou")
        evento = M2MEvento.objects.filter(chip=chip, tipo="edicao").latest("pk")
        self.assertEqual(set(evento.dados["alteracoes"]), {"descricao", "setor"})

    def test_formulario_nunca_tem_campo_de_situacao_atual(self):
        html = self.html(self.get("m2m_chip_novo"))
        for campo in ("sessao_estado", "consumo_bytes", "falhas_consecutivas", "monitorado"):
            self.assertNotIn(f'name="{campo}"', html)

    def test_desativar_exige_motivo_e_nao_apaga_o_historico(self):
        chip = self.chip(1)
        self.atualizar(chip, base.roteador(), origem="manual")
        r = self.client.post(reverse("m2m_chip_monitoramento", args=[chip.pk]), {"acao": "desativar", "motivo": "  "}, follow=True)
        self.assertIn("Informe o motivo", "".join(str(m) for m in r.context["messages"]))
        self.assertTrue(M2MChip.objects.get(pk=chip.pk).monitorado)
        self.client.post(reverse("m2m_chip_monitoramento", args=[chip.pk]), {"acao": "desativar", "motivo": "Chip devolvido"})
        chip.refresh_from_db()
        self.assertFalse(chip.monitorado)
        self.assertEqual(chip.desativado_motivo, "Chip devolvido")
        self.assertEqual(chip.consultas.count(), 2)                         # histórico intacto
        self.client.post(reverse("m2m_chip_monitoramento", args=[chip.pk]), {"acao": "reativar", "motivo": "Voltou"})
        chip.refresh_from_db()
        self.assertTrue(chip.monitorado)

    def test_acao_desconhecida_nao_faz_nada(self):
        chip = self.chip(1)
        self.client.post(reverse("m2m_chip_monitoramento", args=[chip.pk]), {"acao": "excluir"})
        self.assertTrue(M2MChip.objects.filter(pk=chip.pk, monitorado=True).exists())

    def test_opcoes_do_select2_para_veiculo_e_responsavel(self):
        cc = CentroCusto.objects.create(numero="9", departamento="TI")
        loc = Localidade.objects.create(local="Fazenda Norte")
        veiculo = self.veiculo(1, codigo="CM-07", nome="Caminhão Pipa", modelo="Atego")
        Usuario.objects.create(nome="Maria Souza", status="ativo", pmb=SimNaoChoices.NAO, centro_custo=cc, localidade=loc)
        Usuario.objects.create(nome="Maria Desligada", status="desligado", pmb=SimNaoChoices.NAO, centro_custo=cc, localidade=loc)
        r = self.client.get(reverse("m2m_opcoes"), {"campo": "veiculo", "q": "caminh"}).json()
        self.assertEqual([o["id"] for o in r["results"]], [veiculo.pk])
        self.assertIn("CM-07", r["results"][0]["text"])
        r = self.client.get(reverse("m2m_opcoes"), {"campo": "responsavel", "q": "maria"}).json()
        self.assertEqual([o["text"] for o in r["results"]], ["Maria Souza"])          # desligado não entra
        self.assertEqual(self.client.get(reverse("m2m_opcoes"), {"campo": "veiculo", "q": "c"}).json()["results"], [])
        self.assertEqual(self.client.get(reverse("m2m_opcoes"), {"campo": "item", "q": "roteador"}).status_code, 400)   # o antigo Equipamento saiu
        self.assertEqual(self.client.get(reverse("m2m_opcoes"), {"campo": "outro", "q": "roteador"}).status_code, 400)

    def test_o_formulario_do_chip_nao_tem_mais_o_campo_equipamento(self):
        chip = self.chip(1)
        for html in (self.html(self.get("m2m_chip_novo")), self.html(self.get("m2m_chip_editar", chip.pk))):
            self.assertNotIn('name="item"', html)
            self.assertNotIn("Buscar equipamento", html)
            self.assertNotIn('data-campo="item"', html)


# ═════════════════════════════════════════════════════════════
# 7. Alertas
# ═════════════════════════════════════════════════════════════

class AlertasTests(TelasBase):
    def setUp(self):
        super().setUp()
        self.chip_ = self.chip(1, nome="Chip Alerta")
        self.falhar(self.chip_, 3)
        m2m.avaliar_ocorrencias(self.chip_, notificar=False)
        self.oc = M2MOcorrencia.objects.get(chip=self.chip_, tipo="falha_recorrente")

    def tratar(self, acao, comentario="", **extra):
        return self.client.post(reverse("m2m_alerta_tratar", args=[self.oc.pk]), {"acao": acao, "comentario": comentario, **extra})

    def test_ocorrencia_de_veiculo_sem_chip_aparece_na_tela_sem_quebrar(self):
        """Ocorrência de VEÍCULO não tem chip: a tela (ajuste mínimo da Fase D) mostra o veículo no lugar do link."""
        v = self.veiculo(1, codigo="TR-77", nome="Trator Esmeralda")
        M2MOcorrencia.objects.create(veiculo=v, tipo="veiculo_sem_chip", nivel="atencao", descricao="Sem chip nenhum")
        html = self.html(self.get("m2m_alertas"))
        self.assertIn("TR-77 · Trator Esmeralda", html)
        self.assertIn("Veículo sem chip", html)
        self.assertIn("Chip Alerta", html)                                           # a do chip continua lá
        self.sem_lixo_de_template(html, "alertas")

    def test_busca_de_alertas_acha_pelo_veiculo(self):
        v = self.veiculo(1, codigo="TR-77", nome="Trator Esmeralda", placa="ABC1234")
        M2MOcorrencia.objects.create(veiculo=v, tipo="veiculo_sem_chip", nivel="atencao", descricao="Sem chip nenhum")
        for busca in ("TR-77", "esmeralda", "abc-1234"):
            with self.subTest(q=busca):
                tipos = [o.tipo for o in m2m.listar_ocorrencias({"q": busca})]
                self.assertEqual(tipos, ["veiculo_sem_chip"])

    def test_assumir_resolver_e_o_historico_de_tratamento(self):
        self.tratar("assumir")
        self.oc.refresh_from_db()
        self.assertEqual(self.oc.estado, "em_tratamento")
        self.tratar("resolver", "Trocado o cabo")
        self.oc.refresh_from_db()
        self.assertEqual(self.oc.estado, "resolvida")
        self.assertEqual(self.oc.resolvida_por, self.admin)
        acoes = list(self.oc.tratamentos.values_list("acao", flat=True))
        self.assertEqual(acoes, ["detectada", "assumir", "resolver"])
        html = self.html(self.get("m2m_alertas", estado="todas"))
        self.assertIn("Trocado o cabo", html)

    def test_resolver_ou_descartar_sem_comentario_e_recusado(self):
        for acao in ("resolver", "descartar"):
            r = self.tratar(acao, "   ", voltar=reverse("m2m_alertas"))
            self.assertEqual(r.status_code, 302)
            self.oc.refresh_from_db()
            self.assertEqual(self.oc.estado, "aberta")
        r = self.client.post(reverse("m2m_alerta_tratar", args=[self.oc.pk]), {"acao": "resolver", "comentario": ""}, follow=True)
        self.assertIn("Explique, no comentário", "".join(str(m) for m in r.context["messages"]))

    def test_descartar_com_comentario_encerra(self):
        self.tratar("descartar", "Já sei disso")
        self.oc.refresh_from_db()
        self.assertEqual(self.oc.estado, "descartada")

    def test_so_comentar_nao_muda_o_estado(self):
        self.tratar("comentar", "Liguei para a TIM")
        self.oc.refresh_from_db()
        self.assertEqual(self.oc.estado, "aberta")
        self.assertTrue(self.oc.tratamentos.filter(acao="comentar", comentario="Liguei para a TIM").exists())

    def test_acao_desconhecida_e_recusada(self):
        self.tratar("excluir")
        self.oc.refresh_from_db()
        self.assertEqual(self.oc.estado, "aberta")

    def test_voltar_so_aceita_caminho_do_modulo(self):
        r = self.tratar("comentar", "x", voltar="https://evil.example/roubo")
        self.assertEqual(r["Location"], reverse("m2m_alertas") + f"?abrir={self.oc.pk}")
        r = self.tratar("comentar", "x", voltar="//evil.example/roubo")
        self.assertTrue(r["Location"].startswith("/agro/conectividade/"))
        r = self.tratar("comentar", "x", voltar="/dashboard/")
        self.assertTrue(r["Location"].startswith("/agro/conectividade/"))
        r = self.tratar("comentar", "x", voltar="/agro/conectividade/alertas/?estado=todas")
        self.assertEqual(r["Location"], "/agro/conectividade/alertas/?estado=todas")
        # endereço antigo (aba aberta antes da mudança) ainda é aceito: ele responde 308 para o novo
        r = self.tratar("comentar", "x", voltar="/m2m/alertas/?estado=todas")
        self.assertEqual(r["Location"], "/m2m/alertas/?estado=todas")

    def test_filtros_da_lista_de_alertas(self):
        outro = self.chip(2, nome="Chip Outro")
        M2MOcorrencia.objects.create(chip=outro, tipo="desatualizado", nivel="critico", descricao="velho")
        self.assertEqual([o.chip.nome for o in self.get("m2m_alertas").context["ocorrencias"]], ["Chip Outro", "Chip Alerta"])   # crítica primeiro
        self.assertEqual([o.chip.nome for o in self.get("m2m_alertas", nivel="atencao").context["ocorrencias"]], ["Chip Alerta"])
        self.assertEqual([o.chip.nome for o in self.get("m2m_alertas", tipo="desatualizado").context["ocorrencias"]], ["Chip Outro"])
        self.assertEqual([o.chip.nome for o in self.get("m2m_alertas", q="alerta").context["ocorrencias"]], ["Chip Alerta"])
        self.assertEqual(len(self.get("m2m_alertas", estado="encerradas").context["ocorrencias"]), 0)
        self.tratar("resolver", "ok")
        self.assertEqual(len(self.get("m2m_alertas", estado="encerradas").context["ocorrencias"]), 1)
        self.assertEqual(len(self.get("m2m_alertas", estado="todas").context["ocorrencias"]), 2)
        self.assertEqual(len(self.get("m2m_alertas", estado="lixo").context["ocorrencias"]), 1)   # inválido = abertas

    def test_o_contador_da_aba_acompanha_as_ocorrencias_abertas(self):
        html = self.html(self.get("m2m_visao_geral"))
        self.assertRegex(html, r'Alertas e ocorrências <span class="m2m-contagem"[^>]*>1</span>')
        self.tratar("resolver", "ok")
        self.assertNotIn("m2m-contagem", self.html(self.get("m2m_visao_geral")))

    def test_quem_so_ve_nao_tem_o_formulario_de_tratar(self):
        html = self.cliente_de(self.usuario_com("so_ve_alerta", "ver")).get(reverse("m2m_alertas")).content.decode("utf-8")
        self.assertIn("Chip Alerta", html)
        self.assertNotIn("m2m-tratar", html)
        self.assertNotIn("Resolver", html)

    def test_abrir_o_detalhe_pelo_link_da_ocorrencia(self):
        html = self.html(self.get("m2m_alertas", abrir=self.oc.pk))
        self.assertRegex(html, rf'(?s)id="oc-{self.oc.pk}".*?<details open>')


# ═════════════════════════════════════════════════════════════
# 8. Configuração
# ═════════════════════════════════════════════════════════════

class ConfiguracaoTelasTests(TelasBase):
    def test_salvar_token_cifra_mostra_so_o_final_e_nunca_devolve_o_valor(self):
        with override_settings(TIM_M2M_TOKEN=""):
            r = self.client.post(reverse("m2m_config_token"), {"acao": "salvar", "token": _TOKEN_DO_BANCO})
            self.assertRedirects(r, reverse("m2m_config"))
            cfg = M2MIntegracao.get()
            self.assertNotIn(_TOKEN_DO_BANCO, cfg.token_cifrado)
            self.assertEqual(tim.token(), _TOKEN_DO_BANCO)
            html = self.html(self.get("m2m_config"))
        self.assertNotIn(_TOKEN_DO_BANCO, html)
        self.assertNotIn(cfg.token_cifrado, html)
        self.assertIn("····" + _TOKEN_DO_BANCO[-4:], html)
        self.assertIn("banco de dados (cifrado)", html)

    def test_campo_de_token_e_senha_sem_autocompletar_e_nunca_preenchido(self):
        html = self.html(self.get("m2m_config"))
        campo = re.search(r'<input[^>]*name="token"[^>]*>', html).group(0)
        self.assertIn('type="password"', campo)
        self.assertIn('autocomplete="off"', campo)
        self.assertNotIn("value=", campo)

    def test_token_do_env_vence_e_a_tela_avisa(self):
        tim.salvar_token(_TOKEN_DO_BANCO, self.admin)
        html = self.html(self.get("m2m_config"))
        self.assertIn("TIM_M2M_TOKEN está definido no .env e vence o token do banco", html)
        self.assertIn("arquivo .env do servidor", html)
        self.assertIn("····" + base.TOKEN[-4:], html)

    def test_sem_chave_de_cifra_a_tela_recusa_gravar_e_mostra_o_comando(self):
        with override_settings(TIM_M2M_TOKEN="", TIM_M2M_CHAVE_CIFRA=""):
            html = self.html(self.get("m2m_config"))
            self.assertIn("Falta a chave de cifra", html)
            self.assertIn("Fernet.generate_key()", html)
            r = self.client.post(reverse("m2m_config_token"), {"acao": "salvar", "token": _TOKEN_DO_BANCO})
        self.assertEqual(r.status_code, 400)
        self.assertIn("TIM_M2M_CHAVE_CIFRA", r.content.decode("utf-8"))
        self.assertNotIn(_TOKEN_DO_BANCO, r.content.decode("utf-8"))
        self.assertEqual(M2MIntegracao.get().token_cifrado, "")

    def test_token_invalido_volta_com_erro_sem_repetir_o_que_foi_digitado(self):
        digitado = "tem espaco no meio do token"
        r = self.client.post(reverse("m2m_config_token"), {"acao": "salvar", "token": digitado})
        self.assertEqual(r.status_code, 400)
        html = r.content.decode("utf-8")
        self.assertIn("Cole só o token", html)
        self.assertNotIn(digitado, html)

    def test_remover_token_do_banco(self):
        tim.salvar_token(_TOKEN_DO_BANCO, self.admin)
        self.client.post(reverse("m2m_config_token"), {"acao": "remover"})
        self.assertEqual(M2MIntegracao.get().token_cifrado, "")
        self.assertTrue(M2MEvento.objects.filter(tipo="token_alterado", dados__acao="removido").exists())

    def test_frequencia_valida_e_gravada_e_auditada(self):
        r = self.client.post(reverse("m2m_config_frequencia"), {"intervalo_minutos": "30", "pausa_entre_chamadas_ms": "1500"})
        self.assertRedirects(r, reverse("m2m_config"))
        cfg = M2MIntegracao.get()
        self.assertEqual((cfg.intervalo_minutos, cfg.pausa_entre_chamadas_ms), (30, 1500))
        self.assertTrue(M2MEvento.objects.filter(tipo="config_alterada").exists())

    def test_frequencia_invalida_volta_com_a_conta_e_com_o_valor_digitado(self):
        r = self.client.post(reverse("m2m_config_frequencia"), {"intervalo_minutos": "5"})
        self.assertEqual(r.status_code, 400)
        html = r.content.decode("utf-8")
        self.assertIn("O intervalo mínimo é de 15 minutos", html)
        self.assertRegex(html, r'name="intervalo_minutos" value="5"')
        self.assertEqual(M2MIntegracao.get().intervalo_minutos, 60)

    def test_frequencia_acima_do_limite_de_requisicoes_mostra_a_conta(self):
        for n in range(1, 11):
            self.chip(n)
        r = self.client.post(reverse("m2m_config_frequencia"), {"intervalo_minutos": "15", "limite_req_minuto": "1"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("req/min", r.content.decode("utf-8"))

    def test_simular_a_frequencia_devolve_a_conta_e_nao_grava(self):
        for n in range(1, 11):
            self.chip(n)
        r = self.client.post(reverse("m2m_config_frequencia"), {"simular": "1", "intervalo_minutos": "15", "limite_req_minuto": "30"})
        d = r.json()
        self.assertTrue(d["ok"])
        self.assertIn("2 consultas x 10 chips / 15 min", d["conta"])
        d = self.client.post(reverse("m2m_config_frequencia"), {"simular": "1", "intervalo_minutos": "15", "limite_req_minuto": "1"}).json()
        self.assertFalse(d["ok"])
        self.assertGreater(d["intervalo_minimo_sugerido"], 15)
        self.assertEqual(M2MIntegracao.get().intervalo_minutos, 60)
        for lixo in ({"intervalo_minutos": "abc"}, {"intervalo_minutos": "0"}, {}):
            self.assertFalse(self.client.post(reverse("m2m_config_frequencia"), {"simular": "1", **lixo}).json()["ok"])

    def test_limites_de_alerta_sao_gravados(self):
        self.client.post(reverse("m2m_config_frequencia"), {
            "falhas_para_alerta": "5", "horas_sem_sessao_alerta": "12", "horas_desatualizado": "48", "pct_consumo_alerta": "90",
        })
        cfg = M2MIntegracao.get()
        self.assertEqual((cfg.falhas_para_alerta, cfg.horas_sem_sessao_alerta, cfg.horas_desatualizado, cfg.pct_consumo_alerta), (5, 12, 48, 90))

    def post_mapa(self, **extra):
        dados = {
            "sessao__campo_estado": "sessao.estado", "sessao__valores_ativa": "UP\nONLINE", "sessao__valores_sem_sessao": "DOWN",
            "sessao__campos_exibir": "sessao.ip", "consumo__campo_volume": "uso.total", "consumo__campo_unidade": "",
            "consumo__unidade_fixa": "MB", "consumo__base": "1000", "consumo__campo_periodo_inicio": "uso.inicio",
            "consumo__campo_periodo_fim": "uso.fim", "identidade__campo_iccid": "sim.iccid", "identidade__campo_imsi": "",
        }
        dados.update(extra)
        return self.client.post(reverse("m2m_config_mapeamento"), dados)

    def test_mapeamento_e_validado_normalizado_gravado_e_auditado(self):
        r = self.post_mapa()
        self.assertRedirects(r, reverse("m2m_config") + "#mapeamento", fetch_redirect_response=False)
        mapa = M2MIntegracao.get().mapeamento
        self.assertEqual(mapa["sessao"]["valores_ativa"], ["UP", "ONLINE"])
        self.assertEqual(mapa["consumo"]["unidade_fixa"], "MB")
        self.assertEqual(mapa["consumo"]["base"], 1000)
        self.assertEqual(mapa["identidade"], {"campo_iccid": "sim.iccid"})
        self.assertTrue(M2MEvento.objects.filter(tipo="mapeamento_alterado").exists())

    def test_mapeamento_invalido_volta_com_o_erro_e_preserva_o_que_foi_digitado(self):
        r = self.post_mapa(**{"sessao__campo_estado": "a..b[", "consumo__unidade_fixa": "Mb"})
        self.assertEqual(r.status_code, 400)
        html = r.content.decode("utf-8")
        self.assertIn("caminho inválido", html)
        self.assertIn("unidade fixa não reconhecida", html)
        self.assertIn('value="a..b["', html)
        self.assertEqual(M2MIntegracao.get().mapeamento, {})

    def test_mapeamento_salvo_nao_reescreve_o_historico_mas_reinterpretar_reescreve(self):
        chip = self.chip(1)
        self.atualizar(chip, base.roteador(), origem="manual")
        self.assertEqual(M2MChip.objects.get(pk=chip.pk).sessao_estado, "nao_interpretado")
        self.post_mapa()
        self.assertEqual(M2MChip.objects.get(pk=chip.pk).sessao_estado, "nao_interpretado")     # salvar não mexe no passado
        r = self.client.post(reverse("m2m_reinterpretar"), follow=True)
        self.assertEqual(M2MChip.objects.get(pk=chip.pk).sessao_estado, "ativa")
        self.assertIn("Histórico reinterpretado", "".join(str(m) for m in r.context["messages"]))
        self.assertEqual(M2MChip.objects.get(pk=chip.pk).sessao_consulta.payload, base.SESSAO_UP)     # o payload nunca é tocado

    def test_previa_ao_vivo_usa_a_ultima_resposta_real_e_nao_grava(self):
        chip = self.chip(1)
        self.atualizar(chip, base.roteador(), origem="manual")
        corpo = {"mapeamento": {
            "sessao": {"campo_estado": "sessao.estado", "valores_ativa": "UP", "campos_exibir": "sessao.ip"},
            "consumo": {"campo_volume": "uso.total", "unidade_fixa": "MB"},
        }}
        r = self.client.post(reverse("m2m_config_previa"), data=json.dumps(corpo), content_type="application/json")
        d = r.json()
        self.assertEqual(d["erros"], [])
        self.assertEqual(d["sessao"]["estado"], "ativa")
        self.assertEqual(d["sessao"]["campos_exibir"], [{"caminho": "sessao.ip", "valor": "10.0.0.5"}])
        self.assertEqual(d["consumo"]["volume_bytes"], 250_500_000)
        self.assertEqual(d["amostras"]["sessao"]["chip"], chip.nome)
        self.assertEqual(M2MIntegracao.get().mapeamento, {})
        corpo["mapeamento"]["sessao"]["valores_ativa"] = "ONLINE"
        d = self.client.post(reverse("m2m_config_previa"), data=json.dumps(corpo), content_type="application/json").json()
        self.assertEqual(d["sessao"]["estado"], "nao_interpretado")
        self.assertEqual(d["sessao"]["estado_bruto"], "UP")

    def test_previa_de_mapeamento_invalido_devolve_so_os_erros(self):
        corpo = {"mapeamento": {"sessao": {"campo_estado": "a..b["}}}
        d = self.client.post(reverse("m2m_config_previa"), data=json.dumps(corpo), content_type="application/json").json()
        self.assertTrue(d["erros"])
        self.assertIsNone(d["sessao"])

    def test_previa_sem_resposta_guardada_diz_que_nao_ha_amostra(self):
        corpo = {"mapeamento": {"sessao": {"campo_estado": "x", "valores_ativa": "UP"}}}
        d = self.client.post(reverse("m2m_config_previa"), data=json.dumps(corpo), content_type="application/json").json()
        self.assertEqual(d["erros"], [])
        self.assertIsNone(d["sessao"])
        self.assertIsNone(d["amostras"]["sessao"])

    def test_previa_com_corpo_invalido_da_400(self):
        r = self.client.post(reverse("m2m_config_previa"), data=json.dumps({"mapeamento": "x"}), content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_teste_de_conexao_devolve_o_resultado_e_guarda_a_resposta(self):
        chip = self.chip(1, nome="Chip Teste")
        self.api_falsa(base.roteador())
        r = self.client.post(reverse("m2m_config_testar"), data=json.dumps({"chip_id": chip.pk}), content_type="application/json")
        d = r.json()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(d["ok"])
        self.assertEqual(d["tom"], "ok")
        self.assertEqual(d["resultado"], "sucesso")
        self.assertEqual(d["http_status"], 200)
        self.assertEqual(d["chip"], "Chip Teste")
        self.assertEqual(M2MConsulta.objects.get(pk=d["consulta_id"]).origem, "teste")
        self.assertEqual(M2MIntegracao.get().ultimo_teste["resultado"], "sucesso")
        html = self.html(self.get("m2m_config"))
        self.assertIn("Último teste", html)

    def test_teste_de_conexao_com_host_inexistente_mostra_erro_de_rede_honesto(self):
        import requests

        chip = self.chip(1)
        self.api_falsa([requests.ConnectionError("getaddrinfo failed")] * 5)
        d = self.client.post(reverse("m2m_config_testar"), data=json.dumps({"chip_id": chip.pk}), content_type="application/json").json()
        self.assertFalse(d["ok"])
        self.assertEqual(d["tom"], "erro")
        self.assertEqual(d["resultado"], "erro_rede")
        self.assertIn("host não encontrado", d["mensagem"])

    def test_teste_de_conexao_sem_token_e_sem_chip_e_recusado(self):
        chip = self.chip(1)
        sessao = self.api_falsa(base.roteador())
        with override_settings(TIM_M2M_TOKEN=""):
            r = self.client.post(reverse("m2m_config_testar"), data=json.dumps({"chip_id": chip.pk}), content_type="application/json")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(sessao.chamadas, [])
        r = self.client.post(reverse("m2m_config_testar"), data=json.dumps({}), content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_nenhuma_tela_aceita_url_da_api(self):
        html = self.html(self.get("m2m_config"))
        self.assertNotRegex(html, r'name="(url|base_url|endereco|host)"')
        self.assertIn("definido no .env", html)

    def test_tarefa_agendada_mostra_o_comando_e_o_estado(self):
        html = self.html(self.get("m2m_config"))
        self.assertIn("python manage.py agendar_m2m criar --intervalo 15", html)
        self.assertIn("Administrador", html)


# ═════════════════════════════════════════════════════════════
# 9. Segredo: o token não aparece em tela nem em JSON
# ═════════════════════════════════════════════════════════════

class SegredoNasTelasTests(TelasBase):
    """O token do ambiente, o do banco e o texto cifrado não saem em NENHUMA resposta."""

    def setUp(self):
        super().setUp()
        tim.salvar_token(_TOKEN_DO_BANCO, self.admin)
        self.cifrado = M2MIntegracao.get().token_cifrado
        self.segredos = (base.TOKEN, _TOKEN_DO_BANCO, self.cifrado)
        self.mapear()
        self.chip_ = self.chip(1, nome="Chip Segredo")
        # A API ecoa o token na resposta e no erro: o que é guardado já vai saneado.
        eco = {"sessao": {"estado": "UP"}, "eco": f"Bearer {base.TOKEN}", "chave": base.TOKEN}
        self.atualizar(self.chip_, base.roteador(sessao=eco), origem="manual")
        self.chip_2 = self.chip(2, nome="Chip Erro")
        self.atualizar(self.chip_2, lambda url: base.Resp(401, json={"erro": f"token {base.TOKEN} recusado"}), origem="manual")
        self.chip_.refresh_from_db()

    def confere(self, resposta, onde):
        texto = resposta.content.decode("utf-8")
        for segredo in self.segredos:
            self.assertNotIn(segredo, texto, f"o token vazou em {onde}")
        self.assertNotRegex(texto, r"Bearer\s+(?!\[oculto\]|\[token\])\S{6,}", f"um 'Bearer <token>' vazou em {onde}")

    def test_nenhuma_tela_mostra_o_token(self):
        for nome, args in (("m2m_visao_geral", []), ("m2m_chips", []), ("m2m_chip_detalhe", [self.chip_.pk]),
                           ("m2m_chip_detalhe", [self.chip_2.pk]), ("m2m_chip_editar", [self.chip_.pk]), ("m2m_chip_novo", []),
                           ("m2m_consumo", []), ("m2m_sessoes", []), ("m2m_alertas", []), ("m2m_config", [])):
            with self.subTest(tela=nome):
                self.confere(self.get(nome, *args), nome)

    def test_nenhum_json_mostra_o_token(self):
        consultas = list(M2MConsulta.objects.all())
        self.assertTrue(consultas)
        for c in consultas:
            self.confere(self.client.get(reverse("m2m_consulta_payload", args=[c.pk])), f"payload da consulta {c.pk}")
        corpo = {"mapeamento": {"sessao": {"campo_estado": "eco", "valores_ativa": "x"}}}
        self.confere(self.client.post(reverse("m2m_config_previa"), data=json.dumps(corpo), content_type="application/json"), "prévia")
        self.confere(self.client.get(reverse("m2m_opcoes"), {"campo": "veiculo", "q": "ab"}), "opções")

    def test_as_respostas_das_acoes_nao_mostram_o_token(self):
        eco = {"sessao": {"estado": "UP"}, "eco": base.TOKEN}
        self.api_falsa(base.roteador(sessao=eco))
        M2MChip.objects.filter(pk=self.chip_.pk).update(ultima_tentativa_em=None)
        r = self.client.post(reverse("m2m_chip_atualizar", args=[self.chip_.pk]), data=json.dumps({"contexto": "linha"}), content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.confere(r, "atualizar")
        r = self.client.post(reverse("m2m_config_testar"), data=json.dumps({"chip_id": self.chip_.pk}), content_type="application/json")
        self.confere(r, "testar")
        self.confere(self.client.post(reverse("m2m_config_token"), {"acao": "salvar", "token": "x y"}), "token inválido")

    def test_o_payload_guardado_foi_saneado_na_gravacao(self):
        payload = M2MChip.objects.get(pk=self.chip_.pk).sessao_consulta.payload
        self.assertNotIn(base.TOKEN, json.dumps(payload))
        self.assertIn("[token]", json.dumps(payload))

    def test_payload_antigo_com_o_token_ainda_sai_saneado(self):
        """Defesa em profundidade: mesmo uma linha gravada por engano com o token em claro sai redigida."""
        consulta = M2MChip.objects.get(pk=self.chip_.pk).sessao_consulta
        M2MConsulta.objects.filter(pk=consulta.pk).update(payload={"eco": base.TOKEN, "outro": f"Bearer {_TOKEN_DO_BANCO}"})
        r = self.client.get(reverse("m2m_consulta_payload", args=[consulta.pk]))
        self.confere(r, "payload antigo")
        self.assertIn("[token]", r.content.decode("utf-8"))

    def test_o_contexto_dos_templates_nao_leva_a_credencial(self):
        r = self.get("m2m_config")
        for nivel in r.context:
            for chave in nivel.flatten() if hasattr(nivel, "flatten") else {}:
                self.assertNotEqual(chave, "token")
        cfg = r.context["cfg"]
        self.assertNotIn("token_cifrado", cfg)
        self.assertNotIn("token", cfg)
        self.assertNotIn("cifrado", json.dumps({k: str(v) for k, v in r.context["integracao"].items()}))


# ═════════════════════════════════════════════════════════════
# 10. Leituras novas do serviço
# ═════════════════════════════════════════════════════════════

class LeiturasDasTelasTests(TelasBase):
    def test_volume_para_bytes_e_o_inverso(self):
        self.assertEqual(m2m.volume_para_bytes("5", "GB"), 5_000_000_000)
        self.assertEqual(m2m.volume_para_bytes("1,5", "GB"), 1_500_000_000)
        self.assertEqual(m2m.volume_para_bytes("1.5", "MB"), 1_500_000)
        self.assertEqual(m2m.volume_para_bytes("2", "GB", {"consumo": {"base": 1024}}), 2 * 1024 ** 3)
        self.assertIsNone(m2m.volume_para_bytes("", "GB"))
        self.assertIsNone(m2m.volume_para_bytes("   ", "GB"))
        for texto, unidade in (("abc", "GB"), ("0", "GB"), ("-1", "GB"), ("1", "Gb"), ("1", "KB"), ("1", ""), ("NaN", "GB"), ("1e30", "GB")):
            with self.subTest(texto=texto, unidade=unidade), self.assertRaises(ValidationError):
                m2m.volume_para_bytes(texto, unidade)
        self.assertEqual(m2m.volume_de_bytes(5_000_000_000), ("5", "GB"))
        self.assertEqual(m2m.volume_de_bytes(1_500_000_000), ("1,5", "GB"))
        self.assertEqual(m2m.volume_de_bytes(300_000_000), ("300", "MB"))
        self.assertEqual(m2m.volume_de_bytes(None), ("", "GB"))
        for valor in (1_234_567, 7_000_000_000, 90_000_000_000_000, 123):
            texto, unidade = m2m.volume_de_bytes(valor)
            self.assertAlmostEqual(m2m.volume_para_bytes(texto, unidade), valor, delta=max(1000, valor // 1000))

    def test_mapeamento_configurado_so_conta_com_o_necessario(self):
        self.assertEqual(m2m.mapeamento_configurado({}), {"sessao": False, "consumo": False, "identidade": False})
        self.assertFalse(m2m.mapeamento_configurado({"sessao": {"campo_estado": "x"}})["sessao"])         # sem valores não interpreta nada
        self.assertTrue(m2m.mapeamento_configurado({"sessao": {"campo_estado": "x", "valores_ativa": ["UP"]}})["sessao"])
        self.assertTrue(m2m.mapeamento_configurado({"consumo": {"campo_volume": "x"}})["consumo"])
        self.assertTrue(m2m.mapeamento_configurado({"identidade": {"campo_iccid": "x"}})["identidade"])

    def test_resumo_de_conectividade_coloca_cada_chip_em_um_segmento_so(self):
        c = self.cenario()
        r = m2m.resumo_conectividade()
        self.assertEqual(r["total"], 5)                                    # o desativado fica de fora
        self.assertEqual(sum(s["n"] for s in r["segmentos"]), r["total"])
        n = {s["chave"]: s["n"] for s in r["segmentos"]}
        self.assertEqual(n, {"ativa": 1, "sem_sessao": 1, "nao_interpretado": 1, "falha_consulta": 1, "nao_consultado": 1})
        self.assertEqual([s["chave"] for s in r["segmentos"]], ["ativa", "sem_sessao", "nao_interpretado", "falha_consulta", "nao_consultado"])
        self.assertAlmostEqual(sum(s["pct"] for s in r["segmentos"]), 100, delta=0.1)

    def test_resumo_de_conectividade_sem_chips_nao_divide_por_zero(self):
        r = m2m.resumo_conectividade()
        self.assertEqual(r["total"], 0)
        self.assertTrue(all(s["n"] == 0 and s["pct"] == 0 for s in r["segmentos"]))

    def test_consumo_agregado_so_soma_chips_com_consumo_interpretado(self):
        self.assertEqual(m2m.consumo_agregado()["total_txt"], "—")
        self.assertFalse(m2m.consumo_agregado()["mapeado"])
        self.cenario()
        a = m2m.consumo_agregado()
        self.assertTrue(a["mapeado"])
        self.assertEqual(a["chips_com_consumo"], 4)                         # ativo, sem sessão, não lido e o com falha (todos têm consumo lido)
        self.assertEqual(a["monitorados"], 5)                               # o pendente não tem
        self.assertEqual(a["total_bytes"], 4 * 250_500_000)
        self.assertEqual(a["total_txt"], interp.formatar_bytes(4 * 250_500_000))
        self.assertIsNotNone(a["mais_antigo_em"])
        self.assertLessEqual(a["mais_antigo_em"], a["mais_recente_em"])

    def test_consumo_agregado_nao_soma_chip_desativado(self):
        c = self.cenario()
        m2m.desativar(c["ativo"], self.usuario, "x")
        self.assertEqual(m2m.consumo_agregado()["chips_com_consumo"], 3)

    def test_anotar_chips_calcula_situacao_frescor_e_percentual_sem_consulta_por_linha(self):
        self.mapear()
        chip = self.chip(1, limite_mensal_bytes=1_000_000_000)
        self.atualizar(chip, base.roteador(), origem="manual")
        lista = m2m.anotar_chips(m2m.listar_chips())
        c = lista[0]
        self.assertEqual(c.situacao, "normal")
        self.assertEqual(c.situacao_rotulo, "Normal")
        self.assertEqual(c.consumo_txt, "250,5 MB")
        self.assertEqual(c.pct_limite, 25)
        self.assertFalse(c.frescor_sessao["desatualizado"])
        self.assertEqual(c.estado_bruto, "UP")
        with CaptureQueriesContext(connection) as q:
            m2m.anotar_chips(m2m.listar_chips())
        self.assertLessEqual(len(q), 4)

    def test_pct_do_limite_so_existe_com_limite_cadastrado(self):
        self.mapear()
        chip = self.chip(1)
        self.atualizar(chip, base.roteador(), origem="manual")
        self.assertIsNone(m2m.anotar_chips(m2m.listar_chips())[0].pct_limite)
        self.assertIsNone(m2m.resumo_do_chip(M2MChip.objects.get(pk=chip.pk))["consumo"]["pct_limite"])

    def test_chip_desatualizado_e_marcado_na_lista_e_no_frescor(self):
        self.mapear()
        chip = self.chip(1)
        self.atualizar(chip, base.roteador(), origem="manual")
        antiga = timezone.now() - timedelta(hours=30)
        M2MChip.objects.filter(pk=chip.pk).update(ultima_valida_em=antiga)
        c = m2m.anotar_chips(m2m.listar_chips({"situacao": "desatualizado"}))
        self.assertEqual([x.pk for x in c], [chip.pk])
        self.assertEqual(c[0].situacao, "desatualizado")
        html = self.html(self.get("m2m_chips"))
        self.assertIn('data-estado="desatualizado"', html)

    def test_resumo_do_chip_traz_o_ultimo_dado_valido_mesmo_depois_de_falha(self):
        self.mapear()
        chip = self.chip(1)
        self.atualizar(chip, base.roteador(), origem="manual")
        self.falhar(chip, 2)
        r = m2m.resumo_do_chip(M2MChip.objects.select_related("sessao_consulta", "consumo_consulta").get(pk=chip.pk))
        self.assertEqual(r["situacao"], "erro_consulta")
        self.assertEqual(r["sessao"]["estado"], "ativa")
        self.assertEqual(r["sessao"]["estado_bruto"], "UP")
        self.assertEqual(r["sessao"]["campos"], [{"caminho": "sessao.ip", "valor": "10.0.0.5"}])
        self.assertEqual(r["consumo"]["volume_txt"], "250,5 MB")
        self.assertEqual(r["consumo"]["valor_original"], "250.5")
        self.assertEqual(r["ultima_falha"]["rotulo"], "API indisponível")

    def test_resumo_do_chip_sem_mapeamento_explica_por_que_nao_interpretou(self):
        chip = self.chip(1)
        self.atualizar(chip, base.roteador(), origem="manual")
        r = m2m.resumo_do_chip(M2MChip.objects.select_related("sessao_consulta", "consumo_consulta").get(pk=chip.pk))
        self.assertEqual(r["sessao"]["estado"], "nao_interpretado")
        self.assertIn("Sem mapeamento", r["sessao"]["motivo"])
        self.assertFalse(r["sessao"]["mapeada"])
        self.assertIsNone(r["consumo"]["volume_bytes"])
        self.assertIn("Sem mapeamento", r["consumo"]["motivo"])

    def test_comparar_periodos_usa_o_ultimo_valor_valido_de_cada_periodo(self):
        self.mapear()
        chip = self.chip(1)
        hoje = timezone.now()

        def consulta(dias_atras, mb):
            c = M2MConsulta.objects.create(
                chip=chip, tipo="consumo", origem="agendada", iniciada_em=hoje - timedelta(days=dias_atras),
                identificador_tipo="iccid", resultado="sucesso", payload={}, volume_bytes=mb * 1_000_000,
            )
            return c

        consulta(40, 100)
        consulta(35, 150)             # último do período A
        consulta(5, 300)
        consulta(2, 420)              # último do período B
        M2MConsulta.objects.create(chip=chip, tipo="consumo", origem="agendada", iniciada_em=hoje - timedelta(days=1),
                                   identificador_tipo="iccid", resultado="timeout")          # falha não conta
        d = lambda n: (hoje - timedelta(days=n)).date()                                         # noqa: E731
        r = m2m.comparar_periodos(d(45), d(30), d(10), d(0))
        self.assertEqual(len(r["linhas"]), 1)
        linha = r["linhas"][0]
        self.assertEqual((linha["a_bytes"], linha["b_bytes"]), (150_000_000, 420_000_000))
        self.assertEqual(linha["diferenca"], 270_000_000)
        self.assertEqual(linha["diferenca_txt"], "+270 MB")
        self.assertEqual(r["total_a_txt"], "150 MB")
        # período sem nenhuma consulta: só o lado que existe, sem inventar diferença
        r = m2m.comparar_periodos(d(200), d(190), d(10), d(0))
        self.assertIsNone(r["linhas"][0]["a_bytes"])
        self.assertIsNone(r["linhas"][0]["diferenca"])
        self.assertEqual(r["linhas"][0]["diferenca_txt"], "—")
        # recorte por chip
        self.assertEqual(m2m.comparar_periodos(d(45), d(30), d(10), d(0), chip=self.chip(2))["linhas"], [])

    def test_formatar_diferenca_tem_sinal(self):
        self.assertEqual(m2m.formatar_diferenca(1_500_000_000), "+1,5 GB")
        self.assertEqual(m2m.formatar_diferenca(-300_000_000), "-300 MB")
        self.assertEqual(m2m.formatar_diferenca(0), "0 B")
        self.assertEqual(m2m.formatar_diferenca(None), "—")

    def test_historico_de_consumo_so_traz_consultas_validas_e_conta_as_falhas_a_parte(self):
        self.mapear()
        chip = self.chip(1)
        self.atualizar(chip, base.roteador(), origem="manual")
        self.atualizar(chip, base.roteador(status=500), tipos=("consumo",), origem="agendada")
        self.assertEqual(m2m.historico_consumo(chip).count(), 1)
        self.assertEqual(m2m.falhas_de_consumo(chip), 1)
        amanha = timezone.localdate() + timedelta(days=1)
        self.assertEqual(m2m.historico_consumo(chip, de=amanha).count(), 0)
        self.assertEqual(m2m.historico_consumo(chip, ate=timezone.localdate()).count(), 1)     # ate é inclusivo

    def test_mudancas_de_estado_trazem_os_rotulos(self):
        self.mapear()
        chip = self.chip(1)
        self.atualizar(chip, base.roteador(), origem="manual")
        M2MChip.objects.filter(pk=chip.pk).update(ultima_tentativa_em=None)
        self.atualizar(chip, base.roteador(sessao=base.SESSAO_DOWN), origem="manual")
        m = m2m.mudancas_de_estado()
        self.assertEqual(len(m), 2)
        self.assertEqual((m[0]["de"], m[0]["para"]), ("Sessão ativa", "Sem sessão"))
        self.assertEqual((m[1]["de"], m[1]["para"]), ("Não consultado", "Sessão ativa"))
        self.assertEqual(m[0]["estado_bruto"], "DOWN")

    def test_eventos_recentes_nunca_citam_o_token(self):
        tim.salvar_token(_TOKEN_DO_BANCO, self.admin)
        m2m.salvar_configuracao({"falhas_para_alerta": 4}, self.admin)
        self.mapear()
        eventos = m2m.eventos_recentes(20, so_da_integracao=True)
        self.assertTrue(eventos)
        tudo = json.dumps([{**e, "em": str(e["em"]), "chip": None} for e in eventos], ensure_ascii=False)
        self.assertNotIn(_TOKEN_DO_BANCO, tudo)
        self.assertNotIn(base.TOKEN, tudo)
        self.assertIn("Token novo gravado (cifrado).", tudo)
        self.assertIn("Campos alterados: falhas_para_alerta.", tudo)
        self.assertTrue(all(e["chip"] is None for e in m2m.eventos_recentes(20, so_da_integracao=True)))

    def test_chips_com_falha_traz_os_mais_antigos_na_falha_primeiro(self):
        a, b = self.chip(1, nome="A"), self.chip(2, nome="B")
        self.falhar(a, 2)
        self.falhar(b, 4)
        self.assertEqual([c.nome for c in m2m.chips_com_falha()], ["B", "A"])
        self.assertEqual(m2m.chips_com_falha()[0].ultima_falha_rotulo, "API indisponível")

    def test_contar_ocorrencias_por_estado_inclui_zeros(self):
        self.assertEqual(m2m.contar_ocorrencias_por_estado(), {"aberta": 0, "em_tratamento": 0, "resolvida": 0, "descartada": 0})

    def test_amostra_de_resposta_so_existe_com_corpo_json(self):
        chip = self.chip(1)
        self.assertIsNone(m2m.amostra_de_resposta("sessao"))
        self.atualizar(chip, base.roteador(), origem="manual")
        a = m2m.amostra_de_resposta("sessao")
        self.assertEqual(a["chip"].pk, chip.pk)
        self.assertIn("sessao.estado", [c["caminho"] for c in a["caminhos"]])
        self.assertIsNotNone(m2m.amostra_de_resposta("consumo"))

    def test_listar_chips_nao_carrega_o_payload(self):
        self.mapear()
        chip = self.chip(1)
        self.atualizar(chip, base.roteador(), origem="manual")
        c = m2m.listar_chips().get(pk=chip.pk)
        self.assertIn("payload", c.sessao_consulta.get_deferred_fields())


class TagsTests(TelasBase):
    def render(self, template, **contexto):
        return Template("{% load m2m_tags %}" + template).render(Context(contexto))

    def test_blocos_de_4(self):
        self.assertEqual(self.render("{{ v|m2m_blocos }}", v="8955100000000000019"), "8955 1000 0000 0000 019")
        self.assertEqual(self.render("{{ v|m2m_blocos }}", v="895510000000000001 99"), "8955 1000 0000 0000 0199")
        self.assertEqual(self.render("{{ v|m2m_blocos }}", v=""), "")
        self.assertEqual(self.render("{{ v|m2m_blocos }}", v=None), "")

    def test_indicador_tem_forma_texto_e_escapa(self):
        html = self.render('{% m2m_ind "erro_consulta" %}')
        self.assertIn('data-estado="erro_consulta"', html)
        self.assertIn("Falha de consulta", html)
        self.assertIn("m2m-ind-forma", html)
        html = self.render("{% m2m_ind chave rotulo %}", chave="ativa", rotulo="<script>alert(1)</script>")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn('data-estado="nao_consultado"', self.render("{% m2m_ind vazio %}", vazio=None))

    def test_todo_estado_com_rotulo_tem_regra_de_forma_no_css(self):
        from ProjetoEstoque.templatetags.m2m_tags import ROTULOS_DE_ESTADO

        css = (_RAIZ / "static" / "css" / "ds-m2m.css").read_text(encoding="utf-8")
        for estado in ROTULOS_DE_ESTADO:
            if estado in ("normal", "info", "sucesso"):
                continue
            self.assertIn(f'.m2m-ind[data-estado="{estado}"]', css, f"o estado '{estado}' não tem forma própria no CSS")
        for estado in ("normal", "info", "sucesso"):
            self.assertIn(f'.m2m-ind[data-estado="{estado}"]', css)

    def test_frescor_marca_o_desatualizado_e_diz_nunca_consultado(self):
        agora = timezone.now()
        velho = m2m.frescor(agora - timedelta(hours=30), 24, agora)
        self.assertIn("desatualizado", self.render("{% m2m_frescor f %}", f=velho))
        self.assertIn('data-velho="1"', self.render("{% m2m_frescor f %}", f=velho))
        novo = m2m.frescor(agora - timedelta(minutes=5), 24, agora)
        html = self.render("{% m2m_frescor f %}", f=novo)
        self.assertIn("há 5 min", html)
        self.assertNotIn("desatualizado", html)
        self.assertIn("nunca consultado", self.render("{% m2m_frescor f %}", f=m2m.frescor(None)))
        self.assertEqual(self.render("{% m2m_frescor f %}", f=None), "")

    def test_bytes_formata_em_pt_br(self):
        self.assertEqual(self.render("{{ v|m2m_bytes:1000 }}", v=1_500_000_000), "1,5 GB")
        self.assertEqual(self.render("{{ v|m2m_bytes:1024 }}", v=1024 ** 2), "1 MB")
        self.assertEqual(self.render("{{ v|m2m_bytes }}", v=None), "—")

    def test_faltam_completa_a_fita(self):
        self.assertEqual(len(self.render("{% for _ in l|m2m_faltam %}x{% endfor %}", l=[1] * 10)), 14)
        self.assertEqual(self.render("{% for _ in l|m2m_faltam %}x{% endfor %}", l=[1] * 30), "")


# ═════════════════════════════════════════════════════════════
# 11. Estáticos: as regras do design system valem para a folha e o script do módulo
# ═════════════════════════════════════════════════════════════

class EstaticosTests(TelasBase):
    def setUp(self):
        super().setUp()
        self.css = (_RAIZ / "static" / "css" / "ds-m2m.css").read_text(encoding="utf-8")
        self.js = (_RAIZ / "static" / "js" / "m2m.js").read_text(encoding="utf-8")

    def sem_comentarios(self, css):
        return re.sub(r"/\*.*?\*/", "", css, flags=re.S)

    def test_nenhuma_cor_escrita_a_mao_na_folha_nem_no_script(self):
        css = self.sem_comentarios(self.css)
        self.assertNotRegex(css, r"#[0-9a-fA-F]{3,8}\b", "cor HEX literal em ds-m2m.css (regra 33)")
        self.assertNotRegex(css, r"\b(?:rgb|rgba|hsl|hsla|oklch|lab)\(", "cor funcional literal em ds-m2m.css")
        js = re.sub(r"/\*.*?\*/", "", self.js, flags=re.S)
        js = re.sub(r"//[^\n]*", "", js)
        self.assertNotRegex(js, r"#[0-9a-fA-F]{6}\b", "cor HEX literal em m2m.js")
        self.assertNotRegex(js, r"\brgba?\(", "cor funcional literal em m2m.js")

    def test_sem_gradiente_sombra_colorida_nem_pilula(self):
        css = self.sem_comentarios(self.css)
        # Direção visual de 12/11/2026 (gestão de ativos): sem degradê, brilho, sombra colorida nem
        # pílula. O ponto do indicador de estado é redondo (`border-radius: 50%`), nada mais.
        for proibido in ("gradient(", "blur(", "backdrop-filter", "drop-shadow", "999px", "radius-pill"):
            self.assertNotIn(proibido, css, f"'{proibido}' contraria a direção visual do módulo")
        # o raio do controle é de 4 a 8px (cartão e tabela usam `--m2m-raio-lg`, de até 10px)
        raio = re.search(r"--m2m-raio:\s*(\d+)px", css)
        self.assertTrue(raio and 4 <= int(raio.group(1)) <= 8)
        raio_lg = re.search(r"--m2m-raio-lg:\s*(\d+)px", css)
        self.assertTrue(raio_lg and 4 <= int(raio_lg.group(1)) <= 10)
        for m in re.finditer(r"box-shadow:\s*([^;]+);", css):
            self.assertNotRegex(m.group(1), r"var\(--(?:m2m-(?:ok|warn|err|info|a)|brand|success|danger|warning)", "sombra colorida")

    def test_so_apelidos_de_tokens_do_sistema(self):
        """Todo `--m2m-*` consumido é definido aqui; todo `--m2m-*` de cor aponta para um token do tokens.css."""
        css = self.sem_comentarios(self.css)
        definidos = set(re.findall(r"(?:^|[{;\s])(--m2m-[a-z0-9-]+)\s*:", css))
        usados = set(re.findall(r"var\((--m2m-[a-z0-9-]+)", css))
        self.assertFalse(usados - definidos, f"token --m2m-* usado e não definido: {sorted(usados - definidos)}")
        bloco = self.css[self.css.index(":where(.m2m-page"):self.css.index("/* O tema escuro")]
        for nome, valor in re.findall(r"(--m2m-(?:bg|surface|sunken|hover|ln(?:-\d)?|tx(?:-\d)?|a(?:-soft|-ring)?|ok|warn|err|info))\s*:\s*([^;]+);", bloco):
            self.assertTrue(valor.strip().startswith("var(--"), f"{nome} deveria ser apelido de token do sistema, não '{valor.strip()}'")

    def test_escopo_dos_tokens_cobre_a_raiz_o_aviso_e_o_select2(self):
        escopo = re.search(r":where\((.*?)\)\s*\{", self.css, re.S).group(1)
        for raiz in (".m2m-page", ".m2m-modal", ".m2m-veu", ".m2m-toast", ".m2m-toast-area", ".select2-container.m2m-s2", ".select2-dropdown.m2m-s2"):
            self.assertIn(raiz, escopo)
        self.assertNotIn(":root", self.sem_comentarios(self.css))
        self.assertNotIn("body.theme-dark", self.sem_comentarios(self.css))       # o escuro sai dos apelidos

    def test_tipografia_so_pela_escala_do_sistema(self):
        css = self.sem_comentarios(self.css)
        self.assertNotRegex(css, r"font-size:\s*[\d.]+(?:px|rem)\b")
        self.assertNotRegex(css, r"font-family:\s*[\"']?Segoe")
        self.assertIn("var(--font-mono)", css)
        self.assertIn("tabular-nums", css)

    def test_estaticos_nao_tem_tag_de_template(self):
        for texto in (self.css, self.js):
            self.assertNotRegex(texto, r"\{%|\{\{")

    def test_o_script_le_a_config_por_json_script_e_nao_interpola_nada(self):
        self.assertIn("getElementById('m2m-config')", self.js)
        html = self.html(self.get("m2m_chips"))
        self.assertIn('id="m2m-config"', html)
        cfg = json.loads(re.search(r'<script id="m2m-config" type="application/json">(.*?)</script>', html, re.S).group(1))
        self.assertEqual(set(cfg), {"csrf_token", "pode_operar", "pode_configurar", "urls"})
        self.assertTrue(cfg["csrf_token"])
        for chave in cfg["urls"].values():
            self.assertTrue(chave.startswith("/agro/"))
        self.assertNotIn("token", {k for k in cfg if k != "csrf_token"})

    def test_o_script_nao_tem_regra_de_negocio_de_formatacao(self):
        for proibido in ("formatar_bytes", "toLocaleString('pt-BR', { style", "innerHTML = d.", "eval(", "document.write"):
            self.assertNotIn(proibido, self.js)

    def test_toda_tela_carrega_a_folha_e_o_script_versionados_e_nenhuma_usa_safe(self):
        pk = self.chip(1).pk
        for nome, args in (("m2m_visao_geral", []), ("m2m_chips", []), ("m2m_chip_detalhe", [pk]), ("m2m_consumo", []),
                           ("m2m_sessoes", []), ("m2m_alertas", []), ("m2m_config", []), ("m2m_chip_novo", [])):
            html = self.html(self.get(nome, *args))
            self.assertRegex(html, r'href="/static/css/ds-m2m\.css\?v=[^"]+"')
            self.assertRegex(html, r'src="/static/js/m2m\.js\?v=[^"]+"')
            self.assertLess(html.index("css/tokens.css"), html.index("css/ds-m2m.css"))     # tokens antes da folha do módulo
            self.assertLess(html.index("css/ui-kit.css"), html.index("css/ds-m2m.css"))
        for arquivo in (_RAIZ / "templates" / "front" / "m2m").glob("*.html"):
            texto = arquivo.read_text(encoding="utf-8")
            self.assertNotIn("|safe", texto, f"{arquivo.name} usa |safe")
            self.assertNotIn("mark_safe", texto)
            self.assertNotRegex(texto, r'\|default:"\[\]"', f"{arquivo.name}: `|default:\"[]\"` antes de json_script (regra 18)")
            self.assertNotRegex(texto, r"<style", f"{arquivo.name} tem CSS inline")
            for m in re.finditer(r'pluralize:"[^",]*"', texto):
                self.fail(f"{arquivo.name}: pluralize de um argumento ({m.group(0)}) — conferir a regra 19")

    def test_comentarios_de_template_do_modulo_sao_de_uma_linha(self):
        for arquivo in (_RAIZ / "templates" / "front" / "m2m").glob("*.html"):
            for numero, linha in enumerate(arquivo.read_text(encoding="utf-8").splitlines(), 1):
                if "{#" in linha:
                    self.assertIn("#}", linha, f"{arquivo.name}:{numero} comentário {{# #}} quebrado em várias linhas (regra 17)")

    def test_todo_include_do_modulo_aponta_para_um_arquivo_que_existe(self):
        raiz = _RAIZ / "templates"
        for arquivo in (raiz / "front" / "m2m").glob("*.html"):
            for caminho in re.findall(r"\{%\s*include\s+'([^']+)'", arquivo.read_text(encoding="utf-8")):
                self.assertTrue((raiz / caminho).is_file(), f"{arquivo.name} inclui '{caminho}', que não existe")

    def test_chartjs_so_e_carregado_onde_ha_grafico(self):
        chip = self.chip(1)
        self.assertNotIn("chart.js", self.html(self.get("m2m_chip_detalhe", chip.pk)))
        for nome in ("m2m_visao_geral", "m2m_chips", "m2m_consumo"):
            self.assertNotIn("chart.js", self.html(self.get(nome)))

    def test_o_tema_escuro_funciona_pelos_apelidos(self):
        """Sem navegador: confere que nenhum apelido do módulo depende de `:root` (que o escuro não troca)
        e que os tokens de estado que a folha consome são redefinidos no bloco escuro do tokens.css."""
        tokens = (_RAIZ / "static" / "css" / "tokens.css").read_text(encoding="utf-8")
        escuro = tokens[tokens.index("body.theme-dark"):]
        for token in ("--success", "--warning", "--danger", "--info", "--text-primary", "--text-tertiary",
                      "--bg-elevated", "--bg-page-2", "--separator-non-opaque", "--chart-1", "--brand"):
            self.assertRegex(escuro, rf"(?m)^\s*{re.escape(token)}\s*:", f"{token} não é redefinido no escuro")
        css = self.sem_comentarios(self.css)
        for token in set(re.findall(r"var\((--[a-z0-9-]+)\)", css)):
            if token.startswith("--m2m-"):
                continue
            self.assertIn(token + ":", tokens.replace(" :", ":"), f"{token} usado sem reserva e não definido em tokens.css")


# ═════════════════════════════════════════════════════════════
# 10. Chip de operadora sem integração (Fase D)
# ═════════════════════════════════════════════════════════════

class ChipDeOutraOperadoraTests(TelasBase):
    def test_lista_e_detalhe_abrem_e_dizem_que_nao_ha_consulta_pela_api(self):
        from ProjetoEstoque.models import ConectividadeOperadora

        vivo = ConectividadeOperadora.objects.create(nome="Vivo", integracao="", ativo=True)
        chip = self.chip(1, nome="Chip da Vivo", operadora=vivo.pk)
        v = self.veiculo(1, codigo="TR-05", nome="Trator Azul")
        self.vincular(chip, v)
        lista = self.html(self.get("m2m_chips"))
        self.assertIn("Chip da Vivo", lista)
        self.assertIn("TR-05 · Trator Azul", lista)                                 # a coluna Veículo
        self.assertIn("Sem integração (não consultado pela API)", lista)
        detalhe = self.html(self.get("m2m_chip_detalhe", chip.pk))
        self.assertIn("Vivo", detalhe)
        self.assertIn("sem integração — não consultado pela API", detalhe)
        self.assertIn("TR-05 · Trator Azul", detalhe)
        self.sem_lixo_de_template(lista, "lista")
        self.sem_lixo_de_template(detalhe, "detalhe")
        r = self.client.post(reverse("m2m_chip_atualizar", args=[chip.pk]), data="{}", content_type="application/json")
        self.assertEqual((r.status_code, r.json()["codigo"]), (409, "sem_integracao"))
