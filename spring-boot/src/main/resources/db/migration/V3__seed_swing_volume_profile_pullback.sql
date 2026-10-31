INSERT INTO algorithms (name, display_name, description, strategy_type, candle_intervals, max_positions, stop_loss_pct)
VALUES (
    'swing_volume_profile_pullback',
    'Bullish Swing Volume-Profile Pullback',
    'Requires confirmed higher highs and higher lows, then enters after a bullish rejection from the latest swing volume-profile value area. Stop below the swing low; target 2R.',
    'swing',
    'ONE_DAY',
    5,
    5.00
)
ON CONFLICT (name) DO NOTHING;
