"""
Frotas e Maquinários — a Composição tecnológica da frota, os Relatórios e o menu lateral do grupo.

A composição é uma LEITURA sobre o que o estoque tecnológico (`TecInstalacao`) e o vínculo de chip (`FrotaVinculo`) já registraram:
o que se trava aqui é que nada é inventado, que o chip só aparece para quem vê chips, e que retirar um equipamento o tira da tela.
Os relatórios apontam para a lista que cada número resume, e o teste confere que o número bate com a lista.
"""
from django.test import Client
from django.urls import reverse

from ProjetoEstoque.models import FrotaVeiculo, TecCategoria, TecFabricante, TecInstalacao
from ProjetoEstoque.tests.test_frota_equipamentos_telas import EquipamentosBase
from ProjetoEstoque.tests.test_m2m_telas import TelasBase
from services import ambiente_service as ambiente
from services import frota_qualidade_service as qualidade
from services import frota_service as frota
from services import tec_catalogo_service as cat
from services import tec_estoque_service as est
from services import tec_instalacao_service as ins

Status = FrotaVeiculo.Status


class ComposicaoBase(EquipamentosBase):
    """A frota de `EquipamentosBase` (a, b, c) com um monitor instalado em `a` e um chip vinculado a `b`; `c` fica sem nada."""

    def setUp(self):
        super().setUp()
        self.karitel = frota.fazendas()[0]
        self.deposito = cat.salvar_local({"nome": "Almoxarifado", "fazenda": self.karitel}, self.usuario)
        self.categoria = TecCategoria.objects.get(nome="Monitor / display")
        modelo = cat.salvar_modelo(
            {"fabricante": TecFabricante.objects.get(nome="John Deere"), "categoria": self.categoria, "nome": "G5", "controle": "individual"},
            self.usuario,
        )
        self.monitor = est.criar_equipamento({"modelo": modelo, "numero_serie": "SN-001", "local": self.deposito}, self.usuario).equipamento
        self.instalacao = ins.instalar(self.monitor, "veiculo", self.a, self.usuario, "instalação inicial").instalacao
        self.chip_b = self.vincular(self.chip(1), self.b)

    def pagina(self, cliente=None, **params):
        return (cliente or self.client).get(reverse("frota_composicao"), params)

    def codigos(self, resposta):
        return sorted(l["veiculo"].codigo_legado for l in resposta.context["linhas"])

    def usuario_tec(self, nome, *extras):
        return self.cliente_de(self.usuario_com(nome, "ver_frota", "ver_tec_estoque", *extras))


class ComposicaoAcessoTests(ComposicaoBase):
    def test_exige_login_ver_a_frota_e_ver_o_estoque_tecnologico(self):
        url = reverse("frota_composicao")
        self.assertEqual(Client().get(url).status_code, 302)
        self.assertEqual(self.cliente_de(self.usuario_com("so_frota", "ver_frota")).get(url).status_code, 403)
        self.assertEqual(self.cliente_de(self.usuario_com("so_tec", "ver_tec_estoque")).get(url).status_code, 403)
        self.assertEqual(self.usuario_tec("os_dois").get(url).status_code, 200)

    def test_o_chip_so_aparece_para_quem_tambem_ve_chips(self):
        sem_chips = self.usuario_tec("sem_chips")
        resposta = self.pagina(sem_chips)
        self.assertEqual({l["origem"] for l in resposta.context["linhas"]}, {"equipamento"})
        html = resposta.content.decode("utf-8")
        self.assertNotIn("Chips vinculados", html)
        self.assertNotIn(self.chip_b.nome, html)
        self.assertIsNone(resposta.context["resumo"]["chips"])
        com_chips = self.usuario_tec("com_chips", "ver_chips")
        self.assertEqual({l["origem"] for l in self.pagina(com_chips).context["linhas"]}, {"equipamento", "chip"})

    def test_filtrar_por_chip_sem_ver_chips_nao_traz_nada(self):
        self.assertEqual(self.pagina(self.usuario_tec("sem_chips"), categoria="chip").context["linhas"], [])


