from django.shortcuts import render, redirect, get_object_or_404
from ..models import Locacao
from ..forms import LocacaoForm


def locacoes_list(request):
    locacoes = Locacao.objects.all()
    return render(request, 'front/locacoes/locacao_list.html', {'locacoes': locacoes})


def locacao_create(request):
    form = LocacaoForm(request.POST or None)
    if form.is_valid():
        locacao = form.save(commit=False)
        locacao.save()
        return redirect('locacoes_list')
    return render(request, 'front/locacoes/locacao_form.html', {'form': form})


def locacao_update(request, pk):
    locacao = get_object_or_404(Locacao, pk=pk)
    form = LocacaoForm(request.POST or None, instance=locacao)
    if form.is_valid():
        locacao = form.save(commit=False)
        locacao.save()
        return redirect('locacoes_list')
    return render(request, 'front/locacoes/locacao_form.html', {'form': form})


def locacao_delete(request, pk):
    locacao = get_object_or_404(Locacao, pk=pk)
    if request.method == 'POST':
        locacao.delete()
        return redirect('locacoes_list')
    return render(request, 'front/locacoes/locacao_confirm_delete.html', {'obj': locacao})
