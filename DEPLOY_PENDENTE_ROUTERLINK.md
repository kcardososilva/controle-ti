# Pendência de deploy em produção — Reconciliação Routerlink + correção "congelado"

> Gerado em 2026-07-23 pelo Claude Code, a pedido do usuário, para servir de runbook
> ao aplicar em produção tudo que foi feito e validado em dev. **Não tenho acesso à
> pasta de produção a partir deste ambiente** — preciso que alguém rode isso lá (ou
> abra uma sessão de lá) seguindo os passos abaixo, na ordem, sempre revisando o
> dry-run antes de `--aplicar`.
>
> **Máquina de produção:** `172.16.60.254:65300` — pasta
> `C:\Projeto Djngo v2\controle-ti\controle-ti\controle`, **Python/venv próprio**
> (`venv\Scripts\python.exe`, Python 3.13 — diferente do dev), `db.sqlite3` próprio.
> Todos os comandos abaixo assumem que você está **dentro dessa pasta** e usa o
> `venv\Scripts\python.exe` dela, não o `python` do PATH. Depois de aplicar tudo,
> reiniciar o servidor (Waitress via `iniciar_servidor.bat`).

## IMPORTANTE — scripts salvos, não fiar na memória

Todo passo ad hoc que antes só estava descrito em texto (Fases 2.5 a 2.16)
agora tem um **script `.py` real e testado** em
`controle/deploy_scripts_routerlink/`. **Rode os scripts, não tente
reescrever a lógica do zero a partir da descrição** — cada um foi testado
contra o banco de dev nesta sessão (inclusive um bug de encoding do Windows
e, no script 09, um bug de tipo — guardava o `Item` errado em vez do
`Preventiva` na lista de resincronização, o que quebraria o `.save()` na
hora de aplicar — ambos só apareceram no teste real, corrigidos antes de
salvar). Todos seguem o mesmo padrão:

- Têm uma flag `APLICAR = False` no topo — na primeira rodada só imprime o
  diagnóstico (dry-run), sem alterar nada. Edite o arquivo (`APLICAR = True`)
  e rode de novo pra aplicar de verdade.
- São **idempotentes**: só preenchem campo em branco, nunca sobrescrevem um
  valor já preenchido — pode rodar de novo sem medo se não tiver certeza se
  já rodou.
- Rodam via `exec(open(...).read())`, **não** via `manage.py shell < arquivo.py`
  — essa segunda forma alimenta o REPL linha a linha por stdin e pode quebrar
  em blocos de código com `for`/`if` indentados; `exec` roda o arquivo inteiro
  de uma vez, sem essa armadilha.

```
controle/deploy_scripts_routerlink/
  01_tempo_contrato.py               (Fase 2.5)
  02_centro_custo_localidade.py      (Fase 2.6, parte 1)
  03_subtipo.py                      (Fase 2.6, parte 2 — reaproveitado na 2.8 também)
  04_numero_nf.py                    (Fase 2.7 — ajustar ARQUIVO no topo do script)
  05_cadastro_sao_paulo.py           (Fase 2.8 — ajustar ARQUIVO no topo do script)
  06_marca.py                        (Fase 2.9)
  07_valor_zero_e_data_entrada.py    (Fase 2.10 — rodar ANTES do 08)
  08_contrato.py                     (Fase 2.11 — depende do 07)
  09_preventiva_switch_ap_meraki.py  (Fase 2.12)
  10_ativar_sao_paulo.py             (Fase 2.13)
  11_quantidade_unitaria.py          (Fase 2.14)
  12_subtipo_cabo_forca.py           (Fase 2.15 — cria o Subtipo "Cabo de Força" antes de aplicar)
  13_corrigir_vinculo_83_114.py      (Fase 2.16 — IDs específicos de dev, ver aviso no topo do script)
```

## Arquivos a levar para produção

Junto com o código normal (git pull ou cópia manual), estes são novos/alterados
nesta leva de trabalho — confirmar que todos chegaram em produção antes de rodar
qualquer comando abaixo:

```
NOVOS:
  ProjetoEstoque/management/commands/aplicar_analise_cadastral.py
  ProjetoEstoque/management/commands/corrigir_periodos_locacao.py
  ProjetoEstoque/management/commands/reconciliar_planilha_fornecedor.py   (não usado nesta leva, mas presente no repo)
  ProjetoEstoque/migrations/0141_adiciona_numero_nf_item.py
  ProjetoEstoque/static/planilhas/modelo_cadastro_item.xlsx
  deploy_scripts_routerlink/*.py   (13 scripts — ver seção "IMPORTANTE" acima; NÃO fazem parte do app, mas precisam estar na pasta controle/ em produção pra rodar)

ALTERADOS:
  ProjetoEstoque/admin.py
  ProjetoEstoque/forms.py
  ProjetoEstoque/models.py                                    (+ Item.numero_nf)
  ProjetoEstoque/templates/front/equipamentos/cadastrar_equipamento.html
  ProjetoEstoque/templates/front/equipamentos/equipamentos_list.html
  ProjetoEstoque/views/equipamentos.py
  ProjetoEstoque/views/relatorios.py
  services/importador_planilha.py
```

