<script setup lang="ts">
import { ref, computed, onMounted, reactive } from 'vue'
import CalendarEvent from './cards/CalendarEvent.vue'
import type { TimeLineGroup } from '../types'

// 接收仪表板配置
const props = defineProps({
  config: {
    type: Object,
    default: () => ({}),
  },
  allowRefresh: {
    type: Boolean,
    default: true,
  },
  api:  {
    type: Object,
    required: true,
  },
})

// 所有订阅数据
const timeLineGroups = reactive<TimeLineGroup[]>([])

// 组件状态
const loading = ref<boolean>(true)
const userScrolled = ref(false) // 是否发生过用户滑动
let fixedBaseIndex = -1 // 固定的 baseIndex，仅在用户滑动后生效

// 当前已加载的天数范围
const loadedRange = reactive({
  before: 0, // 已加载的过去天数
  after: 0,  // 已加载的未来天数
})

async function fetchTimeLineGroups(beforeDays: number, afterDays: number) {
  try {
    const res: any[] = await props.api.get(`plugin/SubscribeCal/grouped_events`, {
      params: { before_days: beforeDays, after_days: afterDays }
    })

    // 按日期分组
    const grouped = new Map<string, TimeLineGroup>()

    res.forEach(item => {
      // 解析UTC时间
      const startDate = parseUTCDateTime(item.dtstart)

      if (startDate) {
        const localDateStr = getLocalISODateString(startDate)
        const endDate = parseUTCDateTime(item.dtend)

        if (endDate) item.dtend = endDate
        // 格式化项目，使用解析后的Date对象
        const formattedItem = {
          ...item,
          // 使用本地时间
          dtstart: startDate,
        }

        if (!grouped.has(localDateStr)) {
          grouped.set(localDateStr, {
            date: localDateStr,
            items: []
          })
        }

        grouped.get(localDateStr)!.items.push(formattedItem)
      }
    })

    // 转换为数组并排序
    const groups = Array.from(grouped.values())
      .sort((a, b) => new Date(a.date).getTime() - new Date(b.date).getTime())

    // 去重
    const uniqueGroups = new Map<string, TimeLineGroup>()
    groups.forEach(group => {
      if (!uniqueGroups.has(group.date)) {
        uniqueGroups.set(group.date, group)
      }
    })

    const filteredGroups = Array.from(uniqueGroups.values())

    if (beforeDays > loadedRange.before) {
      // 添加过往数据
      timeLineGroups.unshift(...filteredGroups)
      loadedRange.before = beforeDays
    }

    if (afterDays > loadedRange.after) {
      // 添加未来数据
      timeLineGroups.push(...filteredGroups)
      loadedRange.after = afterDays
    }
  } catch (error) {
    console.error(error)
  }
}

/**
 * 将 %Y%m%dT%H%M%SZ 格式的时间字符串转为 Date 对象（UTC 时间）
 * @param dateStr 输入格式如 '20250405T160000Z'
 * @returns {Date|null} 解析后的 本地时间 Date 对象，失败返回 null
 */
 const parseUTCDateTime = (dateStr: string): Date | null => {
  // 简单校验格式是否符合 YYYYMMDDTHHMMSSZ
  const regex = /^\d{8}T\d{6}Z$/
  if (!regex.test(dateStr)) {
    console.warn(`Invalid date format: ${dateStr}`)
    return null
  }

  const year = parseInt(dateStr.slice(0, 4), 10)
  const month = parseInt(dateStr.slice(4, 6), 10) - 1 // 月份从 0 开始
  const day = parseInt(dateStr.slice(6, 8), 10)
  const hour = parseInt(dateStr.slice(9, 11), 10)
  const minute = parseInt(dateStr.slice(11, 13), 10)
  const second = parseInt(dateStr.slice(13, 15), 10)

  return new Date(Date.UTC(year, month, day, hour, minute, second))
}

function getLocalISODateString(date: Date = new Date()): string {
  const offset = date.getTimezoneOffset() * 60000
  return new Date(date.getTime() - offset).toISOString().slice(0, 10)
}

