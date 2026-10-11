<template>
  <Teleport to="body">
    <div class="toast-host" aria-live="polite">
      <transition-group name="toast">
        <div
          v-for="item in items"
          :key="item.id"
          class="toast-item"
          :class="`is-${item.type}`"
          role="status"
          @click="dismiss(item.id)"
        >
          {{ item.message }}
        </div>
      </transition-group>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { useToast } from '../composables/useToast'

const { items, dismiss } = useToast()
</script>

<style scoped>
.toast-host {
  position: fixed;
  top: 20px;
  left: 50%;
  transform: translateX(-50%);
  z-index: 9999;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  pointer-events: none;
}

.toast-item {
  pointer-events: auto;
  max-width: min(520px, 88vw);
  padding: 9px 16px;
  border-radius: 8px;
  font-size: 13px;
  line-height: 1.5;
  color: #fff;
  background: rgba(30, 30, 30, 0.92);
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.18);
  cursor: pointer;
}

.toast-item.is-success {
  background: var(--dp-primary, #18a058);
}

.toast-item.is-error {
  background: #c0392b;
}

.toast-item.is-info {
  background: rgba(30, 30, 30, 0.92);
}

.toast-enter-active,
.toast-leave-active {
  transition: all 0.22s ease;
}

.toast-enter-from,
.toast-leave-to {
  opacity: 0;
  transform: translateY(-8px);
}
</style>
