package com.trendinvestorr.data.fetcher;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.trendinvestorr.core.constants.CandleInterval;
import com.trendinvestorr.core.constants.MarketConstants;
import org.apache.commons.csv.CSVFormat;
import org.apache.commons.csv.CSVParser;
import org.apache.commons.csv.CSVRecord;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.client.WebClient;

import com.trendinvestorr.data.model.Candle;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneId;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;

/** NSE bhavcopy downloader + parser. Port of Python {@code BhavcopyFetcher}. */

@Component
public class NseBhavcopyFetcher implements BhavcopyPort {

    private static final Logger log = LoggerFactory.getLogger(NseBhavcopyFetcher.class);
    private static final DateTimeFormatter DDMYYYY = DateTimeFormatter.ofPattern("ddMMyyyy");
    private static final String URL =
            "https://nsearchives.nseindia.com/api/reports?archives=%5B%7B%22name%22%3A%22CM%20Bhavcopy/csv%22%2C%22type%22%3A%22archives%22%2C%22section%22%3A%22equities%22%7D%5D&type=equities&mode=single";
    private static final Path CACHE_ROOT = Path.of("data_cache", "bhavcopy");

    private final WebClient http;
    private final ObjectMapper json;

    public NseBhavcopyFetcher(WebClient.Builder builder, ObjectMapper json) {
        this.http = builder.baseUrl("https://nsearchives.nseindia.com").build();
        this.json = json;
        try { Files.createDirectories(CACHE_ROOT); } catch (Exception ignored) {}
    }

    @Override
    public List<Candle> fetch(Instant targetDate, int maxFallbackDays) {
        LocalDate date = targetDate.atZone(ZoneId.of("Asia/Kolkata")).toLocalDate();
        for (int i = 0; i <= maxFallbackDays && i < 7; i++) {
            LocalDate d = date.minusDays(i);
            if (d.getDayOfWeek().getValue() > 5) continue; // skip weekends
            Path cache = CACHE_ROOT.resolve("bhavcopy_" + d + ".csv");
            String csv = readCacheOrDownload(d, cache);
            if (csv != null) {
                try { Files.writeString(cache, csv, StandardCharsets.UTF_8); } catch (Exception ignored) {}
                return parse(csv, d);
            }
        }
        return List.of();
    }

    private String readCacheOrDownload(LocalDate date, Path cache) {
        try {
            if (Files.exists(cache)) return Files.readString(cache, StandardCharsets.UTF_8);
        } catch (Exception ignored) {}
        // Warm cookies then download.
        try {
            http.get().uri("/").retrieve().bodyToMono(String.class).block(java.time.Duration.ofSeconds(10));
        } catch (Exception e) {
            log.debug("NSE cookie prefetch failed: {}", e.getMessage());
        }
        try {
            String fileUrl = "https://nsearchives.nseindia.com/content/cm/BhsecQryData_" + date.format(DDMYYYY) + ".csv";
            return WebClient.builder().baseUrl(fileUrl).build()
                    .get()
                    .header("User-Agent", MarketConstants.NSE_USER_AGENT)
                    .header("Referer", MarketConstants.NSE_REFERER)
                    .retrieve()
                    .bodyToMono(String.class)
                    .block(java.time.Duration.ofSeconds(20));
        } catch (Exception e) {
            log.debug("NSE bhavcopy download {} failed: {}", date, e.getMessage());
            return null;
        }
    }

    private List<Candle> parse(String csv, LocalDate date) {
        List<Candle> out = new ArrayList<>();
        Instant ts = date.atTime(15, 30).atZone(ZoneId.of("Asia/Kolkata")).toInstant();
        try (CSVParser parser = CSVParser.parse(csv, CSVFormat.Builder.create()
                .setHeader().setSkipHeaderRecord(true).setTrim(true).build())) {
            for (CSVRecord r : parser) {
                String series = text(r, "SERIES", "series");
                if (!Set.of("EQ", "BE", "BZ").contains(series.toUpperCase())) continue;
                String symbol = text(r, "SYMBOL", "TckrSymb", "TCKRSYMB");
                double open = num(r, "OPEN", "OpnPric", "OPNPRIC");
                double high = num(r, "HIGH", "HighPric", "HGHPric");
                double low = num(r, "LOW", "LowPric", "LWHPric");
                double close = num(r, "CLOSE", "ClosPric", "CLSPric");
                long vol = (long) num(r, "TTL_TRD_QNTY", "TottrdQnty", "VOLUME");
                long delQty = (long) num(r, "DELIV_QTY", "DlvryQty");
                double delPct = num(r, "DELIV_PER", "DlvryPct");
                out.add(new Candle(symbol, ts, open, high, low, close, vol,
                        CandleInterval.ONE_DAY, delQty, delPct, 0.0));
            }
        } catch (Exception e) {
            log.warn("Bhavcopy parse failed for {}: {}", date, e.getMessage());
        }
        return out;
    }

    private static String text(CSVRecord r, String... names) {
        for (String n : names) {
            if (r.isMapped(n)) {
                String v = r.get(n);
                if (v != null && !v.isBlank()) return v.strip();
            }
        }
        return "";
    }
    private static double num(CSVRecord r, String... names) {
        for (String n : names) {
            if (r.isMapped(n)) {
                String v = r.get(n);
                if (v != null && !v.isBlank()) {
                    try { return Double.parseDouble(v.replace(",", "")); }
                    catch (NumberFormatException ignored) {}
                }
            }
        }
        return 0.0;
    }
    @java.lang.SuppressWarnings("unused") private ObjectMapper json() { return json; }

    @Override
    public List<Candle> deliveryLeaders(Instant targetDate, double minPct, long minVol) {
        return fetch(targetDate, 3).stream()
                .filter(c -> c.deliveryPct() >= minPct && c.volume() >= minVol)
                .sorted(Comparator.comparingDouble(Candle::deliveryPct).reversed())
                .toList();
    }

    @Override
    public List<Candle> volumeLeaders(Instant targetDate, int topN) {
        return fetch(targetDate, 3).stream()
                .sorted(Comparator.comparingLong(Candle::volume).reversed())
                .limit(topN)
                .toList();
    }

    @Override
    public int cleanupOldCache(int keepDays) {
        try (var stream = Files.list(CACHE_ROOT)) {
            var cutoff = Instant.now().minusSeconds((long) keepDays * 86_400L);
            int[] n = {0};
            stream.filter(p -> {
                try { return Files.getLastModifiedTime(p).toInstant().isBefore(cutoff); }
                catch (Exception ignored) { return false; }
            }).forEach(p -> { try { Files.deleteIfExists(p); n[0]++; } catch (Exception ignored) {} });
            return n[0];
        } catch (Exception e) {
            log.debug("bhavcopy cleanup failed: {}", e.getMessage());
            return 0;
        }
    }
    @java.lang.SuppressWarnings("unused") private List<Candle> empty() { return new ArrayList<>(); }
}
