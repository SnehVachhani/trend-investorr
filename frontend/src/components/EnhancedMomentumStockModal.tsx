'use client';

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import {
  getCandles,
  getStock,
  getStockFundamentals,
  type FundamentalData,
  type FundamentalCompetitor,
  type FundamentalLine,
  type FundamentalRatio,
  type StockFundamentalsResponse,
} from '@/lib/api';
import { assessFundamentals, ratioRating } from '@/lib/fundamental-analysis';
import { formatStockSymbol, getStockCompanyName } from '@/lib/stock-display';
import type { Candle, CandleInterval, StockDetail, StockModalData } from '@/lib/types';
import CandleChart from './CandleChart';
import StockIdentity from './StockIdentity';

interface ChartRange {
  key: string;
  label: string;
  interval: CandleInterval;
  fetchDays: number;
  description: string;
  durationDays?: number;
  sessions?: number;
}

const CHART_RANGES: ChartRange[] = [
  { key: '1D', label: '1D', interval: '5m', fetchDays: 10, sessions: 1, description: '1 trading day' },
  { key: '1W', label: '1W', interval: '15m', fetchDays: 30, sessions: 5, description: '1 trading week' },
  { key: '1M', label: '1M', interval: '1h', fetchDays: 60, durationDays: 30, description: '1 month' },
  { key: '3M', label: '3M', interval: '1d', fetchDays: 120, durationDays: 90, description: '3 months' },
  { key: '6M', label: '6M', interval: '1d', fetchDays: 210, durationDays: 180, description: '6 months' },
  { key: '1Y', label: '1Y', interval: '1d', fetchDays: 400, durationDays: 365, description: '1 year' },
  { key: '2Y', label: '2Y', interval: '1w', fetchDays: 730, durationDays: 730, description: '2 years' },
];

const NSE_DATE_FORMATTER = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Asia/Kolkata', year: 'numeric', month: '2-digit', day: '2-digit',
});
const NSE_TIME_FORMATTER = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
});

function nseDateKey(time: number): string {
  const parts = NSE_DATE_FORMATTER.formatToParts(new Date(time * 1000));
  const year = parts.find((part) => part.type === 'year')?.value ?? '';
  const month = parts.find((part) => part.type === 'month')?.value ?? '';
  const day = parts.find((part) => part.type === 'day')?.value ?? '';
  return `${year}-${month}-${day}`;
}

function nseMinuteOfDay(time: number): number {
  const parts = NSE_TIME_FORMATTER.formatToParts(new Date(time * 1000));
  const hour = Number(parts.find((part) => part.type === 'hour')?.value ?? 0);
  const minute = Number(parts.find((part) => part.type === 'minute')?.value ?? 0);
  return hour * 60 + minute;
}

function consolidateDailyCandles(candles: Candle[]): Candle[] {
  const sessions = new Map<string, Candle[]>();
  for (const candle of candles) {
    const key = nseDateKey(candle.time);
    sessions.set(key, [...(sessions.get(key) ?? []), candle]);
  }

  return [...sessions.entries()].map(([dateKey, sessionCandles]) => {
    const ordered = [...sessionCandles].sort((left, right) => left.time - right.time);
    const first = ordered[0];
    const last = ordered[ordered.length - 1];
    const [year, month, day] = dateKey.split('-').map(Number);
    return {
      time: Date.UTC(year, month - 1, day) / 1000,
      open: first.open,
      high: Math.max(...ordered.map((candle) => candle.high)),
      low: Math.min(...ordered.map((candle) => candle.low)),
      close: last.close,
      volume: last.volume,
    };
  }).sort((left, right) => left.time - right.time);
}

function candlesForRange(candles: Candle[], range: ChartRange): Candle[] {
  if (candles.length === 0) return [];
  if (range.sessions) {
    const sessions = [...new Set(candles.map((candle) => nseDateKey(candle.time)))].slice(-range.sessions);
    const sessionSet = new Set(sessions);
    const result = candles.filter((candle) => sessionSet.has(nseDateKey(candle.time)));
    if (range.sessions !== 1 || result.length === 0) return result;
    const marketHours = result.filter((candle) => {
      const minute = nseMinuteOfDay(candle.time);
      return minute >= (9 * 60 + 15) && minute <= (15 * 60 + 30);
    });
    return marketHours.length > 0 ? marketHours : result;
  }

  const uniqueCandles = range.interval === '1d' ? consolidateDailyCandles(candles) : candles;
  if (!range.durationDays) return uniqueCandles;
  const latestTime = uniqueCandles[uniqueCandles.length - 1].time * 1000;
  const cutoff = latestTime - range.durationDays * 24 * 60 * 60 * 1000;
  return uniqueCandles.filter((candle) => candle.time * 1000 >= cutoff);
}

function asNumber(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value !== 'string') return null;
  const parsed = Number(value.replace(/[,%₹,\s]/g, ''));
  return Number.isFinite(parsed) ? parsed : null;
}

