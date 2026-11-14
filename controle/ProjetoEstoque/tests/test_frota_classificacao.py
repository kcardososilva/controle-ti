"""
`services/frota_classificacao_service.py` — categorias e classes de Frotas e Maquinários.

A regra: o nome ORIGINAL do ERP se preserva, a categoria superior só agrupa, o código do ERP é a
identidade e o que já existe nunca é renomeado em silêncio.
"""
from django.core.exceptions import ValidationError
from django.db import connection

from ProjetoEstoque.models import (
    FrotaCentroCusto,
    FrotaClasseMecanica,
    FrotaClasseOperacional,
    FrotaEmpresa,
    FrotaEvento,
    FrotaNatureza,
    FrotaTipo,
    FrotaVeiculo,
    M2MEvento,
)
from ProjetoEstoque.tests import test_frota as base
from services import frota_classificacao_service as classes
from services import frota_service as frota

# As 28 classes operacionais da planilha do ERP (equipamentos motorizados.xlsx) e a categoria que a
# organização sugere para cada uma. "" = nenhuma regra reconhece: vai para "Outros" e a tela avisa.
CLASSES_DO_ERP = {
    "MOTOCICLETA": "Motocicleta",
    "UTILITÁRIO": "Veículo utilitário",
    "AUTOMOVEL": "Veículo utilitário",
    "TRATOR LEVE": "Trator",
    "TRATOR MEDIO": "Trator",
    "TRATOR PESADO": "Trator",
    "GERADOR": "Gerador",
    "MOTOR ESTACIONARIO": "Gerador",
    "COLHEITADEIRA DE TABACO": "Colheitadeira",
    "COLHEITADEIRA DE ALGODÃO": "Colheitadeira",
    "COLHEITADEIRA DE GRÃOS": "Colheitadeira",
    "CAMINHÃO ROLL OFF": "Caminhão",
    "CAMINHÃO APOIO": "Caminhão",
    "CAMINHÃO MUNCK": "Caminhão",
    "CAMINHÃO COMBOIO": "Caminhão",
    "CAMINHÃO PIPA/BOMBEIRO": "Caminhão",
    "CAMINHÃO HERBICIDA": "Caminhão",
    "CAMINHAO OFICINA": "Caminhão",
    "CAVALO MECÂNICO": "Caminhão",
    "PULVERIZADOR": "Pulverizador",
    "EMPILHADEIRA": "Empilhadeira",
    "PA CARREGADEIRA": "Linha amarela",
    "MOTONIVELADORA": "Linha amarela",
    "CONSERVAÇÃO ESTRADAS": "Linha amarela",
    "DISTR. SOLIDOS": "Distribuidor",
    "ADUBAÇAO": "Distribuidor",
    "DESPONTADEIRA": "Implemento agrícola",
    "PLATAFORMA": "",
}


class SugestoesTests(base.FrotaBase):
    def test_toda_classe_do_erp_tem_a_sugestao_documentada(self):
        self.assertEqual(len(CLASSES_DO_ERP), 28)
        for nome, esperada in CLASSES_DO_ERP.items():
            with self.subTest(classe=nome):
                self.assertEqual(classes.sugerir_categoria(nome), esperada)

    def test_so_a_plataforma_fica_sem_categoria_sugerida(self):
        sem = [n for n in CLASSES_DO_ERP if not classes.sugerir_categoria(n)]
        self.assertEqual(sem, ["PLATAFORMA"])

    def test_a_sugestao_ignora_caixa_e_acento(self):
        self.assertEqual(classes.sugerir_categoria("caminhão munck"), "Caminhão")
        self.assertEqual(classes.sugerir_categoria("  TRATOR   leve "), "Trator")

    def test_natureza_sugerida_pela_empresa(self):
        self.assertEqual(classes.sugerir_natureza("SANTA COLOMBA"), FrotaNatureza.PROPRIO)
        self.assertEqual(classes.sugerir_natureza("Santa Colomba Agropecuária"), FrotaNatureza.PROPRIO)
        self.assertEqual(classes.sugerir_natureza("TERCEIROS"), FrotaNatureza.TERCEIRO)
        self.assertEqual(classes.sugerir_natureza("OUTRA EMPRESA"), "")

    def test_toda_categoria_sugerida_existe_ou_e_criavel(self):
        for nome in set(CLASSES_DO_ERP.values()) - {""}:
            categoria, _ = classes.obter_ou_criar_categoria(nome, self.usuario)
            self.assertEqual(categoria.nome, nome)


