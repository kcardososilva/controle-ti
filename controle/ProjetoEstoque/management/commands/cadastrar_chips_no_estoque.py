"""
Cadastra no estoque de TI os chips de conectividade que ainda não têm item — com os dados e o padrão dos chips que o TI
já cadastrou (regra 66).

    python manage.py cadastrar_chips_no_estoque                        # ENSAIO: mostra o padrão e o que seria criado; nada é gravado
    python manage.py cadastrar_chips_no_estoque --executar --usuario <login>
    python manage.py cadastrar_chips_no_estoque --executar --usuario <login> --limite 5       # um teste com poucos
    python manage.py cadastrar_chips_no_estoque --executar --usuario <login> --conciliar      # liga antes os pares de mesmo ICCID

O QUE É CRIADO. Um `Item` do tipo "É chip (SIM)" por chip, com o número de série igual ao ICCID, ligado ao chip de conectividade
(`M2MChip.item_ti`, origem "item_do_chip"). Os demais campos saem do `padrao_de_estoque`: o valor MAIS COMUM entre os itens de chip
que já existem (nome, marca, modelo, valor, categoria, fornecedor, NF, data de compra). O comando imprime o padrão com a
consistência de cada campo ("50 de 50") antes de gravar qualquer coisa.

O QUE NÃO É CRIADO — e é o ponto: nenhum vínculo com colaborador, nenhuma movimentação, nenhum termo. Os chips que já estão no
estoque foram ENTREGUES a uma pessoa (uma movimentação de entrega com termo assinado os levou para o centro de custo e a
localidade dela e os deixou "Ativo"). Os que faltam ainda não foram entregues; então o padrão de centro de custo, localidade e
status é o de ANTES da entrega (a origem daquela movimentação e o primeiro status do histórico): em estoque, em Backup. Quando
forem entregues, a entrega normal (movimentação + termo) faz o resto.

SEGURANÇA. Sem `--executar` nada é gravado. Com ele, `--usuario` é obrigatório (é o autor do cadastro). Cada chip é uma
transação: um que falha não desfaz os outros, e rodar de novo cria só os que ainda faltam (é idempotente). Nunca altera um item
existente. Chip sem ICCID e chip com ICCID repetido (ambíguo) são pulados e listados. Como `manage.py shell < script` não serve
para escrever no banco (regra 22), isto é um comando.

SÓ CARACTERES CP1252 na saída (console do Windows).
"""

from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from ProjetoEstoque.models import CentroCusto, Categoria, Fornecedor, Localidade, StatusItemChoices
from services import chip_estoque_ti_service as estoque

OBSERVACAO_PADRAO = "Cadastrado a partir do chip de conectividade (comando cadastrar_chips_no_estoque); ainda nao entregue."


def _rotulo(modelo, pk, campo_nome) -> str:
    if pk is None:
        return "(vazio)"
    objeto = modelo.objects.filter(pk=pk).first()
    return f"{pk} - {campo_nome(objeto)}" if objeto else f"{pk} - (nao encontrado)"


