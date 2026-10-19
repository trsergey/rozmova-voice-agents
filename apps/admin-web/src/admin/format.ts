// Dates as the admin shows them: Ukrainian, in the viewer's time zone.
const dateTime = new Intl.DateTimeFormat("uk-UA", { dateStyle: "medium", timeStyle: "short" });

export function formatDateTime(iso: string): string {
  return dateTime.format(new Date(iso));
}

const time = new Intl.DateTimeFormat("uk-UA", { timeStyle: "medium" });

export function formatTime(iso: string): string {
  return time.format(new Date(iso));
}

// 0:42, 12:05, 1:02:03
export function formatDuration(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = String(total % 60).padStart(2, "0");
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
}

export function formatMs(ms: number): string {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} с` : `${Math.round(ms)} мс`;
}
