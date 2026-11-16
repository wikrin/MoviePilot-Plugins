<script setup>
import { computed, defineComponent, h, ref, watch } from 'vue'
import TorrentTree from './TorrentTree.vue'
import {
  CHANGE_COLORS,
  CHANGE_LABELS,
  diffSegments,
  OPERATION_LABELS,
  RESULT_COLORS,
  RESULT_LABELS,
} from '../utils/torrentTree.js'
import { formatSize } from '../api/torrentformatter.js'

const props = defineProps({
  plan: { type: Object, required: true },
  result: { type: Object, default: null },
  statusLoading: Boolean,
  statusError: { type: String, default: '' },
})
const emit = defineEmits(['refresh-result', 'copy', 'recover', 'history'])
const view = ref('tree')
const side = ref('before')
const relatedOnly = ref(false)
const search = ref('')
const filter = ref('all')
const page = ref(1)
const selectedFile = ref(null)
const after = computed(() => props.result?.actual || props.plan.after)
const scalars = computed(() =>
  props.plan.changes.filter(item => !['file', 'auto_tmm'].includes(item.kind) && !item.id.startsWith('validation:')),
)
const autoSteps = computed(() => props.plan.changes.filter(item => item.kind === 'auto_tmm'))
const logs = computed(() => new Map((props.result?.operations || []).map(item => [item.change.id, item])))
const changes = computed(() =>
  props.plan.changes.filter(
    item =>
      (filter.value === 'all' || item.status === filter.value) &&
      (!relatedOnly.value || item.status !== 'unchanged' || (props.plan.counts.save_path && item.kind === 'file')) &&
      (!search.value ||
        `${item.before} ${item.after} ${item.reason}`.toLocaleLowerCase().includes(search.value.toLocaleLowerCase())),
  ),
)
const pages = computed(() => Math.max(1, Math.ceil(changes.value.length / 20)))
const pageChanges = computed(() => changes.value.slice((page.value - 1) * 20, page.value * 20))
const selectedChange = computed(() =>
  props.plan.changes.find(item => item.kind === 'file' && item.file_id === selectedFile.value),
)
const selectedAfter = computed(() => after.value.files.find(item => item.index === selectedFile.value))
const selectedBefore = computed(() => props.plan.before.files.find(item => item.index === selectedFile.value))
const selectedFullBefore = computed(() =>
  selectedChange.value ? `${props.plan.before.save_path.replace(/\/$/, '')}/${selectedChange.value.before}` : '',
)
const selectedFullAfter = computed(() =>
  selectedAfter.value ? `${after.value.save_path.replace(/\/$/, '')}/${selectedAfter.value.name}` : '',
)
const confirmed = computed(() => (props.result?.operations || []).filter(item => item.status === 'confirmed').length)

const DiffPair = defineComponent({
  props: { before: { type: String, default: '' }, after: { type: String, default: '' } },
  setup(values) {
    return () => {
      const diff = diffSegments(values.before, values.after)
      const line = (mode, changed) =>
        h('div', { class: `tf-diff-line tf-diff-line--${mode}` }, [
          h('span', { class: 'tf-diff-sign', 'aria-hidden': 'true' }, mode === 'before' ? '−' : '+'),
          h(
            'code',
            { class: 'tf-code-path', 'aria-label': `${mode === 'before' ? '修改前' : '修改后'}：${values[mode]}` },
            [diff.prefix, changed ? h('mark', { class: `tf-diff-mark--${mode}` }, changed) : null, diff.suffix],
          ),
        ])
      return h('div', { class: 'tf-diff-pair' }, [line('before', diff.removed), line('after', diff.added)])
    }
  },
})
function label(change) {
  return {
    save_path: '保存路径',
    name: '种子显示名',
    file: '相对文件路径',
    auto_tmm: change.id.includes('pause') ? '自动管理 · 移动前' : '自动管理',
  }[change.kind]
}
function scalarValue(change, stage) {
  if (change.kind === 'auto_tmm') return change[stage] === 'true' ? '开启' : '关闭'
  return change.kind === 'name' ? props.plan[stage].name : props.plan[stage].save_path
}
function changeLabel(change) {
  const log = logs.value.get(change.id)
  if (log) return OPERATION_LABELS[log.status]
  return change.kind === 'file' && change.status === 'unchanged' && props.plan.counts.save_path
    ? '随目录移动'
    : CHANGE_LABELS[change.status]
}
watch([search, filter, relatedOnly], () => {
  page.value = 1
})
watch(
  () => props.plan.torrent_hash,
  () => {
    page.value = 1
    selectedFile.value = null
    search.value = ''
  },
)
</script>

