<template>
  <n-config-provider :locale="zhCN" :date-locale="dateZhCN" :theme="naiveTheme" :theme-overrides="themeOverrides">
    <n-message-provider>
      <n-dialog-provider>
        <!-- 应用初始化 loading -->
        <div v-if="!appReady" class="app-loading">
          <n-spin size="large" />
        </div>
        <router-view v-else />
      </n-dialog-provider>
    </n-message-provider>
  </n-config-provider>
</template>

<script setup lang="ts">
import { ref, watch, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { NConfigProvider, NMessageProvider, NDialogProvider, NSpin, zhCN, dateZhCN } from 'naive-ui'
import { useTheme } from '@/composables/useTheme'
import { useConfigStore } from '@/stores/config'
import { useAuthStore } from '@/stores/auth'
import { loadUserPrefs } from '@/api/userPrefs'
import { useChatStore } from '@/stores/chat'

const { naiveTheme, themeOverrides } = useTheme()
const configStore = useConfigStore()
const authStore = useAuthStore()
const chatStore = useChatStore()
const router = useRouter()

const appReady = ref(false)

// 同步 HTML 属性，使全局 CSS 变量生效
watch(() => configStore.themeMode, (mode) => {
  document.documentElement.setAttribute('theme-mode', mode)
  document.body.setAttribute('style', mode === 'dark' ? 'background:#0b0e14' : 'background:#f8f7fc')
}, { immediate: true })

// 应用初始化
onMounted(async () => {
  try {
    // 等待后端就绪
    await fetch('/api/wait-ready')
  } catch {
    // 后端可能未就绪，继续
  }

  // 确保 router 就绪
  await router.isReady()

  // 初始化认证状态
  if (authStore.token) {
    try {
      await authStore.init()
      // 登录后加载用户偏好（替代 localStorage）
      if (authStore.isLoggedIn) {
        const prefs = await loadUserPrefs().catch(() => null)
        if (prefs) {
          configStore.applyUserPrefs(prefs)
          chatStore.applyUserPrefs(prefs)
        }
      }
    } catch {
      // 初始化失败，token 无效
    }
  } else {
    authStore.initialized = true
  }

  appReady.value = true
})
</script>

<style scoped>
.app-loading {
  display: flex;
  justify-content: center;
  align-items: center;
  min-height: 100vh;
}
</style>
