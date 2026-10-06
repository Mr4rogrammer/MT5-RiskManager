#!/bin/bash

source /scripts/02-common.sh

log_message "RUNNING" "07-start-wine-flask.sh"

# The desktop autostart session may not inherit the container env (.env),
# so load the API/bot settings from s6's saved container environment.
for var_file in /run/s6/container_environment/MT5_* /run/s6/container_environment/CRT_* /run/s6/container_environment/CRT2_* /run/s6/container_environment/BOT_DB_* /run/s6/container_environment/DSW_* /run/s6/container_environment/IND_*; do
    [ -f "$var_file" ] || continue
    var_name=$(basename "$var_file")
    [ -n "${!var_name}" ] || export "$var_name=$(cat "$var_file")"
done

flask_log="/config/flask.log"

# Keep the Flask API (and CRT bot) running: restart it whenever it exits.
run_flask_forever() {
    while true; do
        # Don't start a second copy if one is already running (e.g. started by hand)
        if pgrep -f "python.*app.py" > /dev/null; then
            sleep 30
            continue
        fi

        log_message "INFO" "Starting Flask server in Wine (output: $flask_log)..."
        echo "===== $(date '+%Y-%m-%d %H:%M:%S') starting app.py =====" >> "$flask_log"

        (cd /app && $wine_executable python -u app.py >> "$flask_log" 2>&1)

        log_message "ERROR" "Flask server exited with code $?. Restarting in 10 seconds..."
        sleep 10
    done
}

run_flask_forever &

log_message "INFO" "Flask watchdog started (PID $!)."
