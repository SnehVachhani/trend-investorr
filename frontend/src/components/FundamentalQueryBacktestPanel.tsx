'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import StockIdentity from './StockIdentity';
import { usePersistentState } from '@/lib/persistence';
import {
  createFundamentalQuery,
  deleteFundamentalQuery,
  getFundamentalQueryBacktest,
  listFundamentalSetupPresets,
  listFundamentalQueries,
  previewFundamentalQuery,
  startFundamentalQueryBacktest,
  updateFundamentalQuery,
  type FundamentalMetric,
  type FundamentalOperator,
  type FundamentalQuery,
  type FundamentalQueryBacktestJob,
  type FundamentalQueryPreview,
  type FundamentalRule,
  type FundamentalSetupPreset,
} from '@/lib/fundamental-query-api';

type DraftRule = Omit<FundamentalRule, 'value'> & { value: string };

const METRICS: Array<{ key: FundamentalMetric; label: string; unit: '%' | '₹ Cr'; description: string }> = [
  { key: 'profit_cagr_5y_pct', label: 'Net profit CAGR · 5 years', unit: '%', description: 'Needs six comparable annual profit periods. Current stored coverage is usually too short.' },
  { key: 'profit_growth_1y_pct', label: 'Net profit growth · 1 year', unit: '%', description: 'Latest annual profit compared with the previous annual period.' },
  { key: 'quarter_profit_yoy_pct', label: 'Quarterly net profit · year on year', unit: '%', description: 'Needs the same quarter one year earlier. Current stored coverage is usually too short.' },
  { key: 'quarter_profit_qoq_pct', label: 'Quarterly net profit · quarter on quarter', unit: '%', description: 'Latest quarter compared with the previous quarter.' },
  { key: 'roe_pct', label: 'Return on equity', unit: '%', description: 'Reported profitability relative to shareholder equity.' },
  { key: 'roce_pct', label: 'Return on capital employed', unit: '%', description: 'Reported profitability relative to capital employed.' },
  { key: 'market_cap_cr', label: 'Estimated market capitalization', unit: '₹ Cr', description: 'Derived from reported P/E × latest annual profit when available; it is an estimate.' },
  { key: 'latest_annual_profit_cr', label: 'Latest annual net profit', unit: '₹ Cr', description: 'Requires positive reported annual earnings.' },
  { key: 'latest_quarter_profit_cr', label: 'Latest quarterly net profit', unit: '₹ Cr', description: 'Requires positive reported quarterly earnings.' },
];

const ALL_SETUP_PRESETS = '__all__';
const PREVIEW_PAGE_SIZE = 25;
const DEFAULT_CAPITAL_PER_STOCK = 100_000;
const WATCHLIST_FILTER_SETUP_IDS = new Set(['trend_continuation', 'liquid_swing_momentum']);

const DEFAULT_RULES: FundamentalRule[] = [
  { metric: 'profit_growth_1y_pct', operator: '>=', value: 25 },
  { metric: 'roe_pct', operator: '>=', value: 12 },
  { metric: 'market_cap_cr', operator: '>=', value: 500 },
];

const NO_STOCKS = new Set(['no_selected_stocks', 'no_eligible_stocks']);
const TERMINAL = new Set(['completed', 'partial', 'failed', 'cancelled', ...NO_STOCKS]);

function dateInputValue(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function draftFromRules(rules: FundamentalRule[]): DraftRule[] {
  return rules.map((rule) => ({ ...rule, value: String(rule.value) }));
}

function validatedRules(drafts: DraftRule[]): FundamentalRule[] {
  if (drafts.length === 0) throw new Error('Add at least one fundamental condition.');
  return drafts.map((draft) => {
    if (draft.value.trim() === '') throw new Error('Each condition needs a numeric value.');
    const value = Number(draft.value);
    if (!Number.isFinite(value)) throw new Error('Each condition needs a valid numeric value.');
    return { metric: draft.metric, operator: draft.operator, value };
  });
}

function sameRules(left: FundamentalRule[], right: FundamentalRule[]): boolean {
  return left.length === right.length && left.every((rule, index) => {
    const other = right[index];
    return rule.metric === other.metric && rule.operator === other.operator && rule.value === other.value;
  });
}

function metricInfo(metric: FundamentalMetric) {
  return METRICS.find((item) => item.key === metric) ?? METRICS[0];
}

function displayValue(value: number | null | undefined, unit: '%' | '₹ Cr') {
  if (value == null || !Number.isFinite(value)) return '—';
  const number = value.toLocaleString('en-IN', { maximumFractionDigits: 2 });
  return unit === '%' ? `${number}%` : `₹${number} Cr`;
}

function displayRupees(value: number | null | undefined) {
  return value == null || !Number.isFinite(value)
    ? '—'
    : `₹${value.toLocaleString('en-IN', { maximumFractionDigits: 0 })}`;
}

function displayDate(value: string | null | undefined) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' });
}

function displayTradeTimestamp(value: string | null | undefined, includeTime: boolean) {
  if (!includeTime) return displayDate(value);
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleString('en-IN', {
      day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit',
      timeZone: 'Asia/Kolkata',
    });
}

function displayPrice(value: number | null | undefined) {
  return value == null || !Number.isFinite(value)
    ? '—'
    : `₹${value.toLocaleString('en-IN', { maximumFractionDigits: 2 })}`;
}

function ruleText(rule: DraftRule): string {
  const metric = metricInfo(rule.metric);
  const comparator: Record<FundamentalOperator, string> = {
    '>': 'more than', '>=': 'at least', '<': 'less than', '<=': 'at most',
  };
  const number = Number(rule.value);
  const value = rule.value.trim() === '' || !Number.isFinite(number)
    ? '[enter a value]'
    : displayValue(number, metric.unit);
  return `${metric.label} ${comparator[rule.operator]} ${value}`;
}