## Fase 0 — Schema e estáticos (sempre necessário)

```bash
# já dentro de C:\Projeto Djngo v2\controle-ti\controle-ti\controle
venv\Scripts\python.exe manage.py migrate            # aplica 0141_adiciona_numero_nf_item
venv\Scripts\python.exe manage.py collectstatic      # produção serve estático via WhiteNoise (DEBUG=False) —
                                     # sem isso o botão "Modelo" na tela de Equipamentos 404.
```

Depois disso, `Item.numero_nf` (campo opcional, "Número da NF", vale pra item
locado ou não) já aparece no formulário de cadastro/edição e na Ficha Técnica
do detalhe. O importador de planilha (`Equipamentos > Importar`) já reconhece
a coluna "Número da NF" / "NÚMERO DA NF" / "NF DO ITEM".

## Fase 1 — Corrigir o bug do "congelado" (independente, sem risco, rodar sempre)

Achado nesta sessão: qualquer item locado com status Ativo/Backup/Manutenção que
**nunca teve nenhum `LocacaoPeriodo`** aparece como "congelado" no detalhe mesmo
estando ativo de verdade — o histórico de locação não existia antes de um certo
ponto e nunca foi backfilled. Em dev isso afetava **645 itens** de 4 fornecedores
(Routerlink, Simpress, Vivo Tech, TIM) — os números em produção vão ser diferentes
(dados reais distintos), mas o bug provavelmente também está lá.

Este comando **não depende da planilha do fornecedor nem de nenhum ID específico**
— só olha o próprio banco de produção. É seguro rodar independente da Fase 2.

```bash
# 1. Dry-run — sempre primeiro, sem --aplicar
venv\Scripts\python.exe manage.py corrigir_periodos_locacao --relatorio "C:\caminho\para\Downloads\congelado_producao_dryrun.xlsx"

# 2. Revisar o relatório gerado (aba "Congelado corrigido"): confirma que a
#    "Data de Início do Período" de cada item bate com a Locacao.data_entrada
#    real dele (ou, se não tinha, com a data de criação do Item).

# 3. Aplicar
venv\Scripts\python.exe manage.py corrigir_periodos_locacao --aplicar --relatorio "C:\caminho\para\Downloads\congelado_producao_aplicado.xlsx"
```

Validação rápida depois de aplicar (deveria dar 0):

```python
venv\Scripts\python.exe manage.py shell -c "
from ProjetoEstoque.models import Item, LocacaoPeriodo
print(Item.objects.filter(locado='sim', status__in=['ativo','backup','manutencao']).exclude(pk__in=LocacaoPeriodo.objects.values('item')).count())
"
```

## Fase 2 — Reconciliação Routerlink (planilha `Analise_Cadastral_Routerlink_vs_Fornecedor.xlsx`)

**⚠️ ATENÇÃO — checar isto antes de qualquer `--aplicar` aqui:** a planilha de
análise tem uma coluna `ID (TI)` que foi gerada comparando contra **este banco de
dev**, onde os IDs de `Item` são específicos daqui. Se o banco de produção tiver
os mesmos registros mas com PKs diferentes (bem provável — bancos separados), o
comando `aplicar_analise_cadastral` vai simplesmente **não encontrar** os itens
pelo ID e pular tudo com aviso — ele tem uma checagem de segurança que confere o
`Número de Série` além do ID antes de tocar em qualquer coisa, então na pior das
hipóteses ele não faz nada (não corrompe dado errado). Mas "não faz nada" não é
o mesmo que "aplicado com sucesso".

**Antes de aplicar em produção:**
1. Rodar em dry-run (sem `--aplicar`) e olhar a lista de avisos.
2. Se a maioria dos avisos for "não encontrado no banco" ou "número de série não
   bate" → os IDs não correspondem. Não adianta rodar essa planilha em produção
   como está — é preciso gerar uma **nova** planilha de Análise Cadastral
   comparando a lista real do fornecedor Routerlink contra o banco de
   **produção** (mesmo processo, dados de origem diferentes), e usar essa nova
   planilha nos comandos abaixo.
3. Se os avisos forem poucos/esperados → os IDs batem (produção pode ter sido
   clonada de dev, ou o snapshot é o mesmo) — pode seguir com o roteiro abaixo.

### 2.1 — Corrigir os itens divergentes (Modelo / Valor Mensal / PMB)

