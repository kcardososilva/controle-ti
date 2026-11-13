"""
Conversa dos chamados (RH ↔ TI, e-mail nos dois sentidos) + preenchimento da vaga pelo RH.

Os 7 cenários de aceite estão cada um na sua classe (`Cenario1…7`). O resto é o
que protege o que ELES não mostram — e a maioria destes defeitos não derruba
tela nenhuma: a página abre, o botão clica, e o erro está na mensagem que foi
parar no chamado errado, no remetente que não devia ter sido aceito, ou no
e-mail registrado duas vezes.

  • VÍNCULO — a resposta tem de cair no chamado certo com várias vagas em
    andamento, SEM depender do assunto; sinal que conflita vai para revisão.
  • REMETENTE/AUTENTICIDADE — vínculo certo não basta: quem escreve precisa ser
    participante, e SPF/DKIM/DMARC reprovado não passa.
  • DUPLICIDADE E RECUPERAÇÃO — o mesmo e-mail por dois caminhos vira UMA
    mensagem; falha de gravação é reprocessável a partir do bruto, sem duplicar.
  • ANEXOS — tipo/conteúdo/tamanho, entrega só autenticada e como download.
  • HONESTIDADE — "enviado" = servidor aceitou; nunca "entregue".
  • INDEPENDÊNCIA — situação do chamado, etapa da vaga e preenchimento não
    se arrastam uns aos outros.

Nenhum e-mail sai de verdade (`mail.outbox`) e nenhuma rede é tocada: o transporte
do Graph é uma sessão falsa. O disco dos anexos é um diretório temporário.
"""

import shutil
import tempfile
import uuid
from email.message import EmailMessage
from email.utils import formatdate
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.mail import EmailMultiAlternatives
from django.test import TestCase, override_settings
from django.urls import reverse

from services import caixa_graph_service
from services import chamado_anexo_service as anexos_svc
from services import chamado_contratacao_service as chamados_svc
from services import chamado_conversa_service as conv
from services import chamado_email_entrada_service as entrada
from services import contratacao_service as cs

from ProjetoEstoque.models import (
    AtendimentoChamadoChoices as Atend,
    ChamadoAnexo,
    ChamadoCaixaEstado,
    ChamadoEmailRecebido as Registro,
    ChamadoMensagem as Msg,
    ChamadoStatusLog,
    Contratacao,
    ContratacaoChamado,
    ContratacaoVagaHistorico,
    GRUPO_CONSULTA_QUIOSQUE,
    GRUPO_FORNECEDOR,
    StatusChamadoContratacao as Status,
    StatusContratacaoChoices,
    TipoChamadoContratacao as Tipo,
)
from ProjetoEstoque.tests.test_chamados_contratacao import (
    DESTINO,
    ChamadosBase,
    _login_restrito,
    _smtp_que_falha_para,
)

_DISCO = tempfile.mkdtemp(prefix="chamados_teste_")

PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
EXE = b"MZ\x90\x00" + b"\x00" * 64


def tearDownModule():
    shutil.rmtree(_DISCO, ignore_errors=True)


def eml(*, de, para, assunto="Re: Chamado", corpo="Resposta.", message_id=None, em_resposta_a=None,
        referencias=None, extras=None, anexos=()):
    """Um e-mail RFC 822 bruto, como a caixa o devolve."""
    m = EmailMessage()
    m["From"] = de
    m["To"] = para if isinstance(para, str) else ", ".join(para)
    m["Subject"] = assunto
    m["Message-ID"] = f"<{message_id or uuid.uuid4().hex + '@teste.invalid'}>"
    m["Date"] = formatdate()
    refs = referencias if referencias is not None else ([em_resposta_a] if em_resposta_a else [])
    if em_resposta_a:
        m["In-Reply-To"] = f"<{em_resposta_a}>"
    if refs:
        m["References"] = " ".join(f"<{r}>" for r in refs)
    for chave, valor in (extras or {}).items():
        m[chave] = valor
    m.set_content(corpo)
    for nome, conteudo in anexos:
        tipo = "application/pdf" if nome.endswith(".pdf") else "application/octet-stream"
        maintype, subtype = tipo.split("/")
        m.add_attachment(conteudo, maintype=maintype, subtype=subtype, filename=nome)
    return m.as_bytes()


@override_settings(CHAMADOS_ARQUIVOS_ROOT=_DISCO)
class ConversaBase(ChamadosBase):
    """Uma vaga em aberto com o chamado de MÁQUINA já enviado — o ponto de partida."""

    def setUp(self):
        super().setUp()
        self.pedir(self.rh, maquina="on", maquina_descricao="Notebook 16 GB")
        self.ch = self.chamado(Tipo.MAQUINA)
        self.abertura = self.ch.mensagens.get(tipo=Msg.Tipo.ABERTURA)
        self.ti_externo = DESTINO[Tipo.MAQUINA]  # quem recebe do lado da TI (Central de Alertas)
        mail.outbox = []

    # ── helpers ──
    def ingerir(self, raw, **kw):
        return entrada.tratar_email(raw, **kw)

    def resposta_ti(self, corpo="Em andamento.", **kw):
        return eml(de=f"Equipe TI <{self.ti_externo}>", para="datasul@teste.invalid", corpo=corpo,
                   em_resposta_a=kw.pop("em_resposta_a", self.abertura.message_id), **kw)

    def resposta_rh(self, corpo="Segue o que faltava.", **kw):
        return eml(de="Maria <maria@teste.invalid>", para="datasul@teste.invalid", corpo=corpo,
                   em_resposta_a=kw.pop("em_resposta_a", self.abertura.message_id), **kw)

    def url_chamado(self, ch=None):
        ch = ch or self.ch
        return reverse("chamado_detail", args=[ch.contratacao_id, ch.pk])

    def html_do_chamado(self, user, ch=None):
        self.client.force_login(user)
        return self.client.get(self.url_chamado(ch)).content.decode()

    def responder_pela_plataforma(self, user, texto="Mensagem.", arquivos=(), ch=None):
        ch = ch or self.ch
        self.client.force_login(user)
        dados = {"texto": texto}
        if arquivos:
            dados["arquivos"] = list(arquivos)
        return self.client.post(
            reverse("chamado_responder", args=[ch.contratacao_id, ch.pk]), dados,
        )

    def mensagens(self, ch=None):
        return list((ch or self.ch).mensagens.exclude(tipo=Msg.Tipo.ABERTURA))


# ─────────────────────────────────────────────────────────────
# Cenário 1 — Resposta de TI por e-mail
# ─────────────────────────────────────────────────────────────

class Cenario1RespostaDeTIPorEmail(ConversaBase):
    def test_a_ti_recebeu_o_email_de_abertura_com_message_id_proprio(self):
        self.assertEqual(self.ch.status, Status.ENVIADO)
        self.assertTrue(self.abertura.message_id)
        self.assertEqual(self.abertura.estado, Msg.Estado.ENVIADA)
        self.assertEqual(self.abertura.lado, Msg.Lado.RH)

    def test_resposta_da_ti_por_email_aparece_na_conversa_e_o_rh_a_ve(self):
        r = self.ingerir(self.resposta_ti(
            "Máquina já separada.\n\nEm seg., 9 de nov. de 2026 às 10:00, Maria escreveu:\n> pedido original",
        ), provider_id="g1")
        self.assertEqual(r.estado, Registro.Estado.PROCESSADO)

        (m,) = self.mensagens()
        self.assertEqual((m.direcao, m.origem, m.lado, m.estado),
                         (Msg.Direcao.ENTRADA, Msg.Origem.EMAIL, Msg.Lado.TI, Msg.Estado.RECEBIDA))
        self.assertEqual(m.corpo, "Máquina já separada.")  # a citação ficou de fora
        self.assertEqual(m.remetente_email, self.ti_externo)

        html = self.html_do_chamado(self.rh)
        self.assertIn("Máquina já separada.", html)
        self.assertIn("Recebida por e-mail", html)
        self.assertIn(">TI<", html)  # o RH identifica o lado de quem escreveu

    def test_a_resposta_atualiza_a_ultima_atividade(self):
        antes = ContratacaoChamado.objects.get(pk=self.ch.pk).ultima_atividade_em
        self.ingerir(self.resposta_ti(), provider_id="g1")
        depois = ContratacaoChamado.objects.get(pk=self.ch.pk).ultima_atividade_em
        self.assertGreaterEqual(depois, antes)

    def test_resposta_so_com_o_texto_citado_nao_vira_mensagem_vazia(self):
        self.ingerir(self.resposta_ti("> tudo citado\n> nada novo"), provider_id="g1")
        (m,) = self.mensagens()
        self.assertTrue(m.corpo.strip())


# ─────────────────────────────────────────────────────────────
# Cenário 2 — Resposta de RH pela plataforma
# ─────────────────────────────────────────────────────────────

