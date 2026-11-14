"""
Chip em tablet e celular do ESTOQUE DE TI (regra 62, 13/11/2026).

O vínculo de chip aponta para UM destino: um veículo (o que já existia) OU um equipamento do estoque de TI
(`FrotaVinculo.item`). Estes testes protegem o que falharia em silêncio:

  • O `Item` É SÓ REFERÊNCIA — nenhuma operação do módulo grava em `Item`, `MovimentacaoItem` ou
    `ItemStatusHistorico` (nem o `updated_at` do aparelho muda). Status do tablet ≠ sessão do chip.
  • UM CHIP, UM VÍNCULO ATIVO, EM QUALQUER DESTINO — e exatamente um destino por linha, garantidos pelo banco.
  • SÓ RECEBE CHIP O TIPO QUE O SUBTIPO MARCA (`aceita_chip`); excluído, de consumo, sem tipo, descartado e
    devolvido são recusados; parado (manutenção/defeito/pausado) recebe com aviso.
  • O CHIP DO TABLET NÃO É COMPONENTE DE MÁQUINA — a composição tecnológica dos veículos o ignora (antes de
    existir o filtro, `veiculo=None` quebrava a tela).
  • A PONTA DO TI — a ficha do aparelho mostra o cartão só a quem tem `ver_chips`; as ações exigem a permissão
    do módulo de chips (não o acesso ao Agro), conferem que o vínculo é MESMO daquele aparelho e voltam para a ficha.
"""

import importlib
import io

from django.contrib.messages import get_messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse

from ProjetoEstoque.forms import SubtipoForm
from ProjetoEstoque.models import (
    Categoria,
    FrotaMovimentacao,
    FrotaVinculo,
    Item,
    ItemStatusHistorico,
    M2MChip,
    M2MEvento,
    MovimentacaoItem,
    Subtipo,
    rotulo_do_equipamento_ti,
)
from ProjetoEstoque.tests.test_frota import FrotaBase
from ProjetoEstoque.tests.test_m2m_telas import TelasBase
from services import frota_composicao_service as composicao
from services import frota_vinculo_service as vinculos
from services import m2m_monitoramento_service as m2m

MIGRACAO = importlib.import_module("ProjetoEstoque.migrations.0201_subtipo_aceita_chip_padrao")


class TIBase(TelasBase, FrotaBase):
    """Cenário: uma categoria, um subtipo que aceita chip (Tablet) e um que não aceita (Monitor).
    `TelasBase` dá o superusuário logado e `usuario_com`/`cliente_de`; `FrotaBase` dá `fixar` e `outro`."""

    def setUp(self):
        super().setUp()
        self.categoria = Categoria.objects.create(nome="Equipamento de teste")
        self.sub_tablet = self.subtipo("Tablet de teste", True)
        self.sub_monitor = self.subtipo("Monitor de teste", False)

    def subtipo(self, nome, aceita):
        return Subtipo.objects.create(nome=nome, alocado="sim", categoria=self.categoria, aceita_chip=aceita)

    def item(self, nome="Galaxy Tab A7", serie="R9XR500VDDJ", subtipo=None, **extra):
        subtipo = self.sub_tablet if subtipo is None else subtipo
        dados = {"nome": nome, "numero_serie": serie, "quantidade": 1, "status": "ativo", "subtipo": subtipo}
        dados.update(extra)
        return Item.objects.create(**dados)

    def estoque_de_ti(self):
        """O que o módulo de chips NUNCA pode mexer: as linhas do `Item` e as contagens do razão do estoque."""
        return (
            list(Item.all_objects.order_by("pk").values()),
            MovimentacaoItem.objects.count(),
            ItemStatusHistorico.objects.count(),
        )


# ═════════════════════════════════════════════════════════════
# Service
# ═════════════════════════════════════════════════════════════

