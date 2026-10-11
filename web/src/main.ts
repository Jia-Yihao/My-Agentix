import { createApp } from 'vue'

import App from './App.vue'
import { router } from './router'
// R4.2 统一入口：双模块共享单一 createApp / router 实例。
// 全局样式：knowledge 基础主题在前，chat 的 body 背景在后者生效（两模块视图均自带容器背景）。
import './modules/knowledge/style.css'
import './modules/chat/styles/global.scss'
import { initThemeFromStorage } from './modules/knowledge/utils/theme'

initThemeFromStorage()
createApp(App).use(router).mount('#app')
