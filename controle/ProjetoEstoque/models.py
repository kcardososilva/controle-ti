from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.db import models, transaction
from django.db.models.functions import Lower
from django.contrib.auth.models import User
import datetime
import os
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
    # Reservado NÃO é um status que alguém escolhe: só nasce (e só some) pelo
    # fluxo de reserva de vaga (`services/reserva_equipamento_service.py`), que
    # grava a vaga, quem reservou e o status de antes. Escolhê-lo à mão deixaria
    # um equipamento "reservado" sem vaga nenhuma — por isso os formulários o
    # tiram da lista (`STATUS_ITEM_ESCOLHIVEIS`).
    RESERVADO = 'reservado', 'Reservado'
    ESTOQUE = 'estoque', 'Estoque'
    MANUTENCAO = 'manutencao', 'Manutenção'
    DEFEITO = 'defeito', 'Defeito'
    PAUSADO = 'pausado', 'Pausado'
    DESCARTE = 'descarte', 'Descarte'
    DEVOLVIDO = 'devolvido', 'Devolvido (Locação)'

# Status que o usuário pode ESCOLHER num formulário (edição do equipamento,
# transferência de equipamento, retorno). Tudo menos "Reservado" — ver o
# comentário em `StatusItemChoices.RESERVADO`.
STATUS_ITEM_ESCOLHIVEIS = [
    (valor, rotulo) for valor, rotulo in StatusItemChoices.choices
    if valor != StatusItemChoices.RESERVADO
]


class AreaOperacionalChoices(models.TextChoices):
    """
    Área de operação do equipamento de infraestrutura de rede.

    Divide o parque entre o que sustenta a PRODUÇÃO (algodoeira, silo,
    indústria, campo, balança, torres) e o que atende a operação de apoio
    (escritório, alojamento, hotel, refeitório). A divisão existe porque a
    responsabilidade pela manutenção preventiva e a apuração da meta são
    por área — ver `services/preventiva_meta_service.py`.

    Vazio (`null`) significa **não classificado**, e não "não produtiva": o
    equipamento não classificado fica FORA do universo da meta e aparece
    como pendência de cadastro no painel. Um default mentiria sobre uma
    decisão que ninguém tomou.
    """
    PRODUTIVA = 'produtiva', 'Área Produtiva'
    NAO_PRODUTIVA = 'nao_produtiva', 'Área Não Produtiva'


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
    SEPARACAO_ENVIO = "separacao_envio", "Remessa para Envio"
    SEPARACAO_DEVOLUCAO = "separacao_devolucao", "Remessa para Devolução"
    DEVOLUCAO_LOCACAO = "devolucao_locacao", "Devolução de Locação"
    OUTROS = "outros", "Outros"

class TipoTransferenciaChoices(models.TextChoices):
    ENTREGA = "entrega", "Entrega"
    DEVOLUCAO = "devolucao", "Devolução"

class StatusOrdemManutencaoChoices(models.TextChoices):
    # Fluxo conduzido pelo fornecedor no Portal (ver OrdemManutencaoService):
    #   aguardando → recebido → em_avaliacao
    #     → aguardando_aprovacao → (TI) aprovado → em_reparo → reparado → devolvido → (TI) concluido
    #                            → (TI) reprovado → devolvido (avaliação técnica) → (TI) concluido
    #     → sem_reparo (contrato do substituto: valor/regime/data/modelo/série) → troca_aguardando_aprovacao
    #         → (TI) troca_aprovada → (forn.) substituto_enviado (+ cobrança pelo equipamento
    #             danificado) → (TI) troca_dano_aprovada → concluido
    #                        → (TI) troca_dano_reprovada → (forn.) substituto_enviado (reenvio da cobrança)
    #         → (TI) troca_reprovada → (forn.) sem_reparo (reenvio de contrato revisado)
    #     → sem_condicoes (motivo + valor) → descarte_avaliacao_aprovada|reprovada
    #         → (TI) descarte_avaliacao_aprovada → (forn.) devolvido_descarte → (TI) descartado
    #         → (TI) descarte_avaliacao_reprovada → (forn.) sem_condicoes (reenvio de valor revisado)
    #     → descarte_local_solicitado (motivo + valor) → (TI) descarte_local_aprovado → (fornecedor) descartado
    #                                                  → (TI) descarte_avaliacao_aprovada (recusa o LOCAL, mas já
    #                                                    aceita o valor — segue direto p/ devolvido_descarte)
    # Toda proposta de valor (reparo, troca ou descarte) vira um registro versionado
    # em OrdemManutencaoOrcamento — aprovar/reprovar é sempre decisão do TI.
    #
    # Troca antecipada (troca_antecipada=True; fornecedor abre pelo Portal p/ evitar
    # equipamento defeituoso parado): troca_ant_sub_enviado (substituto a caminho)
    #   → (TI) troca_ant_sub_recebido (recebe o substituto, ativa em estoque)
    #   → (TI) troca_ant_def_enviado (envia o defeituoso → MANUTENCAO)
    #   → (forn.) troca_ant_def_recebido (recebe o defeituoso → PAUSADO)
    #   → (forn.) troca_ant_aguardando_aprovacao (envia a proposta de reparo)
    #   → (TI) troca_ant_aprovado → concluido | troca_ant_reprovado → (forn.) troca_ant_aguardando_aprovacao (reenvio)
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
    TROCA_AGUARDANDO_APROVACAO = "troca_aguardando_aprovacao", "Troca — contrato do substituto aguardando aprovação do TI"
    TROCA_APROVADA = "troca_aprovada", "Troca — contrato aprovado pelo TI"
    TROCA_REPROVADA = "troca_reprovada", "Troca — contrato reprovado pelo TI"
    SUBSTITUTO_ENVIADO = "substituto_enviado", "Substituto enviado — aguardando aprovação da cobrança pelo dano"
    TROCA_DANO_APROVADA = "troca_dano_aprovada", "Troca — cobrança pelo equipamento danificado aprovada"
    TROCA_DANO_REPROVADA = "troca_dano_reprovada", "Troca — cobrança pelo equipamento danificado reprovada"
    SEM_CONDICOES = "sem_condicoes", "Sem condições de reparo — aguardando devolução"
    DESCARTE_AVALIACAO_APROVADA = "descarte_avaliacao_aprovada", "Descarte — avaliação aprovada pelo TI"
    DESCARTE_AVALIACAO_REPROVADA = "descarte_avaliacao_reprovada", "Descarte — avaliação reprovada pelo TI"
    DEVOLVIDO_DESCARTE = "devolvido_descarte", "Devolvido para descarte — aguardando recebimento do TI"
    DESCARTE_LOCAL_SOLICITADO = "descarte_local_solicitado", "Descarte local solicitado — aguardando aprovação do TI"
    DESCARTE_LOCAL_APROVADO = "descarte_local_aprovado", "Descarte local aprovado — aguardando o fornecedor"
    DESCARTADO = "descartado", "Descartado"
    # Troca antecipada de equipamento (substituto chega antes de enviar o defeituoso)
    TROCA_ANT_SUBSTITUTO_ENVIADO = "troca_ant_sub_enviado", "Troca antecipada — substituto a caminho"
    TROCA_ANT_SUBSTITUTO_RECEBIDO = "troca_ant_sub_recebido", "Troca antecipada — substituto recebido, enviar o defeituoso"
    TROCA_ANT_DEFEITUOSO_ENVIADO = "troca_ant_def_enviado", "Troca antecipada — defeituoso enviado ao fornecedor"
    TROCA_ANT_DEFEITUOSO_RECEBIDO = "troca_ant_def_recebido", "Troca antecipada — defeituoso recebido, aguardando proposta"
    TROCA_ANT_AGUARDANDO_APROVACAO = "troca_ant_aguardando_aprovacao", "Troca antecipada — proposta aguardando aprovação do TI"
    TROCA_ANT_APROVADO = "troca_ant_aprovado", "Troca antecipada — proposta aprovada"
    TROCA_ANT_REPROVADO = "troca_ant_reprovado", "Troca antecipada — proposta reprovada"
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
    # Marca os tipos de equipamento que podem receber um chip (tablet, celular, roteador 4G…). É o que
    # o módulo Frotas e Conectividade consulta para aceitar o equipamento como DESTINO de um chip —
    # lido, nunca escrito por ele (regra 62). Quem decide é o TI, no cadastro do subtipo.
    aceita_chip = models.BooleanField(
        "Aceita chip (SIM)", default=False,
        help_text="Marque para os tipos de equipamento que usam chip (ex.: Tablet, Celular). "
                  "Só eles aparecem para vincular um chip na ficha do equipamento.",
    )
    # O OUTRO lado: o próprio item deste tipo É um chip (SIM) — o patrimônio do chip no estoque de TI (valor, NF,
    # termo de responsabilidade, movimentações). É o que a conciliação com Frotas e Conectividade consulta para
    # saber quais itens do estoque podem ser ligados a um chip de conectividade (`M2MChip.item_ti`, regra 66).
    e_chip = models.BooleanField(
        "É chip (SIM)", default=False,
        help_text="Marque no tipo cujos itens SÃO chips (ex.: CHIP). Esses itens podem ser ligados ao chip de "
                  "conectividade (linha, operadora, sessão e consumo) pelo ICCID. Um tipo não é chip e aceita chip ao mesmo tempo.",
    )

    def clean(self):
        super().clean()
        if self.e_chip and self.aceita_chip:
            raise ValidationError({
                "e_chip": "Um tipo não pode ser chip e aceitar chip ao mesmo tempo: o chip é o item; o tablet é onde ele vai.",
            })

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


# Nome do grupo Django que identifica usuários do Portal de Licenças Office.
# Fonte única — importado pelo middleware, views e data migration. Deliberadamente
# SEPARADO de GRUPO_FORNECEDOR: um parceiro de licenças (ex.: Routerlink) não deve
# ganhar acesso ao Portal do Fornecedor (equipamentos/manutenção) de brinde, e
# vice-versa — "somente esse módulo" é a regra.
GRUPO_PARCEIRO_LICENCA = "Parceiro de Licenças"


# ─── Grupos de ACESSO RESTRITO (perfis que só alcançam UMA tela) ──────────────
#
# Diferença em relação a GRUPO_FORNECEDOR / GRUPO_PARCEIRO_LICENCA: aqueles dois
# são EMPRESAS externas com portal próprio (`/portal/`, `/portal-licencas/`) e um
# perfil em tabela (PerfilFornecedor/PerfilParceiroLicenca) que amarra o login à
# empresa. Estes dois são PESSOAS — RH, gestor de área, terceiro — que usam uma
# tela que o TI já tinha, sem ganhar o resto do sistema. Não há empresa a amarrar,
# então não há tabela de perfil: a autorização é o grupo, e só.
#
# Fonte única dos nomes. O literal "Visualizador TV" está copiado em SEIS
# arquivos neste projeto (middleware, views/plantas, admin, 3 services e um
# management command) — mudar o nome daquele grupo hoje exige caçar os seis.
# Estes nascem importados de um lugar só para não repetir o mesmo erro.
GRUPO_PORTAL_CONTRATACAO = "Portal de Contratações"
GRUPO_CONSULTA_QUIOSQUE = "Consulta Quiosque"

# Frotas e Conectividade (ex-"Monitoramento TIM M2M"): dois grupos, criados/renomeados
# pela migration 0191 (a 0189 criou o antigo "Monitoramento M2M", que foi renomeado).
#   • GRUPO_FROTAS          — as 9 permissões de operação. `administrar_frota_config`
#                             (token, frequência, mapeamento, tipos, operadoras e
#                             regras de alerta) NÃO vai no grupo: é permissão
#                             explícita, dada a quem de fato mexe na credencial.
#   • GRUPO_FROTAS_CONSULTA — só as 4 de leitura (ver_frota, ver_chips,
#                             consultar_conectividade, ver_historico_frota).
# Fonte única dos nomes: nenhum outro arquivo repete o literal (a migration 0191
# é a exceção legítima — migration não importa model).
GRUPO_FROTAS = "Frotas e Conectividade"
GRUPO_FROTAS_CONSULTA = "Frotas — Consulta"

# Ambientes do Zelo (TI · Agro) — ver `services/ambiente_service.py`.
#   • O TI é o padrão de todo usuário interno (não é permissão a conceder).
#   • O Agro exige `acessar_zelo_agro` OU qualquer permissão de módulo do Agro.
#   • GRUPO_SOMENTE_AGRO prende o login no Agro (o "acesso somente ao Zelo Agro").
GRUPO_SOMENTE_AGRO = "Zelo Agro — Exclusivo"
# Portal do Colaborador (2026-11-14): quem VÊ, MOVIMENTA e CADASTRA no Agro, sem dado da API e sem o TI. Também prende o login no
# Agro (`ambiente_service.GRUPOS_SOMENTE_AGRO`); a diferença para o "Exclusivo" é que este já leva as permissões de uso do
# módulo e NENHUMA de API. A lista mora em `services/colaborador_agro_service.py` (a migration 0209 é o retrato dela).
GRUPO_COLABORADOR_AGRO = "Zelo Agro — Colaborador"
PERMISSOES_AGRO_AMBIENTE = [
    ("acessar_zelo_agro", "Pode acessar o ambiente Zelo Agro (visão geral)"),
]


