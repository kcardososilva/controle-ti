"""
Frotas e Maquinários — Categorias e classes: a tela única dos cadastros de classificação (categorias, classes
operacionais e mecânicas, empresas e centros de custo). A regra de negócio está em `test_frota_classificacao`; aqui, a tela:
quem acessa, o que mostra, o que cada aba grava e o que a reclassificação faz.
"""
import re

from django.contrib.messages import get_messages
from django.urls import reverse

from ProjetoEstoque.models import (
    FrotaCentroCusto,
    FrotaClasseMecanica,
    FrotaClasseOperacional,
    FrotaEmpresa,
    FrotaEvento,
    FrotaTipo,
    FrotaVeiculo,
)
from ProjetoEstoque.tests.test_frota_equipamentos_telas import EquipamentosBase
from services import frota_service as frota


class CategoriasBase(EquipamentosBase):
    def pagina(self, aba="categorias", **extra):
        return self.client.get(reverse("frota_categorias"), {"aba": aba, **extra})

    def mensagens(self, resposta):
        return [str(m) for m in get_messages(resposta.wsgi_request)]

    def salvar(self, **dados):
        return self.client.post(reverse("frota_cadastro_salvar"), dados)


class AcessoETelaTests(CategoriasBase):
    def test_as_cinco_abas_com_as_contagens(self):
        resposta = self.pagina()
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual([(a, r) for a, r, _ in resposta.context["abas"]], [
            ("categorias", "Categorias"), ("classes-operacionais", "Classes operacionais"), ("classes-mecanicas", "Classes mecânicas"),
            ("empresas", "Empresas"), ("centros-de-custo", "Centros de custo"),
        ])
        contagens = {a: n for a, _, n in resposta.context["abas"]}
        self.assertEqual((contagens["empresas"], contagens["classes-operacionais"], contagens["centros-de-custo"]), (2, 1, 1))
        self.assertEqual(contagens["categorias"], FrotaTipo.objects.count())

    def test_so_quem_administra_a_configuracao_acessa(self):
        so_ve = self.cliente_de(self.usuario_com("so_ve", "ver_frota", "editar_frota"))
        self.assertEqual(so_ve.get(reverse("frota_categorias")).status_code, 403)
        self.assertEqual(so_ve.post(reverse("frota_cadastro_salvar"), {"chave": "empresa", "nome": "X"}).status_code, 403)
        admin = self.cliente_de(self.usuario_com("admin_frota", "ver_frota", "administrar_frota_config"))
        self.assertEqual(admin.get(reverse("frota_categorias")).status_code, 200)

    def test_o_endereco_antigo_dos_tipos_abre_a_aba_de_categorias(self):
        resposta = self.client.get(reverse("frota_config_tipos"))
        self.assertEqual((resposta.status_code, resposta.context["aba_cadastro"]), (200, "categorias"))

    def test_aba_desconhecida_e_cadastro_desconhecido_dao_404(self):
        self.assertEqual(self.pagina(aba="voo").status_code, 404)
        self.assertEqual(self.salvar(chave="voo", nome="X").status_code, 404)

    def test_salvar_nao_aceita_get(self):
        self.assertEqual(self.client.get(reverse("frota_cadastro_salvar")).status_code, 405)

    def test_cada_aba_renderiza_sem_lixo_de_template(self):
        for aba in ("categorias", "classes-operacionais", "classes-mecanicas", "empresas", "centros-de-custo"):
            with self.subTest(aba=aba):
                html = self.pagina(aba=aba).content.decode("utf-8")
                self.sem_lixo_de_template(html, aba)
                self.assertIn("Categorias e classes", html)
                self.assertIn("Nada se exclui", html)

    def test_o_menu_da_conectividade_nao_oferece_mais_os_tipos(self):
        html = self.client.get(reverse("frota_config_operadoras")).content.decode("utf-8")
        self.assertNotIn(reverse("frota_config_tipos"), html.split('class="m2m-segmentos m2m-segmentos--grande"')[1].split("</nav>")[0])