class Cenario2RespostaDeRHPelaPlataforma(ConversaBase):
    def test_rh_responde_e_a_mensagem_sai_por_email_para_a_ti_e_fica_registrada(self):
        conv.atualizar_atendimento(chamado_pk=self.ch.pk, para=Atend.AGUARDANDO_RH,
                                   motivo="Preciso do CPF do gestor", user=self.ti)
        resp = self.responder_pela_plataforma(self.rh, "O gestor é o João.")
        self.assertEqual(resp.status_code, 302)

        (m,) = self.mensagens()
        self.assertEqual((m.lado, m.origem, m.estado), (Msg.Lado.RH, Msg.Origem.PLATAFORMA, Msg.Estado.ENVIADA))
        self.assertEqual(m.autor, self.rh)

        (saida,) = mail.outbox
        self.assertEqual(saida.to, [self.ti_externo])
        self.assertIn("O gestor é o João.", saida.body)
        self.assertEqual(saida.extra_headers["Message-ID"], f"<{m.message_id}>")
        # Costura da conversa: In-Reply-To aponta para a mensagem anterior (a abertura).
        self.assertEqual(saida.extra_headers["In-Reply-To"], f"<{self.abertura.message_id}>")
        self.assertIn(f"<{self.abertura.message_id}>", saida.extra_headers["References"])
        self.assertTrue(saida.subject.startswith("Re:"))
        self.assertIn(conv.referencia_do_chamado(self.ch.pk), saida.subject)

    def test_a_resposta_do_rh_devolve_o_chamado_a_fila_da_ti_e_registra_quem_mudou(self):
        conv.atualizar_atendimento(chamado_pk=self.ch.pk, para=Atend.AGUARDANDO_RH, motivo="", user=self.ti)
        self.responder_pela_plataforma(self.rh)
        self.ch.refresh_from_db()
        self.assertEqual(self.ch.atendimento, Atend.AGUARDANDO_ATENDIMENTO)
        log = ChamadoStatusLog.objects.filter(chamado=self.ch).latest("pk")
        self.assertTrue(log.automatico)
        self.assertEqual((log.de, log.para, log.criado_por), (Atend.AGUARDANDO_RH, Atend.AGUARDANDO_ATENDIMENTO, self.rh))

    def test_ti_responde_pela_plataforma_para_o_solicitante_com_a_equipe_em_copia(self):
        self.responder_pela_plataforma(self.ti, "Previsão: amanhã.")
        (saida,) = mail.outbox
        self.assertEqual(saida.to, ["maria@teste.invalid"])
        self.assertEqual(saida.cc, [self.ti_externo])
        (m,) = self.mensagens()
        self.assertEqual(m.lado, Msg.Lado.TI)

    def test_o_corpo_da_mensagem_e_escapado_no_email_e_na_tela(self):
        self.responder_pela_plataforma(self.rh, "<script>alert(1)</script> & <b>x</b>")
        (saida,) = mail.outbox
        html = saida.alternatives[0][0]
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>alert(1)</script>", self.html_do_chamado(self.rh))

    def test_mensagem_vazia_e_recusada(self):
        self.responder_pela_plataforma(self.rh, "   ")
        self.assertEqual(self.mensagens(), [])
        self.assertEqual(mail.outbox, [])


# ─────────────────────────────────────────────────────────────
# Cenário 3 — Resposta de RH por e-mail
# ─────────────────────────────────────────────────────────────

class Cenario3RespostaDeRHPorEmail(ConversaBase):
    def test_resposta_do_rh_por_email_cai_no_chamado_certo_e_a_ti_continua_a_conversa(self):
        r = self.ingerir(self.resposta_rh("O gestor é o João."), provider_id="g1")
        self.assertEqual(r.estado, Registro.Estado.PROCESSADO)
        (m,) = self.mensagens()
        self.assertEqual((m.lado, m.origem, m.autor), (Msg.Lado.RH, Msg.Origem.EMAIL, self.rh))
        self.assertEqual(m.chamado, self.ch)

        # A TI vê pela plataforma ...
        self.assertIn("O gestor é o João.", self.html_do_chamado(self.ti))
        # ... e continua a conversa: a resposta da TI sai por e-mail para o RH.
        self.responder_pela_plataforma(self.ti, "Obrigado, seguindo.")
        self.assertEqual(mail.outbox[-1].to, ["maria@teste.invalid"])

    def test_resposta_do_rh_em_chamado_que_esperava_por_ele_volta_para_a_fila(self):
        conv.atualizar_atendimento(chamado_pk=self.ch.pk, para=Atend.AGUARDANDO_INFORMACOES, motivo="", user=self.ti)
        self.ingerir(self.resposta_rh(), provider_id="g1")
        self.ch.refresh_from_db()
        self.assertEqual(self.ch.atendimento, Atend.AGUARDANDO_ATENDIMENTO)

    def test_respostas_em_sequencia_ficam_em_ordem_e_sem_perda(self):
        for i in range(3):
            self.ingerir(self.resposta_rh(f"Parte {i}", em_resposta_a=self.abertura.message_id), provider_id=f"g{i}")
        self.assertEqual([m.corpo for m in self.mensagens()], ["Parte 0", "Parte 1", "Parte 2"])


# ─────────────────────────────────────────────────────────────
# Cenário 4 — Chamados simultâneos
# ─────────────────────────────────────────────────────────────

class Cenario4ChamadosSimultaneos(ConversaBase):
    def setUp(self):
        super().setUp()
        # Mais dois chamados: outro TIPO na mesma vaga, e a MESMA máquina em OUTRA vaga.
        self.pedir(self.rh, usuario_email="on", criar_usuario_rede="on")
        self.vaga2 = Contratacao.objects.create(
            cargo="Analista Fiscal", tipo_vaga="nova", computador="notebook",
            centro_custo=self.cc, localidade=self.local, criado_por=self.ti, atualizado_por=self.ti,
        )
        self.pedir(self.rh, vaga=self.vaga2, maquina="on", maquina_descricao="Outro notebook")
        self.ch_usuario = self.chamado(Tipo.USUARIO_EMAIL)
        self.ch_b = ContratacaoChamado.objects.get(contratacao=self.vaga2, tipo=Tipo.MAQUINA)
        self.todos = {self.ch: "A-maquina", self.ch_usuario: "A-usuario", self.ch_b: "B-maquina"}
        mail.outbox = []

    def _abertura(self, ch):
        return ch.mensagens.get(tipo=Msg.Tipo.ABERTURA).message_id

    def test_cada_resposta_cai_no_seu_chamado_mesmo_com_o_mesmo_assunto_e_fora_de_ordem(self):
        ordem = [self.ch_b, self.ch, self.ch_usuario]
        for i, ch in enumerate(ordem):
            self.ingerir(
                eml(de="Maria <maria@teste.invalid>", para="datasul@teste.invalid",
                    assunto="Re: Chamado", corpo=f"para {self.todos[ch]}",
                    em_resposta_a=self._abertura(ch)),
                provider_id=f"g{i}",
            )
        for ch, rotulo in self.todos.items():
            (m,) = self.mensagens(ch)
            self.assertEqual(m.corpo, f"para {rotulo}", f"histórico do chamado {rotulo} misturado")

    def test_o_assunto_nao_e_usado_para_vincular(self):
        """Um assunto IGUAL ao do chamado, sem nenhum sinal de vínculo, não prende a mensagem."""
        r = self.ingerir(
            eml(de="Maria <maria@teste.invalid>", para="datasul@teste.invalid", assunto=self.ch.assunto,
                corpo="parece do chamado, mas não cita nada"),
            provider_id="g1",
        )
        self.assertEqual(r.estado, Registro.Estado.IGNORADO)
        self.assertEqual(Msg.objects.filter(direcao=Msg.Direcao.ENTRADA).count(), 0)

    def test_referencia_assinada_no_corpo_vincula_quando_os_cabecalhos_se_perderam(self):
        ref = conv.referencia_do_chamado(self.ch_b.pk)
        r = self.ingerir(
            eml(de="Maria <maria@teste.invalid>", para="datasul@teste.invalid",
                corpo=f"Encaminhando de outro cliente de e-mail.\n\nRef. do chamado: {ref}"),
            provider_id="g1",
        )
        self.assertEqual(r.estado, Registro.Estado.PROCESSADO)
        self.assertEqual(r.chamado, self.ch_b)

    def test_referencia_com_assinatura_forjada_nao_vincula(self):
        r = self.ingerir(
            eml(de="Maria <maria@teste.invalid>", para="datasul@teste.invalid",
                corpo=f"Ref. do chamado: CH-{self.ch.pk}-00000000"),
            provider_id="g1",
        )
        self.assertEqual(r.estado, Registro.Estado.IGNORADO)

    def test_sinais_que_apontam_para_chamados_diferentes_vao_para_revisao_e_nao_para_um_palpite(self):
        r = self.ingerir(
            eml(de="Maria <maria@teste.invalid>", para="datasul@teste.invalid",
                em_resposta_a=self._abertura(self.ch),
                corpo=f"Ref. do chamado: {conv.referencia_do_chamado(self.ch_b.pk)}"),
            provider_id="g1",
        )
        self.assertEqual(r.estado, Registro.Estado.QUARENTENA)
        self.assertIn("ambíguo", r.motivo)
        self.assertEqual(Msg.objects.filter(direcao=Msg.Direcao.ENTRADA).count(), 0)

    def test_id_de_conversa_do_provedor_e_aprendido_e_passa_a_vincular(self):
        self.ingerir(self.resposta_rh("primeira"), provider_id="g1", conversation_id="AAQk-conv-1")
        self.ch.refresh_from_db()
        self.assertEqual(self.ch.conversation_id, "AAQk-conv-1")
        # Segunda mensagem SEM cabeçalhos de vínculo: só o id de conversa a prende.
        r = self.ingerir(
            eml(de="Maria <maria@teste.invalid>", para="datasul@teste.invalid", corpo="segunda"),
            provider_id="g2", conversation_id="AAQk-conv-1",
        )
        self.assertEqual(r.chamado, self.ch)

    def test_resposta_da_ti_ao_chamado_de_outro_tipo_nao_vaza_para_o_da_maquina(self):
        self.ingerir(eml(de=f"TI <{DESTINO[Tipo.USUARIO_EMAIL]}>", para="datasul@teste.invalid",
                         corpo="usuário criado", em_resposta_a=self._abertura(self.ch_usuario)),
                     provider_id="g1")
        self.assertEqual(self.mensagens(self.ch), [])
        self.assertEqual(len(self.mensagens(self.ch_usuario)), 1)


# ─────────────────────────────────────────────────────────────
# Cenário 5 — Anexos e falhas
# ─────────────────────────────────────────────────────────────

