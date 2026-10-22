from django.contrib import admin
from django.db.models import Sum
from .models import (
    Categoria, Subtipo, Localidade, Fornecedor, CentroCusto, Funcao,
    Item, Usuario, Locacao, Comentario, CicloManutencao, MovimentacaoItem,
    CheckListModelo, CheckListPergunta, Preventiva, PreventivaExecucao, PreventivaResposta,
    PreventivaAreaResponsavel, PreventivaCicloMeta,
    Licenca, MovimentacaoLicenca, LicencaLote, LicencaOffice, ItemLote, LoteEstoque,
    PlantaProjeto, PerfilFornecedor, PerfilParceiroLicenca, OrdemManutencao, OrdemManutencaoEvento,
    LocacaoPeriodo, ItemColaborador, RegistroSeguranca, NovidadeSistema,
    TermoAssinatura,
    RedeDispositivo, RedePorta, RedeRack, RedeRota, RedeTopologia,
    RedeTopologiaHistorico,
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


@admin.register(PerfilFornecedor)
class PerfilFornecedorAdmin(AuditAdminMixin):
    list_display = ("id", "usuario", "fornecedor", "ativo")
    list_filter = ("ativo", "fornecedor")
    search_fields = ("usuario__username", "usuario__email", "fornecedor__nome")
    autocomplete_fields = ("usuario", "fornecedor")
    list_select_related = ("usuario", "fornecedor")


@admin.register(PerfilParceiroLicenca)
class PerfilParceiroLicencaAdmin(AuditAdminMixin):
    list_display = ("id", "usuario", "parceiro", "ativo")
    list_filter = ("ativo", "parceiro")
    search_fields = ("usuario__username", "usuario__email", "parceiro__nome")
    autocomplete_fields = ("usuario", "parceiro")
    list_select_related = ("usuario", "parceiro")


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
        "centro_custo", "localidade", "subtipo", "fornecedor", "locado", "compartilhado",
        "excluido",
    )
    # CORREÇÃO: Removido 'categoria' do filtro direto (use subtipo__categoria se precisar)
    list_filter = (
        "status", "pmb", "item_consumo", "subtipo",
        "localidade", "centro_custo", "fornecedor", "locado", "compartilhado", "excluido",
    )
    search_fields = ("nome", "numero_serie", "marca", "modelo", "numero_pedido", "numero_nf")

    # Fornecedor continua aqui, pois pertence ao Item
    autocomplete_fields = ("centro_custo", "localidade", "subtipo", "fornecedor")

    inlines = [LocacaoInline, ComentarioInline]
    list_select_related = ("centro_custo", "localidade", "subtipo", "fornecedor")
    ordering = ("nome",)
    readonly_fields = AuditAdminMixin.readonly_fields + ("excluido_em", "excluido_por")
    actions = ["restaurar_selecionados"]

    def get_queryset(self, request):
        # `Item.objects` (default manager) já esconde itens excluídos (soft
        # delete) — no admin, staff precisa enxergá-los para poder restaurar.
        qs = Item.all_objects.get_queryset()
        ordering = self.get_ordering(request)
        if ordering:
            qs = qs.order_by(*ordering)
        return qs

    @admin.action(description="Restaurar equipamentos selecionados (desfaz exclusão lógica)")
    def restaurar_selecionados(self, request, queryset):
        atualizados = queryset.filter(excluido=True).update(
            excluido=False, excluido_em=None, excluido_por=None,
        )
        self.message_user(request, f"{atualizados} equipamento(s) restaurado(s).")


@admin.register(ItemColaborador)
class ItemColaboradorAdmin(AuditAdminMixin):
    list_display = ("id", "item", "colaborador", "ativo", "data_vinculo", "data_devolucao")
    list_filter = ("ativo",)
    search_fields = ("item__nome", "item__numero_serie", "colaborador__nome", "colaborador__matricula")
    autocomplete_fields = ("item", "colaborador")
    list_select_related = ("item", "colaborador")
    date_hierarchy = "data_vinculo"