```bash
venv\Scripts\python.exe manage.py aplicar_analise_cadastral \
  --arquivo "C:\caminho\para\Analise_Cadastral_Routerlink_vs_Fornecedor.xlsx" \
  --fornecedor "Routerlink" \
  --relatorio "C:\caminho\para\Downloads\routerlink_producao_dryrun.xlsx"
# revisar o relatório, depois:
venv\Scripts\python.exe manage.py aplicar_analise_cadastral \
  --arquivo "C:\caminho\para\Analise_Cadastral_Routerlink_vs_Fornecedor.xlsx" \
  --fornecedor "Routerlink" --aplicar \
  --relatorio "C:\caminho\para\Downloads\routerlink_producao_aplicado.xlsx"
```

Regra de PMB usada (a pedido do usuário — a planilha de análise erra nisso):
nome do item contém "PMB" → força PMB=Sim; senão, usa o dado do fornecedor.

### 2.2 — Cadastrar os equipamentos "Ausente no TI" (separando licenças)

```bash
venv\Scripts\python.exe manage.py aplicar_analise_cadastral \
  --arquivo "C:\caminho\para\Analise_Cadastral_Routerlink_vs_Fornecedor.xlsx" \
  --fornecedor "Routerlink" --aplicar --cadastrar-novos \
  --relatorio "C:\caminho\para\Downloads\routerlink_cadastro_producao.xlsx" \
  --planilha-licencas "C:\caminho\para\Downloads\routerlink_licencas_producao.xlsx"
```

Linhas cujo Part Number bate padrão de licença (`SPLA-`, `LIC-` etc.) **nunca**
viram `Item` — vão para a planilha de licenças à parte, pra resolver no módulo
de Licenças manualmente. Em dev foram 11 dessas.

**Depois de cadastrar**, o usuário pediu para os itens novos nascerem com status
**Backup** (não Ativo) até ativação manual:

```python
venv\Scripts\python.exe manage.py shell -c "
from ProjetoEstoque.models import Item, StatusItemChoices
qs = Item.objects.filter(observacoes__icontains='aplicar_analise_cadastral')
print('candidatos:', qs.count())
qs.update(status=StatusItemChoices.BACKUP)
"
```

### 2.3 — ⚠️ Exclusão física dos "Ausente no Fornecedor" (IRREVERSÍVEL)

Itens que só existem no TI, sumiram da lista do fornecedor. O usuário pediu
exclusão **física** (não soft-delete), cascateando Locação, Movimentações,
Preventivas/Execuções/Respostas de checklist e histórico de status. Em dev:
60 itens, 60 Locações, 64 Movimentações, 3 Preventivas, 4 Execuções, 40
Respostas, 6 registros de status — **tudo apagado de verdade, sem volta**.

```bash
# dry-run primeiro, SEMPRE — o relatório grava o detalhe de tudo que seria
# apagado, porque depois de aplicar não tem mais como recuperar do banco.
venv\Scripts\python.exe manage.py aplicar_analise_cadastral \
  --arquivo "C:\caminho\para\Analise_Cadastral_Routerlink_vs_Fornecedor.xlsx" \
  --fornecedor "Routerlink" \
  --relatorio "C:\caminho\para\Downloads\routerlink_exclusao_dryrun_producao.xlsx"
# revisar a aba "Excluidos (detalhe)" e "Movimentacoes excluidas" com calma.
# só depois:
venv\Scripts\python.exe manage.py aplicar_analise_cadastral \
  --arquivo "C:\caminho\para\Analise_Cadastral_Routerlink_vs_Fornecedor.xlsx" \
  --fornecedor "Routerlink" --aplicar --excluir-ausentes \
  --relatorio "C:\caminho\para\Downloads\routerlink_exclusao_producao.xlsx"
```

Antes de rodar isso em produção, considerar pedir confirmação explícita de novo
— é dado real de produção sendo apagado permanentemente, mesmo que o usuário já
tenha autorizado esse mesmo tipo de ação em dev.

### 2.4 — Data de entrada dos itens novos + correção do período (encadeado com a Fase 1)

Depois do cadastro (2.2) e do ajuste de status (dentro de 2.2), rodar de novo o
comando da Fase 1 — ele preenche a `data_entrada` que a planilha não trazia
(inferida pelo modelo/fornecedor, igual foi feito em dev) e realinha o período
de locação desses itens novos, além de repetir o backfill do "congelado" pra
qualquer coisa que tenha ficado pra trás:

```bash
venv\Scripts\python.exe manage.py corrigir_periodos_locacao --relatorio "C:\caminho\para\Downloads\periodos_fase2_dryrun.xlsx"
# revisar, depois:
venv\Scripts\python.exe manage.py corrigir_periodos_locacao --aplicar --relatorio "C:\caminho\para\Downloads\periodos_fase2_aplicado.xlsx"
```

### 2.5 — Tempo de contrato (36 meses) nas locações sem esse dado

Script: `deploy_scripts_routerlink/01_tempo_contrato.py`.

```bash
venv\Scripts\python.exe manage.py shell -c "exec(open(r'deploy_scripts_routerlink\01_tempo_contrato.py', encoding='utf-8').read())"
```

