"""
Mapa de Rede — topologia, portas de switch e o novo design system do módulo.

O que estes testes protegem, em ordem de risco:

  1. **O enlace tem DUAS pernas.** `rede_topologia_service.conectar` grava o
     par nos dois sentidos. Gravar `destino_porta` à mão deixaria o mapa
     mostrando o cabo saindo do switch e o relatório de portas do outro
     equipamento dizendo que ele está livre — divergência que não aparece em
     tela nenhuma até alguém ir conferir no rack.

  2. **Gerar portas não apaga trabalho de campo.** Regerar o painel de um
     switch de 48 portas tem de preservar o que já tem cabo. A primeira
     versão deste service apagava tudo e recriava.

  3. **O cabo aparece UMA vez no grafo.** As duas portas apontam uma para a
     outra; sem a de-duplicação o mapa desenharia dois fios sobrepostos e o
     KPI de enlaces contaria o dobro.

  4. **A ponte servidor → `rede-mapa.js`.** Um `.js` em `static/` não passa
     pelo motor de template: chave esquecida em `_config_mapa` é `undefined`
     no navegador e a bancada abre inerte, sem erro no servidor.

  5. **O design system do módulo não copia o do sistema.** `ds-rede.css` é
     linguagem própria (aresta reta, densidade de prancha), mas toda cor e
     todo tamanho saem de `tokens.css` — senão ele envelhece sozinho, que é
     a falha das regras 26, 31 e 33.
"""
import json
import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from ProjetoEstoque.models import (
    Categoria,
    Item,
    Localidade,
    RedeDispositivo,
    RedeMeioChoices,
    RedePapelChoices,
    RedePorta,
    RedePortaStatusChoices,
    RedePortaTipoChoices,
    RedeRack,
    RedeRota,
    RedeRotaInstalacaoChoices,
    RedeRotaTipoChoices,
    RedeTopologia,
    Subtipo,
)
from services import rede_topologia_service as rede


def _virgulas_de_topo(seletor: str) -> list[str]:
    """
    Separa um seletor nas vírgulas de PRIMEIRO nível.

    `str.split(",")` parte também dentro de `:is(.a, .b)` e transforma um
    seletor legítimo em pedaços sem âncora — foi o que deu falso positivo na
    primeira versão do teste abaixo.
    """
    partes, atual, nivel = [], [], 0
    for ch in seletor:
        if ch == "(":
            nivel += 1
        elif ch == ")":
            nivel -= 1
        if ch == "," and nivel == 0:
            partes.append("".join(atual).strip())
            atual = []
            continue
        atual.append(ch)
    partes.append("".join(atual).strip())
    return [p for p in partes if p]


_TEMPLATES = Path(settings.BASE_DIR) / "ProjetoEstoque" / "templates" / "front"
_JS = Path(settings.BASE_DIR) / "ProjetoEstoque" / "static" / "js"
_CSS = Path(settings.BASE_DIR) / "ProjetoEstoque" / "static" / "css"


class BaseRede(TestCase):
    """Um parque mínimo de verdade: borda, switch de 24 portas e dois APs."""

    def setUp(self):
        self.user = User.objects.create_superuser("rede_teste", "r@t.com", "x")
        self.client = Client()
        self.client.force_login(self.user)

        self.local = Localidade.objects.create(local="ESCRITÓRIO TI — TESTE")
        cat = Categoria.objects.create(nome="Rede")
        self.sub_switch = Subtipo.objects.create(nome="Switches", alocado="nao", categoria=cat)
        self.sub_ap = Subtipo.objects.create(nome="Access-Point", alocado="nao", categoria=cat)
        self.sub_cam = Subtipo.objects.create(nome="Câmera", alocado="nao", categoria=cat)

        def ativo(nome, sub, marca="Cisco", modelo="Catalyst 1000 24p"):
            return Item.objects.create(
                nome=nome, subtipo=sub, localidade=self.local, status="ativo",
                marca=marca, modelo=modelo, criado_por=self.user, atualizado_por=self.user,
            )

        self.switch = ativo("SW-A-SC-KTL-TI", self.sub_switch)
        self.switch2 = ativo("SW-B-SC-KTL-TI", self.sub_switch, modelo="Catalyst 1000 8p")
        self.ap1 = ativo("AP-01-SC-KTL-TI", self.sub_ap, modelo="Catalyst 9115")
        self.ap2 = ativo("AP-02-SC-KTL-TI", self.sub_ap, modelo="Catalyst 9115")
        self.camera = ativo("CAM-PATIO-TI", self.sub_cam, marca="Intelbras", modelo="VIP")

        self.disp = rede.dispositivo_de(self.switch, self.user)
        self.disp2 = rede.dispositivo_de(self.switch2, self.user)
        self.disp_ap = rede.dispositivo_de(self.ap1, self.user)


# ──────────────────────────────────────────────────────────────────────────────
# Catálogo e geração de portas
# ──────────────────────────────────────────────────────────────────────────────

class CatalogoDeModelosTests(TestCase):
    def test_todo_modelo_tem_as_chaves_que_gerar_portas_le(self):
        """
        Mesma lição da regra 45: um modelo acrescentado sem uma das chaves
        não falha no cadastro — falha na hora de gerar as portas, com o
        técnico na frente do rack.
        """
        obrigatorias = {"nome", "acessos", "uplinks", "poe", "velocidade",
                        "rotulo", "rotulo_uplink", "poe_watts", "fabricante"}
        for chave, modelo in rede.MODELOS.items():
            faltando = obrigatorias - set(modelo)
            self.assertFalse(faltando, f"{chave} sem as chaves {faltando}")
            self.assertGreater(modelo["acessos"], 0, f"{chave} sem portas de acesso")
            self.assertIn("{n}", modelo["rotulo"], f"{chave}: rótulo sem o lugar do número")

    def test_catalogo_para_a_tela_soma_acessos_e_uplinks(self):
        for linha in rede.catalogo_modelos():
            modelo = rede.MODELOS[linha["chave"]]
            self.assertEqual(linha["portas"], modelo["acessos"] + modelo["uplinks"])

    def test_palpite_de_modelo_reconhece_o_parque_da_empresa(self):
        casos = [
            ("Cisco", "Catalyst C1000-24P-4G-L", "cisco_c1000_24p"),
            ("Cisco", "Catalyst C9200L-48P-4G", "cisco_c9200l_48p"),
            ("Cisco", "CBS350-24P-4G", "cisco_cbs350_24p"),
            ("Cisco", "Meraki MS120-48LP", "meraki_ms120_48"),
            ("Cisco", "IE-3300-8P2S", "cisco_ie3300_8p"),
        ]
        for marca, modelo, esperado in casos:
            item = Item(nome="SW", marca=marca, modelo=modelo)
            self.assertEqual(rede.modelo_sugerido(item), esperado, f"{marca} {modelo}")

    def test_palpite_vazio_quando_nao_ha_pista(self):
        self.assertEqual(rede.modelo_sugerido(Item(nome="", marca="", modelo="")), "")


class GerarPortasTests(BaseRede):
    def test_gera_acessos_e_uplinks_com_o_rotulo_do_fabricante(self):
        criadas = rede.gerar_portas(self.disp, "cisco_c1000_24p", usuario=self.user)
        self.assertEqual(criadas, 28)                     # 24 acessos + 4 SFP
        portas = list(self.disp.portas.order_by("numero"))
        self.assertEqual(portas[0].rotulo, "Gi1/0/1")
        self.assertEqual(portas[11].rotulo, "Gi1/0/12")
        self.assertEqual(portas[23].rotulo, "Gi1/0/24")
        self.assertEqual(portas[24].tipo, RedePortaTipoChoices.SFP)
        self.assertEqual(portas[24].rotulo, "Gi1/1/1")
        self.assertTrue(portas[0].poe)
        self.assertFalse(portas[24].poe, "uplink não entrega PoE")
        self.assertEqual(portas[24].meio, RedeMeioChoices.FIBRA)

    def test_gerar_duas_vezes_nao_duplica(self):
        rede.gerar_portas(self.disp, "cisco_c1000_8p", usuario=self.user)
        novamente = rede.gerar_portas(self.disp, "cisco_c1000_8p", usuario=self.user)
        self.assertEqual(novamente, 0)
        self.assertEqual(self.disp.portas.count(), 10)

    def test_regerar_preserva_porta_com_cabo(self):
        """
        O cabeamento de um rack é trabalho de campo de alguém. Regerar por
        cima apagaria o mapeamento inteiro sem aviso — e foi o que a primeira
        versão fazia.
        """
        rede.gerar_portas(self.disp, "cisco_c1000_8p", usuario=self.user)
        rede.gerar_portas(self.disp_ap, "terminal_1", usuario=self.user)
        porta = self.disp.portas.get(numero=3)
        destino = self.disp_ap.portas.get(numero=1)
        rede.conectar(porta, destino, usuario=self.user)
        porta.descricao = "AP do alojamento 4"
        porta.save()

        rede.gerar_portas(self.disp, "cisco_c1000_8p", usuario=self.user, substituir=True)

        porta.refresh_from_db()
        self.assertEqual(porta.descricao, "AP do alojamento 4")
        self.assertEqual(porta.destino_porta_id, destino.pk)
        self.assertEqual(porta.status, RedePortaStatusChoices.ATIVA)

    def test_modelo_desconhecido_recusa(self):
        with self.assertRaises(ValueError):
            rede.gerar_portas(self.disp, "switch_imaginario", usuario=self.user)

    def test_gerar_grava_o_modelo_no_dispositivo(self):
        rede.gerar_portas(self.disp, "cisco_c1000_48p", usuario=self.user)
        self.disp.refresh_from_db()
        self.assertEqual(self.disp.modelo_portas, "cisco_c1000_48p")

    def test_contas_do_dispositivo(self):
        rede.gerar_portas(self.disp, "generico_8", usuario=self.user)
        self.assertEqual(self.disp.total_portas, 8)
        self.assertEqual(self.disp.portas_ativas, 0)
        self.assertEqual(self.disp.ocupacao_pct, 0)
        p = self.disp.portas.first()
        p.status = RedePortaStatusChoices.ATIVA
        p.save()
        # 1 de 8 = 12,5%; `round` do Python arredonda para o PAR, então 12.
        self.assertEqual(self.disp.ocupacao_pct, 12)


# ──────────────────────────────────────────────────────────────────────────────
# Enlace
# ──────────────────────────────────────────────────────────────────────────────

class EnlaceTests(BaseRede):
    def setUp(self):
        super().setUp()
        rede.gerar_portas(self.disp, "cisco_c1000_8p", usuario=self.user)
        rede.gerar_portas(self.disp2, "cisco_c1000_8p", usuario=self.user)
        rede.gerar_portas(self.disp_ap, "terminal_1", usuario=self.user)

    def test_conectar_grava_as_duas_pernas(self):
        a = self.disp.portas.get(numero=1)
        b = self.disp2.portas.get(numero=9)       # uplink SFP
        rede.conectar(a, b, meio=RedeMeioChoices.FIBRA, usuario=self.user)

        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.destino_porta_id, b.pk)
        self.assertEqual(b.destino_porta_id, a.pk)
        self.assertEqual(a.destino_item_id, self.switch2.pk)
        self.assertEqual(b.destino_item_id, self.switch.pk)
        self.assertEqual(a.status, RedePortaStatusChoices.ATIVA)
        self.assertEqual(b.status, RedePortaStatusChoices.ATIVA)
        self.assertEqual(a.meio, RedeMeioChoices.FIBRA)
        self.assertEqual(b.meio, RedeMeioChoices.FIBRA)

    def test_desconectar_solta_as_duas_pernas(self):
        a = self.disp.portas.get(numero=1)
        b = self.disp2.portas.get(numero=1)
        rede.conectar(a, b, usuario=self.user)
        rede.desconectar(a, usuario=self.user)

        a.refresh_from_db()
        b.refresh_from_db()
        for p in (a, b):
            self.assertIsNone(p.destino_porta_id)
            self.assertIsNone(p.destino_item_id)
            self.assertEqual(p.status, RedePortaStatusChoices.LIVRE)

    def test_conectar_em_porta_ja_ocupada_libera_o_cabo_anterior(self):
        """
        Sem soltar antes, a restrição 1-1 do banco recusaria a gravação e o
        usuário levaria um erro que não diz nada sobre o cabo que já estava
        ali. O contrato é: a última ligação vale, e a anterior fica solta.
        """
        a = self.disp.portas.get(numero=1)
        b = self.disp2.portas.get(numero=1)
        c = self.disp2.portas.get(numero=2)
        rede.conectar(a, b, usuario=self.user)
        rede.conectar(a, c, usuario=self.user)

        a.refresh_from_db(); b.refresh_from_db(); c.refresh_from_db()
        self.assertEqual(a.destino_porta_id, c.pk)
        self.assertEqual(c.destino_porta_id, a.pk)
        self.assertIsNone(b.destino_porta_id)
        self.assertEqual(b.status, RedePortaStatusChoices.LIVRE)

    def test_destino_sem_porta_cadastrada(self):
        """Câmera e desktop não precisam ter portas modeladas."""
        a = self.disp.portas.get(numero=4)
        rede.conectar(a, destino_item=self.camera, usuario=self.user)
        a.refresh_from_db()
        self.assertEqual(a.destino_item_id, self.camera.pk)
        self.assertIsNone(a.destino_porta_id)
        self.assertEqual(a.status, RedePortaStatusChoices.ATIVA)

    def test_recusa_laco_e_mesmo_equipamento(self):
        a = self.disp.portas.get(numero=1)
        b = self.disp.portas.get(numero=2)
        with self.assertRaises(rede.EnlaceInvalido):
            rede.conectar(a, a, usuario=self.user)
        with self.assertRaises(rede.EnlaceInvalido):
            rede.conectar(a, b, usuario=self.user)

    def test_recusa_enlace_sem_destino(self):
        with self.assertRaises(rede.EnlaceInvalido):
            rede.conectar(self.disp.portas.get(numero=1), usuario=self.user)


