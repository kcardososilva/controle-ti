# Padrão de Layout — Sistema **Zelo** (Santa Colomba · Controle de TI)

Guia **e prompt reutilizável** para refinar (ou criar) qualquer tela com aparência
**profissional, consistente e sem “cara de IA”**, mantendo 100% das funcionalidades.

Baseado no redesenho aprovado do **base.html** e do módulo **Quiosque**
(dashboard, detalhe, mapa e matrículas).

---

## 0. Prompt pronto (cole isto ao pedir uma tela)

> **Refine a tela `<NOME/URL>` do sistema Zelo (Django, pt-BR) para ficar profissional e sem “cara de IA”, sem mudança radical.**
>
> **Regras obrigatórias:**
> 1. **Não altere as funcionalidades.** Mexa só no template (e, se necessário, ajustes mínimos de contexto na view). **Preserve todos os hooks de JS** (IDs, classes usadas em `querySelector`, `json_script`, `data-*`).
> 2. **Use os tokens do `base.html`** (`--brand`, `--success`, `--warning`, `--danger`, `--info`, `--text-*`, `--separator-*`, `--bg-*`, `--radius-*`, `--shadow-*`). **Nunca** use hex chumbado nem `:root` dentro da tela. Escopo do CSS por prefixo: `:where(.xx-page)`.
> 3. **Um único acento: o azul da marca** (`--brand`). Cor semântica (verde/âmbar/vermelho) **só quando comunica significado** (status, atenção, erro). Nada de arco-íris.
> 4. **Adicione o breadcrumb no conteúdo** (topo da página), não no topbar: `{% block breadcrumbs %}<nav class="breadcrumbs">…</nav>{% endblock %}` com `Início › Seção › Página`.
> 5. **Remova os “tells” de IA** (ver checklist §6): gradiente em texto, glow/pulse, blur/vibrancy, números 800 gigantes, banner colorido grande, botões pílula-total, raios exagerados, ícones arco-íris.
> 6. **Padronize** cards, tabelas e botões pelos componentes deste guia. **Dark mode** deve funcionar (só tokens).
> 7. Ao final, **valide** renderizando a tela (§11) e confirme que os hooks de JS continuam presentes.
>
> Entregue o template completo em pt-BR e explique brevemente o que mudou e o que foi preservado. Procure **1–2 melhorias profissionais** aplicáveis (§10).

---

## 1. Princípios

- **Densidade com calma**: informação clara, hierarquia por tipografia e espaçamento — não por cor e sombra.
- **Consistência > novidade**: as telas parecem o mesmo produto.
- **Cor com intenção**: neutro por padrão; acento e semântico com parcimônia.
- **Movimento discreto e funcional** (120–200ms), nunca decorativo.
- **Acessível**: `aria-*`, foco visível, navegável por teclado.

---

## 2. Fundações — tokens (nunca hardcode)

| Uso | Token |
|---|---|
| Acento da marca | `--brand`, `--brand-hover`, `--brand-soft`, `--brand-ring` |
| Texto | `--text-primary/secondary/tertiary/quaternary` |
| Superfícies | `--bg-elevated`, `--bg-page-2`, `--bg-hover`, `--bg-fill-tertiary` |
| Linhas | `--separator-hairline`, `--separator-non-opaque` |
| Semântico | `--success(-soft)`, `--warning(-soft)`, `--danger(-soft)`, `--info(-soft)` |
| Raio | `--radius-sm` (10, botões) · `--radius-md` (14) · `--radius-lg` (18, cards) |
| Sombra | `--shadow-xs` (repouso) · `--shadow-sm/md` (hover) |
| Mono | `--font-mono` (série, código, IP, MAC) |
| Transição | `--transition-fast` |

Números sempre com `font-variant-numeric: tabular-nums`.

---

## 3. Estrutura da página

