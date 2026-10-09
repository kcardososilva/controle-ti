"""
Auditoria do módulo de licenças — dinheiro parado e contadores divergentes.

Responde, de uma vez, as três perguntas que ninguém faz até doer:

  1. **Que assentos estão no nome de quem já saiu da empresa?** É despesa
     recorrente por licença de quem não trabalha mais aqui. A cascata de
     desligamento cobre quem foi desligado PELO fluxo; quem foi marcado como
     desligado direto na ficha (ou antes da cascata existir) deixou o assento
     preso e invisível, porque nenhuma tela cruzava "assento ativo" com
     "status do colaborador".

  2. **Algum contador de saldo divergiu do razão?** `quantidade_disponivel` é
     cache; `MovimentacaoLicenca` é a verdade. Divergência faz a tela oferecer
     assento que não existe (ou esconder assento pago).

  3. **Que pools estão esgotados?** A próxima atribuição vai falhar na frente
     do operador, e dá para comprar antes.

  4. **O que vence, quanto custa e quanto disso está parado?** A renovação
     chegava pela fatura do fornecedor — ou seja, a empresa descobria o
     vencimento DEPOIS de renovar, quando já não havia decisão a tomar. O
     valor do ciclo aparece ao lado dos assentos ociosos daquele lote, porque
     juntos eles mudam a pergunta de "renova?" para "renova quantos?".

Sai com código 1 se achar assento órfão ou divergência — para ser agendado no
Task Scheduler e **falhar visivelmente**, mesma convenção dos outros comandos
agendados do projeto. Pool esgotado é aviso, não falha: é estado legítimo.

**Só lê.** O reparo de contador é `--reconciliar`, explícito, e mesmo assim
nunca toca no razão: alinha o cache ao que as movimentações dizem.

Uso:
  python manage.py auditar_licencas
  python manage.py auditar_licencas --reconciliar
  python manage.py auditar_licencas --dias 90     # janela da renovação
"""
import logging

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


def _brl(valor):
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


