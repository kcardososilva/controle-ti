# Fase 2.11 do runbook (DEPLOY_PENDENTE_ROUTERLINK.md).
#
# Roda com (dentro da pasta controle, produção):
#   venv\Scripts\python.exe manage.py shell -c "exec(open(r'deploy_scripts_routerlink\08_contrato.py', encoding='utf-8').read())"
#
# DEPENDE do 07_valor_zero_e_data_entrada.py já ter rodado (com APLICAR=True)
# antes — a regra abaixo decide o número do contrato pela Locacao.data_entrada,
# então rodar isto antes deixaria alguns itens sem data para basear a decisão.
#
# Padrão identificado em produção/dev: 394 de 687 locações Routerlink já
# tinham Locacao.contrato preenchido. 'RL240423-12-24' é o contrato "mestre"
# (317 ocorrências, cobre TODAS as datas de entrada, de 2025-02-04 a
# 2026-02-04). 'RL260287' é um contrato novo usado SOMENTE pelo lote que
# entrou em 2026-06-26 (12 de 13 itens dessa data). As ~44 variações únicas
# "RL240423-12-XX" (cada uma aparecendo 1x só) e o valor "9844" (2x) são
# resíduos pré-existentes de outra origem (possivelmente número de pedido
# lançado no campo errado) — NÃO são tocados aqui, e não fazem parte do
# padrão aplicado (só preenchemos o que está em branco, nunca sobrescrevemos
# um contrato já preenchido, seja qual for o valor).
#
# Regra aplicada aos itens SEM contrato:
#   data_entrada == DATA_CONTRATO_NOVO  -> CONTRATO_NOVO
#   qualquer outra data (ou sem data)   -> CONTRATO_MESTRE
#
# COMO USAR: 1) rode com APLICAR=False, confira a prévia; 2) troque para
# APLICAR=True e rode de novo.

import datetime
from collections import Counter

from ProjetoEstoque.models import Locacao

FORNECEDOR = "Routerlink"
CONTRATO_MESTRE = "RL240423-12-24"
CONTRATO_NOVO = "RL260287"
DATA_CONTRATO_NOVO = datetime.date(2026, 6, 26)
APLICAR = True

alvo = list(
    Locacao.objects.filter(equipamento__fornecedor__nome__iexact=FORNECEDOR, contrato__isnull=True)
    .select_related("equipamento")
) + list(
    Locacao.objects.filter(equipamento__fornecedor__nome__iexact=FORNECEDOR, contrato="")
    .select_related("equipamento")
)
alvo = list({loc.pk: loc for loc in alvo}.values())
print(f"Locações Routerlink sem contrato: {len(alvo)}")

resumo = Counter()
aplicar_lista = []
for loc in alvo:
    contrato = CONTRATO_NOVO if loc.data_entrada == DATA_CONTRATO_NOVO else CONTRATO_MESTRE
    loc.contrato = contrato
    aplicar_lista.append(loc)
    resumo[contrato] += 1

print("\nContrato a aplicar:")
for val, n in resumo.most_common():
    print(f"  {n}\t{val}")

if APLICAR:
    Locacao.objects.bulk_update(aplicar_lista, ["contrato"])
    print(f"\nAPLICADO — {len(aplicar_lista)} locações atualizadas.")
else:
    print("\nDRY-RUN — nada alterado. Troque APLICAR=True e rode de novo depois de conferir.")
