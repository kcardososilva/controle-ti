"""
Chamados de estrutura da contratação — pedir à TI, por e-mail, a máquina, o
usuário/e-mail e o acesso aos sistemas do novo colaborador.

O que cada bloco protege. Quase todas são falhas que NÃO derrubam tela: a página
abre, o botão clica, a mensagem diz "ok" — e o defeito está no que NÃO aconteceu
ou no que aconteceu em dobro.

  • DUPLICIDADE — clique repetido, duas abas ou reenvio gerando o mesmo chamado
    duas vezes na caixa da TI. Quem segura é a constraint do banco.
  • FALHA ISOLADA — um e-mail que não sai não pode impedir os outros, e o
    reenvio só pode tocar no que falhou (o que saiu não sai de novo).
  • HONESTIDADE — "e-mail enviado" não é "chamado aberto". O sistema só afirma
    que o chamado existe quando o TI registra o número.
  • AUTORIZAÇÃO NAS 3 CAMADAS — caminho (middleware), ação (decorador) e service.
    Esconder o botão não é autorização.
  • STATUS — só a vaga EM ABERTO pede estrutura, e pedir não muda o status.
  • DESTINATÁRIO — vem da Central de Alertas; nenhum endereço no código.

Os e-mails vão para `mail.outbox` (o runner troca o backend por locmem): nada
sai de verdade.
"""

from datetime import timedelta
from smtplib import SMTPException
from unittest import mock

from django.contrib.auth.models import Group, User
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.core.mail import EmailMultiAlternatives
from django.db import IntegrityError, connection, transaction
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from services import acesso_restrito_service as ar
from services import chamado_contratacao_service as svc
from services import email_alertas

from ProjetoEstoque.models import (
    CanalNotificacao,
    CentroCusto,
    Contratacao,
    ContratacaoChamado,
    ContratacaoChamadoTentativa,
    ContratacaoEvento,
    ContratacaoSistema,
    GRUPO_CONSULTA_QUIOSQUE,
    GRUPO_FORNECEDOR,
    GRUPO_PORTAL_CONTRATACAO,
    Localidade,
    SistemaCorporativo,
    StatusChamadoContratacao as Status,
    StatusContratacaoChoices,
    TipoChamadoContratacao as Tipo,
)
from ProjetoEstoque.views.contratacoes import FILA_ESTRUTURA_LIMITE

DESTINO = {
    Tipo.MAQUINA: "maquinas@teste.invalid",
    Tipo.USUARIO_EMAIL: "acessos@teste.invalid",
    Tipo.SISTEMAS: "sistemas@teste.invalid",
}


def _login_restrito(username, grupo):
    user = User.objects.create_user(username, password="x", first_name=username.title())
    user.groups.add(Group.objects.get(name=grupo))
    return user


class ChamadosBase(TestCase):
    def setUp(self):
        cache.clear()
        self.ti = User.objects.create_user("ti", password="x", is_staff=True, first_name="Tiago", email="tiago@teste.invalid")
        self.rh = _login_restrito("rh.maria", GRUPO_PORTAL_CONTRATACAO)
        self.rh.email = "maria@teste.invalid"
        self.rh.save()
        self.olheiro = _login_restrito("consulta.joao", GRUPO_CONSULTA_QUIOSQUE)

        # A Central de Alertas define QUEM recebe cada tipo. Endereços de teste,
        # nunca os do .env: o resultado não pode depender da máquina que roda.
        email_alertas.sincronizar_catalogo_notificacoes()
        for tipo, endereco in DESTINO.items():
            CanalNotificacao.objects.filter(codigo=email_alertas.CANAL_CHAMADO_CONTRATACAO[tipo]).update(
                destinatarios_customizados_ativo=True, destinatarios_customizados=endereco,
            )

        self.cc = CentroCusto.objects.create(numero="100", departamento="Tecnologia")
        self.local = Localidade.objects.create(local="Fazenda Sol Nascente")
        self.sistema, _ = SistemaCorporativo.objects.get_or_create(
            nome="TOTVS - Datasul", defaults={"ordem": 10, "exige_perfil_espelho": True},
        )
        self.vaga = Contratacao.objects.create(
            cargo="Analista de Dados", tipo_vaga="nova", computador="notebook",
            computador_justificativa="16 GB de RAM", telefonia="ramal",
            centro_custo=self.cc, localidade=self.local,
            colaborador_nome="Carlos Souza", perfil_espelho="ana.lima",
            criado_por=self.ti, atualizado_por=self.ti,
        )
        ContratacaoSistema.objects.create(
            contratacao=self.vaga, sistema=self.sistema, necessario=True, perfil_espelho="ana.lima",
        )
        mail.outbox = []

    def tearDown(self):
        cache.clear()

    # ── helpers ──
    def url_solicitar(self, vaga=None):
        return reverse("contratacao_chamados_solicitar", args=[(vaga or self.vaga).pk])

    def pedir(self, user, *, vaga=None, **campos):
        self.client.force_login(user)
        return self.client.post(self.url_solicitar(vaga), campos)

    TODOS = {
        "maquina": "on", "maquina_descricao": "Notebook + headset",
        "usuario_email": "on", "criar_usuario_rede": "on", "criar_email": "on",
        "sistemas": "on", "sistemas_suporte": ["orientacao", "liberacao"],
    }

    def chamado(self, tipo):
        return ContratacaoChamado.objects.get(contratacao=self.vaga, tipo=tipo)


# ─────────────────────────────────────────────────────────────
# Cenário 2 e 3 — solicitação completa e parcial
# ─────────────────────────────────────────────────────────────

