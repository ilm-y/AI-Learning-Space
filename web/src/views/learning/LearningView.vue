<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  getAttentionItems,
  getCurrentSession,
  getLearningStateOverview,
  getUnderstanding,
} from '@/api'
import { notifyError } from '@/utils/error'
import { formatDateTime } from '@/utils/format'
import type {
  AttentionItem,
  AttentionReason,
  LearningStateResponse,
  SessionResponse,
  SessionStatus,
  UnderstandingResponse,
  UnderstandingStatus,
} from '@/types/api'

type TagType = 'primary' | 'success' | 'warning' | 'info' | 'danger'

const SESSION_STATUS_META: Record<SessionStatus, { label: string; type: TagType }> = {
  ACTIVE: { label: '进行中', type: 'success' },
  PAUSED: { label: '已暂停', type: 'warning' },
  COMPLETED: { label: '已完成', type: 'info' },
  ABANDONED: { label: '已放弃', type: 'info' },
}

const UNDERSTANDING_STATUS_META: Record<
  UnderstandingStatus,
  { label: string; type: TagType }
> = {
  UNKNOWN: { label: '未接触', type: 'info' },
  EXPOSED: { label: '已接触', type: 'primary' },
  PARTIAL: { label: '部分理解', type: 'warning' },
  DEMONSTRATED: { label: '已掌握', type: 'success' },
  MISCONCEPTION: { label: '存在误区', type: 'danger' },
}

const ATTENTION_REASON_META: Record<
  AttentionReason,
  { label: string; type: TagType }
> = {
  MISCONCEPTION: { label: '误区', type: 'danger' },
  MISCONCEPTION_RESOLVING: { label: '误区待解除', type: 'warning' },
  LONG_UNVERIFIED: { label: '久未验证', type: 'info' },
  NEEDS_REVIEW: { label: '待复习', type: 'warning' },
}

const session = ref<SessionResponse | null>(null)
const sessionLoading = ref(false)

const overview = ref<LearningStateResponse[]>([])
const overviewLoading = ref(false)

const attention = ref<AttentionItem[]>([])
const attentionLoading = ref(false)

const detailVisible = ref(false)
const selectedConceptId = ref<number | null>(null)
const selectedConceptName = ref<string | null>(null)
const understanding = ref<UnderstandingResponse | null>(null)
const understandingLoading = ref(false)
const understandingLoaded = ref(false)
const understandingFailed = ref(false)

const statusSummary = computed(() => {
  const counts = new Map<UnderstandingStatus, number>()
  for (const item of overview.value) {
    counts.set(item.status, (counts.get(item.status) ?? 0) + 1)
  }
  return (Object.keys(UNDERSTANDING_STATUS_META) as UnderstandingStatus[])
    .map((status) => ({
      status,
      label: UNDERSTANDING_STATUS_META[status].label,
      type: UNDERSTANDING_STATUS_META[status].type,
      count: counts.get(status) ?? 0,
    }))
    .filter((item) => item.count > 0)
})

const detailTitle = computed(() => {
  const name =
    selectedConceptName.value ??
    (selectedConceptId.value !== null ? `知识点 #${selectedConceptId.value}` : '知识点')
  return `理解详情 · ${name}`
})

function conceptLabel(item: { conceptId: number; conceptName: string | null }): string {
  return item.conceptName || `概念 #${item.conceptId}`
}

function sessionStatusMeta(status: SessionStatus) {
  return SESSION_STATUS_META[status]
}

function understandingStatusMeta(status: UnderstandingStatus) {
  return UNDERSTANDING_STATUS_META[status]
}

function attentionReasonMeta(reason: AttentionReason) {
  return ATTENTION_REASON_META[reason]
}

async function loadSession() {
  sessionLoading.value = true
  try {
    session.value = await getCurrentSession()
  } catch (error) {
    notifyError(error, '加载当前学习目标失败')
  } finally {
    sessionLoading.value = false
  }
}

async function loadOverview() {
  overviewLoading.value = true
  try {
    overview.value = (await getLearningStateOverview()) ?? []
  } catch (error) {
    notifyError(error, '加载学习状态失败')
  } finally {
    overviewLoading.value = false
  }
}

async function loadAttention() {
  attentionLoading.value = true
  try {
    attention.value = (await getAttentionItems(10)) ?? []
  } catch (error) {
    notifyError(error, '加载推荐关注失败')
  } finally {
    attentionLoading.value = false
  }
}

