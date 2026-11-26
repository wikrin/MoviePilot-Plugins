<script setup lang="ts">
import { computed, inject, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  errorMessage,
  type Api,
  type Health,
  type Status,
  type UpstreamHistory,
  type UpstreamProbe,
  type UpstreamResult,
} from '../api'
import UpstreamMonitor from './UpstreamMonitor.vue'
import SurfaceCard from './SurfaceCard.vue'
const props = withDefaults(defineProps<{ api: Api; dirty: boolean; mobile?: boolean }>(), { mobile: false })
const emit = defineEmits(['busy', 'status'])
const status = ref<Status | null>(null)
const error = ref('')
const notice = ref('')
const working = ref(false)
const checking = ref(false)
const manualUpstreams = ref<UpstreamResult[]>([])
const manualCheckedAt = ref('')
const manualAttempted = ref(false)
const manualError = ref('')
const manualNotice = ref('')
const manualHealth = ref<Health | null>(null)
const upstreamHistory = ref<UpstreamHistory | null>(null)
const historyLoading = ref(false)
const historyError = ref('')
const action = ref('')
const timer = ref<ReturnType<typeof setTimeout>>()
let stopped = false
let loading = false
let statusRequestId = 0
let historyRequestId = 0
let manualRequestId = 0
let lastHistoryRefresh: number | null = null
const confirmation = ref('')
const actionMenu = ref(false)
const confirm = inject<((options: any) => Promise<boolean>) | null>('moviepilot:confirm', null)
const labels: Record<string, string> = {
  running: '运行中',
  stopped: '已停止',
  starting: '启动中',
  connecting: '连接中',
  downloading: '下载中',
  error: '异常',
}
// 收发方向共用展示结构，速率与累计仍分别保留「采集中 / 不可用」语义。
const networkDirections = [
  { key: 'rx', label: '接收' },
  { key: 'tx', label: '发送' },
] as const
const active = computed(() => ['running', 'starting', 'connecting', 'downloading'].includes(status.value?.state || ''))
const blocked = computed(() => props.dirty || working.value || checking.value || status.value?.busy)
const metricBytes = (value: number | null | undefined) => {
  if (value == null) return '不可用'
  if (value < 1024) return `${value.toFixed(0)} B`
  const units = ['KiB', 'MiB', 'GiB', 'TiB']
  let size = value / 1024
  let unit = 0
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024
    unit++
  }
  return `${size.toFixed(1)} ${units[unit]}`
}
const metricRate = (value: number | null | undefined, supported = true) => {
  if (!supported) return '不可用'
  return value == null ? '采集中' : `${metricBytes(value)}/s`
}
async function refresh() {
  if (loading || stopped) return
  const id = ++statusRequestId
  loading = true
  try {
    const data: Status = await props.api.get('status')
    if (!stopped && id === statusRequestId) {
      status.value = data
      emit('status', data)
    }
  } catch (err) {
    if (!stopped && id === statusRequestId) error.value = errorMessage(err)
  } finally {
    if (id === statusRequestId) loading = false
  }
}
async function refreshHistory() {
  if (historyLoading.value || stopped) return
  const id = ++historyRequestId
  historyLoading.value = true
  historyError.value = ''
  lastHistoryRefresh = Date.now()
  try {
    const data: UpstreamHistory = await props.api.get('upstreams/history')
    if (!Array.isArray(data?.items) || !Number.isFinite(data.window_start) || !Number.isFinite(data.window_end))
      throw new Error('上游监测历史响应无效，请更新插件后重试')
    if (!stopped && id === historyRequestId) upstreamHistory.value = data
  } catch (err) {
    if (!stopped && id === historyRequestId) historyError.value = errorMessage(err)
  } finally {
    if (!stopped && id === historyRequestId) historyLoading.value = false
  }
}
// 状态每 5 秒刷新、自动历史每分钟只读刷新；手动探测单独存放，不改写自动历史。
async function poll() {
  if (!document.hidden && !working.value) {
    if (lastHistoryRefresh == null || Date.now() - lastHistoryRefresh >= 60000) void refreshHistory()
    await refresh()
  }
  if (!stopped) timer.value = setTimeout(poll, 5000)
}
const controlMessage = (value: string) =>
  value === 'stop'
    ? '停止后将恢复原始 TMDB 连接。'
    : value === 'restart'
      ? '此操作会重启当前托管服务，期间服务连接将暂时中断。'
      : '此操作会停止当前托管进程并安装插件指定的目标版本；下载或更新完成后需手动启动。'
