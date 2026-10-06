from flask import Blueprint, jsonify
from flasgger import swag_from
import MetaTrader5 as mt5
import crt_bot

bot_bp = Blueprint('bot', __name__)


@bot_bp.route('/bot/status', methods=['GET'])
@swag_from({
    'tags': ['Bot'],
    'responses': {
        200: {'description': 'CRT bot status, settings and recent signals/orders.'}
    }
})
def bot_status():
    """
    CRT Bot Status
    ---
    description: Whether the CRT bot is trading, its settings, and the last 100 signals/orders.
    """
    terminal = mt5.terminal_info()
    return jsonify({
        "enabled": crt_bot.is_enabled(),
        # MT5 "Algo Trading" button — must be true for orders to go through
        "algo_trading": terminal.trade_allowed if terminal is not None else None,
        "timeframes": list(crt_bot.TIMEFRAMES),
        # sets (e.g. commission_pct_symbols) are not JSON serializable
        "settings": {k: sorted(v) if isinstance(v, set) else v
                     for k, v in crt_bot.settings.items()},
        "events": list(crt_bot.events),
    })


@bot_bp.route('/bot/start', methods=['POST'])
@swag_from({
    'tags': ['Bot'],
    'responses': {200: {'description': 'CRT bot enabled.'}}
})
def bot_start():
    """
    Start CRT Bot
    ---
    description: Enable automatic CRT trading.
    """
    crt_bot.enable()
    return jsonify({"enabled": True})


@bot_bp.route('/bot/stop', methods=['POST'])
@swag_from({
    'tags': ['Bot'],
    'responses': {200: {'description': 'CRT bot disabled.'}}
})
def bot_stop():
    """
    Stop CRT Bot
    ---
    description: Disable automatic CRT trading. Open positions keep their SL/TP.
    """
    crt_bot.disable()
    return jsonify({"enabled": False})