function queryText(rules: DraftRule[]): string {
  if (rules.length === 0) return 'Add a condition to define the qualifying stocks.';
  return `Include a stock only when ${rules.map(ruleText).join('; and ')}. Missing data does not pass a condition.`;
}

function percentageClass(value: number | null | undefined) {
  return value == null ? '' : value >= 0 ? 'wl-analysis-positive' : 'wl-analysis-negative';
}

export default function FundamentalQueryBacktestPanel() {
  const defaultEnd = useMemo(() => dateInputValue(new Date()), []);
  const defaultStart = useMemo(() => {
    const date = new Date();
    date.setFullYear(date.getFullYear() - 2);
    return dateInputValue(date);
  }, []);
  const [queries, setQueries] = useState<FundamentalQuery[]>([]);
  const [queriesLoading, setQueriesLoading] = useState(true);
  const [setups, setSetups] = useState<FundamentalSetupPreset[]>([]);
  const [setupsLoading, setSetupsLoading] = useState(true);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [name, setName] = useState('Growing, profitable companies · Core 3');
  const [rules, setRules] = useState<DraftRule[]>(() => draftFromRules(DEFAULT_RULES));
  const [previewDate, setPreviewDate] = useState('');
  const [preview, setPreview] = useState<FundamentalQueryPreview | null>(null);
  const [previewKey, setPreviewKey] = useState('');
  const [previewPage, setPreviewPage] = useState(0);
  const [missingPage, setMissingPage] = useState(0);
  const [startDate, setStartDate] = usePersistentState('workspace.fundamentalBacktest.startDate', defaultStart);
  const [endDate, setEndDate] = usePersistentState('workspace.fundamentalBacktest.endDate', defaultEnd);
  const [targetR, setTargetR] = usePersistentState('workspace.fundamentalBacktest.targetR', '2');
  const [maxHoldBars, setMaxHoldBars] = usePersistentState('workspace.fundamentalBacktest.maxHoldFiveMinuteBars', '1500');
  const [stopBufferPct, setStopBufferPct] = usePersistentState('workspace.fundamentalBacktest.stopBufferPct', '0.5');
  const [selectedSetupId, setSelectedSetupId] = usePersistentState('workspace.fundamentalBacktest.selectedSetup', ALL_SETUP_PRESETS);
  const [jobId, setJobId] = usePersistentState<string | null>('workspace.fundamentalBacktest.jobId', null);
  const [job, setJob] = useState<FundamentalQueryBacktestJob | null>(null);
  const [selectedTradeSetupId, setSelectedTradeSetupId] = useState<string | null>(null);
  const [busy, setBusy] = useState<'save' | 'preview' | 'run' | 'delete' | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [jobReadError, setJobReadError] = useState('');
  const queryDraftTouched = useRef(false);

  const selectedQuery = queries.find((query) => query.id === selectedId) ?? null;
  const selectedSetup = setups.find((setup) => setup.id === selectedSetupId) ?? null;
  const setupIds = selectedSetupId === ALL_SETUP_PRESETS
    ? setups.map((setup) => setup.id)
    : setups.some((setup) => setup.id === selectedSetupId) ? [selectedSetupId] : [];
  const usesWatchlistFilterSetups = setupIds.some((setupId) => WATCHLIST_FILTER_SETUP_IDS.has(setupId));
  const usesStandaloneSetupRiskControls = setupIds.some((setupId) => !WATCHLIST_FILTER_SETUP_IDS.has(setupId));
  const previewPageCount = preview ? Math.max(1, Math.ceil(preview.stocks.length / PREVIEW_PAGE_SIZE)) : 1;
  const currentPreviewPage = Math.min(previewPage, previewPageCount - 1);
  const previewPageStocks = preview?.stocks.slice(currentPreviewPage * PREVIEW_PAGE_SIZE, (currentPreviewPage + 1) * PREVIEW_PAGE_SIZE) ?? [];
  const missingStocks = preview?.missing_stocks ?? [];
  const missingPageCount = Math.max(1, Math.ceil(missingStocks.length / PREVIEW_PAGE_SIZE));
  const currentMissingPage = Math.min(missingPage, missingPageCount - 1);
  const missingPageStocks = missingStocks.slice(currentMissingPage * PREVIEW_PAGE_SIZE, (currentMissingPage + 1) * PREVIEW_PAGE_SIZE);
  const currentRules = useMemo(() => {
    try { return validatedRules(rules); } catch { return null; }
  }, [rules]);
  const currentKey = JSON.stringify({ rules: currentRules, as_of: previewDate || null });
  const previewIsCurrent = Boolean(preview && previewKey === currentKey);
  const isSavedVersion = Boolean(
    selectedQuery && name.trim() === selectedQuery.name && currentRules
      && sameRules(currentRules, selectedQuery.rules),
  );
  const shownQueryText = previewIsCurrent && preview?.query_text
    ? preview.query_text
    : isSavedVersion && selectedQuery?.query_text ? selectedQuery.query_text : queryText(rules);
  const previewMetrics = Array.from(new Set(rules.map((rule) => rule.metric)));
  const historyHeavyMetrics = previewMetrics.filter((metric) => metric === 'profit_cagr_5y_pct' || metric === 'quarter_profit_yoy_pct');

  useEffect(() => {
    let active = true;
    listFundamentalQueries()
      .then((items) => {
        if (!active) return;
        setQueries(items);
        if (!queryDraftTouched.current) {
          const matchingDefault = items.find((query) => query.name === 'Growing, profitable companies · Core 3' && sameRules(query.rules, DEFAULT_RULES));
          if (matchingDefault) {
            setSelectedId(matchingDefault.id);
            setName(matchingDefault.name);
            setRules(draftFromRules(matchingDefault.rules));
          }
        }
      })
      .catch((requestError) => { if (active) setError(requestError instanceof Error ? requestError.message : 'Unable to load saved queries.'); })
      .finally(() => { if (active) setQueriesLoading(false); });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    let active = true;
    listFundamentalSetupPresets()
      .then((items) => {
        if (!active) return;
        setSetups(items);
        setSelectedSetupId((current) => current === ALL_SETUP_PRESETS || items.some((setup) => setup.id === current) ? current : ALL_SETUP_PRESETS);
      })
      .catch((requestError) => { if (active) setError(requestError instanceof Error ? requestError.message : 'Unable to load technical setup presets.'); })
      .finally(() => { if (active) setSetupsLoading(false); });
    return () => { active = false; };
  }, [setSelectedSetupId]);

  useEffect(() => {
    if (!jobId) return;
    let active = true;
    let timer: ReturnType<typeof setInterval> | null = null;
    const refresh = async () => {
      try {
        const result = await getFundamentalQueryBacktest(jobId);
        if (!active) return;
        setJob(result);
        setJobReadError('');
        if (TERMINAL.has(result.status) && timer) {
          clearInterval(timer);
          timer = null;
        }
      } catch (requestError) {
        if (active) setJobReadError(requestError instanceof Error ? requestError.message : 'Unable to read backtest progress.');
      }
    };
    void refresh();
    timer = setInterval(() => void refresh(), 2500);
    return () => { active = false; if (timer) clearInterval(timer); };
  }, [jobId]);

  const updateRule = (index: number, patch: Partial<DraftRule>) => {
    setRules((current) => current.map((rule, ruleIndex) => ruleIndex === index ? { ...rule, ...patch } : rule));
  };

  const chooseQuery = (query: FundamentalQuery) => {
    queryDraftTouched.current = true;
    setSelectedId(query.id);
    setName(query.name);
    setRules(draftFromRules(query.rules));
    setPreview(null);
    setPreviewKey('');
    setError('');
    setNotice(`Loaded ${query.name} · version ${query.version}.`);
  };

  const newQuery = () => {
    queryDraftTouched.current = true;
    setSelectedId(null);
    setName('Growing, profitable companies · Core 3');
    setRules(draftFromRules(DEFAULT_RULES));
    setPreview(null);
    setPreviewKey('');
    setError('');
    setNotice('Started a new query from the default quality conditions.');
  };

  const saveQuery = async (values: FundamentalRule[]): Promise<FundamentalQuery> => {
    const cleanName = name.trim();
    if (!cleanName) throw new Error('Give this query a name before saving it.');
    const saved = selectedId == null
      ? await createFundamentalQuery(cleanName, values)
      : await updateFundamentalQuery(selectedId, cleanName, values);
    setQueries((current) => [saved, ...current.filter((item) => item.id !== saved.id)]);
    queryDraftTouched.current = true;
    setSelectedId(saved.id);
    setName(saved.name);
    setRules(draftFromRules(saved.rules));
    return saved;
  };

  const handleSave = async () => {
    setError('');
    setNotice('');
    if (isSavedVersion && selectedQuery) {
      setNotice(`${selectedQuery.name} · version ${selectedQuery.version} is already saved.`);
      return;
    }
    setBusy('save');
    try {
      const saved = await saveQuery(validatedRules(rules));
      setNotice(`Saved ${saved.name} · version ${saved.version}.`);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Unable to save query.');
    } finally {
      setBusy(null);
    }
  };

  const handleDelete = async () => {
    if (!selectedId || !selectedQuery) return;
    if (!window.confirm(`Delete “${selectedQuery.name}”? Existing backtest results remain separate.`)) return;
    setBusy('delete');
    setError('');
    try {
      await deleteFundamentalQuery(selectedId);
      setQueries((current) => current.filter((query) => query.id !== selectedId));
      newQuery();
      setNotice('Query deleted.');
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Unable to delete query.');
    } finally {
      setBusy(null);
    }
  };

  const handlePreview = async () => {
    setBusy('preview');
    setError('');
    setNotice('');
    try {
      const values = validatedRules(rules);
      const key = JSON.stringify({ rules: values, as_of: previewDate || null });
      const result = await previewFundamentalQuery(values, previewDate || undefined);
      setPreview(result);
      setPreviewKey(key);
      setPreviewPage(0);
      setMissingPage(0);
      setNotice(`Selected ${result.coverage.eligible} stocks as of ${displayDate(result.as_of)}.`);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Unable to preview query.');
    } finally {
      setBusy(null);
    }
  };

  const handleRun = async () => {
    setError('');
    setNotice('');
    if (!startDate || !endDate || startDate > endDate) {
      setError('Choose a valid backtest date range.');
      return;
    }
    if (setupsLoading || setupIds.length === 0) {
      setError('Technical setup presets are still loading. Try again in a moment.');
      return;
    }
    const target = Number(targetR);
    const hold = Number(maxHoldBars);
    const stopBuffer = Number(stopBufferPct);
    if (usesStandaloneSetupRiskControls && (targetR.trim() === '' || !Number.isFinite(target) || target < 0.5 || target > 10)) {
      setError('Target must be between 0.5 and 10 times initial entry risk.');
      return;
    }
    if (usesStandaloneSetupRiskControls && (maxHoldBars.trim() === '' || !Number.isInteger(hold) || hold < 1 || hold > 7500)) {
      setError('Maximum hold must be a whole number from 1 to 7,500 five-minute candles.');
      return;
    }
    if (usesStandaloneSetupRiskControls && (stopBufferPct.trim() === '' || !Number.isFinite(stopBuffer) || stopBuffer < 0 || stopBuffer > 10)) {
      setError('Stop buffer must be between 0% and 10%.');
      return;
    }
    setBusy('run');
    try {
      const values = validatedRules(rules);
      const query = isSavedVersion && selectedQuery ? selectedQuery : await saveQuery(values);
      const universe = previewIsCurrent && preview
        ? preview
        : await previewFundamentalQuery(values, previewDate || undefined);
      if (!(previewIsCurrent && preview)) {
        setPreview(universe);
        setPreviewKey(JSON.stringify({ rules: values, as_of: previewDate || null }));
        setPreviewPage(0);
        setMissingPage(0);
      }
      if (universe.stocks.length === 0) {
        throw new Error(`The query selected no stocks as of ${displayDate(universe.as_of)}. Adjust the query or selection date before running technical setups.`);
      }
      const started = await startFundamentalQueryBacktest({
        query_id: query.id,
        start_date: startDate,
        end_date: endDate,
        selected_stock_ids: universe.stocks.map((stock) => stock.stock_id),
        universe_as_of: universe.as_of,
        setup_ids: setupIds,
        ...(usesStandaloneSetupRiskControls ? {
          target_r: target,
          max_hold_bars: hold,
          stop_buffer_pct: stopBuffer,
        } : {}),
      });
      setJob(null);
      setJobReadError('');
      setJobId(started.job_id);
      setNotice(`Backtest started for ${query.name} · version ${query.version} · ${universe.stocks.length} selected stocks.`);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Unable to start backtest.');
    } finally {
      setBusy(null);
    }
  };

  const progress = job?.progress?.total ? Math.round((job.progress.completed / job.progress.total) * 100) : 0;
  const qualificationPoints = job?.qualification_timeline ?? [];
  const noStocks = Boolean(job && NO_STOCKS.has(job.status));
  const selectedTradeSetup = job?.leaderboard?.find((row) => row.setup_id === selectedTradeSetupId) ?? null;
  const selectedSetupTrades = selectedTradeSetupId
    ? (job?.trades ?? []).filter((trade) => trade.setup_id === selectedTradeSetupId)
    : [];

  useEffect(() => {
    if (!selectedTradeSetupId) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setSelectedTradeSetupId(null);
    };
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [selectedTradeSetupId]);

  return (
    <div className="fq-panel">
      <header className="fq-hero">
        <span className="fq-kicker">QUERY-SELECTED STOCKS · TECHNICAL SETUPS</span>
        <h1 className="bt-section-title">Technical setup backtest</h1>
        <p>Run the selected setups on the stocks returned by your query. Standalone patterns use five-minute candles for signal detection and trade simulation. The Watchlist Analysis presets also execute on five-minute candles and retain their daily context filters.</p>
      </header>

      {error && <div className="bt-error" role="alert">{error}</div>}
      {notice && <div className="fq-notice" role="status">{notice}</div>}

      <div className="fq-builder-grid">
        <section className="card fq-builder">
          <div className="fq-section-heading">
            <div><span className="fq-step">01 · Stock selection</span><h2>Build the stock query</h2></div>
            <button type="button" className="btn btn-secondary" onClick={newQuery} disabled={busy != null}>New query</button>
          </div>
          <p className="fq-muted">This query selects the stocks once at the preview date. Setups use historical price candles at their configured timeframe and do not recheck financial data at historical dates.</p>
          <label className="bt-field-label" htmlFor="fq-name">Query name</label>
          <input id="fq-name" className="bt-field-input fq-name-input" maxLength={100} value={name} onChange={(event) => { queryDraftTouched.current = true; setName(event.target.value); }} placeholder="Name this stock query" />
          <div className="fq-rule-heading"><strong>All conditions must pass</strong><small>Stocks with missing values do not qualify.</small></div>
          <div className="fq-rules">
            {rules.map((rule, index) => (
              <div className="fq-rule" key={`${index}-${rule.metric}`}>
                <label className="fq-rule-metric"><span>Metric</span><select value={rule.metric} onChange={(event) => { queryDraftTouched.current = true; updateRule(index, { metric: event.target.value as FundamentalMetric }); }} title={metricInfo(rule.metric).description}>{METRICS.map((metric) => <option key={metric.key} value={metric.key}>{metric.label}{metric.key === 'profit_cagr_5y_pct' || metric.key === 'quarter_profit_yoy_pct' ? ' · needs more history' : ''}</option>)}</select></label>
                <label className="fq-rule-operator"><span>Compare</span><select value={rule.operator} onChange={(event) => { queryDraftTouched.current = true; updateRule(index, { operator: event.target.value as FundamentalOperator }); }}><option value=">=">≥</option><option value=">">&gt;</option><option value="<=">≤</option><option value="<">&lt;</option></select></label>
                <label className="fq-rule-value"><span>{metricInfo(rule.metric).unit}</span><input type="number" step="any" value={rule.value} onChange={(event) => { queryDraftTouched.current = true; updateRule(index, { value: event.target.value }); }} /></label>
                <button type="button" className="fq-remove-rule" onClick={() => { queryDraftTouched.current = true; setRules((current) => current.filter((_, ruleIndex) => ruleIndex !== index)); }} aria-label={`Remove ${metricInfo(rule.metric).label} condition`} title="Remove condition">×</button>
              </div>
            ))}
          </div>
          <button type="button" className="btn btn-secondary fq-add-rule" onClick={() => { queryDraftTouched.current = true; setRules((current) => [...current, { metric: 'profit_growth_1y_pct', operator: '>=', value: '10' }]); }}>+ Add condition</button>
          <p className="fq-history-note">The starter screen uses three core checks: annual profit growth, ROE, and estimated market capitalization. Growth needs positive profit in both matching periods; standalone history is used only when consolidated history cannot calculate that metric. Market capitalization remains an estimate from P/E × latest annual profit.</p>
          <div className="fq-query-text" aria-live="polite"><span>Exact query</span><p>{shownQueryText}</p>{previewIsCurrent && <small>Validated against the backend preview.</small>}</div>
          <div className="fq-builder-actions"><button type="button" className="btn btn-primary" onClick={handleSave} disabled={busy != null || !currentRules || !name.trim()}>{busy === 'save' ? 'Saving…' : selectedId ? 'Save new version' : 'Save query'}</button>{selectedId && <button type="button" className="btn btn-secondary" onClick={handleDelete} disabled={busy != null}>{busy === 'delete' ? 'Deleting…' : 'Delete'}</button>}</div>
        </section>

        <aside className="fq-aside">
          <section className="card fq-saved">
            <div className="fq-section-heading"><div><span className="fq-step">LIBRARY</span><h2>Saved queries</h2></div><span className="fq-count">{queries.length}</span></div>
            {queriesLoading ? <p className="fq-muted">Loading queries…</p> : queries.length === 0 ? <p className="fq-muted">No saved queries yet. Start with the quality conditions on the left.</p> : <div className="fq-saved-list">{queries.map((query) => <button key={query.id} type="button" className={query.id === selectedId ? 'is-selected' : ''} onClick={() => chooseQuery(query)}><strong>{query.name}</strong><small>Version {query.version} · {query.rules.length} conditions · updated {displayDate(query.updated_at)}</small></button>)}</div>}
          </section>
        </aside>
      </div>

      <section className="card fq-membership-section">
        <div className="fq-section-heading"><div><span className="fq-step">02 · QUERY RESULTS</span><h2>Preview and select stocks</h2></div></div>
        <p className="fq-muted">The preview determines the stock list for the run. All selected setups use this same list throughout the historical price range.</p>
        <div className="fq-membership-toolbar">
          <label className="fq-membership-field"><span>Select stocks using data as of</span><input id="fq-preview-date" type="date" value={previewDate} onChange={(event) => setPreviewDate(event.target.value)} /></label>
          <button type="button" className="btn btn-secondary" onClick={handlePreview} disabled={busy != null || !currentRules}>{busy === 'preview' ? 'Checking…' : 'Preview stock list'}</button>
        </div>

        {preview && previewIsCurrent && <div className="fq-preview-results">
          <div className="fq-section-heading"><div><span className="fq-step">STOCK SELECTION PREVIEW</span><h3>Stock list as of {displayDate(preview.as_of)}</h3></div><span className="fq-count">{preview.coverage.eligible} / {preview.coverage.total}</span></div>
          <div className="fq-stats"><div><span>Selected</span><strong>{preview.coverage.eligible}</strong></div><div><span>Missing query data</span><strong>{preview.coverage.insufficient}</strong></div><div><span>Do not meet query</span><strong>{preview.coverage.failed}</strong></div></div>
          {preview.coverage.insufficient > 0 && <p className="fq-gap-summary">Of {preview.coverage.insufficient} incomplete stocks, {preview.coverage.incomplete_but_ruled_out ?? 0} already fail at least one available condition; {preview.coverage.unresolved ?? preview.coverage.insufficient} cannot yet be judged against all conditions.</p>}
          {Object.keys(preview.coverage.missing_by_metric_reason ?? {}).length > 0 && <div className="fq-missing-breakdown"><strong>Why each input is unavailable</strong><div className="fq-gap-category-list">{Object.entries(preview.coverage.missing_by_metric_reason ?? {}).flatMap(([metric, reasons]) => Object.entries(reasons).map(([reason, count]) => <div className="fq-gap-category" key={`${metric}-${reason}`}><span>{metricInfo(metric as FundamentalMetric).label}</span><span>{preview.coverage.missing_reason_labels?.[reason] || reason.replaceAll('_', ' ')}</span><strong>{count}</strong></div>))}</div><small>Counts overlap when a stock is missing more than one condition. Standalone history is used only when a complete consolidated calculation is unavailable.</small></div>}
          {historyHeavyMetrics.length > 0 && preview.coverage.insufficient > 0 && <p className="fq-history-note">{historyHeavyMetrics.map((metric) => `${metricInfo(metric).label}: ${preview.coverage.missing_by_metric?.[metric] ?? 'some'} missing`).join(' · ')}. These rules need more comparable history for those stocks.</p>}
          {preview.stocks.length > 0 ? <>
            <div className="fq-table-wrap"><table className="data-table fq-table"><thead><tr><th>Stock</th>{previewMetrics.map((metric) => <th key={metric}>{metricInfo(metric).label}</th>)}<th>Market cap source</th></tr></thead><tbody>{previewPageStocks.map((stock) => <tr key={stock.stock_id}><td><StockIdentity symbol={stock.symbol} name={stock.name} /></td>{previewMetrics.map((metric) => <td key={metric} data-sort-value={String(stock.metrics?.[metric] ?? '')}>{displayValue(stock.metrics?.[metric], metricInfo(metric).unit)}</td>)}<td>{stock.market_cap_source === 'estimated_pe_x_annual_profit' ? 'Estimated: P/E × annual profit' : stock.market_cap_source || '—'}</td></tr>)}</tbody></table></div>
            <div className="fq-pagination" aria-label="Qualifying stock pages">
              <span>Showing {currentPreviewPage * PREVIEW_PAGE_SIZE + 1}–{Math.min((currentPreviewPage + 1) * PREVIEW_PAGE_SIZE, preview.stocks.length)} of {preview.stocks.length}</span>
              <div><button type="button" className="btn btn-secondary" onClick={() => setPreviewPage((page) => Math.max(0, page - 1))} disabled={currentPreviewPage === 0}>Previous</button><span>Page {currentPreviewPage + 1} of {previewPageCount}</span><button type="button" className="btn btn-secondary" onClick={() => setPreviewPage((page) => Math.min(previewPageCount - 1, page + 1))} disabled={currentPreviewPage >= previewPageCount - 1}>Next</button></div>
            </div>
          </> : <p className="fq-muted">No stocks match this query on this date.</p>}
          {missingStocks.length > 0 && <section className="fq-missing-stocks"><div className="fq-section-heading"><div><span className="fq-step">INCOMPLETE INPUTS</span><h3>Stocks and their missing data</h3></div><span className="fq-count">{missingStocks.length}</span></div><p className="fq-muted">Each row lists the exact unavailable condition and its cause. “Already fails” identifies stocks that cannot qualify even if the missing data arrives.</p><div className="fq-table-wrap"><table className="data-table fq-table"><thead><tr><th>Stock</th><th>Missing condition and reason</th><th>Already fails</th></tr></thead><tbody>{missingPageStocks.map((stock) => <tr key={stock.stock_id}><td><StockIdentity symbol={stock.symbol} name={stock.name} /></td><td><ul className="fq-gap-stock-list">{stock.missing.map((item) => <li key={`${item.metric}-${item.code}`}><strong>{metricInfo(item.metric).label}</strong><span>{item.label}{item.basis ? ` · ${item.basis} history` : ''}</span></li>)}</ul></td><td>{stock.known_failures.length > 0 ? <ul className="fq-gap-stock-list">{stock.known_failures.map((failure, index) => <li key={`${failure.metric}-${index}`}><strong>{failure.label}</strong><span>Actual {displayValue(failure.actual, metricInfo(failure.metric).unit)} {failure.operator} required {displayValue(failure.threshold, metricInfo(failure.metric).unit)}</span></li>)}</ul> : 'No known failing condition'}</td></tr>)}</tbody></table></div><div className="fq-pagination" aria-label="Incomplete stock pages"><span>Showing {currentMissingPage * PREVIEW_PAGE_SIZE + 1}–{Math.min((currentMissingPage + 1) * PREVIEW_PAGE_SIZE, missingStocks.length)} of {missingStocks.length}</span><div><button type="button" className="btn btn-secondary" onClick={() => setMissingPage((page) => Math.max(0, page - 1))} disabled={currentMissingPage === 0}>Previous</button><span>Page {currentMissingPage + 1} of {missingPageCount}</span><button type="button" className="btn btn-secondary" onClick={() => setMissingPage((page) => Math.min(missingPageCount - 1, page + 1))} disabled={currentMissingPage >= missingPageCount - 1}>Next</button></div></div></section>}
          {preview.limitations?.length > 0 && <div className="fq-limitations"><strong>Data notes</strong><ul>{preview.limitations.map((item, index) => <li key={index}>{item}</li>)}</ul></div>}
        </div>}

        <div className="fq-inline-backtest">
          <div className="fq-inline-backtest-heading"><div><span className="fq-step">BACKTEST PRESETS</span><h3>Choose a system setup</h3></div><span className="fq-count">{setups.length} setups</span></div>
          <p className="fq-muted">Patterns are predefined in the setup library. Select one preset or compare all; you do not need to build technical rules.</p>
          {setupsLoading ? <p className="fq-muted">Loading setup presets…</p> : <div className="fq-setup-selector">
            <label className="fq-membership-field" htmlFor="fq-setup-select"><span>Technical setup preset</span><select id="fq-setup-select" value={selectedSetupId} onChange={(event) => setSelectedSetupId(event.target.value)}><option value={ALL_SETUP_PRESETS}>Compare all setup presets ({setups.length}) · 5m</option>{setups.map((setup) => <option key={setup.id} value={setup.id}>{setup.name} · {setup.candle_interval}</option>)}</select></label>
            <div className="fq-setup-summary" aria-live="polite">{selectedSetup ? <><strong>{selectedSetup.name}</strong><span>{selectedSetup.description}</span><small>Execution interval: {selectedSetup.candle_interval}. Entry: {selectedSetup.entry_rule} Exit: {selectedSetup.exit_rule}</small></> : <><strong>All {setups.length} setup presets</strong><span>Every preset uses five-minute execution candles on the same query-selected stocks and date range, then the results are ranked side by side. The two Watchlist Analysis presets also use daily context filters.</span></>}</div>
          </div>}
          <div className="fq-run-fields fq-run-dates"><label><span>Start date</span><input aria-label="Backtest start date" type="date" value={startDate} onChange={(event) => setStartDate(event.target.value)} /></label><label><span>End date</span><input aria-label="Backtest end date" type="date" value={endDate} onChange={(event) => setEndDate(event.target.value)} /></label></div>
          <p className="fq-parameter-note">Capital is fixed at ₹1,00,000 per selected stock. Each stock compounds in its own wallet. {previewIsCurrent && preview ? `For ${preview.stocks.length} selected stocks, total starting capital is ${displayRupees(preview.stocks.length * DEFAULT_CAPITAL_PER_STOCK)}.` : 'Total starting capital is the selected stock count × ₹1,00,000.'}</p>
          {usesStandaloneSetupRiskControls && <details className="fq-advanced-settings"><summary>Setup risk settings</summary><div className="fq-run-fields"><label><span>Profit target (× entry risk)</span><input type="number" min="0.5" max="10" step="0.1" value={targetR} onChange={(event) => setTargetR(event.target.value)} /></label><label><span>Maximum hold (5-minute candles)</span><input type="number" min="1" max="7500" step="1" value={maxHoldBars} onChange={(event) => setMaxHoldBars(event.target.value)} /></label><label><span>Stop buffer (%)</span><input type="number" min="0" max="10" step="0.1" value={stopBufferPct} onChange={(event) => setStopBufferPct(event.target.value)} /></label></div><p className="fq-parameter-note">Standalone setups check every five-minute candle. The default 1,500-candle hold limit is about 20 full NSE sessions; entries and time exits use the next candle&apos;s open.</p>{usesWatchlistFilterSetups && <p className="fq-parameter-note">Strong trend continuation and Liquid swing momentum keep their Watchlist Analysis stop, target, and holding-period defaults. Their entries and exits are still simulated on five-minute candles.</p>}</details>}
          {usesWatchlistFilterSetups && !usesStandaloneSetupRiskControls && <p className="fq-parameter-note">Strong trend continuation and Liquid swing momentum keep their Watchlist Analysis stop, target, and holding-period defaults.</p>}
          <div className="fq-run-footer"><p>The query-selected stock list is fixed for the run. All selected setups use five-minute candles for entry and exit timing; the Watchlist Analysis-derived setups also use their completed daily context filters.</p><button type="button" className="btn btn-primary" onClick={handleRun} disabled={busy != null || !currentRules || setupsLoading || setupIds.length === 0}>{busy === 'run' ? 'Starting…' : isSavedVersion ? 'Run backtest' : 'Save query & run backtest'}</button></div>
        </div>
      </section>

      {jobId && <section className="card fq-progress" aria-live="polite">
        <div className="fq-section-heading"><div><span className="fq-step">RUN {jobId}</span><h2>{!job ? 'Loading backtest…' : noStocks ? job.universe_selection ? 'No stocks selected by the query' : 'Older run: no stocks matched historical checks' : job.status === 'completed' ? 'Backtest complete' : job.status === 'partial' ? 'Backtest completed with gaps' : job.status === 'failed' ? 'Backtest failed' : job.status === 'cancelled' ? 'Backtest cancelled' : 'Backtest in progress'}</h2></div><strong>{noStocks ? 'No stocks to test' : `${job?.progress?.completed ?? 0} / ${job?.progress?.total ?? '—'}`}</strong></div>
        <div className="fq-progress-track"><span style={{ width: `${Math.min(100, Math.max(0, progress))}%` }} /></div>
        {job?.query && <><p className="fq-muted">{job.query.name} · version {job.query.version} · {job.query.rules.length} conditions</p><div className="fq-query-text fq-run-query"><span>Query used to select stocks</span><p>{job.query.query_text || queryText(draftFromRules(job.query.rules))}</p></div></>}
        {job?.settings && <p className="fq-run-settings">
          Execution candles: {job.settings.candle_interval || 'not recorded for this saved run'}.{' '}
          {job.settings.candle_interval === '5m' && job.settings.setup_ids?.some((setupId) => !WATCHLIST_FILTER_SETUP_IDS.has(setupId)) && <>
            Standalone setup exits: {job.settings.target_r}× entry-risk target · {job.settings.max_hold_bars} five-minute candles (about {(job.settings.max_hold_bars / 75).toFixed(1)} sessions) · {job.settings.stop_buffer_pct}% stop buffer.{' '}
          </>}
          {!job.settings.candle_interval && job.settings.setup_ids?.some((setupId) => !WATCHLIST_FILTER_SETUP_IDS.has(setupId)) && 'Standalone setup candle interval was not recorded for this saved run. '}
          {job.settings.setup_ids?.some((setupId) => WATCHLIST_FILTER_SETUP_IDS.has(setupId)) && 'Watchlist-derived presets retain their original stop, target, and holding-period defaults.'}
        </p>}
        {jobReadError && <div className="bt-error">{jobReadError}</div>}
        {job?.error && <div className="bt-error">{job.error}</div>}
      </section>}

      {job && TERMINAL.has(job.status) && <div className="fq-results">
        {job.universe_selection ? (
          <section className="card fq-result-section">
            <div className="fq-section-heading"><div><span className="fq-step">QUERY-SELECTED STOCKS</span><h2>Stock list used for every setup</h2></div><span className="fq-count">{job.universe_selection.stock_count} stocks</span></div>
          <p className="fq-muted">Selected by the saved query as of {displayDate(job.universe_selection.as_of)}. This same stock list was used for the full historical candle range. Starting capital: {displayRupees(job.universe_selection.stock_count * (job.settings?.capital_per_stock ?? job.settings?.initial_capital ?? DEFAULT_CAPITAL_PER_STOCK))} total at {displayRupees(job.settings?.capital_per_stock ?? job.settings?.initial_capital ?? DEFAULT_CAPITAL_PER_STOCK)} per stock.</p>
            <details className="fq-selected-stock-list"><summary>View the exact stock list</summary><p className="fq-muted">{job.universe_selection.symbols.join(' · ')}</p></details>
          </section>
        ) : qualificationPoints.length > 0 && (
          <section className="card fq-result-section">
            <div className="fq-section-heading"><div><span className="fq-step">SAVED RUN</span><h2>Previous stock-selection method</h2></div></div>
            <p className="fq-muted">This saved result came from the earlier version, which rechecked the query at historical cutoffs. Run the backtest again to use the query-selected stock list for the full period.</p>
          </section>
        )}

        <section className="card fq-result-section">
          <div className="fq-section-heading"><div><span className="fq-step">SETUP COMPARISON</span><h2>Technical setup leaderboard</h2></div></div>
          <p className="fq-muted">Portfolio period return is the percentage change from the total starting capital to the sum of the ending stock wallets. Capital is fixed at ₹1 lakh per selected stock; stocks that could not be tested remain idle cash in this calculation. Mean tested-stock return averages only completed stock runs. Neither is annualized. Average trade is the arithmetic mean return across completed trades. Every result column can be sorted.</p>
          {job.universe_selection ? job.leaderboard?.length > 0 ? <div className="fq-table-wrap"><table className="data-table fq-table">
            <thead><tr><th>Setup</th><th>Stocks tested</th><th>Trades</th><th>Win rate</th><th>Portfolio period return</th><th>Mean tested-stock return</th><th>Average trade</th><th>Mean stock drawdown</th><th>Details</th></tr></thead>
            <tbody>{job.leaderboard.map((row) => <tr key={row.setup_id}>
              <td><button type="button" className="fq-result-link" onClick={() => setSelectedTradeSetupId(row.setup_id)} aria-label={`View ${row.setup_name} trades`}><strong>{row.setup_name}</strong></button></td>
              <td data-sort-value={String(row.stock_count)}>{row.stock_count}</td>
              <td data-sort-value={String(row.trades)}>{row.trades}</td>
              <td data-sort-value={String(row.win_rate_pct ?? '')}>{displayValue(row.win_rate_pct, '%')}</td>
              <td className={percentageClass(row.portfolio_period_return_pct)} data-sort-value={String(row.portfolio_period_return_pct ?? '')}><strong>{displayValue(row.portfolio_period_return_pct, '%')}</strong><br /><small>{row.portfolio_initial_capital == null ? 'Rerun to calculate' : `${displayRupees(row.portfolio_initial_capital)} → ${displayRupees(row.portfolio_final_capital)}`}</small></td>
              <td className={percentageClass(row.total_return_pct)} data-sort-value={String(row.total_return_pct ?? '')}><button type="button" className={`fq-result-link ${percentageClass(row.total_return_pct)}`} onClick={() => setSelectedTradeSetupId(row.setup_id)} aria-label={`View ${row.setup_name} trades; mean stock return for the selected period ${displayValue(row.total_return_pct, '%')}`}>{displayValue(row.total_return_pct, '%')}</button></td>
              <td className={percentageClass(row.average_return_pct)} data-sort-value={String(row.average_return_pct ?? '')}>{displayValue(row.average_return_pct, '%')}</td>
              <td data-sort-value={String(row.max_drawdown_pct ?? '')}>{displayValue(row.max_drawdown_pct, '%')}</td>
              <td><button type="button" className="btn btn-secondary fq-view-trades" onClick={() => setSelectedTradeSetupId(row.setup_id)}>View {row.trades} trades</button></td>
            </tr>)}</tbody>
          </table></div> : <p className="fq-muted">No setup results are available for the selected stocks.</p> : <p className="fq-muted">Run this backtest again to compare setups on the query-selected stock list.</p>}
        </section>

        {selectedTradeSetup && <div className="bt-comparison-modal-backdrop" role="presentation" onClick={() => setSelectedTradeSetupId(null)}>
          <section className="bt-comparison-modal fq-trade-modal" role="dialog" aria-modal="true" aria-labelledby="fq-trade-modal-title" onClick={(event) => event.stopPropagation()}>
            <div className="bt-comparison-modal-header">
              <div><span className="bt-comparison-detail-label">Selected technical setup</span><h2 id="fq-trade-modal-title" className="bt-comparison-detail-title">{selectedTradeSetup.setup_name}</h2><p className="bt-comparison-subtitle">{selectedTradeSetup.trades} trades across {selectedTradeSetup.stock_count} tested stocks · mean stock return for selected period {displayValue(selectedTradeSetup.total_return_pct, '%')}</p></div>
              <button type="button" className="btn btn-secondary bt-comparison-close" onClick={() => setSelectedTradeSetupId(null)}>Close</button>
            </div>
            {selectedSetupTrades.length > 0 ? <div className="fq-table-wrap fq-trade-table-wrap"><table className="data-table fq-table fq-trade-table">
              <thead><tr><th>Stock</th><th>Signal time</th><th>Entry time</th><th>Entry price</th><th>Swing low</th><th>Swing high</th><th>Volume POC</th><th>Value-area low</th><th>Value-area high</th><th>Stop</th><th>Target</th><th>Exit time</th><th>Exit price</th><th>Return</th><th>Exit reason</th></tr></thead>
              <tbody>{selectedSetupTrades.map((trade, index) => <tr key={`${trade.symbol}-${trade.entry_date}-${index}`}>
                <td data-sort-value={`${trade.stock_name} ${trade.symbol}`}><StockIdentity symbol={trade.symbol} name={trade.stock_name} /></td>
                <td data-sort-value={trade.signal_date}>{displayTradeTimestamp(trade.signal_date, true)}</td>
                <td data-sort-value={trade.entry_date}>{displayTradeTimestamp(trade.entry_date, true)}</td>
                <td data-sort-value={String(trade.entry_price ?? '')}>{displayPrice(trade.entry_price)}</td>
                <td data-sort-value={String(trade.swing_low ?? '')}>{displayPrice(trade.swing_low)}</td>
                <td data-sort-value={String(trade.swing_high ?? '')}>{displayPrice(trade.swing_high)}</td>
                <td data-sort-value={String(trade.poc ?? '')}>{displayPrice(trade.poc)}</td>
                <td data-sort-value={String(trade.value_area_low ?? '')}>{displayPrice(trade.value_area_low)}</td>
                <td data-sort-value={String(trade.value_area_high ?? '')}>{displayPrice(trade.value_area_high)}</td>
                <td data-sort-value={String(trade.stop_price ?? '')}>{displayPrice(trade.stop_price)}</td>
                <td data-sort-value={String(trade.target_price ?? '')}>{displayPrice(trade.target_price)}</td>
                <td data-sort-value={trade.exit_date}>{displayTradeTimestamp(trade.exit_date, true)}</td>
                <td data-sort-value={String(trade.exit_price ?? '')}>{displayPrice(trade.exit_price)}</td>
                <td className={percentageClass(trade.return_pct)} data-sort-value={String(trade.return_pct ?? '')}>{displayValue(trade.return_pct, '%')}</td>
                <td data-sort-value={trade.exit_reason?.replaceAll('_', ' ') || ''}>{trade.exit_reason?.replaceAll('_', ' ') || '—'}</td>
              </tr>)}</tbody>
            </table></div> : <p className="fq-muted">No trades were generated for this setup in the selected date range.</p>}
          </section>
        </div>}
        {job.failed_stocks && job.failed_stocks.length > 0 && <section className="card fq-result-section"><div className="fq-section-heading"><div><span className="fq-step">DATA GAPS</span><h2>Stocks that could not be tested</h2></div><span className="fq-count">{job.failed_stocks.length}</span></div><div className="fq-table-wrap"><table className="data-table fq-table"><thead><tr><th>Stock</th><th>Reason</th></tr></thead><tbody>{job.failed_stocks.map((stock) => <tr key={stock.symbol}><td><StockIdentity symbol={stock.symbol} /></td><td>{stock.error}</td></tr>)}</tbody></table></div></section>}
        {job.limitations?.length > 0 && <section className="card fq-result-section fq-limitations"><strong>Method and data limitations</strong><ul>{job.limitations.map((item, index) => <li key={index}>{item}</li>)}</ul></section>}
      </div>}
    </div>
  );
}
