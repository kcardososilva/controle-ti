# Fase 2.12 do runbook (DEPLOY_PENDENTE_ROUTERLINK.md).
#
# Roda com (dentro da pasta controle, produção):
#   venv\Scripts\python.exe manage.py shell -c "exec(open(r'deploy_scripts_routerlink\09_preventiva_switch_ap_meraki.py', encoding='utf-8').read())"
#
# Padrão identificado (dev, 2026-07-24): dos itens Routerlink com
# precisa_preventiva='sim' nos subtipos Switches / Access-Point / Meraki,
# só uma fração já tinha uma Preventiva agendada (34/86 switches, 17/204
# access-point, 1/8 meraki) — os demais nunca tiveram nenhuma. O checklist
# usado em 100% dos casos existentes já é o checklist do próprio Subtipo
# (Check List Switches / Check List Access-Point / Check List Meraki, todos
# com intervalo_dias=0 no cadastro do checklist — o intervalo real vem de
# Item.data_limite_preventiva, JÁ preenchido por item, não mexemos nisso).
#
# Este script reusa a MESMA lógica da view oficial de sincronização em massa
# (preventiva_sincronizar_programacao, ProjetoEstoque/views/preventivas.py)
# — checklist por Subtipo, get_or_create, data_proxima=hoje na criação,
# pausa automática se o item já está em status não-operacional — só que
# escopado a Routerlink + estes 3 subtipos (a view oficial roda para TODOS
# os fornecedores; achamos melhor não tocar outros fornecedores agora, já
# que não foi pedido).
#
# EXCLUSÃO CONHECIDA: Item ID 945 "FW-FGT-RDM" (modelo FG-200E, marca
# FORTINET) está com subtipo=Meraki no cadastro, o que é um erro de
# classificação pré-existente (é um FortiGate, não um equipamento Meraki) —
# ver AVISO impresso abaixo. Excluído automaticamente via filtro de marca
# (qualquer item Meraki com marca contendo "FORTINET"), não incluído no
# lote de preventivas. NÃO corrigido aqui — o Subtipo correto já existe no
# sistema ("Fortinet", 1 item hoje) mas a correção não foi pedida.
#
# COMO USAR: 1) rode com APLICAR=False, confira a prévia; 2) troque para
# APLICAR=True e rode de novo.

from django.db import transaction
from django.utils import timezone

from ProjetoEstoque.models import CheckListModelo, Item, Preventiva, SimNaoChoices, StatusItemChoices
from ProjetoEstoque.views.preventivas import _intervalo_preventiva

FORNECEDOR = "Routerlink"
SUBTIPOS = ["Switches", "Access-Point", "Meraki"]
APLICAR = False

_STATUS_PAUSANTES = {
    StatusItemChoices.PAUSADO, StatusItemChoices.BACKUP, StatusItemChoices.ESTOQUE,
    StatusItemChoices.MANUTENCAO, StatusItemChoices.DEFEITO, StatusItemChoices.DESCARTE,
    StatusItemChoices.DEVOLVIDO,
}

hoje = timezone.localdate()

suspeitos = Item.objects.filter(
    fornecedor__nome__iexact=FORNECEDOR, subtipo__nome__iexact="Meraki", marca__icontains="FORTINET",
)
print("=== AVISO: itens Meraki com marca Fortinet (excluídos, subtipo provavelmente errado) ===")
for it in suspeitos:
    print(f"  ID {it.pk} | {it.nome} | modelo={it.modelo} | marca={it.marca}")
if not suspeitos:
    print("  nenhum encontrado.")

itens = (
    Item.objects
    .filter(
        fornecedor__nome__iexact=FORNECEDOR,
        subtipo__nome__in=SUBTIPOS,
        precisa_preventiva=SimNaoChoices.SIM,
    )
    .exclude(pk__in=suspeitos.values("pk"))
    .select_related("subtipo")
)
print(f"\nItens-alvo (precisa_preventiva=sim, subtipo em {SUBTIPOS}): {itens.count()}")

checklists = {c.subtipo_id: c for c in CheckListModelo.objects.filter(subtipo__nome__in=SUBTIPOS, ativo=SimNaoChoices.SIM)}
for st in SUBTIPOS:
    achou = any(c.subtipo.nome == st for c in checklists.values())
    print(f"  Checklist ativo para {st!r}: {'OK' if achou else 'FALTANDO — item deste subtipo será pulado'}")

existentes_ids = set(Preventiva.objects.filter(equipamento__in=itens).values_list("equipamento_id", "checklist_modelo_id"))

criar, resync, sem_checklist, sem_intervalo, pausar, retomar = [], [], 0, 0, [], []
for item in itens:
    checklist = checklists.get(item.subtipo_id)
    if checklist is None:
        sem_checklist += 1
        continue
    intervalo, origem = _intervalo_preventiva(item, checklist)
    if intervalo <= 0:
        sem_intervalo += 1
        print(f"  SEM intervalo configurado: ID {item.pk} | {item.nome} (nem item.data_limite_preventiva nem checklist.intervalo_dias)")
        continue
    if (item.pk, checklist.pk) in existentes_ids:
        prev = Preventiva.objects.get(equipamento=item, checklist_modelo=checklist)
        antes = prev.data_proxima
        prev.sincronizar_data_proxima(hoje, salvar=False)
        if prev.data_proxima != antes:
            resync.append(prev)
        item_pausante = item.status in _STATUS_PAUSANTES
        if item_pausante and not prev.pausada:
            pausar.append(prev)
        elif not item_pausante and prev.pausada:
            retomar.append(prev)
        continue
    criar.append((item, checklist))

print(f"\nPreventivas a CRIAR: {len(criar)}")
resumo_por_subtipo = {}
for item, checklist in criar:
    resumo_por_subtipo[item.subtipo.nome] = resumo_por_subtipo.get(item.subtipo.nome, 0) + 1
for nome, n in resumo_por_subtipo.items():
    print(f"  {nome}: {n}")
print(f"Preventivas já existentes a ressincronizar (data_proxima defasada): {len(resync)}")
print(f"Sem checklist ativo p/ subtipo (pulados): {sem_checklist}")
print(f"Sem intervalo configurado (pulados, listados acima): {sem_intervalo}")

if APLICAR:
    with transaction.atomic():
        criadas = 0
        for item, checklist in criar:
            prev = Preventiva.objects.create(
                equipamento=item, checklist_modelo=checklist,
                data_ultima=None, data_proxima=hoje, dentro_do_prazo=True,
            )
            criadas += 1
            if item.status in _STATUS_PAUSANTES:
                prev.pausar()
        for prev in resync:
            prev.save(update_fields=["data_proxima", "dentro_do_prazo", "updated_at"])
        for prev in pausar:
            prev.pausar()
        for prev in retomar:
            prev.retomar()
    print(f"\nAPLICADO — {criadas} preventivas criadas, {len(resync)} ressincronizadas, "
          f"{len(pausar)} pausadas, {len(retomar)} retomadas.")
else:
    print("\nDRY-RUN — nada alterado. Troque APLICAR=True e rode de novo depois de conferir.")
