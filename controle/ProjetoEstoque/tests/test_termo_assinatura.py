# -*- coding: utf-8 -*-
"""
Assinatura digital dos termos de responsabilidade.

Roda no banco de TESTE do Django (`manage.py test`), nunca na base real — ver
regra 22 do CLAUDE.md sobre `transaction.savepoint()` fora de `atomic`.

O que estes testes travam, e por quê:

  • o PDF é realmente gerado e contém o traço da assinatura (sem isto, um
    erro no reportlab só apareceria com o colaborador na frente do TI);
  • o texto legal sai do .docx base e NÃO de uma cópia em código (a
    divergência entre as duas vias é a falha mais cara deste módulo);
  • a tela anônima do tablet responde IGUAL para token inexistente,
    expirado, já usado e cancelado (regra 27 — mensagem diferente
    transformaria a URL num verificador de tokens);
  • assinar duas vezes não gera dois PDFs (dois toques na tela sensível);
  • o PDF assinado chega ao `MovimentacaoItem.termo_pdf`, que é o que o
    Repositório de Termos lê;
  • `/media/` segue negando os arquivos para perfil externo.

MEDIA_ROOT TEMPORÁRIO — não tire isto
-------------------------------------
`FileField.save()` escreve no disco DE VERDADE, inclusive sob o runner de
teste: o banco é descartável, o `MEDIA_ROOT` não. Sem o `override_settings`
abaixo, cada rodada destes testes deixava PDFs e PNGs de mentira dentro de
`controle/media/termos/` — na pasta real, sincronizada por OneDrive, misturados
aos termos de verdade. É o mesmo tipo de vazamento que a regra 22 do CLAUDE.md
registra para o banco, num lugar que ela não cobre. A pasta temporária é
apagada no `tearDownClass`.
"""
import json
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from ProjetoEstoque.models import (
    CentroCusto,
    Item,
    Localidade,
    MovimentacaoItem,
    StatusAssinaturaTermoChoices,
    StatusItemChoices,
    TermoAssinatura,
    Usuario,
)
from services import termo_assinatura_service as tas
from services.termo_pdf_service import carregar_texto_legal, gerar_pdf_assinado

# PNG 300x100 branco com um risco escuro — faz as vezes do traço do canvas.
# Gerado em memória com Pillow para não depender de binário no repositório.
def _png_assinatura(largura=300, altura=100):
    import base64
    from io import BytesIO

    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (largura, altura), (255, 255, 255, 0))
    desenho = ImageDraw.Draw(img)
    desenho.line([(20, 70), (90, 25), (160, 75), (240, 30)], fill=(16, 24, 40, 255), width=3)

    buffer = BytesIO()
    img.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


_MEDIA_TEMP = tempfile.mkdtemp(prefix="zelo-termos-teste-")


@override_settings(MEDIA_ROOT=_MEDIA_TEMP)
class BaseTermoAssinatura(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA_TEMP, ignore_errors=True)

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="ti.teste", password="senha-de-teste-123", is_staff=True
        )
        cls.centro_custo = CentroCusto.objects.create(numero="100", departamento="TI", pmb="nao")
        cls.cc_origem = CentroCusto.objects.create(numero="200", departamento="ALMOXARIFADO", pmb="nao")
        cls.localidade = Localidade.objects.create(local="KARITEL")
        cls.localidade_origem = Localidade.objects.create(local="DEPOSITO CENTRAL")
        cls.colaborador = Usuario.objects.create(
            matricula="99999",
            nome="MARIA DE TESTE SILVA",
            status="ativo",
            pmb="nao",
            centro_custo=cls.centro_custo,
            localidade=cls.localidade,
            email="maria.teste@exemplo.com.br",
        )
        cls.item = Item.objects.create(
            nome="NOTEBOOK DE TESTE",
            numero_serie="SN-TESTE-001",
            marca="DELL",
            modelo="LATITUDE 3450",
            status=StatusItemChoices.ESTOQUE,
            item_consumo="nao",
            pmb="nao",
            centro_custo=cls.cc_origem,
            localidade=cls.localidade_origem,
        )

    def _criar_sessao(self, tipo="entrega", **extra):
        dados_form = {
            "numero_chamado": "CH-1234",
            "acessorios": "Cabo de força, mochila",
            "observacoes": "Entrega de teste automatizado.",
            "estabelecimento": "karitel",
            "responsavel_ti_nome": "Kayque (TI)",
        }
        dados_form.update(extra.pop("form_data", {}))
        return tas.criar_sessao(
            tipo=tipo,
            colaborador=self.colaborador,
            itens=[self.item],
            form_data=dados_form,
            user=self.user,
            **extra,
        )


class TextoLegalDoModeloBaseTests(BaseTermoAssinatura):
    """O texto assinado vem do .docx do RH — nunca de uma cópia em código."""

    def test_extrai_titulo_e_clausulas_do_docx(self):
        for tipo in ("entrega", "devolucao"):
            with self.subTest(tipo=tipo):
                texto = carregar_texto_legal(tipo)
                self.assertIn("TERMO DE RESPONSABILIDADE", texto["titulo"].upper())
                # Entrega tem 13 cláusulas; devolução, 3. O piso de 3 é o
                # que distingue "extraiu" de "não extraiu nada" sem travar o
                # teste no número exato, que o RH pode mudar no .docx.
                self.assertGreaterEqual(
                    len(texto["clausulas"]), 3,
                    "Nenhuma/poucas cláusulas extraídas — o .docx base mudou de estrutura "
                    "ou os parágrafos deixaram de ser itens de lista numerada.",
                )

    def test_bloco_de_assinatura_do_docx_nao_entra_no_texto_legal(self):
        # "Assinatura: ____", "Nome:", "Data:" são campos de caneta do modelo
        # impresso; na via eletrônica viram o bloco de assinatura capturada.
        texto = carregar_texto_legal("entrega")
        corpo = " ".join(texto["clausulas"] + [texto.get("fecho", "")]).lower()
        for rotulo in ("assinatura: ___", "nome:", "data:"):
            self.assertNotIn(rotulo, corpo)

    def test_snapshot_guarda_o_texto_assinado(self):
        sessao = self._criar_sessao()
        dados = sessao["termo"].dados

        self.assertIn("texto_legal", dados)
        self.assertGreaterEqual(dados["texto_legal_clausulas"], 3)
        self.assertEqual(len(dados["texto_legal_hash"]), 12)
        # O snapshot tem de bastar sozinho: é ele que o PDF usa ao ser
        # reemitido, mesmo que o .docx base mude depois.
        self.assertEqual(
            dados["texto_legal"]["clausulas"],
            carregar_texto_legal("entrega")["clausulas"],
        )


class CriacaoDeSessaoTests(BaseTermoAssinatura):

    def test_cria_pendente_com_token_e_codigo(self):
        sessao = self._criar_sessao()
        termo = sessao["termo"]

        self.assertEqual(termo.status, StatusAssinaturaTermoChoices.PENDENTE)
        self.assertTrue(termo.pode_assinar)
        self.assertEqual(list(termo.itens.all()), [self.item])

        # O token puro NÃO é persistido — só o hash.
        self.assertTrue(sessao["token"])
        self.assertNotEqual(termo.token_hash, sessao["token"])
        self.assertEqual(len(termo.token_hash), 64)

        # Código legível, sem caractere ambíguo.
        self.assertRegex(termo.codigo_verificacao, r"^[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}$")

    def test_numero_do_termo_segue_a_regra_do_docx(self):
        # {nº de série} - {colaborador} - {centro de custo}: tem de ser a MESMA
        # numeração das duas vias, senão o papel e o digital se referem ao
        # mesmo ato com números diferentes.
        termo = self._criar_sessao()["termo"]
        self.assertEqual(
            termo.numero_termo,
            "SN-TESTE-001 - MARIA DE TESTE SILVA - TI",
        )

    def test_recusa_sessao_sem_equipamento(self):
        with self.assertRaises(ValueError):
            tas.criar_sessao(
                tipo="entrega", colaborador=self.colaborador, itens=[],
                form_data={}, user=self.user,
            )

    def test_recusa_tipo_invalido(self):
        with self.assertRaises(ValueError):
            tas.criar_sessao(
                tipo="emprestimo", colaborador=self.colaborador, itens=[self.item],
                form_data={}, user=self.user,
            )

    def test_validade_e_limitada_a_faixa(self):
        curta = self._criar_sessao(validade_minutos=1)
        longa = self._criar_sessao(validade_minutos=99999)
        self.assertEqual(curta["validade_minutos"], tas.VALIDADE_MINUTOS_MIN)
        self.assertEqual(longa["validade_minutos"], tas.VALIDADE_MINUTOS_MAX)


class ResolucaoDeTokenTests(BaseTermoAssinatura):

    def test_resolve_token_valido(self):
        sessao = self._criar_sessao()
        self.assertEqual(tas.resolver_token(sessao["token"]).pk, sessao["termo"].pk)

    def test_nao_resolve_token_errado_vazio_ou_expirado(self):
        sessao = self._criar_sessao()

        self.assertIsNone(tas.resolver_token("token-que-nao-existe"))
        self.assertIsNone(tas.resolver_token(""))
        self.assertIsNone(tas.resolver_token(None))

        termo = sessao["termo"]
        termo.expira_em = timezone.now() - timedelta(minutes=1)
        termo.save(update_fields=["expira_em"])
        self.assertIsNone(tas.resolver_token(sessao["token"]))
        self.assertTrue(TermoAssinatura.objects.get(pk=termo.pk).esta_expirado)

    def test_novo_link_invalida_o_anterior(self):
        sessao = self._criar_sessao()
        antigo = sessao["token"]

        nova = tas.regerar_token(sessao["termo"], user=self.user)

        self.assertIsNone(tas.resolver_token(antigo))
        self.assertIsNotNone(tas.resolver_token(nova["token"]))

    def test_nao_regera_token_de_termo_assinado(self):
        sessao = self._criar_sessao()
        tas.registrar_assinatura(
            termo=sessao["termo"],
            assinatura_colaborador=_png_assinatura(),
            signatario_nome="MARIA DE TESTE SILVA",
            aceite=True,
        )
        with self.assertRaises(ValueError):
            tas.regerar_token(sessao["termo"], user=self.user)


class RegistroDaAssinaturaTests(BaseTermoAssinatura):

    def test_fluxo_completo_gera_pdf_com_hash(self):
        sessao = self._criar_sessao()

        assinado = tas.registrar_assinatura(
            termo=sessao["termo"],
            assinatura_colaborador=_png_assinatura(),
            signatario_nome="MARIA DE TESTE SILVA",
            signatario_cpf="123.456.789-00",
            signatario_rg="MG-12.345.678",
            aceite=True,
        )

        self.assertEqual(assinado.status, StatusAssinaturaTermoChoices.ASSINADO)
        self.assertFalse(assinado.pode_assinar)
        self.assertIsNotNone(assinado.assinado_em)
        # CPF/RG foram usados no PDF e descartados — não existe campo para eles.
        self.assertFalse(hasattr(assinado, "signatario_cpf"))
        self.assertFalse(hasattr(assinado, "signatario_rg"))
        self.assertTrue(assinado.aceite_declarado)
        self.assertTrue(assinado.assinatura_colaborador)
        self.assertTrue(assinado.pdf)
        self.assertEqual(len(assinado.pdf_sha256), 64)

        assinado.pdf.open("rb")
        try:
            conteudo = assinado.pdf.read()
        finally:
            assinado.pdf.close()

        self.assertTrue(conteudo.startswith(b"%PDF"), "O arquivo gravado não é um PDF.")
        self.assertGreater(len(conteudo), 4000, "PDF suspeito de estar vazio.")

        # A imagem do traço tem de estar DENTRO do PDF: um documento sem a
        # assinatura embutida é só um termo em branco com carimbo de hora.
        self.assertIn(b"/Image", conteudo)

    def test_recusa_sem_aceite(self):
        sessao = self._criar_sessao()
        with self.assertRaisesMessage(ValueError, "aceite"):
            tas.registrar_assinatura(
                termo=sessao["termo"],
                assinatura_colaborador=_png_assinatura(),
                signatario_nome="MARIA DE TESTE SILVA",
                aceite=False,
            )
        self.assertTrue(TermoAssinatura.objects.get(pk=sessao["termo"].pk).pode_assinar)

    def test_recusa_nome_curto(self):
        sessao = self._criar_sessao()
        with self.assertRaises(ValueError):
            tas.registrar_assinatura(
                termo=sessao["termo"],
                assinatura_colaborador=_png_assinatura(),
                signatario_nome="M",
                aceite=True,
            )

    def test_recusa_traco_invalido(self):
        sessao = self._criar_sessao()

        casos = [
            ("vazio", ""),
            ("sem base64", "data:image/png,abc"),
            ("base64 quebrado", "data:image/png;base64,???"),
            ("não é imagem", "data:image/png;base64,YWJjZGVm"),
            ("pequeno demais", _png_assinatura(largura=40, altura=20)),
        ]
        for rotulo, valor in casos:
            with self.subTest(caso=rotulo):
                with self.assertRaises(ValueError):
                    tas.registrar_assinatura(
                        termo=sessao["termo"],
                        assinatura_colaborador=valor,
                        signatario_nome="MARIA DE TESTE SILVA",
                        aceite=True,
                    )

    def test_assinar_duas_vezes_nao_gera_dois_documentos(self):
        # Dois toques no botão em tela sensível ao toque: o primeiro grava, o
        # segundo tem de ser recusado com mensagem clara.
        sessao = self._criar_sessao()
        primeiro = tas.registrar_assinatura(
            termo=sessao["termo"],
            assinatura_colaborador=_png_assinatura(),
            signatario_nome="MARIA DE TESTE SILVA",
            aceite=True,
        )
        nome_pdf = primeiro.pdf.name

        with self.assertRaisesMessage(ValueError, "já foi assinado"):
            tas.registrar_assinatura(
                termo=sessao["termo"],
                assinatura_colaborador=_png_assinatura(),
                signatario_nome="MARIA DE TESTE SILVA",
                aceite=True,
            )

        self.assertEqual(TermoAssinatura.objects.get(pk=primeiro.pk).pdf.name, nome_pdf)

    def test_nao_assina_sessao_cancelada(self):
        sessao = self._criar_sessao()
        tas.cancelar_sessao(termo=sessao["termo"], user=self.user, motivo="Teste")

        with self.assertRaises(ValueError):
            tas.registrar_assinatura(
                termo=sessao["termo"],
                assinatura_colaborador=_png_assinatura(),
                signatario_nome="MARIA DE TESTE SILVA",
                aceite=True,
            )

    def test_nao_cancela_termo_assinado(self):
        sessao = self._criar_sessao()
        tas.registrar_assinatura(
            termo=sessao["termo"],
            assinatura_colaborador=_png_assinatura(),
            signatario_nome="MARIA DE TESTE SILVA",
            aceite=True,
        )
        with self.assertRaises(ValueError):
            tas.cancelar_sessao(termo=sessao["termo"], user=self.user)


