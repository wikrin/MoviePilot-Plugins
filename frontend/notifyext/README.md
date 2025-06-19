## 👋 介绍
MoviePilot 消息通知扩展插件用于增强系统消息通知功能，支持自定义模板和分发规则，实现更灵活的消息通知管理。

## ✨ 特性
- 🔧 自定义消息模板
- 📝 灵活的消息分发规则
- 🎯 支持正则表达式匹配
- ⏰ 消息冷却时间控制
- 📦 消息批量聚合
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

### 🌰 正则匹配使用示例

#### Extractors 配置
使用 `extractors` 定义需要从消息中提取的信息:

```yaml
extractors:
  - field: 'title'  # 匹配来源: title(标题)、text(内容)、link(链接)、image(图片)
    org_msg_title: '.*'  # 将完整标题存储到 org_msg_title 变量
  
  - field: 'text'
    torrent_name: '你下载的种子''(?P<torrent_name>.*?)''被管理员删除'
    reason: '原因：(?P<reason>.*)'
```

#### 正则匹配规则
支持两种捕获组语法:

1. 命名捕获组 `(?P<name>expression)`
```yaml
example: '原因：(?P<reason>.*)'  # 存储到变量 reason
# 模板中使用: {% if reason %}{{ reason }}{% endif %}
```

2. 普通捕获组 `(expression)`
```yaml
site_name: '【站点\s+([^\s]+)\s+消息】'  # 存储到变量 site_name
# 模板中使用: {% if site_name %}{{ site_name }}{% endif %}
# 注意: 每条正则仅提取第一个匹配组
```

#### MetaBase 配置
用于将提取的信息绑定到媒体信息对象:

```yaml
MetaBase:
  title: 'torrent_name'  # 将 torrent_name 的匹配结果绑定到 title
  tmdbid: 'tmdb_id'     # 可选:绑定 tmdbid 实现精准识别
```

**注意事项:**
- 必须绑定 `title` 才能获取完整媒体信息
- 未绑定时模板仅可使用 `extractors` 提取的变量
- 更多支持的属性请参考 [MetaInfo](https://github.com/jxxghp/MoviePilot/blob/fcd5ca3fda1992ece6bb2111afa1b75909d0557f/app/schemas/context.py#L6-L61)

### 消息聚合
通过消息聚合功能，可以将同一规则匹配到的多条消息在设定时间后统一发送。

#### 配置方法
在规则配置中添加 `Aggregate` 段落：

```yaml
Aggregate:
  required: ['field1', 'field2']  # 需要匹配的必要字段
  send_in: 2  # 可选，聚合等待时间(小时)，默认2小时
```

- `required`: 列表类型，指定需要完全匹配才会被加入聚合的字段
- `send_in`: 浮点类型，收集时间内符合规则的消息，默认为2小时

聚合消息模板中可使用的特殊变量：
- `messages`: 列表类型，包含所有`required`匹配的消息内容
- `count`: 数字类型，消息总数
- `first_time`: 首条消息时间
- `last_time`: 最后一条消息时间

#### 使用示例
以`观众` 删种的`站点消息`为例:

##### 规则YAML模板
```yaml
extractors:
  - field: 'title'
    site_name: '【站点\s+([^\s]+)\s+消息】' # 提取站点名称

  - field: 'text'
    audiences: |-
      标题：(?P<title>种子被删除)
      内容：
      你下载的种子'(?P<torrent_name>[^']+)'被管理员删除。原因：(?P<reason>.+。)

Aggregate: # 消息聚合存在即开启
  required: ['site_name', 'title', 'torrent_name', 'reason'] # 需要匹配全部字段才会加入消息聚合, 可按场景增删
  send_on: 0.1 # 收集0.1小时内所有匹配的消息
```
需要增加站点`删种消息` 只需续写`field: 'text'`
```yaml
...existing code...

  - field: 'text'
    audiences: |-
      标题：(?P<title>种子被删除)
      内容：
      你下载的种子'(?P<torrent_name>[^']+)'被管理员删除。原因：(?P<reason>.+。)

    xxxxx: |-
      标题...
      ...

...existing code...
```

##### 消息渲染模板
```json
{
    "title": "📢 站点消息通知",
    "text": (
        "───────────────\n"
        "{%- set sites = {} -%}"
        "{%- for msg in messages -%}"
        "{%- if msg.site_name and msg.reason -%}"
        "{%- set _ = sites.update({msg.site_name: msg.reason}) -%}"
        "{%- endif -%}"
        "{%- endfor -%}"
        "{%- for site, reason in sites.items() %}\n"
        "*🔹 站点：{{ site }}*\n"
        "🔸 原因：{{ reason }}\n"
        "{%- for msg in messages if msg.site_name == site %}\n"
        "➤ *{{ msg.torrent_name }}*"
        "{%- endfor -%}"
        "\n───────────────"
        "{%- if not loop.last %}\n{% endif -%}"
        "{%- endfor -%}\n"
        "⏰ 统计时间：{{ last_time | truncate(16, True, '') }}"
    )
}
```

启用聚合功能后，插件会：
1. 收集指定时间内符合规则的消息
2. 在等待时间结束后统一发送
3. 如果程序重启，超时的消息会在重启后5分钟分别发送

## 📝 注意事项
1. 正则表达式规则请确保格式正确