class CadastroDeApoioTests(base.FrotaBase):
    def eventos_de_config(self):
        return list(M2MEvento.objects.filter(tipo=M2MEvento.Tipo.CONFIG_FROTA).order_by("pk"))

    def test_cria_classe_operacional_com_categoria_e_audita(self):
        c = classes.salvar_cadastro("classe_operacional", {"nome": "TRATOR LEVE", "codigo_erp": "34", "categoria": self.trator.pk}, self.usuario)
        self.assertEqual((c.nome, c.codigo_erp, c.categoria, c.ativo), ("TRATOR LEVE", "34", self.trator, True))
        evento = self.eventos_de_config()[-1]
        self.assertEqual((evento.dados["alvo"], evento.dados["id"], evento.dados["criado"]), ("classe_operacional", c.pk, True))
        self.assertEqual(evento.usuario, self.usuario)

    def test_nome_e_unico_sem_acento_nem_caixa(self):
        classes.salvar_cadastro("classe_operacional", {"nome": "COLHEITADEIRA DE GRÃOS"}, self.usuario)
        for repetido in ("colheitadeira de grãos", "COLHEITADEIRA DE GRAOS", "  colheitadeira   de graos "):
            with self.subTest(nome=repetido), self.assertRaises(ValidationError) as cm:
                classes.salvar_cadastro("classe_operacional", {"nome": repetido}, self.usuario)
            self.assertIn("nome", cm.exception.message_dict)

    def test_codigo_do_erp_e_unico_quando_preenchido_e_o_vazio_repete(self):
        classes.salvar_cadastro("empresa", {"nome": "A", "codigo_erp": "1"}, self.usuario)
        with self.assertRaises(ValidationError) as cm:
            classes.salvar_cadastro("empresa", {"nome": "B", "codigo_erp": "1"}, self.usuario)
        self.assertIn("codigo_erp", cm.exception.message_dict)
        classes.salvar_cadastro("empresa", {"nome": "C"}, self.usuario)
        classes.salvar_cadastro("empresa", {"nome": "D"}, self.usuario)

    def test_editar_so_muda_o_que_veio_e_nao_grava_nada_se_nada_mudou(self):
        e = classes.salvar_cadastro("empresa", {"nome": "SANTA COLOMBA", "codigo_erp": "1", "natureza_padrao": "proprio"}, self.usuario)
        antes = len(self.eventos_de_config())
        classes.salvar_cadastro("empresa", {"nome": "SANTA COLOMBA"}, self.usuario, e)
        self.assertEqual(len(self.eventos_de_config()), antes)
        classes.salvar_cadastro("empresa", {"natureza_padrao": "terceiro"}, self.usuario, e)
        e.refresh_from_db()
        self.assertEqual((e.nome, e.codigo_erp, e.natureza_padrao), ("SANTA COLOMBA", "1", "terceiro"))
        self.assertEqual(self.eventos_de_config()[-1].dados["alteracoes"]["natureza_padrao"], {"de": "proprio", "para": "terceiro"})

    def test_natureza_invalida_da_empresa(self):
        with self.assertRaises(ValidationError) as cm:
            classes.salvar_cadastro("empresa", {"nome": "X", "natureza_padrao": "alugado"}, self.usuario)
        self.assertIn("natureza_padrao", cm.exception.message_dict)

    def test_categoria_inativa_nao_e_escolhida_de_novo_mas_a_que_ja_esta_vale(self):
        FrotaTipo.objects.filter(pk=self.trator.pk).update(ativo=False)
        trator = FrotaTipo.objects.get(pk=self.trator.pk)
        with self.assertRaises(ValidationError) as cm:
            classes.salvar_cadastro("classe_operacional", {"nome": "TRATOR LEVE", "categoria": trator.pk}, self.usuario)
        self.assertIn("categoria", cm.exception.message_dict)
        FrotaTipo.objects.filter(pk=self.trator.pk).update(ativo=True)
        c = classes.salvar_cadastro("classe_operacional", {"nome": "TRATOR LEVE", "categoria": trator.pk}, self.usuario)
        FrotaTipo.objects.filter(pk=self.trator.pk).update(ativo=False)
        classes.salvar_cadastro("classe_operacional", {"nome": "TRATOR LEVE 2", "categoria": trator.pk}, self.usuario, c)   # já era dela

    def test_inativar_nao_apaga_e_preserva_quem_usa(self):
        c = classes.salvar_cadastro("classe_operacional", {"nome": "TRATOR LEVE", "categoria": self.trator.pk}, self.usuario)
        v = frota.criar_veiculo({"nome": "T", "tipo": self.trator.pk, "classe_operacional": c.pk}, self.usuario)
        classes.salvar_cadastro("classe_operacional", {"ativo": False}, self.usuario, c)
        self.assertEqual(FrotaVeiculo.objects.get(pk=v.pk).classe_operacional, c)

    def test_nome_vazio_ou_longo_e_recusado(self):
        for nome in ("", "   ", "x" * 121):
            with self.subTest(nome=nome[:5]), self.assertRaises(ValidationError):
                classes.salvar_cadastro("centro_custo", {"nome": nome}, self.usuario)

    def test_o_tipo_ganha_a_expectativa_de_identificacao(self):
        t = frota.salvar_tipo({"nome": "Motocicleta", "espera_placa": True, "espera_chassi": "1"}, self.usuario)
        self.assertEqual((t.espera_placa, t.espera_chassi), (True, True))
        frota.salvar_tipo({"espera_placa": False}, self.usuario, t)
        t.refresh_from_db()
        self.assertEqual((t.espera_placa, t.espera_chassi), (False, True))
        self.assertEqual(frota.salvar_tipo({"nome": "Outro"}, self.usuario).espera_placa, False)

    def test_listar_com_contagem_e_busca_sem_acento(self):
        c = classes.salvar_cadastro("centro_custo", {"nome": "CAMINHÕES TABACO", "codigo_erp": "32120"}, self.usuario)
        classes.salvar_cadastro("centro_custo", {"nome": "TRATOR LEVE - GRAOS", "codigo_erp": "32107"}, self.usuario)
        frota.criar_veiculo({"nome": "V", "tipo": self.trator.pk, "centro_custo": c.pk}, self.usuario)
        self.assertEqual({x.nome: x.n_equipamentos for x in classes.listar_cadastro("centro_custo")}, {"CAMINHÕES TABACO": 1, "TRATOR LEVE - GRAOS": 0})
        self.assertEqual([x.nome for x in classes.listar_cadastro("centro_custo", "caminhoes")], ["CAMINHÕES TABACO"])
        self.assertEqual([x.nome for x in classes.listar_cadastro("centro_custo", "32107")], ["TRATOR LEVE - GRAOS"])

    def test_cada_cadastro_tem_o_seu_modelo(self):
        self.assertEqual(
            {k: c.modelo for k, c in classes.CATALOGOS.items()},
            {"classe_operacional": FrotaClasseOperacional, "classe_mecanica": FrotaClasseMecanica, "empresa": FrotaEmpresa, "centro_custo": FrotaCentroCusto},
        )


