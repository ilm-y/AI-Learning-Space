<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import type { CSSProperties } from 'vue'
import { ElMessage } from 'element-plus'
import DOMPurify from 'dompurify'
import { marked } from 'marked'
import { ApiError, createConversation, listConversations, listMessages, listSpaces } from '@/api'
import { streamChatMessage } from '@/utils/sseClient'
import type {
  CitationResponse,
  ConversationResponse,
  LearningSpaceResponse,
  MessageResponse,
  MessageRole,
} from '@/types/api'

/** 视图内的消息模型：历史消息与本地流式消息共用一个结构。 */
interface ChatMessage {
  key: string
  id: number | null
  role: MessageRole
  content: string
  citations: CitationResponse[]
  streaming?: boolean
  error?: string | null
  truncated?: boolean
  createdAt: string
}

const spaces = ref<LearningSpaceResponse[]>([])
const conversations = ref<ConversationResponse[]>([])
const messages = ref<ChatMessage[]>([])

const activeSpaceId = ref<number | null>(null)
const activeConversationId = ref<number | null>(null)

const spacesLoading = ref(false)
const conversationsLoading = ref(false)
const messagesLoading = ref(false)
const creating = ref(false)
const streaming = ref(false)
const input = ref('')

const messagePane = ref<HTMLElement | null>(null)

let abortController: AbortController | null = null
let tempId = 0
/** 每次发送 / 取消都会自增，用来丢弃过期流的回调。 */
let sendSeq = 0

const activeConversation = computed(
  () => conversations.value.find((item) => item.id === activeConversationId.value) ?? null,
)
const isArchived = computed(() => activeConversation.value?.status === 'ARCHIVED')
const canSend = computed(
  () =>
    activeConversationId.value !== null &&
    !isArchived.value &&
    !streaming.value &&
    input.value.trim().length > 0,
)
const activeIndex = computed(() =>
  activeConversationId.value === null ? '' : String(activeConversationId.value),
)

function renderMarkdown(content: string): string {
  const html = marked.parse(content, { async: false, gfm: true, breaks: true })
  return DOMPurify.sanitize(html as string)
}

function citationTagType(citation: CitationResponse): 'primary' | 'info' | 'danger' {
  if (citation.available) {
    return 'info'
  }
  return citation.isHistorical ? 'primary' : 'danger'
}

function citationSuffix(citation: CitationResponse): string {
  if (citation.available) {
    return ''
  }
  return citation.isHistorical ? '（历史版本）' : '（来源已删除）'
}

function citationTooltip(citation: CitationResponse): string {
  if (citation.available) {
    return ''
  }
  return citation.isHistorical
    ? '该引用基于旧版本索引，当前内容可能已更新'
    : '引用的文档或片段已不可用'
}

function bubbleStyle(role: MessageRole): CSSProperties {
  return {
    padding: '10px 14px',
    borderRadius: '8px',
    background:
      role === 'USER' ? 'var(--el-color-primary-light-9)' : 'var(--el-fill-color-light)',
    wordBreak: 'break-word',
  }
}

function toChatMessage(message: MessageResponse): ChatMessage {
  return {
    key: `m-${message.id}`,
    id: message.id,
    role: message.role,
    content: message.content,
    citations: message.citations ?? [],
    truncated: message.truncated,
    createdAt: message.createdAt,
  }
}

function errorText(error: unknown, fallback: string): string {
  if (error instanceof ApiError && error.message) {
    return error.message
  }
  if (error instanceof Error && error.message) {
    return error.message
  }
  return fallback
}

function notifyError(error: unknown, fallback: string) {
  ElMessage.error(errorText(error, fallback))
}

function scrollToBottom() {
  void nextTick(() => {
    const pane = messagePane.value
    if (pane) {
      pane.scrollTop = pane.scrollHeight
    }
  })
}

function cancelStream() {
  sendSeq += 1
  abortController?.abort()
  abortController = null
  streaming.value = false
}

async function loadMessages() {
  cancelStream()
  messages.value = []
  if (activeConversationId.value === null) {
    return
  }

  messagesLoading.value = true
  try {
    const list = await listMessages(activeConversationId.value)
    messages.value = list.map(toChatMessage)
    scrollToBottom()
  } catch (error) {
    notifyError(error, '加载消息失败')
  } finally {
    messagesLoading.value = false
  }
}

async function loadConversations() {
  cancelStream()
  conversations.value = []
  activeConversationId.value = null
  messages.value = []
  if (activeSpaceId.value === null) {
    return
  }

  conversationsLoading.value = true
  try {
    conversations.value = await listConversations(activeSpaceId.value)
    if (conversations.value.length > 0) {
      activeConversationId.value = conversations.value[0].id
    }
  } catch (error) {
    notifyError(error, '加载对话失败')
  } finally {
    conversationsLoading.value = false
  }
}

function handleSelectConversation(index: string) {
  const id = Number(index)
  if (Number.isFinite(id) && id !== activeConversationId.value) {
    activeConversationId.value = id
  }
}

