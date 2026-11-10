<script setup>
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import AuthField from '../components/AuthField.vue'
import AuthShell from '../components/AuthShell.vue'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const router = useRouter()

const form = reactive({ username: '', password: '', confirm: '' })
const error = ref('')

function validate() {
  const username = form.username.trim()
  if (username.length < 2 || username.length > 32) return '用户名需要 2~32 个字符'
  if (!/^[A-Za-z0-9_\u4e00-\u9fa5]+$/.test(username)) return '用户名只能包含中英文、数字与下划线'
  if (form.password.length < 6) return '密码至少 6 位'
  if (form.password !== form.confirm) return '两次输入的密码不一致'
  return ''
}

async function onSubmit() {
  error.value = validate()
  if (error.value) return
  try {
    await auth.register(form.username.trim(), form.password)
    router.push('/')
  } catch (e) {
    error.value = e?.message || '注册失败，请稍后再试'
  }
}
</script>

<template>
  <AuthShell title="创建账号">
    <form @submit.prevent="onSubmit">
      <AuthField
        id="reg-username"
        v-model="form.username"
        label="用户名"
        type="text"
        autocomplete="username"
        placeholder="2~32 个字符，可用中英文、数字与下划线"
      />
      <AuthField
        id="reg-password"
        v-model="form.password"
        label="密码"
        type="password"
        autocomplete="new-password"
        placeholder="至少 6 位"
      />
      <AuthField
        id="reg-confirm"
        v-model="form.confirm"
        label="确认密码"
        type="password"
        autocomplete="new-password"
        placeholder="再输一遍"
      />

      <p v-if="error" class="auth-alert" role="alert">
        <svg
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="1.8"
          stroke-linecap="round"
          stroke-linejoin="round"
          aria-hidden="true"
        >
          <path d="M12 8v5M12 17h.01M12 3l9 17H3z" />
        </svg>
        <span>{{ error }}</span>
      </p>

      <button class="btn btn-primary auth-submit" type="submit" :disabled="auth.loading">
        {{ auth.loading ? '注册中…' : '注册并登录' }}
      </button>
    </form>

    <template #foot>
      已有账号？
      <router-link class="auth-switch" to="/login">去登录</router-link>
    </template>
  </AuthShell>
</template>
