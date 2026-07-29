# Script de RECUPERAÇÃO DE EMERGÊNCIA para banco SQLite corrompido
# ("database disk image is malformed" em produção, 2026-07-24).
#
# COMO USAR (dentro da pasta controle, produção, com o servidor JÁ PARADO):
#   1) Ajuste as 2 constantes abaixo (CORROMPIDO e NOVO).
#   2) venv\Scripts\python.exe recuperar_producao_corrompida.py
#
# O QUE ELE FAZ (nunca escreve no arquivo corrompido, só lê dele):
#   Fase 1 — para CADA modelo do sistema, tenta extrair os dados do banco
#            CORROMPIDO via "manage.py dumpdata" (um arquivo .json por
#            modelo, em pasta separada). Se um modelo específico estiver
#            numa parte corrompida do arquivo, ele FALHA SÓ NAQUELE MODELO
#            e continua pros outros — ao contrário de um dumpdata único
#            gigante, que pararia no primeiro erro e perderia tudo.
#   Fase 2 — cria um banco NOVO, vazio, com o schema atual (migrate).
#   Fase 3 — para cada .json extraído com sucesso na Fase 1, importa
#            (loaddata) no banco NOVO.
#   Fase 4 — compara contagem de linhas por tabela entre o corrompido
#            (onde deu pra ler) e o novo, pra você saber exatamente o que
#            foi recuperado e o que não foi.
#
# Não mexe no banco de produção atual (nem no corrompido nem em nenhum
# outro) além de LER do corrompido. O banco novo/recuperado fica pronto
# para você revisar antes de decidir colocá-lo no lugar do atual.

import json
import os
import subprocess
import sys
from pathlib import Path

# Console do Windows costuma estar em cp1252, que não representa vários
# caracteres que podem aparecer em mensagens de erro (inclusive o caractere
# de substituição �, produzido pelo próprio errors="replace" usado
# abaixo ao capturar saída de subprocessos). Sem isso, um print() de uma
# mensagem de erro pode ele mesmo travar o script no meio da recuperação.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

CORROMPIDO = r"C:\Users\kayque.silva\OneDrive - SANTA COLOMBA AGROPECUARIA LTDA\Área de Trabalho\Projeto Estoque\Projeto\controle\db.sqlite3.CORROMPIDO_recebido_2026-07-24.bak"
NOVO = r"C:\Users\kayque.silva\OneDrive - SANTA COLOMBA AGROPECUARIA LTDA\Área de Trabalho\Projeto Estoque\Projeto\controle\db_recuperado_2026-07-24.sqlite3"

PASTA_DUMPS = Path("recuperacao_dumps_2026-07-24")
PASTA_DUMPS.mkdir(exist_ok=True)

# Apps/modelos que uma migração nova já recria sozinha com PKs próprios —
# NUNCA importar de volta (causaria conflito de ID com o que o migrate já criou).
EXCLUIR = {
    "contenttypes.contenttype",
    "auth.permission",
    "admin.logentry",
    "sessions.session",
}

PY = sys.executable
import re
_RE_MODELO = re.compile(r"^[A-Za-z0-9_]+\.[a-z0-9_]+$")


