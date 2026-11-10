/**
 * 任务页：一键完成与可选凭证。
 *
 * 锁住的契约：
 * 1. 待完成任务点「标记完成」直接完成，请求体不强制带 evidence / note；
 * 2. 完成任务可以带可选凭证或心得，两者都不填也能提交；
 * 3. 自述完成显示"自述完成"并给「补凭证」入口；
 * 4. 补凭证单独走 attachTaskEvidence，成功后刷新清单。
 */

import { h } from 'vue'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

enableAutoUnmount(afterEach)

vi.mock('../../api/endpoints', () => ({
  attachTaskEvidence: vi.fn(),
  clearTasks: vi.fn(),
  completeTask: vi.fn(),
  getPending: vi.fn(),
  listTasks: vi.fn(),
}))

import {
  attachTaskEvidence,
  completeTask,
  getPending,
  listTasks,
} from '../../api/endpoints'
import { usePlansStore } from '../../stores/plans'
import TasksView from '../TasksView.vue'

const RouterLinkStub = {
  props: ['to'],
  setup(props, { slots }) {
    const path = typeof props.to === 'string' ? props.to : (props.to?.path ?? '#')
    return () => h('a', { href: path }, slots.default?.())
  },
}

const PLAN_ID = 'plan-a'

const PLAN_ITEM = {
  id: PLAN_ID,
  title: 'Python 后端',
  target_job: 'Python 后端开发',
  latest_round: { round_no: 1 },
  counts: { rounds: 1, tasks: 2, growth: 0 },
  current_skills: [{ skill: 'Python', level: '入门' }],
}

function tasksPayload(overrides = {}) {
  return {
    store_status: 'ok',
    writable: true,
    rounds: [
      { round: 1, target_job: 'Python 后端开发', task_count: 2, completed_count: 1 },
    ],
    latest_round: {
      round: 1,
      target_job: 'Python 后端开发',
      learning_plan: [{ topic: '数据结构', covers: ['Python'], stage: 1 }],
      project_plan: [],
      snapshot: null,
      tasks: [],
    },
    latest_goal: null,
    pending_next_round: null,
    tasks: [
      {
        task_id: 'r001-l01',
        kind: 'learning',
        round: 1,
        index: 1,
        title: '把数据结构练熟',
        skills: ['Python'],
        status: 'pending',
        created_at: '2026-11-01T00:00:00Z',
        completed_at: null,
        evidence: '',
        evidence_kind: '',
      },
      {
        task_id: 'r001-l02',
        kind: 'learning',
        round: 1,
        index: 2,
        title: '做完单元测试',
        skills: ['Python'],
        status: 'completed',
        created_at: '2026-11-01T00:00:00Z',
        completed_at: '2026-11-05T00:00:00Z',
        evidence: '',
        evidence_kind: 'self_report',
        note: '终于跑通了',
      },
    ],
    summary: { total: 2, completed: 1, pending: 1 },
    ...overrides,
  }
}

function mountTasks(props = {}) {
  const plans = usePlansStore()
  plans.items = [structuredClone(PLAN_ITEM)]
  plans.select(PLAN_ID)
  return mount(TasksView, {
    props: { tab: 'tasks', ...props },
    global: { stubs: { RouterLink: RouterLinkStub, Teleport: true } },
  })
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  getPending.mockResolvedValue({ events: [], tasks: [], restored: true })
  listTasks.mockResolvedValue(tasksPayload())
  completeTask.mockResolvedValue({
    completion_status: 'completed',
    completion_written: true,
    already_completed: false,
    growth_event: { evidence_kind: 'self_report', evidence: '' },
    growth_event_status: 'ok',
    growth_evaluated: true,
    growth_outcome: 'upgraded',
    upgraded: true,
    updates: [],
    blocked: [],
    warnings: [],
  })
  attachTaskEvidence.mockResolvedValue({
    status: 'attached',
    evidence_kind: 'growth',
    verified: true,
    verification_written: true,
    pending_next_round: false,
  })
})

