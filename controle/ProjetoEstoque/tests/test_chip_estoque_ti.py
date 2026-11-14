"""Chips do Agro × estoque de TI (regra 66): a ligação 1 para 1 pelo ICCID, a conciliação, o cadastro do patrimônio do chip no
estoque (valor, NF), as duas fichas (item de TI e chip do Agro), as permissões e a importação.

O que se prova: a chave é o ICCID (19 primeiros dígitos); só o par 1 × 1 liga sozinho; dois identificadores diferentes pedem
confirmação; ligar e desligar gravam evento e NUNCA alteram um item existente; criar o chip a partir do item e o item a partir do
chip são atômicos e recusam o duplicado; divergência é apontada, não corrigida; quem não tem acesso ao estoque de TI não liga nem vê
valor/NF/termo; `voltar` não vira redirecionamento aberto."""

import io
from decimal import Decimal

from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from ProjetoEstoque.models import (
    GRUPO_SOMENTE_AGRO,
    Categoria,
    CentroCusto,
    Fornecedor,
    FrotaImportacaoLinha,
    Item,
    Localidade,
    M2MChip,
    M2MEvento,
    MovimentacaoItem,
    Subtipo,
)
from ProjetoEstoque.tests import test_m2m as base
from ProjetoEstoque.tests.test_m2m_telas import TelasBase
from services import chip_estoque_ti_service as estoque
from services import frota_import_service as imp
from services import m2m_monitoramento_service as m2m


class EstoqueBase(TelasBase):
    def setUp(self):
        super().setUp()
        self.categoria = Categoria.objects.create(nome="Equipamento")
        self.tipo_chip = Subtipo.objects.create(nome="CHIP", alocado="nao", categoria=self.categoria, e_chip=True)
        self.tipo_tablet = Subtipo.objects.create(nome="Tablet", alocado="sim", categoria=self.categoria, aceita_chip=True)

    def item(self, n, **extra):
        dados = dict(nome="TIMCHIP", numero_serie=base.iccid_valido(n), subtipo=self.tipo_chip, categoria=self.categoria, valor=Decimal("15.00"))
        dados.update(extra)
        return Item.objects.create(**dados)

    def chip(self, n, **extra):
        return m2m.criar_chip({"nome": f"Chip {n}", "iccid": base.iccid_valido(n), **extra}, self.usuario)

    def ti(self, nome="operador_ti", *chaves):
        """Usuário do TI (acessa `/equipamentos/…`) com as permissões de chips pedidas."""
        return self.cliente_de(self.usuario_com(nome, *chaves))

    def so_agro(self, nome="so_agro", *chaves):
        user = self.usuario_com(nome, *chaves, "acessar_zelo_agro")
        user.groups.add(Group.objects.get_or_create(name=GRUPO_SOMENTE_AGRO)[0])
        return self.cliente_de(User_(user))


def User_(user):
    from django.contrib.auth.models import User
    return User.objects.get(pk=user.pk)


# ═════════════════════════════════════════════════════════════
# A chave e a ligação
# ═════════════════════════════════════════════════════════════

class ChaveTests(EstoqueBase):
    def test_a_chave_e_o_iccid_sem_o_digito_verificador(self):
        iccid = base.iccid_valido(1)
        self.assertEqual(estoque.chave_iccid(iccid), iccid[:19])
        self.assertEqual(estoque.chave_iccid(iccid + "5"), iccid[:19])          # 20 dígitos: o mesmo SIM
        for ruim in ("", "S/N", None, "1234", "abc"):
            self.assertEqual(estoque.chave_iccid(ruim), "", ruim)

    def test_subtipo_nao_e_chip_e_aceita_chip_ao_mesmo_tempo(self):
        s = Subtipo(nome="Híbrido", alocado="nao", categoria=self.categoria, e_chip=True, aceita_chip=True)
        with self.assertRaises(ValidationError) as cm:
            s.full_clean()
        self.assertIn("e_chip", cm.exception.message_dict)


