/**
 * 流程轨显示**后端回报的真实节点状态**。
 *
 * 后端 run 记录的 progress 是执行体自己写的：哪个节点在跑、哪个跑完、
 * 哪个失败/被停止/本轮不执行。界面只做映射，不按时间推算，也不给未回报
 * 的节点编状态。
 */

import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import AgentPipeline from '../AgentPipeline.vue'

function nodeStates(wrapper) {
  return wrapper.findAll('.agent-node').map((node) => node.text())
}

describe('AgentPipeline 节点进度', () => {
  it('运行中按 progress 标出已完成与正在进行，并点名当前节点', () => {
    const wrapper = mount(AgentPipeline, {
      props: {
        status: 'running',
        progress: [
          { node: 'profile', status: 'done' },
          { node: 'growth', status: 'running' },
        ],
        currentNode: 'growth',
      },
    })

    const cards = nodeStates(wrapper)
    expect(cards[0]).toContain('已完成')
    expect(cards[1]).toContain('进行中')
    expect(cards[2]).not.toContain('进行中')
    expect(wrapper.text()).toContain('正在执行「成长追踪」')
    expect(wrapper.text()).toContain('第 2/7 步')
  })

  it('两个节点并行时把两个都在跑的步骤都点名，而不是只报最后一个', () => {
    const wrapper = mount(AgentPipeline, {
      props: {
        status: 'running',
        progress: [
          { node: 'retrieval', status: 'running' },
          { node: 'profile', status: 'running' },
        ],
        currentNode: 'profile',
      },
    })

    const cards = nodeStates(wrapper)
    expect(cards[0]).toContain('进行中')
    expect(cards[2]).toContain('进行中')
    expect(wrapper.text()).toContain('正在并行执行「能力画像」「招聘要求整理」')
    expect(wrapper.text()).not.toContain('第 2/7 步')
  })

  it('失败时指出断在哪个节点，而不是只说"中断了"', () => {
    const wrapper = mount(AgentPipeline, {
      props: {
        status: 'failed',
        progress: [
          { node: 'profile', status: 'done' },
          { node: 'job', status: 'failed', error: 'ValueError: 空要求' },
        ],
        currentNode: null,
      },
    })

    expect(nodeStates(wrapper)[3]).toContain('失败')
    expect(wrapper.text()).toContain('「岗位要求」这一步中断了')
  })

  it('条件分支没走到的节点标为已跳过', () => {
    const wrapper = mount(AgentPipeline, {
      props: {
        status: 'succeeded',
        progress: [
          { node: 'learning', status: 'skipped' },
          { node: 'project', status: 'skipped' },
        ],
      },
    })

    expect(nodeStates(wrapper)[5]).toContain('已跳过')
    expect(nodeStates(wrapper)[6]).toContain('已跳过')
  })

  it('停止分析时标出被停止的节点', () => {
    const wrapper = mount(AgentPipeline, {
      props: {
        status: 'failed',
        progress: [{ node: 'learning', status: 'canceled' }],
      },
    })

    expect(nodeStates(wrapper)[5]).toContain('已停止')
    expect(wrapper.text()).toContain('「学习路线」这一步被停止')
  })

  it('旧记录没有节点进度时退回原来的整条轨说法，不编造单节点状态', () => {
    const running = mount(AgentPipeline, {
      props: { status: 'running', progress: [] },
    })
    expect(running.text()).toContain('七个步骤正在依次进行')
    expect(running.text()).not.toContain('进行中')

    const failed = mount(AgentPipeline, {
      props: { status: 'failed', progress: [] },
    })
    expect(failed.text()).toContain('暂时看不出是哪一步出的问题')
  })
})
