const key = 'ssearch.query.v1';

/** Ephemeral partition/session UI recovery only; never persisted settings or credentials. */
export function readTransientQuery(): string {
  try { return sessionStorage.getItem(key) ?? ''; } catch { return ''; }
}
export function writeTransientQuery(value: string): void {
  try {
    if (new TextEncoder().encode(value).length <= 65_536) sessionStorage.setItem(key, value);
    else sessionStorage.removeItem(key);
  } catch { /* Storage refusal must not prevent editing/search. */ }
}
