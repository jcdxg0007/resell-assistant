import React, { useState, useEffect, useCallback } from 'react';
import {
  Card, Table, Tabs, Typography, Button, Space, Tag, message,
  Popconfirm,
} from 'antd';
import { ReloadOutlined, RocketOutlined, DeleteOutlined } from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import api from '../../services/api';

const { Title, Paragraph, Text } = Typography;

const NOTE_STATUS: Record<string, { label: string; color: string }> = {
  draft: { label: '草稿', color: 'default' },
  scheduled: { label: '待发布', color: 'processing' },
  published: { label: '已发布', color: 'green' },
  restricted: { label: '被限流', color: 'volcano' },
  removed: { label: '已删除', color: 'default' },
};

const NOTE_TYPE: Record<string, string> = {
  seed_review: '种草测评',
  tutorial: '教程攻略',
  collection: '合集推荐',
  comparison: '对比评测',
  scene: '场景展示',
  avoid_trap: '避坑指南',
};

interface NoteItem {
  id: string;
  title: string;
  body: string;
  note_type: string;
  content_type: string;
  status: string;
  tags: string[] | null;
  topics: string[] | null;
  published_at: string | null;
  created_at: string | null;
}

const XhsWorkbench: React.FC = () => {
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState<NoteItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [activeTab, setActiveTab] = useState('all');

  const fetchNotes = useCallback(async (p = 1) => {
    setLoading(true);
    try {
      const params: Record<string, unknown> = { page: p, page_size: 20 };
      if (activeTab !== 'all') params.status = activeTab;
      const res = await api.get('/xhs/notes', { params });
      setData(res.data.items || []);
      setTotal(res.data.total || 0);
      setPage(p);
    } catch { /* */ }
    setLoading(false);
  }, [activeTab]);

  useEffect(() => { fetchNotes(); }, [fetchNotes]);

  const handlePublish = async (id: string) => {
    try {
      await api.post(`/xhs/notes/${id}/publish`);
      message.success('已加入发布队列');
      fetchNotes(page);
    } catch (e: any) {
      message.error(e.response?.data?.detail || '操作失败');
    }
  };

  const handleDelete = async (id: string) => {
    try {
      await api.delete(`/xhs/notes/${id}`);
      message.success('已删除');
      fetchNotes(page);
    } catch { message.error('操作失败'); }
  };

  const columns: ColumnsType<NoteItem> = [
    {
      title: '标题', dataIndex: 'title', ellipsis: true, width: 240,
      render: (t: string) => <Text ellipsis style={{ maxWidth: 220 }}>{t}</Text>,
    },
    {
      title: '类型', dataIndex: 'note_type', width: 100,
      render: (t: string) => <Tag>{NOTE_TYPE[t] || t}</Tag>,
    },
    {
      title: '形式', dataIndex: 'content_type', width: 70,
      render: (t: string) => t === 'video' ? '视频' : '图文',
    },
    {
      title: '标签', dataIndex: 'tags', width: 160, ellipsis: true,
      render: (tags: string[]) => tags?.slice(0, 3).map((t, i) => <Tag key={i} style={{ marginBottom: 2 }}>#{t}</Tag>) || '-',
    },
    {
      title: '状态', dataIndex: 'status', width: 90,
      render: (s: string) => {
        const cfg = NOTE_STATUS[s] || { label: s, color: 'default' };
        return <Tag color={cfg.color}>{cfg.label}</Tag>;
      },
    },
    {
      title: '发布时间', dataIndex: 'published_at', width: 120,
      render: (t: string) => t ? new Date(t).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '-',
    },
    {
      title: '操作', width: 160,
      render: (_: unknown, r: NoteItem) => (
        <Space size="small">
          {r.status === 'draft' && (
            <Button size="small" type="primary" icon={<RocketOutlined />} onClick={() => handlePublish(r.id)}>发布</Button>
          )}
          {['draft', 'published'].includes(r.status) && (
            <Popconfirm title="确定删除？" onConfirm={() => handleDelete(r.id)}>
              <Button size="small" danger icon={<DeleteOutlined />}>删除</Button>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ];

  return (
    <div>
      <Title level={3} style={{ marginBottom: 4 }}>小红书工作台</Title>
      <Paragraph type="secondary">管理笔记草稿、已发布笔记内容</Paragraph>

      <Card
        style={{ marginTop: 16 }}
        extra={
          <Button icon={<ReloadOutlined />} onClick={() => fetchNotes(1)}>刷新</Button>
        }
      >
        <Tabs
          activeKey={activeTab}
          onChange={(k) => { setActiveTab(k); setPage(1); }}
          items={[
            { key: 'all', label: '全部' },
            { key: 'draft', label: '草稿' },
            { key: 'scheduled', label: '待发布' },
            { key: 'published', label: '已发布' },
          ]}
        />
        <Table
          columns={columns}
          dataSource={data}
          rowKey="id"
          loading={loading}
          pagination={{ current: page, total, pageSize: 20, onChange: (p) => fetchNotes(p), showTotal: (t) => `共 ${t} 篇` }}
          scroll={{ x: 900 }}
          locale={{ emptyText: '暂无笔记数据' }}
        />
      </Card>
    </div>
  );
};

export default XhsWorkbench;