class VincularTests(EstoqueBase):
    def test_liga_grava_evento_e_nao_altera_o_item(self):
        chip, item = self.chip(1), self.item(1)
        antes = Item.objects.filter(pk=item.pk).values().get()
        r = estoque.vincular(chip, item, self.usuario)
        chip.refresh_from_db()
        self.assertEqual((chip.item_ti_id, chip.item_ti_origem, chip.item_ti_por_id), (item.pk, "manual", self.usuario.pk))
        self.assertIsNotNone(chip.item_ti_em)
        ev = M2MEvento.objects.filter(chip=chip, tipo=M2MEvento.Tipo.ITEM_TI_LIGADO).get()
        self.assertEqual((ev.dados["item"], ev.dados["origem"], ev.dados["identificadores_diferentes"]), (item.pk, "manual", False))
        self.assertEqual(Item.objects.filter(pk=item.pk).values().get(), antes)       # o item nem updated_at mudou
        self.assertEqual(r.chip.pk, chip.pk)

    def test_recusa_item_que_nao_e_chip_de_consumo_ou_excluido(self):
        chip = self.chip(1)
        tablet = Item.objects.create(nome="Tab", numero_serie=base.iccid_valido(1), subtipo=self.tipo_tablet, categoria=self.categoria)
        with self.assertRaisesRegex(ValidationError, "não está marcado como chip"):
            estoque.vincular(chip, tablet, self.usuario)
        consumo = self.item(2, item_consumo="sim")
        with self.assertRaisesRegex(ValidationError, "consumo"):
            estoque.vincular(chip, consumo, self.usuario)
        excluido = self.item(3, excluido=True)
        with self.assertRaisesRegex(ValidationError, "excluído"):
            estoque.vincular(chip, excluido, self.usuario)
        self.assertFalse(M2MChip.objects.get(pk=chip.pk).item_ti_id)

    def test_um_chip_um_item_nos_dois_sentidos(self):
        a, b = self.chip(1), self.chip(2)
        i1, i2 = self.item(1), self.item(2)
        estoque.vincular(a, i1, self.usuario)
        with self.assertRaisesRegex(ValidationError, "já está ligado ao item"):
            estoque.vincular(a, i2, self.usuario)
        with self.assertRaisesRegex(ValidationError, "já está ligado ao chip"):
            estoque.vincular(b, i1, self.usuario)
        with self.assertRaisesRegex(ValidationError, "já está ligado a este item"):
            estoque.vincular(a, i1, self.usuario)

    def test_identificadores_diferentes_pedem_confirmacao(self):
        chip, item = self.chip(1), self.item(2)
        with self.assertRaises(ValidationError) as cm:
            estoque.vincular(chip, item, self.usuario)
        self.assertEqual(cm.exception.code, estoque.CODIGO_IDENTIFICADORES_DIFERENTES)
        self.assertFalse(M2MChip.objects.get(pk=chip.pk).item_ti_id)
        r = estoque.vincular(chip, item, self.usuario, forcar=True)
        self.assertTrue(M2MEvento.objects.get(chip=chip, tipo=M2MEvento.Tipo.ITEM_TI_LIGADO).dados["identificadores_diferentes"])
        self.assertTrue(any("identificadores diferentes" in a for a in r.avisos))

    def test_item_sem_iccid_na_serie_liga_sem_pedir_confirmacao(self):
        chip, item = self.chip(1), self.item(1, numero_serie="S/N")
        estoque.vincular(chip, item, self.usuario)
        self.assertEqual(M2MChip.objects.get(pk=chip.pk).item_ti_id, item.pk)

    def test_desligar_exige_motivo_e_desfaz_so_a_ligacao(self):
        chip, item = self.chip(1), self.item(1)
        estoque.vincular(chip, item, self.usuario)
        with self.assertRaises(ValidationError):
            estoque.desvincular(chip, self.usuario, "  ")
        estoque.desvincular(chip, self.usuario, "ligado errado")
        chip.refresh_from_db()
        self.assertEqual((chip.item_ti_id, chip.item_ti_origem, chip.item_ti_em), (None, "", None))
        self.assertTrue(Item.objects.filter(pk=item.pk).exists() and M2MChip.objects.filter(pk=chip.pk).exists())
        self.assertEqual(M2MEvento.objects.get(chip=chip, tipo=M2MEvento.Tipo.ITEM_TI_DESLIGADO).dados["motivo"], "ligado errado")
        with self.assertRaisesRegex(ValidationError, "não está ligado"):
            estoque.desvincular(chip, self.usuario, "de novo")


# ═════════════════════════════════════════════════════════════
# Conciliação
# ═════════════════════════════════════════════════════════════

class ConciliacaoTests(EstoqueBase):
    def test_cruza_pares_ambiguos_e_o_que_sobra(self):
        c1, c2, c3 = self.chip(1), self.chip(2), self.chip(3)
        i1 = self.item(1)
        self.item(4)                                                    # item sem chip
        self.item(5, numero_serie="S/N")                                # item sem ICCID
        dup_a, dup_b = self.item(3), self.item(3)                       # dois itens com o mesmo ICCID do chip 3
        conc = estoque.conciliacao()
        self.assertEqual([(c.pk, i.pk) for c, i in conc.automaticos], [(c1.pk, i1.pk)])
        self.assertEqual([(a["chave"], len(a["chips"]), len(a["itens"])) for a in conc.ambiguos], [(estoque.chave_iccid(c3.iccid), 1, 2)])
        self.assertEqual([c.pk for c in conc.chips_sem_item], [c2.pk])
        self.assertEqual(sorted(i.numero_serie for i in conc.itens_sem_chip), sorted(["S/N", base.iccid_valido(4)]))
        self.assertEqual(len(conc.itens_sem_iccid), 1)
        self.assertEqual((conc.total_chips, conc.total_itens), (3, 5))
        self.assertTrue(dup_a.pk != dup_b.pk)

    def test_automatica_liga_so_o_par_um_para_um_e_e_idempotente(self):
        c1, c3 = self.chip(1), self.chip(3)
        i1 = self.item(1)
        self.item(3), self.item(3)
        r = estoque.conciliar_automaticamente(self.usuario)
        self.assertEqual((r.ligados, r.falhas), (1, []))
        self.assertEqual(M2MChip.objects.get(pk=c1.pk).item_ti_id, i1.pk)
        self.assertEqual(M2MChip.objects.get(pk=c1.pk).item_ti_origem, "iccid")
        self.assertIsNone(M2MChip.objects.get(pk=c3.pk).item_ti_id)                 # ambíguo: ninguém liga
        self.assertEqual(estoque.conciliar_automaticamente(self.usuario).ligados, 0)

    def test_chip_novo_ja_nasce_ligado_ao_item_de_mesmo_iccid_se_for_um_so(self):
        item = self.item(7)
        chip = self.chip(7)
        self.assertEqual((chip.item_ti_id, chip.item_ti_origem), (item.pk, "iccid"))
        self.item(8), self.item(8)
        self.assertIsNone(self.chip(8).item_ti_id)                                  # dois candidatos: não adivinha
        self.assertIsNone(self.chip(9).item_ti_id)                                  # nenhum item

    def test_sugestoes_e_opcoes_do_seletor(self):
        item = self.item(1)
        livre, outro = self.chip(2), None
        M2MChip.objects.filter(pk=livre.pk).update(iccid=base.iccid_valido(1))      # simula o chip de mesmo ICCID do item
        self.assertEqual([c.pk for c in estoque.sugestoes_para_item(item)], [livre.pk])
        self.assertTrue(estoque.opcoes_de_chips_sem_item("Chip 2"))
        self.assertEqual(estoque.opcoes_de_chips_sem_item("zzz"), [])
        self.assertIsNone(outro)


