"""
Endereços ANTIGOS do módulo Frotas e Conectividade (até 2026-11-13).

Este arquivo NÃO é montado no `ROOT_URLCONF` e não serve tela nenhuma. Ele é a
tabela de tradução que `views.ambiente.rota_legada_frota` consulta: o caminho
antigo é resolvido AQUI (`resolve(caminho, urlconf=este módulo)`), o que dá o
NOME da rota e os parâmetros; o mesmo nome é então revertido no urlconf
principal, onde hoje mora em `/agro/…`. Como os nomes não mudaram na migração,
a tradução é exata — sem regex por caminho e sem lista paralela de destinos.

O redirecionamento é 308 (permanente, mantém o método): um formulário aberto
numa aba antiga ainda faz o POST chegar ao endereço novo com o mesmo corpo e o
mesmo token CSRF. Favoritos, links em e-mail antigo e integrações seguem
funcionando sem acesso a registro nenhum se perder.

Para conferir que nada ficou de fora: `tests/test_ambientes.py`
(`RotasLegadasTests`) percorre TODAS as entradas daqui.

Não acrescentar rota NOVA aqui: rota nova já nasce em `/agro/`.
"""
from django.urls import path


def _nunca(request, *args, **kwargs):  # pragma: no cover — só existe para o `path()` ter um alvo
    raise AssertionError("urls_legado_frota é tabela de tradução, não urlconf servido.")


urlpatterns = [
    # ─── /m2m/ (Monitoramento TIM M2M) ───────────────────────────────────────
    path("m2m/", _nunca, name="m2m_visao_geral"),
    path("m2m/chips/", _nunca, name="m2m_chips"),
    path("m2m/chips/novo/", _nunca, name="m2m_chip_novo"),
    path("m2m/chips/<int:pk>/", _nunca, name="m2m_chip_detalhe"),
    path("m2m/chips/<int:pk>/editar/", _nunca, name="m2m_chip_editar"),
    path("m2m/chips/<int:pk>/monitoramento/", _nunca, name="m2m_chip_monitoramento"),
    path("m2m/chips/<int:pk>/atualizar/", _nunca, name="m2m_chip_atualizar"),
    path("m2m/consultas/<int:pk>/", _nunca, name="m2m_consulta_payload"),
    path("m2m/opcoes/", _nunca, name="m2m_opcoes"),
    path("m2m/consumo/", _nunca, name="m2m_consumo"),
    path("m2m/sessoes/", _nunca, name="m2m_sessoes"),
    path("m2m/alertas/", _nunca, name="m2m_alertas"),
    path("m2m/alertas/<int:pk>/tratar/", _nunca, name="m2m_alerta_tratar"),
    path("m2m/configuracao/", _nunca, name="m2m_config"),
    path("m2m/configuracao/token/", _nunca, name="m2m_config_token"),
    path("m2m/configuracao/frequencia/", _nunca, name="m2m_config_frequencia"),
    path("m2m/configuracao/mapeamento/", _nunca, name="m2m_config_mapeamento"),
    path("m2m/configuracao/previa/", _nunca, name="m2m_config_previa"),
    path("m2m/configuracao/testar/", _nunca, name="m2m_config_testar"),
    path("m2m/configuracao/reinterpretar/", _nunca, name="m2m_reinterpretar"),

    # ─── /frotas/ (Frotas e Conectividade) ───────────────────────────────────
    path("frotas/", _nunca, name="frota_visao_geral"),
    path("frotas/movimentacoes/", _nunca, name="frota_movimentacoes"),
    path("frotas/veiculos/", _nunca, name="frota_veiculos"),
    path("frotas/veiculos/novo/", _nunca, name="frota_veiculo_novo"),
    path("frotas/veiculos/<int:pk>/", _nunca, name="frota_veiculo_detalhe"),
    path("frotas/veiculos/<int:pk>/editar/", _nunca, name="frota_veiculo_editar"),
    path("frotas/veiculos/<int:pk>/status/", _nunca, name="frota_veiculo_status"),
    path("frotas/vincular/", _nunca, name="frota_vincular"),
    path("frotas/vinculos/<int:vinculo_id>/desvincular/", _nunca, name="frota_desvincular"),
    path("frotas/vinculos/<int:vinculo_id>/transferir/", _nunca, name="frota_transferir"),
    path("frotas/vinculos/<int:vinculo_id>/substituir/", _nunca, name="frota_substituir"),
    path("frotas/chips/<int:pk>/retornar/", _nunca, name="frota_retornar"),
    path("frotas/chips/<int:pk>/inativar/", _nunca, name="frota_inativar"),
    path("frotas/opcoes/", _nunca, name="frota_opcoes"),
    path("frotas/chips/importar/", _nunca, name="frota_importar"),
    path("frotas/chips/importar/modelo/", _nunca, name="frota_modelo_planilha"),
    path("frotas/chips/importar/<int:pk>/", _nunca, name="frota_importacao"),
    path("frotas/chips/importar/<int:pk>/confirmar/", _nunca, name="frota_importacao_confirmar"),
    path("frotas/chips/importar/<int:pk>/cancelar/", _nunca, name="frota_importacao_cancelar"),
    path("frotas/chips/importar/<int:pk>/relatorio/", _nunca, name="frota_importacao_relatorio"),
    path("frotas/chips/importacoes/", _nunca, name="frota_importacoes"),
    path("frotas/configuracao/tipos/", _nunca, name="frota_config_tipos"),
    path("frotas/configuracao/tipos/salvar/", _nunca, name="frota_config_tipo_salvar"),
    path("frotas/tipos/rapido/", _nunca, name="frota_tipo_rapido"),
    path("frotas/configuracao/operadoras/", _nunca, name="frota_config_operadoras"),
    path("frotas/configuracao/operadoras/salvar/", _nunca, name="frota_config_operadora_salvar"),
    path("frotas/configuracao/regras/", _nunca, name="frota_config_regras"),
    path("frotas/configuracao/regras/<str:tipo>/salvar/", _nunca, name="frota_config_regra_salvar"),
    path("frotas/configuracao/regras/<str:tipo>/restaurar/", _nunca, name="frota_config_regra_restaurar"),
]
