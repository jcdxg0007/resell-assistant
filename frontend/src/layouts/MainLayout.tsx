import React, { useState } from 'react';
import { Outlet, useNavigate, useLocation } from 'react-router-dom';
import { Layout, Menu, theme, Typography } from 'antd';
import {
  DashboardOutlined,
  SearchOutlined,
  ShopOutlined,
  ShoppingCartOutlined,
  UserOutlined,
  RobotOutlined,
  CustomerServiceOutlined,
  SettingOutlined,
  AppstoreOutlined,
} from '@ant-design/icons';

const { Header, Sider, Content } = Layout;

const menuItems = [
  { key: '/dashboard', icon: <DashboardOutlined />, label: '工作台' },
  {
    key: 'selection', icon: <SearchOutlined />, label: '选品助手',
    children: [
      { key: '/selection/xianyu', label: '闲鱼比价' },
      { key: '/selection/xhs', label: '小红书选品' },
      { key: '/selection/virtual', label: '虚拟商品' },
    ],
  },
  {
    key: 'sales', icon: <ShopOutlined />, label: '销售助手',
    children: [
      { key: '/sales/xianyu', label: '闲鱼工作台' },
      { key: '/sales/xhs', label: '小红书工作台' },
    ],
  },
  { key: '/orders', icon: <ShoppingCartOutlined />, label: '订单管理' },
  { key: '/customer', icon: <CustomerServiceOutlined />, label: '客服中心' },
  { key: '/ai-ops', icon: <RobotOutlined />, label: 'AI运营' },
  { key: '/accounts', icon: <UserOutlined />, label: '账号管理' },
  { key: '/settings', icon: <SettingOutlined />, label: '系统设置' },
];

const MainLayout: React.FC = () => {
  const [collapsed, setCollapsed] = useState(false);
  const navigate = useNavigate();
  const location = useLocation();
  const { token: { colorBgContainer, borderRadiusLG } } = theme.useToken();

  const selectedKeys = [location.pathname];
  const openKeys = menuItems
    .filter(item => 'children' in item && item.children?.some(c => location.pathname.startsWith(c.key)))
    .map(item => item.key);

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider
        collapsible
        collapsed={collapsed}
        onCollapse={setCollapsed}
        theme="light"
        width={220}
        style={{ borderRight: '1px solid #f0f0f0' }}
      >
        <div style={{ height: 64, display: 'flex', alignItems: 'center', justifyContent: 'center', borderBottom: '1px solid #f0f0f0' }}>
          <AppstoreOutlined style={{ fontSize: 24, color: '#1677ff' }} />
          {!collapsed && <Typography.Title level={5} style={{ margin: '0 0 0 8px', whiteSpace: 'nowrap' }}>转卖助手</Typography.Title>}
        </div>
        <Menu
          mode="inline"
          selectedKeys={selectedKeys}
          defaultOpenKeys={openKeys}
          items={menuItems}
          onClick={({ key }) => navigate(key)}
          style={{ borderRight: 0 }}
        />
      </Sider>
      <Layout>
        <Header style={{ padding: '0 24px', background: colorBgContainer, borderBottom: '1px solid #f0f0f0', display: 'flex', alignItems: 'center', justifyContent: 'flex-end' }}>
          <Typography.Text type="secondary">v0.1.0</Typography.Text>
        </Header>
        <Content style={{ margin: 16, padding: 24, background: colorBgContainer, borderRadius: borderRadiusLG, overflow: 'auto' }}>
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  );
};

export default MainLayout;
