"""
Cadastro rápido de subtipo e localidade no formulário de item (pedido de 2026-11-14).

Quem cadastra um equipamento e não acha o subtipo ou a localidade cadastra ali mesmo, num popup, sem sair do formulário
nem perder o que já digitou. Travado aqui:

  • o registro nasce com o autor e volta para o campo (id + o texto do <option>);
  • NÃO DUPLICA: nome igual — sem diferenciar maiúscula, acento nem espaço repetido — devolve o que já existe
    (subtipo: dentro da mesma categoria; em outra categoria é outro subtipo);
  • entrada inválida volta 400 com o erro por campo e NADA é gravado;
  • só POST, só logado;
  • o formulário de cadastro E o de edição trazem os dois botões e os dois popups, e os popups ficam FORA do
    <form id="itemForm"> (<form> dentro de <form> é inválido: o navegador descarta o interno e o popup "enviava" o item).
"""
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from ProjetoEstoque.models import Categoria, Item, Localidade, Subtipo
from services import cadastro_rapido_service as rapido


class CadastroRapidoBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="operador_ti", password="x")
        self.client.force_login(self.user)
        self.rede = Categoria.objects.create(nome="Rede")
        self.energia = Categoria.objects.create(nome="Energia")

    def subtipo(self, **dados):
        dados = {"nome": "Switch", "categoria": self.rede.pk, "alocado": "nao", **dados}
        return self.client.post(reverse("subtipo_cadastro_rapido"), dados)

    def localidade(self, **dados):
        return self.client.post(reverse("localidade_cadastro_rapido"), {"local": "Karitel", **dados})


class SubtipoTests(CadastroRapidoBase):
    def test_cria_e_devolve_o_texto_do_option(self):
        r = self.subtipo()
        self.assertEqual(r.status_code, 200)
        d = r.json()
        sub = Subtipo.objects.get(pk=d["id"])
        self.assertEqual((d["ok"], d["texto"], d["ja_existia"]), (True, "Switch (Rede)", False))
        self.assertEqual(d["texto"], str(sub))                                   # o mesmo texto que o <option> do formulário
        self.assertEqual((sub.criado_por, sub.atualizado_por, sub.alocado), (self.user, self.user, "nao"))
        self.assertFalse(sub.aceita_chip or sub.e_chip)                          # o popup não decide o que é raro

    def test_nome_repetido_na_mesma_categoria_devolve_o_existente(self):
        primeiro = self.subtipo(nome="Switch").json()
        for variacao in ("switch", "  SWITCH ", "Swítch", "Sw   itch".replace("   ", "")):
            d = self.subtipo(nome=variacao).json()
            self.assertEqual((d["ok"], d["id"], d["ja_existia"]), (True, primeiro["id"], True), variacao)
        self.assertEqual(Subtipo.objects.count(), 1)

    def test_o_mesmo_nome_em_outra_categoria_e_outro_subtipo(self):
        a = self.subtipo(nome="Cabo", categoria=self.rede.pk).json()
        b = self.subtipo(nome="Cabo", categoria=self.energia.pk).json()
        self.assertNotEqual(a["id"], b["id"])
        self.assertFalse(b["ja_existia"])
        self.assertEqual(Subtipo.objects.filter(nome="Cabo").count(), 2)

    def test_invalido_devolve_400_com_o_erro_por_campo_e_nao_grava(self):
        r = self.subtipo(nome="", categoria="")
        self.assertEqual(r.status_code, 400)
        d = r.json()
        self.assertFalse(d["ok"])
        self.assertIn("nome", d["erros"])
        self.assertIn("categoria", d["erros"])
        self.assertEqual(Subtipo.objects.count(), 0)

    def test_categoria_inexistente_e_recusada(self):
        r = self.subtipo(categoria=999999)
        self.assertEqual(r.status_code, 400)
        self.assertIn("categoria", r.json()["erros"])
        self.assertEqual(Subtipo.objects.count(), 0)

    def test_alocado_invalido_e_recusado(self):
        r = self.subtipo(alocado="talvez")
        self.assertEqual(r.status_code, 400)
        self.assertIn("alocado", r.json()["erros"])


class LocalidadeTests(CadastroRapidoBase):
    def test_cria_e_devolve_o_nome(self):
        d = self.localidade().json()
        loc = Localidade.objects.get(pk=d["id"])
        self.assertEqual((d["ok"], d["texto"], d["ja_existia"]), (True, "Karitel", False))
        self.assertEqual((loc.local, loc.criado_por, loc.atualizado_por), ("Karitel", self.user, self.user))

    def test_nome_repetido_devolve_a_existente(self):
        primeira = self.localidade(local="Fazenda Pamplona").json()
        for variacao in ("fazenda pamplona", " FAZENDA   PAMPLONA ", "Fazenda Pámplona"):
            d = self.localidade(local=variacao).json()
            self.assertEqual((d["id"], d["ja_existia"]), (primeira["id"], True), variacao)
        self.assertEqual(Localidade.objects.count(), 1)

    def test_vazio_devolve_400_e_nao_grava(self):
        for vazio in ("", "   "):
            r = self.localidade(local=vazio)
            self.assertEqual(r.status_code, 400, repr(vazio))
            self.assertIn("local", r.json()["erros"])
        self.assertEqual(Localidade.objects.count(), 0)


