import type { Candle } from './types';

export const MASTER_SETUP_DEFINITIONS = [
  { id: 'consolidation', label: 'Tight consolidation · 5+ sessions', shortLabel: 'Tight · 5+D', description: 'Recent 5–9 session range stays within 6%.' },
  { id: 'consolidation_10', label: 'Tight consolidation · 10+ sessions', shortLabel: 'Tight · 10+D', description: 'Recent 10–14 session range stays within 6%.' },
  { id: 'consolidation_15', label: 'Tight consolidation · 15+ sessions', shortLabel: 'Tight · 15+D', description: 'Recent 15+ session range stays within 6%, measured over at most 24 sessions.' },
  { id: 'consolidation_after_rally_2', label: 'Consolidation after rally · 2 sessions', shortLabel: 'Post-rally · 2D', description: 'A 10%+ rise over the preceding 5 sessions, followed immediately by a 2-session range within 8% near the rally high.' },
  { id: 'consolidation_after_rally_3', label: 'Consolidation after rally · 3 sessions', shortLabel: 'Post-rally · 3D', description: 'A 10%+ rise over the preceding 5 sessions, followed immediately by a 3-session range within 8% near the rally high.' },
  { id: 'consolidation_after_rally_5', label: 'Consolidation after rally · 5 sessions', shortLabel: 'Post-rally · 5D', description: 'A 10%+ rise over the preceding 5 sessions, followed immediately by a 4–5 session range within 8% near the rally high.' },
  { id: 'consolidation_after_rally_10', label: 'Consolidation after rally · 10 sessions', shortLabel: 'Post-rally · 10D', description: 'A 10%+ rise over the preceding 5 sessions, followed immediately by a 6–10 session range within 8% near the rally high.' },
  { id: 'consolidation_after_rally_15', label: 'Consolidation after rally · 15 sessions', shortLabel: 'Post-rally · 15D', description: 'A 10%+ rise over the preceding 5 sessions, followed immediately by an 11–15 session range within 8% near the rally high.' },
  { id: 'consolidation_after_rally_20', label: 'Consolidation after rally · 16–20 sessions', shortLabel: 'Post-rally · 16–20D', description: 'A 10%+ rise over the preceding 5 sessions, followed immediately by a 16–20 session range within 8% near the rally high.' },
  { id: 'consolidation_after_rally_20_plus', label: 'Consolidation after rally · 20+ sessions', shortLabel: 'Post-rally · 20+D', description: 'A 10%+ rise over the preceding 5 sessions, followed immediately by at least 21 sessions in a range within 8% near the rally high.' },
  { id: 'consolidation_after_fall_2', label: 'Consolidation after fall · 2 sessions', shortLabel: 'Post-fall · 2D', description: 'A 10%+ fall over the preceding 5 sessions, followed immediately by a 2-session range within 8% near the post-fall low.' },
  { id: 'consolidation_after_fall_3', label: 'Consolidation after fall · 3 sessions', shortLabel: 'Post-fall · 3D', description: 'A 10%+ fall over the preceding 5 sessions, followed immediately by a 3-session range within 8% near the post-fall low.' },
  { id: 'consolidation_after_fall_5', label: 'Consolidation after fall · 5 sessions', shortLabel: 'Post-fall · 5D', description: 'A 10%+ fall over the preceding 5 sessions, followed immediately by a 4–5 session range within 8% near the post-fall low.' },
  { id: 'consolidation_after_fall_10', label: 'Consolidation after fall · 10 sessions', shortLabel: 'Post-fall · 10D', description: 'A 10%+ fall over the preceding 5 sessions, followed immediately by a 6–10 session range within 8% near the post-fall low.' },
  { id: 'consolidation_after_fall_15', label: 'Consolidation after fall · 15 sessions', shortLabel: 'Post-fall · 15D', description: 'A 10%+ fall over the preceding 5 sessions, followed immediately by an 11–15 session range within 8% near the post-fall low.' },
  { id: 'consolidation_after_fall_20', label: 'Consolidation after fall · 16–20 sessions', shortLabel: 'Post-fall · 16–20D', description: 'A 10%+ fall over the preceding 5 sessions, followed immediately by a 16–20 session range within 8% near the post-fall low.' },
  { id: 'consolidation_after_fall_20_plus', label: 'Consolidation after fall · 20+ sessions', shortLabel: 'Post-fall · 20+D', description: 'A 10%+ fall over the preceding 5 sessions, followed immediately by at least 21 sessions in a range within 8% near the post-fall low.' },
  { id: 'breakout', label: '20-day high breakout', shortLabel: '20D breakout', description: 'Latest close clears the prior 20-session high.' },
  { id: 'uptrend_pullback', label: 'Pullback in an uptrend', shortLabel: 'Uptrend pullback', description: 'Price is above a rising 20/50-session trend after a measured pullback.' },
  { id: 'base_pullback_surge', label: 'Base → shallow pullback → sharp surge', shortLabel: 'Base + surge', description: 'A compact base is followed by a 2–7% dip and a 4.5%+ close near the high.' },
] as const;

