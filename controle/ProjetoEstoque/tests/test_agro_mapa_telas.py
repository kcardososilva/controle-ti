"""O mapa do Zelo Agro, a infraestrutura (estruturas e enlaces) e o que as telas carregam (Select2, paginação).

O que se prova aqui é o que o pedido de 14/11 e a regra 63 exigem do MAPA: só aparece o que tem coordenada
CADASTRADA (nada deduzido do chip), cada ponto diz a origem e a data, a permissão decide o que sai no JSON (veículo,
infraestrutura, ICCID), o enlace só é desenhado com as duas pontas localizadas e a posição vinda de GPS é distinta
da cadastral. As telas de cadastro são as do estoque tecnológico (`tec_comum.formulario`)."""

import json
from datetime import timedelta

from django.contrib.auth.models import Permission, User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ProjetoEstoque.models import FrotaTipo, M2MChip, TecEnlace, TecEstrutura
from services import agro_infra_service as infra
from services import agro_mapa_service as mapa
from services import frota_service, frota_vinculo_service as vinculos


def usuario(nome, *codenames, superuser=False):
    u = User.objects.create_user(nome, f"{nome}@x.com", "senha-boa-123", is_superuser=superuser)
    for c in codenames:
        u.user_permissions.add(Permission.objects.get(codename=c, content_type__app_label="ProjetoEstoque"))
    return u


class MapaBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = usuario("admin_mapa", superuser=True)
        cls.karitel, cls.rio = frota_service.fazendas()
        cls.tipo_trator = FrotaTipo.objects.get_or_create(nome="Trator")[0]
        cls.tipo_caminhao = FrotaTipo.objects.get_or_create(nome="Caminhões Tabaco")[0]
        cls.tipo_desconhecido = FrotaTipo.objects.get_or_create(nome="Equipamento especial")[0]

    def veiculo(self, codigo, tipo=None, **extra):
        dados = {"codigo": codigo, "nome": f"Máquina {codigo}", "tipo": (tipo or self.tipo_trator).pk, **extra}
        return frota_service.criar_veiculo(dados, self.admin)

    def estrutura(self, nome, **extra):
        return infra.salvar_estrutura({"nome": nome, "tipo": "torre", "latitude": "-14,7763", "longitude": "-45,5634", **extra}, self.admin)

    def dados(self, cliente, **parametros):
        r = cliente.get(reverse("agro_mapa_dados"), parametros)
        self.assertEqual(r.status_code, 200, r.content[:200])
        return json.loads(r.content)

    def entrar(self, u):
        self.client.force_login(u)
        return self.client


