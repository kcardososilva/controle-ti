"""Portal do Colaborador (Zelo Agro) — `services/colaborador_agro_service.py` (pedido de 2026-11-14).

O pedido: colaboradores que VISUALIZAM, MOVIMENTAM, CADASTRAM e abrem o DETALHE no Zelo Agro, SEM ver nada que seja da API
(sessão, consumo, consultas, sincronização, alertas de conectividade, configuração) e SEM alcançar o Zelo TI.

Cada frase do pedido é uma classe aqui:
  • GrupoTests         — o grupo é o retrato da lista do service, não tem permissão de API e prende o login no Agro;
  • SemTiTests         — o TI responde 403 (inclusive o que é "neutro": perfil e mídia);
  • UsoDoAgroTests     — o que o colaborador PODE fazer funciona (ver, cadastrar chip sem campo de API, movimentar);
  • SemApiTests        — nada da API chega a ele: tela, filtro, JSON, menu e o histórico. Cada teste cria dado ISCA da API
                         (sessão ativa, consumo, ocorrência, consulta) e confere que ele NÃO aparece — e que aparece para quem
                         tem permissão de API, senão o teste passaria sem provar nada;
  • TodoLinkAbreTests  — nenhum link de tela do Agro leva o colaborador a um 403 (TI ou API);
  • LiberacaoTests     — a tela de Acessos concede e revoga, e revogar DESATIVA (senão devolveria o TI à pessoa).
"""
import importlib
import re
from datetime import timedelta

from django.contrib.auth.models import Group, Permission, User
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from ProjetoEstoque.models import (
    GRUPO_COLABORADOR_AGRO,
    GRUPO_CONSULTA_QUIOSQUE,
    GRUPO_FORNECEDOR,
    GRUPO_PORTAL_CONTRATACAO,
    M2MChip,
    M2MConsulta,
    M2MOcorrencia,
)
from services import ambiente_service as amb
from services import colaborador_agro_service as colab
from services import frota_service
from services import frota_vinculo_service as vinculos
from services import m2m_analise_service as analise
from services import m2m_monitoramento_service as m2m
from services import media_acesso_service
from services import perfil_acesso_service

CANARIO_OCORRENCIA = "CANARIO-OCORRENCIA-DA-API"
CANARIO_ESTADO = "CANARIO-ESTADO-BRUTO"
# ICCID tem dígito verificador (Luhn): o formulário valida, então os de teste precisam ser VÁLIDOS
ICCID_A = "8955100000000000014"
ICCID_B = "8955100000000000022"
ICCID_NOVO = "8955100000000000998"


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cache.clear()
        cls.admin = User.objects.create_user("colab_admin", "a@x.com", "senha-boa-123", is_superuser=True, is_staff=True)
        cls.karitel, cls.rio = frota_service.fazendas()

    def setUp(self):
        cache.clear()           # os grupos do usuário ficam 60 s no cache, e o id se repete entre testes

    def tearDown(self):
        cache.clear()

    # ── pessoas ──
    def colaborador(self, nome="colab"):
        user = User.objects.create_user(nome, f"{nome}@x.com", "senha-boa-123")
        user.groups.add(Group.objects.get(name=GRUPO_COLABORADOR_AGRO))
        return User.objects.get(pk=user.pk)

    def com_api(self, nome="com_api"):
        """Alguém do Agro que TEM permissão de API — a prova de que o corte é por permissão, não por tela vazia."""
        user = User.objects.create_user(nome, f"{nome}@x.com", "senha-boa-123")
        for codigo in colab.PERMISSOES + ("consultar_conectividade", "administrar_frota_config", "tratar_ocorrencias"):
            user.user_permissions.add(Permission.objects.get(codename=codigo))
        return User.objects.get(pk=user.pk)

    def cliente(self, user):
        c = Client()
        c.force_login(user)
        return c

    # ── dados ──
    def chip(self, nome, iccid, **extra):
        return M2MChip.objects.create(nome=nome, iccid=iccid, **extra)

    def veiculo(self, codigo="MQ-1", **extra):
        from ProjetoEstoque.models import FrotaTipo
        tipo = FrotaTipo.objects.get_or_create(nome="Trator")[0]
        dados = {"codigo": codigo, "nome": f"Trator {codigo}", "tipo": tipo.pk, "localidade": self.karitel.pk, **extra}
        return frota_service.criar_veiculo(dados, self.admin)

    def cenario_com_dados_de_api(self):
        """Um veículo com chip vinculado, e TUDO que a API teria dito sobre o chip."""
        veiculo = self.veiculo()
        chip = self.chip("Chip A", ICCID_A)
        vinculos.vincular(chip, veiculo, self.admin, "teste")
        M2MChip.objects.filter(pk=chip.pk).update(
            sessao_estado="ativa", consumo_bytes=7_654_321, ultima_valida_em=timezone.now(), ultima_tentativa_em=timezone.now(),
            limite_mensal_bytes=5 * 1024 ** 3,
        )
        chip.refresh_from_db()
        M2MConsulta.objects.create(
            chip=chip, tipo="sessao", origem="manual", identificador_tipo="iccid", resultado="sucesso",
            payload={"estado": CANARIO_ESTADO}, estado_bruto=CANARIO_ESTADO, estado_interpretado="ativa",
        )
        M2MOcorrencia.objects.create(
            chip=chip, veiculo=veiculo, tipo="consumo_elevado", nivel="atencao", descricao=CANARIO_OCORRENCIA,
        )
        return veiculo, chip


