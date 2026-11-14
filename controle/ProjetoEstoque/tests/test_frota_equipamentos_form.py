"""
Frotas e Maquinários — o formulário de cadastro e edição do equipamento (as sete seções do prompt).

O que mais importa aqui é a garantia de que EDITAR NÃO PERDE INFORMAÇÃO: um equipamento importado do ERP tem placa interna,
chassi repetido, data de 1905 e ano-modelo à frente — nada disso o formulário deixa de aceitar na edição de OUTRO campo.
"""
import re
from datetime import date

from django.urls import reverse

from ProjetoEstoque.models import FrotaCentroCusto, FrotaEvento, FrotaVeiculo, Usuario
from ProjetoEstoque.tests.test_frota_equipamentos_telas import EquipamentosBase
from services import frota_service as frota

Status = FrotaVeiculo.Status
SECOES = ["Identificação", "Classificação", "Dados técnicos", "Empresa e centro de custo", "Datas", "Localização e responsável", "Documentos e observações"]


class FormularioBase(EquipamentosBase):
    def dados(self, **extra):
        base = {
            "codigo": "", "codigo_legado": "", "nome": "TRATOR TESTE", "tipo": self.trator.pk, "classe_operacional": "", "classe_mecanica": "",
            "natureza": "proprio", "fabricante": "", "modelo": "", "ano_fabricacao": "", "ano_modelo": "", "chassi_serie": "", "empresa": "",
            "centro_custo": "", "data_compra": "", "data_inicio": "", "localidade": "", "setor": "", "area_operacao": "", "responsavel": "",
            "placa": "", "renavam": "", "observacoes": "", "exige_conectividade": "", "status": "ativo",
        }
        base.update(extra)
        return base

    def campos(self, resposta):
        return {c["nome"]: c for p in resposta.context["paineis"] for c in p["campos"]}