<template>
  <section class="tf-preview">
    <div class="tf-heading">
      <div>
        <span class="tf-eyebrow">04 / {{ plan.mode === 'recover' ? 'RESTORE PREVIEW' : 'CHANGE PREVIEW' }}</span>
        <h2>{{ plan.mode === 'recover' ? '核对恢复差异' : '看清每一项变化' }}</h2>
        <p>
          {{
            result
              ? '左侧保留原始快照，右侧展示下载器回读的实际状态。'
              : '这是只读计划。确认前不会移动、重命名文件或改写下载记录。'
          }}
        </p>
      </div>
      <v-chip
        :color="plan.status === 'blocked' ? 'error' : plan.status === 'ready' ? 'primary' : 'default'"
        variant="tonal"
        size="small"
        >{{
          plan.status === 'blocked'
            ? '不能执行'
            : plan.status === 'ready'
              ? plan.mode === 'recover'
                ? '可确认恢复'
                : '可确认处理'
              : '无变更'
        }}</v-chip
      >
    </div>
    <v-alert
      v-if="plan.status !== 'ready'"
      :type="plan.status === 'blocked' ? 'warning' : 'info'"
      variant="tonal"
      density="compact"
      class="tf-inline-alert"
      >{{ plan.message }}</v-alert
    >
    <div v-if="plan.media" class="tf-preview-media tf-card">
      <v-icon
        :icon="plan.media.media_type === 'tv' ? 'mdi-television-classic' : 'mdi-movie-open-outline'"
        color="primary"
        size="22"
      />
      <div>
        <strong
          >{{ plan.media.title }} <span class="tf-muted">{{ plan.media.year }}</span></strong
        ><span class="tf-meta-line"
          >{{ plan.media.media_type === 'tv' ? '电视剧' : '电影' }} · {{ plan.media.media_source }}
          {{ plan.media.media_id }} ·
          {{
            {
              manual: '手动指定',
              history: '下载记录',
              assisted: '辅种源记录',
              recognized: '名称识别',
            }[plan.media.source]
          }}</span
        >
      </div>
    </div>
    <div class="tf-scalar-grid">
      <div v-for="change in scalars" :key="change.id" class="tf-card tf-scalar">
        <div class="tf-card-header">
          <h3>{{ label(change) }}</h3>
          <v-chip :color="CHANGE_COLORS[change.status]" size="x-small" variant="tonal">{{
            CHANGE_LABELS[change.status]
          }}</v-chip>
        </div>
        <div class="tf-scalar-body">
          <DiffPair :before="scalarValue(change, 'before')" :after="scalarValue(change, 'after')" />
          <p v-if="change.reason" class="tf-muted">{{ change.reason }}</p>
          <p v-if="change.status === 'unsupported'" class="tf-muted">
            请求目标（不会执行）：<code class="tf-code-path">{{ change.after }}</code>
          </p>
          <button
            v-if="change.kind === 'save_path'"
            class="tf-copy-link"
            type="button"
            @click="emit('copy', plan.after.save_path)"
          >
            复制预计路径
          </button>
        </div>
      </div>
    </div>
    <div v-if="autoSteps.length" class="tf-tmm-note">
      <v-icon icon="mdi-cog-sync-outline" size="16" /><span v-for="step in autoSteps" :key="step.id"
        >自动管理：{{ scalarValue(step, 'before') }} → {{ scalarValue(step, 'after')
        }}<small>{{ step.reason }}</small></span
      >
    </div>
    <v-alert v-if="plan.warnings.length" variant="tonal" type="warning" density="compact" class="mt-3 mb-3">
      <ul class="tf-warning-list">
        <li v-for="warning in plan.warnings.slice(0, 1)" :key="warning">{{ warning }}</li>
      </ul>
      <span v-if="plan.warnings.length > 1">其他跳过原因在清单中展开。</span>
    </v-alert>
    <div v-if="result" class="tf-card tf-result">
      <div class="tf-card-header">
        <h3>执行结果 · {{ confirmed }} / {{ result.operations.length }} 项确认</h3>
        <v-chip :color="RESULT_COLORS[result.status]" variant="tonal" size="small">{{
          RESULT_LABELS[result.status]
        }}</v-chip>
      </div>
      <div class="tf-card-body">
        <p class="mt-0">{{ result.message }}</p>
        <div v-if="result.actual">
          <span class="tf-summary-label">实际名称 / 保存路径</span
          ><strong class="tf-summary-name">{{ result.actual.name }}</strong
          ><code class="tf-code-path mt-1">{{ result.actual.save_path }}</code>
        </div>
        <div v-for="log in result.operations.slice(0, 8)" :key="log.change.id" class="tf-operation-row">
          <span :title="log.change.after"
            >{{ label(log.change)
            }}<small>{{ log.change.kind === 'file' ? log.change.after : log.message }}</small></span
          ><v-chip
            :color="log.status === 'confirmed' ? 'success' : log.status === 'failed' ? 'error' : 'warning'"
            variant="tonal"
            size="x-small"
            >{{ OPERATION_LABELS[log.status] }}</v-chip
          >
        </div>
        <v-btn v-if="result.operations.length > 8" size="small" variant="text" @click="view = 'list'"
          >在清单中查看所有操作</v-btn
        >
        <p v-if="!result.operations.length" class="mb-0">无需布局修改，仅核对原状态、还原下载记录并清理备份。</p>
        <v-alert v-if="statusError" type="error" density="compact" variant="tonal" class="mt-3">{{
          statusError
        }}</v-alert>
        <div class="tf-action-row">
          <v-btn variant="tonal" color="primary" size="small" :loading="statusLoading" @click="emit('refresh-result')"
            >回查实际状态</v-btn
          ><v-btn
            v-if="result.can_recover && result.mode === 'format'"
            variant="text"
            size="small"
            :disabled="result.actual?.moving"
            @click="emit('recover')"
            >预览恢复</v-btn
          ><v-btn v-else variant="text" size="small" @click="emit('history')">处理历史</v-btn>
        </div>
      </div>
    </div>
    <div class="tf-card tf-file-preview">
      <div class="tf-preview-toolbar">
        <v-btn-toggle v-model="view" mandatory density="compact" color="primary" variant="tonal" divided
          ><v-btn value="tree" size="small" prepend-icon="mdi-file-tree-outline">目录树</v-btn
          ><v-btn value="list" size="small" prepend-icon="mdi-format-list-checks">变更清单</v-btn></v-btn-toggle
        ><span class="tf-muted text-caption"
          >{{ plan.counts.files }} 个文件改名 / {{ plan.counts.total_files }} 个文件</span
        >
      </div>
      <div class="tf-preview-filters">
        <v-text-field
          v-model="search"
          label="搜索路径或文件名"
          prepend-inner-icon="mdi-magnify"
          variant="outlined"
          density="compact"
          hide-details
          clearable
          @update:model-value="search = $event || ''"
        /><v-checkbox
          v-model="relatedOnly"
          label="仅处理相关"
          color="primary"
          density="compact"
          hide-details
          title="包含变更、跳过、不支持及随目录移动的文件"
        />
      </div>
      <template v-if="view === 'tree'"
        ><v-tabs v-model="side" class="tf-tree-side-tabs" color="primary" density="compact" grow
          ><v-tab value="before">{{ plan.mode === 'recover' ? '恢复前' : '处理前' }}</v-tab
          ><v-tab value="after">{{
            result?.actual ? '实际状态' : plan.mode === 'recover' ? '预计恢复后' : '预计处理后'
          }}</v-tab></v-tabs
        >
        <div class="tf-tree-dual">
          <div class="tf-tree-pane" :class="{ 'tf-tree-pane--selected': side === 'before' }">
            <div class="tf-tree-pane-title">
              <span class="tf-before-dot" />{{ plan.mode === 'recover' ? '当前状态 · 恢复前' : '原始布局 · 处理前' }}
            </div>
            <TorrentTree
              :snapshot="plan.before"
              :changes="plan.changes"
              :only-changed="relatedOnly"
              :search="search"
              :selected-file-id="selectedFile"
              label="修改前目录树"
              @select="selectedFile = $event"
            />
          </div>
          <div class="tf-tree-pane" :class="{ 'tf-tree-pane--selected': side === 'after' }">
            <div class="tf-tree-pane-title">
              <span class="tf-after-dot" />{{
                result?.actual ? '下载器回读 · 实际状态' : plan.mode === 'recover' ? '预计恢复后布局' : '预计处理后布局'
              }}
            </div>
            <TorrentTree
              :snapshot="after"
              :changes="plan.changes"
              :operations="result?.actual ? result.operations : []"
              :actual="Boolean(result?.actual)"
              :only-changed="relatedOnly"
              :search="search"
              :selected-file-id="selectedFile"
              label="修改后目录树"
              @select="selectedFile = $event"
            />
          </div>
        </div>
        <div class="tf-tree-legend">
          <span><i class="tf-legend-primary" />文件路径改名</span><span><i class="tf-legend-move" />保存目录移动</span
          ><span><i class="tf-legend-warning" />跳过 / 不支持</span><span><i class="tf-legend-error" />异常</span>
        </div>
      </template>
      <template v-else>
        <div class="tf-list-filter">
          <v-select
            v-model="filter"
            :items="[
              { title: '全部状态', value: 'all' },
              ...Object.entries(CHANGE_LABELS).map(([value, title]) => ({ value, title })),
            ]"
            label="筛选状态"
            density="compact"
            variant="outlined"
            hide-details
          /><span class="tf-muted text-caption">清单为计划值；执行确认状态以标签与回读结果为准。</span>
        </div>
        <div v-for="change in pageChanges" :key="change.id" class="tf-change-row">
          <div class="tf-change-heading">
            <span
              >{{ label(change) }}<small v-if="change.file_id !== null"> #{{ change.file_id }}</small></span
            ><v-chip
              :color="logs.get(change.id)?.status === 'confirmed' ? 'success' : CHANGE_COLORS[change.status]"
              size="x-small"
              variant="tonal"
              >{{ changeLabel(change) }}</v-chip
            >
          </div>
          <DiffPair :before="change.before" :after="change.after" />
          <p v-if="logs.get(change.id)?.message || change.reason" class="tf-change-reason">
            {{ logs.get(change.id)?.message || change.reason }}
          </p>
        </div>
        <div v-if="!pageChanges.length" class="tf-empty">
          <h3>没有匹配项</h3>
          <p>尝试取消筛选或搜索其他路径。</p>
        </div>
        <div class="tf-pagination">
          <span>{{ changes.length }} 项 · {{ page }} / {{ pages }} 页</span>
          <div>
            <v-btn size="small" variant="text" :disabled="page <= 1" @click="page--">上一页</v-btn
            ><v-btn size="small" variant="text" :disabled="page >= pages" @click="page++">下一页</v-btn>
          </div>
        </div>
      </template>
    </div>
    <div v-if="selectedChange" class="tf-card mt-4">
      <div class="tf-card-header">
        <h3>选中文件 · #{{ selectedChange.file_id }}</h3>
        <span class="tf-muted text-caption">{{ formatSize(selectedBefore?.size) }}</span>
      </div>
      <div class="tf-card-body">
        <span class="tf-summary-label">下载器中的完整位置</span>
        <DiffPair :before="selectedFullBefore" :after="selectedFullAfter" />
        <p v-if="selectedChange.reason">{{ selectedChange.reason }}</p>
        <button class="tf-copy-link" type="button" @click="emit('copy', selectedFullAfter)">
          复制{{ result?.actual ? '实际' : '预计' }}位置
        </button>
      </div>
    </div>
  </section>
</template>
