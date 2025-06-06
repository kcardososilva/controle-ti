from datetime import timedelta, date
from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from .models import Equipamento, Categoria, Subtipo, Preventiva
from .forms import CategoriaForm, SubtipoForm, EquipamentoForm, ComentarioForm, PreventivaForm
from django.shortcuts import render, redirect
import openpyxl
from openpyxl.utils import get_column_letter
from django.http import HttpResponse
from django.contrib import messages
from django.utils import timezone
from dateutil.relativedelta import relativedelta


############# Home #################
@login_required
def home(request):
    equipamentos = Equipamento.objects.all()
    categorias = Categoria.objects.all()
    subtipos = Subtipo.objects.all()

    search = request.GET.get('search')
   
    if search:
        equipamentos = equipamentos.filter(nome__icontains=search) | equipamentos.filter(numero_serie__icontains=search) | equipamentos.filter(subtipo__nome__icontains=search)

    if request.GET.get('categoria'):
        equipamentos = equipamentos.filter(categoria_id=request.GET.get('categoria'))

    if request.GET.get('subtipo'):
        equipamentos = equipamentos.filter(subtipo_id=request.GET.get('subtipo'))

    if request.GET.get('status'):
        equipamentos = equipamentos.filter(status=request.GET.get('status'))

    context = {
        'equipamentos': equipamentos,
        'categorias': categorias,
        'subtipos': subtipos,
        'status_choices': Equipamento.STATUS_CHOICES
    }
    return render(request, 'front\\home.html', context)


############### Visualização de Equipamento #########################
@login_required
def equipamento_detalhe(request, pk):
    equipamento = get_object_or_404(Equipamento, pk=pk)
    comentarios = equipamento.comentarios.order_by('-criado_em')

    if request.method == 'POST':
        form = ComentarioForm(request.POST)
        if form.is_valid():
            comentario = form.save(commit=False)
            comentario.equipamento = equipamento
            comentario.autor = request.user
            comentario.save()
            return redirect('equipamento_detalhe', pk=equipamento.pk)
    else:
        form = ComentarioForm()

    return render(request, 'front\\equipamento_detalhe.html', {
        'equipamento': equipamento,
        'comentarios': comentarios,
        'form_comentario': form,
    })

    

################ Cadastro de Categoria #####################

@login_required
def cadastrar_categoria(request):
    if request.method == 'POST':
        form = CategoriaForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('home')
    else:
        form = CategoriaForm()
    return render(request, 'front\\cadastrar_categoria.html', {'form': form})

#################### Cadastro de SUbtipo ######################
@login_required
def cadastrar_subtipo(request):
    if request.method == 'POST':
        form = SubtipoForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('home')
    else:
        form = SubtipoForm()
    return render(request, 'front\\cadastrar_subtipo.html', {'form': form})

################### Cadastro de equipamento ###########################
@login_required
def cadastrar_equipamento(request):
    if request.method == 'POST':
        form = EquipamentoForm(request.POST)
        if form.is_valid():
            equipamento = form.save(commit=False)
            equipamento._user = request.user  # Passa o usuário para o modelo
            equipamento.save()
            return redirect('home')
    else:
        form = EquipamentoForm()
    return render(request, 'front\\cadastrar_equipamento.html', {'form': form})

############# Edição de equipamento ##########
@login_required
def editar_equipamento(request, pk):
    equipamento = get_object_or_404(Equipamento, pk=pk)

    if request.method == 'POST':
        form = EquipamentoForm(request.POST, instance=equipamento)
        if form.is_valid():
            form.save()
            return redirect('home')
    else:
        form = EquipamentoForm(instance=equipamento)

    return render(request, 'front\\editar_equipamento.html', {'form': form, 'equipamento': equipamento})

######### exclusão de equipamento #############
@login_required
def excluir_equipamento(request, pk):
    equipamento = get_object_or_404(Equipamento, pk=pk)
    equipamento.delete()
    return redirect('home')



### Gerar Relatório ###
@login_required
def exportar_equipamentos_excel(request):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Relatório de Equipamentos Santa Colomba"

    colunas = [
        "ID", "Nome", "Categoria", "Subtipo", "Número de Série",
        "Marca", "Modelo", "Local", "Status", "Quantidade",
        "Estoque Mínimo", "Observações"
    ]
    ws.append(colunas)
    
    for equipamento in Equipamento.objects.select_related('categoria', 'subtipo').all():
        ws.append([
            equipamento.id,
            equipamento.nome,
            equipamento.categoria.nome,
            equipamento.subtipo.nome,
            equipamento.numero_serie,
            equipamento.marca or "",
            equipamento.modelo or "",
            equipamento.local,
            equipamento.get_status_display(),
            equipamento.quantidade,
            equipamento.estoque_minimo,
            equipamento.observacoes or ""
          
        ])

    ## Ajuste automático das larguras de coluna
    for col in ws.columns:
        max_length = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            if cell.value:
                max_length = max(max_length, len(str(cell.value)))
        ws.column_dimensions[col_letter].width = max_length + 2

    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response["Content-Disposition"] = 'attachment; filename="equipamentos.xlsx"'
    wb.save(response)
    return response

################ Visualizar Preventivas ####################

def visualizar_preventivas(request, equipamento_id):
    equipamento = get_object_or_404(Equipamento, id=equipamento_id)
    preventivas = Preventiva.objects.filter(equipamento=equipamento).order_by('-data_ultima')

    # calculo de dias
    for preventiva in preventivas:
        if preventiva.data_proxima:
            dias_restantes = (preventiva.data_proxima - date.today()).days
        else:
            dias_restantes = None
        preventiva.dias_restantes = dias_restantes

    return render(request, 'front/preventivas.html', {
        'equipamento': equipamento,
        'preventivas': preventivas,
    })



################## Preventivas #################

@login_required
def cadastrar_preventiva(request, equipamento_id):
    equipamento = get_object_or_404(Equipamento, id=equipamento_id)

    if request.method == 'POST':
        form = PreventivaForm(request.POST)
        if form.is_valid():
            preventiva = form.save(commit=False)
            preventiva.equipamento = equipamento
            preventiva.autor = request.user
            preventiva.data_ultima = timezone.now()

            # Define próxima preventiva (baseado nos meses definidos no equipamento)
            meses = equipamento.data_limite_preventiva or 3  # padrão 3 meses
            preventiva.data_proxima = preventiva.data_ultima + timedelta(days=30 * meses)

            preventiva.save()

            # Atualiza equipamento com a nova data da última preventiva
            equipamento.ultima_preventiva = preventiva.data_ultima
            equipamento.save()
            messages.success(
                request,
                f"Preventiva cadastrada com sucesso. Próxima prevista para {preventiva.data_proxima.strftime('%d/%m/%Y')}."
            )

            return redirect('equipamento_detalhe', pk=equipamento.id)
    else:
        form = PreventivaForm()

    return render(request, 'front\\preventiva_form.html', {
        'form': form,
        'equipamento': equipamento,
    })