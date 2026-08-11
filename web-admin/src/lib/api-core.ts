// Shared admin-API request core, DISTRIBUTED across every plugin's web-admin
// (biffo-template#1492). It carries exactly the parts that were rewritten
// identically, badly, by hand in three plugins: the fetch wrapper, the bearer
// auth header, error handling, and per-request base resolution. It carries
// NONE of a plugin's own endpoints — those are plugin-owned and live beside
// this module in each repo's own api.ts (see this skeleton's starter copy).
//
// `getIdToken` is `() => string | null | Promise<string | null>`, matching
// `./auth.ts`'s `getFreshIdToken()`, and MUST be called fresh per request —
// never snapshotted. `CognitoUserSession` is an immutable value object, so a
// client built from a token captured once at mount sends whatever was left on
// the token's remaining lifetime at that instant; once it lapses every call
// 401s for the rest of the page's life (ideation#69). idea-scout's original
// api.ts used a synchronous `token: () => string | null` for exactly this
// reason — it could not await a fresh resolution — and shipped the bug. This
// core is built async from the start so that mistake cannot recur here.
export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

export type GetIdToken = () => string | null | Promise<string | null>

/**
 * Maps a failed response into the error a plugin wants to surface, INSTEAD of
 * the default `ApiError(status, body)`.
 *
 * It receives the `Response` with its body **unread**, so it can `.json()` the
 * payload — Core returns `{"detail": "..."}` for a permission failure, and a
 * plugin usually wants that sentence rather than a status code. It also
 * receives the per-call `context` string, because the useful wording is
 * endpoint-specific: "you need the admin role to **mint links**" is actionable
 * where "403" is not.
 *
 * Added for biffo-template#1492. The marketing plugin could not adopt this core
 * without it: `createRequest` read the body into a string and threw immediately,
 * leaving no seam to inspect it first, so migrating would have collapsed a dozen
 * hand-written, endpoint-specific messages into one generic status string. That
 * is a behaviour regression, and the plugin correctly refused rather than
 * dropping them — the gap was in this module, not in the plugin.
 *
 * MUST NOT RETURN NORMALLY: it either throws, or returns a rejected promise.
 * The `never` return type says so; a mapper that falls through would make
 * `request()` resolve `undefined` for a failed call, which is the silent-success
 * shape this estate spends most of its time eliminating. `assertThrew` in
 * `api-core.test.ts` holds that line.
 */
export type ErrorMapper = (
  response: Response,
  context: string | undefined,
) => Promise<never> | never

/**
 * Build a `request<T>(method, path, body?, base?)` function bound to a token
 * source and a default base. A plugin's own api.ts calls this once per
 * `createApi()` and defines its endpoints on top of the result — see the
 * starter `api.ts` in this same directory for the worked shape.
 */
export function createRequest(getIdToken: GetIdToken, defaultBase: string, onError?: ErrorMapper) {
  return async function request<T>(
    method: string,
    path: string,
    body?: unknown,
    base: string = defaultBase,
    context?: string,
  ): Promise<T> {
    const token = await getIdToken()
    const res = await fetch(`${base}${path}`, {
      method,
      headers: {
        'Content-Type': 'application/json',
        ...(token != null ? { Authorization: `Bearer ${token}` } : {}),
      },
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    })
    if (!res.ok) {
      // The mapper runs FIRST, and gets the response with its body still
      // unread. Order is load-bearing: `res.text()` below consumes the stream,
      // and a mapper handed an already-consumed Response cannot call `.json()`
      // — it would silently see an empty body and fall back to a generic
      // message, which is the exact failure this hook exists to prevent.
      if (onError) await onError(res, context)
      // Read the body for the reason: Core returns a JSON detail for a
      // permission failure, and "403" alone tells an admin nothing about
      // which rule bit.
      const detail = await res.text().catch(() => res.statusText)
      throw new ApiError(res.status, detail || res.statusText)
    }
    if (res.status === 204) return undefined as T
    return (await res.json()) as T
  }
}
