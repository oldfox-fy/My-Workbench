<template>
  <div class="login-container">
    <n-card class="login-card" :bordered="true">
      <template #header>
        <div class="login-header">
          <h1 class="login-title">My Workbench</h1>
          <p class="login-subtitle">私人订制的 AI 工作台</p>
        </div>
      </template>

      <n-form ref="formRef" :model="form" :rules="rules" label-placement="top">
        <n-form-item label="用户名" path="username">
          <n-input
            v-model:value="form.username"
            placeholder="请输入用户名"
            size="large"
            :disabled="loading"
            @keyup.enter="handleLogin"
          />
        </n-form-item>

        <n-form-item label="密码" path="password">
          <n-input
            v-model:value="form.password"
            type="password"
            placeholder="请输入密码"
            size="large"
            :disabled="loading"
            show-password-on="click"
            @keyup.enter="handleLogin"
          />
        </n-form-item>

        <n-form-item>
          <n-checkbox v-model:checked="form.rememberMe" :disabled="loading">
            记住我（7天内免登录）
          </n-checkbox>
        </n-form-item>

        <n-form-item>
          <n-button
            type="primary"
            block
            size="large"
            :loading="loading"
            @click="handleLogin"
          >
            登 录
          </n-button>
        </n-form-item>
      </n-form>

      <div class="login-footer">
        <n-button text type="primary" @click="router.push('/register')">
          没有账号？去注册
        </n-button>
      </div>

      <!-- 等待审批提示 -->
      <n-alert
        v-if="showPendingNotice"
        type="warning"
        title="账号待审批"
        class="pending-notice"
      >
        您的账号正在等待管理员审批，审批通过后即可登录使用。
      </n-alert>
    </n-card>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { NCard, NForm, NFormItem, NInput, NButton, NCheckbox, NAlert, useMessage } from 'naive-ui'
import type { FormInst, FormRules } from 'naive-ui'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const route = useRoute()
const message = useMessage()
const authStore = useAuthStore()

const formRef = ref<FormInst>()
const loading = ref(false)
const showPendingNotice = ref(route.query.pending === '1')

const form = reactive({
  username: '',
  password: '',
  rememberMe: false,
})

const rules: FormRules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, message: '密码至少 6 位', trigger: 'blur' },
  ],
}

async function handleLogin() {
  try {
    await formRef.value?.validate()
  } catch {
    return
  }

  loading.value = true
  try {
    await authStore.login(form.username, form.password, form.rememberMe)
    message.success('登录成功')
    router.push('/chat')
  } catch (err: any) {
    message.error(err.message || '登录失败')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-container {
  display: flex;
  justify-content: center;
  align-items: center;
  min-height: 100vh;
  padding: 24px;
}

.login-card {
  width: 100%;
  max-width: 400px;
}

.login-header {
  text-align: center;
}

.login-title {
  margin: 0;
  font-size: 28px;
  font-weight: 700;
}

.login-subtitle {
  margin: 8px 0 0;
  opacity: 0.6;
  font-size: 14px;
}

.login-footer {
  text-align: center;
  margin-top: 8px;
}

.pending-notice {
  margin-top: 16px;
}
</style>
