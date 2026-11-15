"""
Módulo de Contratações (admissão / onboarding de TI) e o aviso de
computadores parados.

O que cada bloco protege — todos são falhas que passam calado:

  • CATÁLOGO DINÂMICO — as perguntas de sistema nascem de `SistemaCorporativo`
    e são injetadas no form em `__init__`. Se a injeção quebrar, o formulário
    continua abrindo e salvando: só que SEM nenhuma pergunta de acesso, e a
    contratação nasce pela metade sem erro nenhum.

  • PEDIDO ≠ PROVISIONADO — desmarcar um sistema já provisionado não pode
    apagar a linha, senão a data em que o acesso foi criado se perde.

  • CONCLUIR COM PENDÊNCIA — "concluída" tira a vaga do quadro. Fechar com
    acesso pendente esconde o pendente, e ele vira o chamado de segunda.

  • AVISO DE COMPUTADOR PARADO — o alerta tem de disparar nos DOIS sentidos
    (entrar em backup → cancelar licença; voltar a ativo → religar) e NÃO
    pode disparar para switch/toner, senão vira ruído e deixa de ser lido.
"""

import codecs
import datetime
import re
from unittest.mock import patch

from django.contrib.auth.models import Permission, User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from services import contratacao_service as cs

from ProjetoEstoque.models import (
    CentroCusto,
    Contratacao,
    ContratacaoEvento,
    ContratacaoLicenca,
    ContratacaoSistema,
    Categoria,
    Item,
    ItemStatusHistorico,
    Licenca,
    LicencaOffice,
    SimNaoChoices,
    SistemaCorporativo,
    StatusContratacaoChoices,
    StatusItemChoices,
    Subtipo,
    Usuario,
)


def _subtipo(nome):
    """`Subtipo.categoria` e NOT NULL no banco -- criar sem ela estoura a
    constraint, nao a validacao do form."""
    categoria, _ = Categoria.objects.get_or_create(nome="Equipamento")
    return Subtipo.objects.create(
        nome=nome, alocado=SimNaoChoices.SIM, categoria=categoria,
    )


class BaseContratacaoTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("ti", password="x", is_staff=True)
        self.client.force_login(self.user)

        self.cc = CentroCusto.objects.create(numero="100", departamento="TI")

        # `get_or_create`, e nao `create`: a migration 0180 ja semeia estes
        # dois no banco de teste. Criar de novo estoura o unique de `nome` --
        # e foi assim que a primeira versao destes testes quebrou.
        self.datasul, _ = SistemaCorporativo.objects.get_or_create(
            nome="TOTVS - Datasul", defaults={"ordem": 10, "exige_perfil_espelho": True},
        )
        self.tekann, _ = SistemaCorporativo.objects.get_or_create(
            nome="Tekann", defaults={"ordem": 50},
        )
        self.tekann.exige_perfil_espelho = False
        self.tekann.save(update_fields=["exige_perfil_espelho"])

        self.licenca = Licenca.objects.create(nome="Microsoft 365 Business Standard")
        self.espelho_usuario = Usuario.objects.create(
            nome="Rodrigo Tadeu", status="ativo", pmb=SimNaoChoices.NAO,
        )

    def _payload(self, **extra):
        dados = {
            "cargo": "Técnico de Segurança do Trabalho",
            "tipo_vaga": "nova",
            "computador": "notebook",
            "computador_justificativa": "Substituição",
            "telefonia": "ramal",
            "ramal_sugerido": "2045",
            "centro_custo": self.cc.pk,
            "email_corporativo": "on",
            "usuario_rede": "on",
            f"sistema_{self.datasul.pk}": "on",
            f"sistema_{self.datasul.pk}_espelho": "rodrigo.tadeu",
        }
        dados.update(extra)
        return dados


