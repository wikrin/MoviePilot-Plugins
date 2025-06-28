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

export interface AggregateConf {
    // 发送间隔
    wait_time: number
    // 包含
    include?: string
    // 排除
    exclude?: string
}

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
    type?: string
    // 聚合配置
    aggregate: AggregateConf
    // 场景开关
    switch: string
    // YAML 配置
    yaml_content?: string
    // 模板ID
    template_id?: string
}

export interface TemplateConf {
    name: string
    id: string
    template?: string
}

export interface FrameHandlerItem {
    // 标题
    title: string
    // 值
    value: string
    // 场景开关
    switch: string
    // 描述
    subtitle: string
}