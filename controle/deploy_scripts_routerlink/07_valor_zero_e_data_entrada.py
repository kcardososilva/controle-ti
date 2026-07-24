# Fase 2.10 do runbook (DEPLOY_PENDENTE_ROUTERLINK.md).
#
# Roda com (dentro da pasta controle, produção):
#   venv\Scripts\python.exe manage.py shell -c "exec(open(r'deploy_scripts_routerlink\07_valor_zero_e_data_entrada.py', encoding='utf-8').read())"
#
# Duas ações independentes, para TODOS os itens Routerlink (não só o lote
# desta sessão) — rodar ANTES do 08_contrato.py, que depende de
# Locacao.data_entrada já preenchida:
#
# A) Item.valor (Valor de Aquisição) = 0 para todo item Routerlink com
#    locado='sim' — inclusive os que já têm um valor > 0 cadastrado. A
#    justificativa é que o item é locado (não comprado): "valor de
#    aquisição" não se aplica, o custo real é Locacao.valor_mensal, já usado
#    nos dashboards (ver regra do CLAUDE.md "custo_itens = Locacao.valor_mensal,
#    não Item.valor"). ESCOPO DELIBERADAMENTE RESTRITO a locado='sim': há 7
#    itens Routerlink com locado='nao' (ids 124, 1220, 1247, 1267, 1285,
#    1332, 1382 em dev — nobreaks e telefones IP com valor de aquisição real
#    cadastrado) que NÃO são tocados aqui — zerar o valor deles apagaria um
#    dado financeiro potencialmente real sem justificativa de negócio (eles
#    não têm Locacao nenhuma, então não são "locados sem valor de aquisição"
#    como o resto). Ver AVISO impresso abaixo — mesmo pré-existente já citado
#    no runbook (achado pendente: possível locado mal marcado). NÃO
#    reversível a partir de backup automático — o relatório abaixo lista o
#    valor antigo de cada item tocado, para conferência/rollback manual.
#
# B) Locacao.data_entrada ausente -> preenchida por inferência (moda do
#    Modelo igual em outra locação Routerlink já com data; sem
#    correspondência, usa a data mais comum entre TODAS as locações
#    Routerlink já preenchidas). Realinha o período aberto (LocacaoPeriodo)
#    correspondente para a mesma data, quando existir e divergir — mesma
#    lógica já usada em corrigir_periodos_locacao.py, mas aqui aplicada a
#    QUALQUER item Routerlink sem data, não só ao lote marcado desta sessão.
#    NUNCA sobrescreve uma data_entrada já preenchida.
#
# COMO USAR: 1) rode com APLICAR=False, confira a prévia; 2) troque para
# APLICAR=True e rode de novo.

import unicodedata
from collections import Counter, defaultdict
from decimal import Decimal

from ProjetoEstoque.models import Item, Locacao, LocacaoPeriodo

FORNECEDOR = "Routerlink"
APLICAR = True


def norm(s):
    if not s:
        return ""
    s = str(s).strip().lower()
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


print("=== AVISO: itens Routerlink com locado='nao' (fora do escopo do item A) ===")
sem_locacao = Item.objects.filter(fornecedor__nome__iexact=FORNECEDOR, locado="nao")
for it in sem_locacao:
    print(f"  ID {it.pk} | {it.nome} | valor={it.valor} | status={it.status}")
if not sem_locacao:
    print("  nenhum encontrado.")

print("\n=== A) Item.valor -> 0 (Valor de Aquisição, só locado='sim') ===")
itens_valor = list(
    Item.objects.filter(fornecedor__nome__iexact=FORNECEDOR, locado="sim").exclude(valor=Decimal("0"))
)
print(f"Itens Routerlink locados com valor != 0 (inclui NULL): {len(itens_valor)}")
nao_nulos = [it for it in itens_valor if it.valor is not None and it.valor != 0]
print(f"  ...dos quais JÁ TINHAM um valor > 0 cadastrado (serão zerados): {len(nao_nulos)}")
for it in sorted(nao_nulos, key=lambda x: -x.valor)[:20]:
    print(f"    ID {it.pk} | {it.nome} | valor atual R$ {it.valor}")
if len(nao_nulos) > 20:
    print(f"    ... e mais {len(nao_nulos) - 20}")

print("\n=== B) Locacao.data_entrada ausente -> inferência por Modelo ===")
alvo_loc = list(
    Locacao.objects.filter(equipamento__fornecedor__nome__iexact=FORNECEDOR, data_entrada__isnull=True)
    .select_related("equipamento")
)
print(f"Locações Routerlink sem data_entrada: {len(alvo_loc)}")

base_qs = Locacao.objects.filter(
    equipamento__fornecedor__nome__iexact=FORNECEDOR, data_entrada__isnull=False
).select_related("equipamento")
por_modelo = defaultdict(Counter)
global_counter = Counter()
for loc in base_qs:
    chave = norm(loc.equipamento.modelo)
    por_modelo[chave][loc.data_entrada] += 1
    global_counter[loc.data_entrada] += 1
data_fallback = global_counter.most_common(1)[0][0] if global_counter else None
print(f"Data de entrada mais comum entre as já preenchidas (fallback global): {data_fallback}")

preenchidos = []
for loc in alvo_loc:
    item = loc.equipamento
    chave = norm(item.modelo)
    if chave in por_modelo:
        data_alvo = por_modelo[chave].most_common(1)[0][0]
        origem = "Modelo igual em outra locação Routerlink"
    else:
        data_alvo = data_fallback
        origem = "Data mais comum entre as locações Routerlink (sem modelo correspondente)"
    periodo_aberto = LocacaoPeriodo.objects.filter(item=item, data_fim__isnull=True).first()
    preenchidos.append({
        "loc": loc, "item": item, "data_alvo": data_alvo, "origem": origem,
        "periodo_aberto": periodo_aberto,
    })
    print(f"  ID {item.pk} | {item.nome} | modelo={item.modelo} -> data_entrada={data_alvo} ({origem})")

if APLICAR:
    for it in itens_valor:
        it.valor = Decimal("0")
    Item.objects.bulk_update(itens_valor, ["valor"])
    print(f"\nAPLICADO (A) — {len(itens_valor)} itens com valor zerado.")

    realinhados = 0
    for p in preenchidos:
        loc = p["loc"]
        loc.data_entrada = p["data_alvo"]
        loc.save(update_fields=["data_entrada", "updated_at"])
        periodo = p["periodo_aberto"]
        if periodo and periodo.data_inicio != p["data_alvo"]:
            periodo.data_inicio = p["data_alvo"]
            periodo.valor_mensal = loc.valor_mensal
            periodo.save(update_fields=["data_inicio", "valor_mensal", "updated_at"])
            realinhados += 1
    print(f"APLICADO (B) — {len(preenchidos)} data_entrada preenchidas, {realinhados} períodos realinhados.")
else:
    print(f"\nDRY-RUN — nada alterado. {len(itens_valor)} valores seriam zerados, "
          f"{len(preenchidos)} data_entrada seriam preenchidas. Troque APLICAR=True e rode de novo depois de conferir.")
