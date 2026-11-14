"""
Frotas e Maquinários — o assistente de importação da planilha, como a PESSOA o usa: os seis passos (envio, mapeamento,
pré-visualização, validação, confirmação, resultado), o que cada um mostra e o que cada um recusa.

As regras da importação em si (o que é válido, o que é conferência, o que nunca se corrige, a idempotência) estão em
`test_frota_equipamento_import.py`. Aqui se prova a CAMADA WEB sobre elas:

  • o mapeamento é enviado pelo formulário REAL: o teste lê o HTML da tela, junta os campos como o navegador faria e os
    envia — se o template e a view discordarem sobre o nome de um campo, o teste quebra;
  • nada é gravado na frota antes da confirmação, e confirmar duas vezes não importa duas vezes;
  • prévia vencida, importação cancelada e passo fora de ordem não se confirmam;
  • tudo o que vem da planilha aparece escapado.
"""
import io
from datetime import timedelta
from html.parser import HTMLParser
from unittest import mock

from django.contrib.messages import get_messages
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from ProjetoEstoque.models import FrotaImportacaoEquipamentos, FrotaVeiculo
from ProjetoEstoque.tests.test_frota_equipamento_import import linha, planilha_do_erp, upload, xlsx_bytes
from ProjetoEstoque.tests.test_m2m_telas import TelasBase
from services import frota_equipamento_import_service as imp

PERMISSAO = "importar_equipamentos_frota"
TIPO_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class _Formulario(HTMLParser):
    """Os campos de UM formulário como o navegador os enviaria: cada `input` (nome e valor) e cada `select` (a opção
    marcada, ou a primeira). Não envia botão, caixa desmarcada nem arquivo."""

    def __init__(self, id_do_form):
        super().__init__()
        self.alvo, self.dentro, self.campos = id_do_form, False, []
        self._select, self._opcoes = None, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self.dentro = a.get("id") == self.alvo
        elif not self.dentro:
            return
        elif tag == "input" and a.get("name") and a.get("type") not in ("submit", "checkbox", "radio", "button", "file"):
            self.campos.append((a["name"], a.get("value", "")))
        elif tag == "select":
            self._select, self._opcoes = a.get("name"), []
        elif tag == "option" and self._select:
            self._opcoes.append((a.get("value", ""), "selected" in a))

    def handle_endtag(self, tag):
        if tag == "form":
            self.dentro = False
        elif tag == "select" and self._select:
            marcada = next((v for v, selecionada in self._opcoes if selecionada), self._opcoes[0][0] if self._opcoes else "")
            self.campos.append((self._select, marcada))
            self._select = None

    def dados(self) -> dict:
        """`{nome: [valores]}` — o cliente de teste manda a lista como campo repetido (o par `_valor`/`_decisao`)."""
        saida: dict = {}
        for nome, valor in self.campos:
            saida.setdefault(nome, []).append(valor)
        return saida


def decidir(dados: dict, campo: str, valor: str, decisao: str) -> None:
    """Muda, no formulário lido, a decisão do `valor` do `campo` (os dois campos andam em pares, na mesma ordem)."""
    dados[f"{campo}_decisao"][dados[f"{campo}_valor"].index(valor)] = decisao


class AssistenteBase(TelasBase):
    # ── o que a pessoa faz ──
    def enviar_pela_tela(self, linhas=None, nome="equipamentos motorizados.xlsx", cliente=None, **extra):
        cliente = cliente or self.client
        arquivo = upload(nome, xlsx_bytes(planilha_do_erp() if linhas is None else linhas, **extra))
        return cliente.post(reverse("frota_equip_importar"), {"arquivo": arquivo})

    def previa_enviada(self, linhas=None, **extra):
        resposta = self.enviar_pela_tela(linhas, **extra)
        self.assertEqual(resposta.status_code, 302, resposta.content.decode("utf-8")[:300])
        return FrotaImportacaoEquipamentos.objects.order_by("-pk").first()

    def pagina(self, importacao, cliente=None, **params):
        return (cliente or self.client).get(reverse("frota_equip_importacao", args=[importacao.pk]), params)

    def formulario(self, html, id_do_form):
        leitor = _Formulario(id_do_form)
        leitor.feed(html)
        return leitor.dados()

    def conferir_pela_tela(self, importacao, decisoes=None, **campos):
        """Abre o mapeamento, lê o formulário como o navegador o faria e o envia. `decisoes`: `[(campo, valor, decisão)]`."""
        html = self.html(self.pagina(importacao, etapa="mapeamento"))
        dados = self.formulario(html, "agro-imp-mapear")
        for campo, valor, decisao in decisoes or []:
            decidir(dados, campo, valor, decisao)
        dados.update(campos)
        return self.client.post(reverse("frota_equip_importacao_mapear", args=[importacao.pk]), dados)

    def previa_conferida(self, linhas=None, **extra):
        previa = self.previa_enviada(linhas, **extra)
        resposta = self.conferir_pela_tela(previa)
        self.assertEqual(resposta.status_code, 302, self.mensagens(resposta))
        previa.refresh_from_db()
        return previa

    def confirmar_pela_tela(self, importacao, **dados):
        return self.client.post(reverse("frota_equip_importacao_confirmar", args=[importacao.pk]), dados)

    def importar_pela_tela(self, linhas=None, atualizar=False, **extra):
        previa = self.previa_conferida(linhas, **extra)
        self.confirmar_pela_tela(previa, **({"atualizar": "1"} if atualizar else {}))
        previa.refresh_from_db()
        return previa

    def mensagens(self, resposta):
        return [str(m) for m in get_messages(resposta.wsgi_request)]


