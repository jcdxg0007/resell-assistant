import React, { useState, useEffect, useCallback } from 'react';
import {
  Typography, Table, Card, Input, Button, Space, Tag, Select, Switch,
  Modal, Form, Popconfirm, App, Row, Col, Tooltip, Menu,
} from 'antd';
import { PlusOutlined, ReloadOutlined, DeleteOutlined, EditOutlined } from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import api from '../../services/api';

const { Title, Text } = Typography;

interface Category {
  id: string;
  name: string;
  slug: string;
  keyword_count: number;
}

interface KeywordRow {
  id: string;
  text: string;
  category_id: string;
  category_name: string | null;
  pdd_mode: string;
  pdd_safe: boolean;
  schedule_enabled: boolean;
  is_active: boolean;
  pdd_last_searched_at: string | null;
  pdd_last_status: string | null;
  pdd_searches_total: number;
  xianyu_safe: boolean;
  xianyu_last_searched_at: string | null;
  xianyu_last_status: string | null;
  xianyu_searches_total: number;
}

const MODE_OPTIONS = [
  { value: 'fast', label: '快速（fast）' },
  { value: 'list_deep', label: '列表深抓（list_deep）' },
  { value: 'detail_smart', label: '详情智能（Phase2）' },
  { value: 'detail_deep', label: '详情深抓（Phase2）' },
];
const MODE_LABEL: Record<string, string> = {
  fast: '快速', list_deep: '列表深抓', detail_smart: '详情智能', detail_deep: '详情深抓',
};

const STATUS_META: Record<string, { color: string; label: string }> = {
  ok: { color: 'success', label: '成功' },
  empty: { color: 'default', label: '空结果' },
  partial: { color: 'gold', label: '部分' },
  failed: { color: 'error', label: '失败' },
  risk_blocked: { color: 'volcano', label: '风控' },
  timeout: { color: 'orange', label: '超时' },
};

const fmtTime = (iso: string | null) => {
  if (!iso) return '从未跑过';
  return new Date(iso).toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
};

