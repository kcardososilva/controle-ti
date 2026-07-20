from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from decimal import Decimal
from .models import (
    Categoria, Subtipo, Localidade, Fornecedor, CentroCusto, Funcao, Usuario,
    Item, Locacao, Comentario, CicloManutencao, MovimentacaoItem,
    StatusItemChoices, TipoMovimentacaoChoices, TipoTransferenciaChoices,
    LocalidadeChoices, CheckListModelo, CheckListPergunta, Preventiva,
    TipoRespostaChoices, SimNaoChoices, Licenca, MovimentacaoLicenca,
    TipoMovLicencaChoices, LicencaLote, LoteEstoque, ItemLote, PlantaProjeto,
    OrdemManutencao, StatusOrdemManutencaoChoices,
    Requisicao, RequisicaoItem, ComentarioRequisicaoItem, ItemPadraoDatasul,
)

BASE_CTRL_CSS = {
    "class": "ctrl",
    "style": "height:48px;border-radius:12px;background:#f8fafc;border:1px solid #d5deea;padding:10px 12px;"
}

# ================== BASES ==================
class CategoriaForm(forms.ModelForm):
    class Meta:
        model = Categoria
        fields = ["nome"]


class SubtipoForm(forms.ModelForm):
    class Meta:
        model = Subtipo
        fields = ["nome", "alocado", "categoria"]
        widgets = {
            "nome": forms.TextInput(attrs={"class": "ctrl", "placeholder": "Ex.: Notebook, Monitor, Headset…"}),
            "alocado": forms.Select(attrs={"class": "ctrl"}),
            "categoria": forms.Select(attrs={"class": "ctrl"}),
        }


class LocalidadeForm(forms.ModelForm):
    class Meta:
        model = Localidade
        fields = ["codigo", "local"]
        widgets = {
            "codigo": forms.Select(attrs=BASE_CTRL_CSS | {"id": "id_codigo"}),
            "local": forms.TextInput(attrs=BASE_CTRL_CSS | {"placeholder": "Ex.: Karitel"}),
        }

    def clean(self):
        cleaned = super().clean()
        codigo = cleaned.get("codigo")
        local = cleaned.get("local")
        if codigo and not local:
            label = dict(LocalidadeChoices.choices).get(codigo)
            cleaned["local"] = label
        return cleaned


class FornecedorForm(forms.ModelForm):
    class Meta:
        model = Fornecedor
        fields = ["nome", "cnpj", "contrato"]


