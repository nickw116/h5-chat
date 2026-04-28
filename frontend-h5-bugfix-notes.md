# H5 Frontend Fix Notes

当前环境不能直接写 `/root/h5-frontend`，所以把需要改的内容整理在这里。

## 1. `/root/h5-frontend/src/utils/format.js`

把 `extractText()` 改成同时兼容：

- `entry.content` 为字符串
- `entry.content` 为 parts 数组
- `entry.message.content` / `entry.item.content` / `entry.payload.content`
- parts 中 `text` / `content` / `value`

可替换为：

```js
export function extractText(entry) {
  if (!entry) return ''
  if (typeof entry === 'string') return entry

  const unwrap = entry.message || entry.item || entry.data || entry.payload || entry
  const content = unwrap.content ?? unwrap.text ?? unwrap.value
  if (typeof content === 'string') return content
  if (Array.isArray(content)) {
    return content
      .map((p) => {
        if (typeof p === 'string') return p
        if (!p || typeof p !== 'object') return ''
        if (typeof p.text === 'string') return p.text
        if (typeof p.content === 'string') return p.content
        if (Array.isArray(p.content)) return extractText({ content: p.content })
        if (typeof p.value === 'string') return p.value
        return ''
      })
      .filter(Boolean)
      .join('')
  }
  if (content && typeof content === 'object') return extractText(content)
  return unwrap.text || entry.text || ''
}
```

## 2. `/root/h5-frontend/src/composables/useChat.js`

### 新增辅助函数

放在常量区附近：

```js
function publicSessionKey(value) {
  const text = String(value || '')
  if (!text.startsWith('agent:')) return text
  const parts = text.split(':')
  return parts.length >= 3 ? parts.slice(2).join(':') : text
}

function isSameSessionKey(left, right) {
  return publicSessionKey(left) === publicSessionKey(right)
}

function historyField(entry, key) {
  for (const source of [entry, entry?.message, entry?.item, entry?.data, entry?.payload]) {
    if (source && typeof source === 'object' && source[key] != null) return source[key]
  }
  return ''
}

function normalizeHistoryRole(entry) {
  const raw = String(
    historyField(entry, 'role') ||
    historyField(entry, 'author') ||
    historyField(entry, 'speaker') ||
    historyField(entry, 'type') ||
    ''
  ).trim().toLowerCase()

  if (['user', 'human', 'client', 'customer', 'input', 'request'].includes(raw)) return 'user'
  if (['assistant', 'ai', 'agent', 'model', 'bot', 'response'].includes(raw)) return 'assistant'
  if (raw.includes('user') || raw.includes('human')) return 'user'
  if (raw.includes('assistant') || raw.includes('agent') || raw.includes('model')) return 'assistant'
  return ''
}

function updateAcpStatus(acpStatus, payload = {}) {
  const runs = Array.isArray(payload.activeAcpRuns) ? payload.activeAcpRuns : []
  const count = Number(payload.activeAcpCount)
  acpStatus.runs = runs
  acpStatus.count = Number.isFinite(count)
    ? count
    : runs.filter((run) => !DONE_PHASES.has(String(run.status || '').toLowerCase())).length
}
```

### 在 `useChat()` 状态里新增 `acpStatus`

```js
const acpStatus = reactive({ count: 0, runs: [] })
```

### 修改 `loadHistory()`

把当前这段：

```js
messages.value = entries
  .filter((e) => e.role === 'user' || e.role === 'assistant')
  .filter((e) => {
    const t = extractText(e).trim()
    return t && !FILTERED_MESSAGES.includes(t)
  })
  .map((e, i) => {
    const content = extractText(e)
    return {
      id: i,
      role: e.role,
      ...
      media: e.role === 'assistant' ? detectMediaUrls(content) : createEmptyMedia(),
    }
  })
```

改成：

```js
messages.value = entries
  .map((e) => ({ entry: e, role: normalizeHistoryRole(e) }))
  .filter(({ role }) => role === 'user' || role === 'assistant')
  .filter(({ entry }) => {
    const t = extractText(entry).trim()
    return t && !FILTERED_MESSAGES.includes(t)
  })
  .map(({ entry, role }, i) => {
    const content = extractText(entry)
    return {
      id: i,
      role,
      content,
      acpContent: '',
      acpExpanded: true,
      acpSteps: [],
      files: [],
      media: role === 'assistant' ? detectMediaUrls(content) : createEmptyMedia(),
      steps: [],
    }
  })
```

### 修改 `handleStreamEvent()`

把 session 过滤：

```js
if (event.sessionKey && event.sessionKey !== sessionKey.value) return
```

改成：

```js
if (event.sessionKey && !isSameSessionKey(event.sessionKey, sessionKey.value)) return
```

并在 snapshot 前加入：

```js
if (kind === 'acp.state') {
  updateAcpStatus(acpStatus, payload)
  return
}
```

在 `kind === 'snapshot'` 分支里，读取 `snap` 后补一行：

```js
updateAcpStatus(acpStatus, snap)
```

### `return` 导出里补上

```js
acpStatus,
```

## 3. `/root/h5-frontend/src/App.vue`

### `useChat()` 解构返回值时补上

```js
acpStatus,
```

### 给 `ChatPage` 传入 prop

```vue
:acp-status="acpStatus"
```

## 4. 构建与发布

应用以上前端改动后执行：

```bash
cd /root/h5-frontend
npm run build
cp -r dist/* /var/www/chat/
```

bridge 重启命令：

```bash
systemctl restart h5-bridge
```
