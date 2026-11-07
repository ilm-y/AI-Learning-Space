<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { UploadRawFile, UploadRequestOptions } from 'element-plus'
import {
  deleteDocument,
  listDocuments,
  listSpaces,
  reindexDocument,
  uploadDocument,
} from '@/api'
import { notifyError } from '@/utils/error'
import { formatBytes, formatDateTime } from '@/utils/format'
import type {
  DocumentResponse,
  DocumentStatus,
  DocumentUploadResponse,
  LearningSpaceResponse,
} from '@/types/api'

type TagType = 'primary' | 'success' | 'warning' | 'info' | 'danger'

const STATUS_META: Record<DocumentStatus, { type: TagType; label: string }> = {
  UPLOADING: { type: 'info', label: '上传中' },
  PROCESSING: { type: 'warning', label: '索引中' },
  READY: { type: 'success', label: '就绪' },
  FAILED: { type: 'danger', label: '失败' },
  DELETING: { type: 'info', label: '删除中' },
  DELETED: { type: 'info', label: '已删除' },
}

/** 这些状态下索引/删除仍在后台推进，需要轮询刷新。 */
const PENDING_STATUSES: DocumentStatus[] = ['UPLOADING', 'PROCESSING', 'DELETING']

const spaces = ref<LearningSpaceResponse[]>([])
const activeSpaceId = ref<number | null>(null)
const documents = ref<DocumentResponse[]>([])

const spacesLoading = ref(false)
const loading = ref(false)
const uploading = ref(false)
const deletingId = ref<number | null>(null)
const reindexingId = ref<number | null>(null)

let pollTimer: number | null = null
let loadSeq = 0

function statusMeta(status: DocumentStatus) {
  return STATUS_META[status]
}

/** FAILED 展示索引失败原因，DELETING 展示清理失败原因。 */
function statusTooltip(row: DocumentResponse): string {
  if (row.status === 'FAILED' && row.indexError) {
    return row.indexError
  }
  if (row.status === 'DELETING' && row.deleteError) {
    return row.deleteError
  }
  return ''
}

function stopPolling() {
  if (pollTimer !== null) {
    window.clearInterval(pollTimer)
    pollTimer = null
  }
}

function syncPolling() {
  const hasPending = documents.value.some((doc) =>
    PENDING_STATUSES.includes(doc.status),
  )
  if (hasPending && pollTimer === null && activeSpaceId.value !== null) {
    pollTimer = window.setInterval(() => {
      void loadDocuments(true)
    }, 3000)
  } else if (!hasPending) {
    stopPolling()
  }
}

async function loadDocuments(silent = false) {
  const spaceId = activeSpaceId.value
  if (spaceId === null) {
    documents.value = []
    stopPolling()
    return
  }

  const seq = ++loadSeq
  if (!silent) {
    loading.value = true
  }
  try {
    const list = await listDocuments(spaceId)
    if (seq !== loadSeq || activeSpaceId.value !== spaceId) {
      return
    }
    documents.value = list
    syncPolling()
  } catch (error) {
    if (!silent) {
      notifyError(error, '加载文档失败')
    }
  } finally {
    if (!silent) {
      loading.value = false
    }
  }
}

function handleRefresh() {
  void loadDocuments()
}

function beforeUpload(file: UploadRawFile): boolean {
  if (activeSpaceId.value === null) {
    ElMessage.warning('请先选择学习空间')
    return false
  }
  const extension = file.name.split('.').pop()?.toLowerCase()
  if (extension !== 'md' && extension !== 'txt') {
    ElMessage.error('仅支持 .md / .txt 文件')
    return false
  }
  if (file.size > 10 * 1024 * 1024) {
    ElMessage.error('单个文件不能超过 10 MB')
    return false
  }
  return true
}

async function handleUpload(
  options: UploadRequestOptions,
): Promise<DocumentUploadResponse | undefined> {
  const spaceId = activeSpaceId.value
  if (spaceId === null) {
    ElMessage.warning('请先选择学习空间')
    return undefined
  }

  const form = new FormData()
  form.append('file', options.file)

  uploading.value = true
  try {
    const result = await uploadDocument(spaceId, form)
    if (result.duplicate) {
      ElMessage.warning(`文件已存在：${result.document.name}`)
    } else {
      ElMessage.success('上传成功，正在后台建立索引')
    }
    await loadDocuments()
    return result
  } catch (error) {
    notifyError(error, '上传失败')
    return undefined
  } finally {
    uploading.value = false
  }
}

