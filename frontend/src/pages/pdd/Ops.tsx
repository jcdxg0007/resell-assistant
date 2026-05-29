import React, { useState, useEffect, useCallback } from 'react';
import {
  Typography, Card, Row, Col, Statistic, Table, Tag, Space, Button,
  Badge, Alert, Select, Tooltip, Empty,
} from 'antd';
import {
  ReloadOutlined, ThunderboltOutlined, CheckCircleOutlined,
  WarningOutlined, ShoppingOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import api from '../../services/api';

const { Title, Paragraph, Text } = Typography;

interface RunRow {
  id: string;
  task_id: string | null;
  source: string;
  keyword_text: string;
  category_name: string | null;
  mode: string | null;
  status: string;
  items_count: number;
  price_min: number | null;
  price_median: number | null;
  risk_signals: string[];
  device_serial: string | null;
  account_name: string | null;
  elapsed_ms: number | null;
  error: string | null;
  created_at: string | null;
}

interface TrendDay {
  date: string;
  total: number;
  ok: number;
  risk_blocked: number;
  failed: number;
  empty: number;
}

interface Summary {
  today: {
    total: number;
    by_status: Record<string, number>;
    items_total: number;
    success_rate: number | null;
    risk_blocked: number;
  };
  trend: TrendDay[];
  recent: RunRow[];
  recent_risk: RunRow[];
  worker: { online: boolean; devices?: string[]; ts?: string };
}

const STATUS_META: Record<string, { color: string; label: string }> = {
  ok: { color: 'success', label: '成功' },
  empty: { color: 'default', label: '空结果' },
  partial: { color: 'gold', label: '部分' },
  failed: { color: 'error', label: '失败' },
  risk_blocked: { color: 'volcano', label: '风控拦截' },
  timeout: { color: 'orange', label: '超时' },
};

const SOURCE_LABEL: Record<string, string> = {
  lib: '词库轮播',
  selection: '选品流程',
  manual: '手动',
  emergency: '紧急',
};

const statusTag = (s: string) => {
  const m = STATUS_META[s] || { color: 'default', label: s };
  return <Tag color={m.color}>{m.label}</Tag>;
};

const fmtTime = (iso: string | null) => {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString('zh-CN', { hour12: false });
};

const PddOps: React.FC = () => {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [rows, setRows] = useState<RunRow[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize] = useState(20);
  const [statusFilter, setStatusFilter] = useState<string | undefined>(undefined);
  const [loading, setLoading] = useState(false);
  const [tableLoading, setTableLoading] = useState(false);

  const fetchSummary = useCallback(async () => {
    setLoading(true);
    try {
      const res = await api.get('/pdd-runs/summary');
      setSummary(res.data);
    } catch {
      /* ignore */
    }
    setLoading(false);
  }, []);

  const fetchRuns = useCallback(async () => {
    setTableLoading(true);
    try {
      const res = await api.get('/pdd-runs/', {
        params: {
          status: statusFilter,
          limit: pageSize,
          offset: (page - 1) * pageSize,
        },
      });
      setRows(res.data.items || []);
      setTotal(res.data.total || 0);
    } catch {
      /* ignore */
    }
    setTableLoading(false);
  }, [statusFilter, page, pageSize]);

  useEffect(() => { fetchSummary(); }, [fetchSummary]);
  useEffect(() => { fetchRuns(); }, [fetchRuns]);

  const refreshAll = () => { fetchSummary(); fetchRuns(); };

  const maxTrend = Math.max(1, ...(summary?.trend || []).map((d) => d.total));

  const columns: ColumnsType<RunRow> = [
    { title: '时间', dataIndex: 'created_at', width: 170, render: fmtTime },
    {
      title: '关键词', dataIndex: 'keyword_text', width: 160,
      render: (t: string, r) => (
        <Space size={4} direction="vertical">
          <Text strong>{t}</Text>
          {r.category_name && <Text type="secondary" style={{ fontSize: 12 }}>{r.category_name}</Text>}
        </Space>
      ),
    },
    { title: '状态', dataIndex: 'status', width: 100, render: statusTag },
    {
      title: '商品数', dataIndex: 'items_count', width: 80,
      render: (n: number) => <Text>{n}</Text>,
    },
    {
      title: '价格(最低/中位)', width: 130,
      render: (_: unknown, r) => (
        r.price_min != null
          ? <Text>¥{r.price_min} / ¥{r.price_median ?? '—'}</Text>
          : <Text type="secondary">—</Text>
      ),
    },
    {
      title: '来源', dataIndex: 'source', width: 90,
      render: (s: string) => <Tag>{SOURCE_LABEL[s] || s}</Tag>,
    },
    { title: '模式', dataIndex: 'mode', width: 70, render: (m: string | null) => m || '—' },
    {
      title: '设备', dataIndex: 'device_serial', width: 120,
      render: (d: string | null) => d ? <Text code style={{ fontSize: 12 }}>{d}</Text> : '—',
    },
    {
      title: '耗时', dataIndex: 'elapsed_ms', width: 80,
      render: (ms: number | null) => ms != null ? `${(ms / 1000).toFixed(1)}s` : '—',
    },
    {
      title: '风控/错误', width: 160,
      render: (_: unknown, r) => {
        if (r.risk_signals && r.risk_signals.length) {
          return <Tag color="volcano">{r.risk_signals.join(', ')}</Tag>;
        }
        return r.error ? <Tooltip title={r.error}><Text type="danger" ellipsis style={{ maxWidth: 140 }}>{r.error}</Text></Tooltip> : '—';
      },
    },
  ];

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Row justify="space-between" align="middle">
        <Col>
          <Title level={3} style={{ marginBottom: 0 }}>PDD 采集监控</Title>
          <Paragraph type="secondary" style={{ marginBottom: 0 }}>
            采集任务历史、成功率与风控告警。数据来自 pdd_search_runs 落库。
          </Paragraph>
        </Col>
        <Col>
          <Space>
            <Badge
              status={summary?.worker?.online ? 'success' : 'error'}
              text={summary?.worker?.online
                ? `Worker 在线${summary.worker.devices?.length ? `（${summary.worker.devices.join(', ')}）` : ''}`
                : 'Worker 离线'}
            />
            <Button icon={<ReloadOutlined />} onClick={refreshAll} loading={loading}>刷新</Button>
          </Space>
        </Col>
      </Row>

      {/* 今日核心指标（近 24h） */}
      <Row gutter={[16, 16]}>
        <Col xs={12} sm={6}>
          <Card loading={loading}>
            <Statistic title="今日任务" value={summary?.today.total ?? 0} prefix={<ThunderboltOutlined />} />
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card loading={loading}>
            <Statistic
              title="成功率"
              value={summary?.today.success_rate ?? 0}
              suffix="%"
              precision={1}
              prefix={<CheckCircleOutlined />}
              valueStyle={{ color: (summary?.today.success_rate ?? 0) >= 80 ? '#52c41a' : '#faad14' }}
            />
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card loading={loading}>
            <Statistic title="抓到商品" value={summary?.today.items_total ?? 0} prefix={<ShoppingOutlined />} />
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card loading={loading}>
            <Statistic
              title="今日风控命中"
              value={summary?.today.risk_blocked ?? 0}
              prefix={<WarningOutlined />}
              valueStyle={{ color: (summary?.today.risk_blocked ?? 0) > 0 ? '#cf1322' : undefined }}
            />
          </Card>
        </Col>
      </Row>

      {/* 风控告警 */}
      {summary?.recent_risk && summary.recent_risk.length > 0 && (
        <Alert
          type="error"
          showIcon
          message={`近 24h 有 ${summary.recent_risk.length} 次风控拦截，请关注`}
          description={
            <Space direction="vertical" size={2}>
              {summary.recent_risk.map((r) => (
                <Text key={r.id}>
                  {fmtTime(r.created_at)} · <Text strong>{r.keyword_text}</Text>
                  {r.risk_signals?.length ? ` · ${r.risk_signals.join(', ')}` : ''}
                </Text>
              ))}
            </Space>
          }
        />
      )}

      {/* 近 7 天趋势 */}
      <Card title="近 7 天趋势" loading={loading}>
        {summary?.trend && summary.trend.length > 0 ? (
          <Space direction="vertical" size={10} style={{ width: '100%' }}>
            {summary.trend.map((d) => (
              <Row key={d.date} align="middle" gutter={12}>
                <Col flex="90px"><Text type="secondary">{d.date.slice(5)}</Text></Col>
                <Col flex="auto">
                  <div style={{ display: 'flex', height: 18, borderRadius: 4, overflow: 'hidden', background: '#f5f5f5', width: `${Math.max(8, (d.total / maxTrend) * 100)}%` }}>
                    <div style={{ flex: d.ok, background: '#52c41a' }} title={`成功 ${d.ok}`} />
                    <div style={{ flex: d.empty, background: '#d9d9d9' }} title={`空 ${d.empty}`} />
                    <div style={{ flex: d.failed, background: '#faad14' }} title={`失败 ${d.failed}`} />
                    <div style={{ flex: d.risk_blocked, background: '#cf1322' }} title={`风控 ${d.risk_blocked}`} />
                  </div>
                </Col>
                <Col flex="120px">
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    共{d.total} · 成{d.ok}{d.risk_blocked ? ` · 风控${d.risk_blocked}` : ''}
                  </Text>
                </Col>
              </Row>
            ))}
            <Space size={16} style={{ marginTop: 4 }}>
              <Text type="secondary" style={{ fontSize: 12 }}><span style={{ color: '#52c41a' }}>■</span> 成功</Text>
              <Text type="secondary" style={{ fontSize: 12 }}><span style={{ color: '#d9d9d9' }}>■</span> 空结果</Text>
              <Text type="secondary" style={{ fontSize: 12 }}><span style={{ color: '#faad14' }}>■</span> 失败</Text>
              <Text type="secondary" style={{ fontSize: 12 }}><span style={{ color: '#cf1322' }}>■</span> 风控</Text>
            </Space>
          </Space>
        ) : (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有采集记录" />
        )}
      </Card>

      {/* 任务流水 */}
      <Card
        title="任务流水"
        extra={
          <Select
            allowClear
            placeholder="按状态筛选"
            style={{ width: 160 }}
            value={statusFilter}
            onChange={(v) => { setStatusFilter(v); setPage(1); }}
            options={Object.entries(STATUS_META).map(([k, v]) => ({ value: k, label: v.label }))}
          />
        }
      >
        <Table<RunRow>
          rowKey="id"
          size="small"
          loading={tableLoading}
          dataSource={rows}
          columns={columns}
          scroll={{ x: 1100 }}
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: false,
            onChange: setPage,
            showTotal: (t) => `共 ${t} 条`,
          }}
        />
      </Card>
    </Space>
  );
};

export default PddOps;
