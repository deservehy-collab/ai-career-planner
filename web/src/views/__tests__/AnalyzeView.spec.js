/**
 * 岗位分析页：停止分析（保留档案 / 连同档案删除）。
 *
 * 锁住的契约：
 * 1. 只有「正在跑 + 有档案归属」时才出现「停止分析」按钮；
 * 2. 点击先出二次确认，两条路都摆明后果：只停止保留档案，或连档案一起永久删除；
 * 3. 「继续分析」不动数据，两个接口都不调；
 * 4. 「只停止分析」只调 cancelPlanRun，档案、轮次、任务、成长全部保留；
 * 5. 「停止并删除」才调 deletePlan，并说明级联删除且无法撤销；
 * 6. 取消请求失败时如实说"可能还在后台跑"，不 reset、不谎报已停止。
 *
 * 后端"取消后不落盘"由 Python 测试覆盖；这里只验证界面与请求边界。
 */

import { flushPromises, shallowMount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../../api/endpoints', () => ({
  cancelPlanRun: vi.fn(),
  createPlan: vi.fn(),
  deletePlan: vi.fn(),
  getAccountProfile: vi.fn(),
  getMe: vi.fn(),
  getRun: vi.fn(),
  listPlans: vi.fn(),
  login: vi.fn(),
  register: vi.fn(),
  saveAccountProfile: vi.fn(),
  startAnalyzeAndSave: vi.fn(),
  startPlanAnalysis: vi.fn(),
}))

import { cancelPlanRun, deletePlan, listPlans } from '../../api/endpoints'
import AnalyzeView from '../AnalyzeView.vue'
import { useAnalysisStore } from '../../stores/analysis'
import { useAuthStore } from '../../stores/auth'
import { usePlansStore } from '../../stores/plans'
import { useProfileStore } from '../../stores/profile'

const PLAN_A = {
  id: 'plan-a',
  title: '会计',
  target_job: '会计',
  counts: { rounds: 0, tasks: 0, growth: 0 },
  current_skills: [],
}

const PLAN_B = {
  id: 'plan-b',
  title: 'Python 后端',
  target_job: 'Python 后端开发',
  latest_round: { round_no: 2 },
  counts: { rounds: 2, tasks: 5, growth: 3 },
  current_skills: [],
}

function seedPlans(items, activeId) {
  const plans = usePlansStore()
  plans.items = items
  plans.total = items.length
  if (activeId) plans.select(activeId)
  return plans
}

function runningAnalysis(planId = 'plan-a', targetJob = '会计') {
  const analysis = useAnalysisStore()
  analysis.status = 'running'
  analysis.runId = 'run-1'
  analysis.planId = planId
  analysis.targetJob = targetJob
  return analysis
}

function mountAnalyze() {
  return shallowMount(AnalyzeView)
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  vi.stubGlobal('fetch', vi.fn())
  const auth = useAuthStore()
  auth.token = 'test-token'
  auth.user = { id: 7, username: 'alice' }
  useProfileStore().targetJob = '会计'
  listPlans.mockResolvedValue({ items: [], total: 0 })
  deletePlan.mockResolvedValue(undefined)
  cancelPlanRun.mockResolvedValue({ canceled: true, status: 'running' })
})

describe('AnalyzeView 停止分析按钮的可见性', () => {
  it('正在跑且有档案归属时出现停止按钮', () => {
    seedPlans([structuredClone(PLAN_A)], 'plan-a')
    runningAnalysis()
    const wrapper = mountAnalyze()

    expect(wrapper.find('.an-stop').exists()).toBe(true)
    expect(wrapper.get('.an-stop').text()).toBe('停止分析')
  })

  it('没有档案归属（planId 为空）时不出现停止按钮', () => {
    seedPlans([], null)
    runningAnalysis(null, '会计')
    const wrapper = mountAnalyze()

    expect(wrapper.find('.an-stop').exists()).toBe(false)
  })

  it('分析不在运行态时不出现停止按钮', () => {
    seedPlans([structuredClone(PLAN_A)], 'plan-a')
    const analysis = runningAnalysis()
    analysis.status = 'idle'
    const wrapper = mountAnalyze()

    expect(wrapper.find('.an-stop').exists()).toBe(false)
  })
})

describe('AnalyzeView 停止分析的二次确认', () => {
  it('两条路都摆出来：保留档案是主操作，删除档案单独说明后果；继续分析不调任何接口', async () => {
    seedPlans([structuredClone(PLAN_A)], 'plan-a')
    runningAnalysis()
    const wrapper = mountAnalyze()

    await wrapper.get('.an-stop').trigger('click')
    const confirm = wrapper.get('.an-stop-confirm')
    expect(confirm.text()).toContain('停止「会计」这次分析？')
    expect(confirm.text()).toContain('0 轮分析')
    expect(confirm.text()).toContain('0 项任务')
    expect(confirm.text()).toContain('0 条成长记录')
    expect(confirm.text()).toContain('原样保留')
    expect(confirm.get('.an-stop-keep').text()).toContain('只停止分析（保留档案）')
    expect(confirm.get('.an-stop-delete').text()).toContain('停止并删除整份档案')
    expect(confirm.text()).toContain('无法撤销')
    expect(confirm.text()).toContain('其他档案不受影响')
    expect(cancelPlanRun).not.toHaveBeenCalled()
    expect(deletePlan).not.toHaveBeenCalled()

    await confirm.findAll('button')[0].trigger('click')
    expect(wrapper.find('.an-stop-confirm').exists()).toBe(false)
    expect(cancelPlanRun).not.toHaveBeenCalled()
    expect(deletePlan).not.toHaveBeenCalled()
  })

  it('预览已有档案时确认面板如实摆出已有的轮次/任务/成长计数', async () => {
    seedPlans([structuredClone(PLAN_B)], 'plan-b')
    runningAnalysis('plan-b', 'Python 后端开发')
    const wrapper = mountAnalyze()

    await wrapper.get('.an-stop').trigger('click')
    const confirm = wrapper.get('.an-stop-confirm')
    expect(confirm.text()).toContain('2 轮分析')
    expect(confirm.text()).toContain('5 项任务')
    expect(confirm.text()).toContain('3 条成长记录')
  })
})