```django
{% extends 'base.html' %}
{% load humanize %}                    {# + l10n se usar |unlocalize #}

{% block title %}…{% endblock %}
{% block page_title %}…{% endblock %}
{% block page_subtitle %}…{% endblock %}

{% block breadcrumbs %}
<nav class="breadcrumbs" aria-label="Você está aqui">
  <a href="{% url 'dashboard' %}"><i class="fa-solid fa-house-chimney"></i>Início</a>
  <i class="fa-solid fa-chevron-right crumb-sep"></i>
  <a href="{% url 'secao' %}">Seção</a>
  <i class="fa-solid fa-chevron-right crumb-sep"></i>
  <span class="crumb-current" aria-current="page">Página atual</span>
</nav>
{% endblock %}

{% block header_actions %}…botões .btn .btn-sm…{% endblock %}

{% block extra_css %}<style> /* escopo :where(.xx-page) */ </style>{% endblock %}
{% block content %}<div class="xx-page"> … </div>{% endblock %}
{% block extra_js %}<script> … </script>{% endblock %}
```

`.breadcrumbs` e `.crumb-sep`/`.crumb-current` já existem no `base.html` — só usar as classes.

---

## 4. Componentes (copiar e adaptar)

### 4.1 Metric strip (KPIs sóbrios; células podem ser filtros)
Separador por `gap:1px` — funciona em qualquer nº de colunas e ao quebrar linha.

```css
.xx-metrics{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:1px;
  background:var(--separator-hairline); border:1px solid var(--separator-hairline);
  border-radius:var(--radius-lg); overflow:hidden; box-shadow:var(--shadow-xs); }
@media(max-width:560px){ .xx-metrics{ grid-template-columns:repeat(2,1fr);} }
.xx-metric{ background:var(--bg-elevated); padding:14px 16px; text-decoration:none; color:inherit; }
.xx-metric.is-active{ background:var(--brand-soft); }         /* quando é filtro */
.xx-metric-l{ display:flex; align-items:center; gap:6px; font-size:11px; font-weight:600; color:var(--text-tertiary); }
.xx-metric-l .mdot{ width:7px; height:7px; border-radius:99px; }  /* semântico */
.xx-metric-v{ margin-top:8px; font-size:24px; font-weight:700; letter-spacing:-.035em; line-height:1;
  color:var(--text-primary); font-variant-numeric:tabular-nums; }
```
> Regra: número **700** (não 800) e ~24px (não 1.7rem gigante). Contagens **reais** (não estimadas na lista visível).

### 4.2 Card
```css
.xx-card{ background:var(--bg-elevated); border:1px solid var(--separator-hairline);
  border-radius:var(--radius-lg); box-shadow:var(--shadow-xs); overflow:hidden; }
.xx-card-head{ display:flex; align-items:center; justify-content:space-between; gap:12px;
  padding:12px 16px; border-bottom:1px solid var(--separator-hairline); }
.xx-card-title{ display:flex; align-items:center; gap:8px; font-size:13.5px; font-weight:600; color:var(--text-primary); }
.xx-card-title i{ color:var(--text-tertiary); font-size:13px; }   /* ícone neutro, não colorido */
.xx-card-body{ padding:16px; }
```

### 4.3 Tabela profissional
```css
.xx-scroll{ width:100%; overflow-x:auto; }
.xx-table{ width:100%; border-collapse:collapse; min-width:820px; }
.xx-table thead th{ position:sticky; top:0; background:var(--bg-elevated); padding:10px 14px; text-align:left;
  font-size:10.5px; text-transform:uppercase; letter-spacing:.05em; color:var(--text-tertiary); font-weight:600;
  border-bottom:1px solid var(--separator-hairline); white-space:nowrap; }
.xx-table tbody td{ padding:11px 14px; border-bottom:1px solid var(--separator-hairline);
  font-size:13px; color:var(--text-primary); vertical-align:middle; white-space:nowrap; }
.xx-table tbody tr[data-href]{ cursor:pointer; }
.xx-table tbody tr:hover{ background:var(--bg-hover); }
.xx-table tbody tr:last-child td{ border-bottom:0; }
/* célula principal: tile de ícone monocromático + nome + sub em mono */
.xx-ic{ width:34px; height:34px; border-radius:8px; display:grid; place-items:center;
  background:var(--bg-fill-tertiary); border:1px solid var(--separator-hairline); color:var(--text-secondary); }
```
Linha inteira clicável (respeita links/botões internos):
```js
document.querySelectorAll('.xx-table tr[data-href]').forEach(tr=>{
  tr.addEventListener('click',e=>{ if(e.target.closest('a,button'))return;
    window.location.href=tr.getAttribute('data-href'); });
});
```

