from flask import Blueprint, jsonify
from flasgger import swag_from
import MetaTrader5 as mt5
import bot_common
import crt_bot
import candle_two_bot

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
        "settings": crt_bot.settings,
        "fees": bot_common.fees,
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
    description: Stop new CRT entries. Open positions keep SL/TP and break-even still runs.
    """
    crt_bot.disable()
    return jsonify({"enabled": False})


# ---------------------------------------------------------------------------
# 2-candle CRT bot (candle_two_bot.py)
# ---------------------------------------------------------------------------

@bot_bp.route('/bot2/status', methods=['GET'])
@swag_from({
    'tags': ['Bot'],
    'responses': {
        200: {'description': '2-candle CRT bot status, settings and recent signals/orders.'}
    }
})
def bot2_status():
    """
    2-Candle CRT Bot Status
    ---
    description: Whether the 2-candle CRT bot is trading, its settings, and the last 100 signals/orders.
    """
    terminal = mt5.terminal_info()
    return jsonify({
        "enabled": candle_two_bot.is_enabled(),
        "algo_trading": terminal.trade_allowed if terminal is not None else None,
        "timeframes": list(crt_bot.TIMEFRAMES),
        "settings": candle_two_bot.settings,
        "fees": bot_common.fees,
        "events": list(candle_two_bot.events),
    })


@bot_bp.route('/bot2/start', methods=['POST'])
@swag_from({
    'tags': ['Bot'],
    'responses': {200: {'description': '2-candle CRT bot enabled.'}}
})
def bot2_start():
    """
    Start 2-Candle CRT Bot
    ---
    description: Enable automatic 2-candle CRT trading.
    """
    candle_two_bot.enable()
    return jsonify({"enabled": True})


@bot_bp.route('/bot2/stop', methods=['POST'])
@swag_from({
    'tags': ['Bot'],
    'responses': {200: {'description': '2-candle CRT bot disabled.'}}
})
def bot2_stop():
    """
    Stop 2-Candle CRT Bot
    ---
    description: Stop new 2-candle CRT entries. Open positions keep SL/TP and break-even still runs.
    """
    candle_two_bot.disable()
    return jsonify({"enabled": False})


# ---------------------------------------------------------------------------
# Generic routes — every bot built on bot_common.Bot, including future ones
# ---------------------------------------------------------------------------

def _bot_state(bot):
    return {
        "name": bot.name,
        "title": bot.title,
        "description": bot.description,
        "enabled": bot.enabled.is_set(),
        "settings": bot.settings,
    }


@bot_bp.route('/bots', methods=['GET'])
@swag_from({'tags': ['Bot'], 'responses': {200: {'description': 'All bots and whether they are trading.'}}})
def bots_list():
    """
    List Bots
    ---
    description: Every registered bot (name, title, enabled, settings).
    """
    return jsonify([_bot_state(b) for b in bot_common.BOTS.values()])


@bot_bp.route('/bots/<name>/status', methods=['GET'])
@swag_from({'tags': ['Bot'], 'parameters': [{'name': 'name', 'in': 'path', 'type': 'string', 'required': True}],
            'responses': {200: {'description': 'Bot status and last 100 events.'}, 404: {'description': 'Unknown bot.'}}})
def bots_status(name):
    """
    Bot Status
    ---
    description: Settings, enabled flag and the last 100 events of one bot.
    """
    bot = bot_common.BOTS.get(name)
    if bot is None:
        return jsonify({"error": f"unknown bot '{name}'", "bots": list(bot_common.BOTS)}), 404
    terminal = mt5.terminal_info()
    return jsonify({**_bot_state(bot),
                    "algo_trading": terminal.trade_allowed if terminal is not None else None,
                    "fees": bot_common.fees,
                    "events": list(bot.events)})


@bot_bp.route('/bots/<name>/<action>', methods=['POST'])
@swag_from({'tags': ['Bot'], 'parameters': [{'name': 'name', 'in': 'path', 'type': 'string', 'required': True},
                                            {'name': 'action', 'in': 'path', 'type': 'string', 'enum': ['start', 'stop'], 'required': True}],
            'responses': {200: {'description': 'New enabled state.'}, 404: {'description': 'Unknown bot or action.'}}})
def bots_control(name, action):
    """
    Start / Stop a Bot
    ---
    description: Start or stop new entries for one bot. Open trades keep SL/TP and break-even still runs.
    """
    bot = bot_common.BOTS.get(name)
    if bot is None or action not in ("start", "stop"):
        return jsonify({"error": "unknown bot or action", "bots": list(bot_common.BOTS)}), 404
    if action == "start":
        bot.enabled.set()
    else:
        bot.enabled.clear()
    return jsonify({"name": name, "enabled": bot.enabled.is_set()})
