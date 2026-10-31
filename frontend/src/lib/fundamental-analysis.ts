import type { FundamentalData, FundamentalLine, FundamentalRatio, FundamentalRecord } from './api';

export type FundamentalFactorGroup = 'quality' | 'valuation' | 'context';

export interface FundamentalScoreFactor {
  key: string;
  label: string;
  group: FundamentalFactorGroup;
  weight: number;
  score: number | null;
  rating: string;
  rationale: string;
  inputs: string[];
  signalsAvailable: number;
  signalsExpected: number;
}

export interface FundamentalAssessment {
  /** Primary score: operating quality, growth, and financial safety only. */
  score: number | null;
  /** Share of the three quality pillars with enough data to calculate a score. */
  coverage: number;
  /** Share of the expected underlying data checks present across quality pillars. */
  signalCoverage: number;
  rating: string;
  factors: FundamentalScoreFactor[];
}

const QUALITY_WEIGHT = 100 / 3;
const YEAR_MS = 365.25 * 24 * 60 * 60 * 1000;
const PROFITABILITY_RECENCY_WEIGHT = 0.2;

function normalized(value: unknown): string {
  return String(value ?? '').toLowerCase().replace(/[^a-z0-9]/g, '');
}

function numeric(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value !== 'string' || value.trim() === '') return null;
  const cleaned = value.replace(/[,%₹,\s]/g, '');
  if (!cleaned) return null;
  const parsed = Number(cleaned);
  return Number.isFinite(parsed) ? parsed : null;
}

function clamp(value: number, min = 0, max = 100): number {
  return Math.max(min, Math.min(max, value));
}

function scoreRating(score: number | null): string {
  if (score == null) return 'No data';
  if (score >= 80) return 'Strong';
  if (score >= 65) return 'Good';
  if (score >= 45) return 'Mixed';
  return 'Weak';
}

function findRatio(ratios: FundamentalRatio[], aliases: string[]): FundamentalRatio | null {
  const wanted = aliases.map(normalized);
  return ratios.find((item) => {
    const key = normalized(item.ratio_name);
    return numeric(item.company_value) != null
      && wanted.some((alias) => key === alias || key.startsWith(alias));
  }) ?? null;
}

function ratioValue(ratios: FundamentalRatio[], aliases: string[]): number | null {
  return numeric(findRatio(ratios, aliases)?.company_value);
}

function periodOrder(line: FundamentalLine): number {
  const date = line.period_end ? Date.parse(line.period_end) : Number.NaN;
  if (Number.isFinite(date)) return date;
  const match = String(line.period_label ?? '').match(/(19|20)\d{2}/);
  return match ? Number(match[0]) * YEAR_MS : 0;
}

function periodIdentity(line: FundamentalLine): string {
  return String(line.period_end || line.period_label || '');
}

function statementLines(lines: FundamentalLine[], kind: string): FundamentalLine[] {
  return lines.filter((line) => normalized(line.statement_kind) === normalized(kind));
}

function consolidatedFirst(lines: FundamentalLine[]): FundamentalLine[] {
  const consolidated = lines.filter((line) => normalized(line.statement_type) === 'consolidated');
  return consolidated.length > 0 ? consolidated : lines;
}

function annualSeries(
  lines: FundamentalLine[],
  predicate: (metric: string) => boolean,
): FundamentalLine[] {
  const candidates = consolidatedFirst(lines.filter((line) => (
    normalized(line.statement_kind) === 'incomestatement'
    && normalized(line.frequency) === 'yearly'
    && predicate(normalized(line.metric_name))
    && numeric(line.value) != null
  ))).sort((left, right) => periodOrder(right) - periodOrder(left));

  const periods = new Set<string>();
  return candidates.filter((line) => {
    const period = periodIdentity(line);
    if (!period || periods.has(period)) return false;
    periods.add(period);
    return true;
  });
}