# ═════════════════════════════════════════════════════════════
# Acesso
# ═════════════════════════════════════════════════════════════

class AcessoTests(AssistenteBase):
    def rotas(self, pk):
        return [
            ("frota_equip_importar", [], "get"), ("frota_equip_importar_modelo", [], "get"), ("frota_equip_importacoes", [], "get"),
            ("frota_equip_importacao", [pk], "get"), ("frota_equip_importacao_mapear", [pk], "post"),
            ("frota_equip_importacao_confirmar", [pk], "post"), ("frota_equip_importacao_cancelar", [pk], "post"),
            ("frota_equip_importacao_relatorio", [pk], "get"),
        ]

    def test_toda_rota_exige_login_e_a_permissao_de_importar(self):
        previa = self.previa_enviada()
        anonimo = Client()
        so_ve = self.cliente_de(self.usuario_com("so_ve", "ver_frota", "editar_frota", "exportar_frota", "validar_inventario_frota"))
        for nome, args, metodo in self.rotas(previa.pk):
            with self.subTest(rota=nome):
                url = reverse(nome, args=args)
                self.assertEqual(getattr(anonimo, metodo)(url).status_code, 302)
                self.assertEqual(getattr(so_ve, metodo)(url).status_code, 403)

    def test_quem_tem_a_permissao_abre_as_telas_de_leitura(self):
        previa = self.previa_enviada()
        importador = self.cliente_de(self.usuario_com("importador", "ver_frota", PERMISSAO))
        for nome, args in [("frota_equip_importar", []), ("frota_equip_importar_modelo", []), ("frota_equip_importacoes", []),
                           ("frota_equip_importacao", [previa.pk]), ("frota_equip_importacao_relatorio", [previa.pk])]:
            with self.subTest(rota=nome):
                self.assertEqual(importador.get(reverse(nome, args=args)).status_code, 200)

    def test_as_acoes_que_gravam_nao_aceitam_get(self):
        previa = self.previa_enviada()
        for nome in ("frota_equip_importacao_mapear", "frota_equip_importacao_confirmar", "frota_equip_importacao_cancelar"):
            with self.subTest(rota=nome):
                self.assertEqual(self.client.get(reverse(nome, args=[previa.pk])).status_code, 405)

    def test_importacao_que_nao_existe_da_404(self):
        for nome in ("frota_equip_importacao", "frota_equip_importacao_relatorio"):
            self.assertEqual(self.client.get(reverse(nome, args=[999999])).status_code, 404)
        for nome in ("frota_equip_importacao_mapear", "frota_equip_importacao_confirmar", "frota_equip_importacao_cancelar"):
            self.assertEqual(self.client.post(reverse(nome, args=[999999])).status_code, 404)

    def test_o_botao_de_importar_so_aparece_para_quem_pode(self):
        url = reverse("frota_equip_importar")
        self.assertIn(url, self.html(self.get("frota_visao_geral")))
        so_ve = self.cliente_de(self.usuario_com("so_ve_2", "ver_frota"))
        self.assertNotIn(url, so_ve.get(reverse("frota_visao_geral")).content.decode("utf-8"))


# ═════════════════════════════════════════════════════════════
# Passo 1: envio
# ═════════════════════════════════════════════════════════════

class EnvioTests(AssistenteBase):
    def test_a_tela_mostra_os_seis_passos_as_colunas_e_o_modelo(self):
        html = self.html(self.get("frota_equip_importar"))
        self.sem_lixo_de_template(html, "envio")
        for rotulo in ("Envio", "Mapeamento", "Pré-visualização", "Validação", "Confirmação", "Resultado"):
            self.assertIn(rotulo, html)
        for coluna in imp.COLUNAS:
            self.assertIn(coluna.rotulo, html)
        self.assertIn(reverse("frota_equip_importar_modelo") + "?formato=xlsx", html)
        self.assertIn(reverse("frota_equip_importar_modelo") + "?formato=csv", html)
        self.assertIn("Zelo Agro", html)

    def test_enviar_le_a_planilha_e_leva_ao_mapeamento_sem_gravar_na_frota(self):
        antes = FrotaVeiculo.objects.count()
        resposta = self.enviar_pela_tela()
        previa = FrotaImportacaoEquipamentos.objects.get()
        self.assertRedirects(
            resposta, reverse("frota_equip_importacao", args=[previa.pk]) + "?etapa=mapeamento", fetch_redirect_response=False,
        )
        self.assertEqual((previa.estado, previa.linhas, previa.criada_por), ("previa", 8, self.admin))
        self.assertEqual(FrotaVeiculo.objects.count(), antes)
        self.assertTrue(any("nada foi gravado" in m for m in self.mensagens(resposta)))

    def test_arquivo_que_nao_serve_volta_para_o_envio_com_o_motivo_e_nao_grava_nada(self):
        casos = {
            "sem arquivo": (None, "Escolha a planilha"),
            "extensão errada": (upload("e.xls", b"x"), "Envie uma planilha"),
            "arquivo vazio": (upload("e.csv", b""), "vazio"),
            "sem a coluna Equipamento": (upload("e.xlsx", xlsx_bytes([["a", "b"]], cabecalho=["Nome", "Valor"])), "Colunas esperadas"),
        }
        for rotulo, (arquivo, trecho) in casos.items():
            with self.subTest(caso=rotulo):
                resposta = self.client.post(reverse("frota_equip_importar"), {"arquivo": arquivo} if arquivo else {})
                self.assertEqual(resposta.status_code, 400)
                self.assertIn(trecho, resposta.context["erro"])
                self.assertIn('role="alert"', resposta.content.decode("utf-8"))
        self.assertEqual(FrotaImportacaoEquipamentos.objects.count(), 0)

    def test_arquivo_maior_que_o_limite_e_recusado_antes_de_ler(self):
        with mock.patch.object(imp, "MAX_BYTES", 10):
            resposta = self.enviar_pela_tela()
        self.assertEqual(resposta.status_code, 400)
        self.assertIn("limite", resposta.context["erro"])
        self.assertEqual(FrotaImportacaoEquipamentos.objects.count(), 0)

    def test_o_modelo_baixa_em_excel_e_em_csv(self):
        xlsx = self.client.get(reverse("frota_equip_importar_modelo"))
        self.assertEqual(xlsx["Content-Type"], TIPO_XLSX)
        self.assertIn("modelo_equipamentos_motorizados.xlsx", xlsx["Content-Disposition"])
        self.assertEqual(load_workbook(io.BytesIO(xlsx.content)).sheetnames, ["Equipamentos", "Instruções"])
        csv = self.client.get(reverse("frota_equip_importar_modelo"), {"formato": "csv"})
        self.assertTrue(csv["Content-Type"].startswith("text/csv"))
        self.assertEqual(csv.content.decode("utf-8-sig").strip().split(";"), imp.ROTULOS)

    def test_as_importacoes_recentes_aparecem_no_envio_e_o_historico_lista_todas(self):
        self.previa_enviada(nome="primeira.xlsx")
        self.previa_enviada([linha(8000001, "SEGUNDA")], nome="segunda.xlsx")
        html = self.html(self.get("frota_equip_importar"))
        self.assertIn("primeira.xlsx", html)
        self.assertIn("segunda.xlsx", html)
        historico = self.get("frota_equip_importacoes")
        self.assertEqual([i.arquivo_nome for i in historico.context["importacoes"]], ["segunda.xlsx", "primeira.xlsx"])
        self.sem_lixo_de_template(self.html(historico), "historico")

    def test_historico_vazio_orienta_o_primeiro_envio(self):
        html = self.html(self.get("frota_equip_importacoes"))
        self.assertIn("Nenhuma importação feita", html)
        self.assertIn(reverse("frota_equip_importar"), html)


