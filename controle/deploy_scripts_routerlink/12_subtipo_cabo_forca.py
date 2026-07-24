# Fase 2.15 do runbook (DEPLOY_PENDENTE_ROUTERLINK.md).
#
# Roda com (dentro da pasta controle, produção):
#   venv\Scripts\python.exe manage.py shell -c "exec(open(r'deploy_scripts_routerlink\12_subtipo_cabo_forca.py', encoding='utf-8').read())"
#
# Cria o Subtipo "Cabo de Força" (categoria="Equipamento", mesmo padrão de
# "Fonte"/"Gbic" — acessório, não item alocado a colaborador) — já
# recomendado no runbook desde a Fase 2.6, nunca criado até agora. Aplica em
# todo item Routerlink com modelo="CAB-ACBZ-10A" sem subtipo. Em dev: 139
# itens (era a maior lacuna de subtipo do fornecedor, 137+ estimado
# originalmente na Fase 2.6).
#
# NÃO inclui "CAB-ACBZ-12A" (5 itens em dev, mesma família/finalidade, ampe-
# ragem diferente) — só o modelo pedido explicitamente. Ver aviso impresso.
#
# COMO USAR: 1) rode com APLICAR=False, confira a prévia; 2) troque para
# APLICAR=True e rode de novo.

from ProjetoEstoque.models import Categoria, Item, Subtipo

FORNECEDOR = "Routerlink"
MODELO_ALVO = "CAB-ACBZ-10A"
SUBTIPO_NOME = "Cabo de Força"
CATEGORIA_NOME = "Equipamento"
ALOCADO = "nao"
APLICAR = False

categoria = Categoria.objects.filter(nome__iexact=CATEGORIA_NOME).first()
if not categoria:
    print(f"ERRO: Categoria {CATEGORIA_NOME!r} não encontrada — confira o nome exato em produção.")

subtipo = Subtipo.objects.filter(nome__iexact=SUBTIPO_NOME).first()
print(f"Subtipo {SUBTIPO_NOME!r}: {'já existe (id=' + str(subtipo.pk) + ')' if subtipo else 'será criado'}")

alvo = list(Item.objects.filter(fornecedor__nome__iexact=FORNECEDOR, modelo__iexact=MODELO_ALVO, subtipo__isnull=True))
print(f"Itens {MODELO_ALVO!r} sem subtipo: {len(alvo)}")

primos = Item.objects.filter(fornecedor__nome__iexact=FORNECEDOR, modelo__iexact="CAB-ACBZ-12A", subtipo__isnull=True)
if primos.exists():
    print(f"\nAVISO: {primos.count()} item(ns) 'CAB-ACBZ-12A' (mesma família, amperagem diferente) "
          f"também sem subtipo — NÃO incluídos aqui, só o modelo pedido explicitamente.")

if APLICAR and categoria:
    if not subtipo:
        subtipo = Subtipo.objects.create(nome=SUBTIPO_NOME, categoria=categoria, alocado=ALOCADO)
        print(f"\nCriado Subtipo {SUBTIPO_NOME!r} (id={subtipo.pk}).")
    for it in alvo:
        it.subtipo = subtipo
    Item.objects.bulk_update(alvo, ["subtipo"])
    print(f"APLICADO — {len(alvo)} itens atualizados com subtipo {SUBTIPO_NOME!r}.")
else:
    print("\nDRY-RUN — nada alterado. Troque APLICAR=True e rode de novo depois de conferir.")
