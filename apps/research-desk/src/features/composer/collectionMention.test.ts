import { describe, expect, it } from 'vitest'
import { detectCollectionMention } from './collectionMention'

describe('detectCollectionMention', () => {
  it('opens on a bare at-sign', () => {
    expect(detectCollectionMention('@', 1)).toEqual({ query: '', start: 0 })
  })

  it('opens after whitespace and keeps the typed query', () => {
    expect(detectCollectionMention('frage @eu', 9)).toEqual({ query: 'eu', start: 6 })
  })

  it('does not treat email-like text as a collection mention', () => {
    expect(detectCollectionMention('name@example.com', 16)).toBeNull()
  })

  it('detects the mention at the current caret instead of the end of the text', () => {
    expect(detectCollectionMention('@recht und mehr', 6)).toEqual({ query: 'recht', start: 0 })
  })
})
