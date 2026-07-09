from django.db import models, transaction
from django.contrib.auth.models import User
import datetime
import uuid
from django.core.validators import MinValueValidator
from decimal import Decimal
from django.utils import timezone
from datetime import timedelta
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError
from dateutil.relativedelta import relativedelta

# ========== CHOICES ==========
class SimNaoChoices(models.TextChoices):
    SIM = 'sim', 'Sim'
    NAO = 'nao', 'Não'

class StatusItemChoices(models.TextChoices):
    ATIVO = 'ativo', 'Ativo'
    BACKUP = 'backup', 'Backup'
    MANUTENCAO = 'manutencao', 'Manutenção'
    DEFEITO = 'defeito', 'Defeito'
    PAUSADO = 'pausado', 'Pausado'
    DESCARTE = 'descarte', 'Descarte'

class StatusUsuarioChoices(models.TextChoices):
    ATIVO = 'ativo', 'Ativo'
    DESLIGADO = 'desligado', 'Desligado'

class TipoMovimentacaoChoices(models.TextChoices):
    TRANSFERENCIA = "transferencia", "Transferência de dispositivo"
    TRANSFERENCIA_EQUIPAMENTO = "transferencia_equipamento", "Transferência Equipamento"
    BAIXA = "baixa", "Baixa"
    ENTRADA = "entrada", "Entrada"
    ENVIO_MANUTENCAO = "envio_manutencao", "Envio para Manutenção"
    RETORNO_MANUTENCAO = "retorno_manutencao", "Retorno de Manutenção"
    OUTROS = "outros", "Outros"

class TipoTransferenciaChoices(models.TextChoices):
    ENTREGA = "entrega", "Entrega"
    DEVOLUCAO = "devolucao", "Devolução"

class StatusOrdemManutencaoChoices(models.TextChoices):
    # Fluxo conduzido pelo fornecedor no Portal (ver OrdemManutencaoService):
    #   aguardando → recebido → em_avaliacao
    #     → aguardando_aprovacao → (TI) aprovado → em_reparo → reparado → devolvido → (TI) concluido
    #                            → (TI) reprovado → devolvido (avaliação técnica) → (TI) concluido
    #     → sem_reparo → substituto_enviado → (TI) concluido
    #     → sem_condicoes (motivo + valor) → (forn.) devolvido_descarte → (TI) descartado
    #     → descarte_local_solicitado (motivo + valor) → (TI) descarte_local_aprovado → (fornecedor) descartado
    #                                                  → (TI) sem_condicoes (recusa: devolver p/ o TI descartar)
    #
    # Troca antecipada (troca_antecipada=True; fornecedor abre pelo Portal p/ evitar
    # equipamento defeituoso parado): troca_ant_sub_enviado (substituto a caminho)
    #   → (TI) troca_ant_sub_recebido (recebe o substituto, ativa em estoque)
    #   → (TI) troca_ant_def_enviado (envia o defeituoso → MANUTENCAO)
    #   → (forn.) troca_ant_def_recebido (recebe o defeituoso → PAUSADO)
    #   → (forn.) concluido (envia a proposta de reparo)
    AGUARDANDO_RECEBIMENTO = "aguardando_recebimento", "Aguardando recebimento"
    RECEBIDO = "recebido", "Recebido pelo fornecedor"
    EM_AVALIACAO = "em_avaliacao", "Em avaliação"
    AGUARDANDO_APROVACAO = "aguardando_aprovacao", "Aguardando aprovação do TI"
    APROVADO = "aprovado", "Reparo aprovado"
    REPROVADO = "reprovado", "Reparo reprovado"
    EM_REPARO = "em_reparo", "Em reparo"
    REPARADO = "reparado", "Reparo concluído"
    DEVOLVIDO = "devolvido", "Devolvido ao cliente — aguardando recebimento"
    SEM_REPARO = "sem_reparo", "Sem reparo — troca"
    SUBSTITUTO_ENVIADO = "substituto_enviado", "Substituto enviado — aguardando recebimento"
    SEM_CONDICOES = "sem_condicoes", "Sem condições de reparo — aguardando devolução"
    DEVOLVIDO_DESCARTE = "devolvido_descarte", "Devolvido para descarte — aguardando recebimento do TI"
    DESCARTE_LOCAL_SOLICITADO = "descarte_local_solicitado", "Descarte local solicitado — aguardando aprovação do TI"
    DESCARTE_LOCAL_APROVADO = "descarte_local_aprovado", "Descarte local aprovado — aguardando o fornecedor"
    DESCARTADO = "descartado", "Descartado"
    # Troca antecipada de equipamento (substituto chega antes de enviar o defeituoso)
    TROCA_ANT_SUBSTITUTO_ENVIADO = "troca_ant_sub_enviado", "Troca antecipada — substituto a caminho"
    TROCA_ANT_SUBSTITUTO_RECEBIDO = "troca_ant_sub_recebido", "Troca antecipada — substituto recebido, enviar o defeituoso"
    TROCA_ANT_DEFEITUOSO_ENVIADO = "troca_ant_def_enviado", "Troca antecipada — defeituoso enviado ao fornecedor"
    TROCA_ANT_DEFEITUOSO_RECEBIDO = "troca_ant_def_recebido", "Troca antecipada — defeituoso recebido, aguardando proposta"
    CONCLUIDO = "concluido", "Concluído"
    CANCELADO = "cancelado", "Cancelado"

class LocalidadeChoices(models.TextChoices):
    KARITEL = "Karitel", "Karitel"
    RIO_DO_MEIO = "Rio_do_Meio", "Rio do Meio"
    Mambai = "Mambai", "Mambai"
    Sao_Paulo =  "Sao_Paulo", "Sao Paulo"
    
class TipoRespostaChoices(models.TextChoices):
    TEXTO    = 'texto', 'Texto'
    NUMERO   = 'numero', 'Número'
    BOOLEANO = 'booleano', 'Sim/Não'
    ESCOLHA  = 'escolha', 'Escolha única'
    
# ========== BASE ABSTRATA ==========
class AuditModel(models.Model):
    criado_por = models.ForeignKey(User, related_name="%(class)s_criador", on_delete=models.SET_NULL, null=True, blank=True)
    atualizado_por = models.ForeignKey(User, related_name="%(class)s_atualizador", on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

# ========== ENTIDADES BASE ==========

class Categoria(AuditModel):
    nome = models.CharField(max_length=100)

    def __str__(self):
        return self.nome


class Subtipo(AuditModel):
    nome = models.CharField(max_length=100)
    alocado = models.CharField(max_length=3, choices=SimNaoChoices.choices)
    categoria = models.ForeignKey(Categoria, on_delete=models.CASCADE)

    def __str__(self):
        return f"{self.nome} ({self.categoria.nome})"


class Localidade(AuditModel):
    # NOVO: código controlado por choices (permite migração sem quebrar)
    codigo = models.CharField(
        max_length=20,
        choices=LocalidadeChoices.choices,
        null=True, blank=True,
        help_text="Sede: "
    )
    local = models.CharField(max_length=100)

    def __str__(self):
        return self.local


# Nome do grupo Django que identifica usuários do Portal do Fornecedor.
# Fonte única — importado pelo middleware, views, services e data migration.
GRUPO_FORNECEDOR = "Fornecedor"


class Fornecedor(AuditModel):
    nome = models.CharField(max_length=100)
    cnpj = models.CharField(max_length=18, unique=True)
    contrato = models.TextField(blank=True, null=True)

    def __str__(self):
        return f"{self.nome} ({self.cnpj})"


class PerfilFornecedor(AuditModel):
    """
    Liga um usuário Django (login) a um Fornecedor, habilitando o acesso ao
    Portal do Fornecedor (área isolada). O sandbox de acesso é garantido por
    três camadas: grupo "Fornecedor" + FornecedorAccessMiddleware + queryset
    sempre filtrado por este vínculo.
    """
    usuario = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="perfil_fornecedor",
        verbose_name="Usuário de acesso",
    )
    fornecedor = models.ForeignKey(
        Fornecedor,
        on_delete=models.CASCADE,
        related_name="perfis",
        verbose_name="Fornecedor",
    )
    ativo = models.BooleanField(
        default=True,
        help_text="Desmarque para suspender o acesso sem excluir o usuário.",
    )
    notificar_defeito_email = models.BooleanField(
        default=True,
        verbose_name="Notificar por e-mail (equipamento em Defeito)",
        help_text="Quando ativo, este login recebe um e-mail sempre que um equipamento do fornecedor for marcado como Defeito.",
    )

    class Meta:
        verbose_name = "Perfil de Fornecedor"
        verbose_name_plural = "Perfis de Fornecedor"

    def __str__(self):
        return f"{self.usuario.username} → {self.fornecedor.nome}"


# ✅ Centro de Custo agora indica se é PMB
class CentroCusto(AuditModel):
    numero = models.CharField(max_length=10)
    departamento = models.CharField(max_length=100)
    pmb = models.CharField(
        max_length=3,
        choices=SimNaoChoices.choices,
        default=SimNaoChoices.NAO,
        help_text="Centro de custo pertence ao PMB?"
    )

    def __str__(self):
        return f"{self.numero} - {self.departamento} [pmb? {dict(SimNaoChoices.choices).get(self.pmb)}]"


class Funcao(AuditModel):
    nome = models.CharField(max_length=100)

    def __str__(self):
        return self.nome






