<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import CalendarEvent from './cards/CalendarEvent.vue'
import type { TimeLineGroup } from '../types'


const props = defineProps({
  config: {
    type: Object,
    default: () => ({}),
  },
  allowRefresh: {
    type: Boolean,
    default: true,
  },
  api: {
    type: Object,
    required: true,
  },
})


const FUTURE_LOAD_THRESHOLD = 200
const FUTURE_LOAD_BATCH_DAYS = 5
const INITIAL_BEFORE_DAYS = 10
const INITIAL_AFTER_DAYS = 5
const INITIAL_VISIBLE_FUTURE_OFFSET = 4
const SCROLL_TRACKING_HOLD_MS = 180
const TIMELINE_TARGET_SELECTORS = [
  '.v-timeline-divider__dot',
  '.v-timeline-item__dot',
  '.v-timeline-divider',
  '.v-timeline-item__divider',
  '.v-timeline-divider__inner-dot',
]


// 保存完整时间轴数据；真正渲染的范围由 computed 决定
const timelineGroups = reactive<TimeLineGroup[]>([])

const loading = ref(true)
const futureLoading = ref(false)
const userScrolled = ref(false)
const hasCenteredInitialItem = ref(false)

const timelineContainerRef = ref<HTMLElement | null>(null)

// 用于区分“用户触发的滚动”与“程序为了居中/校正触发的滚动”
const pointerScrollIntent = ref(false)
const suppressScrollTracking = ref(false)

// before / after 表示已经向接口请求过的时间范围，不等于当前可见范围
const loadedRange = reactive({
  before: 0,
  after: 0,
})

let suppressScrollTrackingTimer: ReturnType<typeof setTimeout> | null = null
let restoreScrollBehaviorTimer: ReturnType<typeof setTimeout> | null = null
let fixedBaseIndex = -1 // 用户接管滚动后锁定基准索引，避免后续增量加载导致视觉中心漂移


// 将 Date 转换成本地时区下的 YYYY-MM-DD。
// 不能直接使用 toISOString()，否则 UTC 偏移可能让本地日期跨天。
function getLocalISODateString(date: Date = new Date()): string {
  const offset = date.getTimezoneOffset() * 60000
  return new Date(date.getTime() - offset).toISOString().slice(0, 10)
}

// 解析后端返回的 UTC 时间字符串，格式固定为 20250101T123456Z。
// 格式非法时返回 null，由上层忽略该条坏数据，保持原有容错行为。
function parseUTCDateTime(dateStr: string): Date | null {
  if (!/^\d{8}T\d{6}Z$/.test(dateStr)) {
    console.warn(`Invalid date format: ${dateStr}`)
    return null
  }

  const year = Number(dateStr.slice(0, 4))
  const month = Number(dateStr.slice(4, 6)) - 1
  const day = Number(dateStr.slice(6, 8))
  const hour = Number(dateStr.slice(9, 11))
  const minute = Number(dateStr.slice(11, 13))
  const second = Number(dateStr.slice(13, 15))

  return new Date(Date.UTC(year, month, day, hour, minute, second))
}

function getTodayStart(): Date {
  const today = new Date()
  return new Date(today.getFullYear(), today.getMonth(), today.getDate())
}

// 将接口返回的原始事件列表按“本地日期”归组
// 分组依据使用本地日期，而不是 UTC 日期，避免晚间/凌晨事件被归到错误日期
// dtstart / dtend 统一转换为 Date
function normalizeGroups(items: any[]): TimeLineGroup[] {
  const groupedByDate = new Map<string, TimeLineGroup>()

  items.forEach(item => {
    const startDate = parseUTCDateTime(item.dtstart)
    if (!startDate) {
      return
    }

    const groupDate = getLocalISODateString(startDate)
    const endDate = parseUTCDateTime(item.dtend)
    const normalizedItem = {
      ...item,
      dtstart: startDate,
      ...(endDate ? { dtend: endDate } : {}),
    }

    if (!groupedByDate.has(groupDate)) {
      groupedByDate.set(groupDate, { date: groupDate, items: [] })
    }

    groupedByDate.get(groupDate)!.items.push(normalizedItem)
  })

  return Array.from(groupedByDate.values()).sort(
    (left, right) => new Date(left.date).getTime() - new Date(right.date).getTime()
  )
}

// 以 date 为唯一键合并分组，避免接口请求范围重叠时同一天重复插入
function dedupeGroupsByDate(groups: TimeLineGroup[]): TimeLineGroup[] {
  const uniqueGroupMap = new Map<string, TimeLineGroup>()

  groups.forEach(group => {
    if (!uniqueGroupMap.has(group.date)) {
      uniqueGroupMap.set(group.date, group)
    }
  })

  return Array.from(uniqueGroupMap.values())
}