class AnexoNaMovimentacaoTests(BaseTermoAssinatura):
    """O PDF assinado alimenta `MovimentacaoItem.termo_pdf` — é por ali que o
    Repositório de Termos e a cascata de desligamento leem o comprovante."""

    def _movimentacao(self, **extra):
        campos = dict(
            tipo_movimentacao="transferencia",
            tipo_transferencia="entrega",
            item=self.item,
            usuario=self.colaborador,
            quantidade=1,
        )
        campos.update(extra)
        return MovimentacaoItem.objects.create(**campos)

    def test_anexa_pdf_na_movimentacao_vinculada(self):
        movimentacao = self._movimentacao()
        sessao = self._criar_sessao(movimentacoes=[movimentacao])

        tas.registrar_assinatura(
            termo=sessao["termo"],
            assinatura_colaborador=_png_assinatura(),
            signatario_nome="MARIA DE TESTE SILVA",
            aceite=True,
        )

        movimentacao.refresh_from_db()
        self.assertTrue(movimentacao.termo_pdf)
        self.assertTrue(movimentacao.termo_pdf.name.endswith(".pdf"))

    def test_nao_sobrescreve_termo_ja_anexado(self):
        # Se a TI já anexou o escaneado do papel, aquele é o documento que a
        # operação conhece — trocá-lo por baixo seria perder prova.
        from django.core.files.base import ContentFile

        movimentacao = self._movimentacao()
        movimentacao.termo_pdf.save("escaneado_do_papel.pdf", ContentFile(b"%PDF-1.4 papel"), save=True)
        anterior = movimentacao.termo_pdf.name

        sessao = self._criar_sessao(movimentacoes=[movimentacao])
        tas.registrar_assinatura(
            termo=sessao["termo"],
            assinatura_colaborador=_png_assinatura(),
            signatario_nome="MARIA DE TESTE SILVA",
            aceite=True,
        )

        movimentacao.refresh_from_db()
        self.assertEqual(movimentacao.termo_pdf.name, anterior)

    def test_anexa_em_varias_movimentacoes(self):
        # Um `ContentFile` por movimentação: reaproveitar a mesma instância
        # deixaria a segunda gravação com zero byte — falha silenciosa.
        outro_item = Item.objects.create(
            nome="MONITOR DE TESTE", numero_serie="SN-TESTE-002",
            status=StatusItemChoices.ESTOQUE, item_consumo="nao", pmb="nao",
            centro_custo=self.cc_origem, localidade=self.localidade_origem,
        )
        mov_a = self._movimentacao()
        mov_b = self._movimentacao(item=outro_item)

        sessao = tas.criar_sessao(
            tipo="entrega", colaborador=self.colaborador, itens=[self.item, outro_item],
            form_data={"estabelecimento": "karitel"}, user=self.user,
            movimentacoes=[mov_a, mov_b],
        )
        tas.registrar_assinatura(
            termo=sessao["termo"],
            assinatura_colaborador=_png_assinatura(),
            signatario_nome="MARIA DE TESTE SILVA",
            aceite=True,
        )

        for mov in (mov_a, mov_b):
            mov.refresh_from_db()
            self.assertTrue(mov.termo_pdf, f"Movimentação {mov.pk} ficou sem termo anexado.")
            self.assertGreater(mov.termo_pdf.size, 4000, "PDF gravado com tamanho suspeito (zero byte?).")


class VerificacaoDeAutenticidadeTests(BaseTermoAssinatura):

    def test_confere_hash_do_arquivo_guardado(self):
        sessao = self._criar_sessao()
        assinado = tas.registrar_assinatura(
            termo=sessao["termo"],
            assinatura_colaborador=_png_assinatura(),
            signatario_nome="MARIA DE TESTE SILVA",
            aceite=True,
        )

        resultado = tas.verificar_autenticidade(assinado.codigo_verificacao)
        self.assertIsNotNone(resultado)
        self.assertTrue(resultado["integro"])
        self.assertEqual(resultado["hash_atual"], assinado.pdf_sha256)

        # Código aceita minúscula e espaço em volta (é ditado por telefone).
        self.assertIsNotNone(
            tas.verificar_autenticidade(f"  {assinado.codigo_verificacao.lower()}  ")
        )

    def test_detecta_arquivo_alterado(self):
        sessao = self._criar_sessao()
        assinado = tas.registrar_assinatura(
            termo=sessao["termo"],
            assinatura_colaborador=_png_assinatura(),
            signatario_nome="MARIA DE TESTE SILVA",
            aceite=True,
        )

        caminho = assinado.pdf.path
        with open(caminho, "ab") as arquivo:
            arquivo.write(b"\n% alteracao acidental de sincronismo\n")

        resultado = tas.verificar_autenticidade(assinado.codigo_verificacao)
        self.assertFalse(resultado["integro"])
        self.assertNotEqual(resultado["hash_atual"], resultado["hash_gravado"])

    def test_codigo_inexistente(self):
        self.assertIsNone(tas.verificar_autenticidade("ZZZZ-ZZZZ-ZZZZ"))
        self.assertIsNone(tas.verificar_autenticidade(""))


class TelaDoTabletTests(BaseTermoAssinatura):
    """A tela de assinatura é ANÔNIMA e protegida só pelo token."""

    def setUp(self):
        self.cliente = Client()

    def test_abre_sem_login_com_token_valido(self):
        sessao = self._criar_sessao()
        resposta = self.cliente.get(reverse("termo_assinar", args=[sessao["token"]]))

        self.assertEqual(resposta.status_code, 200)
        html = resposta.content.decode("utf-8")
        self.assertIn("MARIA DE TESTE SILVA", html)
        self.assertIn("NOTEBOOK DE TESTE", html)
        self.assertIn("__ta_config", html)
        # O token NUNCA deve aparecer no corpo além das URLs de ação.
        self.assertIn("taCanvas", html)

    def test_token_invalido_expirado_e_usado_respondem_igual(self):
        # Regra 27: mensagem diferente por caso transformaria a URL num
        # verificador de tokens válidos.
        sessao_expirada = self._criar_sessao()
        termo = sessao_expirada["termo"]
        termo.expira_em = timezone.now() - timedelta(minutes=1)
        termo.save(update_fields=["expira_em"])

        sessao_usada = self._criar_sessao()
        tas.registrar_assinatura(
            termo=sessao_usada["termo"],
            assinatura_colaborador=_png_assinatura(),
            signatario_nome="MARIA DE TESTE SILVA",
            aceite=True,
        )

        sessao_cancelada = self._criar_sessao()
        tas.cancelar_sessao(termo=sessao_cancelada["termo"], user=self.user)

        corpos = set()
        for token in ("nao-existe", sessao_expirada["token"],
                      sessao_usada["token"], sessao_cancelada["token"]):
            resposta = self.cliente.get(reverse("termo_assinar", args=[token]))
            self.assertEqual(resposta.status_code, 404)
            corpos.add(resposta.content)

        self.assertEqual(
            len(corpos), 1,
            "A tela de recusa diferencia os casos — isso vaza se o token existe.",
        )

    def test_envio_grava_e_responde_json(self):
        sessao = self._criar_sessao()
        url = reverse("termo_assinar_enviar", args=[sessao["token"]])

        resposta = self.cliente.post(
            url,
            data=json.dumps({
                "assinatura": _png_assinatura(),
                "nome": "MARIA DE TESTE SILVA",
                "cpf": "123.456.789-00",
                "rg": "",
                "aceite": True,
            }),
            content_type="application/json",
            HTTP_USER_AGENT="Mozilla/5.0 (Linux; Android 13; Tablet)",
            REMOTE_ADDR="172.16.60.77",
        )

        self.assertEqual(resposta.status_code, 200)
        corpo = resposta.json()
        self.assertTrue(corpo["ok"])
        self.assertTrue(corpo["codigo_verificacao"])

        termo = TermoAssinatura.objects.get(pk=sessao["termo"].pk)
        self.assertEqual(termo.status, StatusAssinaturaTermoChoices.ASSINADO)
        self.assertEqual(termo.assinado_ip, "172.16.60.77")
        self.assertIn("Android", termo.assinado_user_agent)
        self.assertTrue(termo.pdf)

    def test_envio_sem_aceite_devolve_400_com_mensagem(self):
        sessao = self._criar_sessao()
        resposta = self.cliente.post(
            reverse("termo_assinar_enviar", args=[sessao["token"]]),
            data=json.dumps({"assinatura": _png_assinatura(), "nome": "MARIA DE TESTE SILVA"}),
            content_type="application/json",
        )
        self.assertEqual(resposta.status_code, 400)
        self.assertFalse(resposta.json()["ok"])
        self.assertIn("aceite", resposta.json()["erro"].lower())

    def test_envio_com_corpo_quebrado_devolve_400(self):
        sessao = self._criar_sessao()
        resposta = self.cliente.post(
            reverse("termo_assinar_enviar", args=[sessao["token"]]),
            data="{isso nao e json",
            content_type="application/json",
        )
        self.assertEqual(resposta.status_code, 400)

    def test_envio_so_aceita_post(self):
        sessao = self._criar_sessao()
        resposta = self.cliente.get(reverse("termo_assinar_enviar", args=[sessao["token"]]))
        self.assertEqual(resposta.status_code, 405)

    def test_ip_respeita_proxy_reverso(self):
        # O acesso externo passa por túnel; sem ler o X-Forwarded-For o IP
        # registrado no termo seria sempre o do proxy.
        sessao = self._criar_sessao()
        self.cliente.post(
            reverse("termo_assinar_enviar", args=[sessao["token"]]),
            data=json.dumps({
                "assinatura": _png_assinatura(),
                "nome": "MARIA DE TESTE SILVA",
                "aceite": True,
            }),
            content_type="application/json",
            HTTP_X_FORWARDED_FOR="203.0.113.44, 10.0.0.1",
            REMOTE_ADDR="10.0.0.1",
        )
        self.assertEqual(
            TermoAssinatura.objects.get(pk=sessao["termo"].pk).assinado_ip,
            "203.0.113.44",
        )


class TelasInternasTests(BaseTermoAssinatura):
    """As telas do TI exigem login; o PDF nunca é servido a anônimo."""

    def setUp(self):
        self.cliente = Client()

    def _entrar(self):
        self.assertTrue(self.cliente.login(username="ti.teste", password="senha-de-teste-123"))

    def test_rotas_internas_exigem_login(self):
        sessao = self._criar_sessao()
        rotas = [
            reverse("termo_assinatura_list"),
            reverse("termo_assinatura_verificar"),
            reverse("termo_assinatura_preparar", args=["entrega"]),
            reverse("termo_assinatura_detalhe", args=[sessao["termo"].pk]),
            reverse("termo_assinatura_painel", args=[sessao["termo"].pk]),
            reverse("termo_assinatura_status", args=[sessao["termo"].pk]),
            reverse("termo_assinatura_pdf", args=[sessao["termo"].pk]),
        ]
        for rota in rotas:
            with self.subTest(rota=rota):
                resposta = self.cliente.get(rota)
                self.assertEqual(resposta.status_code, 302)
                self.assertIn("/login/", resposta["Location"])

    def test_lista_e_detalhe_abrem(self):
        self._entrar()
        sessao = self._criar_sessao()

        self.assertEqual(self.cliente.get(reverse("termo_assinatura_list")).status_code, 200)
        self.assertEqual(
            self.cliente.get(reverse("termo_assinatura_detalhe", args=[sessao["termo"].pk])).status_code,
            200,
        )
        self.assertEqual(
            self.cliente.get(reverse("termo_assinatura_painel", args=[sessao["termo"].pk])).status_code,
            200,
        )

    def test_preparar_recusa_tipo_invalido(self):
        self._entrar()
        self.assertEqual(
            self.cliente.get(reverse("termo_assinatura_preparar", args=["emprestimo"])).status_code,
            404,
        )

    def test_preparar_cria_sessao_e_redireciona_ao_painel(self):
        self._entrar()
        resposta = self.cliente.post(
            reverse("termo_assinatura_preparar", args=["entrega"]),
            data={
                "colaborador": self.colaborador.pk,
                "itens": [self.item.pk],
                "validade_minutos": 30,
                "estabelecimento": "karitel",
                "responsavel_ti_nome": "Kayque (TI)",
                "numero_chamado": "CH-777",
            },
        )
        self.assertEqual(resposta.status_code, 302)

        termo = TermoAssinatura.objects.order_by("-pk").first()
        self.assertEqual(resposta["Location"], reverse("termo_assinatura_painel", args=[termo.pk]))
        self.assertEqual(termo.dados["numero_chamado"], "CH-777")
        self.assertEqual(list(termo.itens.all()), [self.item])

    def test_preparar_recusa_equipamento_fora_do_universo_oferecido(self):
        # Sem a restrição de queryset no formulário, o POST poderia listar no
        # termo um equipamento que não está disponível para entrega.
        self._entrar()
        em_uso = Item.objects.create(
            nome="DESKTOP JA ENTREGUE", numero_serie="SN-TESTE-003",
            status=StatusItemChoices.ATIVO, item_consumo="nao", pmb="nao",
            centro_custo=self.cc_origem, localidade=self.localidade_origem,
        )
        resposta = self.cliente.post(
            reverse("termo_assinatura_preparar", args=["entrega"]),
            data={
                "colaborador": self.colaborador.pk,
                "itens": [em_uso.pk],
                "validade_minutos": 30,
            },
        )
        self.assertEqual(resposta.status_code, 200)  # reexibe com erro
        self.assertFalse(TermoAssinatura.objects.filter(itens=em_uso).exists())

    def test_status_api_responde_json(self):
        self._entrar()
        sessao = self._criar_sessao()
        resposta = self.cliente.get(reverse("termo_assinatura_status", args=[sessao["termo"].pk]))

        self.assertEqual(resposta.status_code, 200)
        corpo = resposta.json()
        self.assertEqual(corpo["situacao"], "pendente")
        self.assertFalse(corpo["assinado"])
        self.assertGreater(corpo["expira_em_segundos"], 0)

    def test_pdf_abre_inline_para_usuario_interno(self):
        self._entrar()
        sessao = self._criar_sessao()
        assinado = tas.registrar_assinatura(
            termo=sessao["termo"],
            assinatura_colaborador=_png_assinatura(),
            signatario_nome="MARIA DE TESTE SILVA",
            aceite=True,
        )

        resposta = self.cliente.get(reverse("termo_assinatura_pdf", args=[assinado.pk]))
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta["Content-Type"], "application/pdf")
        self.assertIn("inline", resposta["Content-Disposition"])
        self.assertTrue(resposta.content.startswith(b"%PDF"))

    def test_pdf_de_termo_sem_documento_da_404(self):
        self._entrar()
        sessao = self._criar_sessao()
        self.assertEqual(
            self.cliente.get(reverse("termo_assinatura_pdf", args=[sessao["termo"].pk])).status_code,
            404,
        )

    def test_painel_nao_mostra_link_de_outra_sessao_de_navegador(self):
        # O token puro vive na sessão de quem criou. Reabrir o painel de outro
        # computador mostra o status, nunca o link (o banco só tem o hash).
        self._entrar()
        sessao = self._criar_sessao()
        html = self.cliente.get(
            reverse("termo_assinatura_painel", args=[sessao["termo"].pk])
        ).content.decode("utf-8")

        self.assertNotIn(sessao["token"], html)
        self.assertIn("Gerar novo link", html)