# ========== ITEM (Equipamento) ==========
class Item(AuditModel):
    nome = models.CharField(max_length=100)
    numero_serie = models.CharField(max_length=100, blank=True, null=True)
    marca = models.CharField(max_length=100, blank=True, null=True)
    modelo = models.CharField(max_length=100, blank=True, null=True)

    centro_custo = models.ForeignKey(
        CentroCusto,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    quantidade = models.PositiveIntegerField(default=1)

    item_consumo = models.CharField(
        max_length=3,
        choices=SimNaoChoices.choices,
        default=SimNaoChoices.NAO
    )

    pmb = models.CharField(
        max_length=3,
        choices=SimNaoChoices.choices,
        default=SimNaoChoices.NAO
    )

    tem_lote = models.BooleanField(
        default=False,
        verbose_name="Controlar por lote?"
    )

    valor = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        blank=True,
        null=True
    )

    status = models.CharField(
        max_length=15,
        choices=StatusItemChoices.choices,
        default=StatusItemChoices.ATIVO
    )

    fornecedor = models.ForeignKey(
        Fornecedor,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    categoria = models.ForeignKey(
        Categoria,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    subtipo = models.ForeignKey(
        Subtipo,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    localidade = models.ForeignKey(
        Localidade,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    precisa_preventiva = models.CharField(
        max_length=3,
        choices=SimNaoChoices.choices,
        default=SimNaoChoices.NAO
    )

    data_limite_preventiva = models.IntegerField(
        help_text="Dias para nova preventiva",
        blank=True,
        null=True
    )

    data_compra = models.DateField(blank=True, null=True)
    numero_pedido = models.CharField(max_length=100, blank=True, null=True)
    observacoes = models.TextField(blank=True, null=True)

    locado = models.CharField(
        max_length=3,
        choices=SimNaoChoices.choices,
        default=SimNaoChoices.NAO
    )

    compartilhado = models.BooleanField(
        default=False,
        verbose_name="Pode ser compartilhado?",
        help_text=(
            "Se marcado, o equipamento pode ficar vinculado a vários colaboradores "
            "ao mesmo tempo (cada um com seu termo de responsabilidade). "
            "Se não, segue o fluxo padrão de detentor único."
        ),
    )

    class Meta:
        verbose_name = "Item / Equipamento"
        verbose_name_plural = "Itens / Equipamentos"
        indexes = [
            models.Index(fields=["nome"]),
            models.Index(fields=["status"]),
            models.Index(fields=["item_consumo"]),
            models.Index(fields=["localidade"]),
            models.Index(fields=["centro_custo"]),
        ]

    @property
    def eh_consumo(self):
        return self.item_consumo == SimNaoChoices.SIM

    @property
    def eh_locado(self):
        return self.locado == SimNaoChoices.SIM

    @property
    def eh_compartilhado(self):
        return bool(self.compartilhado)

    def clean(self):
        super().clean()

        errors = {}

        if self.item_consumo == SimNaoChoices.SIM and self.locado == SimNaoChoices.SIM:
            errors["item_consumo"] = "Item de consumo não pode ser cadastrado como locado."

        if self.item_consumo == SimNaoChoices.SIM and self.compartilhado:
            errors["compartilhado"] = "Item de consumo não pode ser marcado como compartilhado."

        if self.precisa_preventiva == SimNaoChoices.SIM and not self.data_limite_preventiva:
            errors["data_limite_preventiva"] = "Informe a periodicidade da preventiva."

        if self.precisa_preventiva == SimNaoChoices.NAO:
            self.data_limite_preventiva = None

        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"{self.nome} - {self.numero_serie or 's/ nº'}"

class Locacao(AuditModel):
    equipamento = models.OneToOneField(Item, on_delete=models.CASCADE, related_name="locacao")
    tempo_locado = models.IntegerField(blank=True, null=True, help_text="Informe a quantidade de meses")
    valor_mensal = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        blank=True,
        null=True,
        validators=[MinValueValidator(0)],
        help_text="Valor do pagamento mensal (R$)"
    )
    data_entrada = models.DateField(blank=True, null=True, help_text="Data de entrada do equipamento locado")
    contrato = models.CharField(max_length=200, blank=True, null=True)
    observacoes = models.TextField(blank=True, null=True)
    fornecedor = models.ForeignKey(Fornecedor, on_delete=models.SET_NULL, blank=True, null=True)

    @property
    def data_vencimento(self):
        """Data em que o contrato de locação encerra."""
        if self.data_entrada and self.tempo_locado:
            return self.data_entrada + relativedelta(months=self.tempo_locado)
        return None

    @property
    def contrato_vencido(self):
        venc = self.data_vencimento
        return venc is not None and datetime.date.today() > venc

    @property
    def dias_pos_contrato(self):
        """
        Dias além do prazo contratado. Retorna None quando:
        - não há vencimento calculável
        - o contrato ainda não venceu
        - o item está pausado (contagem encerrada)
        """
        if not self.contrato_vencido:
            return None
        status = getattr(self.equipamento, "status", None)
        if status == StatusItemChoices.PAUSADO:
            return None
        return (datetime.date.today() - self.data_vencimento).days

    @property
    def meses_e_dias_pos_contrato(self):
        """Tupla (meses, dias_restantes) para exibição amigável."""
        dias = self.dias_pos_contrato
        if dias is None:
            return None
        delta = relativedelta(datetime.date.today(), self.data_vencimento)
        return delta.months + delta.years * 12, delta.days

    def __str__(self):
        return f"Locação: {self.equipamento.nome} - {self.tempo_locado or 0} meses"


class LocacaoPeriodo(AuditModel):
    """
    Período de cobrança de aluguel de um item locado. Abre quando o item fica
    Ativo/Backup (contagem começa do 0) e fecha quando vai para Pausado/Defeito
    (congela). Os períodos fechados viram histórico permanente; ao reativar,
    um novo período começa do zero, preservando o histórico.
    """
    item = models.ForeignKey(
        Item, on_delete=models.CASCADE, related_name="locacao_periodos"
    )
    valor_mensal = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    data_inicio = models.DateField()
    data_fim = models.DateField(null=True, blank=True, help_text="Vazio = período em andamento")
    motivo_fim = models.CharField(
        max_length=20, blank=True, null=True,
        help_text="Status que encerrou o período (pausado/defeito).",
    )

    class Meta:
        ordering = ["-data_inicio", "-id"]
        verbose_name = "Período de Locação"
        verbose_name_plural = "Períodos de Locação"
        indexes = [models.Index(fields=["item", "data_fim"])]

    @property
    def em_andamento(self) -> bool:
        return self.data_fim is None

    @property
    def _fim_efetivo(self):
        return self.data_fim or datetime.date.today()

    @property
    def meses(self) -> int:
        """Meses cheios decorridos no período."""
        delta = relativedelta(self._fim_efetivo, self.data_inicio)
        return delta.years * 12 + delta.months

    @property
    def dias(self) -> int:
        return (self._fim_efetivo - self.data_inicio).days

    @property
    def valor_acumulado(self):
        return (self.valor_mensal or Decimal("0.00")) * self.meses

    def __str__(self):
        fim = self.data_fim.isoformat() if self.data_fim else "em andamento"
        return f"Locação {self.item.nome}: {self.data_inicio} → {fim}"


# ========== PLANTA DE LOCALIDADE ==========
class PlantaProjeto(AuditModel):
    nome         = models.CharField(max_length=200, verbose_name="Nome da Planta")
    descricao    = models.TextField(blank=True, verbose_name="Descrição")
    localidade   = models.ForeignKey(
        Localidade,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="plantas",
        verbose_name="Localidade"
    )
    layout       = models.JSONField(
        default=dict,
        verbose_name="Layout JSON",
        help_text="Estrutura visual da planta: elementos, conexões e posições"
    )
    layout_version = models.PositiveIntegerField(default=1, verbose_name="Versão do Layout")
    imagem_fundo = models.ImageField(
        upload_to="plantas/fundos/",
        null=True, blank=True,
        verbose_name="Imagem de Fundo",
        help_text="Planta baixa ou croqui (PNG/JPG, máx. 10 MB)"
    )
    visualizadores_tv = models.ManyToManyField(
        User,
        blank=True,
        related_name='plantas_tv_autorizadas',
        verbose_name="Visualizadores TV",
        help_text="Usuários do grupo 'Visualizador TV' autorizados a ver esta planta no modo TV."
    )

    class Meta:
        verbose_name = "Planta de Localidade"
        verbose_name_plural = "Plantas de Localidades"
        ordering = ["localidade__local", "nome"]
        indexes = [models.Index(fields=["localidade"])]

    def __str__(self):
        return f"{self.nome} — {self.localidade or 'Sem localidade'}"

    _TIPOS_FORMA = frozenset({'quadro', 'circulo', 'linha', 'texto'})

    @property
    def total_elementos(self):
        return sum(1 for e in self.layout.get("elements", []) if e.get("type") not in self._TIPOS_FORMA)

    @property
    def elementos_com_prtg(self):
        return sum(1 for e in self.layout.get("elements", []) if e.get("type") not in self._TIPOS_FORMA and e.get("prtg_objid"))


class PlantaLayoutHistorico(models.Model):
    planta    = models.ForeignKey(
        PlantaProjeto, on_delete=models.CASCADE, related_name='historico_versoes'
    )
    versao    = models.PositiveIntegerField()
    layout    = models.JSONField()
    salvo_por = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='layouts_salvos'
    )
    descricao = models.CharField(max_length=200, blank=True, verbose_name="Descrição")
    salvo_em  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-versao']
        unique_together = [['planta', 'versao']]
        verbose_name = "Versão de Layout"
        verbose_name_plural = "Versões de Layout"

    def __str__(self):
        return f"{self.planta.nome} — v{self.versao}"


class LoteEstoque(AuditModel):
    fornecedor = models.ForeignKey(
        "Fornecedor",
        on_delete=models.PROTECT,
        related_name="lotes_estoque",
        verbose_name="Fornecedor"
    )

    data_entrada = models.DateField(
        verbose_name="Data de Entrada"
    )

    numero_nf = models.CharField(
        max_length=60,
        verbose_name="Número da NF"
    )

    quantidade = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        verbose_name="Quantidade do Lote"
    )

    custo_unitario = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        verbose_name="Custo Unitário"
    )

    observacao_tecnica = models.TextField(
        blank=True,
        null=True,
        verbose_name="Observação Técnica"
    )

    class Meta:
        verbose_name = "Lote de Estoque"
        verbose_name_plural = "Lotes de Estoque"
        ordering = ["-data_entrada", "-created_at"]
        indexes = [
            models.Index(fields=["numero_nf"]),
            models.Index(fields=["data_entrada"]),
            models.Index(fields=["fornecedor"]),
        ]

    @property
    def valor_total_calculado(self):
        quantidade = Decimal(self.quantidade or 0)
        custo_unitario = self.custo_unitario or Decimal("0.00")
        return quantidade * custo_unitario

    def clean(self):
        super().clean()

        errors = {}

        if not self.fornecedor_id:
            errors["fornecedor"] = "Fornecedor é obrigatório para o lote."

        if not self.data_entrada:
            errors["data_entrada"] = "Data de entrada é obrigatória para o lote."

        if not self.numero_nf:
            errors["numero_nf"] = "Número da NF é obrigatório para o lote."

        if not self.quantidade or self.quantidade <= 0:
            errors["quantidade"] = "A quantidade do lote deve ser maior que zero."

        if not self.custo_unitario or self.custo_unitario <= 0:
            errors["custo_unitario"] = "O custo unitário deve ser maior que zero."

        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"Lote NF {self.numero_nf} - {self.fornecedor}"
    
class ItemLote(AuditModel):
    item = models.ForeignKey(
        "Item",
        on_delete=models.PROTECT,
        related_name="vinculos_lote",
        verbose_name="Item / Equipamento"
    )

    lote = models.ForeignKey(
        "LoteEstoque",
        on_delete=models.PROTECT,
        related_name="itens_vinculados",
        verbose_name="Lote"
    )

    quantidade_entrada = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
        verbose_name="Quantidade de Entrada"
    )

    quantidade_disponivel = models.PositiveIntegerField(
        validators=[MinValueValidator(0)],
        verbose_name="Quantidade Disponível"
    )

    custo_unitario = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        verbose_name="Custo Unitário"
    )

    class Meta:
        verbose_name = "Vínculo Item x Lote"
        verbose_name_plural = "Vínculos Item x Lote"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["item"]),
            models.Index(fields=["lote"]),
        ]

    @property
    def valor_total_calculado(self):
        quantidade = Decimal(self.quantidade_entrada or 0)
        custo_unitario = self.custo_unitario or Decimal("0.00")
        return quantidade * custo_unitario

    def clean(self):
        super().clean()

        errors = {}

        if not self.item_id:
            errors["item"] = "Item é obrigatório no vínculo de lote."

        if not self.lote_id:
            errors["lote"] = "Lote é obrigatório."

        if not self.quantidade_entrada or self.quantidade_entrada <= 0:
            errors["quantidade_entrada"] = "Quantidade de entrada deve ser maior que zero."

        if self.quantidade_disponivel is None:
            errors["quantidade_disponivel"] = "Quantidade disponível é obrigatória."

        elif self.quantidade_disponivel > self.quantidade_entrada:
            errors["quantidade_disponivel"] = "Quantidade disponível não pode ser maior que a entrada."

        if not self.custo_unitario or self.custo_unitario <= 0:
            errors["custo_unitario"] = "Custo unitário deve ser maior que zero."

        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"{self.item} | {self.lote} | Qtd: {self.quantidade_entrada}"
