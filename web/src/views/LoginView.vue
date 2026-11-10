<script setup>
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import AuthField from '../components/AuthField.vue'
import AuthShell from '../components/AuthShell.vue'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

const form = reactive({ username: '', password: '' })
const error = ref('')

// 本地演示账号：填进输入框但不自动提交，登录仍然由用户点按钮决定。
// 这台机器的库里要先有这个账号（注册页走一遍即可），它只是本机演示用。
const DEMO_ACCOUNT = { username: 'demo', password: 'demo1234' }

function fillDemo() {
  form.username = DEMO_ACCOUNT.username
  form.password = DEMO_ACCOUNT.password
  error.value = ''
}

async function onSubmit() {
  error.value = ''
  if (!form.username || !form.password) {
    error.value = '请填写用户名和密码'
    return
  }
  try {
    await auth.login(form.username, form.password)
    const redirect = route.query.redirect
    router.push(typeof redirect === 'string' && redirect.startsWith('/') ? redirect : '/')
  } catch (e) {
    error.value = e?.message || '登录失败，请稍后再试'
  }
}
</script>

<template>
  <AuthShell title="登录账号">
    <form @submit.prevent="onSubmit">
      <AuthField
        id="login-username"
        v-model="form.username"
        label="用户名"
        type="text"
        autocomplete="username"
        placeholder="中英文、数字或下划线"
      />
      <AuthField
        id="login-password"
        v-model="form.password"
        label="密码"
        type="password"
        autocomplete="current-password"
        placeholder="登录密码"
      />

      <div class="auth-demo">
        <div class="auth-demo-text">
          <strong>体验账号</strong>
          <code>{{ DEMO_ACCOUNT.username }} / {{ DEMO_ACCOUNT.password }}</code>
        </div>
        <button class="btn btn-sm" type="button" :disabled="auth.loading" @click="fillDemo">
          一键填入
        </button>
      </div>

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
        {{ auth.loading ? '登录中…' : '登录' }}
      </button>
    </form>

    <template #foot>
      还没有账号？
      <router-link class="auth-switch" to="/register">注册一个</router-link>
    </template>
  </AuthShell>
</template>