async function openUnderstanding(
  item: { conceptId: number; conceptName: string | null },
) {
  selectedConceptId.value = item.conceptId
  selectedConceptName.value = item.conceptName
  understanding.value = null
  understandingLoaded.value = false
  understandingFailed.value = false
  detailVisible.value = true

  understandingLoading.value = true
  try {
    // 知识点存在但尚未建立理解时后端返回 data: null，按空状态处理。
    understanding.value = await getUnderstanding(item.conceptId)
  } catch (error) {
    understandingFailed.value = true
    notifyError(error, '加载理解详情失败')
  } finally {
    understandingLoading.value = false
    understandingLoaded.value = true
  }
}

onMounted(() => {
  void loadSession()
  void loadOverview()
  void loadAttention()
})
</script>

<template>
  <div style="display: flex; flex-direction: column; gap: 16px">
    <!-- 当前学习目标 -->
    <el-card shadow="never">
      <template #header>
        <div style="display: flex; align-items: center; gap: 8px">
          <span style="font-weight: 600">当前学习目标</span>
          <el-button
            link
            style="margin-left: auto"
            :loading="sessionLoading"
            @click="loadSession"
          >
            刷新
          </el-button>
        </div>
      </template>

      <div v-loading="sessionLoading">
        <el-empty
          v-if="!sessionLoading && !session"
          description="还没有进行中的学习目标：开始一段学习后会自动出现"
          :image-size="70"
        />

        <el-descriptions v-else-if="session" :column="2" border>
          <el-descriptions-item label="标题">
            {{ session.title || '未命名学习目标' }}
          </el-descriptions-item>
          <el-descriptions-item label="状态">
            <el-tag :type="sessionStatusMeta(session.status).type" size="small">
              {{ sessionStatusMeta(session.status).label }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="学习目标" :span="2">
            {{ session.goal || '—' }}
          </el-descriptions-item>
          <el-descriptions-item label="当前卡点" :span="2">
            {{ session.currentFocus || '—' }}
          </el-descriptions-item>
          <el-descriptions-item label="开始时间">
            {{ formatDateTime(session.startedAt) }}
          </el-descriptions-item>
          <el-descriptions-item label="最近活跃">
            {{ formatDateTime(session.lastActiveAt) }}
          </el-descriptions-item>
        </el-descriptions>
      </div>
    </el-card>

    <!-- Understanding 状态 -->
    <el-card shadow="never">
      <template #header>
        <div style="display: flex; align-items: center; gap: 8px">
          <span style="font-weight: 600">Understanding 状态</span>
          <el-text size="small" type="info">
            共 {{ overview.length }} 个知识点
          </el-text>
          <el-button
            link
            style="margin-left: auto"
            :loading="overviewLoading"
            @click="loadOverview"
          >
            刷新
          </el-button>
        </div>
      </template>

      <div v-loading="overviewLoading">
        <el-empty
          v-if="!overviewLoading && overview.length === 0"
          description="暂无学习状态：产生学习证据后才会出现"
          :image-size="70"
        />

        <template v-else>
          <div style="display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px">
            <el-tag
              v-for="item in statusSummary"
              :key="item.status"
              :type="item.type"
              effect="plain"
            >
              {{ item.label }} {{ item.count }}
            </el-tag>
          </div>

          <el-table :data="overview" size="small">
            <el-table-column label="知识点" min-width="160">
              <template #default="{ row }">{{ conceptLabel(row) }}</template>
            </el-table-column>
            <el-table-column label="状态" width="120">
              <template #default="{ row }">
                <el-tag
                  :type="understandingStatusMeta(row.status).type"
                  size="small"
                >
                  {{ understandingStatusMeta(row.status).label }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="最近证据" width="150">
              <template #default="{ row }">
                {{ formatDateTime(row.lastEvidenceAt) }}
              </template>
            </el-table-column>
            <el-table-column label="下次复习" width="150">
              <template #default="{ row }">
                {{ formatDateTime(row.nextReviewAt) }}
              </template>
            </el-table-column>
            <el-table-column label="复习优先级" width="110">
              <template #default="{ row }">{{ row.reviewPriority ?? '—' }}</template>
            </el-table-column>
            <el-table-column label="操作" width="110">
              <template #default="{ row }">
                <el-button link type="primary" @click="openUnderstanding(row)">
                  查看理解
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </template>
      </div>
    </el-card>

    <!-- 推荐关注 -->
    <el-card shadow="never">
      <template #header>
        <div style="display: flex; align-items: center; gap: 8px">
          <span style="font-weight: 600">推荐关注</span>
          <el-text size="small" type="info">
            误区 &gt; 久未验证 &gt; 到期待复习
          </el-text>
          <el-button
            link
            style="margin-left: auto"
            :loading="attentionLoading"
            @click="loadAttention"
          >
            刷新
          </el-button>
        </div>
      </template>

      <div v-loading="attentionLoading">
        <el-empty
          v-if="!attentionLoading && attention.length === 0"
          description="暂无需要特别关注的知识点"
          :image-size="70"
        />

        <el-table v-else :data="attention" size="small">
          <el-table-column label="知识点" min-width="160">
            <template #default="{ row }">{{ conceptLabel(row) }}</template>
          </el-table-column>
          <el-table-column label="原因" width="140">
            <template #default="{ row }">
              <el-tag :type="attentionReasonMeta(row.reason).type" size="small">
                {{ attentionReasonMeta(row.reason).label }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="Understanding" width="130">
            <template #default="{ row }">
              <el-tag
                :type="understandingStatusMeta(row.status).type"
                size="small"
                effect="plain"
              >
                {{ understandingStatusMeta(row.status).label }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="最近证据" width="150">
            <template #default="{ row }">
              {{ formatDateTime(row.lastEvidenceAt) }}
            </template>
          </el-table-column>
          <el-table-column label="下次复习" width="150">
            <template #default="{ row }">
              {{ formatDateTime(row.nextReviewAt) }}
            </template>
          </el-table-column>
          <el-table-column label="操作" width="110">
            <template #default="{ row }">
              <el-button link type="primary" @click="openUnderstanding(row)">
                查看理解
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </el-card>

    <!-- 单个知识点的理解详情 -->
    <el-dialog v-model="detailVisible" :title="detailTitle" width="640px">
      <div v-loading="understandingLoading">
        <el-empty
          v-if="understandingLoaded && understandingFailed"
          description="加载理解详情失败，请稍后重试"
          :image-size="70"
        />
        <el-empty
          v-else-if="understandingLoaded && !understanding"
          description="该知识点尚未建立理解：产生学习证据后才能形成理解快照"
          :image-size="70"
        />

        <template v-else-if="understanding">
          <el-descriptions :column="2" border>
            <el-descriptions-item label="状态">
              <el-tag :type="understandingStatusMeta(understanding.status).type">
                {{ understandingStatusMeta(understanding.status).label }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="更新时间">
              {{ formatDateTime(understanding.updatedAt) }}
            </el-descriptions-item>
            <el-descriptions-item label="摘要" :span="2">
              {{ understanding.summary || '—' }}
            </el-descriptions-item>
            <el-descriptions-item label="最近一次解释" :span="2">
              {{ understanding.latestUserExplanation || '—' }}
            </el-descriptions-item>
          </el-descriptions>

          <div style="margin-top: 16px">
            <el-text tag="b">已理解</el-text>
            <div style="margin-top: 8px">
              <el-text v-if="understanding.knownParts.length === 0" type="info">
                暂无
              </el-text>
              <el-tag
                v-for="part in understanding.knownParts"
                :key="part"
                type="success"
                effect="plain"
                style="margin: 0 6px 6px 0"
              >
                {{ part }}
              </el-tag>
            </div>
          </div>

          <div style="margin-top: 16px">
            <el-text tag="b">仍不确定</el-text>
            <div style="margin-top: 8px">
              <el-text v-if="understanding.uncertainParts.length === 0" type="info">
                暂无
              </el-text>
              <el-tag
                v-for="part in understanding.uncertainParts"
                :key="part"
                type="warning"
                effect="plain"
                style="margin: 0 6px 6px 0"
              >
                {{ part }}
              </el-tag>
            </div>
          </div>

          <div style="margin-top: 16px">
            <el-text tag="b">当前误区</el-text>
            <div style="margin-top: 8px">
              <el-text v-if="understanding.misconceptions.length === 0" type="info">
                暂无
              </el-text>
              <el-alert
                v-for="misconception in understanding.misconceptions"
                :key="misconception.text"
                :title="misconception.text"
                :description="`状态：${
                  misconception.status === 'ACTIVE' ? '仍成立' : '待确认'
                } · 最近证据：${formatDateTime(misconception.lastEvidenceAt)}`"
                type="error"
                :closable="false"
                show-icon
                style="margin-bottom: 8px"
              />
            </div>
          </div>
        </template>
      </div>
    </el-dialog>
  </div>
</template>