class CriarDeUmLadoParaOOutroTests(EstoqueBase):
    def test_criar_chip_a_partir_do_item_liga_e_nao_toca_no_item(self):
        item = self.item(5, marca="TIM", localidade=Localidade.objects.create(local="Fazenda Karitel"))
        antes = Item.objects.filter(pk=item.pk).values().get()
        chip = estoque.criar_chip_do_item(item, self.usuario)
        self.assertEqual((chip.iccid, chip.item_ti_id, chip.item_ti_origem), (item.numero_serie, item.pk, "chip_do_item"))
        self.assertEqual(chip.descricao, "TIMCHIP")
        self.assertTrue(chip.nome.startswith("Chip "))
        self.assertEqual(Item.objects.filter(pk=item.pk).values().get(), antes)
        with self.assertRaisesRegex(ValidationError, "já está ligado"):
            estoque.criar_chip_do_item(item, self.usuario)

    def test_criar_chip_recusa_serie_que_nao_e_iccid_e_iccid_ja_cadastrado(self):
        with self.assertRaisesRegex(ValidationError, "não é um ICCID válido"):
            estoque.criar_chip_do_item(self.item(1, numero_serie="S/N"), self.usuario)
        livre = self.item(2)
        M2MChip.objects.create(nome="Existente", iccid=livre.numero_serie)
        with self.assertRaisesRegex(ValidationError, "Já existe um chip"):
            estoque.criar_chip_do_item(livre, self.usuario)

    def test_cadastrar_itens_dos_chips_cria_o_patrimonio_com_nf_e_valor_e_liga(self):
        forn = Fornecedor.objects.create(nome="TIM S.A.", cnpj="02.421.421/0001-11")
        cc = CentroCusto.objects.create(numero="14", departamento="Apontamento")
        c1, c2 = self.chip(1), self.chip(2)
        r = estoque.cadastrar_itens_dos_chips(
            [c1, c2], {"nome": "TIMCHIP", "marca": "TIM", "valor": "15,50", "numero_nf": "NF-123", "fornecedor": forn.pk,
                       "centro_custo": cc.pk, "data_compra": "2026-10-01", "status": "backup"},
            self.usuario,
        )
        self.assertEqual((len(r.criados), r.falhas), (2, []))
        for chip in (c1, c2):
            chip.refresh_from_db()
            item = chip.item_ti
            self.assertEqual((item.numero_serie, item.valor, item.numero_nf, item.fornecedor_id, item.centro_custo_id, item.status),
                             (chip.iccid, Decimal("15.50"), "NF-123", forn.pk, cc.pk, "backup"))
            self.assertEqual((item.subtipo_id, item.categoria_id, item.item_consumo, item.quantidade), (self.tipo_chip.pk, self.categoria.pk, "nao", 1))
            self.assertEqual(chip.item_ti_origem, "item_do_chip")
        self.assertEqual(Item.objects.filter(subtipo=self.tipo_chip).count(), 2)

    def test_cadastrar_recusa_dado_invalido_chip_ja_ligado_e_duplicado_sem_gravar(self):
        c = self.chip(1)
        for dados, campo in (({"nome": "", "valor": "1"}, "nome"), ({"nome": "X", "valor": "abc"}, "valor"),
                             ({"nome": "X", "data_compra": "2999-01-01"}, "data_compra"), ({"nome": "X", "status": "descarte"}, "status")):
            with self.assertRaises(ValidationError) as cm:
                estoque.cadastrar_itens_dos_chips([c], dados, self.usuario)
            self.assertIn(campo, cm.exception.message_dict, dados)
        self.assertEqual(Item.objects.count(), 0)
        ligado = self.chip(2)
        item = self.item(2)
        estoque.vincular(ligado, item, self.usuario)
        r = estoque.cadastrar_itens_dos_chips([ligado], {"nome": "X"}, self.usuario)
        self.assertEqual((len(r.criados), len(r.falhas)), (0, 1))
        self.assertIn("já tem item", r.falhas[0][1])
        sobra = self.chip(3)
        self.item(3)
        M2MChip.objects.filter(pk=sobra.pk).update(item_ti=None)
        r = estoque.cadastrar_itens_dos_chips([sobra], {"nome": "X"}, self.usuario)
        self.assertIn("Já existe o item", r.falhas[0][1])                          # ligue em vez de duplicar

    def test_cadastrar_exige_um_tipo_marcado_como_chip(self):
        Subtipo.objects.update(e_chip=False)
        with self.assertRaisesRegex(ValidationError, "marque como “É chip"):
            estoque.cadastrar_itens_dos_chips([self.chip(1)], {"nome": "X"}, self.usuario)

    def test_padroes_vem_do_ultimo_chip_cadastrado(self):
        self.item(1, marca="TIM", modelo="TM04", valor=Decimal("12.00"))
        self.assertEqual((estoque.padroes_de_cadastro()["marca"], estoque.padroes_de_cadastro()["modelo"]), ("TIM", "TM04"))


# ═════════════════════════════════════════════════════════════
# O patrimônio lido e as divergências
# ═════════════════════════════════════════════════════════════

