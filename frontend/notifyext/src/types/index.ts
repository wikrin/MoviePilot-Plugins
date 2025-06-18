export interface NotificationRule {
    // 配置名
    name: string
    // 配置ID
    id: string
    // 目标渠道
    target: string
    // 配置开关
    enabled: boolean
    // 规则类型
    type: string
    // YAML 配置
    yaml_content?: string
    // 模板ID
    Template_id?: string
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