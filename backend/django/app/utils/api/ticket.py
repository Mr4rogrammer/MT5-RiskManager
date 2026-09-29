import os
import requests
from typing import Dict
import pandas as pd
from datetime import datetime, timedelta
from dotenv import load_dotenv
import logging
import traceback
from app.utils.constants import MT5Timeframe
from app.utils.constants import TIMEZONE

load_dotenv()
logger = logging.getLogger(__name__)

BASE_URL = os.getenv('MT5_API_URL')

def history_deals_get(from_date: datetime, to_date: datetime, position: int = None) -> Dict:
    try:
        params = {
            'from_date': from_date.isoformat(),
            'to_date': to_date.isoformat()
        }
        
        if position is not None:
            params['position'] = position
            
        url = f"{BASE_URL}/history_deals_get"
        response = requests.get(url, params=params)
        response.raise_for_status()
        
        return response.json()
    except Exception as e:
        error_msg = f"Exception fetching history deals: {e}\n{traceback.format_exc()}"
        logger.error(error_msg)

def history_orders_get(ticket: int) -> Dict:
    try:
        params = {'ticket': ticket}
            
        url = f"{BASE_URL}/history_orders_get"
        response = requests.get(url, params=params)
        response.raise_for_status()
        
        return response.json()
    except Exception as e:
        error_msg = f"Exception fetching history orders for ticket {ticket}: {e}\n{traceback.format_exc()}"
        logger.error(error_msg)

def get_deal_from_ticket(ticket: int, from_date: datetime, to_date: datetime) -> Dict:
    # Convert datetime to MT5 time (integer)
    from_timestamp = int(from_date.timestamp())
    to_timestamp = int(to_date.timestamp())

    # Retrieve deals using the specified date range and position
    deals = history_deals_get(from_timestamp, to_timestamp, position=ticket)
    if not deals:
        error_msg = f"No deal history found for position ticket {ticket} between {from_date} and {to_date}."
        logger.error(error_msg)
        return None

    # Convert deals to a DataFrame for easier processing
    deals_df = pd.DataFrame(deals)

    # Optional: Verify that all deals belong to the same symbol
    if not deals_df.empty and not all(deal == deals_df['symbol'].iloc[0] for deal in deals_df['symbol']):
        error_msg = f"Inconsistent symbols found in deals for position ticket {ticket}."
        logger.error(error_msg)
        return None

    # Extract relevant information
    if not deals_df.empty:
        deal_details = {
            'ticket': ticket,
            'symbol': deals_df['symbol'].iloc[0],
            'type': 'BUY' if deals_df['type'].iloc[0] == 'DEAL_TYPE_BUY' else 'SELL',
            'volume': deals_df['volume'].sum(),
            'open_time': datetime.fromtimestamp(deals_df['time'].min(), tz=TIMEZONE),
            'close_time': datetime.fromtimestamp(deals_df['time'].max(), tz=TIMEZONE),
            'open_price': deals_df['price'].iloc[0],
            'close_price': deals_df['price'].iloc[-1],
            'profit': deals_df['profit'].sum(),
            'commission': deals_df['commission'].sum(),
            'swap': deals_df['swap'].sum(),
            'comment': deals_df['comment'].iloc[-1]  # Use the last comment if multiple
        }
        return deal_details
    else:
        return None


def get_order_from_ticket(ticket: int) -> Dict:
    # Get the order history
    orders = history_orders_get(ticket=ticket)
    if orders is None or len(orders) == 0:
        error_msg = f"No order history found for ticket {ticket}"
        logger.error(error_msg)
        return None

    # Directly use the dictionary without calling _asdict()
    order_dict = orders[0]

    return order_dict