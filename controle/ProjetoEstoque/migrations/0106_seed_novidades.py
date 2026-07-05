from datetime import date

from django.db import migrations


# Entradas iniciais do changelog — refletem as mudanças reais recém-entregues.
_SEED = [
    {
        "tipo": "novo",
        "versao": "4.1.0",
        "data": date(2026, 7, 5),
        "titulo": "Monitoramento de segurança de acessos",
        "descricao": (
            "Registro de logins, falhas e logout com IP e agente, além de "
            "detecção de acessos suspeitos (rajada de falhas de login). Dispara "
            "alerta por e-mail e mantém uma trilha de eventos para auditoria."
        ),
    },
    {
        "tipo": "melhoria",
        "versao": "4.1.0",
        "data": date(2026, 7, 5),
        "titulo": "Tela de perfil reformulada",
        "descricao": (
            "Perfil reorganizado com Segurança da conta (acessos recentes e "
            "postura de segurança), Minha atividade e Novidades do sistema."
        ),
    },
    {
        "tipo": "correcao",
        "versao": "4.1.0",
        "data": date(2026, 7, 5),
        "titulo": "Fornecedores removidos da lista de técnicos",
        "descricao": (
            "As listas de técnico em Preventivas (Minhas Atividades, Plano de "
            "Agendamento e Desempenho) não exibem mais usuários do Portal do "
            "Fornecedor."
        ),
    },
]


def seed(apps, schema_editor):
    Novidade = apps.get_model("ProjetoEstoque", "NovidadeSistema")
    if Novidade.objects.exists():
        return  # não duplica se já houver conteúdo
    for row in _SEED:
        Novidade.objects.create(**row)


def unseed(apps, schema_editor):
    Novidade = apps.get_model("ProjetoEstoque", "NovidadeSistema")
    Novidade.objects.filter(titulo__in=[r["titulo"] for r in _SEED]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("ProjetoEstoque", "0105_novidadesistema"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
