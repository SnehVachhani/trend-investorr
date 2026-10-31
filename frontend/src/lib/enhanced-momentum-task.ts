import {
  scanEnhancedMomentumUniverse,
  type EnhancedMomentumScanRequest,
  type EnhancedMomentumScanResponse,
} from './api';
import { readPersistentValue, writePersistentValue } from './persistence';

export const ENHANCED_MOMENTUM_TASK_KEY = 'workspace.enhancedMomentum.scanTask';
export const ENHANCED_MOMENTUM_SCAN_TIMINGS_KEY = 'workspace.enhancedMomentum.scanTimings';
const ENHANCED_MOMENTUM_SCAN_KEY = 'workspace.enhancedMomentum.scan';
const MAX_SAVED_SCAN_TIMINGS = 10;

export interface EnhancedMomentumScanTiming {
  taskId: string;
  durationMs: number;
}

export const DEFAULT_ENHANCED_MOMENTUM_FILTERS: EnhancedMomentumScanRequest = {
  top_n: null,
  min_price: 20,
  min_avg_turnover_cr: 1,
  min_score: 55,
  min_volume_ratio: 1.2,
  min_return_20d: 0,
  min_return_60d: 0,
  min_history: 260,
  trend_mode: 'aligned',
  market_mode: 'all',
  require_breakout: false,
};

export interface EnhancedMomentumScanTask {
  id: string;
  status: 'running' | 'success' | 'error';
  request: EnhancedMomentumScanRequest;
  startedAt: number;
  finishedAt?: number;
  error?: string;
}

let activeTaskId: string | null = null;
let activePromise: Promise<EnhancedMomentumScanResponse> | null = null;

function createTaskId() {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID();
  return `enhanced-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : 'Unable to run enhanced momentum scan';
}

function recordScanTiming(taskId: string, durationMs: number) {
  const saved = readPersistentValue<EnhancedMomentumScanTiming[]>(ENHANCED_MOMENTUM_SCAN_TIMINGS_KEY, []);
  const timings = Array.isArray(saved)
    ? saved.filter((timing) => timing && typeof timing.taskId === 'string' && Number.isFinite(timing.durationMs) && timing.durationMs >= 0)
    : [];
  if (timings.some((timing) => timing.taskId === taskId)) return;
  writePersistentValue<EnhancedMomentumScanTiming[]>(ENHANCED_MOMENTUM_SCAN_TIMINGS_KEY, [
    ...timings,
    { taskId, durationMs },
  ].slice(-MAX_SAVED_SCAN_TIMINGS));
}

function executeTask(task: EnhancedMomentumScanTask): Promise<EnhancedMomentumScanResponse> {
  if (activePromise && activeTaskId === task.id) return activePromise;

  activeTaskId = task.id;
  activePromise = scanEnhancedMomentumUniverse(task.request)
    .then((result) => {
      const finishedAt = Date.now();
      writePersistentValue(ENHANCED_MOMENTUM_SCAN_KEY, result);
      recordScanTiming(task.id, Math.max(0, finishedAt - task.startedAt));
      writePersistentValue<EnhancedMomentumScanTask>(ENHANCED_MOMENTUM_TASK_KEY, {
        ...task,
        status: 'success',
        finishedAt,
        error: undefined,
      });
      return result;
    })
    .catch((error: unknown) => {
      writePersistentValue<EnhancedMomentumScanTask>(ENHANCED_MOMENTUM_TASK_KEY, {
        ...task,
        status: 'error',
        finishedAt: Date.now(),
        error: errorMessage(error),
      });
      throw error;
    })
    .finally(() => {
      if (activeTaskId === task.id) {
        activeTaskId = null;
        activePromise = null;
      }
    });

  return activePromise;
}

export function startEnhancedMomentumScan(
  request: EnhancedMomentumScanRequest,
  options: { preserveSavedScan?: boolean } = {},
) {
  const existing = readPersistentValue<EnhancedMomentumScanTask | null>(ENHANCED_MOMENTUM_TASK_KEY, null);
  if (existing?.status === 'running') return executeTask(existing);

  const task: EnhancedMomentumScanTask = {
    id: createTaskId(),
    status: 'running',
    request: { ...request },
    startedAt: Date.now(),
  };
  if (!options.preserveSavedScan) writePersistentValue(ENHANCED_MOMENTUM_SCAN_KEY, null);
  writePersistentValue(ENHANCED_MOMENTUM_TASK_KEY, task);
  return executeTask(task);
}

export function resumeEnhancedMomentumScan(task: EnhancedMomentumScanTask) {
  if (task.status !== 'running') return null;
  return executeTask(task);
}