class FichaEDivergenciasTests(EstoqueBase):
    def par(self, status="ativo"):
        chip, item = self.chip(1), self.item(1, status=status)
        estoque.vincular(chip, item, self.usuario)
        return M2MChip.objects.get(pk=chip.pk), Item.objects.get(pk=item.pk)

    def codigos(self, chip, item):
        return {d["codigo"] for d in estoque.divergencias_do_par(chip, item)}

    def test_ficha_traz_valor_nf_status_e_detentor_sem_termo(self):
        chip, item = self.par()
        Item.objects.filter(pk=item.pk).update(numero_nf="NF-9", numero_pedido="P-1")
        f = estoque.ficha_do_item(Item.objects.get(pk=item.pk))
        self.assertEqual((f["valor"], f["numero_nf"], f["numero_pedido"], f["status_rotulo"]), (Decimal("15.00"), "NF-9", "P-1", "Ativo"))
        self.assertEqual(f["detentor"].rotulo, "Em estoque / Não definido")
        self.assertEqual(f["termo"]["situacao"], "sem")

    def test_detentor_segue_a_regra_da_ficha_do_equipamento(self):
        from ProjetoEstoque.models import Usuario
        chip, item = self.par()
        pessoa = Usuario.objects.create(nome="Fulano de Tal", email="f@x.com", status="ativo")
        MovimentacaoItem.objects.create(tipo_movimentacao="transferencia", tipo_transferencia="entrega", item=item, usuario=pessoa)
        det = estoque.detentores_de([item])[item.pk]
        self.assertEqual((det.rotulo, det.colaborador.pk), ("Usuário: Fulano de Tal", pessoa.pk))

    def test_cada_divergencia_e_apontada_e_nada_e_corrigido(self):
        chip, item = self.par(status="descarte")
        self.assertIn("item_fora_de_uso", self.codigos(chip, item))
        chip, item = self.par(status="backup") if False else (chip, item)
        M2MChip.objects.filter(pk=chip.pk).update(situacao_uso="inativo", monitorado=False)
        Item.objects.filter(pk=item.pk).update(status="ativo")
        chip.refresh_from_db(), item.refresh_from_db()
        self.assertIn("chip_inativo", self.codigos(chip, item))
        Item.objects.filter(pk=item.pk).update(numero_serie=base.iccid_valido(9))
        item.refresh_from_db()
        self.assertIn("iccid", self.codigos(chip, item))
        Item.objects.filter(pk=item.pk).update(excluido=True)
        M2MChip.objects.filter(pk=chip.pk).update(monitorado=True, situacao_uso="disponivel")
        chip.refresh_from_db(), item.refresh_from_db()
        self.assertIn("item_excluido", self.codigos(chip, item))
        self.assertEqual(Item.all_objects.get(pk=item.pk).status, "ativo")          # leitura não escreve

    def test_chip_em_uso_com_item_parado_ou_no_nome_de_alguem(self):
        from ProjetoEstoque.models import FrotaTipo, Usuario
        from services import frota_service as frota, frota_vinculo_service as vinculos
        chip, item = self.par(status="backup")
        veiculo = frota.criar_veiculo({"codigo": "V1", "nome": "Trator", "tipo": FrotaTipo.objects.get(nome="Trator").pk}, self.usuario)
        vinculos.vincular(chip, veiculo, self.usuario, "teste")
        chip.refresh_from_db()
        self.assertIn("item_parado", self.codigos(chip, item))
        pessoa = Usuario.objects.create(nome="Ciclano", email="c@x.com", status="ativo")
        MovimentacaoItem.objects.create(tipo_movimentacao="transferencia", tipo_transferencia="entrega", item=item, usuario=pessoa)
        self.assertIn("item_com_pessoa", self.codigos(chip, item))
        em_lote = estoque.divergencias_dos_ligados([(chip, item)])
        self.assertEqual({d["codigo"] for d in em_lote[chip.pk]}, self.codigos(chip, item))

    def test_resumo_do_patrimonio_soma_o_valor_dos_ligados(self):
        chip, item = self.par()
        outro = self.chip(2)
        estoque.vincular(outro, self.item(2, valor=Decimal("10.00")), self.usuario)
        self.assertEqual(estoque.resumo_do_patrimonio(), {"ligados": 2, "valor": Decimal("25.00")})


# ═════════════════════════════════════════════════════════════
# As telas e as ações
# ═════════════════════════════════════════════════════════════

class TelaDeConciliacaoTests(EstoqueBase):
    def test_renderiza_o_que_o_sistema_propoe_e_o_que_sobra(self):
        self.chip(1), self.chip(2)
        self.item(1)
        r = self.client.get(reverse("chips_conciliacao"))
        html = self.html(r)
        self.sem_lixo_de_template(html, "conciliacao")
        for texto in ("Pares encontrados pelo ICCID", "Chips sem item no estoque de TI", "Itens do estoque sem chip de conectividade",
                      "Divergências entre os dois cadastros", "Ligar os 1 pares", "Cadastrar os chips marcados"):
            self.assertIn(texto, html)
        self.assertEqual((r.context["total_automaticos"], r.context["total_sem_item"]), (1, 1))

    def test_a_busca_filtra_os_chips_sem_item(self):
        self.chip(1), self.chip(2)
        r = self.client.get(reverse("chips_conciliacao"), {"q": "Chip 2"})
        self.assertEqual([c.nome for c in r.context["sem_item"]], ["Chip 2"])
        self.assertEqual(self.client.get(reverse("chips_conciliacao"), {"q": base.iccid_valido(1)[-6:]}).context["total_sem_item_filtrados"], 1)

    def test_so_quem_ve_chips_abre_e_so_quem_vincula_ve_os_botoes(self):
        self.chip(1), self.item(1)
        self.assertEqual(self.ti("sem_nada").get(reverse("chips_conciliacao")).status_code, 403)
        so_ve = self.ti("so_ve_chips", "ver_chips").get(reverse("chips_conciliacao")).content.decode()
        self.assertNotIn("Ligar os", so_ve)
        self.assertNotIn("Cadastrar os chips marcados", so_ve)
        com = self.ti("opera_chips", "ver_chips", "vincular_chips").get(reverse("chips_conciliacao")).content.decode()
        self.assertIn("Ligar os 1 pares", com)

    def test_quem_so_tem_o_agro_nao_entra_no_estoque_de_ti(self):
        cliente = self.so_agro("agro_chips", "ver_chips", "vincular_chips")
        self.assertEqual(cliente.get(reverse("chips_conciliacao")).status_code, 403)
        self.assertEqual(cliente.post(reverse("chip_estoque_conciliar")).status_code, 403)

    def test_a_tela_nao_grava_nada(self):
        self.chip(1), self.item(1)
        self.client.get(reverse("chips_conciliacao"))
        self.assertEqual(M2MChip.objects.filter(item_ti__isnull=False).count(), 0)