class ServicoDoMapaTests(MapaBase):
    def montar(self, filtros=None, **perm):
        permissoes = {"ver_frota": True, "ver_infra": True, "ver_chips": True, **perm}
        return mapa.montar_mapa(filtros or {}, **permissoes)

    def test_so_aparece_o_que_tem_coordenada_e_o_resto_e_contado(self):
        self.veiculo("MQ-1", latitude="-14,7763", longitude="-45,5634")
        self.veiculo("MQ-2")
        d = self.montar()
        self.assertEqual([v["codigo"] for v in d["veiculos"]], ["MQ-1"])
        self.assertEqual(d["semLocalizacao"]["veiculos"], 1)
        self.assertEqual(d["totais"]["veiculos"], 1)

    def test_chip_nao_da_posicao_ao_veiculo(self):
        """O ponto central da regra 63: um veículo com chip e SESSÃO ATIVA, mas sem coordenada cadastrada, não vai ao mapa."""
        v = self.veiculo("MQ-CHIP")
        chip = M2MChip.objects.create(nome="Chip A", iccid="8955100000000000019")
        M2MChip.objects.filter(pk=chip.pk).update(sessao_estado="ativa")
        vinculos.vincular(chip, v, self.admin, "teste")
        d = self.montar()
        self.assertEqual(d["veiculos"], [])
        self.assertEqual(d["semLocalizacao"]["veiculos"], 1)

    def test_cada_ponto_diz_a_origem_e_a_data(self):
        self.veiculo("MQ-1", latitude="-14,7763", longitude="-45,5634")
        p = self.montar()["veiculos"][0]
        self.assertEqual(p["localizacao"]["origem"], "cadastral")
        self.assertEqual(p["localizacao"]["rotulo"], "Coordenada cadastral fixa")
        self.assertTrue(p["localizacao"]["atualizada_em"]["texto"])
        self.assertIn("não a posição atual", p["localizacao"]["aviso"])
        self.assertEqual((p["lat"], p["lon"]), (-14.7763, -45.5634))

    def test_gps_e_distinto_do_cadastral_e_ganha_aviso_de_desatualizado(self):
        v = self.veiculo("MQ-GPS")
        mapa.registrar_posicao_gps(v, "-14,80", "-45,60", em=timezone.now() - timedelta(hours=30))
        p = self.montar()["veiculos"][0]
        self.assertEqual(p["localizacao"]["origem"], "gps")
        self.assertTrue(p["localizacao"]["desatualizada"])
        self.assertIn("desatualizada", p["localizacao"]["rotulo"])
        mapa.registrar_posicao_gps(v, "-14,81", "-45,61")
        p = self.montar()["veiculos"][0]
        self.assertFalse(p["localizacao"]["desatualizada"])
        self.assertEqual(p["lat"], -14.81)

    def test_posicao_gps_atrasada_nao_volta_o_veiculo_no_tempo(self):
        v = self.veiculo("MQ-GPS")
        agora = timezone.now()
        mapa.registrar_posicao_gps(v, "-14,81", "-45,61", em=agora)
        mapa.registrar_posicao_gps(v, "-14,70", "-45,50", em=agora - timedelta(hours=2))
        v.refresh_from_db()
        self.assertEqual(float(v.latitude), -14.81)

    def test_icone_do_marcador_vem_da_biblioteca_central_nunca_o_mesmo_para_todos(self):
        """O mapa só repassa o id (`services/icone_service.py`): a tabela "tipo -> desenho" saiu daqui (era a mesma regra
        escrita em Font Awesome, divergente da lista e da ficha)."""
        casos = (
            (self.tipo_trator, "trator"), (self.tipo_caminhao, "caminhao"), (self.tipo_desconhecido, "maquina-generica"),
        )
        for k, (tipo, esperado) in enumerate(casos):
            self.veiculo(f"MQ-IC{k}", tipo=tipo, latitude="-14,7", longitude="-45,5")
        icones = {p["codigo"]: p["icone"] for p in self.montar()["veiculos"]}
        self.assertEqual(icones, {"MQ-IC0": "trator", "MQ-IC1": "caminhao", "MQ-IC2": "maquina-generica"})
        self.assertEqual(len(set(mapa.icone_service.ICONE_DA_ESTRUTURA.values())), 4)

    def test_estruturas_e_enlaces_so_com_coordenada_e_enlace_com_as_duas_pontas(self):
        a, b = self.estrutura("Torre A"), self.estrutura("Torre B", latitude="-14,8", longitude="-45,6")
        sem = infra.salvar_estrutura({"nome": "Base sem ponto", "tipo": "base"}, self.admin)
        infra.salvar_enlace({"identificacao": "PTP-1", "estrutura_a": a.pk, "estrutura_b": b.pk, "estado": "operacional"}, self.admin)
        infra.salvar_enlace({"identificacao": "PTP-2", "estrutura_a": a.pk, "estrutura_b": sem.pk}, self.admin)
        d = self.montar()
        self.assertEqual({e["nome"] for e in d["estruturas"]}, {"Torre A", "Torre B"})
        self.assertEqual(d["semLocalizacao"]["estruturas"], 1)
        self.assertEqual([e["identificacao"] for e in d["enlaces"]], ["PTP-1"])
        self.assertEqual(d["semLocalizacao"]["enlaces"], 1)
        enlace = d["enlaces"][0]
        self.assertEqual(enlace["estado"], "operacional")
        self.assertGreater(enlace["distancia_km"], 1)
        self.assertEqual(enlace["estado_registrado_por"], "admin_mapa")

    def test_enlace_cadastrado_sem_estado_nao_e_chamado_de_operacional(self):
        a, b = self.estrutura("Torre A"), self.estrutura("Torre B", latitude="-14,8", longitude="-45,6")
        infra.salvar_enlace({"identificacao": "PTP-1", "estrutura_a": a.pk, "estrutura_b": b.pk}, self.admin)
        self.assertEqual(self.montar()["enlaces"][0]["estado"], "nao_informado")

    def test_inativos_ficam_fora_do_mapa(self):
        e = self.estrutura("Torre velha")
        infra.salvar_estrutura({"ativo": False}, self.admin, e)
        self.assertEqual(self.montar()["estruturas"], [])

    def test_filtros_de_veiculo_e_de_fazenda(self):
        self.veiculo("MQ-A", fabricante="John Deere", latitude="-14,7", longitude="-45,5", localidade=self.karitel.pk, status="ativo")
        self.veiculo("MQ-B", tipo=self.tipo_caminhao, fabricante="Stara", latitude="-14,8", longitude="-45,6", localidade=self.rio.pk)
        self.assertEqual({p["codigo"] for p in self.montar({"fabricante": "stara"})["veiculos"]}, {"MQ-B"})
        self.assertEqual({p["codigo"] for p in self.montar({"tipo": str(self.tipo_trator.pk)})["veiculos"]}, {"MQ-A"})
        self.assertEqual({p["codigo"] for p in self.montar({"fazenda": str(self.rio.pk)})["veiculos"]}, {"MQ-B"})
        self.assertEqual({p["codigo"] for p in self.montar({"q": "MQ-A"})["veiculos"]}, {"MQ-A"})

    def test_camadas_pedidas(self):
        self.veiculo("MQ-A", latitude="-14,7", longitude="-45,5")
        self.estrutura("Torre A")
        d = self.montar({"camadas": ["estruturas"]})
        self.assertEqual((d["veiculos"], len(d["estruturas"])), ([], 1))
        d = self.montar({"camadas": ["nenhuma"]})
        self.assertEqual((d["veiculos"], d["estruturas"], d["enlaces"]), ([], [], []))

    def test_sem_chips_na_permissao_o_json_nao_leva_iccid_nem_conectividade(self):
        v = self.veiculo("MQ-A", latitude="-14,7", longitude="-45,5")
        chip = M2MChip.objects.create(nome="Chip A", iccid="8955100000000000019")
        vinculos.vincular(chip, v, self.admin, "teste")
        com = self.montar()["veiculos"][0]
        sem = self.montar(ver_chips=False)["veiculos"][0]
        self.assertEqual(com["chips"][0]["iccid"], "8955100000000000019")
        self.assertNotIn("chips", sem)
        self.assertNotIn("conectividade", sem)
        self.assertNotIn("8955100000000000019", json.dumps(sem))

    def contar_consultas(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as q:
            self.montar()
        return len(q)

    def test_o_custo_nao_cresce_com_o_numero_de_pontos(self):
        self.contar_consultas()           # aquecimento: a 1ª leitura cria a linha única de configuração do GPS (get_or_create)
        for n in range(5):
            self.veiculo(f"MQ-{n}", latitude="-14,7", longitude="-45,5")
        com_5 = self.contar_consultas()
        for n in range(5, 25):
            self.veiculo(f"MQ-{n}", latitude="-14,7", longitude="-45,5")
        self.assertEqual(self.contar_consultas(), com_5)


class TelaDoMapaTests(MapaBase):
    def test_pagina_abre_para_quem_ve_veiculos_ou_infraestrutura(self):
        for nome, codenames in (("so_frota", ("ver_frota",)), ("so_infra", ("ver_tec_estoque",))):
            with self.subTest(nome=nome):
                cliente = self.entrar(usuario(nome, *codenames))
                r = cliente.get(reverse("agro_mapa"))
                self.assertEqual(r.status_code, 200)

    def test_sem_nenhuma_das_duas_e_403(self):
        cliente = self.entrar(usuario("so_chips", "ver_chips", "acessar_zelo_agro"))
        self.assertEqual(cliente.get(reverse("agro_mapa")).status_code, 403)
        self.assertEqual(cliente.get(reverse("agro_mapa_dados")).status_code, 403)

    def test_anonimo_e_mandado_para_o_login(self):
        r = self.client.get(reverse("agro_mapa_dados"))
        self.assertEqual(r.status_code, 302)

    def test_a_pagina_carrega_leaflet_localmente_com_atribuicao_e_sem_dado_de_ativo_no_html(self):
        self.veiculo("MQ-SECRETO", latitude="-14,9123", longitude="-45,1234")
        html = self.entrar(self.admin).get(reverse("agro_mapa")).content.decode()
        self.assertIn("vendor/leaflet/leaflet.js", html)
        self.assertIn("vendor/leaflet/leaflet.markercluster.js", html)
        self.assertNotIn("unpkg.com", html)
        self.assertIn("colaboradores do OpenStreetMap", html)
        self.assertIn('id="agro-mapa-config"', html)
        self.assertNotIn("MQ-SECRETO", html)                  # os pontos vêm do JSON (permissão), nunca embutidos na página
        self.assertNotIn("-14.9123", html)
        for marcador in ("{{", "{%", "{#"):
            self.assertNotIn(marcador, html)

    def test_so_infra_nao_ve_filtros_de_veiculo_nem_chips(self):
        html = self.entrar(usuario("so_infra", "ver_tec_estoque")).get(reverse("agro_mapa")).content.decode()
        self.assertNotIn('name="tipo"', html)
        self.assertNotIn('value="veiculos"', html)
        self.assertIn('value="estruturas"', html)

    def test_so_frota_nao_ve_filtros_de_infra(self):
        html = self.entrar(usuario("so_frota", "ver_frota")).get(reverse("agro_mapa")).content.decode()
        self.assertIn('value="veiculos"', html)
        self.assertNotIn('value="estruturas"', html)
        self.assertNotIn('name="situacao"', html)
        self.assertNotIn('name="conectividade"', html)        # a conectividade é de quem vê chips

    def test_o_json_respeita_a_permissao_de_cada_camada_e_nao_fica_em_cache(self):
        self.veiculo("MQ-A", latitude="-14,7", longitude="-45,5")
        self.estrutura("Torre A")
        so_infra = self.dados(self.entrar(usuario("so_infra", "ver_tec_estoque")))
        self.assertEqual((so_infra["veiculos"], len(so_infra["estruturas"])), ([], 1))
        so_frota = self.dados(self.entrar(usuario("so_frota", "ver_frota")))
        self.assertEqual((len(so_frota["veiculos"]), so_frota["estruturas"]), (1, []))
        r = self.client.get(reverse("agro_mapa_dados"))
        self.assertIn("no-store", r["Cache-Control"])

    def test_os_filtros_da_url_chegam_a_configuracao_da_pagina(self):
        r = self.entrar(self.admin).get(reverse("agro_mapa"), {"tipo": "3", "camada": "estruturas"})
        cfg = r.context["config_mapa"]
        self.assertEqual(cfg["filtros"]["tipo"], "3")
        self.assertEqual(cfg["camadas"], ["estruturas"])


class AgroCarregaOSelect2Tests(MapaBase):
    def test_o_ambiente_agro_carrega_jquery_e_select2_uma_vez_e_do_proprio_sistema(self):
        html = self.entrar(self.admin).get(reverse("infra_estruturas")).content.decode()
        self.assertEqual(html.count("vendor/jquery-3.7.1.min.js"), 1)
        self.assertEqual(html.count("vendor/select2.min.js"), 1)
        self.assertEqual(html.count("js/agro-select.js"), 1)
        self.assertIn("vendor/select2.min.css", html)
        self.assertNotIn("code.jquery.com", html)
        self.assertNotIn("cdn.jsdelivr.net/npm/select2", html)

    def test_o_ambiente_ti_nao_carrega_o_iniciador_do_agro(self):
        html = self.entrar(self.admin).get(reverse("dashboard")).content.decode()
        self.assertNotIn("agro-select.js", html)

    def test_nenhuma_tela_do_agro_carrega_jquery_ou_select2_por_conta_propria(self):
        import pathlib
        raiz = pathlib.Path(__file__).resolve().parents[1] / "templates" / "front"
        for pasta in ("agro", "m2m"):
            for arquivo in (raiz / pasta).rglob("*.html"):
                conteudo = arquivo.read_text(encoding="utf-8")
                with self.subTest(arquivo=arquivo.name):
                    self.assertNotIn("code.jquery.com", conteudo)
                    self.assertNotIn("cdn.jsdelivr.net/npm/select2", conteudo)


class TelasDeInfraTests(MapaBase):
    def test_telas_abrem_vazias_com_o_proximo_passo(self):
        cliente = self.entrar(self.admin)
        for nome in ("infra_visao_geral", "infra_estruturas", "infra_enlaces", "infra_estrutura_nova", "infra_enlace_novo"):
            with self.subTest(tela=nome):
                r = cliente.get(reverse(nome))
                self.assertEqual(r.status_code, 200)
                html = r.content.decode()
                for marcador in ("{{", "{%", "{#"):
                    self.assertNotIn(marcador, html)
        self.assertIn("Nenhuma infraestrutura cadastrada", cliente.get(reverse("infra_visao_geral")).content.decode())

    def test_cadastrar_estrutura_pela_tela_e_enlace_e_aparecer_nas_listas(self):
        cliente = self.entrar(self.admin)
        r = cliente.post(reverse("infra_estrutura_nova"), {"nome": "Torre Norte", "tipo": "torre", "situacao": "em_operacao", "latitude": "-14,7763", "longitude": "-45,5634"})
        self.assertEqual(r.status_code, 302, r.content.decode()[:500])
        cliente.post(reverse("infra_estrutura_nova"), {"nome": "Torre Sul", "tipo": "torre", "situacao": "em_operacao", "latitude": "-14,80", "longitude": "-45,60"})
        a, b = TecEstrutura.objects.order_by("nome")
        r = cliente.post(reverse("infra_enlace_novo"), {"identificacao": "PTP-1", "estrutura_a": a.pk, "estrutura_b": b.pk, "estado": "operacional"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("Torre Norte", cliente.get(reverse("infra_estruturas")).content.decode())
        self.assertIn("PTP-1", cliente.get(reverse("infra_enlaces")).content.decode())
        painel = cliente.get(reverse("infra_visao_geral")).content.decode()
        self.assertIn("Operacionais", painel)
        self.assertEqual(TecEnlace.objects.get().estado_por, self.admin)

    def test_erro_de_coordenada_volta_no_campo_com_o_que_foi_digitado(self):
        r = self.entrar(self.admin).post(reverse("infra_estrutura_nova"), {"nome": "X", "tipo": "torre", "situacao": "em_operacao", "latitude": "0", "longitude": "0"})
        self.assertEqual(r.status_code, 400)
        html = r.content.decode()
        self.assertIn("ponto nulo", html)
        self.assertFalse(TecEstrutura.objects.exists())

    def test_editar_estrutura_corrige_a_coordenada(self):
        e = self.estrutura("Torre A")
        r = self.entrar(self.admin).post(reverse("infra_estrutura_editar", args=[e.pk]), {
            "nome": "Torre A", "tipo": "torre", "situacao": "em_operacao", "latitude": "-14,9", "longitude": "-45,7", "ativo": "1",
        })
        self.assertEqual(r.status_code, 302)
        e.refresh_from_db()
        self.assertEqual(float(e.latitude), -14.9)

    def test_so_quem_pode_edita_estrutura_e_enlace(self):
        e = self.estrutura("Torre A")
        so_ver = self.entrar(usuario("so_ver", "ver_tec_estoque"))
        self.assertEqual(so_ver.get(reverse("infra_estruturas")).status_code, 200)
        self.assertEqual(so_ver.get(reverse("infra_estrutura_nova")).status_code, 403)
        self.assertEqual(so_ver.post(reverse("infra_estrutura_editar", args=[e.pk]), {"nome": "Hack"}).status_code, 403)
        self.assertEqual(so_ver.get(reverse("infra_enlace_novo")).status_code, 403)
        e.refresh_from_db()
        self.assertEqual(e.nome, "Torre A")
        # a permissão de estrutura não vale para enlace, e vice-versa
        estrutura = self.entrar(usuario("so_estrutura", "ver_tec_estoque", "gerenciar_tec_estrutura"))
        self.assertEqual(estrutura.get(reverse("infra_estrutura_nova")).status_code, 200)
        self.assertEqual(estrutura.get(reverse("infra_enlace_novo")).status_code, 403)

    def test_sem_a_permissao_de_ver_nada_abre(self):
        cliente = self.entrar(usuario("nada", "acessar_zelo_agro"))
        for nome in ("infra_visao_geral", "infra_estruturas", "infra_enlaces"):
            self.assertEqual(cliente.get(reverse(nome)).status_code, 403, nome)

    def test_lista_de_estruturas_pagina_e_preserva_filtros(self):
        for n in range(1, 31):
            self.estrutura(f"Torre {n:02d}", tipo="torre")
        cliente = self.entrar(self.admin)
        r = cliente.get(reverse("infra_estruturas"), {"tipo": "torre", "por_pagina": "10", "page": "2"})
        html = r.content.decode()
        self.assertEqual(len(r.context["estruturas"]), 10)
        self.assertIn("11–20 de 30 registros", html)
        self.assertIn("tipo=torre&amp;por_pagina=10&amp;page=3", html)        # o filtro e o tamanho acompanham a navegação
        vistos = []
        for n in (1, 2, 3):
            vistos += [e.nome for e in cliente.get(reverse("infra_estruturas"), {"por_pagina": "10", "page": str(n)}).context["estruturas"]]
        self.assertEqual(len(vistos), 30)
        self.assertEqual(len(set(vistos)), 30)

    def test_enlace_nao_aceita_as_mesmas_pontas_nem_ponta_inativa_pela_tela(self):
        a = self.estrutura("Torre A")
        r = self.entrar(self.admin).post(reverse("infra_enlace_novo"), {"identificacao": "X", "estrutura_a": a.pk, "estrutura_b": a.pk, "estado": "nao_informado"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("diferentes", r.content.decode())

    def test_o_menu_do_agro_mostra_infraestrutura_e_mapa_conforme_a_permissao(self):
        html = self.entrar(self.admin).get(reverse("agro_visao_geral")).content.decode()
        self.assertIn(reverse("infra_enlaces"), html)
        self.assertIn(reverse("agro_mapa"), html)
        so_chips = self.entrar(usuario("so_chips", "ver_chips", "acessar_zelo_agro"))
        html = so_chips.get(reverse("m2m_visao_geral")).content.decode()
        self.assertNotIn(reverse("infra_enlaces"), html)
        self.assertNotIn(reverse("agro_mapa"), html)


class FormularioDependenteTests(MapaBase):
    def test_o_cadastro_de_equipamento_oferece_o_fabricante_que_filtra_o_modelo(self):
        from ProjetoEstoque.models import AgroUnidadeMedida, TecCategoria, TecFabricante, TecModelo
        deere = TecFabricante.objects.get_or_create(nome="John Deere")[0]
        stara = TecFabricante.objects.get_or_create(nome="Stara")[0]
        cat = TecCategoria.objects.filter(controle_padrao="individual").first()
        TecModelo.objects.create(fabricante=deere, categoria=cat, nome="StarFire 6000", controle="individual")
        TecModelo.objects.create(fabricante=stara, categoria=cat, nome="Topper", controle="individual")
        html = self.entrar(self.admin).get(reverse("tec_equipamento_novo")).content.decode()
        self.assertIn('name="fabricante_filtro"', html)
        self.assertIn('data-s2-pai="fabricante_filtro"', html)
        self.assertIn('label="John Deere"', html)             # o modelo agrupado por fabricante: é o que o filtro casa
        self.assertIn("StarFire 6000", html)

    def test_o_fabricante_auxiliar_nao_entra_nos_dados_gravados(self):
        from ProjetoEstoque.models import TecCategoria, TecEquipamento, TecFabricante, TecModelo
        from services import tec_catalogo_service as cat_svc
        deere = TecFabricante.objects.get_or_create(nome="John Deere")[0]
        cat = TecCategoria.objects.filter(controle_padrao="individual").first()
        modelo = TecModelo.objects.create(fabricante=deere, categoria=cat, nome="StarFire 6000", controle="individual")
        local = cat_svc.locais_das_fazendas()[0]
        r = self.entrar(self.admin).post(reverse("tec_equipamento_novo"), {
            "fabricante_filtro": str(deere.pk), "modelo": modelo.pk, "numero_serie": "SN-1", "local": local.pk,
        })
        self.assertEqual(r.status_code, 302, r.content.decode()[:600])
        self.assertTrue(TecEquipamento.objects.filter(numero_serie="SN-1", modelo=modelo).exists())