class Usuario(AuditModel):
    matricula = models.CharField(
        max_length=30,
        unique=True,
        blank=True,
        null=True,
        verbose_name="Matrícula"
    )

    nome = models.CharField(max_length=100)
    status = models.CharField(max_length=10, choices=StatusUsuarioChoices.choices)
    data_inicio = models.DateField(default=datetime.date.today)
    data_termino = models.DateField(blank=True, null=True)
    pmb = models.CharField(max_length=3, choices=SimNaoChoices.choices)
    email = models.EmailField(blank=True, null=True)

    centro_custo = models.ForeignKey(
        CentroCusto,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    localidade = models.ForeignKey(
        Localidade,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    funcao = models.ForeignKey(
        Funcao,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    # ── Hierarquia organizacional (preenchida via importação da planilha RH) ──
    # Ordem real: Diretor Geral → Diretor → Gestor → Coordenador → Supervisor
    diretor_geral = models.CharField(
        max_length=150, blank=True, null=True, verbose_name="Diretor Geral"
    )
    diretor = models.CharField(
        max_length=150, blank=True, null=True, verbose_name="Diretor"
    )
    gestor = models.CharField(
        max_length=150,
        blank=True,
        null=True,
        verbose_name="Gestor"
    )
    coordenador = models.CharField(
        max_length=150,
        blank=True,
        null=True,
        verbose_name="Coordenador"
    )
    supervisor = models.CharField(
        max_length=150,
        blank=True,
        null=True,
        verbose_name="Supervisor"
    )
    responsavel = models.CharField(
        max_length=150,
        blank=True,
        null=True,
        verbose_name="Responsável",
        help_text="Responsável hierárquico consolidado (Junção: Supervisor > Coordenador > Gestor > Diretor > Diretor Geral)"
    )

    class Meta:
        ordering = ["nome"]
        indexes = [
            models.Index(fields=["matricula"]),
            models.Index(fields=["nome"]),
            models.Index(fields=["status"]),
            models.Index(fields=["responsavel"]),
            models.Index(fields=["diretor"]),
            models.Index(fields=["diretor_geral"]),
        ]

    def __str__(self):
        if self.matricula:
            return f"{self.matricula} - {self.nome}"
        return f"{self.nome} ({self.email or 'sem e-mail'})"


# ========== COMENTARIO ==========


class Comentario(AuditModel):
    texto = models.TextField()
    item = models.ForeignKey(Item, on_delete=models.CASCADE, null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Comentário"
        verbose_name_plural = "Comentários"

    def __str__(self):
        return f"Comentário sobre {self.item.nome if self.item else 'Sem item'}"


# ========== MANUTENÇÃO ==========

class CicloManutencao(AuditModel):
    status_inicial = models.CharField(max_length=20)
    data_inicio = models.DateField(default=datetime.date.today)
    data_fim = models.DateField(blank=True, null=True)
    causa = models.TextField()
    custo = models.DecimalField(max_digits=10, decimal_places=2)
    item = models.ForeignKey(Item, on_delete=models.CASCADE)

    class Meta:
        ordering = ["-data_inicio"]
        verbose_name = "Ciclo de Manutenção"
        verbose_name_plural = "Ciclos de Manutenção"

    def __str__(self):
        return f"Ciclo {self.item.nome} - {self.status_inicial}"


class MovimentacaoItem(AuditModel):
    tipo_movimentacao = models.CharField(
        max_length=30,
        choices=TipoMovimentacaoChoices.choices,
        verbose_name="Tipo de Movimentação"
    )

    tipo_transferencia = models.CharField(
        max_length=10,
        blank=True,
        null=True,
        choices=TipoTransferenciaChoices.choices,
        verbose_name="Tipo de Transferência"
    )

    item = models.ForeignKey(
        "Item",
        on_delete=models.CASCADE,
        related_name="movimentacoes",
        verbose_name="Item"
    )

    lote = models.ForeignKey(
        "LoteEstoque",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="movimentacoes",
        verbose_name="Lote vinculado"
    )

    usuario = models.ForeignKey(
        "Usuario",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="movimentacoes",
        verbose_name="Usuário / Solicitante"
    )

    quantidade = models.PositiveIntegerField(
        default=1,
        verbose_name="Quantidade"
    )

    localidade_origem = models.ForeignKey(
        "Localidade",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="movs_origem",
        verbose_name="Localidade de Origem"
    )

    localidade_destino = models.ForeignKey(
        "Localidade",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="movs_destino",
        verbose_name="Localidade de Destino"
    )

    centro_custo_origem = models.ForeignKey(
        "CentroCusto",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="movs_origem_cc",
        verbose_name="Centro de Custo Origem"
    )

    centro_custo_destino = models.ForeignKey(
        "CentroCusto",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="movs_destino_cc",
        verbose_name="Centro de Custo Destino"
    )

    fornecedor_manutencao = models.ForeignKey(
        "Fornecedor",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="manutencoes",
        verbose_name="Fornecedor Manutenção"
    )

    status_retorno = models.CharField(
        max_length=15,
        choices=StatusItemChoices.choices,
        blank=True,
        null=True,
        verbose_name="Status Retorno"
    )

    status_transferencia = models.CharField(
        max_length=15,
        choices=StatusItemChoices.choices,
        blank=True,
        null=True,
        verbose_name="Novo Status"
    )

    numero_pedido = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        verbose_name="Nº Pedido/NF"
    )

    observacao = models.TextField(
        blank=True,
        null=True,
        verbose_name="Observações"
    )

    chamado = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        verbose_name="Nº Chamado"
    )

    custo = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name="Custo da Operação"
    )

    termo_pdf = models.FileField(
        upload_to="termos/",
        blank=True,
        null=True,
        verbose_name="Termo de Responsabilidade"
    )

    class Meta:
        verbose_name = "Movimentação de Item"
        verbose_name_plural = "Movimentações de Itens"
        ordering = ["-created_at"]

    def clean(self):
        super().clean()

        errors = {}

        if not self.tipo_movimentacao:
            errors["tipo_movimentacao"] = "Informe o tipo de movimentação."

        if not self.item_id:
            errors["item"] = "Selecione o item."

        if not self.quantidade or self.quantidade <= 0:
            errors["quantidade"] = "Informe uma quantidade maior que zero."

        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"[{self.get_tipo_movimentacao_display()}] {self.item} x{self.quantidade}"


class ItemColaborador(AuditModel):
    """
    Vínculo ATIVO entre um equipamento COMPARTILHADO e um colaborador.

    Itens não-compartilhados continuam derivando o detentor da última
    movimentação (não usam esta tabela). Para itens compartilhados, cada
    entrega abre um vínculo e cada devolução o encerra (ativo=False),
    permitindo que o mesmo equipamento fique com vários colaboradores ao
    mesmo tempo, com rastreabilidade por termo (FK para a movimentação).
    """
    item = models.ForeignKey(
        "Item",
        on_delete=models.CASCADE,
        related_name="vinculos_colaborador",
        verbose_name="Equipamento",
    )
    colaborador = models.ForeignKey(
        "Usuario",
        on_delete=models.CASCADE,
        related_name="itens_compartilhados",
        verbose_name="Colaborador",
    )
    ativo = models.BooleanField(default=True, verbose_name="Vínculo ativo")
    data_vinculo = models.DateTimeField(default=timezone.now, verbose_name="Vinculado em")
    data_devolucao = models.DateTimeField(blank=True, null=True, verbose_name="Devolvido em")

    movimentacao_entrega = models.ForeignKey(
        "MovimentacaoItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="vinculos_abertos",
        verbose_name="Movimentação de entrega",
    )
    movimentacao_devolucao = models.ForeignKey(
        "MovimentacaoItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="vinculos_encerrados",
        verbose_name="Movimentação de devolução",
    )
    observacao = models.TextField(blank=True, null=True, verbose_name="Observações")

    class Meta:
        verbose_name = "Vínculo de Equipamento Compartilhado"
        verbose_name_plural = "Vínculos de Equipamentos Compartilhados"
        ordering = ["-data_vinculo"]
        indexes = [
            models.Index(fields=["item", "ativo"]),
            models.Index(fields=["colaborador", "ativo"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["item", "colaborador"],
                condition=models.Q(ativo=True),
                name="uniq_vinculo_ativo_item_colaborador",
            ),
        ]

    def __str__(self):
        estado = "ativo" if self.ativo else "encerrado"
        return f"{self.item} ↔ {self.colaborador} ({estado})"


# ----------------- ORDEM DE MANUTENÇÃO (Portal do Fornecedor) -----------------
class OrdemManutencao(AuditModel):
    """
    Ordem de serviço de manutenção externa conduzida pelo fornecedor.
    Criada automaticamente quando um item é enviado para manutenção com um
    `fornecedor_manutencao` definido. O fornecedor avança o status pelo Portal;
    o TI conclui (confirma o retorno do item reparado ou recebe o substituto).
    Toda transição é gravada em OrdemManutencaoEvento (auditoria / timeline).
    """
    item = models.ForeignKey(
        "Item",
        on_delete=models.CASCADE,
        related_name="ordens_manutencao",
        verbose_name="Equipamento",
    )
    fornecedor = models.ForeignKey(
        "Fornecedor",
        on_delete=models.PROTECT,
        related_name="ordens_manutencao",
        verbose_name="Fornecedor responsável",
    )
    movimentacao_origem = models.ForeignKey(
        "MovimentacaoItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ordens_manutencao",
        verbose_name="Movimentação de envio",
    )
    status = models.CharField(
        max_length=30,
        choices=StatusOrdemManutencaoChoices.choices,
        default=StatusOrdemManutencaoChoices.AGUARDANDO_RECEBIMENTO,
    )
    diagnostico = models.TextField(
        blank=True,
        null=True,
        verbose_name="Diagnóstico do fornecedor",
    )
    item_substituto = models.ForeignKey(
        "Item",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ordens_substituicao",
        verbose_name="Equipamento substituto",
    )
    # Dados do contrato de substituição (informados pelo fornecedor na troca)
    substituto_contrato = models.CharField(
        max_length=200, blank=True, null=True, verbose_name="Contrato de substituição"
    )
    substituto_valor = models.DecimalField(
        max_digits=12, decimal_places=2, blank=True, null=True, verbose_name="Valor da substituição"
    )
    substituto_data = models.DateField(
        blank=True, null=True, verbose_name="Data da substituição"
    )
    substituto_locado = models.CharField(
        max_length=3, choices=SimNaoChoices.choices, default=SimNaoChoices.NAO,
        verbose_name="Substituto entra como locação?",
    )
    # Tempo do contrato de locação do substituto (meses). Alimenta Locacao.tempo_locado,
    # de onde os dashboards derivam vencimento do contrato. Só faz sentido quando a
    # troca entra como locação (substituto_locado = "sim").
    substituto_tempo_meses = models.PositiveIntegerField(
        blank=True, null=True, verbose_name="Tempo de contrato (meses)",
    )
    # Nota: "sem condições de reparo" (descarte) reaproveita os campos existentes —
    # o motivo vai em `diagnostico` e o valor em `valor_avaliacao_tecnica` (mesmo
    # campo do fluxo de reprovação, que também é um desfecho sem reparo).
    devolucao_localidade = models.ForeignKey(
        "Localidade", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="ordens_devolucao",
        verbose_name="Localidade de devolução",
    )
    reparo_valor = models.DecimalField(
        max_digits=12, decimal_places=2, blank=True, null=True,
        verbose_name="Valor do reparo realizado",
    )
    # ── Fluxo de aprovação de reparo ───────────────────────────────────────
    # O fornecedor envia o ORÇAMENTO (valor do conserto) antes de reparar; o TI
    # aprova ou reprova. Aprovado → conserta e informa valor do conserto + valor
    # total (pode incluir película/capa/etc., por isso ≥ conserto). Reprovado →
    # informa valor de avaliação técnica e devolve ao cliente.
    valor_orcamento = models.DecimalField(
        max_digits=12, decimal_places=2, blank=True, null=True,
        verbose_name="Orçamento do reparo (proposto)",
    )
    valor_conserto = models.DecimalField(
        max_digits=12, decimal_places=2, blank=True, null=True,
        verbose_name="Valor do conserto",
    )
    valor_total = models.DecimalField(
        max_digits=12, decimal_places=2, blank=True, null=True,
        verbose_name="Valor total (conserto + extras)",
    )
    valor_avaliacao_tecnica = models.DecimalField(
        max_digits=12, decimal_places=2, blank=True, null=True,
        verbose_name="Valor da avaliação técnica",
    )
    aprovado_por = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="ordens_manutencao_decididas",
        verbose_name="Decisão do TI por",
    )
    decisao_em = models.DateTimeField(null=True, blank=True, verbose_name="Data da decisão do TI")
    # ── Garantia do reparo / troca ─────────────────────────────────────────
    # O fornecedor informa, ao concluir o reparo ou enviar o substituto, se o
    # serviço tem garantia e por quantos dias. A CONTAGEM só começa quando o TI
    # confirma o recebimento (garantia_inicio é gravado em _on_concluido). Ao
    # expirar (garantia_fim < hoje), o item deixa de estar na garantia do reparo.
    tem_garantia = models.CharField(
        max_length=3, choices=SimNaoChoices.choices, default=SimNaoChoices.NAO,
        verbose_name="Reparo/troca com garantia?",
    )
    garantia_dias = models.PositiveIntegerField(
        null=True, blank=True, verbose_name="Prazo de garantia (dias)",
    )
    garantia_inicio = models.DateField(
        null=True, blank=True, verbose_name="Início da garantia (recebimento do TI)",
    )
    garantia_fim = models.DateField(
        null=True, blank=True, verbose_name="Fim da garantia",
    )
    chamado = models.CharField(max_length=100, blank=True, null=True, verbose_name="Nº Chamado")
    # Troca antecipada: o fornecedor manda o substituto ANTES de o defeituoso ser
    # enviado, para não deixar o equipamento com defeito parado. Muda o fluxo e o
    # stepper (ver ETAPAS_MACRO / etapa_macro e o OrdemManutencaoService).
    troca_antecipada = models.BooleanField(default=False, verbose_name="Troca antecipada")
    finalizada_em = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Ordem de Manutenção"
        verbose_name_plural = "Ordens de Manutenção"
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["fornecedor"]),
        ]

    @property
    def aberta(self) -> bool:
        return self.status not in (
            StatusOrdemManutencaoChoices.CONCLUIDO,
            StatusOrdemManutencaoChoices.CANCELADO,
            StatusOrdemManutencaoChoices.DESCARTADO,
        )

    # Macro-etapas para o stepper visual (detalhe TI e Portal). São 5 rótulos; o
    # fluxo de troca antecipada usa um conjunto próprio (mesma quantidade de etapas).
    ETAPAS_MACRO_NORMAL = ["Recebimento", "Avaliação", "Aprovação", "Reparo / Troca", "Concluído"]
    ETAPAS_MACRO_ANTECIPADA = ["Substituto a caminho", "Substituto recebido", "Defeituoso enviado", "Fornecedor recebeu", "Concluído"]

    @property
    def ETAPAS_MACRO(self):
        return self.ETAPAS_MACRO_ANTECIPADA if self.troca_antecipada else self.ETAPAS_MACRO_NORMAL

    @property
    def etapa_macro(self) -> int:
        """Índice (0-5) da macro-etapa atual no fluxo, para o stepper visual.
        Agrupa os status nas 5 etapas de `ETAPAS_MACRO`. Não consulta o banco."""
        if self.troca_antecipada:
            return {
                "troca_ant_sub_enviado": 0,
                "troca_ant_sub_recebido": 1,
                "troca_ant_def_enviado": 2,
                "troca_ant_def_recebido": 3,
                "concluido": 5, "cancelado": 4,
            }.get(self.status, 0)
        return {
            "aguardando_recebimento": 0,
            "recebido": 1, "em_avaliacao": 1,
            "aguardando_aprovacao": 2, "aprovado": 2, "reprovado": 2,
            "em_reparo": 3, "reparado": 3, "sem_reparo": 3,
            "substituto_enviado": 3, "devolvido": 3, "sem_condicoes": 3,
            "devolvido_descarte": 3,
            "descarte_local_solicitado": 3, "descarte_local_aprovado": 3,
            "concluido": 5, "descartado": 5, "cancelado": 4,
        }.get(self.status, 0)

    @property
    def cancelada(self) -> bool:
        return self.status == StatusOrdemManutencaoChoices.CANCELADO

    # ── Garantia do reparo / troca ─────────────────────────────────────────
    #: Status em que a garantia já foi (ou pode ter sido) definida pelo fornecedor.
    _GARANTIA_STATUS_RELEVANTES = (
        StatusOrdemManutencaoChoices.REPARADO,
        StatusOrdemManutencaoChoices.DEVOLVIDO,
        StatusOrdemManutencaoChoices.SUBSTITUTO_ENVIADO,
        StatusOrdemManutencaoChoices.CONCLUIDO,
    )

    @property
    def tem_garantia_reparo(self) -> bool:
        return self.tem_garantia == SimNaoChoices.SIM

    @property
    def garantia_relevante(self) -> bool:
        """A OS já passou por reparo/troca — faz sentido exibir a garantia."""
        return self.status in self._GARANTIA_STATUS_RELEVANTES

    @property
    def garantia_iniciada(self) -> bool:
        return self.garantia_inicio is not None

    @property
    def garantia_dias_restantes(self):
        """Dias até o fim da garantia (negativo = expirada). None se não iniciada."""
        if not self.garantia_fim:
            return None
        return (self.garantia_fim - timezone.localdate()).days

    @property
    def garantia_vigente(self) -> bool:
        d = self.garantia_dias_restantes
        return d is not None and d >= 0

    @property
    def garantia_expirada(self) -> bool:
        d = self.garantia_dias_restantes
        return d is not None and d < 0

    @property
    def garantia_status(self) -> str:
        """'sem_garantia' | 'aguardando_inicio' | 'vigente' | 'expirada'."""
        if not self.tem_garantia_reparo:
            return "sem_garantia"
        if not self.garantia_iniciada:
            return "aguardando_inicio"
        return "expirada" if self.garantia_expirada else "vigente"

    @property
    def garantia_status_display(self) -> str:
        return {
            "sem_garantia": "Sem garantia de reparo",
            "aguardando_inicio": "Garantia inicia na confirmação do TI",
            "vigente": "Em garantia",
            "expirada": "Garantia expirada",
        }[self.garantia_status]

    def __str__(self):
        return f"OS #{self.pk} — {self.item} ({self.get_status_display()})"


