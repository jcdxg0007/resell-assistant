import React, { useState, useEffect, useCallback } from 'react';
import {
  Typography, Table, Card, Button, Space, Tag, Row, Col, Statistic,
  Modal, Descriptions, message, Select, Input, Tooltip, Badge, Alert,
} from 'antd';
import {
  ReloadOutlined, ExclamationCircleOutlined,
  DollarOutlined, CopyOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import api from '../../services/api';

const { Title, Text } = Typography;

const STATUS_MAP: Record<string, { label: string; color: string }> = {
  pending: { label: '待采购', color: 'orange' },
  purchasing: { label: '采购中', color: 'processing' },
  purchased: { label: '已采购', color: 'cyan' },
  shipped: { label: '已发货', color: 'blue' },
  delivered: { label: '已签收', color: 'green' },
  completed: { label: '已完成', color: 'success' },
  refunding: { label: '退款中', color: 'volcano' },
  refunded: { label: '已退款', color: 'default' },
  cancelled: { label: '已取消', color: 'default' },
  error: { label: '异常', color: 'error' },
};

interface OrderItem {
  id: string;
  sale_platform: string;
  sale_order_id: string;
  buyer_name: string;
  buyer_phone: string | null;
  buyer_address: string;
  buyer_note: string | null;
  sale_price: number;
  platform_fee: number;
  purchase_cost: number | null;
  shipping_cost: number;
  actual_profit: number | null;
  source_platform: string | null;
  source_order_id: string | null;
  status: string;
  order_type: string;
  error_message: string | null;
  paid_at: string | null;
  purchased_at: string | null;
  shipped_at: string | null;
  created_at: string | null;
  logistics: Array<{
    id: string;
    direction: string;
    carrier: string;
    tracking_number: string;
    status: string;
    synced_to_sale_platform: boolean;
  }>;
}

interface OrderStats {
  status_counts: Record<string, number>;
  total_profit: number;
  total_revenue: number;
  total_orders: number;
}

const Orders: React.FC = () => {
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState<OrderItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [stats, setStats] = useState<OrderStats | null>(null);
  const [statusFilter, setStatusFilter] = useState<string | undefined>();
  const [detailVisible, setDetailVisible] = useState(false);
  const [detailOrder, setDetailOrder] = useState<OrderItem | null>(null);
  const [manualVisible, setManualVisible] = useState(false);
  const [manualForm, setManualForm] = useState({ source_platform: 'pinduoduo', source_order_id: '', purchase_cost: '' });

  const fetchOrders = useCallback(async (p = 1) => {
    setLoading(true);
    try {
      const res = await api.get('/orders/', { params: { page: p, page_size: 20, status: statusFilter } });
      setData(res.data.items || []);
      setTotal(res.data.total || 0);
      setPage(p);
    } catch { /* handled by interceptor */ }
    setLoading(false);
  }, [statusFilter]);

  const fetchStats = useCallback(async () => {
    try {
      const res = await api.get('/orders/stats');
      setStats(res.data);
    } catch { /* ignored */ }
  }, []);

  useEffect(() => { fetchOrders(); fetchStats(); }, [fetchOrders, fetchStats]);

  const handleManualPurchase = async () => {
    if (!detailOrder) return;
    try {
      await api.post(`/orders/${detailOrder.id}/manual-purchase`, {
        source_platform: manualForm.source_platform,
        source_order_id: manualForm.source_order_id,
        purchase_cost: parseFloat(manualForm.purchase_cost),
      });
      message.success('采购信息已录入');
      setManualVisible(false);
      fetchOrders(page);
    } catch { message.error('操作失败'); }
  };

  const handleReturn = async (orderId: string) => {
    try {
      const res = await api.post(`/orders/${orderId}/return`, { reason: '买家申请退货' });
      message.info(res.data.message);
      if (res.data.return_address) {
        Modal.info({
          title: '源商家退货地址',
          content: (
            <div>
              <p>地址: {res.data.return_address.address}</p>
              <p>联系人: {res.data.return_address.contact_name}</p>
              <p>电话: {res.data.return_address.contact_phone}</p>
              <p style={{ color: '#ff4d4f' }}>请在闲鱼聊天中把此地址发给买家</p>
            </div>
          ),
        });
      }
      fetchOrders(page);
    } catch { message.error('操作失败'); }
  };

  const columns: ColumnsType<OrderItem> = [
    {
      title: '订单号', dataIndex: 'sale_order_id', width: 140, ellipsis: true,
      render: (id: string) => <Text copyable={{ text: id }}>{id?.slice(-8)}</Text>,
    },
    {
      title: '平台', dataIndex: 'sale_platform', width: 70,
      render: (p: string) => <Tag>{p === 'xianyu' ? '闲鱼' : p}</Tag>,
    },
    {
      title: '售价', dataIndex: 'sale_price', width: 80,
      render: (v: number) => <Text>¥{v?.toFixed(2)}</Text>,
    },
    {
      title: '成本', dataIndex: 'purchase_cost', width: 80,
      render: (v: number | null) => v != null ? <Text>¥{v.toFixed(2)}</Text> : <Text type="secondary">-</Text>,
    },
    {
      title: '利润', dataIndex: 'actual_profit', width: 80,
      render: (v: number | null) => {
        if (v == null) return '-';
        return <Text type={v > 0 ? 'success' : 'danger'}>¥{v.toFixed(2)}</Text>;
      },
    },
    {
      title: '状态', dataIndex: 'status', width: 90,
      render: (s: string, record) => {
        const cfg = STATUS_MAP[s] || { label: s, color: 'default' };
        return (
          <Tooltip title={record.error_message}>
            <Tag color={cfg.color}>{cfg.label}</Tag>
            {record.error_message && <ExclamationCircleOutlined style={{ color: '#ff4d4f', marginLeft: 4 }} />}
          </Tooltip>
        );
      },
    },
    {
      title: '物流', width: 80,
      render: (_: unknown, record: OrderItem) => {
        const fwd = record.logistics?.find(l => l.direction === 'forward');
        if (!fwd) return <Text type="secondary">-</Text>;
        return (
          <Tooltip title={`${fwd.carrier} ${fwd.tracking_number}`}>
            <Badge status={fwd.synced_to_sale_platform ? 'success' : 'processing'} text={fwd.status === 'delivered' ? '已签收' : '运输中'} />
          </Tooltip>
        );
      },
    },
    { title: '买家', dataIndex: 'buyer_name', width: 80, ellipsis: true },
    {
      title: '时间', dataIndex: 'created_at', width: 120,
      render: (t: string) => t ? new Date(t).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '-',
    },
    {
      title: '操作', width: 160,
      render: (_: unknown, record: OrderItem) => (
        <Space size="small" wrap>
          <Button size="small" onClick={() => { setDetailOrder(record); setDetailVisible(true); }}>详情</Button>
          {['pending', 'error'].includes(record.status) && (
            <Button size="small" type="primary" onClick={() => { setDetailOrder(record); setManualVisible(true); }}>手动采购</Button>
          )}
          {['purchased', 'shipped', 'delivered'].includes(record.status) && (
            <Button size="small" danger onClick={() => handleReturn(record.id)}>退货</Button>
          )}
        </Space>
      ),
    },
  ];

  return (
    <>
      <Title level={4}>订单管理</Title>

      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col xs={12} sm={6}>
          <Card><Statistic title="总订单" value={stats?.total_orders || 0} /></Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card><Statistic title="待处理" value={(stats?.status_counts?.pending || 0) + (stats?.status_counts?.error || 0)} valueStyle={{ color: '#faad14' }} prefix={<ExclamationCircleOutlined />} /></Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card><Statistic title="总营收" value={stats?.total_revenue || 0} precision={2} prefix="¥" /></Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card><Statistic title="总利润" value={stats?.total_profit || 0} precision={2} prefix={<DollarOutlined />} valueStyle={{ color: '#52c41a' }} /></Card>
        </Col>
      </Row>

      <Card
        title="订单列表"
        extra={
          <Space>
            <Select placeholder="状态筛选" allowClear style={{ width: 120 }} value={statusFilter} onChange={setStatusFilter} options={Object.entries(STATUS_MAP).map(([k, v]) => ({ value: k, label: v.label }))} />
            <Button icon={<ReloadOutlined />} onClick={() => { fetchOrders(1); fetchStats(); }}>刷新</Button>
          </Space>
        }
      >
        <Table
          columns={columns}
          dataSource={data}
          rowKey="id"
          loading={loading}
          pagination={{ current: page, total, pageSize: 20, onChange: (p) => fetchOrders(p), showTotal: (t) => `共 ${t} 单` }}
          scroll={{ x: 1100 }}
          locale={{ emptyText: '暂无订单数据' }}
          rowClassName={(record) => record.status === 'pending' ? 'ant-table-row-pending' : ''}
        />
      </Card>

      <Modal title="订单详情" open={detailVisible} onCancel={() => setDetailVisible(false)} footer={null} width={640}>
        {detailOrder && (
          <>
            {detailOrder.status === 'pending' && (
              <Alert
                type="warning"
                showIcon
                style={{ marginBottom: 12 }}
                message="此订单待采购"
                description="请手动在货源平台下单，然后点击「手动采购」录入采购信息"
                action={
                  <Button
                    size="small"
                    type="primary"
                    onClick={() => { setManualVisible(true); }}
                  >
                    手动采购
                  </Button>
                }
              />
            )}
            <Descriptions column={2} bordered size="small">
              <Descriptions.Item label="订单号" span={2}>
                <Text copyable>{detailOrder.sale_order_id}</Text>
              </Descriptions.Item>
              <Descriptions.Item label="售价">¥{detailOrder.sale_price}</Descriptions.Item>
              <Descriptions.Item label="成本">{detailOrder.purchase_cost != null ? `¥${detailOrder.purchase_cost}` : '-'}</Descriptions.Item>
              <Descriptions.Item label="手续费">¥{detailOrder.platform_fee}</Descriptions.Item>
              <Descriptions.Item label="利润">{detailOrder.actual_profit != null ? `¥${detailOrder.actual_profit}` : '-'}</Descriptions.Item>
              <Descriptions.Item label="买家">{detailOrder.buyer_name}</Descriptions.Item>
              <Descriptions.Item label="手机">
                {detailOrder.buyer_phone ? <Text copyable>{detailOrder.buyer_phone}</Text> : '-'}
              </Descriptions.Item>
              <Descriptions.Item label="地址" span={2}>
                {detailOrder.buyer_address ? (
                  <Space>
                    <span>{detailOrder.buyer_address}</span>
                    <Tooltip title="复制完整收货信息">
                      <Button
                        type="link"
                        size="small"
                        icon={<CopyOutlined />}
                        onClick={() => {
                          const info = [detailOrder.buyer_name, detailOrder.buyer_phone, detailOrder.buyer_address].filter(Boolean).join(' ');
                          navigator.clipboard.writeText(info);
                          message.success('收货信息已复制');
                        }}
                      />
                    </Tooltip>
                  </Space>
                ) : '-'}
              </Descriptions.Item>
              {detailOrder.buyer_note && (
                <Descriptions.Item label="买家留言" span={2}>{detailOrder.buyer_note}</Descriptions.Item>
              )}
              <Descriptions.Item label="源平台">{detailOrder.source_platform || '-'}</Descriptions.Item>
              <Descriptions.Item label="源订单号">
                {detailOrder.source_order_id ? <Text copyable>{detailOrder.source_order_id}</Text> : '-'}
              </Descriptions.Item>
              <Descriptions.Item label="状态" span={2}>
                <Tag color={STATUS_MAP[detailOrder.status]?.color}>{STATUS_MAP[detailOrder.status]?.label}</Tag>
                {detailOrder.error_message && <Text type="danger" style={{ marginLeft: 8 }}>{detailOrder.error_message}</Text>}
              </Descriptions.Item>
              {detailOrder.logistics?.map((l) => (
                <Descriptions.Item key={l.id} label={l.direction === 'forward' ? '发货物流' : '退货物流'} span={2}>
                  {l.carrier} {l.tracking_number} ({l.synced_to_sale_platform ? '已同步' : '待同步'})
                </Descriptions.Item>
              ))}
            </Descriptions>
          </>
        )}
      </Modal>

      <Modal title="手动录入采购信息" open={manualVisible} onCancel={() => setManualVisible(false)} onOk={handleManualPurchase}>
        <Space direction="vertical" style={{ width: '100%' }}>
          <Select value={manualForm.source_platform} onChange={(v) => setManualForm({ ...manualForm, source_platform: v })} style={{ width: '100%' }}
            options={[{ value: 'pinduoduo', label: '拼多多' }, { value: 'taobao', label: '淘宝' }]} />
          <Input placeholder="源平台订单号" value={manualForm.source_order_id}
            onChange={(e) => setManualForm({ ...manualForm, source_order_id: e.target.value })} />
          <Input placeholder="采购金额" prefix="¥" value={manualForm.purchase_cost}
            onChange={(e) => setManualForm({ ...manualForm, purchase_cost: e.target.value })} />
        </Space>
      </Modal>
    </>
  );
};

export default Orders;
