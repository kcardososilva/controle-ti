"""Painel de notificações (/alertas/notificacoes/): salvar sem recarregar a página (pedido de 14/11).

Cada ação do painel responde de DUAS maneiras, com a mesma regra por trás:
  • formulário comum (sem JavaScript): grava, deixa uma mensagem e redireciona;
  • chamada da própria tela (`X-Requested-With`): grava e devolve JSON com a mensagem, os totais do topo e o cartão do canal
    já redesenhado — e NÃO deixa mensagem na sessão (senão ela reapareceria na próxima abertura da página).
"""

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from ProjetoEstoque.models import CanalNotificacao
from services.email_alertas import sincronizar_catalogo_notificacoes

AJAX = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}


class PainelBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user("painel_admin", "painel@x.com", "senha-boa-123", is_superuser=True)
        sincronizar_catalogo_notificacoes()
        # canal de lista FIXA: aceita destinatários próprios e mostra o "x" de cada e-mail
        cls.canal = CanalNotificacao.objects.get(codigo="ninja_licencas_liberar")

    def setUp(self):
        self.client.force_login(self.admin)

    def url(self, nome, *args):
        return reverse(nome, args=[self.canal.pk, *args])

    def definir(self, texto):
        CanalNotificacao.objects.filter(pk=self.canal.pk).update(destinatarios_customizados=texto, destinatarios_customizados_ativo=bool(texto))
        self.canal.refresh_from_db()


class PaginaTests(PainelBase):
    def test_a_pagina_desenha_o_cartao_de_cada_canal_pelo_parcial(self):
        """Include com caminho errado não dá erro: renderiza vazio. Por isso confere o elemento-âncora de CADA canal."""
        html = self.client.get(reverse("alertas_notificacoes")).content.decode()
        for canal in CanalNotificacao.objects.all():
            self.assertIn(f'id="al-canal-{canal.pk}"', html, canal.codigo)

    def test_os_formularios_do_painel_se_oferecem_ao_script_e_o_script_e_carregado(self):
        self.definir("a@x.com, b@x.com")
        html = self.client.get(reverse("alertas_notificacoes")).content.decode()
        self.assertIn("js/notificacoes-ajax.js?v=", html)
        # ativar/desativar, salvar destinatários e o "x" de cada e-mail
        self.assertIn(f'action="{self.url("alertas_notificacao_toggle")}"', html)
        self.assertGreaterEqual(html.count("data-al-ajax"), 3)

    def test_a_faixa_do_topo_tem_ancora_para_o_script_atualizar_os_totais(self):
        html = self.client.get(reverse("alertas_notificacoes")).content.decode()
        for chave in ("total", "ativos", "inativos", "envios"):
            self.assertIn(f'data-al-kpi="{chave}"', html)
        self.assertIn('data-al-metric="inativos"', html)


class FormularioComumTests(PainelBase):
    """Sem JavaScript o painel continua do jeito que era: grava, avisa e redireciona."""

    def test_toggle_redireciona_e_deixa_a_mensagem(self):
        antes = self.canal.ativo
        resposta = self.client.post(self.url("alertas_notificacao_toggle"))
        self.assertRedirects(resposta, reverse("alertas_notificacoes"), fetch_redirect_response=False)
        self.canal.refresh_from_db()
        self.assertNotEqual(self.canal.ativo, antes)
        self.assertEqual(len(list(get_messages(resposta.wsgi_request))), 1)

    def test_destinatarios_redireciona_e_grava(self):
        resposta = self.client.post(self.url("alertas_notificacao_destinatarios"), {"destinatarios": "novo@x.com"})
        self.assertEqual(resposta.status_code, 302)
        self.canal.refresh_from_db()
        self.assertEqual(self.canal.destinatarios_customizados, "novo@x.com")

    def test_so_post_muda_alguma_coisa(self):
        for nome in ("alertas_notificacao_toggle", "alertas_notificacao_destinatarios", "alertas_notificacao_remover_email"):
            antes = (self.canal.ativo, self.canal.destinatarios_customizados)
            self.assertEqual(self.client.get(self.url(nome)).status_code, 302, nome)
            self.canal.refresh_from_db()
            self.assertEqual((self.canal.ativo, self.canal.destinatarios_customizados), antes, nome)


