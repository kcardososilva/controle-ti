"""A biblioteca de ícones do Zelo (`services/icone_service.py`, tags, sprite) e a aplicação dela nas telas.

O que se prova:
  • INTEGRIDADE — todo ícone do catálogo tem arquivo, todo arquivo está no catálogo, o sprite e o manifesto gravados
    batem com a pasta (quem troca um .svg e esquece de rodar `montar_icones_zelo` quebra aqui, não na tela);
  • TEMA — o traço é `currentColor`, sem nenhuma cor escrita no arquivo: o mesmo desenho serve no claro e no escuro;
  • CLASSIFICAÇÃO CENTRAL — os nomes que o cadastro REAL tem ("Access-Point", "ANTENA BASTÃO", "CAMINHÃO PIPA/BOMBEIRO")
    caem no ícone certo; o que nenhuma regra reconhece cai no genérico do ambiente, nunca num desenho por palpite;
  • A TELA SÓ PERGUNTA — lista, ficha, Select2, mapa e dashboard recebem o id do mesmo serviço;
  • HONESTIDADE DA ORIGEM — os arquivos atuais são `interno`; arte do Canva entra por PNG e o manifesto diz `canva`."""

import re
import tempfile
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.template import Context, Template
from django.test import TestCase
from django.urls import reverse

from ProjetoEstoque.models import Categoria, FrotaTipo, Item, Subtipo
from services import frota_service
from services import icone_service as ic


