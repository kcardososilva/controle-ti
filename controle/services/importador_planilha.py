import pandas as pd
from decimal import Decimal, InvalidOperation

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required

import unicodedata
import re


class ImportadorPlanilhaService:
    """
    Importador de itens ajustado para a nova regra do sistema:

    1. Item normal:
       - Cadastra dados básicos do Item.
       - Usa quantidade, valor, data_compra e numero_pedido do Item.

    2. Item locado:
       - Cadastra Item.
       - Não preenche data_compra nem numero_pedido no Item.
       - Cria/atualiza Locacao.
       - Locacao.fornecedor herda o fornecedor do Item.

    3. Item de consumo:
       - Cadastra Item.
       - Força tem_lote=True.
       - Cria/atualiza LoteEstoque.
       - Cria/atualiza ItemLote.
       - Item.quantidade, Item.valor, Item.fornecedor, Item.numero_pedido e Item.data_compra
         passam a ser derivados do lote.
    """

    def __init__(self, arquivo, atualizar_sem_serie=False, usuario=None):
        self.arquivo = arquivo
        self.atualizar_sem_serie = atualizar_sem_serie
        self.usuario = usuario

        self.Item = self._find_model("Item")
        self.Locacao = self._find_model("Locacao")
        self.LoteEstoque = self._find_model("LoteEstoque")
        self.ItemLote = self._find_model("ItemLote")

        self.CentroCusto = self._find_model("CentroCusto")
        self.Fornecedor = self._find_model("Fornecedor")
        self.Subtipo = self._find_model("Subtipo")
        self.Localidade = self._find_model("Localidade")

        self.aliases = {
            "Nome": ["Nome", "ITEM", "EQUIPAMENTO", "DESCRIÇÃO", "DESCRICAO"],
            "Status": ["Status"],
            "Localidade": ["Localidade", "Local"],
            "SUBTIPO": ["SUBTIPO", "Subtipo", "Tipo", "Tipo de Item"],
            "Fabricante": ["Fabricante", "Marca"],
            "MODELO": ["MODELO", "Modelo"],
            "NÚMERO DE SÉRIE": [
                "NÚMERO DE SÉRIE",
                "Numero de Serie",
                "Número de Série",
                "Nº Série",
                "N Série",
                "NS",
                "Serial",
            ],
            "CENTRO DE CUSTO": ["CENTRO DE CUSTO", "Centro de Custo", "CC"],
            "PMB?": ["PMB?", "PMB", "PMB? "],
            "ITEM CONSUMO": ["ITEM CONSUMO", "Item Consumo", "Consumo", "Item de Consumo"],
            "REQUER PREVENTIVA": ["REQUER PREVENTIVA", "PREVENTIVA", "Precisa Preventiva"],
            "PERIODICIDADE": ["PERIODICIDADE", "Data Limite Preventiva", "Periodicidade Preventiva"],
            "LOCADO": ["LOCADO", "Locado"],

            # Aquisição normal / locação
            "QUANTIDADE": ["QUANTIDADE", "Quantidade", "Qtd", "QTD"],
            "FORNECEDOR": ["FORNECEDOR", "FORNECEDOR ", "Fornecedor"],
            "ANEXO": ["ANEXO", "ANEXO ", "CONTRATO", "Contrato"],
            "VALOR": ["VALOR", "VALOR UNITÁRIO", "VALOR UNITARIO", "Valor Unitário"],
            "VALOR MENSAL": ["VALOR MENSAL", "Valor Mensal", "Mensalidade"],
            "DATA COMPRA": ["DATA COMPRA", "Data Compra", "Data da Compra"],
            "NUMERO PEDIDO": [
                "NUMERO PEDIDO",
                "NÚMERO PEDIDO",
                "Número Pedido",
                "Nº Pedido",
                "PEDIDO",
                "Pedido",
            ],

            # Locação
            "DATA ENTRADA": ["DATA ENTRADA", "Data Entrada", "Data de Entrada"],
            "TEMPO CONTRATO": ["TEMPO CONTRATO", "Tempo Contrato", "Tempo de Contrato"],

            # Lote
            "LOTE FORNECEDOR": [
                "LOTE FORNECEDOR",
                "Fornecedor Lote",
                "FORNECEDOR LOTE",
                "Fornecedor",
                "FORNECEDOR",
            ],
            "LOTE DATA ENTRADA": [
                "LOTE DATA ENTRADA",
                "Data Entrada Lote",
                "DATA ENTRADA LOTE",
                "Data de Entrada",
                "DATA ENTRADA",
            ],
            "LOTE NUMERO NF": [
                "LOTE NUMERO NF",
                "LOTE NÚMERO NF",
                "Número NF",
                "Numero NF",
                "Nº NF",
                "NF",
                "Nota Fiscal",
            ],
            "LOTE QUANTIDADE": [
                "LOTE QUANTIDADE",
                "Quantidade Lote",
                "QTD LOTE",
                "Quantidade",
                "QUANTIDADE",
            ],
            "LOTE CUSTO UNITARIO": [
                "LOTE CUSTO UNITARIO",
                "LOTE CUSTO UNITÁRIO",
                "Custo Unitário",
                "Custo Unitario",
                "VALOR UNITÁRIO",
                "VALOR UNITARIO",
                "VALOR",
            ],
            "LOTE OBSERVACAO": [
                "LOTE OBSERVACAO",
                "LOTE OBSERVAÇÃO",
                "Observação Lote",
                "Observacao Lote",
                "OBS LOTE",
                "Observação Técnica",
            ],
        }

    def executar(self):
        df = pd.read_excel(self.arquivo)
        df.columns = [str(c).strip() for c in df.columns if str(c).strip() != "None"]

        criados = []
        atualizados = []
        ignorados = []
        erros = []

        numeros_serie_planilha = set()

        for idx, row in df.iterrows():
            linha_excel = idx + 2

            try:
                # Uma transação por linha (savepoint curto): mantém o import
                # resiliente a erro pontual sem quebrar as linhas seguintes e evita
                # segurar o lock de escrita do SQLite pela duração do arquivo
                # inteiro — cada linha libera o lock assim que termina, permitindo
                # que outras escritas do sistema (movimentações, preventivas etc.)
                # intercalem em vez de esperar todo o import terminar.
                with transaction.atomic():
                    resultado_linha = self._processar_linha(
                        row=row,
                        columns=df.columns,
                        linha_excel=linha_excel,
                        numeros_serie_planilha=numeros_serie_planilha,
                    )

                if resultado_linha["acao"] == "criado":
                    criados.append(resultado_linha["item"])

                elif resultado_linha["acao"] == "atualizado":
                    atualizados.append(resultado_linha["item"])

                elif resultado_linha["acao"] == "ignorado":
                    ignorados.append(resultado_linha["item"])
                    erros.append(f"Linha {linha_excel}: {resultado_linha['item']['motivo']}")

            except Exception as exc:
                ignorados.append({
                    "linha": linha_excel,
                    "motivo": str(exc),
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
            },
        }

    def _processar_linha(self, *, row, columns, linha_excel, numeros_serie_planilha):
        nome = self._clean_text(self._get_value(row, columns, self.aliases["Nome"]))
        numero_serie = self._clean_text(self._get_value(row, columns, self.aliases["NÚMERO DE SÉRIE"]))

        if not nome:
            return self._ignorado(linha_excel, "Sem nome do item/equipamento.")

        if numero_serie:
            chave_serie = self._normalize_text(numero_serie)

            if chave_serie in numeros_serie_planilha:
                return self._ignorado(
                    linha_excel,
                    f"Número de série duplicado na planilha ({numero_serie})."
                )

            numeros_serie_planilha.add(chave_serie)

        status = self._map_status(self._get_value(row, columns, self.aliases["Status"]))

        local_nome = self._clean_text(self._get_value(row, columns, self.aliases["Localidade"]))
        subtipo_nome = self._clean_text(self._get_value(row, columns, self.aliases["SUBTIPO"]))
        marca = self._clean_text(self._get_value(row, columns, self.aliases["Fabricante"]))
        modelo = self._clean_text(self._get_value(row, columns, self.aliases["MODELO"]))
        centro_custo_nome = self._clean_text(self._get_value(row, columns, self.aliases["CENTRO DE CUSTO"]))

        pmb = self._map_sim_nao(
            self._get_value(row, columns, self.aliases["PMB?"]),
            default="nao",
        )

        item_consumo = self._map_sim_nao(
            self._get_value(row, columns, self.aliases["ITEM CONSUMO"]),
            default="nao",
        )

        locado = self._map_sim_nao(
            self._get_value(row, columns, self.aliases["LOCADO"]),
            default="nao",
        )

        precisa_preventiva = self._map_sim_nao(
            self._get_value(row, columns, self.aliases["REQUER PREVENTIVA"]),
            default="nao",
        )

        periodicidade = self._to_int(
            self._get_value(row, columns, self.aliases["PERIODICIDADE"])
        )

        if precisa_preventiva == "sim" and not periodicidade:
            return self._ignorado(
                linha_excel,
                "Item marcado com preventiva, mas sem periodicidade."
            )

        if precisa_preventiva == "nao":
            periodicidade = None

        if item_consumo == "sim" and locado == "sim":
            return self._ignorado(
                linha_excel,
                "Item de consumo não pode ser importado como locado."
            )

        centro_custo = self._find_fk(self.CentroCusto, centro_custo_nome)
        subtipo = self._find_fk(self.Subtipo, subtipo_nome)
        localidade = self._find_fk(self.Localidade, local_nome)

        fornecedor_nome = self._clean_text(
            self._get_value(row, columns, self.aliases["FORNECEDOR"])
        )
        fornecedor = self._find_fk(self.Fornecedor, fornecedor_nome)

        quantidade_item = self._to_int(
            self._get_value(row, columns, self.aliases["QUANTIDADE"])
        ) or 1

        valor_item = self._to_decimal(
            self._get_value(row, columns, self.aliases["VALOR"])
        )

        data_compra = self._to_date(
            self._get_value(row, columns, self.aliases["DATA COMPRA"])
        )

        numero_pedido = self._clean_text(
            self._get_value(row, columns, self.aliases["NUMERO PEDIDO"])
        )

        data_entrada_locacao = self._to_date(
            self._get_value(row, columns, self.aliases["DATA ENTRADA"])
        )

        tempo_contrato = self._to_int(
            self._get_value(row, columns, self.aliases["TEMPO CONTRATO"])
        )

        valor_mensal = self._to_decimal(
            self._get_value(row, columns, self.aliases["VALOR MENSAL"])
        )

        if valor_mensal is None:
            valor_mensal = valor_item

        anexo = self._clean_text(
            self._get_value(row, columns, self.aliases["ANEXO"])
        )

        observacoes_item = self._montar_observacoes(anexo)

        item = self._buscar_item(numero_serie=numero_serie, nome=nome)
        criando = item is None

        if criando:
            item = self.Item(numero_serie=numero_serie or None)

        item.nome = nome
        item.marca = marca
        item.modelo = modelo
        item.centro_custo = centro_custo
        item.item_consumo = item_consumo
        item.pmb = pmb
        item.status = status
        item.subtipo = subtipo
        item.localidade = localidade
        item.precisa_preventiva = precisa_preventiva
        item.data_limite_preventiva = periodicidade
        item.locado = locado
        item.observacoes = observacoes_item

        self._preencher_auditoria(item, criando=criando)

        if item_consumo == "sim":
            lote = self._criar_ou_atualizar_lote_consumo(
                row=row,
                columns=columns,
                item=item,
                linha_excel=linha_excel,
            )

            item.tem_lote = True
            item.quantidade = lote.quantidade
            item.valor = lote.custo_unitario
            item.fornecedor = lote.fornecedor
            item.numero_pedido = lote.numero_nf
            item.data_compra = lote.data_entrada
            item.locado = "nao"

            item.full_clean()
            item.save()

            self._criar_ou_atualizar_item_lote(item=item, lote=lote)

        elif locado == "sim":
            item.tem_lote = False
            item.quantidade = quantidade_item
            item.valor = valor_item
            item.fornecedor = fornecedor

            # Regra nova: locado não preenche data_compra nem pedido/NF no Item.
            item.data_compra = None
            item.numero_pedido = None

            item.full_clean()
            item.save()

            self._criar_ou_atualizar_locacao(
                item=item,
                fornecedor=item.fornecedor,
                tempo_contrato=tempo_contrato,
                valor_mensal=valor_mensal,
                data_entrada=data_entrada_locacao,
                contrato=anexo,
            )

        else:
            item.tem_lote = False
            item.quantidade = quantidade_item
            item.valor = valor_item
            item.fornecedor = fornecedor
            item.data_compra = data_compra
            item.numero_pedido = numero_pedido

            item.full_clean()
            item.save()

        return {
            "acao": "criado" if criando else "atualizado",
            "item": {
                "linha": linha_excel,
                "nome": item.nome,
                "numero_serie": item.numero_serie or "-",
                "item_consumo": item.item_consumo,
                "locado": item.locado,
            }
        }

    def _buscar_item(self, *, numero_serie, nome):
        if numero_serie:
            item = self.Item.objects.filter(numero_serie=numero_serie).first()
            if item:
                return item

        if self.atualizar_sem_serie:
            item = (
                self.Item.objects
                .filter(nome=nome)
                .filter(numero_serie__isnull=True)
                .first()
            )

            if item:
                return item

            item = self.Item.objects.filter(nome=nome, numero_serie="").first()
            if item:
                return item

        return None

    def _criar_ou_atualizar_lote_consumo(self, *, row, columns, item, linha_excel):
        fornecedor_lote_nome = self._clean_text(
            self._get_value(row, columns, self.aliases["LOTE FORNECEDOR"])
        )

        fornecedor_lote = self._find_fk(self.Fornecedor, fornecedor_lote_nome)

        data_entrada_lote = self._to_date(
            self._get_value(row, columns, self.aliases["LOTE DATA ENTRADA"])
        )

        numero_nf = self._clean_text(
            self._get_value(row, columns, self.aliases["LOTE NUMERO NF"])
        )

        quantidade_lote = self._to_int(
            self._get_value(row, columns, self.aliases["LOTE QUANTIDADE"])
        )

        custo_unitario = self._to_decimal(
            self._get_value(row, columns, self.aliases["LOTE CUSTO UNITARIO"])
        )

        observacao_lote = self._clean_text(
            self._get_value(row, columns, self.aliases["LOTE OBSERVACAO"])
        )

        if not fornecedor_lote:
            raise ValidationError(f"Linha {linha_excel}: fornecedor do lote não encontrado ou não informado.")

        if not data_entrada_lote:
            raise ValidationError(f"Linha {linha_excel}: data de entrada do lote não informada.")

        if not numero_nf:
            raise ValidationError(f"Linha {linha_excel}: número da NF do lote não informado.")

        if not quantidade_lote or quantidade_lote <= 0:
            raise ValidationError(f"Linha {linha_excel}: quantidade do lote inválida.")

        if not custo_unitario or custo_unitario <= 0:
            raise ValidationError(f"Linha {linha_excel}: custo unitário do lote inválido.")

        lote = (
            self.LoteEstoque.objects
            .filter(
                fornecedor=fornecedor_lote,
                numero_nf=numero_nf,
                data_entrada=data_entrada_lote,
            )
            .first()
        )

        criando = lote is None

        if criando:
            lote = self.LoteEstoque()

        lote.fornecedor = fornecedor_lote
        lote.data_entrada = data_entrada_lote
        lote.numero_nf = numero_nf
        lote.quantidade = quantidade_lote
        lote.custo_unitario = custo_unitario
        lote.observacao_tecnica = observacao_lote or self._montar_observacoes_lote(item)

        self._preencher_auditoria(lote, criando=criando)

        lote.full_clean()
        lote.save()

        return lote

    def _criar_ou_atualizar_item_lote(self, *, item, lote):
        item_lote = (
            self.ItemLote.objects
            .filter(item=item, lote=lote)
            .first()
        )

        criando = item_lote is None

        if criando:
            item_lote = self.ItemLote(item=item, lote=lote)

        quantidade_anterior = item_lote.quantidade_entrada or 0 if item_lote.pk else 0
        quantidade_nova = lote.quantidade
        diferenca = quantidade_nova - quantidade_anterior

        if criando:
            item_lote.quantidade_entrada = lote.quantidade
            item_lote.quantidade_disponivel = lote.quantidade
        else:
            nova_disponivel = (item_lote.quantidade_disponivel or 0) + diferenca

            if nova_disponivel < 0:
                raise ValidationError(
                    "A quantidade do lote não pode ser menor que a quantidade já consumida/movimentada."
                )

            item_lote.quantidade_entrada = quantidade_nova
            item_lote.quantidade_disponivel = nova_disponivel

        item_lote.custo_unitario = lote.custo_unitario

        self._preencher_auditoria(item_lote, criando=criando)

        item_lote.full_clean()
        item_lote.save()

        return item_lote

    def _criar_ou_atualizar_locacao(
        self,
        *,
        item,
        fornecedor,
        tempo_contrato,
        valor_mensal,
        data_entrada,
        contrato,
    ):
        if self.Locacao is None:
            return None

        defaults_locacao = {
            "tempo_locado": tempo_contrato,
            "valor_mensal": valor_mensal,
            "data_entrada": data_entrada,
            "contrato": contrato,
            "observacoes": self._montar_observacoes_locacao(contrato),
            "fornecedor": fornecedor,
        }

        locacao, criando = self.Locacao.objects.update_or_create(
            equipamento=item,
            defaults=defaults_locacao,
        )

        self._preencher_auditoria(locacao, criando=criando)
        locacao.save()

        return locacao

    def _preencher_auditoria(self, obj, criando=False):
        if not self.usuario:
            return

        if criando and hasattr(obj, "criado_por") and not getattr(obj, "criado_por_id", None):
            obj.criado_por = self.usuario

        if hasattr(obj, "atualizado_por"):
            obj.atualizado_por = self.usuario

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

    def _normalize_header(self, value):
        if value is None:
            return ""

        value = str(value).strip().lower()
        value = "".join(
            c for c in unicodedata.normalize("NFKD", value)
            if not unicodedata.combining(c)
        )

        return " ".join(value.split())

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
            "manutenção": "manutencao",
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
            return value.quantize(Decimal("0.01"))

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
            parsed = pd.to_datetime(value, errors="coerce", dayfirst=True)

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
                if field.get_internal_type() in {"CharField", "TextField"}:
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

    def _montar_observacoes_lote(self, item):
        return f"Lote importado por planilha para o item: {item.nome}."

    def _ignorado(self, linha_excel, motivo):
        return {
            "acao": "ignorado",
            "item": {
                "linha": linha_excel,
                "motivo": motivo,
            }
        }


@login_required
def importar_planilha(request):
    if request.method != "POST":
        return JsonResponse({
            "ok": False,
            "mensagem": "Método não permitido.",
        }, status=405)

    arquivo = request.FILES.get("arquivo")

    if not arquivo:
        return JsonResponse({
            "ok": False,
            "mensagem": "Selecione um arquivo.",
        }, status=400)

    try:
        service = ImportadorPlanilhaService(
            arquivo,
            atualizar_sem_serie=True,
            usuario=request.user,
        )

        resultado = service.executar()

        return JsonResponse({
            "ok": True,
            "mensagem": "Importação concluída com sucesso.",
            "resultado": resultado,
        })

    except Exception as e:
        return JsonResponse({
            "ok": False,
            "mensagem": f"Erro ao importar: {str(e)}",
        }, status=500)