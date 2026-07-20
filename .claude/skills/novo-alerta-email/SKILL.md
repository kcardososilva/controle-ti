---
name: novo-alerta-email
description: Adiciona um novo tipo de alerta/notificação por e-mail ao sistema (função em email_alertas.py + entrada no catálogo + gatilho de disparo). Usar quando o pedido for "avisar por e-mail quando X acontecer", "criar alerta de Y".
---

# Novo alerta por e-mail

Toda a lógica de e-mail vive em `controle/services/email_alertas.py`. Nunca
disparar e-mail direto de uma view ou model — sempre por uma função nova nesse
arquivo, chamada via `transaction.on_commit()` (transacional) ou pelo
management command `enviar_alertas` (periódico/manual).

## Passo a passo

1. **Escrever a função** em `email_alertas.py`, padrão `alerta_<algo>(...)  -> bool`.
   Importar models **lazily dentro da função** (evita import circular — é o
   padrão de todo o arquivo). Montar `assunto`, `texto` (plain) e `html`, e
   chamar:
   ```python
   return _enviar(assunto, texto, html, destinatarios, codigo="<codigo_do_canal>")
   ```
   `_enviar()` já cuida de: checar a chave-mestra `ConfiguracaoSistema.alertas_email_ativos`,
   checar se o canal `codigo` está ativo em `CanalNotificacao`, substituir
   destinatários por lista customizada se configurado, e contabilizar
   `total_envios`/`ultimo_envio`. **Não duplicar essa lógica na função nova.**

2. **Adicionar entrada em `CATALOGO_NOTIFICACOES`** (topo do arquivo) — é a
   fonte da verdade do painel `/alertas/notificacoes/`. Sem essa entrada o
   canal não aparece lá e o TI não consegue ligar/desligar. Campos:
   `codigo` (slug único, é o mesmo passado em `_enviar(..., codigo=...)`),
   `nome`, `categoria`, `descricao`, `icone` (FontAwesome), `tipo_destinatarios`
   (`"fixo"` ou `"dinamico"`), `origem_disparo` (texto explicando quando dispara
   — usado só como documentação no painel).
   - Se `tipo_destinatarios="dinamico"`, também preencher
     `destino_gerenciado_em` explicando onde o destinatário é definido (ex.:
     "E-mail do colaborador vinculado à transferência").

3. **Wire do gatilho** — onde a função é chamada, depende do tipo de evento:
   - **Transacional** (algo que aconteceu numa request): no `service`
     correspondente (ex.: `movimentacao_service.py`), dentro de
     `transaction.on_commit(lambda: alerta_x(...))`. Nunca bloquear a request
     esperando o SMTP.
   - **Signal** (mudança de estado de um model, ex.: `Item.post_save`): em
     `signals.py`, seguindo o padrão de `alerta_item_defeito`.
   - **Agendado/periódico**: adicionar ao management command
     `enviar_alertas.py` com um novo `--tipo`, ou incluir no
     `relatorio_diario()` se fizer sentido como parte do digest diário.

4. **Testar**: rodar o gatilho manualmente (ou `python manage.py enviar_alertas --tipo <novo>`)
   e conferir em `/alertas/notificacoes/` que o canal aparece, está ativo, e
   que `total_envios` incrementa após o disparo.

## Checklist final
- [ ] Função importa models lazily (dentro da função)
- [ ] Usa `_enviar(..., codigo=...)` — não monta `EmailMultiAlternatives` na mão
- [ ] Entrada correspondente em `CATALOGO_NOTIFICACOES`
- [ ] Disparo é fire-and-forget (`transaction.on_commit`) se for transacional
- [ ] Canal aparece e liga/desliga corretamente em `/alertas/notificacoes/`
- [ ] Texto do e-mail em pt-BR
