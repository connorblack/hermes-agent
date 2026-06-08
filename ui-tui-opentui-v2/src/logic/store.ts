/**
 * Session/message store — the SOLID side (spec v4 §1, §7). Plain `createStore`
 * + an `apply(event)` reducer, à la opencode `context/sync-v2.tsx`. NOT Effect.
 * The boundary calls `apply` with already-decoded `GatewayEvent`s via
 * GatewayService.subscribe.
 *
 * Phase 2b: an assistant turn is ONE ordered `parts[]` of a discriminated union
 * (text / reasoning / tool), so tool calls render INLINE between text blocks
 * instead of dumped as separate rows below (§7 — the "dump-below" bug). Tools are
 * matched start↔complete by `tool_id`; `tool.complete` updates that part IN PLACE.
 * User/system rows stay flat `text` (no parts). Carried from Phase 1: streaming
 * concat (prefer `payload.text`), skin→theme, LRU dedup, hydrate-while-buffering.
 */
import { createStore, produce } from 'solid-js/store'

import type { GatewayEvent, GatewaySkinDecoded } from '../boundary/schema/GatewayEvent.ts'
import { stripToolEnvelope } from './toolOutput.ts'
import { DEFAULT_THEME, type Theme, themeFromSkin } from './theme.ts'

/** A tool call inside an assistant turn (matched start↔complete by `id`=tool_id). */
export interface ToolPartState {
  type: 'tool'
  id: string
  name: string
  state: 'running' | 'complete'
  /** Envelope-stripped output (multi-line → block render; the view caps it). */
  resultText?: string
  /** Short one-line status when there's no substantial output. */
  summary?: string
  error?: string
  lineCount?: number
}

/** One ordered piece of an assistant turn (§7). */
export type Part =
  | { type: 'text'; id: string; text: string }
  | { type: 'reasoning'; id: string; text: string }
  | ToolPartState

export interface Message {
  readonly role: 'user' | 'assistant' | 'system'
  /** Flat body for user/system rows (and settled/resumed assistant rows). */
  text: string
  /** Ordered parts for a live assistant turn; absent for user/system. */
  parts?: Part[]
  streaming?: boolean
}

/**
 * A BLOCKING interactive request from the agent (spec §8 #6 — unhandled = deadlock).
 * Each is answered via the matching `*.respond` RPC; Esc/Ctrl+C sends deny/empty.
 */
export type ActivePrompt =
  | { kind: 'clarify'; question: string; choices: string[] | null; requestId: string }
  | { kind: 'approval'; command: string; description: string }
  | { kind: 'sudo'; requestId: string }
  | { kind: 'secret'; envVar: string; prompt: string; requestId: string }
  // local (non-gateway) Y/N confirm — e.g. /clear, /new (spec §2a)
  | { kind: 'confirm'; message: string; onConfirm: () => void }

export interface StoreState {
  ready: boolean
  messages: Message[]
  theme: Theme
  /** The active blocking prompt (composer is hidden while set); undefined when none. */
  prompt: ActivePrompt | undefined
}

const LRU_LIMIT = 1000

/** Read a string field off an unknown payload record (no `any`, no cast). */
function readStr(payload: { readonly [k: string]: unknown }, key: string): string | undefined {
  const v = payload[key]
  return typeof v === 'string' ? v : undefined
}