@admin.register(LocacaoPeriodo)
class LocacaoPeriodoAdmin(AuditAdminMixin):
    list_display = ("id", "item", "valor_mensal", "data_inicio", "data_fim", "motivo_fim")
    list_filter = ("motivo_fim",)
    search_fields = ("item__nome", "item__numero_serie")
    autocomplete_fields = ("item",)
    list_select_related = ("item",)
    date_hierarchy = "data_inicio"


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


class OrdemManutencaoEventoInline(admin.TabularInline):
    model = OrdemManutencaoEvento
    extra = 0
    fields = ("status", "observacao", "criado_por", "created_at")
    readonly_fields = ("criado_por", "created_at")


@admin.register(OrdemManutencao)
class OrdemManutencaoAdmin(AuditAdminMixin):
    list_display = ("id", "item", "fornecedor", "status", "item_substituto", "finalizada_em", "created_at")
    list_filter = ("status", "fornecedor")
    search_fields = ("item__nome", "item__numero_serie", "fornecedor__nome", "chamado")
    autocomplete_fields = ("item", "fornecedor", "item_substituto", "movimentacao_origem")
    list_select_related = ("item", "fornecedor", "item_substituto")
    inlines = [OrdemManutencaoEventoInline]


@admin.register(OrdemManutencaoEvento)
class OrdemManutencaoEventoAdmin(AuditAdminMixin):
    list_display = ("id", "ordem", "status", "criado_por", "created_at")
    list_filter = ("status",)
    search_fields = ("ordem__item__nome", "observacao")
    autocomplete_fields = ("ordem",)
    list_select_related = ("ordem", "criado_por")


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


# Metas por área e ciclo de 90 dias. O cadastro tem UI própria em
# /preventiva/metas/configurar/ — o admin fica como porta de serviço
# (correção pontual, conferência de auditoria), não como fluxo principal.
@admin.register(PreventivaAreaResponsavel)
class PreventivaAreaResponsavelAdmin(AuditAdminMixin):
    list_display = ("area", "subtipo_label", "tecnico", "ativo")
    list_filter = ("area", "ativo", "subtipo")
    search_fields = ("tecnico__username", "tecnico__first_name", "tecnico__last_name")
    autocomplete_fields = ("subtipo", "tecnico")
    list_select_related = ("subtipo", "tecnico")

    @admin.display(description="Subtipo", ordering="subtipo__nome")
    def subtipo_label(self, obj):
        return obj.subtipo.nome if obj.subtipo else "Todos (padrão da área)"


@admin.register(PreventivaCicloMeta)
class PreventivaCicloMetaAdmin(AuditAdminMixin):
    list_display = ("subtipo", "data_inicio", "data_fim_col", "duracao_dias", "encerrado")
    list_filter = ("subtipo", "encerrado")
    date_hierarchy = "data_inicio"
    autocomplete_fields = ("subtipo",)
    list_select_related = ("subtipo",)

    @admin.display(description="Fim do ciclo")
    def data_fim_col(self, obj):
        return obj.data_fim


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
    list_display = ("id", "preventiva_link", "data_execucao", "hora_inicio", "hora_fim", "duracao_fmt", "criado_por", "created_at")
    list_filter = ("data_execucao",)
    search_fields = ("preventiva__equipamento__nome", "observacao")
    inlines = [PreventivaRespostaInline]
    date_hierarchy = "data_execucao"
    filter_horizontal = ("tecnicos_auxiliares",)

    @admin.display(description="Preventiva / Equipamento")
    def preventiva_link(self, obj):
        return f"{obj.preventiva.equipamento.nome} ({obj.preventiva.id})"

    @admin.display(description="Duração")
    def duracao_fmt(self, obj):
        return obj.duracao_formatada or "—"


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


