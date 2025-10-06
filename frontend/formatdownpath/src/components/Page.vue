<script setup lang="ts">
import { ref, reactive, onMounted, onUnmounted } from 'vue'

interface TorrentItem {
  hash: string
  name: string
  save_path: string
  files_count: number
  downloader: string
}

const props = defineProps({
  api: {
    type: Object,
    required: true,
  },
  initialConfig: {
    type: Object,
    default: () => ({}),
  }
})

const emit = defineEmits(['close', 'switch', 'save'])

// 通知状态
const snackbar = reactive({
  show: false,
  text: '',
  color: 'success',
  timeout: 3000,
})

// 显示通知
function showNotification(text, color = 'success') {
  snackbar.text = text
  snackbar.color = color
  snackbar.show = true
}

// 分页配置
const pageSize = ref(10)
const currentPage = ref(1)
const allHashes = ref<string[]>([])
const torrentList = ref<TorrentItem[]>([])
const loading = ref(false)
const hasMore = ref(true)
const error = ref<string | null>(null)

// 处理后的数据存储
const processedData = ref<Record<string, string>>({})

// 滚动节流控制
let isScrolling = false

// 获取基础数据
const fetchProcessedData = async () => {
  try {
    error.value = null
    const response = await props.api.get(`plugin/FormatDownPath/processed_data`)

    processedData.value = response
    allHashes.value = Object.keys(response)
    console.log('成功加载基础数据，共', allHashes.value.length, '条记录')

    // 重置分页状态
    currentPage.value = 1
    torrentList.value = []
    hasMore.value = true

  } catch (err) {
    console.error('获取基础数据失败:', err)
    error.value = '无法加载基础数据，请检查网络连接'
    hasMore.value = false
  }
}

// 校验 TorrentItem 数据是否合法
const isValidTorrentItem = (item: any): item is TorrentItem => {
  return (
    typeof item.hash === 'string' &&
    typeof item.name === 'string' &&
    typeof item.save_path === 'string' &&
    typeof item.files_count === 'number'
  );
};

// 获取单个种子数据并校验
const fetchAndValidateTorrent = async (hash: string, processedData: Record<string, string>, api: any) => {
  try {
    const data = await api.get(`plugin/FormatDownPath/torrent_data?torrent_hash=${hash}`)
    const torrentItem = {
      ...data,
      hash,
      downloader: processedData[hash] || '未知下载器'
    };
    return isValidTorrentItem(torrentItem) ? torrentItem : null;
  } catch (e) {
    console.error(`加载种子数据失败 (${hash}):`, e)
    return null;
  }
};

// 加载分页数据
const loadData = async () => {
  if (loading.value || !hasMore.value) return

  loading.value = true
  try {
    const start = (currentPage.value - 1) * pageSize.value
    const end = start + pageSize.value
    const currentHashes = allHashes.value.slice(start, end)

    if (!currentHashes.length) {
      hasMore.value = false
      return
    }

    const results = (await Promise.all(
      currentHashes.map(hash =>
        fetchAndValidateTorrent(hash, processedData.value, props.api)
      )
    )).filter(Boolean) as TorrentItem[]

    torrentList.value.push(...results)
    currentPage.value++
    hasMore.value = end < allHashes.value.length

    if (!results.length && currentPage.value === 2) {
      hasMore.value = false
      console.error('没有更多数据了')
    }

  } catch (err) {
    error.value = '数据加载失败，请尝试刷新页面'
    console.error('分页加载异常:', err)
  } finally {
    loading.value = false
  }
}

// 恢复种子处理
const recoverFromHistory = async (hash: string, downloader: string) => {
  try {
    loading.value = true
    const [success, message]: [Boolean, string] = await props.api.post(
      `plugin/FormatDownPath/recover_from_history`, {downloader: downloader, torrent_hash: hash})
    if (success) {
      showNotification(message)
      // 本地状态更新
      torrentList.value = torrentList.value.filter(t => t.hash !== hash)
      delete processedData.value[hash]
      allHashes.value = allHashes.value.filter(h => h !== hash)
  } else {
      showNotification(message, 'error')
    }
    // 重新计算分页
    if (currentPage.value > 1) {
      await loadData()
    }

  } catch (err) {
    error.value = '恢复操作失败，请检查下载器状态'
    console.error('恢复异常:', err)
  } finally {
    loading.value = false
  }
}

// 滚动处理
const container = ref<HTMLElement>()

const handleScroll = () => {
  if (!container.value || isScrolling || loading.value) return

  const { scrollTop, scrollHeight, clientHeight } = container.value
  const scrollRemain = scrollHeight - (scrollTop + clientHeight)

  if (scrollRemain < 500) {
    loadData()
  }
}

// 生命周期管理
onMounted(async () => {
  await fetchProcessedData()
  await loadData()
  container.value?.addEventListener('scroll', handleScroll)
})

