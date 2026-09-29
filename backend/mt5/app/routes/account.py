from flask import Blueprint, jsonify
import MetaTrader5 as mt5
from flasgger import swag_from
import logging

account_bp = Blueprint('account', __name__)
logger = logging.getLogger(__name__)

@account_bp.route('/account_info', methods=['GET'])
@swag_from({
    'tags': ['Account'],
    'responses': {
        200: {
            'description': 'Account information retrieved successfully.',
            'schema': {
                'type': 'object',
                'properties': {
                    'login': {'type': 'integer'},
                    'trade_mode': {'type': 'integer'},
                    'leverage': {'type': 'integer'},
                    'limit_orders': {'type': 'integer'},
                    'margin_so_mode': {'type': 'integer'},
                    'trade_allowed': {'type': 'boolean'},
                    'trade_expert': {'type': 'boolean'},
                    'margin_mode': {'type': 'integer'},
                    'currency_digits': {'type': 'integer'},
                    'fifo_close': {'type': 'boolean'},
                    'balance': {'type': 'number'},
                    'credit': {'type': 'number'},
                    'profit': {'type': 'number'},
                    'equity': {'type': 'number'},
                    'margin': {'type': 'number'},
                    'margin_free': {'type': 'number'},
                    'margin_level': {'type': 'number'},
                    'margin_so_call': {'type': 'number'},
                    'margin_so_so': {'type': 'number'},
                    'margin_initial': {'type': 'number'},
                    'margin_maintenance': {'type': 'number'},
                    'assets': {'type': 'number'},
                    'liabilities': {'type': 'number'},
                    'commission_blocked': {'type': 'number'},
                    'name': {'type': 'string'},
                    'server': {'type': 'string'},
                    'currency': {'type': 'string'},
                    'company': {'type': 'string'}
                }
            }
        },
        500: {
            'description': 'Failed to retrieve account information.'
        }
    }
})
def get_account_info():
    """
    Get Account Information
    ---
    description: Retrieve full account details including balance, equity, margin, profit, leverage, broker info, and trading permissions.
    """
    try:
        if not mt5.initialize():
            return jsonify({"error": "Failed to initialize MT5"}), 500

        account_info = mt5.account_info()
        if account_info is None:
            return jsonify({"error": "Failed to get account info"}), 500

        return jsonify(account_info._asdict())

    except Exception as e:
        logger.error(f"Error in get_account_info: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500
