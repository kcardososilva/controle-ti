"""
Semeia o catálogo de Sistemas Corporativos com os seis sistemas que o
formulário do Planner já perguntava — é o que faz a primeira contratação
aberta no sistema novo nascer com as mesmas perguntas do quadro antigo, em
vez de uma tela vazia que ninguém sabe preencher.

ADITIVA e idempotente: só cria o que falta (casando pelo nome) e nunca
sobrescreve o que o TI tiver editado depois. A reversão apaga apenas os
nomes semeados que ninguém usou — sistema já citado numa contratação é
protegido pela FK e não deve sumir de um `migrate` para trás.
"""

from django.db import migrations

SISTEMAS = [
    ("TOTVS - Datasul", "ERP corporativo — módulo administrativo.", 10),
    ("TOTVS x PIMS MC", "Integração PIMS Multicultivo.", 20),
    ("TOTVS - Beneficiamento", "Módulo de beneficiamento.", 30),
    ("TOTVS - Pesagem", "Balança / pesagem de carga.", 40),
    ("Tekann", "Sistema Tekann.", 50),
    ("ID-Cotton", "Rastreabilidade de fardos de algodão.", 60),
]


def semear(apps, schema_editor):
    SistemaCorporativo = apps.get_model("ProjetoEstoque", "SistemaCorporativo")
    existentes = set(
        SistemaCorporativo.objects.values_list("nome", flat=True)
    )
    novos = [
        SistemaCorporativo(
            nome=nome, descricao=descricao, ordem=ordem,
            ativo=True, exige_perfil_espelho=True,
        )
        for nome, descricao, ordem in SISTEMAS
        if nome not in existentes
    ]
    SistemaCorporativo.objects.bulk_create(novos)


def remover(apps, schema_editor):
    SistemaCorporativo = apps.get_model("ProjetoEstoque", "SistemaCorporativo")
    nomes = [nome for nome, _, _ in SISTEMAS]
    (
        SistemaCorporativo.objects
        .filter(nome__in=nomes, pedidos__isnull=True)
        .delete()
    )


class Migration(migrations.Migration):

    dependencies = [
        ("ProjetoEstoque", "0179_contratacao_contratacaoevento_contratacaolicenca_and_more"),
    ]

    operations = [
        migrations.RunPython(semear, remover),
    ]