const POST_RALLY_LOOKBACK_SESSIONS = 5;
const POST_RALLY_MINIMUM_ADVANCE_PCT = 10;
const POST_FALL_LOOKBACK_SESSIONS = 5;
const POST_FALL_MINIMUM_DECLINE_PCT = 10;

export type MasterSetupId = (typeof MASTER_SETUP_DEFINITIONS)[number]['id'];

export interface MasterSetupSignal {
  matched: boolean;
  reason: string;
  signalTime: number | null;
  durationSessions?: number;
  impulsePct?: number;
}

export interface MasterSetupSignals extends Record<MasterSetupId, MasterSetupSignal> {
  candleCount: number;
}

export type MasterPreBreakoutStatus =
  | 'near-trigger'
  | 'building'
  | 'developing'
  | 'breakout-confirmed'
  | 'not-ready'
  | 'insufficient-history'
  | 'unmapped';

export interface MasterPreBreakoutAnalysis {
  historyReady: boolean;
  coreScore: number;
  baseScore: number;
  proximityScore: number;
  trendScore: number;
  volatilityScore: number;
  volumeScore: number;
  pivotPrice: number | null;
  triggerPrice: number | null;
  invalidationPrice: number | null;
  twoRTargetPrice: number | null;
  distanceToPivotPct: number | null;
  baseRangePct: number | null;
  baseSessions: number | null;
  atrPct: number | null;
  volatilityContraction: number | null;
  volumeDryUp: number | null;
  breakoutAlreadyClosed: boolean;
}

export interface MasterPreBreakoutSignal extends MasterPreBreakoutAnalysis {
  status: MasterPreBreakoutStatus;
  score: number | null;
  relativeStrengthScore: number | null;
  relativePerformancePct: number | null;
}

const NO_MATCH = 'No matching pattern in the available daily history.';

function percentChange(value: number, base: number): number | null {
  if (!Number.isFinite(value) || !Number.isFinite(base) || base <= 0) return null;
  return ((value / base) - 1) * 100;
}

function rangePct(candles: Candle[]): number | null {
  if (candles.length === 0) return null;
  const high = Math.max(...candles.map((candle) => candle.high));
  const low = Math.min(...candles.map((candle) => candle.low));
  return low > 0 ? ((high / low) - 1) * 100 : null;
}

function mean(values: number[]): number | null {
  const usable = values.filter((value) => Number.isFinite(value));
  return usable.length ? usable.reduce((sum, value) => sum + value, 0) / usable.length : null;
}

function rangeQualityScore(widthPct: number): number {
  if (widthPct <= 5) return 20;
  if (widthPct <= 7) return 18;
  if (widthPct <= 9) return 15;
  if (widthPct <= 11) return 11;
  if (widthPct <= 14) return 6;
  return 0;
}

function pivotProximityScore(distancePct: number): number {
  if (distancePct < 0) {
    const extensionPct = Math.abs(distancePct);
    return extensionPct <= 0.5 ? 18 : extensionPct <= 1.5 ? 10 : 0;
  }
  if (distancePct <= 1) return 25;
  if (distancePct <= 2.5) return 22;
  if (distancePct <= 4) return 18;
  if (distancePct <= 6) return 12;
  if (distancePct <= 8) return 5;
  return 0;
}

function volatilityContractionScore(ratio: number | null): number {
  if (ratio == null) return 7;
  if (ratio <= 0.65) return 15;
  if (ratio <= 0.8) return 12;
  if (ratio <= 0.95) return 8;
  if (ratio <= 1.1) return 4;
  return 0;
}

