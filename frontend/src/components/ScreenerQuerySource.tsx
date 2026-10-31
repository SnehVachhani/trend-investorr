'use client';

import { useEffect, useRef, useState, type ChangeEvent, type Dispatch, type SetStateAction } from 'react';
import {
  completeScreenerQueryCandidateRefresh,
  failScreenerQueryCandidateRefresh,
  getScreenerQueryCandidateRefresh,
  getStockDisplayMetadata,
  isEquityScannerResult,
  reportScreenerQueryCandidateProgress,
  startScreenerQueryCandidateRefresh,
  type EnhancedMomentumScanResponse,
  type EnhancedMomentumScanResult,
  type ScreenerQueryCandidateRefreshJob,
  type ScreenerQueryCandidateRow,
} from '@/lib/api';
import { usePersistentState } from '@/lib/persistence';
import { parseScreenerQueryCsv } from '@/lib/screener-query-csv';

export interface ScreenerQuerySnapshot {
  query: string;
  updatedAt: string;
  stockMappingVersion?: number;
  unmatchedSymbols: number;
  pageCount: number;
  metricsByStockId: Record<string, {
    roePct: number | null;
    profitGrowth1yPct: number | null;
    marketCapCr: number | null;
  }>;
  scan: EnhancedMomentumScanResponse;
}

interface ScreenerQuerySourceProps {
  query: string;
  setQuery: Dispatch<SetStateAction<string>>;
  snapshot: ScreenerQuerySnapshot | null;
  onSnapshot: (snapshot: ScreenerQuerySnapshot) => void;
}

function screenerQueryUrl(query: string): string {
  return `https://www.screener.in/screen/raw/?order=&query=${encodeURIComponent(query.trim())}&sort=&source=`;
}

function waitForScreenerConnector(): Promise<boolean> {
  const nonce = window.crypto.randomUUID();
  return new Promise((resolve) => {
    const timeout = window.setTimeout(() => {
      window.removeEventListener('message', onMessage);
      resolve(false);
    }, 1200);
    const onMessage = (event: MessageEvent) => {
      if (event.source !== window || event.origin !== window.location.origin) return;
      if (event.data?.type !== 'trend-investorr:screener:ready' || event.data?.nonce !== nonce) return;
      window.clearTimeout(timeout);
      window.removeEventListener('message', onMessage);
      resolve(true);
    };
    window.addEventListener('message', onMessage);
    window.postMessage({ type: 'trend-investorr:screener:ping', nonce }, window.location.origin);
  });
}

function requestScreenerConnectorStatus(): void {
  window.postMessage({ type: 'trend-investorr:screener:status-request' }, window.location.origin);
}

function acknowledgeScreenerConnectorJob(jobId: string): void {
  window.postMessage({ type: 'trend-investorr:screener:ack', jobId }, window.location.origin);
}

function indiaDate(): string {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Kolkata',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(new Date());
}

