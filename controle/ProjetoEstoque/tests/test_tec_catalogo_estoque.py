"""
Tecnologia agrícola do Zelo Agro — catálogo (fabricantes, categorias, modelos, unidades, locais,
fornecedores) e estoque tecnológico (equipamentos individuais + materiais por quantidade + razão).

Cenários 1 e 2 da spec (cadastro de equipamento e controle de estoque) e a parte do cenário 8
(integridade) que não depende da instalação.
"""

import importlib
import uuid
from decimal import Decimal

from django.apps import apps as django_apps
from django.contrib.auth.models import Group, Permission, User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import ForeignKey
from django.test import TestCase

from ProjetoEstoque.models import (
    PERMISSOES_TEC,
    AgroFornecedor,
    AgroLocal,
    AgroUnidadeMedida,
    Item,
    Localidade,
    MovimentacaoItem,
    TecCategoria,
    TecEquipamento,
    TecEvento,
    TecFabricante,
    TecModelo,
    TecMovimentacao,
    TecSaldo,
)
from services import frota_service
from services import tec_catalogo_service as cat
from services import tec_estoque_service as est

D = Decimal
Situacao = TecEquipamento.Situacao


def erros(excecao: ValidationError) -> dict:
    return excecao.message_dict if hasattr(excecao, "error_dict") else {"__all__": excecao.messages}


class TecBase(TestCase):
    def setUp(self):
        self.usuario = User.objects.create_user("tec_operador", "t@t.com", "x", first_name="Tânia", last_name="Operadora")
        self.karitel, self.rio = frota_service.fazendas()
        self.deposito = cat.salvar_local({"nome": "Almoxarifado", "fazenda": self.karitel}, self.usuario)
        self.oficina = cat.salvar_local({"nome": "Oficina", "fazenda": self.rio}, self.usuario)
        self.john_deere = TecFabricante.objects.get(nome="John Deere")
        self.motorola = TecFabricante.objects.get(nome="Motorola")
        self.cat_monitor = TecCategoria.objects.get(nome="Monitor / display")
        self.cat_radio = TecCategoria.objects.get(nome="Rádio portátil (HT)")
        self.cat_cabo = TecCategoria.objects.get(nome="Cabo")
        self.un = AgroUnidadeMedida.objects.get(sigla="un")
        self.metro = AgroUnidadeMedida.objects.get(sigla="m")

    # fábricas
    def modelo_individual(self, nome="G5", fabricante=None, categoria=None, **extra):
        return cat.salvar_modelo(
            {"fabricante": fabricante or self.john_deere, "categoria": categoria or self.cat_monitor, "nome": nome, "controle": "individual", **extra},
            self.usuario,
        )

    def modelo_material(self, nome="Cabo UTP", unidade=None, **extra):
        return cat.salvar_modelo(
            {"fabricante": self.motorola, "categoria": self.cat_cabo, "nome": nome, "controle": "quantidade", "unidade": unidade or self.metro, **extra},
            self.usuario,
        )

    def equipamento(self, modelo=None, serie="SN-001", local=None, **extra):
        return est.criar_equipamento(
            {"modelo": modelo or self.modelo_individual(), "numero_serie": serie, "local": local or self.deposito, **extra}, self.usuario,
        ).equipamento


# ═════════════════════════════════════════════════════════════
# 1. Dados semeados, permissões e a separação do TI
# ═════════════════════════════════════════════════════════════

class SementesETests(TestCase):
    def test_so_os_quatro_fabricantes_que_a_spec_nomeia(self):
        self.assertEqual(
            set(TecFabricante.objects.values_list("nome", flat=True)), {"Solinftec", "John Deere", "Stara", "Motorola"},
        )

    def test_nenhum_modelo_de_fabrica(self):
        """G5, StarFire, JDLink… são exemplos do texto da spec, não cadastro."""
        self.assertEqual(TecModelo.objects.count(), 0)

    def test_categorias_cobrem_os_grupos_e_os_dois_controles(self):
        grupos = set(TecCategoria.objects.values_list("grupo", flat=True))
        for grupo in ("agricultura_precisao", "radiocomunicacao", "rede", "conectividade", "energia", "cabeamento", "sensores"):
            self.assertIn(grupo, grupos)
        self.assertEqual(TecCategoria.objects.get(nome="Rádio portátil (HT)").controle_padrao, "individual")
        self.assertEqual(TecCategoria.objects.get(nome="Cabo").controle_padrao, "quantidade")
        self.assertEqual(TecCategoria.objects.get(nome="Injetor PoE").controle_padrao, "quantidade")

    def test_unidades_de_medida_e_fracionavel(self):
        self.assertTrue(AgroUnidadeMedida.objects.get(sigla="m").fracionavel)
        self.assertFalse(AgroUnidadeMedida.objects.get(sigla="un").fracionavel)

    def test_as_13_permissoes_existem_e_a_migration_repete_o_models(self):
        codigos = {c for c, _ in PERMISSOES_TEC}
        self.assertEqual(len(codigos), 13)
        existentes = set(Permission.objects.filter(content_type__app_label="ProjetoEstoque", codename__in=codigos).values_list("codename", flat=True))
        self.assertEqual(existentes, codigos)
        migration = importlib.import_module("ProjetoEstoque.migrations.0194_tec_dados_permissoes")
        self.assertEqual(list(migration.PERMISSOES), list(PERMISSOES_TEC))

    def test_grupos_gestao_e_consulta(self):
        gestao = Group.objects.get(name="Tecnologia Agrícola — Gestão")
        consulta = Group.objects.get(name="Tecnologia Agrícola — Consulta")
        gestao_codigos = set(gestao.permissions.values_list("codename", flat=True))
        self.assertIn("instalar_tec_equipamento", gestao_codigos)
        self.assertNotIn("administrar_tec_config", gestao_codigos)
        self.assertEqual(set(consulta.permissions.values_list("codename", flat=True)), {"ver_tec_estoque", "ver_tec_historico"})


