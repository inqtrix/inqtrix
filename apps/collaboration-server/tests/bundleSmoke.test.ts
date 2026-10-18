import { spawnSync } from 'node:child_process'

import { describe, expect, it } from 'vitest'

describe('production bundle', () => {
  it('loads all bundled modules before the fail-loud configuration gate', () => {
    const result = spawnSync(process.execPath, ['dist/main.cjs'], {
      cwd: process.cwd(),
      encoding: 'utf8',
      env: {
        NODE_ENV: 'test',
        PATH: process.env.PATH ?? '',
      },
      timeout: 10_000,
    })
    const output = `${result.stdout}${result.stderr}`

    expect(result.status).toBe(1)
    expect(output).toContain('"event":"collaboration_sidecar_start_failed"')
    expect(output).not.toContain('Dynamic require')
    expect(output).not.toContain('SyntaxError')
  })
})