class PelaTelaTests(PainelBase):
    """A mesma ação, chamada por fetch: JSON com o cartão novo, sem recarregar nada."""

    def test_toggle_devolve_o_cartao_redesenhado_e_os_totais(self):
        self.canal.ativo = True
        self.canal.save(update_fields=["ativo"])
        ativos_antes = CanalNotificacao.objects.filter(ativo=True).count()

        resposta = self.client.post(self.url("alertas_notificacao_toggle"), **AJAX)
        dados = resposta.json()

        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["nivel"], "warning")
        self.assertIn("desativada", dados["mensagem"])
        self.assertIn(f'id="al-canal-{self.canal.pk}"', dados["html"])
        self.assertIn("Desativada", dados["html"])
        self.assertEqual(dados["kpi"]["ativos"], ativos_antes - 1)
        self.assertEqual(dados["kpi"]["inativos"], dados["kpi"]["total"] - dados["kpi"]["ativos"])

    def test_a_mensagem_nao_fica_na_sessao_para_reaparecer_na_proxima_abertura(self):
        resposta = self.client.post(self.url("alertas_notificacao_toggle"), **AJAX)
        self.assertEqual(len(list(get_messages(resposta.wsgi_request))), 0)
        pagina = self.client.get(reverse("alertas_notificacoes")).content.decode()
        self.assertNotIn("alert-container", pagina)

    def test_destinatarios_grava_e_o_cartao_volta_com_o_valor_novo(self):
        resposta = self.client.post(self.url("alertas_notificacao_destinatarios"), {"destinatarios": "ti@x.com, chefe@x.com"}, **AJAX)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.canal.refresh_from_db()
        self.assertEqual(self.canal.destinatarios_customizados, "ti@x.com, chefe@x.com")
        self.assertIn('value="ti@x.com, chefe@x.com"', dados["html"])
        self.assertIn("chefe@x.com", dados["html"])

    def test_destinatarios_em_branco_volta_ao_padrao_do_sistema(self):
        self.definir("ti@x.com")
        dados = self.client.post(self.url("alertas_notificacao_destinatarios"), {"destinatarios": "  "}, **AJAX).json()
        self.assertTrue(dados["ok"])
        self.canal.refresh_from_db()
        self.assertFalse(self.canal.destinatarios_customizados_ativo)
        self.assertIn("padrão", dados["mensagem"])

    def test_remover_um_email_tira_so_ele_e_o_cartao_volta_sem_o_x_dele(self):
        self.definir("ti@x.com, chefe@x.com")
        dados = self.client.post(self.url("alertas_notificacao_remover_email"), {"email": "chefe@x.com"}, **AJAX).json()
        self.assertTrue(dados["ok"])
        self.canal.refresh_from_db()
        self.assertEqual(self.canal.destinatarios_customizados, "ti@x.com")
        self.assertNotIn("chefe@x.com", dados["html"])
        self.assertIn("ti@x.com", dados["html"])

    def test_remover_o_ultimo_email_avisa_que_o_canal_nao_envia_mais(self):
        self.definir("ti@x.com")
        dados = self.client.post(self.url("alertas_notificacao_remover_email"), {"email": "ti@x.com"}, **AJAX).json()
        self.assertEqual(dados["nivel"], "warning")
        self.assertIn("não enviará", dados["mensagem"])
        self.canal.refresh_from_db()
        # lista vazia continua "personalizada": reintroduzir o padrão do .env em silêncio seria o oposto do que a pessoa fez
        self.assertTrue(self.canal.destinatarios_customizados_ativo)

    def test_remover_sem_informar_o_email_nao_apaga_nada(self):
        self.definir("ti@x.com")
        resposta = self.client.post(self.url("alertas_notificacao_remover_email"), {"email": ""}, **AJAX)
        self.assertEqual(resposta.json()["nivel"], "info")
        self.canal.refresh_from_db()
        self.assertEqual(self.canal.destinatarios_customizados, "ti@x.com")

    def test_canal_inexistente_responde_404_em_json_e_nao_quebra_a_tela(self):
        resposta = self.client.post(reverse("alertas_notificacao_toggle", args=[999999]), **AJAX)
        self.assertEqual(resposta.status_code, 404)
        dados = resposta.json()
        self.assertFalse(dados["ok"])
        self.assertIsNone(dados["html"])
        self.assertIn("kpi", dados)

    def test_canal_de_defeito_nao_aceita_destinatarios_e_o_erro_chega_como_erro(self):
        defeito = CanalNotificacao.objects.get(codigo="item_defeito")
        resposta = self.client.post(reverse("alertas_notificacao_destinatarios", args=[defeito.pk]), {"destinatarios": "x@x.com"}, **AJAX)
        self.assertEqual(resposta.status_code, 400)
        self.assertEqual(resposta.json()["nivel"], "error")
        defeito.refresh_from_db()
        self.assertNotEqual(defeito.destinatarios_customizados, "x@x.com")

    def test_sem_login_nao_ha_resposta_de_json(self):
        self.client.logout()
        resposta = self.client.post(self.url("alertas_notificacao_toggle"), **AJAX)
        self.assertEqual(resposta.status_code, 302)          # vai para o login; o JS trata o redirect como "sessão expirada"
        self.assertIn("login", resposta["Location"])
        self.canal.refresh_from_db()