# ═════════════════════════════════════════════════════════════
# Passo 2: mapeamento
# ═════════════════════════════════════════════════════════════

class MapeamentoTests(AssistenteBase):
    def test_a_tela_mostra_cada_coluna_com_a_do_arquivo_e_os_valores_a_decidir(self):
        previa = self.previa_enviada()
        resposta = self.pagina(previa)                                           # sem ?etapa: abre onde se está
        html = self.html(resposta)
        self.sem_lixo_de_template(html, "mapeamento")
        self.assertEqual(resposta.context["etapa"], "mapeamento")
        for coluna in imp.COLUNAS:
            self.assertIn(f'name="coluna_{coluna.campo}"', html)
        for grupo in ("situacao", "empresa", "classe_operacional"):
            self.assertIn(f'name="{grupo}_valor"', html)
            self.assertIn(f'name="{grupo}_decisao"', html)
        self.assertIn('class="agro-sugerido"', html)                              # "sugerido": o sistema propôs, a pessoa confere
        self.assertIn("SANTA COLOMBA", html)
        self.assertIn("data-rotulo=", html)                                      # a linha vira cartão no celular

    def test_enviar_o_formulario_lido_da_tela_confere_o_mapeamento_e_abre_a_previa(self):
        previa = self.previa_enviada()
        self.assertFalse(previa.mapeamento["conferido"])
        resposta = self.conferir_pela_tela(previa)
        self.assertRedirects(
            resposta, reverse("frota_equip_importacao", args=[previa.pk]) + "?etapa=previa", fetch_redirect_response=False,
        )
        previa.refresh_from_db()
        self.assertTrue(previa.mapeamento["conferido"])
        r = imp.resumo(previa)
        self.assertEqual((r["novos"], r["rejeitados"], r["pronta"]), (8, 0, True))
        self.assertEqual(FrotaVeiculo.objects.count(), 0)                        # conferir não grava nada na frota

    def test_valor_sem_decisao_rejeita_a_linha_e_a_tela_avisa_antes(self):
        previa = self.previa_enviada([linha(8110001, "A"), linha(8110002, "B", situacao="Vendido")])
        html = self.html(self.pagina(previa))
        self.assertIn("1 valor sem destino", html)
        self.assertIn("Vendido", html)
        resposta = self.conferir_pela_tela(previa)                               # segue sem decidir: a linha é rejeitada
        previa.refresh_from_db()
        self.assertEqual((previa.validas, previa.rejeitados), (1, 1))
        self.assertTrue(resposta.status_code == 302)

    def test_a_decisao_da_pessoa_vale_e_a_linha_deixa_de_ser_rejeitada(self):
        previa = self.previa_enviada([linha(8110001, "A"), linha(8110002, "B", situacao="Vendido")])
        self.conferir_pela_tela(previa, decisoes=[("situacao", "Vendido", "inativo")])
        previa.refresh_from_db()
        self.assertEqual((previa.validas, previa.rejeitados), (2, 0))
        self.assertEqual(previa.itens.get(codigo_legado="8110002").dados["status"], "inativo")

    def test_voltar_ao_mapeamento_mostra_o_que_foi_decidido(self):
        previa = self.previa_enviada([linha(8110001, "A"), linha(8110002, "B", situacao="Vendido")])
        self.conferir_pela_tela(previa, decisoes=[("situacao", "Vendido", "manutencao")])
        html = self.html(self.pagina(previa, etapa="mapeamento"))
        self.assertRegex(html, r'<option value="manutencao"\s+selected>')

    def test_sem_a_coluna_empresa_a_tela_avisa_que_nenhuma_linha_tem_natureza(self):
        previa = self.previa_enviada()
        html = self.html(self.pagina(previa))
        self.assertNotIn("não tem a coluna Empresa", html)
        colunas = dict(self.formulario(html, "agro-imp-mapear"))
        colunas["coluna_empresa"] = [""]
        resposta = self.client.post(reverse("frota_equip_importacao_mapear", args=[previa.pk]), colunas)
        self.assertEqual(resposta.status_code, 302)
        html = self.html(self.pagina(previa, etapa="mapeamento"))
        self.assertIn("não tem a coluna Empresa mapeada", html)
        previa.refresh_from_db()
        self.assertEqual(previa.rejeitados, previa.linhas)                        # sem natureza, nenhuma linha passa

    def test_campo_obrigatorio_sem_coluna_e_recusado_com_a_mensagem(self):
        previa = self.previa_enviada()
        resposta = self.conferir_pela_tela(previa, coluna_codigo_legado=[""])
        self.assertRedirects(
            resposta, reverse("frota_equip_importacao", args=[previa.pk]) + "?etapa=mapeamento", fetch_redirect_response=False,
        )
        self.assertTrue(any("Escolha a coluna de Equipamento" in m for m in self.mensagens(resposta)))
        previa.refresh_from_db()
        self.assertFalse(previa.mapeamento["conferido"])

    def test_coluna_fora_do_arquivo_e_valor_invalido_sao_recusados(self):
        previa = self.previa_enviada()
        resposta = self.conferir_pela_tela(previa, coluna_modelo=["99"])
        self.assertTrue(any("não existe no arquivo" in m for m in self.mensagens(resposta)))
        resposta = self.conferir_pela_tela(previa, decisoes=[("empresa", "SANTA COLOMBA", "voando")])
        self.assertTrue(any("Valor inválido" in m for m in self.mensagens(resposta)))
        previa.refresh_from_db()
        self.assertFalse(previa.mapeamento["conferido"])

    def test_cabecalho_com_titulo_acima_e_mapeado_do_mesmo_jeito(self):
        previa = self.previa_enviada(titulo_acima="Relatório de equipamentos — 13/11/2026")
        self.conferir_pela_tela(previa)
        previa.refresh_from_db()
        self.assertEqual((previa.validas, previa.rejeitados), (8, 0))