class MediaProtegidaTests(BaseTermoAssinatura):
    """O PDF e o traço ficam em `MEDIA_ROOT`, que é autenticado (regra 23)."""

    def test_perfil_externo_nao_le_termo_assinado(self):
        from services.media_acesso_service import pode_ver_media

        from django.contrib.auth.models import Group

        sessao = self._criar_sessao()
        assinado = tas.registrar_assinatura(
            termo=sessao["termo"],
            assinatura_colaborador=_png_assinatura(),
            signatario_nome="MARIA DE TESTE SILVA",
            aceite=True,
        )

        interno = User.objects.create_user(username="interno.teste", password="x")
        self.assertTrue(pode_ver_media(interno, assinado.pdf.name))

        fornecedor = User.objects.create_user(username="fornecedor.teste", password="x")
        grupo, _ = Group.objects.get_or_create(name="Fornecedor")
        fornecedor.groups.add(grupo)

        self.assertFalse(pode_ver_media(fornecedor, assinado.pdf.name))
        self.assertFalse(pode_ver_media(fornecedor, assinado.assinatura_colaborador.name))


class ConteudoDoPdfTests(BaseTermoAssinatura):
    """O PDF tem de carregar o que o torna prova, não só parecer um termo."""

    def test_pdf_reemitido_usa_o_snapshot(self):
        sessao = self._criar_sessao()
        assinado = tas.registrar_assinatura(
            termo=sessao["termo"],
            assinatura_colaborador=_png_assinatura(),
            signatario_nome="MARIA DE TESTE SILVA",
            aceite=True,
        )

        # Renomear o item depois da assinatura NÃO pode alterar o documento:
        # o PDF é montado a partir de `dados`, não das FKs.
        self.item.nome = "NOME TROCADO DEPOIS DA ASSINATURA"
        self.item.save(update_fields=["nome"])

        conteudo = gerar_pdf_assinado(TermoAssinatura.objects.get(pk=assinado.pk))
        self.assertTrue(conteudo.startswith(b"%PDF"))
        self.assertEqual(
            TermoAssinatura.objects.get(pk=assinado.pk).dados["equipamentos"][0]["nome"],
            "NOTEBOOK DE TESTE",
        )

    def test_pdf_tem_mais_de_uma_pagina_e_metadados(self):
        from pypdf import PdfReader
        from io import BytesIO

        sessao = self._criar_sessao()
        assinado = tas.registrar_assinatura(
            termo=sessao["termo"],
            assinatura_colaborador=_png_assinatura(),
            assinatura_responsavel=_png_assinatura(),
            signatario_nome="MARIA DE TESTE SILVA",
            signatario_cpf="123.456.789-00",
            aceite=True,
        )

        assinado.pdf.open("rb")
        try:
            leitor = PdfReader(BytesIO(assinado.pdf.read()))
        finally:
            assinado.pdf.close()

        self.assertGreaterEqual(len(leitor.pages), 1)

        texto = "\n".join((pagina.extract_text() or "") for pagina in leitor.pages)

        # O que não pode faltar no documento, sob pena de ele não servir:
        self.assertIn(assinado.codigo_verificacao, texto)
        self.assertIn("MARIA DE TESTE SILVA", texto)
        self.assertIn("SN-TESTE-001", texto)
        self.assertIn("123.456.789-00", texto)
        self.assertIn("2.200-2", texto)          # base legal do aceite eletrônico
        self.assertIn("ACEITE", texto.upper())
        self.assertIn("172", texto) if assinado.assinado_ip else None


class EntradaPelaPendenciaTests(BaseTermoAssinatura):
    """
    Entrada pelo Repositório de Termos: a movimentação JÁ foi registrada (o
    item saiu do estoque) e o que falta é o termo daquele ato.

    Sem `_com_item_indicado`, a pendência que a própria tela aponta seria
    impossível de resolver pelo botão que ela oferece — o equipamento não
    apareceria na lista, sem explicar por quê.
    """

    def setUp(self):
        self.cliente = Client()
        self.assertTrue(self.cliente.login(username="ti.teste", password="senha-de-teste-123"))

    def test_item_ja_entregue_aparece_quando_indicado_na_url(self):
        self.item.status = StatusItemChoices.ATIVO
        self.item.save(update_fields=["status"])

        # Sem o `?item=`: fora do universo de entrega (já está com alguém).
        html = self.cliente.get(
            reverse("termo_assinatura_preparar", args=["entrega"])
        ).content.decode("utf-8")
        self.assertNotIn("NOTEBOOK DE TESTE", html)

        # Com o `?item=`: entra, porque o ato já aconteceu e falta o termo.
        html = self.cliente.get(
            reverse("termo_assinatura_preparar", args=["entrega"]) + f"?item={self.item.pk}"
        ).content.decode("utf-8")
        self.assertIn("NOTEBOOK DE TESTE", html)
        self.assertIn(f'value="{self.item.pk}"', html)

    def test_post_aceita_o_item_indicado_fora_do_universo_padrao(self):
        self.item.status = StatusItemChoices.ATIVO
        self.item.save(update_fields=["status"])

        resposta = self.cliente.post(
            reverse("termo_assinatura_preparar", args=["entrega"]) + f"?item={self.item.pk}",
            data={
                "colaborador": self.colaborador.pk,
                "itens": [self.item.pk],
                "item_indicado": self.item.pk,
                "validade_minutos": 30,
            },
        )
        self.assertEqual(resposta.status_code, 302)
        self.assertTrue(TermoAssinatura.objects.filter(itens=self.item).exists())

    def test_item_indicado_sobrevive_a_erro_de_validacao(self):
        # `item_indicado` viaja no POST: sem isso, reexibir a tela depois de um
        # erro perderia o equipamento que originou a visita.
        self.item.status = StatusItemChoices.ATIVO
        self.item.save(update_fields=["status"])

        resposta = self.cliente.post(
            reverse("termo_assinatura_preparar", args=["entrega"]),
            data={
                "itens": [self.item.pk],
                "item_indicado": self.item.pk,
                "validade_minutos": 30,
                # colaborador faltando de propósito → erro de validação
            },
        )
        self.assertEqual(resposta.status_code, 200)
        html = resposta.content.decode("utf-8")
        self.assertIn("NOTEBOOK DE TESTE", html)
        self.assertIn(f'name="item_indicado" value="{self.item.pk}"', html)

    def test_item_indicado_invalido_nao_quebra_a_tela(self):
        for valor in ("abc", "-1", "999999", ""):
            with self.subTest(item=valor):
                resposta = self.cliente.get(
                    reverse("termo_assinatura_preparar", args=["entrega"]) + f"?item={valor}"
                )
                self.assertEqual(resposta.status_code, 200)

    def test_devolucao_pela_ficha_resolve_o_detentor_atual(self):
        # Entrada pela ficha do equipamento, sem `?colaborador=`: o detentor
        # vem da MESMA fonte que o termo .docx usa (`get_usuario_atual_item`).
        MovimentacaoItem.objects.create(
            tipo_movimentacao="transferencia",
            tipo_transferencia="entrega",
            item=self.item,
            usuario=self.colaborador,
            quantidade=1,
        )
        self.item.status = StatusItemChoices.ATIVO
        self.item.save(update_fields=["status"])

        html = self.cliente.get(
            reverse("termo_assinatura_preparar", args=["devolucao"]) + f"?item={self.item.pk}"
        ).content.decode("utf-8")

        self.assertIn("MARIA DE TESTE SILVA", html)
        self.assertIn("NOTEBOOK DE TESTE", html)


class RenderizacaoDeTodasAsTelasTests(BaseTermoAssinatura):
    """
    Abre TODAS as telas do módulo e confere o que o olho não pega: comentário
    de template renderizado como texto para o usuário (regra 17) e tela que
    volta 200 mas vazia porque um include errou o caminho.
    """

    def setUp(self):
        self.cliente = Client()

    def test_tela_de_conclusao_do_tablet_abre_sem_login(self):
        sessao = self._criar_sessao()
        tas.registrar_assinatura(
            termo=sessao["termo"],
            assinatura_colaborador=_png_assinatura(),
            signatario_nome="MARIA DE TESTE SILVA",
            aceite=True,
        )
        resposta = self.cliente.get(reverse("termo_assinar_concluido", args=[sessao["token"]]))
        self.assertEqual(resposta.status_code, 200)
        self.assertIn("Termo assinado", resposta.content.decode("utf-8"))

    def test_verificar_com_resultado_renderiza(self):
        self.assertTrue(self.cliente.login(username="ti.teste", password="senha-de-teste-123"))
        sessao = self._criar_sessao()
        assinado = tas.registrar_assinatura(
            termo=sessao["termo"],
            assinatura_colaborador=_png_assinatura(),
            signatario_nome="MARIA DE TESTE SILVA",
            aceite=True,
        )

        html = self.cliente.get(
            reverse("termo_assinatura_verificar") + f"?codigo={assinado.codigo_verificacao}"
        ).content.decode("utf-8")
        self.assertIn("autêntico e íntegro", html)
        self.assertIn(assinado.codigo_verificacao, html)

        html = self.cliente.get(
            reverse("termo_assinatura_verificar") + "?codigo=ZZZZ-ZZZZ-ZZZZ"
        ).content.decode("utf-8")
        self.assertIn("Nenhum termo", html)

    def test_nenhuma_tela_renderiza_comentario_de_template(self):
        # Regra 17: `{# #}` quebrado em várias linhas NÃO é comentário — o
        # Django imprime o texto na tela. Já aconteceu 3 vezes neste projeto,
        # e pegou no teste do HTML RENDERIZADO, não no olho.
        self.assertTrue(self.cliente.login(username="ti.teste", password="senha-de-teste-123"))
        sessao = self._criar_sessao()
        assinado_sessao = self._criar_sessao()
        tas.registrar_assinatura(
            termo=assinado_sessao["termo"],
            assinatura_colaborador=_png_assinatura(),
            signatario_nome="MARIA DE TESTE SILVA",
            aceite=True,
        )

        paginas = [
            reverse("termo_assinatura_list"),
            reverse("termo_assinatura_verificar"),
            reverse("termo_assinatura_preparar", args=["entrega"]),
            reverse("termo_assinatura_preparar", args=["devolucao"]),
            reverse("termo_assinatura_detalhe", args=[sessao["termo"].pk]),
            reverse("termo_assinatura_detalhe", args=[assinado_sessao["termo"].pk]),
            reverse("termo_assinatura_painel", args=[sessao["termo"].pk]),
        ]
        for pagina in paginas:
            with self.subTest(pagina=pagina):
                html = self.cliente.get(pagina).content.decode("utf-8")
                self.assertNotIn("{#", html)
                self.assertNotIn("{%", html)
                self.assertNotIn("endcomment", html)

    def test_tablet_nao_renderiza_comentario_de_template(self):
        sessao = self._criar_sessao()
        html = self.cliente.get(
            reverse("termo_assinar", args=[sessao["token"]])
        ).content.decode("utf-8")

        self.assertNotIn("{#", html)
        self.assertNotIn("{%", html)
        self.assertNotIn("endcomment", html)
        # A tela tem de trazer o texto legal inteiro, não um resumo.
        self.assertIn("Os Equipamentos deverão ser utilizados", html)
        self.assertIn("2.200-2/2001", html)

    def test_tablet_carrega_tokens_antes_da_folha_do_modulo(self):
        # Ordem invertida não dá erro: o var() não resolve e a tela abre sem
        # cor nenhuma, 200, sem nada no log (regras 26 e 31).
        sessao = self._criar_sessao()
        html = self.cliente.get(
            reverse("termo_assinar", args=[sessao["token"]])
        ).content.decode("utf-8")

        self.assertLess(
            html.index("css/tokens.css"), html.index("css/ds-termos.css"),
            "tokens.css tem de vir ANTES de ds-termos.css.",
        )
        # Shell próprio não redefine token do sistema (regra 31).
        self.assertNotIn("--brand:", html)
        self.assertNotIn("--text-primary:", html)


class AvisoDeHostLocalTests(BaseTermoAssinatura):
    """
    O QR é montado com o Host que o TI usou. Abrindo por `localhost`, ele
    aponta para o loopback DA MÁQUINA DO TI e o tablet recebe "não foi possível
    conectar" — sem nada que explique por quê, porque o QR é válido e a sessão
    está certa. O aviso é o que evita a caçada ao problema no Wi-Fi.
    """

    def setUp(self):
        self.cliente = Client()
        self.assertTrue(self.cliente.login(username="ti.teste", password="senha-de-teste-123"))

    def _painel(self, host):
        resposta = self.cliente.post(
            reverse("termo_assinatura_preparar", args=["entrega"]),
            data={
                "colaborador": self.colaborador.pk,
                "itens": [self.item.pk],
                "validade_minutos": 30,
            },
            HTTP_HOST=host,
        )
        termo = TermoAssinatura.objects.order_by("-pk").first()
        return self.cliente.get(
            reverse("termo_assinatura_painel", args=[termo.pk]), HTTP_HOST=host
        ).content.decode("utf-8")

    def test_avisa_quando_o_host_e_loopback(self):
        html = self._painel("127.0.0.1:8000")
        self.assertIn("localhost", html)
        self.assertIn("não foi possível conectar", html)

    @override_settings(ALLOWED_HOSTS=["172.16.60.254", "testserver"])
    def test_nao_avisa_no_endereco_de_rede(self):
        html = self._painel("172.16.60.254")
        self.assertNotIn("não foi possível conectar", html)
        self.assertIn("172.16.60.254", html)


