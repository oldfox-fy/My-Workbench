<template>
  <div class="kb-graph-container" :class="[configStore.themeMode]">
    <!-- 顶栏 -->
    <header class="kb-graph-topbar">
      <div class="kb-topbar-left">
        <n-button text class="icon-btn" @click="goKnowledge" title="返回知识库">
          <template #icon><n-icon :size="20"><ArrowBackOutline /></n-icon></template>
        </n-button>
        <span class="kb-graph-title">
          <n-icon :size="18" style="vertical-align:-3px"><GitNetworkOutline /></n-icon>
          知识图谱
        </span>
      </div>
      <div class="kb-topbar-right">
        <n-checkbox v-model:checked="includeTags" @update:checked="reload">显示标签</n-checkbox>
        <n-text depth="3" v-if="graph" class="kb-graph-stats">
          {{ graph.stats.note_count }} 笔记 · {{ graph.stats.edge_count }} 链接
          <span v-if="graph.stats.missing_count">· {{ graph.stats.missing_count }} 未创建</span>
        </n-text>
        <n-button size="small" text @click="reload" title="刷新">
          <template #icon><n-icon><RefreshOutline /></n-icon></template>
        </n-button>
      </div>
    </header>

    <!-- 画布 -->
    <div ref="wrapRef" class="kb-graph-canvas-wrap">
      <n-spin v-if="loading" :show="true" class="kb-graph-loading" />
      <div v-else-if="error" class="kb-graph-empty">{{ error }}</div>
      <div v-else-if="graph && graph.nodes.length === 0" class="kb-graph-empty">
        知识库中还没有笔记，或笔记之间还没有 [[双链]]。<br />
        在笔记里用 <code>[[另一篇笔记]]</code> 建立链接后，这里就会显示关系网络。
      </div>
      <canvas
        v-show="!loading && !error && graph && graph.nodes.length > 0"
        ref="canvasRef"
        @mousedown="onMouseDown"
        @mousemove="onMouseMove"
        @mouseup="onMouseUp"
        @mouseleave="onMouseUp"
        @wheel.prevent="onWheel"
        @dblclick="onDblClick"
      ></canvas>
      <!-- hover 提示 -->
      <div v-if="hoverLabel" class="kb-graph-tip" :style="{ left: tipPos.x + 'px', top: tipPos.y + 'px' }">
        {{ hoverLabel }}
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { useRouter } from 'vue-router'
import { NButton, NIcon, NText, NSpin, NCheckbox, useMessage } from 'naive-ui'
import { ArrowBackOutline, GitNetworkOutline, RefreshOutline } from '@vicons/ionicons5'
import { useConfigStore } from '@/stores/config'
import { useKnowledgeStore } from '@/stores/knowledge'
import { getGraph, type GraphData, type GraphNode } from '@/api/knowledge'

const router = useRouter()
const configStore = useConfigStore()
const kbStore = useKnowledgeStore()
const message = useMessage()

const wrapRef = ref<HTMLDivElement | null>(null)
const canvasRef = ref<HTMLCanvasElement | null>(null)
const graph = ref<GraphData | null>(null)
const loading = ref(false)
const error = ref('')
const includeTags = ref(false)

// 物理节点
interface PNode extends GraphNode { x: number; y: number; vx: number; vy: number }
let nodes: PNode[] = []
let edges: { s: PNode; t: PNode; type: string }[] = []

// 视图变换
let scale = 1
let offsetX = 0
let offsetY = 0

// 交互状态
let dragNode: PNode | null = null
let panning = false
let lastMouse = { x: 0, y: 0 }
const hoverLabel = ref('')
const tipPos = ref({ x: 0, y: 0 })
let hoverNode: PNode | null = null

let rafId = 0
let ctx: CanvasRenderingContext2D | null = null
let alpha = 1  // 模拟退火系数

// 颜色（随主题）
function palette() {
  const dark = configStore.themeMode === 'dark'
  return {
    bg: dark ? '#16161a' : '#fafafa',
    note: '#4a7cf7',
    missing: dark ? '#666' : '#bbb',
    tag: '#e08a3c',
    edge: dark ? 'rgba(150,160,190,0.25)' : 'rgba(80,90,120,0.22)',
    edgeMissing: dark ? 'rgba(150,150,150,0.15)' : 'rgba(150,150,150,0.2)',
    label: dark ? '#cfd3dc' : '#333',
  }
}

async function reload() {
  loading.value = true
  error.value = ''
  try {
    graph.value = await getGraph(includeTags.value)
    initSimulation()
  } catch (e: any) {
    error.value = e.message || '构建图谱失败'
  } finally {
    loading.value = false
  }
}