class PerfilParceiroLicenca(AuditModel):
    """
    Liga um usuário Django (login) a um Fornecedor tratado aqui como "empresa
    parceira" (ex.: Routerlink), habilitando acesso ao Portal de Licenças
    Office — área isolada e restrita SOMENTE ao módulo `LicencaOffice`.
    Reaproveita o cadastro de `Fornecedor` (mesmo conceito de "empresa
    externa" já usado no resto do sistema) em vez de criar um cadastro de
    parceiro do zero, mas é um perfil de acesso independente de
    `PerfilFornecedor` — as duas áreas isoladas nunca se misturam.

    Sandbox de acesso (mesma defesa em profundidade do Portal do
    Fornecedor): grupo "Parceiro de Licenças" + LicencaOfficeAccessMiddleware
    + views decoradas com `@parceiro_licenca_required`.
    """
    usuario = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="perfil_parceiro_licenca",
        verbose_name="Usuário de acesso",
    )
    parceiro = models.ForeignKey(
        Fornecedor,
        on_delete=models.CASCADE,
        related_name="perfis_parceiro_licenca",
        verbose_name="Empresa parceira",
        help_text="Empresa externa com acesso ao Portal de Licenças Office (ex.: Routerlink).",
    )
    ativo = models.BooleanField(
        default=True,
        help_text="Desmarque para suspender o acesso sem excluir o usuário.",
    )
    pode_ver_colaboradores = models.BooleanField(
        default=False,
        verbose_name="Pode ver colaboradores",
        help_text="Quando ativo, este parceiro também enxerga a lista de colaboradores "
                   "(nome, e-mail, função, centro de custo e localidade — sem dados "
                   "sensíveis de RH) no Portal de Licenças. Desligado por padrão: cada "
                   "parceiro só ganha essa visão se o TI liberar explicitamente.",
    )

    class Meta:
        verbose_name = "Perfil de Parceiro de Licenças"
        verbose_name_plural = "Perfis de Parceiros de Licenças"

    def __str__(self):
        return f"{self.usuario.username} → {self.parceiro.nome} (Licenças Office)"


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
class ItemManager(models.Manager):
    """
    Manager padrão: nunca retorna itens excluídos (soft delete). Isso filtra
    automaticamente listagens, dashboards, exports e seletores de formulário
    (tudo que usa `Item.objects`) sem precisar tocar em cada view/form.

    FKs para Item (MovimentacaoItem.item, OrdemManutencao.item, etc.) e o
    Django Admin usam `Item.all_objects` (base manager, ver Meta abaixo) —
    o histórico ligado a um item excluído continua 100% acessível.
    """
    def get_queryset(self):
        return super().get_queryset().filter(excluido=False)


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

    # Classificação do equipamento de rede entre Área Produtiva e Área Não
    # Produtiva. É o que liga o ativo ao técnico responsável e à apuração da
    # meta de preventivas (ver AreaOperacionalChoices e
    # services/preventiva_meta_service.py). Opcional de propósito: o campo
    # vale para switch, access point, Meraki, core, fonte, antena e rádio,
    # e não faz sentido para monitor ou headset.
    area_operacional = models.CharField(
        max_length=15,
        choices=AreaOperacionalChoices.choices,
        blank=True,
        null=True,
        db_index=True,
        verbose_name="Área de operação",
        help_text=(
            "Para equipamentos de infraestrutura de rede: indica se o ativo "
            "sustenta a área produtiva ou a área não produtiva. Define o "
            "técnico responsável pela preventiva e a apuração da meta."
        ),
    )

    data_compra = models.DateField(blank=True, null=True)
    numero_pedido = models.CharField(max_length=100, blank=True, null=True)
    numero_nf = models.CharField(
        max_length=60,
        blank=True,
        null=True,
        verbose_name="Número da NF",
        help_text="Nota fiscal do item — válido tanto para item próprio quanto locado."
    )
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

    # ── Exclusão lógica (soft delete) ──────────────────────────────────
    # Um delete físico apagaria (CASCADE) todo o histórico de movimentações,
    # OS de manutenção, ciclos e preventivas — ou seria bloqueado (PROTECT)
    # por lotes vinculados. Excluir aqui só marca o item como excluído: ele
    # some das listagens/dashboards/seletores (via `ItemManager`), mas a
    # linha e todo o histórico ligado a ela permanecem intactos.
    excluido = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name="Excluído",
        help_text="Item removido das listagens e seletores. Histórico vinculado é preservado.",
    )
    excluido_em = models.DateTimeField(null=True, blank=True, verbose_name="Excluído em")
    excluido_por = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Excluído por",
    )

    objects = ItemManager()
    all_objects = models.Manager()

    class Meta:
        verbose_name = "Item / Equipamento"
        verbose_name_plural = "Itens / Equipamentos"
        base_manager_name = "all_objects"
        indexes = [
            models.Index(fields=["nome"]),
            models.Index(fields=["status"]),
            models.Index(fields=["item_consumo"]),
            models.Index(fields=["localidade"]),
            models.Index(fields=["centro_custo"]),
            models.Index(fields=["excluido"]),
            # Universo da meta de preventivas: filtra por subtipo + área +
            # "precisa preventiva" + status numa única varredura.
            models.Index(
                fields=["subtipo", "area_operacional", "precisa_preventiva", "status"],
                name="item_universo_meta_idx",
            ),
        ]

    @property
    def area_operacional_label(self):
        """Rótulo curto da área ('Produtiva' / 'Não Produtiva' / 'Não classificada')."""
        if self.area_operacional == AreaOperacionalChoices.PRODUTIVA:
            return "Produtiva"
        if self.area_operacional == AreaOperacionalChoices.NAO_PRODUTIVA:
            return "Não Produtiva"
        return "Não classificada"

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
    def aguardando_inicio(self) -> bool:
        """True quando a cobrança ainda não começou (data_inicio no futuro)."""
        return self.data_inicio > datetime.date.today()

    @property
    def _fim_efetivo(self):
        return self.data_fim or datetime.date.today()

    @property
    def meses(self) -> int:
        """Meses cheios decorridos no período. 0 se ainda não começou."""
        if self.aguardando_inicio:
            return 0
        delta = relativedelta(self._fim_efetivo, self.data_inicio)
        return delta.years * 12 + delta.months

    @property
    def dias(self) -> int:
        if self.aguardando_inicio:
            return 0
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

        # Instância completamente vazia: cadastro de item de consumo sem dar
        # entrada ainda (ver `ItemCreateService.criar_item` / `item_update`).
        # Django chama `Model.clean()` incondicionalmente dentro do
        # `ModelForm.full_clean()`, mesmo quando o form marcou estes campos
        # como não-obrigatórios e o usuário deixou tudo em branco de
        # propósito — sem este escape, `LoteEstoqueCreateForm.is_valid()`
        # nunca passaria nesse cenário. Uma instância parcialmente
        # preenchida continua caindo nas validações abaixo normalmente.
        #
        # `quantidade` (tem `default=1`) fica de fora do sinal de presença de
        # propósito: se o campo não vier no POST, `ModelForm._post_clean()`
        # (via `construct_instance`) preserva o default do model em vez de
        # zerá-lo, o que faria uma instância genuinamente vazia parecer
        # "preenchida" só por causa do `1` default. Os outros 4 campos não
        # têm default — ausência neles é sempre um sinal confiável.
        preenchido = any([
            self.fornecedor_id,
            self.data_entrada,
            self.numero_nf,
            self.custo_unitario,
        ])

        if not preenchido:
            return

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

    area_administrativa = models.BooleanField(
        default=False,
        verbose_name="Área administrativa",
        help_text="Colaboradores de área administrativa costumam receber licenças de software no cadastro.",
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

    # ── Reversão ────────────────────────────────────────────────────────
    # A movimentação nunca é apagada — reverter só desfaz o efeito no
    # estoque/item e marca estes campos, preservando o registro de que a
    # operação aconteceu (e foi depois revertida).
    revertida = models.BooleanField(default=False, db_index=True, verbose_name="Revertida")
    revertida_em = models.DateTimeField(null=True, blank=True, verbose_name="Revertida em")
    revertida_por = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Revertida por",
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
    # Identificação do equipamento substituto — já exigida na PROPOSTA do contrato
    # (antes da aprovação do TI), para que o TI possa avaliar o modelo oferecido
    # antes de aprovar o valor/regime. Número de série é o do equipamento físico
    # já separado pelo fornecedor para o envio.
    substituto_modelo = models.CharField(
        max_length=200, blank=True, null=True, verbose_name="Modelo do substituto"
    )
    substituto_numero_serie = models.CharField(
        max_length=200, blank=True, null=True, verbose_name="Nº de série do substituto"
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
    # Lote de Manutenção (fatura/documento consolidado): funcionalidade
    # descontinuada (as telas de criação/listagem foram removidas — ver Lote de
    # Envio, que cobre o mesmo caso de uso com NF/rastreio). Campo mantido só
    # para não perder o vínculo de lotes já existentes; SET_NULL preserva o
    # histórico da OS caso o lote seja excluído diretamente no banco/admin.
    lote_manutencao = models.ForeignKey(
        "LoteManutencao",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="ordens",
        verbose_name="Lote de manutenção",
    )

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
                "troca_ant_aguardando_aprovacao": 3,
                "troca_ant_aprovado": 3,
                "troca_ant_reprovado": 3,
                "concluido": 5, "cancelado": 4,
            }.get(self.status, 0)
        return {
            "aguardando_recebimento": 0,
            "recebido": 1, "em_avaliacao": 1,
            "aguardando_aprovacao": 2, "aprovado": 2, "reprovado": 2,
            "em_reparo": 3, "reparado": 3, "sem_reparo": 3,
            "troca_aguardando_aprovacao": 3, "troca_aprovada": 3, "troca_reprovada": 3,
            "substituto_enviado": 3, "troca_dano_aprovada": 3, "troca_dano_reprovada": 3,
            "devolvido": 3, "sem_condicoes": 3,
            "descarte_avaliacao_aprovada": 3, "descarte_avaliacao_reprovada": 3,
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

    # ── Custo final da OS ────────────────────────────────────────────────────
    @property
    def valor_manutencao(self):
        """
        Custo final e definitivo desta OS. Não existe um único campo de valor
        porque o desfecho do fluxo muda qual campo é o relevante — este método
        resolve isso na ordem correta. Usado no histórico do Portal/TI. Retorna
        None se a OS ainda não tem custo apurado.

        Prioridade:
          1. reparo_valor            — reparo concluído OU devolução sem reparo (reprovado)
          2. substituto_valor        — troca padrão (equipamento substituído); soma
             valor_avaliacao_tecnica quando também houver cobrança aprovada pelo
             equipamento danificado (os dois custos compõem esta OS de troca)
          3. valor_orcamento         — troca antecipada concluída (proposta do fornecedor)
          4. valor_avaliacao_tecnica — descarte (local ou via TI), sem os campos acima
        """
        if self.reparo_valor is not None:
            return self.reparo_valor
        if self.substituto_valor is not None:
            total = self.substituto_valor
            if self.valor_avaliacao_tecnica is not None:
                total += self.valor_avaliacao_tecnica
            return total
        if self.troca_antecipada and self.valor_orcamento is not None:
            return self.valor_orcamento
        if self.valor_avaliacao_tecnica is not None:
            return self.valor_avaliacao_tecnica
        return None

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


class OrdemManutencaoOrcamento(AuditModel):
    """
    Histórico versionado de propostas de orçamento. Cada vez que o fornecedor
    envia um orçamento (proposta), cria-se um novo registro. O TI pode aprovar
    ou reprovar, passando a ordem para um novo status.

    Fluxo de status:
      • PROPOSTO → enviado pelo fornecedor, aguardando decisão do TI
      • APROVADO → TI aprovou, pode iniciar o reparo
      • REPROVADO → TI reprovou, fornecedor pode reenvidar novo orçamento ou devolver
    """
    class StatusOrcamentoChoices(models.TextChoices):
        PROPOSTO = "proposto", "Enviado para análise"
        APROVADO = "aprovado", "Aprovado"
        REPROVADO = "reprovado", "Reprovado"

    class TipoOrcamento(models.TextChoices):
        REPARO = "reparo", "Orçamento de reparo"
        TROCA = "troca", "Contrato do equipamento substituto (troca)"
        TROCA_DANIFICADO = "troca_danificado", "Cobrança pelo equipamento danificado (troca)"
        DESCARTE = "descarte", "Avaliação de descarte (devolução ao TI)"
        DESCARTE_LOCAL = "descarte_local", "Avaliação de descarte local"

    ordem = models.ForeignKey(
        OrdemManutencao,
        on_delete=models.CASCADE,
        related_name="orcamentos",
        verbose_name="Ordem de Manutenção",
    )
    tipo = models.CharField(
        max_length=20,
        choices=TipoOrcamento.choices,
        default=TipoOrcamento.REPARO,
        verbose_name="Tipo de avaliação",
        help_text="De qual desfecho esta proposta faz parte (reparo, troca ou descarte).",
    )
    numero = models.PositiveIntegerField(
        verbose_name="Número da proposta",
        help_text="1, 2, 3... (auto-incrementado por ordem de criação)",
    )
    valor = models.DecimalField(
        max_digits=12, decimal_places=2,
        verbose_name="Valor proposto",
    )
    status = models.CharField(
        max_length=12,
        choices=StatusOrcamentoChoices.choices,
        default=StatusOrcamentoChoices.PROPOSTO,
        verbose_name="Status da proposta",
    )
    motivo_rejeicao = models.TextField(
        blank=True, null=True,
        verbose_name="Motivo da rejeição (se reprovado)",
    )

    class Meta:
        ordering = ["numero"]
        verbose_name = "Orçamento de Manutenção"
        verbose_name_plural = "Orçamentos de Manutenção"
        indexes = [
            models.Index(fields=["ordem", "numero"]),
        ]

    def __str__(self):
        return f"OS #{self.ordem_id} — Proposta {self.numero} (R$ {self.valor})"

    def is_proposta_atual(self):
        """Retorna True se esta é a proposta atualmente em análise pelo TI."""
        ordem = self.ordem
        if ordem.status not in (
            StatusOrdemManutencaoChoices.AGUARDANDO_APROVACAO,
            StatusOrdemManutencaoChoices.REPROVADO,
            StatusOrdemManutencaoChoices.TROCA_ANT_AGUARDANDO_APROVACAO,
            StatusOrdemManutencaoChoices.TROCA_ANT_REPROVADO,
            StatusOrdemManutencaoChoices.TROCA_AGUARDANDO_APROVACAO,
            StatusOrdemManutencaoChoices.TROCA_REPROVADA,
            StatusOrdemManutencaoChoices.SUBSTITUTO_ENVIADO,
            StatusOrdemManutencaoChoices.TROCA_DANO_REPROVADA,
            StatusOrdemManutencaoChoices.SEM_CONDICOES,
            StatusOrdemManutencaoChoices.DESCARTE_AVALIACAO_REPROVADA,
            StatusOrdemManutencaoChoices.DESCARTE_LOCAL_SOLICITADO,
        ):
            return False
        return self.numero == ordem.orcamentos.count()

    def get_status_contexto(self):
        """Retorna o label inteligente do status, contextualizando o fluxo.

        O status gravado (`self.status`) já reflete a decisão real do TI
        (ver `OrdemManutencaoService._on_decisao_ti`) — por isso aprovado e
        reprovado são verificados diretamente, sem depender de
        `is_proposta_atual()`. Essa checagem só entra para diferenciar, entre
        as propostas ainda PROPOSTAS, qual está em análise agora.
        """
        if self.status == self.StatusOrcamentoChoices.APROVADO:
            return "Aprovada — reparo autorizado"
        elif self.status == self.StatusOrcamentoChoices.REPROVADO:
            return "Reprovada — reenvio necessário"
        elif self.status == self.StatusOrcamentoChoices.PROPOSTO:
            if self.is_proposta_atual():
                return "Em análise do TI"
            else:
                return "Substituída por novo orçamento"
        return self.get_status_display()

    def get_badge_status(self):
        """Retorna um dict com classe CSS e ícone para renderização de badge.

        Mesma ordem de verificação de `get_status_contexto()`: o status
        decidido (aprovado/reprovado) é definitivo e não deve ser
        reavaliado por `is_proposta_atual()`.
        """
        if self.status == self.StatusOrcamentoChoices.APROVADO:
            return {
                "class": "pf-orc-aprovado",
                "icon": "fa-check-circle",
                "label": "Aprovada",
                "context": "Reparo autorizado"
            }
        elif self.status == self.StatusOrcamentoChoices.REPROVADO:
            return {
                "class": "pf-orc-reprovado",
                "icon": "fa-xmark-circle",
                "label": "Reprovada",
                "context": "Reenvio necessário"
            }
        elif self.status == self.StatusOrcamentoChoices.PROPOSTO:
            if self.is_proposta_atual():
                return {
                    "class": "pf-orc-proposto-atual",
                    "icon": "fa-hourglass-half",
                    "label": "Em análise",
                    "context": "Aguardando TI"
                }
            else:
                return {
                    "class": "pf-orc-substituido",
                    "icon": "fa-file-lines",
                    "label": "Substituída",
                    "context": "Sem decisão — sobreposta por nova proposta"
                }
        return {
            "class": "pf-orc-unknown",
            "icon": "fa-question-circle",
            "label": "Desconhecido",
            "context": ""
        }

    def get_status_emoji(self):
        """Retorna emoji representativo do status (alternativa aos ícones)."""
        if self.status == self.StatusOrcamentoChoices.PROPOSTO and self.is_proposta_atual():
            return "⏳"
        elif self.status == self.StatusOrcamentoChoices.PROPOSTO:
            return "📋"
        elif self.status == self.StatusOrcamentoChoices.APROVADO:
            return "✅"
        elif self.status == self.StatusOrcamentoChoices.REPROVADO:
            return "❌"
        return "•"

    def is_resubmissao(self):
        """Retorna True se esta não é a primeira proposta (número > 1)."""
        return self.numero > 1

    def is_rejeicao_clara(self):
        """Retorna True se esta proposta foi claramente reprovada."""
        return self.status == self.StatusOrcamentoChoices.REPROVADO and self.motivo_rejeicao


class LoteManutencao(AuditModel):
    """
    Agrupamento de Ordens de Manutenção já concluídas em um único documento
    (lote/fatura). Modelo mantido só por compatibilidade com dados já
    existentes — as telas de criação/listagem (Portal e TI) foram removidas;
    o caso de uso (retorno de reparo concluído ao TI) hoje é coberto pelo
    Lote de Envio (`LoteEnvioFornecedor`, tipo `reparo_concluido`), que tem
    NF e rastreio de status. Não criar novos registros deste modelo.
    """
    nome = models.CharField(max_length=150, verbose_name="Nome do lote")
    data = models.DateField(verbose_name="Data do lote")
    fornecedor = models.ForeignKey(
        "Fornecedor",
        on_delete=models.PROTECT,
        related_name="lotes_manutencao",
        verbose_name="Fornecedor",
    )

    class Meta:
        ordering = ["-data", "-created_at"]
        verbose_name = "Lote de Manutenção"
        verbose_name_plural = "Lotes de Manutenção"
        indexes = [
            models.Index(fields=["fornecedor", "data"]),
        ]

    def __str__(self):
        return f"{self.nome} — {self.fornecedor.nome} ({self.data:%d/%m/%Y})"

    @property
    def quantidade_ordens(self) -> int:
        return len(self.ordens.all())

    @property
    def valor_total(self):
        from decimal import Decimal
        total = Decimal("0.00")
        for ordem in self.ordens.all():
            valor = ordem.valor_manutencao
            if valor:
                total += valor
        return total


class StatusLoteEnvioFornecedorChoices(models.TextChoices):
    ABERTO = "aberto", "Em organização"
    ENVIADO = "enviado", "Enviado ao TI"
    CANCELADO = "cancelado", "Cancelado"


class TipoItemLoteEnvioChoices(models.TextChoices):
    TROCA_ANTECIPADA = "troca_antecipada", "Troca antecipada"
    EQUIPAMENTO_NOVO = "equipamento_novo", "Equipamento novo"
    REPARO_CONCLUIDO = "reparo_concluido", "Reparo concluído"


class StatusItemLoteEnvioChoices(models.TextChoices):
    RASCUNHO = "rascunho", "Em organização (rascunho)"
    ENVIADO = "enviado", "Enviado ao TI"
    RECEBIDO = "recebido", "Recebido pelo TI"
    CANCELADO = "cancelado", "Cancelado"


class LoteEnvioFornecedor(AuditModel):
    """
    "Carrinho" que o próprio fornecedor monta no Portal para organizar o envio de
    trocas antecipadas e cadastro de equipamento novo ao TI — direção oposta à de
    `LoteSeparacao` (que só o TI cria). Um fornecedor pode ter vários lotes ABERTOS
    simultâneos (ex.: remessas físicas separadas); os itens dentro dele ficam em
    RASCUNHO (ver `LoteEnvioFornecedorItem`) até o fornecedor enviar — só nesse
    momento eles viram, de fato, uma OrdemManutencao/Item real no sistema.
    """
    nome = models.CharField(max_length=150, verbose_name="Nome do lote")
    fornecedor = models.ForeignKey(
        "Fornecedor",
        on_delete=models.PROTECT,
        related_name="lotes_envio",
        verbose_name="Fornecedor",
    )
    status = models.CharField(
        max_length=10,
        choices=StatusLoteEnvioFornecedorChoices.choices,
        default=StatusLoteEnvioFornecedorChoices.ABERTO,
        db_index=True,
    )
    observacoes = models.TextField(blank=True, null=True)
    enviado_em = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Lote de Envio do Fornecedor"
        verbose_name_plural = "Lotes de Envio do Fornecedor"
        indexes = [
            models.Index(fields=["fornecedor", "status"]),
        ]

    def __str__(self):
        return f"{self.nome} — {self.fornecedor.nome}"

    @property
    def quantidade_itens(self) -> int:
        return len(self.itens.exclude(status=StatusItemLoteEnvioChoices.CANCELADO))

    @property
    def quantidade_rascunho(self) -> int:
        return sum(1 for i in self.itens.all() if i.status == StatusItemLoteEnvioChoices.RASCUNHO)


class LoteEnvioFornecedorItem(AuditModel):
    """
    Linha do `LoteEnvioFornecedor`. Enquanto `status=RASCUNHO` é só dado bruto de
    formulário — nenhuma transição real acontece ainda. Só no envio (ver
    `services/lote_envio_fornecedor_service.py::LoteEnvioFornecedorService.enviar_item`)
    é que a linha vira, de fato: uma troca antecipada real (`OrdemManutencao`, via
    `OrdemManutencaoService.abrir_troca_antecipada`), um `Item` novo (PAUSADO), ou
    — no caso REPARO_CONCLUIDO — a transição da OS de reparo já existente
    (`ordem`) para `DEVOLVIDO` (a OS já existe desde a criação do rascunho; só o
    estado dela ainda não mudou).
    """
    lote = models.ForeignKey(
        LoteEnvioFornecedor,
        on_delete=models.CASCADE,
        related_name="itens",
        verbose_name="Lote de envio",
    )
    tipo = models.CharField(max_length=20, choices=TipoItemLoteEnvioChoices.choices, verbose_name="Tipo")
    status = models.CharField(
        max_length=10,
        choices=StatusItemLoteEnvioChoices.choices,
        default=StatusItemLoteEnvioChoices.RASCUNHO,
        db_index=True,
    )
    # ── Caso TROCA_ANTECIPADA (rascunho dos dados do formulário já existente) ──
    item_defeituoso = models.ForeignKey(
        "Item",
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="rascunhos_troca_antecipada",
        verbose_name="Equipamento com defeito",
    )
    sub_modelo = models.CharField(max_length=200, blank=True, default="", verbose_name="Modelo do substituto")
    sub_serie = models.CharField(max_length=200, blank=True, default="", verbose_name="Série do substituto")
    sub_marca = models.CharField(max_length=200, blank=True, default="", verbose_name="Marca do substituto")
    sub_data_contrato = models.DateField(null=True, blank=True, verbose_name="Data de contrato do equipamento")
    # ── Caso EQUIPAMENTO_NOVO (rascunho de um cadastro de item) ─────────────────
    novo_nome = models.CharField(max_length=200, blank=True, default="", verbose_name="Nome/Modelo")
    novo_numero_serie = models.CharField(max_length=200, blank=True, default="", verbose_name="Número de série")
    novo_marca = models.CharField(max_length=200, blank=True, default="", verbose_name="Marca")
    novo_modelo = models.CharField(max_length=200, blank=True, default="", verbose_name="Modelo")
    novo_categoria = models.ForeignKey(
        "Categoria", on_delete=models.SET_NULL, null=True, blank=True, related_name="+", verbose_name="Categoria",
    )
    novo_subtipo = models.ForeignKey(
        "Subtipo", on_delete=models.SET_NULL, null=True, blank=True, related_name="+", verbose_name="Subtipo",
    )
    novo_localidade = models.ForeignKey(
        "Localidade", on_delete=models.SET_NULL, null=True, blank=True, related_name="+", verbose_name="Localidade",
    )
    novo_centro_custo = models.ForeignKey(
        "CentroCusto", on_delete=models.SET_NULL, null=True, blank=True, related_name="+", verbose_name="Centro de custo",
    )
    novo_locado = models.CharField(
        max_length=3, choices=SimNaoChoices.choices, default=SimNaoChoices.NAO, verbose_name="Entra como locação?",
    )
    novo_pmb = models.CharField(
        max_length=3, choices=SimNaoChoices.choices, default=SimNaoChoices.NAO, verbose_name="PMB?",
    )
    novo_valor = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, verbose_name="Valor")
    novo_contrato = models.CharField(max_length=200, blank=True, default="", verbose_name="Contrato")
    novo_tempo_contrato_meses = models.PositiveIntegerField(null=True, blank=True, verbose_name="Tempo de contrato (meses)")
    novo_cobranca_proximo_ano = models.CharField(
        max_length=3, choices=SimNaoChoices.choices, default=SimNaoChoices.NAO,
        verbose_name="Cobrança do aluguel só a partir do próximo ano?",
        help_text=(
            "Se Sim, o período de locação começa em 1º de janeiro do ano seguinte à "
            "confirmação de recebimento pelo TI. Se Não, começa na data em que o "
            "fornecedor cadastrou o equipamento neste lote."
        ),
    )
    # ── Caso REPARO_CONCLUIDO (retorno de uma OS de reparo normal já existente) ─
    localidade_devolucao = models.ForeignKey(
        "Localidade",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="+",
        verbose_name="Localidade de devolução",
    )
    valor_avaliacao_tecnica = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        verbose_name="Valor da avaliação técnica",
        help_text="Só se aplica quando a OS foi reprovada (devolução sem reparo).",
    )
    # ── Resultado da materialização (preenchido só no envio, exceto em
    # REPARO_CONCLUIDO onde a OS já existe e é setada na criação) ──────────────
    ordem = models.ForeignKey(
        "OrdemManutencao",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="+",
        verbose_name="Ordem de manutenção gerada",
    )
    item_resultado = models.ForeignKey(
        "Item",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="+",
        verbose_name="Equipamento gerado",
    )
    enviado_em = models.DateTimeField(null=True, blank=True)
    recebido_em = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]
        verbose_name = "Item do Lote de Envio"
        verbose_name_plural = "Itens do Lote de Envio"
        indexes = [
            models.Index(fields=["tipo", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["item_defeituoso"],
                condition=models.Q(status="rascunho", tipo="troca_antecipada"),
                name="uniq_item_defeituoso_rascunho_aberto",
            ),
            models.UniqueConstraint(
                fields=["ordem"],
                condition=models.Q(status="rascunho", tipo="reparo_concluido"),
                name="uniq_ordem_reparo_rascunho_aberto",
            ),
        ]

    def __str__(self):
        if self.item_defeituoso_id:
            alvo = self.item_defeituoso.nome
        elif self.tipo == TipoItemLoteEnvioChoices.REPARO_CONCLUIDO and self.ordem_id:
            alvo = self.ordem.item.nome
        else:
            alvo = self.novo_nome or "Equipamento novo"
        return f"{self.get_tipo_display()} — {alvo}"


class LoteEnvioFornecedorAnexo(AuditModel):
    """
    NF (ou outro documento) anexada a um `LoteEnvioFornecedor`. Espelha
    `OrdemManutencaoAnexo`: tanto o fornecedor quanto o TI podem anexar/excluir.
    """
    class OrigemAnexo(models.TextChoices):
        FORNECEDOR = "fornecedor", "Fornecedor"
        TI = "ti", "TI"

    lote = models.ForeignKey(
        LoteEnvioFornecedor,
        on_delete=models.CASCADE,
        related_name="anexos",
    )
    arquivo = models.FileField(upload_to="lote_envio_fornecedor/nf/%Y/%m/", verbose_name="Arquivo")
    origem = models.CharField(
        max_length=12, choices=OrigemAnexo.choices, default=OrigemAnexo.FORNECEDOR,
        verbose_name="Origem",
    )
    descricao = models.CharField(max_length=200, blank=True, default="", verbose_name="Descrição")

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "Anexo do Lote de Envio"
        verbose_name_plural = "Anexos do Lote de Envio"

    def __str__(self):
        return f"NF Lote #{self.lote_id} ({self.get_origem_display()})"

    @property
    def nome_arquivo(self):
        import os
        return os.path.basename(self.arquivo.name) if self.arquivo else ""


class TipoSeparacaoChoices(models.TextChoices):
    ENVIO = "envio", "Remessa para Envio"
    DEVOLUCAO = "devolucao", "Remessa para Devolução"


class StatusSeparacaoChoices(models.TextChoices):
    ABERTO = "aberto", "Em separação"
    ENVIADO = "enviado", "Enviado"
    CANCELADO = "cancelado", "Removido da separação"


class LoteSeparacao(AuditModel):
    """
    Agrupamento nomeado de itens em separação (Envio ou Devolução) para despacho
    conjunto. Espelha `LoteManutencao`, mas para a etapa ANTERIOR à movimentação
    real — reúne itens já marcados como "em separação" (ver `SeparacaoItem`) antes
    de virarem, de fato, um envio para manutenção ou uma devolução de locação.
    """
    nome = models.CharField(max_length=150, verbose_name="Nome do lote")
    tipo = models.CharField(max_length=10, choices=TipoSeparacaoChoices.choices, verbose_name="Tipo")
    fornecedor = models.ForeignKey(
        "Fornecedor",
        on_delete=models.PROTECT,
        related_name="lotes_separacao",
        verbose_name="Fornecedor",
    )
    status = models.CharField(
        max_length=12,
        choices=StatusSeparacaoChoices.choices,
        default=StatusSeparacaoChoices.ABERTO,
        db_index=True,
    )
    observacoes = models.TextField(blank=True, null=True)
    enviado_em = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Lote de Remessa"
        verbose_name_plural = "Lotes de Remessa"
        indexes = [
            models.Index(fields=["tipo", "status"]),
        ]

    def __str__(self):
        return f"{self.nome} — {self.fornecedor.nome}"

    @property
    def quantidade_itens(self) -> int:
        return len(self.itens.all())

    @property
    def quantidade_abertos(self) -> int:
        return sum(1 for i in self.itens.all() if i.status == StatusSeparacaoChoices.ABERTO)


class SeparacaoItem(AuditModel):
    """
    Marca um Item como fisicamente separado (aguardando envio ao fornecedor para
    manutenção, ou aguardando devolução por fim de contrato de Locação),
    independente do `Item.status` atual. É uma etapa de organização anterior à
    movimentação real: o despacho (ver `services/separacao_service.py`) sempre
    passa pelo `MovimentacaoEstoqueService`, que aplica as regras de negócio de
    verdade (mudança de status, abertura de Ordem de Manutenção, etc.).

    Só pode existir UM registro `aberto` por item por vez (`uniq_item_separacao_
    aberta`) — itens enviados/cancelados não bloqueiam uma nova separação futura.
    """
    item = models.ForeignKey(
        "Item",
        on_delete=models.CASCADE,
        related_name="separacoes",
        verbose_name="Equipamento",
    )
    tipo = models.CharField(max_length=10, choices=TipoSeparacaoChoices.choices, verbose_name="Tipo")
    lote = models.ForeignKey(
        LoteSeparacao,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="itens",
        verbose_name="Lote de separação",
    )
    fornecedor = models.ForeignKey(
        "Fornecedor",
        on_delete=models.PROTECT,
        related_name="itens_separados",
        verbose_name="Fornecedor",
    )
    status = models.CharField(
        max_length=12,
        choices=StatusSeparacaoChoices.choices,
        default=StatusSeparacaoChoices.ABERTO,
        db_index=True,
    )
    observacoes = models.TextField(blank=True, null=True)
    enviado_em = models.DateTimeField(null=True, blank=True)
    # Movimentação que colocou o item nesta área de separação.
    movimentacao_entrada = models.ForeignKey(
        "MovimentacaoItem",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="+",
        verbose_name="Movimentação de entrada em separação",
    )
    # Movimentação real de despacho (envio_manutencao ou devolucao_locacao),
    # gerada quando o item sai desta área de separação.
    movimentacao_despacho = models.ForeignKey(
        "MovimentacaoItem",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="+",
        verbose_name="Movimentação de despacho",
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Item em Remessa"
        verbose_name_plural = "Itens em Remessa"
        indexes = [
            models.Index(fields=["tipo", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["item"],
                condition=models.Q(status="aberto"),
                name="uniq_item_separacao_aberta",
            )
        ]

    def __str__(self):
        return f"{self.item.nome} — {self.get_tipo_display()} ({self.get_status_display()})"


class SeparacaoItemAnexo(AuditModel):
    """
    Nota fiscal (ou outro documento) anexada a um item em Remessa — espelha
    `OrdemManutencaoAnexo`/`LoteEnvioFornecedorAnexo`. Pode haver mais de uma
    por item (ex.: NF do envio ao fornecedor e, depois, NF da devolução).
    """
    separacao = models.ForeignKey(
        SeparacaoItem,
        on_delete=models.CASCADE,
        related_name="anexos_nf",
        verbose_name="Item em remessa",
    )
    arquivo = models.FileField(upload_to="separacao/nf/%Y/%m/", verbose_name="Arquivo")
    descricao = models.CharField(max_length=200, blank=True, default="", verbose_name="Descrição")

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "Nota Fiscal da Remessa"
        verbose_name_plural = "Notas Fiscais da Remessa"

    def __str__(self):
        return f"NF — {self.separacao.item.nome}"

    @property
    def nome_arquivo(self):
        import os
        return os.path.basename(self.arquivo.name) if self.arquivo else ""


class DocumentoFiscalRemessa(AuditModel):
    """
    Aviso interno de controle (estilo nota fiscal) gerado a partir de itens em
    Remessa, para avisar o setor Fiscal por e-mail que equipamento(s) estão
    indo (Envio) ou voltando (Devolução) ao fornecedor. NÃO é a Nota Fiscal
    Eletrônica real do fornecedor — não tem CFOP/tributos, apenas identifica
    o equipamento (fornecedor, modelo, nº de série) para o controle interno.

    O PDF não é persistido em disco (evita expor pelo /media/, que é servido
    sem autenticação) — é gerado sob demanda a partir de `itens` tanto para o
    e-mail quanto para novo download (ver `services/documento_fiscal_service.py`).
    """
    tipo = models.CharField(max_length=10, choices=TipoSeparacaoChoices.choices, verbose_name="Tipo")
    lote = models.ForeignKey(
        LoteSeparacao,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="documentos_fiscais",
        verbose_name="Lote de origem",
    )
    itens = models.ManyToManyField(
        SeparacaoItem,
        related_name="documentos_fiscais",
        verbose_name="Itens do documento",
    )
    email_enviado = models.BooleanField(default=False, verbose_name="E-mail enviado ao Fiscal")
    destinatarios_envio = models.TextField(
        blank=True, default="",
        verbose_name="Destinatários no momento do envio",
        help_text="Snapshot dos e-mails para quem foi enviado (separados por vírgula).",
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Documento Fiscal de Remessa"
        verbose_name_plural = "Documentos Fiscais de Remessa"

    def __str__(self):
        return f"{self.numero} — {self.get_tipo_display()}"

    @property
    def numero(self) -> str:
        return f"DF-{self.pk:05d}" if self.pk else "DF-#####"


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
    # Âncora da contagem quando o item volta a "ativo": a partir desta data o
    # intervalo recomeça do zero (regra de negócio: item parado não acumula uso).
    data_reativacao = models.DateField(blank=True, null=True, verbose_name="Última reativação do equipamento")

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

    def base_contagem(self):
        """
        Âncora oficial da contagem do intervalo: a data mais recente entre a
        última execução e a última reativação do equipamento. Usada por TODOS
        os cálculos de "próxima preventiva" (telas, dashboard e e-mails) para
        que a contagem recomece quando o item volta a operar.
        """
        datas = [d for d in (self.data_ultima, self.data_reativacao) if d]
        return max(datas) if datas else None

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
        base = self.base_contagem()
        if self.data_agendamento:
            nova = self.data_agendamento
        elif base and dias > 0:
            nova = base + timedelta(days=dias)
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
        base = data_exec or self.base_contagem() or timezone.now().date()
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
        Retoma a contagem quando o equipamento volta ao status 'ativo'.

        Regra de negócio: a contagem RECOMEÇA na reativação — o intervalo
        completo passa a contar a partir de hoje (item parado não acumula uso).
        Um agendamento explícito (data_agendamento) continua tendo prioridade.
        Sem intervalo configurado, usa os dias congelados na pausa como fallback.
        """
        if not self.pausada:
            return
        hoje = timezone.now().date()
        self.pausada = False
        self.data_reativacao = hoje
        dias = self._periodo_referencia()
        if self.data_agendamento:
            self.data_proxima = self.data_agendamento
        elif dias > 0:
            self.data_proxima = hoje + timedelta(days=dias)
        elif self.dias_restantes_pausa is not None:
            self.data_proxima = hoje + timedelta(days=self.dias_restantes_pausa)
        self.dentro_do_prazo = True if not self.data_proxima else hoje <= self.data_proxima
        self.data_pausada = None
        self.dias_restantes_pausa = None
        self.save(update_fields=[
            "pausada", "data_reativacao", "data_pausada", "dias_restantes_pausa",
            "data_proxima", "dentro_do_prazo", "updated_at",
        ])

    @transaction.atomic
    def registrar_execucao(self, respostas_dict: dict, usuario=None, observacao=None, foto_antes=None, foto_depois=None, foto_antes_2=None, foto_depois_2=None, data_execucao=None, hora_inicio=None, hora_fim=None, fotos=None):
        """
        Registra a execução sem sobrescrever históricos anteriores.
        respostas_dict: { pergunta_id: valor_string }

        `fotos`: [(arquivo, momento)] da galeria nova (ver
        services/preventiva_fotos_service.py) — quantas forem necessárias, cada
        uma marcada como "antes", "depois" ou "ambos".

        Os parâmetros `foto_antes`/`foto_depois`/`foto_antes_2`/`foto_depois_2`
        seguem aceitos para não quebrar chamadas existentes: quando vierem, são
        convertidos em itens da galeria. Preferir sempre `fotos`.
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

        # 3) evidências → galeria. Os parâmetros legados de foto única viram
        # itens da galeria, para que exista UMA fonte de verdade de evidência.
        from services import preventiva_fotos_service as _fotos

        lista_fotos = list(fotos or [])
        if not lista_fotos:
            for arquivo, momento in (
                (foto_antes, PreventivaFoto.Momento.ANTES),
                (foto_antes_2, PreventivaFoto.Momento.ANTES),
                (foto_depois, PreventivaFoto.Momento.DEPOIS),
                (foto_depois_2, PreventivaFoto.Momento.DEPOIS),
            ):
                if arquivo:
                    lista_fotos.append((arquivo, momento))
        if lista_fotos:
            # Espelha os campos legados da execução E da preventiva.
            _fotos.adicionar(execucao, lista_fotos, usuario=usuario)

        # 4) atualiza os campos de "última execução" para agenda/relatórios
        self.data_ultima = hoje
        self.data_agendamento = None  # agendamento consumido pela execução
        if observacao:
            self.observacao = observacao

        self.recomputar_prazo(hoje)
        self.save(update_fields=["data_ultima", "data_agendamento", "data_proxima", "dentro_do_prazo", "observacao", "updated_at"])


def sincronizar_preventivas_com_status(item) -> tuple[int, int]:
    """
    Pausa/retoma as preventivas do item conforme o status atual dele.
    Regra de negócio: contagem de preventiva só corre para item ATIVO —
    backup, defeito, manutenção, pausado etc. congelam a contagem, e a
    volta a ativo reinicia o intervalo (ver Preventiva.retomar).
    Retorna (pausadas, retomadas).
    """
    pausante = item.status != StatusItemChoices.ATIVO
    pausadas = retomadas = 0
    for prev in item.preventivas.all():
        if pausante and not prev.pausada:
            prev.pausar()
            pausadas += 1
        elif not pausante and prev.pausada:
            prev.retomar()
            retomadas += 1
    return pausadas, retomadas


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

    # Preventivas realizadas em dupla: o técnico principal registra o
    # formulário e atribui o(s) colega(s) que estiveram com ele no serviço,
    # para que as horas trabalhadas também sejam contabilizadas para eles
    # (mesmo período hora_inicio/hora_fim da execução — trabalho simultâneo).
    tecnicos_auxiliares = models.ManyToManyField(
        User,
        blank=True,
        related_name="execucoes_preventiva_auxiliar",
        verbose_name="Técnicos auxiliares",
        help_text="Outros técnicos que participaram desta execução junto com o responsável.",
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


class PreventivaFoto(AuditModel):
    """
    Galeria de evidências de uma preventiva — quantas fotos forem necessárias,
    cada uma marcada como "antes", "depois" ou valendo para os DOIS momentos.

    Substitui os 4 campos fixos (`foto_antes`, `foto_depois`, `foto_antes_2`,
    `foto_depois_2`) que existiam em `Preventiva` e em `PreventivaExecucao`.
    Aqueles campos continuam no banco de propósito (ver migration
    0166): são a rede de segurança do histórico já registrado, e a migração de
    dados apenas COPIA o caminho do arquivo para cá, sem mover nem reprocessar
    nenhuma imagem — os arquivos em `MEDIA_ROOT` ficam exatamente onde estão.

    `momento = AMBOS` é o caso que motivou o modelo: numa preventiva de veículo
    uma mesma foto costuma valer como evidência de antes e de depois, e obrigar
    o técnico a enviar o mesmo arquivo duas vezes só duplicava armazenamento.
    """

    class Momento(models.TextChoices):
        ANTES  = "antes",  "Antes"
        DEPOIS = "depois", "Depois"
        AMBOS  = "ambos",  "Antes e depois"

    # A preventiva é SEMPRE preenchida; a execução é opcional porque as fotos
    # legadas do snapshot de `Preventiva` são anteriores ao próprio model
    # PreventivaExecucao e não têm execução a que se vincular. Sem isso, essas
    # evidências antigas se perderiam na migração.
    preventiva = models.ForeignKey(
        Preventiva, on_delete=models.CASCADE, related_name="fotos",
        verbose_name="Preventiva",
    )
    execucao = models.ForeignKey(
        PreventivaExecucao, on_delete=models.CASCADE, related_name="fotos",
        null=True, blank=True, verbose_name="Execução",
        help_text="Vazio nas evidências anteriores ao histórico de execuções.",
    )

    imagem = models.ImageField(upload_to="preventivas/%Y/%m/", verbose_name="Imagem")
    momento = models.CharField(
        max_length=10, choices=Momento.choices, default=Momento.ANTES,
        db_index=True, verbose_name="Momento",
    )
    legenda = models.CharField(
        max_length=200, blank=True, default="", verbose_name="Legenda",
        help_text="Descrição opcional do que a foto mostra.",
    )
    ordem = models.PositiveIntegerField(default=0, verbose_name="Ordem")

    # Preenchido só nas fotos criadas pela migração de dados, com o nome do
    # campo de origem (ex.: "execucao.foto_antes_2"). Serve para auditar o que
    # veio do modelo antigo e para a migração ser reversível sem adivinhação.
    origem_legado = models.CharField(
        max_length=40, blank=True, default="", db_index=True,
        verbose_name="Origem (campo legado)",
    )

    class Meta:
        ordering = ["ordem", "id"]
        verbose_name = "Foto de Preventiva"
        verbose_name_plural = "Fotos de Preventiva"
        indexes = [
            models.Index(fields=["preventiva", "momento"]),
            models.Index(fields=["execucao", "ordem"]),
        ]

    def __str__(self):
        return f"{self.get_momento_display()} — {self.preventiva.equipamento.nome}"

    @property
    def vale_para_antes(self) -> bool:
        return self.momento in (self.Momento.ANTES, self.Momento.AMBOS)

    @property
    def vale_para_depois(self) -> bool:
        return self.momento in (self.Momento.DEPOIS, self.Momento.AMBOS)


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


# ----------------- META DE PREVENTIVAS POR ÁREA -----------------
class PreventivaAreaResponsavel(AuditModel):
    """
    Técnico responsável pelas preventivas de uma área de operação.

    É cadastro de RESPONSABILIDADE, não de atribuição: quem executa continua
    gravado em `Preventiva.tecnico` (agendamento) e em
    `PreventivaExecucao.tecnico` (execução). Esta tabela responde "de quem é
    este parque", e o módulo a usa em dois lugares:

      1. o Plano de Agendamento pré-seleciona este técnico ao agendar
         equipamento da área (sem sobrescrever atribuição já feita à mão);
      2. a apuração da meta credita a PENDÊNCIA a este técnico quando a
         preventiva ainda não tem um responsável próprio.

    `subtipo` nulo é o responsável PADRÃO da área — vale para todo subtipo
    que não tenha linha específica. É o que permite começar com "switches do
    produtivo são do Vinicius" e depois destacar os access points sem mexer
    no que já existe.
    """
    area = models.CharField(
        max_length=15,
        choices=AreaOperacionalChoices.choices,
        verbose_name="Área de operação",
    )
    subtipo = models.ForeignKey(
        Subtipo,
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="responsaveis_preventiva",
        verbose_name="Subtipo de equipamento",
        help_text="Vazio = responsável padrão da área (vale para todos os subtipos).",
    )
    tecnico = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="areas_preventiva",
        verbose_name="Técnico responsável",
    )
    ativo = models.BooleanField(default=True, verbose_name="Ativo")
    observacao = models.TextField(blank=True, null=True)

    class Meta:
        verbose_name = "Responsável por Área (Preventivas)"
        verbose_name_plural = "Responsáveis por Área (Preventivas)"
        ordering = ["area", "subtipo__nome"]
        constraints = [
            # Uma linha por (área, subtipo). As duas restrições existem porque
            # o SQLite (e o Postgres) tratam NULL como valor DISTINTO num
            # UNIQUE composto: sem a segunda, daria para cadastrar dois
            # responsáveis padrão para a mesma área.
            models.UniqueConstraint(
                fields=["area", "subtipo"],
                condition=models.Q(subtipo__isnull=False),
                name="uniq_resp_area_subtipo",
            ),
            models.UniqueConstraint(
                fields=["area"],
                condition=models.Q(subtipo__isnull=True),
                name="uniq_resp_area_padrao",
            ),
        ]

    def __str__(self):
        escopo = self.subtipo.nome if self.subtipo else "todos os subtipos"
        nome = self.tecnico.get_full_name() or self.tecnico.username
        return f"{self.get_area_display()} · {escopo} → {nome}"


class PreventivaCicloMeta(AuditModel):
    """
    Ciclo de referência da meta de manutenção preventiva.

    Um ciclo é uma JANELA DE TEMPO (90 dias para switches) dentro da qual todo
    equipamento elegível do subtipo deve receber uma preventiva. A meta não é
    "cada equipamento a cada 90 dias" (isso é `Preventiva.data_proxima`, que
    continua valendo por ativo): é um prazo COMUM, o mesmo para a área inteira,
    para que exista uma data em que a meta do técnico fecha e um número que o
    gestor possa comparar entre ciclos.

    O ciclo não guarda resultado nenhum. Previstos, realizados e pendentes são
    apurados na hora a partir de `PreventivaExecucao` por
    `services/preventiva_meta_service.py` — gravar o placar criaria uma segunda
    verdade que envelhece sozinha quando alguém corrige uma execução.
    """
    DURACAO_PADRAO = 90

    subtipo = models.ForeignKey(
        Subtipo,
        on_delete=models.PROTECT,
        related_name="ciclos_meta",
        verbose_name="Subtipo de equipamento",
        help_text="Subtipo cujo parque este ciclo cobre (ex.: Switches).",
    )
    data_inicio = models.DateField(verbose_name="Início do ciclo")
    duracao_dias = models.PositiveIntegerField(
        default=DURACAO_PADRAO,
        validators=[MinValueValidator(1)],
        verbose_name="Duração (dias)",
        help_text="Ciclo de referência inicial dos switches: 90 dias.",
    )
    encerrado = models.BooleanField(
        default=False,
        verbose_name="Encerrado manualmente",
        help_text="Marca o ciclo como fechado antes do prazo (não altera a apuração do período).",
    )
    observacao = models.TextField(blank=True, null=True)

    class Meta:
        verbose_name = "Ciclo de Meta (Preventivas)"
        verbose_name_plural = "Ciclos de Meta (Preventivas)"
        ordering = ["-data_inicio", "subtipo__nome"]
        constraints = [
            models.UniqueConstraint(
                fields=["subtipo", "data_inicio"],
                name="uniq_ciclo_subtipo_inicio",
            ),
        ]
        indexes = [
            models.Index(fields=["subtipo", "data_inicio"]),
        ]

    def __str__(self):
        return f"{self.subtipo.nome} · {self.data_inicio:%d/%m/%Y} a {self.data_fim:%d/%m/%Y}"

    # ── Janela ────────────────────────────────────────────────────────────
    @property
    def data_fim(self):
        """Último dia do ciclo (INCLUSIVE). 90 dias = início + 89."""
        return self.data_inicio + timedelta(days=int(self.duracao_dias) - 1)

    def contem(self, dia) -> bool:
        return bool(dia) and self.data_inicio <= dia <= self.data_fim

    @property
    def contem_hoje(self) -> bool:
        """Para o template marcar o ciclo atual no seletor (o `{% if %}` do
        Django não chama método com argumento)."""
        return self.contem(timezone.localdate())

    def dias_decorridos(self, hoje=None) -> int:
        """Dias já vividos do ciclo, de 0 (não começou) até `duracao_dias`."""
        hoje = hoje or timezone.localdate()
        if hoje < self.data_inicio:
            return 0
        return min((hoje - self.data_inicio).days + 1, int(self.duracao_dias))

    def dias_restantes(self, hoje=None) -> int:
        """Dias que ainda restam, contando hoje. 0 quando o prazo já passou."""
        hoje = hoje or timezone.localdate()
        if hoje > self.data_fim:
            return 0
        if hoje < self.data_inicio:
            return int(self.duracao_dias)
        return (self.data_fim - hoje).days + 1

    def pct_tempo(self, hoje=None) -> int:
        """Quanto do prazo já passou, em %. É a régua contra a qual a execução é lida."""
        total = int(self.duracao_dias) or 1
        return min(100, round(self.dias_decorridos(hoje) / total * 100))

    def situacao(self, hoje=None) -> str:
        """`futuro` | `vigente` | `encerrado` — estado do ciclo na linha do tempo."""
        hoje = hoje or timezone.localdate()
        if self.encerrado or hoje > self.data_fim:
            return "encerrado"
        if hoje < self.data_inicio:
            return "futuro"
        return "vigente"

    def clean(self):
        super().clean()
        if not self.data_inicio or not self.subtipo_id or not self.duracao_dias:
            return
        # Ciclos do mesmo subtipo não podem se sobrepor: um equipamento
        # executado num dia coberto por dois ciclos contaria duas vezes, e as
        # duas apurações divergiriam da outra sem ninguém saber qual está certa.
        conflito = (
            PreventivaCicloMeta.objects
            .filter(subtipo_id=self.subtipo_id)
            .exclude(pk=self.pk)
        )
        for outro in conflito:
            if self.data_inicio <= outro.data_fim and outro.data_inicio <= self.data_fim:
                raise ValidationError({
                    "data_inicio": (
                        f"Este período se sobrepõe ao ciclo de "
                        f"{outro.data_inicio:%d/%m/%Y} a {outro.data_fim:%d/%m/%Y}. "
                        f"Ajuste a data de início ou a duração."
                    )
                })


class PeriodicidadeChoices(models.TextChoices):
    """
    Ciclo de cobranca do lote. O rateio para custo mensal/anual mora em
    `services/licenca_service._MESES_POR_CICLO` — **a lista de opcoes aqui e a
    tabela de divisores ali tem de andar juntas**.

    Ja nao andaram: a tabela mapeava uma chave `trimestral` que NAO era opcao
    deste enum e nao tinha `trienal` nem `contrato`, que eram. Um lote Trienal
    caia no fallback de 1 mes e tinha o custo mensal publicado **36x maior** do
    que o real. Nenhum lote da base estava nesses ciclos, entao o defeito nunca
    apareceu — mas o maior contrato do acervo passa de R$ 1 mi por ciclo, e ali
    o divisor errado nao e centavo, e ordem de grandeza.
    """
    MENSAL = "mensal", _("Mensal")
    TRIMESTRAL = "trimestral", _("Trimestral")
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

    # ── Renovação ────────────────────────────────────────────────────────
    # O vencimento é DERIVADO de `data_compra` + ciclo quando este campo está
    # vazio, e é o campo que vale quando está preenchido. Os dois existem
    # porque contrato de verdade quase nunca renova exatamente no aniversário
    # da compra: assina-se no dia 12, o faturamento vira no dia 1º, e a
    # diferença é justamente a janela em que a renovação é decidida.
    #
    # Derivar sem campo deixaria o TI sem como corrigir a data real; só o
    # campo deixaria 47 lotes já cadastrados sem vencimento nenhum. Com os
    # dois, o acervo inteiro ganha uma data no mesmo dia e quem souber a data
    # certa pode gravá-la.
    data_renovacao = models.DateField(
        null=True, blank=True, verbose_name="Próxima renovação",
        help_text="Deixe em branco para o sistema calcular a partir da data de "
                  "compra e da periodicidade. Preencha quando o contrato tiver "
                  "data de renovação própria.",
    )
    renovacao_automatica = models.BooleanField(
        default=True, verbose_name="Renova automaticamente",
        help_text="Desmarque para contrato que exige renovação manual — o aviso "
                  "de vencimento passa a ser tratado como ação obrigatória, não "
                  "como informação.",
    )

    numero_pedido = models.CharField(max_length=50, null=True, blank=True)
    fornecedor = models.ForeignKey("Fornecedor", on_delete=models.SET_NULL, null=True, blank=True)
    centro_custo = models.ForeignKey("CentroCusto", on_delete=models.SET_NULL, null=True, blank=True)
    observacao = models.TextField(blank=True, null=True)

    class Meta:
        ordering = ["-data_compra", "-id"]
        verbose_name = "Lote de Licença"
        indexes = [
            # A tela de renovações filtra por janela de vencimento e o alerta
            # por e-mail roda a mesma pergunta todo dia.
            models.Index(fields=["data_renovacao"], name="licencalote_renov_idx"),
            models.Index(fields=["licenca", "data_compra"], name="licencalote_lic_compra_idx"),
        ]

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


class StatusLicencaOfficeChoices(models.TextChoices):
    OK = "ok", _("OK")
    PROBLEMA = "problema", _("Com Problema")
    EXPIRADA = "expirada", _("Expirada")
    CANCELADA = "cancelada", _("Cancelada")


# --- LICENÇA POR EQUIPAMENTO (chave única, ex.: Office OEM/Retail) ---
class LicencaOffice(AuditModel):
    """
    Controle de licença com CHAVE ÚNICA por equipamento (ex.: Office
    Home/Business vendido preso a uma máquina) — diferente do módulo
    `Licenca`/`LicencaLote` acima, que é um POOL de assentos por nome de
    software alocado a colaboradores. Aqui cada registro é uma chave/serial
    real, e o vínculo relevante é com o `Item` (notebook/desktop), não com o
    `Usuario`. Editável manualmente ou importada de planilha (ver
    `services/licenca_office_import_service.py`); aparece no detalhe do
    equipamento vinculado.
    """
    produto = models.CharField(
        max_length=160, verbose_name="Produto",
        help_text="Ex.: Office Home and Business 2016",
    )
    chave_produto = models.CharField(
        max_length=100, db_index=True, verbose_name="Chave / Serial",
    )
    status = models.CharField(
        max_length=20, choices=StatusLicencaOfficeChoices.choices,
        default=StatusLicencaOfficeChoices.OK, verbose_name="Status",
    )
    conta_vinculada = models.CharField(
        max_length=150, blank=True, verbose_name="Conta / E-mail de ativação",
    )
    id_dell = models.CharField(
        max_length=60, blank=True, verbose_name="ID Principal Dell",
        help_text="Service Tag / identificador do fabricante, quando aplicável.",
    )
    data_licenca = models.DateField(null=True, blank=True, verbose_name="Data da Licença")
    estacao_importada = models.CharField(
        max_length=100, blank=True, verbose_name="Estação (planilha)",
        help_text="Nome da estação como veio da planilha de origem — usado para localizar o "
                   "equipamento na importação. O vínculo oficial é o campo 'Equipamento' abaixo.",
    )
    usuario_vinculado = models.CharField(
        max_length=100, blank=True, verbose_name="Usuário vinculado à máquina",
    )
    item = models.OneToOneField(
        "Item", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="licenca_office", verbose_name="Equipamento vinculado",
        help_text="Notebook/desktop do estoque a que esta licença pertence. Deixe em branco "
                   "para manter a licença sem uso (em estoque).",
    )
    observacao = models.TextField(blank=True, null=True)

    class Meta:
        ordering = ["produto", "chave_produto"]
        verbose_name = "Licença Office (por equipamento)"
        verbose_name_plural = "Licenças Office (por equipamento)"
        indexes = [
            models.Index(fields=["chave_produto"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self):
        return f"{self.produto} — {self.chave_produto}"


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
    # A licença do NinjaOne se cobra por dispositivo, e a máquina parada (backup, defeito, manutenção, pausado) segue
    # contando até alguém removê-la LÁ. A importação/sincronização nunca apaga um dispositivo daqui (regra 59-d), então
    # "sumiu do NinjaOne" não é detectável; quem tira a licença dá o recado aqui, e a lista a liberar deixa de cobrá-lo
    # (`services/ninja_licenca_service`, regra 69). Se o aparelho voltar a falar com o NinjaOne DEPOIS desta data, a
    # remoção não pegou e ele volta à lista.
    licenca_liberada_em = models.DateTimeField(
        null=True, blank=True, db_index=True, verbose_name="Licença removida do NinjaOne em",
        help_text="Quando o TI removeu este dispositivo do NinjaOne para liberar a licença. Vazio = ainda não removida.",
    )
    licenca_liberada_por = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="ninja_licencas_liberadas",
        verbose_name="Removida por",
    )

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


class NinjaConexao(models.Model):
    """A conexão OAuth do sistema com a API do NinjaOne — uma linha só (`get()`).

    Os tokens NUNCA ficam em claro: `access_cifrado` e `refresh_cifrado` são Fernet
    (chave `NINJA_CHAVE_CIFRA`, no .env) e a tela só mostra o estado. Quem cifra e
    decifra é `services/ninja_api_service.py`; nada mais lê estes campos.

    `sincronizando_desde` é a trava contra duas sincronizações ao mesmo tempo (botão
    + tarefa agendada, ou duplo clique): o `UPDATE` condicional é atômico no banco,
    então vale entre processos — um `cache.add` só valeria dentro do servidor web.
    """

    access_cifrado = models.TextField(blank=True, default="")
    refresh_cifrado = models.TextField(blank=True, default="")
    expira_em = models.DateTimeField(null=True, blank=True, verbose_name="Access token expira em")
    escopo = models.CharField(max_length=255, blank=True, default="")
    conectado_por = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
        verbose_name="Conectado por",
    )
    conectado_em = models.DateTimeField(null=True, blank=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    sincronizando_desde = models.DateTimeField(null=True, blank=True)
    ultima_sincronizacao_em = models.DateTimeField(null=True, blank=True)
    ultimo_erro = models.TextField(blank=True, default="")
    ultimo_erro_em = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Conexão NinjaOne"
        verbose_name_plural = "Conexão NinjaOne"

    def __str__(self):
        return "Conexão NinjaOne"

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def conectada(self):
        return bool(self.refresh_cifrado or self.access_cifrado)


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
    # Recuperacao de senha por link (services/recuperacao_senha_service.py).
    # PEDIDO entra na trilha mesmo quando o identificador nao resolve em
    # ninguem — pedido "vazio" repetido e justamente o sinal de sondagem.
    SENHA_RESET_PEDIDO = "senha_reset_pedido", "Recuperação de senha solicitada"
    SENHA_RESET_OK     = "senha_reset_ok", "Senha redefinida por link"


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

    class AtualizacaoStatus(models.TextChoices):
        EM_DIA     = 'em_dia', 'Em dia'
        BAIXANDO   = 'baixando', 'Baixando'
        INSTALANDO = 'instalando', 'Instalando'
        BLOQUEADA  = 'bloqueada', 'Bloqueada'

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
    # versionCode do app rodando agora no aparelho — reportado em TODO check-in (ao contrário do
    # app_versao do /enroll/, que fica congelado na versão da 1ª matrícula). Numérico e sempre
    # crescente: melhor campo para comparar contra `atualizacao.version_code` (ver
    # services/quiosque_service.py) e montar o selo de conformidade por aparelho no painel.
    app_versao_codigo   = models.PositiveIntegerField(null=True, blank=True, verbose_name='Código da versão do app')
    atualizacao_status  = models.CharField(max_length=12, choices=AtualizacaoStatus.choices, blank=True, default='', verbose_name='Status da auto-atualização')
    atualizacao_motivo  = models.CharField(max_length=255, blank=True, default='', verbose_name='Motivo do bloqueio da atualização')
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

    # ── Telemetria de sinal Wi-Fi (opt-in por aparelho, v1.6.0+) ──
    telemetria_wifi = models.BooleanField(default=False, verbose_name='Telemetria de sinal Wi-Fi')

    # ── Telemetria de rede móvel (opt-in por aparelho, v1.10.0+) — geração
    # (3G/4G/5G), operadora e força do sinal do chip. Separada de
    # telemetria_wifi porque no Android exige READ_PHONE_STATE (permissão
    # sensível) e só faz sentido em aparelho COM chip; padrão SEGURO = False,
    # mesmo critério dos demais gates. Ver INFORME_SERVIDOR_TELEMETRIA_REDE_MOVEL.md.
    telemetria_movel = models.BooleanField(default=False, verbose_name='Telemetria de rede móvel (3G/4G/5G)')

    # ── Gates locais da tela "Gerência do TI" no aparelho (v1.7.1+) — são
    # PERMISSÕES, não comandos remotos: o servidor só libera/bloqueia a ação no
    # próprio aparelho; quem aciona é sempre alguém na frente do celular (tela já
    # protegida por PIN). Padrão SEGURO = False (nenhum aparelho ganha uma ação
    # disruptiva nova por omissão — mesmo cuidado de telemetria_wifi). Ver
    # INFORME_SERVIDOR_CACHE_E_ENERGIA.md.
    permite_reiniciar = models.BooleanField(
        default=False, verbose_name='Permite reiniciar (botão no app)',
    )
    # Best-effort: Android não tem API pública de Device Owner para desligar por
    # completo (exige permissão de assinatura android.permission.SHUTDOWN) — na
    # maioria dos aparelhos de fábrica sem root a tentativa via app falha com
    # SecurityException. Ligar este campo (OU permite_reiniciar) TAMBÉM libera o
    # menu nativo do botão físico de energia (Reiniciar/Desligar juntos — o
    # Android não separa os dois nesse menu); esse caminho funciona de verdade.
    permite_desligar = models.BooleanField(
        default=False, verbose_name='Permite desligar (botão no app, best-effort)',
    )
    # Quando True, o app limpa o PRÓPRIO cache sozinho a cada aplicação de
    # políticas (boot/retomada do quiosque) — sem o TI precisar abrir a tela.
    # Não afeta outros apps instalados (sem API pública para isso — ver informe §3).
    limpeza_cache_automatica = models.BooleanField(
        default=False, verbose_name='Limpeza automática de cache do app',
    )
    # Libera "Forçar parada"/"Limpar armazenamento"/"Limpar cache" na tela nativa
    # "Informações do app" (aberta via toque longo num ícone do grid do quiosque) para
    # QUALQUER app liberado (apps_permitidos). O Android não deixa separar as 3 ações —
    # é uma única restrição (DISALLOW_APPS_CONTROL), tudo ou nada. "Limpar
    # armazenamento" apaga TODOS os dados do app (login, config etc.), não só o cache.
    # Ver INFORME_SERVIDOR_CACHE_E_ENERGIA.md §7.
    permite_limpar_apps_terceiros = models.BooleanField(
        default=False, verbose_name='Permite limpar cache/dados dos apps liberados (tela nativa)',
    )
    # Libera a barra de status + bandeja de notificações dentro do Lock Task
    # (LOCK_TASK_FEATURE_NOTIFICATIONS + LOCK_TASK_FEATURE_SYSTEM_INFO, v1.9.0+)
    # para o colaborador ver/receber notificação de WhatsApp Business, Outlook,
    # Gmail e chamada do telefone. Só abre a "porta": o app de origem também
    # precisa estar em apps_permitidos, senão fica suspenso e nunca notifica
    # nada. Padrão SEGURO = False (mesmo cuidado dos outros gates acima). Ver
    # INFORME_SERVIDOR_NOTIFICACOES.md.
    permite_notificacoes = models.BooleanField(
        default=False, verbose_name='Permite notificações (barra de status + bandeja)',
    )

    # ── Cache/dados do PRÓPRIO app Quiosque (snapshot do último check-in — mesmo
    # padrão de ram_total_mb/armazenamento_total_mb: vêm em TODO check-in, então
    # não valem como histórico linha a linha no KioskCheckin). Não inclui outros
    # apps instalados (ver INFORME_SERVIDOR_CACHE_E_ENERGIA.md §3).
    cache_app_mb = models.PositiveIntegerField(null=True, blank=True, verbose_name='Cache do app (MB)')
    dados_app_mb = models.PositiveIntegerField(null=True, blank=True, verbose_name='Dados do app (MB)')

    # ── Rede Wi-Fi provisionada pelo servidor (v1.7.0+) — o app cria/assume a
    # rede como dona, com MAC aleatório desligado desde a criação. wifi_senha
    # fica em texto puro (ao contrário de admin_pin_hash) porque o app PRECISA
    # do valor real para configurar a rede no Android — não há como recuperar
    # de um hash. SSID vazio = sem rede provisionada configurada para o device.
    wifi_ssid  = models.CharField(max_length=64, blank=True, default='', verbose_name='SSID da rede provisionada')
    wifi_senha = models.CharField(max_length=128, blank=True, default='', verbose_name='Senha da rede provisionada')

    # ── Verificação de MAC (v1.7.0+) — snapshot do último check-in (mesmo
    # padrão de atualizacao_status/atualizacao_motivo): texto descritivo, não
    # histórico linha a linha. `ultima_mac_em_uso` comparado a `mac` é o que
    # confirma se o MAC de fábrica realmente está em uso na conexão atual.
    ultima_mac_em_uso      = models.CharField(max_length=17, null=True, blank=True, verbose_name='MAC em uso (última leitura)')
    wifi_mac_politica      = models.CharField(max_length=255, blank=True, default='', verbose_name='Política de MAC (última leitura)')
    wifi_rede_provisionada = models.CharField(max_length=255, blank=True, default='', verbose_name='Provisionamento de rede (última leitura)')

    # ── Inventário de apps do aparelho (recebido no check-in só quando muda) ──
    # apps_hash = impressão digital da lista (dedup); apps_atualizado_em = quando o
    # inventário foi substituído pela última vez. A lista em si fica em KioskDeviceApp.
    apps_hash          = models.CharField(max_length=64, blank=True, default='', verbose_name='Hash do inventário de apps')
    apps_atualizado_em = models.DateTimeField(null=True, blank=True, verbose_name='Inventário de apps atualizado em')
    # Apps liberados abertos pelo Zelo e ainda não fechados (package names, do mais
    # recente para o mais antigo) — snapshot do último check-in (v1.8.0+). Base do
    # "Apps em uso" no painel e do comando `fechar_apps`.
    apps_abertos       = models.JSONField(default=list, blank=True, verbose_name='Apps abertos (último check-in)')

    # ── Estado mais recente (atualizado a cada check-in) ──
    ultima_latitude   = models.FloatField(null=True, blank=True)
    ultima_longitude  = models.FloatField(null=True, blank=True)
    ultima_precisao_m = models.FloatField(null=True, blank=True)
    ultima_bateria    = models.IntegerField(null=True, blank=True)
    ultima_rede       = models.CharField(max_length=20, blank=True, default='')
    ultimo_checkin    = models.DateTimeField(null=True, blank=True)

    # ── Conectividade do último check-in (snapshot — mesmo padrão de
    # ultima_rede/ultima_bateria). Alimenta o selo de sinal em CIMA do ponto no
    # mapa da frota (/quiosque/mapa/), que mostra a última posição conhecida de
    # cada aparelho: sem snapshot aqui seria 1 query de último check-in por
    # aparelho (N+1 na tela de mapa).
    #   ultima_rede_tipo  = transporte normalizado (wifi/movel/ethernet/nenhuma)
    #   ultimo_sinal_*    = força do sinal DO TRANSPORTE EM USO (Wi-Fi ou chip),
    #                       já unificada para o selo não precisar saber qual é.
    ultima_rede_tipo        = models.CharField(max_length=12, blank=True, default='', verbose_name='Tipo de rede (última leitura)')
    ultima_movel_geracao    = models.CharField(max_length=6, blank=True, default='', verbose_name='Geração móvel (última leitura)')
    ultima_movel_operadora  = models.CharField(max_length=40, blank=True, default='', verbose_name='Operadora (última leitura)')
    ultimo_sinal_nivel      = models.IntegerField(null=True, blank=True, verbose_name='Nível de sinal 0-4 (última leitura)')
    ultimo_sinal_dbm        = models.IntegerField(null=True, blank=True, verbose_name='Sinal em dBm (última leitura)')

    # ── Memória e armazenamento (snapshot do último check-in — não é histórico;
    # o app manda esses 7 campos em TODO check-in, a cada ~5s, então guardar linha
    # por linha infla demais o KioskCheckin). Ver INFORME_SERVIDOR_MEMORIA_DISCO.
    ram_total_mb            = models.PositiveIntegerField(null=True, blank=True, verbose_name='RAM total (MB)')
    ram_livre_mb            = models.PositiveIntegerField(null=True, blank=True, verbose_name='RAM livre (MB)')
    ram_usada_mb            = models.PositiveIntegerField(null=True, blank=True, verbose_name='RAM usada (MB)')
    ram_pouca               = models.BooleanField(default=False, verbose_name='Pouca RAM (sinalizado pelo Android)')
    armazenamento_total_mb  = models.PositiveIntegerField(null=True, blank=True, verbose_name='Armazenamento total (MB)')
    armazenamento_livre_mb  = models.PositiveIntegerField(null=True, blank=True, verbose_name='Armazenamento livre (MB)')
    armazenamento_usado_mb  = models.PositiveIntegerField(null=True, blank=True, verbose_name='Armazenamento usado (MB)')

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
    mac_em_uso   = models.CharField(max_length=17, null=True, blank=True, verbose_name='MAC em uso')  # MAC visto pelo roteador nesta conexão; comparar com device.mac indica se o MAC aleatório ainda está ativo
    online       = models.BooleanField(default=True)

    # ── Telemetria de sinal Wi-Fi (opt-in — só vem quando device.telemetria_wifi
    # está ligada). Fica no check-in (histórico), não no device, porque o
    # diagnóstico de instabilidade exige ver a série ao longo do tempo, não só
    # o último valor (ver INFORME_SERVIDOR_WIFI_TELEMETRIA §2.2).
    wifi_rssi_dbm         = models.IntegerField(null=True, blank=True, verbose_name='RSSI Wi-Fi (dBm)')
    wifi_nivel            = models.IntegerField(null=True, blank=True, verbose_name='Nível de sinal (0-4)')
    wifi_velocidade_mbps  = models.IntegerField(null=True, blank=True, verbose_name='Velocidade do link (Mbps)')
    wifi_frequencia_mhz   = models.IntegerField(null=True, blank=True, verbose_name='Frequência do canal (MHz)')
    wifi_banda_ghz        = models.CharField(max_length=4, null=True, blank=True, verbose_name='Banda (GHz)')

    # ── Telemetria de rede móvel (opt-in — só vem quando device.telemetria_movel
    # está ligada; v1.10.0+). Fica no check-in, e não só no device, porque o mapa
    # de rota precisa do sinal NO PONTO onde o aparelho estava — é isso que
    # permite ver em que trecho da fazenda o 4G cai. Ver
    # INFORME_SERVIDOR_TELEMETRIA_REDE_MOVEL.md.
    #   rede_tipo        = transporte normalizado no instante da coleta. Derivado
    #                      de `rede` quando o app não manda explícito, para que
    #                      TODA linha (inclusive as já gravadas) tenha um valor
    #                      consultável sem reinterpretar texto livre na leitura.
    #   movel_tecnologia = valor cru do Android (LTE, NR_NSA, HSPA+…) — auditoria;
    #                      `movel_geracao` é o derivado que a UI exibe.
    rede_tipo        = models.CharField(max_length=12, blank=True, default='', db_index=True, verbose_name='Tipo de rede')
    movel_geracao    = models.CharField(max_length=6, blank=True, default='', verbose_name='Geração móvel (2g/3g/4g/5g)')
    movel_tecnologia = models.CharField(max_length=24, blank=True, default='', verbose_name='Tecnologia móvel (crua)')
    movel_operadora  = models.CharField(max_length=40, blank=True, default='', verbose_name='Operadora')
    movel_rssi_dbm   = models.IntegerField(null=True, blank=True, verbose_name='Sinal móvel (dBm)')
    movel_nivel      = models.IntegerField(null=True, blank=True, verbose_name='Nível de sinal móvel (0-4)')

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
    """Comando remoto enviado pelo TI ao device (app v1.8.0+ executa e confirma).

    Vai no `comandos` de TODA resposta do /checkin/ enquanto o aparelho não
    confirmar (reentrega até o ACK — sobrevive à perda de uma resposta de rede);
    o app executa cada `id` no máximo uma vez. Ver
    INFORME_SERVIDOR_COMANDOS_REMOTOS.md e services/quiosque_service.py.
    """
    class Tipo(models.TextChoices):
        SINCRONIZAR_CONFIG  = 'sincronizar_config', 'Sincronizar configuração'
        REENVIAR_INVENTARIO = 'reenviar_inventario', 'Reenviar inventário de apps'
        EXIBIR_MENSAGEM     = 'exibir_mensagem', 'Exibir mensagem'
        FECHAR_APPS         = 'fechar_apps', 'Fechar apps'
        LIMPAR_CACHE        = 'limpar_cache', 'Limpar cache do app'
        BLOQUEAR_TELA       = 'bloquear_tela', 'Bloquear tela'
        DESBLOQUEAR         = 'desbloquear', 'Desbloquear tela'
        REINICIAR           = 'reiniciar', 'Reiniciar aparelho'
        REINICIAR_APP       = 'reiniciar_app', 'Reiniciar aplicativo'
        REATIVAR_QUIOSQUE   = 'reativar_quiosque', 'Reativar quiosque'
        SAIR_QUIOSQUE       = 'sair_quiosque', 'Sair do quiosque'
        # Legados de verdade: valores descartados antes da v1.7.2, substituídos
        # por OUTRO `tipo` (nunca reentregues; só aparecem no histórico já
        # existente — o painel não os oferece mais, ver quiosque_service.COMANDOS).
        # `desbloquear` e `reiniciar_app` NÃO entram aqui: são os valores atuais
        # e definitivos desde a v1.8.2 (ver INFORME_SERVIDOR_COMANDOS_REMOTOS.md),
        # não sinônimos de outra coisa — ficaram marcados como legado por engano
        # numa build anterior deste model.
        BLOQUEAR_LEGADO         = 'bloquear', 'Bloquear dispositivo (legado)'
        MENSAGEM_LEGADO         = 'mensagem', 'Exibir mensagem (legado)'
        ATUALIZAR_CONFIG_LEGADO = 'atualizar_config', 'Atualizar configuração (legado)'

    class Status(models.TextChoices):
        PENDENTE      = 'pendente', 'Pendente'
        # Já foi em algum check-in; continua sendo reentregue até o ACK chegar.
        ENTREGUE      = 'entregue', 'Entregue'
        EXECUTADO     = 'executado', 'Executado'
        FALHOU        = 'falhou', 'Falhou'
        NAO_SUPORTADO = 'nao_suportado', 'Não suportado'
        EXPIRADO      = 'expirado', 'Expirado'

    ABERTOS = (Status.PENDENTE, Status.ENTREGUE)

    device      = models.ForeignKey(KioskDevice, on_delete=models.CASCADE, related_name='comandos')
    tipo        = models.CharField(max_length=40, choices=Tipo.choices)
    payload     = models.JSONField(default=dict, blank=True)
    status      = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDENTE, db_index=True)
    detalhe     = models.TextField(blank=True, default='')
    criado_por  = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='kiosk_comandos', verbose_name='Solicitado por')
    criado_em   = models.DateTimeField(auto_now_add=True)
    # Passado desse horário o app não executa (confirma `expirado`) e o servidor
    # para de reentregar. Todo comando criado pelo painel nasce com validade.
    expira_em   = models.DateTimeField(null=True, blank=True, verbose_name='Expira em')
    entregue_em = models.DateTimeField(null=True, blank=True)  # 1ª entrega (não muda nas reentregas)
    executado_em = models.DateTimeField(null=True, blank=True, verbose_name='Executado em (no aparelho)')
    finalizado_em = models.DateTimeField(null=True, blank=True)  # chegada do ACK (ou expiração no servidor)
    app_versao  = models.CharField(max_length=20, blank=True, default='', verbose_name='Versão do app no ACK')

    class Meta:
        ordering = ['-criado_em']
        verbose_name = 'Comando de Quiosque'
        verbose_name_plural = 'Comandos de Quiosque'
        indexes = [models.Index(fields=['device', 'status'])]
        permissions = [
            ('reiniciar_quiosque_remoto', 'Pode reiniciar aparelhos do quiosque remotamente'),
            # sair_quiosque é o comando mais sensível do canal: entrega o
            # aparelho para uso normal do Android, fora do quiosque, para quem
            # estiver na frente dele naquele momento, sem PIN nenhum. Ver
            # INFORME_SERVIDOR_COMANDOS_REMOTOS.md §2.
            ('sair_quiosque_remoto', 'Pode tirar aparelhos do quiosque remotamente'),
        ]

    def __str__(self):
        return f"{self.get_tipo_display()} → {self.device} ({self.status})"

    @property
    def aberto(self) -> bool:
        return self.status in self.ABERTOS

    @property
    def vencido(self) -> bool:
        """Aberto e fora da validade — o servidor só o fecha no próximo check-in
        do aparelho; até lá o painel já o mostra como expirado."""
        return self.aberto and self.expira_em is not None and self.expira_em < timezone.now()


class KioskDeviceApp(models.Model):
    """App abrível instalado no aparelho — inventário enviado no check-in.

    O app envia a lista completa dos apps com ícone de launcher (os únicos que podem
    virar atalho no modo quiosque), e **só quando ela muda**. O servidor substitui o
    conjunto inteiro do device. O TI marca quais ficam liberados → viram
    `KioskDevice.apps_permitidos`. A CHAVE é o `pkg`; o `nome` é só exibição (varia
    com o idioma do aparelho e não deve ser usado em lógica).
    """
    device        = models.ForeignKey(KioskDevice, on_delete=models.CASCADE, related_name='apps')
    pkg           = models.CharField(max_length=255, verbose_name='Pacote')
    nome          = models.CharField(max_length=255, blank=True, default='', verbose_name='Nome')
    sistema       = models.BooleanField(default=False, verbose_name='App de sistema')
    versao        = models.CharField(max_length=100, blank=True, default='', verbose_name='Versão')
    versao_codigo = models.BigIntegerField(default=0, verbose_name='Código da versão')
    atualizado_em = models.DateTimeField(null=True, blank=True, verbose_name='Atualizado em (no aparelho)')
    visto_em      = models.DateTimeField(auto_now=True, verbose_name='Visto em')

    class Meta:
        # Não-sistema primeiro (os que o TI costuma liberar), depois nome/pacote.
        ordering = ['sistema', 'nome', 'pkg']
        verbose_name = 'App de Dispositivo de Quiosque'
        verbose_name_plural = 'Apps de Dispositivos de Quiosque'
        unique_together = ('device', 'pkg')
        indexes = [models.Index(fields=['device', 'sistema'])]

    def __str__(self):
        return f"{self.nome or self.pkg} ({self.device})"


class KioskInstaladorLink(models.Model):
    """Link de instalação do APK do app Quiosque, com token opaco e validade curta.

    O .apk em si NÃO fica no banco nem no /media/ (servido sem autenticação) —
    fica numa pasta protegida no servidor (settings.KIOSK_APK_DIR). Só o hash do
    token é persistido; o token puro só existe uma vez, na resposta da geração do
    link (e no QR Code) — mesmo princípio do token de device e do PIN do TI.
    Quem baixa é o celular escaneando o QR, sem sessão logada; por isso a
    proteção é o token com validade curta, não @login_required.
    """
    token_hash         = models.CharField(max_length=64, unique=True, db_index=True)
    nome_arquivo        = models.CharField(max_length=255, verbose_name='Arquivo')
    expira_em           = models.DateTimeField(verbose_name='Expira em')
    revogado            = models.BooleanField(default=False)
    downloads           = models.PositiveIntegerField(default=0)
    ultimo_download_em  = models.DateTimeField(null=True, blank=True)
    ultimo_download_ip  = models.GenericIPAddressField(null=True, blank=True)
    criado_por          = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='kiosk_instaladores')
    criado_em           = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-criado_em']
        verbose_name = 'Link de Instalador de Quiosque'
        verbose_name_plural = 'Links de Instalador de Quiosque'

    def __str__(self):
        return f"Instalador {self.nome_arquivo} ({'revogado' if self.revogado else 'ativo'})"

    def esta_valido(self) -> bool:
        if self.revogado:
            return False
        return self.expira_em > timezone.now()


# ========== SOLICITAÇÕES DE COMPRA (KANBAN) ==========
# Rastreio interno do fluxo Datasul (requisição → aprovação do gestor) →
# Paradigma (compra). Sem integração real com esses sistemas externos — os
# números de requisição/compra são digitados manualmente quando já existem lá.
# Ver `services/requisicao_service.py` para as regras de transição de status.

class TipoRequisicaoChoices(models.TextChoices):
    COMPRA = "compra", "Compra"
    ESTOQUE = "estoque", "Estoque"


class StatusRequisicaoChoices(models.TextChoices):
    RASCUNHO = "rascunho", "Rascunho"
    ENVIADA_APROVACAO = "enviada_aprovacao", "Enviada para Aprovação"
    APROVADA = "aprovada", "Aprovada"
    NAO_APROVADA = "nao_aprovada", "Não Aprovada"
    PAUSADA = "pausada", "Pausada"
    COM_ERRO = "com_erro", "Com Erro"
    CANCELADA = "cancelada", "Cancelada"


class StatusItemSolicitacaoChoices(models.TextChoices):
    NAO_CADASTRADO = "nao_cadastrado", "Item não cadastrado"
    NAO_SOLICITADO = "nao_solicitado", "Item não solicitado"
    SOLICITADO = "solicitado", "Item Solicitado"
    PAUSADO = "pausado", "Item pausado"
    REPROVADO = "reprovado", "Item reprovado"
    RETIRADO = "retirado", "Item Retirado"


class Requisicao(AuditModel):
    """
    Agrupamento de RequisicaoItem enviados juntos para aprovação — espelha uma
    requisição real do Datasul. Só existe (com PK) a partir do momento em que
    o usuário agrupa itens soltos; itens ainda soltos (ideia/rascunho solto)
    têm `RequisicaoItem.requisicao = None`.
    """
    tipo = models.CharField(max_length=10, choices=TipoRequisicaoChoices.choices, verbose_name="Tipo")
    status = models.CharField(max_length=20, choices=StatusRequisicaoChoices.choices,
                               default=StatusRequisicaoChoices.RASCUNHO, db_index=True)
    numero_datasul = models.CharField(max_length=30, blank=True, null=True, db_index=True,
        verbose_name="Número da Requisição (Datasul)",
        help_text="Preenchido manualmente quando a requisição já existir no Datasul. Sem integração automática.")
    numero_paradigma = models.CharField(max_length=30, blank=True, null=True, verbose_name="Número no Paradigma")
    observacoes = models.TextField(blank=True, null=True)
    status_anterior_pausa = models.CharField(max_length=20, choices=StatusRequisicaoChoices.choices,
        blank=True, null=True, verbose_name="Status antes de pausar")
    solicitada_em = models.DateTimeField(null=True, blank=True, verbose_name="Solicitada em")
    enviada_em = models.DateTimeField(null=True, blank=True)
    decidida_em = models.DateTimeField(null=True, blank=True, verbose_name="Aprovada/Reprovada em")
    decidida_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="requisicoes_decididas", verbose_name="Aprovada/Reprovada por")

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Requisição"
        verbose_name_plural = "Requisições"
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["tipo", "status"]),
            models.Index(fields=["numero_datasul"]),
        ]

    def __str__(self):
        return self.numero_datasul or f"Requisição #{self.pk} ({self.get_status_display()})"


