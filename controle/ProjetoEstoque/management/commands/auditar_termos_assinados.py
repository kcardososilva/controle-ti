"""
Varredura de integridade dos termos assinados digitalmente.

A assinatura digital vale como prova porque o `pdf_sha256` gravado no ato
permite afirmar, depois, que o arquivo é o mesmo que o sistema emitiu. Essa
garantia só é útil se alguém CONFERIR — e até aqui a conferência existia apenas
em `/termos/assinaturas/verificar/`, uma tela que confere **um** termo, quando
alguém já tem motivo para desconfiar. Problema de arquivo não anuncia: ele é
descoberto no dia em que o documento é pedido, que é o pior dia para descobrir.

O risco concreto não é hipotético. O `MEDIA_ROOT` deste projeto fica dentro de
uma pasta sincronizada por OneDrive — o mesmo motivo que levou o `settings.py` a
oferecer `DJANGO_DB_PATH`/`DJANGO_LOG_DIR` fora dela. Sincronização que resolve
conflito criando "arquivo (2)", restauração parcial de backup e pasta movida
produzem exatamente dois estados: arquivo que SUMIU e arquivo que MUDOU. Os dois
são detectáveis aqui, e só aqui.

Três resultados possíveis por termo:

  OK        o SHA-256 recalculado bate com o gravado na assinatura;
  ALTERADO  o arquivo existe mas mudou — a prova não serve mais;
  AUSENTE   o arquivo não está no storage (perda).

Sai com código 1 se houver qualquer termo ALTERADO ou AUSENTE, para poder ser
agendado no Task Scheduler e falhar visivelmente (os outros comandos agendados
do projeto seguem a mesma convenção).

Uso:
  python manage.py auditar_termos_assinados
  python manage.py auditar_termos_assinados --so-problemas
  python manage.py auditar_termos_assinados --desde 2026-01-01
"""
import logging

from django.core.management.base import BaseCommand

logger = logging.getLogger("seguranca")


class Command(BaseCommand):
    help = "Reconfere o SHA-256 de todos os termos assinados e aponta arquivo alterado ou ausente."

    def add_arguments(self, parser):
        parser.add_argument(
            "--so-problemas",
            action="store_true",
            help="Lista apenas os termos alterados ou ausentes.",
        )
        parser.add_argument(
            "--desde",
            default="",
            help="Confere só os termos assinados a partir desta data (AAAA-MM-DD).",
        )

    def handle(self, *args, **opts):
        from services.termo_assinatura_service import verificar_integridade_assinados

        resultado = verificar_integridade_assinados(desde=opts["desde"] or None)

        total = resultado["total"]
        problemas = resultado["alterados"] + resultado["ausentes"]

        if not total:
            self.stdout.write(self.style.WARNING("Nenhum termo assinado para conferir."))
            return

        for linha in resultado["itens"]:
            if linha["situacao"] == "ok":
                if opts["so_problemas"]:
                    continue
                estilo, rotulo = self.style.SUCCESS, "OK      "
            elif linha["situacao"] == "alterado":
                estilo, rotulo = self.style.ERROR, "ALTERADO"
            else:
                estilo, rotulo = self.style.ERROR, "AUSENTE "

            self.stdout.write(estilo(
                f"{rotulo}  termo {linha['pk']:>5}  {linha['codigo']}  "
                f"{linha['assinado_em']}  {linha['colaborador']}"
            ))

        self.stdout.write("")
        resumo = (
            f"{total} termo(s) assinado(s) conferido(s): "
            f"{resultado['ok']} íntegro(s), "
            f"{resultado['alterados']} alterado(s), "
            f"{resultado['ausentes']} ausente(s)."
        )

        if problemas:
            self.stdout.write(self.style.ERROR(resumo))
            self.stdout.write(self.style.ERROR(
                "Termo alterado ou ausente não serve mais como prova. Recupere o "
                "arquivo do backup do MEDIA_ROOT da data da assinatura — o PDF NÃO "
                "pode ser reemitido, porque a reemissão produziria um documento "
                "novo (hash diferente) e não o que o colaborador assinou."
            ))
            # Vai para `logs/seguranca.log`: se o comando rodar agendado, a saída
            # no console se perde e este é o rastro que fica.
            logger.error(
                "Auditoria de termos assinados: %s alterado(s) e %s ausente(s) "
                "de %s conferido(s).",
                resultado["alterados"], resultado["ausentes"], total,
            )
            raise SystemExit(1)

        self.stdout.write(self.style.SUCCESS(resumo))
        logger.info(
            "Auditoria de termos assinados: %s termo(s) conferido(s), todos íntegros.",
            total,
        )
