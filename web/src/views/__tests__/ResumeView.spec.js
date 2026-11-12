/**
 * 简历工作台。
 *
 * 锁住的契约：
 * 1. 自动带入只搬档案里已有的事实（目标岗位、专业、能力表、已完成项目），
 *    姓名/电话/邮箱保持空白，不编造；
 * 2. 右侧改一个字，左侧纸面立刻同步；
 * 3. 必填项读数按姓名/电话/邮箱/目标/学校/专业六项算，缺几项就报几项；
 * 4. 导出 PDF / Word 都是显式下载，不写业务数据；
 * 5. 草稿按「账号 + 档案」分开，切档案不会串内容；
 * 6. 没有档案时给出建档案入口，而不是一张空简历。
 */

import { h } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../../api/endpoints', () => ({
  exportResumeDocx: vi.fn(),
  exportResumePdf: vi.fn(),
  getGrowthHistory: vi.fn(),
  getPlan: vi.fn(),
  listPlans: vi.fn(),
  listTasks: vi.fn(),
}))

vi.mock('../../utils/download', () => ({
  saveBlob: vi.fn(),
}))

import {
  exportResumeDocx,
  exportResumePdf,
  getGrowthHistory,
  getPlan,
  listPlans,
  listTasks,
} from '../../api/endpoints'
import { useAuthStore } from '../../stores/auth'
import { usePlansStore } from '../../stores/plans'
import { saveBlob } from '../../utils/download'
import ResumeView from '../ResumeView.vue'

const RouterLinkStub = {
  props: ['to'],
  setup(props, { slots }) {
    const path = typeof props.to === 'string' ? props.to : (props.to?.path ?? '#')
    return () => h('a', { href: path }, slots.default?.())
  },
}

const PLAN_A = {
  id: 'plan-a',
  title: 'Java 后端',
  target_job: 'Java 后端开发',
  latest_round: { round_no: 1 },
  counts: { rounds: 1, tasks: 2, growth: 1 },
  current_skills: [],
}

const PLAN_B = {
  id: 'plan-b',
  title: '数据分析',
  target_job: '数据分析师',
  latest_round: { round_no: 1 },
  counts: { rounds: 1, tasks: 0, growth: 0 },
  current_skills: [],
}

const PLAN_DETAIL = {
  id: 'plan-a',
  title: 'Java 后端',
  target_job: 'Java 后端开发',
  profile_snapshot: {
    major: '软件工程',
    grade: '大三',
    skills: ['Git', 'Spring Boot'],
    projects: [],
  },
  current_skills: [
    { skill: 'Java', level: '掌握' },
    { skill: 'Spring Boot', level: '掌握' },
    { skill: 'Git', level: '了解' },
  ],
  latest_round: { round_no: 1 },
  counts: { rounds: 1, tasks: 2, growth: 1 },
}

const TASKS = {
  writable: true,
  tasks: [
    {
      task_id: 'r001-l01',
      kind: 'learning',
      round: 1,
      index: 1,
      title: '把 Java 集合练熟',
      status: 'completed',
      completed_at: '2026-11-01T00:00:00Z',
    },
    {
      task_id: 'r001-p01',
      kind: 'project',
      round: 1,
      index: 1,
      title: '课程管理后台',
      status: 'pending',
    },
  ],
}

const GROWTH = {
  history: [
    {
      at: '2026-11-03T00:00:00Z',
      events: [
        {
          kind: 'project',
          title: '校园二手交易平台',
          evidence: '独立完成订单与鉴权模块。',
          skills: ['Java', 'MySQL'],
          at: '2026-11-03T00:00:00Z',
        },
        {
          kind: 'project',
          title: '被撤回的项目',
          evidence: '不应出现',
          at: '2026-11-04T00:00:00Z',
        },
      ],
      voided_event_indices: [1],
    },
  ],
}

