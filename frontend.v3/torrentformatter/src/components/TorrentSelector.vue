<script setup>
import { computed } from 'vue'
import { formatSize, shortHash } from '../api/torrentformatter.js'
import { PAGE_SIZE_OPTIONS } from '../composables/useTorrentFormatter.js'

const props = defineProps({
  downloaders: { type: Array, default: () => [] },
  downloader: { type: String, default: '' },
  query: { type: String, default: '' },
  items: { type: Array, default: () => [] },
  total: { type: Number, default: 0 },
  page: { type: Number, default: 1 },
  pageSize: { type: Number, default: 25 },
  selectedHash: { type: String, default: '' },
  loading: Boolean,
  busy: Boolean,
  error: { type: String, default: '' },
})
const emit = defineEmits(['update:downloader', 'update:query', 'update:page', 'update:pageSize', 'select', 'refresh'])
const pages = computed(() => Math.max(1, Math.ceil(props.total / props.pageSize)))
const rangeStart = computed(() => (props.total ? (props.page - 1) * props.pageSize + 1 : 0))
const rangeEnd = computed(() => Math.min(props.page * props.pageSize, props.total))
const options = computed(() =>
  props.downloaders.map(item => ({
    title: `${item.name}${item.type === 'transmission' ? ' · Transmission' : item.type === 'qbittorrent' ? ' · qBittorrent' : ''}`,
    value: item.name,
  })),
)
</script>

<template>
  <section>
    <div class="tf-heading">
      <div>
        <span class="tf-eyebrow">01 / SELECT A TORRENT</span>
        <h2>选择要处理的种子</h2>
        <p>选择一个种子，核对后进入媒体信息。选择其他种子会替换当前目标，清空媒体与临时规则，不会累计多选。</p>
      </div>
    </div>
    <v-alert v-if="error" type="error" variant="tonal" density="compact" class="tf-inline-alert"
      ><span>{{ error }}</span
      ><v-btn size="small" variant="text" @click="emit('refresh')">重试</v-btn></v-alert
    >
    <div class="tf-card">
      <div class="tf-toolbar">
        <v-select
          class="tf-downloader-field"
          :model-value="downloader"
          :items="options"
          label="下载器"
          placeholder="请选择下载器"
          :disabled="busy || !options.length"
          variant="outlined"
          density="compact"
          hide-details
          @update:model-value="emit('update:downloader', $event || '')"
        />
        <v-text-field
          class="tf-search-field"
          :model-value="query"
          label="搜索名称或 Hash"
          prepend-inner-icon="mdi-magnify"
          :disabled="busy || !downloader"
          maxlength="200"
          clearable
          variant="outlined"
          density="compact"
          hide-details
          @update:model-value="emit('update:query', $event || '')"
        />
      </div>
      <v-progress-linear v-if="loading" color="primary" indeterminate aria-label="正在加载种子" />
      <div v-if="items.length" class="tf-torrent-list" aria-label="种子列表">
        <button
          v-for="item in items"
          :key="item.hash"
          class="tf-torrent-row"
          :class="{ 'tf-torrent-row--selected': selectedHash === item.hash }"
          type="button"
          :aria-label="`选择此种子 ${item.name}`"
          :aria-pressed="selectedHash === item.hash"
          :disabled="busy"
          @click="emit('select', item)"
        >
          <span class="tf-torrent-copy">
            <span class="tf-torrent-title">
              <strong :title="item.name">{{ item.name }}</strong>
              <v-chip
                v-if="item.processed"
                size="x-small"
                color="warning"
                variant="tonal"
                prepend-icon="mdi-backup-restore"
                >需先恢复</v-chip
              >
            </span>
            <span class="tf-torrent-meta">
              <span class="tf-torrent-meta-item tf-row-size">
                <v-icon icon="mdi-database-outline" size="14" aria-hidden="true" />
                {{ formatSize(item.total_size) }}
              </span>
              <span v-if="item.state" class="tf-torrent-meta-item tf-torrent-state">
                <v-icon icon="mdi-download-outline" size="14" aria-hidden="true" />
                {{ item.state }}
              </span>
              <span v-if="item.tags?.length" class="tf-torrent-meta-item tf-torrent-tags" aria-label="种子标签">
                <v-icon icon="mdi-tag-multiple-outline" size="14" aria-hidden="true" />
                <v-chip v-for="tag in item.tags" :key="tag" size="x-small" variant="tonal">{{ tag }}</v-chip>
              </span>
            </span>
            <span class="tf-torrent-location">
              <span class="tf-torrent-path" :title="item.save_path">{{ item.save_path }}</span>
              <span class="tf-mono tf-muted" :title="item.hash">{{ shortHash(item.hash) }}</span>
            </span>
          </span>
          >
        </button>
      </div>
      <div v-else-if="!loading" class="tf-empty">
        <div class="tf-empty-icon">
          <v-icon :icon="!downloaders.length ? 'mdi-download-off-outline' : 'mdi-file-search-outline'" size="26" />
        </div>
        <h3>
          {{
            !downloaders.length
              ? '还没有可用下载器'
              : !downloader
                ? '请先选择下载器'
                : query
                  ? '没有找到匹配的种子'
                  : '下载器中暂无种子'
          }}
        </h3>
        <p>
          {{
            !downloaders.length
              ? '请在 MoviePilot 设置中启用 qBittorrent 或 Transmission，然后刷新工作台。'
              : !downloader
                ? '选择下载器后才会加载种子列表。'
                : query
                  ? '尝试更短的名称，或使用完整Hash 搜索。'
                  : '添加种子后刷新即可选择，无需开启本插件的定时或事件处理。'
          }}
        </p>
      </div>
      <div class="tf-pagination tf-page-controls">
        <div class="tf-page-summary">
          <span>{{ rangeStart }} - {{ rangeEnd }} / {{ total }} 个种子</span>
          <v-select
            :model-value="pageSize"
            :items="PAGE_SIZE_OPTIONS"
            aria-label="每页种子数量"
            density="compact"
            variant="outlined"
            hide-details
            :disabled="busy || loading || !downloader"
            @update:model-value="emit('update:pageSize', Number($event))"
          />
        </div>
        <v-pagination
          :model-value="page"
          :length="pages"
          :total-visible="5"
          density="comfortable"
          :disabled="busy || loading || !downloader"
          aria-label="种子分页"
          @update:model-value="emit('update:page', $event)"
        />
      </div>
    </div>
  </section>
</template>
