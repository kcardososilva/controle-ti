"""
Recuperação de senha ("esqueci minha senha") — o fluxo todo.

Duas coisas aqui falham em SILÊNCIO na tela e por isso só o teste pega:

  • a tela de "enviado" aparece igual em todos os casos (de propósito, para não
    revelar quais logins existem), então "não enviou" e "enviou" são
    indistinguíveis pelo HTML — a verificação tem de ser na caixa de saída;

  • o link do e-mail é montado por `{% url 'password_reset_confirm' %}` dentro
    do template de e-mail. Renomear a rota não quebra nenhuma tela: quebra só
    o link que chega para o usuário.
"""
import re

from django.contrib.auth.models import Group, User
from django.core import mail
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from ProjetoEstoque.models import RegistroSeguranca


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class FluxoRecuperacaoTests(TestCase):
    def setUp(self):
        # O limite de pedidos vive no cache e é compartilhado entre testes:
        # sem limpar, um teste gasta a cota do seguinte.
        cache.clear()
        self.client = Client()
        self.user = User.objects.create_user(
            "joao.silva", "joao@exemplo.com", "SenhaAntiga!99"
        )

    def _pedir(self, identificador):
        return self.client.post(
            reverse("password_reset"), {"identificador": identificador}
        )

    def test_tela_de_pedido_abre_sem_sessao(self):
        resp = self.client.get(reverse("password_reset"))
        self.assertEqual(resp.status_code, 200)

    def test_login_oferece_o_link_de_recuperacao(self):
        """Sem o link na tela de login, o fluxo existe e ninguém encontra."""
        html = self.client.get(reverse("login")).content.decode("utf-8")
        self.assertIn(reverse("password_reset"), html)
        self.assertIn("Esqueci minha senha", html)

    def test_pedido_por_username_envia_o_link(self):
        """O ponto da tela: quem decora o login, não o e-mail, consegue usar."""
        self._pedir("joao.silva")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("joao@exemplo.com", mail.outbox[0].to)
        self.assertRegex(mail.outbox[0].body, r"/senha/nova/[^\s/]+/[^\s/]+/")

    def test_pedido_por_email_ignora_maiusculas(self):
        self._pedir("JOAO@EXEMPLO.COM")
        self.assertEqual(len(mail.outbox), 1)

    def test_identificador_inexistente_responde_igual_e_nao_envia(self):
        """
        A mesma resposta para quem existe e para quem não existe é o que impede
        usar a tela como verificador de logins válidos — o portal está exposto
        na internet.
        """
        resp_valido = self._pedir("joao.silva")
        cache.clear()
        mail.outbox.clear()
        resp_invalido = self._pedir("nao.existe.ninguem")

        self.assertEqual(resp_valido.status_code, resp_invalido.status_code)
        self.assertEqual(resp_valido["Location"], resp_invalido["Location"])
        self.assertEqual(len(mail.outbox), 0)

    def test_conta_sem_email_nao_gera_envio(self):
        User.objects.create_user("sem.email", "", "x")
        self._pedir("sem.email")
        self.assertEqual(len(mail.outbox), 0)

    def test_conta_inativa_nao_recupera(self):
        """Redefinir senha não pode ser caminho para reativar acesso cortado."""
        User.objects.create_user("demitido", "ex@exemplo.com", "x", is_active=False)
        self._pedir("demitido")
        self.assertEqual(len(mail.outbox), 0)

    def test_troca_de_senha_pelo_link_funciona(self):
        self._pedir("joao.silva")
        caminho = re.search(r"/senha/nova/\S+", mail.outbox[0].body).group(0)

        # O Django redireciona o link do e-mail para a URL com o token em
        # sessão antes de mostrar o formulário.
        resp = self.client.get(caminho)
        destino = resp["Location"] if resp.status_code == 302 else caminho
        self.assertEqual(self.client.get(destino).status_code, 200)

        self.client.post(destino, {
            "new_password1": "NovaSenha!2026",
            "new_password2": "NovaSenha!2026",
        })
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("NovaSenha!2026"))

    def test_link_adulterado_nao_mostra_formulario(self):
        resp = self.client.get("/senha/nova/MQ/token-invalido-aaaa/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Link inválido", resp.content.decode("utf-8"))

    def test_limite_por_identificador_corta_o_envio(self):
        """Sem o limite, a tela é um disparador de e-mail para a caixa alheia."""
        for _ in range(7):
            self._pedir("joao.silva")
        self.assertEqual(len(mail.outbox), 4, "o teto por identificador é 4/h")

    def test_pedido_e_conclusao_entram_na_trilha(self):
        self._pedir("joao.silva")
        caminho = re.search(r"/senha/nova/\S+", mail.outbox[0].body).group(0)
        destino = self.client.get(caminho)["Location"]
        self.client.post(destino, {
            "new_password1": "NovaSenha!2026",
            "new_password2": "NovaSenha!2026",
        })
        tipos = set(RegistroSeguranca.objects.values_list("tipo", flat=True))
        self.assertIn("senha_reset_pedido", tipos)
        self.assertIn("senha_reset_ok", tipos)

    def test_pedido_vazio_tambem_fica_registrado(self):
        """Pedido que não resolve em ninguém é o sinal de sondagem."""
        self._pedir("alvo.inexistente")
        registro = RegistroSeguranca.objects.get(tipo="senha_reset_pedido")
        self.assertIn("0 e-mail", registro.detalhe)


