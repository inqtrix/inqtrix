import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const source = readFileSync(new URL('../../../e2e/engines/agent-desk-live.mjs', import.meta.url), 'utf8')

/** The text of one top-level function, found by its declaration and balanced braces. */
function functionText(text, name) {
  const start = text.indexOf(`async function ${name}(`)
  assert.notEqual(start, -1, `${name} is not declared`)
  const open = text.indexOf('{', text.indexOf(')', start))
  let depth = 0
  for (let index = open; index < text.length; index += 1) {
    if (text[index] === '{') depth += 1
    if (text[index] === '}') {
      depth -= 1
      if (depth === 0) return text.slice(start, index + 1)
    }
  }
  throw new Error(`${name} has no closing brace`)
}

test('the live runner opens the run setup only through selectRunSetup', () => {
  // A click on the trigger while the menu still fades out after Escape is
  // swallowed: the menu opens and closes at once. selectRunSetup returns only
  // once its menu is gone; a hand-written click sequence beside it did not
  // wait, and the next choice of the run setup never opened its menu.
  const helper = functionText(source, 'selectRunSetup')
  const rest = source.replace(helper, '')
  assert.equal(rest.match(/\.agent-run-setup-trigger['"]\)\s*\.click\(/g), null)
  assert.equal(rest.match(/name: ['"]Ausführung['"]/g), null)
  assert.match(helper, /\.agent-run-setup-trigger'\)\.click\(\)/)
  assert.match(helper, /getByRole\('menu'\)\.first\(\)\.waitFor\(\{ state: 'detached'/)
})
