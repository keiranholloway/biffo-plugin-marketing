/** Composes the "copy everything for this channel" text (#103a) — headline,
 * body, CTA and, when one exists, the channel's own tracked link, joined the
 * way an operator would actually want to paste them into a composer: one
 * blank line between each piece, not run together.
 *
 * This is the real unit of work when publishing (issue #103's own framing):
 * an operator copying a channel's copy four separate times, in the right
 * order, into a composer is exactly the tedium a single "copy all" control
 * removes. `linkUrl` is omitted entirely when there is nothing to copy (no
 * tracked link minted yet, or this deployment has no public base URL) —
 * never a placeholder line saying so, since that placeholder would itself
 * get pasted into the composer.
 */
export function composeChannelCopy({
  headline,
  body,
  cta,
  linkUrl,
}: {
  headline: string
  body: string
  cta: string
  linkUrl: string | null
}): string {
  const parts = [headline, body, cta]
  if (linkUrl !== null && linkUrl !== '') parts.push(linkUrl)
  return parts.join('\n\n')
}