class Cenario5AnexosEFalhas(ConversaBase):
    # ── anexos enviados pela plataforma ──
    def test_mensagem_com_anexo_chega_por_email_e_o_arquivo_fica_disponivel_a_quem_pode(self):
        pdf = SimpleUploadedFile("especificacao.pdf", PDF, content_type="application/pdf")
        self.responder_pela_plataforma(self.rh, "Segue a especificação.", [pdf])
        (m,) = self.mensagens()
        (anexo,) = m.anexos.all()
        self.assertEqual((anexo.nome_original, anexo.tamanho, anexo.bloqueado), ("especificacao.pdf", len(PDF), False))

        (saida,) = mail.outbox
        self.assertEqual([a[0] for a in saida.attachments], ["especificacao.pdf"])

        url = reverse("chamado_anexo_download", args=[self.vaga.pk, self.ch.pk, anexo.pk])
        for user in (self.rh, self.ti):  # RH e TI
            self.client.force_login(user)
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200, user.username)
            self.assertEqual(b"".join(resp.streaming_content), PDF)
            self.assertIn("attachment", resp["Content-Disposition"])
            self.assertEqual(resp["Content-Type"], "application/pdf")
            self.assertEqual(resp["X-Content-Type-Options"], "nosniff")

    def test_anexo_nao_e_servido_a_quem_nao_pode_ver_o_chamado(self):
        self.responder_pela_plataforma(self.rh, "x", [SimpleUploadedFile("a.pdf", PDF)])
        anexo = ChamadoAnexo.objects.get()
        url = reverse("chamado_anexo_download", args=[self.vaga.pk, self.ch.pk, anexo.pk])
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)  # anônimo → login
        for outsider in (self.olheiro, _login_restrito("forn.x", GRUPO_FORNECEDOR)):
            self.client.force_login(outsider)
            self.assertNotEqual(self.client.get(url).status_code, 200, outsider.username)

    def test_anexo_so_e_alcancado_pelo_chamado_a_que_pertence(self):
        self.responder_pela_plataforma(self.rh, "x", [SimpleUploadedFile("a.pdf", PDF)])
        anexo = ChamadoAnexo.objects.get()
        outro = Contratacao.objects.create(cargo="Outra", tipo_vaga="nova", criado_por=self.ti)
        self.client.force_login(self.ti)
        resp = self.client.get(reverse("chamado_anexo_download", args=[outro.pk, self.ch.pk, anexo.pk]))
        self.assertEqual(resp.status_code, 404)

    def test_arquivo_fica_fora_de_qualquer_pasta_servida(self):
        from django.conf import settings
        self.responder_pela_plataforma(self.rh, "x", [SimpleUploadedFile("a.pdf", PDF)])
        anexo = ChamadoAnexo.objects.get()
        self.assertTrue(anexo.arquivo.path.startswith(_DISCO))
        self.assertFalse(anexo.arquivo.path.startswith(str(settings.MEDIA_ROOT)))
        self.assertNotIn("a.pdf", anexo.arquivo.name)  # o nome do remetente não vira caminho

    def test_tipos_perigosos_e_conteudo_falsificado_sao_recusados_e_nada_e_enviado(self):
        casos = [
            ("instalador.exe", EXE, "não permitido"),
            ("script.js", b"alert(1)", "não permitido"),
            ("pagina.html", b"<html>", "não permitido"),
            ("logo.svg", b"<svg onload=x>", "não permitido"),
            ("falso.pdf", EXE, "não corresponde"),
            ("vazio.pdf", b"", "vazio"),
        ]
        for nome, conteudo, trecho in casos:
            with self.subTest(nome=nome):
                resp = self.responder_pela_plataforma(self.rh, "x", [SimpleUploadedFile(nome, conteudo)])
                self.assertContains(resp, trecho)
                self.assertEqual(self.mensagens(), [])
                self.assertEqual(mail.outbox, [])

    def test_arquivo_grande_demais_e_recusado(self):
        grande = SimpleUploadedFile("grande.pdf", PDF + b"0" * (anexos_svc.LIMITE_ARQUIVO + 1))
        resp = self.responder_pela_plataforma(self.rh, "x", [grande])
        self.assertContains(resp, "maior que")
        self.assertEqual(self.mensagens(), [])

    def test_nome_do_arquivo_e_higienizado(self):
        self.assertEqual(anexos_svc.nome_seguro("../../etc/passwd"), "passwd")
        self.assertEqual(anexos_svc.nome_seguro("a\\b\\c.pdf"), "c.pdf")
        self.assertEqual(anexos_svc.nome_seguro('x"<y>.pdf'), "xy.pdf")
        self.assertEqual(anexos_svc.nome_seguro(""), "arquivo")

    # ── anexos recebidos por e-mail ──
    def test_anexo_recebido_vira_arquivo_e_o_bloqueado_vira_aviso_sem_arquivo(self):
        r = self.ingerir(self.resposta_ti("Segue.", anexos=[("laudo.pdf", PDF), ("programa.exe", EXE)]),
                         provider_id="g1")
        self.assertEqual(r.estado, Registro.Estado.PROCESSADO)
        ok = ChamadoAnexo.objects.get(nome_original="laudo.pdf")
        bloqueado = ChamadoAnexo.objects.get(nome_original="programa.exe")
        self.assertFalse(ok.bloqueado)
        self.assertTrue(ok.arquivo)
        self.assertTrue(bloqueado.bloqueado)
        self.assertFalse(bloqueado.arquivo)
        self.assertIn("não permitido", bloqueado.motivo_bloqueio)
        html = self.html_do_chamado(self.rh)
        self.assertIn("laudo.pdf", html)
        self.assertIn("não disponível", html)

    def test_anexo_bloqueado_nao_e_baixavel(self):
        self.ingerir(self.resposta_ti(anexos=[("programa.exe", EXE)]), provider_id="g1")
        anexo = ChamadoAnexo.objects.get()
        self.client.force_login(self.ti)
        resp = self.client.get(reverse("chamado_anexo_download", args=[self.vaga.pk, self.ch.pk, anexo.pk]))
        self.assertEqual(resp.status_code, 404)

    # ── falha de ENVIO ──
    def test_falha_de_envio_e_registrada_e_a_nova_tentativa_nao_duplica(self):
        with mock.patch.object(EmailMultiAlternatives, "send", _smtp_que_falha_para("Re:")):
            self.responder_pela_plataforma(self.rh, "Importante.")
        (m,) = self.mensagens()
        self.assertEqual(m.estado, Msg.Estado.FALHA)
        self.assertIn("servidor", m.erro)
        self.assertEqual(mail.outbox, [])
        id_original = m.message_id

        self.client.force_login(self.rh)
        url = reverse("chamado_mensagem_reenviar", args=[self.vaga.pk, self.ch.pk, m.pk])
        self.client.post(url)
        m.refresh_from_db()
        self.assertEqual(m.estado, Msg.Estado.ENVIADA)
        self.assertEqual(m.tentativas, 2)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].extra_headers["Message-ID"], f"<{id_original}>")  # o mesmo id

        self.client.post(url)  # segundo clique: o que já foi enviado não sai de novo
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(len(self.mensagens()), 1)

    def test_falha_de_envio_para_o_solicitante_sem_email_e_explicada(self):
        # O solicitante sem e-mail: nem no cadastro nem na mensagem de abertura que ele enviou.
        User.objects.filter(pk=self.rh.pk).update(email="")
        Msg.objects.filter(chamado=self.ch).update(remetente_email="")
        self.responder_pela_plataforma(self.ti, "Olá.")
        (m,) = self.mensagens()
        self.assertEqual(m.estado, Msg.Estado.FALHA)
        self.assertIn("e-mail cadastrado", m.erro)
        self.assertEqual(mail.outbox, [])

    def test_o_rh_nao_ve_o_texto_tecnico_do_erro_so_a_ti(self):
        with mock.patch.object(EmailMultiAlternatives, "send", _smtp_que_falha_para("Re:")):
            self.responder_pela_plataforma(self.rh, "x")
        self.assertNotIn("servidor recusou a conexão", self.html_do_chamado(self.rh))
        self.assertIn("servidor recusou a conexão", self.html_do_chamado(self.ti))

    def test_nao_se_conversa_antes_do_email_de_abertura_sair(self):
        ContratacaoChamado.objects.filter(pk=self.ch.pk).update(status=Status.FALHA)
        with self.assertRaises(conv.ConversaRecusada):
            conv.responder(chamado_pk=self.ch.pk, user=self.rh, texto="oi")

    # ── falha de RECEBIMENTO ──
    def test_falha_ao_gravar_e_reprocessavel_a_partir_do_bruto_sem_duplicar(self):
        raw = self.resposta_ti("Resposta importante.")
        with mock.patch.object(conv, "registrar_mensagem_recebida", side_effect=RuntimeError("disco cheio")):
            r = self.ingerir(raw, provider_id="g1")
        self.assertEqual(r.estado, Registro.Estado.FALHA)
        reg = Registro.objects.get(provider_id="g1")
        self.assertEqual(reg.estado, Registro.Estado.FALHA)
        self.assertIn("disco cheio", reg.motivo)
        self.assertTrue(reg.bruto)  # o e-mail ficou guardado
        self.assertEqual(self.mensagens(), [])

        r2 = entrada.reprocessar_falha(registro_pk=reg.pk, user=self.ti)
        self.assertEqual(r2.estado, Registro.Estado.PROCESSADO)
        self.assertEqual(len(self.mensagens()), 1)

        with self.assertRaises(conv.ConversaRecusada):  # já não está em falha
            entrada.reprocessar_falha(registro_pk=reg.pk, user=self.ti)
        self.assertEqual(len(self.mensagens()), 1)

    def test_o_mesmo_email_por_dois_caminhos_vira_uma_mensagem_so(self):
        raw = self.resposta_ti("Uma vez só.", message_id="fixo-123@teste.invalid")
        self.ingerir(raw, provider_id="caixa-1")
        r = self.ingerir(raw, provider_id="caixa-2")  # mesmo Message-ID, outro id no provedor
        self.assertTrue(r.duplicado)
        self.assertEqual(len(self.mensagens()), 1)
        r = self.ingerir(raw, provider_id="caixa-1")  # releitura do mesmo
        self.assertTrue(r.duplicado)
        self.assertEqual(len(self.mensagens()), 1)

    def test_corrida_no_insert_vira_ja_registrado_e_nao_erro(self):
        raw = self.resposta_ti("Corrida.")
        original = entrada._gravar_mensagem

        def gravar_e_depois_falhar(*a, **kw):
            original(*a, **kw)
            from django.db import IntegrityError
            raise IntegrityError("UNIQUE constraint failed: message_id")

        with mock.patch.object(entrada, "_gravar_mensagem", gravar_e_depois_falhar):
            # Simula outra leitura que já gravou: aqui a própria chamada grava e levanta.
            r = self.ingerir(raw, provider_id="g1")
        self.assertEqual(r.estado, Registro.Estado.PROCESSADO)
        self.assertEqual(len(self.mensagens()), 1)


