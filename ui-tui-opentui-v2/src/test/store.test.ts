/**
 * Store test (spec v4 §5 Layer 3). Pure data behavior of the reducer: skin →
 * theme, LRU dedup, hydrate-while-buffering (Phase 1); and the Phase 2b ordered
 * `parts[]` model — text/tool interleave in one turn, tool start↔complete matched
 * by id and updated IN PLACE, `{output,exit_code}` envelope stripped.
 */
import { describe, expect, test } from 'bun:test'

import { DEFAULT_THEME } from '../logic/theme.ts'
import { createSessionStore, type Message } from '../logic/store.ts'

describe('session store — theming / dedup / hydrate (Phase 1)', () => {
  test('gateway.ready{skin} re-themes; default before', () => {
    const store = createSessionStore()
    expect(store.state.theme.brand.name).toBe(DEFAULT_THEME.brand.name)
    store.apply({
      type: 'gateway.ready',
      payload: { skin: { branding: { agent_name: 'Zephyr' }, colors: { ui_primary: '#123456' } } }
    })
    expect(store.state.ready).toBe(true)
    expect(store.state.theme.brand.name).toBe('Zephyr')
    expect(store.state.theme.color.primary).toBe('#123456')
  })

  test('skin.changed updates the theme live', () => {
    const store = createSessionStore()
    store.apply({ type: 'skin.changed', payload: { branding: { agent_name: 'Aurora' } } })
    expect(store.state.theme.brand.name).toBe('Aurora')
  })

  test('LRU dedup: duplicate(id) returns false once, true after', () => {
    const store = createSessionStore()
    expect(store.duplicate('evt-1')).toBe(false)
    expect(store.duplicate('evt-1')).toBe(true)
    expect(store.duplicate(undefined)).toBe(false) // no id → never deduped
  })

  test('hydrate replaces history, then replays events buffered mid-hydrate', () => {
    const store = createSessionStore()
    const snapshot: Message[] = [
      { role: 'user', text: 'old q' },
      { role: 'assistant', text: 'old a' }
    ]
    // Simulate a live event arriving DURING hydrate by emitting inside loadSnapshot.
    let emittedDuring = false
    store.hydrate(() => {
      if (!emittedDuring) {
        emittedDuring = true
        store.apply({ type: 'message.start' })
        store.apply({ type: 'message.delta', payload: { text: 'live!' } })
      }
      return snapshot
    })
    // snapshot (2) + the buffered live assistant turn (1) replayed after
    expect(store.state.messages.length).toBe(3)
    expect(store.state.messages[0]!.text).toBe('old q')
    // the streamed assistant text now lives in an ordered text part
    expect(store.state.messages[2]!.parts?.[0]).toMatchObject({ type: 'text', text: 'live!' })
  })
})

