"""
Frotas e Maquinários — as telas: a principal (indicadores, pesquisa, filtros e listagem), o detalhe em abas, o
formulário, as categorias e classes, o assistente de importação, a composição tecnológica, as movimentações, o
histórico e os relatórios.

Os serviços por trás estão em `test_frota_equipamentos.py`, `test_frota_classificacao.py` e
`test_frota_equipamento_import.py`; aqui se prova o que a PESSOA vê e consegue fazer — e o que NÃO pode, por permissão.
"""
import io
import re
from datetime import date

from django.core.cache import cache
from django.urls import reverse
from openpyxl import load_workbook

from ProjetoEstoque.models import (
    FrotaCentroCusto,
    FrotaClasseOperacional,
    FrotaEmpresa,
    FrotaEvento,
    FrotaNatureza,
    FrotaTipo,
    FrotaVeiculo,
)
from ProjetoEstoque.tests.test_m2m_telas import TelasBase
from services import frota_service as frota

Status = FrotaVeiculo.Status


class EquipamentosBase(TelasBase):
    """Um admin logado (de `TelasBase`) e uma frota pequena que cobre as distinções da tela."""

    def setUp(self):
        super().setUp()
        self.trator = FrotaTipo.objects.get(nome="Trator")
        self.caminhao = FrotaTipo.objects.get(nome="Caminhão")
        self.moto = FrotaTipo.objects.create(nome="Motocicleta", espera_placa=True, espera_chassi=True)
        self.santa = FrotaEmpresa.objects.create(codigo_erp="1", nome="SANTA COLOMBA", natureza_padrao="proprio")
        self.terceiros = FrotaEmpresa.objects.create(codigo_erp="2", nome="TERCEIROS", natureza_padrao="terceiro")
        self.classe = FrotaClasseOperacional.objects.create(codigo_erp="34", nome="TRATOR LEVE", categoria=self.trator)
        self.centro = FrotaCentroCusto.objects.create(codigo_erp="32107", nome="TRATOR LEVE - GRAOS")
        self.a = frota.criar_veiculo({
            "nome": "TRATOR NH 3880", "tipo": self.trator.pk, "codigo_legado": "8110001", "modelo": "TRATOR NH 3880",
            "empresa": self.santa.pk, "natureza": "proprio", "classe_operacional": self.classe.pk, "centro_custo": self.centro.pk,
            "setor": "GRÃOS", "ano_fabricacao": 2011, "ano_modelo": 2011, "placa": "C811009", "chassi_serie": "1CQS660ALH0120143",
            "data_compra": date(2018, 8, 15),
        }, self.usuario)
        self.b = frota.criar_veiculo({
            "nome": "VW 17.210", "tipo": self.caminhao.pk, "codigo_legado": "9100592", "modelo": "VW 17.210",
            "empresa": self.terceiros.pk, "natureza": "terceiro", "ano_fabricacao": 2026, "ano_modelo": 2027, "placa": "VAY0B31",
            "chassi_serie": "953678TG8VR016233",
        }, self.usuario)
        self.c = frota.criar_veiculo({
            "nome": "HONDA NXR BROS 160", "tipo": self.moto.pk, "codigo_legado": "8720079", "modelo": "NXR BROS 160",
            "empresa": self.santa.pk, "natureza": "proprio", "ano_fabricacao": 2024,
        }, self.usuario)
        frota.mudar_status(self.c, Status.MANUTENCAO, self.usuario, "revisão")
        FrotaVeiculo.objects.filter(pk__in=[self.a.pk, self.c.pk]).update(validacao_pendente=True)

    def codigos_da_lista(self, resposta):
        return [v.codigo_legado for v in resposta.context["veiculos"]]