function relativeStrengthPoints(relativePerformancePct: number | null): number {
  if (relativePerformancePct == null) return 5;
  if (relativePerformancePct >= 8) return 10;
  if (relativePerformancePct >= 4) return 8;
  if (relativePerformancePct >= 0) return 6;
  if (relativePerformancePct >= -3) return 3;
  return 0;
}

/**
 * Score a completed daily chart for a tight base approaching resistance.
 * The score describes setup readiness; it is not a probability of success.
 */
export function analyzeMasterPreBreakout(source: Candle[]): MasterPreBreakoutAnalysis {
  const byTime = new Map<number, Candle>();
  for (const candle of source) {
    if (
      Number.isFinite(candle.time)
      && Number.isFinite(candle.open)
      && Number.isFinite(candle.high)
      && Number.isFinite(candle.low)
      && Number.isFinite(candle.close)
      && Number.isFinite(candle.volume)
      && candle.low > 0
      && candle.high >= candle.low
      && candle.close > 0
    ) byTime.set(candle.time, candle);
  }
  const candles = [...byTime.values()].sort((left, right) => left.time - right.time);
  const insufficient: MasterPreBreakoutAnalysis = {
    historyReady: false,
    coreScore: 0,
    baseScore: 0,
    proximityScore: 0,
    trendScore: 0,
    volatilityScore: 0,
    volumeScore: 0,
    pivotPrice: null,
    triggerPrice: null,
    invalidationPrice: null,
    twoRTargetPrice: null,
    distanceToPivotPct: null,
    baseRangePct: null,
    baseSessions: null,
    atrPct: null,
    volatilityContraction: null,
    volumeDryUp: null,
    breakoutAlreadyClosed: false,
  };
  if (candles.length < 55) return insufficient;

  const latest = candles[candles.length - 1];
  const baseOptions = [8, 10, 12, 15, 20]
    .filter((length) => candles.length > length)
    .map((length) => {
      const base = candles.slice(-length - 1, -1);
      const high = Math.max(...base.map((candle) => candle.high));
      const low = Math.min(...base.map((candle) => candle.low));
      const widthPct = low > 0 ? ((high / low) - 1) * 100 : Number.POSITIVE_INFINITY;
      const distancePct = high > 0 ? ((high - latest.close) / high) * 100 : Number.POSITIVE_INFINITY;
      const baseScore = rangeQualityScore(widthPct);
      const proximityScore = pivotProximityScore(distancePct);
      return { length, high, low, widthPct, distancePct, baseScore, proximityScore };
    });
  const base = baseOptions.sort((left, right) => (
    (right.baseScore + right.proximityScore) - (left.baseScore + left.proximityScore)
    || right.length - left.length
  ))[0];
  if (!base || !Number.isFinite(base.high) || base.high <= 0) return insufficient;

  const trueRanges: number[] = [];
  const trueRangePcts: number[] = [];
  for (let index = 0; index < candles.length; index += 1) {
    const candle = candles[index];
    const previousClose = index > 0 ? candles[index - 1].close : candle.close;
    const trueRange = Math.max(
      candle.high - candle.low,
      Math.abs(candle.high - previousClose),
      Math.abs(candle.low - previousClose),
    );
    trueRanges.push(trueRange);
    trueRangePcts.push(previousClose > 0 ? (trueRange / previousClose) * 100 : 0);
  }

  const atr14 = mean(trueRanges.slice(-14));
  const atrPct = mean(trueRangePcts.slice(-14));
  const recentVolatility = mean(trueRangePcts.slice(-10));
  const precedingVolatility = mean(trueRangePcts.slice(-40, -10));
  const volatilityContraction = (
    recentVolatility != null && precedingVolatility != null && precedingVolatility > 0
  ) ? recentVolatility / precedingVolatility : null;
  const volatilityScore = volatilityContractionScore(volatilityContraction);

  const sma20 = mean(candles.slice(-20).map((candle) => candle.close)) ?? 0;
  const sma50 = mean(candles.slice(-50).map((candle) => candle.close)) ?? 0;
  const priorSma20 = mean(candles.slice(-25, -5).map((candle) => candle.close)) ?? 0;
  const risingSma20 = sma20 > priorSma20;
  const trendScore = latest.close > sma20 && sma20 > sma50 && risingSma20
    ? 20
    : latest.close > sma50 && sma20 > sma50
      ? 15
      : latest.close > sma50
        ? 9
        : latest.close > sma20 && risingSma20
          ? 8
          : 0;

  const baseStart = candles.length - base.length - 1;
  const baseVolumes = candles.slice(baseStart, candles.length - 1).map((candle) => candle.volume);
  const precedingBaseVolumes = candles.slice(Math.max(0, baseStart - 20), baseStart).map((candle) => candle.volume);
  const baseVolume = mean(baseVolumes);
  const precedingVolume = mean(precedingBaseVolumes);
  const volumeDryUp = baseVolume != null && precedingVolume != null && precedingVolume > 0
    ? baseVolume / precedingVolume
    : null;
  const dryUpPoints = volumeDryUp == null
    ? 3
    : volumeDryUp <= 0.5 ? 6
      : volumeDryUp <= 0.7 ? 5
        : volumeDryUp <= 0.9 ? 4
          : volumeDryUp <= 1.1 ? 3
            : volumeDryUp <= 1.3 ? 1 : 0;
  const prior20Volume = mean(candles.slice(-21, -1).map((candle) => candle.volume));
  const volumeExpansion = prior20Volume != null && prior20Volume > 0 ? latest.volume / prior20Volume : 0;
  const closeLocation = latest.high > latest.low ? (latest.close - latest.low) / (latest.high - latest.low) : 0.5;
  const demandPoints = latest.close > latest.open && closeLocation >= 0.65
    ? volumeExpansion >= 1.5 ? 4 : volumeExpansion >= 1.2 ? 3 : 2
    : 0;
  const volumeScore = Math.min(10, dryUpPoints + demandPoints);

  const triggerPrice = atr14 != null ? base.high + atr14 * 0.1 : base.high;
  const invalidationPrice = atr14 != null ? Math.max(0, base.low - atr14 * 0.25) : base.low;
  const riskPerShare = triggerPrice - invalidationPrice;
  const twoRTargetPrice = riskPerShare > 0 ? triggerPrice + riskPerShare * 2 : null;

  const baseScore = base.baseScore;
  const proximityScore = base.proximityScore;
  const coreScore = baseScore + proximityScore + trendScore + volatilityScore + volumeScore;
  return {
    historyReady: true,
    coreScore,
    baseScore,
    proximityScore,
    trendScore,
    volatilityScore,
    volumeScore,
    pivotPrice: base.high,
    triggerPrice,
    invalidationPrice,
    twoRTargetPrice,
    distanceToPivotPct: base.distancePct,
    baseRangePct: base.widthPct,
    baseSessions: base.length,
    atrPct,
    volatilityContraction,
    volumeDryUp,
    breakoutAlreadyClosed: latest.close > base.high,
  };
}

