#!/bin/bash

source /scripts/02-common.sh

log_message "RUNNING" "07-start-wine-flask.sh"

log_message "INFO" "Starting Flask server in Wine environment..."

# The desktop autostart session may not inherit the container env (.env),
# so load the API/bot settings from s6's saved container environment.
for var_file in /run/s6/container_environment/MT5_* /run/s6/container_environment/CRT_*; do
    [ -f "$var_file" ] || continue
    var_name=$(basename "$var_file")
    [ -n "${!var_name}" ] || export "$var_name=$(cat "$var_file")"
done

# Run the Flask app using Wine's Python
wine python /app/app.py &

FLASK_PID=$!

# Give the server some time to start
sleep 5

# Check if the Flask server is running
if ps -p $FLASK_PID > /dev/null; then
    log_message "INFO" "Flask server in Wine started successfully with PID $FLASK_PID."
else
    log_message "ERROR" "Failed to start Flask server in Wine."
    exit 1
fi