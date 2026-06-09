/**
 * DEV BENCH — NOT a test, NOT production code. Throwaway memory-measurement
 * harness for the Epic 5 comparison doc. Empirically checks whether the rolling
 * MESSAGE_CAP bounds the native (Yoga/renderable) allocation footprint as the
 * transcript grows. Excluded from `bun test` (not a *.test.ts) and lint-clean.
 *
 *   Uncapped:  HERMES_TUI_MAX_MESSAGES=100000 bun scripts/mem-bench.ts
 *   Capped:    HERMES_TUI_MAX_MESSAGES=400    bun scripts/mem-bench.ts
 *
 * Run each as a SEPARATE bun invocation so the WASM/native heap starts fresh.
 *
 * Signal: native `getAllocatorStats().activeAllocations` (the Zig-side allocator
 * count — every live renderable/Yoga subtree contributes) and the recursive
 * renderable descendant count under `renderer.root`. RSS is reported too but is
 * noisy and grow-only (WASM linear memory never returns to the OS), so the
 * meaningful comparison is the SLOPE of activeAllocations / descendant count:
 * capped should plateau after ~CAP messages; uncapped should keep climbing.
 *
 * GC: forces `Bun.gc(true)` (synchronous) before each sample to measure RETAINED
 * memory, not garbage. (`--expose-gc`/`global.gc` is unavailable under Bun.)
 */
import { resolveRenderLib } from '@opentui/core'
import type { Renderable } from '@opentui/core'
import { testRender } from '@opentui/solid'

import { createSessionStore } from '../src/logic/store.ts'
import { App } from '../src/view/App.tsx'
import { ThemeProvider } from '../src/view/theme.tsx'

const lib = resolveRenderLib()

const TOTAL = Number.parseInt(process.env.MEM_BENCH_TOTAL ?? '5000', 10)
const SAMPLE_EVERY = Number.parseInt(process.env.MEM_BENCH_SAMPLE ?? '250', 10)
const cap = process.env.HERMES_TUI_MAX_MESSAGES ?? '(default 400)'

const MB = (bytes: number) => (bytes / 1024 / 1024).toFixed(1)

/** Recursively count every Renderable under root (a proxy for live Yoga nodes). */
function descendantCount(node: Renderable): number {
  let n = 0
  for (const child of node.getChildren()) n += 1 + descendantCount(child)
  return n
}

/** One streamed assistant turn = a few text parts (a realistic multi-node subtree). */
function pushTurn(store: ReturnType<typeof createSessionStore>, i: number): void {
  store.pushUser(`user message ${i}: please summarize the situation in a few lines`)
  store.apply({ type: 'message.start' })
  store.apply({ type: 'message.delta', payload: { text: `Sure — point one for turn ${i}. ` } })
  store.apply({ type: 'message.delta', payload: { text: `Here is point two with a bit more detail. ` } })
  store.apply({ type: 'message.delta', payload: { text: `And a closing point three for turn ${i}.` } })
  store.apply({ type: 'message.complete' })
}

async function main(): Promise<void> {
  const store = createSessionStore()
  store.apply({ type: 'gateway.ready' })

  const setup = await testRender(
    () => (
      <ThemeProvider theme={() => store.state.theme}>
        <App store={store} />
      </ThemeProvider>
    ),
    { width: 100, height: 40, exitOnCtrlC: false }
  )
  await setup.renderOnce()
  await setup.flush()

  // header: pad to fixed widths for a readable table
  process.stdout.write(`\n=== mem-bench  cap=${cap}  total=${TOTAL}  sampleEvery=${SAMPLE_EVERY} ===\n`)
  process.stdout.write(
    'pushes | msgs | rss(MB) | heapUsed(MB) | external(MB) | arrayBuf(MB) | activeAllocs | renderables\n'
  )
  process.stdout.write(
    '-------+------+---------+--------------+--------------+--------------+--------------+------------\n'
  )

  async function sample(pushes: number): Promise<void> {
    await setup.renderOnce()
    await setup.flush()
    Bun.gc(true) // synchronous, full GC — measure retained, not garbage
    const m = process.memoryUsage()
    const alloc = lib.getAllocatorStats()
    const renderables = descendantCount(setup.renderer.root)
    const cols = [
      String(pushes).padStart(6),
      String(store.state.messages.length).padStart(4),
      MB(m.rss).padStart(7),
      MB(m.heapUsed).padStart(12),
      MB(m.external).padStart(12),
      MB(m.arrayBuffers).padStart(12),
      String(alloc.activeAllocations).padStart(12),
      String(renderables).padStart(11)
    ]
    process.stdout.write(cols.join(' | ') + '\n')
  }

  await sample(0)
  for (let i = 1; i <= TOTAL; i++) {
    pushTurn(store, i)
    if (i % SAMPLE_EVERY === 0) await sample(i)
  }

  setup.renderer.destroy()
}

await main()