class FolhaDeAutenticacaoTests(TestCase):
    """O login e as 4 telas de recuperação dividem `static/css/auth.css`."""

    def test_login_usa_a_folha_externa_e_nao_style_inline(self):
        """
        A verificação é no HTML RENDERIZADO, não no arquivo: o comentário
        `{# ... #}` que explica a extração cita a palavra `<style>` e daria
        falso positivo — e comentário de template não chega ao navegador.
        """
        html = self.client.get(reverse("login")).content.decode("utf-8")
        self.assertNotIn("<style", html, "o CSS do login voltou para dentro do HTML")
        self.assertIn("css/auth.css", html)

    def test_folha_existe_e_nao_tem_tag_de_template(self):
        from pathlib import Path

        from django.conf import settings

        css = Path(
            settings.BASE_DIR, "ProjetoEstoque", "static", "css", "auth.css"
        ).read_text(encoding="utf-8")
        self.assertGreater(len(css), 5000)
        self.assertNotRegex(css, r"\{%|\{\{")

    def test_as_quatro_telas_renderizam(self):
        """Um `{% extends %}` com caminho errado quebra só na hora de abrir."""
        self.assertEqual(self.client.get(reverse("password_reset")).status_code, 200)
        self.assertEqual(
            self.client.get(reverse("password_reset_done")).status_code, 200
        )
        self.assertEqual(
            self.client.get(reverse("password_reset_complete")).status_code, 200
        )
        self.assertEqual(
            self.client.get("/senha/nova/MQ/x-y/").status_code, 200
        )


class PerfilNoChipTests(TestCase):
    """
    O chip do topo mostrava "Administrador" fixo para todo mundo — inclusive
    para fornecedor e visualizador de TV. O rótulo agora vem de
    services/perfil_acesso_service.py.
    """

    def setUp(self):
        cache.clear()
        self.client = Client()

    def _rotulo(self, user):
        from services.perfil_acesso_service import perfil_de_acesso

        cache.clear()
        return perfil_de_acesso(user)["rotulo"]

    def test_superusuario_e_administrador(self):
        u = User.objects.create_superuser("adm", "a@a.com", "x")
        self.assertEqual(self._rotulo(u), "Administrador")

    def test_grupo_interno_vira_o_rotulo(self):
        u = User.objects.create_user("op", "o@o.com", "x")
        u.groups.add(Group.objects.get_or_create(name="Operador TI")[0])
        self.assertEqual(self._rotulo(u), "Operador TI")

    def test_papel_maior_ganha_do_menor(self):
        u = User.objects.create_user("ge", "g@g.com", "x")
        u.groups.add(Group.objects.get_or_create(name="Operador TI")[0])
        u.groups.add(Group.objects.get_or_create(name="Gestor TI")[0])
        self.assertEqual(self._rotulo(u), "Gestor TI")

    def test_fornecedor_nao_se_ve_como_administrador(self):
        """O caso que motivou a correção."""
        from ProjetoEstoque.models import (
            Fornecedor,
            GRUPO_FORNECEDOR,
            PerfilFornecedor,
        )

        u = User.objects.create_user("forn", "f@f.com", "x")
        u.groups.add(Group.objects.get_or_create(name=GRUPO_FORNECEDOR)[0])
        empresa = Fornecedor.objects.create(
            nome="Empresa Externa", cnpj="11.111.111/1111-11",
            criado_por=u, atualizado_por=u,
        )
        PerfilFornecedor.objects.create(
            usuario=u, fornecedor=empresa, criado_por=u, atualizado_por=u
        )

        from services.perfil_acesso_service import perfil_de_acesso

        cache.clear()
        perfil = perfil_de_acesso(u)
        self.assertEqual(perfil["rotulo"], "Fornecedor")
        self.assertEqual(perfil["detalhe"], "Empresa Externa")
        self.assertTrue(perfil["externo"])

    def test_externo_ganha_de_grupo_interno_residual(self):
        """
        Um grupo interno sobrando num login externo não pode fazer o chip
        anunciar privilégio que o middleware não concede.
        """
        from ProjetoEstoque.models import GRUPO_FORNECEDOR

        u = User.objects.create_user("misto", "m@m.com", "x")
        u.is_staff = True
        u.save()
        u.groups.add(Group.objects.get_or_create(name=GRUPO_FORNECEDOR)[0])
        u.groups.add(Group.objects.get_or_create(name="Gestor TI")[0])
        self.assertEqual(self._rotulo(u), "Fornecedor")

    def test_anonimo_nao_quebra(self):
        from django.contrib.auth.models import AnonymousUser

        from services.perfil_acesso_service import perfil_de_acesso

        self.assertEqual(perfil_de_acesso(AnonymousUser())["rotulo"], "Sem perfil")

    def test_chip_do_topo_mostra_o_rotulo_real(self):
        u = User.objects.create_user("op2", "o2@o.com", "x", is_staff=True)
        u.groups.add(Group.objects.get_or_create(name="Operador TI")[0])
        self.client.force_login(u)
        html = self.client.get(reverse("dashboard")).content.decode("utf-8")
        self.assertIn('class="profile-role">Operador TI<', html)
        self.assertNotIn('class="profile-role">Administrador<', html)


