import pandas as pd
import numpy as np

def mean_reversion(data, window=20, num_std_dev=2):
    """
    Calculates the Mean Reversion signals based on Bollinger Bands.

    Parameters:
    - data (pd.DataFrame): DataFrame containing at least a 'close' column with closing prices.
    - window (int): The rolling window size for calculating the moving average and standard deviation.
    - num_std_dev (int): Number of standard deviations to set the upper and lower bands.

    Returns:
    - pd.Series: A series with 'top', 'bottom', or 0 indicating potential reversal points.
    """

    # Ensure the DataFrame has a 'close' column
    if 'close' not in data.columns:
        raise ValueError("DataFrame must contain a 'close' column.")

    # Calculate the rolling mean and standard deviation
    rolling_mean = data['close'].rolling(window=window).mean()
    rolling_std = data['close'].rolling(window=window).std()

    # Calculate Bollinger Bands
    data['bollinger_upper'] = rolling_mean + (rolling_std * num_std_dev)
    data['bollinger_lower'] = rolling_mean - (rolling_std * num_std_dev)

    # Initialize the mean_reversion column with 0
    data['mean_reversion'] = 0

    # Generate signals
    for idx in range(1, len(data)):
        previous_close = data['close'].iloc[idx - 1]
        current_close = data['close'].iloc[idx]
        previous_upper = data['bollinger_upper'].iloc[idx - 1]
        current_upper = data['bollinger_upper'].iloc[idx]
        previous_lower = data['bollinger_lower'].iloc[idx - 1]
        current_lower = data['bollinger_lower'].iloc[idx]

        # Check for price crossing above the upper band (potential mean reversion to the downside)
        if (previous_close <= previous_upper) and (current_close > current_upper):
            data.at[data.index[idx], 'mean_reversion'] = 'top'

        # Check for price crossing below the lower band (potential mean reversion to the upside)
        elif (previous_close >= previous_lower) and (current_close < current_lower):
            data.at[data.index[idx], 'mean_reversion'] = 'bottom'

        # Optionally, you can add conditions for crossing back within the bands
        # to reset signals or confirm reversals.

    # Replace NaN values with 0
    data['mean_reversion'].fillna(0, inplace=True)

    # Clean up intermediate columns if not needed
    data.drop(['bollinger_upper', 'bollinger_lower'], axis=1, inplace=True)

    return data['mean_reversion']
