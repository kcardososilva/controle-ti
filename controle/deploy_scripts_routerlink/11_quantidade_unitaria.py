# Fase 2.14 do runbook (DEPLOY_PENDENTE_ROUTERLINK.md).
#
# Roda com (dentro da pasta controle, produção):
#   venv\Scripts\python.exe manage.py shell -c "exec(open(r'deploy_scripts_routerlink\11_quantidade_unitaria.py', encoding='utf-8').read())"
#
# Item.quantidade = 1 para TODO item Routerlink, incondicionalmente (inclusive
# os que já têm outro valor). Verificado em dev: nenhum item Routerlink usa
# tem_lote=True (controle por lote/ItemLote) nem item_consumo='sim' — ou
# seja, quantidade aqui é só um campo solto, não recalculado a partir de
# nenhum outro lugar (diferente do caso descrito na memória
# item_quantidade_vs_lotes, que só vale pra item com controle de lote). Em
# dev, 9 dos 687 itens tinham quantidade != 1 (5 com 2, 4 com 0).
#
# COMO USAR: 1) rode com APLICAR=False, confira a prévia; 2) troque para
# APLICAR=True e rode de novo.

from ProjetoEstoque.models import Item

FORNECEDOR = "Routerlink"
APLICAR = False

itens = list(Item.objects.filter(fornecedor__nome__iexact=FORNECEDOR).exclude(quantidade=1))
print(f"Itens Routerlink com quantidade != 1: {len(itens)}")
for it in itens:
    print(f"  ID {it.pk} | {it.nome} | quantidade atual={it.quantidade} | tem_lote={it.tem_lote} | item_consumo={it.item_consumo}")

com_lote = [it for it in itens if it.tem_lote]
if com_lote:
    print(f"\nAVISO: {len(com_lote)} item(ns) com tem_lote=True — quantidade normalmente é recalculada "
          f"a partir da soma dos ItemLote (ver migration 0140). Definir aqui pode ser sobrescrito depois "
          f"na próxima edição do item. Revisar antes de aplicar:")
    for it in com_lote:
        print(f"  ID {it.pk} | {it.nome}")

if APLICAR:
    for it in itens:
        it.quantidade = 1
    Item.objects.bulk_update(itens, ["quantidade"])
    print(f"\nAPLICADO — {len(itens)} itens com quantidade ajustada para 1.")
else:
    print("\nDRY-RUN — nada alterado. Troque APLICAR=True e rode de novo depois de conferir.")
