"""
Cria o painel de portas dos equipamentos de rede, em lote.

Sem este comando, o Mapa de Rede nasce inútil: são 89 switches e 203 access
points, e cadastrar 24 portas de cada um a mão é trabalho que simplesmente não
acontece. O palpite de modelo sai de `marca`/`modelo` do inventário
(`rede_topologia_service.modelo_sugerido`), que acerta a maioria dos Cisco —
e é só palpite: quem não bater fica listado para ajuste na tela.

**O comando é ADITIVO e idempotente.** `gerar_portas` cria só as posições que
faltam e nunca toca em porta com cabo (ver regra 49): rodar duas vezes não
duplica nada, e rodar depois de o TI mapear um rack não apaga o trabalho dele.
É por isso que ele pode ser rodado sem medo mesmo com o mapa já em uso.

Por que é management command e não `manage.py shell <`: ele ESCREVE no banco, e
a regra 22 do CLAUDE.md existe porque um ensaio pelo shell interativo já gravou
47 datas em produção por engano. Aqui há `--dry-run`, contagem antes e depois, e
uma transação por equipamento.

    python manage.py gerar_portas_rede --dry-run
    python manage.py gerar_portas_rede
    python manage.py gerar_portas_rede --localidade 22 --modelo cisco_c1000_24p
    python manage.py gerar_portas_rede --classes switch,ap --terminais
"""
from collections import Counter

from django.core.management.base import BaseCommand, CommandError

from ProjetoEstoque.models import Item, RedePorta
from services import rede_topologia_service as rede

# Classes que ganham painel por padrão. Terminal (AP, câmera, desktop) fica de
# fora: a porta dele é uma só e o enlace pode ser registrado pelo
# `destino_item`, sem modelar nada. `--terminais` inclui esses com 1 porta.
_CLASSES_PADRAO = ("switch", "wlc", "patch", "nvr")
_MODELO_TERMINAL = "terminal_1"


class Command(BaseCommand):
    help = "Cria em lote as portas dos equipamentos de rede, pelo modelo do catálogo."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Só mostra o que faria. Nada é gravado.",
        )
        parser.add_argument(
            "--localidade", type=int, default=None,
            help="Restringe a uma localidade (pk).",
        )
        parser.add_argument(
            "--modelo", default=None,
            help="Força um modelo do catálogo para todos (ignora o palpite).",
        )
        parser.add_argument(
            "--padrao", default="generico_24",
            help="Modelo usado quando o palpite não acha nada. Padrão: generico_24.",
        )
        parser.add_argument(
            "--classes", default=",".join(_CLASSES_PADRAO),
            help=f"Classes a cobrir, separadas por vírgula. Padrão: {','.join(_CLASSES_PADRAO)}.",
        )
        parser.add_argument(
            "--terminais", action="store_true",
            help="Inclui equipamentos de ponta (AP, câmera, desktop) com 1 porta.",
        )

    def handle(self, *args, **opcoes):
        forcado = opcoes["modelo"]
        padrao = opcoes["padrao"]
        for chave in (forcado, padrao):
            if chave and chave not in rede.MODELOS:
                raise CommandError(
                    f"Modelo '{chave}' não está no catálogo. "
                    f"Disponíveis: {', '.join(sorted(rede.MODELOS))}"
                )

        classes = {c.strip() for c in opcoes["classes"].split(",") if c.strip()}
        if opcoes["terminais"]:
            classes |= {"ap", "antena", "camera", "desktop", "notebook",
                        "impressora", "telefone", "radio", "starlink",
                        "firewall", "roteador"}

        qs = Item.objects.select_related("subtipo", "localidade").filter(status="ativo")
        if opcoes["localidade"]:
            qs = qs.filter(localidade_id=opcoes["localidade"])

        alvos = []
        for item in qs.order_by("localidade__local", "nome"):
            classe, _ = rede.classe_de(item.subtipo.nome if item.subtipo else "")
            if classe not in classes:
                continue
            if classe in ("ap", "antena", "camera", "desktop", "notebook",
                          "impressora", "telefone"):
                modelo = forcado or _MODELO_TERMINAL
            else:
                modelo = forcado or rede.modelo_sugerido(item) or padrao
            alvos.append((item, classe, modelo))

        if not alvos:
            self.stdout.write(self.style.WARNING(
                "Nenhum equipamento no recorte. Confira --localidade e --classes."
            ))
            return

        # Medir ANTES é o que permite conferir que a operação fez o que disse —
        # mesma disciplina da regra 22.
        portas_antes = RedePorta.objects.count()

        por_modelo = Counter(m for _, _, m in alvos)
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n{len(alvos)} equipamento(s) no recorte · {portas_antes} porta(s) já cadastradas"
        ))
        for modelo, n in por_modelo.most_common():
            m = rede.MODELOS[modelo]
            self.stdout.write(
                f"  {n:>4} × {m['nome']}  ({m['acessos']}+{m['uplinks']} portas)"
            )

        if opcoes["dry_run"]:
            sem_palpite = [i.nome for i, _, m in alvos if m == padrao and not forcado]
            if sem_palpite:
                self.stdout.write(self.style.WARNING(
                    f"\n{len(sem_palpite)} equipamento(s) cairiam no modelo padrão "
                    f"({padrao}) porque marca/modelo não dizem nada:"
                ))
                for nome in sem_palpite[:15]:
                    self.stdout.write(f"  · {nome}")
                if len(sem_palpite) > 15:
                    self.stdout.write(f"  … e outros {len(sem_palpite) - 15}")
            self.stdout.write(self.style.NOTICE("\nEnsaio — nada foi gravado."))
            return

        criadas_total = 0
        tocados = 0
        falhas = []
        for item, classe, modelo in alvos:
            try:
                disp = rede.dispositivo_de(item)
                criadas = rede.gerar_portas(disp, modelo)
            except Exception as exc:                      # noqa: BLE001
                # Um equipamento problemático não pode derrubar os outros 300:
                # a falha é acumulada e mostrada no fim (mesma postura da
                # devolução em massa de licenças).
                falhas.append(f"{item.nome}: {exc}")
                continue
            if criadas:
                tocados += 1
                criadas_total += criadas

        portas_depois = RedePorta.objects.count()
        self.stdout.write(self.style.SUCCESS(
            f"\n{criadas_total} porta(s) criada(s) em {tocados} equipamento(s)."
        ))
        self.stdout.write(
            f"Portas no banco: {portas_antes} → {portas_depois} "
            f"(diferença {portas_depois - portas_antes})"
        )
        if falhas:
            self.stdout.write(self.style.ERROR(f"\n{len(falhas)} falha(s):"))
            for linha in falhas[:20]:
                self.stdout.write(f"  · {linha}")

        self.stdout.write(self.style.NOTICE(
            "\nPorta com cabo foi preservada. O próximo passo é ligar os cabos em "
            "/plantas/rede/ — modo cabo (tecla C) ou o botão 'Ligar cabo' na porta."
        ))