class OrdemManutencaoEvento(AuditModel):
    """Linha do tempo de uma OrdemManutencao: cada transição vira um evento."""
    ordem = models.ForeignKey(
        OrdemManutencao,
        on_delete=models.CASCADE,
        related_name="eventos",
    )
    status = models.CharField(
        max_length=30,
        choices=StatusOrdemManutencaoChoices.choices,
        verbose_name="Status registrado",
    )
    observacao = models.TextField(blank=True, null=True)

    class Meta:
        ordering = ["created_at", "id"]
        verbose_name = "Evento de Manutenção"
        verbose_name_plural = "Eventos de Manutenção"

    def __str__(self):
        return f"OS #{self.ordem_id} → {self.get_status_display()}"


class OrdemManutencaoAnexo(AuditModel):
    """
    Nota fiscal (ou documento) anexada a uma Ordem de Manutenção. Tanto o
    fornecedor quanto o TI podem anexar quantas quiserem.
    """
    class OrigemAnexo(models.TextChoices):
        FORNECEDOR = "fornecedor", "Fornecedor"
        TI = "ti", "TI"

    ordem = models.ForeignKey(
        OrdemManutencao,
        on_delete=models.CASCADE,
        related_name="anexos",
    )
    arquivo = models.FileField(upload_to="manutencao/nf/%Y/%m/", verbose_name="Arquivo")
    origem = models.CharField(
        max_length=12, choices=OrigemAnexo.choices, default=OrigemAnexo.TI,
        verbose_name="Origem",
    )
    descricao = models.CharField(max_length=200, blank=True, default="", verbose_name="Descrição")

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "Anexo de Manutenção"
        verbose_name_plural = "Anexos de Manutenção"

    def __str__(self):
        return f"NF OS #{self.ordem_id} ({self.get_origem_display()})"

    @property
    def nome_arquivo(self):
        import os
        return os.path.basename(self.arquivo.name) if self.arquivo else ""


# ----------------- CHECKLIST -----------------
class CheckListModelo(AuditModel):
    nome = models.CharField(max_length=120)
    ativo = models.CharField(max_length=3, choices=SimNaoChoices.choices, default=SimNaoChoices.SIM)
    subtipo = models.ForeignKey(Subtipo, on_delete=models.SET_NULL, null=True, blank=True,
                                help_text="Opcional: restringe este checklist a um subtipo de item.")
    intervalo_dias = models.PositiveIntegerField(default=0, help_text="Periodicidade padrão (dias). 0 = sem programação.")

    class Meta:
        ordering = ["nome"]
        verbose_name = "Modelo de Checklist"
        verbose_name_plural = "Modelos de Checklist"

    def __str__(self):
        return self.nome

class CheckListPergunta(AuditModel):
    checklist_modelo = models.ForeignKey(CheckListModelo, on_delete=models.CASCADE, related_name="perguntas")
    texto_pergunta = models.CharField(max_length=255)
    tipo_resposta = models.CharField(max_length=12, choices=TipoRespostaChoices.choices, default=TipoRespostaChoices.TEXTO)
    obrigatorio = models.CharField(max_length=3, choices=SimNaoChoices.choices, default=SimNaoChoices.SIM)
    # Para tipo ESCOLHA: opções separadas por vírgula
    opcoes = models.CharField(max_length=400, blank=True, null=True, help_text="Para escolha única: separe opções por vírgula.")

    ordem = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["ordem", "id"]
        verbose_name = "Pergunta de Checklist"
        verbose_name_plural = "Perguntas de Checklist"

    def __str__(self):
        return f"[{self.checklist_modelo}] {self.texto_pergunta}"