class SolicitacaoTests(ChamadosBase):
    def test_solicitacao_completa_envia_tres_emails_independentes(self):
        resp = self.pedir(self.rh, **self.TODOS)
        self.assertEqual(resp.status_code, 302)

        self.assertEqual(len(mail.outbox), 3)
        assuntos = [m.subject for m in mail.outbox]
        self.assertEqual(len(set(assuntos)), 3, "os três assuntos têm de ser diferentes")
        self.assertTrue(any("Nova solicitação de preparação de equipamento" in a for a in assuntos))
        self.assertTrue(any("Solicitação de criação de acessos corporativos" in a for a in assuntos))
        self.assertTrue(any("Solicitação de acessos e integração aos sistemas" in a for a in assuntos))
        # Cada um vai para o destinatário DO SEU tipo.
        for m in mail.outbox:
            esperado = (
                DESTINO[Tipo.MAQUINA] if "preparação de equipamento" in m.subject
                else DESTINO[Tipo.USUARIO_EMAIL] if "acessos corporativos" in m.subject
                else DESTINO[Tipo.SISTEMAS]
            )
            self.assertEqual(m.to, [esperado])

        for tipo in DESTINO:
            self.assertEqual(self.chamado(tipo).status, Status.ENVIADO)

    def test_cada_email_traz_os_dados_da_vaga_e_o_responsavel(self):
        self.pedir(self.rh, **self.TODOS)
        for m in mail.outbox:
            corpo = m.body
            self.assertIn("Analista de Dados", corpo)
            self.assertIn("Tecnologia", corpo)            # departamento
            self.assertIn("Fazenda Sol Nascente", corpo)  # unidade
            self.assertIn(f"Nº {self.vaga.pk}", corpo)    # identificação da vaga
            self.assertIn("Rh.Maria (maria@teste.invalid)", corpo)  # responsável
            self.assertIn("Abertura:", corpo)
            self.assertIn("Enviado em:", corpo)
            self.assertIn("Área responsável: Tecnologia da Informação (TI)", corpo)
            # Versão HTML no padrão institucional das demais comunicações.
            html = m.alternatives[0][0]
            self.assertIn("Santa Colomba", html)

    def test_cada_email_so_leva_o_que_a_atividade_precisa(self):
        """O nome do contratado vai nos três tipos (a TI não prepara máquina, login nem
        acesso para "uma pessoa") — mas nenhum outro dado pessoal."""
        self.pedir(self.rh, **self.TODOS)
        por_tipo = {t: next(m for m in mail.outbox if m.to == [d]) for t, d in DESTINO.items()}
        for tipo in DESTINO:
            self.assertIn("Carlos Souza", por_tipo[tipo].body)
            self.assertIn("Carlos Souza", por_tipo[tipo].subject)
            for proibido in ("CPF", "RG", "telefone", "senha"):
                self.assertNotIn(proibido, por_tipo[tipo].body)
        # Conteúdo específico de cada tipo.
        self.assertIn("Notebook + headset", por_tipo[Tipo.MAQUINA].body)
        self.assertIn("ana.lima", por_tipo[Tipo.USUARIO_EMAIL].body)
        self.assertIn("TOTVS - Datasul", por_tipo[Tipo.SISTEMAS].body)
        self.assertIn("Liberação de acesso", por_tipo[Tipo.SISTEMAS].body)

    def test_texto_livre_e_escapado_no_html(self):
        """Quem digita pode ser terceiro do Portal; o e-mail vai para a caixa da TI."""
        self.pedir(self.rh, **{**self.TODOS, "maquina_descricao": "<script>alert(1)</script>"})
        html = next(m for m in mail.outbox if m.to == [DESTINO[Tipo.MAQUINA]]).alternatives[0][0]
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_solicitacao_parcial_so_envia_o_que_foi_marcado(self):
        self.pedir(self.rh, maquina="on", maquina_descricao="Notebook")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(list(ContratacaoChamado.objects.values_list("tipo", flat=True)), [Tipo.MAQUINA])

    def test_segunda_solicitacao_acrescenta_sem_reenviar_a_maquina(self):
        self.pedir(self.rh, maquina="on")
        self.pedir(self.rh, maquina="on", usuario_email="on", criar_email="on")
        self.assertEqual(len(mail.outbox), 2, "a máquina não pode sair de novo")
        self.assertEqual(
            sorted(ContratacaoChamado.objects.values_list("tipo", flat=True)),
            sorted([Tipo.MAQUINA, Tipo.USUARIO_EMAIL]),
        )

    def test_nao_gera_chamado_de_usuario_nem_de_sistemas_quando_nao_marcados(self):
        self.pedir(self.rh, maquina="on")
        self.assertFalse(ContratacaoChamado.objects.filter(tipo=Tipo.USUARIO_EMAIL).exists())
        self.assertFalse(ContratacaoChamado.objects.filter(tipo=Tipo.SISTEMAS).exists())

    def test_pedir_estrutura_nao_muda_o_status_da_vaga(self):
        antes = Contratacao.objects.values().get(pk=self.vaga.pk)
        self.pedir(self.rh, **self.TODOS)
        depois = Contratacao.objects.values().get(pk=self.vaga.pk)
        for campo in ("status", "vaga_preenchida_em", "equipamento_entregue_em", "concluida_em", "cargo"):
            self.assertEqual(antes[campo], depois[campo], campo)
        self.assertEqual(depois["status"], StatusContratacaoChoices.ABERTA)

    def test_ti_tambem_pode_solicitar(self):
        self.pedir(self.ti, maquina="on")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(self.chamado(Tipo.MAQUINA).criado_por, self.ti)


class RegrasDoPedidoTests(ChamadosBase):
    def test_maquina_e_obrigatoria(self):
        resp = self.pedir(self.rh, usuario_email="on", criar_email="on")
        self.assertEqual(resp.status_code, 200)  # a janela volta aberta, com o erro
        self.assertContains(resp, "obrigatória")
        self.assertFalse(ContratacaoChamado.objects.exists())
        self.assertEqual(mail.outbox, [])

    def test_maquina_deixa_de_ser_obrigatoria_se_a_vaga_nao_precisa_de_computador(self):
        self.vaga.computador = "nenhum"
        self.vaga.save()
        self.pedir(self.rh, usuario_email="on", criar_email="on")
        self.assertEqual(len(mail.outbox), 1)
        self.assertFalse(ContratacaoChamado.objects.filter(tipo=Tipo.MAQUINA).exists())

    def test_maquina_ja_pedida_nao_e_exigida_de_novo(self):
        self.pedir(self.rh, maquina="on")
        resp = self.pedir(self.rh, usuario_email="on", criar_usuario_rede="on")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(mail.outbox), 2)

    def test_nenhum_chamado_marcado_e_recusado(self):
        resp = self.pedir(self.rh)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mail.outbox, [])

    def test_usuario_email_exige_dizer_o_que_criar(self):
        resp = self.pedir(self.rh, maquina="on", usuario_email="on")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "marque o que precisa ser criado")
        self.assertFalse(ContratacaoChamado.objects.exists(), "recusa não pode deixar a máquina pela metade")

    def test_sistemas_exige_tipo_de_suporte(self):
        resp = self.pedir(self.rh, maquina="on", sistemas="on")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(ContratacaoChamado.objects.exists())

    def test_sistemas_sem_sistema_na_vaga_exige_descricao(self):
        self.vaga.sistemas.all().delete()
        resp = self.pedir(self.rh, maquina="on", sistemas="on", sistemas_suporte=["orientacao"])
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(ContratacaoChamado.objects.exists())
        self.pedir(self.rh, maquina="on", sistemas="on", sistemas_suporte=["orientacao"],
                   sistemas_obs="Vai usar o Tekann")
        self.assertEqual(len(mail.outbox), 2)

    def test_recusa_devolve_a_janela_aberta_com_o_que_foi_digitado(self):
        resp = self.pedir(self.rh, usuario_email="on", usuario_email_obs="texto digitado")
        self.assertContains(resp, 'data-abrir="1"')
        self.assertContains(resp, "texto digitado")


