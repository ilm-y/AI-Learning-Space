<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import {
  archiveMemory,
  hybridSearchMemories,
  listMemories,
  listSpaces,
  updateMemory,
} from '@/api'
import { notifyError } from '@/utils/error'
import { formatDateTime } from '@/utils/format'
import type {
  LearningSpaceResponse,
  MemoryHybridStatus,
  MemoryRequest,
  MemoryResponse,
  MemoryStatus,
  MemoryType,
} from '@/types/api'

type TagType = 'primary' | 'success' | 'warning' | 'info' | 'danger'
type Mode = 'list' | 'hybrid'

interface MemoryRow {
  memory: MemoryResponse
  rrfScore?: number
  lexicalRank?: number | null
  vectorRank?: number | null
  vectorScore?: number | null
}

interface HybridAlert {
  type: 'success' | 'warning' | 'error' | 'info'
  title: string
  description: string
}

const MEMORY_TYPE_META: Record<MemoryType, { label: string; type: TagType }> = {
  USER: { label: '用户', type: 'primary' },
  LEARNING: { label: '学习', type: 'success' },
  PREFERENCE: { label: '偏好', type: 'warning' },
}

const MEMORY_STATUS_META: Record<MemoryStatus, { label: string; type: TagType }> = {
  CANDIDATE: { label: '候选', type: 'warning' },
  ACTIVE: { label: '生效', type: 'success' },
  ARCHIVED: { label: '已归档', type: 'info' },
}

const ALL_STATUS_OPTIONS: Array<{ value: MemoryStatus; label: string }> = [
  { value: 'CANDIDATE', label: '候选' },
  { value: 'ACTIVE', label: '生效' },
  { value: 'ARCHIVED', label: '归档' },
]

const HYBRID_STATUS_LABEL: Record<MemoryHybridStatus, string> = {
  OK: '正常',
  DEGRADED_VECTOR: '向量路降级',
  DEGRADED_KEYWORD: '词法路降级',
  UNAVAILABLE: '不可用',
  BLANK_QUERY: '空查询',
}

const spaces = ref<LearningSpaceResponse[]>([])
const activeSpaceId = ref<number | undefined>(undefined)
const keyword = ref('')

const rows = ref<MemoryRow[]>([])
const mode = ref<Mode>('list')
const hybridStatus = ref<MemoryHybridStatus | null>(null)
const warnings = ref<string[]>([])
const elapsedMs = ref<number | null>(null)

const spacesLoading = ref(false)
const loading = ref(false)
const actionId = ref<number | null>(null)

const dialogVisible = ref(false)
const editingMemory = ref<MemoryResponse | null>(null)
const formRef = ref<FormInstance>()
const saving = ref(false)

/** 丢弃过期响应，避免快速切换筛选时旧结果覆盖新结果。 */
let loadSeq = 0

const form = reactive({
  memoryType: 'USER' as MemoryType,
  content: '',
  memoryKey: '',
  importance: undefined as number | undefined,
  status: 'ACTIVE' as MemoryStatus,
})

const rules: FormRules = {
  memoryType: [
    { required: true, message: '请选择记忆类型', trigger: 'change' },
  ],
  content: [
    {
      validator: (_rule, value, callback) => {
        if (typeof value === 'string' && value.trim().length > 0) {
          callback()
        } else {
          callback(new Error('记忆内容不能为空'))
        }
      },
      trigger: 'blur',
    },
  ],
}

/** 状态机只允许向后推进，编辑弹窗只展示合法目标状态。 */
const statusOptions = computed(() => {
  const current = editingMemory.value?.status
  if (!current) {
    return ALL_STATUS_OPTIONS
  }
  const allowed: MemoryStatus[] = [current]
  if (current === 'CANDIDATE') {
    allowed.push('ACTIVE', 'ARCHIVED')
  } else if (current === 'ACTIVE') {
    allowed.push('ARCHIVED')
  }
  return ALL_STATUS_OPTIONS.filter((option) => allowed.includes(option.value))
})

