import React, { useState, useEffect, useCallback } from 'react';
import {
  Typography, Table, Card, Input, Button, Space, Tag, Row, Col,
  Progress, App, Alert, Modal, Descriptions, Badge, Drawer, Tooltip,
} from 'antd';
import {
  SearchOutlined, ReloadOutlined, ControlOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import api from '../../services/api';
import PddRhythmConfig from '../pdd/Config';

const { Title, Text } = Typography;

// ── 闲鱼侧：选品推荐 ──────────────────────────────────────────
interface ProductItem {
  product: {
    id: string;
    title: string;
    source_platform: string;
    price: number;
    category: string | null;
    image_urls: string[] | null;
  };
  score: {
    total_score: number;
    decision: string;
    decision_label: string;
    dimensions: Record<string, { score: number; max: number; label: string }>;
    scored_at: string | null;
  } | null;
}

const decisionColors: Record<string, string> = {
  strong_recommend: 'green',
  worth_try: 'blue',
  average: 'orange',
  skip: 'red',
};

// ── PDD 侧：采集任务历史 ──────────────────────────────────────
interface PddRun {
  id: string;
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
  elapsed_ms: number | null;
  error: string | null;
  created_at: string | null;
}

interface PddSummary {
  today: {
    total: number;
    items_total: number;
    success_rate: number | null;
    risk_blocked: number;
  };
  recent_risk: PddRun[];
  worker: { online: boolean; devices?: string[] };
}

const PDD_STATUS_META: Record<string, { color: string; label: string }> = {
  ok: { color: 'success', label: '成功' },
  empty: { color: 'default', label: '空结果' },
  partial: { color: 'gold', label: '部分' },
  failed: { color: 'error', label: '失败' },
  risk_blocked: { color: 'volcano', label: '风控拦截' },
  timeout: { color: 'orange', label: '超时' },
};

const pddStatusTag = (s: string) => {
  const m = PDD_STATUS_META[s] || { color: 'default', label: s };
  return <Tag color={m.color}>{m.label}</Tag>;
};

const fmtTime = (iso: string | null) => {
  if (!iso) return '—';
  return new Date(iso).toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
};

const MultiPlatformCompare: React.FC = () => {
  const { message } = App.useApp();

  // 闲鱼推荐
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState<ProductItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);

  // 搜索
  const [keyword, setKeyword] = useState('');
  const [searchLoading, setSearchLoading] = useState(false);
  const [searchResult, setSearchResult] = useState<'success' | 'error' | null>(null);
  const [searchKeyword, setSearchKeyword] = useState('');

  // 详情
  const [detailVisible, setDetailVisible] = useState(false);
  const [detailItem, setDetailItem] = useState<ProductItem | null>(null);

  // PDD
  const [pddSummary, setPddSummary] = useState<PddSummary | null>(null);
  const [pddRuns, setPddRuns] = useState<PddRun[]>([]);
  const [pddLoading, setPddLoading] = useState(false);

  // 采集节奏控制窗口
  const [rhythmOpen, setRhythmOpen] = useState(false);

  const fetchRecommendations = useCallback(async (p: number = 1) => {
    setLoading(true);
    try {
      const res = await api.get('/selection/xianyu/recommendations', {
        params: { page: p, page_size: 20, min_score: 0 },
      });
      setData(res.data.items || []);
      setTotal(res.data.total || 0);
      setPage(p);
    } catch (err) {
      const e = err as { response?: { status?: number } };
      if (e.response?.status !== 401) message.error('加载闲鱼数据失败');
    } finally {
      setLoading(false);
    }
  }, [message]);

  const fetchPdd = useCallback(async () => {
    setPddLoading(true);
    try {
      const [sumRes, runsRes] = await Promise.all([
        api.get('/pdd-runs/summary'),
        api.get('/pdd-runs/', { params: { limit: 20, offset: 0 } }),
      ]);
      setPddSummary(sumRes.data);
      setPddRuns(runsRes.data.items || []);
    } catch {
      /* ignore */
    }
    setPddLoading(false);
  }, []);

  useEffect(() => { fetchRecommendations(); }, [fetchRecommendations]);
  useEffect(() => { fetchPdd(); }, [fetchPdd]);

  const refreshAll = () => { fetchRecommendations(page); fetchPdd(); };

  const handleSearch = async () => {
    if (!keyword.trim()) {
      message.warning('请输入搜索关键词');
      return;
    }
    setSearchLoading(true);
    setSearchResult(null);
    try {
      await api.post('/products/search', { keyword: keyword.trim(), platform: 'xianyu' });
      setSearchKeyword(keyword.trim());
      setSearchResult('success');
      message.success('闲鱼搜索任务已提交');
    } catch {
      setSearchResult('error');
      message.error('搜索任务提交失败');
    } finally {
      setSearchLoading(false);
    }
  };

  const showDetail = (item: ProductItem) => {
    setDetailItem(item);
    setDetailVisible(true);
  };

  // ── 闲鱼表格（紧凑）──────────────────────────────────────
  const xianyuColumns: ColumnsType<ProductItem> = [
    {
      title: '商品',
      dataIndex: ['product', 'title'],
      ellipsis: true,
      render: (title: string, record: ProductItem) => (
        <Space size={6}>
          {record.product.image_urls?.[0] && (
            <img src={record.product.image_urls[0]} alt="" style={{ width: 32, height: 32, objectFit: 'cover', borderRadius: 4 }} />
          )}
          <Text ellipsis style={{ maxWidth: 150 }}>{title}</Text>
        </Space>
      ),
    },
    {
      title: '采购价',
      dataIndex: ['product', 'price'],
      width: 80,
      sorter: (a, b) => a.product.price - b.product.price,
      render: (v: number) => <Text>¥{v?.toFixed(2)}</Text>,
    },
    {
      title: '评分',
      dataIndex: ['score', 'total_score'],
      width: 70,
      sorter: (a, b) => (a.score?.total_score || 0) - (b.score?.total_score || 0),
      defaultSortOrder: 'descend',
      render: (score: number, record: ProductItem) => {
        if (!score) return <Text type="secondary">—</Text>;
        const color = score >= 80 ? '#52c41a' : score >= 60 ? '#1677ff' : score >= 40 ? '#faad14' : '#ff4d4f';
        return (
          <Tooltip title={record.score?.decision_label}>
            <Text strong style={{ color, cursor: 'pointer' }} onClick={() => showDetail(record)}>{score}</Text>
          </Tooltip>
        );
      },
    },
    {
      title: '判定',
      dataIndex: ['score', 'decision'],
      width: 88,
      render: (decision: string, record: ProductItem) =>
        decision ? <Tag color={decisionColors[decision]}>{record.score?.decision_label}</Tag> : '—',
    },
  ];

  // ── PDD 表格（紧凑）──────────────────────────────────────
  const pddColumns: ColumnsType<PddRun> = [
    {
      title: '关键词',
      dataIndex: 'keyword_text',
      ellipsis: true,
      render: (t: string, r) => (
        <Space size={2} direction="vertical">
          <Text ellipsis style={{ maxWidth: 150 }}>{t}</Text>
          {r.category_name && <Text type="secondary" style={{ fontSize: 11 }}>{r.category_name}</Text>}
        </Space>
      ),
    },
    { title: '状态', dataIndex: 'status', width: 90, render: pddStatusTag },
    { title: '商品', dataIndex: 'items_count', width: 56 },
    {
      title: '最低/中位价',
      width: 110,
      render: (_: unknown, r) =>
        r.price_min != null
          ? <Text>¥{r.price_min}/¥{r.price_median ?? '—'}</Text>
          : <Text type="secondary">—</Text>,
    },
    { title: '时间', dataIndex: 'created_at', width: 96, render: fmtTime },
  ];

  const worker = pddSummary?.worker;

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      {/* 标题 + 工具栏 */}
      <Row justify="space-between" align="middle">
        <Col><Title level={4} style={{ margin: 0 }}>多平台比价</Title></Col>
        <Col>
          <Space>
            <Badge
              status={worker?.online ? 'success' : 'error'}
              text={worker?.online
                ? `PDD Worker 在线${worker.devices?.length ? `（${worker.devices.join(', ')}）` : ''}`
                : 'PDD Worker 离线'}
            />
            <Button icon={<ControlOutlined />} onClick={() => setRhythmOpen(true)}>采集节奏</Button>
            <Button icon={<ReloadOutlined />} onClick={refreshAll}>刷新</Button>
          </Space>
        </Col>
      </Row>

      {/* 搜索 */}
      <Card styles={{ body: { padding: 12 } }}>
        <Input.Search
          placeholder="搜索商品关键词或粘贴链接（提交闲鱼采集；拼多多由词库轮播自动采集）"
          enterButton={<><SearchOutlined /> 搜索</>}
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          onSearch={handleSearch}
          loading={searchLoading}
        />
        {searchResult === 'success' && (
          <Alert
            style={{ marginTop: 12 }}
            message={`闲鱼搜索任务已提交：「${searchKeyword}」，完成后点刷新查看`}
            type="success" showIcon closable onClose={() => setSearchResult(null)}
            action={<Button size="small" onClick={() => fetchRecommendations(1)}>刷新</Button>}
          />
        )}
        {searchResult === 'error' && (
          <Alert style={{ marginTop: 12 }} message="搜索任务提交失败，请稍后重试" type="error" showIcon closable onClose={() => setSearchResult(null)} />
        )}
      </Card>

      {/* PDD 采集概况（紧凑条）+ 风控告警 */}
      <Card styles={{ body: { padding: '10px 16px' } }} loading={pddLoading}>
        <Space size="large" wrap>
          <Text type="secondary">PDD 今日</Text>
          <Text>任务 <Text strong>{pddSummary?.today.total ?? 0}</Text></Text>
          <Text>成功率 <Text strong style={{ color: (pddSummary?.today.success_rate ?? 0) >= 80 ? '#52c41a' : '#faad14' }}>{pddSummary?.today.success_rate ?? 0}%</Text></Text>
          <Text>抓到商品 <Text strong>{pddSummary?.today.items_total ?? 0}</Text></Text>
          <Text>风控命中 <Text strong style={{ color: (pddSummary?.today.risk_blocked ?? 0) > 0 ? '#cf1322' : undefined }}>{pddSummary?.today.risk_blocked ?? 0}</Text></Text>
        </Space>
      </Card>
      {pddSummary?.recent_risk && pddSummary.recent_risk.length > 0 && (
        <Alert
          type="error" showIcon
          message={`PDD 近 24h 有 ${pddSummary.recent_risk.length} 次风控拦截，请关注`}
          description={
            <Space direction="vertical" size={2}>
              {pddSummary.recent_risk.map((r) => (
                <Text key={r.id}>{fmtTime(r.created_at)} · <Text strong>{r.keyword_text}</Text>{r.risk_signals?.length ? ` · ${r.risk_signals.join(', ')}` : ''}</Text>
              ))}
            </Space>
          }
        />
      )}

      {/* 左右并排：闲鱼 vs 拼多多 */}
      <Row gutter={16}>
        <Col xs={24} xl={12}>
          <Card
            title={<Space><Tag color="gold">闲鱼</Tag>采集结果</Space>}
            extra={<Text type="secondary">共 {total}</Text>}
            styles={{ body: { padding: 12 } }}
          >
            <Table<ProductItem>
              size="small"
              columns={xianyuColumns}
              dataSource={data}
              rowKey={(r) => r.product.id}
              loading={loading}
              pagination={{
                current: page, total, pageSize: 20, size: 'small',
                onChange: (p) => fetchRecommendations(p),
                showSizeChanger: false,
              }}
              locale={{ emptyText: '暂无闲鱼选品数据' }}
            />
          </Card>
        </Col>
        <Col xs={24} xl={12}>
          <Card
            title={<Space><Tag color="red">拼多多</Tag>采集结果</Space>}
            extra={<Text type="secondary">最近 {pddRuns.length} 条</Text>}
            styles={{ body: { padding: 12 } }}
          >
            <Table<PddRun>
              size="small"
              columns={pddColumns}
              dataSource={pddRuns}
              rowKey="id"
              loading={pddLoading}
              pagination={{ pageSize: 20, size: 'small', showSizeChanger: false }}
              locale={{ emptyText: '暂无 PDD 采集记录，跑一波词库轮播后刷新' }}
            />
          </Card>
        </Col>
      </Row>

      {/* 采集节奏控制窗口 */}
      <Drawer
        title="PDD 采集节奏控制"
        width={560}
        open={rhythmOpen}
        onClose={() => setRhythmOpen(false)}
        destroyOnClose
      >
        <PddRhythmConfig embedded />
      </Drawer>

      {/* 闲鱼评分详情 */}
      <Modal
        title="商品评分详情"
        open={detailVisible}
        onCancel={() => setDetailVisible(false)}
        footer={null}
        width={640}
      >
        {detailItem && (
          <>
            <Descriptions column={2} bordered size="small" style={{ marginBottom: 16 }}>
              <Descriptions.Item label="商品名称" span={2}>{detailItem.product.title}</Descriptions.Item>
              <Descriptions.Item label="采购价">¥{detailItem.product.price}</Descriptions.Item>
              <Descriptions.Item label="综合评分">
                <Text strong style={{ fontSize: 18 }}>{detailItem.score?.total_score || '—'}</Text>/100
              </Descriptions.Item>
            </Descriptions>
            {detailItem.score?.dimensions && (
              <Card title="十维度评分" size="small">
                {Object.entries(detailItem.score.dimensions).map(([name, dim]) => (
                  <div key={name} style={{ display: 'flex', alignItems: 'center', marginBottom: 8 }}>
                    <Text style={{ width: 140 }}>{name}</Text>
                    <Progress
                      percent={Math.round((dim.score / dim.max) * 100)}
                      size="small"
                      style={{ flex: 1, marginRight: 12 }}
                      format={() => `${dim.score}/${dim.max}`}
                    />
                    <Tag>{dim.label}</Tag>
                  </div>
                ))}
              </Card>
            )}
          </>
        )}
      </Modal>
    </Space>
  );
};

export default MultiPlatformCompare;