class FormularioDinamicoTests(BaseContratacaoTests):
    def test_cada_sistema_ativo_vira_uma_pergunta_no_formulario(self):
        resp = self.client.get(reverse("contratacao_create"))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("TOTVS - Datasul", html)
        self.assertIn("Tekann", html)
        self.assertIn(f'name="sistema_{self.datasul.pk}"', html)

    def test_sistema_sem_espelho_nao_ganha_campo_de_espelho(self):
        """`exige_perfil_espelho=False` é o que diferencia um acesso que se
        copia de outro usuário de um que não tem esse conceito."""
        resp = self.client.get(reverse("contratacao_create"))
        html = resp.content.decode()
        self.assertIn(f'name="sistema_{self.datasul.pk}_espelho"', html)
        self.assertNotIn(f'name="sistema_{self.tekann.pk}_espelho"', html)

    def test_sistema_inativo_sai_do_formulario_das_proximas(self):
        self.tekann.ativo = False
        self.tekann.save(update_fields=["ativo"])
        resp = self.client.get(reverse("contratacao_create"))
        self.assertNotIn(f'name="sistema_{self.tekann.pk}"', resp.content.decode())

    def test_abrir_contratacao_grava_sistemas_marcados_com_espelho(self):
        resp = self.client.post(reverse("contratacao_create"), self._payload())
        self.assertEqual(resp.status_code, 302)

        obj = Contratacao.objects.get()
        linhas = {linha.sistema_id: linha for linha in obj.sistemas.all()}
        self.assertIn(self.datasul.pk, linhas)
        self.assertTrue(linhas[self.datasul.pk].necessario)
        self.assertEqual(linhas[self.datasul.pk].perfil_espelho, "rodrigo.tadeu")
        self.assertNotIn(self.tekann.pk, linhas, "sistema não marcado não deveria virar linha")

    def test_licencas_marcadas_viram_itens_de_provisionamento(self):
        self.client.post(
            reverse("contratacao_create"),
            self._payload(**{f"licenca_{self.licenca.pk}": "on"}),
        )
        obj = Contratacao.objects.get()
        self.assertEqual(
            list(obj.licencas_necessarias.values_list("licenca_id", flat=True)),
            [self.licenca.pk],
        )

    def test_licenca_marcada_grava_o_usuario_espelho(self):
        """Licença tem a MESMA linha do sistema (caixa + de quem copiar o
        perfil) e grava pelo mesmo caminho. Se o espelho da licença se perder
        no POST, o TI recebe o pedido do assento sem saber de qual perfil
        copiar — e o defeito não aparece em erro nenhum."""
        self.client.post(
            reverse("contratacao_create"),
            self._payload(**{
                f"licenca_{self.licenca.pk}": "on",
                f"licenca_{self.licenca.pk}_espelho": "Rodrigo Tadeu",
            }),
        )
        obj = Contratacao.objects.get()
        linha = obj.licencas_necessarias.get(licenca=self.licenca)
        self.assertEqual(linha.perfil_espelho, "Rodrigo Tadeu")

    def test_licenca_sem_espelho_fica_em_branco(self):
        self.client.post(
            reverse("contratacao_create"),
            self._payload(**{f"licenca_{self.licenca.pk}": "on"}),
        )
        obj = Contratacao.objects.get()
        linha = obj.licencas_necessarias.get(licenca=self.licenca)
        self.assertEqual(linha.perfil_espelho, "")

    def test_licenca_desmarcada_sai_da_contratacao(self):
        """O par do teste acima: a lista unificada é a única fonte do que foi
        pedido, então desmarcar tem de APAGAR a linha — enquanto o assento
        ainda não foi atribuído."""
        self.client.post(
            reverse("contratacao_create"),
            self._payload(**{f"licenca_{self.licenca.pk}": "on"}),
        )
        obj = Contratacao.objects.get()
        self.client.post(
            reverse("contratacao_update", args=[obj.pk]), self._payload(),
        )
        self.assertFalse(obj.licencas_necessarias.exists())

    def test_licenca_provisionada_sobrevive_ao_desmarcar(self):
        """Apagar a linha de um assento JÁ atribuído jogaria fora a data da
        atribuição — o único registro de que ele saiu do lote."""
        self.client.post(
            reverse("contratacao_create"),
            self._payload(**{f"licenca_{self.licenca.pk}": "on"}),
        )
        obj = Contratacao.objects.get()
        linha = obj.licencas_necessarias.get()
        linha.provisionada = True
        linha.save(update_fields=["provisionada"])

        self.client.post(
            reverse("contratacao_update", args=[obj.pk]), self._payload(),
        )
        self.assertTrue(obj.licencas_necessarias.filter(pk=linha.pk).exists())

    def test_vaga_de_substituicao_salva_sem_o_colaborador_substituido(self):
        """Exigir o substituido empurraria o RH a marcar "nova vaga" so para
        conseguir salvar -- e aí a contagem de aumento de quadro, que e o
        motivo de `tipo_vaga` existir, passaria a mentir."""
        resp = self.client.post(
            reverse("contratacao_create"),
            self._payload(tipo_vaga="substituicao", substituido=""),
        )
        self.assertEqual(resp.status_code, 302, "a vaga de substituicao foi recusada")
        obj = Contratacao.objects.get()
        self.assertEqual(obj.tipo_vaga, "substituicao")
        self.assertIsNone(obj.substituido_id)

    def test_telefonia_nenhuma_limpa_ramal_e_ligacao_direta(self):
        """Ramal sugerido guardado numa contratacao sem telefonia vira dado
        orfao que ninguem explica depois."""
        self.client.post(reverse("contratacao_create"), self._payload(
            telefonia="nenhum", ramal_sugerido="2045", ligacao_direta="on",
        ))
        obj = Contratacao.objects.get()
        self.assertEqual(obj.ramal_sugerido, "")
        self.assertFalse(obj.ligacao_direta)

    def test_abertura_registra_evento_na_linha_do_tempo(self):
        self.client.post(reverse("contratacao_create"), self._payload())
        obj = Contratacao.objects.get()
        self.assertTrue(obj.eventos.filter(tipo="criacao").exists())


class SincronizacaoDeSistemasTests(BaseContratacaoTests):
    def setUp(self):
        super().setUp()
        self.client.post(reverse("contratacao_create"), self._payload())
        self.obj = Contratacao.objects.get()

    def test_desmarcar_sistema_ja_provisionado_preserva_a_linha(self):
        """A data em que o acesso foi criado é o único registro de que ele
        existe — apagar a linha ao desmarcar jogaria isso fora."""
        linha = self.obj.sistemas.get(sistema=self.datasul)
        cs.alternar_sistema(linha=linha, user=self.user)

        self.client.post(
            reverse("contratacao_update", args=[self.obj.pk]),
            self._payload(**{f"sistema_{self.datasul.pk}": ""}),
        )

        linha.refresh_from_db()
        self.assertFalse(linha.necessario)
        self.assertTrue(linha.provisionado)
        self.assertIsNotNone(linha.provisionado_em)

    def test_desmarcar_sistema_nao_provisionado_apaga_a_linha(self):
        self.client.post(
            reverse("contratacao_update", args=[self.obj.pk]),
            self._payload(**{f"sistema_{self.datasul.pk}": ""}),
        )
        self.assertFalse(
            ContratacaoSistema.objects.filter(contratacao=self.obj, sistema=self.datasul).exists()
        )


