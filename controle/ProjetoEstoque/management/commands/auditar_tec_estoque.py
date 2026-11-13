"""
Auditoria do estoque tecnológico do Zelo Agro — o cache e o estado contra o razão.

Responde: o saldo de algum material por quantidade divergiu do razão (`TecMovimentacao`)? Algum
equipamento individual aparece como disponível num local onde o razão diz que ele não está (ou o
contrário)? Algum equipamento consta como instalado sem instalação ativa, ou tem instalação ativa sem
estar instalado, ou o razão discorda de quantas instalações há num destino? `TecSaldo` é CACHE e a
situação do equipamento é ESTADO; o razão é a verdade. Divergência faz a tela oferecer material que
não existe, ou esconder o que existe, ou mostrar um equipamento numa máquina onde ele não está.

Sai com código 1 se achar qualquer divergência — para ser agendado no Task Scheduler e FALHAR
visivelmente, mesma convenção de `auditar_licencas`.

**Só lê.** O reparo é `--reconciliar`, explícito: alinha o cache de MATERIAL ao razão e nunca toca no
razão nem nos equipamentos (divergência de equipamento indica um defeito a investigar, não um número a
regravar).

Uso:
  python manage.py auditar_tec_estoque
  python manage.py auditar_tec_estoque --reconciliar
"""
from django.core.management.base import BaseCommand

from services import tec_estoque_service as estoque
from services import tec_instalacao_service as instalacoes


class Command(BaseCommand):
    help = "Confere o estoque tecnológico do Agro contra o razão. Sai com 1 se houver divergência."

    def add_arguments(self, parser):
        parser.add_argument("--reconciliar", action="store_true", help="Alinha o cache de material ao razão (não toca no razão nem nos equipamentos).")

    def handle(self, *args, **opcoes):
        resultado = estoque.conferir_consistencia()
        for d in resultado["materiais"]:
            self.stdout.write(f"MATERIAL   {d['modelo']} @ {d['local']}: cache {d['cache']:f} x razao {d['razao']:f}")
        for d in resultado["equipamentos"]:
            self.stdout.write(f"EQUIPAMENTO {d['modelo']} @ {d['local']}: disponiveis {d['disponiveis']:f} x razao {d['razao']:f}")
        # Instalacao nao se reconcilia: estado e razao divergirem e um defeito, nao um numero a regravar.
        inst = instalacoes.conferir_instalacoes()
        for e in inst["instalado_sem_instalacao"]:
            self.stdout.write(f"INSTALADO SEM INSTALACAO ATIVA  {e.codigo} ({e.modelo})")
        for e in inst["instalacao_sem_instalado"]:
            self.stdout.write(f"INSTALACAO ATIVA, EQUIPAMENTO NAO INSTALADO  {e.codigo} ({e.modelo}): {e.get_situacao_display()}")
        for d in inst["razao"]:
            self.stdout.write(f"INSTALACOES x RAZAO  {d['modelo']} em {d['destino_tipo']} #{d['destino_id']}: instalacoes {d['instalacoes']:f} x razao {d['razao']:f}")
        if resultado["ok"] and inst["ok"]:
            self.stdout.write(self.style.SUCCESS("Estoque tecnologico consistente com o razao."))
            return
        if opcoes["reconciliar"] and resultado["materiais"]:
            corrigidos = estoque.reconciliar()
            self.stdout.write(f"{corrigidos} saldo(s) de material reconciliado(s).")
            resultado = estoque.conferir_consistencia()
            if resultado["ok"] and inst["ok"]:
                self.stdout.write(self.style.SUCCESS("Estoque tecnologico consistente com o razao."))
                return
        if opcoes["reconciliar"]:
            restantes = (
                len(resultado["equipamentos"]) + len(inst["instalado_sem_instalacao"])
                + len(inst["instalacao_sem_instalado"]) + len(inst["razao"])
            )
            self.stdout.write(self.style.ERROR(f"Restam {restantes} divergencia(s) de equipamento ou instalacao, que nao se reconciliam: investigar."))
        raise SystemExit(1)
