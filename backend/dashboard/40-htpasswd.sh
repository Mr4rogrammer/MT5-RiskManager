#!/bin/sh
# Checks the login settings (DASHBOARD_USER / DASHBOARD_PASSWORD, used by auth.js) and
# creates the writable folder for start/stop control files.
# Refuses to start without them, so the dashboard is never served unprotected.
set -e

if [ -z "$DASHBOARD_USER" ] || [ -z "$DASHBOARD_PASSWORD" ]; then
    echo "DASHBOARD_USER and DASHBOARD_PASSWORD must be set" >&2
    exit 1
fi

# Start/stop control files (config/control on the host): nginx writes them
mkdir -p /botcontrol/.tmp
chown nginx:nginx /botcontrol /botcontrol/.tmp
chmod 777 /botcontrol /botcontrol/.tmp   # stays writable if the mt5 container re-owns config/