Em dev, 403 das 404 locações Routerlink já preenchidas usavam exatamente 36
meses (padrão de contrato) — só 1 tinha valor diferente (1 mês, item id 112
em dev, provável exceção/typo pré-existente, **não mexi nele**; o script só
preenche o que está em branco, nunca sobrescreve). O script imprime a
contagem de valores já usados em produção antes de aplicar — conferir se 36
meses realmente é o padrão predominante lá também; se não for, ajustar a
constante `TEMPO_MESES` no topo do arquivo antes de rodar com `APLICAR=True`.

### 2.6 — Centro de Custo, Localidade (backup) e Subtipo dos itens novos

Scripts: `deploy_scripts_routerlink/02_centro_custo_localidade.py` (Centro de
Custo TI para todo Routerlink sem CC + Localidade Karitel só para o lote da
Fazenda) e `deploy_scripts_routerlink/03_subtipo.py` (genérico — roda de novo
depois da Fase 2.8 também, sem duplicar lógica).

```bash
venv\Scripts\python.exe manage.py shell -c "exec(open(r'deploy_scripts_routerlink\02_centro_custo_localidade.py', encoding='utf-8').read())"
venv\Scripts\python.exe manage.py shell -c "exec(open(r'deploy_scripts_routerlink\03_subtipo.py', encoding='utf-8').read())"
```

Subtipo casa o `modelo` do item contra o subtipo já usado em outro
equipamento Routerlink com o MESMO modelo (moda), mais uma lista de extensões
por família validadas manualmente (ver `EXTENSOES` no topo do script). Em
dev aplicou em 81 dos 245 itens da Fazenda; os outros ficaram sem subtipo de
propósito — majoritariamente `CAB-ACBZ-10A` (137 itens, cabo de força), sem
categoria cadastrada pra isso. **Recomendo criar um Subtipo "Cabo de
Força/Alimentação" em produção antes de rodar**, pra não repetir a lacuna —
se criar, adicione a entrada correspondente em `EXTENSOES` no script antes
de aplicar.

### 2.7 — Número da NF (planilha crua do fornecedor, com coluna NFe)

Script: `deploy_scripts_routerlink/04_numero_nf.py` — **ajuste a constante
`ARQUIVO` no topo** pro caminho real da planilha desta leva de produção antes
de rodar.

```bash
venv\Scripts\python.exe manage.py shell -c "exec(open(r'deploy_scripts_routerlink\04_numero_nf.py', encoding='utf-8').read())"
```

Fonte em dev: `RELAÇÃO DE EQUIPAMENTOS CORRETA - FORNECEDOR.xlsx` (abas
"Fazenda" + "São Paulo", cabeçalho na linha 4: Part Number/Serial/NFe/Valor
Mensal/PMB). Casa por `numero_serie` (não por ID). Em dev: 695 linhas válidas
na planilha, 653 itens do banco batidos e atualizados, 0 conflito (nenhum
item tinha NF antes). O script nunca sobrescreve uma NF já preenchida que
divirja da planilha — só avisa e pula, pra revisão manual.

### 2.8 — Cadastro dos equipamentos da filial São Paulo

Script: `deploy_scripts_routerlink/05_cadastro_sao_paulo.py` — **ajuste as
constantes `ARQUIVO` e `LOCALIDADE_NOME` no topo** (confirme que "TI - SP" é
o nome exato cadastrado em produção; foi confirmado assim para dev, mas pode
ter grafia diferente lá) antes de rodar. **Depois, rode o script 03 de novo**
(2.6) pra preencher o Subtipo desses itens — não duplica a lógica de mapeamento.

```bash
venv\Scripts\python.exe manage.py shell -c "exec(open(r'deploy_scripts_routerlink\05_cadastro_sao_paulo.py', encoding='utf-8').read())"
# depois de aplicar, rodar de novo:
venv\Scripts\python.exe manage.py shell -c "exec(open(r'deploy_scripts_routerlink\03_subtipo.py', encoding='utf-8').read())"
```

A planilha "RELAÇÃO DE EQUIPAMENTOS CORRETA - FORNECEDOR.xlsx" tem 2 abas:
"Fazenda" (já reconciliada nas fases anteriores) e **"São Paulo"**, que nunca
tinha sido comparada contra o TI antes (a análise cadastral original só
cobria a aba Fazenda) — em dev, todos os 30 itens de lá eram cadastro zero.
O script tem uma checagem de segurança: se algum número de série da aba já
existir no banco, ele avisa e recusa aplicar até você confirmar/corrigir —
não gera duplicata.

### 2.9 — Marca (fabricante) dos itens novos + correção de bug pré-existente

Script: `deploy_scripts_routerlink/06_marca.py`.

```bash
venv\Scripts\python.exe manage.py shell -c "exec(open(r'deploy_scripts_routerlink\06_marca.py', encoding='utf-8').read())"
```

