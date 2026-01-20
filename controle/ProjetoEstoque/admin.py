from django.contrib import admin
from django.db.models import Sum
from .models import (
    Categoria, Subtipo, Localidade, Fornecedor, CentroCusto, Funcao,
    Item, Usuario, Locacao, Comentario, CicloManutencao, MovimentacaoItem,
    CheckListModelo, CheckListPergunta, Preventiva, PreventivaExecucao, PreventivaResposta,
    Licenca, MovimentacaoLicenca, LicencaLote
)

# ---------------------------
# Helpers/Audit
# ---------------------------
class AuditAdminMixin(admin.ModelAdmin):
    """
    Mixin para preencher automaticamente campos de auditoria
    e configurar layout padrão.
    """
    readonly_fields = ("criado_por", "atualizado_por", "created_at", "updated_at")
    list_per_page = 25
    date_hierarchy = "created_at"

    def save_model(self, request, obj, form, change):
        if not change and not getattr(obj, "criado_por_id", None):
            obj.criado_por = request.user
        obj.atualizado_por = request.user
        super().save_model(request, obj, form, change)

    def save_formset(self, request, form, formset, change):
        instances = formset.save(commit=False)
        for instance in instances:
            if hasattr(instance, 'atualizado_por'):
                instance.atualizado_por = request.user
            if not instance.pk and hasattr(instance, 'criado_por'):
                instance.criado_por = request.user
            instance.save()
        formset.save_m2m()


# ---------------------------
# Cadastros Básicos
# ---------------------------
@admin.register(Categoria)
class CategoriaAdmin(AuditAdminMixin):
    list_display = ("id", "nome")
    search_fields = ("nome",)
    ordering = ("nome",)


@admin.register(Subtipo)
class SubtipoAdmin(AuditAdminMixin):
    list_display = ("id", "nome", "categoria", "alocado")
    list_filter = ("alocado", "categoria")
    search_fields = ("nome", "categoria__nome")
    autocomplete_fields = ("categoria",)
    ordering = ("nome",)


@admin.register(Localidade)
class LocalidadeAdmin(AuditAdminMixin):
    list_display = ("id", "local", "codigo")
    list_filter = ("codigo",)
    search_fields = ("local", "codigo")
    ordering = ("local",)


@admin.register(Fornecedor)
class FornecedorAdmin(AuditAdminMixin):
    list_display = ("id", "nome", "cnpj")
    search_fields = ("nome", "cnpj")
    ordering = ("nome",)


@admin.register(CentroCusto)
class CentroCustoAdmin(AuditAdminMixin):
    list_display = ("id", "numero", "departamento", "pmb")
    list_filter = ("pmb",)
    search_fields = ("numero", "departamento")
    ordering = ("numero",)


@admin.register(Funcao)
class FuncaoAdmin(AuditAdminMixin):
    list_display = ("id", "nome")
    search_fields = ("nome",)
    ordering = ("nome",)


# ---------------------------
# Usuário
# ---------------------------
@admin.register(Usuario)
class UsuarioAdmin(AuditAdminMixin):
    list_display = ("id", "nome", "email", "status", "pmb", "centro_custo", "localidade", "funcao")
    list_filter = ("status", "pmb", "centro_custo", "localidade", "funcao")
    search_fields = ("nome", "email")
    autocomplete_fields = ("centro_custo", "localidade", "funcao")
    list_select_related = ("centro_custo", "localidade", "funcao")
    ordering = ("nome",)


# ---------------------------
# Equipamentos (Item) e Relacionados
# ---------------------------
class LocacaoInline(admin.StackedInline):
    model = Locacao
    extra = 0
    max_num = 1
    # CORREÇÃO: Removido 'autocomplete_fields' para fornecedor, pois o campo não existe mais na Locacao.
    # Adicionado 'data_entrada' que criamos.
    fields = ("data_entrada", "tempo_locado", "valor_mensal", "contrato", "observacoes")


class ComentarioInline(admin.TabularInline):
    model = Comentario
    extra = 0
    fields = ("texto", "criado_por", "created_at")
    readonly_fields = ("criado_por", "created_at")


