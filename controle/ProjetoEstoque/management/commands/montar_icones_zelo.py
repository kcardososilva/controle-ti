"""
Monta o sprite e o manifesto da biblioteca de ícones a partir da pasta `static/icones/zelo/svg/`.

    python manage.py montar_icones_zelo              # regrava sprite.svg e manifesto.json
    python manage.py montar_icones_zelo --verificar  # não grava; sai com código 1 se estiverem fora de sincronia

É o passo que falta quando alguém troca um `.svg` (por exemplo, a arte final exportada do Canva) ou acrescenta um ícone
ao catálogo: o arquivo individual é o ativo, o sprite é derivado dele. Depois de regravar, suba o `ASSETS_VERSION` e
rode `collectstatic` (o sprite é cacheado como qualquer estático). Ver `services/icone_service.py`.
"""

from django.core.management.base import BaseCommand, CommandError

from services import icone_service as icones


class Command(BaseCommand):
    help = "Monta static/icones/zelo/sprite.svg e manifesto.json a partir dos .svg da biblioteca."

    def add_arguments(self, parser):
        parser.add_argument("--verificar", action="store_true", help="Só confere; não grava nada.")

    def handle(self, *args, **opcoes):
        icones.limpar_cache()
        faltam = [i.id for i in icones.CATALOGO if not icones.formato_de(i.id)]
        orfaos = sorted(
            p.stem for p in icones.PASTA_SVG.glob("*.svg") if not icones.existe(p.stem)
        ) if icones.PASTA_SVG.is_dir() else []
        if faltam:
            self.stdout.write(self.style.WARNING(f"Sem arquivo ({len(faltam)}): {', '.join(faltam)}"))
        if orfaos:
            self.stdout.write(self.style.WARNING(f"Arquivo sem entrada no catálogo ({len(orfaos)}): {', '.join(orfaos)}"))

        if opcoes["verificar"]:
            if faltam or orfaos or not icones.sprite_esta_em_dia():
                raise CommandError("A biblioteca de ícones está fora de sincronia. Rode: python manage.py montar_icones_zelo")
            self.stdout.write(self.style.SUCCESS("Biblioteca de ícones em dia."))
            return

        icones.ARQUIVO_SPRITE.write_text(icones.montar_sprite(), encoding="utf-8", newline="\n")
        icones.ARQUIVO_MANIFESTO.write_text(icones.montar_manifesto_json(), encoding="utf-8", newline="\n")
        com_arquivo = sum(1 for i in icones.CATALOGO if icones.formato_de(i.id))
        self.stdout.write(self.style.SUCCESS(f"Sprite e manifesto gravados: {com_arquivo} ícones."))