describe('AnalyzeView 只停止分析', () => {
  it('只调 cancelPlanRun，档案与已有记录原样保留，分析回 idle', async () => {
    const plans = seedPlans([structuredClone(PLAN_A), structuredClone(PLAN_B)], 'plan-a')
    const analysis = runningAnalysis()
    const wrapper = mountAnalyze()

    await wrapper.get('.an-stop').trigger('click')
    await wrapper.get('.an-stop-confirm .an-stop-keep').trigger('click')
    await flushPromises()

    expect(cancelPlanRun).toHaveBeenCalledWith('plan-a', 'run-1')
    expect(deletePlan).not.toHaveBeenCalled()
    expect(plans.items).toHaveLength(2)
    expect(plans.activeId).toBe('plan-a')
    expect(analysis.status).toBe('idle')
    expect(analysis.isRunning).toBe(false)
    expect(wrapper.find('.an-stop-confirm').exists()).toBe(false)
    expect(wrapper.find('.an-stop').exists()).toBe(false)
    expect(wrapper.get('.an-stop-notice').text()).toContain('原样保留')
    expect(wrapper.get('.an-stop-notice').text()).toContain('会计')
  })

  it('分析在点停止前已经跑完时，如实说结果已保存而不是谎报停止成功', async () => {
    seedPlans([structuredClone(PLAN_A)], 'plan-a')
    const analysis = runningAnalysis()
    cancelPlanRun.mockResolvedValue({ canceled: false, status: 'succeeded' })
    const wrapper = mountAnalyze()

    await wrapper.get('.an-stop').trigger('click')
    await wrapper.get('.an-stop-confirm .an-stop-keep').trigger('click')
    await flushPromises()

    expect(deletePlan).not.toHaveBeenCalled()
    expect(analysis.status).toBe('idle')
    expect(wrapper.get('.an-stop-notice').text()).toContain('已经跑完')
    expect(wrapper.get('.an-stop-notice').text()).toContain('已保存')
  })

  it('停止请求失败时保持运行态并说明可能还在后台跑，可重试', async () => {
    seedPlans([structuredClone(PLAN_A)], 'plan-a')
    const analysis = runningAnalysis()
    cancelPlanRun.mockRejectedValue(new Error('网络断了'))
    const wrapper = mountAnalyze()

    await wrapper.get('.an-stop').trigger('click')
    await wrapper.get('.an-stop-confirm .an-stop-keep').trigger('click')
    await flushPromises()

    expect(cancelPlanRun).toHaveBeenCalledWith('plan-a', 'run-1')
    expect(deletePlan).not.toHaveBeenCalled()
    expect(analysis.isRunning).toBe(true)
    expect(wrapper.find('.an-stop-confirm').exists()).toBe(true)
    expect(wrapper.get('.an-stop-error').text()).toContain('停止没有送达服务')
    expect(wrapper.get('.an-stop-error').text()).toContain('可能还在后台跑')
    expect(wrapper.find('.an-stop-notice').exists()).toBe(false)
  })
})

describe('AnalyzeView 停止并删除档案', () => {
  it('确认后删除当前分析档案，分析回 idle，落到剩下的档案并给成功提示', async () => {
    const plans = seedPlans([structuredClone(PLAN_A), structuredClone(PLAN_B)], 'plan-a')
    const analysis = runningAnalysis()
    listPlans.mockResolvedValue({ items: [structuredClone(PLAN_B)], total: 1 })
    const wrapper = mountAnalyze()

    await wrapper.get('.an-stop').trigger('click')
    await wrapper.get('.an-stop-confirm .an-stop-delete').trigger('click')
    await flushPromises()

    expect(cancelPlanRun).not.toHaveBeenCalled()
    expect(deletePlan).toHaveBeenCalledWith('plan-a')
    expect(analysis.status).toBe('idle')
    expect(analysis.isRunning).toBe(false)
    expect(plans.activeId).toBe('plan-b')
    expect(wrapper.find('.an-stop-confirm').exists()).toBe(false)
    expect(wrapper.find('.an-stop').exists()).toBe(false)
    expect(wrapper.get('.an-stop-notice').text()).toContain('已停止分析并删除档案「会计」')
  })

  it('删除失败时给出错误提示，不谎报已删除', async () => {
    seedPlans([structuredClone(PLAN_A)], 'plan-a')
    runningAnalysis()
    deletePlan.mockRejectedValue(new Error('服务暂时不可用'))
    const wrapper = mountAnalyze()

    await wrapper.get('.an-stop').trigger('click')
    await wrapper.get('.an-stop-confirm .an-stop-delete').trigger('click')
    await flushPromises()

    expect(deletePlan).toHaveBeenCalledWith('plan-a')
    expect(wrapper.find('.an-stop-error').exists()).toBe(true)
    expect(wrapper.text()).toContain('档案没有删除')
  })
})