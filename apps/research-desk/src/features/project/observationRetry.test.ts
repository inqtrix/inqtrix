import { describe, expect, it, vi } from 'vitest'

import {
  createStoppedObservations,
  OBSERVATION_MAX_FAILURES,
  observationRetryDelay,
} from './observationRetry'

describe('observationRetryDelay', () => {
  it('doubles from the base to a 30 s cap and stops after ten failures in a row', () => {
    const delays = Array.from(
      { length: OBSERVATION_MAX_FAILURES },
      (_, index) => observationRetryDelay(index + 1, 1_500),
    )

    expect(delays).toEqual([
      1_500, 3_000, 6_000, 12_000, 24_000, 30_000, 30_000, 30_000, 30_000, null,
    ])
  })

  it('starts from the base of the poll that failed', () => {
    expect(observationRetryDelay(1, 750)).toBe(750)
    expect(observationRetryDelay(2, 750)).toBe(1_500)
    expect(observationRetryDelay(9, 750)).toBe(30_000)
    expect(observationRetryDelay(10, 750)).toBeNull()
  })
})

class Tab extends EventTarget {
  visible = true
}

function stoppedObservations() {
  const online = new EventTarget()
  const tab = new Tab()
  const onChange = vi.fn()
  const stopped = createStoppedObservations(onChange, {
    isVisible: () => tab.visible,
    online,
    visibility: tab,
  })
  return { onChange, online, stopped, tab }
}

describe('createStoppedObservations', () => {
  it('starts every stopped poll once when the browser is online again', () => {
    const { onChange, online, stopped } = stoppedObservations()
    const first = vi.fn()
    const second = vi.fn()

    stopped.stop('a', first)
    stopped.stop('b', second)
    expect(onChange).toHaveBeenLastCalledWith(true)

    online.dispatchEvent(new Event('online'))
    online.dispatchEvent(new Event('online'))

    expect(first).toHaveBeenCalledTimes(1)
    expect(second).toHaveBeenCalledTimes(1)
    expect(onChange).toHaveBeenLastCalledWith(false)
  })

  it('starts a stopped poll when the tab becomes visible, not while it is hidden', () => {
    const { online, stopped, tab } = stoppedObservations()
    const restart = vi.fn()
    tab.visible = false

    stopped.stop('a', restart)
    tab.dispatchEvent(new Event('visibilitychange'))
    expect(restart).not.toHaveBeenCalled()

    tab.visible = true
    tab.dispatchEvent(new Event('visibilitychange'))
    online.dispatchEvent(new Event('online'))

    expect(restart).toHaveBeenCalledTimes(1)
  })

  it('keeps the latest restart of a poll that stopped twice', () => {
    const { online, stopped } = stoppedObservations()
    const stale = vi.fn()
    const latest = vi.fn()

    stopped.stop('a', stale)
    stopped.stop('a', latest)
    online.dispatchEvent(new Event('online'))

    expect(stale).not.toHaveBeenCalled()
    expect(latest).toHaveBeenCalledTimes(1)
  })

  it('never starts a poll it dropped or cleared', () => {
    const { onChange, online, stopped } = stoppedObservations()
    const dropped = vi.fn()
    const cleared = vi.fn()

    stopped.stop('a', dropped)
    stopped.drop('a')
    expect(onChange).toHaveBeenLastCalledWith(false)
    stopped.stop('b', cleared)
    stopped.clear()
    expect(onChange).toHaveBeenLastCalledWith(false)
    online.dispatchEvent(new Event('online'))

    expect(dropped).not.toHaveBeenCalled()
    expect(cleared).not.toHaveBeenCalled()
  })

  it('listens only while a poll is stopped', () => {
    const { online, stopped, tab } = stoppedObservations()
    const addOnline = vi.spyOn(online, 'addEventListener')
    const removeOnline = vi.spyOn(online, 'removeEventListener')
    const addTab = vi.spyOn(tab, 'addEventListener')
    const removeTab = vi.spyOn(tab, 'removeEventListener')

    stopped.stop('a', vi.fn())
    stopped.stop('b', vi.fn())
    expect(addOnline).toHaveBeenCalledTimes(1)
    expect(addTab).toHaveBeenCalledTimes(1)

    stopped.drop('a')
    expect(removeOnline).not.toHaveBeenCalled()
    stopped.drop('b')
    expect(removeOnline).toHaveBeenCalledTimes(1)
    expect(removeTab).toHaveBeenCalledTimes(1)

    stopped.stop('c', vi.fn())
    online.dispatchEvent(new Event('online'))
    expect(removeOnline).toHaveBeenCalledTimes(2)
    expect(removeTab).toHaveBeenCalledTimes(2)
  })
})
