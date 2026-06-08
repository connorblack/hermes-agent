/**
 * Resume mapper test (spec §1 lifecycle; gotcha §8 #5). The `session.resume`
 * history maps into the store's Message[], folding tool rows ({name,context},
 * NO text) into the preceding assistant turn's ordered parts so they render.
 */
import { describe, expect, test } from 'bun:test'

import { mapResumeHistory } from '../logic/resume.ts'

describe('mapResumeHistory (Phase 4b)', () => {
  test('maps user/assistant text + folds tool rows into the preceding assistant parts', () => {
    const msgs = mapResumeHistory([
      { role: 'user', text: 'list files' },
      { role: 'assistant', text: 'Listing.' },
      { role: 'tool', name: 'terminal', context: 'ls -la' },
      { role: 'assistant', text: 'Done.' }
    ])
    expect(msgs.map(m => m.role)).toEqual(['user', 'assistant', 'assistant'])
    expect(msgs[0]).toMatchObject({ role: 'user', text: 'list files' })

    const a1 = msgs[1]!
    expect(a1.parts?.map(p => p.type)).toEqual(['text', 'tool']) // text + folded tool, inline
    const tool = a1.parts![1]!
    if (tool.type === 'tool') {
      expect(tool).toMatchObject({ name: 'terminal', state: 'complete', summary: 'ls -la' })
    } else {
      throw new Error('expected a folded tool part')
    }
    expect(msgs[2]).toMatchObject({ role: 'assistant', text: 'Done.' })
  })

  test('a tool row with no preceding assistant gets a standalone assistant holder', () => {
    const msgs = mapResumeHistory([{ role: 'tool', name: 'read_file', context: 'foo.ts' }])
    expect(msgs).toHaveLength(1)
    expect(msgs[0]!.role).toBe('assistant')
    expect(msgs[0]!.parts?.[0]).toMatchObject({ type: 'tool', name: 'read_file', summary: 'foo.ts' })
  })

  test('ignores non-arrays and unknown roles', () => {
    expect(mapResumeHistory(null)).toEqual([])
    expect(mapResumeHistory([{ role: 'weird', text: 'x' }])).toEqual([])
  })
})
