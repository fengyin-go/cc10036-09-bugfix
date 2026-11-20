<template>
  <section class="page" data-testid="export-detail">
    <header class="page-head">
      <div>
        <h2>即席导出详情</h2>
        <p class="page-desc">
          产物 → 签收 → 交付。文件写完不等于完成，签收后才释放队列。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="detail-back" type="button" @click="goBack">返回列表</button>
        <button class="btn" data-testid="detail-run" type="button" :disabled="busy" @click="runExport">
          开始导出
        </button>
        <button
          class="btn primary"
          data-testid="detail-sign-off"
          type="button"
          :disabled="busy || !signableReceipt"
          @click="signOffCurrent"
        >
          完成导出（签收）
        </button>
        <button class="btn" data-testid="detail-recover" type="button" :disabled="busy" @click="recover">
          中断恢复对账
        </button>
      </div>
    </header>

    <p v-if="notice" class="notice-text" data-testid="detail-notice">{{ notice }}</p>
    <p v-if="errorMessage" class="error-text" data-testid="detail-error">{{ errorMessage }}</p>

    <div class="stat-row" v-if="detail">
      <article class="stat-card">
        <span class="stat-label">任务状态</span>
        <strong class="stat-value">{{ detail.task?.status ?? '—' }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">本期签收次数</span>
        <strong class="stat-value" data-testid="detail-sign-count">{{ totalSignOffs }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">待处理项</span>
        <strong class="stat-value" data-testid="detail-pending-count">{{ detail.pending_count ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">队列 / 待签收</span>
        <strong class="stat-value">
          {{ detail.queue?.queue_size ?? 0 }} / {{ detail.queue?.awaiting_count ?? 0 }}
        </strong>
      </article>
    </div>

    <h3 class="section-title">产物</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>凭证</th><th>周期</th><th>状态</th><th>签收次数</th>
          <th>文件</th><th>大小</th><th>文件完整性</th><th>交付状态</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="item in detail?.artifacts ?? []"
          :key="item.receipt_id"
          :data-testid="`artifact-${item.receipt_id}`"
          :data-receipt-state="item.state"
        >
          <td>#{{ item.receipt_id }}</td>
          <td>{{ item.report_period }}</td>
          <td>{{ item.state }}</td>
          <td>{{ item.sign_off_count }}</td>
          <td class="path-cell">{{ item.artifact_path || '—' }}</td>
          <td>{{ item.file_size_bytes }} B</td>
          <td>{{ item.artifact_intact ? '完整' : item.artifact_check || '—' }}</td>
          <td>{{ item.delivery_status }}</td>
          <td class="row-actions">
            <button
              class="link"
              type="button"
              :data-testid="`artifact-sign-${item.receipt_id}`"
              :disabled="item.state !== '文件已写完'"
              @click="signOffArtifact(item.receipt_id)"
            >
              签收
            </button>
            <button
              class="link"
              type="button"
              :data-testid="`artifact-invalidate-${item.receipt_id}`"
              @click="invalidate(item.receipt_id)"
            >
              作废产物
            </button>
          </td>
        </tr>
        <tr v-if="detail && !detail.artifacts?.length">
          <td colspan="9" class="empty-state">还没有产物，先执行一次导出</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">待处理项</h3>
    <table class="data-table">
      <thead>
        <tr><th>类型</th><th>说明</th><th>凭证</th><th>状态</th></tr>
      </thead>
      <tbody>
        <tr
          v-for="(item, index) in detail?.pending_items ?? []"
          :key="`${item.kind}-${index}`"
          :data-testid="`pending-${index}`"
        >
          <td>{{ item.kind }}</td>
          <td>{{ item.reason }}</td>
          <td>{{ item.receipt_id ? `#${item.receipt_id}` : '—' }}</td>
          <td>{{ item.state ?? '待处理' }}</td>
        </tr>
        <tr v-if="detail && !detail.pending_items?.length">
          <td colspan="4" class="empty-state">没有待处理项</td>
        </tr>
      </tbody>
    </table>

    <section v-if="recoverResult" class="recover-panel" data-testid="recover-result">
      <h3 class="section-title">恢复对账结果</h3>
      <p>
        周期 {{ recoverResult.period }}：补签收 {{ recoverResult.signed_off }} 项，
        需补跑 {{ recoverResult.needs_rerun }} 项，残留队列 {{ recoverResult.queue_size }} 项。
      </p>
      <table class="data-table">
        <thead>
          <tr><th>任务</th><th>凭证</th><th>发现</th><th>处理</th><th>依据</th></tr>
        </thead>
        <tbody>
          <tr v-for="(item, index) in recoverResult.dispositions" :key="index">
            <td>{{ item.task_name }}</td>
            <td>{{ item.receipt_id ? `#${item.receipt_id}` : '—' }}</td>
            <td>{{ item.state }}</td>
            <td>{{ item.disposition }}</td>
            <td>{{ item.detail || '—' }}</td>
          </tr>
        </tbody>
      </table>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import {
  invalidateArtifact,
  loadExportDetail,
  recoverExports,
  runExport as runExportApi,
  signOff,
} from '@/api/workflow-service'
import type { ExportDetail, RecoverResult } from '@/api/workflow-service'

const props = defineProps<{ taskId: string }>()
const taskId = computed(() => Number(props.taskId))
const router = useRouter()
const detail = ref<ExportDetail | null>(null)
const recoverResult = ref<RecoverResult | null>(null)
const notice = ref('')
const errorMessage = ref('')
const busy = ref(false)

const totalSignOffs = computed(() =>
  (detail.value?.artifacts ?? []).reduce((sum, item) => sum + item.sign_off_count, 0),
)
const signableReceipt = computed(() =>
  (detail.value?.artifacts ?? []).find((item) => item.state === '文件已写完'),
)

function goBack() {
  void router.push('/adhoc_export')
}

async function refresh() {
  errorMessage.value = ''
  try {
    detail.value = await loadExportDetail(taskId.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '详情读取失败'
  }
}

async function runExport() {
  await withBusy(async () => {
    const result = await runExportApi(taskId.value)
    notice.value = result.message
  })
}

function signOffCurrent() {
  void signOffArtifact(signableReceipt.value?.receipt_id)
}

async function signOffArtifact(receiptId?: number) {
  await withBusy(async () => {
    const result = await signOff(taskId.value, receiptId)
    notice.value = result.message
  })
}

async function invalidate(receiptId: number) {
  await withBusy(async () => {
    const result = await invalidateArtifact(taskId.value, receiptId, '演练：产物校验失败')
    notice.value = result.message
  })
}

async function recover() {
  await withBusy(async () => {
    recoverResult.value = await recoverExports()
    notice.value = '已按签收账本完成对账'
  })
}

async function withBusy(task: () => Promise<void>) {
  busy.value = true
  notice.value = ''
  errorMessage.value = ''
  try {
    await task()
    await refresh()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  } finally {
    busy.value = false
  }
}

onMounted(refresh)
</script>

<style scoped>
.section-title { margin: 18px 0 8px; font-size: 14px; }
.notice-text { color: #15803d; font-size: 13px; }
.path-cell { word-break: break-all; font-size: 12px; color: #475569; }
.recover-panel { margin-top: 12px; }
</style>
