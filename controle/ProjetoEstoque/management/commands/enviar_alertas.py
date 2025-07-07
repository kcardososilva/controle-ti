import smtplib
from email.message import EmailMessage
from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from ProjetoEstoque.models import Equipamento, Preventiva  # ajuste se o nome do app for diferente
from django.conf import settings

class Command(BaseCommand):
    help = "Envia alertas de preventiva e equipamentos críticos via SMTP"

    def handle(self, *args, **kwargs):
        hoje = timezone.now().date()

        # Preventivas que vencem em até 3 dias
        preventivas_criticas = Preventiva.objects.filter(
            data_proxima__lte=hoje + timedelta(days=10)
        ).select_related('equipamento')


        # Access Points com status backup e quantidade baixa
        aps_criticos = Equipamento.objects.filter(
            subtipo__nome__icontains='access-point',
            status='backup',
            
        )

        switches_criticos = Equipamento.objects.filter(
            subtipo__nome__icontains='Switches',
            status='backup',
        )

        if preventivas_criticas.exists() or switches_criticos.exists() or aps_criticos.exists():
            mensagem = "🚨 ALERTA AUTOMÁTICO DE EQUIPAMENTOS 🚨\n\n"

            # Preventivas próximas
            if preventivas_criticas.exists():
                mensagem += f"🛠️ Preventivas próximas do vencimento ({preventivas_criticas.count()}):\n"
                for p in preventivas_criticas:
                    mensagem += f"- {p.equipamento.nome} | Próxima: {p.data_proxima.strftime('%d/%m/%Y')}\n"

            # Equipamentos em backup críticos
            if switches_criticos.exists():
                if switches_criticos.count() <4:
                    mensagem += f"\nQuantidade de Switch em Backup ({switches_criticos.count()}):\n"
                    for eq in switches_criticos:
                        mensagem += f"- {eq.nome}\n"
                else:
                    mensagem += f"\nOs switchs estão com estoque acima de 4 unidades\n\n"

            # Access Points críticos
            if aps_criticos.exists():
                if aps_criticos.count() < 5:
                    mensagem += f"\nQuantidade de Access-Point com estoque crítico : {aps_criticos.count()} encontrados\n \n Acces-Point atual em estoque: \n"
                    for ap in aps_criticos:
                        mensagem += f"- {ap.nome}\n"
                else:
                    mensagem += f"\nOs Access-Point estão com estoque acima de 5 unidades\n\n"

            # Enviar e-mail via SMTP
            email = EmailMessage()
            email['Subject'] = '🔔 Alerta de Equipamentos - Santa Colomba'
            email['From'] = settings.EMAIL_HOST_USER
            email['To'] = settings.ALERTA_EMAIL
            email.set_content(mensagem)

            try:
                with smtplib.SMTP(settings.EMAIL_HOST, settings.EMAIL_PORT) as server:
                    server.starttls()
                    server.login(settings.EMAIL_HOST_USER, settings.EMAIL_HOST_PASSWORD)
                    server.send_message(email)

                self.stdout.write(self.style.SUCCESS("✅ Alerta enviado com sucesso via SMTP."))

            except Exception as e:
                self.stdout.write(self.style.ERROR(f"❌ Erro ao enviar e-mail: {e}"))

        else:
            self.stdout.write("Nenhum alerta necessário hoje.")
