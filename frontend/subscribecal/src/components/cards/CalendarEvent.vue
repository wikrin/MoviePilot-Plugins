<script setup lang="ts">
import { computed, ref } from 'vue'
import { TimeLineGroup, TimeLineItem } from '../../types'

const props = defineProps({
  events: {
    type: Object as () => TimeLineGroup,
    default: () => {},
  },
  position: {
    type: String,
    default: 'top',
  }
})

// 控制对话框显示状态
const showDialog = ref(false)

// 打开弹窗
const openDialog = () => {
  showDialog.value = true
}

// 关闭弹窗
const closeDialog = () => {
  showDialog.value = false
}

const handleDialogClick = (event: Event) => {
  // 如果点击的是 dialog 元素本身（即背景），就关闭弹窗
  if ((event.target as HTMLElement).classList.contains('modal-dialog')) {
    closeDialog()
  }
}

/**
 * 将 %Y%m%dT%H%M%SZ 格式的时间字符串转为 Date 对象（UTC 时间）
 * @param dateStr 输入格式如 '20250405T160000Z'
 * @returns {Date|null} 解析后的 UTC 时间 Date 对象，失败返回 null
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

  // 使用 UTC 时间构造
  return new Date(Date.UTC(year, month, day, hour, minute, second))
}

function getStatusColor(event: TimeLineItem): string {
  // 如果开始和结束时间都是 16:00，则认为时间不准确
  if (event.dtstart?.slice(9, 13) === '1600' && event.dtend?.slice(9, 13) === '1600') {
    return '#9C27B0' // 深紫红 - 时间不准确
  }

  const date = parseUTCDateTime(event.dtstart)
  if (!date) return '#CCCCCC' // 浅灰 - 默认未知状态

  // 转换为本地时间
  const localDate = new Date(date.getTime() + new Date().getTimezoneOffset() * 60000)
  const localHour = localDate.getHours()

  if (localHour >= 6 && localHour < 12) return '#FFA000' // 橘黄色 - 上午
  else if (localHour >= 12 && localHour < 14) return '#FFF176' // 浅金黄 - 中午
  else if (localHour >= 14 && localHour < 18) return '#4DD0E1' // 天蓝 - 下午
  else if (localHour >= 18 && localHour < 20) return '#FB8C00' // 橘红 - 傍晚
  else if (localHour >= 20 || localHour < 6) return '#303F9F' // 深蓝紫 - 夜间/凌晨

  return '#CCCCCC' // 默认浅灰色
}

// 排序
const sortedEvents = computed(() => {
  const events = props.events.items
  if (!Array.isArray(events) || events.length === 0) {
    return []
  }

  return events
    .filter(event => event?.dtstart)
    .sort((a, b) => {
      const formatDateString = (dateStr: string): string => {
        return `${dateStr.slice(0, 4)}-${dateStr.slice(4, 6)}-${dateStr.slice(6, 8)}T${dateStr.slice(9, 11)}:${dateStr.slice(11, 13)}:${dateStr.slice(13, 15)}Z`
      }

      const dateA = Date.parse(formatDateString(a.dtstart))
      const dateB = Date.parse(formatDateString(b.dtstart))

      // 非法日期排在后面
      if (isNaN(dateA)) return 1
      if (isNaN(dateB)) return -1

      // 按时间排序
      if (dateA !== dateB) return dateA - dateB

      // 时间相同则按 id 排序
      if (a.id !== b.id) return a.id < b.id ? -1 : 1

      // 判断 episode 是否存在
      const hasAEpisode = a.episode != null
      const hasBEpisode = b.episode != null

      // 没有 episode 的排在后面
      if (!hasAEpisode && hasBEpisode) return 1
      if (hasAEpisode && !hasBEpisode) return -1

      // 如果都有 episode，则按 episode 升序排列
      if (hasAEpisode && hasBEpisode) return (a.episode ?? 0) - (b.episode ?? 0)

      // 如果都没有 episode，保持原有顺序
      return 0
    })
})

// 过滤出 id 不重复的事件，并取前四项
const uniqueEvents = computed<TimeLineItem[]>(() => {
  const seenIds = new Set()
  const result: TimeLineItem[] = []

  for (const event of sortedEvents.value) {
    if (!seenIds.has(event.id)) {
      seenIds.add(event.id)
      result.push(event)
      if (result.length >= 4) break
    }
  }

  return result
})

function getIconForEventType(type: string): string {
  switch (type.toLowerCase()) {
    case '电视剧':
      return 'mdi-television-play'
    case '电影':
      return 'mdi-filmstrip'
    default:
      return 'mdi-help-box'
  }
}
</script>

<template>
  <div class="card-container" @click="openDialog">
    <div class="stacked-cards">
      <div
        v-for="(event, index) in uniqueEvents"
        :key="event.id"
        class="stacked-image"
        :class="`image-${position}`"
      >
        <img :src="index === 0 ? event.poster : event.backdrop" />
      </div>

      <!-- 标题 -->
      <div class="titles-container" :class="`title-${position}`">
        {{ sortedEvents[0]?.summary }}
      </div>
    </div>
    <!-- dialog 弹窗 -->
    <v-dialog v-model="showDialog" max-width="50rem" scrollable>
      <div class="modal-content" @click="handleDialogClick">
        <v-card>
        <v-card-title class="bg-primary-lighten-5">{{events.date}}</v-card-title>
        <v-card-text class="py-4">
          <!-- 竖向时间轴 -->
          <v-timeline
            side="end"
            direction="vertical"
            line-color="primary"
            class="dense-timeline scrollable-timeline"
          >
            <v-timeline-item
              v-for="event in sortedEvents"
              :key="event.uid"
              size="small"
              :dot-color="getStatusColor(event)"
              :icon="getIconForEventType(event.type)"
              fill-dot
            >
              <VCard>
              <div class="d-flex justify-space-between flex-nowrap flex-row">
                <div class="ma-auto">
                  <VImg
                    height="75"
                    width="50"
                    :src="event.poster"
                    aspect-ratio="2/3"
                    class="object-cover rounded ring-gray-500"
                    cover
                  >
                    <template #placeholder>
                      <div class="w-full h-full">
                        <VSkeletonLoader class="object-cover aspect-w-2 aspect-h-3" />
                      </div>
                    </template>
                  </VImg>
                </div>
                <div>
                  <VCardSubtitle class="pa-1 px-2 font-bold break-words whitespace-break-spaces">
                    {{ event.summary }}
                  </VCardSubtitle>
                  <VCardText class="pa-0 px-2 break-words">
                    <v-icon small color="#FFB400" class="mr-1">mdi-star</v-icon>
                    <span class="mr-4">{{ event.vote ?? '暂无' }}</span>
                    <v-icon small color="primary" class="mr-1">mdi-clock-time-four-outline</v-icon>
                    <span>
                      {{ parseUTCDateTime(event.dtstart)?.toTimeString().slice(0, 5) }} -
                      {{ parseUTCDateTime(event.dtend)?.toTimeString().slice(0, 5) }}
                    </span>
                  </VCardText>
                </div>
              </div>
            </VCard>
            </v-timeline-item>
          </v-timeline>
        </v-card-text>
        </v-card>
      </div>
    </v-dialog>
  </div>
</template>

<style scoped>
.card-container {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 0;
}

.stacked-cards {
  position: relative;
  width: 150px;
  aspect-ratio: 1 / 1;
  display: flex;
  justify-content: center;
  align-items: center;
}

.stacked-image {
  position: absolute;
  background: white;
  padding: 0.5%;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
  transition: all 0.3s ease;
}

.stacked-image img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

/* 堆叠图片的定位 */
.stacked-image:nth-child(1) {
  width: 45%;
  height: 68%;
  z-index: 4;  /* 最顶层 */
}

 .stacked-image:nth-child(2) {
  width: 55%;
  height: 35%;
  z-index: 2; /* 第三层 */
}