### 4.4 Status (ponto + rótulo, semântico)
```css
.xx-status{ display:inline-flex; align-items:center; gap:7px; font-size:12.5px; font-weight:500; }
.xx-status .sdot{ width:7px; height:7px; border-radius:99px; }
.xx-status.ok{ color:var(--success);}  .xx-status.ok  .sdot{ background:var(--success);}
.xx-status.warn{ color:var(--warning);} .xx-status.warn .sdot{ background:var(--warning);}
.xx-status.bad{ color:var(--danger);}   .xx-status.bad .sdot{ background:var(--danger);}
.xx-status.off{ color:var(--text-tertiary);} .xx-status.off .sdot{ background:var(--text-quaternary);}
```
> **Bateria/valores**: colorir só quando pede atenção (baixo=danger, médio=warning, ok=neutro). Verde em tudo vira ruído.

### 4.5 Botões (use os do base.html)
`.btn`, `.btn-primary`, `.btn-danger`, `.btn-sm` — já padronizados (raio 10px, sombra sutil no primário). **Não** crie botões pílula nem `btn-light` (não existe). Ícone de ação em linha: quadrado 30px, raio-sm, neutro → `--brand`/`--danger` no hover.

### 4.6 Formulário
`.form-control` do base.html. Label `font-size:12px; color:var(--text-secondary)` (não caixa-alta gritante). Agrupar em `.field{display:flex;flex-direction:column;gap:6px}`.

### 4.7 Estado vazio
```css
.xx-empty{ display:grid; place-items:center; gap:10px; padding:44px 16px; text-align:center;
  color:var(--text-tertiary); font-size:13px; }
.xx-empty i{ font-size:24px; color:var(--text-quaternary); }
```

---

## 5. Regras de cor

- **1 acento** (brand blue) para ação/foco/ativo.
- **Semântico só com significado.** Paleta iOS **dessaturada**; nunca hex solto.
- **Ícones de card/cabeçalho**: neutros (`--text-tertiary`). Cor só onde informa.
- **Dark mode**: só tokens → adapta sozinho. Cores de mapa (Leaflet) podem ser hex fixo (superfície especial), alinhadas à paleta (`#34c759` online, `#8e8e93` offline, `#0071e3` foco).

---

## 6. Checklist ANTI-IA (remover se existir)

- [ ] Gradiente em **texto** (wordmark, título) → cor sólida.
- [ ] **Glow/pulse**, sombras coloridas “brilhando”, `box-shadow` de neon.
- [ ] **Blur/vibrancy** (`backdrop-filter`) decorativo.
- [ ] **Ícones arco-íris** (cada item uma cor aleatória saturada).
- [ ] Números **800 gigantes** (1.6–1.7rem) em KPI → 700 / ~24px, tabulares.
- [ ] **Banner colorido grande** de “explicação” → nota discreta (`--bg-page-2`, texto tercário).
- [ ] Botões **pílula total** → raio `--radius-sm`.
- [ ] Raios `--radius-xl`/22px por toda parte → `--radius-lg` (18) em card, `--radius-md/sm` no resto.
- [ ] **Hex chumbado** (`#4f46e5`, `#1e8e3e`, `#d93025`…) → tokens.
- [ ] Partículas/canvas 3D decorativo.
- [ ] Emoji como ícone; múltiplas fontes; tudo centralizado sem hierarquia.

