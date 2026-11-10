/**
 * 能力差距表：等级判定与证据判定必须分开显示。
 *
 * 只验证“后端给的 display_status / verification 有没有如实出现在界面上”：
 * 自述达标不能画成「已满足」，跨技能推断必须写明不是直接证据。
 */

import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import SkillGapTable from '../SkillGapTable.vue'

function gap(overrides = {}) {
  return {
    skill: 'Python',
    status: '已满足',
    display_status: '已满足',
    verification: 'direct',
    supported_by: '无',
    current_level: '中级',
    required_level: '中级',
    importance: 5,
    priority: '低',
    gap: '',
    evidence_kind: 'project',
    evidence_ref: '',
    evidence_note: '课程项目：用 Python 写了爬虫',
    ...overrides,
  }
}

function mountTable(gaps, props = {}) {
  return mount(SkillGapTable, {
    props: { gaps, ...props },
  })
}

describe('SkillGapTable', () => {
  it('自述达标显示为未验证，而不是已满足', () => {
    const wrapper = mountTable([
      gap({
        display_status: '未验证',
        verification: 'unverified',
        evidence_kind: 'self_report',
        evidence_ref: '',
        evidence_note: '简历里写了会 Python',
        priority: '中',
      }),
    ])

    const text = wrapper.text()
    expect(text).toContain('未验证')
    expect(text).not.toContain('已满足')
    expect(wrapper.find('.badge-warn').exists()).toBe(true)
    expect(text).toContain('只有自述，还没有项目或成长记录')
  })

  it('跨技能推断写明支撑技能与非直接证据', () => {
    const wrapper = mountTable([
      gap({
        skill: '教学设计',
        status: '部分满足',
        display_status: '疑似具备',
        verification: 'inferred',
        supported_by: '教育心理学',
        evidence_kind: 'growth',
        evidence_ref: '成长记录：2026-05 支教课程设计',
        evidence_note: '支教项目：设计过一节课',
        gap: '等级上可由「教育心理学」迁移，但缺少「教学设计」的直接记录。',
      }),
    ])

    const text = wrapper.text()
    expect(text).toContain('疑似具备')
    expect(text).toContain('支撑技能：教育心理学')
    expect(text).toContain('成长记录')
    expect(text).toContain('佐证：成长记录：2026-05 支教课程设计')
    expect(text).toContain('证据：支教项目：设计过一节课')
    expect(text).toContain('靠相近技能推断，没有这项技能的直接证据')
  })

  it('直接证据的已满足不显示告警行', () => {
    const wrapper = mountTable([gap()])

    const text = wrapper.text()
    expect(text).toContain('已满足')
    expect(text).toContain('证据：课程项目：用 Python 写了爬虫')
    expect(text).not.toContain('靠相近技能推断')
    expect(text).not.toContain('只有自述')
  })

  it('没有 display_status 的旧结果回落到 status', () => {
    const wrapper = mountTable([
      gap({ status: '明显不足', display_status: undefined, verification: undefined, priority: '高' }),
    ])

    expect(wrapper.text()).toContain('明显不足')
    expect(wrapper.find('.badge-danger').exists()).toBe(true)
  })

  it('showSupportedBy=false 时不显示支撑技能行', () => {
    const wrapper = mountTable(
      [gap({ supported_by: '教育心理学', display_status: '疑似具备', verification: 'inferred' })],
      { showSupportedBy: false },
    )

    expect(wrapper.text()).not.toContain('支撑技能')
  })
})
