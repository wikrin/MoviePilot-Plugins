<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import {
  buildTorrentTree,
  CHANGE_LABELS,
  flattenVisibleTree,
  folderIds,
  OPERATION_LABELS,
} from '../utils/torrentTree.js'
import { formatSize } from '../api/torrentformatter.js'

const props = defineProps({
  snapshot: { type: Object, required: true },
  changes: { type: Array, default: () => [] },
  operations: { type: Array, default: () => [] },
  label: { type: String, default: '种子目录树' },
  onlyChanged: Boolean,
  search: { type: String, default: '' },
  selectedFileId: { type: [Number, String], default: null },
  actual: Boolean,
})
const emit = defineEmits(['select'])
const tree = computed(() =>
  buildTorrentTree(props.snapshot, props.changes, {
    onlyChanged: props.onlyChanged,
    search: props.search,
    operations: props.operations,
    actual: props.actual,
  }),
)
const expanded = ref(new Set(['root']))
const focused = ref('root')
const viewport = ref(null)
const scrollTop = ref(0)
const height = ref(320)
const rowHeight = 32
const rows = computed(() => flattenVisibleTree(tree.value, expanded.value))
const first = computed(() => Math.max(0, Math.floor(scrollTop.value / rowHeight) - 5))
const windowRows = computed(() => rows.value.slice(first.value, first.value + Math.ceil(height.value / rowHeight) + 10))
const afterSpace = computed(() => Math.max(0, rows.value.length - first.value - windowRows.value.length) * rowHeight)
let observer

watch(
  () => props.snapshot.hash,
  () => {
    expanded.value = new Set(props.snapshot.files.length <= 30 ? folderIds(tree.value) : ['root'])
    focused.value = 'root'
    scrollTop.value = 0
    if (viewport.value) viewport.value.scrollTop = 0
  },
  { immediate: true },
)
watch(rows, value => {
  if (!value.some(item => item.id === focused.value)) focused.value = 'root'
  const max = Math.max(0, value.length * rowHeight - height.value)
  if (scrollTop.value > max) {
    scrollTop.value = max
    if (viewport.value) viewport.value.scrollTop = max
  }
})

function toggle(node) {
  const next = new Set(expanded.value)
  if (next.has(node.id)) next.delete(node.id)
  else next.add(node.id)
  expanded.value = next
}
function activate(node) {
  focused.value = node.id
  if (node.type === 'folder') toggle(node)
  else emit('select', node.fileId)
}
async function focusNode(node) {
  if (!node || !viewport.value) return
  focused.value = node.id
  const index = rows.value.findIndex(item => item.id === node.id)
  if (index * rowHeight < scrollTop.value) scrollTop.value = index * rowHeight
  else if ((index + 1) * rowHeight > scrollTop.value + height.value)
    scrollTop.value = (index + 1) * rowHeight - height.value
  viewport.value.scrollTop = scrollTop.value
  await nextTick()
  Array.from(viewport.value.querySelectorAll('[data-tree-id]'))
    .find(item => item.dataset.treeId === node.id)
    ?.focus({ preventScroll: true })
}
function keyboard(event, node) {
  const index = rows.value.findIndex(item => item.id === node.id)
  if (!['ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight', 'Home', 'End', 'Enter', ' '].includes(event.key)) return
  event.preventDefault()
  if (event.key === 'ArrowDown') void focusNode(rows.value[Math.min(rows.value.length - 1, index + 1)])
  if (event.key === 'ArrowUp') void focusNode(rows.value[Math.max(0, index - 1)])
  if (event.key === 'Home') void focusNode(rows.value[0])
  if (event.key === 'End') void focusNode(rows.value.at(-1))
  if (event.key === 'ArrowRight' && node.type === 'folder') {
    if (!expanded.value.has(node.id)) toggle(node)
    else void focusNode(rows.value[index + 1])
  }
  if (event.key === 'ArrowLeft') {
    if (node.type === 'folder' && expanded.value.has(node.id)) toggle(node)
    else void focusNode(rows.value.find(item => item.id === node.parentId))
  }
  if (event.key === 'Enter' || event.key === ' ') activate(node)
}
function stateLabel(node) {
  if (node.operationStatus) return OPERATION_LABELS[node.operationStatus]
  if (node.status !== 'unchanged') return CHANGE_LABELS[node.status] || node.status
  return node.locationChanged ? '位置变化' : !node.selected ? '未选中' : ''
}
onMounted(() => {
  if (typeof ResizeObserver !== 'undefined') {
    observer = new ResizeObserver(([entry]) => {
      height.value = entry.contentRect.height || 320
    })
    observer.observe(viewport.value)
  }
})
onUnmounted(() => observer?.disconnect())
</script>

