from django.contrib import admin

# Register your models here.

from .models import Categoria, Subtipo, Equipamento, Comentario, Preventiva

admin.site.register(Categoria)
admin.site.register(Subtipo)
admin.site.register(Equipamento)
admin.site.register(Comentario)
admin.site.register(Preventiva)
