"""
Perfis de ACESSO RESTRITO — Portal de Contratações e Consulta Quiosque.

O que cada bloco protege. Todas são falhas de SEGURANÇA que passam calado,
porque a tela continua abrindo normalmente para quem testa logado como admin:

  • CAMINHO — o middleware tem de barrar TUDO fora da área, e barrar DENTRO
    dela também (`/quiosque/` guarda consulta e gestão lado a lado). Um prefixo
    esquecido não quebra nada: só entrega uma tela a mais a quem não deveria.

  • AÇÃO — esconder o botão não é autorização. Se o decorador cair, o POST
    montado à mão passa, e o solicitante marca "acesso criado" num sistema que
    ninguém criou — que é a única informação que o checklist existe para dar.

  • STAFF NÃO É RESTRITO — os quatro middlewares do projeto liberam staff. Se
    `conceder` aceitasse um administrador, apareceria uma linha na tela de
    gestão e NENHUMA restrição no sistema: a pior combinação possível, porque
    quem administra passa a acreditar numa trava que não existe.

  • CACHE — `_grupos_do_usuario` guarda os grupos por 60s. Sem derrubar a chave
    ao conceder/revogar, "removi o acesso e a pessoa continuou entrando" fica
    verdadeiro por um minuto e ninguém reproduz depois.

  • MENU — o menu do TI listado para um perfil restrito é link que devolve a
    pessoa para onde ela já estava, e um inventário dos módulos do sistema de
    graça.
"""

from django.contrib.auth.models import Group, Permission, User
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import RequestFactory, TestCase
from django.urls import reverse

from services import acesso_restrito_service as ar

from ProjetoEstoque.models import (
    CentroCusto,
    Contratacao,
    ContratacaoSistema,
    GRUPO_CONSULTA_QUIOSQUE,
    GRUPO_PORTAL_CONTRATACAO,
    KioskDevice,
    SistemaCorporativo,
    StatusContratacaoChoices,
)


def _restrito(username, grupo):
    """Usuário de acesso restrito: ativo, SEM staff, dentro do grupo.

    `is_staff=False` não é detalhe de arrumação — é o que torna a restrição
    efetiva. Ver `ar._interno`.
    """
    user = User.objects.create_user(username, password="x")
    user.groups.add(Group.objects.get(name=grupo))
    return user


class BaseAcessoTests(TestCase):
    def setUp(self):
        # `_grupos_do_usuario` cacheia por 60s e o cache do runner é de
        # processo: sem limpar, o perfil de um teste vaza para o próximo.
        cache.clear()

        self.ti = User.objects.create_user("ti", password="x", is_staff=True)
        self.rh = _restrito("rh.maria", GRUPO_PORTAL_CONTRATACAO)
        self.olheiro = _restrito("consulta.joao", GRUPO_CONSULTA_QUIOSQUE)

        self.cc = CentroCusto.objects.create(numero="100", departamento="TI")
        self.sistema, _ = SistemaCorporativo.objects.get_or_create(
            nome="TOTVS - Datasul", defaults={"ordem": 10, "exige_perfil_espelho": True},
        )
        self.vaga = Contratacao.objects.create(
            cargo="Técnico de Segurança do Trabalho",
            tipo_vaga="nova", computador="notebook", telefonia="ramal",
            centro_custo=self.cc, criado_por=self.ti, atualizado_por=self.ti,
        )
        self.linha = ContratacaoSistema.objects.create(
            contratacao=self.vaga, sistema=self.sistema, necessario=True,
        )
        self.device = KioskDevice.objects.create(
            token_hash="a" * 64, apelido="Tablet Portaria", modelo="Galaxy Tab A8",
        )

    def tearDown(self):
        cache.clear()


# ─────────────────────────────────────────────────────────────
# A tabela de caminhos, sem HTTP
# ─────────────────────────────────────────────────────────────

