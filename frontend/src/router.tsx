import React, { lazy, Suspense } from 'react';
import { createBrowserRouter, Navigate, useLocation, useNavigate, isRouteErrorResponse, useRouteError } from 'react-router-dom';
import { Spin, Result, Button } from 'antd';
import MainLayout from './layouts/MainLayout';

const Loading = () => (
  <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: '100%', minHeight: 300 }}>
    <Spin size="large" />
  </div>
);

function RouteError() {
  const navigate = useNavigate();
  const error = useRouteError();
  const is404 = isRouteErrorResponse(error) && error.status === 404;
  return (
    <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: '100vh' }}>
      <Result
        status={is404 ? '404' : 'error'}
        title={is404 ? '404' : '页面出错了'}
        subTitle={is404 ? '抱歉，你访问的页面不存在或已下线。' : '抱歉，页面发生了一些问题。'}
        extra={
          <Button type="primary" onClick={() => navigate('/dashboard', { replace: true })}>
            返回工作台
          </Button>
        }
      />
    </div>
  );
}

const lazyLoad = (factory: () => Promise<{ default: React.ComponentType }>) => (
  <Suspense fallback={<Loading />}>{React.createElement(lazy(factory))}</Suspense>
);

function AuthGuard({ children }: { children: React.ReactNode }) {
  const location = useLocation();
  const token = localStorage.getItem('token');
  if (!token) {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }
  return <>{children}</>;
}

const router = createBrowserRouter([
  {
    path: '/login',
    element: lazyLoad(() => import('./pages/login')),
  },
  {
    path: '/',
    element: (
      <AuthGuard>
        <MainLayout />
      </AuthGuard>
    ),
    errorElement: <RouteError />,
    children: [
      { index: true, element: <Navigate to="/dashboard" replace /> },
      { path: 'dashboard', element: lazyLoad(() => import('./pages/dashboard')) },
      { path: 'selection/xianyu', element: lazyLoad(() => import('./pages/selection/MultiPlatformCompare')) },
      { path: 'selection/pdd-keywords', element: lazyLoad(() => import('./pages/selection/PddKeywords')) },
      { path: 'selection/xhs', element: lazyLoad(() => import('./pages/selection/XhsSelection')) },
      { path: 'selection/virtual', element: lazyLoad(() => import('./pages/selection/VirtualSelection')) },
      { path: 'sales/xianyu', element: lazyLoad(() => import('./pages/sales/XianyuWorkbench')) },
      { path: 'sales/xhs', element: lazyLoad(() => import('./pages/sales/XhsWorkbench')) },
      { path: 'orders', element: lazyLoad(() => import('./pages/orders')) },
      { path: 'customer', element: lazyLoad(() => import('./pages/customer')) },
      { path: 'ai-ops', element: lazyLoad(() => import('./pages/ai-ops')) },
      { path: 'accounts', element: lazyLoad(() => import('./pages/accounts')) },
      { path: 'settings', element: lazyLoad(() => import('./pages/settings')) },
      { path: '*', element: <Navigate to="/dashboard" replace /> },
    ],
  },
]);

export default router;