class ReclassificarTests(base.FrotaBase):
    def setUp(self):
        super().setUp()
        self.pesado = FrotaTipo.objects.create(nome="Trator pesado")
        self.classe = classes.salvar_cadastro("classe_operacional", {"nome": "TRATOR PESADO", "categoria": self.trator.pk}, self.usuario)
        self.v1 = frota.criar_veiculo({"nome": "A", "tipo": self.trator.pk, "classe_operacional": self.classe.pk}, self.usuario)
        self.v2 = frota.criar_veiculo({"nome": "B", "tipo": self.caminhao.pk, "classe_operacional": self.classe.pk}, self.usuario)
        self.fora = frota.criar_veiculo({"nome": "C", "tipo": self.caminhao.pk}, self.usuario)

    def test_mudar_a_categoria_da_classe_nao_mexe_nos_equipamentos(self):
        classes.salvar_cadastro("classe_operacional", {"categoria": self.pesado.pk}, self.usuario, self.classe)
        self.assertEqual(FrotaVeiculo.objects.get(pk=self.v1.pk).tipo, self.trator)

    def test_reclassificar_leva_os_equipamentos_da_classe_para_a_categoria_e_audita_cada_um(self):
        classes.salvar_cadastro("classe_operacional", {"categoria": self.pesado.pk}, self.usuario, self.classe)
        mudaram = classes.reclassificar_equipamentos(self.classe, self.usuario)
        self.assertEqual(mudaram, 2)
        self.assertEqual({FrotaVeiculo.objects.get(pk=p).tipo for p in (self.v1.pk, self.v2.pk)}, {self.pesado})
        self.assertEqual(FrotaVeiculo.objects.get(pk=self.fora.pk).tipo, self.caminhao)          # outra classe: intacto
        evento = FrotaEvento.objects.filter(veiculo=self.v2, tipo=FrotaEvento.Tipo.EDICAO).latest("pk")
        self.assertEqual(evento.dados["alteracoes"]["tipo"], {"de": "Caminhão", "para": "Trator pesado"})
        self.assertEqual(evento.dados["origem"], "reclassificacao")

    def test_reclassificar_de_novo_nao_faz_nada(self):
        classes.salvar_cadastro("classe_operacional", {"categoria": self.pesado.pk}, self.usuario, self.classe)
        classes.reclassificar_equipamentos(self.classe, self.usuario)
        self.assertEqual(classes.reclassificar_equipamentos(self.classe, self.usuario), 0)

    def test_classe_sem_categoria_nao_reclassifica_ninguem(self):
        livre = classes.salvar_cadastro("classe_operacional", {"nome": "PLATAFORMA"}, self.usuario)
        frota.criar_veiculo({"nome": "P", "tipo": self.caminhao.pk, "classe_operacional": livre.pk}, self.usuario)
        self.assertEqual(classes.reclassificar_equipamentos(livre, self.usuario), 0)


