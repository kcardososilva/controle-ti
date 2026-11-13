"""
Ambientes do Zelo (TI · Agro) — `services/ambiente_service.py`, `AmbienteMiddleware`,
o seletor da barra superior, o menu do Agro e os endereços antigos de Frotas.

Critérios de aceite cobertos aqui (pedido de 2026-11-13):
  1  alternar entre TI e Agro sem novo login (mesma sessão);
  2  navegação, cor e rotas mudam conforme o ambiente;
  3  acesso a cada ambiente respeita a permissão de cada um;
  4  Frotas e Chips e Conectividade com submenus independentes;
  13 trocar de ambiente não concede permissão nenhuma.
E a compatibilidade dos endereços antigos (/frotas/…, /m2m/…).
"""
from django.contrib.auth.models import Group, Permission, User
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import resolve, reverse

from ProjetoEstoque import urls_legado_frota
from ProjetoEstoque.models import (
    GRUPO_FORNECEDOR,
    GRUPO_PORTAL_CONTRATACAO,
    GRUPO_SOMENTE_AGRO,
    Notificacao,
    PreferenciaAmbiente,
)
from services import ambiente_service as amb


class AmbientesBase(TestCase):
    def setUp(self):
        # os grupos do usuário ficam 60 s no cache, e o id se repete entre testes
        cache.clear()

    def tearDown(self):
        cache.clear()

    def usuario(self, nome, *codenames, grupos=(), **campos):
        user = User.objects.create_user(username=nome, password="x", **campos)
        for codigo in codenames:
            user.user_permissions.add(Permission.objects.get(codename=codigo))
        for nome_grupo in grupos:
            user.groups.add(Group.objects.get_or_create(name=nome_grupo)[0])
        return User.objects.get(pk=user.pk)  # sem o cache de permissões do objeto antigo

    def cliente(self, user):
        c = Client()
        c.force_login(user)
        return c

    # os quatro perfis do pedido
    def so_ti(self):
        return self.usuario("so_ti")

    def os_dois(self):
        return self.usuario("os_dois", "ver_frota", "ver_chips")

    def so_agro(self):
        return self.usuario("so_agro", "ver_frota", grupos=(GRUPO_SOMENTE_AGRO,))


class RegraTests(AmbientesBase):
    def test_rota_define_o_ambiente(self):
        self.assertEqual(amb.ambiente_do_caminho("/agro/"), amb.AGRO)
        self.assertEqual(amb.ambiente_do_caminho("/agro/conectividade/chips/3/"), amb.AGRO)
        self.assertEqual(amb.ambiente_do_caminho("/frotas/veiculos/"), amb.AGRO)  # endereço antigo
        self.assertEqual(amb.ambiente_do_caminho("/m2m/"), amb.AGRO)
        self.assertEqual(amb.ambiente_do_caminho("/dashboard/"), amb.TI)
        self.assertEqual(amb.ambiente_do_caminho("/equipamentos/"), amb.TI)
        self.assertEqual(amb.ambiente_do_caminho("/agronomia/"), amb.TI)  # prefixo exige a barra
        for neutro in ("/", "/static/css/base.css", "/login/", "/perfil/", "/notificacoes/marcar-lidas/"):
            self.assertIsNone(amb.ambiente_do_caminho(neutro), neutro)

    def test_ti_e_o_padrao_de_todo_interno(self):
        """Login novo, sem grupo nenhum, continua no TI — não é permissão a conceder."""
        self.assertEqual(amb.ambientes_de(self.so_ti()), (amb.TI,))

    def test_permissao_de_modulo_do_agro_da_o_ambiente(self):
        self.assertEqual(amb.ambientes_de(self.os_dois()), (amb.TI, amb.AGRO))
        self.assertEqual(amb.ambientes_de(self.usuario("visao", "acessar_zelo_agro")), (amb.TI, amb.AGRO))

    def test_grupo_exclusivo_prende_no_agro(self):
        self.assertEqual(amb.ambientes_de(self.so_agro()), (amb.AGRO,))

    def test_grupo_exclusivo_ja_traz_a_visao_geral(self):
        """Quem é preso no Agro nunca fica sem ambiente nenhum (migration 0192)."""
        preso = self.usuario("preso", grupos=(GRUPO_SOMENTE_AGRO,))
        self.assertEqual(amb.ambientes_de(preso), (amb.AGRO,))

    def test_superusuario_alcanca_os_dois_mesmo_no_grupo_exclusivo(self):
        su = self.usuario("su", grupos=(GRUPO_SOMENTE_AGRO,), is_superuser=True, is_staff=True)
        self.assertEqual(amb.ambientes_de(su), (amb.TI, amb.AGRO))

    def test_staff_nao_passa_por_cima_do_exclusivo(self):
        staff = self.usuario("staff_agro", "ver_frota", grupos=(GRUPO_SOMENTE_AGRO,), is_staff=True)
        self.assertEqual(amb.ambientes_de(staff), (amb.AGRO,))

    def test_portais_e_perfis_restritos_ficam_fora(self):
        for grupo in (GRUPO_FORNECEDOR, GRUPO_PORTAL_CONTRATACAO):
            self.assertEqual(amb.ambientes_de(self.usuario(f"u_{grupo[:5]}", "ver_frota", grupos=(grupo,))), ())


