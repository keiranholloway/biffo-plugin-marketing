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

let userPool: CognitoUserPool | null = null

async function getUserPool(): Promise<CognitoUserPool | null> {
  if (userPool) return userPool
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
  userPool = new CognitoUserPool(poolData)
  return userPool
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
      resolve(err ?? !session?.isValid() ? null : session)
    })
  })
}

/** Test-only: reset the memoised pool. */
export function __resetUserPoolForTests(): void {
  userPool = null
}