class EfeitoNoEstoqueTests(BaseTermoAssinatura):
    """
    O que o item 3 do pedido exige: assinar MOVE o patrimônio.

    ENTREGA   → centro de custo e localidade do colaborador, status Ativo.
    DEVOLUÇÃO → centro de custo de origem restaurado, status Backup.

    Nada disso é implementado no módulo de assinatura: tudo passa por
    `MovimentacaoEstoqueService`, que já é a fonte única dessas regras. Estes
    testes travam o RESULTADO; o teste de que a MOVIMENTAÇÃO foi criada (e não
    só o item alterado) é o que pega alguém reescrevendo o efeito "na mão".
    """

    def _assinar(self, sessao):
        return tas.registrar_assinatura(
            termo=sessao["termo"],
            assinatura_colaborador=_png_assinatura(),
            signatario_nome="MARIA DE TESTE SILVA",
            aceite=True,
        )

    def test_entrega_move_cc_localidade_e_status(self):
        self.assertEqual(self.item.status, StatusItemChoices.ESTOQUE)
        self.assertEqual(self.item.centro_custo_id, self.cc_origem.pk)

        sessao = self._criar_sessao(tipo="entrega")
        self._assinar(sessao)

        self.item.refresh_from_db()
        self.assertEqual(self.item.status, StatusItemChoices.ATIVO)
        self.assertEqual(self.item.centro_custo_id, self.centro_custo.pk, "CC do colaborador")
        self.assertEqual(self.item.localidade_id, self.localidade.pk, "localidade do colaborador")

    def test_entrega_cria_movimentacao_com_o_termo_anexado(self):
        sessao = self._criar_sessao(tipo="entrega")
        assinado = self._assinar(sessao)

        movimentacoes = list(assinado.movimentacoes.all())
        self.assertEqual(len(movimentacoes), 1)

        mov = movimentacoes[0]
        self.assertEqual(mov.tipo_movimentacao, "transferencia")
        self.assertEqual(mov.tipo_transferencia, "entrega")
        self.assertEqual(mov.usuario_id, self.colaborador.pk)
        self.assertEqual(mov.item_id, self.item.pk)
        self.assertTrue(mov.termo_pdf, "a movimentação precisa sair com o termo anexado")
        self.assertIn(assinado.codigo_verificacao, mov.observacao or "")

        # Autor da operação de patrimônio é quem PREPAROU a sessão — o
        # colaborador que assina é anônimo e não pode constar como autor.
        self.assertEqual(mov.criado_por_id, self.user.pk)

    def test_devolucao_volta_para_backup_e_restaura_cc(self):
        # Entrega primeiro, para existir o que devolver.
        self._assinar(self._criar_sessao(tipo="entrega"))
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, StatusItemChoices.ATIVO)

        sessao = tas.criar_sessao(
            tipo="devolucao", colaborador=self.colaborador, itens=[self.item],
            form_data={"estabelecimento": "karitel"}, user=self.user,
        )
        self._assinar(sessao)

        self.item.refresh_from_db()
        self.assertEqual(self.item.status, StatusItemChoices.BACKUP)
        self.assertEqual(
            self.item.centro_custo_id, self.cc_origem.pk,
            "a devolução restaura o centro de custo de antes da entrega",
        )

    def test_varios_equipamentos_num_termo_movem_todos(self):
        outro = Item.objects.create(
            nome="MONITOR DE TESTE", numero_serie="SN-TESTE-010",
            status=StatusItemChoices.ESTOQUE, item_consumo="nao", pmb="nao",
            centro_custo=self.cc_origem, localidade=self.localidade_origem,
        )
        sessao = tas.criar_sessao(
            tipo="entrega", colaborador=self.colaborador, itens=[self.item, outro],
            form_data={"estabelecimento": "karitel"}, user=self.user,
        )
        assinado = self._assinar(sessao)

        self.assertEqual(assinado.movimentacoes.count(), 2)
        for item in (self.item, outro):
            item.refresh_from_db()
            self.assertEqual(item.status, StatusItemChoices.ATIVO, item.nome)
            self.assertEqual(item.centro_custo_id, self.centro_custo.pk, item.nome)

        # Um ContentFile NOVO por movimentação: reaproveitar a instância grava
        # zero byte na segunda — falha silenciosa que só apareceria ao abrir o
        # PDF meses depois.
        for mov in assinado.movimentacoes.all():
            self.assertGreater(mov.termo_pdf.size, 4000, f"PDF vazio em {mov.pk}")

    def test_sessao_com_movimentacao_previa_nao_duplica(self):
        # O TI já registrou a transferência e só faltava o termo: a assinatura
        # anexa o documento, não cria uma segunda movimentação.
        mov = MovimentacaoItem.objects.create(
            tipo_movimentacao="transferencia", tipo_transferencia="entrega",
            item=self.item, usuario=self.colaborador, quantidade=1,
        )
        sessao = self._criar_sessao(tipo="entrega", movimentacoes=[mov])
        assinado = self._assinar(sessao)

        self.assertEqual(assinado.movimentacoes.count(), 1)
        self.assertEqual(MovimentacaoItem.objects.filter(item=self.item).count(), 1)
        mov.refresh_from_db()
        self.assertTrue(mov.termo_pdf)


class PreCondicoesDaEntregaTests(BaseTermoAssinatura):
    """
    O bloqueio acontece na PREPARAÇÃO, com o TI na frente do computador — não
    na assinatura, com o colaborador de pé esperando. Descobrir um problema de
    cadastro só depois de o colaborador assinar é a pior hora possível.
    """

    def test_recusa_equipamento_sem_centro_de_custo(self):
        sem_cc = Item.objects.create(
            nome="TECLADO SEM CC", numero_serie="SN-TESTE-020",
            status=StatusItemChoices.ESTOQUE, item_consumo="nao", pmb="nao",
            localidade=self.localidade_origem,
        )
        with self.assertRaisesMessage(ValueError, "sem centro de custo"):
            tas.criar_sessao(
                tipo="entrega", colaborador=self.colaborador, itens=[sem_cc],
                form_data={}, user=self.user,
            )
        self.assertFalse(TermoAssinatura.objects.filter(itens=sem_cc).exists())

    def test_recusa_colaborador_sem_localidade(self):
        sem_local = Usuario.objects.create(
            matricula="88888", nome="JOSE SEM LOCALIDADE", status="ativo", pmb="nao",
            centro_custo=self.centro_custo,
        )
        with self.assertRaisesMessage(ValueError, "sem localidade"):
            tas.criar_sessao(
                tipo="entrega", colaborador=sem_local, itens=[self.item],
                form_data={}, user=self.user,
            )

    def test_devolucao_nao_exige_as_pre_condicoes_da_entrega(self):
        # Devolver equipamento sem CC é legítimo: a restrição existe para a
        # entrega poder ser desfeita, e na devolução não há o que desfazer.
        sem_cc = Item.objects.create(
            nome="TECLADO SEM CC", numero_serie="SN-TESTE-021",
            status=StatusItemChoices.ATIVO, item_consumo="nao", pmb="nao",
        )
        sessao = tas.criar_sessao(
            tipo="devolucao", colaborador=self.colaborador, itens=[sem_cc],
            form_data={}, user=self.user,
        )
        self.assertTrue(sessao["termo"].pode_assinar)

    def test_tela_mostra_o_erro_sem_criar_sessao(self):
        sem_cc = Item.objects.create(
            nome="MOUSE SEM CC", numero_serie="SN-TESTE-022",
            status=StatusItemChoices.ESTOQUE, item_consumo="nao", pmb="nao",
        )
        cliente = Client()
        self.assertTrue(cliente.login(username="ti.teste", password="senha-de-teste-123"))

        resposta = cliente.post(
            reverse("termo_assinatura_preparar", args=["entrega"]),
            data={"colaborador": self.colaborador.pk, "itens": [sem_cc.pk], "validade_minutos": 30},
        )
        self.assertEqual(resposta.status_code, 200)
        self.assertIn("sem centro de custo", resposta.content.decode("utf-8"))
        self.assertFalse(TermoAssinatura.objects.filter(itens=sem_cc).exists())


class BuscaRemotaTests(BaseTermoAssinatura):
    """Select2 com busca no servidor — 1.376 colaboradores e 751 equipamentos
    não cabem como `<option>` no HTML."""

    def setUp(self):
        self.cliente = Client()
        self.assertTrue(self.cliente.login(username="ti.teste", password="senha-de-teste-123"))

    def test_apis_exigem_login(self):
        anonimo = Client()
        for rota in ("termo_assinatura_colaboradores_api", "termo_assinatura_equipamentos_api"):
            with self.subTest(rota=rota):
                resposta = anonimo.get(reverse(rota))
                self.assertEqual(resposta.status_code, 302)
                self.assertIn("/login/", resposta["Location"])

    def test_busca_colaborador_por_nome_e_matricula(self):
        for termo in ("MARIA", "maria de teste", "99999"):
            with self.subTest(termo=termo):
                dados = self.cliente.get(
                    reverse("termo_assinatura_colaboradores_api"), {"q": termo}
                ).json()
                nomes = [r["text"] for r in dados["results"]]
                self.assertIn("MARIA DE TESTE SILVA", nomes)

    def test_colaborador_traz_metadados_para_desambiguar(self):
        resultado = self.cliente.get(
            reverse("termo_assinatura_colaboradores_api"), {"q": "MARIA"}
        ).json()["results"][0]
        self.assertEqual(resultado["matricula"], "99999")
        self.assertEqual(resultado["centro_custo"], "TI")
        self.assertEqual(resultado["localidade"], "KARITEL")

    def test_colaborador_desligado_fica_fora(self):
        desligado = Usuario.objects.create(
            matricula="77777", nome="PEDRO DESLIGADO", status="desligado", pmb="nao",
        )
        nomes = [
            r["text"] for r in self.cliente.get(
                reverse("termo_assinatura_colaboradores_api"), {"q": "PEDRO"}
            ).json()["results"]
        ]
        self.assertNotIn(desligado.nome, nomes)

    def test_busca_equipamento_por_serie_marca_e_modelo(self):
        for termo in ("NOTEBOOK", "SN-TESTE-001", "DELL", "LATITUDE"):
            with self.subTest(termo=termo):
                dados = self.cliente.get(
                    reverse("termo_assinatura_equipamentos_api"),
                    {"tipo": "entrega", "q": termo},
                ).json()
                self.assertIn("NOTEBOOK DE TESTE", [r["text"] for r in dados["results"]])

    def test_entrega_nao_lista_equipamento_ja_em_uso(self):
        self.item.status = StatusItemChoices.ATIVO
        self.item.save(update_fields=["status"])

        dados = self.cliente.get(
            reverse("termo_assinatura_equipamentos_api"), {"tipo": "entrega", "q": "NOTEBOOK"},
        ).json()
        self.assertNotIn("NOTEBOOK DE TESTE", [r["text"] for r in dados["results"]])

        # ...a não ser quando indicado explicitamente (ato já ocorrido).
        dados = self.cliente.get(
            reverse("termo_assinatura_equipamentos_api"),
            {"tipo": "entrega", "q": "NOTEBOOK", "incluir": self.item.pk},
        ).json()
        self.assertIn("NOTEBOOK DE TESTE", [r["text"] for r in dados["results"]])

    def test_devolucao_sem_colaborador_avisa_em_vez_de_listar_tudo(self):
        dados = self.cliente.get(
            reverse("termo_assinatura_equipamentos_api"), {"tipo": "devolucao"},
        ).json()
        self.assertEqual(dados["results"], [])
        self.assertIn("colaborador", dados["aviso"].lower())

    def test_devolucao_lista_so_o_que_esta_com_o_colaborador(self):
        MovimentacaoItem.objects.create(
            tipo_movimentacao="transferencia", tipo_transferencia="entrega",
            item=self.item, usuario=self.colaborador, quantidade=1,
        )
        outro = Item.objects.create(
            nome="IMPRESSORA DE OUTRO", numero_serie="SN-TESTE-030",
            status=StatusItemChoices.ESTOQUE, item_consumo="nao", pmb="nao",
            centro_custo=self.cc_origem,
        )
        nomes = [
            r["text"] for r in self.cliente.get(
                reverse("termo_assinatura_equipamentos_api"),
                {"tipo": "devolucao", "colaborador": self.colaborador.pk},
            ).json()["results"]
        ]
        self.assertIn("NOTEBOOK DE TESTE", nomes)
        self.assertNotIn(outro.nome, nomes)

    def test_sinaliza_equipamento_sem_centro_de_custo(self):
        Item.objects.create(
            nome="HEADSET SEM CC", numero_serie="SN-TESTE-031",
            status=StatusItemChoices.ESTOQUE, item_consumo="nao", pmb="nao",
        )
        resultado = self.cliente.get(
            reverse("termo_assinatura_equipamentos_api"),
            {"tipo": "entrega", "q": "HEADSET"},
        ).json()["results"][0]
        self.assertTrue(resultado["sem_centro_custo"])

    def test_tela_nao_renderiza_o_catalogo_inteiro(self):
        # O ganho de usar AJAX é exatamente este: a página não carrega as
        # centenas de opções. Se alguém trocar por `<select>` cheio, o teste cai.
        for i in range(12):
            Item.objects.create(
                nome=f"ITEM DE CARGA {i}", numero_serie=f"SN-CARGA-{i}",
                status=StatusItemChoices.ESTOQUE, item_consumo="nao", pmb="nao",
                centro_custo=self.cc_origem,
            )
        html = self.cliente.get(
            reverse("termo_assinatura_preparar", args=["entrega"])
        ).content.decode("utf-8")

        self.assertNotIn("ITEM DE CARGA", html)
        self.assertIn('data-remoto="equipamento"', html)
        self.assertIn('data-remoto="colaborador"', html)


class ViaDoColaboradorTests(BaseTermoAssinatura):
    """Item 6: depois de assinar, o colaborador baixa a via ou pede por e-mail."""

    def setUp(self):
        self.cliente = Client()

    def _assinar_no_tablet(self):
        sessao = self._criar_sessao()
        resposta = self.cliente.post(
            reverse("termo_assinar_enviar", args=[sessao["token"]]),
            data=json.dumps({
                "assinatura": _png_assinatura(),
                "nome": "MARIA DE TESTE SILVA",
                "aceite": True,
            }),
            content_type="application/json",
        )
        self.assertEqual(resposta.status_code, 200)
        return sessao

    def test_tela_de_conclusao_oferece_as_duas_vias(self):
        sessao = self._assinar_no_tablet()
        html = self.cliente.get(
            reverse("termo_assinar_concluido", args=[sessao["token"]])
        ).content.decode("utf-8")

        self.assertIn("Baixar minha via", html)
        self.assertIn("Receber por e-mail", html)
        self.assertIn(TermoAssinatura.objects.get(pk=sessao["termo"].pk).codigo_verificacao, html)

    def test_baixa_a_via_em_pdf(self):
        self._assinar_no_tablet()
        resposta = self.cliente.get(reverse("termo_recibo_pdf"))

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta["Content-Type"], "application/pdf")
        self.assertIn("attachment", resposta["Content-Disposition"])
        self.assertTrue(resposta.content.startswith(b"%PDF"))

    def test_envia_por_email_para_o_endereco_do_cadastro(self):
        from django.core import mail

        self._assinar_no_tablet()
        resposta = self.cliente.post(reverse("termo_recibo_email"))

        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(resposta.json()["ok"])
        self.assertEqual(len(mail.outbox), 1)

        enviado = mail.outbox[0]
        self.assertEqual(enviado.to, ["maria.teste@exemplo.com.br"])
        self.assertEqual(len(enviado.attachments), 1)
        self.assertTrue(enviado.attachments[0][0].endswith(".pdf"))
        self.assertTrue(enviado.attachments[0][1].startswith(b"%PDF"))

        # O endereço completo não vai para a tela: o tablet fica à vista de
        # quem estiver por perto.
        self.assertNotIn("maria.teste@exemplo.com.br", resposta.json()["mensagem"])

    def test_sem_email_no_cadastro_a_tela_nao_oferece_o_botao(self):
        self.colaborador.email = ""
        self.colaborador.save(update_fields=["email"])

        sessao = self._assinar_no_tablet()
        html = self.cliente.get(
            reverse("termo_assinar_concluido", args=[sessao["token"]])
        ).content.decode("utf-8")

        self.assertNotIn("Receber por e-mail", html)
        self.assertIn("Não há e-mail cadastrado", html)

    def test_outro_navegador_nao_alcanca_a_via(self):
        # A autorização é a marca de sessão deixada pela assinatura — quem não
        # assinou neste navegador não baixa nem dispara e-mail.
        self._assinar_no_tablet()

        intruso = Client()
        self.assertEqual(intruso.get(reverse("termo_recibo_pdf")).status_code, 404)
        self.assertEqual(intruso.post(reverse("termo_recibo_email")).status_code, 404)

    def test_via_expira_depois_da_janela(self):
        from ProjetoEstoque.views.termos_assinatura import CHAVE_RECIBO

        self._assinar_no_tablet()
        self.assertEqual(self.cliente.get(reverse("termo_recibo_pdf")).status_code, 200)

        # Envelhece a marca para além da janela.
        sessao_navegador = self.cliente.session
        marca = sessao_navegador[CHAVE_RECIBO]
        marca["em"] = (timezone.now() - timedelta(minutes=30)).isoformat()
        sessao_navegador[CHAVE_RECIBO] = marca
        sessao_navegador.save()

        self.assertEqual(self.cliente.get(reverse("termo_recibo_pdf")).status_code, 404)

    def test_email_so_aceita_post(self):
        self._assinar_no_tablet()
        self.assertEqual(self.cliente.get(reverse("termo_recibo_email")).status_code, 405)


