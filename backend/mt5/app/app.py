import logging
import os
from flask import Flask
from dotenv import load_dotenv
import MetaTrader5 as mt5
from flasgger import Swagger
from swagger import swagger_config
from auth import require_api_key
from ws import register_ws

# Import routes
from routes.health import health_bp
from routes.symbol import symbol_bp
from routes.data import data_bp
from routes.position import position_bp
from routes.order import order_bp
from routes.history import history_bp
from routes.error import error_bp
from routes.account import account_bp
from routes.config import config_bp
from routes.riskValue import risk_bp
from routes.bot import bot_bp
from crt_bot import start_crt_bot
from candle_two_bot import start_candle_two_bot
from daily_sweep_bot import start_daily_sweep_bot
from indicator_bots import start_indicator_bots

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

swagger = Swagger(app, config=swagger_config)

# ----------------------------------------------------------------
# Register WebSocket endpoint (/ws) — uses the same Flask port.
# Starts ONE tick loop and ONE account loop as daemon threads.
# ----------------------------------------------------------------
#register_ws(app)

# ----------------------------------------------------------------
# Register global authentication — runs before every request.
# Public routes (health, swagger) are exempt (see auth.py).
# ----------------------------------------------------------------
app.before_request(require_api_key)

# Register blueprints
app.register_blueprint(health_bp)
app.register_blueprint(symbol_bp)
app.register_blueprint(data_bp)
app.register_blueprint(position_bp)
app.register_blueprint(order_bp)
app.register_blueprint(history_bp)
app.register_blueprint(error_bp)
app.register_blueprint(account_bp)
app.register_blueprint(config_bp)
app.register_blueprint(risk_bp)
app.register_blueprint(bot_bp)

if __name__ == '__main__':
    if not mt5.initialize():
        logger.error("Failed to initialize MT5.")
    start_crt_bot()
    start_candle_two_bot()
    start_daily_sweep_bot()
    start_indicator_bots()
    app.run(host='0.0.0.0', port=int(os.environ.get('MT5_API_PORT', 5001)))