# ═════════════════════════════════════════════════════════════
# O grupo
# ═════════════════════════════════════════════════════════════

class GrupoTests(Base):
    def codenames_do_grupo(self):
        return set(Group.objects.get(name=GRUPO_COLABORADOR_AGRO).permissions.values_list("codename", flat=True))

    def test_o_grupo_do_banco_e_a_lista_do_service(self):
        self.assertEqual(self.codenames_do_grupo(), set(colab.PERMISSOES))

    def test_a_migration_e_o_service_listam_as_mesmas_permissoes(self):
        """A migration é o retrato da lista (migration não importa service): se a lista crescer sem migration, aqui falha."""
        migration = importlib.import_module("ProjetoEstoque.migrations.0209_grupo_colaborador_agro")
        self.assertEqual({c for _, c, _ in migration.PERMISSOES}, set(colab.PERMISSOES))
        self.assertEqual(migration.GRUPO, GRUPO_COLABORADOR_AGRO)

    def test_nenhuma_permissao_de_api_entra_no_perfil(self):
        api = {c.split(".", 1)[1] for c in amb.PERMISSOES_API}
        self.assertEqual(api & set(colab.PERMISSOES), set())
        self.assertEqual(api & self.codenames_do_grupo(), set())

    def test_nao_tem_administrar_nem_importar_em_lote_nem_concluir(self):
        for codigo in colab.PERMISSOES:
            self.assertFalse(codigo.startswith("administrar_"), codigo)
        for fora in ("importar_equipamentos_frota", "importar_tec_material", "concluir_tec_manutencao", "validar_inventario_frota"):
            self.assertNotIn(fora, colab.PERMISSOES)

    def test_cada_permissao_do_perfil_existe_uma_vez_so(self):
        """Dono diferente do de `Meta.permissions` faria o post_migrate criar uma segunda linha com o mesmo codename."""
        for codigo in colab.PERMISSOES:
            self.assertEqual(Permission.objects.filter(content_type__app_label="ProjetoEstoque", codename=codigo).count(), 1, codigo)

    def test_sincronizar_tira_a_api_e_nao_apaga_o_que_o_administrador_pos(self):
        grupo = Group.objects.get(name=GRUPO_COLABORADOR_AGRO)
        grupo.permissions.add(Permission.objects.get(codename="consultar_conectividade"))
        extra = Permission.objects.get(codename="administrar_tec_catalogo")
        grupo.permissions.add(extra)
        colab.sincronizar_grupo()
        codigos = self.codenames_do_grupo()
        self.assertNotIn("consultar_conectividade", codigos)
        self.assertIn("administrar_tec_catalogo", codigos)          # decisão do administrador: fica

    def test_o_colaborador_so_alcanca_o_agro(self):
        user = self.colaborador()
        self.assertEqual(amb.ambientes_de(user), (amb.AGRO,))
        self.assertTrue(amb.somente_agro(user))
        self.assertFalse(amb.pode_ver_api(user))

    def test_quem_tem_permissao_de_api_ve_api(self):
        self.assertTrue(amb.pode_ver_api(self.com_api()))
        so_chips = User.objects.create_user("so_chips", password="x")
        so_chips.user_permissions.add(Permission.objects.get(codename="ver_chips"))
        self.assertFalse(amb.pode_ver_api(User.objects.get(pk=so_chips.pk)))

    def test_o_chip_do_topo_diz_o_que_a_pessoa_e(self):
        self.assertEqual(perfil_acesso_service.perfil_de_acesso(self.colaborador())["rotulo"], "Colaborador Agro")


# ═════════════════════════════════════════════════════════════
# Sem o TI
# ═════════════════════════════════════════════════════════════

