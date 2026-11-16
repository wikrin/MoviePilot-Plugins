const collator = new Intl.Collator('zh-CN', { numeric: true, sensitivity: 'base' })
const normalize = value => String(value || '').replaceAll('\\', '/')

/** Map 构建目录，特殊文件名如 __proto__ 不会污染对象原型。 */
export function buildTorrentTree(
  snapshot,
  changes = [],
  { onlyChanged = false, search = '', operations = [], actual = false } = {},
) {
  const rootPath = normalize(snapshot?.save_path)
  const fileChanges = new Map(changes.filter(item => item.kind === 'file').map(item => [item.file_id, item]))
  const logs = new Map(operations.map(item => [item.change.id, item]))
  const saveChange = changes.find(item => item.kind === 'save_path' && item.status === 'changed')
  const locationChanged = Boolean(
    saveChange && (!actual || rootPath.replace(/\/$/, '') !== normalize(saveChange.before).replace(/\/$/, '')),
  )
  const root = {
    id: 'root',
    parentId: null,
    name: rootPath || '保存目录',
    path: rootPath,
    type: 'folder',
    children: [],
    changed: locationChanged,
    count: 0,
    relatedCount: 0,
  }
  const folders = new Map([['', root]])
  const usedIds = new Set()
  for (const [position, file] of (snapshot?.files || []).entries()) {
    const relative = normalize(file.name)
    const parts = relative.split('/').filter(Boolean)
    const name = parts.pop() || relative
    let parent = root
    let key = ''
    for (const part of parts) {
      key = key ? `${key}/${part}` : part
      if (!folders.has(key)) {
        const node = {
          id: `folder:${key}`,
          parentId: parent.id,
          name: part,
          path: `${rootPath.replace(/\/$/, '')}/${key}`,
          type: 'folder',
          children: [],
          changed: false,
          count: 0,
          relatedCount: 0,
        }
        folders.set(key, node)
        parent.children.push(node)
      }
      parent = folders.get(key)
    }
    const change =
      fileChanges.get(file.index) || changes.find(item => item.kind === 'file' && item.id === `file:${position}`)
    const log = change ? logs.get(change.id) : null
    const status =
      actual && change?.status === 'changed' && normalize(file.name) === normalize(change.before)
        ? 'unchanged'
        : change?.status || 'unchanged'
    const related = status !== 'unchanged' || locationChanged || Boolean(log && log.status !== 'confirmed')
    let id = `file:${file.index ?? position}`
    if (usedIds.has(id)) id += `:${position}`
    usedIds.add(id)
    parent.children.push({
      id,
      parentId: parent.id,
      fileId: file.index ?? position,
      name,
      path: `${rootPath.replace(/\/$/, '')}/${relative}`,
      relative,
      type: 'file',
      size: file.size,
      status,
      related,
      reason: log?.message || change?.reason || '',
      operationStatus: log?.status || '',
      locationChanged,
      selected: file.selected !== false,
    })
  }
  const term = search.trim().toLocaleLowerCase()
  function prune(node) {
    if (node.type === 'file') {
      return (!onlyChanged || node.related) && (!term || node.path.toLocaleLowerCase().includes(term)) ? node : null
    }
    node.children = node.children
      .map(prune)
      .filter(Boolean)
      .sort((a, b) => (a.type === b.type ? collator.compare(a.name, b.name) : a.type === 'folder' ? -1 : 1))
    node.count = node.children.reduce((count, item) => count + (item.type === 'file' ? 1 : item.count), 0)
    node.relatedCount = node.children.reduce(
      (count, item) => count + (item.type === 'file' ? Number(item.related) : item.relatedCount),
      0,
    )
    node.changed = node.changed || node.relatedCount > 0
    return node.id === 'root' || node.children.length ? node : null
  }
  return prune(root)
}

/** 展开状态只决定可见行，DOM 由树组件窗口化渲染。 */
export function flattenVisibleTree(root, expanded = new Set(['root'])) {
  const rows = []
  function visit(node, depth, position = 1, siblingCount = 1) {
    rows.push({ ...node, depth, position, siblingCount })
    if (node.type === 'folder' && expanded.has(node.id))
      node.children.forEach((child, index) => visit(child, depth + 1, index + 1, node.children.length))
  }
  if (root) visit(root, 0)
  return rows
}

export function folderIds(root) {
  const ids = []
  function visit(node) {
    if (node.type === 'folder') {
      ids.push(node.id)
      node.children.forEach(visit)
    }
  }
  if (root) visit(root)
  return ids
}

/** 线性前后缀差异，用 Vue 文本节点呈现，不构造 HTML。 */
export function diffSegments(before = '', after = '') {
  const left = Array.from(String(before)),
    right = Array.from(String(after))
  let prefix = 0,
    suffix = 0
  while (prefix < Math.min(left.length, right.length) && left[prefix] === right[prefix]) prefix++
  while (
    suffix < Math.min(left.length, right.length) - prefix &&
    left[left.length - suffix - 1] === right[right.length - suffix - 1]
  )
    suffix++
  return {
    prefix: left.slice(0, prefix).join(''),
    removed: left.slice(prefix, left.length - suffix).join(''),
    added: right.slice(prefix, right.length - suffix).join(''),
    suffix: suffix ? left.slice(-suffix).join('') : '',
  }
}

export const CHANGE_LABELS = {
  changed: '将修改',
  unchanged: '保留',
  skipped: '跳过',
  unsupported: '不支持',
  invalid: '异常',
}
export const CHANGE_COLORS = {
  changed: 'primary',
  unchanged: 'default',
  skipped: 'warning',
  unsupported: 'warning',
  invalid: 'error',
}
export const OPERATION_LABELS = { pending: '未执行', confirmed: '已确认', failed: '失败', unknown: '未确认' }
export const RESULT_LABELS = { success: '已完成', partial: '部分执行', failed: '执行失败', verifying: '确认中' }
export const RESULT_COLORS = { success: 'success', partial: 'warning', failed: 'error', verifying: 'info' }