class TelaPrincipalTests(EquipamentosBase):
    def test_renderiza_com_o_cabecalho_os_indicadores_e_a_tabela(self):
        html = self.html(self.get("frota_visao_geral"))
        self.sem_lixo_de_template(html, "frota_visao_geral")
        self.assertIn("Frotas e Maquinários", html)
        self.assertIn("Gerencie veículos, máquinas e equipamentos motorizados da operação.", html)
        self.assertIn('aria-label="Você está em"', html)
        for rotulo in ("Total de equipamentos", "Próprios", "Terceiros", "Em uso", "Em manutenção", "Sem identificação completa"):
            self.assertIn(rotulo, html)
        for coluna in ("Código", "Equipamento", "Classe", "Modelo", "Empresa", "Setor", "Placa", "Ano", "Situação"):
            self.assertRegex(html, rf'class="agro-ordem"[^>]*>{coluna}<')

    def test_os_indicadores_saem_da_base_cadastrada(self):
        k = self.get("frota_visao_geral").context["kpis"]
        self.assertEqual((k["total"], k["proprios"], k["terceiros"], k["em_uso"], k["em_manutencao"]), (3, 2, 1, 2, 1))
        self.assertEqual(k["sem_identificacao"], 1)                 # só a moto, que espera placa e chassi
        self.assertEqual(k["em_uso_a_validar"], 1)

    def test_as_colunas_secundarias_ficam_marcadas_para_o_tablet_e_continuam_no_html(self):
        """No tablet o CSS esconde Classe, Modelo, Setor e Ano (`.agro-sec`) para que Situação e Ações não saiam da tela; o HTML
        entrega todas, porque no celular a linha vira cartão e mostra tudo (e a ficha traz o resto)."""
        html = self.html(self.get("frota_visao_geral"))
        cabecalho = html.split("<thead>")[1].split("</thead>")[0]
        for rotulo in ("Classe", "Modelo", "Setor", "Ano"):
            self.assertRegex(cabecalho, rf'<th class="agro-sec"[^>]*>\s*<a[^>]*>{rotulo}<', rotulo)
            self.assertRegex(html, rf'<td class="[^"]*agro-sec[^"]*" data-rotulo="{rotulo}"|<td data-rotulo="{rotulo}" class="[^"]*agro-sec', rotulo)
        for rotulo in ("Código", "Equipamento", "Empresa", "Placa", "Situação"):
            self.assertNotRegex(cabecalho, rf'<th class="agro-sec"[^>]*>\s*<a[^>]*>{rotulo}<', rotulo)     # o essencial nunca se esconde

    def test_as_acoes_do_cabecalho_dependem_da_permissao(self):
        html = self.html(self.get("frota_visao_geral"))
        self.assertIn(reverse("frota_veiculo_novo"), html)
        so_ve = self.cliente_de(self.usuario_com("so_ve", "ver_frota"))
        html = so_ve.get(reverse("frota_visao_geral")).content.decode("utf-8")
        self.assertNotIn(reverse("frota_veiculo_novo"), html)
        self.assertNotIn("Importar planilha", html)
        self.assertNotIn("Exportar", html)

    def test_cada_linha_mostra_os_dados_principais(self):
        html = self.html(self.get("frota_visao_geral"))
        linha = re.search(r'<tr class="m2m-linha agro-linha"[^>]*data-veiculo-id="%d".*?</tr>' % self.a.pk, html, re.S).group(0)
        for esperado in ("8110001", self.a.codigo, "TRATOR NH 3880", "TRATOR LEVE", "SANTA COLOMBA", "Próprio", "GRÃOS", "C811009", "2011", "Em uso", "a validar"):
            self.assertIn(esperado, linha)
        self.assertIn(reverse("frota_veiculo_detalhe", args=[self.a.pk]), linha)
        outra = re.search(r'<tr class="m2m-linha agro-linha"[^>]*data-veiculo-id="%d".*?</tr>' % self.b.pk, html, re.S).group(0)
        self.assertIn("Terceiro", outra)
        self.assertIn("2026/2027", outra)
        self.assertNotIn("a validar", outra)

    def test_sem_permissao_de_ver_a_tela_e_negada(self):
        self.assertEqual(self.cliente_de(self.usuario_com("sem_nada")).get(reverse("frota_visao_geral")).status_code, 403)

    def test_quem_so_ve_chips_vai_para_a_conectividade(self):
        c = self.cliente_de(self.usuario_com("so_chips", "ver_chips"))
        self.assertRedirects(c.get(reverse("frota_visao_geral")), reverse("m2m_visao_geral"), fetch_redirect_response=False)

