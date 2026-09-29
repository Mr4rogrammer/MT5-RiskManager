from flask import Blueprint, request, jsonify
import MetaTrader5 as mt5
import math


risk_bp = Blueprint("order", __name__)



@risk_bp.route("/lot-size", methods=["POST"])
def calculate_lot_size():

    try:
        data = request.get_json()

        symbol = data["symbol"].upper()
        side = data["side"].upper()
        sl = float(data["sl"])
        risk_value = float(data["riskValue"])

        # Initialize MT5
        if not mt5.initialize():
            return jsonify({
                "success": False,
                "message": "MT5 initialization failed"
            }), 500

        # Get symbol information
        symbol_info = mt5.symbol_info(symbol)

        if symbol_info is None:
            return jsonify({
                "success": False,
                "message": f"Symbol not found: {symbol}"
            }), 400

        # Make symbol visible if required
        if not symbol_info.visible:
            if not mt5.symbol_select(symbol, True):
                return jsonify({
                    "success": False,
                    "message": f"Unable to select {symbol}"
                }), 400

        # Get current price
        tick = mt5.symbol_info_tick(symbol)

        if tick is None:
            return jsonify({
                "success": False,
                "message": f"Unable to get current price for {symbol}"
            }), 400

        # BUY uses ASK
        # SELL uses BID
        if side == "BUY":

            entry = tick.ask
            order_type = mt5.ORDER_TYPE_BUY

            if sl >= entry:
                return jsonify({
                    "success": False,
                    "message": "BUY SL must be below current Ask"
                }), 400

        elif side == "SELL":

            entry = tick.bid
            order_type = mt5.ORDER_TYPE_SELL

            if sl <= entry:
                return jsonify({
                    "success": False,
                    "message": "SELL SL must be above current Bid"
                }), 400

        else:

            return jsonify({
                "success": False,
                "message": "side must be BUY or SELL"
            }), 400

        # Calculate loss for 1 lot
        loss_for_one_lot = mt5.order_calc_profit(
            order_type,
            symbol,
            1.0,
            entry,
            sl
        )

        if loss_for_one_lot is None:
            return jsonify({
                "success": False,
                "message": f"MT5 calculation failed: {mt5.last_error()}"
            }), 400

        loss_for_one_lot = abs(loss_for_one_lot)

        if loss_for_one_lot <= 0:
            return jsonify({
                "success": False,
                "message": "SL risk calculation is zero"
            }), 400

        # Calculate required lot
        raw_lot = risk_value / loss_for_one_lot

        # Broker volume rules
        min_lot = symbol_info.volume_min
        max_lot = symbol_info.volume_max
        lot_step = symbol_info.volume_step

        # If calculated lot is below minimum
        if raw_lot < min_lot:
            return jsonify({
                "success": False,
                "message": "Risk value is too small for minimum lot size"
            }), 400

        # Don't exceed broker maximum
        lot = min(raw_lot, max_lot)

        # Round DOWN to broker step
        lot = math.floor(lot / lot_step) * lot_step

        lot = round(lot, 8)

        return jsonify({
            "lotSize": lot
        }), 200

    except KeyError as e:

        return jsonify({
            "success": False,
            "message": f"Missing field: {e.args[0]}"
        }), 400

    except ValueError:

        return jsonify({
            "success": False,
            "message": "Invalid numeric value"
        }), 400

    except Exception as e:

        return jsonify({
            "success": False,
            "message": str(e)
        }), 500