# ═════════════════════════════════════════════════════════════
# Passos 3 e 4: pré-visualização e validação
# ═════════════════════════════════════════════════════════════

class PreviaEValidacaoTests(AssistenteBase):
    def setUp(self):
        super().setUp()
        self.previa = self.previa_conferida()

    def test_a_previa_lista_uma_linha_por_equipamento_da_planilha(self):
        resposta = self.pagina(self.previa, etapa="previa")
        html = self.html(resposta)
        self.sem_lixo_de_template(html, "previa")
        self.assertEqual(sorted(l["codigo_legado"] for l in resposta.context["linhas"]), sorted(str(l[2]) for l in planilha_do_erp()))
        self.assertIn("Nada foi gravado na frota", html)
        for celula in ("TRATOR NH 3880", "NZH4261", "Próprio", "Terceiro", "Caminhão"):
            self.assertIn(celula, html)

    def test_o_resumo_traz_os_totais_e_os_fatos_do_arquivo(self):
        html = self.html(self.pagina(self.previa, etapa="previa"))
        for rotulo in ("Linhas lidas", "Novos", "Já cadastrados", "Repetidos", "Rejeitados", "Com conferência"):
            self.assertIn(rotulo, html)
        self.assertIn("Sem placa:", html)
        self.assertIn("Sem chassi:", html)

    def test_filtros_por_situacao_busca_e_conferencia(self):
        def codigos(**params):
            return sorted(l["codigo_legado"] for l in self.pagina(self.previa, etapa="previa", **params).context["linhas"])

        self.assertEqual(codigos(q="honda"), ["8720079"])
        self.assertEqual(codigos(regra="chassi_duplicado"), ["8110001", "8210001"])
        self.assertEqual(codigos(situacao="rejeitada"), [])
        self.assertEqual(len(codigos(situacao="valida")), 8)
        self.assertEqual(codigos(situacao="inventada"), codigos())                # filtro desconhecido não esconde linha
        vazio = self.html(self.pagina(self.previa, etapa="previa", situacao="rejeitada"))
        self.assertIn("Nenhuma linha neste recorte", vazio)

    def test_a_previa_mostra_as_conferencias_sem_corrigir_nada(self):
        html = self.html(self.pagina(self.previa, etapa="previa", regra="data_compra_fora_de_faixa"))
        self.assertIn("8410008", html)
        self.assertIn("1905", html)                                                # a data entra como veio
        linha_ = self.previa.itens.get(codigo_legado="8410008")
        self.assertEqual(linha_.dados["data_compra"], "1905-07-03")

    def test_a_validacao_agrupa_por_regra_e_diz_que_nada_e_corrigido(self):
        resposta = self.pagina(self.previa, etapa="validacao")
        html = self.html(resposta)
        self.sem_lixo_de_template(html, "validacao")
        codigos = {g["codigo"]: g for g in resposta.context["grupos"]}
        for esperado in ("chassi_duplicado", "data_compra_fora_de_faixa", "ano_modelo_no_futuro", "inicio_antes_da_compra"):
            self.assertIn(esperado, codigos)
        self.assertEqual(codigos["chassi_duplicado"]["n"], 2)
        self.assertIn("Nada aqui é corrigido", html)
        self.assertIn(codigos["chassi_duplicado"]["titulo"], html)
        self.assertIn(reverse("frota_equip_importacao_relatorio", args=[self.previa.pk]) + "?so=inconsistencias", html)

    def test_a_validacao_lista_os_motivos_das_linhas_rejeitadas(self):
        previa = self.previa_enviada([linha(8110001, "A"), linha(8110002, ""), linha(8110003, "C", compra="31/02/2020")])
        self.conferir_pela_tela(previa)
        html = self.html(self.pagina(previa, etapa="validacao"))
        self.assertIn("Linhas rejeitadas", html)
        self.assertIn("O modelo está vazio", html)

    def test_a_validacao_avisa_data_repetida_em_lote(self):
        from datetime import date
        linhas = [linha(8000000 + n, f"T{n}", compra=date(2024, 1, 1)) for n in range(imp.LIMITE_DE_DATA_EM_LOTE)]
        previa = self.previa_conferida(linhas)
        html = self.html(self.pagina(previa, etapa="validacao"))
        self.assertIn("Datas repetidas em lote", html)
        self.assertIn("01/01/2024", html)

    def test_a_validacao_sem_nada_suspeito_diz_isso(self):
        previa = self.previa_conferida([linha(8110001, "TRATOR LIMPO", placa="ABC1D23", chassi="9BWZZZ377VT004251")])
        html = self.html(self.pagina(previa, etapa="validacao"))
        self.assertIn("Nenhuma linha foi rejeitada", html)

    def test_nada_foi_gravado_ate_aqui(self):
        self.pagina(self.previa, etapa="previa")
        self.pagina(self.previa, etapa="validacao")
        self.assertEqual(FrotaVeiculo.objects.count(), 0)