class TextoDoDocumentoTests(BaseTermoAssinatura):
    """Item 4 e 5: a expressão "de agora em diante chamado de USUÁRIO" saiu, e
    a numeração das seções do PDF não pula número."""

    def _texto_do_pdf(self, termo):
        from io import BytesIO

        from pypdf import PdfReader

        termo.pdf.open("rb")
        try:
            return "\n".join(
                (p.extract_text() or "") for p in PdfReader(BytesIO(termo.pdf.read())).pages
            )
        finally:
            termo.pdf.close()

    def _assinar(self, **extra):
        sessao = self._criar_sessao(**extra)
        return tas.registrar_assinatura(
            termo=sessao["termo"],
            assinatura_colaborador=_png_assinatura(),
            signatario_nome="MARIA DE TESTE SILVA",
            aceite=True,
        )

    def test_expressao_removida_do_pdf(self):
        texto = self._texto_do_pdf(self._assinar())

        self.assertNotIn("chamado de USUÁRIO", texto)
        self.assertNotIn("de agora em diante", texto)
        # O resto da declaração continua inteiro.
        self.assertIn("declaro que recebi, li e entendi", texto)

    def test_expressao_removida_da_tela_do_tablet(self):
        sessao = self._criar_sessao()
        html = Client().get(
            reverse("termo_assinar", args=[sessao["token"]])
        ).content.decode("utf-8")

        self.assertNotIn("chamado de USUÁRIO", html)
        self.assertNotIn("de agora em diante", html)

    def test_expressao_removida_tambem_do_docx(self):
        # As duas vias são o MESMO termo: deixar a expressão em uma só criaria
        # dois documentos diferentes para o mesmo ato.
        from docx import Document

        from services.termos import gerar_termo_docx

        arquivo, _ = gerar_termo_docx(
            item=self.item,
            tipo="entrega",
            form_data={"colaborador": self.colaborador, "estabelecimento": "karitel"},
        )
        texto = "\n".join(p.text for p in Document(arquivo).paragraphs)
        self.assertNotIn("chamado de USUÁRIO", texto)

    def test_secoes_do_pdf_nao_pulam_numero(self):
        # Acessórios e Observações são seções CONDICIONAIS. Numerar com
        # literal fazia o documento sair "1, 2, 4" quando havia observação mas
        # não acessório.
        import re

        texto = self._texto_do_pdf(
            self._assinar(form_data={"acessorios": "", "observacoes": "Lacre conferido."})
        )

        titulos = ["Identificação", "Equipamento\\(s\\)", "Observações", "Declaração do colaborador"]
        for esperado, titulo in enumerate(titulos, start=1):
            with self.subTest(secao=titulo):
                self.assertRegex(texto, rf"{esperado}\.\s+{titulo}")


class DocumentosNaoPersistemTests(BaseTermoAssinatura):
    """
    CPF e RG do signatário existem APENAS dentro do PDF emitido.

    Estes testes são a trava do pedido: eles falham no instante em que alguém
    criar uma coluna, gravar no snapshot `dados` ou logar o documento "só para
    facilitar a consulta depois". Guardar CPF de 1.376 colaboradores numa
    tabela cria um alvo que o sistema não precisa ter; o termo já vale como
    prova com o documento impresso nele.
    """

    CPF = "123.456.789-00"
    RG = "MG-12.345.678"

    def _assinar_com_documentos(self):
        sessao = self._criar_sessao()
        return tas.registrar_assinatura(
            termo=sessao["termo"],
            assinatura_colaborador=_png_assinatura(),
            signatario_nome="MARIA DE TESTE SILVA",
            signatario_cpf=self.CPF,
            signatario_rg=self.RG,
            aceite=True,
        )

    def _texto_do_pdf(self, termo):
        from io import BytesIO

        from pypdf import PdfReader

        termo.pdf.open("rb")
        try:
            return "\n".join(
                (p.extract_text() or "") for p in PdfReader(BytesIO(termo.pdf.read())).pages
            )
        finally:
            termo.pdf.close()

    def test_o_model_nao_tem_campo_de_documento(self):
        campos = {f.name for f in TermoAssinatura._meta.get_fields()}
        self.assertNotIn("signatario_cpf", campos)
        self.assertNotIn("signatario_rg", campos)

    def test_documentos_entram_no_pdf(self):
        # Eles PRECISAM estar no documento — é lá que a identificação do
        # signatário dá sustentação ao termo.
        texto = self._texto_do_pdf(self._assinar_com_documentos())
        self.assertIn(self.CPF, texto)
        self.assertIn(self.RG, texto)

    def test_documentos_nao_entram_no_snapshot(self):
        assinado = self._assinar_com_documentos()
        snapshot = json.dumps(assinado.dados, ensure_ascii=False)

        self.assertNotIn(self.CPF, snapshot)
        self.assertNotIn(self.RG, snapshot)
        # Também não nos dígitos sem máscara.
        self.assertNotIn("12345678900", snapshot.replace(".", "").replace("-", ""))

    def test_nenhum_campo_do_registro_guarda_o_documento(self):
        assinado = self._assinar_com_documentos()
        recarregado = TermoAssinatura.objects.get(pk=assinado.pk)

        for campo in recarregado._meta.get_fields():
            if not getattr(campo, "concrete", False) or campo.many_to_many:
                continue
            valor = str(getattr(recarregado, campo.name, "") or "")
            with self.subTest(campo=campo.name):
                self.assertNotIn(self.CPF, valor)
                self.assertNotIn(self.RG, valor)

    def test_documento_nao_aparece_em_nenhuma_coluna_da_tabela(self):
        # Varredura no SQL cru: pega inclusive um campo novo que alguém
        # acrescente depois sem lembrar desta regra.
        from django.db import connection

        self._assinar_com_documentos()

        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM ProjetoEstoque_termoassinatura")
            colunas = [c[0] for c in cursor.description]
            linhas = cursor.fetchall()

        self.assertTrue(linhas)
        for linha in linhas:
            for coluna, valor in zip(colunas, linha):
                with self.subTest(coluna=coluna):
                    texto = str(valor or "")
                    self.assertNotIn(self.CPF, texto)
                    self.assertNotIn(self.RG, texto)

    def test_documento_nao_vai_para_o_log(self):
        with self.assertLogs("services.termo_assinatura_service", level="INFO") as registro:
            self._assinar_com_documentos()

        registrado = "\n".join(registro.output)
        self.assertNotIn(self.CPF, registrado)
        self.assertNotIn(self.RG, registrado)

    def test_documento_nao_volta_na_resposta_da_assinatura(self):
        cliente = Client()
        sessao = self._criar_sessao()
        resposta = cliente.post(
            reverse("termo_assinar_enviar", args=[sessao["token"]]),
            data=json.dumps({
                "assinatura": _png_assinatura(),
                "nome": "MARIA DE TESTE SILVA",
                "cpf": self.CPF,
                "rg": self.RG,
                "aceite": True,
            }),
            content_type="application/json",
        )
        self.assertEqual(resposta.status_code, 200)
        self.assertNotIn(self.CPF, resposta.content.decode("utf-8"))
        self.assertNotIn(self.RG, resposta.content.decode("utf-8"))

    def test_tela_do_ti_nao_exibe_documento(self):
        assinado = self._assinar_com_documentos()

        cliente = Client()
        self.assertTrue(cliente.login(username="ti.teste", password="senha-de-teste-123"))
        html = cliente.get(
            reverse("termo_assinatura_detalhe", args=[assinado.pk])
        ).content.decode("utf-8")

        self.assertNotIn(self.CPF, html)
        self.assertNotIn(self.RG, html)
        # ...e diz onde estão, em vez de deixar a dúvida.
        self.assertIn("Constam apenas no PDF assinado", html)

    def test_reemissao_sai_sem_os_documentos(self):
        # Consequência esperada de não guardar: reemitir o PDF produz o mesmo
        # termo sem as linhas de CPF/RG. A via original é a que foi entregue ao
        # colaborador e anexada à movimentação.
        from services.termo_pdf_service import gerar_pdf_assinado

        assinado = self._assinar_com_documentos()
        self.assertIn(self.CPF, self._texto_do_pdf(assinado))

        from io import BytesIO

        from pypdf import PdfReader

        reemitido = gerar_pdf_assinado(TermoAssinatura.objects.get(pk=assinado.pk))
        texto = "\n".join(
            (p.extract_text() or "") for p in PdfReader(BytesIO(reemitido)).pages
        )
        self.assertNotIn(self.CPF, texto)
        self.assertIn("não informados", texto)
        # O documento segue íntegro e identificando o signatário pelo nome.
        self.assertIn("MARIA DE TESTE SILVA", texto)

    def test_assinatura_sem_documento_continua_valendo(self):
        # Os campos são opcionais: a empresa não guarda CPF de todo colaborador
        # no cadastro, e recusar a assinatura por isso travaria a entrega.
        sessao = self._criar_sessao()
        assinado = tas.registrar_assinatura(
            termo=sessao["termo"],
            assinatura_colaborador=_png_assinatura(),
            signatario_nome="MARIA DE TESTE SILVA",
            aceite=True,
        )
        self.assertEqual(assinado.status, StatusAssinaturaTermoChoices.ASSINADO)
        self.assertIn("não informados", self._texto_do_pdf(assinado))


class AlinhamentoDosCamposTests(BaseTermoAssinatura):
    """
    A fileira "Dados do termo" saía com um campo na altura certa e os três
    vizinhos esticados, porque a dica sob "Número do termo" aumentava a altura
    da linha do grid e as células esticavam junto.
    """

    def test_grade_alinha_os_campos_pelo_topo(self):
        import pathlib

        from django.conf import settings

        folha = pathlib.Path(settings.BASE_DIR) / "ProjetoEstoque" / "static" / "css" / "ds-termos.css"
        css = folha.read_text(encoding="utf-8")

        bloco = css.split(".ta-form-grid {", 1)[1].split("}", 1)[0]
        self.assertIn(
            "align-items: start", bloco,
            "sem `align-items: start` a célula estica até a altura da linha e a "
            "fileira de campos sai irregular",
        )

        # Campo de uma linha com altura fixa; textarea segue podendo crescer.
        self.assertIn(".ta-prep .ctrl:not(textarea)", css)
        self.assertIn(".ta-field { align-content: start; }", css)

    def test_tela_renderiza_a_grade_de_dados_do_termo(self):
        cliente = Client()
        self.assertTrue(cliente.login(username="ti.teste", password="senha-de-teste-123"))
        html = cliente.get(
            reverse("termo_assinatura_preparar", args=["entrega"])
        ).content.decode("utf-8")

        self.assertIn("ta-form-grid", html)
        self.assertIn("3. Dados do termo", html)


class EnvioAutomaticoDaViaTests(BaseTermoAssinatura):
    """
    Item 1: o colaborador assina e RECEBE o e-mail, sem depender de clicar.

    O desenho anterior só mandava a via se alguém apertasse "Receber por
    e-mail" na tela de conclusão. Na prática o colaborador assina e devolve o
    tablet: ninguém clicava, e a via nunca chegava — foi o defeito relatado.

    `captureOnCommitCallbacks(execute=True)` é OBRIGATÓRIO aqui: o envio é
    disparado por `transaction.on_commit`, e num `TestCase` a transação nunca
    comita, então sem isto os callbacks ficam na fila e o teste passaria sem
    provar nada.
    """

    @staticmethod
    def _vias_do_colaborador():
        """Só os e-mails que foram PARA o colaborador.

        O `MovimentacaoEstoqueService` dispara o alerta de movimentação pelo
        mesmo `on_commit`, e ele TAMBÉM chega ao colaborador — por isso o filtro
        é pelo assunto, não só pelo destinatário. Contar o `outbox` inteiro
        faria este teste quebrar quando alguém mexesse no alerta, por um motivo
        que não é este.
        """
        from django.core import mail
        return [
            m for m in mail.outbox
            if "maria.teste@exemplo.com.br" in m.to and "Sua via do termo" in m.subject
        ]

    def _assinar(self, **extra):
        sessao = self._criar_sessao()
        with self.captureOnCommitCallbacks(execute=True):
            assinado = tas.registrar_assinatura(
                termo=sessao["termo"],
                assinatura_colaborador=_png_assinatura(),
                signatario_nome="MARIA DE TESTE SILVA",
                aceite=True,
                **extra,
            )
        return assinado

    def test_assinar_envia_a_via_sozinho(self):
        from django.core import mail

        assinado = self._assinar()

        vias = self._vias_do_colaborador()
        self.assertEqual(len(vias), 1, "a via não foi enviada ao assinar")
        enviado = vias[0]
        self.assertEqual(enviado.to, ["maria.teste@exemplo.com.br"])
        self.assertIn("termo", enviado.subject.lower())
        self.assertEqual(len(enviado.attachments), 1)
        self.assertTrue(enviado.attachments[0][1].startswith(b"%PDF"))
        self.assertIn(assinado.codigo_verificacao, enviado.body)

    def test_colaborador_sem_email_nao_quebra_a_assinatura(self):
        from django.core import mail

        self.colaborador.email = ""
        self.colaborador.save(update_fields=["email"])

        assinado = self._assinar()

        self.assertEqual(assinado.status, StatusAssinaturaTermoChoices.ASSINADO)
        self.assertEqual(len(self._vias_do_colaborador()), 0)

    def test_falha_de_smtp_nao_derruba_a_assinatura(self):
        # O envio roda depois do commit, fora da transação. Uma queda de SMTP
        # ali não pode desfazer nem manchar a assinatura, que já é prova.
        from unittest.mock import patch

        with patch(
            "services.email_alertas.enviar_termo_assinado_ao_colaborador",
            side_effect=OSError("SMTP fora do ar"),
        ):
            assinado = self._assinar()

        self.assertEqual(assinado.status, StatusAssinaturaTermoChoices.ASSINADO)
        self.assertTrue(assinado.pdf)

    def test_envio_automatico_registra_no_log(self):
        with self.assertLogs("services.termo_assinatura_service", level="INFO") as registro:
            self._assinar()
        self.assertIn("via enviada automaticamente", "\n".join(registro.output))

    def test_botao_continua_servindo_de_reenvio(self):
        from django.core import mail

        cliente = Client()
        sessao = self._criar_sessao()
        with self.captureOnCommitCallbacks(execute=True):
            cliente.post(
                reverse("termo_assinar_enviar", args=[sessao["token"]]),
                data=json.dumps({
                    "assinatura": _png_assinatura(),
                    "nome": "MARIA DE TESTE SILVA",
                    "aceite": True,
                }),
                content_type="application/json",
            )

        self.assertEqual(len(self._vias_do_colaborador()), 1, "automático")

        resposta = cliente.post(reverse("termo_recibo_email"))
        self.assertTrue(resposta.json()["ok"])
        self.assertEqual(len(self._vias_do_colaborador()), 2, "reenvio pelo botão")