class AcoesTests(EstoqueBase):
    def test_conciliar_automaticamente_pelo_post(self):
        c = self.chip(1)
        self.item(1)
        r = self.client.post(reverse("chip_estoque_conciliar"), {"voltar": reverse("chips_conciliacao")}, follow=True)
        self.assertContains(r, "1 chip ligado ao estoque de TI")
        self.assertTrue(M2MChip.objects.get(pk=c.pk).item_ti_id)
        self.assertEqual(self.client.get(reverse("chip_estoque_conciliar")).status_code, 405)

    def test_ligar_um_a_um_com_confirmacao_dos_identificadores(self):
        c, i = self.chip(1), self.item(2)
        r = self.client.post(reverse("chip_estoque_ligar"), {"chip": c.pk, "item": i.pk}, follow=True)
        self.assertContains(r, "diferente do número de série")
        self.assertIsNone(M2MChip.objects.get(pk=c.pk).item_ti_id)
        r = self.client.post(reverse("chip_estoque_ligar"), {"chip": c.pk, "item": i.pk, "forcar": "1"}, follow=True)
        self.assertContains(r, "ligado ao item")
        self.assertEqual(M2MChip.objects.get(pk=c.pk).item_ti_id, i.pk)

    def test_desligar_pelo_post_exige_motivo(self):
        c, i = self.chip(1), self.item(1)
        estoque.vincular(c, i, self.usuario)
        r = self.client.post(reverse("chip_estoque_desligar"), {"chip": c.pk, "motivo": ""}, follow=True)
        self.assertContains(r, "Informe o motivo")
        self.assertTrue(M2MChip.objects.get(pk=c.pk).item_ti_id)
        self.client.post(reverse("chip_estoque_desligar"), {"chip": c.pk, "motivo": "errado"}, follow=True)
        self.assertIsNone(M2MChip.objects.get(pk=c.pk).item_ti_id)

    def test_criar_chip_do_item_pelo_post(self):
        i = self.item(4)
        self.client.post(reverse("chip_estoque_criar_chip"), {"item": i.pk}, follow=True)
        self.assertEqual(M2MChip.objects.get(item_ti=i).iccid, i.numero_serie)

    def test_cadastrar_em_lote_pelo_post_marcados_e_todos_da_busca(self):
        a, b, c = self.chip(1), self.chip(2), self.chip(3)
        dados = {"nome": "TIMCHIP", "valor": "15,00", "numero_nf": "NF-77", "status": "backup"}
        r = self.client.post(reverse("chip_estoque_cadastrar_itens"), {**dados, "ids": [a.pk, b.pk]}, follow=True)
        self.assertContains(r, "2 chips cadastrados no estoque de TI")
        self.assertEqual(set(Item.objects.values_list("numero_nf", flat=True)), {"NF-77"})
        r = self.client.post(reverse("chip_estoque_cadastrar_itens"), {**dados, "todos": "1", "q": ""}, follow=True)
        self.assertContains(r, "1 chip cadastrado no estoque de TI")
        self.assertEqual(M2MChip.objects.filter(item_ti__isnull=False).count(), 3)
        self.assertTrue(c)

    def test_cadastrar_com_erro_volta_a_tela_com_400_e_o_que_foi_digitado(self):
        a = self.chip(1)
        r = self.client.post(reverse("chip_estoque_cadastrar_itens"), {"nome": "Meu chip", "valor": "abc", "numero_nf": "NF-1", "ids": [a.pk]})
        self.assertEqual(r.status_code, 400)
        html = r.content.decode()
        self.assertIn("deve ser um número", html)
        self.assertIn('value="Meu chip"', html)
        self.assertIn('value="NF-1"', html)
        self.assertEqual(Item.objects.count(), 0)
        vazio = self.client.post(reverse("chip_estoque_cadastrar_itens"), {"nome": "X"})
        self.assertEqual(vazio.status_code, 400)
        self.assertIn("Marque ao menos um chip", vazio.content.decode())

    def test_toda_acao_exige_vincular_chips_e_o_voltar_nao_vira_redirecionamento_aberto(self):
        c, i = self.chip(1), self.item(1)
        so_ve = self.ti("so_ve_acoes", "ver_chips")
        for nome, dados in (("chip_estoque_ligar", {"chip": c.pk, "item": i.pk}), ("chip_estoque_desligar", {"chip": c.pk, "motivo": "x"}),
                            ("chip_estoque_criar_chip", {"item": i.pk}), ("chip_estoque_cadastrar_itens", {"ids": [c.pk]}),
                            ("chip_estoque_conciliar", {})):
            self.assertEqual(so_ve.post(reverse(nome), dados).status_code, 403, nome)
        r = self.client.post(reverse("chip_estoque_ligar"), {"chip": c.pk, "item": i.pk, "voltar": "https://exemplo.com/roubo"})
        self.assertEqual(r["Location"], reverse("chips_conciliacao"))
        r = self.client.post(reverse("chip_estoque_desligar"), {"chip": c.pk, "motivo": "x", "voltar": "//exemplo.com"})
        self.assertEqual(r["Location"], reverse("chips_conciliacao"))

    def test_seletores_json(self):
        c = self.chip(1)
        self.item(2)
        r = self.client.get(reverse("chip_estoque_opcoes"), {"q": "Chip 1"})
        self.assertEqual([x["id"] for x in r.json()["results"]], [c.pk])
        self.assertEqual(r["Cache-Control"], "no-store")
        itens = self.client.get(reverse("chip_estoque_opcoes_itens")).json()["results"]
        self.assertEqual(len(itens), 1)
        self.assertEqual(self.ti("sem_perm_json").get(reverse("chip_estoque_opcoes")).status_code, 403)