class MiddlewareTests(AmbientesBase):
    def test_so_ti_nao_entra_no_agro(self):
        c = self.cliente(self.so_ti())
        self.assertEqual(c.get("/agro/").status_code, 403)
        self.assertEqual(c.get(reverse("frota_veiculos")).status_code, 403)
        self.assertEqual(c.post(reverse("frota_vincular"), {}).status_code, 403)

    def test_so_agro_nao_entra_no_ti(self):
        c = self.cliente(self.so_agro())
        self.assertEqual(c.get("/dashboard/").status_code, 403)
        self.assertEqual(c.get("/equipamentos/").status_code, 403)
        self.assertEqual(c.post("/movimentacoes/nova/", {}).status_code, 403)
        # a raiz leva ao Agro e as telas neutras (conta) continuam abertas
        self.assertRedirects(c.get("/"), reverse("agro_visao_geral"), fetch_redirect_response=False)
        self.assertEqual(c.get(reverse("admin_perfil")).status_code, 200)

    def test_quem_tem_os_dois_alterna_sem_novo_login(self):
        c = self.cliente(self.os_dois())
        agro = c.get(reverse("agro_visao_geral"))
        self.assertEqual(agro.status_code, 200)
        ti = c.get(reverse("dashboard"))
        self.assertEqual(ti.status_code, 200)
        # a mesma sessão serviu os dois — nenhum redirecionamento para o login
        self.assertEqual(c.get(reverse("agro_visao_geral")).status_code, 200)

    def test_view_do_agro_barra_mesmo_sem_o_middleware(self):
        """2ª camada: o decorador da view decide sozinho (esconder o link não é autorização)."""
        from django.test import RequestFactory
        from ProjetoEstoque.views.ambiente import agro_visao_geral
        from django.core.exceptions import PermissionDenied
        req = RequestFactory().get("/agro/")
        req.user = self.so_ti()
        with self.assertRaises(PermissionDenied):
            agro_visao_geral(req)

    def test_perfil_restrito_nao_e_afetado(self):
        user = self.usuario("rh", grupos=(GRUPO_PORTAL_CONTRATACAO,))
        r = self.cliente(user).get("/agro/")
        # quem decide é o middleware do perfil restrito (volta para a área dele), não o de ambiente
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r["Location"].startswith("/contratacoes/"))