class CentroCustoForm(forms.ModelForm):
    class Meta:
        model = CentroCusto
        fields = ["numero", "departamento", "pmb"]
        widgets = {
            "numero": forms.TextInput(attrs={"class": "ctrl", "placeholder": "Ex.: 1101"}),
            "departamento": forms.TextInput(attrs={"class": "ctrl", "placeholder": "Ex.: TI Corporativo"}),
            "pmb": forms.Select(attrs={"class": "ctrl"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for f in self.fields.values():
            css = f.widget.attrs.get("class", "")
            if "ctrl" not in css:
                f.widget.attrs["class"] = (css + " ctrl").strip()


class FuncaoForm(forms.ModelForm):
    class Meta:
        model = Funcao
        fields = ["nome"]
        widgets = {
            "nome": forms.TextInput(attrs={
                "class": "ctrl",
                "placeholder": "Ex.: Técnico de TI",
            })
        }


# Widget único para datas: nativo do browser e com formato ISO (YYYY-MM-DD)
DATE_WIDGET = forms.DateInput(
    attrs={
        "type": "date",
        "class": "ctrl",
    },
    format="%Y-%m-%d",
)


class UsuarioForm(forms.ModelForm):
    class Meta:
        model = Usuario
        fields = [
            "matricula",
            "nome",
            "status",
            "data_inicio",
            "data_termino",
            "pmb",
            "email",
            "centro_custo",
            "localidade",
            "funcao",
        ]

        widgets = {
            "data_inicio": DATE_WIDGET,
            "data_termino": DATE_WIDGET,
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["data_inicio"].input_formats = ["%Y-%m-%d"]
        self.fields["data_termino"].input_formats = ["%Y-%m-%d"]

        for name, field in self.fields.items():
            existing_class = field.widget.attrs.get("class", "")
            if "ctrl" not in existing_class:
                field.widget.attrs["class"] = f"{existing_class} ctrl".strip()

        for name in ("status", "pmb", "centro_custo", "localidade", "funcao"):
            if name in self.fields:
                existing = self.fields[name].widget.attrs.get("class", "")
                if "select2" not in existing:
                    self.fields[name].widget.attrs["class"] = f"{existing} select2".strip()
                self.fields[name].widget.attrs.setdefault("data-placeholder", "Selecione...")

        self.fields["matricula"].widget.attrs.update({
            "placeholder": "Ex: 12345",
            "autocomplete": "off",
        })

        self.fields["nome"].widget.attrs.update({
            "placeholder": "Nome completo do funcionário",
            "autocomplete": "off",
        })

        self.fields["email"].widget.attrs.update({
            "placeholder": "email@empresa.com.br",
            "autocomplete": "off",
        })

        self.fields["email"].required = False
        self.fields["matricula"].required = False
        self.fields["centro_custo"].required = False
        self.fields["localidade"].required = False
        self.fields["funcao"].required = False
        self.fields["data_termino"].required = False

        self.fields["centro_custo"].queryset = CentroCusto.objects.order_by("numero", "departamento")
        self.fields["centro_custo"].empty_label = "—"

        self.fields["localidade"].queryset = Localidade.objects.order_by("local")
        self.fields["localidade"].empty_label = "—"

        self.fields["funcao"].queryset = Funcao.objects.order_by("nome")
        self.fields["funcao"].empty_label = "—"

        if self.instance and self.instance.pk and not self.is_bound:
            if self.instance.data_inicio:
                self.fields["data_inicio"].initial = self.instance.data_inicio
                self.fields["data_inicio"].widget.attrs["value"] = self.instance.data_inicio.strftime("%Y-%m-%d")

            if self.instance.data_termino:
                self.fields["data_termino"].initial = self.instance.data_termino
                self.fields["data_termino"].widget.attrs["value"] = self.instance.data_termino.strftime("%Y-%m-%d")

    def clean_matricula(self):
        matricula = self.cleaned_data.get("matricula")

        if not matricula:
            return None

        matricula = str(matricula).strip()

        qs = Usuario.objects.filter(matricula__iexact=matricula)

        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)

        if qs.exists():
            raise forms.ValidationError("Já existe um funcionário cadastrado com esta matrícula.")

        return matricula

    def clean(self):
        data = super().clean()

        if self.instance and self.instance.pk:
            posted_raw = self.data.get(self.add_prefix("data_inicio"), None)
            if posted_raw in (None, "") and self.instance.data_inicio:
                data["data_inicio"] = self.instance.data_inicio

        di = data.get("data_inicio")
        dt = data.get("data_termino")

        if di and dt and dt < di:
            self.add_error("data_termino", "A data de término não pode ser anterior à data de início.")

        return data

    def save(self, commit=True):
        instance = super().save(commit=False)

        if self.instance and self.instance.pk:
            posted_raw = self.data.get(self.add_prefix("data_inicio"), None)
            if posted_raw in (None, "") and self.instance.data_inicio:
                instance.data_inicio = self.instance.data_inicio

        if commit:
            instance.save()
            self.save_m2m()

        return instance


class ImportarUsuariosForm(forms.Form):
    MODO_IMPORTACAO_CHOICES = [
        ("ultima_aba", "Importar somente a aba mensal mais recente"),
        ("todas_abas", "Importar todas as abas da planilha"),
        ("aba_especifica", "Importar uma aba específica"),
    ]

    arquivo = forms.FileField(
        label="Planilha Excel do RH",
        help_text="Envie a planilha mensal do RH no formato .xlsx.",
        widget=forms.FileInput(attrs={
            "class": "ctrl",
            "accept": ".xlsx",
        })
    )

    modo_importacao = forms.ChoiceField(
        label="Modo de importação",
        choices=MODO_IMPORTACAO_CHOICES,
        initial="ultima_aba",
        widget=forms.Select(attrs={
            "class": "ctrl",
        })
    )

    nome_aba = forms.CharField(
        label="Nome da aba",
        required=False,
        help_text="Preencha somente se escolher importação por aba específica. Exemplo: Abr 2026.",
        widget=forms.TextInput(attrs={
            "class": "ctrl",
            "placeholder": "Ex: Abr 2026",
        })
    )

    desligar_ausentes = forms.BooleanField(
        required=False,
        initial=False,
        label="Desligar funcionários ausentes na aba importada",
        help_text="Use apenas se a aba mensal representar a base oficial completa de funcionários ativos."
    )
    
# ================== ITEM ==================

def formatar_data_para_input_html(data):
    """
    Converte DateField para o formato aceito por input type=date.
    Evita campo vazio ao editar registros existentes.
    """
    if not data:
        return None

    return data.strftime("%Y-%m-%d")

class ItemForm(forms.ModelForm):
    data_compra = forms.DateField(
        required=False,
        widget=forms.DateInput(
            format="%Y-%m-%d",
            attrs={
                "type": "date",
                "class": "form-control",
            }
        ),
        input_formats=["%Y-%m-%d", "%d/%m/%Y"],
    )

    # Renderiza o BooleanField do modelo como um Select "Sim/Não", para manter
    # o mesmo padrão visual dos campos "locado" e "item_consumo".
    compartilhado = forms.TypedChoiceField(
        required=False,
        label="Pode ser compartilhado?",
        choices=((False, "Não"), (True, "Sim")),
        coerce=lambda v: v in (True, "True", "true", "1", 1),
        empty_value=False,
        widget=forms.Select(attrs={"class": "form-control"}),
    )

    class Meta:
        model = Item
        fields = [
            "locado",
            "compartilhado",
            "nome",
            "numero_serie",
            "marca",
            "modelo",
            "subtipo",
            "localidade",
            "status",
            "centro_custo",
            "fornecedor",
            "quantidade",
            "item_consumo",
            "pmb",
            "precisa_preventiva",
            "data_limite_preventiva",
            "valor",
            "data_compra",
            "numero_pedido",
            "observacoes",
        ]

        widgets = {
            "locado": forms.Select(attrs={"class": "form-control"}),
            "nome": forms.TextInput(attrs={"class": "form-control"}),
            "numero_serie": forms.TextInput(attrs={"class": "form-control"}),
            "marca": forms.TextInput(attrs={"class": "form-control"}),
            "modelo": forms.TextInput(attrs={"class": "form-control"}),
            "subtipo": forms.Select(attrs={"class": "form-control"}),
            "localidade": forms.Select(attrs={"class": "form-control"}),
            "status": forms.Select(attrs={"class": "form-control"}),
            "centro_custo": forms.Select(attrs={"class": "form-control"}),
            "fornecedor": forms.Select(attrs={"class": "form-control"}),
            "quantidade": forms.NumberInput(attrs={"class": "form-control", "min": "1"}),
            "item_consumo": forms.Select(attrs={"class": "form-control"}),
            "pmb": forms.Select(attrs={"class": "form-control"}),
            "precisa_preventiva": forms.Select(attrs={"class": "form-control"}),
            "data_limite_preventiva": forms.NumberInput(attrs={"class": "form-control", "min": "1"}),
            "valor": forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0"}),
            "numero_pedido": forms.TextInput(attrs={"class": "form-control"}),
            "observacoes": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["quantidade"].required = False
        self.fields["valor"].required = False
        self.fields["data_compra"].required = False
        self.fields["numero_pedido"].required = False
        self.fields["fornecedor"].required = False
        self.fields["data_limite_preventiva"].required = False

        if self.instance and self.instance.pk:
            self.initial["data_compra"] = formatar_data_para_input_html(
                self.instance.data_compra
            )

    def clean(self):
        cleaned = super().clean()

        locado = cleaned.get("locado")
        item_consumo = cleaned.get("item_consumo")
        precisa_preventiva = cleaned.get("precisa_preventiva")
        data_limite_preventiva = cleaned.get("data_limite_preventiva")
        quantidade = cleaned.get("quantidade")

        eh_locado = locado == SimNaoChoices.SIM
        eh_consumo = item_consumo == SimNaoChoices.SIM

        if eh_consumo and eh_locado:
            self.add_error("item_consumo", "Item de consumo não pode ser cadastrado como locado.")
            self.add_error("locado", "Item locado deve ser um ativo/equipamento, não item de consumo.")

        if eh_consumo and cleaned.get("compartilhado"):
            self.add_error(
                "compartilhado",
                "Item de consumo não pode ser compartilhado (ele é controlado por estoque/lote).",
            )

        if precisa_preventiva == SimNaoChoices.SIM and not data_limite_preventiva:
            self.add_error("data_limite_preventiva", "Informe a periodicidade da preventiva em dias.")

        if precisa_preventiva == SimNaoChoices.NAO:
            cleaned["data_limite_preventiva"] = None

        if not eh_consumo:
            if not quantidade or quantidade <= 0:
                self.add_error("quantidade", "Informe uma quantidade válida.")

        if eh_locado:
            cleaned["data_compra"] = None
            cleaned["numero_pedido"] = None

        # Enquanto o equipamento está em manutenção (status=MANUTENCAO, seja via
        # OS do Portal do Fornecedor ou via Ciclo de Manutenção interno), o
        # status não pode ser alterado manualmente por aqui — só pelo fluxo de
        # retorno legítimo (conclusão da OS ou encerramento do Ciclo), que já
        # grava o novo status diretamente e não passa por este form.
        if (
            self.instance
            and self.instance.pk
            and self.instance.status == StatusItemChoices.MANUTENCAO
            and cleaned.get("status") != StatusItemChoices.MANUTENCAO
        ):
            self.add_error(
                "status",
                "Este equipamento está em manutenção — o status não pode ser "
                "alterado manualmente. Conclua a Ordem de Manutenção ou encerre "
                "o Ciclo de Manutenção para liberá-lo.",
            )

        return cleaned


class LocacaoForm(forms.ModelForm):
    data_entrada = forms.DateField(
        required=False,
        widget=forms.DateInput(
            format="%Y-%m-%d",
            attrs={
                "type": "date",
                "class": "form-control",
            }
        ),
        input_formats=["%Y-%m-%d", "%d/%m/%Y"],
    )

    class Meta:
        model = Locacao
        fields = [
            "tempo_locado",
            "valor_mensal",
            "data_entrada",
            "contrato",
        ]

        widgets = {
            "tempo_locado": forms.NumberInput(attrs={"class": "form-control", "min": "1"}),
            "valor_mensal": forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0"}),
            "contrato": forms.TextInput(attrs={"class": "form-control"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self.instance and self.instance.pk:
            self.initial["data_entrada"] = formatar_data_para_input_html(
                self.instance.data_entrada
            )

    def clean(self):
        cleaned = super().clean()

        if not cleaned.get("data_entrada"):
            self.add_error("data_entrada", "Informe a data de entrada da locação.")

        if not cleaned.get("tempo_locado"):
            self.add_error("tempo_locado", "Informe o tempo de contrato em meses.")

        if cleaned.get("valor_mensal") is None:
            self.add_error("valor_mensal", "Informe o valor mensal da locação.")

        return cleaned
# ================== COMENTÁRIO ==================
class ComentarioForm(forms.ModelForm):
    class Meta:
        model = Comentario
        fields = ["texto", "item"]

# ================== MANUTENÇÃO ==================
class CicloManutencaoForm(forms.ModelForm):
    class Meta:
        model = CicloManutencao
        # item é injetado pela view via URL — nunca exposto ao usuário
        fields = ["status_inicial", "data_inicio", "data_fim", "causa", "custo"]
        widgets = {
            "data_inicio": DATE_WIDGET,
            "data_fim": DATE_WIDGET,
            "causa": forms.Textarea(attrs={"rows": 3}),
            "custo": forms.NumberInput(attrs={"step": "0.01"}),
        }

# ================== MOVIMENTAÇÃO ==================
class MovimentacaoItemForm(forms.ModelForm):
    lote_fornecedor = forms.ModelChoiceField(
        queryset=Fornecedor.objects.all(),
        required=False,
        label="Fornecedor do Lote",
        widget=forms.Select(attrs={"class": "form-control"})
    )

    lote_data_entrada = forms.DateField(
        required=False,
        label="Data de Entrada do Lote",
        input_formats=["%Y-%m-%d", "%d/%m/%Y"],
        widget=forms.DateInput(attrs={
            "class": "form-control",
            "type": "date"
        })
    )

    lote_numero_nf = forms.CharField(
        required=False,
        label="Número da NF",
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "Ex: 123456"
        })
    )

    lote_quantidade = forms.IntegerField(
        required=False,
        label="Quantidade de Entrada",
        min_value=1,
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "min": "1"
        })
    )

    lote_custo_unitario = forms.DecimalField(
        required=False,
        label="Custo Unitário",
        min_value=Decimal("0.01"),
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "step": "0.01",
            "min": "0.01"
        })
    )

    lote_observacao_tecnica = forms.CharField(
        required=False,
        label="Observação Técnica do Lote",
        widget=forms.Textarea(attrs={
            "class": "form-control",
            "rows": 3,
            "placeholder": "Descreva observações técnicas, fiscais ou de recebimento do lote."
        })
    )

    # Renomear o equipamento direto na transferência de equipamento (opcional).
    # Em branco mantém o nome atual; só renomeia quando preenchido e diferente.
    novo_nome = forms.CharField(
        required=False,
        max_length=100,
        label="Renomear equipamento",
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "maxlength": "100",
            "placeholder": "Deixe em branco para manter o nome atual",
        })
    )

    class Meta:
        model = MovimentacaoItem
        fields = [
            "tipo_movimentacao",
            "tipo_transferencia",
            "item",
            "usuario",
            "quantidade",
            "lote",
            "localidade_destino",
            "centro_custo_destino",
            "fornecedor_manutencao",
            "numero_pedido",
            "observacao",
            "chamado",
            "custo",
            "termo_pdf",
            "status_transferencia",
            "status_retorno",
        ]

        widgets = {
            "tipo_movimentacao": forms.Select(attrs={"class": "form-control"}),
            "item": forms.Select(attrs={"class": "form-control"}),
            "usuario": forms.Select(attrs={"class": "form-control"}),
            "lote": forms.Select(attrs={"class": "form-control"}),
            "localidade_destino": forms.Select(attrs={"class": "form-control"}),
            "centro_custo_destino": forms.Select(attrs={"class": "form-control"}),
            "fornecedor_manutencao": forms.Select(attrs={"class": "form-control"}),
            "status_transferencia": forms.Select(attrs={"class": "form-control"}),
            "status_retorno": forms.Select(attrs={"class": "form-control"}),

            "observacao": forms.Textarea(attrs={
                "class": "form-control",
                "rows": 3,
                "placeholder": "Descreva detalhes importantes..."
            }),

            "numero_pedido": forms.TextInput(attrs={"class": "form-control"}),
            "chamado": forms.TextInput(attrs={"class": "form-control"}),
            "custo": forms.NumberInput(attrs={
                "class": "form-control",
                "step": "0.01",
                "min": "0"
            }),
            "tipo_transferencia": forms.RadioSelect(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        for field in self.fields.values():
            field.required = False

        # "Retorno de Manutenção" foi descontinuado como lançamento manual:
        # o retorno agora é tratado pelo fluxo de aprovação de manutenção.
        # Removemos a opção do seletor, mantendo-a apenas quando se está editando
        # uma movimentação que já é desse tipo (para não quebrar registros antigos).
        editando_retorno = bool(
            self.instance
            and self.instance.pk
            and self.instance.tipo_movimentacao == TipoMovimentacaoChoices.RETORNO_MANUTENCAO
        )
        if not editando_retorno:
            self.fields["tipo_movimentacao"].choices = [
                choice
                for choice in self.fields["tipo_movimentacao"].choices
                if choice[0] != TipoMovimentacaoChoices.RETORNO_MANUTENCAO
            ]

        self.fields["lote"].queryset = (
            LoteEstoque.objects
            .none()
        )

        item_id = None

        if self.data:
            item_id = self.data.get("item") or self.data.get("id_item")

        elif self.instance and self.instance.pk and self.instance.item_id:
            item_id = self.instance.item_id

        if item_id:
            self.fields["lote"].queryset = (
                LoteEstoque.objects
                .filter(itens_vinculados__item_id=item_id)
                .distinct()
                .order_by("-data_entrada", "-created_at")
            )

        self.fields["lote"].label_from_instance = self._label_lote

        # Reaproveitado por 4 tipos (envio_manutencao, separacao_envio,
        # separacao_devolucao, devolucao_locacao): rótulo genérico em vez do
        # nome de campo do banco ("Fornecedor Manutenção").
        self.fields["fornecedor_manutencao"].label = "Fornecedor"

    def _label_lote(self, lote):
        item_lote = (
            ItemLote.objects
            .filter(lote=lote)
            .select_related("item", "item__centro_custo")
            .first()
        )

        saldo = item_lote.quantidade_disponivel if item_lote else lote.quantidade
        centro_custo = "-"

        if item_lote and item_lote.item and item_lote.item.centro_custo:
            centro_custo = str(item_lote.item.centro_custo)

        return f"NF {lote.numero_nf} | Saldo: {saldo} | CC: {centro_custo} | {lote.data_entrada:%d/%m/%Y}"

    def _checar_manutencao_duplicada(self, item):
        """Bloqueia reenviar um item que já está em manutenção — evita
        duplicidade quando o item já está fisicamente parado (com o
        fornecedor, via OS do Portal, OU internamente, via Ciclo de
        Manutenção — dois mecanismos independentes que levam ao mesmo
        `status=MANUTENCAO`; checar o status cobre os dois)."""
        if not item:
            return
        if item.status != StatusItemChoices.MANUTENCAO:
            return
        from services.ordem_manutencao_service import OrdemManutencaoService
        ordem_aberta = OrdemManutencaoService.ordem_aberta(item)
        # Erro não-vinculado ao campo "item" (None em vez de "item"): usar
        # add_error("item", ...) removeria "item" de cleaned_data, e o
        # model.clean() de MovimentacaoItem re-adicionaria um "Selecione o
        # item." confuso por cima desta mensagem (item_id ficaria vazio na
        # instância construída pelo ModelForm).
        if ordem_aberta:
            self.add_error(
                None,
                f'O equipamento "{item.nome}" já está em manutenção externa '
                f"(OS #{ordem_aberta.pk} — {ordem_aberta.get_status_display()}). "
                "Não é possível enviá-lo novamente enquanto essa ordem estiver aberta.",
            )
        else:
            self.add_error(
                None,
                f'O equipamento "{item.nome}" já está em manutenção (ciclo interno '
                "em andamento). Encerre o ciclo atual antes de enviá-lo novamente.",
            )

    def _checar_item_bloqueado_por_manutencao(self, item, tipo):
        """Uma vez em manutenção (status=MANUTENCAO), o item está fisicamente
        fora de uso — nenhuma outra movimentação que mexa em status/posse
        (entrega/devolução, transferência de equipamento, devolução de
        locação) faz sentido até ele voltar pelo fluxo de retorno legítimo
        (conclusão da OS ou encerramento do Ciclo)."""
        if not item or item.status != StatusItemChoices.MANUTENCAO:
            return
        self.add_error(
            None,
            f'O equipamento "{item.nome}" está em manutenção e não pode ser '
            "movimentado até retornar (conclua a Ordem de Manutenção ou encerre "
            "o Ciclo de Manutenção correspondente).",
        )

    def clean(self):
        cleaned = super().clean()

        tipo = cleaned.get("tipo_movimentacao")
        item = cleaned.get("item")
        lote = cleaned.get("lote")
        quantidade = cleaned.get("quantidade")

        if not tipo:
            self.add_error("tipo_movimentacao", "O tipo de movimentação é obrigatório.")
            return cleaned

        if not item:
            self.add_error("item", "Selecione o item.")
            return cleaned

        if tipo == "entrada":
            lote_quantidade = cleaned.get("lote_quantidade")

            if not cleaned.get("lote_fornecedor"):
                self.add_error("lote_fornecedor", "Informe o fornecedor do lote.")

            if not cleaned.get("lote_data_entrada"):
                self.add_error("lote_data_entrada", "Informe a data de entrada do lote.")

            if not cleaned.get("lote_numero_nf"):
                self.add_error("lote_numero_nf", "Informe o número da NF.")

            if not lote_quantidade or lote_quantidade <= 0:
                self.add_error("lote_quantidade", "Informe a quantidade de entrada.")

            if not cleaned.get("lote_custo_unitario"):
                self.add_error("lote_custo_unitario", "Informe o custo unitário.")

            if not cleaned.get("localidade_destino"):
                self.add_error("localidade_destino", "Informe a localidade de destino.")

            if not cleaned.get("centro_custo_destino"):
                self.add_error("centro_custo_destino", "Informe o centro de custo destino.")

        elif tipo == "baixa":
            if not lote:
                self.add_error("lote", "Selecione o lote que será baixado.")

            if not quantidade or quantidade <= 0:
                self.add_error("quantidade", "Informe a quantidade da baixa.")

            if not cleaned.get("usuario"):
                self.add_error("usuario", "Informe o solicitante da baixa.")

            if not cleaned.get("localidade_destino"):
                self.add_error("localidade_destino", "Informe a localidade.")

            if not cleaned.get("centro_custo_destino"):
                self.add_error("centro_custo_destino", "Informe o centro de custo destino.")

            if not cleaned.get("observacao"):
                self.add_error("observacao", "Justifique a baixa nas observações.")

        elif tipo == "transferencia":
            self._checar_item_bloqueado_por_manutencao(item, tipo)
            acao = cleaned.get("tipo_transferencia")

            if not acao:
                self.add_error("tipo_transferencia", "Selecione o tipo da transferência.")

            if not cleaned.get("termo_pdf"):
                self.add_error("termo_pdf", "O termo de responsabilidade em PDF é obrigatório.")

            if acao == "entrega":
                if not cleaned.get("usuario"):
                    self.add_error("usuario", "Selecione o usuário para entrega.")

                if not cleaned.get("localidade_destino"):
                    self.add_error("localidade_destino", "Informe a localidade de destino.")

                # O centro de custo do equipamento (CC proprietário) é o destino para
                # onde a devolução vai retornar o item. Se o equipamento for entregue
                # sem CC, a devolução não terá para onde voltar. Por isso, exige-se que
                # o equipamento tenha um centro de custo definido antes da entrega.
                # Itens compartilhados são isentos: eles não restauram CC na devolução
                # (são ativos compartilhados por vários setores).
                if item and not item.centro_custo_id and not item.compartilhado:
                    self.add_error(
                        None,
                        f'O equipamento "{item.nome}" está sem centro de custo (CC '
                        "proprietário). Defina o centro de custo do equipamento na tela "
                        "de edição do equipamento antes de fazer a entrega — sem ele, ao "
                        "ser devolvido o sistema não saberá para qual centro de custo "
                        "retornar o item.",
                    )

        elif tipo == "transferencia_equipamento":
            self._checar_item_bloqueado_por_manutencao(item, tipo)
            if not cleaned.get("localidade_destino"):
                self.add_error("localidade_destino", "Informe a nova localidade.")

            # Centro de custo NÃO é exigido aqui: o equipamento mantém o CC atual.

            if not cleaned.get("status_transferencia"):
                self.add_error("status_transferencia", "Informe o novo status.")

        elif tipo == "envio_manutencao":
            if not cleaned.get("observacao"):
                self.add_error("observacao", "Descreva o problema nas observações.")
            if not cleaned.get("fornecedor_manutencao"):
                self.add_error("fornecedor_manutencao", "Informe o fornecedor para onde o equipamento será enviado.")
            self._checar_manutencao_duplicada(item)

        elif tipo in ("retorno_manutencao", "retorno"):
            if not cleaned.get("localidade_destino"):
                self.add_error("localidade_destino", "Informe onde o item será guardado.")

        elif tipo in ("separacao_envio", "separacao_devolucao"):
            if not cleaned.get("fornecedor_manutencao"):
                self.add_error("fornecedor_manutencao", "Informe o fornecedor de destino da separação.")
            if tipo == "separacao_envio":
                self._checar_manutencao_duplicada(item)

        elif tipo == "devolucao_locacao":
            self._checar_item_bloqueado_por_manutencao(item, tipo)
            if not cleaned.get("fornecedor_manutencao"):
                self.add_error("fornecedor_manutencao", "Informe o fornecedor (locadora) de destino.")

            if item and str(item.locado) != "sim":
                self.add_error(
                    None,
                    f'O equipamento "{item.nome}" não está marcado como locado — a '
                    "devolução de locação só se aplica a itens com contrato de Locação.",
                )

        return cleaned
    
class LoteEstoqueCreateForm(forms.ModelForm):
    data_entrada = forms.DateField(
        required=False,
        widget=forms.DateInput(
            format="%Y-%m-%d",
            attrs={
                "type": "date",
                "class": "form-control",
            }
        ),
        input_formats=["%Y-%m-%d", "%d/%m/%Y"],
    )

    class Meta:
        model = LoteEstoque
        fields = [
            "fornecedor",
            "data_entrada",
            "numero_nf",
            "quantidade",
            "custo_unitario",
            "observacao_tecnica",
        ]

        widgets = {
            "fornecedor": forms.Select(attrs={"class": "form-control"}),
            "numero_nf": forms.TextInput(attrs={"class": "form-control", "placeholder": "Número da NF"}),
            "quantidade": forms.NumberInput(attrs={"class": "form-control", "min": "1"}),
            "custo_unitario": forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0.01"}),
            "observacao_tecnica": forms.Textarea(attrs={
                "class": "form-control",
                "rows": 3,
                "placeholder": "Observações técnicas do lote, recebimento ou conferência."
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self.instance and self.instance.pk:
            self.initial["data_entrada"] = formatar_data_para_input_html(
                self.instance.data_entrada
            )

    def clean(self):
        cleaned = super().clean()

        data_entrada = cleaned.get("data_entrada")
        quantidade = cleaned.get("quantidade")
        custo_unitario = cleaned.get("custo_unitario")

        if not data_entrada:
            self.add_error("data_entrada", "Informe a data de entrada do lote.")

        if not quantidade or quantidade <= 0:
            self.add_error("quantidade", "A quantidade do lote deve ser maior que zero.")

        if not custo_unitario or custo_unitario <= 0:
            self.add_error("custo_unitario", "O custo unitário deve ser maior que zero.")

        return cleaned
# ================== HELPERS/BASE ESTILO ==================
class BaseStyledForm(forms.ModelForm):
    """Aplica a classe .ctrl a todos os widgets automaticamente."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for f in self.fields.values():
            base_cls = f.widget.attrs.get("class", "")
            f.widget.attrs["class"] = (base_cls + " ctrl").strip()

class ChecklistModeloForm(BaseStyledForm):
    class Meta:
        model = CheckListModelo
        fields = ["nome", "subtipo", "ativo"]
        widgets = {
            "nome": forms.TextInput(attrs={"placeholder": "Ex.: Preventiva de Notebook"}),
        }

class ChecklistPerguntaForm(BaseStyledForm):
    class Meta:
        model = CheckListPergunta
        fields = ["texto_pergunta", "tipo_resposta", "obrigatorio"]
        widgets = {
            "texto_pergunta": forms.TextInput(attrs={"placeholder": "Descreva a pergunta…"}),
        }

class PreventivaStartForm(forms.Form):
    item = forms.ModelChoiceField(
        queryset=Item.objects.select_related("subtipo")
                           .filter(status__in=['ativo', 'backup', 'manutencao'])
                           .order_by("nome"),
        label="Equipamento / Ativo",
        widget=forms.Select(attrs={"class": "form-control select2", "data-placeholder": "Busque o ativo..."}),
        empty_label=None
    )
    
    checklist_modelo = forms.ModelChoiceField(
        queryset=CheckListModelo.objects.filter(ativo="sim").order_by("nome"),
        label="Modelo de Checklist",
        widget=forms.Select(attrs={"class": "form-control select2", "data-placeholder": "Selecione o serviço..."}),
        empty_label=None
    )

    def __init__(self, *args, **kwargs):
        item_instance = kwargs.pop("item_instance", None)
        super().__init__(*args, **kwargs)
        
        # Lógica de Filtro Dinâmico
        if item_instance:
            self.fields["item"].initial = item_instance.pk
            if item_instance.subtipo:
                self.fields["checklist_modelo"].queryset = (
                    CheckListModelo.objects
                    .filter(subtipo=item_instance.subtipo, ativo="sim")
                    .order_by("nome")
                )
        
        # Se houve post e o item mudou
        elif self.data and self.data.get('item'):
            try:
                item_id = int(self.data.get('item'))
                item_obj = Item.objects.get(pk=item_id)
                if item_obj.subtipo:
                    self.fields["checklist_modelo"].queryset = (
                        CheckListModelo.objects
                        .filter(subtipo=item_obj.subtipo, ativo="sim")
                        .order_by("nome")
                    )
            except Exception:
                pass

# ================== LICENÇAS ==================
ISO_FMT = "%Y-%m-%d"

class LicencaForm(forms.ModelForm):
    class Meta:
        model = Licenca
        fields = ["nome", "fornecedor", "centro_custo", "pmb", "observacao"]

        widgets = {
            "nome": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "Ex: Microsoft Office 365"
            }),
            "fornecedor": forms.Select(attrs={"class": "form-control"}),
            "centro_custo": forms.Select(attrs={"class": "form-control"}),
            "pmb": forms.Select(attrs={"class": "form-control"}),
            "observacao": forms.Textarea(attrs={
                "class": "form-control",
                "rows": 4,
                "placeholder": "Insira detalhes adicionais aqui..."
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["fornecedor"].empty_label = "Selecione um Fornecedor..."
        self.fields["centro_custo"].empty_label = "CC proprietário (devolução retorna aqui)"
        self.fields["centro_custo"].required = False

class MovimentacaoLicencaForm(forms.ModelForm):
    lote_id_select = forms.CharField(required=False, widget=forms.HiddenInput())

    class Meta:
        model = MovimentacaoLicenca
        fields = ["tipo", "licenca", "usuario", "observacao"]
        widgets = {
            "tipo": forms.Select(attrs={"class": "form-control"}),
            "licenca": forms.Select(attrs={"class": "form-control", "id": "id_licenca"}),
            "usuario": forms.Select(attrs={"class": "form-control"}),
            "observacao": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }

    def clean(self):
        cleaned_data = super().clean()
        # (Mantém a mesma validação robusta de antes)
        tipo = cleaned_data.get("tipo")
        licenca = cleaned_data.get("licenca")
        usuario = cleaned_data.get("usuario")
        lote_id = cleaned_data.get("lote_id_select")

        if not licenca or not tipo: return cleaned_data

        if tipo == TipoMovLicencaChoices.ATRIBUICAO:
            if not usuario:
                self.add_error('usuario', "Informe o colaborador.")
                return cleaned_data
            
            last_mov = MovimentacaoLicenca.objects.filter(
                licenca=licenca, usuario=usuario
            ).order_by('-created_at', '-id').first()

            if last_mov and last_mov.tipo == TipoMovLicencaChoices.ATRIBUICAO:
                self.add_error('usuario', f"O usuário já possui esta licença.")

            if lote_id:
                try:
                    lote = LicencaLote.objects.get(pk=lote_id, licenca=licenca)
                    if lote.quantidade_disponivel < 1:
                        raise ValidationError(f"O Lote #{lote_id} está esgotado.")
                    cleaned_data['lote_manual_obj'] = lote
                except LicencaLote.DoesNotExist:
                    raise ValidationError("Lote inválido.")
            else:
                total = LicencaLote.objects.filter(licenca=licenca).aggregate(s=Sum('quantidade_disponivel'))['s'] or 0
                if total < 1:
                    raise ValidationError("Estoque esgotado.")

        elif tipo == TipoMovLicencaChoices.DEVOLUCAO:
            if not usuario:
                self.add_error('usuario', "Informe o colaborador.")
            
            last_attrib = MovimentacaoLicenca.objects.filter(
                licenca=licenca, usuario=usuario, tipo=TipoMovLicencaChoices.ATRIBUICAO
            ).order_by('-created_at', '-id').first()

            if not last_attrib:
                self.add_error('usuario', "Este usuário não possui licença ativa para devolver.")
            else:
                cleaned_data['origem_devolucao'] = last_attrib

        return cleaned_data

    @transaction.atomic
    def save(self, commit=True, user=None):
        instance = super().save(commit=False)
        
        lote_manual = self.cleaned_data.get('lote_manual_obj')
        origem_devolucao = self.cleaned_data.get('origem_devolucao')

        if user:
            if instance.pk is None: instance.criado_por = user
            instance.atualizado_por = user

        if commit:
            # === ATRIBUIÇÃO (SAÍDA) ===
            if instance.tipo == TipoMovLicencaChoices.ATRIBUICAO:
                lote_alvo = None
                
                # 1. Define Lote (Manual ou FIFO)
                if lote_manual:
                    lote_alvo = LicencaLote.objects.select_for_update().get(pk=lote_manual.pk)
                else:
                    lote_alvo = LicencaLote.objects.select_for_update().filter(
                        licenca=instance.licenca,
                        quantidade_disponivel__gt=0
                    ).order_by('data_compra', 'id').first()

                if lote_alvo:
                    # 2. Baixa de Estoque
                    lote_alvo.quantidade_disponivel = max(0, lote_alvo.quantidade_disponivel - 1)
                    lote_alvo.save()
                    
                    instance.lote = lote_alvo
                    
                    # 3. Define Centro de Custo (Destino = Usuário)
                    if instance.usuario and instance.usuario.centro_custo:
                        instance.centro_custo_destino = instance.usuario.centro_custo
                    
                    # Cálculo do custo mensal unitário por licença
                    custo_ciclo = lote_alvo.custo_ciclo or Decimal(0)
                    periodicidade = str(lote_alvo.periodicidade or "").lower()
                    qtd_total = Decimal(lote_alvo.quantidade_total or 1)

                    custo_unitario_ciclo = custo_ciclo / qtd_total

                    if periodicidade == "anual":
                        instance.valor_unitario = (custo_unitario_ciclo / Decimal("12")).quantize(Decimal("0.01"))
                    elif periodicidade == "semestral":
                        instance.valor_unitario = (custo_unitario_ciclo / Decimal("6")).quantize(Decimal("0.01"))
                    elif periodicidade == "trimestral":
                        instance.valor_unitario = (custo_unitario_ciclo / Decimal("3")).quantize(Decimal("0.01"))
                    else:
                        instance.valor_unitario = custo_unitario_ciclo.quantize(Decimal("0.01"))

            # === DEVOLUÇÃO (ENTRADA) ===
            elif instance.tipo == TipoMovLicencaChoices.DEVOLUCAO:
                lote_retorno = None
                origem_id = origem_devolucao.lote_id if origem_devolucao else None
                
                if origem_id:
                    try:
                        lote_retorno = LicencaLote.objects.select_for_update().get(pk=origem_id)
                    except LicencaLote.DoesNotExist: pass
                
                if not lote_retorno:
                    lote_retorno = LicencaLote.objects.select_for_update().filter(
                        licenca=instance.licenca
                    ).order_by('-data_compra').first()

                if lote_retorno:
                    lote_retorno.quantidade_disponivel += 1
                    lote_retorno.save()
                    instance.lote = lote_retorno
                    # Retorna custo para quem comprou (Lote)
                    instance.centro_custo_destino = lote_retorno.centro_custo
                else:
                    instance.centro_custo_destino = getattr(instance.licenca, 'centro_custo', None)

                # Mantém valor histórico (estorna o valor cobrado na saída)
                if origem_devolucao:
                    instance.valor_unitario = origem_devolucao.valor_unitario

            instance.save()

        return instance
# --- FORMULÁRIO DE LOTE (Estoque Específico) ---
class LicencaLoteForm(forms.ModelForm):
    class Meta:
        model = LicencaLote
        fields = [
            "licenca", "fornecedor", "centro_custo", 
            "numero_pedido", "data_compra", 
            "quantidade_total", # Disponível removido da tela
            "periodicidade", "custo_ciclo", 
            "observacao"
        ]
        widgets = {
            "licenca": forms.Select(attrs={"class": "form-control"}),
            "fornecedor": forms.Select(attrs={"class": "form-control"}),
            "centro_custo": forms.Select(attrs={"class": "form-control"}),
            
            "numero_pedido": forms.TextInput(attrs={"class": "form-control", "placeholder": "Ex: PO-2024-001"}),
            "data_compra": forms.DateInput(format='%Y-%m-%d', attrs={"class": "form-control", "type": "date"}),
            
            "quantidade_total": forms.NumberInput(attrs={"class": "form-control", "min": 1}),
            "periodicidade": forms.Select(attrs={"class": "form-control"}),
            "custo_ciclo": forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "placeholder": "0.00"}),
            
            "observacao": forms.Textarea(attrs={"class": "form-control", "rows": 3, "placeholder": "Detalhes adicionais..."}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Formata a data corretamente para edição
        if self.instance.pk and self.instance.data_compra:
            self.fields['data_compra'].initial = self.instance.data_compra.strftime('%Y-%m-%d')

    def clean_quantidade_total(self):
        qtd = self.cleaned_data.get('quantidade_total')
        if qtd is not None and qtd < 1:
            raise forms.ValidationError("A quantidade total deve ser pelo menos 1.")
        
        # Validação extra na edição: não permitir reduzir total abaixo do que já foi usado
        if self.instance.pk:
            usados = self.instance.quantidade_total - self.instance.quantidade_disponivel
            if qtd < usados:
                raise forms.ValidationError(f"Não é possível reduzir para {qtd}. Já existem {usados} licenças em uso neste lote.")
        
        return qtd

ESTABELECIMENTO_CHOICES = [
    ("rio_do_meio", "Rio do Meio"),
    ("karitel", "Karitel"),
    ("sao_paulo", "São Paulo"),
    ("sta_edwiges", "Sta. Edwiges"),
]


class TermoGeracaoForm(forms.Form):
    colaborador = forms.ModelChoiceField(
        queryset=Usuario.objects.filter(status="ativo").order_by("nome"),
        required=False,
        label="Colaborador",
        empty_label="Selecione um colaborador",
        widget=forms.Select(attrs={
            "class": "form-select select2"
        })
    )

    numero_termo = forms.CharField(
        required=False,
        label="Número do termo",
        widget=forms.TextInput(attrs={"class": "form-control"})
    )

    numero_chamado = forms.CharField(
        required=False,
        label="Número do chamado",
        widget=forms.TextInput(attrs={"class": "form-control"})
    )

    acessorios = forms.CharField(
        required=False,
        label="Acessórios",
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3})
    )

    observacoes = forms.CharField(
        required=False,
        label="Observações do termo",
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 4})
    )

    responsavel_ti_nome = forms.CharField(
        required=False,
        label="Responsável do TI",
        widget=forms.TextInput(attrs={"class": "form-control"})
    )

    estabelecimento = forms.ChoiceField(
        choices=ESTABELECIMENTO_CHOICES,
        required=False,
        label="Estabelecimento",
        widget=forms.Select(attrs={
            "class": "form-select select2-basic"
        })
    )


# ================== PLANTAS ==================
class PlantaProjetoForm(forms.ModelForm):
    class Meta:
        model = PlantaProjeto
        fields = ["nome", "localidade", "descricao", "imagem_fundo"]
        widgets = {
            "nome": forms.TextInput(attrs={
                "class": "ctrl", "placeholder": "Ex: Karitel - Sala Técnica"
            }),
            "localidade": forms.Select(attrs={
                "class": "ctrl"
            }),
            "descricao": forms.Textarea(attrs={
                "class": "ctrl", "rows": 3,
                "placeholder": "Descrição opcional da planta..."
            }),
            "imagem_fundo": forms.ClearableFileInput(attrs={
                "class": "ctrl", "accept": ".png,.jpg,.jpeg,.svg"
            }),
        }

    def clean_imagem_fundo(self):
        img = self.cleaned_data.get("imagem_fundo")
        if img and hasattr(img, "name"):
            ext = img.name.rsplit(".", 1)[-1].lower()
            if ext not in ("png", "jpg", "jpeg"):
                raise ValidationError("Formato não suportado. Use PNG ou JPG.")
            if img.size > 10 * 1024 * 1024:
                raise ValidationError("Imagem excede o limite de 10 MB.")
        return img


# ================== REQUISIÇÕES (KANBAN) ==================
class ItemPadraoSelectWidget(forms.Select):
    """<select> com data-atributos (código/descrição/categoria) por opção —
    usados só no cliente pra autopreencher o formulário; sem AJAX porque o
    catálogo é pequeno o bastante pra vir todo no HTML (mesmo padrão de
    categoria/item_vinculado neste form)."""

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        if value:
            obj = self._cache().get(str(value.value if hasattr(value, "value") else value))
            if obj:
                option["attrs"]["data-codigo"] = obj.codigo
                option["attrs"]["data-descricao"] = obj.descricao
                option["attrs"]["data-categoria"] = obj.categoria_id
        return option

    def _cache(self):
        # Sem filtro de `ativo` aqui de propósito: o widget só descreve as
        # opções que o campo já decidiu renderizar (via queryset do field);
        # um item padrão desativado, mas ainda referenciado pela edição de um
        # item existente, também precisa dos data-atributos.
        if not hasattr(self, "_itens_cache"):
            self._itens_cache = {str(o.pk): o for o in ItemPadraoDatasul.objects.all()}
        return self._itens_cache


class RequisicaoItemForm(forms.ModelForm):
    item_padrao = forms.ModelChoiceField(
        queryset=ItemPadraoDatasul.objects.filter(ativo=True).select_related("categoria").order_by("descricao"),
        required=False, label="Selecionar item padrão (Datasul)",
        widget=ItemPadraoSelectWidget(attrs={"class": "ctrl"}),
        help_text="Opcional — preenche código, descrição e categoria automaticamente. Não achou? Preencha os campos abaixo manualmente.",
    )

    class Meta:
        model = RequisicaoItem
        fields = ["tipo", "categoria", "item_vinculado", "codigo", "descricao", "quantidade", "justificativa"]
        widgets = {
            "tipo": forms.Select(attrs={"class": "ctrl"}),
            "categoria": forms.Select(attrs={"class": "ctrl"}),
            "item_vinculado": forms.Select(attrs={"class": "ctrl"}),
            "codigo": forms.TextInput(attrs={"class": "ctrl", "placeholder": "Código no Datasul (se já existir)"}),
            "descricao": forms.TextInput(attrs={"class": "ctrl", "placeholder": "Ex.: Toner Impressora Setor A"}),
            "quantidade": forms.NumberInput(attrs={"class": "ctrl", "min": 1}),
            "justificativa": forms.Textarea(attrs={"class": "ctrl", "rows": 3}),
        }

    field_order = ["item_padrao", "tipo", "categoria", "item_vinculado", "codigo", "descricao", "quantidade", "justificativa"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["item_vinculado"].queryset = Item.objects.filter(tem_lote=True).order_by("nome")
        self.fields["item_vinculado"].required = False
        self.fields["codigo"].required = False
        self.fields["justificativa"].required = False

        # Reabre a seleção do item padrão ao editar um item que foi criado a
        # partir do catálogo (casado pelo código salvo) — sem isso o seletor
        # volta em branco toda vez que a tela é reaberta, dando a impressão
        # de que a vinculação com o catálogo se perdeu, mesmo o código
        # continuando salvo no item.
        if self.instance.pk and self.instance.codigo:
            match = ItemPadraoDatasul.objects.filter(codigo=self.instance.codigo).first()
            if match:
                if not match.ativo:
                    self.fields["item_padrao"].queryset = (
                        self.fields["item_padrao"].queryset | ItemPadraoDatasul.objects.filter(pk=match.pk)
                    )
                self.initial["item_padrao"] = match.pk


class ItemPadraoDatasulForm(forms.ModelForm):
    class Meta:
        model = ItemPadraoDatasul
        fields = ["codigo", "descricao", "categoria", "ativo"]
        widgets = {
            "codigo": forms.TextInput(attrs={"class": "ctrl", "placeholder": "Ex.: 10023456"}),
            "descricao": forms.TextInput(attrs={"class": "ctrl", "placeholder": "Ex.: Toner HP 105A Preto"}),
            "categoria": forms.Select(attrs={"class": "ctrl"}),
            "ativo": forms.CheckboxInput(attrs={"class": "ctrl-check"}),
        }


class ItemPadraoImportForm(forms.Form):
    arquivo = forms.FileField(
        label="Planilha (.xlsx)",
        help_text="Colunas obrigatórias: Código, Descrição, Categoria (a categoria já precisa existir no sistema).",
        widget=forms.ClearableFileInput(attrs={"class": "ctrl", "accept": ".xlsx"}),
    )


class ComentarioRequisicaoItemForm(forms.ModelForm):
    class Meta:
        model = ComentarioRequisicaoItem
        fields = ["texto"]
        widgets = {
            "texto": forms.Textarea(attrs={"class": "ctrl", "rows": 2, "placeholder": "Escreva um comentário..."}),
        }


class RequisicaoNumerosForm(forms.ModelForm):
    class Meta:
        model = Requisicao
        fields = ["numero_datasul", "numero_paradigma"]
        widgets = {
            "numero_datasul": forms.TextInput(attrs={"class": "ctrl", "placeholder": "Ex.: REQ-5510"}),
            "numero_paradigma": forms.TextInput(attrs={"class": "ctrl", "placeholder": "Número no Paradigma"}),
        }


class RequisicaoReceberCompraForm(forms.Form):
    """Recebimento de uma compra vinculada a um item de estoque — mesmos
    dados exigidos pela Entrada em Movimentações (menos a quantidade, que
    aqui é sempre a solicitada na requisição, não reeditável)."""
    fornecedor = forms.ModelChoiceField(
        queryset=Fornecedor.objects.order_by("nome"),
        label="Fornecedor",
        widget=forms.Select(attrs={"class": "ctrl"}),
    )
    numero_nf = forms.CharField(
        label="Número da NF",
        widget=forms.TextInput(attrs={"class": "ctrl", "placeholder": "Ex.: 123456"}),
    )
    custo_unitario = forms.DecimalField(
        label="Custo Unitário",
        min_value=Decimal("0.01"),
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={"class": "ctrl", "step": "0.01", "min": "0.01"}),
    )
    localidade_destino = forms.ModelChoiceField(
        queryset=Localidade.objects.order_by("local"),
        label="Localidade",
        widget=forms.Select(attrs={"class": "ctrl"}),
    )
    centro_custo_destino = forms.ModelChoiceField(
        queryset=CentroCusto.objects.order_by("numero"),
        label="Centro de Custo",
        widget=forms.Select(attrs={"class": "ctrl"}),
    )
    observacao = forms.CharField(
        required=False,
        label="Observação (opcional)",
        widget=forms.Textarea(attrs={"class": "ctrl", "rows": 3}),
    )