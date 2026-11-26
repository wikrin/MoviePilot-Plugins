<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, shallowRef, watch } from 'vue'
import {
  formatUpstreamTime,
  type Health,
  type UpstreamHistory,
  type UpstreamResult,
  type UpstreamSample,
} from '../api'

const props = withDefaults(
  defineProps<{
    history: UpstreamHistory | null
    loading: boolean
    error: string
    checking?: boolean
    blocked?: boolean
    manualAttempted?: boolean
    manualUpstreams?: UpstreamResult[]
    manualCheckedAt?: string
    manualHealth?: Health | null
    manualError?: string
    manualNotice?: string
    mobile?: boolean
  }>(),
  {
    checking: false,
    blocked: false,
    manualAttempted: false,
    manualUpstreams: () => [],
    manualCheckedAt: '',
    manualHealth: null,
    manualError: '',
    manualNotice: '',
    mobile: false,
  },
)
const emit = defineEmits<{ refresh: []; probe: [] }>()
const root = shallowRef<HTMLElement | null>(null)
let scrollTarget: HTMLElement | null = null
let pinnedScrollTop = 0
const upstreamNames = ['tmdb', 'tvdb', 'bangumi'] as const
const rows = computed(() =>
  upstreamNames.map(name => ({
    name,
    series: props.history?.items.find(row => row.name.toLowerCase() === name),
    manual: props.manualUpstreams.find(item => item.name.toLowerCase() === name),
  })),
)
// 桌面悬停显示说明；窄屏点击固定，再次点击、点外部或纵向滚动时关闭。
const hovered = shallowRef<
  | { kind: 'sample'; name: string; slot: number; element: HTMLElement }
  | { kind: 'manual'; name: string; element: HTMLElement }
  | null
>(null)
const hoveredSample = computed(() => {
  const target = hovered.value
  return target?.kind === 'sample'
    ? props.history?.items
        .find(row => row.name.toLowerCase() === target.name)
        ?.samples.find(sample => sample.slot_start === target.slot)
    : undefined
})
const showTooltip = (name: string, sample: UpstreamSample, event: MouseEvent) => {
  if (props.mobile) return
  hovered.value = { kind: 'sample', name, slot: sample.slot_start, element: event.currentTarget as HTMLElement }
}
const toggleSampleTooltip = (name: string, sample: UpstreamSample, event: Event) => {
  if (!props.mobile) return
  const element = event.currentTarget as HTMLElement
  const active = hovered.value
  hovered.value = active?.kind === 'sample' && active.name === name && active.slot === sample.slot_start
    ? null
    : { kind: 'sample', name, slot: sample.slot_start, element }
  pinnedScrollTop = scrollTarget?.scrollTop || 0
}
const sampleSelected = (name: string, sample: UpstreamSample) =>
  hovered.value?.kind === 'sample' && hovered.value.name === name && hovered.value.slot === sample.slot_start
const showManualTooltip = (name: string, event: MouseEvent) => {
  if (props.mobile || !manualReason(rows.value.find(row => row.name === name)?.manual)) return
  hovered.value = { kind: 'manual', name, element: event.currentTarget as HTMLElement }
}
const toggleManualTooltip = (name: string, event: Event) => {
  if (!props.mobile || !manualReason(rows.value.find(row => row.name === name)?.manual)) return
  const element = event.currentTarget as HTMLElement
  hovered.value = hovered.value?.kind === 'manual' && hovered.value.name === name
    ? null
    : { kind: 'manual', name, element }
  pinnedScrollTop = scrollTarget?.scrollTop || 0
}
const hideTooltip = (event: MouseEvent) => {
  if (!props.mobile && hovered.value?.element === event.currentTarget) hovered.value = null
}
const dismissPinnedTooltip = (event: Event) => {
  if (!props.mobile || !hovered.value) return
  const target = event.target as Node | null
  if (target && hovered.value.element.contains(target)) return
  hovered.value = null
}
const dismissOnScroll = () => {
  if (props.mobile && hovered.value && scrollTarget?.scrollTop !== pinnedScrollTop) hovered.value = null
}
const bindScrollTarget = () => {
  scrollTarget?.removeEventListener('scroll', dismissOnScroll)
  scrollTarget = props.mobile
    ? root.value?.closest<HTMLElement>('.ct-layout') || root.value?.parentElement || null
    : null
  scrollTarget?.addEventListener('scroll', dismissOnScroll, { passive: true })
}
onMounted(() => {
  document.addEventListener('pointerdown', dismissPinnedTooltip)
  bindScrollTarget()
})
onBeforeUnmount(() => {
  document.removeEventListener('pointerdown', dismissPinnedTooltip)
  scrollTarget?.removeEventListener('scroll', dismissOnScroll)
  scrollTarget = null
})
watch(
  () => [props.manualAttempted, props.checking, props.manualUpstreams],
  () => {
    if (hovered.value?.kind === 'manual') hovered.value = null
  },
)
watch(
  () => props.mobile,
  () => {
    hovered.value = null
    bindScrollTarget()
  },
)
type UpstreamTone = 'normal' | 'slow' | 'high-latency' | 'failed' | 'unknown'
type UpstreamMeasurement = Pick<UpstreamSample, 'reachable' | 'latency_ms'>
// 色阶只按连通性与延迟判断；未知和缺测保留灰色，不伪装为快速成功。
const normalLimitMs = 1000
const slowLimitMs = 3000
const validLatency = (value: number | null | undefined): value is number =>
  value != null && Number.isFinite(value) && value >= 0
