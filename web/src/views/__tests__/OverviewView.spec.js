/**
 * 概览页：一条主线上挂四张站牌。锁的是「这一屏只讲主线，站位由当前档案决定」。
 *
 * 四条契约：
 * 1. 路线恒为四站，编号就是顺序；
 * 2. 只有一站亮着（实色 +「你在这」），主行动全页只有一个，随当前档案在三档之间切换；
 * 3. 七个步骤收在第 2 站的牌子里，不再单独占一条色带；
 * 4. 页面直接结束在路线图，不再铺档案摘要/上手须知页脚，整页不发任何请求。
 */

import { h } from 'vue'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import OverviewView from '../OverviewView.vue'
import { usePlansStore } from '../../stores/plans'

const RouterLinkStub = {
  props: ['to'],
  setup(props, { slots }) {
    const path = typeof props.to === 'string' ? props.to : (props.to?.path ?? '#')
    return () => h('a', { href: path }, slots.default?.())
  },
}

const PLAN = {
  id: 'plan-a',
  title: 'Python 后端开发',
  target_job: 'Python 后端开发',
  state: 'active',
  is_legacy: false,
  created_at: '2026-11-01T08:00:00.000Z',
  updated_at: '2026-11-05T09:00:00.000Z',
  current_skills: [
    { skill: 'FastAPI', level: 'working', evidence: '课程项目' },
    { skill: 'MySQL', level: 'basic' },
    { skill: 'Docker', level: 'basic' },
  ],
  latest_round: {
    id: 'round-2',
    round_no: 2,
    target_job: 'Python 后端开发',
    finished_at: '2026-11-05T09:00:00.000Z',
  },
  counts: { rounds: 2, tasks: 5, growth: 3 },
}

const FRESH_PLAN = {
  id: 'plan-b',
  title: '跨境电商运营',
  target_job: '跨境电商运营',
  state: 'active',
  is_legacy: false,
  created_at: '2026-11-06T08:00:00.000Z',
  updated_at: '2026-11-06T08:00:00.000Z',
  current_skills: [],
  latest_round: null,
  counts: { rounds: 0, tasks: 0, growth: 0 },
}

function seedPlans(items, activeId = null) {
  const plans = usePlansStore()
  plans.items = items
  plans.total = items.length
  if (activeId) plans.select(activeId)
  return plans
}

function mountOverview() {
  return mount(OverviewView, { global: { stubs: { RouterLink: RouterLinkStub } } })
}

/** 亮着的那一站（至多一站）。 */
function currentStop(wrapper) {
  return wrapper.findAll('.ov-stop').filter((stop) => stop.classes('is-current'))
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.stubGlobal('fetch', vi.fn())
})