# ─────────────────────────────────────────────────────────────
# Cenário 4 — duplicidade
# ─────────────────────────────────────────────────────────────

class DuplicidadeTests(ChamadosBase):
    def test_clique_repetido_nao_duplica_chamado_nem_email(self):
        for _ in range(3):
            self.pedir(self.rh, **self.TODOS)
        self.assertEqual(len(mail.outbox), 3)
        self.assertEqual(ContratacaoChamado.objects.count(), 3)
        # O que foi pedido antes continua registrado, com UMA tentativa cada.
        self.assertEqual(ContratacaoChamadoTentativa.objects.count(), 3)

    def test_repeticao_avisa_que_ja_estava_solicitado(self):
        self.pedir(self.rh, maquina="on")
        resp = self.client.post(self.url_solicitar(), {"maquina": "on"}, follow=True)
        textos = " ".join(str(m) for m in resp.context["messages"])
        self.assertIn("já tinham sido solicitados", textos)

    def test_o_banco_recusa_dois_chamados_do_mesmo_tipo_na_mesma_vaga(self):
        ContratacaoChamado.objects.create(contratacao=self.vaga, tipo=Tipo.MAQUINA)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ContratacaoChamado.objects.create(contratacao=self.vaga, tipo=Tipo.MAQUINA)

    def test_corrida_perdida_vira_ja_solicitado_e_nao_derruba_os_outros(self):
        """Duas requisições passam pela checagem juntas: a segunda esbarra no
        INSERT. Simula o IntegrityError só no tipo máquina."""
        original = ContratacaoChamado.objects.create

        def create(**kw):
            if kw.get("tipo") == Tipo.MAQUINA:
                raise IntegrityError("duplicate")
            return original(**kw)

        with mock.patch.object(ContratacaoChamado.objects, "create", side_effect=create):
            r = svc.solicitar(
                contratacao_pk=self.vaga.pk, user=self.rh,
                selecao={"maquina": {"descricao": ""},
                         "usuario_email": {"criar_email": True, "criar_usuario_rede": False, "observacao": ""}},
            )
        self.assertEqual(r.ja_solicitados, [Tipo.MAQUINA])
        self.assertEqual([c.tipo for c in r.enviados], [Tipo.USUARIO_EMAIL])
        self.assertEqual(len(mail.outbox), 1)


# ─────────────────────────────────────────────────────────────
# Cenário 5 — falha de envio
# ─────────────────────────────────────────────────────────────

def _smtp_que_falha_para(trecho_do_assunto):
    original = EmailMultiAlternatives.send

    def send(self, fail_silently=False):
        if trecho_do_assunto in self.subject:
            raise SMTPException("servidor recusou a conexão")
        return original(self, fail_silently)
    return send


class FalhaDeEnvioTests(ChamadosBase):
    def _pedir_com_falha_no_usuario(self):
        with mock.patch.object(EmailMultiAlternatives, "send", _smtp_que_falha_para("criação de acessos corporativos")):
            self.pedir(self.rh, **self.TODOS)

    def test_falha_de_um_nao_impede_os_outros(self):
        self._pedir_com_falha_no_usuario()
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(self.chamado(Tipo.MAQUINA).status, Status.ENVIADO)
        self.assertEqual(self.chamado(Tipo.SISTEMAS).status, Status.ENVIADO)
        falho = self.chamado(Tipo.USUARIO_EMAIL)
        self.assertEqual(falho.status, Status.FALHA)
        self.assertIn("servidor recusou a conexão", falho.ultimo_erro)

    def test_falha_fica_no_historico_de_tentativas(self):
        self._pedir_com_falha_no_usuario()
        t = self.chamado(Tipo.USUARIO_EMAIL).historico.get()
        self.assertFalse(t.sucesso)
        self.assertEqual(t.criado_por, self.rh)
        self.assertIn("servidor recusou", t.erro)

    def test_reenvio_so_toca_no_que_falhou(self):
        self._pedir_com_falha_no_usuario()
        self.assertEqual(len(mail.outbox), 2)

        chamado = svc.reenviar(
            contratacao_pk=self.vaga.pk, chamado_pk=self.chamado(Tipo.USUARIO_EMAIL).pk, user=self.rh,
        )
        self.assertEqual(chamado.status, Status.ENVIADO)
        self.assertEqual(len(mail.outbox), 3, "só o que falhou sai de novo")
        self.assertIn("(reenvio)", mail.outbox[-1].subject)
        self.assertEqual(chamado.tentativas, 2)
        self.assertEqual(chamado.historico.count(), 2)
        # Os que já tinham saído seguem com uma tentativa só.
        self.assertEqual(self.chamado(Tipo.MAQUINA).tentativas, 1)

    def test_reenviar_o_que_ja_foi_enviado_e_recusado(self):
        self.pedir(self.rh, maquina="on")
        with self.assertRaises(svc.SolicitacaoRecusada):
            svc.reenviar(contratacao_pk=self.vaga.pk, chamado_pk=self.chamado(Tipo.MAQUINA).pk, user=self.rh)
        self.assertEqual(len(mail.outbox), 1)

    def test_botao_de_reenvio_pela_tela_nao_duplica(self):
        self._pedir_com_falha_no_usuario()
        url = reverse("contratacao_chamado_reenviar", args=[self.vaga.pk, self.chamado(Tipo.USUARIO_EMAIL).pk])
        self.client.post(url)
        self.client.post(url)  # segundo clique
        self.assertEqual(len(mail.outbox), 3)

    def test_reenvio_que_falha_de_novo_registra_a_segunda_tentativa(self):
        self._pedir_com_falha_no_usuario()
        with mock.patch.object(EmailMultiAlternatives, "send", _smtp_que_falha_para("criação de acessos corporativos")):
            c = svc.reenviar(
                contratacao_pk=self.vaga.pk, chamado_pk=self.chamado(Tipo.USUARIO_EMAIL).pk, user=self.ti,
            )
        self.assertEqual(c.status, Status.FALHA)
        self.assertEqual(c.tentativas, 2)
        self.assertEqual(c.historico.count(), 2)

    def test_defeito_ao_montar_a_mensagem_tambem_vira_falha_e_nao_estoura(self):
        with mock.patch.object(email_alertas, "montar_chamado_contratacao", side_effect=KeyError("x")):
            self.pedir(self.rh, maquina="on", usuario_email="on", criar_email="on")
        self.assertEqual(self.chamado(Tipo.MAQUINA).status, Status.FALHA)
        self.assertEqual(self.chamado(Tipo.USUARIO_EMAIL).status, Status.FALHA)
        self.assertEqual(mail.outbox, [])

    def test_envio_interrompido_pode_ser_retomado_mas_o_recente_nao(self):
        c = ContratacaoChamado.objects.create(
            contratacao=self.vaga, tipo=Tipo.MAQUINA, status=Status.PENDENTE,
            dados={"vaga_id": self.vaga.pk, "cargo": "X"}, criado_por=self.rh,
        )
        self.assertFalse(c.reenviavel, "pendente recente = envio em andamento")
        with self.assertRaises(svc.SolicitacaoRecusada):
            svc.reenviar(contratacao_pk=self.vaga.pk, chamado_pk=c.pk, user=self.rh)

        ContratacaoChamado.objects.filter(pk=c.pk).update(updated_at=timezone.now() - timedelta(minutes=30))
        c.refresh_from_db()
        self.assertTrue(c.reenviavel)
        self.assertTrue(c.interrompido)
        svc.reenviar(contratacao_pk=self.vaga.pk, chamado_pk=c.pk, user=self.rh)
        c.refresh_from_db()
        self.assertEqual(c.status, Status.ENVIADO)


