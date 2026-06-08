/**
 * Resume snapshot mapper (spec §1 lifecycle; gotcha §8 #5). Maps the
 * `session.resume` response `messages` (tui_gateway `_history_to_messages`) into
 * the store's `Message[]`. Each history entry is either `{role, text}` (user/
 * assistant/system) or `{role:'tool', name, context}` (NO text — render it).
 *
 * Tool rows are folded into the PRECEDING assistant turn's ordered `parts[]`
 * (state:'complete', summary=context) so a resumed transcript renders inline like
 * a live one. Resumed assistant text is given a single text part so it renders
 * through the native markdown path. IDs are `r*` (distinct from live `p*`).
 */
import type { Message, Part } from './store.ts'

function readStr(value: unknown, key: string): string | undefined {
  if (!value || typeof value !== 'object') return undefined
  const v = (value as { [k: string]: unknown })[key]
  return typeof v === 'string' ? v : undefined
}

export function mapResumeHistory(history: unknown): Message[] {
  if (!Array.isArray(history)) return []
  const out: Message[] = []
  let seq = 0
  const id = () => `r${++seq}`
  let currentAssistant: Message | undefined

  for (const raw of history) {
    const role = readStr(raw, 'role')

    if (role === 'tool') {
      const name = readStr(raw, 'name') ?? 'tool'
      const context = readStr(raw, 'context')
      const tool: Part = { type: 'tool', id: id(), name, state: 'complete' }
      if (context) tool.summary = context
      if (!currentAssistant) {
        currentAssistant = { role: 'assistant', text: '', parts: [] }
        out.push(currentAssistant)
      }
      ;(currentAssistant.parts ??= []).push(tool)
      continue
    }

    const text = readStr(raw, 'text') ?? ''
    if (role === 'assistant') {
      const parts: Part[] = text ? [{ type: 'text', id: id(), text }] : []
      currentAssistant = { role: 'assistant', text, parts }
      out.push(currentAssistant)
    } else if (role === 'user' || role === 'system') {
      out.push({ role, text })
      currentAssistant = undefined
    }
  }

  return out
}
