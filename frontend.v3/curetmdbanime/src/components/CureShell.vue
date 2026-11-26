<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, reactive, watch, ref } from 'vue'
import '../styles/cure.css'

type NavGroup = {
  id: string
  title: string
  icon: string
  overviewId: string
  children: { id: string; title: string; icon?: string }[]
}
const props = withDefaults(
  defineProps<{
    page: string
    groups: NavGroup[]
    groupTitle: string
    pageTitle: string
    dirty?: boolean
    busy?: boolean
    themeMode?: 'light' | 'dark'
  }>(),
  { dirty: false, busy: false },
)
const emit = defineEmits<{
  'update:page': [value: string]
  close: []
}>()
const root = ref<HTMLElement | null>(null)
const layout = ref<HTMLElement | null>(null)
const main = ref<HTMLElement | null>(null)
const paginationHost = ref<HTMLElement | null>(null)
const mobile = ref(false)
const compactExpanded = ref('service')
const availableHeight = ref('90dvh')
const expanded = reactive<Record<string, boolean>>({ service: true, correction: false })
let observer: ResizeObserver | undefined
let viewport: VisualViewport | null = null
let measureFrame = 0
function measureHeight() {
  if (!root.value) return
  const rect = root.value.getBoundingClientRect()
  // 布局宽度不受弹窗缩放动画影响，需与 CSS 容器断点保持一致。
  const nextMobile = root.value.clientWidth < 720
  // 进入窄屏时仅展开当前分组；桌面端各分组的展开状态独立保留。
  if (nextMobile && !mobile.value) compactExpanded.value = props.groups.find(groupActive)?.id || 'service'
  mobile.value = nextMobile
  const visibleViewport = window.visualViewport
  const viewportHeight = visibleViewport?.height ?? window.innerHeight
  const fullscreen = Boolean(root.value.closest('.v-dialog--fullscreen'))
  // 全屏弹窗的入场动画会暂时改变 rect.top，不能把动画偏移误算成顶部留白。
  const top = fullscreen ? 0 : Math.max(0, rect.top - (visibleViewport?.offsetTop ?? 0))
  const available = Math.max(0, viewportHeight - top - (fullscreen ? 0 : 12))
  // 全屏弹窗（含横屏/平板）和窄屏工作台不套用桌面的 84% 高度上限。
  availableHeight.value = `${mobile.value || fullscreen ? available : Math.min(available, viewportHeight * 0.84)}px`
}
// 合并同一帧内的窗口、视口与容器变化，避免反复测量导致布局抖动。
function scheduleMeasure() {
  cancelAnimationFrame(measureFrame)
  measureFrame = requestAnimationFrame(measureHeight)
}
function toggleGroup(group: NavGroup) {
  if (mobile.value) compactExpanded.value = compactExpanded.value === group.id ? '' : group.id
  else expanded[group.id] = !expanded[group.id]
}
const groupExpanded = (group: NavGroup) => (mobile.value ? compactExpanded.value === group.id : expanded[group.id])
function openGroup(group: NavGroup) {
  expanded[group.id] = true
  compactExpanded.value = group.id
  emit('update:page', group.overviewId)
}
function groupActive(group: NavGroup) {
  return props.page === group.overviewId || group.children.some(child => child.id === props.page)
}
watch(
  () => props.page,
  () => {
    const group = props.groups.find(groupActive)
    if (group) {
      expanded[group.id] = true
      compactExpanded.value = group.id
    }
    if (layout.value) layout.value.scrollTop = 0
    if (main.value) main.value.scrollTop = 0
  },
)
onMounted(async () => {
  await nextTick()
  measureHeight()
  window.addEventListener('resize', scheduleMeasure)
  viewport = window.visualViewport
  viewport?.addEventListener('resize', scheduleMeasure)
  viewport?.addEventListener('scroll', scheduleMeasure)
  if (typeof ResizeObserver !== 'undefined') {
    observer = new ResizeObserver(scheduleMeasure)
    observer.observe(root.value!)
  }
})
onUnmounted(() => {
  window.removeEventListener('resize', scheduleMeasure)
  viewport?.removeEventListener('resize', scheduleMeasure)
  viewport?.removeEventListener('scroll', scheduleMeasure)
  observer?.disconnect()
  cancelAnimationFrame(measureFrame)
})
</script>

