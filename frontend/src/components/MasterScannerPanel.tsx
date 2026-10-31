'use client';

import Link from 'next/link';
import { useEffect, useMemo, useState } from 'react';
import {
  getEnhancedSetupCandles,
  getFundamentalScoreInputs,
  getMasterCandidateMetrics,
  type EnhancedMomentumScanRequest,
  type EnhancedMomentumScanResponse,
  type EnhancedMomentumScanResult,
  type MasterCandidateMetrics,
  isEquityScannerResult,
} from '@/lib/api';
import { DEFAULT_ENHANCED_MOMENTUM_FILTERS, ENHANCED_MOMENTUM_TASK_KEY, resumeEnhancedMomentumScan, startEnhancedMomentumScan, type EnhancedMomentumScanTask } from '@/lib/enhanced-momentum-task';
import { readPersistentValue, usePersistentState, writePersistentValue } from '@/lib/persistence';
import { assessFundamentals, extractMasterFundamentalMetrics, type FundamentalAssessment } from '@/lib/fundamental-analysis';
import {
  MASTER_SETUP_DEFINITIONS,
  analyzeMasterPreBreakout,
  detectMasterSetups,
  scoreMasterPreBreakout,
  type MasterPreBreakoutAnalysis,
  type MasterPreBreakoutSignal,
  type MasterPreBreakoutStatus,
  type MasterSetupId,
  type MasterSetupSignals,
} from '@/lib/master-setup-scanner';
import type { Candle, StockModalData } from '@/lib/types';
import ScreenerQuerySource, { type ScreenerQuerySnapshot } from './ScreenerQuerySource';
import StockIdentity from './StockIdentity';

type MasterMetric = 'profit_growth_1y_pct' | 'roe_pct' | 'market_cap_cr' | 'fundamental_score';
type RuleOperator = '>=' | '>' | '<=' | '<';
type SetupFilter = 'all' | 'any' | 'none' | MasterSetupId;
type SavedSetupFilter = SetupFilter | 'consolidation_after_rally' | 'consolidation_after_rally_25' | 'consolidation_after_rally_general' | 'consolidation_after_fall' | 'consolidation_16' | 'consolidation_24';
type PreBreakoutFilter = 'all' | MasterPreBreakoutStatus;
type DailyMoveWindow = 20 | 40 | 60;
interface DailyMoveFilter {
  sessions: DailyMoveWindow;
  minimumCount: number | null;
}
interface DailyMoveCounts {
  sessions20: number | null;
  sessions40: number | null;
  sessions60: number | null;
}
interface MasterRule {
  id: string;
  metric: MasterMetric;
  operator: RuleOperator;
  value: number | null;
}
interface MasterFundamentals {
  assessment: FundamentalAssessment;
  netProfitGrowth1yPct: number | null;
  roePct: number | null;
  marketCapCr: number | null;
}
interface MasterScanCacheEntry {
  cacheKey: string;
  candidateMetricsVersion?: number;
  setupSignalsVersion?: number;
  candidateMetrics?: Record<number, MasterCandidateMetrics>;
  dailyMoveCounts?: Record<number, DailyMoveCounts>;
  setupSignals?: Record<number, MasterSetupSignals>;
  preBreakoutSignals?: Record<number, MasterPreBreakoutAnalysis>;
  fundamentals?: Record<number, MasterFundamentals>;
}
type SortKey = 'rank' | 'stock' | 'sector' | 'price' | 'momentum' | 'fundamental' | 'return20' | 'return60' | 'return120' | 'return252' | 'upMove20' | 'upMove40' | 'upMove60' | 'riskAdjusted' | 'atr' | 'volume' | 'breakout' | 'readiness' | 'stage' | 'pivotGap' | 'riskTarget' | 'profitGrowth' | 'roe' | 'marketCap';
type SortDirection = 'asc' | 'desc';

const METRICS: Array<{ value: MasterMetric; label: string; unit: string }> = [
  { value: 'profit_growth_1y_pct', label: 'Net profit growth · 1Y', unit: '%' },
  { value: 'roe_pct', label: 'Return on equity', unit: '%' },
  { value: 'market_cap_cr', label: 'Market capitalisation', unit: '₹ Cr' },
  { value: 'fundamental_score', label: 'Fundamental score', unit: '/100' },
];

const SORT_DEFAULTS: Record<SortKey, SortDirection> = {
  rank: 'asc', stock: 'asc', sector: 'asc', price: 'desc', momentum: 'desc', fundamental: 'desc',
  return20: 'desc', return60: 'desc', return120: 'desc', return252: 'desc', upMove20: 'desc', upMove40: 'desc', upMove60: 'desc', riskAdjusted: 'desc', atr: 'asc', volume: 'desc', breakout: 'desc',
  readiness: 'desc', stage: 'asc', pivotGap: 'asc', riskTarget: 'asc',
  profitGrowth: 'desc', roe: 'desc', marketCap: 'desc',
};

const EMPTY_CANDIDATES: EnhancedMomentumScanResult[] = [];
const MASTER_SCAN_CACHE_KEY = 'workspace.master.scanCache';
const MASTER_CANDIDATE_METRICS_CACHE_VERSION = 2;
const MASTER_SETUP_SIGNALS_CACHE_VERSION = 9;
const DEFAULT_DAILY_MOVE_FILTER: DailyMoveFilter = { sessions: 20, minimumCount: null };

const MASTER_SETUP_GROUPS = [
  {
    id: 'tight-consolidation',
    label: 'Tight consolidation',
    setups: MASTER_SETUP_DEFINITIONS.filter(({ id }) => id === 'consolidation' || id === 'consolidation_10' || id === 'consolidation_15'),
  },
  {
    id: 'consolidation-after-rally',
    label: 'Consolidation after rally',
    setups: MASTER_SETUP_DEFINITIONS.filter(({ id }) => id.startsWith('consolidation_after_rally_')),
  },
  {
    id: 'consolidation-after-fall',
    label: 'Consolidation after fall',
    setups: MASTER_SETUP_DEFINITIONS.filter(({ id }) => id.startsWith('consolidation_after_fall_')),
  },
  {
    id: 'other-setups',
    label: 'Other daily setups',
    setups: MASTER_SETUP_DEFINITIONS.filter(({ id }) => !id.startsWith('consolidation')),
  },
] as const;

function saveMasterScanCache(cacheKey: string, update: Omit<MasterScanCacheEntry, 'cacheKey'>) {
  const saved = readPersistentValue<MasterScanCacheEntry | null>(MASTER_SCAN_CACHE_KEY, null);
  const current = saved?.cacheKey === cacheKey ? saved : { cacheKey };
  writePersistentValue<MasterScanCacheEntry>(MASTER_SCAN_CACHE_KEY, { ...current, ...update });
}

function hasCompleteSetupSignals(
  setupSignals: Record<number, MasterSetupSignals> | undefined,
  stockIds: number[],
): setupSignals is Record<number, MasterSetupSignals> {
  return Boolean(setupSignals && stockIds.every((stockId) => {
    const signals = setupSignals[stockId];
    return signals != null
      && Number.isFinite(signals.candleCount)
      && MASTER_SETUP_DEFINITIONS.every(({ id }) => {
        const signal = signals[id];
        return signal != null && typeof signal.matched === 'boolean' && typeof signal.reason === 'string';
      });
  }));
}

function hasCompletePreBreakoutSignals(
  signals: Record<number, MasterPreBreakoutAnalysis> | undefined,
  stockIds: number[],
): signals is Record<number, MasterPreBreakoutAnalysis> {
  return Boolean(signals && stockIds.every((stockId) => {
    const signal = signals[stockId];
    return signal != null
      && typeof signal.historyReady === 'boolean'
      && Number.isFinite(signal.coreScore)
      && (signal.pivotPrice === null || Number.isFinite(signal.pivotPrice));
  }));
}

function preBreakoutStatusLabel(status: MasterPreBreakoutStatus): string {
  switch (status) {
    case 'near-trigger': return 'Near trigger';
    case 'building': return 'Building';
    case 'developing': return 'Developing';
    case 'breakout-confirmed': return 'Breakout confirmed';
    case 'not-ready': return 'Not ready';
    case 'insufficient-history': return 'Insufficient history';
    case 'unmapped': return 'No database match';
  }
}