function metricGrowthProfile(lines: FundamentalLine[], predicate: (metric: string) => boolean): {
  score: number;
  signalsAvailable: number;
  input: string;
} | null {
  const points = annualSeries(lines, predicate);
  if (points.length === 0) return null;

  const comparableChanges: number[] = [];
  for (let index = 0; index < points.length - 1; index += 1) {
    const current = numeric(points[index].value);
    const previous = numeric(points[index + 1].value);
    if (current == null || previous == null || previous <= 0) continue;
    comparableChanges.push(((current - previous) / previous) * 100);
  }

  const latest = points[0];
  const latestValue = numeric(latest.value);
  const oldestValue = numeric(points[points.length - 1].value);
  const years = points.length > 1
    ? Math.max(1, Math.round((periodOrder(points[0]) - periodOrder(points[points.length - 1])) / YEAR_MS))
    : 0;
  const cagr = points.length >= 2 && latestValue != null && oldestValue != null && latestValue > 0 && oldestValue > 0
    ? (Math.pow(latestValue / oldestValue, 1 / years) - 1) * 100
    : null;
  const reportedChange = numeric(latest.change_pct);
  const growthRate = cagr ?? (comparableChanges.length > 0
    ? comparableChanges.reduce((sum, item) => sum + item, 0) / comparableChanges.length
    : reportedChange);
  if (growthRate == null) return null;

  const rateScore = clamp(50 + growthRate * 2);
  const persistenceSource = comparableChanges.length > 0
    ? comparableChanges
    : reportedChange == null ? [] : [reportedChange];
  const persistence = persistenceSource.length > 0
    ? (persistenceSource.filter((change) => change >= 0).length / persistenceSource.length) * 100
    : null;
  const score = persistence == null ? rateScore : rateScore * 0.7 + persistence * 0.3;
  const periodLabel = String(latest.period_label || latest.period_end || 'latest year');
  const growthLabel = cagr != null
    ? `${years}Y CAGR`
    : comparableChanges.length > 0 ? 'average annual growth' : 'reported annual change';
  const persistenceLabel = persistence == null
    ? ''
    : ` · positive in ${persistenceSource.filter((change) => change >= 0).length}/${persistenceSource.length} comparable years`;

  return {
    score,
    signalsAvailable: 1,
    input: `${growthLabel} ${growthRate >= 0 ? '+' : ''}${growthRate.toFixed(1)}% through ${periodLabel}${persistenceLabel}`,
  };
}

function returnRatioScore(value: number, sector: number | null): number {
  // Upstox gives one sector benchmark, not a peer distribution. A bounded
  // percentage-point comparison is deliberately described as relative, not a percentile.
  return sector == null || sector === 0
    ? clamp(50 + value * 2.5)
    : clamp(50 + (value - sector) * 2);
}

function lowerMultipleScore(value: number, sector: number | null): number | null {
  if (value <= 0) return null;
  if (sector == null || sector <= 0) return null;
  return clamp(50 - 20 * Math.log(value / sector));
}

function returnInput(label: string, value: number, sector: number | null): string {
  return sector == null
    ? `${label} ${value.toFixed(1)}% · no sector benchmark`
    : `${label} ${value.toFixed(1)}% · sector ${sector.toFixed(1)}%`;
}

function makeFactor(
  key: string,
  label: string,
  group: FundamentalFactorGroup,
  score: number | null,
  rationale: string,
  inputs: string[],
  signalsAvailable = 0,
  signalsExpected = 0,
): FundamentalScoreFactor {
  const roundedScore = score == null ? null : Math.round(clamp(score));
  const rating = group === 'context' ? 'Context only' : scoreRating(roundedScore);
  return {
    key,
    label,
    group,
    weight: group === 'quality' ? QUALITY_WEIGHT : 0,
    score: roundedScore,
    rating,
    rationale,
    inputs,
    signalsAvailable,
    signalsExpected,
  };
}

function annualMetricSeries(
  lines: FundamentalLine[],
  kind: string,
  predicate: (metric: string) => boolean,
): FundamentalLine[] {
  const candidates = consolidatedFirst(statementLines(lines, kind).filter((line) => (
    normalized(line.frequency) === 'yearly'
    && predicate(normalized(line.metric_name))
    && numeric(line.value) != null
  ))).sort((left, right) => periodOrder(right) - periodOrder(left));
  const periods = new Set<string>();
  return candidates.filter((line) => {
    const period = periodIdentity(line);
    if (!period || periods.has(period)) return false;
    periods.add(period);
    return true;
  });
}

