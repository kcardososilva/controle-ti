
from decimal import Decimal, InvalidOperation
from pathlib import Path
import unicodedata

import pandas as pd
from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = "Importa itens/equipamentos da planilha Excel, respeitando a estrutura de colunas enviada e sem duplicar número de série."

    def add_arguments(self, parser):
        parser.add_argument(
            "--arquivo",
            type=str,
            default="dispositivos.xlsx",
            help="Caminho do arquivo Excel (.xlsx). Padrão: dispositivos.xlsx",
        )
        parser.add_argument(
            "--atualizar-sem-serie",
            action="store_true",
            help="Quando o item não tiver número de série, tenta localizar por nome e atualizar em vez de criar outro.",
        )

    def handle(self, *args, **options):
        arquivo = options["arquivo"]
        atualizar_sem_serie = options["atualizar_sem_serie"]

        Item = self._find_model("Item")
        Locacao = self._find_model("Locacao")
        CentroCusto = self._find_model("CentroCusto")
        Fornecedor = self._find_model("Fornecedor")
        Subtipo = self._find_model("Subtipo")
        Localidade = self._find_model("Localidade")

        caminho = Path(arquivo)
        if not caminho.exists():
            self.stdout.write(self.style.ERROR(f"Arquivo não encontrado: {caminho.resolve()}"))
            return

        try:
            df = pd.read_excel(caminho)
        except Exception as exc:
            self.stdout.write(self.style.ERROR(f"Erro ao ler a planilha: {exc}"))
            return

        if df.empty:
            self.stdout.write(self.style.WARNING("A planilha está vazia. Nada para importar."))
            return

        df.columns = [str(col).strip() for col in df.columns if str(col).strip() != "None"]

        aliases = {
            "Nome": ["Nome"],
            "Status": ["Status"],
            "Localidade": ["Localidade", "Local"],
            "SUBTIPO": ["SUBTIPO", "Subtipo"],
            "Fabricante": ["Fabricante", "Marca"],
            "MODELO": ["MODELO", "Modelo"],
            "NÚMERO DE SÉRIE": ["NÚMERO DE SÉRIE", "Numero de Serie", "Número de Série"],
            "CENTRO DE CUSTO": ["CENTRO DE CUSTO", "Centro de Custo"],
            "PMB?": ["PMB?", "PMB? "],
            "ITEM CONSUMO": ["ITEM CONSUMO"],
            "REQUER PREVENTIVA": ["REQUER PREVENTIVA", "PREVENTIVA"],
            "PERIODICIDADE": ["PERIODICIDADE"],
            "LOCADO": ["LOCADO"],
            "TEMPO CONTRATO": ["TEMPO CONTRATO"],
            "FORNECEDOR": ["FORNECEDOR", "FORNECEDOR "],
            "ANEXO": ["ANEXO"],
            "VALOR": ["VALOR", "VALOR UNITÁRIO", "VALOR UNITARIO", "VALOR MENSAL"],
            "DATA ENTRADA": ["DATA ENTRADA"],
        }

        faltantes = []
        for canonical, names in aliases.items():
            if not any(self._find_column(df.columns, name) for name in names):
                # Campos opcionais não devem travar a importação
                if canonical not in {"ANEXO", "VALOR", "DATA ENTRADA", "TEMPO CONTRATO", "FORNECEDOR"}:
                    faltantes.append(canonical)

        if faltantes:
            self.stdout.write(
                self.style.ERROR(
                    "A planilha não possui estas colunas obrigatórias: " + ", ".join(faltantes)
                )
            )
            return

        duplicados_na_planilha = self._duplicados_na_planilha(df, aliases)
        if duplicados_na_planilha:
            self.stdout.write(self.style.ERROR(
                "A planilha possui números de série duplicados. Corrija antes de importar:"
            ))
            for serie in duplicados_na_planilha:
                self.stdout.write(f" - {serie}")
            return

        total_linhas = 0
        criados = 0
        atualizados = 0
        locacoes_criadas = 0
        locacoes_atualizadas = 0
        ignorados_duplicidade = 0
        ignorados_sem_nome = 0
        erros = []

        with transaction.atomic():
            for idx, row in df.iterrows():
                linha_excel = idx + 2
                total_linhas += 1

                try:
                    nome = self._clean_text(self._get_value(row, df.columns, aliases["Nome"]))
                    status = self._map_status(self._get_value(row, df.columns, aliases["Status"]))
                    local_nome = self._clean_text(self._get_value(row, df.columns, aliases["Localidade"]))
                    subtipo_nome = self._clean_text(self._get_value(row, df.columns, aliases["SUBTIPO"]))
                    marca = self._clean_text(self._get_value(row, df.columns, aliases["Fabricante"]))
                    modelo = self._clean_text(self._get_value(row, df.columns, aliases["MODELO"]))
                    numero_serie = self._clean_text(self._get_value(row, df.columns, aliases["NÚMERO DE SÉRIE"]))
                    centro_custo_nome = self._clean_text(self._get_value(row, df.columns, aliases["CENTRO DE CUSTO"]))
                    pmb = self._map_sim_nao(self._get_value(row, df.columns, aliases["PMB?"]), default="nao")
                    item_consumo = self._map_sim_nao(self._get_value(row, df.columns, aliases["ITEM CONSUMO"]), default="nao")
                    precisa_preventiva = self._map_sim_nao(self._get_value(row, df.columns, aliases["REQUER PREVENTIVA"]), default="nao")
                    periodicidade = self._to_int(self._get_value(row, df.columns, aliases["PERIODICIDADE"]))
                    locado = self._map_sim_nao(self._get_value(row, df.columns, aliases["LOCADO"]), default="nao")
                    tempo_contrato = self._to_int(self._get_value(row, df.columns, aliases["TEMPO CONTRATO"]))
                    fornecedor_nome = self._clean_text(self._get_value(row, df.columns, aliases["FORNECEDOR"]))
                    anexo = self._clean_text(self._get_value(row, df.columns, aliases["ANEXO"]))
                    valor = self._to_decimal(self._get_value(row, df.columns, aliases["VALOR"]))
                    data_entrada = self._to_date(self._get_value(row, df.columns, aliases["DATA ENTRADA"]))

                    if not nome:
                        ignorados_sem_nome += 1
                        erros.append(f"Linha {linha_excel}: sem nome do equipamento. Registro ignorado.")
                        continue

                    centro_custo = self._find_fk(CentroCusto, centro_custo_nome)
                    fornecedor = self._find_fk(Fornecedor, fornecedor_nome)
                    subtipo = self._find_fk(Subtipo, subtipo_nome)
                    localidade = self._find_fk(Localidade, local_nome)

                    observacoes = self._montar_observacoes(anexo)

                    defaults_item = {
                        "nome": nome,
                        "marca": marca,
                        "modelo": modelo,
                        "centro_custo": centro_custo,
                        "quantidade": 1,
                        "item_consumo": item_consumo,
                        "pmb": pmb,
                        "valor": valor,
                        "status": status,
                        "fornecedor": fornecedor,
                        "subtipo": subtipo,
                        "localidade": localidade,
                        "precisa_preventiva": precisa_preventiva,
                        "data_limite_preventiva": periodicidade,
                        "locado": locado,
                        "observacoes": observacoes,
                    }

                    if numero_serie:
                        item_existente = Item.objects.filter(numero_serie=numero_serie).first()
                        if item_existente:
                            ignorados_duplicidade += 1
                            erros.append(
                                f"Linha {linha_excel}: já existe equipamento com número de série "
                                f"{numero_serie}. Registro ignorado."
                            )
                            continue

                        Item.objects.create(numero_serie=numero_serie, **defaults_item)
                        criados += 1
                        item = Item.objects.get(numero_serie=numero_serie)
                    else:
                        item = None
                        if atualizar_sem_serie:
                            item = Item.objects.filter(nome=nome).filter(numero_serie__isnull=True).first()
                            if item is None:
                                item = Item.objects.filter(nome=nome, numero_serie="").first()

                        if item:
                            for campo, valor_campo in defaults_item.items():
                                setattr(item, campo, valor_campo)
                            item.save()
                            atualizados += 1
                        else:
                            item = Item.objects.create(numero_serie=None, **defaults_item)
                            criados += 1

                    if locado == "sim" and Locacao is not None:
                        defaults_locacao = {
                            "tempo_locado": tempo_contrato,
                            "valor_mensal": valor,
                            "data_entrada": data_entrada,
                            "contrato": anexo,
                            "observacoes": self._montar_observacoes_locacao(anexo),
                            "fornecedor": fornecedor,
                        }
                        _, created = Locacao.objects.update_or_create(
                            equipamento=item,
                            defaults=defaults_locacao,
                        )
                        if created:
                            locacoes_criadas += 1
                        else:
                            locacoes_atualizadas += 1

                except Exception as exc:
                    erros.append(f"Linha {linha_excel}: erro ao importar. Detalhe: {exc}")

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("=== RESUMO DA IMPORTAÇÃO ==="))
        self.stdout.write(f"Linhas lidas: {total_linhas}")
        self.stdout.write(f"Itens criados: {criados}")
        self.stdout.write(f"Itens atualizados (sem série): {atualizados}")
        self.stdout.write(f"Locações criadas: {locacoes_criadas}")
        self.stdout.write(f"Locações atualizadas: {locacoes_atualizadas}")
        self.stdout.write(f"Ignorados por duplicidade de nº de série: {ignorados_duplicidade}")
        self.stdout.write(f"Ignorados sem nome: {ignorados_sem_nome}")
        self.stdout.write(f"Erros/logs: {len(erros)}")

        if erros:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING("=== DETALHES ==="))
            for erro in erros:
                self.stdout.write(erro)

    def _find_model(self, model_name):
        for model in apps.get_models():
            if model.__name__.lower() == model_name.lower():
                return model
        raise LookupError(f"Model '{model_name}' não encontrado entre as apps instaladas.")

    def _find_column(self, columns, wanted):
        wanted_norm = self._normalize_header(wanted)
        for col in columns:
            if self._normalize_header(col) == wanted_norm:
                return col
        return None

    def _get_value(self, row, columns, candidates):
        if isinstance(candidates, str):
            candidates = [candidates]
        for wanted in candidates:
            col = self._find_column(columns, wanted)
            if col:
                return row.get(col)
        return None

    def _normalize_header(self, value):
        if value is None:
            return ""
        value = str(value).strip().lower()
        value = "".join(
            c for c in unicodedata.normalize("NFKD", value)
            if not unicodedata.combining(c)
        )
        return " ".join(value.split())

    def _clean_text(self, value):
        if value is None:
            return None
        try:
            if pd.isna(value):
                return None
        except Exception:
            pass
        text = str(value).strip()
        if not text:
            return None
        return text

    def _strip_accents(self, text):
        return "".join(
            c for c in unicodedata.normalize("NFKD", str(text))
            if not unicodedata.combining(c)
        )

    def _map_sim_nao(self, value, default="nao"):
        text = self._clean_text(value)
        if not text:
            return default
        text = self._strip_accents(text).strip().lower()

        if text in {"sim", "s", "yes", "y", "true", "1"}:
            return "sim"
        if text in {"nao", "não", "n", "no", "false", "0"}:
            return "nao"
        return default

    def _map_status(self, value):
        text = self._clean_text(value)
        if not text:
            return "ativo"

        text_norm = self._strip_accents(text).strip().lower()

        mapa = {
            "ativo": "ativo",
            "backup": "backup",
            "manutencao": "manutencao",
            "manutenção": "manutencao",
            "defeito": "defeito",
            "pausado": "pausado",
            "inativo": "pausado",
        }
        return mapa.get(text_norm, "ativo")

    def _to_decimal(self, value):
        if value is None:
            return None
        try:
            if pd.isna(value):
                return None
        except Exception:
            pass

        if isinstance(value, Decimal):
            return value

        if isinstance(value, (int, float)):
            return Decimal(str(value)).quantize(Decimal("0.01"))

        text = str(value).strip()
        if not text:
            return None

        text = text.replace("R$", "").replace(" ", "")
        if "," in text and "." in text:
            text = text.replace(".", "").replace(",", ".")
        elif "," in text:
            text = text.replace(",", ".")

        try:
            return Decimal(text).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            return None

    def _to_int(self, value):
        if value is None:
            return None
        try:
            if pd.isna(value):
                return None
        except Exception:
            pass

        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)

        text = str(value).strip()
        if not text:
            return None

        try:
            return int(float(text.replace(",", ".")))
        except ValueError:
            return None

    def _to_date(self, value):
        if value is None:
            return None
        try:
            if pd.isna(value):
                return None
        except Exception:
            pass

        if hasattr(value, "date"):
            try:
                return value.date()
            except Exception:
                pass

        parsed = pd.to_datetime(value, errors="coerce")
        if pd.isna(parsed):
            return None
        return parsed.date()

    def _find_fk(self, model_class, nome):
        if model_class is None or not nome:
            return None

        if hasattr(model_class, "nome"):
            obj = model_class.objects.filter(nome__iexact=nome).first()
            if obj:
                return obj

        for field in model_class._meta.fields:
            if getattr(field, "get_internal_type", lambda: "")() == "CharField":
                kwargs = {f"{field.name}__iexact": nome}
                obj = model_class.objects.filter(**kwargs).first()
                if obj:
                    return obj

        return None

    def _duplicados_na_planilha(self, df, aliases):
        series = []
        for _, row in df.iterrows():
            valor = self._clean_text(self._get_value(row, df.columns, aliases["NÚMERO DE SÉRIE"]))
            if valor:
                series.append(valor.strip().lower())

        vistos = set()
        duplicados = set()
        for serie in series:
            if serie in vistos:
                duplicados.add(serie)
            else:
                vistos.add(serie)

        return sorted(duplicados)

    def _montar_observacoes(self, anexo):
        if not anexo:
            return "Importado por planilha."
        return f"Importado por planilha. Referência/Anexo: {anexo}"

    def _montar_observacoes_locacao(self, anexo):
        if not anexo:
            return "Importado por planilha."
        return f"Importado por planilha. Referência do contrato/anexo: {anexo}"
