# Script de RECUPERAÇÃO DE EMERGÊNCIA v2 — banco SQLite corrompido
# ("database disk image is malformed" em produção, 2026-07-24).
#
# Por que v2: a v1 usava "manage.py dumpdata", que passa pelo Django ORM —
# e o Django às vezes monta a consulta usando um ÍNDICE (Meta.ordering do
# model, ou o índice da PK) pra decidir a ordem de leitura. Como vários
# índices deste banco estão corrompidos (confirmado via PRAGMA
# integrity_check), o dumpdata "deu certo" (sem erro) mas leu um conjunto
# INCOMPLETO de linhas em pelo menos 1 tabela (Item: faltaram 2 de 1540,
# ids 790 e 1310 — presentes e legíveis no banco, só não vieram no dump).
# Isso derrubou em cascata mais 10 tabelas por causa de chave estrangeira.
#
# v2 copia TABELA POR TABELA via SQL puro, sem NENHUM ORDER BY — isso força
# uma varredura natural da própria tabela (rowid), sem tocar em nenhum
# índice secundário. Confirmado por teste: TODA tabela deste banco responde
# a um COUNT(*) bruto sem erro (a corrupção reportada pelo integrity_check
# não impede leitura direta da tabela, só invalida os índices).
#
# COMO USAR (dentro da pasta controle, produção, com o servidor JÁ PARADO):
#   1) Ajuste as 2 constantes abaixo (CORROMPIDO e NOVO).
#   2) venv\Scripts\python.exe recuperar_producao_corrompida_v2.py
#
# Nunca escreve no arquivo corrompido — abre ele read-only (ATTACH). O
# banco novo é criado do zero via "manage.py migrate" antes de qualquer
# cópia.

import subprocess
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

CORROMPIDO = r"C:\Users\kayque.silva\OneDrive - SANTA COLOMBA AGROPECUARIA LTDA\Área de Trabalho\Projeto Estoque\Projeto\controle\db.sqlite3.CORROMPIDO_recebido_2026-07-24.bak"
NOVO = r"C:\Users\kayque.silva\OneDrive - SANTA COLOMBA AGROPECUARIA LTDA\Área de Trabalho\Projeto Estoque\Projeto\controle\db_recuperado_v2_2026-07-24.sqlite3"

# Tabelas que uma migração nova já recria sozinha (permissões/content types/
# sessões/log do admin) — NUNCA copiar: os IDs de auth_permission/content
# type do banco velho não batem com os que o migrate novo já gerou; copiar
# por cima causaria FK errada ou duplicata.
EXCLUIR_TABELAS = {
    "django_content_type", "auth_permission", "django_admin_log",
    "django_session", "django_migrations",
    "auth_user_user_permissions", "auth_group_permissions",
}

PY = sys.executable

print("=== FASE 1: criando banco NOVO com schema limpo (migrate) ===")
r = subprocess.run([PY, "manage.py", "migrate"], env={**__import__("os").environ, "DJANGO_DB_PATH": NOVO},
                    capture_output=True, text=True, encoding="utf-8", errors="replace")
print(r.stdout[-1500:])
if r.returncode != 0:
    print("ERRO no migrate:")
    print(r.stderr[-3000:])
    sys.exit(1)

print("\n=== FASE 2: copiando tabela por tabela (SQL puro, sem ORDER BY) ===")
con = sqlite3.connect(NOVO)
con.execute("ATTACH DATABASE ? AS old_db", (CORROMPIDO,))
con.execute("PRAGMA foreign_keys = OFF")

tabelas = [
    r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
]
tabelas = [t for t in tabelas if t not in EXCLUIR_TABELAS]

sucesso, falha = [], []
for tabela in tabelas:
    try:
        existe_na_antiga = con.execute(
            "SELECT COUNT(*) FROM old_db.sqlite_master WHERE type='table' AND name=?", (tabela,)
        ).fetchone()[0]
        if not existe_na_antiga:
            print(f"  PULADO {tabela}: não existe no banco corrompido (tabela nova, ok)")
            continue

        cols_novo = [c[1] for c in con.execute(f'PRAGMA table_info("{tabela}")').fetchall()]
        cols_antigo = [c[1] for c in con.execute(f'PRAGMA old_db.table_info("{tabela}")').fetchall()]
        cols_comuns = [c for c in cols_novo if c in cols_antigo]
        col_list = ", ".join(f'"{c}"' for c in cols_comuns)

        con.execute(f'DELETE FROM "{tabela}"')  # tabela nova vem só com seed (categorias etc.) de migration de dados
        con.execute(f'INSERT INTO "{tabela}" ({col_list}) SELECT {col_list} FROM old_db."{tabela}"')
        n = con.execute(f'SELECT COUNT(*) FROM "{tabela}"').fetchone()[0]
        n_origem = con.execute(f'SELECT COUNT(*) FROM old_db."{tabela}"').fetchone()[0]
        con.commit()
        status = "OK" if n == n_origem else "PARCIAL"
        sucesso.append((tabela, n, n_origem))
        print(f"  {status} {tabela}: {n}/{n_origem} linhas")
    except Exception as e:
        con.rollback()
        falha.append((tabela, str(e)))
        print(f"  FALHOU {tabela}: {e}")

con.execute("PRAGMA foreign_keys = ON")

print(f"\nTabelas copiadas: {len(sucesso)} | Falharam: {len(falha)}")
if falha:
    print("Tabelas que falharam:")
    for t, erro in falha:
        print(f"  - {t}: {erro[:300]}")

print("\n=== FASE 3: integrity_check do banco recuperado ===")
r = con.execute("PRAGMA integrity_check;").fetchall()
for row in r[:20]:
    print(" ", row[0])
print("Total de problemas:", len(r), "(esperado: ['ok'] = 1 problema só, a mensagem 'ok')")

print("\n=== FASE 4: checagem de foreign keys (após religar) ===")
problemas_fk = con.execute("PRAGMA foreign_key_check;").fetchall()
print(f"Violações de FK encontradas: {len(problemas_fk)}")
for row in problemas_fk[:20]:
    print(" ", row)

con.close()

print("\n=== RESUMO FINAL ===")
print(f"Banco recuperado em: {NOVO}")
parciais = [t for t, n, n2 in sucesso if n != n2]
if parciais:
    print(f"ATENÇÃO — tabelas com contagem PARCIAL (não bateu 100% com a origem): {parciais}")
else:
    print("Todas as tabelas bateram 100% com a contagem da origem.")