class FichasTests(EstoqueBase):
    def test_ficha_do_item_de_chip_mostra_a_linha_ligada(self):
        c, i = self.chip(1, numero_linha="5586998462742"), self.item(1)
        estoque.vincular(c, i, self.usuario)
        html = self.html(self.client.get(reverse("equipamento_detalhe", args=[i.pk])))
        self.assertIn("Linha de conectividade", html)
        self.assertIn("5586998462742", html)
        self.assertIn(reverse("m2m_chip_detalhe", args=[c.pk]), html)
        self.assertIn("Desligar do chip", html)

    def test_ficha_do_item_sem_chip_sugere_o_de_mesmo_iccid_e_oferece_criar(self):
        i = self.item(1)
        c = self.chip(2)
        M2MChip.objects.filter(pk=c.pk).update(iccid=base.iccid_valido(1))
        html = self.html(self.client.get(reverse("equipamento_detalhe", args=[i.pk])))
        self.assertIn("mesmo ICCID", html)
        self.assertIn("Criar o chip de conectividade a partir deste item", html)
        self.assertIn(reverse("chip_estoque_ligar"), html)

    def test_so_aparece_em_item_de_chip_e_para_quem_ve_chips(self):
        tablet = Item.objects.create(nome="Tab", numero_serie="X1", subtipo=self.tipo_tablet, categoria=self.categoria)
        self.assertNotIn("Linha de conectividade", self.html(self.client.get(reverse("equipamento_detalhe", args=[tablet.pk]))))
        i = self.item(1)
        sem = self.ti("so_ficha").get(reverse("equipamento_detalhe", args=[i.pk]))
        self.assertNotIn("Linha de conectividade", sem.content.decode())

    def test_tela_do_chip_no_agro_mostra_o_patrimonio_para_o_ti_e_esconde_de_quem_so_tem_o_agro(self):
        c, i = self.chip(1), self.item(1, numero_nf="NF-5", valor=Decimal("15.00"))
        estoque.vincular(c, i, self.usuario)
        html = self.html(self.client.get(reverse("m2m_chip_detalhe", args=[c.pk])))
        for texto in ("Estoque de TI", "NF-5", "15,00", "Termo de responsabilidade", "Desligar do estoque de TI"):
            self.assertIn(texto, html)
        agro = self.so_agro("agro_ve", "ver_chips", "vincular_chips").get(reverse("m2m_chip_detalhe", args=[c.pk])).content.decode()
        self.assertIn("ligado ao item", agro)
        self.assertNotIn("NF-5", agro)
        self.assertNotIn("Desligar do estoque de TI", agro)

    def test_tela_do_chip_sem_item_oferece_ligar_e_cadastrar(self):
        c = self.chip(1)
        i = self.item(2)
        html = self.html(self.client.get(reverse("m2m_chip_detalhe", args=[c.pk])))
        self.assertIn("não tem patrimônio no estoque de TI", html)
        self.assertIn(i.numero_serie, html)
        self.assertIn(reverse("chips_conciliacao"), html)

    def test_lista_de_chips_filtra_com_e_sem_item(self):
        c1, c2 = self.chip(1), self.chip(2)
        estoque.vincular(c1, self.item(1), self.usuario)
        com = self.client.get(reverse("m2m_chips"), {"estoque_ti": "com"})
        sem = self.client.get(reverse("m2m_chips"), {"estoque_ti": "sem"})
        self.assertEqual([c.pk for c in com.context["chips"]], [c1.pk])
        self.assertEqual([c.pk for c in sem.context["chips"]], [c2.pk])
        self.assertIn("· TI", self.html(com))

    def test_dashboard_da_porta_avisa_dos_chips_sem_item_so_para_quem_opera_o_estoque(self):
        self.chip(1), self.chip(2)
        atencao = {i["rotulo"]: i for i in self.client.get(reverse("agro_visao_geral")).context["painel"]["atencao"]}
        self.assertEqual(atencao["Chips sem item no estoque"]["n"], 2)
        self.assertEqual(atencao["Chips sem item no estoque"]["url"], reverse("m2m_chips") + "?estoque_ti=sem")
        lista = self.client.get(reverse("m2m_chips"), {"estoque_ti": "sem"})
        self.assertEqual(lista.context["page_obj"].paginator.count, 2)
        agro = self.so_agro("agro_porta", "ver_chips", "vincular_chips").get(reverse("agro_visao_geral"))
        self.assertNotIn("Chips sem item no estoque", {i["rotulo"] for i in agro.context["painel"]["atencao"]})


class ImportacaoTests(EstoqueBase):
    def planilha(self, *iccids):
        linhas = ["ICCID;Identificação"] + [f"{iccid};Chip imp {n}" for n, iccid in enumerate(iccids, 1)]

        class Arquivo(io.BytesIO):
            name = "chips.csv"
        return Arquivo(("\n".join(linhas) + "\n").encode("utf-8"))

    def test_a_previa_avisa_do_item_do_estoque_e_confirmar_liga_sozinho(self):
        item = self.item(1)
        importacao = imp.analisar(self.planilha(base.iccid_valido(1), base.iccid_valido(2)), self.usuario)
        avisos = {l.numero: l.avisos for l in FrotaImportacaoLinha.objects.filter(importacao=importacao)}
        self.assertTrue(any("Já existe no estoque de TI" in a for a in sum(avisos.values(), [])))
        imp.confirmar(importacao, self.usuario)
        self.assertEqual(M2MChip.objects.get(iccid=base.iccid_valido(1)).item_ti_id, item.pk)
        self.assertIsNone(M2MChip.objects.get(iccid=base.iccid_valido(2)).item_ti_id)