class BaseVaziaTests(TelasBase):
    """Sem nenhum equipamento: a tela não mostra tabela vazia nem indicador inventado, convida a começar."""

    def test_convida_a_importar_a_planilha_ou_cadastrar(self):
        html = self.html(self.get("frota_visao_geral"))
        self.assertIn("Nenhum equipamento cadastrado", html)
        self.assertIn(reverse("frota_veiculo_novo"), html)
        self.assertNotIn('class="m2m-tabela agro-tabela"', html)

    def test_os_indicadores_da_base_vazia_sao_zero_e_nao_ha_nada_a_exportar_ou_validar(self):
        resposta = self.get("frota_visao_geral")
        k = resposta.context["kpis"]
        self.assertEqual((k["total"], k["proprios"], k["terceiros"], k["em_uso"], k["em_manutencao"], k["sem_identificacao"], k["a_validar"]), (0,) * 7)
        html = resposta.content.decode("utf-8")
        self.assertNotIn("Exportar", html)
        self.assertNotIn("Validar selecionados", html)


class EscoposTests(EquipamentosBase):
    def test_proprios_e_terceiros_sao_filtros_sobre_a_mesma_base(self):
        todos = self.codigos_da_lista(self.get("frota_visao_geral"))
        proprios = self.codigos_da_lista(self.get("frota_equipamentos_proprios"))
        terceiros = self.codigos_da_lista(self.get("frota_equipamentos_terceiros"))
        self.assertEqual(sorted(todos), ["8110001", "8720079", "9100592"])
        self.assertEqual(sorted(proprios), ["8110001", "8720079"])
        self.assertEqual(terceiros, ["9100592"])

    def test_a_rota_de_antes_abre_a_mesma_tela(self):
        resposta = self.get("frota_veiculos")
        self.assertEqual(len(resposta.context["veiculos"]), 3)
        self.assertTemplateUsed(resposta, "front/agro/frota/equipamentos.html")

    def test_o_caminho_do_alto_diz_onde_se_esta(self):
        html = self.html(self.get("frota_equipamentos_terceiros"))
        self.assertRegex(html, r'Zelo Agro</a></li>\s*<li><a href="[^"]*">Frotas e Maquinários</a></li>\s*<li><span aria-current="page">Terceiros</span></li>')
        self.assertIn("Não entram na conta do patrimônio próprio", html)

    def test_na_rota_de_terceiros_a_natureza_nao_e_um_filtro_a_escolher(self):
        html = self.html(self.get("frota_equipamentos_terceiros", natureza="proprio"))
        self.assertNotIn('name="natureza"', html)                                   # a rota manda: o parâmetro não vence
        self.assertEqual(self.html and [v.codigo_legado for v in self.get("frota_equipamentos_terceiros", natureza="proprio").context["veiculos"]], ["9100592"])

    def test_os_filtros_rapidos_levam_a_contagem_de_cada_um(self):
        segmentos = {s["rotulo"]: s for s in self.get("frota_visao_geral").context["segmentos"]}
        self.assertEqual({r: s["n"] for r, s in segmentos.items()}, {"Todos": 3, "Próprios": 2, "Terceiros": 1, "Em uso": 2, "Em manutenção": 1})
        self.assertTrue(segmentos["Todos"]["ativo"])
        self.assertEqual(segmentos["Terceiros"]["url"], reverse("frota_equipamentos_terceiros"))
        em_uso = self.get("frota_visao_geral", status="ativo")
        self.assertEqual(sorted(self.codigos_da_lista(em_uso)), ["8110001", "9100592"])
        self.assertTrue({s["rotulo"]: s for s in em_uso.context["segmentos"]}["Em uso"]["ativo"])

    def test_o_filtro_de_situacao_vale_dentro_do_escopo(self):
        resposta = self.get("frota_equipamentos_proprios", status="manutencao")
        self.assertEqual(self.codigos_da_lista(resposta), ["8720079"])
        url = {s["rotulo"]: s["url"] for s in resposta.context["segmentos"]}["Terceiros"]
        self.assertIn("status=manutencao", url)                      # trocar de escopo mantém a situação


