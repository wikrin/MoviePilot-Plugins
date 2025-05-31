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
const loading = ref(true)

// 获取日历事件
async function fetchTimeLineGroups() {
  try {
    const res: Object = await props.api.get('plugin/SubscribeCal/grouped_events')

    // 转换对象结构为 TimeLineGroup[]
    const groups = Object.entries(res).map(([date, items]) => ({
      date,
      items: items
    }))

    timeLineGroups.push(...groups)
  } catch (error) {
    console.error(error)
  }
}

// 获取状态颜色
function getStatusColor(group: TimeLineGroup): string {
  const today = new Date()
  const [year, month, day] = group.date.split('-').map(Number)
  const eventDate = new Date(year, month - 1, day)
  const diffDays = Math.ceil((eventDate.getTime() - today.getTime()) / (1000 * 60 * 60 * 24))

  if (eventDate < new Date(today.getFullYear(), today.getMonth(), today.getDate())) {
    return 'grey' // 过去
  } else if (diffDays === 0) {
    return 'primary' // 今天
  } else if (diffDays <= 3) {
    return 'warning' // 即将到来（3天内）
  } else {
    return 'success' // 较远未来
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

// 显示三项
const displayedGroups = computed(() => {
  return timeLineGroups.slice(0, 3)
})

// 初始化
onMounted(async () => {
  await fetchTimeLineGroups()
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
              :key="index"
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