class ComposicaoDadosTests(ComposicaoBase):
    def test_o_resumo_sai_da_base_real(self):
        r = self.pagina().context["resumo"]
        self.assertEqual((r["maquinas"], r["com_tecnologia"], r["sem_tecnologia"], r["componentes"], r["chips"]), (3, 2, 1, 1, 1))
        self.assertEqual([(c["nome"], c["n"]) for c in r["por_categoria"]], [("Monitor / display", 1)])

    def test_maquina_desativada_nao_entra_na_conta(self):
        FrotaVeiculo.objects.filter(pk=self.c.pk).update(status=Status.DESATIVADO)
        r = self.pagina().context["resumo"]
        self.assertEqual((r["maquinas"], r["sem_tecnologia"]), (2, 0))

    def test_lista_so_o_que_esta_instalado_de_verdade(self):
        resposta = self.pagina()
        html = self.html(resposta)
        self.sem_lixo_de_template(html, "composicao")
        self.assertEqual(len(resposta.context["linhas"]), 2)
        for texto in ("SN-001", "G5", "John Deere", "Monitor / display", self.chip_b.nome, "8110001", "9100592"):
            self.assertIn(texto, html)
        self.assertNotIn("8720079", html)                                 # a moto não tem nada: não aparece nos instalados

    def test_cada_componente_leva_ao_proprio_registro(self):
        html = self.html(self.pagina())
        self.assertIn(reverse("tec_equipamento_detalhe", args=[self.monitor.pk]), html)
        self.assertIn(reverse("m2m_chip_detalhe", args=[self.chip_b.pk]), html)
        self.assertIn(reverse("frota_veiculo_detalhe", args=[self.a.pk]), html)

    def test_filtros_por_categoria_chip_natureza_e_busca(self):
        self.assertEqual(self.codigos(self.pagina(categoria=str(self.categoria.pk))), ["8110001"])
        self.assertEqual(self.codigos(self.pagina(categoria="chip")), ["9100592"])
        self.assertEqual(self.codigos(self.pagina(natureza="proprio")), ["8110001"])
        self.assertEqual(self.codigos(self.pagina(natureza="terceiro")), ["9100592"])
        self.assertEqual(self.codigos(self.pagina(q="sn-001")), ["8110001"])
        self.assertEqual(self.codigos(self.pagina(q="trator nh")), ["8110001"])
        self.assertEqual(self.codigos(self.pagina(q="não existe")), [])
        self.assertIn("Nenhum componente neste recorte", self.html(self.pagina(q="não existe")))

    def test_a_visao_das_maquinas_sem_tecnologia(self):
        resposta = self.pagina(vista="sem")
        html = self.html(resposta)
        self.assertEqual([v.codigo_legado for v in resposta.context["linhas"]], ["8720079"])
        self.assertIn("HONDA NXR BROS 160", html)
        self.assertNotIn("TRATOR NH 3880", html)
        self.assertEqual(self.pagina(vista="sem", natureza="terceiro").context["linhas"], [])
        self.assertEqual([v.codigo_legado for v in self.pagina(vista="sem", q="honda").context["linhas"]], ["8720079"])

    def test_retirar_o_equipamento_o_tira_da_lista_e_a_maquina_passa_a_ficar_sem_tecnologia(self):
        ins.retirar(self.instalacao.pk, self.deposito, self.usuario, "troca")
        self.assertEqual(self.codigos(self.pagina()), ["9100592"])
        self.assertEqual([v.codigo_legado for v in self.pagina(vista="sem").context["linhas"]], ["8110001", "8720079"])
        self.assertEqual(TecInstalacao.objects.filter(veiculo=self.a).count(), 1)   # o histórico da instalação fica

    def test_equipamento_instalado_num_operador_nao_conta_como_componente_de_maquina(self):
        from ProjetoEstoque.models import Usuario
        operador = Usuario.objects.create(nome="Ana Operadora", status="ativo", pmb="nao")
        modelo = cat.salvar_modelo(
            {"fabricante": TecFabricante.objects.get(nome="Motorola"), "categoria": TecCategoria.objects.get(nome="Rádio portátil (HT)"),
             "nome": "HT1250", "controle": "individual"}, self.usuario,
        )
        radio = est.criar_equipamento({"modelo": modelo, "numero_serie": "RD-1", "local": self.deposito}, self.usuario).equipamento
        ins.instalar(radio, "operador", operador, self.usuario, "rádio da operadora")
        self.assertEqual(self.pagina().context["resumo"]["componentes"], 1)
        self.assertNotIn("RD-1", self.html(self.pagina()))

    def test_sem_nenhum_componente_a_tela_diz_isso_sem_inventar(self):
        from django.db.models import F

        from ProjetoEstoque.models import FrotaVinculo
        TecInstalacao.objects.all().update(fim_em=F("inicio_em"))             # o período encerra (não pode acabar antes de começar)
        FrotaVinculo.objects.all().update(fim_em=F("inicio_em"))
        html = self.html(self.pagina())
        self.assertIn("Nenhum componente instalado", html)
        self.assertEqual(self.pagina().context["resumo"]["com_tecnologia"], 0)

    def test_a_listagem_pagina(self):
        for n in range(30):
            equipamento = est.criar_equipamento(
                {"modelo": self.monitor.modelo, "numero_serie": f"LOTE-{n:03d}", "local": self.deposito}, self.usuario,
            ).equipamento
            ins.instalar(equipamento, "veiculo", self.a, self.usuario, "carga")
        resposta = self.pagina()
        self.assertEqual(resposta.context["total_filtrado"], 32)
        self.assertEqual(len(resposta.context["linhas"]), 25)
        self.assertEqual(len(self.pagina(page=2).context["linhas"]), 7)