<template>
  <div class="tf-tree">
    <div class="tf-tree-tools">
      <span>{{ tree.count }} 个文件 · {{ tree.relatedCount }} 项相关</span>
      <div>
        <button type="button" @click="expanded = new Set(folderIds(tree))">展开</button
        ><button type="button" @click="expanded = new Set(['root'])">折叠</button>
      </div>
    </div>
    <div
      ref="viewport"
      class="tf-tree-scroll"
      role="tree"
      :aria-label="label"
      @scroll.passive="scrollTop = $event.target.scrollTop"
    >
      <div :style="{ blockSize: `${first * rowHeight}px` }" aria-hidden="true" />
      <button
        v-for="node in windowRows"
        :key="node.id"
        :data-tree-id="node.id"
        class="tf-tree-row"
        :class="{
          'tf-tree-row--root': node.id === 'root',
          'tf-tree-row--selected': node.type === 'file' && selectedFileId === node.fileId,
          'tf-tree-row--changed': node.changed || node.status === 'changed',
          'tf-tree-row--warning': ['skipped', 'unsupported'].includes(node.status),
          'tf-tree-row--error': node.status === 'invalid',
        }"
        :style="{
          paddingInlineStart: `${10 + node.depth * 16}px`,
          minInlineSize: `${Math.max(280, 150 + node.depth * 16)}px`,
        }"
        type="button"
        role="treeitem"
        :aria-level="node.depth + 1"
        :aria-expanded="node.type === 'folder' ? expanded.has(node.id) : undefined"
        :aria-selected="node.type === 'file' ? selectedFileId === node.fileId : undefined"
        :aria-posinset="node.position"
        :aria-setsize="node.siblingCount"
        :tabindex="focused === node.id ? 0 : -1"
        :title="`${node.path}${node.reason ? '\n' + node.reason : ''}`"
        @click="activate(node)"
        @focus="focused = node.id"
        @keydown="keyboard($event, node)"
      >
        <svg class="tf-tree-chevron" viewBox="0 0 16 16" aria-hidden="true">
          <path
            v-if="node.type === 'folder'"
            :d="expanded.has(node.id) ? 'm4 6 4 4 4-4' : 'm6 4 4 4-4 4'"
            fill="none"
            stroke="currentColor"
            stroke-width="1.5"
            stroke-linecap="round"
            stroke-linejoin="round"
          />
        </svg>
        <svg class="tf-tree-icon" viewBox="0 0 20 20" fill="none" aria-hidden="true">
          <path
            v-if="node.type === 'folder'"
            d="M2 4h6l2 2h8v10H2V4Z"
            stroke="currentColor"
            stroke-width="1.3"
            stroke-linejoin="round"
          />
          <path
            v-else
            d="M5 2h6l4 4v12H5V2Zm6 0v5h4M8 10h4M8 13h4"
            stroke="currentColor"
            stroke-width="1.3"
            stroke-linejoin="round"
          />
        </svg>
        <span class="tf-tree-name">{{ node.name }}</span>
        <span v-if="node.type === 'folder' && node.id !== 'root'" class="tf-tree-count">{{ node.count }}</span>
        <span v-else-if="node.type === 'file' && stateLabel(node)" class="tf-tree-state">{{ stateLabel(node) }}</span>
        <span v-if="node.type === 'file'" class="tf-tree-size">{{ formatSize(node.size) }}</span>
      </button>
      <div :style="{ blockSize: `${afterSpace}px` }" aria-hidden="true" />
      <div v-if="!tree.count" class="tf-tree-no-match">没有匹配的文件。试试取消筛选或修改路径关键词。</div>
    </div>
    <div class="tf-tree-caption">点击文件核对相对路径 · ↑↓ 导航 · ←→ 展开/折叠 · Home/End 跳转</div>
  </div>
</template>
