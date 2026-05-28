from django.db import models, transaction
from django.contrib.auth.models import User
import datetime
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

class StatusUsuarioChoices(models.TextChoices):
    ATIVO = 'ativo', 'Ativo'
    DESLIGADO = 'desligado', 'Desligado'

class TipoMovimentacaoChoices(models.TextChoices):
    TRANSFERENCIA = "transferencia", "Transferência"
    TRANSFERENCIA_EQUIPAMENTO = "transferencia_equipamento", "Transferência Equipamento"
    BAIXA = "baixa", "Baixa"
    ENTRADA = "entrada", "Entrada"
    ENVIO_MANUTENCAO = "envio_manutencao", "Envio para Manutenção"
    RETORNO_MANUTENCAO = "retorno_manutencao", "Retorno de Manutenção"
    OUTROS = "outros", "Outros"

class TipoTransferenciaChoices(models.TextChoices):
    ENTREGA = "entrega", "Entrega"
    DEVOLUCAO = "devolucao", "Devolução"

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


class Fornecedor(AuditModel):
    nome = models.CharField(max_length=100)
    cnpj = models.CharField(max_length=18, unique=True)
    contrato = models.TextField(blank=True, null=True)

    def __str__(self):
        return f"{self.nome} ({self.cnpj})"


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

    def clean(self):
        super().clean()

        errors = {}

        if self.item_consumo == SimNaoChoices.SIM and self.locado == SimNaoChoices.SIM:
            errors["item_consumo"] = "Item de consumo não pode ser cadastrado como locado."

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

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Preventiva"
        verbose_name_plural = "Preventivas"

    def __str__(self):
        return f"Preventiva de {self.equipamento.nome} ({self.checklist_modelo or 'sem checklist'})"

    def _periodo_referencia(self) -> int:
        """
        Usa a periodicidade do modelo; se zero/ausente, tenta usar 'data_limite_preventiva' do Item (dias).
        """
        if self.checklist_modelo and self.checklist_modelo.intervalo_dias:
            return int(self.checklist_modelo.intervalo_dias)
        try:
            return int(self.equipamento.data_limite_preventiva or 0)
        except Exception:
            return 0

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
    def registrar_execucao(self, respostas_dict: dict, usuario=None, observacao=None, foto_antes=None, foto_depois=None):
        """
        Registra a execução sem sobrescrever históricos anteriores.
        respostas_dict: { pergunta_id: valor_string }
        """
        hoje = timezone.now().date()

        # 1) cria a execução (histórico)
        execucao = PreventivaExecucao.objects.create(
            preventiva=self,
            data_execucao=hoje,
            observacao=(observacao or ""),
            foto_antes=foto_antes,
            foto_depois=foto_depois,
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

        self.recomputar_prazo(hoje)
        self.save(update_fields=["data_ultima", "data_agendamento", "data_proxima", "dentro_do_prazo", "observacao", "foto_antes", "foto_depois", "updated_at"])


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

    class Meta:
        ordering = ["-data_execucao", "-created_at"]
        verbose_name = "Execução de Preventiva"
        verbose_name_plural = "Execuções de Preventiva"

    def __str__(self):
        return f"Execução {self.data_execucao:%d/%m/%Y} - {self.preventiva.equipamento.nome}"


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
    """Registra cada mudança de status PRTG (conectividade de rede) de um equipamento.

    Populado automaticamente pela view item_monitoracao toda vez que o painel
    é aberto: consulta o PRTG ao vivo e grava se o status mudou desde o último
    registro. Diferente de ItemStatusHistorico (campo manual Item.status), este
    modelo reflete o estado real de rede reportado pelo PRTG.
    """
    item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name='prtg_historico',
        verbose_name='Equipamento',
    )
    prtg_objid      = models.IntegerField(verbose_name='PRTG ObjID')
    status_anterior = models.CharField(max_length=20, blank=True, default='')
    status_novo     = models.CharField(max_length=20)
    registrado_em   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-registrado_em']
        verbose_name = 'Histórico PRTG do Item'
        verbose_name_plural = 'Históricos PRTG dos Itens'
        indexes = [models.Index(fields=['item', '-registrado_em'])]

    def __str__(self):
        return f"{self.item.nome}: {self.status_anterior or '(novo)'} → {self.status_novo} (PRTG)"


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