# ═════════════════════════════════════════════════════════════
# Relatórios
# ═════════════════════════════════════════════════════════════

class RelatoriosTests(EquipamentosBase):
    def test_exige_login_e_ver_a_frota(self):
        url = reverse("frota_relatorios")
        self.assertEqual(Client().get(url).status_code, 302)
        self.assertEqual(self.cliente_de(self.usuario_com("so_chips", "ver_chips")).get(url).status_code, 403)
        self.assertEqual(self.cliente_de(self.usuario_com("ve", "ver_frota")).get(url).status_code, 200)

    def test_a_pagina_renderiza_com_os_quadros(self):
        resposta = self.get("frota_relatorios")
        html = self.html(resposta)
        self.sem_lixo_de_template(html, "relatorios")
        for titulo in ("Inventário", "Por categoria", "Por classe operacional", "Conferências do cadastro", "Movimentações, histórico e importações"):
            self.assertIn(titulo, html)
        self.assertEqual(resposta.context["kpis"]["total"], 3)

    def test_o_quadro_por_categoria_fecha_com_o_total_e_cada_linha_abre_a_lista_do_mesmo_tamanho(self):
        quadro = self.get("frota_relatorios").context["por_categoria"]
        self.assertEqual(sum(c["total"] for c in quadro), FrotaVeiculo.objects.count())
        for c in quadro:
            with self.subTest(categoria=c["categoria"]):
                lista = self.get("frota_visao_geral", tipo=c["tipo_id"]).context
                self.assertEqual(lista["total_filtrado"], c["total"])
                self.assertEqual(frota.listar_veiculos({"natureza": "proprio", "tipo": c["tipo_id"]}).count(), c["proprios"])

    def test_o_quadro_por_classe_inclui_quem_nao_tem_classe_e_fecha_com_o_total(self):
        quadro = self.get("frota_relatorios").context["por_classe"]
        self.assertEqual(sum(c["total"] for c in quadro), FrotaVeiculo.objects.count())
        sem_classe = [c for c in quadro if c["classe_id"] is None]
        self.assertEqual(sum(c["total"] for c in sem_classe), 2)
        com_classe = next(c for c in quadro if c["classe_id"] == self.classe.pk)
        self.assertEqual((com_classe["classe"], com_classe["total"]), ("TRATOR LEVE", 1))
        self.assertEqual(self.get("frota_visao_geral", classe_operacional=self.classe.pk).context["total_filtrado"], 1)
        self.assertIn(reverse("frota_visao_geral") + "?pendencia=sem_classe", self.html(self.get("frota_relatorios")))

    def test_cada_conferencia_tem_o_mesmo_numero_da_lista_que_ela_abre(self):
        conferencias = self.get("frota_relatorios").context["conferencias"]
        self.assertTrue(conferencias)
        for c in conferencias:
            with self.subTest(conferencia=c["chave"]):
                self.assertIn(c["chave"], qualidade.PENDENCIAS)
                self.assertEqual(self.get("frota_visao_geral", pendencia=c["chave"]).context["total_filtrado"], c["n"])
        chaves = {c["chave"] for c in conferencias}
        self.assertIn("validacao", chaves)                      # `a` e `c` ficaram a validar

    def test_so_aparece_conferencia_que_tem_equipamento(self):
        FrotaVeiculo.objects.update(validacao_pendente=False)
        chaves = {c["chave"] for c in self.get("frota_relatorios").context["conferencias"]}
        self.assertNotIn("validacao", chaves)

    def test_o_botao_de_excel_so_aparece_para_quem_pode_exportar(self):
        html = self.html(self.get("frota_relatorios"))
        self.assertIn(reverse("frota_visao_geral") + "?formato=xlsx", html)
        so_ve = self.cliente_de(self.usuario_com("so_ve_rel", "ver_frota"))
        html = so_ve.get(reverse("frota_relatorios")).content.decode("utf-8")
        self.assertNotIn("formato=xlsx", html)
        self.assertEqual(so_ve.get(reverse("frota_visao_geral"), {"formato": "xlsx"}).status_code, 403)

    def test_movimentacoes_historico_e_importacoes_dependem_da_permissao_de_cada_um(self):
        so_ve = self.cliente_de(self.usuario_com("so_ve_rel2", "ver_frota")).get(reverse("frota_relatorios")).content.decode("utf-8")
        self.assertNotIn(reverse("frota_equip_movimentacoes"), so_ve)
        self.assertNotIn(reverse("frota_equip_importacoes"), so_ve)
        self.assertIn("Sem permissão para os relatórios", so_ve)
        completo = self.html(self.get("frota_relatorios"))
        for rota in ("frota_equip_movimentacoes", "frota_equip_historico", "frota_equip_importacoes"):
            self.assertIn(reverse(rota), completo)

