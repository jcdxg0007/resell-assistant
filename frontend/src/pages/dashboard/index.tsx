import React, { useState, useEffect, useCallback } from 'react';
import {
  Typography, Card, Row, Col, Statistic, List, Tag, Space,
  Button, Spin,
} from 'antd';
import {
  ShoppingCartOutlined, DollarOutlined,
  AlertOutlined, RobotOutlined, ReloadOutlined,
  UserOutlined, MessageOutlined,
} from '@ant-design/icons';
import api from '../../services/api';

const { Title, Text, Paragraph } = Typography;

const Dashboard: React.FC = () => {
  const [orderStats, setOrderStats] = useState<any>(null);
  const [accountStats, setAccountStats] = useState<any>(null);
  const [aiSuggestions, setAiSuggestions] = useState<string[]>([]);
  const [unreadMessages, setUnreadMessages] = useState(0);
  const [loading, setLoading] = useState(false);

  const fetchAll = useCallback(async () => {
    setLoading(true);
    try {
      const [ordRes, acctRes, aiRes, msgRes] = await Promise.all([
        api.get('/orders/stats').catch(() => ({ data: {} })),
        api.get('/accounts/stats/summary').catch(() => ({ data: {} })),
        api.get('/ai-ops/suggestions').catch(() => ({ data: { suggestions: [] } })),
        api.get('/customer/conversations', { params: { status: 'active', page_size: 1 } }).catch(() => ({ data: { total_unread: 0 } })),
      ]);
      setOrderStats(ordRes.data);
      setAccountStats(acctRes.data);
      setAiSuggestions(aiRes.data.suggestions || []);
      setUnreadMessages(msgRes.data.total_unread || 0);
    } catch { /* handled */ }
    setLoading(false);
  }, []);

  useEffect(() => { fetchAll(); }, [fetchAll]);

  return (
    <Spin spinning={loading}>
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <Row justify="space-between" align="middle">
          <Col><Title level={4} style={{ margin: 0 }}>运营仪表盘</Title></Col>
          <Col><Button icon={<ReloadOutlined />} onClick={fetchAll}>刷新</Button></Col>
        </Row>

        {/* Core metrics */}
        <Row gutter={[16, 16]}>
          <Col xs={12} sm={6}>
            <Card>
              <Statistic title="总订单" value={orderStats?.total_orders || 0} prefix={<ShoppingCartOutlined />} />
            </Card>
          </Col>
          <Col xs={12} sm={6}>
            <Card>
              <Statistic title="总利润" value={orderStats?.total_profit || 0} precision={2} prefix={<DollarOutlined />} valueStyle={{ color: '#52c41a' }} />
            </Card>
          </Col>
          <Col xs={12} sm={6}>
            <Card>
              <Statistic title="总营收" value={orderStats?.total_revenue || 0} precision={2} prefix="¥" />
            </Card>
          </Col>
          <Col xs={12} sm={6}>
            <Card>
              <Statistic title="待处理" value={(orderStats?.status_counts?.pending || 0) + (orderStats?.status_counts?.error || 0)} prefix={<AlertOutlined />} valueStyle={{ color: (orderStats?.status_counts?.error || 0) > 0 ? '#ff4d4f' : undefined }} />
            </Card>
          </Col>
        </Row>

        {/* Account & Message stats */}
        <Row gutter={[16, 16]}>
          <Col xs={12} sm={6}>
            <Card>
              <Statistic title="活跃账号" value={accountStats?.total_active || 0} prefix={<UserOutlined />} />
            </Card>
          </Col>
          <Col xs={12} sm={6}>
            <Card>
              <Statistic title="闲鱼账号" value={accountStats?.by_platform?.xianyu || 0} suffix={<Text type="secondary" style={{ fontSize: 14 }}>个</Text>} />
            </Card>
          </Col>
          <Col xs={12} sm={6}>
            <Card>
              <Statistic title="小红书账号" value={accountStats?.by_platform?.xiaohongshu || 0} suffix={<Text type="secondary" style={{ fontSize: 14 }}>个</Text>} />
            </Card>
          </Col>
          <Col xs={12} sm={6}>
            <Card>
              <Statistic title="未读消息" value={unreadMessages} prefix={<MessageOutlined />} valueStyle={{ color: unreadMessages > 0 ? '#faad14' : undefined }} />
            </Card>
          </Col>
        </Row>

        {/* Order status breakdown */}
        {orderStats?.status_counts && (
          <Card title="订单状态分布" size="small">
            <Space wrap>
              {Object.entries(orderStats.status_counts as Record<string, number>)
                .filter(([, v]) => v > 0)
                .map(([status, count]) => {
                  const labels: Record<string, string> = {
                    pending: '待采购', purchasing: '采购中', purchased: '已采购',
                    shipped: '已发货', delivered: '已签收', completed: '已完成',
                    refunding: '退款中', refunded: '已退款', error: '异常',
                  };
                  const colors: Record<string, string> = {
                    pending: 'orange', error: 'red', completed: 'green',
                    shipped: 'blue', refunding: 'volcano',
                  };
                  return (
                    <Tag key={status} color={colors[status] || 'default'}>
                      {labels[status] || status}: {count as number}
                    </Tag>
                  );
                })}
            </Space>
          </Card>
        )}

        {/* AI Suggestions */}
        <Card
          title={<><RobotOutlined /> AI运营建议</>}
          size="small"
          extra={<Tag color="blue">AI生成</Tag>}
        >
          {aiSuggestions.length > 0 ? (
            <List
              size="small"
              dataSource={aiSuggestions}
              renderItem={(item, idx) => (
                <List.Item>
                  <Text>{idx + 1}. {item}</Text>
                </List.Item>
              )}
            />
          ) : (
            <Paragraph type="secondary">系统运行后将自动生成运营建议</Paragraph>
          )}
        </Card>

        {/* Quick actions */}
        <Card title="快捷操作" size="small">
          <Space wrap>
            <Button type="primary" href="#/selection/xianyu">闲鱼选品</Button>
            <Button type="primary" href="#/selection/xhs">小红书选品</Button>
            <Button href="#/orders">订单管理</Button>
            <Button href="#/customer">客服中心</Button>
            <Button href="#/accounts">账号管理</Button>
          </Space>
        </Card>
      </Space>
    </Spin>
  );
};

export default Dashboard;