class PreferenciaTests(AmbientesBase):
    def test_raiz_abre_no_ultimo_ambiente_usado_inclusive_em_nova_sessao(self):
        user = self.os_dois()
        c = self.cliente(user)
        c.get(reverse("agro_visao_geral"))
        self.assertEqual(PreferenciaAmbiente.objects.get(user=user).ambiente, "agro")
        self.assertRedirects(c.get("/"), reverse("agro_visao_geral"), fetch_redirect_response=False)
        # novo acesso (outra sessão, outro computador): a preferência está no banco
        self.assertRedirects(self.cliente(user).get("/"), reverse("agro_visao_geral"), fetch_redirect_response=False)
        # voltou para o TI: a raiz volta a ser o painel do TI
        c.get(reverse("dashboard"))
        self.assertEqual(PreferenciaAmbiente.objects.get(user=user).ambiente, "ti")
        self.assertEqual(c.get("/").status_code, 200)

    def test_post_e_ajax_nao_mudam_a_preferencia(self):
        user = self.os_dois()
        c = self.cliente(user)
        c.get(reverse("dashboard"))
        c.get(reverse("agro_visao_geral"), HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(PreferenciaAmbiente.objects.get(user=user).ambiente, "ti")

    def test_trocar_nao_concede(self):
        """Critério 13: quem não tem o Agro bate no /agro/, e nada muda — nem preferência, nem acesso."""
        user = self.so_ti()
        c = self.cliente(user)
        c.get("/agro/")
        self.assertFalse(PreferenciaAmbiente.objects.filter(user=user, ambiente="agro").exists())
        self.assertFalse(User.objects.get(pk=user.pk).has_perm("ProjetoEstoque.acessar_zelo_agro"))
        self.assertEqual(c.get("/").status_code, 200)  # a raiz continua sendo o TI

    def test_preferencia_por_ambiente_perdido_e_ignorada(self):
        user = self.so_ti()
        PreferenciaAmbiente.objects.create(user=user, ambiente="agro")  # tinha acesso, perdeu
        self.assertEqual(self.cliente(user).get("/").status_code, 200)


class ShellTests(AmbientesBase):
    def test_identidade_e_menu_seguem_a_rota(self):
        c = self.cliente(self.os_dois())
        agro = c.get(reverse("agro_visao_geral")).content.decode()
        ti = c.get(reverse("dashboard")).content.decode()
        self.assertIn('data-ambiente="agro"', agro)
        self.assertIn('data-ambiente="ti"', ti)
        # o menu do Agro tem Frotas e Chips e Conectividade como acessos independentes
        self.assertIn('data-submenu-target="sub-agro-frotas"', agro)
        self.assertIn('data-submenu-target="sub-agro-conectividade"', agro)
        self.assertIn(reverse("frota_veiculos"), agro)
        # nada do Agro no menu do TI, nada do TI no menu do Agro
        self.assertNotIn("sub-agro-", ti)
        self.assertNotIn('id="sub-m2m"', ti)
        self.assertNotIn('data-submenu-target="sub-dash"', agro)

    def test_tela_de_frota_veste_o_agro(self):
        c = self.cliente(self.os_dois())
        html = c.get(reverse("frota_veiculos")).content.decode()
        self.assertIn('data-ambiente="agro"', html)
        self.assertIn('class="sub-link is-active" aria-current="page">Veículos e equipamentos', html)

    def test_seletor_so_para_quem_tem_os_dois(self):
        dois = self.cliente(self.os_dois()).get(reverse("dashboard")).content.decode()
        self.assertIn("ambiente-seletor", dois)
        self.assertIn(f'href="{reverse("agro_visao_geral")}"', dois)
        um = self.cliente(self.so_ti()).get(reverse("dashboard")).content.decode()
        self.assertNotIn("ambiente-seletor", um)
        self.assertIn("ambiente-etiqueta", um)

    def test_menu_do_agro_respeita_a_permissao_de_cada_tela(self):
        so_chips = self.usuario("so_chips", "ver_chips")
        html = self.cliente(so_chips).get(reverse("m2m_visao_geral")).content.decode()
        self.assertIn("sub-agro-conectividade", html)
        self.assertNotIn("sub-agro-frotas", html)          # sem ver_frota, o grupo some
        self.assertNotIn(reverse("m2m_config"), html)      # sem administrar_frota_config

    def test_visoes_gerais_separadas_por_secao(self):
        c = self.cliente(self.os_dois())
        frota = c.get(reverse("frota_visao_geral")).content.decode()
        conect = c.get(reverse("m2m_visao_geral")).content.decode()
        self.assertIn('id="m2m-b-frota"', frota)
        self.assertNotIn('id="m2m-b-conect"', frota)
        self.assertIn('id="m2m-b-conect"', conect)
        self.assertNotIn('id="m2m-b-frota"', conect)
        self.assertIn('aria-label="Seções de Frotas"', frota)
        self.assertIn('aria-label="Seções de Chips e Conectividade"', conect)

    def test_visao_geral_leva_quem_so_ve_um_lado_para_o_seu(self):
        so_chips = self.cliente(self.usuario("so_chips2", "ver_chips"))
        self.assertRedirects(so_chips.get(reverse("frota_visao_geral")), reverse("m2m_visao_geral"),
                             fetch_redirect_response=False)
        so_frota = self.cliente(self.usuario("so_frota", "ver_frota"))
        self.assertRedirects(so_frota.get(reverse("m2m_visao_geral")), reverse("frota_visao_geral"),
                             fetch_redirect_response=False)


class CorDoMenuTests(TestCase):
    def test_cada_ambiente_tem_a_sua_cor_de_menu(self):
        """Menu lateral azul no TI e verde no Agro, em todos os estados (pedido de 2026-11-13).
        Por ID para vencer o trilho recolhido, que redefine os mesmos tokens."""
        from pathlib import Path
        from django.conf import settings
        css = (Path(settings.BASE_DIR) / "ProjetoEstoque" / "static" / "css" / "base.css").read_text(encoding="utf-8")
        import re
        ti = re.search(r'body:not\(\[data-ambiente="agro"\]\) #sidebar \{(.*?)\}', css, re.S)
        agro = re.search(r'body\[data-ambiente="agro"\] #sidebar \{(.*?)\}', css, re.S)
        self.assertIsNotNone(ti)
        self.assertIsNotNone(agro)
        self.assertIn("background: #1e40af", ti.group(1))
        self.assertIn("background: #123a28", agro.group(1))


class SinoTests(AmbientesBase):
    def test_sino_nao_mistura_os_ambientes(self):
        Notificacao.objects.create(titulo="Equipamento voltou da manutenção", categoria="manutencao")
        Notificacao.objects.create(titulo="Transferência aguardando aprovação", categoria="agro_transferencia")
        c = self.cliente(self.os_dois())
        ti = c.get(reverse("dashboard")).content.decode()
        agro = c.get(reverse("agro_visao_geral")).content.decode()
        self.assertIn("Equipamento voltou da manutenção", ti)
        self.assertNotIn("Transferência aguardando aprovação", ti)
        self.assertIn("Transferência aguardando aprovação", agro)
        self.assertNotIn("Equipamento voltou da manutenção", agro)

    def test_abrir_o_sino_no_agro_so_marca_as_do_agro(self):
        ti = Notificacao.objects.create(titulo="TI", categoria="manutencao")
        agro = Notificacao.objects.create(titulo="Agro", categoria="agro_x")
        self.cliente(self.os_dois()).post(reverse("notificacoes_marcar_lidas"), {"ambiente": "agro"})
        ti.refresh_from_db()
        agro.refresh_from_db()
        self.assertFalse(ti.lida)
        self.assertTrue(agro.lida)


class RotasLegadasTests(AmbientesBase):
    """Todo endereço antigo de Frotas e Conectividade chega ao mesmo registro no endereço novo."""

    PARAMETROS = {"pk": 7, "vinculo_id": 9, "tipo": "sem_sessao"}

    def _antigos(self):
        for padrao in urls_legado_frota.urlpatterns:
            kwargs = {k: self.PARAMETROS[k] for k in padrao.pattern.converters}
            antigo = "/" + str(padrao.pattern)
            for nome, valor in kwargs.items():
                antigo = antigo.replace(f"<int:{nome}>", str(valor)).replace(f"<str:{nome}>", str(valor))
            yield antigo, padrao.name, kwargs

    def test_cada_endereco_antigo_vai_para_o_novo_pelo_nome(self):
        c = self.cliente(self.os_dois())
        vistos = 0
        for antigo, nome, kwargs in self._antigos():
            novo = reverse(nome, kwargs=kwargs)
            self.assertTrue(novo.startswith("/agro/"), novo)
            r = c.get(antigo)
            self.assertEqual(r.status_code, 308, antigo)
            self.assertEqual(r["Location"], novo, antigo)
            vistos += 1
        self.assertEqual(vistos, len(urls_legado_frota.urlpatterns))

    def test_mantem_querystring_e_metodo(self):
        c = self.cliente(self.os_dois())
        r = c.get("/m2m/chips/?situacao_uso=vinculado&q=abc")
        self.assertEqual(r["Location"], reverse("m2m_chips") + "?situacao_uso=vinculado&q=abc")
        r = c.post("/frotas/vincular/", {"chip": "1"})
        self.assertEqual(r.status_code, 308)  # 308 = o navegador repete o POST no endereço novo
        self.assertEqual(r["Location"], reverse("frota_vincular"))

    def test_antigo_sem_equivalente_e_404(self):
        self.assertEqual(self.cliente(self.os_dois()).get("/frotas/nao-existe/").status_code, 404)

    def test_todas_as_rotas_do_modulo_moram_no_agro(self):
        for _, nome, kwargs in self._antigos():
            self.assertTrue(reverse(nome, kwargs=kwargs).startswith("/agro/"), nome)
            self.assertEqual(resolve(reverse(nome, kwargs=kwargs)).url_name, nome)

    def test_quem_so_tem_ti_tambem_nao_passa_pelo_endereco_antigo(self):
        self.assertEqual(self.cliente(self.so_ti()).get("/frotas/veiculos/").status_code, 403)