class IntegridadeDaBibliotecaTests(TestCase):
    def test_todo_icone_do_catalogo_tem_arquivo_e_todo_arquivo_esta_no_catalogo(self):
        sem_arquivo = [i.id for i in ic.CATALOGO if not ic.formato_de(i.id)]
        self.assertEqual(sem_arquivo, [], "ícones do catálogo sem .svg")
        orfaos = sorted(p.stem for p in ic.PASTA_SVG.glob("*.svg") if not ic.existe(p.stem))
        self.assertEqual(orfaos, [], "arquivos .svg sem entrada no catálogo")

    def test_ids_do_catalogo_sao_unicos_e_o_sprite_tem_um_simbolo_por_icone(self):
        ids = [i.id for i in ic.CATALOGO]
        self.assertEqual(len(ids), len(set(ids)))
        sprite = ic.ARQUIVO_SPRITE.read_text(encoding="utf-8")
        for ident in ids:
            self.assertIn(f'id="{ic.simbolo(ident)}"', sprite, ident)

    def test_sprite_e_manifesto_estao_em_dia_com_a_pasta(self):
        self.assertTrue(
            ic.sprite_esta_em_dia(),
            "O sprite/manifesto estão fora de sincronia com static/icones/zelo/svg. Rode: python manage.py montar_icones_zelo",
        )

    def test_toda_regra_aponta_para_icone_que_existe_e_o_ambiente_e_valido(self):
        for padrao, ident, ambientes in ic.REGRAS:
            re.compile(padrao)
            self.assertTrue(ic.existe(ident), f"regra {padrao!r} aponta para {ident!r}, que não existe")
            self.assertTrue(set(ambientes) <= set(ic.AMBIENTES))

    def test_os_dois_genericos_existem(self):
        for ambiente in ic.AMBIENTES:
            self.assertTrue(ic.existe(ic.ICONE_PADRAO[ambiente]))

    def test_o_traco_e_currentcolor_e_nenhuma_cor_esta_escrita_no_arquivo(self):
        """Compatibilidade com tema claro e escuro: cor fixa dentro do SVG não acompanharia o tema."""
        for arquivo in ic.PASTA_SVG.glob("*.svg"):
            texto = arquivo.read_text(encoding="utf-8")
            self.assertIn('stroke="currentColor"', texto, arquivo.name)
            self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b|rgb\(|hsl\(", texto), f"{arquivo.name} tem cor fixa")
            for atributo in re.findall(r'(?:fill|stroke)="([^"]+)"', texto):
                self.assertIn(atributo, ("none", "currentColor"), arquivo.name)

    def test_grade_comum_viewbox_24(self):
        for arquivo in ic.PASTA_SVG.glob("*.svg"):
            self.assertIn('viewBox="0 0 24 24"', arquivo.read_text(encoding="utf-8"), arquivo.name)

    def test_manifesto_traz_o_que_a_administracao_da_biblioteca_pede(self):
        linha = next(l for l in ic.manifesto() if l["id"] == "trator")
        for campo in ("id", "categoria", "subtipo", "arquivo", "formato", "origem", "referencia"):
            self.assertIn(campo, linha)
        self.assertEqual((linha["formato"], linha["origem"], linha["arquivo"]), ("svg", "interno", "svg/trator.svg"))
        self.assertTrue(linha["referencia"]["regras"], "o trator tem de dizer por quais regras é aplicado")

    def test_arte_do_canva_entra_por_png_e_o_manifesto_a_declara(self):
        with tempfile.TemporaryDirectory() as pasta:
            (Path(pasta) / "motocicleta.png").write_bytes(b"\x89PNG")
            with mock.patch.object(ic, "PASTA_SVG", Path(pasta) / "vazio"), mock.patch.object(ic, "PASTA_PNG", Path(pasta)):
                ic.limpar_cache()
                try:
                    self.assertEqual(ic.formato_de("motocicleta"), "png")
                    linha = next(l for l in ic.manifesto() if l["id"] == "motocicleta")
                    self.assertEqual((linha["formato"], linha["origem"]), ("png", "canva"))
                    html = Template('{% load zelo_icones %}{% zelo_icone "motocicleta" %}').render(Context())
                    self.assertIn("<img", html)
                    self.assertIn("icones/zelo/png/motocicleta.png", html)
                finally:
                    ic.limpar_cache()


class ClassificacaoCentralTests(TestCase):
    def test_nomes_do_pedido_e_do_cadastro_real_no_zelo_ti(self):
        casos = {
            "Notebook": "notebook", "Desktop": "desktop", "Monitor": "monitor", "Tablet": "tablet", "Celular": "smartphone",
            "Switches": "switch", "ROTEADOR": "roteador", "Access-Point": "access-point", "Firewall": "firewall",
            "PATCH PANEL POE": "patch-panel", "Impressora": "impressora", "Nobreak": "nobreak", "IP-Phone": "telefone",
            "Câmera": "camera", "Teclado": "teclado", "Mouse": "mouse", "PROJETOR": "projetor", "CHIP": "chip-sim",
            "ANTENA BASTÃO": "antena-omni", "ANTENA OMNIDIRECIONAL": "antena-omni", "ANTENA PAINEL OUTDOR": "antena-direcional",
            "ANTENA ARS PE": "antena", "STARLINK": "parabolica", "Cabo UTP": "cabo", "Headset": "headset",
            # "FONTE AP OUTDOOR" é uma FONTE: casar "AP" antes de "fonte" a faria virar access point
            "FONTE AP OUTDOOR": "fonte-energia", "POWER INJECTOR": "fonte-energia", "ESTABILIZADOR": "nobreak",
            "Conversores de Mídia": "transceptor", "DVR": "seguranca",
        }
        for nome, esperado in casos.items():
            with self.subTest(nome=nome):
                self.assertEqual(ic.icone_id(nome, ambiente="ti"), esperado)

    def test_tipos_do_zelo_agro(self):
        casos = {
            "Trator": "trator", "Motocicleta": "motocicleta", "Caminhão": "caminhao", "Colheitadeira": "colheitadeira",
            "Pulverizador": "pulverizador", "Plantadeira": "plantadeira", "Distribuidor": "distribuidor", "Empilhadeira": "empilhadeira",
            "Gerador": "gerador", "Veículo utilitário": "veiculo-utilitario", "Implemento agrícola": "implemento",
            "Receptor GPS / GNSS": "receptor-gps", "Monitor / display": "monitor-agricola", "Controlador eletrônico": "controlador-eletronico",
            "Sensor": "sensor", "Rádio portátil (HT)": "radio-ht", "Rádio móvel": "radio-veicular", "Rádio base": "radio-base",
            "Repetidora": "repetidora", "Antena de radiocomunicação": "antena-radio", "Rádio Motorola HT": "radio-ht",
            "Rádio de enlace (PTP)": "enlace-ptp", "Componente RTK": "base-rtk", "Antena direcional": "antena-direcional",
            "Antena omnidirecional": "antena-omni", "Módulo embarcado": "tecnologia-embarcada",
            "Modem / módulo de comunicação": "modem-agricola", "Switch": "switch", "Nobreak": "nobreak",
        }
        for nome, esperado in casos.items():
            with self.subTest(nome=nome):
                self.assertEqual(ic.icone_id(nome, ambiente="agro"), esperado)

    def test_o_ambiente_desempata_o_mesmo_nome(self):
        self.assertEqual(ic.icone_id("Monitor", ambiente="ti"), "monitor")
        self.assertEqual(ic.icone_id("Monitor", ambiente="agro"), "monitor-agricola")
        self.assertEqual(ic.icone_id("Modem", ambiente="ti"), "modem")
        self.assertEqual(ic.icone_id("Modem", ambiente="agro"), "modem-agricola")

    def test_a_classe_do_veiculo_vence_o_tipo(self):
        """O tipo de "CAMINHÃO PIPA/BOMBEIRO" e de "MOTOR ESTACIONARIO" no cadastro é só "Caminhão" e "Gerador"."""
        self.assertEqual(ic.icone_do_veiculo("CAMINHÃO PIPA/BOMBEIRO", "Caminhão"), "caminhao-pipa")
        self.assertEqual(ic.icone_do_veiculo("CAMINHÃO COMBOIO", "Caminhão"), "caminhao-comboio")
        self.assertEqual(ic.icone_do_veiculo("MOTOR ESTACIONARIO", "Gerador"), "motor-estacionario")
        self.assertEqual(ic.icone_do_veiculo("PA CARREGADEIRA", "Linha amarela"), "pa-carregadeira")
        self.assertEqual(ic.icone_do_veiculo("MOTONIVELADORA", "Linha amarela"), "motoniveladora")
        self.assertEqual(ic.icone_do_veiculo(None, "Trator"), "trator")
        self.assertEqual(ic.icone_do_veiculo("CAMINHÃO ROLL OFF", "Caminhão"), "caminhao")

    def test_tipo_desconhecido_cai_no_generico_do_ambiente_nunca_num_palpite(self):
        self.assertEqual(ic.icone_id("Coisa Nova Que Ninguém Cadastrou", ambiente="ti"), "generico-ti")
        self.assertEqual(ic.icone_id("Coisa Nova Que Ninguém Cadastrou", ambiente="agro"), "maquina-generica")
        self.assertEqual(ic.icone_id("", ambiente="ti"), "generico-ti")
        self.assertEqual(ic.icone_id(None, ambiente="agro"), "maquina-generica")
        # nomes de MARCA sem o tipo dentro ficam genéricos: desenhar um tipo por palpite seria inventar
        for marca in ("Meraki", "Fortinet", "Cisco WLC"):
            self.assertEqual(ic.icone_id(marca, ambiente="ti"), "generico-ti", marca)

    def test_a_categoria_e_a_segunda_chance_e_so_e_lida_se_o_nome_nao_bastou(self):
        chamadas = []

        def categoria():
            chamadas.append(1)
            return "Switch"

        self.assertEqual(ic.icone_id("Notebook", ambiente="ti", categoria=categoria), "notebook")
        self.assertEqual(chamadas, [], "o nome bastou: ler a categoria seria uma consulta por linha à toa")
        self.assertEqual(ic.icone_id("Modelo X-9000", ambiente="ti", categoria=categoria), "switch")
        self.assertEqual(len(chamadas), 1)

    def test_ambiente_invalido_nao_e_adivinhado(self):
        with self.assertRaises(ValueError):
            ic.icone_id("Trator", ambiente="outro")

    def test_marca_nao_vira_logomarca_o_icone_e_do_tipo(self):
        """"Equipamento John Deere / Stara / Solinftec" são o TIPO do equipamento; a marca é outro campo."""
        self.assertEqual(ic.icone_id("Receptor GPS John Deere StarFire", ambiente="agro"), "receptor-gps")
        self.assertEqual(ic.icone_id("Monitor Stara Topper", ambiente="agro"), "monitor-agricola")
        self.assertEqual(ic.icone_id("Solinftec Solix", ambiente="agro"), "tecnologia-embarcada")
        self.assertFalse([i for i in ic.CATALOGO if re.search(r"deere|stara|solinftec|motorola", i.id + i.rotulo, re.I)])

    def test_estruturas_tem_simbolo_proprio(self):
        self.assertEqual(
            {t: ic.icone_da_estrutura(t) for t in ("torre", "estacao", "base", "referencia")},
            {"torre": "torre", "estacao": "estacao", "base": "base-rtk", "referencia": "ponto-referencia"},
        )


class TagsTests(TestCase):
    def render(self, modelo, **contexto):
        return Template("{% load zelo_icones %}" + modelo).render(Context(contexto))

    def test_o_icone_usa_o_sprite_e_e_decorativo_por_padrao(self):
        html = self.render('{% zelo_icone "trator" %}')
        self.assertIn('class="zi"', html)
        self.assertIn("icones/zelo/sprite.svg", html)
        self.assertIn("#zi-trator", html)
        self.assertIn('aria-hidden="true"', html)

    def test_com_titulo_o_icone_vira_imagem_acessivel(self):
        html = self.render('{% zelo_icone "trator" titulo="Trator" %}')
        self.assertIn('role="img"', html)
        self.assertIn('aria-label="Trator"', html)
        self.assertNotIn('aria-hidden="true"', html)

    def test_id_inexistente_some_sem_quebrar_a_tela(self):
        self.assertEqual(self.render('{% zelo_icone "nao-existe" %}').strip(), "")

    def test_titulo_com_html_e_escapado(self):
        html = self.render('{% zelo_icone "trator" titulo=t %}', t='<script>alert(1)</script>')
        self.assertNotIn("<script>", html)

    def test_tag_do_veiculo_usa_classe_antes_do_tipo(self):
        tipo = FrotaTipo.objects.get_or_create(nome="Caminhão")[0]

        class Classe:
            nome = "CAMINHÃO PIPA/BOMBEIRO"

        class V:
            classe_operacional = Classe()

        V.tipo = tipo
        self.assertIn("#zi-caminhao-pipa", self.render("{% zelo_icone_veiculo v %}", v=V()))

    def test_tag_do_item_ti_le_a_categoria_so_quando_o_nome_nao_basta(self):
        cat = Categoria.objects.create(nome="Equipamento")
        conhecido = Subtipo.objects.create(nome="Notebook", alocado="sim", categoria=cat)
        estranho = Subtipo.objects.create(nome="Modelo Z", alocado="sim", categoria=Categoria.objects.create(nome="Switch"))
        item_a, item_b = Item(nome="a", subtipo=conhecido), Item(nome="b", subtipo=estranho)
        conhecido_sem_cache = Subtipo.objects.get(pk=conhecido.pk)       # `categoria` ainda NÃO carregada
        item_a.subtipo = conhecido_sem_cache
        with self.assertNumQueries(0):
            self.assertIn("#zi-notebook", self.render("{% zelo_icone_item i %}", i=item_a))
        self.assertIn("#zi-switch", self.render("{% zelo_icone_item i %}", i=item_b))

    def test_filtro_devolve_so_o_id(self):
        self.assertEqual(self.render('{{ "Rádio Motorola HT"|zelo_icone_id:"agro" }}'), "radio-ht")


class AplicacaoNasTelasTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user("icones_admin", "i@x.com", "senha-boa-123", is_superuser=True)

    def setUp(self):
        self.client.force_login(self.admin)

    def test_toda_pagina_carrega_o_helper_de_navegador_com_o_sprite(self):
        html = self.client.get(reverse("agro_visao_geral")).content.decode()
        self.assertIn("js/zelo-icones.js", html)
        self.assertRegex(html, r'data-sprite="[^"]*icones/zelo/sprite\.svg')

    def test_lista_e_filtro_de_equipamentos_do_agro_usam_o_icone_do_tipo(self):
        tipo = FrotaTipo.objects.get_or_create(nome="Trator")[0]
        frota_service.criar_veiculo({"codigo": "MQ-I1", "nome": "Trator I1", "tipo": tipo.pk}, self.admin)
        html = self.client.get(reverse("frota_visao_geral")).content.decode()
        self.assertIn("#zi-trator", html)
        self.assertIn('data-icone="trator"', html)           # a opção "Trator" do filtro de categoria (vira Select2 com ícone)

    def test_ficha_e_lista_de_equipamentos_de_ti_usam_o_icone_do_subtipo(self):
        categoria = Categoria.objects.create(nome="Equipamento")
        subtipo = Subtipo.objects.create(nome="Notebook", alocado="sim", categoria=categoria)
        item = Item.objects.create(nome="NB-001", subtipo=subtipo, numero_serie="SN1")
        lista = self.client.get(reverse("equipamentos_list")).content.decode()
        self.assertIn("#zi-notebook", lista)
        self.assertIn('data-icone="notebook"', lista)         # opção do filtro de subtipo
        ficha = self.client.get(reverse("equipamento_detalhe", args=[item.pk])).content.decode()
        self.assertIn("#zi-notebook", ficha)

    def test_subtipos_do_cadastro_mostram_o_icone(self):
        categoria = Categoria.objects.create(nome="Equipamento")
        Subtipo.objects.create(nome="Switches", alocado="sim", categoria=categoria)
        self.assertIn("#zi-switch", self.client.get(reverse("subtipo_list")).content.decode())

    def test_a_busca_remota_de_veiculos_devolve_o_icone_para_o_select2(self):
        from services import m2m_monitoramento_service as m2m

        tipo = FrotaTipo.objects.get_or_create(nome="Colheitadeira")[0]
        frota_service.criar_veiculo({"codigo": "MQ-COL1", "nome": "Colheitadeira Um", "tipo": tipo.pk}, self.admin)
        achados = m2m.buscar_veiculos("MQ-COL")
        self.assertEqual(achados[0]["icone"], "colheitadeira")

    def test_o_catalogo_vivo_de_design_system_nao_foi_quebrado_pelo_css_novo(self):
        from django.contrib.staticfiles import finders

        css = Path(finders.find("css/ui-kit.css")).read_text(encoding="utf-8")
        self.assertIn(".zi {", css)
        self.assertIn("display: inline-block", css[css.index(".zi {"):css.index(".zi--sm")])