class CategoriasTests(CategoriasBase):
    def test_a_aba_de_categorias_mostra_o_uso_de_cada_uma(self):
        html = self.pagina().content.decode("utf-8")
        linha = re.search(rf'id="categoria-{self.moto.pk}".*?</form>', html, re.S).group(0)
        self.assertIn('name="espera_placa" value="1" checked', linha)
        self.assertIn('name="espera_chassi" value="1" checked', linha)
        self.assertRegex(linha, r'<span class="m2m-num">1</span> equipamento')

    def test_cria_a_categoria_com_o_que_ela_costuma_ter(self):
        r = self.client.post(reverse("frota_config_tipo_salvar"), {"nome": "Empilhadeira", "espera_chassi": "1"})
        self.assertRedirects(r, reverse("frota_config_tipos"), fetch_redirect_response=False)
        t = FrotaTipo.objects.get(nome="Empilhadeira")
        self.assertEqual((t.espera_placa, t.espera_chassi, t.exige_conectividade), (False, True, False))

    def test_editar_a_categoria_pela_linha_grava_o_que_esta_marcado_e_so_isso(self):
        self.client.post(reverse("frota_config_tipo_salvar"), {"id": self.trator.pk, "nome": "Trator", "espera_placa": "1", "ativo": "1"})
        t = FrotaTipo.objects.get(pk=self.trator.pk)
        self.assertEqual((t.espera_placa, t.espera_chassi, t.ativo), (True, False, True))

    def test_nome_repetido_volta_com_a_mensagem(self):
        r = self.client.post(reverse("frota_config_tipo_salvar"), {"nome": "trator"}, follow=True)
        self.assertIn("Já existe", " ".join(m.message for m in r.context["messages"]))