export function createSessionStore() {
  const [state, setState] = createStore<StoreState>({
    ready: false,
    messages: [],
    theme: DEFAULT_THEME,
    prompt: undefined
  })

  // Monotonic part id (stable `key` per part so a new tool part below a streaming
  // text part doesn't remount/re-tokenize it).
  let partSeq = 0
  const nextId = () => `p${++partSeq}`

  // LRU id-dedup: events that carry a stable id are applied at most once.
  const applied = new Set<string>()
  function duplicate(id: string | undefined): boolean {
    if (!id) return false
    if (applied.has(id)) return true
    applied.add(id)
    if (applied.size > LRU_LIMIT) {
      const oldest = applied.values().next()
      if (!oldest.done) applied.delete(oldest.value)
    }
    return false
  }

  // Hydrate-while-buffering (resume): while a snapshot is loading, live events
  // queue here and replay after the snapshot is reconciled (opencode sync-v2).
  let buffering: GatewayEvent[] | null = null

  function setSkin(skin: GatewaySkinDecoded | undefined): void {
    setState('theme', themeFromSkin(skin))
  }

  // ── parts helpers (operate on a draft message inside produce) ───────────
  function appendPart(m: Message, type: 'text' | 'reasoning', text: string): void {
    const parts = (m.parts ??= [])
    const last = parts[parts.length - 1]
    if (last && last.type === type) last.text += text
    else parts.push({ type, id: nextId(), text })
  }

  /** The live (last) assistant message, optionally only when still streaming. */
  function liveAssistant(draft: StoreState, streamingOnly = false): Message | undefined {
    const last = draft.messages[draft.messages.length - 1]
    if (last && last.role === 'assistant' && (!streamingOnly || last.streaming)) return last
    return undefined
  }

  /** Ensure there's an open assistant turn to attach parts to (tool/reasoning). */
  function ensureAssistant(draft: StoreState): Message {
    const live = liveAssistant(draft, true)
    if (live) return live
    const created: Message = { role: 'assistant', text: '', parts: [], streaming: true }
    draft.messages.push(created)
    return created
  }

  /** Find a tool part by id, scanning recent assistant turns (complete may land late). */
  function findToolPart(draft: StoreState, id: string): ToolPartState | undefined {
    for (let i = draft.messages.length - 1; i >= 0; i--) {
      const parts = draft.messages[i]?.parts
      if (!parts) continue
      for (let j = parts.length - 1; j >= 0; j--) {
        const p = parts[j]
        if (p && p.type === 'tool' && p.id === id) return p
      }
    }
    return undefined
  }

  /** Push a user message (composer submit). */
  function pushUser(text: string) {
    setState(
      produce(draft => {
        draft.messages.push({ role: 'user', text })
      })
    )
  }

  /** Push a system line (slash output, errors, notices). */
  function pushSystem(text: string) {
    setState(
      produce(draft => {
        draft.messages.push({ role: 'system', text })
      })
    )
  }

  /** Clear the transcript (e.g. /clear, /new). */
  function clearTranscript() {
    setState('messages', [])
  }

  /** Open a local Y/N confirm dialog (non-gateway; e.g. /clear). */
  function setConfirm(message: string, onConfirm: () => void) {
    setState('prompt', { kind: 'confirm', message, onConfirm })
  }

  /** Reduce a decoded gateway event into the store. The sole boundary->Solid sink. */
  function apply(event: GatewayEvent): void {
    if (buffering) {
      buffering.push(event)
      return
    }
    applyNow(event)
  }

  function applyNow(event: GatewayEvent): void {
    switch (event.type) {
      case 'gateway.ready':
        setState('ready', true)
        setSkin(event.payload?.skin)
        break
      case 'skin.changed':
        setSkin(event.payload)
        break
      case 'message.start':
        setState(
          produce(draft => {
            draft.messages.push({ role: 'assistant', text: '', parts: [], streaming: true })
          })
        )
        break
      case 'message.delta': {
        // prefer `text` over `rendered` (gotcha §8 #4 — rendered is incremental Rich-ANSI).
        const text = event.payload?.text ?? ''
        if (!text) break
        setState(
          produce(draft => {
            const live = liveAssistant(draft, true)
            if (live) appendPart(live, 'text', text)
          })
        )
        break
      }
      case 'message.complete':
        setState(
          produce(draft => {
            const live = liveAssistant(draft, true)
            if (!live) return
            // If no deltas arrived (complete-only gateways), seed the full text once.
            const finalText = event.payload?.text
            const hasText = (live.parts ?? []).some(p => p.type === 'text' && p.text.length > 0)
            if (finalText && !hasText) appendPart(live, 'text', finalText)
            live.streaming = false
          })
        )
        break
      case 'reasoning.delta':
      case 'thinking.delta': {
        const text = event.payload?.text ?? ''
        if (!text) break
        setState(
          produce(draft => {
            appendPart(ensureAssistant(draft), 'reasoning', text)
          })
        )
        break
      }
      case 'tool.start': {
        const id = readStr(event.payload, 'tool_id')
        if (!id) break
        const name = readStr(event.payload, 'name') ?? 'tool'
        setState(
          produce(draft => {
            const live = ensureAssistant(draft)
            ;(live.parts ??= []).push({ type: 'tool', id, name, state: 'running' })
          })
        )
        break
      }
      case 'tool.complete': {
        const id = readStr(event.payload, 'tool_id')
        if (!id) break
        const name = readStr(event.payload, 'name')
        const error = readStr(event.payload, 'error')
        const summary = readStr(event.payload, 'summary')
        const resultText = stripToolEnvelope(readStr(event.payload, 'result_text') ?? summary ?? '')
        const lineCount = resultText ? resultText.replace(/\s+$/, '').split('\n').length : 0
        setState(
          produce(draft => {
            let part = findToolPart(draft, id)
            if (!part) {
              // complete without a matching start — append a settled tool part.
              part = { type: 'tool', id, name: name ?? 'tool', state: 'running' }
              ;(ensureAssistant(draft).parts ??= []).push(part)
            }
            part.state = 'complete'
            part.lineCount = lineCount
            if (name) part.name = name
            if (resultText) part.resultText = resultText
            if (summary) part.summary = summary
            if (error) part.error = error
          })
        )
        break
      }
      // ── blocking prompts (spec §8 #6 — unhandled = the agent deadlocks) ──
      case 'clarify.request':
        setState('prompt', {
          kind: 'clarify',
          question: event.payload.question ?? '',
          // decoded choices are readonly — copy to the store's mutable string[]
          choices: event.payload.choices ? [...event.payload.choices] : null,
          requestId: event.payload.request_id
        })
        break
      case 'approval.request':
        setState('prompt', { kind: 'approval', command: event.payload.command, description: event.payload.description })
        break
      case 'sudo.request':
        setState('prompt', { kind: 'sudo', requestId: event.payload.request_id })
        break
      case 'secret.request':
        setState('prompt', {
          kind: 'secret',
          envVar: event.payload.env_var,
          prompt: event.payload.prompt,
          requestId: event.payload.request_id
        })
        break
      // Other event types (chrome, subagents) are reduced in later phases;
      // unhandled members are intentionally ignored here.
    }
  }

  /** Clear the active blocking prompt (after it's answered/cancelled). */
  function clearPrompt(): void {
    setState('prompt', undefined)
  }

  // ── resume hydrate (opencode sync-v2): buffer live events while the snapshot
  // loads, then replace history + replay the buffer in order. Split into begin/
  // commit so the buffer can span an async `session.resume` RPC.
  /** Start buffering live events (call BEFORE the async resume RPC). Idempotent. */
  function beginBuffer(): void {
    if (!buffering) buffering = []
  }

  /** Replace history with the resume snapshot, then replay events buffered meanwhile. */
  function commitSnapshot(snapshot: Message[]): void {
    setState('messages', snapshot)
    const pending = buffering ?? []
    buffering = null
    for (const event of pending) applyNow(event)
  }

  /** Synchronous convenience: buffer → load → commit (used by tests). */
  function hydrate(loadSnapshot: () => Message[]): void {
    beginBuffer()
    commitSnapshot(loadSnapshot())
  }

  return {
    state,
    apply,
    pushUser,
    pushSystem,
    clearTranscript,
    setConfirm,
    hydrate,
    beginBuffer,
    commitSnapshot,
    duplicate,
    clearPrompt
  } as const
}

export type SessionStore = ReturnType<typeof createSessionStore>
