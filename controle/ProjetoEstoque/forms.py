from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from decimal import Decimal
from ProjetoEstoque.models import Usuario
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
    # 🔧 Forçamos o formato que o navegador entende (YYYY-MM-DD)
    data_entrada = forms.DateField(
        required=False,
        widget=forms.DateInput(
            format='%Y-%m-%d',
            attrs={
                'type': 'date',
            }
        ),
        # Aceita tanto o formato do navegador quanto dd/mm/aaaa, se necessário
        input_formats=['%Y-%m-%d', '%d/%m/%Y'],
    )

    class Meta:
        model = Locacao
        fields = ["tempo_locado", "valor_mensal", "data_entrada", "fornecedor", "contrato", "observacoes"]
        widgets = {
            "observacoes": forms.Textarea(attrs={'rows': 2}),
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
            "tipo_movimentacao", "tipo_transferencia", "item", "usuario",
            "quantidade", "localidade_destino", "centro_custo_destino",
            "fornecedor_manutencao", "numero_pedido", "observacao",
            "chamado", "custo", "termo_pdf", "status_transferencia"
        ]
        widgets = {
            # Classes 'form-control' para estilo. O JS agora só aplicará Select2 nos tags <select>
            "tipo_movimentacao": forms.Select(attrs={"class": "form-control"}),
            "item": forms.Select(attrs={"class": "form-control"}),
            "usuario": forms.Select(attrs={"class": "form-control"}),
            "localidade_destino": forms.Select(attrs={"class": "form-control"}),
            "centro_custo_destino": forms.Select(attrs={"class": "form-control"}),
            "fornecedor_manutencao": forms.Select(attrs={"class": "form-control"}),
            "status_transferencia": forms.Select(attrs={"class": "form-control"}),
            
            # CORREÇÃO: Widget explícito de Textarea para não ser confundido pelo JS
            "observacao": forms.Textarea(attrs={
                "class": "form-control", 
                "rows": 3, 
                "placeholder": "Descreva detalhes importantes..."
            }),
            
            "numero_pedido": forms.TextInput(attrs={"class": "form-control"}),
            "chamado": forms.TextInput(attrs={"class": "form-control"}),
            "custo": forms.NumberInput(attrs={"class": "form-control", "step": "0.01"}),
            "tipo_transferencia": forms.RadioSelect(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Remove a obrigatoriedade do HTML para permitir a validação condicional no clean()
        for field in self.fields.values():
            field.required = False

    def clean(self):
        cleaned = super().clean()
        tipo = cleaned.get("tipo_movimentacao")

        if not tipo:
            self.add_error("tipo_movimentacao", "O tipo de movimentação é obrigatório.")
            return cleaned

        # Regras condicionais de validação
        if tipo == "transferencia":
            acao = cleaned.get("tipo_transferencia")
            if not acao:
                self.add_error("tipo_transferencia", "Selecione o tipo da transferência (Entrega ou Devolução).")
            if not cleaned.get("termo_pdf"):
                self.add_error("termo_pdf", "O termo de responsabilidade (PDF) é obrigatório.")
            
            # Entrega: Usuário e Localidade são obrigatórios
            if acao == "entrega":
                if not cleaned.get("usuario"):
                    self.add_error("usuario", "Selecione o usuário para entrega.")
                if not cleaned.get("localidade_destino"):
                    self.add_error("localidade_destino", "Informe a localidade de destino.")
                # CC Destino é opcional aqui (preenchido pelo Model se vazio)

        elif tipo == "transferencia_equipamento":
            if not cleaned.get("localidade_destino"):
                self.add_error("localidade_destino", "Informe a nova localidade.")
            if not cleaned.get("centro_custo_destino"):
                self.add_error("centro_custo_destino", "Informe o novo centro de custo.")
            if not cleaned.get("status_transferencia"):
                self.add_error("status_transferencia", "Informe o novo status.")

        elif tipo == "entrada":
            if not cleaned.get("quantidade"): self.add_error("quantidade", "Quantidade obrigatória.")
            if not cleaned.get("numero_pedido"): self.add_error("numero_pedido", "Nº Pedido obrigatório.")

        elif tipo == "baixa":
            if not cleaned.get("quantidade"): self.add_error("quantidade", "Quantidade obrigatória.")
            if not cleaned.get("observacao"): self.add_error("observacao", "Justifique a baixa nas observações.")

        elif tipo == "envio_manutencao":
            if not cleaned.get("observacao"): self.add_error("observacao", "Descreva o problema nas observações.")

        elif tipo == "retorno_manutencao" or tipo == "retorno":
            if not cleaned.get("localidade_destino"): self.add_error("localidade_destino", "Informe onde o item será guardado.")

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
            except:
                pass

# ================== LICENÇAS ==================
ISO_FMT = "%Y-%m-%d"

class LicencaForm(forms.ModelForm):
    class Meta:
        model = Licenca
        # Apenas os campos solicitados
        fields = ["nome", "fornecedor", "pmb", "observacao"]
        
        widgets = {
            "nome": forms.TextInput(attrs={
                "class": "form-control", 
                "placeholder": "Ex: Microsoft Office 365"
            }),
            "fornecedor": forms.Select(attrs={
                "class": "form-control"
            }),
            "pmb": forms.Select(attrs={
                "class": "form-control"
            }),
            "observacao": forms.Textarea(attrs={
                "class": "form-control", 
                "rows": 4, 
                "placeholder": "Insira detalhes adicionais aqui..."
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Placeholder para o Select2 funcionar bonito
        self.fields['fornecedor'].empty_label = "Selecione um Fornecedor..."

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
                    
                    # 4. [MATEMÁTICA CORRIGIDA] Cálculo do Custo Mensal (Alocação)
                    custo_base = lote_alvo.custo_ciclo or Decimal(0)
                    periodicidade = str(lote_alvo.periodicidade).lower()

                    quantidade = lote_alvo.quantidade_total
                    valor_Lote = lote_alvo.custo_ciclo
                    if periodicidade == 'anual':
                        # Anual: R$ 600,00 / 12 = R$ 50,00/mês
                        instance.valor_unitario = (valor_Lote / quantidade) / 12
                    elif periodicidade == 'semestral':
                        # Semestral: R$ 300,00 / 6 = R$ 50,00/mês
                        instance.valor_unitario = custo_base / Decimal(6)
                    elif periodicidade == 'trimestral':
                        instance.valor_unitario = custo_base / Decimal(3)
                    else:
                        # Mensal (ou indefinido): Valor integral
                        instance.valor_unitario = custo_base

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