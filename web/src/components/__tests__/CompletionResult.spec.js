/**
 * 提交结果：自述完成与有凭证必须一眼分得清。
 *
 * 锁住的契约：
 * 1. 没附凭证时不把空证据写成「（空）」，而是明说「自述完成 / 未附凭证」；
 * 2. 心得单独显示，不冒充凭证；
 * 3. 有凭证的成果标出「已附凭证」。
 */

import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import CompletionResult from '../CompletionResult.vue'

function result(overrides = {}) {
  return {
    status: 'ok',
    completion_status: 'completed',
    completion_written: true,
    growth_event_status: 'ok',
    growth_evaluated: true,
    growth_outcome: 'upgraded',
    task: {
      completed_at: '2026-11-05T00:00:00Z',
      evidence: '',
      note: '终于跑通了',
    },
    growth_event: {
      type: 'learning',
      title: '做完单元测试',
      skills: ['Python'],
      evidence: '',
      evidence_kind: 'self_report',
      note: '终于跑通了',
    },
    updates: [],
    blocked: [],
    skipped: [],
    warnings: [],
    ...overrides,
  }
}

describe('CompletionResult 自述完成', () => {
  it('没附凭证时标出自述完成与未附凭证，不再写（空）', () => {
    const wrapper = mount(CompletionResult, { props: { result: result() } })

    expect(wrapper.text()).toContain('自述完成')
    expect(wrapper.text()).toContain('未附凭证')
    expect(wrapper.text()).not.toContain('（空）')
    expect(wrapper.text()).toContain('终于跑通了')
  })

  it('有凭证时标出已附凭证，不再出现未附凭证', () => {
    const wrapper = mount(CompletionResult, {
      props: {
        result: result({
          task: {
            completed_at: '2026-11-05T00:00:00Z',
            evidence: 'https://github.com/demo/tests',
            note: '',
          },
          growth_event: {
            type: 'learning',
            title: '做完单元测试',
            skills: ['Python'],
            evidence: 'https://github.com/demo/tests',
            evidence_kind: 'growth',
            note: '',
          },
        }),
      },
    })

    expect(wrapper.text()).toContain('已附凭证')
    expect(wrapper.text()).not.toContain('未附凭证')
  })
})
