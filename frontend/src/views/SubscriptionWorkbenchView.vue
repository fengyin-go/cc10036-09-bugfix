<template>
  <section class="page" data-testid="subscription-workbench">
    <header class="page-head">
      <div>
        <h2>报表订阅工作台</h2>
        <p class="page-desc">
          读取即席导出的签收数据：只有已签收的产物才交付，同一份文件同一周期只消费一次。
        </p>
      </div>
      <div class="page-actions">
        <label class="period-field">
          <span>周期</span>
          <input v-model="period" data-testid="period" placeholder="如 2026-11" />
        </label>
        <button class="btn" data-testid="load-board" type="button" :disabled="busy" @click="selectSubscription(selectedId)">
          刷新签收数据
        </button>
        <button class="btn" data-testid="month-close" type="button" :disabled="busy" @click="triggerMonthClose">
          触发月结
        </button>
      </div>
    </header>

    <p v-if="notice" class="notice-text" data-testid="workbench-notice">{{ notice }}</p>
    <p v-if="errorMessage" class="error-text" data-testid="workbench-error">{{ errorMessage }}</p>

    <div class="filter-bar">
      <label class="filter-item">
        <span>订阅配置</span>
        <select v-model.number="selectedId" data-testid="subscription-select" @change="selectSubscription(selectedId)">
          <option v-for="item in subscriptions" :key="item.id" :value="item.id">
            {{ item.订阅名称 }}（{{ item.status }}）
          </option>
        </select>
      </label>
      <button
        class="btn primary"
        data-testid="deliver"
        type="button"
        :disabled="busy || !selectedId"
        @click="deliverOnce"
      >
        交付本期报表
      </button>
    </div>

    <div class="stat-row" v-if="board">
      <article class="stat-card">
        <span class="stat-label">报表对象</span>
        <strong class="stat-value small">{{ board.subscription?.报表对象 ?? '—' }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">本期签收次数</span>
        <strong class="stat-value" data-testid="board-sign-count">{{ board.sign_off_total }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">交付记录</span>
        <strong class="stat-value" data-testid="board-delivery-count">{{ board.delivery_count }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">实际消费</span>
        <strong class="stat-value" data-testid="board-consumption">{{ board.consumption_total }}</strong>
      </article>
    </div>

    <h3 class="section-title">签收数据（工作台读取）</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>凭证</th><th>周期</th><th>状态</th><th>签收次数</th>
          <th>文件</th><th>交付状态</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="item in board?.receipts ?? []"
          :key="item.receipt_id"
          :data-testid="`board-receipt-${item.receipt_id}`"
          :data-signed="item.signed_off_at ? 'true' : 'false'"
        >
          <td>#{{ item.receipt_id }}</td>
          <td>{{ item.report_period }}</td>
          <td>{{ item.state }}</td>
          <td>{{ item.sign_off_count }}</td>
          <td class="path-cell">{{ item.artifact_path || '—' }}</td>
          <td>{{ item.delivery_status }}</td>
        </tr>
        <tr v-if="board && !board.receipts.length">
          <td colspan="6" class="empty-state">本期还没有签收数据</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">交付记录</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>周期</th><th>凭证</th><th>触发</th><th>读取文件</th>
          <th>字节</th><th>签收次数</th><th>状态</th><th>说明</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="item in board?.deliveries ?? []"
          :key="`${item.period}-${item.receipt_id}-${item.delivered_at}`"
          :data-testid="`delivery-${item.receipt_id}`"
        >
          <td>{{ item.period }}</td>
          <td>#{{ item.receipt_id }}</td>
          <td>{{ item.trigger }}</td>
          <td class="path-cell">{{ item.artifact_path }}</td>
          <td>{{ item.read_bytes }}</td>
          <td>{{ item.sign_off_count }}</td>
          <td>{{ item.status }}</td>
          <td>{{ item.invalid_reason || '—' }}</td>
        </tr>
        <tr v-if="board && !board.deliveries.length">
          <td colspan="8" class="empty-state">本期还没有交付</td>
        </tr>
      </tbody>
    </table>

    <section v-if="monthCloseResult" data-testid="month-close-result" class="month-close-panel">
      <h3 class="section-title">月结结果（周期 {{ monthCloseResult.period }}）</h3>
      <p>
        触发 {{ monthCloseResult.triggered }} 个订阅，新交付 {{ monthCloseResult.delivered }}，
        跳过 {{ monthCloseResult.skipped }}。
      </p>
      <table class="data-table">
        <thead>
          <tr><th>订阅</th><th>报表</th><th>结果</th><th>本次读取文件</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in monthCloseResult.results" :key="item.subscription_id">
            <td>{{ item.订阅名称 }}</td>
            <td>{{ item.报表对象 }}</td>
            <td>{{ item.message }}</td>
            <td class="path-cell">{{ item.artifact_path || (item.read_files ?? []).join('；') || '未读取' }}</td>
          </tr>
        </tbody>
      </table>
    </section>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import {
  closeMonth,
  deliverReport,
  listSubscriptions,
  loadDeliveryBoard,
} from '@/api/workflow-service'
import type {
  DeliveryBoard,
  MonthCloseResult,
  SubscriptionRow,
} from '@/api/workflow-service'

const route = useRoute()
const subscriptions = ref<SubscriptionRow[]>([])
const selectedId = ref<number>(0)
const period = ref('2026-11')
const board = ref<DeliveryBoard | null>(null)
const monthCloseResult = ref<MonthCloseResult | null>(null)
const notice = ref('')
const errorMessage = ref('')
const busy = ref(false)

async function loadSubscriptions() {
  try {
    subscriptions.value = await listSubscriptions()
    if (!selectedId.value && subscriptions.value.length) {
      selectedId.value = subscriptions.value[0].id
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '订阅列表读取失败'
  }
}

async function selectSubscription(id: number) {
  if (!id) return
  await withBusy(async () => {
    board.value = await loadDeliveryBoard(id, period.value || undefined)
  })
}

async function deliverOnce() {
  if (!selectedId.value) return
  await withBusy(async () => {
    const result = await deliverReport(selectedId.value, period.value || undefined)
    notice.value = result.message
    board.value = await loadDeliveryBoard(selectedId.value, period.value || undefined)
  })
}

async function triggerMonthClose() {
  await withBusy(async () => {
    monthCloseResult.value = await closeMonth(period.value || undefined)
    notice.value = `月结完成：新交付 ${monthCloseResult.value.delivered} 份`
    if (selectedId.value) {
      board.value = await loadDeliveryBoard(selectedId.value, period.value || undefined)
    }
  })
}

async function withBusy(task: () => Promise<void>) {
  busy.value = true
  notice.value = ''
  errorMessage.value = ''
  try {
    await task()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  } finally {
    busy.value = false
  }
}

onMounted(async () => {
  await loadSubscriptions()
  const fromQuery = Number(route.query.subscription ?? 0)
  if (fromQuery && subscriptions.value.some((item) => item.id === fromQuery)) {
    selectedId.value = fromQuery
  }
  if (selectedId.value) await selectSubscription(selectedId.value)
})
</script>

<style scoped>
.section-title { margin: 18px 0 8px; font-size: 14px; }
.notice-text { color: #15803d; font-size: 13px; }
.stat-value.small { font-size: 14px; }
.path-cell { word-break: break-all; font-size: 12px; color: #475569; }
.period-field { display: flex; flex-direction: column; font-size: 12px; color: var(--muted); }
.month-close-panel { margin-top: 12px; }
</style>