function samePeriodPair(lines: FundamentalLine[]): { assets: number; liabilities: number } | null {
  const findPair = (balance: FundamentalLine[]): { assets: number; liabilities: number } | null => {
    const assets = balance.filter((line) => /^(totalasset|totalassets)$/.test(normalized(line.metric_name)) && numeric(line.value) != null);
    const liabilities = balance.filter((line) => /^(totalliability|totalliabilities|totalliabilityandshareholdersfunds)$/.test(normalized(line.metric_name)) && numeric(line.value) != null);
    const liabilityPeriods = new Set(liabilities.map(periodIdentity));
    assets.sort((left, right) => periodOrder(right) - periodOrder(left));
    for (const item of assets) {
      const period = periodIdentity(item);
      const matchingLiabilities = liabilities.find((line) => periodIdentity(line) === period);
      const assetValue = numeric(item.value);
      const liabilityValue = numeric(matchingLiabilities?.value);
      if (period && liabilityPeriods.has(period) && assetValue != null && assetValue > 0 && liabilityValue != null && liabilityValue >= 0) {
        return { assets: assetValue, liabilities: liabilityValue };
      }
    }
    return null;
  };
  const allBalance = statementLines(lines, 'balance_sheet');
  const consolidatedPair = findPair(allBalance.filter((line) => normalized(line.statement_type) === 'consolidated'));
  if (consolidatedPair) return consolidatedPair;
  const byType = new Map<string, FundamentalLine[]>();
  for (const line of allBalance) {
    const type = normalized(line.statement_type) || 'unknown';
    byType.set(type, [...(byType.get(type) ?? []), line]);
  }
  for (const statementType of byType.values()) {
    const pair = findPair(statementType);
    if (pair) return pair;
  }
  return null;
}

function operatingCashSeries(lines: FundamentalLine[]): FundamentalLine[] {
  return annualMetricSeries(lines, 'cash_flow', (metric) => (
    /operating.*activit|cash.*operat|operatingcashflow/.test(metric)
  ));
}

function cashConversionScore(lines: FundamentalLine[]): { score: number; input: string } | null {
  const cash = operatingCashSeries(lines);
  const profit = annualMetricSeries(lines, 'income_statement', (metric) => (
    /^(netprofit|netprofitaftertax|profitaftertax|profitaftertaxation|profitfortheperiod|profitfortheyear|pat)$/.test(metric)
  ));
  const profitByPeriod = new Map(profit.map((line) => [periodIdentity(line), numeric(line.value)]));
  const conversions = cash.slice(0, 4)
    .map((line) => ({ line, cashValue: numeric(line.value), profitValue: profitByPeriod.get(periodIdentity(line)) }))
    .filter((item): item is { line: FundamentalLine; cashValue: number; profitValue: number } => (
      item.cashValue != null && item.profitValue != null && item.profitValue > 0
    ));
  if (conversions.length === 0) return null;
  const scores = conversions.map((item) => clamp((item.cashValue / item.profitValue) * 50));
  const average = scores.reduce((sum, item) => sum + item, 0) / scores.length;
  const averageRatio = conversions.reduce((sum, item) => sum + item.cashValue / item.profitValue, 0) / conversions.length;
  return {
    score: average,
    input: `Operating cash flow / net profit ${averageRatio.toFixed(2)}× on ${conversions.length} comparable profitable year${conversions.length === 1 ? '' : 's'}`,
  };
}

function quarterlyEarningsScore(lines: FundamentalLine[]): { score: number; input: string } | null {
  const candidates = consolidatedFirst(statementLines(lines, 'income_statement').filter((line) => (
    normalized(line.frequency) === 'quarterly'
    && /^(netprofit|netprofitaftertax|profitaftertax|profitaftertaxation|profitfortheperiod|profitfortheyear|pat)$/.test(normalized(line.metric_name))
    && numeric(line.value) != null
  ))).sort((left, right) => periodOrder(right) - periodOrder(left));

  const periods = new Set<string>();
  const latestFour = candidates.filter((line) => {
    const period = periodIdentity(line);
    if (!period || periods.has(period)) return false;
    periods.add(period);
    return true;
  }).slice(0, 4);
  if (latestFour.length < 4) return null;

  const quarterValues = latestFour.map((line) => numeric(line.value));
  if (quarterValues.some((value) => value == null)) return null;
  const reportedValues = quarterValues as number[];
  const profitableQuarters = reportedValues.filter((value) => value > 0).length;
  const trailingProfit = reportedValues.reduce((sum, value) => sum + value, 0);
  const profitableQuarterScore = (profitableQuarters / 4) * 100;
  const trailingProfitScore = trailingProfit > 0 ? 100 : 0;
  const score = profitableQuarterScore * 0.6 + trailingProfitScore * 0.4;
  const periodsLabel = latestFour.map((line) => String(line.period_label || line.period_end)).reverse().join(', ');
  const trailingProfitLabel = trailingProfit < 0
    ? `−₹${Math.abs(trailingProfit).toFixed(2)} cr`
    : `₹${trailingProfit.toFixed(2)} cr`;

  return {
    score,
    input: `Latest 4 reported quarters (${periodsLabel}): profitable in ${profitableQuarters}/4; trailing PAT ${trailingProfitLabel}; recent earnings score ${Math.round(score)}/100 (60% quarter consistency, 40% positive TTM PAT)`,
  };
}

