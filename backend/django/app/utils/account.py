import pandas as pd
import logging
import traceback

from app.utils.api.positions import get_positions

logger = logging.getLogger(__name__)

def have_open_positions_in_symbol(symbol):
    try:
        positions = get_positions()
        # Handle empty DataFrame case
        if not isinstance(positions, pd.DataFrame):
            positions = pd.DataFrame(positions)  # Convert to DataFrame if it's not already

        if len(positions) == 0:
            return False
            
        return symbol in positions['symbol'].values if 'symbol' in positions.columns else False
    except Exception as e:
        logger.error(f"Error in have_open_positions_in_symbol: {e}\n{traceback.format_exc()}")
        return False