# ----------------- PREVENTIVA -----------------
class Preventiva(AuditModel):
    equipamento = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="preventivas")
    checklist_modelo = models.ForeignKey(CheckListModelo, on_delete=models.SET_NULL, null=True, blank=True)

    # Técnico responsável pela execução desta preventiva agendada.
    tecnico = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="preventivas_atribuidas",
        verbose_name="Técnico responsável",
        help_text="Usuário (equipe de TI) responsável por executar esta atividade.",
    )

    data_ultima = models.DateField(blank=True, null=True)
    data_proxima = models.DateField(blank=True, null=True)
    data_agendamento = models.DateField(blank=True, null=True, verbose_name="Data agendada",
                                        help_text="Data explícita para a próxima execução. Sobrepõe o cálculo automático. Limpa após execução.")
    dentro_do_prazo = models.BooleanField(default=True)

    # Controle de pausa — ativado quando o equipamento sai de "ativo"
    pausada = models.BooleanField(default=False, verbose_name="Preventiva pausada")
    data_pausada = models.DateField(blank=True, null=True, verbose_name="Data de início da pausa")
    dias_restantes_pausa = models.IntegerField(blank=True, null=True, verbose_name="Dias restantes congelados na pausa")

    observacao = models.TextField(blank=True, null=True)
    # Mantemos como "última evidência" para compatibilidade
    foto_antes  = models.ImageField(upload_to="preventivas/%Y/%m/", blank=True, null=True)
    foto_depois = models.ImageField(upload_to="preventivas/%Y/%m/", blank=True, null=True)
    # Evidências adicionais (opcionais) — segundo par antes/depois
    foto_antes_2  = models.ImageField(upload_to="preventivas/%Y/%m/", blank=True, null=True)
    foto_depois_2 = models.ImageField(upload_to="preventivas/%Y/%m/", blank=True, null=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Preventiva"
        verbose_name_plural = "Preventivas"

    def __str__(self):
        return f"Preventiva de {self.equipamento.nome} ({self.checklist_modelo or 'sem checklist'})"

    def _periodo_referencia(self) -> int:
        """
        Intervalo oficial da programação (dias).
        Prioriza o intervalo específico do ATIVO (data_limite_preventiva), pois é
        específico do equipamento; se ausente, usa o do modelo de checklist.
        (Mesma ordem de prioridade usada nas listas — _intervalo_preventiva.)
        """
        try:
            item_intervalo = int(self.equipamento.data_limite_preventiva or 0)
        except Exception:
            item_intervalo = 0
        if item_intervalo > 0:
            return item_intervalo
        if self.checklist_modelo and self.checklist_modelo.intervalo_dias:
            return int(self.checklist_modelo.intervalo_dias)
        return 0

    def sincronizar_data_proxima(self, hoje=None, salvar=True):
        """
        Recalcula e PERSISTE `data_proxima` como a DATA EFETIVA da próxima execução,
        para que dashboards, alertas e e-mails (que consultam o campo) reflitam o
        status real:
          - agendamento explícito (data_agendamento) tem prioridade;
          - senão, data_ultima + intervalo;
          - senão, mantém data_proxima existente ou hoje (nunca executada).
        Não altera preventivas pausadas (contagem congelada).
        Retorna a data_proxima resultante.
        """
        if self.pausada:
            return self.data_proxima
        hoje = hoje or timezone.now().date()
        dias = self._periodo_referencia()
        if self.data_agendamento:
            nova = self.data_agendamento
        elif self.data_ultima and dias > 0:
            nova = self.data_ultima + timedelta(days=dias)
        elif self.data_proxima:
            nova = self.data_proxima
        else:
            nova = hoje
        if nova != self.data_proxima or self.dentro_do_prazo != (hoje <= nova):
            self.data_proxima = nova
            self.dentro_do_prazo = hoje <= nova
            if salvar:
                self.save(update_fields=["data_proxima", "dentro_do_prazo", "updated_at"])
        return nova

    def recomputar_prazo(self, data_exec=None):
        """Recalcula data_proxima e dentro_do_prazo. Não age enquanto pausada."""
        if self.pausada:
            return
        base = data_exec or self.data_ultima or timezone.now().date()
        dias = self._periodo_referencia()
        self.data_proxima = (base + timedelta(days=dias)) if dias > 0 else None
        if self.data_proxima:
            self.dentro_do_prazo = timezone.now().date() <= self.data_proxima
        else:
            self.dentro_do_prazo = True

    def pausar(self):
        """
        Congela a contagem registrando quantos dias restavam até a próxima execução.
        Chamado quando o equipamento sai do status 'ativo'.
        """
        if self.pausada:
            return
        hoje = timezone.now().date()
        self.pausada = True
        self.data_pausada = hoje
        if self.data_proxima:
            self.dias_restantes_pausa = max((self.data_proxima - hoje).days, 0)
        else:
            self.dias_restantes_pausa = None
        # Item fora de operação não é considerado atrasado
        self.dentro_do_prazo = True
        self.save(update_fields=[
            "pausada", "data_pausada", "dias_restantes_pausa",
            "dentro_do_prazo", "updated_at",
        ])

    def retomar(self):
        """
        Retoma a contagem a partir de hoje com os dias restantes que foram congelados.
        Chamado quando o equipamento volta ao status 'ativo'.
        """
        if not self.pausada:
            return
        hoje = timezone.now().date()
        self.pausada = False
        if self.dias_restantes_pausa is not None:
            self.data_proxima = hoje + timedelta(days=self.dias_restantes_pausa)
            self.dentro_do_prazo = hoje <= self.data_proxima
        else:
            # Sem data congelada — recalcula normalmente a partir de hoje
            self.recomputar_prazo(hoje)
        self.data_pausada = None
        self.dias_restantes_pausa = None
        self.save(update_fields=[
            "pausada", "data_pausada", "dias_restantes_pausa",
            "data_proxima", "dentro_do_prazo", "updated_at",
        ])

    @transaction.atomic
    def registrar_execucao(self, respostas_dict: dict, usuario=None, observacao=None, foto_antes=None, foto_depois=None, foto_antes_2=None, foto_depois_2=None, data_execucao=None, hora_inicio=None, hora_fim=None):
        """
        Registra a execução sem sobrescrever históricos anteriores.
        respostas_dict: { pergunta_id: valor_string }
        """
        hoje = data_execucao or timezone.now().date()

        # Snapshot de desempenho (antes de limpar data_agendamento)
        data_agendada_snap = self.data_agendamento
        no_prazo_snap = (data_agendada_snap is None) or (hoje <= data_agendada_snap)

        # 1) cria a execução (histórico)
        execucao = PreventivaExecucao.objects.create(
            preventiva=self,
            data_execucao=hoje,
            observacao=(observacao or ""),
            foto_antes=foto_antes,
            foto_depois=foto_depois,
            foto_antes_2=foto_antes_2,
            foto_depois_2=foto_depois_2,
            tecnico=(self.tecnico or usuario),
            data_agendada=data_agendada_snap,
            no_prazo=no_prazo_snap,
            hora_inicio=hora_inicio,
            hora_fim=hora_fim,
            criado_por=usuario,
            atualizado_por=usuario,
        )

        # 2) cria respostas (ligadas à preventiva e à execução)
        perguntas = (self.checklist_modelo.perguntas.all() if self.checklist_modelo else [])
        bulk = []
        for p in perguntas:
            valor = (respostas_dict.get(str(p.id)) or "").strip()
            if p.obrigatorio == SimNaoChoices.SIM and not valor:
                raise ValueError(f"Pergunta obrigatória sem resposta: {p.texto_pergunta}")
            bulk.append(PreventivaResposta(
                preventiva=self,
                execucao=execucao,
                pergunta=p,
                resposta=valor,
                criado_por=usuario,
                atualizado_por=usuario,
            ))
        if bulk:
            PreventivaResposta.objects.bulk_create(bulk)

        # 3) atualiza os campos de "última execução" para agenda/relatórios
        self.data_ultima = hoje
        self.data_agendamento = None  # agendamento consumido pela execução
        if observacao:
            self.observacao = observacao
        if foto_antes:
            self.foto_antes = foto_antes
        if foto_depois:
            self.foto_depois = foto_depois
        if foto_antes_2:
            self.foto_antes_2 = foto_antes_2
        if foto_depois_2:
            self.foto_depois_2 = foto_depois_2

        self.recomputar_prazo(hoje)
        self.save(update_fields=["data_ultima", "data_agendamento", "data_proxima", "dentro_do_prazo", "observacao", "foto_antes", "foto_depois", "foto_antes_2", "foto_depois_2", "updated_at"])


# --- NOVO: execuções de preventiva, com fotos por execução ---
class PreventivaExecucao(AuditModel):
    """
    Histórico de execuções de uma Preventiva.
    Mantém as evidências e a data da execução, evitando sobrescrita.
    """
    preventiva = models.ForeignKey(Preventiva, on_delete=models.CASCADE, related_name="execucoes")
    data_execucao = models.DateField(default=timezone.now)
    observacao = models.TextField(blank=True, null=True)
    foto_antes  = models.ImageField(upload_to="preventivas/%Y/%m/", blank=True, null=True)
    foto_depois = models.ImageField(upload_to="preventivas/%Y/%m/", blank=True, null=True)
    # Evidências adicionais (opcionais) — segundo par antes/depois
    foto_antes_2  = models.ImageField(upload_to="preventivas/%Y/%m/", blank=True, null=True)
    foto_depois_2 = models.ImageField(upload_to="preventivas/%Y/%m/", blank=True, null=True)

    # Snapshot de desempenho: técnico responsável e data agendada no momento da
    # execução (data_agendamento é limpo após executar). Permite medir pontualidade.
    tecnico = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="execucoes_preventiva",
        verbose_name="Técnico",
    )
    data_agendada = models.DateField(
        null=True, blank=True,
        verbose_name="Data agendada (no momento da execução)",
    )
    no_prazo = models.BooleanField(
        default=True,
        verbose_name="Executada no prazo",
        help_text="True quando a execução ocorreu até a data agendada (ou sem agendamento).",
    )

    # Apontamento de horas trabalhadas pelo técnico nesta execução.
    hora_inicio = models.TimeField(
        null=True, blank=True,
        verbose_name="Hora de início",
        help_text="Horário em que o técnico iniciou o serviço.",
    )
    hora_fim = models.TimeField(
        null=True, blank=True,
        verbose_name="Hora de término",
        help_text="Horário em que o técnico concluiu o serviço.",
    )
    duracao_minutos = models.PositiveIntegerField(
        null=True, blank=True,
        verbose_name="Duração (minutos)",
        help_text="Tempo gasto na execução, em minutos. Calculado a partir de hora_inicio e hora_fim.",
    )

    class Meta:
        ordering = ["-data_execucao", "-created_at"]
        verbose_name = "Execução de Preventiva"
        verbose_name_plural = "Execuções de Preventiva"

    def __str__(self):
        return f"Execução {self.data_execucao:%d/%m/%Y} - {self.preventiva.equipamento.nome}"

    @staticmethod
    def calcular_duracao_minutos(hora_inicio, hora_fim):
        """
        Duração em minutos entre dois TimeField. Se a hora final for menor que a
        inicial, assume que o serviço cruzou a meia-noite (+24h). Retorna None se
        faltar uma das pontas.
        """
        if not hora_inicio or not hora_fim:
            return None
        ini = hora_inicio.hour * 60 + hora_inicio.minute
        fim = hora_fim.hour * 60 + hora_fim.minute
        delta = fim - ini
        if delta < 0:
            delta += 24 * 60  # cruzou a meia-noite
        return delta

    @property
    def duracao_horas(self):
        """Duração em horas decimais (ex.: 1.5) ou None."""
        minutos = self.duracao_minutos
        if minutos is None:
            minutos = self.calcular_duracao_minutos(self.hora_inicio, self.hora_fim)
        if minutos is None:
            return None
        return round(minutos / 60, 2)

    @property
    def duracao_formatada(self):
        """Duração legível, ex.: '1h 30min', '2h', '45min' ou None."""
        minutos = self.duracao_minutos
        if minutos is None:
            minutos = self.calcular_duracao_minutos(self.hora_inicio, self.hora_fim)
        if minutos is None:
            return None
        horas, mins = divmod(int(minutos), 60)
        if horas and mins:
            return f"{horas}h {mins}min"
        if horas:
            return f"{horas}h"
        return f"{mins}min"

    def save(self, *args, **kwargs):
        # Mantém duracao_minutos sempre coerente com as horas informadas.
        if self.hora_inicio and self.hora_fim:
            self.duracao_minutos = self.calcular_duracao_minutos(self.hora_inicio, self.hora_fim)
        super().save(*args, **kwargs)


