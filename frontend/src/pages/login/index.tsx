import React, { useState } from 'react';
import { Button, Card, Form, Input, Typography, message } from 'antd';
import { useNavigate, useLocation } from 'react-router-dom';
import api from '../../services/api';

const { Title, Text } = Typography;

const Login: React.FC = () => {
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();
  const location = useLocation();

  const onFinish = async (values: { username: string; password: string }) => {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      params.append('username', values.username);
      params.append('password', values.password);

      const res = await api.post('/auth/login', params, {
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      });

      localStorage.setItem('token', res.data.access_token);
      localStorage.setItem('user', JSON.stringify(res.data.user));
      message.success(`欢迎回来，${res.data.user.display_name || res.data.user.username}`);

      const from = (location.state as { from?: { pathname: string } })?.from?.pathname || '/dashboard';
      navigate(from, { replace: true });
    } catch (err: unknown) {
      const error = err as { response?: { data?: { detail?: string } } };
      message.error(error.response?.data?.detail || '登录失败，请检查用户名和密码');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div
      style={{
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: 24,
        background: 'linear-gradient(160deg, #f0f5ff 0%, #ffffff 45%, #f6ffed 100%)',
      }}
    >
      <Card
        style={{
          width: '100%',
          maxWidth: 400,
          borderRadius: 12,
          boxShadow: '0 8px 24px rgba(15, 23, 42, 0.08)',
        }}
      >
        <div style={{ textAlign: 'center', marginBottom: 28 }}>
          <Title level={3} style={{ marginBottom: 8 }}>
            转卖助手
          </Title>
          <Text type="secondary">登录以继续使用</Text>
        </div>
        <Form layout="vertical" requiredMark={false} onFinish={onFinish}>
          <Form.Item
            label="用户名"
            name="username"
            rules={[{ required: true, message: '请输入用户名' }]}
          >
            <Input placeholder="用户名" size="large" autoComplete="username" />
          </Form.Item>
          <Form.Item
            label="密码"
            name="password"
            rules={[{ required: true, message: '请输入密码' }]}
          >
            <Input.Password placeholder="密码" size="large" autoComplete="current-password" />
          </Form.Item>
          <Form.Item style={{ marginBottom: 0 }}>
            <Button type="primary" htmlType="submit" block size="large" loading={loading}>
              登录
            </Button>
          </Form.Item>
        </Form>
      </Card>
    </div>
  );
};

export default Login;