@admin.register(LicencaOffice)
class LicencaOfficeAdmin(AuditAdminMixin):
    list_display = (
        "id", "produto", "chave_produto", "status", "item",
        "usuario_vinculado", "data_licenca",
    )
    list_filter = ("status",)
    search_fields = (
        "produto", "chave_produto", "conta_vinculada", "id_dell",
        "estacao_importada", "usuario_vinculado", "item__nome",
    )
    autocomplete_fields = ("item",)
    list_select_related = ("item",)
    ordering = ("produto", "chave_produto")


@admin.register(LoteEstoque)
class LoteEstoqueAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "numero_nf",
       
        "fornecedor",
        "data_entrada",
        "quantidade",
        "custo_unitario",
        
    )
    search_fields = ("numero_nf", "fornecedor__nome")
    list_filter = ("fornecedor", "data_entrada")
    readonly_fields = ("created_at", "updated_at")


@admin.register(ItemLote)
class ItemLoteAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "item",
        "lote",
        "quantidade_entrada",
        "quantidade_disponivel",
        "custo_unitario",
        
    )
    search_fields = ("item__nome", "item__numero_serie", "lote__numero_nf")
    list_filter = ("lote__fornecedor", "lote__data_entrada")
    readonly_fields = ("created_at", "updated_at")


@admin.register(PlantaProjeto)
class PlantaProjetoAdmin(AuditAdminMixin):
    list_display    = ("id", "nome", "localidade", "total_elementos", "elementos_com_prtg", "criado_por", "created_at")
    list_filter     = ("localidade",)
    search_fields   = ("nome", "descricao", "localidade__local")
    ordering        = ("localidade__local", "nome")
    readonly_fields = ("created_at", "updated_at", "criado_por", "atualizado_por")
    filter_horizontal = ("visualizadores_tv",)
    fieldsets = (
        (None, {"fields": ("nome", "descricao", "localidade", "imagem_fundo")}),
        ("Visualizadores TV", {
            "description": "Selecione os usuários do grupo 'Visualizador TV' que podem ver esta planta no modo TV.",
            "fields": ("visualizadores_tv",),
        }),
        ("Auditoria", {"fields": ("criado_por", "atualizado_por", "created_at", "updated_at"), "classes": ("collapse",)}),
    )


