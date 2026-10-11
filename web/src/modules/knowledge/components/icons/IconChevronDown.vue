<template>
  <svg :width="size" :height="size" viewBox="0 0 24 24" fill="none" stroke="currentColor" :stroke-width="strokeWidth">
    <polyline points="6 9 12 15 18 9" />
  </svg>
</template>

<script setup lang="ts">
withDefaults(defineProps<{ size?: number; strokeWidth?: number | string }>(), { size: 12, strokeWidth: 2 })
</script>
