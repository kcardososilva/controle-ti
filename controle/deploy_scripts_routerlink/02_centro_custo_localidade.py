# Fase 2.6 (parte 1) do runbook (DEPLOY_PENDENTE_ROUTERLINK.md).
#
# Roda com (dentro da pasta controle, produção):
#   venv\Scripts\python.exe manage.py shell -c "exec(open(r'deploy_scripts_routerlink\02_centro_custo_localidade.py', encoding='utf-8').read())"
#
# Duas regras distintas do usuário:
#   1) TODO equipamento Routerlink deve estar no Centro de Custo "TI" —
#      regra geral, sem exceção conhecida (em dev, 100% dos que já tinham
#      CC preenchido já eram TI). Aplica em QUALQUER item Routerlink com
#      centro_custo em branco, não só nos cadastrados nesta leva.
#   2) Equipamento em status Backup vai para "Escritório TI - Karitel" —
#      ESCOPO RESTRITO de propósito aos itens cadastrados via
#      aplicar_analise_cadastral (aba "Fazenda" do fornecedor, fisicamente
#      em Karitel). NÃO generalizei essa parte pra "todo item Routerlink em
#      backup sem localidade", porque agora sabemos que existe uma filial
#      em São Paulo — aplicar Karitel de forma cega em itens de origem
#      desconhecida arriscaria localizar errado equipamento de outro lugar.
#      Rodar a Fase 2.8 (cadastro São Paulo) ANTES ou DEPOIS deste script
#      não faz diferença — o marcador usado aqui só pega o lote da Fazenda.
#
# COMO USAR: 1) rode com APLICAR=False, confira as contagens; 2) troque
# para APLICAR=True e rode de novo.

from ProjetoEstoque.models import Item, CentroCusto, Localidade

FORNECEDOR = "Routerlink"
CC_NUMERO = "12105"
CC_DEPARTAMENTO = "TI"
LOCALIDADE_BACKUP_FAZENDA = "ESCRITÓRIO TI - KARITEL"
MARCADOR_LOTE_FAZENDA = "aplicar_analise_cadastral"
APLICAR = False

cc_ti = CentroCusto.objects.filter(numero=CC_NUMERO, departamento=CC_DEPARTAMENTO).first()
if not cc_ti:
    print(f"ERRO: CentroCusto numero={CC_NUMERO!r} departamento={CC_DEPARTAMENTO!r} não encontrado — confira o cadastro em produção antes de continuar.")
else:
    print(f"CentroCusto alvo: {cc_ti.pk} — {cc_ti.numero} {cc_ti.departamento}")

loc_karitel = Localidade.objects.filter(local=LOCALIDADE_BACKUP_FAZENDA).first()
if not loc_karitel:
    print(f"ERRO: Localidade {LOCALIDADE_BACKUP_FAZENDA!r} não encontrada — confira o nome exato em produção.")
else:
    print(f"Localidade alvo: {loc_karitel.pk} — {loc_karitel.local}")

sem_cc = Item.objects.filter(fornecedor__nome__iexact=FORNECEDOR, centro_custo__isnull=True)
print(f"\nItens Routerlink sem Centro de Custo (qualquer origem): {sem_cc.count()}")

sem_loc_fazenda = Item.objects.filter(
    fornecedor__nome__iexact=FORNECEDOR,
    observacoes__icontains=MARCADOR_LOTE_FAZENDA,
    localidade__isnull=True,
)
print(f"Itens do lote Fazenda sem Localidade: {sem_loc_fazenda.count()}")

if APLICAR and cc_ti and loc_karitel:
    n_cc = sem_cc.update(centro_custo=cc_ti)
    n_loc = sem_loc_fazenda.update(localidade=loc_karitel)
    print(f"\nAPLICADO — Centro de Custo preenchido: {n_cc} | Localidade preenchida: {n_loc}")
else:
    print("\nDRY-RUN — nada alterado. Troque APLICAR=True e rode de novo depois de conferir.")
