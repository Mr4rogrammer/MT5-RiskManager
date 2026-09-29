import os
import logging
from flask import request, jsonify

logger = logging.getLogger(__name__)

# Routes that do not require authentication
PUBLIC_ROUTES = [
    '/health',
    '/apidocs/',
    '/apispec_1.json',
    '/flasgger_static/',
]


def _is_public_route(path):
    """Check if the request path matches any public (exempt) route."""
    for route in PUBLIC_ROUTES:
        if path == route or path.startswith(route):
            return True
    return False


def require_api_key():
    """
    Flask before_request handler that validates the API key.

    Reads the expected key from the MT5_API_KEY environment variable and
    compares it against the value supplied in the ``Authorization`` header.

    Requests to public routes (health check, Swagger UI) are allowed
    through without authentication.

    Returns:
        None            – if the request is authorised (Flask continues).
        (Response, 401) – if the API key is missing.
        (Response, 403) – if the API key is invalid.
        (Response, 500) – if MT5_API_KEY is not configured on the server.
    """
    # Allow public routes without auth
    if _is_public_route(request.path):
        return None

    api_key = os.environ.get('MT5_API_KEY')

    # Fail-safe: if the server has no key configured, reject all requests
    if not api_key:
        logger.error(
            "MT5_API_KEY environment variable is not set. "
            "All authenticated requests will be rejected."
        )
        return jsonify({
            "error": "Server authentication is not configured"
        }), 500

    # Extract the key from the Authorization header
    auth_header = request.headers.get('Authorization')

    if not auth_header:
        return jsonify({
            "error": "Missing Authorization header"
        }), 401

    # Support both raw key and "Bearer <key>" format
    token = auth_header
    if auth_header.lower().startswith('bearer '):
        token = auth_header[7:]

    if token != api_key:
        logger.warning(
            "Invalid API key received from %s for %s %s",
            request.remote_addr,
            request.method,
            request.path,
        )
        return jsonify({
            "error": "Invalid API key"
        }), 403

    # Authorised – let the request through
    return None
