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



OPCOES = [('ok', 'Ok'), ('nao_ok', 'Não Ok')]

class PreventivaFormComum(forms.ModelForm):
    status_cabo_ethernet = forms.ChoiceField(label="Confirme se todos os cabos Ethernet estão conectados corretamente e sem sinais de desgaste ou danos", choices=OPCOES, widget=forms.RadioSelect)
    limpeza_equipamento = forms.ChoiceField(label="Remover poeira e sujeira acumulada nas portas, ventiladores e em outras partes do equipamento", choices=OPCOES, widget=forms.RadioSelect)
    status_leds = forms.ChoiceField(label="Observe os LEDs do equipamento para garantir que todas as portas estão operando e não há falhas ou interrupções no sinal", choices=OPCOES, widget=forms.RadioSelect)
    imagem_antes = forms.ImageField(label="Foto Antes da Preventiva (opcional)", required=False)
    imagem_depois = forms.ImageField(label="Foto Depois da Preventiva (opcional)", required=False)
    observacoes = forms.CharField(widget=forms.Textarea(attrs={'rows': 3, 'placeholder': 'Insira as Observações referente aos tópicos'}), required=False)
    

    class Meta:
        model = Preventiva
        fields = ['imagem_antes','imagem_depois','observacoes', 'status_cabo_ethernet', 'limpeza_equipamento', 'status_leds']

class PreventivaFormSwitch(PreventivaFormComum):
   
    status_temperatura = forms.ChoiceField(label="Muitos switches possuem sensores de temperatura que ajudam a garantir que o equipamento não esteja superaquecendo", choices=OPCOES, widget=forms.RadioSelect)
    status_teste_portas = forms.ChoiceField(label="Execute testes de conectividade para garantir que todas as portas estão funcionando corretamente", choices=OPCOES, widget=forms.RadioSelect)
    status_teste_rede = forms.ChoiceField(label="Após a manutenção, realize testes de rede para garantir que as configurações do switch estão funcionando corretamente", choices=OPCOES, widget=forms.RadioSelect)

    class Meta(PreventivaFormComum.Meta):
        fields = PreventivaFormComum.Meta.fields + [
            'status_temperatura',
            'status_teste_portas',
            'status_teste_rede',
        ]

class PreventivaFormAP(PreventivaFormComum):
    status_local_ap = forms.ChoiceField(label="Confirme se o AP está localizado em um local adequado, longe de fontes de calor excessivo ou interferência", choices=OPCOES, widget=forms.RadioSelect)
    status_velocidade_ap = forms.ChoiceField(label="Realize testes de velocidade periódicos para garantir que o AP está oferecendo o desempenho esperado", choices=OPCOES, widget=forms.RadioSelect)
    status_cobertura_ap = forms.ChoiceField(label="Teste a cobertura do sinal Wi-Fi para garantir que o AP está alcançando todas as áreas desejadas", choices=OPCOES, widget=forms.RadioSelect)
   

    class Meta(PreventivaFormComum.Meta):
        fields = PreventivaFormComum.Meta.fields + [
            'status_local_ap',
            'status_velocidade_ap',
            'status_cobertura_ap',
            
        ]