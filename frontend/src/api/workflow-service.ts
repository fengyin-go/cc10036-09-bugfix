/** 导出签收与订阅交付：详情页/工作台读取的跨模块链路数据。 */
import { request } from './client'

export type ReceiptHistory = { at?: string; event: string; [key: string]: unknown }

export type ExportReceipt = {
  receipt_id: number
  task_id: number
  task_name: string
  report_object: string
  report_period: string
  export_format: string
  row_limit: number
  row_count: number
  compressed: boolean
  file_size_bytes: number
  checksum: string
  artifact_path: string
  content_preview: string
  written_at: string | null
  signed_off_at: string | null
  sign_off_count: number
  state: string
  delivery_status: string
  delivered_at: string | null
  delivery_cycle: string
  consumption_count: number
  needs_rerun: boolean
  rerun_reason: string
  history: ReceiptHistory[]
  artifact_intact?: boolean
  artifact_check?: string
}

export type PendingItem = {
  kind: string
  reason: string
  receipt_id?: number
  state?: string
  info?: Record<string, unknown>
}

export type ExportDetail = {
  ok: boolean
  message?: string
  task?: Record<string, unknown>
  report_object?: string
  artifacts?: ExportReceipt[]
  pending_items?: PendingItem[]
  pending_count?: number
  queue?: {
    queue_size: number
    running_count: number
    awaiting_count: number
    pending_count: number
  }
}

export type ActionResult = { ok: boolean; message: string; task?: Record<string, unknown> }

export type DeliveryRecord = {
  period: string
  receipt_id: number
  task_id: number
  checksum: string
  read_bytes: number
  artifact_path: string
  sign_off_count: number
  delivered_at: string
  trigger: string
  status: string
  invalid_reason?: string
}

export type DeliveryBoard = {
  ok: boolean
  message?: string
  period: string
  subscription: {
    id: number
    订阅名称?: string
    name?: string
    报表对象?: string
    status?: string
    推送周期?: string
  }
  receipts: ExportReceipt[]
  sign_off_total: number
  sign_off_max: number
  deliveries: DeliveryRecord[]
  delivery_count: number
  consumption_total: number
}

export type DeliverResult = {
  ok: boolean
  message: string
  consumed: boolean
  reason?: string
  period?: string
  receipt_id?: number
  artifact_path?: string
  read_bytes?: number
  sign_off_count?: number
  delivery?: DeliveryRecord
  read_files?: string[]
}

export type MonthCloseResult = {
  ok: boolean
  period: string
  triggered: number
  delivered: number
  skipped: number
  results: (DeliverResult & { subscription_id: number; 订阅名称?: string; 报表对象?: string })[]
}

export type RecoverResult = {
  ok: boolean
  period: string
  signed_off: number
  needs_rerun: number
  queue_size: number
  running_count: number
  dispositions: {
    task_id: number
    task_name: string
    receipt_id: number | null
    period?: string
    state: string
    disposition: string
    detail: string
  }[]
}

async function readJson<T>(response: Response): Promise<T> {
  return (await response.json()) as T
}

export async function loadExportDetail(taskId: number): Promise<ExportDetail> {
  const response = await request(`/api/workflow/adhoc-export/${taskId}/detail`)
  if (!response.ok) throw new Error('导出详情读取失败')
  return readJson<ExportDetail>(response)
}

export async function runExport(taskId: number): Promise<ActionResult> {
  const response = await request('/api/workflow/adhoc-export/run', {
    method: 'POST',
    body: JSON.stringify({ task_id: taskId }),
  })
  if (!response.ok) throw new Error('导出执行失败')
  return readJson<ActionResult>(response)
}

export async function signOff(taskId: number, receiptId?: number): Promise<ActionResult> {
  const query = receiptId ? `?receipt_id=${receiptId}` : ''
  const response = await request(`/api/workflow/adhoc-export/${taskId}/sign-off${query}`, {
    method: 'POST',
  })
  if (!response.ok) throw new Error('签收失败')
  return readJson<ActionResult>(response)
}

export async function invalidateArtifact(
  taskId: number,
  receiptId: number,
  reason: string,
): Promise<ActionResult> {
  const response = await request(
    `/api/workflow/adhoc-export/${taskId}/invalidate/${receiptId}`,
    { method: 'POST', body: JSON.stringify({ reason }) },
  )
  if (!response.ok) throw new Error('作废产物失败')
  return readJson<ActionResult>(response)
}

export async function recoverExports(): Promise<RecoverResult> {
  const response = await request('/api/workflow/adhoc-export/recover', { method: 'POST' })
  if (!response.ok) throw new Error('恢复对账失败')
  return readJson<RecoverResult>(response)
}

export async function loadDeliveryBoard(
  subscriptionId: number,
  period?: string,
): Promise<DeliveryBoard> {
  const query = period ? `?period=${encodeURIComponent(period)}` : ''
  const response = await request(`/api/workflow/report-subscription/${subscriptionId}/board${query}`)
  if (!response.ok) throw new Error('订阅工作台读取失败')
  return readJson<DeliveryBoard>(response)
}

export async function deliverReport(
  subscriptionId: number,
  period?: string,
): Promise<DeliverResult> {
  const response = await request('/api/workflow/report-subscription/deliver', {
    method: 'POST',
    body: JSON.stringify({ subscription_id: subscriptionId, period }),
  })
  if (!response.ok) throw new Error('报表交付失败')
  return readJson<DeliverResult>(response)
}

export async function closeMonth(period?: string): Promise<MonthCloseResult> {
  const response = await request('/api/workflow/report-subscription/month-close', {
    method: 'POST',
    body: JSON.stringify({ period: period ?? null }),
  })
  if (!response.ok) throw new Error('月结触发失败')
  return readJson<MonthCloseResult>(response)
}

export type SubscriptionRow = {
  id: number
  status: string
  订阅名称: string
  报表对象: string
  推送周期: string
  最近推送时间: string
  [key: string]: string | number | boolean
}

export async function listSubscriptions(): Promise<SubscriptionRow[]> {
  const response = await request('/api/report_subscription?size=200')
  if (!response.ok) throw new Error('订阅列表读取失败')
  const payload = (await response.json()) as { items: SubscriptionRow[] }
  return payload.items
}