class RequisicaoItem(AuditModel):
    """Card individual do Kanban de Requisições. Ver `services/requisicao_service.py`
    (`coluna_kanban`) para como o status deste item + o status da `requisicao`
    (quando houver) determinam em qual coluna o card aparece."""
    requisicao = models.ForeignKey(Requisicao, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="itens", verbose_name="Requisição")
    tipo = models.CharField(max_length=10, choices=TipoRequisicaoChoices.choices, verbose_name="Tipo")
    categoria = models.ForeignKey(Categoria, on_delete=models.PROTECT, related_name="itens_requisicao",
        verbose_name="Categoria")
    item_vinculado = models.ForeignKey(Item, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="requisicoes_kanban", verbose_name="Item / Equipamento (opcional)",
        help_text="Vincula a um item de consumo já cadastrado (com ou sem estoque ainda) — mostra a "
                   "quantidade disponível e permite dar entrada real ao receber a compra. Deixe em "
                   "branco para cadastro livre.")
    codigo = models.CharField(max_length=60, blank=True, null=True, verbose_name="Código",
        help_text="Código no Datasul, se já existir. Pode ficar em branco enquanto é só uma ideia.")
    descricao = models.CharField(max_length=255, verbose_name="Descrição")
    quantidade = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])
    status = models.CharField(max_length=15, choices=StatusItemSolicitacaoChoices.choices,
        default=StatusItemSolicitacaoChoices.NAO_CADASTRADO, db_index=True)
    justificativa = models.TextField(blank=True, null=True, verbose_name="Justificativa / motivo do pedido")
    retirado_em = models.DateTimeField(null=True, blank=True, verbose_name="Retirado em")
    retirado_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="itens_requisicao_retirados", verbose_name="Retirado por")

    # Preenchidos no recebimento de itens tipo Compra (ver
    # `RequisicaoService.finalizar_compra_estoque` / `finalizar_compra_sem_estoque`)
    # — únicos para TODO item de Compra, tenha ou não `item_vinculado`, pra
    # permitir calcular o custo da requisição a partir dos itens pedidos.
    numero_nf = models.CharField(max_length=30, blank=True, null=True, verbose_name="Número da NF")
    valor_unitario = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True,
        verbose_name="Valor Unitário (recebimento)")
    fornecedor = models.ForeignKey("Fornecedor", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="itens_requisicao_recebidos", verbose_name="Fornecedor (recebimento)",
        help_text="Quem entregou a compra. Gravado tanto no recebimento com vínculo de estoque "
                   "(mesmo valor enviado à Entrada em Movimentações) quanto sem vínculo — só assim "
                   "fica registrado no RequisicaoItem nos dois casos.")

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Item de Requisição"
        verbose_name_plural = "Itens de Requisição"
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["requisicao", "status"]),
            models.Index(fields=["tipo", "status"]),
        ]

    def __str__(self):
        return f"{self.descricao} ({self.get_status_display()})"

    @property
    def quantidade_disponivel_lote(self):
        if not self.item_vinculado_id:
            return None
        from django.db.models import Sum
        return self.item_vinculado.vinculos_lote.aggregate(t=Sum("quantidade_disponivel"))["t"] or 0

    @property
    def valor_total(self):
        """Custo do item para a requisição (quantidade × valor unitário
        recebido) — `None` enquanto o item de Compra ainda não foi recebido."""
        if self.valor_unitario is None:
            return None
        return self.valor_unitario * self.quantidade


class ComentarioRequisicaoItem(AuditModel):
    """Comentário sobre um `RequisicaoItem`. Modelo dedicado (não reaproveita
    `Comentario`, que é hardcoded para `Item`) — mesma convenção de comentário
    específico por domínio já usada no projeto."""
    requisicao_item = models.ForeignKey(RequisicaoItem, on_delete=models.CASCADE,
        related_name="comentarios", verbose_name="Item de Requisição")
    texto = models.TextField()

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Comentário do Item de Requisição"
        verbose_name_plural = "Comentários do Item de Requisição"

    def __str__(self):
        return f"Comentário sobre {self.requisicao_item.descricao}"


class ItemPadraoDatasul(AuditModel):
    """Catálogo de itens padronizados com código do Datasul, mantido pelo TI —
    usado só como atalho de preenchimento no formulário de `RequisicaoItem`
    (`codigo`/`descricao` são copiados na hora, sem FK persistida: apagar ou
    editar um item padrão depois não afeta requisições já criadas)."""
    codigo = models.CharField(max_length=60, unique=True, verbose_name="Código (Datasul)")
    descricao = models.CharField(max_length=255, verbose_name="Descrição")
    categoria = models.ForeignKey(Categoria, on_delete=models.PROTECT,
        related_name="itens_padrao_datasul", verbose_name="Categoria")
    ativo = models.BooleanField(default=True, verbose_name="Ativo",
        help_text="Itens inativos somem do seletor do formulário, mas continuam no catálogo.")

    class Meta:
        ordering = ["descricao"]
        verbose_name = "Item Padrão (Datasul)"
        verbose_name_plural = "Itens Padrão (Datasul)"
        indexes = [models.Index(fields=["ativo"])]

    def __str__(self):
        return f"{self.codigo} — {self.descricao}"


# ========== ASSINATURA DIGITAL DOS TERMOS DE RESPONSABILIDADE ==========
# Coleta da assinatura manuscrita do colaborador em tablet, no ato da entrega
# ou da devolução do equipamento, e emissão do termo já assinado em PDF.
#
# Por que um modelo próprio e não mais um campo em `MovimentacaoItem`: o termo
# assinado é PROVA, e prova precisa de tudo o que a cerca — quem assinou,
# quando, de qual aparelho/IP, sobre QUAL texto, com qual traço e com que
# integridade. Nada disso cabe num `FileField`. O `MovimentacaoItem.termo_pdf`
# continua existindo e continua sendo a via do termo escaneado em papel: o
# fluxo digital ALIMENTA aquele campo ao final (ver
# `termo_assinatura_service.registrar_assinatura`), para que o Repositório de
# Termos e a cascata de desligamento sigam funcionando sem precisar saber que
# a assinatura veio de um tablet.

class StatusAssinaturaTermoChoices(models.TextChoices):
    PENDENTE  = "pendente",  "Aguardando assinatura"
    ASSINADO  = "assinado",  "Assinado"
    CANCELADO = "cancelado", "Cancelado"
    # "Expirado" NAO e estado gravado: e derivado de `expira_em` (ver
    # `esta_expirado`). Gravar exigiria uma tarefa agendada só para virar
    # linhas de pendente para expirado — e um termo que ninguém assinou dentro
    # do prazo é, para todos os efeitos, indistinguível de um cujo prazo acabou
    # agora. Menos peça móvel, mesma resposta.


class StatusViaEmailChoices(models.TextChoices):
    """
    Resultado do envio da VIA do termo para o e-mail do colaborador.

    Existe porque "não recebi o e-mail" era uma pergunta sem resposta: o envio
    acontece num `on_commit`, o sucesso ia só para `logger.info` (que no
    settings.py vai para o CONSOLE, não para arquivo) e a falha ia para um log
    rotativo que ninguém lê olhando a tela do termo. Resultado: era impossível
    distinguir "o sistema mandou e o e-mail se perdeu no caminho" de "o sistema
    nunca tentou" — e as duas situações pedem providências opostas.

    Com isto gravado na linha, a tela do termo responde a pergunta, e o botão
    "Reenviar via" do TI tem onde registrar a nova tentativa.
    """
    NAO_ENVIADA = "nao_enviada", "Via ainda não enviada"
    ENVIADA     = "enviada",     "Via enviada"
    FALHOU      = "falhou",      "Falha no envio"
    SEM_EMAIL   = "sem_email",   "Colaborador sem e-mail no cadastro"
    # Termos assinados ANTES deste rastreamento existir. Não é o mesmo que
    # "não enviada": naquele desenho o envio automático já acontecia, só não
    # deixava registro — marcar esses como "não enviada" afirmaria que o
    # sistema não tentou, o que não é verdade e manda o TI investigar a coisa
    # errada. "Não há registro" é a informação correta, e é o que fica.
    ANTERIOR    = "anterior",    "Sem registro (anterior ao rastreamento)"


def _upload_assinatura_traco(instance, filename):
    return f"termos/assinaturas/{timezone.now():%Y/%m}/{uuid.uuid4().hex}.png"


def _upload_assinatura_pdf(instance, filename):
    return f"termos/assinados/{timezone.now():%Y/%m}/{filename}"


