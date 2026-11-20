import { createRouter, createWebHistory } from 'vue-router'

import ExportDetailView from '@/views/ExportDetailView.vue'
import Overview from '@/views/Overview.vue'
import SubscriptionWorkbenchView from '@/views/SubscriptionWorkbenchView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    // 具体路径必须排在动态模块路由之前，否则会被 /:moduleKey 抢先匹配。
    {
      path: '/report_subscription/workbench',
      name: 'subscription-workbench',
      component: SubscriptionWorkbenchView,
    },
    {
      path: '/adhoc_export/:taskId/detail',
      name: 'export-detail',
      component: ExportDetailView,
      props: true,
    },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
