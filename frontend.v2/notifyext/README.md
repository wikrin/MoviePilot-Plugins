## 👋 插件介绍

MoviePilot 消息通知扩展插件用于增强系统消息通知功能，支持**自定义模板**、**消息分发**与**消息聚合**，实现更灵活的消息推送机制。

---

## ✨ 特性
- 🔧 自定义消息模板
- 📝 灵活的消息分发规则
- 🎯 支持正则表达式匹配
- ⏰ 消息冷却时间控制
- 📦 消息批量聚合
- 🔍 元数据提取与识别

---

## ⚙️ 配置说明

### 🛠 基础配置

- `消息冷却时间(分钟)`：防止短时间内重复发送相同消息。

---

### 📄 消息模板

使用与主程序一致的格式与语法，在“正则匹配”模式下可结合提取字段进行动态渲染。

---

### 🔄 分发规则

分发规则决定了消息的处理方式和目标渠道：

> 目标渠道  
> - 消息发往何处

---

### 🌰 正则匹配使用示例

#### 🧩 Extractors 配置

`extractors` 用于从原始消息中提取字段信息：

```yaml
extractors:
  - field: 'title'
    org_msg_title: '.*'  # 将完整标题存储到变量 org_msg_title
  
  - field: 'text'
    torrent_name: '你下载的种子''(?P<torrent_name>.*?)''被管理员删除'
    reason: '原因：(?P<reason>.*)'
```

#### 🧾 正则匹配规则

支持两种捕获组语法：

1. **命名捕获组** `(?P<name>expression)`  
   ```yaml
   example: '原因：(?P<reason>.*)'  # 存储到变量 reason
   # 模板中使用: {% if reason %}{{ reason }}{% endif %}
   ```

2. **普通捕获组** `(expression)`  
   ```yaml
   site_name: '【站点\s+([^\s]+)\s+消息】'  # 存储到变量 site_name
   # 模板中使用: {% if site_name %}{{ site_name }}{% endif %}
   # 注意: 每条正则仅提取第一个匹配组
   ```

---

#### 🗂 MetaBase 配置

将提取的信息绑定到媒体对象上：

```yaml
MetaBase:
  title: 'torrent_name'  # 将 torrent_name 的匹配结果绑定到 title
  tmdbid: 'tmdb_id'      # 可选: 绑定 tmdbid 实现精准识别
```

📌 **注意事项：**

- 必须绑定 `title` 才能获取完整媒体信息
- 未绑定时模板仅可使用 `extractors` 提取的变量
- 更多支持的属性请参考 [MetaInfo](https://github.com/jxxghp/MoviePilot/blob/fcd5ca3fda1992ece6bb2111afa1b75909d0557f/app/schemas/context.py#L6-L61)

---

### 📥 消息聚合

消息聚合功能允许将符合同一规则的多条消息在设定的时间窗口后统一发送。

#### ⚙️ 配置说明

- **排除**：若消息内容匹配此规则，则不会参与聚合。
- **包含**：若消息内容匹配此规则，则会触发聚合操作。
- **等待时间**：在指定的时间窗口内，若有新消息匹配，等待时间会被重置；否则，聚合消息将在超时后自动发送。

#### 💡 关键行为提示

> 🔍
> - 若“包含规则”留空且“等待时间”设为 `0`，则聚合功能将被禁用。
> - 若“等待时间”设为 `0`，则符合条件的消息将被拦截且不进行聚合。

#### 🧪 聚合消息模板中可用变量

| 变量名      | 类型     | 描述                           |
|-------------|----------|--------------------------------|
| messages  | 列表     | 包含所有匹配的消息内容         |
| count     | 数字     | 聚合的消息总数                 |
| first_time| 字符串   | 第一条消息到达的时间            |
| last_time | 字符串   | 最后一条消息到达的时间          |

---

#### 📋 使用示例：站点删种通知

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
```

如需增加新的站点删种规则，只需续写 `field: 'text'` 部分：

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

1. 收集时间内所有符合规则的消息
2. 在等待时间结束后统一发送
3. 如果程序重启，超时的消息会在重启后5分钟分别发送

---

## 📝 注意事项

1. 请确保正则表达式格式正确。