<template>
  <section
    ref="root"
    class="ct-shell"
    :class="{ 'ct-shell--light': themeMode === 'light' }"
    :style="{ '--ct-available-height': availableHeight }"
  >
    <div
      class="ct-frame"
      :class="{
        'ct-frame--history': page === 'correction-history',
        'ct-frame--logs': page === 'service-logs',
      }"
    >
      <header class="ct-header">
        <div class="ct-brand">
          <div class="ct-brand-mark" aria-hidden="true"><v-icon icon="mdi-animation-play-outline" size="24" /></div>
          <div class="ct-brand-copy">
            <div class="ct-crumb">MoviePilot <span>/</span> 插件 <span>⏎</span></div>
            <div class="ct-title-line">
              <h1>
                CTMDbA
                <span style="font-size: 0.7em; opacity: 0.7">· {{ groupTitle }}</span>
              </h1>
            </div>
          </div>
        </div>
        <div class="ct-header-actions">
          <slot name="header-actions" :mobile="mobile" />
          <v-btn
            class="ct-config-action ct-close-config"
            color="default"
            variant="text"
            size="small"
            :disabled="busy"
            aria-label="关闭配置"
            @click="emit('close')"
            >关闭</v-btn
          >
        </div>
      </header>

      <div ref="layout" class="ct-layout">
        <nav class="ct-nav" aria-label="插件设置目录">
          <div class="ct-nav-caption">插件设置</div>
          <div
            v-for="group in groups"
            :key="group.id"
            class="ct-nav-group"
            :class="{ 'ct-nav-group--active': groupActive(group) }"
          >
            <div class="ct-nav-parent-row">
              <button
                type="button"
                class="ct-nav-item ct-nav-parent"
                :class="{
                  'ct-nav-item--active': groupActive(group),
                  'ct-nav-item--current': page === group.overviewId,
                }"
                :aria-current="page === group.overviewId ? 'page' : undefined"
                :aria-expanded="groupExpanded(group)"
                :disabled="busy"
                @click="openGroup(group)"
              >
                <v-icon :icon="group.icon" size="18" />
                <span>{{ group.title }}</span>
              </button>
              <button
                type="button"
                class="ct-nav-toggle"
                :aria-label="`${groupExpanded(group) ? '收起' : '展开'}${group.title}`"
                :aria-expanded="groupExpanded(group)"
                :aria-controls="`ct-nav-children-${group.id}`"
                :disabled="busy"
                @click="toggleGroup(group)"
              >
                <v-icon :icon="groupExpanded(group) ? 'mdi-chevron-down' : 'mdi-chevron-right'" size="18" />
              </button>
            </div>
            <div
              v-if="groupExpanded(group)"
              :id="`ct-nav-children-${group.id}`"
              class="ct-nav-children"
              role="group"
              :aria-label="`${group.title}页面`"
            >
              <button
                v-for="child in group.children"
                :key="child.id"
                type="button"
                class="ct-nav-item ct-nav-child"
                :class="{ 'ct-nav-item--active': page === child.id }"
                :aria-current="page === child.id ? 'page' : undefined"
                :disabled="busy"
                @click="emit('update:page', child.id)"
              >
                <v-icon v-if="child.icon" :icon="child.icon" size="16" />
                <span>{{ child.title }}</span>
              </button>
            </div>
          </div>
          <aside class="ct-service-source ct-surface pa-3 text-caption" aria-label="服务来源与开源支持">
            <h2 class="text-caption font-weight-bold text-high-emphasis mb-1">服务来源</h2>
            <div class="d-flex flex-column align-start">
              <span>服务由以下项目驱动</span>
              <v-btn
                prepend-icon="mdi-github"
                href="https://github.com/wikrin/CureTMDbAnime"
                append-icon="mdi-open-in-new"
                color="primary"
                rel="noopener noreferrer"
                size="small"
                target="_blank"
                variant="text"
                class="text-none px-0"
              >
                CureTMDbAnime
              </v-btn>
            </div>
            <p class="mt-2 mb-0">如果对你有帮助，欢迎点亮🌟</p>
          </aside>
        </nav>

        <!-- 桌面正文独立滚动；窄屏交给外层 layout，历史吸顶与分页复用同一滚动上下文。 -->
        <main
          ref="main"
          class="ct-main"
          :class="{ 'ct-main--logs': page === 'service-logs', 'ct-main--history': page === 'correction-history' }"
          :aria-label="`${groupTitle} / ${pageTitle}`"
        >
          <slot :mobile="mobile" :scroll-element="mobile ? layout : main" :pagination-target="paginationHost" />
        </main>
      </div>

      <div ref="paginationHost" class="ct-pagination-host" />
      <footer class="ct-dock">
        <slot name="actions" />
      </footer>
    </div>
  </section>
</template>
