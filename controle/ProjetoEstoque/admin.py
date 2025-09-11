from django.contrib import admin
from .models import (
    Categoria, Subtipo, Usuario, Fornecedor, Localidade, Funcao, CentroCusto,
    Item, Locacao, Comentario, MovimentacaoItem, CicloManutencao
)


# ========== CLASSES AUXILIARES ==========
class AuditAdmin(admin.ModelAdmin):
    """Base para mostrar auditoria automaticamente"""
    readonly_fields = ('criado_por', 'atualizado_por', 'created_at', 'updated_at')
    list_filter = ('created_at', 'updated_at')
    search_fields = ('id',)


# ========== ADMIN ITEM ==========
@admin.register(Item)
class ItemAdmin(AuditAdmin):
    list_display = (
        "id", "nome", "numero_serie", "marca", "modelo",
        "status", "quantidade", "item_consumo", "pmb", "precisa_preventiva", "locado"
    )
    list_filter = ("status", "item_consumo", "pmb", "precisa_preventiva", "locado", "categoria", "subtipo")
    search_fields = ("nome", "numero_serie", "marca", "modelo")
    fieldsets = (
        ("Identificação", {
            "fields": ("nome", "numero_serie", "marca", "modelo")
        }),
        ("Estoque e Valor", {
            "fields": ("quantidade", "item_consumo", "pmb", "valor", "status")
        }),
        ("Relações", {
            "fields": ("fornecedor", "categoria", "subtipo", "localidade")
        }),
        ("Manutenção Preventiva", {
            "fields": ("precisa_preventiva", "data_limite_preventiva")
        }),
        ("Compra / Locação", {
            "fields": ("data_compra", "numero_pedido", "locado")
        }),
        ("Observações", {
            "fields": ("observacoes",)
        }),
        ("Auditoria", {
            "fields": ("criado_por", "atualizado_por", "created_at", "updated_at")
        }),
    )


# ========== ADMIN LOCAÇÃO ==========
@admin.register(Locacao)
class LocacaoAdmin(AuditAdmin):
    list_display = ("equipamento", "tempo_locado", "fornecedor")
    search_fields = ("equipamento__nome", "fornecedor__nome")
    fieldsets = (
        ("Informações de Locação", {
            "fields": ("equipamento", "tempo_locado", "contrato", "observacoes", "fornecedor")
        }),
        ("Auditoria", {
            "fields": ("criado_por", "atualizado_por", "created_at", "updated_at")
        }),
    )


# ========== ADMIN BÁSICOS ==========
@admin.register(Categoria)
class CategoriaAdmin(AuditAdmin):
    list_display = ("id", "nome")


@admin.register(Subtipo)
class SubtipoAdmin(AuditAdmin):
    list_display = ("id", "nome", "alocado", "categoria")
    list_filter = ("alocado", "categoria")


@admin.register(Fornecedor)
class FornecedorAdmin(AuditAdmin):
    list_display = ("id", "nome", "cnpj")


@admin.register(Localidade)
class LocalidadeAdmin(AuditAdmin):
    list_display = ("id", "local")


@admin.register(Funcao)
class FuncaoAdmin(AuditAdmin):
    list_display = ("id", "nome")


@admin.register(CentroCusto)
class CentroCustoAdmin(AuditAdmin):
    list_display = ("id", "numero", "departamento")


@admin.register(Usuario)
class UsuarioAdmin(AuditAdmin):
    list_display = ("id", "nome", "status", "email", "centro_custo", "localidade", "funcao")
    list_filter = ("status", "centro_custo", "localidade")


# ========== ADMIN MANUTENÇÃO ==========
@admin.register(CicloManutencao)
class CicloManutencaoAdmin(AuditAdmin):
    list_display = ("id", "item", "status_inicial", "data_inicio", "data_fim", "custo")
    list_filter = ("status_inicial", "data_inicio")


# ========== ADMIN MOVIMENTAÇÃO ==========
@admin.register(MovimentacaoItem)
class MovimentacaoItemAdmin(admin.ModelAdmin):
    list_display = (
        "tipo_movimentacao",
        "item",
        "usuario",
        "localidade_origem",
        "localidade_destino",
        "centro_custo_origem",
        "centro_custo_destino",
        "quantidade",
        "custo",
    )
    list_filter = ("tipo_movimentacao", "localidade_origem", "localidade_destino", "centro_custo_origem", "centro_custo_destino")

# ========== ADMIN COMENTÁRIOS ==========
@admin.register(Comentario)
class ComentarioAdmin(AuditAdmin):
    list_display = ("id", "item", "texto", "created_at")
    search_fields = ("texto",)

### Licenças
