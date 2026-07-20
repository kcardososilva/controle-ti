---
name: planta-canvas
description: Guia para adicionar/alterar funcionalidades no mapa visual de infraestrutura (Canvas 2D/3D vanilla JS compartilhado entre editor, viewer e modo TV). Usar quando o pedido envolver planta_editor, planta_viewer, planta_tv, novo tipo de elemento/conexão no mapa.
---

# Mapa de Plantas — Canvas 2D/3D

Três templates compartilham a mesma arquitetura de Canvas vanilla JS (sem
biblioteca): `templates/front/plantas/planta_editor.html`,
`planta_viewer.html`, `planta_tv.html` (este último standalone, não estende
`base.html`). Mudar o schema de dados em um exige revisar os outros dois.

## Regra inegociável: `layout_json`

O layout é `PlantaProjeto.layout` (JSONField: `{elements:[], connections:[]}`).
No template, **sempre**:
```html
{{ layout_json|json_script:"__layout_data" }}
<script>
const layout = JSON.parse(document.getElementById('__layout_data').textContent);
</script>
```
**Nunca `|safe`** — labels de elemento são texto livre digitado pelo usuário;
`|safe` aqui é XSS direto. `layout_json` no contexto da view deve ser o dict
Python puro, não `json.dumps()` (o filtro `json_script` já serializa).

## Schema

- **Elemento**: `{id, type, x, y, width, height, label, color, prtg_objid, item_id, ...}`.
  Tipos: `switch`, `router`, `ap`, `server`, `firewall`, `printer`, `storage`,
  `camera`, `forma`, `texto`. **Novo tipo de equipamento não é `type` novo** —
  formas planas usam `type="quadro"` + `shapeKind` (ver memória
  `editor_plantas_formas_escala`); só criar `type` novo para equipamento real
  com ícone próprio.
- **Conexão**: `{id, from, to, type, color, strokeWidth, dash, arrow}` —
  propriedades por conexão sobrepõem o `CN_CFG` global.
- **Bordas de forma**: `el.borderColor`, `el.borderWidth`, `el.borderStyle`
  (por elemento, não global).
- **Escala**: `canvas.scale.pxPerMeter` — usar isso pra converter, nunca
  hardcodar pixel↔metro.

## Status PRTG no canvas

`elStKey(el)` → `ST_MAP[parseInt(dev.status)]`. `dev.status` já é o status
**efetivo** (pior entre device e ping) devolvido pelo servidor — não
recalcular no cliente. Se adicionar um código de status novo, **`ST_MAP`
precisa cobrir os 12 códigos PRTG (1–12)**; código `10` (unusual) mapeia pra
`"warning"`. Esquecer um código faz o elemento cair em estado indefinido no
desenho.

## Viewer — refresh e toasts

`refreshStatus()` chama `/plantas/prtg/status/` a cada 30s.
`detectarMudancas(prev, newMap)` só dispara toast a partir da **segunda**
carga (flag `_firstLoad`) — se o toast está disparando na abertura da página,
essa flag foi quebrada.

## HiDPI

Editor/viewer escalam o backing store por `devicePixelRatio`
(`setTransform(DPR)` sobre VW/VH lógico) para nitidez em telas retina. **O
modo TV ainda não tem esse tratamento** (ver memória `canvas_hidpi_plantas`)
— se for mexer em nitidez do TV, é trabalho novo, não replicar um fix
existente.

## Modo TV — 3D isométrico

`isoProj(wx, wy, wz)` projeta `azimuth` (órbita horizontal) + `pitch`
(elevação, 30° padrão) + `pan3D` (offset screen-space). `EL_H3D` define altura
3D por tipo de equipamento; `CABLE_Z = 5` é a altura de roteamento de
cabos/conexões acima do piso. `connPath3D(cn)` projeta os waypoints
ortogonais reais (mesmo roteamento do 2D) via `isoProj` — start/end na altura
do elemento, intermediários em `CABLE_Z`. Novo tipo de elemento 3D exige
entrada em `EL_H3D` e, se for forma (`quadro`/`circulo`/`linha`), tratamento
em `drawForma3D`.

## Números localizados (pt-BR) quebrando parseFloat

Template pt-BR localiza floats (vírgula decimal) — passar coordenadas/números
para JS sempre via `json_script` (não interpolação direta no HTML) ou filtro
`|unlocalize`, senão `parseFloat("12,5")` quebra silenciosamente
(ver memória `locale_floats_js`).

## Checklist final
- [ ] `layout_json` nunca passa por `|safe`
- [ ] Mudança de schema replicada nos 3 templates (editor/viewer/tv) se aplicável
- [ ] `ST_MAP` cobre os 12 códigos PRTG
- [ ] Números para JS via `json_script`, não interpolados direto
- [ ] `dev.status` usado como veio do servidor (já é o efetivo)
