from dataclasses import dataclass

@dataclass
class ConnorsConfig:
    """Shared configuration for the Connors RSI Pullback strategy."""
    rsi_period: int = 10
    rsi_entry_threshold: float = 30.0
    rsi_exit_threshold: float = 40.0
    sma_period: int = 200
    max_hold_candles: int = 10
    stop_loss_pct: float = 0.0  # 0.0 means disabled

# Global shared instance
CONNORS_CONFIG = ConnorsConfig()