class Command(BaseCommand):
    help = (
        "Audita licenças: assentos presos em colaborador desligado, saldo de lote "
        "divergente do razão e pools esgotados."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--reconciliar",
            action="store_true",
            help="Alinha o saldo dos lotes divergentes ao razão (não altera movimentações).",
        )
        # 60 dias é o padrão porque é a janela em que a renovação ainda é uma
        # DECISÃO: dá tempo de medir uso, cortar assento ocioso e negociar.
        # Com 30 já só dá para aprovar o que o fornecedor mandou.
        parser.add_argument(
            "--dias",
            type=int,
            default=60,
            help="Janela do relatório de renovação, em dias (padrão: 60).",
        )

    def handle(self, *args, **opts):
        from services import licenca_service as ls

        resumo = ls.resumo_saude()
        orfaos = resumo["orfaos"]
        divergencias = resumo["divergencias"]
        esgotados = resumo["esgotados"]

        # ── 1. Assentos órfãos ───────────────────────────────────────────
        self.stdout.write(self.style.MIGRATE_HEADING(
            "\n1. ASSENTOS NO NOME DE COLABORADOR NÃO ATIVO"
        ))
        if not orfaos:
            self.stdout.write(self.style.SUCCESS("   Nenhum. Todas as licenças estão com gente ativa."))
        else:
            for o in orfaos:
                self.stdout.write(self.style.ERROR(
                    f"   {o['mensal']:>10} /mês  {o['licenca'].nome[:34]:<34} "
                    f"{o['usuario'].nome[:26]:<26} ({o['usuario'].status})"
                ))
            self.stdout.write(self.style.ERROR(
                f"\n   {len(orfaos)} assento(s) preso(s) — "
                f"{_brl(resumo['orfaos_mensal'])}/mês, "
                f"{_brl(resumo['orfaos_anual'])}/ano saindo por licença de quem já saiu."
            ))
            self.stdout.write(
                "   Resolva em /licencas/saude/ (botão devolver) ou na ficha do colaborador."
            )

        # ── 2. Divergências de contador ──────────────────────────────────
        self.stdout.write(self.style.MIGRATE_HEADING(
            "\n2. SALDO DE LOTE x RAZÃO"
        ))
        if not divergencias:
            self.stdout.write(self.style.SUCCESS("   Todos os contadores batem com as movimentações."))
        else:
            for d in divergencias:
                self.stdout.write(self.style.ERROR(
                    f"   lote #{d['lote'].pk:<5} {d['licenca'].nome[:30]:<30} "
                    f"gravado={d['gravado']:<4} razão={d['esperado']:<4} total={d['total']}"
                ))
                for motivo in d["motivos"]:
                    self.stdout.write(f"       · {motivo}")

            if opts["reconciliar"]:
                self.stdout.write("")
                corrigidos = 0
                for d in divergencias:
                    r = ls.reconciliar_lote(
                        d["lote"], observacao="manage.py auditar_licencas --reconciliar",
                    )
                    if r["mudou"]:
                        corrigidos += 1
                        self.stdout.write(self.style.WARNING(
                            f"   lote #{d['lote'].pk}: saldo {r['antes']} -> {r['depois']}"
                        ))
                self.stdout.write(self.style.SUCCESS(
                    f"   {corrigidos} lote(s) reconciliado(s). Nenhuma movimentação alterada."
                ))
            else:
                self.stdout.write(
                    "\n   Para alinhar os contadores: "
                    "manage.py auditar_licencas --reconciliar"
                )

        # ── 3. Pools esgotados ───────────────────────────────────────────
        self.stdout.write(self.style.MIGRATE_HEADING("\n3. POOLS ESGOTADOS"))
        if not esgotados:
            self.stdout.write(self.style.SUCCESS("   Nenhum. Todas as licenças têm assento livre."))
        else:
            for e in esgotados:
                self.stdout.write(self.style.WARNING(
                    f"   {e['licenca'].nome[:40]:<40} {e['em_uso']}/{e['total']} em uso"
                ))
            self.stdout.write(
                f"\n   {len(esgotados)} licença(s) sem assento livre — a próxima "
                "atribuição vai falhar."
            )

        if resumo["sem_lote"]:
            self.stdout.write(self.style.MIGRATE_HEADING("\n4. LICENÇA SEM LOTE (cadastro pela metade)"))
            for lic in resumo["sem_lote"]:
                self.stdout.write(self.style.WARNING(f"   {lic.nome}"))

        # ── 5. Renovação ─────────────────────────────────────────────────
        dias = opts["dias"]
        renov = ls.resumo_renovacoes(dias=dias)

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n5. RENOVAÇÃO — PRÓXIMOS {dias} DIAS"
        ))
        if not renov["linhas"]:
            self.stdout.write(self.style.SUCCESS(
                f"   Nada vencendo na janela de {dias} dias."
            ))
        else:
            for r in renov["linhas"]:
                prazo = (
                    f"VENCIDO há {r['dias_abs']}d" if r["vencido"]
                    else f"em {r['dias']}d"
                )
                estilo = self.style.ERROR if r["dias"] <= 30 else self.style.WARNING
                self.stdout.write(estilo(
                    f"   {prazo:>16}  {r['licenca'].nome[:30]:<30} "
                    f"lote #{r['lote'].pk:<5} {r['em_uso']:>3}/{r['total']:<4} em uso  "
                    f"{_brl(r['custo_ciclo']):>16}"
                    + ("  [RENOVACAO MANUAL]" if not r["automatica"] else "")
                ))
            self.stdout.write(
                f"\n   {len(renov['linhas'])} lote(s), "
                f"{_brl(renov['total_valor'])} a renovar."
            )
            if renov["total_desperdicio"] > 0:
                self.stdout.write(self.style.WARNING(
                    f"   {_brl(renov['total_desperdicio'])} disso é assento comprado "
                    "e NAO usado — economia disponível sem negociar preço."
                ))
            if renov["manuais"]:
                self.stdout.write(self.style.ERROR(
                    f"   {len(renov['manuais'])} contrato(s) NAO renovam sozinhos — "
                    "no vencimento a licença simplesmente para."
                ))

        # Lote sem data nenhuma não é "não vence": é lote que o sistema não
        # consegue vigiar, e a diferença importa para quem confia no relatório.
        if renov["sem_data"]:
            self.stdout.write(self.style.WARNING(
                f"\n   {len(renov['sem_data'])} lote(s) SEM data de compra nem de "
                "renovação — fora do calendário. Preencha em /licencas/renovacoes/."
            ))

        # ── Saída ────────────────────────────────────────────────────────
        # Vencido e "manual prestes a vencer" entram no código de saída porque
        # os dois param serviço: o primeiro já parou, o segundo para sozinho se
        # ninguém agir. Pool esgotado continua sendo AVISO — é estado legítimo.
        renov_critica = bool(renov["vencidos"]) or bool(renov["manuais"])
        falhou = (
            bool(orfaos)
            or bool(divergencias and not opts["reconciliar"])
            or renov_critica
        )
        self.stdout.write("")
        if falhou:
            logger.warning(
                "Auditoria de licenças: %s assento(s) órfão(s) (%s/mês), "
                "%s divergência(s), %s vencido(s), %s renovação(ões) manual(is).",
                len(orfaos), resumo["orfaos_mensal"], len(divergencias),
                len(renov["vencidos"]), len(renov["manuais"]),
            )
            raise SystemExit(1)

        self.stdout.write(self.style.SUCCESS("Módulo de licenças sem pendência."))