# ═════════════════════════════════════════════════════════════
# Passos 5 e 6: confirmação e resultado
# ═════════════════════════════════════════════════════════════

class ConfirmacaoTests(AssistenteBase):
    def test_a_tela_de_confirmacao_diz_o_que_sera_gravado(self):
        previa = self.previa_conferida()
        resposta = self.pagina(previa, etapa="confirmacao")
        html = self.html(resposta)
        self.sem_lixo_de_template(html, "confirmacao")
        self.assertIn(reverse("frota_equip_importacao_confirmar", args=[previa.pk]), html)
        self.assertIn("Equipamentos novos", html)
        self.assertIn("validação pendente", html)
        self.assertNotIn('name="atualizar"', html)                               # nada já cadastrado difere: nada a autorizar
        self.assertNotIn("disabled", html.split('id="agro-imp-confirmar"')[1].split("</form>")[0])

    def test_confirmar_grava_so_agora_com_validacao_pendente_e_leva_ao_resultado(self):
        previa = self.previa_conferida()
        self.assertEqual(FrotaVeiculo.objects.count(), 0)
        resposta = self.confirmar_pela_tela(previa)
        self.assertRedirects(
            resposta, reverse("frota_equip_importacao", args=[previa.pk]) + "?etapa=resultado", fetch_redirect_response=False,
        )
        self.assertTrue(any("8 importados" in m for m in self.mensagens(resposta)))
        self.assertEqual(FrotaVeiculo.objects.count(), 8)
        self.assertEqual(FrotaVeiculo.objects.filter(validacao_pendente=True).count(), 8)
        previa.refresh_from_db()
        self.assertEqual((previa.estado, previa.importados, previa.confirmada_por), ("confirmada", 8, self.admin))
        self.assertEqual(FrotaVeiculo.objects.get(codigo_legado="8110001").codigo, "MQ-00001")

    def test_o_resultado_mostra_o_que_aconteceu_e_oferece_o_relatorio(self):
        previa = self.importar_pela_tela()
        resposta = self.pagina(previa)
        html = self.html(resposta)
        self.sem_lixo_de_template(html, "resultado")
        self.assertEqual(resposta.context["etapa"], "resultado")
        self.assertIn("Importação concluída", html)
        self.assertIn(reverse("frota_equip_importacao_relatorio", args=[previa.pk]), html)
        self.assertIn(reverse("frota_visao_geral") + "?validacao=pendente", html)
        self.assertEqual(len(resposta.context["linhas"]), 8)

    def test_confirmar_duas_vezes_nao_importa_duas_vezes(self):
        previa = self.previa_conferida()
        self.confirmar_pela_tela(previa)
        segunda = self.confirmar_pela_tela(previa)
        self.assertTrue(any("já foi confirmada" in m for m in self.mensagens(segunda)))
        self.assertEqual(FrotaVeiculo.objects.count(), 8)

    def test_reimportar_o_mesmo_arquivo_nao_cria_nada_e_avisa(self):
        self.importar_pela_tela()
        segunda = self.importar_pela_tela(nome="outra copia.xlsx")
        self.assertEqual((segunda.importados, segunda.existentes), (0, 8))
        self.assertEqual(FrotaVeiculo.objects.count(), 8)
        self.assertIn("já foi importado", self.html(self.pagina(segunda)))

    def test_confirmar_sem_conferir_o_mapeamento_e_recusado(self):
        previa = self.previa_enviada()
        resposta = self.confirmar_pela_tela(previa)
        self.assertTrue(any("Confira o mapeamento" in m for m in self.mensagens(resposta)))
        self.assertEqual(FrotaVeiculo.objects.count(), 0)

    def test_cancelar_descarta_a_previa_e_nada_e_gravado(self):
        previa = self.previa_conferida()
        resposta = self.client.post(reverse("frota_equip_importacao_cancelar", args=[previa.pk]))
        self.assertRedirects(resposta, reverse("frota_equip_importacao", args=[previa.pk]), fetch_redirect_response=False)
        previa.refresh_from_db()
        self.assertEqual(previa.estado, "cancelada")
        html = self.html(self.pagina(previa))
        self.assertIn("Importação cancelada", html)
        self.assertIn("Nada foi gravado", html)
        confirmar = self.confirmar_pela_tela(previa)
        self.assertTrue(any("já foi cancelada" in m for m in self.mensagens(confirmar)))
        self.assertEqual(FrotaVeiculo.objects.count(), 0)

    def test_previa_vencida_nao_se_confirma_e_a_tela_diz_por_que(self):
        previa = self.previa_conferida()
        FrotaImportacaoEquipamentos.objects.filter(pk=previa.pk).update(criada_em=timezone.now() - timedelta(hours=25))
        resposta = self.pagina(previa)
        self.assertEqual(resposta.context["etapa"], "resultado")
        self.assertIn("A prévia expirou", self.html(resposta))
        confirmar = self.confirmar_pela_tela(previa)
        self.assertTrue(any("expirou" in m for m in self.mensagens(confirmar)))
        self.assertEqual(FrotaVeiculo.objects.count(), 0)
        self.assertIn("Expirada", self.html(self.get("frota_equip_importacoes")))

    def test_passo_fora_de_ordem_volta_para_o_que_cabe(self):
        previa = self.previa_enviada()
        for etapa in ("previa", "validacao", "confirmacao", "resultado"):
            with self.subTest(etapa=etapa):
                resposta = self.pagina(previa, etapa=etapa)
                self.assertRedirects(
                    resposta, reverse("frota_equip_importacao", args=[previa.pk]) + "?etapa=mapeamento", fetch_redirect_response=False,
                )
        self.assertTrue(any("Confira o mapeamento" in m for m in self.mensagens(self.pagina(previa, etapa="previa"))))
        self.confirmar_pela_tela(self.previa_conferida())
        concluida = FrotaImportacaoEquipamentos.objects.order_by("-pk").first()
        resposta = self.pagina(concluida, etapa="confirmacao")
        self.assertRedirects(
            resposta, reverse("frota_equip_importacao", args=[concluida.pk]) + "?etapa=resultado", fetch_redirect_response=False,
        )


