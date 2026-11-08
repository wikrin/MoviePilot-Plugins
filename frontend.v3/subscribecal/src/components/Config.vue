<script setup lang="ts">
import copy from 'copy-to-clipboard'
import { ref, reactive, computed, onMounted, type PropType } from 'vue'

type ConfigState = {
  enabled: boolean
  calc_time: number
  onlyonce: boolean
  interval_minutes: number
  calname: string
  cron: string
  dashboard_size: number
  gist_enabled: boolean
  gist_url: string
}

const props = defineProps({
  api: {
    type: Object,
    required: true,
  },
  initialConfig: {
    type: Object as PropType<Partial<ConfigState>>,
    default: () => ({})
  }
})

const emit = defineEmits(['close', 'save'])

// 插件默认配置
const defaultConfig: ConfigState = {
  enabled: false,
  calc_time: 0,
  onlyonce: false,
  interval_minutes: 15,
  calname: '追剧日历',
  cron: '',
  dashboard_size: 6,
  gist_enabled: false,
  gist_url: '',
}

const config = reactive<ConfigState>({ ...defaultConfig })
const linkType = ref<'public' | 'gist'>('public')
const gistStatus = reactive<{
  loaded: boolean
  available: boolean
  message: string
}>({
  loaded: false,
  available: false,
  message: '',
})
const gistUnavailableMessage = computed(() => {
  if (!gistStatus.loaded) return '正在检查 GitHub Token 和 Gist 权限'
  if (!gistStatus.available) return gistStatus.message || 'Gist 当前不可用'
  return ''
})

const isFormValid = ref(true)
const form = ref<{ resetValidation: () => void } | null>(null)
const error = ref<string | null>(null)
const successMessage = ref<string | null>(null)
const saving = ref(false)
const syncingGist = ref(false)
const downloadingICS = ref(false)
const publicDomain = ref('')
const publicUrl = ref('')
const loadedPublicDomain = ref('')

const selectedUrl = computed(() =>
  linkType.value === 'gist' ? config.gist_url : publicUrl.value,
)

const displayedLink = computed({
  get: () => linkType.value === 'gist' ? config.gist_url : publicDomain.value,
  set: (value: string) => {
    if (linkType.value === 'public') publicDomain.value = value
  },
})

// 下载ics文件
async function downloadICS() {
  downloadingICS.value = true
  try {
    const response = await props.api.get('plugin/SubscribeCal/download/calendar.ics', {
      responseType: 'text',
    })
    const blob = new Blob([response], { type: 'text/calendar' })
    const url = window.URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.setAttribute('download', `${config.calname}.ics`)
    document.body.appendChild(link)
    link.click()
    link.remove()

    showNotification('ICS 文件已成功下载', 'success')
  } catch (err) {
    console.error('ICS 文件下载失败:', err)
    showNotification('ICS 文件下载失败', 'error')
  } finally {
    downloadingICS.value = false
  }
}

async function copySelectedUrl() {
  try {
    let url = selectedUrl.value
    if (linkType.value === 'public' && publicDomain.value !== loadedPublicDomain.value) {
      const request = startConfigStatusRequest(publicDomain.value)
      void request.completion.catch((err: unknown) =>
        console.error('读取订阅日历配置状态失败:', err),
      )
      url = await request.publicUrl
    }
    if (!url) {
      showNotification('当前没有可复制的订阅地址', 'warning')
      return
    }
    if (copy(url)) showNotification('订阅地址已复制', 'success')
    else showNotification('复制订阅地址失败', 'error')
  } catch (err) {
    console.error('获取订阅链接失败:', err)
    showNotification(err instanceof Error ? err.message : '复制订阅地址失败', 'error')
  }
}