class SeparacaoDoEstoqueDeTITests(TecBase):
    def test_nenhum_model_do_dominio_aponta_para_o_estoque_de_ti(self):
        proibidos = {Item, MovimentacaoItem, django_apps.get_model("ProjetoEstoque", "Fornecedor"), django_apps.get_model("ProjetoEstoque", "Categoria"), django_apps.get_model("ProjetoEstoque", "Subtipo")}
        modelos = [m for m in django_apps.get_app_config("ProjetoEstoque").get_models() if m.__name__.startswith(("Tec", "Agro"))]
        self.assertGreaterEqual(len(modelos), 9)
        for modelo in modelos:
            for campo in modelo._meta.get_fields():
                if campo.is_relation and campo.concrete:
                    with self.subTest(modelo=modelo.__name__, campo=campo.name):
                        self.assertNotIn(campo.related_model, proibidos, f"{modelo.__name__}.{campo.name} aponta para o estoque de TI")

    def test_o_ciclo_completo_nao_toca_no_estoque_de_ti(self):
        antes = (Item.objects.count(), MovimentacaoItem.objects.count(), django_apps.get_model("ProjetoEstoque", "Fornecedor").objects.count())
        eq = self.equipamento()
        est.mover_equipamento_de_local(eq, self.oficina, self.usuario, "para a oficina")
        est.inativar_equipamento(eq, self.usuario, "baixa")
        mat = self.modelo_material()
        est.registrar_entrada_material(mat, self.deposito, 10, self.usuario)
        est.registrar_saida_material(mat, self.deposito, 3, self.usuario, "consumo")
        self.assertEqual(antes, (Item.objects.count(), MovimentacaoItem.objects.count(), django_apps.get_model("ProjetoEstoque", "Fornecedor").objects.count()))


# ═════════════════════════════════════════════════════════════
# 2. Catálogo
# ═════════════════════════════════════════════════════════════

class CatalogoTests(TecBase):
    def test_fabricante_unico_sem_distinguir_caixa(self):
        novo = cat.salvar_fabricante({"nome": "  Raven   Industries "}, self.usuario)
        self.assertEqual(novo.nome, "Raven Industries")
        with self.assertRaises(ValidationError) as ctx:
            cat.salvar_fabricante({"nome": "RAVEN industries"}, self.usuario)
        self.assertIn("nome", erros(ctx.exception))
        with self.assertRaises(ValidationError):
            cat.salvar_fabricante({"nome": "   "}, self.usuario)

    def test_edicao_audita_de_para_e_sem_mudanca_nao_grava(self):
        fab = cat.salvar_fabricante({"nome": "Trimble"}, self.usuario)
        eventos = TecEvento.objects.filter(alvo="fabricante", alvo_id=fab.pk)
        self.assertEqual(eventos.count(), 1)
        cat.salvar_fabricante({"nome": "Trimble"}, self.usuario, fab)           # nada mudou
        self.assertEqual(eventos.count(), 1)
        cat.salvar_fabricante({"nome": "Trimble Navigation"}, self.usuario, fab)
        ultimo = eventos.first()
        self.assertEqual(ultimo.tipo, "edicao")
        self.assertEqual(ultimo.dados["alteracoes"]["nome"], {"de": "Trimble", "para": "Trimble Navigation"})

    def test_inativar_fabricante_nao_apaga_os_modelos(self):
        modelo = self.modelo_individual()
        cat.salvar_fabricante({"ativo": False}, self.usuario, self.john_deere)
        self.assertTrue(TecModelo.objects.filter(pk=modelo.pk).exists())
        self.assertNotIn(self.john_deere, list(cat.listar_fabricantes(somente_ativos=True)))
        with self.assertRaises(ValidationError) as ctx:     # fabricante inativo não recebe modelo novo
            self.modelo_individual("Outro")
        self.assertIn("fabricante", erros(ctx.exception))

    def test_categoria_valida_grupo_e_controle(self):
        nova = cat.salvar_categoria({"nome": "Estação meteorológica", "grupo": "sensores", "controle_padrao": "individual"}, self.usuario)
        self.assertEqual(nova.grupo, "sensores")
        with self.assertRaises(ValidationError):
            cat.salvar_categoria({"nome": "X", "grupo": "inventado"}, self.usuario)
        with self.assertRaises(ValidationError):
            cat.salvar_categoria({"nome": "Y", "controle_padrao": "outro"}, self.usuario)

    def test_unidade_unica_e_nao_fracionavel_com_saldo_fracionado_e_recusada(self):
        with self.assertRaises(ValidationError):
            cat.salvar_unidade({"sigla": "M", "nome": "Outro metro"}, self.usuario)        # 'm' já existe (sem distinguir caixa)
        mat = self.modelo_material()
        est.registrar_entrada_material(mat, self.deposito, "2,5", self.usuario)
        with self.assertRaises(ValidationError) as ctx:
            cat.salvar_unidade({"fracionavel": False}, self.usuario, self.metro)
        self.assertIn("fracionavel", erros(ctx.exception))

    # ── modelo ──
    def test_modelo_individual_descarta_unidade_e_quantidade_exige(self):
        individual = self.modelo_individual(unidade=self.metro.pk)
        self.assertIsNone(individual.unidade)
        with self.assertRaises(ValidationError) as ctx:
            cat.salvar_modelo({"fabricante": self.motorola, "categoria": self.cat_cabo, "nome": "Sem unidade", "controle": "quantidade"}, self.usuario)
        self.assertIn("unidade", erros(ctx.exception))

    def test_controle_padrao_da_categoria_e_usado_quando_nao_informado(self):
        modelo = cat.salvar_modelo({"fabricante": self.motorola, "categoria": self.cat_cabo, "nome": "Cabo coaxial", "unidade": self.metro}, self.usuario)
        self.assertEqual(modelo.controle, "quantidade")
        modelo2 = cat.salvar_modelo({"fabricante": self.motorola, "categoria": self.cat_radio, "nome": "HT genérico"}, self.usuario)
        self.assertEqual(modelo2.controle, "individual")

    def test_modelo_unico_por_fabricante_mas_o_mesmo_nome_vale_em_outro_fabricante(self):
        self.modelo_individual("Display X")
        with self.assertRaises(ValidationError):
            self.modelo_individual("display x")
        outro = self.modelo_individual("Display X", fabricante=TecFabricante.objects.get(nome="Stara"))
        self.assertEqual(outro.nome, "Display X")

    def test_estoque_minimo_do_individual_e_inteiro_e_nunca_negativo(self):
        with self.assertRaises(ValidationError):
            self.modelo_individual("A", estoque_minimo="2,5")
        with self.assertRaises(ValidationError):
            self.modelo_individual("B", estoque_minimo="-1")
        self.assertEqual(self.modelo_individual("C", estoque_minimo="3").estoque_minimo, D("3"))
        fracionado = self.modelo_material("Cabo fino", estoque_minimo="12,5")
        self.assertEqual(fracionado.estoque_minimo, D("12.5"))
        inteiro = AgroUnidadeMedida.objects.get(sigla="cx")
        with self.assertRaises(ValidationError):
            self.modelo_material("Conector", unidade=inteiro, estoque_minimo="1,5")

    def test_modelo_com_historico_nao_muda_controle_nem_fabricante_nem_unidade(self):
        modelo = self.modelo_individual("Com histórico")
        self.equipamento(modelo)
        for mudanca in ({"controle": "quantidade", "unidade": self.metro.pk}, {"fabricante": self.motorola.pk}):
            with self.subTest(mudanca=mudanca), self.assertRaises(ValidationError):
                cat.salvar_modelo(mudanca, self.usuario, modelo)
        livre = self.modelo_individual("Sem histórico")
        mudado = cat.salvar_modelo({"controle": "quantidade", "unidade": self.metro.pk}, self.usuario, livre)
        self.assertEqual(mudado.controle, "quantidade")
        # a categoria é só classificação: pode mudar sempre
        self.assertEqual(cat.salvar_modelo({"categoria": self.cat_radio.pk}, self.usuario, modelo).categoria, self.cat_radio)

    def test_filtros_da_lista_de_modelos(self):
        self.modelo_individual("G5")
        self.modelo_material("Cabo UTP")
        self.assertEqual(cat.listar_modelos({"controle": "quantidade"}).count(), 1)
        self.assertEqual(cat.listar_modelos({"fabricante": self.john_deere.pk}).count(), 1)
        self.assertEqual(cat.listar_modelos({"q": "utp"}).count(), 1)
        self.assertEqual(cat.listar_modelos({"fabricante": "abc"}).count(), 0)      # id inválido: lista vazia, nunca 500
        self.assertEqual(cat.listar_modelos({"grupo": "cabeamento"}).count(), 1)

    # ── local ──
    def test_local_so_aceita_karitel_ou_rio_do_meio(self):
        outra = Localidade.objects.create(local="Sala de reuniões")
        with self.assertRaises(ValidationError) as ctx:
            cat.salvar_local({"nome": "Depósito", "fazenda": outra}, self.usuario)
        self.assertIn("fazenda", erros(ctx.exception))

    def test_local_unico_na_fazenda_e_inativar_com_material_e_recusado(self):
        with self.assertRaises(ValidationError):
            cat.salvar_local({"nome": "almoxarifado", "fazenda": self.karitel}, self.usuario)
        self.assertEqual(cat.salvar_local({"nome": "Almoxarifado", "fazenda": self.rio}, self.usuario).fazenda, self.rio)   # outro local de outra fazenda
        self.equipamento()
        with self.assertRaises(ValidationError) as ctx:
            cat.salvar_local({"ativo": False}, self.usuario, self.deposito)
        self.assertIn("ativo", erros(ctx.exception))

    # ── fornecedor ──
    def test_fornecedor_valida_cnpj_e_cpf_e_guarda_so_digitos(self):
        f = cat.salvar_fornecedor({"nome": "AgroTech", "documento": "11.222.333/0001-81"}, self.usuario)
        self.assertEqual(f.documento, "11222333000181")
        cpf = cat.salvar_fornecedor({"nome": "Autônomo", "documento": "529.982.247-25"}, self.usuario)
        self.assertEqual(cpf.documento, "52998224725")
        for invalido in ("11.222.333/0001-82", "111.111.111-11", "123"):
            with self.subTest(doc=invalido), self.assertRaises(ValidationError):
                cat.salvar_fornecedor({"nome": "X", "documento": invalido}, self.usuario)
        with self.assertRaises(ValidationError) as ctx:          # documento repetido
            cat.salvar_fornecedor({"nome": "Outro", "documento": "11222333000181"}, self.usuario)
        self.assertIn("documento", erros(ctx.exception))
        self.assertEqual(cat.salvar_fornecedor({"nome": "Sem doc"}, self.usuario).documento, "")
        self.assertEqual(cat.salvar_fornecedor({"nome": "Sem doc 2"}, self.usuario).documento, "")   # vazio não conflita

    def test_fornecedor_email_invalido(self):
        with self.assertRaises(ValidationError) as ctx:
            cat.salvar_fornecedor({"nome": "X", "email": "isso-nao-e-email"}, self.usuario)
        self.assertIn("email", erros(ctx.exception))


