"""
sync_ninja — Sincroniza dispositivos NinjaOne RMM com o banco de dados.

Uso:
    python manage.py sync_ninja                   # Sync completo + snapshot
    python manage.py sync_ninja --snapshot-only   # Apenas snapshot (mais rápido)
    python manage.py sync_ninja --purge-old 30    # Remove snapshots com mais de 30 dias

Agendamento no Windows Task Scheduler:
    Sync completo a cada hora:
        python manage.py sync_ninja
    Snapshot a cada 15 minutos (relatorio de uso mais granular):
        python manage.py sync_ninja --snapshot-only
"""

from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Sincroniza dispositivos NinjaOne RMM e grava snapshots para relatório de uso"

    def add_arguments(self, parser):
        parser.add_argument(
            "--snapshot-only",
            action="store_true",
            help="Apenas grava snapshot do estado atual sem buscar dados da API",
        )
        parser.add_argument(
            "--purge-old",
            type=int,
            metavar="DIAS",
            help="Remove snapshots com mais de N dias para liberar espaço",
        )
        parser.add_argument(
            "--match-serials",
            action="store_true",
            help="Re-executa apenas o vínculo de serial -> Item, sem chamar a API",
        )

    def handle(self, *args, **options):
        from services.ninja_service import (
            is_configured, sync_devices, _take_snapshot, get_live_status,
        )

        # ── Limpeza de snapshots antigos ──────────────────────────────────
        if options.get("purge_old"):
            dias = options["purge_old"]
            self._purge_old_snapshots(dias)
            return

        # ── Re-vínculo de seriais sem chamar API ──────────────────────────
        if options.get("match_serials"):
            self._rematch_serials()
            return

        # ── Verifica configuração ─────────────────────────────────────────
        if not is_configured():
            self.stderr.write(
                self.style.ERROR(
                    "NinjaOne não configurado. "
                    "Defina NINJA_BASE_URL, NINJA_CLIENT_ID e NINJA_CLIENT_SECRET no .env"
                )
            )
            return

        start = timezone.now()

        # ── Snapshot apenas ───────────────────────────────────────────────
        if options.get("snapshot_only"):
            from ProjetoEstoque.models import NinjaDevice
            if not NinjaDevice.objects.exists():
                self.stderr.write(self.style.WARNING(
                    "Nenhum dispositivo no banco. Execute sync_ninja sem --snapshot-only primeiro."
                ))
                return
            count = _take_snapshot()
            elapsed = (timezone.now() - start).total_seconds()
            self.stdout.write(self.style.SUCCESS(
                f"[NinjaOne] Snapshot registrado: {count} dispositivo(s) em {elapsed:.1f}s"
            ))
            return

        # ── Sync completo ─────────────────────────────────────────────────
        self.stdout.write("[NinjaOne] Iniciando sincronização completa...")
        result = sync_devices()
        elapsed = (timezone.now() - start).total_seconds()

        if result.get("error"):
            self.stderr.write(self.style.ERROR(
                "[NinjaOne] Erro ao sincronizar. Verifique os logs e as credenciais no .env."
            ))
            return

        self.stdout.write(self.style.SUCCESS(
            f"[NinjaOne] Sincronização concluída em {elapsed:.1f}s\n"
            f"  Dispositivos sincronizados : {result['synced']}\n"
            f"  Vinculados ao estoque (S/N) : {result['matched']}\n"
            f"  Online agora               : {result['online']}"
        ))

    def _purge_old_snapshots(self, dias: int):
        from ProjetoEstoque.models import NinjaDeviceSnapshot
        from django.utils import timezone
        import datetime

        corte = timezone.now() - datetime.timedelta(days=dias)
        count, _ = NinjaDeviceSnapshot.objects.filter(timestamp__lt=corte).delete()
        self.stdout.write(self.style.SUCCESS(
            f"[NinjaOne] {count} snapshot(s) com mais de {dias} dias removidos."
        ))

    def _rematch_serials(self):
        """Re-tenta vincular NinjaDevice -> Item por serial sem chamar a API."""
        from ProjetoEstoque.models import NinjaDevice, Item

        updated = 0
        for dev in NinjaDevice.objects.filter(serial_number__gt=""):
            item = Item.objects.filter(numero_serie__iexact=dev.serial_number).first()
            if item and dev.item_id != item.pk:
                dev.item = item
                dev.save(update_fields=["item"])
                updated += 1
            elif not item and dev.item_id:
                dev.item = None
                dev.save(update_fields=["item"])

        self.stdout.write(self.style.SUCCESS(
            f"[NinjaOne] Re-vínculo: {updated} dispositivo(s) atualizados."
        ))