describe('OverviewView 四站路线图', () => {
  it('路线恒为四站，编号 1 到 4 就是使用顺序', () => {
    const wrapper = mountOverview()
    const stops = wrapper.findAll('.ov-stop')

    expect(stops).toHaveLength(4)
    expect(stops.map((stop) => stop.get('.ov-stop-mark').text())).toEqual(['1', '2', '3', '4'])
    expect(stops.map((stop) => stop.get('.ov-stop-head').attributes('href')))
      .toEqual(['/assistant', '/analyze', '/tasks', '/growth'])
  })

  it('还没有档案时，第 2 站亮着，主行动是新建档案', () => {
    const wrapper = mountOverview()

    expect(currentStop(wrapper)).toHaveLength(1)
    expect(wrapper.get('.ov-stop.is-current').get('.ov-stop-mark').text()).toBe('2')
    expect(wrapper.get('.ov-map-plane').classes()).toContain('is-step2')
    expect(wrapper.get('.ov-stop-flag').text()).toBe('你在这')
    expect(wrapper.get('.ov-stop.is-current .ov-stop-line').text()).toContain('还没有求职档案')
    expect(wrapper.get('.ov-now-btn').attributes('href')).toBe('/analyze')
    expect(wrapper.get('.ov-now-btn').text()).toContain('新建档案')
    expect(wrapper.get('.ov-route-state').text()).toBe('第 1 站已完成 · 第 2 站进行中')

    expect(wrapper.get('.ov-stop--1').classes()).toContain('is-done')
    expect(wrapper.get('.ov-stop--2').classes()).toContain('is-current')
    expect(wrapper.get('.ov-stop--3').classes()).toContain('is-todo')
    expect(wrapper.get('.ov-stop--4').classes()).toContain('is-todo')
  })

  it('有档案但还没选中时，主行动指向「我的档案」去选择', () => {
    seedPlans([PLAN, FRESH_PLAN])

    const wrapper = mountOverview()

    expect(wrapper.get('.ov-stop.is-current .ov-stop-line').text()).toContain('已有 2 份档案')
    expect(wrapper.get('.ov-now-btn').attributes('href')).toBe('/home')
    expect(wrapper.get('.ov-now-btn').text()).toContain('选择档案')
    expect(wrapper.get('.ov-now-alt').attributes('href')).toBe('/analyze')
  })

  it('选中还没分析的档案时，主行动是这份档案的首次分析', () => {
    seedPlans([PLAN, FRESH_PLAN], FRESH_PLAN.id)

    const wrapper = mountOverview()

    expect(wrapper.get('.ov-stop.is-current .ov-stop-line').text()).toContain('跨境电商运营')
    expect(wrapper.get('.ov-now-btn').attributes('href')).toBe('/analyze')
    expect(wrapper.get('.ov-now-btn').text()).toContain('开始分析')
  })

  it('当前档案已有轮次时主行动转成去执行，焦点移到第 3 站', () => {
    seedPlans([PLAN, FRESH_PLAN], PLAN.id)

    const wrapper = mountOverview()

    expect(wrapper.get('.ov-stop.is-current').get('.ov-stop-mark').text()).toBe('3')
    expect(wrapper.get('.ov-map-plane').classes()).toContain('is-step3')
    expect(wrapper.get('.ov-now-btn').attributes('href')).toBe('/tasks')
    expect(wrapper.get('.ov-route-state').text()).toBe('第 2 站已完成 · 第 3 站进行中')
    expect(wrapper.get('.ov-stop--1').classes()).toContain('is-done')
    expect(wrapper.get('.ov-stop--2').classes()).toContain('is-done')
    expect(wrapper.get('.ov-stop--4').classes()).toContain('is-todo')
  })

  it('全页只有一个主行动按钮，次级入口是文字链接', () => {
    const wrapper = mountOverview()

    expect(wrapper.findAll('.ov-now-btn')).toHaveLength(1)
    expect(wrapper.get('.ov-now-alt').attributes('href')).toBe('/assistant')
  })
})

describe('OverviewView 第 2 站的七步', () => {
  it('七个步骤按工作流顺序收在第 2 站的牌子里，产出仍是四个去处', () => {
    const wrapper = mountOverview()

    expect(wrapper.findAll('.ov-steps')).toHaveLength(1)
    expect(wrapper.get('.ov-steps').element.closest('.ov-stop--2')).toBeTruthy()
    expect(wrapper.findAll('.ov-tick')).toHaveLength(7)
    expect(wrapper.findAll('.ov-tick-no').map((node) => node.text())).toEqual(['1', '2', '3', '4', '5', '6', '7'])
    expect(wrapper.findAll('.ov-legend-item')).toHaveLength(2)
    expect(wrapper.findAll('.ov-chain-out')).toHaveLength(4)
  })

  it('页面不再铺档案摘要或上手须知页脚', () => {
    seedPlans([PLAN, FRESH_PLAN], PLAN.id)
    const wrapper = mountOverview()

    expect(wrapper.find('.ov-meta').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('开始之前')
    expect(wrapper.text()).not.toContain('换设备要重填')
  })

  it('整页不发请求：换页与读数都不碰后端', () => {
    seedPlans([PLAN], PLAN.id)
    mountOverview()

    expect(fetch).not.toHaveBeenCalled()
  })
})