function cashFlowConsistency(lines: FundamentalLine[]): { score: number; input: string } | null {
  const cash = operatingCashSeries(lines).slice(0, 4);
  if (cash.length === 0) return null;
  const values = cash.map((line) => ({ line, value: numeric(line.value) })).filter((item): item is { line: FundamentalLine; value: number } => item.value != null);
  if (values.length === 0) return null;
  const positiveCount = values.filter((item) => item.value > 0).length;
  return {
    score: (positiveCount / values.length) * 100,
    input: `Operating cash flow positive in ${positiveCount}/${values.length} latest reported year${values.length === 1 ? '' : 's'}`,
  };
}

function isBankLike(ratios: FundamentalRatio[], sector?: string | null): boolean {
  const bankRatioPresent = [
    ['net npa', 'net non performing asset', 'net non performing assets'],
    ['gross npa', 'gross non performing asset', 'gross non performing assets'],
    ['casa', 'casa ratio'],
    ['nim', 'net interest margin'],
    ['crar', 'capital adequacy ratio'],
  ].some((aliases) => ratioValue(ratios, aliases) != null);
  return bankRatioPresent || normalized(sector).includes('bank');
}

function lowerRiskRatioScore(value: number, sector: number | null): number {
  if (sector != null && sector > 0 && value > 0) return clamp(50 - 20 * Math.log(value / sector));
  // Fallback scale is explicit and bounded because no peer distribution exists.
  return clamp(100 - Math.max(0, value) * 20);
}

function higherFinancialRatioScore(value: number, sector: number | null, fallbackCenter: number): number {
  return sector == null
    ? clamp(50 + (value - fallbackCenter) * 2)
    : clamp(50 + (value - sector) * 2);
}

function promoterHolding(shareholdings: FundamentalRecord[]): { latest: number; change: number | null; period: string } | null {
  const entries = shareholdings.filter((row) => normalized(row.category).includes('promoter') && numeric(row.percentage) != null);
  entries.sort((left, right) => {
    const leftDate = left.period_end ? Date.parse(String(left.period_end)) : periodOrder({ period_label: String(left.period_label ?? '') });
    const rightDate = right.period_end ? Date.parse(String(right.period_end)) : periodOrder({ period_label: String(right.period_label ?? '') });
    return rightDate - leftDate;
  });
  if (entries.length === 0) return null;
  const current = numeric(entries[0].percentage);
  if (current == null) return null;
  const prior = numeric(entries[1]?.percentage);
  return {
    latest: current,
    change: prior == null ? null : current - prior,
    period: String(entries[0].period_label || entries[0].period_end || 'Latest period'),
  };
}