class TabelaDeCaminhosTests(TestCase):
    def test_contratacao_alcanca_o_modulo_e_nada_mais(self):
        libera = lambda p: ar.caminho_liberado(p, "contratacao")  # noqa: E731
        self.assertTrue(libera("/contratacoes/"))
        self.assertTrue(libera("/contratacoes/nova/"))
        self.assertTrue(libera("/contratacoes/7/"))
        self.assertTrue(libera("/contratacoes/7/etapa/"))
        self.assertFalse(libera("/equipamentos/"))
        self.assertFalse(libera("/quiosque/"))
        self.assertFalse(libera("/admin/"))
        self.assertFalse(libera("/media/termos/x.pdf"))

    def test_catalogo_de_sistemas_fica_fora_do_portal_de_contratacao(self):
        # É configuração do TI: define o que o formulário pergunta.
        self.assertFalse(ar.caminho_liberado("/contratacoes/sistemas/", "contratacao"))
        self.assertFalse(ar.caminho_liberado("/contratacoes/sistemas/novo/", "contratacao"))

    def test_consulta_quiosque_le_as_telas_e_nao_alcanca_as_de_gestao(self):
        libera = lambda p: ar.caminho_liberado(p, "quiosque")  # noqa: E731
        # Leitura
        self.assertTrue(libera("/quiosque/"))
        self.assertTrue(libera("/quiosque/mapa/"))
        self.assertTrue(libera("/quiosque/indicadores/"))
        self.assertTrue(libera("/quiosque/12/"))
        self.assertTrue(libera("/quiosque/12/mapa/atualizar/"))
        self.assertTrue(libera("/quiosque/12/sinal/"))
        self.assertTrue(libera("/quiosque/12/checkins/exportar/"))
        # Gestão — o pk vem ANTES do trecho proibido, por isso `bloqueados_contem`
        self.assertFalse(libera("/quiosque/12/config/"))
        self.assertFalse(libera("/quiosque/12/comando/"))
        self.assertFalse(libera("/quiosque/12/revogar/"))
        self.assertFalse(libera("/quiosque/12/excluir/"))
        self.assertFalse(libera("/quiosque/matriculas/"))
        self.assertFalse(libera("/quiosque/apk/upload/"))
        self.assertFalse(libera("/quiosque/instalador/gerar/"))

    def test_api_do_aparelho_nunca_entra_na_area_de_consulta(self):
        # `/api/quiosque/` é autenticada por token do device, não por sessão.
        self.assertFalse(ar.caminho_liberado("/api/quiosque/checkin/", "quiosque"))
        self.assertFalse(ar.caminho_liberado("/api/quiosque/enroll/", "quiosque"))

    def test_estatico_e_login_ficam_liberados_para_os_dois(self):
        # Sem isto a tela abre sem CSS e a pessoa não consegue sair.
        for chave in ("contratacao", "quiosque"):
            self.assertTrue(ar.caminho_liberado("/static/css/tokens.css", chave))
            self.assertTrue(ar.caminho_liberado("/login/", chave))
            self.assertTrue(ar.caminho_liberado("/logout/", chave))


# ─────────────────────────────────────────────────────────────
# Quem é restrito
# ─────────────────────────────────────────────────────────────

class QuemEhRestritoTests(BaseAcessoTests):
    def test_cada_grupo_devolve_a_sua_chave(self):
        self.assertEqual(ar.perfil_de(self.rh), "contratacao")
        self.assertEqual(ar.perfil_de(self.olheiro), "quiosque")

    def test_usuario_interno_nao_tem_perfil_restrito(self):
        self.assertIsNone(ar.perfil_de(self.ti))

    def test_staff_no_grupo_continua_interno(self):
        # Os middlewares liberam staff em qualquer caminho. Se `perfil_de`
        # dissesse "restrito" aqui, o template esconderia botões de um
        # administrador que continua podendo tudo — trava de mentira.
        self.ti.groups.add(Group.objects.get(name=GRUPO_PORTAL_CONTRATACAO))
        cache.clear()
        self.assertIsNone(ar.perfil_de(self.ti))
        self.assertTrue(ar.pode_gerenciar_contratacao(self.ti))

    def test_anonimo_nao_e_restrito(self):
        from django.contrib.auth.models import AnonymousUser
        self.assertIsNone(ar.perfil_de(AnonymousUser()))

    def test_capacidades_sao_opostas_entre_os_dois_perfis(self):
        # Quem só consulta o Quiosque não perde o Portal de Contratações por
        # tabela — perde porque o middleware não o deixa chegar lá.
        self.assertFalse(ar.pode_gerenciar_contratacao(self.rh))
        self.assertTrue(ar.pode_gerenciar_quiosque(self.rh))
        self.assertFalse(ar.pode_gerenciar_quiosque(self.olheiro))
        self.assertTrue(ar.pode_gerenciar_contratacao(self.olheiro))


# ─────────────────────────────────────────────────────────────
# Camada 1 — middleware
# ─────────────────────────────────────────────────────────────

