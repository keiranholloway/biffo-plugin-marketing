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
 * Build a `request<T>(method, path, body?, base?)` function bound to a token
 * source and a default base. A plugin's own api.ts calls this once per
 * `createApi()` and defines its endpoints on top of the result — see the
 * starter `api.ts` in this same directory for the worked shape.
 */
export function createRequest(getIdToken: GetIdToken, defaultBase: string) {
  return async function request<T>(
    method: string,
    path: string,
    body?: unknown,
    base: string = defaultBase,
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