---

## 7. Movimento
Transições 120–200ms (`--transition-fast`). Hover: fundo / borda / leve elevação. **Sem** animação infinita decorativa (exceto indicadores de status pontuais e discretos).

## 8. Acessibilidade
`aria-label`/`aria-current` nos breadcrumbs e ações; `:focus-visible` (herda do base); alvos ≥ 30px; contraste ok; navegação por teclado (Esc fecha modal/drawer; `/` foca busca).

## 9. Preservar funcionalidade (crítico)
- **Não** renomeie IDs/classes usados em JS (`querySelector`), nem os IDs de `json_script`, nem `data-*`.
- **Não** altere views/URLs/forms — salvo ajuste mínimo de contexto (ex.: contagens reais para um metric strip). Se mexer na view, mantenha o comportamento existente.
- Mantenha `{% csrf_token %}`, `action`, `method`, `name=` dos formulários.
- Floats em URL/JS: `|unlocalize` (+ `{% load l10n %}`) ou `json_script` — nunca o valor localizado (vírgula) direto.

---

## 10. Melhorias profissionais a considerar (por tela)

Escolha as que fizerem sentido — sem inflar:

- **Copiar para a área de transferência** (códigos, série, IP) com feedback breve.
- **Linha clicável** → detalhe (ignorando links/botões internos).
- **Busca client-side** que filtra a lista/tabela ao digitar.
- **Filtro por chips/segmented** ou metric-cells clicáveis com estado ativo.
- **Toggle de densidade** (confortável/compacto) persistido em `localStorage`.
- **Seleção múltipla + barra de ação em massa** (aparece só com seleção).
- **Colunas ordenáveis** (caret sutil, `tabular-nums`).
- **Drawer de detalhe** ao clicar na linha (desktop: lateral; mobile: bottom-sheet).
- **Sticky header** na tabela; paginação “X–Y de N” + linhas por página.
- **Skeleton/loading** discreto em telas com AJAX.
- **Estados vazios** com ação (“criar o primeiro …”).
- **Atalhos de teclado** para power users (`/` busca, `Esc` fecha).
- **Breadcrumbs** em toda tela (1 bloco).
- **Tabular-nums** em toda coluna numérica; **mono** em série/código/IP/MAC.

---

## 11. Validação (rodar antes de entregar)

De dentro de `controle/`, renderize a tela com um request real + contexto mock
(pega erro de sintaxe, `{% url %}` inexistente, filtro sem `{% load %}`):

```python
# _smoke.py  (apagar depois)
import django, os; os.environ.setdefault("DJANGO_SETTINGS_MODULE","controle.settings"); django.setup()
from django.test import RequestFactory; from django.urls import resolve
from django.contrib.auth.models import AnonymousUser
from django.template.loader import get_template, render_to_string
get_template("front/<dominio>/<tela>.html")                       # compila
r = RequestFactory().get("/<url>/"); r.user = AnonymousUser()
try: r.resolver_match = resolve("/<url>/")
except Exception: r.resolver_match = None
html = render_to_string("front/<dominio>/<tela>.html", { "request": r, "user": r.user, ...mock... }, request=r)
print(len(html), "breadcrumbs" in html)   # + checar hooks de JS específicos
```

`python _smoke.py` deve renderizar sem exceção e conter os hooks esperados.

---

### Referência viva
Telas já no padrão: **módulo Quiosque completo** (`quiosque_dashboard`, `quiosque_detalhe`,
`quiosque_mapa`, `quiosque_matriculas`, `quiosque_config`) e **`equipamentos_list`**
(breadcrumb + botões). Use-as como exemplo — incl. formulário em seções + barra de salvar fixa
(`quiosque_config`) e metric strip + tabela (`quiosque_dashboard`).