describe('TasksView 一键完成', () => {
  it('待完成任务点「标记完成」直接提交，不强制填证据或心得', async () => {
    const wrapper = mountTasks()
    await flushPromises()

    const row = wrapper.findAll('.reg-row').find((item) => item.text().includes('把数据结构练熟'))
    expect(row).toBeTruthy()

    await row.get('.reg-complete').trigger('click')
    await flushPromises()

    expect(completeTask).toHaveBeenCalledTimes(1)
    const [taskId, body, planId] = completeTask.mock.calls[0]
    expect(taskId).toBe('r001-l01')
    expect(planId).toBe(PLAN_ID)
    expect(body.evidence).toBeUndefined()
    expect(body.note).toBeUndefined()
    expect(body.profile_skills).toEqual([{ skill: 'Python', level: '入门' }])
  })

  it('可选凭证与心得都不填时，弹层也能提交', async () => {
    const wrapper = mountTasks()
    await flushPromises()

    const row = wrapper.findAll('.reg-row').find((item) => item.text().includes('把数据结构练熟'))
    await row.get('.reg-more').trigger('click')
    await flushPromises()

    expect(wrapper.find('.reg-sheet').exists()).toBe(true)
    await wrapper.get('.reg-sheet__foot .btn-primary').trigger('click')
    await flushPromises()

    expect(completeTask).toHaveBeenCalledTimes(1)
    expect(completeTask.mock.calls[0][1].evidence).toBeUndefined()
  })

  it('填了心得就单独提交 note，不并入 evidence', async () => {
    const wrapper = mountTasks()
    await flushPromises()

    const row = wrapper.findAll('.reg-row').find((item) => item.text().includes('把数据结构练熟'))
    await row.get('.reg-more').trigger('click')
    await wrapper.find('#tv-note').setValue('删除旋转卡了两天')

    await wrapper.get('.reg-sheet__foot .btn-primary').trigger('click')
    await flushPromises()

    const body = completeTask.mock.calls[0][1]
    expect(body.note).toBe('删除旋转卡了两天')
    expect(body.evidence).toBeUndefined()
  })

  it('一键完成失败时把错误显示在清单上，按钮恢复可用', async () => {
    completeTask.mockRejectedValue(new Error('请求失败（422）：证据不能为空'))
    const wrapper = mountTasks()
    await flushPromises()

    const row = wrapper.findAll('.reg-row').find((item) => item.text().includes('把数据结构练熟'))
    await row.get('.reg-complete').trigger('click')
    await flushPromises()

    const alert = wrapper.get('[role="alert"]')
    expect(alert.text()).toContain('证据不能为空')
    expect(row.get('.reg-complete').attributes('disabled')).toBeUndefined()
  })

  it('重试成功后清掉上一次的一键完成错误', async () => {
    completeTask.mockRejectedValueOnce(new Error('请求失败（422）：证据不能为空'))
    const wrapper = mountTasks()
    await flushPromises()

    const row = wrapper.findAll('.reg-row').find((item) => item.text().includes('把数据结构练熟'))
    await row.get('.reg-complete').trigger('click')
    await flushPromises()
    expect(wrapper.find('[role="alert"]').exists()).toBe(true)

    await row.get('.reg-complete').trigger('click')
    await flushPromises()

    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
  })

  it('自述完成的任务显示「自述完成」并给补凭证入口', async () => {
    const wrapper = mountTasks()
    await flushPromises()

    const row = wrapper.findAll('.reg-row').find((item) => item.text().includes('做完单元测试'))
    expect(row.text()).toContain('自述完成')
    expect(row.text()).toContain('未附凭证')

    await row.get('.reg-attach').trigger('click')
    await flushPromises()
    expect(wrapper.find('#tv-attach-evidence').exists()).toBe(true)

    await wrapper.get('#tv-attach-evidence').setValue('https://github.com/demo/tests')
    await wrapper.get('.reg-sheet__foot .btn-primary').trigger('click')
    await flushPromises()

    expect(attachTaskEvidence).toHaveBeenCalledWith(
      'r001-l02',
      { evidence: 'https://github.com/demo/tests' },
      PLAN_ID,
    )
    expect(listTasks).toHaveBeenCalledTimes(2)
  })

  it('任务行只显示计划明细里真有的难度或预计周数，旧轮次不编一个', async () => {
    const payload = tasksPayload()
    payload.latest_round = {
      ...payload.latest_round,
      learning_plan: [
        { topic: '数据结构', covers: ['Python'], stage: 1, estimated_weeks: 2 },
      ],
      project_plan: [{ name: '项目甲', tech_stack: ['Python'], difficulty: '中等' }],
    }
    payload.tasks.push(
      {
        task_id: 'r001-p01',
        kind: 'project',
        round: 1,
        index: 1,
        title: '做完项目甲',
        skills: ['Python'],
        status: 'pending',
        created_at: '2026-11-01T00:00:00Z',
        completed_at: null,
        evidence: '',
        evidence_kind: '',
      },
      {
        task_id: 'r000-l01',
        kind: 'learning',
        round: 0,
        index: 1,
        title: '旧轮次的任务',
        skills: [],
        status: 'pending',
        created_at: '2026-10-01T00:00:00Z',
        completed_at: null,
        evidence: '',
        evidence_kind: '',
      },
    )
    listTasks.mockResolvedValue(payload)

    const wrapper = mountTasks()
    await flushPromises()

    const learningRow = wrapper.findAll('.reg-row').find((item) => item.text().includes('把数据结构练熟'))
    expect(learningRow.get('.task-load').text()).toBe('约 2 周')
    const projectRow = wrapper.findAll('.reg-row').find((item) => item.text().includes('做完项目甲'))
    expect(projectRow.get('.task-load').text()).toBe('中等')
    const oldRow = wrapper.findAll('.reg-row').find((item) => item.text().includes('旧轮次的任务'))
    expect(oldRow.find('.task-load').exists()).toBe(false)
  })

  it('已附凭证的任务不再显示补凭证按钮', async () => {
    listTasks.mockResolvedValue(tasksPayload({
      tasks: tasksPayload().tasks.map((task) => (
        task.task_id === 'r001-l02'
          ? { ...task, evidence: 'https://github.com/demo/tests', evidence_kind: 'growth' }
          : task
      )),
    }))
    const wrapper = mountTasks()
    await flushPromises()

    const row = wrapper.findAll('.reg-row').find((item) => item.text().includes('做完单元测试'))
    expect(row.text()).toContain('已附凭证')
    expect(row.find('.reg-attach').exists()).toBe(false)
  })
})

