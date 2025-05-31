<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'

const props = defineProps({
  api: {
    type: Object,
    required: true,
  },
  initialConfig: {
    type: Object,
    default: () => ({})
  }
})

const emit = defineEmits(['close', 'save'])

// 插件默认配置
const defaultConfig = reactive({
  enabled: false,
  calc_time: 0,
  onlyonce: false,
  interval_minutes: 15,
  calname: '追剧日历',
  cron: ''
})

const config = reactive({ ...defaultConfig })

const isFormValid = ref(true)
const form = ref<{ resetValidation: () => void } | null>(null)
const error = ref<string | null>(null)
const successMessage = ref<string | null>(null)
const saving = ref(false)

// 系统设置项
const SystemSettings = ref<any>({
  // 基础设置
  Basic: {
    APP_DOMAIN: null,
    API_TOKEN: null,
  },
})

// 下载ics文件
async function downloadICS() {
  try {
    const response = await props.api.get('plugin/SubscribeCal/download/calendar.ics', {
      responseType: 'text',
    })
    console.log(response)
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
  }
}

// 复制Url
async function copyUrl() {
  try {
    let value = SystemSettings.value.Basic.APP_DOMAIN
    const token = SystemSettings.value.Basic.API_TOKEN

    // 校验必要字段
    if (!value) {
      showNotification('域名未配置', 'warning')
      return
    }

    // 构建 URL
    let baseUrl = value
    if (!baseUrl.startsWith('http://') && !baseUrl.startsWith('https://')) {
      baseUrl = 'http://' + baseUrl
    }
    const url = new URL(`${baseUrl}/plugin/SubscribeCal/subscribe`)
    url.searchParams.append('apikey', token)

    // 复制到剪贴板
    await navigator.clipboard.writeText(url.toString())
    showNotification('已复制到剪贴板！', 'success')
  } catch (err) {
    console.error('操作失败:', err)
    showNotification('复制失败，请检查浏览器权限或输入内容', 'error')
  }
}

// 加载系统设置
async function loadSystemSettings() {
  try {
    const result: { [key: string]: any } = await props.api.get('system/env')
    if (result.success) {
      // 将API返回的值赋值给SystemSettings
      for (const sectionKey of Object.keys(SystemSettings.value) as Array<keyof typeof SystemSettings.value>) {
        Object.keys(SystemSettings.value[sectionKey]).forEach((key: string) => {
          if (result.data.hasOwnProperty(key)) {
            (SystemSettings.value[sectionKey] as any)[key] = result.data[key]
          }
        })
      }
      // 判断 APP_DOMAIN 是否为空
      if (!SystemSettings.value.Basic.APP_DOMAIN) {
        SystemSettings.value.Basic.APP_DOMAIN = `${window.location.origin}`
      }
    }
  } catch (error) {
    console.log(error)
  }
}

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

// 保存配置
async function saveConfig() {
  saving.value = true
  error.value = null

  try {
    emit('save', { ...config })
    successMessage.value = '配置保存成功'
  } catch (err) {
    console.error('保存配置失败:', err)
    error.value = err.message || '保存配置失败'
  } finally {
    saving.value = false
  }
}

// 重置表单
function resetForm() {
  Object.keys(defaultConfig).forEach(key => {
    config[key] = defaultConfig[key]
  })

  if (form.value) {
    form.value.resetValidation()
  }
}

