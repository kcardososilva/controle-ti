"""
Telas da instalação: a Composição tecnológica no detalhe da máquina (equipamentos, chips e material
instalados agora, e os que já passaram por ela) e as instalações no detalhe do equipamento.

Cenário 3 da spec (instalar monitor e receptor, vincular um chip e ver tudo na composição) visto pelo
navegador, mais a permissão de cada bloco.
"""

from django.contrib.auth.models import Permission, User
from django.test import Client, TestCase
from django.urls import reverse

from ProjetoEstoque.models import FrotaTipo, FrotaVeiculo, TecCategoria, TecFabricante, Usuario
from ProjetoEstoque.tests.test_m2m import iccid_valido
from services import frota_service
from services import frota_vinculo_service as vinculos
from services import m2m_monitoramento_service as m2m
from services import tec_catalogo_service as cat
from services import tec_estoque_service as est
from services import tec_instalacao_service as ins


class ComposicaoNasTelasBase(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("comp_admin", "a@a.com", "x")
        self.karitel, self.rio = frota_service.fazendas()
        self.deposito = cat.salvar_local({"nome": "Almoxarifado", "fazenda": self.karitel}, self.admin)
        self.deere = TecFabricante.objects.get(nome="John Deere")
        self.motorola = TecFabricante.objects.get(nome="Motorola")
        self.cat_monitor = TecCategoria.objects.get(nome="Monitor / display")
        self.cat_radio = TecCategoria.objects.get(nome="Rádio portátil (HT)")
        self.g5 = cat.salvar_modelo(
            {"fabricante": self.deere, "categoria": self.cat_monitor, "nome": "G5", "controle": "individual"}, self.admin,
        )
        self.cabo = cat.salvar_modelo(
            {"fabricante": self.motorola, "categoria": TecCategoria.objects.get(nome="Cabo"), "nome": "Cabo UTP",
             "controle": "quantidade", "unidade": cat.listar_unidades().get(sigla="m")}, self.admin,
        )
        tipo = FrotaTipo.objects.get(nome="Trator")
        self.trator = frota_service.criar_veiculo({"codigo": "TR-025", "nome": "Trator 25", "tipo": tipo.pk}, self.admin)
        self.outro = frota_service.criar_veiculo({"codigo": "TR-026", "nome": "Trator 26", "tipo": tipo.pk}, self.admin)
        self.client = Client()
        self.client.force_login(self.admin)

    def equipamento(self, serie="SN-1", modelo=None):
        return est.criar_equipamento({"modelo": modelo or self.g5, "numero_serie": serie, "local": self.deposito}, self.admin).equipamento

    def usuario_com(self, login, *codenames):
        user = User.objects.create_user(login, f"{login}@t.com", "x")
        user.user_permissions.add(*Permission.objects.filter(codename__in=codenames))
        c = Client()
        c.force_login(user)
        return user, c

    def detalhe(self, cliente=None, veiculo=None):
        return (cliente or self.client).get(reverse("frota_veiculo_detalhe", args=[(veiculo or self.trator).pk]))

    def painel(self, html):
        return html[html.index('id="m2m-v-d-comp"'):html.index('id="m2m-v-d-chips"')]


class ComposicaoDoVeiculoTests(ComposicaoNasTelasBase):
    def test_sem_nada_instalado_mostra_o_estado_vazio_com_caminho_para_o_estoque(self):
        r = self.detalhe()
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn("Composição tecnológica", html)
        self.assertIn("Nenhum equipamento instalado", html)
        self.assertIn(reverse("tec_equipamentos"), html)

    def test_cenario_3_monitor_receptor_chip_e_material_aparecem_na_composicao(self):
        monitor = self.equipamento("MON-1")
        starfire = cat.salvar_modelo({"fabricante": self.deere, "categoria": self.cat_radio, "nome": "StarFire", "controle": "individual"}, self.admin)
        receptor = self.equipamento("REC-1", starfire)
        ins.instalar(monitor, "veiculo", self.trator, self.admin, "monitor da cabine")
        ins.instalar(receptor, "veiculo", self.trator, self.admin, "receptor no teto")
        est.registrar_entrada_material(self.cabo, self.deposito, 40, self.admin)
        ins.aplicar_material(self.cabo, self.deposito, 12, "veiculo", self.trator, self.admin, "chicote")
        chip = m2m.criar_chip({"nome": "Chip do trator", "iccid": iccid_valido(7)}, self.admin)
        vinculos.vincular(chip, self.trator, self.admin, "conectividade")

        painel = self.painel(self.detalhe().content.decode())
        for esperado in ("G5", "StarFire", "John Deere", "Monitor / display", "EQ-", "S/N MON-1", "S/N REC-1",
                         "Conectividade", "Chip M2M", "Chip do trator", "Cabo UTP", "12 m aplicado"):
            with self.subTest(esperado=esperado):
                self.assertIn(esperado, painel)
        self.assertIn('<span class="m2m-painel-t-n">4</span>', painel)                 # 2 equipamentos + 1 chip + 1 material
        self.assertIn(reverse("tec_equipamento_detalhe", args=[monitor.pk]), painel)

    def test_a_contagem_do_painel_soma_equipamentos_e_chips(self):
        ins.instalar(self.equipamento("A"), "veiculo", self.trator, self.admin, "x")
        chip = m2m.criar_chip({"nome": "Chip A", "iccid": iccid_valido(8)}, self.admin)
        vinculos.vincular(chip, self.trator, self.admin, "x")
        self.assertIn('Composição tecnológica<span class="m2m-painel-t-n">2</span>', self.detalhe().content.decode())

    def test_o_que_esta_em_outra_maquina_nao_aparece_aqui(self):
        ins.instalar(self.equipamento("OUTRA"), "veiculo", self.outro, self.admin, "x")
        self.assertNotIn("S/N OUTRA", self.detalhe().content.decode())
        self.assertIn("S/N OUTRA", self.detalhe(veiculo=self.outro).content.decode())

    def test_o_que_foi_retirado_sai_da_composicao_e_vai_para_ja_passaram(self):
        inst = ins.instalar(self.equipamento("VELHO"), "veiculo", self.trator, self.admin, "x").instalacao
        ins.retirar(inst.pk, self.deposito, self.admin, "tela rachada")
        painel = self.painel(self.detalhe().content.decode())
        self.assertIn("Já passaram por esta máquina", painel)
        self.assertIn("tela rachada", painel)
        self.assertIn("Nenhum equipamento instalado", painel)                           # nada instalado agora
        self.assertIn('<details class="m2m-passado">', painel)

    def test_o_resumo_conta_os_equipamentos_instalados(self):
        ins.instalar(self.equipamento("A"), "veiculo", self.trator, self.admin, "x")
        ins.instalar(self.equipamento("B"), "veiculo", self.trator, self.admin, "x")
        self.assertIn('<dt>Equipamentos instalados</dt><dd class="m2m-num">2</dd>', self.detalhe().content.decode())

    def test_sem_a_permissao_do_estoque_tecnologico_a_composicao_some(self):
        ins.instalar(self.equipamento("A"), "veiculo", self.trator, self.admin, "x")
        _, c = self.usuario_com("so_frota", "ver_frota")
        r = self.detalhe(c)
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertNotIn("Composição tecnológica", html)
        self.assertNotIn("S/N A", html)
        self.assertNotIn("Equipamentos instalados", html)

    def test_sem_ver_chips_a_composicao_mostra_so_a_contagem_do_chip(self):
        chip = m2m.criar_chip({"nome": "Chip secreto", "iccid": iccid_valido(9)}, self.admin)
        vinculos.vincular(chip, self.trator, self.admin, "x")
        _, c = self.usuario_com("sem_chips", "ver_frota", "ver_tec_estoque")
        painel = self.painel(self.detalhe(c).content.decode())
        self.assertIn("1 chip vinculado", painel)
        self.assertIn("pedem a permissão de ver chips", painel)
        self.assertNotIn("Chip secreto", painel)
        self.assertNotIn(reverse("m2m_chip_detalhe", args=[chip.pk]), painel)

    def test_com_ver_chips_a_linha_do_chip_leva_ao_chip(self):
        chip = m2m.criar_chip({"nome": "Chip visivel", "iccid": iccid_valido(10)}, self.admin)
        vinculos.vincular(chip, self.trator, self.admin, "x")
        _, c = self.usuario_com("com_chips", "ver_frota", "ver_tec_estoque", "ver_chips")
        painel = self.painel(self.detalhe(c).content.decode())
        self.assertIn("Chip visivel", painel)
        self.assertIn(reverse("m2m_chip_detalhe", args=[chip.pk]), painel)

    def test_nomes_com_html_sao_escapados(self):
        modelo = cat.salvar_modelo({"fabricante": self.deere, "categoria": self.cat_monitor, "nome": "<b>X</b>", "controle": "individual"}, self.admin)
        ins.instalar(self.equipamento("<i>1</i>", modelo), "veiculo", self.trator, self.admin, "x")
        html = self.detalhe().content.decode()
        self.assertNotIn("<b>X</b>", html)
        self.assertIn("&lt;b&gt;X&lt;/b&gt;", html)

    def test_o_numero_de_consultas_nao_cresce_com_a_quantidade_de_equipamentos(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        def consultas():
            with CaptureQueriesContext(connection) as capturadas:
                self.detalhe()
            return len(capturadas)

        ins.instalar(self.equipamento("A"), "veiculo", self.trator, self.admin, "x")
        self.detalhe()                                                                  # aquece a sessão e o que o middleware guarda
        com_um = consultas()
        for n in range(5):
            ins.instalar(self.equipamento(f"B{n}"), "veiculo", self.trator, self.admin, "x")
        self.assertEqual(consultas(), com_um)


class InstalacoesNoDetalheDoEquipamentoTests(ComposicaoNasTelasBase):
    def detalhe_do(self, eq, cliente=None):
        return (cliente or self.client).get(reverse("tec_equipamento_detalhe", args=[eq.pk]))

    def test_nunca_instalado(self):
        html = self.detalhe_do(self.equipamento()).content.decode()
        self.assertIn("Nunca foi instalado", html)
        self.assertIn("<dt>Responsável</dt>", html)

    def test_instalado_mostra_onde_esta_o_periodo_em_curso_e_o_responsavel(self):
        eq = self.equipamento()
        resp = Usuario.objects.create(nome="Carlos Responsável", status="ativo", pmb="nao")
        FrotaVeiculo.objects.filter(pk=self.trator.pk).update(responsavel=resp)
        ins.instalar(eq, "veiculo", self.trator, self.admin, "x")
        html = self.detalhe_do(eq).content.decode()
        self.assertIn("TR-025 · Trator 25", html)
        self.assertIn(reverse("frota_veiculo_detalhe", args=[self.trator.pk]), html)
        self.assertIn("em curso", html)
        self.assertIn("Carlos Responsável", html)

    def test_historico_dos_periodos_depois_de_uma_transferencia(self):
        eq = self.equipamento()
        primeira = ins.instalar(eq, "veiculo", self.trator, self.admin, "x").instalacao
        ins.transferir(primeira.pk, "veiculo", self.outro, self.admin, "máquina parada")
        html = self.detalhe_do(eq).content.decode()
        self.assertIn("TR-025 · Trator 25", html)
        self.assertIn("TR-026 · Trator 26", html)
        self.assertIn("máquina parada", html)
        self.assertIn('Instalações<span class="m2m-painel-t-n">2</span>', html)

    def test_o_link_da_maquina_so_aparece_para_quem_ve_frota(self):
        eq = self.equipamento()
        ins.instalar(eq, "veiculo", self.trator, self.admin, "x")
        _, c = self.usuario_com("so_estoque", "ver_tec_estoque")
        html = self.detalhe_do(eq, c).content.decode()
        self.assertIn("TR-025 · Trator 25", html)
        self.assertNotIn(reverse("frota_veiculo_detalhe", args=[self.trator.pk]), html)

    def test_operador_aparece_como_responsavel_e_sem_link(self):
        modelo = cat.salvar_modelo({"fabricante": self.motorola, "categoria": self.cat_radio, "nome": "DEP 450", "controle": "individual"}, self.admin)
        ht = self.equipamento("HT-1", modelo)
        operadora = Usuario.objects.create(nome="Ana Operadora", status="ativo", pmb="nao")
        ins.instalar(ht, "operador", operadora, self.admin, "entregue")
        html = self.detalhe_do(ht).content.decode()
        self.assertIn("<dt>Responsável</dt><dd>Ana Operadora</dd>", html)

    def test_a_lista_mostra_onde_o_instalado_esta(self):
        ins.instalar(self.equipamento(), "veiculo", self.trator, self.admin, "x")
        html = self.client.get(reverse("tec_equipamentos")).content.decode()
        self.assertIn("TR-025 · Trator 25", html)
        self.assertIn("máquina", html)
