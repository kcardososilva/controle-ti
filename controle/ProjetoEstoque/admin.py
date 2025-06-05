from django.contrib import admin

# Register your models here.

from .models import Categoria, Subtipo, Equipamento

admin.site.register(Categoria)
admin.site.register(Subtipo)
admin.site.register(Equipamento)