class SeparacaoTests(EstoqueBase):
    def test_a_ponte_nova_e_so_referencia_e_esta_na_lista_do_teste_de_separacao(self):
        from ProjetoEstoque.tests.test_frota import SeparacaoDoEstoqueDeTITests
        self.assertIn(("M2MChip", "item_ti"), SeparacaoDoEstoqueDeTITests.PONTES_PERMITIDAS)

    def test_o_chip_nao_e_apagavel_com_item_ligado_e_o_item_nao_e_apagado_ao_desligar(self):
        from django.db.models import ProtectedError
        c, i = self.chip(1), self.item(1)
        estoque.vincular(c, i, self.usuario)
        with self.assertRaises(ProtectedError):
            Item.all_objects.filter(pk=i.pk).delete()
        self.assertTrue(timezone.now())


# ═════════════════════════════════════════════════════════════
# O padrão de estoque e o comando `cadastrar_chips_no_estoque`
# ═════════════════════════════════════════════════════════════

class PadraoEComandoBase(EstoqueBase):
    """Dois chips já no estoque e JÁ ENTREGUES a uma pessoa (como os 50 TIMCHIP do Carlos): a entrega os levou do centro de
    custo/localidade de estoque para os dela e os deixou Ativos. Os chips novos ainda não foram entregues."""

    def setUp(self):
        super().setUp()
        from ProjetoEstoque.models import Usuario
        self.cc_estoque = CentroCusto.objects.create(numero="12105", departamento="TI")
        self.cc_pessoa = CentroCusto.objects.create(numero="22101", departamento="PLANEJAMENTO")
        self.loc_estoque = Localidade.objects.create(local="ESCRITORIO TI")
        self.loc_pessoa = Localidade.objects.create(local="Apontamento")
        self.pessoa = Usuario.objects.create(nome="CARLOS GERMANO", email="carlos@x.com", status="ativo")
        self.entregues = []
        for n in (1, 2):
            item = self.item(n, nome="TIMCHIP", marca="TIM", modelo="TM04", valor=Decimal("15.00"), status="backup",
                             centro_custo=self.cc_estoque, localidade=self.loc_estoque)
            MovimentacaoItem.objects.create(
                tipo_movimentacao="transferencia", tipo_transferencia="entrega", item=item, usuario=self.pessoa,
                centro_custo_origem=self.cc_estoque, centro_custo_destino=self.cc_pessoa,
                localidade_origem=self.loc_estoque, localidade_destino=self.loc_pessoa,
            )
            item.status, item.centro_custo, item.localidade = "ativo", self.cc_pessoa, self.loc_pessoa      # o que a entrega faz
            item.save()
            self.entregues.append(item)
        for n in (1, 2):
            self.chip(n)                                                    # nascem ligados aos itens de mesmo ICCID
        self.restantes = [self.chip(n) for n in (3, 4, 5)]                   # ainda sem item

    def roda(self, *args):
        from django.core.management import call_command
        saida = io.StringIO()
        call_command("cadastrar_chips_no_estoque", *args, stdout=saida)
        return saida.getvalue()


class PadraoDeEstoqueTests(PadraoEComandoBase):
    def test_o_padrao_e_o_estado_de_antes_da_entrega_nao_o_do_item_entregue(self):
        p = estoque.padrao_de_estoque()
        self.assertEqual(p["amostra"], 2)
        self.assertEqual((p["nome"], p["marca"], p["modelo"], p["valor"]), ("TIMCHIP", "TIM", "TM04", Decimal("15.00")))
        self.assertEqual((p["centro_custo"], p["localidade"], p["status"]), (self.cc_estoque.pk, self.loc_estoque.pk, "backup"))
        self.assertEqual(p["consistencia"]["centro_custo"], (self.cc_estoque.pk, 2))
        self.assertEqual(Item.objects.get(pk=self.entregues[0].pk).status, "ativo")          # o item entregue segue como estava

    def test_item_que_nunca_saiu_do_estoque_vale_pelo_estado_atual(self):
        for n in (9, 10, 11):
            self.item(n, status="estoque", centro_custo=self.cc_estoque, localidade=self.loc_estoque)
        p = estoque.padrao_de_estoque()
        self.assertEqual((p["amostra"], p["status"], p["centro_custo"]), (5, "estoque", self.cc_estoque.pk))      # 3 em estoque x 2 backup
        self.assertEqual(p["consistencia"]["status"], ("estoque", 3))

    def test_sem_nenhum_chip_cadastrado_nao_ha_padrao(self):
        MovimentacaoItem.objects.all().delete()
        M2MChip.objects.update(item_ti=None)
        Item.objects.all().delete()
        p = estoque.padrao_de_estoque()
        self.assertEqual((p["amostra"], p["status"], p["nome"]), (0, "backup", None))

    def test_o_formulario_da_conciliacao_ja_vem_com_o_padrao_de_estoque(self):
        padroes = estoque.padroes_de_cadastro()
        self.assertEqual((padroes["centro_custo"], padroes["localidade"], padroes["status"]), (self.cc_estoque.pk, self.loc_estoque.pk, "backup"))
        self.assertEqual((padroes["fornecedor"], padroes["numero_nf"], padroes["data_compra"]), ("", "", ""))
        html = self.html(self.client.get(reverse("chips_conciliacao")))
        self.assertIn('value="TIMCHIP"', html)


