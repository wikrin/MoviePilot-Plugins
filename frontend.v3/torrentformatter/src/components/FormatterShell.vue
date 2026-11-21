<script setup>
import { nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import '../styles/formatter.css'

const props = defineProps({
  title: { type: String, default: '种子格式化' },
  subtitle: { type: String, default: '工作台' },
  section: { type: String, default: '' },
  groups: { type: Array, default: () => [] },
  activeGroup: { type: String, default: 'workspace' },
  busy: Boolean,
})
const emit = defineEmits(['update:section', 'update:mobile', 'close', 'refresh'])
const expandedGroups = ref([props.activeGroup])
const lastSections = new Map()
watch(() => props.section, value => {
  lastSections.set(props.activeGroup, value)
  if (!expandedGroups.value.includes(props.activeGroup)) expandedGroups.value.push(props.activeGroup)
}, { immediate: true })
function isExpanded(item) {
  return expandedGroups.value.includes(item.id) && (!compactNav.value || props.activeGroup === item.id)
}
function selectGroup(item) {
  if (props.busy) return
  if (!item.children?.length) return emit('update:section', item.id)
  if (item.id === props.activeGroup) {
    expandedGroups.value = expandedGroups.value.includes(item.id)
      ? expandedGroups.value.filter(id => id !== item.id)
      : [...expandedGroups.value, item.id]
  } else {
    if (!expandedGroups.value.includes(item.id)) expandedGroups.value.push(item.id)
    emit('update:section', lastSections.get(item.id) || item.children[0].id)
  }
}
const root = ref(null)
const layout = ref(null)
const paginationHost = ref(null)
const mobile = ref(false)
const compactNav = ref(false)
const availableHeight = ref('90dvh')
let observer
let measureFrame
function measureHeight() {
  if (root.value) {
    availableHeight.value = `${Math.max(320, window.innerHeight - Math.max(0, root.value.getBoundingClientRect().top) - 12)}px`
    compactNav.value = root.value.getBoundingClientRect().width < 1100
    const isMobile = root.value.getBoundingClientRect().width < 720
    if (mobile.value !== isMobile) {
      mobile.value = isMobile
      emit('update:mobile', isMobile)
    }
  }
}
onMounted(async () => {
  await nextTick()
  measureHeight()
  window.addEventListener('resize', measureHeight)
  if (typeof ResizeObserver !== 'undefined') {
    observer = new ResizeObserver(() => {
      cancelAnimationFrame(measureFrame)
      measureFrame = requestAnimationFrame(measureHeight)
    })
    observer.observe(root.value)
  }
})
onUnmounted(() => {
  window.removeEventListener('resize', measureHeight)
  observer?.disconnect()
  cancelAnimationFrame(measureFrame)
})
</script>

<template>
  <section ref="root" class="tf-shell" :style="{ '--tf-available-height': availableHeight }">
    <div class="tf-frame tf-frame--unified" :class="{ 'tf-frame--history': section === 'history', 'tf-frame--workspace': section !== 'history', 'tf-frame--selection': section === 'select' }">
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
            </div>
          </div>
        </div>
        <div class="tf-header-actions">
          <slot name="header-actions" />
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
            aria-label="刷新当前页面"
            @click="emit('refresh')"
            ><v-icon icon="mdi-refresh"
          /></v-btn>
          <v-btn variant="text" size="small" :disabled="busy" aria-label="关闭种子格式化" @click="emit('close')"
            >关闭</v-btn
          >
        </div>
      </header>
      <div ref="layout" class="tf-layout">
        <nav class="tf-nav tf-nav--tree" aria-label="种子格式化导航">
          <div class="tf-nav-groups">
            <div v-for="item in groups" :key="item.id" class="tf-nav-group"
              :class="{ 'tf-nav-group--active': activeGroup === item.id }">
              <button type="button" class="tf-nav-item tf-nav-parent"
                :class="{ 'tf-nav-item--active': section === item.id }"
                :disabled="busy"
                :aria-current="section === item.id ? 'page' : undefined"
                :aria-expanded="item.children ? isExpanded(item) : undefined"
                :aria-controls="item.children ? `tf-nav-${item.id}` : undefined"
                @click="selectGroup(item)">
                <v-icon :icon="item.icon" size="19" aria-hidden="true" />
                <span class="tf-nav-copy"><strong>{{ item.title }}</strong></span>
                <span v-if="item.badge" class="tf-nav-count">{{ item.badge }}</span>
                <v-icon v-if="item.children" class="tf-nav-chevron"
                  :icon="isExpanded(item) ? 'mdi-chevron-up' : 'mdi-chevron-down'"
                  size="16" aria-hidden="true" />
              </button>
              <div v-if="item.children" v-show="isExpanded(item)" :id="`tf-nav-${item.id}`" class="tf-nav-children">
                <button v-for="child in item.children" :key="child.id" type="button"
                  class="tf-nav-item tf-nav-child" :class="{ 'tf-nav-item--active': section === child.id }"
                  :disabled="busy" :aria-current="section === child.id ? 'page' : undefined"
                  @click="emit('update:section', child.id)">
                  <v-icon :icon="child.icon" size="17" aria-hidden="true" />
                  <span class="tf-nav-copy"><strong>{{ child.title }}</strong></span>
                  <span v-if="child.badge" class="tf-nav-count">{{ child.badge }}</span>
                </button>
              </div>
            </div>
          </div>
          <div class="tf-nav-note">
            <v-icon icon="mdi-shield-check-outline" size="18" /><strong>先预览，再执行</strong>
            <p>操作原始下载目录。请核对媒体与每一项差异，处理记录可用于恢复。</p>
          </div>
        </nav>
        <main class="tf-main">
          <slot :mobile="mobile" :scroll-element="layout" :pagination-target="paginationHost" />
        </main>
        <aside class="tf-aside" :class="{ 'tf-aside--actions': $slots['summary-actions'] }" aria-label="本次影响摘要">
          <div v-if="$slots['summary-actions']" class="tf-aside-content">
            <slot name="summary" />
          </div>
          <slot v-else name="summary" />
          <div v-if="$slots['summary-actions']" class="tf-aside-actions">
            <slot name="summary-actions" />
          </div>
        </aside>
      </div>
      <div ref="paginationHost" class="tf-pagination-host" />
      <div class="tf-mobile-dock">
        <slot name="actions" :mobile="mobile" />
      </div>
    </div>
  </section>
</template>
