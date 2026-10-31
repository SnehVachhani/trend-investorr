package com.trendinvestorr.data.fetcher;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.trendinvestorr.core.constants.Exchange;
import com.trendinvestorr.core.exceptions.DataFetchError;
import com.trendinvestorr.data.model.Stock;
import com.trendinvestorr.storage.entity.StockEntity;
import com.trendinvestorr.storage.repository.StockRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.client.WebClient;

import java.time.LocalDate;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;

/**
 * SmartAPI scrip-master loader. Port of Python {@code InstrumentLoader}.
 *
 * <p>SRP: downloads the JSON master, builds in-memory symbol/token indexes; the {@link
 * StockRepository} is the persistence back-end used by {@link #syncToDb()}.
 *
 * <p>DIP: callers depend on {@link InstrumentLookupPort}, so the master source can be
 * swapped (e.g. embeeded snapshot, REST, DB-only) without touching consumers.
 */
@Component
public class SmartApiInstrumentLoader implements InstrumentLookupPort {

    private static final Logger log = LoggerFactory.getLogger(SmartApiInstrumentLoader.class);
    private static final String URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json";
    private static final Set<String> EQUITY_SERIES = Set.of("EQ", "BE", "SM", "ST", "BZ");

    private final WebClient http;
    private final ObjectMapper json;
    private final StockRepository stocks;

    private final java.util.Map<String, Stock> bySymbol = new ConcurrentHashMap<>();
    private final java.util.Map<String, Stock> byToken = new ConcurrentHashMap<>();
    private volatile LocalDate loadedDate;
    private volatile int totalInstruments;

    public SmartApiInstrumentLoader(WebClient.Builder builder, ObjectMapper json, StockRepository stocks) {
        this.http = builder.baseUrl(URL).build();
        this.json = json;
        this.stocks = stocks;
    }

    public boolean isLoaded() { return loadedDate != null && !bySymbol.isEmpty(); }
    public int totalInstruments() { return totalInstruments; }

    @Override
    public int load(boolean forceRefresh) throws DataFetchError {
        LocalDate today = LocalDate.now();
        if (!forceRefresh && isLoaded() && today.equals(loadedDate)) {
            return totalInstruments;
        }
        try {
            String body = http.get().retrieve().bodyToMono(String.class).block(java.time.Duration.ofSeconds(30));
            JsonNode root = json.readTree(body == null ? "{}" : body);
            bySymbol.clear();
            byToken.clear();
            int n = 0;
            for (JsonNode row : root) {
                String exch = row.path("exch_seg").asText("");
                if (!Set.of("NSE", "BSE", "NFO", "MCX").contains(exch)) continue;
                String symbol = row.path("symbol").asText("");
                String token = row.path("token").asText("");
                if (symbol.isBlank() || token.isBlank()) continue;
                Stock s = new Stock(symbol, token, row.path("name").asText(""),
                        Exchange.valueOf(exch), row.path("series").asText("EQ"),
                        "", row.path("industry").asText(""),
                        row.path("marketCap").asDouble(0.0),
                        row.path("lotsize").asInt(1));
                bySymbol.put(symbol + "@" + exch, s);
                byToken.put(token, s);
                n++;
            }
            totalInstruments = n;
            loadedDate = today;
            log.info("Instrument master loaded: {} instruments", n);
            return n;
        } catch (Exception e) {
            throw new DataFetchError("Instrument master download failed", e);
        }
    }

    @Override
    public Optional<Stock> bySymbol(String symbol, Exchange exchange) {
        Stock s = bySymbol.get(symbol + "@" + exchange.name());
        if (s != null) return Optional.of(s);
        return bySymbol.values().stream().filter(x -> x.symbol().equalsIgnoreCase(symbol)).findFirst();
    }

    @Override
    public Optional<Stock> byToken(String token) {
        return Optional.ofNullable(byToken.get(token));
    }

    @Override
    public List<Stock> byExchange(Exchange exchange) {
        List<Stock> out = new ArrayList<>();
        for (Stock s : bySymbol.values()) {
            if (s.exchange() == exchange) out.add(s);
        }
        return out;
    }

    @Override
    public List<Stock> search(String query, Exchange filterExchange, int maxResults) {
        String q = query == null ? "" : query.toUpperCase();
        List<Stock> hits = new ArrayList<>();
        for (Stock s : bySymbol.values()) {
            if (filterExchange != null && s.exchange() != filterExchange) continue;
            if (s.symbol().toUpperCase().contains(q) || s.name().toUpperCase().contains(q)) {
                hits.add(s);
                if (hits.size() >= maxResults) break;
            }
        }
        return hits;
    }

    @Override
    public List<String> nseEquitySymbols() {
        List<String> out = new ArrayList<>();
        for (Stock s : byExchange(Exchange.NSE)) {
            if (EQUITY_SERIES.contains(s.series())) out.add(s.symbol());
        }
        return out;
    }

    @Override
    public int syncToDb() {
        int count = 0;
        for (Stock s : byToken.values()) {
            if (!EQUITY_SERIES.contains(s.series()) && s.exchange() != Exchange.NSE) continue;
            StockEntity row = stocks.findByToken(s.token()).orElseGet(StockEntity::new);
            row.setSymbol(s.symbol());
            row.setToken(s.token());
            row.setName(s.name());
            row.setExchange(s.exchange());
            row.setSeries(s.series());
            row.setIndustry(s.industry());
            row.setMarketCapCr(s.marketCapCr());
            row.setLotSize(s.lotSize());
            row.setIsActive(true);
            stocks.save(row);
            count++;
        }
        log.info("syncToDb upserted {} stock rows", count);
        return count;
    }
}