function getBaseIndex(): number {
  if (userScrolled.value && fixedBaseIndex >= 0) {
    return fixedBaseIndex
  }
  const todayStr = getLocalISODateString()
  const todayIndex = timeLineGroups.findIndex(g => g.date === todayStr)

  if (todayIndex !== -1) {
    return todayIndex
  }

  const futureIndex = timeLineGroups.findIndex(g => {
    const groupDate = new Date(g.date)
    const today = new Date()
    return groupDate >= new Date(today.getFullYear(), today.getMonth(), today.getDate())
  })

  return futureIndex !== -1 ? futureIndex : 0
}

function getUniqueGroups(groups: TimeLineGroup[]): TimeLineGroup[] {
  const seen = new Set<string>()
  const result: TimeLineGroup[] = []

  for (const group of groups) {
    if (!seen.has(group.date)) {
      seen.add(group.date)
      result.push(group)
    }
  }
  return result
}

// 计算显示的分组
const displayedGroups = computed(() => {
  console.log('[getDisplayedGroups] 计算显示的分组...')
  const baseIndex = getBaseIndex()
  if (baseIndex === -1) return []

  // 先统一去重
  const uniqueGroups = getUniqueGroups(timeLineGroups)

  if (userScrolled.value) {
    // 已滑动：返回所有去重后的数据
    return uniqueGroups
  } else {
    // 未滑动：只显示 baseIndex 前2个 + 后4个 范围内的数据
    const start = Math.max(0, baseIndex - 2)
    const end = Math.min(uniqueGroups.length - 1, baseIndex + 4)

    return uniqueGroups.slice(start, end + 1)
  }
})

// 获取状态颜色
function getStatusColor(group: TimeLineGroup): string {
  const today = new Date()
  const [year, month, day] = group.date.split('-').map(Number)
  const eventDate = new Date(year, month - 1, day)
  const diffDays = Math.ceil((eventDate.getTime() - today.getTime()) / (1000 * 60 * 60 * 24))

  if (eventDate < new Date(today.getFullYear(), today.getMonth(), today.getDate())) {
    return '#CCCCCC' // 过去
  } else if (diffDays === 0) {
    return '#66BB6A' // 今天
  } else if (diffDays <= 3) {
    return '#FFC107' // 即将到来
  } else {
    return '#FF6D00' // 较远未来
  }
}

function getTimelineItemSize(group: TimeLineGroup): string {
  if (group.items.length === 1) {
    return 'x-small'
  } else if (group.items.length <= 4) {
    return 'small' // 或 'regular', 'large' 等
  } else {
     return 'regular'
  }
}

// 初始化
onMounted(async () => {
  await fetchTimeLineGroups(4, 5)
  loading.value = false
})
</script>

<template>
  <div class="dashboard-widget">
    <!-- 带边框的卡片 -->
    <v-card>
      <v-card-item v-if="config?.attrs?.title">
        <v-card-title>{{ config?.attrs?.title }}</v-card-title>
      </v-card-item>
      <v-card-text>
        <!-- 加载中状态 -->
        <div v-if="loading" class="d-flex justify-center align-center py-4">
          <v-progress-circular indeterminate color="primary"></v-progress-circular>
        </div>
        <!-- 时间轴内容 -->
        <div v-else class="timeline-container px-2">
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
              :dot-color="getStatusColor(group)"
              :size="getTimelineItemSize(group)">
              <template v-slot:opposite>
                {{group.date}}
              </template>
              <CalendarEvent :events="group" :position="index % 2 === 0 ? 'top' : 'bottom'"/>
            </v-timeline-item>
          </v-timeline>
        </div>
      </v-card-text>
      <div class="absolute right-5 top-5">
        <VIcon class="cursor-move">mdi-drag</VIcon>
      </div>
    </v-card>
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
  overflow-y: auto;
}

.v-timeline.dense-timeline {
  display: flex;
  flex-wrap: nowrap;
  width: 100%;
  min-width: 100%;
}

.timeline-card {
  height: 100%;
  display: flex;
  flex-direction: column;
}
</style>