# ---------------------------
# Segurança — trilha de autenticação (ISO 27001 A.8.15/A.8.16)
# ---------------------------
@admin.register(RegistroSeguranca)
class RegistroSegurancaAdmin(admin.ModelAdmin):
    """Somente-leitura: evidência de eventos de autenticação para revisão."""
    list_display  = ("criado_em", "tipo", "username", "ip", "suspeito", "detalhe")
    list_filter   = ("tipo", "suspeito", "criado_em")
    search_fields = ("username", "ip", "user_agent", "caminho")
    date_hierarchy = "criado_em"
    list_per_page = 50
    readonly_fields = (
        "tipo", "username", "usuario", "ip", "user_agent",
        "caminho", "suspeito", "detalhe", "criado_em",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False  # visualização apenas — registros são imutáveis


# ---------------------------
# Novidades do sistema (changelog)
# ---------------------------
@admin.register(NovidadeSistema)
class NovidadeSistemaAdmin(AuditAdminMixin):
    list_display  = ("data", "tipo", "versao", "titulo", "ativo")
    list_filter   = ("tipo", "ativo", "data")
    search_fields = ("titulo", "descricao", "versao")
    list_editable = ("ativo",)
    date_hierarchy = "data"
    fieldsets = (
        (None, {"fields": ("tipo", "titulo", "descricao")}),
        ("Publicação", {"fields": ("versao", "data", "ativo")}),
        ("Auditoria", {"fields": ("criado_por", "atualizado_por", "created_at", "updated_at"), "classes": ("collapse",)}),
    )



# ---------------------------
# Assinatura digital de termos
# ---------------------------
@admin.register(TermoAssinatura)
class TermoAssinaturaAdmin(AuditAdminMixin):
    """
    Consulta e cancelamento. O ATO em si não é editável pelo admin: o traço, o
    PDF, o hash, o IP e os carimbos de tempo são prova do que aconteceu, e um
    campo editável ali transformaria a trilha de auditoria em algo que alguém
    com acesso ao admin pode reescrever — o que anula o motivo de ela existir.

    Criar também está desligado: a sessão nasce do fluxo com o colaborador
    presente (`termo_assinatura_service.criar_sessao`), que é quem gera o token
    e monta o snapshot do termo. Uma linha criada à mão aqui nasceria sem token
    utilizável e sem texto legal.

    **Os `readonly_fields` abaixo são a PRIMEIRA barreira, não a única.** A
    segunda é `TermoAssinatura.save()`, que recusa alterar a prova de um termo
    já assinado (`CAMPOS_IMUTAVEIS_APOS_ASSINATURA`, regra 42). As duas se
    complementam e nenhuma substitui a outra: aqui a proteção é da TELA e vale
    só para quem passa por ela — um `save()` em shell, num import ou numa
    rotina nova não encosta nesta lista. Por isso os dois conjuntos de campos
    são praticamente iguais; se um campo novo de prova aparecer, ele precisa
    entrar nos DOIS lugares.

    Note que o que sobra editável (`motivo_cancelamento`, `itens`,
    `movimentacoes`) está fora da lista imutável de propósito: o motivo do
    cancelamento não é prova da assinatura, e os M2M não passam pelo
    `save()` do model.
    """
    list_display = (
        "codigo_verificacao", "colaborador", "tipo", "status",
        "assinado_em", "expira_em", "criado_por",
    )
    list_filter = ("status", "tipo", "assinado_em")
    search_fields = (
        "codigo_verificacao", "numero_termo", "colaborador__nome",
        "signatario_nome", "pdf_sha256",
    )
    autocomplete_fields = ("colaborador",)
    filter_horizontal = ("itens", "movimentacoes")
    date_hierarchy = "created_at"
    list_per_page = 50

    readonly_fields = AuditAdminMixin.readonly_fields + (
        "tipo", "colaborador", "status", "token_hash", "expira_em",
        "numero_termo", "dados", "codigo_verificacao",
        "assinatura_colaborador", "assinatura_responsavel",
        "signatario_nome",
        "assinado_em", "assinado_ip", "assinado_user_agent", "aceite_declarado",
        "pdf", "pdf_sha256", "cancelado_em",
        # `desligamento` marca o EFEITO que a assinatura produziu (devolveu tudo,
        # removeu as licenças, desligou o cadastro). Virar esse booleano depois
        # do ato reescreveria o que o termo significa, então ele é prova como os
        # outros — e sem estar aqui o `save()` do model recusaria a gravação,
        # devolvendo 500 no admin em vez de mensagem.
        "desligamento",
        # Registro do envio da via: é histórico de uma ação do sistema. Editável
        # aqui, viraria "marque como enviada" — o oposto do que o campo existe
        # para responder.
        "via_email_status", "via_email_em", "via_email_tentativas", "via_email_detalhe",
    )

    fieldsets = (
        ("Identificação", {"fields": ("codigo_verificacao", "tipo", "status", "colaborador",
                                      "numero_termo", "desligamento")}),
        ("Via enviada ao colaborador", {
            "fields": ("via_email_status", "via_email_em", "via_email_tentativas",
                       "via_email_detalhe"),
            "classes": ("collapse",),
        }),
        # Sem CPF/RG: eles não são persistidos, existem só dentro do PDF
        # emitido (ver TermoAssinatura.signatario_nome e a regra 35).
        ("Signatário", {"fields": ("signatario_nome", "aceite_declarado")}),
        ("Ato", {"fields": ("assinado_em", "assinado_ip", "assinado_user_agent",
                            "assinatura_colaborador", "assinatura_responsavel")}),
        ("Documento", {"fields": ("pdf", "pdf_sha256")}),
        ("Sessão", {"fields": ("expira_em", "token_hash", "cancelado_em", "motivo_cancelamento"),
                    "classes": ("collapse",)}),
        ("Snapshot do termo", {"fields": ("dados",), "classes": ("collapse",)}),
        ("Vínculos", {"fields": ("itens", "movimentacoes")}),
        ("Auditoria", {"fields": ("criado_por", "atualizado_por", "created_at", "updated_at"),
                       "classes": ("collapse",)}),
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        # Termo assinado é prova; apagar a linha destruiria o registro do ato
        # junto com o documento. Cancelar sessão pendente é o caminho (UI).
        return False


# ---------------------------
# Mapa de Rede (topologia + portas de switch)
# ---------------------------
# O admin aqui é rede de segurança e conferência em massa, não a interface de
# trabalho — quem mapeia a rede usa a bancada (`/plantas/rede/`), onde o cabo
# é gravado nos dois sentidos pelo service. Por isso o enlace NÃO é editável
# em linha: `destino_porta` gravado à mão deixaria o grafo com uma perna só
# (ver o comentário no bloco de models e `rede_topologia_service.conectar`).


class RedePortaInline(admin.TabularInline):
    model = RedePorta
    fk_name = "dispositivo"
    extra = 0
    fields = ("numero", "rotulo", "tipo", "status", "velocidade", "poe", "vlan",
              "meio", "destino_item", "descricao")
    readonly_fields = ("destino_item",)
    ordering = ("numero",)
    autocomplete_fields = ()
    show_change_link = True


@admin.register(RedeDispositivo)
class RedeDispositivoAdmin(AuditAdminMixin):
    list_display = ("item", "papel", "ip_gerencia", "prtg_objid",
                    "modelo_portas", "col_portas", "col_ocupacao", "col_onde")
    list_filter = ("papel", "rack", "item__localidade", "item__area_operacional")
    search_fields = ("item__nome", "item__numero_serie", "ip_gerencia", "modelo_portas")
    autocomplete_fields = ("item", "rack")
    inlines = [RedePortaInline]

    def get_queryset(self, request):
        from django.db.models import Count, Q
        return (
            super().get_queryset(request)
            .select_related("item", "item__localidade", "rack")
            .annotate(
                n_portas=Count("portas", distinct=True),
                n_ativas=Count("portas", filter=Q(portas__status="ativa"), distinct=True),
            )
        )

    # As propriedades do model contam porta por linha; para a LISTA isso seria
    # uma consulta por dispositivo. `annotate` resolve as duas colunas numa
    # varredura só — a mesma razão pela qual o grafo não lê porta por nó.
    def get_list_select_related(self, request):
        return ("item", "item__localidade", "rack")

    @admin.display(description="Portas (em uso / total)", ordering="-n_portas")
    def col_portas(self, obj):
        return f"{obj.n_ativas} / {obj.n_portas}"

    @admin.display(description="Ocupação")
    def col_ocupacao(self, obj):
        if not obj.n_portas:
            return "—"
        return f"{round(obj.n_ativas * 100 / obj.n_portas)}%"

    @admin.display(description="Onde está")
    def col_onde(self, obj):
        """Rack + U, ou a coordenada. É a coluna que responde onde o técnico
        vai encontrar o ferro."""
        if obj.rack_id:
            return f"{obj.rack}{f' · U{obj.rack_u}' if obj.rack_u else ''}"
        if obj.latitude is not None:
            return f"{obj.latitude}, {obj.longitude}"
        return "—"


@admin.register(RedePorta)
class RedePortaAdmin(AuditAdminMixin):
    list_display = ("dispositivo", "numero", "rotulo", "tipo", "status",
                    "velocidade", "poe", "vlan", "meio", "destino_item",
                    "destino_porta", "rota")
    list_filter = ("status", "tipo", "meio", "poe", "rota", "dispositivo__papel")
    search_fields = ("rotulo", "vlan", "descricao",
                     "dispositivo__item__nome", "destino_item__nome")
    # Enlace é gravado pelo service, nos dois sentidos. Editar a FK aqui
    # criaria cabo de uma perna só, invisível em tela nenhuma.
    # `rota` entra junto: `vincular_rota` grava nas duas pernas, e mexer na
    # FK aqui deixaria o duto com metade dos cabos declarados.
    readonly_fields = ("destino_porta", "destino_item", "rota")

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            "dispositivo__item", "destino_item",
            "destino_porta__dispositivo__item", "rota",
        )


@admin.register(RedeTopologia)
class RedeTopologiaAdmin(AuditAdminMixin):
    list_display = ("nome", "localidade", "area", "versao", "total_nos", "updated_at")
    list_filter = ("localidade", "area")
    search_fields = ("nome", "descricao")
    readonly_fields = ("versao",)


@admin.register(RedeTopologiaHistorico)
class RedeTopologiaHistoricoAdmin(admin.ModelAdmin):
    list_display = ("topologia", "versao", "salvo_por", "salvo_em", "descricao")
    list_filter = ("topologia",)
    readonly_fields = ("topologia", "versao", "layout", "salvo_por", "salvo_em", "descricao")

    def has_add_permission(self, request):
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Planta georreferenciada (rack e rota)
# ──────────────────────────────────────────────────────────────────────────────

class RedeDispositivoNoRackInline(admin.TabularInline):
    """Quem está no armário, com a altura em U — a elevação em forma de lista."""
    model = RedeDispositivo
    fk_name = "rack"
    extra = 0
    fields = ("item", "papel", "rack_u", "rack_altura_u", "ip_gerencia")
    readonly_fields = ("item",)
    ordering = ("-rack_u",)
    show_change_link = True


@admin.register(RedeRack)
class RedeRackAdmin(AuditAdminMixin):
    list_display = ("nome", "codigo", "localidade", "ambiente", "us",
                    "col_ocupacao", "col_posicao")
    list_filter = ("localidade", "area")
    search_fields = ("nome", "codigo", "ambiente", "observacoes")
    inlines = [RedeDispositivoNoRackInline]

    def get_queryset(self, request):
        return (
            super().get_queryset(request)
            .select_related("localidade")
            .prefetch_related("equipamentos")
        )

    @admin.display(description="Ocupação (U)")
    def col_ocupacao(self, obj):
        return f"{obj.us_ocupados} / {obj.us}"

    @admin.display(description="No mapa", boolean=True)
    def col_posicao(self, obj):
        return obj.tem_geo


@admin.register(RedeRota)
class RedeRotaAdmin(AuditAdminMixin):
    list_display = ("nome", "localidade", "tipo", "instalacao", "col_metros",
                    "fibras", "col_enlaces", "col_pontos")
    list_filter = ("tipo", "instalacao", "localidade", "area")
    search_fields = ("nome", "observacoes")
    autocomplete_fields = ("rack_a", "rack_b")
    # O comprimento é MEDIDO sobre o traçado por `salvar_rota`. Editável aqui,
    # ele passaria a afirmar um metro que o desenho não tem — e é esse número
    # que decide se o lance cabe nos 90 m da norma.
    readonly_fields = ("comprimento_m",)

    def get_queryset(self, request):
        from django.db.models import Count
        return (
            super().get_queryset(request)
            .select_related("localidade")
            .annotate(n_enlaces=Count("enlaces", distinct=True))
        )

    @admin.display(description="Comprimento", ordering="comprimento_m")
    def col_metros(self, obj):
        m = float(obj.comprimento_m or 0)
        return f"{m / 1000:.2f} km" if m >= 1000 else f"{m:.0f} m"

    @admin.display(description="Enlaces", ordering="-n_enlaces")
    def col_enlaces(self, obj):
        return obj.n_enlaces

    @admin.display(description="Pontos")
    def col_pontos(self, obj):
        return obj.total_pontos
