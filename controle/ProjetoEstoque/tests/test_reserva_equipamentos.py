"""
Reserva de equipamentos para vagas, entrega por movimentação, conclusão da
contratação e e-mails de chamado.

O que cada bloco protege — todos são falhas que passam calado:

  • RESERVA ≠ ENTREGA — reservar só troca o status (Backup → Reservado) e grava a
    reserva. Se mexesse em localidade, centro de custo ou gerasse movimentação,
    o inventário passaria a ter duas verdades sobre o mesmo equipamento.

  • UNICIDADE E REVALIDAÇÃO — o mesmo equipamento em duas vagas, ou um item que
    saiu do Backup entre abrir a tela e confirmar, é o que corrompe o estoque.

  • ENTREGA FECHA A RESERVA — só a movimentação patrimonial de entrega ao
    colaborador da vaga vale; usar o equipamento em outra necessidade exige
    confirmação + justificativa (no serviço, não só no formulário).

  • REPOSIÇÃO — sem substituto em Backup a vaga fica PENDENTE (sem mudar de
    status) e o alerta sai UMA vez por ocorrência.

  • CONCLUSÃO — não existe "concluir mesmo assim": a etapa que não se aplica é
    dispensada com justificativa; a integração é confirmada pelo RH.

  • E-MAIL — cada tipo tem corpo próprio, sem dado sensível, com logo embutida.
"""

import datetime
from unittest import mock

from django.contrib.auth.models import Group, User
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from services import chamado_contratacao_service as chamados_svc
from services import contratacao_service as cs
from services import email_alertas
from services import email_layout
from services import reserva_equipamento_service as rs
from services.movimentacao_service import MovimentacaoEstoqueService

from ProjetoEstoque.forms import MovimentacaoItemForm
from ProjetoEstoque.models import (
    CanalNotificacao,
    Categoria,
    CentroCusto,
    Contratacao,
    ContratacaoChamado,
    ContratacaoEtapaRegistro,
    GRUPO_PORTAL_CONTRATACAO,
    Item,
    ItemStatusHistorico,
    Localidade,
    MovimentacaoItem,
    ReservaEquipamento,
    SimNaoChoices,
    StatusContratacaoChoices,
    StatusItemChoices,
    Subtipo,
    Usuario,
)

Estado = ReservaEquipamento.Estado


def _subtipo(nome):
    categoria, _ = Categoria.objects.get_or_create(nome="Equipamento")
    return Subtipo.objects.get_or_create(
        nome=nome, defaults={"alocado": SimNaoChoices.NAO, "categoria": categoria},
    )[0]


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.ti = User.objects.create_user("ti", password="x", is_staff=True, email="ti@exemplo.com")
        self.ti2 = User.objects.create_user("ti2", password="x", is_staff=True)
        self.rh = User.objects.create_user("rh.maria", password="x")
        self.rh.groups.add(Group.objects.get(name=GRUPO_PORTAL_CONTRATACAO))

        self.cc = CentroCusto.objects.create(numero="100", departamento="Operações")
        self.loc = Localidade.objects.create(local="Unidade Norte")
        self.notebook = _subtipo("Notebook")
        self.monitor = _subtipo("Monitor")

        self.vaga = Contratacao.objects.create(
            cargo="Técnico de Campo", computador="notebook", centro_custo=self.cc,
            localidade=self.loc, criado_por=self.ti, atualizado_por=self.ti,
        )
        self.vaga2 = Contratacao.objects.create(
            cargo="Analista", computador="notebook", centro_custo=self.cc, localidade=self.loc,
        )
        self.nb1 = self._item("NB-001", self.notebook)
        self.nb2 = self._item("NB-002", self.notebook)
        self.mon1 = self._item("MON-001", self.monitor)

        # A Central de Alertas define QUEM recebe: endereços de teste, nunca os do
        # .env — o resultado não pode depender da máquina que roda.
        email_alertas.sincronizar_catalogo_notificacoes()
        for codigo in (
            "vaga_sem_equipamento", "chamado_contratacao_maquina",
            "chamado_contratacao_usuario_email", "chamado_contratacao_sistemas",
        ):
            CanalNotificacao.objects.filter(codigo=codigo).update(
                destinatarios_customizados_ativo=True, destinatarios_customizados="ti@teste.invalid",
            )
        mail.outbox = []

    def _alertas(self):
        """Só os e-mails de "vaga sem equipamento": a entrega, o computador parado e
        outros avisos do sistema também saem pela mesma caixa nos testes."""
        return [m for m in mail.outbox if "Vaga sem equipamento" in m.subject]

    def _item(self, nome, subtipo, status=StatusItemChoices.BACKUP, **extra):
        return Item.objects.create(
            nome=nome, numero_serie=f"SN-{nome}", subtipo=subtipo, status=status,
            centro_custo=self.cc, localidade=self.loc, item_consumo=SimNaoChoices.NAO, **extra,
        )

    def _reservar(self, vaga=None, itens=None, user=None, **kw):
        return rs.reservar(
            contratacao_pk=(vaga or self.vaga).pk,
            item_ids=[i.pk for i in (itens or [self.nb1])], user=user or self.ti, **kw,
        )

    def _colaborador(self, nome="Fulano de Tal"):
        return Usuario.objects.create(
            nome=nome, status="ativo", pmb=SimNaoChoices.NAO,
            centro_custo=self.cc, localidade=self.loc,
        )

    def _form_entrega(self, item, usuario, **extra):
        from django.core.files.uploadedfile import SimpleUploadedFile
        dados = {
            "tipo_movimentacao": "transferencia", "tipo_transferencia": "entrega",
            "item": item.pk, "usuario": usuario.pk, "quantidade": 1,
            "localidade_destino": self.loc.pk, **extra,
        }
        return MovimentacaoItemForm(
            data=dados,
            files={"termo_pdf": SimpleUploadedFile("t.pdf", b"%PDF-1.4 x", content_type="application/pdf")},
        )

    def _entregar(self, item, usuario, **extra):
        form = self._form_entrega(item, usuario, **extra)
        self.assertTrue(form.is_valid(), form.errors.as_text())
        return MovimentacaoEstoqueService.registrar(form=form, user=self.ti)


