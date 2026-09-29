# backend/django/app/quant/algorithms/mean_reversion/trailing-stop.py

import traceback
import logging
from dotenv import load_dotenv
import os
from datetime import datetime, timedelta
from time import sleep, perf_counter

import requests
import pandas as pd

from app.utils.arithmetics import (
    calculate_order_capital,
    calculate_order_size_usd,
    calculate_commission,
    convert_lots_to_usd,
    convert_usd_to_lots,
    calculate_trade_volume,
    get_price_at_pnl,
    get_pnl_at_price
)
from app.utils.constants import MT5Timeframe, TIMEZONE
from app.utils.api.data import fetch_data_pos, symbol_info_tick
from app.utils.api.positions import get_positions
from app.utils.api.order import modify_sl_tp
from app.utils.api.ticket import get_order_from_ticket, get_deal_from_ticket
from app.utils.db.mutation import mutate_trade
from app.utils.db.get import get_trade_with_mutations
from app.quant.algorithms.mean_reversion.config import (
    PAIRS,
    MAIN_TIMEFRAME,
    TP_PNL_MULTIPLIER,
    SL_PNL_MULTIPLIER,
    LEVERAGE,
    DEVIATION,
    CAPITAL_PER_TRADE,
    TRAILING_STOP_STEPS
)

load_dotenv()
logger = logging.getLogger(__name__)

EPSILON = 1e-4  # Define an appropriate epsilon value


