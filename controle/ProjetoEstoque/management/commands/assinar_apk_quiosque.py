"""
Registra a versão (version_code/version_name + sha256) do .apk do Quiosque
atualmente publicado em KIOSK_APK_DIR, habilitando a auto-atualização em campo
dos aparelhos já matriculados (Device Owner). A autenticidade da instalação em
si é garantida pelo próprio Android (mesma keystore de assinatura do app já
instalado) — este comando só calcula o sha256 para o app detectar download
incompleto/corrompido antes de instalar.

Necessário só para quem substitui o .apk copiando manualmente em KIOSK_APK_DIR;
o upload pela tela de Matrículas (`/quiosque/matriculas/`) já faz isso sozinho
quando o version_code é informado no formulário de envio.

Rodar toda vez que o .apk publicado for substituído por uma build nova (fora do
upload da tela). Sem isso, os aparelhos em campo continuam vendo a versão
anterior como "a mais recente" — não quebra nada, só não dispara a
auto-atualização.

Uso:
    python manage.py assinar_apk_quiosque <version_code> <version_name>
    python manage.py assinar_apk_quiosque 9 1.6.0
"""
from django.core.management.base import BaseCommand, CommandError

from services import quiosque_service as qs


class Command(BaseCommand):
    help = "Registra a versão do .apk atual do Quiosque (KIOSK_APK_DIR) para habilitar auto-atualização."

    def add_arguments(self, parser):
        parser.add_argument("version_code", type=int, help="versionCode do .apk (inteiro, sempre crescente)")
        parser.add_argument("version_name", type=str, help='versionName do .apk (ex.: "1.6.0")')

    def handle(self, *args, **options):
        try:
            info = qs.registrar_versao_apk_atual(
                version_code=options["version_code"],
                version_name=options["version_name"],
            )
        except ValueError as exc:
            raise CommandError(str(exc))

        self.stdout.write(self.style.SUCCESS(
            f"OK — '{info['apk_nome']}' assinado: version_code={info['version_code']} "
            f"version_name={info['version_name']} sha256={info['sha256'][:16]}…"
        ))
