## 👋 介绍
MoviePilot 消息通知扩展插件用于增强系统消息通知功能，支持自定义模板和分发规则，实现更灵活的消息通知管理。

## ✨ 特性
- 🔧 自定义消息模板
- 📝 灵活的消息分发规则
- 🎯 支持正则表达式匹配
- ⏰ 消息冷却时间控制
- 🔍 元数据提取与识别

## ⚙️ 配置说明

### 基础配置
- `消息冷却时间(分钟)`: 防止短时间内重复发送相同消息.

### 消息模板
使用主程序相同`格式&语法`,
在`正则匹配`模式下支持自定义字段的使用

### 分发规则
分发规则决定消息如何被处理和发送：
> 目标渠道
> - 消息发往何处

> 规则类型
> - 内容类型: 仅处理`资源入库` `资源下载` `添加订阅` `订阅完成`
> - 正则匹配: 在YAML中编辑匹配规则

### 🌰 正则模式使用示例:

`extractors` 中定义需要提取的信息, 例如
```yaml
extractors:
  - field: 'title' # 从消息的标题中匹配 除此之外还有 'text:消息内容', 'link:消息链接', 'image:图片地址' ...
    org_msg_title: '.*' # 存储原始消息标题到 org_msg_title 中

  - field: 'text' # 从消息的内容中匹配
    torrent_name: '你下载的种子''(?P<torrent_name>.*?)''被管理员删除' # 存储种子名到 torrent_name 中
    reason: '原因：(?P<reason>.*)' # 存储原因到 reason 中
```
在消息模板中就可使用`{% if org_msg_title %} {{ org_msg_title }}{% endif %}`  
如果使用`(?P<example>...)` 则模板中需要使用`{% if example %} {{ example }}{% endif %}` `MetaBase`绑定同理

`MetaBase` 中定义元数据对象属性与提取信息的绑定关系, 例如

```yaml
MetaBase:
  title: 'torrent_name'
```
此时`title` 的值为 `extractors`中 `torrent_name` 所匹配的结果, 需要获得媒体信息必须绑定 `title`，否则模板中可用字段仅有 `extractors`中提取的结果
可绑定`tmdbid`精准识别
更多`MetaBase`属性见[MetaInfo](https://github.com/jxxghp/MoviePilot/blob/fcd5ca3fda1992ece6bb2111afa1b75909d0557f/app/schemas/context.py#L6-L61)

## 📝 注意事项
1. 正则表达式规则请确保格式正确