class PesquisaEFiltrosTests(EquipamentosBase):
    def lista(self, **params):
        return self.codigos_da_lista(self.get("frota_visao_geral", **params))

    def test_busca_por_codigo_original_interno_nome_modelo_placa_chassi_e_centro_de_custo(self):
        casos = {
            "8110001": ["8110001"], self.a.codigo: ["8110001"], "nh 3880": ["8110001"], "17.210": ["9100592"], "VAY0B31": ["9100592"],
            "vay-0b31": ["9100592"], "1CQS660": ["8110001"], "32107": ["8110001"], "trator leve - graos": ["8110001"], "bros": ["8720079"],
        }
        for termo, esperado in casos.items():
            with self.subTest(busca=termo):
                self.assertEqual(self.lista(q=termo), esperado)

    def test_busca_sem_acento_e_sem_caixa(self):
        FrotaVeiculo.objects.filter(pk=self.a.pk).update(modelo="CAMINHÃO ROLL OFF")
        self.assertEqual(self.lista(q="caminhao roll"), ["8110001"])

    def test_filtros_avancados(self):
        self.assertEqual(self.lista(classe_operacional=self.classe.pk), ["8110001"])
        self.assertEqual(self.lista(centro_custo=self.centro.pk), ["8110001"])
        self.assertEqual(self.lista(empresa=self.terceiros.pk), ["9100592"])
        self.assertEqual(self.lista(setor="grãos"), ["8110001"])
        self.assertEqual(self.lista(ano_modelo=2027), ["9100592"])
        self.assertEqual(self.lista(ano_fabricacao=2024), ["8720079"])
        self.assertEqual(self.lista(tipo=self.moto.pk), ["8720079"])
        self.assertEqual(sorted(self.lista(validacao="pendente")), ["8110001", "8720079"])
        self.assertEqual(self.lista(validacao="validado"), ["9100592"])
        self.assertEqual(self.lista(pendencia="identificacao"), ["8720079"])
        self.assertEqual(self.lista(pendencia="sem_classe").sort() or sorted(self.lista(pendencia="sem_classe")), ["8720079", "9100592"])

    def test_o_painel_de_filtros_abre_quando_ha_filtro_avancado_em_uso(self):
        self.assertFalse(self.get("frota_visao_geral").context["filtros_avancados_abertos"])
        self.assertTrue(self.get("frota_visao_geral", empresa=self.santa.pk).context["filtros_avancados_abertos"])
        html = self.html(self.get("frota_visao_geral", empresa=self.santa.pk))
        self.assertRegex(html, r'id="m2m-filtros-painel"\s*>')                      # sem o atributo `hidden`

    def test_os_filtros_em_uso_viram_etiquetas_que_se_removem(self):
        resposta = self.get("frota_visao_geral", empresa=self.santa.pk, q="trator")
        rotulos = {f["rotulo"]: f["remover"] for f in resposta.context["filtros_ativos"]}
        self.assertEqual(set(rotulos), {"Empresa: SANTA COLOMBA", "Busca: trator"})
        self.assertNotIn("empresa=", rotulos["Empresa: SANTA COLOMBA"])
        self.assertIn("q=trator", rotulos["Empresa: SANTA COLOMBA"])

    def test_filtro_sem_resultado_oferece_limpar(self):
        html = self.html(self.get("frota_visao_geral", q="inexistente-xyz"))
        self.assertIn("Nenhum equipamento para os filtros aplicados", html)
        self.assertIn("Limpar filtros", html)

    def test_as_opcoes_dos_selects_sao_so_o_que_existe(self):
        opcoes = self.get("frota_visao_geral").context["opcoes"]
        self.assertEqual([n for _, n in opcoes["classes_operacionais"]], ["TRATOR LEVE"])
        self.assertEqual([n for _, n in opcoes["centros_de_custo"]], ["32107 · TRATOR LEVE - GRAOS"])
        self.assertEqual(opcoes["setores"], [("GRÃOS", "GRÃOS")])
        self.assertEqual([a for a, _ in opcoes["anos_fabricacao"]], [2026, 2024, 2011])