async function control(value: string) {
  if (blocked.value || stopped) return
  if (['stop', 'restart', 'download', 'update'].includes(value)) {
    if (confirm) {
      if (!(await confirm({ title: '确认服务操作', content: controlMessage(value), type: 'warn' }))) return
    } else {
      confirmation.value = value
      return
    }
  }
  await execute(value)
}
function mobileControl(value: string) {
  actionMenu.value = false
  void control(value)
}
async function execute(value: string) {
  if (blocked.value || stopped) return
  confirmation.value = ''
  working.value = true
  emit('busy', true)
  error.value = ''
  notice.value = ''
  action.value = value
  try {
    status.value = await props.api.post('control', { action: value })
    notice.value = ['start', 'restart'].includes(value)
      ? '启动任务已提交，请在运行状态中核对实际状态。'
      : '操作已完成。'
  } catch (err) {
    error.value = `${errorMessage(err)}；请刷新核对实际状态后再操作。`
  } finally {
    working.value = false
    emit('busy', false)
    await refresh()
  }
}
async function probe() {
  if (blocked.value || stopped) return
  const id = ++manualRequestId
  checking.value = true
  manualAttempted.value = true
  emit('busy', true)
  manualError.value = ''
  manualNotice.value = ''
  manualUpstreams.value = []
  manualCheckedAt.value = ''
  manualHealth.value = null
  try {
    const result: { health?: Health } = await props.api.post('probe')
    if (stopped || id !== manualRequestId) return
    const checkedAt = new Date().toISOString()
    await refresh()
    if (stopped || id !== manualRequestId) return
    manualHealth.value = result?.health || status.value?.health || null
    manualCheckedAt.value = checkedAt
    if (manualHealth.value?.capabilities?.upstreams) {
      const data: UpstreamProbe = await props.api.get('upstreams')
      const validItems =
        Array.isArray(data?.items) &&
        data.items.every(
          item =>
            item &&
            typeof item.name === 'string' &&
            item.name.length > 0 &&
            typeof item.message === 'string' &&
            typeof item.reachable === 'boolean' &&
            typeof item.available === 'boolean' &&
            Number.isInteger(item.status_code) &&
            (item.status_code === 0 || (item.status_code >= 100 && item.status_code <= 599)) &&
            item.reachable === (item.status_code !== 0) &&
            item.available === (item.reachable && item.status_code >= 200 && item.status_code < 300) &&
            typeof item.latency_ms === 'number' &&
            Number.isFinite(item.latency_ms) &&
            item.latency_ms >= 0,
        )
      if (!validItems || typeof data.checked_at !== 'string' || !Number.isFinite(Date.parse(data.checked_at)))
        throw new Error('上游检测响应无效，请更新服务后重试')
      if (!stopped && id === manualRequestId) {
        manualUpstreams.value = data.items
        manualCheckedAt.value = data.checked_at
      }
    } else if (!stopped) manualNotice.value = '上游检测需要新版服务；若服务启用管理鉴权，还需配置匹配的令牌。'
  } catch (err) {
    if (!stopped && id === manualRequestId) manualError.value = errorMessage(err)
  } finally {
    if (!stopped && id === manualRequestId) {
      checking.value = false
      emit('busy', false)
    }
  }
}
onMounted(() => {
  void poll()
})
onBeforeUnmount(() => {
  if (working.value || checking.value) emit('busy', false)
  stopped = true
  clearTimeout(timer.value)
  statusRequestId++
  historyRequestId++
  manualRequestId++
})
</script>

