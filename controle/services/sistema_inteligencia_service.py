import csv
import unicodedata
from collections import defaultdict
from decimal import Decimal

from django.db.models import Count, Sum, Q
from django.urls import reverse, NoReverseMatch
from django.utils import timezone

from ProjetoEstoque.models import (
    SimNaoChoices,
    StatusUsuarioChoices,
    TipoMovimentacaoChoices,
    TipoTransferenciaChoices,
    TipoMovLicencaChoices,
    Categoria,
    Subtipo,
    Localidade,
    Fornecedor,
    CentroCusto,
    Funcao,
    Item,
    Locacao,
    LoteEstoque,
    ItemLote,
    Usuario,
    MovimentacaoItem,
    Preventiva,
    Licenca,
    LicencaLote,
    MovimentacaoLicenca,
)


class SistemaInteligenciaService:
    """
    Motor central de análise do sistema.

    Objetivo:
    - encontrar dados duplicados;
    - encontrar divergências operacionais;
    - encontrar inconsistências de estoque/lote/licença;
    - gerar KPIs;
    - alimentar dashboard e relatórios.
    """

    SEVERITY_META = {
        "critico": {
            "label": "Crítico",
            "weight": 4,
            "icon": "fa-triangle-exclamation",
        },
        "alto": {
            "label": "Alto",
            "weight": 3,
            "icon": "fa-circle-exclamation",
        },
        "medio": {
            "label": "Médio",
            "weight": 2,
            "icon": "fa-circle-info",
        },
        "baixo": {
            "label": "Baixo",
            "weight": 1,
            "icon": "fa-circle-check",
        },
    }

    SCOPE_LABELS = {
        "usuario": "Usuários",
        "item": "Itens / Equipamentos",
        "licenca": "Licenças",
        "lote": "Lotes",
        "movimentacao": "Movimentações",
        "preventiva": "Preventivas",
        "cadastro": "Cadastros Base",
        "sistema": "Sistema",
    }

    TYPE_LABELS = {
        "duplicado": "Duplicado",
        "divergente": "Divergente",
        "pendencia": "Pendência",
        "cadastro_incompleto": "Cadastro incompleto",
        "saldo": "Saldo / Estoque",
        "vencimento": "Vencimento",
        "risco": "Risco operacional",
    }

    def __init__(self):
        self.today = timezone.localdate()

    # =========================================================
    # HELPERS
    # =========================================================

    def normalizar(self, value):
        if value is None:
            return ""

        value = str(value).strip().lower()
        value = unicodedata.normalize("NFKD", value)
        value = "".join(c for c in value if not unicodedata.combining(c))
        value = " ".join(value.split())

        return value

    def is_blank(self, value):
        return value is None or str(value).strip() == ""

    def safe_decimal(self, value):
        if value is None:
            return Decimal("0.00")

        try:
            return Decimal(value)
        except Exception:
            return Decimal("0.00")

    def reverse_first(self, route_names, pk):
        for name in route_names:
            try:
                return reverse(name, args=[pk])
            except NoReverseMatch:
                continue

        return ""

    def add_issue(
        self,
        issues,
        severity,
        issue_type,
        scope,
        title,
        description,
        identifier="",
        affected_count=1,
        url="",
        hint="",
        payload=None,
    ):
        meta = self.SEVERITY_META.get(severity, self.SEVERITY_META["baixo"])

        issues.append({
            "severity": severity,
            "severity_label": meta["label"],
            "severity_weight": meta["weight"],
            "severity_icon": meta["icon"],
            "type": issue_type,
            "type_label": self.TYPE_LABELS.get(issue_type, issue_type),
            "scope": scope,
            "scope_label": self.SCOPE_LABELS.get(scope, scope),
            "title": title,
            "description": description,
            "identifier": identifier,
            "affected_count": affected_count,
            "url": url,
            "hint": hint,
            "payload": payload or {},
        })

    def group_by_normalized(self, objects, key_func):
        groups = defaultdict(list)

        for obj in objects:
            key = self.normalizar(key_func(obj))

            if key:
                groups[key].append(obj)

        return {
            key: values
            for key, values in groups.items()
            if len(values) > 1
        }

    # =========================================================
    # ANÁLISE PRINCIPAL
    # =========================================================

    def build_report(self, filters=None):
        filters = filters or {}

        issues = []

        self.detect_cadastro_duplicates(issues)
        self.detect_usuario_issues(issues)
        self.detect_item_issues(issues)
        self.detect_lote_issues(issues)
        self.detect_movimentacao_issues(issues)
        self.detect_licenca_issues(issues)
        self.detect_preventiva_issues(issues)

        issues = self.apply_filters(issues, filters)
        issues = sorted(
            issues,
            key=lambda item: (
                -item["severity_weight"],
                item["scope_label"],
                item["title"],
            )
        )

        kpis = self.build_kpis(issues)

        return {
            "issues": issues,
            "kpis": kpis,
        }

    def apply_filters(self, issues, filters):
        q = self.normalizar(filters.get("q"))
        severity = filters.get("severity")
        scope = filters.get("scope")
        issue_type = filters.get("type")

        result = []

        for issue in issues:
            if severity and issue["severity"] != severity:
                continue

            if scope and issue["scope"] != scope:
                continue

            if issue_type and issue["type"] != issue_type:
                continue

            if q:
                searchable = self.normalizar(
                    f"{issue['title']} {issue['description']} "
                    f"{issue['identifier']} {issue['scope_label']} {issue['type_label']}"
                )

                if q not in searchable:
                    continue

            result.append(issue)

        return result

    def build_kpis(self, issues):
        total = len(issues)

        by_severity = defaultdict(int)
        by_scope = defaultdict(int)
        by_type = defaultdict(int)

        for issue in issues:
            by_severity[issue["severity"]] += 1
            by_scope[issue["scope"]] += 1
            by_type[issue["type"]] += 1

        return {
            "total": total,
            "criticos": by_severity["critico"],
            "altos": by_severity["alto"],
            "medios": by_severity["medio"],
            "baixos": by_severity["baixo"],
            "duplicados": by_type["duplicado"],
            "divergentes": by_type["divergente"],
            "pendencias": by_type["pendencia"],
            "cadastro_incompleto": by_type["cadastro_incompleto"],
            "riscos": by_type["risco"],
            "usuarios": by_scope["usuario"],
            "itens": by_scope["item"],
            "licencas": by_scope["licenca"],
            "lotes": by_scope["lote"],
            "movimentacoes": by_scope["movimentacao"],
            "preventivas": by_scope["preventiva"],
        }

    # =========================================================
    # DUPLICADOS CADASTRAIS
    # =========================================================

    def detect_cadastro_duplicates(self, issues):
        # Usuários por nome normalizado
        usuarios_dup_nome = self.group_by_normalized(
            Usuario.objects.all().only("id", "nome", "matricula", "email"),
            lambda u: u.nome
        )

        for _, users in usuarios_dup_nome.items():
            nomes = ", ".join(f"{u.nome} ({u.matricula or 'sem matrícula'})" for u in users[:5])

            self.add_issue(
                issues,
                severity="alto",
                issue_type="duplicado",
                scope="usuario",
                title="Possível duplicidade de usuário por nome",
                description=f"Foram encontrados {len(users)} usuários com nome igual ou muito semelhante: {nomes}.",
                identifier=users[0].nome,
                affected_count=len(users),
                url=self.reverse_first(["usuario_detail"], users[0].pk),
                hint="Validar se são pessoas distintas. Se forem o mesmo colaborador, manter apenas um cadastro consolidado.",
            )

        # Usuários por e-mail
        usuarios_dup_email = self.group_by_normalized(
            Usuario.objects.exclude(email__isnull=True).exclude(email="").only("id", "nome", "email"),
            lambda u: u.email
        )

        for _, users in usuarios_dup_email.items():
            self.add_issue(
                issues,
                severity="critico",
                issue_type="duplicado",
                scope="usuario",
                title="E-mail duplicado em usuários",
                description=f"O e-mail {users[0].email} está vinculado a {len(users)} usuários.",
                identifier=users[0].email,
                affected_count=len(users),
                url=self.reverse_first(["usuario_detail"], users[0].pk),
                hint="E-mail deve ser único para evitar erro em termo, licença e rastreabilidade.",
            )

        # Itens por número de série
        itens_dup_serie = self.group_by_normalized(
            Item.objects.exclude(numero_serie__isnull=True).exclude(numero_serie="").only("id", "nome", "numero_serie"),
            lambda i: i.numero_serie
        )

        for _, itens in itens_dup_serie.items():
            self.add_issue(
                issues,
                severity="critico",
                issue_type="duplicado",
                scope="item",
                title="Número de série duplicado",
                description=f"O número de série {itens[0].numero_serie} aparece em {len(itens)} itens.",
                identifier=itens[0].numero_serie,
                affected_count=len(itens),
                url=self.reverse_first(["equipamento_detalhe", "item_detail"], itens[0].pk),
                hint="Número de série deve ser identificador operacional único para equipamentos não consumíveis.",
            )

        # Fornecedor por CNPJ
        fornecedores_dup_cnpj = self.group_by_normalized(
            Fornecedor.objects.exclude(cnpj__isnull=True).exclude(cnpj="").only("id", "nome", "cnpj"),
            lambda f: f.cnpj
        )

        for _, fornecedores in fornecedores_dup_cnpj.items():
            self.add_issue(
                issues,
                severity="alto",
                issue_type="duplicado",
                scope="cadastro",
                title="CNPJ duplicado em fornecedores",
                description=f"O CNPJ {fornecedores[0].cnpj} está cadastrado em {len(fornecedores)} fornecedores.",
                identifier=fornecedores[0].cnpj,
                affected_count=len(fornecedores),
                hint="Consolidar fornecedores duplicados para evitar compras/lotes separados indevidamente.",
            )

        # Centro de custo por número
        cc_dup_numero = self.group_by_normalized(
            CentroCusto.objects.all().only("id", "numero", "departamento"),
            lambda c: c.numero
        )

        for _, centros in cc_dup_numero.items():
            self.add_issue(
                issues,
                severity="alto",
                issue_type="duplicado",
                scope="cadastro",
                title="Centro de custo duplicado por número",
                description=f"O número {centros[0].numero} aparece em {len(centros)} centros de custo.",
                identifier=centros[0].numero,
                affected_count=len(centros),
                hint="Centro de custo duplicado distorce apropriação de custos, dashboards e relatórios.",
            )

        # Licenças por nome
        lic_dup_nome = self.group_by_normalized(
            Licenca.objects.all().only("id", "nome"),
            lambda l: l.nome
        )

        for _, licencas in lic_dup_nome.items():
            self.add_issue(
                issues,
                severity="medio",
                issue_type="duplicado",
                scope="licenca",
                title="Licença duplicada por nome",
                description=f"A licença {licencas[0].nome} possui {len(licencas)} cadastros semelhantes.",
                identifier=licencas[0].nome,
                affected_count=len(licencas),
                url=self.reverse_first(["licenca_detail"], licencas[0].pk),
                hint="Consolidar licenças equivalentes evita distorção em lotes e custos.",
            )

        # Localidades por nome
        loc_dup_nome = self.group_by_normalized(
            Localidade.objects.all().only("id", "local"),
            lambda l: l.local
        )

        for _, locais in loc_dup_nome.items():
            self.add_issue(
                issues,
                severity="baixo",
                issue_type="duplicado",
                scope="cadastro",
                title="Localidade duplicada",
                description=f"A localidade {locais[0].local} possui {len(locais)} cadastros semelhantes.",
                identifier=locais[0].local,
                affected_count=len(locais),
                hint="Padronizar localidades melhora filtros e relatórios.",
            )

        # Funções por nome
        fun_dup_nome = self.group_by_normalized(
            Funcao.objects.all().only("id", "nome"),
            lambda f: f.nome
        )

        for _, funcoes in fun_dup_nome.items():
            self.add_issue(
                issues,
                severity="baixo",
                issue_type="duplicado",
                scope="cadastro",
                title="Função duplicada",
                description=f"A função {funcoes[0].nome} possui {len(funcoes)} cadastros semelhantes.",
                identifier=funcoes[0].nome,
                affected_count=len(funcoes),
                hint="Padronizar funções melhora consultas por cargo e integração com RH.",
            )

    # =========================================================
    # USUÁRIOS
    # =========================================================

    def detect_usuario_issues(self, issues):
        usuarios = Usuario.objects.select_related("centro_custo", "localidade", "funcao")

        for usuario in usuarios:
            url = self.reverse_first(["usuario_detail"], usuario.pk)

            if self.is_blank(usuario.matricula):
                self.add_issue(
                    issues,
                    "medio",
                    "cadastro_incompleto",
                    "usuario",
                    "Usuário sem matrícula",
                    f"O usuário {usuario.nome} não possui matrícula informada.",
                    identifier=usuario.nome,
                    url=url,
                    hint="Atualizar pela importação mensal do RH ou preencher manualmente.",
                )

            if self.is_blank(usuario.email):
                self.add_issue(
                    issues,
                    "medio",
                    "cadastro_incompleto",
                    "usuario",
                    "Usuário sem e-mail",
                    f"O usuário {usuario.nome} está sem e-mail cadastrado.",
                    identifier=usuario.nome,
                    url=url,
                    hint="Gerar e-mail padrão ou atualizar com base no RH.",
                )

            if usuario.status == StatusUsuarioChoices.ATIVO and usuario.data_termino:
                self.add_issue(
                    issues,
                    "alto",
                    "divergente",
                    "usuario",
                    "Usuário ativo com data de término",
                    f"O usuário {usuario.nome} está ativo, mas possui data de término {usuario.data_termino:%d/%m/%Y}.",
                    identifier=usuario.nome,
                    url=url,
                    hint="Validar se deve ser desligado ou remover a data de término.",
                )

            if usuario.status == StatusUsuarioChoices.DESLIGADO and not usuario.data_termino:
                self.add_issue(
                    issues,
                    "medio",
                    "divergente",
                    "usuario",
                    "Usuário desligado sem data de término",
                    f"O usuário {usuario.nome} está desligado, mas não possui data de término.",
                    identifier=usuario.nome,
                    url=url,
                    hint="Informar data de desligamento para controle dos 30 dias de remoção de licenças.",
                )

            if usuario.centro_custo:
                cc_text = self.normalizar(f"{usuario.centro_custo.numero} {usuario.centro_custo.departamento}")

                if usuario.centro_custo.pmb == SimNaoChoices.SIM and usuario.pmb != SimNaoChoices.SIM:
                    self.add_issue(
                        issues,
                        "medio",
                        "divergente",
                        "usuario",
                        "Usuário em centro PMB marcado como não PMB",
                        f"O usuário {usuario.nome} pertence ao centro de custo PMB {usuario.centro_custo}, mas está com PMB = Não.",
                        identifier=usuario.nome,
                        url=url,
                        hint="Aplicar regra automática: centro PMB deve refletir usuário PMB.",
                    )

                if "tabaco" in cc_text and usuario.pmb != SimNaoChoices.SIM:
                    self.add_issue(
                        issues,
                        "alto",
                        "divergente",
                        "usuario",
                        "Usuário do centro Tabaco não marcado como PMB",
                        f"O usuário {usuario.nome} pertence ao centro de custo {usuario.centro_custo}, mas PMB está como Não.",
                        identifier=usuario.nome,
                        url=url,
                        hint="Ajustar PMB para Sim conforme regra definida na importação do RH.",
                    )

        self.detect_usuario_desligado_com_pendencias(issues)

    def detect_usuario_desligado_com_pendencias(self, issues):
        active_items_by_user = self.get_active_items_by_user()
        active_licenses_by_user = self.get_active_licenses_by_user()

        usuarios_desligados = Usuario.objects.filter(status=StatusUsuarioChoices.DESLIGADO)

        for usuario in usuarios_desligados:
            itens = active_items_by_user.get(usuario.pk, [])
            licencas = active_licenses_by_user.get(usuario.pk, [])

            if not itens and not licencas:
                continue

            url = self.reverse_first(["usuario_detail"], usuario.pk)

            self.add_issue(
                issues,
                "critico",
                "risco",
                "usuario",
                "Usuário desligado com ativos ou licenças vinculadas",
                (
                    f"O usuário {usuario.nome} está desligado e ainda possui "
                    f"{len(itens)} item(ns) e {len(licencas)} licença(s) vinculados."
                ),
                identifier=usuario.nome,
                affected_count=len(itens) + len(licencas),
                url=url,
                hint="Acessar o detalhe do usuário e devolver/remover os ativos e licenças pendentes.",
                payload={
                    "itens": [item.nome for item in itens[:5]],
                    "licencas": [mov.licenca.nome for mov in licencas[:5]],
                }
            )

    # =========================================================
    # ITENS
    # =========================================================

    def detect_item_issues(self, issues):
        from django.db.models import Prefetch
        itens = (
            Item.objects
            .select_related("centro_custo", "localidade", "subtipo", "categoria", "fornecedor")
            .prefetch_related("vinculos_lote", "preventivas", "locacao")
        )

        for item in itens:
            url = self.reverse_first(["equipamento_detalhe", "item_detail"], item.pk)

            if item.item_consumo == SimNaoChoices.SIM and item.locado == SimNaoChoices.SIM:
                self.add_issue(
                    issues,
                    "critico",
                    "divergente",
                    "item",
                    "Item de consumo cadastrado como locado",
                    f"O item {item.nome} está marcado como consumo e locado ao mesmo tempo.",
                    identifier=str(item),
                    url=url,
                    hint="A regra do model já impede isso no cadastro, mas há dado legado inconsistente.",
                )

            if item.precisa_preventiva == SimNaoChoices.SIM and not item.data_limite_preventiva:
                self.add_issue(
                    issues,
                    "alto",
                    "cadastro_incompleto",
                    "item",
                    "Item precisa preventiva, mas não possui periodicidade",
                    f"O item {item.nome} está marcado como precisa preventiva, mas não tem data_limite_preventiva.",
                    identifier=str(item),
                    url=url,
                    hint="Informar periodicidade em dias.",
                )

            if item.tem_lote and not item.vinculos_lote.exists():
                self.add_issue(
                    issues,
                    "alto",
                    "divergente",
                    "item",
                    "Item com controle por lote sem vínculo de lote",
                    f"O item {item.nome} exige controle por lote, mas não possui vínculo em ItemLote.",
                    identifier=str(item),
                    url=url,
                    hint="Realizar entrada com lote ou corrigir o flag tem_lote.",
                )

            if not item.tem_lote and item.vinculos_lote.exists():
                self.add_issue(
                    issues,
                    "medio",
                    "divergente",
                    "item",
                    "Item sem controle por lote possui vínculo de lote",
                    f"O item {item.nome} não está marcado como controlado por lote, mas possui vínculo em ItemLote.",
                    identifier=str(item),
                    url=url,
                    hint="Validar se o item deve ter tem_lote=True.",
                )

            if item.locado == SimNaoChoices.SIM:
                locacao = getattr(item, "locacao", None)

                if not locacao:
                    self.add_issue(
                        issues,
                        "alto",
                        "cadastro_incompleto",
                        "item",
                        "Item locado sem dados de locação",
                        f"O item {item.nome} está marcado como locado, mas não possui registro em Locacao.",
                        identifier=str(item),
                        url=url,
                        hint="Criar registro de locação ou revisar o campo locado.",
                    )

            if item.item_consumo == SimNaoChoices.NAO and self.is_blank(item.numero_serie):
                self.add_issue(
                    issues,
                    "baixo",
                    "cadastro_incompleto",
                    "item",
                    "Item não consumível sem número de série",
                    f"O item {item.nome} não é consumo e não possui número de série.",
                    identifier=str(item),
                    url=url,
                    hint="Cadastrar número de série para rastreabilidade patrimonial.",
                )

    # =========================================================
    # LOTES
    # =========================================================

    def detect_lote_issues(self, issues):
        for vinculo in ItemLote.objects.select_related("item", "lote"):
            url = self.reverse_first(["equipamento_detalhe", "item_detail"], vinculo.item_id)

            if vinculo.quantidade_disponivel > vinculo.quantidade_entrada:
                self.add_issue(
                    issues,
                    "critico",
                    "saldo",
                    "lote",
                    "Saldo de item/lote maior que quantidade de entrada",
                    (
                        f"O vínculo {vinculo} possui saldo disponível "
                        f"{vinculo.quantidade_disponivel}, maior que a entrada {vinculo.quantidade_entrada}."
                    ),
                    identifier=str(vinculo),
                    url=url,
                    hint="Corrigir saldo disponível do vínculo ItemLote.",
                )

            if vinculo.custo_unitario <= 0:
                self.add_issue(
                    issues,
                    "alto",
                    "divergente",
                    "lote",
                    "Vínculo de lote com custo unitário inválido",
                    f"O vínculo {vinculo} possui custo unitário menor ou igual a zero.",
                    identifier=str(vinculo),
                    url=url,
                    hint="Ajustar custo unitário para manter relatórios financeiros confiáveis.",
                )

        for lote in LoteEstoque.objects.select_related("fornecedor"):
            total_vinculos = lote.itens_vinculados.aggregate(total=Sum("quantidade_entrada"))["total"] or 0

            if total_vinculos > lote.quantidade:
                self.add_issue(
                    issues,
                    "critico",
                    "saldo",
                    "lote",
                    "Quantidade vinculada maior que quantidade do lote",
                    (
                        f"O lote NF {lote.numero_nf} possui quantidade {lote.quantidade}, "
                        f"mas os vínculos somam {total_vinculos}."
                    ),
                    identifier=f"NF {lote.numero_nf}",
                    affected_count=total_vinculos,
                    hint="Revisar entradas/vínculos do lote.",
                )

            if lote.custo_unitario <= 0:
                self.add_issue(
                    issues,
                    "alto",
                    "divergente",
                    "lote",
                    "Lote com custo unitário inválido",
                    f"O lote NF {lote.numero_nf} possui custo unitário menor ou igual a zero.",
                    identifier=f"NF {lote.numero_nf}",
                    hint="Corrigir custo unitário do lote.",
                )

        nf_groups = (
            LoteEstoque.objects
            .values("numero_nf", "fornecedor_id")
            .annotate(total=Count("id"))
            .filter(total__gt=1)
        )

        for group in nf_groups:
            self.add_issue(
                issues,
                "medio",
                "duplicado",
                "lote",
                "Possível duplicidade de lote por NF e fornecedor",
                (
                    f"A NF {group['numero_nf']} aparece em {group['total']} lotes "
                    f"para o mesmo fornecedor."
                ),
                identifier=group["numero_nf"],
                affected_count=group["total"],
                hint="Validar se são entradas diferentes ou duplicidade de cadastro.",
            )

        for lote in LicencaLote.objects.select_related("licenca", "fornecedor"):
            url = self.reverse_first(["licenca_detail"], lote.licenca_id)

            if lote.quantidade_disponivel > lote.quantidade_total:
                self.add_issue(
                    issues,
                    "critico",
                    "saldo",
                    "licenca",
                    "Saldo de lote de licença maior que quantidade comprada",
                    (
                        f"O lote #{lote.pk} da licença {lote.licenca.nome} possui "
                        f"saldo {lote.quantidade_disponivel}, maior que o total {lote.quantidade_total}."
                    ),
                    identifier=f"Lote licença #{lote.pk}",
                    url=url,
                    hint="Corrigir saldo disponível da licença.",
                )

            if not lote.custo_ciclo or lote.custo_ciclo <= 0:
                self.add_issue(
                    issues,
                    "alto",
                    "cadastro_incompleto",
                    "licenca",
                    "Lote de licença sem custo de ciclo válido",
                    f"O lote #{lote.pk} da licença {lote.licenca.nome} está sem custo_ciclo válido.",
                    identifier=f"Lote licença #{lote.pk}",
                    url=url,
                    hint="Informar custo de ciclo para cálculo correto de custo mensal/anual.",
                )

    # =========================================================
    # MOVIMENTAÇÕES
    # =========================================================

    def detect_movimentacao_issues(self, issues):
        movs = MovimentacaoItem.objects.select_related("item", "usuario", "lote")

        for mov in movs:
            url = self.reverse_first(["movimentacao_detail"], mov.pk)

            if mov.tipo_movimentacao == TipoMovimentacaoChoices.ENTRADA:
                if mov.item and mov.item.tem_lote and not mov.lote_id:
                    self.add_issue(
                        issues,
                        "critico",
                        "divergente",
                        "movimentacao",
                        "Entrada de item controlado por lote sem lote",
                        f"A movimentação #{mov.pk} é uma entrada de item com controle por lote, mas não possui lote vinculado.",
                        identifier=f"Movimentação #{mov.pk}",
                        url=url,
                        hint="Vincular lote ou revisar a movimentação.",
                    )

            if mov.tipo_movimentacao == TipoMovimentacaoChoices.BAIXA:
                if mov.item and mov.item.tem_lote and not mov.lote_id:
                    self.add_issue(
                        issues,
                        "alto",
                        "divergente",
                        "movimentacao",
                        "Baixa de item com lote sem informar lote",
                        f"A movimentação #{mov.pk} baixa um item controlado por lote, mas não informa qual lote saiu.",
                        identifier=f"Movimentação #{mov.pk}",
                        url=url,
                        hint="Baixa por lote precisa informar lote para manter saldo correto.",
                    )

            if mov.tipo_movimentacao == TipoMovimentacaoChoices.TRANSFERENCIA:
                if mov.tipo_transferencia == TipoTransferenciaChoices.ENTREGA and not mov.usuario_id:
                    self.add_issue(
                        issues,
                        "alto",
                        "divergente",
                        "movimentacao",
                        "Entrega sem usuário responsável",
                        f"A movimentação #{mov.pk} é entrega, mas não possui usuário vinculado.",
                        identifier=f"Movimentação #{mov.pk}",
                        url=url,
                        hint="Informar usuário responsável pela posse do item.",
                    )

                if not mov.termo_pdf:
                    self.add_issue(
                        issues,
                        "medio",
                        "cadastro_incompleto",
                        "movimentacao",
                        "Transferência sem termo PDF",
                        f"A movimentação #{mov.pk} não possui termo de responsabilidade anexado.",
                        identifier=f"Movimentação #{mov.pk}",
                        url=url,
                        hint="Anexar termo para rastreabilidade patrimonial.",
                    )

            if mov.quantidade <= 0:
                self.add_issue(
                    issues,
                    "critico",
                    "divergente",
                    "movimentacao",
                    "Movimentação com quantidade inválida",
                    f"A movimentação #{mov.pk} possui quantidade menor ou igual a zero.",
                    identifier=f"Movimentação #{mov.pk}",
                    url=url,
                    hint="Corrigir quantidade da movimentação.",
                )

    # =========================================================
    # LICENÇAS
    # =========================================================

    def detect_licenca_issues(self, issues):
        movs = MovimentacaoLicenca.objects.select_related("usuario", "licenca", "lote", "centro_custo_destino")

        for mov in movs:
            url = self.reverse_first(["licenca_detail"], mov.licenca_id)

            if mov.tipo == TipoMovLicencaChoices.ATRIBUICAO:
                if not mov.usuario_id:
                    self.add_issue(
                        issues,
                        "critico",
                        "divergente",
                        "licenca",
                        "Atribuição de licença sem usuário",
                        f"A movimentação de licença #{mov.pk} é atribuição, mas não possui usuário.",
                        identifier=f"Mov. licença #{mov.pk}",
                        url=url,
                        hint="Atribuição precisa de usuário para rastreabilidade.",
                    )

                if not mov.lote_id:
                    self.add_issue(
                        issues,
                        "medio",
                        "cadastro_incompleto",
                        "licenca",
                        "Atribuição de licença sem lote",
                        f"A licença {mov.licenca.nome} foi atribuída sem lote vinculado.",
                        identifier=mov.licenca.nome,
                        url=url,
                        hint="Se possível, vincular ao lote de compra da licença.",
                    )

                if not mov.valor_unitario or mov.valor_unitario <= 0:
                    self.add_issue(
                        issues,
                        "alto",
                        "cadastro_incompleto",
                        "licenca",
                        "Atribuição de licença sem valor unitário",
                        f"A licença {mov.licenca.nome} foi atribuída sem valor_unitario válido.",
                        identifier=mov.licenca.nome,
                        url=url,
                        hint="Corrigir valor unitário para cálculo financeiro por usuário.",
                    )

        active_licenses_by_user = self.get_active_licenses_by_user()

        usuarios_map = {
            u.pk: u
            for u in Usuario.objects.filter(pk__in=active_licenses_by_user.keys())
        }

        for usuario_id, movs_ativas in active_licenses_by_user.items():
            usuario = usuarios_map.get(usuario_id)

            if not usuario:
                continue

            if usuario.status == StatusUsuarioChoices.DESLIGADO and movs_ativas:
                self.add_issue(
                    issues,
                    "critico",
                    "risco",
                    "licenca",
                    "Usuário desligado ainda possui licenças ativas",
                    f"O usuário {usuario.nome} está desligado e possui {len(movs_ativas)} licença(s) ativa(s).",
                    identifier=usuario.nome,
                    affected_count=len(movs_ativas),
                    url=self.reverse_first(["usuario_detail"], usuario.pk),
                    hint="Remover todas as licenças no detalhe do usuário.",
                )

    # =========================================================
    # PREVENTIVAS
    # =========================================================

    def detect_preventiva_issues(self, issues):
        itens_com_preventiva = Item.objects.filter(precisa_preventiva=SimNaoChoices.SIM)

        for item in itens_com_preventiva:
            if not item.preventivas.exists():
                self.add_issue(
                    issues,
                    "alto",
                    "pendencia",
                    "preventiva",
                    "Item marcado para preventiva sem plano preventivo",
                    f"O item {item.nome} está marcado como precisa preventiva, mas não possui preventiva cadastrada.",
                    identifier=str(item),
                    url=self.reverse_first(["equipamento_detalhe", "item_detail"], item.pk),
                    hint="Criar preventiva vinculada ao item.",
                )

        for preventiva in Preventiva.objects.select_related("equipamento", "checklist_modelo"):
            if preventiva.data_proxima and preventiva.data_proxima < self.today:
                self.add_issue(
                    issues,
                    "alto",
                    "vencimento",
                    "preventiva",
                    "Preventiva vencida",
                    (
                        f"A preventiva do item {preventiva.equipamento.nome} venceu em "
                        f"{preventiva.data_proxima:%d/%m/%Y}."
                    ),
                    identifier=str(preventiva.equipamento),
                    url=self.reverse_first(["equipamento_detalhe", "item_detail"], preventiva.equipamento_id),
                    hint="Registrar execução ou reprogramar preventiva.",
                )

            if preventiva.checklist_modelo is None:
                self.add_issue(
                    issues,
                    "medio",
                    "cadastro_incompleto",
                    "preventiva",
                    "Preventiva sem modelo de checklist",
                    f"A preventiva do item {preventiva.equipamento.nome} não possui modelo de checklist.",
                    identifier=str(preventiva.equipamento),
                    url=self.reverse_first(["equipamento_detalhe", "item_detail"], preventiva.equipamento_id),
                    hint="Vincular modelo de checklist para padronizar execução.",
                )

    # =========================================================
    # SNAPSHOTS OPERACIONAIS
    # =========================================================

    def get_active_items_by_user(self):
        result = defaultdict(list)
        latest_by_item = {}

        qs = (
            MovimentacaoItem.objects
            .select_related("item", "usuario")
            .filter(item__isnull=False)
            .order_by("item_id", "-created_at", "-id")
        )

        for mov in qs:
            if mov.item_id in latest_by_item:
                continue

            latest_by_item[mov.item_id] = mov

            if not mov.usuario_id:
                continue

            if mov.tipo_movimentacao in [
                TipoMovimentacaoChoices.BAIXA,
                TipoMovimentacaoChoices.ENTRADA,
                TipoMovimentacaoChoices.ENVIO_MANUTENCAO,
            ]:
                continue

            if mov.tipo_movimentacao == TipoMovimentacaoChoices.TRANSFERENCIA:
                if mov.tipo_transferencia == TipoTransferenciaChoices.DEVOLUCAO:
                    continue

            result[mov.usuario_id].append(mov.item)

        return result

    def get_active_licenses_by_user(self):
        result = defaultdict(list)
        processed = set()

        qs = (
            MovimentacaoLicenca.objects
            .select_related("usuario", "licenca", "lote")
            .filter(usuario__isnull=False, licenca__isnull=False)
            .order_by("usuario_id", "licenca_id", "lote_id", "-created_at", "-id")
        )

        for mov in qs:
            key = (mov.usuario_id, mov.licenca_id, mov.lote_id)

            if key in processed:
                continue

            processed.add(key)

            if mov.tipo == TipoMovLicencaChoices.ATRIBUICAO:
                result[mov.usuario_id].append(mov)

        return result

    # =========================================================
    # BUSCA GLOBAL
    # =========================================================

    def global_search(self, query, limit=8):
        q = (query or "").strip()

        if len(q) < 2:
            return []

        results = []

        for usuario in Usuario.objects.filter(
            Q(nome__icontains=q) |
            Q(email__icontains=q) |
            Q(matricula__icontains=q)
        ).select_related("centro_custo")[:limit]:
            results.append({
                "scope": "Usuário",
                "title": usuario.nome,
                "subtitle": f"Matrícula: {usuario.matricula or '—'} · {usuario.email or 'sem e-mail'}",
                "url": self.reverse_first(["usuario_detail"], usuario.pk),
                "icon": "fa-user",
            })

        for item in Item.objects.filter(
            Q(nome__icontains=q) |
            Q(numero_serie__icontains=q) |
            Q(modelo__icontains=q) |
            Q(marca__icontains=q)
        ).select_related("subtipo")[:limit]:
            results.append({
                "scope": "Item",
                "title": item.nome,
                "subtitle": f"Série: {item.numero_serie or '—'} · Status: {item.get_status_display()}",
                "url": self.reverse_first(["equipamento_detalhe", "item_detail"], item.pk),
                "icon": "fa-box",
            })

        for licenca in Licenca.objects.filter(nome__icontains=q)[:limit]:
            results.append({
                "scope": "Licença",
                "title": licenca.nome,
                "subtitle": f"Fornecedor: {licenca.fornecedor or '—'}",
                "url": self.reverse_first(["licenca_detail"], licenca.pk),
                "icon": "fa-certificate",
            })

        for fornecedor in Fornecedor.objects.filter(
            Q(nome__icontains=q) |
            Q(cnpj__icontains=q)
        )[:limit]:
            results.append({
                "scope": "Fornecedor",
                "title": fornecedor.nome,
                "subtitle": f"CNPJ: {fornecedor.cnpj or '—'}",
                "url": "",
                "icon": "fa-truck",
            })

        return results[:limit * 4]