const hybridAlert = computed<HybridAlert | null>(() => {
  if (mode.value !== 'hybrid' || hybridStatus.value === null) {
    return null
  }
  const suffix = warnings.value.length > 0 ? ` ${warnings.value.join('；')}` : ''
  switch (hybridStatus.value) {
    case 'OK':
      return null
    case 'DEGRADED_VECTOR':
      return {
        type: 'warning',
        title: '向量检索降级',
        description: `本次结果仅来自关键词检索。${suffix}`,
      }
    case 'DEGRADED_KEYWORD':
      return {
        type: 'warning',
        title: '关键词检索降级',
        description: `本次结果仅来自向量检索。${suffix}`,
      }
    case 'UNAVAILABLE':
      return {
        type: 'error',
        title: '记忆检索暂不可用',
        description: `这不代表没有相关记忆，请稍后重试。${suffix}`,
      }
    case 'BLANK_QUERY':
      return {
        type: 'info',
        title: '请输入搜索关键词',
        description: '',
      }
    default:
      return null
  }
})

function memoryTypeMeta(type: MemoryType) {
  return MEMORY_TYPE_META[type]
}

function memoryStatusMeta(status: MemoryStatus) {
  return MEMORY_STATUS_META[status]
}

function scopeLabel(memory: MemoryResponse): string {
  switch (memory.scopeType) {
    case 'GLOBAL':
      return '全局'
    case 'SPACE':
      return `空间 #${memory.learningSpaceId ?? '—'}`
    case 'CONCEPT':
      return `概念 #${memory.conceptId ?? '—'}`
    default:
      return memory.scopeType
  }
}

function rankText(row: MemoryRow): string {
  const parts: string[] = []
  if (row.rrfScore !== undefined) {
    parts.push(`RRF ${row.rrfScore.toFixed(4)}`)
  }
  if (row.lexicalRank !== null && row.lexicalRank !== undefined) {
    parts.push(`词法 #${row.lexicalRank}`)
  }
  if (row.vectorRank !== null && row.vectorRank !== undefined) {
    parts.push(`向量 #${row.vectorRank}`)
  }
  if (row.vectorScore !== null && row.vectorScore !== undefined) {
    parts.push(`相似度 ${row.vectorScore.toFixed(4)}`)
  }
  return parts.join(' · ')
}

async function loadList() {
  const seq = ++loadSeq
  loading.value = true
  try {
    const list = await listMemories({
      spaceId: activeSpaceId.value,
    })
    if (seq !== loadSeq) {
      return
    }
    rows.value = list.map((memory) => ({ memory }))
    mode.value = 'list'
    hybridStatus.value = null
    warnings.value = []
    elapsedMs.value = null
  } catch (error) {
    if (seq === loadSeq) {
      notifyError(error, '加载记忆失败')
    }
  } finally {
    if (seq === loadSeq) {
      loading.value = false
    }
  }
}

async function runHybridSearch() {
  const searchKeyword = keyword.value.trim()
  if (!searchKeyword) {
    await loadList()
    return
  }

  const seq = ++loadSeq
  loading.value = true
  try {
    const result = await hybridSearchMemories({
      spaceId: activeSpaceId.value,
      keyword: searchKeyword,
      limit: 20,
    })
    if (seq !== loadSeq) {
      return
    }
    rows.value = result.items.map((hit) => ({
      memory: hit.memory,
      rrfScore: hit.rrfScore,
      lexicalRank: hit.lexicalRank,
      vectorRank: hit.vectorRank,
      vectorScore: hit.vectorScore,
    }))
    mode.value = 'hybrid'
    hybridStatus.value = result.status
    warnings.value = result.warnings ?? []
    elapsedMs.value = result.elapsedMs
  } catch (error) {
    if (seq === loadSeq) {
      notifyError(error, '检索失败')
    }
  } finally {
    if (seq === loadSeq) {
      loading.value = false
    }
  }
}

