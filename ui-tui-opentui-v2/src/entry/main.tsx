/**
 * Entry — the single boundary edge (spec v4 §3.1). This is the ONE place that:
 *   - acquires the renderer (acquireRelease + Deferred-on-destroy),
 *   - creates the Solid store,
 *   - wires GatewayService.subscribe -> store.apply  (Effect->Solid contact #2),
 *   - does the one-line `render(() => <App/>, renderer)` bridge (contact #1),
 *   - (live) bootstraps a session and optionally submits an initial prompt,
 *   - blocks until the renderer is destroyed (user quit),
 * and at the bottom PROVIDES the layers and runs (`Effect.provide(AppLayer)`).
 *
 * Backend selection (import.meta.main):
 *   - default            → the LIVE `liveGatewayLayer` (spawns the real Python
 *     `tui_gateway`); after `gateway.ready` it `session.create`s and, if an
 *     initial prompt is given (HERMES_TUI_PROMPT or argv), `prompt.submit`s it.
 *     The composer lands in Phase 2 — until then the initial prompt is how a
 *     streamed reply is driven into the transcript (spec Phase-1 smoke).
 *   - HERMES_TUI_FAKE=1  → the scripted FakeGateway "hello" (offline dev/CI).
 *
 * The body of `run` does not change when the backend swaps — that's the point of
 * the layer; only `makeAppLayer(...)` differs at the edge.
 */
import { render } from '@opentui/solid'
import { Deferred, Duration, Effect } from 'effect'

import { GatewayService, type GatewayServiceShape } from '../boundary/gateway/GatewayService.ts'
import { liveGatewayLayer } from '../boundary/gateway/liveGateway.ts'
import { getLog } from '../boundary/log.ts'
import { acquireRenderer } from '../boundary/renderer.ts'
import { makeAppLayer } from '../boundary/runtime.ts'
import { mapResumeHistory } from '../logic/resume.ts'
import { dispatchSlash, type SlashContext } from '../logic/slash.ts'
import { createSessionStore, type SessionStore } from '../logic/store.ts'
import { App } from '../view/App.tsx'
import { ThemeProvider } from '../view/theme.tsx'
import { makeFakeGatewayLayer, type FakeGatewayController } from './fakeGateway.ts'

export interface TuiInput {
  /** Mouse tracking on/off. */
  readonly mouse: boolean
  /** Skip the live session bootstrap (the fake backend drives the stream itself). */
  readonly fake: boolean
  /** Terminal width passed to `session.create` (Ink uses the live cols; 80 is a fine default). */
  readonly cols: number
  /** Optional initial prompt submitted once the session is ready — the Phase-1 stand-in for the composer. */
  readonly initialPrompt?: string
  /** Resume a session instead of creating one: a session id, or 'recent'/'last' (→ session.most_recent). */
  readonly resumeId?: string
}

const READY_POLL = Duration.millis(100)
const READY_TIMEOUT_MS = 20_000

/**
 * Live session bootstrap: wait for the unsolicited `gateway.ready` handshake,
 * then either RESUME a session (hydrate its transcript — incl. tool rows — via
 * the snapshot, buffering live events across the RPC) or CREATE a fresh one, and
 * (if given) submit the initial prompt. Forked into the entry scope so it runs
 * concurrently with the render + the quit-await. Any failure is logged and
 * swallowed — a bootstrap hiccup must never tear down the rendered UI.
 */
const bootstrapSession = (gateway: GatewayServiceShape, store: SessionStore, input: TuiInput) =>
  Effect.gen(function* () {
    const log = getLog()
    let waited = 0
    while (!store.state.ready && waited < READY_TIMEOUT_MS) {
      yield* Effect.sleep(READY_POLL)
      waited += 100
    }
    if (!store.state.ready) {
      log.warn('bootstrap', 'no gateway.ready within timeout', { waited })
      return
    }

    let sid: string | undefined
    if (input.resumeId) {
      sid = input.resumeId
      if (sid === 'recent' || sid === 'last') {
        const recent = yield* gateway.request<{ session_id?: string }>('session.most_recent', {})
        sid = recent?.session_id
      }
      if (!sid) {
        log.warn('bootstrap', 'no session to resume', { resumeId: input.resumeId })
        return
      }
      // Buffer live events across the resume RPC, then replace history + replay.
      store.beginBuffer()
      const t0 = Date.now()
      const resumed = yield* gateway.request<{ messages?: unknown }>('session.resume', {
        cols: input.cols,
        session_id: sid
      })
      const t1 = Date.now()
      const snapshot = mapResumeHistory(resumed?.messages)
      store.commitSnapshot(snapshot)
      // Hydration profile: rpc_ms = server load + transport; hydrate_ms = map + store write.
      log.info('bootstrap', 'session resumed', {
        count: snapshot.length,
        hydrate_ms: Date.now() - t1,
        rpc_ms: t1 - t0,
        sid
      })
    } else {
      const created = yield* gateway.request<{ session_id?: string }>('session.create', { cols: input.cols })
      sid = created?.session_id ?? gateway.sessionId()
      if (!sid) {
        log.warn('bootstrap', 'session.create returned no session_id')
        return
      }
      log.info('bootstrap', 'session created', { sid })
    }

    const prompt = input.initialPrompt?.trim()
    if (prompt) {
      store.pushUser(prompt)
      yield* gateway.request('prompt.submit', { session_id: sid, text: prompt })
    }
  }).pipe(Effect.catchCause(cause => Effect.sync(() => getLog().warn('bootstrap', 'failed', { cause: String(cause) }))))