def trailing_stop_algorithm():
    """
    Continuously monitors open trades, detects closed trades, manages trailing stops,
    and sends notifications. Utilizes a cached state to detect changes in open positions
    and interacts with the MT5 API and Django models.
    """

    try:
        current_time = datetime.now(TIMEZONE).replace(microsecond=0)
        positions = get_positions()

        if positions.empty:
            logger.info('No positions found')
            return

        for index, position in positions.iterrows():
            logger.info('Starting position timer')
            position_start_time = perf_counter()  # Start timing for the position

            # Time calculation of each position
            # Check if the position ticket exists in trades dict
            trade_with_mutations = get_trade_with_mutations(position.ticket)

            if trade_with_mutations is None:
                error_msg = f"No trade found with ticket {position.ticket}"
                logger.error(error_msg)
                continue

            trade = trade_with_mutations.get("trade")
            mutations = trade_with_mutations.get("mutations", [])
    
            current_sl_pnl, current_sl_pnl_excluding_commission = get_pnl_at_price(
                position.sl, position.price_open, trade.position_size_usd, trade.leverage,
                trade.type, trade.order_commission
            )

            for trailing_step in TRAILING_STOP_STEPS:
                logger.info('Starting trailing step timer')
                trailing_start_time = perf_counter()  # Start timing for the trailing step

                # Time calculation of each trailing stop
                trigger_pnl = trade.capital * trailing_step['trigger_pnl_multiplier']
                new_sl_pnl = trade.capital * trailing_step['new_sl_pnl_multiplier']

                trigger_price, trigger_price_excluding_commission = get_price_at_pnl(
                    desired_pnl=trigger_pnl,
                    entry_price=position.price_open,
                    commission=trade.order_commission,
                    order_size_usd=trade.position_size_usd,
                    leverage=trade.leverage,
                    type=trade.type
                )
        
                new_sl_price, new_sl_price_excluding_commission = get_price_at_pnl(
                    desired_pnl=new_sl_pnl,
                    commission=trade.order_commission,
                    order_size_usd=trade.position_size_usd,
                    leverage=trade.leverage,
                    entry_price=position.price_open,
                    type=trade.type
                )          

                trigger_pnl, trigger_pnl_excluding_commission = get_pnl_at_price(
                    current_price=trigger_price,
                    entry_price=position.price_open,
                    order_size_usd=trade.position_size_usd,
                    leverage=trade.leverage,
                    type=trade.type,
                    commission=trade.order_commission
                )
    
                pnl_at_new_sl, pnl_at_new_sl_excluding_commission = get_pnl_at_price(
                    current_price=new_sl_price,
                    entry_price=position.price_open,
                    order_size_usd=trade.position_size_usd,
                    leverage=trade.leverage,
                    type=trade.type,
                    commission=trade.order_commission
                )

                nothing_is_none = position.profit is not None and trigger_pnl is not None and position.sl is not None and new_sl_price is not None
                
                # Profit is higher than required trigger in this step
                if nothing_is_none and position.profit >= trigger_pnl:
                    # New SL is better than current SL
                    if (trade.type == 'BUY' and new_sl_price > position.sl + EPSILON) or (trade.type == 'SELL' and new_sl_price < position.sl - EPSILON):
                        sl_info = {
                            'event': 'trailing_stop_triggered',
                            'position_data': {
                                'symbol': position.symbol,
                                'trade_open_date': position.time.isoformat(),
                                'type': trade.type,
                                'entry_price': f"${position.price_open:.5f}",
                                'current_price': f"${position.price_current:.5f}",
                                'capital_used': f"${trade.capital:.5f}",
                                'position_size': f"${trade.position_size_usd:.5f}",
                                'deduced_volume': f"${calculate_trade_volume(position.price_open, position.price_current, position.profit, trade.leverage):.5f}",
                                'deduced_volume_lots': f"${convert_usd_to_lots(position.symbol, trade.position_size_usd, trade.type):.5f}",
                                'commission': f"${trade.order_commission:.5f}",
                            },
                            'trigger_data': {
                                'trigger_price': f"${trigger_price:.5f}",
                                'trigger_pnl': f"${trigger_pnl:.5f}",
                                'trigger_pnl_percentage': f"{(trigger_pnl / trade.capital) * 100:.5f}%",
                                'trigger_price_excluding_commission': f"${trigger_price_excluding_commission:.5f}",
                                'trigger_pnl_excluding_commission': f"${trigger_pnl_excluding_commission:.5f}",
                                'trigger_pnl_excluding_commission_percentage': f"{(trigger_pnl_excluding_commission / trade.capital) * 100:.5f}%",
                            },
                            'current_pnl': {
                                'current_pnl': f"${position.profit:.5f}",
                            },
                            'old_sl': {
                                'old_sl': f"${position.sl:.5f}",
                                'pnl_at_old_sl': f"${current_sl_pnl:.5f}",
                                'old_sl_pnl_percentage': f"{(current_sl_pnl / trade.capital) * 100:.5f}%",
                            },
                            'new_sl': {
                                'new_sl': f"${new_sl_price:.5f}",
                                'pnl_at_new_sl': f"${pnl_at_new_sl:.5f}",
                                'new_sl_pnl_percentage': f"{(pnl_at_new_sl / trade.capital) * 100:.5f}%",
                                'new_sl_excluding_commission': f"${new_sl_price_excluding_commission:.5f}",
                                'pnl_at_new_sl_excluding_commission': f"${pnl_at_new_sl_excluding_commission:.5f}",
                                'new_sl_pnl_excluding_commission_percentage': f"{(pnl_at_new_sl_excluding_commission / trade.capital) * 100:.5f}%",
                            }
                        }

                        # Modify the Stop Loss and Take Profit
                        modify_request = modify_sl_tp(position, new_sl_price)
                        if modify_request is not None:
                            logger.info({'message': 'successfully modified sl from mt5 api', 'modify_request': modify_request, 'sl_info': sl_info})

                            # Create a mutation record in the database
                            mutation = mutate_trade(position, current_time, new_sl_price, pnl_at_new_sl)
                            if mutation is not None:
                                logger.info({'message': 'mutation created', 'mutation': mutation})
                            else:
                                logger.info({'message': 'mutation creation failed', 'sl_info': sl_info})
                        else:
                            logger.info({'message': 'failed to modify sl from mt5 api', 'sl_info': sl_info})
                        
                        # End timing for the trailing step
                        trailing_end_time = perf_counter()
                        trailing_duration = trailing_end_time - trailing_start_time
                        logger.info(f"Processed trailing_step for position {position.ticket} in {trailing_duration:.4f} seconds.")
                        
                        break  # Exit the trailing steps loop after modification
                    # else:
                        # print(f"Warning: Stop Loss is None for position {position.ticket}")
                # else:
                    # print(f"Warning: Profit or trigger PNL is None for position {position.ticket}")

            # End timing for the position
            position_end_time = perf_counter()
            position_duration = position_end_time - position_start_time
            logger.info(f"Processed position {position.ticket} in {position_duration:.4f} seconds.")

    except Exception as e:
        error_msg = f"Exception in trailing_stop_algorithm: {e}\n{traceback.format_exc()}"
        logger.error(error_msg)