onMounted (() => {
  // 加载初始配置
  if (props.initialConfig) {
    Object.keys(props.initialConfig).forEach(key => {
      if (key in config) {
        config[key] = props.initialConfig[key]
      }
    })
  }
  // 加载系统设置
  loadSystemSettings()
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
                <v-col cols="12" md="4" class="pr-md-2 pb-2">
                  <div class="setting-item d-flex align-center justify-space-between pa-3 bg-grey-lighten-3 rounded">
                    <div class="d-flex align-center">
                      <v-icon icon="mdi-power" size="small" :color="config.enabled ? 'info' : 'grey'" class="mr-3" />
                      <span class="text-subtitle-2">启用插件</span>
                    </div>
                    <v-switch
                      v-model="config.enabled"
                      color="info"
                      inset
                      density="compact"
                      hide-details
                      class="small-switch"
                    />
                  </div>
                </v-col>
                <!--  -->
                <v-col cols="12" md="4" class="pr-md-2 pb-2">
                  <div class="setting-item d-flex align-center justify-space-between pa-3 bg-grey-lighten-3 rounded">
                    <div class="d-flex align-center">
                      <v-icon icon="mdi-history" size="small" :color="config.calc_time ? 'info' : 'grey'" class="mr-3" />
                      <span class="text-subtitle-2">根据下载记录补充时间</span>
                    </div>
                    <v-switch
                      v-model="config.calc_time"
                      color="info"
                      inset
                      density="compact"
                      hide-details
                      class="small-switch"
                    />
                  </div>
                </v-col>
                <v-col cols="12" md="4" class="pr-md-2 pb-2">
                  <div class="setting-item d-flex align-center justify-space-between pa-3 bg-grey-lighten-3 rounded">
                    <div class="d-flex align-center">
                      <v-icon icon="mdi-update" size="small" :color="config.enabled ? 'info' : 'grey'" class="mr-3" />
                      <span class="text-subtitle-2">立即更新一次</span>
                    </div>
                    <v-switch
                      v-model="config.onlyonce"
                      color="info"
                      inset
                      density="compact"
                      hide-details
                      class="small-switch"
                    />
                  </div>
                </v-col>
              </v-row>
              <!-- 文本填写区域 -->
              <v-row no-gutters>
                <v-col cols="12" md="4" class="pl-md-2 pb-2">
                  <div class="setting-item d-flex align-center justify-space-between pa-3 bg-grey-lighten-3 rounded">
                    <div class="d-flex align-center">
                      <span class="text-subtitle-2">日历名称</span>
                    </div>
                    <v-text-field
                      v-model.number="config.calname"
                      density="compact"
                      hide-details
                      style="max-width: 150px"
                    />
                  </div>
                </v-col>
                <v-col cols="12" md="4" class="pl-md-2 pb-2">
                  <div class="setting-item d-flex align-center justify-space-between pa-3 bg-grey-lighten-3 rounded">
                    <div class="d-flex align-center">
                      <span class="text-subtitle-2">时间取整间隔（分钟）</span>
                    </div>
                    <v-text-field
                      v-model.number="config.interval_minutes"
                      density="compact"
                      hide-details
                      style="max-width: 150px"
                    />
                  </div>
                </v-col>
                <v-col cols="12" md="4" class="pl-md-2 pb-2">
                  <div class="setting-item d-flex align-center justify-space-between pa-3 bg-grey-lighten-3 rounded">
                    <div class="d-flex align-center">
                      <span class="text-subtitle-2">数据更新周期</span>
                    </div>
                    <v-text-field
                      v-model.number="config.cron"
                      density="compact"
                      hide-details
                      placeholder="留空自动"
                      style="max-width: 150px"
                    />
                  </div>
                </v-col>
              </v-row>
            </v-card-text>
          </v-card>
          <!-- 下载与说明 -->
          <v-divider class="my-4" />
          <v-row>
            <v-col cols="4" md="2">
              <v-btn color="primary" :disabled="!isFormValid || saving" @click="downloadICS" :loading="saving" prepend-icon="mdi-download" variant="tonal">下载ICS文件</v-btn>
            </v-col>
            <v-col cols="20" md="10">
              <!-- 输入框与按钮行 -->
              <div class="d-flex align-center mb-2">
                <!-- 输入框 -->
                <v-text-field
                  v-model="SystemSettings.Basic.APP_DOMAIN"
                  label="iCal订阅链接"
                  density="compact"
                  placeholder="DOMAIN:PORT 或 IP:PORT"
                  hide-details
                  class="flex-grow-1"
                />
                <!-- 复制按钮 -->
                <v-btn
                  icon="mdi-content-copy"
                  size="small"
                  variant="text"
                  color="primary"
                  @click="copyUrl"
                />
                </div>
            </v-col>
          </v-row>

          <v-alert type="info" title="iCal订阅链接:">
            <div class="space-y-2">
              <ul class="list-disc pl-5 space-y-1">
                <li>链接包含API密钥，请妥善保管防止泄露⚠️⚠️</li>
                <li>将iCal链接添加到支持订阅的日历应用（如Outlook、Google Calendar等）</li>
                <li>服务需公网访问，请将域名替换为您的公网IP或域名</li>
              </ul>
            </div>
          </v-alert>

          <v-card-actions class="px-2 py-1">
            <v-spacer></v-spacer>
            <v-btn color="secondary" variant="text" @click="resetForm" prepend-icon="mdi-restore" size="small">恢复默认</v-btn>
            <v-btn color="primary" :disabled="!isFormValid || saving" @click="saveConfig" :loading="saving" prepend-icon="mdi-content-save" variant="text" size="small">保存配置</v-btn>
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