<template>
  <div class="ct-runtime">
    <v-alert v-if="error" type="error" variant="tonal" class="mb-3" density="compact"
      >{{ error }}<v-btn variant="text" size="small" @click="refresh">刷新状态</v-btn></v-alert
    >
    <v-alert v-if="notice" type="info" variant="tonal" class="mb-3" density="compact">{{ notice }}</v-alert>
    <SurfaceCard
      vuetify
      variant="outlined"
      :rounded="true"
      class="ct-runtime-card mb-4"
      header-class="ct-runtime-header"
    >
      <template #header>
        <div class="ct-runtime-title">
          <h3>运行状态</h3>
          <v-chip
            class="ct-runtime-state"
            :color="status?.state === 'running' ? 'success' : 'secondary'"
            size="small"
            >{{ labels[status?.state || ''] || '读取中' }}</v-chip
          >
        </div>
        <div v-if="status?.controllable" class="ct-runtime-actions" role="group" aria-label="服务生命周期操作">
          <v-btn
            v-if="active"
            size="small"
            variant="tonal"
            :disabled="blocked"
            :loading="working && action === 'stop'"
            @click="control('stop')"
            >停止服务</v-btn
          >
          <v-btn
            v-else
            color="primary"
            size="small"
            variant="tonal"
            :disabled="blocked || !status.enabled"
            :loading="working && action === 'start'"
            @click="control('start')"
            >启动服务</v-btn
          >
          <template v-if="!mobile">
            <v-btn
              size="small"
              variant="text"
              :disabled="blocked || !status.enabled"
              :loading="working && action === 'restart'"
              @click="control('restart')"
              >重启服务</v-btn
            >
            <v-btn
              size="small"
              variant="text"
              :disabled="blocked"
              :loading="working && ['download', 'update'].includes(action)"
              title="安装插件指定的目标版本，可重新下载；完成后需手动启动。"
              @click="control(status.binary_exists ? 'update' : 'download')"
              >{{ status.binary_exists ? '重装目标版本' : '下载目标版本' }}</v-btn
            >
          </template>
          <v-menu v-else v-model="actionMenu" location="bottom end" :close-on-content-click="false">
            <template #activator="{ props: activatorProps }">
              <v-btn
                v-bind="activatorProps"
                class="ct-runtime-more"
                icon="mdi-dots-horizontal"
                size="small"
                variant="text"
                :disabled="blocked"
                aria-label="更多服务操作"
              />
            </template>
            <v-list density="compact" aria-label="更多服务操作">
              <v-list-item
                title="重启服务"
                :disabled="blocked || !status.enabled"
                @click="mobileControl('restart')"
              />
              <v-list-item
                :title="status.binary_exists ? '重装目标版本' : '下载目标版本'"
                :disabled="blocked"
                @click="mobileControl(status.binary_exists ? 'update' : 'download')"
              />
            </v-list>
          </v-menu>
        </div>
      </template>
        <p v-if="!status" class="ct-runtime-loading" role="status">正在读取服务状态…</p>
        <dl v-if="status" class="ct-runtime-address">
          <dt>使用地址</dt>
          <dd>{{ status.address }}</dd>
        </dl>
        <dl v-if="status" class="ct-runtime-facts">
          <div>
            <dt>部署模式</dt>
            <dd>{{ status.mode === 'local' ? '插件托管' : '外部服务' }}</dd>
          </div>
          <div>
            <dt>服务版本</dt>
            <dd>{{ status.health?.version || status.installed_version || '尚未检测' }}</dd>
          </div>
          <div v-if="status.mode === 'local'">
            <dt>目标版本</dt>
            <dd>{{ status.required_version }}</dd>
          </div>
          <div>
            <dt>TMDB 代理补丁</dt>
            <dd>{{ status.patched ? '已应用' : '未应用' }}</dd>
          </div>
        </dl>
        <v-alert v-if="status?.error" type="warning" variant="tonal" density="compact" class="mt-3">{{
          status.error
        }}</v-alert>
        <v-alert
          v-if="status?.health?.status === 'legacy'"
          type="warning"
          variant="tonal"
          density="compact"
          class="mt-3"
          >当前服务版本仅支持旧版映射接口。健康详情、上游检测和服务日志需部署包含管理接口的新版本。</v-alert
        >
        <v-alert
          v-if="status?.health?.logging?.available === false"
          type="warning"
          variant="tonal"
          density="compact"
          class="mt-3"
          >服务日志持久化暂不可用，请检查数据目录并修复存储后重启。支持实时流时仍可接收后续输出，但无法补回未保存的历史。</v-alert
        >
        <v-alert v-if="confirmation" variant="tonal" type="warning" class="mt-3"
          >{{ controlMessage(confirmation) }}
          <div class="mt-2">
            <v-btn color="warning" size="small" :disabled="blocked" @click="execute(confirmation)">确认操作</v-btn
            ><v-btn variant="text" size="small" @click="confirmation = ''">取消</v-btn>
          </div></v-alert
        >
        <p v-if="status?.mode === 'external'" class="ct-runtime-note">
          外部容器只读监测；无法从插件读取外部进程的资源数据，且不会展示容器整体指标。生命周期操作由部署工具管理。
        </p>
        <p v-else-if="status?.mode === 'local'" class="ct-runtime-note">
          更新安装插件指定的目标版本，失败保留原可执行文件；完成后需手动启动。
        </p>
    </SurfaceCard>
    <SurfaceCard
      v-if="status?.mode === 'local' && status.pid"
      vuetify
      variant="outlined"
      :rounded="true"
      class="ct-runtime-card ct-process-metrics mb-4"
      aria-label="进程资源"
      header-class="ct-runtime-header"
    >
      <template #header>
        <h3 class="ct-resource-title">进程资源</h3>
        <span class="ct-process-pid">PID {{ status.pid }}</span>
      </template>
        <dl class="ct-runtime-facts">
          <div>
            <dt>内存（RSS）</dt>
            <dd>{{ metricBytes(status.process_metrics?.memory_bytes) }}</dd>
          </div>
          <div>
            <dt>CPU（单核口径）</dt>
            <dd>
              {{
                status.process_metrics?.cpu_percent == null
                  ? '采集中'
                  : `${status.process_metrics.cpu_percent.toFixed(1)}%`
              }}
            </dd>
          </div>
          <div>
            <dt>网络速率</dt>
            <dd class="ct-metric-network">
              <span v-for="direction in networkDirections" :key="direction.key" class="ct-metric-line"
                ><span class="ct-metric-direction">{{ direction.label }}</span
                ><span class="ct-metric-value">{{
                  metricRate(
                    status.process_metrics?.[`network_${direction.key}_rate`],
                    !status.process_metrics || status.process_metrics[`network_${direction.key}_bytes`] != null,
                  )
                }}</span></span
              >
            </dd>
          </div>
          <div>
            <dt>网络累计</dt>
            <dd class="ct-metric-network">
              <span v-for="direction in networkDirections" :key="direction.key" class="ct-metric-line"
                ><span class="ct-metric-direction">{{ direction.label }}</span
                ><span class="ct-metric-value">{{
                  metricBytes(status.process_metrics?.[`network_${direction.key}_bytes`])
                }}</span></span
              >
            </dd>
          </div>
        </dl>
    </SurfaceCard>
    <UpstreamMonitor
      :history="upstreamHistory"
      :loading="historyLoading"
      :error="historyError"
      :checking="checking"
      :blocked="blocked"
      :manual-attempted="manualAttempted"
      :manual-upstreams="manualUpstreams"
      :manual-checked-at="manualCheckedAt"
      :manual-health="manualHealth"
      :manual-error="manualError"
      :manual-notice="manualNotice"
      :mobile="mobile"
      @probe="probe"
      @refresh="refreshHistory"
    />
  </div>