function refreshCurrent() {
  if (keyword.value.trim()) {
    void runHybridSearch()
  } else {
    void loadList()
  }
}

function handleSearch() {
  void refreshCurrent()
}

function handleReset() {
  keyword.value = ''
  void loadList()
}

function handleSpaceChange() {
  void refreshCurrent()
}

async function handleArchive(memory: MemoryResponse) {
  try {
    await ElMessageBox.confirm(
      '归档后该记忆会退出检索；后端将 ARCHIVED 视为终态，无法恢复。确定归档？',
      '归档记忆',
      { type: 'warning', confirmButtonText: '归档', cancelButtonText: '取消' },
    )
  } catch {
    return
  }

  actionId.value = memory.id
  try {
    await archiveMemory(memory.id)
    ElMessage.success('已归档')
    await refreshCurrent()
  } catch (error) {
    notifyError(error, '归档失败')
  } finally {
    actionId.value = null
  }
}

function openEdit(memory: MemoryResponse) {
  if (memory.status === 'ARCHIVED') {
    ElMessage.warning('已归档的记忆不可编辑')
    return
  }
  editingMemory.value = memory
  form.memoryType = memory.memoryType
  form.content = memory.content
  form.memoryKey = memory.memoryKey ?? ''
  form.importance = memory.importance ?? undefined
  form.status = memory.status
  dialogVisible.value = true
  void nextTick(() => formRef.value?.clearValidate())
}

async function submitEdit() {
  const memory = editingMemory.value
  if (!memory || !formRef.value) {
    return
  }

  try {
    await formRef.value.validate()
  } catch {
    return
  }

  saving.value = true
  try {
    // PATCH 是全量校验：memoryType / content 必填，importance 不传会被清空。
    const payload: MemoryRequest = {
      learningSpaceId: memory.learningSpaceId,
      conceptId: memory.conceptId,
      memoryType: form.memoryType,
      memoryKey: form.memoryKey.trim() ? form.memoryKey.trim() : null,
      content: form.content.trim(),
      importance: form.importance ?? null,
      status: form.status,
    }
    await updateMemory(memory.id, payload)
    ElMessage.success('已保存')
    dialogVisible.value = false
    await refreshCurrent()
  } catch (error) {
    notifyError(error, '保存失败')
  } finally {
    saving.value = false
  }
}

onMounted(async () => {
  spacesLoading.value = true
  try {
    spaces.value = await listSpaces()
  } catch (error) {
    notifyError(error, '加载学习空间失败')
  } finally {
    spacesLoading.value = false
  }
  await loadList()
})
</script>