const labels: Record<UpstreamTone, string> = {
  normal: '响应正常',
  slow: '响应偏慢',
  'high-latency': '高延迟',
  failed: '连接失败',
  unknown: '无法探测',
}
const measurementTone = (item?: UpstreamMeasurement): UpstreamTone => {
  // 连接失败优先于延迟，不能把快速失败的探测显示为正常。
  if (item?.reachable === false) return 'failed'
  if (item?.reachable !== true || !validLatency(item.latency_ms)) return 'unknown'
  return item.latency_ms <= normalLimitMs ? 'normal' : item.latency_ms <= slowLimitMs ? 'slow' : 'high-latency'
}
const measurementLabel = (item?: UpstreamMeasurement) =>
  item?.reachable === true && !validLatency(item.latency_ms) ? '无有效延迟' : labels[measurementTone(item)]
const sampleTone = (sample?: UpstreamSample | null): UpstreamTone =>
  sample?.checked_at == null ? 'unknown' : measurementTone(sample)
const manualTone = (item?: UpstreamResult): UpstreamTone =>
  !props.manualAttempted || props.checking ? 'unknown' : measurementTone(item)
const manualLabel = (item?: UpstreamResult) => {
  if (!props.manualAttempted) return '未检测'
  if (props.checking) return '检测中'
  if (item) return measurementLabel(item)
  if (props.manualError) return '未完成'
  if (!props.manualHealth?.capabilities?.upstreams) return '不可测'
  return props.manualCheckedAt ? '未返回结果' : '未检测'
}
const manualMetrics = (item: UpstreamResult) => (validLatency(item.latency_ms) ? `${item.latency_ms} ms` : '—')
const manualReason = (item?: UpstreamResult) => (props.manualAttempted && !props.checking ? item?.message || '' : '')
const percent = (value: number | null) => (value == null ? '—' : `${value.toFixed(2)}%`)
const latency = (value: number | null) => (validLatency(value) ? `${value.toFixed(1)} ms` : '—')
const sampleLabel = (sample: UpstreamSample) => (sample.checked_at == null ? '未采样' : measurementLabel(sample))
const tooltip = (sample: UpstreamSample) =>
  [
    `采样时间：${formatUpstreamTime(sample.checked_at ?? sample.slot_start)}${sample.checked_at == null ? '（未采样）' : ''}`,
    `连通性：${sampleLabel(sample)}`,
    `延迟：${latency(sample.latency_ms)}`,
    ...(sample.checked_at != null && sample.reachable !== true && sample.message ? [`原因：${sample.message}`] : []),
  ].join('\n')
const hoveredText = computed(() => {
  if (hoveredSample.value) return tooltip(hoveredSample.value)
  const target = hovered.value
  const reason = target?.kind === 'manual' ? manualReason(rows.value.find(row => row.name === target.name)?.manual) : ''
  return reason ? `手动说明：${reason}` : ''
})
</script>

