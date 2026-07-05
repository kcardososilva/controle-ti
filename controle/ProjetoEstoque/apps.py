from django.apps import AppConfig


class ProjetoestoqueConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'ProjetoEstoque'

    def ready(self):
        from django.db.backends.signals import connection_created

        def ativar_wal(sender, connection, **kwargs):
            if connection.vendor == 'sqlite':
                with connection.cursor() as cursor:
                    # WAL: leitura e escrita em paralelo (escritas nao travam leitores).
                    cursor.execute('PRAGMA journal_mode=WAL;')
                    cursor.execute('PRAGMA synchronous=NORMAL;')
                    # Espera ate 20s por um lock em vez de falhar com "database is locked".
                    cursor.execute('PRAGMA busy_timeout=20000;')
                    # Tabelas temporarias/indices em memoria + mais cache de paginas (~20MB).
                    cursor.execute('PRAGMA temp_store=MEMORY;')
                    cursor.execute('PRAGMA cache_size=-20000;')

        connection_created.connect(ativar_wal)

        # Conecta os signals (histórico de locação por status do item)
        from . import signals  # noqa: F401

        # Conecta o monitoramento de segurança (ISO 27001 A.8.15/A.8.16):
        # login/logout/falha → RegistroSeguranca + alerta de acesso suspeito.
        from services import seguranca_service  # noqa: F401
