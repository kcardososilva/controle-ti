"""
Lista de ativos (`/equipamentos/`): filtro de Área de operação e tela enxuta.

Pedido de 15/11/2026: (1) filtrar por "Área Produtiva" e "Área Não Produtiva" e
(2) deixar a tela mais minimalista.

O que falha em silêncio aqui:

1. **NULL e "" são o MESMO estado** (regra 47a: vazio é "não classificado"). Um
   filtro "Sem área" que olhasse só `isnull` deixaria de fora tudo o que foi
   gravado com string vazia, e o gestor concluiria que já classificou tudo.
2. O filtro mora em `_aplicar_filtros_itens`, a fonte única que a EXPORTAÇÃO
   também usa. Se alguém o mover para a view da lista, a planilha passa a
   exportar o parque inteiro com a tela mostrando só uma área.
3. `form.reset()` do navegador devolve o rádio ao valor que o servidor
   renderizou — por isso "Limpar" marca "Todas" à mão (ver o JS). Aqui só se
   confere o lado do servidor: o rádio marcado SEGUE a URL.
4. Valor desconhecido (`?area=lixo`) é ignorado, não vira lista vazia.
"""
import io
import re
from decimal import Decimal
from datetime import date

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from ProjetoEstoque.models import (
    AreaOperacionalChoices,
    Categoria,
    Fornecedor,
    Item,
    ItemLote,
    LoteEstoque,
    StatusItemChoices,
    Subtipo,
)