# ACRESCENTA o vínculo da resposta à execução
class PreventivaResposta(AuditModel):
    preventiva = models.ForeignKey(Preventiva, on_delete=models.CASCADE, related_name="respostas")
    execucao = models.ForeignKey(PreventivaExecucao, on_delete=models.CASCADE, related_name="respostas")
    pergunta = models.ForeignKey(CheckListPergunta, on_delete=models.CASCADE)
    resposta = models.TextField(blank=True, null=True)

    class Meta:
        verbose_name = "Resposta de Preventiva"
        verbose_name_plural = "Respostas de Preventiva"

    def __str__(self):
        return f"{self.pergunta.texto_pergunta}: {self.resposta}"

class PeriodicidadeChoices(models.TextChoices):
    MENSAL = "mensal", _("Mensal")
    SEMESTRAL = "semestral", _("Semestral")
    ANUAL = "anual", _("Anual")
    TRI = "trienal", _("Trienal")
    CONTRATO = "contrato", _("Contrato/Outro")

class TipoMovLicencaChoices(models.TextChoices):
    ATRIBUICAO = 'atribuicao', _('Atribuição (Saída)')
    DEVOLUCAO = 'devolucao', _('Devolução (Entrada)')  # Mudamos de REMOCAO para DEVOLUCAO para alinhar com seu erro

# --- MODELO LICENÇA ---
class Licenca(AuditModel):
    # Campos Essenciais (Cadastro Simplificado)
    nome = models.CharField(max_length=160, verbose_name="Nome da Licença")
    fornecedor = models.ForeignKey("Fornecedor", on_delete=models.SET_NULL, null=True, blank=True)
    pmb = models.CharField(max_length=3, choices=SimNaoChoices.choices, default=SimNaoChoices.NAO, verbose_name="PMB?")
    observacao = models.TextField(blank=True, null=True)

    # [CORREÇÃO DO ERRO] Este campo é necessário para a devolução funcionar!
    # Ele define o "Centro de Custo Dono" da licença (ex: TI). 
    centro_custo = models.ForeignKey(
        "CentroCusto", 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        help_text="Centro de Custo proprietário (ex: TI) para onde o ativo volta na devolução."
    )


    class Meta:
        ordering = ["nome"]
        verbose_name = "Licença"
        verbose_name_plural = "Licenças"

    def __str__(self):
        return self.nome

# --- MODELO LOTE ---
class LicencaLote(AuditModel):
    licenca = models.ForeignKey(Licenca, on_delete=models.CASCADE, related_name="lotes")
    quantidade_total = models.PositiveIntegerField(verbose_name="Qtd. Comprada")
    quantidade_disponivel = models.PositiveIntegerField(verbose_name="Saldo Disponível", default=0)
    custo_ciclo = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    periodicidade = models.CharField(max_length=20, choices=PeriodicidadeChoices.choices, default=PeriodicidadeChoices.ANUAL)
    data_compra = models.DateField(null=True, blank=True)
    numero_pedido = models.CharField(max_length=50, null=True, blank=True)
    fornecedor = models.ForeignKey("Fornecedor", on_delete=models.SET_NULL, null=True, blank=True)
    centro_custo = models.ForeignKey("CentroCusto", on_delete=models.SET_NULL, null=True, blank=True)
    observacao = models.TextField(blank=True, null=True)

    class Meta:
        ordering = ["-data_compra", "-id"]
        verbose_name = "Lote de Licença"

    def save(self, *args, **kwargs):
        if self._state.adding and self.quantidade_disponivel is None:
            self.quantidade_disponivel = self.quantidade_total
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Lote #{self.pk} - {self.licenca}"

# --- MODELO MOVIMENTAÇÃO ---
class MovimentacaoLicenca(AuditModel):
    tipo = models.CharField(max_length=20, choices=TipoMovLicencaChoices.choices)
    licenca = models.ForeignKey(Licenca, on_delete=models.CASCADE, related_name="movimentacoes")
    usuario = models.ForeignKey("Usuario", on_delete=models.SET_NULL, null=True, blank=True)
    lote = models.ForeignKey(LicencaLote, on_delete=models.SET_NULL, null=True, blank=True)
    
    # Destino do Custo (Usuário ou Estoque)
    centro_custo_destino = models.ForeignKey("CentroCusto", on_delete=models.SET_NULL, null=True, blank=True)
    valor_unitario = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    observacao = models.TextField(blank=True, null=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Movimentação de Licença"

    def __str__(self):
        return f"{self.get_tipo_display()} - {self.licenca}"


# ========== HISTÓRICO DE STATUS DO ITEM ==========

class ItemStatusHistorico(models.Model):
    """Registra cada mudança de status de um equipamento para monitoração de tempo."""
    item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name='status_historico',
        verbose_name='Equipamento',
    )
    status_anterior = models.CharField(max_length=20, blank=True, default='')
    status_novo     = models.CharField(max_length=20)
    alterado_em     = models.DateTimeField(auto_now_add=True)
    alterado_por    = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        verbose_name='Alterado por',
    )

    class Meta:
        ordering = ['-alterado_em']
        verbose_name = 'Histórico de Status do Item'
        verbose_name_plural = 'Históricos de Status de Itens'
        indexes = [models.Index(fields=['item', '-alterado_em'])]

    def __str__(self):
        return f"{self.item.nome}: {self.status_anterior or '(novo)'} → {self.status_novo}"


# ========== HISTÓRICO DE CONECTIVIDADE PRTG DO ITEM ==========

class ItemPRTGHistorico(models.Model):
    """Histórico de status PRTG (conectividade de rede) de devices monitorados.

    Registra cada MUDANÇA de status reportada pelo PRTG e cobre TODOS os devices
    monitorados (não apenas os vinculados a um Item via planta): por isso a chave
    de identificação é o `prtg_objid`, e o vínculo com um Item do estoque (`item`)
    é OPCIONAL — pode ser nulo para devices de rede que ainda não estão cadastrados
    como equipamento no estoque.

    O carimbo `registrado_em` reflete, sempre que possível, o MOMENTO REAL da
    transição reportado pelo PRTG (uptimesince/downtimesince), e não apenas o
    instante em que o coletor rodou — por isso usa `default=timezone.now` (e não
    `auto_now_add`), permitindo gravar o tempo real do evento.
    """
    item = models.ForeignKey(
        Item,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='prtg_historico',
        verbose_name='Equipamento',
    )
    prtg_objid      = models.IntegerField(db_index=True, verbose_name='PRTG ObjID')
    device_nome     = models.CharField(max_length=255, blank=True, default='', verbose_name='Device (PRTG)')
    device_host     = models.CharField(max_length=255, blank=True, default='', verbose_name='Host / IP')
    device_grupo    = models.CharField(max_length=255, blank=True, default='', verbose_name='Grupo (PRTG)')
    status_anterior = models.CharField(max_length=20, blank=True, default='')
    status_novo     = models.CharField(max_length=20)
    registrado_em   = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ['-registrado_em']
        verbose_name = 'Histórico PRTG'
        verbose_name_plural = 'Históricos PRTG'
        indexes = [
            models.Index(fields=['item', '-registrado_em']),
            models.Index(fields=['prtg_objid', '-registrado_em']),
        ]

    def __str__(self):
        alvo = self.item.nome if self.item else (self.device_nome or f'objid {self.prtg_objid}')
        return f"{alvo}: {self.status_anterior or '(novo)'} → {self.status_novo} (PRTG)"


# ========== NINJAONE RMM ==========

class NinjaDevice(AuditModel):
    """Dispositivos NinjaOne importados via planilha CSV (exportada do NinjaOne).
    Vincula ao Item do estoque pelo número de série (BIOS) ou pelo nome do dispositivo."""

    ninja_id = models.IntegerField(unique=True, null=True, blank=True, verbose_name="ID NinjaOne", db_index=True)
    display_name = models.CharField(max_length=255, verbose_name="Nome do dispositivo")
    hostname = models.CharField(max_length=255, blank=True, verbose_name="Hostname / DNS")
    serial_number = models.CharField(max_length=100, blank=True, db_index=True, verbose_name="Número de série")
    item = models.OneToOneField(
        "Item",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="ninja_device",
        verbose_name="Item vinculado (estoque)",
    )
    os_name = models.CharField(max_length=255, blank=True, verbose_name="Sistema Operacional")
    manufacturer = models.CharField(max_length=255, blank=True, verbose_name="Fabricante")
    model_name = models.CharField(max_length=255, blank=True, verbose_name="Modelo")
    processor = models.CharField(max_length=255, blank=True, verbose_name="Processador")
    total_memory_mb = models.IntegerField(null=True, blank=True, verbose_name="Memória RAM (MB)")
    ip_address = models.CharField(max_length=50, blank=True, verbose_name="Endereço IP")
    last_contact = models.DateTimeField(null=True, blank=True, verbose_name="Último contato")
    is_online = models.BooleanField(default=False, verbose_name="Online agora")
    last_user = models.CharField(max_length=255, blank=True, verbose_name="Último usuário logado")
    organization_name = models.CharField(max_length=255, blank=True, verbose_name="Organização")
    local = models.CharField(max_length=120, blank=True, db_index=True, verbose_name="Local / Site")
    node_class = models.CharField(max_length=60, blank=True, verbose_name="Tipo de dispositivo")
    last_sync = models.DateTimeField(auto_now=True, verbose_name="Última importação")

    class Meta:
        verbose_name = "Dispositivo NinjaOne"
        verbose_name_plural = "Dispositivos NinjaOne"
        ordering = ["display_name"]
        indexes = [
            models.Index(fields=["is_online"]),
            models.Index(fields=["serial_number"]),
        ]

    def __str__(self):
        return self.display_name or f"Device #{self.ninja_id}"

    @property
    def memory_gb(self):
        if self.total_memory_mb:
            return round(self.total_memory_mb / 1024, 1)
        return None

    @property
    def node_class_label(self):
        labels = {
            "WINDOWS_WORKSTATION": "Workstation Windows",
            "WINDOWS_SERVER": "Servidor Windows",
            "MAC": "Mac",
            "LINUX_WORKSTATION": "Workstation Linux",
            "LINUX_SERVER": "Servidor Linux",
            "NMS_SWITCH": "Switch",
            "NMS_ROUTER": "Roteador",
            "NMS_PRINTER": "Impressora",
            "NMS_FIREWALL": "Firewall",
            "ANDROID": "Android",
            "APPLE_IOS": "iOS",
        }
        return labels.get(self.node_class, self.node_class or "—")


