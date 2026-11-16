<script setup>
import { nextTick, onMounted, onUnmounted, ref } from 'vue'
import '../styles/formatter.css'

defineProps({
  title: { type: String, default: '种子格式化' },
  subtitle: { type: String, default: '工作台' },
  badge: { type: String, default: 'PREVIEW FIRST' },
  section: { type: String, default: '' },
  sections: { type: Array, default: () => [] },
  busy: Boolean,
  showSwitch: { type: Boolean, default: true },
})
const emit = defineEmits(['update:section', 'close', 'switch', 'refresh'])
const root = ref(null)
const layout = ref(null)
const paginationHost = ref(null)
const mobile = ref(false)
const availableHeight = ref('90dvh')
let observer
function measureHeight() {
  if (root.value) {
    availableHeight.value = `${Math.max(320, window.innerHeight - Math.max(0, root.value.getBoundingClientRect().top) - 12)}px`
    mobile.value = root.value.getBoundingClientRect().width < 720
  }
}
onMounted(async () => {
  await nextTick()
  measureHeight()
  window.addEventListener('resize', measureHeight)
  if (typeof ResizeObserver !== 'undefined') {
    observer = new ResizeObserver(measureHeight)
    observer.observe(root.value)
  }
})
onUnmounted(() => {
  window.removeEventListener('resize', measureHeight)
  observer?.disconnect()
})
</script>

<template>
  <section ref="root" class="tf-shell" :style="{ '--tf-available-height': availableHeight }">
    <div class="tf-frame" :class="{ 'tf-frame--history': section === 'history' }">
      <header class="tf-header">
        <div class="tf-brand">
          <div class="tf-brand-mark" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none">
              <path d="M4 5h6l2 3h8v11H4V5Z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" />
              <path
                d="m9 13 2 2 4-4"
                stroke="currentColor"
                stroke-width="1.7"
                stroke-linecap="round"
                stroke-linejoin="round"
              />
            </svg>
          </div>
          <div>
            <div class="tf-crumb">MoviePilot <span>/</span> 插件 <span>/</span> {{ subtitle }}</div>
            <div class="tf-title-line">
              <h1>{{ title }}</h1>
              <span class="tf-brand-badge">{{ badge }}</span>
            </div>
          </div>
        </div>
        <div class="tf-header-actions">
          <slot name="header-actions" />
          <v-btn
            v-if="showSwitch"
            class="tf-switch-button"
            variant="text"
            size="small"
            :disabled="busy"
            @click="emit('switch')"
            >{{ subtitle === '全局配置' ? '打开工作台' : '全局配置' }}</v-btn
          >
          <v-btn
            v-if="showSwitch"
            class="tf-switch-icon"
            variant="text"
            size="small"
            icon
            :disabled="busy"
            :aria-label="subtitle === '全局配置' ? '打开单种子工作台' : '打开全局配置'"
            @click="emit('switch')"
            ><v-icon :icon="subtitle === '全局配置' ? 'mdi-file-tree-outline' : 'mdi-cog-outline'"
          /></v-btn>
          <v-btn
            class="tf-refresh-button"
            variant="tonal"
            size="small"
            :disabled="busy"
            prepend-icon="mdi-refresh"
            @click="emit('refresh')"
            >刷新</v-btn
          >
          <v-btn
            class="tf-refresh-icon"
            variant="text"
            size="small"
            icon
            :disabled="busy"
            aria-label="刷新工作台"
            @click="emit('refresh')"
            ><v-icon icon="mdi-refresh"
          /></v-btn>
          <v-btn variant="text" size="small" :disabled="busy" aria-label="关闭种子格式化" @click="emit('close')"
            >关闭</v-btn
          >
        </div>
      </header>
      <div ref="layout" class="tf-layout">
        <nav class="tf-nav" aria-label="工作台分组">
          <div class="tf-nav-caption">{{ subtitle === '全局配置' ? '插件设置' : '处理流程' }}</div>
          <button
            v-for="(item, index) in sections"
            :key="item.id"
            type="button"
            class="tf-nav-item"
            :class="{ 'tf-nav-item--active': section === item.id, 'tf-nav-item--separate': item.id === 'history' }"
            :disabled="busy"
            :aria-current="section === item.id ? 'step' : undefined"
            @click="emit('update:section', item.id)"
          >
            <span class="tf-nav-number">{{ item.number || String(index + 1).padStart(2, '0') }}</span>
            <span class="tf-nav-copy"
              ><strong>{{ item.title }}</strong
              ><small>{{ item.subtitle }}</small></span
            >
            <span v-if="item.badge" class="tf-nav-count">{{ item.badge }}</span>
          </button>
          <div class="tf-nav-note">
            <v-icon icon="mdi-shield-check-outline" size="18" /><strong>先预览，再执行</strong>
            <p>操作原始下载目录。请核对媒体与每一项差异，处理记录可用于恢复。</p>
          </div>
        </nav>
        <main class="tf-main">
          <slot :mobile="mobile" :scroll-element="layout" :pagination-target="paginationHost" />
        </main>
        <aside class="tf-aside" aria-label="本次影响摘要">
          <slot name="summary" />
        </aside>
      </div>
      <div ref="paginationHost" class="tf-pagination-host" />
      <div class="tf-mobile-dock">
        <slot name="actions" />
      </div>
    </div>
  </section>
</template>
