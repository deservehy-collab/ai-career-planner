/**
 * 目标结局卡：达成与"依据不足"必须说成两回事。
 *
 * 能力达标但岗位要求是模型推断时，界面不能出现"目标已达成"。
 */

import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import GoalCard from '../GoalCard.vue'

function goal(overrides = {}) {
  return {
    status: 'achieved',
    basis: 'user_pasted',
    grounded: true,
    required_count: 2,
    satisfied_count: 2,
    remaining_count: 0,
    remaining: [],
    remaining_high: [],
    needs_verification: [],
    unmet: [],
    ungrounded_requirements: [],
    message: '岗位要求的 2 项能力均已满足，并且都有项目经历或成长记录作为直接证据。',
    ...overrides,
  }
}

function mountCard(props = {}) {
  return mount(GoalCard, {
    props: { goal: goal(), targetJob: 'AI 应用开发工程师', ...props },
  })
}

describe('GoalCard', () => {
  it('达成时给出收尾结论、依据与达标计数', () => {
    const wrapper = mountCard()
    const text = wrapper.text()

    expect(text).toContain('目标结局')
    expect(text).toContain('目标已达成')
    expect(text).toContain('2 / 2 项')
    expect(text).toContain('你粘贴的招聘要求')
    expect(text).toContain('这个目标到此收尾')
    expect(text).toContain('不会自动生成下一轮任务')
    expect(wrapper.find('.badge-ok').exists()).toBe(true)
  })

  it('依据不足时不能宣布达成，并列出模型补充项', () => {
    const wrapper = mountCard({
      goal: goal({
        status: 'basis_insufficient',
        basis: 'user_pasted',
        grounded: false,
        ungrounded_requirements: ['LangGraph'],
        message: '能力表已达到全部要求，但其中 1 项要求是模型补充的（LangGraph）、招聘原文里没有，无法确认目标达成。',
      }),
    })
    const text = wrapper.text()

    expect(text).toContain('目标结局：无法确认')
    expect(text).toContain('依据不足，无法确认达成')
    expect(text).toContain('不能宣布达成')
    expect(text).toContain('「LangGraph」是 AI 补充的')
    expect(text).not.toContain('这个目标到此收尾')
    expect(wrapper.find('.badge-warn').exists()).toBe(true)
  })

  it('模型推断的岗位要求会标成没有原文或画像依据', () => {
    const wrapper = mountCard({
      goal: goal({
        status: 'basis_insufficient',
        basis: 'model_only',
        grounded: false,
        message: '能力表已达到全部要求，但岗位要求既没有招聘原文、也不在内置岗位画像里，属于模型推断，无法确认目标达成。',
      }),
    })
    expect(wrapper.text()).toContain('AI 推断（没有招聘要求或岗位资料可依）')
  })
})