class _Base(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_superuser("areafiltro", "a@a.com", "x")
        self.client.force_login(self.user)
        cat = Categoria.objects.create(
            nome="Rede", criado_por=self.user, atualizado_por=self.user
        )
        self.switch = Subtipo.objects.create(
            nome="Switches", alocado="nao", categoria=cat,
            criado_por=self.user, atualizado_por=self.user,
        )
        self.antena = Subtipo.objects.create(
            nome="Antenas", alocado="nao", categoria=cat,
            criado_por=self.user, atualizado_por=self.user,
        )

    def _item(self, nome, area=None, subtipo=None, **extra):
        return Item.objects.create(
            nome=nome, subtipo=subtipo or self.switch, area_operacional=area,
            criado_por=self.user, atualizado_por=self.user, **extra,
        )

    def _partial(self, **params):
        """O ramo AJAX da lista: é o que o JS usa a cada filtro."""
        resp = self.client.get(
            reverse("equipamentos_list"), params, HTTP_X_REQUESTED_WITH="XMLHttpRequest"
        )
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def _pagina(self, **params):
        resp = self.client.get(reverse("equipamentos_list"), params)
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode("utf-8")


class FiltroDeAreaTests(_Base):
    def setUp(self):
        super().setUp()
        self._item("Switch Alfa", AreaOperacionalChoices.PRODUTIVA)
        self._item("Switch Beta", AreaOperacionalChoices.NAO_PRODUTIVA)
        self._item("Switch Gama", None)
        self._item("Switch Delta", "")          # vazio gravado como string

    def _nomes_presentes(self, **params):
        tbody = self._partial(**params)["tbody"]
        return {n for n in ("Alfa", "Beta", "Gama", "Delta") if f"Switch {n}" in tbody}

    def test_produtiva(self):
        self.assertEqual(self._nomes_presentes(area="produtiva"), {"Alfa"})

    def test_nao_produtiva(self):
        self.assertEqual(self._nomes_presentes(area="nao_produtiva"), {"Beta"})

    def test_sem_area_junta_null_e_string_vazia(self):
        self.assertEqual(self._nomes_presentes(area="sem_area"), {"Gama", "Delta"})

    def test_sem_filtro_traz_tudo(self):
        self.assertEqual(self._nomes_presentes(), {"Alfa", "Beta", "Gama", "Delta"})
        self.assertEqual(self._nomes_presentes(area=""), {"Alfa", "Beta", "Gama", "Delta"})

    def test_valor_desconhecido_e_ignorado_e_nao_zera_a_lista(self):
        self.assertEqual(
            self._nomes_presentes(area="lixo"), {"Alfa", "Beta", "Gama", "Delta"}
        )

    def test_o_contador_acompanha_o_filtro(self):
        self.assertEqual(self._partial(area="produtiva")["count"], 1)
        self.assertEqual(self._partial(area="sem_area")["count"], 2)

    def test_combina_com_status(self):
        self._item("Switch Epsilon", AreaOperacionalChoices.PRODUTIVA,
                   status=StatusItemChoices.BACKUP)
        tbody = self._partial(area="produtiva", status="backup")["tbody"]
        self.assertIn("Switch Epsilon", tbody)
        self.assertNotIn("Switch Alfa", tbody)

    def test_combina_com_subtipo(self):
        self._item("Antena Zeta", AreaOperacionalChoices.PRODUTIVA, subtipo=self.antena)
        tbody = self._partial(area="produtiva", subtipo=self.antena.pk)["tbody"]
        self.assertIn("Antena Zeta", tbody)
        self.assertNotIn("Switch Alfa", tbody)

    def test_exportacao_respeita_a_area(self):
        """A planilha é exatamente o que a tela mostra (mesma `_aplicar_filtros_itens`)."""
        from openpyxl import load_workbook

        def nomes_da_planilha(**params):
            resp = self.client.get(reverse("equipamentos_exportar"), params)
            self.assertEqual(resp.status_code, 200)
            wb = load_workbook(io.BytesIO(resp.content))
            valores = {
                str(c.value)
                for ws in wb.worksheets
                for linha in ws.iter_rows()
                for c in linha
                if c.value is not None
            }
            return {n for n in ("Alfa", "Beta", "Gama", "Delta") if f"Switch {n}" in valores}

        self.assertEqual(nomes_da_planilha(area="produtiva"), {"Alfa"})
        self.assertEqual(nomes_da_planilha(area="sem_area"), {"Gama", "Delta"})
        self.assertEqual(nomes_da_planilha(), {"Alfa", "Beta", "Gama", "Delta"})


class BarraDeFiltrosTests(_Base):
    def test_quatro_radios_e_o_marcado_segue_a_url(self):
        for valor in ("", "produtiva", "nao_produtiva", "sem_area"):
            with self.subTest(area=valor):
                html = self._pagina(area=valor) if valor else self._pagina()
                # `type="radio"` de propósito: o `sizeForm` (itens por página) repete a
                # URL em campos ocultos, e um `name="area"` oculto também existe.
                self.assertEqual(html.count('type="radio" name="area"'), 4)
                marcado = re.findall(r'<input type="radio" name="area" value="([^"]*)" checked>', html)
                self.assertEqual(marcado, [valor])

    def test_area_invalida_deixa_todas_marcado(self):
        html = self._pagina(area="lixo")
        marcado = re.findall(r'<input type="radio" name="area" value="([^"]*)" checked>', html)
        self.assertEqual(marcado, [""])

    def test_rotulos_da_area(self):
        html = self._pagina()
        for rotulo in ("Todas", "Produtiva", "Não produtiva", "Sem área"):
            self.assertIn(f"<span>{rotulo}</span>", html)

    def _avancados_escondido(self, html):
        achado = re.search(r'<div class="eq-avancados" id="filtrosAvancados"( hidden)?>', html)
        self.assertIsNotNone(achado, "o painel 'Mais filtros' sumiu da página")
        return achado.group(1) is not None

    def test_mais_filtros_fechado_sem_filtro_avancado(self):
        self.assertTrue(self._avancados_escondido(self._pagina()))

    def test_busca_area_e_atalho_de_status_nao_abrem_o_painel(self):
        """Estão sempre à vista na barra: não há o que revelar."""
        html = self._pagina(nome="alfa", area="produtiva", status="ativo")
        self.assertTrue(self._avancados_escondido(html))

    def test_filtro_avancado_abre_o_painel_e_aparece_no_badge(self):
        html = self._pagina(subtipo=self.switch.pk, modelo="x")
        self.assertFalse(self._avancados_escondido(html))
        self.assertIn('id="filtrosBadgeText">2<', html)

    def test_status_fora_dos_atalhos_conta_como_filtro_escondido(self):
        """`reservado` só existe no select de 'Mais filtros': sem abrir o painel
        não haveria nenhum sinal de que a lista está filtrada por ele."""
        html = self._pagina(status="reservado")
        self.assertFalse(self._avancados_escondido(html))
        self.assertIn('id="filtrosBadgeText">1<', html)

    def test_limpar_so_aparece_com_filtro(self):
        sem = self._pagina()
        com = self._pagina(area="produtiva")
        self.assertRegex(sem, r'id="btnLimpar" class="eq-link" hidden')
        self.assertNotRegex(com, r'id="btnLimpar" class="eq-link" hidden')

    def test_atalho_de_status_marcado_vem_da_url(self):
        html = self._pagina(status="backup")
        self.assertRegex(html, r'class="eq-chip is-active" data-val="backup"')
        self.assertNotRegex(html, r'class="eq-chip is-active" data-val=""')

    def test_ids_dos_campos_continuam_os_mesmos(self):
        """O JS e outras telas apontam para eles."""
        html = self._pagina()
        for ident in ("filtrosForm", "filtro-nome", "filtro-subtipo", "filtro-status",
                      "filtro-localidade", "filtro-centro-custo", "filtro-tipo-item",
                      "filtro-estoque", "filtro-num-serie", "filtro-modelo",
                      "filtro-fornecedor", "chipsWrap", "btnLimpar", "pp", "countResults",
                      "btnImportar", "exportBtn", "btnNovoAtivo", "kpisWrap"):
            self.assertIn(f'id="{ident}"', html, ident)

    def test_status_no_select_segue_a_url(self):
        html = self._pagina(status="reservado")
        self.assertRegex(html, r'<option value="reservado"\s+selected>')


class BuscaPorNumeroDeSerieTests(_Base):
    """
    Pedido de 15/11/2026: a busca por nº de série fica ao lado da busca por
    nome — é como o time procura equipamento no dia a dia. Era um campo de
    "Mais filtros", recolhido por padrão.
    """

    def setUp(self):
        super().setUp()
        self._item("Switch Alfa", numero_serie="FOC1234ABCD")
        self._item("Switch Beta", numero_serie="FOC9999ZZZZ")

    def test_o_campo_esta_na_barra_ao_lado_da_busca_por_nome(self):
        html = self._pagina()
        self.assertEqual(html.count('id="filtro-num-serie"'), 1)
        barra = html.index('class="eq-barra-linha"')
        self.assertLess(barra, html.index('id="filtro-nome"'))
        self.assertLess(html.index('id="filtro-nome"'), html.index('id="filtro-num-serie"'))
        # ...e à frente do controle de área, fora do painel recolhido
        self.assertLess(html.index('id="filtro-num-serie"'), html.index('id="areaWrap"'))
        self.assertLess(html.index('id="filtro-num-serie"'), html.index('id="filtrosAvancados"'))

    def test_filtra_por_trecho_do_numero_de_serie(self):
        tbody = self._partial(numero_serie="1234abc")["tbody"]       # sem diferenciar maiúscula
        self.assertIn("Switch Alfa", tbody)
        self.assertNotIn("Switch Beta", tbody)

    def test_filtro_de_serie_nao_abre_o_mais_filtros_nem_conta_no_badge(self):
        html = self._pagina(numero_serie="FOC1234")
        self.assertRegex(html, r'<div class="eq-avancados" id="filtrosAvancados" hidden>')
        self.assertIn('id="filtrosBadgeText">0<', html)
        self.assertIn('value="FOC1234"', html)

    def test_limpar_aparece_com_o_filtro_de_serie(self):
        self.assertNotRegex(self._pagina(numero_serie="FOC"), r'id="btnLimpar" class="eq-link" hidden')

    def test_a_exportacao_respeita_o_numero_de_serie(self):
        from openpyxl import load_workbook

        resp = self.client.get(reverse("equipamentos_exportar"), {"numero_serie": "FOC1234"})
        wb = load_workbook(io.BytesIO(resp.content))
        valores = {str(c.value) for ws in wb.worksheets for linha in ws.iter_rows() for c in linha if c.value}
        self.assertIn("Switch Alfa", valores)
        self.assertNotIn("Switch Beta", valores)

    def test_a_dica_de_area_nao_aparece_com_o_serie_junto(self):
        tbody = self._partial(area="produtiva", numero_serie="nao-existe")["tbody"]
        self.assertNotIn("classificado como", tbody)


class MobileDaListaTests(_Base):
    """
    O print do usuário (15/11/2026) mostrava a linha "S/N · marca / modelo"
    cortada na borda do cartão. A CAUSA não estava neste CSS: o `base.css`
    global tem `th, td { white-space: nowrap }`, e `white-space` é HERDADO —
    o texto dentro da célula nunca quebrava, por mais `overflow-wrap` que o
    `.eq-asset-sub` declarasse. O cartão do mobile tem de devolver a quebra.
    """

    def _css(self):
        from pathlib import Path

        from django.conf import settings

        caminho = Path(settings.BASE_DIR) / "ProjetoEstoque" / "static" / "css" / "equipamentos-list.css"
        return caminho.read_text(encoding="utf-8")

    def _bloco_mobile(self):
        achado = re.search(r"@media \(max-width: 860px\) \{(.*?)\n\}\n", self._css(), re.S)
        self.assertIsNotNone(achado, "o bloco mobile (max-width: 860px) sumiu")
        return achado.group(1)

    def test_a_celula_do_cartao_devolve_a_quebra_de_linha(self):
        self.assertRegex(
            self._bloco_mobile(),
            r"\.eq-table tbody td \{\s*white-space: normal;",
            "sem isto a linha de S/N vaza do cartão (herda o nowrap global de `td`)",
        )

    def test_o_bloco_do_ativo_nao_se_mede_pelo_proprio_texto(self):
        self.assertRegex(self._bloco_mobile(), r"\.eq-asset-main \{\s*flex: 1 1 0;")

    def test_o_motivo_continua_existindo_no_base_css(self):
        """Se o `base.css` deixar de pôr nowrap em td, o conserto acima vira
        código morto — e este teste avisa para alguém decidir se o tira."""
        from pathlib import Path

        from django.conf import settings

        base = (Path(settings.BASE_DIR) / "ProjetoEstoque" / "static" / "css" / "base.css").read_text(encoding="utf-8")
        self.assertRegex(base, r"th, td \{[^}]*white-space: nowrap;")

    def test_celulas_de_dados_levam_o_conteudo_num_envolucro(self):
        """No cartão do mobile o `<td>` é uma grade rótulo | valor: sem o `.eq-cel`
        o segundo filho (CC, subtipo) cairia embaixo do RÓTULO."""
        self._item("Switch Cartao", AreaOperacionalChoices.PRODUTIVA)
        tbody = self._partial()["tbody"]
        self.assertEqual(tbody.count('class="eq-cel"'), 3)           # Tipo, Status, Localidade

    def test_busca_por_nome_e_por_serie_dividem_a_linha_no_celular(self):
        self.assertRegex(
            self._bloco_mobile_540(),
            r"\.eq-busca,\s*\.eq-busca--serie \{\s*flex: 1 1 9rem;",
        )

    def _bloco_mobile_540(self):
        achado = re.search(r"@media \(max-width: 540px\) \{(.*?)\n\}\n", self._css(), re.S)
        self.assertIsNotNone(achado, "o bloco mobile (max-width: 540px) sumiu")
        return achado.group(1)


class TelaEnxutaTests(_Base):
    def test_sem_selo_nem_cartoes_de_kpi(self):
        html = self._pagina()
        self.assertNotIn("page-kicker", html)
        self.assertNotIn("eq-kpi-card", html)
        self.assertNotIn("eq-filters-summary", html)
        self.assertEqual(html.count('class="eq-numeros"'), 1)

    def test_a_faixa_de_numeros_tambem_vem_no_ramo_ajax(self):
        dados = self._partial()
        self.assertIn('class="eq-numeros"', dados["kpis"])

    def test_manutencao_ou_defeito_acende_so_quando_ha(self):
        self._item("Switch Ok")
        self.assertIn("eq-numeros-item is-zero", self._partial()["kpis"])
        self._item("Switch Quebrado", status=StatusItemChoices.DEFEITO)
        self.assertIn("is-alerta", self._partial()["kpis"])

    def test_valor_em_estoque_so_aparece_quando_ha_valor(self):
        self._item("Switch Simples")
        self.assertNotIn("Valor em estoque", self._partial()["kpis"])

    def test_tabela_tem_cinco_colunas_e_sem_fornecedor(self):
        html = self._pagina()
        cabecalho = re.search(r"<thead>(.*?)</thead>", html, re.S).group(1)
        self.assertEqual(cabecalho.count("<th"), 5)
        self.assertNotIn("Fornecedor", cabecalho)
        self.assertNotIn("Lotes / Saldo", cabecalho)
        # a ordenação por coluna do JS é por ÍNDICE: tem de ser 0..3, sem buraco
        self.assertEqual(re.findall(r'data-col="(\d+)"', cabecalho), ["0", "1", "2", "3"])

    def test_cada_linha_tem_cinco_celulas(self):
        self._item("Switch Linha")
        tbody = self._partial()["tbody"]
        linha = re.search(r"<tr class=\"eq-clickable\".*?</tr>", tbody, re.S).group(0)
        self.assertEqual(linha.count("<td"), 5)

    def test_saldo_de_lote_vira_uma_linha_dentro_do_ativo(self):
        fornecedor = Fornecedor.objects.create(
            nome="Fornecedora", cnpj="33.333.333/3333-33",
            criado_por=self.user, atualizado_por=self.user,
        )
        item = self._item("Toner Preto")
        lote = LoteEstoque.objects.create(
            fornecedor=fornecedor, data_entrada=date.today(), numero_nf="NF-1",
            quantidade=8, custo_unitario=Decimal("20.00"),
            criado_por=self.user, atualizado_por=self.user,
        )
        ItemLote.objects.create(
            item=item, lote=lote, quantidade_entrada=8, quantidade_disponivel=3,
            custo_unitario=Decimal("20.00"),
            criado_por=self.user, atualizado_por=self.user,
        )
        tbody = self._partial()["tbody"]
        self.assertIn("eq-asset-lote", tbody)
        self.assertIn("3/8", tbody)
        self.assertIn("60,00", tbody)            # 3 x 20,00 — o valor continua na linha
        self.assertNotIn("eq-lote-stack", tbody)

    def test_item_sem_lote_nao_ganha_a_linha(self):
        self._item("Switch Sem Lote")
        tbody = self._partial()["tbody"]
        self.assertNotIn("eq-asset-lote", tbody)
        self.assertNotIn("Sem lote", tbody)

    def test_patrimonio_nao_ganha_selo_de_tipo(self):
        """É o caso comum: só o que foge dele (consumo, locado, com lote) se anuncia."""
        self._item("Switch Comum")
        self.assertNotIn("tp-patrimonio", self._partial()["tbody"])
        self._item("Papel A4", item_consumo="sim")
        self.assertIn("tp-consumo", self._partial(nome="Papel")["tbody"])

    def test_etiqueta_da_area_aparece_so_para_quem_tem_area(self):
        self._item("Switch Prod", AreaOperacionalChoices.PRODUTIVA)
        self._item("Switch Apoio", AreaOperacionalChoices.NAO_PRODUTIVA)
        self._item("Switch Livre")
        tbody = self._partial()["tbody"]
        self.assertEqual(tbody.count('class="eq-area is-produtiva"'), 1)
        self.assertEqual(tbody.count('class="eq-area is-nao_produtiva"'), 1)

    def test_lista_vazia_de_area_explica_onde_se_classifica(self):
        self._item("Switch Sem Area")
        tbody = self._partial(area="produtiva")["tbody"]
        self.assertIn("Nenhum ativo classificado como Produtiva", tbody)
        self.assertIn("Controle Operacional", tbody)

    def test_dica_de_area_so_quando_a_area_e_o_unico_filtro(self):
        """Com busca, status ou subtipo junto o vazio pode ser deles: a dica
        acusaria a área sem motivo."""
        self._item("Switch Sem Area")
        for outros in ({"nome": "zzzz"}, {"status": "backup"}, {"modelo": "zzz"},
                       {"subtipo": self.antena.pk}):
            with self.subTest(outros=outros):
                tbody = self._partial(area="produtiva", **outros)["tbody"]
                self.assertNotIn("classificado como", tbody)
                self.assertIn("Ajuste os filtros", tbody)

    def test_valor_agrupa_o_milhar_no_formato_da_localidade(self):
        """`floatformat:2|intcomma` dava "2,000,00": o intcomma recebe o texto já
        formatado e põe a vírgula inglesa na parte inteira."""
        fornecedor = Fornecedor.objects.create(
            nome="Fornecedora", cnpj="33.333.333/3333-33",
            criado_por=self.user, atualizado_por=self.user,
        )
        item = self._item("Toner Grande")
        lote = LoteEstoque.objects.create(
            fornecedor=fornecedor, data_entrada=date.today(), numero_nf="NF-2",
            quantidade=100, custo_unitario=Decimal("20.00"),
            criado_por=self.user, atualizado_por=self.user,
        )
        ItemLote.objects.create(
            item=item, lote=lote, quantidade_entrada=100, quantidade_disponivel=100,
            custo_unitario=Decimal("20.00"),
            criado_por=self.user, atualizado_por=self.user,
        )
        dados = self._partial()
        for nome, trecho in (("tbody", dados["tbody"]), ("kpis", dados["kpis"])):
            self.assertIn("2.000,00", trecho, nome)
            self.assertNotIn("2,000,00", trecho, nome)

    def test_lista_vazia_de_outro_filtro_mantem_o_texto_generico(self):
        tbody = self._partial(nome="inexistente")["tbody"]
        self.assertIn("Ajuste os filtros", tbody)
        self.assertNotIn("classificado como", tbody)

    def test_sem_area_vazio_nao_usa_a_dica_de_classificacao(self):
        tbody = self._partial(area="sem_area")["tbody"]
        self.assertNotIn("classificado como", tbody)

    def test_sem_css_nem_js_inline(self):
        html = self._pagina()
        self.assertNotIn("<style", html)
        self.assertIn("css/equipamentos-list.css", html)

    def test_numero_de_consultas_nao_cresce_com_o_numero_de_itens(self):
        """A etiqueta de área e a linha de lote saem de campos do próprio item:
        nenhuma consulta por linha."""
        self._item("Switch Um", AreaOperacionalChoices.PRODUTIVA)
        self._partial()                                        # aquece caches de sessão
        with CaptureQueriesContext(connection) as com_um:
            self._partial()
        for i in range(6):
            self._item(f"Switch Extra {i}", AreaOperacionalChoices.PRODUTIVA)
        with CaptureQueriesContext(connection) as com_sete:
            self._partial()
        self.assertEqual(len(com_um), len(com_sete))