describe('session store — ordered parts (Phase 2b)', () => {
  test('interleaves text → tool → text as ordered parts in one assistant turn', () => {
    const store = createSessionStore()
    store.apply({ type: 'message.start' })
    store.apply({ type: 'message.delta', payload: { text: 'before ' } })
    store.apply({ type: 'tool.start', payload: { tool_id: 't1', name: 'terminal' } })
    // result_text is the {output,exit_code} JSON envelope — the store strips it.
    store.apply({
      type: 'tool.complete',
      payload: { tool_id: 't1', result_text: '{"output":"hello\\nworld","exit_code":0}' }
    })
    store.apply({ type: 'message.delta', payload: { text: 'after' } })
    store.apply({ type: 'message.complete' })

    const msg = store.state.messages.at(-1)!
    expect(msg.role).toBe('assistant')
    expect(msg.streaming).toBe(false)
    const parts = msg.parts ?? []
    expect(parts.map(p => p.type)).toEqual(['text', 'tool', 'text'])
    expect(parts[0]).toMatchObject({ type: 'text', text: 'before ' })
    expect(parts[2]).toMatchObject({ type: 'text', text: 'after' })
    const tool = parts[1]!
    if (tool.type === 'tool') {
      expect(tool.state).toBe('complete')
      expect(tool.name).toBe('terminal')
      expect(tool.resultText).toBe('hello\nworld') // envelope stripped
      expect(tool.lineCount).toBe(2)
    } else {
      throw new Error('expected a tool part at index 1')
    }
  })

  test('tool.complete updates the running tool part IN PLACE (not a new row)', () => {
    const store = createSessionStore()
    store.apply({ type: 'message.start' })
    store.apply({ type: 'tool.start', payload: { tool_id: 'x', name: 'read_file' } })
    expect(store.state.messages.at(-1)!.parts).toHaveLength(1)
    expect(store.state.messages.at(-1)!.parts![0]).toMatchObject({ type: 'tool', state: 'running', name: 'read_file' })

    store.apply({ type: 'tool.complete', payload: { tool_id: 'x', summary: 'read 42 lines' } })
    const parts = store.state.messages.at(-1)!.parts!
    expect(parts).toHaveLength(1) // updated in place — NOT appended as a separate row
    const tool = parts[0]!
    if (tool.type === 'tool') {
      expect(tool.state).toBe('complete')
      expect(tool.summary).toBe('read 42 lines')
    } else {
      throw new Error('expected a tool part')
    }
  })

  test('reasoning.delta accumulates into a reasoning part', () => {
    const store = createSessionStore()
    store.apply({ type: 'message.start' })
    store.apply({ type: 'reasoning.delta', payload: { text: 'thinking ' } })
    store.apply({ type: 'reasoning.delta', payload: { text: 'hard' } })
    const parts = store.state.messages.at(-1)!.parts ?? []
    expect(parts[0]).toMatchObject({ type: 'reasoning', text: 'thinking hard' })
  })
})

describe('session store — blocking prompts (Phase 3)', () => {
  test('approval.request sets an approval prompt; clearPrompt clears it', () => {
    const store = createSessionStore()
    expect(store.state.prompt).toBeUndefined()
    store.apply({ type: 'approval.request', payload: { command: 'rm -rf /tmp/x', description: 'delete temp' } })
    expect(store.state.prompt).toMatchObject({ kind: 'approval', command: 'rm -rf /tmp/x', description: 'delete temp' })
    store.clearPrompt()
    expect(store.state.prompt).toBeUndefined()
  })

  test('clarify.request carries question + choices + request_id', () => {
    const store = createSessionStore()
    store.apply({ type: 'clarify.request', payload: { question: 'Which?', choices: ['a', 'b'], request_id: 'r1' } })
    const p = store.state.prompt
    expect(p).toMatchObject({ kind: 'clarify', question: 'Which?', requestId: 'r1' })
    if (p?.kind === 'clarify') expect(p.choices).toEqual(['a', 'b'])
  })

  test('clarify.request with null choices → free-text only', () => {
    const store = createSessionStore()
    store.apply({ type: 'clarify.request', payload: { question: 'Name?', choices: null, request_id: 'r2' } })
    const p = store.state.prompt
    if (p?.kind === 'clarify') expect(p.choices).toBeNull()
  })

  test('sudo.request + secret.request set masked prompts', () => {
    const store = createSessionStore()
    store.apply({ type: 'sudo.request', payload: { request_id: 's1' } })
    expect(store.state.prompt).toMatchObject({ kind: 'sudo', requestId: 's1' })
    store.apply({ type: 'secret.request', payload: { env_var: 'API_KEY', prompt: 'Enter key', request_id: 's2' } })
    expect(store.state.prompt).toMatchObject({ kind: 'secret', envVar: 'API_KEY', requestId: 's2' })
  })
})

describe('session store — resume hydrate (Phase 4b)', () => {
  test('beginBuffer + commitSnapshot replaces history then replays events buffered across the resume', () => {
    const store = createSessionStore()
    store.beginBuffer()
    // a live event arrives DURING the (async) session.resume RPC
    store.apply({ type: 'message.start' })
    store.apply({ type: 'message.delta', payload: { text: 'live during resume' } })
    // the snapshot commits afterwards
    store.commitSnapshot([{ role: 'user', text: 'old question' }])
    expect(store.state.messages).toHaveLength(2) // snapshot(1) + the replayed assistant turn(1)
    expect(store.state.messages[0]).toMatchObject({ role: 'user', text: 'old question' })
    expect(store.state.messages[1]!.parts?.[0]).toMatchObject({ type: 'text', text: 'live during resume' })
  })
})