function bankSafetySignals(ratios: FundamentalRatio[]): { scores: number[]; inputs: string[] } {
  const scores: number[] = [];
  const inputs: string[] = [];
  const npaAliases = ['net npa', 'net non performing asset', 'net non performing assets', 'gross npa', 'gross non performing asset', 'gross non performing assets'];
  const npa = findRatio(ratios, npaAliases);
  const npaValue = numeric(npa?.company_value);
  if (npaValue != null) {
    scores.push(lowerRiskRatioScore(npaValue, numeric(npa?.sector_value)));
    inputs.push(`${npa?.ratio_name || 'NPA'} ${npaValue.toFixed(2)}%${numeric(npa?.sector_value) == null ? '' : ` · sector ${numeric(npa?.sector_value)?.toFixed(2)}%`}`);
  }

  const casaAliases = ['casa', 'casa ratio'];
  const casa = findRatio(ratios, casaAliases);
  const casaValue = numeric(casa?.company_value);
  if (casaValue != null) {
    scores.push(higherFinancialRatioScore(casaValue, numeric(casa?.sector_value), 40));
    inputs.push(`${casa?.ratio_name || 'CASA'} ${casaValue.toFixed(1)}%${numeric(casa?.sector_value) == null ? '' : ` · sector ${numeric(casa?.sector_value)?.toFixed(1)}%`}`);
  }

  const capitalAliases = ['crar', 'capital adequacy ratio', 'capital adequacy'];
  const capital = findRatio(ratios, capitalAliases);
  const capitalValue = numeric(capital?.company_value);
  if (capitalValue != null) {
    scores.push(higherFinancialRatioScore(capitalValue, numeric(capital?.sector_value), 12));
    inputs.push(`${capital?.ratio_name || 'Capital adequacy'} ${capitalValue.toFixed(1)}%${numeric(capital?.sector_value) == null ? '' : ` · sector ${numeric(capital?.sector_value)?.toFixed(1)}%`}`);
  }
  return { scores, inputs };
}

function valuationFactor(ratios: FundamentalRatio[]): FundamentalScoreFactor {
  const candidates = [
    { aliases: ['pe', 'p/e', 'price to earnings', 'price earnings ratio'], label: 'P/E' },
    { aliases: ['pb', 'p/b', 'price to book', 'price book ratio'], label: 'P/B' },
    { aliases: ['ev/ebitda', 'enterprise value to ebitda'], label: 'EV/EBITDA' },
  ];
  const scores: number[] = [];
  const inputs: string[] = [];
  for (const candidate of candidates) {
    const ratio = findRatio(ratios, candidate.aliases);
    const value = numeric(ratio?.company_value);
    const sector = numeric(ratio?.sector_value);
    if (value == null) continue;
    if (sector == null || sector <= 0 || value <= 0) {
      inputs.push(`${candidate.label} ${value.toFixed(2)} · no usable sector benchmark`);
      continue;
    }
    scores.push(lowerMultipleScore(value, sector) ?? 50);
    inputs.push(`${candidate.label} ${value.toFixed(2)}× · sector ${sector.toFixed(2)}×`);
  }
  return makeFactor(
    'valuation',
    'Valuation',
    'valuation',
    scores.length > 0 ? scores.reduce((sum, item) => sum + item, 0) / scores.length : null,
    'A separate sector-relative context score from positive P/E, P/B, and EV/EBITDA values. It is not included in quality.',
    inputs,
  );
}

