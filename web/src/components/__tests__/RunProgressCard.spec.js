/** 进度卡：后端回报节点时点名当前步骤与已完成步数；没有节点信息时不编造。 */

import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import RunProgressCard from '../RunProgressCard.vue'

describe('RunProgressCard 节点进度', () => {
  it('运行中点名当前节点并显示已完成步数', () => {
    const wrapper = mount(RunProgressCard, {
      props: {
        status: 'running',
        startedAt: new Date().toISOString(),
        progress: [
          { node: 'profile', status: 'done' },
          { node: 'growth', status: 'done' },
          { node: 'retrieval', status: 'running' },
        ],
        currentNode: 'retrieval',
      },
    })

    expect(wrapper.text()).toContain('正在执行「招聘要求整理」')
    expect(wrapper.text()).toContain('第 3/7 步')
    expect(wrapper.text()).toContain('已完成 2/7 步')
    wrapper.unmount()
  })

  it('两个节点并行时列出全部在跑的步骤，不只报最后一个', () => {
    const wrapper = mount(RunProgressCard, {
      props: {
        status: 'running',
        startedAt: new Date().toISOString(),
        progress: [
          { node: 'profile', status: 'running' },
          { node: 'retrieval', status: 'running' },
        ],
        currentNode: 'retrieval',
      },
    })

    expect(wrapper.text()).toContain('正在并行执行「能力画像」「招聘要求整理」')
    expect(wrapper.text()).not.toContain('第 3/7 步')
    wrapper.unmount()
  })

  it('同一节点被重复回报时，已完成步数按节点去重，不会出现 8/7', () => {
    const done = (node) => ({ node, status: 'done' })
    const wrapper = mount(RunProgressCard, {
      props: {
        status: 'running',
        startedAt: new Date().toISOString(),
        progress: [
          done('profile'),
          done('growth'),
          done('retrieval'),
          done('job'),
          done('job'),
          done('assessment'),
          done('learning'),
          done('project'),
        ],
      },
    })

    expect(wrapper.text()).toContain('已完成 7/7 步')
    expect(wrapper.text()).not.toContain('已完成 8/7 步')
    wrapper.unmount()
  })

  it('旧记录没有节点进度时保留原来的整段说明', () => {
    const wrapper = mount(RunProgressCard, {
      props: { status: 'running', startedAt: new Date().toISOString() },
    })

    expect(wrapper.text()).toContain('正在分析岗位、能力和学习路线')
    expect(wrapper.text()).not.toContain('第 1/7 步')
    wrapper.unmount()
  })
})
