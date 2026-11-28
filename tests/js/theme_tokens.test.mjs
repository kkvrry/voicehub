// @ts-check
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { test } from 'node:test'

const ROOT = path.resolve(import.meta.dirname, '..', '..')
const SETTINGS_DIR = path.join(ROOT, 'web', 'src', 'settings')

const REQUIRED_TOKENS = [
  '--sh-canvas',
  '--sh-panel',
  '--sh-chip',
  '--sh-glass',
  '--sh-glass-soft',
  '--sh-glass-strong',
  '--sh-glass-border',
  '--sh-line',
  '--sh-line-soft',
  '--sh-field',
  '--sh-field-focus',
  '--sh-hover',
  '--sh-track',
  '--sh-text',
  '--sh-text-2',
  '--sh-text-3',
  '--sh-text-4',
  '--sh-accent',
  '--sh-accent-contrast',
  '--sh-ok',
  '--sh-warn',
  '--sh-danger',
  '--sh-shadow-1',
  '--sh-shadow-2',
  '--sh-shadow-pop',
  '--sh-inset-hi',
]

/** 外观在浅色层覆盖过的表面/控件变量，在深色层必须全部对称覆盖，防止浅底盖过暗底。 */
const REQUIRED_LOOK_TOKENS = [
  '--sh-canvas',
  '--sh-panel',
  '--sh-chip',
  '--sh-glass',
  '--sh-glass-soft',
  '--sh-glass-strong',
  '--sh-glass-border',
  '--sh-line',
  '--sh-line-soft',
  '--sh-field',
  '--sh-field-focus',
  '--sh-hover',
  '--sh-track',
  '--sh-accent',
  '--sh-accent-contrast',
  '--sh-shadow-1',
  '--sh-shadow-2',
  '--sh-shadow-pop',
  '--sh-inset-hi',
]

/**
 * @param {string} css
 * @param {string} selectorPrefix
 */
function extractBlockTokens(css, selectorPrefix) {
  const escaped = selectorPrefix.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const match = css.match(new RegExp(`${escaped}\\s*\\{([\\s\\S]*?)\\}`))
  assert.ok(match, `Missing selector block: ${selectorPrefix}`)
  const tokens = new Set()
  for (const m of match[1].matchAll(/(--sh-[a-z0-9-]+)\s*:/g)) {
    tokens.add(m[1])
  }
  return tokens
}

test('base theme.css defines all required tokens in light and dark blocks', () => {
  const css = fs.readFileSync(path.join(SETTINGS_DIR, 'theme.css'), 'utf8')
  const light = extractBlockTokens(css, ':root')
  const dark = extractBlockTokens(css, ":root[data-theme='dark']")
  for (const token of REQUIRED_TOKENS) {
    assert.ok(light.has(token), `theme.css :root missing ${token}`)
    assert.ok(dark.has(token), `theme.css dark missing ${token}`)
  }
})

for (const look of ['classic', 'paper', 'celadon', 'stage']) {
  test(`look-${look}.css covers all surface/field/text tokens in light and dark`, () => {
    const css = fs.readFileSync(path.join(SETTINGS_DIR, `look-${look}.css`), 'utf8')
    const light = extractBlockTokens(css, `:root[data-look='${look}']`)
    const dark = extractBlockTokens(css, `:root[data-look='${look}'][data-theme='dark']`)
    for (const token of REQUIRED_LOOK_TOKENS) {
      assert.ok(light.has(token), `look-${look}.css light missing ${token}`)
      assert.ok(dark.has(token), `look-${look}.css dark missing ${token}`)
    }
    for (const token of light) {
      if (token.startsWith('--sh-r-') || token.startsWith('--sh-radius-') || token.startsWith('--sh-font')) {
        continue
      }
      if (token === '--sh-hit' || token === '--sh-hit-text' || token.startsWith('--sh-media-')) {
        continue
      }
      assert.ok(dark.has(token), `look-${look}.css dark missing light-overridden ${token}`)
    }
  })
}

test('components do not hardcode hex fallbacks inside var(--sh-*, #...)', () => {
  const srcDir = path.join(ROOT, 'web', 'src')
  /** @type {string[]} */
  const offenders = []
  /** @param {string} dir */
  function walk(dir) {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name)
      if (entry.isDirectory()) {
        walk(full)
        continue
      }
      if (!entry.name.endsWith('.vue') && !entry.name.endsWith('.css')) continue
      if (entry.name === 'theme.css' || entry.name.startsWith('look-')) continue
      const content = fs.readFileSync(full, 'utf8')
      if (/var\(--[a-z0-9-]+,\s*#[0-9a-fA-F]{3,8}\)/.test(content)) {
        offenders.push(path.relative(ROOT, full))
      }
    }
  }
  walk(srcDir)
  assert.deepEqual(offenders, [])
})