def rodar(env_extra, *args, capturar_saida=False):
    env = os.environ.copy()
    env.update(env_extra)
    return subprocess.run(
        [PY, "manage.py", *args], env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def listar_modelos():
    """Roda um comando auxiliar via manage.py shell pra listar app_label.model_name de todos os apps do projeto.
    O shell deste projeto imprime um banner tipo "64 objects imported automatically..." antes de
    qualquer coisa — por isso a lista é filtrada por um regex estrito (só linhas "algo.algo"),
    não só "contém um ponto"."""
    codigo = (
        "import django.apps as apps_mod\n"
        "for m in apps_mod.apps.get_models():\n"
        "    label = m._meta.app_label\n"
        "    if label in ('admin','contenttypes','sessions'):\n"
        "        continue\n"
        "    print(f'{label}.{m._meta.model_name}')\n"
    )
    r = subprocess.run(
        [PY, "manage.py", "shell", "-c", codigo],
        env={**os.environ, "DJANGO_DB_PATH": CORROMPIDO},
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if r.returncode != 0:
        print("ERRO ao listar modelos (o banco corrompido nem abre pro Django). Saída:")
        print(r.stderr[-3000:])
        sys.exit(1)
    modelos = [l.strip() for l in r.stdout.splitlines() if _RE_MODELO.match(l.strip())]
    return [m for m in modelos if m not in EXCLUIR]


print("=== FASE 1: extraindo dados do banco CORROMPIDO, modelo por modelo ===")
modelos = listar_modelos()
print(f"Total de modelos a tentar: {len(modelos)}")

sucesso, falha = [], []
for modelo in modelos:
    destino = PASTA_DUMPS / f"{modelo}.json"
    r = rodar(
        {"DJANGO_DB_PATH": CORROMPIDO},
        "dumpdata", modelo, "--natural-foreign", "--natural-primary", "--indent", "2",
        capturar_saida=True,
    )
    if r.returncode == 0 and r.stdout.strip():
        destino.write_text(r.stdout, encoding="utf-8")
        try:
            n = len(json.loads(r.stdout))
        except Exception:
            n = "?"
        sucesso.append((modelo, n))
        print(f"  OK   {modelo} ({n} registros)")
    else:
        falha.append((modelo, (r.stderr or "").strip().splitlines()[-1] if r.stderr else "sem dados/erro desconhecido"))
        print(f"  FALHOU {modelo}: {falha[-1][1][:200]}")

print(f"\nExtraídos com sucesso: {len(sucesso)} | Falharam: {len(falha)}")
if falha:
    print("Modelos que falharam na extração (dados dessas tabelas podem estar na parte corrompida):")
    for m, erro in falha:
        print(f"  - {m}: {erro[:200]}")

print("\n=== FASE 2: criando banco NOVO com schema limpo (migrate) ===")
r = rodar({"DJANGO_DB_PATH": NOVO}, "migrate")
print(r.stdout[-2000:])
if r.returncode != 0:
    print("ERRO no migrate do banco novo:")
    print(r.stderr[-3000:])
    sys.exit(1)

print("\n=== FASE 3: importando os .json extraídos no banco NOVO ===")
importados, falha_import = [], []
for modelo, n in sucesso:
    destino = PASTA_DUMPS / f"{modelo}.json"
    r = rodar({"DJANGO_DB_PATH": NOVO}, "loaddata", str(destino), capturar_saida=True)
    if r.returncode == 0:
        importados.append(modelo)
        print(f"  OK   {modelo}")
    else:
        falha_import.append((modelo, (r.stderr or "").strip().splitlines()[-1] if r.stderr else "erro desconhecido"))
        print(f"  FALHOU import {modelo}: {falha_import[-1][1][:200]}")

print(f"\nImportados: {len(importados)} | Falharam ao importar: {len(falha_import)}")

print("\n=== FASE 4: comparação de contagem por modelo ===")
codigo_contagem = (
    "import django.apps as apps_mod\n"
    "for m in apps_mod.apps.get_models():\n"
    "    label = m._meta.app_label\n"
    "    if label in ('admin','contenttypes','sessions'):\n"
    "        continue\n"
    "    try:\n"
    "        print(f'{label}.{m._meta.model_name}={m.objects.count()}')\n"
    "    except Exception as e:\n"
    "        print(f'{label}.{m._meta.model_name}=ERRO:{e}')\n"
)
r_novo = subprocess.run(
    [PY, "manage.py", "shell", "-c", codigo_contagem],
    env={**os.environ, "DJANGO_DB_PATH": NOVO},
    capture_output=True, text=True, encoding="utf-8", errors="replace",
)
print("Contagens no banco RECUPERADO:")
print(r_novo.stdout)

print("\n=== RESUMO FINAL ===")
print(f"Banco recuperado gerado em: {NOVO}")
print(f"JSONs de cada modelo (auditoria) em: {PASTA_DUMPS.resolve()}")
print("Revise as contagens acima e a lista de modelos que FALHARAM antes de decidir")
print("colocar este banco recuperado no lugar do db.sqlite3 atual. NÃO troquei nada")
print("automaticamente — isso é uma decisão que precisa de confirmação.")