async function syncGist() {
  syncingGist.value = true
  try {
    const response = await props.api.post(
      'plugin/SubscribeCal/gist/sync',
      undefined,
      { feedback: 'silent' },
    )
    if (!response.success) {
      gistStatus.available = false
      gistStatus.message = response.message
      showNotification(response.message || 'Gist 同步失败。', 'error')
      return
    }
    config.gist_url = response.data
    gistStatus.available = true
    gistStatus.message = response.message
    showNotification(response.message || 'Gist 同步成功。', 'success')
  } catch (err) {
    console.error('Gist 同步请求失败:', err)
    gistStatus.available = false
    gistStatus.message = err instanceof Error ? err.message : 'Gist 同步失败。'
    showNotification(gistStatus.message, 'error')
  } finally {
    syncingGist.value = false
  }
}

function startConfigStatusRequest(
  domain?: string,
  checkGist = config.gist_enabled,
  includeConfig = true,
) {
  let receivedText = ''
  let pendingLine = ''
  let configReceived = false
  let resolvePublicUrl: (url: string) => void = () => { }
  const publicUrlReady = new Promise<string>(resolve => {
    resolvePublicUrl = resolve
  })

  gistStatus.loaded = false
  gistStatus.available = false
  gistStatus.message = ''

  function applyRecord(line: string) {
    if (!line.trim()) return
    try {
      const record = JSON.parse(line) as {
        type?: string
        data?: Record<string, unknown>
      }
      if (record.type === 'config' && record.data && includeConfig) {
        const data = record.data
        publicDomain.value = typeof data.public_domain === 'string'
          ? data.public_domain
          : window.location.origin
        publicUrl.value = typeof data.public_url === 'string' ? data.public_url : ''
        loadedPublicDomain.value = publicDomain.value
        if (typeof data.gist_url === 'string' && data.gist_url) config.gist_url = data.gist_url
        configReceived = true
        resolvePublicUrl(publicUrl.value)
      } else if (record.type === 'gist' && record.data) {
        gistStatus.loaded = true
        gistStatus.available = record.data.available === true
        gistStatus.message = typeof record.data.message === 'string'
          ? record.data.message
          : '无法确认 GitHub Gist 权限。'
      }
    } catch (err) {
      console.error('解析订阅日历状态流失败:', err)
      gistStatus.loaded = true
      gistStatus.available = false
      gistStatus.message = '状态响应格式无效。'
    }
  }

  function consumeResponseText(text: string) {
    if (text.length < receivedText.length) return
    pendingLine += text.slice(receivedText.length)
    receivedText = text
    const lines = pendingLine.split('\n')
    pendingLine = lines.pop() ?? ''
    lines.forEach(applyRecord)
  }

  const completion = props.api.get('plugin/SubscribeCal/config/status', {
    params: { ...(domain ? { domain } : {}), check_gist: checkGist },
    responseType: 'text',
    feedback: 'silent',
    onDownloadProgress: (progressEvent: { event?: ProgressEvent }) => {
      const request = progressEvent.event?.target as XMLHttpRequest | null
      if (request && typeof request.responseText === 'string') {
        consumeResponseText(request.responseText)
      }
    },
  }).then((response: unknown) => {
    if (typeof response === 'string') consumeResponseText(response)
    if (pendingLine.trim()) applyRecord(pendingLine)
    if (!configReceived) resolvePublicUrl('')
  }).catch((err: unknown) => {
    if (!configReceived) resolvePublicUrl('')
    gistStatus.loaded = true
    gistStatus.available = false
    gistStatus.message = err instanceof Error ? err.message : '无法读取订阅日历配置。'
    throw err
  })

  return { publicUrl: publicUrlReady, completion }
}

async function loadConfigStatus() {
  const currentOrigin = window.location.origin
  publicDomain.value = currentOrigin
  try {
    await startConfigStatusRequest(currentOrigin, config.gist_enabled).completion
  } catch (err) {
    console.error('读取订阅日历配置状态失败:', err)
  }
}

function onGistEnabledChange(enabled: boolean | null) {
  if (enabled !== true) {
    gistStatus.loaded = true
    gistStatus.available = false
    gistStatus.message = 'Gist 同步未启用。'
    return
  }

  void startConfigStatusRequest(undefined, true, false).completion.catch((err: unknown) =>
    console.error('读取订阅日历 Gist 状态失败:', err),
  )
}