function display(value: unknown, digits = 2): string {
  if (value == null || value === '') return '—';
  const numeric = asNumber(value);
  if (numeric != null) return numeric.toLocaleString('en-IN', { maximumFractionDigits: digits });
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

function titleCase(value: unknown): string {
  return String(value ?? '')
    .replace(/[_-]+/g, ' ')
    .replace(/\b\w/g, (letter) => letter.toUpperCase()) || '—';
}

function formatMoney(value: number | null | undefined): string {
  return value == null ? '—' : `₹${value.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function formatDate(value: unknown): string {
  if (!value) return '—';
  const date = new Date(String(value));
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' });
}

function sectionDate(data: FundamentalData | null): string {
  const dates = [data?.profile?.snapshot_date, ...((data?.sync_state ?? []).map((item) => item.last_success_at))]
    .filter(Boolean)
    .map((value) => new Date(String(value)).getTime())
    .filter(Number.isFinite);
  return dates.length ? formatDate(Math.max(...dates)) : 'No sync recorded';
}

function peerCompanyName(peer: FundamentalCompetitor): string {
  const provided = String(peer.company_name ?? '').trim();
  if (provided) return provided;
  const profile = String(peer.company_profile ?? '').trim();
  const firstClause = profile.match(/^(.{1,140}?)\s+is\b/i);
  return firstClause ? firstClause[1].replace(/\s*\([^)]*\)/g, '').trim() : '';
}

function RatingPill({ value }: { value: string }) {
  return <span className={`fund-rating-pill ${value.toLowerCase().replace(/\s+/g, '-')}`}>{value}</span>;
}

function EmptyMessage({ children }: { children: ReactNode }) {
  return <div className="fund-empty-message">{children}</div>;
}

function RatioSection({ ratios }: { ratios: FundamentalRatio[] }) {
  if (ratios.length === 0) return <EmptyMessage>No key ratios have been synced for this stock.</EmptyMessage>;
  return (
    <div className="fund-table-wrap">
      <table className="fund-table">
        <thead><tr><th>Ratio</th><th>Company</th><th>Sector</th><th>Rating</th></tr></thead>
        <tbody>{ratios.map((ratio, index) => (
          <tr key={`${ratio.ratio_name}-${index}`}>
            <td>{ratio.ratio_name || 'Ratio'}</td>
            <td>{ratio.company_raw || display(ratio.company_value)}</td>
            <td>{ratio.sector_raw || display(ratio.sector_value)}</td>
            <td><RatingPill value={ratioRating(ratio)} /></td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}

function RatioHistoryTable({ ratios }: { ratios: FundamentalRatio[] }) {
  if (ratios.length === 0) return <EmptyMessage>No historical ratio snapshots are available yet.</EmptyMessage>;

  const periodMap = new Map<string, { key: string; snapshotDate: unknown }>();
  const ratioMap = new Map<string, { key: string; label: string; values: Map<string, FundamentalRatio> }>();
  for (const ratio of ratios) {
    const snapshotDate = ratio.snapshot_date;
    const key = periodKey(snapshotDate, snapshotDate, 'snapshot');
    if (!periodMap.has(key)) periodMap.set(key, { key, snapshotDate });
    const ratioKey = normalizeKey(ratio.ratio_name) || 'other';
    let row = ratioMap.get(ratioKey);
    if (!row) {
      row = { key: ratioKey, label: titleCase(ratio.ratio_name), values: new Map() };
      ratioMap.set(ratioKey, row);
    }
    row.values.set(key, ratio);
  }

  const periods = [...periodMap.values()].sort((left, right) => (
    periodTimestamp(left.snapshotDate, left.snapshotDate) - periodTimestamp(right.snapshotDate, right.snapshotDate)
  ));
  const viewportKey = periods.map((period) => period.key).join('|');

  return (
    <div className="fund-period-table-block">
      <PeriodTableViewport periodKey={viewportKey} label="Ratio history; newest snapshot on the right, scroll horizontally for earlier snapshots">
        <table className="fund-table fund-statement-pivot fund-ratio-history" aria-label="Ratio history by snapshot date">
          <thead><tr>
            <th scope="col">Ratio</th>
            {periods.map((period) => <th key={period.key} scope="col"><span>{formatDate(period.snapshotDate)}</span></th>)}
          </tr></thead>
          <tbody>{[...ratioMap.values()].map((row) => (
            <tr key={row.key}>
              <th scope="row">{row.label}</th>
              {periods.map((period) => {
                const item = row.values.get(period.key);
                return (
                  <td key={period.key}>
                    {item ? (
                      <div className="fund-ratio-history-cell">
                        <strong>{item.company_raw || display(item.company_value)}</strong>
                        <small>Sector · {item.sector_raw || display(item.sector_value)}</small>
                      </div>
                    ) : '—'}
                  </td>
                );
              })}
            </tr>
          ))}</tbody>
        </table>
      </PeriodTableViewport>
    </div>
  );
}

type ReportingBasis = 'consolidated' | 'standalone';

function normalizeKey(value: unknown): string {
  return String(value ?? '').toLowerCase().replace(/[^a-z0-9]/g, '');
}

function periodKey(periodEnd: unknown, periodLabel: unknown, frequency: unknown): string {
  return String(frequency || 'reported') + '|' + String(periodEnd || periodLabel || 'unknown');
}

function periodTimestamp(periodEnd: unknown, periodLabel: unknown): number {
  const endDate = periodEnd ? Date.parse(String(periodEnd)) : Number.NaN;
  if (Number.isFinite(endDate)) return endDate;
  const labelDate = periodLabel ? Date.parse(String(periodLabel)) : Number.NaN;
  if (Number.isFinite(labelDate)) return labelDate;
  const year = String(periodLabel ?? '').match(/(19|20)\d{2}/);
  return year ? Date.UTC(Number(year[0]), 0, 1) : 0;
}

function unitLabel(value: unknown): string {
  const unit = String(value ?? '');
  const key = normalizeKey(unit);
  if (key === 'crore' || key === 'crores') return '₹ crore';
  if (key.includes('rupeespershare')) return '₹ / share';
  if (key.includes('percent') || key === 'percentage') return '%';
  if (key === 'rupees' || key === 'inr') return '₹';
  return titleCase(unit);
}

function periodHeading(periodEnd: unknown, periodLabel: unknown, frequency: unknown): { primary: string; secondary: string } {
  const sourceLabel = String(periodLabel || formatDate(periodEnd));
  const normalizedFrequency = normalizeKey(frequency);
  const endDate = periodEnd ? Date.parse(String(periodEnd)) : Number.NaN;
  const labelDate = Number.isFinite(endDate) ? endDate : Date.parse(sourceLabel);
  let fiscalQuarter: number | null = null;
  let fiscalYear: number | null = null;

  if (Number.isFinite(labelDate)) {
    const date = new Date(labelDate);
    const month = date.getUTCMonth() + 1;
    const year = date.getUTCFullYear();
    if (normalizedFrequency === 'quarterly') {
      fiscalQuarter = month >= 4 ? Math.ceil((month - 3) / 3) : 4;
      fiscalYear = month >= 4 ? year + 1 : year;
    } else if (normalizedFrequency === 'yearly') {
      fiscalYear = year;
    }
  } else {
    const quarterMatch = sourceLabel.match(/\bQ([1-4])\b.*?FY\s?(20)?(\d{2,4})/i);
    const yearMatch = sourceLabel.match(/(19|20)\d{2}/);
    if (quarterMatch) {
      fiscalQuarter = Number(quarterMatch[1]);
      const parsedYear = Number(quarterMatch[3]);
      fiscalYear = parsedYear < 100 ? 2000 + parsedYear : parsedYear;
    } else if (yearMatch) {
      fiscalYear = Number(yearMatch[0]);
    }
  }

  const shortYear = fiscalYear == null ? '' : String(fiscalYear).slice(-2);
  if (normalizedFrequency === 'quarterly' && fiscalQuarter != null && shortYear) {
    return { primary: 'Q' + fiscalQuarter + ' FY' + shortYear, secondary: sourceLabel };
  }
  if (normalizedFrequency === 'yearly' && shortYear) {
    return { primary: 'FY' + shortYear, secondary: sourceLabel };
  }
  return { primary: sourceLabel, secondary: '' };
}

function BasisToggle({ value, available, onChange, label }: {
  value: ReportingBasis;
  available: Record<ReportingBasis, boolean>;
  onChange: (next: ReportingBasis) => void;
  label: string;
}) {
  return (
    <div className="fund-toggle-group" role="group" aria-label={label}>
      {(['consolidated', 'standalone'] as const).map((basis) => (
        <button
          key={basis}
          type="button"
          className={value === basis ? 'is-active' : ''}
          aria-pressed={value === basis}
          disabled={!available[basis]}
          title={available[basis] ? titleCase(basis) : 'No ' + basis + ' statement is available from Upstox'}
          onClick={() => onChange(basis)}
        >
          {titleCase(basis)}
        </button>
      ))}
    </div>
  );
}

function FrequencyToggle({ value, options, onChange, label }: {
  value: string;
  options: string[];
  onChange: (next: string) => void;
  label: string;
}) {
  return (
    <div className="fund-toggle-group" role="group" aria-label={label}>
      {options.map((frequency) => (
        <button
          key={frequency}
          type="button"
          className={value === frequency ? 'is-active' : ''}
          aria-pressed={value === frequency}
          onClick={() => onChange(frequency)}
        >
          {['yearly', 'annual', 'annually'].includes(normalizeKey(frequency)) ? 'Annual' : titleCase(frequency)}
        </button>
      ))}
    </div>
  );
}

function PeriodTableViewport({ periodKey: key, label, children, viewportClassName = 'fund-pivot-wrap' }: {
  periodKey: string;
  label: string;
  children: ReactNode;
  viewportClassName?: string;
}) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const [edges, setEdges] = useState({ hasOverflow: false, canScrollOlder: false, canScrollNewer: false });
  const syncEdges = useCallback(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const maxScroll = Math.max(0, viewport.scrollWidth - viewport.clientWidth);
    setEdges({
      hasOverflow: maxScroll > 2,
      canScrollOlder: viewport.scrollLeft > 2,
      canScrollNewer: viewport.scrollLeft < maxScroll - 2,
    });
  }, []);

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    viewport.scrollLeft = viewport.scrollWidth;
    syncEdges();
    viewport.addEventListener('scroll', syncEdges, { passive: true });
    const resizeObserver = new ResizeObserver(syncEdges);
    resizeObserver.observe(viewport);
    if (viewport.firstElementChild) resizeObserver.observe(viewport.firstElementChild);
    return () => {
      viewport.removeEventListener('scroll', syncEdges);
      resizeObserver.disconnect();
    };
  }, [key, syncEdges]);

  const scrollHorizontally = (direction: -1 | 1) => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    viewport.scrollBy({ left: direction * Math.max(220, viewport.clientWidth * 0.78), behavior: 'smooth' });
  };

  return (
    <div className="fund-period-scroll-shell">
      <div
        ref={viewportRef}
        className={'fund-table-wrap ' + viewportClassName}
        role="region"
        aria-label={label}
        tabIndex={0}
      >
        {children}
      </div>
      {edges.hasOverflow && (
        <div className="fund-period-scroll-controls">
          <button type="button" aria-label="Scroll to older periods" title="Older periods" disabled={!edges.canScrollOlder} onClick={() => scrollHorizontally(-1)}>←</button>
          <button type="button" aria-label="Scroll to newer periods" title="Newer periods" disabled={!edges.canScrollNewer} onClick={() => scrollHorizontally(1)}>→</button>
        </div>
      )}
    </div>
  );
}

function StatementTable({ lines, title, scrollKey }: { lines: FundamentalLine[]; title: string; scrollKey: string }) {
  if (lines.length === 0) return <EmptyMessage>No reported periods are available for this selection.</EmptyMessage>;

  const periodMap = new Map<string, { key: string; label: string; periodEnd: string | null; frequency: string }>();
  const metricMap = new Map<string, {
    key: string;
    label: string;
    unit: string;
    values: Map<string, { value: number | null; change: number | null }>;
  }>();

  for (const line of lines) {
    const key = periodKey(line.period_end, line.period_label, line.frequency);
    if (!periodMap.has(key)) {
      periodMap.set(key, {
        key,
        label: String(line.period_label || formatDate(line.period_end)),
        periodEnd: line.period_end ?? null,
        frequency: String(line.frequency || ''),
      });
    }
    const metricKey = normalizeKey(line.metric_name) || 'other';
    let metric = metricMap.get(metricKey);
    if (!metric) {
      metric = {
        key: metricKey,
        label: titleCase(line.metric_name),
        unit: unitLabel(line.unit),
        values: new Map(),
      };
      metricMap.set(metricKey, metric);
    }
    metric.values.set(key, {
      value: asNumber(line.value),
      change: asNumber(line.change_pct),
    });
  }

  const periods = [...periodMap.values()].sort((left, right) => (
    periodTimestamp(left.periodEnd, left.label) - periodTimestamp(right.periodEnd, right.label)
  ));
  const metrics = [...metricMap.values()];
  const viewportKey = scrollKey + '|' + periods.map((period) => period.key).join('|');

  return (
    <div className="fund-period-table-block">
      <PeriodTableViewport periodKey={viewportKey} label={title + ' periods; newest on the right, scroll horizontally for older periods'}>
        <table className="fund-table fund-statement-pivot" aria-label={title + ' by reported period'}>
        <thead>
          <tr>
            <th scope="col">Metric</th>
            {periods.map((period) => {
              const heading = periodHeading(period.periodEnd, period.label, period.frequency);
              return (
                <th key={period.key} scope="col">
                  <span>{heading.primary}</span>
                  {heading.secondary && <small>{heading.secondary}</small>}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {metrics.map((metric) => (
            <tr key={metric.key}>
              <th scope="row">
                <span>{metric.label}</span>
                {metric.unit !== '—' && <small>{metric.unit}</small>}
              </th>
              {periods.map((period) => {
                const cell = metric.values.get(period.key);
                return (
                  <td key={period.key}>
                    {cell ? (
                      <>
                        <strong>{display(cell.value)}</strong>
                        {cell.change != null && <small className="fund-period-change">{display(cell.change, 1)}% reported change</small>}
                      </>
                    ) : '—'}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
        </table>
      </PeriodTableViewport>
    </div>
  );
}

function preferredMetric(lines: FundamentalLine[], preferred: string[]): FundamentalLine | null {
  const matches = lines.filter((line) => asNumber(line.value) != null && preferred.some((name) => {
    const key = normalizeKey(line.metric_name);
    return key === name || key.startsWith(name);
  }));
  matches.sort((left, right) => {
    const leftKey = normalizeKey(left.metric_name);
    const rightKey = normalizeKey(right.metric_name);
    const leftRank = preferred.indexOf(leftKey);
    const rightRank = preferred.indexOf(rightKey);
    return (leftRank < 0 ? preferred.length + 1 : leftRank) - (rightRank < 0 ? preferred.length + 1 : rightRank);
  });
  return matches[0] ?? null;
}

function fiscalQuarterIdentity(periodEnd: unknown, periodLabel: unknown): { quarter: number; year: number } | null {
  const sourceLabel = String(periodLabel ?? '');
  const endDate = periodEnd ? Date.parse(String(periodEnd)) : Number.NaN;
  const labelDate = Number.isFinite(endDate) ? endDate : Date.parse(sourceLabel);
  if (Number.isFinite(labelDate)) {
    const date = new Date(labelDate);
    const month = date.getUTCMonth() + 1;
    const year = date.getUTCFullYear();
    return {
      quarter: month >= 4 ? Math.ceil((month - 3) / 3) : 4,
      year: month >= 4 ? year + 1 : year,
    };
  }
  const match = sourceLabel.match(/\bQ([1-4])\b.*?FY\s?(20)?(\d{2,4})/i);
  if (!match) return null;
  const parsedYear = Number(match[3]);
  return { quarter: Number(match[1]), year: parsedYear < 100 ? 2000 + parsedYear : parsedYear };
}

function fiscalYearNumber(periodEnd: unknown, periodLabel: unknown): number | null {
  const endDate = periodEnd ? Date.parse(String(periodEnd)) : Number.NaN;
  const labelDate = Number.isFinite(endDate) ? endDate : Date.parse(String(periodLabel ?? ''));
  if (Number.isFinite(labelDate)) return new Date(labelDate).getUTCFullYear();
  const sourceLabel = String(periodLabel ?? '');
  const fiscalMatch = sourceLabel.match(/\bFY\s?(20)?(\d{2,4})\b/i);
  if (fiscalMatch) {
    const parsedYear = Number(fiscalMatch[2]);
    return parsedYear < 100 ? 2000 + parsedYear : parsedYear;
  }
  const yearMatch = sourceLabel.match(/(19|20)\d{2}/);
  return yearMatch ? Number(yearMatch[0]) : null;
}

function percentageChange(previous: unknown, current: unknown): number | null {
  const previousValue = asNumber(previous);
  const currentValue = asNumber(current);
  if (previousValue == null || currentValue == null || previousValue <= 0) return null;
  return ((currentValue - previousValue) / previousValue) * 100;
}

function financialAmount(line: FundamentalLine | null): string {
  if (!line) return 'Not reported';
  const value = asNumber(line.value);
  if (value == null) return 'Not reported';
  const formatted = display(value, 2);
  const unit = normalizeKey(line.unit);
  if (unit.includes('crore')) return `₹${formatted} crore`;
  if (unit === 'rupees' || unit === 'inr' || unit === 'rs' || unit.includes('indianrupee')) return `₹${formatted}`;
  if (unit.includes('rupeesper')) return `₹${formatted} / share`;
  return `${formatted}${line.unit ? ` ${titleCase(line.unit)}` : ''}`;
}

function yoyLabel(current: FundamentalLine | null, previous: FundamentalLine | null): { text: string; tone: string } {
  const change = percentageChange(previous?.value, current?.value);
  if (change == null) return { text: 'YoY unavailable', tone: '' };
  return {
    text: `(${change > 0 ? '+' : ''}${change.toFixed(1)}% YoY)`,
    tone: change > 0 ? 'is-positive' : change < 0 ? 'is-negative' : '',
  };
}

function IncomeGrowthSummary({ lines }: { lines: FundamentalLine[] }) {
  const grouped = new Map<string, { key: string; periodEnd: string | null; label: string; lines: FundamentalLine[] }>();
  for (const line of lines) {
    if (!['yearly', 'annual', 'annually'].includes(normalizeKey(line.frequency))) continue;
    const key = periodKey(line.period_end, line.period_label, line.frequency);
    let period = grouped.get(key);
    if (!period) {
      period = {
        key,
        periodEnd: line.period_end == null ? null : String(line.period_end),
        label: String(line.period_label || formatDate(line.period_end)),
        lines: [],
      };
      grouped.set(key, period);
    }
    period.lines.push(line);
  }
  const periods = [...grouped.values()]
    .map((period) => ({
      ...period,
      fiscalYear: fiscalYearNumber(period.periodEnd, period.label),
      revenue: preferredMetric(period.lines, ['revenue', 'totalrevenue', 'revenuefromoperations', 'sales']),
      profit: preferredMetric(period.lines, ['profitaftertax', 'netprofit', 'profitfortheperiod', 'profitforperiod', 'pat']),
    }))
    .filter((period) => period.revenue || period.profit)
    .sort((left, right) => periodTimestamp(right.periodEnd, right.label) - periodTimestamp(left.periodEnd, left.label));
  const latest = periods[0];
  const calculate = (metric: 'revenue' | 'profit', years: number, compound: boolean) => {
    if (!latest || latest.fiscalYear == null) {
      return { value: null as number | null, detail: 'Annual periods are not available' };
    }
    const previous = periods.find((period) => period.fiscalYear === latest.fiscalYear! - years);
    if (!previous) {
      return {
        value: null as number | null,
        detail: years === 1 ? `Need FY${String(latest.fiscalYear - 1).slice(-2)} data` : `Need FY${String(latest.fiscalYear - years).slice(-2)} data`,
      };
    }
    const currentLine = latest[metric];
    const previousLine = previous[metric];
    if (!currentLine || !previousLine) return { value: null as number | null, detail: 'Metric not reported for both periods' };
    const currentValue = asNumber(currentLine.value);
    const previousValue = asNumber(previousLine.value);
    if (currentValue == null || previousValue == null) return { value: null as number | null, detail: 'Metric not reported for both periods' };
    if (compound && (currentValue <= 0 || previousValue <= 0)) {
      return { value: null as number | null, detail: 'CAGR requires positive values at both endpoints' };
    }
    if (!compound && previousValue <= 0) {
      return { value: null as number | null, detail: 'Growth is not meaningful from a non-positive base' };
    }
    const value = compound
      ? (Math.pow(currentValue / previousValue, 1 / years) - 1) * 100
      : ((currentValue - previousValue) / previousValue) * 100;
    return {
      value,
      detail: compound
        ? `${periodHeading(latest.periodEnd, latest.label, 'yearly').primary} to ${periodHeading(previous.periodEnd, previous.label, 'yearly').primary}`
        : `${periodHeading(latest.periodEnd, latest.label, 'yearly').primary} vs ${periodHeading(previous.periodEnd, previous.label, 'yearly').primary}`,
    };
  };
  const rows = [
    { label: '1Y growth', revenue: calculate('revenue', 1, false), profit: calculate('profit', 1, false) },
    { label: '3Y CAGR', revenue: calculate('revenue', 3, true), profit: calculate('profit', 3, true) },
  ];

  return (
    <section className="fund-growth-summary" aria-label="Income growth summary">
      <div className="fund-growth-heading"><strong>Growth</strong></div>
      <div className="fund-table-wrap">
        <table className="fund-table fund-growth-table">
          <thead><tr><th scope="col">Period</th><th scope="col">Revenue</th><th scope="col">Net profit</th></tr></thead>
          <tbody>{rows.map((row) => (
            <tr key={row.label}>
              <th scope="row">{row.label}</th>
              {(['revenue', 'profit'] as const).map((metric) => {
                const result = row[metric];
                return (
                  <td key={metric}>
                    <strong>{result.value == null ? '—' : `${result.value > 0 ? '+' : ''}${result.value.toFixed(1)}%`}</strong>
                    <small>{result.detail}</small>
                  </td>
                );
              })}
            </tr>
          ))}</tbody>
        </table>
      </div>
    </section>
  );
}

function RevenueProfitChart({ lines, frequency }: { lines: FundamentalLine[]; frequency: string }) {
  const [selectedPeriodKey, setSelectedPeriodKey] = useState<string | null>(null);
  const annual = ['yearly', 'annual', 'annually'].includes(normalizeKey(frequency));
  const periodFrequency = annual ? 'yearly' : 'quarterly';
  const grouped = new Map<string, { key: string; label: string; periodEnd: string | null; lines: FundamentalLine[] }>();
  for (const line of lines) {
    const lineIsAnnual = ['yearly', 'annual', 'annually'].includes(normalizeKey(line.frequency));
    if (lineIsAnnual !== annual) continue;
    const key = periodKey(line.period_end, line.period_label, line.frequency);
    let period = grouped.get(key);
    if (!period) {
      period = {
        key,
        label: String(line.period_label || formatDate(line.period_end)),
        periodEnd: line.period_end ?? null,
        lines: [],
      };
      grouped.set(key, period);
    }
    period.lines.push(line);
  }

  const allPoints = [...grouped.values()]
    .map((period) => ({
      ...period,
      revenue: preferredMetric(period.lines, ['revenue', 'totalrevenue', 'revenuefromoperations', 'sales']),
      profit: preferredMetric(period.lines, ['netprofit', 'profitaftertax', 'profitfortheperiod', 'profitforperiod', 'pat']),
    }))
    .filter((point) => point.revenue || point.profit)
    .sort((left, right) => periodTimestamp(left.periodEnd, left.label) - periodTimestamp(right.periodEnd, right.label));
  const points = allPoints;

  if (points.length === 0) {
    return <EmptyMessage>{annual ? 'Annual revenue and net profit are not available for this reporting basis.' : 'Quarterly revenue and net profit are not available for this reporting basis.'}</EmptyMessage>;
  }

  const selectedPoint = points.find((point) => point.key === selectedPeriodKey) ?? points[points.length - 1];
  const selectedFiscalYear = fiscalYearNumber(selectedPoint.periodEnd, selectedPoint.label);
  const selectedFiscalQuarter = fiscalQuarterIdentity(selectedPoint.periodEnd, selectedPoint.label);
  const priorYearPoint = annual
    ? selectedFiscalYear == null ? null : allPoints.find((point) => (
      fiscalYearNumber(point.periodEnd, point.label) === selectedFiscalYear - 1
    )) ?? null
    : selectedFiscalQuarter == null ? null : allPoints.find((point) => {
      const identity = fiscalQuarterIdentity(point.periodEnd, point.label);
      return identity?.quarter === selectedFiscalQuarter.quarter
        && identity.year === selectedFiscalQuarter.year - 1;
    }) ?? null;
  const selectedHeading = periodHeading(selectedPoint.periodEnd, selectedPoint.label, periodFrequency);
  const priorYearHeading = priorYearPoint
    ? periodHeading(priorYearPoint.periodEnd, priorYearPoint.label, periodFrequency)
    : null;

  const values = points.flatMap((point) => [asNumber(point.revenue?.value), asNumber(point.profit?.value)])
    .filter((value): value is number => value != null);
  const maxValue = Math.max(...values, 1);
  const minValue = Math.min(...values, 0);
  const valueRange = maxValue - minValue || 1;
  // Keep periods wide enough to inspect individually. The viewport can then
  // scroll even when Upstox only supplies a few periods for this symbol.
  const chartWidth = Math.max(720, 56 + points.length * 126);
  const chartHeight = 220;
  const left = 54;
  const right = 14;
  const top = 15;
  const plotHeight = 148;
  const plotWidth = chartWidth - left - right;
  const step = plotWidth / points.length;
  const baseline = top + (maxValue / valueRange) * plotHeight;
  const barWidth = Math.min(25, Math.max(16, step * 0.29));
  const rawUnit = points.find((point) => point.revenue?.unit || point.profit?.unit)?.revenue?.unit
    || points.find((point) => point.profit?.unit)?.profit?.unit;
  const unit = rawUnit ? unitLabel(rawUnit) : 'reported units';
  const revenueYoy = yoyLabel(selectedPoint.revenue, priorYearPoint?.revenue ?? null);
  const profitYoy = yoyLabel(selectedPoint.profit, priorYearPoint?.profit ?? null);

  const bar = (value: unknown, x: number, series: 'revenue' | 'profit', label: string, unitName: string) => {
    const numeric = asNumber(value);
    if (numeric == null) return null;
    const height = Math.abs(numeric) / valueRange * plotHeight;
    const y = numeric >= 0 ? baseline - height : baseline;
    const detail = label + ' · ' + display(numeric) + (unitName ? ' ' + unitName : '');
    return (
      <g key={series}>
        <title>{detail}</title>
        <rect
          className={'fund-quarterly-bar fund-quarterly-bar-' + series}
          x={x}
          y={y}
          width={barWidth}
          height={height}
          rx="3"
        />
      </g>
    );
  };

  return (
    <div className="fund-quarterly-card">
      <div className="fund-subsection-heading">
        <div><strong>{annual ? 'Annual performance' : 'Quarterly performance'}</strong></div>
        <div className="fund-quarterly-legend" aria-label="Chart legend">
          <span><i className="fund-legend-swatch is-revenue" />Revenue</span>
          <span><i className="fund-legend-swatch is-profit" />Net profit</span>
        </div>
      </div>
      <div className="fund-quarterly-readout" aria-live="polite" aria-atomic="true">
        <div className="fund-quarterly-readout-heading">
          <strong>{selectedHeading.primary} · {selectedPoint.label}</strong>
          <small>{priorYearHeading ? `Compared with ${priorYearHeading.primary} · ${priorYearPoint?.label}` : 'Comparison unavailable'}</small>
        </div>
        <div className="fund-quarterly-readout-metrics">
          <div><span>Revenue</span><strong>{financialAmount(selectedPoint.revenue)} <small className={'fund-quarterly-yoy ' + revenueYoy.tone}>{revenueYoy.text}</small></strong></div>
          <div><span>Net profit</span><strong>{financialAmount(selectedPoint.profit)} <small className={'fund-quarterly-yoy ' + profitYoy.tone}>{profitYoy.text}</small></strong></div>
        </div>
      </div>
      <PeriodTableViewport
        periodKey={periodFrequency + '|' + points.map((point) => point.key).join('|')}
        label={(annual ? 'Annual' : 'Quarterly') + ' revenue and profit chart; newest period on the right'}
        viewportClassName="fund-quarterly-scroll fund-pivot-wrap"
      >
        <svg
          className="fund-quarterly-chart"
          viewBox={'0 0 ' + chartWidth + ' ' + chartHeight}
          width={chartWidth}
          height={chartHeight}
          role="group"
          aria-label={(annual ? 'Annual' : 'Quarterly') + ' revenue in gray and net profit in green. Older periods appear on the left and newer periods on the right. Amounts in ' + unit + '.'}
        >
          <title>{(annual ? 'Annual' : 'Quarterly') + ' revenue and net profit'}</title>
          <desc>Each period shows revenue and net profit side by side, ordered from oldest to newest.</desc>
          {[maxValue, (maxValue + minValue) / 2, minValue].map((value, index) => {
            const y = top + ((maxValue - value) / valueRange) * plotHeight;
            return (
              <g key={index}>
                <line className="fund-quarterly-gridline" x1={left} x2={chartWidth - right} y1={y} y2={y} />
                <text className="fund-quarterly-axis-label" x={left - 6} y={y + 3} textAnchor="end">{display(value, 0)}</text>
              </g>
            );
          })}
          <line className="fund-quarterly-baseline" x1={left} x2={chartWidth - right} y1={baseline} y2={baseline} />
          {points.map((point, index) => {
            const center = left + step * (index + 0.5);
            const revenueX = center - barWidth;
            const profitX = center;
            const revenueValue = point.revenue ? asNumber(point.revenue.value) : null;
            const profitValue = point.profit ? asNumber(point.profit.value) : null;
            const heading = periodHeading(point.periodEnd, point.label, periodFrequency);
            const isSelected = selectedPoint.key === point.key;
            return (
              <g
                key={point.key}
                className={`fund-quarterly-period-group${isSelected ? ' is-selected' : ''}`}
                role="button"
                tabIndex={0}
                aria-pressed={isSelected}
                aria-label={`${heading.primary}, ${point.label}. Focus or hover to view revenue, net profit, and year-on-year change.`}
                onMouseEnter={() => setSelectedPeriodKey(point.key)}
                onFocus={() => setSelectedPeriodKey(point.key)}
                onClick={() => setSelectedPeriodKey(point.key)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    setSelectedPeriodKey(point.key);
                  }
                }}
              >
                <rect className="fund-quarterly-selection" x={center - step * 0.43} y={top - 4} width={step * 0.86} height={chartHeight - top - 9} rx="6" />
                {bar(revenueValue, revenueX, 'revenue', 'Revenue · ' + heading.primary + ' (' + point.label + ')', point.revenue?.unit || '')}
                {bar(profitValue, profitX, 'profit', 'Net profit · ' + heading.primary + ' (' + point.label + ')', point.profit?.unit || '')}
                <text className="fund-quarterly-period" x={center} y={chartHeight - 27} textAnchor="middle">{heading.primary}</text>
                <text className="fund-quarterly-period-year" x={center} y={chartHeight - 13} textAnchor="middle">{heading.secondary || point.label}</text>
              </g>
            );
          })}
        </svg>
      </PeriodTableViewport>
    </div>
  );
}

function StatementSection({ lines, kind, title }: { lines: FundamentalLine[]; kind: string; title: string }) {
  const [basis, setBasis] = useState<ReportingBasis>('consolidated');
  const [frequency, setFrequency] = useState('quarterly');
  const availableBases: Record<ReportingBasis, boolean> = {
    consolidated: lines.some((line) => normalizeKey(line.statement_kind) === normalizeKey(kind)
      && normalizeKey(line.statement_type) === 'consolidated'),
    standalone: lines.some((line) => normalizeKey(line.statement_kind) === normalizeKey(kind)
      && normalizeKey(line.statement_type) === 'standalone'),
  };
  const activeBasis = availableBases[basis] ? basis
    : availableBases.consolidated ? 'consolidated'
      : availableBases.standalone ? 'standalone'
        : basis;
  const candidateRows = lines.filter((line) => normalizeKey(line.statement_kind) === normalizeKey(kind)
    && normalizeKey(line.statement_type) === activeBasis);
  const frequencies = [...new Set(candidateRows.map((line) => normalizeKey(line.frequency)).filter(Boolean))];
  const activeFrequency = frequencies.includes(frequency)
    ? frequency
    : frequencies.includes('quarterly') ? 'quarterly'
      : frequencies.includes('yearly') ? 'yearly'
        : frequencies[0] ?? '';
  const rows = activeFrequency
    ? candidateRows.filter((line) => normalizeKey(line.frequency) === activeFrequency)
    : candidateRows;

  return (
    <div className="fund-statement-block">
      <div className="fund-statement-controls">
        <div className="fund-control-pair">
          <span>Reporting basis</span>
          <BasisToggle value={activeBasis} available={availableBases} onChange={setBasis} label={title + ' reporting basis'} />
        </div>
        {frequencies.length > 1 && (
          <div className="fund-control-pair">
            <span>Period</span>
            <FrequencyToggle value={activeFrequency} options={frequencies} onChange={setFrequency} label={title + ' period frequency'} />
          </div>
        )}
      </div>
      {kind === 'income_statement' && (
        <RevenueProfitChart
          key={`${activeBasis}|${activeFrequency}`}
          lines={candidateRows}
          frequency={activeFrequency}
        />
      )}
      <StatementTable lines={rows} title={title} scrollKey={`${activeBasis}|${activeFrequency}`} />
      {kind === 'income_statement' && <IncomeGrowthSummary lines={candidateRows} />}
    </div>
  );
}

function holdingCategoryLabel(category: unknown): string {
  const key = normalizeKey(category);
  if (key.includes('promoter')) return 'Promoters';
  if (key === 'fii' || key.includes('foreigninstitution')) return 'Foreign institutions (FII)';
  if (key.includes('mutualfund')) return 'Mutual funds';
  if (key.includes('otherdii')) return 'Other domestic institutions';
  if (key.includes('insurance')) return 'Insurance';
  if (key.includes('retail')) return 'Retail and other';
  if (key.includes('government')) return 'Government';
  if (key.includes('bank')) return 'Banks';
  return titleCase(category);
}

function holdingTone(category: unknown): string {
  const key = normalizeKey(category);
  if (key.includes('promoter')) return 'promoter';
  if (key === 'fii' || key.includes('foreigninstitution')) return 'fii';
  if (key.includes('mutualfund')) return 'mutual';
  if (key.includes('dii') || key.includes('insurance') || key.includes('bank')) return 'dii';
  if (key.includes('retail')) return 'retail';
  return 'other';
}

function holdingChangeSentiment(category: unknown, change: number | null): string {
  if (change == null || change === 0) return '';
  const key = normalizeKey(category);
  const institutional = key.includes('promoter') || key === 'fii'
    || key.includes('foreigninstitution') || key.includes('mutualfund')
    || key.includes('dii') || key.includes('insurance') || key.includes('bank')
    || key.includes('government') || key.includes('institution');
  const dispersed = key.includes('retail') || key === 'other'
    || key.includes('otherinvestor') || key.includes('otherpublic')
    || key.includes('noninstitutional') || key.includes('individual')
    || key === 'public';
  if (institutional) return change > 0 ? 'is-positive' : 'is-negative';
  if (dispersed) return change < 0 ? 'is-positive' : 'is-negative';
  return '';
}

function ShareholdingSection({ records }: { records: FundamentalData['shareholdings'] }) {
  if (records.length === 0) return <EmptyMessage>No shareholding history has been synced yet.</EmptyMessage>;

  const periodMap = new Map<string, { key: string; label: string; periodEnd: string | null }>();
  const categorySet = new Set<string>();
  for (const item of records) {
    const key = periodKey(item.period_end, item.period_label, 'quarterly');
    if (!periodMap.has(key)) {
      periodMap.set(key, {
        key,
        label: String(item.period_label || formatDate(item.period_end)),
        periodEnd: item.period_end == null ? null : String(item.period_end),
      });
    }
    if (item.category != null) categorySet.add(String(item.category));
  }

  const periods = [...periodMap.values()].sort((left, right) => (
    periodTimestamp(left.periodEnd, left.label) - periodTimestamp(right.periodEnd, right.label)
  ));
  const categories = [...categorySet].sort((left, right) => {
    const priority = (category: string) => {
      const key = normalizeKey(category);
      if (key.includes('promoter')) return 0;
      if (key === 'fii' || key.includes('foreigninstitution')) return 1;
      if (key.includes('mutualfund')) return 2;
      if (key.includes('otherdii')) return 3;
      if (key.includes('insurance')) return 4;
      if (key.includes('retail')) return 5;
      return 6;
    };
    return priority(left) - priority(right) || left.localeCompare(right);
  });
  const latest = periods[periods.length - 1];
  const latestHeading = periodHeading(latest.periodEnd, latest.label, 'quarterly');
  const categoryRank = new Map(categories.map((category, index) => [category, index]));
  const latestRecords = records
    .filter((item) => periodKey(item.period_end, item.period_label, 'quarterly') === latest.key)
    .sort((left, right) => (categoryRank.get(String(left.category)) ?? Number.MAX_SAFE_INTEGER)
      - (categoryRank.get(String(right.category)) ?? Number.MAX_SAFE_INTEGER));
  const total = latestRecords.reduce((sum, item) => sum + Math.max(0, asNumber(item.percentage) ?? 0), 0);
  const scaling = total > 100 ? 100 / total : 1;

  return (
    <div className="fund-shareholding">
      <div className="fund-shareholding-summary">
        <div className="fund-shareholding-heading">
          <div><strong>Latest reported ownership</strong><span>{latestHeading.primary}{latestHeading.secondary ? ' · ' + latestHeading.secondary : ''}</span></div>
          <small>{latestRecords.length} investor groups</small>
        </div>
        <div className="fund-owner-stack" role="img" aria-label={'Shareholding for ' + latest.label + ': ' + latestRecords.map((item) => holdingCategoryLabel(item.category) + ' ' + display(item.percentage, 2) + '%').join(', ')}>
          {latestRecords.map((item, index) => {
            const percentage = Math.max(0, asNumber(item.percentage) ?? 0);
            return (
              <span
                key={String(item.category) + '-' + index}
                className={'fund-owner-segment fund-owner-' + holdingTone(item.category)}
                style={{ width: String(percentage * scaling) + '%' }}
                title={holdingCategoryLabel(item.category) + ': ' + display(percentage, 2) + '%'}
              />
            );
          })}
        </div>
        <div className="fund-owner-legend">
          {latestRecords.map((item, index) => {
            const category = String(item.category ?? 'Other');
            const current = asNumber(item.percentage);
            return (
              <div className="fund-owner-card" key={category + '-' + index}>
                <span className={'fund-owner-swatch fund-owner-' + holdingTone(category)} />
                <div className="fund-owner-card-copy">
                  <span>{holdingCategoryLabel(category)}</span>
                  <strong>{current == null ? '—' : display(current, 2) + '%'}</strong>
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {periods.length > 1 && (
        <div className="fund-holding-history">
          <div className="fund-subsection-heading">
            <div><strong>Shareholding history</strong></div>
          </div>
          <PeriodTableViewport
            periodKey={periods.map((period) => period.key).join('|')}
            label="Shareholding history; newest quarter on the right, scroll horizontally for older quarters"
          >
            <table className="fund-table fund-statement-pivot fund-holding-pivot" aria-label="Shareholding history by quarter">
              <thead>
                <tr>
                  <th scope="col">Investor group</th>
                  {periods.map((period) => {
                    const heading = periodHeading(period.periodEnd, period.label, 'quarterly');
                    return (
                      <th key={period.key} scope="col">
                        <span>{heading.primary}</span>
                        {heading.secondary && <small>{heading.secondary}</small>}
                      </th>
                    );
                  })}
                </tr>
              </thead>
              <tbody>
                {categories.map((category) => (
                  <tr key={category}>
                    <th scope="row">
                      <span className={'fund-owner-swatch fund-owner-' + holdingTone(category)} />
                      {holdingCategoryLabel(category)}
                    </th>
                    {periods.map((period, periodIndex) => {
                      const item = records.find((record) => String(record.category) === category
                        && periodKey(record.period_end, record.period_label, 'quarterly') === period.key);
                      const value = asNumber(item?.percentage);
                      const previousPeriod = periodIndex > 0 ? periods[periodIndex - 1] : null;
                      const previousItem = previousPeriod
                        ? records.find((record) => String(record.category) === category
                          && periodKey(record.period_end, record.period_label, 'quarterly') === previousPeriod.key)
                        : null;
                      const previousValue = asNumber(previousItem?.percentage);
                      const change = value == null || previousValue == null ? null : value - previousValue;
                      const sentiment = holdingChangeSentiment(category, change);
                      return (
                        <td
                          key={period.key}
                          className={'fund-holding-cell ' + (sentiment || 'is-neutral')}
                          title={value == null ? 'No holding data reported' : holdingCategoryLabel(category) + ': ' + display(value, 2) + '%'}
                        >
                          {value == null ? '—' : <strong>{display(value, 2)}%</strong>}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </PeriodTableViewport>
        </div>
      )}
    </div>
  );
}

function FundamentalDetails({ payload }: { payload: StockFundamentalsResponse | null }) {
  const data = payload?.fundamentals ?? null;
  const profile = data?.profile;
  const shareholdings = data?.shareholdings ?? [];
  const actions = data?.corporate_actions ?? [];
  const competitors = data?.competitors ?? [];
  const sync = data?.sync_state ?? [];
  const rawLatest = data?.raw_latest ?? [];

  return (
    <div className="fund-details-content">
      <section className="fund-section fund-company-section">
        <div className="fund-section-heading"><h3>Company</h3><span>{payload?.source === 'upstox' ? 'Upstox' : 'Provider'}</span></div>
        <div className="fund-company-grid">
          <div><small>Symbol</small><strong>{formatStockSymbol(payload?.symbol ?? '') || '—'}</strong></div>
          <div><small>ISIN</small><strong>{payload?.isin || '—'}</strong></div>
          <div><small>Sector</small><strong>{display(profile?.sector || payload?.sector)}</strong></div>
          <div><small>Data as of</small><strong>{sectionDate(data)}</strong></div>
          {profile?.sector_market_cap_inr != null && <div><small>Sector market cap (INR)</small><strong>₹{display(profile.sector_market_cap_inr)}</strong></div>}
        </div>
        {profile?.company_profile ? <p className="fund-company-description">{String(profile.company_profile)}</p> : !profile && <EmptyMessage>Upstox company profile has not been synced for this symbol.</EmptyMessage>}
      </section>

      <section className="fund-section">
        <div className="fund-section-heading"><h3>Income statement</h3></div>
        <StatementSection lines={data?.lines ?? []} kind="income_statement" title="Income statement" />
      </section>

      <section className="fund-section">
        <div className="fund-section-heading"><h3>Balance sheet</h3></div>
        <StatementSection lines={data?.lines ?? []} kind="balance_sheet" title="Balance sheet" />
      </section>

      <section className="fund-section">
        <div className="fund-section-heading"><h3>Cash flow</h3></div>
        <StatementSection lines={data?.lines ?? []} kind="cash_flow" title="Cash flow" />
      </section>

      <section className="fund-section">
        <div className="fund-section-heading"><h3>Shareholding pattern</h3></div>
        <ShareholdingSection records={shareholdings} />
      </section>

      <section className="fund-section">
        <div className="fund-section-heading"><h3>Key ratios</h3><span>Company compared with sector</span></div>
        <RatioSection ratios={data?.ratios ?? []} />
      </section>

      <section className="fund-section">
        <div className="fund-section-heading"><h3>Ratio history</h3></div>
        <RatioHistoryTable ratios={data?.ratio_history ?? []} />
      </section>

      <section className="fund-section">
        <div className="fund-section-heading"><h3>Corporate actions</h3><span>{actions.length} records</span></div>
        {actions.length === 0 ? <EmptyMessage>No corporate actions are available yet.</EmptyMessage> : (
          <div className="fund-table-wrap"><table className="fund-table"><thead><tr><th>Action</th><th>Effective</th><th>Record date</th><th>Amount / ratio</th><th>Details</th></tr></thead><tbody>
            {actions.map((item, index) => <tr key={`${item.event_key}-${index}`}><td>{titleCase(item.action_type)}</td><td>{formatDate(item.effective_date)}</td><td>{formatDate(item.record_date)}</td><td>{display(item.amount)}{item.ratio ? ` · ${display(item.ratio)}` : ''}</td><td>{display(item.details)}</td></tr>)}
          </tbody></table></div>
        )}
      </section>

      <section className="fund-section">
        <div className="fund-section-heading"><h3>Sector peers</h3><span>{competitors.length} records</span></div>
        {competitors.length === 0 ? <EmptyMessage>No peer snapshot is available yet.</EmptyMessage> : (
          <div className="fund-table-wrap"><table className="fund-table"><thead><tr><th>Company</th><th>Sector</th><th>P/E</th><th>Market cap</th></tr></thead><tbody>
            {competitors.map((item, index) => {
              const companyName = peerCompanyName(item);
              const marketCap = item.market_cap_cr != null && item.market_cap_cr > 0
                ? item.market_cap_cr
                : item.sector_market_cap_inr;
              return (
                <tr key={`${item.competitor_instrument_key}-${index}`}>
                  <td>{item.symbol && companyName ? <StockIdentity symbol={item.symbol} name={companyName} company_name={companyName} sector={item.sector} market_cap_cr={marketCap} /> : companyName || '—'}</td>
                  <td>{display(item.sector)}</td>
                  <td title={item.pe_snapshot_date ? `Upstox ratio snapshot · ${formatDate(item.pe_snapshot_date)}` : 'Peer P/E from the latest available Upstox ratio snapshot'}>
                    {item.pe_ratio == null || !Number.isFinite(item.pe_ratio) ? '—' : item.pe_ratio > 0 ? `${item.pe_ratio.toFixed(2)}×` : 'N/M'}
                  </td>
                  <td>{marketCap == null ? '—' : `₹${display(marketCap)}`}</td>
                </tr>
              );
            })}
          </tbody></table></div>
        )}
      </section>

      <section className="fund-section">
        <div className="fund-section-heading"><h3>Data freshness</h3><span>Per Upstox feed</span></div>
        {sync.length === 0 ? <EmptyMessage>No sync status has been recorded for this stock.</EmptyMessage> : (
          <div className="fund-table-wrap"><table className="fund-table"><thead><tr><th>Feed</th><th>Status</th><th>Last successful</th><th>Last error</th></tr></thead><tbody>
            {sync.map((item, index) => <tr key={`${item.endpoint}-${index}`}><td>{titleCase(item.endpoint)}</td><td><RatingPill value={titleCase(item.last_status)} /></td><td>{formatDate(item.last_success_at)}</td><td>{display(item.last_error)}</td></tr>)}
          </tbody></table></div>
        )}
      </section>

      <section className="fund-section fund-raw-section">
        <div className="fund-section-heading"><h3>Latest raw Upstox responses</h3><span>{rawLatest.length} feeds</span></div>
        {rawLatest.length === 0 ? <EmptyMessage>No raw Upstox payloads are stored yet.</EmptyMessage> : rawLatest.map((item, index) => (
          <details className="fund-raw-payload" key={`${item.endpoint}-${index}`}>
            <summary>{titleCase(item.endpoint)} <small>as of {formatDate(item.snapshot_date)}</small></summary>
            <pre>{JSON.stringify(item.payload ?? {}, null, 2)}</pre>
          </details>
        ))}
      </section>

    </div>
  );
}

export default function EnhancedMomentumStockModal({ stock, onClose }: { stock: StockModalData; onClose: () => void }) {
  const [fundamentals, setFundamentals] = useState<StockFundamentalsResponse | null>(null);
  const [stockDetail, setStockDetail] = useState<StockDetail | null>(null);
  const [fundamentalsLoading, setFundamentalsLoading] = useState(true);
  const [fundamentalsError, setFundamentalsError] = useState('');
  const [chartResult, setChartResult] = useState<{ key: string; candles: Candle[]; error: string } | null>(null);
  const [range, setRange] = useState(CHART_RANGES[0]);
  const chartKey = `${stock.symbol}:${range.key}:${range.interval}:${range.fetchDays}`;
  const chartLoading = chartResult?.key !== chartKey;
  const candles = useMemo(() => candlesForRange(chartResult?.key === chartKey ? chartResult.candles : [], range), [chartResult, chartKey, range]);
  const chartError = chartResult?.key === chartKey ? chartResult.error : '';

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    window.addEventListener('keydown', handleKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener('keydown', handleKeyDown);
    };
  }, [onClose]);

  useEffect(() => {
    let active = true;
    getStock(stock.symbol, stock.stock_id)
      .then((response) => { if (active) setStockDetail(response); })
      .catch(() => { if (active) setStockDetail(null); });
    return () => { active = false; };
  }, [stock.symbol, stock.stock_id]);

  useEffect(() => {
    let active = true;
    getStockFundamentals(stock.symbol, stock.stock_id)
      .then((response) => { if (active) setFundamentals(response); })
      .catch((error: unknown) => { if (active) setFundamentalsError(error instanceof Error ? error.message : 'Unable to load Upstox fundamentals'); })
      .finally(() => { if (active) setFundamentalsLoading(false); });
    return () => { active = false; };
  }, [stock.symbol, stock.stock_id]);

  useEffect(() => {
    let active = true;
    getCandles(stock.symbol, range.interval, range.fetchDays, stock.stock_id)
      .then((rows) => { if (active) setChartResult({ key: chartKey, candles: rows, error: '' }); })
      .catch((error: unknown) => {
        if (active) setChartResult({ key: chartKey, candles: [], error: error instanceof Error ? error.message : 'Unable to load chart history' });
      });
    return () => { active = false; };
  }, [stock.symbol, stock.stock_id, range, chartKey]);

  const profile = fundamentals?.fundamentals.profile;
  const companySector = String(profile?.sector || stock.sector || stock.industry || stockDetail?.sector || stockDetail?.industry || '');
  const assessment = useMemo(() => assessFundamentals(fundamentals?.fundamentals ?? null, companySector), [fundamentals, companySector]);
  const companyName = getStockCompanyName(stock) || fundamentals?.company_name || fundamentals?.name || stockDetail?.company_name || stockDetail?.name || formatStockSymbol(stock.symbol);
  const chartClose = candles[candles.length - 1]?.close ?? stock.close ?? stockDetail?.latest_candle?.close ?? stockDetail?.latest_price ?? null;
  const close20SessionsAgo = range.interval === '1d' && candles.length > 20 ? candles[candles.length - 21]?.close : null;
  const derivedReturn20d = close20SessionsAgo && chartClose != null && close20SessionsAgo > 0 ? (chartClose / close20SessionsAgo - 1) * 100 : null;
  const return20d = stock.return_20d ?? derivedReturn20d;
  const marketCap = stock.market_cap_cr ?? stockDetail?.market_cap_cr ?? null;
  const asOfDate = stock.as_of_date || stockDetail?.indicators?.date || stockDetail?.latest_candle?.timestamp || profile?.snapshot_date;
  const trendAligned = stock.trend_aligned;
  const technical = stockDetail?.indicators;

  return (
    <div className="enhanced-stock-modal-overlay" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <section className="enhanced-stock-modal" role="dialog" aria-modal="true" aria-labelledby="enhanced-stock-modal-title">
        <header className="enhanced-stock-modal-header">
          <div className="enhanced-stock-modal-title-group">
            <span className="fund-symbol-chip">{formatStockSymbol(stock.symbol)}</span>
            <div><h2 id="enhanced-stock-modal-title">{companyName}</h2><p>{display(companySector)} · {stockDetail?.exchange || 'NSE'} · {formatDate(asOfDate)}</p></div>
          </div>
          <button type="button" className="enhanced-stock-modal-close" onClick={onClose} aria-label="Close stock details" autoFocus>×</button>
        </header>

        <div className="enhanced-stock-modal-body">
          <aside className="enhanced-stock-fundamentals-pane" aria-label="Company fundamentals">
            <div className="fund-score-overview">
              <div className="fund-score-main">
                <span className="fund-score-label">Fundamental quality</span>
                <strong>{assessment.score == null ? '—' : assessment.score}<small>{assessment.score == null ? '' : '/100'}</small></strong>
                <RatingPill value={assessment.rating} />
              </div>
              <div className="fund-score-context">
                <span>{assessment.coverage}% pillars · {assessment.signalCoverage}% input coverage</span>
                <small>{fundamentalsLoading ? 'Loading Upstox data…' : fundamentalsError ? 'Fundamentals unavailable' : `Data as of ${sectionDate(fundamentals?.fundamentals ?? null)}`}</small>
              </div>
            </div>
            <details className="fund-methodology">
              <summary>How the quality score is built</summary>
              <p>The headline score equally weights three quality pillars: profitability and earnings quality, growth and persistence, and financial safety.</p>
              <ul>
                <li>When both are available, profitability blends historical return ratios and annual operating-cash conversion (80%) with a recent earnings score (20%). The recent score weights profitable-quarter consistency at 60% and positive trailing-four-quarter reported PAT at 40%; if either side is unavailable, the available side is used.</li>
                <li>Growth uses annual revenue and net-profit history, including CAGR when enough periods are available and the consistency of yearly changes.</li>
                <li>Safety uses liabilities/assets and operating-cash-flow consistency for non-banks; banks use reported NPA, CASA, and capital-adequacy measures.</li>
                <li>Upstox supplies a sector comparison value, not a peer distribution. Sector-relative ratings are not percentiles; bounded fallback scales are used where no sector benchmark exists.</li>
                <li>Valuation is shown as a separate sector-relative measure. Promoter movement is informational context. Neither is included in the quality score.</li>
              </ul>
              <p>Missing quality factors are excluded from the average. A headline score requires profitability and safety data, at least two of three pillars, and 40% underlying input coverage. These heuristic ratings are not a buy or sell signal.</p>
            </details>
            <div className="fund-score-factor-list">
              {assessment.factors.map((item) => (
                <article className="fund-score-factor" key={item.key}>
                  <div className="fund-factor-topline"><strong>{item.label}</strong><RatingPill value={item.rating} /></div>
                  {item.score != null && <div className="fund-factor-meter"><span style={{ width: `${item.score}%` }} /></div>}
                  <div className="fund-factor-meta">
                    <span>{item.score == null ? item.group === 'context' ? 'Context only' : '—' : `${item.score}/100`}</span>
                    <span>{item.group === 'quality' ? `${Math.round(item.weight)}% of quality` : item.group === 'valuation' ? 'Separate measure' : `${item.inputs.length ? 'Informational only' : 'No holding data'}`}</span>
                  </div>
                  {item.group === 'quality' && <div className="fund-factor-meta"><span>Input coverage</span><span>{item.signalsAvailable}/{item.signalsExpected} checks</span></div>}
                  <p>{item.rationale}</p>
                  {item.inputs.length > 0 && <ul>{item.inputs.map((input) => <li key={input}>{input}</li>)}</ul>}
                </article>
              ))}
            </div>
            {fundamentalsError && <div className="fund-load-error">Could not load Upstox fundamentals: {fundamentalsError}</div>}
            <FundamentalDetails payload={fundamentals} />
            {fundamentalsLoading && <div className="fund-loading-overlay" aria-live="polite">Fetching company fundamentals…</div>}
          </aside>

          <main className="enhanced-stock-chart-pane">
            <div className="enhanced-stock-chart-heading">
              <div><span className="fund-section-eyebrow">Price history</span><strong>{formatMoney(chartClose)}</strong></div>
              <div className="enhanced-stock-range-tabs" aria-label="Chart time range">
                {CHART_RANGES.map((item) => <button key={item.key} type="button" className={range.key === item.key ? 'active' : ''} onClick={() => setRange(item)} aria-pressed={range.key === item.key} title={`${item.description} · ${item.interval} candles`}>{item.label}</button>)}
              </div>
            </div>
            <div className="enhanced-stock-chart">
              {chartLoading ? <div className="fund-chart-state">Loading price history…</div>
                : chartError ? <div className="fund-chart-state">{chartError}</div>
                  : candles.length ? <CandleChart key={`${stock.symbol}-${range.key}`} candles={candles} height={470} />
                    : <div className="fund-chart-state">No chart candles are available for this range.</div>}
            </div>
            <div className="enhanced-stock-chart-footer">
              <span>{range.description} · {candles.length.toLocaleString()} candles</span>
              <span>{stock.enhanced_score == null ? 'Technical view' : <>Scan momentum score <strong>{stock.enhanced_score.toFixed(1)}</strong></>}</span>
            </div>
            <div className="enhanced-stock-momentum-strip">
              <div><small>Market cap</small><strong>{marketCap != null && marketCap > 0 ? `₹${marketCap.toLocaleString('en-IN', { maximumFractionDigits: 0 })} Cr` : '—'}</strong></div>
              <div><small>20 day return</small><strong className={return20d == null ? '' : return20d >= 0 ? 'positive' : 'negative'}>{return20d == null ? '—' : `${return20d >= 0 ? '+' : ''}${return20d.toFixed(1)}%`}</strong></div>
              <div><small>60 day relative strength</small><strong className={stock.market_relative_60d == null ? '' : stock.market_relative_60d >= 0 ? 'positive' : 'negative'}>{stock.market_relative_60d == null ? '—' : `${stock.market_relative_60d >= 0 ? '+' : ''}${stock.market_relative_60d.toFixed(1)}%`}</strong></div>
              <div><small>Breakout / ATH</small><strong>{stock.breakout_55 ? '55D breakout' : stock.breakout_20 ? '20D breakout' : '—'}{stock.at_all_time_high && <span role="img" aria-label="All-time high" title="The latest daily high is the highest recorded daily high in available history">{' '}📈</span>}</strong></div>
              <div><small>Trend</small><strong>{trendAligned == null ? (stockDetail?.indicators?.sma_50 != null && chartClose != null ? chartClose >= stockDetail.indicators.sma_50 ? 'Above 50D SMA' : 'Below 50D SMA' : '—') : trendAligned ? 'Aligned' : 'Mixed'}</strong></div>
            </div>
            <section className="enhanced-stock-technical-snapshot" aria-label="Latest technical indicators">
              <div className="enhanced-stock-technical-heading"><strong>Technical snapshot</strong><small>{technical?.date ? formatDate(technical.date) : 'Latest available'}</small></div>
              {technical ? (
                <div className="enhanced-stock-technical-grid">
                  <div><small>RSI · 14</small><strong>{technical.rsi_14 == null ? '—' : technical.rsi_14.toFixed(1)}</strong></div>
                  <div><small>SMA · 20</small><strong>{formatMoney(technical.sma_20)}</strong></div>
                  <div><small>SMA · 50</small><strong>{formatMoney(technical.sma_50)}</strong></div>
                  <div><small>SMA · 200</small><strong>{formatMoney(technical.sma_200)}</strong></div>
                  <div><small>MACD histogram</small><strong>{technical.macd_histogram == null ? '—' : technical.macd_histogram.toFixed(2)}</strong></div>
                  <div><small>ADX · 14</small><strong>{technical.adx_14 == null ? '—' : technical.adx_14.toFixed(1)}</strong></div>
                </div>
              ) : <p>No technical indicator snapshot is available for this stock.</p>}
            </section>
            <p className="fund-score-disclaimer">Fundamental ratings are a transparent summary of reported Upstox data, not a buy or sell recommendation. Missing or stale inputs can lower coverage.</p>
          </main>
        </div>
      </section>
    </div>
  );
}
