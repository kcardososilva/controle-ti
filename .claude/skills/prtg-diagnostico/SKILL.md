---
name: prtg-diagnostico
description: Runbook de diagnóstico para problemas de integração PRTG — status incorreto no mapa/canvas, alarme por e-mail que parou de disparar, cache desatualizado. Usar quando o pedido envolver "PRTG não está atualizando", "alerta de equipamento offline não chegou", "status errado na planta".
---

# Diagnóstico PRTG

Toda a integração passa por `controle/services/prtg_service.py`
(`get_devices_map()`). Credenciais (`PRTG_URL`/`PRTG_USER`/`PRTG_PASSHASH`)
ficam só no `.env` do servidor — nunca chegam ao browser. O frontend só vê
`/plantas/prtg/status/` (`@login_required`).

## 1. Status errado (device parece Up mas está caído, ou vice-versa)

O PRTG separa status de **device** (agregado de todos os sensores) do status
do **sensor de ping**. Se o ping não é o sensor-raiz de dependência do
device, o device pode aparecer "Up" com o ping "Down". `get_devices_map()` já
resolve isso pegando o **pior** dos dois (`status` = efetivo,
`device_status`/`ping_status` = brutos, separados). Se algo no template está
usando `device_status` cru em vez de `status`, é esse o bug.

Checar também `_status_int()` — parsing tem prioridade `status_raw` (número) →
`int(float(status))` → mapeamento de texto (`"Down (Ping test failed)"` →
split em `"("` → `"down"` → `5`). Textos compostos não mapeados caem em
`unknown`.

## 2. Cache desatualizado / mudança de schema não aparece

Devices e sensores de ping são cacheados **separadamente** por 30s, chaves
`_CACHE_KEY_DEVICES` e `_CACHE_KEY_PING` em `prtg_service.py` (hoje
`prtg_devices_v2` / `prtg_ping_sensors_v3`). Se você mudar o formato do dict
retornado, **sempre incrementar o sufixo de versão** da chave — senão entradas
antigas em cache (30s de TTL, mas pode persistir mais se o processo não
reiniciar) quebram o consumidor silenciosamente.

## 3. Alarme por e-mail parou de chegar

Checar nesta ordem exata (é a cadeia real de dependências, todas precisam
estar OK):

1. Tarefa `ControleEstoque_MonitorarPRTG` registrada e rodando —
   `python manage.py agendar_prtg listar`. **Diferente do relatório diário,
   essa tarefa não se auto-registra**; sem ela `ItemPRTGHistorico` nunca
   recebe evento novo e nenhuma transição é detectada.
2. `ConfiguracaoSistema.alertas_email_ativos = True` em `/alertas/notificacoes/`
   — chave-mestra, suprime tudo silenciosamente (só loga `INFO`) se desligada.
3. Canal `prtg_transicoes` ativo no mesmo painel — `total_envios` mostra se já
   disparou alguma vez.
4. A transição precisa ser **real** (`status_anterior` preenchido). A
   **primeira observação** de cada device após reset de histórico nunca gera
   alarme mesmo que já esteja offline — é proposital (evita avalanche), mas
   confunde quem espera alarme imediato depois de reconstruir o histórico.

## 4. Testar rápido

```bash
cd controle
python manage.py monitorar_prtg   # roda a coleta uma vez, sem esperar o agendamento
```
Depois checar `ItemPRTGHistorico` no admin/shell para ver se o evento entrou.

## Checklist final
- [ ] Usa `status` efetivo, não `device_status` cru
- [ ] Se mudou schema do dict, versão da chave de cache incrementada
- [ ] Tarefa `agendar_prtg` rodando (não só a de relatório diário)
- [ ] Chave-mestra + canal `prtg_transicoes` ativos
- [ ] Não confundir "sem alarme na primeira observação" com bug
