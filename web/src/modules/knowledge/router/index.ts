// R4.2：knowledge 模块不再自建 router 实例（单入口单 router），
// 这里只导出挂载到根 router 的模块路由表（统一 /knowledge 前缀）。
import HistoryView from '../views/HistoryView.vue'
import HomeView from '../views/HomeView.vue'
import PersonalKbView from '../views/PersonalKbView.vue'
import VectorDbView from '../views/VectorDbView.vue'

export const knowledgeRoutes = [
  { path: '/knowledge', redirect: { name: 'home' } },
  { path: '/knowledge/home', name: 'home', component: HomeView },
  { path: '/knowledge/kb', name: 'kb', component: PersonalKbView },
  { path: '/knowledge/history', name: 'history', component: HistoryView },
  { path: '/knowledge/vector-db', name: 'vector-db', component: VectorDbView },
]
