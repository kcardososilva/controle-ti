"""
Frotas e Conectividade — o cadastro do veículo e a relação chip x veículo nas listas (12/11/2026).

O que estes testes protegem:

  • FAZENDA — o veículo escolhe entre Fazenda Karitel e Fazenda Rio do Meio, e só. O `Localidade` do
    sistema é o cadastro de setores do estoque de TI inteiro: as duas linhas são criadas uma vez e
    reaproveitadas. A restrição é da TELA; quem já tem outra unidade continua com ela ao editar.
  • NOVO TIPO — o formulário do veículo cria o tipo por um JSON próprio, com a MESMA permissão da
    tela de Configuração (`administrar_frota_config`): quem só edita veículo não ganha esse poder.
  • VINCULADOS x LIVRES — a lista de chips e a de veículos mostram a contagem de cada lado e cada
    segmento abre a lista filtrada com o mesmo número.
"""

from django.urls import reverse

from ProjetoEstoque.models import FrotaTipo, FrotaVeiculo, Localidade
from ProjetoEstoque.tests.test_m2m_telas import TelasBase
from services import frota_service as frota


class FazendasTests(TelasBase):
    def dados_do_veiculo(self, **extra):
        dados = {
            "codigo": "TR-900", "nome": "Trator de teste", "tipo": FrotaTipo.objects.get(nome="Trator").pk,
            "status": "ativo", "exige_conectividade": "",
        }
        dados.update(extra)
        return dados

    def test_cria_as_duas_fazendas_uma_vez_so(self):
        Localidade.objects.filter(local__icontains="Fazenda").delete()
        primeira = frota.fazendas()
        segunda = frota.fazendas()
        self.assertEqual([f.local for f in primeira], ["Fazenda Karitel", "Fazenda Rio do Meio"])
        self.assertEqual([f.codigo for f in primeira], ["Karitel", "Rio_do_Meio"])
        self.assertEqual([f.pk for f in primeira], [f.pk for f in segunda])
        self.assertEqual(Localidade.objects.filter(local__startswith="Fazenda ").count(), 2)

    def test_reaproveita_a_linha_que_ja_existe_sem_distinguir_caixa(self):
        existente = Localidade.objects.create(local="FAZENDA KARITEL")
        self.assertEqual(frota.fazendas()[0].pk, existente.pk)
        self.assertEqual(Localidade.objects.filter(local__iexact="Fazenda Karitel").count(), 1)

    def test_formulario_oferece_so_as_duas_fazendas(self):
        Localidade.objects.create(local="Colheita Tabaco")
        resposta = self.get("frota_veiculo_novo")
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual([nome for _, nome in resposta.context["localidades"]], ["Fazenda Karitel", "Fazenda Rio do Meio"])
        html = resposta.content.decode("utf-8")
        self.assertIn("Fazenda Karitel", html)
        self.assertNotIn("Colheita Tabaco", html)

    def test_cadastra_com_uma_das_fazendas(self):
        rio = frota.fazendas()[1]
        resposta = self.client.post(reverse("frota_veiculo_novo"), self.dados_do_veiculo(localidade=rio.pk))
        self.assertEqual(resposta.status_code, 302)
        self.assertEqual(FrotaVeiculo.objects.get(codigo="TR-900").localidade_id, rio.pk)

    def test_recusa_outra_unidade_e_nao_grava(self):
        outra = Localidade.objects.create(local="Colheita Tabaco")
        resposta = self.client.post(reverse("frota_veiculo_novo"), self.dados_do_veiculo(localidade=outra.pk))
        self.assertEqual(resposta.status_code, 400)
        self.assertIn("Escolha Fazenda Karitel ou Fazenda Rio do Meio", resposta.content.decode("utf-8"))
        self.assertFalse(FrotaVeiculo.objects.filter(codigo="TR-900").exists())
        # valor que nem é um id também é recusado
        resposta = self.client.post(reverse("frota_veiculo_novo"), self.dados_do_veiculo(localidade="abc"))
        self.assertEqual(resposta.status_code, 400)

    def test_sem_fazenda_continua_valendo(self):
        resposta = self.client.post(reverse("frota_veiculo_novo"), self.dados_do_veiculo(localidade=""))
        self.assertEqual(resposta.status_code, 302)
        self.assertIsNone(FrotaVeiculo.objects.get(codigo="TR-900").localidade_id)

    def test_veiculo_com_unidade_antiga_continua_com_ela_ao_editar(self):
        antiga = Localidade.objects.create(local="Setor Antigo")
        veiculo = self.veiculo(1, localidade=antiga.pk)
        pagina = self.get("frota_veiculo_editar", veiculo.pk)
        self.assertIn("Setor Antigo", [nome for _, nome in pagina.context["localidades"]])
        dados = self.dados_do_veiculo(codigo=veiculo.codigo, nome="Nome novo", localidade=antiga.pk)
        resposta = self.client.post(reverse("frota_veiculo_editar", args=[veiculo.pk]), dados)
        self.assertEqual(resposta.status_code, 302)
        veiculo.refresh_from_db()
        self.assertEqual((veiculo.nome, veiculo.localidade_id), ("Nome novo", antiga.pk))
        # mas trocar para outra unidade que não seja fazenda, não
        outra = Localidade.objects.create(local="Outro Setor")
        dados["localidade"] = outra.pk
        self.assertEqual(self.client.post(reverse("frota_veiculo_editar", args=[veiculo.pk]), dados).status_code, 400)


