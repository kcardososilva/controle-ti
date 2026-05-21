import datetime
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from django.core.exceptions import FieldDoesNotExist
from django.db import IntegrityError, transaction
from django.utils import timezone
from openpyxl import load_workbook

from ProjetoEstoque.models import (
    Usuario,
    CentroCusto,
    Localidade,
    Funcao,
    StatusUsuarioChoices,
)


MESES_PT = {
    "jan": 1,
    "janeiro": 1,
    "fev": 2,
    "fevereiro": 2,
    "mar": 3,
    "marco": 3,
    "março": 3,
    "abr": 4,
    "abril": 4,
    "mai": 5,
    "maio": 5,
    "jun": 6,
    "junho": 6,
    "jul": 7,
    "julho": 7,
    "ago": 8,
    "agosto": 8,
    "set": 9,
    "setembro": 9,
    "out": 10,
    "outubro": 10,
    "nov": 11,
    "novembro": 11,
    "dez": 12,
    "dezembro": 12,
}


def model_has_field(model, field_name):
    try:
        model._meta.get_field(field_name)
        return True
    except FieldDoesNotExist:
        return False


def normalizar_texto(valor):
    if valor is None:
        return ""

    valor = str(valor).strip().lower()
    valor = unicodedata.normalize("NFKD", valor)
    valor = "".join(c for c in valor if not unicodedata.combining(c))
    valor = re.sub(r"[^a-z0-9\s]", " ", valor)
    valor = re.sub(r"\s+", " ", valor).strip()

    return valor


def normalizar_matricula(valor):
    if valor is None:
        return None

    valor = str(valor).strip()

    if not valor:
        return None

    if valor.endswith(".0"):
        valor = valor[:-2]

    valor = re.sub(r"\s+", "", valor)

    return valor or None


def parse_excel_date(valor):
    if not valor:
        return None

    if isinstance(valor, datetime.datetime):
        return valor.date()

    if isinstance(valor, datetime.date):
        return valor

    if isinstance(valor, (int, float)):
        try:
            return datetime.date(1899, 12, 30) + datetime.timedelta(days=int(valor))
        except Exception:
            return None

    texto = str(valor).strip()

    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.datetime.strptime(texto, fmt).date()
        except ValueError:
            continue

    return None


def sim_nao(valor):
    texto = normalizar_texto(valor)

    if texto in ("sim", "s", "yes", "y", "1", "verdadeiro", "true"):
        return "sim"

    return "nao"


def status_value(tipo):
    choices = dict(StatusUsuarioChoices.choices)
    values = list(choices.keys())

    if tipo == "ativo":
        for candidato in ("ativo", "ATIVO", "A"):
            if candidato in values:
                return candidato

    if tipo == "desligado":
        for candidato in ("desligado", "inativo", "INATIVO", "D", "I"):
            if candidato in values:
                return candidato

    return values[0] if values else tipo


def is_status_desligado(valor, data_desligamento=None):
    texto = normalizar_texto(valor)

    if data_desligamento:
        return True

    return texto in (
        "desligado",
        "desligada",
        "inativo",
        "inativa",
        "demitido",
        "demitida",
        "encerrado",
        "encerrada",
        "rescindido",
        "rescindida",
        "desligamento",
    )


def gerar_email_base(nome):
    partes = normalizar_texto(nome).split()

    if not partes:
        return None

    primeiro = partes[0]
    ultimo = partes[-1] if len(partes) > 1 else partes[0]

    return f"{primeiro}.{ultimo}@santacolomba.com"


def gerar_email_usuario(nome, matricula=None, usuario_id=None):
    email = gerar_email_base(nome)

    if not email:
        return None

    qs = Usuario.objects.filter(email__iexact=email)

    if usuario_id:
        qs = qs.exclude(pk=usuario_id)

    if not qs.exists():
        return email

    if matricula:
        partes = email.split("@")
        email_com_matricula = f"{partes[0]}.{matricula}@{partes[1]}"

        qs = Usuario.objects.filter(email__iexact=email_com_matricula)

        if usuario_id:
            qs = qs.exclude(pk=usuario_id)

        if not qs.exists():
            return email_com_matricula

    return email


def nome_aba_para_data(nome_aba):
    texto = normalizar_texto(nome_aba)
    partes = texto.split()

    mes = None
    ano = None

    for parte in partes:
        if parte in MESES_PT:
            mes = MESES_PT[parte]

        if parte.isdigit() and len(parte) == 4:
            ano = int(parte)

    if mes and ano:
        return datetime.date(ano, mes, 1)

    return None