# ═════════════════════════════════════════════════════════════
# 3. Equipamentos individuais (cenário 1)
# ═════════════════════════════════════════════════════════════

class EquipamentoIndividualTests(TecBase):
    def test_cadastro_gera_codigo_evento_e_entrada_no_razao(self):
        fornecedor = cat.salvar_fornecedor({"nome": "Fornecedor"}, self.usuario)
        r = est.criar_equipamento(
            {"modelo": self.modelo_individual(), "numero_serie": "  SN 77 ", "patrimonio": "pat-9", "local": self.deposito,
             "data_aquisicao": "2026-10-01", "fornecedor": fornecedor, "observacoes": "Tela 12\"", "documento": "NF 123"},
            self.usuario,
        )
        eq = r.equipamento
        self.assertEqual(eq.codigo, f"EQ-{eq.pk:05d}")
        self.assertEqual((eq.numero_serie, eq.patrimonio, eq.situacao, eq.local), ("SN 77", "PAT-9", "disponivel", self.deposito))
        self.assertEqual(eq.criado_por, self.usuario)
        mov = r.movimentacoes[0]
        self.assertEqual((mov.tipo, mov.equipamento, mov.destino_tipo, mov.destino_id, mov.documento, mov.fornecedor), ("entrada", eq, "estoque", self.deposito.pk, "NF 123", fornecedor))
        self.assertEqual(mov.usuario_nome, "tec_operador")
        self.assertTrue(TecEvento.objects.filter(alvo="equipamento", alvo_id=eq.pk, tipo="cadastro").exists())

    def test_cenario_1_quatro_marcas_aparecem_no_inventario_com_a_classificacao(self):
        casos = [
            ("John Deere", "Monitor / display", "G5", "JD-1"),
            ("Stara", "Controlador eletrônico", "Controlador S", "ST-1"),
            ("Solinftec", "Módulo embarcado", "Módulo Y", "SO-1"),
            ("Motorola", "Rádio portátil (HT)", "HT-A", "MO-1"),
        ]
        for fabricante, categoria, nome, serie in casos:
            modelo = cat.salvar_modelo(
                {"fabricante": TecFabricante.objects.get(nome=fabricante), "categoria": TecCategoria.objects.get(nome=categoria), "nome": nome}, self.usuario,
            )
            self.equipamento(modelo, serie)
        lista = list(est.listar_equipamentos())
        self.assertEqual(len(lista), 4)
        self.assertEqual({(e.modelo.fabricante.nome, e.modelo.categoria.nome) for e in lista}, {(c[0], c[1]) for c in casos})
        self.assertEqual(est.listar_equipamentos({"fabricante": TecFabricante.objects.get(nome="Stara").pk}).count(), 1)
        self.assertEqual(est.listar_equipamentos({"grupo": "radiocomunicacao"}).count(), 1)
        self.assertEqual(est.listar_equipamentos({"q": "mo-1"}).count(), 1)
        self.assertEqual(est.listar_equipamentos({"situacao": "disponivel"}).count(), 4)
        self.assertEqual(est.listar_equipamentos({"modelo": "x"}).count(), 0)                      # id inválido: vazio

    def test_exige_serie_ou_patrimonio_e_local(self):
        with self.assertRaises(ValidationError) as ctx:
            est.criar_equipamento({"modelo": self.modelo_individual(), "local": self.deposito}, self.usuario)
        self.assertIn("numero_serie", erros(ctx.exception))
        with self.assertRaises(ValidationError) as ctx:
            est.criar_equipamento({"modelo": self.modelo_individual("Z"), "patrimonio": "P1"}, self.usuario)
        self.assertIn("local", erros(ctx.exception))
        self.assertEqual(est.criar_equipamento({"modelo": self.modelo_individual("W"), "patrimonio": "P2", "local": self.deposito}, self.usuario).equipamento.numero_serie, "")

    def test_serie_unica_por_fabricante_e_patrimonio_unico(self):
        modelo_a = self.modelo_individual("Modelo A")
        modelo_b = self.modelo_individual("Modelo B")
        primeiro = self.equipamento(modelo_a, "SN-DUP")
        with self.assertRaises(ValidationError) as ctx:       # mesma série, OUTRO modelo do mesmo fabricante
            self.equipamento(modelo_b, "sn-dup")
        self.assertIn(primeiro.codigo, str(erros(ctx.exception)["numero_serie"]))      # a mensagem aponta o equipamento que já tem a série
        stara = cat.salvar_modelo({"fabricante": TecFabricante.objects.get(nome="Stara"), "categoria": self.cat_monitor, "nome": "Modelo S"}, self.usuario)
        self.assertEqual(self.equipamento(stara, "SN-DUP").numero_serie, "SN-DUP")          # outro fabricante pode repetir
        est.criar_equipamento({"modelo": modelo_a, "patrimonio": "PAT-1", "local": self.deposito}, self.usuario)
        with self.assertRaises(ValidationError) as ctx:
            est.criar_equipamento({"modelo": modelo_a, "patrimonio": "pat-1", "local": self.deposito}, self.usuario)
        self.assertIn("patrimonio", erros(ctx.exception))

    def test_modelo_por_quantidade_nao_vira_equipamento_e_vice_versa(self):
        with self.assertRaises(ValidationError) as ctx:
            est.criar_equipamento({"modelo": self.modelo_material(), "numero_serie": "X", "local": self.deposito}, self.usuario)
        self.assertIn("quantidade", str(erros(ctx.exception)["modelo"]))
        with self.assertRaises(ValidationError) as ctx:
            est.registrar_entrada_material(self.modelo_individual("Individual"), self.deposito, 1, self.usuario)
        self.assertIn("individualmente", str(erros(ctx.exception)["modelo"]))

    def test_modelo_inativo_e_local_inativo_nao_recebem(self):
        modelo = self.modelo_individual()
        cat.salvar_modelo({"ativo": False}, self.usuario, modelo)
        with self.assertRaises(ValidationError):
            est.criar_equipamento({"modelo": modelo, "numero_serie": "1", "local": self.deposito}, self.usuario)
        vazio = cat.salvar_local({"nome": "Vazio", "fazenda": self.karitel}, self.usuario)
        cat.salvar_local({"ativo": False}, self.usuario, vazio)
        with self.assertRaises(ValidationError):
            est.criar_equipamento({"modelo": self.modelo_individual("N"), "numero_serie": "2", "local": vazio}, self.usuario)

    def test_data_de_aquisicao_no_futuro_e_recusada_e_aceita_dd_mm_aaaa(self):
        modelo = self.modelo_individual()
        with self.assertRaises(ValidationError):
            est.criar_equipamento({"modelo": modelo, "numero_serie": "F", "local": self.deposito, "data_aquisicao": "2099-01-01"}, self.usuario)
        eq = est.criar_equipamento({"modelo": modelo, "numero_serie": "G", "local": self.deposito, "data_aquisicao": "15/10/2026"}, self.usuario).equipamento
        self.assertEqual(str(eq.data_aquisicao), "2026-10-15")

    def test_token_repetido_devolve_o_primeiro_sem_duplicar(self):
        token = uuid.uuid4()
        dados = {"modelo": self.modelo_individual(), "numero_serie": "T1", "local": self.deposito}
        primeiro = est.criar_equipamento(dados, self.usuario, token=token)
        segundo = est.criar_equipamento(dados, self.usuario, token=str(token))
        self.assertFalse(primeiro.repetida)
        self.assertTrue(segundo.repetida)
        self.assertEqual(primeiro.equipamento.pk, segundo.equipamento.pk)
        self.assertEqual(TecEquipamento.objects.count(), 1)
        self.assertEqual(TecMovimentacao.objects.count(), 1)

    def test_sem_usuario_nao_registra(self):
        with self.assertRaises(ValidationError):
            est.criar_equipamento({"modelo": self.modelo_individual(), "numero_serie": "A", "local": self.deposito}, None)

    def test_edicao_audita_e_nao_mexe_em_situacao_nem_local(self):
        eq = self.equipamento(serie="ANTIGO")
        est.editar_equipamento(eq, {"numero_serie": "NOVO", "observacoes": "revisado", "situacao": "inativo", "local": self.oficina.pk}, self.usuario)
        eq.refresh_from_db()
        self.assertEqual((eq.numero_serie, eq.observacoes, eq.situacao, eq.local), ("NOVO", "revisado", "disponivel", self.deposito))
        evento = TecEvento.objects.filter(alvo="equipamento", alvo_id=eq.pk, tipo="edicao").get()
        self.assertEqual(evento.dados["alteracoes"]["numero_serie"], {"de": "ANTIGO", "para": "NOVO"})
        antes = TecEvento.objects.count()
        est.editar_equipamento(eq, {"numero_serie": "NOVO"}, self.usuario)           # nada mudou
        self.assertEqual(TecEvento.objects.count(), antes)

    def test_edicao_nao_deixa_o_equipamento_sem_identificacao(self):
        eq = self.equipamento(serie="UNICO")
        with self.assertRaises(ValidationError):
            est.editar_equipamento(eq, {"numero_serie": ""}, self.usuario)

    def test_mover_de_local_gera_transferencia_e_preserva_a_origem(self):
        eq = self.equipamento()
        r = est.mover_equipamento_de_local(eq, self.oficina, self.usuario, "levado para a oficina")
        eq.refresh_from_db()
        self.assertEqual((eq.situacao, eq.local), ("disponivel", self.oficina))
        mov = r.movimentacoes[0]
        self.assertEqual((mov.tipo, mov.origem_id, mov.destino_id, mov.motivo), ("transferencia", self.deposito.pk, self.oficina.pk, "levado para a oficina"))
        with self.assertRaises(ValidationError):                 # mesmo local
            est.mover_equipamento_de_local(eq, self.oficina, self.usuario, "de novo")
        with self.assertRaises(ValidationError):                 # sem motivo
            est.mover_equipamento_de_local(eq, self.deposito, self.usuario, "  ")

    def test_inativar_baixa_e_nao_reinativa(self):
        eq = self.equipamento()
        r = est.inativar_equipamento(eq, self.usuario, "queimou")
        eq.refresh_from_db()
        self.assertEqual((eq.situacao, eq.local), ("inativo", None))
        self.assertEqual((r.movimentacoes[0].tipo, r.movimentacoes[0].origem_tipo, r.movimentacoes[0].destino_tipo), ("inativacao", "estoque", "externo"))
        with self.assertRaises(ValidationError):
            est.inativar_equipamento(eq, self.usuario, "de novo")
        with self.assertRaises(ValidationError):                 # inativo não muda de local
            est.mover_equipamento_de_local(eq, self.oficina, self.usuario, "x")

    def test_equipamento_instalado_nao_se_inativa(self):
        eq = self.equipamento()
        TecEquipamento.objects.filter(pk=eq.pk).update(situacao="instalado", local=None)      # simula a instalação (E4)
        with self.assertRaises(ValidationError) as ctx:
            est.inativar_equipamento(eq, self.usuario, "x")
        self.assertIn("retire-o", str(erros(ctx.exception)))

    def test_timeline_junta_cadastro_e_razao_do_mais_novo_para_o_mais_antigo(self):
        eq = self.equipamento()
        est.mover_equipamento_de_local(eq, self.oficina, self.usuario, "mudança")
        est.editar_equipamento(eq, {"observacoes": "obs"}, self.usuario)
        linha = est.linha_do_tempo_equipamento(eq)
        titulos = [i["titulo"] for i in linha]
        self.assertEqual(set(titulos), {"Cadastrado", "Entrada no estoque", "Transferência", "Editado"})
        self.assertEqual([i["em"] for i in linha], sorted((i["em"] for i in linha), reverse=True))
        transferencia = next(i for i in linha if i["titulo"] == "Transferência")
        self.assertEqual((transferencia["motivo"], transferencia["usuario"]), ("mudança", "tec_operador"))
        self.assertIn("Almoxarifado", transferencia["detalhe"])


