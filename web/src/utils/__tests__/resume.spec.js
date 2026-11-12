/**
 * 简历数据层：只从已保存的档案 / 任务 / 成长记录里带入事实，
 * 不补造姓名、电话、学校、公司等用户没填过的内容。
 */

import { describe, expect, it } from 'vitest'

import {
  buildAutofillResume,
  emptyResume,
  normalizeResume,
  resumeStats,
} from '../resume'

function planDetail(overrides = {}) {
  return {
    id: 'plan-a',
    target_job: 'Java 后端开发',
    profile_snapshot: {
      major: '软件工程',
      grade: '大三',
      skills: ['Redis', 'Git'],
      projects: [],
    },
    current_skills: [
      { skill: 'Java', level: '熟练' },
      { skill: 'Spring Boot', level: '熟练' },
      { skill: 'MySQL', level: '掌握' },
      { skill: 'Redis', level: '了解' },
    ],
    ...overrides,
  }
}

function tasksPayload(overrides = {}) {
  return {
    tasks: [
      {
        task_id: 'p1',
        kind: 'project',
        title: '校园二手交易平台',
        skills: ['Spring Boot', 'MySQL'],
        status: 'completed',
        completed_at: '2026-10-01T00:00:00Z',
        evidence: '负责订单模块，压测 QPS 600。',
      },
      {
        task_id: 'l1',
        kind: 'learning',
        title: 'Java 集合',
        skills: ['Java'],
        status: 'completed',
        completed_at: '2026-09-01T00:00:00Z',
        evidence: '',
      },
    ],
    ...overrides,
  }
}

function growthPayload(overrides = {}) {
  return {
    history: [
      {
        at: '2026-10-05T00:00:00Z',
        events: [
          {
            kind: 'project',
            title: '校园二手交易平台',
            skills: ['Spring Boot'],
            evidence: '上线后日均 120 单。',
            at: '2026-10-05T00:00:00Z',
          },
          {
            kind: 'project',
            title: '个人博客系统',
            skills: ['Vue'],
            evidence: '',
            at: '2026-09-20T00:00:00Z',
          },
        ],
      },
    ],
    ...overrides,
  }
}

describe('buildAutofillResume', () => {
  it('只带入档案里真实存在的目标岗位、专业、年级与技能', () => {
    const resume = buildAutofillResume({
      plan: planDetail(),
      tasksData: tasksPayload(),
      growthData: growthPayload(),
    })

    expect(resume.intent.targetJob).toBe('Java 后端开发')
    expect(resume.education[0].major).toBe('软件工程')
    expect(resume.education[0].grade).toBe('大三')
    expect(resume.basic.name).toBe('')
    expect(resume.basic.phone).toBe('')
    expect(resume.basic.email).toBe('')
    expect(resume.education[0].school).toBe('')
  })

  it('技能按已有等级分组，自述技能补进其他组，不重复', () => {
    const resume = buildAutofillResume({ plan: planDetail() })
    const labels = resume.skills.map((group) => group.label)
    const all = resume.skills.flatMap((group) => group.items)

    expect(labels).toContain('熟练')
    expect(labels).toContain('掌握')
    expect(labels).toContain('了解')
    expect(all.filter((item) => item === 'Redis')).toHaveLength(1)
    expect(all).toContain('Git')
  })

  it('项目经历优先用成长记录，重复的任务不重复成两条', () => {
    const resume = buildAutofillResume({
      plan: planDetail(),
      tasksData: tasksPayload(),
      growthData: growthPayload(),
    })
    const titles = resume.projects.map((item) => item.title)

    expect(titles[0]).toBe('校园二手交易平台')
    expect(titles).toContain('个人博客系统')
    expect(titles.filter((title) => title === '校园二手交易平台')).toHaveLength(1)
    expect(titles).not.toContain('Java 集合')
  })

  it('没有成长记录时，才从已完成的项目任务里补一条，且保留证据', () => {
    const resume = buildAutofillResume({
      plan: planDetail(),
      tasksData: tasksPayload(),
      growthData: null,
    })

    expect(resume.projects).toHaveLength(1)
    expect(resume.projects[0].title).toBe('校园二手交易平台')
    expect(resume.projects[0].bullets.join('')).toContain('QPS 600')
  })

  it('自我评价只陈述已有事实，不编造年限或获奖', () => {
    const resume = buildAutofillResume({
      plan: planDetail(),
      tasksData: tasksPayload(),
      growthData: growthPayload(),
    })

    expect(resume.summary).toContain('Java 后端开发')
    expect(resume.summary).toContain('Java')
    expect(resume.summary).not.toMatch(/年经验|获奖|精通/)
  })

  it('没有档案时返回可直接编辑的空结构，教育经历留一组空输入', () => {
    const resume = buildAutofillResume({})

    expect(resume.intent.targetJob).toBe('')
    expect(resume.education).toHaveLength(1)
    expect(resume.projects).toEqual([])
    expect(resume.skills).toEqual([])
    expect(resume.summary).toBe('')
  })
})

describe('resumeStats', () => {
  it('统计已填写叶子字段与六项必填缺口', () => {
    const resume = emptyResume()
    expect(resumeStats(resume).pending).toBe(6)

    resume.basic.name = '王小明'
    resume.basic.phone = '13800000000'
    resume.intent.targetJob = 'Java 后端开发'
    resume.education[0].school = '示例大学'
    resume.education[0].major = '软件工程'
    resume.summary = '求职目标：Java 后端开发。'

    const stats = resumeStats(resume)
    expect(stats.pending).toBe(1)
    expect(stats.filled).toBe(6)
  })
})

describe('normalizeResume', () => {
  it('去首尾空白、丢弃全空条目与空技能组', () => {
    const resume = emptyResume()
    resume.basic.name = '  王小明  '
    resume.intent.targetJob = '  后端开发  '
    resume.education[0].school = '  示例大学  '
    resume.education.push({ school: '', major: '', degree: '', time: '', gpa: '', grade: '' })
    resume.skills.push({ label: '   ', items: ['  ', ''] })
    resume.projects.push({ title: '  项目 A  ', role: '', time: '', bullets: ['  第一条  ', '   '] })

    const normalized = normalizeResume(resume)

    expect(normalized.basic.name).toBe('王小明')
    expect(normalized.intent.targetJob).toBe('后端开发')
    expect(normalized.education).toHaveLength(1)
    expect(normalized.skills).toEqual([])
    expect(normalized.projects).toHaveLength(1)
    expect(normalized.projects[0].bullets).toEqual(['第一条'])
  })
})