class VinculoComEquipamentoDeTITests(TIBase):
    def test_vincula_o_chip_ao_tablet_e_grava_a_referencia(self):
        tablet, chip = self.item(), self.chip(1)
        resultado = vinculos.vincular(chip, tablet, self.usuario, "tablet entregue ao setor")
        vinculo = resultado.vinculo
        self.assertEqual((vinculo.item_id, vinculo.veiculo_id), (tablet.pk, None))
        self.assertEqual((vinculo.destino_tipo, vinculo.destino), ("item", tablet))
        self.assertEqual(vinculo.destino_rotulo, "Galaxy Tab A7 · R9XR500VDDJ")
        chip.refresh_from_db()
        self.assertEqual(chip.situacao_uso, "vinculado")
        mov = resultado.movimentacao
        self.assertEqual((mov.tipo, mov.item_destino_id, mov.veiculo_destino_id), ("vinculacao", tablet.pk, None))
        self.assertEqual(mov.destino_rotulo, "Galaxy Tab A7 · R9XR500VDDJ")
        self.assertEqual(mov.origem_rotulo, "")
        evento = M2MEvento.objects.filter(chip=chip, tipo="vinculado").get()
        self.assertEqual(evento.dados["destino_texto"], "equipamento Galaxy Tab A7 · R9XR500VDDJ")
        self.assertIn("Vinculado ao equipamento Galaxy Tab A7", m2m._resumo_da_movimentacao("vinculado", evento.dados))

    def test_o_evento_antigo_de_veiculo_continua_legivel(self):
        """Evento gravado antes do equipamento de TI só tinha `veiculo_*`: a frase não pode perder o veículo."""
        dados = {"veiculo_destino": "VEI-001 · Trator", "motivo": "teste"}
        self.assertEqual(
            m2m._resumo_da_movimentacao("vinculado", dados), "Vinculado ao veículo VEI-001 · Trator. Motivo: teste",
        )

    def test_nenhuma_operacao_grava_no_estoque_de_ti(self):
        tablet, outro, celular = self.item(), self.item("Galaxy Tab A9", "SN-2"), self.item("Galaxy A13", "SN-3")
        veiculo = self.veiculo(1)
        chip, reserva = self.chip(1), self.chip(2)
        antes = self.estoque_de_ti()
        r = vinculos.vincular(chip, tablet, self.usuario, "teste")
        r = vinculos.transferir(r.vinculo.pk, veiculo, self.usuario, "teste")          # tablet -> veículo
        r = vinculos.transferir(r.vinculo.pk, outro, self.usuario, "teste")            # veículo -> tablet
        r = vinculos.transferir(r.vinculo.pk, celular, self.usuario, "teste")          # tablet -> tablet
        r = vinculos.substituir(r.vinculo.pk, reserva, self.usuario, "teste")
        vinculos.desvincular(r.vinculo.pk, self.usuario, "teste")
        vinculos.vincular(chip, tablet, self.usuario, "de novo")
        vinculos.inativar(chip, self.usuario, "teste")
        self.assertGreaterEqual(FrotaMovimentacao.objects.filter(item_origem__isnull=False).count()
                                + FrotaMovimentacao.objects.filter(item_destino__isnull=False).count(), 6)
        self.assertEqual(self.estoque_de_ti(), antes)

    def test_o_status_do_aparelho_e_a_sessao_do_chip_nao_se_derivam(self):
        tablet, chip = self.item(), self.chip(1)
        vinculos.vincular(chip, tablet, self.usuario, "teste")
        self.fixar(chip, "sem_sessao")
        tablet.refresh_from_db()
        self.assertEqual(tablet.status, "ativo")                         # chip sem sessão não "estraga" o tablet
        Item.all_objects.filter(pk=tablet.pk).update(status="defeito")
        chip.refresh_from_db()
        self.assertEqual((chip.situacao_uso, chip.sessao_estado), ("vinculado", "sem_sessao"))   # nem o contrário

    def test_recusa_o_tipo_que_nao_aceita_chip(self):
        monitor = self.item("Monitor Dell", "M-1", subtipo=self.sub_monitor)
        with self.assertRaises(ValidationError) as cm:
            vinculos.vincular(self.chip(1), monitor, self.usuario, "teste")
        self.assertIn("não está marcado para receber chip", " ".join(cm.exception.messages))
        self.assertFalse(FrotaVinculo.objects.exists())

    def test_recusa_o_equipamento_sem_tipo_excluido_de_consumo_descartado_e_devolvido(self):
        chip = self.chip(1)
        casos = {
            "sem tipo": Item.objects.create(nome="Sem tipo", quantidade=1, status="ativo"),
            "excluído": self.item("Excluído", "E-1", excluido=True),
            "de consumo": self.item("Consumo", "C-1", item_consumo="sim"),
            "descarte": self.item("Descarte", "D-1", status="descarte"),
            "devolvido": self.item("Devolvido", "V-1", status="devolvido"),
        }
        for rotulo, item in casos.items():
            with self.subTest(rotulo):
                with self.assertRaises(ValidationError):
                    vinculos.vincular(chip, item, self.usuario, "teste")
        self.assertFalse(FrotaVinculo.objects.exists())
        chip.refresh_from_db()
        self.assertEqual(chip.situacao_uso, "disponivel")

    def test_equipamento_parado_recebe_o_chip_com_aviso(self):
        for n, status in enumerate(("manutencao", "defeito", "pausado"), start=1):
            with self.subTest(status):
                tablet = self.item(f"Tab {n}", f"S-{n}", status=status)
                resultado = vinculos.vincular(self.chip(n), tablet, self.usuario, "teste")
                self.assertEqual(len(resultado.avisos), 1)
                self.assertIn("vinculado mesmo assim", resultado.avisos[0])

    def test_equipamento_em_backup_recebe_sem_aviso(self):
        resultado = vinculos.vincular(self.chip(1), self.item(status="backup"), self.usuario, "preparando o aparelho")
        self.assertEqual(resultado.avisos, [])

    def test_o_tablet_pode_ter_mais_de_um_chip(self):
        tablet = self.item()
        vinculos.vincular(self.chip(1), tablet, self.usuario, "chip 1")
        vinculos.vincular(self.chip(2), tablet, self.usuario, "chip 2 (dual SIM)")
        self.assertEqual(FrotaVinculo.objects.filter(item=tablet, fim_em__isnull=True).count(), 2)

    def test_chip_que_ja_esta_num_veiculo_nao_vai_para_o_tablet(self):
        chip, veiculo = self.chip(1), self.veiculo(1)
        vinculos.vincular(chip, veiculo, self.usuario, "teste")
        with self.assertRaises(ValidationError) as cm:
            vinculos.vincular(chip, self.item(), self.usuario, "teste")
        self.assertIn("já está vinculado ao veículo VEI-001", " ".join(cm.exception.messages))

    def test_chip_que_ja_esta_no_tablet_nao_vai_para_o_veiculo_sem_transferir(self):
        chip = self.chip(1)
        vinculos.vincular(chip, self.item(), self.usuario, "teste")
        with self.assertRaises(ValidationError) as cm:
            vinculos.vincular(chip, self.veiculo(1), self.usuario, "teste")
        self.assertIn("já está vinculado ao equipamento Galaxy Tab A7", " ".join(cm.exception.messages))

    def test_o_destino_so_pode_ser_veiculo_ou_item(self):
        with self.assertRaises(ValidationError):
            vinculos.vincular(self.chip(1), "um texto", self.usuario, "teste")