class AtualizacaoDosJaCadastradosTests(AssistenteBase):
    """Equipamento que já existe só muda com autorização explícita, e só nos dados que o ERP conhece."""

    def setUp(self):
        super().setUp()
        self.importar_pela_tela()
        self.alterada = [
            linha(8110001, "TRATOR NH 3880", placa="ABC1D23", chassi="1CQS660ALH0120143"),
            *[l for l in planilha_do_erp()[1:]],
        ]

    def test_a_previa_mostra_o_que_a_planilha_mudaria(self):
        previa = self.previa_conferida(self.alterada)
        resposta = self.pagina(previa, etapa="previa", situacao="existente")
        html = self.html(resposta)
        self.assertIn("A planilha mudaria", html)
        self.assertIn("ABC1D23", html)
        mudou = next(l for l in resposta.context["linhas"] if l["codigo_legado"] == "8110001")
        self.assertEqual([m["rotulo"] for m in mudou["mudancas"]], ["Placa"])
        self.assertEqual(imp.resumo(previa)["com_diferencas"], 1)

    def test_ja_cadastrada_nao_e_pintada_como_erro_e_so_a_rejeitada_e(self):
        """"Já cadastrada" tem um motivo (o equipamento não é alterado), mas não é falha: vermelho é só da linha rejeitada. 395
        linhas vermelhas numa reimportação fariam a tela parecer um alarme que ninguém decidiu."""
        previa = self.previa_conferida([*self.alterada, linha(9900009, "")])
        html = self.html(self.pagina(previa, etapa="previa", situacao="existente"))
        self.assertIn("Equipamento já cadastrado", html)
        self.assertNotIn("m2m-motivos--erro", html)
        rejeitadas = self.html(self.pagina(previa, etapa="previa", situacao="rejeitada"))
        self.assertIn("m2m-motivos--erro", rejeitadas)
        self.assertIn("O modelo está vazio", rejeitadas)

    def test_sem_autorizacao_o_equipamento_fica_como_esta(self):
        previa = self.previa_conferida(self.alterada)
        html = self.html(self.pagina(previa, etapa="confirmacao"))
        self.assertIn('name="atualizar"', html)
        self.assertIn("O que mudaria nos já cadastrados", html)
        self.confirmar_pela_tela(previa)
        previa.refresh_from_db()
        self.assertEqual((previa.importados, previa.atualizados, previa.atualizar_existentes), (0, 0, False))
        self.assertEqual(FrotaVeiculo.objects.get(codigo_legado="8110001").placa, "")

    def test_com_autorizacao_so_a_placa_muda_e_o_resultado_diz_o_que_foi_aplicado(self):
        previa = self.previa_conferida(self.alterada)
        self.confirmar_pela_tela(previa, atualizar="1")
        previa.refresh_from_db()
        self.assertEqual((previa.atualizados, previa.atualizar_existentes), (1, True))
        veiculo = FrotaVeiculo.objects.get(codigo_legado="8110001")
        self.assertEqual((veiculo.placa, veiculo.nome), ("ABC1D23", "TRATOR NH 3880"))
        html = self.html(self.pagina(previa))
        self.assertIn("Atualizado:", html)
        self.assertIn("Com autorização para atualizar", html)

    def test_sem_nada_a_importar_nem_atualizar_o_botao_fica_desligado(self):
        previa = self.previa_conferida()                                          # a mesma planilha, já importada
        html = self.html(self.pagina(previa, etapa="confirmacao"))
        formulario = html.split('id="agro-imp-confirmar"')[1].split("</form>")[0]
        self.assertIn("disabled", formulario)
        self.assertNotIn('name="atualizar"', formulario)


