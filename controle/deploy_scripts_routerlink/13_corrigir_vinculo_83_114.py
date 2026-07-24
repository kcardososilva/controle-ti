# Fase 2.16 do runbook (DEPLOY_PENDENTE_ROUTERLINK.md).
#
# Roda com (dentro da pasta controle, produção):
#   venv\Scripts\python.exe manage.py shell -c "exec(open(r'deploy_scripts_routerlink\13_corrigir_vinculo_83_114.py', encoding='utf-8').read())"
#
# Corrige os 2 itens do achado "vínculo incompleto" (documentado desde a
# Fase 2.6) — descobertos como "não cadastrados" numa nova comparação do
# usuário contra a planilha "1V RELAÇÃO DE EQUIPAMENTOS CORRETA -
# FORNECEDOR.xlsx", mas na verdade JÁ EXISTEM no banco (ids 83 e 114 em dev)
# — só que com `fornecedor=NULO`, o que os fazia sumir de qualquer
# comparação filtrada por "Routerlink". Ambos criados em 27/10/2025, antes
# desta sessão — bug de importação antigo, não relacionado a este trabalho.
#
# IMPORTANTE: os IDs 83/114 são específicos do banco de DEV. Em produção,
# repita a mesma investigação feita nesta sessão (cruzar a planilha do
# fornecedor contra Item.numero_serie de TODOS os fornecedores, não só
# Routerlink) pra achar os IDs corretos — ver a query pronta comentada no
# final deste arquivo. NÃO rode este script tal como está sem confirmar os
# IDs certos primeiro.
#
# Item 114 (serial FTX1640GP1V): nome/modelo tinham um typo pré-existente
# ("AIR-CAP36021-A-K9", dígito 1 em vez de letra I) — confirmado contra 35
# outros itens Routerlink com o modelo certo "AIR-CAP3602I-A-K9". Já corre-
# tamente locado=sim, subtipo=Access-Point, marca=Cisco — só fornecedor e
# nome/modelo estavam errados, e Locacao.valor_mensal estava zerado (planilha
# diz R$25).
#
# Item 83 (serial N4IG3901602IA): nome "POE 200 AT" — confirmado contra 4
# outros itens Routerlink com modelo "PWR-INJ-8023AT" (mesmo subtipo Fonte,
# mas marca Cisco, não Intelbras como estava aqui). locado='não' e sem
# nenhuma Locacao — precisa virar locado='sim' com Locacao nova (R$14/mês,
# planilha).
#
# Depois de rodar este script, ainda é necessário (nessa ordem, pra herdar
# data_entrada/contrato automaticamente pelos scripts já existentes):
#   1) 07_valor_zero_e_data_entrada.py  (agora os 2 itens contam como
#      Routerlink — pega a data_entrada em aberto de ambos)
#   2) 08_contrato.py                    (preenche o contrato dos dois)
#   3) manage.py corrigir_periodos_locacao --aplicar   (abre o LocacaoPeriodo
#      que falta pro item 83, que nunca teve nenhum)
#
# COMO USAR: 1) rode com APLICAR=False, confira a prévia; 2) troque para
# APLICAR=True e rode de novo.

from decimal import Decimal

from ProjetoEstoque.models import Fornecedor, Item, Locacao

APLICAR = False

fornecedor = Fornecedor.objects.filter(nome__iexact="Routerlink").first()
if not fornecedor:
    print("ERRO: Fornecedor 'Routerlink' não encontrado.")

item_114 = Item.objects.filter(pk=114).first()
item_83 = Item.objects.filter(pk=83).first()

print("=== Prévia ===")
if item_114:
    print(f"ID 114 | serial={item_114.numero_serie} | nome atual={item_114.nome!r} | fornecedor atual={item_114.fornecedor}")
    print(f"       -> vai virar: nome/modelo='AIR-CAP3602I-A-K9', fornecedor=Routerlink, numero_nf='18058', "
          f"Locacao.valor_mensal=25.00")
else:
    print("ID 114 não encontrado neste banco — confira o ID correto antes de aplicar.")

if item_83:
    print(f"ID 83  | serial={item_83.numero_serie} | nome atual={item_83.nome!r} | fornecedor atual={item_83.fornecedor} | locado atual={item_83.locado}")
    print(f"       -> vai virar: nome/modelo='PWR-INJ-8023AT', marca='CISCO', fornecedor=Routerlink, "
          f"locado='sim', valor=0, numero_nf='17388', nova Locacao com valor_mensal=14.00")
else:
    print("ID 83 não encontrado neste banco — confira o ID correto antes de aplicar.")

if APLICAR and fornecedor and item_114 and item_83:
    item_114.fornecedor = fornecedor
    item_114.nome = "AIR-CAP3602I-A-K9"
    item_114.modelo = "AIR-CAP3602I-A-K9"
    item_114.numero_nf = "18058"
    item_114.save()
    loc_114 = item_114.locacao
    loc_114.fornecedor = fornecedor
    loc_114.valor_mensal = Decimal("25.00")
    loc_114.save()

    item_83.fornecedor = fornecedor
    item_83.nome = "PWR-INJ-8023AT"
    item_83.modelo = "PWR-INJ-8023AT"
    item_83.marca = "CISCO"
    item_83.locado = "sim"
    item_83.valor = Decimal("0")
    item_83.numero_nf = "17388"
    item_83.save()
    Locacao.objects.create(equipamento=item_83, fornecedor=fornecedor, valor_mensal=Decimal("14.00"))

    print("\nAPLICADO — itens 114 e 83 corrigidos. Rode em seguida: 07 -> 08 -> "
          "'manage.py corrigir_periodos_locacao --aplicar'.")
else:
    print("\nDRY-RUN — nada alterado. Troque APLICAR=True e rode de novo depois de conferir.")

# Query pronta pra achar os IDs certos em produção (cruzando TODOS os
# fornecedores, não só Routerlink, porque é exatamente esse vínculo que
# está quebrado):
#
# from ProjetoEstoque.models import Item
# for serie in ["FTX1640GP1V", "N4IG3901602IA"]:
#     for it in Item.all_objects.filter(numero_serie__iexact=serie):
#         print(it.pk, it.nome, it.modelo, it.fornecedor, it.locado, it.status)
