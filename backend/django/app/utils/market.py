from datetime import datetime, timedelta

from app.utils.constants import CRYPTOCURRENCIES, TIMEZONE
from app.utils.api.data import fetch_data_pos, symbol_info_tick

def is_market_open(symbol):
    if symbol in CRYPTOCURRENCIES:
        return True
    else:
        # Check whether the market is open, if it's a crypto then market doesn't close
        tick = symbol_info_tick(symbol)
        if tick is not None and not tick.empty:
            # Extract the first timestamp from the Series
            tick_time = datetime.fromtimestamp(tick.time.iloc[0], tz=TIMEZONE)
            current_time = datetime.now(TIMEZONE)
            time_difference = current_time - tick_time

            if time_difference > timedelta(minutes=5):
                return False
            else:
                return True
        else:
            return False