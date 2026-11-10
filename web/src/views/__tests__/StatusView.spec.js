/**
 * 系统状态页：模型通道切换（P3 前端自动化）。
 *
 * 只验证"用户点了会发生什么"：调接口 → 重新体检 → 失败时把原因显示出来；
 * 以及没配 key 时云端选项不可点。后端行为由 Python 测试覆盖。
 */

import { h } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../../api/endpoints', () => ({
  getHealth: vi.fn(),
  setLlmChannel: vi.fn(),
}))

import { getHealth, setLlmChannel } from '../../api/endpoints'
import { useHealthStore } from '../../stores/health'
import StatusView from '../StatusView.vue'

function healthPayload(overrides = {}) {
  const base = {
    status: 'ok',
    llm_channel: {
      channel: 'local',
      cloud_configured: true,
      active_model: 'qwen2.5:3b',
      cloud_model: 'glm-4.7-flash',
      cloud_auth_failed: false,
    },
    ollama: {
      ok: true,
      base_url: 'http://127.0.0.1:11434',
      chat_model: 'qwen2.5:3b',
      chat_model_ready: true,
      models: ['qwen2.5:3b'],
      error: null,
    },
    knowledge: {
      skills: { ok: true, count: 42, error: null },
      projects: { ok: true, count: 16, error: null },
    },
    stores: {
      growth: {
        status: 'ok', writable: true, exists: true,
        path: 'runs/users/user_1/growth.json', warning: null,
      },
      loop: {
        status: 'ok', writable: true, exists: true,
        path: 'runs/users/user_1/loop.json', rounds: 1, warning: null,
      },
    },
    config: { chat_model: 'qwen2.5:3b', runs_dir: 'runs', task_workers: 1 },
  }
  return {
    ...base,
    ...overrides,
    llm_channel: { ...base.llm_channel, ...(overrides.llm_channel || {}) },
  }
}

function primeHealth(overrides = {}) {
  const store = useHealthStore()
  store.data = healthPayload(overrides)
  return store
}

const RouterLinkStub = {
  props: ['to'],
  setup(props, { slots }) {
    return () => h('a', { href: props.to }, slots.default?.())
  },
}

function mountView(props = {}) {
  return mount(StatusView, {
    props: { tab: 'ai', ...props },
    global: { stubs: { RouterLink: RouterLinkStub } },
  })
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

describe('StatusView 模型通道切换', () => {
  it('点击云端后调用切换接口并重新体检', async () => {
    primeHealth()
    setLlmChannel.mockResolvedValue({ channel: 'cloud' })
    getHealth.mockResolvedValue(healthPayload({
      llm_channel: { channel: 'cloud', active_model: 'glm-4.7-flash' },
    }))

    const wrapper = mountView()
    await wrapper.get('input[value="cloud"]').trigger('change')
    await flushPromises()

    expect(setLlmChannel).toHaveBeenCalledTimes(1)
    expect(setLlmChannel).toHaveBeenCalledWith('cloud')
    expect(getHealth).toHaveBeenCalledTimes(1)
    expect(wrapper.text()).toContain('云端运行')
  })

  it('没配置 key 时云端选项置灰，不能误点', () => {
    primeHealth({ llm_channel: { cloud_configured: false } })

    const wrapper = mountView()
    const cloud = wrapper.get('input[value="cloud"]')

    expect(cloud.attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('还没有配置云端密钥，暂时不能选')
  })

  it('切换失败时显示原因，不静默吞掉', async () => {
    primeHealth()
    setLlmChannel.mockRejectedValue(new Error('未配置 CLOUD_API_KEY，不能切到云端'))

    const wrapper = mountView()
    await wrapper.get('input[value="cloud"]').trigger('change')
    await flushPromises()

    expect(wrapper.text()).toContain('还没有配置云端密钥，暂时不能切到云端')
    expect(getHealth).not.toHaveBeenCalled()
  })
})

describe('StatusView 多级子页面', () => {
  it('体检摘要常驻，默认落在 AI 与运行', () => {
    primeHealth()
    const wrapper = mountView()

    expect(wrapper.find('.status-hero').exists()).toBe(true)
    const links = wrapper.findAll('.section-nav__link')
    expect(links).toHaveLength(3)
    expect(links[0].attributes('aria-current')).toBe('page')
    expect(wrapper.get('.pg-crumb').text()).toContain('系统状态')
    expect(wrapper.get('.page-head h1').text()).toBe('AI 与运行')
    expect(wrapper.find('.status-channel').exists()).toBe(true)
    expect(wrapper.find('.status-stores').exists()).toBe(false)
  })

  it('资料与存储子页只显示资料和存储卡', () => {
    primeHealth()
    const wrapper = mountView({ tab: 'data' })

    expect(wrapper.get('.page-head h1').text()).toBe('资料与存储')
    expect(wrapper.findAll('.section-nav__link')[1].attributes('aria-current')).toBe('page')
    expect(wrapper.find('.status-stores').exists()).toBe(true)
    expect(wrapper.find('.status-channel').exists()).toBe(false)
  })

  it('排查配置子页只显示运行设置', () => {
    primeHealth()
    const wrapper = mountView({ tab: 'diag' })

    expect(wrapper.get('.page-head h1').text()).toBe('排查配置')
    expect(wrapper.find('.status-config').exists()).toBe(true)
    expect(wrapper.find('.status-stores').exists()).toBe(false)
  })
})
