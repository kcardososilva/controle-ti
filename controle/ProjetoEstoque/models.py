from django.db import models
from django.contrib.auth.models import User

class Categoria(models.Model):
    nome = models.CharField(max_length=100)

    def __str__(self):
        return self.nome

class Subtipo(models.Model):
    nome = models.CharField(max_length=100)
    categoria = models.ForeignKey(Categoria, on_delete=models.CASCADE, related_name='subtipos')

    def __str__(self):
        return f"{self.nome} ({self.categoria.nome})"

class Equipamento(models.Model):
    STATUS_CHOICES = [
        ('ativo', 'Ativo'),
        ('backup', 'Backup'),
        ('manutencao', 'Manutenção'),
        ('queimado', 'Defeito'),
    ]

    nome = models.CharField(max_length=100)
    categoria = models.ForeignKey(Categoria, on_delete=models.PROTECT)
    subtipo = models.ForeignKey(Subtipo, on_delete=models.PROTECT, blank=True, null=True)
    numero_serie = models.CharField(max_length=100, unique=True)
    marca = models.CharField(max_length=100, blank=True, null=True)
    modelo = models.CharField(max_length=100, blank=True, null=True)
    local = models.CharField(max_length=100)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ativo')
    quantidade = models.PositiveIntegerField(default=1)
    estoque_minimo = models.PositiveIntegerField(default=0)
    observacoes = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)
    criado_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='equipamentos_criados')
    atualizado_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='equipamentos_atualizados')

    def save(self, *args, **kwargs):
        if not self.pk and not self.criado_por:
            self.criado_por = getattr(self, '_user', None)
        self.atualizado_por = getattr(self, '_user', None)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.nome} - {self.numero_serie}"

class Comentario(models.Model):
    equipamento = models.ForeignKey('Equipamento', on_delete=models.CASCADE, related_name='comentarios')
    autor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    texto = models.TextField()
    criado_em = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Comentário por {self.autor} em {self.equipamento}"