Preenche `Item.marca` em qualquer Item Routerlink sem marca — correspondência
exata por modelo contra outro já classificado, com fallback por prefixo de
part number (lista `PREFIXOS_CISCO` no script — em dev, todo prefixo
observado até agora é Cisco, inclusive Meraki, que no cadastro existente já
usa marca="Cisco", não "Meraki" — Meraki vira `Subtipo`, não `Marca`, neste
sistema). Em dev aplicou em 269 de 276; os outros 7 ficaram sem marca por
falta de confiança no part number (`865408-B21`, `P74296-205`, `5051024W`,
`CORDAO-QD-RJ9`, `HEADSET-CHS-60`, `PATCH-CORD-FIBRA-2.5M-MULTI` x2).

O script **também detecta e imprime** (não corrige sozinho sem você
confirmar) um possível bug pré-existente: itens com `marca` literalmente
igual ao `modelo` (erro de importação antigo — em dev eram 6 registros,
`CAB-STACK-1M`/`CAB-STACK-50CM`/`MX64-HW`). Revise a lista impressa antes de
rodar com `APLICAR=True` — o script corrige automaticamente para "Cisco" só
os que estiverem nessa lista quando aplicar.

### 2.10 — Valor de Aquisição zerado + data de locação padronizada (todos os itens)

Script: `deploy_scripts_routerlink/07_valor_zero_e_data_entrada.py`. **Rodar
ANTES do 2.11** — o script de contrato decide o valor pela `data_entrada`, que
precisa estar completa primeiro.

```bash
venv\Scripts\python.exe manage.py shell -c "exec(open(r'deploy_scripts_routerlink\07_valor_zero_e_data_entrada.py', encoding='utf-8').read())"
```

Duas ações, escopo = **todos** os itens Routerlink (não só o lote desta
sessão):
- `Item.valor` (Valor de Aquisição) → 0, só para itens com `locado='sim'`
  (inclusive os que já tinham um valor > 0 cadastrado — Routerlink é locação,
  o custo real é `Locacao.valor_mensal`, já usado nos dashboards). Em dev
  zerou 645 itens, 179 dos quais tinham valor > 0 de verdade (ex.: R$4000,
  R$500). **Deliberadamente não mexe** nos 7 itens com `locado='nao'` (ver
  achado pendente novo abaixo) — zerar o valor deles apagaria um dado
  financeiro que pode ser real.
- `Locacao.data_entrada` ausente → preenchida por inferência (moda por
  Modelo, senão data mais comum entre as locações Routerlink já
  preenchidas), realinhando o período aberto (`LocacaoPeriodo`) junto. Em
  dev preencheu 5.

### 2.11 — Número do contrato (Locacao.contrato) nas locações sem esse dado

Script: `deploy_scripts_routerlink/08_contrato.py`. **Depende do 2.10** já
aplicado (usa `Locacao.data_entrada` pra decidir qual contrato).

```bash
venv\Scripts\python.exe manage.py shell -c "exec(open(r'deploy_scripts_routerlink\08_contrato.py', encoding='utf-8').read())"
```

Padrão identificado em dev: `RL240423-12-24` é o contrato "mestre" (cobre
todas as datas de entrada, de 2025-02-04 até 2026-02-04); `RL260287` é um
contrato novo usado só pelo lote que entrou em 2026-06-26. Regra: item cuja
`data_entrada == 2026-06-26` recebe `RL260287`, qualquer outra data recebe o
contrato mestre. **Confira em produção se essas duas datas/contratos batem**
— se produção tiver um lote de entrada diferente com contrato próprio, ajuste
as constantes `DATA_CONTRATO_NOVO`/`CONTRATO_NOVO` no topo do script antes de
aplicar. Em dev preencheu 286 locações (nunca sobrescreve um contrato já
preenchido, mesmo os ~44 valores estranhos residuais — ver achado pendente
já documentado).

### 2.12 — Preventivas de Switch / Access-Point / Meraki propagadas para os demais itens

Script: `deploy_scripts_routerlink/09_preventiva_switch_ap_meraki.py`.

```bash
venv\Scripts\python.exe manage.py shell -c "exec(open(r'deploy_scripts_routerlink\09_preventiva_switch_ap_meraki.py', encoding='utf-8').read())"
```

Padrão identificado em dev: dos itens Routerlink com `precisa_preventiva=sim`
nestes 3 subtipos, só uma fração já tinha uma `Preventiva` agendada (34/86
switches, 17/204 access-point, 1/8 meraki) — os demais nunca tiveram nenhuma,
apesar de precisarem. O checklist usado em 100% dos casos existentes já é o
checklist do próprio Subtipo (`Check List Switches`/`Access-Point`/`Meraki`);
o intervalo real vem de `Item.data_limite_preventiva` (já preenchido por
item, o script não mexe nisso). O script reaproveita a mesma lógica da view
oficial `preventiva_sincronizar_programacao`, só que escopada a Routerlink +
estes 3 subtipos (a view oficial roda para todos os fornecedores; não
tocamos os outros porque não foi pedido desta vez). Em dev criou 245
preventivas novas e ressincronizou 25 que já existiam com `data_proxima`
defasada. **Exclui automaticamente** qualquer item Meraki cuja marca contenha
"FORTINET" (achado pendente novo — ver abaixo) — não force isso incluindo
manualmente sem corrigir o Subtipo primeiro.

