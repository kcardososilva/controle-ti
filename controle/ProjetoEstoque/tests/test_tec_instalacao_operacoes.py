"""
Telas das OPERAÇÕES de instalação do Zelo Agro: instalar (do equipamento e da máquina), retirar ou
desvincular, transferir, substituir, aplicar e devolver material.

O que se confere aqui é a tela e a permissão (validada no backend, não só o botão); a regra de negócio
está em `test_tec_instalacao.py`. Cenários 3, 5, 6 e 8 da spec vistos pelo navegador.
"""

import uuid

from django.contrib.auth.models import Permission, User
from django.test import Client
from django.urls import reverse

from ProjetoEstoque.models import FrotaVeiculo, TecEquipamento, TecInstalacao, TecMovimentacao, TecSaldo, Usuario
from ProjetoEstoque.tests.test_tec_instalacao_telas import ComposicaoNasTelasBase
from services import ambiente_service
from services import tec_catalogo_service as cat
from services import tec_estoque_service as est
from services import tec_instalacao_service as ins


class OperacoesBase(ComposicaoNasTelasBase):
    def setUp(self):
        super().setUp()
        self.oficina = cat.salvar_local({"nome": "Oficina", "fazenda": self.rio}, self.admin)
        self.operadora = Usuario.objects.create(nome="Ana Operadora", status="ativo", pmb="nao")

    def post(self, url, dados=None, cliente=None, **extra):
        return (cliente or self.client).post(url, {"token": str(uuid.uuid4()), **(dados or {})}, **extra)

    def instalado(self, serie="SN-1", destino=None):
        eq = self.equipamento(serie)
        return ins.instalar(eq, "veiculo", destino or self.trator, self.admin, "inicial")

    def atual(self, eq):
        return TecEquipamento.objects.get(pk=eq.pk)


# ═════════════════════════════════════════════════════════════
# Acesso: a permissão é conferida na view
# ═════════════════════════════════════════════════════════════

class AcessoDasOperacoesTests(OperacoesBase):
    def urls(self):
        eq = self.equipamento("ACESSO")
        inst = ins.instalar(self.equipamento("ACESSO-2"), "veiculo", self.trator, self.admin, "x").instalacao
        return {
            "instalar": reverse("tec_equipamento_instalar", args=[eq.pk]),
            "instalar_na_maquina": reverse("tec_veiculo_instalar", args=[self.trator.pk]),
            "retirar": reverse("tec_instalacao_retirar", args=[inst.pk]),
            "transferir": reverse("tec_instalacao_transferir", args=[inst.pk]),
            "substituir": reverse("tec_instalacao_substituir", args=[inst.pk]),
            "aplicar": reverse("tec_material_aplicar", args=[self.cabo.pk]),
            "aplicar_novo": reverse("tec_material_aplicar_novo"),
            "devolver": reverse("tec_material_devolver", args=[self.cabo.pk]),
        }

    def test_anonimo_vai_para_o_login(self):
        for nome, url in self.urls().items():
            with self.subTest(url=nome):
                r = Client().get(url)
                self.assertEqual(r.status_code, 302)
                self.assertIn("/login", r["Location"])

    def test_sem_permissao_recebe_403_no_get_e_no_post(self):
        _, c = self.usuario_com("sem_nada")
        for nome, url in self.urls().items():
            with self.subTest(url=nome):
                self.assertEqual(c.get(url).status_code, 403)
                self.assertEqual(c.post(url, {}).status_code, 403)

    def test_so_ver_estoque_nao_opera(self):
        _, c = self.usuario_com("so_ve", "ver_tec_estoque")
        for nome, url in self.urls().items():
            with self.subTest(url=nome):
                self.assertEqual(c.get(url).status_code, 403)

    def test_quem_instala_nao_transfere_e_quem_transfere_nao_instala(self):
        urls = self.urls()
        _, instala = self.usuario_com("so_instala", "ver_tec_estoque", "instalar_tec_equipamento")
        for nome in ("instalar", "instalar_na_maquina", "retirar", "substituir", "aplicar", "aplicar_novo", "devolver"):
            with self.subTest(url=nome):
                self.assertEqual(instala.get(urls[nome]).status_code, 200)
        self.assertEqual(instala.get(urls["transferir"]).status_code, 403)
        _, transfere = self.usuario_com("so_transfere", "ver_tec_estoque", "transferir_tec_equipamento")
        self.assertEqual(transfere.get(urls["transferir"]).status_code, 200)
        for nome in ("instalar", "retirar", "substituir", "aplicar", "devolver"):
            with self.subTest(url=nome):
                self.assertEqual(transfere.get(urls[nome]).status_code, 403)

    def test_cadastrar_equipamento_nao_da_o_direito_de_instalar(self):
        _, c = self.usuario_com("so_cadastra", "ver_tec_estoque", "cadastrar_tec_equipamento")
        for nome, url in self.urls().items():
            with self.subTest(url=nome):
                self.assertEqual(c.get(url).status_code, 403)

    def test_equipamento_ou_instalacao_inexistente_e_404(self):
        self.assertEqual(self.client.get(reverse("tec_equipamento_instalar", args=[999999])).status_code, 404)
        self.assertEqual(self.client.get(reverse("tec_instalacao_retirar", args=[999999])).status_code, 404)
        self.assertEqual(self.client.get(reverse("tec_veiculo_instalar", args=[999999])).status_code, 404)

    def test_o_menu_do_agro_acende_equipamentos_nas_telas_de_operacao(self):
        for nome in ("tec_instalacao_retirar", "tec_veiculo_instalar", "tec_equipamento_instalar"):
            menu = ambiente_service.nav_de(self.admin, nome)
            grupo = next(g for g in menu if g.get("chave") == "estoque")
            item = next(i for i in grupo["itens"] if i["rotulo"] == "Equipamentos")
            with self.subTest(rota=nome):
                self.assertTrue(item["ativo"])