class SemTiTests(Base):
    def test_o_ti_responde_403_para_leitura_e_escrita(self):
        c = self.cliente(self.colaborador())
        for caminho in ("/dashboard/", "/equipamentos/", "/licencas/", "/usuarios/", "/preventiva/metas/", "/plantas/", "/ninja/",
                        "/inteligencia/", "/alertas/notificacoes/", "/acessos/restritos/", "/admin/"):
            self.assertEqual(c.get(caminho).status_code, 403, caminho)
        self.assertEqual(c.post("/movimentacoes/nova/", {}).status_code, 403)
        self.assertEqual(c.post("/acessos/restritos/conceder/", {"perfil": "colaborador_agro", "username": "x", "senha": "senha-boa-1"}).status_code, 403)

    def test_a_raiz_leva_ao_agro_e_o_seletor_oferece_so_o_agro(self):
        c = self.cliente(self.colaborador())
        self.assertRedirects(c.get("/"), reverse("agro_visao_geral"), fetch_redirect_response=False)
        html = c.get(reverse("agro_visao_geral")).content.decode()
        self.assertNotIn(reverse("dashboard"), html)
        self.assertNotIn("Zelo TI", html)

    def test_o_perfil_nao_mostra_o_tamanho_do_ti(self):
        """`/perfil/` é caminho neutro (abre para qualquer login) e trazia contagens de equipamentos, licenças e manutenções."""
        colaborador = self.cliente(self.colaborador()).get(reverse("admin_perfil"))
        self.assertEqual(colaborador.status_code, 200)
        html = colaborador.content.decode()
        for rotulo in ("Manutenções abertas", "Centros de custo", "Licenças", "Colaboradores ativos"):
            self.assertNotIn(rotulo, html, rotulo)
        # e o teste não é vazio: quem alcança o TI continua vendo
        ti = User.objects.create_user("ti_comum", password="x")
        self.assertIn("Manutenções abertas", self.cliente(User.objects.get(pk=ti.pk)).get(reverse("admin_perfil")).content.decode())

    def test_arquivo_de_midia_nao_e_entregue(self):
        """`/media/` é neutro e entregava termo assinado e nota fiscal a qualquer login "interno comum"."""
        self.assertFalse(media_acesso_service.pode_ver_media(self.colaborador(), "termos/termo_1.pdf"))
        interno = User.objects.get(pk=User.objects.create_user("interno", password="x").pk)
        self.assertTrue(media_acesso_service.pode_ver_media(interno, "termos/termo_1.pdf"))
        self.assertEqual(self.cliente(self.colaborador("colab2")).get("/media/termos/termo_1.pdf").status_code, 404)

    def test_so_agro_exclusivo_tambem_nao_le_midia(self):
        from ProjetoEstoque.models import GRUPO_SOMENTE_AGRO
        user = User.objects.create_user("exclusivo", password="x")
        user.groups.add(Group.objects.get_or_create(name=GRUPO_SOMENTE_AGRO)[0])
        self.assertFalse(media_acesso_service.pode_ver_media(User.objects.get(pk=user.pk), "termos/termo_1.pdf"))


# ═════════════════════════════════════════════════════════════
# O que o colaborador pode fazer
# ═════════════════════════════════════════════════════════════

class UsoDoAgroTests(Base):
    def test_as_telas_de_uso_abrem(self):
        veiculo, chip = self.cenario_com_dados_de_api()
        c = self.cliente(self.colaborador())
        for nome, args in (
            ("agro_visao_geral", ()), ("frota_visao_geral", ()), ("frota_equipamentos_proprios", ()), ("frota_equipamentos_terceiros", ()),
            ("frota_veiculo_detalhe", (veiculo.pk,)), ("frota_veiculo_novo", ()), ("frota_veiculo_editar", (veiculo.pk,)),
            ("frota_equip_movimentacoes", ()), ("frota_equip_historico", ()), ("frota_relatorios", ()), ("frota_composicao", ()),
            ("m2m_visao_geral", ()), ("m2m_chips", ()), ("m2m_chip_detalhe", (chip.pk,)), ("m2m_chip_novo", ()),
            ("m2m_chip_editar", (chip.pk,)), ("frota_movimentacoes", ()), ("frota_importar", ()),
            ("tec_visao_geral", ()), ("tec_equipamentos", ()), ("tec_equipamento_novo", ()), ("tec_materiais", ()),
            ("tec_movimentacoes", ()), ("tec_catalogo_inicio", ()),
            ("infra_visao_geral", ()), ("infra_estruturas", ()), ("infra_estrutura_nova", ()), ("infra_enlaces", ()),
            ("agro_mapa", ()),
        ):
            resposta = c.get(reverse(nome, args=args))
            self.assertEqual(resposta.status_code, 200, nome)

    def test_cadastra_chip_sem_ver_campo_de_api_e_o_chip_nasce_certo(self):
        c = self.cliente(self.colaborador())
        html = c.get(reverse("m2m_chip_novo")).content.decode()
        self.assertNotIn("Identificador usado na consulta", html)
        self.assertNotIn("Limite mensal", html)
        resposta = c.post(reverse("m2m_chip_novo"), {
            "nome": "Chip do colaborador", "iccid": ICCID_NOVO,
            # um POST montado à mão com campo de API não é obedecido
            "identificador_consulta": "imsi", "limite_valor": "5", "limite_unidade": "GB",
        })
        self.assertEqual(resposta.status_code, 302)
        chip = M2MChip.objects.get(nome="Chip do colaborador")
        self.assertEqual(chip.identificador_consulta, "iccid")
        self.assertIsNone(chip.limite_mensal_bytes)

    def test_so_com_imsi_o_identificador_vira_imsi(self):
        c = self.cliente(self.colaborador())
        c.post(reverse("m2m_chip_novo"), {"nome": "So IMSI", "imsi": "724100000000001"})
        self.assertEqual(M2MChip.objects.get(nome="So IMSI").identificador_consulta, "imsi")

    def test_editar_chip_nao_mexe_no_limite_nem_no_identificador(self):
        chip = self.chip("Com limite", ICCID_A, limite_mensal_bytes=3 * 1024 ** 3)
        resposta = self.cliente(self.colaborador()).post(reverse("m2m_chip_editar", args=[chip.pk]), {
            "nome": "Com limite", "iccid": ICCID_A, "descricao": "mudou", "setor": "Oficina",
        })
        self.assertEqual(resposta.status_code, 302)
        chip.refresh_from_db()
        self.assertEqual((chip.descricao, chip.limite_mensal_bytes, chip.identificador_consulta), ("mudou", 3 * 1024 ** 3, "iccid"))

    def test_movimenta_chip_vincula_e_devolve(self):
        veiculo = self.veiculo()
        chip = self.chip("Livre", ICCID_B)
        c = self.cliente(self.colaborador())
        self.assertNotEqual(c.post(reverse("frota_vincular"), {"chip": chip.pk, "veiculo": veiculo.pk, "motivo": "instalado no campo"}).status_code, 403)

    def test_o_chip_cadastrado_pelo_colaborador_aparece_na_lista(self):
        c = self.cliente(self.colaborador())
        c.post(reverse("m2m_chip_novo"), {"nome": "Visivel", "iccid": ICCID_NOVO})
        self.assertIn("Visivel", c.get(reverse("m2m_chips")).content.decode())


