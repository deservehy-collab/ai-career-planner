/**
 * 页面骨架 PageShell 的契约：全站只有一套版面。
 *
 * 这里锁四件事，改了就会让某一页跟别的页长得不一样：
 * 1. 报头永远是 eyebrow + title，deck 是可缺省的副题；
 * 2. 有 rail 才出现细栏，并且 --rail/--full 两档互斥；
 * 3. 细栏的无障碍名字可由 railLabel 覆盖，默认「次要读数」；
 * 4. 默认插槽永远落在 .pg-main —— 主线只有一个。
 */

import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import PageShell from '../PageShell.vue'

function shell(slots = {}, props = {}) {
  return mount(PageShell, {
    props: { title: '概览', ...props },
    slots: { default: '<p class="probe">主线</p>', ...slots },
  })
}

describe('PageShell 骨架', () => {
  it('报头固定是 eyebrow + title，deck 缺省时不占位', () => {
    const withDeck = shell({ deck: '<span>这一屏只回答三件事</span>' }, { eyebrow: '本地化 AI 求职规划' })
    expect(withDeck.get('.pg-mast .pg-eyebrow').text()).toBe('本地化 AI 求职规划')
    expect(withDeck.get('.pg-mast .pg-title').text()).toBe('概览')
    expect(withDeck.get('.pg-deck').text()).toContain('这一屏只回答三件事')

    const withoutDeck = shell()
    expect(withoutDeck.find('.pg-deck').exists()).toBe(false)
    expect(withoutDeck.find('.pg-eyebrow').exists()).toBe(false)
  })

  it('默认插槽落在 .pg-main，细栏只在给了 rail 时才出现', () => {
    const full = shell()
    expect(full.get('.pg-main .probe').text()).toBe('主线')
    expect(full.get('.pg-body').classes()).toContain('pg-body--full')
    expect(full.find('.pg-index').exists()).toBe(false)

    const railed = shell({ rail: '<p>目标岗位</p>' })
    expect(railed.get('.pg-body').classes()).toContain('pg-body--rail')
    expect(railed.get('.pg-index').text()).toBe('目标岗位')
  })

  it('细栏的无障碍名字默认是「次要读数」，页面可以改成自己的说法', () => {
    expect(shell({ rail: 'x' }).get('.pg-index').attributes('aria-label')).toBe('次要读数')
    expect(shell({ rail: 'x' }, { railLabel: '我的档案摘要' })
      .get('.pg-index').attributes('aria-label')).toBe('我的档案摘要')
  })
})
