"""
Reconcilia o parque locado de um fornecedor com uma planilha de referência
enviada por ele (ex.: "relação de itens locados" da Routerlink), casando
cada linha com o item já cadastrado pelo número de série (chave única).

Três ações independentes, cada uma atrás da sua própria flag:
1. Bateu por número de série → corrige modelo/valor mensal (Locacao) se
   houver diferença.
2. Item cadastrado do fornecedor que não aparece mais na planilha →
   candidato a exclusão lógica (soft delete, `--excluir-nao-encontrados`).
3. Linha da planilha sem item correspondente no sistema → candidato a
   cadastro como novo Item locado + Locacao (`--cadastrar-novos`).

Sempre roda em modo simulação (dry-run) por padrão e sempre gera um
relatório .xlsx com o detalhe de cada categoria. Use --aplicar para gravar
as correções de modelo/valor mensal; --excluir-nao-encontrados e
--cadastrar-novos exigem --aplicar junto e são passos separados de
propósito, dado o impacto de cada um.
"""

import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone


class Command(BaseCommand):
    help = (
        "Reconcilia itens locados de um fornecedor com a planilha de referência enviada por ele: "
        "corrige modelo/valor mensal dos que baterem por número de série, marca como excluídos "
        "(soft delete) os que não aparecerem mais na planilha e cadastra os que só existem na planilha."
    )

    ALIASES = {
        "MODELO": ["Part Number", "PART NUMBER", "Modelo", "MODELO"],
        "SERIE": [
            "Serial", "SERIE", "SÉRIE",
            "Número de Série", "NÚMERO DE SÉRIE", "Numero de Serie",
            "Nº Série", "N Série", "NS",
        ],
        "VALOR_MENSAL": ["Valor Mensal", "VALOR MENSAL", "Mensalidade"],
        "NFE": ["NFe", "NF", "Nota Fiscal", "Número NF", "Numero NF"],
        "PMB": ["PMB", "PMB?"],
    }

    def add_arguments(self, parser):
        parser.add_argument("--arquivo", required=True, help="Caminho do .xlsx enviado pelo fornecedor.")
        parser.add_argument("--fornecedor", required=True, help='Nome do fornecedor cadastrado (ex.: "Routerlink").')
        parser.add_argument(
            "--aplicar", action="store_true",
            help="Grava as correções de modelo/valor mensal dos itens que bateram por número de série. "
                 "Sem esta flag, o comando só simula e gera o relatório.",
        )
        parser.add_argument(
            "--excluir-nao-encontrados", action="store_true",
            help="Só tem efeito junto com --aplicar. Marca como excluídos (soft delete) os itens do "
                 "fornecedor que não apareceram na planilha. Revise o relatório antes de usar.",
        )
        parser.add_argument(
            "--cadastrar-novos", action="store_true",
            help="Só tem efeito junto com --aplicar. Cadastra como Item locado (+ Locacao) cada linha da "
                 "planilha cujo número de série não bate com nenhum item já cadastrado do fornecedor.",
        )
        parser.add_argument("--usuario", default=None, help="Username para registrar como responsável pela alteração (auditoria).")
        parser.add_argument("--relatorio", default=None, help="Caminho de saída do relatório .xlsx. Padrão: ao lado do arquivo de entrada.")

    def handle(self, *args, **options):
        from ProjetoEstoque.models import Fornecedor, Item

        caminho = Path(options["arquivo"])
        if not caminho.exists():
            raise CommandError(f"Arquivo não encontrado: {caminho}")

        aplicar = options["aplicar"]
        excluir_nao_encontrados = options["excluir_nao_encontrados"]
        cadastrar_novos = options["cadastrar_novos"]
        fornecedor_nome = options["fornecedor"].strip()

        usuario = None
        if options["usuario"]:
            usuario = User.objects.filter(username=options["usuario"]).first()
            if not usuario:
                raise CommandError(f"Usuário '{options['usuario']}' não encontrado.")

        fornecedor_obj = Fornecedor.objects.filter(nome__iexact=fornecedor_nome).first()
        if not fornecedor_obj:
            raise CommandError(f"Fornecedor '{fornecedor_nome}' não encontrado no cadastro.")

        sheet_map, avisos_planilha = self._ler_planilha(caminho)
        if not sheet_map:
            raise CommandError("Nenhuma linha com número de série válido foi encontrada na planilha.")

        qs_escopo = Item.objects.filter(fornecedor__nome__iexact=fornecedor_nome, locado="sim")
        total_escopo = qs_escopo.count()
        if total_escopo == 0:
            raise CommandError(
                f"Nenhum item locado encontrado para o fornecedor '{fornecedor_nome}'. Confira o nome cadastrado."
            )

        db_map = {}
        sem_serie_db = []
        for item in qs_escopo.select_related("locacao"):
            chave = self._normalizar_serie(item.numero_serie)
            if not chave:
                sem_serie_db.append(item)
                continue
            db_map[chave] = item

        chaves_planilha = set(sheet_map.keys())
        chaves_db = set(db_map.keys())

        chaves_batidas = chaves_planilha & chaves_db
        chaves_so_planilha = chaves_planilha - chaves_db
        chaves_so_db = chaves_db - chaves_planilha

        atualizados, sem_mudanca = self._processar_atualizacoes(
            chaves_batidas, sheet_map, db_map, aplicar=aplicar, usuario=usuario,
        )

        excluidos = []
        if chaves_so_db:
            excluidos = self._processar_exclusoes(
                [db_map[k] for k in chaves_so_db],
                aplicar=aplicar and excluir_nao_encontrados,
                usuario=usuario,
            )

        so_planilha_rows = [sheet_map[k] for k in chaves_so_planilha]

        cadastrados = self._processar_cadastros(
            so_planilha_rows,
            fornecedor=fornecedor_obj,
            aplicar=aplicar and cadastrar_novos,
            usuario=usuario,
        )

        caminho_relatorio = self._gravar_relatorio(
            options["relatorio"], caminho, atualizados, sem_mudanca,
            [db_map[k] for k in chaves_so_db], cadastrados, sem_serie_db,
        )

        self._imprimir_resumo(
            fornecedor_nome=fornecedor_nome,
            total_escopo=total_escopo,
            total_planilha=len(sheet_map),
            atualizados=atualizados,
            sem_mudanca=sem_mudanca,
            candidatos_exclusao=len(chaves_so_db),
            excluidos=excluidos,
            so_planilha=len(chaves_so_planilha),
            cadastrados=cadastrados,
            sem_serie_db=len(sem_serie_db),
            avisos_planilha=avisos_planilha,
            aplicar=aplicar,
            excluir_nao_encontrados=excluir_nao_encontrados,
            cadastrar_novos=cadastrar_novos,
            caminho_relatorio=caminho_relatorio,
        )

    # ------------------------------------------------------------------ #
    # Leitura da planilha
    # ------------------------------------------------------------------ #

    def _ler_planilha(self, caminho):
        xl = pd.ExcelFile(caminho)
        sheet_map = {}
        avisos = []

        for nome_aba in xl.sheet_names:
            bruto = xl.parse(nome_aba, header=None)
            header_idx = self._localizar_linha_cabecalho(bruto)

            if header_idx is None:
                avisos.append(f"Aba '{nome_aba}': não encontrei uma coluna de número de série, aba ignorada.")
                continue

            df = xl.parse(nome_aba, header=header_idx)
            df.columns = [str(c).strip() for c in df.columns]

            col_modelo = self._find_column(df.columns, self.ALIASES["MODELO"])
            col_serie = self._find_column(df.columns, self.ALIASES["SERIE"])
            col_valor = self._find_column(df.columns, self.ALIASES["VALOR_MENSAL"])
            col_nfe = self._find_column(df.columns, self.ALIASES["NFE"])
            col_pmb = self._find_column(df.columns, self.ALIASES["PMB"])

            if not col_serie:
                avisos.append(f"Aba '{nome_aba}': coluna de número de série não encontrada, aba ignorada.")
                continue

            for idx, row in df.iterrows():
                linha_planilha = f"{nome_aba}!{header_idx + idx + 2}"

                serie_bruta = self._clean_text(row.get(col_serie))
                modelo = self._clean_text(row.get(col_modelo)) if col_modelo else None
                valor_mensal = self._to_decimal(row.get(col_valor)) if col_valor else None
                nfe = self._clean_text(row.get(col_nfe)) if col_nfe else None
                pmb = self._clean_text(row.get(col_pmb)) if col_pmb else None

                chave = self._normalizar_serie(serie_bruta)
                if not chave:
                    continue

                if chave in sheet_map:
                    avisos.append(
                        f"Linha {linha_planilha}: número de série duplicado na planilha "
                        f"({serie_bruta}), mantida a primeira ocorrência."
                    )
                    continue

                sheet_map[chave] = {
                    "linha": linha_planilha,
                    "aba": nome_aba,
                    "serie": serie_bruta,
                    "modelo": modelo,
                    "valor_mensal": valor_mensal,
                    "nfe": nfe,
                    "pmb": pmb,
                }

        return sheet_map, avisos

    def _localizar_linha_cabecalho(self, bruto, limite_linhas=10):
        alvo = self._normalize_header(self.ALIASES["SERIE"][0])
        for idx in range(min(limite_linhas, len(bruto))):
            valores = [self._normalize_header(v) for v in bruto.iloc[idx].tolist()]
            if any(alvo in v or v in {self._normalize_header(a) for a in self.ALIASES["SERIE"]} for v in valores):
                return idx
        return None

    # ------------------------------------------------------------------ #
    # Aplicação das mudanças
    # ------------------------------------------------------------------ #

    @transaction.atomic
    def _processar_atualizacoes(self, chaves_batidas, sheet_map, db_map, *, aplicar, usuario):
        atualizados = []
        sem_mudanca = []

        for chave in chaves_batidas:
            item = db_map[chave]
            linha = sheet_map[chave]

            mudou_modelo = bool(linha["modelo"]) and (item.modelo or "").strip().lower() != linha["modelo"].strip().lower()
            locacao = getattr(item, "locacao", None)
            mudou_valor = (
                locacao is not None
                and linha["valor_mensal"] is not None
                and (locacao.valor_mensal or Decimal("0")) != linha["valor_mensal"]
            )

            if not mudou_modelo and not mudou_valor:
                sem_mudanca.append({"item": item, "linha": linha})
                continue

            registro = {
                "item": item,
                "linha": linha,
                "modelo_antigo": item.modelo,
                "modelo_novo": linha["modelo"] if mudou_modelo else item.modelo,
                "valor_antigo": locacao.valor_mensal if locacao else None,
                "valor_novo": linha["valor_mensal"] if mudou_valor else (locacao.valor_mensal if locacao else None),
                "sem_locacao": locacao is None,
            }
            atualizados.append(registro)

            if aplicar:
                if mudou_modelo:
                    item.modelo = linha["modelo"]
                if usuario:
                    item.atualizado_por = usuario
                item.save(update_fields=["modelo", "atualizado_por", "updated_at"] if usuario else ["modelo", "updated_at"])

                if mudou_valor and locacao is not None:
                    locacao.valor_mensal = linha["valor_mensal"]
                    if usuario:
                        locacao.atualizado_por = usuario
                    locacao.save()

        return atualizados, sem_mudanca

    @transaction.atomic
    def _processar_cadastros(self, rows, *, fornecedor, aplicar, usuario):
        from ProjetoEstoque.models import Item, Locacao

        registros = []

        for linha in rows:
            registro = {"linha": linha, "item": None}
            registros.append(registro)

            if not aplicar:
                continue

            modelo = linha["modelo"]
            nome = (modelo or f"Equipamento {fornecedor.nome} S/N {linha['serie']}")[:100]

            item = Item(
                nome=nome,
                modelo=modelo,
                numero_serie=linha["serie"],
                fornecedor=fornecedor,
                locado="sim",
                status="ativo",
                pmb="sim" if linha.get("pmb") else "nao",
            )
            if usuario:
                item.criado_por = usuario
                item.atualizado_por = usuario
            item.full_clean()
            item.save()

            observacoes = f"Cadastrado via reconciliação de planilha do fornecedor {fornecedor.nome}. Linha {linha['linha']}."
            if linha.get("nfe"):
                observacoes += f" NFe: {linha['nfe']}."

            locacao = Locacao(
                equipamento=item,
                valor_mensal=linha["valor_mensal"],
                fornecedor=fornecedor,
                observacoes=observacoes,
            )
            if usuario:
                locacao.criado_por = usuario
                locacao.atualizado_por = usuario
            locacao.full_clean()
            locacao.save()

            registro["item"] = item

        return registros

    @transaction.atomic
    def _processar_exclusoes(self, itens, *, aplicar, usuario):
        excluidos = []
        agora = timezone.now()

        for item in itens:
            excluidos.append(item)
            if aplicar:
                item.excluido = True
                item.excluido_em = agora
                item.excluido_por = usuario
                item.save(update_fields=["excluido", "excluido_em", "excluido_por"])

        return excluidos

    # ------------------------------------------------------------------ #
    # Relatório
    # ------------------------------------------------------------------ #

    def _gravar_relatorio(self, caminho_opt, caminho_entrada, atualizados, sem_mudanca, so_db, cadastrados, sem_serie_db):
        if caminho_opt:
            destino = Path(caminho_opt)
        else:
            destino = caminho_entrada.with_name(caminho_entrada.stem + "_relatorio_reconciliacao.xlsx")

        def linha_item(item, extra=None):
            base = {
                "ID": item.pk,
                "Nome": item.nome,
                "Modelo atual": item.modelo,
                "Número de série": item.numero_serie,
                "Localidade": str(item.localidade) if item.localidade_id else "",
                "Status": item.status,
            }
            if extra:
                base.update(extra)
            return base

        df_atualizados = pd.DataFrame([
            linha_item(r["item"], {
                "Modelo novo": r["modelo_novo"],
                "Valor mensal atual": r["valor_antigo"],
                "Valor mensal novo": r["valor_novo"],
                "Sem registro de Locação?": "Sim" if r["sem_locacao"] else "Não",
                "Linha planilha": r["linha"]["linha"],
            })
            for r in atualizados
        ])

        df_sem_mudanca = pd.DataFrame([
            linha_item(r["item"], {"Linha planilha": r["linha"]["linha"]})
            for r in sem_mudanca
        ])

        df_so_db = pd.DataFrame([linha_item(item) for item in so_db])

        df_cadastrados = pd.DataFrame([
            {
                "Aba": r["linha"]["aba"],
                "Linha planilha": r["linha"]["linha"],
                "Modelo": r["linha"]["modelo"],
                "Número de série": r["linha"]["serie"],
                "Valor mensal": r["linha"]["valor_mensal"],
                "NFe": r["linha"]["nfe"],
                "Item criado (ID)": r["item"].pk if r["item"] else "",
            }
            for r in cadastrados
        ])

        df_sem_serie = pd.DataFrame([linha_item(item) for item in sem_serie_db])

        df_resumo = pd.DataFrame([
            {"Métrica": "Itens atualizados (modelo/valor mensal)", "Quantidade": len(atualizados)},
            {"Métrica": "Itens batidos sem mudança", "Quantidade": len(sem_mudanca)},
            {"Métrica": "Itens só no sistema (candidatos a exclusão)", "Quantidade": len(so_db)},
            {"Métrica": "Linhas só na planilha (cadastradas/candidatas a cadastro)", "Quantidade": len(cadastrados)},
            {"Métrica": "Itens no sistema sem número de série (fora da reconciliação automática)", "Quantidade": len(sem_serie_db)},
        ])

        with pd.ExcelWriter(destino, engine="openpyxl") as writer:
            df_resumo.to_excel(writer, sheet_name="Resumo", index=False)
            df_atualizados.to_excel(writer, sheet_name="Atualizados", index=False)
            df_sem_mudanca.to_excel(writer, sheet_name="Sem mudanca", index=False)
            df_so_db.to_excel(writer, sheet_name="So no sistema (excluir)", index=False)
            df_cadastrados.to_excel(writer, sheet_name="So na planilha (cadastro)", index=False)
            df_sem_serie.to_excel(writer, sheet_name="Sem serie no sistema", index=False)

        return destino

    def _imprimir_resumo(self, **kw):
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"=== RECONCILIAÇÃO — {kw['fornecedor_nome']} ==="))
        self.stdout.write(f"Modo: {'APLICANDO' if kw['aplicar'] else 'SIMULAÇÃO (dry-run)'}")
        self.stdout.write(f"Itens locados cadastrados no sistema para este fornecedor: {kw['total_escopo']}")
        self.stdout.write(f"Linhas válidas lidas da planilha: {kw['total_planilha']}")
        self.stdout.write("")
        self.stdout.write(f"Batidos por número de série e ATUALIZADOS (modelo/valor mensal): {len(kw['atualizados'])}")
        self.stdout.write(f"Batidos por número de série, sem mudança: {kw['sem_mudanca'] and len(kw['sem_mudanca'])}")
        self.stdout.write(
            f"Só no sistema, não apareceram na planilha (candidatos a exclusão): {kw['candidatos_exclusao']}"
            + (f" — {len(kw['excluidos'])} EXCLUÍDOS agora" if kw['excluir_nao_encontrados'] and kw['aplicar'] else " — nenhum excluído (use --excluir-nao-encontrados junto com --aplicar)")
        )
        criados_agora = [r for r in kw["cadastrados"] if r["item"] is not None]
        self.stdout.write(
            f"Só na planilha, sem correspondência no sistema: {kw['so_planilha']}"
            + (f" — {len(criados_agora)} CADASTRADOS agora" if kw['cadastrar_novos'] and kw['aplicar'] else " — nenhum cadastrado (use --cadastrar-novos junto com --aplicar)")
        )
        self.stdout.write(f"Itens no sistema sem número de série cadastrado (fora do match automático): {kw['sem_serie_db']}")

        if kw["avisos_planilha"]:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING("=== AVISOS DA LEITURA DA PLANILHA ==="))
            for aviso in kw["avisos_planilha"]:
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

    def _normalize_header(self, value):
        if value is None:
            return ""
        text = str(value).strip().lower()
        text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
        return " ".join(text.split())

    def _find_column(self, columns, candidatos):
        alvo = {self._normalize_header(c) for c in candidatos}
        for col in columns:
            if self._normalize_header(col) in alvo:
                return col
        return None

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
