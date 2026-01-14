from django.db import models, transaction
from django.contrib.auth.models import User
import datetime
from django.core.validators import MinValueValidator
from django.db.models import Q, Count, F, ExpressionWrapper, IntegerField
from decimal import Decimal, ROUND_HALF_UP
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


class MovimentacaoItem(AuditModel):
    # [cite_start]... Campos existentes mantidos ... [cite: 103, 104, 105]
    tipo_movimentacao = models.CharField(max_length=30, choices=TipoMovimentacaoChoices.choices)
    tipo_transferencia = models.CharField(max_length=10, blank=True, null=True, choices=TipoTransferenciaChoices.choices)
    quantidade = models.PositiveIntegerField(default=1)
    observacao = models.TextField(blank=True, null=True, verbose_name="Observações")
    chamado = models.CharField(max_length=100, blank=True, null=True, verbose_name="Nº Chamado")
    custo = models.DecimalField(max_digits=10, decimal_places=2, default=0, verbose_name="Custo da Operação")
    termo_pdf = models.FileField(upload_to="termos/", blank=True, null=True, verbose_name="Termo de Responsabilidade")

    # Relacionamentos
    item = models.ForeignKey("Item", on_delete=models.CASCADE, related_name="movimentacoes")
    usuario = models.ForeignKey("Usuario", on_delete=models.SET_NULL, null=True, blank=True, related_name="movimentacoes")
    localidade_origem = models.ForeignKey("Localidade", on_delete=models.SET_NULL, null=True, blank=True, related_name="movs_origem")
    localidade_destino = models.ForeignKey("Localidade", on_delete=models.SET_NULL, null=True, blank=True, related_name="movs_destino")
    centro_custo_origem = models.ForeignKey("CentroCusto", on_delete=models.SET_NULL, null=True, blank=True, related_name="movs_origem_cc")
    centro_custo_destino = models.ForeignKey("CentroCusto", on_delete=models.SET_NULL, null=True, blank=True, related_name="movs_destino_cc")
    fornecedor_manutencao = models.ForeignKey("Fornecedor", on_delete=models.SET_NULL, null=True, blank=True, related_name="manutencoes")

    status_retorno = models.CharField(max_length=15, choices=StatusItemChoices.choices, blank=True, null=True)
    status_transferencia = models.CharField(max_length=15, choices=StatusItemChoices.choices, blank=True, null=True, verbose_name="Novo Status")
    numero_pedido = models.CharField(max_length=100, blank=True, null=True, verbose_name="Nº Pedido/NF")

    class Meta:
        verbose_name = "Movimentação de Item"
        verbose_name_plural = "Movimentações de Itens"
        ordering = ["-created_at"]

    def __str__(self):
        return f"[{self.get_tipo_movimentacao_display()}] {self.item} x{self.quantidade}"

    @transaction.atomic
    def save(self, *args, **kwargs):
        is_new = self.pk is None
        
        # Carrega item atual
        item_obj = self.item
        if not is_new:
            item_obj.refresh_from_db()
        
        current_loc = item_obj.localidade
        current_cc = item_obj.centro_custo

        # === AUTOMAÇÃO DE CENTRO DE CUSTO ===
        
        # 1. Entrega: Se CC Destino vazio, usa CC do Usuário
        if self.tipo_movimentacao == TipoMovimentacaoChoices.TRANSFERENCIA and self.tipo_transferencia == TipoTransferenciaChoices.ENTREGA:
            if not self.centro_custo_destino and self.usuario and self.usuario.centro_custo:
                self.centro_custo_destino = self.usuario.centro_custo

        # 2. Devolução: Volta para a "Origem" (CC que entregou o item originalmente)
        elif self.tipo_movimentacao == TipoMovimentacaoChoices.TRANSFERENCIA and self.tipo_transferencia == TipoTransferenciaChoices.DEVOLUCAO:
            # A origem desta movimentação é onde o item está agora (com o usuário)
            self.centro_custo_origem = current_cc
            self.localidade_origem = current_loc
            
            # Busca a última ENTREGA para saber de onde o item veio (TI/Estoque)
            last_entrega = MovimentacaoItem.objects.filter(
                item=item_obj, 
                tipo_movimentacao=TipoMovimentacaoChoices.TRANSFERENCIA,
                tipo_transferencia=TipoTransferenciaChoices.ENTREGA
            ).order_by('-created_at').first()

            if last_entrega and last_entrega.centro_custo_origem:
                # Define o destino da devolução como a origem da entrega
                self.centro_custo_destino = last_entrega.centro_custo_origem
            elif not self.centro_custo_destino:
                # Se não houver histórico, o ideal seria ter um CC Padrão de Estoque configurado no sistema.
                # Como fallback, mantém vazio ou pode-se definir um padrão aqui.
                pass

        super().save(*args, **kwargs)

        # === SNAPSHOT DE ORIGEM (Apenas criação) ===
        if is_new:
            updates = {}
            if not self.localidade_origem_id and current_loc:
                updates['localidade_origem_id'] = current_loc.id
            if not self.centro_custo_origem_id and current_cc:
                updates['centro_custo_origem_id'] = current_cc.id
            if updates:
                MovimentacaoItem.objects.filter(pk=self.pk).update(**updates)

        # === ATUALIZAÇÃO DO ITEM ===
        update_fields = ['updated_at']
        
        # Manutenção (Envio)
        if self.tipo_movimentacao == TipoMovimentacaoChoices.ENVIO_MANUTENCAO:
            item_obj.status = StatusItemChoices.MANUTENCAO
            item_obj.quantidade = max(0, (item_obj.quantidade or 0) - (self.quantidade or 1))
            update_fields.extend(['status', 'quantidade'])

        # Manutenção (Retorno)
        elif self.tipo_movimentacao in (TipoMovimentacaoChoices.RETORNO_MANUTENCAO, "retorno"):
            item_obj.status = StatusItemChoices.BACKUP
            item_obj.quantidade = (item_obj.quantidade or 0) + (self.quantidade or 1)
            if self.localidade_destino:
                item_obj.localidade = self.localidade_destino; update_fields.append('localidade')
            update_fields.extend(['status', 'quantidade'])

        # Baixa (Cálculo automático de custo se não informado)
        elif self.tipo_movimentacao == TipoMovimentacaoChoices.BAIXA:
            item_obj.quantidade = max(0, (item_obj.quantidade or 0) - (self.quantidade or 1))
            update_fields.append('quantidade')
            if not self.custo:
                val_unit = item_obj.valor or Decimal("0.00")
                qtd = Decimal(self.quantidade or 1)
                self.custo = val_unit * qtd
                # Atualiza o custo da movimentação após salvar o item
                MovimentacaoItem.objects.filter(pk=self.pk).update(custo=self.custo)

        # Transferências (Atualização de posse)
        elif self.tipo_movimentacao == TipoMovimentacaoChoices.TRANSFERENCIA:
            if self.localidade_destino:
                item_obj.localidade = self.localidade_destino; update_fields.append('localidade')
            if self.centro_custo_destino:
                item_obj.centro_custo = self.centro_custo_destino; update_fields.append('centro_custo')

            if self.tipo_transferencia == TipoTransferenciaChoices.ENTREGA and item_obj.status == StatusItemChoices.BACKUP:
                item_obj.status = StatusItemChoices.ATIVO; update_fields.append('status')
            elif self.tipo_transferencia == TipoTransferenciaChoices.DEVOLUCAO and item_obj.status == StatusItemChoices.ATIVO:
                item_obj.status = StatusItemChoices.BACKUP; update_fields.append('status')

        elif self.tipo_movimentacao == TipoMovimentacaoChoices.TRANSFERENCIA_EQUIPAMENTO:
            if self.localidade_destino:
                item_obj.localidade = self.localidade_destino; update_fields.append('localidade')
            if self.centro_custo_destino:
                item_obj.centro_custo = self.centro_custo_destino; update_fields.append('centro_custo')
            if self.status_transferencia:
                item_obj.status = self.status_transferencia; update_fields.append('status')

        item_obj.save(update_fields=list(set(update_fields)))



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
        """Recalcula data_proxima e dentro_do_prazo."""
        base = data_exec or self.data_ultima or timezone.now().date()
        dias = self._periodo_referencia()
        self.data_proxima = (base + timedelta(days=dias)) if dias > 0 else None
        if self.data_proxima:
            self.dentro_do_prazo = timezone.now().date() <= self.data_proxima
        else:
            self.dentro_do_prazo = True

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
        if observacao:
            self.observacao = observacao
        if foto_antes:
            self.foto_antes = foto_antes
        if foto_depois:
            self.foto_depois = foto_depois

        self.recomputar_prazo(hoje)
        self.save(update_fields=["data_ultima", "data_proxima", "dentro_do_prazo", "observacao", "foto_antes", "foto_depois", "updated_at"])


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

