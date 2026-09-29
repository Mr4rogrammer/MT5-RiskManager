swagger_config = {
    "swagger": "2.0",
    "info": {
        "title": "MetaTrader5 API",
        "description": (
            "API documentation for MetaTrader5 Flask application.\n\n"
            "**Authentication:** All endpoints (except `/health`) require an "
            "API key passed via the `Authorization` header.\n\n"
            "Use the **Authorize** button and enter your key as:\n"
            "`Bearer <your-api-key>` or just `<your-api-key>`."
        ),
        "version": "1.0.0"
    },
    "basePath": "/",
    "https": True,
    "schemes": [
        "https"
    ],
    "securityDefinitions": {
        "ApiKeyAuth": {
            "type": "apiKey",
            "name": "Authorization",
            "in": "header",
            "description": "Enter your API key as: Bearer <key> or just <key>"
        }
    },
    "security": [
        {
            "ApiKeyAuth": []
        }
    ],
    "specs": [
        {
            "endpoint": 'apispec_1',
            "route": '/apispec_1.json',
            "rule_filter": lambda rule: True,  # Include all routes
            "model_filter": lambda tag: True,  # Include all models
        }
    ],
    "static_url_path": "/flasgger_static",
    "swagger_ui": True,
    "specs_route": "/apidocs/",
    "headers": []
}