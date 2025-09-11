from django.db import models, transaction
from django.contrib.auth.models import User
import datetime
from django.core.validators import MinValueValidator
from django.db.models import Q, Count, F, ExpressionWrapper, IntegerField
from decimal import Decimal
from django.utils import timezone
from datetime import timedelta, date
from django.utils.translation import gettext_lazy as _

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
    criado_por = models.ForeignKey(User, related_name="%(class)s_criador", on_delete=models.SET_NULL, null=True)
    atualizado_por = models.ForeignKey(User, related_name="%(class)s_atualizador", on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

# ========== ENTIDADES BASE ==========
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
    cnpj = models.CharField(max_length=18)
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


# ========== USUÁRIO / LICENÇA ==========



# ========== ITEM (Equipamento) ==========

class Item(AuditModel):
    nome = models.CharField(max_length=100)
    numero_serie = models.CharField(max_length=100, blank=True, null=True)
    marca = models.CharField(max_length=100, blank=True, null=True)
    modelo = models.CharField(max_length=100, blank=True, null=True)
    
    centro_custo = models.ForeignKey(CentroCusto, on_delete=models.SET_NULL, null=True, blank=True)  # <-- novo campo
    quantidade = models.PositiveIntegerField(default=1)
    item_consumo = models.CharField(max_length=3, choices=SimNaoChoices.choices, default=SimNaoChoices.NAO)
    pmb = models.CharField(max_length=3, choices=SimNaoChoices.choices, default=SimNaoChoices.NAO)

    valor = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    status = models.CharField(max_length=15, choices=StatusItemChoices.choices, default=StatusItemChoices.ATIVO)

    fornecedor = models.ForeignKey(Fornecedor, on_delete=models.SET_NULL, null=True, blank=True)
    categoria = models.ForeignKey(Categoria, on_delete=models.SET_NULL, null=True, blank=True)
    subtipo = models.ForeignKey(Subtipo, on_delete=models.SET_NULL, null=True, blank=True)
    localidade = models.ForeignKey(Localidade, on_delete=models.SET_NULL, null=True, blank=True)

    precisa_preventiva = models.CharField(max_length=3, choices=SimNaoChoices.choices, default=SimNaoChoices.NAO)
    data_limite_preventiva = models.IntegerField(help_text="Dias para nova preventiva", blank=True, null=True)

    data_compra = models.DateField(blank=True, null=True)
    numero_pedido = models.CharField(max_length=100, blank=True, null=True)
    observacoes = models.TextField(blank=True, null=True)

    locado = models.CharField(max_length=3, choices=SimNaoChoices.choices, default=SimNaoChoices.NAO)

    class Meta:
        verbose_name = "Item / Equipamento"
        verbose_name_plural = "Itens / Equipamentos"

    def __str__(self):
        return f"{self.nome} - {self.numero_serie or 's/ nº'}"



class Usuario(AuditModel):
    nome = models.CharField(max_length=100)
    status = models.CharField(max_length=10, choices=StatusUsuarioChoices.choices)
    data_inicio = models.DateField(default=datetime.date.today)
    data_termino = models.DateField(blank=True, null=True)
    pmb = models.CharField(max_length=3, choices=SimNaoChoices.choices)
    email = models.EmailField()
    centro_custo = models.ForeignKey(CentroCusto, on_delete=models.SET_NULL, null=True)
    localidade = models.ForeignKey(Localidade, on_delete=models.SET_NULL, null=True)
    funcao = models.ForeignKey(Funcao, on_delete=models.SET_NULL, null=True)

    def __str__(self):
        return f"{self.nome} ({self.email})"
# ========== LOCAÇÃO ==========

class Locacao(AuditModel):
    equipamento = models.OneToOneField(Item, on_delete=models.CASCADE, related_name="locacao")
    tempo_locado = models.IntegerField(blank=True, null=True, help_text="Informe a quantidade de meses")
    valor_mensal = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True,
                                       validators=[MinValueValidator(0)], help_text="Valor do pagamento mensal (R$)")
    contrato = models.CharField(max_length=200, blank=True, null=True)
    observacoes = models.TextField(blank=True, null=True)
    fornecedor = models.ForeignKey(Fornecedor, on_delete=models.SET_NULL, blank=True, null=True)

    def __str__(self):
        return f"Locação: {self.equipamento.nome} - {self.tempo_locado or 0} meses"


# ========== COMENTARIO ==========


class Comentario(AuditModel):
    texto = models.TextField()
    item = models.ForeignKey(Item, on_delete=models.CASCADE, null=True, blank=True)

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

    def __str__(self):
        return f"Ciclo {self.item.nome} - {self.status_inicial}"


