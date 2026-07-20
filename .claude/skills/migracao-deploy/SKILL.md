---
name: migracao-deploy
description: Checklist para criar migrations Django com segurança e lembrar do que precisa ser replicado manualmente em produção (DB separado, agendamentos do Task Scheduler). Usar sempre que uma mudança tocar models.py ou settings de produção.
---

# Migration + Deploy

Este projeto **não tem CI/CD**: produção roda de uma pasta/máquina separada
(`C:\Projeto Djngo v2\...`) com o **próprio `db.sqlite3`**. Uma migration
aplicada em dev não chega em produção sozinha.

## Criando a migration

1. `cd controle && python manage.py makemigrations`
2. **Backwards-safe obrigatório**: não remover coluna/tabela numa única
   migration se algum código em produção ainda depender dela — usar um
   período de transição (deprecar, depois remover numa migration seguinte).
   Projeto está na migration `0103+`; nunca editar uma migration já aplicada
   em produção, sempre criar uma nova.
3. `python manage.py migrate` para aplicar em dev.
4. Se a mudança envolve caminho de arquivo/DB, lembrar que o `db.sqlite3` do
   repo pode estar dentro do OneDrive — se aparecer erro
   `"attempt to write a readonly database"`, não é bug de código: é o
   OneDrive travando o arquivo (`DJANGO_DB_PATH` deveria apontar pra fora do
   OneDrive; ver memória `sqlite_onedrive_readonly`).

## Levando para produção

Depois de validar em dev, **avisar explicitamente o usuário** do que precisa
ser replicado manualmente na pasta de produção (nunca aplicar remoto sozinho
sem confirmação — é ação com efeito em sistema compartilhado):

1. Copiar o(s) arquivo(s) de migration novos para
   `ProjetoEstoque/migrations/` em produção.
2. Rodar `python manage.py migrate` na pasta de produção (DB próprio, dados
   reais de colaboradores/equipamentos).
3. Se a mudança adicionou um **novo management command agendado**
   (ex.: coletor, digest), lembrar que produção usa **Windows Task Scheduler**,
   não cron/Celery — é preciso registrar a tarefa lá com
   `agendar_relatorio criar` / `agendar_prtg criar` (terminal Administrador),
   e essas tarefas **não se auto-registram**.
4. Servidor de produção roda via Waitress (não `runserver`), com WhiteNoise
   servindo estáticos e SQLite em modo WAL — se a mudança envolve
   performance/concorrência, checar essas três peças antes de suspeitar do
   código da view.
5. `DJANGO_DEBUG=False` sempre em produção; `DJANGO_ALLOWED_HOSTS` deve conter
   IP interno (`172.16.60.254`) e o domínio externo.

## Checklist final
- [ ] Migration é backwards-safe
- [ ] `migrate` rodado em dev
- [ ] Usuário avisado do que replicar manualmente em produção (arquivo de
      migration + `migrate` + eventual tarefa agendada nova)
- [ ] Se mexeu em `.env`/settings, checklist de variáveis críticas de produção
      revisado (DEBUG, ALLOWED_HOSTS)
