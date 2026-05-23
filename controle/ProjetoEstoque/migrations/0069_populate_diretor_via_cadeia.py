"""
Migration de dados: popula os campos 'diretor' e corrige 'diretor_geral' de
todos os usuários existentes percorrendo a cadeia hierárquica da planilha.

Algoritmo:
  Para cada usuário com 'gestor' preenchido:
    1. Busca a linha do gestor no banco de dados (pelo valor abreviado do campo).
    2. O gestor do gestor = Diretor; o gestor do Diretor = Diretor Geral.
    3. O campo 'diretor_geral' já foi populado pela migration 0068;
       aqui apenas corrige casos onde a cadeia resolve um valor diferente.

Usa apenas o ORM do Django — não lê a planilha Excel novamente.
Os dados necessários (gestor, diretor, diretor_geral de cada nível) já
estão no próprio banco após os imports anteriores.
"""
import unicodedata
import re
from difflib import SequenceMatcher
from django.db import migrations


def _norm(v):
    if not v:
        return ""
    v = str(v).strip().lower()
    v = unicodedata.normalize("NFKD", v)
    v = "".join(c for c in v if not unicodedata.combining(c))
    v = re.sub(r"[^a-z0-9 ]", " ", v)
    return re.sub(r"\s+", " ", v).strip()


def _resolver_no_indice(nome_curto, indice):
    """
    Localiza no índice (norm_nome -> dict) o colaborador referenciado por um
    nome abreviado. Replica as 5 estratégias do UsuarioImportService.
    """
    if not nome_curto:
        return None
    seg = str(nome_curto).strip().split("/")[0].strip()
    alvo = _norm(seg)
    if not alvo:
        return None

    if alvo in indice:
        return indice[alvo]

    partes = alvo.split()

    if len(partes) >= 2:
        pri, ult = partes[0], partes[-1]
        cands = [
            (k, v) for k, v in indice.items()
            if k.split() and k.split()[0] == pri and k.split()[-1] == ult
        ]
        if len(cands) == 1:
            return cands[0][1]
        if len(cands) > 1:
            return max(cands, key=lambda x: SequenceMatcher(None, alvo, x[0]).ratio())[1]

    cands = [(k, v) for k, v in indice.items() if all(p in k.split() for p in partes)]
    if len(cands) == 1:
        return cands[0][1]
    if len(cands) > 1:
        return max(cands, key=lambda x: SequenceMatcher(None, alvo, x[0]).ratio())[1]

    def _palavras_batem(k_norm):
        kp = k_norm.split()
        return all(any(SequenceMatcher(None, p, w).ratio() >= 0.75 for w in kp) for p in partes)

    cands = [(k, v) for k, v in indice.items() if _palavras_batem(k)]
    if len(cands) == 1:
        return cands[0][1]
    if len(cands) > 1:
        return max(cands, key=lambda x: SequenceMatcher(None, alvo, x[0]).ratio())[1]

    best_s, best_v = 0, None
    for k, v in indice.items():
        s = SequenceMatcher(None, alvo, k).ratio()
        if s > best_s:
            best_s, best_v = s, v
    return best_v if best_s >= 0.72 else None


def _limpar(v):
    v = str(v or "").strip()
    return v if v and v not in ("-", "–", "—") else None


def popular_diretor(apps, schema_editor):
    Usuario = apps.get_model("ProjetoEstoque", "Usuario")

    # Construir índice norm(nome) → {gestor, diretor_geral}
    # usando os dados já presentes no banco (vindos dos imports anteriores).
    todos = list(
        Usuario.objects.values("id", "nome", "gestor", "diretor_geral")
    )
    indice = {}
    for u in todos:
        nome = str(u["nome"] or "").strip()
        if nome:
            indice[_norm(nome)] = {
                "gestor": _limpar(u["gestor"]),
                "id": u["id"],
            }

    DIRETOR_GERAL_PADRAO = "MIGUEL PRADO"
    atualizados = 0

    for u in todos:
        gestor_val = _limpar(u["gestor"])
        if not gestor_val:
            continue

        # Percorre a cadeia: gestor → gestor-do-gestor → ... → DG (auto-ref)
        chain = []
        current = gestor_val
        seen = set()

        while current:
            cn = _norm(current)
            if cn in seen:
                break
            seen.add(cn)

            row = _resolver_no_indice(current, indice)
            chain.append(current)

            if not row:
                break

            next_g = row.get("gestor")
            if next_g and _norm(next_g) == cn:
                break  # auto-referência = topo (DG)

            current = next_g

        diretor_geral_novo = chain[-1] if chain else DIRETOR_GERAL_PADRAO
        diretor_novo = chain[-2] if len(chain) >= 2 else None

        # Atualiza apenas se houver mudança
        obj = Usuario.objects.get(pk=u["id"])
        changed = False

        if obj.diretor != diretor_novo:
            obj.diretor = diretor_novo
            changed = True

        dg_atual = _limpar(obj.diretor_geral)
        if dg_atual != diretor_geral_novo:
            obj.diretor_geral = diretor_geral_novo
            changed = True

        if changed:
            obj.save(update_fields=["diretor", "diretor_geral"])
            atualizados += 1

    # Garante DG padrão para quem não tem gestor (e portanto não entrou no loop)
    sem_dg = Usuario.objects.filter(diretor_geral__isnull=True)
    if sem_dg.exists():
        sem_dg.update(diretor_geral=DIRETOR_GERAL_PADRAO)


def reverter(apps, schema_editor):
    Usuario = apps.get_model("ProjetoEstoque", "Usuario")
    Usuario.objects.update(diretor=None)


class Migration(migrations.Migration):

    dependencies = [
        ("ProjetoEstoque", "0068_populate_diretor_geral"),
    ]

    operations = [
        migrations.RunPython(popular_diretor, reverter),
    ]