@admin.register(Item)
class ItemAdmin(AuditAdminMixin):
    list_display = (
        "id", "nome", "numero_serie", "status", "quantidade",
        "centro_custo", "localidade", "subtipo", "fornecedor", "locado"
    )
    # CORREÇÃO: Removido 'categoria' do filtro direto (use subtipo__categoria se precisar)
    list_filter = (
        "status", "pmb", "item_consumo", "subtipo",
        "localidade", "centro_custo", "fornecedor", "locado"
    )
    search_fields = ("nome", "numero_serie", "marca", "modelo", "numero_pedido")
    
    # Fornecedor continua aqui, pois pertence ao Item
    autocomplete_fields = ("centro_custo", "localidade", "subtipo", "fornecedor")
    
    inlines = [LocacaoInline, ComentarioInline]
    list_select_related = ("centro_custo", "localidade", "subtipo", "fornecedor")
    ordering = ("nome",)


@admin.register(Locacao)
class LocacaoAdmin(AuditAdminMixin):
    # CORREÇÃO: Removido 'fornecedor' de list_display e autocomplete_fields
    list_display = ("id", "equipamento", "data_entrada", "tempo_locado", "valor_mensal", "contrato")
    search_fields = ("equipamento__nome", "contrato")
    autocomplete_fields = ("equipamento",)
    list_select_related = ("equipamento",)


@admin.register(Comentario)
class ComentarioAdmin(AuditAdminMixin):
    list_display = ("id", "item", "texto_curto", "criado_por", "created_at")
    search_fields = ("texto", "item__nome")
    autocomplete_fields = ("item",)
    list_select_related = ("item", "criado_por")

    @admin.display(description="Texto")
    def texto_curto(self, obj):
        return (obj.texto[:75] + '...') if len(obj.texto) > 75 else obj.texto


@admin.register(CicloManutencao)
class CicloManutencaoAdmin(AuditAdminMixin):
    list_display = ("id", "item", "status_inicial", "data_inicio", "data_fim", "custo")
    list_filter = ("status_inicial",)
    search_fields = ("item__nome", "causa")
    autocomplete_fields = ("item",)
    list_select_related = ("item",)


@admin.register(MovimentacaoItem)
class MovimentacaoItemAdmin(AuditAdminMixin):
    list_display = (
        "id", "item", "tipo_movimentacao", "tipo_transferencia", "usuario",
        "quantidade", "localidade_origem", "localidade_destino", "created_at"
    )
    list_filter = (
        "tipo_movimentacao", "tipo_transferencia",
        "localidade_destino", "centro_custo_destino",
    )
    search_fields = ("item__nome", "usuario__nome", "chamado", "numero_pedido")
    autocomplete_fields = (
        "item", "usuario", "localidade_origem", "localidade_destino",
        "centro_custo_origem", "centro_custo_destino", "fornecedor_manutencao",
    )
    list_select_related = (
        "item", "usuario", "localidade_origem", "localidade_destino",
        "centro_custo_origem", "centro_custo_destino",
    )


# ---------------------------
# Manutenção Preventiva / Checklist
# ---------------------------
class CheckListPerguntaInline(admin.TabularInline):
    model = CheckListPergunta
    extra = 0
    fields = ("ordem", "texto_pergunta", "tipo_resposta", "obrigatorio", "opcoes")
    ordering = ("ordem", "id")


@admin.register(CheckListModelo)
class CheckListModeloAdmin(AuditAdminMixin):
    list_display = ("id", "nome", "ativo", "subtipo", "intervalo_dias")
    list_filter = ("ativo", "subtipo")
    search_fields = ("nome",)
    autocomplete_fields = ("subtipo",)
    inlines = [CheckListPerguntaInline]
    list_select_related = ("subtipo",)
    ordering = ("nome",)


@admin.register(CheckListPergunta)
class CheckListPerguntaAdmin(AuditAdminMixin):
    list_display = ("id", "checklist_modelo", "ordem", "texto_pergunta", "tipo_resposta")
    list_filter = ("tipo_resposta", "obrigatorio")
    search_fields = ("texto_pergunta", "checklist_modelo__nome")
    autocomplete_fields = ("checklist_modelo",)
    ordering = ("checklist_modelo", "ordem")


