/**
 * 成长记录：一条流水。
 *
 * 锁住的契约：
 * 1. 顶部深色条只放本轮状态，主操作竖排在本轮条下方的行动区；三条读数在右侧栏；
 * 2. 待交接 / 已入账在同一条虚线上，中间有交接点；待交接是虚线卡、已入账是实线行；
 * 3. 没有待交接成果时不出现交接点，主按钮禁用，并说明该去哪里补；
 * 4. 主操作仍要二次确认，确认文案说明会改写成长历史。
 *
 * 后端行为（真正记账、作废、撤回）由 Python 测试覆盖，这里只验证界面与请求边界。
 */

import { h } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../../api/endpoints', () => ({
  addManualEvent: vi.fn(),
  getGrowthHistory: vi.fn(),
  getPending: vi.fn(),
  listTasks: vi.fn(),
  startNextRound: vi.fn(),
  voidGrowthEvent: vi.fn(),
  withdrawPending: vi.fn(),
}))

vi.mock('../../composables/useRunPolling', () => ({
  pollRun: vi.fn(),
}))

import {
  addManualEvent,
  getGrowthHistory,
  getPending,
  listTasks,
} from '../../api/endpoints'
import { usePlansStore } from '../../stores/plans'
import GrowthView from '../GrowthView.vue'

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
  latest_round: { round_no: 2 },
  counts: { rounds: 2, tasks: 3, growth: 1 },
  current_skills: [],
}

const PENDING = {
  store_status: 'ok',
  writable: true,
  restored: true,
  warnings: [],
  skipped: [],
  events: [
    {
      type: 'learning',
      title: '补完单元测试',
      skills: ['pytest'],
      evidence: '给 3 个接口补了 pytest 用例。',
      at: '2026-11-05T07:00:00Z',
    },
  ],
  tasks: [{ task_id: 't-1', title: '把项目补上单元测试与 CI' }],
}

const HISTORY = {
  status: 'ok',
  writable: true,
  confirmed_count: 1,
  voided_count: 0,
  latest_skills: [{ skill: 'FastAPI', level: 'working' }],
  baseline: { source: 'user_profile', at: '2026-10-22T02:00:00Z' },
  history: [
    {
      at: '2026-11-05T08:00:00Z',
      target_job: 'Python 后端开发',
      kind: 'round',
      knowledge_base_status: 'ok',
      level_changes: [
        { skill: 'FastAPI', from: 'basic', to: 'working', gate_status: 'passed' },
      ],
      events: [{ title: '补完单元测试', evidence: '给 3 个接口补了 pytest 用例。' }],
      blocked_upgrades: [],
      voided_event_indices: [],
    },
  ],
}

const PLAN = {
  rounds: [{ round: 1 }, { round: 2 }],
  latest_round: { round: 2, target_job: 'Python 后端开发' },
  latest_goal: { status: 'in_progress' },
  pending_next_round: null,
}

function mountGrowth() {
  const plans = usePlansStore()
  plans.items = [structuredClone(PLAN_ITEM)]
  plans.select(PLAN_ID)
  return mount(GrowthView, {
    global: { stubs: { RouterLink: RouterLinkStub, Teleport: true } },
  })
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  getPending.mockResolvedValue(structuredClone(PENDING))
  getGrowthHistory.mockResolvedValue(structuredClone(HISTORY))
  listTasks.mockResolvedValue(structuredClone(PLAN))
})