function initSimulation() {
  if (!graph.value) return
  const w = wrapRef.value?.clientWidth || 800
  const h = wrapRef.value?.clientHeight || 600
  const cx = w / 2, cy = h / 2
  const idMap = new Map<string, PNode>()
  nodes = graph.value.nodes.map((n, i) => {
    const angle = (i / graph.value!.nodes.length) * Math.PI * 2
    const r = Math.min(w, h) * 0.3
    const p: PNode = {
      ...n,
      x: cx + Math.cos(angle) * r + (Math.random() - 0.5) * 40,
      y: cy + Math.sin(angle) * r + (Math.random() - 0.5) * 40,
      vx: 0, vy: 0,
    }
    idMap.set(n.id, p)
    return p
  })
  edges = graph.value.edges
    .map(e => ({ s: idMap.get(e.source)!, t: idMap.get(e.target)!, type: e.type }))
    .filter(e => e.s && e.t)
  scale = 1; offsetX = 0; offsetY = 0
  alpha = 1
  startLoop()
}

// 力导向一步
function tick() {
  const w = wrapRef.value?.clientWidth || 800
  const h = wrapRef.value?.clientHeight || 600
  const cx = w / 2, cy = h / 2
  const REPULSE = 6000
  const SPRING = 0.02
  const SPRING_LEN = 90
  const CENTER = 0.005
  const DAMP = 0.85

  // 斥力（O(n^2)，几百节点足够）
  for (let i = 0; i < nodes.length; i++) {
    const a = nodes[i]
    for (let j = i + 1; j < nodes.length; j++) {
      const b = nodes[j]
      let dx = a.x - b.x, dy = a.y - b.y
      let d2 = dx * dx + dy * dy
      if (d2 < 0.01) { d2 = 0.01; dx = Math.random(); dy = Math.random() }
      const f = REPULSE / d2
      const d = Math.sqrt(d2)
      const fx = (dx / d) * f, fy = (dy / d) * f
      a.vx += fx; a.vy += fy
      b.vx -= fx; b.vy -= fy
    }
  }
  // 引力（边）
  for (const e of edges) {
    const dx = e.t.x - e.s.x, dy = e.t.y - e.s.y
    const d = Math.sqrt(dx * dx + dy * dy) || 1
    const f = (d - SPRING_LEN) * SPRING
    const fx = (dx / d) * f, fy = (dy / d) * f
    e.s.vx += fx; e.s.vy += fy
    e.t.vx -= fx; e.t.vy -= fy
  }
  // 向心 + 阻尼 + 位移
  for (const n of nodes) {
    n.vx += (cx - n.x) * CENTER
    n.vy += (cy - n.y) * CENTER
    n.vx *= DAMP; n.vy *= DAMP
    if (n !== dragNode) {
      n.x += n.vx * alpha
      n.y += n.vy * alpha
    }
  }
  alpha *= 0.997
  if (alpha < 0.03) alpha = 0.03
}

function draw() {
  if (!ctx || !canvasRef.value) return
  const c = palette()
  const cv = canvasRef.value
  ctx.setTransform(1, 0, 0, 1, 0, 0)
  ctx.clearRect(0, 0, cv.width, cv.height)
  ctx.fillStyle = c.bg
  ctx.fillRect(0, 0, cv.width, cv.height)
  const dpr = window.devicePixelRatio || 1
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  ctx.save()
  ctx.translate(offsetX, offsetY)
  ctx.scale(scale, scale)

  // 边
  for (const e of edges) {
    ctx.strokeStyle = e.type === 'missing' ? c.edgeMissing : c.edge
    ctx.lineWidth = 1 / scale
    ctx.beginPath()
    ctx.moveTo(e.s.x, e.s.y)
    ctx.lineTo(e.t.x, e.t.y)
    ctx.stroke()
  }
  // 节点
  for (const n of nodes) {
    const r = nodeRadius(n)
    ctx.beginPath()
    ctx.arc(n.x, n.y, r, 0, Math.PI * 2)
    ctx.fillStyle = n.type === 'missing' ? c.missing : n.type === 'tag' ? c.tag : c.note
    ctx.globalAlpha = n.type === 'missing' ? 0.6 : 1
    ctx.fill()
    ctx.globalAlpha = 1
    if (n === hoverNode) {
      ctx.strokeStyle = '#fff'
      ctx.lineWidth = 2 / scale
      ctx.stroke()
    }
    // 标签（缩放足够大或高连接度时显示）
    if (scale > 0.7 || n.degree >= 3) {
      ctx.fillStyle = c.label
      ctx.font = `${12 / scale}px 'Inter', sans-serif`
      ctx.textAlign = 'center'
      ctx.fillText(n.label, n.x, n.y + r + 12 / scale)
    }
  }
  ctx.restore()
}

function nodeRadius(n: PNode) {
  return 4 + Math.min(n.degree, 10) * 1.2
}

function loop() {
  tick()
  draw()
  rafId = requestAnimationFrame(loop)
}
function startLoop() {
  cancelAnimationFrame(rafId)
  loop()
}

// ---------- 坐标换算 ----------
function toWorld(clientX: number, clientY: number) {
  const rect = canvasRef.value!.getBoundingClientRect()
  const x = (clientX - rect.left - offsetX) / scale
  const y = (clientY - rect.top - offsetY) / scale
  return { x, y }
}

function pickNode(clientX: number, clientY: number): PNode | null {
  const { x, y } = toWorld(clientX, clientY)
  for (let i = nodes.length - 1; i >= 0; i--) {
    const n = nodes[i]
    const r = nodeRadius(n) + 4
    if ((n.x - x) ** 2 + (n.y - y) ** 2 <= r * r) return n
  }
  return null
}

