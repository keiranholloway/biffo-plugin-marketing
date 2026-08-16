/** Element identity + operator selection, the frontend half of issue #145.
 *
 * Mirrors `pipeline.py`'s `ELEMENT_LIST_KEYS`/`known_element_ids` on the
 * backend deliberately: the server stamps a stable `id` onto every element
 * of every one of these lists at persist time
 * (`pipeline.with_element_ids`), and this is the one place the frontend
 * reads that same set back out of a parsed artefact body — generically,
 * over the raw JSON, rather than four kind-specific copies (one per
 * `ResearchSynthesisBody`/`PositioningBody`/`ChannelPlanBody`/`CopySetBody`)
 * that the four artefact kinds would otherwise need to keep in step by hand.
 */

import type { Artefact } from './api'
import { parseArtefactBody } from './api'

/** Same field names `pipeline.ELEMENT_LIST_KEYS` (Python) lists —
 * `findings` (research), `segments`/`pillars`/`ctas` (positioning),
 * `channels` (channel_plan AND copy, which share the field name but not the
 * item shape; nothing here needs to tell them apart), and `proposals`
 * (channel_plan's outside-the-selection entries, #67).
 *
 * A proposal is selectable for the same reason a channel is — an operator
 * approving a stage says which of its elements carry forward, and a proposal
 * they want to keep on the record should survive that. It carrying forward
 * still does not make it a channel: everything downstream of the gate reads
 * `channels` alone (`pipeline.channel_key_motions`). */
const ELEMENT_LIST_KEYS = ['findings', 'segments', 'pillars', 'ctas', 'channels', 'proposals'] as const

/** One operator-selectable element, generic across every artefact kind — an
 * `id` to select/deselect by, and a short human-readable `label` for the
 * drop-consequence preview (`PipelineStage`'s "this will be dropped" list). */
export interface SelectableElement {
  id: string
  label: string
}

/** The single text field that best identifies one element, whichever of the
 * five shapes it is. Checked in an order that cannot collide: a research
 * finding, a segment, a pillar and a CTA each have exactly one of
 * `signal`/`name`/`pillar`/`text`; a channel-plan recommendation and a piece
 * of copy both use `channel_key`/`suggested_label`, distinguished from each
 * other only by copy also carrying `headline` — so copy's richer label is
 * checked first. */
function labelOf(item: Record<string, unknown>): string {
  if (typeof item.signal === 'string') return item.signal
  if (typeof item.name === 'string') return item.name
  if (typeof item.pillar === 'string') return item.pillar
  if (typeof item.text === 'string') return item.text
  const channel =
    typeof item.channel_key === 'string'
      ? item.channel_key
      : typeof item.suggested_label === 'string'
        ? item.suggested_label
        : 'this item'
  return typeof item.headline === 'string' ? `${channel} — ${item.headline}` : channel
}

/** Every element in `artefact`'s body that carries a server-assigned `id` —
 * `[]` for no artefact, an unparseable body, or a body from before #150
 * whose elements have none. An element with no `id` is not offered here:
 * `pipeline.selected_body` on the server drops an id-less item the moment
 * ANY selection is submitted (it cannot have been named in one), so
 * offering it a checkbox that silently does nothing the moment a sibling
 * element is deselected would be worse than not offering one at all. */
export function elementsOf(artefact: Artefact | null): SelectableElement[] {
  const body = parseArtefactBody<Record<string, unknown>>(artefact)
  if (body === null) return []
  const elements: SelectableElement[] = []
  for (const key of ELEMENT_LIST_KEYS) {
    const items = body[key]
    if (!Array.isArray(items)) continue
    for (const item of items) {
      if (item === null || typeof item !== 'object') continue
      const record = item as Record<string, unknown>
      if (typeof record.id !== 'string' || record.id === '') continue
      elements.push({ id: record.id, label: labelOf(record) })
    }
  }
  return elements
}

/** The controls an artefact-body renderer needs to show and toggle a
 * per-element checkbox, without knowing where the selection state actually
 * lives (`Pipeline.tsx`, per stage) — the same seam `channelLookup` already
 * uses for the taxonomy. */
export interface ElementSelection {
  isSelected: (id: string) => boolean
  toggle: (id: string) => void
}
