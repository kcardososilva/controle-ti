# Fase 2.9 do runbook (DEPLOY_PENDENTE_ROUTERLINK.md).
#
# Roda com (dentro da pasta controle, produção):
#   venv\Scripts\python.exe manage.py shell -c "exec(open(r'deploy_scripts_routerlink\06_marca.py', encoding='utf-8').read())"
#
# Preenche Item.marca em QUALQUER Item Routerlink sem marca — correspondência
# exata por Modelo contra outro Item já classificado, com fallback por
# prefixo de part number (lista PREFIXOS_CISCO abaixo; em dev, TODO prefixo
# de part number Routerlink observado até agora é Cisco, inclusive Meraki —
# aqui marca="Cisco", "Meraki" fica só no Subtipo).
#
# Também corrige um bug pré-existente (não relacionado a esta leva de
# trabalho): itens com marca == modelo literalmente iguais (erro de
# importação antigo) — a query abaixo lista quem se encaixa; SÓ CORRIGE se
# você confirmar que realmente é o mesmo padrão de erro (marca claramente
# não é um nome de fabricante de verdade).
#
# COMO USAR: 1) rode com APLICAR=False, confira a prévia (incluindo a lista
# de possíveis marca==modelo); 2) troque para APLICAR=True e rode de novo.

import unicodedata
from collections import Counter, defaultdict

from django.db.models import F
from ProjetoEstoque.models import Item

FORNECEDOR = "Routerlink"
APLICAR = False

PREFIXOS_CISCO = (
    "air-", "ws-", "cp-", "c3kx-", "vic2-", "pvdm3-", "hwic-", "vwic2-", "wic-",
    "ism-", "ucsc-", "cab-acbz", "cab-stk-e", "cab-7513ac", "sfp-", "pwr-inj",
    "c2960s-stack", "cisco", "mx64", "mx67", "mx75", "mx84",
)


def norm(s):
    if not s:
        return ""
    s = str(s).strip().lower()
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


print("=== Possível bug pré-existente: marca == modelo ===")
suspeitos = list(Item.objects.filter(fornecedor__nome__iexact=FORNECEDOR, marca=F("modelo")))
for it in suspeitos:
    print(f"  ID {it.pk} | nome={it.nome!r} | modelo={it.modelo!r} | marca={it.marca!r}")
if not suspeitos:
    print("  nenhum encontrado.")

base = Item.objects.filter(fornecedor__nome__iexact=FORNECEDOR).exclude(marca__isnull=True).exclude(marca="")
base = base.exclude(pk__in=[it.pk for it in suspeitos])  # não usar os corrompidos como referência
por_modelo = defaultdict(Counter)
for it in base:
    por_modelo[norm(it.modelo)][it.marca.strip().title()] += 1
mapa_exato = {m: c.most_common(1)[0][0] for m, c in por_modelo.items()}

alvo = list(Item.objects.filter(fornecedor__nome__iexact=FORNECEDOR).filter(marca__isnull=True) | Item.objects.filter(fornecedor__nome__iexact=FORNECEDOR).filter(marca=""))
alvo = list({it.pk: it for it in alvo}.values())
print(f"\nItens Routerlink sem marca: {len(alvo)}")

aplicar_lista = []
sem_match = Counter()
resumo = Counter()
for it in alvo:
    m = norm(it.modelo)
    if m in mapa_exato:
        it.marca = mapa_exato[m]
        aplicar_lista.append(it)
        resumo[it.marca] += 1
    elif m.startswith(PREFIXOS_CISCO):
        it.marca = "Cisco"
        aplicar_lista.append(it)
        resumo["Cisco (por prefixo)"] += 1
    else:
        sem_match[it.modelo] += 1

print(f"\nCom marca a aplicar: {len(aplicar_lista)}")
for nome, n in resumo.most_common():
    print(f"  {nome}: {n}")

print(f"\nSEM correspondência (ficam sem marca — revisar manualmente): {sum(sem_match.values())}")
for modelo, n in sem_match.most_common():
    print(f"  {n}\t{modelo}")

if APLICAR:
    Item.objects.bulk_update(aplicar_lista, ["marca"])
    print(f"\nAPLICADO — {len(aplicar_lista)} itens atualizados por modelo.")
    if suspeitos:
        for it in suspeitos:
            it.marca = "Cisco"  # ajuste manualmente aqui se algum suspeito não for realmente Cisco
        Item.objects.bulk_update(suspeitos, ["marca"])
        print(f"APLICADO — {len(suspeitos)} registros com marca==modelo corrigidos para Cisco.")
else:
    print("\nDRY-RUN — nada alterado. Troque APLICAR=True e rode de novo depois de conferir.")