class RestricoesDoBancoTests(TIBase):
    def test_exatamente_um_destino_por_vinculo(self):
        chip, veiculo, tablet = self.chip(1), self.veiculo(1), self.item()
        with self.assertRaises(IntegrityError), transaction.atomic():
            FrotaVinculo.objects.create(chip=chip, veiculo=veiculo, item=tablet)      # os dois
        with self.assertRaises(IntegrityError), transaction.atomic():
            FrotaVinculo.objects.create(chip=chip)                                    # nenhum

    def test_um_chip_so_pode_ter_um_vinculo_ativo_em_qualquer_destino(self):
        chip = self.chip(1)
        FrotaVinculo.objects.create(chip=chip, item=self.item())
        with self.assertRaises(IntegrityError), transaction.atomic():
            FrotaVinculo.objects.create(chip=chip, veiculo=self.veiculo(1))
        with self.assertRaises(IntegrityError), transaction.atomic():
            FrotaVinculo.objects.create(chip=chip, item=self.item("Outro", "O-1"))

    def test_a_movimentacao_nao_aceita_veiculo_e_item_do_mesmo_lado(self):
        chip, veiculo, tablet = self.chip(1), self.veiculo(1), self.item()
        with self.assertRaises(IntegrityError), transaction.atomic():
            FrotaMovimentacao.objects.create(
                tipo="vinculacao", chip=chip, veiculo_destino=veiculo, item_destino=tablet, usuario=self.usuario,
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            FrotaMovimentacao.objects.create(
                tipo="transferencia", chip=chip, veiculo_origem=veiculo, item_origem=tablet, usuario=self.usuario,
            )

    def test_o_vinculo_de_tablet_continua_historico_imutavel(self):
        r = vinculos.vincular(self.chip(1), self.item(), self.usuario, "teste")
        with self.assertRaises(ValidationError):
            r.vinculo.delete()
        r.vinculo.motivo_fim = "mexi"
        with self.assertRaises(ValidationError):
            r.vinculo.save()                                                           # só o encerramento é gravável


class MovimentacoesComEquipamentoTests(TIBase):
    def test_transferencia_entre_os_tipos_de_destino(self):
        tablet, outro, veiculo, chip = self.item(), self.item("Galaxy Tab A9", "SN-2"), self.veiculo(1), self.chip(1)
        r = vinculos.vincular(chip, tablet, self.usuario, "teste")
        r = vinculos.transferir(r.vinculo.pk, veiculo, self.usuario, "foi para o trator")
        mov = r.movimentacao
        self.assertEqual((mov.origem_rotulo, mov.destino_rotulo), ("Galaxy Tab A7 · R9XR500VDDJ", "VEI-001 · Veículo 1"))
        self.assertEqual((r.vinculo.veiculo_id, r.vinculo.item_id), (veiculo.pk, None))
        r = vinculos.transferir(r.vinculo.pk, outro, self.usuario, "voltou para um tablet")
        self.assertEqual((r.vinculo.veiculo_id, r.vinculo.item_id), (None, outro.pk))
        self.assertEqual(FrotaVinculo.objects.filter(chip=chip).count(), 3)           # os 3 períodos ficam no histórico
        self.assertEqual(FrotaVinculo.objects.filter(chip=chip, fim_em__isnull=True).count(), 1)
        evento = M2MEvento.objects.filter(chip=chip, tipo="transferido").latest("pk")
        self.assertIn("Transferido do veículo VEI-001", m2m._resumo_da_movimentacao("transferido", evento.dados))
        self.assertIn("para o equipamento Galaxy Tab A9", m2m._resumo_da_movimentacao("transferido", evento.dados))

    def test_transferir_para_o_mesmo_equipamento_e_recusado(self):
        tablet = self.item()
        r = vinculos.vincular(self.chip(1), tablet, self.usuario, "teste")
        with self.assertRaises(ValidationError) as cm:
            vinculos.transferir(r.vinculo.pk, tablet, self.usuario, "teste")
        self.assertIn("O equipamento de destino é o mesmo de origem.", " ".join(cm.exception.messages))

    def test_substitui_o_chip_no_mesmo_tablet(self):
        tablet, velho, novo = self.item(), self.chip(1), self.chip(2)
        r = vinculos.vincular(velho, tablet, self.usuario, "teste")
        r = vinculos.substituir(r.vinculo.pk, novo, self.usuario, "chip danificado", "manutencao")
        saida, entrada = r.movimentacoes
        self.assertEqual((saida.item_origem_id, entrada.item_destino_id), (tablet.pk, tablet.pk))
        self.assertEqual(saida.operacao, entrada.operacao)
        velho.refresh_from_db(), novo.refresh_from_db()
        self.assertEqual((velho.situacao_uso, novo.situacao_uso), ("manutencao", "vinculado"))
        self.assertEqual(r.vinculo.item_id, tablet.pk)

    def test_substituir_pelo_mesmo_chip_diz_equipamento(self):
        chip = self.chip(1)
        r = vinculos.vincular(chip, self.item(), self.usuario, "teste")
        with self.assertRaises(ValidationError) as cm:
            vinculos.substituir(r.vinculo.pk, chip, self.usuario, "teste")
        self.assertIn("que já está no equipamento", " ".join(cm.exception.messages))

    def test_desvincula_e_o_chip_continua_cadastrado_e_disponivel(self):
        chip = self.chip(1)
        r = vinculos.vincular(chip, self.item(), self.usuario, "teste")
        r = vinculos.desvincular(r.vinculo.pk, self.usuario, "aparelho devolvido")
        chip.refresh_from_db()
        self.assertEqual(chip.situacao_uso, "disponivel")
        self.assertEqual(r.movimentacao.item_origem.nome, "Galaxy Tab A7")
        self.assertIsNotNone(r.vinculo_encerrado.fim_em)

    def test_inativar_o_chip_do_tablet_encerra_o_vinculo(self):
        chip = self.chip(1)
        vinculos.vincular(chip, self.item(), self.usuario, "teste")
        r = vinculos.inativar(chip, self.usuario, "chip perdido")
        self.assertEqual(r.movimentacao.item_origem.nome, "Galaxy Tab A7")
        self.assertIsNotNone(r.vinculo_encerrado.fim_em)
        self.assertFalse(FrotaVinculo.objects.filter(chip=chip, fim_em__isnull=True).exists())

    def test_vinculo_ja_encerrado_recusa_dizendo_o_equipamento(self):
        r = vinculos.vincular(self.chip(1), self.item(), self.usuario, "teste")
        vinculos.desvincular(r.vinculo.pk, self.outro, "saiu")
        with self.assertRaises(ValidationError) as cm:
            vinculos.desvincular(r.vinculo.pk, self.usuario, "de novo")
        texto = " ".join(cm.exception.messages)
        self.assertIn("já foi desvinculado por outro_usuario", texto)
        self.assertIn("equipamento Galaxy Tab A7", texto)

    def test_transferencia_ja_feita_recusa_com_o_destino_certo(self):
        r = vinculos.vincular(self.chip(1), self.item(), self.usuario, "teste")
        vinculos.transferir(r.vinculo.pk, self.item("Outro", "O-1"), self.outro, "foi")
        with self.assertRaises(ValidationError) as cm:
            vinculos.transferir(r.vinculo.pk, self.veiculo(1), self.usuario, "de novo")
        self.assertIn("transferido para outro equipamento por outro_usuario", " ".join(cm.exception.messages))


class LeiturasComEquipamentoTests(TIBase):
    def test_historico_do_chip_traz_os_periodos_em_tablet_e_em_veiculo(self):
        chip, tablet, veiculo = self.chip(1), self.item(), self.veiculo(1)
        r = vinculos.vincular(chip, tablet, self.usuario, "teste")
        vinculos.transferir(r.vinculo.pk, veiculo, self.usuario, "foi")
        atual, anterior = vinculos.historico_chip(chip)
        self.assertEqual((atual["destino_tipo"], atual["veiculo"].pk, atual["item"]), ("veiculo", veiculo.pk, None))
        self.assertEqual((anterior["destino_tipo"], anterior["item"].pk, anterior["veiculo"]), ("item", tablet.pk, None))
        self.assertEqual(anterior["destino_rotulo"], "Galaxy Tab A7 · R9XR500VDDJ")

    def test_a_busca_das_movimentacoes_acha_pelo_nome_e_pela_serie_do_aparelho(self):
        vinculos.vincular(self.chip(1), self.item(), self.usuario, "teste")
        vinculos.vincular(self.chip(2), self.veiculo(1), self.usuario, "teste")
        for busca in ("Galaxy", "R9XR500"):
            with self.subTest(busca):
                achadas = list(vinculos.historico_movimentacoes({"q": busca}))
                self.assertEqual([m.chip.nome for m in achadas], ["Chip 1"])

    def test_o_excel_das_movimentacoes_diz_origem_e_destino_do_equipamento(self):
        from openpyxl import load_workbook

        r = vinculos.vincular(self.chip(1), self.item(), self.usuario, "teste")
        vinculos.transferir(r.vinculo.pk, self.veiculo(1), self.usuario, "foi")
        ws = load_workbook(io.BytesIO(vinculos.exportar_movimentacoes_xlsx()))["Movimentações"]
        linhas = [[c.value for c in linha] for linha in ws.iter_rows(min_row=1)]
        self.assertEqual(linhas[0][6:8], ["Origem (veículo ou equipamento)", "Destino (veículo ou equipamento)"])
        transferencia = next(l for l in linhas[1:] if l[1] == "Transferência")
        self.assertEqual(transferencia[6:8], ["Galaxy Tab A7 · R9XR500VDDJ", "VEI-001 · Veículo 1"])

    def test_chips_do_equipamento_traz_ativos_com_a_conectividade_e_os_anteriores(self):
        tablet, velho, novo = self.item(), self.chip(1), self.chip(2)
        r = vinculos.vincular(velho, tablet, self.usuario, "teste")
        self.fixar(velho, "ativa")
        r = vinculos.substituir(r.vinculo.pk, novo, self.usuario, "chip danificado")
        dados = vinculos.chips_do_equipamento(tablet)
        self.assertEqual([c["chip"].nome for c in dados["ativos"]], ["Chip 2"])
        self.assertEqual(dados["ativos"][0]["chip"].situacao, "pendente")            # anotado, nunca consultado
        self.assertEqual(dados["total_anteriores"], 1)
        self.assertEqual(dados["anteriores"][0]["chip"].nome, "Chip 1")
        self.assertEqual(dados["anteriores"][0]["fim_tipo"], "substituicao")
        self.assertEqual(dados["anteriores"][0]["motivo_fim"], "chip danificado")

    def test_equipamento_aceita_chip(self):
        self.assertTrue(vinculos.equipamento_aceita_chip(self.item()))
        self.assertFalse(vinculos.equipamento_aceita_chip(self.item("M", "M-1", subtipo=self.sub_monitor)))
        self.assertFalse(vinculos.equipamento_aceita_chip(self.item("C", "C-1", item_consumo="sim")))
        self.assertFalse(vinculos.equipamento_aceita_chip(Item.objects.create(nome="x", quantidade=1)))

    def test_a_lista_de_chips_mostra_e_busca_pelo_equipamento(self):
        chip = self.chip(1)
        self.chip(2)
        vinculos.vincular(chip, self.item(), self.usuario, "teste")
        achados = m2m.anotar_chips(m2m.listar_chips({"q": "R9XR500"}))
        self.assertEqual([c.nome for c in achados], ["Chip 1"])
        self.assertEqual(achados[0].equipamento_atual.nome, "Galaxy Tab A7")
        self.assertEqual((achados[0].destino_atual_tipo, achados[0].veiculo_atual), ("item", None))
        self.assertEqual(achados[0].destino_atual_rotulo, "Galaxy Tab A7 · R9XR500VDDJ")

    def test_alerta_de_sem_sessao_vale_para_chip_em_tablet_e_nao_quebra(self):
        chip = self.chip(1)
        vinculos.vincular(chip, self.item(), self.usuario, "teste")
        self.fixar(chip, "sem_sessao")
        m2m.avaliar_ocorrencias(M2MChip.objects.get(pk=chip.pk), notificar=False)         # sem veículo: não pode explodir


class ComposicaoDasMaquinasTests(TIBase):
    def test_o_chip_do_tablet_nao_entra_na_composicao_dos_veiculos(self):
        veiculo = self.veiculo(1)
        vinculos.vincular(self.chip(1), veiculo, self.usuario, "no trator")
        vinculos.vincular(self.chip(2), self.item(), self.usuario, "no tablet")
        self.assertEqual(composicao.resumo()["chips"], 1)
        linhas = composicao.componentes()
        self.assertEqual([(l["origem"], l["codigo"]) for l in linhas], [("chip", "Chip 1")])


# ═════════════════════════════════════════════════════════════
# O marcador `aceita_chip` no subtipo
# ═════════════════════════════════════════════════════════════

class SubtipoAceitaChipTests(TIBase):
    def test_o_formulario_do_subtipo_grava_o_marcador(self):
        form = SubtipoForm(data={"nome": "Roteador 4G", "alocado": "sim", "categoria": self.categoria.pk, "aceita_chip": "on"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertTrue(form.save().aceita_chip)
        sem = SubtipoForm(data={"nome": "Mouse", "alocado": "sim", "categoria": self.categoria.pk})
        self.assertTrue(sem.is_valid(), sem.errors)
        self.assertFalse(sem.save().aceita_chip)                                        # por padrão, não aceita

    def test_a_tela_do_subtipo_oferece_o_marcador(self):
        resposta = self.client.get(reverse("subtipo_create"))
        self.assertContains(resposta, 'name="aceita_chip"')
        self.assertContains(resposta, "Aceita chip (SIM)")

    def test_a_migration_marca_so_tablet_e_celular_e_nao_desfaz_nada(self):
        from django.apps import apps

        tablet = Subtipo.objects.create(nome=" Tablet ", alocado="sim", categoria=self.categoria)
        celular = Subtipo.objects.create(nome="CELULAR", alocado="sim", categoria=self.categoria)
        outro = Subtipo.objects.create(nome="Notebook", alocado="sim", categoria=self.categoria)
        ja_marcado = Subtipo.objects.create(nome="Roteador 4G", alocado="sim", categoria=self.categoria, aceita_chip=True)
        MIGRACAO.marcar(apps, None)
        for s in (tablet, celular, outro, ja_marcado):
            s.refresh_from_db()
        self.assertEqual([tablet.aceita_chip, celular.aceita_chip, outro.aceita_chip, ja_marcado.aceita_chip], [True, True, False, True])


# ═════════════════════════════════════════════════════════════
# A ponta do TI: a ficha do aparelho
# ═════════════════════════════════════════════════════════════

class FichaDoEquipamentoTests(TIBase):
    def setUp(self):
        super().setUp()
        self.tablet = self.item()
        self.ficha = reverse("equipamento_detalhe", args=[self.tablet.pk])
        self.vincular_url = reverse("equipamento_chip_vincular", args=[self.tablet.pk])

    def test_o_cartao_so_aparece_para_quem_tem_ver_chips(self):
        sem = self.cliente_de(self.usuario_com("sem_perm"))
        self.assertNotContains(sem.get(self.ficha), 'id="ad-chip-card"')
        com = self.cliente_de(self.usuario_com("so_ver", "ver_chips"))
        resposta = com.get(self.ficha)
        self.assertContains(resposta, 'id="ad-chip-card"')
        self.assertContains(resposta, "Nenhum chip vinculado a este equipamento.")
        self.assertNotContains(resposta, self.vincular_url)                              # ver não é operar
        self.assertNotContains(resposta, "equipamento-chip.js")

    def test_com_a_permissao_de_vincular_o_cartao_oferece_o_formulario_e_a_busca(self):
        resposta = self.cliente_de(self.usuario_com("op", "ver_chips", "vincular_chips")).get(self.ficha)
        self.assertContains(resposta, self.vincular_url)
        self.assertContains(resposta, "equipamento-chip.js")
        self.assertContains(resposta, reverse("equipamento_chip_opcoes"))

    def test_o_tipo_que_nao_aceita_chip_nao_mostra_o_cartao_e_o_de_consumo_tambem_nao(self):
        cliente = self.cliente_de(self.usuario_com("op", "ver_chips", "vincular_chips"))
        monitor = self.item("Monitor Dell", "M-1", subtipo=self.sub_monitor)
        self.assertNotContains(cliente.get(reverse("equipamento_detalhe", args=[monitor.pk])), 'id="ad-chip-card"')
        consumo = self.item("Cabo", "K-1", item_consumo="sim")
        self.assertNotContains(cliente.get(reverse("equipamento_detalhe", args=[consumo.pk])), 'id="ad-chip-card"')

    def test_o_tipo_que_deixou_de_aceitar_ainda_mostra_o_chip_que_ele_ja_teve(self):
        vinculos.vincular(self.chip(1), self.tablet, self.usuario, "teste")
        Subtipo.objects.filter(pk=self.sub_tablet.pk).update(aceita_chip=False)
        resposta = self.cliente_de(self.usuario_com("so_ver", "ver_chips")).get(self.ficha)
        self.assertContains(resposta, "Chip 1")
        resposta = self.cliente_de(self.usuario_com("op", "ver_chips", "vincular_chips")).get(self.ficha)
        self.assertNotContains(resposta, self.vincular_url)                              # não aceita mais: não oferece vincular outro

    def test_o_cartao_mostra_o_chip_a_linha_a_sessao_e_o_historico(self):
        velho, novo = self.chip(1, numero_linha="5586998462835"), self.chip(2)
        r = vinculos.vincular(velho, self.tablet, self.usuario, "teste")
        vinculos.substituir(r.vinculo.pk, novo, self.usuario, "chip danificado")
        self.fixar(novo, "ativa")
        resposta = self.cliente_de(self.usuario_com("op", "ver_chips", "transferir_chips")).get(self.ficha)
        self.assertContains(resposta, "Chip 2")
        self.assertContains(resposta, "Sessão ativa")
        self.assertContains(resposta, "Chips anteriores (1)")
        self.assertContains(resposta, "chip danificado")
        self.assertContains(resposta, reverse("m2m_chip_detalhe", args=[novo.pk]))
        self.assertContains(resposta, "Nada muda no estoque deste equipamento.")

    def test_vincula_pela_ficha_e_volta_para_ela_sem_mexer_no_estoque(self):
        chip = self.chip(1)
        antes = self.estoque_de_ti()
        cliente = self.cliente_de(self.usuario_com("op", "ver_chips", "vincular_chips"))
        resposta = cliente.post(self.vincular_url, {"chip": chip.pk, "motivo": "tablet entregue"})
        self.assertRedirects(resposta, self.ficha, fetch_redirect_response=False)
        vinculo = FrotaVinculo.objects.get(chip=chip, fim_em__isnull=True)
        self.assertEqual(vinculo.item_id, self.tablet.pk)
        self.assertEqual(vinculo.inicio_por.username, "op")
        self.assertEqual(self.estoque_de_ti(), antes)

    def test_erro_de_validacao_volta_para_a_ficha_com_a_mensagem_e_nao_grava(self):
        chip = self.chip(1)
        cliente = self.cliente_de(self.usuario_com("op", "ver_chips", "vincular_chips"))
        resposta = cliente.post(self.vincular_url, {"chip": chip.pk, "motivo": ""}, follow=True)
        self.assertEqual(resposta.redirect_chain[-1][0], self.ficha)
        self.assertTrue(any("motivo" in str(m).lower() for m in get_messages(resposta.wsgi_request)))
        self.assertFalse(FrotaVinculo.objects.exists())
        monitor = self.item("Monitor Dell", "M-1", subtipo=self.sub_monitor)
        resposta = cliente.post(reverse("equipamento_chip_vincular", args=[monitor.pk]), {"chip": chip.pk, "motivo": "x"}, follow=True)
        self.assertTrue(any("não está marcado para receber chip" in str(m) for m in get_messages(resposta.wsgi_request)))
        self.assertFalse(FrotaVinculo.objects.exists())

    def test_sem_a_permissao_as_acoes_dao_403_e_nada_e_gravado(self):
        chip = self.chip(1)
        r = vinculos.vincular(chip, self.tablet, self.usuario, "teste")
        so_ver = self.cliente_de(self.usuario_com("so_ver", "ver_chips"))
        self.assertEqual(so_ver.post(self.vincular_url, {"chip": self.chip(2).pk, "motivo": "x"}).status_code, 403)
        url_des = reverse("equipamento_chip_desvincular", args=[self.tablet.pk, r.vinculo.pk])
        self.assertEqual(so_ver.post(url_des, {"motivo": "x"}).status_code, 403)
        url_sub = reverse("equipamento_chip_substituir", args=[self.tablet.pk, r.vinculo.pk])
        self.assertEqual(so_ver.post(url_sub, {"chip_novo": self.chip(3).pk, "motivo": "x"}).status_code, 403)
        # vincular_chips NÃO dá desvincular, e transferir_chips NÃO dá vincular
        so_vincular = self.cliente_de(self.usuario_com("vinc", "vincular_chips"))
        self.assertEqual(so_vincular.post(url_des, {"motivo": "x"}).status_code, 403)
        so_transferir = self.cliente_de(self.usuario_com("transf", "transferir_chips"))
        self.assertEqual(so_transferir.post(self.vincular_url, {"chip": self.chip(4).pk, "motivo": "x"}).status_code, 403)
        self.assertTrue(FrotaVinculo.objects.filter(pk=r.vinculo.pk, fim_em__isnull=True).exists())

    def test_so_aceita_post(self):
        cliente = self.cliente_de(self.usuario_com("op", "vincular_chips"))
        self.assertEqual(cliente.get(self.vincular_url).status_code, 405)

    def test_desvincula_e_substitui_pela_ficha(self):
        velho, novo = self.chip(1), self.chip(2)
        r = vinculos.vincular(velho, self.tablet, self.usuario, "teste")
        cliente = self.cliente_de(self.usuario_com("op", "ver_chips", "transferir_chips"))
        url_sub = reverse("equipamento_chip_substituir", args=[self.tablet.pk, r.vinculo.pk])
        resposta = cliente.post(url_sub, {"chip_novo": novo.pk, "motivo": "chip danificado", "destino_antigo": "manutencao"})
        self.assertRedirects(resposta, self.ficha, fetch_redirect_response=False)
        velho.refresh_from_db()
        self.assertEqual(velho.situacao_uso, "manutencao")
        ativo = FrotaVinculo.objects.get(item=self.tablet, fim_em__isnull=True)
        self.assertEqual(ativo.chip_id, novo.pk)
        url_des = reverse("equipamento_chip_desvincular", args=[self.tablet.pk, ativo.pk])
        resposta = cliente.post(url_des, {"motivo": "aparelho devolvido", "destino": "disponivel"})
        self.assertRedirects(resposta, self.ficha, fetch_redirect_response=False)
        self.assertFalse(FrotaVinculo.objects.filter(item=self.tablet, fim_em__isnull=True).exists())

    def test_o_id_do_vinculo_na_url_nao_serve_para_mexer_no_chip_de_outro_aparelho(self):
        outro = self.item("Galaxy Tab A9", "SN-2")
        r = vinculos.vincular(self.chip(1), outro, self.usuario, "teste")
        cliente = self.cliente_de(self.usuario_com("op", "ver_chips", "transferir_chips"))
        url = reverse("equipamento_chip_desvincular", args=[self.tablet.pk, r.vinculo.pk])      # vínculo de OUTRO aparelho
        resposta = cliente.post(url, {"motivo": "tentativa"}, follow=True)
        self.assertTrue(any("não está vinculado a este equipamento" in str(m) for m in get_messages(resposta.wsgi_request)))
        self.assertTrue(FrotaVinculo.objects.filter(pk=r.vinculo.pk, fim_em__isnull=True).exists())
        url = reverse("equipamento_chip_substituir", args=[self.tablet.pk, r.vinculo.pk])
        cliente.post(url, {"chip_novo": self.chip(2).pk, "motivo": "tentativa"})
        self.assertTrue(FrotaVinculo.objects.filter(pk=r.vinculo.pk, fim_em__isnull=True).exists())

    def test_a_busca_do_seletor_traz_so_chips_disponiveis(self):
        livre, usado = self.chip(1), self.chip(2)
        vinculos.vincular(usado, self.tablet, self.usuario, "teste")
        cliente = self.cliente_de(self.usuario_com("op", "vincular_chips"))
        dados = cliente.get(reverse("equipamento_chip_opcoes")).json()
        self.assertEqual([r["id"] for r in dados["results"]], [livre.pk])
        self.assertEqual(cliente.get(reverse("equipamento_chip_opcoes"), {"q": "zzz"}).json(), {"results": []})
        self.assertEqual(self.cliente_de(self.usuario_com("so_ver", "ver_chips")).get(reverse("equipamento_chip_opcoes")).status_code, 403)

    def test_quem_nao_esta_logado_vai_para_o_login(self):
        from django.test import Client

        resposta = Client().post(self.vincular_url, {"chip": 1, "motivo": "x"})
        self.assertEqual(resposta.status_code, 302)
        self.assertIn("login", resposta["Location"])


# ═════════════════════════════════════════════════════════════
# O outro lado: as telas do módulo de chips
# ═════════════════════════════════════════════════════════════

class TelasDoModuloComEquipamentoTests(TIBase):
    def setUp(self):
        super().setUp()
        self.tablet = self.item()
        self.chip_no_tablet = self.chip(1)
        vinculos.vincular(self.chip_no_tablet, self.tablet, self.usuario, "teste")

    def test_o_detalhe_do_chip_mostra_o_equipamento_com_link_para_a_ficha(self):
        resposta = self.client.get(reverse("m2m_chip_detalhe", args=[self.chip_no_tablet.pk]))
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Equipamento de TI")
        self.assertContains(resposta, reverse("equipamento_detalhe", args=[self.tablet.pk]))
        self.assertContains(resposta, "hoje no equipamento Galaxy Tab A7")
        self.assertContains(resposta, "Transferir para um veículo")

    def test_a_lista_de_chips_mostra_onde_o_chip_esta(self):
        resposta = self.client.get(reverse("m2m_chips"))
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Galaxy Tab A7 · R9XR500VDDJ")

    def test_a_tela_de_movimentacoes_mostra_o_equipamento(self):
        resposta = self.client.get(reverse("frota_movimentacoes"))
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, reverse("equipamento_detalhe", args=[self.tablet.pk]))
        busca = self.client.get(reverse("frota_movimentacoes"), {"q": "R9XR500"})
        self.assertContains(busca, "Chip 1")

    def test_a_visao_geral_da_frota_e_a_composicao_continuam_abrindo(self):
        for nome in ("frota_visao_geral", "frota_composicao", "m2m_visao_geral"):
            with self.subTest(nome):
                self.assertEqual(self.client.get(reverse(nome)).status_code, 200)

    def test_o_nome_do_equipamento_nunca_vira_html(self):
        suspeito = self.item('<script>alert(1)</script>', "X-1")
        vinculos.vincular(self.chip(2), suspeito, self.usuario, "teste")
        corpo = self.client.get(reverse("m2m_chips")).content.decode()
        self.assertNotIn("<script>alert(1)</script>", corpo)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", corpo)

    def test_o_rotulo_do_equipamento(self):
        self.assertEqual(rotulo_do_equipamento_ti(self.tablet), "Galaxy Tab A7 · R9XR500VDDJ")
        sem_serie = self.item("Galaxy A13", "S/N")
        self.assertEqual(rotulo_do_equipamento_ti(sem_serie), "Galaxy A13")
        sem_nada = Item.objects.create(nome="Sem série", quantidade=1)
        self.assertEqual(rotulo_do_equipamento_ti(sem_nada), "Sem série")
        self.assertEqual(rotulo_do_equipamento_ti(None), "")