class ClassesEOutrosCadastrosTests(CategoriasBase):
    def test_cria_classe_operacional_com_categoria_e_volta_para_a_aba(self):
        r = self.salvar(chave="classe_operacional", nome="CAMINHÃO ROLL OFF", codigo_erp="13", categoria=self.caminhao.pk)
        self.assertRedirects(r, reverse("frota_categorias") + "?aba=classes-operacionais", fetch_redirect_response=False)
        c = FrotaClasseOperacional.objects.get(nome="CAMINHÃO ROLL OFF")
        self.assertEqual((c.codigo_erp, c.categoria), ("13", self.caminhao))
        self.assertIn("criada", self.mensagens(r)[0])

    def test_o_nome_original_e_a_busca_sem_acento(self):
        self.salvar(chave="classe_operacional", nome="CAMINHÃO ROLL OFF", codigo_erp="13")
        resposta = self.pagina("classes-operacionais", q="caminhao roll")
        self.assertEqual([c.nome for c in resposta.context["itens"]], ["CAMINHÃO ROLL OFF"])
        self.assertEqual([c.nome for c in self.pagina("classes-operacionais", q="13").context["itens"]], ["CAMINHÃO ROLL OFF"])
        self.assertEqual(list(self.pagina("classes-operacionais", q="nao-existe").context["itens"]), [])

    def test_aviso_de_classes_sem_categoria(self):
        self.assertNotIn("sem categoria", self.pagina("classes-operacionais").content.decode("utf-8").split("Cadastros de classificação")[-1].split("<section")[0])
        self.salvar(chave="classe_operacional", nome="PLATAFORMA")
        html = self.pagina("classes-operacionais").content.decode("utf-8")
        self.assertIn("1 classe sem categoria.", html)
        self.assertIn("Os equipamentos dessas classes ficam em “Outros”", html)

    def test_mudar_a_categoria_da_classe_nao_mexe_nos_equipamentos_a_menos_que_se_peca(self):
        classe = self.classe
        v = FrotaVeiculo.objects.get(pk=self.a.pk)
        self.assertEqual((v.tipo, v.classe_operacional), (self.trator, classe))
        self.salvar(chave="classe_operacional", id=classe.pk, nome=classe.nome, codigo_erp="34", categoria=self.caminhao.pk, ativo="1")
        self.assertEqual(FrotaVeiculo.objects.get(pk=self.a.pk).tipo, self.trator)
        r = self.salvar(chave="classe_operacional", id=classe.pk, nome=classe.nome, codigo_erp="34", categoria=self.caminhao.pk, ativo="1", reclassificar="1")
        self.assertEqual(FrotaVeiculo.objects.get(pk=self.a.pk).tipo, self.caminhao)
        self.assertIn("1 equipamento reclassificado", " ".join(self.mensagens(r)))
        self.assertTrue(FrotaEvento.objects.filter(veiculo=self.a, dados__origem="reclassificacao").exists())

    def test_a_opcao_de_reclassificar_so_aparece_quando_ha_equipamento_na_classe(self):
        html = self.pagina("classes-operacionais").content.decode("utf-8")
        self.assertIn('name="reclassificar"', html)
        self.assertIn("Aplicar aos 1 já cadastrado", html)
        self.salvar(chave="classe_operacional", nome="VAZIA")
        vazia = re.search(r'<form class="agro-cfg-linha" method="post" action="[^"]+" id="cadastro-%d">.*?</form>' % FrotaClasseOperacional.objects.get(nome="VAZIA").pk,
                          self.pagina("classes-operacionais").content.decode("utf-8"), re.S).group(0)
        self.assertNotIn("reclassificar", vazia)

    def test_empresa_com_natureza_padrao(self):
        r = self.salvar(chave="empresa", nome="FAZENDA VIZINHA", codigo_erp="3", natureza_padrao="terceiro")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(FrotaEmpresa.objects.get(nome="FAZENDA VIZINHA").natureza_padrao, "terceiro")
        self.salvar(chave="empresa", id=self.santa.pk, nome="SANTA COLOMBA", codigo_erp="1", natureza_padrao="terceiro", ativo="1")
        self.assertEqual(FrotaEmpresa.objects.get(pk=self.santa.pk).natureza_padrao, "terceiro")

    def test_classe_mecanica_e_centro_de_custo(self):
        self.salvar(chave="classe_mecanica", nome="LINHA AMARELA", codigo_erp="14")
        self.salvar(chave="centro_custo", nome="CAMINHAO - GRAOS", codigo_erp="32119")
        self.assertEqual(FrotaClasseMecanica.objects.get(codigo_erp="14").nome, "LINHA AMARELA")
        self.assertEqual(FrotaCentroCusto.objects.get(codigo_erp="32119").nome, "CAMINHAO - GRAOS")

    def test_inativar_pela_linha_e_nao_marcar_ativo_inativa(self):
        self.salvar(chave="centro_custo", id=self.centro.pk, nome=self.centro.nome, codigo_erp="32107")            # "ativo" ausente = desmarcado
        self.assertFalse(FrotaCentroCusto.objects.get(pk=self.centro.pk).ativo)
        self.assertEqual(FrotaVeiculo.objects.get(pk=self.a.pk).centro_custo, self.centro)                        # quem usa continua usando

    def test_nome_e_codigo_repetidos_voltam_com_a_mensagem_sem_gravar(self):
        r = self.salvar(chave="empresa", nome="santa colomba")
        self.assertIn("Já existe", " ".join(self.mensagens(r)))
        r = self.salvar(chave="centro_custo", nome="OUTRO CENTRO", codigo_erp="32107")
        self.assertIn("Já existe", " ".join(self.mensagens(r)))
        self.assertEqual(FrotaEmpresa.objects.filter(nome__iexact="santa colomba").count(), 1)
        self.assertFalse(FrotaCentroCusto.objects.filter(nome="OUTRO CENTRO").exists())

    def test_cada_alteracao_e_auditada(self):
        from ProjetoEstoque.models import M2MEvento

        antes = M2MEvento.objects.filter(tipo=M2MEvento.Tipo.CONFIG_FROTA).count()
        self.salvar(chave="classe_mecanica", nome="NOVA")
        self.assertEqual(M2MEvento.objects.filter(tipo=M2MEvento.Tipo.CONFIG_FROTA).count(), antes + 1)

    def test_a_tela_nao_faz_consulta_por_linha(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        for n in range(30):
            frota.criar_veiculo({"nome": f"T{n}", "tipo": self.trator.pk, "codigo_legado": f"95{n:05d}", "classe_operacional": self.classe.pk}, self.usuario)
        for n in range(20):
            FrotaClasseOperacional.objects.create(codigo_erp=f"9{n}", nome=f"CLASSE {n}", categoria=self.trator)
        self.pagina("classes-operacionais")
        with CaptureQueriesContext(connection) as com_20:
            self.pagina("classes-operacionais")
        for n in range(20, 40):
            FrotaClasseOperacional.objects.create(codigo_erp=f"9{n}", nome=f"CLASSE {n}", categoria=self.trator)
        with CaptureQueriesContext(connection) as com_40:
            self.pagina("classes-operacionais")
        self.assertEqual(len(com_20), len(com_40))