// 通知状态
const snackbar = reactive({
  show: false,
  text: '',
  color: 'success',
  timeout: 3000,
})

// 显示通知
function showNotification(text: string, color = 'success') {
  snackbar.text = text
  snackbar.color = color
  snackbar.show = true
}

async function saveConfig() {
  saving.value = true
  error.value = null

  try {
    emit('save', { ...config })
    successMessage.value = '配置保存成功'
  } catch (err) {
    console.error('保存配置失败:', err)
    error.value = err instanceof Error ? err.message : '保存配置失败'
  } finally {
    saving.value = false
  }
}

// 重置表单
function resetForm() {
  Object.assign(config, defaultConfig)

  if (form.value) {
    form.value.resetValidation()
  }
}

onMounted(() => {
  // 加载初始配置
  Object.assign(config, props.initialConfig)
  loadConfigStatus()
})
</script>

<template>
  <div class="plugin-config">
    <v-card flat class="rounded border">
      <!-- 标题区域 -->
      <v-card-title class="text-subtitle-1 d-flex align-center px-3 py-2 bg-primary-lighten-5">
        <v-icon icon="mdi-cog" class="mr-2" color="primary" size="small" />
        <span>订阅日历配置</span>
      </v-card-title>
      <v-card-text class="px-3 py-2">
        <v-form ref="form" v-model="isFormValid" @submit.prevent="saveConfig">
          <!-- 新增的选项卡组件 -->
          <v-card flat class="rounded mb-3 border config-card">
            <v-card-text class="px-3 py-2">
              <v-row dense no-gutters>
                <!-- 插件开关 -->
                <v-col cols="12" md="3" class="pr-md-2 pb-2">
                  <div class="setting-item d-flex align-center justify-space-between pa-3 bg-grey-lighten-3 rounded">
                    <div class="d-flex align-center">
                      <v-icon icon="mdi-power" size="small" :color="config.enabled ? 'info' : 'grey'" class="mr-3" />
                      <span class="text-subtitle-2">启用插件</span>
                    </div>
                    <v-switch v-model="config.enabled" color="info" inset density="compact" hide-details
                      class="small-switch" />
                  </div>
                </v-col>
                <v-col cols="12" md="3" class="pr-md-2 pb-2">
                  <div class="setting-item d-flex align-center justify-space-between pa-3 bg-grey-lighten-3 rounded">
                    <div class="d-flex align-center">
                      <v-icon icon="mdi-github" size="small" :color="config.gist_enabled ? 'info' : 'grey'"
                        class="mr-3" />
                      <span class="text-subtitle-2">同步到 Gist</span>
                    </div>
                    <v-switch v-model="config.gist_enabled" @update:model-value="onGistEnabledChange" color="info" inset
                      density="compact" hide-details class="small-switch" :disabled="!config.enabled" />
                  </div>
                </v-col>
                <v-col cols="12" md="3" class="pr-md-2 pb-2">
                  <div class="setting-item d-flex align-center justify-space-between pa-3 bg-grey-lighten-3 rounded">
                    <div class="d-flex align-center">
                      <v-icon icon="mdi-history" size="small" :color="config.calc_time ? 'info' : 'grey'"
                        class="mr-3" />
                      <span class="text-subtitle-2">下载记录补时</span>
                    </div>
                    <v-switch v-model="config.calc_time" color="info" inset density="compact" hide-details
                      class="small-switch" />
                  </div>
                </v-col>
                <v-col cols="12" md="3" class="pr-md-2 pb-2">
                  <div class="setting-item d-flex align-center justify-space-between pa-3 bg-grey-lighten-3 rounded">
                    <div class="d-flex align-center">
                      <v-icon icon="mdi-update" size="small" :color="config.enabled ? 'info' : 'grey'" class="mr-3" />
                      <span class="text-subtitle-2">运行一次</span>
                    </div>
                    <v-switch v-model="config.onlyonce" color="info" inset density="compact" hide-details
                      class="small-switch" />
                  </div>
                </v-col>
              </v-row>
              <!-- 文本填写区域 -->
              <v-row no-gutters>
                <v-col cols="12" md="3" class="pl-md-2 pb-2">
                  <div class="setting-item d-flex align-center justify-space-between pa-3 bg-grey-lighten-3 rounded">
                    <div class="d-flex align-center">
                      <span class="text-subtitle-2">日历名称</span>
                    </div>
                    <v-text-field v-model.number="config.calname" density="compact" hide-details
                      style="max-width: 150px" />
                  </div>
                </v-col>
                <v-col cols="12" md="3" class="pl-md-2 pb-2">
                  <div class="setting-item d-flex align-center justify-space-between pa-3 bg-grey-lighten-3 rounded">
                    <div class="d-flex align-center">
                      <span class="text-subtitle-2">时间取整间隔(分钟)</span>
                    </div>
                    <v-text-field v-model.number="config.interval_minutes" density="compact" hide-details
                      style="max-width: 150px" />
                  </div>
                </v-col>
                <v-col cols="12" md="3" class="pl-md-2 pb-2">
                  <div class="setting-item d-flex align-center justify-space-between pa-3 bg-grey-lighten-3 rounded">
                    <div class="d-flex align-center">
                      <span class="text-subtitle-2">运行周期</span>
                    </div>
                    <v-text-field v-model.number="config.cron" density="compact" hide-details placeholder="留空自动"
                      style="max-width: 150px" />
                  </div>
                </v-col>
                <v-col cols="12" md="3" class="pl-md-2 pb-2">
                  <div class="setting-item d-flex align-center justify-space-between pa-3 bg-grey-lighten-3 rounded">
                    <div class="d-flex align-center">
                      <span class="text-subtitle-2">仪表盘宽度</span>
                    </div>
                    <v-text-field v-model.number="config.dashboard_size" density="compact" hide-details
                      placeholder="留空自动" />
                  </div>
                </v-col>
              </v-row>
            </v-card-text>
          </v-card>
          <!-- 订阅链接与下载 -->
          <v-divider class="my-4" />
          <v-row dense align="center">
            <v-col cols="12" sm="3">
              <v-btn-toggle v-model="linkType" mandatory density="compact" color="primary" divided aria-label="订阅链接类型">
                <v-btn value="public" prepend-icon="mdi-api">API</v-btn>
                <v-btn value="gist" prepend-icon="mdi-github">Gist</v-btn>
              </v-btn-toggle>
            </v-col>
            <v-col cols="12" sm="6">
              <v-text-field v-model="displayedLink" :label="linkType === 'gist' ? 'Gist ICS 订阅地址' : 'iCal API 订阅域名'"
                density="compact" :placeholder="linkType === 'gist' ? '' : 'DOMAIN:PORT 或 IP:PORT'" hide-details
                :readonly="linkType === 'gist'">
                <template #append-inner>
                  <v-tooltip v-if="linkType === 'gist'" :text="gistUnavailableMessage || '上传'">
                    <template #activator="{ props: tooltipProps }">
                      <span v-bind="tooltipProps">
                        <v-btn icon="mdi-upload" size="small" variant="text" color="primary"
                          :disabled="!config.gist_enabled || !gistStatus.loaded || !gistStatus.available || syncingGist"
                          :loading="syncingGist" aria-label="上传到 Gist" @click.stop="syncGist" />
                      </span>
                    </template>
                  </v-tooltip>
                  <v-tooltip text="复制">
                    <template #activator="{ props: tooltipProps }">
                      <span v-bind="tooltipProps">
                        <v-btn icon="mdi-content-copy" size="small" variant="text" color="primary"
                          :disabled="!selectedUrl" aria-label="复制当前订阅地址" @click.stop="copySelectedUrl" />
                      </span>
                    </template>
                  </v-tooltip>
                </template>
              </v-text-field>
            </v-col>
            <v-col cols="12" sm="3" class="d-flex align-center justify-end ga-2">
              <v-btn color="primary" variant="tonal" prepend-icon="mdi-download"
                :disabled="!isFormValid || downloadingICS" :loading="downloadingICS" @click="downloadICS">下载 ICS</v-btn>
            </v-col>
          </v-row>
          <v-alert type="info" title="iCal 订阅链接说明" variant="tonal" class="mt-2">
            <div class="d-flex flex-column ga-1">
              <div>订阅链接包含 API 密钥，请妥善保管，防止泄露。</div>
              <div>可将 iCal 链接添加到 Outlook、Google Calendar 等支持订阅的日历应用。</div>
              <div>服务需公网访问，请使用公网 IP 或域名。</div>
              <div>使用 Gist 需主项目 GitHub Token 具备 gist 权限；开启同步并点击同步后，即可切换到 Gist 复制地址。</div>
              <div>Gist 为 Secret 状态，不会公开列出，但持有链接者仍可访问，请勿公开分享。</div>
            </div>
          </v-alert>

          <v-card-actions class="px-2 py-1">
            <v-spacer></v-spacer>
            <v-btn color="secondary" variant="text" @click="resetForm" prepend-icon="mdi-restore"
              size="small">恢复默认</v-btn>
            <v-btn color="primary" :disabled="!isFormValid || saving" @click="saveConfig" :loading="saving"
              prepend-icon="mdi-content-save" variant="text" size="small">保存配置</v-btn>
            <v-btn color="grey" @click="emit('close')" prepend-icon="mdi-close" variant="text" size="small">关闭</v-btn>
          </v-card-actions>
        </v-form>
      </v-card-text>
      <!-- 通知弹窗 -->
      <v-snackbar v-model="snackbar.show" :color="snackbar.color" :timeout="snackbar.timeout">
        {{ snackbar.text }}
        <template v-slot:actions>
          <v-btn variant="text" @click="snackbar.show = false"> 关闭 </v-btn>
        </template>
      </v-snackbar>
    </v-card>
  </div>
