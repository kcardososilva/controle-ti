from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from dateutil.relativedelta import relativedelta
from datetime import datetime, timedelta, date


## Banco categoria ##
class Categoria(models.Model):
    nome = models.CharField(max_length=100)

    def __str__(self):
        return self.nome
## banco Subtipo
class Subtipo(models.Model):
    nome = models.CharField(max_length=100)
    categoria = models.ForeignKey(Categoria, on_delete=models.CASCADE, related_name='subtipos')

    def __str__(self):
        return f"{self.nome} ({self.categoria.nome})"

## banco equipamentos ##
class Equipamento(models.Model):
    STATUS_CHOICES = [
        ('ativo', 'Ativo'),
        ('backup', 'Backup'),
        ('correcao', 'Correção'),
        ('manutencao', 'Manutenção'),
        ('queimado', 'Queimado'),
    ]

    nome = models.CharField(max_length=100)
    categoria = models.ForeignKey(Categoria, on_delete=models.PROTECT)
    subtipo = models.ForeignKey(Subtipo, on_delete=models.PROTECT)
    numero_serie = models.CharField(max_length=100, unique=True, null=True,  blank=True)
    marca = models.CharField(max_length=100, blank=True, null=True)
    modelo = models.CharField(max_length=100, blank=True, null=True)
    local = models.CharField(max_length=100)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ativo')
    quantidade = models.PositiveIntegerField(default=1)
    estoque_minimo = models.PositiveIntegerField(default=0)
    observacoes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    precisa_preventiva = models.CharField(max_length=3, choices=[('sim', 'Sim'), ('nao', 'Não')], default='nao')
    data_limite_preventiva = models.PositiveIntegerField(blank=True, null=True, help_text="Intervalo em meses")
    criado_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='equipamentos_criados')
    atualizado_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='equipamentos_atualizados')


    def __str__(self):
        return f"{self.nome} - {self.numero_serie}"

## Banco comentários ##
class Comentario(models.Model):
    equipamento = models.ForeignKey('Equipamento', on_delete=models.CASCADE, related_name='comentarios')
    autor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    texto = models.TextField()
    criado_em = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Comentário por {self.autor} em {self.equipamento}"
    


## Banco Preventivas ##

OPCOES = [
    ('ok', 'Ok'),
    ('nao_ok', 'Não Ok'),
]

class Preventiva(models.Model):
    equipamento = models.ForeignKey('Equipamento', on_delete=models.CASCADE)
    data_ultima = models.DateTimeField()
    data_proxima = models.DateField(null=True, blank=True)
    autor = models.ForeignKey('auth.User', on_delete=models.CASCADE)
    dentro_do_prazo = models.CharField(max_length=10, choices=[('sim', 'Sim'), ('não', 'Não')], default='não')
    observacoes = models.TextField(blank=True, null=True)
    imagem_antes = models.ImageField(upload_to='preventivas/antes/', blank=True, null=True)
    imagem_depois = models.ImageField(upload_to='preventivas/depois/', blank=True, null=True)

    # Campos para perguntas objetivas
    status_cabo_ethernet = models.CharField(max_length=10, choices=OPCOES, blank=True, null=True)
    limpeza_equipamento = models.CharField(max_length=10, choices=OPCOES, blank=True, null=True)
    status_leds = models.CharField(max_length=10, choices=OPCOES, blank=True, null=True)
    status_firmware = models.CharField(max_length=10, choices=OPCOES, blank=True, null=True)
    status_firmware_bkp = models.CharField(max_length=10, choices=OPCOES, blank=True, null=True)
    status_congestionamento = models.CharField(max_length=10, choices=OPCOES, blank=True, null=True)
    status_temperatura = models.CharField(max_length=10, choices=OPCOES, blank=True, null=True)
    status_teste_portas = models.CharField(max_length=10, choices=OPCOES, blank=True, null=True)
    status_failover = models.CharField(max_length=10, choices=OPCOES, blank=True, null=True)
    status_teste_rede = models.CharField(max_length=10, choices=OPCOES, blank=True, null=True)
    status_local_ap = models.CharField(max_length=10, choices=OPCOES, blank=True, null=True)
    status_velocidade_ap = models.CharField(max_length=10, choices=OPCOES, blank=True, null=True)
    status_cobertura_ap = models.CharField(max_length=10, choices=OPCOES, blank=True, null=True)
    status_canais_ap = models.CharField(max_length=10, choices=OPCOES, blank=True, null=True)
    status_wps_ap = models.CharField(max_length=10, choices=OPCOES, blank=True, null=True)
    copia_seguranca_ap = models.CharField(max_length=10, choices=OPCOES, blank=True, null=True)

    def save(self, *args, **kwargs):
        # Garante que data_proxima seja recalculada
        if not self.data_proxima:
            meses = self.equipamento.data_limite_preventiva or 3
            self.data_proxima = self.data_ultima + timedelta(days=30 * meses)

        # Correção: comparar datas no mesmo formato
        if self.data_proxima and self.data_ultima:
            if self.data_ultima.date() <= self.data_proxima.date():
                self.dentro_do_prazo = "sim"
            else:
                self.dentro_do_prazo = "não"
        super().save(*args, **kwargs)

