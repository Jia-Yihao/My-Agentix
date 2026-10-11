import { describe, expect, it } from 'vitest'
import {
  buildRestoreTaskFromDocument,
  canCancelUploadTask,
  canRetryUploadTask,
  displayFileName,
  displayTaskPercent,
  INDEX_FAILURE_MESSAGE,
  INDEX_STALL_HINT,
  uploadTaskStatusLabel,
  isPendingUploadDocument,
  isReadyDocument,
  mergeRestoreUploadTasks,
} from './kbUploadProgress'

describe('isReadyDocument', () => {
  it('only ready passes', () => {
    expect(isReadyDocument({ status: 'ready' })).toBe(true)
    expect(isReadyDocument({ status: 'indexing' })).toBe(false)
    expect(isReadyDocument({ status: 'failed' })).toBe(false)
  })
})

describe('isPendingUploadDocument', () => {
  it('includes draft indexing failed', () => {
    expect(isPendingUploadDocument({ status: 'draft' })).toBe(true)
    expect(isPendingUploadDocument({ status: 'indexing' })).toBe(true)
    expect(isPendingUploadDocument({ status: 'failed' })).toBe(true)
    expect(isPendingUploadDocument({ status: 'ready' })).toBe(false)
  })
})

describe('buildRestoreTaskFromDocument', () => {
  it('failed doc becomes failed task without fake 100% index', () => {
    const t = buildRestoreTaskFromDocument(
      { id: 'd1', title: 'a.pdf', status: 'failed' },
      null,
    )
    expect(t.status).toBe('failed')
    expect(t.errorMessage).toBe(INDEX_FAILURE_MESSAGE)
    expect(t.uploadProgress).toBe(100)
    expect(t.indexProgress).toBe(0)
  })

  it('indexing doc uses job progress', () => {
    const t = buildRestoreTaskFromDocument(
      { id: 'd2', title: 'b.pdf', status: 'indexing', folder_path: 'dir' },
      { id: 'j1', status: 'running', progress: 42 },
    )
    expect(t.status).toBe('indexing')
    expect(t.jobId).toBe('j1')
    expect(t.indexProgress).toBe(42)
    expect(t.fileName).toBe('b.pdf')
  })

  it('indexing doc with failed job stays indexing', () => {
    const t = buildRestoreTaskFromDocument(
      { id: 'd3', title: 'c.pdf', status: 'indexing' },
      { id: 'j2', status: 'failed', progress: 80 },
    )
    expect(t.status).toBe('indexing')
    expect(t.indexProgress).toBe(80)
  })
})

describe('displayFileName', () => {
  it('uses basename for legacy path title', () => {
    expect(displayFileName({ title: '资料/报告.pdf', folder_path: null })).toBe('报告.pdf')
  })
})

describe('mergeRestoreUploadTasks', () => {
  it('replaces same documentId', () => {
    const merged = mergeRestoreUploadTasks(
      [{ id: 'a', documentId: 'd1' }],
      [{ id: 'b', documentId: 'd1' }],
    )
    expect(merged).toHaveLength(1)
    expect(merged[0].id).toBe('b')
  })
})

describe('displayTaskPercent', () => {
  it('uses upload progress while uploading', () => {
    expect(
      displayTaskPercent({ status: 'uploading', uploadProgress: 55, indexProgress: 0 }),
    ).toBe(55)
  })

  it('uses index progress after upload', () => {
    expect(
      displayTaskPercent({ status: 'indexing', uploadProgress: 100, indexProgress: 42 }),
    ).toBe(42)
  })

  it('uses index progress when job already running but status not flipped', () => {
    expect(
      displayTaskPercent({
        status: 'uploading',
        uploadProgress: 0,
        indexProgress: 50,
        jobId: 'j1',
      }),
    ).toBe(50)
  })

  it('caps failed at 99 when index was 100', () => {
    expect(
      displayTaskPercent({ status: 'failed', uploadProgress: 100, indexProgress: 100 }),
    ).toBe(99)
  })
})

