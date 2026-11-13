/**
 * 面试陪练页。
 *
 * 锁住的契约：
 * 1. 没有当前档案时给「去我的档案」入口，生成按钮不可用；
 * 2. 出题带上 plan_id 与本机简历草稿，草稿按「账号 + 档案」取；
 * 3. 题目按技术/项目/动机/反问分组，参考要点默认收起；
 * 4. 失败如实显示后端的人话文案，并给重试；
 * 5. 题目按账号 + 档案缓存在本机：切页面、刷新回来都还在，也不会重复出题。
 */

import { h } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../../api/endpoints', () => ({
  generateInterviewQuestions: vi.fn(),
  listPlans: vi.fn(),
}))

import {
  generateInterviewQuestions,
  listPlans,
} from '../../api/endpoints'
import { useAuthStore } from '../../stores/auth'
import { usePlansStore } from '../../stores/plans'
import InterviewView from '../InterviewView.vue'

const RouterLinkStub = {
  props: ['to'],
  setup(props, { slots }) {
    const path = typeof props.to === 'string' ? props.to : (props.to?.path ?? '#')
    return () => h('a', { href: path }, slots.default?.())
  },
}

const PLAN = {
  id: 'plan-a',
  title: 'Java 后端',
  target_job: 'Java 后端开发',
  counts: { rounds: 1, tasks: 2, growth: 1 },
}

const RESPONSE = {
  target_job: 'Java 后端开发',
  channel: 'local',
  model: 'qwen2.5:3b',
  questions: [
    {
      category: '技术',
      question: '讲讲 Spring Boot 自动配置原理。',
      why: '核对框架基础。',
      key_points: ['自动配置类', '条件注解'],
      follow_up: '如果冲突怎么排查？',
    },
    {
      category: '技术',
      question: 'MySQL 索引在什么情况下失效？',
      why: '',
      key_points: [],
      follow_up: '',
    },
    {
      category: '项目',
      question: '订单模块你怎么设计？',
      why: '核对项目深度。',
      key_points: ['订单状态机'],
      follow_up: '遇到过并发问题吗？',
    },
    {
      category: '动机',
      question: '为什么想做后端开发？',
      why: '',
      key_points: ['结合已完成的学习任务'],
      follow_up: '',
    },
    {
      category: '反问',
      question: '入职后前三个月的工作重点是什么？',
      why: '',
      key_points: ['问清团队分工与考核'],
      follow_up: '',
    },
  ],
  sources: { resume: true, has_jd: true, fact_count: 4, skill_count: 2 },
}

function seedPlans(items = [PLAN]) {
  const auth = useAuthStore()
  auth.user = { id: 'user-test', username: 'tester' }
  const plans = usePlansStore()
  plans.items = structuredClone(items)
  plans.select(items[0]?.id || null)
  return plans
}

function mountView() {
  return mount(InterviewView, {
    global: { stubs: { RouterLink: RouterLinkStub } },
  })
}

beforeEach(() => {
  setActivePinia(createPinia())
  window.localStorage.clear()
  vi.clearAllMocks()
  listPlans.mockResolvedValue({ items: [structuredClone(PLAN)], total: 1 })
  generateInterviewQuestions.mockResolvedValue(structuredClone(RESPONSE))
})

describe('面试陪练空态与入口', () => {
  it('没有档案时给去建档案的入口，生成按钮不可用', async () => {
    const auth = useAuthStore()
    auth.user = { id: 'user-test', username: 'tester' }
    listPlans.mockResolvedValue({ items: [], total: 0 })

    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.find('.iv-gate').exists()).toBe(true)
    expect(wrapper.get('.iv-generate').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('去我的档案')
  })

  it('有档案但未生成时只留空态标题与六张空题卡，不自动请求模型', async () => {
    seedPlans()
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.find('.iv-empty').exists()).toBe(true)
    expect(wrapper.find('.iv-empty-title').exists()).toBe(true)
    expect(wrapper.findAll('.iv-ghost')).toHaveLength(6)
    expect(wrapper.find('.iv-lead').exists()).toBe(false)
    expect(generateInterviewQuestions).not.toHaveBeenCalled()
  })
})