class Command(BaseCommand):
    help = "Cadastra no estoque de TI os chips sem item, com o padrao dos chips ja cadastrados (sem vinculo com colaborador)."

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Sob pythonw (tarefa agendada) stdout/stderr podem ser None; o comando também roda do console.
        import io
        import sys
        for nome in ("stdout", "stderr"):
            if getattr(sys, nome) is None:
                setattr(sys, nome, io.StringIO())

    def add_arguments(self, parser):
        parser.add_argument("--executar", action="store_true", help="Grava de verdade. Sem isto e apenas um ensaio.")
        parser.add_argument("--usuario", help="Login do autor do cadastro (obrigatorio com --executar).")
        parser.add_argument("--limite", type=int, default=0, help="Cadastra so os N primeiros (um teste). 0 = todos.")
        parser.add_argument("--conciliar", action="store_true", help="Antes, liga os pares 1 x 1 de mesmo ICCID (os chips que ja tem item).")
        parser.add_argument("--valor", help="Troca o valor unitario do padrao (ex.: 15,00).")
        parser.add_argument("--nf", help="Numero da nota fiscal do lote (o padrao e vazio).")
        parser.add_argument("--data-compra", dest="data_compra", help="Data de compra do lote, AAAA-MM-DD (o padrao e vazio).")
        parser.add_argument("--fornecedor", type=int, help="Id do fornecedor do lote (o padrao e vazio).")
        parser.add_argument("--status", choices=[v for v in estoque.STATUS_DE_CADASTRO], help="Troca o status inicial do padrao (Backup).")

    # ── saida ──
    def _linha(self, texto="", estilo=None):
        self.stdout.write(estilo(texto) if estilo else texto)

    def _padrao(self, padrao: dict, dados: dict) -> None:
        cons = padrao["consistencia"]
        n = padrao["amostra"]
        self._linha(f"Padrao lido de {n} chip{'s' if n != 1 else ''} ja cadastrado{'s' if n != 1 else ''} no estoque de TI:")
        linhas = [
            ("Nome", dados["nome"], cons.get("nome")),
            ("Marca", dados["marca"], cons.get("marca")),
            ("Modelo", dados["modelo"], cons.get("modelo")),
            ("Valor unitario", f"R$ {Decimal(dados['valor']):.2f}" if dados["valor"] not in (None, "") else "(vazio)", cons.get("valor")),
            ("Categoria", _rotulo(Categoria, dados["categoria"], lambda o: o.nome), cons.get("categoria")),
            ("Centro de custo", _rotulo(CentroCusto, dados["centro_custo"], lambda o: f"{o.numero} {o.departamento}"), cons.get("centro_custo")),
            ("Localidade", _rotulo(Localidade, dados["localidade"], lambda o: o.local), cons.get("localidade")),
            ("Status inicial", dict(StatusItemChoices.choices).get(dados["status"], dados["status"]), cons.get("status")),
            ("Fornecedor", _rotulo(Fornecedor, dados["fornecedor"], lambda o: o.nome), cons.get("fornecedor")),
            ("Nota fiscal", dados["numero_nf"] or "(vazio)", cons.get("numero_nf")),
            ("Data de compra", dados["data_compra"] or "(vazio)", cons.get("data_compra")),
        ]
        for rotulo, valor, c in linhas:
            quantos = f"  [{c[1]} de {n}]" if c else ""
            self._linha(f"  {rotulo:<16} {valor}{quantos}")
        self._linha("  Colaborador       nenhum (sem vinculo, sem movimentacao, sem termo): ainda nao entregue")
        self._linha("  Observacao        " + dados["observacoes"])

    def handle(self, *args, **opcoes):
        executar = opcoes["executar"]
        usuario = None
        if executar:
            if not opcoes["usuario"]:
                raise CommandError("Informe --usuario <login>: o cadastro precisa de um autor.")
            usuario = get_user_model().objects.filter(username=opcoes["usuario"]).first()
            if usuario is None:
                raise CommandError(f"Usuario '{opcoes['usuario']}' nao encontrado.")

        if not estoque.subtipos_de_chip():
            raise CommandError("Nenhum tipo esta marcado como 'E chip (SIM)': marque o tipo dos chips no cadastro de tipos.")
        padrao = estoque.padrao_de_estoque()
        if not padrao["amostra"]:
            raise CommandError(
                "Nenhum chip esta cadastrado no estoque de TI ainda: nao ha padrao para seguir. "
                "Use a tela de conciliacao (cadastro em lote) e informe os dados."
            )

        dados = {campo: padrao[campo] for campo in estoque.CAMPOS_DO_PADRAO}
        dados["observacoes"] = OBSERVACAO_PADRAO
        if opcoes["valor"] is not None:
            dados["valor"] = opcoes["valor"]
        if opcoes["nf"] is not None:
            dados["numero_nf"] = opcoes["nf"]
        if opcoes["data_compra"]:
            try:
                dados["data_compra"] = date.fromisoformat(opcoes["data_compra"])
            except ValueError:
                raise CommandError("--data-compra deve ser AAAA-MM-DD.") from None
        if opcoes["fornecedor"] is not None:
            if not Fornecedor.objects.filter(pk=opcoes["fornecedor"]).exists():
                raise CommandError(f"Fornecedor {opcoes['fornecedor']} nao encontrado.")
            dados["fornecedor"] = opcoes["fornecedor"]
        if opcoes["status"]:
            dados["status"] = opcoes["status"]
        try:
            if dados["valor"] not in (None, ""):
                Decimal(str(dados["valor"]).replace(",", "."))
        except InvalidOperation:
            raise CommandError("--valor invalido (ex.: 15,00).") from None

        self._linha()
        self._padrao(padrao, {**dados, "valor": str(dados["valor"]).replace(",", ".") if dados["valor"] not in (None, "") else None})

        # ── o que falta ──
        conc = estoque.conciliacao()
        if opcoes["conciliar"] and executar:
            ligados = estoque.conciliar_automaticamente(usuario)
            self._linha()
            self._linha(f"Conciliacao previa: {ligados.ligados} chip(s) ligado(s) ao item de mesmo ICCID.", self.style.SUCCESS)
            for chip, texto in ligados.falhas:
                self._linha(f"  {chip.nome}: {texto}", self.style.WARNING)
            conc = estoque.conciliacao()
        alvos = [c for c in conc.chips_sem_item if c.iccid]
        sem_iccid = [c for c in conc.chips_sem_item if not c.iccid]
        if opcoes["limite"] and opcoes["limite"] > 0:
            alvos = alvos[: opcoes["limite"]]
        em_uso = [c for c in alvos if c.situacao_uso == "vinculado"]
        valor_unitario = Decimal(str(dados["valor"]).replace(",", ".")) if dados["valor"] not in (None, "") else Decimal("0")

        self._linha()
        self._linha(f"Chips de conectividade sem item no estoque: {len(conc.chips_sem_item)}")
        self._linha(f"  a cadastrar agora ........ {len(alvos)}" + (f"  (limite {opcoes['limite']})" if opcoes["limite"] else ""))
        self._linha(f"  valor de aquisicao ....... R$ {valor_unitario * len(alvos):.2f}")
        if conc.automaticos and not opcoes["conciliar"]:
            self._linha(f"  pares de mesmo ICCID ..... {len(conc.automaticos)} (ja existem no estoque; ligue na tela de conciliacao ou use --conciliar)")
        if conc.ambiguos:
            self._linha(f"  ambiguos (ICCID repetido)  {len(conc.ambiguos)} (nao sao tocados)", self.style.WARNING)
        if sem_iccid:
            self._linha(f"  sem ICCID (pulados) ...... {len(sem_iccid)}: " + ", ".join(c.nome for c in sem_iccid[:5]), self.style.WARNING)
        if em_uso:
            self._linha(
                f"  em uso no Agro ........... {len(em_uso)} (vinculados a veiculo ou aparelho; entram em Backup e a conciliacao os aponta como divergencia)"
            )
        for chip in alvos[:5]:
            self._linha(f"    ex.: {chip.nome}  ICCID {chip.iccid}")
        if len(alvos) > 5:
            self._linha(f"    ... e mais {len(alvos) - 5}")

        if not alvos:
            self._linha()
            self._linha("Nada a cadastrar.", self.style.SUCCESS)
            return
        if not executar:
            self._linha()
            self._linha("ENSAIO: nada foi gravado.", self.style.WARNING)
            self._linha("Para cadastrar: python manage.py cadastrar_chips_no_estoque --executar --usuario <login>")
            return

        # ── grava (uma transacao por chip, dentro do service) ──
        self._linha()
        self._linha(f"Cadastrando {len(alvos)} chip(s) como {usuario.get_username()}...")
        resumo = estoque.cadastrar_itens_dos_chips(alvos, dados, usuario, categoria=dados["categoria"])
        self._linha(f"Cadastrados e ligados: {len(resumo.criados)}", self.style.SUCCESS)
        total = sum((i.valor or Decimal("0")) for _, i in resumo.criados)
        if resumo.criados:
            self._linha(f"  valor de aquisicao ....... R$ {total:.2f}")
        for chip, texto in resumo.falhas:
            self._linha(f"  NAO cadastrado: {chip.nome} - {texto}", self.style.ERROR)
        final = estoque.conciliacao()
        self._linha()
        self._linha(
            f"Situacao agora: {len(final.ligados)} ligado(s), {len(final.automaticos)} par(es) por ligar, "
            f"{len(final.chips_sem_item)} chip(s) sem item, {len(final.itens_sem_chip)} item(ns) sem chip."
        )
        if resumo.falhas:
            raise CommandError(f"{len(resumo.falhas)} chip(s) nao foram cadastrados (acima). Corrija e rode de novo: so os que faltam sao criados.")
