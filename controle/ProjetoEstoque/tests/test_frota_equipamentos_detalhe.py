"""
Frotas e Maquinários — o detalhe do equipamento em abas, as ações dele (validar, registrar movimentação, excluir) e as
telas de Movimentações e Histórico da frota.

A base (`EquipamentosBase`: um admin logado e três equipamentos que cobrem as distinções da tela) é a de
`test_frota_equipamentos_telas`.
"""
import io
import re

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from openpyxl import load_workbook

from ProjetoEstoque.models import FrotaCentroCusto, FrotaEvento, FrotaVeiculo, Localidade, Usuario
from ProjetoEstoque.tests.test_frota_equipamentos_telas import EquipamentosBase
from services import frota_service as frota
from services import frota_vinculo_service as vinculos

Status = FrotaVeiculo.Status


class DetalheTests(EquipamentosBase):
    def detalhe(self, veiculo=None, cliente=None, **params):
        return (cliente or self.client).get(reverse("frota_veiculo_detalhe", args=[(veiculo or self.a).pk]), params)

    def pagina(self, resposta=None):
        resposta = resposta or self.detalhe()
        self.assertEqual(resposta.status_code, 200)
        return resposta.content.decode("utf-8")

    def cabecalho(self, html):
        return re.search(r'<header class="page-header.*?</header>', html, re.S).group(0)

    def test_cabecalho_com_nome_codigo_copiavel_classe_empresa_e_setor(self):
        html = self.pagina()
        self.sem_lixo_de_template(html, "detalhe")
        self.assertIn("TRATOR NH 3880", html)
        self.assertRegex(html, r'Código: <b class="m2m-id">8110001</b>')
        self.assertIn('data-agro-copiar="8110001"', html)
        self.assertIn("Classe: TRATOR LEVE", html)
        self.assertIn("Empresa: SANTA COLOMBA", html)
        self.assertIn("Setor: GRÃOS", html)
        self.assertRegex(html, r'Zelo Agro</a></li>\s*<li><a href="[^"]*">Frotas e Maquinários</a></li>\s*<li><span aria-current="page">8110001</span></li>')

    def test_o_resumo_traz_os_seis_dados_do_prompt(self):
        resumo = re.search(r'<section class="m2m-resumo".*?</section>', self.pagina(), re.S).group(0)
        for rotulo in ("Empresa / propriedade", "Situação operacional", "Classe operacional", "Modelo", "Localização", "Responsável"):
            self.assertIn(rotulo, resumo)
        for esperado in ("Em uso", "a validar", "Próprio"):
            self.assertIn(esperado, resumo)

    def test_as_cinco_abas_do_prompt(self):
        abas = re.findall(r'<(?:section|div)[^>]*data-secao="([^"]+)"', self.pagina())
        self.assertEqual(abas, ["Dados gerais", "Dados técnicos", "Composição tecnológica", "Movimentações", "Histórico"])

    def test_dados_gerais(self):
        geral = re.search(r'id="m2m-v-d-geral".*?</section>', self.pagina(), re.S).group(0)
        for esperado in ("Código original (ERP)", "8110001", "Identificador interno", self.a.codigo, "Empresa", "Classe operacional",
                         "Classe mecânica", "Modelo", "Setor", "Centro de custo", "32107", "TRATOR LEVE - GRAOS"):
            self.assertIn(esperado, geral)

    def test_dados_tecnicos_e_documentos(self):
        FrotaVeiculo.objects.filter(pk=self.a.pk).update(observacoes="Documento no escritório")
        tecnico = re.search(r'id="m2m-v-d-tecnico".*?</section>', self.pagina(), re.S).group(0)
        for esperado in ("1CQS660ALH0120143", "C811009", "2011", "15/08/2018", "Documentos e observações", "Documento no escritório"):
            self.assertIn(esperado, tecnico)

    def test_o_codigo_original_ausente_diz_que_nao_veio_do_erp(self):
        manual = frota.criar_veiculo({"codigo": "COTONN", "nome": "COTONN", "tipo": self.trator.pk}, self.usuario)
        html = self.pagina(self.detalhe(manual))
        self.assertIn("não veio do ERP", html)
        self.assertIn('data-agro-copiar="COTONN"', html)               # sem código do ERP, o interno é o que se copia

    def test_conferencias_apontam_o_que_precisa_de_olho_sem_corrigir(self):
        # o duplicado entra como o ERP o traz (a digitação à mão o recusaria, de propósito)
        FrotaVeiculo.objects.create(codigo="MQ-9", nome="OUTRO", tipo=self.trator, codigo_legado="8210001", chassi_serie="1CQS660ALH0120143")
        conferir = re.search(r'id="m2m-v-d-conferir".*?</section>', self.pagina(), re.S).group(0)
        self.assertIn("Chassi repetido", conferir)
        self.assertIn("8210001", conferir)
        self.assertIn("Placa fora do padrão", conferir)
        self.assertIn("nada disso é corrigido sozinho", conferir)
        self.assertEqual(FrotaVeiculo.objects.get(pk=self.a.pk).chassi_serie, "1CQS660ALH0120143")

    def test_cadastro_sem_pendencia_diz_que_nao_ha_o_que_conferir(self):
        limpo = frota.criar_veiculo({
            "nome": "TRATOR JD 8400R", "tipo": self.trator.pk, "codigo_legado": "8130001", "classe_operacional": self.classe.pk,
            "setor": "GRÃOS", "ano_fabricacao": 2020, "ano_modelo": 2020, "placa": "ABC1D23", "chassi_serie": "LIMPO123",
        }, self.usuario)
        self.assertIn("Nada a conferir neste cadastro.", self.pagina(self.detalhe(limpo)))

    def test_o_caminhao_de_terceiro_sem_classe_nem_setor_tem_o_que_conferir(self):
        html = self.pagina(self.detalhe(self.b))
        conferir = re.search(r'id="m2m-v-d-conferir".*?</section>', html, re.S).group(0)
        for esperado in ("Sem classe operacional", "Setor não informado", "Ano-modelo à frente do ano corrente"):
            self.assertIn(esperado, conferir)

    def test_aviso_de_situacao_a_validar_com_o_botao_so_para_quem_pode(self):
        html = self.pagina()
        self.assertIn("Situação a validar.", html)
        self.assertIn(reverse("frota_veiculo_validar", args=[self.a.pk]), html)
        so_ve = self.cliente_de(self.usuario_com("so_ve", "ver_frota"))
        html = self.pagina(self.detalhe(cliente=so_ve))
        self.assertIn("Situação a validar.", html)
        self.assertNotIn(reverse("frota_veiculo_validar", args=[self.a.pk]), html)

    def test_validado_nao_mostra_o_aviso_nem_o_botao(self):
        html = self.pagina(self.detalhe(self.b))
        self.assertNotIn("Situação a validar.", html)
        self.assertNotIn(reverse("frota_veiculo_validar", args=[self.b.pk]), html)

    def test_as_acoes_do_cabecalho_dependem_da_permissao(self):
        cab = self.cabecalho(self.pagina())
        for esperado in ("Editar", "Registrar movimentação", "Vincular tecnologia", "Consultar histórico", "Mais ações", "Validar cadastro",
                         "Mudar situação", "Excluir equipamento"):
            self.assertIn(esperado, cab)
        so_ve = self.cliente_de(self.usuario_com("so_ve2", "ver_frota"))
        cab = self.cabecalho(self.pagina(self.detalhe(cliente=so_ve)))
        for ausente in ("Editar", "Registrar movimentação", "Mais ações", "Vincular tecnologia"):
            self.assertNotIn(ausente, cab)
        self.assertIn("Consultar histórico", cab)

    def test_equipamento_desativado_nao_oferece_movimentar_nem_excluir(self):
        frota.mudar_status(self.b, Status.DESATIVADO, self.usuario)
        html = self.pagina(self.detalhe(self.b))
        cab = self.cabecalho(html)
        self.assertNotIn("Registrar movimentação", cab)
        self.assertNotIn("Excluir equipamento", cab)
        self.assertIn("Equipamento desativado (excluído)", html)

    def test_aba_de_movimentacoes_mostra_o_de_para_o_motivo_e_quem(self):
        frota.registrar_movimentacao(self.a, {"setor": "CACAU"}, self.usuario, motivo="safra de cacau")
        aba = re.search(r'id="m2m-v-d-mov".*?</section>', self.pagina(), re.S).group(0)
        for esperado in ("Setor", "GRÃOS", "CACAU", "safra de cacau", self.usuario.get_username()):
            self.assertIn(esperado, aba)

    def test_aba_de_movimentacoes_vazia(self):
        self.assertIn("Nenhuma movimentação registrada", self.pagina(self.detalhe(self.b)))

    def test_historico_traz_o_cadastro_e_o_que_depois_aconteceu(self):
        frota.registrar_movimentacao(self.a, {"setor": "CACAU"}, self.usuario, motivo="safra")
        hist = re.search(r'id="m2m-v-d-hist".*?</section>', self.pagina(), re.S).group(0)
        for esperado in ("Veículo cadastrado", "Movimentação registrada", "Motivo: safra"):
            self.assertIn(esperado, hist)

    def test_filtro_do_historico_por_fonte(self):
        resposta = self.detalhe(hist="evento")
        self.pagina(resposta)
        self.assertEqual({i["fonte"] for i in resposta.context["linha_do_tempo"]}, {"evento"})

    def test_a_aba_de_composicao_junta_instalados_chips_e_conectividade(self):
        aba = re.search(r'id="m2m-v-d-composicao".*?(?=<section class="m2m-painel" id="m2m-v-d-mov")', self.pagina(), re.S).group(0)
        for ancora in ('id="m2m-v-d-comp"', 'id="m2m-v-d-chips"', 'id="m2m-v-d-conect"'):
            self.assertIn(ancora, aba)

    def test_o_detalhe_nao_faz_consulta_por_linha_do_historico(self):
        for n in range(15):
            frota.registrar_movimentacao(self.a, {"setor": f"SETOR {n}"}, self.usuario, motivo=f"mudança {n}")
        self.detalhe()
        with CaptureQueriesContext(connection) as poucas:
            self.detalhe()
        for n in range(15, 30):
            frota.registrar_movimentacao(self.a, {"setor": f"SETOR {n}"}, self.usuario, motivo=f"mudança {n}")
        with CaptureQueriesContext(connection) as muitas:
            self.detalhe()
        self.assertEqual(len(poucas), len(muitas))