class NinjaDeviceSnapshot(models.Model):
    """
    Snapshot do estado de cada dispositivo NinjaOne, gravado a cada importação de CSV.
    Base de dados para o relatório de uso/tempo de atividade por máquina.
    Gerado automaticamente a cada importação (UI ou comando importar_ninja_csv).
    """

    device = models.ForeignKey(
        NinjaDevice,
        on_delete=models.CASCADE,
        related_name="snapshots",
        verbose_name="Dispositivo",
    )
    timestamp = models.DateTimeField(db_index=True, verbose_name="Horário")
    is_online = models.BooleanField(default=False, verbose_name="Online")
    current_user = models.CharField(max_length=255, blank=True, verbose_name="Usuário logado")
    ip_address = models.CharField(max_length=50, blank=True, verbose_name="IP")

    class Meta:
        verbose_name = "Snapshot NinjaOne"
        verbose_name_plural = "Snapshots NinjaOne"
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["device", "-timestamp"]),
            models.Index(fields=["timestamp"]),
            models.Index(fields=["device", "is_online"]),
        ]

    def __str__(self):
        estado = "online" if self.is_online else "offline"
        return f"{self.device} — {self.timestamp:%d/%m %H:%M} ({estado})"


class NinjaLoginRegistro(models.Model):
    """
    Histórico de validação de login de um dispositivo NinjaOne.

    Compara o último usuário ativo no dispositivo (NinjaDevice.last_user) com o
    colaborador atribuído ao item no sistema (última transferência de 'entrega'
    não devolvida). Um novo registro é gravado sempre que o status/usuário muda,
    formando o histórico exibido na tela de "Registro de Login".
    """

    STATUS_CONFERE = "confere"
    STATUS_DIVERGENTE = "divergente"
    STATUS_SEM_ATRIBUICAO = "sem_atribuicao"
    STATUS_SEM_LOGIN = "sem_login"
    STATUS_CHOICES = [
        (STATUS_CONFERE, "Confere"),
        (STATUS_DIVERGENTE, "Divergente"),
        (STATUS_SEM_ATRIBUICAO, "Sem atribuição no sistema"),
        (STATUS_SEM_LOGIN, "Sem login no dispositivo"),
    ]

    device = models.ForeignKey(
        NinjaDevice,
        on_delete=models.CASCADE,
        related_name="login_registros",
        verbose_name="Dispositivo",
    )
    verificado_em = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="Verificado em")
    device_user = models.CharField(max_length=255, blank=True, verbose_name="Login no dispositivo")
    usuario_sistema = models.ForeignKey(
        "Usuario",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="ninja_login_registros",
        verbose_name="Colaborador atribuído",
    )
    usuario_sistema_nome = models.CharField(max_length=150, blank=True, verbose_name="Colaborador atribuído (snapshot)")
    usuario_detectado = models.CharField(max_length=150, blank=True, verbose_name="Colaborador detectado pelo login")
    item_nome = models.CharField(max_length=200, blank=True, verbose_name="Item")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, db_index=True, verbose_name="Status")
    detalhe = models.CharField(max_length=400, blank=True, verbose_name="Detalhe")

    class Meta:
        verbose_name = "Registro de login NinjaOne"
        verbose_name_plural = "Registros de login NinjaOne"
        ordering = ["-verificado_em", "-id"]
        indexes = [
            models.Index(fields=["device", "-verificado_em"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self):
        return f"{self.device} — {self.get_status_display()} ({self.verificado_em:%d/%m/%Y %H:%M})"


# ========== CONFIGURAÇÃO DO SISTEMA ==========

class ConfiguracaoSistema(models.Model):
    """Singleton de configuração global. Sempre usar ConfiguracaoSistema.get()."""
    alertas_email_ativos = models.BooleanField(
        default=True,
        verbose_name="Alertas de e-mail ativos",
        help_text="Quando desativado, nenhum e-mail de alerta é enviado pelo sistema (útil em ambiente de testes).",
    )
    updated_at = models.DateTimeField(auto_now=True)
    atualizado_por = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Atualizado por",
    )

    class Meta:
        verbose_name = "Configuração do Sistema"
        verbose_name_plural = "Configurações do Sistema"

    def __str__(self):
        estado = "ATIVO" if self.alertas_email_ativos else "DESATIVADO"
        return f"Configuração do Sistema — Alertas: {estado}"

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class CanalNotificacao(models.Model):
    """Registro central de cada TIPO de e-mail de alerta/notificação do sistema
    (um por função em services/email_alertas.py). Permite ao TI, sem mexer em
    código, ativar/desativar cada notificação individualmente e — quando o
    destinatário é uma lista fixa — redirecioná-la. O catálogo (nome/descrição/
    categoria/origem) é definido em código (`email_alertas.CATALOGO_NOTIFICACOES`)
    e sincronizado nesta tabela a cada acesso ao painel; só os campos de ESTADO
    (`ativo`, `destinatarios_customizados`, contadores) persistem por instalação.
    Novas notificações adicionadas ao catálogo aparecem aqui automaticamente."""

    class TipoDestinatarios(models.TextChoices):
        FIXO = "fixo", "Lista fixa (editável aqui)"
        DINAMICO = "dinamico", "Definido em outra tela do sistema"

    codigo = models.SlugField(max_length=60, unique=True)
    nome = models.CharField(max_length=150)
    descricao = models.CharField(max_length=255, blank=True, default="")
    categoria = models.CharField(max_length=60, blank=True, default="")
    icone = models.CharField(max_length=40, blank=True, default="fa-bell")
    tipo_destinatarios = models.CharField(
        max_length=10, choices=TipoDestinatarios.choices, default=TipoDestinatarios.FIXO,
    )
    destino_gerenciado_em = models.CharField(max_length=255, blank=True, default="")
    origem_disparo = models.CharField(max_length=255, blank=True, default="")

    ativo = models.BooleanField(default=True, verbose_name="Notificação ativa")
    destinatarios_customizados_ativo = models.BooleanField(
        default=False,
        verbose_name="Usa lista customizada",
        help_text=(
            "Quando ativo, `destinatarios_customizados` é a lista definitiva (mesmo vazia — "
            "nesse caso ninguém recebe). Quando inativo, usa o padrão do sistema (.env). "
            "Ativado automaticamente ao editar a lista ou remover uma pessoa individualmente."
        ),
    )
    destinatarios_customizados = models.TextField(
        blank=True, default="",
        verbose_name="Destinatários customizados",
        help_text="E-mails separados por vírgula. Vazio = usa o padrão do sistema (.env).",
    )

    ultimo_envio = models.DateTimeField(null=True, blank=True)
    total_envios = models.PositiveIntegerField(default=0)

    updated_at = models.DateTimeField(auto_now=True)
    atualizado_por = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
        verbose_name="Atualizado por",
    )

    class Meta:
        ordering = ["categoria", "nome"]
        verbose_name = "Canal de Notificação"
        verbose_name_plural = "Canais de Notificação"

    def __str__(self):
        return self.nome

    def destinatarios_lista(self) -> list[str]:
        return [e.strip() for e in self.destinatarios_customizados.split(",") if e.strip()]


class Notificacao(models.Model):
    """Notificação exibida no sino do topo. Criada em eventos do sistema — hoje,
    cada movimentação de manutenção entre fornecedor e TI.

    Uma mesma notificação pode ser vista por dois públicos com estados de leitura
    independentes: o time interno/TI (sino de base.html, campo `lida`) e o
    fornecedor dono da OS (sino do Portal, campo `lida_fornecedor`). `fornecedor`
    define de qual fornecedor é a notificação (o Portal só mostra as dele)."""
    titulo = models.CharField(max_length=200)
    mensagem = models.TextField(blank=True)
    url = models.CharField(max_length=300, blank=True)
    portal_url = models.CharField(max_length=300, blank=True, help_text="Link usado no sino do Portal do Fornecedor")
    icone = models.CharField(max_length=40, default="fa-bell")
    categoria = models.CharField(max_length=40, default="geral")
    fornecedor = models.ForeignKey(
        "Fornecedor", on_delete=models.CASCADE, null=True, blank=True,
        related_name="notificacoes",
    )
    lida = models.BooleanField(default=False, verbose_name="Lida (interno/TI)")
    lida_fornecedor = models.BooleanField(default=False, verbose_name="Lida (fornecedor)")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Notificação"
        verbose_name_plural = "Notificações"
        indexes = [
            models.Index(fields=["lida", "-created_at"]),
            models.Index(fields=["fornecedor", "lida_fornecedor", "-created_at"]),
        ]

    def __str__(self):
        return self.titulo


# ── Segurança: trilha de eventos de autenticação (ISO 27001 A.8.15 / A.8.16) ──
class TipoEventoSegurancaChoices(models.TextChoices):
    LOGIN_OK      = "login_ok", "Login bem-sucedido"
    LOGIN_FALHA   = "login_falha", "Falha de login"
    LOGOUT        = "logout", "Logout"
    ACESSO_NEGADO = "acesso_negado", "Acesso negado"