async function handleReindex(row: DocumentResponse) {
  reindexingId.value = row.id
  try {
    await reindexDocument(row.id)
    ElMessage.success('已提交重建')
    await loadDocuments(true)
  } catch (error) {
    notifyError(error, '重建失败')
  } finally {
    reindexingId.value = null
  }
}

async function handleDelete(row: DocumentResponse) {
  try {
    await ElMessageBox.confirm(
      `确定删除「${row.name}」？删除后文档会立即退出检索白名单。`,
      '删除文档',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }

  deletingId.value = row.id
  try {
    await deleteDocument(row.id)
    ElMessage.success('已提交删除')
    await loadDocuments(true)
  } catch (error) {
    notifyError(error, '删除失败')
  } finally {
    deletingId.value = null
  }
}

watch(activeSpaceId, () => {
  stopPolling()
  documents.value = []
  void loadDocuments()
})

onMounted(async () => {
  spacesLoading.value = true
  try {
    spaces.value = await listSpaces()
    if (spaces.value.length > 0) {
      activeSpaceId.value = spaces.value[0].id
    }
  } catch (error) {
    notifyError(error, '加载学习空间失败')
  } finally {
    spacesLoading.value = false
  }
})

onBeforeUnmount(() => {
  stopPolling()
})
</script>

<template>
  <div>
    <el-card shadow="never">
      <div style="display: flex; flex-wrap: wrap; gap: 8px; align-items: center">
        <el-select
          v-model="activeSpaceId"
          :loading="spacesLoading"
          :disabled="spacesLoading || spaces.length === 0"
          placeholder="选择学习空间"
          style="width: 220px"
        >
          <el-option
            v-for="space in spaces"
            :key="space.id"
            :label="space.name"
            :value="space.id"
          />
        </el-select>

        <el-upload
          :show-file-list="false"
          :http-request="handleUpload"
          :before-upload="beforeUpload"
          accept=".md,.txt"
          :disabled="activeSpaceId === null || uploading"
        >
          <el-button
            type="primary"
            :loading="uploading"
            :disabled="activeSpaceId === null"
          >
            {{ uploading ? '上传中…' : '上传文档' }}
          </el-button>
        </el-upload>

        <el-text size="small" type="info">
          仅支持 .md / .txt，单文件 ≤ 10 MB；上传后索引异步执行
        </el-text>

        <el-button style="margin-left: auto" @click="handleRefresh">刷新</el-button>
      </div>

      <el-alert
        v-if="!spacesLoading && spaces.length === 0"
        title="暂无学习空间"
        description="文档必须归属某个学习空间，请先创建学习空间。"
        type="warning"
        :closable="false"
        show-icon
        style="margin-top: 12px"
      />

      <el-table
        v-loading="loading"
        :data="documents"
        empty-text="暂无文档"
        style="margin-top: 16px"
      >
        <el-table-column prop="name" label="名称" min-width="180" show-overflow-tooltip />
        <el-table-column prop="fileName" label="文件名" min-width="180" show-overflow-tooltip />
        <el-table-column prop="fileType" label="类型" width="80" />
        <el-table-column label="大小" width="100">
          <template #default="{ row }">{{ formatBytes(row.size) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tooltip
              v-if="statusTooltip(row)"
              :content="statusTooltip(row)"
              placement="top"
            >
              <el-tag :type="statusMeta(row.status).type">
                {{ statusMeta(row.status).label }}
              </el-tag>
            </el-tooltip>
            <el-tag v-else :type="statusMeta(row.status).type">
              {{ statusMeta(row.status).label }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="分块" width="80">
          <template #default="{ row }">{{ row.chunkCount ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="创建时间" width="150">
          <template #default="{ row }">{{ formatDateTime(row.createdAt) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="150">
          <template #default="{ row }">
            <el-button
              v-if="row.status === 'FAILED'"
              link
              type="primary"
              :loading="reindexingId === row.id"
              @click="handleReindex(row)"
            >
              重建
            </el-button>
            <el-button
              link
              type="danger"
              :loading="deletingId === row.id"
              :disabled="row.status === 'DELETING'"
              @click="handleDelete(row)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>