def selecionar_abas(workbook, modo_importacao="ultima_aba", nome_aba=None):
    abas = workbook.sheetnames

    if modo_importacao == "todas_abas":
        return abas

    if modo_importacao == "aba_especifica":
        if nome_aba and nome_aba in abas:
            return [nome_aba]

        raise ValueError(f"A aba '{nome_aba}' não foi encontrada na planilha.")

    abas_com_data = []

    for aba in abas:
        data_ref = nome_aba_para_data(aba)
        if data_ref:
            abas_com_data.append((data_ref, aba))

    if abas_com_data:
        abas_com_data.sort(key=lambda item: item[0])
        return [abas_com_data[-1][1]]

    return [workbook.active.title]


def score_nome(a, b):
    return SequenceMatcher(None, normalizar_texto(a), normalizar_texto(b)).ratio()


def encontrar_usuario_por_nome(nome, limite=0.92):
    if not nome:
        return None

    direto = Usuario.objects.filter(nome__iexact=nome).first()

    if direto:
        return direto

    melhor = None
    melhor_score = 0

    for usuario in Usuario.objects.all().only("id", "nome", "matricula"):
        score = score_nome(nome, usuario.nome)

        if score > melhor_score:
            melhor = usuario
            melhor_score = score

    if melhor and melhor_score >= limite:
        return melhor

    return None


def buscar_ou_criar_funcao(nome):
    if not nome:
        return None

    nome = str(nome).strip()

    if not nome:
        return None

    obj = Funcao.objects.filter(nome__iexact=nome).first()

    if obj:
        return obj

    return Funcao.objects.create(nome=nome)


def buscar_ou_criar_localidade(nome):
    if not nome:
        return None

    nome = str(nome).strip()

    if not nome:
        return None

    if model_has_field(Localidade, "local"):
        obj = Localidade.objects.filter(local__iexact=nome).first()

        if obj:
            return obj

        try:
            return Localidade.objects.create(local=nome)
        except IntegrityError:
            return Localidade.objects.filter(local__icontains=nome).first()

    if model_has_field(Localidade, "nome"):
        obj = Localidade.objects.filter(nome__iexact=nome).first()

        if obj:
            return obj

        try:
            return Localidade.objects.create(nome=nome)
        except IntegrityError:
            return Localidade.objects.filter(nome__icontains=nome).first()

    return None


def buscar_ou_criar_centro_custo(numero, descricao):
    numero = normalizar_matricula(numero)
    descricao = str(descricao or "").strip()

    if not numero and not descricao:
        return None

    qs = CentroCusto.objects.all()

    if numero and model_has_field(CentroCusto, "numero"):
        obj = qs.filter(numero__iexact=numero).first()

        if obj:
            return obj

    if descricao and model_has_field(CentroCusto, "departamento"):
        obj = qs.filter(departamento__iexact=descricao).first()

        if obj:
            return obj

    data = {}

    if model_has_field(CentroCusto, "numero"):
        data["numero"] = numero or descricao[:20]

    if model_has_field(CentroCusto, "departamento"):
        data["departamento"] = descricao or numero

    if not data:
        return None

    try:
        return CentroCusto.objects.create(**data)
    except Exception:
        if numero and model_has_field(CentroCusto, "numero"):
            return CentroCusto.objects.filter(numero__icontains=numero).first()

        if descricao and model_has_field(CentroCusto, "departamento"):
            return CentroCusto.objects.filter(departamento__icontains=descricao).first()

    return None

def centro_custo_eh_tabaco(centro_custo):
    """
    Regra de negócio:
    Todo usuário vinculado a centro de custo relacionado a TABACO
    deve ser marcado automaticamente como PMB = sim.
    """

    if not centro_custo:
        return False

    campos_para_validar = [
        getattr(centro_custo, "numero", ""),
        getattr(centro_custo, "codigo", ""),
        getattr(centro_custo, "departamento", ""),
        getattr(centro_custo, "nome", ""),
        str(centro_custo),
    ]

    texto = " ".join(str(campo or "") for campo in campos_para_validar)
    texto_normalizado = normalizar_texto(texto)

    return "tabaco" in texto_normalizado


