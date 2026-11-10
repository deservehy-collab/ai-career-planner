/**
 * 我的档案：基础资料 + 求职档案列表。
 *
 * 锁五条契约：
 * 1. 头部目标岗位与「画像填了 n / 5 项」仍由本地基础资料算出；
 * 2. 左栏是基础资料字段，右栏只显示**当前档案**的能力表；
 * 3. 档案列表来自 plans store，当前档案有标记，切换会改 activeId；
 * 4. 删除必须二次确认，确认文案说明会连同分析/任务/成长一起删除；
 * 5. 删除当前档案后自动落到剩下的档案，没有档案时回到空态。
 */

import { h } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../../api/endpoints', () => ({
  createPlan: vi.fn(),
  deletePlan: vi.fn(),
  getAccountProfile: vi.fn(),
  listPlans: vi.fn(),
  saveAccountProfile: vi.fn(),
}))

import { deletePlan, listPlans } from '../../api/endpoints'
import HomeView from '../HomeView.vue'
import { useAuthStore } from '../../stores/auth'
import { usePlansStore } from '../../stores/plans'
import { useProfileStore } from '../../stores/profile'

const RouterLinkStub = {
  props: ['to'],
  setup(props, { slots }) {
    const path = typeof props.to === 'string' ? props.to : (props.to?.path ?? '#')
    return () => h('a', { href: path }, slots.default?.())
  },
}

const PLAN_A = {
  id: 'plan-a',
  title: 'Python 后端',
  target_job: 'Python 后端开发',
  updated_at: '2026-11-05T09:00:00.000Z',
  latest_round: { round_no: 2, finished_at: '2026-11-05T09:00:00.000Z' },
  counts: { rounds: 2, tasks: 5, growth: 3 },
  current_skills: [
    { skill: 'FastAPI', level: 'working', evidence: '课程项目' },
    { skill: 'MySQL', level: 'working', evidence: '作业系统' },
  ],
}

const PLAN_B = {
  id: 'plan-b',
  title: '会计',
  target_job: '会计',
  updated_at: '2026-11-04T09:00:00.000Z',
  latest_round: { round_no: 1 },
  counts: { rounds: 1, tasks: 2, growth: 0 },
  current_skills: [],
  is_legacy: true,
}

function mountHome() {
  return mount(HomeView, { global: { stubs: { RouterLink: RouterLinkStub } } })
}

function seedPlans(items = [structuredClone(PLAN_A), structuredClone(PLAN_B)], activeId = 'plan-a') {
  const plans = usePlansStore()
  plans.items = items
  plans.total = items.length
  if (activeId) plans.select(activeId)
  return plans
}

function fillProfile() {
  const profile = useProfileStore()
  profile.major = '计算机科学与技术'
  profile.grade = '大三'
  profile.targetJob = 'Python 后端开发'
  profile.skills = ['Python', 'MySQL', 'Git']
  profile.projects = ['在线作业系统']
  return profile
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  vi.stubGlobal('fetch', vi.fn())
  const auth = useAuthStore()
  auth.token = 'test-token'
  auth.user = { id: 7, username: 'alice' }
  listPlans.mockResolvedValue({ items: [], total: 0 })
  deletePlan.mockResolvedValue(undefined)
})