class RelatoriosBaseVaziaTests(TelasBase):
    def test_base_vazia_nao_inventa_numero(self):
        self.assertEqual(FrotaVeiculo.objects.count(), 0)
        resposta = self.get("frota_relatorios")
        html = self.html(resposta)
        self.assertEqual((resposta.context["por_categoria"], resposta.context["por_classe"], resposta.context["conferencias"]), ([], [], []))
        self.assertEqual(resposta.context["kpis"]["total"], 0)
        self.assertIn("Nenhum equipamento cadastrado ainda", html)
        self.assertIn("Nenhuma conferência pendente", html)


# ═════════════════════════════════════════════════════════════
# Menu lateral do grupo
# ═════════════════════════════════════════════════════════════

class MenuDeFrotasTests(EquipamentosBase):
    ROTULOS = ["Visão geral", "Próprios", "Terceiros", "Categorias e classes", "Importação de planilha", "Composição tecnológica",
               "Movimentações", "Histórico", "Relatórios"]

    def grupo(self, user, url_name=""):
        return next((g for g in ambiente.nav_de(user, url_name) if g.get("chave") == "frotas"), None)

    def test_o_grupo_tem_os_itens_da_estrutura_pedida_na_ordem(self):
        grupo = self.grupo(self.admin)
        self.assertEqual(grupo["rotulo"], "Frotas e Maquinários")
        self.assertEqual([i["rotulo"] for i in grupo["itens"]], self.ROTULOS)
        self.assertEqual(
            [i["href"] for i in grupo["itens"]],
            [reverse(n) for n in ("frota_visao_geral", "frota_equipamentos_proprios", "frota_equipamentos_terceiros", "frota_categorias",
                                  "frota_equip_importar", "frota_composicao", "frota_equip_movimentacoes", "frota_equip_historico",
                                  "frota_relatorios")],
        )

    def test_cada_item_so_aparece_para_quem_tem_a_permissao_dele(self):
        casos = {
            "Categorias e classes": "administrar_frota_config", "Importação de planilha": "importar_equipamentos_frota",
            "Composição tecnológica": "ver_tec_estoque", "Movimentações": "ver_historico_frota", "Histórico": "ver_historico_frota",
        }
        base = [i["rotulo"] for i in self.grupo(self.usuario_com("base_menu", "ver_frota"))["itens"]]
        self.assertEqual(base, ["Visão geral", "Próprios", "Terceiros", "Relatórios"])
        for n, (rotulo, permissao) in enumerate(casos.items()):
            with self.subTest(item=rotulo):
                itens = [i["rotulo"] for i in self.grupo(self.usuario_com(f"menu_{n}", "ver_frota", permissao))["itens"]]
                self.assertIn(rotulo, itens)

    def test_quem_so_ve_chips_nao_ve_o_grupo_de_frotas_e_maquinarios(self):
        self.assertIsNone(self.grupo(self.usuario_com("so_chips_menu", "ver_chips")))

    def test_o_item_certo_acende_em_cada_tela_do_grupo(self):
        casos = {
            "frota_visao_geral": "Visão geral", "frota_veiculos": "Visão geral", "frota_veiculo_detalhe": "Visão geral",
            "frota_veiculo_editar": "Visão geral", "frota_veiculo_novo": "Visão geral", "frota_veiculo_movimentar": "Visão geral",
            "frota_equipamentos_proprios": "Próprios", "frota_equipamentos_terceiros": "Terceiros",
            "frota_categorias": "Categorias e classes", "frota_config_tipos": "Categorias e classes",
            "frota_cadastro_salvar": "Categorias e classes", "frota_config_tipo_salvar": "Categorias e classes",
            "frota_equip_importar": "Importação de planilha", "frota_equip_importacao": "Importação de planilha",
            "frota_equip_importacoes": "Importação de planilha", "frota_equip_importacao_mapear": "Importação de planilha",
            "frota_composicao": "Composição tecnológica", "frota_equip_movimentacoes": "Movimentações",
            "frota_equip_historico": "Histórico", "frota_relatorios": "Relatórios",
        }
        for rota, esperado in casos.items():
            with self.subTest(rota=rota):
                acesos = [i["rotulo"] for i in self.grupo(self.admin, rota)["itens"] if i["ativo"]]
                self.assertEqual(acesos, [esperado])

    def test_as_telas_de_conectividade_nao_acendem_nada_em_frotas(self):
        for rota in ("m2m_visao_geral", "m2m_chips", "frota_importar", "frota_importacao", "frota_movimentacoes", "m2m_alertas"):
            with self.subTest(rota=rota):
                self.assertFalse(any(i["ativo"] for i in self.grupo(self.admin, rota)["itens"]))

    def test_toda_rota_do_menu_existe_e_abre(self):
        for item in self.grupo(self.admin)["itens"]:
            with self.subTest(item=item["rotulo"]):
                resposta = self.client.get(item["href"])
                self.assertEqual(resposta.status_code, 200)
                self.assertIn(item["href"], resposta.content.decode("utf-8"))             # e o menu o mostra na própria página

    def test_o_menu_renderizado_traz_o_grupo_e_marca_o_item_atual(self):
        html = self.html(self.get("frota_relatorios"))
        self.assertIn("Frotas e Maquinários", html)
        self.assertRegex(html, rf'href="{reverse("frota_relatorios")}"[^>]*aria-current="page"|aria-current="page"[^>]*href="{reverse("frota_relatorios")}"')

    def test_o_nome_antigo_do_grupo_saiu(self):
        self.assertNotIn("Tipos de veículo", [i["rotulo"] for i in self.grupo(self.admin)["itens"]])
        self.assertNotIn("Veículos e equipamentos", [i["rotulo"] for i in self.grupo(self.admin)["itens"]])
