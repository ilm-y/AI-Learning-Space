<script setup lang="ts">
import { useRoute } from 'vue-router'

const route = useRoute()
</script>

<template>
  <el-card shadow="never" class="placeholder">
    <h2>{{ route.meta.title ?? '功能开发中' }}</h2>
    <p>{{ route.meta.description ?? '该页面尚未实现。' }}</p>
    <el-tag type="warning" effect="plain">开发中</el-tag>
  </el-card>
</template>

<style scoped>
.placeholder {
  max-width: 560px;
}

.placeholder h2 {
  margin: 0 0 10px;
  font-size: 20px;
  color: #111827;
}

.placeholder p {
  margin: 0 0 16px;
  color: #6b7280;
}
</style>