# ═════════════════════════════════════════════════════════════
# Passo a passo, relatório e segurança
# ═════════════════════════════════════════════════════════════

class PassosTests(AssistenteBase):
    def estados(self, resposta):
        return {p["chave"]: p["estado"] for p in resposta.context["passos"]}

    def links(self, resposta):
        return {p["chave"]: p["url"] for p in resposta.context["passos"]}

    def test_no_mapeamento_so_o_envio_esta_concluido_e_os_demais_estao_fechados(self):
        previa = self.previa_enviada()
        resposta = self.pagina(previa)
        self.assertEqual(self.estados(resposta), {
            "envio": "concluido", "mapeamento": "atual", "previa": "pendente", "validacao": "pendente", "confirmacao": "pendente",
            "resultado": "pendente",
        })
        links = self.links(resposta)
        self.assertEqual((links["previa"], links["validacao"], links["confirmacao"], links["resultado"]), ("", "", "", ""))

    def test_depois_de_conferir_os_passos_seguintes_abrem(self):
        previa = self.previa_conferida()
        resposta = self.pagina(previa, etapa="validacao")
        self.assertEqual(self.estados(resposta), {
            "envio": "concluido", "mapeamento": "concluido", "previa": "concluido", "validacao": "atual", "confirmacao": "pendente",
            "resultado": "pendente",
        })
        links = self.links(resposta)
        self.assertTrue(links["confirmacao"].endswith("?etapa=confirmacao"))
        self.assertEqual(links["resultado"], "")
        self.assertIn(f'href="{links["previa"]}"', resposta.content.decode("utf-8"))

    def test_confirmada_tudo_esta_concluido_e_cancelada_so_o_que_aconteceu(self):
        confirmada = self.importar_pela_tela()
        self.assertEqual(set(self.estados(self.pagina(confirmada)).values()), {"concluido", "atual"})
        self.assertEqual(self.estados(self.pagina(confirmada))["resultado"], "atual")
        cancelada = self.previa_conferida([linha(8000001, "OUTRA")])
        self.client.post(reverse("frota_equip_importacao_cancelar", args=[cancelada.pk]))
        self.assertEqual(self.estados(self.pagina(cancelada)), {
            "envio": "concluido", "mapeamento": "concluido", "previa": "pendente", "validacao": "pendente", "confirmacao": "pendente",
            "resultado": "atual",
        })


class RelatorioETextoTests(AssistenteBase):
    def test_o_relatorio_baixa_em_excel_e_o_de_inconsistencias_so_leva_o_que_importa(self):
        previa = self.previa_conferida()
        completo = self.client.get(reverse("frota_equip_importacao_relatorio", args=[previa.pk]))
        self.assertEqual(completo["Content-Type"], TIPO_XLSX)
        self.assertIn(f"relatorio_importacao_{previa.pk}_", completo["Content-Disposition"])
        self.assertEqual(load_workbook(io.BytesIO(completo.content)).sheetnames, ["Resumo", "Linhas", "Inconsistências", "Mapeamento"])
        so_inconsistencias = self.client.get(reverse("frota_equip_importacao_relatorio", args=[previa.pk]), {"so": "inconsistencias"})
        self.assertIn(f"inconsistencias_importacao_{previa.pk}_", so_inconsistencias["Content-Disposition"])
        self.assertEqual(load_workbook(io.BytesIO(so_inconsistencias.content)).sheetnames, ["Resumo", "Inconsistências"])

    def test_o_relatorio_vale_em_qualquer_estado(self):
        enviada = self.previa_enviada()
        self.assertEqual(self.client.get(reverse("frota_equip_importacao_relatorio", args=[enviada.pk])).status_code, 200)
        confirmada = self.importar_pela_tela([linha(8000001, "OUTRA")])
        self.assertEqual(self.client.get(reverse("frota_equip_importacao_relatorio", args=[confirmada.pk])).status_code, 200)

    def test_o_que_vem_da_planilha_aparece_escapado(self):
        previa = self.previa_conferida([linha(8110001, "<script>alert(1)</script>", setor="<img src=x onerror=alert(2)>")])
        for etapa in ("previa", "validacao", "confirmacao"):
            with self.subTest(etapa=etapa):
                html = self.html(self.pagina(previa, etapa=etapa))
                self.assertNotIn("<script>alert(1)</script>", html)
                self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", self.html(self.pagina(previa, etapa="previa")))
        self.assertNotIn("<script>alert(1)</script>", self.html(self.pagina(previa, etapa="mapeamento")))

    def test_nome_de_arquivo_com_html_nao_vaza_para_a_tela(self):
        previa = self.previa_enviada(nome="<b>x</b>.xlsx")
        self.assertNotIn("<b>x</b>", self.html(self.pagina(previa)))
        self.assertNotIn("<b>x</b>", self.html(self.get("frota_equip_importar")))

    def test_formula_na_planilha_entra_como_texto(self):
        previa = self.previa_conferida([linha(8110001, "=HYPERLINK(\"http://x\")")])
        self.assertEqual(previa.itens.get().dados["modelo"], "=HYPERLINK(\"http://x\")")
        self.assertEqual(previa.itens.get().situacao, "valida")


# ═════════════════════════════════════════════════════════════
# Aceite: a planilha de verdade, pela tela
# ═════════════════════════════════════════════════════════════