# ─────────────────────────────────────────────────────────────
# Honestidade: enviado ≠ aberto
# ─────────────────────────────────────────────────────────────

class HonestidadeTests(ChamadosBase):
    def test_enviado_sem_numero_nao_afirma_que_o_chamado_foi_aberto(self):
        self.pedir(self.rh, maquina="on")
        self.client.force_login(self.rh)
        html = self.client.get(reverse("contratacao_detail", args=[self.vaga.pk])).content.decode()
        self.assertIn("E-mail enviado com sucesso", html)
        self.assertIn("Ainda sem confirmação de", html)
        self.assertNotIn("registrado pelo TI", html)
        self.assertFalse(self.chamado(Tipo.MAQUINA).confirmado)

    def test_ti_registra_o_numero_e_so_entao_o_chamado_consta_confirmado(self):
        self.pedir(self.rh, maquina="on")
        c = self.chamado(Tipo.MAQUINA)
        self.client.force_login(self.ti)
        self.client.post(
            reverse("contratacao_chamado_identificador", args=[self.vaga.pk, c.pk]),
            {"identificador": "48213"},
        )
        c.refresh_from_db()
        self.assertTrue(c.confirmado)
        html = self.client.get(reverse("contratacao_detail", args=[self.vaga.pk])).content.decode()
        self.assertIn("Chamado nº 48213 registrado pelo TI", html)

    def test_solicitante_nao_registra_numero(self):
        self.pedir(self.rh, maquina="on")
        c = self.chamado(Tipo.MAQUINA)
        resp = self.client.post(
            reverse("contratacao_chamado_identificador", args=[self.vaga.pk, c.pk]), {"identificador": "1"},
        )
        self.assertEqual(resp.status_code, 403)
        c.refresh_from_db()
        self.assertEqual(c.identificador, "")

    def test_numero_so_vale_para_chamado_cujo_email_saiu(self):
        c = ContratacaoChamado.objects.create(
            contratacao=self.vaga, tipo=Tipo.MAQUINA, status=Status.FALHA, dados={},
        )
        with self.assertRaises(svc.SolicitacaoRecusada):
            svc.registrar_identificador(
                contratacao_pk=self.vaga.pk, chamado_pk=c.pk, identificador="9", user=self.ti,
            )

    def test_o_painel_distingue_os_quatro_estados(self):
        ContratacaoChamado.objects.create(contratacao=self.vaga, tipo=Tipo.MAQUINA, status=Status.ENVIADO, dados={})
        ContratacaoChamado.objects.create(contratacao=self.vaga, tipo=Tipo.USUARIO_EMAIL, status=Status.FALHA, dados={})
        ContratacaoChamado.objects.create(contratacao=self.vaga, tipo=Tipo.SISTEMAS, status=Status.PENDENTE, dados={})
        vaga = Contratacao.objects.prefetch_related("chamados").get(pk=self.vaga.pk)
        estados = {l["tipo"]: l["estado_rotulo"] for l in svc.painel_de_chamados(vaga)}
        self.assertEqual(estados[Tipo.MAQUINA], "E-mail enviado com sucesso")
        self.assertEqual(estados[Tipo.USUARIO_EMAIL], "Falha no envio")
        self.assertEqual(estados[Tipo.SISTEMAS], "Envio pendente")

        ContratacaoChamado.objects.all().delete()
        vaga = Contratacao.objects.prefetch_related("chamados").get(pk=self.vaga.pk)
        self.assertTrue(all(l["estado_rotulo"] == "Não solicitado" for l in svc.painel_de_chamados(vaga)))

    def test_a_tela_mostra_sempre_os_tres_tipos(self):
        self.client.force_login(self.rh)
        html = self.client.get(reverse("contratacao_detail", args=[self.vaga.pk])).content.decode()
        self.assertEqual(html.count("Não solicitado"), 3)

    def test_a_tela_mostra_quem_pediu_quando_e_para_quem(self):
        self.pedir(self.rh, maquina="on")
        self.client.force_login(self.ti)
        html = self.client.get(reverse("contratacao_detail", args=[self.vaga.pk])).content.decode()
        self.assertIn("Rh.Maria", html)
        self.assertIn(DESTINO[Tipo.MAQUINA], html)
        self.assertIn("Histórico de tentativas (1)", html)

    def test_solicitante_ve_a_equipe_e_nao_o_endereco(self):
        self.pedir(self.rh, maquina="on")
        html = self.client.get(reverse("contratacao_detail", args=[self.vaga.pk])).content.decode()
        self.assertNotIn(DESTINO[Tipo.MAQUINA], html)
        self.assertIn("Equipe de TI (1 destinatário)", html)

    def test_solicitante_nao_ve_o_texto_tecnico_do_erro(self):
        with mock.patch.object(EmailMultiAlternatives, "send", _smtp_que_falha_para("preparação de equipamento")):
            self.pedir(self.rh, maquina="on")
        html = self.client.get(reverse("contratacao_detail", args=[self.vaga.pk])).content.decode()
        self.assertNotIn("servidor recusou a conexão", html)
        self.assertIn("O e-mail não foi enviado", html)
        self.client.force_login(self.ti)
        html = self.client.get(reverse("contratacao_detail", args=[self.vaga.pk])).content.decode()
        self.assertIn("servidor recusou a conexão", html)

    def test_linha_do_tempo_registra_pedido_envio_e_falha(self):
        with mock.patch.object(EmailMultiAlternatives, "send", _smtp_que_falha_para("acessos corporativos")):
            self.pedir(self.rh, maquina="on", usuario_email="on", criar_email="on")
        eventos = " | ".join(ContratacaoEvento.objects.filter(tipo="chamado").values_list("descricao", flat=True))
        self.assertIn("Chamado solicitado: Configuração e entrega de máquina", eventos)
        self.assertIn("E-mail do chamado “Configuração e entrega de máquina” enviado", eventos)
        self.assertIn("Falha no envio do e-mail do chamado “Criação de usuário e e-mail”", eventos)


