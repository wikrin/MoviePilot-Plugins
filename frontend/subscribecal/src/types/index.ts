export interface TimeLineItem {
  // 订阅表ID
  id: number
  // 起始时间
  dtstart: string
  // 结束时间
  dtend: string
  // 标题
  summary: string
  // 描述
  description?: string
  // 地点
  location?: string
  // 唯一标识
  uid: string
  // 年份
  year: string
  // 类型
  type: string
  // 季号
  season?: number
  // 集号
  episode?: number
  // 海报
  poster: string
  // 背景图
  backdrop: string
  // 评分，float
  vote: GLfloat
  // 状态：N-新建 R-订阅中 P-待定 S-暂停
  state: string
}

// 日历事件组
export interface TimeLineGroup {
    // 日期
    date: string
    // 事件列表
    items: TimeLineItem[]
}