function preBreakoutTooltip(signal: MasterPreBreakoutSignal | undefined): string {
  if (!signal) return 'Daily candle scan is still loading.';
  if (signal.status === 'unmapped') return 'No active NSE or BSE equity record is mapped to this Screener company, so there is no instrument ID or candle history to scan.';
  if (signal.score == null) return 'At least 55 completed daily candles are needed for the trend and base analysis.';
  const relativeNote = signal.relativePerformancePct == null
    ? `Relative strength unavailable; neutral ${signal.relativeStrengthScore}/10 points used.`
    : `60-session market-relative return ${formatPercent(signal.relativePerformancePct)}; relative strength ${signal.relativeStrengthScore}/10.`;
  return [
    `${preBreakoutStatusLabel(signal.status)} · readiness score ${signal.score}/100.`,
    `Trend ${signal.trendScore}/20 · base ${signal.baseScore}/20 · pivot proximity ${signal.proximityScore}/25 · volatility contraction ${signal.volatilityScore}/15 · volume ${signal.volumeScore}/10.`,
    `Base ${signal.baseSessions} sessions, ${formatNumber(signal.baseRangePct, 1)}% wide; pivot ₹${formatNumber(signal.pivotPrice, 2)} is ${formatPivotGap(signal.distanceToPivotPct)}.`,
    relativeNote,
  ].join('\n');
}

function hasCompleteDailyMoveCounts(
  countsByStock: Record<number, DailyMoveCounts> | undefined,
  stockIds: number[],
): countsByStock is Record<number, DailyMoveCounts> {
  return Boolean(countsByStock && stockIds.every((stockId) => {
    const counts = countsByStock[stockId];
    return counts != null
      && (counts.sessions20 === null || (typeof counts.sessions20 === 'number' && Number.isFinite(counts.sessions20)))
      && (counts.sessions40 === null || (typeof counts.sessions40 === 'number' && Number.isFinite(counts.sessions40)))
      && (counts.sessions60 === null || (typeof counts.sessions60 === 'number' && Number.isFinite(counts.sessions60)));
  }));
}

function countFivePercentUpSessions(source: Candle[], sessions: DailyMoveWindow): number | null {
  const byTime = new Map<number, Candle>();
  for (const candle of source) {
    if (Number.isFinite(candle.time) && Number.isFinite(candle.close) && candle.close > 0) {
      byTime.set(candle.time, candle);
    }
  }
  const candles = [...byTime.values()].sort((left, right) => left.time - right.time);
  if (candles.length < sessions + 1) return null;

  const window = candles.slice(-(sessions + 1));
  let count = 0;
  for (let index = 1; index < window.length; index += 1) {
    if ((window[index].close / window[index - 1].close - 1) * 100 >= 5) count += 1;
  }
  return count;
}

function countMasterDailyMoves(candles: Candle[]): DailyMoveCounts {
  return {
    sessions20: countFivePercentUpSessions(candles, 20),
    sessions40: countFivePercentUpSessions(candles, 40),
    sessions60: countFivePercentUpSessions(candles, 60),
  };
}

function dailyMoveCountForWindow(counts: DailyMoveCounts | undefined, sessions: DailyMoveWindow): number | null {
  if (!counts) return null;
  if (sessions === 20) return counts.sessions20;
  if (sessions === 40) return counts.sessions40;
  return counts.sessions60;
}

function hasCandidateMetrics(
  candidateMetrics: Record<number, MasterCandidateMetrics> | undefined,
  stockIds: number[],
): candidateMetrics is Record<number, MasterCandidateMetrics> {
  return Boolean(candidateMetrics && stockIds.every((stockId) => Object.prototype.hasOwnProperty.call(candidateMetrics, stockId)));
}

function metricValue(stock: EnhancedMomentumScanResult, fundamentals: MasterFundamentals | undefined, metric: MasterMetric): number | null {
  switch (metric) {
    case 'profit_growth_1y_pct': return fundamentals?.netProfitGrowth1yPct ?? null;
    case 'roe_pct': return fundamentals?.roePct ?? null;
    case 'market_cap_cr': return fundamentals?.marketCapCr ?? (stock.market_cap_cr > 0 ? stock.market_cap_cr : null);
    case 'fundamental_score': return fundamentals?.assessment.score ?? null;
  }
}

function formatNumber(value: number | null | undefined, digits = 1): string {
  return value == null || !Number.isFinite(value) ? '—' : value.toLocaleString('en-IN', { maximumFractionDigits: digits, minimumFractionDigits: digits });
}

function formatPercent(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? '—' : `${value >= 0 ? '+' : ''}${formatNumber(value)}%`;
}

function formatPivotGap(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '—';
  return value >= 0 ? `${formatNumber(value)}% below` : `${formatNumber(Math.abs(value))}% above`;
}

function formatMarketCap(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) || value <= 0 ? '—' : `₹${value.toLocaleString('en-IN', { maximumFractionDigits: 0 })} Cr`;
}

function ruleMatches(stock: EnhancedMomentumScanResult, fundamentals: MasterFundamentals | undefined, rule: MasterRule): boolean {
  if (rule.value == null || !Number.isFinite(rule.value)) return true;
  const value = metricValue(stock, fundamentals, rule.metric);
  if (value == null) return false;
  switch (rule.operator) {
    case '>=': return value >= rule.value;
    case '>': return value > rule.value;
    case '<=': return value <= rule.value;
    case '<': return value < rule.value;
  }
}

function sortValue(
  stock: EnhancedMomentumScanResult,
  fundamentals: MasterFundamentals | undefined,
  key: SortKey,
  dailyMoveCounts?: DailyMoveCounts,
  preBreakout?: MasterPreBreakoutSignal,
): string | number | null {
  switch (key) {
    case 'rank': return stock.rank;
    case 'stock': return stock.company_name || stock.name || stock.symbol;
    case 'sector': return stock.sector || null;
    case 'price': return stock.close;
    case 'momentum': return stock.enhanced_score;
    case 'fundamental': return fundamentals?.assessment.score ?? null;
    case 'return20': return stock.return_20d;
    case 'return60': return stock.return_60d;
    case 'return120': return stock.return_120d;
    case 'return252': return stock.return_252d;
    case 'upMove20': return dailyMoveCounts?.sessions20 ?? null;
    case 'upMove40': return dailyMoveCounts?.sessions40 ?? null;
    case 'upMove60': return dailyMoveCounts?.sessions60 ?? null;
    case 'riskAdjusted': return stock.risk_adjusted_momentum_score;
    case 'atr': return stock.atr_pct_20d;
    case 'volume': return stock.volume_ratio_20d;
    case 'breakout': return stock.at_all_time_high ? 3 : stock.breakout_55 ? 2 : stock.breakout_20 ? 1 : 0;
    case 'readiness': return preBreakout && ['near-trigger', 'building', 'developing'].includes(preBreakout.status) ? preBreakout.score : null;
    case 'stage': return preBreakout ? preBreakoutStatusLabel(preBreakout.status) : null;
    case 'pivotGap': return preBreakout?.distanceToPivotPct == null ? null : Math.abs(preBreakout.distanceToPivotPct);
    case 'riskTarget': return preBreakout?.twoRTargetPrice ?? null;
    case 'profitGrowth': return fundamentals?.netProfitGrowth1yPct ?? null;
    case 'roe': return fundamentals?.roePct ?? null;
    case 'marketCap': return fundamentals?.marketCapCr ?? (stock.market_cap_cr > 0 ? stock.market_cap_cr : null);
  }
}

function SortHeader({ label, sortKey, activeKey, direction, onSort }: {
  label: string;
  sortKey: SortKey;
  activeKey: SortKey;
  direction: SortDirection;
  onSort: (key: SortKey) => void;
}) {
  const active = sortKey === activeKey;
  return (
    <th aria-sort={active ? (direction === 'asc' ? 'ascending' : 'descending') : 'none'}>
      <button type="button" className="momentum-sort-button" onClick={() => onSort(sortKey)}>
        {label}<span aria-hidden="true">{active ? direction === 'asc' ? '↑' : '↓' : '↕'}</span>
      </button>
    </th>
  );
}