</template>

<style scoped>
/* 插件配置容器样式 */
.plugin-config {
  max-width: 80rem;
  margin: 0 auto;
  padding: 0.5rem;
}

/* 主色浅化 5 级的背景颜色样式 */
.bg-primary-lighten-5 {
  background-color: rgba(var(--v-theme-primary), 0.07);
}

/* 通用边框样式 */
.border {
  border: thin solid rgba(var(--v-border-color), var(--v-border-opacity));
}

/* 设置项样式 */
.setting-item {
  border-radius: 8px;
  transition: all 0.2s ease;
  padding: 0.5rem;
  height: 100%;
  display: flex;
  align-items: center;
}

/* 设置项悬停样式 */
.setting-item:hover {
  background-color: rgba(var(--v-theme-primary), 0.03);
}

/* 确保所有列的高度一致 */
.v-col {
  display: flex;
  align-items: stretch;
}

/* 小开关组件样式，调整大小和位置 */
.small-switch {
  transform: scale(0.8);
  margin-right: -8px;
  flex-shrink: 0;
}

/* 配置卡片样式 */
.config-card {
  /* 背景渐变，包含主背景和重复渐变效果 */
  background-image: linear-gradient(to right, rgba(var(--v-theme-surface), 0.98), rgba(var(--v-theme-surface), 0.95)),
    repeating-linear-gradient(45deg, rgba(var(--v-theme-primary), 0.03), rgba(var(--v-theme-primary), 0.03) 10px, transparent 10px, transparent 20px);
  background-attachment: fixed;
  box-shadow: 0 1px 2px rgba(var(--v-border-color), 0.05) !important;
  transition: all 0.3s ease;
}

/* 配置卡片悬停样式 */
.config-card:hover {
  box-shadow: 0 3px 6px rgba(var(--v-border-color), 0.1) !important;
}

/* 副标题 2 文本样式 */
.text-subtitle-2 {
  font-size: 0.875rem !important;
  font-weight: 500;
  white-space: nowrap;
  margin-right: 0.5rem;
}
</style>