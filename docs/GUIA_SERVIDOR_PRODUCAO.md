# Guia do Servidor de Produção — Sistema de Controle de TI

**Santa Colomba Agropecuária**
Documento criado em: 26/06/2026
Assunto: melhoria de desempenho do servidor (troca do `runserver` por **Waitress**), tutorial de uso e deploy.

---

## Índice

1. [Por que mudou (o problema)](#1-por-que-mudou-o-problema)
2. [O que foi implementado](#2-o-que-foi-implementado)
3. [Antes vs Depois (runserver x Waitress)](#3-antes-vs-depois-runserver-x-waitress)
4. [Tutorial — Rodar e acessar na sua máquina](#4-tutorial--rodar-e-acessar-na-sua-máquina)
5. [Tutorial — Subir em produção (passo a passo)](#5-tutorial--subir-em-produção-passo-a-passo)
6. [Como parar e reiniciar o servidor](#6-como-parar-e-reiniciar-o-servidor)
7. [Impactos e pontos de atenção](#7-impactos-e-pontos-de-atenção)
8. [Solução de problemas (troubleshooting)](#8-solução-de-problemas-troubleshooting)
9. [Próximas melhorias possíveis](#9-próximas-melhorias-possíveis)
10. [Anexo — Detalhes técnicos das mudanças](#10-anexo--detalhes-técnicos-das-mudanças)

---

## 1. Por que mudou (o problema)

O servidor estava **lento com vários usuários ao mesmo tempo**. O diagnóstico mostrou que **não era volume de dados** (o banco tem ~3,4 MB), e sim a **forma como o servidor rodava**:

- O sistema estava sendo executado em produção com `python manage.py runserver`, que é um **servidor de desenvolvimento** — a própria documentação do Django avisa que ele **não deve ser usado em produção**.
- Os arquivos estáticos (CSS, JS, imagens, ícones) eram servidos **um a um pelo Python**, sem compressão e sem cache.
- Em momentos de vários acessos simultâneos, as requisições **entravam em fila** → sensação de travamento.

A solução foi trocar a "ferramenta de teste" por uma **ferramenta de produção de verdade**, sem mexer em nenhuma regra de negócio do sistema.

---

## 2. O que foi implementado

| Arquivo | O que mudou |
|---|---|
| `servir_producao.py` *(novo)* | Sobe o sistema com **Waitress** (servidor WSGI real para Windows), com 8 threads. |
| `iniciar_servidor.bat` *(novo)* | **Atalho**: clica 2x, ele roda o `collectstatic` e sobe o servidor. Detecta o venv automaticamente. |
| `requirements.txt` | Adicionadas as bibliotecas `waitress` e `whitenoise`. |
| `controle/settings.py` | Ativado o **WhiteNoise** (serve estáticos comprimidos com cache no navegador). |
| `controle/urls.py` | `/media/` (fotos de itens, termos, anexos) agora funciona também com `DEBUG=False`. |
| `ProjetoEstoque/apps.py` | Banco SQLite reforçado: `busy_timeout`, `temp_store=MEMORY`, mais cache (o modo WAL já existia). |
| `ProjetoEstoque/middleware.py` | Checagem de grupo (TV/Fornecedor) com **cache de 60s** → menos consultas ao banco por requisição. |

> ✅ **Nenhuma view, formulário, service, model ou regra de negócio foi alterada.** Itens, movimentações, termos, preventivas, dashboards, PRTG, quiosque e portal do fornecedor continuam idênticos.

**As 3 melhorias que mais pesam:** Waitress (atende em paralelo) + WhiteNoise (estáticos rápidos) + SQLite WAL (leitura e escrita sem travar).

---

## 3. Antes vs Depois (runserver x Waitress)

| Aspecto | `runserver` (antes) | Waitress + WhiteNoise (agora) |
|---|---|---|
| Para que foi feito | Programar/testar no PC | Atender usuários de verdade, o dia todo |
| Vários acessos juntos | Requisições **enfileiram** | Pool de **8 threads** em paralelo |
| Estáticos (CSS/JS) | Pelo Python, sem compressão/cache | **Comprimido (Brotli)** + **cache** no navegador |
| Com `DEBUG=False` | **Não serve estático** (site sem visual) | Serve normalmente |
| Estável ligado 24/7 | Frágil | Estável |
| Segurança | Django: *"nunca use em produção"* | Servidor WSGI próprio para isso |
| Recarrega código sozinho | Sim | Não (reinicia ao subir código) |

> 💡 Com **um único usuário** a diferença é pequena. O ganho grande aparece com **muitos acessos simultâneos** — exatamente o cenário que estava lento.

---

## 4. Tutorial — Rodar e acessar na sua máquina

Para testar localmente (no seu PC de desenvolvimento):

**Opção A — pelo atalho (igual produção):**
1. Dê **2 cliques** em `iniciar_servidor.bat`.
   - Como no seu PC não há venv, ele avisa e usa o Python do sistema (isso é normal para teste).
2. Espere aparecer na janela preta: `Escutando em  http://0.0.0.0:65300`
3. Abra no navegador: **http://localhost:65300**
4. Faça login normalmente.

**Opção B — modo dev de sempre (para programar):**
```
cd controle
python manage.py runserver
```
Abra: **http://127.0.0.1:8000**

> ⚠️ `0.0.0.0` **não** é um endereço para digitar — significa "escutando em todas as interfaces". Você acessa por `localhost` ou `127.0.0.1`.

---

## 5. Tutorial — Subir em produção (passo a passo)

> A produção roda de uma pasta/máquina separada:
> `C:\Projeto Djngo v2\controle-ti\controle-ti\controle`
> com venv próprio em `venv\Scripts\python.exe` e banco de dados próprio.

**Passo 1 — Copiar os arquivos** alterados/novos para a pasta de produção:
- `servir_producao.py` *(novo)*
- `iniciar_servidor.bat` *(novo)*
- `requirements.txt`
- `controle/settings.py`
- `controle/urls.py`
- `ProjetoEstoque/apps.py`
- `ProjetoEstoque/middleware.py`
- `GUIA_SERVIDOR_PRODUCAO.md` *(este guia)*

**Passo 2 — Instalar as bibliotecas no venv de produção** (obrigatório):
```
"C:\Projeto Djngo v2\controle-ti\controle-ti\controle\venv\Scripts\python.exe" -m pip install waitress whitenoise
```

**Passo 3 — Conferir o caminho do venv** no topo do `iniciar_servidor.bat`:
```
set "VENV_DIR=C:\Projeto Djngo v2\controle-ti\controle-ti\controle\venv"
```
(Se o venv estiver em outro lugar, corrija essa linha. A pasta certa do venv é a que contém `Scripts\python.exe` dentro.)

**Passo 4 — Parar o `runserver` atual** (Ctrl+C na janela dele, ou feche).

**Passo 5 — Iniciar o novo servidor:** dê **2 cliques** em `iniciar_servidor.bat`.
Você verá:
```
Usando Python do venv.
Coletando arquivos estaticos...
XXX static files copied...
==============================================================
  Sistema de Controle de TI - Servidor de Producao (Waitress)
  Escutando em  http://0.0.0.0:65300
  Threads: 8
==============================================================
```

**Passo 6 — Validar no navegador:** abra `http://172.16.60.254:65300` (ou o domínio externo) e confirme:
- ✅ o visual/CSS carrega;
- ✅ fotos de itens e termos abrem;
- ✅ abrir 2–3 telas ao mesmo tempo **não trava**.

> 📌 **Esta atualização NÃO precisa de `migrate`** — nenhum model foi alterado.
> Lembrete geral: toda vez que você mudar um *model*, é obrigatório rodar `manage.py migrate` na máquina de produção.

---

## 6. Como parar e reiniciar o servidor

- **Parar:** clique na janela preta do servidor e aperte **Ctrl+C**.
- **Reiniciar (ex.: depois de subir código novo):** pare com Ctrl+C e dê **2 cliques** no `.bat` de novo.
- **Importante:** o Waitress **não recarrega o código sozinho** (diferente do `runserver`). Toda vez que subir arquivos novos, **reinicie** o servidor para as mudanças valerem.
- A **janela preta precisa ficar aberta** enquanto o sistema estiver no ar. Se fechar, o servidor cai.

---

## 7. Impactos e pontos de atenção

Os únicos comportamentos que mudam:

1. **Grupos "Visualizador TV" e "Fornecedor" demoram até 60s para valer.**
   Se adicionar/remover alguém desses grupos, pode levar até 1 minuto (por causa do cache de desempenho). Todo o resto (login, permissões de admin) continua **imediato**.

2. **O servidor não recarrega código sozinho.** Reinicie após cada deploy (ver seção 6).

3. **Arquivos de `/media/` ficam acessíveis por URL direta em produção** (era o comportamento desejado e já valia em dev). Apenas fica o registro, caso algum termo seja considerado sigiloso.

4. **Dependência nova:** o servidor **não inicia** sem `waitress` e `whitenoise` instalados no venv (Passo 2). Se esquecer, o `.bat` dá erro de import logo no começo.

5. **Tarefas agendadas** (`enviar_alertas`, `monitorar_prtg`, `agendar_relatorio`) **não são afetadas** — o middleware só roda em requisições web. Os ajustes de banco valem para elas, mas só para melhor.

---

## 8. Solução de problemas (troubleshooting)

| Sintoma | Causa provável | Solução |
|---|---|---|
| `[AVISO] venv nao encontrado` | Caminho do venv errado no `.bat`, ou está testando no PC de dev | Em produção: corrija o `VENV_DIR`. No dev: é normal, ele usa o Python do sistema. |
| `ModuleNotFoundError: waitress` / `whitenoise` | Bibliotecas não instaladas no venv | Rode o Passo 2 (`pip install waitress whitenoise`). |
| Site abre **sem CSS / sem visual** | `collectstatic` não rodou, ou `STATIC_ROOT` vazio | O `.bat` já roda o `collectstatic`. Rode manualmente: `python manage.py collectstatic --noinput`. |
| Erro **400 Bad Request** | Host não está em `DJANGO_ALLOWED_HOSTS` | Adicione o IP/domínio no `.env` (`172.16.60.254`, domínio externo). |
| Fotos/termos não abrem (404) | Arquivo não existe em `media/`, ou caminho errado | Confirme que a pasta `media/` foi para produção. |
| `Address already in use` (porta 65300) | Já existe um servidor rodando na porta | Feche o `runserver`/servidor antigo antes de iniciar o novo. |
| Página continua lenta | Cache do navegador antigo, ou `DEBUG=True` em produção | Force atualização (Ctrl+F5). Confirme `DJANGO_DEBUG=False` no `.env` de produção. |

---

## 9. Próximas melhorias possíveis

- **Auto-início do servidor** (Agendador de Tarefas do Windows "Ao iniciar o sistema") — sobe o servidor sozinho se a máquina reiniciar, sem precisar abrir o `.bat` na mão.
- **Apontar as tarefas agendadas para o `python.exe` do venv** (caso ainda usem o Python global).
- **Cache de páginas/dashboards pesados** caso a base cresça muito no futuro.
- **Migração para PostgreSQL** apenas se o volume de usuários/dados crescer bastante (hoje o SQLite com WAL atende bem).

---

## 10. Anexo — Detalhes técnicos das mudanças

**`servir_producao.py`** — inicia `waitress.serve(application, host, port, threads=8)`. Aceita variáveis de ambiente `HOST`, `PORT`, `THREADS`.

**`iniciar_servidor.bat`** — usa o Python do venv (`VENV_DIR\Scripts\python.exe`) se existir; senão cai para o Python do PATH (teste local). Roda `collectstatic --noinput` e depois `servir_producao.py`.

**`settings.py`:**
- `whitenoise.middleware.WhiteNoiseMiddleware` adicionado logo após o `SecurityMiddleware`.
- `STORAGES["staticfiles"] = "whitenoise.storage.CompressedStaticFilesStorage"` (compressão sem manifesto — não quebra referências `{% static %}` existentes).

**`urls.py`** — rota `re_path(r'^media/(?P<path>.*)$', serve, {'document_root': MEDIA_ROOT})` para servir uploads também com `DEBUG=False` (o WhiteNoise serve só estáticos, não media, que muda em tempo de execução).

**`apps.py`** — no signal `connection_created`, para SQLite:
```
PRAGMA journal_mode=WAL;       (já existia — leitura e escrita em paralelo)
PRAGMA synchronous=NORMAL;     (já existia)
PRAGMA busy_timeout=20000;     (espera até 20s por lock em vez de falhar)
PRAGMA temp_store=MEMORY;      (tabelas temporárias em RAM)
PRAGMA cache_size=-20000;      (~20 MB de cache de páginas)
```

**`middleware.py`** — função `_grupos_do_usuario(user)` cacheia os nomes de grupo por 60s (`LocMemCache`), eliminando 2 consultas ao banco por requisição em `TVAccessMiddleware` e `FornecedorAccessMiddleware`.

**Validação feita (26/06/2026):** com `DEBUG=False`, `login/` → **200**; arquivo estático → **200** com `Content-Encoding: br` (Brotli) e `Cache-Control: max-age=60`. `python manage.py check` → 0 erros.

---

*Fim do guia. Em caso de dúvida, este arquivo fica em `controle/GUIA_SERVIDOR_PRODUCAO.md`.*
