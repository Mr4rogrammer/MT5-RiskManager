from flask import Blueprint, request, jsonify
import MetaTrader5 as mt5
import math


risk_bp = Blueprint("order", __name__)


def normalize_volume(volume, symbol_info):

    min_volume = symbol_info.volume_min
    max_volume = symbol_info.volume_max
    volume_step = symbol_info.volume_step

    # Do not exceed broker maximum
    volume = min(volume, max_volume)

    # Round DOWN so risk never exceeds requested risk
    volume = math.floor(volume / volume_step) * volume_step

    return round(volume, 8)


@risk_bp.route("/riskValue", methods=["POST"])
def calculate_risk_value():

    try:

        data = request.get_json()

        if not data:
            return jsonify({
                "success": False,
                "message": "Request body is required"
            }), 400

        # -----------------------------------------
        # Request values
        # -----------------------------------------

        symbol = data.get("symbol")
        side = data.get("side")
        sl = data.get("sl")
        tp = data.get("tp")
        risk_value = data.get("riskValue")

        # -----------------------------------------
        # Validation
        # -----------------------------------------

        if not symbol:
            return jsonify({
                "success": False,
                "message": "symbol is required"
            }), 400

        if not side:
            return jsonify({
                "success": False,
                "message": "side is required"
            }), 400

        if sl is None:
            return jsonify({
                "success": False,
                "message": "sl is required"
            }), 400

        if tp is None:
            return jsonify({
                "success": False,
                "message": "tp is required"
            }), 400

        if risk_value is None:
            return jsonify({
                "success": False,
                "message": "riskValue is required"
            }), 400

        symbol = symbol.upper()
        side = side.upper()

        sl = float(sl)
        tp = float(tp)
        risk_value = float(risk_value)

        if side not in ["BUY", "SELL"]:
            return jsonify({
                "success": False,
                "message": "side must be BUY or SELL"
            }), 400

        if risk_value <= 0:
            return jsonify({
                "success": False,
                "message": "riskValue must be greater than 0"
            }), 400

        # -----------------------------------------
        # Check MT5
        # -----------------------------------------

        if not mt5.terminal_info():

            if not mt5.initialize():

                return jsonify({
                    "success": False,
                    "message": "Unable to initialize MT5",
                    "error": str(mt5.last_error())
                }), 500

        # -----------------------------------------
        # Symbol
        # -----------------------------------------

        symbol_info = mt5.symbol_info(symbol)

        if symbol_info is None:

            return jsonify({
                "success": False,
                "message": f"Symbol not found: {symbol}"
            }), 400

        # Make sure symbol is visible
        if not symbol_info.visible:

            if not mt5.symbol_select(symbol, True):

                return jsonify({
                    "success": False,
                    "message": f"Unable to select symbol: {symbol}"
                }), 400

        # -----------------------------------------
        # Current tick
        # -----------------------------------------

        tick = mt5.symbol_info_tick(symbol)

        if tick is None:

            return jsonify({
                "success": False,
                "message": f"Unable to get tick for {symbol}"
            }), 400

        # -----------------------------------------
        # Entry price
        # -----------------------------------------

        if side == "BUY":

            entry = tick.ask
            order_type = mt5.ORDER_TYPE_BUY

        else:

            entry = tick.bid
            order_type = mt5.ORDER_TYPE_SELL

        # -----------------------------------------
        # Validate SL / TP
        # -----------------------------------------

        if side == "BUY":

            if sl >= entry:
                return jsonify({
                    "success": False,
                    "message": "BUY: SL must be below current Ask"
                }), 400

            if tp <= entry:
                return jsonify({
                    "success": False,
                    "message": "BUY: TP must be above current Ask"
                }), 400

        else:

            if sl <= entry:
                return jsonify({
                    "success": False,
                    "message": "SELL: SL must be above current Bid"
                }), 400

            if tp >= entry:
                return jsonify({
                    "success": False,
                    "message": "SELL: TP must be below current Bid"
                }), 400

        # -----------------------------------------
        # Calculate loss for 1 lot
        # -----------------------------------------

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
                "message": "Unable to calculate risk",
                "error": str(mt5.last_error())
            }), 400

        loss_for_one_lot = abs(loss_for_one_lot)

        if loss_for_one_lot <= 0:

            return jsonify({
                "success": False,
                "message": "Calculated risk is zero"
            }), 400

        # -----------------------------------------
        # Calculate lot size
        # -----------------------------------------

        raw_lot = risk_value / loss_for_one_lot

        # -----------------------------------------
        # Broker volume rules
        # -----------------------------------------

        min_lot = symbol_info.volume_min
        max_lot = symbol_info.volume_max
        lot_step = symbol_info.volume_step

        if raw_lot < min_lot:

            return jsonify({
                "success": False,
                "message": "Risk value is too small for minimum lot size",
                "minimumLot": min_lot,
                "calculatedLot": raw_lot
            }), 400

        lot = min(raw_lot, max_lot)

        # Round DOWN
        lot = math.floor(lot / lot_step) * lot_step

        lot = round(lot, 8)

        # -----------------------------------------
        # Calculate actual risk
        # -----------------------------------------

        actual_risk = mt5.order_calc_profit(
            order_type,
            symbol,
            lot,
            entry,
            sl
        )

        if actual_risk is None:

            return jsonify({
                "success": False,
                "message": "Unable to calculate actual risk",
                "error": str(mt5.last_error())
            }), 400

        actual_risk = abs(actual_risk)

        # -----------------------------------------
        # Calculate reward
        # -----------------------------------------

        reward = mt5.order_calc_profit(
            order_type,
            symbol,
            lot,
            entry,
            tp
        )

        if reward is None:

            return jsonify({
                "success": False,
                "message": "Unable to calculate reward",
                "error": str(mt5.last_error())
            }), 400

        reward = abs(reward)

        # -----------------------------------------
        # Risk Reward
        # -----------------------------------------

        risk_reward = 0

        if actual_risk > 0:
            risk_reward = reward / actual_risk

        # -----------------------------------------
        # Response
        # -----------------------------------------

        return jsonify({

            "success": True,

            "symbol": symbol,
            "side": side,

            "market": {
                "bid": tick.bid,
                "ask": tick.ask
            },

            "trade": {
                "entry": entry,
                "sl": sl,
                "tp": tp
            },

            "risk": {
                "maxRisk": risk_value,
                "actualRisk": round(actual_risk, 2),
                "reward": round(reward, 2),
                "riskReward": round(risk_reward, 2)
            },

            "volume": {
                "lot": lot,
                "min": min_lot,
                "max": max_lot,
                "step": lot_step
            }

        }), 200

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