function seedPlans(items = [PLAN_A]) {
  const auth = useAuthStore()
  auth.user = { id: 'user-test', username: 'tester' }
  const plans = usePlansStore()
  plans.items = structuredClone(items)
  plans.select(items[0]?.id || null)
  return plans
}

function mountResume() {
  return mount(ResumeView, {
    global: { stubs: { RouterLink: RouterLinkStub } },
  })
}

beforeEach(() => {
  setActivePinia(createPinia())
  window.localStorage.clear()
  vi.clearAllMocks()
  getPlan.mockResolvedValue(structuredClone(PLAN_DETAIL))
  listTasks.mockResolvedValue(structuredClone(TASKS))
  getGrowthHistory.mockResolvedValue(structuredClone(GROWTH))
  listPlans.mockResolvedValue({ items: [structuredClone(PLAN_A)], total: 1 })
  exportResumeDocx.mockResolvedValue(new Blob(['docx'], { type: 'application/octet-stream' }))
  exportResumePdf.mockResolvedValue(new Blob(['pdf'], { type: 'application/pdf' }))
})

describe('ResumeView 自动带入与纸面', () => {
  it('只搬已验证事实：目标岗位/专业/技能/成长项目，姓名留空', async () => {
    seedPlans()
    const wrapper = mountResume()
    await flushPromises()

    expect(getPlan).toHaveBeenCalledWith('plan-a')
    expect(listTasks).toHaveBeenCalledWith({}, {}, 'plan-a')
    expect(getGrowthHistory).toHaveBeenCalledWith({}, 'plan-a')

    const paper = wrapper.get('.rm-sheet')
    expect(paper.get('.rm-paper-intent').text()).toContain('Java 后端开发')
    expect(paper.text()).toContain('软件工程')
    expect(paper.text()).toContain('掌握 - Java、Spring Boot')
    expect(paper.text()).toContain('了解 - Git')

    // 姓名没有来源，纸面维持占位而不是编一个名字
    expect(wrapper.get('.rm-paper-name').classes()).toContain('is-empty')
    expect(wrapper.get('.rm-paper-name').text()).toBe('你的姓名')

    // 成长记录里的项目优先，被作废的那条不出现
    expect(paper.text()).toContain('校园二手交易平台')
    expect(paper.text()).not.toContain('被撤回的项目')

    // 作废的那条不算进来源读数
    expect(wrapper.get('.rm-source').text()).toContain('成长记录 1 条')
    expect(wrapper.get('.rm-stat.is-pending').text()).toContain('4')
  })

  it('改姓名后纸面同步，必填待补减少', async () => {
    seedPlans()
    const wrapper = mountResume()
    await flushPromises()

    await wrapper.get('input[autocomplete="name"]').setValue('张三')
    await wrapper.get('input[autocomplete="tel"]').setValue('13800000000')
    await wrapper.get('input[autocomplete="email"]').setValue('zhangsan@example.com')
    await flushPromises()

    expect(wrapper.get('.rm-paper-name').text()).toBe('张三')
    expect(wrapper.get('.rm-paper-name').classes()).not.toContain('is-empty')
    expect(wrapper.get('.rm-stat.is-pending').text()).toContain('1')
    expect(wrapper.get('.rm-stat.is-pending strong').text()).toBe('1')
  })
})

