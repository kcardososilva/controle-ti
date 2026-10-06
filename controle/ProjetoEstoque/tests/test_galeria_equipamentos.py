"""
Galeria de equipamentos: saldo em lote no cartão.

A visão em LISTA tem a coluna "Lotes / Saldo" desde sempre; a GALERIA não
mostrava nada disso, e quem conferia estoque de consumo por ali precisava
voltar para a tabela.

Duas coisas falham em silêncio aqui:

1. A galeria é carregada por AJAX, num ramo separado da view
   (`if is_partial`). Hoje os dois ramos renderizam com o MESMO `context`, e
   é só por isso que os campos de lote chegam ao cartão. Se um dia a galeria
   passar a montar o próprio contexto sem chamar
   `_enriquecer_itens_com_lotes`, `item.lotes_count` vira string vazia no
   template, o `{% if %}` dá falso e o bloco **some do cartão sem erro
   nenhum**.

2. O bloco só deve aparecer para item QUE TEM lote. Patrimônio não tem, e um
   "Sem lote" em cada cartão seria ruído numa tela cheia deles.
"""
from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from ProjetoEstoque.models import (
    Categoria,
    Fornecedor,
    Item,
    ItemLote,
    LoteEstoque,
    Subtipo,
)


class SaldoDeLoteNaGaleriaTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_superuser("gal", "g@g.com", "x")
        self.client.force_login(self.user)

        cat = Categoria.objects.create(
            nome="Suprimentos", criado_por=self.user, atualizado_por=self.user
        )
        self.subtipo = Subtipo.objects.create(
            nome="Toner", alocado="nao", categoria=cat,
            criado_por=self.user, atualizado_por=self.user,
        )
        self.fornecedor = Fornecedor.objects.create(
            nome="Fornecedora", cnpj="33.333.333/3333-33",
            criado_por=self.user, atualizado_por=self.user,
        )

    def _item(self, nome):
        return Item.objects.create(
            nome=nome, subtipo=self.subtipo,
            criado_por=self.user, atualizado_por=self.user,
        )

    def _com_lote(self, nome, entrada, disponivel, custo="10.00"):
        item = self._item(nome)
        lote = LoteEstoque.objects.create(
            fornecedor=self.fornecedor, data_entrada=date.today(),
            numero_nf=f"NF-{nome[:6]}", quantidade=entrada,
            custo_unitario=Decimal(custo),
            criado_por=self.user, atualizado_por=self.user,
        )
        ItemLote.objects.create(
            item=item, lote=lote,
            quantidade_entrada=entrada, quantidade_disponivel=disponivel,
            custo_unitario=Decimal(custo),
            criado_por=self.user, atualizado_por=self.user,
        )
        return item

    def _galeria(self):
        """O HTML do ramo AJAX da galeria — não a página inteira."""
        resp = self.client.get(
            reverse("equipamentos_list"),
            {"view": "gallery"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 200)
        dados = resp.json()
        self.assertIn(
            "gallery", dados,
            "o ramo AJAX não devolveu a galeria — veja `if view_mode == "
            "'gallery'` em views/equipamentos.py",
        )
        return dados["gallery"]

    # ── o que o usuário pediu ───────────────────────────────────────────────

    def test_item_com_lote_mostra_o_saldo_no_cartao(self):
        self._com_lote("Toner Preto", entrada=20, disponivel=12, custo="35.50")
        html = self._galeria()

        self.assertIn("eq-gc-lote", html, "o bloco de saldo não foi renderizado")
        self.assertIn("Saldo em lote", html)
        self.assertIn("12", html, "a quantidade disponível não aparece")
        self.assertIn("20", html, "a quantidade de entrada não aparece")
        # 12 x 35,50 = 426,00
        self.assertIn("426,00", html, "o valor do saldo não aparece")

    def test_item_sem_lote_nao_ganha_o_bloco(self):
        """Patrimônio não tem lote; um "Sem lote" por cartão seria ruído."""
        self._item("Notebook sem lote")
        html = self._galeria()

        self.assertIn("eq-gc", html, "o cartão nem foi renderizado")
        self.assertNotIn("eq-gc-lote", html)
        self.assertNotIn("Saldo em lote", html)

    # ── o que quebra em silêncio ────────────────────────────────────────────

    def test_barra_reaproveita_as_classes_da_tabela(self):
        """
        A cor por faixa de saldo (ok / atenção / crítico / zerado) é decidida
        em `.eq-lote-fill` — uma regra só, usada pelas duas visões. Se o
        cartão passar a ter classe própria, as duas divergem no primeiro
        ajuste de limiar e ninguém percebe.
        """
        self._com_lote("Toner Ciano", entrada=10, disponivel=1)
        html = self._galeria()
        self.assertIn("eq-lote-progress", html)
        self.assertIn("eq-lote-fill", html)

    def test_status_do_saldo_chega_ao_cartao(self):
        """
        1 de 10 é 10% — faixa `critico` (<= 20%). É o status que pinta a
        barra de vermelho; sem ele o saldo baixo fica com cara de saldo
        normal.
        """
        self._com_lote("Toner Quase Zero", entrada=10, disponivel=1)
        html = self._galeria()
        self.assertIn("eq-lote-critico", html)
        self.assertIn('class="eq-lote-fill critico"', html)

    def test_saldo_zerado_e_marcado(self):
        self._com_lote("Toner Acabado", entrada=10, disponivel=0)
        html = self._galeria()
        self.assertIn("eq-lote-zerado", html)

    def test_percentual_vai_para_a_largura_e_para_o_aria(self):
        """
        A barra é a leitura de relance; o `aria-valuenow` é a mesma
        informação para quem usa leitor de tela.
        """
        self._com_lote("Toner Metade", entrada=10, disponivel=5)
        html = self._galeria()
        self.assertIn("width: 50%", html)
        self.assertIn('aria-valuenow="50"', html)

    def test_galeria_e_tabela_recebem_os_mesmos_campos(self):
        """
        O ponto frágil: hoje os dois ramos do AJAX usam o MESMO `context`, e
        é por isso que a galeria tem os campos de lote. Se a galeria passar a
        montar contexto próprio sem `_enriquecer_itens_com_lotes`, o
        `{% if item.lotes_count > 0 %}` dá falso e o bloco some do cartão —
        sem erro, sem log, sem teste de view falhando.
        """
        self._com_lote("Toner Comparado", entrada=8, disponivel=3, custo="20.00")

        galeria = self._galeria()
        tabela = self.client.get(
            reverse("equipamentos_list"),
            {"view": "list"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        ).json()["tbody"]

        # 3 x 20,00 = 60,00 — o mesmo número nas duas visões.
        self.assertIn("60,00", tabela)
        self.assertIn("60,00", galeria)