# ──────────────────────────────────────────────────────────────────────────────
# Grafo, arranjo, conferência e impacto
# ──────────────────────────────────────────────────────────────────────────────

class GrafoTests(BaseRede):
    def setUp(self):
        super().setUp()
        rede.gerar_portas(self.disp, "cisco_c1000_8p", usuario=self.user)
        rede.gerar_portas(self.disp2, "cisco_c1000_8p", usuario=self.user)
        rede.gerar_portas(self.disp_ap, "terminal_1", usuario=self.user)
        rede.conectar(
            self.disp.portas.get(numero=9), self.disp2.portas.get(numero=9),
            meio=RedeMeioChoices.FIBRA, usuario=self.user,
        )
        rede.conectar(
            self.disp.portas.get(numero=1), self.disp_ap.portas.get(numero=1),
            usuario=self.user,
        )
        rede.conectar(self.disp.portas.get(numero=2), destino_item=self.camera,
                      usuario=self.user)

    def _grafo(self, **kw):
        return rede.montar_grafo(localidade_id=self.local.pk, com_status=False, **kw)

    def test_cabo_entra_uma_vez_so(self):
        g = self._grafo()
        chaves = {(e["de"], e["para"]) for e in g["enlaces"]}
        self.assertEqual(len(g["enlaces"]), 3, g["enlaces"])
        self.assertEqual(len(chaves), 3, "enlace duplicado no grafo")

    def test_enlace_carrega_porta_das_duas_pontas(self):
        g = self._grafo()
        fibra = next(e for e in g["enlaces"] if e["meio"] == RedeMeioChoices.FIBRA)
        self.assertTrue(fibra["porta_de"])
        self.assertTrue(fibra["porta_para"])
        self.assertIsNotNone(fibra["porta_para_id"])

    def test_enlace_para_equipamento_sem_portas_nao_tem_porta_destino(self):
        g = self._grafo()
        no_cam = next(n for n in g["nos"] if n["item_id"] == self.camera.pk)
        enl = next(e for e in g["enlaces"] if e["para"] == no_cam["id"] or e["de"] == no_cam["id"])
        self.assertEqual(enl["porta_para"], "")

    def test_kpis_batem_com_o_cadastro(self):
        k = self._grafo()["kpis"]
        self.assertEqual(k["switches"], 2)
        self.assertEqual(k["aps"], 2)
        self.assertEqual(k["enlaces"], 3)
        self.assertEqual(k["fibra"], 1)
        self.assertEqual(k["portas_totais"], 21)         # 10 + 10 + 1
        self.assertEqual(k["portas_ativas"], 5)          # 2 uplinks + 2 pontas + 1 câmera
        self.assertEqual(k["portas_livres"], 16)

    def test_ativo_de_nucleo_sem_faceta_aparece_como_nao_mapeado(self):
        """
        Um switch cadastrado hoje tem de aparecer no mapa hoje. Exigir o
        cadastro da faceta de rede antes o deixaria invisível até alguém
        lembrar dele.
        """
        novo = Item.objects.create(
            nome="SW-NOVO-TI", subtipo=self.sub_switch, localidade=self.local,
            status="ativo", criado_por=self.user, atualizado_por=self.user,
        )
        g = self._grafo()
        no = next(n for n in g["nos"] if n["item_id"] == novo.pk)
        self.assertFalse(no["mapeado"])
        self.assertIsNone(no["dispositivo_id"])
        self.assertEqual(no["portas_total"], 0)

    def test_sem_terminais_a_espinha_dorsal_continua_de_pe(self):
        g = self._grafo(incluir_terminais=False)
        classes = {n["classe"] for n in g["nos"]}
        self.assertIn("switch", classes)
        self.assertNotIn("ap", classes)
        for e in g["enlaces"]:
            self.assertIn(e["de"], {n["id"] for n in g["nos"]})
            self.assertIn(e["para"], {n["id"] for n in g["nos"]})

    def test_item_excluido_fica_fora(self):
        self.ap2.excluido = True
        self.ap2.save()
        g = self._grafo()
        self.assertNotIn(self.ap2.pk, [n["item_id"] for n in g["nos"]])

    def test_classificacao_por_subtipo_cobre_o_parque_real(self):
        self.assertEqual(rede.classe_de("Switches")[0], "switch")
        self.assertEqual(rede.classe_de("Access-Point")[0], "ap")
        self.assertEqual(rede.classe_de("STARLINK")[0], "starlink")
        self.assertEqual(rede.classe_de("Câmera")[0], "camera")
        self.assertEqual(rede.classe_de("Cisco WLC")[0], "wlc")
        self.assertEqual(rede.classe_de("ANTENA PAINEL")[0], "antena")
        self.assertEqual(rede.classe_de("Headset")[0], "generico")
        self.assertEqual(rede.classe_de("")[0], "generico")

    def test_papel_do_dispositivo_vence_o_padrao_do_subtipo(self):
        self.disp.papel = RedePapelChoices.CORE
        self.disp.save()
        g = self._grafo()
        no = next(n for n in g["nos"] if n["item_id"] == self.switch.pk)
        self.assertEqual(no["papel"], RedePapelChoices.CORE)


class ArranjoTests(BaseRede):
    def test_todo_no_recebe_posicao(self):
        g = rede.montar_grafo(localidade_id=self.local.pk, com_status=False)
        a = rede.auto_arranjo(g["nos"], g["enlaces"])
        for no in g["nos"]:
            self.assertIn(no["id"], a["nos"], f"{no['nome']} ficaria em 0,0")
            self.assertGreaterEqual(a["nos"][no["id"]]["x"], 0)

    def test_faixa_por_camada_e_por_localidade(self):
        g = rede.montar_grafo(localidade_id=self.local.pk, com_status=False)
        por_papel = rede.auto_arranjo(g["nos"], g["enlaces"], "papel")
        por_local = rede.auto_arranjo(g["nos"], g["enlaces"], "localidade")
        self.assertTrue(por_papel["zonas"])
        self.assertTrue(por_local["zonas"])
        self.assertEqual([z["nome"] for z in por_local["zonas"]], [self.local.local])

    def test_canvas_cobre_o_desenho(self):
        g = rede.montar_grafo(localidade_id=self.local.pk, com_status=False)
        a = rede.auto_arranjo(g["nos"], g["enlaces"])
        maior_y = max(p["y"] for p in a["nos"].values())
        self.assertGreater(a["canvas"]["height"], maior_y)

    def test_arranjo_sem_nos_nao_estoura(self):
        a = rede.auto_arranjo([], [])
        self.assertEqual(a["nos"], {})
        self.assertEqual(a["zonas"], [])


class ConferenciaTests(BaseRede):
    def _avisos(self):
        g = rede.montar_grafo(localidade_id=self.local.pk, com_status=False)
        return {a["codigo"]: a for a in g["avisos"]}

    def test_ip_repetido_e_erro(self):
        self.disp.ip_gerencia = "172.16.60.10"
        self.disp.save()
        self.disp2.ip_gerencia = "172.16.60.10"
        self.disp2.save()
        aviso = self._avisos()["ip_duplicado"]
        self.assertEqual(aviso["nivel"], "erro")
        self.assertEqual(len(aviso["nos"]), 2)

    def test_switch_isolado_entra_agrupado(self):
        """
        Um aviso por switch daria 92 linhas idênticas no dia em que o mapa
        começa vazio — e lista de 92 avisos iguais não é lida, é fechada.
        """
        aviso = self._avisos()["sem_enlace"]
        self.assertEqual(aviso["nivel"], "alerta")
        self.assertEqual(len(aviso["nos"]), 2)
        self.assertIn("2 switch", aviso["titulo"])

    def test_porta_ativa_sem_destino(self):
        rede.gerar_portas(self.disp, "generico_8", usuario=self.user)
        p = self.disp.portas.get(numero=1)
        p.status = RedePortaStatusChoices.ATIVA
        p.save()
        aviso = self._avisos()["porta_ativa_sem_destino"]
        self.assertEqual(aviso["nivel"], "alerta")

    def test_ocupacao_no_limite_avisa(self):
        rede.gerar_portas(self.disp, "generico_8", usuario=self.user)
        for p in self.disp.portas.all():
            p.status = RedePortaStatusChoices.ATIVA
            p.save()
        aviso = self._avisos()["ocupacao_limite"]
        self.assertEqual(aviso["nivel"], "alerta")
        self.assertIn("100%", aviso["titulo"])

    def test_switch_sem_portas_avisa(self):
        self.assertIn("sem_portas", self._avisos())

    def test_avisos_vem_do_mais_grave_para_o_menos(self):
        self.disp.ip_gerencia = "10.0.0.1"
        self.disp.save()
        self.disp2.ip_gerencia = "10.0.0.1"
        self.disp2.save()
        g = rede.montar_grafo(localidade_id=self.local.pk, com_status=False)
        niveis = [a["nivel"] for a in g["avisos"]]
        self.assertEqual(niveis[0], "erro")
        self.assertEqual(niveis, sorted(niveis, key=lambda n: {"erro": 0, "alerta": 1, "info": 2}[n]))


class ImpactoTests(BaseRede):
    def test_queda_do_switch_isola_quem_depende_dele(self):
        """
        O que o escopo do projeto pede (seção 7.12): a causa provável está no
        switch, não nas 12 câmeras.
        """
        rede.gerar_portas(self.disp, "cisco_c1000_8p", usuario=self.user)
        rede.gerar_portas(self.disp2, "cisco_c1000_8p", usuario=self.user)
        rede.gerar_portas(self.disp_ap, "terminal_1", usuario=self.user)
        self.disp.papel = RedePapelChoices.BORDA
        self.disp.save()
        self.disp2.papel = RedePapelChoices.ACESSO
        self.disp2.save()

        rede.conectar(self.disp.portas.get(numero=9), self.disp2.portas.get(numero=9),
                      usuario=self.user)
        rede.conectar(self.disp2.portas.get(numero=1), self.disp_ap.portas.get(numero=1),
                      usuario=self.user)

        g = rede.montar_grafo(localidade_id=self.local.pk, com_status=False)
        no_sw2 = next(n for n in g["nos"] if n["item_id"] == self.switch2.pk)
        imp = rede.impacto(no_sw2["id"], g["nos"], g["enlaces"])

        isolados = {i["nome"] for i in imp["isolados"]}
        self.assertIn(self.ap1.nome, isolados)
        self.assertEqual(imp["diretos"], 2)

        # Quem NUNCA teve cabo não é vítima desta queda. A primeira versão
        # listava todo nó que a borda não alcança, e com 89 switches sem
        # enlace cadastrado o relatório culpava um switch de acesso por
        # equipamentos que já estavam fora do grafo antes dele cair.
        self.assertNotIn(self.ap2.nome, isolados)
        self.assertGreater(imp["ja_isolados"], 0)


# ──────────────────────────────────────────────────────────────────────────────
# A ponte servidor → JS, e as fatias de marcação
# ──────────────────────────────────────────────────────────────────────────────

class ConfigDaBancadaTests(BaseRede):
    """Mesmo contrato do editor de plantas, para o mesmo risco."""

    CHAVES = {
        "grafo_url", "salvar_url", "dispositivo_url", "portas_url", "porta_url",
        "enlace_url", "impacto_url", "historico_url", "inventario_url",
        "criar_url", "prtg_busca_url", "exportar_url", "csrf_token", "prtg_ok",
        "mapa_id", "mapa_nome", "mapa_versao", "layout", "catalogo",
        # A planta georreferenciada é tela irmã: a bancada precisa do link
        # para ela, da lista de rotas (seletor da porta) e da elevação do rack.
        "planta_url", "rota_url", "elevacao_url",
    }

    def _abrir(self):
        return self.client.get(reverse("rede_mapa"))

    def test_bancada_abre(self):
        self.assertEqual(self._abrir().status_code, 200)

    def test_config_tem_exatamente_as_chaves_que_o_js_le(self):
        self.assertEqual(set(self._abrir().context["mapa_config"]), self.CHAVES)

    def test_js_le_todas_as_chaves_da_config(self):
        js = (_JS / "rede-mapa.js").read_text(encoding="utf-8")
        lidas = set(re.findall(r"\bCFG\.(\w+)", js))
        self.assertEqual(
            lidas, self.CHAVES,
            f"só no JS: {lidas - self.CHAVES} | só na view: {self.CHAVES - lidas}",
        )

    def test_urls_resolvidas_e_nao_literais(self):
        cfg = self._abrir().context["mapa_config"]
        for chave in (c for c in self.CHAVES if c.endswith("_url")):
            self.assertTrue(cfg[chave].startswith("/"), f"{chave} = {cfg[chave]!r}")

    def test_sem_mapa_salvo_a_bancada_abre_na_visao_corporativa(self):
        cfg = self._abrir().context["mapa_config"]
        self.assertIsNone(cfg["mapa_id"])
        self.assertEqual(cfg["mapa_versao"], 0)
        self.assertEqual(cfg["layout"], {})

    def test_todas_as_fatias_chegam_ao_html(self):
        """
        Include com caminho errado não levanta erro: renderiza vazio. Os ids
        aqui são os que `rede-mapa.js` procura — perder um deixa a bancada
        aberta e inerte.
        """
        html = self._abrir().content.decode("utf-8")
        ancoras = {
            "topbar": 'id="localidade"',
            "trilho_esq": 'id="inventario"',
            "prancha": 'id="prancha"',
            "inspetor": 'id="faceChassi"',
            "status": 'id="lerSalvo"',
            "modais": 'id="modalCabo"',
        }
        for fatia, ancora in ancoras.items():
            self.assertIn(ancora, html, f"a fatia {fatia} não chegou ao HTML")
        self.assertNotIn("_nx_", html, "sobrou nome de include no HTML")

    def test_fatias_existem(self):
        for fatia in ("topbar", "trilho_esq", "prancha", "inspetor", "status", "modais"):
            caminho = _TEMPLATES / "rede" / f"_nx_{fatia}.html"
            self.assertTrue(caminho.is_file(), f"_nx_{fatia}.html não existe")

    def test_json_script_vem_antes_do_script(self):
        html = self._abrir().content.decode("utf-8")
        self.assertLess(html.find('id="__rede_config"'), html.find("rede-mapa.js"))

    def test_estaticos_entram_com_cache_busting(self):
        html = self._abrir().content.decode("utf-8")
        self.assertRegex(html, r"ds-rede\.css\?v=")
        self.assertRegex(html, r"rede-mapa\.js\?v=")

    def test_template_da_bancada_nao_tem_css_nem_js_inline(self):
        """A regra que veio da extração do editor: folha e script em static/."""
        html = self._abrir().content.decode("utf-8")
        inicio = html.find('class="nx-app"')
        self.assertNotIn("<style", html[inicio:], "CSS inline na bancada")

    def test_ids_lidos_pelo_js_existem_no_html(self):
        """
        `getElementById` de id inexistente devolve `null` sem erro, e o
        primeiro clique estoura no console — nunca no servidor. Este teste é
        a ponte no sentido oposto do contrato de config.
        """
        html = self._abrir().content.decode("utf-8")
        js = (_JS / "rede-mapa.js").read_text(encoding="utf-8")
        lista = re.search(r"function cachearElementos\(\) \{\s*\[(.*?)\]\.forEach", js, re.S)
        self.assertIsNotNone(lista, "a lista de ids do JS mudou de forma")
        ids = re.findall(r"'([A-Za-z0-9_]+)'", lista.group(1))
        self.assertGreater(len(ids), 40)
        faltando = [i for i in ids if f'id="{i}"' not in html]
        self.assertFalse(faltando, f"o JS procura ids que o HTML não tem: {faltando}")


