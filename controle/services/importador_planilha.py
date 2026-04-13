import pandas as pd
from decimal import Decimal, InvalidOperation
from django.db import transaction
from django.apps import apps
import unicodedata
import re


class ImportadorPlanilhaService:
    def __init__(self, arquivo, atualizar_sem_serie=False):
        self.arquivo = arquivo
        self.atualizar_sem_serie = atualizar_sem_serie

        self.Item = self._find_model("Item")
        self.Locacao = self._find_model("Locacao")
        self.CentroCusto = self._find_model("CentroCusto")
        self.Fornecedor = self._find_model("Fornecedor")
        self.Subtipo = self._find_model("Subtipo")
        self.Localidade = self._find_model("Localidade")

        self.aliases = {
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
            "ANEXO": ["ANEXO", "ANEXO ", "CONTRATO", "Contrato"],
            "VALOR": ["VALOR", "VALOR UNITÁRIO", "VALOR UNITARIO", "VALOR MENSAL"],
            "DATA ENTRADA": ["DATA ENTRADA"],
        }

    def executar(self):
        df = pd.read_excel(self.arquivo)
        df.columns = [str(c).strip() for c in df.columns if str(c).strip() != "None"]

        criados = []
        atualizados = []
        ignorados = []
        erros = []
        numeros_serie = set()

        with transaction.atomic():
            for idx, row in df.iterrows():
                linha_excel = idx + 2

                try:
                    nome = self._clean_text(self._get_value(row, df.columns, self.aliases["Nome"]))
                    numero_serie = self._clean_text(
                        self._get_value(row, df.columns, self.aliases["NÚMERO DE SÉRIE"])
                    )

                    if not nome:
                        ignorados.append({
                            "linha": linha_excel,
                            "motivo": "Sem nome do equipamento"
                        })
                        erros.append(f"Linha {linha_excel}: sem nome.")
                        continue

                    if numero_serie:
                        chave = self._normalize_text(numero_serie)

                        if chave in numeros_serie:
                            ignorados.append({
                                "linha": linha_excel,
                                "motivo": f"Número de série duplicado na planilha ({numero_serie})"
                            })
                            erros.append(
                                f"Linha {linha_excel}: número de série duplicado na planilha ({numero_serie})."
                            )
                            continue

                        numeros_serie.add(chave)

                        if self.Item.objects.filter(numero_serie=numero_serie).exists():
                            ignorados.append({
                                "linha": linha_excel,
                                "motivo": f"Número de série já cadastrado ({numero_serie})"
                            })
                            erros.append(
                                f"Linha {linha_excel}: número de série já cadastrado ({numero_serie})."
                            )
                            continue

                    status = self._map_status(self._get_value(row, df.columns, self.aliases["Status"]))
                    local_nome = self._clean_text(self._get_value(row, df.columns, self.aliases["Localidade"]))
                    subtipo_nome = self._clean_text(self._get_value(row, df.columns, self.aliases["SUBTIPO"]))
                    marca = self._clean_text(self._get_value(row, df.columns, self.aliases["Fabricante"]))
                    modelo = self._clean_text(self._get_value(row, df.columns, self.aliases["MODELO"]))
                    centro_custo_nome = self._clean_text(
                        self._get_value(row, df.columns, self.aliases["CENTRO DE CUSTO"])
                    )
                    pmb = self._map_sim_nao(
                        self._get_value(row, df.columns, self.aliases["PMB?"]),
                        default="nao"
                    )
                    item_consumo = self._map_sim_nao(
                        self._get_value(row, df.columns, self.aliases["ITEM CONSUMO"]),
                        default="nao"
                    )
                    precisa_preventiva = self._map_sim_nao(
                        self._get_value(row, df.columns, self.aliases["REQUER PREVENTIVA"]),
                        default="nao"
                    )
                    periodicidade = self._to_int(
                        self._get_value(row, df.columns, self.aliases["PERIODICIDADE"])
                    )
                    locado = self._map_sim_nao(
                        self._get_value(row, df.columns, self.aliases["LOCADO"]),
                        default="nao"
                    )
                    tempo_contrato = self._to_int(
                        self._get_value(row, df.columns, self.aliases["TEMPO CONTRATO"])
                    )
                    fornecedor_nome = self._clean_text(
                        self._get_value(row, df.columns, self.aliases["FORNECEDOR"])
                    )
                    anexo = self._clean_text(
                        self._get_value(row, df.columns, self.aliases["ANEXO"])
                    )
                    valor = self._to_decimal(
                        self._get_value(row, df.columns, self.aliases["VALOR"])
                    )
                    data_entrada = self._to_date(
                        self._get_value(row, df.columns, self.aliases["DATA ENTRADA"])
                    )

                    centro_custo = self._find_fk(self.CentroCusto, centro_custo_nome)
                    fornecedor = self._find_fk(self.Fornecedor, fornecedor_nome)
                    subtipo = self._find_fk(self.Subtipo, subtipo_nome)
                    localidade = self._find_fk(self.Localidade, local_nome)

                    observacoes_item = self._montar_observacoes(anexo)

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
                        "observacoes": observacoes_item,
                    }

                    if numero_serie:
                        item = self.Item.objects.create(numero_serie=numero_serie, **defaults_item)
                        criados.append({
                            "nome": item.nome,
                            "numero_serie": item.numero_serie or "-"
                        })
                    else:
                        item = None

                        if self.atualizar_sem_serie:
                            item = self.Item.objects.filter(nome=nome).filter(numero_serie__isnull=True).first()
                            if item is None:
                                item = self.Item.objects.filter(nome=nome, numero_serie="").first()

                        if item:
                            for campo, valor_campo in defaults_item.items():
                                setattr(item, campo, valor_campo)
                            item.save()
                            atualizados.append({
                                "nome": item.nome,
                                "numero_serie": item.numero_serie or "-"
                            })
                        else:
                            item = self.Item.objects.create(numero_serie=None, **defaults_item)
                            criados.append({
                                "nome": item.nome,
                                "numero_serie": item.numero_serie or "-"
                            })

                    if locado == "sim" and self.Locacao is not None:
                        defaults_locacao = {
                            "tempo_locado": tempo_contrato,
                            "valor_mensal": valor,
                            "data_entrada": data_entrada,
                            "contrato": anexo,
                            "observacoes": self._montar_observacoes_locacao(anexo),
                            "fornecedor": fornecedor,
                        }
                        self.Locacao.objects.update_or_create(
                            equipamento=item,
                            defaults=defaults_locacao,
                        )

                except Exception as exc:
                    ignorados.append({
                        "linha": linha_excel,
                        "motivo": str(exc)
                    })
                    erros.append(f"Linha {linha_excel}: {exc}")

        return {
            "criados": criados,
            "atualizados": atualizados,
            "ignorados": ignorados,
            "erros": erros,
            "totais": {
                "criados": len(criados),
                "atualizados": len(atualizados),
                "ignorados": len(ignorados),
            }
        }

    def _find_model(self, name):
        for model in apps.get_models():
            if model.__name__.lower() == name.lower():
                return model
        raise LookupError(f"Model '{name}' não encontrado.")

    def _normalize_text(self, text):
        if not text:
            return None

        text = str(text).strip().lower()
        text = "".join(
            c for c in unicodedata.normalize("NFKD", text)
            if not unicodedata.combining(c)
        )
        text = re.sub(r"[^a-z0-9]", "", text)
        return text

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
        return text or None

    def _map_sim_nao(self, value, default="nao"):
        text = self._clean_text(value)
        if not text:
            return default

        text = self._normalize_text(text)
        if text in {"sim", "s", "yes", "y", "true", "1"}:
            return "sim"
        if text in {"nao", "n", "no", "false", "0"}:
            return "nao"
        return default

    def _map_status(self, value):
        text = self._clean_text(value)
        if not text:
            return "ativo"

        text = self._normalize_text(text)
        mapa = {
            "ativo": "ativo",
            "backup": "backup",
            "manutencao": "manutencao",
            "defeito": "defeito",
            "pausado": "pausado",
            "inativo": "pausado",
        }
        return mapa.get(text, "ativo")

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

        try:
            return int(float(str(value).replace(",", ".")))
        except Exception:
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

        try:
            parsed = pd.to_datetime(value, errors="coerce")
            if pd.isna(parsed):
                return None
            return parsed.date()
        except Exception:
            return None

    def _find_fk(self, model_class, valor):
        if model_class is None or not valor:
            return None

        valor_norm = self._normalize_text(valor)

        for obj in model_class.objects.all():
            for field in model_class._meta.fields:
                if field.get_internal_type() == "CharField":
                    valor_bd = getattr(obj, field.name, None)
                    if valor_bd and self._normalize_text(valor_bd) == valor_norm:
                        return obj

        return None

    def _montar_observacoes(self, anexo):
        if not anexo:
            return "Importado por planilha."
        return f"Importado por planilha. Referência/Anexo: {anexo}"

    def _montar_observacoes_locacao(self, anexo):
        if not anexo:
            return "Importado por planilha."
        return f"Importado por planilha. Referência do contrato/anexo: {anexo}"