class EtapasTests(BaseContratacaoTests):
    def setUp(self):
        super().setUp()
        self.obj = Contratacao.objects.create(
            cargo="Assistente Administrativo", computador="notebook", centro_custo=self.cc,
        )
        self.colaborador = Usuario.objects.create(
            nome="Maria Souza", status="ativo", pmb=SimNaoChoices.NAO,
        )
        self.item = Item.objects.create(
            nome="NB-0001", subtipo=_subtipo("Notebook"), status=StatusItemChoices.ESTOQUE,
        )

    def _entrega_de_fato(self):
        """A entrega só vale com a movimentação patrimonial: cria a linha que o
        serviço de movimentação criaria (a regra de estoque tem teste próprio)."""
        from ProjetoEstoque.models import MovimentacaoItem
        return MovimentacaoItem.objects.create(
            tipo_movimentacao="transferencia", tipo_transferencia="entrega",
            item=self.item, usuario=self.colaborador,
        )

    def test_preencher_vaga_avanca_etapa_e_grava_data(self):
        self.client.post(reverse("contratacao_etapa", args=[self.obj.pk]), {
            "acao": "preencher_vaga", "colaborador": self.colaborador.pk,
        })
        self.obj.refresh_from_db()
        self.assertEqual(self.obj.status, StatusContratacaoChoices.VAGA_PREENCHIDA)
        self.assertIsNotNone(self.obj.vaga_preenchida_em)
        self.assertEqual(self.obj.colaborador_id, self.colaborador.pk)

    def test_corrigir_contratado_nao_empurra_a_etapa_para_tras(self):
        cs.definir_contratado(
            contratacao=self.obj, colaborador=self.colaborador, nome="", user=self.user,
        )
        self._entrega_de_fato()
        cs.registrar_entrega(
            contratacao=self.obj, item=self.item, sem_equipamento=False, user=self.user,
        )
        cs.definir_contratado(
            contratacao=self.obj, colaborador=self.colaborador, nome="Maria S.", user=self.user,
        )
        self.obj.refresh_from_db()
        self.assertEqual(self.obj.status, StatusContratacaoChoices.EQUIPAMENTO_ENTREGUE)

    def test_registrar_entrega_liga_a_contratacao_ao_equipamento(self):
        cs.definir_contratado(
            contratacao=self.obj, colaborador=self.colaborador, nome="", user=self.user,
        )
        self._entrega_de_fato()
        self.client.post(reverse("contratacao_etapa", args=[self.obj.pk]), {
            "acao": "registrar_entrega", "item": self.item.pk,
        })
        self.obj.refresh_from_db()
        self.assertEqual(self.obj.item_entregue_id, self.item.pk)
        self.assertEqual(self.obj.status, StatusContratacaoChoices.EQUIPAMENTO_ENTREGUE)

    def test_entrega_nao_movimenta_estoque(self):
        """O inventário muda de mãos em Movimentações. Se esta tela também
        mexesse no item, passariam a existir duas verdades sobre a máquina."""
        from ProjetoEstoque.models import MovimentacaoItem

        cs.definir_contratado(
            contratacao=self.obj, colaborador=self.colaborador, nome="", user=self.user,
        )
        self._entrega_de_fato()
        antes = MovimentacaoItem.objects.filter(item=self.item).count()
        cs.registrar_entrega(
            contratacao=self.obj, item=self.item, sem_equipamento=False, user=self.user,
        )
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, StatusItemChoices.ESTOQUE)
        self.assertEqual(MovimentacaoItem.objects.filter(item=self.item).count(), antes)

    def test_concluir_com_pendencia_e_recusado(self):
        ContratacaoSistema.objects.create(
            contratacao=self.obj, sistema=self.datasul, necessario=True,
        )
        ok, faltas = cs.concluir(contratacao=self.obj, user=self.user)
        self.obj.refresh_from_db()
        self.assertFalse(ok)
        self.assertTrue(faltas)
        self.assertNotEqual(self.obj.status, StatusContratacaoChoices.CONCLUIDA)

    def test_nao_existe_concluir_mesmo_assim(self):
        """A saída para o que não se aplica é DISPENSAR a etapa, com justificativa."""
        import inspect
        self.assertNotIn("forcar", inspect.signature(cs.concluir).parameters)

    def test_concluir_sem_pendencia_fecha_direto(self):
        self.obj.computador = "nenhum"
        self.obj.usuario_rede = False
        self.obj.email_corporativo = False
        self.obj.colaborador = self.colaborador
        self.obj.status = StatusContratacaoChoices.VAGA_PREENCHIDA
        self.obj.save()
        cs.confirmar_integracao(contratacao_pk=self.obj.pk, user=self.user)
        ok, faltas = cs.concluir(contratacao=self.obj, user=self.user)
        self.assertTrue(ok)
        self.assertEqual(faltas, [])

    def test_reabrir_volta_para_a_etapa_que_os_fatos_sustentam(self):
        cs.definir_contratado(
            contratacao=self.obj, colaborador=self.colaborador, nome="", user=self.user,
        )
        self._entrega_de_fato()
        cs.registrar_entrega(
            contratacao=self.obj, item=self.item, sem_equipamento=False, user=self.user,
        )
        Contratacao.objects.filter(pk=self.obj.pk).update(
            status=StatusContratacaoChoices.CONCLUIDA, concluida_em=timezone.now(),
        )
        self.obj.refresh_from_db()
        cs.reabrir(contratacao=self.obj, user=self.user)
        self.obj.refresh_from_db()
        self.assertEqual(self.obj.status, StatusContratacaoChoices.EQUIPAMENTO_ENTREGUE)
        self.assertIsNone(self.obj.concluida_em)

    def test_cancelar_guarda_o_motivo(self):
        self.client.post(reverse("contratacao_etapa", args=[self.obj.pk]), {
            "acao": "cancelar", "motivo": "Vaga congelada pela diretoria",
        })
        self.obj.refresh_from_db()
        self.assertEqual(self.obj.status, StatusContratacaoChoices.CANCELADA)
        self.assertIn("congelada", self.obj.motivo_cancelamento)


