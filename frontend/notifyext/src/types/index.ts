export interface NotificationRule {
    // 配置名
    name: string
    // 配置ID
    id: string
    // 目标渠道
    target: string
    // 配置开关
    enabled: boolean
    // 规则类型 (可选值: regex, ctype)
    type: string
    // YAML 配置
    yaml_content?: string
    // 媒体类型(None为全部, 可选值: movie, tv)
    media_type?: string
    // 媒体类别
    media_category: Array<string>
    // 正则模式模板ID
    Template_id?: string
    // 订阅添加
    subscribeAdded?: string
    // 订阅完成
    subscribeComplete?: string
    // 入库成功
    organizeSuccess?: string
    // 下载添加
    downloadAdded?: string
}

export interface templateConf {
    name: string
    id: string
    template?: string
}

export interface NotificationConf {
    // 名称
    name: string
    // 类型 telegram/wechat/vocechat/synologychat
    type: string
    // 配置
    config: { [key: string]: any }
    // 场景开关
    switchs?: string[]
    // 是否启用
    enabled: boolean
  }