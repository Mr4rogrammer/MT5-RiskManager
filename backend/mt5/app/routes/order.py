from flask import Blueprint, jsonify, request
import MetaTrader5 as mt5
import logging
from flasgger import swag_from

order_bp = Blueprint('order', __name__)

logger = logging.getLogger(__name__)


def normalize_order_type(order_type):
    """
    Convert API string BUY/SELL to MT5 order type constant.
    """
    if isinstance(order_type, str):
        order_type = order_type.upper().strip()

        if order_type == "BUY":
            return mt5.ORDER_TYPE_BUY

        if order_type == "SELL":
            return mt5.ORDER_TYPE_SELL

    # Also allow numeric MT5 constants
    if order_type in (mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_SELL):
        return order_type

    return None


def normalize_filling_type(filling_type):
    """
    Convert API string filling mode to MT5 filling constant.
    """

    if filling_type is None:
        return mt5.ORDER_FILLING_IOC

    if isinstance(filling_type, str):
        filling_type = filling_type.upper().strip()

        filling_modes = {
            "ORDER_FILLING_IOC": mt5.ORDER_FILLING_IOC,
            "ORDER_FILLING_FOK": mt5.ORDER_FILLING_FOK,
            "ORDER_FILLING_RETURN": mt5.ORDER_FILLING_RETURN,
        }

        return filling_modes.get(filling_type)

    # Also allow numeric MT5 constants
    if filling_type in (
        mt5.ORDER_FILLING_IOC,
        mt5.ORDER_FILLING_FOK,
        mt5.ORDER_FILLING_RETURN,
    ):
        return filling_type

    return None


def result_to_dict(result):
    """
    Safely convert MT5 result namedtuple to dictionary.
    """
    if result is None:
        return None

    try:
        return result._asdict()
    except Exception:
        return str(result)


