import React, { useState, useEffect, useCallback } from 'react';
import {
  Typography, Table, Card, Button, Space, Tag, Modal, Form,
  Input, Select, InputNumber, message, Popconfirm, Progress, Row, Col, Statistic,
} from 'antd';
import {
  PlusOutlined, ReloadOutlined, PauseCircleOutlined, PlayCircleOutlined, EditOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import api from '../../services/api';

const { Title } = Typography;

const PLATFORM_MAP: Record<string, { label: string; color: string }> = {
  xianyu: { label: '闲鱼', color: 'orange' },
  xiaohongshu: { label: '小红书', color: 'red' },
  douyin: { label: '抖音', color: 'blue' },
};

const LIFECYCLE_MAP: Record<string, { label: string; color: string }> = {
  nurturing: { label: '养号期', color: 'default' },
  cold_start: { label: '冷启动', color: 'processing' },
  growing: { label: '成长期', color: 'cyan' },
  mature: { label: '成熟期', color: 'green' },
  suspended: { label: '已暂停', color: 'error' },
};

interface AccountItem {
  id: string;
  platform: string;
  account_name: string;
  identity_group: string;
  niche: string | null;
  proxy_url: string | null;
  lifecycle_stage: string;
  daily_publish_limit: number;
  daily_published_count: number;
  health_score: number;
  is_active: boolean;
  suspended_reason: string | null;
  created_at: string | null;
}

interface Summary {
  total_active: number;
  suspended: number;
  by_platform: Record<string, number>;
}

const Accounts: React.FC = () => {
  const [accounts, setAccounts] = useState<AccountItem[]>([]);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editModalOpen, setEditModalOpen] = useState(false);
  const [editingAccount, setEditingAccount] = useState<AccountItem | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [form] = Form.useForm();
  const [editForm] = Form.useForm();

  const fetchAccounts = useCallback(async () => {
    setLoading(true);
    try {
      const [listRes, summaryRes] = await Promise.all([
        api.get('/accounts/', { params: { page_size: 100 } }),
        api.get('/accounts/stats/summary').catch(() => ({ data: null })),
      ]);
      setAccounts(listRes.data.items || []);
      if (summaryRes.data) setSummary(summaryRes.data);
    } catch {
      message.error('获取账号列表失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchAccounts(); }, [fetchAccounts]);

  const handleCreate = async () => {
    try {
      const values = await form.validateFields();
      setSubmitting(true);
      await api.post('/accounts/', values);
      message.success('账号创建成功');
      setModalOpen(false);
      form.resetFields();
      fetchAccounts();
    } catch (err: unknown) {
      const error = err as { response?: { data?: { detail?: string } }; errorFields?: unknown };
      if (error.response) message.error(error.response.data?.detail || '创建失败');
    } finally {
      setSubmitting(false);
    }
  };

  const openEdit = (record: AccountItem) => {
    setEditingAccount(record);
    editForm.setFieldsValue({
      proxy_url: record.proxy_url || '',
      user_agent: '',
      niche: record.niche || '',
      lifecycle_stage: record.lifecycle_stage,
      daily_publish_limit: record.daily_publish_limit,
    });
    setEditModalOpen(true);
  };

  const handleEdit = async () => {
    if (!editingAccount) return;
    try {
      const values = await editForm.validateFields();
      setSubmitting(true);
      const payload: Record<string, unknown> = {};
      if (values.proxy_url !== undefined && values.proxy_url !== (editingAccount.proxy_url || ''))
        payload.proxy_url = values.proxy_url || null;
      if (values.niche !== undefined && values.niche !== (editingAccount.niche || ''))
        payload.niche = values.niche || null;
      if (values.lifecycle_stage && values.lifecycle_stage !== editingAccount.lifecycle_stage)
        payload.lifecycle_stage = values.lifecycle_stage;
      if (values.daily_publish_limit !== undefined && values.daily_publish_limit !== editingAccount.daily_publish_limit)
        payload.daily_publish_limit = values.daily_publish_limit;
      if (values.user_agent) payload.user_agent = values.user_agent;

      if (Object.keys(payload).length === 0) {
        message.info('没有修改任何内容');
        return;
      }
      await api.put(`/accounts/${editingAccount.id}`, payload);
      message.success('账号信息已更新');
      setEditModalOpen(false);
      setEditingAccount(null);
      editForm.resetFields();
      fetchAccounts();
    } catch (err: unknown) {
      const error = err as { response?: { data?: { detail?: string } }; errorFields?: unknown };
      if (error.response) message.error(error.response.data?.detail || '更新失败');
    } finally {
      setSubmitting(false);
    }
  };

  const handleSuspend = async (id: string) => {
    await api.post(`/accounts/${id}/suspend`);
    message.success('账号已暂停');
    fetchAccounts();
  };

  const handleActivate = async (id: string) => {
    await api.post(`/accounts/${id}/activate`);
    message.success('账号已激活');
    fetchAccounts();
  };

  const columns: ColumnsType<AccountItem> = [
    {
      title: '平台', dataIndex: 'platform', width: 90,
      render: (v: string) => <Tag color={PLATFORM_MAP[v]?.color}>{PLATFORM_MAP[v]?.label || v}</Tag>,
    },
    { title: '账号名', dataIndex: 'account_name', width: 150 },
    { title: '身份组', dataIndex: 'identity_group', width: 100 },
    { title: '品类', dataIndex: 'niche', width: 100, render: (v: string) => v || '-' },
    {
      title: '生命周期', dataIndex: 'lifecycle_stage', width: 90,
      render: (v: string) => <Tag color={LIFECYCLE_MAP[v]?.color}>{LIFECYCLE_MAP[v]?.label || v}</Tag>,
    },
    {
      title: '今日发布', width: 100,
      render: (_: unknown, r: AccountItem) => `${r.daily_published_count} / ${r.daily_publish_limit}`,
    },
    {
      title: '健康度', dataIndex: 'health_score', width: 120,
      render: (v: number) => (
        <Progress
          percent={v}
          size="small"
          strokeColor={v >= 80 ? '#52c41a' : v >= 50 ? '#faad14' : '#ff4d4f'}
          format={(p) => `${p}`}
        />
      ),
    },
    {
      title: '代理 IP', dataIndex: 'proxy_url', ellipsis: true, width: 160,
      render: (v: string) => v ? <Tag color="blue">{v}</Tag> : <Tag color="warning">未配置</Tag>,
    },
    {
      title: '状态', dataIndex: 'is_active', width: 80,
      render: (v: boolean) => v ? <Tag color="success">正常</Tag> : <Tag color="error">暂停</Tag>,
    },
    {
      title: '操作', width: 160, fixed: 'right',
      render: (_: unknown, record: AccountItem) => (
        <Space size="small">
          <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(record)}>编辑</Button>
          {record.is_active ? (
            <Popconfirm title="确认暂停该账号？" onConfirm={() => handleSuspend(record.id)}>
              <Button size="small" icon={<PauseCircleOutlined />} danger>暂停</Button>
            </Popconfirm>
          ) : (
            <Button size="small" icon={<PlayCircleOutlined />} onClick={() => handleActivate(record.id)}>激活</Button>
          )}
        </Space>
      ),
    },
  ];

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Row gutter={16}>
        <Col span={6}>
          <Card><Statistic title="活跃账号" value={summary?.total_active ?? 0} /></Card>
        </Col>
        <Col span={6}>
          <Card><Statistic title="已暂停" value={summary?.suspended ?? 0} valueStyle={{ color: '#ff4d4f' }} /></Card>
        </Col>
        <Col span={4}>
          <Card><Statistic title="闲鱼" value={summary?.by_platform?.xianyu ?? 0} /></Card>
        </Col>
        <Col span={4}>
          <Card><Statistic title="小红书" value={summary?.by_platform?.xiaohongshu ?? 0} /></Card>
        </Col>
        <Col span={4}>
          <Card><Statistic title="抖音" value={summary?.by_platform?.douyin ?? 0} /></Card>
        </Col>
      </Row>

      <Card
        title={<Title level={4} style={{ margin: 0 }}>账号管理</Title>}
        extra={
          <Space>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>添加账号</Button>
            <Button icon={<ReloadOutlined />} onClick={fetchAccounts}>刷新</Button>
          </Space>
        }
      >
        <Table<AccountItem>
          rowKey="id"
          columns={columns}
          dataSource={accounts}
          loading={loading}
          pagination={false}
          scroll={{ x: 1300 }}
          locale={{ emptyText: '暂无账号，请点击「添加账号」录入' }}
        />
      </Card>

      {/* 添加账号弹窗 */}
      <Modal
        title="添加账号"
        open={modalOpen}
        onOk={handleCreate}
        onCancel={() => { setModalOpen(false); form.resetFields(); }}
        confirmLoading={submitting}
        okText="创建"
      >
        <Form form={form} layout="vertical">
          <Form.Item name="platform" label="平台" rules={[{ required: true, message: '请选择平台' }]}>
            <Select placeholder="选择平台">
              <Select.Option value="xianyu">闲鱼</Select.Option>
              <Select.Option value="xiaohongshu">小红书</Select.Option>
              <Select.Option value="douyin">抖音</Select.Option>
            </Select>
          </Form.Item>
          <Form.Item name="account_name" label="账号名称" rules={[{ required: true, message: '请输入账号名称' }]}>
            <Input placeholder="例如：闲鱼小号1" />
          </Form.Item>
          <Form.Item name="identity_group" label="身份组" rules={[{ required: true, message: '请输入身份组' }]}>
            <Input placeholder="例如：group-01（同一手机号的账号归为一组）" />
          </Form.Item>
          <Form.Item name="niche" label="品类定位">
            <Input placeholder="例如：3C数码、小家具" />
          </Form.Item>
          <Form.Item name="proxy_url" label="代理 IP">
            <Input placeholder="例如：socks5://user:pass@ip:port" />
          </Form.Item>
          <Form.Item name="user_agent" label="User-Agent">
            <Input.TextArea rows={2} placeholder="留空则自动生成" />
          </Form.Item>
        </Form>
      </Modal>

      {/* 编辑账号弹窗 */}
      <Modal
        title={`编辑账号 - ${editingAccount?.account_name || ''}`}
        open={editModalOpen}
        onOk={handleEdit}
        onCancel={() => { setEditModalOpen(false); setEditingAccount(null); editForm.resetFields(); }}
        confirmLoading={submitting}
        okText="保存"
      >
        <Form form={editForm} layout="vertical">
          <Form.Item name="proxy_url" label="代理 IP">
            <Input placeholder="例如：socks5://user:pass@ip:port" allowClear />
          </Form.Item>
          <Form.Item name="niche" label="品类定位">
            <Input placeholder="例如：3C数码、小家具" allowClear />
          </Form.Item>
          <Form.Item name="lifecycle_stage" label="生命周期">
            <Select>
              <Select.Option value="nurturing">养号期</Select.Option>
              <Select.Option value="cold_start">冷启动</Select.Option>
              <Select.Option value="growing">成长期</Select.Option>
              <Select.Option value="mature">成熟期</Select.Option>
            </Select>
          </Form.Item>
          <Form.Item name="daily_publish_limit" label="每日发布上限">
            <InputNumber min={1} max={50} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="user_agent" label="User-Agent">
            <Input.TextArea rows={2} placeholder="留空不修改" />
          </Form.Item>
        </Form>
      </Modal>
    </Space>
  );
};

export default Accounts;
