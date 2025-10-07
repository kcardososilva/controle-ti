# admin.py
from django.contrib import admin
from django.db.models import Sum
from .models import (
    Categoria, Subtipo, Localidade, Fornecedor, CentroCusto, Funcao,
    Item, Usuario, Locacao, Comentario, CicloManutencao, MovimentacaoItem,
    CheckListModelo, CheckListPergunta, Preventiva, PreventivaResposta,
    Licenca, MovimentacaoLicenca, LicencaLote,
    StatusItemChoices, TipoMovimentacaoChoices, TipoTransferenciaChoices,
    SimNaoChoices,
)

# ---------------------------
# Helpers/Audit
# ---------------------------
class AuditAdminMixin(admin.ModelAdmin):
    readonly_fields = ("criado_por", "atualizado_por", "created_at", "updated_at")
    list_per_page = 25
    date_hierarchy = "created_at"

    def save_model(self, request, obj, form, change):
        if not change and not getattr(obj, "criado_por_id", None):
            obj.criado_por = request.user
        obj.atualizado_por = request.user
        super().save_model(request, obj, form, change)


# ---------------------------
# Bases
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
    search_fields = ("local",)
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
# Item / Equipamento
# ---------------------------
class LocacaoInline(admin.StackedInline):
    model = Locacao
    extra = 0
    max_num = 1
    autocomplete_fields = ("fornecedor",)
    fk_name = "equipamento"


class ComentarioInline(admin.TabularInline):
    model = Comentario
    extra = 0
    fields = ("texto", "criado_por", "created_at")
    readonly_fields = ("criado_por", "created_at")


@admin.register(Item)
class ItemAdmin(AuditAdminMixin):
    list_display = (
        "id", "nome", "numero_serie", "status", "quantidade",
        "centro_custo", "localidade", "subtipo", "fornecedor", "valor", "pmb", "item_consumo",
    )
    list_filter = (
        "status", "pmb", "item_consumo", "categoria", "subtipo",
        "localidade", "centro_custo", "fornecedor",
    )
    search_fields = ("nome", "numero_serie", "marca", "modelo")
    autocomplete_fields = ("centro_custo", "localidade", "subtipo", "categoria", "fornecedor")
    inlines = [LocacaoInline, ComentarioInline]
    list_select_related = ("centro_custo", "localidade", "subtipo", "fornecedor", "categoria")
    ordering = ("nome",)


@admin.register(Locacao)
class LocacaoAdmin(AuditAdminMixin):
    list_display = ("id", "equipamento", "tempo_locado", "valor_mensal", "fornecedor")
    search_fields = ("equipamento__nome", "fornecedor__nome")
    autocomplete_fields = ("equipamento", "fornecedor")
    list_select_related = ("equipamento", "fornecedor")


@admin.register(Comentario)
class ComentarioAdmin(AuditAdminMixin):
    list_display = ("id", "item", "texto")
    search_fields = ("texto", "item__nome")
    autocomplete_fields = ("item",)
    list_select_related = ("item",)


@admin.register(CicloManutencao)
class CicloManutencaoAdmin(AuditAdminMixin):
    list_display = ("id", "item", "status_inicial", "data_inicio", "data_fim", "custo")
    list_filter = ("status_inicial",)
    search_fields = ("item__nome",)
    autocomplete_fields = ("item",)
    list_select_related = ("item",)


@admin.register(MovimentacaoItem)
class MovimentacaoItemAdmin(AuditAdminMixin):
    list_display = (
        "id", "item", "tipo_movimentacao", "tipo_transferencia", "usuario",
        "quantidade", "localidade_origem", "localidade_destino",
        "centro_custo_origem", "centro_custo_destino", "custo", "created_at",
    )
    list_filter = (
        "tipo_movimentacao", "tipo_transferencia",
        "localidade_destino", "centro_custo_destino",
    )
    search_fields = ("item__nome", "usuario__nome", "chamado")
    autocomplete_fields = (
        "item", "usuario", "localidade_origem", "localidade_destino",
        "centro_custo_origem", "centro_custo_destino", "fornecedor_manutencao",
    )
    list_select_related = (
        "item", "usuario", "localidade_origem", "localidade_destino",
        "centro_custo_origem", "centro_custo_destino",
    )


# ---------------------------
# Usuário
# ---------------------------
@admin.register(Usuario)
class UsuarioAdmin(AuditAdminMixin):
    list_display = ("id", "nome", "email", "status", "pmb", "centro_custo", "localidade", "funcao", "data_inicio", "data_termino")
    list_filter = ("status", "pmb", "centro_custo", "localidade", "funcao")
    search_fields = ("nome", "email")
    autocomplete_fields = ("centro_custo", "localidade", "funcao")
    list_select_related = ("centro_custo", "localidade", "funcao")
    ordering = ("nome",)


# ---------------------------
# Checklist / Preventiva
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
    list_display = ("id", "checklist_modelo", "ordem", "texto_pergunta", "tipo_resposta", "obrigatorio")
    list_filter = ("tipo_resposta", "obrigatorio")
    search_fields = ("texto_pergunta", "checklist_modelo__nome")
    autocomplete_fields = ("checklist_modelo",)
    ordering = ("checklist_modelo", "ordem", "id")


