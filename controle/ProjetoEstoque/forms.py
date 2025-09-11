from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError
from django.db import transaction
from .models import (
    Categoria, Subtipo, Localidade, Fornecedor, CentroCusto, Funcao, Usuario,
    Item, Locacao, Comentario, CicloManutencao, MovimentacaoItem,
    StatusItemChoices, TipoMovimentacaoChoices, TipoTransferenciaChoices,
    LocalidadeChoices, CheckListModelo, CheckListPergunta, Preventiva,
    TipoRespostaChoices, SimNaoChoices, Licenca, MovimentacaoLicenca,
    TipoMovLicencaChoices
)

DATE_WIDGET = forms.DateInput(attrs={"type": "date"})

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
            "categoria": forms.Select(attrs={"class": "ctrl", "data-select2": "1"}),
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


class UsuarioForm(forms.ModelForm):
    class Meta:
        model = Usuario
        fields = [
            "nome", "status", "data_inicio", "data_termino",
            "pmb", "email", "centro_custo", "localidade", "funcao"
        ]
        widgets = {
            "data_inicio": DATE_WIDGET,
            "data_termino": DATE_WIDGET,
        }

# ================== ITEM ==================
class ItemForm(forms.ModelForm):
    class Meta:
        model = Item
        fields = [
            # principais
            "nome", "numero_serie", "quantidade", "marca", "modelo", "centro_custo",
            # estoque/consumo
            "pmb", "item_consumo",
            # valor e status
            "valor", "status",
            # relações
            "subtipo", "localidade", "fornecedor",
            # observações
            "observacoes",
            # preventiva
            "precisa_preventiva", "data_limite_preventiva",
            # locação
            "locado", "data_compra", "numero_pedido",
        ]


class LocacaoForm(forms.ModelForm):
    class Meta:
        model = Locacao
        fields = ["tempo_locado", "valor_mensal", "contrato", "observacoes", "fornecedor"]
        labels = {
            "tempo_locado": "Tempo locado (meses)",
            "valor_mensal": "Valor mensal (R$)",
        }

# ================== COMENTÁRIO ==================
class ComentarioForm(forms.ModelForm):
    class Meta:
        model = Comentario
        fields = ["texto", "item"]

# ================== MANUTENÇÃO ==================
class CicloManutencaoForm(forms.ModelForm):
    class Meta:
        model = CicloManutencao
        fields = ["item", "status_inicial", "data_inicio", "data_fim", "causa", "custo"]
        widgets = {
            "data_inicio": DATE_WIDGET,
            "data_fim": DATE_WIDGET,
            "causa": forms.Textarea(attrs={"rows": 3}),
            "custo": forms.NumberInput(attrs={"step": "0.01"}),
        }

