// Removing Cognito credentials belonging to pools this deployment no longer uses.
//
// `amazon-cognito-identity-js` stores tokens under keys shaped
// `CognitoIdentityServiceProvider.<ClientId>.<username>.<tokenType>` and reads
// them back scoped by Client ID. Replacing a user pool therefore does not clear
// the old one's keys: they are simply never read again, and they accumulate for
// as long as the browser profile lives. `dev.biffo.io` was measured carrying
// four pools' credentials — three of them dead — where AWS has one pool and one
// client (biffo-template#834).
//
// What this is NOT: a fix for a wrong-identity read. Because every consumer
// resolves its pool from the runtime identity document (#403) and lets
// amazon-cognito-identity-js scope the lookup by Client ID, a stale pool's
// tokens are never enumerated and never selected. That claim was made and
// withdrawn on #834.
//
// What it IS: dead bearer tokens for real identities should not sit in browser
// storage forever, and any future code that enumerates
// `CognitoIdentityServiceProvider.*` — a debug helper, a sign-out-everywhere
// action, a migration — should not inherit a growing minefield.

/** `CognitoIdentityServiceProvider.<clientId>.<rest…>` — capture the client id. */
const COGNITO_KEY = /^CognitoIdentityServiceProvider\.([^.]+)\./

/**
 * Delete every Cognito credential key whose Client ID is not `clientId`.
 *
 * Returns the keys removed, so a caller can log or assert on them. A no-op when
 * `clientId` is falsy — an unresolved identity must never be treated as "no
 * client matches", which would delete the live session along with the residue.
 *
 * Storage is injected for tests and to stay safe where there is none: this
 * module is imported during `next build`'s prerender in Node, where
 * `localStorage` does not exist.
 */
export function pruneForeignCognitoCredentials(
  clientId: string | null | undefined,
  storage: Storage | undefined = typeof localStorage === 'undefined' ? undefined : localStorage,
): string[] {
  if (!clientId || !storage) return []

  // Snapshot the keys first, via the Storage API rather than Object.keys:
  // removeItem() mutates the live key set, so index-based iteration would skip
  // entries as it shrinks and leave half the residue behind.
  const keys: string[] = []
  for (let i = 0; i < storage.length; i++) {
    const key = storage.key(i)
    if (key !== null) keys.push(key)
  }

  const removed: string[] = []
  for (const key of keys) {
    const match = COGNITO_KEY.exec(key)
    if (match && match[1] !== clientId) {
      storage.removeItem(key)
      removed.push(key)
    }
  }
  return removed
}
