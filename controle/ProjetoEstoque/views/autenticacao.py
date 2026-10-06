"""
Recuperação de senha ("esqueci minha senha").

As quatro telas do fluxo nativo do Django, com três mudanças sobre o padrão:

  • o campo aceita USUÁRIO ou e-mail (ver recuperacao_senha_service);
  • há limite de pedidos por IP e por identificador;
  • cada pedido e cada troca efetiva entram em `RegistroSeguranca`.

A resposta ao usuário é SEMPRE a mesma — "se existir, enviamos" — inclusive
quando o limite foi estourado ou o identificador não resolveu em ninguém.
Qualquer diferença de mensagem transformaria a tela num verificador de logins
válidos, e o portal está exposto na internet.

Regra de arquitetura: a lógica mora em services/recuperacao_senha_service.py.
Aqui ficam só a ligação com o Django e a decisão de o que responder.
"""
from django import forms
from django.contrib.auth.forms import PasswordResetForm, SetPasswordForm
from django.contrib.auth.views import (
    PasswordResetCompleteView,
    PasswordResetConfirmView,
    PasswordResetDoneView,
    PasswordResetView,
)
from django.urls import reverse_lazy

from services import recuperacao_senha_service as svc


class FormRecuperacaoSenha(PasswordResetForm):
    """
    `PasswordResetForm` com o campo trocado de e-mail para "usuário ou e-mail".

    O campo do pai é `EmailField`, que recusaria um username na validação antes
    de chegar ao `get_users`. Por isso ele é redeclarado como `CharField` — a
    resolução de quem recebe o link passa a ser nossa.
    """

    email = None  # some do form; o campo abaixo toma o lugar

    identificador = forms.CharField(
        label="Usuário ou e-mail",
        max_length=254,
        strip=True,
        widget=forms.TextInput(attrs={
            "autocomplete": "username",
            "autofocus": True,
            "class": "form-control",
            "placeholder": "seu.usuario",
            "spellcheck": "false",
        }),
    )

    def get_users(self, identificador):
        return svc.usuarios_para_identificador(identificador)

    def save(self, **kwargs):
        """
        O pai lê `self.cleaned_data["email"]`; aqui o dado está em
        `identificador`. Em vez de reescrever o envio (que monta token, uid e
        contexto do e-mail), só traduzimos a chave e delegamos.
        """
        self.cleaned_data = dict(self.cleaned_data)
        self.cleaned_data["email"] = self.cleaned_data.get("identificador", "")
        return super().save(**kwargs)


class RecuperarSenhaView(PasswordResetView):
    template_name = "registration/password_reset_form.html"
    email_template_name = "registration/password_reset_email.html"
    subject_template_name = "registration/password_reset_subject.txt"
    form_class = FormRecuperacaoSenha
    success_url = reverse_lazy("password_reset_done")

    def form_valid(self, form):
        identificador = form.cleaned_data.get("identificador", "")

        if not svc.pedido_dentro_do_limite(self.request, identificador):
            # Estourou o limite: NÃO envia e NÃO avisa. Avisar entregaria ao
            # atacante a confirmação de que o alvo é real e que a cota existe;
            # para o usuário legítimo que insistiu, a tela segue dizendo o
            # mesmo que diria se tivesse enviado.
            svc.registrar_pedido(self.request, identificador, enviados=0)
            return super(PasswordResetView, self).form_valid(form)

        alvos = svc.usuarios_para_identificador(identificador)
        resposta = super().form_valid(form)
        svc.registrar_pedido(self.request, identificador, enviados=len(alvos))
        return resposta


class RecuperarSenhaEnviadoView(PasswordResetDoneView):
    template_name = "registration/password_reset_done.html"


class FormNovaSenha(SetPasswordForm):
    """Só aplica a classe de estilo do shell de autenticação nos dois campos."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for campo in self.fields.values():
            campo.widget.attrs.setdefault("class", "form-control")
            campo.widget.attrs.setdefault("autocomplete", "new-password")


class RecuperarSenhaConfirmarView(PasswordResetConfirmView):
    template_name = "registration/password_reset_confirm.html"
    form_class = FormNovaSenha
    success_url = reverse_lazy("password_reset_complete")

    def form_valid(self, form):
        resposta = super().form_valid(form)
        svc.registrar_conclusao(self.request, form.user)
        return resposta


class RecuperarSenhaConcluidoView(PasswordResetCompleteView):
    template_name = "registration/password_reset_complete.html"