class PainelTests(BaseContratacaoTests):
    def test_atraso_so_existe_quando_ha_data_prevista(self):
        """Contratação sem prazo não pode ser cobrada por um prazo inventado."""
        ontem = datetime.date.today() - datetime.timedelta(days=3)
        atrasada = Contratacao.objects.create(cargo="Atrasada", data_prevista_inicio=ontem)
        sem_data = Contratacao.objects.create(cargo="Sem prazo")

        self.assertTrue(atrasada.atrasada)
        self.assertFalse(sem_data.atrasada)

    def test_contratacao_concluida_sai_da_contagem_de_atrasadas(self):
        ontem = datetime.date.today() - datetime.timedelta(days=3)
        obj = Contratacao.objects.create(
            cargo="Fechada", data_prevista_inicio=ontem, computador="nenhum",
            colaborador_nome="Alguém",
            status=StatusContratacaoChoices.CONCLUIDA, concluida_em=timezone.now(),
        )
        obj.refresh_from_db()
        self.assertFalse(obj.atrasada)
        self.assertEqual(cs.painel()["kpis"]["atrasadas"], 0)

    def test_cancelada_nao_aparece_em_coluna_nenhuma(self):
        obj = Contratacao.objects.create(cargo="Cancelada")
        cs.cancelar(contratacao=obj, motivo="desistência", user=self.user)
        painel = cs.painel()
        ids = [c.pk for coluna in painel["colunas"] for c in coluna["cards"]]
        self.assertNotIn(obj.pk, ids)

    def test_coluna_de_concluidas_mostra_so_o_mes_corrente(self):
        """Sem o corte, a quarta coluna cresce para sempre e deixa de ser
        legível justamente onde se quer ver o que fechou agora."""
        antiga = Contratacao.objects.create(cargo="Antiga", computador="nenhum")
        Contratacao.objects.filter(pk=antiga.pk).update(
            status=StatusContratacaoChoices.CONCLUIDA,
            concluida_em=timezone.now() - datetime.timedelta(days=75),
        )

        coluna = [
            c for c in cs.painel()["colunas"]
            if c["chave"] == StatusContratacaoChoices.CONCLUIDA
        ][0]
        self.assertNotIn(antiga.pk, [c.pk for c in coluna["cards"]])

    def test_painel_responde_200_e_lista_as_quatro_etapas(self):
        Contratacao.objects.create(cargo="Pesquisador de Projetos — Cacau")
        resp = self.client.get(reverse("contratacao_painel"))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        for rotulo in ("Em aberto", "Vaga preenchida", "Equipamento entregue", "Concluída"):
            self.assertIn(rotulo, html)
        self.assertIn("Pesquisador de Projetos", html)

    def test_placar_conta_o_equipamento_junto_com_os_acessos(self):
        obj = Contratacao.objects.create(cargo="Com máquina", computador="notebook")
        ContratacaoSistema.objects.create(
            contratacao=obj, sistema=self.datasul, necessario=True,
        )
        placar = cs.resumo_provisionamento(obj)
        self.assertEqual(placar["total"], 2)
        self.assertEqual(placar["feitos"], 0)