### 2.13 — Ativação dos equipamentos de São Paulo

Script: `deploy_scripts_routerlink/10_ativar_sao_paulo.py`.

```bash
venv\Scripts\python.exe manage.py shell -c "exec(open(r'deploy_scripts_routerlink\10_ativar_sao_paulo.py', encoding='utf-8').read())"
```

A "ativação manual" que ficou pendente desde a Fase 2.8 (itens nasceram
Backup de propósito). Muda status para Ativo em todo item Routerlink cuja
Localidade seja "TI - SP" (ajustar `LOCALIDADE_NOME` se o nome exato em
produção divergir). Ativo e Backup estão no mesmo grupo "ATIVOS" do
`locacao_service.py` — não mexe em `LocacaoPeriodo`. Retoma automaticamente
qualquer `Preventiva` pausada desses itens (em dev não havia nenhuma). Em
dev: 30 itens ativados.

### 2.14 — Quantidade unitária em todos os itens Routerlink

Script: `deploy_scripts_routerlink/11_quantidade_unitaria.py`.

```bash
venv\Scripts\python.exe manage.py shell -c "exec(open(r'deploy_scripts_routerlink\11_quantidade_unitaria.py', encoding='utf-8').read())"
```

`Item.quantidade = 1` para todo item Routerlink, incondicional (nenhum item
Routerlink usa `tem_lote=True`, então não há recálculo automático a partir
de `ItemLote` que possa desfazer isso depois — ver memória
`item_quantidade_vs_lotes`, que só se aplica a item com controle de lote).
Em dev: 9 itens corrigidos (5 estavam com quantidade=2, 4 com quantidade=0).

### 2.15 — Subtipo "Cabo de Força" para os CAB-ACBZ-10A

Script: `deploy_scripts_routerlink/12_subtipo_cabo_forca.py`. Resolve a
recomendação que ficou pendente desde a Fase 2.6 (maior lacuna de subtipo do
fornecedor).

```bash
venv\Scripts\python.exe manage.py shell -c "exec(open(r'deploy_scripts_routerlink\12_subtipo_cabo_forca.py', encoding='utf-8').read())"
```

Cria o Subtipo "Cabo de Força" (Categoria="Equipamento", `alocado="nao"` —
mesmo padrão de "Fonte"/"Gbic", acessório não alocado a colaborador; ajuste
`ALOCADO` no topo do script se não for esse o critério certo) e aplica em
todo item Routerlink com modelo="CAB-ACBZ-10A" sem subtipo. Em dev: 139
itens. **Não inclui** os 5 itens "CAB-ACBZ-12A" (mesma família, amperagem
diferente) — só o modelo pedido explicitamente; o script avisa se encontrar
esses casos, mas não aplica. Depois de rodar em produção, `03_subtipo.py`
já vai reconhecer "Cabo de Força" como opção de moda pra futuros cadastros
com modelo exatamente igual.

### 2.16 — Correção do achado "vínculo incompleto" (itens 83/114 em dev)

Script: `deploy_scripts_routerlink/13_corrigir_vinculo_83_114.py`. **⚠️ Os IDs
83/114 são específicos do banco de dev** — o script tem, comentada no final,
a query pronta pra achar os IDs corretos em produção (cruza os dois números
de série contra `Item.all_objects` de TODOS os fornecedores). Rode essa
query primeiro, edite o script com os IDs certos, só depois aplique.

```bash
venv\Scripts\python.exe manage.py shell -c "exec(open(r'deploy_scripts_routerlink\13_corrigir_vinculo_83_114.py', encoding='utf-8').read())"
# depois, nesta ordem (reaproveita a inferência de data/contrato já testada):
venv\Scripts\python.exe manage.py shell -c "exec(open(r'deploy_scripts_routerlink\07_valor_zero_e_data_entrada.py', encoding='utf-8').read())"
venv\Scripts\python.exe manage.py shell -c "exec(open(r'deploy_scripts_routerlink\08_contrato.py', encoding='utf-8').read())"
venv\Scripts\python.exe manage.py corrigir_periodos_locacao --aplicar --relatorio "C:\caminho\para\Downloads\periodos_pos_fix_83_114.xlsx"
```