<template>
  <section ref="root" class="ct-upstream-monitor" aria-label="连通性监测">
    <header class="ct-monitor-header">
      <div class="ct-monitor-copy">
        <h3>连通性监测</h3>
      </div>
      <div class="ct-monitor-actions" role="group" aria-label="连通性检测操作">
        <v-btn
          color="primary"
          variant="tonal"
          size="small"
          :loading="checking"
          :disabled="blocked || checking"
          @click="emit('probe')"
          >立即检测</v-btn
        >
        <v-btn variant="text" size="small" :loading="loading" :disabled="loading" @click="emit('refresh')"
          >刷新图表</v-btn
        >
      </div>
    </header>
    <v-progress-linear v-if="loading" indeterminate color="primary" class="mb-3" />
    <v-alert v-if="error" type="error" variant="tonal" density="compact" class="mb-3"
      >{{ error }}；刷新图表可重试，已加载的历史不会被清空。</v-alert
    >
    <v-alert
      v-if="history?.message"
      :type="history.monitoring ? 'warning' : 'info'"
      variant="tonal"
      density="compact"
      class="mb-3"
      >{{ history.message }}</v-alert
    >
    <p v-if="!loading && !error && (!history || history.checked_at == null)" class="ct-monitor-empty">
      暂无自动监测记录；启用服务后等待下一轮半小时采样。打开页面或手动测试不会补入历史。
    </p>

    <article
      v-for="row in rows"
      :key="row.name"
      class="ct-upstream-history ct-surface app-surface app-surface-static"
      :data-upstream="row.name"
      :aria-label="`${row.name.toUpperCase()} 连通性监测`"
    >
      <div class="ct-upstream-heading">
        <div class="ct-upstream-identity">
          <strong>{{ row.name.toUpperCase() }}</strong>
          <span class="ct-upstream-auto" :class="`ct-upstream-tone--${sampleTone(row.series?.latest)}`">
            <span class="ct-upstream-dot" aria-hidden="true" />
            <span
              class="ct-upstream-state"
              :title="
                row.series?.latest
                  ? `最新自动检测：${formatUpstreamTime(row.series.latest.checked_at!)}`
                  : row.series
                    ? '尚未自动采样'
                    : '此上游的自动历史尚未加载'
              "
              >{{ row.series?.latest ? sampleLabel(row.series.latest) : row.series ? '未采样' : '未知' }}</span
            >
          </span>
          <span
            v-if="manualAttempted"
            class="ct-upstream-manual"
            :class="`ct-upstream-tone--${manualTone(row.manual)}`"
          >
            <span class="ct-upstream-state">{{ manualLabel(row.manual) }}</span>
            <span
              v-if="row.manual && !checking"
              class="ct-upstream-manual-metrics"
              :class="{ 'ct-upstream-manual-metrics--has-reason': !!manualReason(row.manual) }"
              :aria-label="`${manualMetrics(row.manual)}${manualReason(row.manual) ? `；${manualReason(row.manual)}` : ''}`"
              @mouseenter="showManualTooltip(row.name, $event)"
              @mouseleave="hideTooltip"
              @click.stop="toggleManualTooltip(row.name, $event)"
              >{{ manualMetrics(row.manual) }}</span
            >
          </span>
        </div>
        <dl
          class="ct-upstream-stats"
          :title="
            row.series
              ? `${row.series.stats.sample_count} 个有效自动样本，${row.series.stats.latency_sample_count} 个 HTTP 响应延迟样本`
              : '尚未加载自动样本'
          "
        >
          <div>
            <dt title="收到上游响应的样本占有效自动探测样本的比例；缺测/不可测不计入分母，与响应码和延迟配色无关">连通率</dt>
            <dd>{{ percent(row.series?.stats.reachability_percent ?? null) }}</dd>
          </div>
          <div>
            <dt>P90 延迟</dt>
            <dd>{{ latency(row.series?.stats.p90_latency_ms ?? null) }}</dd>
          </div>
          <div>
            <dt>平均延迟</dt>
            <dd>{{ latency(row.series?.stats.avg_latency_ms ?? null) }}</dd>
          </div>
        </dl>
      </div>
      <div v-if="row.series?.samples.length && !mobile" class="ct-upstream-track ct-upstream-track--desktop">
        <div
          class="ct-upstream-bars ct-upstream-bars--desktop"
          role="group"
          :aria-label="`${row.name.toUpperCase()} 半小时自动检测历史，从旧到新`"
        >
          <span
            v-for="(sample, index) in row.series.samples"
            :key="sample.slot_start"
            class="ct-upstream-cell"
            :class="[
              `ct-upstream-tone--${sampleTone(sample)}`,
              { 'ct-upstream-cell--current': index === row.series.samples.length - 1 },
            ]"
            :data-state="sample.state"
            :data-slot="sample.slot_start"
            :data-tone="sampleTone(sample)"
            role="img"
            :aria-label="tooltip(sample)"
            @mouseenter="showTooltip(row.name, sample, $event)"
            @mouseleave="hideTooltip"
          />
        </div>
      </div>
      <div v-else-if="row.series?.samples.length" class="ct-upstream-track ct-upstream-track--mobile">
        <div class="ct-upstream-track-caption" aria-hidden="true"><span>较早</span><span>最新</span></div>
        <div
          class="ct-upstream-bars ct-upstream-bars--mobile"
          role="group"
          :aria-label="`${row.name.toUpperCase()} 过去 24 小时自动检测历史，按从左到右、从上到下排列`"
        >
          <button
            v-for="(sample, index) in row.series.samples"
            :key="sample.slot_start"
            type="button"
            class="ct-upstream-cell"
            :class="[
              `ct-upstream-tone--${sampleTone(sample)}`,
              {
                'ct-upstream-cell--current': index === row.series.samples.length - 1,
                'ct-upstream-cell--selected': sampleSelected(row.name, sample),
              },
            ]"
            :data-state="sample.state"
            :data-slot="sample.slot_start"
            :data-tone="sampleTone(sample)"
            :aria-label="tooltip(sample)"
            :aria-pressed="sampleSelected(row.name, sample)"
            @click.stop="toggleSampleTooltip(row.name, sample, $event)"
          />
        </div>
      </div>
      <p v-else class="ct-upstream-history-empty">
        {{ history ? '暂无此上游的自动监测记录。' : '自动历史尚未加载。' }}
      </p>
    </article>

    <v-tooltip
      v-if="hovered && hoveredText"
      :model-value="true"
      :activator="hovered.element"
      location="top"
      :open-on-hover="false"
      :open-on-focus="false"
      :open-on-click="false"
      :eager="false"
      transition="none"
    >
      <div class="ct-upstream-tooltip" :class="{ 'ct-upstream-manual-tooltip': hovered.kind === 'manual' }">
        {{ hoveredText }}
      </div>
    </v-tooltip>

    <footer class="ct-monitor-footer">
      <div class="ct-monitor-legend" aria-label="状态图例">
        <span class="ct-upstream-tone--normal"><i />≤ {{ normalLimitMs }} ms</span>
        <span class="ct-upstream-tone--slow"><i />&gt; {{ normalLimitMs }} 且 ≤ {{ slowLimitMs }} ms</span>
        <span class="ct-upstream-tone--high-latency"><i />&gt; {{ slowLimitMs }} ms</span>
        <span class="ct-upstream-tone--failed"><i />连接失败 / 超时</span>
        <span class="ct-upstream-tone--unknown"><i />缺测 / 无法探测 / 无有效延迟</span>
      </div>
      <p v-if="history">
        {{ formatUpstreamTime(history.window_start) }} — {{ formatUpstreamTime(history.window_end)
        }}<br />最后自动检测：{{ history.checked_at == null ? '尚无记录' : formatUpstreamTime(history.checked_at) }}
      </p>
    </footer>
  </section>