class PainelMinimalistaTests(BaseContratacaoTests):
    """O painel de 15/11/2026: tela limpa. O que estes testes seguram é o que o pedido tirou da tela E o que NÃO podia sair
    (o que pede ação continua lá), porque tirar demais também passa calado: a tela abre 200 e a informação some."""

    def painel(self, **filtros):
        return self.client.get(reverse("contratacao_painel"), filtros).content.decode()

    @staticmethod
    def cartao_de(html, cargo):
        """O HTML do cartão da vaga (`<a class="ctr-vaga ...">...</a>`) que tem o cargo."""
        for bloco in re.findall(r'<a class="ctr-vaga[^"]*"\s+href="[^"]*">.*?</a>', html, flags=re.S):
            if cargo in bloco:
                return bloco
        raise AssertionError(f"cartão de {cargo!r} não encontrado no painel")

    def test_os_numeros_ficam_numa_faixa_so_sem_o_cartao_heroi_nem_os_quatro_kpis(self):
        html = self.painel()
        self.assertEqual(html.count('class="ctr-numeros"'), 1)
        for rotulo in ("Em aberto", "Sem equipamento", "Atrasadas", "Concluídas no mês"):
            self.assertRegex(html, rf"<dt>{rotulo}</dt>")
        for sobra in ("ctr-kpi", 'class="ctr-hero', "Mês de referência", "vagas ainda sem contratado</span>"):
            self.assertNotIn(sobra, html)

    def test_cabecalho_sem_selo_repetido_e_sem_subtitulo_duplicado_na_topbar(self):
        html = self.painel()
        self.assertNotIn("Pessoas &amp; TI", html)
        self.assertNotIn("page-kicker", html)
        self.assertIn('<span class="topbar-subtitle"></span>', html)
        self.assertIn("Do pedido do gestor ao primeiro dia com tudo funcionando.", html)
        # as duas ações da página continuam no CORPO (regra 16), sem o tamanho "grande"
        self.assertIn(reverse("contratacao_create"), html)
        self.assertIn(reverse("chamados_lista"), html)
        self.assertNotIn("is-grande", html)

    def test_zero_fica_apagado_e_so_o_que_ha_ganha_cor(self):
        html = self.painel()
        self.assertEqual(html.count("ctr-numeros-item is-zero"), 4)
        self.assertNotIn("is-alerta", html)
        ontem = datetime.date.today() - datetime.timedelta(days=3)
        Contratacao.objects.create(cargo="Atrasada", data_prevista_inicio=ontem)
        Contratacao.objects.create(
            cargo="Fechada", computador="nenhum", colaborador_nome="Alguém",
            status=StatusContratacaoChoices.CONCLUIDA, concluida_em=timezone.now(),
        )
        html = self.painel()
        self.assertIn("ctr-numeros-item is-alerta", html)
        self.assertIn("ctr-numeros-item is-ok", html)
        # os que ainda são zero seguem apagados: a conta vem dos números do service, não de um palpite
        kpis = cs.painel()["kpis"]
        zeros = sum(1 for n in (kpis["abertas"], kpis["sem_equipamento"], kpis["atrasadas"], kpis["concluidas_mes"]) if not n)
        self.assertEqual(html.count("ctr-numeros-item is-zero"), zeros)

    def test_percentual_do_mes_so_aparece_quando_ha_concluidas(self):
        self.assertNotIn("<small>", re.search(r'<dl class="ctr-numeros".*?</dl>', self.painel(), re.S).group(0))
        Contratacao.objects.create(
            cargo="Fechada", computador="nenhum", colaborador_nome="Alguém",
            status=StatusContratacaoChoices.CONCLUIDA, concluida_em=timezone.now(),
        )
        faixa = re.search(r'<dl class="ctr-numeros".*?</dl>', self.painel(), re.S).group(0)
        self.assertRegex(faixa, r"<small>\d+%</small>")
        self.assertIn("Concluídas desde", faixa)                                # a nota longa virou `title`

    def test_filtros_sem_rotulo_a_mostra_e_com_os_mesmos_nomes_de_campo(self):
        html = self.painel()
        self.assertIn('class="ctr-filtros is-leve"', html)
        for campo, nome in (("ctrBusca", "q"), ("ctrCc", "centro_custo"), ("ctrLoc", "localidade")):
            self.assertIn(f'<label class="ctr-sr-only" for="{campo}">', html)
            self.assertIn(f'id="{campo}" name="{nome}"', html)
        self.assertIn('placeholder="Buscar cargo, contratado ou gestor"', html)
        self.assertIn('<option value="">Centro de custo</option>', html)
        self.assertIn('<option value="">Localidade</option>', html)
        self.assertNotIn("ctr-filtro-label", html)

    def test_limpar_so_aparece_com_filtro_ativo(self):
        limpar = f'href="{reverse("contratacao_painel")}" class="ctr-btn">Limpar</a>'
        self.assertNotIn(limpar, self.painel())
        self.assertIn(limpar, self.painel(q="abc"))
        self.assertIn(limpar, self.painel(centro_custo=str(self.cc.pk)))

    def test_atalhos_de_configuracao_continuam_so_para_quem_gerencia(self):
        html = self.painel()
        self.assertIn(reverse("sistema_corporativo_list"), html)
        self.assertIn(reverse("acesso_restrito_gerenciar"), html)

    def test_o_cartao_da_vaga_fica_so_com_o_essencial(self):
        ontem = datetime.date.today() - datetime.timedelta(days=2)
        centro = CentroCusto.objects.create(numero="777", departamento="Departamento Unico Zeta")
        vaga = Contratacao.objects.create(
            cargo="Analista Exclusivo", colaborador_nome="Fulano de Tal",
            computador="notebook", centro_custo=centro, data_prevista_inicio=ontem,
        )
        html = self.painel()
        self.assertIn("Departamento Unico Zeta", html)                          # existe na tela (a lista do filtro)...
        cartao = self.cartao_de(html, "Analista Exclusivo")
        self.assertIn("Fulano de Tal", cartao)
        self.assertIn(ontem.strftime("%d/%m"), cartao)                          # a data prevista
        self.assertIn("ctr-chip is-perigo", cartao)                             # atrasada continua gritando
        self.assertIn("ctr-vaga-placar", cartao)                                # o placar feitos/total
        # ...mas NÃO no cartão: saíram o centro de custo, o tipo de computador e a lista de pendências (moram no detalhe)
        self.assertNotIn("Departamento Unico Zeta", cartao)
        self.assertNotIn(vaga.get_computador_display(), cartao)
        self.assertNotIn("ctr-vaga-pend", cartao)
        self.assertNotIn("pendência", cartao)

    def test_alertas_que_pedem_acao_continuam_no_cartao(self):
        Contratacao.objects.create(cargo="Sem máquina", computador="notebook")
        cartao = self.cartao_de(self.painel(), "Sem máquina")
        self.assertIn("Sem equipamento reservado", cartao)

    def test_as_etapas_do_quadro_seguem_com_titulo_e_contador(self):
        Contratacao.objects.create(cargo="Qualquer")
        html = self.painel()
        for etapa in ("Em aberto", "Vaga preenchida", "Equipamento entregue", "Concluída"):
            self.assertRegex(html, rf'{etapa}\s*<span class="ctr-coluna-contador">\(\d+\)</span>')
        self.assertIn("ctr-add-card", html)                                     # o "+ Nova contratação" da primeira coluna fica
        self.assertNotIn("ctr-coluna-desc", html)                               # a descrição da etapa virou `title`