export function assessFundamentals(
  data: Pick<FundamentalData, 'ratios' | 'lines' | 'shareholdings'> | null,
  sector?: string | null,
): FundamentalAssessment {
  const ratios = data?.ratios ?? [];
  const lines = data?.lines ?? [];
  const bankLike = isBankLike(ratios, sector);
  const factors: FundamentalScoreFactor[] = [];

  const roe = findRatio(ratios, ['roe', 'return on equity', 'return on shareholders equity']);
  const roce = findRatio(ratios, ['roce', 'return on capital employed']);
  const roa = findRatio(ratios, ['roa', 'return on assets']);
  const nim = findRatio(ratios, ['nim', 'net interest margin']);
  const returnRatios = bankLike
    ? [roa ?? roe, nim].filter((item): item is FundamentalRatio => item != null)
    : [roce ?? (roa == null ? roe : null), roa].filter((item): item is FundamentalRatio => item != null);
  const profitabilityScores = returnRatios.map((item) => {
    const value = numeric(item.company_value);
    return value == null ? null : returnRatioScore(value, numeric(item.sector_value));
  }).filter((value): value is number => value != null);
  const cashConversion = bankLike ? null : cashConversionScore(lines);
  if (cashConversion) profitabilityScores.push(cashConversion.score);
  const historicalProfitabilityScore = profitabilityScores.length > 0
    ? profitabilityScores.reduce((sum, item) => sum + item, 0) / profitabilityScores.length
    : null;
  const recentEarnings = quarterlyEarningsScore(lines);
  const profitabilityScore = historicalProfitabilityScore != null && recentEarnings
    ? historicalProfitabilityScore * (1 - PROFITABILITY_RECENCY_WEIGHT)
      + recentEarnings.score * PROFITABILITY_RECENCY_WEIGHT
    : historicalProfitabilityScore ?? recentEarnings?.score ?? null;
  const profitabilityInputs = returnRatios.map((item) => {
    const value = numeric(item.company_value);
    return value == null ? '' : returnInput(item.ratio_name || 'Return', value, numeric(item.sector_value));
  }).filter(Boolean);
  if (cashConversion) profitabilityInputs.push(cashConversion.input);
  if (recentEarnings) profitabilityInputs.push(recentEarnings.input);
  const profitabilityExpected = (bankLike ? 2 : 3) + 1;
  factors.push(makeFactor(
    'profitability',
    'Profitability & earnings quality',
    'quality',
    profitabilityScore,
    bankLike
      ? 'Blends return on assets (or ROE when ROA is absent) and net interest margin with recent quarterly reported earnings when four comparable quarters are available. Sector benchmarks are single benchmark comparisons, not peer percentiles.'
      : 'Blends ROCE and ROA (ROE only as a fallback) plus annual operating-cash conversion with recent quarterly reported earnings when four comparable quarters are available. Sector benchmarks are single benchmark comparisons, not peer percentiles.',
    profitabilityInputs,
    profitabilityScores.length + (recentEarnings ? 1 : 0),
    profitabilityExpected,
  ));

  const revenueGrowth = metricGrowthProfile(lines, (metric) => /^(revenue|sales|totalincome|totalrevenue|revenuefromoperations)$/.test(metric));
  const profitGrowth = metricGrowthProfile(lines, (metric) => /^(netprofit|netprofitaftertax|profitaftertax|profitaftertaxation|profitfortheperiod|profitfortheyear|pat)$/.test(metric));
  const growthMetrics = [revenueGrowth, profitGrowth].filter((item): item is NonNullable<typeof item> => item != null);
  const growthInputs = [
    revenueGrowth ? `Revenue: ${revenueGrowth.input}` : '',
    profitGrowth ? `Net profit: ${profitGrowth.input}` : '',
  ].filter(Boolean);
  factors.push(makeFactor(
    'growth',
    'Growth & persistence',
    'quality',
    growthMetrics.length > 0 ? growthMetrics.reduce((sum, item) => sum + item.score, 0) / growthMetrics.length : null,
    'Uses annual revenue and net-profit history, with CAGR where the reported periods support it and consistency across comparable years. Short histories are shown with lower input coverage.',
    growthInputs,
    growthMetrics.length,
    2,
  ));

  let safetyScores: number[] = [];
  let safetyInputs: string[] = [];
  const safetyExpected = bankLike ? 3 : 2;
  if (bankLike) {
    const bankSignals = bankSafetySignals(ratios);
    safetyScores = bankSignals.scores;
    safetyInputs = bankSignals.inputs;
  } else {
    const balance = samePeriodPair(lines);
    if (balance) {
      const ratio = balance.liabilities / balance.assets;
      safetyScores.push(clamp(100 * (1 - ratio)));
      safetyInputs.push(`Total liabilities / assets ${(ratio * 100).toFixed(1)}% · latest matching balance-sheet period`);
    }
    const cashHealth = cashFlowConsistency(lines);
    if (cashHealth) {
      safetyScores.push(cashHealth.score);
      safetyInputs.push(cashHealth.input);
    }
  }
  factors.push(makeFactor(
    'financial-safety',
    'Financial safety',
    'quality',
    safetyScores.length > 0 ? safetyScores.reduce((sum, item) => sum + item, 0) / safetyScores.length : null,
    bankLike
      ? 'Uses reported NPA, CASA, and capital adequacy measures where available. Bank liabilities/assets and operating cash-flow proxies are excluded because they are not comparable bank safety measures.'
      : 'Uses liabilities/assets from a matching balance-sheet period and the share of recent annual periods with positive operating cash flow. Upstox does not provide debt/equity or interest-coverage data in this feed.',
    safetyInputs,
    safetyScores.length,
    safetyExpected,
  ));

  factors.push(valuationFactor(ratios));

  const holding = promoterHolding(data?.shareholdings ?? []);
  const ownershipInputs = holding
    ? [
      `Promoter holding ${holding.latest.toFixed(1)}% (${holding.period})`,
      holding.change == null
        ? 'Only one reported period is available'
        : `${holding.change >= 0 ? '+' : ''}${holding.change.toFixed(2)} percentage points from the prior period`,
    ]
    : [];
  factors.push(makeFactor(
    'ownership-context',
    'Ownership context',
    'context',
    null,
    'Promoter movement is displayed for context only. A single quarter can be noisy, so ownership change is not included in the quality score.',
    ownershipInputs,
    ownershipInputs.length,
    2,
  ));

  const qualityFactors = factors.filter((item) => item.group === 'quality');
  const availableQualityFactors = qualityFactors.filter((item) => item.score != null);
  const coverage = Math.round((availableQualityFactors.length / qualityFactors.length) * 100);
  const expectedSignals = qualityFactors.reduce((sum, item) => sum + item.signalsExpected, 0);
  const availableSignals = qualityFactors.reduce((sum, item) => sum + item.signalsAvailable, 0);
  const signalCoverage = expectedSignals > 0 ? Math.round((availableSignals / expectedSignals) * 100) : 0;
  const profitabilityAvailable = qualityFactors.find((item) => item.key === 'profitability')?.score != null;
  const safetyAvailable = qualityFactors.find((item) => item.key === 'financial-safety')?.score != null;
  const enoughData = profitabilityAvailable && safetyAvailable && coverage >= 67 && signalCoverage >= 40;
  const score = availableQualityFactors.length > 0
    ? Math.round(availableQualityFactors.reduce((sum, item) => sum + (item.score ?? 0), 0) / availableQualityFactors.length)
    : null;

  return {
    score: enoughData ? score : null,
    coverage,
    signalCoverage,
    rating: !enoughData ? 'Insufficient data' : scoreRating(score),
    factors,
  };
}

