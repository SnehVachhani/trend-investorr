package com.trendinvestorr.data.fetcher;

import com.trendinvestorr.core.constants.Exchange;
import com.trendinvestorr.data.model.Stock;

import java.util.List;
import java.util.Optional;

/**
 * Instrument-master port — abstracts the SmartAPI scrip-master download/cache/lookup
 * pipeline. Domain code asks for {@link Stock} records; the impl decides whether to
 * resolve from disk cache, HTTP download, or the local {@code stocks} table.
 *
 * <p>Port of {@code data.fetchers.instrument_loader.InstrumentLoader}.
 */
public interface InstrumentLookupPort {

    /** Refresh the instrument master (fetch + cache when {@code forceRefresh}). */
    int load(boolean forceRefresh) throws com.trendinvestorr.core.exceptions.DataFetchError;

    default int load() throws com.trendinvestorr.core.exceptions.DataFetchError {
        return load(false);
    }

    Optional<Stock> bySymbol(String symbol, Exchange exchange);

    Optional<Stock> byToken(String token);

    List<Stock> byExchange(Exchange exchange);

    /** Substring search across symbol/name. */
    List<Stock> search(String query, Exchange filterExchange, int maxResults);

    default List<Stock> search(String query) {
        return search(query, null, 20);
    }

    /** Equity-series symbols only ({@code EQ/BE/SM/ST/BZ}). */
    List<String> nseEquitySymbols();

    /** Sync loaded instruments to the {@code stocks} DB table; returns rows upserted. */
    int syncToDb();
}
