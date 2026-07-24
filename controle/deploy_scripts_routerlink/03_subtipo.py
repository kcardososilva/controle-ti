# Fase 2.6 (parte 2) do runbook (DEPLOY_PENDENTE_ROUTERLINK.md).
#
# Roda com (dentro da pasta controle, produção):
#   venv\Scripts\python.exe manage.py shell -c "exec(open(r'deploy_scripts_routerlink\03_subtipo.py', encoding='utf-8').read())"
#
# Genérico e reutilizável: preenche Subtipo em QUALQUER Item Routerlink que
# esteja sem subtipo, usando o subtipo já usado por OUTRO equipamento
# Routerlink com o mesmo Modelo (moda — valor mais comum), mais as extensões
# manuais abaixo (família de modelo sem correspondência exata, mas cujo
# subtipo eu já confirmei olhando outro Item da mesma família — ver o
# raciocínio completo na conversa do Claude Code, seção "1 - adicione as
# marcas..."). Roda depois de qualquer cadastro novo (Fase 2.2 ou 2.8) —
# ordem não importa entre as duas, mas rodar isto ANTES de cadastrar tudo
# dá uma base de referência menor (menos preciso). Idempotente — só toca
# item com subtipo=NULL, nunca sobrescreve.
#
# COMO USAR: 1) rode com APLICAR=False, confira a prévia; 2) troque para
# APLICAR=True e rode de novo.

import unicodedata
from collections import Counter, defaultdict

from ProjetoEstoque.models import Item, Subtipo

FORNECEDOR = "Routerlink"
APLICAR = False

# Extensões por família — modelo sem correspondência EXATA na base já
# classificada, mas cujo subtipo correto eu confirmei olhando um Item real
# da mesma linha de produto (ex.: CP-7937G é um telefone IP da mesma família
# que CP-7911G/CP-7942G, já classificados como "IP-Phone").
EXTENSOES = {
    "CP-7937G": "IP-Phone",
    "CP-7962G": "IP-Phone",
    "SFP-GE-T": "Gbic",
    "CP-PWR-CUBE-3": "Fonte",
    "C3KX-PWR-350WAC": "Fonte",
    "AIR-AP3802I-Z-K9": "Access-Point",
    "AIR-CT2504-K9": "Cisco WLC",
    "CISCO2921/K9": "ROTEADOR",
    "CP-8945-L-K9": "IP-Phone",
    "HEADSET-CHS-60": "Headset",
    "C2960S-STACK": "CABO STACK",
    "CAB-STK-E-0.5M": "CABO STACK",
    "MX67-HW": "Meraki",
}


def norm(s):
    if not s:
        return ""
    s = str(s).strip().lower()
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


base = Item.objects.filter(fornecedor__nome__iexact=FORNECEDOR).exclude(subtipo__isnull=True).select_related("subtipo")
por_modelo = defaultdict(Counter)
for it in base:
    por_modelo[norm(it.modelo)][it.subtipo_id] += 1
mapa_exato = {m: c.most_common(1)[0][0] for m, c in por_modelo.items()}

subtipo_por_nome = {s.nome: s.pk for s in Subtipo.objects.all()}
faltando_subtipo_cadastro = [nome for nome in set(EXTENSOES.values()) if nome not in subtipo_por_nome]
if faltando_subtipo_cadastro:
    print(f"AVISO: estes Subtipo não existem em produção, extensão pulada: {faltando_subtipo_cadastro}")

mapa_extensoes = {norm(m): subtipo_por_nome[nome] for m, nome in EXTENSOES.items() if nome in subtipo_por_nome}
mapa_final = {**mapa_exato, **mapa_extensoes}

alvo = list(Item.objects.filter(fornecedor__nome__iexact=FORNECEDOR, subtipo__isnull=True))
print(f"Itens Routerlink sem subtipo: {len(alvo)}")

por_modelo_alvo = defaultdict(list)
for it in alvo:
    por_modelo_alvo[it.modelo].append(it)

aplicar_lista = []
sem_match = Counter()
resumo_aplicado = Counter()
subtipo_nome_por_id = {v: k for k, v in subtipo_por_nome.items()}
for modelo, itens in por_modelo_alvo.items():
    chave = norm(modelo)
    if chave in mapa_final:
        subtipo_id = mapa_final[chave]
        for it in itens:
            it.subtipo_id = subtipo_id
            aplicar_lista.append(it)
        resumo_aplicado[subtipo_nome_por_id.get(subtipo_id, subtipo_id)] += len(itens)
    else:
        sem_match[modelo] = len(itens)

print(f"\nCom subtipo a aplicar: {len(aplicar_lista)}")
for nome, n in resumo_aplicado.most_common():
    print(f"  {nome}: {n}")

print(f"\nSEM correspondência (ficam sem subtipo — revisar manualmente): {sum(sem_match.values())}")
for modelo, n in sem_match.most_common():
    print(f"  {n}\t{modelo}")

if APLICAR:
    Item.objects.bulk_update(aplicar_lista, ["subtipo"])
    print(f"\nAPLICADO — {len(aplicar_lista)} itens atualizados.")
else:
    print("\nDRY-RUN — nada alterado. Troque APLICAR=True e rode de novo depois de conferir.")
