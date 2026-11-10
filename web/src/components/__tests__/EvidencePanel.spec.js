/**
 * 依据面板：编号钉死在数组位置，分组只管显示顺序。
 *
 * 最要紧的一件事：「你粘贴的原话」和「内置资料摘要」不能混在一栏里统称为原文 ——
 * 那等于替一家从没看过的公司说话。
 */

import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import EvidencePanel from '../EvidencePanel.vue'

function sentence(text, extra = {}) {
  return {
    provenance: 'user_pasted',
    note: text,
    skill: 'Python',
    requirement_type: 'required',
    ...extra,
  }
}

function digest(text, extra = {}) {
  return sentence(text, {
    provenance: 'official_digest',
    source_id: 'jd_hundsun_001',
    requirement_type: 'preferred',
    ...extra,
  })
}

function mountPanel(props = {}) {
  return mount(EvidencePanel, {
    props: {
      evidence: [],
      status: '',
      diagnostics: null,
      requirements: null,
      ...props,
    },
  })
}

describe('EvidencePanel 分组', () => {
  it('原话和整理稿各占一组，标题写清各自是什么', () => {
    const text = mountPanel({
      evidence: [sentence('会用 Python 写脚本'), digest('熟悉 SQL 取数')],
      status: 'user_pasted',
    }).text()

    expect(text).toContain('你粘贴的招聘要求：1 句原话')
    expect(text).toContain('同类岗位资料：1 条资料')
    expect(text).toContain('不是这家公司的原话')
  })

  it('分组不重排编号：位置在第 3 条的原话仍然是 E03', () => {
    const wrapper = mountPanel({
      evidence: [sentence('第一条原话'), digest('一条整理稿'), sentence('第三条原话')],
    })
    const text = wrapper.text()

    expect(text).toContain('E01')
    expect(text).toContain('E03')
    expect(text).not.toContain('E04')
    expect(wrapper.findAll('details').at(1).text()).toContain('E02')
  })

  it('旧版本没有 provenance 的条目落在「其他资料条目」，不冒充原话', () => {
    const text = mountPanel({ evidence: [{ note: '一条老证据', skill: 'Excel' }] }).text()

    expect(text).toContain('其他资料条目：1 条旧依据')
    expect(text).not.toContain('你粘贴的招聘要求')
  })

  it('只有整理稿时标题不写成「你提供的原文」', () => {
    const head = mountPanel({
      evidence: [digest('熟悉分布式系统'), digest('了解 Docker')],
      status: 'official_digest',
    }).find('.card-head').text()

    expect(head).toContain('同类岗位资料（不是你粘贴的要求）')
    expect(head).toContain('可以回查，但不是这家公司的原话')
    expect(head).toContain('来自同类岗位资料')
    expect(head).not.toContain('你提供的招聘要求')
  })

  it('条目上的编号可点，不摆数据库里的编号', () => {
    const text = mountPanel({ evidence: [digest('一条整理稿')] }).text()

    expect(text).toContain('E01')
    expect(text).toContain('优先考虑')
    expect(text).not.toContain('jd_hundsun_001')
  })
})

describe('EvidencePanel 计数与降级', () => {
  it('内置资料条目被截断时说明丢了多少条', () => {
    const text = mountPanel({
      evidence: [digest('带进来的第一条')],
      diagnostics: { dropped_entries: 9 },
    }).text()

    expect(text).toContain('另有 9 条没参与分析')
    expect(text).not.toContain('原文太长')
  })

  it('原文超长被丢弃的是「句」，与资料条目的措辞不混用', () => {
    const text = mountPanel({
      evidence: [sentence('带进来的第一句')],
      diagnostics: { dropped_sentences: 7 },
    }).text()

    expect(text).toContain('已丢弃 7 句')
    expect(text).not.toContain('没进分析')
  })

  it('依据为空数组：说清没有可回查的东西，并给出补救动作', () => {
    const text = mountPanel({
      evidence: [],
      status: 'no_source',
      diagnostics: { reason: 'no_jd_text' },
    }).text()

    expect(text).toContain('没有一句能回查')
    expect(text).toContain('把招聘页的「任职要求」粘到上面')
  })

  it('字段缺失和「真的没有依据」是两句话', () => {
    expect(mountPanel({ evidence: null }).text()).toContain('这次没有拿到岗位依据')
    expect(mountPanel({ evidence: [] }).text()).not.toContain('这次没有拿到岗位依据')
  })

  it('反向引用仍按编号回指岗位要求', () => {
    const text = mountPanel({
      evidence: [sentence('会用 Python'), digest('熟悉 SQL')],
      requirements: [
        { skill: 'Python 脚本', evidence_refs: ['E01'] },
        { skill: '数据库', evidence_refs: ['E02', 'E99'] },
      ],
    }).text()

    expect(text).toContain('Python 脚本')
    expect(text).toContain('数据库')
    expect(text).not.toContain('E99')
  })
})