class EndurecimentoTests(TestCase):
    """
    Item 11. O que está testado aqui é sobretudo o que NÃO pode ser ligado:
    cada um desses flags, no ambiente atual, quebra algo sem dar erro.
    """

    def test_cookie_de_csrf_continua_legivel_por_js(self):
        """
        Sete templates leem `document.cookie` para mandar o token no fetch
        (sino de notificações, ficha do equipamento, licenças Office, os dois
        portais e duas telas de preventiva). Com HTTPONLY eles levam 403 sem
        nenhuma mensagem na tela.
        """
        from django.conf import settings

        self.assertFalse(settings.CSRF_COOKIE_HTTPONLY)

    def test_cookies_seguros_acompanham_a_chave_de_https(self):
        """
        Ligar SESSION_COOKIE_SECURE com o acesso externo em http:// tranca
        todo mundo fora: o cookie nunca volta e o login entra em laço. Os três
        têm de andar juntos, atrás de DJANGO_HTTPS.
        """
        from django.conf import settings

        self.assertEqual(settings.SESSION_COOKIE_SECURE, settings.HTTPS_ATIVO)
        self.assertEqual(settings.CSRF_COOKIE_SECURE, settings.HTTPS_ATIVO)
        self.assertEqual(settings.SECURE_SSL_REDIRECT, settings.HTTPS_ATIVO)

    def test_hsts_so_com_https(self):
        """HSTS é irreversível pelo prazo anunciado — nunca sem TLS no ar."""
        from django.conf import settings

        if not settings.HTTPS_ATIVO:
            self.assertEqual(settings.SECURE_HSTS_SECONDS, 0)

    def test_endurecimento_que_nao_depende_de_tls_esta_ligado(self):
        from django.conf import settings

        self.assertTrue(settings.SECURE_CONTENT_TYPE_NOSNIFF)
        self.assertTrue(settings.SESSION_COOKIE_HTTPONLY)
        self.assertEqual(settings.X_FRAME_OPTIONS, "DENY")
        self.assertEqual(settings.SECURE_REFERRER_POLICY, "same-origin")
        self.assertEqual(settings.SESSION_COOKIE_SAMESITE, "Lax")

    def test_samesite_nao_e_strict(self):
        """'Strict' quebraria o retorno do OAuth do NinjaOne."""
        from django.conf import settings

        self.assertNotEqual(settings.SESSION_COOKIE_SAMESITE, "Strict")

    def test_sessao_tem_prazo_e_janela_deslizante(self):
        from django.conf import settings

        self.assertLessEqual(settings.SESSION_COOKIE_AGE, 24 * 3600)
        self.assertTrue(
            settings.SESSION_SAVE_EVERY_REQUEST,
            "sem isso quem está trabalhando é derrubado no meio do serviço",
        )

    def test_link_de_recuperacao_tem_prazo_curto(self):
        from django.conf import settings

        self.assertLessEqual(settings.PASSWORD_RESET_TIMEOUT, 24 * 3600)

    def test_remetente_definido(self):
        """Sem DEFAULT_FROM_EMAIL o envio sai como webmaster@localhost."""
        from django.conf import settings

        self.assertTrue(settings.DEFAULT_FROM_EMAIL)
        self.assertNotIn("webmaster@localhost", settings.DEFAULT_FROM_EMAIL)

    def test_logging_grava_erro_e_seguranca_em_arquivo(self):
        from django.conf import settings

        handlers = settings.LOGGING["handlers"]
        self.assertIn("arquivo_erros", handlers)
        self.assertIn("arquivo_seguranca", handlers)
        self.assertIn("seguranca", settings.LOGGING["loggers"])

    def test_painel_de_tv_recebe_sessao_longa(self):
        """
        A tela de parede fica logada por dias. Com o prazo global de 12h ela
        cairia no login no meio do expediente — e esticar o prazo de TODO
        mundo por causa dela seria o caminho errado.
        """
        from services.sessao_service import GRUPO_TV, SESSAO_TV_SEGUNDOS

        tv = User.objects.create_user("painel", "tv@x.com", "SenhaTv!2026")
        tv.groups.add(Group.objects.get_or_create(name=GRUPO_TV)[0])

        cliente = Client()
        cliente.login(username="painel", password="SenhaTv!2026")
        self.assertEqual(
            cliente.session.get_expiry_age(), SESSAO_TV_SEGUNDOS
        )

    def test_usuario_comum_fica_no_prazo_global(self):
        from django.conf import settings

        User.objects.create_user("comum", "c@x.com", "SenhaC!2026")
        cliente = Client()
        cliente.login(username="comum", password="SenhaC!2026")
        self.assertLessEqual(
            cliente.session.get_expiry_age(), settings.SESSION_COOKIE_AGE
        )