# ================== MOVIMENTAÇÃO ==================
class MovimentacaoItemForm(forms.ModelForm):
    class Meta:
        model = MovimentacaoItem
        fields = [
            "tipo_movimentacao",
            "tipo_transferencia",
            "item",
            "usuario",
            "quantidade",
            "localidade_destino",
            "centro_custo_destino",
            "fornecedor_manutencao",
            "numero_pedido",  # exibido no form
            "observacao",
            "chamado",
            "custo",
            "termo_pdf",
        ]
        widgets = {
            "tipo_movimentacao": forms.Select(attrs={"class": "ctrl"}),
            "tipo_transferencia": forms.RadioSelect,
            "item": forms.Select(attrs={"class": "ctrl"}),
            "usuario": forms.Select(attrs={"class": "ctrl"}),
            "localidade_destino": forms.Select(attrs={"class": "ctrl"}),
            "centro_custo_destino": forms.Select(attrs={"class": "ctrl"}),
            "fornecedor_manutencao": forms.Select(attrs={"class": "ctrl"}),
            "numero_pedido": forms.TextInput(attrs={"class": "ctrl", "placeholder": "Ex.: PO-2025-0001"}),
            "observacao": forms.Textarea(attrs={"class": "ctrl", "rows": 3}),
            "chamado": forms.TextInput(attrs={"class": "ctrl"}),
            "custo": forms.NumberInput(attrs={"class": "ctrl", "step": "0.01"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Campos ficam opcionalmente obrigatórios via regra dinâmica (clean)
        for f in self.fields.values():
            f.required = False

    def clean(self):
        cleaned = super().clean()
        tipo = cleaned.get("tipo_movimentacao")

        # alias aceito no front
        if tipo == "retorno":
            tipo = "retorno_manutencao"
            cleaned["tipo_movimentacao"] = tipo

        # ===== TRANSFERÊNCIA =====
        if tipo == "transferencia":
            # exige tipo_transferencia
            if not cleaned.get("tipo_transferencia"):
                self.add_error("tipo_transferencia", "Selecione Entrega ou Devolução.")

            # exige CC e Local destino
            if not cleaned.get("localidade_destino"):
                self.add_error("localidade_destino", "Localidade destino é obrigatória para transferência.")
            if not cleaned.get("centro_custo_destino"):
                self.add_error("centro_custo_destino", "Centro de custo destino é obrigatório para transferência.")

            # termo obrigatório (regra de negócio)
            if not cleaned.get("termo_pdf"):
                self.add_error("termo_pdf", "O termo PDF é obrigatório para transferência.")

            # ⚠️ FIX: Entrega SEMPRE precisa de usuário (vincula posse)
            if cleaned.get("tipo_transferencia") == "entrega" and not cleaned.get("usuario"):
                self.add_error("usuario", "Informe o usuário para a Transferência (Entrega).")

        # ===== BAIXA =====
        elif tipo == "baixa":
            if not cleaned.get("quantidade"):
                self.add_error("quantidade", "Informe a quantidade para a baixa.")
            if not cleaned.get("localidade_destino"):
                self.add_error("localidade_destino", "Localidade é obrigatória na baixa.")
            if not cleaned.get("centro_custo_destino"):
                self.add_error("centro_custo_destino", "Centro de custo é obrigatório na baixa.")

        # ===== ENTRADA =====
        elif tipo == "entrada":
            if not cleaned.get("quantidade"):
                self.add_error("quantidade", "Informe a quantidade para a entrada.")
            if not cleaned.get("localidade_destino"):
                self.add_error("localidade_destino", "Localidade é obrigatória na entrada.")
            if not cleaned.get("centro_custo_destino"):
                self.add_error("centro_custo_destino", "Centro de custo é obrigatório na entrada.")
            if not (cleaned.get("numero_pedido") or "").strip():
                self.add_error("numero_pedido", "Informe o número do pedido para a entrada.")

        # ===== ENVIO MANUTENÇÃO =====
        elif tipo == "envio_manutencao":
            if not cleaned.get("fornecedor_manutencao"):
                self.add_error("fornecedor_manutencao", "Informe o fornecedor de manutenção.")

        # ===== RETORNO MANUTENÇÃO =====
        elif tipo == "retorno_manutencao":
            # sem regras adicionais; o model/view já limpam o usuário se necessário
            pass

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
        queryset=Item.objects.select_related("subtipo").order_by("nome"),
        label=_("Equipamento"),
        widget=forms.Select(attrs={"class": "ctrl"}),
    )
    checklist_modelo = forms.ModelChoiceField(
        queryset=CheckListModelo.objects.select_related("subtipo").order_by("nome"),
        label=_("Modelo de Checklist"),
        widget=forms.Select(attrs={"class": "ctrl"}),
    )

    def __init__(self, *args, **kwargs):
        item_instance = kwargs.pop("item_instance", None)
        super().__init__(*args, **kwargs)
        if item_instance and item_instance.subtipo_id:
            self.fields["item"].initial = item_instance.pk
            self.fields["checklist_modelo"].queryset = (
                CheckListModelo.objects
                .filter(subtipo=item_instance.subtipo, ativo="sim")
                .select_related("subtipo")
                .order_by("nome")
            )

# ================== LICENÇAS ==================
class LicencaForm(forms.ModelForm):
    class Meta:
        model = Licenca
        fields = [
            "nome", "fornecedor", "centro_custo",
            "quantidade", "custo",  # <- ✅ custo aqui
            "pmb", "periodicidade",
            "data_inicio", "data_fim", "observacao",
        ]
        widgets = {
            "nome": forms.TextInput(attrs={"class": "ctrl", "placeholder": "Ex.: Microsoft 365 Business"}),
            "fornecedor": forms.Select(attrs={"class": "ctrl"}),
            "centro_custo": forms.Select(attrs={"class": "ctrl"}),
            "quantidade": forms.NumberInput(attrs={"class": "ctrl", "min": 0}),
            "custo": forms.NumberInput(attrs={"class": "ctrl", "step": "0.01", "min": 0}),  # ✅
            "pmb": forms.Select(attrs={"class": "ctrl"}),
            "periodicidade": forms.Select(attrs={"class": "ctrl"}),
            "data_inicio": forms.DateInput(attrs={"type": "date", "class": "ctrl"}),
            "data_fim": forms.DateInput(attrs={"type": "date", "class": "ctrl"}),
            "observacao": forms.Textarea(attrs={"class": "ctrl", "rows": 4, "placeholder": "Observações…"}),
        }

    def clean(self):
        cleaned = super().clean()
        ini, fim = cleaned.get("data_inicio"), cleaned.get("data_fim")
        if ini and fim and fim < ini:
            self.add_error("data_fim", "A data de fim não pode ser anterior à data de início.")
        return cleaned

class MovimentacaoLicencaForm(forms.ModelForm):
    class Meta:
        model = MovimentacaoLicenca
        fields = ["tipo", "licenca", "usuario", "centro_custo_destino", "observacao"]
        widgets = {
            "tipo": forms.Select(attrs={"class": "ctrl"}),
            "licenca": forms.Select(attrs={"class": "ctrl"}),
            "usuario": forms.Select(attrs={"class": "ctrl"}),
            "centro_custo_destino": forms.Select(attrs={"class": "ctrl"}),
            "observacao": forms.Textarea(attrs={"class": "ctrl", "rows": 3}),
        }

    def clean(self):
        cleaned = super().clean()
        tipo = cleaned.get("tipo")
        usuario = cleaned.get("usuario")
        if tipo in (TipoMovLicencaChoices.ATRIBUICAO, TipoMovLicencaChoices.REMOCAO) and not usuario:
            raise ValidationError("Informe o usuário para atribuir/remover a licença.")
        return cleaned

    @transaction.atomic
    def save(self, commit=True, user=None):
        """
        Salva a movimentação e ajusta o estoque da licença.
        - Usa a instância do modelo (self.instance).
        - Preenche criado_por/atualizado_por.
        - Faz fallback do centro de custo se não vier no form.
        - Ajusta 'quantidade' da Licença com lock pessimista.
        """
        instance: MovimentacaoLicenca = super().save(commit=False)

        # Fallback do Centro de Custo (usuário > licença)
        if instance.centro_custo_destino_id is None:
            if instance.usuario and instance.usuario.centro_custo_id:
                instance.centro_custo_destino = instance.usuario.centro_custo
            elif instance.licenca and instance.licenca.centro_custo_id:
                instance.centro_custo_destino = instance.licenca.centro_custo

        # Auditoria
        if user is not None:
            if not instance.pk:
                instance.criado_por = user
            instance.atualizado_por = user

        is_new = instance.pk is None
        if commit:
            instance.save()  # dispara o save() do modelo se houver lógica lá

        # ===== Ajuste de estoque da licença (apenas em criação) =====
        if is_new:
            lic = Licenca.objects.select_for_update().get(pk=instance.licenca_id)

            if instance.tipo == TipoMovLicencaChoices.ATRIBUICAO:
                if (lic.quantidade or 0) <= 0:
                    # rollback automático por causa do @atomic
                    raise ValidationError("Licença sem assentos disponíveis.")
                lic.quantidade = (lic.quantidade or 0) - 1
                lic.save(update_fields=["quantidade", "updated_at"])

            elif instance.tipo == TipoMovLicencaChoices.REMOCAO:
                # Garante que há uma atribuição ativa antes de remover
                last = (
                    MovimentacaoLicenca.objects
                    .filter(licenca_id=instance.licenca_id, usuario_id=instance.usuario_id)
                    .exclude(pk=instance.pk)
                    .order_by("-created_at", "-id")
                    .first()
                )
                if not last or last.tipo != TipoMovLicencaChoices.ATRIBUICAO:
                    raise ValidationError("Não há atribuição ativa dessa licença para este usuário.")
                lic.quantidade = (lic.quantidade or 0) + 1
                lic.save(update_fields=["quantidade", "updated_at"])

        return instance