# ═════════════════════════════════════════════════════════════
# Instalar
# ═════════════════════════════════════════════════════════════

class InstalarNasTelasTests(OperacoesBase):
    def test_instalar_a_partir_do_equipamento(self):
        eq = self.equipamento()
        url = reverse("tec_equipamento_instalar", args=[eq.pk])
        html = self.client.get(url).content.decode()
        self.assertIn("Instalar EQ-", html)
        self.assertIn("<optgroup", html)
        self.assertIn("TR-025 · Trator 25", html)
        self.assertIn("Ana Operadora", html)
        r = self.post(url, {"destino": f"veiculo:{self.trator.pk}", "motivo": "monitor da cabine"})
        self.assertRedirects(r, reverse("tec_equipamento_detalhe", args=[eq.pk]), fetch_redirect_response=False)
        self.assertEqual(self.atual(eq).situacao, "instalado")
        self.assertEqual(TecInstalacao.objects.get(equipamento=eq).veiculo_id, self.trator.pk)
        mensagens = [str(m) for m in r.wsgi_request._messages]
        self.assertTrue(any("instalado em TR-025 · Trator 25" in m for m in mensagens), mensagens)

    def test_instalar_em_operador(self):
        eq = self.equipamento()
        r = self.post(reverse("tec_equipamento_instalar", args=[eq.pk]), {"destino": f"operador:{self.operadora.pk}", "motivo": "entregue"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(TecInstalacao.objects.get(equipamento=eq).operador_id, self.operadora.pk)

    def test_erro_por_campo_preserva_o_que_foi_digitado_e_nao_grava(self):
        eq = self.equipamento()
        url = reverse("tec_equipamento_instalar", args=[eq.pk])
        r = self.post(url, {"destino": f"veiculo:{self.trator.pk}", "motivo": "", "observacoes": "lado direito"})
        self.assertEqual(r.status_code, 400)
        html = r.content.decode()
        self.assertIn("lado direito", html)
        self.assertIn("Informe o motivo", html)
        r = self.post(url, {"destino": "", "motivo": "x"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Escolha para onde vai", r.content.decode())
        r = self.post(url, {"destino": "torre:1", "motivo": "x"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.atual(eq).situacao, "disponivel")
        self.assertFalse(TecInstalacao.objects.exists())

    def test_duplo_envio_com_o_mesmo_token_nao_instala_duas_vezes(self):
        eq = self.equipamento()
        url = reverse("tec_equipamento_instalar", args=[eq.pk])
        dados = {"token": str(uuid.uuid4()), "destino": f"veiculo:{self.trator.pk}", "motivo": "x"}
        self.client.post(url, dados)
        r = self.client.post(url, dados, follow=True)
        self.assertEqual(TecInstalacao.objects.filter(equipamento=eq).count(), 1)
        self.assertEqual(TecMovimentacao.objects.filter(tipo="instalacao").count(), 1)
        self.assertIn("já tinha sido registrada", r.content.decode())

    def test_equipamento_que_nao_esta_disponivel_nem_abre_o_formulario(self):
        eq = self.equipamento()
        ins.instalar(eq, "veiculo", self.trator, self.admin, "x")
        r = self.client.get(reverse("tec_equipamento_instalar", args=[eq.pk]), follow=True)
        self.assertRedirects(r, reverse("tec_equipamento_detalhe", args=[eq.pk]))
        self.assertIn("só equipamento disponível se instala", r.content.decode())

    def test_duas_telas_abertas_a_segunda_recebe_a_situacao_atual(self):
        eq = self.equipamento()
        url = reverse("tec_equipamento_instalar", args=[eq.pk])
        self.client.get(url)                                                       # as duas pessoas abriram
        self.post(url, {"destino": f"veiculo:{self.trator.pk}", "motivo": "primeira"})
        r = self.post(url, {"destino": f"veiculo:{self.outro.pk}", "motivo": "segunda"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("já está instalado", r.content.decode())
        self.assertEqual(TecInstalacao.objects.get(equipamento=eq).veiculo_id, self.trator.pk)

    def test_maquina_desativada_nao_aparece_nem_recebe(self):
        eq = self.equipamento()
        FrotaVeiculo.objects.filter(pk=self.outro.pk).update(status="desativado")
        url = reverse("tec_equipamento_instalar", args=[eq.pk])
        self.assertNotIn("TR-026", self.client.get(url).content.decode())
        r = self.post(url, {"destino": f"veiculo:{self.outro.pk}", "motivo": "x"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("desativada", r.content.decode())

    def test_instalar_a_partir_da_maquina_volta_para_a_composicao(self):
        eq = self.equipamento("NA-MAQUINA")
        url = reverse("tec_veiculo_instalar", args=[self.trator.pk])
        html = self.client.get(url).content.decode()
        self.assertIn("Instalar em TR-025", html)
        self.assertIn("S/N NA-MAQUINA", html)
        voltar = f"{reverse('frota_veiculo_detalhe', args=[self.trator.pk])}#m2m-v-d-comp"
        r = self.post(url, {"equipamento": eq.pk, "motivo": "receptor", "voltar": voltar})
        self.assertRedirects(r, voltar, fetch_redirect_response=False)
        self.assertEqual(TecInstalacao.objects.get(equipamento=eq).veiculo_id, self.trator.pk)

    def test_voltar_externo_e_ignorado(self):
        eq = self.equipamento()
        url = reverse("tec_veiculo_instalar", args=[self.trator.pk])
        r = self.post(url, {"equipamento": eq.pk, "motivo": "x", "voltar": "//evil.example/roubo"})
        self.assertEqual(r["Location"], f"{reverse('frota_veiculo_detalhe', args=[self.trator.pk])}#m2m-v-d-comp")
        outro = self.equipamento("OUTRO")
        r = self.post(url, {"equipamento": outro.pk, "motivo": "x", "voltar": "https://evil.example/"})
        self.assertTrue(r["Location"].startswith("/agro/frotas/"))

    def test_sem_equipamento_disponivel_a_tela_diz_o_que_fazer(self):
        html = self.client.get(reverse("tec_veiculo_instalar", args=[self.trator.pk])).content.decode()
        self.assertIn("Não há equipamento disponível", html)

    def test_equipamento_inexistente_ou_nao_escolhido(self):
        url = reverse("tec_veiculo_instalar", args=[self.trator.pk])
        r = self.post(url, {"equipamento": "", "motivo": "x"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Escolha o equipamento", r.content.decode())
        r = self.post(url, {"equipamento": "999999", "motivo": "x"})
        self.assertEqual(r.status_code, 400)

    def test_nomes_com_html_sao_escapados_no_seletor(self):
        modelo = cat.salvar_modelo({"fabricante": self.deere, "categoria": self.cat_monitor, "nome": "<script>x</script>", "controle": "individual"}, self.admin)
        self.equipamento("<b>1</b>", modelo)
        html = self.client.get(reverse("tec_veiculo_instalar", args=[self.trator.pk])).content.decode()
        self.assertNotIn("<script>x</script>", html)
        self.assertNotIn("<b>1</b>", html)


# ═════════════════════════════════════════════════════════════
# Retirar, desvincular, transferir, substituir
# ═════════════════════════════════════════════════════════════

class RetirarTransferirSubstituirNasTelasTests(OperacoesBase):
    def test_retirar_devolve_ao_estoque_no_local(self):
        inst = self.instalado().instalacao
        url = reverse("tec_instalacao_retirar", args=[inst.pk])
        html = self.client.get(url).content.decode()
        self.assertIn("Retirar EQ-", html)
        self.assertIn("Oficina", html)
        r = self.post(url, {"local": self.oficina.pk, "motivo": "tela rachada"})
        self.assertEqual(r.status_code, 302)
        eq = self.atual(inst.equipamento)
        self.assertEqual((eq.situacao, eq.local_id), ("disponivel", self.oficina.pk))
        inst.refresh_from_db()
        self.assertEqual(inst.motivo_fim, "tela rachada")

    def test_retirar_sem_local_ou_sem_motivo_volta_com_erro(self):
        inst = self.instalado().instalacao
        url = reverse("tec_instalacao_retirar", args=[inst.pk])
        for dados, esperado in (({"local": "", "motivo": "x"}, "Escolha o local"), ({"local": self.oficina.pk, "motivo": ""}, "Informe o motivo")):
            with self.subTest(dados=dados):
                r = self.post(url, dados)
                self.assertEqual(r.status_code, 400)
                self.assertIn(esperado, r.content.decode())
        self.assertEqual(self.atual(inst.equipamento).situacao, "instalado")

    def test_o_operador_e_desvinculado_nao_retirado(self):
        eq = self.equipamento()
        inst = ins.instalar(eq, "operador", self.operadora, self.admin, "entregue").instalacao
        html = self.client.get(reverse("tec_instalacao_retirar", args=[inst.pk])).content.decode()
        self.assertIn("Desvincular EQ-", html)
        r = self.post(reverse("tec_instalacao_retirar", args=[inst.pk]), {"local": self.deposito.pk, "motivo": "devolvido"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(TecMovimentacao.objects.filter(equipamento=eq).order_by("pk").last().tipo, "desvinculacao")

    def test_instalacao_ja_encerrada_nao_abre_e_o_post_velho_e_recusado_com_quem_e_quando(self):
        inst = self.instalado().instalacao
        url = reverse("tec_instalacao_retirar", args=[inst.pk])
        self.client.get(url)
        self.post(url, {"local": self.deposito.pk, "motivo": "primeira"})
        r = self.client.get(url, follow=True)
        self.assertIn("já foi retirado por", r.content.decode())
        r = self.post(url, {"local": self.deposito.pk, "motivo": "segunda"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("já foi retirado por", r.content.decode())
        self.assertEqual(TecMovimentacao.objects.filter(tipo="retirada").count(), 1)

    def test_transferir_nao_oferece_o_destino_atual_e_preserva_o_historico(self):
        inst = self.instalado().instalacao
        url = reverse("tec_instalacao_transferir", args=[inst.pk])
        html = self.client.get(url).content.decode()
        self.assertNotIn(f'value="veiculo:{self.trator.pk}"', html)
        self.assertIn(f'value="veiculo:{self.outro.pk}"', html)
        r = self.post(url, {"destino": f"veiculo:{self.outro.pk}", "motivo": "máquina parada"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(TecInstalacao.objects.filter(equipamento=inst.equipamento).count(), 2)
        self.assertEqual(TecInstalacao.objects.get(equipamento=inst.equipamento, fim_em__isnull=True).veiculo_id, self.outro.pk)

    def test_transferir_para_o_mesmo_destino_por_post_direto_e_recusado(self):
        inst = self.instalado().instalacao
        r = self.post(reverse("tec_instalacao_transferir", args=[inst.pk]), {"destino": f"veiculo:{self.trator.pk}", "motivo": "x"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(TecInstalacao.objects.count(), 1)

    def test_ht_de_um_operador_para_outro_cenario_5(self):
        ht = self.equipamento("HT-1")
        primeira = ins.instalar(ht, "operador", self.operadora, self.admin, "safra").instalacao
        outra = Usuario.objects.create(nome="Bruno Operador", status="ativo", pmb="nao")
        r = self.post(reverse("tec_instalacao_transferir", args=[primeira.pk]), {"destino": f"operador:{outra.pk}", "motivo": "troca de turno"})
        self.assertEqual(r.status_code, 302)
        html = self.client.get(reverse("tec_equipamento_detalhe", args=[ht.pk])).content.decode()
        self.assertIn("Ana Operadora", html)
        self.assertIn("Bruno Operador", html)
        self.assertIn("troca de turno", html)

    def test_substituir_cenario_6(self):
        inst = self.instalado("VELHO").instalacao
        novo = self.equipamento("NOVO")
        url = reverse("tec_instalacao_substituir", args=[inst.pk])
        html = self.client.get(url).content.decode()
        self.assertIn("S/N NOVO", html)
        self.assertNotIn("S/N VELHO", html)                                          # o que está instalado não é opção
        r = self.post(url, {"equipamento_novo": novo.pk, "destino_do_antigo": "disponivel", "local": self.oficina.pk, "motivo": "tela rachada"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.atual(novo).situacao, "instalado")
        self.assertEqual((self.atual(inst.equipamento).situacao, self.atual(inst.equipamento).local_id), ("disponivel", self.oficina.pk))
        self.assertEqual(TecMovimentacao.objects.filter(tipo="substituicao").count(), 2)

    def test_substituir_mandando_o_antigo_para_a_manutencao_nao_pede_local(self):
        inst = self.instalado("VELHO").instalacao
        novo = self.equipamento("NOVO")
        r = self.post(reverse("tec_instalacao_substituir", args=[inst.pk]), {"equipamento_novo": novo.pk, "destino_do_antigo": "manutencao", "motivo": "assistência"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.atual(inst.equipamento).situacao, "em_manutencao")

    def test_substituir_sem_local_quando_o_antigo_volta_ao_estoque_e_recusado(self):
        inst = self.instalado("VELHO").instalacao
        novo = self.equipamento("NOVO")
        r = self.post(reverse("tec_instalacao_substituir", args=[inst.pk]), {"equipamento_novo": novo.pk, "destino_do_antigo": "disponivel", "local": "", "motivo": "x"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Escolha o local", r.content.decode())
        self.assertEqual(self.atual(novo).situacao, "disponivel")

    def test_substituir_sem_equipamento_disponivel_avisa(self):
        inst = self.instalado("SOZINHO").instalacao
        html = self.client.get(reverse("tec_instalacao_substituir", args=[inst.pk])).content.decode()
        self.assertIn("Não há outro equipamento disponível", html)


# ═════════════════════════════════════════════════════════════
# Material por quantidade
# ═════════════════════════════════════════════════════════════

class MaterialNasTelasTests(OperacoesBase):
    def setUp(self):
        super().setUp()
        est.registrar_entrada_material(self.cabo, self.deposito, 100, self.admin)

    def saldo(self, local=None):
        return TecSaldo.objects.get(modelo=self.cabo, local=local or self.deposito).quantidade

    def test_aplicar_a_partir_do_material(self):
        url = reverse("tec_material_aplicar", args=[self.cabo.pk])
        html = self.client.get(url).content.decode()
        self.assertIn("Aplicar material", html)
        self.assertIn("Almoxarifado", html)
        r = self.post(url, {"local": self.deposito.pk, "quantidade": "30", "destino": f"veiculo:{self.trator.pk}", "motivo": "chicote"})
        self.assertRedirects(r, reverse("tec_material_detalhe", args=[self.cabo.pk]), fetch_redirect_response=False)
        self.assertEqual(self.saldo(), 70)
        self.assertEqual(ins.composicao_do_veiculo(self.trator)["materiais"][0]["quantidade"], 30)

    def test_aplicar_a_partir_da_maquina_escolhendo_o_material(self):
        url = reverse("tec_material_aplicar_novo")
        html = self.client.get(url, {"destino": f"veiculo:{self.trator.pk}"}).content.decode()
        self.assertIn("Cabo UTP", html)
        self.assertIn(f'value="veiculo:{self.trator.pk}" selected', html)           # o destino já vem escolhido
        voltar = f"{reverse('frota_veiculo_detalhe', args=[self.trator.pk])}#m2m-v-d-comp"
        r = self.post(url, {"modelo": self.cabo.pk, "local": self.deposito.pk, "quantidade": "5", "destino": f"veiculo:{self.trator.pk}", "motivo": "x", "voltar": voltar})
        self.assertRedirects(r, voltar, fetch_redirect_response=False)
        self.assertEqual(self.saldo(), 95)

    def test_aplicar_mais_do_que_ha_volta_com_erro_no_campo(self):
        url = reverse("tec_material_aplicar", args=[self.cabo.pk])
        r = self.post(url, {"local": self.deposito.pk, "quantidade": "101", "destino": f"veiculo:{self.trator.pk}", "motivo": "x"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Saldo insuficiente", r.content.decode())
        self.assertEqual(self.saldo(), 100)

    def test_duplo_envio_nao_debita_duas_vezes(self):
        url = reverse("tec_material_aplicar", args=[self.cabo.pk])
        dados = {"token": str(uuid.uuid4()), "local": self.deposito.pk, "quantidade": "10", "destino": f"veiculo:{self.trator.pk}", "motivo": "x"}
        self.client.post(url, dados)
        self.client.post(url, dados)
        self.assertEqual(self.saldo(), 90)

    def test_devolver_so_oferece_onde_esta_aplicado(self):
        ins.aplicar_material(self.cabo, self.deposito, 30, "veiculo", self.trator, self.admin, "x")
        url = reverse("tec_material_devolver", args=[self.cabo.pk])
        html = self.client.get(url).content.decode()
        self.assertIn(f'value="veiculo:{self.trator.pk}"', html)
        self.assertNotIn(f'value="veiculo:{self.outro.pk}"', html)
        self.assertIn("30 m", html)
        r = self.post(url, {"destino": f"veiculo:{self.trator.pk}", "quantidade": "10", "local": self.oficina.pk, "motivo": "sobrou"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.saldo(self.oficina), 10)
        self.assertEqual(ins.composicao_do_veiculo(self.trator)["materiais"][0]["quantidade"], 20)

    def test_devolver_mais_do_que_foi_aplicado_e_recusado(self):
        ins.aplicar_material(self.cabo, self.deposito, 30, "veiculo", self.trator, self.admin, "x")
        r = self.post(reverse("tec_material_devolver", args=[self.cabo.pk]), {"destino": f"veiculo:{self.trator.pk}", "quantidade": "31", "local": self.oficina.pk, "motivo": "x"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Só há 30", r.content.decode())
        self.assertEqual(self.saldo(), 70)

    def test_devolver_sem_nada_aplicado_avisa(self):
        html = self.client.get(reverse("tec_material_devolver", args=[self.cabo.pk])).content.decode()
        self.assertIn("não está aplicado em nenhuma máquina", html)

    def test_o_detalhe_do_material_mostra_onde_esta_aplicado(self):
        ins.aplicar_material(self.cabo, self.deposito, 30, "veiculo", self.trator, self.admin, "x")
        html = self.client.get(reverse("tec_material_detalhe", args=[self.cabo.pk])).content.decode()
        self.assertIn("Aplicado em", html)
        self.assertIn("TR-025 · Trator 25", html)
        self.assertIn(f"{reverse('tec_material_devolver', args=[self.cabo.pk])}?destino=veiculo:{self.trator.pk}", html)
        self.assertIn("Aplicar em máquina", html)

    def test_material_individual_nao_tem_tela_de_aplicar(self):
        self.assertEqual(self.client.get(reverse("tec_material_aplicar", args=[self.g5.pk])).status_code, 404)


# ═════════════════════════════════════════════════════════════
# Botões: só aparecem para quem pode (e a rota recusa de qualquer jeito)
# ═════════════════════════════════════════════════════════════

class BotoesDasOperacoesTests(OperacoesBase):
    def test_equipamento_disponivel_mostra_instalar_so_para_quem_pode(self):
        eq = self.equipamento()
        url = reverse("tec_equipamento_detalhe", args=[eq.pk])
        instalar = reverse("tec_equipamento_instalar", args=[eq.pk])
        self.assertIn(instalar, self.client.get(url).content.decode())
        _, so_ve = self.usuario_com("so_ve_b", "ver_tec_estoque")
        self.assertNotIn(instalar, so_ve.get(url).content.decode())

    def test_equipamento_instalado_mostra_transferir_substituir_e_retirar(self):
        inst = self.instalado().instalacao
        url = reverse("tec_equipamento_detalhe", args=[inst.equipamento_id])
        html = self.client.get(url).content.decode()
        for nome in ("tec_instalacao_transferir", "tec_instalacao_substituir", "tec_instalacao_retirar"):
            self.assertIn(reverse(nome, args=[inst.pk]), html)
        self.assertNotIn(reverse("tec_equipamento_instalar", args=[inst.equipamento_id]), html)
        _, so_instala = self.usuario_com("so_instala_b", "ver_tec_estoque", "instalar_tec_equipamento")
        html = so_instala.get(url).content.decode()
        self.assertIn(reverse("tec_instalacao_retirar", args=[inst.pk]), html)
        self.assertNotIn(reverse("tec_instalacao_transferir", args=[inst.pk]), html)

    def test_composicao_da_maquina_traz_os_botoes_por_permissao(self):
        inst = self.instalado().instalacao
        url = reverse("frota_veiculo_detalhe", args=[self.trator.pk])
        html = self.client.get(url).content.decode()
        painel = html[html.index('id="m2m-v-d-comp"'):html.index('id="m2m-v-d-chips"')]
        self.assertIn(reverse("tec_veiculo_instalar", args=[self.trator.pk]), painel)
        self.assertIn(reverse("tec_instalacao_retirar", args=[inst.pk]), painel)
        self.assertIn(reverse("tec_instalacao_substituir", args=[inst.pk]), painel)
        self.assertIn(reverse("tec_instalacao_transferir", args=[inst.pk]), painel)
        self.assertIn("%23m2m-v-d-comp", painel)                                        # volta para o painel
        _, leitor = self.usuario_com("leitor_comp", "ver_frota", "ver_tec_estoque")
        painel = leitor.get(url).content.decode()
        painel = painel[painel.index('id="m2m-v-d-comp"'):painel.index('id="m2m-v-d-chips"')]
        for rota in ("tec_veiculo_instalar",):
            self.assertNotIn(reverse(rota, args=[self.trator.pk]), painel)
        self.assertNotIn(reverse("tec_instalacao_retirar", args=[inst.pk]), painel)
        self.assertNotIn("Ações", painel)

    def test_maquina_desativada_nao_oferece_instalar(self):
        FrotaVeiculo.objects.filter(pk=self.outro.pk).update(status="desativado")
        html = self.client.get(reverse("frota_veiculo_detalhe", args=[self.outro.pk])).content.decode()
        self.assertNotIn(reverse("tec_veiculo_instalar", args=[self.outro.pk]), html)

    def test_material_instalado_na_maquina_oferece_devolver(self):
        est.registrar_entrada_material(self.cabo, self.deposito, 20, self.admin)
        ins.aplicar_material(self.cabo, self.deposito, 5, "veiculo", self.trator, self.admin, "x")
        html = self.client.get(reverse("frota_veiculo_detalhe", args=[self.trator.pk])).content.decode()
        self.assertIn(f"{reverse('tec_material_devolver', args=[self.cabo.pk])}?destino=veiculo:{self.trator.pk}", html)