describe('TasksView 完成离场动画', () => {
  it('标记完成后先留在原地播放离场动画，动画结束才刷新清单', async () => {
    const wrapper = mountTasks()
    await flushPromises()

    const row = wrapper.findAll('.reg-row').find((item) => item.text().includes('把数据结构练熟'))
    await row.get('.reg-complete').trigger('click')
    await flushPromises()

    // 请求已成功、动画已开始，但这一行还没有被立刻抽走
    expect(completeTask).toHaveBeenCalledTimes(1)
    expect(listTasks).toHaveBeenCalledTimes(1)
    expect(row.classes()).toContain('is-leaving')

    await new Promise((resolve) => { setTimeout(resolve, 500) })
    await flushPromises()

    expect(listTasks).toHaveBeenCalledTimes(2)
  })

  it('系统开启减少动态效果时跳过等待，直接刷新清单', async () => {
    window.matchMedia = vi.fn().mockReturnValue({ matches: true })
    const wrapper = mountTasks()
    await flushPromises()

    const row = wrapper.findAll('.reg-row').find((item) => item.text().includes('把数据结构练熟'))
    await row.get('.reg-complete').trigger('click')
    await flushPromises()

    expect(listTasks).toHaveBeenCalledTimes(2)
    delete window.matchMedia
  })

  it('进度条按已完成数量 / 总任务数更新', async () => {
    const wrapper = mountTasks()
    await flushPromises()

    const bar = wrapper.get('.task-progress')
    expect(bar.attributes('aria-valuenow')).toBe('50')
    expect(bar.attributes('aria-valuemax')).toBe('100')
    expect(bar.get('.task-progress__fill').attributes('style')).toContain('50%')
  })
})