class TermoAssinatura(AuditModel):
    """
    Uma sessão de assinatura digital de termo de responsabilidade.

    Nasce PENDENTE, criada por um usuário interno com o equipamento (ou os
    equipamentos) e o colaborador já definidos, e carrega um token opaco de uso
    único e validade curta. O tablet abre `/termos/assinar/<token>/` — rota
    ANÔNIMA de propósito: quem assina é o colaborador, que não tem login no
    sistema, e exigir sessão ali significaria o TI emprestar a própria sessão
    para o colaborador assinar, o que é pior em todos os sentidos. A proteção é
    o token (só o hash é persistido), o prazo curto e o uso único.

    `dados` guarda um SNAPSHOT do que foi apresentado na tela no momento da
    assinatura (número do termo, chamado, acessórios, observações, lista de
    equipamentos com série e patrimônio, e o texto legal em vigor). É snapshot
    por necessidade jurídica: o colaborador assinou AQUELE texto sobre AQUELE
    equipamento, e nem a renomeação do item depois nem uma atualização do
    modelo base do termo podem reescrever o que ele assinou.
    """
    tipo = models.CharField(
        max_length=10,
        choices=TipoTransferenciaChoices.choices,
        verbose_name="Tipo de termo",
    )

    colaborador = models.ForeignKey(
        "Usuario",
        on_delete=models.PROTECT,
        related_name="termos_assinatura",
        verbose_name="Colaborador signatário",
    )

    itens = models.ManyToManyField(
        "Item",
        related_name="termos_assinatura",
        blank=True,
        verbose_name="Equipamentos do termo",
    )

    # Preenchido quando a assinatura nasce de uma movimentação já registrada
    # (ou de várias, no caso do lote/desligamento). Ao concluir, o PDF assinado
    # é gravado também no `termo_pdf` de cada uma delas.
    movimentacoes = models.ManyToManyField(
        "MovimentacaoItem",
        related_name="assinaturas_termo",
        blank=True,
        verbose_name="Movimentações vinculadas",
    )

    status = models.CharField(
        max_length=12,
        choices=StatusAssinaturaTermoChoices.choices,
        default=StatusAssinaturaTermoChoices.PENDENTE,
        db_index=True,
        verbose_name="Situação",
    )

    # Desligamento é uma DEVOLUÇÃO com consequências maiores, não um terceiro
    # tipo de termo: o texto legal assinado é o mesmo da devolução (o
    # `docs_templates/.../Termo_de_Devolução.docx`), e o `tipo` continua
    # "devolucao" para o PDF, a movimentação e os relatórios não precisarem
    # aprender um caso novo. O que este campo marca é o EFEITO: ao assinar,
    # além de devolver os equipamentos, o sistema remove todas as licenças do
    # colaborador e marca o cadastro como desligado (ver
    # `termo_assinatura_service._executar_desligamento`).
    desligamento = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name="Encerra o vínculo (desligamento)",
        help_text=(
            "Ao assinar, devolve TODOS os equipamentos, remove TODAS as licenças "
            "e marca o colaborador como desligado."
        ),
    )

    # -- Token do tablet ---------------------------------------------------
    # Só o SHA-256 é persistido; o token puro existe uma única vez, na resposta
    # da criação da sessão (no QR Code e no link copiável) — mesmo princípio do
    # token de device e do link de instalador do Quiosque.
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    expira_em = models.DateTimeField(verbose_name="Link expira em")

    numero_termo = models.CharField(max_length=200, blank=True, verbose_name="Número do termo")
    dados = models.JSONField(default=dict, blank=True, verbose_name="Snapshot do termo")

    # Código curto e legível impresso no PDF, para conferir a autenticidade do
    # documento depois (`/termos/assinaturas/verificar/`). Não é segredo: a
    # tela de verificação é autenticada e o código só serve para LOCALIZAR o
    # registro — a prova de integridade é o `pdf_sha256`.
    codigo_verificacao = models.CharField(max_length=20, unique=True, db_index=True)

    # -- Traço da assinatura -----------------------------------------------
    assinatura_colaborador = models.ImageField(
        upload_to=_upload_assinatura_traco, blank=True, null=True,
        verbose_name="Assinatura do colaborador",
    )
    assinatura_responsavel = models.ImageField(
        upload_to=_upload_assinatura_traco, blank=True, null=True,
        verbose_name="Assinatura do responsável do TI",
    )

    # -- Identificação declarada no ato ------------------------------------
    # O texto do termo tem campos de RG e CPF em branco para preenchimento à
    # mão. Na via digital eles são digitados pelo próprio signatário no tablet,
    # entram no PDF emitido e PARAM AÍ.
    #
    # NÃO existe campo de CPF/RG neste model, e isso é deliberado: guardar o
    # documento de 1.376 colaboradores numa coluna cria um alvo que o sistema
    # não precisa ter. O termo vale como prova com o documento impresso nele; a
    # via fica com o colaborador e no `MovimentacaoItem.termo_pdf`, que é
    # arquivo protegido por `media_acesso_service` (regra 23) — não uma tabela
    # consultável que vaza inteira se o banco vazar. A migration `0170` removeu
    # as colunas `signatario_cpf`/`signatario_rg` e apagou o conteúdo delas.
    #
    # Para localizar termos por pessoa, use `colaborador` — já está aqui.
    signatario_nome = models.CharField(max_length=150, blank=True, verbose_name="Nome confirmado")

    # -- Trilha de auditoria do ato ----------------------------------------
    assinado_em = models.DateTimeField(null=True, blank=True, verbose_name="Assinado em")
    assinado_ip = models.GenericIPAddressField(null=True, blank=True, verbose_name="IP do aparelho")
    assinado_user_agent = models.CharField(max_length=400, blank=True, verbose_name="Aparelho/navegador")
    aceite_declarado = models.BooleanField(
        default=False,
        verbose_name="Aceite eletrônico declarado",
        help_text="O signatário marcou, no tablet, que leu o termo e aceita assiná-lo eletronicamente.",
    )

    # -- Documento emitido -------------------------------------------------
    pdf = models.FileField(
        upload_to=_upload_assinatura_pdf, blank=True, null=True,
        verbose_name="Termo assinado (PDF)",
    )
    pdf_sha256 = models.CharField(
        max_length=64, blank=True, db_index=True, verbose_name="SHA-256 do PDF",
    )

    cancelado_em = models.DateTimeField(null=True, blank=True, verbose_name="Cancelado em")
    motivo_cancelamento = models.CharField(max_length=200, blank=True, verbose_name="Motivo do cancelamento")

    # -- Via enviada ao colaborador ----------------------------------------
    # Rastro do envio automático (e de cada reenvio). Ver
    # `StatusViaEmailChoices` para o porquê de isto ser coluna e não só log.
    via_email_status = models.CharField(
        max_length=12,
        choices=StatusViaEmailChoices.choices,
        default=StatusViaEmailChoices.NAO_ENVIADA,
        db_index=True,
        verbose_name="Via por e-mail",
    )
    via_email_em = models.DateTimeField(
        null=True, blank=True, verbose_name="Última tentativa de envio da via",
    )
    via_email_tentativas = models.PositiveIntegerField(
        default=0, verbose_name="Tentativas de envio da via",
    )
    # Endereço MASCARADO e/ou motivo da falha. Mascarado de propósito: o e-mail
    # completo já está em `Usuario.email` e repeti-lo aqui só multiplicaria o
    # lugar de onde ele vaza — para saber o destino exato, olhe o cadastro.
    via_email_detalhe = models.CharField(
        max_length=300, blank=True, verbose_name="Resultado do envio da via",
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Assinatura de Termo"
        verbose_name_plural = "Assinaturas de Termos"
        indexes = [
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["colaborador", "-created_at"]),
            models.Index(fields=["tipo", "-created_at"]),
        ]

    def __str__(self):
        return f"Termo de {self.get_tipo_display()} - {self.colaborador} - {self.get_status_display()}"

    # -- Imutabilidade da prova --------------------------------------------
    #
    # Um termo assinado é PROVA: o colaborador declarou aceite sobre AQUELE
    # texto, sobre AQUELES equipamentos, e o `pdf_sha256` é o que permite
    # afirmar depois que o PDF não mudou. Se qualquer um destes campos puder ser
    # reescrito, a verificação de autenticidade passa a atestar o documento
    # ERRADO — e continua dizendo "íntegro", porque o hash teria sido
    # regravado junto. É a pior falha possível aqui: silenciosa e com cara de
    # sucesso.
    #
    # O risco real não é o invasor — é o código. Um `save()` distraído numa
    # rotina futura (recalcular `dados`, reaproveitar a linha para "reassinar",
    # um import que atualiza itens em massa) faz isso sem má intenção e sem
    # deixar rastro. Por isso o bloqueio é no MODEL, e não só na view: é o
    # único ponto por onde todo caminho de gravação passa.
    #
    # `via_email_*` fica FORA da lista de propósito: ele registra o que
    # aconteceu DEPOIS da assinatura (o envio da via, e cada reenvio pedido
    # pelo TI) e não faz parte do que foi assinado.
    CAMPOS_IMUTAVEIS_APOS_ASSINATURA = (
        "tipo", "colaborador", "dados", "numero_termo",
        "token_hash", "codigo_verificacao",
        "assinatura_colaborador", "assinatura_responsavel",
        "signatario_nome", "aceite_declarado",
        "assinado_em", "assinado_ip", "assinado_user_agent",
        "pdf", "pdf_sha256", "desligamento",
    )

    def _valor_de_prova(self, campo):
        """
        Valor comparável do campo: FK pelo id (sem buscar o objeto) e arquivo
        pelo caminho — comparar `FieldFile` por igualdade não serve.

        "Vazio" é normalizado para `None` porque os dois vazios NÃO são o mesmo
        objeto dependendo de como a linha chegou: um campo de arquivo opcional
        (`assinatura_responsavel`) vale `None` numa instância recém-construída
        (é o `get_default()` de um `FileField(null=True)`) e `''` numa linha
        lida do banco, que foi gravada a partir do `FieldFile` vazio. Sem
        normalizar, qualquer `save()` legítimo num termo assinado sem
        assinatura do responsável — que é o caso COMUM, o campo é opcional —
        seria recusado apontando um campo que ninguém tocou.
        """
        meta = self._meta.get_field(campo)
        if meta.is_relation:
            return getattr(self, meta.attname)
        valor = getattr(self, campo, None)
        valor = getattr(valor, "name", valor)
        return None if valor in ("", None) else valor

    def save(self, *args, **kwargs):
        """
        Recusa alterar a prova de um termo já assinado.

        A comparação é contra o BANCO, não contra o estado em memória: o
        próprio ato de assinar grava todos estes campos de uma vez, e naquele
        momento a linha ainda está PENDENTE — então aquele `save()` passa. O
        que o guarda impede é o save SEGUINTE.
        """
        if self.pk and self.status == StatusAssinaturaTermoChoices.ASSINADO:
            campos = kwargs.get("update_fields")
            # Atalho: save que só toca campo liberado (`via_email_*`,
            # `atualizado_por`) não precisa consultar o banco. `update_fields`
            # ausente significa "grava tudo", e aí a conferência sempre vale.
            protegidos = set(self.CAMPOS_IMUTAVEIS_APOS_ASSINATURA)
            if campos is None or (set(campos) & protegidos):
                anterior = (
                    type(self)._base_manager
                    .filter(pk=self.pk, status=StatusAssinaturaTermoChoices.ASSINADO)
                    .first()
                )
                if anterior is not None:
                    a_conferir = protegidos if campos is None else (set(campos) & protegidos)
                    alterados = sorted(
                        campo for campo in a_conferir
                        if self._valor_de_prova(campo) != anterior._valor_de_prova(campo)
                    )
                    if alterados:
                        raise ValidationError(
                            "Este termo já foi assinado e seu conteúdo não pode ser "
                            "alterado — ele é a prova do que o colaborador firmou. "
                            "Campo(s) recusado(s): " + ", ".join(alterados) + ". "
                            "Para desfazer o efeito de uma entrega, reverta a "
                            "movimentação; para corrigir um dado, emita um termo novo."
                        )
        return super().save(*args, **kwargs)

    # -- Estado derivado ---------------------------------------------------
    @property
    def esta_expirado(self) -> bool:
        return (
            self.status == StatusAssinaturaTermoChoices.PENDENTE
            and self.expira_em is not None
            and self.expira_em <= timezone.now()
        )

    @property
    def pode_assinar(self) -> bool:
        return self.status == StatusAssinaturaTermoChoices.PENDENTE and not self.esta_expirado

    @property
    def situacao_slug(self) -> str:
        """Situação para a UI, com o "expirado" derivado já resolvido."""
        if self.esta_expirado:
            return "expirado"
        return self.status

    @property
    def situacao_label(self) -> str:
        if self.esta_expirado:
            return "Link expirado"
        return self.get_status_display()

    @property
    def equipamentos_resumo(self) -> str:
        """Resumo dos equipamentos a partir do SNAPSHOT, não das FKs — é o que
        foi assinado, e segue legível mesmo que o item seja renomeado depois."""
        linhas = (self.dados or {}).get("equipamentos") or []
        nomes = [str(linha.get("nome") or "").strip() for linha in linhas]
        nomes = [n for n in nomes if n]
        if not nomes:
            return "—"
        if len(nomes) == 1:
            return nomes[0]
        return f"{nomes[0]} + {len(nomes) - 1} equipamento{'s' if len(nomes) > 2 else ''}"


# ========== MAPA DE REDE (TOPOLOGIA) ==========
# O módulo de Plantas desenha ONDE o equipamento está. O que faltava era COMO
# ele está ligado: qual porta do switch, com que meio, para onde vai. Sem isso
# "mapear a rede da empresa" era uma pergunta respondida por planilha paralela.
#
# A decisão que organiza tudo aqui: o cabo é um fato da PORTA, não do desenho.
# O enlace mora em RedePorta.destino_porta / destino_item; o
# RedeTopologia.layout guarda só geometria (posição do nó, zonas, notas). Por
# isso um mapa nunca pode discordar do inventário de portas — e a mesma ligação
# aparece em todos os mapas que mostrem os dois equipamentos. O contrário
# (enlace dentro do JSON do mapa) daria dois desenhos afirmando cabeamentos
# diferentes para o mesmo switch, sem ninguém para desempatar.


class RedePapelChoices(models.TextChoices):
    """
    Camada do equipamento na hierarquia da rede.

    É o que permite o auto-arranjo do mapa sair parecido com um diagrama de
    verdade (borda em cima, terminal embaixo) em vez de uma nuvem de caixas.
    """
    BORDA        = 'borda',        'Borda / Internet'
    CORE         = 'core',         'Núcleo (core)'
    DISTRIBUICAO = 'distribuicao', 'Distribuição'
    ACESSO       = 'acesso',       'Acesso'
    TERMINAL     = 'terminal',     'Terminal'


class RedePortaTipoChoices(models.TextChoices):
    RJ45    = 'rj45',    'RJ45 (cobre)'
    SFP     = 'sfp',     'SFP / SFP+ (fibra)'
    COMBO   = 'combo',   'Combo (uplink)'
    CONSOLE = 'console', 'Console'


class RedePortaStatusChoices(models.TextChoices):
    """
    Situação administrativa da porta.

    ATIVA é o estado de uma porta COM cabo — é ele que responde "quantas
    portas do switch estão em uso". LIVRE é porta disponível. Os outros três
    existem porque "não tem cabo" e "não pode receber cabo" são coisas
    diferentes na hora de planejar a próxima instalação.
    """
    ATIVA     = 'ativa',     'Ativa (em uso)'
    LIVRE     = 'livre',     'Livre'
    RESERVADA = 'reservada', 'Reservada'
    DEFEITO   = 'defeito',   'Com defeito'
    DESLIGADA = 'desligada', 'Desligada (administrativo)'


class RedeVelocidadeChoices(models.TextChoices):
    FE    = '100m', '100 Mbps'
    GE    = '1g',   '1 Gbps'
    MGE   = '2.5g', '2,5 Gbps'
    TENGE = '10g',  '10 Gbps'


class RedeMeioChoices(models.TextChoices):
    UTP    = 'utp',    'Cabo UTP'
    FIBRA  = 'fibra',  'Fibra óptica'
    RADIO  = 'radio',  'Enlace de rádio'
    STACK  = 'stack',  'Empilhamento (stack)'
    LOGICO = 'logico', 'Lógico / virtual'


class RedeDispositivo(AuditModel):
    """
    A faceta de REDE de um ativo do inventário.

    Não é um cadastro paralelo de equipamento: é uma extensão 1-1 de Item
    com o que só a rede precisa saber (papel, IP de gerência, vínculo PRTG).
    Pôr essas colunas em Item cobraria de monitor e headset um custo que é
    de switch e access point — e Item é consumido por 174 templates.
    """
    item = models.OneToOneField(
        Item,
        on_delete=models.CASCADE,
        related_name='rede',
        verbose_name="Equipamento",
    )
    papel = models.CharField(
        max_length=14,
        choices=RedePapelChoices.choices,
        default=RedePapelChoices.ACESSO,
        db_index=True,
        verbose_name="Papel na rede",
    )
    ip_gerencia = models.GenericIPAddressField(
        null=True, blank=True,
        verbose_name="IP de gerência",
        help_text="Endereço de administração do equipamento.",
    )
    prtg_objid = models.PositiveIntegerField(
        null=True, blank=True,
        db_index=True,
        verbose_name="ID do objeto no PRTG",
        help_text="Vínculo com o monitoramento. Sem ele o nó aparece como sem monitoramento.",
    )
    modelo_portas = models.CharField(
        max_length=40, blank=True,
        verbose_name="Modelo do catálogo",
        help_text="Chave do modelo usado para gerar as portas (ver rede_topologia_service).",
    )
    observacoes = models.TextField(blank=True, verbose_name="Observações de rede")

    # ── Onde ele está FISICAMENTE ──────────────────────────────────────────
    # `Item.localidade` responde "em que fazenda", que é o que o inventário
    # precisa. Nenhuma dessas três colunas responde "em que armário, em que
    # altura, em que ponto do terreno" — e é essa a pergunta de quem vai
    # puxar o cabo. Ficam aqui, na faceta de rede, e não em Item, pela mesma
    # razão do bloco acima: monitor e headset não têm posição em rack.
    rack = models.ForeignKey(
        'RedeRack',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='equipamentos',
        verbose_name="Rack",
    )
    rack_u = models.PositiveSmallIntegerField(
        null=True, blank=True,
        verbose_name="Posição no rack (U)",
        help_text="Contada de baixo para cima, como na régua do rack. Vazio = posição não declarada.",
    )
    rack_altura_u = models.PositiveSmallIntegerField(
        default=1,
        verbose_name="Altura no rack (U)",
        help_text="Quantas unidades o equipamento ocupa. Switch de 24p costuma ser 1U.",
    )
    latitude = models.DecimalField(
        max_digits=10, decimal_places=7,
        null=True, blank=True,
        verbose_name="Latitude",
        help_text="Posição no mapa. Vazio = herda a posição do rack, se houver.",
    )
    longitude = models.DecimalField(
        max_digits=10, decimal_places=7,
        null=True, blank=True,
        verbose_name="Longitude",
    )

    class Meta:
        verbose_name = "Dispositivo de rede"
        verbose_name_plural = "Dispositivos de rede"
        ordering = ['item__nome']

    def __str__(self):
        return f"{self.item.nome} ({self.get_papel_display()})"

    # ── Portas: as contas que a tela e o relatório pedem ──────────────────
    @property
    def total_portas(self) -> int:
        return self.portas.count()

    @property
    def portas_ativas(self) -> int:
        return self.portas.filter(status=RedePortaStatusChoices.ATIVA).count()

    @property
    def ocupacao_pct(self) -> int:
        total = self.total_portas
        if not total:
            return 0
        return round(self.portas_ativas * 100 / total)


class RedePorta(AuditModel):
    """
    Uma porta física do equipamento — e, quando ela tem cabo, o outro lado dele.

    destino_porta é 1-1 porque uma porta só pode estar ligada a uma outra:
    o banco recusa um segundo cabo no mesmo destino, que é o erro de cadastro
    mais comum ao mapear um rack inteiro na mão. O par é gravado nos DOIS
    sentidos por rede_topologia_service.conectar — nunca à mão, senão o grafo
    sai com enlace de uma perna só.

    destino_item existe para o caso em que o outro lado NÃO tem portas
    cadastradas (uma câmera, um desktop): dá para registrar "a porta 7 vai na
    câmera do pátio" sem obrigar a modelar as portas da câmera.
    """
    dispositivo = models.ForeignKey(
        RedeDispositivo,
        on_delete=models.CASCADE,
        related_name='portas',
        verbose_name="Dispositivo",
    )
    numero = models.PositiveSmallIntegerField(verbose_name="Número")
    rotulo = models.CharField(
        max_length=40, blank=True,
        verbose_name="Rótulo",
        help_text="Identificação do fabricante (ex.: Gi1/0/12).",
    )
    tipo = models.CharField(
        max_length=10,
        choices=RedePortaTipoChoices.choices,
        default=RedePortaTipoChoices.RJ45,
        verbose_name="Tipo",
    )
    status = models.CharField(
        max_length=10,
        choices=RedePortaStatusChoices.choices,
        default=RedePortaStatusChoices.LIVRE,
        db_index=True,
        verbose_name="Situação",
    )
    velocidade = models.CharField(
        max_length=6, blank=True,
        choices=RedeVelocidadeChoices.choices,
        verbose_name="Velocidade",
    )
    poe = models.BooleanField(default=False, verbose_name="PoE")
    vlan = models.CharField(max_length=40, blank=True, verbose_name="VLAN")
    meio = models.CharField(
        max_length=8,
        choices=RedeMeioChoices.choices,
        default=RedeMeioChoices.UTP,
        verbose_name="Meio do enlace",
    )
    destino_porta = models.OneToOneField(
        'self',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='porta_origem',
        verbose_name="Porta de destino",
    )
    destino_item = models.ForeignKey(
        Item,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='portas_de_entrada',
        verbose_name="Equipamento de destino",
    )
    descricao = models.CharField(max_length=160, blank=True, verbose_name="Descrição")
    rota = models.ForeignKey(
        'RedeRota',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='enlaces',
        verbose_name="Rota física do cabo",
        help_text="Por onde este lance passa no terreno. Várias portas dividem a mesma vala.",
    )

    class Meta:
        verbose_name = "Porta de rede"
        verbose_name_plural = "Portas de rede"
        ordering = ['dispositivo__item__nome', 'numero']
        constraints = [
            models.UniqueConstraint(
                fields=['dispositivo', 'numero'], name='uniq_porta_por_dispositivo'
            ),
        ]
        indexes = [models.Index(fields=['dispositivo', 'status'])]

    def __str__(self):
        return f"{self.dispositivo.item.nome} · {self.rotulo or self.numero}"

    @property
    def tem_enlace(self) -> bool:
        return bool(self.destino_porta_id or self.destino_item_id)

    def clean(self):
        super().clean()
        if self.destino_porta_id and self.destino_porta_id == self.pk:
            raise ValidationError({"destino_porta": "Uma porta não se liga a si mesma."})
        if self.destino_porta_id and self.dispositivo_id:
            destino = RedePorta.objects.filter(pk=self.destino_porta_id).first()
            if destino and destino.dispositivo_id == self.dispositivo_id:
                raise ValidationError(
                    {"destino_porta": "A porta de destino está no mesmo equipamento."}
                )


class RedeTopologia(AuditModel):
    """
    Um mapa salvo: escopo (localidade/área) + geometria.

    layout guarda SÓ desenho — {"nos": {"<item_id>": {"x":..., "y":...}},
    "zonas": [...], "notas": [...], "canvas": {...}}. Enlace nenhum entra
    aqui: ele é da porta (ver o comentário no topo deste bloco).
    """
    nome       = models.CharField(max_length=160, verbose_name="Nome do mapa")
    descricao  = models.TextField(blank=True, verbose_name="Descrição")
    localidade = models.ForeignKey(
        Localidade,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='topologias',
        verbose_name="Localidade",
    )
    area = models.CharField(
        max_length=15, blank=True,
        choices=AreaOperacionalChoices.choices,
        verbose_name="Área de operação",
        help_text="Vazio = o mapa não filtra por área.",
    )
    layout  = models.JSONField(default=dict, verbose_name="Geometria do mapa")
    versao  = models.PositiveIntegerField(default=1, verbose_name="Versão")

    class Meta:
        verbose_name = "Mapa de rede"
        verbose_name_plural = "Mapas de rede"
        ordering = ['localidade__local', 'nome']
        indexes = [models.Index(fields=['localidade'])]

    def __str__(self):
        return f"{self.nome} — {self.localidade or 'Corporativo'}"

    @property
    def total_nos(self) -> int:
        return len((self.layout or {}).get("nos") or {})


class RedeTopologiaHistorico(models.Model):
    """Versões anteriores da geometria — mesmo contrato do histórico de plantas."""
    topologia = models.ForeignKey(
        RedeTopologia, on_delete=models.CASCADE, related_name='historico'
    )
    versao    = models.PositiveIntegerField()
    layout    = models.JSONField()
    salvo_por = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='topologias_salvas'
    )
    descricao = models.CharField(max_length=200, blank=True, verbose_name="Descrição")
    salvo_em  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-versao']
        unique_together = [['topologia', 'versao']]
        verbose_name = "Versão de mapa de rede"
        verbose_name_plural = "Versões de mapa de rede"

    def __str__(self):
        return f"{self.topologia.nome} — v{self.versao}"


# ========== PLANTA GEORREFERENCIADA DA REDE (RACK, ROTA) ==========
# O bloco acima responde COMO o equipamento está ligado. O que faltava era
# POR ONDE o cabo passa fisicamente, e EM QUE rack o equipamento está.
#
# A separação que organiza este bloco é a mesma do anterior, um degrau abaixo:
#
#   • o CABO é fato da PORTA        (RedePorta.destino_porta) — já existia;
#   • a ROTA é fato do TERRENO      (RedeRota.pontos, em lat/lon);
#   • o cabo APONTA para a rota     (RedePorta.rota) — "este enlace viaja por
#     aquela vala".
#
# Por que não guardar o traçado dentro da porta: uma vala de fibra entre a
# sede e o galpão carrega doze enlaces. Com o traçado na porta, o mesmo
# caminho seria redesenhado doze vezes, e remanejar a vala obrigaria a
# corrigir doze cadastros — na prática, a corrigir nenhum. Com a rota como
# objeto, mede-se o comprimento uma vez, conta-se a ocupação de fibras, e
# "quem cai se esta vala for rompida" é uma pergunta respondível.
#
# Por que lat/lon e não x/y do desenho: o pedido é desenhar sobre o mapa real
# (ver Architecture Rule 15 para o provedor de tiles). Coordenada de tela
# muda de significado quando alguém dá zoom; coordenada geográfica é o fato,
# e é o que permite o comprimento sair em METROS — que é o número que decide
# se o lance de UTP cabe nos 90 m da norma ou se precisa de fibra.


class RedeRotaTipoChoices(models.TextChoices):
    """
    O que passa dentro da rota.

    Separar fibra de UTP não é detalhe de catálogo: o limite de 90 m do
    cabeamento horizontal (TIA-568) vale para o par trançado e não vale para
    a fibra. É esse par (tipo + comprimento medido) que faz a conferência da
    planta apontar o lance impossível antes de alguém puxar o cabo.
    """
    FIBRA_MM    = 'fibra_mm',   'Fibra óptica multimodo (OM3/OM4)'
    FIBRA_SM    = 'fibra_sm',   'Fibra óptica monomodo (OS2)'
    UTP_CAT5E   = 'utp_cat5e',  'Cabo UTP Cat5e'
    UTP_CAT6    = 'utp_cat6',   'Cabo UTP Cat6'
    UTP_CAT6A   = 'utp_cat6a',  'Cabo UTP Cat6A'
    COAXIAL     = 'coaxial',    'Cabo coaxial'
    ELETRICO    = 'eletrico',   'Alimentação elétrica'
    RADIO       = 'radio',      'Enlace de rádio (visada)'
    DUTO_VAZIO  = 'duto_vazio', 'Duto reservado (vazio)'


class RedeRotaInstalacaoChoices(models.TextChoices):
    """
    Como a rota está instalada no terreno.

    Muda o custo de mexer nela e muda quem some quando ela rompe: o lance
    aéreo cai com o galho, o subterrâneo cai com a retroescavadeira.
    """
    SUBTERRANEO = 'subterraneo', 'Subterrâneo (vala / duto)'
    AEREO       = 'aereo',       'Aéreo (poste / espinada)'
    EMBUTIDO    = 'embutido',    'Embutido (eletroduto / parede)'
    CANALETA    = 'canaleta',    'Canaleta aparente'
    LANCADO     = 'lancado',     'Lançado (forro / piso elevado)'
    VISADA      = 'visada',      'Visada livre (rádio)'


class RedeRack(AuditModel):
    """
    Um rack, armário ou caixa de emenda — o lugar físico do equipamento.

    Existe como objeto, e não como texto em `Item.observacoes`, por três
    perguntas que só um objeto responde: que altura útil (U) ainda sobra,
    o que exatamente cai quando falta energia neste armário, e onde ele
    está no mapa para a rota de fibra chegar até ele.
    """
    nome = models.CharField(max_length=120, verbose_name="Nome do rack")
    codigo = models.CharField(
        max_length=40, blank=True,
        verbose_name="Código / etiqueta",
        help_text="Como ele está etiquetado em campo (ex.: RK-SEDE-01).",
    )
    localidade = models.ForeignKey(
        Localidade,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='racks',
        verbose_name="Localidade",
    )
    area = models.CharField(
        max_length=15, blank=True,
        choices=AreaOperacionalChoices.choices,
        verbose_name="Área de operação",
    )
    ambiente = models.CharField(
        max_length=120, blank=True,
        verbose_name="Ambiente",
        help_text="Sala, galpão ou poste onde o rack está (ex.: CPD, almoxarifado).",
    )
    us = models.PositiveSmallIntegerField(
        default=42,
        verbose_name="Altura útil (U)",
        help_text="Quantas unidades de rack cabem. Caixa de emenda: deixe 1.",
    )
    latitude = models.DecimalField(
        max_digits=10, decimal_places=7,
        null=True, blank=True,
        verbose_name="Latitude",
    )
    longitude = models.DecimalField(
        max_digits=10, decimal_places=7,
        null=True, blank=True,
        verbose_name="Longitude",
    )
    observacoes = models.TextField(blank=True, verbose_name="Observações")

    class Meta:
        verbose_name = "Rack de rede"
        verbose_name_plural = "Racks de rede"
        ordering = ['localidade__local', 'nome']
        indexes = [models.Index(fields=['localidade'])]

    def __str__(self):
        return f"{self.nome}{f' ({self.codigo})' if self.codigo else ''}"

    @property
    def tem_geo(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    @property
    def us_ocupados(self) -> int:
        """Soma da altura dos equipamentos com posição declarada."""
        return sum(
            d.rack_altura_u or 1
            for d in self.equipamentos.all()
            if d.rack_u
        )


class RedeRota(AuditModel):
    """
    O caminho FÍSICO de um lance de cabo, em coordenadas geográficas.

    `pontos` é a lista do traçado — `[[lat, lon], [lat, lon], …]`, na ordem
    em que o cabo anda. Não é geometria de desenho: é o terreno. Daí sair
    `comprimento_m` em metros reais (ver `rede_topologia_service`), que é o
    número que o projeto precisa e que nenhuma coordenada de tela dá.

    `comprimento_m` é CALCULADO e gravado em vez de apurado a cada leitura
    porque ele entra em ordenação, em soma por tipo e na conferência dos
    90 m — e recalcular haversine de 400 rotas a cada tela seria pagar três
    vezes pelo mesmo número. Quem grava é sempre o service, nunca a tela.
    """
    nome = models.CharField(
        max_length=160,
        verbose_name="Nome da rota",
        help_text="Como o pessoal de campo chama o lance (ex.: Vala CPD -> Galpão 2).",
    )
    localidade = models.ForeignKey(
        Localidade,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='rotas_rede',
        verbose_name="Localidade",
    )
    area = models.CharField(
        max_length=15, blank=True,
        choices=AreaOperacionalChoices.choices,
        verbose_name="Área de operação",
    )
    tipo = models.CharField(
        max_length=12,
        choices=RedeRotaTipoChoices.choices,
        default=RedeRotaTipoChoices.FIBRA_SM,
        db_index=True,
        verbose_name="Tipo de cabo",
    )
    instalacao = models.CharField(
        max_length=12,
        choices=RedeRotaInstalacaoChoices.choices,
        default=RedeRotaInstalacaoChoices.SUBTERRANEO,
        verbose_name="Forma de instalação",
    )
    pontos = models.JSONField(
        default=list,
        verbose_name="Traçado",
        help_text="Lista [[lat, lon], ...] na ordem do lance.",
    )
    comprimento_m = models.DecimalField(
        max_digits=10, decimal_places=2,
        default=0,
        verbose_name="Comprimento (m)",
        help_text="Medido sobre o traçado pelo service. Não editar à mão.",
    )
    fibras = models.PositiveSmallIntegerField(
        default=0,
        verbose_name="Fibras / pares no cabo",
        help_text="Capacidade do cabo. 0 = não informado.",
    )
    rack_a = models.ForeignKey(
        RedeRack,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='rotas_saindo',
        verbose_name="Rack de origem",
    )
    rack_b = models.ForeignKey(
        RedeRack,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='rotas_chegando',
        verbose_name="Rack de destino",
    )
    observacoes = models.TextField(blank=True, verbose_name="Observações")

    class Meta:
        verbose_name = "Rota de cabo"
        verbose_name_plural = "Rotas de cabo"
        ordering = ['localidade__local', 'nome']
        indexes = [
            models.Index(fields=['localidade', 'tipo']),
        ]

    def __str__(self):
        return f"{self.nome} - {self.get_tipo_display()}"

    @property
    def e_cobre(self) -> bool:
        """Par trançado — é quem responde ao limite de 90 m."""
        return self.tipo in (
            RedeRotaTipoChoices.UTP_CAT5E,
            RedeRotaTipoChoices.UTP_CAT6,
            RedeRotaTipoChoices.UTP_CAT6A,
        )

    @property
    def total_pontos(self) -> int:
        return len(self.pontos or [])


# ========== CONTRATAÇÃO DE COLABORADOR (admissão / onboarding de TI) ==========
#
# O que isto substitui: um quadro do Microsoft Planner onde o RH abria um card
# por vaga e o TI lia, no corpo do card, um texto colado do formulário
# ("[TI] Ferramenta de trabalho: Computador: Notebook", "[TI] Sistema: TOTVS -
# Datasul: Sim", ...). Aquilo respondia "o que pediram", mas não respondia
# nada do que o TI precisa saber depois: QUAL notebook foi entregue, QUAIS
# acessos já foram criados, há quanto tempo a vaga está aberta e quantas
# estão atrasadas. Card de Planner não tem vínculo com o equipamento nem com
# a licença — e é justamente esse vínculo que o resto deste sistema já tem.
#
# Por que três modelos e não um JSONField com o formulário inteiro: o pedido
# ("preciso de Datasul") e o provisionamento ("Datasul criado em 12/03") são
# fatos de datas diferentes sobre a mesma linha. Num JSON, marcar um acesso
# como feito reescreve o documento todo e não sobra histórico; com
# `ContratacaoSistema` cada acesso tem o seu próprio estado e a sua própria
# data, e "o que ainda falta para esta pessoa começar?" vira uma consulta em
# vez de leitura de texto.

class StatusContratacaoChoices(models.TextChoices):
    ABERTA = "aberta", "Em aberto"
    VAGA_PREENCHIDA = "vaga_preenchida", "Vaga preenchida"
    EQUIPAMENTO_ENTREGUE = "equipamento_entregue", "Equipamento entregue"
    CONCLUIDA = "concluida", "Concluída"
    CANCELADA = "cancelada", "Cancelada"


class TipoVagaChoices(models.TextChoices):
    SUBSTITUICAO = "substituicao", "Substituição"
    NOVA = "nova", "Nova vaga (aumento de quadro)"
    TEMPORARIO = "temporario", "Temporário / safra"


class TipoComputadorChoices(models.TextChoices):
    NOTEBOOK = "notebook", "Notebook"
    DESKTOP = "desktop", "Desktop"
    NENHUM = "nenhum", "Não precisa de computador"


class TipoTelefoniaChoices(models.TextChoices):
    NENHUM = "nenhum", "Não precisa"
    RAMAL = "ramal", "Ramal"
    CELULAR = "celular", "Celular corporativo"
    RAMAL_CELULAR = "ramal_celular", "Ramal + celular corporativo"


class SistemaCorporativo(AuditModel):
    """Catálogo dos sistemas a que uma contratação pode exigir acesso (TOTVS
    Datasul, PIMS MC, Beneficiamento, Pesagem, Tekann, ID-Cotton...).

    É cadastro, e não lista fixa em código, porque a lista muda sem aviso
    (sistema novo entra, módulo sai) e quem sabe disso é o TI, não quem
    mantém o código. Cada linha vira uma pergunta no formulário de
    contratação; desmarcar `ativo` tira a pergunta das PRÓXIMAS contratações
    sem apagar o histórico das antigas.

    `licenca` é opcional e liga o acesso ao assento pago correspondente
    (ex.: "TOTVS - Datasul" com a licença "DATASUL / PIMS"): quando existe, a
    contratação sabe que criar aquele acesso consome um assento, e a tela de
    provisionamento leva o TI para a atribuição da licença em vez de deixar o
    assento sair do controle."""

    nome = models.CharField(max_length=120, unique=True, verbose_name="Sistema")
    descricao = models.CharField(
        max_length=255, blank=True, default="", verbose_name="Descrição",
        help_text="Aparece como ajuda embaixo da pergunta no formulário de contratação.",
    )
    ordem = models.PositiveSmallIntegerField(
        default=0, verbose_name="Ordem",
        help_text="Ordem de exibição no formulário (menor primeiro).",
    )
    ativo = models.BooleanField(
        default=True, verbose_name="Ativo",
        help_text="Inativo some do formulário das próximas contratações, mas continua nas já abertas.",
    )
    exige_perfil_espelho = models.BooleanField(
        default=True, verbose_name="Pede usuário espelho",
        help_text="Quando marcado, o formulário pergunta de qual usuário copiar o perfil.",
    )
    licenca = models.ForeignKey(
        "Licenca", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="sistemas_corporativos", verbose_name="Licença vinculada",
        help_text="Se o acesso consome um assento de licença, aponte aqui — a contratação "
                  "passa a cobrar a atribuição do assento junto com o acesso.",
    )

    class Meta:
        ordering = ["ordem", "nome"]
        verbose_name = "Sistema Corporativo"
        verbose_name_plural = "Sistemas Corporativos"
        indexes = [models.Index(fields=["ativo", "ordem"])]

    def __str__(self):
        return self.nome


class Contratacao(AuditModel):
    """Uma vaga em processo de admissão, do pedido do gestor até o
    colaborador sentado com tudo funcionando.

    As quatro etapas (`StatusContratacaoChoices`) são as mesmas colunas do
    quadro do Planner que este módulo substitui — mantidas de propósito, para
    quem já usava o quadro não precisar reaprender o fluxo:

        Em aberto -> Vaga preenchida -> Equipamento entregue -> Concluída

    A diferença é o que cada etapa passa a gravar: `colaborador` liga a vaga
    ao cadastro real do colaborador, `item_entregue` ao equipamento do
    inventário, e os carimbos de data permitem medir quanto tempo a vaga
    ficou em cada etapa. O card do Planner não tinha nenhum dos três.

    O que este modelo NÃO faz: movimentar estoque. Registrar a entrega aqui
    grava QUAL equipamento foi entregue; a transferência continua sendo feita
    em Movimentações, que é a fonte única do inventário (ver
    `services/movimentacao_service.py`). Duplicar isso aqui criaria duas
    verdades sobre o mesmo equipamento."""

    # ── A vaga ────────────────────────────────────────────────────────────
    cargo = models.CharField(
        max_length=150, verbose_name="Cargo da vaga",
        help_text="Como a vaga é conhecida — ex.: “Técnico de Segurança do Trabalho”.",
    )
    funcao = models.ForeignKey(
        Funcao, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="contratacoes", verbose_name="Função cadastrada",
        help_text="Opcional: liga a vaga à função já cadastrada no sistema.",
    )
    centro_custo = models.ForeignKey(
        CentroCusto, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="contratacoes", verbose_name="Centro de custo",
    )
    localidade = models.ForeignKey(
        Localidade, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="contratacoes", verbose_name="Localidade",
    )
    gestor = models.CharField(
        max_length=150, blank=True, default="", verbose_name="Gestor solicitante",
    )
    tipo_vaga = models.CharField(
        max_length=15, choices=TipoVagaChoices.choices,
        default=TipoVagaChoices.SUBSTITUICAO, verbose_name="Tipo da vaga",
    )
    substituido = models.ForeignKey(
        Usuario, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="contratacoes_substituindo", verbose_name="Substitui qual colaborador",
        help_text="Em vaga de substituição: de quem é a cadeira. É dele que sai o perfil "
                  "a espelhar e, muitas vezes, o próprio equipamento.",
    )
    data_prevista_inicio = models.DateField(
        null=True, blank=True, verbose_name="Previsão de início",
        help_text="Data em que a pessoa começa. É o prazo contra o qual a contratação atrasa.",
    )

    # ── Quem ocupou a vaga ────────────────────────────────────────────────
    colaborador = models.ForeignKey(
        Usuario, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="contratacoes", verbose_name="Colaborador contratado",
    )
    colaborador_nome = models.CharField(
        max_length=150, blank=True, default="", verbose_name="Nome do contratado",
        help_text="Use quando a pessoa ainda não tem cadastro de colaborador no sistema.",
    )
    # Dados que o RH informa ao marcar a vaga como preenchida. Todos opcionais
    # (exceto o nome, que é o próprio ato de preencher): o RH não deve ser
    # obrigado a inventar o que ainda não existe. Cargo, setor e unidade NÃO são
    # repetidos aqui — já são da vaga, e duplicá-los criaria duas verdades.
    colaborador_email = models.EmailField(
        blank=True, default="", verbose_name="E-mail do contratado",
        help_text="Corporativo ou pessoal, o que o RH tiver. Não vai nos e-mails de chamado.",
    )
    colaborador_telefone = models.CharField(
        max_length=30, blank=True, default="", verbose_name="Telefone do contratado",
    )
    colaborador_observacoes = models.TextField(
        blank=True, default="", verbose_name="Observações sobre o contratado",
    )
    data_admissao = models.DateField(
        null=True, blank=True, verbose_name="Data de admissão",
        help_text="Prevista ou efetiva. Diferente de “Previsão de início” da vaga, que é o prazo do pedido.",
    )
    preenchida_por = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="vagas_preenchidas", verbose_name="Preenchida por",
    )

    # ── Ferramenta de trabalho ────────────────────────────────────────────
    computador = models.CharField(
        max_length=10, choices=TipoComputadorChoices.choices,
        default=TipoComputadorChoices.NOTEBOOK, verbose_name="Computador",
    )
    computador_justificativa = models.TextField(
        blank=True, default="", verbose_name="Justificativa / configuração",
        help_text="Por que precisa e qual configuração — ex.: “Substituição”, “Tabulação de dados, 16 GB”.",
    )
    item_entregue = models.ForeignKey(
        Item, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="contratacoes", verbose_name="Equipamento entregue",
    )
    telefonia = models.CharField(
        max_length=15, choices=TipoTelefoniaChoices.choices,
        default=TipoTelefoniaChoices.NENHUM, verbose_name="Telefonia",
    )
    ligacao_direta = models.BooleanField(default=False, verbose_name="Fará ligação direta")
    ramal_sugerido = models.CharField(
        max_length=30, blank=True, default="", verbose_name="Ramal sugerido",
    )

    # ── Acessos ───────────────────────────────────────────────────────────
    email_corporativo = models.BooleanField(default=True, verbose_name="E-mail corporativo")
    email_sugerido = models.CharField(
        max_length=150, blank=True, default="", verbose_name="E-mail sugerido",
    )
    usuario_rede = models.BooleanField(default=True, verbose_name="Usuário de rede (login)")
    perfil_espelho = models.CharField(
        max_length=150, blank=True, default="", verbose_name="Usuário espelho (rede)",
        help_text="De qual usuário copiar as permissões de pasta e de grupo.",
    )
    acesso_vpn = models.BooleanField(default=False, verbose_name="Acesso VPN")
    acesso_impressora = models.BooleanField(default=False, verbose_name="Acesso a impressora")

    observacoes = models.TextField(blank=True, default="", verbose_name="Observações")

    # ── Andamento ─────────────────────────────────────────────────────────
    status = models.CharField(
        max_length=25, choices=StatusContratacaoChoices.choices,
        default=StatusContratacaoChoices.ABERTA, db_index=True,
    )
    vaga_preenchida_em = models.DateTimeField(null=True, blank=True)
    equipamento_entregue_em = models.DateTimeField(null=True, blank=True)
    concluida_em = models.DateTimeField(null=True, blank=True)
    cancelada_em = models.DateTimeField(null=True, blank=True)
    motivo_cancelamento = models.CharField(max_length=255, blank=True, default="")

    # ── Integração do colaborador ─────────────────────────────────────────
    # A integração é um eixo à parte do status da vaga: a vaga pode estar
    # "Preenchida" (ou até "Equipamento entregue") com a integração ainda em
    # andamento. Quem confirma, quando e com qual observação fica aqui; o
    # estado (não iniciada / em andamento / confirmada) é DERIVADO — ver
    # `contratacao_service.situacao_integracao`.
    integracao_confirmada_em = models.DateTimeField(null=True, blank=True)
    integracao_confirmada_por = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="integracoes_confirmadas", verbose_name="Integração confirmada por",
    )
    integracao_observacao = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Contratação"
        verbose_name_plural = "Contratações"
        indexes = [
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["data_prevista_inicio"]),
        ]

    def __str__(self):
        return f"{self.cargo} — {self.get_status_display()}"

    @property
    def nome_contratado(self) -> str:
        """Quem ocupa a vaga, venha do cadastro ou do campo livre. A vaga
        começa sem ninguém, então o vazio é um estado legítimo."""
        if self.colaborador_id:
            return self.colaborador.nome
        return self.colaborador_nome or ""

    @property
    def precisa_computador(self) -> bool:
        return self.computador != TipoComputadorChoices.NENHUM

    @property
    def encerrada(self) -> bool:
        return self.status in (
            StatusContratacaoChoices.CONCLUIDA, StatusContratacaoChoices.CANCELADA,
        )

    @property
    def atrasada(self) -> bool:
        """Previsão de início já passou e a contratação não fechou. É a única
        métrica de urgência do módulo: sem data prevista não há atraso —
        contratação sem prazo não é cobrada por um prazo inventado."""
        if self.encerrada or not self.data_prevista_inicio:
            return False
        return self.data_prevista_inicio < datetime.date.today()

    @property
    def dias_para_inicio(self):
        if not self.data_prevista_inicio:
            return None
        return (self.data_prevista_inicio - datetime.date.today()).days


