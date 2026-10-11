import { describe, expect, it, vi } from 'vitest'

const { getMock, postMock } = vi.hoisted(() => ({ getMock: vi.fn(), postMock: vi.fn() }))
vi.mock('./http', () => ({ http: { get: getMock, post: postMock } }))

import {
  getIndexJobTimingConfig,
  parseIndexJobProgress,
  retryIndexJob,
  unwrapIndexJobPayload,
} from './indexJobs'

describe('parseIndexJobProgress', () => {
  it('parses number and string', () => {
    expect(parseIndexJobProgress(42)).toBe(42)
    expect(parseIndexJobProgress('80')).toBe(80)
    expect(parseIndexJobProgress(undefined)).toBe(0)
  })
})

describe('unwrapIndexJobPayload', () => {
  it('unwraps { data: job }', () => {
    const job = { id: 'j1', status: 'running', progress: 20 }
    expect(unwrapIndexJobPayload({ data: job })).toEqual(job)
  })

  it('accepts flat job object', () => {
    const job = { id: 'j2', status: 'queued', progress: 0 }
    expect(unwrapIndexJobPayload(job)).toEqual(job)
  })
})

describe('retryIndexJob', () => {
  it('POSTs to the per-job retry endpoint (reuses the original job)', async () => {
    postMock.mockResolvedValueOnce({ data: { data: { id: 'j1', status: 'queued' } } })
    await retryIndexJob('j1')
    expect(postMock).toHaveBeenCalledTimes(1)
    expect(postMock).toHaveBeenCalledWith('/index-jobs/j1/retry')
  })
})

describe('getIndexJobTimingConfig', () => {
  it('GETs the timing config endpoint', async () => {
    getMock.mockResolvedValueOnce({
      data: { data: { job_timeout_seconds: 1800, stale_queued_seconds: 300, stale_running_seconds: 1800, stall_hint_seconds: 2100 } },
    })
    const res = await getIndexJobTimingConfig()
    expect(getMock).toHaveBeenCalledWith('/index-jobs/timing-config')
    expect(res.data?.data?.stall_hint_seconds).toBe(2100)
  })
})
