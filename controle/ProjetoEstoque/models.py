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
    # ✅ número do pedido — obrigatório apenas para ENTRADA
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
        is_new = self.pk is None
        super().save(*args, **kwargs)  # precisamos do pk/created_at

        item = self.item

        if not is_new:
            # nada a fazer em updates
            return

        # ===== REGRAS DE ESTOQUE (como já havia no seu código) =====
        # ===== TRANSFERÊNCIAS DE ESTOQUE (ajuste de status backup/ativo) =====
        if self.tipo_movimentacao == "transferencia":
            update_fields = ["updated_at"]
            mudou_algo = False

            # Atualiza localidade/CC como já fazia
            if self.localidade_destino:
                item.localidade = self.localidade_destino
                update_fields.append("localidade")
                mudou_algo = True
            if self.centro_custo_destino:
                item.centro_custo = self.centro_custo_destino
                update_fields.append("centro_custo")
                mudou_algo = True

            # === Regra solicitada ===
            # ENTREGAR ao usuário -> se está BACKUP vira ATIVO
            if self.tipo_transferencia == TipoTransferenciaChoices.ENTREGA and self.usuario_id:
                if item.status == StatusItemChoices.BACKUP:
                    item.status = StatusItemChoices.ATIVO
                    update_fields.append("status")
                    mudou_algo = True

            # DEVOLVER do usuário -> se está ATIVO volta para BACKUP
            elif self.tipo_transferencia == TipoTransferenciaChoices.DEVOLUCAO:
                if item.status == StatusItemChoices.ATIVO:
                    item.status = StatusItemChoices.BACKUP
                    update_fields.append("status")
                    mudou_algo = True

            if mudou_algo:
                # salva somente o que de fato mudou
                item.save(update_fields=list(dict.fromkeys(update_fields)))

        elif self.tipo_movimentacao == "baixa":
            nova_qtd = max(0, (item.quantidade or 0) - (self.quantidade or 0))
            item.quantidade = nova_qtd
            item.save(update_fields=["quantidade", "updated_at"])

            # ===== NOVO: snapshot de custo (FIFO sobre ENTRADAS) =====
            try:
                if not self.custo or self.custo <= 0:
                    cutoff = self.created_at  # baixa “congela” custo até aqui

                    # 1) Carrega ENTRADAS (lotes) do item até o cutoff, em ordem
                    entradas = (
                        MovimentacaoItem.objects
                        .filter(item_id=item.id,
                                tipo_movimentacao="entrada",
                                created_at__lte=cutoff)
                        .order_by("created_at", "id")
                        .values("quantidade", "custo")
                    )

                    lotes = []
                    for e in entradas:
                        q = int(e["quantidade"] or 0)
                        if q <= 0:
                            continue
                        # preço unitário do lote: custo_total / qtd_lote
                        unit = Decimal("0.00")
                        if e["custo"] is not None and q > 0:
                            unit = (Decimal(e["custo"]) / Decimal(q)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                        lotes.append([q, unit])  # [quantidade_disponível, preço_unitário]

                    # 2) Debita as BAIXAS anteriores desse item (consome dos lotes em FIFO)
                    prev_baixas = (
                        MovimentacaoItem.objects
                        .filter(item_id=item.id,
                                tipo_movimentacao="baixa",
                                created_at__lt=cutoff)
                        .order_by("created_at", "id")
                        .values("quantidade")
                    )

                    # cópia mutável para consumo
                    saldo = [[q, u] for (q, u) in lotes]
                    for b in prev_baixas:
                        consume = int(b["quantidade"] or 0)
                        idx = 0
                        while consume > 0 and idx < len(saldo):
                            disp, unit = saldo[idx]
                            if disp <= 0:
                                idx += 1
                                continue
                            take = min(disp, consume)
                            saldo[idx][0] = disp - take
                            consume -= take
                            if saldo[idx][0] == 0:
                                idx += 1
                        if consume > 0:
                            # débito acima do registrado → ignora (sem custo)
                            pass

                    # 3) Calcula custo da BAIXA atual, consumindo do saldo remanescente
                    qty = int(self.quantidade or 0)
                    total_cost = Decimal("0.00")
                    idx = 0
                    while qty > 0 and idx < len(saldo):
                        disp, unit = saldo[idx]
                        if disp <= 0:
                            idx += 1
                            continue
                        take = min(disp, qty)
                        total_cost += (Decimal(take) * unit)
                        saldo[idx][0] = disp - take
                        qty -= take
                        if saldo[idx][0] == 0:
                            idx += 1

                    # 4) Se ainda faltou quantidade, usa último preço unitário conhecido ou item.valor
                    if qty > 0:
                        last_unit = Decimal("0.00")
                        for qrem, unit in reversed(lotes):
                            if unit > 0:
                                last_unit = unit
                                break
                        if last_unit == Decimal("0.00"):
                            last_unit = Decimal(item.valor or 0)
                        total_cost += (Decimal(qty) * last_unit)

                    # grava o snapshot de custo na própria baixa (sem recursão)
                    MovimentacaoItem.objects.filter(pk=self.pk).update(custo=total_cost)
                    self.custo = total_cost
            except Exception:
                # Em caso de erro, não quebrar o fluxo da baixa
                pass

        elif self.tipo_movimentacao == "entrada":
            # soma quantidade
            item.quantidade = (item.quantidade or 0) + (self.quantidade or 0)
            fields = ["quantidade", "updated_at"]

            if self.localidade_destino:
                item.localidade = self.localidade_destino
                fields.append("localidade")
            if self.centro_custo_destino:
                item.centro_custo = self.centro_custo_destino
                fields.append("centro_custo")

            # mantém seu comportamento de atualizar o valor unitário do item,
            # mas isso NÃO afeta baixas já gravadas (que agora ficam "congeladas")
            try:
                qtd = Decimal(self.quantidade or 1)
                custo_total = Decimal(self.custo or 0)
                if custo_total > 0 and qtd > 0:
                    valor_unit = (custo_total / qtd).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    item.valor = valor_unit
                    fields.append("valor")
            except Exception:
                pass

            item.save(update_fields=fields)

        elif self.tipo_movimentacao == "envio_manutencao":
            item.status = StatusItemChoices.MANUTENCAO
            item.quantidade = max(0, (item.quantidade or 0) - 1)
            item.save(update_fields=["status", "quantidade", "updated_at"])

        elif self.tipo_movimentacao in ("retorno_manutencao", "retorno"):
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


def _capacidade_licenca(lic):
    for campo in ("assentos", "quantidade", "qtd_total", "qtd", "total_assentos"):
        if hasattr(lic, campo):
            val = getattr(lic, campo)
            if val is not None:
                try:
                    return max(0, int(val))
                except Exception:
                    pass
    return 1

class MovimentacaoLicenca(AuditModel):
    tipo = models.CharField(max_length=16, choices=TipoMovLicencaChoices.choices)
    licenca = models.ForeignKey(Licenca, on_delete=models.CASCADE, related_name="movimentacoes")
    usuario = models.ForeignKey("Usuario", on_delete=models.SET_NULL, null=True, blank=True, related_name="mov_licencas")
    centro_custo_destino = models.ForeignKey("CentroCusto", on_delete=models.SET_NULL, null=True, blank=True, related_name="mov_licencas_destino")
    lote = models.ForeignKey("LicencaLote", on_delete=models.SET_NULL, null=True, blank=True, related_name="movimentacoes")
    observacao = models.TextField(blank=True, null=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Movimentação de Licença"
        verbose_name_plural = "Movimentações de Licenças"

    def __str__(self):
        return f"[{self.get_tipo_display()}] {self.licenca.nome}"

    @transaction.atomic
    def save(self, *args, **kwargs):
        # --- Lock da licença ---
        lic = self.licenca
        if self.licenca_id:
            lic = type(self.licenca).objects.select_for_update().get(pk=self.licenca_id)

        # === Último movimento por usuário (para saber quem está ativo) ===
        qs_last = (type(self).objects
                   .filter(licenca_id=lic.id, usuario__isnull=False)
                   .exclude(pk=self.pk)
                   .order_by("usuario_id", "created_at", "id"))
        last_by_user = {}
        for mv in qs_last:
            last_by_user[mv.usuario_id] = mv  # guarda o ÚLTIMO por usuário

        # Mapa de ativos por lote (apenas quando último = ATRIBUICAO)
        ativos_por_lote = {}
        for lm in last_by_user.values():
            if lm.tipo == TipoMovLicencaChoices.ATRIBUICAO and lm.lote_id:
                ativos_por_lote[lm.lote_id] = ativos_por_lote.get(lm.lote_id, 0) + 1

        # Estado do usuário desta movimentação
        ultimo_do_usuario = last_by_user.get(self.usuario_id) if self.usuario_id else None
        ja_ativo_mesmo_usuario = bool(
            ultimo_do_usuario and ultimo_do_usuario.tipo == TipoMovLicencaChoices.ATRIBUICAO
        )

        # --- ATRIBUIÇÃO: escolher lote automaticamente se não veio ---
        lote_locked = None
        if getattr(self, "tipo", None) == TipoMovLicencaChoices.ATRIBUICAO and not getattr(self, "lote_id", None):
            lotes = list(lic.lotes.select_for_update().order_by("created_at", "id"))
            escolhido = None

            # 1) Preferir lotes com quantidade_disponivel > 0
            for lt in lotes:
                disp = getattr(lt, "quantidade_disponivel", None)
                if disp is not None and int(disp) > 0:
                    escolhido = lt
                    break

            # 2) Se não achou, calcular capacidade efetiva e vagas reais
            if escolhido is None:
                for lt in lotes:
                    cap_total = int(getattr(lt, "quantidade_total", 0) or 0)
                    disp_campo = getattr(lt, "quantidade_disponivel", None)
                    # capacidade efetiva: total>0 senão usa disponivel, senão 1
                    cap_efetiva = cap_total if cap_total > 0 else (int(disp_campo) if disp_campo is not None else 1)
                    ativos_no_lt = int(ativos_por_lote.get(lt.id, 0))
                    disponivel_efetivo = (int(disp_campo) if disp_campo is not None else (cap_efetiva - ativos_no_lt))
                    if disponivel_efetivo > 0:
                        escolhido = lt
                        break

            if escolhido:
                self.lote = escolhido
                lote_locked = type(escolhido).objects.select_for_update().get(pk=escolhido.pk)

        # Se veio lote informado, aplicar lock
        if getattr(self, "lote_id", None) and lote_locked is None:
            lote_locked = type(self.lote).objects.select_for_update().get(pk=self.lote_id)

        # === VALIDAÇÃO DE CAPACIDADE ===
        if getattr(self, "tipo", None) == TipoMovLicencaChoices.ATRIBUICAO:
            if self.lote_id:
                lot = lote_locked or self.lote
                cap_total = int(getattr(lot, "quantidade_total", 0) or 0)
                disp_campo = getattr(lot, "quantidade_disponivel", None)
                cap_efetiva = cap_total if cap_total > 0 else (int(disp_campo) if disp_campo is not None else 1)

                ativos_no_lote = int(ativos_por_lote.get(self.lote_id, 0))
                disponivel_efetivo = (int(disp_campo) if disp_campo is not None else (cap_efetiva - ativos_no_lote))

                if ja_ativo_mesmo_usuario:
                    # Já está ativo: precisa ser no MESMO lote (senão requer devolução prévia)
                    if not (ultimo_do_usuario and ultimo_do_usuario.lote_id == self.lote_id):
                        raise ValueError("Usuário já possui um assento ativo nesta licença. Faça a devolução antes de mudar de lote.")
                else:
                    if disponivel_efetivo < 0:
                        raise ValueError("Licença sem quantidade disponível para atribuição.")
            else:
                # Sem lote: regra global da licença
                cap_global = _capacidade_licenca(lic) or 0
                if cap_global <= 0:
                    cap_global = 1
                ativos_outros = 0
                for uid, lm in last_by_user.items():
                    if uid == self.usuario_id:
                        continue
                    if lm.tipo == TipoMovLicencaChoices.ATRIBUICAO:
                        ativos_outros += 1
                disponivel = cap_global - ativos_outros
                if not ja_ativo_mesmo_usuario and disponivel < 1:
                    raise ValueError("Licença sem quantidade disponível para atribuição.")

        is_create = self.pk is None
        super().save(*args, **kwargs)

        # === AJUSTE DO ESTOQUE DO LOTE (somente ao criar) ===
        if is_create and getattr(self, "lote_id", None):
            lot = lote_locked or self.lote
            cap_total = int(getattr(lot, "quantidade_total", 0) or 0)
            disp_campo = getattr(lot, "quantidade_disponivel", None)

            # reconstruir disponibilidade real quando necessário
            cap_efetiva = cap_total if cap_total > 0 else (int(disp_campo) if disp_campo is not None else 1)

            if self.tipo == TipoMovLicencaChoices.ATRIBUICAO:
                consumir = not (ja_ativo_mesmo_usuario and ultimo_do_usuario and ultimo_do_usuario.lote_id == self.lote_id)
                if consumir:
                    if disp_campo is None:
                        # Recalcula: cap − (ativos_no_lote + 1)
                        ativos_no_lote = int(ativos_por_lote.get(self.lote_id, 0))
                        lot.quantidade_disponivel = max(0, cap_efetiva - (ativos_no_lote + 1))
                    else:
                        lot.quantidade_disponivel = max(0, int(disp_campo) - 1)
                    lot.save(update_fields=["quantidade_disponivel", "updated_at"])

            elif self.tipo == TipoMovLicencaChoices.DEVOLUCAO:
                # Aumenta a disponibilidade do lote do qual o usuário estava ativo
                if ultimo_do_usuario and ultimo_do_usuario.tipo == TipoMovLicencaChoices.ATRIBUICAO and ultimo_do_usuario.lote_id:
                    lot_ant = type(ultimo_do_usuario.lote).objects.select_for_update().get(pk=ultimo_do_usuario.lote_id)
                    cap_ant = int(getattr(lot_ant, "quantidade_total", 0) or 0)
                    disp_ant = getattr(lot_ant, "quantidade_disponivel", None)
                    cap_ant_ef = cap_ant if cap_ant >= 0 else (int(disp_ant) if disp_ant is not None else 1)
                    atual = int(disp_ant or 0)
                    lot_ant.quantidade_disponivel = min(cap_ant_ef, atual + 1)
                    lot_ant.save(update_fields=["quantidade_disponivel", "updated_at"])
    @property
    def custo_ciclo_usado(self):
        """Custo do ciclo aplicado nessa movimentação (preferindo o custo do lote)."""
        try:
            if getattr(self, "lote_id", None) and self.lote and self.lote.custo_ciclo is not None:
                return self.lote.custo_ciclo
        except Exception:
            pass
        return self.licenca.custo

    @property
    def custo_mensal_usado(self):
        """Custo mensal normalizado a partir do custo_ciclo_usado e periodicidade da licença."""
        valor = self.custo_ciclo_usado
        if valor is None:
            return None
        meses = self.licenca._meses_do_ciclo()
        if not meses:
            return None
        return (Decimal(valor) / Decimal(meses)).quantize(Decimal("0.01"))
    
# --- ADICIONE ABAIXO DO MODELO Licenca ---
class LicencaLote(AuditModel):
    licenca = models.ForeignKey("Licenca", on_delete=models.CASCADE, related_name="lotes")
    quantidade_total = models.PositiveIntegerField()
    quantidade_disponivel = models.PositiveIntegerField()
    custo_ciclo = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    data_compra = models.DateField(null=True, blank=True)
    fornecedor = models.ForeignKey("Fornecedor", on_delete=models.SET_NULL, null=True, blank=True)
    centro_custo = models.ForeignKey("CentroCusto", on_delete=models.SET_NULL, null=True, blank=True)
    observacao = models.TextField(blank=True, null=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "Lote de Licença"
        verbose_name_plural = "Lotes de Licenças"

    def __str__(self):
        return f"Lote {self.licenca.nome} (tot={self.quantidade_total}, disp={self.quantidade_disponivel})"

    def custo_mensal(self):
        if not self.custo_ciclo:
            return None
        meses = self.licenca._meses_do_ciclo()
        if not meses:
            return None
        return (self.custo_ciclo / Decimal(meses)).quantize(Decimal("0.01"))

