-- ============================================================
-- Trend-Investorr V1 schema. Port of storage/database.py SCHEMA_MIGRATIONS.
-- Idempotent (IF NOT EXISTS) so it runs cleanly on a fresh DB
-- and is safe against a pre-existing DB (Flyway baseline-on-migrate).
-- ============================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ── Sectors ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sectors (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(50) UNIQUE NOT NULL,
    display_name    VARCHAR(100) NOT NULL,
    nifty_index     VARCHAR(50) DEFAULT '',
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ── Stocks (master instrument list) ──────────────────────────
CREATE TABLE IF NOT EXISTS stocks (
    id              SERIAL PRIMARY KEY,
    symbol          VARCHAR(30) NOT NULL,
    token           VARCHAR(20) NOT NULL,
    name            VARCHAR(200) DEFAULT '',
    exchange        VARCHAR(10) NOT NULL DEFAULT 'NSE',
    series          VARCHAR(10) DEFAULT 'EQ',
    sector_id       INTEGER REFERENCES sectors(id),
    industry        VARCHAR(200) DEFAULT '',
    market_cap_cr   NUMERIC(14,2) DEFAULT 0,
    lot_size        INTEGER DEFAULT 1,
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(symbol, exchange)
);
CREATE INDEX IF NOT EXISTS idx_stocks_symbol ON stocks(symbol);
CREATE INDEX IF NOT EXISTS idx_stocks_token  ON stocks(token);
CREATE INDEX IF NOT EXISTS idx_stocks_sector ON stocks(sector_id);

-- ── Candle tables (one per interval) ─────────────────────────
CREATE TABLE IF NOT EXISTS candles_1m (
    id              BIGSERIAL PRIMARY KEY,
    stock_id        INTEGER REFERENCES stocks(id) ON DELETE CASCADE,
    timestamp       TIMESTAMPTZ NOT NULL,
    open            NUMERIC(12,2) NOT NULL,
    high            NUMERIC(12,2) NOT NULL,
    low             NUMERIC(12,2) NOT NULL,
    close           NUMERIC(12,2) NOT NULL,
    volume          BIGINT NOT NULL DEFAULT 0,
    vwap            NUMERIC(12,2) DEFAULT 0,
    UNIQUE(stock_id, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_candles_1m_stock_ts ON candles_1m(stock_id, timestamp DESC);

CREATE TABLE IF NOT EXISTS candles_5m (
    id              BIGSERIAL PRIMARY KEY,
    stock_id        INTEGER REFERENCES stocks(id) ON DELETE CASCADE,
    timestamp       TIMESTAMPTZ NOT NULL,
    open            NUMERIC(12,2) NOT NULL,
    high            NUMERIC(12,2) NOT NULL,
    low             NUMERIC(12,2) NOT NULL,
    close           NUMERIC(12,2) NOT NULL,
    volume          BIGINT NOT NULL DEFAULT 0,
    vwap            NUMERIC(12,2) DEFAULT 0,
    UNIQUE(stock_id, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_candles_5m_stock_ts ON candles_5m(stock_id, timestamp DESC);

CREATE TABLE IF NOT EXISTS candles_15m (
    id              BIGSERIAL PRIMARY KEY,
    stock_id        INTEGER REFERENCES stocks(id) ON DELETE CASCADE,
    timestamp       TIMESTAMPTZ NOT NULL,
    open            NUMERIC(12,2) NOT NULL,
    high            NUMERIC(12,2) NOT NULL,
    low             NUMERIC(12,2) NOT NULL,
    close           NUMERIC(12,2) NOT NULL,
    volume          BIGINT NOT NULL DEFAULT 0,
    vwap            NUMERIC(12,2) DEFAULT 0,
    UNIQUE(stock_id, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_candles_15m_stock_ts ON candles_15m(stock_id, timestamp DESC);

CREATE TABLE IF NOT EXISTS candles_1h (
    id              BIGSERIAL PRIMARY KEY,
    stock_id        INTEGER REFERENCES stocks(id) ON DELETE CASCADE,
    timestamp       TIMESTAMPTZ NOT NULL,
    open            NUMERIC(12,2) NOT NULL,
    high            NUMERIC(12,2) NOT NULL,
    low             NUMERIC(12,2) NOT NULL,
    close           NUMERIC(12,2) NOT NULL,
    volume          BIGINT NOT NULL DEFAULT 0,
    vwap            NUMERIC(12,2) DEFAULT 0,
    UNIQUE(stock_id, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_candles_1h_stock_ts ON candles_1h(stock_id, timestamp DESC);

CREATE TABLE IF NOT EXISTS candles_1d (
    id              BIGSERIAL PRIMARY KEY,
    stock_id        INTEGER REFERENCES stocks(id) ON DELETE CASCADE,
    timestamp       TIMESTAMPTZ NOT NULL,
    open            NUMERIC(12,2) NOT NULL,
    high            NUMERIC(12,2) NOT NULL,
    low             NUMERIC(12,2) NOT NULL,
    close           NUMERIC(12,2) NOT NULL,
    volume          BIGINT NOT NULL DEFAULT 0,
    vwap            NUMERIC(12,2) DEFAULT 0,
    delivery_qty    BIGINT DEFAULT 0,
    delivery_pct    NUMERIC(6,2) DEFAULT 0,
    UNIQUE(stock_id, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_candles_1d_stock_ts ON candles_1d(stock_id, timestamp DESC);

CREATE TABLE IF NOT EXISTS candles_1w (
    id              BIGSERIAL PRIMARY KEY,
    stock_id        INTEGER REFERENCES stocks(id) ON DELETE CASCADE,
    timestamp       TIMESTAMPTZ NOT NULL,
    open            NUMERIC(12,2) NOT NULL,
    high            NUMERIC(12,2) NOT NULL,
    low             NUMERIC(12,2) NOT NULL,
    close           NUMERIC(12,2) NOT NULL,
    volume          BIGINT NOT NULL DEFAULT 0,
    vwap            NUMERIC(12,2) DEFAULT 0,
    UNIQUE(stock_id, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_candles_1w_stock_ts ON candles_1w(stock_id, timestamp DESC);

-- ── Candle coverage (data range tracker) ────────────────────
CREATE TABLE IF NOT EXISTS candle_coverage (
    id              SERIAL PRIMARY KEY,
    stock_id        INTEGER REFERENCES stocks(id) ON DELETE CASCADE,
    interval        VARCHAR(20) NOT NULL,
    earliest_ts     TIMESTAMPTZ NOT NULL,
    latest_ts       TIMESTAMPTZ NOT NULL,
    candle_count    INTEGER DEFAULT 0,
    source          VARCHAR(30) DEFAULT 'angel_one',
    last_updated    TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(stock_id, interval)
);
CREATE INDEX IF NOT EXISTS idx_candle_coverage_stock    ON candle_coverage(stock_id);
CREATE INDEX IF NOT EXISTS idx_candle_coverage_interval ON candle_coverage(interval);

-- ── Watchlists (parent: list metadata) ──────────────────────
CREATE TABLE IF NOT EXISTS watchlists (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(100) UNIQUE NOT NULL,
    description     TEXT DEFAULT '',
    list_type       VARCHAR(30) DEFAULT 'custom',
    is_active       BOOLEAN DEFAULT TRUE,
    max_stocks      INTEGER DEFAULT 500,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS watchlist_items (
    id              SERIAL PRIMARY KEY,
    watchlist_id    INTEGER REFERENCES watchlists(id) ON DELETE CASCADE,
    stock_id        INTEGER REFERENCES stocks(id) ON DELETE CASCADE,
    source          VARCHAR(30) DEFAULT 'manual',
    priority        INTEGER DEFAULT 0,
    added_at        TIMESTAMPTZ DEFAULT NOW(),
    notes           TEXT DEFAULT '',
    UNIQUE(watchlist_id, stock_id)
);
CREATE INDEX IF NOT EXISTS idx_watchlist_items_watchlist ON watchlist_items(watchlist_id);
CREATE INDEX IF NOT EXISTS idx_watchlist_items_stock     ON watchlist_items(stock_id);

INSERT INTO watchlists (name, description, list_type, max_stocks)
VALUES
    ('nse500',       'NSE 500 index stocks — default for WebSocket and scanning', 'system', 500),
    ('manual_picks', 'Manually curated stocks to watch',                          'custom', 0)
ON CONFLICT (name) DO NOTHING;

-- ── Volume snapshots (intraday cumulative) ──────────────────
CREATE TABLE IF NOT EXISTS volume_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    stock_id        INTEGER REFERENCES stocks(id) ON DELETE CASCADE,
    timestamp       TIMESTAMPTZ NOT NULL,
    cumulative_vol  BIGINT NOT NULL,
    avg_vol_30d     BIGINT DEFAULT 0,
    avg_vol_90d     BIGINT DEFAULT 0,
    volume_ratio    NUMERIC(8,2) DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_volume_snapshots_stock_ts ON volume_snapshots(stock_id, timestamp DESC);

-- ── Broker accounts ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS broker_accounts (
    id              SERIAL PRIMARY KEY,
    broker_name     VARCHAR(50) NOT NULL DEFAULT 'angel_one',
    client_id       VARCHAR(50) NOT NULL,
    is_active       BOOLEAN DEFAULT TRUE,
    is_paper        BOOLEAN DEFAULT FALSE,
    static_ip       VARCHAR(45) DEFAULT '',
    last_login_at   TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(broker_name, client_id)
);

-- ── Signals ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS signals (
    id                  BIGSERIAL PRIMARY KEY,
    stock_id            INTEGER REFERENCES stocks(id),
    sector_id           INTEGER REFERENCES sectors(id),
    signal_type         VARCHAR(10) NOT NULL,
    strength            VARCHAR(20) NOT NULL,
    score               NUMERIC(5,3) NOT NULL,
    analyzer_name       VARCHAR(50) DEFAULT '',
    analyzer_confidence NUMERIC(5,3) DEFAULT 0,
    analyzer_reasoning  TEXT DEFAULT '',
    volume_current      BIGINT DEFAULT 0,
    volume_avg          BIGINT DEFAULT 0,
    volume_ratio        NUMERIC(8,2) DEFAULT 0,
    price               NUMERIC(12,2) NOT NULL,
    status              VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    metadata            JSONB DEFAULT '{}',
    created_at          TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_signals_stock  ON signals(stock_id);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);
CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at DESC);

-- ── Orders ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS orders (
    id              BIGSERIAL PRIMARY KEY,
    signal_id       BIGINT REFERENCES signals(id),
    stock_id        INTEGER REFERENCES stocks(id),
    order_type      VARCHAR(20) NOT NULL,
    product_type    VARCHAR(20) NOT NULL DEFAULT 'INTRADAY',
    transaction_type VARCHAR(10) NOT NULL,
    quantity        INTEGER NOT NULL,
    price           NUMERIC(12,2) NOT NULL,
    trigger_price   NUMERIC(12,2) DEFAULT 0,
    status          VARCHAR(20) NOT NULL DEFAULT 'CREATED',
    is_paper_trade  BOOLEAN DEFAULT TRUE,
    broker_order_id VARCHAR(50) DEFAULT '',
    filled_qty      INTEGER DEFAULT 0,
    avg_fill_price  NUMERIC(12,2) DEFAULT 0,
    slippage        NUMERIC(6,4) DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_orders_signal ON orders(signal_id);
CREATE INDEX IF NOT EXISTS idx_orders_stock  ON orders(stock_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

-- ── Positions (current holdings) ────────────────────────────
CREATE TABLE IF NOT EXISTS positions (
    id              BIGSERIAL PRIMARY KEY,
    stock_id        INTEGER REFERENCES stocks(id),
    quantity        INTEGER NOT NULL,
    avg_buy_price   NUMERIC(12,2) NOT NULL,
    current_price   NUMERIC(12,2) DEFAULT 0,
    unrealized_pnl  NUMERIC(14,2) DEFAULT 0,
    is_paper        BOOLEAN DEFAULT TRUE,
    product_type    VARCHAR(20) NOT NULL DEFAULT 'INTRADAY',
    opened_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(stock_id, is_paper, product_type)
);
CREATE INDEX IF NOT EXISTS idx_positions_stock ON positions(stock_id);

-- ── Daily P&L ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS daily_pnl (
    id              SERIAL PRIMARY KEY,
    date            DATE NOT NULL,
    realized_pnl    NUMERIC(14,2) DEFAULT 0,
    unrealized_pnl  NUMERIC(14,2) DEFAULT 0,
    total_trades    INTEGER DEFAULT 0,
    winning_trades  INTEGER DEFAULT 0,
    losing_trades   INTEGER DEFAULT 0,
    max_drawdown    NUMERIC(14,2) DEFAULT 0,
    portfolio_value NUMERIC(16,2) DEFAULT 0,
    cash_balance    NUMERIC(16,2) DEFAULT 0,
    is_paper        BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(date, is_paper)
);

-- ── Audit log (immutable) ──────────────────────────────────
CREATE TABLE IF NOT EXISTS audit_log (
    id              BIGSERIAL PRIMARY KEY,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    event_type      VARCHAR(50) NOT NULL,
    details         JSONB DEFAULT '{}',
    ip_address      VARCHAR(45) DEFAULT '',
    checksum        VARCHAR(64) NOT NULL,
    prev_checksum   VARCHAR(64) DEFAULT '',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_log_event ON audit_log(event_type);
CREATE INDEX IF NOT EXISTS idx_audit_log_ts    ON audit_log(timestamp DESC);

-- ── Live ticks (raw real-time data, auto-purged) ───────────
CREATE TABLE IF NOT EXISTS live_ticks (
    id              BIGSERIAL PRIMARY KEY,
    stock_id        INTEGER REFERENCES stocks(id),
    token           VARCHAR(20) NOT NULL,
    timestamp       TIMESTAMPTZ NOT NULL,
    ltp             NUMERIC(12,2) NOT NULL,
    volume          BIGINT DEFAULT 0,
    open            NUMERIC(12,2) DEFAULT 0,
    high            NUMERIC(12,2) DEFAULT 0,
    low             NUMERIC(12,2) DEFAULT 0,
    best_bid        NUMERIC(12,2) DEFAULT 0,
    best_ask        NUMERIC(12,2) DEFAULT 0,
    avg_trade_price NUMERIC(12,2) DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_live_ticks_token_ts ON live_ticks(token, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_live_ticks_ts       ON live_ticks(timestamp DESC);

-- ── Technical indicators (EOD cache) ───────────────────────
CREATE TABLE IF NOT EXISTS technical_indicators (
    id              BIGSERIAL PRIMARY KEY,
    stock_id        INTEGER REFERENCES stocks(id) ON DELETE CASCADE,
    date            DATE NOT NULL,
    rsi_14          NUMERIC(6,2),
    ema_20          NUMERIC(12,2),
    ema_50          NUMERIC(12,2),
    atr_14          NUMERIC(12,2),
    macd_line       NUMERIC(12,2),
    macd_signal     NUMERIC(12,2),
    delivery_pct    NUMERIC(6,2),
    sma_20          NUMERIC(12,2),
    sma_50          NUMERIC(12,2),
    sma_200         NUMERIC(12,2),
    macd_histogram  NUMERIC(12,4),
    bollinger_upper NUMERIC(12,2),
    bollinger_lower NUMERIC(12,2),
    bollinger_width NUMERIC(8,4),
    supertrend      NUMERIC(12,2),
    supertrend_direction INTEGER DEFAULT 0,
    adx_14          NUMERIC(6,2),
    obv             BIGINT,
    pivot_point     NUMERIC(12,2),
    pivot_r1        NUMERIC(12,2),
    pivot_r2        NUMERIC(12,2),
    pivot_s1        NUMERIC(12,2),
    pivot_s2        NUMERIC(12,2),
    support_level   NUMERIC(12,2),
    resistance_level NUMERIC(12,2),
    high_52w        NUMERIC(12,2),
    low_52w         NUMERIC(12,2),
    vwap_daily      NUMERIC(12,2),
    rsi_10          NUMERIC(6,2),
    UNIQUE(stock_id, date)
);
CREATE INDEX IF NOT EXISTS idx_tech_ind_stock_date ON technical_indicators(stock_id, date DESC);

-- ── Sector history (breadth/momentum) ────────────────────────
CREATE TABLE IF NOT EXISTS sector_history (
    id                SERIAL PRIMARY KEY,
    sector_id         INTEGER REFERENCES sectors(id) ON DELETE CASCADE,
    date              DATE NOT NULL,
    advancing_stocks  INTEGER DEFAULT 0,
    declining_stocks  INTEGER DEFAULT 0,
    total_volume      BIGINT DEFAULT 0,
    heat_score        NUMERIC(5,2) DEFAULT 0.5,
    UNIQUE(sector_id, date)
);

-- ── Scan history (scanner stats for backtesting) ────────────
CREATE TABLE IF NOT EXISTS scan_history (
    id              BIGSERIAL PRIMARY KEY,
    stock_id        INTEGER REFERENCES stocks(id) ON DELETE CASCADE,
    date            DATE NOT NULL,
    peak_vol_ratio  NUMERIC(8,2) DEFAULT 0,
    signals_fired   INTEGER DEFAULT 0,
    UNIQUE(stock_id, date)
);

-- ── Scanner signals (post-market → next-day pipeline) ──────
CREATE TABLE IF NOT EXISTS scanner_signals (
    id              BIGSERIAL PRIMARY KEY,
    stock_id        INTEGER REFERENCES stocks(id) ON DELETE CASCADE,
    scan_date       DATE NOT NULL,
    target_date     DATE NOT NULL,
    scanner_name    VARCHAR(50) NOT NULL,
    pattern         VARCHAR(50) NOT NULL,
    direction       VARCHAR(10) NOT NULL,
    confidence      NUMERIC(5,3) NOT NULL,
    price           NUMERIC(12,2) NOT NULL,
    strength        VARCHAR(20) NOT NULL,
    details         JSONB DEFAULT '{}',
    status          VARCHAR(20) DEFAULT 'ACTIVE',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(stock_id, scan_date, scanner_name, pattern)
);
CREATE INDEX IF NOT EXISTS idx_scanner_signals_target ON scanner_signals(target_date, status);
CREATE INDEX IF NOT EXISTS idx_scanner_signals_stock  ON scanner_signals(stock_id, target_date);

-- ── App config (runtime-tunable parameters) ────────────────
CREATE TABLE IF NOT EXISTS app_config (
    id              SERIAL PRIMARY KEY,
    key             VARCHAR(100) UNIQUE NOT NULL,
    value           TEXT NOT NULL,
    data_type       VARCHAR(20) NOT NULL DEFAULT 'string',
    category        VARCHAR(50) DEFAULT 'general',
    description     TEXT DEFAULT '',
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_by      VARCHAR(50) DEFAULT 'system'
);

INSERT INTO app_config (key, value, data_type, category, description)
VALUES
    ('analyzer_type', 'static', 'string', 'analyzer', 'Active analyzer: static, gemini, composite'),
    ('spike_multiplier', '2.0', 'float', 'scanner', 'Volume spike threshold multiplier'),
    ('min_signal_score', '0.35', 'float', 'scanner', 'Minimum signal score to generate'),
    ('max_capital_per_trade_pct', '5.0', 'float', 'risk', 'Max capital % per trade'),
    ('max_daily_loss_pct', '2.0', 'float', 'risk', 'Max daily loss % before halt'),
    ('max_open_positions', '20', 'int', 'risk', 'Max simultaneous open positions'),
    ('default_stop_loss_pct', '2.0', 'float', 'risk', 'Default stop loss percentage'),
    ('default_target_pct', '5.0', 'float', 'risk', 'Default target percentage'),
    ('trading_mode', 'PAPER', 'string', 'trading', 'Trading mode: PAPER, LIVE, DUAL'),
    ('min_price', '1.0', 'float', 'filter', 'Minimum stock price for scanning'),
    ('max_price', '99999.0', 'float', 'filter', 'Maximum stock price for scanning'),
    ('min_avg_volume', '10000', 'int', 'filter', 'Minimum 30-day avg volume'),
    ('slippage_pct', '0.1', 'float', 'trading', 'Simulated slippage percentage'),
    ('circuit_breaker_daily_loss_pct', '3.0', 'float', 'circuit_breaker', 'Circuit breaker daily loss trigger'),
    ('circuit_breaker_consecutive_losses', '5', 'int', 'circuit_breaker', 'Max consecutive losses before halt'),
    ('charges_enabled', 'true', 'bool', 'trading_costs', 'Apply configured equity-delivery charges in backtests'),
    ('charges_product_type', 'DELIVERY', 'string', 'trading_costs', 'Product type used by the charge schedule'),
    ('charges_exchange', 'NSE', 'string', 'trading_costs', 'Exchange rates used by the charge schedule'),
    ('charges_brokerage_rate_pct', '0.0', 'float', 'trading_costs', 'Brokerage percentage per executed delivery order'),
    ('charges_brokerage_cap_rupees', '0.0', 'float', 'trading_costs', 'Maximum brokerage per executed delivery order'),
    ('charges_brokerage_min_rupees', '0.0', 'float', 'trading_costs', 'Minimum brokerage per executed delivery order'),
    ('charges_stt_buy_pct', '0.1', 'float', 'trading_costs', 'STT percentage on equity-delivery buys'),
    ('charges_stt_sell_pct', '0.1', 'float', 'trading_costs', 'STT percentage on equity-delivery sells'),
    ('charges_transaction_charge_pct', '0.0030699', 'float', 'trading_costs', 'NSE transaction charge percentage on turnover'),
    ('charges_ipft_charge_pct', '0.0000001', 'float', 'trading_costs', 'NSE IPFT percentage on turnover'),
    ('charges_stamp_duty_buy_pct', '0.015', 'float', 'trading_costs', 'Stamp duty percentage on delivery buys'),
    ('charges_sebi_fee_per_crore', '10.0', 'float', 'trading_costs', 'SEBI turnover fee in rupees per crore'),
    ('charges_dp_charge_per_sell_rupees', '12.5', 'float', 'trading_costs', 'DP charge per delivery sell instruction and ISIN before GST'),
    ('charges_gst_pct', '18.0', 'float', 'trading_costs', 'GST percentage on applicable services and DP charges')
ON CONFLICT (key) DO NOTHING;

-- ── Algorithms master table ─────────────────────────────────
CREATE TABLE IF NOT EXISTS algorithms (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(100) UNIQUE NOT NULL,
    display_name    VARCHAR(200) NOT NULL,
    description     TEXT,
    strategy_type   VARCHAR(50) NOT NULL DEFAULT 'swing',
    candle_intervals VARCHAR(200),
    max_positions   INTEGER DEFAULT 5,
    stop_loss_pct   NUMERIC(5,2) DEFAULT 5.00,
    reference_url   TEXT,
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO algorithms (name, display_name, description, strategy_type, candle_intervals, max_positions, stop_loss_pct)
VALUES
    ('connors_rsi_pullback', 'Connors RSI Pullback',
     'Mean-reversion: buys oversold stocks (RSI<25) in uptrends (price>SMA200). Exits on RSI>40 or 10-day time stop.',
     'swing', 'ONE_DAY', 5, 5.00),
    ('rayner_dip_buyer', 'Rayner 10-Day Low Dip Buyer',
     'Buys 10-day lows at 2% discount in uptrends (price>SMA200). Exits when close>SMA5 or 20-day time stop.',
     'positional', 'ONE_DAY', 10, 5.00),
    ('scanner_confluence', 'Scanner Confluence',
     'Meta-algorithm: enters when 2+ scanners agree on same stock. Intraday with 3% SL / 2% TP.',
     'intraday', 'FIVE_MINUTE', 5, 3.00)
ON CONFLICT (name) DO NOTHING;

-- ── Watchlist ↔ algorithm bindings ──────────────────────────
CREATE TABLE IF NOT EXISTS watchlist_algorithm_bindings (
    id              SERIAL PRIMARY KEY,
    watchlist_id    INTEGER REFERENCES watchlists(id) ON DELETE CASCADE,
    algorithm_name  VARCHAR(100) NOT NULL REFERENCES algorithms(name),
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(watchlist_id, algorithm_name)
);
CREATE INDEX IF NOT EXISTS idx_wab_watchlist  ON watchlist_algorithm_bindings(watchlist_id);
CREATE INDEX IF NOT EXISTS idx_wab_algorithm ON watchlist_algorithm_bindings(algorithm_name);

INSERT INTO watchlists (name, description, list_type, max_stocks)
VALUES
    ('hot_stocks',         'High momentum stocks with recent volume surges',  'custom', 100),
    ('trending_breakouts', 'Stocks breaking out of consolidation patterns',    'custom', 50),
    ('quality_value',      'Blue-chip stocks with strong fundamentals for pullback buying', 'custom', 50)
ON CONFLICT (name) DO NOTHING;

-- ── Backtest runs ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS backtest_runs (
    id                  SERIAL PRIMARY KEY,
    algorithm_name      VARCHAR(100) NOT NULL,
    stock_symbol        VARCHAR(30) NOT NULL,
    stock_name          VARCHAR(200) DEFAULT '',
    start_date          DATE NOT NULL,
    end_date            DATE NOT NULL,
    initial_capital     NUMERIC(16,2) DEFAULT 1000000,
    status              VARCHAR(20) DEFAULT 'running',
    total_return_pct    NUMERIC(10,2),
    cagr_pct            NUMERIC(10,2),
    max_drawdown_pct    NUMERIC(10,2),
    sharpe_ratio        NUMERIC(8,3),
    win_rate_pct        NUMERIC(6,2),
    total_trades        INTEGER,
    winning_trades      INTEGER DEFAULT 0,
    losing_trades       INTEGER DEFAULT 0,
    avg_win_pct         NUMERIC(8,2),
    avg_loss_pct        NUMERIC(8,2),
    profit_factor       NUMERIC(8,3),
    avg_hold_days       NUMERIC(6,1),
    final_capital       NUMERIC(16,2),
    total_charges       NUMERIC(14,2) DEFAULT 0,
    equity_curve        JSONB DEFAULT '[]',
    config              JSONB DEFAULT '{}',
    error_message       TEXT DEFAULT '',
    created_at          TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_bt_runs_created ON backtest_runs(created_at DESC);

-- ── Backtest trades ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS backtest_trades (
    id              SERIAL PRIMARY KEY,
    run_id          INTEGER REFERENCES backtest_runs(id) ON DELETE CASCADE,
    symbol          VARCHAR(30) NOT NULL,
    entry_date      DATE NOT NULL,
    exit_date       DATE,
    entry_price     NUMERIC(12,2) NOT NULL,
    exit_price      NUMERIC(12,2),
    quantity        INTEGER DEFAULT 0,
    pnl             NUMERIC(14,2) DEFAULT 0,
    pnl_pct         NUMERIC(8,2) DEFAULT 0,
    exit_reason     VARCHAR(50),
    hold_days       INTEGER DEFAULT 0,
    metadata        JSONB DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_bt_trades_run ON backtest_trades(run_id);