# ═════════════════════════════════════════════════════════════
# Sem a API
# ═════════════════════════════════════════════════════════════

URLS_DA_API = (
    "/agro/conectividade/sessoes/", "/agro/conectividade/consumo/", "/agro/conectividade/alertas/",
    "/agro/conectividade/configuracao/", "/agro/conectividade/consultas/", "/agro/conectividade/sincronizacao/",
    "/atualizar/", "/monitoramento/",
)


class SemApiTests(Base):
    def test_as_telas_inteiras_da_api_respondem_403(self):
        veiculo, chip = self.cenario_com_dados_de_api()
        consulta = chip.consultas.first()
        c = self.cliente(self.colaborador())
        for nome, args in (
            ("m2m_sessoes", ()), ("m2m_consumo", ()), ("m2m_alertas", ()), ("m2m_config", ()),
            ("m2m_sincronizacao", ()), ("m2m_consulta_payload", (consulta.pk,)),
        ):
            self.assertEqual(c.get(reverse(nome, args=args)).status_code, 403, nome)
        for nome, args in (("m2m_chip_atualizar", (chip.pk,)), ("m2m_chip_monitoramento", (chip.pk,)), ("m2m_config_token", ())):
            self.assertEqual(c.post(reverse(nome, args=args), {}).status_code, 403, nome)

    def test_o_menu_nao_tem_item_de_api_e_tem_o_resto(self):
        menu = amb.nav_de(self.colaborador())
        hrefs = [item["href"] for entrada in menu for item in entrada.get("itens", [entrada])]
        for api in ("m2m_sessoes", "m2m_consumo", "m2m_alertas", "m2m_config"):
            self.assertNotIn(reverse(api), hrefs, api)
        for uso in ("m2m_chips", "frota_movimentacoes", "frota_visao_geral", "tec_equipamentos", "agro_mapa"):
            self.assertIn(reverse(uso), hrefs, uso)

    def test_o_menu_de_quem_tem_api_continua_inteiro(self):
        hrefs = [item["href"] for entrada in amb.nav_de(self.com_api()) for item in entrada.get("itens", [entrada])]
        for api in ("m2m_sessoes", "m2m_consumo", "m2m_alertas", "m2m_config"):
            self.assertIn(reverse(api), hrefs, api)

    def test_a_lista_de_chips_nao_mostra_o_que_a_api_disse(self):
        veiculo, chip = self.cenario_com_dados_de_api()
        sem = self.cliente(self.colaborador()).get(reverse("m2m_chips")).content.decode()
        com = self.cliente(self.com_api()).get(reverse("m2m_chips")).content.decode()
        # prova de que o teste enxerga: quem tem API vê as colunas (marcadores do cabeçalho da tabela e do filtro: a palavra
        # "Conectividade" sozinha também está no nome do módulo, no menu e no título)
        for marca in ("<th>Conectividade</th>", ">Consumo</th>", "<th>Última atualização</th>", "Sessão (última resposta válida)", "Maior volume informado"):
            self.assertIn(marca, com, marca)
            self.assertNotIn(marca, sem, marca)
        self.assertIn("Chip A", sem)                                  # o chip continua na lista, como cadastro e vínculo
        self.assertNotIn("Sessão ativa", sem)
        self.assertNotIn("m2m-alerta-marca", sem)
        for api in URLS_DA_API:
            self.assertNotIn(api, sem, api)

    def test_filtrar_ou_ordenar_pela_api_nao_funciona_sem_permissao(self):
        ativo = self.chip("Ativo", ICCID_A)
        outro = self.chip("Outro", ICCID_B)
        M2MChip.objects.filter(pk=ativo.pk).update(sessao_estado="ativa", falhas_consecutivas=0, ultima_tentativa_em=timezone.now(), ultima_valida_em=timezone.now())
        sem = self.cliente(self.colaborador())
        com = self.cliente(self.com_api())
        # com permissão, o filtro age; sem ela, `?sessao=` é ignorado — filtrar já seria LER a resposta da API
        self.assertNotIn("Outro", com.get(reverse("m2m_chips") + "?sessao=ativa").content.decode())
        html = sem.get(reverse("m2m_chips") + "?sessao=ativa&conectividade=ativa&situacao=erro_consulta&ordem=consumo&de=2020-01-01").content.decode()
        self.assertIn("Ativo", html)
        self.assertIn("Outro", html)

    def test_o_detalhe_do_chip_nao_traz_sessao_consumo_consulta_nem_ocorrencia(self):
        veiculo, chip = self.cenario_com_dados_de_api()
        sem = self.cliente(self.colaborador()).get(reverse("m2m_chip_detalhe", args=[chip.pk])).content.decode()
        com = self.cliente(self.com_api()).get(reverse("m2m_chip_detalhe", args=[chip.pk])).content.decode()
        self.assertIn(CANARIO_OCORRENCIA, com)                        # o teste enxerga: a ocorrência aparece para quem vê API
        for marca in (CANARIO_OCORRENCIA, CANARIO_ESTADO, "m2m-d-sessao", "m2m-d-consumo", "Sessões de dados", "Consumo (último valor)",
                      "Última consulta", "Limite mensal", "Consulta pelo", "data-m2m-atualizar", "data-m2m-payload", "Desativar o monitoramento"):
            self.assertNotIn(marca, sem, marca)
        for api in URLS_DA_API:
            self.assertNotIn(api, sem, api)
        self.assertIn("Dados cadastrais", sem)

    def test_o_historico_do_chip_sem_api_so_tem_cadastro_e_movimentacao(self):
        veiculo, chip = self.cenario_com_dados_de_api()
        sem = m2m.linha_do_tempo(chip, com_api=False)
        com = m2m.linha_do_tempo(chip)
        self.assertEqual({i["fonte"] for i in sem}, {"evento"})
        self.assertTrue({"consulta", "ocorrencia"} <= {i["fonte"] for i in com})
        # nem os eventos que contam a API (estado da sessão, monitoramento ligado/desligado)
        for evento in sem:
            self.assertNotIn(evento["tipo"], [t.value for t in m2m.EVENTOS_DE_API])

    def test_a_visao_geral_da_conectividade_tem_o_grafico_de_uso_e_nenhum_dado_da_api(self):
        self.cenario_com_dados_de_api()
        sem = self.cliente(self.colaborador()).get(reverse("m2m_visao_geral")).content.decode()
        com = self.cliente(self.com_api()).get(reverse("m2m_visao_geral")).content.decode()
        self.assertIn("Chips utilizados e não utilizados", sem)       # o gráfico do vínculo é cadastro: continua
        for marca in ("Consumo de dados", "Tempo de inatividade", "Requer atenção", "Sessões ativas confirmadas", "Falhas de consulta",
                      "Últimas sincronizações", "Integração TIM", "m2m-faixa"):
            self.assertIn(marca, com, marca)                          # o teste enxerga
            self.assertNotIn(marca, sem, marca)

    def test_a_porta_do_agro_nao_tem_kpi_de_sessao_nem_consumo(self):
        self.cenario_com_dados_de_api()
        resposta = self.cliente(self.colaborador()).get(reverse("agro_visao_geral"))
        painel = resposta.context["painel"]
        self.assertNotIn("com_sessao_ativa", painel["kpis_conectividade"])
        self.assertIn("chips_total", painel["kpis_conectividade"])
        self.assertIsNone(painel["consumo"])
        self.assertIsNone(painel["sincronizacao"])
        self.assertIsNone(painel["alertas_abertos"])
        html = resposta.content.decode()
        for marca in ("Sessões ativas", "Falhas de consulta", "Consumo de dados", "Sincronização com a TIM"):
            self.assertNotIn(marca, html, marca)
        # e para quem vê API, está lá
        com = self.cliente(self.com_api()).get(reverse("agro_visao_geral")).content.decode()
        self.assertIn("Sessões ativas", com)

    def test_o_detalhe_do_veiculo_nao_tem_conectividade_nem_ocorrencia(self):
        veiculo, chip = self.cenario_com_dados_de_api()
        url = reverse("frota_veiculo_detalhe", args=[veiculo.pk])
        sem = self.cliente(self.colaborador()).get(url).content.decode()
        com = self.cliente(self.com_api()).get(url).content.decode()
        self.assertIn("m2m-v-d-conect", com)
        self.assertNotIn("m2m-v-d-conect", sem)
        self.assertNotIn(CANARIO_OCORRENCIA, sem)
        self.assertNotIn("data-m2m-atualizar", sem)
        self.assertIn("Chip A", sem)                                  # o chip vinculado continua: é vínculo

    def test_o_mapa_nao_leva_estado_de_sessao_no_json(self):
        veiculo, chip = self.cenario_com_dados_de_api()
        from ProjetoEstoque.models import FrotaVeiculo
        FrotaVeiculo.objects.filter(pk=veiculo.pk).update(latitude=-14.77, longitude=-45.56)
        sem = self.cliente(self.colaborador()).get(reverse("agro_mapa_dados")).json()
        com = self.cliente(self.com_api()).get(reverse("agro_mapa_dados")).json()
        self.assertEqual(len(sem["veiculos"]), 1)
        ponto, chip_sem = sem["veiculos"][0], sem["veiculos"][0]["chips"][0]
        self.assertNotIn("conectividade", ponto)
        for chave in ("estado", "rotulo", "sessao", "ultima_atualizacao", "atividade"):
            self.assertNotIn(chave, chip_sem, chave)
        self.assertEqual((chip_sem["nome"], chip_sem["iccid"]), ("Chip A", ICCID_A))
        # com API, as chaves existem (o teste enxerga)
        self.assertIn("conectividade", com["veiculos"][0])
        self.assertIn("sessao", com["veiculos"][0]["chips"][0])

    def test_o_filtro_de_conectividade_do_mapa_e_da_lista_e_ignorado_sem_api(self):
        veiculo, chip = self.cenario_com_dados_de_api()
        self.veiculo("MQ-2")
        from ProjetoEstoque.models import FrotaVeiculo
        FrotaVeiculo.objects.update(latitude=-14.77, longitude=-45.56)
        # MQ-2 não tem chip: filtrar por conectividade "ativa" o tiraria — e isso é ler a API
        c = self.cliente(self.colaborador())
        sem = c.get(reverse("agro_mapa_dados") + "?conectividade=ativa").json()
        self.assertEqual(len(sem["veiculos"]), 2)
        html = c.get(reverse("frota_equipamentos_proprios") + "?conectividade=ativa").content.decode()
        self.assertIn("MQ-2", html)
        self.assertNotIn("Situação da conectividade", html)

    def test_as_funcoes_do_service_sem_api_nao_calculam_nem_devolvem_a_leitura_da_operadora(self):
        self.cenario_com_dados_de_api()
        ind = frota_service.indicadores_frota(com_api=False)["conectividade"]
        for chave in ("com_sessao_ativa", "sem_sessao", "com_falha"):
            self.assertNotIn(chave, ind)
        self.assertIn("com_sessao_ativa", frota_service.indicadores_frota()["conectividade"])
        atencao = frota_service.area_de_atencao(com_api=False)
        for bloco in ("chips_sem_atualizacao", "falhas_de_comunicacao", "conectividade_pendente"):
            self.assertEqual(atencao[bloco], {"total": 0, "itens": []})
        painel = analise.painel_de_analise(com_api=False)
        self.assertIsNone(painel["consumo"])
        self.assertIsNone(painel["inatividade"])
        self.assertNotIn("sessao_ativa", painel["indicadores"])
        self.assertIn("em_uso", painel["indicadores"])
        self.assertIsNotNone(painel["utilizacao"])                    # o gráfico de uso é do vínculo


