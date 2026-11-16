<script setup>
import { computed, defineAsyncComponent, ref, shallowRef, watch } from 'vue'
import { useTheme } from 'vuetify'
import Config from './components/Config.vue'
import Page from './components/Page.vue'
import { createMockApi } from './dev/mockApi.js'
import { loadFederatedView } from './dev/loadFederation.js'

const remoteEntry = new URLSearchParams(window.location.search).get('remote')
const pageComponent = remoteEntry ? defineAsyncComponent(() => loadFederatedView(remoteEntry, 'Page')) : Page
const configComponent = remoteEntry ? defineAsyncComponent(() => loadFederatedView(remoteEntry, 'Config')) : Config

const api = shallowRef(createMockApi())
const config = ref(api.value.getConfig())
const mode = ref('page')
const themeName = ref('light')
const scenario = ref('normal')
const version = ref(0)
const theme = useTheme()
const notice = ref('')
const themeOptions = [
  { title: '浅色', value: 'light' },
  { title: '深色', value: 'dark' },
  { title: '透明', value: 'transparent' },
]
const caseOptions = [
  { title: '正常场景', value: 'normal' },
  { title: '部分失败', value: 'partial' },
  { title: '移动确认中', value: 'moving' },
  { title: '下载器离线', value: 'offline' },
  { title: '1000 文件种子', value: 'large' },
  { title: '历史种子已删除', value: 'history-missing' },
  { title: '历史已转种', value: 'history-transferred' },
  { title: '批量删除混合结果', value: 'history-batch-mixed' },
  { title: '删除后响应丢失', value: 'history-response-lost' },
]
const demoStyle = computed(() =>
  themeName.value === 'transparent'
    ? {
        '--app-grouped-list-background': 'rgba(var(--v-theme-surface), .76)',
        '--app-grouped-list-backdrop-filter': 'blur(16px)',
        '--app-surface-border': '1px solid rgba(var(--v-theme-on-surface), .12)',
      }
    : {},
)
watch(themeName, value => {
  theme.global.name.value = value === 'dark' ? 'dark' : 'light'
})
watch(scenario, value => {
  api.value.setScenario(value)
  version.value++
  mode.value = 'page'
})
function reset() {
  api.value = createMockApi()
  api.value.setScenario(scenario.value)
  config.value = api.value.getConfig()
  version.value++
  mode.value = 'page'
  notice.value = ''
}
function save(value) {
  api.value.setConfig(value)
  config.value = value
  notice.value = '演示配置已保存到内存，单次临时规则不会修改它。'
}
</script>

<template>
  <v-app>
    <v-main>
      <div class="tf-demo" :class="{ 'tf-demo--glass': themeName === 'transparent' }" :style="demoStyle">
        <div class="tf-demo-toolbar">
          <div><strong>本地安全演示</strong><span>仅操作内存示例，不连接 MoviePilot 或任何下载器。</span></div>
          <div class="tf-demo-controls">
            <v-select
              v-model="themeName"
              :items="themeOptions"
              label="演示主题"
              aria-label="演示主题"
              density="compact"
              variant="outlined"
              hide-details
            /><v-select
              v-model="scenario"
              :items="caseOptions"
              label="演示场景"
              aria-label="演示场景"
              density="compact"
              variant="outlined"
              hide-details
            /><v-btn variant="tonal" size="small" @click="reset">重置示例</v-btn>
          </div>
        </div>
        <v-alert
          v-if="notice"
          type="success"
          variant="tonal"
          density="compact"
          class="mb-3"
          closable
          @click:close="notice = ''"
          >{{ notice }}</v-alert
        >
        <v-card class="tf-demo-surface" elevation="0">
          <component
            :is="pageComponent"
            v-if="mode === 'page'"
            :key="version"
            :api="api"
            @close="mode = 'closed'"
            @switch="mode = 'config'"
          />
          <component
            :is="configComponent"
            v-else-if="mode === 'config'"
            :api="api"
            :initial-config="config"
            @close="mode = 'closed'"
            @switch="mode = 'page'"
            @save="save"
          />
          <div v-else class="tf-demo-closed">
            <h2>演示工作台已关闭</h2>
            <p>这里不是真实插件宿主，所有数据都只在当前页面内存中。</p>
            <v-btn color="primary" variant="tonal" @click="mode = 'page'">重新打开</v-btn>
          </div>
        </v-card>
      </div>
    </v-main>
  </v-app>
</template>

<style scoped>
.tf-demo {
  min-height: 100dvh;
  padding: 24px;
  background: rgba(var(--v-theme-on-surface), 0.035);
}

.tf-demo--glass {
  background:
    radial-gradient(at 10% 0%, rgba(var(--v-theme-primary), 0.14), transparent 55%),
    radial-gradient(at 85% 85%, rgba(var(--v-theme-info), 0.13), transparent 45%);
}

.tf-demo-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 16px;
  max-width: 96rem;
  margin: 0 auto 16px;
}

.tf-demo-toolbar strong {
  display: block;
  font-size: 13px;
  color: rgb(var(--v-theme-primary));
}

.tf-demo-toolbar span {
  display: block;
  margin-top: 4px;
  font-size: 11px;
  color: rgba(var(--v-theme-on-surface), 0.6);
}

.tf-demo-controls {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
}

.tf-demo-controls > :first-child {
  width: 105px;
}

.tf-demo-controls > :nth-child(2) {
  width: 165px;
}

.tf-demo-surface {
  max-width: 96rem;
  margin-inline: auto;
  border: 1px solid rgba(var(--v-theme-on-surface), 0.1);
  border-radius: 18px;
  background: var(--app-grouped-list-background, rgb(var(--v-theme-surface)));
}

.tf-demo-closed {
  padding: 60px 20px;
  text-align: center;
}

.tf-demo-closed p {
  margin: 12px 0 20px;
  font-size: 13px;
}

@media (max-width: 600px) {
  .tf-demo {
    padding: 12px;
  }

  .tf-demo-controls {
    width: 100%;
  }

  .tf-demo-surface {
    border-radius: 12px;
  }
}
</style>