function asStockModalData(stock: EnhancedMomentumScanResult, marketCapCr?: number | null): StockModalData {
  return {
    symbol: stock.symbol,
    stock_id: stock.stock_id,
    exchange: stock.exchange,
    name: stock.name,
    company_name: stock.company_name,
    sector: stock.sector,
    industry: stock.industry,
    as_of_date: stock.as_of_date,
    close: stock.close,
    market_cap_cr: marketCapCr ?? stock.market_cap_cr,
    return_20d: stock.return_20d,
    market_relative_60d: stock.market_relative_60d,
    enhanced_score: stock.enhanced_score,
    breakout_20: stock.breakout_20,
    breakout_55: stock.breakout_55,
    at_all_time_high: stock.at_all_time_high,
    trend_aligned: stock.trend_aligned,
  };
}

export default function MasterScannerPanel() {
  const [scan] = usePersistentState<EnhancedMomentumScanResponse | null>('workspace.enhancedMomentum.scan', null);
  const [scanTask] = usePersistentState<EnhancedMomentumScanTask | null>(ENHANCED_MOMENTUM_TASK_KEY, null);
  const [momentumFilters] = usePersistentState<EnhancedMomentumScanRequest>('workspace.enhancedMomentum.filters', DEFAULT_ENHANCED_MOMENTUM_FILTERS);
  const [source, setSource] = usePersistentState<'enhanced-momentum' | 'screener-query'>('workspace.master.source', 'enhanced-momentum');
  const [screenerQuery, setScreenerQuery] = usePersistentState('workspace.master.screenerQuery.expression', '');
  const [screenerSnapshot, setScreenerSnapshot] = usePersistentState<ScreenerQuerySnapshot | null>('workspace.master.screenerQuery.snapshot', null);
  const [rules, setRules] = usePersistentState<MasterRule[]>('workspace.master.rules', []);
  const [setupFilter, setSetupFilter] = usePersistentState<SavedSetupFilter>('workspace.master.setupFilter', 'all');
  const [preBreakoutFilter, setPreBreakoutFilter] = usePersistentState<PreBreakoutFilter>('workspace.master.preBreakoutFilter', 'all');
  const [dailyMoveFilter, setDailyMoveFilter] = usePersistentState<DailyMoveFilter>('workspace.master.dailyMoveFilter', DEFAULT_DAILY_MOVE_FILTER);
  const activeSetupFilter: SetupFilter = setupFilter === 'consolidation_after_rally'
    ? 'all'
    : setupFilter === 'consolidation_after_fall'
      ? 'consolidation_after_fall_20_plus'
      : setupFilter === 'consolidation_after_rally_25' || setupFilter === 'consolidation_after_rally_general'
      ? 'consolidation_after_rally_20_plus'
      : setupFilter === 'consolidation_16'
        ? 'consolidation_10'
        : setupFilter === 'consolidation_24'
          ? 'consolidation_15'
          : setupFilter;
  const [dailyMoveCounts, setDailyMoveCounts] = useState<Record<number, DailyMoveCounts>>({});
  const [setupSignals, setSetupSignals] = useState<Record<number, MasterSetupSignals>>({});
  const [preBreakoutAnalyses, setPreBreakoutAnalyses] = useState<Record<number, MasterPreBreakoutAnalysis>>({});
  const [setupLoadedKey, setSetupLoadedKey] = useState('');
  const [setupLoadingKey, setSetupLoadingKey] = useState('');
  const [setupError, setSetupError] = useState('');
  const [setupRefreshId, setSetupRefreshId] = useState(0);
  const [fundamentals, setFundamentals] = useState<Record<number, MasterFundamentals>>({});
  const [fundamentalsLoadedKey, setFundamentalsLoadedKey] = useState('');
  const [fundamentalsLoadingKey, setFundamentalsLoadingKey] = useState('');
  const [fundamentalsError, setFundamentalsError] = useState('');
  const [candidateMetrics, setCandidateMetrics] = useState<Record<number, MasterCandidateMetrics>>({});
  const [candidateMetricsLoadingKey, setCandidateMetricsLoadingKey] = useState('');
  const [candidateMetricsError, setCandidateMetricsError] = useState('');
  const [scanError, setScanError] = useState('');
  const [sort, setSort] = useState<{ key: SortKey; direction: SortDirection }>({ key: 'readiness', direction: 'desc' });

  const activeScan = source === 'screener-query' ? screenerSnapshot?.scan ?? null : scan;
  const sourceCandidateResults = useMemo(
    () => (activeScan?.results ?? EMPTY_CANDIDATES).filter(isEquityScannerResult),
    [activeScan?.results],
  );
  const candidateResults = useMemo(
    () => sourceCandidateResults.map((stock) => {
      const metrics = candidateMetrics[stock.stock_id];
      if (!metrics) return stock;
      if (source === 'screener-query') return { ...stock, ...metrics };
      const enriched = { ...stock } as EnhancedMomentumScanResult;
      for (const [field, value] of Object.entries(metrics)) {
        const values = enriched as unknown as Record<string, unknown>;
        if (values[field] == null && value != null) values[field] = value;
      }
      return enriched;
    }),
    [sourceCandidateResults, candidateMetrics, source],
  );
  const needsCandidateMetrics = source === 'screener-query' || sourceCandidateResults.some((stock) => (
    stock.enhanced_score == null
    || stock.return_20d == null
    || stock.return_60d == null
    || stock.volume_ratio_20d == null
    || stock.atr_pct_20d == null
  ));
  const candidateIdsKey = candidateResults.map((stock) => stock.stock_id).join(',');
  const matchedCandidateIdsKey = candidateResults.filter((stock) => stock.stock_id > 0).map((stock) => stock.stock_id).join(',');
  const sectorKey = candidateResults.map((stock) => `${stock.stock_id}:${stock.sector ?? ''}`).join(',');
  const asOfDate = activeScan?.as_of_date ?? '';
  const dataKey = `${asOfDate}|${candidateIdsKey}`;
  const fundamentalDataKey = `${dataKey}|${sectorKey}`;
  const scanGeneration = source === 'screener-query'
    ? screenerSnapshot?.updatedAt ?? 'no-screener-snapshot'
    : scanTask?.id ?? 'saved-snapshot';
  const masterCacheKey = `${scanGeneration}|${fundamentalDataKey}`;
  const setupRunKey = `${masterCacheKey}|${setupRefreshId}`;
  const activeRules = rules.filter((rule) => rule.value != null && Number.isFinite(rule.value));
  const scanning = source === 'enhanced-momentum' && scanTask?.status === 'running';
  const setupLoading = Boolean(dataKey && setupLoadingKey === setupRunKey);
  const setupReady = Boolean(dataKey && setupLoadedKey === setupRunKey);
  const preBreakoutSignals = useMemo(
    () => setupReady
      ? Object.fromEntries(candidateResults.map((stock) => [
        stock.stock_id,
        stock.stock_id > 0
          ? scoreMasterPreBreakout(preBreakoutAnalyses[stock.stock_id], stock.market_relative_60d)
          : { ...scoreMasterPreBreakout(undefined, null), status: 'unmapped' as const },
      ])) as Record<number, MasterPreBreakoutSignal>
      : {},
    [candidateResults, preBreakoutAnalyses, setupReady],
  );
  const fundamentalsLoading = Boolean(fundamentalDataKey && fundamentalsLoadingKey === masterCacheKey);
  const fundamentalsReady = Boolean(fundamentalDataKey && fundamentalsLoadedKey === masterCacheKey);
  const candidateMetricsLoading = Boolean(needsCandidateMetrics && matchedCandidateIdsKey && candidateMetricsLoadingKey === masterCacheKey);

  useEffect(() => {
    if (source !== 'enhanced-momentum' || scanTask?.status !== 'running') return;
    void resumeEnhancedMomentumScan(scanTask)
      ?.catch((error: unknown) => setScanError(error instanceof Error ? error.message : 'Unable to update enhanced candidates'));
  }, [scanTask, source]);

  useEffect(() => {
    if (setupFilter === 'consolidation_after_rally') setSetupFilter('all');
    else if (setupFilter === 'consolidation_after_fall') setSetupFilter('consolidation_after_fall_20_plus');
    else if (setupFilter === 'consolidation_after_rally_25' || setupFilter === 'consolidation_after_rally_general') setSetupFilter('consolidation_after_rally_20_plus');
    else if (setupFilter === 'consolidation_16') setSetupFilter('consolidation_10');
    else if (setupFilter === 'consolidation_24') setSetupFilter('consolidation_15');
  }, [setupFilter, setSetupFilter]);

  useEffect(() => {
    if (!asOfDate) {
      setSetupSignals({});
      setPreBreakoutAnalyses({});
      setDailyMoveCounts({});
      setSetupLoadedKey('');
      setSetupLoadingKey('');
      setSetupError('');
      return;
    }
    if (!matchedCandidateIdsKey) {
      setSetupSignals({});
      setPreBreakoutAnalyses({});
      setDailyMoveCounts({});
      setSetupLoadedKey(setupRunKey);
      setSetupLoadingKey('');
      setSetupError('');
      return;
    }
    let active = true;
    const stockIds = matchedCandidateIdsKey.split(',').map(Number).filter(Number.isFinite);
    const cached = readPersistentValue<MasterScanCacheEntry | null>(MASTER_SCAN_CACHE_KEY, null);
    if (
      setupRefreshId === 0
      && cached?.cacheKey === masterCacheKey
      && cached.setupSignalsVersion === MASTER_SETUP_SIGNALS_CACHE_VERSION
      && hasCompleteSetupSignals(cached.setupSignals, stockIds)
      && hasCompletePreBreakoutSignals(cached.preBreakoutSignals, stockIds)
      && hasCompleteDailyMoveCounts(cached.dailyMoveCounts, stockIds)
    ) {
      setSetupSignals(cached.setupSignals);
      setPreBreakoutAnalyses(cached.preBreakoutSignals);
      setDailyMoveCounts(cached.dailyMoveCounts);
      setSetupLoadedKey(setupRunKey);
      setSetupLoadingKey('');
      setSetupError('');
      return () => { active = false; };
    }
    setSetupSignals({});
    setPreBreakoutAnalyses({});
    setDailyMoveCounts({});
    setSetupLoadingKey(setupRunKey);
    setSetupError('');
    void getEnhancedSetupCandles(stockIds, asOfDate, 120)
      .then((candlesByStock) => {
        if (!active) return;
        const next = Object.fromEntries(stockIds.map((stockId) => [stockId, detectMasterSetups(candlesByStock[String(stockId)] ?? [])]));
        const nextPreBreakout = Object.fromEntries(stockIds.map((stockId) => [stockId, analyzeMasterPreBreakout(candlesByStock[String(stockId)] ?? [])]));
        const nextDailyMoveCounts = Object.fromEntries(stockIds.map((stockId) => [stockId, countMasterDailyMoves(candlesByStock[String(stockId)] ?? [])]));
        setSetupSignals(next);
        setPreBreakoutAnalyses(nextPreBreakout);
        setDailyMoveCounts(nextDailyMoveCounts);
        setSetupLoadedKey(setupRunKey);
        saveMasterScanCache(masterCacheKey, {
          setupSignalsVersion: MASTER_SETUP_SIGNALS_CACHE_VERSION,
          setupSignals: next,
          preBreakoutSignals: nextPreBreakout,
          dailyMoveCounts: nextDailyMoveCounts,
        });
      })
      .catch((error: unknown) => {
        if (!active) return;
        setSetupError(error instanceof Error ? error.message : 'Unable to load daily charts for setup scanning and daily-move counts');
      })
      .finally(() => {
        if (active) setSetupLoadingKey('');
      });
    return () => { active = false; };
  }, [matchedCandidateIdsKey, asOfDate, dataKey, masterCacheKey, setupRunKey, setupRefreshId]);

  useEffect(() => {
    if (!needsCandidateMetrics || !asOfDate || !matchedCandidateIdsKey) {
      setCandidateMetrics({});
      setCandidateMetricsLoadingKey('');
      setCandidateMetricsError('');
      return;
    }
    let active = true;
    const stockIds = matchedCandidateIdsKey.split(',').map(Number).filter(Number.isFinite);
    const cached = readPersistentValue<MasterScanCacheEntry | null>(MASTER_SCAN_CACHE_KEY, null);
    if (
      cached?.cacheKey === masterCacheKey
      && cached.candidateMetricsVersion === MASTER_CANDIDATE_METRICS_CACHE_VERSION
      && hasCandidateMetrics(cached.candidateMetrics, stockIds)
    ) {
      setCandidateMetrics(cached.candidateMetrics);
      setCandidateMetricsLoadingKey('');
      setCandidateMetricsError('');
      return () => { active = false; };
    }
    setCandidateMetrics({});
    setCandidateMetricsLoadingKey(masterCacheKey);
    setCandidateMetricsError('');
    void getMasterCandidateMetrics(stockIds, asOfDate)
      .then((metricsByStockId) => {
        if (!active) return;
        const next = Object.fromEntries(stockIds.map((stockId) => [stockId, metricsByStockId[String(stockId)] ?? {}]));
        setCandidateMetrics(next);
        const unavailableCount = stockIds.filter((stockId) => {
          const metrics = metricsByStockId[String(stockId)];
          return !metrics || metrics.enhanced_score == null || metrics.return_20d == null || metrics.return_60d == null;
        }).length;
        setCandidateMetricsError(unavailableCount > 0
          ? `Daily history is insufficient to calculate the full momentum set for ${unavailableCount} mapped candidate${unavailableCount === 1 ? '' : 's'}.`
          : '');
        saveMasterScanCache(masterCacheKey, {
          candidateMetricsVersion: MASTER_CANDIDATE_METRICS_CACHE_VERSION,
          candidateMetrics: next,
        });
      })
      .catch((error: unknown) => {
        if (!active) return;
        setCandidateMetricsError(error instanceof Error ? error.message : 'Unable to load daily momentum and volume metrics');
      })
      .finally(() => {
        if (active) setCandidateMetricsLoadingKey('');
      });
    return () => { active = false; };
  }, [needsCandidateMetrics, asOfDate, matchedCandidateIdsKey, masterCacheKey]);

  useEffect(() => {
    if (!candidateIdsKey) {
      setFundamentals({});
      setFundamentalsLoadedKey('');
      setFundamentalsLoadingKey('');
      setFundamentalsError('');
      return;
    }
    let active = true;
    const candidateIds = candidateIdsKey.split(',').map(Number).filter(Number.isFinite);
    const stockIds = sourceCandidateResults.filter((stock) => stock.stock_id > 0).map((stock) => stock.stock_id);
    const sectorsById = Object.fromEntries(sourceCandidateResults.map((stock) => [stock.stock_id, stock.sector]));
    const screenerMetrics = source === 'screener-query' ? screenerSnapshot?.metricsByStockId ?? {} : {};
    const cached = readPersistentValue<MasterScanCacheEntry | null>(MASTER_SCAN_CACHE_KEY, null);
    if (cached?.cacheKey === masterCacheKey && cached.fundamentals) {
      setFundamentals(cached.fundamentals);
      setFundamentalsLoadedKey(masterCacheKey);
      setFundamentalsLoadingKey('');
      setFundamentalsError('');
      return () => { active = false; };
    }
    if (stockIds.length === 0) {
      const next = Object.fromEntries(candidateIds.map((stockId) => {
        const imported = screenerMetrics[String(stockId)];
        return [stockId, {
          assessment: assessFundamentals(null),
          netProfitGrowth1yPct: imported?.profitGrowth1yPct ?? null,
          roePct: imported?.roePct ?? null,
          marketCapCr: imported?.marketCapCr ?? null,
        }];
      }));
      setFundamentals(next);
      setFundamentalsLoadedKey(masterCacheKey);
      setFundamentalsLoadingKey('');
      saveMasterScanCache(masterCacheKey, { fundamentals: next });
      return () => { active = false; };
    }
    setFundamentals({});
    setFundamentalsLoadingKey(masterCacheKey);
    setFundamentalsError('');
    void getFundamentalScoreInputs(stockIds)
      .then((inputs) => {
        if (!active) return;
        const next = Object.fromEntries(candidateIds.map((stockId) => {
          const input = inputs[String(stockId)] ?? null;
          const imported = screenerMetrics[String(stockId)];
          const localMetrics = extractMasterFundamentalMetrics(input);
          return [stockId, {
            assessment: assessFundamentals(input, sectorsById[stockId]),
            netProfitGrowth1yPct: imported?.profitGrowth1yPct ?? localMetrics.netProfitGrowth1yPct,
            roePct: imported?.roePct ?? localMetrics.roePct,
            marketCapCr: imported?.marketCapCr ?? (typeof input?.market_cap_cr === 'number' && Number.isFinite(input.market_cap_cr) && input.market_cap_cr > 0 ? input.market_cap_cr : null),
          }];
        }));
        setFundamentals(next);
        setFundamentalsLoadedKey(masterCacheKey);
        saveMasterScanCache(masterCacheKey, { fundamentals: next });
      })
      .catch((error: unknown) => {
        if (!active) return;
        setFundamentalsError(error instanceof Error ? error.message : 'Unable to load fundamental inputs');
        setFundamentals(Object.fromEntries(candidateIds.map((stockId) => [stockId, {
          assessment: assessFundamentals(null),
          netProfitGrowth1yPct: screenerMetrics[String(stockId)]?.profitGrowth1yPct ?? null,
          roePct: screenerMetrics[String(stockId)]?.roePct ?? null,
          marketCapCr: screenerMetrics[String(stockId)]?.marketCapCr ?? null,
        }])));
        setFundamentalsLoadedKey(masterCacheKey);
      })
      .finally(() => {
        if (active) setFundamentalsLoadingKey('');
      });
    return () => { active = false; };
  }, [candidateIdsKey, dataKey, fundamentalDataKey, masterCacheKey, sourceCandidateResults, source, screenerSnapshot?.metricsByStockId]);

  const setupCounts = useMemo(() => {
    const counts = Object.fromEntries(MASTER_SETUP_DEFINITIONS.map(({ id }) => [id, 0])) as Record<MasterSetupId, number>;
    let noSetupCount = 0;
    let noDataCount = 0;
    for (const stock of candidateResults) {
      const signals = setupSignals[stock.stock_id];
      if (!signals) continue;
      if (signals.candleCount < 3) {
        noDataCount += 1;
        continue;
      }
      let hasMatch = false;
      for (const { id } of MASTER_SETUP_DEFINITIONS) {
        if (signals[id].matched) {
          counts[id] += 1;
          hasMatch = true;
        }
      }
      if (!hasMatch) noSetupCount += 1;
    }
    return { counts, noSetupCount, noDataCount };
  }, [candidateResults, setupSignals]);
  const candidatesMatchingMasterFilters = useMemo(() => candidateResults.filter((stock) => {
    const fundamental = fundamentals[stock.stock_id];
    if (!activeRules.every((rule) => ruleMatches(stock, fundamental, rule))) return false;
    if (dailyMoveFilter.minimumCount != null) {
      const count = dailyMoveCountForWindow(dailyMoveCounts[stock.stock_id], dailyMoveFilter.sessions);
      if (count == null || count < dailyMoveFilter.minimumCount) return false;
    }
    if (activeSetupFilter === 'all') return true;
    const signals = setupSignals[stock.stock_id];
    if (activeSetupFilter === 'any') return Boolean(signals && MASTER_SETUP_DEFINITIONS.some(({ id }) => signals[id].matched));
    if (activeSetupFilter === 'none') return Boolean(signals && signals.candleCount >= 3 && MASTER_SETUP_DEFINITIONS.every(({ id }) => !signals[id].matched));
    return Boolean(signals?.[activeSetupFilter]?.matched);
  }), [candidateResults, fundamentals, activeRules, dailyMoveFilter, dailyMoveCounts, activeSetupFilter, setupSignals]);
  const preBreakoutCounts = useMemo(() => {
    const counts: Record<MasterPreBreakoutStatus, number> = {
      'near-trigger': 0,
      building: 0,
      developing: 0,
      'breakout-confirmed': 0,
      'not-ready': 0,
      'insufficient-history': 0,
      unmapped: 0,
    };
    for (const stock of candidatesMatchingMasterFilters) {
      const signal = preBreakoutSignals[stock.stock_id];
      if (signal) counts[signal.status] += 1;
    }
    return counts;
  }, [candidatesMatchingMasterFilters, preBreakoutSignals]);
  const preBreakoutRanks = useMemo(() => {
    const eligible = candidateResults
      .map((stock) => ({ stock, signal: preBreakoutSignals[stock.stock_id] }))
      .filter(({ signal }) => signal && signal.score != null && [
        'near-trigger', 'building', 'developing',
      ].includes(signal.status))
      .sort((left, right) => (
        (right.signal?.score ?? -1) - (left.signal?.score ?? -1)
        || (left.signal?.distanceToPivotPct ?? Number.POSITIVE_INFINITY) - (right.signal?.distanceToPivotPct ?? Number.POSITIVE_INFINITY)
        || left.stock.symbol.localeCompare(right.stock.symbol)
      ));
    return Object.fromEntries(eligible.map(({ stock }, index) => [stock.stock_id, index + 1]));
  }, [candidateResults, preBreakoutSignals]);
  const unmatchedStockCount = candidateResults.filter((stock) => stock.stock_id <= 0).length;

  const filteredResults = useMemo(() => {
    const results = candidatesMatchingMasterFilters.filter((stock) => (
      preBreakoutFilter === 'all' || preBreakoutSignals[stock.stock_id]?.status === preBreakoutFilter
    ));
    return [...results].sort((left, right) => {
      const a = sortValue(left, fundamentals[left.stock_id], sort.key, dailyMoveCounts[left.stock_id], preBreakoutSignals[left.stock_id]);
      const b = sortValue(right, fundamentals[right.stock_id], sort.key, dailyMoveCounts[right.stock_id], preBreakoutSignals[right.stock_id]);
      if (a == null || a === '') return b == null || b === '' ? 0 : 1;
      if (b == null || b === '') return -1;
      const comparison = typeof a === 'number' && typeof b === 'number'
        ? a - b
        : String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: 'base' });
      return sort.direction === 'asc' ? comparison : -comparison;
    });
  }, [candidatesMatchingMasterFilters, preBreakoutSignals, preBreakoutFilter, fundamentals, dailyMoveCounts, sort]);

  const sortResults = (key: SortKey) => setSort((current) => ({
    key,
    direction: current.key === key ? (current.direction === 'asc' ? 'desc' : 'asc') : SORT_DEFAULTS[key],
  }));

  const runCandidateScan = () => {
    setScanError('');
    void startEnhancedMomentumScan(momentumFilters)
      .catch((error: unknown) => setScanError(error instanceof Error ? error.message : 'Unable to update enhanced candidates'));
  };

  const addRule = () => setRules((current) => [...current, {
    id: `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
    metric: 'profit_growth_1y_pct',
    operator: '>=',
    value: null,
  }]);

  const updateRule = (id: string, changes: Partial<MasterRule>) => setRules((current) => current.map((rule) => (
    rule.id === id ? { ...rule, ...changes } : rule
  )));

  const removeRule = (id: string) => setRules((current) => current.filter((rule) => rule.id !== id));

  const updateDailyMoveWindow = (sessions: DailyMoveWindow) => {
    setDailyMoveFilter((current) => ({
      ...current,
      sessions,
      minimumCount: current.minimumCount == null ? null : Math.min(current.minimumCount, sessions),
    }));
    setSort({ key: `upMove${sessions}` as SortKey, direction: 'desc' });
  };

  const selectSetup = (filter: SetupFilter) => setSetupFilter((current) => current === filter ? 'all' : filter);
  const selectPreBreakoutFilter = (filter: PreBreakoutFilter) => setPreBreakoutFilter((current) => current === filter ? 'all' : filter);
  const clearMasterFilters = () => {
    setRules([]);
    setSetupFilter('all');
    setDailyMoveFilter((current) => ({ ...current, minimumCount: null }));
  };

  const candidateCount = candidateResults.length;
  const hasOtherMasterFilters = activeRules.length > 0 || dailyMoveFilter.minimumCount != null || activeSetupFilter !== 'all';
  const setupStatusText = setupLoading ? 'Scanning daily charts…' : setupError ? 'Daily chart scan unavailable' : setupReady ? 'Daily chart scan complete' : 'Waiting for candidates';

  return (
    <div className="master-page">
      <section className="master-hero">
        <div>
          <div className="momentum-eyebrow"><span className="momentum-eyebrow-dot" /> Candidate quality + chart patterns</div>
          <h1 className="page-title">Master</h1>
          <p>Choose a candidate source, then refine its stocks with the same company fundamentals and daily chart setup scanners.</p>
        </div>
        <div className="master-hero-actions">
          {source === 'enhanced-momentum' && <>
            <Link href="/enhanced-momentum" className="btn btn-secondary">Adjust momentum filters</Link>
            <button type="button" className="btn btn-primary" onClick={runCandidateScan} disabled={scanning}>
              {scanning ? 'Updating candidates…' : 'Update enhanced candidates'}
            </button>
          </>}
        </div>
      </section>

      <section className="card master-source-nodes" aria-label="Master candidate sources">
        <div>
          <strong>Candidate source</strong>
          <small>Both sources feed the same Master setup scanners and filters.</small>
        </div>
        <div className="master-source-node-list" role="group" aria-label="Candidate source">
          <button type="button" aria-pressed={source === 'enhanced-momentum'} className={`master-source-node${source === 'enhanced-momentum' ? ' is-selected' : ''}`} onClick={() => setSource('enhanced-momentum')}>
            <strong>Enhanced Momentum</strong><small>Local price and volume scanner</small>
          </button>
          <button type="button" aria-pressed={source === 'screener-query'} className={`master-source-node${source === 'screener-query' ? ' is-selected' : ''}`} onClick={() => setSource('screener-query')}>
            <strong>Screener.in Query</strong><small>Fundamental query results</small>
          </button>
        </div>
      </section>

      <section className="master-summary-grid">
        <div className="stat-card"><div className="stat-card-label">{source === 'screener-query' ? 'Screener query candidates' : 'Enhanced candidates'}</div><div className="stat-card-value">{activeScan ? candidateCount : '—'}</div><div className="stat-card-sub">{source === 'screener-query' && screenerSnapshot ? `Updated ${new Date(screenerSnapshot.updatedAt).toLocaleString('en-IN')}` : asOfDate ? `As of ${asOfDate}` : source === 'screener-query' ? 'Refresh the saved query to begin' : 'Run the enhanced scanner to begin'}</div></div>
        <div className="stat-card"><div className="stat-card-label">Candidates with a setup</div><div className="stat-card-value">{setupReady ? candidateCount - setupCounts.noSetupCount - setupCounts.noDataCount - unmatchedStockCount : '—'}</div><div className="stat-card-sub">Across {MASTER_SETUP_DEFINITIONS.length} daily scanners</div></div>
        <div className="stat-card"><div className="stat-card-label">No setup detected</div><div className="stat-card-value">{setupReady ? setupCounts.noSetupCount : '—'}</div><div className="stat-card-sub">{setupStatusText}</div></div>
        <div className="stat-card"><div className="stat-card-label">Insufficient daily history</div><div className="stat-card-value">{setupReady ? setupCounts.noDataCount : '—'}</div><div className="stat-card-sub">At least 3 daily bars are needed to scan{unmatchedStockCount ? ` · ${unmatchedStockCount} have no active NSE/BSE equity match` : ''}</div></div>
      </section>

      {source === 'enhanced-momentum' && (scanError || (scanTask?.status === 'error' && scanTask.error)) && <div className="bt-error">{scanError || scanTask?.error}</div>}
      {setupError && <div className="bt-error">Daily setup scan: {setupError}</div>}
      {scanning && <div className="card master-data-note">Enhanced Momentum is updating the shared candidate list. Master will rescan its fundamentals and daily setup matches when the scan finishes.</div>}
      {source === 'screener-query' && <ScreenerQuerySource query={screenerQuery} setQuery={setScreenerQuery} snapshot={screenerSnapshot} onSnapshot={setScreenerSnapshot} />}

      <section className="card master-filter-card">
        <div className="master-section-heading">
          <div><h2>Fundamental conditions</h2><p>All entered conditions must match. Missing fundamental values are excluded when that metric is filtered; blank values are ignored.</p></div>
          <button type="button" className="btn btn-secondary" onClick={addRule}>Add condition</button>
        </div>
        {rules.length === 0 ? (
          <div className="master-empty-rules">No extra fundamental conditions. Add net profit growth, ROE, market capitalisation, or fundamental score rules to narrow the candidate list.</div>
        ) : (
          <div className="master-rules-list">
            {rules.map((rule) => {
              const metric = METRICS.find((item) => item.value === rule.metric) ?? METRICS[0];
              return (
                <div className="master-rule-row" key={rule.id}>
                  <label className="form-label"><span>Metric</span><select className="input" value={rule.metric} onChange={(event) => updateRule(rule.id, { metric: event.target.value as MasterMetric })}>{METRICS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
                  <label className="form-label"><span>Condition</span><select className="input" value={rule.operator} onChange={(event) => updateRule(rule.id, { operator: event.target.value as RuleOperator })}><option value=">=">≥</option><option value=">">&gt;</option><option value="<=">≤</option><option value="<">&lt;</option></select></label>
                  <label className="form-label"><span>Value · {metric.unit}</span><input className="input" type="number" step="any" value={rule.value ?? ''} placeholder="Enter value" onChange={(event) => updateRule(rule.id, { value: event.target.value.trim() === '' ? null : Number(event.target.value) })} /></label>
                  <button type="button" className="master-remove-rule" onClick={() => removeRule(rule.id)} aria-label="Remove fundamental condition">Remove</button>
                </div>
              );
            })}
          </div>
        )}
        <div className="master-rule-footer">
          <span>{activeRules.length} active condition{activeRules.length === 1 ? '' : 's'}</span>
          {rules.length > 0 && <button type="button" className="master-text-button" onClick={() => setRules([])}>Clear conditions</button>}
        </div>
      </section>

      <section className="master-setups-section">
        <div className="master-section-heading">
          <div><h2>Daily setup scanners</h2><p>Consolidation categories are grouped separately and mutually exclusive; a rally or fall classification replaces a plain tight-consolidation match. Other scanners use up to 120 calendar days of daily data.</p></div>
          <div className="master-setup-toolbar">
            <div className="master-scan-status" aria-live="polite">{setupStatusText}</div>
            <button type="button" className="btn btn-secondary" onClick={() => setSetupRefreshId((current) => current + 1)} disabled={!candidateCount || setupLoading}>Recheck setups</button>
          </div>
        </div>
        <div className="master-setup-groups">
          {MASTER_SETUP_GROUPS.map((group) => (
            <section className="master-setup-group" key={group.id}>
              <h3 className="master-setup-group-title">{group.label}</h3>
              <div className="master-setup-grid">
                {group.setups.map((setup) => (
                  <button key={setup.id} type="button" className={`card master-setup-card${activeSetupFilter === setup.id ? ' is-selected' : ''}`} onClick={() => selectSetup(setup.id)} title={setup.description}>
                    <span className="master-setup-count">{setupReady ? setupCounts.counts[setup.id] : '—'}</span>
                    <strong>{setup.label}</strong>
                    <small>{setup.description}</small>
                  </button>
                ))}
              </div>
            </section>
          ))}
          <section className="master-setup-group">
            <h3 className="master-setup-group-title">Scan coverage</h3>
            <div className="master-setup-grid">
              <button type="button" className={`card master-setup-card master-no-setup-card${activeSetupFilter === 'none' ? ' is-selected' : ''}`} onClick={() => selectSetup('none')}>
                <span className="master-setup-count">{setupReady ? setupCounts.noSetupCount : '—'}</span>
                <strong>No setup detected</strong>
                <small>Stocks with no current setup match.</small>
              </button>
              <div className="card master-setup-card master-no-setup-card" aria-label="Stocks without enough daily history">
                <span className="master-setup-count">{setupReady ? setupCounts.noDataCount : '—'}</span>
                <strong>Insufficient daily data</strong>
                <small>These stocks are separated from the no-match count.</small>
              </div>
            </div>
          </section>
        </div>
      </section>

      <section className="card master-prebreakout-section" aria-labelledby="master-prebreakout-title">
        <div className="master-section-heading">
          <div>
            <h2 id="master-prebreakout-title">Pre-breakout scanner</h2>
            <p>Ranks Screener candidates that are tightening below resistance. Near trigger means 70+ within 3% of the pivot, with the 20-session average above the 50-session average; building means 55+ within 6%, and developing means 40+ within 8%, both above the 50-session average. “Breakout confirmed” means the latest completed daily close is above the selected pivot. The score needs at least 55 daily bars. Tile counts and results both respect the active Master filters.</p>
          </div>
          <div className="master-setup-toolbar">
            <div className="master-scan-status" aria-live="polite">{setupStatusText}</div>
            {hasOtherMasterFilters && <button type="button" className="btn btn-secondary" onClick={clearMasterFilters}>Clear other filters</button>}
          </div>
        </div>
        <div className="master-prebreakout-cards" role="group" aria-label="Filter by pre-breakout stage">
          {([
            ['all', hasOtherMasterFilters ? 'All matching candidates' : 'All candidates', candidatesMatchingMasterFilters.length],
            ['near-trigger', 'Near trigger', preBreakoutCounts['near-trigger']],
            ['building', 'Building', preBreakoutCounts.building],
            ['developing', 'Developing', preBreakoutCounts.developing],
            ['breakout-confirmed', 'Breakout confirmed', preBreakoutCounts['breakout-confirmed']],
            ['insufficient-history', 'Insufficient history', preBreakoutCounts['insufficient-history']],
            ['unmapped', 'No database match', preBreakoutCounts.unmapped],
          ] as Array<[PreBreakoutFilter, string, number]>).map(([filter, label, count]) => (
            <button
              key={filter}
              type="button"
              className={`master-prebreakout-card${preBreakoutFilter === filter ? ' is-selected' : ''}`}
              aria-pressed={preBreakoutFilter === filter}
              onClick={() => selectPreBreakoutFilter(filter)}
              disabled={!candidateCount}
            >
              <span>{setupReady ? count : '—'}</span>
              <strong>{label}</strong>
            </button>
          ))}
        </div>
        <p className="master-prebreakout-method">The pivot is the highest high in the best-fitting prior 8–20 session base, excluding the latest bar. Score weights are trend (20), base tightness (20), distance to pivot (25), volatility contraction (15), volume behaviour (10), and 60-session market-relative strength (10). Trigger is pivot + 0.1 ATR; invalidation is base low − 0.25 ATR; the 2R guide is calculated from that risk. Readiness is a heuristic score, not success odds. Stocks without 55 daily bars or an NSE/BSE database match remain visible in separate coverage groups.</p>
      </section>

      <section className="master-results-section">
        <div className="master-section-heading master-results-heading">
          <div><h2>Master candidates</h2><p>{filteredResults.length} of {candidateCount} candidates · click any header to sort and any stock name for its shared chart and fundamentals view.</p></div>
          <div className="master-results-filter-controls">
            <div className="master-daily-move-filter">
              <label className="form-label"><span>≥5% up days · window</span><select className="input" value={dailyMoveFilter.sessions} onChange={(event) => updateDailyMoveWindow(Number(event.target.value) as DailyMoveWindow)}><option value={20}>20 sessions</option><option value={40}>40 sessions</option><option value={60}>60 sessions</option></select></label>
              <label className="form-label"><span>Minimum count</span><input className="input" type="number" min={0} max={dailyMoveFilter.sessions} step={1} value={dailyMoveFilter.minimumCount ?? ''} placeholder="Off" onChange={(event) => setDailyMoveFilter((current) => {
                const raw = event.target.value.trim();
                if (raw === '') return { ...current, minimumCount: null };
                const value = Number(raw);
                return { ...current, minimumCount: Number.isFinite(value) ? Math.max(0, Math.min(current.sessions, Math.floor(value))) : null };
              })} /></label>
              <small>Count single-session close-to-close gains of 5% or more.</small>
            </div>
            <label className="form-label master-setup-filter">Pre-breakout stage<select className="input" value={preBreakoutFilter} onChange={(event) => setPreBreakoutFilter(event.target.value as PreBreakoutFilter)}>
              <option value="all">All candidates</option>
              <option value="near-trigger">Near trigger</option>
              <option value="building">Building</option>
              <option value="developing">Developing</option>
              <option value="breakout-confirmed">Breakout confirmed</option>
              <option value="not-ready">Not ready</option>
              <option value="insufficient-history">Insufficient history</option>
              <option value="unmapped">No database match</option>
            </select></label>
            <label className="form-label master-setup-filter">Setup filter<select className="input" value={activeSetupFilter} onChange={(event) => setSetupFilter(event.target.value as SetupFilter)}><option value="all">All candidates</option><option value="any">Any detected setup</option><option value="none">No setup detected</option>{MASTER_SETUP_DEFINITIONS.map((setup) => <option key={setup.id} value={setup.id}>{setup.label}</option>)}</select></label>
          </div>
        </div>
        {candidateMetricsLoading && <div className="master-data-note">Loading daily returns, volume profiles, and momentum scores…</div>}
        {candidateMetricsError && <div className="master-data-note">Daily momentum metrics: {candidateMetricsError}</div>}
        {fundamentalsError && <div className="master-data-note">Fundamental data could not be loaded: {fundamentalsError}</div>}
        {fundamentalsLoading && <div className="master-data-note">Loading fundamental score, net profit growth, and ROE for {candidateCount} candidates…</div>}
        {!activeScan && !scanning ? (
          <div className="card master-empty-results"><strong>{source === 'screener-query' ? 'No Screener query candidates loaded yet.' : 'No enhanced candidates loaded yet.'}</strong><span>{source === 'screener-query' ? 'Enter the Screener query and refresh candidates. Master uses the same saved momentum filters and daily setup scanners.' : 'Update candidates here, or run Enhanced Momentum first. Master uses the same saved momentum filters.'}</span></div>
        ) : (
          <div className="card momentum-table-wrap master-table-wrap">
            <table className="data-table momentum-table master-table">
              <thead><tr>
                <SortHeader label="#" sortKey="rank" activeKey={sort.key} direction={sort.direction} onSort={sortResults} />
                <SortHeader label="Stock" sortKey="stock" activeKey={sort.key} direction={sort.direction} onSort={sortResults} />
                <SortHeader label="Sector" sortKey="sector" activeKey={sort.key} direction={sort.direction} onSort={sortResults} />
                <SortHeader label="Price" sortKey="price" activeKey={sort.key} direction={sort.direction} onSort={sortResults} />
                <SortHeader label="Momentum" sortKey="momentum" activeKey={sort.key} direction={sort.direction} onSort={sortResults} />
                <SortHeader label="Pre-breakout rank / score" sortKey="readiness" activeKey={sort.key} direction={sort.direction} onSort={sortResults} />
                <SortHeader label="Breakout stage" sortKey="stage" activeKey={sort.key} direction={sort.direction} onSort={sortResults} />
                <SortHeader label="Pivot gap" sortKey="pivotGap" activeKey={sort.key} direction={sort.direction} onSort={sortResults} />
                <SortHeader label="Trigger / stop / 2R guide" sortKey="riskTarget" activeKey={sort.key} direction={sort.direction} onSort={sortResults} />
                <SortHeader label="Fundamental score" sortKey="fundamental" activeKey={sort.key} direction={sort.direction} onSort={sortResults} />
                <SortHeader label="≥5% up days · 20S" sortKey="upMove20" activeKey={sort.key} direction={sort.direction} onSort={sortResults} />
                <SortHeader label="≥5% up days · 40S" sortKey="upMove40" activeKey={sort.key} direction={sort.direction} onSort={sortResults} />
                <SortHeader label="≥5% up days · 60S" sortKey="upMove60" activeKey={sort.key} direction={sort.direction} onSort={sortResults} />
                <SortHeader label="20D return" sortKey="return20" activeKey={sort.key} direction={sort.direction} onSort={sortResults} />
                <SortHeader label="60D return" sortKey="return60" activeKey={sort.key} direction={sort.direction} onSort={sortResults} />
                <SortHeader label="120D return" sortKey="return120" activeKey={sort.key} direction={sort.direction} onSort={sortResults} />
                <SortHeader label="252D return" sortKey="return252" activeKey={sort.key} direction={sort.direction} onSort={sortResults} />
                <SortHeader label="Risk-adjusted" sortKey="riskAdjusted" activeKey={sort.key} direction={sort.direction} onSort={sortResults} />
                <SortHeader label="ATR 20/60/120/252" sortKey="atr" activeKey={sort.key} direction={sort.direction} onSort={sortResults} />
                <SortHeader label="Volume 20/60/120/252" sortKey="volume" activeKey={sort.key} direction={sort.direction} onSort={sortResults} />
                <SortHeader label="Breakout / ATH" sortKey="breakout" activeKey={sort.key} direction={sort.direction} onSort={sortResults} />
                <SortHeader label="Net profit growth · 1Y" sortKey="profitGrowth" activeKey={sort.key} direction={sort.direction} onSort={sortResults} />
                <SortHeader label="ROE" sortKey="roe" activeKey={sort.key} direction={sort.direction} onSort={sortResults} />
                <SortHeader label="Market cap" sortKey="marketCap" activeKey={sort.key} direction={sort.direction} onSort={sortResults} />
              </tr></thead>
              <tbody>
                {filteredResults.map((stock) => {
                  const fundamental = fundamentals[stock.stock_id];
                  const preBreakout = preBreakoutSignals[stock.stock_id];
                  const breakoutRank = preBreakoutRanks[stock.stock_id];
                  return (
                    <tr key={stock.stock_id}>
                      <td><strong>{stock.rank}</strong></td>
                      <td><StockIdentity className="momentum-stock-open" {...asStockModalData(stock, fundamental?.marketCapCr)} /></td>
                      <td>{stock.sector || '—'}</td>
                      <td>{stock.close > 0 ? `₹${formatNumber(stock.close, 2)}` : '—'}</td>
                      <td><span className={`momentum-score ${stock.enhanced_score >= 70 ? 'is-strong' : ''}`}>{formatNumber(stock.enhanced_score, 1)}</span></td>
                      <td title={preBreakoutTooltip(preBreakout)}>
                        <span className={`momentum-score${preBreakout?.score != null && preBreakout.score >= 70 ? ' is-strong' : ''}`} data-stage={preBreakout?.status}>
                          {preBreakout?.score == null ? '—' : `${breakoutRank ? `#${breakoutRank} · ` : ''}${preBreakout.score}`}
                        </span>
                        <small>{preBreakout ? preBreakoutStatusLabel(preBreakout.status) : setupLoading ? 'Scanning daily bars' : 'Waiting for daily scan'}</small>
                      </td>
                      <td className="master-prebreakout-stage" data-stage={preBreakout?.status} title={preBreakoutTooltip(preBreakout)}>{preBreakout ? preBreakoutStatusLabel(preBreakout.status) : '—'}</td>
                      <td title={preBreakout?.pivotPrice == null ? undefined : `Pivot ₹${formatNumber(preBreakout.pivotPrice, 2)} · base ${preBreakout.baseSessions} sessions at ${formatNumber(preBreakout.baseRangePct, 1)}% width`}>
                        {formatPivotGap(preBreakout?.distanceToPivotPct)}
                      </td>
                      <td title={preBreakoutTooltip(preBreakout)}>
                        {preBreakout?.triggerPrice == null ? '—' : `₹${formatNumber(preBreakout.triggerPrice, 2)}`}
                        <small>Stop ₹{formatNumber(preBreakout?.invalidationPrice, 2)} · 2R ₹{formatNumber(preBreakout?.twoRTargetPrice, 2)}</small>
                      </td>
                      <td title={fundamental?.assessment ? `${fundamental.assessment.rating} · ${fundamental.assessment.coverage}% pillar coverage · ${fundamental.assessment.signalCoverage}% input coverage` : fundamentalsError}><span className={`momentum-score ${fundamental?.assessment.score != null && fundamental.assessment.score >= 70 ? 'is-strong' : ''}`}>{fundamentalsLoading && !fundamentalsReady ? '…' : formatNumber(fundamental?.assessment.score, 0)}</span></td>
                      <td>{dailyMoveCounts[stock.stock_id]?.sessions20 ?? '—'}</td>
                      <td>{dailyMoveCounts[stock.stock_id]?.sessions40 ?? '—'}</td>
                      <td>{dailyMoveCounts[stock.stock_id]?.sessions60 ?? '—'}</td>
                      <td className={stock.return_20d == null ? '' : stock.return_20d >= 0 ? 'momentum-positive' : 'momentum-negative'}>{formatPercent(stock.return_20d)}</td>
                      <td className={stock.return_60d == null ? '' : stock.return_60d >= 0 ? 'momentum-positive' : 'momentum-negative'}>{formatPercent(stock.return_60d)}</td>
                      <td className={stock.return_120d == null ? '' : stock.return_120d >= 0 ? 'momentum-positive' : 'momentum-negative'}>{formatPercent(stock.return_120d)}</td>
                      <td className={stock.return_252d == null ? '' : stock.return_252d >= 0 ? 'momentum-positive' : 'momentum-negative'}>{formatPercent(stock.return_252d)}</td>
                      <td>{formatNumber(stock.risk_adjusted_momentum_score)}</td>
                      <td><span title={`ATR 20D ${formatNumber(stock.atr_pct_20d, 2)}% · 60D ${formatNumber(stock.atr_pct_60d, 2)}% · 120D ${formatNumber(stock.atr_pct_120d, 2)}% · 252D ${formatNumber(stock.atr_pct_252d, 2)}%`}>{formatNumber(stock.atr_pct_20d, 2)}%</span><small>60D {formatNumber(stock.atr_pct_60d, 2)}% · 120D {formatNumber(stock.atr_pct_120d, 2)}% · 252D {formatNumber(stock.atr_pct_252d, 2)}%</small></td>
                      <td><span title={`Volume ratio 20D ${formatNumber(stock.volume_ratio_20d, 2)}× · 60D ${formatNumber(stock.volume_ratio_60d, 2)}× · 120D ${formatNumber(stock.volume_ratio_120d, 2)}× · 252D ${formatNumber(stock.volume_ratio_252d, 2)}×`}>{formatNumber(stock.volume_ratio_20d, 2)}×</span><small>60D {formatNumber(stock.volume_ratio_60d, 2)}× · 120D {formatNumber(stock.volume_ratio_120d, 2)}× · 252D {formatNumber(stock.volume_ratio_252d, 2)}×</small></td>
                      <td>{stock.at_all_time_high ? 'ATH' : stock.breakout_55 ? '55D breakout' : stock.breakout_20 ? '20D breakout' : '—'}</td>
                      <td className={fundamental?.netProfitGrowth1yPct == null ? '' : fundamental.netProfitGrowth1yPct >= 0 ? 'momentum-positive' : 'momentum-negative'}>{fundamentalsLoading && !fundamentalsReady ? '…' : formatPercent(fundamental?.netProfitGrowth1yPct)}</td>
                      <td>{fundamentalsLoading && !fundamentalsReady ? '…' : formatPercent(fundamental?.roePct)}</td>
                      <td>{formatMarketCap(fundamental?.marketCapCr ?? stock.market_cap_cr)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {candidateResults.length === 0 && <div className="empty-state"><div className="empty-state-text">{source === 'screener-query' ? 'No equity candidates were found in the imported Screener query results.' : 'No enhanced equity candidates matched the saved momentum rules.'}</div></div>}
            {candidateResults.length > 0 && filteredResults.length === 0 && <div className="empty-state"><div className="empty-state-text">No candidates match these Master conditions.</div><div className="empty-state-sub">Clear or loosen the fundamental conditions, or change the setup filter.</div></div>}
          </div>
        )}
      </section>
    </div>
  );
}