class ContratacaoVagaHistorico(AuditModel):
    """Cada vez que a vaga foi preenchida, o contratado foi corrigido ou a vaga
    foi reaberta — com o estado ANTES e DEPOIS, quem fez e por quê.

    Existe porque `ContratacaoEvento.descricao` cabe em 255 caracteres: serve
    para a linha do tempo, não para guardar o contratado anterior por inteiro.
    Reabrir uma vaga LIMPA os dados do contratado da vaga (ela volta a não ter
    ninguém); o que ele era fica aqui, e nunca é editado nem apagado."""

    class Acao(models.TextChoices):
        PREENCHIMENTO = "preenchimento", "Vaga preenchida"
        CORRECAO = "correcao", "Dados do contratado corrigidos"
        REABERTURA = "reabertura", "Vaga reaberta"
        CADASTRO = "cadastro", "Colaborador cadastrado no sistema"

    contratacao = models.ForeignKey(
        Contratacao, on_delete=models.CASCADE, related_name="historico_vaga",
    )
    acao = models.CharField(max_length=15, choices=Acao.choices)
    status_antes = models.CharField(max_length=25, blank=True, default="")
    status_depois = models.CharField(max_length=25, blank=True, default="")
    motivo = models.TextField(blank=True, default="")
    antes = models.JSONField(default=dict, blank=True)
    depois = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        verbose_name = "Histórico do preenchimento da vaga"
        verbose_name_plural = "Histórico do preenchimento das vagas"
        indexes = [models.Index(fields=["contratacao", "-created_at"])]


class ContratacaoSistema(AuditModel):
    """Um acesso pedido para uma contratação — a linha "[TI] Sistema: X: Sim"
    do formulário do Planner, agora com estado próprio.

    `necessario` é o PEDIDO (o gestor marcou que precisa) e `provisionado` é
    o FATO (o TI criou). Os dois são campos separados porque a resposta a
    "falta alguma coisa para essa pessoa começar?" é a diferença entre eles —
    com um campo só, marcar como feito apagaria o pedido e a pergunta
    deixaria de ter resposta."""

    contratacao = models.ForeignKey(
        Contratacao, on_delete=models.CASCADE, related_name="sistemas",
        verbose_name="Contratação",
    )
    sistema = models.ForeignKey(
        SistemaCorporativo, on_delete=models.PROTECT, related_name="pedidos",
        verbose_name="Sistema",
    )
    necessario = models.BooleanField(default=False, verbose_name="Necessário")
    perfil_espelho = models.CharField(
        max_length=150, blank=True, default="", verbose_name="Usuário espelho",
        help_text="Nome do usuário de quem copiar o perfil neste sistema.",
    )
    observacao = models.CharField(max_length=255, blank=True, default="")
    provisionado = models.BooleanField(default=False, verbose_name="Acesso criado")
    provisionado_em = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["sistema__ordem", "sistema__nome"]
        verbose_name = "Sistema da Contratação"
        verbose_name_plural = "Sistemas da Contratação"
        constraints = [
            models.UniqueConstraint(
                fields=["contratacao", "sistema"], name="contratacao_sistema_unico",
            ),
        ]

    def __str__(self):
        return f"{self.sistema} — {'sim' if self.necessario else 'não'}"


class ContratacaoLicenca(AuditModel):
    """Assento de licença que a contratação precisa (Microsoft 365, Power BI...).

    Fica separado de `ContratacaoSistema` porque licença é ativo com custo e
    saldo (ver `services/licenca_service.py`): aqui se declara a necessidade,
    e a atribuição de verdade continua acontecendo no módulo de Licenças, que
    é quem debita o lote. `movimentacao` guarda o elo quando a atribuição é
    feita, para a contratação poder provar qual assento saiu."""

    contratacao = models.ForeignKey(
        Contratacao, on_delete=models.CASCADE, related_name="licencas_necessarias",
        verbose_name="Contratação",
    )
    licenca = models.ForeignKey(
        "Licenca", on_delete=models.PROTECT, related_name="contratacoes",
        verbose_name="Licença",
    )
    # Mesmo campo, mesmo tipo e mesmo papel do `ContratacaoSistema.perfil_espelho`:
    # no formulário as duas coisas são UMA lista só ("marque a caixa e diga de
    # quem copiar o perfil"), e tipos diferentes nos dois lados obrigariam a
    # duas versões de cada linha do template, do form e do service.
    perfil_espelho = models.CharField(
        max_length=150, blank=True, default="", verbose_name="Usuário espelho",
        help_text="Nome do usuário de quem copiar o perfil nesta licença.",
    )
    observacao = models.CharField(max_length=255, blank=True, default="")
    provisionada = models.BooleanField(default=False, verbose_name="Assento atribuído")
    provisionada_em = models.DateTimeField(null=True, blank=True)
    movimentacao = models.ForeignKey(
        "MovimentacaoLicenca", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="contratacoes", verbose_name="Movimentação do assento",
    )

    class Meta:
        ordering = ["licenca__nome"]
        verbose_name = "Licença da Contratação"
        verbose_name_plural = "Licenças da Contratação"
        constraints = [
            models.UniqueConstraint(
                fields=["contratacao", "licenca"], name="contratacao_licenca_unica",
            ),
        ]

    def __str__(self):
        return f"{self.licenca} — {'atribuída' if self.provisionada else 'pendente'}"


class ContratacaoEvento(AuditModel):
    """Linha do tempo da contratação: abertura, mudança de etapa, acesso
    criado, cancelamento. Mesma função do `OrdemManutencaoEvento` no módulo
    de manutenção — o estado atual diz ONDE a vaga está, o evento diz COMO
    ela chegou lá, e é isso que se lê quando alguém pergunta por que a
    admissão demorou."""

    contratacao = models.ForeignKey(
        Contratacao, on_delete=models.CASCADE, related_name="eventos",
        verbose_name="Contratação",
    )
    tipo = models.CharField(max_length=30, default="etapa")
    descricao = models.CharField(max_length=255)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Evento da Contratação"
        verbose_name_plural = "Eventos da Contratação"
        indexes = [models.Index(fields=["contratacao", "-created_at"])]

    def __str__(self):
        return self.descricao


class ContratacaoEtapaRegistro(AuditModel):
    """Registro MANUAL de uma etapa do processo — só existe quando alguém
    precisou DECIDIR algo que o sistema não deduz sozinho:

      • `concluida`  — o TI registrou que a etapa foi feita (as orientações de
        uso dos sistemas, por exemplo: não há movimentação nem chamado que
        prove que a conversa aconteceu);
      • `dispensada` — a etapa não se aplica a esta contratação, e a
        justificativa diz por quê.

    Etapa que o sistema deduz do que já existe (colaborador cadastrado,
    equipamento reservado, entrega por movimentação, acesso provisionado,
    chamado concluído) não tem linha aqui: duplicar o fato num registro à mão
    criaria duas verdades que divergem. Nunca é apagada; desfazer uma dispensa
    grava `desfeita_em`, e a linha continua como histórico."""

    class Estado(models.TextChoices):
        CONCLUIDA = "concluida", "Concluída"
        DISPENSADA = "dispensada", "Dispensada"

    contratacao = models.ForeignKey(
        Contratacao, on_delete=models.CASCADE, related_name="etapas_registro",
    )
    chave = models.CharField(max_length=30)
    estado = models.CharField(max_length=12, choices=Estado.choices)
    justificativa = models.CharField(max_length=255, blank=True, default="")
    registrada_em = models.DateTimeField(default=timezone.now)
    desfeita_em = models.DateTimeField(null=True, blank=True)
    desfeita_por = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )

    class Meta:
        ordering = ["-registrada_em", "-pk"]
        verbose_name = "Registro de etapa da contratação"
        verbose_name_plural = "Registros de etapa da contratação"
        constraints = [
            # Uma linha VIGENTE por (vaga, etapa). A desfeita fica de histórico.
            models.UniqueConstraint(
                fields=["contratacao", "chave"],
                condition=models.Q(desfeita_em__isnull=True),
                name="etapa_registro_vigente_unica",
            ),
        ]
        indexes = [models.Index(fields=["contratacao", "chave"])]

    def __str__(self):
        return f"{self.chave}: {self.get_estado_display()}"


class ReservaEquipamento(AuditModel):
    """A destinação PLANEJADA de um equipamento do estoque para uma vaga.

    Reserva NÃO é entrega, transferência nem ativação: o equipamento continua
    no mesmo lugar, no mesmo centro de custo, sem baixa e sem termo — só o
    status muda (Backup → Reservado) e esta linha registra para quê. A entrega
    é a movimentação patrimonial feita pelo TI em nome do colaborador; é ela
    que fecha a reserva (`estado=entregue`) e guarda a prova em `movimentacao`.

    A linha NUNCA é apagada: liberar, ceder para outra necessidade ou entregar
    muda `estado` e grava quem/quando/por quê. O histórico de um equipamento
    ("já foi reservado para quem?") e de uma vaga ("que máquinas já passaram
    por ela?") sai daqui.

    `contratacao` é SET_NULL e há `vaga_rotulo` de propósito: excluir a vaga
    não pode levar o histórico da reserva junto — o rótulo diz de qual vaga era.
    """

    class Estado(models.TextChoices):
        ATIVA = "ativa", "Reservado"
        ENTREGUE = "entregue", "Entregue"
        LIBERADA = "liberada", "Liberado (voltou ao Backup)"
        CEDIDA = "cedida", "Usado em outra necessidade"

    class Motivo(models.TextChoices):
        CANCELAMENTO = "cancelamento", "Vaga cancelada"
        EXCLUSAO = "exclusao", "Vaga excluída"
        MANUAL = "manual", "Liberado pelo TI"
        ENTREGA = "entrega", "Entregue ao colaborador"
        REPOSICAO = "reposicao", "Usado em reposição"
        STATUS_ALTERADO = "status_alterado", "Status alterado fora da reserva"

    contratacao = models.ForeignKey(
        Contratacao, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="reservas", verbose_name="Vaga",
    )
    vaga_rotulo = models.CharField(max_length=200, blank=True, default="")
    item = models.ForeignKey(
        Item, on_delete=models.PROTECT, related_name="reservas", verbose_name="Equipamento",
    )
    item_rotulo = models.CharField(max_length=200, blank=True, default="")
    colaborador = models.ForeignKey(
        Usuario, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="reservas_equipamento", verbose_name="Colaborador",
        help_text="Preenchido quando a vaga é preenchida e o colaborador cadastrado.",
    )
    estado = models.CharField(
        max_length=10, choices=Estado.choices, default=Estado.ATIVA, db_index=True,
    )
    status_anterior = models.CharField(
        max_length=15, default=StatusItemChoices.BACKUP,
        help_text="Status do equipamento antes de reservar — para onde ele volta ao liberar.",
    )
    reservado_em = models.DateTimeField(default=timezone.now)
    adicional = models.BooleanField(
        default=False,
        help_text="O TI confirmou que a vaga precisa de mais de um equipamento deste tipo.",
    )

    encerrada_em = models.DateTimeField(null=True, blank=True)
    encerrada_por = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    motivo_tipo = models.CharField(max_length=20, choices=Motivo.choices, blank=True, default="")
    motivo = models.TextField(blank=True, default="")
    movimentacao = models.ForeignKey(
        MovimentacaoItem, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="reservas", verbose_name="Movimentação",
        help_text="A entrega que fechou a reserva — ou a movimentação que usou o equipamento em outra necessidade.",
    )
    substituida_por = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="substitui",
    )

    # O alerta "vaga sem equipamento" é por OCORRÊNCIA (uma cessão sem substituto
    # = um e-mail). Estes campos são o que impede o mesmo problema de ser
    # avisado de novo a cada tela aberta ou a cada clique.
    alerta_em = models.DateTimeField(null=True, blank=True)
    alerta_resultado = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["-reservado_em", "-pk"]
        verbose_name = "Reserva de equipamento"
        verbose_name_plural = "Reservas de equipamento"
        constraints = [
            # Um equipamento não pode estar reservado para duas vagas ao mesmo
            # tempo. É o BANCO que garante — dois TIs clicando juntos esbarram
            # no mesmo INSERT, e o segundo vira "já reservado".
            models.UniqueConstraint(
                fields=["item"], condition=models.Q(estado="ativa"),
                name="reserva_ativa_unica_por_item",
            ),
        ]
        indexes = [
            models.Index(fields=["contratacao", "estado"]),
            models.Index(fields=["colaborador", "estado"]),
        ]

    def __str__(self):
        return f"{self.item_rotulo or self.item_id} → {self.vaga_rotulo or self.contratacao_id} ({self.get_estado_display()})"

    @property
    def ativa(self) -> bool:
        return self.estado == self.Estado.ATIVA


class TipoChamadoContratacao(models.TextChoices):
    """Os três chamados que uma vaga pode pedir à TI. Cada um é independente:
    tem a própria mensagem, o próprio destinatário e o próprio estado."""
    MAQUINA = "maquina", "Configuração e entrega de máquina"
    USUARIO_EMAIL = "usuario_email", "Criação de usuário e e-mail"
    SISTEMAS = "sistemas", "Orientação e uso dos sistemas"


class AtendimentoChamadoChoices(models.TextChoices):
    """Andamento do chamado na TI — independente do status da vaga e do estado
    do envio do e-mail.

    "Aberto" quer dizer que a TI registrou o chamado no atendimento (é o que o
    nº do chamado informado na vaga faz); o chamado recém-enviado ainda é
    "Aguardando atendimento". Quem muda a situação é a TI; a única mudança
    automática é a volta para "Aguardando atendimento" quando o RH responde a
    um chamado que esperava por ele (ver `chamado_conversa_service`)."""
    ABERTO = "aberto", "Aberto"
    AGUARDANDO_ATENDIMENTO = "aguardando_atendimento", "Aguardando atendimento"
    EM_ANDAMENTO = "em_andamento", "Em andamento"
    AGUARDANDO_RH = "aguardando_rh", "Aguardando retorno do RH"
    AGUARDANDO_INFORMACOES = "aguardando_informacoes", "Aguardando informações"
    CONCLUIDO = "concluido", "Concluído"
    CANCELADO = "cancelado", "Cancelado"


class StatusChamadoContratacao(models.TextChoices):
    """Estado do ENVIO do e-mail — e só dele.

    "Não solicitado" não é um valor: é a ausência da linha. Nenhum estado diz
    "chamado aberto", de propósito: o sistema só sabe que o servidor de e-mail
    ACEITOU a mensagem, não que alguém abriu um chamado no sistema de
    atendimento (não há integração). A confirmação, quando existe, é o
    `identificador` registrado pelo TI."""
    PENDENTE = "pendente", "Envio pendente"
    ENVIADO = "enviado", "E-mail enviado com sucesso"
    FALHA = "falha", "Falha no envio"


class ContratacaoChamado(AuditModel):
    """Um chamado pedido por e-mail à TI para preparar a estrutura de uma vaga.

    No máximo UM por (vaga, tipo) — `chamado_unico_por_tipo`. É essa restrição
    do BANCO, e não um `if` na view, que impede o clique repetido, a aba
    duplicada e o reenvio de gerar chamado em dobro: o segundo INSERT falha, e
    o service trata a falha como "já solicitado".

    `dados` é o SNAPSHOT do que o solicitante confirmou na janela (cargo,
    setor, necessidades...). A nova tentativa reenvia ESSE conteúdo, não o que a
    vaga diz hoje: o que a TI recebe tem de ser o que foi confirmado.

    `criado_por`/`created_at` (do AuditModel) são QUEM solicitou e QUANDO.
    """

    PENDENTE_EXPIRA = datetime.timedelta(minutes=10)

    contratacao = models.ForeignKey(
        Contratacao, on_delete=models.CASCADE, related_name="chamados",
        verbose_name="Contratação",
    )
    tipo = models.CharField(max_length=20, choices=TipoChamadoContratacao.choices)
    status = models.CharField(
        max_length=10, choices=StatusChamadoContratacao.choices,
        default=StatusChamadoContratacao.PENDENTE, db_index=True,
    )
    dados = models.JSONField(default=dict, blank=True, verbose_name="Dados enviados")
    assunto = models.CharField(max_length=255, blank=True, default="")
    destinatarios = models.TextField(
        blank=True, default="",
        help_text="Quem recebeu na ÚLTIMA tentativa, separado por vírgula.",
    )
    enviado_em = models.DateTimeField(null=True, blank=True)
    tentativas = models.PositiveSmallIntegerField(default=0)
    ultimo_erro = models.TextField(blank=True, default="")

    # Só o TI preenche: sem integração com o sistema de atendimento, é o único
    # jeito de o número do chamado chegar aqui.
    identificador = models.CharField(
        max_length=60, blank=True, default="", verbose_name="Nº do chamado",
        help_text="Registrado pelo TI quando o chamado é aberto no sistema de atendimento.",
    )
    identificador_em = models.DateTimeField(null=True, blank=True)

    # ── Atendimento (a conversa RH ↔ TI) ─────────────────────────────────
    # `status` acima descreve o ENVIO do e-mail de abertura e continua sendo só
    # isso. `atendimento` é o andamento do pedido na TI — outro eixo, de
    # propósito: "o e-mail saiu" e "a máquina foi entregue" são fatos diferentes,
    # e nenhum deles é o status da VAGA (um chamado concluído não preenche a
    # vaga, e preencher a vaga não conclui chamado).
    atendimento = models.CharField(
        max_length=30, choices=AtendimentoChamadoChoices.choices,
        default=AtendimentoChamadoChoices.AGUARDANDO_ATENDIMENTO, db_index=True,
        verbose_name="Situação do atendimento",
    )
    responsavel = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="chamados_contratacao_atendidos", verbose_name="Responsável na TI",
    )
    ultima_atividade_em = models.DateTimeField(
        null=True, blank=True, db_index=True,
        help_text="Última mensagem ou mudança de situação — é o que ordena a fila.",
    )
    # Id de conversa do provedor de e-mail (Graph `conversationId`), aprendido
    # quando a primeira resposta chega. Um dos sinais de vínculo — nunca o único.
    conversation_id = models.CharField(max_length=255, blank=True, default="", db_index=True)

    class Meta:
        ordering = ["created_at"]
        verbose_name = "Chamado da Contratação"
        verbose_name_plural = "Chamados da Contratação"
        constraints = [
            models.UniqueConstraint(
                fields=["contratacao", "tipo"], name="chamado_unico_por_tipo",
            ),
        ]

    def __str__(self):
        return f"{self.get_tipo_display()} — {self.get_status_display()}"

    @property
    def lista_destinatarios(self) -> list:
        return [e.strip() for e in self.destinatarios.split(",") if e.strip()]

    @property
    def confirmado(self) -> bool:
        """O TI registrou o número do chamado. É a ÚNICA coisa que autoriza
        dizer que o chamado existe; e-mail enviado, sozinho, não autoriza."""
        return bool(self.identificador)

    @property
    def interrompido(self) -> bool:
        """Ficou "pendente" além do prazo: o envio não terminou (servidor
        reiniciado no meio, por exemplo) e não se sabe se a mensagem saiu."""
        return (
            self.status == StatusChamadoContratacao.PENDENTE
            and timezone.now() - self.updated_at > self.PENDENTE_EXPIRA
        )

    @property
    def reenviavel(self) -> bool:
        """Só a falha — e o envio interrompido — admitem nova tentativa. O que
        já foi enviado com sucesso nunca é reenviado: seria chamado em dobro."""
        return self.status == StatusChamadoContratacao.FALHA or self.interrompido


class ContratacaoChamadoTentativa(AuditModel):
    """Uma tentativa de envio — a primeira e cada reenvio. É o histórico das
    comunicações: quem tentou, quando, para quem e com que resultado. Nunca é
    apagada nem editada; o estado atual mora em `ContratacaoChamado`."""

    chamado = models.ForeignKey(
        ContratacaoChamado, on_delete=models.CASCADE, related_name="historico",
        verbose_name="Chamado",
    )
    sucesso = models.BooleanField(default=False)
    assunto = models.CharField(max_length=255, blank=True, default="")
    destinatarios = models.TextField(blank=True, default="")
    erro = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Tentativa de envio do chamado"
        verbose_name_plural = "Tentativas de envio do chamado"
        indexes = [models.Index(fields=["chamado", "-created_at"])]

    def __str__(self):
        return f"{'ok' if self.sucesso else 'falha'} — {self.created_at:%d/%m/%Y %H:%M}"


class SuporteSistemasChoices(models.TextChoices):
    """O que o solicitante pede no chamado de sistemas. Não é campo de modelo:
    vira texto no snapshot `ContratacaoChamado.dados`, então mudar a lista não
    exige migration nem altera o que já foi enviado."""
    ORIENTACAO = "orientacao", "Orientação de uso dos sistemas"
    CONFIGURACAO = "configuracao", "Configuração do acesso"
    LIBERACAO = "liberacao", "Liberação de acesso"


# ─────────────────────────────────────────────────────────────────────────────
# Conversa RH ↔ TI de cada chamado (e-mail nos dois sentidos)
# ─────────────────────────────────────────────────────────────────────────────

class ArmazenamentoChamados(FileSystemStorage):
    """Disco PRIVADO dos anexos e dos e-mails brutos dos chamados.

    Fica fora de `MEDIA_ROOT` e de `STATIC_ROOT` de propósito: nada ali é
    alcançável por URL. O único caminho até o arquivo é a view autenticada de
    download, que confere se a pessoa pode ver o chamado. O local é lido de
    `settings.CHAMADOS_ARQUIVOS_ROOT` a cada uso (e não fixado na construção)
    para o teste poder apontar para um diretório temporário."""

    @property
    def base_location(self):
        return settings.CHAMADOS_ARQUIVOS_ROOT

    @property
    def location(self):
        return os.path.abspath(self.base_location)


class ChamadoMensagem(AuditModel):
    """Uma mensagem da conversa de um chamado — enviada pela plataforma,
    recebida por e-mail, ou o e-mail de abertura.

    É o REGISTRO: nunca é editada nem apagada pela tela. O que mudou depois
    (falha de entrega, nova tentativa) é anotado nos campos de estado, não
    reescrevendo o conteúdo.

    O estado diz só o que o sistema SABE:
      • `enviada`  — o servidor SMTP aceitou. NÃO é "entregue" nem "lida";
      • `falha`    — o servidor recusou (ou o envio não terminou);
      • `recebida` — o e-mail foi capturado da caixa e registrado aqui;
      • `entrega_recusada` — chegou um aviso de não entrega (DSN) citando esta
        mensagem. É a única evidência de entrega que a caixa fornece.
    """

    class Direcao(models.TextChoices):
        SAIDA = "saida", "Enviada"
        ENTRADA = "entrada", "Recebida"

    class Origem(models.TextChoices):
        PLATAFORMA = "plataforma", "Pela plataforma"
        EMAIL = "email", "Por e-mail"

    class Lado(models.TextChoices):
        RH = "rh", "RH"
        TI = "ti", "TI"
        OUTRO = "outro", "Outro participante"

    class Tipo(models.TextChoices):
        ABERTURA = "abertura", "Solicitação"
        MENSAGEM = "mensagem", "Mensagem"

    class Estado(models.TextChoices):
        PENDENTE = "pendente", "Envio pendente"
        ENVIADA = "enviada", "E-mail enviado"
        FALHA = "falha", "Falha no envio"
        RECEBIDA = "recebida", "Recebida por e-mail"

    PENDENTE_EXPIRA = datetime.timedelta(minutes=10)

    chamado = models.ForeignKey(
        ContratacaoChamado, on_delete=models.CASCADE, related_name="mensagens",
    )
    tipo = models.CharField(max_length=10, choices=Tipo.choices, default=Tipo.MENSAGEM)
    direcao = models.CharField(max_length=10, choices=Direcao.choices)
    origem = models.CharField(max_length=12, choices=Origem.choices)
    lado = models.CharField(max_length=6, choices=Lado.choices)
    estado = models.CharField(max_length=10, choices=Estado.choices, db_index=True)

    autor = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="mensagens_de_chamado",
        help_text="Vazio quando o remetente não é um usuário do sistema (resposta por e-mail).",
    )
    remetente_nome = models.CharField(max_length=150, blank=True, default="")
    remetente_email = models.CharField(max_length=254, blank=True, default="")
    destinatarios = models.TextField(blank=True, default="")
    assunto = models.CharField(max_length=255, blank=True, default="")
    corpo = models.TextField(blank=True, default="")

    # Cabeçalhos RFC 5322 que mantêm a conversa costurada. `message_id` é a
    # CHAVE de deduplicação (única quando preenchida): a mesma mensagem lida
    # duas vezes da caixa, ou reenviada após uma falha, nunca vira duas linhas.
    message_id = models.CharField(max_length=255, blank=True, default="", db_index=True)
    in_reply_to = models.CharField(max_length=255, blank=True, default="")
    referencias = models.TextField(blank=True, default="")

    enviado_em = models.DateTimeField(null=True, blank=True)
    recebido_em = models.DateTimeField(null=True, blank=True)
    tentativas = models.PositiveSmallIntegerField(default=0)
    erro = models.TextField(blank=True, default="")
    entrega_recusada = models.BooleanField(default=False)
    entrega_erro = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["created_at", "pk"]
        verbose_name = "Mensagem do Chamado"
        verbose_name_plural = "Mensagens dos Chamados"
        constraints = [
            models.UniqueConstraint(
                fields=["message_id"], condition=~models.Q(message_id=""),
                name="chamado_mensagem_id_unico",
            ),
        ]
        indexes = [models.Index(fields=["chamado", "created_at"])]

    def __str__(self):
        return f"{self.get_lado_display()} · {self.get_estado_display()} · {self.assunto[:40]}"

    @property
    def reenviavel(self) -> bool:
        """Só a saída que falhou (ou ficou pendente além do prazo) admite nova
        tentativa — o que foi aceito pelo servidor nunca é reenviado."""
        if self.direcao != self.Direcao.SAIDA:
            return False
        if self.estado == self.Estado.FALHA:
            return True
        return (
            self.estado == self.Estado.PENDENTE
            and timezone.now() - self.updated_at > self.PENDENTE_EXPIRA
        )


class ChamadoAnexo(AuditModel):
    """Arquivo de uma mensagem. Quando o arquivo foi BLOQUEADO (tipo proibido,
    grande demais, conteúdo que não bate com a extensão) a linha existe mesmo
    assim — sem arquivo — para a conversa dizer que havia um anexo e por que ele
    não está disponível, em vez de sumir calada."""

    mensagem = models.ForeignKey(ChamadoMensagem, on_delete=models.CASCADE, related_name="anexos")
    arquivo = models.FileField(
        storage=ArmazenamentoChamados(), upload_to="anexos/%Y/%m/", blank=True, max_length=255,
    )
    nome_original = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100, blank=True, default="")
    tamanho = models.PositiveBigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True, default="")
    bloqueado = models.BooleanField(default=False)
    motivo_bloqueio = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["pk"]
        verbose_name = "Anexo do Chamado"
        verbose_name_plural = "Anexos dos Chamados"

    def __str__(self):
        return self.nome_original


class ChamadoStatusLog(AuditModel):
    """Cada mudança da situação do atendimento: de → para, quem, quando, por
    quê. `automatico` marca a única mudança que o sistema faz sozinho (resposta
    do RH reabrindo a fila da TI). Nunca é editada."""

    chamado = models.ForeignKey(ContratacaoChamado, on_delete=models.CASCADE, related_name="historico_status")
    de = models.CharField(max_length=30, choices=AtendimentoChamadoChoices.choices, blank=True, default="")
    para = models.CharField(max_length=30, choices=AtendimentoChamadoChoices.choices)
    motivo = models.CharField(max_length=255, blank=True, default="")
    automatico = models.BooleanField(default=False)

    class Meta:
        ordering = ["created_at", "pk"]
        verbose_name = "Mudança de situação do chamado"
        verbose_name_plural = "Mudanças de situação dos chamados"