# ─────────────────────────────────────────────────────────────
# Cenário 6 — controle de acesso e de status
# ─────────────────────────────────────────────────────────────

class ControleDeAcessoTests(ChamadosBase):
    def test_quem_so_tem_consulta_quiosque_e_barrado_no_caminho(self):
        resp = self.pedir(self.olheiro, maquina="on")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse("quiosque_dashboard"))
        self.assertFalse(ContratacaoChamado.objects.exists())
        self.assertEqual(mail.outbox, [])

    def test_fornecedor_e_barrado(self):
        forn = User.objects.create_user("forn", password="x")
        forn.groups.add(Group.objects.get_or_create(name=GRUPO_FORNECEDOR)[0])
        resp = self.pedir(forn, maquina="on")
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(ContratacaoChamado.objects.exists())

    def test_decorador_barra_mesmo_sem_o_middleware(self):
        pedido = RequestFactory().post("/qualquer/")

        @ar.exige_solicitar_estrutura
        def falsa(request):
            return "passou"

        for user in (self.olheiro,):
            pedido.user = user
            with self.assertRaises(PermissionDenied):
                falsa(pedido)
        for user in (self.rh, self.ti):
            pedido.user = user
            self.assertEqual(falsa(pedido), "passou")

    def test_service_recusa_por_si_mesmo(self):
        """3ª camada: chamado direto, sem passar por view nem middleware."""
        forn = User.objects.create_user("forn2", password="x")
        forn.groups.add(Group.objects.get_or_create(name=GRUPO_FORNECEDOR)[0])
        for intruso in (self.olheiro, forn):
            with self.assertRaises(PermissionDenied):
                svc.solicitar(contratacao_pk=self.vaga.pk, user=intruso, selecao={"maquina": {}})
        self.assertFalse(ContratacaoChamado.objects.exists())

    def test_quem_acumula_contratacoes_e_quiosque_pode_pedir(self):
        duplo = _login_restrito("duplo", GRUPO_PORTAL_CONTRATACAO)
        duplo.groups.add(Group.objects.get(name=GRUPO_CONSULTA_QUIOSQUE))
        cache.clear()
        self.pedir(duplo, maquina="on")
        self.assertEqual(len(mail.outbox), 1)

    def test_botao_aparece_para_quem_pode_e_a_vaga_esta_em_aberto(self):
        for user in (self.rh, self.ti):
            self.client.force_login(user)
            html = self.client.get(reverse("contratacao_detail", args=[self.vaga.pk])).content.decode()
            self.assertIn("Solicitar estrutura e abrir chamados", html, user.username)
            self.assertIn('<dialog class="ctr-modal" id="ctrChamadosModal"', html)

    def test_vaga_que_nao_esta_em_aberto_nao_aceita_o_pedido(self):
        for status in (StatusContratacaoChoices.VAGA_PREENCHIDA, StatusContratacaoChoices.EQUIPAMENTO_ENTREGUE,
                       StatusContratacaoChoices.CONCLUIDA, StatusContratacaoChoices.CANCELADA):
            with self.subTest(status=status):
                Contratacao.objects.filter(pk=self.vaga.pk).update(status=status)
                resp = self.pedir(self.rh, **self.TODOS)
                self.assertEqual(resp.status_code, 200)
                self.assertContains(resp, "só podem ser solicitados com a vaga em aberto")
                self.assertFalse(ContratacaoChamado.objects.exists())
                self.assertEqual(mail.outbox, [])

    def test_botao_some_e_a_tela_explica_quando_a_vaga_nao_esta_em_aberto(self):
        Contratacao.objects.filter(pk=self.vaga.pk).update(status=StatusContratacaoChoices.VAGA_PREENCHIDA)
        self.client.force_login(self.rh)
        html = self.client.get(reverse("contratacao_detail", args=[self.vaga.pk])).content.decode()
        self.assertNotIn("Solicitar estrutura e abrir chamados", html)
        self.assertNotIn('<dialog class="ctr-modal" id="ctrChamadosModal"', html)  # o <script> ainda cita o id; a janela não existe
        self.assertIn("só podem ser solicitados com a vaga", html)

    def test_reenvio_tambem_exige_vaga_em_aberto(self):
        with mock.patch.object(EmailMultiAlternatives, "send", _smtp_que_falha_para("preparação de equipamento")):
            self.pedir(self.rh, maquina="on")
        c = self.chamado(Tipo.MAQUINA)
        Contratacao.objects.filter(pk=self.vaga.pk).update(status=StatusContratacaoChoices.CONCLUIDA)
        with self.assertRaises(svc.SolicitacaoRecusada):
            svc.reenviar(contratacao_pk=self.vaga.pk, chamado_pk=c.pk, user=self.rh)

    def test_so_get_nao_dispara_nada(self):
        self.client.force_login(self.rh)
        resp = self.client.get(self.url_solicitar())
        self.assertEqual(resp.status_code, 405)
        self.assertEqual(mail.outbox, [])

    def test_chamado_de_outra_vaga_nao_e_alcancado_pela_url(self):
        outra = Contratacao.objects.create(cargo="Outra", tipo_vaga="nova", criado_por=self.ti)
        c = ContratacaoChamado.objects.create(contratacao=outra, tipo=Tipo.MAQUINA, status=Status.FALHA, dados={})
        self.client.force_login(self.rh)
        resp = self.client.post(reverse("contratacao_chamado_reenviar", args=[self.vaga.pk, c.pk]))
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(mail.outbox, [])