describe('GrowthView 本轮条与一条流水', () => {
  it('深色条只放轮次与目标，主操作竖排到本轮条下方', async () => {
    const wrapper = mountGrowth()
    await flushPromises()

    expect(wrapper.find('.growth-rail').exists()).toBe(false)
    expect(wrapper.find('.growth-desk').exists()).toBe(false)
    expect(wrapper.find('.gr-now-facts').exists()).toBe(false)

    expect(wrapper.get('.gr-now-no').text()).toBe('第 2 轮')
    expect(wrapper.get('.gr-now-total').text()).toBe('共 2 轮')
    expect(wrapper.get('.gr-now-target').text()).toBe('Python 后端开发')
    // 生成按钮不再挂在深色条里，而是在它下方的行动区竖排
    expect(wrapper.find('.gr-now .gr-next-btn').exists()).toBe(false)
    expect(wrapper.get('.gr-aux .gr-next-btn').text()).toBe('生成下一轮计划')
    expect(wrapper.get('.gr-aux .gr-next-btn').attributes('disabled')).toBeUndefined()
    expect(wrapper.find('.gr-next-hint').exists()).toBe(false)

    const rail = wrapper.get('.gr-rail')
    expect(rail.findAll('.gr-rail-stats dd').map((dd) => dd.text())).toEqual(['1条', '1次', '1项'])
    expect(rail.get('.gr-rail-skills').text()).toContain('FastAPI')

    // 三块数据都必须带当前档案 id，不能落到账号级旧接口上
    expect(getPending).toHaveBeenCalledWith({}, PLAN_ID)
    expect(getGrowthHistory).toHaveBeenCalledWith({}, PLAN_ID)
    expect(listTasks).toHaveBeenCalledWith({}, {}, PLAN_ID)
  })

  it('没有选中档案时不发任何成长请求，只给去档案页的入口', async () => {
    const wrapper = mount(GrowthView, { global: { stubs: { RouterLink: RouterLinkStub } } })
    await flushPromises()

    expect(getPending).not.toHaveBeenCalled()
    expect(getGrowthHistory).not.toHaveBeenCalled()
    expect(listTasks).not.toHaveBeenCalled()
    expect(wrapper.get('.notice-warn').text()).toContain('还没有选择求职档案')
    expect(wrapper.get('.notice-warn a[href="/home"]').exists()).toBe(true)
  })

  it('一条流水：待交接一条成果，中间有交接点，已入账一条历史', async () => {
    const wrapper = mountGrowth()
    await flushPromises()

    const flow = wrapper.get('.gr-flow')
    const ledgers = flow.findAll('.gr-ledger')
    expect(ledgers).toHaveLength(2)
    expect(ledgers[0].classes()).toContain('gr-ledger--pending')
    expect(ledgers[1].classes()).toContain('gr-ledger--posted')
    expect(ledgers[0].get('.gr-ledger-title').text()).toBe('待交接')
    expect(ledgers[1].get('.gr-ledger-title').text()).toBe('已入账')
    expect(flow.get('.gr-handover').text()).toContain('生成下一轮时')
    expect(wrapper.findAll('.evidence-item')).toHaveLength(1)
    expect(wrapper.findAll('.growth-entry')).toHaveLength(1)
    expect(wrapper.get('.growth-entry__sum').text()).toContain('1 项等级变化')

    await wrapper.get('.growth-entry__head').trigger('click')
    expect(wrapper.get('.growth-entry__body').text()).toContain('FastAPI')
    expect(wrapper.get('.growth-entry__body').text()).toContain('basic')
  })

  it('没有待交接成果时不出现交接点，主按钮禁用，并指向任务页', async () => {
    getPending.mockResolvedValue({
      ...structuredClone(PENDING),
      events: [],
      tasks: [],
    })
    const wrapper = mountGrowth()
    await flushPromises()

    expect(wrapper.find('.gr-handover').exists()).toBe(false)
    expect(wrapper.get('.gr-next-btn').attributes('disabled')).toBeDefined()
    expect(wrapper.get('.gr-next-hint').text()).toContain('当前没有待交接成果')
    expect(wrapper.get('.gr-ledger--pending .growth-empty').exists()).toBe(true)
    expect(wrapper.get('.gr-ledger--pending a[href="/tasks"]').exists()).toBe(true)
  })

  it('主操作先出确认条，确认文案说明会更新成长记录', async () => {
    const wrapper = mountGrowth()
    await flushPromises()

    await wrapper.get('.gr-next-btn').trigger('click')
    const confirm = wrapper.get('.gr-confirm')
    expect(confirm.text()).toContain('确认生成下一轮')
    expect(confirm.text()).toContain('1 条')
    expect(confirm.text()).toContain('成长记录和任务清单会一起更新')

    await wrapper.get('.gr-confirm .btn-row .btn:first-child').trigger('click')
    expect(wrapper.find('.gr-confirm').exists()).toBe(false)
  })

  it('没有任务轮次时，深色条指向岗位分析的首次计划', async () => {
    listTasks.mockResolvedValue({
      rounds: [],
      latest_round: null,
      latest_goal: null,
      pending_next_round: null,
    })
    const wrapper = mountGrowth()
    await flushPromises()

    expect(wrapper.find('.gr-now-no').exists()).toBe(false)
    expect(wrapper.get('.gr-now-none').text()).toContain('还没有任务轮次')
    expect(wrapper.get('.gr-now-none a[href="/analyze"]').exists()).toBe(true)
  })

  it('待交接超过一页时可翻页，来源任务仍按原始索引对齐', async () => {
    const events = Array.from({ length: 7 }, (_, i) => ({
      type: 'learning',
      title: `成果 ${i + 1}`,
      skills: ['pytest'],
      evidence: '',
      at: `2026-11-0${i + 1}T07:00:00Z`,
    }))
    getPending.mockResolvedValue({
      ...structuredClone(PENDING),
      events,
      tasks: events.map((_, i) => ({ task_id: `t-${i + 1}`, title: `任务 ${i + 1}` })),
    })
    const wrapper = mountGrowth()
    await flushPromises()

    expect(wrapper.findAll('.evidence-item')).toHaveLength(5)
    expect(wrapper.get('.gr-pager').text()).toContain('第 1 / 2 页')
    expect(wrapper.get('.gr-pager button').attributes('disabled')).toBeDefined()

    await wrapper.get('.gr-pager').findAll('button')[1].trigger('click')

    expect(wrapper.findAll('.evidence-item')).toHaveLength(2)
    expect(wrapper.findAll('.evidence-item')[1].text()).toContain('成果 7')
    // 第二页第一条仍对着第 6 个任务，撤回入口不会错位
    expect(wrapper.findAll('.evidence-item')[0].text()).toContain('任务 6')
    expect(wrapper.get('.gr-pager').text()).toContain('第 2 / 2 页')
    expect(wrapper.get('.gr-pager').findAll('button')[1].attributes('disabled')).toBeDefined()
  })

  it('已入账超过一页时按新到旧翻页', async () => {
    const history = Array.from({ length: 7 }, (_, i) => ({
      at: `2026-11-0${i + 1}T08:00:00Z`,
      target_job: 'Python 后端开发',
      kind: 'round',
      level_changes: [],
      events: [],
      blocked_upgrades: [],
      voided_event_indices: [],
    }))
    getGrowthHistory.mockResolvedValue({ ...structuredClone(HISTORY), history })
    const wrapper = mountGrowth()
    await flushPromises()

    expect(wrapper.findAll('.growth-entry')).toHaveLength(5)
    expect(wrapper.get('.gr-pager').text()).toContain('第 1 / 2 页')
    expect(wrapper.text()).toContain('2026-11-07')
    expect(wrapper.text()).not.toContain('2026-11-01')

    await wrapper.get('.gr-pager').findAll('button')[1].trigger('click')

    expect(wrapper.findAll('.growth-entry')).toHaveLength(2)
    expect(wrapper.findAll('.growth-entry')[1].text()).toContain('2026-11-01')
  })
})