class TelasRenderizamTests(BaseContratacaoTests):
    """Template quebrado responde 500 -- e include com caminho errado
    renderiza VAZIO sem erro nenhum (regra dos templates fatiados). Estes
    testes abrem cada tela do modulo uma vez, com dado dentro."""

    def setUp(self):
        super().setUp()
        self.client.post(reverse("contratacao_create"), self._payload(**{
            f"licenca_{self.licenca.pk}": "on",
            f"licenca_{self.licenca.pk}_espelho": "Rodrigo Tadeu",
        }))
        self.obj = Contratacao.objects.get()

    def test_detalhe_abre_com_checklist_licenca_e_linha_do_tempo(self):
        resp = self.client.get(reverse("contratacao_detail", args=[self.obj.pk]))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("TOTVS - Datasul", html)
        self.assertIn("rodrigo.tadeu", html)
        self.assertIn("Microsoft 365 Business Standard", html)
        self.assertIn("Linha do tempo", html)
        self.assertIn("Equipamento", html)

    def test_edicao_reabre_com_o_que_foi_marcado(self):
        resp = self.client.get(reverse("contratacao_update", args=[self.obj.pk]))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('value="rodrigo.tadeu"', html)
        self.assertIn("Salvar", html)

        # O sistema marcado tem de voltar MARCADO: reabrir a edicao com a
        # caixa limpa faria `_sincronizar_sistemas` apagar o pedido no
        # proximo salvamento, sem ninguem ter desmarcado nada.
        caixa = re.search(
            rf'<input[^>]*name="sistema_{self.datasul.pk}"[^>]*>', html,
        )
        self.assertIsNotNone(caixa, "a pergunta do sistema sumiu da edicao")
        self.assertIn("checked", caixa.group(0))

    def test_edicao_reabre_com_a_licenca_marcada_e_o_espelho(self):
        """Reabrir a edição com a caixa limpa faria o próximo salvamento
        apagar o pedido do assento sem ninguém ter desmarcado nada — o mesmo
        defeito que o teste do sistema acima protege."""
        resp = self.client.get(reverse("contratacao_update", args=[self.obj.pk]))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()

        caixa = re.search(
            rf'<input[^>]*name="licenca_{self.licenca.pk}"[^>]*>', html,
        )
        self.assertIsNotNone(caixa, "a licenca sumiu da lista de acessos")
        self.assertIn("checked", caixa.group(0))

        espelho = re.search(
            rf'<input[^>]*name="licenca_{self.licenca.pk}_espelho"[^>]*>', html,
        )
        self.assertIsNotNone(espelho, "o campo de espelho da licenca sumiu")
        self.assertIn('value="Rodrigo Tadeu"', espelho.group(0))

    def test_lista_de_acessos_traz_sistema_e_licenca_juntos(self):
        """A união das duas listas é pedido do usuário: uma caixa por acesso,
        sistema e licença no mesmo lugar e com a mesma linha."""
        resp = self.client.get(reverse("contratacao_create"))
        html = resp.content.decode()
        self.assertIn(f'name="sistema_{self.datasul.pk}"', html)
        self.assertIn(f'name="licenca_{self.licenca.pk}"', html)
        self.assertIn(f'name="licenca_{self.licenca.pk}_espelho"', html)
        self.assertIn("Sistemas e licen", html)
        # Um <datalist> para todas as linhas — ver `_campo_de_espelho`.
        self.assertEqual(html.count('id="ctrUsuariosEspelho"'), 1)

    def test_nenhuma_tela_vaza_comentario_de_template(self):
        """`{# ... #}` só comenta UMA linha: quebrado em duas, o Django
        imprime o texto na tela. Responde 200, não acusa nada no console e
        só aparece para quem está olhando — aconteceu com o comentário da
        lista unificada, e este teste é o que impede a volta."""
        telas = [
            reverse("contratacao_painel"),
            reverse("contratacao_create"),
            reverse("contratacao_detail", args=[self.obj.pk]),
            reverse("contratacao_update", args=[self.obj.pk]),
            reverse("sistema_corporativo_list"),
            reverse("sistema_corporativo_create"),
        ]
        for url in telas:
            html = self.client.get(url).content.decode()
            self.assertNotIn("{#", html, f"comentario vazou em {url}")
            self.assertNotIn("{%", html, f"tag de template vazou em {url}")

    def test_confirmacao_de_exclusao_abre(self):
        self.user.user_permissions.add(
            Permission.objects.get(codename="delete_contratacao")
        )
        resp = self.client.get(reverse("contratacao_delete", args=[self.obj.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_formulario_de_sistema_abre_para_novo_e_para_edicao(self):
        self.assertEqual(
            self.client.get(reverse("sistema_corporativo_create")).status_code, 200
        )
        self.assertEqual(
            self.client.get(
                reverse("sistema_corporativo_update", args=[self.datasul.pk])
            ).status_code, 200,
        )

    def test_nenhuma_tela_do_modulo_tem_folha_inline(self):
        """O CSS do modulo mora em ds-contratacoes.css. `<style>` colado no
        template volta a viajar no HTML a cada page-load e para de ser
        cacheavel -- a regressao que o teste de assets ja trava no base."""
        rotas = [
            reverse("contratacao_painel"),
            reverse("contratacao_create"),
            reverse("contratacao_detail", args=[self.obj.pk]),
            reverse("sistema_corporativo_list"),
        ]
        for rota in rotas:
            html = self.client.get(rota).content.decode()
            self.assertNotIn("<style", html, rota)


class CadastroDeSistemasTests(BaseContratacaoTests):
    def test_sistema_em_uso_nao_pode_ser_excluido(self):
        obj = Contratacao.objects.create(cargo="Qualquer")
        ContratacaoSistema.objects.create(contratacao=obj, sistema=self.datasul, necessario=True)

        self.user.user_permissions.add(
            Permission.objects.get(codename="delete_sistemacorporativo")
        )
        resp = self.client.post(
            reverse("sistema_corporativo_delete", args=[self.datasul.pk]), follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(SistemaCorporativo.objects.filter(pk=self.datasul.pk).exists())

    def test_migration_semeia_os_seis_sistemas_do_formulario_antigo(self):
        """A primeira contratacao aberta no sistema novo tem de nascer com as
        mesmas perguntas do quadro do Planner -- tela vazia ninguem preenche."""
        nomes = set(SistemaCorporativo.objects.values_list("nome", flat=True))
        for esperado in ("TOTVS - Datasul", "TOTVS x PIMS MC", "TOTVS - Beneficiamento",
                         "TOTVS - Pesagem", "Tekann", "ID-Cotton"):
            self.assertIn(esperado, nomes)

    def test_lista_de_sistemas_abre(self):
        resp = self.client.get(reverse("sistema_corporativo_list"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("TOTVS - Datasul", resp.content.decode())


class MenuETemaTests(BaseContratacaoTests):
    def test_item_contratacoes_esta_no_submenu_pessoas(self):
        resp = self.client.get(reverse("contratacao_painel"))
        html = resp.content.decode()
        self.assertIn('href="/contratacoes/"', html)
        self.assertIn("Contratações", html)

    def test_telas_carregam_a_folha_do_modulo_com_cache_busting(self):
        from ProjetoEstoque.context_processors import ASSETS_VERSION

        for rota in ("contratacao_painel", "contratacao_create", "sistema_corporativo_list"):
            html = self.client.get(reverse(rota)).content.decode()
            self.assertIn("css/ds-contratacoes.css", html, rota)
            self.assertIn(ASSETS_VERSION, html, rota)


# ─────────────────────────────────────────────────────────────
# Aviso de computadores parados (backup / pausado)
# ─────────────────────────────────────────────────────────────

class ComputadoresParadosTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("ti2", password="x")
        self.categoria, _ = Categoria.objects.get_or_create(nome="Equipamento")
        self.notebook = _subtipo("Notebook")
        self.desktop = _subtipo("Desktop")
        self.switch = _subtipo("Switches")

    def _item(self, nome, subtipo, status=StatusItemChoices.ATIVO):
        return Item.objects.create(
            nome=nome, subtipo=subtipo, categoria=self.categoria, status=status,
        )

    def test_lista_traz_so_notebook_e_desktop_parados(self):
        from services.email_alertas import computadores_parados

        self._item("NB-PARADO", self.notebook, StatusItemChoices.BACKUP)
        self._item("DT-PARADO", self.desktop, StatusItemChoices.PAUSADO)
        self._item("NB-ATIVO", self.notebook, StatusItemChoices.ATIVO)
        self._item("SW-PARADO", self.switch, StatusItemChoices.BACKUP)

        nomes = {d["item"].nome for d in computadores_parados()}
        self.assertEqual(nomes, {"NB-PARADO", "DT-PARADO"})

    def test_lista_informa_a_licenca_office_presa_a_maquina(self):
        """É a licença que o TI cancela — sem ela no e-mail, a lista não
        responde à pergunta que motivou o alerta."""
        from services.email_alertas import computadores_parados

        item = self._item("NB-LIC", self.notebook, StatusItemChoices.BACKUP)
        LicencaOffice.objects.create(
            produto="Office Home and Business 2019", chave_produto="XXXXX-YYYYY", item=item,
        )
        dado = computadores_parados()[0]
        self.assertIsNotNone(dado["licenca_office"])
        self.assertEqual(dado["licenca_office"].produto, "Office Home and Business 2019")

    def test_lista_calcula_ha_quanto_tempo_a_maquina_esta_parada(self):
        from services.email_alertas import computadores_parados

        # O historico e gravado por signal em `Item.save()` -- criar a linha a
        # mao alem dele produz DUAS entradas "backup" e a leitura pega a mais
        # recente (foi o que derrubou a primeira versao deste teste). Aqui a
        # maquina entra em backup de verdade e so a data e recuada.
        item = self._item("NB-TEMPO", self.notebook, StatusItemChoices.ATIVO)
        item.status = StatusItemChoices.BACKUP
        item.save()
        ItemStatusHistorico.objects.filter(item=item, status_novo="backup").update(
            alterado_em=timezone.now() - datetime.timedelta(days=40),
        )
        self.assertEqual(computadores_parados()[0]["dias"], 40)

    def test_maquina_sem_historico_aparece_sem_tempo_em_vez_de_zero(self):
        """`dias=None` e "nao sei desde quando", que a tela escreve como
        travessao. Zero seria lido como "parou hoje" -- afirmacao diferente."""
        from services.email_alertas import computadores_parados

        item = self._item("NB-SEM-HIST", self.notebook, StatusItemChoices.PAUSADO)
        ItemStatusHistorico.objects.filter(item=item).delete()
        self.assertIsNone(computadores_parados()[0]["dias"])

    def test_sem_maquina_parada_nenhum_email_sai(self):
        """Caixa de entrada com “0 registros” toda semana é o que faz o
        alerta ser ignorado quando passa a ter conteúdo."""
        from services.email_alertas import alerta_computadores_parados

        self._item("NB-OK", self.notebook, StatusItemChoices.ATIVO)
        with patch("services.email_alertas._enviar") as enviar:
            self.assertFalse(alerta_computadores_parados())
            enviar.assert_not_called()

    def test_email_sai_no_canal_proprio_e_cita_as_duas_situacoes(self):
        from services.email_alertas import alerta_computadores_parados

        self._item("NB-1", self.notebook, StatusItemChoices.BACKUP)
        self._item("DT-1", self.desktop, StatusItemChoices.PAUSADO)

        with patch("services.email_alertas._enviar", return_value=True) as enviar:
            alerta_computadores_parados()
            assunto, _texto, html = enviar.call_args.args[:3]
            self.assertEqual(enviar.call_args.kwargs["codigo"], "computadores_parados")
            self.assertIn("2 computador", assunto)
            self.assertIn("Em backup (1)", html)
            self.assertIn("Pausados (1)", html)

    def test_lista_completa_vai_no_csv_em_anexo(self):
        """O corpo do e-mail corta em 50 linhas por tabela e vários clientes
        truncam HTML acima de ~102 KB -- com 150 máquinas paradas, só o corpo
        entregaria uma lista cortada sem avisar que cortou."""
        from services.email_alertas import alerta_computadores_parados

        item = self._item("NB-CSV", self.notebook, StatusItemChoices.BACKUP)
        LicencaOffice.objects.create(
            produto="Office 2019", chave_produto="AAAAA-BBBBB", item=item,
        )

        with patch("services.email_alertas._enviar", return_value=True) as enviar:
            alerta_computadores_parados()
            anexos = enviar.call_args.kwargs["anexos"]

        self.assertEqual(len(anexos), 1)
        nome, conteudo, mimetype = anexos[0]
        self.assertTrue(nome.endswith(".csv"))
        self.assertEqual(mimetype, "text/csv")
        texto = conteudo.decode("utf-8-sig")
        self.assertIn("NB-CSV", texto)
        self.assertIn("Office 2019", texto)
        self.assertIn("Licença Office", texto)

    def test_csv_abre_no_excel_com_acento_correto(self):
        """Sem BOM o Excel do Windows lê o CSV como ANSI e "Situação" aparece
        corrompido na planilha de conferência."""
        from services.email_alertas import _csv_computadores_parados, computadores_parados

        self._item("NB-BOM", self.notebook, StatusItemChoices.PAUSADO)
        conteudo = _csv_computadores_parados(computadores_parados())
        self.assertTrue(conteudo.startswith(codecs.BOM_UTF8))

    def test_canal_esta_no_catalogo_de_notificacoes(self):
        """Sem entrada no catálogo o canal não aparece em /alertas/notificacoes/
        e o TI não consegue ligar, desligar nem redirecionar o aviso."""
        from services.email_alertas import CATALOGO_NOTIFICACOES

        codigos = {c["codigo"] for c in CATALOGO_NOTIFICACOES}
        self.assertIn("computadores_parados", codigos)

    def test_catalogo_sincroniza_o_canal_na_tabela(self):
        from services.email_alertas import sincronizar_catalogo_notificacoes
        from ProjetoEstoque.models import CanalNotificacao

        sincronizar_catalogo_notificacoes()
        self.assertTrue(CanalNotificacao.objects.filter(codigo="computadores_parados").exists())


class GatilhoDeMovimentacaoTests(TestCase):
    """O aviso na movimentação. Tudo aqui é `transaction.on_commit`, então os
    testes usam `captureOnCommitCallbacks` — sem isso o callback nunca roda e
    o teste passaria sem testar nada."""

    def setUp(self):
        self.categoria, _ = Categoria.objects.get_or_create(nome="Equipamento")
        self.notebook = _subtipo("Notebook")
        self.switch = _subtipo("Switches")

    def _item(self, nome, subtipo, status):
        return Item.objects.create(
            nome=nome, subtipo=subtipo, categoria=self.categoria, status=status,
        )

    def test_notebook_que_entra_em_backup_dispara_o_aviso(self):
        item = self._item("NB-MOV", self.notebook, StatusItemChoices.ATIVO)
        with patch("services.email_alertas.alerta_computadores_parados") as alerta:
            with self.captureOnCommitCallbacks(execute=True):
                item.status = StatusItemChoices.BACKUP
                item.save()
        alerta.assert_called_once()
        self.assertEqual(alerta.call_args.kwargs["item_destaque_pk"], item.pk)
        self.assertEqual(alerta.call_args.kwargs["status_anterior"], "ativo")

    def test_notebook_que_volta_a_ativo_tambem_dispara(self):
        """A volta é quando se RELIGA a licença — avisar só a ida deixaria
        máquina em uso com licença cancelada."""
        item = self._item("NB-VOLTA", self.notebook, StatusItemChoices.BACKUP)
        with patch("services.email_alertas.alerta_computadores_parados") as alerta:
            with self.captureOnCommitCallbacks(execute=True):
                item.status = StatusItemChoices.ATIVO
                item.save()
        alerta.assert_called_once()
        self.assertEqual(alerta.call_args.kwargs["status_anterior"], "backup")

    def test_troca_entre_dois_status_parados_nao_avisa_de_novo(self):
        item = self._item("NB-BK", self.notebook, StatusItemChoices.BACKUP)
        with patch("services.email_alertas.alerta_computadores_parados") as alerta:
            with self.captureOnCommitCallbacks(execute=True):
                item.status = StatusItemChoices.PAUSADO
                item.save()
        alerta.assert_not_called()

    def test_switch_em_backup_nao_dispara_o_aviso(self):
        item = self._item("SW-BK", self.switch, StatusItemChoices.ATIVO)
        with patch("services.email_alertas.alerta_computadores_parados") as alerta:
            with self.captureOnCommitCallbacks(execute=True):
                item.status = StatusItemChoices.BACKUP
                item.save()
        alerta.assert_not_called()

    def test_criar_notebook_ja_em_backup_nao_dispara(self):
        """Cadastro não é movimentação: avisar aqui encheria a caixa durante
        uma importação de planilha."""
        with patch("services.email_alertas.alerta_computadores_parados") as alerta:
            with self.captureOnCommitCallbacks(execute=True):
                self._item("NB-NOVO", self.notebook, StatusItemChoices.BACKUP)
        alerta.assert_not_called()