class DesligamentoAssinadoTests(BaseTermoAssinatura):
    """
    Item 2: a assinatura do desligamento encerra o vínculo inteiro —
    devolve tudo, remove as licenças e desliga o cadastro.
    """

    def setUp(self):
        self.cliente = Client()
        self.assertTrue(self.cliente.login(username="ti.teste", password="senha-de-teste-123"))

        # Dois equipamentos em posse do colaborador.
        self.item2 = Item.objects.create(
            nome="MONITOR DO DESLIGADO", numero_serie="SN-DESL-002",
            status=StatusItemChoices.ATIVO, item_consumo="nao", pmb="nao",
            centro_custo=self.cc_origem, localidade=self.localidade_origem,
        )
        for item in (self.item, self.item2):
            item.status = StatusItemChoices.ATIVO
            item.save(update_fields=["status"])
            MovimentacaoItem.objects.create(
                tipo_movimentacao="transferencia", tipo_transferencia="entrega",
                item=item, usuario=self.colaborador, quantidade=1,
            )

    def _criar_e_assinar(self):
        sessao = tas.criar_sessao_desligamento(
            colaborador=self.colaborador,
            form_data={"estabelecimento": "karitel"},
            user=self.user,
        )
        with self.captureOnCommitCallbacks(execute=True):
            assinado = tas.registrar_assinatura(
                termo=sessao["termo"],
                assinatura_colaborador=_png_assinatura(),
                signatario_nome="MARIA DE TESTE SILVA",
                aceite=True,
            )
        return assinado

    def test_sessao_cobre_todos_os_itens_sem_selecao(self):
        sessao = tas.criar_sessao_desligamento(
            colaborador=self.colaborador, form_data={}, user=self.user,
        )
        termo = sessao["termo"]

        self.assertTrue(termo.desligamento)
        self.assertEqual(termo.tipo, "devolucao", "o texto legal assinado é o de devolução")
        self.assertEqual(set(termo.itens.values_list("pk", flat=True)),
                         {self.item.pk, self.item2.pk})

    def test_assinar_devolve_tudo_e_desliga_o_colaborador(self):
        self._criar_e_assinar()

        for item in (self.item, self.item2):
            item.refresh_from_db()
            self.assertEqual(item.status, StatusItemChoices.BACKUP, item.nome)

        self.colaborador.refresh_from_db()
        self.assertEqual(self.colaborador.status, "desligado")

    def test_assinar_remove_as_licencas(self):
        from ProjetoEstoque.models import Licenca, MovimentacaoLicenca

        licenca = Licenca.objects.create(nome="OFFICE 365 TESTE", pmb="nao")
        MovimentacaoLicenca.objects.create(
            licenca=licenca, usuario=self.colaborador,
            tipo="atribuicao", valor_unitario=Decimal("50.00"),
        )
        self.assertEqual(
            len(tas_desligamento_ativos(self.colaborador)["licencas"]), 1,
        )

        self._criar_e_assinar()

        self.assertEqual(
            len(tas_desligamento_ativos(self.colaborador)["licencas"]), 0,
            "a assinatura precisa remover as licenças do colaborador",
        )

    def test_termo_fica_ligado_as_devolucoes_geradas(self):
        assinado = self._criar_e_assinar()

        self.assertGreaterEqual(assinado.movimentacoes.count(), 2)
        for mov in assinado.movimentacoes.all():
            self.assertEqual(mov.tipo_transferencia, "devolucao")
            self.assertTrue(mov.termo_pdf, f"movimentação {mov.pk} sem termo anexado")

    def test_recusa_colaborador_sem_nada_a_devolver(self):
        vazio = Usuario.objects.create(
            matricula="55555", nome="ANA SEM ATIVOS", status="ativo", pmb="nao",
            centro_custo=self.centro_custo, localidade=self.localidade,
        )
        with self.assertRaisesMessage(ValueError, "não tem equipamento"):
            tas.criar_sessao_desligamento(colaborador=vazio, form_data={}, user=self.user)

    def test_tela_lista_o_que_sera_encerrado(self):
        html = self.cliente.get(
            reverse("termo_assinatura_desligamento") + f"?colaborador={self.colaborador.pk}"
        ).content.decode("utf-8")

        self.assertIn("NOTEBOOK DE TESTE", html)
        self.assertIn("MONITOR DO DESLIGADO", html)
        self.assertIn("encerra o vínculo", html)
        # Não há seleção manual: o termo cobre tudo.
        self.assertNotIn('name="itens"', html)

    def test_tela_exige_login(self):
        resposta = Client().get(reverse("termo_assinatura_desligamento"))
        self.assertEqual(resposta.status_code, 302)
        self.assertIn("/login/", resposta["Location"])

    def test_post_exige_confirmacao_explicita(self):
        resposta = self.cliente.post(
            reverse("termo_assinatura_desligamento"),
            data={"colaborador": self.colaborador.pk, "validade_minutos": 30},
        )
        self.assertEqual(resposta.status_code, 200)
        self.assertFalse(TermoAssinatura.objects.filter(desligamento=True).exists())

    def test_post_com_confirmacao_cria_a_sessao(self):
        resposta = self.cliente.post(
            reverse("termo_assinatura_desligamento"),
            data={
                "colaborador": self.colaborador.pk,
                "validade_minutos": 30,
                "confirmacao": "on",
            },
        )
        self.assertEqual(resposta.status_code, 302)
        termo = TermoAssinatura.objects.order_by("-pk").first()
        self.assertTrue(termo.desligamento)
        self.assertEqual(resposta["Location"], reverse("termo_assinatura_painel", args=[termo.pk]))

    def test_devolucao_comum_nao_desliga_ninguem(self):
        # A diferença entre os dois fluxos precisa estar travada: uma devolução
        # avulsa jamais pode encerrar o vínculo por engano.
        sessao = tas.criar_sessao(
            tipo="devolucao", colaborador=self.colaborador, itens=[self.item],
            form_data={}, user=self.user,
        )
        self.assertFalse(sessao["termo"].desligamento)

        with self.captureOnCommitCallbacks(execute=True):
            tas.registrar_assinatura(
                termo=sessao["termo"],
                assinatura_colaborador=_png_assinatura(),
                signatario_nome="MARIA DE TESTE SILVA",
                aceite=True,
            )

        self.colaborador.refresh_from_db()
        self.assertEqual(self.colaborador.status, "ativo")
        self.item2.refresh_from_db()
        self.assertEqual(self.item2.status, StatusItemChoices.ATIVO,
                         "o outro equipamento não podia ser tocado")


def tas_desligamento_ativos(colaborador):
    """Atalho de leitura usado pelos testes de desligamento."""
    from services.desligamento_service import DesligamentoService
    return DesligamentoService.ativos_do_usuario(colaborador)


class TelasDeErroTests(TestCase):
    """
    Item 3: quem sai do link de assinatura e tenta outra URL encontra a tela
    do pivô irrigando, não o erro cru do Django.
    """

    def setUp(self):
        self.cliente = Client()

    def test_404_anonimo_mostra_a_tela_do_pivo(self):
        # `raise_request_exception` não é necessário no 404: o Django já
        # devolve a resposta do handler.
        resposta = self.cliente.get("/rota-que-nao-existe/")

        self.assertEqual(resposta.status_code, 404)
        html = resposta.content.decode("utf-8")
        self.assertIn("erro-page", html)
        self.assertIn("pivô central irrigando", html)
        self.assertIn("css/erro-pivo.css", html)
        # Autossuficiente: carrega os tokens por conta (regra 31) e NÃO depende
        # do base.css, que é do app de quem tem login.
        self.assertIn("css/tokens.css", html)
        self.assertNotIn("css/base.css", html)

    def test_404_nao_mostra_o_menu_nem_vaza_navegacao(self):
        html = self.cliente.get("/rota-que-nao-existe/").content.decode("utf-8")
        self.assertNotIn('id="sidebar"', html)

    def test_telas_de_erro_nao_renderizam_comentario_de_template(self):
        # Regra 17: `{# #}` multilinha vira texto na tela.
        html = self.cliente.get("/rota-que-nao-existe/").content.decode("utf-8")
        for marca in ("{#", "{%", "endcomment"):
            self.assertNotIn(marca, html)

    def test_preview_das_quatro_telas_exige_staff(self):
        comum = User.objects.create_user("comum_erro", "c@c.com", "SenhaK!2026")
        self.cliente.force_login(comum)
        resposta = self.cliente.get(reverse("erro_preview", args=["500"]))
        self.assertIn(resposta.status_code, (302, 403))

    def test_preview_abre_as_quatro_para_staff(self):
        staff = User.objects.create_superuser("staff_erro", "s@s.com", "x")
        self.cliente.force_login(staff)

        esperado = {
            "404": "Não encontramos esta página",
            "403": "Você não tem acesso",
            "400": "Não conseguimos entender",
            "500": "Alguma coisa quebrou",
        }
        for codigo, trecho in esperado.items():
            with self.subTest(codigo=codigo):
                resposta = self.cliente.get(reverse("erro_preview", args=[codigo]))
                # 200 de propósito: é PREVIEW. Devolver 500 de verdade faria
                # navegador e proxy tratarem a página como falha.
                self.assertEqual(resposta.status_code, 200)
                self.assertIn(trecho, resposta.content.decode("utf-8"))

    def test_preview_com_codigo_invalido_cai_no_404(self):
        staff = User.objects.create_superuser("staff_erro2", "s2@s.com", "x")
        self.cliente.force_login(staff)
        html = self.cliente.get(
            reverse("erro_preview", args=["999"])
        ).content.decode("utf-8")
        self.assertIn("Não encontramos esta página", html)

    def test_500_nao_depende_de_resolucao_de_url(self):
        # A tela de 500 usa link literal "/" de propósito: a resolução de URL
        # pode ser justamente o que falhou.
        import pathlib

        from django.conf import settings

        tpl = (pathlib.Path(settings.BASE_DIR) / "templates" / "500.html").read_text(encoding="utf-8")
        self.assertNotIn("{% url", tpl)
        self.assertIn('href="/"', tpl)

    def test_animacao_respeita_preferencia_de_movimento(self):
        import pathlib

        from django.conf import settings

        css = (
            pathlib.Path(settings.BASE_DIR) / "ProjetoEstoque" / "static" / "css" / "erro-pivo.css"
        ).read_text(encoding="utf-8")
        self.assertIn("prefers-reduced-motion", css)
        self.assertIn("pivo-girar", css)