# ─────────────────────────────────────────────────────────────────────────
# Cenário 1 — reserva de equipamento para vaga aberta
# ─────────────────────────────────────────────────────────────────────────
class ReservaTests(Base):
    def test_reservar_muda_backup_para_reservado_e_grava_a_reserva(self):
        reserva, = self._reservar()
        self.nb1.refresh_from_db()
        self.assertEqual(self.nb1.status, StatusItemChoices.RESERVADO)
        self.assertEqual(reserva.estado, Estado.ATIVA)
        self.assertEqual(reserva.contratacao_id, self.vaga.pk)
        self.assertEqual(reserva.criado_por_id, self.ti.pk)
        self.assertEqual(reserva.status_anterior, StatusItemChoices.BACKUP)
        self.assertIn("Técnico de Campo", reserva.vaga_rotulo)
        self.assertIn("SN-NB-001", reserva.item_rotulo)

    def test_reservar_nao_move_patrimonio_nem_gera_movimentacao(self):
        """Reserva não é entrega: só o status muda."""
        antes = (self.nb1.localidade_id, self.nb1.centro_custo_id)
        self._reservar()
        self.nb1.refresh_from_db()
        self.assertEqual((self.nb1.localidade_id, self.nb1.centro_custo_id), antes)
        self.assertFalse(MovimentacaoItem.objects.filter(item=self.nb1).exists())

    def test_o_historico_de_status_do_item_registra_a_reserva(self):
        self._reservar()
        self.assertTrue(
            ItemStatusHistorico.objects.filter(item=self.nb1, status_novo="reservado").exists()
        )

    def test_equipamento_reservado_nao_pode_ir_para_outra_vaga(self):
        self._reservar()
        with self.assertRaises(rs.ReservaRecusada) as ctx:
            self._reservar(vaga=self.vaga2)
        self.assertIn("já está reservado", str(ctx.exception))

    def test_o_banco_impede_duas_reservas_ativas_do_mesmo_item(self):
        from django.db import IntegrityError, transaction
        self._reservar()
        with self.assertRaises(IntegrityError), transaction.atomic():
            ReservaEquipamento.objects.create(
                contratacao=self.vaga2, item=self.nb1, estado=Estado.ATIVA,
            )

    def test_so_backup_pode_ser_reservado(self):
        ativo = self._item("NB-ATIVO", self.notebook, status=StatusItemChoices.ATIVO)
        manut = self._item("NB-MANUT", self.notebook, status=StatusItemChoices.MANUTENCAO)
        for item in (ativo, manut):
            with self.assertRaises(rs.ReservaRecusada, msg=item.nome) as ctx:
                self._reservar(itens=[item])
            self.assertIn("Só equipamento em Backup", str(ctx.exception))
            item.refresh_from_db()
            self.assertNotEqual(item.status, StatusItemChoices.RESERVADO)

    def test_material_de_consumo_nao_e_reservavel(self):
        consumo = self._item("CABO", self.monitor)
        Item.objects.filter(pk=consumo.pk).update(item_consumo=SimNaoChoices.SIM)
        with self.assertRaises(rs.ReservaRecusada):
            self._reservar(itens=[consumo])

    def test_tudo_ou_nada_um_item_invalido_nao_reserva_os_outros(self):
        ativo = self._item("NB-ATIVO", self.notebook, status=StatusItemChoices.ATIVO)
        with self.assertRaises(rs.ReservaRecusada):
            self._reservar(itens=[self.mon1, ativo])
        self.mon1.refresh_from_db()
        self.assertEqual(self.mon1.status, StatusItemChoices.BACKUP)
        self.assertFalse(ReservaEquipamento.objects.exists())

    def test_segundo_equipamento_do_mesmo_tipo_exige_confirmar_que_precisa_de_mais_de_um(self):
        self._reservar(itens=[self.nb1])
        with self.assertRaises(rs.ReservaRecusada) as ctx:
            self._reservar(itens=[self.nb2])
        self.assertIn("mais de um", str(ctx.exception))
        reserva, = self._reservar(itens=[self.nb2], adicional=True)
        self.assertTrue(reserva.adicional)

    def test_tipos_diferentes_na_mesma_vaga_sao_livres(self):
        self._reservar(itens=[self.nb1, self.mon1])
        self.assertEqual(self.vaga.reservas.filter(estado=Estado.ATIVA).count(), 2)

    def test_so_o_ti_reserva(self):
        with self.assertRaises(PermissionDenied):
            self._reservar(user=self.rh)

    def test_vaga_encerrada_nao_recebe_reserva(self):
        Contratacao.objects.filter(pk=self.vaga.pk).update(status=StatusContratacaoChoices.CANCELADA)
        with self.assertRaises(rs.ReservaRecusada):
            self._reservar()

    def test_seletor_lista_so_backup_sem_reserva(self):
        self._reservar(itens=[self.nb1])
        ids = set(rs.itens_elegiveis(todos_os_tipos=True).values_list("pk", flat=True))
        self.assertNotIn(self.nb1.pk, ids)
        self.assertIn(self.nb2.pk, ids)
        self.assertIn(self.mon1.pk, ids)

    def test_seletor_aceita_outros_tipos_alem_dos_usuais(self):
        cabo = self._item("Switch X", _subtipo("Switches"))
        self.assertNotIn(cabo.pk, set(rs.itens_elegiveis().values_list("pk", flat=True)))
        self.assertIn(cabo.pk, set(rs.itens_elegiveis(todos_os_tipos=True).values_list("pk", flat=True)))

    def test_tela_da_vaga_mostra_o_seletor_so_para_o_ti(self):
        self.client.force_login(self.ti)
        html = self.client.get(reverse("contratacao_detail", args=[self.vaga.pk])).content.decode()
        self.assertIn("Reservar equipamento do Backup", html)
        self.assertIn("NB-001", html)

        self.client.force_login(self.rh)
        html = self.client.get(reverse("contratacao_detail", args=[self.vaga.pk])).content.decode()
        self.assertNotIn("Reservar equipamento do Backup", html)

    def test_rh_nao_alcanca_o_endpoint_de_reserva(self):
        self.client.force_login(self.rh)
        resp = self.client.post(reverse("contratacao_reservar", args=[self.vaga.pk]), {"item": self.nb1.pk})
        self.assertEqual(resp.status_code, 403)
        self.nb1.refresh_from_db()
        self.assertEqual(self.nb1.status, StatusItemChoices.BACKUP)

    def test_reservar_pela_ficha_do_equipamento(self):
        self.client.force_login(self.ti)
        resp = self.client.post(
            reverse("equipamento_reservar", args=[self.nb1.pk]), {"contratacao": self.vaga.pk},
        )
        self.assertEqual(resp.status_code, 302)
        self.nb1.refresh_from_db()
        self.assertEqual(self.nb1.status, StatusItemChoices.RESERVADO)
        html = self.client.get(reverse("equipamento_detalhe", args=[self.nb1.pk])).content.decode()
        self.assertIn("Reservado para", html)

    def test_status_reservado_nao_e_escolhivel_na_edicao_do_equipamento(self):
        from ProjetoEstoque.forms import ItemForm
        valores = [v for v, _ in ItemForm(instance=self.nb2).fields["status"].choices]
        self.assertNotIn("reservado", valores)
        self._reservar()
        valores = [v for v, _ in ItemForm(instance=Item.objects.get(pk=self.nb1.pk)).fields["status"].choices]
        self.assertIn("reservado", valores, "o item JÁ reservado mantém a opção, senão o formulário o trocaria")

    def test_edicao_nao_tira_o_equipamento_reservado_da_reserva_em_silencio(self):
        from ProjetoEstoque.forms import ItemForm
        self._reservar()
        item = Item.objects.get(pk=self.nb1.pk)
        dados = {
            "locado": "nao", "compartilhado": "False", "nome": item.nome, "status": "backup",
            "centro_custo": self.cc.pk, "quantidade": 1, "item_consumo": "nao", "pmb": "nao",
            "precisa_preventiva": "nao",
        }
        form = ItemForm(data=dados, instance=item)
        self.assertFalse(form.is_valid())
        self.assertIn("status", form.errors)


