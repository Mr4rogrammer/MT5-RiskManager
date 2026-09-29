import logging

from app.nexus.models import Trade
from django.db import transaction

logger = logging.getLogger(__name__)

def close_trade(ticket, close_time, close_price, pnl, pnl_excluding_commission, closing_reason, closed_deal):
    with transaction.atomic():
        try:
            trade = Trade.objects.get(transaction_broker_id=ticket)
        except Trade.DoesNotExist:
            error_msg = f"No Trade found with transaction_broker_id {ticket}"
            logger.error(error_msg)
            return None

        trade.close_time = close_time
        trade.close_price = close_price
        trade.pnl = pnl
        trade.pnl_excluding_commission = pnl_excluding_commission
        trade.closing_reason = closing_reason

        # Optionally, update max_drawdown and max_profit if available
        trade.max_drawdown = closed_deal.get('max_drawdown', trade.max_drawdown)
        trade.max_profit = closed_deal.get('max_profit', trade.max_profit)

        trade.save()
        logger.info(f"Updated Trade ID {trade.id} with closing details.")

        # Send a notification about the closed trade
        logger.info({
            "event": "trade_closed",
            "trade_id": trade.id,
            "symbol": trade.symbol,
            "close_time": trade.close_time.isoformat(),
            "close_price": str(trade.close_price),
            "pnl": str(trade.pnl),
            "pnl_excluding_commission": str(trade.pnl_excluding_commission),
            "closing_reason": trade.closing_reason,
        })