import traceback
import logging
import pandas as pd

from django.db import transaction

from app.utils.api.data import symbol_info
from app.nexus.models import Trade, TradeClosePricesMutation

logger = logging.getLogger(__name__)

def mutate_trade(position, current_time, new_sl_price_including_commission, pnl_at_new_sl_including_commission):
    try:        
        with transaction.atomic():
            trade = Trade.objects.get(transaction_broker_id=position.ticket)
            TradeClosePricesMutation.objects.create(
                trade=trade,
                mutation_time=current_time,
                mutation_price=position.price_current,
                new_sl_price=new_sl_price_including_commission,
                pnl_at_new_sl_price=pnl_at_new_sl_including_commission
            )
            logger.info(f"Created TradeClosePricesMutation for Trade ID {trade.id}")
            
    except Trade.DoesNotExist:
        error_msg = f"No Trade found with transaction_broker_id {position.ticket}"
        logger.error(error_msg)

    except Exception as e:
        error_msg = f"Error creating TradeClosePricesMutation: {e}\n{traceback.format_exc()}"
        logger.error(error_msg)