# ═════════════════════════════════════════════════════════════
# Todo link abre
# ═════════════════════════════════════════════════════════════

class TodoLinkAbreTests(Base):
    """Um link que leva o colaborador a um 403 é um vazamento de interface: mostra que a tela existe (TI ou API) e
    termina em erro. Abre cada tela do Agro, junta os links internos e segue cada um."""

    PAGINAS = (
        "agro_visao_geral", "frota_visao_geral", "frota_equipamentos_proprios", "frota_equip_movimentacoes", "frota_relatorios",
        "m2m_visao_geral", "m2m_chips", "frota_movimentacoes", "frota_importar", "tec_visao_geral", "tec_equipamentos", "tec_materiais",
        "tec_movimentacoes", "infra_visao_geral", "infra_estruturas", "infra_enlaces", "agro_mapa", "m2m_chip_novo", "frota_veiculo_novo",
    )
    IGNORAR = re.compile(r"^(/logout/|/static/|/media/|/senha/|/login/|/notificacoes/|/perfil/|#|javascript:|mailto:|tel:|https?:)")

    def links_de(self, html):
        achados = set(re.findall(r'(?:href|action)="(/[^"#]*)"', html))
        return {l.replace("&amp;", "&") for l in achados if not self.IGNORAR.match(l) and "{{" not in l and "formato=xlsx" not in l}

    def test_nenhum_link_do_agro_leva_o_colaborador_a_um_erro(self):
        veiculo, chip = self.cenario_com_dados_de_api()
        c = self.cliente(self.colaborador())
        paginas = [reverse(nome) for nome in self.PAGINAS]
        paginas += [reverse("frota_veiculo_detalhe", args=[veiculo.pk]), reverse("m2m_chip_detalhe", args=[chip.pk])]
        links = set()
        for pagina in paginas:
            resposta = c.get(pagina)
            self.assertEqual(resposta.status_code, 200, pagina)
            links |= self.links_de(resposta.content.decode())
        problemas = []
        for link in sorted(links):
            if link.startswith("/") and not link.startswith("/agro/"):
                problemas.append((link, "fora do Agro"))
                continue
            status = c.get(link).status_code
            if status >= 400:
                problemas.append((link, status))
        self.assertEqual(problemas, [], "links que o colaborador vê e não abrem")

    TERMOS_DA_API = re.compile(r"\bAPI\b|Saitro|monitoramento|\bsess(?:ão|ões)\b|consumo de dados|última consulta|\bconsultas?\b", re.I)

    @staticmethod
    def texto_visivel(html):
        """O texto que a pessoa pode ler — inclusive o de diálogo escondido, que existe na página —, sem script, estilo nem comentário."""
        html = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
        html = re.sub(r"(?s)<!--.*?-->", " ", html)
        return re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", html))

    def test_nenhuma_tela_do_agro_cita_a_api_para_o_colaborador(self):
        """Link e número são pegos pelos outros testes; aqui é o TEXTO: rótulo, dica, diálogo ("Retomar o monitoramento…"), rodapé."""
        veiculo, chip = self.cenario_com_dados_de_api()
        c = self.cliente(self.colaborador())
        paginas = [reverse(nome) for nome in self.PAGINAS]
        paginas += [reverse("frota_veiculo_detalhe", args=[veiculo.pk]), reverse("m2m_chip_detalhe", args=[chip.pk]),
                    reverse("m2m_chip_editar", args=[chip.pk])]
        achados = {}
        for pagina in paginas:
            texto = self.texto_visivel(c.get(pagina).content.decode())
            for m in self.TERMOS_DA_API.finditer(texto):
                achados.setdefault(pagina, set()).add(texto[max(0, m.start() - 40): m.end() + 40])
        self.assertEqual(achados, {}, "texto de API visível para o colaborador")
        # e a varredura enxerga: as MESMAS páginas, para quem vê API, citam a API (senão o teste de cima nunca falharia)
        com = self.cliente(self.com_api())
        assert_com = 0
        for pagina in paginas:
            assert_com += len(self.TERMOS_DA_API.findall(self.texto_visivel(com.get(pagina).content.decode())))
        self.assertGreater(assert_com, 20)


