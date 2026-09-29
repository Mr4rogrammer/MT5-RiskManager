from django_filters import rest_framework as filters
from .models import Trade

class TradeFilter(filters.FilterSet):
    # Time range filters
    entry_time_after = filters.DateTimeFilter(field_name='entry_time', lookup_expr='gte')
    entry_time_before = filters.DateTimeFilter(field_name='entry_time', lookup_expr='lte')
    close_time_after = filters.DateTimeFilter(field_name='close_time', lookup_expr='gte')
    close_time_before = filters.DateTimeFilter(field_name='close_time', lookup_expr='lte')
    
    # Numeric range filters
    pnl_min = filters.NumberFilter(field_name='pnl', lookup_expr='gte')
    pnl_max = filters.NumberFilter(field_name='pnl', lookup_expr='lte')
    position_size_min = filters.NumberFilter(field_name='position_size_usd', lookup_expr='gte')
    position_size_max = filters.NumberFilter(field_name='position_size_usd', lookup_expr='lte')
    
    # Symbol filters
    symbol = filters.CharFilter(lookup_expr='iexact')
    symbols = filters.BaseInFilter(field_name='symbol', lookup_expr='in')
    
    # Trade type filter
    type = filters.CharFilter(lookup_expr='iexact')
    
    # Status filters
    is_open = filters.BooleanFilter(field_name='close_time', lookup_expr='isnull')

    class Meta:
        model = Trade
        fields = {
            'leverage': ['exact', 'gte', 'lte'],
            'max_drawdown': ['gte', 'lte'],
            'max_profit': ['gte', 'lte'],
            'closing_reason': ['exact', 'icontains'],
        }