class RegistroSeguranca(models.Model):
    """
    Trilha de eventos de autenticação para monitoramento de segurança
    (ISO 27001 A.8.15 Registro / A.8.16 Monitoramento).

    Gravado automaticamente por signals (services/seguranca_service.py). Não
    estende AuditModel: é gerado pelo sistema e imutável (somente-leitura no
    admin). `suspeito=True` sinaliza anomalia (rajada de falhas ou login logo
    após várias falhas) e dispara alerta por e-mail.
    """
    tipo = models.CharField(max_length=20, choices=TipoEventoSegurancaChoices.choices)
    username = models.CharField(max_length=254, blank=True, help_text="Usuário informado na tentativa")
    usuario = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="eventos_seguranca",
    )
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=400, blank=True)
    caminho = models.CharField(max_length=200, blank=True)
    suspeito = models.BooleanField(default=False)
    detalhe = models.CharField(max_length=200, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-criado_em"]
        verbose_name = "Registro de segurança"
        verbose_name_plural = "Registros de segurança"
        indexes = [
            models.Index(fields=["tipo", "criado_em"]),
            models.Index(fields=["username", "criado_em"]),
            models.Index(fields=["ip", "criado_em"]),
            models.Index(fields=["suspeito", "criado_em"]),
        ]

    def __str__(self):
        return f"{self.get_tipo_display()} · {self.username or '—'} · {self.criado_em:%d/%m/%Y %H:%M}"


# ── Novidades do sistema (changelog: o que foi implementado/atualizado/corrigido) ──
class TipoNovidadeChoices(models.TextChoices):
    NOVO     = "novo", "Novo"
    MELHORIA = "melhoria", "Melhoria"
    CORRECAO = "correcao", "Correção"


class NovidadeSistema(AuditModel):
    """
    Changelog do sistema: novidades de atualização (novo recurso, melhoria ou
    correção). Gerenciado no admin e exibido na tela de perfil. Diferente do
    feed de atividade operacional (SistemaNoticiasService) — aqui são as
    mudanças do próprio sistema.
    """
    versao = models.CharField(max_length=20, blank=True, help_text="Ex.: 4.1.0")
    data = models.DateField(default=timezone.localdate, help_text="Data da atualização")
    tipo = models.CharField(
        max_length=12, choices=TipoNovidadeChoices.choices,
        default=TipoNovidadeChoices.NOVO,
    )
    titulo = models.CharField(max_length=140)
    descricao = models.TextField(blank=True)
    ativo = models.BooleanField(default=True, help_text="Desmarque para ocultar sem excluir")

    class Meta:
        ordering = ["-data", "-id"]
        verbose_name = "Novidade do sistema"
        verbose_name_plural = "Novidades do sistema"
        indexes = [models.Index(fields=["ativo", "-data"])]

    def __str__(self):
        return f"[{self.get_tipo_display()}] {self.titulo}"


# ── Signals: rastrear mudanças de status automaticamente ──────────────────────
from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver


@receiver(pre_save, sender=Item)
def _item_status_pre_save(sender, instance, **kwargs):
    if instance.pk:
        try:
            instance._status_old = Item.objects.only('status').get(pk=instance.pk).status
        except Item.DoesNotExist:
            instance._status_old = None
    else:
        instance._status_old = None


@receiver(post_save, sender=Item)
def _item_status_post_save(sender, instance, created, **kwargs):
    status_old = getattr(instance, '_status_old', None)
    usuario = instance.atualizado_por or instance.criado_por
    if created:
        ItemStatusHistorico.objects.create(
            item=instance,
            status_anterior='',
            status_novo=instance.status,
            alterado_por=usuario,
        )
    elif status_old is not None and status_old != instance.status:
        ItemStatusHistorico.objects.create(
            item=instance,
            status_anterior=status_old,
            status_novo=instance.status,
            alterado_por=usuario,
        )


# ========== MÓDULO QUIOSQUE (integração com app Android) ==========
# Celulares corporativos em modo quiosque enviam telemetria para este sistema via
# API HTTPS (/api/quiosque/...). Os dados vêm do DISPOSITIVO (não de um usuário
# logado), por isso estes models NÃO estendem AuditModel. A identidade do device é
# o device_uuid + token; o vínculo com um Item do estoque é opcional.

class KioskMatricula(models.Model):
    """Código de matrícula de uso único para o enrollment de um device quiosque.

    Gerado pelo TI no dashboard e digitado no app no 1º acesso. Protege o enroll
    para que nenhum aparelho se registre sozinho.
    """
    codigo      = models.CharField(max_length=16, unique=True, db_index=True, verbose_name='Código')
    descricao   = models.CharField(max_length=120, blank=True, default='', verbose_name='Descrição')
    usado       = models.BooleanField(default=False)
    usado_em    = models.DateTimeField(null=True, blank=True)
    device      = models.ForeignKey('KioskDevice', on_delete=models.SET_NULL, null=True, blank=True, related_name='matricula')
    expira_em   = models.DateTimeField(null=True, blank=True, verbose_name='Expira em')
    criado_por  = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='kiosk_matriculas')
    criado_em   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-criado_em']
        verbose_name = 'Matrícula de Quiosque'
        verbose_name_plural = 'Matrículas de Quiosque'

    def __str__(self):
        return f"{self.codigo} ({'usado' if self.usado else 'disponível'})"

    def esta_valida(self) -> bool:
        if self.usado:
            return False
        if self.expira_em and self.expira_em < timezone.now():
            return False
        return True


class KioskDevice(models.Model):
    """Celular corporativo em modo quiosque, integrado ao sistema."""
    device_uuid   = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    token_hash    = models.CharField(max_length=64, db_index=True)  # sha256 do token (nunca o token puro)
    serial        = models.CharField(max_length=120, blank=True, default='', db_index=True, verbose_name='Número de série')
    android_id    = models.CharField(max_length=64, blank=True, default='', db_index=True)
    mac           = models.CharField(max_length=17, null=True, blank=True, verbose_name='MAC Wi-Fi')  # Identidade estável (Device Owner); null em emulador/sem DO
    apelido       = models.CharField(max_length=120, blank=True, default='', verbose_name='Apelido')
    fabricante    = models.CharField(max_length=80, blank=True, default='')
    modelo        = models.CharField(max_length=120, blank=True, default='')
    android_versao = models.CharField(max_length=20, blank=True, default='', verbose_name='Versão Android')
    app_versao    = models.CharField(max_length=20, blank=True, default='', verbose_name='Versão do app')
    ram_mb        = models.IntegerField(null=True, blank=True, verbose_name='RAM (MB)')
    item          = models.ForeignKey('Item', on_delete=models.SET_NULL, null=True, blank=True, related_name='kiosk_devices', verbose_name='Equipamento vinculado')
    ativo         = models.BooleanField(default=True, verbose_name='Ativo')

    # ── Configuração controlada pelo TI (enviada ao device) ──
    wifi_only             = models.BooleanField(default=True, verbose_name='Somente Wi-Fi')
    apps_permitidos       = models.JSONField(default=list, blank=True, verbose_name='Apps permitidos (packages)')
    admin_pin_hash        = models.CharField(max_length=255, blank=True, default='', verbose_name='Hash do PIN do TI')
    intervalo_checkin_seg = models.IntegerField(default=300, verbose_name='Intervalo de check-in (s)')
    mensagem_quiosque     = models.CharField(max_length=200, blank=True, default='', verbose_name='Mensagem do quiosque')
    config_versao         = models.IntegerField(default=1, verbose_name='Versão da configuração')

    # ── Inventário de apps do aparelho (recebido no check-in só quando muda) ──
    # apps_hash = impressão digital da lista (dedup); apps_atualizado_em = quando o
    # inventário foi substituído pela última vez. A lista em si fica em KioskDeviceApp.
    apps_hash          = models.CharField(max_length=64, blank=True, default='', verbose_name='Hash do inventário de apps')
    apps_atualizado_em = models.DateTimeField(null=True, blank=True, verbose_name='Inventário de apps atualizado em')

    # ── Estado mais recente (atualizado a cada check-in) ──
    ultima_latitude   = models.FloatField(null=True, blank=True)
    ultima_longitude  = models.FloatField(null=True, blank=True)
    ultima_precisao_m = models.FloatField(null=True, blank=True)
    ultima_bateria    = models.IntegerField(null=True, blank=True)
    ultima_rede       = models.CharField(max_length=20, blank=True, default='')
    ultimo_checkin    = models.DateTimeField(null=True, blank=True)

    criado_em     = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    # Janela (segundos) sem check-in para considerar o device offline
    OFFLINE_APOS = 900

    class Meta:
        ordering = ['-ultimo_checkin', '-criado_em']
        verbose_name = 'Dispositivo de Quiosque'
        verbose_name_plural = 'Dispositivos de Quiosque'
        indexes = [models.Index(fields=['ativo', '-ultimo_checkin'])]

    def __str__(self):
        return self.apelido or self.modelo or str(self.device_uuid)

    @property
    def online(self) -> bool:
        if not self.ultimo_checkin:
            return False
        return (timezone.now() - self.ultimo_checkin).total_seconds() <= self.OFFLINE_APOS

    @property
    def tem_localizacao(self) -> bool:
        return self.ultima_latitude is not None and self.ultima_longitude is not None


class KioskCheckin(models.Model):
    """Telemetria de um check-in (heartbeat) — base do histórico de atividade."""
    device       = models.ForeignKey(KioskDevice, on_delete=models.CASCADE, related_name='checkins')
    latitude     = models.FloatField(null=True, blank=True)
    longitude    = models.FloatField(null=True, blank=True)
    precisao_m   = models.FloatField(null=True, blank=True)
    bateria      = models.IntegerField(null=True, blank=True)
    carregando   = models.BooleanField(default=False)
    rede         = models.CharField(max_length=20, blank=True, default='')
    ssid         = models.CharField(max_length=64, null=True, blank=True, verbose_name='SSID')  # Rede Wi-Fi no instante do check-in; null fora de Wi-Fi/sem localização
    online       = models.BooleanField(default=True)
    # Instante REAL da coleta no aparelho (ISO 8601 com fuso). Pode estar no passado
    # quando o app entrega uma fila offline em rajada. registrado_em = chegada no servidor.
    coletado_em   = models.DateTimeField(null=True, blank=True, db_index=True, verbose_name='Coletado em')
    registrado_em = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-registrado_em']
        verbose_name = 'Check-in de Quiosque'
        verbose_name_plural = 'Check-ins de Quiosque'
        indexes = [
            models.Index(fields=['device', '-registrado_em']),
            models.Index(fields=['device', '-coletado_em']),
        ]

    def __str__(self):
        return f"{self.device}: {self.registrado_em:%d/%m/%Y %H:%M}"

    @property
    def quando(self):
        """Momento exibido no histórico: a coleta real, ou a chegada se não houver."""
        return self.coletado_em or self.registrado_em


class KioskComando(models.Model):
    """Comando remoto enviado pelo TI ao device (entregue no próximo check-in)."""
    class Tipo(models.TextChoices):
        BLOQUEAR         = 'bloquear', 'Bloquear dispositivo'
        DESBLOQUEAR      = 'desbloquear', 'Desbloquear dispositivo'
        MENSAGEM         = 'mensagem', 'Exibir mensagem'
        ATUALIZAR_CONFIG = 'atualizar_config', 'Atualizar configuração'
        REINICIAR_APP    = 'reiniciar_app', 'Reiniciar aplicativo'

    class Status(models.TextChoices):
        PENDENTE  = 'pendente', 'Pendente'
        ENTREGUE  = 'entregue', 'Entregue'
        EXECUTADO = 'executado', 'Executado'
        FALHOU    = 'falhou', 'Falhou'

    device      = models.ForeignKey(KioskDevice, on_delete=models.CASCADE, related_name='comandos')
    tipo        = models.CharField(max_length=20, choices=Tipo.choices)
    payload     = models.JSONField(default=dict, blank=True)
    status      = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDENTE, db_index=True)
    detalhe     = models.CharField(max_length=255, blank=True, default='')
    criado_por  = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='kiosk_comandos')
    criado_em   = models.DateTimeField(auto_now_add=True)
    entregue_em = models.DateTimeField(null=True, blank=True)
    finalizado_em = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-criado_em']
        verbose_name = 'Comando de Quiosque'
        verbose_name_plural = 'Comandos de Quiosque'
        indexes = [models.Index(fields=['device', 'status'])]

    def __str__(self):
        return f"{self.get_tipo_display()} → {self.device} ({self.status})"


class KioskDeviceApp(models.Model):
    """App abrível instalado no aparelho — inventário enviado no check-in.

    O app envia a lista completa dos apps com ícone de launcher (os únicos que podem
    virar atalho no modo quiosque), e **só quando ela muda**. O servidor substitui o
    conjunto inteiro do device. O TI marca quais ficam liberados → viram
    `KioskDevice.apps_permitidos`. A CHAVE é o `pkg`; o `nome` é só exibição (varia
    com o idioma do aparelho e não deve ser usado em lógica).
    """
    device   = models.ForeignKey(KioskDevice, on_delete=models.CASCADE, related_name='apps')
    pkg      = models.CharField(max_length=255, verbose_name='Pacote')
    nome     = models.CharField(max_length=255, blank=True, default='', verbose_name='Nome')
    sistema  = models.BooleanField(default=False, verbose_name='App de sistema')
    visto_em = models.DateTimeField(auto_now=True, verbose_name='Visto em')

    class Meta:
        # Não-sistema primeiro (os que o TI costuma liberar), depois nome/pacote.
        ordering = ['sistema', 'nome', 'pkg']
        verbose_name = 'App de Dispositivo de Quiosque'
        verbose_name_plural = 'Apps de Dispositivos de Quiosque'
        unique_together = ('device', 'pkg')
        indexes = [models.Index(fields=['device', 'sistema'])]

    def __str__(self):
        return f"{self.nome or self.pkg} ({self.device})"