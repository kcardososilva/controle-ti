"""
Marca Item.pmb = "Sim" nos equipamentos vinculados a colaboradores dos centros
de custo Tabaco já classificados como PMB (CentroCusto.pmb = "Sim").

"PMB/Tabaco" = centros de custo cujo departamento contém "TABACO" E que já
estão marcados pmb=Sim no cadastro de Centro de Custo (nem todo CC Tabaco é
PMB — ex.: "FACILITIES - TABACO" e "PROGRAMAS CORPORATIVOS-TABACO" são
Tabaco mas NÃO PMB, e ficam de fora).

"Vinculado ao colaborador" usa a MESMA regra de posse já usada na tela de
detalhe do colaborador (`_itens_ativos_do_usuario`, em
ProjetoEstoque/views/usuarios.py) — para nunca divergir do que já aparece
na tela:
- Itens COMPARTILHADOS: vínculo ativo em ItemColaborador.
- Itens NÃO compartilhados: detentor derivado da última movimentação do
  item (ignora devolução/baixa, que não representam posse atual).

Uso:
    python manage.py atualizar_pmb_tabaco --dry-run   # apenas relatório
    python manage.py atualizar_pmb_tabaco             # aplica as alterações
"""
from django.core.management.base import BaseCommand

from ProjetoEstoque.models import CentroCusto, SimNaoChoices, Usuario
from ProjetoEstoque.views.usuarios import _itens_ativos_do_usuario


class Command(BaseCommand):
    help = 'Marca PMB = "Sim" nos itens vinculados a colaboradores dos centros de custo Tabaco/PMB.'

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Não grava nada — apenas mostra o que seria alterado.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        ccs = list(
            CentroCusto.objects.filter(departamento__icontains="tabaco", pmb=SimNaoChoices.SIM)
        )
        if not ccs:
            self.stdout.write(self.style.ERROR(
                "Nenhum centro de custo Tabaco com PMB = Sim encontrado. Nada a fazer."
            ))
            return

        self.stdout.write(self.style.NOTICE(f"{len(ccs)} centro(s) de custo PMB/Tabaco considerado(s):"))
        for cc in ccs:
            self.stdout.write(f"  - {cc.numero}/{cc.departamento}")

        usuarios = list(
            Usuario.objects.filter(centro_custo__in=ccs).select_related("centro_custo")
        )
        self.stdout.write(self.style.NOTICE(f"\n{len(usuarios)} colaborador(es) nesses centros de custo.\n"))

        # item.pk -> (item, [nomes dos colaboradores que o vinculam])
        itens_para_atualizar = {}
        for usuario in usuarios:
            for item in _itens_ativos_do_usuario(usuario):
                if item.pmb == SimNaoChoices.SIM:
                    continue
                entry = itens_para_atualizar.setdefault(item.pk, (item, []))
                entry[1].append(usuario.nome)

        if not itens_para_atualizar:
            self.stdout.write(self.style.SUCCESS(
                "Nada a atualizar — todos os itens vinculados já estão com PMB = Sim."
            ))
            return

        self.stdout.write(self.style.WARNING(
            f"{len(itens_para_atualizar)} item(ns) serão marcados PMB = Sim:"
        ))
        itens_ordenados = sorted(itens_para_atualizar.values(), key=lambda par: par[0].nome)
        for item, nomes in itens_ordenados:
            ns = item.numero_serie or "—"
            quem = nomes[0] if len(nomes) == 1 else f"{nomes[0]} (+{len(nomes) - 1})"
            self.stdout.write(
                f"  - {item.nome[:35]:35}  NS:{ns[:18]:18}  Colaborador: {quem}"
            )

        if dry_run:
            self.stdout.write(self.style.NOTICE("\n[dry-run] Nenhuma alteração gravada."))
            return

        atualizados = 0
        for item, _nomes in itens_ordenados:
            item.pmb = SimNaoChoices.SIM
            item.save(update_fields=["pmb", "updated_at"])
            atualizados += 1

        self.stdout.write(self.style.SUCCESS(f"\n{atualizados} item(ns) atualizado(s) para PMB = Sim."))
