/** 分析结果里的"模型通道提示"必须原样交给界面（P1 后端 llm_warnings 的前端落点）。 */

import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { getRun } from '../../api/endpoints'
import { useAnalysisStore } from '../analysis'

vi.mock('../../api/endpoints', () => ({
  getRun: vi.fn(),
  startAnalyzeAndSave: vi.fn(),
}))

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

describe('useAnalysisStore.llmWarnings', () => {
  it('把后端降级提示暴露给分析页', () => {
    const store = useAnalysisStore()
    const warning = '云端模型被限流（429），本次已改用本地模型 qwen2.5:3b。'
    store.result = { llm_warnings: [warning] }

    expect(store.llmWarnings).toEqual([warning])
  })

  it('旧结果里没有 llm_warnings 时返回空数组，不报错也不伪造提示', () => {
    const store = useAnalysisStore()
    store.result = { user_skills: [] }

    expect(store.llmWarnings).toEqual([])
  })
})

describe('useAnalysisStore.progress', () => {
  it('把后端回报的节点进度原样暴露给界面', async () => {
    getRun.mockResolvedValue({
      status: 'running',
      progress: [
        { node: 'profile', status: 'done' },
        { node: 'growth', status: 'running' },
      ],
      current_node: 'growth',
    })
    const store = useAnalysisStore()

    await store.fetchOnce('run-progress')

    expect(store.progress.map((item) => item.node)).toEqual(['profile', 'growth'])
    expect(store.currentNode).toBe('growth')
  })

  it('旧记录没有 progress 字段时给空进度，不伪造节点状态', async () => {
    getRun.mockResolvedValue({ status: 'running' })
    const store = useAnalysisStore()

    await store.fetchOnce('run-old')

    expect(store.progress).toEqual([])
    expect(store.currentNode).toBeNull()
  })

  it('reset 清空进度（下一次分析从头开始）', () => {
    const store = useAnalysisStore()
    store.progress = [{ node: 'profile', status: 'done' }]
    store.currentNode = 'profile'

    store.reset()

    expect(store.progress).toEqual([])
    expect(store.currentNode).toBeNull()
  })
})

describe('useAnalysisStore.roundInfo', () => {
  it('预览结果解包出 analysis，并标记 isPreview', async () => {
    getRun.mockResolvedValue({
      status: 'succeeded',
      finished_at: '2026-10-23T10:00:00',
      result: {
        new_plan: false,
        round_created: false,
        round_no: null,
        existing_round_no: 2,
        target_job: 'AI工程师',
        goal: { status: 'in_progress' },
        warning: '当前已有第 2 轮计划，本次分析只做预览，任务清单没有改动。',
        analysis: { user_skills: [] },
      },
    })
    const store = useAnalysisStore()

    await store.fetchOnce('run-1')

    expect(store.result).toEqual({ user_skills: [] })
    expect(store.isPreview).toBe(true)
    expect(store.roundInfo.roundNo).toBeNull()
    expect(store.roundWarning).toContain('预览')
  })

  it('新计划结果带出新轮次号，且不算预览', async () => {
    getRun.mockResolvedValue({
      status: 'succeeded',
      finished_at: '2026-10-23T10:00:00',
      result: {
        new_plan: true,
        round_created: true,
        round_no: 3,
        existing_round_no: 2,
        target_job: 'AI工程师',
        goal: { status: 'in_progress' },
        warning: '',
        analysis: { user_skills: [] },
      },
    })
    const store = useAnalysisStore()

    await store.fetchOnce('run-2')

    expect(store.isPreview).toBe(false)
    expect(store.roundInfo.roundCreated).toBe(true)
    expect(store.roundInfo.roundNo).toBe(3)
  })
})
