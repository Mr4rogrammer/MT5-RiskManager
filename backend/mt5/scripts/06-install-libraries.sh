#!/bin/bash

source /scripts/02-common.sh

log_message "RUNNING" "06-install-libraries.sh"

# Install the API's Python libraries in Wine if any of them is missing
if ! $wine_executable python -c "import MetaTrader5, flask, flask_sock, flasgger, pandas, dotenv, pytz" > /dev/null 2>&1; then
    log_message "INFO" "Installing Python libraries in Wine..."
    $wine_executable python -m pip install --no-cache-dir -r /app/requirements.txt
else
    log_message "INFO" "Python libraries are already installed in Wine."
fi