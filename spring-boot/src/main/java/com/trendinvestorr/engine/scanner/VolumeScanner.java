package com.trendinvestorr.engine.scanner;

import com.trendinvestorr.core.constants.MarketConstants;
import com.trendinvestorr.core.constants.SignalSource;
import com.trendinvestorr.core.constants.SignalStrength;
import com.trendinvestorr.core.config.ScannerProperties;
import com.trendinvestorr.data.model.Observation;
import com.trendinvestorr.data.model.Tick;
import com.trendinvestorr.data.model.VolumeSnapshot;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.time.Duration;
import java.time.LocalTime;
import java.time.ZonedDateTime;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Real-time volume-spike detection.
 *
 * <p>Tracks cumulative intraday volume per stock from the WebSocket tick feed,
 * compares it against time-normalized historical averages (the volume curve),
 * and emits an {@link Observation} whenever the time-normalized ratio clears
 * the configured spike multiplier.
 *
 * <p><b>SOLID:</b>
 * <ul>
 *   <li>SRP — only volume-spike detection; volume-curve math is delegated to
 *       {@link VolumeCurve}, cooldown to {@link SignalCooldown}, historical
 *       lookup to {@link HistoricalVolumePort}.</li>
 *   <li>OCP — new spike-strength rules can be added via {@link #strengthFor}.</li>
 *   <li>DIP — depends on {@link HistoricalVolumePort} abstraction, not the DB.</li>
 *   <li>ISP — exposes only {@link Scanner} to the strategy layer.</li>
 * </ul>
 *
 * <p>Port of {@code engine/volume_scanner.py} {@code VolumeScanner} +
 * {@code VolumeTracker} + {@code VolumeScanResult}.
 */
@Service
public class VolumeScanner implements Scanner {

    private static final Logger log = LoggerFactory.getLogger(VolumeScanner.class);

    private final HistoricalVolumePort historicalVolume;
    private final SignalCooldown cooldown;

    /** token → live tracker */
    private final Map<String, VolumeTracker> trackers = new ConcurrentHashMap<>();
    /** token → latest spike result (for getSpikes()) */
    private final Map<String, Observation> latestSpikes = new ConcurrentHashMap<>();

    private volatile double spikeMultiplier;
    private long ticksProcessed = 0L;
    private long spikesDetectedToday = 0L;

    public VolumeScanner(HistoricalVolumePort historicalVolume,
                         ScannerProperties props) {
        this.historicalVolume = historicalVolume;
        double mult = (props != null && props.volume() != null)
                ? props.volume().spikeMultiplier() : 2.0;
        this.spikeMultiplier = mult <= 0 ? 2.0 : mult;
        this.cooldown = new SignalCooldown(Duration.ofMinutes(30));
    }

    /** Runtime-tunable spike threshold (port of {@code set_spike_multiplier}). */
    public void setSpikeMultiplier(double multiplier) {
        this.spikeMultiplier = multiplier <= 0 ? 2.0 : multiplier;
        log.info("Volume spike multiplier updated to {}x", this.spikeMultiplier);
    }

    @Override
    public SignalSource source() {
        return SignalSource.VOLUME_SPIKE;
    }

    @Override
    public List<Observation> onTick(Tick tick) {
        ticksProcessed++;
        if (tick == null || tick.token() == null) return List.of();

        VolumeTracker tracker = trackers.computeIfAbsent(tick.token(),
                t -> new VolumeTracker(tick.symbol(), t));
        tracker.update(tick);

        VolumeEvaluation ev = evaluate(tracker, tick.timestamp());
        if (ev == null || !ev.spike) return List.of();

        if (cooldown.isOnCooldown(tick.token())) return List.of();

        Observation obs = toObservation(tracker, ev);
        latestSpikes.put(tick.token(), obs);
        spikesDetectedToday++;
        cooldown.mark(tick.token());

        log.info("🔥 VOLUME SPIKE: {} | {:.1f}x normalized | vol={} vs avg={} | ₹{} | {}",
                ev.symbol, ev.timeNormalizedRatio, ev.cumulativeVolume, ev.avgVolume30d,
                ev.price, ev.strength);
        return List.of(obs);
    }

    @Override
    public void resetDaily() {
        trackers.values().forEach(VolumeTracker::reset);
        latestSpikes.clear();
        cooldown.reset();
        spikesDetectedToday = 0L;
        log.info("Volume scanner reset for new trading day");
    }

    /** All currently-detected spikes, strongest first. */
    public List<Observation> getSpikes() {
        List<Observation> list = new ArrayList<>(latestSpikes.values());
        list.sort(Comparator.comparingDouble(
                (Observation o) -> strengthScore(o.confidence())).reversed());
        return list;
    }

    /** Snapshot for one token, or {@code null}. */
    public VolumeSnapshot snapshot(String token) {
        VolumeTracker t = trackers.get(token);
        if (t == null) return null;
        long[] avg = historicalVolume.averagesForToken(token);
        if (avg == null) return null;
        return new VolumeSnapshot(t.symbol, t.lastUpdate != null ? t.lastUpdate : nowInstant(),
                t.cumulativeVolume, avg[0], avg.length > 1 ? avg[1] : avg[0]);
    }

    public Map<String, Object> stats() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("tracked_stocks", trackers.size());
        m.put("active_spikes", latestSpikes.size());
        m.put("spikes_today", spikesDetectedToday);
        m.put("ticks_processed", ticksProcessed);
        m.put("spike_multiplier", spikeMultiplier);
        return m;
    }

    // ── Evaluation ────────────────────────────────────────────

    private VolumeEvaluation evaluate(VolumeTracker tracker, java.time.Instant timestamp) {
        long[] avg = historicalVolume.averagesForToken(tracker.token);
        if (avg == null || avg[0] == 0) return null;

        long avg30d = avg[0];
        long avg90d = avg.length > 1 ? avg[1] : avg30d;

        double rawRatio = (double) tracker.cumulativeVolume / avg30d;

        LocalTime now = (timestamp == null)
                ? LocalTime.now(MarketConstants.IST)
                : timestamp.atZone(MarketConstants.IST).toLocalTime();
        double expectedPct = VolumeCurve.expectedFraction(now);
        if (expectedPct <= 0) return null;

        double expectedVolume = avg30d * expectedPct;
        if (expectedVolume <= 0) return null;

        double timeNormalizedRatio = tracker.cumulativeVolume / expectedVolume;
        boolean spike = timeNormalizedRatio >= spikeMultiplier;
        SignalStrength strength = strengthFor(timeNormalizedRatio);

        return new VolumeEvaluation(
                tracker.symbol, tracker.token, tracker.cumulativeVolume,
                avg30d, avg90d, rawRatio, timeNormalizedRatio,
                strength, tracker.lastPrice, spike,
                tracker.exchangeVwap, tracker.buySellRatio,
                tracker.bidAskSpread, tracker.gapPct);
    }

    /** Strength bands — port of {@code _determine_strength}. */
    private static SignalStrength strengthFor(double ratio) {
        if (ratio >= 5.0) return SignalStrength.EXTREME;
        if (ratio >= 3.0) return SignalStrength.STRONG;
        if (ratio >= 2.0) return SignalStrength.MODERATE;
        return SignalStrength.WEAK;
    }

    private static double strengthScore(double confidence) {
        return confidence;
    }

    private static Observation toObservation(VolumeTracker t, VolumeEvaluation ev) {
        Map<String, Object> details = new LinkedHashMap<>();
        details.put("volume", ev.cumulativeVolume);
        details.put("avg_30d", ev.avgVolume30d);
        details.put("raw_ratio", ev.rawRatio);
        details.put("normalized_ratio", ev.timeNormalizedRatio);
        details.put("exchange_vwap", Math.round(ev.exchangeVwap * 100.0) / 100.0);
        details.put("buy_sell_ratio", Math.round(ev.buySellRatio * 100.0) / 100.0);
        details.put("bid_ask_spread", Math.round(ev.bidAskSpread * 100.0) / 100.0);
        details.put("gap_pct", Math.round(ev.gapPct * 100.0) / 100.0);
        double confidence = switch (ev.strength) {
            case EXTREME -> 0.85;
            case STRONG  -> 0.75;
            case MODERATE -> 0.60;
            case WEAK    -> 0.40;
        };
        return new Observation(
                ev.symbol, ev.token, SignalSource.VOLUME_SPIKE,
                "bullish", confidence, ev.price,
                "volume_spike", null, details);
    }

    private static java.time.Instant nowInstant() {
        return ZonedDateTime.now(MarketConstants.IST).toInstant();
    }

    // ── Per-token live tracker (port of VolumeTracker) ────────

    static final class VolumeTracker {
        final String symbol;
        final String token;
        long cumulativeVolume = 0L;
        long tickCount = 0L;
        double lastPrice = 0.0;
        double openPrice = 0.0;
        double highPrice = 0.0;
        double lowPrice = Double.POSITIVE_INFINITY;
        java.time.Instant lastUpdate = null;
        double exchangeVwap = 0.0;
        double buySellRatio = 1.0;
        double bidAskSpread = 0.0;
        double prevClose = 0.0;
        double gapPct = 0.0;

        VolumeTracker(String symbol, String token) {
            this.symbol = symbol;
            this.token = token;
        }

        void update(Tick tick) {
            this.cumulativeVolume = tick.volume();      // WS sends cumulative
            this.lastPrice = tick.ltp();
            this.tickCount++;
            this.lastUpdate = tick.timestamp();
            if (tick.open() > 0) this.openPrice = tick.open();
            if (tick.high() > 0) this.highPrice = tick.high();
            if (tick.low() > 0 && tick.low() < this.lowPrice) this.lowPrice = tick.low();
            if (tick.avgTradePrice() > 0) this.exchangeVwap = tick.avgTradePrice();
            if (tick.totalSellQty() > 0) {
                this.buySellRatio = (double) tick.totalBuyQty() / tick.totalSellQty();
            }
            if (tick.bestBid() > 0 && tick.bestAsk() > 0) {
                this.bidAskSpread = tick.bestAsk() - tick.bestBid();
            }
            if (tick.close() > 0 && this.prevClose == 0) {
                this.prevClose = tick.close();
                if (this.openPrice > 0) {
                    this.gapPct = ((this.openPrice - tick.close()) / tick.close()) * 100.0;
                }
            }
        }

        void reset() {
            this.cumulativeVolume = 0L;
            this.tickCount = 0L;
            this.lastPrice = 0.0;
            this.openPrice = 0.0;
            this.highPrice = 0.0;
            this.lowPrice = Double.POSITIVE_INFINITY;
            this.lastUpdate = null;
            this.exchangeVwap = 0.0;
            this.buySellRatio = 1.0;
            this.bidAskSpread = 0.0;
            this.prevClose = 0.0;
            this.gapPct = 0.0;
        }
    }

    /** Immutable evaluation snapshot (port of VolumeScanResult). */
    private record VolumeEvaluation(
            String symbol, String token, long cumulativeVolume,
            long avgVolume30d, long avgVolume90d,
            double rawRatio, double timeNormalizedRatio,
            SignalStrength strength, double price, boolean spike,
            double exchangeVwap, double buySellRatio,
            double bidAskSpread, double gapPct) {}
}
