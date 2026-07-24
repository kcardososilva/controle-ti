"""
Corrige dois problemas encadeados no histórico de locação (`LocacaoPeriodo`):

1. `Locacao.data_entrada` em branco nos itens recém-cadastrados via
   `aplicar_analise_cadastral --cadastrar-novos` (a planilha de análise não
   trazia essa informação). Infere uma data lógica por equipamento: usa a
   data de entrada mais comum entre OUTRAS locações do mesmo fornecedor com
   o mesmo modelo já cadastradas; sem correspondência, usa a data mais
   comum entre TODAS as locações já existentes do fornecedor (o maior lote
   de entrada em massa já registrado). Realinha o período aberto desses
   itens (criado hoje pelo signal no momento do cadastro) para essa mesma
   data — item recém-criado tem exatamente um período, então não há risco
   de sobrescrever um histórico real de pausa/reativação.

2. Bug do campo "congelado": qualquer item locado com status Ativo/Backup/
   Manutenção que NUNCA teve nenhum `LocacaoPeriodo` aparece como
   "congelado" no detalhe, mesmo estando ativo — porque a lógica de
   congelamento (`item.eh_locado and loc_periodo_atual is None`) não
   distingue "nunca teve período" de "pausado de propósito". Isso afeta
   itens cadastrados antes do recurso de histórico de locação existir (ou
   por fluxos que não passam pelo signal). Faz o backfill do período
   inicial ausente usando `Locacao.data_entrada` (fonte da verdade); sem
   essa data, usa a data de criação do próprio Item como aproximação.

Sempre roda em modo simulação (dry-run) por padrão; use --aplicar para
gravar. Sempre gera um relatório .xlsx com o detalhe de cada mudança.
"""

import unicodedata
from collections import Counter
from pathlib import Path

