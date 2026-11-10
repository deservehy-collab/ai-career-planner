import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'

import ProfileForm from '../ProfileForm.vue'

beforeEach(() => {
  setActivePinia(createPinia())
})

function mountForm() {
  return mount(ProfileForm)
}

describe('ProfileForm 简洁表单', () => {
  it('标签旁不再有说明文字，字段提示收进 placeholder', () => {
    const wrapper = mountForm()

    expect(wrapper.findAll('.hint')).toHaveLength(0)
    expect(wrapper.text()).not.toContain('填一次就行')
    expect(wrapper.text()).not.toContain('已填')
    expect(wrapper.get('#pf-job').attributes('placeholder')).toContain('必填')
    expect(wrapper.get('#pf-skills').attributes('placeholder')).toContain('每行一项')
    expect(wrapper.get('#pf-projects').attributes('placeholder')).toContain('每行一项')
  })
})