describe('uploadTaskStatusLabel', () => {
  it('shows 处理中 when index progress is available', () => {
    expect(
      uploadTaskStatusLabel({
        status: 'uploading',
        uploadProgress: 0,
        indexProgress: 50,
        jobId: 'j1',
      }),
    ).toBe('处理中')
  })
})

describe('uploadTaskStatusLabel（生产单源）', () => {
  it('failed shows 处理失败', () => {
    expect(
      uploadTaskStatusLabel({
        status: 'failed',
        jobId: 'j1',
        uploadProgress: 100,
        indexProgress: 0,
      }),
    ).toBe(INDEX_FAILURE_MESSAGE)
  })

  it('succeeded shows 完成', () => {
    expect(
      uploadTaskStatusLabel({
        status: 'succeeded',
        uploadProgress: 100,
        indexProgress: 100,
      }),
    ).toBe('完成')
  })
})

describe('canCancelUploadTask', () => {
  it('allows cancel for active and failed', () => {
    expect(canCancelUploadTask('uploading')).toBe(true)
    expect(canCancelUploadTask('failed')).toBe(true)
    expect(canCancelUploadTask('succeeded')).toBe(false)
  })
})

describe('canRetryUploadTask', () => {
  it('failed task with jobId can retry', () => {
    expect(canRetryUploadTask({ status: 'failed', jobId: 'j1' })).toBe(true)
  })

  it('failed task without jobId but with documentId can retry (resolved at click time)', () => {
    expect(canRetryUploadTask({ status: 'failed', documentId: 'd1' })).toBe(true)
  })

  it('cannot retry without any identifier (backend retry reuses the original job)', () => {
    expect(canRetryUploadTask({ status: 'failed' })).toBe(false)
  })

  it('healthy indexing task hides retry to avoid a guaranteed 400', () => {
    expect(canRetryUploadTask({ status: 'indexing', jobId: 'j1' })).toBe(false)
  })

  it('stale indexing task exposes retry once the stall hint is shown', () => {
    expect(
      canRetryUploadTask({ status: 'indexing', jobId: 'j1', errorMessage: INDEX_STALL_HINT }),
    ).toBe(true)
  })

  it('poll-failure hint alone is not enough to retry', () => {
    expect(
      canRetryUploadTask({ status: 'indexing', jobId: 'j1', errorMessage: '暂时无法获取索引进度，正在自动重试…' }),
    ).toBe(false)
  })

  it('succeeded task never offers retry', () => {
    expect(canRetryUploadTask({ status: 'succeeded', jobId: 'j1' })).toBe(false)
  })
})

describe('buildRestoreTaskFromDocument jobId carry-over', () => {
  it('failed doc keeps jobId so the retry entry survives a page reload', () => {
    const t = buildRestoreTaskFromDocument(
      { id: 'd9', title: 'f.pdf', status: 'failed' },
      { id: 'j9', status: 'failed', progress: 0 },
    )
    expect(t.status).toBe('failed')
    expect(t.jobId).toBe('j9')
    expect(canRetryUploadTask(t)).toBe(true)
  })

  it('failed doc without jobId still retries via documentId fallback', () => {
    const t = buildRestoreTaskFromDocument(
      { id: 'd10', title: 'g.pdf', status: 'failed' },
      null,
    )
    expect(t.jobId).toBeUndefined()
    expect(t.documentId).toBe('d10')
    // 点击重试时按 documentId 补拉 latest job；拉不到则静默不动
    expect(canRetryUploadTask(t)).toBe(true)
  })

  it('task without any identifier never offers retry', () => {
    expect(canRetryUploadTask({ status: 'failed' })).toBe(false)
    expect(canRetryUploadTask({ status: 'indexing' })).toBe(false)
  })
})