@dataclass
class ResultadoImportacao:
    abas_processadas: list = field(default_factory=list)
    criados: list = field(default_factory=list)
    atualizados: list = field(default_factory=list)
    desligados: list = field(default_factory=list)
    ignorados: list = field(default_factory=list)
    erros: list = field(default_factory=list)

    def as_dict(self):
        return {
            "abas_processadas": self.abas_processadas,
            "totais": {
                "criados": len(self.criados),
                "atualizados": len(self.atualizados),
                "desligados": len(self.desligados),
                "ignorados": len(self.ignorados),
                "erros": len(self.erros),
            },
            "criados": self.criados,
            "atualizados": self.atualizados,
            "desligados": self.desligados,
            "ignorados": self.ignorados,
            "erros": self.erros,
        }


class UsuarioImportService:
    COLUNAS_RH = {
        "matricula": ["matricula", "matrícula"],
        "nome": ["nome"],
        "status": ["observacao", "observação", "status"],
        "data_inicio": ["data admissao", "data admissão"],
        "funcao": ["cargo basico descricao", "cargo básico descrição", "cargo basico descricao"],
        "centro_custo_numero": ["centro custo", "centro de custo"],
        "centro_custo_descricao": ["centro custo descricao", "centro custo descrição", "centro custo-descricao", "centro custo-descrição"],
        "estabelecimento": ["estabelecimento descricao", "estabelecimento descrição"],
        "data_termino": ["data desligamento", "data demissao", "data demissão"],
        "localidade": ["unid lotacao descricao", "unid lotação descrição", "unidade lotacao descricao", "unidade lotação descrição"],
        "email": ["email", "e-mail"],
        "pmb": ["pmb"],
    }

    def __init__(
        self,
        arquivo,
        user,
        modo_importacao="ultima_aba",
        nome_aba=None,
        desligar_ausentes=False,
    ):
        self.arquivo = arquivo
        self.user = user
        self.modo_importacao = modo_importacao or "ultima_aba"
        self.nome_aba = nome_aba
        self.desligar_ausentes = desligar_ausentes

        self.resultado = ResultadoImportacao()
        self.status_ativo = status_value("ativo")
        self.status_desligado = status_value("desligado")

        self.matriculas_processadas = set()
        self.nomes_processados = set()

    def executar(self):
        wb = load_workbook(self.arquivo, data_only=True)
        abas = selecionar_abas(wb, self.modo_importacao, self.nome_aba)

        with transaction.atomic():
            for nome_aba in abas:
                ws = wb[nome_aba]
                self.resultado.abas_processadas.append(nome_aba)

                headers = self._mapear_headers(ws)

                if "nome" not in headers:
                    self.resultado.erros.append(
                        f"Aba '{nome_aba}' ignorada: coluna Nome não encontrada."
                    )
                    continue

                for row_idx in range(2, ws.max_row + 1):
                    dados = self._ler_linha(ws, row_idx, headers)

                    if not any(dados.values()):
                        continue

                    self._processar_linha(nome_aba, row_idx, dados)

            if self.desligar_ausentes:
                self._desligar_ausentes()

        return self.resultado.as_dict()

    def _mapear_headers(self, ws):
        headers = {}

        primeira_linha = next(ws.iter_rows(min_row=1, max_row=1, values_only=True))

        for idx, coluna in enumerate(primeira_linha, start=1):
            coluna_norm = normalizar_texto(coluna)

            if not coluna_norm:
                continue

            for campo, aliases in self.COLUNAS_RH.items():
                aliases_norm = [normalizar_texto(alias) for alias in aliases]

                if coluna_norm in aliases_norm:
                    headers[campo] = idx
                    break

        return headers

    def _ler_linha(self, ws, row_idx, headers):
        dados = {}

        for campo, col_idx in headers.items():
            dados[campo] = ws.cell(row=row_idx, column=col_idx).value

        return dados

    def _processar_linha(self, nome_aba, row_idx, dados):
        nome = str(dados.get("nome") or "").strip()
        matricula = normalizar_matricula(dados.get("matricula"))

        if not nome:
            self.resultado.ignorados.append({
                "aba": nome_aba,
                "linha": row_idx,
                "motivo": "Linha sem nome.",
            })
            return

        nome_norm = normalizar_texto(nome)

        if matricula:
            self.matriculas_processadas.add(matricula)

        if nome_norm:
            self.nomes_processados.add(nome_norm)

        usuario = None

        if matricula:
            usuario = Usuario.objects.filter(matricula__iexact=matricula).first()

        if not usuario:
            usuario = encontrar_usuario_por_nome(nome)

        data_inicio = parse_excel_date(dados.get("data_inicio"))
        data_termino = parse_excel_date(dados.get("data_termino"))
        status_planilha = dados.get("status")
        desligado = is_status_desligado(status_planilha, data_termino)

        centro_custo = buscar_ou_criar_centro_custo(
            dados.get("centro_custo_numero"),
            dados.get("centro_custo_descricao"),
        )

        localidade_nome = dados.get("localidade") or dados.get("estabelecimento")
        localidade = buscar_ou_criar_localidade(localidade_nome)
        funcao = buscar_ou_criar_funcao(dados.get("funcao"))

        # Regra automática de PMB:
        # Todo funcionário pertencente ao centro de custo TABACO recebe PMB = sim.
        if centro_custo_eh_tabaco(centro_custo):
            pmb_valor = "sim"
        else:
            pmb_valor = sim_nao(dados.get("pmb"))

        email_planilha = str(dados.get("email") or "").strip() or None

        if usuario:
            email = email_planilha or usuario.email or gerar_email_usuario(
                nome=nome,
                matricula=matricula,
                usuario_id=usuario.pk,
            )
        else:
            email = email_planilha or gerar_email_usuario(
                nome=nome,
                matricula=matricula,
            )

        payload = {
            "matricula": matricula,
            "nome": nome,
            "email": email,
            "status": self.status_desligado if desligado else self.status_ativo,
            "data_inicio": data_inicio,
            "data_termino": data_termino if desligado else None,
            "pmb": pmb_valor,
            "centro_custo": centro_custo,
            "localidade": localidade,
            "funcao": funcao,
        }

        if usuario:
            self._atualizar_usuario(usuario, payload, desligado, nome_aba, row_idx)
            return

        self._criar_usuario(payload, desligado, nome_aba, row_idx)

    def _atualizar_usuario(self, usuario, payload, desligado, nome_aba, row_idx):
        alterados = []

        for campo, valor in payload.items():
            if campo == "matricula" and not valor:
                continue

            if campo == "data_inicio" and not valor and usuario.data_inicio:
                continue

            if campo in ("centro_custo", "localidade", "funcao") and valor is None:
                continue

            if getattr(usuario, campo) != valor:
                setattr(usuario, campo, valor)
                alterados.append(campo)

        if hasattr(usuario, "atualizado_por"):
            usuario.atualizado_por = self.user
            alterados.append("atualizado_por")

        if alterados:
            usuario.save(update_fields=list(set(alterados)))

            destino = self.resultado.desligados if desligado else self.resultado.atualizados

            destino.append({
                "aba": nome_aba,
                "linha": row_idx,
                "nome": usuario.nome,
                "matricula": usuario.matricula or "—",
                "email": usuario.email or "—",
                "acao": "desligado" if desligado else "atualizado",
                "campos": ", ".join(sorted(set(alterados))),
            })
        else:
            self.resultado.ignorados.append({
                "aba": nome_aba,
                "linha": row_idx,
                "nome": usuario.nome,
                "matricula": usuario.matricula or "—",
                "motivo": "Sem alterações.",
            })

    def _criar_usuario(self, payload, desligado, nome_aba, row_idx):
        if not payload.get("data_inicio"):
            payload["data_inicio"] = timezone.localdate()

        novo = Usuario(**payload)

        if hasattr(novo, "criado_por"):
            novo.criado_por = self.user

        if hasattr(novo, "atualizado_por"):
            novo.atualizado_por = self.user

        novo.save()

        destino = self.resultado.desligados if desligado else self.resultado.criados

        destino.append({
            "aba": nome_aba,
            "linha": row_idx,
            "nome": novo.nome,
            "matricula": novo.matricula or "—",
            "email": novo.email or "—",
            "acao": "criado como desligado" if desligado else "criado",
        })

    def _desligar_ausentes(self):
        ativos = Usuario.objects.filter(status=self.status_ativo)

        for usuario in ativos:
            matricula = usuario.matricula
            nome_norm = normalizar_texto(usuario.nome)

            presente_por_matricula = matricula and matricula in self.matriculas_processadas
            presente_por_nome = nome_norm and nome_norm in self.nomes_processados

            if presente_por_matricula or presente_por_nome:
                continue

            usuario.status = self.status_desligado
            usuario.data_termino = timezone.localdate()

            update_fields = ["status", "data_termino"]

            if hasattr(usuario, "atualizado_por"):
                usuario.atualizado_por = self.user
                update_fields.append("atualizado_por")

            usuario.save(update_fields=update_fields)

            self.resultado.desligados.append({
                "aba": "ausente",
                "linha": "-",
                "nome": usuario.nome,
                "matricula": usuario.matricula or "—",
                "email": usuario.email or "—",
                "acao": "desligado por ausência na planilha mensal",
            })