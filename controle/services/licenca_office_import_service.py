import unicodedata
from datetime import date, datetime

from django.core.exceptions import ValidationError

from ProjetoEstoque.models import Item, LicencaOffice, SimNaoChoices, StatusLicencaOfficeChoices


def _norm(s):
    """minúsculas, sem acento, espaços internos colapsados e sem sobra nas pontas —
    usado tanto para casar cabeçalhos da planilha quanto para casar 'Estação' com
    `Item.nome` sem depender de digitação idêntica."""
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return " ".join(s.strip().lower().split())


def _parse_data(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    texto = str(value).strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(texto, fmt).date()
        except ValueError:
            continue
    return None


def _texto(value):
    if value is None:
        return ""
    return str(value).strip()


class LicencaOfficeImportService:
    """
    Importa/atualiza `LicencaOffice` a partir de uma planilha .xlsx no formato
    do controle de licenças Office por máquina — colunas: Produto, Serial,
    Status, Conta, ID Principal Dell, Data da Licença, Estação, Usuário
    vinculado com a máquina.

    Upsert por `chave_produto` (Serial): reimportar a mesma planilha
    ATUALIZA os registros existentes em vez de duplicá-los. A coluna
    "Estação" tenta casar com `Item.nome` (sem acento/maiúscula) para
    vincular automaticamente o equipamento; quando não encontra (ou o
    equipamento já está vinculado a outra licença), a linha é salva sem
    vínculo e reportada como aviso — nunca bloqueia o restante da importação.
    """

    # cabeçalho normalizado (ver `_norm`) -> nome do campo em `LicencaOffice`
    _COLUNA_POR_HEADER = {
        "produto": "produto",
        "serial": "chave_produto",
        "chave": "chave_produto",
        "chave do produto": "chave_produto",
        "chave produto": "chave_produto",
        "status": "status",
        "conta": "conta_vinculada",
        "conta vinculada": "conta_vinculada",
        "id principal dell": "id_dell",
        "id dell": "id_dell",
        "service tag": "id_dell",
        "data da licenca": "data_licenca",
        "data licenca": "data_licenca",
        "estacao": "estacao_importada",
        "usuario vinculado com a maquina": "usuario_vinculado",
        "usuario vinculado": "usuario_vinculado",
    }

    _STATUS_POR_TEXTO = {
        "ok": StatusLicencaOfficeChoices.OK,
        "problema": StatusLicencaOfficeChoices.PROBLEMA,
        "com problema": StatusLicencaOfficeChoices.PROBLEMA,
        "expirada": StatusLicencaOfficeChoices.EXPIRADA,
        "vencida": StatusLicencaOfficeChoices.EXPIRADA,
        "cancelada": StatusLicencaOfficeChoices.CANCELADA,
        "cancelado": StatusLicencaOfficeChoices.CANCELADA,
    }

    @classmethod
    def importar(cls, *, arquivo, user):
        """Retorna (criados, atualizados, vinculados, erros)."""
        from openpyxl import load_workbook

        try:
            wb = load_workbook(arquivo, data_only=True)
        except Exception:
            raise ValidationError("Não foi possível ler o arquivo — envie uma planilha .xlsx válida.")

        ws = wb.active
        primeira_linha = next(ws.iter_rows(min_row=1, max_row=1), None)
        if primeira_linha is None:
            raise ValidationError("A planilha está vazia.")

        col_map = {}
        for idx, cell in enumerate(primeira_linha):
            campo = cls._COLUNA_POR_HEADER.get(_norm(cell.value))
            if campo and campo not in col_map:
                col_map[campo] = idx

        faltando = {"produto", "chave_produto"} - col_map.keys()
        if faltando:
            raise ValidationError(
                "Planilha sem as colunas obrigatórias Produto e/ou Serial. Use os cabeçalhos do "
                "modelo: Produto, Serial, Status, Conta, ID Principal Dell, Data da Licença, "
                "Estação, Usuário vinculado com a máquina."
            )

        def valor(row, campo):
            idx = col_map.get(campo)
            return row[idx].value if idx is not None else None

        itens_por_nome = {
            _norm(nome): pk
            for pk, nome in Item.objects.filter(item_consumo=SimNaoChoices.NAO).values_list("id", "nome")
        }

        criados = atualizados = vinculados = 0
        erros = []

        for row_idx, row in enumerate(ws.iter_rows(min_row=2), start=2):
            produto = _texto(valor(row, "produto"))
            chave = _texto(valor(row, "chave_produto"))

            if not produto and not chave:
                continue  # linha em branco

            if not produto or not chave:
                erros.append(f"Linha {row_idx}: Produto e Serial são obrigatórios.")
                continue

            status = cls._STATUS_POR_TEXTO.get(
                _norm(valor(row, "status")), StatusLicencaOfficeChoices.OK
            )
            estacao = _texto(valor(row, "estacao_importada"))

            obj = LicencaOffice.objects.filter(chave_produto=chave).first()
            if obj is None:
                obj = LicencaOffice(chave_produto=chave, criado_por=user)
                criados += 1
            else:
                atualizados += 1

            obj.produto = produto
            obj.status = status
            obj.conta_vinculada = _texto(valor(row, "conta_vinculada"))
            obj.id_dell = _texto(valor(row, "id_dell"))
            obj.data_licenca = _parse_data(valor(row, "data_licenca"))
            obj.estacao_importada = estacao
            obj.usuario_vinculado = _texto(valor(row, "usuario_vinculado"))
            obj.atualizado_por = user

            if estacao:
                item_id = itens_por_nome.get(_norm(estacao))
                if not item_id:
                    erros.append(
                        f'Linha {row_idx}: estação "{estacao}" não encontrada no cadastro de '
                        "equipamentos — licença salva sem vínculo (vincule manualmente depois)."
                    )
                else:
                    outro_vinculo = (
                        LicencaOffice.objects.filter(item_id=item_id).exclude(pk=obj.pk).first()
                    )
                    if outro_vinculo:
                        erros.append(
                            f'Linha {row_idx}: equipamento "{estacao}" já está vinculado à licença '
                            f"{outro_vinculo.chave_produto} — esta linha foi salva sem vínculo."
                        )
                    else:
                        obj.item_id = item_id
                        vinculados += 1

            obj.full_clean()
            obj.save()

        return criados, atualizados, vinculados, erros
