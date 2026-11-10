/**
 * 岗位画像卡：三条来源必须分开说。
 *
 * 画像每条的 source 是代码回查出来的（原话里找得到才算 jd），
 * 界面不能把"模型补的"画成"有据可查的"。
 */

import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import JobProfileCard from '../JobProfileCard.vue'

function entry(name, source, extras = {}) {
  return { name, detail: `${name}的说明`, source, evidence_refs: [], source_notes: [], ...extras }
}

function profile(overrides = {}) {
  return {
    target_job: '会计',
    summary: '负责日常账务处理与报表编制。',
    core_tasks: [entry('审核原始凭证', 'jd', { evidence_refs: ['E01'], source_notes: ['负责原始凭证审核'] })],
    tools: [entry('财务软件', 'standard')],
    deliverables: [entry('月度报表', 'model')],
    entry_requirements: [],
    career_path: [],
    ...overrides,
  }
}

function mountCard(props = {}) {
  return mount(JobProfileCard, { props: { profile: profile(), origin: 'generated', ...props } })
}

describe('JobProfileCard', () => {
  it('五条分区只摆有内容的，并按来源分开标注', () => {
    const wrapper = mountCard()
    const text = wrapper.text()

    expect(text).toContain('核心工作任务')
    expect(text).toContain('常用工具与方法')
    expect(text).toContain('交付物')
    expect(text).not.toContain('入行门槛')
    expect(text).toContain('你粘贴的要求里有')
    expect(text).toContain('同类岗位的常见要求（人工整理）')
    expect(text).toContain('AI 补充（要求里没写）')
  })

  it('原话能折叠展开，编号芯片可点', async () => {
    const wrapper = mountCard()
    const details = wrapper.find('details.fold')
    expect(details.exists()).toBe(true)

    await wrapper.find('.chip').trigger('click')
    expect(wrapper.emitted('select-ref')?.[0]).toEqual(['E01'])
  })

  it('origin 如实透出：缓存与本次生成不是一回事', () => {
    expect(mountCard({ origin: 'cache' }).text()).toContain('沿用之前保存的岗位资料')
    expect(mountCard({ origin: 'knowledge_base' }).text()).toContain('没有调用 AI')
  })

  it('没有画像时说明降级，不摆空表', () => {
    const wrapper = mountCard({ profile: null, origin: 'unavailable' })
    const text = wrapper.text()

    expect(text).toContain('本次没有可用的岗位资料')
    expect(text).toContain('岗位要求与学习路线仍按已有依据正常产出')
    expect(wrapper.find('.data').exists()).toBe(false)
  })

  it('画像清洗告警原样透出', () => {
    const wrapper = mountCard({ warnings: ['岗位画像的「交付物」有 2 条重复，已合并'] })
    expect(wrapper.text()).toContain('有 2 条重复，已合并')
  })
})