class TipoMovLicencaChoices(models.TextChoices):
    ATRIBUICAO = 'atribuicao', _('Atribuição (Saída)')
    DEVOLUCAO = 'devolucao', _('Devolução (Entrada)')

# --- MODELO LOTE ---
class LicencaLote(AuditModel):
    licenca = models.ForeignKey(Licenca, on_delete=models.CASCADE, related_name="lotes")
    quantidade_total = models.PositiveIntegerField(verbose_name="Qtd. Comprada")
    quantidade_disponivel = models.PositiveIntegerField(verbose_name="Saldo Disponível", default=0)
    custo_ciclo = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    periodicidade = models.CharField(max_length=20, choices=PeriodicidadeChoices, default='anual')
    data_compra = models.DateField(null=True, blank=True)
    numero_pedido = models.CharField(max_length=50, null=True, blank=True)
    fornecedor = models.ForeignKey("Fornecedor", on_delete=models.SET_NULL, null=True, blank=True)
    centro_custo = models.ForeignKey("CentroCusto", on_delete=models.SET_NULL, null=True, blank=True)
    observacao = models.TextField(blank=True, null=True)

    class Meta:
        ordering = ["-data_compra", "-id"]
        verbose_name = "Lote de Licença"

    def save(self, *args, **kwargs):
        if (self._state.adding or not self.pk) and not self.quantidade_disponivel:
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