class ChamadoEmailRecebido(AuditModel):
    """Cada e-mail lido da caixa — o que se fez com ele e, quando não deu certo,
    por quê. É o que torna a sincronização RECUPERÁVEL:

      • o e-mail bruto fica guardado, então reprocessar uma falha não depende de
        a caixa ainda tê-lo;
      • `provider_id` (id da mensagem no provedor) e `message_id` impedem que o
        mesmo e-mail vire duas mensagens, venha da leitura repetida ou do
        reprocessamento;
      • `quarentena` guarda o que parece ser do chamado mas não passou nas
        verificações (remetente desconhecido, falha de SPF/DKIM, vínculo
        ambíguo) para o TI decidir — nada disso aparece na conversa antes.

    E-mail sem relação com chamado (`ignorado`) guarda só os identificadores,
    para não ser baixado de novo a cada leitura; assunto, remetente e conteúdo
    de e-mail que não é nosso não são retidos."""

    class Estado(models.TextChoices):
        PROCESSADO = "processado", "Registrado no chamado"
        QUARENTENA = "quarentena", "Aguardando revisão do TI"
        FALHA = "falha", "Falha na sincronização"
        IGNORADO = "ignorado", "Sem relação com chamado"
        DESCARTADO = "descartado", "Descartado pelo TI"

    provider_id = models.CharField(max_length=255, blank=True, default="", db_index=True)
    message_id = models.CharField(max_length=255, blank=True, default="", db_index=True)
    conversation_id = models.CharField(max_length=255, blank=True, default="")
    estado = models.CharField(max_length=12, choices=Estado.choices, db_index=True)
    motivo = models.TextField(blank=True, default="")
    tentativas = models.PositiveSmallIntegerField(default=0)
    recebido_em = models.DateTimeField(null=True, blank=True)
    remetente = models.CharField(max_length=254, blank=True, default="")
    assunto = models.CharField(max_length=255, blank=True, default="")
    bruto = models.FileField(
        storage=ArmazenamentoChamados(), upload_to="brutos/%Y/%m/", blank=True, max_length=255,
    )
    chamado = models.ForeignKey(
        ContratacaoChamado, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="emails_recebidos",
    )
    mensagem = models.OneToOneField(
        ChamadoMensagem, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="origem_email",
    )
    resolvido_por = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="emails_de_chamado_resolvidos",
    )
    resolvido_em = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "E-mail recebido do chamado"
        verbose_name_plural = "E-mails recebidos dos chamados"
        constraints = [
            models.UniqueConstraint(
                fields=["provider_id"], condition=~models.Q(provider_id=""),
                name="chamado_email_provider_unico",
            ),
        ]


class ChamadoCaixaEstado(models.Model):
    """Cursor e saúde da sincronização com a caixa de e-mail — uma linha só.

    `marca_dagua` é o `receivedDateTime` do último e-mail tratado; cada leitura
    recomeça um pouco ANTES dela (sobreposição), e a deduplicação por
    `provider_id`/`message_id` faz a sobreposição ser inofensiva. É assim que um
    e-mail que chegou "fora de ordem" não fica para trás."""

    marca_dagua = models.DateTimeField(null=True, blank=True)
    ultima_execucao = models.DateTimeField(null=True, blank=True)
    ultimo_sucesso = models.DateTimeField(null=True, blank=True)
    ultimo_erro = models.TextField(blank=True, default="")
    ultimo_resumo = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        verbose_name = "Estado da caixa de chamados"

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


# ═════════════════════════════════════════════════════════════════════════════
# Monitoramento TIM M2M
#
# Regras que moldam estes models (a lógica está em services/tim_m2m_service.py,
# m2m_interpretacao.py e m2m_monitoramento_service.py):
#
#   • A resposta da API é guardada CRUA (`M2MConsulta.payload`). Só é
#     interpretada se `M2MIntegracao.mapeamento` disser como — a documentação da
#     TIM ainda não foi obtida, então nenhum campo é presumido.
#   • SITUAÇÃO ATUAL ≠ HISTÓRICO. O chip guarda o último dado VÁLIDO
#     (`sessao_consulta`, `consumo_consulta`); a falha de uma consulta só mexe nos
#     contadores de falha e NUNCA nesse dado. O histórico (`M2MConsulta`) é
#     imutável e o chip é PROTECT: nada daqui é apagado, só desativado.
#   • Falha de consulta nunca vira "chip indisponível": quem falhou foi a
#     consulta (rede, credencial, API, identificador), não necessariamente o chip.
# ═════════════════════════════════════════════════════════════════════════════

class M2MIntegracao(models.Model):
    """Configuração e saúde da integração com a API TIM M2M — uma linha só
    (`get()`), no molde de `ChamadoCaixaEstado`.

    O token NUNCA é guardado em claro: `token_cifrado` é Fernet (chave em
    `TIM_M2M_CHAVE_CIFRA`, no .env) e a tela só mostra `token_sufixo`. Se
    `TIM_M2M_TOKEN` existir no .env, ele vence este campo
    (`services/tim_m2m_service.token()`).
    """

    # ── credencial ──
    token_cifrado = models.TextField(blank=True, default="")
    token_sufixo = models.CharField(max_length=8, blank=True, default="")
    token_atualizado_em = models.DateTimeField(null=True, blank=True)
    token_atualizado_por = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )

    # ── frequência (a tarefa do Agendador é só o "tique"; quem manda é isto) ──
    # Dois ritmos: chip com SESSÃO ATIVA é onde o dado muda (consumo e sessão andam) e merece o ritmo curto;
    # chip sem sessão fica no ritmo de repouso. O que cabe de fato é decidido pelo limite de requisições
    # por minuto (`limite_req_minuto`), que o ciclo respeita em qualquer configuração: o intervalo é o
    # DESEJADO, e a tela mostra o efetivo.
    intervalo_minutos = models.PositiveIntegerField(
        default=1, validators=[MinValueValidator(1)],
        help_text="De quanto em quanto tempo um chip com sessão ativa é consultado (desejado; o limite de "
                  "requisições da API pode alongar). Mínimo de 1 minuto.",
    )
    intervalo_repouso_minutos = models.PositiveIntegerField(
        default=15, validators=[MinValueValidator(1)],
        help_text="De quanto em quanto tempo um chip SEM sessão ativa é consultado. Mínimo de 1 minuto.",
    )
    pausa_entre_chamadas_ms = models.PositiveIntegerField(default=1000)
    limite_req_minuto = models.PositiveIntegerField(
        default=30,
        help_text="Teto de requisições por minuto. Conservador até a TIM informar o limite real.",
    )
    max_chips_execucao = models.PositiveIntegerField(default=100)
    tique_minutos = models.PositiveSmallIntegerField(
        default=15,
        help_text="Intervalo da tarefa do Agendador (informado pelo comando). Só serve para dizer se a tarefa parou.",
    )

    # ── limites de alerta ──
    falhas_para_alerta = models.PositiveSmallIntegerField(default=3)
    horas_sem_sessao_alerta = models.PositiveIntegerField(default=24)
    horas_desatualizado = models.PositiveIntegerField(default=24)
    pct_consumo_alerta = models.PositiveSmallIntegerField(default=80)

    # ── interpretação (ver services/m2m_interpretacao.py) ──
    mapeamento = models.JSONField(default=dict, blank=True)
    dados_por_eventos = models.BooleanField(
        default=True,
        help_text=(
            "Lê as sessões de dados do chip na plataforma (os mesmos eventos da aba Dados do portal) para dizer se "
            "há sessão ativa e quanto foi consumido. Desligado, só vale o resumo do SIM Card, que na prática "
            "demora para refletir o uso (ver a regra 61 do CLAUDE.md)."
        ),
    )

    # ── saúde ──
    ultima_execucao = models.DateTimeField(null=True, blank=True)
    ultimo_sucesso = models.DateTimeField(null=True, blank=True)
    ultimo_erro = models.TextField(blank=True, default="")
    ultimo_status_http = models.PositiveSmallIntegerField(null=True, blank=True)
    autenticada_em = models.DateTimeField(null=True, blank=True)       # último 2xx
    falha_autenticacao_em = models.DateTimeField(null=True, blank=True)  # último 401/403
    bloqueado_ate = models.DateTimeField(null=True, blank=True)          # 429: não consulta até aqui
    executando_desde = models.DateTimeField(null=True, blank=True)       # trava contra execução sobreposta
    proxima_execucao = models.DateTimeField(null=True, blank=True)
    ultimo_teste = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = "Integração TIM M2M"
        verbose_name_plural = "Integração TIM M2M"

    def __str__(self):
        return "Integração TIM M2M"

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


def operadora_padrao_id():
    """`default` de `M2MChip.operadora`: a operadora com integração TIM M2M (a que o
    módulo nasceu atendendo). `None` se ela foi apagada — aí o INSERT falha e quem
    chamou precisa escolher uma operadora. Fica no módulo (e não em lambda) porque
    a migration 0190 referencia este nome."""
    return (
        ConectividadeOperadora.objects.filter(integracao=ConectividadeOperadora.Integracao.TIM_M2M)
        .order_by("pk").values_list("pk", flat=True).first()
    )


class ConectividadeOperadora(models.Model):
    """Operadora de chips. É a costura para outras operadoras além da TIM: o ciclo
    de monitoramento só consulta chips cuja operadora tem `integracao="tim_m2m"`; os
    demais aparecem como "sem integração — não consultado pela API". `integracao`
    vazia = operadora cadastrada só para organizar o parque (sem consulta)."""

    class Integracao(models.TextChoices):
        TIM_M2M = "tim_m2m", "TIM M2M (API)"

    nome = models.CharField(max_length=80, unique=True)
    integracao = models.CharField(
        max_length=20, choices=Integracao.choices, blank=True, default="",
        help_text="Vazio = sem integração: os chips são cadastrados e controlados, mas não consultados pela API.",
    )
    ativo = models.BooleanField(default=True)

    class Meta:
        ordering = ["nome"]
        verbose_name = "Operadora de conectividade"
        verbose_name_plural = "Operadoras de conectividade"

    def __str__(self):
        return self.nome

    @property
    def tem_integracao(self) -> bool:
        return bool(self.integracao)


# Permissões do módulo. A fonte é esta lista (`Meta.permissions` dos dois models as
# usa); a migration 0191 repete os mesmos códigos e um teste confere que não divergem.
PERMISSOES_FROTA_VEICULO = [
    ("ver_frota", "Pode ver os veículos e equipamentos da frota"),
    ("editar_frota", "Pode cadastrar e editar veículos e equipamentos da frota"),
]
PERMISSOES_FROTA_CHIP = [
    ("ver_chips", "Pode ver os chips de conectividade"),
    ("importar_chips", "Pode cadastrar chips (individualmente ou por planilha)"),
    ("vincular_chips", "Pode vincular chips a veículos"),
    ("transferir_chips", "Pode desvincular, transferir, substituir, devolver à disponibilidade e inativar chips"),
    ("consultar_conectividade", "Pode ver sessão e consumo dos chips e atualizar a consulta"),
    ("ver_historico_frota", "Pode ver o histórico de movimentações, os alertas e as ocorrências"),
    ("tratar_ocorrencias", "Pode tratar as ocorrências de conectividade"),
    ("administrar_frota_config", "Pode administrar a integração, os tipos de veículo, as operadoras e as regras de alerta"),
]
# Inventário de Frotas e Maquinários (planilha do ERP, validação, relatórios). Lista À PARTE de
# `PERMISSOES_FROTA_VEICULO` de propósito: aquela é o retrato da migration 0191 (um teste confere
# que as duas não divergem); estas nascem na 0198.
PERMISSOES_FROTA_INVENTARIO = [
    ("importar_equipamentos_frota", "Pode importar equipamentos motorizados por planilha"),
    ("validar_inventario_frota", "Pode validar o inventário: a situação e o cadastro dos equipamentos importados"),
    ("exportar_frota", "Pode exportar a frota e emitir os relatórios de frotas e maquinários"),
]


class M2MChip(AuditModel):
    """Um chip de conectividade acompanhado (hoje TIM M2M; a operadora é um cadastro).
    O cadastro é do TI; a SITUAÇÃO ATUAL (sessão, consumo, falhas) é preenchida só pelo
    serviço de monitoramento e a SITUAÇÃO DE USO (`situacao_uso`) só pelo serviço de
    vínculo (`services/frota_vinculo_service.py`) — ninguém a edita à mão."""

    class Identificador(models.TextChoices):
        ICCID = "iccid", "ICCID"
        IMSI = "imsi", "IMSI"

    class SituacaoUso(models.TextChoices):
        DISPONIVEL = "disponivel", "Disponível"
        VINCULADO = "vinculado", "Vinculado"
        MANUTENCAO = "manutencao", "Em manutenção"
        INATIVO = "inativo", "Inativo"

    class SessaoEstado(models.TextChoices):
        ATIVA = "ativa", "Sessão ativa"
        SEM_SESSAO = "sem_sessao", "Sem sessão"
        NAO_INTERPRETADO = "nao_interpretado", "Não interpretado"
        NAO_CONSULTADO = "nao_consultado", "Não consultado"

    # ── cadastro ──
    nome = models.CharField(max_length=120, unique=True)
    iccid = models.CharField(max_length=20, blank=True, default="")
    imsi = models.CharField(max_length=15, blank=True, default="")
    identificador_consulta = models.CharField(
        max_length=5, choices=Identificador.choices, default=Identificador.ICCID,
        help_text="Qual identificador vai na URL da consulta. O escolhido precisa estar preenchido.",
    )
    descricao = models.CharField(max_length=255, blank=True, default="")
    # O "equipamento" do chip é o VEÍCULO ou o tablet/celular, pelo vínculo (`FrotaVinculo`). O antigo FK direto para
    # `Item` saiu na migration 0190; a ligação de HOJE é outra coisa: `item_ti` é o PATRIMÔNIO do chip no estoque de TI
    # (o `Item` do subtipo "É chip": valor, nota fiscal, termo de responsabilidade, status). A chave da ligação é o
    # ICCID (= número de série do item). Só REFERÊNCIA: o módulo lê o item e nunca o grava (regra 66).
    item_ti = models.OneToOneField(
        "Item", on_delete=models.PROTECT, null=True, blank=True, related_name="chip_conectividade",
        verbose_name="Item no estoque de TI",
        help_text="O chip como PATRIMÔNIO no estoque de TI. Ligação 1 para 1: um item só tem um chip, e um chip só tem um item.",
    )
    item_ti_em = models.DateTimeField(null=True, blank=True)
    item_ti_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    item_ti_origem = models.CharField(
        max_length=16, blank=True, default="",
        choices=[
            ("manual", "Ligado à mão"),
            ("iccid", "Mesmo ICCID (automático)"),
            ("chip_do_item", "Chip criado a partir do item"),
            ("item_do_chip", "Item criado a partir do chip"),
        ],
        help_text="Como a ligação nasceu; vazio quando não há item.",
    )
    operadora = models.ForeignKey(
        "ConectividadeOperadora", on_delete=models.PROTECT, related_name="chips", default=operadora_padrao_id,
    )
    numero_linha = models.CharField(
        max_length=15, blank=True, default="",
        help_text="Número da linha, só dígitos (com DDD). Opcional; não é único.",
    )
    situacao_uso = models.CharField(
        max_length=12, choices=SituacaoUso.choices, default=SituacaoUso.DISPONIVEL, db_index=True,
        help_text="Gravada só pelo serviço de vínculo. Não confundir com `monitorado` (consulta à API).",
    )
    localidade = models.ForeignKey(
        Localidade, on_delete=models.SET_NULL, null=True, blank=True, related_name="chips_m2m",
    )
    setor = models.CharField(max_length=100, blank=True, default="")
    responsavel = models.ForeignKey(
        Usuario, on_delete=models.SET_NULL, null=True, blank=True, related_name="chips_m2m",
        verbose_name="Responsável",
    )
    observacoes = models.TextField(blank=True, default="")
    limite_mensal_bytes = models.PositiveBigIntegerField(
        null=True, blank=True,
        help_text="Opcional. É o ÚNICO gatilho do alerta de consumo elevado.",
    )

    # ── monitoramento ──
    monitorado = models.BooleanField(default=True, db_index=True)
    desativado_em = models.DateTimeField(null=True, blank=True)
    desativado_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    desativado_motivo = models.CharField(max_length=255, blank=True, default="")

    # ── situação atual (derivada das consultas; nunca editada à mão) ──
    sessao_estado = models.CharField(
        max_length=20, choices=SessaoEstado.choices, default=SessaoEstado.NAO_CONSULTADO,
    )
    sessao_consulta = models.ForeignKey(
        "M2MConsulta", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    consumo_consulta = models.ForeignKey(
        "M2MConsulta", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    consumo_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    ultima_valida_em = models.DateTimeField(null=True, blank=True)
    ultima_tentativa_em = models.DateTimeField(null=True, blank=True)
    ultima_falha_em = models.DateTimeField(null=True, blank=True)
    ultima_falha_tipo = models.CharField(max_length=20, blank=True, default="")
    falhas_consecutivas = models.PositiveIntegerField(default=0)
    proxima_consulta_em = models.DateTimeField(null=True, blank=True)
    # Até onde as sessões de dados (`M2MSessaoDados`) já foram lidas na plataforma: o dia mais antigo
    # já baixado por inteiro e a última leitura. Só o serviço de dados grava.
    dados_desde = models.DateField(null=True, blank=True)
    dados_sincronizados_em = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["nome"]
        verbose_name = "Chip de conectividade"
        verbose_name_plural = "Chips de conectividade"
        permissions = PERMISSOES_FROTA_CHIP
        constraints = [
            models.UniqueConstraint(
                fields=["iccid"], condition=~models.Q(iccid=""), name="m2m_chip_iccid_unico",
            ),
            models.UniqueConstraint(
                fields=["imsi"], condition=~models.Q(imsi=""), name="m2m_chip_imsi_unico",
            ),
            models.CheckConstraint(
                condition=(
                    (models.Q(identificador_consulta="iccid") & ~models.Q(iccid=""))
                    | (models.Q(identificador_consulta="imsi") & ~models.Q(imsi=""))
                ),
                name="m2m_chip_identificador_preenchido",
            ),
        ]
        indexes = [models.Index(fields=["monitorado", "proxima_consulta_em"], name="m2m_chip_devidos_idx")]

    def __str__(self):
        return self.nome

    @property
    def identificador(self) -> str:
        """O valor que vai na URL da consulta (ICCID ou IMSI, conforme `identificador_consulta`)."""
        return self.iccid if self.identificador_consulta == self.Identificador.ICCID else self.imsi

    def clean(self):
        erros = {}
        if self.identificador_consulta == self.Identificador.ICCID and not self.iccid:
            erros["iccid"] = "Informe o ICCID, ou escolha o IMSI como identificador de consulta."
        if self.identificador_consulta == self.Identificador.IMSI and not self.imsi:
            erros["imsi"] = "Informe o IMSI, ou escolha o ICCID como identificador de consulta."
        if erros:
            raise ValidationError(erros)

    @property
    def consultavel_pela_api(self) -> bool:
        """A operadora do chip tem integração (hoje só a TIM M2M). Sem ela o chip é
        cadastrado e controlado, mas NUNCA consultado. Em lista, carregue a
        operadora com `select_related("operadora")`."""
        return self.operadora.integracao == ConectividadeOperadora.Integracao.TIM_M2M

    def situacao_monitoramento_com(self, horas_desatualizado: int, agora=None) -> str:
        """`desativado` / `sem_integracao` (operadora sem API) / `pendente` (nunca
        consultado) / `erro_consulta` (a última tentativa falhou) / `desatualizado`
        (último dado válido velho demais) / `normal`. Recebe o limite em horas para a
        lista não consultar a configuração uma vez por linha."""
        agora = agora or timezone.now()
        if not self.monitorado:
            return "desativado"
        if not self.consultavel_pela_api:
            return "sem_integracao"
        if self.falhas_consecutivas > 0:
            return "erro_consulta"
        if self.ultima_tentativa_em is None:
            return "pendente"
        referencia = self.ultima_valida_em or self.created_at
        if referencia is not None and agora - referencia > timedelta(hours=horas_desatualizado):
            return "desatualizado"
        return "normal"

    @property
    def situacao_monitoramento(self) -> str:
        """Atalho de `situacao_monitoramento_com` que lê a configuração. Em LISTA,
        chame `situacao_monitoramento_com` com o limite já carregado (evita 1 consulta por linha)."""
        return self.situacao_monitoramento_com(M2MIntegracao.get().horas_desatualizado)


class M2MConsulta(models.Model):
    """Cada chamada à API — IMUTÁVEL: nunca editada, nunca apagada (o chip é
    PROTECT). É o histórico; o que o chip mostra "agora" aponta para a última
    consulta válida de cada tipo.

    `payload` é a resposta crua (até 64 KB; acima disso é truncada com marcação).
    Os campos normalizados só são preenchidos quando o mapeamento existe e o parse
    deu certo — e podem ser RECALCULADOS (`reinterpretar_historico`), nunca o payload.
    """

    class Tipo(models.TextChoices):
        SESSAO = "sessao", "Sessão"
        CONSUMO = "consumo", "Consumo"

    class Origem(models.TextChoices):
        MANUAL = "manual", "Manual"
        AGENDADA = "agendada", "Agendada"
        TESTE = "teste", "Teste de conexão"

    class Resultado(models.TextChoices):
        SUCESSO = "sucesso", "Sucesso"
        SUCESSO_SEM_CORPO = "sucesso_sem_corpo", "Sucesso sem conteúdo"
        FALHA_AUTENTICACAO = "falha_autenticacao", "Credencial recusada"
        NAO_ENCONTRADO = "nao_encontrado", "Não encontrado pela API"
        LIMITE_EXCEDIDO = "limite_excedido", "Limite de requisições excedido"
        TIMEOUT = "timeout", "Tempo esgotado"
        ERRO_REDE = "erro_rede", "Erro de rede"
        INDISPONIVEL = "indisponivel", "API indisponível"
        RESPOSTA_INVALIDA = "resposta_invalida", "Resposta inválida"

    RESULTADOS_VALIDOS = ("sucesso", "sucesso_sem_corpo")
    # Os únicos campos que `reinterpretar_historico` pode regravar numa consulta já existente.
    CAMPOS_NORMALIZADOS = (
        "valor_original", "unidade_original", "volume_bytes",
        "periodo_inicio", "periodo_fim", "estado_bruto", "estado_interpretado",
    )

    chip = models.ForeignKey(M2MChip, on_delete=models.PROTECT, related_name="consultas")
    tipo = models.CharField(max_length=10, choices=Tipo.choices)
    origem = models.CharField(max_length=10, choices=Origem.choices)
    usuario = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    iniciada_em = models.DateTimeField(default=timezone.now)
    duracao_ms = models.PositiveIntegerField(null=True, blank=True)
    tentativas = models.PositiveSmallIntegerField(default=1)
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    identificador_tipo = models.CharField(max_length=5, choices=M2MChip.Identificador.choices)
    resultado = models.CharField(max_length=20, choices=Resultado.choices)
    payload = models.JSONField(null=True, blank=True)
    mensagem_erro = models.TextField(blank=True, default="")

    # ── normalizados (só com mapeamento e parse bem-sucedido) ──
    valor_original = models.CharField(max_length=100, blank=True, default="")
    unidade_original = models.CharField(max_length=40, blank=True, default="")
    volume_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    periodo_inicio = models.DateTimeField(null=True, blank=True)
    periodo_fim = models.DateTimeField(null=True, blank=True)
    estado_bruto = models.CharField(max_length=100, blank=True, default="")
    estado_interpretado = models.CharField(max_length=20, blank=True, default="")

    class Meta:
        ordering = ["-iniciada_em", "-pk"]
        verbose_name = "Consulta TIM M2M"
        verbose_name_plural = "Consultas TIM M2M"
        indexes = [
            models.Index(fields=["chip", "tipo", "-iniciada_em"], name="m2m_consulta_chip_tipo_idx"),
            models.Index(fields=["-iniciada_em"], name="m2m_consulta_recente_idx"),
        ]

    def __str__(self):
        return f"{self.get_tipo_display()} · {self.chip_id} · {self.get_resultado_display()}"

    @property
    def valida(self) -> bool:
        return self.resultado in self.RESULTADOS_VALIDOS

    def save(self, *args, **kwargs):
        if not self._state.adding:
            campos = kwargs.get("update_fields")
            if not campos or not set(campos) <= set(self.CAMPOS_NORMALIZADOS):
                raise ValidationError(
                    "A consulta é histórico imutável: só os campos normalizados podem ser recalculados."
                )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("A consulta é histórico imutável e não pode ser apagada.")


class M2MSessaoDados(models.Model):
    """Uma SESSÃO DE DADOS do chip na plataforma — a mesma linha da aba "Dados" do portal da Saitro
    (`GET /simcards/{id}/consumo/dados/{AAAA-MM-DD}/`). É daqui que saem "há sessão ativa?" e o
    consumo: o resumo do SIM Card (`conexao`, `servicos.dados.consumo`) demora a refletir o uso.

    `id_evento` é o id da PLATAFORMA e identifica a linha: a mesma sessão volta a cada leitura, com a
    duração e os bytes maiores (a plataforma atualiza uma sessão aberta de ~15 em ~15 min), e a
    leitura só a ATUALIZA. `fim` vazio é a sessão em andamento ("Online" no portal). Os bytes são os
    do evento, exatos; o portal os mostra em base 1024.

    Não é histórico imutável como `M2MConsulta`: é o espelho do que a plataforma informa, e a
    plataforma corrige a própria linha."""

    chip = models.ForeignKey(M2MChip, on_delete=models.CASCADE, related_name="sessoes_dados")
    id_evento = models.BigIntegerField(unique=True)
    inicio = models.DateTimeField()
    fim = models.DateTimeField(null=True, blank=True)
    duracao_s = models.PositiveIntegerField(default=0)
    ip = models.CharField(max_length=45, blank=True, default="")
    apn = models.CharField(max_length=100, blank=True, default="")
    tecnologia = models.CharField(max_length=30, blank=True, default="")
    location_info = models.CharField(max_length=100, blank=True, default="")
    upload_bytes = models.PositiveBigIntegerField(default=0)
    download_bytes = models.PositiveBigIntegerField(default=0)
    total_bytes = models.PositiveBigIntegerField(default=0)
    lido_em = models.DateTimeField(default=timezone.now, help_text="Quando o sistema leu esta linha pela última vez.")

    class Meta:
        ordering = ["-inicio", "-id_evento"]
        verbose_name = "Sessão de dados"
        verbose_name_plural = "Sessões de dados"
        indexes = [models.Index(fields=["chip", "-inicio"], name="m2m_sessdados_chip_idx")]

    def __str__(self):
        return f"{self.chip_id} · {self.inicio:%d/%m/%Y %H:%M}"

    @property
    def em_andamento(self) -> bool:
        return self.fim is None


class M2MExecucao(models.Model):
    """Uma execução do ciclo de monitoramento (o que a tarefa do Agendador roda a cada tique): quando,
    quantos chips, quantas chamadas à API, o que mudou e como terminou. É o registro que responde "o
    sistema está sincronizando sozinho?" sem depender de alguém ter a tela aberta.

    Só nasce execução que de fato começou a consultar; ciclo que pulou (sem token, credencial recusada,
    API pausada, outra execução em andamento) fica em `M2MIntegracao` — um ciclo a cada minuto que apenas
    pula encheria a tabela sem dizer nada. Os números são do próprio ciclo, gravados no fim; execução
    `em_andamento` há mais de `M2MIntegracao`'s trava é uma que morreu no meio (a próxima assume).

    Retenção: `monitoramento_service.podar_execucoes` mantém os últimos 7 dias (o painel só mostra os
    recentes), e a poda é a única exceção ao "histórico nunca é apagado" do módulo — as consultas, os
    eventos e as sessões, que são o dado de verdade, continuam intactos."""

    class Estado(models.TextChoices):
        EM_ANDAMENTO = "em_andamento", "Em andamento"
        CONCLUIDA = "concluida", "Concluída"
        COM_FALHAS = "com_falhas", "Concluída com falhas"
        INTERROMPIDA = "interrompida", "Interrompida"
        ERRO = "erro", "Erro"

    origem = models.CharField(max_length=20, default="agendada")
    estado = models.CharField(max_length=20, choices=Estado.choices, default=Estado.EM_ANDAMENTO, db_index=True)
    iniciada_em = models.DateTimeField(default=timezone.now, db_index=True)
    concluida_em = models.DateTimeField(null=True, blank=True)
    duracao_ms = models.PositiveIntegerField(default=0)
    vencidos = models.PositiveIntegerField(default=0, help_text="Chips vencidos quando o ciclo começou.")
    consultados = models.PositiveIntegerField(default=0)
    sucessos = models.PositiveIntegerField(default=0)
    falhas = models.PositiveIntegerField(default=0)
    chamadas = models.PositiveIntegerField(default=0, help_text="Requisições HTTP feitas à API (a lista completa e as sessões de dados contam).")
    pendentes = models.PositiveIntegerField(default=0, help_text="Chips que ficaram vencidos para o próximo ciclo (limite de tempo ou de requisições).")
    interrompido_por = models.CharField(max_length=30, blank=True, default="")
    erro = models.TextField(blank=True, default="")
    mudancas = models.JSONField(default=dict, blank=True, help_text="O que este ciclo atualizou: estado de sessão, consumo, sessões de dados.")

    class Meta:
        ordering = ["-iniciada_em", "-id"]
        verbose_name = "Execução do monitoramento"
        verbose_name_plural = "Execuções do monitoramento"

    def __str__(self):
        return f"Execução {self.pk} · {self.iniciada_em:%d/%m %H:%M}"

    @property
    def duracao_s(self) -> float:
        """A duração em segundos (uma casa), para a tela."""
        return round(self.duracao_ms / 1000, 1)


class M2MOcorrencia(models.Model):
    """Alerta do módulo. Dois tipos de origem:

    • CONDIÇÃO (falhas, sem sessão, desatualizado, consumo, identificador, veículo sem
      chip, veículo sem atualização): abre quando a condição fica verdadeira e resolve
      sozinha quando ela some. No máximo UMA ocorrência em aberto (aberta/em
      tratamento) por (chip, tipo) — ou, nas que não têm chip (`veiculo_sem_chip`,
      `veiculo_sem_atualizacao`), por (veículo, tipo). As duas restrições condicionais
      abaixo são a deduplicação.
    • EVENTO (`chip_desvinculado`, `alteracao_vinculo`): informativo, nasce de uma
      movimentação do vínculo e só se encerra por tratamento manual (não há "condição
      que some").

    `chip` é opcional porque `veiculo_sem_chip` é, por definição, de um veículo que não
    tem chip. Nas ocorrências de chip, `veiculo` é só contexto (o veículo a que o chip
    estava ligado) e não entra na deduplicação."""

    class Tipo(models.TextChoices):
        FALHA_RECORRENTE = "falha_recorrente", "Falhas de consulta seguidas"
        SEM_SESSAO = "sem_sessao", "Sem sessão"
        DESATUALIZADO = "desatualizado", "Dados desatualizados"
        CONSUMO_ELEVADO = "consumo_elevado", "Consumo elevado"
        IDENTIFICADOR_DIVERGENTE = "identificador_divergente", "Identificador divergente"
        VEICULO_SEM_CHIP = "veiculo_sem_chip", "Veículo sem chip"
        CHIP_DESVINCULADO = "chip_desvinculado", "Chip desvinculado"
        ALTERACAO_VINCULO = "alteracao_vinculo", "Alteração de vínculo"
        VEICULO_SEM_ATUALIZACAO = "veiculo_sem_atualizacao", "Veículo sem atualização de conectividade"

    # Tipos que nascem de um EVENTO do vínculo (não têm "condição" para resolver sozinhos).
    TIPOS_DE_EVENTO = ("chip_desvinculado", "alteracao_vinculo")
    # Tipos que pertencem a um veículo, sem chip.
    TIPOS_DE_VEICULO = ("veiculo_sem_chip", "veiculo_sem_atualizacao")

    class Nivel(models.TextChoices):
        INFO = "info", "Informativo"
        ATENCAO = "atencao", "Atenção"
        CRITICO = "critico", "Crítico"

    class Estado(models.TextChoices):
        ABERTA = "aberta", "Aberta"
        EM_TRATAMENTO = "em_tratamento", "Em tratamento"
        RESOLVIDA = "resolvida", "Resolvida"
        DESCARTADA = "descartada", "Descartada"

    ESTADOS_ABERTOS = ("aberta", "em_tratamento")

    chip = models.ForeignKey(M2MChip, on_delete=models.PROTECT, related_name="ocorrencias", null=True, blank=True)
    veiculo = models.ForeignKey(
        "FrotaVeiculo", on_delete=models.PROTECT, related_name="ocorrencias", null=True, blank=True,
    )
    tipo = models.CharField(max_length=30, choices=Tipo.choices)
    nivel = models.CharField(max_length=10, choices=Nivel.choices, default=Nivel.ATENCAO)
    estado = models.CharField(max_length=15, choices=Estado.choices, default=Estado.ABERTA, db_index=True)
    descricao = models.TextField(blank=True, default="")
    detectada_em = models.DateTimeField(default=timezone.now)
    ultima_deteccao_em = models.DateTimeField(default=timezone.now)
    deteccoes = models.PositiveIntegerField(default=1)
    resolvida_em = models.DateTimeField(null=True, blank=True)
    resolvida_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        ordering = ["-detectada_em", "-pk"]
        verbose_name = "Ocorrência de conectividade"
        verbose_name_plural = "Ocorrências de conectividade"
        constraints = [
            models.UniqueConstraint(
                fields=["chip", "tipo"],
                condition=models.Q(estado__in=["aberta", "em_tratamento"], chip__isnull=False),
                name="m2m_ocorrencia_aberta_unica",
            ),
            models.UniqueConstraint(
                fields=["veiculo", "tipo"],
                condition=models.Q(estado__in=["aberta", "em_tratamento"], chip__isnull=True, veiculo__isnull=False),
                name="m2m_ocorrencia_veiculo_aberta_unica",
            ),
            models.CheckConstraint(
                condition=models.Q(chip__isnull=False) | models.Q(veiculo__isnull=False),
                name="m2m_ocorrencia_tem_alvo",
            ),
        ]
        indexes = [models.Index(fields=["estado", "-ultima_deteccao_em"], name="m2m_ocorrencia_estado_idx")]

    def __str__(self):
        alvo = self.chip_id or (f"veículo {self.veiculo_id}" if self.veiculo_id else "—")
        return f"{self.get_tipo_display()} · {alvo} · {self.get_estado_display()}"

    @property
    def aberta_ou_em_tratamento(self) -> bool:
        return self.estado in self.ESTADOS_ABERTOS


class M2MOcorrenciaTratamento(models.Model):
    """Andamento de uma ocorrência: quem fez o quê e quando. `usuario` nulo = o
    próprio sistema (detecção, resolução automática)."""

    class Acao(models.TextChoices):
        DETECTADA = "detectada", "Detectada"
        ASSUMIR = "assumir", "Assumida"
        RESOLVER = "resolver", "Resolvida"
        DESCARTAR = "descartar", "Descartada"
        COMENTAR = "comentar", "Comentário"
        RESOLVIDA_AUTOMATICA = "resolvida_automatica", "Resolvida automaticamente"
        CONDICAO_ENCERRADA = "condicao_encerrada", "Condição encerrada"

    ocorrencia = models.ForeignKey(M2MOcorrencia, on_delete=models.CASCADE, related_name="tratamentos")
    usuario = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    acao = models.CharField(max_length=25, choices=Acao.choices)
    comentario = models.TextField(blank=True, default="")
    em = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["em", "pk"]
        verbose_name = "Tratamento de ocorrência TIM M2M"
        verbose_name_plural = "Tratamentos de ocorrências TIM M2M"


class M2MEvento(models.Model):
    """Trilha de auditoria do módulo. `chip` é nulo nos eventos da integração
    (token, configuração, mapeamento, teste de conexão). `dados` traz o de/para
    e NUNCA o token."""

    class Tipo(models.TextChoices):
        CADASTRO = "cadastro", "Chip cadastrado"
        EDICAO = "edicao", "Chip editado"
        DESATIVADO = "desativado", "Monitoramento desativado"
        REATIVADO = "reativado", "Monitoramento reativado"
        TOKEN_ALTERADO = "token_alterado", "Token alterado"
        CONFIG_ALTERADA = "config_alterada", "Configuração alterada"
        MAPEAMENTO_ALTERADO = "mapeamento_alterado", "Mapeamento alterado"
        TESTE_CONEXAO = "teste_conexao", "Teste de conexão"
        ESTADO_SESSAO_MUDOU = "estado_sessao_mudou", "Estado da sessão mudou"
        # Movimentações do chip (espelham `FrotaMovimentacao`, com o id dela em `dados`).
        ENTRADA = "entrada", "Entrada do chip"
        VINCULADO = "vinculado", "Chip vinculado"
        DESVINCULADO = "desvinculado", "Chip desvinculado"
        TRANSFERIDO = "transferido", "Chip transferido"
        SUBSTITUIDO = "substituido", "Chip substituído"
        RETORNO_DISPONIBILIDADE = "retorno_disponibilidade", "Chip devolvido à disponibilidade"
        INATIVADO = "inativado", "Chip inativado"
        # Gravado pela migration 0190 ao remover o antigo vínculo direto com `Item`.
        ITEM_REMOVIDO = "item_removido", "Vínculo antigo com equipamento removido"
        CONFIG_FROTA = "config_frota", "Cadastro de apoio alterado"
        # A ligação do chip ao seu item do estoque de TI (`M2MChip.item_ti`, regra 66).
        ITEM_TI_LIGADO = "item_ti_ligado", "Chip ligado ao estoque de TI"
        ITEM_TI_DESLIGADO = "item_ti_desligado", "Chip desligado do estoque de TI"

    chip = models.ForeignKey(M2MChip, on_delete=models.PROTECT, null=True, blank=True, related_name="eventos")
    tipo = models.CharField(max_length=25, choices=Tipo.choices)
    usuario = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    dados = models.JSONField(default=dict, blank=True)
    em = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-em", "-pk"]
        verbose_name = "Evento TIM M2M"
        verbose_name_plural = "Eventos TIM M2M"
        indexes = [models.Index(fields=["chip", "-em"], name="m2m_evento_chip_idx")]

    def __str__(self):
        return f"{self.get_tipo_display()} · {self.em:%d/%m/%Y %H:%M}"


# ═════════════════════════════════════════════════════════════════════════════
# Frotas e Conectividade
#
# O módulo cresceu do monitoramento de chips TIM M2M para o controle da FROTA
# (veículos e equipamentos agrícolas) e dos chips que ela usa. Regras que moldam
# os models abaixo (a lógica está em services/frota_service.py,
# frota_vinculo_service.py, frota_import_service.py e m2m_monitoramento_service.py):
#
#   • NÃO MEXE NO ESTOQUE DE TI. Nenhuma operação de chip ou de veículo altera `Item`,
#     `MovimentacaoItem` nem `ItemStatusHistorico`; a única escrita é CRIAR o item de um chip que ainda
#     não tem patrimônio, por ação explícita do TI (regra 66) — nunca editar um item existente.
#     As ÚNICAS pontes para `Item` são REFERÊNCIAS, e o teste de separação
#     as lista: o destino do vínculo (`FrotaVinculo.item`, tablet/celular — regra 62), o que a
#     movimentação lembra (`item_origem`/`item_destino`) e o PATRIMÔNIO do chip (`M2MChip.item_ti`
#     — regra 66). Fora isso, só `Localidade` (unidade/fazenda) e `Usuario` (responsável) são
#     reaproveitados, por serem cadastros organizacionais.
#   • UM CHIP, UM VÍNCULO ATIVO. A garantia é a `UniqueConstraint` parcial de
#     `FrotaVinculo` (chip, quando `fim_em` é nulo) — é o BANCO que impede dois
#     vínculos ativos, mesmo com duas pessoas clicando ao mesmo tempo.
#   • HISTÓRICO IMUTÁVEL. `FrotaMovimentacao` e `FrotaEvento` nunca são editados nem
#     apagados; `FrotaVinculo` só muda ao ser ENCERRADO (e uma vez). O admin é só leitura.
#   • STATUS OPERACIONAL ≠ CONECTIVIDADE. `FrotaVeiculo.status` (ativo, manutenção…)
#     e a situação da sessão dos chips são duas coisas; uma nunca deriva da outra.
# ═════════════════════════════════════════════════════════════════════════════

class FrotaTipo(models.Model):
    """Tipo de veículo/equipamento (Trator, Caminhão…). Extensível pelo TI.
    `exige_conectividade` é o PADRÃO do tipo: `FrotaVeiculo.exige_conectividade`
    (quando preenchido) vence."""

    nome = models.CharField(max_length=80, unique=True)
    exige_conectividade = models.BooleanField(
        default=False,
        help_text="Veículo ativo deste tipo, sem chip vinculado, gera o alerta \"Veículo sem chip\".",
    )
    ativo = models.BooleanField(default=True)
    # O que o tipo ESPERA ter. Não é validação: placa e chassi não se aplicam a todo equipamento
    # (gerador, trator e implemento nem sempre têm), então a falta de um deles NUNCA bloqueia o
    # cadastro. Só que, num tipo que costuma tê-los (moto, caminhão, utilitário), a falta passa a
    # ser uma pendência cadastral ("Sem identificação completa").
    espera_placa = models.BooleanField(
        default=False, help_text="Equipamento deste tipo costuma ser emplacado: ficar sem placa vira pendência cadastral.",
    )
    espera_chassi = models.BooleanField(
        default=False, help_text="Equipamento deste tipo costuma ter chassi / nº de série: ficar sem ele vira pendência cadastral.",
    )

    class Meta:
        ordering = ["nome"]
        verbose_name = "Tipo de veículo"
        verbose_name_plural = "Tipos de veículo"

    def __str__(self):
        return self.nome


class FrotaNatureza(models.TextChoices):
    """De quem é o ativo. Classificação INDEPENDENTE da classe operacional: um trator pode ser
    próprio ou de terceiro. Equipamento de terceiro nunca entra na conta do patrimônio próprio."""

    PROPRIO = "proprio", "Próprio"
    TERCEIRO = "terceiro", "Terceiro"


class _CatalogoERP(models.Model):
    """Base dos cadastros de apoio que a planilha do ERP traz como PAR (código + nome): empresa,
    classe operacional, classe mecânica e centro de custo. O `codigo_erp` é a identidade vinda do
    ERP (preservado como veio, é o que a importação usa para reconhecer a mesma linha numa nova
    planilha); o `nome` é o rótulo. Vazio de código = cadastro criado à mão no sistema.

    Nunca se apaga: `ativo=False` tira das PRÓXIMAS escolhas e preserva os equipamentos que o usam."""

    codigo_erp = models.CharField("Código no ERP", max_length=20, blank=True, default="")
    nome = models.CharField(max_length=120)
    ativo = models.BooleanField(default=True)

    class Meta:
        abstract = True
        ordering = ["nome"]
        constraints = [
            models.UniqueConstraint(fields=["codigo_erp"], condition=~models.Q(codigo_erp=""), name="%(class)s_codigo_erp_unico"),
            models.UniqueConstraint(Lower("nome"), name="%(class)s_nome_unico"),
        ]

    def __str__(self):
        return self.nome


class FrotaEmpresa(_CatalogoERP):
    """A empresa dona do equipamento, como o ERP a nomeia ("SANTA COLOMBA", "TERCEIROS"). A
    `natureza_padrao` só SUGERE a natureza do ativo: ela é um campo próprio do equipamento."""

    natureza_padrao = models.CharField(max_length=8, choices=FrotaNatureza.choices, default=FrotaNatureza.PROPRIO)

    class Meta(_CatalogoERP.Meta):
        verbose_name = "Empresa (frota)"
        verbose_name_plural = "Empresas (frota)"


class FrotaClasseOperacional(_CatalogoERP):
    """A classe operacional do ERP ("TRATOR LEVE", "CAMINHÃO ROLL OFF"…) com o nome ORIGINAL
    preservado. A `categoria` (um `FrotaTipo`) é a organização superior: agrupa várias classes sem
    apagar a classificação de origem. Sem categoria = "a classificar"."""

    categoria = models.ForeignKey(
        FrotaTipo, on_delete=models.SET_NULL, null=True, blank=True, related_name="classes_operacionais",
        help_text="A categoria superior que agrupa esta classe.",
    )

    class Meta(_CatalogoERP.Meta):
        verbose_name = "Classe operacional"
        verbose_name_plural = "Classes operacionais"


class FrotaClasseMecanica(_CatalogoERP):
    """A classe mecânica do ERP ("TRATOR LEVE", "LINHA AMARELA"…): outra leitura do mesmo equipamento,
    que NÃO é um nível da classe operacional (uma "PA CARREGADEIRA" operacional pode ser "LINHA
    AMARELA" mecânica) — por isso é uma classificação própria."""

    class Meta(_CatalogoERP.Meta):
        verbose_name = "Classe mecânica"
        verbose_name_plural = "Classes mecânicas"


class FrotaCentroCusto(_CatalogoERP):
    """O centro de custo do ERP ("32107 · TRATOR LEVE - GRAOS"). É cadastro PRÓPRIO da frota: o
    `CentroCusto` do estoque de TI é outro catálogo (departamentos), e misturá-los poluiria as
    telas dos dois lados. O `codigo_erp` é a identidade — a planilha traz o mesmo código com nomes
    grafados de duas formas, e a importação guarda o primeiro e AVISA da divergência."""

    class Meta(_CatalogoERP.Meta):
        verbose_name = "Centro de custo (frota)"
        verbose_name_plural = "Centros de custo (frota)"


class FrotaVeiculo(AuditModel):
    """Veículo ou equipamento agrícola da frota — o registro PRINCIPAL do ativo motorizado. Os
    equipamentos tecnológicos (monitor, receptor, rádio…) não são campos daqui: moram no estoque
    tecnológico e chegam à máquina por instalação (`TecInstalacao`), e os chips por vínculo.

    Documentos (placa, RENAVAM, chassi/série) são opcionais: trator, pulverizador e implemento nem
    sempre têm placa. Dois identificadores, de propósito: `codigo` é o do Zelo Agro (interno) e
    `codigo_legado` é o do ERP, preservado como veio."""

    class Status(models.TextChoices):
        ATIVO = "ativo", "Em uso"
        MANUTENCAO = "manutencao", "Em manutenção"
        INATIVO = "inativo", "Inativo"
        DESATIVADO = "desativado", "Desativado"

    Natureza = FrotaNatureza

    # identificação
    codigo = models.CharField(max_length=30, unique=True, help_text="Identificador interno do Zelo Agro. Único; gravado em caixa alta.")
    codigo_legado = models.CharField(
        "Código original (ERP)", max_length=30, blank=True, default="",
        help_text="O código da coluna \"Equipamento\" da planilha do ERP, como veio. Único quando preenchido; é com ele que a importação reconhece o mesmo equipamento.",
    )
    nome = models.CharField(max_length=120)
    tipo = models.ForeignKey(FrotaTipo, on_delete=models.PROTECT, related_name="veiculos")
    fabricante = models.CharField(max_length=80, blank=True, default="")
    modelo = models.CharField(max_length=80, blank=True, default="")
    ano_fabricacao = models.PositiveSmallIntegerField(null=True, blank=True)
    ano_modelo = models.PositiveSmallIntegerField(null=True, blank=True)
    # classificação: duas coisas independentes (de quem é × o que é) mais a leitura mecânica do ERP
    natureza = models.CharField(max_length=8, choices=FrotaNatureza.choices, default=FrotaNatureza.PROPRIO, db_index=True)
    empresa = models.ForeignKey(FrotaEmpresa, on_delete=models.PROTECT, null=True, blank=True, related_name="veiculos")
    classe_operacional = models.ForeignKey(
        FrotaClasseOperacional, on_delete=models.PROTECT, null=True, blank=True, related_name="veiculos",
        verbose_name="Classe operacional",
    )
    classe_mecanica = models.ForeignKey(
        FrotaClasseMecanica, on_delete=models.PROTECT, null=True, blank=True, related_name="veiculos",
        verbose_name="Classe mecânica",
    )
    centro_custo = models.ForeignKey(
        FrotaCentroCusto, on_delete=models.PROTECT, null=True, blank=True, related_name="veiculos", verbose_name="Centro de custo",
    )
    # datas (como o ERP as trouxe: não são corrigidas — as suspeitas viram conferência)
    data_compra = models.DateField(null=True, blank=True, verbose_name="Data de compra")
    data_inicio = models.DateField(null=True, blank=True, verbose_name="Data de início")
    # documentos (todos opcionais)
    placa = models.CharField(
        max_length=8, blank=True, default="",
        help_text="Placa do veículo (AAA9999 ou Mercosul) ou a identificação interna do equipamento. Única quando preenchida.",
    )
    renavam = models.CharField(max_length=11, blank=True, default="", help_text="11 dígitos.")
    chassi_serie = models.CharField(
        max_length=30, blank=True, default="", verbose_name="Chassi / nº de série",
        help_text="Chassi ou número de série. Não é único no banco: o ERP traz o mesmo chassi em dois equipamentos, e isso é conferência, não bloqueio.",
    )
    # lotação e responsáveis
    localidade = models.ForeignKey(
        Localidade, on_delete=models.SET_NULL, null=True, blank=True, related_name="veiculos_frota",
        verbose_name="Unidade / fazenda",
    )
    setor = models.CharField(max_length=100, blank=True, default="")
    area_operacao = models.CharField(max_length=100, blank=True, default="", verbose_name="Área de operação")
    responsavel = models.ForeignKey(
        Usuario, on_delete=models.SET_NULL, null=True, blank=True, related_name="veiculos_frota",
        verbose_name="Responsável",
    )
    observacoes = models.TextField(blank=True, default="")
    # operação
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ATIVO, db_index=True)
    # A situação como o ERP a trouxe ("Em Uso"): só registro. Importar NÃO é validar — o equipamento
    # nasce com a validação PENDENTE, até o responsável pelo inventário confirmar a situação.
    situacao_origem = models.CharField(max_length=40, blank=True, default="", verbose_name="Situação na planilha")
    validacao_pendente = models.BooleanField(
        default=False, db_index=True,
        help_text="Veio de importação e o responsável pelo inventário ainda não confirmou a situação.",
    )
    exige_conectividade = models.BooleanField(
        null=True, blank=True,
        help_text="Vazio = herda do tipo. Sim/Não sobrepõe o padrão do tipo.",
    )
    # Onde o equipamento está — e DE ONDE vem essa informação. Só existe coordenada que alguem cadastrou
    # (`cadastral`) ou que um rastreador entregou (`gps`, quando houver integração). Nunca é deduzida de chip,
    # sessão ou consumo: a TIM não informa posição (regra 63).
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    localizacao_origem = models.CharField(
        max_length=10, blank=True, default="", choices=[("cadastral", "Coordenada cadastral"), ("gps", "GPS")],
        help_text="Vazio = localização desconhecida.",
    )
    localizacao_em = models.DateTimeField(null=True, blank=True, help_text="Quando a coordenada foi cadastrada ou recebida.")

    class Meta:
        ordering = ["codigo"]
        verbose_name = "Veículo / equipamento"
        verbose_name_plural = "Veículos e equipamentos"
        permissions = PERMISSOES_FROTA_VEICULO + PERMISSOES_FROTA_INVENTARIO
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(latitude__isnull=True, longitude__isnull=True)
                           | (models.Q(latitude__isnull=False, longitude__isnull=False)
                              & models.Q(latitude__gte=-90, latitude__lte=90, longitude__gte=-180, longitude__lte=180))),
                name="frota_veiculo_coordenada_valida",
            ),
            models.UniqueConstraint(fields=["placa"], condition=~models.Q(placa=""), name="frota_veiculo_placa_unica"),
            models.UniqueConstraint(
                fields=["codigo_legado"], condition=~models.Q(codigo_legado=""), name="frota_veiculo_codigo_legado_unico",
            ),
        ]
        # O chassi deixou de ser ÚNICO no banco (era `frota_veiculo_chassi_unico`): o ERP traz o mesmo chassi
        # em dois equipamentos, e a regra do projeto é importar e SINALIZAR, nunca recusar nem corrigir.
        # A duplicidade continua barrada ao DIGITAR um chassi (service), e é apontada nas conferências.
        indexes = [
            models.Index(fields=["chassi_serie"], name="frota_veiculo_chassi_idx"),
            models.Index(fields=["natureza", "status"], name="frota_veiculo_nat_status_idx"),
        ]

    def __str__(self):
        return f"{self.codigo} · {self.nome}"

    def save(self, *args, **kwargs):
        self.codigo = (self.codigo or "").strip().upper()      # rede de segurança: o service já normaliza
        super().save(*args, **kwargs)

    @property
    def exige_conectividade_efetiva(self) -> bool:
        """Sim/Não do próprio veículo; se vazio, o padrão do tipo."""
        if self.exige_conectividade is not None:
            return self.exige_conectividade
        return bool(self.tipo.exige_conectividade)