class MiddlewareTests(BaseAcessoTests):
    def test_portal_de_contratacao_abre_o_painel(self):
        self.client.force_login(self.rh)
        resp = self.client.get(reverse("contratacao_painel"))
        self.assertEqual(resp.status_code, 200)

    def test_portal_de_contratacao_e_expulso_de_qualquer_outra_tela(self):
        self.client.force_login(self.rh)
        for nome in ("equipamentos_list", "quiosque_dashboard", "dashboard",
                     "admin_perfil", "sistema_corporativo_list"):
            with self.subTest(tela=nome):
                resp = self.client.get(reverse(nome))
                self.assertEqual(resp.status_code, 302)
                self.assertEqual(resp.url, reverse("contratacao_painel"))

    def test_consulta_quiosque_abre_as_tres_telas_de_leitura(self):
        self.client.force_login(self.olheiro)
        for nome in ("quiosque_dashboard", "quiosque_mapa", "quiosque_indicadores"):
            with self.subTest(tela=nome):
                self.assertEqual(self.client.get(reverse(nome)).status_code, 200)

    def test_consulta_quiosque_ve_a_ficha_do_aparelho(self):
        self.client.force_login(self.olheiro)
        resp = self.client.get(reverse("quiosque_detalhe", args=[self.device.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Tablet Portaria", resp.content.decode())

    def test_sobre_o_quiosque_e_leitura_e_alcancada_pelos_dois_publicos(self):
        """`/quiosque/sobre/` é texto de referência, não ação — por isso não tem
        decorador de capacidade nem entra em `bloqueados`/`bloqueados_contem`.
        Os dois públicos do módulo (TI e Consulta Quiosque) precisam alcançá-la."""
        for user in (self.ti, self.olheiro):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                resp = self.client.get(reverse("quiosque_sobre"))
                self.assertEqual(resp.status_code, 200)
                self.assertIn("Sobre o Quiosque", resp.content.decode())

    def test_consulta_quiosque_e_barrada_nas_telas_de_gestao(self):
        self.client.force_login(self.olheiro)
        alvos = [
            ("quiosque_matriculas", []),
            ("quiosque_config_editar", [self.device.pk]),
            ("quiosque_comando_novo", [self.device.pk]),
            ("quiosque_revogar", [self.device.pk]),
            ("quiosque_excluir", [self.device.pk]),
            ("quiosque_instalador_gerar", []),
            ("quiosque_apk_upload", []),
        ]
        for nome, args in alvos:
            with self.subTest(tela=nome):
                resp = self.client.get(reverse(nome, args=args))
                self.assertEqual(resp.status_code, 302)
                self.assertEqual(resp.url, reverse("quiosque_dashboard"))

    def test_post_de_comando_tambem_e_barrado_pelo_caminho(self):
        # O middleware roda antes da view: nem chega ao @require_POST.
        self.client.force_login(self.olheiro)
        resp = self.client.post(
            reverse("quiosque_comando_novo", args=[self.device.pk]), {"tipo": "reiniciar"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse("quiosque_dashboard"))

    def test_consulta_quiosque_e_expulsa_de_fora_do_modulo(self):
        self.client.force_login(self.olheiro)
        resp = self.client.get(reverse("contratacao_painel"))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse("quiosque_dashboard"))

    def test_usuario_interno_nao_e_afetado(self):
        self.client.force_login(self.ti)
        self.assertEqual(self.client.get(reverse("contratacao_painel")).status_code, 200)
        self.assertEqual(self.client.get(reverse("quiosque_dashboard")).status_code, 200)
        self.assertEqual(self.client.get(reverse("sistema_corporativo_list")).status_code, 200)


# ─────────────────────────────────────────────────────────────
# Camada 2 — decoradores de capacidade
# ─────────────────────────────────────────────────────────────

class CapacidadeTests(BaseAcessoTests):
    def test_solicitante_abre_vaga(self):
        """O portal existe para ISSO. Se cair, a feature inteira não serve."""
        self.client.force_login(self.rh)
        resp = self.client.post(reverse("contratacao_create"), {
            "cargo": "Auxiliar Administrativo",
            "tipo_vaga": "nova",
            "computador": "desktop",
            "telefonia": "nenhum",
            "centro_custo": self.cc.pk,
            "email_corporativo": "on",
            f"sistema_{self.sistema.pk}": "on",
            f"sistema_{self.sistema.pk}_espelho": "rodrigo.tadeu",
        })
        self.assertEqual(resp.status_code, 302)
        nova = Contratacao.objects.get(cargo="Auxiliar Administrativo")
        self.assertEqual(nova.criado_por, self.rh)
        self.assertTrue(nova.sistemas.filter(sistema=self.sistema, necessario=True).exists())

    def test_solicitante_edita_a_propria_vaga(self):
        self.client.force_login(self.rh)
        self.assertEqual(
            self.client.get(reverse("contratacao_update", args=[self.vaga.pk])).status_code,
            200,
        )

    def test_solicitante_nao_avanca_etapa(self):
        self.client.force_login(self.rh)
        resp = self.client.post(
            reverse("contratacao_etapa", args=[self.vaga.pk]), {"acao": "concluir"},
        )
        self.assertEqual(resp.status_code, 403)
        self.vaga.refresh_from_db()
        self.assertEqual(self.vaga.status, StatusContratacaoChoices.ABERTA)

    def test_solicitante_nao_marca_acesso_como_provisionado(self):
        self.client.force_login(self.rh)
        resp = self.client.post(
            reverse("contratacao_sistema_toggle", args=[self.vaga.pk, self.linha.pk])
        )
        self.assertEqual(resp.status_code, 403)
        self.linha.refresh_from_db()
        self.assertFalse(self.linha.provisionado)

    def test_solicitante_nao_exclui_nem_com_a_permissao_django(self):
        """A permissão do Django não vale como passe aqui.

        Se o decorador de capacidade não existisse, bastaria a permissão
        `delete_contratacao` (que qualquer grupo pode acabar recebendo) para o
        solicitante apagar a própria vaga e o histórico dela.
        """
        self.rh.user_permissions.add(
            Permission.objects.get(codename="delete_contratacao")
        )
        cache.clear()
        self.client.force_login(self.rh)
        resp = self.client.get(reverse("contratacao_delete", args=[self.vaga.pk]))
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(Contratacao.objects.filter(pk=self.vaga.pk).exists())

    def test_decorador_de_quiosque_barra_mesmo_sem_o_middleware(self):
        """2ª camada isolada: a view recusa por si, não por causa do caminho.

        O middleware já barra `/quiosque/<pk>/comando/`, então pelo cliente de
        teste nunca se chega ao decorador. Aqui ele é chamado direto — é o que
        protege a view se um dia a rota mudar de forma.
        """
        pedido = RequestFactory().post("/qualquer/")
        pedido.user = self.olheiro

        @ar.exige_gestao_quiosque
        def falsa_view(request):
            return "passou"

        with self.assertRaises(PermissionDenied):
            falsa_view(pedido)

        pedido.user = self.ti
        self.assertEqual(falsa_view(pedido), "passou")


# ─────────────────────────────────────────────────────────────
# Camada 3 — o que a tela mostra
# ─────────────────────────────────────────────────────────────

class TelaTests(BaseAcessoTests):
    def test_menu_do_ti_nao_aparece_para_perfil_restrito(self):
        self.client.force_login(self.rh)
        html = self.client.get(reverse("contratacao_painel")).content.decode()
        self.assertIn("Painel de contratações", html)
        self.assertIn("Abrir nova vaga", html)
        # Itens do menu do TI: nenhum deles pode sobrar.
        for item in ("Movimentações", "Licenças de Software", "Ativos / Equipamentos",
                     "Painéis Gerenciais", "Preventivas"):
            with self.subTest(item=item):
                self.assertNotIn(item, html)

    def test_menu_do_ti_continua_inteiro_para_usuario_interno(self):
        self.client.force_login(self.ti)
        html = self.client.get(reverse("contratacao_painel")).content.decode()
        self.assertIn("Movimentações", html)
        self.assertIn("Painéis Gerenciais", html)

    def test_sino_de_notificacoes_sai_para_perfil_restrito(self):
        # O painel do sino lista notificação de todo o sistema.
        self.client.force_login(self.rh)
        html = self.client.get(reverse("contratacao_painel")).content.decode()
        self.assertNotIn('id="notifWrap"', html)

        self.client.force_login(self.ti)
        html = self.client.get(reverse("contratacao_painel")).content.decode()
        self.assertIn('id="notifWrap"', html)

    def test_chip_do_perfil_nao_e_link_para_perfil_restrito(self):
        self.client.force_login(self.rh)
        html = self.client.get(reverse("contratacao_painel")).content.decode()
        self.assertIn("Portal de Contratações", html)
        self.assertNotIn(f'href="{reverse("admin_perfil")}"', html)

    def test_atalhos_de_configuracao_saem_do_painel_de_contratacao(self):
        self.client.force_login(self.rh)
        html = self.client.get(reverse("contratacao_painel")).content.decode()
        self.assertNotIn(reverse("sistema_corporativo_list"), html)
        self.assertNotIn(reverse("acesso_restrito_gerenciar"), html)

    def test_detalhe_esconde_provisionamento_e_etapas_do_solicitante(self):
        self.client.force_login(self.rh)
        html = self.client.get(reverse("contratacao_detail", args=[self.vaga.pk])).content.decode()
        # Vê o pedido e o andamento...
        self.assertIn("TOTVS - Datasul", html)
        self.assertIn("Linha do tempo", html)
        # ...e não aciona nada.
        self.assertNotIn("Marcar como criado", html)
        self.assertNotIn("Concluir contratação", html)
        self.assertNotIn("Cancelar vaga", html)
        self.assertNotIn("Registrar entrega", html)
        self.assertNotIn(reverse("contratacao_delete", args=[self.vaga.pk]), html)

    def test_detalhe_mostra_tudo_para_o_ti(self):
        self.client.force_login(self.ti)
        html = self.client.get(reverse("contratacao_detail", args=[self.vaga.pk])).content.decode()
        self.assertIn("Marcar como criado", html)
        # Vaga ainda em aberto: o card das etapas existe, mas só se conclui depois
        # de a vaga ser preenchida.
        self.assertIn("Etapas para concluir", html)
        self.assertIn("só pode ser concluída depois de a vaga ser preenchida", html)

    def test_dashboard_do_quiosque_esconde_matriculas_da_consulta(self):
        self.client.force_login(self.olheiro)
        html = self.client.get(reverse("quiosque_dashboard")).content.decode()
        self.assertNotIn(reverse("quiosque_matriculas"), html)
        self.assertNotIn(reverse("acesso_restrito_gerenciar"), html)
        # O que ela vem ver continua lá.
        self.assertIn("Tablet Portaria", html)
        self.assertIn(reverse("quiosque_mapa"), html)
        # "Sobre o quiosque" é leitura, sem decorador de capacidade: continua
        # visível mesmo sem o `{% if pode_gerenciar_quiosque %}` que tira Matrículas.
        self.assertIn(reverse("quiosque_sobre"), html)

    def test_ficha_do_aparelho_esconde_comando_configuracao_e_exclusao(self):
        self.client.force_login(self.olheiro)
        html = self.client.get(reverse("quiosque_detalhe", args=[self.device.pk])).content.decode()
        self.assertNotIn("Enviar comando", html)
        self.assertNotIn("Zona de perigo", html)
        self.assertNotIn("Editar configuração", html)
        self.assertNotIn(reverse("quiosque_config_editar", args=[self.device.pk]), html)
        # Mas a configuração APLICADA é informação, e fica.
        self.assertIn("Configuração aplicada", html)

    def test_ficha_do_aparelho_mostra_tudo_para_o_ti(self):
        self.client.force_login(self.ti)
        html = self.client.get(reverse("quiosque_detalhe", args=[self.device.pk])).content.decode()
        self.assertIn("Enviar comando", html)
        self.assertIn("Zona de perigo", html)


# ─────────────────────────────────────────────────────────────
# Tela de gestão dos acessos
# ─────────────────────────────────────────────────────────────

class GestaoDeAcessosTests(BaseAcessoTests):
    def test_so_administrador_abre_a_tela(self):
        self.client.force_login(self.ti)
        self.assertEqual(
            self.client.get(reverse("acesso_restrito_gerenciar")).status_code, 200,
        )

        comum = User.objects.create_user("operador", password="x")
        self.client.force_login(comum)
        resp = self.client.get(reverse("acesso_restrito_gerenciar"))
        self.assertEqual(resp.status_code, 302)

    def test_conceder_cria_o_login_sem_staff(self):
        self.client.force_login(self.ti)
        resp = self.client.post(reverse("acesso_restrito_conceder"), {
            "perfil": "contratacao",
            "username": "rh.ana",
            "email": "ana@santacolomba.com.br",
            "senha": "segredo123",
        })
        self.assertEqual(resp.status_code, 302)
        nova = User.objects.get(username="rh.ana")
        self.assertFalse(nova.is_staff)
        self.assertFalse(nova.is_superuser)
        self.assertTrue(nova.check_password("segredo123"))
        self.assertEqual(ar.perfil_de(nova), "contratacao")

    def test_conceder_vincula_login_que_ja_existe_sem_pedir_senha(self):
        ja = User.objects.create_user("gestor.caio", password="x")
        self.client.force_login(self.ti)
        self.client.post(reverse("acesso_restrito_conceder"), {
            "perfil": "quiosque", "username": "gestor.caio",
        })
        ja.refresh_from_db()
        self.assertEqual(ar.perfil_de(ja), "quiosque")
        self.assertTrue(ja.check_password("x"))

    def test_conceder_recusa_administrador(self):
        with self.assertRaises(ValidationError) as ctx:
            ar.conceder(chave="contratacao", username="ti")
        self.assertIn("administrador", "; ".join(ctx.exception.messages))
        self.assertIsNone(ar.perfil_de(self.ti))

    def test_conceder_soma_o_perfil_sem_tirar_o_que_a_pessoa_ja_tem(self):
        # Antes isto era recusado ("dois perfis não se somam"). Agora o mesmo
        # login acumula os dois, e conceder nunca substitui o grupo anterior.
        usuario, criado = ar.conceder(chave="quiosque", username="rh.maria")
        self.assertFalse(criado)
        self.assertEqual(ar.perfis_de(usuario), ("contratacao", "quiosque"))

    def test_conceder_exige_senha_para_login_novo(self):
        with self.assertRaises(ValidationError):
            ar.conceder(chave="contratacao", username="rh.novo")
        self.assertFalse(User.objects.filter(username="rh.novo").exists())

    def test_senha_curta_e_recusada(self):
        with self.assertRaises(ValidationError):
            ar.conceder(chave="contratacao", username="rh.novo", senha="123")

    def test_revogar_tira_o_acesso_e_mantem_o_login(self):
        self.client.force_login(self.ti)
        self.client.post(
            reverse("acesso_restrito_revogar", args=[self.rh.pk]),
            {"perfil": "contratacao"},
        )
        self.rh.refresh_from_db()
        self.assertTrue(self.rh.is_active)
        self.assertIsNone(ar.perfil_de(self.rh))

    def test_revogar_vale_na_hora_e_nao_em_sessenta_segundos(self):
        """O cache de grupos tem TTL de 60s.

        Sem `_esquecer_cache`, a pessoa cujo acesso foi revogado continua
        navegando na área por até um minuto — e "revoguei e ele continuou
        entrando" é impossível de reproduzir depois, porque na segunda tentativa
        já passou.
        """
        self.assertEqual(ar.perfil_de(self.rh), "contratacao")  # aquece o cache
        ar.revogar(chave="contratacao", usuario=self.rh)
        self.assertIsNone(ar.perfil_de(self.rh))

    def test_conceder_tambem_vale_na_hora(self):
        novo = User.objects.create_user("rh.bia", password="x")
        self.assertIsNone(ar.perfil_de(novo))  # aquece o cache como "interno"
        ar.conceder(chave="contratacao", username="rh.bia")
        self.assertEqual(ar.perfil_de(novo), "contratacao")

    def test_candidatos_exclui_staff_e_quem_ja_tem_ESTE_acesso(self):
        nomes = [u.username for u in ar.candidatos("contratacao")]
        self.assertNotIn("ti", nomes)             # staff
        self.assertNotIn("rh.maria", nomes)       # já tem ESTE perfil
        # Tem o outro perfil, não este: continua sendo candidato — os perfis se somam.
        self.assertIn("consulta.joao", nomes)
        User.objects.create_user("livre", password="x")
        self.assertIn("livre", [u.username for u in ar.candidatos("contratacao")])
        self.assertIn("livre", [u.username for u in ar.candidatos("quiosque")])

    def test_painel_lista_os_dois_perfis_com_seus_usuarios(self):
        painel = ar.painel_de_acessos()
        self.assertEqual([p["chave"] for p in painel], ["contratacao", "quiosque"])
        por_chave = {p["chave"]: p for p in painel}
        self.assertIn(self.rh, por_chave["contratacao"]["usuarios"])
        self.assertIn(self.olheiro, por_chave["quiosque"]["usuarios"])


# ─────────────────────────────────────────────────────────────
# Mais de um grupo no mesmo login (Contratações + Quiosque)
# ─────────────────────────────────────────────────────────────

class VariosGruposTests(BaseAcessoTests):
    """Os perfis são independentes e se SOMAM.

    A falha a vigiar não derruba tela nenhuma: é o perfil que "vence" e engole o
    outro (o comportamento antigo de `perfil_de`), e a pessoa perde o acesso a
    uma tela sem que nada avise. Daí cada teste afirmar os DOIS lados.
    """

    def setUp(self):
        super().setUp()
        self.duplo = _restrito("duplo.ana", GRUPO_PORTAL_CONTRATACAO)
        self.duplo.groups.add(Group.objects.get(name=GRUPO_CONSULTA_QUIOSQUE))
        cache.clear()

    def test_perfis_de_devolve_os_dois_na_ordem_da_tabela(self):
        self.assertEqual(ar.perfis_de(self.duplo), ("contratacao", "quiosque"))
        # Quem tem um só continua com um só: nada vazou de um login para outro.
        self.assertEqual(ar.perfis_de(self.rh), ("contratacao",))
        self.assertEqual(ar.perfis_de(self.olheiro), ("quiosque",))

    def test_alcanca_as_duas_areas(self):
        self.client.force_login(self.duplo)
        self.assertEqual(self.client.get(reverse("contratacao_painel")).status_code, 200)
        self.assertEqual(self.client.get(reverse("contratacao_detail", args=[self.vaga.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("quiosque_dashboard")).status_code, 200)
        self.assertEqual(self.client.get(reverse("quiosque_detalhe", args=[self.device.pk])).status_code, 200)

    def test_continua_preso_fora_das_duas_areas(self):
        self.client.force_login(self.duplo)
        for nome in ("equipamentos_list", "dashboard", "admin_perfil"):
            with self.subTest(tela=nome):
                resp = self.client.get(reverse(nome))
                self.assertEqual(resp.status_code, 302)
                self.assertEqual(resp.url, reverse("contratacao_painel"))

    def test_cada_perfil_mantem_a_propria_trava(self):
        """Somar áreas não soma poderes: o Quiosque segue só leitura e a vaga
        segue sem atendimento, mesmo para quem tem os dois."""
        self.client.force_login(self.duplo)

        # Quiosque: barrado pelo CAMINHO (middleware), como para o olheiro puro.
        resp = self.client.post(
            reverse("quiosque_comando_novo", args=[self.device.pk]), {"tipo": "reiniciar"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse("quiosque_dashboard"))
        self.assertEqual(
            self.client.get(reverse("quiosque_matriculas")).status_code, 302,
        )

        # Contratações: o caminho passa, a AÇÃO é recusada (decorador).
        resp = self.client.post(
            reverse("contratacao_etapa", args=[self.vaga.pk]), {"acao": "concluir"},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(
            self.client.get(reverse("sistema_corporativo_list")).status_code, 302,
        )

    def test_capacidades_ficam_cada_uma_no_seu_modulo(self):
        self.assertFalse(ar.pode_gerenciar_contratacao(self.duplo))
        self.assertFalse(ar.pode_gerenciar_quiosque(self.duplo))
        self.assertTrue(ar.pode_solicitar_estrutura(self.duplo))

    def test_conceder_o_segundo_grupo_nao_tira_o_primeiro(self):
        novo = User.objects.create_user("gestor.leo", password="x")
        ar.conceder(chave="contratacao", username="gestor.leo")
        ar.conceder(chave="quiosque", username="gestor.leo")
        self.assertEqual(ar.perfis_de(novo), ("contratacao", "quiosque"))

    def test_conceder_o_mesmo_grupo_de_novo_e_inofensivo(self):
        ar.conceder(chave="contratacao", username="rh.maria")
        self.assertEqual(ar.perfis_de(self.rh), ("contratacao",))
        self.assertEqual(self.rh.groups.filter(name=GRUPO_PORTAL_CONTRATACAO).count(), 1)

    def test_remover_contratacoes_mantem_o_quiosque_funcionando(self):
        """O cenário de aceite: remove um grupo, o outro continua."""
        self.client.force_login(self.duplo)
        self.assertEqual(self.client.get(reverse("contratacao_painel")).status_code, 200)

        ar.revogar(chave="contratacao", usuario=self.duplo)

        self.assertEqual(ar.perfis_de(self.duplo), ("quiosque",))
        self.assertEqual(self.client.get(reverse("quiosque_dashboard")).status_code, 200)
        resp = self.client.get(reverse("contratacao_painel"))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse("quiosque_dashboard"))

    def test_remover_quiosque_mantem_contratacoes_funcionando(self):
        self.client.force_login(self.duplo)
        ar.revogar(chave="quiosque", usuario=self.duplo)

        self.assertEqual(ar.perfis_de(self.duplo), ("contratacao",))
        self.assertEqual(self.client.get(reverse("contratacao_painel")).status_code, 200)
        self.assertEqual(self.client.get(reverse("quiosque_dashboard")).status_code, 302)

    def test_revogar_pela_tela_diz_que_o_outro_acesso_continua(self):
        self.client.force_login(self.ti)
        resp = self.client.post(
            reverse("acesso_restrito_revogar", args=[self.duplo.pk]),
            {"perfil": "contratacao"}, follow=True,
        )
        mensagens = " ".join(str(m) for m in resp.context["messages"])
        self.assertIn("continua com o acesso", mensagens)
        self.assertIn("Consulta Quiosque", mensagens)

    def test_menu_traz_uma_secao_por_perfil(self):
        pedido = RequestFactory().get(reverse("contratacao_detail", args=[self.vaga.pk]))
        pedido.user = self.duplo
        ctx = ar.contexto_restrito(pedido)
        self.assertEqual([s["chave"] for s in ctx["acesso_restrito_secoes"]], ["contratacao", "quiosque"])
        contratacao, quiosque = ctx["acesso_restrito_secoes"]
        # O detalhe pendura no primeiro item de CONTRATAÇÕES e não acende nada no Quiosque.
        self.assertTrue(contratacao["menu"][0]["ativo"])
        self.assertFalse(any(i["ativo"] for i in quiosque["menu"]))

    def test_menu_renderizado_mostra_as_duas_areas(self):
        self.client.force_login(self.duplo)
        html = self.client.get(reverse("contratacao_painel")).content.decode()
        self.assertIn("Painel de contratações", html)
        self.assertIn(reverse("quiosque_dashboard"), html)
        self.assertIn("Aparelhos", html)

    def test_chip_diz_que_a_pessoa_acumula_as_duas_areas(self):
        from services.perfil_acesso_service import perfil_de_acesso
        cache.clear()
        self.assertEqual(perfil_de_acesso(self.duplo)["rotulo"], "Contratações + Quiosque")

    def test_tela_de_gestao_mostra_o_outro_acesso_da_mesma_pessoa(self):
        self.client.force_login(self.ti)
        html = self.client.get(reverse("acesso_restrito_gerenciar")).content.decode()
        self.assertIn("também: Consulta Quiosque", html)
        self.assertIn("também: Portal de Contratações", html)

    def test_staff_nos_dois_grupos_continua_interno(self):
        self.ti.groups.add(
            Group.objects.get(name=GRUPO_PORTAL_CONTRATACAO),
            Group.objects.get(name=GRUPO_CONSULTA_QUIOSQUE),
        )
        cache.clear()
        self.assertEqual(ar.perfis_de(self.ti), ())
        self.assertTrue(ar.pode_gerenciar_contratacao(self.ti))
        self.assertTrue(ar.pode_gerenciar_quiosque(self.ti))


# ─────────────────────────────────────────────────────────────
# Encanamento
# ─────────────────────────────────────────────────────────────

class EncanamentoTests(BaseAcessoTests):
    def test_migration_cria_os_dois_grupos(self):
        for nome in (GRUPO_PORTAL_CONTRATACAO, GRUPO_CONSULTA_QUIOSQUE):
            self.assertTrue(Group.objects.filter(name=nome).exists(), nome)

    def test_chip_do_topo_mostra_o_papel_real(self):
        from services.perfil_acesso_service import perfil_de_acesso
        cache.clear()
        self.assertEqual(perfil_de_acesso(self.rh)["rotulo"], "Portal de Contratações")
        self.assertEqual(perfil_de_acesso(self.olheiro)["rotulo"], "Consulta Quiosque")
        self.assertTrue(perfil_de_acesso(self.rh)["externo"])

    def test_contexto_de_template_fecha_com_as_capacidades(self):
        pedido = RequestFactory().get(reverse("contratacao_painel"))
        pedido.user = self.rh
        ctx = ar.contexto_restrito(pedido)
        self.assertEqual(ctx["acesso_restrito"], "contratacao")
        self.assertFalse(ctx["pode_gerenciar_contratacao"])
        self.assertTrue(ctx["pode_gerenciar_quiosque"])
        self.assertEqual(len(ctx["acesso_restrito_secoes"]), 1)
        menu = ctx["acesso_restrito_secoes"][0]["menu"]
        self.assertEqual(len(menu), 3)  # painel · abrir vaga · chamados de TI
        self.assertTrue(menu[0]["ativo"])

    def test_contexto_de_template_sai_neutro_para_interno(self):
        pedido = RequestFactory().get("/")
        pedido.user = self.ti
        ctx = ar.contexto_restrito(pedido)
        self.assertIsNone(ctx["acesso_restrito"])
        self.assertTrue(ctx["pode_gerenciar_contratacao"])
        self.assertTrue(ctx["pode_gerenciar_quiosque"])

    def test_menu_pendura_o_detalhe_no_primeiro_item(self):
        # `/contratacoes/7/` não é nenhum dos dois itens do menu: sem a regra
        # de fallback o menu ficaria todo apagado na tela de detalhe.
        pedido = RequestFactory().get(reverse("contratacao_detail", args=[self.vaga.pk]))
        pedido.user = self.rh
        menu = ar.contexto_restrito(pedido)["acesso_restrito_secoes"][0]["menu"]
        self.assertTrue(menu[0]["ativo"])
        self.assertFalse(menu[1]["ativo"])

    def test_middleware_esta_registrado_depois_dos_outros_portais(self):
        from django.conf import settings
        lista = list(settings.MIDDLEWARE)
        self.assertIn("ProjetoEstoque.middleware.AcessoRestritoMiddleware", lista)
        self.assertGreater(
            lista.index("ProjetoEstoque.middleware.AcessoRestritoMiddleware"),
            lista.index("django.contrib.auth.middleware.AuthenticationMiddleware"),
        )

    def test_todo_icone_do_menu_reduzido_existe_no_sprite(self):
        """O `icone` da tabela PERFIS tem de ser um `<symbol>` do base.html.

        Um id errado não quebra nada: o `<use>` aponta para o vazio e o item do
        menu aparece sem ícone, com HTTP 200 e nada no console. É a contrapartida
        de gerar o menu em loop — `test_menu_lateral` não consegue conferir um id
        que só existe em tempo de execução.
        """
        import re
        from pathlib import Path

        from django.conf import settings

        base = Path(settings.BASE_DIR) / "ProjetoEstoque" / "templates" / "base.html"
        definidos = set(re.findall(r'<symbol id="(ic-[a-z-]+)"', base.read_text(encoding="utf-8")))
        self.assertTrue(definidos, "o sprite de ícones sumiu do base.html")
        for chave, perfil in ar.PERFIS.items():
            for item in perfil["nav"]:
                with self.subTest(perfil=chave, item=item["rotulo"]):
                    self.assertIn(item["icone"], definidos)

    def test_context_processor_esta_registrado(self):
        from django.conf import settings
        processadores = settings.TEMPLATES[0]["OPTIONS"]["context_processors"]
        self.assertIn("ProjetoEstoque.context_processors.acesso_restrito", processadores)
