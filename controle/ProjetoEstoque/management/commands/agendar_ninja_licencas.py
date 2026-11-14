"""
Tarefa SEMANAL do aviso "licenças do NinjaOne presas a máquinas paradas" (backup, defeito, manutenção, pausado) no Windows
Task Scheduler. Mesmo agendamento do `agendar_computadores_parados` (a classe é a dele; aqui só mudam o nome da tarefa e o
que ela dispara), na mesma ideia: sem a tarefa a lista só sai quando alguém aperta "Enviar e-mail agora" na tela
Licenças a liberar do NinjaOne ou roda o comando à mão.

Uso:
    python manage.py agendar_ninja_licencas criar              # segunda, 08:00
    python manage.py agendar_ninja_licencas criar --dia QUA --hora 07:30
    python manage.py agendar_ninja_licencas listar
    python manage.py agendar_ninja_licencas remover
    python manage.py agendar_ninja_licencas executar           # dispara agora (teste)
"""
from .agendar_computadores_parados import Command as _AgendadorSemanal

NOME_TAREFA = "ControleEstoque_NinjaLicencas"


class Command(_AgendadorSemanal):
    help = "Gerencia a tarefa semanal do aviso de licenças do NinjaOne presas a máquinas paradas"

    nome_tarefa = NOME_TAREFA
    tipo_alerta = "ninja"
    nome_comando = "agendar_ninja_licencas"
    sem_dados = "nenhuma máquina parada tem licença do NinjaOne a remover"

    def _enviar(self) -> bool:
        from services.email_alertas import alerta_ninja_licencas
        return alerta_ninja_licencas()
