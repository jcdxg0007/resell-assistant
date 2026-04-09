import React, { lazy, Suspense } from 'react';
import { createBrowserRouter, Navigate } from 'react-router-dom';
import { Spin } from 'antd';
import MainLayout from './layouts/MainLayout';

const Loading = () => (
  <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: '100%', minHeight: 300 }}>
    <Spin size="large" />
  </div>
);

const lazyLoad = (factory: () => Promise<{ default: React.ComponentType }>) => (
  <Suspense fallback={<Loading />}>{React.createElement(lazy(factory))}</Suspense>
);

const router = createBrowserRouter([
  {
    path: '/login',
    element: lazyLoad(() => import('./pages/login')),
  },
  {
    path: '/',
    element: <MainLayout />,
    children: [
      { index: true, element: <Navigate to="/dashboard" replace /> },
      { path: 'dashboard', element: lazyLoad(() => import('./pages/dashboard')) },
      { path: 'selection/xianyu', element: lazyLoad(() => import('./pages/selection/XianyuSelection')) },
      { path: 'selection/xhs', element: lazyLoad(() => import('./pages/selection/XhsSelection')) },
      { path: 'selection/virtual', element: lazyLoad(() => import('./pages/selection/VirtualSelection')) },
      { path: 'sales/xianyu', element: lazyLoad(() => import('./pages/sales/XianyuWorkbench')) },
      { path: 'sales/xhs', element: lazyLoad(() => import('./pages/sales/XhsWorkbench')) },
      { path: 'orders', element: lazyLoad(() => import('./pages/orders')) },
      { path: 'customer', element: lazyLoad(() => import('./pages/customer')) },
      { path: 'ai-ops', element: lazyLoad(() => import('./pages/ai-ops')) },
      { path: 'accounts', element: lazyLoad(() => import('./pages/accounts')) },
      { path: 'settings', element: lazyLoad(() => import('./pages/settings')) },
    ],
  },
]);

export default router;