</template>
<style scoped>
.ct-runtime {
  container: ct-runtime / inline-size;
  min-inline-size: 0;
}
.ct-runtime-card {
  min-inline-size: 0;
}
:deep(.ct-runtime-header) {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px 14px;
  min-inline-size: 0;
  padding-block: 12px;
  white-space: normal;
  overflow: visible;
}
.ct-runtime-title {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  min-inline-size: 0;
}
.ct-runtime-title h3,
.ct-resource-title {
  margin: 0;
  font-size: 14px;
  font-weight: 650;
  line-height: 1.4;
}
.ct-runtime-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 4px;
  min-inline-size: 0;
  margin-inline-start: auto;
}
.ct-runtime-actions > button {
  min-inline-size: 0;
  min-block-size: 32px;
  max-inline-size: 100%;
  padding-inline: 10px;
}
/* 地址独占一行并允许长链接换行；状态与资源按内容宽度从四列降为两列、单列。 */
.ct-runtime-address {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  align-items: baseline;
  gap: 8px 12px;
  margin-block-end: 16px;
}
.ct-runtime-address dt,
.ct-runtime-facts dt {
  color: var(--ct-muted);
  font-size: 11px;
}
.ct-runtime-address dd,
.ct-runtime-facts dd {
  min-inline-size: 0;
  margin: 0;
  overflow-wrap: anywhere;
  font-size: 12px;
  line-height: 1.6;
}
.ct-runtime-facts {
  display: grid;
  gap: 14px;
  grid-template-columns: repeat(4, minmax(0, 1fr));
}
.ct-runtime-facts > div {
  min-inline-size: 0;
}
.ct-runtime-facts dd {
  margin-block-start: 5px;
}
.ct-runtime-loading {
  padding-block: 8px;
  color: var(--ct-muted);
  font-size: 12px;
}
.ct-runtime-note {
  margin-block-start: 14px;
  color: var(--ct-muted);
  font-size: 11px;
  line-height: 1.6;
}
.ct-process-pid {
  color: var(--ct-muted);
  font-size: 11px;
  font-weight: 500;
}
.ct-metric-network {
  display: grid;
  gap: 3px;
}
.ct-metric-line {
  display: flex;
  align-items: baseline;
  gap: 6px;
}
.ct-metric-direction {
  flex: 0 0 auto;
  color: var(--ct-muted);
  font-size: 10px;
}
.ct-metric-value {
  min-inline-size: 0;
  font-variant-numeric: tabular-nums;
}
@container ct-runtime (width < 640px) {
  .ct-runtime-facts {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
@container ct-runtime (width < 520px) {
  .ct-runtime-actions > button {
    min-block-size: 36px;
    padding-inline: 8px;
    font-size: 11px;
  }
}
@container ct-runtime (width < 400px) {
  .ct-runtime-facts {
    grid-template-columns: minmax(0, 1fr);
  }
  .ct-runtime-address {
    grid-template-columns: minmax(0, 1fr);
    gap: 4px;
  }
}
</style>