</template>

<style scoped>
.ct-upstream-monitor {
  min-inline-size: 0;
  margin-block-end: 16px;
}

.ct-monitor-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
  margin-block-end: 12px;
}

.ct-monitor-copy {
  flex: 1 1 240px;
  min-inline-size: 0;
}

.ct-monitor-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  min-inline-size: 0;
  margin-inline-start: auto;
}

.ct-monitor-actions > button {
  min-inline-size: 0;
  min-block-size: 32px;
  padding-inline: 10px;
}

.ct-monitor-header h3 {
  margin: 0;
  font-size: 14px;
  font-weight: 650;
}

.ct-monitor-empty {
  margin-block-end: 12px;
  font-size: 12px;
  color: rgba(var(--v-theme-on-surface), 0.65);
}

.ct-upstream-history-empty {
  margin: 0 0 8px;
  font-size: 11px;
  line-height: 1.6;
  color: rgba(var(--v-theme-on-surface), 0.65);
  overflow-wrap: anywhere;
}

.ct-upstream-history {
  min-inline-size: 0;
  padding: 14px 16px;
  margin-block-end: 8px;
}

/* 标题、自动/手动结果和统计采用弹性换行，长内容不会挤出卡片。 */
.ct-upstream-heading {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 10px 18px;
  margin-block-end: 8px;
}

.ct-upstream-identity {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  flex: 1 1 320px;
  min-inline-size: 0;
  gap: 8px;
  font-size: 13px;
}

.ct-upstream-identity > strong {
  flex-shrink: 0;
}

.ct-upstream-auto,
.ct-upstream-manual {
  display: inline-flex;
  align-items: center;
  flex-wrap: wrap;
  min-inline-size: 0;
  gap: 6px;
}

.ct-upstream-manual-metrics {
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}

.ct-upstream-manual-metrics--has-reason {
  cursor: help;
}

.ct-upstream-dot {
  flex: 0 0 9px;
  inline-size: 9px;
  block-size: 9px;
  border-radius: 50%;
  background: var(--ct-upstream-color);
}