class SecoesETelaTests(FormularioBase):
    def test_as_sete_secoes_do_prompt_na_ordem(self):
        resposta = self.get("frota_veiculo_novo")
        self.assertEqual([p["titulo"] for p in resposta.context["paineis"]], SECOES)
        html = self.html(resposta)
        self.sem_lixo_de_template(html, "formulário")
        self.assertEqual(re.findall(r'<h3 class="m2m-painel-t" id="agro-f-\w+"><span class="agro-passo m2m-num">(\d)</span>([^<]+)</h3>', html),
                         [(str(i + 1), t) for i, t in enumerate(SECOES)])

    def test_cada_campo_do_prompt_esta_no_formulario(self):
        campos = self.campos(self.get("frota_veiculo_novo"))
        esperados = {"codigo", "codigo_legado", "nome", "tipo", "classe_operacional", "classe_mecanica", "natureza", "fabricante", "modelo",
                     "ano_fabricacao", "ano_modelo", "chassi_serie", "empresa", "centro_custo", "data_compra", "data_inicio", "localidade",
                     "setor", "area_operacao", "responsavel", "placa", "renavam", "observacoes", "exige_conectividade", "status",
                     "latitude", "longitude"}      # a coordenada cadastrada (mapa, regra 63)
        self.assertEqual(set(campos), esperados)

    def test_o_codigo_original_e_o_interno_sao_dois_campos_separados(self):
        campos = self.campos(self.get("frota_veiculo_novo"))
        self.assertEqual((campos["codigo"]["rotulo"], campos["codigo_legado"]["rotulo"]), ("Identificador interno", "Código original (ERP)"))
        self.assertFalse(campos["codigo"]["obrigatorio"])
        self.assertIn("gerar", campos["codigo"]["dica"])

    def test_na_edicao_o_codigo_interno_e_obrigatorio_e_nao_ha_situacao_inicial(self):
        campos = self.campos(self.get("frota_veiculo_editar", self.a.pk))
        self.assertTrue(campos["codigo"]["obrigatorio"])
        self.assertNotIn("status", campos)

    def test_so_o_que_o_prompt_diz_e_obrigatorio(self):
        obrigatorios = {n for n, c in self.campos(self.get("frota_veiculo_novo")).items() if c["obrigatorio"]}
        self.assertEqual(obrigatorios, {"nome", "tipo", "natureza"})                  # placa e chassi nunca

    def test_natureza_nasce_em_proprio_e_so_tem_as_duas_opcoes(self):
        c = self.campos(self.get("frota_veiculo_novo"))["natureza"]
        self.assertEqual((c["valor"], [v for v, _ in c["opcoes"]]), ("proprio", ["proprio", "terceiro"]))

    def test_os_selects_grandes_ganham_select2_e_modelo_e_setor_aceitam_digitar(self):
        html = self.html(self.get("frota_veiculo_novo"))
        for nome in ("classe_operacional", "classe_mecanica", "empresa", "centro_custo"):
            self.assertRegex(html, rf'<select name="{nome}"[^>]*data-m2m-s2="local"')
        for nome in ("modelo", "setor"):
            self.assertRegex(html, rf'<select name="{nome}"[^>]*data-agro-tags="1"')
        self.assertRegex(html, r'<select name="responsavel"[^>]*data-m2m-s2="ajax"[^>]*data-fonte="frota_opcoes"')

    def test_equipamento_novo_nao_nasce_com_o_primeiro_modelo_nem_setor_escolhido(self):
        """Um <select> sem opção vazia pré-seleciona a primeira opção no navegador."""
        html = self.html(self.get("frota_veiculo_novo"))
        for nome in ("modelo", "setor", "classe_operacional", "empresa", "centro_custo", "responsavel"):
            select = re.search(rf'<select name="{nome}".*?</select>', html, re.S).group(0)
            primeira = re.search(r"<option[^>]*>", select).group(0)
            self.assertIn('value=""', primeira, nome)
            self.assertNotIn("selected", select, nome)

    def test_as_opcoes_de_modelo_e_setor_sao_as_que_ja_existem(self):
        campos = self.campos(self.get("frota_veiculo_novo"))
        self.assertEqual({v for v, _ in campos["modelo"]["opcoes"]}, {"TRATOR NH 3880", "VW 17.210", "NXR BROS 160"})
        self.assertEqual({v for v, _ in campos["setor"]["opcoes"]}, {"GRÃOS"})

    def test_datas_sao_campos_de_data_com_o_valor_em_iso(self):
        html = self.html(self.get("frota_veiculo_editar", self.a.pk))
        self.assertRegex(html, r'<input type="date" name="data_compra" value="2018-08-15"')
        self.assertRegex(html, r'<input type="date" name="data_inicio" value=""')

    def test_a_configuracao_do_autopreenchimento_vai_por_json_script(self):
        html = self.html(self.get("frota_veiculo_novo"))
        self.assertIn('<script id="agro-form-config" type="application/json">', html)
        config = self.get("frota_veiculo_novo").context["config_do_formulario"]
        self.assertEqual(config["classe_categoria"], {str(self.classe.pk): str(self.trator.pk)})
        self.assertEqual(config["empresa_natureza"], {str(self.santa.pk): "proprio", str(self.terceiros.pk): "terceiro"})
        self.assertEqual(config["tipo_espera"][str(self.moto.pk)], {"placa": True, "chassi": True})
        self.assertEqual(config["tipo_espera"][str(self.trator.pk)], {"placa": False, "chassi": False})

    def test_a_dica_da_categoria_depende_de_quem_administra_a_configuracao(self):
        self.assertIn("Nova categoria", self.campos(self.get("frota_veiculo_novo"))["tipo"]["dica"])
        sem = self.cliente_de(self.usuario_com("so_edita", "ver_frota", "editar_frota"))
        resposta = sem.get(reverse("frota_veiculo_novo"))
        self.assertIn("Peça a quem administra a configuração", self.campos(resposta)["tipo"]["dica"])
        self.assertNotIn("Nova categoria</button>", resposta.content.decode("utf-8"))

    def test_o_responsavel_ja_escolhido_vem_no_html_para_o_select2_mostrar(self):
        pessoa = Usuario.objects.create(nome="Maria Operadora", matricula="9")
        FrotaVeiculo.objects.filter(pk=self.a.pk).update(responsavel=pessoa)
        html = self.html(self.get("frota_veiculo_editar", self.a.pk))
        self.assertRegex(html, rf'<option value="{pessoa.pk}" selected>Maria Operadora</option>')

    def test_cadastro_inativo_que_o_equipamento_ja_usa_nao_some_da_edicao(self):
        from ProjetoEstoque.models import FrotaClasseOperacional

        FrotaClasseOperacional.objects.filter(pk=self.classe.pk).update(ativo=False)
        opcoes = [v for v, _ in self.campos(self.get("frota_veiculo_editar", self.a.pk))["classe_operacional"]["opcoes"]]
        self.assertIn(self.classe.pk, opcoes)
        self.assertNotIn(self.classe.pk, [v for v, _ in self.campos(self.get("frota_veiculo_novo"))["classe_operacional"]["opcoes"]])

    def test_so_quem_edita_a_frota_abre_o_formulario(self):
        so_ve = self.cliente_de(self.usuario_com("so_ve", "ver_frota"))
        self.assertEqual(so_ve.get(reverse("frota_veiculo_novo")).status_code, 403)
        self.assertEqual(so_ve.get(reverse("frota_veiculo_editar", args=[self.a.pk])).status_code, 403)
        self.assertEqual(so_ve.post(reverse("frota_veiculo_novo"), self.dados()).status_code, 403)


