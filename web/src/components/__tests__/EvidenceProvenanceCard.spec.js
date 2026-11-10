/**
 * 来源账卡：界面只读后端算好的 source_report，不自己判来源。
 *
 * 关键在"陌生岗位"那句话的几种说法 —— 目录读不出来时不能谎称岗位不在里面，
 * 有原文时又不能说成"全是模型推断"；内置资料摘要能回查，但要和原话分两个数摆。
 */

import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import EvidenceProvenanceCard from '../EvidenceProvenanceCard.vue'

function report(overrides = {}) {
  return {
    target_job: '后端开发',
    requirement_basis: 'user_pasted',
    counts: { user_jd: 3, official_data: 0, knowledge_base: 1, model_inference: 2, total: 6 },
    citations: [],
    model_only_skills: ['沟通', '抗压'],
    evidence_sentence_count: 8,
    official_source_ids: [],
    catalog_profile_matched: true,
    job_profile_origin: 'generated',
    ...overrides,
  }
}

/** 后端新加的按 provenance 分类计数；不传就是旧结果，走 evidence_sentence_count 兜底。 */
function split(overrides = {}) {
  return {
    user_jd: 8,
    official_data: 0,
    knowledge_base: 0,
    model_inference: 0,
    unattributed: 0,
    ...overrides,
  }
}

function mountCard(props = {}) {
  return mount(EvidenceProvenanceCard, {
    props: { report: report(), basis: 'user_pasted', ...props },
  })
}

describe('EvidenceProvenanceCard', () => {
  it('四档计数与可回查句数原样摆出来', () => {
    const text = mountCard().text()

    expect(text).toContain('你粘贴的招聘要求')
    expect(text).toContain('3 条')
    expect(text).toContain('同类岗位的常见要求')
    expect(text).toContain('1 条')
    expect(text).toContain('AI 推断')
    expect(text).toContain('2 条')
    expect(text).toContain('8 句')
  })

  it('模型推断项单列出来，并说明"降权不删除"', () => {
    const text = mountCard().text()

    expect(text).toContain('AI 推断出来的要求')
    expect(text).toContain('沟通')
    expect(text).toContain('没有删掉')
    expect(text).not.toContain('用到的资料出处')
  })

  it('资料出处只报份数，不摆数据库里的编号', () => {
    const text = mountCard({
      report: report({
        counts: { user_jd: 3, official_data: 2, knowledge_base: 1, model_inference: 2, total: 8 },
        official_source_ids: ['source_acct_std_001', 'source_acct_std_002'],
      }),
    }).text()

    expect(text).toContain('公开资料摘要')
    expect(text).toContain('2 条')
    expect(text).not.toContain('source_acct_std_001')
  })

  it('旧结果没有分来源计数时，全部按你粘的原话计', () => {
    const text = mountCard().text()

    expect(text).toContain('你粘贴的要求')
    expect(text).toContain('8 句')
    expect(text).not.toContain('同类岗位资料')
  })

  it('原话和内置摘要是两个数，各数各的', () => {
    const text = mountCard({
      report: report({
        evidence_by_source: split({ user_jd: 5, official_data: 12 }),
        evidence_sentence_count: 17,
      }),
    }).text()

    expect(text).toContain('5 句')
    expect(text).toContain('同类岗位资料')
    expect(text).toContain('12 条')
  })

  it('没粘原文、只用内置摘要：说清"能回查但不是这家公司的话"', () => {
    const text = mountCard({
      report: report({
        requirement_basis: 'knowledge_base',
        evidence_by_source: split({ user_jd: 0, official_data: 14 }),
        official_source_ids: ['jd_hundsun_001'],
      }),
      basis: 'knowledge_base',
    }).text()

    expect(text).toContain('这次没有粘贴招聘要求')
    expect(text).toContain('共 1 份')
    expect(text).toContain('同类岗位的通行说法')
    expect(text).toContain('不代表这家公司真实写过')
    expect(text).not.toContain('没有一句能回查')
  })

  it('目录里没有、又没粘原文：说成全部模型推断', () => {
    const text = mountCard({
      report: report({ catalog_profile_matched: false, evidence_sentence_count: 0 }),
      basis: 'model_only',
    }).text()

    expect(text).toContain('不在已有的岗位资料里')
    expect(text).toContain('没有一句能回查')
  })

  it('目录里没有、但有原文：不能说成模型推断', () => {
    const text = mountCard({
      report: report({ catalog_profile_matched: false }),
    }).text()

    expect(text).toContain('你粘贴的 8 句要求里')
    expect(text).toContain('3 条岗位要求能直接对上原话')
    expect(text).toContain('其余 2 条是 AI 按常识补的')
    expect(text).not.toContain('没有一句能回查')
  })

  it('粘了原文却一条都没对上：不写"照样能回查"，按模型推断对待', () => {
    // 数字来自实测：陌生岗位（护士）粘 3 句原文，7 条要求 0 条命中原话
    const wrapper = mountCard({
      report: report({
        catalog_profile_matched: false,
        counts: { user_jd: 0, official_data: 0, knowledge_base: 0, model_inference: 7, total: 7 },
        evidence_sentence_count: 3,
      }),
    })

    expect(wrapper.text()).toContain('这次粘贴了 3 句要求')
    expect(wrapper.text()).toContain('没有一条岗位要求能对上原话')
    expect(wrapper.text()).toContain('7 条都按 AI 推断对待')
    expect(wrapper.text()).not.toContain('照样能逐条回查')
    expect(wrapper.find('.notice').classes()).toContain('notice-warn')
  })

  it('目录里没有、只有内置摘要：承认能回查但点明是同类岗位', () => {
    const text = mountCard({
      report: report({
        catalog_profile_matched: false,
        evidence_sentence_count: 0,
        evidence_by_source: split({ user_jd: 0, official_data: 6 }),
      }),
    }).text()

    expect(text).toContain('不在已有的岗位资料里')
    expect(text).toContain('6 条同类岗位资料')
    expect(text).toContain('不是这家公司的要求')
    expect(text).not.toContain('没有一句能回查')
  })

  it('目录读不出来时不谎称岗位不在里面', () => {
    const text = mountCard({
      report: report({ catalog_profile_matched: null }),
    }).text()

    expect(text).toContain('无法判断这个岗位有没有记录')
    expect(text).not.toContain('不在已有的岗位资料里')
  })

  it('较早的结果没有 source_report 时如实说明', () => {
    const text = mountCard({ report: null }).text()

    expect(text).toContain('这份结果里没有资料统计')
    expect(text).toContain('重新分析一次')
  })

  it('每条要求都能回查时不摆模型推断清单', () => {
    const text = mountCard({
      report: report({
        counts: { user_jd: 4, official_data: 0, knowledge_base: 0, model_inference: 0, total: 4 },
        model_only_skills: [],
      }),
    }).text()

    expect(text).toContain('都能回查到依据')
    expect(text).not.toContain('AI 推断出来的要求')
  })
})