describe('HomeView 基础资料头', () => {
  it('画像为空时头部给默认目标，三个栏目都显示空态', () => {
    seedPlans([], null)
    const wrapper = mountHome()

    expect(wrapper.get('.arc-target').text()).toBe('还没有目标岗位')
    expect(wrapper.get('.arc-meter-text').text()).toBe('画像填了 0 / 5 项')
    expect(wrapper.findAll('.arc-empty')).toHaveLength(3)
    expect(wrapper.find('.arc-fields').exists()).toBe(false)
    expect(wrapper.find('table.data--skills').exists()).toBe(false)
    expect(wrapper.get('.arc-head a[href="/analyze"]').text()).toBe('去填写画像')
  })

  it('填满画像后显示目标、完成度与五个字段', () => {
    fillProfile()
    seedPlans()
    const wrapper = mountHome()

    expect(wrapper.get('.arc-target').text()).toBe('Python 后端开发')
    expect(wrapper.get('.arc-line').text()).toContain('计算机科学与技术')
    expect(wrapper.get('.arc-line').text()).toContain('大三')
    expect(wrapper.get('.arc-meter-text').text()).toBe('画像填了 5 / 5 项')
    expect(wrapper.findAll('.arc-field')).toHaveLength(5)
    expect(wrapper.get('.arc-head a[href="/analyze"]').text()).toBe('编辑画像并分析')
  })

  it('能力表只取当前档案，不拿本机缓存冒充', () => {
    fillProfile()
    useProfileStore().lastSkills = [{ skill: '不该出现', level: 'expert' }]
    seedPlans()
    const wrapper = mountHome()

    const rows = wrapper.findAll('table.data--skills tbody tr')
    expect(rows).toHaveLength(2)
    expect(rows[0].text()).toContain('FastAPI')
    expect(wrapper.text()).not.toContain('不该出现')
    expect(wrapper.get('.arc-side-no').text()).toBe('2项能力')

    usePlansStore().select('plan-b')
    return wrapper.vm.$nextTick().then(() => {
      expect(wrapper.find('table.data--skills').exists()).toBe(false)
      expect(wrapper.get('.arc-side-no').text()).toBe('0项能力')
    })
  })

  it('不再重复概览的成长闭环图，也不再走旧的两张卡片', () => {
    const wrapper = mountHome()

    expect(wrapper.find('.loop-row').exists()).toBe(false)
    expect(wrapper.find('.grid.grid-2').exists()).toBe(false)
  })
})

describe('HomeView 求职档案列表', () => {
  it('没有档案时给空态与新建入口', () => {
    seedPlans([], null)
    const wrapper = mountHome()

    expect(wrapper.get('.arc-plans').text()).toContain('还没有求职档案')
    expect(wrapper.get('.arc-plans a[href="/analyze"]').exists()).toBe(true)
  })

  it('列出全部档案，当前档案有标记，旧数据档案标明只读', () => {
    seedPlans()
    const wrapper = mountHome()

    const rows = wrapper.findAll('.arc-plan')
    expect(rows).toHaveLength(2)
    expect(rows[0].text()).toContain('Python 后端')
    expect(rows[0].text()).toContain('2 轮 · 5 项任务 · 3 条成长')
    expect(rows[0].text()).toContain('当前档案')
    expect(rows[1].text()).toContain('旧数据只读')
  })

  it('删除先确认，确认文案说明会级联删除；取消不动数据', async () => {
    seedPlans()
    const wrapper = mountHome()

    await wrapper.findAll('.arc-plan-delete')[1].trigger('click')
    const confirm = wrapper.get('.arc-plan-confirm')
    expect(confirm.text()).toContain('删除「会计」？')
    expect(confirm.text()).toContain('1 轮分析')
    expect(confirm.text()).toContain('2 项任务')
    expect(confirm.text()).toContain('其他档案不受影响')
    expect(deletePlan).not.toHaveBeenCalled()

    await confirm.findAll('button')[0].trigger('click')
    expect(wrapper.find('.arc-plan-confirm').exists()).toBe(false)
    expect(deletePlan).not.toHaveBeenCalled()
  })

  it('确认删除当前档案后切到剩下的档案，并同步 URL 里的档案 id', async () => {
    const plans = seedPlans()
    listPlans.mockResolvedValue({ items: [structuredClone(PLAN_B)], total: 1 })
    const wrapper = mountHome()

    await wrapper.findAll('.arc-plan-delete')[0].trigger('click')
    await wrapper.get('.arc-plan-confirm .btn-danger').trigger('click')
    await flushPromises()

    expect(deletePlan).toHaveBeenCalledWith('plan-a')
    expect(plans.activeId).toBe('plan-b')
    expect(wrapper.get('.notice-ok').text()).toContain('已切换到剩下的档案')
    expect(wrapper.findAll('.arc-plan')).toHaveLength(1)
  })
})

describe('HomeView 精简文案', () => {
  it('不再铺页面导语，空档案只留标题与入口', () => {
    seedPlans([], null)
    const wrapper = mountHome()

    expect(wrapper.find('.pg-deck').exists()).toBe(false)
    expect(wrapper.find('.pg-eyebrow').exists()).toBe(false)
    const empty = wrapper.findAll('.arc-empty').map((node) => node.text())
    expect(empty.every((text) => !text.includes('每开始一次新的岗位分析'))).toBe(true)
    expect(wrapper.text()).toContain('去开始第一份档案')
  })
})