Origem: o usuário cruzou a planilha `1V RELAÇÃO DE EQUIPAMENTOS CORRETA -
FORNECEDOR.xlsx` contra o sistema e reportou 2 itens como "não cadastrados"
(series `FTX1640GP1V` e `N4IG3901602IA`). Investigação em dev mostrou que
**já existiam** — são exatamente os 2 itens do achado "vínculo incompleto"
documentado desde a Fase 2.6 (ids 83 e 114), com `fornecedor=NULO`, por
isso desapareciam de qualquer comparação filtrada por Routerlink. Confirmado
contra os itens irmãos (35 com modelo `AIR-CAP3602I-A-K9` corretos, 4 com
modelo `PWR-INJ-8023AT` corretos) que havia também um typo de modelo (114) e
marca errada — Intelbras em vez de Cisco (83). O item 83 também estava
`locado=não` sem nenhuma `Locacao` — virou `locado=sim` com Locacao nova
(R$14/mês, igual à planilha).

**Efeito colateral observado durante a aplicação em dev (não é bug, é
comportamento correto)**: enquanto isso rodava, outros 2 itens dos "7 itens
locado='nao' suspeitos" (achado antigo) foram editados — pelo próprio
usuário, pelo visto, via a tela normal de edição — virando `locado=sim` com
Locacao própria (ids 1220 e 1267 em dev). Como os scripts 07/08 processam
**qualquer** locação Routerlink com dado em aberto (não só as que eu mirava),
eles pegaram esses 2 de brinde e preencheram `data_entrada`/contrato
automaticamente. `corrigir_periodos_locacao` completou o período do 1220
(status ativo) e corretamente NÃO criou período pro 1267 (status Defeito,
não deveria ter período aberto mesmo). Fica como registro: se isso acontecer
de novo em produção durante uma aplicação, é esperado — os scripts são
seguros de rodar mesmo com edições concorrentes acontecendo.

## Achados que ficaram pendentes (não foram corrigidos, só documentados)

Vale checar se existem em produção também:

1. ~~4 itens Routerlink com vínculo incompleto~~ — **RESOLVIDO na Fase 2.16**
   pros 2 casos que de fato existiam (ids 83 e 114 em dev, séries
   `N4IG3901602IA`/`FTX1640GP1V` — fornecedor NULO, typo de modelo/marca
   errada). Rodar o script 13 (com os IDs certos de produção) resolve.
2. **1 duplicata de número de série pré-existente** em dev (`VOLT-PWR-48V-10A-0006`,
   dois itens diferentes, valores mensais diferentes: R$30 e R$50) — não
   relacionada a este trabalho, criada em 18/06/2026, antes desta sessão.
3. **Provável erro de digitação no número de série** do item id 1247 em dev
   (`FHC11259AH5` no banco vs `FCH11259AH5` na planilha do fornecedor —
   letras trocadas) — por isso não recebeu NF automaticamente. Vale corrigir
   manualmente e reaplicar a Fase 2.7 nele.
4. ~~44 séries na planilha nova sem correspondência no banco~~ — **RE-ANALISADO
   na Fase 2.16** cruzando a planilha `1V RELAÇÃO...xlsx` (695 linhas)
   contra `Item.all_objects` de todos os fornecedores: sobram só **11** sem
   correspondência real, e as 11 são exatamente as licenças de software já
   separadas em `Routerlink_licencas_para_resolver.xlsx` (corretamente nunca
   viraram `Item`). Ou seja, não há mais gap real de cadastro — o número 44
   original era de antes da reconciliação completa. Segue valendo checar as
   **5 séries no banco sem correspondência na planilha nova** (não
   investigado, fora do escopo desta vez) e o typo do item 1247 abaixo.
5. **5 itens Routerlink com `locado='nao'` mas com "cara" de locado** (ids em
   dev: 124 ATTIV 1200VA BIVOLT, 1247 CP-7911G, 1285 IP PHONE 7942, 1332 UPS
   NEW ORION, 1382 PROBE - BACKUP) — não têm nenhum registro de `Locacao`,
   por isso ficaram de fora das Fases 2.10/2.11/2.4 (não têm
   `data_entrada`/contrato pra preencher). Dos 7 originais, **1220 e 1267 já
   foram corrigidos** (aparentemente pelo próprio usuário, via tela normal,
   durante a Fase 2.16 — ver nota de efeito colateral acima). Os mesmos
   modelos (CP-7911G, CP-7942G) aparecem alhures como locado='sim' com
   Locacao normal — pode ser
   um erro de cadastro (deveriam ser locado='sim' com Locacao própria) ou
   pode ser intencional (equipamento comprado avulso, não parte do contrato
   de locação). Não alterei `locado` porque é uma decisão de classificação,
   não um preenchimento de lacuna. Um desses (id 1247) é o mesmo do achado 3
   acima (typo de série).
6. **5 itens "CAB-ACBZ-12A" sem subtipo** (mesma família do "CAB-ACBZ-10A" que
   ganhou o Subtipo "Cabo de Força" na Fase 2.15, só que 12A em vez de 10A) —
   candidato óbvio para o mesmo Subtipo, mas o usuário só pediu explicitamente
   pelo modelo 10A. Se quiser estender, é só rodar o script 12 de novo com
   `MODELO_ALVO = "CAB-ACBZ-12A"`.
