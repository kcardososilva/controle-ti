from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.conf import settings
from django.utils import timezone
from ..models import (
    Item, StatusItemChoices, Usuario, Localidade, CentroCusto,
    Fornecedor, Subtipo, Categoria, Funcao, Licenca, MovimentacaoItem,
)


@login_required
def sobre_plataforma(request):
    app_name = getattr(settings, "PROJECT_NAME", "Controle de Ativos")
    version = getattr(settings, "APP_VERSION", "1.0.0")
    build_date = getattr(settings, "APP_BUILD_DATE", None)

    total_itens = Item.objects.count()
    total_ativos = Item.objects.filter(status=StatusItemChoices.ATIVO).count()
    total_backup = Item.objects.filter(status=StatusItemChoices.BACKUP).count()
    total_manut = Item.objects.filter(status=StatusItemChoices.MANUTENCAO).count()
    total_defeito = Item.objects.filter(status=StatusItemChoices.DEFEITO).count()

    ctx = {
        "app_name": app_name,
        "version": version,
        "build_date": build_date,
        "now": timezone.now(),
        "totais": {
            "itens": total_itens,
            "ativos": total_ativos,
            "backup": total_backup,
            "manutencao": total_manut,
            "defeitos": total_defeito,
            "usuarios": Usuario.objects.count(),
            "localidades": Localidade.objects.count(),
            "centros": CentroCusto.objects.count(),
            "fornecedores": Fornecedor.objects.count(),
            "subtipos": Subtipo.objects.count(),
            "categorias": Categoria.objects.count(),
            "funcoes": Funcao.objects.count(),
            "licencas": Licenca.objects.count(),
            "movimentacoes": MovimentacaoItem.objects.count(),
        },
    }
    return render(request, "front/sobre_plataforma.html", ctx)