async function handleCreateConversation() {
  const spaceId = activeSpaceId.value
  if (spaceId === null) {
    return
  }

  creating.value = true
  try {
    const created = await createConversation(spaceId)
    conversations.value.unshift(created)
    activeConversationId.value = created.id
    ElMessage.success('已创建对话')
  } catch (error) {
    notifyError(error, '创建对话失败')
  } finally {
    creating.value = false
  }
}

function handleEnter(event: KeyboardEvent) {
  // 跳过中文输入法的组合态，避免选词时误发送；
  // 生成中 / 不可发送时保留默认换行行为
  if (event.isComposing || event.shiftKey || streaming.value || !canSend.value) {
    return
  }
  event.preventDefault()
  void handleSend()
}

function handleStop() {
  cancelStream()
  const last = messages.value[messages.value.length - 1]
  if (last && last.role === 'ASSISTANT' && last.streaming) {
    last.streaming = false
    if (!last.content) {
      last.error = '已停止生成'
    }
  }
}

async function handleSend() {
  const content = input.value.trim()
  const conversationId = activeConversationId.value
  if (!content || conversationId === null || isArchived.value || streaming.value) {
    return
  }

  input.value = ''
  streaming.value = true
  const seq = ++sendSeq

  const userMessage = reactive<ChatMessage>({
    key: `local-user-${++tempId}`,
    id: null,
    role: 'USER',
    content,
    citations: [],
    createdAt: new Date().toISOString(),
  })
  const assistantMessage = reactive<ChatMessage>({
    key: `local-assistant-${++tempId}`,
    id: null,
    role: 'ASSISTANT',
    content: '',
    citations: [],
    streaming: true,
    createdAt: new Date().toISOString(),
  })
  messages.value.push(userMessage, assistantMessage)
  scrollToBottom()

  const controller = new AbortController()
  abortController = controller

  try {
    await streamChatMessage(
      { conversationId, content, signal: controller.signal },
      {
        onDelta: ({ delta }) => {
          if (seq !== sendSeq) {
            return
          }
          assistantMessage.content += delta
          scrollToBottom()
        },
        onEnd: (end) => {
          if (seq !== sendSeq) {
            return
          }
          if (typeof end.messageId === 'number') {
            assistantMessage.id = end.messageId
          }
          assistantMessage.citations = end.citations
          assistantMessage.truncated = end.truncated === true
          assistantMessage.streaming = false
        },
        onServerError: (error) => {
          if (seq !== sendSeq) {
            return
          }
          assistantMessage.error = error.message || '生成失败'
          assistantMessage.streaming = false
        },
        onClose: () => {
          if (seq !== sendSeq) {
            return
          }
          assistantMessage.streaming = false
        },
      },
    )
  } catch (error) {
    if (seq !== sendSeq) {
      return
    }
    const aborted =
      (error instanceof DOMException && error.name === 'AbortError') ||
      (typeof error === 'object' &&
        error !== null &&
        (error as { name?: unknown }).name === 'AbortError')

    if (aborted) {
      assistantMessage.streaming = false
      if (!assistantMessage.content) {
        assistantMessage.error = '已停止生成'
      }
      return
    }

    const message = errorText(error, '发送失败')
    assistantMessage.error = message
    assistantMessage.streaming = false
    ElMessage.error(message)
  } finally {
    if (seq === sendSeq) {
      streaming.value = false
      abortController = null
      assistantMessage.streaming = false
      scrollToBottom()
    }
  }
}

watch(activeSpaceId, () => {
  void loadConversations()
})

watch(activeConversationId, () => {
  void loadMessages()
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
  cancelStream()
})
</script>