class OrdenacaoEPaginacaoTests(EquipamentosBase):
    def test_ordem_padrao_e_pelo_codigo_do_erp(self):
        self.assertEqual(self.codigos_da_lista(self.get("frota_visao_geral")), ["8110001", "8720079", "9100592"])

    def test_clicar_no_cabecalho_ordena_e_clicar_de_novo_inverte(self):
        resposta = self.get("frota_visao_geral", ordem="ano", dir="asc")
        self.assertEqual(self.codigos_da_lista(resposta), ["8110001", "8720079", "9100592"])
        cabecalho = {c["chave"]: c for c in resposta.context["cabecalhos"]}["ano"]
        self.assertEqual((cabecalho["sentido"], cabecalho["aria"]), ("asc", "ascending"))
        self.assertIn("dir=desc", cabecalho["url"])
        invertida = self.get("frota_visao_geral", ordem="ano", dir="desc")
        self.assertEqual(self.codigos_da_lista(invertida)[0], "9100592")

    def test_so_o_cabecalho_ativo_mostra_o_sentido(self):
        resposta = self.get("frota_visao_geral", ordem="placa")
        sentidos = {c["chave"]: c["sentido"] for c in resposta.context["cabecalhos"]}
        self.assertEqual({k: v for k, v in sentidos.items() if v}, {"placa": "asc"})

    def test_ordem_ou_sentido_invalidos_caem_no_padrao(self):
        resposta = self.get("frota_visao_geral", ordem="'; drop table", dir="x")
        self.assertEqual(self.codigos_da_lista(resposta), ["8110001", "8720079", "9100592"])

    def test_ordenar_mantem_os_filtros_e_volta_a_pagina_1(self):
        c = {c["chave"]: c["url"] for c in self.get("frota_visao_geral", empresa=self.santa.pk, page="2").context["cabecalhos"]}
        self.assertIn(f"empresa={self.santa.pk}", c["nome"])
        self.assertNotIn("page=", c["nome"])

    def test_por_pagina_aceita_so_as_opcoes_da_tela(self):
        self.assertEqual(self.get("frota_visao_geral").context["por_pagina"], 25)
        self.assertEqual(self.get("frota_visao_geral", por_pagina="50").context["por_pagina"], 50)
        self.assertEqual(self.get("frota_visao_geral", por_pagina="7").context["por_pagina"], 25)
        self.assertEqual(self.get("frota_visao_geral", por_pagina="abc").context["por_pagina"], 25)

    def test_pagina_corta_a_lista_e_conserva_o_tamanho_nos_links(self):
        for n in range(30):
            frota.criar_veiculo({"nome": f"TRATOR {n}", "tipo": self.trator.pk, "codigo_legado": f"900{n:04d}"}, self.usuario)
        resposta = self.get("frota_visao_geral", por_pagina="25")
        self.assertEqual((len(resposta.context["veiculos"]), resposta.context["page_obj"].paginator.num_pages), (25, 2))
        self.assertIn("por_pagina=25", resposta.context["querystring"])
        segunda = self.get("frota_visao_geral", por_pagina="25", page="2")
        self.assertEqual(len(segunda.context["veiculos"]), 8)

    def test_a_lista_nao_faz_consulta_por_linha(self):
        for n in range(40):
            frota.criar_veiculo({
                "nome": f"T{n}", "tipo": self.trator.pk, "codigo_legado": f"901{n:04d}", "empresa": self.santa.pk,
                "classe_operacional": self.classe.pk, "centro_custo": self.centro.pk,
            }, self.usuario)
        self.get("frota_visao_geral")                                 # aquece sessão e cache
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as poucos:
            self.get("frota_visao_geral", por_pagina="25")
        with CaptureQueriesContext(connection) as muitos:
            self.get("frota_visao_geral", por_pagina="50")
        self.assertEqual(len(poucos), len(muitos))                    # 25 ou 43 equipamentos: o mesmo número de consultas


