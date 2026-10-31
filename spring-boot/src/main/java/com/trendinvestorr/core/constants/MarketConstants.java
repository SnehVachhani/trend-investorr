package com.trendinvestorr.core.constants;

import java.time.ZoneId;
import java.time.LocalTime;
import java.util.List;

/**
 * Application-wide compile-time constants. Port of {@code core.constants}.
 *
 * <p>These are NOT runtime-tunable — runtime params live in the {@code app_config} DB table.
 */
public final class MarketConstants {

    /** India Standard Time, used for all market-hour arithmetic. */
    public static final ZoneId IST = ZoneId.of("Asia/Kolkata");

    // ── Market hours (IST) ──────────────────────────────────
    public static final LocalTime MARKET_OPEN      = LocalTime.of(9, 15);
    public static final LocalTime MARKET_CLOSE     = LocalTime.of(15, 30);
    public static final LocalTime PRE_MARKET_OPEN  = LocalTime.of(9, 0);
    public static final LocalTime PRE_MARKET_CLOSE = LocalTime.of(9, 15);

    // ── Scheduler time boundaries (IST) ────────────────────
    public static final LocalTime PREMARKET_WARMUP_START    = LocalTime.of(8, 45);
    public static final LocalTime INTRADAY_SQUAREOFF_TIME    = LocalTime.of(15, 15);
    public static final LocalTime POST_MARKET_START         = LocalTime.of(15, 45);
    public static final LocalTime POST_MARKET_END           = LocalTime.of(17, 0);
    public static final LocalTime NIGHT_IDLE_START          = LocalTime.of(17, 0);
    public static final LocalTime NIGHT_IDLE_END            = LocalTime.of(8, 45);

    // ── SmartAPI throttling / limits ───────────────────────
    public static final int SMARTAPI_MAX_WEBSOCKET_CONNECTIONS = 3;
    public static final int SMARTAPI_MAX_TOKENS_PER_CONNECTION = 1000;
    public static final int SMARTAPI_MAX_ORDERS_PER_SECOND     = 10;
    public static final int SMARTAPI_SAFE_OPS_LIMIT            = 8;
    public static final int SMARTAPI_SESSION_EXPIRY_HOUR       = 0;

    public static final String SMARTAPI_INSTRUMENT_LIST_URL =
            "https://marginapi.angelbroking.com/OpenAPIService/rest/secure/"
          + "angelbroking/instrument/v1/getMasterData";

    // ── NSE bhavcopy / headers ──────────────────────────────
    public static final String NSE_BHAVCOPY_URL_TEMPLATE =
            "https://nsearcharies.nseindia.com/products/content/sec_bhavdata_full_{date}.csv";

    public static final String NSE_USER_AGENT =
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36";
    public static final String NSE_REFERER = "https://www.nseindia.com";

    // ── NSE Holidays 2026 (update annually) ─────────────────
    public static final List<String> NSE_HOLIDAYS_2026 = List.of(
            "2026-01-26", "2026-02-17", "2026-03-10", "2026-03-30", "2026-04-02",
            "2026-04-03", "2026-04-14", "2026-05-01", "2026-06-06", "2026-07-06",
            "2026-08-15", "2026-08-16", "2026-09-04", "2026-10-02", "2026-10-20",
            "2026-10-21", "2026-11-09", "2026-11-10", "2026-11-19", "2026-12-25"
    );

    private MarketConstants() {}
}