import pandas as pd
from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = (
        "Preenche Locacao.data_entrada ausente nos itens recém-cadastrados (inferência lógica por "
        "modelo/fornecedor) e corrige o bug do campo 'congelado' abrindo o LocacaoPeriodo inicial "
        "que nunca existiu para itens locados ativos legados."
    )

    def add_arguments(self, parser):
        parser.add_argument("--aplicar", action="store_true", help="Grava as correções. Sem esta flag, só simula e gera o relatório.")
        parser.add_argument(
            "--marcador-novos", default="aplicar_analise_cadastral",
            help="Texto usado para identificar (via campo Observações) os itens recém-cadastrados "
                 "cujo data_entrada está ausente e cujo período deve ser realinhado.",
        )
        parser.add_argument("--relatorio", default=None, help="Caminho de saída do relatório .xlsx.")

    def handle(self, *args, **options):
        from ProjetoEstoque.models import Item, Locacao, LocacaoPeriodo

        aplicar = options["aplicar"]
        marcador = options["marcador_novos"]

        preenchidos, realinhados, avisos1 = self._preencher_data_entrada_e_realinhar(marcador, aplicar=aplicar)
        abertos, avisos3 = self._backfill_periodos_ausentes(aplicar=aplicar)

        avisos = avisos1 + avisos3

        caminho_relatorio = self._gravar_relatorio(
            options["relatorio"], preenchidos=preenchidos, realinhados=realinhados, abertos=abertos,
        )

        self._imprimir_resumo(
            aplicar=aplicar, preenchidos=preenchidos, realinhados=realinhados,
            abertos=abertos, avisos=avisos, caminho_relatorio=caminho_relatorio,
        )

    # ------------------------------------------------------------------ #
    # Fase 1+2 — preencher data_entrada ausente (itens recém-cadastrados) e
    # realinhar o período aberto correspondente na MESMA passada (em memória,
    # para o dry-run funcionar sem depender de uma escrita prévia no banco).
    # ------------------------------------------------------------------ #

    @transaction.atomic
    def _preencher_data_entrada_e_realinhar(self, marcador, *, aplicar):
        from ProjetoEstoque.models import Locacao, LocacaoPeriodo

        preenchidos = []
        realinhados = []
        avisos = []

        alvo = Locacao.objects.filter(
            equipamento__observacoes__icontains=marcador,
            data_entrada__isnull=True,
        ).select_related("equipamento", "equipamento__fornecedor")

        if not alvo.exists():
            return preenchidos, realinhados, avisos

        fornecedores = {loc.equipamento.fornecedor_id for loc in alvo if loc.equipamento.fornecedor_id}

        # Base de referência por fornecedor: locações já existentes (não deste lote) com data preenchida.
        base_por_modelo = {}
        base_global = {}
        for fornecedor_id in fornecedores:
            base_qs = (
                Locacao.objects
                .filter(equipamento__fornecedor_id=fornecedor_id, data_entrada__isnull=False)
                .exclude(equipamento__observacoes__icontains=marcador)
                .select_related("equipamento")
            )
            por_modelo = {}
            global_counter = Counter()
            for loc in base_qs:
                chave = self._normalizar(loc.equipamento.modelo)
                por_modelo.setdefault(chave, Counter())[loc.data_entrada] += 1
                global_counter[loc.data_entrada] += 1
            base_por_modelo[fornecedor_id] = por_modelo
            base_global[fornecedor_id] = global_counter.most_common(1)[0][0] if global_counter else None

        for loc in alvo:
            item = loc.equipamento
            fornecedor_id = item.fornecedor_id
            if not fornecedor_id or not base_global.get(fornecedor_id):
                avisos.append(f"[data_entrada] Item {item.pk} ({item.nome}): sem base de referência do fornecedor para inferir data — ignorado.")
                continue

            chave = self._normalizar(item.modelo)
            por_modelo = base_por_modelo[fornecedor_id]
            if chave in por_modelo:
                data_alvo = por_modelo[chave].most_common(1)[0][0]
                origem = "Modelo igual em outra locação do fornecedor"
            else:
                data_alvo = base_global[fornecedor_id]
                origem = "Data de entrada mais comum do fornecedor (sem modelo correspondente)"

            preenchidos.append({
                "item": item, "id": item.pk, "nome": item.nome, "modelo": item.modelo,
                "data_entrada": data_alvo, "origem": origem,
            })

            # Período aberto do mesmo item — item recém-criado tem exatamente
            # um período (aberto hoje pelo signal), então realinhar é seguro.
            periodo_aberto = LocacaoPeriodo.objects.filter(item=item, data_fim__isnull=True).first()
            if periodo_aberto and periodo_aberto.data_inicio != data_alvo:
                realinhados.append({
                    "item": item, "id": item.pk, "nome": item.nome,
                    "data_antiga": periodo_aberto.data_inicio, "data_nova": data_alvo,
                })

            if aplicar:
                loc.data_entrada = data_alvo
                loc.save(update_fields=["data_entrada", "updated_at"])
                if periodo_aberto and periodo_aberto.data_inicio != data_alvo:
                    periodo_aberto.data_inicio = data_alvo
                    periodo_aberto.valor_mensal = loc.valor_mensal
                    periodo_aberto.save(update_fields=["data_inicio", "valor_mensal", "updated_at"])

        return preenchidos, realinhados, avisos

    # ------------------------------------------------------------------ #
    # Fase 3 — backfill do período inicial ausente (bug do "congelado")
    # ------------------------------------------------------------------ #

    @transaction.atomic
    def _backfill_periodos_ausentes(self, *, aplicar):
        from ProjetoEstoque.models import Item, LocacaoPeriodo

        abertos = []
        avisos = []

        candidatos = (
            Item.objects
            .filter(locado="sim", status__in=["ativo", "backup", "manutencao"])
            .exclude(pk__in=LocacaoPeriodo.objects.values("item"))
            .select_related("locacao", "fornecedor")
        )

        for item in candidatos:
            locacao = getattr(item, "locacao", None)
            if locacao and locacao.data_entrada:
                data_inicio = locacao.data_entrada
                origem = "Locacao.data_entrada"
            else:
                data_inicio = item.created_at.date()
                origem = "Data de criação do Item (sem data_entrada disponível)"

            abertos.append({
                "item": item, "id": item.pk, "nome": item.nome,
                "fornecedor": item.fornecedor.nome if item.fornecedor else "-",
                "status": item.status, "data_inicio": data_inicio, "origem": origem,
            })

            if aplicar:
                LocacaoPeriodo.objects.create(
                    item=item,
                    valor_mensal=locacao.valor_mensal if locacao else None,
                    data_inicio=data_inicio,
                )

        return abertos, avisos

    # ------------------------------------------------------------------ #
    # Relatório
    # ------------------------------------------------------------------ #

    def _gravar_relatorio(self, caminho_opt, *, preenchidos, realinhados, abertos):
        destino = Path(caminho_opt) if caminho_opt else Path.home() / "Downloads" / "correcao_periodos_locacao_relatorio.xlsx"

        df_preenchidos = pd.DataFrame([
            {"ID": p["id"], "Nome": p["nome"], "Modelo": p["modelo"], "Data de Entrada Aplicada": p["data_entrada"], "Origem": p["origem"]}
            for p in preenchidos
        ])
        df_realinhados = pd.DataFrame([
            {"ID": r["id"], "Nome": r["nome"], "Data Antiga (período)": r["data_antiga"], "Data Nova (período)": r["data_nova"]}
            for r in realinhados
        ])
        df_abertos = pd.DataFrame([
            {"ID": a["id"], "Nome": a["nome"], "Fornecedor": a["fornecedor"], "Status": a["status"], "Data de Início do Período": a["data_inicio"], "Origem": a["origem"]}
            for a in abertos
        ])
        df_resumo = pd.DataFrame([
            {"Métrica": "Locação com data_entrada preenchida (itens recém-cadastrados)", "Quantidade": len(preenchidos)},
            {"Métrica": "Período aberto realinhado para a nova data_entrada", "Quantidade": len(realinhados)},
            {"Métrica": "Período inicial aberto (bug 'congelado' corrigido)", "Quantidade": len(abertos)},
        ])

        with pd.ExcelWriter(destino, engine="openpyxl") as writer:
            df_resumo.to_excel(writer, sheet_name="Resumo", index=False)
            df_preenchidos.to_excel(writer, sheet_name="Data entrada preenchida", index=False)
            df_realinhados.to_excel(writer, sheet_name="Periodo realinhado", index=False)
            df_abertos.to_excel(writer, sheet_name="Congelado corrigido", index=False)

        return destino

    def _imprimir_resumo(self, **kw):
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("=== CORREÇÃO DE PERÍODOS DE LOCAÇÃO ==="))
        self.stdout.write(f"Modo: {'APLICANDO' if kw['aplicar'] else 'SIMULAÇÃO (dry-run)'}")
        self.stdout.write(f"Data de entrada preenchida (itens recém-cadastrados): {len(kw['preenchidos'])}")
        self.stdout.write(f"Período aberto realinhado: {len(kw['realinhados'])}")
        self.stdout.write(f"Bug 'congelado' corrigido (período inicial aberto): {len(kw['abertos'])}")

        if kw["avisos"]:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING("=== AVISOS ==="))
            for aviso in kw["avisos"]:
                self.stdout.write(f" - {aviso}")

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"Relatório detalhado gravado em: {kw['caminho_relatorio']}"))

        if not kw["aplicar"]:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(
                "Nenhuma alteração foi gravada (modo simulação). Revise o relatório e rode de novo com --aplicar."
            ))

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _normalizar(self, valor):
        if not valor:
            return ""
        text = str(valor).strip().lower()
        text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
        return " ".join(text.split())
