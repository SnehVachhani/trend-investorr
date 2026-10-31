'use client';

import {
  useCallback,
  useMemo,
  useState,
  useSyncExternalStore,
  type Dispatch,
  type SetStateAction,
} from 'react';

export const PERSISTENCE_EVENT = 'trend-investorr:persistence';

function decode<T>(raw: string | null, fallback: T): T {
  if (raw == null) return fallback;
  try {
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

export function readPersistentValue<T>(key: string, fallback: T): T {
  if (typeof window === 'undefined') return fallback;
  try {
    return decode(window.localStorage.getItem(key), fallback);
  } catch {
    return fallback;
  }
}

export function writePersistentValue<T>(key: string, value: T): boolean {
  if (typeof window === 'undefined') return false;
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
    window.dispatchEvent(new CustomEvent(PERSISTENCE_EVENT, { detail: key }));
    return true;
  } catch {
    return false;
  }
}

function subscribeToKey(key: string, callback: () => void) {
  if (typeof window === 'undefined') return () => {};

  const onStorage = (event: StorageEvent) => {
    if (event.key === key || event.key === null) callback();
  };
  const onPersistenceEvent = (event: Event) => {
    if ((event as CustomEvent<string>).detail === key) callback();
  };

  window.addEventListener('storage', onStorage);
  window.addEventListener(PERSISTENCE_EVENT, onPersistenceEvent);
  return () => {
    window.removeEventListener('storage', onStorage);
    window.removeEventListener(PERSISTENCE_EVENT, onPersistenceEvent);
  };
}

/**
 * Persist workspace state in the browser without coupling it to a route.
 * The custom event makes updates visible immediately in the same tab; the
 * native storage event keeps another tab in sync as well.
 */
export function usePersistentState<T>(key: string, initialValue: T): [T, Dispatch<SetStateAction<T>>] {
  const [fallback] = useState(initialValue);
  const subscribe = useCallback((callback: () => void) => subscribeToKey(key, callback), [key]);
  const getSnapshot = useCallback(() => {
    if (typeof window === 'undefined') return null;
    try {
      return window.localStorage.getItem(key);
    } catch {
      return null;
    }
  }, [key]);
  const raw = useSyncExternalStore(subscribe, getSnapshot, () => null);
  const value = useMemo(() => decode(raw, fallback), [fallback, raw]);

  const setValue = useCallback<Dispatch<SetStateAction<T>>>((update) => {
    if (typeof window === 'undefined') return;
    const current = readPersistentValue(key, fallback);
    const next = typeof update === 'function'
      ? (update as (previous: T) => T)(current)
      : update;
    // Storage can be unavailable or full; the in-memory UI should continue.
    writePersistentValue(key, next);
  }, [fallback, key]);

  return [value, setValue];
}
