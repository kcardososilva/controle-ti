"""Paginação do Zelo Agro: UMA só (`frota_comum.paginar` + `m2m/_paginacao.html`), feita no BANCO.

O pedido de 14/11 exige primeira/última página, total de registros, tamanho de página (10/25/50/100),
preservação de pesquisa e filtros ao virar a página e nenhum registro repetido ou faltando. Aqui se prova isso
no helper e numa tela real (a lista de chips); as outras listas usam o mesmo helper e o mesmo parcial."""

from django.contrib.auth.models import User
from django.db import connection
from django.template.loader import render_to_string
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext

from ProjetoEstoque.models import M2MChip
from ProjetoEstoque.views import frota_comum as comum


def pedido(**parametros):
    return RequestFactory().get("/x/", parametros)


class PaginarTests(TestCase):
    def setUp(self):
        self.chips = [
            M2MChip.objects.create(nome=f"Chip {n:03d}", iccid=f"8955100000000{n:06d}"[:19]) for n in range(1, 131)
        ]

    def consulta(self):
        return M2MChip.objects.order_by("nome")

    def test_o_tamanho_pedido_vale_so_dentro_das_opcoes(self):
        for pedido_, esperado in (({}, 25), ({"por_pagina": "10"}, 10), ({"por_pagina": "100"}, 100),
                                  ({"por_pagina": "7"}, 25), ({"por_pagina": "100000"}, 25), ({"por_pagina": "abc"}, 25),
                                  ({"por_pagina": "-5"}, 25)):
            with self.subTest(pedido=pedido_):
                page, _, _ = comum.paginar(pedido(**pedido_), self.consulta())
                self.assertEqual(page.paginator.per_page, esperado)

    def test_o_padrao_da_tela_continua_valendo_quando_nada_e_pedido(self):
        page, _, _ = comum.paginar(pedido(), self.consulta(), por_pagina=30)
        self.assertEqual(page.paginator.per_page, 30)
        self.assertIn(30, page.opcoes_por_pagina)                         # o padrão da tela também aparece nas opções

    def test_nenhum_registro_repete_nem_falta_percorrendo_todas_as_paginas(self):
        for tamanho in (10, 25, 50, 100):
            with self.subTest(tamanho=tamanho):
                vistos = []
                paginas = comum.paginar(pedido(por_pagina=str(tamanho)), self.consulta())[0].paginator.num_pages
                for n in range(1, paginas + 1):
                    page, _, _ = comum.paginar(pedido(por_pagina=str(tamanho), page=str(n)), self.consulta())
                    vistos.extend(c.pk for c in page.object_list)
                self.assertEqual(len(vistos), 130)
                self.assertEqual(len(set(vistos)), 130)
                self.assertEqual(vistos, [c.pk for c in self.consulta()])  # e na mesma ordem

    def test_pagina_fora_do_intervalo_cai_na_ultima_sem_estourar(self):
        page, _, _ = comum.paginar(pedido(por_pagina="50", page="999"), self.consulta())
        self.assertEqual(page.number, 3)
        self.assertEqual(len(page.object_list), 30)
        page, _, _ = comum.paginar(pedido(page="abc"), self.consulta())
        self.assertEqual(page.number, 1)

    def test_a_paginacao_e_do_banco_e_nao_carrega_a_tabela_toda(self):
        with CaptureQueriesContext(connection) as q:
            page, _, _ = comum.paginar(pedido(por_pagina="10"), self.consulta())
            list(page.object_list)
        sql = [c["sql"] for c in q.captured_queries]
        self.assertEqual(len(sql), 2)                                     # um COUNT e um SELECT com LIMIT
        self.assertTrue(any("COUNT(" in s.upper() for s in sql))
        self.assertTrue(any("LIMIT 10" in s.upper() for s in sql))

    def test_os_links_preservam_pesquisa_e_filtros_e_trocar_o_tamanho_volta_para_a_pagina_1(self):
        req = pedido(q="chip", situacao="vinculado", page="3", por_pagina="10")
        page, paginas, qs = comum.paginar(req, self.consulta())
        self.assertNotIn("page=", qs)                                     # os links de página acrescentam o `page`
        self.assertIn("q=chip", qs)
        self.assertIn("por_pagina=10", qs)                                # a escolha do tamanho acompanha a navegação
        self.assertNotIn("por_pagina", page.qs_base)                      # o seletor de tamanho a troca
        self.assertIn("situacao=vinculado", page.qs_base)
        html = render_to_string("front/m2m/_paginacao.html", {"page_obj": page, "paginas": paginas, "querystring": qs})
        self.assertIn('aria-label="Primeira página"', html)
        self.assertIn('aria-label="Última página"', html)
        self.assertIn("130 registros", html)
        self.assertIn("situacao=vinculado&amp;por_pagina=25", html)       # tamanho novo: sem `page` (volta à 1)
        self.assertNotIn("por_pagina=25&amp;page", html)


class ParcialTests(TestCase):
    def render(self, n_registros, **pedido_):
        for n in range(n_registros):
            M2MChip.objects.create(nome=f"C{n:03d}", iccid=f"8955100000001{n:06d}"[:19])
        page, paginas, qs = comum.paginar(pedido(**pedido_), M2MChip.objects.order_by("nome"))
        return render_to_string("front/m2m/_paginacao.html", {"page_obj": page, "paginas": paginas, "querystring": qs})

    def test_sem_resultado_nao_desenha_nada(self):
        self.assertEqual(self.render(0).strip(), "")

    def test_uma_pagina_so_mostra_o_total_sem_navegacao(self):
        html = self.render(5)
        self.assertIn("1–5 de 5 registros", html)
        self.assertNotIn("Primeira página", html)
        self.assertNotIn("Por página", html)                              # com 10 ou menos, escolher tamanho não faz sentido

    def test_na_primeira_pagina_primeira_e_anterior_ficam_desabilitadas(self):
        html = self.render(60, por_pagina="10")
        self.assertNotIn('aria-label="Primeira página"', html)
        self.assertIn('aria-label="Última página"', html)
        self.assertIn("Por página", html)

    def test_na_ultima_pagina_proxima_e_ultima_ficam_desabilitadas(self):
        html = self.render(60, por_pagina="10", page="6")
        self.assertIn('aria-label="Primeira página"', html)
        self.assertNotIn('aria-label="Última página"', html)
        self.assertIn("51–60 de 60 registros", html)