class _SomenteInsercaoQuerySet(models.QuerySet):
    """QuerySet de tabela de HISTÓRICO: `update`/`delete` em massa recusados. É o
    complemento do `save`/`delete` da instância (que o ORM não passa por aqui)."""

    def update(self, **kwargs):
        raise ValidationError("Este registro é histórico imutável e não pode ser alterado.")

    def delete(self):
        raise ValidationError("Este registro é histórico imutável e não pode ser apagado.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError("Este registro é histórico imutável e não pode ser alterado.")


class FrotaImportacaoChips(models.Model):
    """Uma importação de chips por planilha: nasce como PRÉVIA (nada gravado nos
    chips), vira `confirmada` ou `cancelada`. A prévia expira em 24 h. Nunca é apagada:
    é o relatório auditado de quem importou o quê."""

    class Estado(models.TextChoices):
        PREVIA = "previa", "Prévia"
        CONFIRMADA = "confirmada", "Confirmada"
        CANCELADA = "cancelada", "Cancelada"

    class Formato(models.TextChoices):
        XLSX = "xlsx", "Excel (.xlsx)"
        CSV = "csv", "CSV"

    VALIDADE_DA_PREVIA = timedelta(hours=24)

    arquivo_nome = models.CharField(max_length=255)
    sha256 = models.CharField(max_length=64, db_index=True)
    formato = models.CharField(max_length=5, choices=Formato.choices)
    estado = models.CharField(max_length=10, choices=Estado.choices, default=Estado.PREVIA, db_index=True)
    criada_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    criada_em = models.DateTimeField(default=timezone.now)
    confirmada_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    confirmada_em = models.DateTimeField(null=True, blank=True)
    # totais (a prévia preenche `linhas`/`validas`/`existentes`/`rejeitados`/`avisos`;
    # a confirmação preenche `importados` e acerta os demais)
    linhas = models.PositiveIntegerField(default=0)
    validas = models.PositiveIntegerField(default=0)
    importados = models.PositiveIntegerField(default=0)
    existentes = models.PositiveIntegerField(default=0)
    rejeitados = models.PositiveIntegerField(default=0)
    avisos = models.PositiveIntegerField(default=0, help_text="Linhas com pelo menos um aviso.")

    class Meta:
        ordering = ["-criada_em", "-pk"]
        verbose_name = "Importação de chips"
        verbose_name_plural = "Importações de chips"

    def __str__(self):
        return f"{self.arquivo_nome} · {self.get_estado_display()}"

    @property
    def expira_em(self):
        return self.criada_em + self.VALIDADE_DA_PREVIA

    @property
    def expirada(self) -> bool:
        return self.estado == self.Estado.PREVIA and timezone.now() > self.expira_em


class FrotaImportacaoLinha(models.Model):
    """Uma linha da planilha, com o que se decidiu sobre ela. `dados` é o valor JÁ
    normalizado (ICCID/IMSI limpos, texto aparado) — é o que a confirmação regrava,
    nunca a célula original."""

    class Situacao(models.TextChoices):
        VALIDA = "valida", "Válida"
        EXISTENTE = "existente", "Já cadastrada"
        REJEITADA = "rejeitada", "Rejeitada"
        IMPORTADA = "importada", "Importada"

    importacao = models.ForeignKey(FrotaImportacaoChips, on_delete=models.CASCADE, related_name="itens")
    numero = models.PositiveIntegerField(help_text="Número da linha na planilha (a do cabeçalho é a 1).")
    dados = models.JSONField(default=dict, blank=True)
    situacao = models.CharField(max_length=10, choices=Situacao.choices, db_index=True)
    motivos = models.JSONField(default=list, blank=True)
    avisos = models.JSONField(default=list, blank=True)
    chip = models.ForeignKey(M2MChip, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        ordering = ["importacao", "numero"]
        verbose_name = "Linha de importação de chips"
        verbose_name_plural = "Linhas de importação de chips"
        indexes = [models.Index(fields=["importacao", "situacao"], name="frota_imp_linha_sit_idx")]

    def __str__(self):
        return f"Linha {self.numero} · {self.get_situacao_display()}"


class FrotaImportacaoEquipamentos(models.Model):
    """Uma importação da planilha de EQUIPAMENTOS MOTORIZADOS (a do ERP): nasce como PRÉVIA (nada é
    gravado na frota), vira `confirmada` ou `cancelada`. A prévia expira em 24 h. Nunca é apagada: é o
    relatório auditado de quem importou o quê. Tabela própria, e não a de chips: as duas listas de
    auditoria não se misturam.

    `mapeamento` guarda as decisões de quem conferiu — a coluna de cada campo e, para os valores que
    pedem decisão, o destino de cada um (situação "Em Uso" → status; empresa → natureza; classe
    operacional → categoria) — e é ele, não o texto da planilha, que a confirmação aplica."""

    class Estado(models.TextChoices):
        PREVIA = "previa", "Prévia"
        CONFIRMADA = "confirmada", "Confirmada"
        CANCELADA = "cancelada", "Cancelada"

    class Formato(models.TextChoices):
        XLSX = "xlsx", "Excel (.xlsx)"
        CSV = "csv", "CSV"

    VALIDADE_DA_PREVIA = timedelta(hours=24)

    arquivo_nome = models.CharField(max_length=255)
    sha256 = models.CharField(max_length=64, db_index=True)
    formato = models.CharField(max_length=5, choices=Formato.choices)
    estado = models.CharField(max_length=10, choices=Estado.choices, default=Estado.PREVIA, db_index=True)
    mapeamento = models.JSONField(default=dict, blank=True)
    atualizar_existentes = models.BooleanField(
        default=False, help_text="Autorizado na confirmação: os equipamentos já cadastrados recebem os dados da planilha.",
    )
    criada_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    criada_em = models.DateTimeField(default=timezone.now)
    confirmada_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    confirmada_em = models.DateTimeField(null=True, blank=True)
    # totais: a prévia preenche `linhas`/`validas`/`existentes`/`rejeitados`/`avisos`; a confirmação,
    # `importados` e `atualizados`. Sempre recalculados das LINHAS (a fonte da verdade).
    linhas = models.PositiveIntegerField(default=0)
    validas = models.PositiveIntegerField(default=0)
    importados = models.PositiveIntegerField(default=0)
    atualizados = models.PositiveIntegerField(default=0)
    existentes = models.PositiveIntegerField(default=0)
    rejeitados = models.PositiveIntegerField(default=0)
    avisos = models.PositiveIntegerField(default=0, help_text="Linhas com pelo menos uma conferência.")

    class Meta:
        ordering = ["-criada_em", "-pk"]
        verbose_name = "Importação de equipamentos"
        verbose_name_plural = "Importações de equipamentos"

    def __str__(self):
        return f"{self.arquivo_nome} · {self.get_estado_display()}"

    @property
    def expira_em(self):
        return self.criada_em + self.VALIDADE_DA_PREVIA

    @property
    def expirada(self) -> bool:
        return self.estado == self.Estado.PREVIA and timezone.now() > self.expira_em


class FrotaImportacaoEquipamentoLinha(models.Model):
    """Uma linha da planilha de equipamentos e o que se decidiu sobre ela. `dados` é o valor JÁ
    normalizado (e `bruto`, o que veio na célula, para o relatório); `motivos` rejeitam a linha e
    `conferencias` só informam: `[{"codigo", "gravidade", "texto"}]`. Importar não corrige nada
    suspeito — a conferência é de quem responde pelo inventário."""

    class Situacao(models.TextChoices):
        VALIDA = "valida", "Válida"
        EXISTENTE = "existente", "Já cadastrada"
        REJEITADA = "rejeitada", "Rejeitada"
        IMPORTADA = "importada", "Importada"
        ATUALIZADA = "atualizada", "Atualizada"

    importacao = models.ForeignKey(FrotaImportacaoEquipamentos, on_delete=models.CASCADE, related_name="itens")
    numero = models.PositiveIntegerField(help_text="Número da linha na planilha (a do cabeçalho é a 1).")
    codigo_legado = models.CharField(max_length=30, blank=True, default="", db_index=True)
    dados = models.JSONField(default=dict, blank=True)
    situacao = models.CharField(max_length=10, choices=Situacao.choices, db_index=True)
    motivos = models.JSONField(default=list, blank=True)
    conferencias = models.JSONField(default=list, blank=True)
    veiculo = models.ForeignKey(FrotaVeiculo, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        ordering = ["importacao", "numero"]
        verbose_name = "Linha de importação de equipamentos"
        verbose_name_plural = "Linhas de importação de equipamentos"
        indexes = [models.Index(fields=["importacao", "situacao"], name="frota_impeq_linha_sit_idx")]

    def __str__(self):
        return f"Linha {self.numero} · {self.get_situacao_display()}"


class FrotaMovimentacao(models.Model):
    """Registro IMUTÁVEL de uma movimentação do chip. Nunca editado, nunca apagado
    (`save` recusa a edição, `delete` levanta, e o queryset recusa `update`/`delete` em
    massa). Várias linhas de UMA mesma ação (a substituição grava duas) compartilham
    `operacao`. `motivo` é obrigatório em tudo, menos na entrada (service)."""

    class Tipo(models.TextChoices):
        ENTRADA = "entrada", "Entrada"
        VINCULACAO = "vinculacao", "Vinculação"
        TRANSFERENCIA = "transferencia", "Transferência"
        DESVINCULACAO = "desvinculacao", "Desvinculação"
        RETORNO = "retorno", "Retorno à disponibilidade"
        SUBSTITUICAO = "substituicao", "Substituição"
        INATIVACAO = "inativacao", "Inativação"

    tipo = models.CharField(max_length=15, choices=Tipo.choices, db_index=True)
    chip = models.ForeignKey(M2MChip, on_delete=models.PROTECT, related_name="movimentacoes")
    chip_relacionado = models.ForeignKey(
        M2MChip, on_delete=models.PROTECT, null=True, blank=True, related_name="+",
        help_text="O outro chip da substituição.",
    )
    operacao = models.UUIDField(default=uuid.uuid4, db_index=True, help_text="Agrupa as linhas de uma mesma ação.")
    veiculo_origem = models.ForeignKey(
        FrotaVeiculo, on_delete=models.PROTECT, null=True, blank=True, related_name="movimentacoes_de_saida",
    )
    veiculo_destino = models.ForeignKey(
        FrotaVeiculo, on_delete=models.PROTECT, null=True, blank=True, related_name="movimentacoes_de_entrada",
    )
    # O OUTRO tipo de destino: um equipamento do estoque de TI (tablet, celular). É só uma REFERÊNCIA para
    # dizer onde o chip esteve — o módulo nunca escreve no `Item` (regra 62). De cada lado, no máximo um
    # dos dois (veículo OU equipamento), garantido pelas restrições abaixo.
    item_origem = models.ForeignKey(
        "Item", on_delete=models.PROTECT, null=True, blank=True, related_name="movimentacoes_de_chip_saida",
    )
    item_destino = models.ForeignKey(
        "Item", on_delete=models.PROTECT, null=True, blank=True, related_name="movimentacoes_de_chip_entrada",
    )
    motivo = models.CharField(max_length=255, blank=True, default="")
    observacoes = models.TextField(blank=True, default="")
    usuario = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="+")
    usuario_nome = models.CharField(max_length=150, blank=True, default="", help_text="O login no momento (sobrevive à exclusão do usuário).")
    em = models.DateTimeField(default=timezone.now, db_index=True)
    importacao = models.ForeignKey(FrotaImportacaoChips, on_delete=models.PROTECT, null=True, blank=True, related_name="movimentacoes")

    objects = _SomenteInsercaoQuerySet.as_manager()

    class Meta:
        ordering = ["-em", "-pk"]
        verbose_name = "Movimentação de chip"
        verbose_name_plural = "Movimentações de chips"
        indexes = [models.Index(fields=["chip", "-em"], name="frota_mov_chip_idx")]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(veiculo_origem__isnull=True) | models.Q(item_origem__isnull=True),
                name="frota_mov_origem_um_so",
            ),
            models.CheckConstraint(
                condition=models.Q(veiculo_destino__isnull=True) | models.Q(item_destino__isnull=True),
                name="frota_mov_destino_um_so",
            ),
        ]

    def __str__(self):
        return f"{self.get_tipo_display()} · chip {self.chip_id} · {self.em:%d/%m/%Y %H:%M}"

    @property
    def origem_rotulo(self) -> str:
        """Onde o chip estava: `COD · nome` do veículo, ou o equipamento de TI, ou `""` (entrada/retorno)."""
        if self.veiculo_origem_id:
            return f"{self.veiculo_origem.codigo} · {self.veiculo_origem.nome}"
        return rotulo_do_equipamento_ti(self.item_origem) if self.item_origem_id else ""

    @property
    def destino_rotulo(self) -> str:
        """Para onde o chip foi (mesma leitura de `origem_rotulo`)."""
        if self.veiculo_destino_id:
            return f"{self.veiculo_destino.codigo} · {self.veiculo_destino.nome}"
        return rotulo_do_equipamento_ti(self.item_destino) if self.item_destino_id else ""

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("A movimentação é histórico imutável e não pode ser editada.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("A movimentação é histórico imutável e não pode ser apagada.")


def rotulo_do_equipamento_ti(item) -> str:
    """Como um equipamento do ESTOQUE DE TI aparece nas telas de chip: o nome e o número de série
    (`Galaxy Tab A7 · R9XR500VDDJ`). Só lê o próprio `Item` — sem consulta extra por linha."""
    if item is None:
        return ""
    serie = (item.numero_serie or "").strip()
    return f"{item.nome} · {serie}" if serie and serie.upper() != "S/N" else item.nome


class FrotaVinculo(models.Model):
    """Um PERÍODO em que um chip esteve num destino. Ativo enquanto `fim_em` é nulo.

    O destino é EXATAMENTE UM de dois: um veículo (`veiculo`) OU um equipamento do estoque de TI
    (`item`: tablet, celular — o que o subtipo marca com `aceita_chip`). Mesmo padrão do
    `TecInstalacao` (um destino por linha, garantido por restrição). O `item` é só REFERÊNCIA: o
    módulo nunca altera estoque, movimentação ou status do `Item` (regra 62).

    O destino pode ter N chips; o chip só pode ter UM vínculo ativo, em qualquer destino —
    garantido pela restrição condicional abaixo, não pelo código (duas pessoas ao mesmo tempo).

    Nunca apagado. A única alteração permitida é ENCERRAR o período (`fim_em`,
    `fim_por`, `motivo_fim`, `mov_fim`), e só enquanto ele está ativo."""

    CAMPOS_ENCERRAMENTO = ("fim_em", "fim_por", "motivo_fim", "mov_fim")

    chip = models.ForeignKey(M2MChip, on_delete=models.PROTECT, related_name="vinculos")
    veiculo = models.ForeignKey(FrotaVeiculo, on_delete=models.PROTECT, null=True, blank=True, related_name="vinculos")
    item = models.ForeignKey("Item", on_delete=models.PROTECT, null=True, blank=True, related_name="vinculos_chip")
    inicio_em = models.DateTimeField(default=timezone.now)
    inicio_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    fim_em = models.DateTimeField(null=True, blank=True)
    fim_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    motivo_fim = models.CharField(max_length=255, blank=True, default="")
    mov_inicio = models.ForeignKey(
        FrotaMovimentacao, on_delete=models.PROTECT, null=True, blank=True, related_name="+",
    )
    mov_fim = models.ForeignKey(
        FrotaMovimentacao, on_delete=models.PROTECT, null=True, blank=True, related_name="+",
    )

    class Meta:
        ordering = ["-inicio_em", "-pk"]
        verbose_name = "Vínculo de chip"
        verbose_name_plural = "Vínculos de chips"
        constraints = [
            models.UniqueConstraint(
                fields=["chip"], condition=models.Q(fim_em__isnull=True), name="frota_vinculo_chip_ativo_unico",
            ),
            models.CheckConstraint(
                condition=models.Q(fim_em__isnull=True) | models.Q(fim_em__gte=models.F("inicio_em")),
                name="frota_vinculo_fim_depois_do_inicio",
            ),
            # EXATAMENTE UM destino por linha: veículo XOR equipamento de TI.
            models.CheckConstraint(
                condition=(
                    models.Q(veiculo__isnull=False, item__isnull=True)
                    | models.Q(veiculo__isnull=True, item__isnull=False)
                ),
                name="frota_vinculo_um_destino",
            ),
        ]
        indexes = [
            models.Index(fields=["veiculo", "fim_em"], name="frota_vinculo_veiculo_idx"),
            models.Index(fields=["item", "fim_em"], name="frota_vinculo_item_idx"),
            models.Index(fields=["chip", "-inicio_em"], name="frota_vinculo_chip_idx"),
        ]

    def __str__(self):
        destino = f"veículo {self.veiculo_id}" if self.veiculo_id else f"equipamento {self.item_id}"
        return f"chip {self.chip_id} → {destino} ({'ativo' if self.fim_em is None else 'encerrado'})"

    @property
    def ativo(self) -> bool:
        return self.fim_em is None

    @property
    def destino_tipo(self) -> str:
        """`"veiculo"` ou `"item"` (equipamento do estoque de TI)."""
        return "veiculo" if self.veiculo_id else "item"

    @property
    def destino(self):
        """O `FrotaVeiculo` ou o `Item` onde o chip está/esteve."""
        return self.veiculo if self.veiculo_id else self.item

    @property
    def destino_rotulo(self) -> str:
        """`COD · nome` do veículo, ou `nome · série` do equipamento de TI."""
        if self.veiculo_id:
            return f"{self.veiculo.codigo} · {self.veiculo.nome}"
        return rotulo_do_equipamento_ti(self.item) if self.item_id else ""

    @property
    def duracao(self):
        """`timedelta` do período (até agora, se ainda ativo)."""
        return (self.fim_em or timezone.now()) - self.inicio_em

    def save(self, *args, **kwargs):
        if not self._state.adding:
            campos = kwargs.get("update_fields")
            if not campos or not set(campos) <= set(self.CAMPOS_ENCERRAMENTO):
                raise ValidationError("O vínculo é histórico: só o encerramento do período pode ser gravado.")
            if type(self).objects.filter(pk=self.pk, fim_em__isnull=False).exists():
                raise ValidationError("Este vínculo já foi encerrado e não pode mais ser alterado.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("O vínculo é histórico e não pode ser apagado.")


class FrotaEvento(models.Model):
    """Linha do tempo do CADASTRO do veículo (criação, edição com de/para, status, movimentação,
    validação do inventário). Vínculos e instalações entram na linha do tempo por leitura, sem
    duplicar dados. Imutável, como `FrotaMovimentacao`.

    `movimentacao` é o registro de uma mudança de LUGAR ou de RESPONSÁVEL (fazenda, setor, centro de
    custo, responsável) feita pela operação "Registrar movimentação", com o motivo. O que o
    formulário de edição muda desses mesmos campos fica como `edicao`; as telas de movimentação
    leem os dois."""

    class Tipo(models.TextChoices):
        CADASTRO = "cadastro", "Veículo cadastrado"
        EDICAO = "edicao", "Veículo editado"
        STATUS = "status", "Status alterado"
        MOVIMENTACAO = "movimentacao", "Movimentação registrada"
        VALIDACAO = "validacao", "Cadastro validado"

    veiculo = models.ForeignKey(FrotaVeiculo, on_delete=models.PROTECT, related_name="eventos")
    tipo = models.CharField(max_length=20, choices=Tipo.choices)
    usuario = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    dados = models.JSONField(default=dict, blank=True)
    em = models.DateTimeField(default=timezone.now)

    objects = _SomenteInsercaoQuerySet.as_manager()

    class Meta:
        ordering = ["-em", "-pk"]
        verbose_name = "Evento do veículo"
        verbose_name_plural = "Eventos dos veículos"
        indexes = [models.Index(fields=["veiculo", "-em"], name="frota_evento_veiculo_idx")]

    def __str__(self):
        return f"{self.get_tipo_display()} · veículo {self.veiculo_id} · {self.em:%d/%m/%Y %H:%M}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("O evento é histórico imutável e não pode ser editado.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("O evento é histórico imutável e não pode ser apagado.")


# Padrão de cada regra de alerta — a migration 0191 semeia estes valores e o service
# recria uma regra apagada a partir deles. Os dois eventos do vínculo nascem DESLIGADOS:
# toda movimentação geraria uma ocorrência, e o TI liga quando quiser acompanhar.
REGRAS_DE_ALERTA_PADRAO = {
    "falha_recorrente": dict(gera_ocorrencia=True, nivel="atencao", envia_email=True),
    "sem_sessao": dict(gera_ocorrencia=True, nivel="atencao", envia_email=True),
    "desatualizado": dict(gera_ocorrencia=True, nivel="atencao", envia_email=True),
    "consumo_elevado": dict(gera_ocorrencia=True, nivel="atencao", envia_email=True),
    "identificador_divergente": dict(gera_ocorrencia=True, nivel="atencao", envia_email=True),
    "veiculo_sem_chip": dict(gera_ocorrencia=True, nivel="atencao", envia_email=True),
    "veiculo_sem_atualizacao": dict(gera_ocorrencia=True, nivel="atencao", envia_email=True),
    "chip_desvinculado": dict(gera_ocorrencia=False, nivel="info", envia_email=False),
    "alteracao_vinculo": dict(gera_ocorrencia=False, nivel="info", envia_email=False),
}


class FrotaRegraAlerta(models.Model):
    """Regra de um tipo de ocorrência: se gera ocorrência, com que nível, e se avisa
    por e-mail — e a QUEM. Uma linha por tipo. O envio sai pelo canal
    `m2m_ocorrencias` da Central de Alertas, que continua sendo a chave liga/desliga
    (junto da chave-mestra); os destinatários vêm daqui."""

    tipo = models.CharField(max_length=30, choices=M2MOcorrencia.Tipo.choices, unique=True)
    gera_ocorrencia = models.BooleanField(default=True)
    nivel = models.CharField(max_length=10, choices=M2MOcorrencia.Nivel.choices, default=M2MOcorrencia.Nivel.ATENCAO)
    envia_email = models.BooleanField(default=False)
    destinatarios_emails = models.TextField(
        blank=True, default="", help_text="E-mails separados por vírgula, ponto e vírgula ou quebra de linha.",
    )
    destinatarios_usuarios = models.ManyToManyField(User, blank=True, related_name="+")

    class Meta:
        ordering = ["tipo"]
        verbose_name = "Regra de alerta"
        verbose_name_plural = "Regras de alerta"

    def __str__(self):
        return self.get_tipo_display()


# ═════════════════════════════════════════════════════════════
# Ambientes do Zelo (TI · Agro)
# ═════════════════════════════════════════════════════════════

class PreferenciaAmbiente(models.Model):
    """O último ambiente que o login usou — para `/` e o pós-login abrirem nele,
    inclusive em outro computador. NÃO é autorização: quem decide se a pessoa
    alcança um ambiente é `services/ambiente_service.py` (permissão e grupo); a
    preferência só escolhe entre os ambientes que ela já alcança.

    Também é o dono da permissão `acessar_zelo_agro` (`Meta.permissions`).
    Gravada pelo `AmbienteMiddleware` só quando o ambiente MUDA."""

    class Ambiente(models.TextChoices):
        TI = "ti", "Zelo TI"
        AGRO = "agro", "Zelo Agro"

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="preferencia_ambiente")
    ambiente = models.CharField(max_length=8, choices=Ambiente.choices, default=Ambiente.TI)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Preferência de ambiente"
        verbose_name_plural = "Preferências de ambiente"
        permissions = PERMISSOES_AGRO_AMBIENTE

    def __str__(self):
        return f"{self.user} → {self.get_ambiente_display()}"


# ════════════════════════════════════════════════════════
# Zelo Agro — Tecnologia agrícola (inventário, estoque tecnológico e infraestrutura)
#
# Spec de 2026-11-13. Plano: `evolucao2-tecnologia-agricola.md`. Regras que moldam estes
# models (a lógica está em `services/tec_*.py`; nenhuma tela grava saldo ou situação
# direto):
#   • NADA daqui toca o estoque de TI (`Item`, `Categoria`, `Fornecedor`, a Rede do TI).
#   • Dois TIPOS de controle que nunca se misturam: o equipamento INDIVIDUAL
#     (`TecEquipamento`, um por número de série) e o material por QUANTIDADE
#     (`TecSaldo`, modelo × local). Quem decide é `TecModelo.controle`.
#   • O razão (`TecMovimentacao`) e a linha do tempo (`TecEvento`) são IMUTÁVEIS. O saldo e a
#     situação são cache/estado escritos só pelo service, dentro de transação.
# ════════════════════════════════════════════════════════

PERMISSOES_TEC = [
    ("ver_tec_estoque", "Pode ver o estoque tecnológico (equipamentos, materiais e catálogo)"),
    ("cadastrar_tec_equipamento", "Pode cadastrar e editar equipamentos e materiais"),
    ("importar_tec_material", "Pode importar equipamentos e materiais por planilha"),
    ("administrar_tec_catalogo", "Pode administrar fabricantes, categorias, modelos e fornecedores"),
    ("instalar_tec_equipamento", "Pode instalar, retirar, substituir e desvincular equipamentos"),
    ("transferir_tec_equipamento", "Pode transferir equipamentos entre máquinas, estruturas e pessoas"),
    ("administrar_tec_enlace", "Pode administrar os enlaces ponto a ponto"),
    ("gerenciar_tec_estrutura", "Pode gerenciar torres e estações"),
    ("gerenciar_tec_radio", "Pode gerenciar a radiocomunicação"),
    ("ver_tec_historico", "Pode consultar o histórico e os relatórios da tecnologia agrícola"),
    ("abrir_tec_manutencao", "Pode abrir e acompanhar manutenções de equipamentos"),
    ("concluir_tec_manutencao", "Pode concluir ou cancelar manutenções de equipamentos"),
    ("administrar_tec_config", "Pode administrar os locais de armazenamento e as unidades de medida"),
]
GRUPO_TEC_GESTAO = "Tecnologia Agrícola — Gestão"
GRUPO_TEC_CONSULTA = "Tecnologia Agrícola — Consulta"


class TecControle(models.TextChoices):
    INDIVIDUAL = "individual", "Individual (por número de série)"
    QUANTIDADE = "quantidade", "Por quantidade"


class TecFabricante(AuditModel):
    """Fabricante configurável (Solinftec, John Deere, Stara, Motorola… e os que vierem).
    Antes só existia como texto solto (`FrotaVeiculo.fabricante`); este é o cadastro."""

    nome = models.CharField(max_length=80, unique=True)
    ativo = models.BooleanField(default=True)

    class Meta:
        ordering = ["nome"]
        verbose_name = "Fabricante"
        verbose_name_plural = "Fabricantes"

    def __str__(self):
        return self.nome


class TecCategoria(AuditModel):
    """Categoria de equipamento/material. `grupo` é o que ROTEIA as áreas do módulo
    (radiocomunicação lista o grupo `radiocomunicacao`, a infraestrutura usa `rede`…);
    `controle_padrao` só PRÉ-PREENCHE o modelo — quem decide é `TecModelo.controle`."""

    class Grupo(models.TextChoices):
        AGRICULTURA_PRECISAO = "agricultura_precisao", "Agricultura de precisão"
        RADIOCOMUNICACAO = "radiocomunicacao", "Radiocomunicação"
        REDE = "rede", "Rede e enlaces"
        CONECTIVIDADE = "conectividade", "Conectividade"
        ENERGIA = "energia", "Energia"
        CABEAMENTO = "cabeamento", "Cabos e acessórios"
        SENSORES = "sensores", "Sensores e controle"
        OUTRO = "outro", "Outros"

    nome = models.CharField(max_length=80, unique=True)
    grupo = models.CharField(max_length=24, choices=Grupo.choices, default=Grupo.OUTRO, db_index=True)
    controle_padrao = models.CharField(max_length=10, choices=TecControle.choices, default=TecControle.INDIVIDUAL)
    ativo = models.BooleanField(default=True)

    class Meta:
        ordering = ["grupo", "nome"]
        verbose_name = "Categoria tecnológica"
        verbose_name_plural = "Categorias tecnológicas"

    def __str__(self):
        return self.nome


class AgroUnidadeMedida(models.Model):
    """Unidade de medida dos materiais por quantidade (un, m, cx, rolo…). `fracionavel`
    decide se a quantidade aceita casas decimais (metro aceita; unidade não)."""

    sigla = models.CharField(max_length=8, unique=True)
    nome = models.CharField(max_length=40)
    fracionavel = models.BooleanField(default=False)
    ativo = models.BooleanField(default=True)

    class Meta:
        ordering = ["sigla"]
        verbose_name = "Unidade de medida"
        verbose_name_plural = "Unidades de medida"

    def __str__(self):
        return self.sigla


class TecModelo(AuditModel):
    """Um modelo do catálogo: fabricante + categoria + nome. É ele que decide o TIPO de
    controle (`controle`): `individual` gera equipamentos com número de série;
    `quantidade` gera saldo por local. `estoque_minimo` vale para os dois: nos
    individuais conta equipamentos DISPONÍVEIS. Não há modelos de fábrica: o catálogo
    é só o que a empresa realmente usa."""

    fabricante = models.ForeignKey(TecFabricante, on_delete=models.PROTECT, related_name="modelos")
    categoria = models.ForeignKey(TecCategoria, on_delete=models.PROTECT, related_name="modelos")
    nome = models.CharField(max_length=120)
    controle = models.CharField(max_length=10, choices=TecControle.choices, default=TecControle.INDIVIDUAL, db_index=True)
    unidade = models.ForeignKey(
        AgroUnidadeMedida, on_delete=models.PROTECT, null=True, blank=True, related_name="modelos",
        help_text="Só nos materiais por quantidade. O equipamento individual é sempre contado em unidades.",
    )
    estoque_minimo = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"), validators=[MinValueValidator(Decimal("0"))])
    observacoes = models.TextField(blank=True, default="", help_text="Observações técnicas do modelo.")
    ativo = models.BooleanField(default=True)

    class Meta:
        ordering = ["fabricante__nome", "nome"]
        verbose_name = "Modelo tecnológico"
        verbose_name_plural = "Modelos tecnológicos"
        constraints = [
            models.UniqueConstraint(fields=["fabricante", "nome"], name="tec_modelo_fabricante_nome_unico"),
            models.CheckConstraint(
                condition=models.Q(controle="individual") | models.Q(unidade__isnull=False),
                name="tec_modelo_quantidade_tem_unidade",
            ),
        ]

    def __str__(self):
        return f"{self.fabricante.nome} {self.nome}"


class AgroLocal(AuditModel):
    """Local de armazenamento do Agro (almoxarifado, oficina, container…), dentro de uma
    unidade (fazenda). Onde o material FICA quando não está instalado em nada."""

    # O local que representa "a fazenda inteira": é ele que as telas de estoque oferecem (Fazenda Karitel /
    # Fazenda Rio do Meio). Nasce sozinho em `tec_catalogo_service.locais_das_fazendas()`.
    NOME_PADRAO = "Estoque"

    nome = models.CharField(max_length=100)
    fazenda = models.ForeignKey(Localidade, on_delete=models.PROTECT, related_name="locais_agro", verbose_name="Unidade / fazenda")
    observacoes = models.CharField(max_length=255, blank=True, default="")
    ativo = models.BooleanField(default=True)

    class Meta:
        ordering = ["fazenda__local", "nome"]
        verbose_name = "Local de armazenamento"
        verbose_name_plural = "Locais de armazenamento"
        constraints = [models.UniqueConstraint(fields=["fazenda", "nome"], name="agro_local_fazenda_nome_unico")]

    @property
    def e_padrao(self) -> bool:
        return (self.nome or "").strip().lower() == self.NOME_PADRAO.lower()

    def __str__(self):
        # O local padrão é "a fazenda": "Estoque · Fazenda Karitel" seria dizer a mesma coisa duas vezes.
        return self.fazenda.local if self.e_padrao else f"{self.nome} · {self.fazenda.local}"


class AgroFornecedor(AuditModel):
    """Fornecedor do Agro, ENXUTO (o módulo completo — contatos, histórico — vem depois).
    Separado do `Fornecedor` do TI, que é a âncora dos portais externos."""

    nome = models.CharField(max_length=120)
    documento = models.CharField(max_length=18, blank=True, default="", help_text="CNPJ ou CPF; único quando preenchido.")
    contato = models.CharField(max_length=120, blank=True, default="")
    telefone = models.CharField(max_length=30, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    observacoes = models.TextField(blank=True, default="")
    ativo = models.BooleanField(default=True)

    class Meta:
        ordering = ["nome"]
        verbose_name = "Fornecedor do Agro"
        verbose_name_plural = "Fornecedores do Agro"
        constraints = [
            models.UniqueConstraint(fields=["documento"], condition=~models.Q(documento=""), name="agro_fornecedor_documento_unico"),
        ]

    def __str__(self):
        return self.nome


class TecEquipamento(AuditModel):
    """UM equipamento individual (monitor, receptor, rádio, antena…), identificado pelo
    número de série ou pelo patrimônio. A `situacao` e o `local` só o service escreve:
    `local` é onde ele está GUARDADO (nulo enquanto instalado ou na assistência)."""

    class Situacao(models.TextChoices):
        DISPONIVEL = "disponivel", "Disponível"
        INSTALADO = "instalado", "Instalado"
        EM_MANUTENCAO = "em_manutencao", "Em manutenção"
        INATIVO = "inativo", "Inativo"

    codigo = models.CharField(max_length=12, unique=True, null=True, blank=True, help_text="EQ-00001; gerado pelo sistema.")
    modelo = models.ForeignKey(TecModelo, on_delete=models.PROTECT, related_name="equipamentos")
    numero_serie = models.CharField(max_length=80, blank=True, default="", verbose_name="Número de série")
    patrimonio = models.CharField(max_length=40, blank=True, default="", verbose_name="Patrimônio / identificação interna")
    data_aquisicao = models.DateField(null=True, blank=True, verbose_name="Data de aquisição")
    fornecedor = models.ForeignKey(AgroFornecedor, on_delete=models.SET_NULL, null=True, blank=True, related_name="equipamentos")
    situacao = models.CharField(max_length=14, choices=Situacao.choices, default=Situacao.DISPONIVEL, db_index=True)
    local = models.ForeignKey(AgroLocal, on_delete=models.PROTECT, null=True, blank=True, related_name="equipamentos")
    observacoes = models.TextField(blank=True, default="", verbose_name="Observações técnicas")

    class Meta:
        ordering = ["-pk"]
        verbose_name = "Equipamento tecnológico"
        verbose_name_plural = "Equipamentos tecnológicos"
        permissions = PERMISSOES_TEC
        constraints = [
            models.UniqueConstraint(
                fields=["modelo", "numero_serie"], condition=~models.Q(numero_serie=""), name="tec_equip_modelo_serie_unico",
            ),
            models.UniqueConstraint(fields=["patrimonio"], condition=~models.Q(patrimonio=""), name="tec_equip_patrimonio_unico"),
            # Disponível sem local não existe: "em estoque" é um lugar. Instalado não tem local de guarda.
            models.CheckConstraint(
                condition=~models.Q(situacao="disponivel") | models.Q(local__isnull=False), name="tec_equip_disponivel_tem_local",
            ),
            models.CheckConstraint(
                condition=~models.Q(situacao="instalado") | models.Q(local__isnull=True), name="tec_equip_instalado_sem_local",
            ),
        ]
        indexes = [models.Index(fields=["modelo", "situacao"], name="tec_equip_modelo_sit_idx")]

    def __str__(self):
        return f"{self.codigo or 'EQ-?'} · {self.modelo}"

    def save(self, *args, **kwargs):
        self.numero_serie = (self.numero_serie or "").strip()
        self.patrimonio = (self.patrimonio or "").strip().upper()
        super().save(*args, **kwargs)

    @property
    def instalacao_ativa(self):
        """A instalação em curso (`TecInstalacao` ou `None`). Lê a lista pré-carregada por
        `Prefetch(..., to_attr="ativas_prefetch")` quando a tela a pediu (sem consulta por linha)."""
        carregadas = getattr(self, "ativas_prefetch", None)
        if carregadas is not None:
            return carregadas[0] if carregadas else None
        return self.instalacoes.filter(fim_em__isnull=True).select_related("veiculo", "operador").first()


class TecSaldo(models.Model):
    """Quantidade de um material por quantidade num local. É CACHE do razão: quem escreve é
    `services/tec_estoque_service.py`, que o confere contra `TecMovimentacao`. O banco
    impede o negativo (`CheckConstraint`); o débito é um UPDATE condicional."""

    modelo = models.ForeignKey(TecModelo, on_delete=models.PROTECT, related_name="saldos")
    local = models.ForeignKey(AgroLocal, on_delete=models.PROTECT, related_name="saldos")
    quantidade = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Saldo de material"
        verbose_name_plural = "Saldos de materiais"
        constraints = [
            models.UniqueConstraint(fields=["modelo", "local"], name="tec_saldo_modelo_local_unico"),
            models.CheckConstraint(condition=models.Q(quantidade__gte=0), name="tec_saldo_nao_negativo"),
        ]

    def __str__(self):
        return f"{self.modelo} @ {self.local}: {self.quantidade}"


class TecMovimentacao(models.Model):
    """O RAZÃO da tecnologia agrícola: cada entrada, saída, instalação, retirada,
    transferência… Imutável (`save` recusa a edição, `delete` levanta e o queryset recusa
    `update`/`delete` em massa). Origem e destino são (tipo, id, rótulo): o rótulo é a foto
    do nome no momento, então o histórico se lê mesmo se o destino for renomeado. Linhas de
    UMA mesma ação (a substituição grava duas) compartilham `operacao`; `token` é a chave de
    idempotência de quem clicou duas vezes."""

    class Tipo(models.TextChoices):
        ENTRADA = "entrada", "Entrada no estoque"
        SAIDA = "saida", "Saída / baixa"
        AJUSTE = "ajuste", "Ajuste de saldo"
        INSTALACAO = "instalacao", "Instalação"
        RETIRADA = "retirada", "Retirada"
        TRANSFERENCIA = "transferencia", "Transferência"
        SUBSTITUICAO = "substituicao", "Substituição"
        ENVIO_MANUTENCAO = "envio_manutencao", "Envio para manutenção"
        RETORNO_MANUTENCAO = "retorno_manutencao", "Retorno da manutenção"
        DESVINCULACAO = "desvinculacao", "Desvinculação"
        INATIVACAO = "inativacao", "Inativação"

    class Ponto(models.TextChoices):
        ESTOQUE = "estoque", "Estoque"
        VEICULO = "veiculo", "Máquina / veículo"
        ESTRUTURA = "estrutura", "Torre / estação"
        OPERADOR = "operador", "Operador"
        PONTA = "ponta", "Ponta de enlace"
        MANUTENCAO = "manutencao", "Manutenção / assistência"
        EXTERNO = "externo", "Externo (fornecedor, baixa)"

    tipo = models.CharField(max_length=20, choices=Tipo.choices, db_index=True)
    modelo = models.ForeignKey(TecModelo, on_delete=models.PROTECT, related_name="movimentacoes")
    equipamento = models.ForeignKey(TecEquipamento, on_delete=models.PROTECT, null=True, blank=True, related_name="movimentacoes")
    quantidade = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("1"))
    saldo_depois = models.DecimalField(
        max_digits=14, decimal_places=3, null=True, blank=True, help_text="Saldo do local depois da operação (só material por quantidade).",
    )
    origem_tipo = models.CharField(max_length=12, choices=Ponto.choices, blank=True, default="")
    origem_id = models.PositiveIntegerField(null=True, blank=True)
    origem_rotulo = models.CharField(max_length=160, blank=True, default="")
    destino_tipo = models.CharField(max_length=12, choices=Ponto.choices, blank=True, default="")
    destino_id = models.PositiveIntegerField(null=True, blank=True)
    destino_rotulo = models.CharField(max_length=160, blank=True, default="")
    motivo = models.CharField(max_length=255, blank=True, default="")
    observacoes = models.TextField(blank=True, default="")
    documento = models.CharField(max_length=60, blank=True, default="", help_text="Nota fiscal ou outro documento da entrada.")
    fornecedor = models.ForeignKey(AgroFornecedor, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    usuario = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="+")
    usuario_nome = models.CharField(max_length=150, blank=True, default="", help_text="O login no momento (sobrevive à exclusão do usuário).")
    em = models.DateTimeField(default=timezone.now, db_index=True)
    operacao = models.UUIDField(default=uuid.uuid4, db_index=True, help_text="Agrupa as linhas de uma mesma ação.")
    token = models.UUIDField(null=True, blank=True, unique=True, help_text="Chave de idempotência do formulário (duplo clique).")

    objects = _SomenteInsercaoQuerySet.as_manager()

    class Meta:
        ordering = ["-em", "-pk"]
        verbose_name = "Movimentação tecnológica"
        verbose_name_plural = "Movimentações tecnológicas"
        indexes = [
            models.Index(fields=["equipamento", "-em"], name="tec_mov_equip_idx"),
            models.Index(fields=["modelo", "-em"], name="tec_mov_modelo_idx"),
            models.Index(fields=["destino_tipo", "destino_id"], name="tec_mov_destino_idx"),
            models.Index(fields=["origem_tipo", "origem_id"], name="tec_mov_origem_idx"),
        ]

    def __str__(self):
        return f"{self.get_tipo_display()} · {self.modelo} · {self.em:%d/%m/%Y %H:%M}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("A movimentação é histórico imutável e não pode ser editada.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("A movimentação é histórico imutável e não pode ser apagada.")


class TecEvento(models.Model):
    """Linha do tempo do CADASTRO (criação e edição com de/para, mudança de situação) de
    qualquer coisa do domínio: equipamento, modelo, fabricante, local, estrutura, enlace…
    Imutável, como `FrotaEvento`. As movimentações NÃO se repetem aqui: entram na linha
    do tempo do equipamento por leitura."""

    class Alvo(models.TextChoices):
        EQUIPAMENTO = "equipamento", "Equipamento"
        MODELO = "modelo", "Modelo"
        FABRICANTE = "fabricante", "Fabricante"
        CATEGORIA = "categoria", "Categoria"
        LOCAL = "local", "Local de armazenamento"
        FORNECEDOR = "fornecedor", "Fornecedor"
        UNIDADE = "unidade", "Unidade de medida"
        ESTRUTURA = "estrutura", "Torre / estação"
        ENLACE = "enlace", "Enlace PTP"
        MANUTENCAO = "manutencao", "Manutenção"

    class Tipo(models.TextChoices):
        CADASTRO = "cadastro", "Cadastrado"
        EDICAO = "edicao", "Editado"
        SITUACAO = "situacao", "Situação alterada"

    alvo = models.CharField(max_length=12, choices=Alvo.choices, db_index=True)
    alvo_id = models.PositiveIntegerField(db_index=True)
    tipo = models.CharField(max_length=10, choices=Tipo.choices)
    usuario = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    dados = models.JSONField(default=dict, blank=True)
    em = models.DateTimeField(default=timezone.now)

    objects = _SomenteInsercaoQuerySet.as_manager()

    class Meta:
        ordering = ["-em", "-pk"]
        verbose_name = "Evento de cadastro tecnológico"
        verbose_name_plural = "Eventos de cadastro tecnológico"
        indexes = [models.Index(fields=["alvo", "alvo_id", "-em"], name="tec_evento_alvo_idx")]

    def __str__(self):
        return f"{self.get_alvo_display()} {self.alvo_id} · {self.get_tipo_display()} · {self.em:%d/%m/%Y %H:%M}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("O evento é histórico imutável e não pode ser editado.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("O evento é histórico imutável e não pode ser apagado.")


class TecInstalacao(models.Model):
    """Um PERÍODO em que um equipamento individual esteve instalado num destino: uma máquina
    (`veiculo`) ou uma pessoa (`operador`, o colaborador do RH — rádio HT, por exemplo). Ativa
    enquanto `fim_em` é nulo. Torres e pontas de enlace entram na etapa de infraestrutura, com a
    sua própria coluna e a mesma regra.

    UMA instalação ativa por equipamento, e isso é garantido pela restrição condicional abaixo, não
    pelo código (duas pessoas ao mesmo tempo). EXATAMENTE UM destino por linha. Nunca apagada: a
    única alteração permitida é ENCERRAR o período (`fim_em`, `fim_por`, `motivo_fim`, `mov_fim`),
    e só enquanto ele está ativo. A transferência encerra uma linha e abre outra, então o vínculo
    anterior fica no histórico."""

    CAMPOS_ENCERRAMENTO = ("fim_em", "fim_por", "motivo_fim", "mov_fim")

    equipamento = models.ForeignKey(TecEquipamento, on_delete=models.PROTECT, related_name="instalacoes")
    veiculo = models.ForeignKey(FrotaVeiculo, on_delete=models.PROTECT, null=True, blank=True, related_name="instalacoes_tec")
    operador = models.ForeignKey(Usuario, on_delete=models.PROTECT, null=True, blank=True, related_name="instalacoes_tec")
    inicio_em = models.DateTimeField(default=timezone.now)
    inicio_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    fim_em = models.DateTimeField(null=True, blank=True)
    fim_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    motivo_fim = models.CharField(max_length=255, blank=True, default="")
    mov_inicio = models.ForeignKey(TecMovimentacao, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    mov_fim = models.ForeignKey(TecMovimentacao, on_delete=models.PROTECT, null=True, blank=True, related_name="+")

    class Meta:
        ordering = ["-inicio_em", "-pk"]
        verbose_name = "Instalação de equipamento"
        verbose_name_plural = "Instalações de equipamentos"
        constraints = [
            models.UniqueConstraint(
                fields=["equipamento"], condition=models.Q(fim_em__isnull=True), name="tec_instalacao_equip_ativa_unica",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(veiculo__isnull=False, operador__isnull=True)
                    | models.Q(veiculo__isnull=True, operador__isnull=False)
                ),
                name="tec_instalacao_um_destino",
            ),
            models.CheckConstraint(
                condition=models.Q(fim_em__isnull=True) | models.Q(fim_em__gte=models.F("inicio_em")),
                name="tec_instalacao_fim_depois_do_inicio",
            ),
        ]
        indexes = [
            models.Index(fields=["veiculo", "fim_em"], name="tec_inst_veiculo_idx"),
            models.Index(fields=["operador", "fim_em"], name="tec_inst_operador_idx"),
            models.Index(fields=["equipamento", "-inicio_em"], name="tec_inst_equip_idx"),
        ]

    def __str__(self):
        return f"{self.equipamento_id} → {self.destino_texto} ({'ativa' if self.fim_em is None else 'encerrada'})"

    @property
    def ativa(self) -> bool:
        return self.fim_em is None

    @property
    def destino_tipo(self) -> str:
        return TecMovimentacao.Ponto.VEICULO if self.veiculo_id else TecMovimentacao.Ponto.OPERADOR

    @property
    def destino_id(self):
        return self.veiculo_id or self.operador_id

    @property
    def destino_texto(self) -> str:
        if self.veiculo_id:
            return f"{self.veiculo.codigo} · {self.veiculo.nome}"
        if self.operador_id:
            return self.operador.nome
        return ""

    @property
    def duracao(self):
        """`timedelta` do período (até agora, se ainda ativa)."""
        return (self.fim_em or timezone.now()) - self.inicio_em

    def save(self, *args, **kwargs):
        if not self._state.adding:
            campos = kwargs.get("update_fields")
            if not campos or not set(campos) <= set(self.CAMPOS_ENCERRAMENTO):
                raise ValidationError("A instalação é histórico: só o encerramento do período pode ser gravado.")
            if type(self).objects.filter(pk=self.pk, fim_em__isnull=False).exists():
                raise ValidationError("Esta instalação já foi encerrada e não pode mais ser alterada.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("A instalação é histórico e não pode ser apagada.")


# ════════════════════════════════════════════════════════════════════════════════
# Infraestrutura georreferenciada do Zelo Agro: estruturas (torre, estação, base, ponto de referência) e enlaces PTP
# ════════════════════════════════════════════════════════════════════════════════

class TecEstrutura(AuditModel):
    """Um ponto fixo da infraestrutura: torre, estação, base de comunicação ou ponto de referência. A
    coordenada é a que alguém CADASTROU — é fato de terreno, não dedução — e pode faltar (a estrutura existe
    e aparece nas listas, mas não no mapa, e a tela diz "localização desconhecida"). `localizacao_em` é quando a
    coordenada foi cadastrada ou corrigida pela última vez.

    O que a estrutura TEM instalado vem do estoque tecnológico (`TecInstalacao`); aqui não há cópia disso."""

    class Tipo(models.TextChoices):
        TORRE = "torre", "Torre"
        ESTACAO = "estacao", "Estação"
        BASE = "base", "Base de comunicação"
        REFERENCIA = "referencia", "Ponto de referência"

    class Situacao(models.TextChoices):
        EM_OPERACAO = "em_operacao", "Em operação"
        MANUTENCAO = "manutencao", "Em manutenção"
        DESATIVADA = "desativada", "Desativada"

    nome = models.CharField(max_length=120, unique=True)
    tipo = models.CharField(max_length=12, choices=Tipo.choices, default=Tipo.TORRE, db_index=True)
    situacao = models.CharField(max_length=12, choices=Situacao.choices, default=Situacao.EM_OPERACAO, db_index=True)
    fazenda = models.ForeignKey(
        Localidade, on_delete=models.SET_NULL, null=True, blank=True, related_name="estruturas_agro",
        verbose_name="Unidade / fazenda",
    )
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    localizacao_em = models.DateTimeField(null=True, blank=True)
    altura_m = models.DecimalField("Altura (m)", max_digits=6, decimal_places=1, null=True, blank=True)
    observacoes = models.TextField(blank=True, default="")
    ativo = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ["nome"]
        verbose_name = "Estrutura (torre, estação, base)"
        verbose_name_plural = "Estruturas (torres, estações, bases)"
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(latitude__isnull=True, longitude__isnull=True)
                           | (models.Q(latitude__isnull=False, longitude__isnull=False)
                              & models.Q(latitude__gte=-90, latitude__lte=90, longitude__gte=-180, longitude__lte=180))),
                name="tec_estrutura_coordenada_valida",
            ),
        ]

    def __str__(self):
        return self.nome

    @property
    def tem_coordenada(self) -> bool:
        return self.latitude is not None and self.longitude is not None


