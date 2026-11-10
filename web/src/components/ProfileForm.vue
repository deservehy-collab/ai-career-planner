<script setup>
import { computed, ref, watch } from 'vue'

import { useProfileStore } from '../stores/profile'
import { ALL_MAJORS, GRADE_OPTIONS, MAJOR_GROUPS } from '../utils/enums'
import { formatList, parseList } from '../utils/storage'

/**
 * 画像表单：直接编辑 profile store；store 内部已把改动写进 localStorage，
 * 所以这里不需要额外的"保存"按钮 —— 刷新页面内容还在。
 *
 * 「清空自述画像」承担的是**防串味**职责：换求职方向时点一下，专业/年级/技能/
 * 项目/目标岗位连同粘贴的招聘要求原文一起归零。分析产出的能力表**不在**此列。
 *
 * 专业 / 年级用下拉：常用值点选即可；专业不在名单里时切「其他（手动输入）」
 * 自由填写 —— 下拉只是输入辅助，不是白名单，任何专业都要能填。
 *
 * 技能 / 项目用多行文本编辑（用户写起来最自然），
 * 与 store 里的字符串数组之间来回转换：文本是输入时的真相，
 * 只有 store 被**外部**改动（如点"清空"）时才回灌文本。
 */
const profile = useProfileStore()

const skillsText = ref(formatList(profile.skills))
const projectsText = ref(formatList(profile.projects))

/** 「其他（手动输入）」选项的哨兵值；不会与真实专业名撞车。 */
const CUSTOM_MAJOR = '__custom__'

const majorSelection = computed({
  get() {
    if (!profile.major) return ''
    return ALL_MAJORS.includes(profile.major) ? profile.major : CUSTOM_MAJOR
  },
  set(value) {
    // 切到手动输入时清空，让用户从头填；选名单里的值则直接落 store
    profile.major = value === CUSTOM_MAJOR ? '' : value
  },
})

function sameList(a, b) {
  const left = a || []
  const right = b || []
  return left.length === right.length && left.every((item, i) => item === right[i])
}

watch(
  () => profile.skills,
  (value) => {
    // 解析后的结果和 store 一致就不动输入框，避免把用户正在敲的逗号/换行冲掉
    if (!sameList(parseList(skillsText.value), value)) skillsText.value = formatList(value)
  },
  { deep: true },
)

watch(
  () => profile.projects,
  (value) => {
    if (!sameList(parseList(projectsText.value), value)) projectsText.value = formatList(value)
  },
  { deep: true },
)

function onSkillsInput(event) {
  skillsText.value = event.target.value
  profile.skills = parseList(event.target.value)
}

function onProjectsInput(event) {
  projectsText.value = event.target.value
  profile.projects = parseList(event.target.value)
}

function handleClear() {
  if (
    !window.confirm(
      '清空画像、目标岗位与粘贴的招聘要求？\n\n' +
        '换求职方向时点这里，上一次填的专业和技能不会偷偷带进下一次分析。\n' +
        '已经分析得到的能力表**不会**被删除。',
    )
  )
    return
  profile.clearProfile()
  skillsText.value = ''
  projectsText.value = ''
}
</script>

<template>
  <section class="panel-block">
    <div class="panel-head">
      <h2>我的画像</h2>
    </div>

    <div v-if="profile.storageFailed" class="notice notice-warn">
      这台设备暂时存不下内容（可能是隐私模式或空间已满），画像这次没能保存，刷新后会丢失。
    </div>

    <div class="grid grid-2">
      <div>
        <div class="field">
          <label for="pf-major">专业</label>
          <select id="pf-major" v-model="majorSelection">
            <option value="">未填写</option>
            <optgroup v-for="group in MAJOR_GROUPS" :key="group.label" :label="group.label">
              <option v-for="item in group.options" :key="item" :value="item">{{ item }}</option>
            </optgroup>
            <option :value="CUSTOM_MAJOR">其他（手动输入）</option>
          </select>
          <input
            v-if="majorSelection === CUSTOM_MAJOR"
            v-model="profile.major"
            type="text"
            placeholder="请输入专业名称，如：护理学"
            style="margin-top: 8px"
          />
        </div>

        <div class="field">
          <label for="pf-grade">年级</label>
          <select id="pf-grade" v-model="profile.grade">
            <option value="">未填写</option>
            <option v-for="item in GRADE_OPTIONS" :key="item" :value="item">{{ item }}</option>
          </select>
        </div>

        <div class="field">
          <label for="pf-job">目标岗位</label>
          <input
            id="pf-job"
            v-model="profile.targetJob"
            type="text"
            placeholder="必填，如：AI 应用开发工程师 / 注册会计师 / 临床护士"
          />
        </div>
      </div>

      <div>
        <div class="field">
          <label for="pf-skills">自述技能</label>
          <textarea
            id="pf-skills"
            :value="skillsText"
            rows="5"
            placeholder="每行一项或用逗号分隔，如：Python、财务报表分析"
            @input="onSkillsInput"
          />
        </div>

        <div class="field">
          <label for="pf-projects">项目经历</label>
          <textarea
            id="pf-projects"
            :value="projectsText"
            rows="3"
            placeholder="每行一项，如：校园二手交易平台、社区医院实习"
            @input="onProjectsInput"
          />
        </div>
      </div>
    </div>

    <div v-if="profile.profileWarnings.length" class="notice notice-info">
      画像还不完整（{{ profile.profileWarnings.join('、') }}）—— 仍可发起分析，
      但岗位匹配结果会偏保守。
    </div>

    <div class="btn-row form-actions">
      <button class="btn" @click="handleClear">清空画像与求职目标</button>
    </div>
  </section>
</template>
