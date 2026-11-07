<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { login } from '@/api'
import { TOKEN_STORAGE_KEY } from '@/api/http'
import { notifyError } from '@/utils/error'

const router = useRouter()
const route = useRoute()

const formRef = ref<FormInstance>()
const loading = ref(false)

const form = reactive({
  username: '',
  password: '',
})

const rules: FormRules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

/** 只接受站内绝对路径，避免 redirect 参数被拿来做开放重定向。 */
function resolveRedirect(): string {
  const value = route.query.redirect
  const target = Array.isArray(value) ? value[0] : value
  if (typeof target === 'string' && target.startsWith('/') && !target.startsWith('//')) {
    return target
  }
  return '/'
}

async function handleSubmit() {
  if (!formRef.value) {
    return
  }

  try {
    await formRef.value.validate()
  } catch {
    return
  }

  loading.value = true
  try {
    const result = await login({
      username: form.username.trim(),
      password: form.password,
    })
    localStorage.setItem(TOKEN_STORAGE_KEY, result.token)
    ElMessage.success(`欢迎回来，${result.user.username}`)
    await router.replace(resolveRedirect())
  } catch (error) {
    notifyError(error, '登录失败')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div
    style="
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
      box-sizing: border-box;
      background: var(--el-fill-color-light);
    "
  >
    <el-card shadow="never" style="width: 100%; max-width: 400px">
      <div style="text-align: center; margin-bottom: 20px">
        <div style="font-size: 22px; font-weight: 700; color: var(--el-text-color-primary)">
          AI Learning Space
        </div>
        <el-text type="info" size="small">登录后进入你的学习空间</el-text>
      </div>

      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-position="top"
        @submit.prevent="handleSubmit"
      >
        <el-form-item label="用户名" prop="username">
          <el-input
            v-model="form.username"
            placeholder="请输入用户名"
            autocomplete="username"
          />
        </el-form-item>

        <el-form-item label="密码" prop="password">
          <el-input
            v-model="form.password"
            type="password"
            show-password
            placeholder="请输入密码"
            autocomplete="current-password"
          />
        </el-form-item>

        <el-button
          type="primary"
          native-type="submit"
          :loading="loading"
          style="width: 100%"
        >
          登录
        </el-button>
      </el-form>

      <el-text
        size="small"
        type="info"
        style="display: block; margin-top: 16px; text-align: center"
      >
        首次使用请先调用 POST /api/auth/register 注册账号
      </el-text>
    </el-card>
  </div>
</template>