# ─────────────────────────────────────────────────────────────
# Destinatário — Central de Alertas, nada no código
# ─────────────────────────────────────────────────────────────

class DestinatarioTests(ChamadosBase):
    def test_o_catalogo_tem_um_canal_por_tipo(self):
        codigos = {c["codigo"] for c in email_alertas.CATALOGO_NOTIFICACOES}
        self.assertEqual(codigos & set(email_alertas.CANAL_CHAMADO_CONTRATACAO.values()),
                         set(email_alertas.CANAL_CHAMADO_CONTRATACAO.values()))

    def test_destinatarios_vem_da_lista_do_canal_e_mudam_sem_mexer_no_codigo(self):
        CanalNotificacao.objects.filter(codigo="chamado_contratacao_maquina").update(
            destinatarios_customizados="a@teste.invalid, b@teste.invalid",
        )
        self.pedir(self.rh, maquina="on")
        self.assertEqual(mail.outbox[0].to, ["a@teste.invalid", "b@teste.invalid"])

    def test_canal_desativado_vira_falha_e_nao_enviado(self):
        CanalNotificacao.objects.filter(codigo="chamado_contratacao_maquina").update(ativo=False)
        self.pedir(self.rh, maquina="on")
        c = self.chamado(Tipo.MAQUINA)
        self.assertEqual(c.status, Status.FALHA)
        self.assertIn("desativado", c.ultimo_erro)
        self.assertEqual(mail.outbox, [])

    def test_lista_vazia_vira_falha_com_motivo(self):
        CanalNotificacao.objects.filter(codigo="chamado_contratacao_maquina").update(destinatarios_customizados="")
        self.pedir(self.rh, maquina="on")
        c = self.chamado(Tipo.MAQUINA)
        self.assertEqual(c.status, Status.FALHA)
        self.assertIn("Nenhum destinatário", c.ultimo_erro)

    def test_alertas_globais_desligados_viram_falha(self):
        from ProjetoEstoque.models import ConfiguracaoSistema
        cfg = ConfiguracaoSistema.get()
        cfg.alertas_email_ativos = False
        cfg.save()
        self.pedir(self.rh, maquina="on")
        self.assertEqual(self.chamado(Tipo.MAQUINA).status, Status.FALHA)
        self.assertEqual(mail.outbox, [])

    def test_a_janela_avisa_antes_quando_o_envio_vai_falhar(self):
        CanalNotificacao.objects.filter(codigo="chamado_contratacao_sistemas").update(ativo=False)
        self.client.force_login(self.ti)
        html = self.client.get(reverse("contratacao_detail", args=[self.vaga.pk])).content.decode()
        self.assertIn("desativado em Central de Alertas", html)

    def test_resumo_da_janela_traz_destinatario_para_o_ti_e_so_a_equipe_para_o_solicitante(self):
        self.client.force_login(self.ti)
        html = self.client.get(reverse("contratacao_detail", args=[self.vaga.pk])).content.decode()
        self.assertIn(f'data-destino="{DESTINO[Tipo.MAQUINA]}"', html)
        self.client.force_login(self.rh)
        html = self.client.get(reverse("contratacao_detail", args=[self.vaga.pk])).content.decode()
        self.assertNotIn(DESTINO[Tipo.MAQUINA], html)
        self.assertIn('data-destino="Equipe de TI"', html)

    def test_o_envio_antigo_continua_igual(self):
        """`_enviar` virou um invólucro de `_enviar_detalhado`: os ~15 alertas
        que o usam não podem ter mudado de comportamento."""
        ok = email_alertas._enviar("Assunto", "texto", "<p>html</p>", ["x@teste.invalid"])
        self.assertIs(ok, True)
        self.assertEqual(mail.outbox[-1].to, ["x@teste.invalid"])
        with mock.patch.object(EmailMultiAlternatives, "send", side_effect=SMTPException("boom")):
            self.assertIs(email_alertas._enviar("A", "t", "<p>h</p>", ["x@teste.invalid"]), False)


# ─────────────────────────────────────────────────────────────
# Atalho do RH — fila no painel, janela depois de criar a vaga, pedido preenchido
# ─────────────────────────────────────────────────────────────