<template>
  <div>
    <el-card shadow="never">
      <div style="display: flex; flex-wrap: wrap; gap: 8px; align-items: center">
        <el-select
          v-model="activeSpaceId"
          :loading="spacesLoading"
          clearable
          placeholder="全部学习空间"
          style="width: 200px"
          @change="handleSpaceChange"
        >
          <el-option
            v-for="space in spaces"
            :key="space.id"
            :label="space.name"
            :value="space.id"
          />
        </el-select>

        <el-input
          v-model="keyword"
          clearable
          placeholder="输入关键词，走混合检索"
          style="width: 280px"
          @keyup.enter="handleSearch"
          @clear="handleReset"
        />

        <el-button type="primary" :loading="loading" @click="handleSearch">
          搜索
        </el-button>
        <el-button @click="handleReset">重置</el-button>
        <el-button @click="refreshCurrent">刷新</el-button>

        <el-tag v-if="mode === 'hybrid'" type="success" effect="plain">
          {{ hybridStatus ? HYBRID_STATUS_LABEL[hybridStatus] : '混合检索' }}
        </el-tag>
        <el-text
          v-if="mode === 'hybrid' && elapsedMs !== null"
          size="small"
          type="info"
        >
          检索耗时 {{ elapsedMs }} ms
        </el-text>
      </div>

      <el-alert
        v-if="hybridAlert"
        :title="hybridAlert.title"
        :description="hybridAlert.description"
        :type="hybridAlert.type"
        :closable="false"
        show-icon
        style="margin-top: 12px"
      />

      <el-table
        v-loading="loading"
        :data="rows"
        :empty-text="mode === 'hybrid' ? '没有匹配的记忆' : '暂无记忆'"
        style="margin-top: 16px"
      >
        <el-table-column
          prop="memory.content"
          label="内容"
          min-width="280"
          show-overflow-tooltip
        />
        <el-table-column label="类型" width="100">
          <template #default="{ row }">
            <el-tag :type="memoryTypeMeta(row.memory.memoryType).type" size="small">
              {{ memoryTypeMeta(row.memory.memoryType).label }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag
              :type="memoryStatusMeta(row.memory.status).type"
              size="small"
            >
              {{ memoryStatusMeta(row.memory.status).label }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="作用域" width="130">
          <template #default="{ row }">
            <el-text size="small" type="info">{{ scopeLabel(row.memory) }}</el-text>
          </template>
        </el-table-column>
        <el-table-column label="重要性" width="90">
          <template #default="{ row }">{{ row.memory.importance ?? '—' }}</template>
        </el-table-column>
        <el-table-column v-if="mode === 'hybrid'" label="相关度" width="120">
          <template #default="{ row }">
            <el-tooltip :content="rankText(row)" placement="top">
              <span>
                {{ row.rrfScore !== undefined ? row.rrfScore.toFixed(4) : '—' }}
              </span>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="更新时间" width="150">
          <template #default="{ row }">
            {{ formatDateTime(row.memory.updatedAt) }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="200">
          <template #default="{ row }">
            <el-button
              link
              type="primary"
              :disabled="row.memory.status === 'ARCHIVED'"
              @click="openEdit(row.memory)"
            >
              编辑
            </el-button>

            <template v-if="row.memory.status === 'ARCHIVED'">
              <el-tooltip
                content="归档是终态，后端不支持 ARCHIVED → ACTIVE"
                placement="top"
              >
                <span style="display: inline-block; margin-left: 8px">
                  <el-button link type="primary" disabled>恢复</el-button>
                </span>
              </el-tooltip>
            </template>
            <el-button
              v-else
              link
              type="warning"
              :loading="actionId === row.memory.id"
              @click="handleArchive(row.memory)"
            >
              归档
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog
      v-model="dialogVisible"
      title="编辑记忆"
      width="560px"
      :close-on-click-modal="false"
    >
      <el-form ref="formRef" :model="form" :rules="rules" label-width="90px">
        <el-form-item label="类型" prop="memoryType">
          <el-select v-model="form.memoryType" style="width: 100%">
            <el-option label="用户" value="USER" />
            <el-option label="学习" value="LEARNING" />
            <el-option label="偏好" value="PREFERENCE" />
          </el-select>
        </el-form-item>

        <el-form-item label="内容" prop="content">
          <el-input
            v-model="form.content"
            type="textarea"
            :rows="5"
            maxlength="2000"
            show-word-limit
          />
        </el-form-item>

        <el-form-item label="记忆标识" prop="memoryKey">
          <el-input v-model="form.memoryKey" placeholder="可留空，由内容哈希兜底" />
        </el-form-item>

        <el-form-item label="重要性" prop="importance">
          <el-input-number
            v-model="form.importance"
            :min="0"
            :max="1"
            :step="0.05"
            :precision="2"
            controls-position="right"
            style="width: 180px"
          />
          <el-text size="small" type="info" style="margin-left: 8px">
            取值 0 – 1
          </el-text>
        </el-form-item>

        <el-form-item label="状态" prop="status">
          <el-select v-model="form.status" style="width: 100%">
            <el-option
              v-for="option in statusOptions"
              :key="option.value"
              :label="option.label"
              :value="option.value"
            />
          </el-select>
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submitEdit">
          保存
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>