@admin.register(Preventiva)
class PreventivaAdmin(AuditAdminMixin):
    list_display = ("id", "equipamento", "checklist_modelo", "data_ultima", "data_proxima", "dentro_do_prazo")
    list_filter = ("dentro_do_prazo", "checklist_modelo")
    search_fields = ("equipamento__nome",)
    autocomplete_fields = ("equipamento", "checklist_modelo")
    list_select_related = ("equipamento", "checklist_modelo")


@admin.register(PreventivaResposta)
class PreventivaRespostaAdmin(AuditAdminMixin):
    list_display = ("id", "preventiva", "pergunta", "resposta")
    search_fields = ("preventiva__equipamento__nome", "pergunta__texto_pergunta", "resposta")
    autocomplete_fields = ("preventiva", "pergunta")
    list_select_related = ("preventiva", "pergunta")


# ---------------------------
# Licenças / Lotes
# ---------------------------
class LicencaLoteInline(admin.TabularInline):
    model = LicencaLote
    extra = 0
    fields = ("quantidade_total", "quantidade_disponivel", "custo_ciclo", "data_compra", "fornecedor", "centro_custo", "observacao")
    autocomplete_fields = ("fornecedor", "centro_custo")
    show_change_link = True


@admin.register(Licenca)
class LicencaAdmin(AuditAdminMixin):
    list_display = (
        "id", "nome", "fornecedor", "centro_custo", "pmb",
        "periodicidade", "data_inicio", "data_fim",
        "quantidade", "custo", "custo_mensal_display", "custo_anual_display",
    )
    list_filter = ("pmb", "periodicidade", "fornecedor", "centro_custo")
    search_fields = ("nome",)
    autocomplete_fields = ("fornecedor", "centro_custo")
    inlines = [LicencaLoteInline]
    list_select_related = ("fornecedor", "centro_custo")
    ordering = ("nome",)

    @admin.display(description="Custo mensal", ordering="custo")
    def custo_mensal_display(self, obj: Licenca):
        val = obj.custo_mensal()
        return f"R$ {val:.2f}" if val is not None else "—"

    @admin.display(description="Custo anual", ordering="custo")
    def custo_anual_display(self, obj: Licenca):
        val = obj.custo_anual_estimado()
        return f"R$ {val:.2f}" if val is not None else "—"


@admin.register(LicencaLote)
class LicencaLoteAdmin(AuditAdminMixin):
    list_display = (
        "id", "licenca", "quantidade_total", "quantidade_disponivel",
        "custo_ciclo", "data_compra", "fornecedor", "centro_custo",
    )
    list_filter = ("licenca", "fornecedor", "centro_custo")
    search_fields = ("licenca__nome",)
    autocomplete_fields = ("licenca", "fornecedor", "centro_custo")
    list_select_related = ("licenca", "fornecedor", "centro_custo")
    ordering = ("-created_at", "-id")


@admin.register(MovimentacaoLicenca)
class MovimentacaoLicencaAdmin(AuditAdminMixin):
    list_display = (
        "id", "licenca", "tipo", "usuario", "centro_custo_destino",
        "lote", "custo_ciclo_usado_display", "custo_mensal_usado_display", "created_at",
    )
    list_filter = ("tipo", "licenca", "lote", "centro_custo_destino")
    search_fields = ("licenca__nome", "usuario__nome")
    autocomplete_fields = ("licenca", "usuario", "centro_custo_destino", "lote")
    list_select_related = ("licenca", "usuario", "centro_custo_destino", "lote")

    readonly_fields = ("custo_ciclo_usado_readonly", "custo_mensal_usado_readonly")

    fieldsets = (
        (None, {
            "fields": ("tipo", "licenca", "usuario", "centro_custo_destino", "lote", "observacao")
        }),
        ("Custos (aplicados nesta movimentação)", {
            "fields": ("custo_ciclo_usado_readonly", "custo_mensal_usado_readonly"),
        }),
        ("Auditoria", {
            "fields": ("criado_por", "atualizado_por", "created_at", "updated_at")
        }),
    )

    @admin.display(description="Custo do ciclo usado")
    def custo_ciclo_usado_display(self, obj: MovimentacaoLicenca):
        v = obj.custo_ciclo_usado
        return f"R$ {v:.2f}" if v is not None else "—"

    @admin.display(description="Custo mensal usado")
    def custo_mensal_usado_display(self, obj: MovimentacaoLicenca):
        v = obj.custo_mensal_usado
        return f"R$ {v:.2f}" if v is not None else "—"

    @admin.display(description="Custo do ciclo usado")
    def custo_ciclo_usado_readonly(self, obj: MovimentacaoLicenca):
        return self.custo_ciclo_usado_display(obj)

    @admin.display(description="Custo mensal usado")
    def custo_mensal_usado_readonly(self, obj: MovimentacaoLicenca):
        return self.custo_mensal_usado_display(obj)
    