class AtalhoDoRhTests(ChamadosBase):
    """O atalho existe para o RH pedir a estrutura em poucos cliques. O risco dele
    não é quebrar tela, é o contrário: ficar quieto quando devia aparecer (a vaga
    some da fila e ninguém pede), aparecer quando não devia (promete uma ação que
    o service recusa) ou pré-marcar um pedido que a vaga não justifica.

    O atalho NUNCA contorna a regra: tudo que ele liga é sugestão numa janela com
    resumo, e o service revalida no POST.
    """

    PAYLOAD_VAGA = {
        "cargo": "Auxiliar Administrativo", "tipo_vaga": "nova", "computador": "desktop",
        "telefonia": "nenhum", "email_corporativo": "on",
    }

    def _abrir_vaga(self, user):
        self.client.force_login(user)
        return self.client.post(
            reverse("contratacao_create"), {**self.PAYLOAD_VAGA, "centro_custo": self.cc.pk},
        )

    def _painel(self, user):
        self.client.force_login(user)
        return self.client.get(reverse("contratacao_painel")).content.decode()

    def _detalhe(self, user, sufixo=""):
        self.client.force_login(user)
        url = reverse("contratacao_detail", args=[self.vaga.pk]) + sufixo
        return self.client.get(url).content.decode()

    def _mudar_status(self, status):
        Contratacao.objects.filter(pk=self.vaga.pk).update(status=status)

    FILA = 'aria-labelledby="ctrFilaTitulo"'
    JANELA = '<dialog class="ctr-modal" id="ctrChamadosModal"'

    # ── depois de criar a vaga ──
    def test_rh_que_abre_a_vaga_cai_direto_na_janela_de_pedido(self):
        resp = self._abrir_vaga(self.rh)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].endswith("?solicitar=1"), resp["Location"])
        html = self.client.get(resp["Location"]).content.decode()
        self.assertIn(self.JANELA, html)
        self.assertIn('data-abrir="1"', html)

    def test_ti_que_abre_a_vaga_nao_recebe_a_janela(self):
        """Quem abre a vaga no TI costuma ser quem vai atendê-la: uma janela de
        pedido a cada vaga criada seria ruído."""
        resp = self._abrir_vaga(self.ti)
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("solicitar", resp["Location"])

    # ── ?solicitar=1 ──
    def test_atalho_abre_a_janela_e_sem_ele_ela_fica_fechada(self):
        self.assertIn('data-abrir="1"', self._detalhe(self.rh, "?solicitar=1"))
        self.assertIn('data-abrir=""', self._detalhe(self.rh))

    def test_atalho_nao_abre_janela_que_nao_existe(self):
        """Vaga fora de "Em aberto" ou com tudo já pedido: a janela nem é
        renderizada — o parâmetro não pode fabricar uma ação que o service recusa."""
        self._mudar_status(StatusContratacaoChoices.VAGA_PREENCHIDA)
        self.assertNotIn(self.JANELA, self._detalhe(self.rh, "?solicitar=1"))

        self._mudar_status(StatusContratacaoChoices.ABERTA)
        self.pedir(self.rh, **self.TODOS)
        self.assertNotIn(self.JANELA, self._detalhe(self.rh, "?solicitar=1"))

    def test_aviso_no_topo_so_enquanto_falta_pedir(self):
        self.assertIn("não pediu a estrutura à TI", self._detalhe(self.rh))
        self.pedir(self.rh, maquina="on", maquina_descricao="x")
        self.assertNotIn("não pediu a estrutura à TI", self._detalhe(self.rh))

    # ── fila e selo no painel ──
    def test_fila_lista_a_vaga_sem_pedido_com_botao_para_a_janela(self):
        html = self._painel(self.rh)
        self.assertIn(self.FILA, html)
        self.assertIn(f'href="{reverse("contratacao_detail", args=[self.vaga.pk])}?solicitar=1"', html)
        # O botão diz de QUAL vaga é: lista de botões iguais não serve a leitor de tela.
        self.assertIn("Solicitar estrutura para a vaga Analista de Dados", html)
        self.assertIn("Estrutura não solicitada", html)

    def test_fila_some_quando_a_maquina_foi_pedida_e_o_selo_vira_solicitada(self):
        self.pedir(self.rh, maquina="on", maquina_descricao="x")
        html = self._painel(self.rh)
        self.assertNotIn(self.FILA, html)
        self.assertIn("Estrutura solicitada", html)
        self.assertNotIn("Estrutura não solicitada", html)
        # "Solicitada" é o que o sistema fez — nunca "chamado aberto".
        self.assertNotIn("chamado aberto", html.lower())

    def test_falha_de_envio_volta_para_a_fila_com_tentar_novamente(self):
        with mock.patch.object(EmailMultiAlternatives, "send", _smtp_que_falha_para("preparação de equipamento")):
            self.pedir(self.rh, maquina="on", maquina_descricao="x")
        html = self._painel(self.rh)
        self.assertIn(self.FILA, html)
        self.assertIn("Falha no envio à TI", html)
        self.assertIn("Ver e tentar novamente", html)
        self.assertIn(f'{reverse("contratacao_detail", args=[self.vaga.pk])}#chamados', html)

    def test_vaga_fora_de_em_aberto_nao_entra_na_fila_nem_ganha_selo(self):
        self._mudar_status(StatusContratacaoChoices.VAGA_PREENCHIDA)
        html = self._painel(self.rh)
        self.assertNotIn(self.FILA, html)
        self.assertNotIn("Estrutura não solicitada", html)

    def test_fila_respeita_o_limite_e_diz_quantas_ficaram_de_fora(self):
        for i in range(7):
            Contratacao.objects.create(
                cargo=f"Vaga extra {i}", tipo_vaga="nova", computador="notebook",
                centro_custo=self.cc, criado_por=self.ti, atualizado_por=self.ti,
            )
        html = self._painel(self.rh)  # 8 vagas sem pedido
        self.assertEqual(html.count('class="ctr-fila-item"'), FILA_ESTRUTURA_LIMITE)
        self.assertIn("+ 3 outras", html)

    def test_painel_nao_ganha_consulta_por_vaga(self):
        """A fila e os selos saem de UMA consulta: com 1 ou 7 vagas o painel custa
        o mesmo. É o cuidado que o resto do painel já tem (regra 47 do CLAUDE.md)."""
        self.client.force_login(self.rh)
        url = reverse("contratacao_painel")
        self.client.get(url)  # aquece sessão/cache: a comparação é entre as duas seguintes
        with CaptureQueriesContext(connection) as uma:
            self.client.get(url)
        for i in range(6):
            Contratacao.objects.create(
                cargo=f"Vaga extra {i}", tipo_vaga="nova", computador="notebook",
                centro_custo=self.cc, criado_por=self.ti, atualizado_por=self.ti,
            )
        with CaptureQueriesContext(connection) as varias:
            self.client.get(url)
        self.assertEqual(len(varias), len(uma))

    # ── situação (service) ──
    def test_situacao_so_considera_vaga_em_aberto(self):
        self._mudar_status(StatusContratacaoChoices.VAGA_PREENCHIDA)
        vaga = Contratacao.objects.get(pk=self.vaga.pk)
        self.assertEqual(svc.situacao_de_estrutura([vaga]), {})
        self.assertFalse(svc.falta_pedir(vaga))

    def test_envio_interrompido_conta_como_falha_na_situacao(self):
        c = ContratacaoChamado.objects.create(
            contratacao=self.vaga, tipo=Tipo.MAQUINA, status=Status.PENDENTE, dados={},
        )
        ContratacaoChamado.objects.filter(pk=c.pk).update(updated_at=timezone.now() - timedelta(minutes=30))
        s = svc.situacao_de_estrutura([self.vaga])[self.vaga.pk]
        self.assertEqual((s["estado"], s["acao"]), ("falha", "reenviar"))

    def test_vaga_sem_computador_so_deve_pedido_se_nada_foi_pedido(self):
        Contratacao.objects.filter(pk=self.vaga.pk).update(computador="nenhum")
        vaga = Contratacao.objects.get(pk=self.vaga.pk)
        self.assertEqual(svc.situacao_de_estrutura([vaga])[vaga.pk]["estado"], "nao_solicitada")
        self.pedir(self.rh, sistemas="on", sistemas_suporte=["liberacao"])
        self.assertEqual(svc.situacao_de_estrutura([vaga])[vaga.pk]["estado"], "solicitada")

    # ── pedido já preenchido ──
    def test_pedido_sugerido_liga_o_que_a_vaga_ja_informa(self):
        s = svc.pedido_sugerido(self.vaga)
        self.assertEqual(s["maquina_descricao"], "16 GB de RAM")
        self.assertTrue(s["usuario_email"])  # a vaga tem contratado
        self.assertTrue(s["sistemas"])       # e tem TOTVS marcado
        self.assertEqual(list(s["sistemas_suporte"]), ["liberacao"])

    def test_sem_contratado_nao_sugere_usuario_e_email(self):
        """Sem nome a TI não tem o que criar — e as caixas de usuário/e-mail da
        vaga nascem marcadas por padrão, então não são uma escolha de ninguém."""
        Contratacao.objects.filter(pk=self.vaga.pk).update(colaborador_nome="")
        s = svc.pedido_sugerido(Contratacao.objects.get(pk=self.vaga.pk))
        self.assertNotIn("usuario_email", s)

    def test_sem_sistema_marcado_nao_sugere_sistemas(self):
        self.vaga.sistemas.all().delete()
        s = svc.pedido_sugerido(Contratacao.objects.get(pk=self.vaga.pk))
        self.assertNotIn("sistemas", s)
        self.assertNotIn("sistemas_suporte", s)

    def test_o_que_ja_foi_pedido_nao_volta_sugerido(self):
        self.pedir(self.rh, **self.TODOS)
        s = svc.pedido_sugerido(Contratacao.objects.get(pk=self.vaga.pk))
        self.assertNotIn("usuario_email", s)
        self.assertNotIn("sistemas", s)

    def test_janela_abre_com_os_blocos_ja_marcados(self):
        html = self._detalhe(self.rh, "?solicitar=1")
        self.assertRegex(html, r'<input[^>]*name="usuario_email"[^>]*checked')
        self.assertRegex(html, r'<input[^>]*name="sistemas"[^>]*checked')
        self.assertRegex(html, r'<input[^>]*name="sistemas_suporte"[^>]*value="liberacao"[^>]*checked')

    def test_pedido_recusado_nao_reaplica_a_sugestao(self):
        """Quem desligou um bloco e errou outro não pode ver a janela religar o que
        desligou: o que volta é o que a pessoa digitou, não a sugestão."""
        self.client.force_login(self.rh)
        resp = self.client.post(
            self.url_solicitar(), {"maquina": "on", "maquina_descricao": "x", "usuario_email": "on"},
        )  # usuário/e-mail sem nada a criar: recusado
        self.assertEqual(resp.status_code, 200)
        self.assertNotRegex(resp.content.decode(), r'<input[^>]*name="sistemas"[^>]*checked')

    def test_sugestao_nao_envia_nada_sozinha(self):
        """Abrir a janela já preenchida não é pedir: só o POST confirmado envia."""
        self._detalhe(self.rh, "?solicitar=1")
        self.assertEqual(mail.outbox, [])
        self.assertEqual(ContratacaoChamado.objects.count(), 0)


