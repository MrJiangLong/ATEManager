<template>
  <el-aside :width="asideWidth" class="sidebar" :class="{ collapsed: isCollapsed }">
    <div class="logo">
      <AppLogoMark :size="42" />
      <div class="logo-text">
        <span class="logo-title" :title="$t('app.name')">{{ $t('app.name') }}</span>
        <span class="logo-sub">{{ $t('app.sub') }}</span>
      </div>
    </div>

    <el-menu
      :default-active="activeMenu"
      router
      :collapse="isCollapsed"
      :collapse-transition="false"
      class="side-menu"
      background-color="transparent"
      text-color="#aab6cf"
      active-text-color="#ffffff"
    >
      <el-menu-item v-for="item in menuItems" :key="item.path" :index="item.path">
        <el-icon><component :is="item.icon" /></el-icon>
        <template #title>{{ $t(item.labelKey) }}</template>
      </el-menu-item>
    </el-menu>

    <div class="sidebar-footer">
      <div v-if="!isCollapsed" class="version-card">
        <div class="version-left">
          <span class="version-dot" />
          <span class="version-title">{{ $t('layout.systemVersion') }}</span>
        </div>
        <span class="version-num">v{{ version || '--' }}</span>
      </div>
      <el-tooltip v-else :content="'v' + (version || '--')" placement="right">
        <div class="version-dot-mini" />
      </el-tooltip>
    </div>
  </el-aside>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { Connection, FirstAidKit, Monitor, Notebook, PieChart, Setting, Tickets } from '@element-plus/icons-vue'
import AppLogoMark from '../components/AppLogoMark.vue'

const props = defineProps({
  collapsed: { type: Boolean, default: false },
  version: { type: String, default: '--' },
})

const route = useRoute()

/* 窄屏强制收起：若仅在样式里把容器压到 64px 而 el-menu 仍是展开态，
   菜单文字会被 overflow:hidden 裁成半截，因此必须与折叠状态联动而非纯 CSS 覆盖 */
const NARROW_QUERY = '(max-width: 900px)'
const narrow = ref(false)
let narrowMedia = null

function onNarrowChange(event) {
  narrow.value = event.matches
}

onMounted(() => {
  narrowMedia = window.matchMedia(NARROW_QUERY)
  narrow.value = narrowMedia.matches
  narrowMedia.addEventListener('change', onNarrowChange)
})

onBeforeUnmount(() => {
  narrowMedia?.removeEventListener('change', onNarrowChange)
})

const isCollapsed = computed(() => props.collapsed || narrow.value)
const asideWidth = computed(() => (isCollapsed.value ? '64px' : '232px'))

/* Trace 为在制列表的深链入口：高亮"在制管理"，自身不出现在菜单中 */
const MENU_PARENT = { '/trace': '/products' }

const activeMenu = computed(() => {
  const path = route.path
  const hit = Object.keys(MENU_PARENT).find((p) => path === p || path.startsWith(p + '/'))
  return hit ? MENU_PARENT[hit] : path
})

/* 菜单：扁平结构，无业务域分组（与目标项目保持单层结构） */
const menuItems = [
  { path: '/dashboard', labelKey: 'menu.dashboard', icon: PieChart },
  { path: '/products',  labelKey: 'menu.products',  icon: Tickets },
  { path: '/records',   labelKey: 'menu.records',   icon: Notebook },
  { path: '/repairs',   labelKey: 'menu.repairs',   icon: FirstAidKit },
  { path: '/clients',   labelKey: 'menu.clients',   icon: Monitor },
  { path: '/sessions',  labelKey: 'menu.sessions',  icon: Connection },
  { path: '/configs',   labelKey: 'menu.configs',   icon: Setting },
]
</script>

