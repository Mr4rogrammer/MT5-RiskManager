#!/bin/sh
# Create the basic-auth file from DASHBOARD_USER / DASHBOARD_PASSWORD.
# Refuses to start without them, so the dashboard is never served unprotected.
set -e

if [ -z "$DASHBOARD_USER" ] || [ -z "$DASHBOARD_PASSWORD" ]; then
    echo "DASHBOARD_USER and DASHBOARD_PASSWORD must be set" >&2
    exit 1
fi

printf '%s:%s\n' "$DASHBOARD_USER" "$(openssl passwd -apr1 "$DASHBOARD_PASSWORD")" \
    > /etc/nginx/.htpasswd