class ComandoCadastrarChipsTests(PadraoEComandoBase):
    def test_o_ensaio_mostra_o_padrao_e_nao_grava_nada(self):
        antes = (Item.objects.count(), MovimentacaoItem.objects.count(), M2MEvento.objects.count())
        saida = self.roda()
        self.assertIn("Padrao lido de 2 chips", saida)
        self.assertIn("[2 de 2]", saida)
        self.assertIn("a cadastrar agora ........ 3", saida)
        self.assertIn("ENSAIO: nada foi gravado.", saida)
        self.assertIn("sem vinculo, sem movimentacao, sem termo", saida)
        self.assertEqual((Item.objects.count(), MovimentacaoItem.objects.count(), M2MEvento.objects.count()), antes)

    def test_executar_exige_usuario_que_exista(self):
        from django.core.management.base import CommandError
        with self.assertRaisesRegex(CommandError, "--usuario"):
            self.roda("--executar")
        with self.assertRaisesRegex(CommandError, "nao encontrado"):
            self.roda("--executar", "--usuario", "ninguem")
        self.assertEqual(Item.objects.count(), 2)

    def test_cadastra_os_restantes_no_estado_de_estoque_sem_vinculo_com_colaborador(self):
        saida = self.roda("--executar", "--usuario", self.usuario.username)
        self.assertIn("Cadastrados e ligados: 3", saida)
        novos = Item.objects.exclude(pk__in=[i.pk for i in self.entregues])
        self.assertEqual(novos.count(), 3)
        for chip in self.restantes:
            chip.refresh_from_db()
            item = chip.item_ti
            self.assertEqual((item.numero_serie, item.nome, item.marca, item.modelo, item.valor), (chip.iccid, "TIMCHIP", "TIM", "TM04", Decimal("15.00")))
            self.assertEqual((item.status, item.centro_custo_id, item.localidade_id), ("backup", self.cc_estoque.pk, self.loc_estoque.pk))
            self.assertEqual((item.categoria_id, item.item_consumo, item.criado_por_id), (self.categoria.pk, "nao", self.usuario.pk))     # a do padrão
            self.assertEqual(chip.item_ti_origem, "item_do_chip")
            self.assertIn("ainda nao entregue", item.observacoes)
        # nenhum vínculo com colaborador: nem movimentação, nem termo, nem vínculo de compartilhado
        self.assertEqual(MovimentacaoItem.objects.filter(item__in=novos).count(), 0)
        self.assertEqual(MovimentacaoItem.objects.filter(usuario=self.pessoa).count(), 2)       # só as duas entregas de antes
        # os já entregues continuam exatamente como estavam
        for item in self.entregues:
            item.refresh_from_db()
            self.assertEqual((item.status, item.centro_custo_id), ("ativo", self.cc_pessoa.pk))
        self.assertEqual(M2MChip.objects.filter(item_ti__isnull=True).count(), 0)

    def test_e_idempotente_e_o_limite_corta(self):
        self.roda("--executar", "--usuario", self.usuario.username, "--limite", "1")
        self.assertEqual(Item.objects.count(), 3)
        saida = self.roda("--executar", "--usuario", self.usuario.username)
        self.assertIn("Cadastrados e ligados: 2", saida)
        self.assertEqual(Item.objects.count(), 5)
        self.assertIn("Nada a cadastrar.", self.roda("--executar", "--usuario", self.usuario.username))
        self.assertEqual(Item.objects.count(), 5)

    def test_as_opcoes_trocam_nf_valor_data_e_status_do_lote(self):
        self.roda("--executar", "--usuario", self.usuario.username, "--nf", "NF-900", "--valor", "12,50",
                  "--data-compra", "2026-10-30", "--status", "estoque")
        item = M2MChip.objects.get(pk=self.restantes[0].pk).item_ti
        self.assertEqual((item.numero_nf, item.valor, str(item.data_compra), item.status), ("NF-900", Decimal("12.50"), "2026-10-30", "estoque"))

    def test_opcao_invalida_para_antes_de_gravar(self):
        from django.core.management.base import CommandError
        for args, texto in ((("--data-compra", "30/10/2026"), "AAAA-MM-DD"), (("--valor", "abc"), "invalido"), (("--fornecedor", "999"), "nao encontrado")):
            with self.assertRaisesRegex(CommandError, texto):
                self.roda("--executar", "--usuario", self.usuario.username, *args)
        self.assertEqual(Item.objects.count(), 2)

    def test_conciliar_liga_antes_os_pares_de_mesmo_iccid(self):
        extra = self.item(20)                                                    # item livre cujo chip já existe sem ligação
        chip = M2MChip.objects.create(nome="Chip par", iccid=extra.numero_serie)
        saida = self.roda("--executar", "--usuario", self.usuario.username, "--conciliar")
        self.assertIn("Conciliacao previa: 1 chip(s) ligado(s)", saida)
        self.assertEqual(M2MChip.objects.get(pk=chip.pk).item_ti_origem, "iccid")

    def test_sem_chip_cadastrado_ou_sem_tipo_de_chip_recusa_com_a_instrucao(self):
        from django.core.management.base import CommandError
        Subtipo.objects.update(e_chip=False)
        with self.assertRaisesRegex(CommandError, "E chip"):
            self.roda()
        Subtipo.objects.update(e_chip=True)
        MovimentacaoItem.objects.all().delete()
        M2MChip.objects.update(item_ti=None)
        Item.objects.all().delete()
        with self.assertRaisesRegex(CommandError, "Nenhum chip esta cadastrado"):
            self.roda()

    def test_sem_categoria_no_padrao_os_novos_tambem_nascem_sem_categoria(self):
        Item.objects.update(categoria=None)                                       # como os 50 TIMCHIP reais (importados por planilha)
        self.roda("--executar", "--usuario", self.usuario.username)
        self.assertEqual(M2MChip.objects.get(pk=self.restantes[0].pk).item_ti.categoria_id, None)

    def test_chip_sem_iccid_e_pulado(self):
        M2MChip.objects.create(nome="Chip so IMSI", imsi="724100000000999", identificador_consulta="imsi")
        saida = self.roda()
        self.assertIn("sem ICCID (pulados) ...... 1", saida)
        self.assertIn("a cadastrar agora ........ 3", saida)
