import React, { useState, useEffect, useCallback } from 'react';
import {
  Card, Table, Tabs, Typography, Button, Space, Tag, message,
  Tooltip, Popconfirm,
} from 'antd';
import { ReloadOutlined, RocketOutlined, FireOutlined, DeleteOutlined } from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import api from '../../services/api';

const { Title, Paragraph, Text } = Typography;

const STATUS_MAP: Record<string, { label: string; color: string }> = {
  draft: { label: '草稿', color: 'default' },
  pending_review: { label: '发布中', color: 'processing' },
  published: { label: '已发布', color: 'green' },
  sold_out: { label: '已售', color: 'purple' },
  removed: { label: '已下架', color: 'default' },
  error: { label: '异常', color: 'error' },
};

interface ListingItem {
  id: string;
  title: string;
  description: string;
  price: number;
  original_cost: number;
  expected_profit: number;
  status: string;
  error_message: string | null;
  views: number;
  wants: number;
  chats: number;
  published_at: string | null;
  last_refreshed_at: string | null;
  created_at: string | null;
}

const XianyuWorkbench: React.FC = () => {
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState<ListingItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [activeTab, setActiveTab] = useState('all');
  const [selectedIds, setSelectedIds] = useState<string[]>([]);

  const fetchListings = useCallback(async (p = 1) => {
    setLoading(true);
    try {
      const params: Record<string, unknown> = { page: p, page_size: 20 };
      if (activeTab !== 'all') params.status = activeTab;
      const res = await api.get('/xianyu/listings', { params });
      setData(res.data.items || []);
      setTotal(res.data.total || 0);
      setPage(p);
    } catch { /* */ }
    setLoading(false);
  }, [activeTab]);

  useEffect(() => { fetchListings(); }, [fetchListings]);

  const handlePublish = async (id: string) => {
    try {
      await api.post(`/xianyu/listings/${id}/publish`);
      message.success('已加入发布队列');
      fetchListings(page);
    } catch (e: any) {
      message.error(e.response?.data?.detail || '操作失败');
    }
  };

  const handleBatchRefresh = async () => {
    if (!selectedIds.length) { message.warning('请先选择商品'); return; }
    try {
      await api.post('/xianyu/listings/batch-refresh', selectedIds);
      message.success('已加入擦亮队列');
      setSelectedIds([]);
    } catch (e: any) {
      message.error(e.response?.data?.detail || '操作失败');
    }
  };

  const handleRemove = async (id: string) => {
    try {
      await api.delete(`/xianyu/listings/${id}`);
      message.success('已下架');
      fetchListings(page);
    } catch { message.error('操作失败'); }
  };

  const columns: ColumnsType<ListingItem> = [
    {
      title: '商品名称', dataIndex: 'title', ellipsis: true, width: 220,
      render: (t: string) => <Text ellipsis style={{ maxWidth: 200 }}>{t}</Text>,
    },
    {
      title: '售价', dataIndex: 'price', width: 80,
      render: (v: number) => `¥${v.toFixed(2)}`,
    },
    {
      title: '成本', dataIndex: 'original_cost', width: 80,
      render: (v: number) => <Text type="secondary">¥{v.toFixed(2)}</Text>,
    },
    {
      title: '预估利润', dataIndex: 'expected_profit', width: 90,
      render: (v: number) => <Text type={v > 0 ? 'success' : 'danger'}>¥{v.toFixed(2)}</Text>,
    },
    {
      title: '状态', dataIndex: 'status', width: 90,
      render: (s: string, r) => {
        const cfg = STATUS_MAP[s] || { label: s, color: 'default' };
        return (
          <Tooltip title={r.error_message}>
            <Tag color={cfg.color}>{cfg.label}</Tag>
          </Tooltip>
        );
      },
    },
    { title: '曝光', dataIndex: 'views', width: 60, render: (v: number) => v || '-' },
    { title: '想要', dataIndex: 'wants', width: 60, render: (v: number) => v || '-' },
    { title: '聊天', dataIndex: 'chats', width: 60, render: (v: number) => v || '-' },
    {
      title: '发布时间', dataIndex: 'published_at', width: 120,
      render: (t: string) => t ? new Date(t).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '-',
    },
    {
      title: '操作', width: 160,
      render: (_: unknown, r: ListingItem) => (
        <Space size="small">
          {['draft', 'error'].includes(r.status) && (
            <Button size="small" type="primary" icon={<RocketOutlined />} onClick={() => handlePublish(r.id)}>发布</Button>
          )}
          {r.status === 'published' && (
            <Popconfirm title="确定下架？" onConfirm={() => handleRemove(r.id)}>
              <Button size="small" danger icon={<DeleteOutlined />}>下架</Button>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ];

  return (
    <div>
      <Title level={3} style={{ marginBottom: 4 }}>闲鱼工作台</Title>
      <Paragraph type="secondary">管理闲鱼草稿、发布与在架商品</Paragraph>

      <Card
        style={{ marginTop: 16 }}
        extra={
          <Space>
            {selectedIds.length > 0 && (
              <Button icon={<FireOutlined />} onClick={handleBatchRefresh}>
                擦亮选中 ({selectedIds.length})
              </Button>
            )}
            <Button icon={<ReloadOutlined />} onClick={() => fetchListings(1)}>刷新</Button>
          </Space>
        }
      >
        <Tabs
          activeKey={activeTab}
          onChange={(k) => { setActiveTab(k); setPage(1); }}
          items={[
            { key: 'all', label: `全部` },
            { key: 'draft', label: '草稿' },
            { key: 'pending_review', label: '发布中' },
            { key: 'published', label: '已发布' },
            { key: 'error', label: '异常' },
            { key: 'removed', label: '已下架' },
          ]}
        />
        <Table
          columns={columns}
          dataSource={data}
          rowKey="id"
          loading={loading}
          pagination={{ current: page, total, pageSize: 20, onChange: (p) => fetchListings(p), showTotal: (t) => `共 ${t} 个` }}
          scroll={{ x: 1100 }}
          locale={{ emptyText: '暂无商品数据' }}
          rowSelection={{
            selectedRowKeys: selectedIds,
            onChange: (keys) => setSelectedIds(keys as string[]),
            getCheckboxProps: (r) => ({ disabled: r.status !== 'published' }),
          }}
        />
      </Card>
    </div>
  );
};

export default XianyuWorkbench;
