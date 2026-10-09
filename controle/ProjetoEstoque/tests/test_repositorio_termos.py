# -*- coding: utf-8 -*-
"""
Repositório de Termos — arquivo por pastas das duas origens de termo.

Escopo DELIBERADAMENTE estreito (foi o pedido): só o que quebraria a operação
sem avisar. Nada de testar rótulo, ícone ou contagem de KPI — isso a tela
mostra e o olho confere.

O que está travado aqui:

  • a DE-DUPLICAÇÃO entre as duas fontes. É a única regra do módulo que, se
    errar, erra em silêncio e com cara de certo: o termo digital grava o PDF no
    `termo_pdf` de cada movimentação vinculada, então sem a de-dup um termo de
    3 equipamentos apareceria 4 vezes (uma como ato, três como movimentações) e
    ninguém saberia dizer qual é o documento;
  • o termo digital SEM movimentação vinculada aparecer — era o caso invisível
    que motivou a mudança;
  • pendência não cobrar termo de quem já assinou (cobraria uma segunda
    assinatura do mesmo ato);
  • as três visões da rota responderem 200.

MEDIA_ROOT temporário: `FileField.save()` escreve no disco de verdade mesmo sob
o runner (ver o cabeçalho de `test_termo_assinatura.py`).
"""
import shutil
import tempfile

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from ProjetoEstoque.models import (
    CentroCusto,
    Fornecedor,
    Item,
    Localidade,
    MovimentacaoItem,
    StatusAssinaturaTermoChoices,
    StatusItemChoices,
    TermoAssinatura,
    Usuario,
)
from services import repositorio_termos_service as rts

_MEDIA_TEMP = tempfile.mkdtemp(prefix="media_repo_termos_")


