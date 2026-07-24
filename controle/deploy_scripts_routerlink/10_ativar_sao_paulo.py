# Fase 2.13 do runbook (DEPLOY_PENDENTE_ROUTERLINK.md).
#
# Roda com (dentro da pasta controle, produção):
#   venv\Scripts\python.exe manage.py shell -c "exec(open(r'deploy_scripts_routerlink\10_ativar_sao_paulo.py', encoding='utf-8').read())"
#
# Ativação manual dos equipamentos da filial São Paulo (Fase 2.8/05_cadastro_
# sao_paulo.py): nasceram com status Backup por pedido explícito do usuário
# ("mantenha o status como backup, depois realizamos a ativação manual").
# Este é esse "depois" — muda status para Ativo em TODO item Routerlink cuja
# Localidade seja "TI - SP" (ajuste LOCALIDADE_NOME no topo se o nome exato
# em produção for diferente).
#
# Ativo e Backup estão ambos no conjunto "ATIVOS" de services/locacao_service.py
# — a troca NÃO abre/fecha LocacaoPeriodo (nenhuma ação de histórico de
# locação necessária). Verificado em dev: nenhuma Preventiva pausada nesses
# itens (não têm preventiva ainda), então também não precisa de retomar().
# Se em produção algum desses itens JÁ tiver Preventiva pausada quando você
# rodar isto, o script chama retomar() nela automaticamente (senão o item
# ficaria "ativo" mas a preventiva continuaria congelada, o que seria outro
# bug do tipo "congelado" — ver corrigir_periodos_locacao.py).
#
# COMO USAR: 1) rode com APLICAR=False, confira a prévia; 2) troque para
# APLICAR=True e rode de novo.

from ProjetoEstoque.models import Item, Preventiva, StatusItemChoices

FORNECEDOR = "Routerlink"
LOCALIDADE_NOME = "TI - SP"
APLICAR = False

itens = list(
    Item.objects.filter(fornecedor__nome__iexact=FORNECEDOR, localidade__local__iexact=LOCALIDADE_NOME)
    .exclude(status=StatusItemChoices.ATIVO)
)
print(f"Itens Routerlink em {LOCALIDADE_NOME!r} fora do status Ativo: {len(itens)}")
for it in itens:
    print(f"  ID {it.pk} | {it.nome} | status atual={it.status}")

prevs_pausadas = list(Preventiva.objects.filter(equipamento__in=itens, pausada=True))
print(f"\nPreventivas pausadas nesses itens (serão retomadas): {len(prevs_pausadas)}")
for p in prevs_pausadas:
    print(f"  Item {p.equipamento_id} | checklist={p.checklist_modelo}")

if APLICAR:
    for it in itens:
        it.status = StatusItemChoices.ATIVO
    Item.objects.bulk_update(itens, ["status"])
    for p in prevs_pausadas:
        p.retomar()
    print(f"\nAPLICADO — {len(itens)} itens ativados, {len(prevs_pausadas)} preventivas retomadas.")
else:
    print("\nDRY-RUN — nada alterado. Troque APLICAR=True e rode de novo depois de conferir.")
