package com.trendinvestorr.data.fetcher;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.trendinvestorr.core.constants.CandleInterval;
import com.trendinvestorr.core.constants.Exchange;
import com.trendinvestorr.data.auth.SmartApiSessionPort;
import com.trendinvestorr.data.model.Candle;
import com.trendinvestorr.data.model.Tick;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.client.WebClient;

import java.time.Instant;
import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.ZoneId;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/**
 * Historical candle fetcher bound to the Angel One SmartAPI {@code /rest/secure/angelbroking/
 * market/v1/candle} endpoint. Port of Python {@code HistoricalFetcher}.
 *
 * <p>SRP: this class only fetches & parses candle rows from SmartAPI; persistence, dedup-by-DB,
 * aggregation etc. live in {@link com.trendinvestorr.data.ingestion.DataIngestionService}.
 *
 * <p>DIP: the broker session is injected through {@link SmartApiSessionPort} so tests can swap
 * a stub session rather than mock the SDK.
 *
 * <p>Rate-limit handling is defensive: SmartAPI caps 2000 candles per request and rejects
 * bursts; we chunk {@link #chunkDays(CandleInterval)} days and pause 100 ms between calls.
 */
@Component
public class SmartApiHistoricalFetcher implements HistoricalCandlePort {

    private static final Logger log = LoggerFactory.getLogger(SmartApiHistoricalFetcher.class);
    private static final DateTimeFormatter API_FMT = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm");
    private static final String ENDPOINT = "https://apiconnect.angelone.in/rest/secure/angelbroking/market/v1/candle";

    private final SmartApiSessionPort session;
    private final ObjectMapper json;
    private final WebClient http;

    public SmartApiHistoricalFetcher(SmartApiSessionPort session, ObjectMapper json, WebClient.Builder webClientBuilder) {
        this.session = session;
        this.json = json;
        this.http = webClientBuilder.baseUrl(ENDPOINT).build();
    }

    @Override
    public List<Candle> getCandlesRange(String symbol, String token, Instant start, Instant end,
                                        CandleInterval interval, Exchange exchange) {
        int chunkDays = chunkDays(interval);
        var candles = new ArrayList<Candle>();
        var seen = new LinkedHashSet<String>();
        Instant cursor = start;
        while (cursor.isBefore(end)) {
            Instant chunkEnd = cursor.plus(java.time.Duration.ofDays(chunkDays));
            if (chunkEnd.isAfter(end)) chunkEnd = end;
            try {
                candles.addAll(fetchChunk(symbol, token, cursor, chunkEnd, interval, exchange, seen));
            } catch (Exception e) {
                log.warn("getCandlesRange {} chunk {}..{} failed: {}", symbol, cursor, chunkEnd, e.getMessage());
            }
            cursor = chunkEnd;
            try { Thread.sleep(100); } catch (InterruptedException ie) { Thread.currentThread().interrupt(); }
        }
        candles.sort(java.util.Comparator.comparing(Candle::timestamp));
        return candles;
    }

    private List<Candle> fetchChunk(String symbol, String token, Instant from, Instant to,
                                    CandleInterval interval, Exchange exchange, Set<String> seen) {
        String fromStr = LocalDateTime.ofInstant(from, ZoneId.of("Asia/Kolkata")).format(API_FMT);
        String toStr = LocalDateTime.ofInstant(to, ZoneId.of("Asia/Kolkata")).format(API_FMT);
        List<Candle> out = new ArrayList<>();
        try {
            session.ensureAuthenticated();
            String bearer = session.feedToken();
            String body = http.post()
                    .header("Authorization", "Bearer " + bearer)
                    .header("Content-Type", "application/json")
                    .header("Accept", "application/json")
                    .bodyValue(java.util.Map.of(
                            "exchange", exchange.name(),
                            "symboltoken", token,
                            "interval", interval.apiCode(),
                            "fromdate", fromStr,
                            "todate", toStr))
                    .retrieve()
                    .bodyToMono(String.class)
                    .block(java.time.Duration.ofSeconds(15));
            JsonNode root = json.readTree(body == null ? "{}" : body);
            JsonNode data = root.path("data");
            if (!data.isArray()) return out;
            for (JsonNode row : data) {
                // SmartAPI row: [timestamp, open, high, low, close, volume]
                String ts = row.get(0).asText();
                Instant t = Instant.from(DateTimeFormatter.ISO_OFFSET_DATE_TIME.parse(ts));
                String key = symbol + "_" + t.toString();
                if (!seen.add(key)) continue;
                out.add(new Candle(symbol, t,
                        row.get(1).asDouble(), row.get(2).asDouble(), row.get(3).asDouble(),
                        row.get(4).asDouble(), row.get(5).asLong(), interval));
            }
        } catch (Exception e) {
            log.debug("fetchChunk {} failed: {}", symbol, e.getMessage());
        }
        return out;
    }

    private static int chunkDays(CandleInterval interval) {
        return switch (interval) {
            case ONE_MINUTE -> 30;
            case FIVE_MINUTE -> 100;
            case FIFTEEN_MINUTE -> 200;
            case ONE_HOUR -> 400;
            case ONE_DAY -> 2000;
            case ONE_WEEK -> 2000;
        };
    }

    public List<Long> volumeHistory(String symbol, String token, int days) {
        List<Candle> c = daily(symbol, token, days);
        return c.stream().map(Candle::volume).toList();
    }

    public java.util.Optional<Double> latestPrice(String symbol, String token) {
        return HistoricalCandlePort.super.latestPrice(symbol, token);
    }

    @SuppressWarnings("unused")
    private Tick placeholder() { return null; } // keeps Tick import valid if extended later
}