describe('GrowthView 自述完成与补凭证', () => {
  it('待交接的自述成果标出未附凭证，并给去任务页补凭证的入口', async () => {
    getPending.mockResolvedValue({
      ...structuredClone(PENDING),
      events: [{
        type: 'learning',
        title: '补完单元测试',
        skills: ['pytest'],
        evidence: '',
        evidence_kind: 'self_report',
        at: '2026-11-05T07:00:00Z',
      }],
    })
    const wrapper = mountGrowth()
    await flushPromises()

    const item = wrapper.get('.evidence-item')
    expect(item.text()).toContain('自述完成')
    expect(item.text()).toContain('未附凭证')
    expect(item.find('a[href="/tasks"]').exists()).toBe(true)
  })

  it('待交接的有凭证成果只标已附凭证', async () => {
    getPending.mockResolvedValue({
      ...structuredClone(PENDING),
      events: [{
        type: 'learning',
        title: '补完单元测试',
        skills: ['pytest'],
        evidence: '给 3 个接口补了 pytest 用例。',
        evidence_kind: 'growth',
        at: '2026-11-05T07:00:00Z',
      }],
    })
    const wrapper = mountGrowth()
    await flushPromises()

    const item = wrapper.get('.evidence-item')
    expect(item.text()).toContain('已附凭证')
    expect(item.text()).not.toContain('未附凭证')
    expect(item.find('a[href="/tasks"]').exists()).toBe(false)
  })

  it('补凭证产生的验证记录在已入账里单独标出', async () => {
    getGrowthHistory.mockResolvedValue({
      ...structuredClone(HISTORY),
      history: [
        ...HISTORY.history,
        {
          at: '2026-11-06T08:00:00Z',
          target_job: 'Python 后端开发',
          kind: 'verification',
          task_id: 't-1',
          level_changes: [],
          events: [],
          // 后端 real 形状是计数（int），不是技能名数组
          changed_skills: 1,
          blocked_upgrades: [],
          voided_event_indices: [],
        },
      ],
    })
    const wrapper = mountGrowth()
    await flushPromises()

    expect(wrapper.text()).toContain('补凭证')
    expect(wrapper.text()).toContain('1 项改为有凭证')

    const head = wrapper
      .findAll('.growth-entry__head')
      .find((btn) => btn.text().includes('补凭证'))
    await head.trigger('click')
    expect(wrapper.text()).toContain('1 项技能从自述改为有凭证')
    expect(wrapper.text()).not.toContain('没有技能需要改写')
  })

  it('手动补录不填凭证也能提交，凭证按选填处理', async () => {
    const wrapper = mountGrowth()
    await flushPromises()

    const manualBtn = wrapper
      .findAll('.gr-ledger-tools button')
      .find((btn) => btn.text().includes('补录成果'))
    expect(manualBtn).toBeTruthy()
    await manualBtn.trigger('click')

    await wrapper.get('#manual-title').setValue('看完了 Git 入门')
    await wrapper.get('#manual-skills').setValue('Git')
    await wrapper.get('.reg-sheet__foot .btn-primary').trigger('click')
    await flushPromises()

    expect(addManualEvent).toHaveBeenCalledTimes(1)
    const [body, planId] = addManualEvent.mock.calls[0]
    expect(planId).toBe(PLAN_ID)
    expect(body.title).toBe('看完了 Git 入门')
    expect(body.evidence).toBeUndefined()
  })
})
