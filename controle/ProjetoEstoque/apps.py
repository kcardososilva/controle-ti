from django.apps import AppConfig


class ProjetoestoqueConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'ProjetoEstoque'

    def ready(self):
        from django.db.backends.signals import connection_created

        def ativar_wal(sender, connection, **kwargs):
            if connection.vendor == 'sqlite':
                with connection.cursor() as cursor:
                    cursor.execute('PRAGMA journal_mode=WAL;')
                    cursor.execute('PRAGMA synchronous=NORMAL;')

        connection_created.connect(ativar_wal)