/** Latest comparable annual net-profit growth and reported ROE for screening. */
export function extractMasterFundamentalMetrics(
  data: Pick<FundamentalData, 'ratios' | 'lines'> | null,
): { netProfitGrowth1yPct: number | null; roePct: number | null } {
  const ratios = data?.ratios ?? [];
  const profitPoints = annualSeries(data?.lines ?? [], (metric) => (
    /^(netprofit|netprofitaftertax|profitaftertax|profitaftertaxation|profitfortheperiod|profitfortheyear|pat)$/.test(metric)
  ));
  let netProfitGrowth1yPct: number | null = null;
  if (profitPoints.length >= 2) {
    const latest = profitPoints[0];
    const latestValue = numeric(latest.value);
    const latestDate = periodOrder(latest);
    const prior = profitPoints.slice(1).find((point) => Math.abs((latestDate - periodOrder(point)) - YEAR_MS) <= 45 * 24 * 60 * 60 * 1000);
    const priorValue = numeric(prior?.value);
    if (latestValue != null && priorValue != null && priorValue > 0) {
      netProfitGrowth1yPct = ((latestValue / priorValue) - 1) * 100;
    }
  }

  return {
    netProfitGrowth1yPct,
    roePct: ratioValue(ratios, ['roe', 'return on equity', 'return on shareholders equity']),
  };
}

export function ratioRating(ratio: FundamentalRatio): string {
  const value = numeric(ratio.company_value);
  if (value == null) return 'No data';
  const key = normalized(ratio.ratio_name);
  const sector = numeric(ratio.sector_value);
  const isReturn = [
    'roe', 'returnonequity', 'returnonshareholdersequity',
    'roce', 'returnoncapitalemployed',
    'roa', 'returnonassets',
  ].some((alias) => key === alias || key.startsWith(alias));
  if (isReturn) {
    if (sector == null || sector === 0) return 'No sector benchmark';
    const score = returnRatioScore(value, sector);
    return score >= 65 ? 'Above sector' : score >= 45 ? 'Near sector' : 'Below sector';
  }

  const isValuation = key === 'pe' || key.startsWith('peratio') || (key.includes('price') && key.includes('earn'))
    || key === 'pb' || key.startsWith('pbratio') || (key.includes('price') && key.includes('book'))
    || (key.includes('ebitda') && (key.includes('ev') || key.includes('enterprisevalue')));
  if (isValuation) {
    if (value <= 0) return 'Not meaningful';
    const score = lowerMultipleScore(value, sector);
    if (score == null) return 'No sector benchmark';
    return score >= 60 ? 'Favorable' : score >= 40 ? 'Fair' : 'Elevated';
  }
  return 'Reported';
}
