<template>
  <div class="register-container">
    <n-card class="register-card" :bordered="true">
      <template #header>
        <div class="register-header">
          <h1 class="register-title">注册账号</h1>
          <p class="register-subtitle">创建你的 My Workbench 账号</p>
        </div>
      </template>

      <n-form ref="formRef" :model="form" :rules="rules" label-placement="top">
        <n-form-item label="用户名" path="username">
          <n-input
            v-model:value="form.username"
            placeholder="2-20位，字母/数字/下划线/中文"
            size="large"
            :disabled="loading"
            maxlength="20"
          />
        </n-form-item>

        <n-form-item label="密码" path="password">
          <n-input
            v-model:value="form.password"
            type="password"
            placeholder="至少6位密码"
            size="large"
            :disabled="loading"
            show-password-on="click"
          />
        </n-form-item>

        <n-form-item label="确认密码" path="confirmPassword">
          <n-input
            v-model:value="form.confirmPassword"
            type="password"
            placeholder="再次输入密码"
            size="large"
            :disabled="loading"
            show-password-on="click"
            @keyup.enter="handleRegister"
          />
        </n-form-item>

        <n-form-item>
          <n-button
            type="primary"
            block
            size="large"
            :loading="loading"
            @click="handleRegister"
          >
            注 册
          </n-button>
        </n-form-item>
      </n-form>

      <div class="register-footer">
        <n-button text type="primary" @click="router.push('/login')">
          已有账号？去登录
        </n-button>
      </div>

      <!-- 注册成功提示 -->
      <n-result
        v-if="registered"
        status="success"
        title="注册成功"
        description="您的账号已提交，请等待管理员审批。"
      >
        <template #footer>
          <n-button type="primary" @click="router.push('/login')">
            返回登录
          </n-button>
        </template>
      </n-result>
    </n-card>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive } from 'vue'
import { useRouter } from 'vue-router'
import {
  NCard, NForm, NFormItem, NInput, NButton, NResult, useMessage,
} from 'naive-ui'
import type { FormInst, FormRules } from 'naive-ui'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const message = useMessage()
const authStore = useAuthStore()

const formRef = ref<FormInst>()
const loading = ref(false)
const registered = ref(false)

const form = reactive({
  username: '',
  password: '',
  confirmPassword: '',
})

const rules: FormRules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    {
      validator: (_rule, value: string) => {
        if (!value) return true
        return /^[\w一-鿿]{2,20}$/.test(value)
      },
      message: '用户名需 2-20 位，仅支持字母、数字、下划线、中文',
      trigger: 'blur',
    },
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, message: '密码至少 6 位', trigger: 'blur' },
  ],
  confirmPassword: [
    { required: true, message: '请再次输入密码', trigger: 'blur' },
    {
      validator: (_rule, value: string) => value === form.password,
      message: '两次输入的密码不一致',
      trigger: 'blur',
    },
  ],
}

async function handleRegister() {
  try {
    await formRef.value?.validate()
  } catch {
    return
  }

  loading.value = true
  try {
    await authStore.register(form.username, form.password)
    registered.value = true
  } catch (err: any) {
    message.error(err.message || '注册失败')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.register-container {
  display: flex;
  justify-content: center;
  align-items: center;
  min-height: 100vh;
  padding: 24px;
}

.register-card {
  width: 100%;
  max-width: 400px;
}

.register-header {
  text-align: center;
}

.register-title {
  margin: 0;
  font-size: 28px;
  font-weight: 700;
}

.register-subtitle {
  margin: 8px 0 0;
  opacity: 0.6;
  font-size: 14px;
}

.register-footer {
  text-align: center;
  margin-top: 8px;
}
</style>