7. **1 item Meraki com marca Fortinet** (`FW-FGT-RDM`, modelo `FG-200E`, id
   945 em dev) — Subtipo cadastrado como "Meraki" mas o equipamento é
   claramente um Fortinet FortiGate (marca já correta, Subtipo errado). O
   Subtipo "Fortinet" já existe no sistema (1 item hoje). Script 09 exclui
   este item automaticamente do lote de preventivas (não aplica o checklist
   errado), mas não corrige o Subtipo — precisa de uma reclassificação manual
   de "Meraki" para "Fortinet".

## Ordem recomendada dos scripts (2.5–2.12)

Todos são idempotentes (só preenchem branco, seguro rodar mais de uma vez),
mas a ordem abaixo dá a base de referência mais completa pros scripts que
aprendem com o que já está classificado (03, 06) e respeita a dependência
07→08 (contrato decide pela data_entrada):

`corrigir_periodos_locacao` (Fase 1/2.4) → 01 (tempo) → 02 (CC/localidade) →
2.2 e 05 (cadastros, Fazenda e depois São Paulo) → 04 (NF) → 03 (subtipo,
rodar de novo depois do cadastro de SP) → 06 (marca) → 07 (valor zero +
data_entrada) → 08 (contrato) → 09 (preventiva switch/AP/meraki) → 10
(ativar São Paulo) → 11 (quantidade unitária) → 12 (subtipo Cabo de Força) →
13 (vínculo 83/114, com IDs corrigidos p/ produção) → 07 e 08 de novo (pegam
os dados em aberto dos itens que o 13 acabou de corrigir) →
`corrigir_periodos_locacao` de novo (garante período correto pros itens
recém-criados na Fase 2.8 e pro item do achado 5 que precisar).

## Checklist final

- [ ] Fase 0 — código deployado, `migrate` e `collectstatic` rodados
- [ ] Fase 1 — bug do "congelado" corrigido em produção (dry-run revisado antes)
- [ ] Fase 2.0 — dry-run inicial conferido: IDs da planilha batem com produção?
      Se não, gerar nova planilha de análise contra o banco de produção antes de continuar.
- [ ] Fase 2.1 — divergências corrigidas
- [ ] Fase 2.2 — equipamentos cadastrados, licenças separadas, status ajustado p/ Backup
- [ ] Fase 2.3 — **confirmação extra obtida** antes de excluir fisicamente; exclusão aplicada
- [ ] Fase 2.4 — data_entrada e período dos itens novos corrigidos
- [ ] Fase 2.5 — script `01_tempo_contrato.py`: tempo de contrato preenchido
- [ ] Fase 2.6 — script `02_centro_custo_localidade.py` + `03_subtipo.py`: Centro de Custo, Localidade e Subtipo preenchidos
- [ ] Fase 2.7 — script `04_numero_nf.py`: Número da NF vinculado
- [ ] Fase 2.8 — script `05_cadastro_sao_paulo.py`: equipamentos de São Paulo cadastrados (confirmar Localidade "TI - SP" em produção antes) + `03_subtipo.py` rodado de novo
- [ ] Fase 2.9 — script `06_marca.py`: Marca preenchida + bug marca==modelo revisado
- [ ] Fase 2.10 — script `07_valor_zero_e_data_entrada.py`: Valor de Aquisição zerado (locado='sim') + data_entrada padronizada
- [ ] Fase 2.11 — script `08_contrato.py`: número do contrato preenchido (confirmar datas/contratos batem com o lote de produção)
- [ ] Fase 2.12 — script `09_preventiva_switch_ap_meraki.py`: preventivas de switch/access-point/meraki propagadas
- [ ] Fase 2.13 — script `10_ativar_sao_paulo.py`: equipamentos de São Paulo ativados
- [ ] Fase 2.14 — script `11_quantidade_unitaria.py`: quantidade = 1 em todos os itens Routerlink
- [ ] Fase 2.15 — script `12_subtipo_cabo_forca.py`: Subtipo "Cabo de Força" criado e aplicado aos CAB-ACBZ-10A
- [ ] Fase 2.16 — achar os IDs corretos em produção (query comentada no script 13), rodar `13_corrigir_vinculo_83_114.py` + `07` + `08` de novo
- [ ] `corrigir_periodos_locacao --aplicar` rodado de novo por último (pega o período dos itens da Fase 2.8 e do achado 5 que precisar)
- [ ] Achados pendentes restantes (1 duplicata + 1 typo de série + 5 séries no banco sem correspondência na planilha nova + 5 itens locado='nao' suspeitos + 5 itens CAB-ACBZ-12A + 1 item Meraki/Fortinet mal classificado) revisados/registrados como tarefa separada
