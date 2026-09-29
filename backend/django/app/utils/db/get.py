import traceback
import logging
from typing import Optional, Dict, Any

from app.nexus.models import Trade, TradeClosePricesMutation

logger = logging.getLogger(__name__)

def get_trade_with_mutations(ticket: int) -> Optional[Dict[str, Any]]:
    try:
        trade = Trade.objects.filter(transaction_broker_id=ticket).first()
        if not trade:
            logger.error(f"No trade found with ticket {ticket}")
            return None

        mutations = TradeClosePricesMutation.objects.filter(trade=trade)
        # Convert queryset to list if you need to serialize it
        mutations_list = list(mutations.values())
        
        return {
            "trade": trade,
            "mutations": mutations_list
        }
    except Exception as e:
        error_msg = f"Error fetching trade with mutations: {e}\n{traceback.format_exc()}"
        logger.error(error_msg)
        return None