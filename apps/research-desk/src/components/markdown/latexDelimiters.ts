/**
 * Brings LaTeX bracket delimiters onto the dollar forms the math plugin
 * understands: a `\[ … \]` block that stands on its own lines becomes
 * display math, an inline `\( … \)` becomes `$ … $`.
 *
 * Everything else is left exactly as written. Fenced code and inline code
 * are never touched, and a `\[` inside a sentence stays a literal bracket
 * — that is how Markdown escapes a bracket, and a reference title such as
 * `Bericht \[2024\]` must not turn into an equation.
 */

const FENCE_OPEN = /^ {0,3}(`{3,}|~{3,})/
const INLINE_CODE = /(`+)[^`][\s\S]*?\1/g
const INLINE_MATH = /\\\((.+?)\\\)/g
/** Private-use code point that never occurs in prose; brackets a masked
 * code span's slot index while the line is transformed. */
const SLOT = ''
const CODE_SPAN_SLOT = /(\d+)/g
const DISPLAY_OPEN = '\\['
const DISPLAY_CLOSE = '\\]'

type Fence = { character: string; width: number }

export function normalizeLatexDelimiters(markdown: string): string {
  if (!markdown.includes(DISPLAY_OPEN) && !markdown.includes('\\(')) return markdown
  const out: string[] = []
  let fence: Fence | null = null
  let display: string[] | null = null

  const flushAsProse = () => {
    if (!display) return
    for (const line of display) out.push(inlineOnLine(line))
    display = null
  }

  for (const line of markdown.split('\n')) {
    if (fence) {
      out.push(line)
      if (closesFence(line, fence)) fence = null
      continue
    }
    const opener = FENCE_OPEN.exec(line)
    if (opener) {
      flushAsProse()
      fence = { character: opener[1][0], width: opener[1].length }
      out.push(line)
      continue
    }
    const trimmed = line.trim()
    if (display) {
      if (trimmed === '') {
        // A blank line ends the paragraph — an unclosed bracket was prose.
        flushAsProse()
        out.push(line)
        continue
      }
      display.push(line)
      if (trimmed.endsWith(DISPLAY_CLOSE)) {
        out.push(...displayBlock(display))
        display = null
      }
      continue
    }
    if (trimmed.startsWith(DISPLAY_OPEN)) {
      if (trimmed.length > DISPLAY_OPEN.length + DISPLAY_CLOSE.length && trimmed.endsWith(DISPLAY_CLOSE)) {
        out.push(...displayBlock([line]))
      } else {
        display = [line]
      }
      continue
    }
    out.push(inlineOnLine(line))
  }
  flushAsProse()
  return out.join('\n')
}

function closesFence(line: string, fence: Fence): boolean {
  const stripped = line.replace(/^ {0,3}/, '')
  let width = 0
  while (width < stripped.length && stripped[width] === fence.character) width += 1
  return width >= fence.width && stripped.slice(width).trim() === ''
}

/** `\[ … \]` on its own lines → `$$` block, expression trimmed. */
function displayBlock(lines: readonly string[]): string[] {
  const joined = lines.map((line) => line.trim()).join('\n')
  const expression = joined.slice(DISPLAY_OPEN.length, joined.length - DISPLAY_CLOSE.length).trim()
  return ['$$', expression, '$$']
}

/** `\( … \)` → `$ … $` outside the inline code spans of one line. */
function inlineOnLine(line: string): string {
  if (!line.includes('\\(')) return line
  const spans: string[] = []
  const masked = line.replace(INLINE_CODE, (span) => {
    spans.push(span)
    return `${SLOT}${spans.length - 1}${SLOT}`
  })
  const converted = masked.replace(INLINE_MATH, (_match, expression: string) => `$${expression.trim()}$`)
  return converted.replace(CODE_SPAN_SLOT, (_match, index: string) => spans[Number(index)])
}
