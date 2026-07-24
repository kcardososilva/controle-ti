# Fase 2.5 do runbook (DEPLOY_PENDENTE_ROUTERLINK.md).
#
# Roda com (dentro da pasta controle, produção):
#   venv\Scripts\python.exe manage.py shell -c "exec(open(r'deploy_scripts_routerlink\01_tempo_contrato.py', encoding='utf-8').read())"
#
# (usar exec(open(...).read()) em vez de "shell < arquivo.py" — o REPL do
# shell lendo de stdin redirecionado pode quebrar em blocos indentados com
# linha em branco no meio; exec roda o arquivo inteiro de uma vez, sem essa
# ambiguidade.)
#
# Preenche Locacao.tempo_locado = 36 meses SÓ onde está em branco — nunca
# sobrescreve um valor já preenchido (mesmo que pareça uma exceção/typo).
# Em dev, 403 das 404 locações Routerlink já preenchidas usavam exatamente
# 36 meses antes de rodar isto.
#
# COMO USAR (sempre em 2 passadas — não usa input() porque não há terminal
# interativo disponível neste modo de execução):
#   1) Rode com APLICAR = False (como está) — só imprime o diagnóstico.
#   2) Confira se o padrão de tempo_locado já usado em produção realmente é
#      36 meses (ver a contagem impressa) — se não for, ajuste TEMPO_MESES.
#   3) Edite este arquivo, troque para APLICAR = True, rode de novo.

from collections import Counter
from ProjetoEstoque.models import Locacao

FORNECEDOR = "Routerlink"
TEMPO_MESES = 36
APLICAR = False

print("=== Conferência: padrão de tempo_locado já usado ===")
ja_preenchidos = Locacao.objects.filter(
    equipamento__fornecedor__nome__iexact=FORNECEDOR, tempo_locado__isnull=False
)
print(Counter(ja_preenchidos.values_list("tempo_locado", flat=True)))

candidatos = Locacao.objects.filter(
    equipamento__fornecedor__nome__iexact=FORNECEDOR, tempo_locado__isnull=True
)
print(f"\nCandidatos (tempo_locado em branco): {candidatos.count()}")

if APLICAR:
    n = candidatos.update(tempo_locado=TEMPO_MESES)
    print(f"APLICADO — atualizados: {n}")
else:
    print(f"DRY-RUN — nada alterado. Reveja a contagem acima, ajuste TEMPO_MESES se preciso,\n"
          f"depois edite este arquivo (APLICAR = True) e rode de novo.")