.stacked-image:nth-child(3) {
  width: 50%;
  height: 35%;
  z-index: 3;  /* 第二层 */
}

.stacked-image:nth-child(4)  {
  width: 60%;
  height: 40%;
  z-index: 1; /* 最底层 */
}

.image-top:nth-child(1) {
  right: 0;
  bottom: 0;
}

.image-top:nth-child(2) {
  right: 3%;
  top: 2%;
}

.image-top:nth-child(3) {
  left: 10%;
  top: 25%;
}

.image-top:nth-child(4) {
  left: 0;
  top: 0;
}

/* 反转模式下的定位 */
.image-bottom:nth-child(1) {
  left: 0;
  top: 0;
}

.image-bottom:nth-child(2) {
  left: 3%;
  top: 62%;
}

.image-bottom:nth-child(3) {
  left: 40%;
  top: 35%;
}

.image-bottom:nth-child(4) {
  left: 40%;
  top: 60%;
}

.titles-container {
  font-size: calc(0.5vw);
  color: #006400;
  font-weight: bold;
  white-space: normal;
  text-align: center;
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 3; /* 保留旧版 */
  line-clamp: 3; /* 新增标准属性 */
  overflow: hidden;
  text-overflow: ellipsis;
  position: absolute;
  width: 45%;
  line-height: 1.2;
  max-height: calc(1.2 * 1.5em * 3);
}

.title-top {
  border-top: 0.1px solid #006400;
  padding-top: 2%;
  left: 3%;
  bottom: 0;
}

.title-bottom {
  border-bottom: 0.1px solid #006400;
  padding-bottom: 2%;
  right: 3%;
  top: 0;
}

/* 设置时间轴最大高度并启用垂直滚动 */
.scrollable-timeline {
  max-height: 60vh; /* 控制最大可视区域高度 */
  overflow-y: auto; /* 启用垂直滚动 */
  scrollbar-width: thin; /* Firefox：窄滚动条 */
}

/* hover效果微调 */
.stacked-image:hover {
  transform: translateY(-15px) rotate(0);
  z-index: 10;
  box-shadow: 0 12px 24px rgba(0, 0, 0, 0.2);
}
</style>