async function buildSnapshot(
  rows: ScreenerQueryCandidateRow[],
  query: string,
  updatedAt: string,
  pageCount: number,
): Promise<ScreenerQuerySnapshot> {
  const metadata = await getStockDisplayMetadata(rows.map((row) => ({ symbol: row.symbol })));
  const metricsByStockId: ScreenerQuerySnapshot['metricsByStockId'] = {};
  const asOfDate = indiaDate();
  const results = rows.map((row, index) => {
    const symbolKey = row.symbol.trim().toUpperCase();
    const baseSymbolKey = symbolKey.replace(/-(EQ|SM|BE|BZ|ST)$/, '');
    const stockId = metadata.stockIdsBySymbol[baseSymbolKey]
      ?? metadata.stockIdsBySymbol[symbolKey]
      ?? -(index + 1);
    metricsByStockId[String(stockId)] = {
      roePct: row.roePct,
      profitGrowth1yPct: row.profitGrowth1yPct,
      marketCapCr: row.marketCapCr,
    };
    return {
      rank: index + 1,
      stock_id: stockId,
      symbol: row.symbol,
      name: metadata.companyNames[row.symbol] || row.name,
      company_name: row.name,
      sector: stockId > 0 ? metadata.sectorsByStockId[String(stockId)] ?? null : null,
      industry: row.industry ?? '',
      market_cap_cr: row.marketCapCr ?? 0,
      as_of_date: asOfDate,
      close: row.close ?? 0,
      return_20d: null,
      return_60d: null,
      return_120d: null,
      return_252d: null,
      market_relative_20d: null,
      market_relative_60d: null,
      market_relative_120d: null,
      volume_ratio: null,
      volume_ratio_20d: null,
      volume_ratio_60d: null,
      volume_ratio_120d: null,
      volume_ratio_252d: null,
      avg_turnover_20d_cr: null,
      avg_turnover_60d_cr: null,
      avg_turnover_120d_cr: null,
      avg_turnover_252d_cr: null,
      avg_delivery_pct_20d: null,
      avg_delivery_pct_60d: null,
      avg_delivery_pct_120d: null,
      avg_delivery_pct_252d: null,
      atr_pct_20d: null,
      atr_pct_60d: null,
      atr_pct_120d: null,
      atr_pct_252d: null,
      realized_volatility_pct: null,
      realized_volatility_20d: null,
      realized_volatility_60d: null,
      realized_volatility_120d: null,
      realized_volatility_252d: null,
      positive_ratio_20d: null,
      positive_ratio_60d: null,
      positive_ratio_120d: null,
      positive_ratio_252d: null,
      breakout_20: false,
      breakout_55: false,
      at_all_time_high: false,
      breakout_margin_atr: null,
      extension_atr: null,
      observations: 0,
      enhanced_score: null,
      base_momentum_score: null,
      market_relative_strength_score: null,
      risk_adjusted_momentum_score: null,
      risk_adjusted_20d: null,
      risk_adjusted_60d: null,
      risk_adjusted_120d: null,
      risk_adjusted_252d: null,
      volume_participation_score: null,
      volume_ratio_score: null,
      turnover_score: null,
      breakout_quality_score: null,
      market_regime_score: null,
      sector_score: null,
      sector_context_available: false,
      trend_aligned: false,
      category: 'screener_query',
    } as unknown as EnhancedMomentumScanResult;
  }).filter(isEquityScannerResult);

  const scan = {
    scanner: 'screener_query',
    as_of_date: asOfDate,
    universe: 'Screener.in query results',
    universe_stocks: rows.length,
    scanned_stocks: rows.length,
    history_eligible_stocks: 0,
    matching_stocks: results.length,
    returned_stocks: results.length,
    filters: {},
    market_context: {
      available: false,
      regime: 'not_applicable',
      regime_score: null,
      primary_benchmark: null,
      nifty: { available: false, observations: 0, trend: 'not_available', return_60d: null },
      sensex: { available: false, observations: 0, trend: 'not_available', return_60d: null },
      sector: { available: false, weight: 0, status: 'not_available' },
      warnings: [],
    },
    formula: { source: 'Screener.in paginated query results', pages: String(pageCount) },
    results,
  } as EnhancedMomentumScanResponse;
  const unmatchedSymbols = results.filter((result) => result.stock_id < 0).length;

  return {
    query: query.trim(),
    updatedAt,
    stockMappingVersion: 3,
    unmatchedSymbols,
    pageCount,
    metricsByStockId,
    scan,
  };
}

function describeProgress(job: ScreenerQueryCandidateRefreshJob | null): string {
  if (!job) return '';
  if (job.status === 'waiting_for_login') return 'Sign in in the opened Chrome window to continue the refresh.';
  if (job.progress.pages_completed > 0) {
    return `Page ${job.progress.pages_completed} · ${job.progress.candidates_found} companies found. ${job.progress.detail}`;
  }
  return job.progress.detail;
}