# ─────────────────────────────────────────────────────────────────────────
# Cenário 4 — cancelamento / exclusão da vaga
# ─────────────────────────────────────────────────────────────────────────
class LiberacaoTests(Base):
    def test_cancelar_a_vaga_devolve_o_equipamento_ao_backup_e_guarda_o_historico(self):
        reserva, = self._reservar()
        cs.cancelar(contratacao=self.vaga, motivo="Vaga congelada", user=self.ti)
        self.nb1.refresh_from_db()
        reserva.refresh_from_db()
        self.assertEqual(self.nb1.status, StatusItemChoices.BACKUP)
        self.assertEqual(reserva.estado, Estado.LIBERADA)
        self.assertEqual(reserva.motivo_tipo, ReservaEquipamento.Motivo.CANCELAMENTO)
        self.assertIsNotNone(reserva.encerrada_em)
        self.assertEqual(reserva.encerrada_por_id, self.ti.pk)
        self.assertIn("congelada", reserva.motivo)

    def test_excluir_a_vaga_libera_e_preserva_a_reserva_no_historico(self):
        reserva, = self._reservar()
        self.client.force_login(self.ti)
        from django.contrib.auth.models import Permission
        self.ti.user_permissions.add(*Permission.objects.filter(codename="delete_contratacao"))
        resp = self.client.post(reverse("contratacao_delete", args=[self.vaga.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Contratacao.objects.filter(pk=self.vaga.pk).exists())
        self.nb1.refresh_from_db()
        reserva.refresh_from_db()
        self.assertEqual(self.nb1.status, StatusItemChoices.BACKUP)
        self.assertEqual(reserva.estado, Estado.LIBERADA)
        self.assertIsNone(reserva.contratacao_id)
        self.assertIn("Técnico de Campo", reserva.vaga_rotulo, "o rótulo diz de qual vaga era")

    def test_cancelar_nao_libera_o_que_ja_foi_entregue(self):
        colab = self._colaborador()
        cs.definir_contratado(contratacao=self.vaga, colaborador=colab, nome="", user=self.ti)
        reserva, = self._reservar()
        self._entregar(self.nb1, colab)
        reserva.refresh_from_db()
        self.assertEqual(reserva.estado, Estado.ENTREGUE)
        cs.cancelar(contratacao=Contratacao.objects.get(pk=self.vaga.pk), motivo="x", user=self.ti)
        reserva.refresh_from_db()
        self.nb1.refresh_from_db()
        self.assertEqual(reserva.estado, Estado.ENTREGUE)
        self.assertEqual(self.nb1.status, StatusItemChoices.ATIVO)

    def test_liberar_manualmente_exige_motivo(self):
        reserva, = self._reservar()
        with self.assertRaises(rs.ReservaRecusada):
            rs.liberar(reserva_pk=reserva.pk, motivo=" ", user=self.ti)
        rs.liberar(reserva_pk=reserva.pk, motivo="Vaga não precisa de notebook", user=self.ti)
        self.nb1.refresh_from_db()
        self.assertEqual(self.nb1.status, StatusItemChoices.BACKUP)

    def test_liberar_so_vale_para_reserva_ativa(self):
        reserva, = self._reservar()
        rs.liberar(reserva_pk=reserva.pk, motivo="teste", user=self.ti)
        with self.assertRaises(rs.ReservaRecusada):
            rs.liberar(reserva_pk=reserva.pk, motivo="de novo", user=self.ti)

    def test_marcar_a_vaga_sem_equipamento_libera_as_reservas(self):
        colab = self._colaborador()
        cs.definir_contratado(contratacao=self.vaga, colaborador=colab, nome="", user=self.ti)
        self._reservar()
        cs.registrar_entrega(
            contratacao=Contratacao.objects.get(pk=self.vaga.pk), item=None,
            sem_equipamento=True, user=self.ti,
        )
        self.nb1.refresh_from_db()
        self.assertEqual(self.nb1.status, StatusItemChoices.BACKUP)


# ─────────────────────────────────────────────────────────────────────────
# Cenários 2 e 3 — colaborador cadastrado, reserva associada, entrega efetiva
# ─────────────────────────────────────────────────────────────────────────
class CadastroEEntregaTests(Base):
    def _preencher(self, **extra):
        dados = {
            "colaborador": None, "colaborador_nome": "Beltrano da Silva",
            "colaborador_email": "beltrano@exemplo.com", "colaborador_telefone": "",
            "data_admissao": datetime.date(2026, 12, 1), "colaborador_observacoes": "",
            "cadastrar_colaborador": True, "matricula": "", "area_administrativa": False,
        }
        dados.update(extra)
        return cs.preencher_vaga(contratacao_pk=self.vaga.pk, dados=dados, user=self.ti)

    def test_preencher_e_cadastrar_usa_os_dados_da_vaga(self):
        vaga = self._preencher()
        colab = vaga.colaborador
        self.assertIsNotNone(colab)
        self.assertEqual(colab.nome, "Beltrano da Silva")
        self.assertEqual(colab.email, "beltrano@exemplo.com")
        self.assertEqual(colab.centro_custo_id, self.cc.pk)
        self.assertEqual(colab.localidade_id, self.loc.pk)
        self.assertEqual(colab.data_inicio, datetime.date(2026, 12, 1))
        self.assertEqual(colab.status, "ativo")
        self.assertIsNone(colab.matricula, "sem matrícula é NULL, não '' (a coluna é única)")
        self.assertEqual(vaga.status, StatusContratacaoChoices.VAGA_PREENCHIDA)

    def test_cadastro_recusa_homonimo_e_desfaz_o_preenchimento(self):
        self._colaborador("Beltrano da Silva")
        with self.assertRaises(cs.TransicaoRecusada) as ctx:
            self._preencher()
        self.assertIn("Já existe um colaborador ativo", str(ctx.exception))
        self.vaga.refresh_from_db()
        self.assertEqual(self.vaga.status, StatusContratacaoChoices.ABERTA, "tudo ou nada")
        self.assertEqual(Usuario.objects.filter(nome="Beltrano da Silva").count(), 1)

    def test_cadastro_recusa_matricula_e_email_em_uso(self):
        Usuario.objects.create(nome="Outro", status="ativo", pmb="nao", matricula="123", email="x@exemplo.com")
        with self.assertRaises(cs.TransicaoRecusada):
            self._preencher(matricula="123")
        with self.assertRaises(cs.TransicaoRecusada):
            self._preencher(colaborador_email="X@exemplo.com")

    def test_formulario_nao_aceita_escolher_cadastrado_e_cadastrar_novo_juntos(self):
        from ProjetoEstoque.forms import ContratacaoPreenchimentoForm
        existente = self._colaborador("Existente")
        form = ContratacaoPreenchimentoForm(data={
            "colaborador": existente.pk, "cadastrar_colaborador": "on",
        })
        self.assertFalse(form.is_valid())

    def test_cadastrar_depois_de_preencher_so_com_o_nome(self):
        self._preencher(cadastrar_colaborador=False)
        self.vaga.refresh_from_db()
        self.assertIsNone(self.vaga.colaborador_id)
        cs.cadastrar_colaborador(
            contratacao_pk=self.vaga.pk, dados={"matricula": "777", "area_administrativa": True}, user=self.ti,
        )
        self.vaga.refresh_from_db()
        self.assertEqual(self.vaga.colaborador.matricula, "777")
        self.assertTrue(self.vaga.colaborador.area_administrativa)
        with self.assertRaises(cs.TransicaoRecusada):
            cs.cadastrar_colaborador(contratacao_pk=self.vaga.pk, dados={}, user=self.ti)

    def test_o_rh_cadastra_pelo_portal(self):
        self.client.force_login(self.rh)
        resp = self.client.post(reverse("contratacao_preencher", args=[self.vaga.pk]), {
            "preencher-colaborador_nome": "Maria Nova", "preencher-cadastrar_colaborador": "on",
            "preencher-matricula": "",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Usuario.objects.filter(nome="Maria Nova").exists())

    def test_reserva_existente_passa_a_ser_do_colaborador_cadastrado(self):
        reserva, = self._reservar()
        self.assertIsNone(reserva.colaborador_id)
        vaga = self._preencher()
        reserva.refresh_from_db()
        self.assertEqual(reserva.colaborador_id, vaga.colaborador_id)
        self.assertEqual(reserva.contratacao_id, self.vaga.pk, "a reserva continua da vaga")

    def test_reabrir_a_vaga_desassocia_o_colaborador_mas_mantem_a_reserva(self):
        reserva, = self._reservar()
        self._preencher()
        cs.reabrir_vaga(contratacao_pk=self.vaga.pk, motivo="candidato desistiu", user=self.ti)
        reserva.refresh_from_db()
        self.nb1.refresh_from_db()
        self.assertIsNone(reserva.colaborador_id)
        self.assertEqual(reserva.estado, Estado.ATIVA)
        self.assertEqual(self.nb1.status, StatusItemChoices.RESERVADO)

    def test_entrega_ao_colaborador_da_vaga_fecha_a_reserva_sem_confirmacao(self):
        reserva, = self._reservar()
        vaga = self._preencher()
        mov = self._entregar(self.nb1, vaga.colaborador)
        reserva.refresh_from_db()
        self.nb1.refresh_from_db()
        vaga.refresh_from_db()
        self.assertEqual(reserva.estado, Estado.ENTREGUE)
        self.assertEqual(reserva.movimentacao_id, mov.pk)
        self.assertEqual(reserva.encerrada_por_id, self.ti.pk)
        self.assertEqual(self.nb1.status, StatusItemChoices.ATIVO)
        self.assertEqual(mov.usuario_id, vaga.colaborador_id)
        self.assertEqual(vaga.item_entregue_id, self.nb1.pk)
        self.assertEqual(vaga.status, StatusContratacaoChoices.EQUIPAMENTO_ENTREGUE)
        self.assertIsNotNone(vaga.equipamento_entregue_em)

    def test_a_entrega_preserva_o_historico_anterior_de_movimentacoes(self):
        MovimentacaoItem.objects.create(
            tipo_movimentacao="transferencia", tipo_transferencia="devolucao", item=self.nb1,
        )
        self._reservar()
        vaga = self._preencher()
        self._entregar(self.nb1, vaga.colaborador)
        self.assertEqual(MovimentacaoItem.objects.filter(item=self.nb1).count(), 2)

    def test_a_entrega_so_encerra_a_vaga_quando_nao_resta_reserva_ativa(self):
        self._reservar(itens=[self.nb1, self.mon1])
        vaga = self._preencher()
        self._entregar(self.nb1, vaga.colaborador)
        vaga.refresh_from_db()
        self.assertEqual(vaga.status, StatusContratacaoChoices.VAGA_PREENCHIDA, "falta o monitor")
        self._entregar(self.mon1, vaga.colaborador)
        vaga.refresh_from_db()
        self.assertEqual(vaga.status, StatusContratacaoChoices.EQUIPAMENTO_ENTREGUE)

    def test_registrar_entrega_manual_exige_movimentacao_de_entrega(self):
        vaga = self._preencher()
        with self.assertRaises(cs.TransicaoRecusada) as ctx:
            cs.registrar_entrega(contratacao=vaga, item=self.nb2, sem_equipamento=False, user=self.ti)
        self.assertIn("movimentação de entrega", str(ctx.exception))
        self._entregar(self.nb2, vaga.colaborador)
        cs.registrar_entrega(contratacao=vaga, item=self.nb2, sem_equipamento=False, user=self.ti)
        vaga.refresh_from_db()
        self.assertEqual(vaga.item_entregue_id, self.nb2.pk)

    def test_registrar_entrega_sem_colaborador_cadastrado_e_recusado(self):
        with self.assertRaises(cs.TransicaoRecusada):
            cs.registrar_entrega(contratacao=self.vaga, item=self.nb1, sem_equipamento=False, user=self.ti)

    def test_entrega_por_termo_assinado_tambem_fecha_a_reserva(self):
        """O termo digital passa pelo MESMO serviço: não pode ter caminho que ignore a reserva."""
        from services import termo_assinatura_service as tas
        reserva, = self._reservar()
        vaga = self._preencher()
        self.assertEqual(
            tas.validar_pre_condicoes(tipo="entrega", colaborador=vaga.colaborador, itens=[Item.objects.get(pk=self.nb1.pk)]),
            [],
        )
        self._entregar(self.nb1, vaga.colaborador)
        reserva.refresh_from_db()
        self.assertEqual(reserva.estado, Estado.ENTREGUE)

    def test_termo_recusa_equipamento_reservado_para_outro_colaborador(self):
        from services import termo_assinatura_service as tas
        self._reservar()
        self._preencher()
        outro = self._colaborador("Outro Colaborador")
        problemas = tas.validar_pre_condicoes(
            tipo="entrega", colaborador=outro, itens=[Item.objects.get(pk=self.nb1.pk)],
        )
        self.assertTrue(any("reservado para" in p for p in problemas))


# ─────────────────────────────────────────────────────────────────────────
# Cenário 5 — usar equipamento reservado em reposição
# ─────────────────────────────────────────────────────────────────────────
class ReposicaoTests(Base):
    def setUp(self):
        super().setUp()
        self.reserva, = self._reservar()
        self.vaga_preenchida = cs.preencher_vaga(
            contratacao_pk=self.vaga.pk, user=self.ti,
            dados={"colaborador_nome": "Beltrano", "cadastrar_colaborador": True},
        )
        self.defeituoso = self._colaborador("Usuário com defeito")

    def test_o_formulario_avisa_e_exige_confirmacao_e_justificativa(self):
        form = self._form_entrega(self.nb1, self.defeituoso)
        self.assertFalse(form.is_valid())
        self.assertIn("RESERVADO", form.non_field_errors()[0])
        self.assertIsNotNone(form.reserva_em_conflito)

        form = self._form_entrega(self.nb1, self.defeituoso, confirmar_uso_reservado="on")
        self.assertFalse(form.is_valid())
        self.assertIn("justificativa_uso_reservado", form.errors)

    def test_o_servico_tambem_exige_nao_so_o_formulario(self):
        """O service impõe a regra mesmo que o formulário nunca tenha sido consultado."""
        mov = MovimentacaoItem(
            tipo_movimentacao="transferencia", tipo_transferencia="entrega",
            item=self.nb1, usuario=self.defeituoso,
        )
        with self.assertRaises(ValidationError):
            rs.tratar_movimentacao(
                item=Item.objects.get(pk=self.nb1.pk), mov=mov, confirmou=False, justificativa="", user=self.ti,
            )
        with self.assertRaises(ValidationError):
            rs.tratar_movimentacao(
                item=Item.objects.get(pk=self.nb1.pk), mov=mov, confirmou=True, justificativa="abc", user=self.ti,
            )
        self.reserva.refresh_from_db()
        self.assertEqual(self.reserva.estado, Estado.ATIVA)

    def test_uso_confirmado_cede_a_reserva_e_registra_tudo(self):
        self._reservar(vaga=self.vaga2, itens=[self.nb2])  # outro notebook já reservado: sem substituto em Backup
        mov = self._entregar(
            self.nb1, self.defeituoso,
            confirmar_uso_reservado="on", justificativa_uso_reservado="Notebook do financeiro queimou",
        )
        self.reserva.refresh_from_db()
        self.nb1.refresh_from_db()
        self.assertEqual(self.reserva.estado, Estado.CEDIDA)
        self.assertEqual(self.reserva.movimentacao_id, mov.pk)
        self.assertEqual(self.reserva.motivo_tipo, ReservaEquipamento.Motivo.REPOSICAO)
        self.assertIn("financeiro queimou", self.reserva.motivo)
        self.assertEqual(self.reserva.encerrada_por_id, self.ti.pk)
        self.assertEqual(self.nb1.status, StatusItemChoices.ATIVO)
        self.assertEqual(mov.reserva_resultado.tipo, "cessao")

    def test_a_vaga_nao_muda_de_status_por_ficar_sem_equipamento(self):
        antes = Contratacao.objects.get(pk=self.vaga.pk).status
        with self.captureOnCommitCallbacks(execute=True):
            self._entregar(
                self.nb1, self.defeituoso,
                confirmar_uso_reservado="on", justificativa_uso_reservado="reposição urgente",
            )
        self.assertEqual(Contratacao.objects.get(pk=self.vaga.pk).status, antes)
        self.assertTrue(rs.pendencias_de_equipamento(Contratacao.objects.get(pk=self.vaga.pk))["pendente"])

    def test_com_substituto_em_backup_nao_alerta_e_o_ti_repoe(self):
        with self.captureOnCommitCallbacks(execute=True):
            mov = self._entregar(
                self.nb1, self.defeituoso,
                confirmar_uso_reservado="on", justificativa_uso_reservado="reposição urgente",
            )
        self.assertEqual(mov.reserva_resultado.candidatos, 1)
        self.assertFalse(mov.reserva_resultado.alerta_disparado)
        self.assertEqual(self._alertas(), [])

        nova, = self._reservar(itens=[self.nb2], substitui_pk=self.reserva.pk)
        self.reserva.refresh_from_db()
        self.assertEqual(self.reserva.substituida_por_id, nova.pk)
        vaga = Contratacao.objects.get(pk=self.vaga.pk)
        self.assertFalse(rs.pendencias_de_equipamento(vaga)["pendente"])
        self.assertEqual(nova.colaborador_id, vaga.colaborador_id)

    def test_sem_substituto_dispara_o_alerta_uma_unica_vez(self):
        self._reservar(vaga=self.vaga2, itens=[self.nb2])
        with self.captureOnCommitCallbacks(execute=True):
            mov = self._entregar(
                self.nb1, self.defeituoso,
                confirmar_uso_reservado="on", justificativa_uso_reservado="reposição urgente",
            )
        self.assertTrue(mov.reserva_resultado.alerta_disparado)
        self.assertEqual(len(self._alertas()), 1)
        msg = self._alertas()[0]
        self.assertIn("Vaga sem equipamento reservado", msg.subject)
        self.assertIn("Técnico de Campo", msg.body)
        self.assertIn("NB-001", msg.body)
        self.assertIn("nova reserva", msg.body)
        self.reserva.refresh_from_db()
        self.assertIsNotNone(self.reserva.alerta_em)

        # Mesma ocorrência: nada de e-mail repetido.
        self.assertFalse(rs._agendar_alerta(self.reserva))
        with self.assertRaises(rs.ReservaRecusada):
            rs.reenviar_alerta(reserva_pk=self.reserva.pk, user=self.ti)
        self.assertEqual(len(self._alertas()), 1)

    def test_falha_no_envio_libera_a_ocorrencia_para_nova_tentativa(self):
        self._reservar(vaga=self.vaga2, itens=[self.nb2])
        with mock.patch.object(
            email_alertas, "alerta_vaga_sem_equipamento",
            return_value=email_alertas.ResultadoEnvio(False, "SMTP fora do ar"),
        ):
            with self.captureOnCommitCallbacks(execute=True):
                self._entregar(
                    self.nb1, self.defeituoso,
                    confirmar_uso_reservado="on", justificativa_uso_reservado="reposição urgente",
                )
        self.reserva.refresh_from_db()
        self.assertIsNone(self.reserva.alerta_em)
        self.assertIn("SMTP fora do ar", self.reserva.alerta_resultado)
        with self.captureOnCommitCallbacks(execute=True):
            rs.reenviar_alerta(reserva_pk=self.reserva.pk, user=self.ti)
        self.assertEqual(len(self._alertas()), 1)

    def test_manutencao_do_equipamento_reservado_tambem_pede_confirmacao(self):
        from ProjetoEstoque.models import Fornecedor
        forn = Fornecedor.objects.create(nome="Assistência")
        form = MovimentacaoItemForm(data={
            "tipo_movimentacao": "envio_manutencao", "item": self.nb1.pk,
            "fornecedor_manutencao": forn.pk, "observacao": "não liga",
        })
        self.assertFalse(form.is_valid())
        self.assertIn("RESERVADO", form.non_field_errors()[0])

    def test_status_alterado_fora_do_fluxo_cede_a_reserva_pela_rede_de_seguranca(self):
        self._reservar(vaga=self.vaga2, itens=[self.nb2])
        item = Item.objects.get(pk=self.nb1.pk)
        with self.captureOnCommitCallbacks(execute=True):
            item.status = StatusItemChoices.DEFEITO
            item.save()
        self.reserva.refresh_from_db()
        self.assertEqual(self.reserva.estado, Estado.CEDIDA)
        self.assertEqual(self.reserva.motivo_tipo, ReservaEquipamento.Motivo.STATUS_ALTERADO)
        self.assertEqual(len(self._alertas()), 1)

    def test_a_api_do_formulario_diz_quando_o_item_esta_reservado(self):
        self.client.force_login(self.ti)
        r = self.client.get(reverse("api_item_reserva"), {"item": self.nb1.pk}).json()
        self.assertTrue(r["reservado"])
        self.assertIn("Técnico de Campo", r["vaga"])
        r = self.client.get(reverse("api_item_reserva"), {"item": self.mon1.pk}).json()
        self.assertFalse(r["reservado"])

    def test_tela_de_movimentacao_leva_o_ti_a_vaga_depois_do_uso(self):
        self.client.force_login(self.ti)
        from django.core.files.uploadedfile import SimpleUploadedFile
        resp = self.client.post(reverse("movimentacao_create"), {
            "tipo_movimentacao": "transferencia", "tipo_transferencia": "entrega",
            "item": self.nb1.pk, "usuario": self.defeituoso.pk, "quantidade": 1,
            "localidade_destino": self.loc.pk,
            "confirmar_uso_reservado": "on", "justificativa_uso_reservado": "reposição urgente",
            "termo_pdf": SimpleUploadedFile("t.pdf", b"%PDF-1.4 x", content_type="application/pdf"),
        })
        self.assertEqual(resp.status_code, 302)
        self.assertIn(f"/contratacoes/{self.vaga.pk}/", resp.url)
        self.assertIn("reposicao=", resp.url)


# ─────────────────────────────────────────────────────────────────────────
# Cenário 6 — conclusão da contratação
# ─────────────────────────────────────────────────────────────────────────
class ConclusaoTests(Base):
    def setUp(self):
        super().setUp()
        # Vaga enxuta: sem sistema, sem e-mail/usuário — só computador.
        Contratacao.objects.filter(pk=self.vaga.pk).update(usuario_rede=False, email_corporativo=False)
        self.vaga.refresh_from_db()

    def _etapas(self):
        return {e.chave: e for e in cs.etapas_do_processo(
            cs.queryset_base().prefetch_related(*cs.PREFETCH_DO_PROCESSO).get(pk=self.vaga.pk)
        )}

    def _preparar_ate_a_entrega(self):
        self._reservar()
        vaga = cs.preencher_vaga(
            contratacao_pk=self.vaga.pk, user=self.ti,
            dados={"colaborador_nome": "Beltrano", "cadastrar_colaborador": True},
        )
        self._entregar(self.nb1, vaga.colaborador)
        return Contratacao.objects.get(pk=self.vaga.pk)

    def _concluir_chamado_da_maquina(self):
        ch = ContratacaoChamado.objects.create(
            contratacao=self.vaga, tipo="maquina", status="enviado", dados={}, atendimento="concluido",
        )
        return ch

    def test_nao_conclui_com_etapas_obrigatorias_pendentes(self):
        vaga = self._preparar_ate_a_entrega()
        ok, faltas = cs.concluir(contratacao=vaga, user=self.rh)
        self.assertFalse(ok)
        self.assertTrue(any("Integração" in f for f in faltas))
        self.assertTrue(any("Chamado de máquina" in f for f in faltas))
        vaga.refresh_from_db()
        self.assertNotEqual(vaga.status, StatusContratacaoChoices.CONCLUIDA)

    def test_nao_existe_concluir_mesmo_assim(self):
        import inspect
        self.assertNotIn("forcar", inspect.signature(cs.concluir).parameters)
        self.client.force_login(self.ti)
        self._preparar_ate_a_entrega()
        self.client.post(reverse("contratacao_etapa", args=[self.vaga.pk]), {"acao": "concluir", "forcar": "1"})
        self.vaga.refresh_from_db()
        self.assertNotEqual(self.vaga.status, StatusContratacaoChoices.CONCLUIDA)

    def test_fluxo_completo_o_rh_confirma_a_integracao_e_conclui(self):
        vaga = self._preparar_ate_a_entrega()
        self._concluir_chamado_da_maquina()
        # Sem usuário/e-mail e sem sistema: essas etapas não se aplicam por configuração;
        # as orientações também (nada a orientar).
        etapas = self._etapas()
        for chave in (cs.USUARIO_EMAIL, cs.SISTEMAS, cs.ORIENTACOES):
            self.assertTrue(etapas[chave].dispensada, chave)
            self.assertEqual(etapas[chave].origem, "configuracao", chave)

        cs.confirmar_integracao(contratacao_pk=vaga.pk, observacao="ok", user=self.rh)
        vaga.refresh_from_db()
        self.assertEqual(vaga.integracao_confirmada_por_id, self.rh.pk)
        self.assertIsNotNone(vaga.integracao_confirmada_em)

        ok, faltas = cs.concluir(contratacao=vaga, user=self.rh)
        self.assertEqual(faltas, [])
        self.assertTrue(ok)
        vaga.refresh_from_db()
        self.assertEqual(vaga.status, StatusContratacaoChoices.CONCLUIDA)
        self.assertIsNotNone(vaga.concluida_em)

    def test_a_vaga_pode_estar_preenchida_com_a_integracao_em_andamento(self):
        self._reservar()
        cs.preencher_vaga(
            contratacao_pk=self.vaga.pk, user=self.ti,
            dados={"colaborador_nome": "Beltrano", "cadastrar_colaborador": True},
        )
        vaga = Contratacao.objects.get(pk=self.vaga.pk)
        self.assertEqual(vaga.status, StatusContratacaoChoices.VAGA_PREENCHIDA)
        self.assertEqual(cs.situacao_integracao(vaga)["estado"], "em_andamento")

    def test_integracao_so_se_confirma_com_a_vaga_preenchida_e_so_uma_vez(self):
        with self.assertRaises(cs.TransicaoRecusada):
            cs.confirmar_integracao(contratacao_pk=self.vaga.pk, user=self.rh)
        vaga = self._preparar_ate_a_entrega()
        cs.confirmar_integracao(contratacao_pk=vaga.pk, user=self.rh)
        with self.assertRaises(cs.TransicaoRecusada):
            cs.confirmar_integracao(contratacao_pk=vaga.pk, user=self.rh)

    def test_desfazer_a_integracao_exige_motivo(self):
        vaga = self._preparar_ate_a_entrega()
        cs.confirmar_integracao(contratacao_pk=vaga.pk, user=self.rh)
        with self.assertRaises(cs.TransicaoRecusada):
            cs.desfazer_integracao(contratacao_pk=vaga.pk, motivo="", user=self.rh)
        cs.desfazer_integracao(contratacao_pk=vaga.pk, motivo="confirmei sem querer", user=self.rh)
        vaga.refresh_from_db()
        self.assertIsNone(vaga.integracao_confirmada_em)

    def test_a_entrega_so_conta_com_movimentacao_nao_com_reserva(self):
        self._reservar()
        cs.preencher_vaga(
            contratacao_pk=self.vaga.pk, user=self.ti,
            dados={"colaborador_nome": "Beltrano", "cadastrar_colaborador": True},
        )
        etapas = self._etapas()
        self.assertTrue(etapas[cs.RESERVA].concluida)
        self.assertTrue(etapas[cs.ENTREGA].pendente)
        self.assertIn("aguardando a movimentação de entrega", etapas[cs.ENTREGA].detalhe)

    def test_chamado_concluido_nao_encerra_os_demais_nem_muda_a_vaga(self):
        vaga = self._preparar_ate_a_entrega()
        antes = vaga.status
        self._concluir_chamado_da_maquina()
        ContratacaoChamado.objects.create(
            contratacao=vaga, tipo="usuario_email", status="enviado", dados={}, atendimento="em_andamento",
        )
        vaga.refresh_from_db()
        self.assertEqual(vaga.status, antes)
        self.assertEqual(
            ContratacaoChamado.objects.get(contratacao=vaga, tipo="usuario_email").atendimento, "em_andamento",
        )

    def test_dispensar_exige_justificativa_e_deixa_rastro(self):
        vaga = self._preparar_ate_a_entrega()
        with self.assertRaises(cs.TransicaoRecusada):
            cs.dispensar_etapa(contratacao_pk=vaga.pk, chave=cs.PREPARACAO, justificativa="", user=self.ti)
        cs.dispensar_etapa(
            contratacao_pk=vaga.pk, chave=cs.PREPARACAO,
            justificativa="Máquina já veio configurada do fornecedor", user=self.ti,
        )
        etapa = self._etapas()[cs.PREPARACAO]
        self.assertTrue(etapa.dispensada)
        self.assertEqual(etapa.origem, "dispensa")
        self.assertIn("configurada", etapa.detalhe)
        self.assertTrue(vaga.eventos.filter(descricao__icontains="Etapa dispensada").exists())

    def test_so_o_ti_dispensa_e_integracao_nao_se_dispensa(self):
        vaga = self._preparar_ate_a_entrega()
        with self.assertRaises(PermissionDenied):
            cs.dispensar_etapa(contratacao_pk=vaga.pk, chave=cs.PREPARACAO, justificativa="x" * 10, user=self.rh)
        with self.assertRaises(cs.TransicaoRecusada):
            cs.dispensar_etapa(contratacao_pk=vaga.pk, chave=cs.INTEGRACAO, justificativa="x" * 10, user=self.ti)

    def test_desfazer_a_dispensa_mantem_o_registro_como_historico(self):
        vaga = self._preparar_ate_a_entrega()
        cs.dispensar_etapa(contratacao_pk=vaga.pk, chave=cs.PREPARACAO, justificativa="não se aplica", user=self.ti)
        cs.desfazer_registro_de_etapa(contratacao_pk=vaga.pk, chave=cs.PREPARACAO, motivo="errei", user=self.ti)
        self.assertTrue(self._etapas()[cs.PREPARACAO].pendente)
        registro = ContratacaoEtapaRegistro.objects.get(contratacao=vaga, chave=cs.PREPARACAO)
        self.assertIsNotNone(registro.desfeita_em)

    def test_orientacoes_podem_ser_registradas_pelo_ti(self):
        Contratacao.objects.filter(pk=self.vaga.pk).update(usuario_rede=False)
        from ProjetoEstoque.models import SistemaCorporativo, ContratacaoSistema
        sis, _ = SistemaCorporativo.objects.get_or_create(nome="Tekann", defaults={"ordem": 5})
        ContratacaoSistema.objects.create(
            contratacao=self.vaga, sistema=sis, necessario=True, provisionado=True,
        )
        etapas = self._etapas()
        self.assertTrue(etapas[cs.ORIENTACOES].pendente)
        cs.registrar_etapa(contratacao_pk=self.vaga.pk, chave=cs.ORIENTACOES, observacao="Treinamento feito", user=self.ti)
        etapa = self._etapas()[cs.ORIENTACOES]
        self.assertTrue(etapa.concluida)
        self.assertEqual(etapa.origem, "manual")

    def test_a_tela_mostra_as_etapas_e_so_o_rh_ou_ti_conclui(self):
        self._preparar_ate_a_entrega()
        for usuario in (self.ti, self.rh):
            self.client.force_login(usuario)
            html = self.client.get(reverse("contratacao_detail", args=[self.vaga.pk])).content.decode()
            self.assertIn("Etapas para concluir", html)
            self.assertIn("Confirmar integração", html)
            self.assertIn("Concluir contratação", html)
            self.assertIn("Integração em andamento", html)

    def test_rh_nao_dispensa_etapa_pelo_endpoint(self):
        self._preparar_ate_a_entrega()
        self.client.force_login(self.rh)
        resp = self.client.post(reverse("contratacao_etapa_processo", args=[self.vaga.pk]), {
            "acao": "dispensar", "chave": cs.PREPARACAO, "motivo": "quero fechar logo",
        })
        self.assertEqual(resp.status_code, 403)

    def test_painel_marca_vaga_sem_equipamento_reservado(self):
        self.client.force_login(self.ti)
        html = self.client.get(reverse("contratacao_painel")).content.decode()
        self.assertIn("Sem equipamento reservado", html)
        self._reservar()
        html = self.client.get(reverse("contratacao_painel")).content.decode()
        # `vaga2` ainda não tem reserva
        self.assertIn("Sem equipamento reservado", html)
        self.assertEqual(rs.vagas_sem_equipamento([self.vaga, self.vaga2]), {self.vaga2.pk: "sem_reserva"})


# ─────────────────────────────────────────────────────────────────────────
# Inventário: Reservado não pode quebrar as regras que já existiam
# ─────────────────────────────────────────────────────────────────────────
class InventarioTests(Base):
    def test_locacao_segue_sendo_cobrada_enquanto_reservado(self):
        from services.locacao_service import ATIVOS
        self.assertIn("reservado", ATIVOS)

    def test_reservado_conta_como_computador_parado(self):
        self.assertTrue(email_alertas.eh_computador_parado(Item.objects.get(pk=self.nb1.pk)) )
        self._reservar()
        item = Item.objects.select_related("subtipo").get(pk=self.nb1.pk)
        self.assertTrue(email_alertas.eh_computador_parado(item))

    def test_backup_para_reservado_nao_dispara_aviso_de_computador_parado(self):
        """As duas pontas são 'parado': a decisão sobre a licença já foi tomada."""
        with self.captureOnCommitCallbacks(execute=True):
            self._reservar()
        assuntos = [m.subject for m in mail.outbox]
        self.assertFalse(any("computador" in a.lower() and "parado" in a.lower() for a in assuntos), assuntos)

    def test_status_transferencia_nao_oferece_reservado(self):
        form = MovimentacaoItemForm()
        valores = [v for v, _ in form.fields["status_transferencia"].choices]
        self.assertNotIn("reservado", valores)

    def test_equipamento_reservado_aparece_entregavel_no_termo_digital(self):
        from ProjetoEstoque.views.termos_assinatura import _itens_entregaveis
        self._reservar()
        self.assertIn(self.nb1.pk, set(_itens_entregaveis().values_list("pk", flat=True)))

    def test_lista_de_equipamentos_filtra_por_reservado(self):
        self._reservar()
        self.client.force_login(self.ti)
        html = self.client.get(reverse("equipamentos_list"), {"status": "reservado"}).content.decode()
        self.assertIn("NB-001", html)
        self.assertNotIn("NB-002", html)


# ─────────────────────────────────────────────────────────────────────────
# Cenário 7 — e-mails de abertura de chamado
# ─────────────────────────────────────────────────────────────────────────
class EmailTests(Base):
    DADOS = {
        "vaga_id": 7, "cargo": "Técnico de Campo", "funcao": "Técnico", "departamento": "Operações (CC 100)",
        "local": "Unidade Norte", "previsao_inicio": "20/11/2026", "tipo_vaga": "Nova vaga",
        "solicitante": "Maria", "solicitante_email": "maria@exemplo.com",
        "computador": "Notebook", "telefonia": "", "descricao": "16 GB",
        "criar_usuario_rede": True, "criar_email": True, "email_sugerido": "fulano",
        "acessos": ["TOTVS"], "sistemas": ["TOTVS"], "licencas": ["Microsoft 365"], "suporte": ["Liberação de acesso"],
    }

    def _montar(self, tipo, **kw):
        agora = timezone.now()
        return email_alertas.montar_chamado_contratacao(
            tipo, self.DADOS, solicitado_em=agora, enviado_em=agora,
            referencia="CH-7-abcd1234", protocolo="CH-00007", colaborador="Fulano Silva", **kw,
        )

    def test_assunto_segue_o_padrao_de_cada_tipo_com_o_nome(self):
        self.assertEqual(self._montar("maquina")[0], "Nova solicitação de preparação de equipamento | Fulano Silva")
        self.assertEqual(self._montar("usuario_email")[0], "Solicitação de criação de acessos corporativos | Fulano Silva")
        self.assertEqual(self._montar("sistemas")[0], "Solicitação de acessos e integração aos sistemas | Fulano Silva")

    def test_sem_colaborador_o_assunto_usa_a_vaga(self):
        agora = timezone.now()
        assunto, _, _ = email_alertas.montar_chamado_contratacao(
            "maquina", self.DADOS, solicitado_em=agora, enviado_em=agora,
        )
        self.assertIn("Vaga nº 7", assunto)

    def test_cada_tipo_tem_corpo_proprio(self):
        corpos = {t: self._montar(t)[2] for t in ("maquina", "usuario_email", "sistemas")}
        self.assertIn("Equipamentos", corpos["maquina"])
        self.assertIn("Acessos corporativos solicitados", corpos["usuario_email"])
        self.assertIn("Sistemas e acessos", corpos["sistemas"])
        self.assertNotIn("Acessos corporativos solicitados", corpos["maquina"])
        self.assertNotIn("Equipamentos reservados", corpos["sistemas"])

    def test_identificacao_do_chamado_em_todos_os_tipos(self):
        for tipo in ("maquina", "usuario_email", "sistemas"):
            _, texto, html = self._montar(tipo)
            for trecho in ("CH-00007", "Nº 7", "Tecnologia da Informação"):
                self.assertIn(trecho, texto, (tipo, trecho))
            self.assertIn("Fulano Silva", html)
            self.assertIn("Abertura", html)

    def test_equipamentos_reservados_aparecem_com_identificacao_e_situacao(self):
        eq = [{"equipamento": "Notebook — Dell", "identificacao": "Série ABC", "situacao": "Reservado", "tom": "roxo"}]
        _, texto, html = self._montar("maquina", equipamentos=eq)
        self.assertIn("Série ABC", html)
        self.assertIn("Reservado", html)
        self.assertIn("Notebook — Dell", texto)

    def test_sem_equipamento_o_email_diz_isso(self):
        _, _, html = self._montar("maquina")
        self.assertIn("Nenhum equipamento reservado", html)

    def test_link_so_aparece_quando_a_url_do_sistema_esta_configurada(self):
        _, _, html = self._montar("maquina")
        self.assertNotIn("Abrir o chamado na plataforma", html)
        _, texto, html = self._montar("maquina", link="http://sistema/contratacoes/7/chamados/1/")
        self.assertIn("Abrir o chamado na plataforma", html)
        self.assertIn("http://sistema/contratacoes/7/chamados/1/", texto)

    def test_link_absoluto_respeita_a_configuracao(self):
        with override_settings(SISTEMA_URL=""):
            self.assertEqual(email_layout.link_absoluto("/x/"), "")
        with override_settings(SISTEMA_URL="http://172.16.60.254:8000"):
            self.assertEqual(email_layout.link_absoluto("/x/"), "http://172.16.60.254:8000/x/")

    def test_valores_do_formulario_sao_escapados(self):
        dados = {**self.DADOS, "cargo": "<script>alert(1)</script>", "descricao": "<img src=x onerror=1>"}
        agora = timezone.now()
        _, _, html = email_alertas.montar_chamado_contratacao(
            "maquina", dados, solicitado_em=agora, enviado_em=agora,
        )
        self.assertNotIn("<script>alert(1)", html)
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;script&gt;", html)

    def test_nao_ha_dado_sensivel_no_corpo(self):
        for tipo in ("maquina", "usuario_email", "sistemas"):
            _, texto, html = self._montar(tipo)
            for proibido in ("senha", "password", "cpf", "token"):
                self.assertNotIn(proibido, (texto + html).lower(), (tipo, proibido))

    def test_html_tem_viewport_celular_e_largura_maxima(self):
        html = self._montar("maquina")[2]
        self.assertIn('name="viewport"', html)
        self.assertIn("max-width:640px", html)
        self.assertIn("@media only screen and (max-width:540px)", html)

    def test_a_logo_vai_embutida_por_cid_no_envio(self):
        with override_settings(
            EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        ):
            vaga = self.vaga
            ch = ContratacaoChamado.objects.create(
                contratacao=vaga, tipo="maquina", status="pendente",
                dados=chamados_svc.montar_dados(vaga, "maquina", {"descricao": "x"}, self.rh),
                criado_por=self.rh,
            )
            chamados_svc._tentar_envio(ch, self.rh)
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertIn(f"cid:{email_layout.LOGO_CID}", msg.alternatives[0][0])
        anexos_cid = [
            p for p in msg.message().walk() if p.get("Content-ID") == f"<{email_layout.LOGO_CID}>"
        ]
        self.assertEqual(len(anexos_cid), 1)
        self.assertEqual(msg.message().get_content_subtype(), "related")

    def test_o_email_da_maquina_traz_o_retrato_atual_da_vaga(self):
        self._reservar()
        cs.preencher_vaga(
            contratacao_pk=self.vaga.pk, user=self.ti,
            dados={"colaborador_nome": "Beltrano Souza", "cadastrar_colaborador": True},
        )
        vaga = Contratacao.objects.get(pk=self.vaga.pk)
        ch = ContratacaoChamado.objects.create(
            contratacao=vaga, tipo="maquina", status="pendente",
            dados=chamados_svc.montar_dados(vaga, "maquina", {"descricao": "x"}, self.rh), criado_por=self.rh,
        )
        chamados_svc._tentar_envio(ch, self.rh)
        msg = mail.outbox[-1]
        self.assertIn("Beltrano Souza", msg.subject)
        self.assertIn("NB-001", msg.body)
        self.assertIn("Reservado", msg.body)
        self.assertIn("CH-%05d" % ch.pk, msg.body)

    def test_alerta_de_vaga_sem_equipamento_tem_o_conteudo_pedido(self):
        reserva, = self._reservar()
        ReservaEquipamento.objects.filter(pk=reserva.pk).update(
            estado=Estado.CEDIDA, encerrada_em=timezone.now(), encerrada_por=self.ti2,
            motivo="Reposição do financeiro",
        )
        reserva.refresh_from_db()
        resultado = email_alertas.alerta_vaga_sem_equipamento(reserva)
        self.assertTrue(resultado.ok)
        msg = mail.outbox[-1]
        self.assertIn("Vaga sem equipamento reservado", msg.subject)
        for trecho in ("Técnico de Campo", "NB-001", "Reposição do financeiro", "nova reserva", "Ação necessária"):
            self.assertIn(trecho, msg.body)

    def test_o_canal_do_alerta_esta_no_catalogo(self):
        codigos = {c["codigo"] for c in email_alertas.CATALOGO_NOTIFICACOES}
        self.assertIn("vaga_sem_equipamento", codigos)

    def test_nenhum_endereco_no_codigo_do_alerta(self):
        import inspect
        import re
        for f in (email_alertas.alerta_vaga_sem_equipamento, chamados_svc.contexto_do_email):
            self.assertIsNone(re.search(r"[\w.]+@[\w.]+\.\w+", inspect.getsource(f)), f.__name__)