<style scoped>
/* ===== 侧边栏 ===== */
.sidebar {
  /* 光学中轴：收起态导轨宽 64px，Logo / 菜单图标 / 版本圆点的中心统一落在 32px。
     展开态据此反推各自的左内边距，使折叠切换时不产生横向跳动 */
  --sidebar-rail: 64px;
  --sidebar-axis: calc(var(--sidebar-rail) / 2);
  --sidebar-logo: 42px;
  --sidebar-gutter: 16px;
  --sidebar-dot: 6px;

  background: linear-gradient(180deg, #0e1b3a 0%, #14274f 100%);
  display: flex;
  flex-direction: column;
  color: #fff;
  overflow: hidden;
  position: relative;
  transition: width 0.25s ease;
}
.sidebar::after {
  content: '';
  position: absolute;
  inset: 0;
  background: radial-gradient(600px 300px at 20% -10%, rgba(90, 139, 255, 0.18), transparent 70%);
  pointer-events: none;
}

/* ===== 折叠态 ===== */
.sidebar.collapsed .logo {
  justify-content: center;
  /* 复位展开态用于对齐中轴的左内边距，让 42px Logo 在 64px 导轨内居中 */
  padding: 20px 0 16px;
}
.sidebar.collapsed .logo-text,
.sidebar.collapsed .version-title,
.sidebar.collapsed .version-num { display: none; }
/* 钉死收起态菜单宽度：EP 的 .el-menu--collapse 宽度公式复用了上面被收窄的
   --el-menu-base-level-padding（8px），不钉死会被算成 24 + 8*2 = 40px，
   条目只剩 20px 宽，激活项缩成一根细胶囊 */
.sidebar.collapsed .side-menu { width: 100%; }
.sidebar.collapsed .side-menu :deep(.el-menu-item) {
  margin: 5px 10px;
  /* 抵消 Element Plus 的层级内边距，使 24px 图标盒在 44px 方块内真正居中。
     此处必须 !important：.el-menu--vertical:not(.el-menu--collapse):not(...)
     .el-menu-item 的选择器权重高于本文件的深度选择器 */
  padding: 0 !important;
  justify-content: center;
}
/* 折叠态 EP 会把菜单项内容自动包进 ElTooltip 的触发层，该层默认
   position:absolute; left:0; width:100%; padding:0 var(--el-menu-base-level-padding)
   且内部左对齐——li 上的 justify-content 够不到它内部的图标，导致图标整体偏左。
   让触发层回归文档流并收缩为内容宽，交还给 li 的 justify-content 居中 */
.sidebar.collapsed .side-menu :deep(.el-menu-tooltip__trigger) {
  position: static;
  width: auto;
  padding: 0;
  justify-content: center;
}
.sidebar.collapsed .sidebar-footer { padding: 12px; }

.version-dot-mini {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #12b76a;
  box-shadow: 0 0 10px #12b76a;
  margin: 0 auto;
}

/* ===== Logo ===== */
.logo {
  display: flex;
  align-items: center;
  gap: 10px;
  /* 左内边距 = 中轴 − Logo 半宽（32 − 21 = 11px），使 Logo 中心与图标同轴 */
  padding: 20px var(--sidebar-gutter) 16px calc(var(--sidebar-axis) - var(--sidebar-logo) / 2);
}
.logo-text {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  line-height: 1.35;
}
.logo-title {
  font-size: 14px;
  font-weight: 700;
  letter-spacing: 0.5px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.logo-sub {
  font-size: 9.5px;
  color: #6f81a8;
  letter-spacing: 1.5px;
  margin-top: 3px;
}

/* ===== 菜单 ===== */
.side-menu {
  /* 收窄 Element Plus 默认的 20px 层级内边距：12px 外边距 + 8px 内边距 = 20px，
     使 24px 图标盒的中心正好落在 32px 中轴上。
     这里覆盖的是 CSS 变量而非 padding 本身——EP 的
     .el-menu--vertical:not(.el-menu--collapse):not(.el-menu--popup-container) .el-menu-item
     选择器权重为 (0,4,0)，高于本文件 (0,3,0) 的深度选择器，直接写 padding 会被压过 */
  --el-menu-base-level-padding: 8px;

  border-right: none;
  flex: 1;
  position: relative;
  z-index: 1;
  overflow-x: hidden;
}
.side-menu :deep(.el-menu-item) {
  height: 46px;
  margin: 5px 12px;
  border-radius: 10px;
  font-size: 14px;
  transition: all 0.2s;
}
.side-menu :deep(.el-menu-item:hover) {
  background: rgba(255, 255, 255, 0.07);
}
.side-menu :deep(.el-menu-item.is-active) {
  background: linear-gradient(135deg, #2f6bff 0%, #5a8bff 100%);
  box-shadow: 0 6px 16px rgba(47, 107, 255, 0.4);
}

/* ===== 版本卡片 ===== */
.sidebar-footer {
  padding: 16px;
  border-top: 1px solid rgba(255, 255, 255, 0.08);
  position: relative;
  z-index: 1;
}
.version-card {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: rgba(255, 255, 255, 0.06);
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 10px;
  /* 左内边距 = 中轴 − 页脚留白 − 圆点半宽（32 − 16 − 3 = 13px） */
  padding: 8px 12px 8px calc(var(--sidebar-axis) - var(--sidebar-gutter) - var(--sidebar-dot) / 2);
  backdrop-filter: blur(8px);
  transition: all 0.25s ease;
}
.version-card:hover {
  background: rgba(47, 107, 255, 0.18);
  border-color: rgba(90, 139, 255, 0.4);
}
.version-left {
  display: flex;
  align-items: center;
  gap: 8px;
}
.version-dot {
  width: var(--sidebar-dot);
  height: var(--sidebar-dot);
  border-radius: 50%;
  background: #12b76a;
  box-shadow: 0 0 8px #12b76a;
}
.version-title {
  font-size: 12.5px;
  color: #b0c2e8;
  font-weight: 500;
}
.version-num {
  font-size: 12px;
  font-weight: 700;
  color: #ffffff;
  background: linear-gradient(135deg, #2f6bff 0%, #5a8bff 100%);
  padding: 2px 8px;
  border-radius: 6px;
  letter-spacing: 0.5px;
  box-shadow: 0 2px 8px rgba(47, 107, 255, 0.4);
}
</style>