describe('TasksView 提交结果抽屉', () => {
  it('提交结果在独立抽屉里打开，不把页面滚回页首', async () => {
    const wrapper = mountTasks()
    await flushPromises()

    const row = wrapper.findAll('.reg-row').find((item) => item.text().includes('把数据结构练熟'))
    await row.get('.reg-complete').trigger('click')
    await flushPromises()
    await new Promise((resolve) => { setTimeout(resolve, 500) })
    await flushPromises()

    expect(wrapper.find('.reg-result-drawer').exists()).toBe(true)
    expect(wrapper.find('#tv-result').exists()).toBe(true)
    expect(Element.prototype.scrollIntoView).not.toHaveBeenCalled()

    await wrapper.get('.reg-result-drawer__foot .btn-primary').trigger('click')
    expect(wrapper.find('.reg-result-drawer').exists()).toBe(false)
  })
})

describe('TasksView 多级子页面', () => {
  it('三个子页各有独立地址，默认落在任务清单', async () => {
    const wrapper = mountTasks()
    await flushPromises()

    const links = wrapper.findAll('.section-nav__link')
    expect(links).toHaveLength(3)
    expect(links.map((link) => link.attributes('href'))).toEqual([
      '/tasks',
      '/tasks/learning',
      '/tasks/projects',
    ])
    expect(links[0].attributes('aria-current')).toBe('page')
    expect(wrapper.get('.pg-crumb').text()).toContain('学习与项目')
    expect(wrapper.get('.pg-title').text()).toBe('任务清单')
    expect(wrapper.find('.reg-row').exists()).toBe(true)
    expect(wrapper.find('.reg-app').exists()).toBe(false)
  })

  it('学习路线子页只显示计划明细', async () => {
    const payload = tasksPayload()
    payload.latest_round = {
      ...payload.latest_round,
      learning_plan: [{ topic: '数据结构', covers: ['Python'], stage: 1 }],
      project_plan: [{ name: '项目甲', tech_stack: ['Python'] }],
    }
    listTasks.mockResolvedValue(payload)

    const wrapper = mountTasks({ tab: 'learning' })
    await flushPromises()

    expect(wrapper.get('.pg-title').text()).toBe('学习路线')
    expect(wrapper.findAll('.section-nav__link')[1].attributes('aria-current')).toBe('page')
    expect(wrapper.get('.learn-route').text()).toContain('数据结构')
    expect(wrapper.text()).not.toContain('项目甲')
    expect(wrapper.find('.reg-row').exists()).toBe(false)
  })

  it('实战项目子页只显示项目明细', async () => {
    const payload = tasksPayload()
    payload.latest_round = {
      ...payload.latest_round,
      learning_plan: [{ topic: '数据结构', covers: ['Python'], stage: 1 }],
      project_plan: [{ name: '项目甲', tech_stack: ['Python'] }],
    }
    listTasks.mockResolvedValue(payload)

    const wrapper = mountTasks({ tab: 'projects' })
    await flushPromises()

    expect(wrapper.get('.pg-title').text()).toBe('实战项目')
    expect(wrapper.get('.proj-grid').text()).toContain('项目甲')
    expect(wrapper.text()).not.toContain('数据结构')
  })
})

describe('TasksView 无当前档案', () => {
  it('只提示先去选档案，不再叠一条加载失败', async () => {
    const wrapper = mount(TasksView, {
      global: { stubs: { RouterLink: RouterLinkStub, Teleport: true } },
    })
    await flushPromises()

    expect(wrapper.get('.notice-warn').text()).toContain('还没有选择求职档案')
    expect(wrapper.text()).not.toContain('加载任务失败')
    expect(listTasks).not.toHaveBeenCalled()
  })
})