@admin.register(Preventiva)
class PreventivaAdmin(AuditAdminMixin):
    list_display = ("id", "equipamento", "checklist_modelo", "data_ultima", "data_proxima", "dentro_do_prazo")
    list_filter = ("dentro_do_prazo", "checklist_modelo")
    search_fields = ("equipamento__nome",)
    autocomplete_fields = ("equipamento", "checklist_modelo")
    list_select_related = ("equipamento", "checklist_modelo")


# Administração de Execuções e Respostas
class PreventivaRespostaInline(admin.TabularInline):
    model = PreventivaResposta
    extra = 0
    fields = ("pergunta", "resposta")
    readonly_fields = ("pergunta", "resposta")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(PreventivaExecucao)
class PreventivaExecucaoAdmin(AuditAdminMixin):
    list_display = ("id", "preventiva_link", "data_execucao", "criado_por", "created_at")
    list_filter = ("data_execucao",)
    search_fields = ("preventiva__equipamento__nome", "observacao")
    inlines = [PreventivaRespostaInline]
    date_hierarchy = "data_execucao"

    @admin.display(description="Preventiva / Equipamento")
    def preventiva_link(self, obj):
        return f"{obj.preventiva.equipamento.nome} ({obj.preventiva.id})"


@admin.register(PreventivaResposta)
class PreventivaRespostaAdmin(AuditAdminMixin):
    list_display = ("id", "execucao", "pergunta", "resposta_curta")
    search_fields = ("execucao__preventiva__equipamento__nome", "pergunta__texto_pergunta", "resposta")
    list_select_related = ("execucao", "pergunta")

    @admin.display(description="Resposta")
    def resposta_curta(self, obj):
        return (obj.resposta[:50] + '...') if obj.resposta and len(obj.resposta) > 50 else obj.resposta


# ---------------------------
# Licenças de Software
# ---------------------------
class LicencaLoteInline(admin.TabularInline):
    model = LicencaLote
    extra = 0
    fields = (
        "quantidade_total", "quantidade_disponivel", "custo_ciclo", 
        "periodicidade", "data_compra", "numero_pedido",
        "fornecedor", "centro_custo"
    )
    autocomplete_fields = ("fornecedor", "centro_custo")
    show_change_link = True


@admin.register(Licenca)
class LicencaAdmin(AuditAdminMixin):
    list_display = ("id", "nome", "fornecedor", "pmb", "created_at")
    list_filter = ("pmb", "fornecedor")
    search_fields = ("nome",)
    autocomplete_fields = ("fornecedor",)
    ordering = ("nome",)


@admin.register(LicencaLote)
class LicencaLoteAdmin(AuditAdminMixin):
    list_display = (
        "id", "licenca", "quantidade_total", "quantidade_disponivel",
        "custo_ciclo", "periodicidade", "data_compra", "fornecedor"
    )
    list_filter = ("periodicidade", "licenca", "fornecedor")
    search_fields = ("licenca__nome", "numero_pedido", "observacao")
    autocomplete_fields = ("licenca", "fornecedor", "centro_custo")
    list_select_related = ("licenca", "fornecedor", "centro_custo")
    ordering = ("-created_at",)


@admin.register(MovimentacaoLicenca)
class MovimentacaoLicencaAdmin(AuditAdminMixin):
    list_display = (
        "id", "licenca", "tipo", "usuario", 
        "lote", "centro_custo_destino", "valor_unitario_display", "created_at"
    )
    list_filter = ("tipo", "licenca", "centro_custo_destino")
    search_fields = ("licenca__nome", "usuario__nome", "observacao")
    autocomplete_fields = ("licenca", "usuario", "centro_custo_destino", "lote")
    list_select_related = ("licenca", "usuario", "centro_custo_destino", "lote")
    
    fieldsets = (
        (None, {
            "fields": ("tipo", "licenca", "lote", "usuario", "centro_custo_destino", "observacao")
        }),
        ("Financeiro", {
            "fields": ("valor_unitario",),
            "description": "Valor unitário congelado no momento da movimentação."
        }),
        ("Auditoria", {
            "fields": ("criado_por", "atualizado_por", "created_at", "updated_at")
        }),
    )

    @admin.display(description="Valor Unit.")
    def valor_unitario_display(self, obj):
        return f"R$ {obj.valor_unitario:.2f}" if obj.valor_unitario else "-"