export function scoreMasterPreBreakout(
  analysis: MasterPreBreakoutAnalysis | undefined,
  relativePerformancePct: number | null | undefined,
): MasterPreBreakoutSignal {
  const empty: MasterPreBreakoutAnalysis = analysis ?? {
    historyReady: false,
    coreScore: 0,
    baseScore: 0,
    proximityScore: 0,
    trendScore: 0,
    volatilityScore: 0,
    volumeScore: 0,
    pivotPrice: null,
    triggerPrice: null,
    invalidationPrice: null,
    twoRTargetPrice: null,
    distanceToPivotPct: null,
    baseRangePct: null,
    baseSessions: null,
    atrPct: null,
    volatilityContraction: null,
    volumeDryUp: null,
    breakoutAlreadyClosed: false,
  };
  if (!empty.historyReady) {
    return {
      ...empty,
      status: 'insufficient-history',
      score: null,
      relativeStrengthScore: null,
      relativePerformancePct: null,
    };
  }

  const relative = relativePerformancePct != null && Number.isFinite(relativePerformancePct)
    ? relativePerformancePct
    : null;
  const relativeStrengthScore = relativeStrengthPoints(relative);
  const score = Math.max(0, Math.min(100, Math.round(empty.coreScore + relativeStrengthScore)));
  const distance = empty.distanceToPivotPct ?? Number.POSITIVE_INFINITY;
  const hasTightBase = (empty.baseRangePct ?? Number.POSITIVE_INFINITY) <= 14;
  const status: MasterPreBreakoutStatus = empty.breakoutAlreadyClosed
    ? 'breakout-confirmed'
    : hasTightBase && empty.trendScore >= 15 && distance >= 0 && distance <= 3 && score >= 70
      ? 'near-trigger'
      : hasTightBase && empty.trendScore >= 9 && distance >= 0 && distance <= 6 && score >= 55
        ? 'building'
        : hasTightBase && empty.trendScore >= 9 && distance >= 0 && distance <= 8 && score >= 40
          ? 'developing'
          : 'not-ready';
  return {
    ...empty,
    status,
    score,
    relativeStrengthScore,
    relativePerformancePct: relative,
  };
}