# ═════════════════════════════════════════════════════════════
# 4. Materiais por quantidade (cenário 2)
# ═════════════════════════════════════════════════════════════

class MaterialPorQuantidadeTests(TecBase):
    def test_entrada_e_saida_mantem_saldo_e_razao(self):
        cabo = self.modelo_material()
        est.registrar_entrada_material(cabo, self.deposito, "100,5", self.usuario, documento="NF 9", motivo="compra")
        r = est.registrar_saida_material(cabo, self.deposito, "40", self.usuario, "instalação na torre", destino="Torre Norte")
        self.assertEqual(r.saldo.quantidade, D("60.500"))
        self.assertEqual(r.movimentacoes[0].saldo_depois, D("60.500"))
        self.assertEqual((r.movimentacoes[0].origem_tipo, r.movimentacoes[0].destino_rotulo), ("estoque", "Torre Norte"))
        self.assertEqual(TecMovimentacao.objects.filter(modelo=cabo).count(), 2)
        self.assertTrue(est.conferir_consistencia()["ok"])

    def test_saldo_insuficiente_recusa_e_nao_deixa_negativo_nem_registra(self):
        cabo = self.modelo_material()
        est.registrar_entrada_material(cabo, self.deposito, 10, self.usuario)
        with self.assertRaises(ValidationError) as ctx:
            est.registrar_saida_material(cabo, self.deposito, 10.001, self.usuario, "x")
        self.assertIn("Saldo insuficiente", str(erros(ctx.exception)["quantidade"]))
        self.assertEqual(TecSaldo.objects.get(modelo=cabo, local=self.deposito).quantidade, D("10"))
        self.assertEqual(TecMovimentacao.objects.filter(modelo=cabo, tipo="saida").count(), 0)
        with self.assertRaises(ValidationError):                          # local que nunca teve saldo
            est.registrar_saida_material(cabo, self.oficina, 1, self.usuario, "x")

    def test_quantidade_precisa_ser_positiva_e_respeitar_a_unidade(self):
        conector = self.modelo_material("Conector RJ45", unidade=AgroUnidadeMedida.objects.get(sigla="un"))
        for invalida in (0, -1, "abc", "1,5", "", None):
            with self.subTest(qtd=invalida), self.assertRaises(ValidationError):
                est.registrar_entrada_material(conector, self.deposito, invalida, self.usuario)
        self.assertEqual(est.registrar_entrada_material(conector, self.deposito, "12", self.usuario).saldo.quantidade, D("12"))
        metro = self.modelo_material("Cabo coaxial")
        self.assertEqual(est.registrar_entrada_material(metro, self.deposito, "0,25", self.usuario).saldo.quantidade, D("0.25"))
        with self.assertRaises(ValidationError):                          # 4 casas
            est.registrar_entrada_material(metro, self.deposito, "0,1234", self.usuario)

    def test_saida_e_ajuste_exigem_motivo_e_entrada_nao(self):
        cabo = self.modelo_material()
        est.registrar_entrada_material(cabo, self.deposito, 5, self.usuario)                       # sem motivo: ok
        with self.assertRaises(ValidationError):
            est.registrar_saida_material(cabo, self.deposito, 1, self.usuario, "")
        with self.assertRaises(ValidationError):
            est.ajustar_saldo(cabo, self.deposito, 3, self.usuario, "  ")

    def test_ajuste_para_cima_e_para_baixo_vira_razao_nao_edicao(self):
        cabo = self.modelo_material()
        est.registrar_entrada_material(cabo, self.deposito, 10, self.usuario)
        baixo = est.ajustar_saldo(cabo, self.deposito, 7, self.usuario, "contagem do inventário")
        self.assertEqual((baixo.saldo.quantidade, baixo.movimentacoes[0].quantidade, baixo.movimentacoes[0].origem_tipo), (D("7"), D("3"), "estoque"))
        cima = est.ajustar_saldo(cabo, self.deposito, 12, self.usuario, "achou uma caixa")
        self.assertEqual((cima.saldo.quantidade, cima.movimentacoes[0].quantidade, cima.movimentacoes[0].destino_tipo), (D("12"), D("5"), "estoque"))
        zerado = est.ajustar_saldo(cabo, self.deposito, 0, self.usuario, "acabou")
        self.assertEqual(zerado.saldo.quantidade, D("0"))
        with self.assertRaises(ValidationError) as ctx:
            est.ajustar_saldo(cabo, self.deposito, 0, self.usuario, "de novo")
        self.assertIn("já é", str(erros(ctx.exception)))
        self.assertTrue(est.conferir_consistencia()["ok"])

    def test_transferencia_entre_locais_move_o_saldo_e_grava_uma_linha(self):
        cabo = self.modelo_material()
        est.registrar_entrada_material(cabo, self.deposito, 20, self.usuario)
        r = est.transferir_material_entre_locais(cabo, self.deposito, self.oficina, 8, self.usuario, "reforço da oficina")
        self.assertEqual(TecSaldo.objects.get(modelo=cabo, local=self.deposito).quantidade, D("12"))
        self.assertEqual(TecSaldo.objects.get(modelo=cabo, local=self.oficina).quantidade, D("8"))
        self.assertEqual((len(r.movimentacoes), r.movimentacoes[0].saldo_depois), (1, D("8")))
        with self.assertRaises(ValidationError):                          # mesmo local
            est.transferir_material_entre_locais(cabo, self.deposito, self.deposito, 1, self.usuario, "x")
        with self.assertRaises(ValidationError):                          # mais do que há: nada muda em NENHUM dos dois locais
            est.transferir_material_entre_locais(cabo, self.deposito, self.oficina, 13, self.usuario, "x")
        self.assertEqual(TecSaldo.objects.get(modelo=cabo, local=self.deposito).quantidade, D("12"))
        self.assertEqual(TecSaldo.objects.get(modelo=cabo, local=self.oficina).quantidade, D("8"))
        self.assertTrue(est.conferir_consistencia()["ok"])

    def test_token_repetido_nao_duplica_a_entrada(self):
        cabo = self.modelo_material()
        token = uuid.uuid4()
        est.registrar_entrada_material(cabo, self.deposito, 10, self.usuario, token=token)
        repetida = est.registrar_entrada_material(cabo, self.deposito, 10, self.usuario, token=token)
        self.assertTrue(repetida.repetida)
        self.assertEqual(TecSaldo.objects.get(modelo=cabo, local=self.deposito).quantidade, D("10"))
        self.assertEqual(TecMovimentacao.objects.count(), 1)

    def test_cenario_2_serializados_e_por_quantidade_juntos_com_saldos_corretos(self):
        monitor = self.modelo_individual("G5", estoque_minimo=2)
        for serie in ("A", "B", "C"):
            self.equipamento(monitor, serie)
        cabo = self.modelo_material(estoque_minimo=80)
        est.registrar_entrada_material(cabo, self.deposito, 100, self.usuario)
        est.registrar_entrada_material(cabo, self.oficina, 30, self.usuario)
        est.registrar_saida_material(cabo, self.deposito, 60, self.usuario, "obra")
        resumo = est.resumo_estoque()
        self.assertEqual((resumo["total"], resumo["por_situacao"]["disponivel"], resumo["materiais"]), (3, 3, 1))
        material = est.listar_materiais().get(pk=cabo.pk)
        self.assertEqual((material.total, material.n_locais), (D("70"), 2))
        self.assertEqual([m.pk for m in est.listar_materiais({"abaixo_minimo": "1"})], [cabo.pk])           # 70 disponíveis < mínimo 80
        self.assertTrue(est.conferir_consistencia()["ok"])

    def test_filtro_por_local_nao_multiplica_o_total(self):
        cabo = self.modelo_material()
        est.registrar_entrada_material(cabo, self.deposito, 10, self.usuario)
        est.registrar_entrada_material(cabo, self.oficina, 5, self.usuario)
        filtrado = est.listar_materiais({"local": self.deposito.pk}).get(pk=cabo.pk)
        self.assertEqual(filtrado.total, D("15"))          # o total do modelo, não só o do local, e SEM duplicar por join
        vazio = cat.salvar_local({"nome": "Sem saldo", "fazenda": self.karitel}, self.usuario)
        self.assertEqual(est.listar_materiais({"local": vazio.pk}).count(), 0)


