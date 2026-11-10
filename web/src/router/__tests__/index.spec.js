/**
 * 多级页面路由契约：每个子页都有自己的地址，地址决定渲染哪个子页。
 * 只要路由表被改回"一个地址 + 页内切换"，这里就会失败。
 */

import { describe, expect, it } from 'vitest'

import router from '../index'

function resolved(path) {
  const route = router.resolve(path)
  return {
    matched: route.matched,
    props: route.matched[0]?.props?.default,
    meta: route.meta,
  }
}

describe('学习与项目子路由', () => {
  it('任务清单、学习路线、实战项目各有地址并映射到对应子页', () => {
    expect(resolved('/tasks')).toMatchObject({
      props: { tab: 'tasks' },
      meta: { title: '学习与项目', sub: '任务清单' },
    })
    expect(resolved('/tasks/learning')).toMatchObject({
      props: { tab: 'learning' },
      meta: { title: '学习与项目', sub: '学习路线' },
    })
    expect(resolved('/tasks/projects')).toMatchObject({
      props: { tab: 'projects' },
      meta: { title: '学习与项目', sub: '实战项目' },
    })
  })
})

describe('系统状态子路由', () => {
  it('AI 与运行、资料与存储、排查配置各有地址并映射到对应子页', () => {
    expect(resolved('/status')).toMatchObject({
      props: { tab: 'ai' },
      meta: { title: '系统状态', sub: 'AI 与运行' },
    })
    expect(resolved('/status/data')).toMatchObject({
      props: { tab: 'data' },
      meta: { title: '系统状态', sub: '资料与存储' },
    })
    expect(resolved('/status/diag')).toMatchObject({
      props: { tab: 'diag' },
      meta: { title: '系统状态', sub: '排查配置' },
    })
  })
})

describe('切页滚动位置', () => {
  const scrollBehavior = router.options.scrollBehavior

  it('同一板块的子页之间原地换页，不回到顶部', () => {
    expect(scrollBehavior({ path: '/tasks/learning' }, { path: '/tasks' }, null)).toBe(false)
    expect(scrollBehavior({ path: '/status/diag' }, { path: '/status/data' }, null)).toBe(false)
  })

  it('换板块才回到顶部', () => {
    expect(scrollBehavior({ path: '/status' }, { path: '/tasks/learning' }, null)).toEqual({ top: 0 })
    expect(scrollBehavior({ path: '/tasks' }, { path: '/overview' }, null)).toEqual({ top: 0 })
  })

  it('浏览器前进后退回到记录位置', () => {
    expect(
      scrollBehavior({ path: '/tasks/learning' }, { path: '/tasks' }, { left: 0, top: 640 }),
    ).toEqual({ left: 0, top: 640 })
  })
})
