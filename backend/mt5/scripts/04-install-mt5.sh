#!/bin/bash

source /scripts/02-common.sh

log_message "RUNNING" "04-install-mt5.sh"

# Check if MetaTrader 5 is installed
if [ -e "$mt5file" ]; then
    log_message "INFO" "File $mt5file already exists."
else
    log_message "INFO" "File $mt5file is not installed. Installing..."

    # Set Windows 10 mode in Wine and download and install MT5
    $wine_executable reg add "HKEY_CURRENT_USER\\Software\\Wine" /v Version /t REG_SZ /d "win10" /f
    log_message "INFO" "Downloading MT5 installer..."
    wget -O /tmp/mt5setup.exe $mt5setup_url > /dev/null 2>&1
    log_message "INFO" "Installing MetaTrader 5..."
    $wine_executable /tmp/mt5setup.exe /auto
    rm -f /tmp/mt5setup.exe
fi

# Recheck if MetaTrader 5 is installed
if [ -e "$mt5file" ]; then
    log_message "INFO" "File $mt5file is installed. Running MT5..."

    # Startup config: turn on Algo Trading so the API/CRT bot can place orders
    # (the toolbar button can't be enabled from Python).
    printf '[Experts]\r\nAllowLiveTrading=1\r\nAllowDllImport=0\r\nEnabled=1\r\n' > /config/mt5-startup.ini
    $wine_executable "$mt5file" "/config:Z:\\config\\mt5-startup.ini" &
else
    log_message "ERROR" "File $mt5file is not installed. MT5 cannot be run."
fi