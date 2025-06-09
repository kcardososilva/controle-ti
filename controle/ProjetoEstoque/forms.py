from django import forms
from .models import Categoria, Subtipo, Equipamento, Comentario, Preventiva



class CategoriaForm(forms.ModelForm):
    class Meta:
        model = Categoria
        fields = ['nome']
        widgets = {
            'nome': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Nome da categoria'})
        }

class SubtipoForm(forms.ModelForm):
    class Meta:
        model = Subtipo
        fields = ['nome', 'categoria']
        widgets = {
            'nome': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Nome do subtipo'}),
            'categoria': forms.Select(attrs={'class': 'form-control'}),
        }

class EquipamentoForm(forms.ModelForm):
    class Meta:
        model = Equipamento
        fields = [
            'nome',
            'categoria',
            'subtipo',
            'numero_serie',
            'marca',
            'modelo',
            'local',
            'status',
            'quantidade',
            'estoque_minimo',
            'precisa_preventiva',
            'data_limite_preventiva',
            'observacoes',
        ]
        widgets = {
            'nome': forms.TextInput(attrs={'class': 'form-control'}),
            'categoria': forms.Select(attrs={'class': 'form-control'}),
            'subtipo': forms.Select(attrs={'class': 'form-control'}),
            'numero_serie': forms.TextInput(attrs={'class': 'form-control'}),
            'marca': forms.TextInput(attrs={'class': 'form-control'}),
            'modelo': forms.TextInput(attrs={'class': 'form-control'}),
            'local': forms.TextInput(attrs={'class': 'form-control'}),
            'status': forms.Select(attrs={'class': 'form-control'}),
            'quantidade': forms.NumberInput(attrs={'class': 'form-control'}),
            'estoque_minimo': forms.NumberInput(attrs={'class': 'form-control'}),
            'precisa_preventiva': forms.Select(choices=[(True, 'Sim'), (False, 'Não')], attrs={'class': 'form-control'}),
            'data_limite_preventiva': forms.NumberInput(attrs={'class': 'form-control', 'min': 1}),
            'observacoes': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
        }

class ComentarioForm(forms.ModelForm):
    class Meta:
        model = Comentario
        fields = ['texto']
        widgets = {
            'texto': forms.Textarea(attrs={'rows': 3, 'placeholder': 'Escreva seu comentário...'}),
        }

class PreventivaForm(forms.ModelForm):
    class Meta:
        model = Preventiva
        fields = ['observacoes']
        widgets = {
            'observacoes': forms.Textarea(attrs={'rows': 3, 'placeholder': 'Descreva o que foi feito na preventiva...'})
        }