describe('面试陪练生成与展示', () => {
  it('带上档案与本机简历草稿，按四类分组展示', async () => {
    seedPlans()
    window.localStorage.setItem(
      'career-agent:resume:user-test:plan-a',
      JSON.stringify({
        version: 2,
        savedAt: '2026-11-10T00:00:00Z',
        resume: {
          basic: { name: '李四' },
          intent: { targetJob: 'Java 后端开发' },
          skills: [{ label: '熟练', items: ['Java', 'Spring Boot'] }],
        },
      }),
    )

    const wrapper = mountView()
    await flushPromises()
    await wrapper.get('.iv-generate').trigger('click')
    await flushPromises()

    const payload = generateInterviewQuestions.mock.calls[0][0]
    expect(payload.plan_id).toBe('plan-a')
    expect(payload.resume.skills[0].items).toContain('Java')

    const titles = wrapper.findAll('.iv-group-title').map((node) => node.text())
    expect(titles).toHaveLength(4)
    expect(titles[0]).toContain('技术题')
    expect(wrapper.findAll('.iv-card')).toHaveLength(5)
    expect(wrapper.find('.iv-source').exists()).toBe(false)
  })

  it('封面目录按四类给出题量，并指向对应分组', async () => {
    seedPlans()
    const wrapper = mountView()
    await flushPromises()
    await wrapper.get('.iv-generate').trigger('click')
    await flushPromises()

    const rows = wrapper.findAll('.iv-toc li')
    expect(rows).toHaveLength(4)
    expect(rows[0].text()).toContain('技术题')
    expect(rows[0].text()).toContain('2')
    expect(rows[3].text()).toContain('反问面试官')
    expect(rows[3].get('a').attributes('href')).toBe('#iv-ask')
    expect(wrapper.find('#iv-tech').exists()).toBe(true)
    expect(wrapper.get('.iv-stamp').text()).toContain('5')
  })

  it('切走再回来题目还在，也不会重复出题', async () => {
    seedPlans()
    const first = mountView()
    await flushPromises()
    await first.get('.iv-generate').trigger('click')
    await flushPromises()
    expect(first.findAll('.iv-card')).toHaveLength(5)

    first.unmount()

    const second = mountView()
    await flushPromises()

    expect(second.findAll('.iv-card')).toHaveLength(5)
    expect(generateInterviewQuestions).toHaveBeenCalledTimes(1)
  })

  it('刷新后从本机缓存恢复题目', async () => {
    seedPlans()
    window.localStorage.setItem(
      'career-agent:interview:user-test:plan-a',
      JSON.stringify(structuredClone(RESPONSE)),
    )

    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.findAll('.iv-card')).toHaveLength(5)
    expect(generateInterviewQuestions).not.toHaveBeenCalled()
  })

  it('参考要点默认收起，点开才显示要点与追问', async () => {
    seedPlans()
    const wrapper = mountView()
    await flushPromises()
    await wrapper.get('.iv-generate').trigger('click')
    await flushPromises()

    expect(wrapper.find('.iv-points').exists()).toBe(false)

    await wrapper.get('.iv-toggle').trigger('click')

    const points = wrapper.get('.iv-points')
    expect(points.text()).toContain('自动配置类')
    expect(points.text()).toContain('如果冲突怎么排查？')
  })

  it('失败显示后端文案，重试会再次请求', async () => {
    seedPlans()
    generateInterviewQuestions.mockRejectedValueOnce(
      new Error('本地模型服务 Ollama 没启动或不可达。'),
    )

    const wrapper = mountView()
    await flushPromises()
    await wrapper.get('.iv-generate').trigger('click')
    await flushPromises()

    expect(wrapper.find('.iv-error').exists()).toBe(true)
    expect(wrapper.text()).toContain('Ollama 没启动')

    await wrapper.get('.iv-error .btn').trigger('click')
    await flushPromises()

    expect(generateInterviewQuestions).toHaveBeenCalledTimes(2)
    expect(wrapper.findAll('.iv-card')).toHaveLength(5)
  })
})