function mergeTimelineGroups(groups: TimeLineGroup[], position: 'before' | 'after') {
  if (!groups.length) {
    return
  }

  const mergedSource = position === 'before'
    ? [...groups, ...timelineGroups]
    : [...timelineGroups, ...groups]

  timelineGroups.splice(0, timelineGroups.length, ...dedupeGroupsByDate(mergedSource))
}


// 接口请求
async function fetchTimelineGroups(beforeDays: number, afterDays: number) {
  try {
    const response: any[] = await props.api.get('plugin/SubscribeCal/grouped_events', {
      params: { before_days: beforeDays, after_days: afterDays },
    })

    const normalizedGroups = normalizeGroups(response)

    if (beforeDays > loadedRange.before) {
      mergeTimelineGroups(normalizedGroups, 'before')
      loadedRange.before = beforeDays
    }

    if (afterDays > loadedRange.after) {
      mergeTimelineGroups(normalizedGroups, 'after')
      loadedRange.after = afterDays
    }
  } catch (error) {
    console.error(error)
  }
}

async function loadMoreFutureGroups() {
  if (futureLoading.value) {
    return
  }

  futureLoading.value = true
  try {
    await fetchTimelineGroups(loadedRange.before, loadedRange.after + FUTURE_LOAD_BATCH_DAYS)
  } finally {
    futureLoading.value = false
  }
}


// 计算“基准日期”所在索引
// 主动滚动后，固定使用 fixedBaseIndex，避免 displayedGroups 重新计算后基准漂移
// 默认优先今天, 今天没有事件时，回退到最近未来日期, 没有未来数据时，退回第一个分组
function getBaseIndex(groups: TimeLineGroup[]): number {
  if (!groups.length) {
    return -1
  }

  if (userScrolled.value && fixedBaseIndex >= 0) {
    return fixedBaseIndex
  }

  const todayString = getLocalISODateString()
  const todayIndex = groups.findIndex(group => group.date === todayString)
  if (todayIndex !== -1) {
    return todayIndex
  }

  const todayStart = getTodayStart()
  const futureIndex = groups.findIndex(group => new Date(group.date) >= todayStart)

  return futureIndex !== -1 ? futureIndex : 0
}

// 首次自动居中时按“离今天最近的日期”选目标
// 与 getBaseIndex 的职责不同：这里追求视觉居中目标，而不是裁剪基准索引
function getTargetGroupDate(groups: TimeLineGroup[]): string | null {
  if (!groups.length) {
    return null
  }

  const todayString = getLocalISODateString()
  const todayTime = new Date(todayString).getTime()

  return groups.reduce((nearestGroup, group) => {
    if (group.date === todayString) {
      return group
    }

    const nearestDiff = Math.abs(new Date(nearestGroup.date).getTime() - todayTime)
    const currentDiff = Math.abs(new Date(group.date).getTime() - todayTime)
    return currentDiff < nearestDiff ? group : nearestGroup
  }).date
}

// 优先查找时间轴真实节点（圆点/分隔节点）作为居中锚点
function getTimelineTargetElement(targetDate: string): HTMLElement | null {
  const container = timelineContainerRef.value
  if (!container) {
    return null
  }

  const timelineItem = container.querySelector<HTMLElement>(`.timeline-group-item[data-group-date="${targetDate}"]`)
  if (!timelineItem) {
    return null
  }

  for (const selector of TIMELINE_TARGET_SELECTORS) {
    const targetElement = timelineItem.querySelector<HTMLElement>(selector)
    if (targetElement) {
      return targetElement
    }
  }

  return timelineItem.querySelector<HTMLElement>(`.timeline-center-anchor[data-group-date-anchor="${targetDate}"]`)
}

function getTimelineTargetCenter(targetElement: HTMLElement, container: HTMLElement): number {
  const targetRect = targetElement.getBoundingClientRect()
  const containerRect = container.getBoundingClientRect()
  return targetRect.left - containerRect.left + container.scrollLeft + targetRect.width / 2
}

// 在程序主动滚动期间短暂关闭滚动跟踪, 用于区分“自动居中/校正”与“用户接管滚动”
function holdScrollTracking(duration = SCROLL_TRACKING_HOLD_MS) {
  suppressScrollTracking.value = true

  if (suppressScrollTrackingTimer) {
    clearTimeout(suppressScrollTrackingTimer)
  }

  suppressScrollTrackingTimer = setTimeout(() => {
    suppressScrollTracking.value = false
    suppressScrollTrackingTimer = null
  }, duration)
}

