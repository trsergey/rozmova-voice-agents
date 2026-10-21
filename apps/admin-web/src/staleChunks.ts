// A tab that outlived a deploy (or came back from the cache on back/forward) still runs the
// old entry, whose lazy chunks the new deploy no longer serves. Such a failed import is
// fixed by loading the current index.html, so it reloads the page once (ROZ-95). The guard
// stops a loop when the chunk is missing for another reason: within RELOAD_GUARD_MS of the
// last reload the error is shown instead.

const RELOAD_KEY = "chunk-reload-at";
export const RELOAD_GUARD_MS = 10_000;

// Chrome, Safari and Firefox word it differently.
const STALE_CHUNK = [
  "Failed to fetch dynamically imported module",
  "Importing a module script failed",
  "error loading dynamically imported module",
];

export function isStaleChunkError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error);
  return STALE_CHUNK.some((text) => message.includes(text));
}

// Reloads unless it did so moments ago; returns whether it did. Storage can be blocked
// (private mode, policies): then it still reloads once per page load.
let reloaded = false;
export function reloadOnce(reload: () => void = () => window.location.reload()): boolean {
  let last: number;
  try {
    last = Number(sessionStorage.getItem(RELOAD_KEY) ?? 0);
  } catch {
    last = 0;
  }
  if (reloaded || Date.now() - last < RELOAD_GUARD_MS) return false;
  reloaded = true;
  try {
    sessionStorage.setItem(RELOAD_KEY, String(Date.now()));
  } catch {
    // Without storage the in-memory flag is the guard.
  }
  reload();
  return true;
}

// For tests: forget the in-memory guard of this page load.
export function resetReloadGuard() {
  reloaded = false;
}

// Wraps a dynamic import: a stale chunk reloads the page and the import never settles (the
// page is going away), anything else, or a stale chunk right after a reload, is rethrown.
export async function loadChunk<M>(load: () => Promise<M>, reload?: () => void): Promise<M> {
  try {
    return await load();
  } catch (error) {
    if (isStaleChunkError(error) && reloadOnce(reload)) return new Promise<M>(() => {});
    throw error;
  }
}

// Vite's own signal for a failed preload of a chunk's dependencies.
export function listenForPreloadErrors() {
  window.addEventListener("vite:preloadError", (event) => {
    if (reloadOnce()) event.preventDefault();
  });
}