class AcoesDoEquipamentoTests(EquipamentosBase):
    def ajax(self, nome, pk, dados, cliente=None):
        return (cliente or self.client).post(reverse(nome, args=[pk]), dados, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def test_validar_pelo_dialogo(self):
        r = self.ajax("frota_veiculo_validar", self.a.pk, {"status": "", "motivo": "conferido no pátio"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertIn("validado", r.json()["mensagem"])
        v = FrotaVeiculo.objects.get(pk=self.a.pk)
        self.assertFalse(v.validacao_pendente)
        self.assertEqual(FrotaEvento.objects.get(veiculo=v, tipo=FrotaEvento.Tipo.VALIDACAO).dados["motivo"], "conferido no pátio")

    def test_validar_e_ajustar_a_situacao_na_mesma_hora(self):
        self.ajax("frota_veiculo_validar", self.a.pk, {"status": "inativo"})
        v = FrotaVeiculo.objects.get(pk=self.a.pk)
        self.assertEqual((v.validacao_pendente, v.status), (False, Status.INATIVO))

    def test_validar_duas_vezes_devolve_o_erro_dentro_do_dialogo(self):
        self.ajax("frota_veiculo_validar", self.a.pk, {})
        r = self.ajax("frota_veiculo_validar", self.a.pk, {})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.json()["ok"])
        self.assertIn("já foi validado", r.json()["mensagem"])

    def test_validar_sem_ajax_volta_para_o_detalhe_com_mensagem(self):
        r = self.client.post(reverse("frota_veiculo_validar", args=[self.a.pk]), {})
        self.assertRedirects(r, reverse("frota_veiculo_detalhe", args=[self.a.pk]), fetch_redirect_response=False)

    def test_so_quem_valida_o_inventario_valida(self):
        editor = self.cliente_de(self.usuario_com("editor", "ver_frota", "editar_frota"))
        self.assertEqual(self.ajax("frota_veiculo_validar", self.a.pk, {}, editor).status_code, 403)
        self.assertTrue(FrotaVeiculo.objects.get(pk=self.a.pk).validacao_pendente)

    def test_movimentar_registra_o_de_para_e_o_motivo_e_campo_em_branco_nao_muda(self):
        r = self.ajax("frota_veiculo_movimentar", self.a.pk, {"setor": "CACAU", "centro_custo": "", "localidade": "", "responsavel": "", "motivo": "safra"})
        self.assertEqual(r.status_code, 200, r.content)
        v = FrotaVeiculo.objects.get(pk=self.a.pk)
        self.assertEqual(v.setor, "CACAU")
        e = FrotaEvento.objects.get(veiculo=v, tipo=FrotaEvento.Tipo.MOVIMENTACAO)
        self.assertEqual((e.dados["motivo"], e.dados["alteracoes"]["setor"]), ("safra", {"de": "GRÃOS", "para": "CACAU"}))
        self.assertEqual(list(e.dados["alteracoes"]), ["setor"])

    def test_movimentar_muda_a_fazenda_so_entre_as_duas(self):
        _, rio = frota.fazendas()
        r = self.ajax("frota_veiculo_movimentar", self.a.pk, {"localidade": str(rio.pk), "motivo": "foi para o Rio do Meio"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(FrotaVeiculo.objects.get(pk=self.a.pk).localidade, rio)
        outra = Localidade.objects.create(local="Colheita Tabaco")
        r = self.ajax("frota_veiculo_movimentar", self.a.pk, {"localidade": str(outra.pk), "motivo": "x"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Escolha Fazenda Karitel ou Fazenda Rio do Meio", r.json()["mensagem"])
        self.assertEqual(FrotaVeiculo.objects.get(pk=self.a.pk).localidade, rio)

    def test_movimentar_exige_motivo_e_mudanca(self):
        r = self.ajax("frota_veiculo_movimentar", self.a.pk, {"setor": "CACAU", "motivo": ""})
        self.assertEqual(r.status_code, 400)
        self.assertIn("motivo", r.json()["mensagem"].lower())
        r = self.ajax("frota_veiculo_movimentar", self.a.pk, {"motivo": "nada mudou"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Nada mudou", r.json()["mensagem"])
        self.assertFalse(FrotaEvento.objects.filter(tipo=FrotaEvento.Tipo.MOVIMENTACAO).exists())

    def test_movimentar_muda_o_centro_de_custo_e_o_responsavel(self):
        outro = FrotaCentroCusto.objects.create(codigo_erp="32119", nome="CAMINHAO - GRAOS")
        pessoa = Usuario.objects.create(nome="Maria Operadora", matricula="123")
        r = self.ajax("frota_veiculo_movimentar", self.a.pk, {"centro_custo": str(outro.pk), "responsavel": str(pessoa.pk), "motivo": "reorganização"})
        self.assertEqual(r.status_code, 200, r.content)
        v = FrotaVeiculo.objects.get(pk=self.a.pk)
        self.assertEqual((v.centro_custo, v.responsavel), (outro, pessoa))

    def test_so_quem_edita_a_frota_movimenta(self):
        leitor = self.cliente_de(self.usuario_com("leitor", "ver_frota", "validar_inventario_frota"))
        self.assertEqual(self.ajax("frota_veiculo_movimentar", self.a.pk, {"setor": "X", "motivo": "x"}, leitor).status_code, 403)

    def test_equipamento_desativado_nao_e_movimentado(self):
        frota.mudar_status(self.b, Status.DESATIVADO, self.usuario)
        r = self.ajax("frota_veiculo_movimentar", self.b.pk, {"setor": "X", "motivo": "x"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("desativado", r.json()["mensagem"])

    def test_o_dialogo_de_excluir_mostra_o_que_foi_conferido(self):
        html = self.client.get(reverse("frota_veiculo_detalhe", args=[self.a.pk])).content.decode("utf-8")
        dialogo = re.search(r'id="frota-dlg-excluir".*?</form>', html, re.S).group(0)
        for esperado in ("Excluir é <b>desativar</b>", "preservados"):
            self.assertIn(esperado, dialogo)
        self.assertRegex(dialogo, r"<b class=\"m2m-num\">0</b> chips vinculados")
        self.assertRegex(dialogo, r"<b class=\"m2m-num\">0</b> equipamentos tecnológicos instalados")
        self.assertNotIn("Não dá para excluir agora", dialogo)

    def test_excluir_e_desativar_com_motivo_e_o_historico_continua(self):
        r = self.ajax("frota_veiculo_status", self.a.pk, {"status": "desativado", "motivo": "vendido"})
        self.assertEqual(r.status_code, 200)
        v = FrotaVeiculo.objects.get(pk=self.a.pk)
        self.assertEqual(v.status, Status.DESATIVADO)
        self.assertTrue(FrotaEvento.objects.filter(veiculo=v, tipo=FrotaEvento.Tipo.CADASTRO).exists())

    def test_com_chip_vinculado_nao_exclui_e_o_dialogo_explica_antes(self):
        vinculos.vincular(self.chip(1), self.a, self.usuario, "teste")
        html = self.client.get(reverse("frota_veiculo_detalhe", args=[self.a.pk])).content.decode("utf-8")
        dialogo = re.search(r'id="frota-dlg-excluir".*?</form>', html, re.S).group(0)
        self.assertIn("Não dá para excluir agora", dialogo)
        self.assertIn("desvincule ou transfira antes", dialogo)
        r = self.ajax("frota_veiculo_status", self.a.pk, {"status": "desativado", "motivo": "x"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(FrotaVeiculo.objects.get(pk=self.a.pk).status, Status.ATIVO)


class MovimentacoesEHistoricoDaFrotaTests(EquipamentosBase):
    def setUp(self):
        super().setUp()
        frota.registrar_movimentacao(self.a, {"setor": "CACAU"}, self.usuario, motivo="safra de cacau")

    def test_movimentacoes_mostram_o_de_para_o_motivo_e_quem_registrou(self):
        html = self.html(self.get("frota_equip_movimentacoes"))
        self.sem_lixo_de_template(html, "movimentações")
        for esperado in ("Movimentações", "8110001", "TRATOR NH 3880", "Setor", "GRÃOS", "CACAU", "safra de cacau", "Em manutenção"):
            self.assertIn(esperado, html)

    def test_movimentacoes_nao_trazem_o_cadastro_nem_a_edicao_que_so_corrige(self):
        frota.editar_veiculo(self.b, {"nome": "VW 17.210 CORRIGIDO"}, self.usuario)
        eventos = self.get("frota_equip_movimentacoes").context["eventos"]
        self.assertEqual({e["tipo"] for e in eventos}, {"movimentacao", "status"})

    def test_historico_traz_tudo(self):
        eventos = self.get("frota_equip_historico").context["eventos"]
        self.assertEqual({e["tipo"] for e in eventos}, {"cadastro", "movimentacao", "status"})
        self.assertGreater(len(eventos), len(self.get("frota_equip_movimentacoes").context["eventos"]))

    def test_filtros(self):
        self.assertEqual(len(self.get("frota_equip_movimentacoes", tipo="situacao").context["eventos"]), 1)
        self.assertEqual(len(self.get("frota_equip_movimentacoes", q="8110001").context["eventos"]), 1)
        self.assertEqual(len(self.get("frota_equip_movimentacoes", natureza="terceiro").context["eventos"]), 0)
        self.assertEqual(len(self.get("frota_equip_historico", tipo="cadastro").context["eventos"]), 3)
        self.assertIn("Nenhum registro para os filtros aplicados", self.html(self.get("frota_equip_movimentacoes", q="nao-existe")))

    def test_exportacao_em_excel_so_com_a_permissao(self):
        r = self.get("frota_equip_movimentacoes", formato="xlsx")
        self.assertEqual(r.status_code, 200)
        linhas = list(load_workbook(io.BytesIO(r.content))["Movimentações"].iter_rows(values_only=True))
        self.assertEqual(linhas[0][5], "Evento")
        self.assertTrue(any(l[7] == "GRÃOS" and l[8] == "CACAU" for l in linhas[1:]))
        so_historico = self.cliente_de(self.usuario_com("so_historico", "ver_historico_frota"))
        self.assertEqual(so_historico.get(reverse("frota_equip_movimentacoes"), {"formato": "xlsx"}).status_code, 403)
        self.assertEqual(so_historico.get(reverse("frota_equip_movimentacoes")).status_code, 200)

    def test_sem_a_permissao_de_historico_e_negado(self):
        so_ve = self.cliente_de(self.usuario_com("so_ve_frota", "ver_frota"))
        for nome in ("frota_equip_movimentacoes", "frota_equip_historico"):
            self.assertEqual(so_ve.get(reverse(nome)).status_code, 403, nome)

    def test_o_detalhe_leva_para_as_movimentacoes_da_frota_filtradas_pelo_equipamento(self):
        html = self.client.get(reverse("frota_veiculo_detalhe", args=[self.a.pk])).content.decode("utf-8")
        self.assertIn(reverse("frota_equip_movimentacoes") + "?q=8110001", html)

    def test_a_lista_nao_faz_consulta_por_evento(self):
        for n in range(25):
            frota.registrar_movimentacao(self.a, {"setor": f"S{n}"}, self.usuario, motivo=f"m{n}")
        self.get("frota_equip_historico")
        with CaptureQueriesContext(connection) as poucas:
            self.get("frota_equip_historico")
        for n in range(25, 50):
            frota.registrar_movimentacao(self.a, {"setor": f"S{n}"}, self.usuario, motivo=f"m{n}")
        with CaptureQueriesContext(connection) as muitas:
            self.get("frota_equip_historico")
        self.assertEqual(len(poucas), len(muitas))
