<template>
  <div class="page-root">
    <div class="top-nav">
      <div class="nav-left">
        <slot name="nav-left">
          <button class="btn-collapse" type="button" title="收起侧边栏" @click="$emit('toggle-sidebar')">
            <IconChevronLeft />
          </button>
        </slot>
      </div>
      <div class="nav-center">
        <slot name="nav-center" />
      </div>
      <div class="nav-right">
        <slot name="nav-right">
          <ChatUserMenu />
        </slot>
      </div>
    </div>
    <slot />
  </div>
</template>

<script setup lang="ts">
import ChatUserMenu from './ChatUserMenu.vue'
import IconChevronLeft from './icons/IconChevronLeft.vue'

defineEmits<{ 'toggle-sidebar': [] }>()
</script>

<style scoped>
.page-root {
  height: 100vh;
  display: flex;
  flex-direction: column;
  background: #f6f6f4;
  color: #1f2328;
  font-family: 'PingFang SC', 'Microsoft YaHei', system-ui, -apple-system, sans-serif;
  --dp-primary: #4f5bd5;
  --dp-primary-solid: #4f5bd5;
  --dp-primary-bg: #eef0fc;
}

.top-nav {
  height: 52px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 0 20px;
  border-bottom: 1px solid rgba(15, 23, 42, 0.06);
  background: #f6f6f4;
  flex-shrink: 0;
}

.nav-left {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 220px;
}

.nav-center {
  display: flex;
  align-items: center;
  justify-content: center;
  flex: 1;
  min-width: 0;
}

.nav-right {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  min-width: 160px;
}

.btn-collapse {
  width: 36px;
  height: 36px;
  border: none;
  background: transparent;
  border-radius: 8px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #333;
}

.btn-collapse:hover {
  background: #f5f5f5;
}

@media (max-width: 700px) {
  .top-nav {
    padding: 0 10px;
  }

  .nav-left,
  .nav-right {
    min-width: 0;
  }
}
</style>