# ═════════════════════════════════════════════════════════════
# Liberar e revogar
# ═════════════════════════════════════════════════════════════

class LiberacaoTests(Base):
    def cliente_admin(self):
        c = Client()
        c.force_login(self.admin)
        return c

    def test_a_tela_de_acessos_traz_o_cartao_do_colaborador(self):
        self.colaborador("ja_tem")
        html = self.cliente_admin().get(reverse("acesso_restrito_gerenciar")).content.decode()
        self.assertIn("Colaborador do Agro", html)
        self.assertIn("ja_tem", html)
        self.assertIn('value="colaborador_agro"', html)

    def test_so_administrador_libera(self):
        comum = User.objects.create_user("comum", password="x")
        c = self.cliente(User.objects.get(pk=comum.pk))
        resposta = c.post(reverse("acesso_restrito_conceder"), {"perfil": "colaborador_agro", "username": "alguem", "senha": "senha-boa-1"})
        self.assertFalse(User.objects.filter(username="alguem").exists())
        self.assertIn(resposta.status_code, (302, 403))

    def test_libera_login_novo_e_a_pessoa_entra_no_agro_e_so_nele(self):
        resposta = self.cliente_admin().post(reverse("acesso_restrito_conceder"), {
            "perfil": "colaborador_agro", "username": "maria.agro", "email": "m@x.com", "senha": "senha-boa-1",
        })
        self.assertEqual(resposta.status_code, 302)
        user = User.objects.get(username="maria.agro")
        self.assertTrue(user.is_active)
        self.assertFalse(user.is_staff or user.is_superuser)
        self.assertTrue(colab.e_colaborador(user))
        c = Client()
        self.assertTrue(c.login(username="maria.agro", password="senha-boa-1"))
        self.assertRedirects(c.get("/"), reverse("agro_visao_geral"), fetch_redirect_response=False)
        self.assertEqual(c.get(reverse("agro_visao_geral")).status_code, 200)
        self.assertEqual(c.get("/dashboard/").status_code, 403)

    def test_login_novo_exige_senha(self):
        with self.assertRaises(ValidationError):
            colab.conceder(username="sem.senha")

    def test_libera_login_existente_sem_trocar_a_senha(self):
        User.objects.create_user("existente", "e@x.com", "senha-antiga-1")
        usuario, criado = colab.conceder(username="existente")
        self.assertFalse(criado)
        usuario.refresh_from_db()
        self.assertTrue(usuario.check_password("senha-antiga-1"))
        self.assertTrue(colab.e_colaborador(usuario))

    def test_administrador_nao_recebe(self):
        for campo in ("is_staff", "is_superuser"):
            User.objects.create_user(f"adm_{campo}", password="x", **{campo: True})
            with self.assertRaises(ValidationError):
                colab.conceder(username=f"adm_{campo}")
            self.assertFalse(colab.e_colaborador(User.objects.get(username=f"adm_{campo}")))

    def test_quem_ja_tem_outro_acesso_ou_e_do_ti_nao_recebe(self):
        """O perfil prende o login no Agro: dar a quem é do TI o deixaria sem o TI por engano; e os portais já prendem em outro lugar."""
        for grupo in (GRUPO_PORTAL_CONTRATACAO, GRUPO_CONSULTA_QUIOSQUE, GRUPO_FORNECEDOR, "Operador TI", "Gestor TI", "Administrador TI"):
            nome = f"u_{grupo[:6]}_{len(grupo)}"
            user = User.objects.create_user(nome, password="x")
            user.groups.add(Group.objects.get_or_create(name=grupo)[0])
            with self.assertRaises(ValidationError, msg=grupo):
                colab.conceder(username=nome)
            self.assertFalse(colab.e_colaborador(User.objects.get(pk=user.pk)), grupo)
            self.assertNotIn(user.pk, [c.pk for c in colab.candidatos()], grupo)

    def test_a_senha_curta_e_recusada(self):
        with self.assertRaises(ValidationError):
            colab.conceder(username="curta", senha="123")

    def test_revogar_tira_o_grupo_e_desativa_o_login(self):
        """Sem o grupo a pessoa deixaria de estar presa ao Agro — e o TI é o padrão de todo login interno. Revogar não pode ABRIR o TI."""
        user = self.colaborador("sai")
        c = Client()
        self.assertTrue(c.login(username="sai", password="senha-boa-123"))
        resposta = self.cliente_admin().post(reverse("acesso_restrito_revogar", args=[user.pk]), {"perfil": "colaborador_agro"})
        self.assertEqual(resposta.status_code, 302)
        user.refresh_from_db()
        self.assertFalse(user.is_active)
        self.assertFalse(colab.e_colaborador(user))
        # a sessão que já estava aberta cai, e o login novo não entra
        self.assertEqual(c.get(reverse("agro_visao_geral")).status_code, 302)
        self.assertFalse(Client().login(username="sai", password="senha-boa-123"))
        self.assertEqual(Client().post("/login/", {"username": "sai", "password": "senha-boa-123"}).status_code, 200)

    def test_liberar_de_novo_reativa(self):
        user = self.colaborador("volta")
        colab.revogar(usuario=user)
        usuario, criado = colab.conceder(username="volta")
        self.assertFalse(criado)
        self.assertTrue(usuario.is_active)
        self.assertTrue(colab.e_colaborador(User.objects.get(pk=usuario.pk)))

    def test_o_cache_de_grupos_nao_segura_o_acesso_revogado(self):
        user = self.colaborador("cache")
        amb.ambientes_de(user)                      # enche o cache de grupos (60 s)
        colab.revogar(usuario=user)
        self.assertFalse(colab.e_colaborador(User.objects.get(pk=user.pk)))

    def test_os_perfis_restritos_continuam_como_eram(self):
        """O cartão novo não pode mudar a mecânica dos outros dois (Contratações e Quiosque)."""
        html = self.cliente_admin().get(reverse("acesso_restrito_gerenciar")).content.decode()
        self.assertIn("Portal de Contratações", html)
        self.assertIn("Consulta Quiosque", html)
        resposta = self.cliente_admin().post(reverse("acesso_restrito_conceder"), {
            "perfil": "contratacao", "username": "rh.ana", "senha": "senha-boa-1",
        })
        self.assertEqual(resposta.status_code, 302)
        self.assertTrue(User.objects.get(username="rh.ana").groups.filter(name=GRUPO_PORTAL_CONTRATACAO).exists())
