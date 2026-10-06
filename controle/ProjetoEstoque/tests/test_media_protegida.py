"""
Entrega autenticada de /media/ — ver services/media_acesso_service.py.

O que estes testes travam (cada um já foi um buraco real ou é o risco de
regressão mais óbvio):
  • anônimo não baixa nada (antes baixava TUDO adivinhando a URL);
  • usuário interno continua baixando (não quebrar o sistema ao fechar o furo);
  • fornecedor baixa a NF dele e NÃO baixa a do concorrente;
  • fornecedor não alcança termo assinado nem foto de preventiva;
  • path traversal não escapa do MEDIA_ROOT.

Roda em banco de teste próprio (TestCase), nunca no banco real.
"""
import shutil
import tempfile
from pathlib import Path

from django.contrib.auth.models import Group, User
from django.test import TestCase, Client, override_settings

from ProjetoEstoque.models import (
    Fornecedor,
    GRUPO_FORNECEDOR,
    Item,
    OrdemManutencao,
    OrdemManutencaoAnexo,
    PerfilFornecedor,
)

_MEDIA_TEMP = tempfile.mkdtemp(prefix="media_teste_")


@override_settings(MEDIA_ROOT=_MEDIA_TEMP)
class MediaProtegidaTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_MEDIA_TEMP, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.client = Client()
        self.interno = User.objects.create_user("interno_teste", password="x")
        self.admin = User.objects.create_superuser("admin_teste", "a@t.com", "x")

        grupo = Group.objects.get_or_create(name=GRUPO_FORNECEDOR)[0]

        self.forn_a = Fornecedor.objects.create(
            nome="Fornecedor A", cnpj="11.111.111/0001-11",
            criado_por=self.admin, atualizado_por=self.admin,
        )
        self.forn_b = Fornecedor.objects.create(
            nome="Fornecedor B", cnpj="22.222.222/0001-22",
            criado_por=self.admin, atualizado_por=self.admin,
        )

        self.user_a = User.objects.create_user("forn_a", password="x")
        self.user_a.groups.add(grupo)
        PerfilFornecedor.objects.create(
            usuario=self.user_a, fornecedor=self.forn_a, ativo=True,
            criado_por=self.admin, atualizado_por=self.admin,
        )

        self.user_b = User.objects.create_user("forn_b", password="x")
        self.user_b.groups.add(grupo)
        PerfilFornecedor.objects.create(
            usuario=self.user_b, fornecedor=self.forn_b, ativo=True,
            criado_por=self.admin, atualizado_por=self.admin,
        )

        # NF do fornecedor A, anexada a uma ordem de manutenção dele.
        self.nf_a = self._anexo_de(self.forn_a, "manutencao/nf/2026/10/nf_a.pdf")
        # Termo assinado — documento interno, de ninguém de fora.
        self.termo = self._criar_arquivo("termos/termo_99.pdf")

    # ── helpers ──────────────────────────────────────────────────────────────

    def _criar_arquivo(self, caminho_rel: str) -> str:
        destino = Path(_MEDIA_TEMP) / caminho_rel
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(b"%PDF-1.4 conteudo de teste")
        return caminho_rel

    def _anexo_de(self, fornecedor, caminho_rel: str) -> str:
        self._criar_arquivo(caminho_rel)
        item = Item.objects.create(
            nome=f"Equipamento {fornecedor.nome}",
            criado_por=self.admin, atualizado_por=self.admin,
        )
        ordem = OrdemManutencao.objects.create(
            item=item, fornecedor=fornecedor,
            criado_por=self.admin, atualizado_por=self.admin,
        )
        OrdemManutencaoAnexo.objects.create(
            ordem=ordem, arquivo=caminho_rel,
            criado_por=self.admin, atualizado_por=self.admin,
        )
        return caminho_rel

    def _get(self, caminho_rel: str):
        return self.client.get(f"/media/{caminho_rel}")

    # ── testes ───────────────────────────────────────────────────────────────

    def test_anonimo_nao_baixa(self):
        """O furo original: sem login, nada."""
        for caminho in (self.termo, self.nf_a):
            resp = self._get(caminho)
            self.assertIn(resp.status_code, (302, 403, 404), caminho)
            if resp.status_code == 302:
                self.assertIn("login", resp["Location"])

    def test_interno_baixa_tudo(self):
        self.client.force_login(self.interno)
        for caminho in (self.termo, self.nf_a):
            self.assertEqual(self._get(caminho).status_code, 200, caminho)

    def test_admin_baixa_tudo(self):
        self.client.force_login(self.admin)
        self.assertEqual(self._get(self.termo).status_code, 200)

    def test_fornecedor_baixa_a_propria_nf(self):
        self.client.force_login(self.user_a)
        self.assertEqual(self._get(self.nf_a).status_code, 200)

    def test_fornecedor_nao_baixa_nf_de_outro(self):
        self.client.force_login(self.user_b)
        self.assertEqual(self._get(self.nf_a).status_code, 404)

    def test_fornecedor_nao_baixa_termo_interno(self):
        self.client.force_login(self.user_a)
        self.assertEqual(self._get(self.termo).status_code, 404)

    def test_traversal_bloqueado(self):
        self.client.force_login(self.admin)
        resp = self._get("../controle/settings.py")
        self.assertNotEqual(resp.status_code, 200)