class EstoqueMinimoTests(TecBase):
    def test_abaixo_do_minimo_para_individual_e_quantidade(self):
        monitor = self.modelo_individual("G5", estoque_minimo=2)
        self.equipamento(monitor, "A")
        cabo = self.modelo_material(estoque_minimo=100)
        est.registrar_entrada_material(cabo, self.deposito, 40, self.usuario)
        suficiente = self.modelo_material("Cabo cheio", estoque_minimo=5)
        est.registrar_entrada_material(suficiente, self.deposito, 9, self.usuario)
        falta = {linha["modelo"].pk: linha for linha in est.abaixo_do_minimo()}
        self.assertEqual(set(falta), {monitor.pk, cabo.pk})
        self.assertEqual(falta[cabo.pk]["falta"], D("60"))
        self.assertEqual(falta[monitor.pk]["disponivel"], D("1"))
        self.assertEqual(est.abaixo_do_minimo()[0]["modelo"].pk, cabo.pk)           # os mais faltantes primeiro
        self.assertEqual(est.resumo_estoque()["abaixo_do_minimo"], 2)

    def test_equipamento_em_manutencao_ou_inativo_nao_conta_como_disponivel(self):
        monitor = self.modelo_individual("G5", estoque_minimo=2)
        a, b = self.equipamento(monitor, "A"), self.equipamento(monitor, "B")
        self.assertEqual(est.abaixo_do_minimo(), [])
        est.inativar_equipamento(b, self.usuario, "baixa")
        self.assertEqual(est.abaixo_do_minimo()[0]["disponivel"], D("1"))


