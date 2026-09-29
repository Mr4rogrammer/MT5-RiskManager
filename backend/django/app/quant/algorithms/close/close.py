import traceback
import logging
from datetime import datetime
from time import sleep

import pandas as pd

from app.utils.api.positions import get_positions
from app.utils.api.ticket import get_order_from_ticket, get_deal_from_ticket
from app.utils.constants import TIMEZONE
from app.utils.db.close import close_trade

logger = logging.getLogger(__name__)

# Dictionary to cache open positions between runs
cached_positions = {}

def close_algorithm():
    """
    Continuously monitors open trades, detects closed trades, and updates their
    corresponding Trade records in the database with closing details.
    """
    global cached_positions

    try:
        current_time = datetime.now(TIMEZONE).replace(microsecond=0)

        # Fetch current open positions
        positions = get_positions()
        if positions.empty:
            positions = pd.DataFrame(columns=[
                'ticket', 'time', 'time_msc', 'time_update', 'time_update_msc', 'type',
                'magic', 'identifier', 'reason', 'volume', 'price_open', 'sl', 'tp',
                'price_current', 'swap', 'profit', 'symbol', 'comment', 'external_id'
            ])

        # Convert time fields to datetime
        positions['time'] = pd.to_datetime(positions['time'], unit='s', utc=True)
        positions['time_update'] = pd.to_datetime(positions['time_update'], unit='s', utc=True)

        # Detect closed trades by comparing cached_positions with current positions
        current_tickets = set(positions['ticket'].values)
        cached_tickets = set(cached_positions.keys())

        # Identify closed tickets
        closed_tickets = cached_tickets - current_tickets

        for ticket in closed_tickets:
            position = cached_positions.pop(ticket)
            sleep(2)  # Optional: delay to ensure the trade is fully processed

            try:
                # Retrieve the closed order and deal details
                closed_order = get_order_from_ticket(ticket)
                closed_deal = get_deal_from_ticket(ticket)

                if closed_deal is None:
                    error_msg = f"Failed to retrieve deal for closed ticket {ticket}."
                    logger.error({
                        "error": error_msg,
                        "ticket": ticket,
                        "position": position.to_dict() if hasattr(position, 'to_dict') else position
                    })
                    continue

                # Extract closing details
                close_time = closed_deal.get('time', current_time)
                close_price = closed_deal.get('price', position.price_current)
                pnl = closed_deal.get('profit', position.profit)
                pnl_excluding_commission = pnl - closed_deal.get('commission', 0)
                closing_reason = closed_deal.get('reason', 'CLOSED')

                # Update the Trade record in the database
                closed_trade = close_trade(position.ticket, close_time, close_price, pnl, pnl_excluding_commission, closing_reason, closed_deal)

                if closed_trade is not None:
                    logger.info({
                        "event": "trade_closed",
                        "trade_id": closed_trade.id,
                        "symbol": closed_trade.symbol,
                    })
                else:
                    error_msg = f"Failed to close trade {ticket}."
                    logger.error({"error": error_msg, "ticket": ticket})

            except Exception as e:
                error_msg = f"Error processing closed ticket {ticket}: {e}\n{traceback.format_exc()}"
                logger.error({"error": error_msg, "ticket": ticket})

        # Update cached_positions with current open positions
        for index, position in positions.iterrows():
            cached_positions[position.ticket] = position

    except Exception as e:
        error_msg = f"Exception in close_algorithm: {e}\n{traceback.format_exc()}"
        logger.error({"error": error_msg})