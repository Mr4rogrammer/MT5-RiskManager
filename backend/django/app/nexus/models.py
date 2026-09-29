from django.db import models

class Trade(models.Model):
    TRADE_TYPE_CHOICES = [
        ('BUY', 'Buy'),
        ('SELL', 'Sell'),
    ]

    CLOSING_REASON_CHOICES = [
        ('TP', 'Take Profit'),
        ('SL', 'Stop Loss'),
        ('MANUAL', 'Manual'),
        ('LIQUIDATION', 'Liquidation'),
        ('OTHER', 'Other'),
    ]

    MARKET_TYPE_CHOICES = [
        ('FOREX', 'Forex'),
        ('CRYPTO', 'Crypto'),
        ('OTHER', 'Other'),
    ]

    TIMEFRAME_CHOICES = [
        ('1M', '1 Minute'),
        ('5M', '5 Minutes'),
        ('15M', '15 Minutes'),
        ('1H', '1 Hour'),
        ('4H', '4 Hours'),
        ('1D', '1 Day'),
    ]

    # Core trade fields
    transaction_broker_id = models.CharField(max_length=100)
    symbol = models.CharField(max_length=10)
    entry_time = models.DateTimeField()
    entry_price = models.FloatField()
    type = models.CharField(max_length=4, choices=TRADE_TYPE_CHOICES)
    position_size_usd = models.FloatField()
    capital = models.FloatField()
    leverage = models.FloatField(default=500)
    order_volume = models.FloatField(null=True, blank=True)
    liquidity_price = models.FloatField()
    break_even_price = models.FloatField()
    order_commission = models.FloatField()

    # Closing details
    close_time = models.DateTimeField(null=True, blank=True)
    close_price = models.FloatField(null=True, blank=True)
    pnl = models.FloatField(null=True, blank=True)
    pnl_excluding_commission = models.FloatField(null=True, blank=True)
    max_drawdown = models.FloatField(null=True, blank=True)
    max_profit = models.FloatField(null=True, blank=True)
    closing_reason = models.CharField(max_length=50, null=True, blank=True, choices=CLOSING_REASON_CHOICES)

    # Additional Info
    strategy = models.CharField(max_length=50)
    broker = models.CharField(max_length=50)
    market_type = models.CharField(max_length=50, choices=MARKET_TYPE_CHOICES)
    timeframe = models.CharField(max_length=50, choices=TIMEFRAME_CHOICES)

    def __str__(self):
        return f"{self.type} {self.symbol} at {self.entry_price}"


class TradeClosePricesMutation(models.Model):
    trade = models.ForeignKey(Trade, on_delete=models.CASCADE, related_name='close_prices_mutations')
    mutation_time = models.DateTimeField(auto_now_add=True)
    mutation_price = models.FloatField(null=True, blank=True)
    new_tp_price = models.FloatField(null=True, blank=True)
    new_sl_price = models.FloatField(null=True, blank=True)
    pnl_at_new_tp_price = models.FloatField(null=True, blank=True)
    pnl_at_new_sl_price = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ['mutation_time']
        verbose_name = "Trade Close Prices Mutation"
        verbose_name_plural = "Trade Close Prices Mutations"

    def __str__(self):
        return f"Mutation for {self.trade} at {self.mutation_time}"