# ═════════════════════════════════════════════════════════════
# 5. O razão é a verdade; o cache é conferido contra ele (cenário 8, parte 1)
# ═════════════════════════════════════════════════════════════

class ConsistenciaTests(TecBase):
    def test_razao_uniforme_equipamento_conta_um_no_local(self):
        monitor = self.modelo_individual()
        a, b = self.equipamento(monitor, "A"), self.equipamento(monitor, "B")
        est.mover_equipamento_de_local(a, self.oficina, self.usuario, "x")
        est.inativar_equipamento(b, self.usuario, "x")
        razao = est.razao_por_modelo_local()
        self.assertEqual(razao[(monitor.pk, self.deposito.pk)], D("0"))
        self.assertEqual(razao[(monitor.pk, self.oficina.pk)], D("1"))
        self.assertTrue(est.conferir_consistencia()["ok"])

    def test_divergencia_do_saldo_e_apontada_sem_corrigir_e_reconciliar_alinha(self):
        cabo = self.modelo_material()
        est.registrar_entrada_material(cabo, self.deposito, 10, self.usuario)
        TecSaldo.objects.filter(modelo=cabo, local=self.deposito).update(quantidade=D("99"))       # cache corrompido
        resultado = est.conferir_consistencia()
        self.assertFalse(resultado["ok"])
        self.assertEqual((resultado["materiais"][0]["cache"], resultado["materiais"][0]["razao"]), (D("99"), D("10")))
        self.assertEqual(TecSaldo.objects.get(modelo=cabo, local=self.deposito).quantidade, D("99"))   # leitura não escreve
        self.assertEqual(est.reconciliar(), 1)
        self.assertEqual(TecSaldo.objects.get(modelo=cabo, local=self.deposito).quantidade, D("10"))
        self.assertTrue(est.conferir_consistencia()["ok"])
        self.assertEqual(est.reconciliar(), 0)

    def test_equipamento_fora_do_lugar_aparece_como_divergencia_e_nao_se_reconcilia(self):
        eq = self.equipamento()
        TecEquipamento.objects.filter(pk=eq.pk).update(local=self.oficina)                         # mudou por fora do service
        resultado = est.conferir_consistencia()
        self.assertFalse(resultado["ok"])
        self.assertEqual(len(resultado["equipamentos"]), 2)               # falta num local, sobra no outro
        est.reconciliar()
        self.assertFalse(est.conferir_consistencia()["ok"])

    def test_saldo_apagado_do_cache_e_recriado_pela_reconciliacao(self):
        cabo = self.modelo_material()
        est.registrar_entrada_material(cabo, self.deposito, 4, self.usuario)
        TecSaldo.objects.filter(modelo=cabo).delete()
        self.assertFalse(est.conferir_consistencia()["ok"])
        est.reconciliar()
        self.assertEqual(TecSaldo.objects.get(modelo=cabo, local=self.deposito).quantidade, D("4"))