class ExportacaoTests(EquipamentosBase):
    def test_exporta_o_recorte_da_tela_em_excel(self):
        resposta = self.get("frota_equipamentos_proprios", formato="xlsx")
        self.assertEqual(resposta.status_code, 200)
        self.assertIn("spreadsheetml", resposta["Content-Type"])
        self.assertRegex(resposta["Content-Disposition"], r'attachment; filename="equipamentos_\d{8}\.xlsx"')
        wb = load_workbook(io.BytesIO(resposta.content))
        self.assertEqual(wb.sheetnames, ["Inventário", "Resumo"])
        linhas = list(wb["Inventário"].iter_rows(values_only=True))
        self.assertEqual(linhas[0][0], "Código original (ERP)")
        self.assertEqual({l[0] for l in linhas[1:]}, {"8110001", "8720079"})            # só os próprios
        self.assertEqual(linhas[1][19 - 1], 2011)                                          # ano de fabricação como número

    def test_a_exportacao_leva_todas_as_informacoes_do_cadastro(self):
        wb = load_workbook(io.BytesIO(self.get("frota_visao_geral", q="8110001", formato="xlsx").content))
        cabecalho = [c.value for c in wb["Inventário"][1]]
        linha = dict(zip(cabecalho, [c.value for c in wb["Inventário"][2]]))
        self.assertEqual((linha["Chassi / nº de série"], linha["Classe operacional"], linha["Centro de custo (código)"]), ("1CQS660ALH0120143", "TRATOR LEVE", "32107"))
        self.assertEqual((linha["Natureza"], linha["Situação"], linha["Validação do inventário"]), ("Próprio", "Em uso", "A validar"))

    def test_sem_a_permissao_de_exportar_e_negado(self):
        so_ve = self.cliente_de(self.usuario_com("so_ve", "ver_frota"))
        self.assertEqual(so_ve.get(reverse("frota_visao_geral"), {"formato": "xlsx"}).status_code, 403)
        com = self.cliente_de(self.usuario_com("exporta", "ver_frota", "exportar_frota"))
        self.assertEqual(com.get(reverse("frota_visao_geral"), {"formato": "xlsx"}).status_code, 200)
        self.assertIn("Exportar", com.get(reverse("frota_visao_geral")).content.decode("utf-8"))

    def test_o_excel_nao_executa_formula(self):
        FrotaVeiculo.objects.filter(pk=self.a.pk).update(nome="=1+1", observacoes="@SOMA(A1)")
        wb = load_workbook(io.BytesIO(self.get("frota_visao_geral", q="8110001", formato="xlsx").content))
        valores = [c.value for c in wb["Inventário"][2] if isinstance(c.value, str)]
        self.assertIn("'=1+1", valores)
        self.assertIn("'@SOMA(A1)", valores)


