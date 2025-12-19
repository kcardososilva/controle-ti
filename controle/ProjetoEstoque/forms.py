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
    TipoMovLicencaChoices, LicencaLote
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
        "class": "ctrl",           # opcional: casa com seu CSS
    },
    format="%Y-%m-%d",
)

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
            # Demais widgets continuam padrão; classes serão aplicadas no __init__
        }

    def __init__(self, *args, **kwargs):
        """
        - Mantém seu baseline (funciona).
        - Aplica Select2 nos selects.
        - Garante que, em edição (GET), o <input type="date"> venha pré-preenchido.
        """
        super().__init__(*args, **kwargs)

        # 🔧 Aceitar formato do <input type="date">
        self.fields["data_inicio"].input_formats = ["%Y-%m-%d"]
        self.fields["data_termino"].input_formats = ["%Y-%m-%d"]

        # Classes padrão e Select2
        for name, field in self.fields.items():
            # aplica .ctrl em todos (exceto os que já têm via DATE_WIDGET)
            if not isinstance(field.widget, forms.DateInput):
                field.widget.attrs.setdefault("class", "ctrl")

        # aplica select2 nos selects
        for name in ("status", "pmb", "centro_custo", "localidade", "funcao"):
            if name in self.fields and hasattr(self.fields[name].widget, "attrs"):
                existing = self.fields[name].widget.attrs.get("class", "")
                if "select2" not in existing:
                    self.fields[name].widget.attrs["class"] = (existing + " select2").strip()
                self.fields[name].widget.attrs.setdefault("data-placeholder", "Selecione...")

        # UX: ordenar combos
        if "centro_custo" in self.fields:
            self.fields["centro_custo"].queryset = CentroCusto.objects.order_by("numero", "departamento")
            self.fields["centro_custo"].empty_label = "—"
        if "localidade" in self.fields:
            self.fields["localidade"].queryset = Localidade.objects.order_by("local")
            self.fields["localidade"].empty_label = "—"
        if "funcao" in self.fields:
            self.fields["funcao"].queryset = Funcao.objects.order_by("nome")
            self.fields["funcao"].empty_label = "—"

        # ✅ PRÉ-PREENCHIMENTO NA EDIÇÃO (GET): value no input[type=date]
        if self.instance and self.instance.pk and not self.is_bound:
            if self.instance.data_inicio:
                self.fields["data_inicio"].initial = self.instance.data_inicio
                self.fields["data_inicio"].widget.attrs["value"] = self.instance.data_inicio.strftime("%Y-%m-%d")

    def clean(self):
        """
        - Mantém sua validação original (término >= início).
        - ✅ PRESERVA data_inicio NA EDIÇÃO SE O POST VIER VAZIO (string vazia vira None no cleaned_data).
        """
        data = super().clean()

        # ✅ preserva data_inicio na edição
        if self.instance and self.instance.pk:
            # Se o POST trouxe '' (vazio) ou None, restaura o valor original
            posted_raw = self.data.get(self.add_prefix("data_inicio"), None)
            if (posted_raw in (None, "")) and self.instance.data_inicio:
                data["data_inicio"] = self.instance.data_inicio

        di = data.get("data_inicio")
        dt = data.get("data_termino")
        if di and dt and dt < di:
            self.add_error("data_termino", "A data de término não pode ser anterior à data de início.")

        return data

    def save(self, commit=True):
        """
        ✅ Blindagem final: na edição, se o POST vier vazio para data_inicio,
        regrava o valor anterior da instância.
        """
        instance = super().save(commit=False)

        if self.instance and self.instance.pk:
            posted_raw = self.data.get(self.add_prefix("data_inicio"), None)
            if (posted_raw in (None, "")) and self.instance.data_inicio:
                instance.data_inicio = self.instance.data_inicio

        if commit:
            instance.save()
            self.save_m2m()
        return instance
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
            "numero_pedido",
            "observacao",
            "chamado",
            "custo",
            "termo_pdf",
            # ✅ status usado apenas em "transferencia_equipamento"
            "status_transferencia",
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
            "custo": forms.NumberInput(attrs={"class": "ctrl", "step": "0.01", "min": "0"}),
            "status_transferencia": forms.Select(attrs={"class": "ctrl"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Tudo opcional por padrão; validação condicional no clean()
        for f in self.fields.values():
            f.required = False

    def clean(self):
        cleaned = super().clean()
        tipo = (cleaned.get("tipo_movimentacao") or "").strip()

        # Alias aceito no front
        if tipo == "retorno":
            tipo = "retorno_manutencao"
            cleaned["tipo_movimentacao"] = tipo

        # ========= Regras por tipo =========

        # TRANSFERÊNCIA (clássica)
        if tipo == "transferencia":
            if not cleaned.get("tipo_transferencia"):
                self.add_error("tipo_transferencia", "Selecione Entrega ou Devolução.")
            if not cleaned.get("localidade_destino"):
                self.add_error("localidade_destino", "Localidade destino é obrigatória para transferência.")
            if not cleaned.get("centro_custo_destino"):
                self.add_error("centro_custo_destino", "Centro de custo destino é obrigatório para transferência.")
            if not cleaned.get("termo_pdf"):
                self.add_error("termo_pdf", "O termo PDF é obrigatório para transferência.")
            if cleaned.get("tipo_transferencia") == "entrega" and not cleaned.get("usuario"):
                self.add_error("usuario", "Informe o usuário para a Transferência (Entrega).")

        # ✅ TRANSFERÊNCIA EQUIPAMENTO (nova)
        elif tipo == "transferencia_equipamento":
            if not cleaned.get("localidade_destino"):
                self.add_error("localidade_destino", "Localidade destino é obrigatória.")
            if not cleaned.get("centro_custo_destino"):
                self.add_error("centro_custo_destino", "Centro de custo destino é obrigatório.")
            if not cleaned.get("status_transferencia"):
                self.add_error("status_transferencia", "Selecione o status do equipamento para a transferência.")

        # BAIXA
        elif tipo == "baixa":
            if not cleaned.get("quantidade"):
                self.add_error("quantidade", "Informe a quantidade para a baixa.")
            if not cleaned.get("localidade_destino"):
                self.add_error("localidade_destino", "Localidade é obrigatória na baixa.")
            if not cleaned.get("centro_custo_destino"):
                self.add_error("centro_custo_destino", "Centro de custo é obrigatório na baixa.")

        # ENTRADA
        elif tipo == "entrada":
            if not cleaned.get("quantidade"):
                self.add_error("quantidade", "Informe a quantidade para a entrada.")
            if not cleaned.get("localidade_destino"):
                self.add_error("localidade_destino", "Localidade é obrigatória na entrada.")
            if not cleaned.get("centro_custo_destino"):
                self.add_error("centro_custo_destino", "Centro de custo é obrigatório na entrada.")
            numero = (cleaned.get("numero_pedido") or "").strip()
            if not numero:
                self.add_error("numero_pedido", "Informe o número do pedido para a entrada.")

        # ENVIO MANUTENÇÃO
        elif tipo == "envio_manutencao":
            if not cleaned.get("fornecedor_manutencao"):
                self.add_error("fornecedor_manutencao", "Informe o fornecedor de manutenção.")

        # Sanitização leve
        custo = cleaned.get("custo")
        if custo is not None and custo < 0:
            self.add_error("custo", "Custo não pode ser negativo.")

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
ISO_FMT = "%Y-%m-%d"

class LicencaForm(forms.ModelForm):
    class Meta:
        model = Licenca
        fields = [
            "nome", "fornecedor", "centro_custo",
            "quantidade", "custo",
            "pmb", "periodicidade",
            "data_inicio", "data_fim", "observacao",
        ]
        widgets = {
            "nome": forms.TextInput(attrs={"class": "ctrl", "placeholder": "Ex.: Microsoft 365 Business"}),
            "fornecedor": forms.Select(attrs={"class": "ctrl"}),
            "centro_custo": forms.Select(attrs={"class": "ctrl"}),
            "quantidade": forms.NumberInput(attrs={"class": "ctrl", "min": 0}),
            "custo": forms.NumberInput(attrs={"class": "ctrl", "step": "0.01", "min": 0}),
            "pmb": forms.Select(attrs={"class": "ctrl"}),
            "periodicidade": forms.Select(attrs={"class": "ctrl"}),

            # ⚠️ IMPORTANTE: define o formato explícito para exibir o valor no input date
            "data_inicio": forms.DateInput(format=ISO_FMT, attrs={"type": "date", "class": "ctrl"}),
            "data_fim":    forms.DateInput(format=ISO_FMT, attrs={"type": "date", "class": "ctrl"}),

            "observacao": forms.Textarea(attrs={"class": "ctrl", "rows": 4, "placeholder": "Observações…"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Garante que o widget conheça o formato de saída
        self.fields["data_inicio"].widget.format = ISO_FMT
        self.fields["data_fim"].widget.format = ISO_FMT

        # Quando for edição, injeta o value no padrão ISO (YYYY-MM-DD)
        inst = self.instance
        if inst and getattr(inst, "pk", None):
            if inst.data_inicio:
                self.fields["data_inicio"].initial = inst.data_inicio.strftime(ISO_FMT)
            if inst.data_fim:
                self.fields["data_fim"].initial = inst.data_fim.strftime(ISO_FMT)

        # Aceita também datas vindas do browser em ISO
        self.fields["data_inicio"].input_formats = [ISO_FMT]
        self.fields["data_fim"].input_formats = [ISO_FMT]

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

        # 🔹 pega o lote do POST (se selecionado)
        lote_id_raw = self.data.get("lote")  # "", None ou "123"
        lote_id = int(lote_id_raw) if lote_id_raw and str(lote_id_raw).isdigit() else None
        if lote_id:
            # ⚠️ IMPORTANTE: persistir o vínculo com o lote
            instance.lote_id = lote_id

        is_new = instance.pk is None
        if commit:
            instance.save()

        # >>> Daqui pra baixo sua lógica de débito/crédito permanece igual <<<
        lic = Licenca.objects.select_for_update().get(pk=instance.licenca_id)

        if is_new and instance.tipo == TipoMovLicencaChoices.ATRIBUICAO:
            if lote_id:
                lote = LicencaLote.objects.select_for_update().get(pk=lote_id)
                if (lote.quantidade_disponivel or 0) <= 0:
                    raise ValidationError("Lote sem assentos disponíveis para atribuição.")
                lote.quantidade_disponivel = (lote.quantidade_disponivel or 0) - 1
                lote.save(update_fields=["quantidade_disponivel", "updated_at"])
            else:
                if (lic.quantidade or 0) <= 0:
                    raise ValidationError("Licença sem quantidade disponível para atribuição.")
                lic.quantidade = (lic.quantidade or 0) - 1
                lic.save(update_fields=["quantidade", "updated_at"])

        elif is_new and instance.tipo == TipoMovLicencaChoices.REMOCAO:
            last = (MovimentacaoLicenca.objects
                    .filter(licenca_id=instance.licenca_id, usuario_id=instance.usuario_id)
                    .exclude(pk=instance.pk)
                    .order_by("-created_at", "-id")
                    .first())
            if not last or last.tipo != TipoMovLicencaChoices.ATRIBUICAO:
                raise ValidationError("Não há atribuição ativa dessa licença para este usuário.")

            if getattr(last, "lote_id", None):
                lote = LicencaLote.objects.select_for_update().get(pk=last.lote_id)
                lote.quantidade_disponivel = (lote.quantidade_disponivel or 0) + 1
                lote.save(update_fields=["quantidade_disponivel", "updated_at"])
            else:
                lic.quantidade = (lic.quantidade or 0) + 1
                lic.save(update_fields=["quantidade", "updated_at"])

        return instance
    
class LicencaLoteForm(forms.ModelForm):
    class Meta:
        model = LicencaLote
        fields = ["licenca", "quantidade_total", "quantidade_disponivel", "custo_ciclo", "data_compra", "fornecedor", "centro_custo", "observacao"]
        widgets = {
            "licenca": forms.Select(attrs={"class": "ctrl"}),
            "quantidade_total": forms.NumberInput(attrs={"class": "ctrl", "min": 0}),
            "quantidade_disponivel": forms.NumberInput(attrs={"class": "ctrl", "min": 0}),
            "custo_ciclo": forms.NumberInput(attrs={"class": "ctrl", "step": "0.01", "min": 0}),
            "data_compra": forms.DateInput(attrs={"type": "date", "class": "ctrl"}),
            "fornecedor": forms.Select(attrs={"class": "ctrl"}),
            "centro_custo": forms.Select(attrs={"class": "ctrl"}),
            "observacao": forms.Textarea(attrs={"class": "ctrl", "rows": 3}),
        }

    def clean(self):
        c = super().clean()
        qt, qd = c.get("quantidade_total") or 0, c.get("quantidade_disponivel") or 0
        if qd > qt:
            self.add_error("quantidade_disponivel", "Disponível não pode superar a quantidade total.")
        return c