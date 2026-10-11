import { createRouter, createWebHistory } from 'vue-router'

import { knowledgeRoutes } from '../modules/knowledge/router'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: { name: 'home' } },
    // R4.5 旧入口兼容重定向
    { path: '/kb', redirect: { name: 'kb' } },
    { path: '/history', redirect: { name: 'history' } },
    { path: '/vector-db', redirect: { name: 'vector-db' } },
    { path: '/knowledge/home', redirect: { name: 'home' } },
    ...knowledgeRoutes,
    { path: '/:pathMatch(.*)*', redirect: { name: 'home' } },
  ],
})