class ValidacaoEmLoteTests(EquipamentosBase):
    def test_validar_em_lote_confirma_so_os_marcados_e_audita_cada_um(self):
        resposta = self.client.post(reverse("frota_equipamentos_validar_lote"), {"ids": [self.a.pk, self.c.pk], "voltar": reverse("frota_visao_geral")})
        self.assertRedirects(resposta, reverse("frota_visao_geral"), fetch_redirect_response=False)
        self.assertFalse(FrotaVeiculo.objects.filter(validacao_pendente=True).exists())
        self.assertEqual(FrotaEvento.objects.filter(tipo=FrotaEvento.Tipo.VALIDACAO).count(), 2)
        self.assertEqual(FrotaEvento.objects.get(veiculo=self.a, tipo=FrotaEvento.Tipo.VALIDACAO).usuario, self.admin)

    def test_so_quem_pode_validar_o_inventario(self):
        sem = self.cliente_de(self.usuario_com("sem_validar", "ver_frota", "editar_frota"))
        self.assertEqual(sem.post(reverse("frota_equipamentos_validar_lote"), {"ids": [self.a.pk]}).status_code, 403)
        self.assertTrue(FrotaVeiculo.objects.get(pk=self.a.pk).validacao_pendente)
        com = self.cliente_de(self.usuario_com("validador", "ver_frota", "validar_inventario_frota"))
        self.assertEqual(com.post(reverse("frota_equipamentos_validar_lote"), {"ids": [self.a.pk]}).status_code, 302)
        self.assertFalse(FrotaVeiculo.objects.get(pk=self.a.pk).validacao_pendente)

    def test_get_nao_valida(self):
        self.assertEqual(self.client.get(reverse("frota_equipamentos_validar_lote")).status_code, 405)

    def test_ja_validado_ou_vazio_nao_quebra(self):
        r = self.client.post(reverse("frota_equipamentos_validar_lote"), {"ids": [self.b.pk]}, follow=True)
        self.assertEqual(r.status_code, 200)
        r = self.client.post(reverse("frota_equipamentos_validar_lote"), {}, follow=True)
        self.assertEqual(r.status_code, 200)

    def test_a_caixa_de_marcar_so_aparece_para_quem_pode_validar_e_so_no_que_esta_pendente(self):
        html = self.html(self.get("frota_visao_geral"))
        self.assertIn("data-agro-marcar-todos", html)
        self.assertEqual(len(re.findall(r'name="ids"', html)), 2)                       # a e c; b já está validado
        so_ve = self.cliente_de(self.usuario_com("so_ve2", "ver_frota"))
        self.assertNotIn('name="ids"', so_ve.get(reverse("frota_visao_geral")).content.decode("utf-8"))
        cache.clear()


