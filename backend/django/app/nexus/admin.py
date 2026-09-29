from django.contrib import admin
from .models import Trade, TradeClosePricesMutation

@admin.register(Trade)
class TradeAdmin(admin.ModelAdmin):
    list_display = [field.name for field in Trade._meta.fields]
    list_filter = [field.name for field in Trade._meta.fields]
    search_fields = [field.name for field in Trade._meta.fields]
        
    ordering = ('-entry_time',)

@admin.register(TradeClosePricesMutation)
class TradeClosePricesMutationAdmin(admin.ModelAdmin):
    list_display = [field.name for field in TradeClosePricesMutation._meta.fields]
    list_filter = [field.name for field in TradeClosePricesMutation._meta.fields]    
    search_fields = [field.name for field in TradeClosePricesMutation._meta.fields]
    
    ordering = ('-mutation_time',)