export default function ScreenerQuerySource({ query, setQuery, snapshot, onSnapshot }: ScreenerQuerySourceProps) {
  const fileInput = useRef<HTMLInputElement>(null);
  const activeJobIdRef = useRef<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [jobId, setJobId] = usePersistentState<string | null>('workspace.master.screenerQuery.jobId', null);
  const [job, setJob] = useState<ScreenerQueryCandidateRefreshJob | null>(null);

  useEffect(() => {
    activeJobIdRef.current = jobId;
  }, [jobId]);

  useEffect(() => {
    const hasUnmatchedRows = Boolean(snapshot?.scan.results.some((stock) => stock.stock_id <= 0));
    if (
      !snapshot
      || (snapshot.stockMappingVersion === 3 && !hasUnmatchedRows)
      || snapshot.scan.results.length === 0
    ) return;
    let active = true;
    const rows = snapshot.scan.results.map((stock) => {
      const metrics = snapshot.metricsByStockId[String(stock.stock_id)];
      return {
        symbol: stock.symbol,
        name: stock.company_name || stock.name,
        industry: stock.industry,
        close: stock.close > 0 ? stock.close : null,
        marketCapCr: metrics?.marketCapCr ?? (stock.market_cap_cr > 0 ? stock.market_cap_cr : null),
        roePct: metrics?.roePct ?? null,
        profitGrowth1yPct: metrics?.profitGrowth1yPct ?? null,
        rank: stock.rank,
      };
    });
    void buildSnapshot(rows, snapshot.query, snapshot.updatedAt, snapshot.pageCount)
      .then((next) => {
        if (active) onSnapshot(next);
      })
      .catch((cause: unknown) => {
        if (active) setError(cause instanceof Error ? cause.message : 'Unable to rematch saved Screener companies to the local stock master.');
      });
    return () => { active = false; };
  }, [snapshot, onSnapshot]);

  useEffect(() => {
    const onConnectorMessage = (event: MessageEvent) => {
      if (event.source !== window || event.origin !== window.location.origin) return;
      const message = event.data;
      if (!message || typeof message !== 'object') return;
      if (
        message.type === 'trend-investorr:screener:status'
        && activeJobIdRef.current == null
        && typeof message.jobId === 'string'
      ) {
        activeJobIdRef.current = message.jobId;
        setJobId(message.jobId);
        setLoading(true);
      }
      if (message.jobId !== activeJobIdRef.current) return;

      if (
        message.type === 'trend-investorr:screener:progress'
        || (message.type === 'trend-investorr:screener:status'
          && ['running', 'waiting_for_login'].includes(message.status))
      ) {
        void reportScreenerQueryCandidateProgress(message.jobId, {
          status: message.status === 'waiting_for_login' ? 'waiting_for_login' : 'running',
          phase: String(message.phase || 'Reading Screener results'),
          pages_completed: Number(message.pages_completed) || 0,
          candidates_found: Number(message.candidates_found) || 0,
          detail: String(message.detail || ''),
        }).catch((cause) => {
          setError(cause instanceof Error ? cause.message : 'Unable to save Screener refresh progress.');
        });
        return;
      }

      if (
        message.type === 'trend-investorr:screener:complete'
        || (message.type === 'trend-investorr:screener:status' && message.status === 'completed_pending_ack')
      ) {
        const rows = Array.isArray(message.rows) ? message.rows as ScreenerQueryCandidateRow[] : [];
        void completeScreenerQueryCandidateRefresh(message.jobId, {
          page_count: Number(message.page_count) || 0,
          rows,
        }).then((updated) => {
          setJob(updated);
          acknowledgeScreenerConnectorJob(message.jobId);
        }).catch((cause) => {
          setError(cause instanceof Error ? cause.message : 'Unable to save Screener query results.');
        });
        return;
      }

      if (
        message.type === 'trend-investorr:screener:error'
        || (message.type === 'trend-investorr:screener:status' && message.status === 'failed_pending_ack')
      ) {
        const detail = String(message.error || 'The Chrome connector could not finish the refresh.');
        void failScreenerQueryCandidateRefresh(message.jobId, detail).then((updated) => {
          setJob(updated);
          setError(detail);
          setJobId(null);
          setLoading(false);
          acknowledgeScreenerConnectorJob(message.jobId);
        }).catch(() => {
          setError(detail);
          setJobId(null);
          setLoading(false);
          acknowledgeScreenerConnectorJob(message.jobId);
        });
      }
    };

    window.addEventListener('message', onConnectorMessage);
    return () => window.removeEventListener('message', onConnectorMessage);
  }, [setJobId]);

  useEffect(() => {
    requestScreenerConnectorStatus();
    if (!jobId) return;
    const timer = window.setInterval(requestScreenerConnectorStatus, 5000);
    return () => window.clearInterval(timer);
  }, [jobId]);

  useEffect(() => {
    if (!jobId) return;
    let active = true;
    let timer: number | undefined;
    let transientErrors = 0;

    const poll = async () => {
      try {
        const current = await getScreenerQueryCandidateRefresh(jobId);
        if (!active) return;
        transientErrors = 0;
        setJob(current);
        if (current.status === 'completed' && current.result) {
          const next = await buildSnapshot(
            current.result.rows,
            current.result.query,
            current.result.updated_at,
            current.result.page_count,
          );
          if (!active) return;
          onSnapshot(next);
          acknowledgeScreenerConnectorJob(jobId);
          setJobId(null);
          setLoading(false);
          setError('');
          return;
        }
        if (current.status === 'failed') {
          acknowledgeScreenerConnectorJob(jobId);
          setError(current.error || 'Unable to refresh Screener query candidates.');
          setJobId(null);
          setLoading(false);
          return;
        }
        timer = window.setTimeout(() => void poll(), 1200);
      } catch (cause) {
        if (!active) return;
        transientErrors += 1;
        if (transientErrors < 5) {
          timer = window.setTimeout(() => void poll(), 1500);
          return;
        }
        setError(cause instanceof Error ? cause.message : 'Unable to read Screener refresh status.');
        setJobId(null);
        setLoading(false);
      }
    };

    void poll();
    return () => {
      active = false;
      if (timer != null) window.clearTimeout(timer);
    };
  }, [jobId, onSnapshot, setJobId]);

  const startRefresh = async () => {
    if (!query.trim()) {
      setError('Enter the Screener.in query first.');
      return;
    }
    setError('');
    setLoading(true);
    try {
      const connectorReady = await waitForScreenerConnector();
      if (!connectorReady) {
        throw new Error('The Screener connector did not respond from this app tab. Open Trend Investorr in regular Chrome at localhost:3000 or 127.0.0.1:3000, enable the connector for that site, reload the app tab, and retry.');
      }
      const nextJob = await startScreenerQueryCandidateRefresh(query.trim());
      setJob(nextJob);
      activeJobIdRef.current = nextJob.job_id;
      setJobId(nextJob.job_id);
      window.postMessage({
        type: 'trend-investorr:screener:start',
        jobId: nextJob.job_id,
        query: query.trim(),
      }, window.location.origin);
    } catch (cause) {
      setLoading(false);
      setError(cause instanceof Error ? cause.message : 'Unable to start the Screener refresh.');
    }
  };

  const importCsv = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;
    setError('');
    setLoading(true);
    try {
      if (!query.trim()) throw new Error('Enter the Screener.in query first.');
      const csvRows = parseScreenerQueryCsv(await file.text());
      const rows = csvRows.map((row, index) => ({ ...row, rank: index + 1 }));
      const next = await buildSnapshot(rows, query, new Date().toISOString(), 0);
      onSnapshot(next);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to import this Screener CSV.');
    } finally {
      setLoading(false);
    }
  };

  const openQuery = () => {
    if (!query.trim()) {
      setError('Enter the Screener.in query first.');
      return;
    }
    window.open(screenerQueryUrl(query), '_blank', 'noopener,noreferrer');
  };

  const refreshing = loading || Boolean(jobId);

  return (
    <section className="card master-screener-source">
      <div className="master-section-heading">
        <div>
          <h2>Screener.in query</h2>
          <p>Refresh reads every result page in regular Chrome using your Screener session. If prompted, sign in in the opened Screener tab; the app does not ask for your password.</p>
        </div>
        <div className="master-setup-toolbar">
          {query.trim() && <button type="button" className="btn btn-secondary" onClick={openQuery}>Open query in Screener</button>}
          <button type="button" className="btn btn-primary" onClick={() => void startRefresh()} disabled={refreshing}>
            {refreshing ? 'Updating candidates…' : 'Update Screener query candidates'}
          </button>
          <button type="button" className="btn btn-secondary" onClick={() => fileInput.current?.click()} disabled={refreshing}>Import CSV</button>
        </div>
      </div>
      <label className="form-label master-screener-query-label">
        <span>Query expression</span>
        <textarea
          className="input master-screener-query"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Example: Market capitalization > 500 AND Return on equity > 15%"
          rows={3}
          maxLength={5000}
        />
      </label>
      <input ref={fileInput} className="master-screener-file-input" type="file" accept=".csv,text/csv" onChange={(event) => void importCsv(event)} />
      <div className="master-screener-source-footer">
        <span aria-live="polite">{snapshot ? `Last updated ${new Date(snapshot.updatedAt).toLocaleString('en-IN')}` : 'No Screener query candidates loaded yet'}</span>
        <span>{snapshot ? `${snapshot.scan.returned_stocks} candidates from ${snapshot.pageCount || 'CSV'} pages` : 'Refresh uses your saved Screener session.'}</span>
      </div>
      {jobId && <div className="master-data-note" role="status">{describeProgress(job)}</div>}
      {snapshot && snapshot.query !== query.trim() && <div className="master-data-note">The query changed after the last update. Refresh again to replace these candidates.</div>}
      {snapshot && snapshot.unmatchedSymbols > 0 && <div className="master-data-note">{snapshot.unmatchedSymbols} companies have no active NSE equity match in the local stock master, so their chart setup results are unavailable.</div>}
      {error && <div className="bt-error" role="alert">{error}</div>}
      <p className="master-screener-help">One-time setup: load the local Screener connector from <code>chrome-extension/screener-connector</code> in <code>chrome://extensions</code>, then reload this app. It only reads Screener result pages and local Trend Investorr pages; it does not read saved cookies or passwords.</p>
    </section>
  );
}