onUnmounted(() => {
  container.value?.removeEventListener('scroll', handleScroll)
})
</script>

<template>
  <!-- 外层容器 -->
  <div class="main-container">
    <!-- 滚动内容区 -->
    <div ref="container" class="scroll-content">
      <!-- 数据列表 -->
      <div v-if="torrentList.length" class="grid grid-cols-1 gap-6">
        <v-card
          v-for="(torrentInfo, index) in torrentList"
          :key="index"
          elevation="2"
          hover
          class="transition-all duration-300"
        >
          <v-card-title class="px-3 pt-2 pb-1">
            <div class="font-bold text-lg break-words leading-tight">
              {{ torrentInfo.name }}
            </div>
          </v-card-title>

          <div class="flex items-center justify-between px-3 pb-2 gap-2">
            <div class="flex flex-col flex-1">
              <div class="text-sm break-all">
                下载器: {{ torrentInfo.downloader }}
                原路径: {{ torrentInfo.save_path }}
              </div>
              <div class="text-sm">
                文件数: {{ torrentInfo.files_count }} | Hash: {{ torrentInfo.hash }}
              </div>
            </div>

            <v-btn
              color="primary"
              size="small"
              elevation="20"
              rounded="xl"
              @click="recoverFromHistory(torrentInfo.hash, torrentInfo.downloader)"
            >
              恢复
            </v-btn>
          </div>
        </v-card>
      </div>

      <!-- 加载状态 -->
      <div v-if="loading" class="text-center py-4">
        <v-progress-circular indeterminate color="primary" />
        <div class="mt-2 text-gray-600">正在加载更多数据...</div>
      </div>

      <!-- 空状态 -->
      <div v-else-if="!hasMore && torrentList.length === 0" class="text-center py-8">
        <v-icon size="48" color="grey">mdi-package-variant-remove</v-icon>
        <div class="mt-2 text-gray-600">未找到可恢复的备份数据</div>
      </div>
    </div>

    <!-- 固定底部操作栏 -->
    <v-footer class="footer-bar">
      <v-container class="d-flex flex-column">
        <!-- 提示信息行 -->
        <div class="d-flex align-center mb-2">
          <v-alert type="warning" variant="tonal" class="flex-grow-1">
            注意：插件重置后备份数据会被清除
          </v-alert>
          <v-alert v-if="error" type="error" class="mt-2 ml-2 flex-grow-1">
            {{ error }}
            <v-btn @click="fetchProcessedData" variant="text" size="small" class="ml-2">重试</v-btn>
          </v-alert>
        </div>

        <!-- 操作按钮行 -->
        <v-card-actions class="px-2 py-1 d-flex justify-space-between">
          <v-btn
            color="info"
            @click="emit('switch')"
            prepend-icon="mdi-view-dashboard"
            variant="text"
          >
            配置页
          </v-btn>
          <v-btn
            color="grey"
            @click="emit('close')"
            prepend-icon="mdi-close"
            variant="text"
          >
            关闭
          </v-btn>
        </v-card-actions>
      </v-container>
    </v-footer>
    <!-- 通知弹窗 -->
    <v-snackbar v-model="snackbar.show" :color="snackbar.color" :timeout="snackbar.timeout">
      {{ snackbar.text }}
      <template v-slot:actions>
        <v-btn variant="text" @click="snackbar.show = false"> 关闭 </v-btn>
      </template>
    </v-snackbar>
  </div>
</template>

<style scoped>
/* 外层容器 */
.main-container {
  height: 90vh;
  display: flex;
  flex-direction: column;
  overflow: hidden; /* 禁用滚动 */
}

/* 滚动区域 */
.scroll-content {
  height: 75vh;
  overflow-y: auto;
  padding: 16px;
}

/* 底部操作栏 */
.footer-bar {
  flex-shrink: 0;
  padding: 0 5px;
  display: flex;
  flex-direction: column;
}

/* 调整 v-container 样式 */
.v-footer .v-container {
  display: flex;
  flex-direction: column;
  justify-content: center; /* 垂直居中内容 */
  gap: 4px; /* 减少行间距 */
  padding: 4px 0; /* 减少内边距 */
}

/* 调整 v-alert 样式 */
.v-alert {
  margin: 0;
  padding: 4px 8px; /* 减少内边距 */
  font-size: 12px; /* 减小字体大小 */
  min-height: auto; /* 取消最小高度 */
}

/* 调整 v-card-actions 样式 */
.v-card-actions {
  padding: 2px 0; /* 减少内边距 */
}

/* 调整 v-btn 样式 */
.v-btn {
  font-size: 12px; /* 减小字体大小 */
  min-height: 24px; /* 减小按钮最小高度 */
  padding: 0 8px; /* 减少按钮内边距 */
}

.v-card {
  margin-bottom: 16px;
}
</style>