class CadastroPeloFormularioTests(FormularioBase):
    def test_cadastro_minimo_gera_o_codigo_interno_e_usa_a_natureza_padrao(self):
        r = self.client.post(reverse("frota_veiculo_novo"), self.dados())
        v = FrotaVeiculo.objects.get(nome="TRATOR TESTE")
        self.assertRedirects(r, reverse("frota_veiculo_detalhe", args=[v.pk]), fetch_redirect_response=False)
        self.assertEqual((v.codigo, v.codigo_legado, v.natureza, v.status), ("MQ-00004", "", "proprio", "ativo"))      # a base já tem MQ-00001 a 3
        self.assertFalse(v.validacao_pendente)                       # cadastro à mão não é "importado a validar"

    def test_cadastro_completo_grava_todos_os_campos(self):
        outro = FrotaCentroCusto.objects.create(codigo_erp="32119", nome="CAMINHAO - GRAOS")
        pessoa = Usuario.objects.create(nome="Maria Operadora", matricula="9")
        rio = frota.fazendas()[1]
        self.client.post(reverse("frota_veiculo_novo"), self.dados(
            codigo="tr-77", codigo_legado="8110077", classe_operacional=self.classe.pk, natureza="terceiro", fabricante="John Deere",
            modelo="8400R", ano_fabricacao="2020", ano_modelo="2021", chassi_serie="1rw8400rvlb", empresa=self.terceiros.pk,
            centro_custo=outro.pk, data_compra="2021-03-10", data_inicio="2021-04-01", localidade=rio.pk, setor="CACAU",
            area_operacao="Plantio", responsavel=pessoa.pk, placa="c8110077", renavam="", observacoes="Doc. no escritório",
        ))
        v = FrotaVeiculo.objects.get(codigo="TR-77")
        self.assertEqual(
            (v.codigo_legado, v.classe_operacional, v.natureza, v.fabricante, v.modelo, v.ano_fabricacao, v.ano_modelo, v.chassi_serie),
            ("8110077", self.classe, "terceiro", "John Deere", "8400R", 2020, 2021, "1RW8400RVLB"),
        )
        self.assertEqual((v.empresa, v.centro_custo, v.data_compra, v.data_inicio, v.localidade, v.setor, v.responsavel),
                         (self.terceiros, outro, date(2021, 3, 10), date(2021, 4, 1), rio, "CACAU", pessoa))
        self.assertEqual((v.placa, v.observacoes), ("C8110077", "Doc. no escritório"))

    def test_o_erro_aparece_no_campo_certo_com_o_que_foi_digitado_preservado(self):
        r = self.client.post(reverse("frota_veiculo_novo"), self.dados(nome="OUTRO", setor="CACAU", ano_modelo="1800"))
        self.assertEqual(r.status_code, 400)
        campos = self.campos(r)
        self.assertIn("ano-modelo", campos["ano_modelo"]["erro"].lower())
        self.assertEqual((campos["nome"]["valor"], campos["setor"]["valor"], campos["ano_modelo"]["valor"]), ("OUTRO", "CACAU", "1800"))
        self.assertIn("Revise os campos destacados", r.content.decode("utf-8"))
        self.assertFalse(FrotaVeiculo.objects.filter(nome="OUTRO").exists())

    def test_a_duplicidade_do_codigo_original_aparece_no_campo_dele(self):
        r = self.client.post(reverse("frota_veiculo_novo"), self.dados(codigo_legado="8110001", nome="OUTRO"))
        self.assertEqual(r.status_code, 400)
        campos = self.campos(r)
        self.assertIn("código original", campos["codigo_legado"]["erro"])
        self.assertEqual(campos["codigo_legado"]["valor"], "8110001")

    def test_o_setor_digitado_que_ainda_nao_existe_e_gravado_e_vira_opcao(self):
        self.client.post(reverse("frota_veiculo_novo"), self.dados(setor="IRRIGAÇÃO NOVA"))
        self.assertEqual(FrotaVeiculo.objects.get(nome="TRATOR TESTE").setor, "IRRIGAÇÃO NOVA")
        self.assertIn("IRRIGAÇÃO NOVA", [v for v, _ in self.campos(self.get("frota_veiculo_novo"))["setor"]["opcoes"]])

    def test_categoria_obrigatoria(self):
        r = self.client.post(reverse("frota_veiculo_novo"), self.dados(tipo=""))
        self.assertEqual(r.status_code, 400)
        self.assertIn("categoria", self.campos(r)["tipo"]["erro"].lower())

    def test_natureza_invalida_e_recusada(self):
        r = self.client.post(reverse("frota_veiculo_novo"), self.dados(natureza="alugado"))
        self.assertEqual(r.status_code, 400)
        self.assertIn("Próprio ou Terceiro", self.campos(r)["natureza"]["erro"])

    def test_placa_so_e_exigida_no_formato_oficial_onde_a_categoria_espera(self):
        r = self.client.post(reverse("frota_veiculo_novo"), self.dados(nome="MOTO", tipo=self.moto.pk, placa="C811009"))
        self.assertEqual(r.status_code, 400)
        self.assertIn("AAA9999", self.campos(r)["placa"]["erro"])
        r = self.client.post(reverse("frota_veiculo_novo"), self.dados(nome="TRATOR COM ETIQUETA", placa="c8110035"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(FrotaVeiculo.objects.get(nome="TRATOR COM ETIQUETA").placa, "C8110035")

    def test_sem_placa_nem_chassi_salva_mesmo_numa_categoria_que_espera(self):
        r = self.client.post(reverse("frota_veiculo_novo"), self.dados(nome="MOTO SEM DOCUMENTO", tipo=self.moto.pk))
        self.assertEqual(r.status_code, 302)
        v = FrotaVeiculo.objects.get(nome="MOTO SEM DOCUMENTO")
        self.assertEqual((v.placa, v.chassi_serie), ("", ""))
        self.assertEqual(frota.indicadores_equipamentos()["sem_identificacao"], 2)         # a moto da base e esta: pendência, não bloqueio


class EdicaoSemPerderInformacaoTests(FormularioBase):
    """O equipamento como o ERP o entregou, editado pelo formulário: nada do que já estava gravado pode se perder."""

    def setUp(self):
        super().setUp()
        self.erp = frota.criar_veiculo({
            "nome": "TRATOR MF 275", "tipo": self.trator.pk, "codigo_legado": "8110009", "modelo": "TRATOR MF 275", "placa": "C8110035",
            "chassi_serie": "2160035035", "data_compra": date(1905, 7, 3), "ano_fabricacao": 2008, "ano_modelo": 2027, "empresa": self.santa.pk,
            "classe_operacional": self.classe.pk, "centro_custo": self.centro.pk, "setor": "GRÃOS", "data_inicio": date(2025, 5, 19),
        }, self.usuario, importacao=self.importacao())
        FrotaVeiculo.objects.create(codigo="MQ-9", nome="GEMEO", tipo=self.trator, chassi_serie="2160035035")     # chassi repetido, como no ERP

    def importacao(self):
        from ProjetoEstoque.models import FrotaImportacaoEquipamentos

        return FrotaImportacaoEquipamentos.objects.create(arquivo_nome="x.xlsx", sha256="0" * 64, formato="xlsx")

    def dados_atuais(self, v):
        return self.dados(
            codigo=v.codigo, codigo_legado=v.codigo_legado, nome=v.nome, tipo=v.tipo_id, classe_operacional=v.classe_operacional_id or "",
            classe_mecanica=v.classe_mecanica_id or "", natureza=v.natureza, modelo=v.modelo, ano_fabricacao=v.ano_fabricacao or "",
            ano_modelo=v.ano_modelo or "", chassi_serie=v.chassi_serie, empresa=v.empresa_id or "", centro_custo=v.centro_custo_id or "",
            data_compra=v.data_compra.isoformat() if v.data_compra else "", data_inicio=v.data_inicio.isoformat() if v.data_inicio else "",
            setor=v.setor, placa=v.placa, observacoes=v.observacoes,
        )

    def editar(self, **mudancas):
        dados = self.dados_atuais(FrotaVeiculo.objects.get(pk=self.erp.pk))
        dados.update(mudancas)
        return self.client.post(reverse("frota_veiculo_editar", args=[self.erp.pk]), dados)

    def test_o_formulario_carrega_o_que_ja_esta_gravado_sem_corrigir(self):
        campos = self.campos(self.get("frota_veiculo_editar", self.erp.pk))
        esperado = {"codigo_legado": "8110009", "placa": "C8110035", "chassi_serie": "2160035035", "data_compra": "1905-07-03",
                    "ano_modelo": 2027, "setor": "GRÃOS", "natureza": "proprio"}
        for nome, valor in esperado.items():
            self.assertEqual(campos[nome]["valor"], valor, nome)

    def test_editar_outro_campo_nao_mexe_no_que_o_erp_trouxe(self):
        r = self.editar(observacoes="Pneus novos")
        self.assertEqual(r.status_code, 302, getattr(r, "context", None) and self.campos(r)["placa"]["erro"])
        v = FrotaVeiculo.objects.get(pk=self.erp.pk)
        self.assertEqual((v.placa, v.chassi_serie, v.data_compra, v.ano_modelo, v.codigo_legado, v.observacoes),
                         ("C8110035", "2160035035", date(1905, 7, 3), 2027, "8110009", "Pneus novos"))

    def test_a_edicao_audita_so_o_que_mudou(self):
        self.editar(observacoes="Pneus novos")
        e = FrotaEvento.objects.filter(veiculo=self.erp, tipo=FrotaEvento.Tipo.EDICAO).latest("pk")
        self.assertEqual(list(e.dados["alteracoes"]), ["observacoes"])

    def test_salvar_sem_mudar_nada_nao_grava_evento(self):
        antes = FrotaEvento.objects.filter(veiculo=self.erp).count()
        self.assertEqual(self.editar().status_code, 302)
        self.assertEqual(FrotaEvento.objects.filter(veiculo=self.erp).count(), antes)

    def test_o_chassi_repetido_continua_valendo_mas_digitar_um_que_outro_equipamento_tem_nao(self):
        self.assertEqual(self.editar(nome="OUTRO NOME").status_code, 302)                      # o repetido que já tinha não bloqueia
        frota.criar_veiculo({"nome": "TERCEIRO", "tipo": self.trator.pk, "chassi_serie": "UNICO1"}, self.usuario)
        r = self.editar(chassi_serie="UNICO1")
        self.assertEqual(r.status_code, 400)                                                  # digitado, e já é de outro: recusa
        self.assertIn("Já existe um veículo com este chassi", self.campos(r)["chassi_serie"]["erro"])
        self.assertEqual(FrotaVeiculo.objects.get(pk=self.erp.pk).chassi_serie, "2160035035")  # e nada foi perdido
        self.assertEqual(self.editar(chassi_serie="CHASSINOVO1").status_code, 302)            # um chassi livre vale

    def test_trocar_a_placa_por_outra_fora_do_padrao_numa_categoria_que_espera_e_recusado(self):
        FrotaVeiculo.objects.filter(pk=self.erp.pk).update(tipo=self.moto)
        self.assertEqual(self.editar(tipo=self.moto.pk, observacoes="ok").status_code, 302)       # a que já tem, não bloqueia
        r = self.editar(tipo=self.moto.pk, placa="ZZZ")
        self.assertEqual(r.status_code, 400)

    def test_o_codigo_interno_nao_pode_ser_apagado_na_edicao(self):
        r = self.editar(codigo="")
        self.assertEqual(r.status_code, 400)
        self.assertIn("código", self.campos(r)["codigo"]["erro"].lower())

    def test_natureza_nao_enviada_nao_muda_a_que_o_equipamento_tem(self):
        FrotaVeiculo.objects.filter(pk=self.erp.pk).update(natureza="terceiro")
        dados = self.dados_atuais(FrotaVeiculo.objects.get(pk=self.erp.pk))
        dados["natureza"] = ""
        self.client.post(reverse("frota_veiculo_editar", args=[self.erp.pk]), dados | {"observacoes": "x"})
        self.assertEqual(FrotaVeiculo.objects.get(pk=self.erp.pk).natureza, "terceiro")

    def test_a_validacao_pendente_e_a_origem_da_planilha_sobrevivem_a_edicao(self):
        FrotaVeiculo.objects.filter(pk=self.erp.pk).update(validacao_pendente=True, situacao_origem="Em Uso")
        self.editar(observacoes="x")
        v = FrotaVeiculo.objects.get(pk=self.erp.pk)
        self.assertEqual((v.validacao_pendente, v.situacao_origem, v.status), (True, "Em Uso", "ativo"))

    def test_a_edicao_que_muda_o_setor_aparece_nas_movimentacoes(self):
        self.editar(setor="CACAU")
        from services import frota_historico_service as historico

        self.assertEqual([d["tipo"] for d in historico.movimentacoes_do_veiculo(FrotaVeiculo.objects.get(pk=self.erp.pk))], ["edicao"])