@override_settings(MEDIA_ROOT=_MEDIA_TEMP)
class RepositorioTermosTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA_TEMP, ignore_errors=True)

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="ti.repo", password="senha-de-teste-123", is_staff=True
        )
        cls.cc = CentroCusto.objects.create(numero="300", departamento="TI", pmb="nao")
        cls.local = Localidade.objects.create(local="KARITEL")
        # CNPJ distinto: a coluna é única e dois vazios colidem.
        cls.forn_a = Fornecedor.objects.create(nome="FORNECEDOR A", cnpj="11.111.111/0001-11")
        cls.forn_b = Fornecedor.objects.create(nome="FORNECEDOR B", cnpj="22.222.222/0001-22")

        cls.colab = Usuario.objects.create(
            matricula="70001", nome="ANA DE TESTE", status="ativo", pmb="nao",
            centro_custo=cls.cc, localidade=cls.local, email="ana@exemplo.com.br",
        )
        cls.item_a = Item.objects.create(
            nome="NOTEBOOK A", numero_serie="SN-A", status=StatusItemChoices.ATIVO,
            item_consumo="nao", pmb="nao", centro_custo=cls.cc, localidade=cls.local,
            fornecedor=cls.forn_a,
        )
        cls.item_b = Item.objects.create(
            nome="MONITOR B", numero_serie="SN-B", status=StatusItemChoices.ATIVO,
            item_consumo="nao", pmb="nao", centro_custo=cls.cc, localidade=cls.local,
            fornecedor=cls.forn_b,
        )

    # ── fábricas ─────────────────────────────────────────────────────────

    def _mov(self, item, *, com_pdf=True, tipo="entrega"):
        mov = MovimentacaoItem.objects.create(
            tipo_movimentacao="transferencia",
            tipo_transferencia=tipo,
            item=item,
            usuario=self.colab,
            quantidade=1,
            localidade_destino=self.local,
        )
        if com_pdf:
            mov.termo_pdf.save(f"termo_{mov.pk}.pdf", ContentFile(b"%PDF-1.4 papel"), save=True)
        return mov

    def _assinatura(self, itens, *, movimentacoes=(), tipo="entrega"):
        """
        Nasce PENDENTE, recebe o PDF e só então vira ASSINADO — a MESMA ordem
        de `registrar_assinatura`, e não um atalho de teste.

        Não é preciosismo: `TermoAssinatura.save()` recusa alterar a prova de um
        termo já assinado (regra 42), então anexar o PDF depois de marcar
        ASSINADO é justamente o que o model existe para barrar. Uma fábrica que
        contornasse isso estaria testando um estado que o sistema não produz.
        """
        termo = TermoAssinatura.objects.create(
            tipo=tipo,
            colaborador=self.colab,
            status=StatusAssinaturaTermoChoices.PENDENTE,
            token_hash=f"hash-{timezone.now().timestamp()}-{len(itens)}",
            expira_em=timezone.now(),
            codigo_verificacao=f"TEST-{TermoAssinatura.objects.count():04d}-AAAA",
            dados={"equipamentos": [
                {"nome": i.nome, "numero_serie": i.numero_serie} for i in itens
            ]},
        )
        termo.pdf.save("assinado.pdf", ContentFile(b"%PDF-1.4 digital"), save=False)
        termo.assinado_em = timezone.now()
        termo.status = StatusAssinaturaTermoChoices.ASSINADO
        termo.save()

        termo.itens.set(itens)
        if movimentacoes:
            termo.movimentacoes.set(movimentacoes)
        return termo

    # ── de-duplicação: o coração do módulo ───────────────────────────────

    def test_termo_digital_substitui_as_movimentacoes_que_ele_cobre(self):
        """
        Um termo digital de 2 equipamentos cobre 2 movimentações, e o MESMO
        arquivo está no `termo_pdf` das duas. Sem a de-dup o acervo mostraria
        3 linhas para 1 documento.
        """
        mov_a = self._mov(self.item_a)
        mov_b = self._mov(self.item_b)
        self._assinatura([self.item_a, self.item_b], movimentacoes=[mov_a, mov_b])

        acervo = rts.acervo(rts.Filtros())

        self.assertEqual(len(acervo), 1, "o documento é um só — uma linha")
        self.assertTrue(acervo[0].digital)
        self.assertEqual(len(acervo[0].equipamentos), 2)

    def test_movimentacao_nao_coberta_continua_no_acervo(self):
        """A de-dup não pode varrer o que ela não cobre: anexo de papel sem
        assinatura digital é documento legítimo e único."""
        mov_a = self._mov(self.item_a)
        self._mov(self.item_b)
        self._assinatura([self.item_a], movimentacoes=[mov_a])

        acervo = rts.acervo(rts.Filtros())

        self.assertEqual(len(acervo), 2)
        self.assertEqual(
            {linha.origem for linha in acervo}, {"digital", "anexo"},
        )

    def test_termo_digital_sem_movimentacao_aparece(self):
        """Era o caso INVISÍVEL que motivou a mudança: assinatura cujo registro
        de movimentação falhou (caminho que `_registrar_movimentacoes` loga e
        segue) não aparecia em repositório nenhum."""
        self._assinatura([self.item_a])

        acervo = rts.acervo(rts.Filtros())

        self.assertEqual(len(acervo), 1)
        self.assertTrue(acervo[0].digital)

    def test_filtro_anexo_nao_mostra_termo_assinado_no_tablet(self):
        """Filtrar por "anexado em papel" tem de excluir as movimentações
        cobertas por assinatura — o PDF delas É o documento digital."""
        mov_a = self._mov(self.item_a)
        self._mov(self.item_b)
        self._assinatura([self.item_a], movimentacoes=[mov_a])

        acervo = rts.acervo(rts.Filtros(origem="anexo"))

        self.assertEqual(len(acervo), 1)
        self.assertEqual(acervo[0].equipamentos[0]["nome"], "MONITOR B")

    # ── pendências ───────────────────────────────────────────────────────

    def test_movimentacao_sem_pdf_mas_com_assinatura_nao_e_pendencia(self):
        """
        Acontece de verdade: `_anexar_pdf_nas_movimentacoes` pula campo já
        preenchido, e a gravação pode falhar. Cobrar o termo outra vez mandaria
        o TI colher uma SEGUNDA assinatura do mesmo ato.
        """
        mov = self._mov(self.item_a, com_pdf=False)
        self._assinatura([self.item_a], movimentacoes=[mov])

        _, total = rts.pendencias(rts.Filtros())
        self.assertEqual(total, 0)

    def test_movimentacao_sem_pdf_e_sem_assinatura_e_pendencia(self):
        self._mov(self.item_a, com_pdf=False)

        pendentes, total = rts.pendencias(rts.Filtros())
        self.assertEqual(total, 1)
        self.assertEqual(pendentes[0].item_id, self.item_a.pk)

    # ── pastas ───────────────────────────────────────────────────────────

    def test_pasta_por_colaborador_agrupa_o_acervo(self):
        self._mov(self.item_a)
        self._mov(self.item_b)

        acervo = rts.acervo(rts.Filtros())
        pastas = rts.pastas(rts.eixo_valido("colaborador"), acervo)

        self.assertEqual(len(pastas), 1)
        self.assertEqual(pastas[0].rotulo, "ANA DE TESTE")
        self.assertEqual(pastas[0].total, 2)

    def test_termo_com_dois_fornecedores_aparece_nas_duas_pastas(self):
        """Pertinência múltipla é a verdade do eixo fornecedor, e a tela avisa
        que a soma das pastas pode passar o total do acervo."""
        self._assinatura([self.item_a, self.item_b])

        acervo = rts.acervo(rts.Filtros())
        pastas = rts.pastas(rts.eixo_valido("fornecedor"), acervo)

        self.assertEqual(len(acervo), 1)
        self.assertEqual({p.rotulo for p in pastas}, {"FORNECEDOR A", "FORNECEDOR B"})
        self.assertEqual(sum(p.total for p in pastas), 2)

    def test_conteudo_da_pasta_devolve_so_as_linhas_dela(self):
        self._mov(self.item_a)
        outro = Usuario.objects.create(
            matricula="70002", nome="BRUNO DE TESTE", status="ativo", pmb="nao",
            centro_custo=self.cc, localidade=self.local,
        )
        mov = self._mov(self.item_b)
        MovimentacaoItem.objects.filter(pk=mov.pk).update(usuario=outro)

        eixo = rts.eixo_valido("colaborador")
        acervo = rts.acervo(rts.Filtros())
        conteudo = rts.conteudo(eixo, str(self.colab.pk), acervo)

        self.assertEqual(len(conteudo), 1)
        self.assertEqual(conteudo[0].colaborador_nome, "ANA DE TESTE")

    def test_eixo_invalido_cai_no_padrao_em_vez_de_quebrar(self):
        """O slug vem da URL: `?agrupar=qualquer-coisa` tem de abrir a tela."""
        self.assertEqual(rts.eixo_valido("nao-existe").slug, rts.EIXO_PADRAO)
        self.assertEqual(rts.eixo_valido("").slug, rts.EIXO_PADRAO)

    # ── busca ────────────────────────────────────────────────────────────

    def test_busca_encontra_pelo_codigo_de_verificacao(self):
        """Código de verificação é o que o TI tem na mão quando alguém manda um
        PDF perguntando "este é autêntico?"."""
        termo = self._assinatura([self.item_a])

        acervo = rts.acervo(rts.Filtros(q=termo.codigo_verificacao))

        self.assertEqual(len(acervo), 1)
        self.assertEqual(acervo[0].codigo_verificacao, termo.codigo_verificacao)

    def test_busca_usa_o_nome_do_snapshot_e_nao_o_nome_atual_do_item(self):
        """
        O documento assinado diz o nome que o equipamento tinha NO ATO. Buscar
        no banco por `itens__nome` acharia o nome de hoje — e o TI que procura
        pelo que está escrito no PDF não encontraria nada.
        """
        self._assinatura([self.item_a])
        Item.objects.filter(pk=self.item_a.pk).update(nome="RENOMEADO DEPOIS")

        self.assertEqual(len(rts.acervo(rts.Filtros(q="NOTEBOOK A"))), 1)
        self.assertEqual(len(rts.acervo(rts.Filtros(q="RENOMEADO DEPOIS"))), 0)

    # ── a tela ───────────────────────────────────────────────────────────

    def test_as_tres_visoes_da_rota_respondem(self):
        mov = self._mov(self.item_a)
        self._assinatura([self.item_b])
        self.client.force_login(self.user)
        url = reverse("repositorio_termos")

        for nome, params in (
            ("pastas", {}),
            ("pasta", {"agrupar": "colaborador", "pasta": str(self.colab.pk)}),
            ("busca", {"q": "NOTEBOOK"}),
        ):
            with self.subTest(visao=nome):
                resposta = self.client.get(url, params)
                self.assertEqual(resposta.status_code, 200)
                self.assertEqual(resposta.context["visao"], nome)

        # Todos os eixos têm de renderizar — cada um tem regra de chave própria.
        for eixo in rts.EIXOS:
            with self.subTest(eixo=eixo.slug):
                self.assertEqual(
                    self.client.get(url, {"agrupar": eixo.slug}).status_code, 200
                )

        self.assertEqual(mov.pk, mov.pk)  # guarda contra fábrica não usada

    def test_a_tela_exige_login(self):
        resposta = self.client.get(reverse("repositorio_termos"))
        self.assertIn(resposta.status_code, (302, 403))

    def test_pasta_inexistente_nao_quebra(self):
        """Link velho, pasta que esvaziou por filtro, id que não existe mais."""
        self.client.force_login(self.user)
        resposta = self.client.get(
            reverse("repositorio_termos"),
            {"agrupar": "colaborador", "pasta": "999999"},
        )
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(len(resposta.context["page_obj"].object_list), 0)
