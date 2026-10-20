// The admin API client, typed from the OpenAPI schema (`make client` writes ./schema.d.ts).
// The session is a same-origin cookie; mutating requests carry the CSRF token the session
// came with (`X-CSRF-Token`). Errors become `ApiError` with admin-api's stable `code`.
import createClient from "openapi-fetch";

import type { components, paths } from "./schema";

export type Schemas = components["schemas"];
export type Session = Schemas["SessionOut"];
export type Role = Schemas["UserOut"]["role"];

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  // admin-api's `error.details`, e.g. the gate's `reason` or a draft's validation errors.
  readonly details: Record<string, unknown>;

  constructor(status: number, code: string, message: string, details: Record<string, unknown> = {}) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export const client = createClient<paths>({
  // A Request needs an absolute URL outside the browser (tests); in the browser it is the page's origin.
  baseUrl: globalThis.location?.origin ?? "http://localhost",
  credentials: "same-origin",
  // Looked up per call, so tests can stub the global fetch after this module loads.
  fetch: (request) => globalThis.fetch(request),
});

export function csrf(token: string) {
  return { "X-CSRF-Token": token };
}

interface Result<T> {
  data?: T;
  error?: unknown;
  response: Response;
}

// The data of a successful call, or an ApiError: admin-api errors are
// {"error": {"code", "message", "details"?}} (admin_api.errors), request validation errors
// {"detail": [...]}.
export async function unwrap<T>(call: Promise<Result<T>>): Promise<T> {
  const { data, error, response } = await call;
  if (response.ok) return data as T;
  const body = (error ?? {}) as { error?: { code?: string; message?: string; details?: Record<string, unknown> } };
  if (body.error?.code) {
    throw new ApiError(response.status, body.error.code, body.error.message ?? response.statusText, body.error.details);
  }
  if (response.status === 422) throw new ApiError(422, "invalid_request", "Перевірте введені дані.");
  throw new ApiError(response.status, "http_error", response.statusText);
}
