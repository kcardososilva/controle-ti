"""
Aplica as correções indicadas por uma planilha de "Análise Cadastral"
(formato gerado pela comparação TI x Fornecedor — colunas Status, Numero de
Serie, Existe no Fornecedor, Existe no TI, Part Number/Modelo, Valor Mensal,
PMB, ID (TI), Nome do Item (TI), Detalhe da Divergencia).

Diferente do `reconciliar_planilha_fornecedor`, este comando NÃO lê a
planilha crua do fornecedor: ele consome o resultado já comparado (uma aba
"Analise Cadastral" com resumo + tabela detalhada a partir da linha 12), e
casa cada linha pelo `ID (TI)` (confirmando pelo número de série).

Regras de correção (recomputadas linha a linha — não confia cegamente no
`Status`/`Detalhe da Divergencia` da planilha, que tem uma falha conhecida
no campo PMB):

1. Modelo: se o item existe nos dois lados e o Part Number do fornecedor
   difere (ignorando maiúsc./espaços) do `Item.modelo`, atualiza para o
   valor do fornecedor — é o dado técnico mais preciso (quem manuseia o
   equipamento é o fornecedor).
2. Valor Mensal: se existe `Locacao` vinculada e o valor do fornecedor
   difere do `Locacao.valor_mensal`, atualiza para o valor do fornecedor —
   é quem fatura a locação, portanto fonte da verdade.
3. PMB: REGRA CORRIGIDA a pedido do usuário — a planilha de análise erra
   ao comparar apenas o texto bruto de PMB do fornecedor. A regra correta:
   - Se o nome do item (`Item.nome`) contém "PMB" -> força `pmb = "sim"`,
     independente do que o fornecedor/TI tinham antes.
   - Caso contrário, e havendo dado do fornecedor para aquele item,
     usa o valor do fornecedor (`"PMB"` -> sim, `"Nao"` -> não).
   - Itens que só existem no TI (não aparecem na planilha do fornecedor)
     só recebem a correção pela regra do nome — não há dado do fornecedor
     para usar como base.

Itens "Ausente no TI" (só existem na planilha do fornecedor) e "Ausente no
Fornecedor" (só existem no TI) só são criados/excluídos com as flags
--cadastrar-novos / --excluir-ausentes (cada uma exige --aplicar junto) —
são passos de maior impacto e risco, mantidos opt-in de propósito.

4. Cadastro (--cadastrar-novos): linhas "Ausente no TI" que são equipamento
   de verdade (não licença — ver item 5) viram Item (locado=sim) + Locacao,
   com os dados do fornecedor. Não há dado de localidade/nome descritivo na
   planilha de análise — o nome do item nasce igual ao Part Number e deve
   ser revisado manualmente depois (localidade, nome amigável).
5. Licenças (sempre separadas, nunca cadastradas como Item): linhas cujo
   Part Number bate um padrão de licença de software (prefixo "SPLA-" ou
   "LIC-", etc.) são gravadas numa planilha à parte para tratamento manual
   no módulo de Licenças — o modelo `Item` é para equipamento físico.
6. Exclusão (--excluir-ausentes): itens "Ausente no Fornecedor" (só existem
   no TI) são APAGADOS FISICAMENTE (não soft-delete) — o que cascateia
   Locação, Movimentações, Preventivas/Execuções/Respostas de checklist e
   histórico de status vinculados a cada item. Ação irreversível: o
   relatório grava o detalhe de tudo que seria/foi apagado ANTES da
   exclusão acontecer, para manter um registro fora do banco.

Sempre roda em modo simulação (dry-run) por padrão; use --aplicar para
gravar. Sempre gera um relatório .xlsx com o detalhe de cada categoria.
"""

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

# Prefixos de Part Number que indicam licença de software (não é Item físico).
PADRAO_LICENCA = re.compile(r"SPLA|^LIC[-_]|LICEN|SUBSCRI|SOFTWARE", re.IGNORECASE)


