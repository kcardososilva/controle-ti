"""
Servidor de PRODUCAO — Sistema de Controle de TI (Santa Colomba).

Sobe a aplicacao com Waitress, um servidor WSGI real e estavel para Windows,
no lugar do `manage.py runserver` (que e apenas para desenvolvimento e
processa as requisicoes praticamente em fila).

Uso:
    python servir_producao.py

ou pelo atalho:  iniciar_servidor.bat

Variaveis de ambiente opcionais (com os padroes atuais):
    HOST     -> 0.0.0.0
    PORT     -> 65300
    THREADS  -> 4
"""
import os


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "controle.settings")

    from waitress import serve
    from controle.wsgi import application

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    threads = int(os.environ.get("THREADS", "4"))

    print("=" * 62)
    print("  Sistema de Controle de TI - Servidor de Producao (Waitress)")
    print(f"  Escutando em  http://{host}:{port}")
    print(f"  Threads: {threads}")
    print("  Pressione CTRL+C para encerrar.")
    print("=" * 62)

    serve(application, host=host, port=port, threads=threads)


if __name__ == "__main__":
    main()