# ──────────────────────────────────────────────────────────────────────────────
# Views e APIs
# ──────────────────────────────────────────────────────────────────────────────

class ApisTests(BaseRede):
    def test_toda_rota_exige_login(self):
        anonimo = Client()
        rotas = [
            reverse("rede_mapa"), reverse("rede_grafo_api"),
            reverse("rede_dispositivo_api"), reverse("rede_portas_api"),
            reverse("rede_porta_api"), reverse("rede_enlace_api"),
            reverse("rede_impacto_api"), reverse("rede_historico_api"),
            reverse("rede_inventario_api"), reverse("rede_mapa_export"),
            reverse("rede_mapa_criar"), reverse("rede_mapa_salvar"),
        ]
        for rota in rotas:
            resp = anonimo.get(rota)
            self.assertIn(resp.status_code, (302, 301), f"{rota} abriu sem login")

    def test_grafo_responde_json_com_kpis_e_avisos(self):
        resp = self.client.get(reverse("rede_grafo_api"), {"status": "0", "arranjo": "1"})
        self.assertEqual(resp.status_code, 200)
        dados = resp.json()
        self.assertTrue(dados["ok"])
        for chave in ("nos", "enlaces", "kpis", "avisos", "arranjo"):
            self.assertIn(chave, dados)

    def test_ficha_cria_a_faceta_de_rede_sob_demanda(self):
        self.assertFalse(RedeDispositivo.objects.filter(item=self.camera).exists())
        resp = self.client.get(reverse("rede_dispositivo_api"), {"item": self.camera.pk})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(RedeDispositivo.objects.filter(item=self.camera).exists())
        self.assertEqual(resp.json()["item"]["nome"], self.camera.nome)

    def test_gravar_ficha_valida_o_ip(self):
        resp = self.client.post(
            reverse("rede_dispositivo_api"),
            json.dumps({"item": self.switch.pk, "ip": "nao-e-um-ip"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()["ok"])

    def test_gravar_ficha_aceita_papel_ip_e_prtg(self):
        resp = self.client.post(
            reverse("rede_dispositivo_api"),
            json.dumps({
                "item": self.switch.pk, "papel": "core",
                "ip": "172.16.60.9", "prtg_objid": "2051",
                "observacoes": "VLAN 10 nativa",
            }),
            content_type="application/json",
        )
        self.assertTrue(resp.json()["ok"])
        self.disp.refresh_from_db()
        self.assertEqual(self.disp.papel, "core")
        self.assertEqual(self.disp.ip_gerencia, "172.16.60.9")
        self.assertEqual(self.disp.prtg_objid, 2051)

    def test_papel_invalido_nao_passa(self):
        self.client.post(
            reverse("rede_dispositivo_api"),
            json.dumps({"item": self.switch.pk, "papel": "imperador"}),
            content_type="application/json",
        )
        self.disp.refresh_from_db()
        self.assertEqual(self.disp.papel, RedePapelChoices.ACESSO)

    def test_gerar_portas_pela_api(self):
        resp = self.client.post(
            reverse("rede_portas_api"),
            json.dumps({"item": self.switch.pk, "acao": "gerar", "modelo": "generico_8"}),
            content_type="application/json",
        )
        dados = resp.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["criadas"], 8)
        self.assertEqual(len(dados["acessos"]), 8)

    def test_modelo_fora_do_catalogo_recusado(self):
        resp = self.client.post(
            reverse("rede_portas_api"),
            json.dumps({"item": self.switch.pk, "acao": "gerar", "modelo": "x"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_marcar_porta_como_livre_solta_o_cabo(self):
        """
        Destino gravado numa porta "livre" é exatamente a inconsistência que
        a conferência técnica aponta. A API não pode criá-la.
        """
        rede.gerar_portas(self.disp, "generico_8", usuario=self.user)
        rede.gerar_portas(self.disp_ap, "terminal_1", usuario=self.user)
        a = self.disp.portas.get(numero=1)
        b = self.disp_ap.portas.get(numero=1)
        rede.conectar(a, b, usuario=self.user)

        self.client.post(
            reverse("rede_porta_api"),
            json.dumps({"porta": a.pk, "status": "livre"}),
            content_type="application/json",
        )
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertIsNone(a.destino_porta_id)
        self.assertIsNone(a.destino_item_id)
        self.assertEqual(b.status, RedePortaStatusChoices.LIVRE)

    def test_conectar_e_desconectar_pela_api(self):
        rede.gerar_portas(self.disp, "generico_8", usuario=self.user)
        rede.gerar_portas(self.disp_ap, "terminal_1", usuario=self.user)
        a = self.disp.portas.get(numero=1)
        b = self.disp_ap.portas.get(numero=1)

        resp = self.client.post(
            reverse("rede_enlace_api"),
            json.dumps({"porta": a.pk, "destino_porta": b.pk, "meio": "utp"}),
            content_type="application/json",
        )
        self.assertTrue(resp.json()["ok"])
        b.refresh_from_db()
        self.assertEqual(b.destino_porta_id, a.pk)

        resp = self.client.post(
            reverse("rede_enlace_api"),
            json.dumps({"porta": a.pk, "acao": "desconectar"}),
            content_type="application/json",
        )
        self.assertTrue(resp.json()["ok"])
        b.refresh_from_db()
        self.assertIsNone(b.destino_porta_id)

    def test_enlace_no_mesmo_equipamento_responde_400(self):
        rede.gerar_portas(self.disp, "generico_8", usuario=self.user)
        a = self.disp.portas.get(numero=1)
        b = self.disp.portas.get(numero=2)
        resp = self.client.post(
            reverse("rede_enlace_api"),
            json.dumps({"porta": a.pk, "destino_porta": b.pk}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("mesmo equipamento", resp.json()["erro"])

    def test_excluir_porta_com_cabo_solta_o_outro_lado(self):
        rede.gerar_portas(self.disp, "generico_8", usuario=self.user)
        rede.gerar_portas(self.disp_ap, "terminal_1", usuario=self.user)
        a = self.disp.portas.get(numero=1)
        b = self.disp_ap.portas.get(numero=1)
        rede.conectar(a, b, usuario=self.user)

        self.client.post(
            reverse("rede_porta_api"),
            json.dumps({"porta": a.pk, "acao": "excluir"}),
            content_type="application/json",
        )
        self.assertFalse(RedePorta.objects.filter(pk=a.pk).exists())
        b.refresh_from_db()
        self.assertIsNone(b.destino_porta_id)
        self.assertEqual(b.status, RedePortaStatusChoices.LIVRE)

    def test_metodo_errado_responde_405(self):
        for rota in ("rede_portas_api", "rede_porta_api", "rede_mapa_salvar", "rede_mapa_criar"):
            resp = self.client.get(reverse(rota))
            self.assertEqual(resp.status_code, 405, rota)

    def test_inventario_filtra_por_localidade_e_busca(self):
        resp = self.client.get(reverse("rede_inventario_api"), {"localidade": self.local.pk})
        nomes = [r["nome"] for r in resp.json()["resultados"]]
        self.assertIn(self.switch.nome, nomes)

        resp = self.client.get(reverse("rede_inventario_api"), {"q": "AP-01"})
        nomes = [r["nome"] for r in resp.json()["resultados"]]
        self.assertIn(self.ap1.nome, nomes)
        self.assertNotIn(self.switch.nome, nomes)

    def test_painel_de_portas_separa_fileira_par_e_impar(self):
        """
        No equipamento a porta 1 e a 2 ficam uma sobre a outra. Fileira única
        não bate com a etiqueta do rack e conferir vira contagem.
        """
        rede.gerar_portas(self.disp, "cisco_c1000_8p", usuario=self.user)
        painel = rede.painel_portas(self.disp)
        self.assertEqual(painel["acessos"][0]["fileira"], 0)
        self.assertEqual(painel["acessos"][1]["fileira"], 1)
        self.assertEqual(painel["acessos"][0]["coluna"], 0)
        self.assertEqual(painel["acessos"][2]["coluna"], 1)
        self.assertEqual(len(painel["uplinks"]), 2)


class MapaSalvoTests(BaseRede):
    def setUp(self):
        super().setUp()
        self.mapa = RedeTopologia.objects.create(
            nome="Mapa de teste", localidade=self.local,
            layout={}, criado_por=self.user, atualizado_por=self.user,
        )

    def test_salvar_geometria_sobe_a_versao(self):
        resp = self.client.post(
            reverse("rede_mapa_salvar"),
            json.dumps({
                "mapa": self.mapa.pk, "versao": self.mapa.versao,
                "layout": {"nos": {"i1": {"x": 20, "y": 40}}, "canvas": {"width": 1400, "height": 900}},
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.json()["versao"], 2)
        self.mapa.refresh_from_db()
        self.assertEqual(self.mapa.layout["nos"]["i1"]["x"], 20)

    def test_conflito_de_versao_nao_sobrescreve(self):
        resp = self.client.post(
            reverse("rede_mapa_salvar"),
            json.dumps({"mapa": self.mapa.pk, "versao": 99, "layout": {}}),
            content_type="application/json",
        )
        dados = resp.json()
        self.assertFalse(dados["ok"])
        self.assertTrue(dados["conflito"])
        self.mapa.refresh_from_db()
        self.assertEqual(self.mapa.versao, 1)

    def test_layout_descarta_chave_desconhecida_e_limita_coordenada(self):
        """
        O layout vai para um JSONField que a tela depois renderiza. Aceitar o
        que o cliente mandar é aceitar carga arbitrária na coluna.
        """
        self.client.post(
            reverse("rede_mapa_salvar"),
            json.dumps({
                "mapa": self.mapa.pk, "versao": 1,
                "layout": {
                    "nos": {"i1": {"x": 999999, "y": -999999, "script": "<img onerror=1>"}},
                    "zonas": [{"nome": "Z" * 400, "x": 0, "y": 0, "w": 10, "h": 10, "lixo": 1}],
                    "enlaces": [{"de": "i1", "para": "i2"}],
                },
            }),
            content_type="application/json",
        )
        self.mapa.refresh_from_db()
        no = self.mapa.layout["nos"]["i1"]
        self.assertNotIn("script", no)
        self.assertEqual(no["x"], 20000)
        self.assertEqual(no["y"], -20000)
        self.assertNotIn("lixo", self.mapa.layout["zonas"][0])
        self.assertEqual(len(self.mapa.layout["zonas"][0]["nome"]), 80)
        self.assertNotIn("enlaces", self.mapa.layout,
                         "o mapa não guarda enlace — ele é fato da porta")

    def test_versao_gravada_entra_no_historico_e_restaura(self):
        self.client.post(
            reverse("rede_mapa_salvar"),
            json.dumps({
                "mapa": self.mapa.pk, "versao": 1, "nova_versao": True,
                "descricao": "antes de mexer",
                "layout": {"nos": {"i1": {"x": 10, "y": 10}}},
            }),
            content_type="application/json",
        )
        hist = self.client.get(reverse("rede_historico_api"), {"mapa": self.mapa.pk}).json()
        self.assertEqual(len(hist["historico"]), 1)
        self.assertEqual(hist["historico"][0]["descricao"], "antes de mexer")

        entrada = hist["historico"][0]
        resp = self.client.post(
            f"{reverse('rede_historico_api')}?mapa={self.mapa.pk}&restaurar={entrada['id']}"
        )
        self.assertTrue(resp.json()["ok"])

    def test_criar_mapa_devolve_a_url_de_abertura(self):
        resp = self.client.post(
            reverse("rede_mapa_criar"),
            json.dumps({"nome": "Rede — Karitel", "localidade": self.local.pk, "area": ""}),
            content_type="application/json",
        )
        dados = resp.json()
        self.assertTrue(dados["ok"])
        self.assertIn("?mapa=", dados["url"])

    def test_criar_mapa_sem_nome_recusa(self):
        resp = self.client.post(
            reverse("rede_mapa_criar"),
            json.dumps({"nome": "   "}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_excluir_mapa_nao_toca_no_cabeamento(self):
        rede.gerar_portas(self.disp, "generico_8", usuario=self.user)
        rede.gerar_portas(self.disp_ap, "terminal_1", usuario=self.user)
        rede.conectar(self.disp.portas.get(numero=1), self.disp_ap.portas.get(numero=1),
                      usuario=self.user)
        antes = RedePorta.objects.filter(status=RedePortaStatusChoices.ATIVA).count()

        self.client.post(reverse("rede_mapa_excluir", args=[self.mapa.pk]))

        self.assertFalse(RedeTopologia.objects.filter(pk=self.mapa.pk).exists())
        self.assertEqual(
            RedePorta.objects.filter(status=RedePortaStatusChoices.ATIVA).count(), antes,
            "excluir o desenho não pode desfazer cabo",
        )


class ExportacaoTests(BaseRede):
    def test_excel_sai_com_as_quatro_abas(self):
        from io import BytesIO
        from openpyxl import load_workbook

        rede.gerar_portas(self.disp, "generico_8", usuario=self.user)
        resp = self.client.get(reverse("rede_mapa_export"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("spreadsheetml", resp["Content-Type"])
        wb = load_workbook(BytesIO(resp.content))
        self.assertEqual(wb.sheetnames, ["Resumo", "Equipamentos", "Portas", "Conferência"])
        self.assertGreater(wb["Portas"].max_row, 5, "a aba de portas saiu vazia")

    def test_excel_respeita_o_recorte_por_localidade(self):
        from io import BytesIO
        from openpyxl import load_workbook

        outra = Localidade.objects.create(local="MAMBAÍ — TESTE")
        resp = self.client.get(reverse("rede_mapa_export"), {"localidade": outra.pk})
        wb = load_workbook(BytesIO(resp.content))
        nomes = [c.value for c in wb["Equipamentos"]["A"]]
        self.assertNotIn(self.switch.nome, nomes)


# ──────────────────────────────────────────────────────────────────────────────
# O design system do módulo
# ──────────────────────────────────────────────────────────────────────────────

class DesignSystemDaRedeTests(TestCase):
    FOLHA = _CSS / "ds-rede.css"

    def test_folha_existe_e_tem_substancia(self):
        self.assertTrue(self.FOLHA.is_file())
        self.assertGreater(self.FOLHA.read_text(encoding="utf-8").count("\n"), 400)

    def _css_sem_print(self):
        texto = self.FOLHA.read_text(encoding="utf-8")
        texto = re.split(r"@media\s+print\s*\{", texto)[0]
        return re.sub(r"/\*.*?\*/", "", texto, flags=re.S)

    def test_nao_sobrescreve_token_do_sistema_no_root(self):
        """Regra 6 e 26: token nasce em tokens.css; a folha de módulo escopa."""
        css = self._css_sem_print()
        self.assertNotRegex(css, r"(^|\})\s*:root\s*\{",
                            "ds-rede.css redefine token no :root")
        self.assertIn(":where(.nx-app", css, "a folha tem de escopar no módulo")

    def test_todo_token_do_modulo_tem_reserva(self):
        """
        `var(--x)` onde `--x` não existe é falha silenciosa: o navegador cai
        no valor inicial e a tela abre sem cor, com HTTP 200 (regra 26).
        """
        css = self._css_sem_print()
        achado = re.search(r":where\(\.nx-app[^)]*\)\s*\{(.*?)\n\}", css, re.S)
        self.assertIsNotNone(achado, "o bloco de tokens do módulo mudou de forma")
        bloco = achado.group(1)
        sem_reserva = [
            m for m in re.findall(r"--nx-[a-z0-9-]+:\s*var\((--[a-z0-9-]+)\)\s*;", bloco)
        ]
        self.assertFalse(sem_reserva, f"token do módulo sem reserva: {sem_reserva}")

    def test_aresta_reta_e_a_linguagem_do_modulo(self):
        """
        O módulo é prancha técnica: raio é linguagem de app de consumo. Um
        `border-radius` com medida aqui é deriva do desenho, não ajuste.
        """
        css = self._css_sem_print()
        self.assertNotRegex(css, r"border-radius:\s*[\d.]+(px|rem)")
        self.assertNotRegex(css, r"border-radius:\s*(999|99)px")

    def test_nenhum_tamanho_de_texto_escrito_a_mao(self):
        css = self._css_sem_print()
        self.assertNotRegex(css, r"font-size:\s*[\d.]+(px|rem)")

    def test_nenhuma_cor_literal_fora_de_reserva(self):
        """
        Cor de domínio (situação, meio do enlace, porta) é permitida pela
        regra 33 — mas aqui ela também sai de token, então a folha inteira
        acompanha o tema sem exceção registrada em teste nenhum.
        """
        css = self._css_sem_print()
        soltas = []
        for m in re.finditer(r"(--[a-z0-9-]+|[a-z-]+)\s*:\s*([^;{}]*?)(#[0-9a-fA-F]{3,8})\b", css):
            valor = m.group(2)
            if valor.count("(") > valor.count(")"):
                continue                       # reserva dentro de var(...)
            soltas.append(m.group(0).strip()[:60])
        self.assertFalse(soltas, f"cor literal solta em ds-rede.css: {soltas}")

    def test_nao_carrega_fonte_propria(self):
        """Uma fonte só no sistema (regra 30): a voz do módulo vem da
        geometria e do monoespaçado que o sistema já tem."""
        css = self.FOLHA.read_text(encoding="utf-8")
        self.assertNotIn("@import", css)
        self.assertNotIn("@font-face", css)
        self.assertNotIn("Segoe UI", css)

    # Prefixos que ANCORAM uma regra dentro do módulo. O que vem depois do
    # primeiro combinador pode ser qualquer classe (`.tvgr-page :is(.btn…)`
    # veste o botão do sistema DENTRO da tela, e só dentro dela) — o que não
    # pode é a regra nascer fora.
    _ANCORAS = (
        "nx-",       # o prefixo do módulo
        "tvgr-",     # gerenciar acesso TV: o JS injeta cartão com estes nomes
        "vw-",       # visualizador
        "tv-",       # modo TV
        "pe-",       # editor
        "plt-",      # as raízes herdadas: .plt-vw-page e .plt-ed-page
        "tvl-",      # lista de plantas no modo TV
        "main-",     # as duas regras que neutralizam o respiro do shell
        "select2-",  # o dropdown nasce no fim do <body>, preso a `.nx-s2`
    )

    def test_toda_regra_nasce_dentro_do_modulo(self):
        """
        A folha é carregada em nove telas; uma regra ancorada fora do módulo
        mudaria as outras 165. O que se confere é o PRIMEIRO componente de
        cada seletor — é ele que decide onde a regra pega.
        """
        css = self._css_sem_print()
        soltos = []
        for m in re.finditer(r"(^|\n)\s*([^@\s][^{]*?)\s*\{", css):
            bruto = m.group(2).strip()
            if bruto.startswith(("@", "from", "to", "0%", "100%")):
                continue
            for parte in _virgulas_de_topo(bruto):
                parte = parte.strip()
                if not parte or parte.startswith(":where"):
                    continue
                # o primeiro componente: até o primeiro combinador
                primeiro = re.split(r"[\s>+~]", parte, 1)[0]
                classes = re.findall(r"\.([a-zA-Z][\w-]*)", primeiro)
                if not classes:
                    continue        # seletor de elemento puro (`.nx-app *`…)
                if not any(c.startswith(self._ANCORAS) for c in classes):
                    soltos.append(parte)
        self.assertFalse(soltos, f"regra ancorada fora do módulo: {soltos}")

    # Toda raiz de tela do módulo e todo elemento que o navegador coloca
    # FORA dela (modal, aviso passageiro) precisa estar no escopo dos
    # tokens — senão `var(--nx-*)` cai no valor inicial e a tela abre sem
    # cor, com HTTP 200 e nada no console.
    _RAIZES = (
        ".nx-app", ".nx-pagina", ".nx-parede", ".plt-vw-page", ".plt-ed-page",
        ".tvgr-page", ".tv-root", ".nx-modal", ".nx-toast", "#tvgrToast",
    )

    def test_toda_raiz_do_modulo_esta_no_escopo_dos_tokens(self):
        css = self.FOLHA.read_text(encoding="utf-8")
        escopo = re.search(r":where\(([^)]*)\)\s*\{", css, re.S).group(1)
        faltando = [r for r in self._RAIZES if r not in escopo]
        self.assertFalse(
            faltando,
            f"raiz fora do escopo dos tokens (a tela abriria sem cor): {faltando}",
        )

    def test_raiz_de_tela_nova_entra_no_escopo_junto(self):
        """
        O outro lado do teste acima. Uma tela nova traz uma raiz nova
        (`.xx-page`); se ela aparecer na folha sem entrar no escopo dos
        tokens, a tela abre sem cor e nada acusa. Aqui a folha é varrida
        atrás de toda raiz com cara de tela, e cada uma tem de constar da
        lista — que é a mesma que o escopo declara.
        """
        css = self._css_sem_print()
        encontradas = set()
        for m in re.finditer(r"(^|\n)\s*([^@\s][^{]*?)\s*\{", css):
            for parte in _virgulas_de_topo(m.group(2).strip()):
                if parte.startswith(":where"):
                    continue
                primeiro = re.split(r"[\s>+~]", parte, 1)[0]
                achado = re.match(r"^(\.[\w-]*-page|\.nx-app|\.nx-pagina)\b", primeiro)
                if achado:
                    encontradas.add(achado.group(1))
        fora = sorted(r for r in encontradas if r not in self._RAIZES)
        self.assertFalse(
            fora,
            f"raiz de tela na folha e fora do escopo dos tokens: {fora}. "
            f"Acrescente-a ao `:where(...)` do topo e a `_RAIZES`.",
        )

    def test_select2_entra_no_escopo_dos_tokens(self):
        """
        O Select2 injeta o dropdown no FIM do `<body>`, fora de `.nx-app`:
        sem entrar no escopo, ele abre sem cor nos dois temas e sem erro
        algum. Já custou um dropdown transparente no módulo de preventivas
        (regra 48) — este teste é para não custar de novo aqui.
        """
        css = self.FOLHA.read_text(encoding="utf-8")
        escopo = re.search(r":where\(([^)]*)\)\s*\{", css).group(1)
        self.assertIn(".select2-container.nx-s2", escopo)
        self.assertIn(".select2-dropdown.nx-s2", escopo)

    def test_toda_regra_do_select2_exige_o_gancho_nx_s2(self):
        """
        Regra do Select2 sem `.nx-s2` mudaria o componente em toda tela que
        carregue esta folha — e ele é usado em metade do sistema.
        """
        css = self._css_sem_print()
        soltas = []
        for m in re.finditer(r"(^|\n)\s*([^@\s][^{]*?)\s*\{", css):
            seletor = m.group(2).strip()
            if "select2" in seletor and "nx-s2" not in seletor:
                soltas.append(seletor)
        self.assertFalse(soltas, f"regra do Select2 sem gancho: {soltas}")


class JsDaBancadaTests(TestCase):
    ARQUIVO = _JS / "rede-mapa.js"

    def test_existe_e_nao_tem_tag_de_template(self):
        self.assertTrue(self.ARQUIVO.is_file())
        texto = self.ARQUIVO.read_text(encoding="utf-8")
        self.assertNotRegex(texto, r"\{%|\{\{")

    def test_texto_do_banco_nunca_entra_por_innerhtml(self):
        """
        Nome de equipamento é digitado por gente. `innerHTML` com esse texto
        é XSS na tela de quem abrir o mapa — mesmo motivo de o layout ir por
        `json_script` e de `|safe` estar proibido no projeto.
        """
        texto = self.ARQUIVO.read_text(encoding="utf-8")
        self.assertNotIn(".innerHTML", texto)

    def test_desenho_e_dado_vem_do_servidor(self):
        """O JS não reimplementa regra de rede: arranjo, classe e aviso são
        do service."""
        texto = self.ARQUIVO.read_text(encoding="utf-8")
        self.assertIn("CFG.grafo_url", texto)
        self.assertIn("arranjo", texto)


# ──────────────────────────────────────────────────────────────────────────────
# O FACEPLATE — o painel montado como o equipamento é
# ──────────────────────────────────────────────────────────────────────────────

class FaceplateTests(BaseRede):
    """
    O arranjo do painel é conhecimento do EQUIPAMENTO, e por isso mora no
    service. Se ele voltar para o JS, o Excel e qualquer outra saída passam a
    reimplementar a mesma regra — e divergem na primeira correção.
    """

    def setUp(self):
        super().setUp()
        rede.gerar_portas(self.disp, "cisco_c1000_24p", usuario=self.user)
        self.painel = rede.painel_portas(self.disp)

    def test_blocos_de_seis_colunas_como_o_vao_do_metal(self):
        """
        Num Catalyst de 24p as portas vêm em quatro grupos de seis colunas
        (doze portas), separados por um vão. É por esse vão que se conta
        "terceiro grupo, segunda coluna" sem percorrer as 24 com o dedo.
        """
        blocos = self.painel["face"]["blocos"]
        self.assertEqual(len(blocos), 2, "24 portas = 12 colunas = 2 blocos de 6")
        self.assertEqual([b["indice"] for b in blocos], [0, 1])
        for bloco in blocos:
            self.assertLessEqual(len(bloco["colunas"]), 6)

    def test_coluna_tem_impar_em_cima_e_par_embaixo(self):
        primeira = self.painel["face"]["blocos"][0]["colunas"][0]
        self.assertEqual(primeira["topo"]["numero"], 1)
        self.assertEqual(primeira["base"]["numero"], 2)
        segunda = self.painel["face"]["blocos"][0]["colunas"][1]
        self.assertEqual(segunda["topo"]["numero"], 3)
        self.assertEqual(segunda["base"]["numero"], 4)

    def test_sfp_fica_na_gaiola_e_fora_dos_blocos(self):
        """Uplink desenhado em linha com o cobre fez mais de um técnico
        procurar o uplink no meio das portas de acesso."""
        numeros_nos_blocos = {
            p["numero"]
            for bloco in self.painel["face"]["blocos"]
            for col in bloco["colunas"]
            for p in (col["topo"], col["base"]) if p
        }
        self.assertEqual(numeros_nos_blocos, set(range(1, 25)))
        self.assertEqual(len(self.painel["face"]["uplinks"]), 4)
        for u in self.painel["face"]["uplinks"]:
            self.assertEqual(u["tipo"], RedePortaTipoChoices.SFP)
            self.assertNotIn(u["numero"], numeros_nos_blocos)

    def test_coluna_sem_par_guarda_o_lugar(self):
        """
        Painel com número ímpar de portas tem a última coluna só com a de
        cima. O `None` é o que faz a tela reservar o vazio; sem ele a coluna
        seguinte sobe e o painel deixa de bater com o equipamento.
        """
        disp = rede.dispositivo_de(self.camera, self.user)
        rede.gerar_portas(disp, "terminal_1", usuario=self.user)
        painel = rede.painel_portas(disp)
        col = painel["face"]["blocos"][0]["colunas"][0]
        self.assertEqual(col["topo"]["numero"], 1)
        self.assertIsNone(col["base"])

    def test_resumo_separa_as_cinco_situacoes(self):
        """
        Dez portas livres e dez com defeito dão a mesma "ocupação de 50%" e
        são problemas opostos. A régua segmentada só distingue os dois se o
        resumo vier por situação.
        """
        p1, p2 = self.disp.portas.order_by("numero")[:2]
        p1.status = RedePortaStatusChoices.DEFEITO
        p1.save()
        p2.status = RedePortaStatusChoices.RESERVADA
        p2.save()
        painel = rede.painel_portas(self.disp)
        self.assertEqual(painel["resumo"][RedePortaStatusChoices.DEFEITO], 1)
        self.assertEqual(painel["resumo"][RedePortaStatusChoices.RESERVADA], 1)
        self.assertEqual(sum(painel["resumo"].values()), painel["total"])

    def test_vizinhos_saem_das_portas_e_agrupam_por_equipamento(self):
        """
        Dois cabos para o mesmo switch são UM vizinho com dois enlaces, não
        dois vizinhos. Agrupar é o que faz a lista responder "ligado a quem"
        em vez de repetir o nome do vizinho a cada cabo.
        """
        rede.gerar_portas(self.disp2, "cisco_c1000_8p", usuario=self.user)
        a1, a2 = self.disp.portas.order_by("numero")[:2]
        b1, b2 = self.disp2.portas.order_by("numero")[:2]
        rede.conectar(a1, b1, usuario=self.user)
        rede.conectar(a2, b2, usuario=self.user)
        painel = rede.painel_portas(self.disp)
        self.assertEqual(len(painel["vizinhos"]), 1)
        vizinho = painel["vizinhos"][0]
        self.assertEqual(vizinho["item_id"], self.switch2.pk)
        self.assertEqual(vizinho["no"], f"i{self.switch2.pk}")
        self.assertEqual(len(vizinho["enlaces"]), 2)

    def test_vizinho_sem_porta_cadastrada_tambem_entra(self):
        """A câmera não tem portas; o cabo que vai nela existe do mesmo jeito."""
        porta = self.disp.portas.order_by("numero").first()
        rede.conectar(porta, destino_item=self.camera, usuario=self.user)
        painel = rede.painel_portas(self.disp)
        nomes = [v["nome"] for v in painel["vizinhos"]]
        self.assertIn(self.camera.nome, nomes)
        enlace = painel["vizinhos"][0]["enlaces"][0]
        self.assertEqual(enlace["porta_remota"], "", "a câmera não tem porta mapeada")

    def test_mapa_compacto_tem_uma_letra_por_porta(self):
        """
        O painel miniatura dentro do nó precisa da situação de cada porta. Com
        90 switches de 24 portas, mandar o objeto inteiro de cada porta só
        para desenhar a miniatura seriam megabytes de JSON por carga.
        """
        grafo = rede.montar_grafo(com_status=False)
        no = next(n for n in grafo["nos"] if n["item_id"] == self.switch.pk)
        self.assertEqual(len(no["portas_mapa"]), 28)
        self.assertEqual(no["portas_mapa"][:24], "L" * 24, "acesso livre = L maiúsculo")
        self.assertEqual(no["portas_mapa"][24:], "l" * 4, "SFP = minúsculo")

    def test_mapa_compacto_acompanha_a_situacao(self):
        porta = self.disp.portas.order_by("numero").first()
        porta.status = RedePortaStatusChoices.DEFEITO
        porta.save()
        grafo = rede.montar_grafo(com_status=False)
        no = next(n for n in grafo["nos"] if n["item_id"] == self.switch.pk)
        self.assertTrue(no["portas_mapa"].startswith("D"))

    def test_no_sem_faceta_nao_estoura_a_miniatura(self):
        """Ativo de núcleo sem `RedeDispositivo` entra no mapa como não
        mapeado — e sem portas para desenhar."""
        grafo = rede.montar_grafo(com_status=False)
        no = next(n for n in grafo["nos"] if n["item_id"] == self.ap2.pk)
        self.assertEqual(no["portas_mapa"], "")


# ──────────────────────────────────────────────────────────────────────────────
# Arranjo em árvore
# ──────────────────────────────────────────────────────────────────────────────

class ArranjoArvoreTests(BaseRede):
    """
    O arranjo por camada põe cada um na faixa do papel dele e ordena pelo
    nome; os uplinks cruzam o desenho. O arranjo em árvore segue o CABO, e a
    propriedade que importa é o pai ficar centrado sobre os filhos.
    """

    def setUp(self):
        super().setUp()
        # borda → switch → dois APs
        self.sub_fw = Subtipo.objects.create(
            nome="Firewall", alocado="nao",
            categoria=Categoria.objects.get(nome="Rede"),
        )
        self.fw = Item.objects.create(
            nome="FW-BORDA-TI", subtipo=self.sub_fw, localidade=self.local,
            status="ativo", criado_por=self.user, atualizado_por=self.user,
        )
        self.disp_fw = rede.dispositivo_de(self.fw, self.user)
        rede.gerar_portas(self.disp_fw, "generico_8", usuario=self.user)
        rede.gerar_portas(self.disp, "cisco_c1000_24p", usuario=self.user)
        rede.gerar_portas(self.disp_ap, "terminal_1", usuario=self.user)
        self.disp_ap2 = rede.dispositivo_de(self.ap2, self.user)
        rede.gerar_portas(self.disp_ap2, "terminal_1", usuario=self.user)

        portas_sw = list(self.disp.portas.order_by("numero"))
        rede.conectar(self.disp_fw.portas.order_by("numero").first(),
                      portas_sw[0], usuario=self.user)
        rede.conectar(portas_sw[1], self.disp_ap.portas.first(), usuario=self.user)
        rede.conectar(portas_sw[2], self.disp_ap2.portas.first(), usuario=self.user)

        self.grafo = rede.montar_grafo(com_status=False)
        self.arranjo = rede.auto_arranjo(
            self.grafo["nos"], self.grafo["enlaces"], "arvore"
        )

    def test_todo_no_recebe_posicao(self):
        for no in self.grafo["nos"]:
            self.assertIn(no["id"], self.arranjo["nos"], no["nome"])

    def test_profundidade_vem_do_cabo_e_nao_do_papel(self):
        """
        O AP é classificado como terminal e o switch como acesso, mas quem
        decide a altura no desenho é a distância em SALTOS até a borda.
        """
        pos = self.arranjo["nos"]
        y_fw = pos[f"i{self.fw.pk}"]["y"]
        y_sw = pos[f"i{self.switch.pk}"]["y"]
        y_ap = pos[f"i{self.ap1.pk}"]["y"]
        self.assertLess(y_fw, y_sw)
        self.assertLess(y_sw, y_ap)

    def test_pai_fica_centrado_sobre_os_filhos(self):
        """É esta propriedade que transforma o uplink num traço curto e
        vertical, em vez de uma diagonal atravessando o mapa."""
        pos = self.arranjo["nos"]
        x_sw = pos[f"i{self.switch.pk}"]["x"]
        x_ap1 = pos[f"i{self.ap1.pk}"]["x"]
        x_ap2 = pos[f"i{self.ap2.pk}"]["x"]
        self.assertAlmostEqual(x_sw, (min(x_ap1, x_ap2) + max(x_ap1, x_ap2)) / 2, delta=1)

    def test_orfao_vai_para_faixa_propria(self):
        """
        Hoje são 89 switches sem enlace cadastrado. Misturá-los à árvore os
        faria parecer ligados a algo.
        """
        nomes = [z["nome"] for z in self.arranjo["zonas"]]
        self.assertTrue(any("Sem enlace" in n for n in nomes), nomes)
        orfao = self.arranjo["nos"][f"i{self.switch2.pk}"]
        na_arvore = self.arranjo["nos"][f"i{self.switch.pk}"]
        self.assertGreater(orfao["y"], na_arvore["y"])

    def test_canvas_cobre_o_desenho(self):
        maior_x = max(p["x"] for p in self.arranjo["nos"].values())
        maior_y = max(p["y"] for p in self.arranjo["nos"].values())
        self.assertGreaterEqual(self.arranjo["canvas"]["width"], maior_x)
        self.assertGreaterEqual(self.arranjo["canvas"]["height"], maior_y)

    def test_arvore_sem_nenhum_enlace_nao_estoura(self):
        """O estado em que a tela nasce: 298 equipamentos, zero cabos."""
        grafo = rede.montar_grafo(com_status=False)
        for porta in RedePorta.objects.filter(destino_porta__isnull=False):
            rede.desconectar(porta, self.user)
        grafo = rede.montar_grafo(com_status=False)
        arranjo = rede.auto_arranjo(grafo["nos"], grafo["enlaces"], "arvore")
        self.assertEqual(len(arranjo["nos"]), len(grafo["nos"]))

    def test_arranjo_vazio_nao_estoura(self):
        arranjo = rede.auto_arranjo([], [], "arvore")
        self.assertEqual(arranjo["nos"], {})


# ──────────────────────────────────────────────────────────────────────────────
# Medida em metros — o número que decide o projeto
# ──────────────────────────────────────────────────────────────────────────────

class MedidaDeRotaTests(TestCase):
    """
    O limite de 90 m do par trançado é o que separa um lance que funciona de
    um que não funciona depois de instalado. Ele só é verificável se o
    comprimento for medido sobre o traçado, em metros reais.
    """

    def test_um_grau_de_latitude_da_cento_e_onze_quilometros(self):
        m = rede.comprimento_rota([[0, 0], [1, 0]])
        self.assertAlmostEqual(m, 111195, delta=60)

    def test_cem_metros_na_coordenada_da_fazenda(self):
        """Mesma coordenada que a regra 15 manda usar para testar tile."""
        m = rede.comprimento_rota([[-14.7763, -45.5634], [-14.77720, -45.5634]])
        self.assertAlmostEqual(m, 100, delta=1)

    def test_soma_segmento_por_segmento(self):
        a = rede.comprimento_rota([[0, 0], [0.001, 0]])
        dobro = rede.comprimento_rota([[0, 0], [0.001, 0], [0.002, 0]])
        self.assertAlmostEqual(dobro, a * 2, delta=0.5)

    def test_um_ponto_so_nao_tem_comprimento(self):
        self.assertEqual(rede.comprimento_rota([[1, 2]]), 0)
        self.assertEqual(rede.comprimento_rota([]), 0)
        self.assertEqual(rede.comprimento_rota(None), 0)

    def test_coordenada_impossivel_e_descartada(self):
        """
        Latitude 95 não é ataque, é ponto impossível — e deixá-la passar faria
        o comprimento sair errado em silêncio, que é pior que recusar.
        """
        limpos = rede.limpar_pontos([[95, 0], [1, 2], [200, 300], [3, 4]])
        self.assertEqual(limpos, [[1.0, 2.0], [3.0, 4.0]])

    def test_ponto_repetido_e_descartado(self):
        """Clique duplo de quem está desenhando não acrescenta traçado."""
        limpos = rede.limpar_pontos([[1, 2], [1, 2], [1, 2], [3, 4]])
        self.assertEqual(len(limpos), 2)

    def test_lixo_no_lugar_do_ponto_nao_estoura(self):
        limpos = rede.limpar_pontos(["x", None, [1], {"lat": 1}, [1, 2]])
        self.assertEqual(limpos, [[1.0, 2.0]])

    def test_traçado_gigante_e_limitado(self):
        limpos = rede.limpar_pontos([[i / 10000, 0] for i in range(900)])
        self.assertLessEqual(len(limpos), 600)

    def test_centro_padrao_e_a_coordenada_da_empresa(self):
        """Abrir o mapa no meio do Atlântico faria a tela parecer quebrada."""
        lat, lon = rede.centro_padrao()
        self.assertAlmostEqual(lat, -14.7763, places=3)
        self.assertAlmostEqual(lon, -45.5634, places=3)

    def test_familia_de_cor_sai_do_tipo(self):
        self.assertEqual(rede.cor_de_rota(RedeRotaTipoChoices.FIBRA_SM), "fibra")
        self.assertEqual(rede.cor_de_rota(RedeRotaTipoChoices.UTP_CAT6), "utp")
        self.assertEqual(rede.cor_de_rota(RedeRotaTipoChoices.RADIO), "radio")
        self.assertEqual(rede.cor_de_rota(RedeRotaTipoChoices.DUTO_VAZIO), "duto")


class RotaTests(BaseRede):
    def test_salvar_rota_mede_o_tracado_e_ignora_medida_do_cliente(self):
        """
        Quem manda o traçado não pode mandar também a medida dele: as duas
        poderiam discordar, e o relatório passaria a afirmar um metro que o
        desenho não tem.
        """
        rota = rede.salvar_rota(RedeRota(), {
            "nome": "Vala CPD",
            "tipo": RedeRotaTipoChoices.FIBRA_SM,
            "pontos": [[-14.7763, -45.5634], [-14.77720, -45.5634]],
            "comprimento_m": 99999,
        }, usuario=self.user)
        self.assertAlmostEqual(float(rota.comprimento_m), 100, delta=1)

    def test_remedir_ao_mudar_o_tracado(self):
        rota = rede.salvar_rota(RedeRota(), {
            "nome": "R", "pontos": [[0, 0], [0.001, 0]],
        }, usuario=self.user)
        antes = float(rota.comprimento_m)
        rede.salvar_rota(rota, {"pontos": [[0, 0], [0.002, 0]]}, usuario=self.user)
        rota.refresh_from_db()
        self.assertAlmostEqual(float(rota.comprimento_m), antes * 2, delta=1)

    def test_tipo_invalido_nao_passa(self):
        rota = rede.salvar_rota(RedeRota(), {
            "nome": "R", "tipo": "cabo_de_aco", "pontos": [[0, 0], [1, 1]],
        }, usuario=self.user)
        self.assertEqual(rota.tipo, RedeRotaTipoChoices.FIBRA_SM, "cai no padrão")

    def test_cobre_sabe_que_e_cobre(self):
        """É a propriedade que faz a conferência dos 90 m valer só para o par
        trançado — a fibra não tem esse limite."""
        for tipo in (RedeRotaTipoChoices.UTP_CAT5E, RedeRotaTipoChoices.UTP_CAT6,
                     RedeRotaTipoChoices.UTP_CAT6A):
            self.assertTrue(RedeRota(tipo=tipo).e_cobre, tipo)
        for tipo in (RedeRotaTipoChoices.FIBRA_SM, RedeRotaTipoChoices.FIBRA_MM,
                     RedeRotaTipoChoices.RADIO):
            self.assertFalse(RedeRota(tipo=tipo).e_cobre, tipo)

    def test_vincular_rota_grava_nas_duas_pernas(self):
        """
        Mesma lição do `conectar`: com o vínculo em uma perna, a ocupação do
        duto sairia pela metade e o "quem cai se romper" perderia metade dos
        enlaces.
        """
        rede.gerar_portas(self.disp, "cisco_c1000_8p", usuario=self.user)
        rede.gerar_portas(self.disp2, "cisco_c1000_8p", usuario=self.user)
        a = self.disp.portas.order_by("numero").first()
        b = self.disp2.portas.order_by("numero").first()
        rede.conectar(a, b, usuario=self.user)
        rota = rede.salvar_rota(RedeRota(), {
            "nome": "Vala", "pontos": [[0, 0], [0.001, 0]],
        }, usuario=self.user)
        rede.vincular_rota(a, rota, usuario=self.user)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.rota_id, rota.pk)
        self.assertEqual(b.rota_id, rota.pk, "a outra ponta ficou sem a rota")

    def test_desvincular_solta_as_duas_pernas(self):
        rede.gerar_portas(self.disp, "cisco_c1000_8p", usuario=self.user)
        rede.gerar_portas(self.disp2, "cisco_c1000_8p", usuario=self.user)
        a = self.disp.portas.order_by("numero").first()
        b = self.disp2.portas.order_by("numero").first()
        rede.conectar(a, b, usuario=self.user)
        rota = rede.salvar_rota(RedeRota(), {"nome": "V", "pontos": [[0, 0], [1, 0]]},
                                usuario=self.user)
        rede.vincular_rota(a, rota, usuario=self.user)
        a.refresh_from_db()
        rede.vincular_rota(a, None, usuario=self.user)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertIsNone(a.rota_id)
        self.assertIsNone(b.rota_id)

    def test_enlace_do_grafo_carrega_a_rota(self):
        """É o elo entre os dois desenhos: o mesmo enlace aparece no diagrama
        e na planta."""
        rede.gerar_portas(self.disp, "cisco_c1000_8p", usuario=self.user)
        rede.gerar_portas(self.disp2, "cisco_c1000_8p", usuario=self.user)
        a = self.disp.portas.order_by("numero").first()
        b = self.disp2.portas.order_by("numero").first()
        rede.conectar(a, b, meio=RedeMeioChoices.FIBRA, usuario=self.user)
        rota = rede.salvar_rota(RedeRota(), {
            "nome": "Vala principal", "pontos": [[0, 0], [0.001, 0]],
        }, usuario=self.user)
        rede.vincular_rota(a, rota, usuario=self.user)
        grafo = rede.montar_grafo(com_status=False)
        enlace = next(e for e in grafo["enlaces"] if e["porta_de_id"] == a.pk)
        self.assertEqual(enlace["rota_id"], rota.pk)
        self.assertEqual(enlace["rota_nome"], "Vala principal")

    def test_fibra_sem_rota_entra_na_conferencia(self):
        rede.gerar_portas(self.disp, "cisco_c1000_8p", usuario=self.user)
        rede.gerar_portas(self.disp2, "cisco_c1000_8p", usuario=self.user)
        a = self.disp.portas.order_by("numero").first()
        b = self.disp2.portas.order_by("numero").first()
        rede.conectar(a, b, meio=RedeMeioChoices.FIBRA, usuario=self.user)
        grafo = rede.montar_grafo(com_status=False)
        codigos = [av["codigo"] for av in grafo["avisos"]]
        self.assertIn("fibra_sem_rota", codigos)


# ──────────────────────────────────────────────────────────────────────────────
# A planta: racks, posição e conferência física
# ──────────────────────────────────────────────────────────────────────────────

class PlantaTests(BaseRede):
    def setUp(self):
        super().setUp()
        self.rack = rede.salvar_rack(RedeRack(), {
            "nome": "Rack do CPD", "codigo": "RK-01", "us": 12,
            "latitude": "-14.7763", "longitude": "-45.5634",
            "localidade": self.local.pk,
        }, usuario=self.user)

    def test_equipamento_herda_a_posicao_do_rack(self):
        """
        É o que torna o cadastro viável: ninguém aponta no mapa a coordenada
        de 24 switches do mesmo armário, mas aponta a do armário uma vez.
        """
        rede.salvar_posicao(self.disp, {"rack": self.rack.pk, "rack_u": 3}, self.user)
        planta = rede.montar_planta(com_status=False)
        eq = next(e for e in planta["equipamentos"] if e["item_id"] == self.switch.pk)
        self.assertAlmostEqual(eq["lat"], -14.7763, places=4)
        self.assertTrue(eq["geo_herdada"])

    def test_coordenada_propria_vence_a_do_rack(self):
        rede.salvar_posicao(self.disp, {
            "rack": self.rack.pk, "latitude": "-10.5", "longitude": "-40.5",
        }, self.user)
        planta = rede.montar_planta(com_status=False)
        eq = next(e for e in planta["equipamentos"] if e["item_id"] == self.switch.pk)
        self.assertAlmostEqual(eq["lat"], -10.5, places=4)
        self.assertFalse(eq["geo_herdada"])

    def test_meia_coordenada_nao_e_posicao(self):
        """
        Latitude sem longitude cairia no meridiano de Greenwich, a 4.000 km da
        fazenda, e apareceria no mapa como se fosse dado bom.
        """
        rede.salvar_posicao(self.disp, {"latitude": "-10.5", "longitude": ""}, self.user)
        self.disp.refresh_from_db()
        self.assertIsNone(self.disp.latitude)
        self.assertIsNone(self.disp.longitude)

    def test_latitude_fora_do_globo_e_recusada(self):
        rede.salvar_posicao(self.disp, {"latitude": "95", "longitude": "10"}, self.user)
        self.disp.refresh_from_db()
        self.assertIsNone(self.disp.latitude)

    def test_altura_zero_no_rack_nao_existe(self):
        """
        Um equipamento de 0U zeraria a soma de ocupação: o armário pareceria
        vazio com vinte switches dentro, e o aviso de "sem espaço" nunca
        dispararia.
        """
        rede.salvar_posicao(self.disp, {"rack_altura_u": 0}, self.user)
        self.disp.refresh_from_db()
        self.assertEqual(self.disp.rack_altura_u, 1)

    def test_tirar_do_rack_limpa_a_altura(self):
        rede.salvar_posicao(self.disp, {"rack": self.rack.pk, "rack_u": 5}, self.user)
        rede.salvar_posicao(self.disp, {"rack": None}, self.user)
        self.disp.refresh_from_db()
        self.assertIsNone(self.disp.rack_id)
        self.assertIsNone(self.disp.rack_u, "posição em U de rack nenhum não faz sentido")

    def test_ocupacao_do_rack_soma_a_altura(self):
        rede.salvar_posicao(self.disp, {
            "rack": self.rack.pk, "rack_u": 1, "rack_altura_u": 2,
        }, self.user)
        rede.salvar_posicao(self.disp2, {
            "rack": self.rack.pk, "rack_u": 5, "rack_altura_u": 1,
        }, self.user)
        self.rack.refresh_from_db()
        self.assertEqual(self.rack.us_ocupados, 3)

    def test_equipamento_sem_u_nao_conta_na_ocupacao(self):
        """Está no armário, mas ninguém disse onde — e chutar a altura faria a
        régua discordar do ferro."""
        rede.salvar_posicao(self.disp, {"rack": self.rack.pk}, self.user)
        self.rack.refresh_from_db()
        self.assertEqual(self.rack.us_ocupados, 0)

    def test_kpis_da_planta(self):
        rede.salvar_posicao(self.disp, {"rack": self.rack.pk, "rack_u": 1}, self.user)
        rede.salvar_rota(RedeRota(), {
            "nome": "F1", "tipo": RedeRotaTipoChoices.FIBRA_SM,
            "pontos": [[0, 0], [0.001, 0]], "localidade": self.local.pk,
        }, usuario=self.user)
        rede.salvar_rota(RedeRota(), {
            "nome": "U1", "tipo": RedeRotaTipoChoices.UTP_CAT6,
            "pontos": [[0, 0], [0.0005, 0]], "localidade": self.local.pk,
        }, usuario=self.user)
        k = rede.montar_planta(com_status=False)["kpis"]
        self.assertEqual(k["racks"], 1)
        self.assertEqual(k["rotas"], 2)
        self.assertGreater(k["metros_fibra"], k["metros_utp"])
        self.assertAlmostEqual(k["metros"], k["metros_fibra"] + k["metros_utp"], delta=1)

    def test_centro_sai_da_media_do_que_esta_posicionado(self):
        planta = rede.montar_planta(com_status=False)
        self.assertTrue(planta["tem_posicao"])
        self.assertAlmostEqual(planta["centro"][0], -14.7763, places=3)

    def test_sem_nada_posicionado_o_centro_e_o_padrao(self):
        RedeRack.objects.all().delete()
        planta = rede.montar_planta(com_status=False)
        self.assertFalse(planta["tem_posicao"])
        self.assertAlmostEqual(planta["centro"][0], rede.centro_padrao()[0], places=3)


class ConferenciaDaPlantaTests(BaseRede):
    """
    A conferência do grafo olha CADASTRO; esta olha FÍSICA. Um lance de 112 m
    em Cat6 passa em qualquer validação de cadastro e falha no campo.
    """

    def _avisos(self):
        planta = rede.montar_planta(com_status=False)
        return {a["codigo"]: a for a in planta["avisos"]}

    def test_cobre_acima_de_noventa_metros_e_erro(self):
        rede.salvar_rota(RedeRota(), {
            "nome": "Lance impossível", "tipo": RedeRotaTipoChoices.UTP_CAT6,
            # ~0,0011 grau de latitude = ~122 m
            "pontos": [[-14.7763, -45.5634], [-14.77740, -45.5634]],
        }, usuario=self.user)
        aviso = self._avisos().get("utp_acima_do_limite")
        self.assertIsNotNone(aviso, "lance de 122 m em Cat6 tem de ser erro")
        self.assertEqual(aviso["nivel"], "erro")

    def test_fibra_longa_nao_e_erro(self):
        """O limite de 90 m é do par trançado. Fibra de 2 km é projeto normal."""
        rede.salvar_rota(RedeRota(), {
            "nome": "Fibra até o galpão", "tipo": RedeRotaTipoChoices.FIBRA_SM,
            "pontos": [[-14.7763, -45.5634], [-14.7963, -45.5634]],
        }, usuario=self.user)
        self.assertNotIn("utp_acima_do_limite", self._avisos())

    def test_cobre_no_limite_avisa_sem_ser_erro(self):
        """Entre 80 e 90 m cabe na norma, mas sem folga para os cordões."""
        rede.salvar_rota(RedeRota(), {
            "nome": "Apertado", "tipo": RedeRotaTipoChoices.UTP_CAT6,
            "pontos": [[-14.7763, -45.5634], [-14.77706, -45.5634]],
        }, usuario=self.user)
        aviso = self._avisos().get("utp_sem_folga")
        self.assertIsNotNone(aviso)
        self.assertEqual(aviso["nivel"], "alerta")

    def test_rota_com_um_ponto_so_avisa(self):
        rede.salvar_rota(RedeRota(), {"nome": "Sem traçado", "pontos": [[1, 2]]},
                         usuario=self.user)
        self.assertIn("rota_sem_tracado", self._avisos())

    def test_mais_enlaces_do_que_fibras_e_erro(self):
        rede.gerar_portas(self.disp, "cisco_c1000_8p", usuario=self.user)
        rede.gerar_portas(self.disp2, "cisco_c1000_8p", usuario=self.user)
        rota = rede.salvar_rota(RedeRota(), {
            "nome": "Duto de 1 fibra", "fibras": 1,
            "pontos": [[0, 0], [0.001, 0]],
        }, usuario=self.user)
        a1, a2 = self.disp.portas.order_by("numero")[:2]
        b1, b2 = self.disp2.portas.order_by("numero")[:2]
        rede.conectar(a1, b1, usuario=self.user)
        rede.conectar(a2, b2, usuario=self.user)
        rede.vincular_rota(a1, rota, usuario=self.user)
        rede.vincular_rota(a2, rota, usuario=self.user)
        aviso = self._avisos().get("rota_sobrecarregada")
        self.assertIsNotNone(aviso, "4 pernas em um duto de 1 fibra tem de acusar")

    def test_rack_sem_espaco_e_erro(self):
        rack = rede.salvar_rack(RedeRack(), {"nome": "Rack 2U", "us": 2}, usuario=self.user)
        rede.salvar_posicao(self.disp, {
            "rack": rack.pk, "rack_u": 1, "rack_altura_u": 4,
        }, self.user)
        self.assertIn("rack_sem_espaco", self._avisos())

    def test_dois_equipamentos_no_mesmo_u_e_erro(self):
        rack = rede.salvar_rack(RedeRack(), {"nome": "Rack", "us": 10}, usuario=self.user)
        rede.salvar_posicao(self.disp, {"rack": rack.pk, "rack_u": 4}, self.user)
        rede.salvar_posicao(self.disp2, {"rack": rack.pk, "rack_u": 4}, self.user)
        aviso = self._avisos().get("rack_colisao")
        self.assertIsNotNone(aviso, "dois ferros na mesma altura tem de acusar")
        self.assertEqual(aviso["nivel"], "erro")

    def test_colisao_conta_a_altura_inteira(self):
        """Um switch de 2U na posição 4 ocupa o U4 e o U5."""
        rack = rede.salvar_rack(RedeRack(), {"nome": "Rack", "us": 10}, usuario=self.user)
        rede.salvar_posicao(self.disp, {
            "rack": rack.pk, "rack_u": 4, "rack_altura_u": 2,
        }, self.user)
        rede.salvar_posicao(self.disp2, {"rack": rack.pk, "rack_u": 5}, self.user)
        self.assertIn("rack_colisao", self._avisos())

    def test_rack_sem_posicao_avisa(self):
        rede.salvar_rack(RedeRack(), {"nome": "Sem coordenada"}, usuario=self.user)
        self.assertIn("rack_sem_posicao", self._avisos())

    def test_avisos_vem_do_mais_grave_para_o_menos(self):
        rede.salvar_rota(RedeRota(), {
            "nome": "Impossível", "tipo": RedeRotaTipoChoices.UTP_CAT6,
            "pontos": [[-14.7763, -45.5634], [-14.77740, -45.5634]],
        }, usuario=self.user)
        rede.salvar_rack(RedeRack(), {"nome": "Sem coordenada"}, usuario=self.user)
        niveis = [a["nivel"] for a in rede.montar_planta(com_status=False)["avisos"]]
        ordem = {"erro": 0, "alerta": 1, "info": 2}
        self.assertEqual(niveis, sorted(niveis, key=lambda n: ordem[n]))


class ElevacaoDeRackTests(BaseRede):
    def setUp(self):
        super().setUp()
        self.rack = rede.salvar_rack(RedeRack(), {"nome": "Rack", "us": 10},
                                     usuario=self.user)
        rede.salvar_posicao(self.disp, {
            "rack": self.rack.pk, "rack_u": 2, "rack_altura_u": 2,
        }, self.user)

    def test_regua_desce_e_o_numero_sobe(self):
        """
        A régua vem de cima para baixo porque é assim que se olha o rack
        aberto; o NÚMERO do U conta de baixo para cima, como a etiqueta do
        fabricante. Inverter um dos dois faz a tela discordar do ferro.
        """
        elev = rede.elevacao_rack(self.rack)
        self.assertEqual(elev["regua"][0]["u"], 10, "a primeira linha é o U do topo")
        self.assertEqual(elev["regua"][-1]["u"], 1, "a última é o U do chão")

    def test_equipamento_ocupa_todas_as_alturas_dele(self):
        elev = rede.elevacao_rack(self.rack)
        por_u = {f["u"]: f["ocupante"] for f in elev["regua"]}
        self.assertIsNotNone(por_u[2])
        self.assertIsNotNone(por_u[3], "um switch de 2U no U2 também ocupa o U3")
        self.assertIsNone(por_u[4])

    def test_so_a_primeira_altura_desenha_o_bloco(self):
        """Sem isso o nome do equipamento seria desenhado duas vezes, uma por
        U ocupado."""
        elev = rede.elevacao_rack(self.rack)
        por_u = {f["u"]: f["ocupante"] for f in elev["regua"]}
        self.assertTrue(por_u[2]["primeiro"])
        self.assertFalse(por_u[3]["primeiro"])
        self.assertEqual(por_u[3]["base_u"], 2)

    def test_quem_esta_no_rack_sem_u_fica_listado_a_parte(self):
        rede.salvar_posicao(self.disp2, {"rack": self.rack.pk}, self.user)
        elev = rede.elevacao_rack(self.rack)
        nomes = [s["nome"] for s in elev["sem_posicao"]]
        self.assertIn(self.switch2.nome, nomes)

    def test_altura_acima_do_rack_nao_estoura(self):
        """Cadastro errado (U 40 num rack de 10) não pode derrubar a tela."""
        rede.salvar_posicao(self.disp2, {"rack": self.rack.pk, "rack_u": 40}, self.user)
        elev = rede.elevacao_rack(self.rack)
        self.assertEqual(len(elev["regua"]), 10)

    def test_contas_do_cabecalho(self):
        elev = rede.elevacao_rack(self.rack)
        self.assertEqual(elev["rack"]["us"], 10)
        self.assertEqual(elev["rack"]["us_ocupados"], 2)
        self.assertEqual(elev["rack"]["us_livres"], 8)


class ImpactoDeRotaTests(BaseRede):
    """
    "A retroescavadeira passou na vala — o que caiu?" é a pergunta que só a
    rota como objeto responde.
    """

    def setUp(self):
        super().setUp()
        self.sub_fw = Subtipo.objects.create(
            nome="Firewall", alocado="nao",
            categoria=Categoria.objects.get(nome="Rede"),
        )
        self.fw = Item.objects.create(
            nome="FW-BORDA", subtipo=self.sub_fw, localidade=self.local,
            status="ativo", criado_por=self.user, atualizado_por=self.user,
        )
        self.disp_fw = rede.dispositivo_de(self.fw, self.user)
        rede.gerar_portas(self.disp_fw, "generico_8", usuario=self.user)
        rede.gerar_portas(self.disp, "cisco_c1000_8p", usuario=self.user)
        rede.gerar_portas(self.disp_ap, "terminal_1", usuario=self.user)

        self.uplink = self.disp_fw.portas.order_by("numero").first()
        portas_sw = list(self.disp.portas.order_by("numero"))
        rede.conectar(self.uplink, portas_sw[0], usuario=self.user)
        rede.conectar(portas_sw[1], self.disp_ap.portas.first(), usuario=self.user)

        self.rota = rede.salvar_rota(RedeRota(), {
            "nome": "Vala do uplink", "tipo": RedeRotaTipoChoices.FIBRA_SM,
            "pontos": [[0, 0], [0.001, 0]],
        }, usuario=self.user)

    def test_romper_a_vala_do_uplink_isola_o_que_esta_atras(self):
        rede.vincular_rota(self.uplink, self.rota, usuario=self.user)
        grafo = rede.montar_grafo(com_status=False)
        imp = rede.impacto_rota(self.rota, grafo["nos"], grafo["enlaces"])
        isolados = {i["nome"] for i in imp["isolados"]}
        self.assertIn(self.switch.nome, isolados)
        self.assertIn(self.ap1.nome, isolados, "o AP depende do switch")
        self.assertNotIn(self.fw.nome, isolados, "a borda é a raiz do percurso")
        self.assertEqual(imp["enlaces_cortados"], 1)

    def test_rota_sem_enlace_nao_isola_ninguem(self):
        """
        Rota sem cabo apontado não derruba nada — e quem JÁ estava fora do
        alcance da borda não pode ser contado como vítima dela. Foi este
        teste que pegou a conta errada: a primeira versão respondia "quem a
        borda não alcança", e com 89 switches sem enlace cadastrado isso
        atribuía a qualquer vala um estrago que não era dela.
        """
        grafo = rede.montar_grafo(com_status=False)
        imp = rede.impacto_rota(self.rota, grafo["nos"], grafo["enlaces"])
        self.assertEqual(imp["enlaces_cortados"], 0)
        self.assertEqual(imp["total"], 0)
        self.assertGreater(imp["ja_isolados"], 0,
                           "os que nunca tiveram cabo têm de ser contados à parte")

    def test_quem_ja_estava_isolado_nao_entra_na_conta(self):
        """O outro lado: mesmo cortando a vala do uplink, o switch que nunca
        teve cabo não é vítima dela."""
        rede.vincular_rota(self.uplink, self.rota, usuario=self.user)
        grafo = rede.montar_grafo(com_status=False)
        imp = rede.impacto_rota(self.rota, grafo["nos"], grafo["enlaces"])
        nomes = {i["nome"] for i in imp["isolados"]}
        self.assertNotIn(self.switch2.nome, nomes)
        self.assertNotIn(self.ap2.nome, nomes)

    def test_impacto_carrega_o_comprimento(self):
        imp = rede.impacto_rota(self.rota, [], [])
        self.assertAlmostEqual(imp["comprimento_m"], float(self.rota.comprimento_m), places=1)


# ──────────────────────────────────────────────────────────────────────────────
# A tela da planta e suas APIs
# ──────────────────────────────────────────────────────────────────────────────

class ConfigDaPlantaTests(BaseRede):
    """Mesmo contrato da bancada, para o mesmo risco: chave esquecida em
    `_config_planta` é `undefined` no navegador e a tela abre inerte."""

    CHAVES = {
        "planta_url", "posicao_url", "rota_url", "rack_url", "elevacao_url",
        "impacto_url", "inventario_url", "exportar_url", "mapa_url",
        "csrf_token", "prtg_ok", "centro_padrao", "limite_utp", "folga_utp",
    }

    def _abrir(self):
        return self.client.get(reverse("rede_planta"))

    def test_planta_abre(self):
        self.assertEqual(self._abrir().status_code, 200)

    def test_config_tem_exatamente_as_chaves_que_o_js_le(self):
        self.assertEqual(set(self._abrir().context["planta_config"]), self.CHAVES)

    def test_js_le_todas_as_chaves_da_config(self):
        js = (_JS / "rede-planta.js").read_text(encoding="utf-8")
        lidas = set(re.findall(r"\bCFG\.(\w+)", js))
        self.assertEqual(
            lidas, self.CHAVES,
            f"só no JS: {lidas - self.CHAVES} | só na view: {self.CHAVES - lidas}",
        )

    def test_urls_resolvidas_e_nao_literais(self):
        cfg = self._abrir().context["planta_config"]
        for chave in (c for c in self.CHAVES if c.endswith("_url")):
            self.assertTrue(cfg[chave].startswith("/"), f"{chave} = {cfg[chave]!r}")

    def test_limite_da_norma_vem_do_servidor(self):
        """O JS mostra o alerta dos 90 m; o número é o do service, para a tela
        e a conferência não discordarem."""
        cfg = self._abrir().context["planta_config"]
        self.assertEqual(cfg["limite_utp"], rede.LIMITE_UTP_M)
        self.assertEqual(cfg["folga_utp"], rede.FOLGA_UTP_M)

    def test_fatias_existem_e_chegam_ao_html(self):
        html = self._abrir().content.decode("utf-8")
        ancoras = {
            "planta_topbar": 'id="localidade"',
            "planta_trilho": 'id="listaRotas"',
            "planta_mapa": 'id="mapaGeo"',
            "planta_inspetor": 'id="inspRota"',
            "planta_status": 'id="lerMetros"',
            "planta_modais": 'id="modalRota"',
        }
        for fatia, ancora in ancoras.items():
            caminho = _TEMPLATES / "rede" / f"_nx_{fatia}.html"
            self.assertTrue(caminho.is_file(), f"_nx_{fatia}.html não existe")
            self.assertIn(ancora, html, f"a fatia {fatia} não chegou ao HTML")
        self.assertNotIn("_nx_", html, "sobrou nome de include no HTML")

    def test_leaflet_e_o_kqmapa_entram_antes_do_nosso_js(self):
        """
        `KQMapa` é a fonte única das camadas de tile (regra 15) e depende do
        Leaflet já carregado. Fora de ordem, a tela abre sem fundo de mapa.
        """
        html = self._abrir().content.decode("utf-8")
        pos_leaflet = html.find("leaflet.js")
        pos_kq = html.find("KQMapa")
        pos_nosso = html.find("rede-planta.js")
        self.assertGreater(pos_leaflet, 0, "o Leaflet não entrou na página")
        self.assertGreater(pos_kq, pos_leaflet, "KQMapa antes do Leaflet")
        self.assertGreater(pos_nosso, pos_kq, "nosso JS antes do KQMapa")

    def test_json_script_vem_antes_do_script(self):
        html = self._abrir().content.decode("utf-8")
        self.assertLess(html.find('id="__planta_config"'), html.find("rede-planta.js"))

    def test_estaticos_entram_com_cache_busting(self):
        html = self._abrir().content.decode("utf-8")
        self.assertRegex(html, r"ds-rede\.css\?v=")
        self.assertRegex(html, r"rede-planta\.js\?v=")

    def test_ids_lidos_pelo_js_existem_no_html(self):
        html = self._abrir().content.decode("utf-8")
        js = (_JS / "rede-planta.js").read_text(encoding="utf-8")
        lista = re.search(r"function cachearElementos\(\) \{\s*\[(.*?)\]\.forEach", js, re.S)
        self.assertIsNotNone(lista, "a lista de ids do JS mudou de forma")
        ids = re.findall(r"'([A-Za-z0-9_]+)'", lista.group(1))
        self.assertGreater(len(ids), 40)
        faltando = [i for i in ids if f'id="{i}"' not in html]
        self.assertFalse(faltando, f"o JS procura ids que o HTML não tem: {faltando}")

    def test_botoes_procurados_pelo_js_existem(self):
        """`$('btnX').onclick` em id inexistente estoura na carga e mata o
        resto das ligações — a tela abre sem nenhum botão funcionando."""
        html = self._abrir().content.decode("utf-8")
        js = (_JS / "rede-planta.js").read_text(encoding="utf-8")
        ids = set(re.findall(r"\$\('([A-Za-z0-9_]+)'\)", js))
        faltando = [i for i in ids if f'id="{i}"' not in html]
        self.assertFalse(faltando, f"o JS liga ids que o HTML não tem: {faltando}")

    def test_template_da_planta_nao_tem_css_inline(self):
        html = self._abrir().content.decode("utf-8")
        inicio = html.find('class="nx-app"')
        self.assertNotIn("<style", html[inicio:], "CSS inline na planta")


class ApisDaPlantaTests(BaseRede):
    def test_toda_rota_exige_login(self):
        self.client.logout()
        for nome in ("rede_planta", "rede_planta_api", "rede_posicao_api",
                     "rede_rota_api", "rede_rack_api", "rede_rack_elevacao_api",
                     "rede_impacto_rota_api", "rede_planta_export"):
            resp = self.client.get(reverse(nome))
            self.assertIn(resp.status_code, (302, 301, 403),
                          f"{nome} respondeu {resp.status_code} sem login")

    def test_planta_api_responde_json_com_kpis_e_avisos(self):
        dados = self.client.get(reverse("rede_planta_api")).json()
        self.assertTrue(dados["ok"])
        for chave in ("racks", "equipamentos", "rotas", "centro", "kpis", "avisos"):
            self.assertIn(chave, dados)

    def test_posicionar_cria_a_faceta_sob_demanda(self):
        """Arrastar um switch do inventário para o mapa é o gesto que o põe na
        planta — exigir cadastro prévio faria o gesto falhar."""
        RedeDispositivo.objects.filter(item=self.ap2).delete()
        resp = self.client.post(
            reverse("rede_posicao_api"),
            data=json.dumps({"item": self.ap2.pk, "latitude": -14.5, "longitude": -45.5}),
            content_type="application/json",
        )
        self.assertTrue(resp.json()["ok"])
        self.assertTrue(RedeDispositivo.objects.filter(item=self.ap2).exists())

    def test_criar_rota_pela_api_mede_o_comprimento(self):
        resp = self.client.post(
            reverse("rede_rota_api"),
            data=json.dumps({
                "acao": "salvar", "nome": "Vala nova",
                "tipo": RedeRotaTipoChoices.UTP_CAT6,
                "pontos": [[-14.7763, -45.5634], [-14.77720, -45.5634]],
            }),
            content_type="application/json",
        )
        dados = resp.json()
        self.assertTrue(dados["ok"])
        self.assertTrue(dados["criada"])
        self.assertAlmostEqual(dados["rota"]["comprimento_m"], 100, delta=1)
        self.assertTrue(dados["rota"]["cobre"])

    def test_rota_sem_nome_recusa(self):
        resp = self.client.post(
            reverse("rede_rota_api"),
            data=json.dumps({"acao": "salvar", "pontos": [[0, 0], [1, 1]]}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()["ok"])

    def test_excluir_rota_nao_desfaz_cabo(self):
        """
        Mesmo contrato do mapa salvo: desenho não manda no inventário. O
        enlace continua, só perde o traçado físico.
        """
        rede.gerar_portas(self.disp, "cisco_c1000_8p", usuario=self.user)
        rede.gerar_portas(self.disp2, "cisco_c1000_8p", usuario=self.user)
        a = self.disp.portas.order_by("numero").first()
        b = self.disp2.portas.order_by("numero").first()
        rede.conectar(a, b, usuario=self.user)
        rota = rede.salvar_rota(RedeRota(), {"nome": "V", "pontos": [[0, 0], [0.001, 0]]},
                                usuario=self.user)
        rede.vincular_rota(a, rota, usuario=self.user)

        resp = self.client.post(
            reverse("rede_rota_api"),
            data=json.dumps({"acao": "excluir", "rota": rota.pk}),
            content_type="application/json",
        )
        self.assertTrue(resp.json()["ok"])
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertIsNone(a.rota_id)
        self.assertEqual(a.destino_porta_id, b.pk, "o cabo não podia sumir")
        self.assertEqual(b.destino_porta_id, a.pk)

    def test_vincular_rota_pela_api_da_porta(self):
        rede.gerar_portas(self.disp, "cisco_c1000_8p", usuario=self.user)
        porta = self.disp.portas.order_by("numero").first()
        rota = rede.salvar_rota(RedeRota(), {"nome": "V", "pontos": [[0, 0], [0.001, 0]]},
                                usuario=self.user)
        resp = self.client.post(
            reverse("rede_porta_api"),
            data=json.dumps({"porta": porta.pk, "rota": rota.pk}),
            content_type="application/json",
        )
        self.assertTrue(resp.json()["ok"])
        porta.refresh_from_db()
        self.assertEqual(porta.rota_id, rota.pk)

    def test_rota_inexistente_na_porta_responde_400(self):
        rede.gerar_portas(self.disp, "cisco_c1000_8p", usuario=self.user)
        porta = self.disp.portas.order_by("numero").first()
        resp = self.client.post(
            reverse("rede_porta_api"),
            data=json.dumps({"porta": porta.pk, "rota": 999999}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_criar_e_excluir_rack_pela_api(self):
        resp = self.client.post(
            reverse("rede_rack_api"),
            data=json.dumps({
                "acao": "salvar", "nome": "Rack API", "us": 24,
                "latitude": -14.7, "longitude": -45.5,
            }),
            content_type="application/json",
        )
        dados = resp.json()
        self.assertTrue(dados["criado"])
        rack_id = dados["rack"]["id"]

        rede.salvar_posicao(self.disp, {"rack": rack_id, "rack_u": 1}, self.user)
        resp = self.client.post(
            reverse("rede_rack_api"),
            data=json.dumps({"acao": "excluir", "rack": rack_id}),
            content_type="application/json",
        )
        self.assertTrue(resp.json()["ok"])
        self.disp.refresh_from_db()
        self.assertIsNone(self.disp.rack_id)
        self.assertTrue(Item.objects.filter(pk=self.switch.pk).exists(),
                        "excluir o armário não pode apagar equipamento")

    def test_rack_sem_nome_recusa(self):
        resp = self.client.post(
            reverse("rede_rack_api"),
            data=json.dumps({"acao": "salvar", "us": 42}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_elevacao_pela_api(self):
        rack = rede.salvar_rack(RedeRack(), {"nome": "R", "us": 6}, usuario=self.user)
        rede.salvar_posicao(self.disp, {"rack": rack.pk, "rack_u": 2}, self.user)
        dados = self.client.get(
            f"{reverse('rede_rack_elevacao_api')}?rack={rack.pk}"
        ).json()
        self.assertTrue(dados["ok"])
        self.assertEqual(len(dados["regua"]), 6)
        self.assertEqual(dados["rack"]["us_ocupados"], 1)

    def test_metodo_errado_responde_405(self):
        for nome in ("rede_posicao_api",):
            resp = self.client.get(reverse(nome))
            self.assertEqual(resp.status_code, 405)

    def test_ficha_da_porta_entrega_as_rotas_e_o_rack(self):
        """Os dois seletores do inspetor vêm na MESMA resposta: dois pedidos a
        mais por clique de nó, num mapa de 90 switches, é latência que
        aparece."""
        rede.salvar_rack(RedeRack(), {"nome": "R"}, usuario=self.user)
        rede.salvar_rota(RedeRota(), {"nome": "V", "pontos": [[0, 0], [1, 0]]},
                         usuario=self.user)
        dados = self.client.get(
            f"{reverse('rede_dispositivo_api')}?item={self.switch.pk}"
        ).json()
        self.assertTrue(dados["ok"])
        self.assertEqual(len(dados["racks"]), 1)
        self.assertEqual(len(dados["rotas"]), 1)

    def test_gravar_posicao_pela_ficha_do_dispositivo(self):
        rack = rede.salvar_rack(RedeRack(), {"nome": "R", "us": 12}, usuario=self.user)
        resp = self.client.post(
            reverse("rede_dispositivo_api"),
            data=json.dumps({
                "item": self.switch.pk, "papel": RedePapelChoices.ACESSO,
                "rack": rack.pk, "rack_u": 7, "rack_altura_u": 2,
                "latitude": "-14.77", "longitude": "-45.56",
            }),
            content_type="application/json",
        )
        self.assertTrue(resp.json()["ok"])
        self.disp.refresh_from_db()
        self.assertEqual(self.disp.rack_id, rack.pk)
        self.assertEqual(self.disp.rack_u, 7)
        self.assertEqual(self.disp.rack_altura_u, 2)


class ExportacaoDaPlantaTests(BaseRede):
    def test_excel_sai_com_as_seis_abas(self):
        from io import BytesIO
        from openpyxl import load_workbook

        rede.salvar_rack(RedeRack(), {"nome": "R", "us": 12, "latitude": -14.7,
                                      "longitude": -45.5}, usuario=self.user)
        rede.salvar_rota(RedeRota(), {"nome": "V", "pontos": [[0, 0], [0.001, 0]]},
                         usuario=self.user)
        resp = self.client.get(reverse("rede_planta_export"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("spreadsheet", resp["Content-Type"])
        wb = load_workbook(BytesIO(resp.content))
        self.assertEqual(
            wb.sheetnames,
            ["Resumo", "Racks", "Elevação", "Rotas", "Lances", "Conferência"],
        )

    def test_aba_de_lances_traz_o_cabo_com_a_rota(self):
        """É o papel que vai para o campo: cada enlace com a vala por onde
        passa e o comprimento dela."""
        from io import BytesIO
        from openpyxl import load_workbook

        rede.gerar_portas(self.disp, "cisco_c1000_8p", usuario=self.user)
        rede.gerar_portas(self.disp2, "cisco_c1000_8p", usuario=self.user)
        a = self.disp.portas.order_by("numero").first()
        b = self.disp2.portas.order_by("numero").first()
        rede.conectar(a, b, usuario=self.user)
        rota = rede.salvar_rota(RedeRota(), {
            "nome": "Vala do campo", "pontos": [[0, 0], [0.001, 0]],
        }, usuario=self.user)
        rede.vincular_rota(a, rota, usuario=self.user)

        resp = self.client.get(reverse("rede_planta_export"))
        wb = load_workbook(BytesIO(resp.content))
        ws = wb["Lances"]
        linhas = [
            [c.value for c in linha]
            for linha in ws.iter_rows(min_row=5, max_row=ws.max_row)
        ]
        self.assertTrue(
            any(linha[0] == "Vala do campo" for linha in linhas if linha[0]),
            "o lance não saiu na aba",
        )

    def test_cabo_sai_uma_vez_so_na_aba_de_lances(self):
        """As duas pernas apontam uma para a outra; sem desempate o papel do
        campo listaria o mesmo cabo duas vezes."""
        from io import BytesIO
        from openpyxl import load_workbook

        rede.gerar_portas(self.disp, "cisco_c1000_8p", usuario=self.user)
        rede.gerar_portas(self.disp2, "cisco_c1000_8p", usuario=self.user)
        a = self.disp.portas.order_by("numero").first()
        b = self.disp2.portas.order_by("numero").first()
        rede.conectar(a, b, usuario=self.user)
        rota = rede.salvar_rota(RedeRota(), {"nome": "V", "pontos": [[0, 0], [0.001, 0]]},
                                usuario=self.user)
        rede.vincular_rota(a, rota, usuario=self.user)

        resp = self.client.get(reverse("rede_planta_export"))
        wb = load_workbook(BytesIO(resp.content))
        ws = wb["Lances"]
        nomes = [
            ws.cell(row=r, column=1).value
            for r in range(5, ws.max_row + 1)
            if ws.cell(row=r, column=1).value
        ]
        self.assertEqual(nomes.count("V"), 1)


class JsDaPlantaTests(TestCase):
    ARQUIVO = _JS / "rede-planta.js"

    def test_existe_e_nao_tem_tag_de_template(self):
        self.assertTrue(self.ARQUIVO.is_file())
        texto = self.ARQUIVO.read_text(encoding="utf-8")
        self.assertNotRegex(texto, r"\{%|\{\{")

    def test_texto_do_banco_nunca_entra_por_innerhtml(self):
        """
        Nome de rack e de rota é digitado por gente. O `divIcon` do Leaflet
        aceita um Element em `html`, então o marcador é montado no DOM — e
        não por string, que seria XSS na tela de quem abrir a planta.
        """
        texto = self.ARQUIVO.read_text(encoding="utf-8")
        self.assertNotIn(".innerHTML", texto)

    def _codigo(self):
        """
        O arquivo sem comentários. O cabeçalho do JS CITA os provedores que
        já quebraram (é o que explica por que a lista mora no KQMapa), e a
        prosa não pode fazer o teste do código falhar — mesma separação que
        `_css_sem_print` faz na folha.
        """
        texto = self.ARQUIVO.read_text(encoding="utf-8")
        texto = re.sub(r"/\*.*?\*/", "", texto, flags=re.S)
        return re.sub(r"(?m)^\s*//.*$", "", texto)

    def test_nao_escolhe_provedor_de_tile_por_conta_propria(self):
        """
        Regra 15: a lista de camadas é única e mora no KQMapa, que tem a
        sonda de placeholder. Uma URL de tile escrita aqui seria a terceira
        vez que este projeto cai num provedor mudo.
        """
        codigo = self._codigo()
        self.assertIn("KQMapa.montarCamadas", codigo)
        self.assertNotIn("tile.openstreetmap", codigo)
        self.assertNotIn("cartocdn", codigo)
        self.assertNotIn("arcgisonline", codigo)
        self.assertNotRegex(codigo, r"\{z\}/\{[xy]\}/\{[xy]\}")

    def test_o_limite_da_norma_nao_e_numero_magico_no_js(self):
        """O JS mostra o alerta; o número é o do service, entregue na config.
        Dois lugares com o mesmo limite divergem na primeira correção."""
        texto = self.ARQUIVO.read_text(encoding="utf-8")
        self.assertIn("CFG.limite_utp", texto)

    def test_comprimento_gravado_e_do_servidor(self):
        """
        O número na tela enquanto se desenha é preview. Mandar `comprimento_m`
        no corpo faria o cliente afirmar uma medida que o traçado não tem.
        """
        self.assertNotRegex(self._codigo(), r"comprimento_m\s*:")