class NovoTipoTests(TelasBase):
    def post(self, cliente=None, **dados):
        return (cliente or self.client).post(reverse("frota_tipo_rapido"), dados)

    def test_cria_o_tipo_e_devolve_json_para_o_formulario(self):
        resposta = self.post(nome="Motoniveladora", exige_conectividade="1")
        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        tipo = FrotaTipo.objects.get(nome="Motoniveladora")
        self.assertEqual((dados["id"], dados["nome"], tipo.exige_conectividade, tipo.ativo), (tipo.pk, "Motoniveladora", True, True))

    def test_sem_marcar_nao_exige_chip(self):
        self.post(nome="Reboque")
        self.assertFalse(FrotaTipo.objects.get(nome="Reboque").exige_conectividade)

    def test_nome_repetido_ou_vazio_volta_400_com_o_motivo(self):
        for nome in ("trator", "  ", ""):
            with self.subTest(nome=nome):
                resposta = self.post(nome=nome)
                self.assertEqual(resposta.status_code, 400)
                corpo = resposta.json()
                self.assertFalse(corpo["ok"])
                self.assertIn("nome", corpo["erros"])
        self.assertEqual(FrotaTipo.objects.filter(nome__iexact="trator").count(), 1)

    def test_so_post(self):
        self.assertEqual(self.client.get(reverse("frota_tipo_rapido")).status_code, 405)

    def test_quem_so_edita_veiculo_nao_cria_tipo(self):
        editor = self.cliente_de(self.usuario_com("so_editor", "editar_frota"))
        self.assertEqual(self.post(editor, nome="Escavadeira").status_code, 403)
        self.assertFalse(FrotaTipo.objects.filter(nome="Escavadeira").exists())

    def test_o_botao_so_aparece_para_quem_administra_a_configuracao(self):
        self.assertIn('data-frota-abrir="novo_tipo"', self.get("frota_veiculo_novo").content.decode("utf-8"))
        editor = self.cliente_de(self.usuario_com("so_editor2", "editar_frota"))
        html = editor.get(reverse("frota_veiculo_novo")).content.decode("utf-8")
        self.assertNotIn('data-frota-abrir="novo_tipo"', html)
        self.assertNotIn("frota-dlg-novo_tipo", html)
        self.assertIn("Peça a quem administra a configuração", html)


class VinculadosELivresTests(TelasBase):
    def setUp(self):
        super().setUp()
        self.livre1, self.livre2, self.preso = self.chip(1), self.chip(2), self.chip(3)
        self.vincular(self.preso, self.veiculo(1, codigo="TR-01", nome="Trator Um"))
        self.veiculo(2, codigo="TR-02", nome="Trator Dois")             # sem chip

    def segmentos(self, nome):
        resposta = self.get(nome)
        self.assertEqual(resposta.status_code, 200)
        chave = "segmentos_uso" if nome == "m2m_chips" else "segmentos_chip"
        return {s["rotulo"]: s for s in resposta.context[chave]}

    def test_lista_de_chips_conta_vinculados_e_disponiveis(self):
        segmentos = self.segmentos("m2m_chips")
        self.assertEqual([(r, s["n"]) for r, s in segmentos.items()], [("Todos", 3), ("Vinculados", 1), ("Disponíveis", 2)])
        self.assertTrue(segmentos["Todos"]["ativo"])

    def test_cada_segmento_abre_a_lista_com_o_mesmo_numero(self):
        for rotulo, esperado in (("Vinculados", 1), ("Disponíveis", 2)):
            with self.subTest(segmento=rotulo):
                url = self.segmentos("m2m_chips")[rotulo]["url"]
                resposta = self.client.get(reverse("m2m_chips") + url)
                self.assertEqual(resposta.context["page_obj"].paginator.count, esperado)
                ativos = [s["rotulo"] for s in resposta.context["segmentos_uso"] if s["ativo"]]
                self.assertEqual(ativos, [rotulo])

    def test_segmento_mantem_os_outros_filtros_e_volta_a_pagina_1(self):
        resposta = self.client.get(reverse("m2m_chips"), {"q": "Chip", "page": "2", "situacao_uso": "vinculado"})
        url = {s["rotulo"]: s["url"] for s in resposta.context["segmentos_uso"]}["Disponíveis"]
        self.assertIn("q=Chip", url)
        self.assertIn("situacao_uso=disponivel", url)
        self.assertNotIn("page=", url)

    def test_manutencao_e_inativos_so_aparecem_quando_existem(self):
        self.assertNotIn("Em manutenção", self.segmentos("m2m_chips"))
        self.assertNotIn("Inativos", self.segmentos("m2m_chips"))

    def test_lista_de_veiculos_conta_com_e_sem_chip(self):
        segmentos = self.segmentos("frota_veiculos")
        self.assertEqual([(r, s["n"]) for r, s in segmentos.items()], [("Todos", 2), ("Com chip", 1), ("Sem chip", 1)])
        resposta = self.client.get(reverse("frota_veiculos") + segmentos["Sem chip"]["url"])
        self.assertEqual([v.codigo for v in resposta.context["veiculos"]], ["TR-02"])

    def test_chip_disponivel_tem_o_botao_vincular_e_o_vinculado_nao(self):
        html = self.get("m2m_chips").content.decode("utf-8")
        linhas = {self.livre1.pk: None, self.preso.pk: None}
        import re
        for pk in linhas:
            linhas[pk] = re.search(rf'<tr class="m2m-linha" data-chip-id="{pk}".*?</tr>', html, re.S).group(0)
        self.assertIn('data-frota-abrir="vincular_veiculo"', linhas[self.livre1.pk])
        self.assertNotIn('data-frota-abrir="vincular_veiculo"', linhas[self.preso.pk])
        # quem não tem a permissão de vincular não vê o botão
        leitor = self.cliente_de(self.usuario_com("so_leitor", "ver"))
        html = leitor.get(reverse("m2m_chips")).content.decode("utf-8")
        self.assertNotIn('data-frota-abrir="vincular_veiculo"', html)
