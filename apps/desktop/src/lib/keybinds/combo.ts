// Keybind combo normalization + display.
//
// A combo is a canonical lowercase string like "mod+k", "mod+shift+]", "shift+x",
// or "r". `mod` is Cmd on macOS / Ctrl elsewhere, so a single binding works on
// both. We derive the base key from `event.code` (not `event.key`) so Shift never
// mutates it ("shift+/" stays "shift+/" instead of becoming "shift+?").

const IS_MAC =
  typeof navigator !== 'undefined' && /mac/i.test(navigator.platform || navigator.userAgent || '')

export const modKey = IS_MAC ? 'metaKey' as const : 'ctrlKey' as const

// event.code → canonical base token. Letters/digits map to their lowercase
// character; everything else uses an explicit name so combos read cleanly.
const CODE_TO_KEY = {
  Backquote: '`',
  Backslash: '\\',
  BracketLeft: '[',
  BracketRight: ']',
  Comma: ',',
  Equal: '=',
  Minus: '-',
  Period: '.',
  Quote: "'",
  Semicolon: ';',
  Slash: '/',
  Space: 'space',
  Enter: 'enter',
  Escape: 'escape',
  Backspace: 'backspace',
  Tab: 'tab',
  ArrowUp: 'up',
  ArrowDown: 'down',
  ArrowLeft: 'left',
  ArrowRight: 'right'
} as const satisfies Record<Capitalize<string>, Lowercase<string>>

type SpecialKey = typeof CODE_TO_KEY[keyof typeof CODE_TO_KEY]

type Alpha = 'a'|'b'|'c'|'d'|'e'|'f'|'g'|'h'|'i'|'j'|'k'|'l'|'m'
           | 'n'|'o'|'p'|'q'|'r'|'s'|'t'|'u'|'v'|'w'|'x'|'y'|'z'

export type Digit = '0'|'1'|'2'|'3'|'4'|'5'|'6'|'7'|'8'|'9'


type FKey =
| 'f1' | 'f2' | 'f3' | 'f4' | 'f5' | 'f6'
| 'f7' | 'f8' | 'f9' | 'f10' | 'f11' | 'f12'
| 'f13' | 'f14' | 'f15' | 'f16' | 'f17' | 'f18'
| 'f19' | 'f20' | 'f21' | 'f22' | 'f23' | 'f24'

type BaseKey = Alpha | Digit | FKey | SpecialKey

// subset of https://developer.mozilla.org/en-US/docs/Web/API/UI_Events/Keyboard_event_code_values
type KeyCode = Uppercase<FKey> | `Digit${Digit}` | `Key${Uppercase<Alpha>}` | keyof typeof CODE_TO_KEY

function baseKeyFromCode(code: KeyCode): BaseKey | null {
  if (code.startsWith('Key')) {
    return code.slice(3).toLowerCase() as Alpha
  }

  if (code.startsWith('Digit')) {
    return code.slice(5) as Digit
  }

  if (code.startsWith('Numpad')) {
    const rest = code.slice(6)

    return /^[0-9]$/.test(rest) ? rest as Digit : null
  }

  if (code.startsWith('F') && /^F\d{1,2}$/.test(code)) {
    return code.toLowerCase() as FKey
  }

  return CODE_TO_KEY[code as keyof typeof CODE_TO_KEY] ?? null
}


const MODIFIER_CODES = new Set([
  'AltLeft',
  'AltRight',
  'ControlLeft',
  'ControlRight',
  'MetaLeft',
  'MetaRight',
  'ShiftLeft',
  'ShiftRight'
])

// Returns the canonical combo for a keydown, or null while only modifiers are
// held (so capture mode keeps waiting for a real key).
export function comboFromEvent(event: KeyboardEvent): Combo | null {
  if (MODIFIER_CODES.has(event.code)) {
    return null
  }

  const base = baseKeyFromCode(event.code as KeyCode)

  if (!base) {
    return null
  }

  const parts: Combo[] = []

  if (event.metaKey || event.ctrlKey) {
    parts.push('mod')
  }

  if (event.altKey) {
    parts.push('alt')
  }

  if (event.shiftKey) {
    parts.push('shift')
  }

  parts.push(base)

  return parts.join('+') as Combo
}

const MOD_LABELS = {
  mod: IS_MAC ? '⌘' : 'Ctrl',
  alt: IS_MAC ? '⌥' : 'Alt',
  shift: IS_MAC ? '⇧' : 'Shift'
} as const

const FANCY_KEY_LABELS = {
  enter: '↵',
  escape: 'Esc',
  backspace: '⌫',
  tab: '⇥',
  space: 'Space',
  up: '↑',
  down: '↓',
  left: '←',
  right: '→',
} as const

const TOKEN_LABELS: Record<string, string> = {
  ...MOD_LABELS,
  ...FANCY_KEY_LABELS
}

function labelForToken(base: string): string {
  if (TOKEN_LABELS[base]) {
    return TOKEN_LABELS[base]
  }

  if (/^f\d{1,2}$/.test(base)) {
    return base.toUpperCase()
  }

  return base.length === 1 ? base.toUpperCase() : base
}

// 

type ModKey = keyof typeof MOD_LABELS

type ModPrefix = `${'mod+'|''}${'alt+'|''}${'shift+'|''}` 

type ModPrefixedCombo<Suffix extends string> = 
  | `${ModPrefix}${Suffix}` 
  | ModKey
  | 'mod+alt' | 'mod+shift' | 'alt+shift' | 'mod+alt+shift'

export type Combo = ModPrefixedCombo<BaseKey>
export type FakeCombo = ModPrefixedCombo<BaseKey | '@' | '?'>

// Human-readable keys, e.g. "mod+shift+k" returns ["⌘","⇧","K"] on macos,["Ctrl","Shift","K"] elsewhere.
export function normalizeCombo(combo: Combo): string[] {
  const parts = combo.split('+')

  return parts.map(p => labelForToken(p.trim()))
}

// Human-readable label, e.g. "mod+shift+k" returns "⌘⇧K" on macOS, "Ctrl+Shift+K" elsewhere.
export function formatCombo(combo: Combo): string {
  return normalizeCombo(combo).join(IS_MAC ? '' : '+') 
}


// like `formatCombo` but allows any input like `@`
export function formatFakeCombo(combo: FakeCombo): string {
  return normalizeCombo(combo as Combo).join(IS_MAC ? '' : '+') 
}

// True when focus is in a text-entry surface, so bare-key shortcuts don't fire
// while the user is typing.
export function isEditableTarget(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null

  return Boolean(
    el?.isContentEditable ||
      el instanceof HTMLInputElement ||
      el instanceof HTMLTextAreaElement ||
      el instanceof HTMLSelectElement
  )
}

// Combos with a primary modifier (Cmd/Ctrl) are safe to fire even while typing
// (e.g. ⌘K from the composer); bare/Shift-only combos are suppressed in inputs.
export function comboAllowedInInput(combo: Combo): boolean {
  return combo.startsWith('mod')
}