# ─────────────────────────────────────────────────────────────
# Cenário 6 — Preenchimento de vaga pelo RH
# ─────────────────────────────────────────────────────────────

class Cenario6PreenchimentoDaVaga(ChamadosBase):
    DADOS = {
        "preencher-colaborador_nome": "Carlos Souza Lima",
        "preencher-colaborador_email": "carlos@pessoal.invalid",
        "preencher-colaborador_telefone": "(34) 99999-0000",
        "preencher-data_admissao": "2026-12-01",
        "preencher-colaborador_observacoes": "Começa na segunda.",
    }

    def setUp(self):
        super().setUp()
        Contratacao.objects.filter(pk=self.vaga.pk).update(colaborador_nome="")
        self.url = reverse("contratacao_preencher", args=[self.vaga.pk])

    def preencher(self, user=None, **extra):
        self.client.force_login(user or self.rh)
        return self.client.post(self.url, {**self.DADOS, **extra})

    def test_rh_marca_a_vaga_como_preenchida_e_tudo_fica_registrado(self):
        antes = StatusContratacaoChoices.ABERTA
        resp = self.preencher()
        self.assertEqual(resp.status_code, 302)
        v = Contratacao.objects.get(pk=self.vaga.pk)
        self.assertEqual(v.status, StatusContratacaoChoices.VAGA_PREENCHIDA)
        self.assertEqual(v.nome_contratado, "Carlos Souza Lima")
        self.assertEqual(v.colaborador_email, "carlos@pessoal.invalid")
        self.assertEqual(v.colaborador_telefone, "(34) 99999-0000")
        self.assertEqual(str(v.data_admissao), "2026-12-01")
        self.assertEqual(v.colaborador_observacoes, "Começa na segunda.")
        self.assertEqual(v.preenchida_por, self.rh)
        self.assertIsNotNone(v.vaga_preenchida_em)

        h = ContratacaoVagaHistorico.objects.get(contratacao=v)
        self.assertEqual((h.acao, h.status_antes, h.status_depois, h.criado_por),
                         ("preenchimento", antes, StatusContratacaoChoices.VAGA_PREENCHIDA, self.rh))
        self.assertEqual(h.depois["colaborador_nome"], "Carlos Souza Lima")
        evento = v.eventos.filter(tipo="etapa").latest("pk")
        self.assertIn("Em aberto → Vaga preenchida", evento.descricao)
        self.assertEqual(evento.criado_por, self.rh)

    def test_o_detalhe_e_o_painel_mostram_o_contratado_e_o_responsavel(self):
        self.preencher()
        self.client.force_login(self.rh)
        html = self.client.get(reverse("contratacao_detail", args=[self.vaga.pk])).content.decode()
        for trecho in ("Carlos Souza Lima", "carlos@pessoal.invalid", "01/12/2026", "Rh.Maria"):
            self.assertIn(trecho, html)
        painel = self.client.get(reverse("contratacao_painel"))
        colunas = {c["chave"]: c["total"] for c in painel.context["painel"]["colunas"]}
        self.assertEqual(colunas[StatusContratacaoChoices.VAGA_PREENCHIDA], 1)
        self.assertEqual(colunas[StatusContratacaoChoices.ABERTA], 0)
        self.assertEqual(painel.context["painel"]["kpis"]["abertas"], 0)

    def test_so_o_nome_e_obrigatorio_e_o_resto_pode_faltar(self):
        resp = self.preencher(**{k: "" for k in self.DADOS if k != "preencher-colaborador_nome"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Contratacao.objects.get(pk=self.vaga.pk).status, StatusContratacaoChoices.VAGA_PREENCHIDA)

    def test_sem_nome_nem_cadastro_a_janela_reabre_com_o_que_foi_digitado(self):
        resp = self.preencher(**{"preencher-colaborador_nome": ""})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Informe quem foi contratado")
        self.assertContains(resp, "carlos@pessoal.invalid")  # o digitado voltou
        self.assertContains(resp, 'id="ctrPreencherModal" data-ctr-dialogo aria-labelledby="ctrPreencherTitulo"\n          data-abrir="1"')
        self.assertEqual(Contratacao.objects.get(pk=self.vaga.pk).status, StatusContratacaoChoices.ABERTA)

    def test_email_telefone_e_data_invalidos_sao_recusados(self):
        for campo, valor in (("preencher-colaborador_email", "isto-nao-e-email"),
                             ("preencher-colaborador_telefone", "<script>"),
                             ("preencher-data_admissao", "1990-01-01")):
            with self.subTest(campo=campo):
                resp = self.preencher(**{campo: valor})
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(Contratacao.objects.get(pk=self.vaga.pk).status, StatusContratacaoChoices.ABERTA)

    def test_so_a_vaga_em_aberto_pode_ser_preenchida_de_novo_nao(self):
        self.preencher()
        resp = self.preencher(**{"preencher-colaborador_nome": "Outro Nome"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Só uma vaga em aberto")
        self.assertEqual(Contratacao.objects.get(pk=self.vaga.pk).nome_contratado, "Carlos Souza Lima")

    def test_quem_nao_pode_nao_preenche_nem_pelo_servico(self):
        self.client.force_login(self.olheiro)
        resp = self.client.post(self.url, self.DADOS)
        self.assertEqual(resp.status_code, 302)                 # o middleware devolve para a área dele
        self.assertNotIn("contratacoes", resp["Location"])
        self.assertEqual(Contratacao.objects.get(pk=self.vaga.pk).status, StatusContratacaoChoices.ABERTA)
        with self.assertRaises(PermissionDenied):
            cs.preencher_vaga(contratacao_pk=self.vaga.pk, dados={"colaborador_nome": "X"}, user=self.olheiro)

    def test_ti_tambem_preenche_e_pelo_caminho_antigo_da_etapa(self):
        self.client.force_login(self.ti)
        self.client.post(reverse("contratacao_etapa", args=[self.vaga.pk]),
                         {"acao": "preencher_vaga", "colaborador_nome": "Via TI"})
        v = Contratacao.objects.get(pk=self.vaga.pk)
        self.assertEqual((v.status, v.nome_contratado, v.preenchida_por),
                         (StatusContratacaoChoices.VAGA_PREENCHIDA, "Via TI", self.ti))

    # ── corrigir ──
    def test_corrigir_exige_motivo_guarda_de_para_e_nao_muda_o_status(self):
        self.preencher()
        url = reverse("contratacao_colaborador_corrigir", args=[self.vaga.pk])
        self.client.force_login(self.rh)
        base = {"corrigir-colaborador_nome": "Carlos Souza Lima", "corrigir-colaborador_email": "novo@pessoal.invalid",
                "corrigir-colaborador_telefone": "(34) 99999-0000", "corrigir-data_admissao": "2026-12-01",
                "corrigir-colaborador_observacoes": "Começa na segunda."}
        sem_motivo = self.client.post(url, {**base, "corrigir-motivo": ""})
        self.assertEqual(sem_motivo.status_code, 200)  # recusado
        self.assertEqual(Contratacao.objects.get(pk=self.vaga.pk).colaborador_email, "carlos@pessoal.invalid")

        self.client.post(url, {**base, "corrigir-motivo": "E-mail digitado errado"})
        v = Contratacao.objects.get(pk=self.vaga.pk)
        self.assertEqual(v.colaborador_email, "novo@pessoal.invalid")
        self.assertEqual(v.status, StatusContratacaoChoices.VAGA_PREENCHIDA)
        h = ContratacaoVagaHistorico.objects.filter(acao="correcao").get()
        self.assertEqual(h.motivo, "E-mail digitado errado")
        self.assertEqual(h.antes["colaborador_email"], "carlos@pessoal.invalid")  # o anterior foi preservado
        self.assertEqual(h.depois["colaborador_email"], "novo@pessoal.invalid")

    def test_correcao_sem_mudanca_e_recusada(self):
        self.preencher()
        with self.assertRaises(cs.TransicaoRecusada):
            cs.corrigir_colaborador(
                contratacao_pk=self.vaga.pk, motivo="sem mudar nada", user=self.rh,
                dados={"colaborador": None, "colaborador_nome": "Carlos Souza Lima",
                       "colaborador_email": "carlos@pessoal.invalid", "colaborador_telefone": "(34) 99999-0000",
                       "data_admissao": Contratacao.objects.get(pk=self.vaga.pk).data_admissao,
                       "colaborador_observacoes": "Começa na segunda."},
            )

    # ── reabrir ──
    def test_reabrir_exige_motivo_limpa_o_contratado_e_guarda_quem_era(self):
        self.preencher()
        url = reverse("contratacao_vaga_reabrir", args=[self.vaga.pk])
        self.client.force_login(self.rh)
        self.assertEqual(self.client.post(url, {"reabrir-motivo": ""}).status_code, 200)
        self.assertEqual(Contratacao.objects.get(pk=self.vaga.pk).status, StatusContratacaoChoices.VAGA_PREENCHIDA)

        self.client.post(url, {"reabrir-motivo": "Candidato desistiu"})
        v = Contratacao.objects.get(pk=self.vaga.pk)
        self.assertEqual(v.status, StatusContratacaoChoices.ABERTA)
        self.assertEqual((v.nome_contratado, v.colaborador_email, v.preenchida_por, v.vaga_preenchida_em),
                         ("", "", None, None))
        h = ContratacaoVagaHistorico.objects.filter(acao="reabertura").get()
        self.assertEqual(h.motivo, "Candidato desistiu")
        self.assertEqual(h.antes["colaborador_nome"], "Carlos Souza Lima")
        self.assertEqual((h.status_antes, h.status_depois),
                         (StatusContratacaoChoices.VAGA_PREENCHIDA, StatusContratacaoChoices.ABERTA))
        # E o histórico do preenchimento anterior continua lá.
        self.assertEqual(ContratacaoVagaHistorico.objects.filter(acao="preenchimento").count(), 1)

    def test_etapas_alem_de_preenchida_so_a_ti_reabre(self):
        self.preencher()
        Contratacao.objects.filter(pk=self.vaga.pk).update(status=StatusContratacaoChoices.EQUIPAMENTO_ENTREGUE)
        with self.assertRaises(cs.TransicaoRecusada):
            cs.reabrir_vaga(contratacao_pk=self.vaga.pk, motivo="queria reabrir", user=self.rh)

    def test_a_vaga_nao_e_excluida_e_o_historico_aparece_na_consulta(self):
        self.preencher()
        self.client.force_login(self.rh)
        html = self.client.get(reverse("contratacao_detail", args=[self.vaga.pk])).content.decode()
        self.assertIn("Histórico do preenchimento (1)", html)
        self.assertTrue(Contratacao.objects.filter(pk=self.vaga.pk).exists())


# ─────────────────────────────────────────────────────────────
# Cenário 7 — Pendências de TI depois do preenchimento
# ─────────────────────────────────────────────────────────────

class Cenario7PendenciasDeTIAposPreencher(ConversaBase):
    def setUp(self):
        super().setUp()
        self.pedir(self.rh, usuario_email="on", criar_usuario_rede="on", criar_email="on")
        self.ch_usuario = self.chamado(Tipo.USUARIO_EMAIL)
        conv.atualizar_atendimento(chamado_pk=self.ch_usuario.pk, para=Atend.CONCLUIDO, motivo="Criado", user=self.ti)
        conv.atualizar_atendimento(chamado_pk=self.ch.pk, para=Atend.EM_ANDAMENTO, motivo="", user=self.ti)
        self.client.force_login(self.rh)
        self.client.post(reverse("contratacao_preencher", args=[self.vaga.pk]),
                         {"preencher-colaborador_nome": "Carlos Souza"})
        mail.outbox = []

    def test_a_vaga_fica_preenchida_e_os_chamados_mantem_a_propria_situacao(self):
        self.assertEqual(Contratacao.objects.get(pk=self.vaga.pk).status, StatusContratacaoChoices.VAGA_PREENCHIDA)
        self.ch.refresh_from_db()
        self.ch_usuario.refresh_from_db()
        self.assertEqual(self.ch.atendimento, Atend.EM_ANDAMENTO)
        self.assertEqual(self.ch_usuario.atendimento, Atend.CONCLUIDO)

    def test_rh_e_ti_acompanham_as_pendencias_ate_a_conclusao(self):
        html = self.html_do_chamado(self.rh)  # conversa continua acessível na vaga preenchida
        self.assertIn("Em andamento", html)

        self.client.force_login(self.rh)
        detalhe = self.client.get(reverse("contratacao_detail", args=[self.vaga.pk]))
        resumo = detalhe.context["chamados_resumo"]
        self.assertEqual((resumo.total, resumo.pendentes), (3 - 1, 1))  # máquina pendente; usuário concluído
        self.assertEqual(resumo.por_equipe, [("Configuração e entrega de máquina", "Equipe de TI")])
        self.assertContains(detalhe, "Ainda com a TI")

        painel = self.client.get(reverse("contratacao_painel"))
        self.assertContains(painel, "1 chamado com a TI")

        lista = self.client.get(reverse("chamados_lista") + "?grupo=em_atendimento")
        self.assertEqual([c.pk for c in lista.context["chamados"]], [self.ch.pk])

    def test_concluir_o_chamado_nao_muda_a_vaga_nem_encerra_os_outros(self):
        conv.atualizar_atendimento(chamado_pk=self.ch.pk, para=Atend.CONCLUIDO, motivo="Entregue", user=self.ti)
        v = Contratacao.objects.get(pk=self.vaga.pk)
        self.assertEqual(v.status, StatusContratacaoChoices.VAGA_PREENCHIDA)  # a vaga não mudou
        self.client.force_login(self.rh)
        painel = self.client.get(reverse("contratacao_painel"))
        self.assertNotContains(painel, "chamado com a TI")  # nada pendente: o selo some

    def test_cancelar_o_chamado_tambem_nao_mexe_na_vaga(self):
        conv.atualizar_atendimento(chamado_pk=self.ch.pk, para=Atend.CANCELADO, motivo="Não precisa", user=self.ti)
        self.assertEqual(Contratacao.objects.get(pk=self.vaga.pk).status, StatusContratacaoChoices.VAGA_PREENCHIDA)

    def test_reenvio_do_email_que_falhou_continua_possivel_com_a_vaga_preenchida(self):
        # Um terceiro chamado cuja abertura falhou ANTES de a vaga ser preenchida.
        Contratacao.objects.filter(pk=self.vaga.pk).update(status=StatusContratacaoChoices.ABERTA)
        with mock.patch.object(EmailMultiAlternatives, "send", _smtp_que_falha_para("integração aos sistemas")):
            self.pedir(self.rh, sistemas="on", sistemas_suporte=["liberacao"])
        falho = self.chamado(Tipo.SISTEMAS)
        self.assertEqual(falho.status, Status.FALHA)
        Contratacao.objects.filter(pk=self.vaga.pk).update(status=StatusContratacaoChoices.VAGA_PREENCHIDA)

        chamados_svc.reenviar(contratacao_pk=self.vaga.pk, chamado_pk=falho.pk, user=self.rh)
        falho.refresh_from_db()
        self.assertEqual(falho.status, Status.ENVIADO)

    def test_vaga_encerrada_nao_retoma_o_envio(self):
        with mock.patch.object(EmailMultiAlternatives, "send", _smtp_que_falha_para("integração aos sistemas")):
            Contratacao.objects.filter(pk=self.vaga.pk).update(status=StatusContratacaoChoices.ABERTA)
            self.pedir(self.rh, sistemas="on", sistemas_suporte=["liberacao"])
        falho = self.chamado(Tipo.SISTEMAS)
        Contratacao.objects.filter(pk=self.vaga.pk).update(status=StatusContratacaoChoices.CONCLUIDA)
        with self.assertRaises(chamados_svc.SolicitacaoRecusada):
            chamados_svc.reenviar(contratacao_pk=self.vaga.pk, chamado_pk=falho.pk, user=self.rh)

    def test_conversa_continua_com_a_vaga_preenchida(self):
        self.responder_pela_plataforma(self.rh, "Alguma novidade?")
        self.assertEqual(len(self.mensagens()), 1)
        self.ingerir(self.resposta_ti("Sim, amanhã."), provider_id="g1")
        self.assertEqual(len(self.mensagens()), 2)


# ─────────────────────────────────────────────────────────────
# Remetente e autenticidade
# ─────────────────────────────────────────────────────────────

class RemetenteEAutenticidadeTests(ConversaBase):
    def test_remetente_desconhecido_vai_para_revisao_mesmo_citando_o_message_id_certo(self):
        r = self.ingerir(eml(de="Intruso <intruso@externo.invalid>", para="datasul@teste.invalid",
                             corpo="Aprovado, pode liberar o acesso.", em_resposta_a=self.abertura.message_id),
                         provider_id="g1")
        self.assertEqual(r.estado, Registro.Estado.QUARENTENA)
        self.assertIn("não reconhecido", r.motivo)
        self.assertEqual(self.mensagens(), [])
        self.assertNotIn("Aprovado, pode liberar", self.html_do_chamado(self.rh))  # nada vaza para a conversa

    def test_falha_de_spf_dkim_dmarc_vai_para_revisao_mesmo_de_remetente_conhecido(self):
        for resultado in ("spf=fail", "dkim=fail", "dmarc=fail", "compauth=fail reason=000"):
            with self.subTest(resultado=resultado):
                r = self.ingerir(self.resposta_ti(extras={"Authentication-Results": f"spf.protection.outlook.com; {resultado}"}),
                                 provider_id=f"g-{resultado}")
                self.assertEqual(r.estado, Registro.Estado.QUARENTENA)
                self.assertIn("autenticidade", r.motivo)
        self.assertEqual(self.mensagens(), [])

    def test_autenticacao_aprovada_passa(self):
        r = self.ingerir(self.resposta_ti(extras={"Authentication-Results": "spf=pass; dkim=pass; dmarc=pass"}),
                         provider_id="g1")
        self.assertEqual(r.estado, Registro.Estado.PROCESSADO)

    def test_ti_libera_o_email_em_revisao_e_o_remetente_passa_a_ser_participante(self):
        self.ingerir(eml(de="Terceiro <terceiro@parceiro.invalid>", para="datasul@teste.invalid",
                         corpo="Sou da empresa que presta o suporte.", em_resposta_a=self.abertura.message_id),
                     provider_id="g1")
        reg = Registro.objects.get(provider_id="g1")
        msg = entrada.liberar_quarentena(registro_pk=reg.pk, lado=Msg.Lado.OUTRO, user=self.ti)
        self.assertEqual((msg.lado, msg.remetente_email), (Msg.Lado.OUTRO, "terceiro@parceiro.invalid"))
        reg.refresh_from_db()
        self.assertEqual((reg.estado, reg.resolvido_por), (Registro.Estado.PROCESSADO, self.ti))

        # Próxima mensagem do mesmo endereço, no mesmo chamado: passa sem revisão.
        r = self.ingerir(eml(de="Terceiro <terceiro@parceiro.invalid>", para="datasul@teste.invalid",
                             corpo="Segunda mensagem.", em_resposta_a=self.abertura.message_id), provider_id="g2")
        self.assertEqual(r.estado, Registro.Estado.PROCESSADO)

    def test_descartar_remove_o_bruto_e_nada_entra_na_conversa(self):
        self.ingerir(eml(de="Intruso <intruso@externo.invalid>", para="datasul@teste.invalid",
                         em_resposta_a=self.abertura.message_id), provider_id="g1")
        reg = Registro.objects.get(provider_id="g1")
        self.assertTrue(reg.bruto)
        entrada.descartar_quarentena(registro_pk=reg.pk, user=self.ti)
        reg.refresh_from_db()
        self.assertEqual(reg.estado, Registro.Estado.DESCARTADO)
        self.assertFalse(reg.bruto)
        self.assertEqual(self.mensagens(), [])

    def test_so_a_ti_trata_a_quarentena(self):
        self.ingerir(eml(de="Intruso <intruso@externo.invalid>", para="datasul@teste.invalid",
                         em_resposta_a=self.abertura.message_id), provider_id="g1")
        reg = Registro.objects.get(provider_id="g1")
        for fn, kw in ((entrada.liberar_quarentena, {"lado": "rh"}), (entrada.descartar_quarentena, {})):
            with self.assertRaises(PermissionDenied):
                fn(registro_pk=reg.pk, user=self.rh, **kw)

    def test_resposta_automatica_e_o_que_o_proprio_sistema_enviou_sao_ignorados(self):
        auto = self.ingerir(self.resposta_ti(assunto="Resposta automática: Re: Chamado",
                                             extras={"Auto-Submitted": "auto-replied"}), provider_id="g1")
        self.assertEqual((auto.estado, auto.motivo), (Registro.Estado.IGNORADO, "resposta automática"))
        from django.conf import settings
        proprio = self.ingerir(eml(de=settings.EMAIL_HOST_USER or "datasul@teste.invalid", para=self.ti_externo, corpo="eco",
                                   em_resposta_a=self.abertura.message_id), provider_id="g2")
        self.assertEqual(proprio.estado, Registro.Estado.IGNORADO)
        self.assertEqual(self.mensagens(), [])

    def test_email_ignorado_nao_retem_conteudo(self):
        self.ingerir(eml(de="Alguem <alguem@x.invalid>", para="datasul@teste.invalid",
                         assunto="Assunto confidencial qualquer", corpo="nada a ver"), provider_id="g1")
        reg = Registro.objects.get(provider_id="g1")
        self.assertEqual((reg.estado, reg.assunto, reg.remetente), (Registro.Estado.IGNORADO, "", ""))
        self.assertFalse(reg.bruto)

    def test_aviso_de_nao_entrega_marca_a_mensagem_nossa_e_nao_vira_mensagem(self):
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        rel = MIMEMultipart("report", report_type="delivery-status")
        rel["From"] = "mailer-daemon@teste.invalid"
        rel["To"] = "datasul@teste.invalid"
        rel["Subject"] = "Undeliverable"
        rel["Message-ID"] = "<dsn-2@teste.invalid>"
        rel["In-Reply-To"] = f"<{self.abertura.message_id}>"
        rel.attach(MIMEText("Delivery has failed: caixa do destinatário cheia", "plain"))
        r = self.ingerir(rel.as_bytes(), provider_id="g1")
        self.assertEqual(r.estado, Registro.Estado.PROCESSADO)
        self.assertEqual(self.mensagens(), [])
        self.abertura.refresh_from_db()
        self.assertTrue(self.abertura.entrega_recusada)
        self.assertIn("Entrega recusada", self.html_do_chamado(self.ti))


# ─────────────────────────────────────────────────────────────
# Situação do chamado: independente, só a TI muda, tudo registrado
# ─────────────────────────────────────────────────────────────

class SituacaoDoChamadoTests(ConversaBase):
    def test_so_a_ti_muda_a_situacao_pela_tela_e_pelo_service(self):
        self.client.force_login(self.rh)
        url = reverse("chamado_atendimento", args=[self.vaga.pk, self.ch.pk])
        self.assertEqual(self.client.post(url, {"situacao": "concluido"}).status_code, 403)
        with self.assertRaises(PermissionDenied):
            conv.atualizar_atendimento(chamado_pk=self.ch.pk, para=Atend.CONCLUIDO, motivo="", user=self.rh)
        self.ch.refresh_from_db()
        self.assertEqual(self.ch.atendimento, Atend.AGUARDANDO_ATENDIMENTO)

    def test_mudanca_registra_responsavel_data_e_motivo(self):
        self.client.force_login(self.ti)
        self.client.post(reverse("chamado_atendimento", args=[self.vaga.pk, self.ch.pk]),
                         {"situacao": "em_andamento", "motivo": "Separando o equipamento", "responsavel": str(self.ti.pk)})
        self.ch.refresh_from_db()
        self.assertEqual((self.ch.atendimento, self.ch.responsavel), (Atend.EM_ANDAMENTO, self.ti))
        log = ChamadoStatusLog.objects.get(chamado=self.ch)
        self.assertEqual((log.de, log.para, log.motivo, log.automatico, log.criado_por),
                         (Atend.AGUARDANDO_ATENDIMENTO, Atend.EM_ANDAMENTO, "Separando o equipamento", False, self.ti))
        self.assertIsNotNone(log.created_at)
        self.assertIn("Separando o equipamento", self.html_do_chamado(self.rh))

    def test_os_sete_estados_existem_na_ordem_da_especificacao(self):
        self.assertEqual(
            [v for v, _ in Atend.choices],
            ["aberto", "aguardando_atendimento", "em_andamento", "aguardando_rh",
             "aguardando_informacoes", "concluido", "cancelado"],
        )

    def test_mudar_para_o_mesmo_estado_nao_grava_historico_vazio(self):
        with self.assertRaises(conv.ConversaRecusada):
            conv.atualizar_atendimento(chamado_pk=self.ch.pk, para=Atend.AGUARDANDO_ATENDIMENTO, motivo="", user=self.ti)
        self.assertEqual(ChamadoStatusLog.objects.count(), 0)

    def test_responsavel_precisa_ser_da_ti(self):
        with self.assertRaises(conv.ConversaRecusada):
            conv.atualizar_atendimento(chamado_pk=self.ch.pk, para=Atend.EM_ANDAMENTO, motivo="",
                                       user=self.ti, responsavel_id=self.rh.pk)

    def test_registrar_o_numero_do_chamado_leva_a_situacao_para_aberto_uma_vez(self):
        chamados_svc.registrar_identificador(contratacao_pk=self.vaga.pk, chamado_pk=self.ch.pk,
                                             identificador="48213", user=self.ti)
        self.ch.refresh_from_db()
        self.assertEqual(self.ch.atendimento, Atend.ABERTO)
        # A TI põe em andamento; corrigir o número depois NÃO volta o chamado para trás.
        conv.atualizar_atendimento(chamado_pk=self.ch.pk, para=Atend.EM_ANDAMENTO, motivo="", user=self.ti)
        chamados_svc.registrar_identificador(contratacao_pk=self.vaga.pk, chamado_pk=self.ch.pk,
                                             identificador="48214", user=self.ti)
        self.ch.refresh_from_db()
        self.assertEqual(self.ch.atendimento, Atend.EM_ANDAMENTO)

    def test_a_situacao_do_chamado_nao_mexe_na_vaga(self):
        for para in (Atend.EM_ANDAMENTO, Atend.CONCLUIDO, Atend.CANCELADO):
            conv.atualizar_atendimento(chamado_pk=self.ch.pk, para=para, motivo="", user=self.ti)
        self.assertEqual(Contratacao.objects.get(pk=self.vaga.pk).status, StatusContratacaoChoices.ABERTA)

    def test_chamado_ja_existente_antes_da_migration_tem_situacao_valida(self):
        legado = ContratacaoChamado.objects.create(
            contratacao=self.vaga, tipo=Tipo.SISTEMAS, status=Status.ENVIADO, dados={},
        )
        self.assertEqual(legado.atendimento, Atend.AGUARDANDO_ATENDIMENTO)
        # Sem mensagem de abertura (criado antes): a tela abre mesmo assim.
        self.client.force_login(self.rh)
        resp = self.client.get(reverse("chamado_detail", args=[self.vaga.pk, legado.pk]))
        self.assertEqual(resp.status_code, 200)


# ─────────────────────────────────────────────────────────────
# Telas e autorização
# ─────────────────────────────────────────────────────────────

class TelasEAutorizacaoTests(ConversaBase):
    def test_a_lista_mostra_os_grupos_e_filtra(self):
        self.client.force_login(self.rh)
        resp = self.client.get(reverse("chamados_lista"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["contagem"], {"pendentes": 1, "em_atendimento": 0, "concluidos": 0})
        self.assertContains(resp, "Configuração e entrega de máquina")
        self.assertEqual(len(self.client.get(reverse("chamados_lista") + "?grupo=concluidos").context["chamados"]), 0)
        self.assertEqual(len(self.client.get(reverse("chamados_lista") + "?q=Analista").context["chamados"]), 1)
        self.assertEqual(len(self.client.get(reverse("chamados_lista") + "?q=zzz").context["chamados"]), 0)

    def test_rh_nao_ve_o_atalho_da_sincronizacao_nem_a_pagina(self):
        self.client.force_login(self.rh)
        self.assertNotContains(self.client.get(reverse("chamados_lista")), reverse("chamados_sincronizacao"))
        self.assertEqual(self.client.get(reverse("chamados_sincronizacao")).status_code, 403)
        self.assertEqual(self.client.post(reverse("chamados_sincronizacao_acao"), {"acao": "sincronizar"}).status_code, 403)

    def test_quem_so_tem_consulta_do_quiosque_nao_alcanca_os_chamados(self):
        self.client.force_login(self.olheiro)
        for url in (reverse("chamados_lista"), self.url_chamado()):
            self.assertNotEqual(self.client.get(url).status_code, 200, url)

    def test_os_endpoints_de_escrita_so_aceitam_post(self):
        self.client.force_login(self.rh)
        self.assertEqual(self.client.get(reverse("chamado_responder", args=[self.vaga.pk, self.ch.pk])).status_code, 405)

    def test_chamado_de_outra_vaga_nao_e_alcancado_pela_url(self):
        outra = Contratacao.objects.create(cargo="Outra", tipo_vaga="nova", criado_por=self.ti)
        self.client.force_login(self.ti)
        resp = self.client.get(reverse("chamado_detail", args=[outra.pk, self.ch.pk]))
        self.assertEqual(resp.status_code, 404)

    def test_aviso_honesto_quando_a_caixa_nao_esta_configurada(self):
        html = self.html_do_chamado(self.rh)
        self.assertIn("não está configurada", html)
        self.assertIn("Responder por esta tela funciona", html)
        self.assertNotIn("datasul@", html)  # nenhum endereço para o RH

    def test_cada_chamado_mostra_o_que_a_especificacao_pede(self):
        """Identificação, tipo, assunto, responsável/equipe, abertura, situação atual,
        e por mensagem: quem escreveu, quando e o conteúdo."""
        self.ingerir(self.resposta_ti("Conteúdo da resposta."), provider_id="g1")
        html = self.html_do_chamado(self.rh)
        self.assertIn(f"Chamado nº {self.ch.pk}", html)                      # identificação
        self.assertIn("Configuração e entrega de máquina", html)             # tipo
        self.assertIn(self.ch.assunto.split(" — ")[0], html)                 # assunto
        self.assertIn("Equipe de TI", html)                                  # responsável / equipe
        self.assertIn(self.ch.created_at.astimezone().strftime("%d/%m/%Y"), html)  # abertura
        self.assertIn("Aguardando atendimento", html)                        # situação atual
        self.assertIn("Conversa", html)
        self.assertIn("Conteúdo da resposta.", html)                         # conteúdo
        self.assertIn(self.ti_externo, self.html_do_chamado(self.ti))        # remetente (endereço só para a TI)
        self.assertNotIn(self.ti_externo, html)                              # ... e não para o RH

    def test_a_tela_nao_diz_chamado_aberto_nem_entregue(self):
        html = self.html_do_chamado(self.ti).lower()
        self.assertNotIn("entregue ao destinatário", html)
        self.assertNotIn("chamado aberto com sucesso", html)
        self.assertIn("isso não confirma a abertura do chamado", html)


# ─────────────────────────────────────────────────────────────
# Sincronização com a caixa (transporte falso)
# ─────────────────────────────────────────────────────────────

class FonteFalsa:
    """O que o Graph devolve: itens + o bruto de cada um."""

    def __init__(self, itens):
        self.itens = itens          # [{"id", "recebido_em", "conversation_id", "raw"}]
        self.baixados = []
        self.quebrar = set()

    def listar(self, desde):
        for i in self.itens:
            yield {k: v for k, v in i.items() if k != "raw"}

    def baixar(self, provider_id):
        self.baixados.append(provider_id)
        if provider_id in self.quebrar:
            raise caixa_graph_service.CaixaIndisponivel("HTTP 503")
        return next(i["raw"] for i in self.itens if i["id"] == provider_id)


class SincronizacaoTests(ConversaBase):
    def _item(self, raw, pid, minutos=0):
        from django.utils import timezone
        from datetime import timedelta
        return {"id": pid, "recebido_em": timezone.now() - timedelta(minutes=minutos), "conversation_id": "", "raw": raw}

    def test_sem_configuracao_a_sincronizacao_fica_desligada_e_diz_isso(self):
        with override_settings(CHAMADOS_GRAPH_TENANT_ID="", CHAMADOS_GRAPH_CLIENT_ID="", CHAMADOS_GRAPH_CLIENT_SECRET=""):
            resumo = entrada.sincronizar()
        self.assertFalse(resumo.configurada)
        self.assertIn("desligada", resumo.texto())

    def test_le_a_caixa_registra_o_que_e_do_chamado_ignora_o_resto_e_nao_duplica(self):
        fonte = FonteFalsa([
            self._item(self.resposta_ti("Resposta A."), "p1", 5),
            self._item(eml(de="Outro <o@x.invalid>", para="datasul@teste.invalid", corpo="spam"), "p2", 4),
            self._item(self.resposta_rh("Resposta B."), "p3", 3),
        ])
        r1 = entrada.sincronizar(fonte=fonte)
        self.assertEqual((r1.lidos, r1.registrados, r1.ignorados, r1.falhas), (3, 2, 1, 0))
        self.assertEqual(len(self.mensagens()), 2)
        self.assertIsNotNone(ChamadoCaixaEstado.get().ultimo_sucesso)
        self.assertIn("2 registrado", ChamadoCaixaEstado.get().ultimo_resumo)

        fonte.baixados.clear()
        r2 = entrada.sincronizar(fonte=fonte)  # a mesma caixa de novo
        self.assertEqual((r2.registrados, r2.duplicados), (0, 3))
        self.assertEqual(fonte.baixados, [], "e-mail já tratado não é baixado de novo")
        self.assertEqual(len(self.mensagens()), 2)

    def test_falha_ao_baixar_e_registrada_e_recuperada_na_proxima_leitura(self):
        fonte = FonteFalsa([self._item(self.resposta_ti("Resposta."), "p1")])
        fonte.quebrar = {"p1"}
        r1 = entrada.sincronizar(fonte=fonte)
        self.assertEqual(r1.falhas, 1)
        reg = Registro.objects.get(provider_id="p1")
        self.assertEqual(reg.estado, Registro.Estado.FALHA)
        self.assertIn("503", reg.motivo)
        self.assertEqual(self.mensagens(), [])

        fonte.quebrar = set()
        r2 = entrada.sincronizar(fonte=fonte)
        self.assertEqual(len(self.mensagens()), 1)
        reg.refresh_from_db()
        self.assertEqual(reg.estado, Registro.Estado.PROCESSADO)
        self.assertEqual(r2.reprocessados + r2.registrados, 1)
        self.assertEqual(len(self.mensagens()), 1)  # e sem duplicar

    def test_erro_da_caixa_nao_derruba_a_execucao_e_fica_registrado(self):
        class Quebrada:
            def listar(self, desde):
                raise caixa_graph_service.CaixaIndisponivel("O provedor recusou a credencial (HTTP 401).")
                yield  # pragma: no cover

        resumo = entrada.sincronizar(fonte=Quebrada())
        self.assertIn("401", resumo.erro)
        estado = ChamadoCaixaEstado.get()
        self.assertIn("401", estado.ultimo_erro)
        self.assertIsNone(estado.ultimo_sucesso)

    def test_o_comando_sinaliza_erro_com_codigo_diferente_de_zero(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError
        with mock.patch.object(entrada, "sincronizar", return_value=entrada.ResumoSincronizacao(erro="HTTP 401")):
            with self.assertRaises(CommandError):
                call_command("sincronizar_chamados")

    def test_o_comando_importa_um_eml(self):
        import os
        from django.core.management import call_command
        caminho = os.path.join(_DISCO, "resposta.eml")
        with open(caminho, "wb") as f:
            f.write(self.resposta_ti("Importada à mão."))
        call_command("sincronizar_chamados", arquivo=[caminho])
        (m,) = self.mensagens()
        self.assertEqual(m.corpo, "Importada à mão.")


@override_settings(CHAMADOS_GRAPH_TENANT_ID="t", CHAMADOS_GRAPH_CLIENT_ID="c",
                   CHAMADOS_GRAPH_CLIENT_SECRET="x", CHAMADOS_CAIXA="caixa@teste.invalid")
class CaixaConfiguradaTests(ConversaBase):
    """Com a caixa CONFIGURADA o caminho da tela é outro (leitura em dia ou parada)
    — e é o de produção. Sem estes testes ele só rodaria depois do deploy."""

    def test_caixa_configurada_e_nunca_lida_aparece_como_parada(self):
        for user in (self.rh, self.ti):
            html = self.html_do_chamado(user)
            self.assertIn("está <strong>parada</strong>", html, user.username)
            self.assertNotIn("não está configurada", html)
        self.client.force_login(self.rh)
        self.assertContains(self.client.get(reverse("chamados_lista")), "parada")

    def test_leitura_recente_some_com_o_aviso(self):
        from django.utils import timezone
        estado = ChamadoCaixaEstado.get()
        estado.ultimo_sucesso = timezone.now()
        estado.save()
        self.assertNotIn("<strong>parada</strong>", self.html_do_chamado(self.rh))

    def test_leitura_antiga_volta_a_avisar(self):
        from datetime import timedelta
        from django.utils import timezone
        estado = ChamadoCaixaEstado.get()
        estado.ultimo_sucesso = timezone.now() - timedelta(minutes=40)
        estado.save()
        self.assertIn("<strong>parada</strong>", self.html_do_chamado(self.rh))

    def test_pagina_de_sincronizacao_mostra_a_caixa_lida_e_o_botao(self):
        self.client.force_login(self.ti)
        resp = self.client.get(reverse("chamados_sincronizacao"))
        self.assertContains(resp, "caixa@teste.invalid")
        self.assertContains(resp, "Ler a caixa agora")


class PaginaDeSincronizacaoTests(ConversaBase):
    """A tela da TI e as ações dela pela interface (não só pelo service)."""

    def setUp(self):
        super().setUp()
        self.ingerir(eml(de="Intruso <intruso@externo.invalid>", para="datasul@teste.invalid",
                         assunto="Aprovação", em_resposta_a=self.abertura.message_id), provider_id="rev")
        with mock.patch.object(conv, "registrar_mensagem_recebida", side_effect=RuntimeError("falha de teste")):
            self.ingerir(self.resposta_ti("Vai falhar."), provider_id="fal")
        self.rev = Registro.objects.get(provider_id="rev")
        self.fal = Registro.objects.get(provider_id="fal")
        self.client.force_login(self.ti)
        self.url_acao = reverse("chamados_sincronizacao_acao")

    def test_a_pagina_mostra_a_situacao_a_revisao_e_as_falhas(self):
        resp = self.client.get(reverse("chamados_sincronizacao"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "não está configurada")
        self.assertContains(resp, "Basic authentication is disabled")  # diz POR QUE precisa do Graph
        self.assertContains(resp, "intruso@externo.invalid")
        self.assertContains(resp, "falha de teste")
        self.assertContains(resp, "Liberar na conversa")

    def test_a_lista_da_ti_mostra_o_atalho_com_a_pendencia(self):
        resp = self.client.get(reverse("chamados_lista"))
        self.assertContains(resp, reverse("chamados_sincronizacao"))

    def test_liberar_pela_tela_coloca_a_mensagem_na_conversa(self):
        self.client.post(self.url_acao, {"acao": "liberar", "registro": self.rev.pk, "lado": "outro"})
        self.rev.refresh_from_db()
        self.assertEqual(self.rev.estado, Registro.Estado.PROCESSADO)
        self.assertEqual(len(self.mensagens()), 1)

    def test_descartar_pela_tela(self):
        self.client.post(self.url_acao, {"acao": "descartar", "registro": self.rev.pk})
        self.rev.refresh_from_db()
        self.assertEqual(self.rev.estado, Registro.Estado.DESCARTADO)
        self.assertEqual(self.mensagens(), [])

    def test_reprocessar_pela_tela_recupera_a_falha_sem_duplicar(self):
        self.client.post(self.url_acao, {"acao": "reprocessar", "registro": self.fal.pk})
        self.fal.refresh_from_db()
        self.assertEqual(self.fal.estado, Registro.Estado.PROCESSADO)
        self.assertEqual(len(self.mensagens()), 1)
        self.client.post(self.url_acao, {"acao": "reprocessar", "registro": self.fal.pk})  # de novo
        self.assertEqual(len(self.mensagens()), 1)

    def test_ler_agora_sem_configuracao_avisa_e_nao_quebra(self):
        resp = self.client.post(self.url_acao, {"acao": "sincronizar"}, follow=True)
        self.assertContains(resp, "desligada")

    def test_acao_desconhecida_ou_registro_invalido_nao_quebra(self):
        for dados in ({"acao": "apagar_tudo"}, {"acao": "liberar", "registro": "abc"},
                      {"acao": "descartar", "registro": "999999"}, {"acao": "liberar"}):
            with self.subTest(dados=dados):
                self.assertEqual(self.client.post(self.url_acao, dados).status_code, 302)


class TransporteGraphTests(TestCase):
    """O transporte com uma sessão HTTP falsa: nenhuma chamada de rede."""

    class Resp:
        def __init__(self, status=200, json=None, content=b""):
            self.status_code, self._json, self.content = status, json or {}, content

        def json(self):
            return self._json

    class Sessao:
        def __init__(self, respostas):
            self.respostas, self.chamadas = list(respostas), []

        def post(self, url, **kw):
            self.chamadas.append(("POST", url, kw))
            return self.respostas.pop(0)

        def get(self, url, **kw):
            self.chamadas.append(("GET", url, kw))
            return self.respostas.pop(0)

    CFG = dict(CHAMADOS_GRAPH_TENANT_ID="t", CHAMADOS_GRAPH_CLIENT_ID="c",
               CHAMADOS_GRAPH_CLIENT_SECRET="SEGREDO-XYZ-123", CHAMADOS_CAIXA="caixa@teste.invalid")

    def setUp(self):
        caixa_graph_service.limpar_token()

    def test_configurada_exige_as_quatro_chaves(self):
        with override_settings(**self.CFG):
            self.assertTrue(caixa_graph_service.configurada())
        with override_settings(**{**self.CFG, "CHAMADOS_GRAPH_CLIENT_SECRET": ""}):
            self.assertFalse(caixa_graph_service.configurada())

    def test_lista_com_paginacao_e_baixa_o_mime(self):
        R = self.Resp
        sessao = self.Sessao([
            R(200, {"access_token": "tok", "expires_in": 3600}),
            R(200, {"value": [{"id": "A", "receivedDateTime": "2026-11-09T10:00:00Z", "conversationId": "c1"}],
                    "@odata.nextLink": "https://graph.microsoft.com/next"}),
            R(200, {"value": [{"id": "B", "receivedDateTime": "2026-11-09T10:05:00Z", "conversationId": "c2"}]}),
            R(200, content=b"MIME-BRUTO"),
        ])
        from django.utils import timezone
        with override_settings(**self.CFG):
            caixa = caixa_graph_service.CaixaGraph(sessao)
            itens = list(caixa.listar(timezone.now()))
            self.assertEqual([i["id"] for i in itens], ["A", "B"])
            self.assertEqual(itens[0]["conversation_id"], "c1")
            self.assertEqual(caixa.baixar("A"), b"MIME-BRUTO")
        tipos = [c[0] for c in sessao.chamadas]
        self.assertEqual(tipos, ["POST", "GET", "GET", "GET"])
        self.assertEqual(sessao.chamadas[1][2]["headers"]["Authorization"], "Bearer tok")
        self.assertIn("receivedDateTime ge", sessao.chamadas[1][2]["params"]["$filter"])
        self.assertTrue(sessao.chamadas[3][1].endswith("/$value"))

    def test_erros_do_provedor_viram_mensagem_clara(self):
        R = self.Resp
        for status, trecho in ((403, "permissão"), (401, "recusou o acesso"), (429, "limitou"), (500, "HTTP 500")):
            with self.subTest(status=status):
                caixa_graph_service.limpar_token()
                sessao = self.Sessao([R(200, {"access_token": "tok", "expires_in": 3600}), R(status)])
                with override_settings(**self.CFG):
                    with self.assertRaises(caixa_graph_service.CaixaIndisponivel) as ctx:
                        caixa_graph_service.CaixaGraph(sessao).baixar("X")
                self.assertIn(trecho, str(ctx.exception))

    def test_credencial_recusada_nao_vaza_o_segredo_na_mensagem(self):
        sessao = self.Sessao([self.Resp(401)])
        with override_settings(**self.CFG):
            with self.assertRaises(caixa_graph_service.CaixaIndisponivel) as ctx:
                caixa_graph_service.CaixaGraph(sessao).baixar("X")
        self.assertNotIn(self.CFG["CHAMADOS_GRAPH_CLIENT_SECRET"], str(ctx.exception))


# ─────────────────────────────────────────────────────────────
# Unidades puras
# ─────────────────────────────────────────────────────────────

class UnidadesTests(TestCase):
    def test_referencia_assinada(self):
        ref = conv.referencia_do_chamado(42)
        self.assertEqual(conv.chamados_da_referencia(f"texto {ref} texto"), {42})
        self.assertEqual(conv.chamados_da_referencia("CH-42-deadbeef"), set())
        self.assertEqual(conv.chamados_da_referencia("CH-43" + ref[5:]), set())  # assinatura de outro id

    def test_ids_de_cabecalho(self):
        self.assertEqual(conv.ids_de_cabecalho("<A@x> <B@Y>\n <c@z>"), ["a@x", "b@y", "c@z"])
        self.assertEqual(conv.normalizar_id("  <Abc@X.com> "), "abc@x.com")

    def test_message_ids_novos_sao_unicos(self):
        self.assertEqual(len({conv.novo_message_id() for _ in range(200)}), 200)

    def test_corte_da_citacao_nos_formatos_dos_clientes_de_email(self):
        cortar = entrada.texto_sem_citacao
        self.assertEqual(cortar("Ok.\n\nEm seg., 9 de nov. de 2026 às 10:00, Maria <m@x> escreveu:\n> antes"), "Ok.")
        self.assertEqual(cortar("Ok.\n\nOn Mon, Nov 9, 2026 at 10:00 AM Maria <m@x>\nwrote:\n> before"), "Ok.")
        self.assertEqual(cortar("Ok.\n\n-----Original Message-----\nFrom: x"), "Ok.")
        self.assertEqual(cortar("Ok.\n\nDe: Maria\nEnviado: segunda\nPara: TI\nAssunto: x\n\ntexto"), "Ok.")
        self.assertEqual(cortar("Ok.\n> citado\nfim"), "Ok.\nfim")
        self.assertEqual(cortar("Ok.\n\nRef. do chamado: CH-1-abcdef12"), "Ok.")

    def test_corpo_html_vira_texto_sem_script_nem_estilo(self):
        html = "<html><head><style>p{}</style></head><body><script>x()</script><p>Olá</p><p>Mundo<br>linha 2</p></body></html>"
        texto = entrada._html_para_texto(html)
        self.assertNotIn("x()", texto)
        self.assertNotIn("p{}", texto)
        self.assertIn("Olá", texto)
        self.assertIn("linha 2", texto)

    def test_email_so_em_html_e_lido(self):
        m = EmailMessage()
        m["From"], m["To"], m["Subject"], m["Message-ID"] = "a@x.invalid", "b@x.invalid", "s", "<h1@x>"
        m.set_content("<p>Resposta <b>em HTML</b></p><script>bad()</script>", subtype="html")
        lido = entrada.interpretar(m.as_bytes())
        self.assertIn("Resposta em HTML", lido.texto_completo)
        self.assertNotIn("bad()", lido.texto_completo)

    def test_email_sem_message_id_ganha_um_derivado_do_conteudo(self):
        bruto = b"From: a@x.invalid\r\nTo: b@x.invalid\r\nSubject: s\r\n\r\ncorpo"
        a, b = entrada.interpretar(bruto), entrada.interpretar(bruto)
        self.assertTrue(a.message_id.startswith("sha256:"))
        self.assertEqual(a.message_id, b.message_id)  # mesmo e-mail, mesma chave de deduplicação

    def test_imagem_embutida_da_assinatura_nao_e_anexo(self):
        m = EmailMessage()
        m["From"], m["To"], m["Subject"], m["Message-ID"] = "a@x.invalid", "b@x.invalid", "s", "<h2@x>"
        m.set_content("texto")
        m.add_alternative('<p>texto <img src="cid:logo"></p>', subtype="html")
        m.get_payload()[1].add_related(PNG, "image", "png", cid="<logo>", disposition="inline", filename="logo.png")
        lido = entrada.interpretar(m.as_bytes())
        self.assertEqual(lido.anexos, [])