class TecEnlace(AuditModel):
    """Um enlace ponto a ponto (PTP) entre duas estruturas. O mapa o desenha como uma LINHA entre as duas
    coordenadas cadastradas — é geografia, não medição: não diz que há comunicação nem a qualidade dela.

    `estado` é o que ALGUÉM REGISTROU (`estado_em`, `estado_por`). Ele nasce "Não informado": rádios cadastrados nas
    pontas não fazem o enlace "operacional". `equipamento_a`/`equipamento_b` são o rádio de cada ponta, só
    referência para consulta (o que se instala e se retira é o motor de instalação do estoque tecnológico)."""

    class Estado(models.TextChoices):
        NAO_INFORMADO = "nao_informado", "Não informado"
        PLANEJADO = "planejado", "Planejado"
        OPERACIONAL = "operacional", "Operacional"
        MANUTENCAO = "manutencao", "Em manutenção"
        INDISPONIVEL = "indisponivel", "Indisponível"

    identificacao = models.CharField("Identificação do enlace", max_length=80, unique=True)
    estrutura_a = models.ForeignKey(TecEstrutura, on_delete=models.PROTECT, related_name="enlaces_como_a", verbose_name="Ponta A")
    estrutura_b = models.ForeignKey(TecEstrutura, on_delete=models.PROTECT, related_name="enlaces_como_b", verbose_name="Ponta B")
    equipamento_a = models.ForeignKey(
        "TecEquipamento", on_delete=models.SET_NULL, null=True, blank=True, related_name="+", verbose_name="Rádio da ponta A",
    )
    equipamento_b = models.ForeignKey(
        "TecEquipamento", on_delete=models.SET_NULL, null=True, blank=True, related_name="+", verbose_name="Rádio da ponta B",
    )
    estado = models.CharField(max_length=14, choices=Estado.choices, default=Estado.NAO_INFORMADO, db_index=True)
    estado_em = models.DateTimeField(null=True, blank=True)
    estado_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    frequencia_ghz = models.DecimalField("Frequência (GHz)", max_digits=5, decimal_places=2, null=True, blank=True)
    observacoes = models.TextField(blank=True, default="")
    ativo = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ["identificacao"]
        verbose_name = "Enlace PTP"
        verbose_name_plural = "Enlaces PTP"
        constraints = [
            models.CheckConstraint(condition=~models.Q(estrutura_a=models.F("estrutura_b")), name="tec_enlace_pontas_diferentes"),
        ]

    def __str__(self):
        return self.identificacao

    @property
    def tem_geometria(self) -> bool:
        """As duas pontas têm coordenada: só então dá para desenhar a linha."""
        return self.estrutura_a.tem_coordenada and self.estrutura_b.tem_coordenada


class FrotaPosicao(models.Model):
    """Uma posição que uma FONTE DE RASTREAMENTO (GPS do veículo, telemetria, rastreador) entregou para um veículo.
    HISTÓRICO IMUTÁVEL: nunca editada, nunca apagada (`save`/`delete` recusam e o queryset recusa `update`/`delete` em
    massa). A posição ATUAL do veículo é `FrotaVeiculo.latitude/longitude` (um espelho da última posição GPS gravada
    aqui) — esta tabela é o que NÃO se sobrescreve: a posição do veículo anterior fica com ele quando o chip muda de
    veículo, porque a linha pertence ao VEÍCULO, e o vínculo do chip é lido (nunca copiado) para dizer "onde está o chip".

    Só entra aqui o que uma integração REAL entregou: a TIM não informa posição, e coordenada cadastrada à mão vive em
    `FrotaVeiculo` com origem "cadastral" (regra 64). `fonte` diz de que sistema veio (é obrigatória: posição sem origem
    é um número sem dono). `chip` é a FOTO do chip instalado no momento da leitura, só quando era exatamente um —
    com vários chips no veículo a pergunta "qual deles?" não tem resposta, e a linha não finge ter."""

    veiculo = models.ForeignKey(FrotaVeiculo, on_delete=models.PROTECT, related_name="posicoes")
    chip = models.ForeignKey(M2MChip, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    latitude = models.DecimalField(max_digits=9, decimal_places=6)
    longitude = models.DecimalField(max_digits=9, decimal_places=6)
    medida_em = models.DateTimeField(help_text="Quando a FONTE mediu a posição (não quando o sistema a recebeu).")
    recebida_em = models.DateTimeField(default=timezone.now, editable=False)
    fonte = models.CharField(max_length=60, help_text="O sistema que entregou a posição (nome da integração ou do rastreador).")
    precisao_m = models.DecimalField("Precisão (m)", max_digits=8, decimal_places=1, null=True, blank=True)
    velocidade_kmh = models.DecimalField("Velocidade (km/h)", max_digits=6, decimal_places=1, null=True, blank=True)
    direcao_graus = models.PositiveSmallIntegerField("Direção (graus)", null=True, blank=True)

    objects = _SomenteInsercaoQuerySet.as_manager()

    class Meta:
        ordering = ["-medida_em", "-pk"]
        verbose_name = "Posição GPS do veículo"
        verbose_name_plural = "Posições GPS dos veículos"
        indexes = [models.Index(fields=["veiculo", "-medida_em"], name="frota_posicao_veiculo_idx")]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(latitude__gte=-90, latitude__lte=90, longitude__gte=-180, longitude__lte=180),
                name="frota_posicao_coordenada_valida",
            ),
            # a mesma leitura reenviada pela fonte (retentativa, reprocessamento) não vira linha nova
            models.UniqueConstraint(fields=["veiculo", "fonte", "medida_em"], name="frota_posicao_leitura_unica"),
            models.CheckConstraint(
                condition=models.Q(direcao_graus__isnull=True) | models.Q(direcao_graus__lte=359),
                name="frota_posicao_direcao_valida",
            ),
        ]

    def __str__(self):
        return f"{self.veiculo_id} · {self.medida_em:%d/%m/%Y %H:%M} · {self.fonte}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("A posição é histórico imutável e não pode ser editada.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("A posição é histórico imutável e não pode ser apagada.")


class FrotaLocalizacaoConfig(models.Model):
    """Quando uma posição GPS deixa de ser "atual" — uma linha só (`get()`), no molde de `M2MIntegracao`.

    É INDEPENDENTE da sincronização da TIM (consumo e sessão do chip): a posição vem de outra fonte, com outro ritmo e
    outros limites, e uma não pode atrasar nem esconder a outra. Cada fonte de GPS entrega num ritmo — de 10 segundos a
    horas, conforme o plano e o equipamento —, então o sistema não decreta um: o TI diz quanto tempo uma posição vale."""

    minutos_atualizada = models.PositiveSmallIntegerField(
        default=30, help_text="Até quantos minutos depois da medição a posição ainda é tratada como ATUAL.",
    )
    horas_sem_atualizacao = models.PositiveSmallIntegerField(
        default=24, help_text="Passado este tempo sem posição nova, a localização é dita SEM ATUALIZAÇÃO RECENTE.",
    )
    atualizado_em = models.DateTimeField(null=True, blank=True)
    atualizado_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        verbose_name = "Configuração da localização GPS"
        verbose_name_plural = "Configuração da localização GPS"

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return f"atual até {self.minutos_atualizada} min; sem atualização depois de {self.horas_sem_atualizacao} h"