from pathlib import Path  # noqa: E402
from unittest import skipUnless  # noqa: E402

from django.conf import settings  # noqa: E402

PLANILHA_REAL = Path(settings.BASE_DIR).parent / "docs" / "equipamentos motorizados.xlsx"


@skipUnless(PLANILHA_REAL.exists(), "a planilha equipamentos motorizados.xlsx não está em docs/")
class PlanilhaRealPelaTelaTests(AssistenteBase):
    """O caminho inteiro de quem usa: envia o arquivo do ERP, confere o mapeamento, confirma e consulta — com os números da
    análise do prompt. É o aceite dos cenários 1 (importar sem duplicar), 2 (preservar os códigos), 3 (próprio × terceiro),
    5 (filtrar e ordenar) e 11 (indicadores da base real), pela camada web."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.bytes_da_planilha = PLANILHA_REAL.read_bytes()

    def importar_a_real(self):
        resposta = self.client.post(reverse("frota_equip_importar"), {"arquivo": upload(PLANILHA_REAL.name, self.bytes_da_planilha)})
        self.assertEqual(resposta.status_code, 302)
        importacao = FrotaImportacaoEquipamentos.objects.order_by("-pk").first()
        self.assertEqual(self.conferir_pela_tela(importacao).status_code, 302)
        self.assertEqual(self.confirmar_pela_tela(importacao).status_code, 302)
        importacao.refresh_from_db()
        return importacao

    def test_o_mapeamento_da_planilha_real_vem_pronto_e_a_previa_nao_rejeita_nada(self):
        resposta = self.client.post(reverse("frota_equip_importar"), {"arquivo": upload(PLANILHA_REAL.name, self.bytes_da_planilha)})
        importacao = FrotaImportacaoEquipamentos.objects.order_by("-pk").first()
        self.assertEqual(resposta.status_code, 302)
        html = self.html(self.pagina(importacao))
        self.assertNotIn("não tem a coluna Empresa", html)
        self.assertEqual({c["situacao"] for c in self.pagina(importacao).context["painel"]["colunas"]}, {"mapeada"})
        self.conferir_pela_tela(importacao)
        importacao.refresh_from_db()
        self.assertEqual((importacao.linhas, importacao.validas, importacao.rejeitados), (395, 395, 0))
        self.assertEqual(FrotaVeiculo.objects.count(), 0)                                  # nada gravado até confirmar

    def test_a_previa_e_a_validacao_mostram_as_ocorrencias_do_prompt_sem_corrigir_nenhuma(self):
        resposta = self.client.post(reverse("frota_equip_importar"), {"arquivo": upload(PLANILHA_REAL.name, self.bytes_da_planilha)})
        importacao = FrotaImportacaoEquipamentos.objects.order_by("-pk").first()
        self.assertEqual(resposta.status_code, 302)
        self.conferir_pela_tela(importacao)
        grupos = {g["codigo"]: g for g in self.pagina(importacao, etapa="validacao").context["grupos"]}
        self.assertEqual({e["codigo_legado"] for e in grupos["chassi_duplicado"]["equipamentos"]}, {"8110001", "8210001"})
        self.assertIn("8410008", {e["codigo_legado"] for e in grupos["data_compra_fora_de_faixa"]["equipamentos"]})
        self.assertIn("9100592", {e["codigo_legado"] for e in grupos["ano_modelo_no_futuro"]["equipamentos"]})
        previa = self.pagina(importacao, etapa="previa", q="8410008")
        self.assertEqual([l["data_compra"] for l in previa.context["linhas"]], ["1905-07-03"])     # a data entra como veio

    def test_importar_395_preserva_os_codigos_diferencia_a_propriedade_e_nao_duplica_na_segunda_vez(self):
        primeira = self.importar_a_real()
        self.assertEqual((primeira.estado, primeira.importados, primeira.rejeitados), ("confirmada", 395, 0))
        self.assertEqual(FrotaVeiculo.objects.count(), 395)
        self.assertEqual(FrotaVeiculo.objects.exclude(codigo_legado="").count(), 395)
        self.assertEqual(FrotaVeiculo.objects.filter(natureza="proprio").count(), 227)
        self.assertEqual(FrotaVeiculo.objects.filter(natureza="terceiro").count(), 168)
        segunda = self.importar_a_real()
        self.assertEqual((segunda.importados, segunda.existentes), (0, 395))
        self.assertEqual(FrotaVeiculo.objects.count(), 395)
        self.assertIn("já foi importado", self.html(self.pagina(segunda)))

    def test_depois_de_importar_a_tela_principal_e_os_relatorios_mostram_a_base_real(self):
        self.importar_a_real()
        principal = self.get("frota_visao_geral")
        k = principal.context["kpis"]
        self.assertEqual((k["total"], k["proprios"], k["terceiros"], k["em_uso"], k["a_validar"]), (395, 227, 168, 395, 395))
        self.assertEqual(self.get("frota_equipamentos_terceiros").context["total_filtrado"], 168)
        self.assertEqual(self.get("frota_visao_geral", q="NZH4261").context["total_filtrado"], 1)
        self.assertEqual(self.get("frota_visao_geral", q="1CQS660ALH0120143").context["total_filtrado"], 2)
        relatorios = self.get("frota_relatorios")
        self.assertEqual(sum(c["total"] for c in relatorios.context["por_categoria"]), 395)
        self.assertEqual(sum(c["total"] for c in relatorios.context["por_classe"]), 395)
        self.assertEqual(len([c for c in relatorios.context["por_classe"] if c["classe_id"]]), 28)
        self.assertIn("MOTOCICLETA", self.html(relatorios))