class PortaTests(CadastroRapidoBase):
    def test_so_aceita_post(self):
        for nome in ("subtipo_cadastro_rapido", "localidade_cadastro_rapido"):
            self.assertEqual(self.client.get(reverse(nome)).status_code, 405, nome)

    def test_exige_login(self):
        self.client.logout()
        for nome in ("subtipo_cadastro_rapido", "localidade_cadastro_rapido"):
            r = self.client.post(reverse(nome), {"local": "X", "nome": "X"})
            self.assertEqual(r.status_code, 302, nome)
            self.assertIn("login", r["Location"])
        self.assertEqual((Subtipo.objects.count(), Localidade.objects.count()), (0, 0))

    def test_o_service_e_a_mesma_regra_da_view(self):
        r = rapido.criar_localidade({"local": "Sede"}, self.user)
        self.assertTrue(r.ok)
        self.assertTrue(rapido.criar_localidade({"local": "sede"}, self.user).ja_existia)


class FormularioDeItemTests(CadastroRapidoBase):
    def paginas(self):
        item = Item.objects.create(nome="Notebook A", subtipo=Subtipo.objects.create(nome="Notebook", categoria=self.rede, alocado="sim"))
        return {
            "cadastro": reverse("cadastrar_equipamento"),
            "edição": reverse("item_update", args=[item.pk]),
        }

    def test_os_dois_botoes_e_os_dois_popups_aparecem_no_cadastro_e_na_edicao(self):
        for rotulo, url in self.paginas().items():
            html = self.client.get(url).content.decode()
            for alvo in ("cr-subtipo", "cr-localidade"):
                self.assertIn(f'data-cadastro-rapido="{alvo}"', html, f"{rotulo}: botão de {alvo}")
                self.assertIn(f'<dialog class="ui-dialogo" id="{alvo}"', html, f"{rotulo}: popup {alvo}")
            self.assertIn('data-alvo="id_subtipo"', html)
            self.assertIn('data-alvo="id_localidade"', html)
            self.assertIn(reverse("subtipo_cadastro_rapido"), html)
            self.assertIn(reverse("localidade_cadastro_rapido"), html)
            self.assertIn("js/cadastro-rapido.js", html)

    def test_os_popups_ficam_fora_do_formulario_do_item(self):
        for rotulo, url in self.paginas().items():
            html = self.client.get(url).content.decode()
            abre = html.index('id="itemForm"')
            fecha = html.index("</form>", abre)
            for alvo in ("cr-subtipo", "cr-localidade"):
                self.assertGreater(html.index(f'id="{alvo}"'), fecha, f"{rotulo}: {alvo} está DENTRO do <form> do item")

    def test_o_popup_de_subtipo_lista_as_categorias(self):
        html = self.client.get(reverse("cadastrar_equipamento")).content.decode()
        popup = html[html.index('id="cr-subtipo"'):html.index('id="cr-localidade"')]
        self.assertIn(f'<option value="{self.rede.pk}">Rede</option>', popup)
        self.assertIn(f'<option value="{self.energia.pk}">Energia</option>', popup)

    def test_sem_categoria_o_popup_explica_o_que_fazer(self):
        Categoria.objects.all().delete()
        html = self.client.get(reverse("cadastrar_equipamento")).content.decode()
        popup = html[html.index('id="cr-subtipo"'):html.index('id="cr-localidade"')]
        self.assertIn("Nenhuma categoria cadastrada", popup)
        self.assertIn(reverse("categorias_list"), popup)

    def test_o_script_estatico_conversa_com_os_atributos_que_o_template_entrega(self):
        js = (Path(settings.BASE_DIR) / "ProjetoEstoque" / "static" / "js" / "cadastro-rapido.js").read_text(encoding="utf-8")
        for atributo in ("data-cadastro-rapido", "data-cadastro-rapido-form", "data-erro-geral", "data-enviar", "data-fechar",
                         "dataset.url", "dataset.alvo"):
            self.assertIn(atributo, js)
        # URL nunca em arquivo estático: ele não passa pelo motor de template (CLAUDE.md, "Telas grandes")
        self.assertNotIn("{%", js)
        self.assertNotIn("/subtipos/", js)

    def test_o_popup_centraliza_por_conta_propria_e_nao_depende_do_padrao_do_navegador(self):
        """O reset do base.css zera as margens (`* { margin: 0 }`); sem `margin: auto` o <dialog> abria colado no canto
        superior esquerdo. A primeira correção (só o `margin: auto`) foi servida sem subir a versão dos estáticos e quem já
        tinha a folha em cache continuou vendo o canto. Hoje a regra declara os quatro valores que centralizam."""
        css = (Path(settings.BASE_DIR) / "ProjetoEstoque" / "static" / "css" / "ui-kit.css").read_text(encoding="utf-8")
        inicio = css.index("\n.ui-dialogo {")
        regra = css[inicio:css.index("}", inicio)]
        for declaracao in ("position: fixed;", "inset: 0;", "margin: auto;", "height: fit-content;"):
            self.assertIn(declaracao, regra)