class Command(BaseCommand):
    help = (
        "Aplica as correções de uma planilha de Análise Cadastral (TI x Fornecedor) já comparada: "
        "corrige Modelo/Valor Mensal pelo dado do fornecedor e PMB pela regra correta (nome contém "
        "'PMB' -> sim; senão, dado do fornecedor). Cadastro/exclusão de itens só com flags explícitas."
    )

    def add_arguments(self, parser):
        parser.add_argument("--arquivo", required=True, help="Caminho da planilha de Análise Cadastral (.xlsx).")
        parser.add_argument("--aba", default="Analise Cadastral", help="Nome da aba com a tabela detalhada.")
        parser.add_argument("--fornecedor", required=True, help='Nome do fornecedor cadastrado (ex.: "Routerlink"), usado como conferência extra por item.')
        parser.add_argument("--aplicar", action="store_true", help="Grava as correções. Sem esta flag, só simula e gera o relatório.")
        parser.add_argument(
            "--cadastrar-novos", action="store_true",
            help="Só tem efeito junto com --aplicar. Cadastra como Item locado (+ Locacao) cada linha 'Ausente "
                 "no TI' que for equipamento (licenças são sempre separadas — ver --planilha-licencas).",
        )
        parser.add_argument(
            "--excluir-ausentes", action="store_true",
            help="Só tem efeito junto com --aplicar. Exclui FISICAMENTE (não soft-delete) os itens 'Ausente no "
                 "Fornecedor' (só existem no TI) — cascateia Locação/Movimentações/Preventivas/Histórico. "
                 "Irreversível: revise o relatório antes de usar.",
        )
        parser.add_argument("--usuario", default=None, help="Username para registrar como responsável pela alteração (auditoria).")
        parser.add_argument("--relatorio", default=None, help="Caminho de saída do relatório .xlsx. Padrão: ao lado do arquivo de entrada.")
        parser.add_argument("--planilha-licencas", default=None, help="Caminho de saída da planilha de licenças separadas. Padrão: ao lado do arquivo de entrada.")

    def handle(self, *args, **options):
        from ProjetoEstoque.models import Fornecedor

        caminho = Path(options["arquivo"])
        if not caminho.exists():
            raise CommandError(f"Arquivo não encontrado: {caminho}")

        aplicar = options["aplicar"]
        cadastrar_novos = options["cadastrar_novos"]
        excluir_ausentes = options["excluir_ausentes"]
        fornecedor_nome = options["fornecedor"].strip()

        usuario = None
        if options["usuario"]:
            usuario = User.objects.filter(username=options["usuario"]).first()
            if not usuario:
                raise CommandError(f"Usuário '{options['usuario']}' não encontrado.")

        fornecedor_obj = Fornecedor.objects.filter(nome__iexact=fornecedor_nome).first()
        if not fornecedor_obj:
            raise CommandError(f"Fornecedor '{fornecedor_nome}' não encontrado no cadastro.")

        df = self._ler_tabela(caminho, options["aba"])

        existe_ambos = df[(df["Existe no Fornecedor"] == True) & (df["Existe no TI"] == True)]  # noqa: E712
        # Só existe no TI (não aparece na planilha do fornecedor) = "Ausente no Fornecedor" -> candidato a revisão/exclusão.
        apenas_ti = df[(df["Existe no Fornecedor"] != True) & (df["Existe no TI"] == True)]  # noqa: E712
        # Só existe na planilha do fornecedor (não está cadastrado no TI) = "Ausente no TI" -> candidato a cadastro futuro.
        apenas_fornecedor = df[(df["Existe no Fornecedor"] == True) & (df["Existe no TI"] != True)]  # noqa: E712

        eh_licenca = apenas_fornecedor["Part Number / Modelo (Fornecedor)"].astype(str).str.contains(PADRAO_LICENCA)
        apenas_fornecedor_licencas = apenas_fornecedor[eh_licenca]
        apenas_fornecedor_equipamento = apenas_fornecedor[~eh_licenca]

        correcoes, sem_mudanca, avisos = self._processar_existentes_ambos(
            existe_ambos, fornecedor_nome=fornecedor_nome, aplicar=aplicar, usuario=usuario,
        )

        correcoes_pmb_apenas_ti, avisos_apenas_ti = self._processar_pmb_apenas_ti(
            apenas_ti, fornecedor_nome=fornecedor_nome, aplicar=aplicar, usuario=usuario,
        )
        avisos.extend(avisos_apenas_ti)

        cadastrados, avisos_cadastro = self._processar_cadastros(
            apenas_fornecedor_equipamento, fornecedor_obj=fornecedor_obj,
            aplicar=aplicar and cadastrar_novos, usuario=usuario,
        )
        avisos.extend(avisos_cadastro)

        excluidos, avisos_exclusao = self._processar_exclusoes(
            apenas_ti, fornecedor_nome=fornecedor_nome,
            aplicar=aplicar and excluir_ausentes, usuario=usuario,
        )
        avisos.extend(avisos_exclusao)

        caminho_licencas = self._gravar_planilha_licencas(options["planilha_licencas"], caminho, apenas_fornecedor_licencas)

        caminho_relatorio = self._gravar_relatorio(
            options["relatorio"], caminho,
            correcoes=correcoes,
            sem_mudanca=sem_mudanca,
            correcoes_pmb_apenas_ti=correcoes_pmb_apenas_ti,
            apenas_ti_revisar=apenas_ti,
            excluidos=excluidos,
            apenas_fornecedor_cadastro=apenas_fornecedor_equipamento,
            cadastrados=cadastrados,
            apenas_fornecedor_licencas=apenas_fornecedor_licencas,
        )

        self._imprimir_resumo(
            aplicar=aplicar,
            cadastrar_novos=cadastrar_novos,
            excluir_ausentes=excluir_ausentes,
            total_existe_ambos=len(existe_ambos),
            correcoes=correcoes,
            sem_mudanca=sem_mudanca,
            correcoes_pmb_apenas_ti=correcoes_pmb_apenas_ti,
            apenas_ti_total=len(apenas_ti),
            excluidos=excluidos,
            apenas_fornecedor_total=len(apenas_fornecedor_equipamento),
            cadastrados=cadastrados,
            licencas_total=len(apenas_fornecedor_licencas),
            avisos=avisos,
            caminho_relatorio=caminho_relatorio,
            caminho_licencas=caminho_licencas,
        )

    # ------------------------------------------------------------------ #
    # Leitura
    # ------------------------------------------------------------------ #

    def _ler_tabela(self, caminho, aba):
        bruto = pd.read_excel(caminho, sheet_name=aba, header=None)

        header_idx = None
        for idx in range(min(30, len(bruto))):
            valores = [self._normalize_header(v) for v in bruto.iloc[idx].tolist()]
            if "status" in valores and any("numero de serie" in v for v in valores):
                header_idx = idx
                break

        if header_idx is None:
            raise CommandError(
                f"Não encontrei a linha de cabeçalho (colunas 'Status' / 'Numero de Serie') na aba '{aba}'."
            )

        df = pd.read_excel(caminho, sheet_name=aba, header=header_idx)
        df.columns = [str(c).strip() for c in df.columns]

        obrigatorias = [
            "Status", "Numero de Serie", "Existe no Fornecedor", "Existe no TI",
            "Part Number / Modelo (Fornecedor)", "Valor Mensal (Fornecedor)",
            "PMB (Fornecedor)", "ID (TI)", "Nome do Item (TI)",
        ]
        faltando = [c for c in obrigatorias if c not in df.columns]
        if faltando:
            raise CommandError(f"Colunas ausentes na planilha: {', '.join(faltando)}")

        return df

    # ------------------------------------------------------------------ #
    # Itens que existem nos dois lados (correto + divergência, recomputado)
    # ------------------------------------------------------------------ #

    @transaction.atomic
    def _processar_existentes_ambos(self, df, *, fornecedor_nome, aplicar, usuario):
        from ProjetoEstoque.models import Item

        correcoes = []
        sem_mudanca = []
        avisos = []

        for _, row in df.iterrows():
            item_id = row.get("ID (TI)")
            if pd.isna(item_id):
                avisos.append(f"Linha com série {row.get('Numero de Serie')}: 'Existe no TI'=True mas sem ID (TI) — ignorada.")
                continue
            item_id = int(item_id)

            item = Item.objects.select_related("locacao", "fornecedor").filter(pk=item_id).first()
            if not item:
                avisos.append(f"ID (TI) {item_id} (série {row.get('Numero de Serie')}): não encontrado no banco — ignorado.")
                continue

            serie_planilha = self._normalizar_serie(row.get("Numero de Serie"))
            serie_db = self._normalizar_serie(item.numero_serie)
            if serie_planilha != serie_db:
                avisos.append(
                    f"ID (TI) {item_id}: número de série não bate (planilha={serie_planilha!r} / banco={serie_db!r}) — ignorado."
                )
                continue

            fornecedor_db = item.fornecedor.nome if item.fornecedor else None
            if not fornecedor_db or self._normalize_header(fornecedor_db) != self._normalize_header(fornecedor_nome):
                avisos.append(
                    f"ID (TI) {item_id} (série {serie_db}): fornecedor no banco é {fornecedor_db!r}, esperado {fornecedor_nome!r} — ignorado."
                )
                continue

            mudancas_item = {}

            # ── Modelo: fornecedor é a fonte da verdade ──
            modelo_forn = self._clean_text(row.get("Part Number / Modelo (Fornecedor)"))
            if modelo_forn and self._normalize_header(modelo_forn) != self._normalize_header(item.modelo or ""):
                mudancas_item["modelo"] = {"campo": "Modelo", "antigo": item.modelo, "novo": modelo_forn}

            # ── PMB: nome contém "PMB" -> sim; senão, valor do fornecedor ──
            pmb_alvo, pmb_motivo = self._calcular_pmb_alvo(item, row.get("PMB (Fornecedor)"))
            if pmb_alvo is not None and pmb_alvo != item.pmb:
                mudancas_item["pmb"] = {"campo": "PMB", "antigo": item.pmb, "novo": pmb_alvo, "motivo": pmb_motivo}

            # ── Valor Mensal (Locacao): fornecedor é a fonte da verdade ──
            locacao = getattr(item, "locacao", None)
            valor_forn = self._to_decimal(row.get("Valor Mensal (Fornecedor)"))
            mudanca_valor = None
            if locacao is not None and valor_forn is not None:
                valor_atual = locacao.valor_mensal if locacao.valor_mensal is not None else Decimal("0.00")
                if valor_atual != valor_forn:
                    mudanca_valor = {"campo": "Valor Mensal (Locação)", "antigo": valor_atual, "novo": valor_forn}
            elif locacao is None and valor_forn:
                avisos.append(f"ID (TI) {item_id} (série {serie_db}): sem Locação vinculada — valor mensal do fornecedor (R$ {valor_forn}) não pôde ser aplicado.")

            if not mudancas_item and not mudanca_valor:
                sem_mudanca.append({"item": item, "linha_serie": serie_db})
                continue

            registro = {
                "item": item,
                "id": item_id,
                "serie": serie_db,
                "nome": item.nome,
                "mudancas": list(mudancas_item.values()) + ([mudanca_valor] if mudanca_valor else []),
            }
            correcoes.append(registro)

            if aplicar:
                if "modelo" in mudancas_item:
                    item.modelo = mudancas_item["modelo"]["novo"]
                if "pmb" in mudancas_item:
                    item.pmb = mudancas_item["pmb"]["novo"]
                if usuario:
                    item.atualizado_por = usuario
                if "modelo" in mudancas_item or "pmb" in mudancas_item:
                    item.full_clean()
                    item.save()

                if mudanca_valor:
                    locacao.valor_mensal = mudanca_valor["novo"]
                    if usuario:
                        locacao.atualizado_por = usuario
                    locacao.full_clean()
                    locacao.save()

        return correcoes, sem_mudanca, avisos

    # ------------------------------------------------------------------ #
    # Itens só no TI (não aparecem na planilha do fornecedor): só PMB por nome
    # ------------------------------------------------------------------ #

    @transaction.atomic
    def _processar_pmb_apenas_ti(self, df, *, fornecedor_nome, aplicar, usuario):
        from ProjetoEstoque.models import Item

        correcoes = []
        avisos = []

        for _, row in df.iterrows():
            item_id = row.get("ID (TI)")
            if pd.isna(item_id):
                continue
            item_id = int(item_id)

            item = Item.objects.filter(pk=item_id).first()
            if not item:
                avisos.append(f"[Ausente no fornecedor] ID (TI) {item_id}: não encontrado no banco — ignorado.")
                continue

            fornecedor_db = item.fornecedor.nome if item.fornecedor else None
            if not fornecedor_db or self._normalize_header(fornecedor_db) != self._normalize_header(fornecedor_nome):
                continue

            if "PMB" not in (item.nome or "").upper():
                continue

            if item.pmb == "sim":
                continue

            correcoes.append({
                "item": item,
                "id": item_id,
                "serie": item.numero_serie,
                "nome": item.nome,
                "mudancas": [{"campo": "PMB", "antigo": item.pmb, "novo": "sim", "motivo": "Nome contém 'PMB'"}],
            })

            if aplicar:
                item.pmb = "sim"
                if usuario:
                    item.atualizado_por = usuario
                item.full_clean()
                item.save()

        return correcoes, avisos

    # ------------------------------------------------------------------ #
    # Cadastro dos "Ausente no TI" que são equipamento (não licença)
    # ------------------------------------------------------------------ #

    @transaction.atomic
    def _processar_cadastros(self, df, *, fornecedor_obj, aplicar, usuario):
        from ProjetoEstoque.models import Item, Locacao

        cadastrados = []
        avisos = []

        for _, row in df.iterrows():
            serie = self._normalizar_serie(row.get("Numero de Serie"))
            modelo = self._clean_text(row.get("Part Number / Modelo (Fornecedor)"))
            valor_mensal = self._to_decimal(row.get("Valor Mensal (Fornecedor)"))

            if not serie:
                avisos.append(f"[Cadastro] Linha sem número de série (Part Number={modelo!r}) — ignorada.")
                continue

            if Item.all_objects.filter(numero_serie=serie).exists():
                avisos.append(f"[Cadastro] Série {serie} já existe no banco (não deveria — recheque a análise) — ignorada.")
                continue

            nome = (modelo or f"Equipamento {fornecedor_obj.nome} S/N {serie}")[:100]
            pmb_alvo = "sim" if "PMB" in nome.upper() else self._map_pmb_fornecedor(row.get("PMB (Fornecedor)"))

            registro = {
                "serie": serie, "nome": nome, "modelo": modelo,
                "valor_mensal": valor_mensal, "pmb": pmb_alvo, "item": None,
            }
            cadastrados.append(registro)

            if not aplicar:
                continue

            item = Item(
                nome=nome,
                modelo=modelo,
                numero_serie=serie,
                fornecedor=fornecedor_obj,
                locado="sim",
                status="ativo",
                pmb=pmb_alvo or "nao",
                observacoes=(
                    "Cadastrado via aplicar_analise_cadastral (planilha de análise TI x Fornecedor) — "
                    "linha só existia na planilha do fornecedor. Revisar localidade/centro de custo/nome."
                ),
            )
            if usuario:
                item.criado_por = usuario
                item.atualizado_por = usuario
            item.full_clean()
            item.save()

            locacao = Locacao(
                equipamento=item,
                valor_mensal=valor_mensal,
                fornecedor=fornecedor_obj,
                observacoes=f"Cadastrado via aplicar_analise_cadastral. Série {serie}.",
            )
            if usuario:
                locacao.criado_por = usuario
                locacao.atualizado_por = usuario
            locacao.full_clean()
            locacao.save()

            registro["item"] = item

        return cadastrados, avisos

    # ------------------------------------------------------------------ #
    # Exclusão física dos "Ausente no Fornecedor" (só existem no TI)
    # ------------------------------------------------------------------ #

    @transaction.atomic
    def _processar_exclusoes(self, df, *, fornecedor_nome, aplicar, usuario):
        from ProjetoEstoque.models import Item, MovimentacaoItem, Preventiva

        excluidos = []
        avisos = []

        ids = []
        for _, row in df.iterrows():
            item_id = row.get("ID (TI)")
            if pd.isna(item_id):
                continue
            ids.append(int(item_id))

        itens = list(
            Item.all_objects
            .select_related("locacao", "fornecedor")
            .filter(pk__in=ids)
        )
        encontrados = {it.pk for it in itens}
        for item_id in ids:
            if item_id not in encontrados:
                avisos.append(f"[Exclusão] ID (TI) {item_id}: não encontrado no banco — ignorado.")

        for item in itens:
            fornecedor_db = item.fornecedor.nome if item.fornecedor else None
            if not fornecedor_db or self._normalize_header(fornecedor_db) != self._normalize_header(fornecedor_nome):
                avisos.append(f"[Exclusão] ID {item.pk}: fornecedor no banco é {fornecedor_db!r}, esperado {fornecedor_nome!r} — ignorado.")
                continue

            locacao = getattr(item, "locacao", None)
            movs = list(MovimentacaoItem.objects.filter(item=item).order_by("-created_at"))
            prevs = list(Preventiva.objects.filter(equipamento=item))

            excluidos.append({
                "item": item,
                "id": item.pk,
                "nome": item.nome,
                "serie": item.numero_serie,
                "modelo": item.modelo,
                "status": item.status,
                "locacao_valor_mensal": locacao.valor_mensal if locacao else None,
                "locacao_contrato": locacao.contrato if locacao else None,
                "qtd_movimentacoes": len(movs),
                "qtd_preventivas": len(prevs),
                "movimentacoes": movs,
            })

        if aplicar and excluidos:
            Item.all_objects.filter(pk__in=[e["id"] for e in excluidos]).delete()

        return excluidos, avisos

    # ------------------------------------------------------------------ #
    # Planilha de licenças (separadas — nunca viram Item)
    # ------------------------------------------------------------------ #

    def _gravar_planilha_licencas(self, caminho_opt, caminho_entrada, df_licencas):
        if caminho_opt:
            destino = Path(caminho_opt)
        else:
            destino = caminho_entrada.with_name(caminho_entrada.stem + "_licencas_para_resolver.xlsx")

        colunas = ["Numero de Serie", "Part Number / Modelo (Fornecedor)", "Valor Mensal (Fornecedor)", "PMB (Fornecedor)"]
        df_saida = df_licencas[colunas].copy() if not df_licencas.empty else pd.DataFrame(columns=colunas)
        df_saida = df_saida.rename(columns={
            "Numero de Serie": "Número/Chave de Licença",
            "Part Number / Modelo (Fornecedor)": "SKU / Part Number",
            "Valor Mensal (Fornecedor)": "Valor Mensal (R$)",
            "PMB (Fornecedor)": "PMB (Fornecedor)",
        })

        nota = pd.DataFrame([{
            "Aviso": (
                "Linhas da planilha de análise cadastral que existem só no fornecedor e são LICENÇA DE SOFTWARE "
                "(SKU com padrão SPLA-/LIC- etc.), não equipamento físico — por isso não foram cadastradas como "
                "Item. Resolver manualmente pelo módulo de Licenças."
            )
        }])

        with pd.ExcelWriter(destino, engine="openpyxl") as writer:
            nota.to_excel(writer, sheet_name="Licenças", index=False)
            df_saida.to_excel(writer, sheet_name="Licenças", index=False, startrow=3)

        return destino

    # ------------------------------------------------------------------ #
    # Regras
    # ------------------------------------------------------------------ #

    def _map_pmb_fornecedor(self, pmb_fornecedor_raw):
        texto = self._clean_text(pmb_fornecedor_raw)
        if not texto:
            return "nao"
        norm = self._normalize_header(texto)
        if norm == "pmb":
            return "sim"
        return "nao"

    def _calcular_pmb_alvo(self, item, pmb_fornecedor_raw):
        if "PMB" in (item.nome or "").upper():
            return "sim", "Nome contém 'PMB'"

        pmb_forn_texto = self._clean_text(pmb_fornecedor_raw)
        if not pmb_forn_texto:
            return None, None

        pmb_forn_norm = self._normalize_header(pmb_forn_texto)
        if pmb_forn_norm == "pmb":
            return "sim", "Fornecedor classifica como PMB"
        if pmb_forn_norm in {"nao", "não"}:
            return "nao", "Fornecedor classifica como Não-PMB"

        return None, None

    # ------------------------------------------------------------------ #
    # Relatório
    # ------------------------------------------------------------------ #

    def _gravar_relatorio(
        self, caminho_opt, caminho_entrada, *,
        correcoes, sem_mudanca, correcoes_pmb_apenas_ti, apenas_ti_revisar,
        excluidos, apenas_fornecedor_cadastro, cadastrados, apenas_fornecedor_licencas,
    ):
        if caminho_opt:
            destino = Path(caminho_opt)
        else:
            destino = caminho_entrada.with_name(caminho_entrada.stem + "_relatorio_aplicacao.xlsx")

        linhas_correcoes = []
        for r in correcoes:
            for m in r["mudancas"]:
                linhas_correcoes.append({
                    "ID": r["id"],
                    "Nome": r["nome"],
                    "Número de Série": r["serie"],
                    "Campo": m["campo"],
                    "Valor Antigo": m["antigo"],
                    "Valor Novo": m["novo"],
                    "Motivo (PMB)": m.get("motivo", ""),
                })
        df_correcoes = pd.DataFrame(linhas_correcoes)

        df_sem_mudanca = pd.DataFrame([
            {"ID": r["item"].pk, "Nome": r["item"].nome, "Número de Série": r["linha_serie"]}
            for r in sem_mudanca
        ])

        linhas_pmb_apenas_ti = []
        for r in correcoes_pmb_apenas_ti:
            for m in r["mudancas"]:
                linhas_pmb_apenas_ti.append({
                    "ID": r["id"], "Nome": r["nome"], "Número de Série": r["serie"],
                    "Campo": m["campo"], "Valor Antigo": m["antigo"], "Valor Novo": m["novo"], "Motivo": m["motivo"],
                })
        df_pmb_apenas_ti = pd.DataFrame(linhas_pmb_apenas_ti)

        # Detalhe completo do que foi/seria fisicamente apagado — registro fora do banco,
        # já que depois da exclusão os dados originais não existem mais.
        linhas_excluidos = []
        for e in excluidos:
            linhas_excluidos.append({
                "ID": e["id"], "Nome": e["nome"], "Número de Série": e["serie"], "Modelo": e["modelo"],
                "Status": e["status"], "Valor Mensal (Locação)": e["locacao_valor_mensal"],
                "Contrato": e["locacao_contrato"], "Qtd. Movimentações apagadas": e["qtd_movimentacoes"],
                "Qtd. Preventivas apagadas": e["qtd_preventivas"],
            })
        df_excluidos = pd.DataFrame(linhas_excluidos)

        linhas_mov_excluidas = []
        for e in excluidos:
            for mov in e["movimentacoes"]:
                linhas_mov_excluidas.append({
                    "Item ID": e["id"], "Item Nome": e["nome"],
                    "Movimentação ID": mov.pk,
                    "Tipo": mov.get_tipo_movimentacao_display(),
                    "Data": timezone.localtime(mov.created_at).replace(tzinfo=None) if mov.created_at else None,
                    "Custo": mov.custo,
                })
        df_mov_excluidas = pd.DataFrame(linhas_mov_excluidas)

        df_cadastrados = pd.DataFrame([
            {
                "Número de Série": c["serie"], "Nome": c["nome"], "Modelo": c["modelo"],
                "Valor Mensal": c["valor_mensal"], "PMB": c["pmb"],
                "Item criado (ID)": c["item"].pk if c["item"] else "",
            }
            for c in cadastrados
        ])

        df_apenas_ti = apenas_ti_revisar[[
            "Numero de Serie", "ID (TI)", "Nome do Item (TI)", "Modelo (TI)", "Valor Mensal (TI)", "PMB (TI)",
        ]].copy() if not apenas_ti_revisar.empty else pd.DataFrame()

        df_apenas_fornecedor = apenas_fornecedor_cadastro[[
            "Numero de Serie", "Part Number / Modelo (Fornecedor)", "Valor Mensal (Fornecedor)", "PMB (Fornecedor)",
        ]].copy() if not apenas_fornecedor_cadastro.empty else pd.DataFrame()

        df_licencas = apenas_fornecedor_licencas[[
            "Numero de Serie", "Part Number / Modelo (Fornecedor)", "Valor Mensal (Fornecedor)", "PMB (Fornecedor)",
        ]].copy() if not apenas_fornecedor_licencas.empty else pd.DataFrame()

        df_resumo = pd.DataFrame([
            {"Métrica": "Itens corrigidos (existem nos 2 lados)", "Quantidade": len(correcoes)},
            {"Métrica": "Campos corrigidos no total", "Quantidade": len(linhas_correcoes)},
            {"Métrica": "Itens batidos sem nenhuma mudança necessária", "Quantidade": len(sem_mudanca)},
            {"Métrica": "PMB corrigido só pelo nome (item ausente na planilha do fornecedor)", "Quantidade": len(correcoes_pmb_apenas_ti)},
            {"Métrica": "Ausente no fornecedor — excluídos/candidatos a exclusão física", "Quantidade": len(excluidos)},
            {"Métrica": "  ...movimentações de estoque junto", "Quantidade": len(linhas_mov_excluidas)},
            {"Métrica": "Ausente no TI (equipamento) — cadastrados/candidatos a cadastro", "Quantidade": len(cadastrados)},
            {"Métrica": "Ausente no TI (licença de software) — separado, NÃO cadastrado como Item", "Quantidade": len(apenas_fornecedor_licencas)},
        ])

        with pd.ExcelWriter(destino, engine="openpyxl") as writer:
            df_resumo.to_excel(writer, sheet_name="Resumo", index=False)
            df_correcoes.to_excel(writer, sheet_name="Correcoes aplicadas", index=False)
            df_sem_mudanca.to_excel(writer, sheet_name="Sem mudanca", index=False)
            df_pmb_apenas_ti.to_excel(writer, sheet_name="PMB so por nome", index=False)
            df_excluidos.to_excel(writer, sheet_name="Excluidos (detalhe)", index=False)
            df_mov_excluidas.to_excel(writer, sheet_name="Movimentacoes excluidas", index=False)
            df_cadastrados.to_excel(writer, sheet_name="Cadastrados", index=False)
            df_licencas.to_excel(writer, sheet_name="Licencas (nao cadastradas)", index=False)
            df_apenas_ti.to_excel(writer, sheet_name="Ausente no fornecedor (raw)", index=False)
            df_apenas_fornecedor.to_excel(writer, sheet_name="Ausente no TI - equip (raw)", index=False)

        return destino

    def _imprimir_resumo(self, **kw):
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("=== APLICAÇÃO DA ANÁLISE CADASTRAL ==="))
        self.stdout.write(f"Modo: {'APLICANDO' if kw['aplicar'] else 'SIMULAÇÃO (dry-run)'}")
        self.stdout.write(f"Itens existentes nos dois lados analisados: {kw['total_existe_ambos']}")
        total_campos = sum(len(r["mudancas"]) for r in kw["correcoes"])
        self.stdout.write(f"Itens corrigidos: {len(kw['correcoes'])}  (total de {total_campos} campos alterados)")
        self.stdout.write(f"Itens batidos sem mudança necessária: {len(kw['sem_mudanca'])}")
        self.stdout.write(f"PMB corrigido só pelo nome (item não está na planilha do fornecedor): {len(kw['correcoes_pmb_apenas_ti'])}")

        total_movs = sum(e["qtd_movimentacoes"] for e in kw["excluidos"])
        total_prevs = sum(e["qtd_preventivas"] for e in kw["excluidos"])
        self.stdout.write(
            f"Ausente no fornecedor (só no TI): {kw['apenas_ti_total']}"
            + (
                f" — {len(kw['excluidos'])} EXCLUÍDOS agora (+{total_movs} movimentações, {total_prevs} preventivas em cascata)"
                if kw["excluir_ausentes"] and kw["aplicar"]
                else " — nenhum excluído (use --excluir-ausentes junto com --aplicar)"
            )
        )
        criados_agora = [c for c in kw["cadastrados"] if c["item"] is not None]
        self.stdout.write(
            f"Ausente no TI, equipamento: {kw['apenas_fornecedor_total']}"
            + (
                f" — {len(criados_agora)} CADASTRADOS agora"
                if kw["cadastrar_novos"] and kw["aplicar"]
                else " — nenhum cadastrado (use --cadastrar-novos junto com --aplicar)"
            )
        )
        self.stdout.write(f"Ausente no TI, licença de software (separado — NUNCA vira Item): {kw['licencas_total']}")

        if kw["avisos"]:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING("=== AVISOS ==="))
            for aviso in kw["avisos"]:
                self.stdout.write(f" - {aviso}")

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"Relatório detalhado gravado em: {kw['caminho_relatorio']}"))
        self.stdout.write(self.style.SUCCESS(f"Planilha de licenças gravada em: {kw['caminho_licencas']}"))

        if not kw["aplicar"]:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(
                "Nenhuma alteração foi gravada (modo simulação). Revise o relatório e rode de novo com --aplicar."
            ))

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _normalize_header(self, value):
        if value is None:
            return ""
        text = str(value).strip().lower()
        text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
        return " ".join(text.split())

    def _clean_text(self, value):
        if value is None:
            return None
        try:
            if pd.isna(value):
                return None
        except Exception:
            pass
        text = str(value).strip()
        return text or None

    def _normalizar_serie(self, valor):
        text = self._clean_text(valor)
        if not text:
            return None
        return text.strip().upper()

    def _to_decimal(self, value):
        if value is None:
            return None
        try:
            if pd.isna(value):
                return None
        except Exception:
            pass

        if isinstance(value, Decimal):
            return value.quantize(Decimal("0.01"))
        if isinstance(value, (int, float)):
            return Decimal(str(value)).quantize(Decimal("0.01"))

        text = str(value).strip().replace("R$", "").replace(" ", "")
        if not text:
            return None
        if "," in text and "." in text:
            text = text.replace(".", "").replace(",", ".")
        elif "," in text:
            text = text.replace(",", ".")

        try:
            return Decimal(text).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            return None
