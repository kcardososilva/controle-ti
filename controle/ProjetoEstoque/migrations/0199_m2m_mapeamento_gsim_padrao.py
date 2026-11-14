"""
Conectividade — mapeamento da API do GSIM (Saitro) como padrão.

A API real (https://tim.saitro.com/api/docs/) foi lida e testada em 13/11/2026; o que a
tela de Configuração pedia para o usuário descobrir ("de que campo vem a sessão? e o
volume?") agora está documentado e foi conferido na resposta real da conta:

  • sessão  = `conexao`: `online` → sessão ativa; `offline` e `nunca_conectou` → sem sessão
              identificada; qualquer outro texto continua "não interpretado";
  • consumo = `servicos.dados.consumo`, texto com a unidade colada ("0,00 KB");
  • identidade = `iccid`.

Esta migration NÃO muda o esquema. Só grava esse mapeamento na linha única da integração
QUANDO ELE ESTÁ VAZIO: quem já configurou o seu (ou limpou de propósito e não tem o
costume de deixar vazio) não é tocado — vazio é o único estado que ela reconhece como
"ninguém mexeu". O texto está repetido aqui de propósito: migration é o retrato do dia e
não importa `services/m2m_interpretacao.py`. Reversa: não faz nada (apagar o mapeamento
de volta seria desfazer uma configuração que o usuário pode ter ajustado depois).
"""

from django.db import migrations

APP = "ProjetoEstoque"

MAPEAMENTO_GSIM = {
    "sessao": {
        "campo_estado": "conexao",
        "valores_ativa": ["online"],
        "valores_sem_sessao": ["offline", "nunca_conectou"],
        "campos_exibir": ["ultimo_acesso", "rede", "ultima_apn", "tecnologia", "cobertura", "status"],
    },
    "consumo": {
        "campo_volume": "servicos.dados.consumo",
        "unidade_no_valor": True,
        "base": 1000,
    },
    "identidade": {"campo_iccid": "iccid"},
}


def aplicar_mapeamento(apps, schema_editor):
    Integracao = apps.get_model(APP, "M2MIntegracao")
    cfg, _ = Integracao.objects.get_or_create(pk=1)
    if not cfg.mapeamento:
        cfg.mapeamento = MAPEAMENTO_GSIM
        cfg.save(update_fields=["mapeamento"])


class Migration(migrations.Migration):
    dependencies = [(APP, "0198_frota_equipamentos_dados")]

    operations = [migrations.RunPython(aplicar_mapeamento, migrations.RunPython.noop)]
