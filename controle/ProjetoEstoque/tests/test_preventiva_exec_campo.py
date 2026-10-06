"""
Tela de execução de preventiva em uso de campo (celular, no galpão).

O que importa aqui é o `rascunho_marco`: é ele que diz ao JS se o rascunho
guardado no aparelho ainda vale. Se o carimbo vier errado, o técnico ou perde
o checklist (rascunho descartado à toa) ou é convidado a repreencher uma
execução que já está no banco. As duas falhas são silenciosas na tela, então
o teste é a única rede de proteção.
"""
from datetime import date

from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.urls import reverse

from ProjetoEstoque.models import (
    Categoria,
    CheckListModelo,
    Item,
    Preventiva,
    PreventivaExecucao,
    Subtipo,
)


class RascunhoMarcoTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_superuser("tec_teste", "t@t.com", "x")
        self.client.force_login(self.user)

        cat = Categoria.objects.create(
            nome="Informática", criado_por=self.user, atualizado_por=self.user
        )
        subtipo = Subtipo.objects.create(
            nome="Notebook", alocado="sim", categoria=cat,
            criado_por=self.user, atualizado_por=self.user,
        )
        self.item = Item.objects.create(
            nome="Notebook de teste", subtipo=subtipo,
            criado_por=self.user, atualizado_por=self.user,
        )
        self.checklist = CheckListModelo.objects.create(
            nome="Checklist de teste", intervalo_dias=90,
            criado_por=self.user, atualizado_por=self.user,
        )
        self.preventiva = Preventiva.objects.create(
            equipamento=self.item, checklist_modelo=self.checklist,
            criado_por=self.user, atualizado_por=self.user,
        )

    def _abrir_execucao(self):
        return self.client.get(
            reverse("preventiva_exec", args=[self.preventiva.pk])
        )

    def test_preventiva_nunca_executada_tem_marco_vazio(self):
        """Sem execução gravada, qualquer rascunho no aparelho é legítimo."""
        resp = self._abrir_execucao()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["rascunho_marco"], "")

    def test_marco_acompanha_a_ultima_execucao_gravada(self):
        execucao = PreventivaExecucao.objects.create(
            preventiva=self.preventiva, data_execucao=date.today(),
            criado_por=self.user, atualizado_por=self.user,
        )
        resp = self._abrir_execucao()
        self.assertEqual(
            resp.context["rascunho_marco"], execucao.updated_at.isoformat(),
            "o carimbo tem que ser o da execução mais recente — é o que faz o "
            "rascunho já enviado ser descartado em vez de reoferecido",
        )

    def test_marco_usa_a_execucao_mais_recente_e_nao_a_primeira(self):
        antiga = PreventivaExecucao.objects.create(
            preventiva=self.preventiva, data_execucao=date(2026, 1, 10),
            criado_por=self.user, atualizado_por=self.user,
        )
        nova = PreventivaExecucao.objects.create(
            preventiva=self.preventiva, data_execucao=date(2026, 6, 20),
            criado_por=self.user, atualizado_por=self.user,
        )
        resp = self._abrir_execucao()
        self.assertEqual(resp.context["rascunho_marco"], nova.updated_at.isoformat())
        self.assertNotEqual(resp.context["rascunho_marco"], antiga.updated_at.isoformat())

    def test_edicao_usa_o_carimbo_da_propria_execucao(self):
        execucao = PreventivaExecucao.objects.create(
            preventiva=self.preventiva, data_execucao=date.today(),
            tecnico=self.user, criado_por=self.user, atualizado_por=self.user,
        )
        resp = self.client.get(
            reverse("preventiva_execucao_editar", args=[execucao.pk])
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["rascunho_marco"], execucao.updated_at.isoformat())

    def test_tela_entrega_o_aviso_e_o_bloco_de_rascunho(self):
        """Marcação e script têm que chegar juntos — um sem o outro é aviso
        que nunca aparece, ou script que erra o elemento."""
        html = self._abrir_execucao().content.decode("utf-8")
        for marca in ("pvRascunho", "pvRascunhoManter", "pvRascunhoDescartar",
                      "pv_rascunho_v1:", "visibilitychange"):
            self.assertIn(marca, html, f"faltou {marca} na tela de execução")


class ToqueEmCampoTests(TestCase):
    """A tela é preenchida em pé, no galpão. Estes alvos não podem encolher."""

    def test_css_eleva_alvo_de_toque_na_tela_de_execucao(self):
        from pathlib import Path
        from django.conf import settings

        texto = (
            Path(settings.BASE_DIR)
            / "ProjetoEstoque" / "templates" / "front" / "preventivas"
            / "preventiva_exec.html"
        ).read_text(encoding="utf-8")

        self.assertIn("pointer: coarse", texto)
        self.assertIn("--pv-control-h: 44px", texto)
        # 16px no campo é o que impede o iOS de dar zoom ao focar o input.
        self.assertIn("font-size: 16px", texto)
