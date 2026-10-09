# -*- coding: utf-8 -*-
"""
Remove CPF e RG do signatário da tabela de termos assinados.

Esses documentos passam a existir APENAS dentro do PDF emitido: eles entram no
texto do termo no momento da assinatura e são descartados em seguida (ver
`services/termo_assinatura_service.registrar_assinatura`). A via com os
documentos fica com o colaborador e no `MovimentacaoItem.termo_pdf`, que é
arquivo protegido por `media_acesso_service` — não uma coluna consultável.

Por que isto é exceção à regra 7 (migrations backwards-safe, sem derrubar
coluna sem período de transição): o objetivo da migration É eliminar o dado.
Um "período de transição" aqui significaria manter CPF de colaboradores no
banco por mais tempo, que é exatamente o que se quer desfazer. O deploy é
manual e aplica código e migration juntos (ver skill `migracao-deploy`), então
não há janela de código antigo contra schema novo.

A limpeza roda ANTES do drop de propósito. No SQLite o `RemoveField` recria a
tabela e o dado sairia junto de qualquer forma, mas o `UPDATE` explícito:
  • deixa a intenção de expurgo registrada na própria migration, em vez de
    depender de um efeito colateral do backend;
  • sobrescreve o valor nas páginas atuais antes da recriação da tabela;
  • funciona igual se o projeto um dia mudar de banco.

Irreversível por natureza: `reverse_code` recria as colunas vazias (o schema
volta), mas o conteúdo não — e não deve mesmo voltar.
"""

from django.db import migrations


def expurgar_documentos(apps, schema_editor):
    TermoAssinatura = apps.get_model("ProjetoEstoque", "TermoAssinatura")

    afetados = (
        TermoAssinatura.objects
        .exclude(signatario_cpf="", signatario_rg="")
        .update(signatario_cpf="", signatario_rg="")
    )
    if afetados:
        print(f"  Documentos (CPF/RG) apagados de {afetados} termo(s) assinado(s).")


def nada_a_desfazer(apps, schema_editor):
    """O schema volta pelo `RemoveField` reverso; o conteúdo não volta — é o
    ponto da migration."""
    return None


class Migration(migrations.Migration):

    dependencies = [
        ('ProjetoEstoque', '0169_termo_assinatura_digital'),
    ]

    operations = [
        migrations.RunPython(expurgar_documentos, nada_a_desfazer),
        migrations.RemoveField(
            model_name='termoassinatura',
            name='signatario_cpf',
        ),
        migrations.RemoveField(
            model_name='termoassinatura',
            name='signatario_rg',
        ),
    ]