function scrollTargetToCenter(targetElement: HTMLElement, container: HTMLElement) {
  const maxScrollLeft = Math.max(0, container.scrollWidth - container.clientWidth)
  if (maxScrollLeft <= 0) {
    return
  }

  const targetCenter = getTimelineTargetCenter(targetElement, container)
  const nextScrollLeft = targetCenter - container.clientWidth / 2
  const clampedScrollLeft = Math.min(Math.max(0, nextScrollLeft), maxScrollLeft)

  if (restoreScrollBehaviorTimer) {
    clearTimeout(restoreScrollBehaviorTimer)
  }

  holdScrollTracking()
  container.style.scrollBehavior = 'auto'
  container.scrollTo({
    left: clampedScrollLeft,
    behavior: 'auto',
  })

  restoreScrollBehaviorTimer = setTimeout(() => {
    container.style.scrollBehavior = ''
    restoreScrollBehaviorTimer = null
  }, 0)
}

// 第一次滚动先把目标大致拉到中间
// 等布局在滚动后稳定后，再取一次真实节点位置做二次校正
async function centerTimelineItem() {
  if (hasCenteredInitialItem.value) {
    return
  }

  await nextTick()

  requestAnimationFrame(() => {
    requestAnimationFrame(async () => {
      const container = timelineContainerRef.value
      const targetDate = getTargetGroupDate(displayedGroups.value)
      if (!container || !targetDate) {
        return
      }

      const initialTarget = getTimelineTargetElement(targetDate)
      if (!initialTarget) {
        return
      }

      await nextTick()

      requestAnimationFrame(() => {
        const alignedTarget = getTimelineTargetElement(targetDate)
        const alignedContainer = timelineContainerRef.value
        if (!alignedTarget || !alignedContainer) {
          return
        }

        scrollTargetToCenter(alignedTarget, alignedContainer)

        requestAnimationFrame(() => {
          const correctedTarget = getTimelineTargetElement(targetDate)
          const correctedContainer = timelineContainerRef.value
          if (!correctedTarget || !correctedContainer) {
            return
          }

          scrollTargetToCenter(correctedTarget, correctedContainer)
          hasCenteredInitialItem.value = true
        })
      })
    })
  })
}


// mergeTimelineGroups 理论上已完成去重；这里再做一次按日期归并
// 作为渲染层的兜底，防止后续维护时重复分组进入模板
const uniqueGroups = computed(() => dedupeGroupsByDate(timelineGroups))

// 初始阶段只展示“全部已加载的过去 + 基准点附近有限未来”
// 以降低首屏密度；一旦用户主动滚动，视为用户接管视野范围
const displayedGroups = computed(() => {
  const groups = uniqueGroups.value
  const baseIndex = getBaseIndex(groups)
  if (baseIndex === -1) {
    return []
  }

  if (userScrolled.value) {
    return groups
  }

  const endIndex = Math.min(groups.length - 1, baseIndex + INITIAL_VISIBLE_FUTURE_OFFSET)
  return groups.slice(0, endIndex + 1)
})


function getStatusColor(group: TimeLineGroup): string {
  const today = new Date()
  const [year, month, day] = group.date.split('-').map(Number)
  const eventDate = new Date(year, month - 1, day)
  const diffDays = Math.ceil((eventDate.getTime() - today.getTime()) / (1000 * 60 * 60 * 24))

  if (eventDate < new Date(today.getFullYear(), today.getMonth(), today.getDate())) {
    return '#CCCCCC'
  }

  if (diffDays === 0) {
    return '#66BB6A'
  }

  if (diffDays <= 3) {
    return '#FFC107'
  }

  return '#FF6D00'
}

function getTimelineItemSize(group: TimeLineGroup): string {
  if (group.items.length === 1) {
    return 'x-small'
  }

  if (group.items.length <= 4) {
    return 'small'
  }

  return 'regular'
}


// 事件处理
function markUserScrolled() {
  if (userScrolled.value) {
    return
  }

  fixedBaseIndex = getBaseIndex(uniqueGroups.value)
  userScrolled.value = true
}

function handleTimelinePointerDown() {
  pointerScrollIntent.value = true
}

function clearTimelinePointerIntent() {
  pointerScrollIntent.value = false
}

