// Runtime core identity — same mechanism as the founder-facing web/'s own
// identity.ts (ADR-0007). This used to fetch admin_app's own /identity route
// instead, on the theory that this app (served by the plugin host at
// /api/v1/plugins/<plugin-slug>/admin/*) is a different origin from the portal
// and so can't reach /.well-known/biffo-identity.json directly. That theory
// was wrong: both are served from the same dev.biffo.io origin. The self-served
// /identity route also turned out to be a dead end even for same-origin
// callers — the API Gateway's own JWT authorizer and the plugin host's
// group_gate both sit in front of it, so it can never be reached before a
// session exists to prove admin-group membership with (confirmed live: a
// direct fetch 401'd). /.well-known/biffo-identity.json has no such problem:
// it's public, unauthenticated static content on the portal's own bucket, and
// was already reachable the whole time.
//
// Core publishes its Cognito coordinates there. We resolve it at RUNTIME so
// this app never bakes the core's pool/client id into its bundle — when core
// replaces its pool, we always see the current one. Same-origin makes a
// relative fetch valid with no CORS. Memoised: at most one request per page
// load. Unreachable → null → the caller treats the visitor as signed out (a
// clean redirect beats trusting a stale local pool id).

export interface CoreIdentity {
  userPoolId: string
  clientId: string
  region?: string
  apiUrl?: string
  portalUrl?: string
}

const IDENTITY_DOCUMENT_PATH = '/.well-known/biffo-identity.json'

let cached: Promise<CoreIdentity | null> | null = null

function identityFromDocument(data: unknown): CoreIdentity | null {
  if (typeof data !== 'object' || data === null) return null
  const doc = data as Record<string, unknown>
  const userPoolId = typeof doc['userPoolId'] === 'string' ? doc['userPoolId'] : ''
  const clientId = typeof doc['clientId'] === 'string' ? doc['clientId'] : ''
  if (!userPoolId || !clientId) return null
  const identity: CoreIdentity = { userPoolId, clientId }
  if (typeof doc['region'] === 'string') identity.region = doc['region']
  if (typeof doc['apiUrl'] === 'string') identity.apiUrl = doc['apiUrl']
  if (typeof doc['portalUrl'] === 'string') identity.portalUrl = doc['portalUrl']
  return identity
}

async function fetchCoreIdentity(): Promise<CoreIdentity | null> {
  try {
    const res = await fetch(IDENTITY_DOCUMENT_PATH, { cache: 'no-store' })
    if (res.ok) {
      const identity = identityFromDocument(await res.json())
      if (identity) return identity
    }
  } catch {
    // Network error or fetch unavailable — fall through to null.
  }
  console.warn(
    `[biffo] could not resolve the core identity document at ${IDENTITY_DOCUMENT_PATH}; ` +
      'treating the visitor as signed out.',
  )
  return null
}

/** Resolve core's Cognito identity at runtime. Memoised; null when unreachable. */
export function resolveCoreIdentity(): Promise<CoreIdentity | null> {
  cached ??= fetchCoreIdentity()
  return cached
}

/** Test-only: clear the memoised resolution. */
export function __resetCoreIdentityForTests(): void {
  cached = null
}
