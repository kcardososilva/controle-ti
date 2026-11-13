"""
Telas do estoque tecnológico e do catálogo do Zelo Agro: acesso por permissão (validado no backend),
formulários de operação com token de envio único, erro por campo, escape de HTML, o menu do Agro e as
seis listas do catálogo.
"""

import re
import uuid
from decimal import Decimal

from django.contrib.auth.models import Permission, User
from django.test import Client, TestCase
from django.urls import reverse

from ProjetoEstoque.models import (
    AgroLocal,
    TecCategoria,
    TecEquipamento,
    TecFabricante,
    TecModelo,
    TecMovimentacao,
    TecSaldo,
)
from services import frota_service
from services import tec_catalogo_service as cat
from services import tec_estoque_service as est


class TelasBase(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("tec_admin", "a@a.com", "x")
        self.karitel, self.rio = frota_service.fazendas()
        self.deposito = cat.salvar_local({"nome": "Almoxarifado", "fazenda": self.karitel}, self.admin)
        self.oficina = cat.salvar_local({"nome": "Oficina", "fazenda": self.rio}, self.admin)
        self.deere = TecFabricante.objects.get(nome="John Deere")
        self.monitor = cat.salvar_modelo(
            {"fabricante": self.deere, "categoria": TecCategoria.objects.get(nome="Monitor / display"), "nome": "G5", "controle": "individual", "estoque_minimo": 2},
            self.admin,
        )
        self.cabo = cat.salvar_modelo(
            {"fabricante": TecFabricante.objects.get(nome="Motorola"), "categoria": TecCategoria.objects.get(nome="Cabo"), "nome": "Cabo UTP",
             "controle": "quantidade", "unidade": cat.listar_unidades().get(sigla="m"), "estoque_minimo": 100},
            self.admin,
        )
        self.eq = est.criar_equipamento({"modelo": self.monitor, "numero_serie": "SN-1", "local": self.deposito}, self.admin).equipamento
        est.registrar_entrada_material(self.cabo, self.deposito, 40, self.admin)
        self.client = Client()
        self.client.force_login(self.admin)

    def usuario_com(self, login, *codenames):
        user = User.objects.create_user(login, f"{login}@t.com", "x")
        user.user_permissions.add(*Permission.objects.filter(codename__in=codenames))
        c = Client()
        c.force_login(user)
        return user, c


# ═════════════════════════════════════════════════════════════
# 1. Acesso
# ═════════════════════════════════════════════════════════════

class AcessoTests(TelasBase):
    def urls_de_leitura(self):
        return [
            reverse("tec_visao_geral"), reverse("tec_equipamentos"), reverse("tec_equipamento_detalhe", args=[self.eq.pk]),
            reverse("tec_materiais"), reverse("tec_material_detalhe", args=[self.cabo.pk]),
            reverse("tec_catalogo_inicio"), *[reverse("tec_catalogo", args=[t]) for t in ("modelos", "fabricantes", "categorias", "unidades", "locais", "fornecedores")],
        ]

    def urls_de_escrita(self):
        return [
            reverse("tec_equipamento_novo"), reverse("tec_equipamento_editar", args=[self.eq.pk]), reverse("tec_equipamento_mover", args=[self.eq.pk]),
            reverse("tec_equipamento_inativar", args=[self.eq.pk]), reverse("tec_material_entrada_nova"), reverse("tec_material_entrada", args=[self.cabo.pk]),
            reverse("tec_material_saida", args=[self.cabo.pk]), reverse("tec_material_ajuste", args=[self.cabo.pk]), reverse("tec_material_transferir", args=[self.cabo.pk]),
            reverse("tec_catalogo_novo", args=["modelos"]), reverse("tec_catalogo_editar", args=["modelos", self.monitor.pk]),
        ]

    def test_anonimo_vai_para_o_login(self):
        for url in self.urls_de_leitura() + self.urls_de_escrita() + [reverse("tec_movimentacoes")]:
            with self.subTest(url=url):
                r = Client().get(url)
                self.assertEqual(r.status_code, 302)
                self.assertIn("/login", r["Location"])

    def test_usuario_sem_permissao_recebe_403_em_tudo(self):
        _, c = self.usuario_com("sem_nada")
        for url in self.urls_de_leitura() + self.urls_de_escrita() + [reverse("tec_movimentacoes")]:
            with self.subTest(url=url):
                self.assertEqual(c.get(url).status_code, 403)

    def test_so_ver_estoque_le_mas_nao_escreve_nem_ve_o_historico(self):
        _, c = self.usuario_com("so_ve", "ver_tec_estoque")
        for url in self.urls_de_leitura():
            with self.subTest(url=url):
                self.assertEqual(c.get(url).status_code, 200)
        for url in self.urls_de_escrita():
            with self.subTest(url=url):
                self.assertEqual(c.get(url).status_code, 403)
                self.assertEqual(c.post(url, {}).status_code, 403)
        self.assertEqual(c.get(reverse("tec_movimentacoes")).status_code, 403)

    def test_botoes_so_aparecem_para_quem_pode(self):
        _, so_ve = self.usuario_com("so_ve2", "ver_tec_estoque")
        html = so_ve.get(reverse("tec_equipamentos")).content.decode()
        self.assertNotIn(reverse("tec_equipamento_novo"), html)
        self.assertNotIn(reverse("tec_equipamento_editar", args=[self.eq.pk]), html)
        detalhe = so_ve.get(reverse("tec_equipamento_detalhe", args=[self.eq.pk])).content.decode()
        self.assertNotIn(reverse("tec_equipamento_mover", args=[self.eq.pk]), detalhe)
        self.assertNotIn(reverse("tec_equipamento_inativar", args=[self.eq.pk]), detalhe)
        _, gestor = self.usuario_com("gestor", "ver_tec_estoque", "cadastrar_tec_equipamento", "transferir_tec_equipamento")
        detalhe = gestor.get(reverse("tec_equipamento_detalhe", args=[self.eq.pk])).content.decode()
        for destino in ("tec_equipamento_mover", "tec_equipamento_inativar", "tec_equipamento_editar"):
            self.assertIn(reverse(destino, args=[self.eq.pk]), detalhe)

    def test_permissao_de_cada_cadastro_do_catalogo(self):
        _, catalogo = self.usuario_com("cat", "ver_tec_estoque", "administrar_tec_catalogo")
        for tipo in ("modelos", "fabricantes", "categorias", "fornecedores"):
            self.assertEqual(catalogo.get(reverse("tec_catalogo_novo", args=[tipo])).status_code, 200, tipo)
        for tipo in ("unidades", "locais"):                                # precisam da permissão de configuração
            self.assertEqual(catalogo.get(reverse("tec_catalogo_novo", args=[tipo])).status_code, 403, tipo)
        _, config = self.usuario_com("cfg", "ver_tec_estoque", "administrar_tec_config")
        for tipo in ("unidades", "locais"):
            self.assertEqual(config.get(reverse("tec_catalogo_novo", args=[tipo])).status_code, 200, tipo)
        self.assertEqual(config.get(reverse("tec_catalogo_novo", args=["modelos"])).status_code, 403)

    def test_catalogo_inexistente_e_404(self):
        self.assertEqual(self.client.get(reverse("tec_catalogo", args=["inventado"])).status_code, 404)
        self.assertEqual(self.client.get(reverse("tec_catalogo_novo", args=["inventado"])).status_code, 404)

    def test_permissao_de_tecnologia_abre_o_ambiente_agro(self):
        """Quem só tem permissão do módulo (sem `acessar_zelo_agro`) alcança o ambiente — senão ela não teria onde ser usada."""
        _, c = self.usuario_com("so_tec", "ver_tec_estoque")
        self.assertEqual(c.get(reverse("tec_visao_geral")).status_code, 200)


# ═════════════════════════════════════════════════════════════
# 2. As telas renderizam
# ═════════════════════════════════════════════════════════════

class TelasRenderizamTests(TelasBase):
    def test_visao_geral_mostra_os_numeros_e_a_falta(self):
        html = self.client.get(reverse("tec_visao_geral")).content.decode()
        self.assertIn("Estoque tecnológico", html)
        self.assertIn("Abaixo do estoque mínimo", html)        # cabo 40 < 100 e monitor 1 < 2
        self.assertIn("Cabo UTP", html)
        self.assertIn("Últimas movimentações", html)

    def test_lista_de_equipamentos_filtros_e_segmentos(self):
        outro = est.criar_equipamento({"modelo": self.monitor, "numero_serie": "SN-2", "local": self.oficina}, self.admin).equipamento
        est.inativar_equipamento(outro, self.admin, "baixa")
        url = reverse("tec_equipamentos")
        html = self.client.get(url).content.decode()
        self.assertIn("SN-1", html)
        self.assertIn("SN-2", html)
        self.assertNotIn("SN-2", self.client.get(url, {"situacao": "disponivel"}).content.decode())
        self.assertIn("SN-2", self.client.get(url, {"situacao": "inativo"}).content.decode())
        self.assertIn("SN-1", self.client.get(url, {"q": "sn-1"}).content.decode())
        self.assertNotIn("SN-1", self.client.get(url, {"q": "nao-existe"}).content.decode())
        self.assertIn("Nenhum resultado", self.client.get(url, {"q": "nao-existe"}).content.decode())
        self.assertEqual(self.client.get(url, {"fabricante": "abc"}).status_code, 200)      # id inválido nunca estoura

    def test_detalhe_do_equipamento_mostra_historico(self):
        est.mover_equipamento_de_local(self.eq, self.oficina, self.admin, "mudança de base")
        html = self.client.get(reverse("tec_equipamento_detalhe", args=[self.eq.pk])).content.decode()
        self.assertIn(self.eq.codigo, html)
        self.assertIn("Transferência", html)
        self.assertIn("mudança de base", html)
        self.assertIn("Entrada no estoque", html)

    def test_lista_de_materiais_e_detalhe(self):
        html = self.client.get(reverse("tec_materiais")).content.decode()
        self.assertIn("Cabo UTP", html)
        self.assertIn("em falta", html)
        detalhe = self.client.get(reverse("tec_material_detalhe", args=[self.cabo.pk])).content.decode()
        self.assertIn("Saldo por local", detalhe)
        self.assertIn("Almoxarifado", detalhe)
        self.assertEqual(self.client.get(reverse("tec_material_detalhe", args=[self.monitor.pk])).status_code, 404)    # individual não tem tela de material
        abaixo = self.client.get(reverse("tec_materiais"), {"abaixo_minimo": "1"}).content.decode()
        self.assertIn("Cabo UTP", abaixo)

    def test_quantidade_sem_zeros_a_direita_e_com_virgula(self):
        from ProjetoEstoque.templatetags.tec_tags import qtd
        for entrada, esperado in ((Decimal("130.500"), "130,5"), ("40.000", "40"), (Decimal("0.250"), "0,25"), (0, "0"), (None, "—"), ("", "—"), ("abc", "abc")):
            with self.subTest(entrada=entrada):
                self.assertEqual(qtd(entrada), esperado)
        est.registrar_entrada_material(self.cabo, self.oficina, "0,5", self.admin)
        html = self.client.get(reverse("tec_material_detalhe", args=[self.cabo.pk])).content.decode()
        self.assertIn("40,5", html)
        self.assertNotIn("40,500", html)

    def test_movimentacoes_listam_e_filtram(self):
        url = reverse("tec_movimentacoes")
        html = self.client.get(url).content.decode()
        self.assertIn("Entrada no estoque", html)
        self.assertIn(self.eq.codigo, html)
        self.assertNotIn("Cabo UTP", self.client.get(url, {"q": self.eq.codigo}).content.decode())
        self.assertEqual(self.client.get(url, {"de": "lixo"}).status_code, 200)
        self.assertEqual(self.client.get(url, {"tipo": "inventado"}).status_code, 200)

    def test_as_seis_listas_do_catalogo_e_o_formulario_de_cada_uma(self):
        for tipo in ("modelos", "fabricantes", "categorias", "unidades", "locais", "fornecedores"):
            with self.subTest(tipo=tipo):
                lista = self.client.get(reverse("tec_catalogo", args=[tipo]))
                self.assertEqual(lista.status_code, 200)
                novo = self.client.get(reverse("tec_catalogo_novo", args=[tipo]))
                self.assertEqual(novo.status_code, 200)
                self.assertIn("<form", novo.content.decode())

    def test_html_do_usuario_sai_escapado(self):
        modelo = cat.salvar_modelo({"fabricante": self.deere, "categoria": TecCategoria.objects.get(nome="Monitor / display"), "nome": "<script>alert(1)</script>"}, self.admin)
        est.criar_equipamento({"modelo": modelo, "numero_serie": "<b>x</b>", "local": self.deposito, "observacoes": "<img src=x onerror=alert(1)>"}, self.admin)
        for url in (reverse("tec_equipamentos"), reverse("tec_catalogo", args=["modelos"]), reverse("tec_movimentacoes")):
            html = self.client.get(url).content.decode()
            self.assertNotIn("<script>alert(1)</script>", html)
            self.assertNotIn("<img src=x", html)
        eq = TecEquipamento.objects.get(numero_serie="<b>x</b>")
        detalhe = self.client.get(reverse("tec_equipamento_detalhe", args=[eq.pk])).content.decode()
        self.assertNotIn("<img src=x", detalhe)
        self.assertIn("&lt;img src=x", detalhe)


# ═════════════════════════════════════════════════════════════
# 3. Formulários de operação
# ═════════════════════════════════════════════════════════════

class FormulariosTests(TelasBase):
    def token_da_tela(self, url):
        html = self.client.get(url).content.decode()
        achou = re.search(r'name="token" value="([0-9a-f-]{36})"', html)
        self.assertIsNotNone(achou, "o formulário de operação precisa de um token de envio único")
        return achou.group(1)

    def test_cadastro_de_equipamento_pela_tela(self):
        url = reverse("tec_equipamento_novo")
        token = self.token_da_tela(url)
        dados = {"token": token, "modelo": self.monitor.pk, "numero_serie": "SN-NOVO", "local": self.oficina.pk, "documento": "NF 7"}
        r = self.client.post(url, dados)
        eq = TecEquipamento.objects.get(numero_serie="SN-NOVO")
        self.assertRedirects(r, reverse("tec_equipamento_detalhe", args=[eq.pk]))
        self.assertEqual((eq.local, eq.situacao), (self.oficina, "disponivel"))
        mov = TecMovimentacao.objects.get(equipamento=eq)
        self.assertEqual((mov.tipo, mov.documento, mov.token), ("entrada", "NF 7", uuid.UUID(token)))

    def test_duplo_envio_com_o_mesmo_token_nao_duplica(self):
        url = reverse("tec_equipamento_novo")
        dados = {"token": str(uuid.uuid4()), "modelo": self.monitor.pk, "numero_serie": "SN-DUP", "local": self.deposito.pk}
        self.client.post(url, dados)
        r = self.client.post(url, dados, follow=True)
        self.assertEqual(TecEquipamento.objects.filter(numero_serie="SN-DUP").count(), 1)
        self.assertEqual(TecMovimentacao.objects.filter(equipamento__numero_serie="SN-DUP").count(), 1)
        self.assertContains(r, "já tinha sido registrada")

    def test_erro_de_validacao_volta_400_com_o_erro_no_campo_e_preserva_o_que_foi_digitado(self):
        url = reverse("tec_equipamento_novo")
        r = self.client.post(url, {"token": str(uuid.uuid4()), "modelo": self.monitor.pk, "numero_serie": "SN-1", "local": self.deposito.pk, "observacoes": "texto que não pode sumir"})
        self.assertEqual(r.status_code, 400)
        html = r.content.decode()
        self.assertIn("Já existe um equipamento John Deere com este número de série", html)
        self.assertIn("texto que não pode sumir", html)
        self.assertIn('value="SN-1"', html)
        self.assertEqual(TecEquipamento.objects.count(), 1)

    def test_editar_nao_muda_local_nem_situacao(self):
        url = reverse("tec_equipamento_editar", args=[self.eq.pk])
        r = self.client.post(url, {"modelo": self.monitor.pk, "numero_serie": "SN-1-CORRIGIDO", "patrimonio": "", "data_aquisicao": "", "fornecedor": "", "observacoes": "ok"})
        self.assertRedirects(r, reverse("tec_equipamento_detalhe", args=[self.eq.pk]))
        self.eq.refresh_from_db()
        self.assertEqual((self.eq.numero_serie, self.eq.local, self.eq.situacao), ("SN-1-CORRIGIDO", self.deposito, "disponivel"))

    def test_mover_e_inativar_pela_tela(self):
        mover = reverse("tec_equipamento_mover", args=[self.eq.pk])
        sem_motivo = self.client.post(mover, {"token": str(uuid.uuid4()), "local": self.oficina.pk, "motivo": ""})
        self.assertEqual(sem_motivo.status_code, 400)
        self.assertContains(sem_motivo, "Informe o motivo", status_code=400)
        ok = self.client.post(mover, {"token": str(uuid.uuid4()), "local": self.oficina.pk, "motivo": "reforço"})
        self.assertEqual(ok.status_code, 302)
        self.eq.refresh_from_db()
        self.assertEqual(self.eq.local, self.oficina)
        inativar = reverse("tec_equipamento_inativar", args=[self.eq.pk])
        self.assertEqual(self.client.post(inativar, {"token": str(uuid.uuid4()), "motivo": "queimou"}).status_code, 302)
        self.eq.refresh_from_db()
        self.assertEqual(self.eq.situacao, "inativo")
        # o botão some e a rota recusa o que já está inativo
        self.assertNotIn(reverse("tec_equipamento_inativar", args=[self.eq.pk]), self.client.get(reverse("tec_equipamento_detalhe", args=[self.eq.pk])).content.decode())
        self.assertEqual(self.client.post(inativar, {"token": str(uuid.uuid4()), "motivo": "de novo"}).status_code, 400)

    def test_entrada_saida_ajuste_e_transferencia_de_material(self):
        entrada = reverse("tec_material_entrada", args=[self.cabo.pk])
        self.assertEqual(self.client.post(entrada, {"token": str(uuid.uuid4()), "local": self.deposito.pk, "quantidade": "60,5", "documento": "NF 9"}).status_code, 302)
        self.assertEqual(TecSaldo.objects.get(modelo=self.cabo, local=self.deposito).quantidade, Decimal("100.5"))
        saida = reverse("tec_material_saida", args=[self.cabo.pk])
        sem_saldo = self.client.post(saida, {"token": str(uuid.uuid4()), "local": self.deposito.pk, "quantidade": "1000", "motivo": "x"})
        self.assertEqual(sem_saldo.status_code, 400)
        self.assertContains(sem_saldo, "Saldo insuficiente", status_code=400)
        self.assertEqual(self.client.post(saida, {"token": str(uuid.uuid4()), "local": self.deposito.pk, "quantidade": "0,5", "motivo": "consumo", "destino": "Torre"}).status_code, 302)
        ajuste = reverse("tec_material_ajuste", args=[self.cabo.pk])
        self.assertEqual(self.client.post(ajuste, {"token": str(uuid.uuid4()), "local": self.deposito.pk, "quantidade": "90", "motivo": "contagem"}).status_code, 302)
        self.assertEqual(TecSaldo.objects.get(modelo=self.cabo, local=self.deposito).quantidade, Decimal("90"))
        transferir = reverse("tec_material_transferir", args=[self.cabo.pk])
        self.assertEqual(self.client.post(transferir, {"token": str(uuid.uuid4()), "local_origem": self.deposito.pk, "local_destino": self.oficina.pk, "quantidade": "30", "motivo": "reforço"}).status_code, 302)
        self.assertEqual(TecSaldo.objects.get(modelo=self.cabo, local=self.oficina).quantidade, Decimal("30"))
        self.assertTrue(est.conferir_consistencia()["ok"])

    def test_entrada_sem_material_fixo_escolhe_o_modelo(self):
        url = reverse("tec_material_entrada_nova")
        self.assertIn("Escolher o material", self.client.get(url).content.decode())
        r = self.client.post(url, {"token": str(uuid.uuid4()), "modelo": self.cabo.pk, "local": self.oficina.pk, "quantidade": "5"})
        self.assertRedirects(r, reverse("tec_material_detalhe", args=[self.cabo.pk]))
        individual = self.client.post(url, {"token": str(uuid.uuid4()), "modelo": self.monitor.pk, "local": self.oficina.pk, "quantidade": "5"})
        self.assertEqual(individual.status_code, 400)       # modelo individual não recebe entrada de material

    def test_voltar_so_aceita_caminho_do_modulo(self):
        url = reverse("tec_equipamento_mover", args=[self.eq.pk])
        base = {"token": str(uuid.uuid4()), "local": self.oficina.pk, "motivo": "x"}
        r = self.client.post(url, {**base, "voltar": "//evil.example/roubo"})
        self.assertEqual(r["Location"], reverse("tec_equipamento_detalhe", args=[self.eq.pk]))
        outro = est.criar_equipamento({"modelo": self.monitor, "numero_serie": "SN-V", "local": self.deposito}, self.admin).equipamento
        r = self.client.post(reverse("tec_equipamento_mover", args=[outro.pk]), {**base, "token": str(uuid.uuid4()), "voltar": "/agro/estoque/equipamentos/?q=sn"})
        self.assertEqual(r["Location"], "/agro/estoque/equipamentos/?q=sn")

    def test_get_nao_grava_nada(self):
        antes = (TecEquipamento.objects.count(), TecMovimentacao.objects.count())
        for nome, args in (("tec_equipamento_novo", []), ("tec_equipamento_mover", [self.eq.pk]), ("tec_equipamento_inativar", [self.eq.pk]), ("tec_material_saida", [self.cabo.pk])):
            self.assertEqual(self.client.get(reverse(nome, args=args)).status_code, 200)
        self.assertEqual(antes, (TecEquipamento.objects.count(), TecMovimentacao.objects.count()))

    def test_catalogo_cria_edita_e_inativa_pela_tela(self):
        url = reverse("tec_catalogo_novo", args=["fabricantes"])
        self.assertEqual(self.client.post(url, {"nome": "Trimble"}).status_code, 302)
        fab = TecFabricante.objects.get(nome="Trimble")
        edit = reverse("tec_catalogo_editar", args=["fabricantes", fab.pk])
        self.assertIn('name="ativo"', self.client.get(edit).content.decode())
        self.client.post(edit, {"nome": "Trimble"})                       # checkbox desmarcado = inativar
        fab.refresh_from_db()
        self.assertFalse(fab.ativo)
        self.client.post(edit, {"nome": "Trimble", "ativo": "1"})
        fab.refresh_from_db()
        self.assertTrue(fab.ativo)
        repetido = self.client.post(url, {"nome": "trimble"})
        self.assertEqual(repetido.status_code, 400)
        self.assertContains(repetido, "Já existe um fabricante", status_code=400)

    def test_modelo_pela_tela_e_unidade_do_material(self):
        url = reverse("tec_catalogo_novo", args=["modelos"])
        r = self.client.post(url, {"fabricante": self.deere.pk, "categoria": TecCategoria.objects.get(nome="Conector").pk, "nome": "RJ45", "controle": "", "unidade": cat.listar_unidades().get(sigla="un").pk, "estoque_minimo": "10", "observacoes": ""})
        self.assertEqual(r.status_code, 302)
        modelo = TecModelo.objects.get(nome="RJ45")
        self.assertEqual((modelo.controle, modelo.unidade.sigla, modelo.estoque_minimo), ("quantidade", "un", Decimal("10")))    # o controle veio do padrão da categoria
        sem_unidade = self.client.post(url, {"fabricante": self.deere.pk, "categoria": TecCategoria.objects.get(nome="Conector").pk, "nome": "Sem unidade", "controle": "quantidade", "unidade": ""})
        self.assertEqual(sem_unidade.status_code, 400)
        self.assertContains(sem_unidade, "Escolha a unidade", status_code=400)


# ═════════════════════════════════════════════════════════════
# 4. Menu do Agro
# ═════════════════════════════════════════════════════════════

class MenuDoAgroTests(TelasBase):
    def menu(self, cliente):
        return cliente.get(reverse("agro_visao_geral")).content.decode()

    def test_grupo_aparece_com_a_permissao_e_cada_item_com_a_sua(self):
        _, so_ve = self.usuario_com("menu_ve", "ver_tec_estoque")
        html = self.menu(so_ve)
        self.assertIn('data-submenu-target="sub-agro-estoque"', html)
        for nome in ("tec_visao_geral", "tec_equipamentos", "tec_materiais", "tec_catalogo_inicio"):
            self.assertIn(f'href="{reverse(nome)}" class="sub-link', html)
        self.assertNotIn(f'href="{reverse("tec_movimentacoes")}" class="sub-link', html)          # exige ver_tec_historico
        _, com_historico = self.usuario_com("menu_hist", "ver_tec_estoque", "ver_tec_historico")
        self.assertIn(f'href="{reverse("tec_movimentacoes")}" class="sub-link', self.menu(com_historico))

    def test_sem_permissao_de_tecnologia_o_grupo_some_e_o_ti_nao_tem_o_grupo(self):
        _, frotas = self.usuario_com("menu_frota", "ver_frota")
        self.assertNotIn("sub-agro-estoque", self.menu(frotas))
        _, ti = self.usuario_com("menu_ti", "ver_tec_estoque")
        self.assertNotIn("sub-agro-estoque", ti.get("/dashboard/").content.decode())

    def test_o_item_acende_nas_telas_do_grupo(self):
        for nome, args in (("tec_visao_geral", []), ("tec_equipamentos", []), ("tec_equipamento_detalhe", [self.eq.pk]), ("tec_materiais", []), ("tec_material_detalhe", [self.cabo.pk]), ("tec_catalogo", ["modelos"]), ("tec_movimentacoes", [])):
            with self.subTest(tela=nome):
                html = self.client.get(reverse(nome, args=args)).content.decode()
                botao = re.search(r'<button[^>]*data-submenu-target="sub-agro-estoque"', html, re.S).group(0)
                self.assertIn("is-active", botao)

    def test_o_icone_do_grupo_existe_no_sprite(self):
        from services.ambiente_service import NAV_AGRO
        grupo = next(e for e in NAV_AGRO if e.get("chave") == "estoque")
        html = self.client.get(reverse("tec_visao_geral")).content.decode()
        self.assertIn(f'id="{grupo["icone"]}"', html)


class PrimeiroPassoTests(TestCase):
    """Banco virgem (só as sementes da migration): a tela orienta em vez de mostrar números zerados sem explicação."""

    def setUp(self):
        self.client = Client()
        self.client.force_login(User.objects.create_superuser("primeiro", "p@p.com", "x"))

    def test_sem_local_a_visao_geral_avisa_e_leva_ao_cadastro(self):
        html = self.client.get(reverse("tec_visao_geral")).content.decode()
        self.assertIn("Nenhum local de armazenamento cadastrado", html)
        self.assertIn(reverse("tec_catalogo", args=["locais"]), html)

    def test_sem_modelo_a_lista_de_equipamentos_manda_cadastrar_o_modelo(self):
        cat.salvar_local({"nome": "Almoxarifado", "fazenda": frota_service.fazendas()[0]}, None)
        html = self.client.get(reverse("tec_equipamentos")).content.decode()
        self.assertIn("Nenhum equipamento cadastrado", html)
        self.assertIn("Cadastrar um modelo no catálogo", html)
        self.assertIn(reverse("tec_catalogo_novo", args=["modelos"]), html)
        self.assertEqual(self.client.get(reverse("tec_materiais")).status_code, 200)
        self.assertEqual(self.client.get(reverse("tec_movimentacoes")).status_code, 200)