const PddKeywords: React.FC = () => {
  const { message } = App.useApp();
  const [categories, setCategories] = useState<Category[]>([]);
  const [rows, setRows] = useState<KeywordRow[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);

  // 过滤
  const [catFilter, setCatFilter] = useState<string | undefined>(undefined);
  const [q, setQ] = useState('');
  const [safeFilter, setSafeFilter] = useState<boolean | undefined>(undefined);

  // 编辑弹窗
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<KeywordRow | null>(null);
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);

  // 新建分类弹窗
  const [catModalOpen, setCatModalOpen] = useState(false);
  const [catForm] = Form.useForm();

  const fetchCategories = useCallback(async () => {
    try {
      const res = await api.get('/pdd-keywords/categories');
      setCategories(res.data || []);
    } catch { /* ignore */ }
  }, []);

  const fetchKeywords = useCallback(async (p = page) => {
    setLoading(true);
    try {
      const res = await api.get('/pdd-keywords/', {
        params: { category_id: catFilter, q: q || undefined, pdd_safe: safeFilter, page: p, page_size: 20 },
      });
      setRows(res.data.items || []);
      setTotal(res.data.total || 0);
      setPage(p);
    } catch { message.error('加载词库失败'); }
    setLoading(false);
  }, [catFilter, q, safeFilter, page, message]);

  useEffect(() => { fetchCategories(); }, [fetchCategories]);
  useEffect(() => { fetchKeywords(1); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [catFilter, safeFilter]);

  // 行内快速切换（安全词 / 调度）
  const patchKeyword = async (id: string, patch: Partial<KeywordRow>, okMsg?: string) => {
    try {
      await api.put(`/pdd-keywords/${id}`, patch);
      setRows((prev) => prev.map((r) => (r.id === id ? { ...r, ...patch } : r)));
      if (okMsg) message.success(okMsg);
    } catch {
      message.error('更新失败');
      fetchKeywords();
    }
  };

  // 表头全选：作用到当前筛选范围（分类 + 搜索词）下的全部词，跨页
  type SwitchField = 'pdd_safe' | 'xianyu_safe' | 'schedule_enabled';
  const allOn = (field: SwitchField) => rows.length > 0 && rows.every((r) => !!r[field]);
  const bulkPatchField = async (field: SwitchField, value: boolean) => {
    setRows((rs) => rs.map((r) => ({ ...r, [field]: value })));  // 乐观更新本页
    try {
      const res = await api.post('/pdd-keywords/bulk-toggle', {
        field, value, category_id: catFilter, q: q || undefined,
      });
      const scope = catFilter ? '该分类' : '全部';
      message.success(`${scope}共 ${res.data.updated} 个词已全部${value ? '开启' : '关闭'}`);
      fetchKeywords();
    } catch {
      message.error('批量更新失败');
      fetchKeywords();
    }
  };

  const openCreate = () => {
    setEditing(null);
    form.resetFields();
    form.setFieldsValue({ pdd_mode: 'fast', pdd_safe: true, xianyu_safe: true, schedule_enabled: true, category_id: catFilter });
    setModalOpen(true);
  };

  const openEdit = (r: KeywordRow) => {
    setEditing(r);
    form.setFieldsValue({
      text: r.text, category_id: r.category_id, pdd_mode: r.pdd_mode,
      pdd_safe: r.pdd_safe, xianyu_safe: r.xianyu_safe, schedule_enabled: r.schedule_enabled,
    });
    setModalOpen(true);
  };

  const submit = async () => {
    const values = await form.validateFields();
    setSaving(true);
    try {
      if (editing) {
        await api.put(`/pdd-keywords/${editing.id}`, values);
        message.success('已保存');
      } else {
        await api.post('/pdd-keywords/', values);
        message.success('已新建');
      }
      setModalOpen(false);
      fetchKeywords(editing ? page : 1);
      fetchCategories();
    } catch (err) {
      const e = err as { response?: { data?: { detail?: string } } };
      message.error(e?.response?.data?.detail || '保存失败');
    }
    setSaving(false);
  };

  const remove = async (id: string) => {
    try {
      await api.delete(`/pdd-keywords/${id}`);
      message.success('已删除');
      fetchKeywords();
      fetchCategories();
    } catch { message.error('删除失败'); }
  };

  const createCategory = async () => {
    const values = await catForm.validateFields();
    try {
      await api.post('/pdd-keywords/categories', values);
      message.success('分类已新建');
      setCatModalOpen(false);
      catForm.resetFields();
      fetchCategories();
    } catch (err) {
      const e = err as { response?: { data?: { detail?: string } } };
      message.error(e?.response?.data?.detail || '新建分类失败');
    }
  };

  const columns: ColumnsType<KeywordRow> = [
    { title: '关键词', dataIndex: 'text', width: 160, render: (t: string) => <Text strong>{t}</Text> },
    { title: '分类', dataIndex: 'category_name', width: 120, render: (c: string | null) => c || '—' },
    {
      title: '模式', dataIndex: 'pdd_mode', width: 100,
      render: (m: string) => <Tag>{MODE_LABEL[m] || m}</Tag>,
    },
    {
      title: (
        <Space direction="vertical" size={2} align="center" style={{ lineHeight: 1.1 }}>
          <Tooltip title="关：PDD 自动跑批会跳过该词（用于禁用敏感词）。表头开关 = 本页全选">PDD</Tooltip>
          <Switch size="small" checked={allOn('pdd_safe')} onChange={(c) => bulkPatchField('pdd_safe', c)} />
        </Space>
      ),
      dataIndex: 'pdd_safe', width: 72, align: 'center',
      render: (v: boolean, r) => (
        <Switch size="small" checked={v} onChange={(c) => patchKeyword(r.id, { pdd_safe: c })} />
      ),
    },
    {
      title: (
        <Space direction="vertical" size={2} align="center" style={{ lineHeight: 1.1 }}>
          <Tooltip title="关：闲鱼自动采集会跳过该词。表头开关 = 本页全选">闲鱼</Tooltip>
          <Switch size="small" checked={allOn('xianyu_safe')} onChange={(c) => bulkPatchField('xianyu_safe', c)} />
        </Space>
      ),
      dataIndex: 'xianyu_safe', width: 72, align: 'center',
      render: (v: boolean, r) => (
        <Switch size="small" checked={v} onChange={(c) => patchKeyword(r.id, { xianyu_safe: c })} />
      ),
    },
    {
      title: (
        <Space direction="vertical" size={2} align="center" style={{ lineHeight: 1.1 }}>
          <Tooltip title="总开关：关掉后两个平台的自动轮播都跳过该词。表头开关 = 本页全选">调度</Tooltip>
          <Switch size="small" checked={allOn('schedule_enabled')} onChange={(c) => bulkPatchField('schedule_enabled', c)} />
        </Space>
      ),
      dataIndex: 'schedule_enabled', width: 72, align: 'center',
      render: (v: boolean, r) => (
        <Switch size="small" checked={v} onChange={(c) => patchKeyword(r.id, { schedule_enabled: c })} />
      ),
    },
    {
      title: '上次跑', dataIndex: 'pdd_last_searched_at', width: 150,
      render: (iso: string | null, r) => (
        <Space size={4}>
          <Text type="secondary" style={{ fontSize: 12 }}>{fmtTime(iso)}</Text>
          {r.pdd_last_status && (
            <Tag color={STATUS_META[r.pdd_last_status]?.color || 'default'}>
              {STATUS_META[r.pdd_last_status]?.label || r.pdd_last_status}
            </Tag>
          )}
        </Space>
      ),
    },
    { title: '累计', dataIndex: 'pdd_searches_total', width: 60 },
    {
      title: '操作', width: 120, fixed: 'right',
      render: (_: unknown, r) => (
        <Space size={4}>
          <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(r)} />
          <Popconfirm title={`删除「${r.text}」？`} onConfirm={() => remove(r.id)} okText="删除" cancelText="取消">
            <Button size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Row justify="space-between" align="middle">
        <Col><Title level={4} style={{ margin: 0 }}>词库管理</Title></Col>
        <Col>
          <Space>
            <Button icon={<PlusOutlined />} type="primary" onClick={openCreate}>新建词</Button>
            <Button onClick={() => setCatModalOpen(true)}>新建分类</Button>
            <Button icon={<ReloadOutlined />} onClick={() => { fetchKeywords(); fetchCategories(); }}>刷新</Button>
          </Space>
        </Col>
      </Row>

      <Row gutter={16}>
        {/* 左：分类 */}
        <Col xs={24} sm={8} md={6} lg={5}>
          <Card title="分类" size="small" styles={{ body: { padding: 4 } }}>
            <Menu
              mode="inline"
              style={{ borderInlineEnd: 'none' }}
              selectedKeys={[catFilter || '__all__']}
              onClick={({ key }) => setCatFilter(key === '__all__' ? undefined : key)}
              items={[
                { key: '__all__', label: '全部分类' },
                ...categories.map((c) => ({
                  key: c.id,
                  label: (
                    <Space style={{ justifyContent: 'space-between', width: '100%' }}>
                      <span>{c.name}</span>
                      <Text type="secondary" style={{ fontSize: 12 }}>{c.keyword_count}</Text>
                    </Space>
                  ),
                })),
              ]}
            />
          </Card>
        </Col>

        {/* 右：关键词 */}
        <Col xs={24} sm={16} md={18} lg={19}>
          <Card styles={{ body: { padding: 12 } }}>
            <Space wrap style={{ marginBottom: 12 }}>
              <Select
                allowClear placeholder="安全词状态" style={{ width: 130 }}
                value={safeFilter}
                onChange={(v) => setSafeFilter(v)}
                options={[{ value: true, label: '仅安全词' }, { value: false, label: '仅禁用词' }]}
              />
              <Input.Search
                placeholder="搜索关键词" style={{ width: 200 }}
                value={q} onChange={(e) => setQ(e.target.value)} onSearch={() => fetchKeywords(1)} allowClear
              />
            </Space>
            <Table<KeywordRow>
              size="small"
              rowKey="id"
              loading={loading}
              columns={columns}
              dataSource={rows}
              scroll={{ x: 880 }}
              pagination={{
                current: page, total, pageSize: 20, showSizeChanger: false,
                onChange: (p) => fetchKeywords(p), showTotal: (t) => `共 ${t} 个词`,
              }}
            />
          </Card>
        </Col>
      </Row>

      {/* 新建/编辑词 */}
      <Modal
        title={editing ? '编辑词' : '新建词'}
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        onOk={submit}
        confirmLoading={saving}
        okText="保存"
        cancelText="取消"
        destroyOnHidden
      >
        <Form form={form} layout="vertical">
          <Form.Item name="text" label="关键词" rules={[{ required: true, message: '请输入关键词' }]}>
            <Input placeholder="如：运动相机自拍杆" maxLength={128} />
          </Form.Item>
          <Form.Item name="category_id" label="分类" rules={[{ required: true, message: '请选择分类' }]}>
            <Select
              placeholder="选择分类"
              options={categories.map((c) => ({ value: c.id, label: c.name }))}
            />
          </Form.Item>
          <Form.Item name="pdd_mode" label="采集模式">
            <Select options={MODE_OPTIONS} />
          </Form.Item>
          <Row gutter={16}>
            <Col span={8}>
              <Form.Item name="pdd_safe" label="PDD 自动" valuePropName="checked">
                <Switch checkedChildren="开" unCheckedChildren="关" />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="xianyu_safe" label="闲鱼 自动" valuePropName="checked">
                <Switch checkedChildren="开" unCheckedChildren="关" />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="schedule_enabled" label="纳入调度" valuePropName="checked">
                <Switch />
              </Form.Item>
            </Col>
          </Row>
        </Form>
      </Modal>

      {/* 新建分类 */}
      <Modal
        title="新建分类"
        open={catModalOpen}
        onCancel={() => setCatModalOpen(false)}
        onOk={createCategory}
        okText="创建"
        cancelText="取消"
        destroyOnHidden
      >
        <Form form={catForm} layout="vertical">
          <Form.Item name="name" label="分类名称" rules={[{ required: true, message: '请输入分类名称' }]}>
            <Input placeholder="如：相机配件" maxLength={64} />
          </Form.Item>
          <Form.Item name="slug" label="slug（选填，留空自动生成）">
            <Input placeholder="如：camera-accessories" maxLength={64} />
          </Form.Item>
        </Form>
      </Modal>
    </Space>
  );
};

export default PddKeywords;
