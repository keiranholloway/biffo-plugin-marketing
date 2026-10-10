import {
  CognitoUserPool,
  type CognitoUserSession,
  type ICognitoUserPoolData,
} from 'amazon-cognito-identity-js'

import { pruneForeignCognitoCredentials } from './cognito-hygiene'
import { resolveCoreIdentity } from './identity'

// SHARED-SESSION INVARIANT (ADR-0007), mirroring the sibling skeleton's auth.ts.
//
// This app NEVER signs anyone in — the core portal owns authentication. It only
// READS the session the portal already established, which works because it points
// at the SAME Cognito User Pool / App Client as the portal (resolved at runtime
// from identity.ts). Same origin + one App Client means amazon-cognito-identity-js's
// localStorage keys (keyed by Client ID, not path) carry the portal's session over
// here for free. Do NOT point at a different pool/client (breaks SSO), and do NOT
// add signIn/signOut here (a second login path bypasses the portal).
//
// The pool is built lazily and memoised: a missing identity resolves to null →
// "signed out", never a hard crash.

let poolPromise: Promise<CognitoUserPool | null> | null = null

// Memoises the IN-FLIGHT promise, not the settled value (biffo-plugin-marketing#55).
// `poolPromise ??=` assigns synchronously, before the first `await` inside the IIFE
// runs, so every caller that arrives before the first resolution completes shares
// that SAME promise instead of each re-running resolveCoreIdentity() and
// pruneForeignCognitoCredentials()'s localStorage scan-and-delete independently.
// A rejection clears the memo (`.catch` below) so one transient failure does not
// poison every later call for the rest of the page's life.
function getUserPool(): Promise<CognitoUserPool | null> {
  poolPromise ??= (async () => {
    const identity = await resolveCoreIdentity()
    if (!identity) return null
    // Once per page load, and only with a resolved client id: drop credentials
    // left behind by pools this deployment no longer uses (biffo-template#834).
    // The portal and the sibling skeleton do the same; this origin is shared, so
    // whichever app loads first does the cleaning.
    pruneForeignCognitoCredentials(identity.clientId)
    const poolData: ICognitoUserPoolData = {
      UserPoolId: identity.userPoolId,
      ClientId: identity.clientId,
    }
    return new CognitoUserPool(poolData)
  })().catch((err: unknown) => {
    poolPromise = null
    throw err
  })
  return poolPromise
}

/** The shared portal session, or null if there isn't a valid one (→ redirect to login). */
export async function getCurrentSession(): Promise<CognitoUserSession | null> {
  const pool = await getUserPool()
  if (!pool) return null
  return new Promise((resolve) => {
    const user = pool.getCurrentUser()
    if (!user) {
      resolve(null)
      return
    }
    user.getSession((err: Error | null, session: CognitoUserSession | null) => {
      resolve((err ?? !session?.isValid()) ? null : session)
    })
  })
}

/**
 * A currently-valid ID token for the shared portal session, or null.
 *
 * Call this per request; NEVER snapshot the JWT. A `CognitoUserSession` is an
 * immutable value object — `getIdToken()` hands back the same `CognitoIdToken`
 * forever, and `isValid()` is true right up to the expiry second. So a client
 * built from `createApi(() => sessionCapturedAtMount.getIdToken().getJwtToken())`
 * sends a token frozen at mount whose remaining life is whatever was left on the
 * *cached* token — possibly seconds. Once it lapses every call 401s for the life
 * of the page and nothing recovers it but a reload (#69).
 *
 * Re-resolving instead is cheap and self-healing: `pool.getCurrentUser()` returns
 * a fresh `CognitoUser` with no in-memory session, so `getSession()` re-reads
 * storage every time and swaps in a new token via the refresh token exactly when
 * the stored one has expired. No network call while the token is still good.
 */
export async function getFreshIdToken(): Promise<string | null> {
  const session = await getCurrentSession()
  return session ? session.getIdToken().getJwtToken() : null
}

/** Test-only: reset the memoised pool. */
export function __resetUserPoolForTests(): void {
  poolPromise = null
}
