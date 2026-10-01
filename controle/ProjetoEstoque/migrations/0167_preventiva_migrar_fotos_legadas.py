"""
Copia as evidências fotográficas dos 4 campos fixos para a galeria
(`PreventivaFoto`), preservando TODO o histórico já registrado.

Princípios desta migração (nenhum é opcional):

1. **Nenhum arquivo é tocado.** Só o CAMINHO guardado no banco é copiado para a
   nova linha (atribuir uma string a um FileField define `.name` sem abrir,
   mover ou reprocessar nada). Os arquivos em MEDIA_ROOT ficam exatamente onde
   estão — inclusive os que hoje só existem no servidor de produção.

2. **As colunas antigas continuam no banco.** Esta migração não remove nada:
   se algo der errado, o dado original segue disponível para recuperar. A
   remoção fica para uma migration futura, depois de validado em produção.

3. **Idempotente.** Reaplicar não duplica: cada linha criada carrega
   `origem_legado`, e a checagem é feita pelo par (dono, campo de origem).

4. **As fotos do snapshot de `Preventiva` também vêm.** Elas são anteriores ao
   próprio model de execuções e, em geral, NÃO têm execução correspondente —
   migrar apenas as execuções perderia essas evidências (no banco auditado, 123
   fotos estavam exclusivamente no snapshot). Entram com `execucao = NULL`,
   exceto quando o mesmo caminho já veio de uma execução daquela preventiva —
   aí seriam a mesma foto duas vezes na galeria.
"""
from django.db import migrations


# Campo legado → momento na galeria. A ordem define a sequência na galeria:
# antes, antes_2, depois, depois_2 — a mesma leitura cronológica que o técnico
# usava ao preencher os 4 campos.
CAMPOS = (
    ("foto_antes",    "antes",  0),
    ("foto_antes_2",  "antes",  1),
    ("foto_depois",   "depois", 2),
    ("foto_depois_2", "depois", 3),
)


def migrar(apps, schema_editor):
    Preventiva = apps.get_model("ProjetoEstoque", "Preventiva")
    PreventivaExecucao = apps.get_model("ProjetoEstoque", "PreventivaExecucao")
    PreventivaFoto = apps.get_model("ProjetoEstoque", "PreventivaFoto")

    novas = []

    # ── 1) Execuções (histórico linha a linha) ───────────────────────────────
    ja_migradas = set(
        PreventivaFoto.objects
        .exclude(origem_legado="")
        .filter(execucao__isnull=False)
        .values_list("execucao_id", "origem_legado")
    )
    execucoes = PreventivaExecucao.objects.all().only(
        "id", "preventiva_id", "foto_antes", "foto_depois", "foto_antes_2",
        "foto_depois_2", "criado_por_id",
    )
    # Caminhos já cobertos por execução, por preventiva — usado no passo 2 para
    # não recriar a mesma imagem que já entrou na galeria pela execução.
    cobertos_por_preventiva = {}

    for execucao in execucoes.iterator():
        for campo, momento, ordem in CAMPOS:
            caminho = getattr(execucao, campo, None)
            # FileField vazio é falsy tanto como "" quanto como None.
            nome = getattr(caminho, "name", caminho) or ""
            if not nome:
                continue
            cobertos_por_preventiva.setdefault(execucao.preventiva_id, set()).add(nome)
            origem = f"execucao.{campo}"
            if (execucao.id, origem) in ja_migradas:
                continue
            novas.append(PreventivaFoto(
                preventiva_id=execucao.preventiva_id,
                execucao_id=execucao.id,
                imagem=nome,
                momento=momento,
                ordem=ordem,
                origem_legado=origem,
                criado_por_id=execucao.criado_por_id,
            ))

    # ── 2) Snapshot de `Preventiva` (evidências anteriores às execuções) ─────
    ja_migradas_prev = set(
        PreventivaFoto.objects
        .exclude(origem_legado="")
        .filter(execucao__isnull=True)
        .values_list("preventiva_id", "origem_legado")
    )
    preventivas = Preventiva.objects.all().only(
        "id", "foto_antes", "foto_depois", "foto_antes_2", "foto_depois_2",
        "criado_por_id",
    )
    for preventiva in preventivas.iterator():
        cobertos = cobertos_por_preventiva.get(preventiva.id, set())
        for campo, momento, ordem in CAMPOS:
            caminho = getattr(preventiva, campo, None)
            nome = getattr(caminho, "name", caminho) or ""
            if not nome or nome in cobertos:
                continue
            origem = f"preventiva.{campo}"
            if (preventiva.id, origem) in ja_migradas_prev:
                continue
            novas.append(PreventivaFoto(
                preventiva_id=preventiva.id,
                execucao_id=None,
                imagem=nome,
                momento=momento,
                ordem=ordem,
                origem_legado=origem,
                criado_por_id=preventiva.criado_por_id,
            ))

    if novas:
        PreventivaFoto.objects.bulk_create(novas, batch_size=500)


def desfazer(apps, schema_editor):
    """Remove SOMENTE as linhas criadas por esta migração (origem_legado
    preenchido). Fotos enviadas pela galeria nova não têm origem_legado e são
    preservadas — um rollback nunca deve apagar evidência nova."""
    PreventivaFoto = apps.get_model("ProjetoEstoque", "PreventivaFoto")
    PreventivaFoto.objects.exclude(origem_legado="").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("ProjetoEstoque", "0166_preventiva_galeria_fotos"),
    ]

    operations = [
        migrations.RunPython(migrar, desfazer),
    ]