function onMouseDown(e: MouseEvent) {
  const n = pickNode(e.clientX, e.clientY)
  if (n) {
    dragNode = n
    alpha = Math.max(alpha, 0.5)
  } else {
    panning = true
  }
  lastMouse = { x: e.clientX, y: e.clientY }
}

function onMouseMove(e: MouseEvent) {
  if (dragNode) {
    const { x, y } = toWorld(e.clientX, e.clientY)
    dragNode.x = x; dragNode.y = y
    dragNode.vx = 0; dragNode.vy = 0
  } else if (panning) {
    offsetX += e.clientX - lastMouse.x
    offsetY += e.clientY - lastMouse.y
    lastMouse = { x: e.clientX, y: e.clientY }
  } else {
    const n = pickNode(e.clientX, e.clientY)
    hoverNode = n
    if (n) {
      hoverLabel.value = n.type === 'missing' ? `${n.label}（未创建）` : n.id
      const rect = wrapRef.value!.getBoundingClientRect()
      tipPos.value = { x: e.clientX - rect.left + 12, y: e.clientY - rect.top + 12 }
    } else {
      hoverLabel.value = ''
    }
  }
}

function onMouseUp() {
  dragNode = null
  panning = false
}

function onWheel(e: WheelEvent) {
  const rect = canvasRef.value!.getBoundingClientRect()
  const mx = e.clientX - rect.left, my = e.clientY - rect.top
  const factor = e.deltaY < 0 ? 1.1 : 0.9
  const newScale = Math.max(0.15, Math.min(4, scale * factor))
  // 以鼠标为中心缩放
  offsetX = mx - (mx - offsetX) * (newScale / scale)
  offsetY = my - (my - offsetY) * (newScale / scale)
  scale = newScale
}

async function onDblClick(e: MouseEvent) {
  const n = pickNode(e.clientX, e.clientY)
  if (!n) return
  if (n.type === 'note') {
    try {
      await kbStore.openFile(n.id, true)
      router.push('/knowledge')
    } catch (err: any) {
      message.error(err.message || '打开笔记失败')
    }
  } else if (n.type === 'missing') {
    message.info(`「${n.label}」尚未创建`)
  }
}

function goKnowledge() {
  router.push('/knowledge')
}

function resizeCanvas() {
  const cv = canvasRef.value, wrap = wrapRef.value
  if (!cv || !wrap) return
  const dpr = window.devicePixelRatio || 1
  cv.width = wrap.clientWidth * dpr
  cv.height = wrap.clientHeight * dpr
  cv.style.width = wrap.clientWidth + 'px'
  cv.style.height = wrap.clientHeight + 'px'
}

onMounted(async () => {
  await new Promise(r => setTimeout(r, 0))
  ctx = canvasRef.value?.getContext('2d') || null
  resizeCanvas()
  window.addEventListener('resize', resizeCanvas)
  await reload()
})

onBeforeUnmount(() => {
  cancelAnimationFrame(rafId)
  window.removeEventListener('resize', resizeCanvas)
})
</script>

<style scoped>
.kb-graph-container {
  display: flex;
  flex-direction: column;
  height: 100vh;
  background: var(--bg-secondary);
  color: var(--text-primary);
  overflow: hidden;
}
.kb-graph-topbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  height: 48px;
  padding: 0 20px;
  background: var(--glass-bg);
  backdrop-filter: blur(8px);
  border-bottom: var(--glass-border);
  flex-shrink: 0;
}
.kb-topbar-left { display: flex; align-items: center; gap: 10px; }
.kb-topbar-right { display: flex; align-items: center; gap: 14px; }
.kb-graph-title {
  font-size: 1.05rem;
  font-weight: 700;
  background: var(--accent-gradient);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}
.kb-graph-stats { font-size: 0.78rem; }
.icon-btn {
  width: 32px; height: 32px;
  display: flex; align-items: center; justify-content: center;
  border-radius: 50%;
  color: var(--text-primary);
}
.icon-btn:hover { background: rgba(74, 124, 247, 0.2); }

.kb-graph-canvas-wrap {
  flex: 1;
  position: relative;
  overflow: hidden;
}
.kb-graph-canvas-wrap canvas { display: block; cursor: grab; }
.kb-graph-canvas-wrap canvas:active { cursor: grabbing; }
.kb-graph-loading {
  position: absolute;
  top: 50%; left: 50%;
  transform: translate(-50%, -50%);
}
.kb-graph-empty {
  position: absolute;
  top: 50%; left: 50%;
  transform: translate(-50%, -50%);
  text-align: center;
  color: var(--text-secondary);
  font-size: 0.9rem;
  line-height: 1.8;
}
.kb-graph-tip {
  position: absolute;
  z-index: 10;
  padding: 4px 10px;
  background: rgba(0, 0, 0, 0.78);
  color: #fff;
  font-size: 0.75rem;
  border-radius: 6px;
  pointer-events: none;
  white-space: nowrap;
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
