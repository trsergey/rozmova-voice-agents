// The admin API calls the demo page needs: session, bot list and call tickets. The session
// is a same-origin cookie; mutating requests carry the CSRF token from the session.

export interface Session {
  user: { id: string; name: string; role: "admin" | "viewer" };
  csrf_token: string;
}

export interface Bot {
  id: string;
  name: string;
  live_version_id: string | null;
  live_version: number | null;
}

export interface Ticket {
  ticket: string;
  call_id: string;
  ws_url: string;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function request<T>(method: string, path: string, csrf?: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (csrf) headers["X-CSRF-Token"] = csrf;
  const response = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
  });
  if (!response.ok) {
    // admin-api errors are {"error": {"code", "message"}} (admin_api.errors).
    const payload = await response.json().catch(() => ({}));
    const error = payload?.error ?? {};
    throw new ApiError(response.status, error.code ?? "http_error", error.message ?? response.statusText);
  }
  return (response.status === 204 ? undefined : await response.json()) as T;
}

export const api = {
  me: () => request<Session>("GET", "/auth/me"),
  loginWithCode: (code: string) => request<Session>("POST", "/auth/access-code", undefined, { code }),
  logout: (csrf: string) => request<undefined>("POST", "/auth/logout", csrf, {}),
  bots: () => request<Bot[]>("GET", "/bots"),
  ticket: (csrf: string, botId: string, callId?: string) =>
    request<Ticket>("POST", "/calls/ticket", csrf, { bot_id: botId, call_id: callId ?? null }),
};