# ═════════════════════════════════════════════════════════════
# 6. Concorrência simulada e travas do banco (cenário 8, parte 2)
# ═════════════════════════════════════════════════════════════

class ConcorrenciaETravasTests(TecBase):
    def test_transicao_condicional_recusa_quando_outra_pessoa_chegou_antes(self):
        eq = self.equipamento()
        velho = TecEquipamento.objects.get(pk=eq.pk)                    # a tela de outra pessoa, aberta antes
        est.inativar_equipamento(eq, self.usuario, "primeiro")
        with self.assertRaises(ValidationError) as ctx:
            with transaction.atomic():
                est.transicao(velho, Situacao.DISPONIVEL, Situacao.INSTALADO, local=None, usuario=self.usuario)
        self.assertIn("inativo", str(ctx.exception))                    # a mensagem traz a situação ATUAL
        self.assertEqual(TecEquipamento.objects.get(pk=eq.pk).situacao, "inativo")

    def test_transicao_com_local_atual_recusa_se_ele_mudou_de_local(self):
        eq = self.equipamento()
        velho = TecEquipamento.objects.get(pk=eq.pk)
        est.mover_equipamento_de_local(eq, self.oficina, self.usuario, "moveu")
        with self.assertRaises(ValidationError):
            with transaction.atomic():
                est.transicao(velho, Situacao.DISPONIVEL, Situacao.DISPONIVEL, local=self.deposito, usuario=self.usuario, local_atual=self.deposito)

    def test_debito_condicional_nao_depende_de_leitura_previa(self):
        cabo = self.modelo_material()
        est.registrar_entrada_material(cabo, self.deposito, 10, self.usuario)
        # duas "telas" viram saldo 10; a primeira tira 8, a segunda também tenta tirar 8
        est.registrar_saida_material(cabo, self.deposito, 8, self.usuario, "primeira")
        with self.assertRaises(ValidationError):
            est.registrar_saida_material(cabo, self.deposito, 8, self.usuario, "segunda")
        self.assertEqual(TecSaldo.objects.get(modelo=cabo, local=self.deposito).quantidade, D("2"))

    def test_operacao_que_falha_no_meio_nao_deixa_rastro(self):
        cabo = self.modelo_material()
        est.registrar_entrada_material(cabo, self.deposito, 5, self.usuario)
        movimentos = TecMovimentacao.objects.count()
        with self.assertRaises(ValidationError):
            est.transferir_material_entre_locais(cabo, self.deposito, self.oficina, 6, self.usuario, "x")
        self.assertEqual(TecMovimentacao.objects.count(), movimentos)
        self.assertFalse(TecSaldo.objects.filter(modelo=cabo, local=self.oficina).exists())

    def test_banco_recusa_saldo_negativo_mesmo_por_fora_do_service(self):
        cabo = self.modelo_material()
        est.registrar_entrada_material(cabo, self.deposito, 1, self.usuario)
        with self.assertRaises(IntegrityError), transaction.atomic():
            TecSaldo.objects.filter(modelo=cabo).update(quantidade=D("-1"))

    def test_banco_recusa_disponivel_sem_local_e_instalado_com_local(self):
        eq = self.equipamento()
        with self.assertRaises(IntegrityError), transaction.atomic():
            TecEquipamento.objects.filter(pk=eq.pk).update(local=None)                       # disponível sem lugar
        with self.assertRaises(IntegrityError), transaction.atomic():
            TecEquipamento.objects.filter(pk=eq.pk).update(situacao="instalado")             # instalado ainda com local de guarda

    def test_banco_recusa_serie_duplicada_no_mesmo_modelo(self):
        modelo = self.modelo_individual()
        self.equipamento(modelo, "REP")
        with self.assertRaises(IntegrityError), transaction.atomic():
            TecEquipamento.objects.create(modelo=modelo, numero_serie="REP", situacao="disponivel", local=self.deposito)

    def test_banco_recusa_modelo_por_quantidade_sem_unidade(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            TecModelo.objects.create(fabricante=self.motorola, categoria=self.cat_cabo, nome="Sem unidade", controle="quantidade")

    def test_banco_recusa_saldo_duplicado_por_modelo_e_local(self):
        cabo = self.modelo_material()
        TecSaldo.objects.create(modelo=cabo, local=self.deposito, quantidade=D("1"))
        with self.assertRaises(IntegrityError), transaction.atomic():
            TecSaldo.objects.create(modelo=cabo, local=self.deposito, quantidade=D("1"))


class HistoricoImutavelTests(TecBase):
    def test_razao_nao_se_edita_nem_se_apaga(self):
        eq = self.equipamento()
        mov = TecMovimentacao.objects.get(equipamento=eq)
        mov.motivo = "adulterado"
        with self.assertRaises(ValidationError):
            mov.save()
        with self.assertRaises(ValidationError):
            mov.delete()
        with self.assertRaises(ValidationError):
            TecMovimentacao.objects.filter(pk=mov.pk).update(motivo="x")
        with self.assertRaises(ValidationError):
            TecMovimentacao.objects.filter(pk=mov.pk).delete()
        with self.assertRaises(ValidationError):
            TecMovimentacao.objects.bulk_update([mov], ["motivo"])
        self.assertEqual(TecMovimentacao.objects.get(pk=mov.pk).motivo, "")

    def test_evento_nao_se_edita_nem_se_apaga(self):
        evento = TecEvento.objects.first()
        evento.dados = {"x": 1}
        with self.assertRaises(ValidationError):
            evento.save()
        with self.assertRaises(ValidationError):
            evento.delete()
        with self.assertRaises(ValidationError):
            TecEvento.objects.all().delete()

    def test_equipamento_com_historico_nao_e_apagado(self):
        eq = self.equipamento()
        with self.assertRaises(Exception):                      # PROTECT: o razão aponta para ele
            with transaction.atomic():
                eq.delete()
        self.assertTrue(TecEquipamento.objects.filter(pk=eq.pk).exists())


class FiltrosDoRazaoTests(TecBase):
    def test_historico_filtra_por_tipo_modelo_ponto_periodo_e_busca(self):
        eq = self.equipamento(serie="BUSCAVEL")
        est.mover_equipamento_de_local(eq, self.oficina, self.usuario, "oficina")
        cabo = self.modelo_material()
        est.registrar_entrada_material(cabo, self.deposito, 5, self.usuario, motivo="compra de cabo")
        todos = est.historico_movimentacoes()
        self.assertEqual(todos.count(), 3)
        self.assertEqual(est.historico_movimentacoes({"tipo": "transferencia"}).count(), 1)
        self.assertEqual(est.historico_movimentacoes({"modelo": cabo.pk}).count(), 1)
        self.assertEqual(est.historico_movimentacoes({"equipamento": eq.pk}).count(), 2)
        self.assertEqual(est.historico_movimentacoes({"q": "buscavel"}).count(), 2)
        self.assertEqual(est.historico_movimentacoes({"q": "compra de cabo"}).count(), 1)
        self.assertEqual(est.historico_movimentacoes({"ponto": "estoque", "ponto_id": self.oficina.pk}).count(), 1)
        self.assertEqual(est.historico_movimentacoes({"usuario": "TEC_OPERADOR"}).count(), 3)
        self.assertEqual(est.historico_movimentacoes({"de": "2099-01-01"}).count(), 0)
        self.assertEqual(est.historico_movimentacoes({"de": "lixo"}).count(), 0)            # data inválida: vazio, não 500
        self.assertEqual(est.historico_movimentacoes({"equipamento": "x"}).count(), 0)
        self.assertEqual(est.historico_movimentacoes({"tipo": "inventado"}).count(), 3)     # tipo desconhecido é ignorado


class ComandoDeAuditoriaTests(TecBase):
    def rodar(self, *args):
        from io import StringIO

        from django.core.management import call_command

        saida = StringIO()
        try:
            call_command("auditar_tec_estoque", *args, stdout=saida)
            return 0, saida.getvalue()
        except SystemExit as exc:
            return exc.code, saida.getvalue()

    def test_consistente_sai_com_zero(self):
        cabo = self.modelo_material()
        est.registrar_entrada_material(cabo, self.deposito, 10, self.usuario)
        self.equipamento()
        codigo, texto = self.rodar()
        self.assertEqual(codigo, 0)
        self.assertIn("consistente", texto)

    def test_divergencia_sai_com_um_e_reconciliar_corrige_o_material(self):
        cabo = self.modelo_material()
        est.registrar_entrada_material(cabo, self.deposito, 10, self.usuario)
        TecSaldo.objects.filter(modelo=cabo).update(quantidade=D("3"))
        codigo, texto = self.rodar()
        self.assertEqual(codigo, 1)
        self.assertIn("MATERIAL", texto)
        self.assertEqual(TecSaldo.objects.get(modelo=cabo).quantidade, D("3"))            # só lê
        codigo, _ = self.rodar("--reconciliar")
        self.assertEqual(codigo, 0)
        self.assertEqual(TecSaldo.objects.get(modelo=cabo).quantidade, D("10"))

    def test_equipamento_fora_do_lugar_nao_se_reconcilia(self):
        eq = self.equipamento()
        TecEquipamento.objects.filter(pk=eq.pk).update(local=self.oficina)
        codigo, texto = self.rodar("--reconciliar")
        self.assertEqual(codigo, 1)
        self.assertIn("nao se reconciliam", texto)