# ✅ Movimentação: adicionamos tipo_transferencia e fornecedor_manutencao
class MovimentacaoItem(AuditModel):
    # Tipo principal (transferencia, baixa, entrada, envio_manutencao, retorno_manutencao)
    tipo_movimentacao = models.CharField(max_length=30, choices=TipoMovimentacaoChoices.choices)

    # Detalhes
    tipo_transferencia = models.CharField(
        max_length=10,
        blank=True, null=True,
        choices=(("entrega", "Entrega"), ("devolucao", "Devolução")),
        help_text="Somente para transferência."
    )
    quantidade = models.PositiveIntegerField(default=1)
    observacao = models.TextField(blank=True, null=True)
    chamado = models.CharField(max_length=100, blank=True, null=True)
    custo = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    termo_pdf = models.FileField(upload_to="termos/", blank=True, null=True)

    # Relações
    item = models.ForeignKey("Item", on_delete=models.CASCADE, related_name="movimentacoes")
    usuario = models.ForeignKey("Usuario", on_delete=models.SET_NULL, null=True, blank=True, related_name="movimentacoes")

    localidade_origem = models.ForeignKey("Localidade", on_delete=models.SET_NULL, null=True, blank=True, related_name="movs_origem")
    localidade_destino = models.ForeignKey("Localidade", on_delete=models.SET_NULL, null=True, blank=True, related_name="movs_destino")
    centro_custo_origem = models.ForeignKey("CentroCusto", on_delete=models.SET_NULL, null=True, blank=True, related_name="movs_origem")
    centro_custo_destino = models.ForeignKey("CentroCusto", on_delete=models.SET_NULL, null=True, blank=True, related_name="movs_destino")

    # Novo: fornecedor para envio à manutenção (substitui “localidade” nesse fluxo)
    fornecedor_manutencao = models.ForeignKey("Fornecedor", on_delete=models.SET_NULL, null=True, blank=True, related_name="manutencoes")

    # Mantemos o campo mas não usamos mais no retorno (regra: voltar como BACKUP)
    status_retorno = models.CharField(max_length=15, choices=StatusItemChoices.choices, blank=True, null=True)
    # ✅ NOVO: número do pedido — obrigatório somente para ENTRADA
    numero_pedido = models.CharField(
        max_length=100, blank=True, null=True,
        help_text="Obrigatório apenas para movimentações do tipo Entrada."
    )   
    class Meta:
        verbose_name = "Movimentação de Item"
        verbose_name_plural = "Movimentações de Itens"
        ordering = ["-created_at"]

    def __str__(self):
        return f"[{self.get_tipo_movimentacao_display()}] {self.item.nome} x{self.quantidade}"

    @transaction.atomic
    def save(self, *args, **kwargs):
        """ Regras solicitadas:
        - Transferência: muda localidade/centro de custo do item (não mexe em quantidade)
        - Baixa (saída): debita quantidade
        - Entrada: adiciona quantidade
        - Envio p/ manutenção: status=manutenção e debita 1
        - Retorno manutenção: status=backup e soma 1
        - Remover usuário de envio/retorno (tratado no form/view)
        """
        is_new = self.pk is None
        super().save(*args, **kwargs)  # salva primeiro pra ter PK para anexos etc.

        if not is_new:
            return

        item = self.item

        if self.tipo_movimentacao == "transferencia":
            if self.localidade_destino:
                item.localidade = self.localidade_destino
            if self.centro_custo_destino:
                item.centro_custo = self.centro_custo_destino
            item.save(update_fields=["localidade", "centro_custo", "updated_at"])

        elif self.tipo_movimentacao == "baixa":
            # saída = debita
            nova_qtd = max(0, (item.quantidade or 0) - (self.quantidade or 0))
            item.quantidade = nova_qtd
            item.save(update_fields=["quantidade", "updated_at"])

        # models.py  (substitua só o bloco de ENTRADA)
        elif self.tipo_movimentacao == "entrada":
            # entrada = soma
            item.quantidade = (item.quantidade or 0) + (self.quantidade or 0)

            # ✅ Atualiza também CC/localidade do item com o destino informado
            fields = ["quantidade", "updated_at"]
            if self.localidade_destino:
                item.localidade = self.localidade_destino
                fields.append("localidade")
            if self.centro_custo_destino:
                item.centro_custo = self.centro_custo_destino
                fields.append("centro_custo")

            item.save(update_fields=fields)

        elif self.tipo_movimentacao == "envio_manutencao":
            # status=manutenção e debita 1 unidade
            item.status = StatusItemChoices.MANUTENCAO
            item.quantidade = max(0, (item.quantidade or 0) - 1)
            item.save(update_fields=["status", "quantidade", "updated_at"])

        elif self.tipo_movimentacao in ("retorno_manutencao", "retorno"):
            # retorna como BACKUP e soma 1
            item.status = StatusItemChoices.BACKUP
            item.quantidade = (item.quantidade or 0) + 1
            item.save(update_fields=["status", "quantidade", "updated_at"])


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
    dentro_do_prazo = models.BooleanField(default=True)

    observacao = models.TextField(blank=True, null=True)
    # NEW: evidências fotográficas da última execução
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
        Usa a periodicidade do modelo de checklist; se zero/ausente,
        tenta usar 'data_limite_preventiva' do Item (inteiro – dias).
        """
        if self.checklist_modelo and self.checklist_modelo.intervalo_dias:
            return int(self.checklist_modelo.intervalo_dias)
        try:
            return int(self.equipamento.data_limite_preventiva or 0)
        except Exception:
            return 0

    def recomputar_prazo(self, data_exec=None):
        """Recalcula data_proxima e dentro_do_prazo."""
        base = data_exec or self.data_ultima or timezone.now().date()
        dias = self._periodo_referencia()
        self.data_proxima = (base + timedelta(days=dias)) if dias > 0 else None
        if self.data_proxima:
            self.dentro_do_prazo = timezone.now().date() <= self.data_proxima
        else:
            self.dentro_do_prazo = True

    @transaction.atomic
    def registrar_execucao(self, respostas_dict: dict, usuario=None):
        """
        Registra a execução preenchendo PreventivaResposta.
        respostas_dict: { pergunta_id: valor_string }
        """
        # atualiza data_ultima
        hoje = timezone.now().date()
        self.data_ultima = hoje
        self.recomputar_prazo(hoje)
        self.save()

        # cria respostas
        perguntas = (self.checklist_modelo.perguntas.all()
                     if self.checklist_modelo else [])
        bulk = []
        for p in perguntas:
            valor = (respostas_dict.get(str(p.id)) or "").strip()
            if p.obrigatorio == SimNaoChoices.SIM and not valor:
                raise ValueError(f"Pergunta obrigatória sem resposta: {p.texto_pergunta}")
            bulk.append(PreventivaResposta(
                preventiva=self,
                pergunta=p,
                resposta=valor,
                criado_por=usuario,
                atualizado_por=usuario,
            ))
        if bulk:
            PreventivaResposta.objects.bulk_create(bulk)

class PreventivaResposta(AuditModel):
    preventiva = models.ForeignKey(Preventiva, on_delete=models.CASCADE, related_name="respostas")
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
    ATRIBUICAO = "atribuicao", _("Atribuição")
    REMOCAO = "remocao", _("Remoção")

class Licenca(AuditModel):
    nome = models.CharField(max_length=160)
    fornecedor = models.ForeignKey("Fornecedor", on_delete=models.SET_NULL, null=True, blank=True)
    centro_custo = models.ForeignKey("CentroCusto", on_delete=models.SET_NULL, null=True, blank=True)
    pmb = models.CharField(max_length=3, choices=SimNaoChoices.choices, default=SimNaoChoices.NAO)

    periodicidade = models.CharField(max_length=16, choices=PeriodicidadeChoices.choices,
                                     default=PeriodicidadeChoices.MENSAL)
    data_inicio = models.DateField()
    data_fim = models.DateField(null=True, blank=True)

    quantidade = models.PositiveIntegerField(default=0, help_text="Assentos disponíveis para atribuição.")
    # ✅ novo
    custo = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                                help_text="Custo do ciclo conforme periodicidade.")
    observacao = models.TextField(blank=True, null=True)

    class Meta:
        ordering = ["nome"]
        verbose_name = "Licença"
        verbose_name_plural = "Licenças"

    def __str__(self):
        return self.nome

    # ------- helpers de custo normalizado -------
    def _meses_do_ciclo(self) -> int | None:
        if self.periodicidade == PeriodicidadeChoices.MENSAL: return 1
        if self.periodicidade == PeriodicidadeChoices.SEMESTRAL: return 6
        if self.periodicidade == PeriodicidadeChoices.ANUAL: return 12
        if self.periodicidade == PeriodicidadeChoices.TRI: return 36
        return None  # contrato/outro

    def custo_mensal(self) -> Decimal | None:
        if not self.custo:
            return None
        meses = self._meses_do_ciclo()
        if not meses:
            return None
        return (self.custo / Decimal(meses)).quantize(Decimal("0.01"))

    def custo_anual_estimado(self) -> Decimal | None:
        cm = self.custo_mensal()
        return (cm * Decimal(12)).quantize(Decimal("0.01")) if cm is not None else None

class MovimentacaoLicenca(AuditModel):
    tipo = models.CharField(max_length=16, choices=TipoMovLicencaChoices.choices)
    licenca = models.ForeignKey(Licenca, on_delete=models.CASCADE, related_name="movimentacoes")
    usuario = models.ForeignKey("Usuario", on_delete=models.SET_NULL, null=True, blank=True, related_name="mov_licencas")
    centro_custo_destino = models.ForeignKey("CentroCusto", on_delete=models.SET_NULL, null=True, blank=True, related_name="mov_licencas_destino")

    observacao = models.TextField(blank=True, null=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Movimentação de Licença"
        verbose_name_plural = "Movimentações de Licenças"

    def __str__(self):
        return f"[{self.get_tipo_display()}] {self.licenca.nome}"

    @transaction.atomic
    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if not is_new:
            return

        lic = self.licenca
        if self.tipo == TipoMovLicencaChoices.ATRIBUICAO:
            # estoque suficiente?
            if (lic.quantidade or 0) <= 0:
                raise ValueError("Licença sem quantidade disponível para atribuição.")
            lic.quantidade = (lic.quantidade or 0) - 1
            lic.save(update_fields=["quantidade", "updated_at"])
        elif self.tipo == TipoMovLicencaChoices.REMOCAO:
            lic.quantidade = (lic.quantidade or 0) + 1
            lic.save(update_fields=["quantidade", "updated_at"])