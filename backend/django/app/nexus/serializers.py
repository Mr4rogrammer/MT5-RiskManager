from rest_framework import serializers
from .models import Trade, TradeClosePricesMutation

class TradeClosePricesMutationSerializer(serializers.ModelSerializer):
    class Meta:
        model = TradeClosePricesMutation
        fields = '__all__'

class TradeSerializer(serializers.ModelSerializer):
    close_prices_mutations = TradeClosePricesMutationSerializer(many=True, read_only=True)

    class Meta:
        model = Trade
        fields = '__all__'