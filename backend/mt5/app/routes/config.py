from flask import Blueprint, jsonify
from flasgger import swag_from
import logging

config_bp = Blueprint('config', __name__)
logger = logging.getLogger(__name__)

# ----------------------------------------------------------------
# Hardcoded config — edit these values as needed.
# ----------------------------------------------------------------
CONFIG = [
    {
    "deviceConfig": {
        "refreshInSeconds": 15,
        "configVersion": 1,
    },
    "accountConfig": {
        "maxDailyLoss": 50,
        "minRiskReward": 1.5,
        "maxTradePerDay":2
    },
    "tradeConfig": [
        {
        "symbol": "EURUSD",
        "time": [
            {
            "startTime": "03:00",
            "endTime": "05:00",
            "timeZone": "America/New_York"
            },
            {
            "startTime": "09:30",
            "endTime": "11:00",
            "timeZone": "America/New_York"
            }
        ]
        }
  ]
}
]

@config_bp.route('/config', methods=['GET'])
@swag_from({
    'tags': ['Config'],
    'responses': {
        200: {
            'description': 'Configuration retrieved successfully.',
            'schema': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {
                        'symbol': {'type': 'string'},
                        'maxRiskPerDay': {'type': 'number'},
                        'time': {
                            'type': 'array',
                            'items': {
                                'type': 'object',
                                'properties': {
                                    'startTime': {'type': 'string'},
                                    'endTime': {'type': 'string'},
                                    'timeZone': {'type': 'string'},
                                    'am/pm': {'type': 'string'}
                                }
                            }
                        }
                    }
                }
            }
        }
    }
})
def get_config():
    """
    Get Configuration
    ---
    description: Retrieve the trading configuration including symbols, risk limits, and trading time windows.
    """
    return jsonify(CONFIG)