class ObterOuCriarTests(base.FrotaBase):
    def test_cria_o_que_nao_existe(self):
        c, criado, aviso = classes.obter_ou_criar("centro_custo", "32107", "TRATOR LEVE - GRAOS", self.usuario)
        self.assertEqual((c.codigo_erp, c.nome, criado, aviso), ("32107", "TRATOR LEVE - GRAOS", True, ""))

    def test_reconhece_pelo_codigo_e_nao_altera_o_que_existe(self):
        existente = classes.salvar_cadastro("centro_custo", {"nome": "CAMINHAO - GRAOS", "codigo_erp": "32119"}, self.usuario)
        c, criado, aviso = classes.obter_ou_criar("centro_custo", "32119", "CAMINHÕES GRÃOS", self.usuario)
        self.assertEqual((c.pk, criado), (existente.pk, False))
        self.assertIn("CAMINHÕES GRÃOS", aviso)
        self.assertIn("CAMINHAO - GRAOS", aviso)
        existente.refresh_from_db()
        self.assertEqual(existente.nome, "CAMINHAO - GRAOS")                    # o cadastro não foi renomeado

    def test_mesmo_nome_com_outra_grafia_nao_gera_aviso(self):
        classes.salvar_cadastro("centro_custo", {"nome": "CAMINHÕES GRÃOS", "codigo_erp": "32119"}, self.usuario)
        _, criado, aviso = classes.obter_ou_criar("centro_custo", "32119", "caminhoes graos", self.usuario)
        self.assertEqual((criado, aviso), (False, ""))

    def test_sem_codigo_reconhece_pelo_nome(self):
        existente = classes.salvar_cadastro("classe_mecanica", {"nome": "LINHA AMARELA"}, self.usuario)
        c, criado, _ = classes.obter_ou_criar("classe_mecanica", "", "linha amarela", self.usuario)
        self.assertEqual((c.pk, criado), (existente.pk, False))

    def test_passa_os_campos_proprios_na_criacao(self):
        e, criado, _ = classes.obter_ou_criar("empresa", "2", "TERCEIROS", self.usuario, natureza_padrao="terceiro")
        self.assertEqual((e.natureza_padrao, criado), ("terceiro", True))

    def test_categoria_existente_e_reaproveitada_com_acento_e_caixa_diferentes(self):
        categoria, criada = classes.obter_ou_criar_categoria("veículo UTILITÁRIO", self.usuario)
        self.assertEqual((categoria.nome, criada), ("Veículo utilitário", False))
        self.assertEqual(FrotaTipo.objects.filter(nome__icontains="utilit").count(), 1)

    def test_categoria_nova_de_moto_nasce_esperando_placa_e_chassi(self):
        moto, criada = classes.obter_ou_criar_categoria("Motocicleta", self.usuario)
        self.assertEqual((criada, moto.espera_placa, moto.espera_chassi), (True, True, True))
        gerador, _ = classes.obter_ou_criar_categoria("Gerador", self.usuario)
        self.assertEqual((gerador.espera_placa, gerador.espera_chassi), (False, False))

    def test_as_categorias_semeadas_pela_migration_que_esperam_placa(self):
        """A 0198 marca Caminhão e Veículo utilitário: é o que faz "sem placa" ser pendência num caminhão."""
        for nome in ("Caminhão", "Veículo utilitário"):
            t = FrotaTipo.objects.get(nome=nome)
            self.assertEqual((t.espera_placa, t.espera_chassi), (True, True), nome)
        for nome in ("Trator", "Pulverizador", "Colheitadeira"):
            t = FrotaTipo.objects.get(nome=nome)
            self.assertEqual((t.espera_placa, t.espera_chassi), (False, False), nome)