describe('ResumeView 导出', () => {
  it('PDF 与 Word 都直接下载，文件名跟当前内容走', async () => {
    seedPlans()
    const wrapper = mountResume()
    await flushPromises()

    await wrapper.get('input[autocomplete="name"]').setValue('张三')

    await wrapper.findAll('.rm-actions .btn')[0].trigger('click')
    await flushPromises()

    expect(exportResumePdf).toHaveBeenCalledTimes(1)
    const pdfPayload = exportResumePdf.mock.calls[0][0]
    expect(pdfPayload.basic.name).toBe('张三')
    expect(pdfPayload.intent.targetJob).toBe('Java 后端开发')
    expect(pdfPayload.projects[0].title).toBe('校园二手交易平台')
    expect(saveBlob.mock.calls[0][1]).toMatch(/^张三.*\.pdf$/)

    await wrapper.findAll('.rm-actions .btn')[1].trigger('click')
    await flushPromises()

    expect(exportResumeDocx).toHaveBeenCalledTimes(1)
    const docxPayload = exportResumeDocx.mock.calls[0][0]
    expect(docxPayload.basic.name).toBe('张三')
    expect(saveBlob).toHaveBeenCalledTimes(2)
    expect(saveBlob.mock.calls[1][1]).toMatch(/^张三.*\.docx$/)
  })

  it('没有浏览器生成 PDF 时把提示原样显示出来', async () => {
    seedPlans()
    exportResumePdf.mockRejectedValueOnce(
      new Error('没有找到 Chrome 或 Edge，暂时无法生成 PDF；可以先导出 Word。'),
    )
    const wrapper = mountResume()
    await flushPromises()

    await wrapper.get('input[autocomplete="name"]').setValue('张三')
    await wrapper.findAll('.rm-actions .btn')[0].trigger('click')
    await flushPromises()

    expect(wrapper.get('.rm-alert').text()).toContain('没有找到 Chrome 或 Edge')
    expect(saveBlob).not.toHaveBeenCalled()
  })
})

describe('ResumeView 档案隔离与空态', () => {
  it('草稿按档案读取，切到另一份档案不会串', async () => {
    seedPlans([PLAN_A, PLAN_B])
    getPlan.mockImplementation(async (id) => ({
      ...structuredClone(PLAN_DETAIL),
      id,
      target_job: id === 'plan-b' ? '数据分析师' : 'Java 后端开发',
      profile_snapshot: { major: id === 'plan-b' ? '统计学' : '软件工程', skills: [] },
      current_skills: [],
    }))

    window.localStorage.setItem(
      'career-agent:resume:user-test:plan-a',
      JSON.stringify({
        version: 1,
        savedAt: '2026-11-10T00:00:00Z',
        resume: {
          basic: { name: '李四' },
          intent: { targetJob: 'Java 后端开发' },
          education: [{ school: '某某大学', major: '软件工程' }],
        },
      }),
    )

    const wrapper = mountResume()
    await flushPromises()
    expect(wrapper.get('.rm-paper-name').text()).toBe('李四')
    expect(wrapper.get('.rm-source').text()).toContain('继续编辑本机草稿')

    const plans = usePlansStore()
    plans.select('plan-b')
    await flushPromises()

    expect(wrapper.get('.rm-paper-name').text()).toBe('你的姓名')
    expect(wrapper.get('.rm-paper-intent').text()).toContain('数据分析师')
  })

  it('没有档案时给建档案入口', async () => {
    const auth = useAuthStore()
    auth.user = { id: 'user-test', username: 'tester' }
    listPlans.mockResolvedValue({ items: [], total: 0 })

    const wrapper = mountResume()
    await flushPromises()

    expect(wrapper.find('.rm-workspace').exists()).toBe(false)
    const gate = wrapper.get('.rm-gate')
    expect(gate.text()).toContain('先建一份岗位档案')
    expect(gate.get('a').attributes('href')).toBe('/analyze')
  })

  it('窄屏用编辑/预览开关切换', async () => {
    seedPlans()
    const wrapper = mountResume()
    await flushPromises()

    const workspace = wrapper.get('.rm-workspace')
    expect(workspace.classes()).toContain('is-edit')

    const tabs = wrapper.findAll('.rm-pane-switch button')
    expect(tabs.map((tab) => tab.text())).toEqual(['编辑', '预览'])
    await tabs[1].trigger('click')
    expect(workspace.classes()).toContain('is-preview')
    expect(tabs[1].attributes('aria-selected')).toBe('true')
  })
})