@order_bp.route('/order', methods=['POST'])
@swag_from({
    'tags': ['Order'],
    'parameters': [
        {
            'name': 'body',
            'in': 'body',
            'required': True,
            'schema': {
                'type': 'object',
                'properties': {
                    'symbol': {'type': 'string'},
                    'volume': {'type': 'number'},
                    'type': {
                        'type': 'string',
                        'enum': ['BUY', 'SELL']
                    },
                    'deviation': {
                        'type': 'integer',
                        'default': 20
                    },
                    'magic': {
                        'type': 'integer',
                        'default': 0
                    },
                    'comment': {
                        'type': 'string',
                        'default': ''
                    },
                    'type_filling': {
                        'type': 'string',
                        'enum': [
                            'ORDER_FILLING_IOC',
                            'ORDER_FILLING_FOK',
                            'ORDER_FILLING_RETURN'
                        ]
                    },
                    'sl': {'type': 'number'},
                    'tp': {'type': 'number'}
                },
                'required': [
                    'symbol',
                    'volume',
                    'type'
                ]
            }
        }
    ],
    'responses': {
        200: {
            'description': 'Order executed successfully.'
        },
        400: {
            'description': 'Bad request or order failed.'
        },
        500: {
            'description': 'Internal server error.'
        }
    }
})
def send_market_order_endpoint():

    try:

        # ------------------------------------------------------------
        # Read JSON
        # ------------------------------------------------------------

        data = request.get_json(silent=True)

        if not data:
            return jsonify({
                "error": "Order data is required"
            }), 400

        logger.info("Received order request: %s", data)

        # ------------------------------------------------------------
        # Validate required fields
        # ------------------------------------------------------------

        required_fields = [
            'symbol',
            'volume',
            'type'
        ]

        missing_fields = [
            field for field in required_fields
            if field not in data
        ]

        if missing_fields:
            return jsonify({
                "error": "Missing required fields",
                "missing_fields": missing_fields
            }), 400

        # ------------------------------------------------------------
        # Symbol
        # ------------------------------------------------------------

        symbol = str(data['symbol']).upper().strip()

        if not symbol:
            return jsonify({
                "error": "Symbol cannot be empty"
            }), 400

        # ------------------------------------------------------------
        # Volume
        # ------------------------------------------------------------

        try:
            volume = float(data['volume'])
        except (ValueError, TypeError):

            return jsonify({
                "error": "Invalid volume"
            }), 400

        if volume <= 0:
            return jsonify({
                "error": "Volume must be greater than 0"
            }), 400

        # ------------------------------------------------------------
        # Normalize BUY / SELL
        # ------------------------------------------------------------

        original_order_type = data['type']

        order_type = normalize_order_type(
            original_order_type
        )

        if order_type is None:

            return jsonify({
                "error": "Invalid order type",
                "received": original_order_type,
                "allowed": [
                    "BUY",
                    "SELL"
                ]
            }), 400

        # ------------------------------------------------------------
        # Normalize filling mode
        # ------------------------------------------------------------

        original_filling_type = data.get(
            'type_filling',
            'ORDER_FILLING_IOC'
        )

        filling_type = normalize_filling_type(
            original_filling_type
        )

        if filling_type is None:

            return jsonify({
                "error": "Invalid type_filling",
                "received": original_filling_type,
                "allowed": [
                    "ORDER_FILLING_IOC",
                    "ORDER_FILLING_FOK",
                    "ORDER_FILLING_RETURN"
                ]
            }), 400

        # ------------------------------------------------------------
        # Initialize MT5
        # ------------------------------------------------------------

        if not mt5.initialize():

            error = mt5.last_error()

            logger.error(
                "MT5 initialize failed: %s",
                error
            )

            return jsonify({
                "error": "MT5 initialization failed",
                "mt5_error": str(error)
            }), 500

        # ------------------------------------------------------------
        # Check symbol
        # ------------------------------------------------------------

        symbol_info = mt5.symbol_info(symbol)

        if symbol_info is None:

            error = mt5.last_error()

            logger.error(
                "Symbol not found: %s",
                symbol
            )

            return jsonify({
                "error": "Symbol not found",
                "symbol": symbol,
                "mt5_error": str(error)
            }), 400

        # ------------------------------------------------------------
        # Make symbol visible
        # ------------------------------------------------------------

        if not symbol_info.visible:

            if not mt5.symbol_select(symbol, True):

                return jsonify({
                    "error": "Failed to select symbol",
                    "symbol": symbol
                }), 400

        # ------------------------------------------------------------
        # Get current market tick
        # ------------------------------------------------------------

        tick = mt5.symbol_info_tick(symbol)

        if tick is None:

            error = mt5.last_error()

            return jsonify({
                "error": "Failed to get symbol price",
                "symbol": symbol,
                "mt5_error": str(error)
            }), 400

        # ------------------------------------------------------------
        # BUY = ASK
        # SELL = BID
        # ------------------------------------------------------------

        if order_type == mt5.ORDER_TYPE_BUY:

            price = tick.ask

            order_type_name = "BUY"

        else:

            price = tick.bid

            order_type_name = "SELL"

        # ------------------------------------------------------------
        # Validate price
        # ------------------------------------------------------------

        if price is None or price <= 0:

            return jsonify({
                "error": "Invalid market price",
                "symbol": symbol,
                "price": price
            }), 400

        # ------------------------------------------------------------
        # Prepare request
        # ------------------------------------------------------------

        request_data = {
            "action": mt5.TRADE_ACTION_DEAL,

            "symbol": symbol,

            "volume": volume,

            # IMPORTANT:
            # This is now the MT5 numeric constant
            "type": order_type,

            "price": price,

            "deviation": int(
                data.get('deviation', 20)
            ),

            "magic": int(
                data.get('magic', 0)
            ),

            "comment": str(
                data.get('comment', '')
            ),

            "type_time": mt5.ORDER_TIME_GTC,

            # IMPORTANT:
            # This is now the MT5 numeric constant
            "type_filling": filling_type,
        }

        # ------------------------------------------------------------
        # Optional Stop Loss
        # ------------------------------------------------------------

        if 'sl' in data and data['sl'] is not None:

            try:

                sl = float(data['sl'])

                if sl > 0:
                    request_data["sl"] = sl

            except (ValueError, TypeError):

                return jsonify({
                    "error": "Invalid SL value"
                }), 400

        # ------------------------------------------------------------
        # Optional Take Profit
        # ------------------------------------------------------------

        if 'tp' in data and data['tp'] is not None:

            try:

                tp = float(data['tp'])

                if tp > 0:
                    request_data["tp"] = tp

            except (ValueError, TypeError):

                return jsonify({
                    "error": "Invalid TP value"
                }), 400

        # ------------------------------------------------------------
        # Log request
        # ------------------------------------------------------------

        logger.info(
            "Sending MT5 order: %s",
            request_data
        )

        # ------------------------------------------------------------
        # Order check
        # ------------------------------------------------------------

        check_result = mt5.order_check(
            request_data
        )

        logger.info(
            "MT5 order_check result: %s",
            result_to_dict(check_result)
        )

        if check_result is None:

            error = mt5.last_error()

            return jsonify({
                "error": "MT5 order_check failed",
                "mt5_error": str(error),
                "request": request_data
            }), 400

        # ------------------------------------------------------------
        # Send order
        # ------------------------------------------------------------

        result = mt5.order_send(
            request_data
        )

        if result is None:

            error = mt5.last_error()

            logger.error(
                "MT5 order_send returned None: %s",
                error
            )

            return jsonify({
                "error": "MT5 order_send failed",
                "mt5_error": str(error),
                "request": request_data
            }), 400

        result_dict = result_to_dict(result)

        logger.info(
            "MT5 order result: %s",
            result_dict
        )

        # ------------------------------------------------------------
        # Check result
        # ------------------------------------------------------------

        if result.retcode != mt5.TRADE_RETCODE_DONE:

            error_code, error_string = mt5.last_error()

            return jsonify({

                "error": "Order failed",

                "order_type": order_type_name,

                "symbol": symbol,

                "price": price,

                "retcode": result.retcode,

                "comment": result.comment,

                "mt5_error": {
                    "code": error_code,
                    "message": error_string
                },

                "result": result_dict

            }), 400

        # ------------------------------------------------------------
        # SUCCESS
        # ------------------------------------------------------------

        return jsonify({

            "message": "Order executed successfully",

            "order_type": order_type_name,

            "symbol": symbol,

            "volume": volume,

            "price": price,

            "result": result_dict

        }), 200

    except Exception as e:

        logger.exception(
            "Error in send_market_order_endpoint"
        )

        return jsonify({
            "error": "Internal server error",
            "message": str(e)
        }), 500