function dateLabel(time: number): string {
  return new Date(time * 1000).toLocaleDateString('en-IN', { day: 'numeric', month: 'short' });
}

function postMoveSetupId(move: 'rally' | 'fall', consolidationLength: number): MasterSetupId {
  const duration = consolidationLength === 2
    ? '2'
    : consolidationLength === 3
      ? '3'
      : consolidationLength <= 5
        ? '5'
        : consolidationLength <= 10
          ? '10'
          : consolidationLength <= 15
            ? '15'
            : consolidationLength <= 20
              ? '20'
              : '20_plus';
  return `consolidation_after_${move}_${duration}` as MasterSetupId;
}

function noMatches(candleCount = 0): MasterSetupSignals {
  const reason = candleCount < 3 ? 'Insufficient daily history to evaluate this setup.' : NO_MATCH;
  return Object.assign(Object.fromEntries(MASTER_SETUP_DEFINITIONS.map(({ id }) => [id, {
    matched: false,
    reason,
    signalTime: null,
  }])) as Record<MasterSetupId, MasterSetupSignal>, { candleCount });
}

export function detectMasterSetups(source: Candle[]): MasterSetupSignals {
  const byTime = new Map<number, Candle>();
  for (const candle of source) {
    if (
      Number.isFinite(candle.time)
      && Number.isFinite(candle.open)
      && Number.isFinite(candle.high)
      && Number.isFinite(candle.low)
      && Number.isFinite(candle.close)
      && candle.low > 0
      && candle.high >= candle.low
      && candle.close > 0
    ) byTime.set(candle.time, candle);
  }
  const candles = [...byTime.values()].sort((left, right) => left.time - right.time);
  const signals = noMatches(candles.length);
  if (candles.length < 3) return signals;

  const latestIndex = candles.length - 1;
  const latest = candles[latestIndex];

  // Assign the longest qualifying tight range to one non-overlapping duration band.
  for (let length = Math.min(24, candles.length); length >= 5; length -= 1) {
    const window = candles.slice(-length);
    const width = rangePct(window);
    if (width != null && width <= 6) {
      const setupId: MasterSetupId = length <= 9
        ? 'consolidation'
        : length <= 14
          ? 'consolidation_10'
          : 'consolidation_15';
      signals[setupId] = {
        matched: true,
        reason: `${length} sessions compressed into a ${width.toFixed(1)}% range through ${dateLabel(latest.time)}.`,
        signalTime: latest.time,
      };
      break;
    }
  }

  // Require a 10%+ close-to-close gain across the five sessions immediately
  // before the current consolidation. The two pattern windows touch directly.
  const longestPossibleConsolidation = candles.length - POST_RALLY_LOOKBACK_SESSIONS - 1;
  for (let consolidationLength = longestPossibleConsolidation; consolidationLength >= 2; consolidationLength -= 1) {
    const consolidationStart = candles.length - consolidationLength;
    const consolidation = candles.slice(consolidationStart);
    const width = rangePct(consolidation);
    if (width == null || width > 8) continue;

    const rallyEndIndex = consolidationStart - 1;
    const rallyEnd = candles[rallyEndIndex];

    const rallyStartIndex = rallyEndIndex - POST_RALLY_LOOKBACK_SESSIONS;
    if (rallyStartIndex < 0) continue;
    const rallyStart = candles[rallyStartIndex];
    const rallyPct = percentChange(rallyEnd.close, rallyStart.close);
    const rally = candles.slice(rallyStartIndex + 1, rallyEndIndex + 1);
    const rallyHigh = Math.max(...rally.map((candle) => candle.high));
    const drawdownFromHigh = percentChange(latest.close, rallyHigh);
    if (rallyPct == null || rallyPct < POST_RALLY_MINIMUM_ADVANCE_PCT || drawdownFromHigh == null || drawdownFromHigh < -10) continue;
    const rallyHighDistance = drawdownFromHigh >= 0
      ? `${drawdownFromHigh.toFixed(1)}% above`
      : `${Math.abs(drawdownFromHigh).toFixed(1)}% below`;
    const setupId = postMoveSetupId('rally', consolidationLength);

    signals[setupId] = {
      matched: true,
      reason: `${POST_RALLY_LOOKBACK_SESSIONS}-session advance of ${rallyPct.toFixed(1)}% through ${dateLabel(rallyEnd.time)}, followed immediately by ${consolidationLength} sessions of consolidation in a ${width.toFixed(1)}% range; latest close is ${rallyHighDistance} the rally high.`,
      signalTime: latest.time,
      durationSessions: consolidationLength,
      impulsePct: rallyPct,
    };
    if (MASTER_SETUP_DEFINITIONS.some(({ id }) => id.startsWith('consolidation_after_rally_') && signals[id as MasterSetupId].matched)) break;
  }

  // Identify a 10%+ fall followed immediately by consolidation near its low.
  const longestPossibleFallConsolidation = candles.length - POST_FALL_LOOKBACK_SESSIONS - 1;
  for (
    let consolidationLength = longestPossibleFallConsolidation;
    consolidationLength >= 2;
    consolidationLength -= 1
  ) {
    const consolidationStart = candles.length - consolidationLength;
    const consolidation = candles.slice(consolidationStart);
    const width = rangePct(consolidation);
    if (width == null || width > 8) continue;

    const fallEndIndex = consolidationStart - 1;
    const fallStartIndex = fallEndIndex - POST_FALL_LOOKBACK_SESSIONS;
    if (fallStartIndex < 0) continue;
    const fallStart = candles[fallStartIndex];
    const fallEnd = candles[fallEndIndex];
    const fallPct = percentChange(fallEnd.close, fallStart.close);
    const fallingLeg = candles.slice(fallStartIndex + 1, fallEndIndex + 1);
    const fallLow = Math.min(...fallingLeg.map((candle) => candle.low));
    const moveFromFallLow = percentChange(latest.close, fallLow);
    if (
      fallPct == null
      || fallPct > -POST_FALL_MINIMUM_DECLINE_PCT
      || moveFromFallLow == null
      || Math.abs(moveFromFallLow) > 10
    ) continue;

    const setupId = postMoveSetupId('fall', consolidationLength);
    signals[setupId] = {
      matched: true,
      reason: `${POST_FALL_LOOKBACK_SESSIONS}-session fall of ${Math.abs(fallPct).toFixed(1)}% through ${dateLabel(fallEnd.time)}, followed immediately by ${consolidationLength} sessions of consolidation in a ${width.toFixed(1)}% range near the post-fall low.`,
      signalTime: latest.time,
      durationSessions: consolidationLength,
      impulsePct: Math.abs(fallPct),
    };
    break;
  }

  const matchedRallySetupId = MASTER_SETUP_DEFINITIONS.find(({ id }) => (
    id.startsWith('consolidation_after_rally_') && signals[id].matched
  ))?.id;
  const matchedFallSetupId = MASTER_SETUP_DEFINITIONS.find(({ id }) => (
    id.startsWith('consolidation_after_fall_') && signals[id].matched
  ))?.id;
  if (matchedRallySetupId && matchedFallSetupId) {
    const rallySignal = signals[matchedRallySetupId];
    const fallSignal = signals[matchedFallSetupId];
    const rallyDuration = rallySignal.durationSessions ?? 0;
    const fallDuration = fallSignal.durationSessions ?? 0;
    const keepRally = rallyDuration > fallDuration
      || (rallyDuration === fallDuration && (rallySignal.impulsePct ?? 0) >= (fallSignal.impulsePct ?? 0));
    if (keepRally) {
      signals[matchedFallSetupId] = {
        matched: false,
        reason: 'Classified as consolidation after rally.',
        signalTime: null,
      };
    } else {
      signals[matchedRallySetupId] = {
        matched: false,
        reason: 'Classified as consolidation after fall.',
        signalTime: null,
      };
    }
  }

  // Keep post-move bases in their own categories rather than duplicating them
  // as ordinary tight consolidations.
  const hasPostMoveConsolidation = MASTER_SETUP_DEFINITIONS.some(({ id }) => (
    id.startsWith('consolidation_after_rally_') || id.startsWith('consolidation_after_fall_')
  ) && signals[id].matched);
  if (hasPostMoveConsolidation) {
    for (const id of ['consolidation', 'consolidation_10', 'consolidation_15'] as const) {
      if (!signals[id].matched) continue;
      signals[id] = {
        matched: false,
        reason: 'Classified as consolidation after a price move.',
        signalTime: null,
      };
    }
  }

  // Breakout: the current close must clear the highest high in the previous 20 sessions.
  if (candles.length >= 21) {
    const prior = candles.slice(-21, -1);
    const priorHigh = Math.max(...prior.map((candle) => candle.high));
    if (latest.close > priorHigh) {
      const volumeBase = prior.reduce((sum, candle) => sum + candle.volume, 0) / prior.length;
      const volumeNote = volumeBase > 0 && latest.volume >= volumeBase * 1.5 ? ' with 1.5× average volume' : '';
      signals.breakout = {
        matched: true,
        reason: `Close cleared the prior 20-session high${volumeNote}.`,
        signalTime: latest.time,
      };
    }
  }

  // Uptrend pullback: require enough observations for both moving averages.
  if (candles.length >= 55) {
    const sma20Bars = candles.slice(-20);
    const sma50Bars = candles.slice(-50);
    const priorSma20Bars = candles.slice(-25, -5);
    const priorSma50Bars = candles.slice(-55, -5);
    const sma20 = sma20Bars.reduce((sum, candle) => sum + candle.close, 0) / sma20Bars.length;
    const sma50 = sma50Bars.reduce((sum, candle) => sum + candle.close, 0) / sma50Bars.length;
    const priorSma20 = priorSma20Bars.reduce((sum, candle) => sum + candle.close, 0) / priorSma20Bars.length;
    const priorSma50 = priorSma50Bars.reduce((sum, candle) => sum + candle.close, 0) / priorSma50Bars.length;
    const recentHigh = Math.max(...candles.slice(-11, -1).map((candle) => candle.high));
    const pullback = percentChange(latest.close, recentHigh);
    if (sma20 > sma50 && sma20 > priorSma20 && sma50 > priorSma50 && latest.close > sma20 && pullback != null && pullback <= -2 && pullback >= -10) {
      signals.uptrend_pullback = {
        matched: true,
      reason: `Price is above rising 20/50-session averages after a ${Math.abs(pullback).toFixed(1)}% pullback from a recent high.`,
        signalTime: latest.time,
      };
    }
  }

  // The user-described sequence: a narrow base, a shallow down day, then a circuit-like surge.
  // This is a price-action heuristic; the available candle API does not expose exchange circuit bands.
  for (let surgeIndex = latestIndex; surgeIndex >= Math.max(3, latestIndex - 4); surgeIndex -= 1) {
    const surge = candles[surgeIndex];
    const previous = candles[surgeIndex - 1];
    const surgePct = percentChange(surge.close, previous.close);
    const nearHigh = surge.high > 0 && surge.close >= surge.high * 0.985;
    if (surgePct == null || surgePct < 4.5 || !nearHigh) continue;

    let foundSequence = false;
    for (let dipIndex = surgeIndex - 1; dipIndex >= Math.max(2, surgeIndex - 3) && !foundSequence; dipIndex -= 1) {
      const dip = candles[dipIndex];
      const priorClose = candles[dipIndex - 1].close;
      const dipPct = percentChange(dip.close, priorClose);
      if (dipPct == null || dipPct > -2 || dipPct < -7) continue;

      for (let baseLength = 2; baseLength <= 5; baseLength += 1) {
        const baseStart = dipIndex - baseLength;
        if (baseStart < 0) continue;
        const base = candles.slice(baseStart, dipIndex);
        const width = rangePct(base);
        if (width == null || width > 6) continue;
        signals.base_pullback_surge = {
          matched: true,
          reason: `${dateLabel(base[0].time)}–${dateLabel(base[base.length - 1].time)} base, ${Math.abs(dipPct).toFixed(1)}% dip, then +${surgePct.toFixed(1)}% close near the high on ${dateLabel(surge.time)}.`,
          signalTime: surge.time,
        };
        foundSequence = true;
        break;
      }
    }
    if (foundSequence) break;
  }

  return signals;
}