<template>
  <div style="display: flex; gap: 16px; height: calc(100vh - 108px)">
    <!-- 左侧：学习空间 + 会话列表 -->
    <el-card
      shadow="never"
      style="width: 300px; flex-shrink: 0"
      :body-style="{
        display: 'flex',
        flexDirection: 'column',
        height: '100%',
        boxSizing: 'border-box',
        padding: '12px',
      }"
    >
      <el-select
        v-model="activeSpaceId"
        :loading="spacesLoading"
        :disabled="spacesLoading || spaces.length === 0"
        placeholder="选择学习空间"
        style="width: 100%"
      >
        <el-option
          v-for="space in spaces"
          :key="space.id"
          :label="space.name"
          :value="space.id"
        />
      </el-select>

      <el-text v-if="!spacesLoading && spaces.length === 0" size="small" type="info">
        暂无学习空间，请先创建学习空间
      </el-text>

      <el-button
        type="primary"
        plain
        :loading="creating"
        :disabled="activeSpaceId === null"
        style="width: 100%; margin-top: 8px"
        @click="handleCreateConversation"
      >
        新建对话
      </el-button>

      <el-divider style="margin: 12px 0" />

      <div v-loading="conversationsLoading" style="flex: 1; min-height: 0; overflow: auto">
        <el-menu
          v-if="conversations.length > 0"
          :default-active="activeIndex"
          style="border-right: none"
          @select="handleSelectConversation"
        >
          <el-menu-item
            v-for="conversation in conversations"
            :key="conversation.id"
            :index="String(conversation.id)"
          >
            <div style="display: flex; align-items: center; gap: 6px; min-width: 0; width: 100%">
              <span
                style="
                  flex: 1;
                  overflow: hidden;
                  text-overflow: ellipsis;
                  white-space: nowrap;
                "
              >
                {{ conversation.title || '未命名对话' }}
              </span>
              <el-tag v-if="conversation.status === 'ARCHIVED'" size="small" type="info">
                已归档
              </el-tag>
            </div>
          </el-menu-item>
        </el-menu>

        <el-empty
          v-else-if="!conversationsLoading"
          :description="activeSpaceId === null ? '请先选择学习空间' : '暂无对话'"
          :image-size="70"
        />
      </div>
    </el-card>

    <!-- 右侧：消息流 + 输入区 -->
    <el-card
      shadow="never"
      style="flex: 1; min-width: 0"
      :body-style="{
        display: 'flex',
        flexDirection: 'column',
        height: '100%',
        boxSizing: 'border-box',
        padding: '0',
      }"
    >
      <div
        style="
          display: flex;
          align-items: center;
          gap: 8px;
          padding: 12px 16px;
          border-bottom: 1px solid var(--el-border-color-lighter);
        "
      >
        <el-text tag="b" size="large">
          {{ activeConversation?.title || '新对话' }}
        </el-text>
        <el-tag v-if="isArchived" type="info" size="small">已归档</el-tag>
        <el-text v-if="streaming" type="info" size="small" style="margin-left: auto">
          正在生成…
        </el-text>
      </div>

      <div
        ref="messagePane"
        v-loading="messagesLoading"
        style="
          flex: 1;
          min-height: 0;
          overflow: auto;
          padding: 16px;
          background: var(--el-fill-color-blank);
        "
      >
        <el-empty
          v-if="activeConversationId === null"
          description="请选择或创建一个对话"
          :image-size="80"
        />
        <el-empty
          v-else-if="!messagesLoading && messages.length === 0"
          description="开始你的第一个问题吧"
          :image-size="80"
        />

        <div
          v-for="message in messages"
          :key="message.key"
          :style="{
            display: 'flex',
            marginBottom: '16px',
            justifyContent: message.role === 'USER' ? 'flex-end' : 'flex-start',
          }"
        >
          <div style="max-width: 80%; min-width: 0">
            <div :style="bubbleStyle(message.role)">
              <template v-if="message.role === 'ASSISTANT'">
                <div v-if="message.content" v-html="renderMarkdown(message.content)" />
                <el-text v-else-if="message.streaming" type="info">正在思考…</el-text>
                <el-alert
                  v-if="message.error"
                  :title="message.error"
                  type="error"
                  :closable="false"
                  show-icon
                  style="margin-top: 8px"
                />
                <div v-if="message.truncated" style="margin-top: 8px">
                  <el-tag type="warning" size="small">回答因长度限制被截断</el-tag>
                </div>
              </template>
              <div v-else style="white-space: pre-wrap">{{ message.content }}</div>
            </div>

            <div
              v-if="message.role === 'ASSISTANT' && message.citations.length > 0"
              style="margin-top: 8px"
            >
              <el-text size="small" type="info">引用来源</el-text>
              <div style="margin-top: 6px; display: flex; flex-wrap: wrap; gap: 6px">
                <el-tooltip
                  v-for="(citation, index) in message.citations"
                  :key="`${message.key}-cite-${index}`"
                  :content="citationTooltip(citation)"
                  :disabled="citation.available"
                  placement="top"
                >
                  <el-tag
                    size="small"
                    effect="plain"
                    :type="citationTagType(citation)"
                  >
                    {{ citation.sourceTitle || '未命名文档' }}
                    <template v-if="citation.chunkIndex !== null">
                      · 片段 {{ citation.chunkIndex + 1 }}
                    </template>
                    {{ citationSuffix(citation) }}
                  </el-tag>
                </el-tooltip>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div
        style="
          padding: 12px 16px;
          border-top: 1px solid var(--el-border-color-lighter);
        "
      >
        <el-alert
          v-if="isArchived"
          title="该对话已归档，无法发送新消息"
          type="warning"
          :closable="false"
          show-icon
          style="margin-bottom: 8px"
        />

        <div style="display: flex; gap: 8px; align-items: flex-end">
          <el-input
            v-model="input"
            type="textarea"
            :rows="2"
            resize="none"
            :disabled="activeConversationId === null || isArchived"
            placeholder="输入问题，Enter 发送，Shift + Enter 换行"
            @keydown.enter="handleEnter"
          />
          <el-button v-if="streaming" type="danger" plain @click="handleStop">
            停止
          </el-button>
          <el-button v-else type="primary" :disabled="!canSend" @click="handleSend">
            发送
          </el-button>
        </div>

        <el-text size="small" type="info" style="display: block; margin-top: 6px">
          回复由 AI 生成，引用来源请在消息下方查看。
        </el-text>
      </div>
    </el-card>
  </div>
</template>