function handleTimelineScroll() {
  const container = timelineContainerRef.value
  if (!container || suppressScrollTracking.value) {
    return
  }

  if (pointerScrollIntent.value) {
    markUserScrolled()
    pointerScrollIntent.value = false
  }

  const distanceToRight = container.scrollWidth - container.clientWidth - container.scrollLeft
  if (distanceToRight <= FUTURE_LOAD_THRESHOLD) {
    void loadMoreFutureGroups()
  }
}

// 将鼠标滚轮的纵向输入映射为横向滚动
function handleTimelineWheel(event: WheelEvent) {
  const container = timelineContainerRef.value
  if (!container) {
    return
  }

  markUserScrolled()
  pointerScrollIntent.value = false
  event.preventDefault()
  container.scrollLeft += event.deltaY
}


// 生命周期与侦听
watch(
  displayedGroups,
  async groups => {
    if (!loading.value && groups.length && !hasCenteredInitialItem.value) {
      await centerTimelineItem()
    }
  },
  { flush: 'post' }
)

// 初始化：预取今天前 10 天、后 5 天的数据
onMounted(async () => {
  await fetchTimelineGroups(INITIAL_BEFORE_DAYS, INITIAL_AFTER_DAYS)
  loading.value = false
  await centerTimelineItem()
})
</script>

<template>
  <div class="dashboard-widget">
    <!-- 使用 v-hover 实现悬停效果 -->
    <v-hover>
      <template #default="{ isHovering, props: hoverProps }">
        <v-card v-bind="hoverProps">
          <v-card-item v-if="config?.attrs?.title">
            <v-card-title>{{ config?.attrs?.title }}</v-card-title>
          </v-card-item>
          <v-card-text>
            <!-- 加载中状态 -->
            <div v-if="loading" class="d-flex justify-center align-center py-4">
              <v-progress-circular indeterminate color="primary"></v-progress-circular>
            </div>

            <!-- 时间轴内容 -->
            <div
              v-else
              ref="timelineContainerRef"
              class="timeline-container"
              @pointerdown="handleTimelinePointerDown"
              @pointerup="clearTimelinePointerIntent"
              @pointercancel="clearTimelinePointerIntent"
              @scroll="handleTimelineScroll"
              @wheel="handleTimelineWheel"
            >
              <div class="timeline-track">
                <!-- 横向时间轴 -->
                <v-timeline
                  direction="horizontal"
                  align="center"
                  line-color="primary"
                  class="dense-timeline"
                >
                  <v-timeline-item
                    v-for="(group, index) in displayedGroups"
                    :key="group.date"
                    class="timeline-group-item"
                    :data-group-date="group.date"
                    :dot-color="getStatusColor(group)"
                    :size="getTimelineItemSize(group)"
                  >
                    <template #opposite>
                      {{ group.date }}
                    </template>

                    <!-- 用于自动居中计算的隐藏锚点，不参与实际交互 -->
                    <div
                      class="timeline-center-anchor"
                      :data-group-date-anchor="group.date"
                      aria-hidden="true"
                    ></div>

                    <CalendarEvent
                      :events="group"
                      :position="index % 2 === 0 ? 'top' : 'bottom'"
                    />
                  </v-timeline-item>
                </v-timeline>
              </div>
            </div>
          </v-card-text>

          <!-- 只在悬停时显示拖拽图标 -->
          <div v-show="isHovering" class="absolute right-5 top-5">
            <v-icon class="cursor-move">mdi-drag</v-icon>
          </div>
        </v-card>
      </template>
    </v-hover>
  </div>
</template>

<style scoped>
.dashboard-widget {
  display: flex;
  flex-direction: column;
  height: 100%; /* 确保占满父容器 */
}

.v-card-text {
  display: flex;
  flex-direction: column;
  flex-grow: 1; /* 占据剩余空间 */
  overflow: hidden;
}

.timeline-container {
  flex-grow: 1; /* 自动扩展以填满可用空间 */
  overflow-x: auto;
  overflow-y: hidden;
  scroll-behavior: smooth;
}

.timeline-track {
  display: inline-flex;
  min-width: max-content;
  padding: 0;
}

:deep(.v-timeline.dense-timeline) {
  display: inline-flex;
  flex-wrap: nowrap;
  width: max-content;
  min-width: max-content;
}

:deep(.timeline-group-item) {
  position: relative;
  flex: 0 0 auto;
  min-width: 0;
}

:deep(.timeline-center-anchor) {
  position: absolute;
  left: 50%;
  top: 0;
  width: 0;
  height: 0;
  transform: translateX(-50%);
  pointer-events: none;
}

.timeline-card {
  height: 100%;
  display: flex;
  flex-direction: column;
}
</style>