class MensagemTests(TestCase):
    """`montar_chamado_contratacao` é pura: dá para travar o conteúdo sem banco."""

    DADOS = {
        "vaga_id": 7, "cargo": "Técnico\nde Campo", "departamento": "Operações", "local": "Unidade Norte",
        "previsao_inicio": "20/11/2026", "tipo_vaga": "Nova vaga", "solicitante": "Maria",
        "criar_usuario_rede": True, "criar_email": False,
    }

    def _montar(self, tipo, **kw):
        agora = timezone.now()
        return email_alertas.montar_chamado_contratacao(
            tipo, {**self.DADOS, **kw}, solicitado_em=agora, enviado_em=agora,
        )

    def test_assunto_e_especifico_de_cada_tipo_e_numa_linha_so(self):
        assuntos = [self._montar(t)[0] for t in ("maquina", "usuario_email", "sistemas")]
        self.assertEqual(len(set(assuntos)), 3)
        for a in assuntos:
            self.assertNotIn("\n", a, "quebra de linha no assunto é recusada pelo Django")
            self.assertIn("Vaga nº 7", a)

    def test_reenvio_e_identificado_no_assunto(self):
        agora = timezone.now()
        assunto, _, _ = email_alertas.montar_chamado_contratacao(
            "maquina", self.DADOS, solicitado_em=agora, enviado_em=agora, tentativa=2,
        )
        self.assertIn("(reenvio)", assunto)

    def test_sem_previsao_diz_que_nao_foi_informada(self):
        _, texto, _ = self._montar("maquina", previsao_inicio="")
        self.assertIn("Previsão de início: Não informada", texto)

    def test_o_aviso_deixa_claro_que_e_solicitacao_e_nao_chamado_aberto(self):
        _, texto, html = self._montar("maquina")
        self.assertIn("solicitação, não um chamado aberto", texto)
        self.assertIn("não um chamado aberto", html)

    def test_nenhum_endereco_no_codigo_do_envio(self):
        import inspect
        for f in (email_alertas.montar_chamado_contratacao, email_alertas.enviar_chamado_contratacao,
                  email_alertas.situacao_canal_chamado, email_alertas._linhas_do_chamado):
            fonte = inspect.getsource(f)
            self.assertNotRegex(fonte, r"[\w.]+@[\w.]+\.\w+", f.__name__)
