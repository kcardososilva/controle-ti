from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from ..models import (
    CentroCusto, Fornecedor, Item, KioskDevice, Licenca, Preventiva, Usuario,
)
from .admin_perfil import SISTEMA_VERSAO


@login_required
def sobre_plataforma(request):
    """Apresentação/portfólio do sistema — vitrine das funcionalidades para
    quem ainda não conhece o Zelo (diretoria, novos colaboradores). Página
    standalone (sem sidebar/topbar), números reais do banco."""
    ctx = {
        "versao": SISTEMA_VERSAO,
        "kpi_itens": Item.objects.count(),
        "kpi_colaboradores": Usuario.objects.filter(status="ativo").count(),
        "kpi_centros_custo": CentroCusto.objects.count(),
        "kpi_preventivas": Preventiva.objects.count(),
        "kpi_licencas": Licenca.objects.count(),
        "kpi_fornecedores": Fornecedor.objects.count(),
        "kpi_kiosk": KioskDevice.objects.count(),
    }
    return render(request, "front/sobre_plataforma.html", ctx)