class ExcluirNaListaTests(EquipamentosBase):
    """O botão "Excluir" da lista (terceiros, próprios e geral): excluir é DESATIVAR — o equipamento sai da lista, do
    Excel e das contagens, continua em "Excluídos" e pode ser restaurado; chip vinculado impede; o que decide é o servidor."""

    def lista(self, nome="frota_equipamentos_terceiros", **params):
        return self.get(nome, **params)

    def excluir(self, veiculo, motivo="vendido", cliente=None):
        return (cliente or self.client).post(
            reverse("frota_veiculo_status", args=[veiculo.pk]),
            {"status": "desativado", "motivo": motivo, "voltar": reverse("frota_equipamentos_terceiros")},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

    def test_cada_linha_tem_o_botao_excluir_com_a_rota_do_status_e_a_da_verificacao(self):
        html = self.html(self.lista())
        self.assertIn('data-frota-abrir="excluir_lista"', html)
        self.assertIn(f'data-url="{reverse("frota_veiculo_status", args=[self.b.pk])}"', html)
        self.assertIn(f'data-verificar="{reverse("frota_veiculo_exclusao", args=[self.b.pk])}"', html)
        self.assertIn('id="frota-dlg-excluir_lista"', html)
        self.assertIn('id="frota-dlg-restaurar_lista"', html)
        self.sem_lixo_de_template(html, "terceiros")

    def test_quem_so_ve_nao_recebe_o_botao_nem_os_dialogos(self):
        cliente = self.cliente_de(self.usuario_com("so_ve_excluir", "ver_frota"))
        html = cliente.get(reverse("frota_equipamentos_terceiros")).content.decode("utf-8")
        self.assertNotIn("excluir_lista", html)
        self.assertNotIn("restaurar_lista", html)
        self.assertEqual(cliente.post(reverse("frota_veiculo_status", args=[self.b.pk]), {"status": "desativado"}).status_code, 403)
        self.assertEqual(cliente.get(reverse("frota_veiculo_exclusao", args=[self.b.pk])).status_code, 403)

    def test_a_verificacao_diz_o_que_impede_e_o_que_fica_guardado(self):
        dados = self.client.get(reverse("frota_veiculo_exclusao", args=[self.b.pk])).json()
        self.assertTrue(dados["pode"])
        self.assertEqual((dados["codigo"], dados["chips_ativos"], dados["equipamentos_instalados"]), ("9100592", 0, 0))
        self.assertGreaterEqual(dados["historico"]["eventos"], 1)
        chip = self.chip_do_veiculo(self.b)
        impedido = self.client.get(reverse("frota_veiculo_exclusao", args=[self.b.pk])).json()
        self.assertFalse(impedido["pode"])
        self.assertEqual(impedido["chips_ativos"], 1)
        self.assertIn("1 chip vinculado", impedido["impedimentos"][0])
        self.assertTrue(chip)

    def chip_do_veiculo(self, veiculo):
        from services import m2m_monitoramento_service as m2m
        from ProjetoEstoque.tests import test_m2m as base
        return m2m.criar_chip({"nome": "Chip do terceiro", "iccid": base.iccid_valido(77), "veiculo": veiculo.pk}, self.usuario)

    def test_excluir_tira_da_lista_do_excel_e_das_contagens_e_registra_no_historico(self):
        antes = self.html(self.lista())
        self.assertIn("9100592", antes)
        r = self.excluir(self.b)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.b.refresh_from_db()
        self.assertEqual(self.b.status, Status.DESATIVADO)
        evento = FrotaEvento.objects.filter(veiculo=self.b, tipo=FrotaEvento.Tipo.STATUS).latest("pk")
        self.assertEqual((evento.dados["para"], evento.dados["motivo"]), ("desativado", "vendido"))
        depois = self.lista()
        self.assertNotIn("9100592", self.html(depois))
        self.assertEqual(depois.context["total_filtrado"], 0)
        segmentos = {s["rotulo"]: s for s in depois.context["segmentos"]}
        self.assertEqual(segmentos["Terceiros"]["n"], 0)
        self.assertEqual(segmentos["Excluídos"]["n"], 1)
        self.assertEqual(segmentos["Todos"]["n"], 2)                                  # a e c: o excluído não conta
        planilha = load_workbook(io.BytesIO(self.lista(formato="xlsx").content), read_only=True)
        celulas = [str(c) for linha in planilha.active.iter_rows(values_only=True) for c in linha if c]
        self.assertNotIn("9100592", celulas)

    def test_o_excluido_continua_acessivel_em_excluidos_e_volta_pelo_restaurar(self):
        self.excluir(self.b)
        r = self.lista(status="desativado")
        self.assertEqual(self.codigos_da_lista(r), ["9100592"])
        html = self.html(r)
        self.assertIn('data-frota-abrir="restaurar_lista"', html)
        self.assertNotIn('data-frota-abrir="excluir_lista"', html)
        restaurar = self.client.post(
            reverse("frota_veiculo_status", args=[self.b.pk]),
            {"status": "ativo", "motivo": "excluído por engano"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertTrue(restaurar.json()["ok"])
        self.assertIn("9100592", self.html(self.lista()))
        self.assertEqual(self.get("frota_equipamentos_terceiros").context["segmentos"][2]["n"], 1)

    def test_o_servidor_recusa_excluir_com_chip_vinculado_mesmo_sem_o_dialogo(self):
        self.chip_do_veiculo(self.b)
        r = self.excluir(self.b)
        self.assertEqual(r.status_code, 400)
        self.assertIn("chip", r.json()["mensagem"])
        self.b.refresh_from_db()
        self.assertEqual(self.b.status, Status.ATIVO)
        self.assertIn("9100592", self.html(self.lista()))

    def test_a_lista_geral_tambem_esconde_o_excluido_e_a_situacao_desativado_o_mostra(self):
        self.excluir(self.b)
        self.assertNotIn("9100592", self.html(self.get("frota_visao_geral")))
        self.assertIn("9100592", self.html(self.get("frota_visao_geral", status="desativado")))

    def test_a_verificacao_nao_vai_para_o_cache_do_navegador(self):
        r = self.client.get(reverse("frota_veiculo_exclusao", args=[self.b.pk]))
        self.assertEqual(r["Cache-Control"], "no-store")
        self.assertEqual(self.client.post(reverse("frota_veiculo_exclusao", args=[self.b.pk])).status_code, 405)