/** The entry Effect. Mirrors opencode `app.tsx:177` `run = Effect.fn("Tui.run")`. */
export const run = Effect.fn('Tui.run')(function* (input: TuiInput) {
  yield* Effect.scoped(
    Effect.gen(function* () {
      // Solid side: the store + reducer. Created here, lives in Solid-land.
      const store = createSessionStore()

      // A blocking prompt owns Ctrl+C (→ cancel) — suppress the global quit while one is up.
      const { renderer, shutdown } = yield* acquireRenderer({
        mouse: input.mouse,
        isBlocked: () => store.state.prompt !== undefined
      })

      // Contact point #2: boundary pushes decoded events into the Solid store.
      const gateway = yield* GatewayService
      yield* gateway.subscribe(event => store.apply(event))

      // Submit a user turn: the service value is in hand, so `gateway.request(...)`
      // is Effect<…, never> — fire it detached with runFork; failures are logged.
      const submitPrompt = (text: string) => {
        store.pushUser(text)
        const sid = gateway.sessionId()
        if (!sid) {
          getLog().warn('submit', 'no session yet — dropping prompt', { text })
          return
        }
        Effect.runFork(
          gateway
            .request('prompt.submit', { session_id: sid, text })
            .pipe(
              Effect.catchCause(cause => Effect.sync(() => getLog().warn('submit', 'failed', { cause: String(cause) })))
            )
        )
      }

      // Slash dispatch context (Solid logic; the boundary just hands it a
      // Promise-returning `request` + the host capabilities it needs).
      const slashCtx: SlashContext = {
        clearTranscript: () => store.clearTranscript(),
        confirm: (message, onConfirm) => store.setConfirm(message, onConfirm),
        logTail: () =>
          getLog()
            .tail(40)
            .map(e => `${e.scope}: ${e.msg}`),
        pushSystem: text => store.pushSystem(text),
        quit: () => {
          if (!renderer.isDestroyed) renderer.destroy()
        },
        request: (method, params) => Effect.runPromise(gateway.request(method, params)),
        sessionId: () => gateway.sessionId(),
        submit: submitPrompt
      }

      // The composer's submit: route `/command` through the slash ladder, else a prompt.
      const submit = (text: string) => {
        if (text.startsWith('/')) void dispatchSlash(text, slashCtx)
        else submitPrompt(text)
      }

      // Blocking-prompt replies (clarify/approval/sudo/secret `*.respond`). Same
      // detached-runFork pattern; failures logged, never thrown into the view.
      const respond = (method: string, params: Record<string, unknown>) => {
        Effect.runFork(
          gateway
            .request(method, params)
            .pipe(
              Effect.catchCause(cause =>
                Effect.sync(() => getLog().warn('respond', 'failed', { cause: String(cause), method }))
              )
            )
        )
      }

      // Live backend: drive a session (create + optional initial prompt) concurrently.
      if (!input.fake) yield* Effect.forkScoped(bootstrapSession(gateway, store, input))

      // Contact point #1: the single render bridge. After this, the screen is Solid's.
      // The theme is sourced reactively from the store (skin events update it).
      yield* Effect.promise(() =>
        render(
          () => (
            <ThemeProvider theme={() => store.state.theme}>
              <App store={store} onSubmit={submit} onRespond={respond} sessionId={() => gateway.sessionId()} />
            </ThemeProvider>
          ),
          renderer
        )
      )

      // Block until the renderer is destroyed (Ctrl+C / quit); finalizers then run.
      yield* Deferred.await(shutdown)
    })
  )
})

/** Scripted "hello" stream so the fake backend paints a non-empty frame offline. */
function streamHello(controller: FakeGatewayController): void {
  controller.emit({ type: 'gateway.ready' })
  controller.emit({ type: 'message.start' })
  for (const chunk of ['Hi ', 'there, ', 'glitch!']) {
    controller.emit({ type: 'message.delta', payload: { text: chunk } })
  }
  controller.emit({ type: 'message.complete' })
}

const TRUE_RE = /^(?:1|true|yes|on)$/i

if (import.meta.main) {
  const fake = TRUE_RE.test(process.env.HERMES_TUI_FAKE?.trim() ?? '')
  const cols = process.stdout.columns || 80
  const initialPrompt = process.env.HERMES_TUI_PROMPT?.trim() || process.argv.slice(2).join(' ').trim()
  const resumeId = process.env.HERMES_TUI_RESUME?.trim()
  const base = { mouse: false, fake, cols }
  const withPrompt = initialPrompt ? { ...base, initialPrompt } : base
  const input: TuiInput = resumeId ? { ...withPrompt, resumeId } : withPrompt

  const onFatal = (error: unknown) => {
    getLog().error('entry', 'fatal', { error: String(error) })
    process.exitCode = 1
  }

  if (fake) {
    const { layer, controller } = makeFakeGatewayLayer()
    // Drive the fake stream shortly after mount so the subscription is live.
    setTimeout(() => streamHello(controller), 50)
    Effect.runPromise(run(input).pipe(Effect.provide(makeAppLayer(layer)))).catch(onFatal)
  } else {
    Effect.runPromise(run(input).pipe(Effect.provide(makeAppLayer(liveGatewayLayer)))).catch(onFatal)
  }
}