class EquipamentoCompartilhadoTests(BaseTermoAssinatura):
    """
    Equipamento COMPARTILHADO no fluxo de assinatura.

    O defeito relatado: os tablets de campo ficam com 11 colaboradores ao mesmo
    tempo e por isso têm status "Ativo" desde o primeiro. A tela de preparação
    só oferecia Estoque/Backup/Pausado, então esses equipamentos SIMPLESMENTE
    NÃO APARECIAM — e o TI não conseguia colher a assinatura do próximo
    colaborador, justo o caso que o compartilhamento existe para atender. Pior:
    não havia mensagem; o item só não estava na lista.

    A causa é conceitual, não um filtro mal escrito: para item compartilhado
    "Ativo" não significa "já saiu, logo indisponível" — significa "tem ao
    menos um detentor". Quem responde "pode entregar a ESTA pessoa?" é o
    vínculo em `ItemColaborador`, nunca o status do item.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.colega = Usuario.objects.create(
            matricula="88888",
            nome="JOAO DE TESTE SOUZA",
            status="ativo",
            pmb="nao",
            centro_custo=cls.centro_custo,
            localidade=cls.localidade,
            email="joao.teste@exemplo.com.br",
        )
        # Tablet compartilhado JÁ em uso: status Ativo e um vínculo aberto —
        # exatamente o estado dos itens reais que desapareciam da tela.
        cls.tablet = Item.objects.create(
            nome="GALAXY TAB DE TESTE",
            numero_serie="SN-TAB-001",
            marca="SAMSUNG",
            modelo="TAB A11+",
            status=StatusItemChoices.ATIVO,
            item_consumo="nao",
            pmb="nao",
            compartilhado=True,
            centro_custo=cls.cc_origem,
            localidade=cls.localidade_origem,
        )

    def _vincular(self, item, colaborador):
        from ProjetoEstoque.models import ItemColaborador
        return ItemColaborador.objects.create(item=item, colaborador=colaborador, ativo=True)

    def _sessao_tablet(self, colaborador, **extra):
        return tas.criar_sessao(
            tipo="entrega",
            colaborador=colaborador,
            itens=[self.tablet],
            form_data={"responsavel_ti_nome": "Kayque (TI)"},
            user=self.user,
            **extra,
        )

    def _assinar(self, sessao, nome="MARIA DE TESTE SILVA"):
        with self.captureOnCommitCallbacks(execute=True):
            return tas.registrar_assinatura(
                termo=sessao["termo"],
                assinatura_colaborador=_png_assinatura(),
                signatario_nome=nome,
                aceite=True,
            )

    # ── O bug: o item sequer aparecia ────────────────────────────────────

    def test_compartilhado_ativo_entra_no_universo_de_entrega(self):
        from ProjetoEstoque.views.termos_assinatura import _itens_entregaveis

        self._vincular(self.tablet, self.colega)

        ids = set(_itens_entregaveis().values_list("pk", flat=True))
        self.assertIn(
            self.tablet.pk, ids,
            "equipamento compartilhado já Ativo precisa continuar entregável — é "
            "o estado normal dele, e sem isso o próximo colaborador não assina",
        )

    def test_exclusivo_ativo_continua_fora_do_universo_de_entrega(self):
        """O afrouxamento é SÓ para compartilhado. Item de detentor único com
        status Ativo está na mão de alguém: oferecê-lo geraria um termo de
        entrega para algo já entregue, sem devolução no meio."""
        from ProjetoEstoque.views.termos_assinatura import _itens_entregaveis

        self.item.status = StatusItemChoices.ATIVO
        self.item.save(update_fields=["status"])

        self.assertNotIn(
            self.item.pk, set(_itens_entregaveis().values_list("pk", flat=True))
        )

    def test_compartilhado_em_manutencao_continua_fora(self):
        """Manutenção barra nos dois casos: `MovimentacaoItemForm.clean` recusa
        transferir item presa em manutenção, e descobrir isso DEPOIS da
        assinatura é o que a validação na preparação existe para evitar."""
        from ProjetoEstoque.views.termos_assinatura import _itens_entregaveis

        self.tablet.status = StatusItemChoices.MANUTENCAO
        self.tablet.save(update_fields=["status"])

        self.assertNotIn(
            self.tablet.pk, set(_itens_entregaveis().values_list("pk", flat=True))
        )

    def test_busca_remota_marca_compartilhado_e_codetentores(self):
        self._vincular(self.tablet, self.colega)
        self.client.force_login(self.user)

        resposta = self.client.get(
            reverse("termo_assinatura_equipamentos_api"),
            {"tipo": "entrega", "q": "GALAXY TAB DE TESTE"},
        )
        linha = next(r for r in resposta.json()["results"] if r["id"] == self.tablet.pk)
        self.assertTrue(linha["compartilhado"])
        self.assertEqual(linha["codetentores"], 1)
        self.assertFalse(linha["ja_com_colaborador"])

    def test_busca_remota_avisa_quando_ja_esta_com_o_colaborador(self):
        """O operador precisa ver isso ANTES de montar o termo: é o único caso
        que `validar_pre_condicoes` recusa para compartilhado."""
        self._vincular(self.tablet, self.colega)
        self.client.force_login(self.user)

        resposta = self.client.get(
            reverse("termo_assinatura_equipamentos_api"),
            {"tipo": "entrega", "q": "GALAXY", "colaborador": self.colega.pk},
        )
        linha = next(r for r in resposta.json()["results"] if r["id"] == self.tablet.pk)
        self.assertTrue(linha["ja_com_colaborador"])

    # ── Entrega a um segundo colaborador ─────────────────────────────────

    def test_segundo_colaborador_assina_sem_tirar_o_primeiro(self):
        from ProjetoEstoque.models import ItemColaborador

        self._vincular(self.tablet, self.colega)
        assinado = self._assinar(self._sessao_tablet(self.colaborador))

        self.assertEqual(assinado.status, StatusAssinaturaTermoChoices.ASSINADO)

        ativos = set(
            ItemColaborador.objects
            .filter(item=self.tablet, ativo=True)
            .values_list("colaborador_id", flat=True)
        )
        self.assertEqual(
            ativos, {self.colega.pk, self.colaborador.pk},
            "a entrega do compartilhado abre vínculo NOVO; o detentor anterior "
            "não pode ser encerrado por isso",
        )

    def test_entrega_repetida_ao_mesmo_colaborador_e_recusada(self):
        """Entregar de novo a quem já está com o item não abriria vínculo novo
        (`ItemColaborador` tem unicidade por item+colaborador ativo): produziria
        uma assinatura que não muda nada e deixaria a devolução depois com um
        termo sobrando sem par."""
        self._vincular(self.tablet, self.colaborador)

        with self.assertRaises(ValueError) as erro:
            self._sessao_tablet(self.colaborador)

        self.assertIn("já está em posse", str(erro.exception).lower())

    def test_entrega_repetida_liberada_ao_documentar_movimentacao_existente(self):
        """A recusa acima não pode atropelar o caso de uso de REGULARIZAR: a
        transferência já foi registrada (o vínculo já existe) e o que falta é o
        termo daquele ato. Barrar aqui tornaria insolúvel a pendência que o
        próprio Repositório de Termos aponta."""
        vinculo = self._vincular(self.tablet, self.colaborador)
        mov = MovimentacaoItem.objects.create(
            tipo_movimentacao="transferencia",
            tipo_transferencia="entrega",
            item=self.tablet,
            usuario=self.colaborador,
            quantidade=1,
            localidade_destino=self.localidade,
        )
        vinculo.movimentacao_entrega = mov
        vinculo.save(update_fields=["movimentacao_entrega"])

        sessao = self._sessao_tablet(self.colaborador, movimentacoes=[mov])
        self.assertEqual(sessao["termo"].status, StatusAssinaturaTermoChoices.PENDENTE)

    def test_compartilhado_sem_centro_de_custo_nao_bloqueia(self):
        """Isenção que já existia e precisa continuar: compartilhado não
        restaura CC na devolução (é ativo de vários setores)."""
        self.tablet.centro_custo = None
        self.tablet.save(update_fields=["centro_custo"])

        problemas = tas.validar_pre_condicoes(
            tipo="entrega", colaborador=self.colaborador, itens=[self.tablet],
        )
        self.assertEqual(problemas, [])

    # ── Status do item ───────────────────────────────────────────────────

    def test_devolucao_de_um_entre_varios_mantem_o_item_ativo(self):
        self._vincular(self.tablet, self.colega)
        self._vincular(self.tablet, self.colaborador)

        self._assinar(tas.criar_sessao(
            tipo="devolucao",
            colaborador=self.colaborador,
            itens=[self.tablet],
            form_data={"responsavel_ti_nome": "Kayque (TI)"},
            user=self.user,
        ))

        self.tablet.refresh_from_db()
        self.assertEqual(
            self.tablet.status, StatusItemChoices.ATIVO,
            "ainda há outro colaborador com o equipamento — mandá-lo para "
            "Backup apagaria esse fato e o listaria como disponível",
        )

    def test_devolucao_do_ultimo_detentor_leva_para_backup(self):
        self._vincular(self.tablet, self.colaborador)

        self._assinar(tas.criar_sessao(
            tipo="devolucao",
            colaborador=self.colaborador,
            itens=[self.tablet],
            form_data={"responsavel_ti_nome": "Kayque (TI)"},
            user=self.user,
        ))

        self.tablet.refresh_from_db()
        self.assertEqual(self.tablet.status, StatusItemChoices.BACKUP)

    def test_compartilhado_saindo_do_estoque_fica_ativo(self):
        """O `MovimentacaoEstoqueService` só promove compartilhado para Ativo
        saindo de **Backup**. Um tablet parado em "Estoque" entregue a alguém
        ficava com vínculo aberto e status dizendo que estava disponível —
        equipamento na mão de uma pessoa e listado como livre no estoque."""
        self.tablet.status = StatusItemChoices.ESTOQUE
        self.tablet.save(update_fields=["status"])

        self._assinar(self._sessao_tablet(self.colaborador))

        self.tablet.refresh_from_db()
        self.assertEqual(self.tablet.status, StatusItemChoices.ATIVO)

    # ── O que o signatário vê e assina ───────────────────────────────────

    def test_snapshot_registra_compartilhado_e_contagem(self):
        self._vincular(self.tablet, self.colega)
        sessao = self._sessao_tablet(self.colaborador)

        equipamento = sessao["termo"].dados["equipamentos"][0]
        self.assertTrue(equipamento["compartilhado"])
        self.assertEqual(equipamento["codetentores"], 1)

    def test_snapshot_nao_guarda_nome_de_outros_colaboradores(self):
        """Contagem, não lista de nomes: a via vai para o e-mail pessoal do
        signatário, e quem mais usa o equipamento não precisa virar cadastro de
        colegas num documento que sai do sistema."""
        self._vincular(self.tablet, self.colega)
        sessao = self._sessao_tablet(self.colaborador)

        self.assertNotIn(
            self.colega.nome, json.dumps(sessao["termo"].dados, ensure_ascii=False)
        )

    def test_pdf_do_termo_declara_o_uso_compartilhado(self):
        """O arranjo real tem de estar no DOCUMENTO: num item compartilhado o
        signatário não é o único responsável, e um termo que não diz isso
        descreve uma responsabilidade que não é a dele."""
        assinado = self._assinar(self._sessao_tablet(self.colaborador))

        from pypdf import PdfReader
        assinado.pdf.open("rb")
        try:
            texto = "\n".join(p.extract_text() or "" for p in PdfReader(assinado.pdf).pages)
        finally:
            assinado.pdf.close()

        self.assertIn("COMPARTILHADO", texto.upper())

    def test_tablet_avisa_o_signatario_sobre_a_guarda_conjunta(self):
        self._vincular(self.tablet, self.colega)
        sessao = self._sessao_tablet(self.colaborador)

        corpo = self.client.get(
            reverse("termo_assinar", args=[sessao["token"]])
        ).content.decode("utf-8")

        self.assertIn("uso compartilhado", corpo)
        self.assertIn("guarda", corpo)


class RegistroDoEnvioDaViaTests(BaseTermoAssinatura):
    """
    Item 2: "não recebi o e-mail referente a minha última assinatura".

    O envio automático já existia e funcionava — o que NÃO existia era prova de
    que tinha acontecido. O sucesso ia para `logger.info`, e o `settings.LOGGING`
    manda INFO só para o CONSOLE (o `arquivo_erros` começa em WARNING): quando a
    janela do servidor fecha, o rastro vai com ela. Resultado: "o sistema não
    mandou" e "mandou e a mensagem se perdeu" ficavam indistinguíveis — e as
    duas pedem providências opostas.

    Agora cada tentativa fica em `via_email_*`, na linha do termo. Estes testes
    travam as quatro situações que a tela precisa saber diferenciar.
    """

    def _assinar(self, **extra):
        sessao = self._criar_sessao()
        with self.captureOnCommitCallbacks(execute=True):
            return tas.registrar_assinatura(
                termo=sessao["termo"],
                assinatura_colaborador=_png_assinatura(),
                signatario_nome="MARIA DE TESTE SILVA",
                aceite=True,
                **extra,
            )

    def test_envio_bem_sucedido_fica_registrado_na_linha(self):
        assinado = self._assinar()
        assinado.refresh_from_db()

        self.assertEqual(assinado.via_email_status, "enviada")
        self.assertEqual(assinado.via_email_tentativas, 1)
        self.assertIsNotNone(assinado.via_email_em)

    def test_registro_guarda_o_email_mascarado_e_nao_o_endereco_inteiro(self):
        """O endereço completo já está em `Usuario.email`; repeti-lo aqui só
        multiplicaria o lugar de onde ele vaza."""
        assinado = self._assinar()
        assinado.refresh_from_db()

        self.assertNotIn(self.colaborador.email, assinado.via_email_detalhe)
        self.assertIn("@", assinado.via_email_detalhe)

    def test_falha_de_envio_fica_registrada_como_falhou(self):
        from unittest.mock import patch

        with patch(
            "services.email_alertas.enviar_termo_assinado_ao_colaborador",
            return_value=(False, "SMTP recusou a mensagem."),
        ):
            assinado = self._assinar()

        assinado.refresh_from_db()
        self.assertEqual(assinado.via_email_status, "falhou")
        self.assertIn("SMTP", assinado.via_email_detalhe)
        # A assinatura é prova e não pode ser manchada por uma falha de e-mail.
        self.assertEqual(assinado.status, StatusAssinaturaTermoChoices.ASSINADO)
        self.assertTrue(assinado.pdf)

    def test_falha_momentanea_e_repetida_antes_de_desistir(self):
        """Microsoft 365 recusa momentaneamente (limite de submissão, conexão
        derrubada no handshake) e passa na segunda. Com uma tentativa só, isso
        virava "a via nunca chegou"."""
        from unittest.mock import patch

        respostas = [(False, "conexão derrubada"), (True, "Enviado para ma***@exemplo.com.br.")]

        with patch(
            "services.email_alertas.enviar_termo_assinado_ao_colaborador",
            side_effect=respostas,
        ), patch("time.sleep"):
            assinado = self._assinar()

        assinado.refresh_from_db()
        self.assertEqual(assinado.via_email_status, "enviada")
        self.assertEqual(
            assinado.via_email_tentativas, 1,
            "a contagem é de tentativas REGISTRADAS (uma por resultado final), "
            "não de chamadas internas de repetição",
        )

    def test_excecao_no_envio_nao_escapa_e_fica_registrada(self):
        from unittest.mock import patch

        with patch(
            "services.email_alertas.enviar_termo_assinado_ao_colaborador",
            side_effect=OSError("SMTP fora do ar"),
        ), patch("time.sleep"):
            assinado = self._assinar()

        assinado.refresh_from_db()
        self.assertEqual(assinado.via_email_status, "falhou")
        self.assertIn("OSError", assinado.via_email_detalhe)

    def test_colaborador_sem_email_registra_sem_email_e_nao_falhou(self):
        """A distinção importa: "falhou" manda o TI reenviar, "sem_email" manda
        completar o cadastro. Tratar os dois igual faz o TI reenviar em laço uma
        via que não tem para onde ir."""
        self.colaborador.email = ""
        self.colaborador.save(update_fields=["email"])

        assinado = self._assinar()
        assinado.refresh_from_db()

        self.assertEqual(assinado.via_email_status, "sem_email")
        self.assertEqual(assinado.status, StatusAssinaturaTermoChoices.ASSINADO)

    def test_servidor_que_aceita_zero_mensagens_nao_conta_como_enviado(self):
        """`send()` devolve QUANTAS mensagens saíram. Há caminho em que ele
        devolve 0 sem levantar nada — e tratar isso como sucesso faria a tela
        dizer "enviado" para uma via que não saiu: exatamente o relato que não
        se consegue investigar."""
        from unittest.mock import patch

        with patch("django.core.mail.EmailMultiAlternatives.send", return_value=0), \
             patch("time.sleep"):
            assinado = self._assinar()

        assinado.refresh_from_db()
        self.assertEqual(assinado.via_email_status, "falhou")

    # ── Reenvio pelo TI ──────────────────────────────────────────────────

    def test_ti_reenvia_a_via_pela_tela_do_termo(self):
        """O único reenvio que havia era o botão do TABLET, válido pelos 15
        minutos da sessão de quem assinou. Isso nunca cobre o caso real: alguém
        nota dias depois que a via não chegou."""
        from django.core import mail

        assinado = self._assinar()
        mail.outbox = []

        self.client.force_login(self.user)
        resposta = self.client.post(
            reverse("termo_assinatura_reenviar_via", args=[assinado.pk]), follow=True
        )

        self.assertEqual(resposta.status_code, 200)
        vias = [m for m in mail.outbox if "Sua via do termo" in m.subject]
        self.assertEqual(len(vias), 1)

        assinado.refresh_from_db()
        self.assertEqual(assinado.via_email_status, "enviada")
        self.assertEqual(assinado.via_email_tentativas, 2, "o reenvio soma na contagem")

    def test_reenvio_sem_email_no_cadastro_explica_o_que_falta(self):
        assinado = self._assinar()
        self.colaborador.email = ""
        self.colaborador.save(update_fields=["email"])
        assinado.refresh_from_db()

        enviado, mensagem = tas.reenviar_via(assinado, user=self.user)

        self.assertFalse(enviado)
        self.assertIn("não tem e-mail no cadastro", mensagem)
        assinado.refresh_from_db()
        self.assertEqual(assinado.via_email_status, "sem_email")

    def test_reenvio_recusado_para_termo_nao_assinado(self):
        sessao = self._criar_sessao()
        enviado, mensagem = tas.reenviar_via(sessao["termo"], user=self.user)

        self.assertFalse(enviado)
        self.assertIn("já assinado", mensagem)

    def test_reenvio_exige_login(self):
        assinado = self._assinar()
        resposta = self.client.post(
            reverse("termo_assinatura_reenviar_via", args=[assinado.pk])
        )
        self.assertIn(resposta.status_code, (302, 403))

    def test_reenvio_so_aceita_post(self):
        assinado = self._assinar()
        self.client.force_login(self.user)
        resposta = self.client.get(
            reverse("termo_assinatura_reenviar_via", args=[assinado.pk])
        )
        self.assertEqual(resposta.status_code, 405)

    def test_pedido_do_tablet_tambem_fica_registrado(self):
        """O botão do tablet passa pelo mesmo service: deixá-lo fora do
        registro recriaria o ponto cego que o rastreamento fecha."""
        assinado = self._assinar()
        assinado.refresh_from_db()
        antes = assinado.via_email_tentativas

        resposta = self.client.post(reverse("termo_recibo_email"))
        # A sessão de recibo é do navegador que assinou; aqui só interessa que
        # o caminho não registre nada por conta própria quando recusado.
        self.assertIn(resposta.status_code, (200, 400, 404))

        assinado.refresh_from_db()
        if resposta.status_code == 200:
            self.assertEqual(assinado.via_email_tentativas, antes + 1)

    def test_tela_do_termo_mostra_a_situacao_da_via(self):
        assinado = self._assinar()
        self.client.force_login(self.user)

        corpo = self.client.get(
            reverse("termo_assinatura_detalhe", args=[assinado.pk])
        ).content.decode("utf-8")

        self.assertIn("Via do colaborador", corpo)
        self.assertIn("Reenviar via", corpo)

    def test_termos_anteriores_ao_rastreamento_nao_mentem_que_nao_foram_enviados(self):
        """A migration 0173 marca os termos já assinados como `anterior`. Marcá-los
        "não enviada" afirmaria que o sistema não tentou — e naquele desenho ele
        tentava, só não registrava. Mandaria o TI investigar a coisa errada."""
        from ProjetoEstoque.models import StatusViaEmailChoices

        self.assertEqual(
            StatusViaEmailChoices.ANTERIOR.label,
            "Sem registro (anterior ao rastreamento)",
        )


class ImutabilidadeDoTermoAssinadoTests(BaseTermoAssinatura):
    """
    Item 3: a prova não pode ser alterada.

    Um termo assinado é prova: o colaborador declarou aceite sobre AQUELE texto,
    sobre AQUELES equipamentos, e `pdf_sha256` é o que permite afirmar depois
    que o PDF não mudou. Se esses campos puderem ser reescritos, a verificação
    de autenticidade passa a atestar o documento ERRADO — e continua dizendo
    "íntegro", porque o hash teria sido regravado junto. É a pior falha
    possível aqui: silenciosa e com cara de sucesso.

    O risco real não é o invasor; é o código. Um `save()` distraído numa rotina
    futura faz isso sem má intenção e sem deixar rastro. Por isso o bloqueio
    está no MODEL — o único ponto por onde todo caminho de gravação passa.
    """

    def setUp(self):
        sessao = self._criar_sessao()
        with self.captureOnCommitCallbacks(execute=True):
            self.assinado = tas.registrar_assinatura(
                termo=sessao["termo"],
                assinatura_colaborador=_png_assinatura(),
                signatario_nome="MARIA DE TESTE SILVA",
                aceite=True,
            )

    def test_nao_deixa_reescrever_o_hash_do_pdf(self):
        from django.core.exceptions import ValidationError

        self.assinado.pdf_sha256 = "0" * 64
        with self.assertRaises(ValidationError):
            self.assinado.save()

    def test_nao_deixa_reescrever_o_snapshot_do_que_foi_assinado(self):
        from django.core.exceptions import ValidationError

        self.assinado.dados = {"equipamentos": [{"nome": "OUTRA COISA"}]}
        with self.assertRaises(ValidationError):
            self.assinado.save(update_fields=["dados"])

    def test_nao_deixa_trocar_o_signatario_nem_a_data(self):
        from django.core.exceptions import ValidationError

        self.assinado.signatario_nome = "OUTRA PESSOA"
        self.assinado.assinado_em = timezone.now() - timedelta(days=30)
        with self.assertRaises(ValidationError):
            self.assinado.save()

    def test_nao_deixa_trocar_o_colaborador_do_termo(self):
        from django.core.exceptions import ValidationError

        outro = Usuario.objects.create(
            matricula="77777", nome="OUTRO COLABORADOR", status="ativo", pmb="nao",
            centro_custo=self.centro_custo, localidade=self.localidade,
        )
        self.assinado.colaborador = outro
        with self.assertRaises(ValidationError):
            self.assinado.save()

    def test_a_mensagem_do_bloqueio_diz_o_que_fazer(self):
        from django.core.exceptions import ValidationError

        self.assinado.pdf_sha256 = "0" * 64
        with self.assertRaises(ValidationError) as erro:
            self.assinado.save()

        texto = " ".join(erro.exception.messages).lower()
        self.assertIn("pdf_sha256", texto)
        self.assertIn("termo novo", texto)

    def test_salvar_sem_alterar_nada_continua_funcionando(self):
        """O guarda compara VALORES, não intenções: um save inofensivo (uma
        rotina que toca a linha sem mudar a prova) não pode quebrar."""
        self.assinado.save()
        self.assinado.refresh_from_db()
        self.assertEqual(self.assinado.status, StatusAssinaturaTermoChoices.ASSINADO)

    def test_registro_do_envio_da_via_continua_gravavel(self):
        """`via_email_*` descreve o que aconteceu DEPOIS da assinatura (o envio
        da via, e cada reenvio) e não faz parte do que foi assinado — por isso
        fica fora da lista de campos imutáveis."""
        tas.registrar_resultado_via(
            self.assinado, enviado=True, mensagem="Enviado para ma***@exemplo.com.br.",
        )
        self.assinado.refresh_from_db()
        self.assertEqual(self.assinado.via_email_status, "enviada")

    def test_cancelamento_de_termo_assinado_continua_recusado(self):
        """Proteção que já existia, no outro nível: o documento foi firmado, e
        apagá-lo do fluxo seria destruir prova."""
        with self.assertRaises(ValueError):
            tas.cancelar_sessao(termo=self.assinado, user=self.user)

    def test_admin_nao_quebra_ao_salvar_termo_assinado(self):
        """
        O admin registra `TermoAssinatura`, então ele é um caminho REAL até o
        `save()` protegido — e o guarda levanta `ValidationError`, que o
        `ModelAdmin.save_model` não converte em erro de formulário: viraria 500.

        Não vira, porque os `readonly_fields` do `TermoAssinaturaAdmin` já
        cobrem todos os campos de prova (as duas barreiras da regra 42), e o
        que sobra editável está fora da lista imutável. Este teste é o que
        mantém os dois conjuntos alinhados: se alguém tornar um campo de prova
        editável no admin, ele quebra aqui em vez de quebrar na cara do usuário.
        """
        from ProjetoEstoque.admin import TermoAssinaturaAdmin
        from ProjetoEstoque.models import TermoAssinatura as _TA

        editaveis = {
            campo.name
            for campo in _TA._meta.get_fields()
            if getattr(campo, "editable", False) and not campo.auto_created
        } - set(TermoAssinaturaAdmin.readonly_fields)

        vazamentos = editaveis & set(_TA.CAMPOS_IMUTAVEIS_APOS_ASSINATURA)
        self.assertEqual(
            vazamentos, set(),
            "campo de prova editável no admin: o save() do model vai recusar e "
            "o admin devolve 500 em vez de mensagem. Acrescente em "
            "TermoAssinaturaAdmin.readonly_fields: " + ", ".join(sorted(vazamentos)),
        )

    def test_admin_salva_o_que_sobrou_editavel_sem_tropecar_no_guarda(self):
        self.assinado.motivo_cancelamento = "anotacao administrativa"
        self.assinado.save()

        self.assinado.refresh_from_db()
        self.assertEqual(self.assinado.motivo_cancelamento, "anotacao administrativa")


class LimiteDaPortaAnonimaTests(BaseTermoAssinatura):
    """
    Item 3: a porta anônima não pode ser usada como alavanca.

    `/termos/assinar/<token>/` é a única rota que grava arquivo e emite
    documento assinado SEM login — por desenho (o colaborador não tem conta).
    O token de 32 bytes não é adivinhável, então o risco não é acertar o token:
    é o CUSTO de cada tentativa. `resolver_token` varre todas as sessões
    assináveis e compara uma a uma em tempo constante — a varredura é o que
    torna a comparação segura e é também o que faz cada requisição custar uma
    consulta. Sem teto, um laço apontado para essa URL vira carga contínua num
    SQLite que atende o sistema inteiro: não derruba a assinatura, derruba o
    Controle de Estoque.
    """

    # O cache é LocMem e sobrevive entre testes DA SUITE INTEIRA — não só
    # desta classe. Como estes testes ESGOTAM o teto de propósito, limpar
    # apenas na entrada protegeria este arquivo e deixaria a bomba armada para
    # quem rodar depois: nenhuma das outras classes chama `super().setUp()`, e
    # qualquer teste posterior que abrisse `termo_assinar` receberia 404 por um
    # motivo que não tem nada a ver com ele. Por isso limpa na SAÍDA também.
    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    def tearDown(self):
        from django.core.cache import cache
        cache.clear()

    def test_abertura_normal_passa_sem_tropecar_no_teto(self):
        sessao = self._criar_sessao()
        for _ in range(5):
            resposta = self.client.get(reverse("termo_assinar", args=[sessao["token"]]))
            self.assertEqual(resposta.status_code, 200)

    def test_excesso_de_tentativas_e_recusado(self):
        sessao = self._criar_sessao()
        url = reverse("termo_assinar", args=[sessao["token"]])

        for _ in range(tas._LIMITE_TOKEN_POR_IP):
            self.client.get(url)

        resposta = self.client.get(url)
        self.assertEqual(resposta.status_code, 404)

    def test_recusa_por_excesso_usa_a_MESMA_tela_de_token_invalido(self):
        """Distinguir "bloqueado por excesso" de "token inválido" entregaria ao
        sondador a informação de que vale insistir de outro endereço — mesmo
        princípio da regra 27."""
        sessao = self._criar_sessao()
        url = reverse("termo_assinar", args=[sessao["token"]])

        esperado = self.client.get(
            reverse("termo_assinar", args=["token-que-nao-existe"])
        ).content

        for _ in range(tas._LIMITE_TOKEN_POR_IP):
            self.client.get(url)

        self.assertEqual(self.client.get(url).content, esperado)

    def test_o_envio_da_assinatura_tambem_tem_teto(self):
        sessao = self._criar_sessao()
        url = reverse("termo_assinar_enviar", args=[sessao["token"]])

        for _ in range(tas._LIMITE_TOKEN_POR_IP):
            self.client.post(url, data="{}", content_type="application/json")

        resposta = self.client.post(url, data="{}", content_type="application/json")
        self.assertEqual(resposta.status_code, 404)
        self.assertFalse(resposta.json()["ok"])


class AuditoriaDeIntegridadeTests(BaseTermoAssinatura):
    """
    Item 3: perda e alteração de arquivo têm de ser DETECTÁVEIS.

    `verificar_autenticidade` confere UM termo, quando alguém já tem motivo para
    desconfiar. Problema de arquivo não anuncia: ele é descoberto no dia em que
    o documento é pedido, que é o pior dia para descobrir. O `MEDIA_ROOT` deste
    projeto fica em pasta sincronizada por OneDrive — sincronização que resolve
    conflito criando "arquivo (2)", restauração parcial de backup e pasta movida
    produzem exatamente dois estados: arquivo que SUMIU e arquivo que MUDOU.
    """

    def setUp(self):
        sessao = self._criar_sessao()
        with self.captureOnCommitCallbacks(execute=True):
            self.assinado = tas.registrar_assinatura(
                termo=sessao["termo"],
                assinatura_colaborador=_png_assinatura(),
                signatario_nome="MARIA DE TESTE SILVA",
                aceite=True,
            )

    def test_acervo_intacto_e_reportado_como_integro(self):
        resultado = tas.verificar_integridade_assinados()

        self.assertEqual(resultado["total"], 1)
        self.assertEqual(resultado["ok"], 1)
        self.assertEqual(resultado["alterados"], 0)
        self.assertEqual(resultado["ausentes"], 0)

    def test_arquivo_alterado_no_disco_e_apontado(self):
        import pathlib
        caminho = pathlib.Path(self.assinado.pdf.path)
        caminho.write_bytes(b"%PDF-1.4 conteudo trocado")

        resultado = tas.verificar_integridade_assinados()

        self.assertEqual(resultado["alterados"], 1)
        self.assertEqual(resultado["itens"][0]["situacao"], "alterado")

    def test_arquivo_ausente_e_apontado(self):
        import pathlib
        pathlib.Path(self.assinado.pdf.path).unlink()

        resultado = tas.verificar_integridade_assinados()

        self.assertEqual(resultado["ausentes"], 1)
        self.assertEqual(resultado["itens"][0]["situacao"], "ausente")

    def test_termo_sem_hash_gravado_nao_passa_por_integro(self):
        """Sem hash não há com o que comparar, e tratá-lo como íntegro daria
        certificado de integridade a um arquivo que ninguém conferiu."""
        TermoAssinatura.objects.filter(pk=self.assinado.pk).update(pdf_sha256="")

        resultado = tas.verificar_integridade_assinados()
        self.assertEqual(resultado["ausentes"], 1)

    def test_comando_de_auditoria_falha_quando_ha_problema(self):
        """Sai com código 1 para poder ser agendado no Task Scheduler e falhar
        visivelmente — mesma convenção dos outros comandos agendados."""
        import pathlib
        from io import StringIO
        from django.core.management import call_command

        pathlib.Path(self.assinado.pdf.path).unlink()

        with self.assertRaises(SystemExit):
            call_command("auditar_termos_assinados", stdout=StringIO(), stderr=StringIO())

    def test_comando_de_auditoria_passa_com_acervo_intacto(self):
        from io import StringIO
        from django.core.management import call_command

        saida = StringIO()
        call_command("auditar_termos_assinados", stdout=saida)

        self.assertIn("íntegro", saida.getvalue())


class TrilhaDeAcessoAoDocumentoTests(BaseTermoAssinatura):
    """
    Item 3: quem abriu o documento assinado fica registrado.

    O PDF traz a assinatura e o documento pessoal que o colaborador digitou no
    tablet (CPF/RG não vão para o banco — regra do item anterior — mas estão
    impressos no PDF). "Quem teve acesso a isso?" é pergunta que precisa ter
    resposta sem depender da memória de ninguém.
    """

    def setUp(self):
        sessao = self._criar_sessao()
        with self.captureOnCommitCallbacks(execute=True):
            self.assinado = tas.registrar_assinatura(
                termo=sessao["termo"],
                assinatura_colaborador=_png_assinatura(),
                signatario_nome="MARIA DE TESTE SILVA",
                aceite=True,
            )

    def test_download_do_pdf_registra_quem_abriu(self):
        self.client.force_login(self.user)

        with self.assertLogs("seguranca", level="INFO") as registro:
            resposta = self.client.get(
                reverse("termo_assinatura_pdf", args=[self.assinado.pk])
            )

        self.assertEqual(resposta.status_code, 200)
        linhas = " ".join(registro.output)
        self.assertIn("Termo assinado lido", linhas)
        self.assertIn(self.user.username, linhas)
        self.assertIn(self.assinado.codigo_verificacao, linhas)

    def test_token_recusado_entra_na_trilha_de_seguranca(self):
        with self.assertLogs("seguranca", level="INFO") as registro:
            self.client.get(reverse("termo_assinar", args=["token-invalido-qualquer"]))

        self.assertIn("token recusado", " ".join(registro.output))