.ct-upstream-state {
  padding: 2px 6px;
  border: 1px solid currentColor;
  border-radius: 6px;
  color: var(--ct-upstream-color);
  font-size: 10px;
  line-height: 1.5;
}

.ct-upstream-tone--unknown .ct-upstream-state {
  color: rgba(var(--v-theme-on-surface), 0.6);
}

.ct-upstream-stats {
  display: flex;
  flex-wrap: wrap;
  gap: 5px 16px;
  margin: 0;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}

.ct-upstream-stats > div {
  display: flex;
  gap: 5px;
  white-space: nowrap;
}

.ct-upstream-stats dt {
  color: rgba(var(--v-theme-on-surface), 0.65);
}

.ct-upstream-stats dd {
  margin: 0;
}

.ct-upstream-track {
  min-inline-size: 0;
  padding-block: 4px;
}

.ct-upstream-track--desktop {
  overflow-x: auto;
  scrollbar-width: thin;
}

.ct-upstream-bars {
  display: grid;
  gap: 3px;
}

/* 桌面单行展示 48 格并允许横向滚动；窄屏每行 24 格，以两行保留完整历史。 */
.ct-upstream-bars--desktop {
  grid-template-columns: repeat(48, 8px);
  inline-size: 525px;
}

.ct-upstream-bars--mobile {
  grid-template-columns: repeat(24, minmax(0, 1fr));
  inline-size: 100%;
  gap: 4px;
}

.ct-upstream-track-caption {
  display: flex;
  justify-content: space-between;
  margin-block-end: 4px;
  color: rgba(var(--v-theme-on-surface), 0.52);
  font-size: 9px;
}

.ct-upstream-cell {
  appearance: none;
  min-inline-size: 0;
  block-size: 22px;
  padding: 0;
  border: 0;
  border-radius: 2px;
  background: var(--ct-upstream-color);
  cursor: help;
}

.ct-upstream-bars--mobile .ct-upstream-cell {
  block-size: 14px;
  cursor: pointer;
  touch-action: manipulation;
}

.ct-upstream-cell--current {
  box-shadow: 0 0 0 1px rgba(var(--v-theme-on-surface), 0.35);
}

.ct-upstream-cell--selected {
  outline: 2px solid rgb(var(--v-theme-primary));
  outline-offset: 1px;
}

.ct-upstream-cell:hover {
  filter: brightness(0.9);
}

.ct-upstream-tone--normal {
  --ct-upstream-color: rgb(var(--v-theme-success));
}

/* 黄色与橙色使用独立色值，避免共享 warning 主题色而无法区分。 */
.ct-upstream-tone--slow {
  --ct-upstream-color: #fbc02d;
}

.ct-upstream-tone--high-latency {
  --ct-upstream-color: #fb8c00;
}

.ct-upstream-tone--failed {
  --ct-upstream-color: rgb(var(--v-theme-error));
}

.ct-upstream-tone--unknown {
  --ct-upstream-color: rgba(var(--v-theme-on-surface), 0.25);
}

/* 提示层保留说明换行并限制宽度，不拦截鼠标，避免悬停区域反复进入/离开。 */
.ct-upstream-tooltip {
  pointer-events: none;
  max-inline-size: min(420px, calc(100vw - 48px));
  white-space: pre-line;
  overflow-wrap: anywhere;
  font-size: 11px;
  line-height: 1.6;
}

.ct-monitor-footer {
  font-size: 10px;
  line-height: 1.7;
  color: rgba(var(--v-theme-on-surface), 0.6);
}

.ct-monitor-footer p {
  margin: 6px 0 0;
  overflow-wrap: anywhere;
}

.ct-monitor-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 14px;
}

.ct-monitor-legend span {
  display: flex;
  align-items: center;
  gap: 5px;
}

.ct-monitor-legend i {
  inline-size: 7px;
  block-size: 12px;
  border-radius: 2px;
  background: var(--ct-upstream-color);
}

@container (width < 720px) {
  .ct-upstream-history {
    padding: 12px;
  }

  .ct-upstream-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: 8px;
  }

  .ct-upstream-identity {
    flex-basis: auto;
  }

  .ct-upstream-stats {
    inline-size: 100%;
    justify-content: space-between;
    gap: 4px 8px;
  }

  .ct-upstream-stats > div {
    flex-direction: column;
    gap: 0;
  }

  .ct-monitor-header {
    align-items: flex-start;
  }

  .ct-monitor-actions {
    margin-inline-start: 0;
  }

  .ct-monitor-actions > button {
    min-block-size: 36px;
    font-size: 11px;
  }
}
</style>
